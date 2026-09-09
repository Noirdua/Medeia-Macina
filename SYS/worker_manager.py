"""Worker task management with persistent database storage.

Manages worker tasks for downloads, searches, imports, etc. with automatic
persistence to database and optional auto-refresh callbacks.
"""

import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable, Tuple
from datetime import datetime
from threading import Thread, Lock, Event
import time

from SYS.logger import log
from SYS.database import (
    db,
    insert_worker,
    update_worker,
    append_worker_stdout,
    get_worker_stdout as db_get_worker_stdout,
    get_active_workers as db_get_active_workers,
    get_worker as db_get_worker,
    expire_running_workers as db_expire_running_workers
)

logger = logging.getLogger(__name__)


class Worker:
    """Represents a single worker task with state management."""

    def __init__(
        self,
        worker_id: str,
        worker_type: str,
        title: str = "",
        description: str = "",
        manager: Optional["WorkerManager"] = None,
    ):
        """Initialize a worker.

        Args:
            worker_id: Unique identifier for this worker
            worker_type: Type of work (e.g., 'download', 'search', 'import')
            title: Human-readable title
            description: Detailed description
            manager: Reference to parent WorkerManager for state updates
        """
        self.id = worker_id
        self.type = worker_type
        self.title = title or worker_type
        self.description = description
        self.manager = manager
        self.status = "running"
        self.progress = ""
        self.details = ""
        self.error_message = ""
        self.result = "pending"
        self._stdout_buffer: list[str] = []
        self._steps_buffer: list[str] = []

    def log_step(self, step_text: str) -> None:
        """Log a step for this worker.

        Args:
            step_text: Text describing the step
        """
        try:
            if self.manager:
                self.manager.log_step(self.id, step_text)
            else:
                logger.info(f"[{self.id}] {step_text}")
        except Exception as e:
            logger.error(f"Error logging step for worker {self.id}: {e}")

    def append_stdout(self, text: str) -> None:
        """Append text to stdout log.

        Args:
            text: Text to append
        """
        try:
            if self.manager:
                self.manager.append_stdout(self.id, text)
            else:
                self._stdout_buffer.append(text)
        except Exception as e:
            logger.error(f"Error appending stdout for worker {self.id}: {e}")

    def get_stdout(self) -> str:
        """Get all stdout for this worker.

        Returns:
            Complete stdout text
        """
        try:
            if self.manager:
                return self.manager.get_stdout(self.id)
            else:
                return "\n".join(self._stdout_buffer)
        except Exception as e:
            logger.error(f"Error getting stdout for worker {self.id}: {e}")
            return ""

    def get_steps(self) -> str:
        """Get all steps for this worker.

        Returns:
            Complete steps text
        """
        try:
            if self.manager:
                return self.manager.get_steps(self.id)
            else:
                return "\n".join(self._steps_buffer)
        except Exception as e:
            logger.error(f"Error getting steps for worker {self.id}: {e}")
            return ""

    def update_progress(self, progress: float | str = 0.0, details: str = "") -> None:
        """Update worker progress.

        Args:
            progress: Progress value (float) or textual like "50%"; will be coerced to float
            details: Additional details
        """
        self.progress = str(progress)
        self.details = details
        try:
            if self.manager:
                # Normalize to a float value for the manager API (0-100)
                try:
                    if isinstance(progress, str) and progress.endswith('%'):
                        progress_value = float(progress.rstrip('%'))
                    else:
                        progress_value = float(progress)
                except Exception:
                    progress_value = 0.0
                self.manager.update_worker(self.id, progress_value, details)
        except Exception as e:
            logger.error(f"Error updating worker {self.id}: {e}")

    def finish(self, result: str = "completed", message: str = "") -> None:
        """Mark worker as finished.

        Args:
            result: Result status ('completed', 'error', 'cancelled')
            message: Result message/error details
        """
        self.result = result
        self.status = "finished"
        self.error_message = message
        try:
            if self.manager:
                # Flush and disable logging handler before marking finished
                self.manager.disable_logging_for_worker(self.id)
                # Then mark as finished in database
                self.manager.finish_worker(self.id, result, message)
        except Exception as e:
            logger.error(f"Error finishing worker {self.id}: {e}")


