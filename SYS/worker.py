from __future__ import annotations

import atexit
import io
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Set, TextIO, Sequence

from SYS.config import get_local_storage_path
from SYS.worker_manager import WorkerManager
from SYS.logger import log


class WorkerOutputMirror(io.TextIOBase):
    """Mirror stdout/stderr to worker manager while preserving console output."""

    def __init__(
        self,
        original: TextIO,
        manager: WorkerManager,
        worker_id: str,
        channel: str,
    ):
        self._original = original
        self._manager = manager
        self._worker_id = worker_id
        self._channel = channel
        self._pending: str = ""

    def write(self, data: str) -> int:  # type: ignore[override]
        if not data:
            return 0
        self._original.write(data)
        self._buffer_text(data)
        return len(data)

    def flush(self) -> None:  # type: ignore[override]
        self._original.flush()
        self._flush_pending(force=True)

    def isatty(self) -> bool:  # pragma: no cover
        return bool(getattr(self._original, "isatty", lambda: False)())

    def _buffer_text(self, data: str) -> None:
        combined = self._pending + data
        lines = combined.splitlines(keepends=True)
        if not lines:
            self._pending = combined
            return

        if lines[-1].endswith(("\n", "\r")):
            complete = lines
            self._pending = ""
        else:
            complete = lines[:-1]
            self._pending = lines[-1]

        for chunk in complete:
            self._emit(chunk)

    def _flush_pending(self, *, force: bool = False) -> None:
        if self._pending and force:
            self._emit(self._pending)
            self._pending = ""

    def _emit(self, text: str) -> None:
        if not text:
            return
        try:
            self._manager.append_stdout(self._worker_id, text, channel=self._channel)
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to append stdout for worker '%s' channel '%s'", self._worker_id, self._channel)

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self._original, "encoding", "utf-8")


class WorkerStageSession:
    """Lifecycle helper for wrapping a CLI cmdlet execution in a worker record."""

    def __init__(
        self,
        *,
        manager: WorkerManager,
        worker_id: str,
        orig_stdout: TextIO,
        orig_stderr: TextIO,
        stdout_proxy: WorkerOutputMirror,
        stderr_proxy: WorkerOutputMirror,
        config: Optional[Dict[str, Any]],
        logging_enabled: bool,
        completion_label: str,
        error_label: str,
    ) -> None:
        self.manager = manager
        self.worker_id = worker_id
        self.orig_stdout = orig_stdout
        self.orig_stderr = orig_stderr
        self.stdout_proxy = stdout_proxy
        self.stderr_proxy = stderr_proxy
        self.config = config
        self.logging_enabled = logging_enabled
        self.closed = False
        self._completion_label = completion_label
        self._error_label = error_label

    def close(self, *, status: str = "completed", error_msg: str = "") -> None:
        if self.closed:
            return
        try:
            self.stdout_proxy.flush()
            self.stderr_proxy.flush()
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to flush worker stdout/stderr proxies for '%s'", self.worker_id)

        sys.stdout = self.orig_stdout
        sys.stderr = self.orig_stderr

        if self.logging_enabled:
            try:
                self.manager.disable_logging_for_worker(self.worker_id)
            except Exception:
                from SYS.logger import logger
                logger.exception("Failed to disable logging for worker '%s'", self.worker_id)

        try:
            if status == "completed":
                self.manager.log_step(self.worker_id, self._completion_label)
            else:
                self.manager.log_step(
                    self.worker_id, f"{self._error_label}: {error_msg or status}"
                )
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to log step for worker '%s' status='%s' error='%s'", self.worker_id, status, error_msg)

        try:
            self.manager.finish_worker(
                self.worker_id, result=status or "completed", error_msg=error_msg or ""
            )
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to finish worker '%s' with status '%s'", self.worker_id, status)

        if self.config and self.config.get("_current_worker_id") == self.worker_id:
            self.config.pop("_current_worker_id", None)
        self.closed = True


