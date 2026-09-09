"""Lightweight console notifier for background WorkerManager tasks.

Registers a refresh callback on WorkerManager and prints concise updates when
workers start, progress, or finish. Intended for CLI background workflows.

Filters to show only workers related to the current pipeline session to avoid
cluttering the terminal with workers from previous sessions.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Set

from SYS.logger import log, debug
import logging
logger = logging.getLogger(__name__)


class BackgroundNotifier:
    """Simple notifier that prints worker status changes for a session."""

    def __init__(
        self,
        manager: Any,
        output: Callable[[str],
                         None] = log,
        session_worker_ids: Optional[Set[str]] = None,
        only_terminal_updates: bool = False,
        overlay_mode: bool = False,
    ) -> None:
        self.manager = manager
        self.output = output
        self.session_worker_ids = session_worker_ids if session_worker_ids is not None else set(
        )
        self.only_terminal_updates = only_terminal_updates
        self.overlay_mode = overlay_mode
        self._filter_enabled = session_worker_ids is not None
        self._last_state: Dict[str,
                               str] = {}

        try:
            self.manager.add_refresh_callback(self._on_refresh)
            self.manager.start_auto_refresh()
        except Exception as exc:  # pragma: no cover - best effort
            debug(f"[notifier] Could not attach refresh callback: {exc}")

    def _render_line(self, worker: Dict[str, Any]) -> Optional[str]:
        # Use worker_id (the actual worker ID we set) for filtering and display
        worker_id = str(worker.get("worker_id") or "").strip()
        if not worker_id:
            # Fallback to database id if worker_id is not set
            worker_id = str(worker.get("id") or "").strip()
        if not worker_id:
            return None

        status = str(worker.get("status") or "running")
        progress_val = worker.get("progress") or worker.get("progress_percent")
        progress = ""
        if isinstance(progress_val, (int, float)):
            progress = f" {progress_val:.1f}%"
        elif progress_val:
            progress = f" {progress_val}"

        step = str(worker.get("current_step") or worker.get("description")
                   or "").strip()
        parts = [f"[worker:{worker_id}] {status}{progress}"]
        if step:
            parts.append(step)
        return " - ".join(parts)

    def _on_refresh(self, workers: list[Dict[str, Any]]) -> None:
        overlay_active_workers = 0

        for worker in workers:
            # Use worker_id (the actual worker ID we set) for filtering
            worker_id = str(worker.get("worker_id") or "").strip()
            if not worker_id:
                # Fallback to database id if worker_id is not set
                worker_id = str(worker.get("id") or "").strip()
            if not worker_id:
                continue

            # If filtering is enabled, skip workers not in this session
            if self._filter_enabled and worker_id not in self.session_worker_ids:
                continue

            status = str(worker.get("status") or "running")

            # Overlay mode: only emit on completion; suppress start/progress spam
            if self.overlay_mode:
                if status in ("completed", "finished", "error"):
                    progress_val = worker.get("progress"
                                              ) or worker.get("progress_percent") or ""
                    step = str(
                        worker.get("current_step") or worker.get("description") or ""
                    ).strip()
                    signature = f"{status}|{progress_val}|{step}"

                    if self._last_state.get(worker_id) == signature:
                        continue

                    self._last_state[worker_id] = signature
                    line = self._render_line(worker)
                    if line:
                        try:
                            self.output(line)
                        except Exception:
                            logger.exception("BackgroundNotifier failed to output overlay line for worker %s", worker_id)

                    self._last_state.pop(worker_id, None)
                    self.session_worker_ids.discard(worker_id)
                continue

            # For terminal-only mode, emit once when the worker finishes and skip intermediate updates
            if self.only_terminal_updates:
                if status in ("completed", "finished", "error"):
                    if self._last_state.get(worker_id) == status:
                        continue
                    self._last_state[worker_id] = status
                    line = self._render_line(worker)
                    if line:
                        try:
                            self.output(line)
                        except Exception:
                            logger.exception("BackgroundNotifier failed to output terminal-only line for worker %s", worker_id)
                    # Stop tracking this worker after terminal notification
                    self.session_worker_ids.discard(worker_id)
                continue

            # Skip finished workers after showing them once (standard verbose mode)
            if status in ("completed", "finished", "error"):
                if worker_id in self._last_state:
                    # Already shown, remove from tracking
                    self._last_state.pop(worker_id, None)
                    self.session_worker_ids.discard(worker_id)
                continue

            progress_val = worker.get("progress"
                                      ) or worker.get("progress_percent") or ""
            step = str(worker.get("current_step") or worker.get("description")
                       or "").strip()
            signature = f"{status}|{progress_val}|{step}"

            if self._last_state.get(worker_id) == signature:
                continue

            self._last_state[worker_id] = signature
            line = self._render_line(worker)
            if line:
                try:
                    self.output(line)
                except Exception:
                    logger.exception("BackgroundNotifier failed to output line for worker %s", worker_id)

        if self.overlay_mode:
            try:
                # If nothing active for this session, clear the overlay text
                if overlay_active_workers == 0:
                    self.output("")
            except Exception:
                logger.exception("BackgroundNotifier failed to clear overlay for session")


def ensure_background_notifier(
    manager: Any,
    output: Callable[[str],
                     None] = log,
    session_worker_ids: Optional[Set[str]] = None,
    only_terminal_updates: bool = False,
    overlay_mode: bool = False,
) -> Optional[BackgroundNotifier]:
    """Attach a BackgroundNotifier to a WorkerManager if not already present.

    Args:
        manager: WorkerManager instance
        output: Function to call for printing updates
        session_worker_ids: Set of worker IDs belonging to this pipeline session.
                           If None, show all workers. If a set (even empty), only show workers in that set.
    """
    if manager is None:
        return None

    existing = getattr(manager, "_background_notifier", None)
    if isinstance(existing, BackgroundNotifier):
        # Update session IDs if provided
        if session_worker_ids is not None:
            existing._filter_enabled = True
            existing.session_worker_ids.update(session_worker_ids)
        # Respect the most restrictive setting for terminal-only updates
        if only_terminal_updates:
            existing.only_terminal_updates = True
        # Enable overlay mode if requested later
        if overlay_mode:
            existing.overlay_mode = True
        return existing

    notifier = BackgroundNotifier(
        manager,
        output,
        session_worker_ids=session_worker_ids,
        only_terminal_updates=only_terminal_updates,
        overlay_mode=overlay_mode,
    )
    try:
        manager._background_notifier = notifier  # type: ignore[attr-defined]
    except Exception as exc:
        logger.exception("Failed to attach background notifier to manager: %s", exc)
    return notifier