class WorkerLoggingHandler(logging.StreamHandler):
    """Custom logging handler that captures logs for a worker."""

    def __init__(
        self,
        worker_id: str,
        manager: Optional["WorkerManager"] = None,
        buffer_size: int = 50,
    ):
        """Initialize the handler.

        Args:
            worker_id: ID of the worker to capture logs for
            buffer_size: Number of logs to buffer before flushing to DB
        """
        super().__init__()
        self.worker_id = worker_id
        self.manager = manager
        self.buffer_size = buffer_size
        self.buffer: list[str] = []
        self._lock = Lock()

        # Set a format that includes timestamp and level
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        self.setFormatter(formatter)

    def emit(self, record):
        """Emit a log record."""
        try:
            # Try to format the record normally
            try:
                msg = self.format(record)
            except (TypeError, ValueError):
                # If formatting fails (e.g., %d format with non-int arg),
                # build message manually without calling getMessage()
                try:
                    # Try to format with args if possible
                    if record.args:
                        msg = record.msg % record.args
                    else:
                        msg = record.msg
                except (TypeError, ValueError):
                    # If that fails too, just use the raw message string
                    msg = str(record.msg)

                # Add timestamp and level if not already in message
                import time

                timestamp = time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(record.created)
                )
                msg = f"{timestamp} - {record.name} - {record.levelname} - {msg}"

            with self._lock:
                self.buffer.append(msg)

                # Flush to DB when buffer reaches size
                if len(self.buffer) >= self.buffer_size:
                    self._flush()
        except Exception:
            self.handleError(record)

    def _flush(self):
        """Flush buffered logs to database."""
        if self.buffer:
            log_text = "\n".join(self.buffer)
            try:
                if self.manager:
                    self.manager.append_stdout(
                        self.worker_id,
                        log_text,
                        channel="log"
                    )
                else:
                    append_worker_stdout(
                        self.worker_id,
                        log_text,
                        channel="log"
                    )
            except Exception as e:
                # If we can't write to DB, at least log it
                log(f"Error flushing worker logs: {e}")
            self.buffer = []

    def flush(self):
        """Flush any buffered records."""
        with self._lock:
            self._flush()
        super().flush()

    def close(self):
        """Close the handler."""
        self.flush()
        super().close()


_DEFAULT_STDOUT_FLUSH_BYTES = 4096
_DEFAULT_STDOUT_FLUSH_INTERVAL = 0.75
_REFRESH_THREAD_JOIN_TIMEOUT = 5