class WorkerManagerRegistry:
    """Process-wide WorkerManager cache keyed by library_root."""

    _manager: Optional[WorkerManager] = None
    _manager_root: Optional[Path] = None
    _orphan_cleanup_done: bool = False
    _registered: bool = False

    @classmethod
    def ensure(cls, config: Dict[str, Any]) -> Optional[WorkerManager]:
        if not isinstance(config, dict):
            return None

        existing = config.get("_worker_manager")
        if isinstance(existing, WorkerManager):
            return existing

        library_root = get_local_storage_path(config)
        if not library_root:
            return None

        try:
            resolved_root = Path(library_root).resolve()
        except Exception:
            resolved_root = Path(library_root)

        try:
            if cls._manager is None or cls._manager_root != resolved_root:
                if cls._manager is not None:
                    try:
                        cls._manager.close()
                    except Exception:
                        from SYS.logger import logger
                        logger.exception("Failed to close existing WorkerManager during registry ensure")
                cls._manager = WorkerManager(auto_refresh_interval=0.5)
                cls._manager_root = resolved_root

            manager = cls._manager
            config["_worker_manager"] = manager

            if manager is not None and not cls._orphan_cleanup_done:
                try:
                    manager.expire_running_workers(
                        older_than_seconds=120,
                        worker_id_prefix="cli_%",
                        reason="CLI session ended unexpectedly; marking worker as failed",
                    )
                except Exception:
                    from SYS.logger import logger
                    logger.exception("Failed to expire running workers during registry ensure")
                else:
                    cls._orphan_cleanup_done = True

            if not cls._registered:
                atexit.register(cls.close)
                cls._registered = True

            return manager
        except Exception as exc:
            log(f"[worker] Could not initialize worker manager: {exc}", file=sys.stderr)
            return None

    @classmethod
    def close(cls) -> None:
        if cls._manager is None:
            return
        try:
            cls._manager.close()
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to close WorkerManager during registry.close()")
        cls._manager = None
        cls._manager_root = None
        cls._orphan_cleanup_done = False


class WorkerStages:
    """Factory methods for stage/pipeline worker sessions."""

    @staticmethod
    def _start_worker_session(
        worker_manager: Optional[WorkerManager],
        *,
        worker_type: str,
        title: str,
        description: str,
        pipe_text: str,
        config: Optional[Dict[str, Any]],
        completion_label: str,
        error_label: str,
        skip_logging_for: Optional[Set[str]] = None,
        session_worker_ids: Optional[Set[str]] = None,
    ) -> Optional[WorkerStageSession]:
        if worker_manager is None:
            return None
        if skip_logging_for and worker_type in skip_logging_for:
            return None

        safe_type = worker_type or "cmd"
        worker_id = f"cli_{safe_type[:8]}_{uuid.uuid4().hex[:6]}"

        try:
            tracked = worker_manager.track_worker(
                worker_id,
                worker_type=worker_type,
                title=title,
                description=description or "(no args)",
                pipe=pipe_text,
            )
            if not tracked:
                return None
        except Exception as exc:
            log(f"[worker] Failed to track {worker_type}: {exc}", file=sys.stderr)
            return None

        if session_worker_ids is not None:
            session_worker_ids.add(worker_id)

        logging_enabled = False
        try:
            handler = worker_manager.enable_logging_for_worker(worker_id)
            logging_enabled = handler is not None
        except Exception:
            logging_enabled = False

        orig_stdout = sys.stdout
        orig_stderr = sys.stderr
        stdout_proxy = WorkerOutputMirror(orig_stdout, worker_manager, worker_id, "stdout")
        stderr_proxy = WorkerOutputMirror(orig_stderr, worker_manager, worker_id, "stderr")
        sys.stdout = stdout_proxy
        sys.stderr = stderr_proxy
        if isinstance(config, dict):
            config["_current_worker_id"] = worker_id

        try:
            worker_manager.log_step(worker_id, f"Started {worker_type}")
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to log start step for worker '%s'", worker_id)

        return WorkerStageSession(
            manager=worker_manager,
            worker_id=worker_id,
            orig_stdout=orig_stdout,
            orig_stderr=orig_stderr,
            stdout_proxy=stdout_proxy,
            stderr_proxy=stderr_proxy,
            config=config,
            logging_enabled=logging_enabled,
            completion_label=completion_label,
            error_label=error_label,
        )

    @classmethod
    def begin_stage(
        cls,
        worker_manager: Optional[WorkerManager],
        *,
        cmd_name: str,
        stage_tokens: Sequence[str],
        config: Optional[Dict[str, Any]],
        command_text: str,
    ) -> Optional[WorkerStageSession]:
        description = " ".join(stage_tokens[1:]) if len(stage_tokens) > 1 else "(no args)"
        session_worker_ids = None
        if isinstance(config, dict):
            session_worker_ids = config.get("_session_worker_ids")

        return cls._start_worker_session(
            worker_manager,
            worker_type=cmd_name,
            title=f"{cmd_name} stage",
            description=description,
            pipe_text=command_text,
            config=config,
            completion_label="Stage completed",
            error_label="Stage error",
            skip_logging_for={".worker", "worker", "workers"},
            session_worker_ids=session_worker_ids,
        )

    @classmethod
    def begin_pipeline(
        cls,
        worker_manager: Optional[WorkerManager],
        *,
        pipeline_text: str,
        config: Optional[Dict[str, Any]],
    ) -> Optional[WorkerStageSession]:
        session_worker_ids: Set[str] = set()
        if isinstance(config, dict):
            config["_session_worker_ids"] = session_worker_ids

        return cls._start_worker_session(
            worker_manager,
            worker_type="pipeline",
            title="Pipeline run",
            description=pipeline_text,
            pipe_text=pipeline_text,
            config=config,
            completion_label="Pipeline completed",
            error_label="Pipeline error",
            session_worker_ids=session_worker_ids,
        )