class WorkerManager:
    """Manages persistent worker tasks using the central medios.db."""

    def __init__(self, auto_refresh_interval: float = 2.0):
        """Initialize the worker manager.

        Args:
            auto_refresh_interval: Seconds between auto-refresh checks (0 = disabled)
        """
        self.auto_refresh_interval = auto_refresh_interval
        self.refresh_callbacks: List[Callable] = []
        self.refresh_thread: Optional[Thread] = None
        self._stop_refresh = False
        self._refresh_stop_event = Event()
        self._lock = Lock()
        self.worker_handlers: Dict[str, WorkerLoggingHandler] = {}
        self._worker_last_step: Dict[str, str] = {}
        self._db_lock = Lock()
        
        # Buffered stdout/log batching to reduce DB lock contention.
        self._stdout_buffers: Dict[Tuple[str, str], List[str]] = {}
        self._stdout_buffer_sizes: Dict[Tuple[str, str], int] = {}
        self._stdout_buffer_steps: Dict[Tuple[str, str], Optional[str]] = {}
        self._stdout_last_flush: Dict[Tuple[str, str], float] = {}
        self._stdout_flush_bytes = _DEFAULT_STDOUT_FLUSH_BYTES
        self._stdout_flush_interval = _DEFAULT_STDOUT_FLUSH_INTERVAL


    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - close database."""
        self.close()

    def add_refresh_callback(
        self,
        callback: Callable[[List[Dict[str,
                                      Any]]],
                           None]
    ) -> None:
        """Register a callback to be called on worker updates.

        Args:
            callback: Function that receives list of active workers
        """
        with self._lock:
            self.refresh_callbacks.append(callback)

    def expire_running_workers(
        self,
        older_than_seconds: int = 300,
        worker_id_prefix: Optional[str] = None,
        reason: Optional[str] = None,
        status: str = "error",
    ) -> int:
        """Mark stale running workers as finished.

        Args:
            older_than_seconds: Idle threshold before expiring.
            worker_id_prefix: Optional wildcard filter (e.g., 'cli_%').
            reason: Error message if none already exists.
            status: New status to apply.

        Returns:
            Count of workers updated.
        """
        try:
            return db_expire_running_workers(
                older_than_seconds=older_than_seconds,
                status=status,
                reason=reason or "Stale worker expired"
            )
        except Exception as exc:
            logger.error(f"Failed to expire stale workers: {exc}", exc_info=True)
            return 0

    def remove_refresh_callback(self, callback: Callable) -> None:
        """Remove a refresh callback.

        Args:
            callback: The callback function to remove
        """
        with self._lock:
            if callback in self.refresh_callbacks:
                self.refresh_callbacks.remove(callback)

    def enable_logging_for_worker(self,
                                  worker_id: str) -> Optional[WorkerLoggingHandler]:
        """Enable logging capture for a worker.

        Creates a logging handler that captures all logs for this worker.

        Args:
            worker_id: ID of the worker to capture logs for

        Returns:
            The logging handler that was created, or None if there was an error
        """
        try:
            handler = WorkerLoggingHandler(worker_id, manager=self)
            with self._lock:
                self.worker_handlers[worker_id] = handler

            # Add the handler to the root logger so it captures all logs
            root_logger = logging.getLogger()
            root_logger.addHandler(handler)
            root_logger.setLevel(logging.DEBUG)  # Capture all levels

            logger.debug(f"[WorkerManager] Enabled logging for worker: {worker_id}")
            return handler
        except Exception as e:
            logger.error(
                f"[WorkerManager] Error enabling logging for worker {worker_id}: {e}",
                exc_info=True
            )
            return None

    def disable_logging_for_worker(self, worker_id: str) -> None:
        """Disable logging capture for a worker and flush any pending logs.

        Args:
            worker_id: ID of the worker to stop capturing logs for
        """
        try:
            with self._lock:
                handler = self.worker_handlers.pop(worker_id, None)

            if handler:
                # Flush and close the handler
                handler.flush()
                handler.close()

                # Remove from root logger
                root_logger = logging.getLogger()
                root_logger.removeHandler(handler)

            # Flush any buffered stdout/log data for this worker
            try:
                self.flush_worker_stdout(worker_id)
            except Exception:
                logger.exception("Failed to flush worker stdout for '%s'", worker_id)

            logger.debug(
                f"[WorkerManager] Disabled logging for worker: {worker_id}"
            )
        except Exception as e:
            logger.error(
                f"[WorkerManager] Error disabling logging for worker {worker_id}: {e}",
                exc_info=True,
            )

    def track_worker(
        self,
        worker_id: str,
        worker_type: str,
        title: str = "",
        description: str = "",
        total_steps: int = 0,
        pipe: Optional[str] = None,
    ) -> bool:
        """Start tracking a new worker.

        Args:
            worker_id: Unique identifier for the worker
            worker_type: Type of worker (e.g., 'download', 'search', 'import')
            title: Worker title/name
            description: Worker description
            total_steps: Total number of steps for progress tracking
            pipe: Text of the originating pipe/prompt, if any

        Returns:
            True if worker was inserted successfully
        """
        try:
            success = insert_worker(
                worker_id,
                worker_type,
                title,
                description
            )
            if success:
                logger.debug(
                    f"[WorkerManager] Tracking worker: {worker_id} ({worker_type})"
                )
                self._start_refresh_if_needed()
                return True
            return False
        except Exception as e:
            logger.error(f"[WorkerManager] Error tracking worker: {e}", exc_info=True)
            return False

    def update_worker(
        self,
        worker_id: str,
        progress: float = 0.0,
        current_step: str = "",
        details: str = "",
        error: str = "",
    ) -> bool:
        """Update worker progress and status.

        Args:
            worker_id: Unique identifier for the worker
            progress: Progress percentage (0-100)
            current_step: Current step description
            details: Additional details
            error: Error message if any

        Returns:
            True if update was successful
        """
        try:
            kwargs: dict[str, Any] = {}
            if progress > 0:
                kwargs["progress"] = progress
            if current_step:
                kwargs["details"] = current_step
            if details:
                kwargs["description"] = details
            if error:
                kwargs["error_message"] = error

            if kwargs:
                if "details" in kwargs and kwargs["details"]:
                    self._worker_last_step[worker_id] = str(kwargs["details"])
                return update_worker(worker_id, **kwargs)
            return True
        except Exception as e:
            logger.error(
                f"[WorkerManager] Error updating worker {worker_id}: {e}",
                exc_info=True
            )
            return False

    def finish_worker(
        self,
        worker_id: str,
        result: str = "completed",
        error_msg: str = "",
        result_data: str = ""
    ) -> bool:
        """Mark a worker as finished.

        Args:
            worker_id: Unique identifier for the worker
            result: Result status ('completed', 'error', 'cancelled')
            error_msg: Error message if any
            result_data: Result data as JSON string (saved in details)

        Returns:
            True if update was successful
        """
        try:
            try:
                self.flush_worker_stdout(worker_id)
            except Exception:
                logger.exception("Failed to flush worker stdout for '%s' during finish", worker_id)
            kwargs = {
                "status": "finished",
                "result": result
            }
            if error_msg:
                kwargs["error_message"] = error_msg
            if result_data:
                kwargs["details"] = result_data

            success = update_worker(worker_id, **kwargs)
            logger.info(f"[WorkerManager] Worker finished: {worker_id} ({result})")
            self._worker_last_step.pop(worker_id, None)
            return success
        except Exception as e:
            logger.error(
                f"[WorkerManager] Error finishing worker {worker_id}: {e}",
                exc_info=True
            )
            return False

    def get_active_workers(self) -> List[Dict[str, Any]]:
        """Get all active (running) workers.

        Returns:
            List of active worker dictionaries
        """
        try:
            return db_get_active_workers()
        except Exception as e:
            logger.error(
                f"[WorkerManager] Error getting active workers: {e}",
                exc_info=True
            )
            return []

    def get_finished_workers(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get all finished workers (completed, errored, or cancelled).

        Args:
            limit: Maximum number of workers to retrieve

        Returns:
            List of finished worker dictionaries
        """
        try:
            # We don't have a get_all_workers in database.py yet, but we'll use a local query
            rows = db.fetchall(f"SELECT * FROM workers WHERE status = 'finished' ORDER BY updated_at DESC LIMIT {limit}")
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(
                f"[WorkerManager] Error getting finished workers: {e}",
                exc_info=True
            )
            return []

    def get_worker(self, worker_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific worker's data.

        Args:
            worker_id: Unique identifier for the worker

        Returns:
            Worker data or None if not found
        """
        try:
            return db_get_worker(worker_id)
        except Exception as e:
            logger.error(
                f"[WorkerManager] Error getting worker {worker_id}: {e}",
                exc_info=True
            )
            return None

    def get_worker_events(self,
                          worker_id: str,
                          limit: int = 500) -> List[Dict[str,
                                                         Any]]:
        """Fetch recorded worker timeline events."""
        try:
            with self._db_lock:
                rows = db.fetchall(
                    "SELECT content, channel, timestamp FROM worker_stdout WHERE worker_id = ? ORDER BY timestamp ASC LIMIT ?",
                    (worker_id, int(limit or 500)),
                ) or []
            events: List[Dict[str, Any]] = []
            for row in rows:
                payload = dict(row)
                events.append(
                    {
                        "message": payload.get("content"),
                        "channel": payload.get("channel") or "stdout",
                        "created_at": payload.get("timestamp"),
                        "step": self._worker_last_step.get(worker_id),
                    }
                )
            return events
        except Exception as e:
            logger.error(
                f"[WorkerManager] Error getting worker events for {worker_id}: {e}",
                exc_info=True,
            )
            return []

    def log_step(self, worker_id: str, step_text: str) -> bool:
        """Log a step to a worker's step history.

        Args:
            worker_id: Unique identifier for the worker
            step_text: Step description to log

        Returns:
            True if successful
        """
        try:
            step_value = str(step_text or "").strip()
            if not step_value:
                return True

            success = update_worker(worker_id, details=step_value)
            append_worker_stdout(worker_id, step_value, channel="step")
            if success:
                self._worker_last_step[worker_id] = step_value
            return success
        except Exception as e:
            logger.error(
                f"[WorkerManager] Error logging step for worker {worker_id}: {e}",
                exc_info=True
            )
            return False

    def _get_last_step(self, worker_id: str) -> Optional[str]:
        """Return the most recent step description for a worker."""
        return self._worker_last_step.get(worker_id)

    def get_steps(self, worker_id: str) -> str:
        """Get step logs for a worker.

        Args:
            worker_id: Unique identifier for the worker

        Returns:
            Steps text or empty string if not found
        """
        try:
            with self._db_lock:
                rows = db.fetchall(
                    "SELECT content FROM worker_stdout WHERE worker_id = ? AND channel = 'step' ORDER BY timestamp ASC",
                    (worker_id,),
                ) or []
            parts = [str(dict(row).get("content") or "").strip() for row in rows]
            parts = [part for part in parts if part]
            if parts:
                return "\n".join(parts)
            worker = db_get_worker(worker_id)
            if isinstance(worker, dict):
                return str(worker.get("details") or "")
            return ""
        except Exception as e:
            logger.error(
                f"[WorkerManager] Error getting steps for worker {worker_id}: {e}",
                exc_info=True
            )
            return ""

    def start_auto_refresh(self) -> None:
        """Start the auto-refresh thread for periodic worker updates."""
        if self.auto_refresh_interval <= 0:
            logger.debug("[WorkerManager] Auto-refresh disabled (interval <= 0)")
            return

        if self.refresh_thread and self.refresh_thread.is_alive():
            logger.debug("[WorkerManager] Auto-refresh already running")
            return

        logger.info(
            f"[WorkerManager] Starting auto-refresh with {self.auto_refresh_interval}s interval"
        )
        self._stop_refresh = False
        self._refresh_stop_event.clear()
        self.refresh_thread = Thread(target=self._auto_refresh_loop, daemon=True)
        self.refresh_thread.start()

    def stop_auto_refresh(self) -> None:
        """Stop the auto-refresh thread."""
        logger.info("[WorkerManager] Stopping auto-refresh")
        self._stop_refresh = True
        self._refresh_stop_event.set()
        if self.refresh_thread:
            self.refresh_thread.join(timeout=_REFRESH_THREAD_JOIN_TIMEOUT)
            self.refresh_thread = None

    def _start_refresh_if_needed(self) -> None:
        """Start auto-refresh if we have active workers and callbacks."""
        active = self.get_active_workers()
        if active and self.refresh_callbacks and not self._stop_refresh:
            self.start_auto_refresh()

    def _auto_refresh_loop(self) -> None:
        """Main auto-refresh loop that periodically queries and notifies."""
        try:
            while not self._stop_refresh:
                if self._refresh_stop_event.wait(self.auto_refresh_interval):
                    break

                # Check if there are active workers
                active = self.get_active_workers()

                if not active:
                    # No more active workers, stop refreshing
                    logger.debug(
                        "[WorkerManager] No active workers, stopping auto-refresh"
                    )
                    break

                # Call all registered callbacks with the active workers
                with self._lock:
                    for callback in self.refresh_callbacks:
                        try:
                            callback(active)
                        except Exception as e:
                            logger.error(
                                f"[WorkerManager] Error in refresh callback: {e}",
                                exc_info=True
                            )

        except Exception as e:
            logger.error(
                f"[WorkerManager] Error in auto-refresh loop: {e}",
                exc_info=True
            )
        finally:
            logger.debug("[WorkerManager] Auto-refresh loop ended")

    def cleanup_old_workers(self, days: int = 7) -> int:
        """Clean up completed/errored workers older than specified days.

        Args:
            days: Delete workers completed more than this many days ago

        Returns:
            Number of workers deleted
        """
        try:
            with self._db_lock:
                cutoff_days = max(0, int(days or 0))
                cutoff_expr = f"-{cutoff_days} days"
                db.execute(
                    "DELETE FROM worker_stdout WHERE worker_id IN (SELECT id FROM workers WHERE status != 'running' AND updated_at < datetime('now', ?))",
                    (cutoff_expr,),
                )
                cur = db.execute(
                    "DELETE FROM workers WHERE status != 'running' AND updated_at < datetime('now', ?)",
                    (cutoff_expr,),
                )
                count = int(getattr(cur, "rowcount", 0) or 0)
            if count > 0:
                logger.info(f"[WorkerManager] Cleaned up {count} old workers")
            return count
        except Exception as e:
            logger.error(
                f"[WorkerManager] Error cleaning up old workers: {e}",
                exc_info=True
            )
            return 0

    def append_stdout(self, worker_id: str, text: str, channel: str = "stdout") -> bool:
        """Append text to a worker's stdout log.

        Args:
            worker_id: Unique identifier for the worker
            text: Text to append
            channel: Logical channel (stdout, stderr, log, etc.)

        Returns:
            True if append was successful
        """
        if not text:
            return True

        now = time.monotonic()
        step_label = self._get_last_step(worker_id)
        key = (worker_id, channel)
        pending_flush: List[Tuple[str, str, Optional[str], str]] = []

        try:
            with self._lock:
                # Initialize last flush time for this buffer
                if key not in self._stdout_last_flush:
                    self._stdout_last_flush[key] = now

                current_step = self._stdout_buffer_steps.get(key)
                if current_step is None:
                    self._stdout_buffer_steps[key] = step_label
                    current_step = step_label

                # If step changes, flush existing buffer to keep step tags coherent
                if current_step != step_label:
                    buffered = "".join(self._stdout_buffers.get(key, []))
                    if buffered:
                        pending_flush.append((worker_id, channel, current_step, buffered))
                    self._stdout_buffers[key] = []
                    self._stdout_buffer_sizes[key] = 0
                    self._stdout_last_flush[key] = now
                    self._stdout_buffer_steps[key] = step_label

                buf = self._stdout_buffers.setdefault(key, [])
                buf.append(text)
                size = self._stdout_buffer_sizes.get(key, 0) + len(text)
                self._stdout_buffer_sizes[key] = size

                last_flush = self._stdout_last_flush.get(key, now)
                should_flush = (
                    size >= self._stdout_flush_bytes
                    or (now - last_flush) >= self._stdout_flush_interval
                )
                if should_flush:
                    buffered = "".join(self._stdout_buffers.get(key, []))
                    if buffered:
                        pending_flush.append(
                            (worker_id, channel, self._stdout_buffer_steps.get(key), buffered)
                        )
                    self._stdout_buffers[key] = []
                    self._stdout_buffer_sizes[key] = 0
                    self._stdout_last_flush[key] = now
                    self._stdout_buffer_steps[key] = None
        except Exception as e:
            logger.error(f"[WorkerManager] Error buffering stdout: {e}", exc_info=True)
            return False

        ok = True
        for wid, ch, step, payload in pending_flush:
            try:
                append_worker_stdout(
                    wid,
                    payload,
                    channel=ch
                )
            except Exception as e:
                logger.error(
                    f"[WorkerManager] Error flushing stdout for {wid}: {e}",
                    exc_info=True,
                )
                ok = False
        return ok

    def flush_worker_stdout(self, worker_id: str) -> bool:
        """Flush any buffered stdout/log data for a worker."""
        keys_to_flush: List[Tuple[str, str]] = []
        with self._lock:
            for key in list(self._stdout_buffers.keys()):
                if key[0] == worker_id:
                    keys_to_flush.append(key)

        ok = True
        for wid, channel in keys_to_flush:
            ok = self._flush_stdout_buffer(wid, channel) and ok
        return ok

    def _flush_stdout_buffer(self, worker_id: str, channel: str) -> bool:
        key = (worker_id, channel)
        with self._lock:
            chunks = self._stdout_buffers.get(key)
            if not chunks:
                return True
            text = "".join(chunks)
            self._stdout_buffers[key] = []
            self._stdout_buffer_sizes[key] = 0
            self._stdout_last_flush[key] = time.monotonic()
            self._stdout_buffer_steps[key] = None

        if not text:
            return True
        try:
            append_worker_stdout(
                worker_id,
                text,
                channel=channel,
            )
            return True
        except Exception as e:
            logger.error(
                f"[WorkerManager] Error flushing stdout for {worker_id}: {e}",
                exc_info=True,
            )
            return False

    def get_stdout(self, worker_id: str) -> str:
        """Get stdout logs for a worker.

        Args:
            worker_id: Unique identifier for the worker

        Returns:
            Worker's stdout or empty string
        """
        try:
            return db_get_worker_stdout(worker_id)
        except Exception as e:
            logger.error(f"[WorkerManager] Error getting stdout: {e}", exc_info=True)
            return ""

    def clear_stdout(self, worker_id: str) -> bool:
        """Clear stdout logs for a worker.

        Args:
            worker_id: Unique identifier for the worker

        Returns:
            True if clear was successful
        """
        try:
            # Not implemented in database.py yet, but we'll add it or skip it
            db.execute("DELETE FROM worker_stdout WHERE worker_id = ?", (worker_id,))
            return True
        except Exception as e:
            logger.error(f"[WorkerManager] Error clearing stdout: {e}", exc_info=True)
            return False

    def close(self) -> None:
        """Close the worker manager."""
        self.stop_auto_refresh()
        try:
            self._flush_all_stdout_buffers()
        except Exception:
            logger.exception("Failed to flush all stdout buffers during WorkerManager.close()")
        logger.info("[WorkerManager] Closed")

    def _flush_all_stdout_buffers(self) -> None:
        keys_to_flush: List[Tuple[str, str]] = []
        with self._lock:
            keys_to_flush = list(self._stdout_buffers.keys())
        for wid, channel in keys_to_flush:
            self._flush_stdout_buffer(wid, channel)
