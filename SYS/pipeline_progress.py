from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Any, Iterator, Optional, Sequence, Tuple
import logging
logger = logging.getLogger(__name__)


class PipelineProgress:
    """Small adapter around PipelineLiveProgress.

    This centralizes the boilerplate used across cmdlets:
    - locating the active Live UI (if any)
    - resolving the current pipe_index from stage context
    - step-based progress (begin_pipe_steps/advance_pipe_step)
    - optional pipe percent/status updates
    - optional byte transfer bars
    - optional local Live panel when a cmdlet runs standalone

    The class is intentionally defensive: all UI operations are best-effort.
    """

    def __init__(self, pipeline_module: Any):
        self._ctx = pipeline_module
        self._local_ui: Optional[Any] = None
        self._local_attached: bool = False

    def ui_and_pipe_index(self) -> Tuple[Optional[Any], int]:
        ui = None
        try:
            ui = self._ctx.get_live_progress(
            ) if hasattr(self._ctx,
                         "get_live_progress") else None
        except Exception:
            logger.exception("Failed to get live progress UI from pipeline context")
            ui = None

        pipe_idx: int = 0
        try:
            stage_ctx = (
                self._ctx.get_stage_context()
                if hasattr(self._ctx,
                           "get_stage_context") else None
            )
            maybe_idx = getattr(
                stage_ctx,
                "pipe_index",
                None
            ) if stage_ctx is not None else None
            if isinstance(maybe_idx, int):
                pipe_idx = int(maybe_idx)
        except Exception:
            logger.exception("Failed to determine pipe index from stage context")
            pipe_idx = 0

        return ui, pipe_idx

    def begin_steps(self, total_steps: int) -> None:
        ui, pipe_idx = self.ui_and_pipe_index()
        if ui is None:
            return
        try:
            begin = getattr(ui, "begin_pipe_steps", None)
            if callable(begin):
                begin(int(pipe_idx), total_steps=int(total_steps))
        except Exception:
            logger.exception("Failed to call begin_pipe_steps on UI")
            return

    def step(self, text: str) -> None:
        ui, pipe_idx = self.ui_and_pipe_index()
        if ui is None:
            return
        try:
            adv = getattr(ui, "advance_pipe_step", None)
            if callable(adv):
                adv(int(pipe_idx), str(text))
        except Exception:
            logger.exception("Failed to advance pipe step on UI")
            return

    def set_step(self, done: int, text: str) -> None:
        ui, pipe_idx = self.ui_and_pipe_index()
        if ui is None:
            return
        try:
            setter = getattr(ui, "set_pipe_step", None)
            if callable(setter):
                setter(int(pipe_idx), int(done), str(text))
        except Exception:
            logger.exception("Failed to set pipe step on UI")
            return

    def set_percent(self, percent: int) -> None:
        ui, pipe_idx = self.ui_and_pipe_index()
        if ui is None:
            return
        try:
            set_pct = getattr(ui, "set_pipe_percent", None)
            if callable(set_pct):
                set_pct(int(pipe_idx), int(percent))
        except Exception:
            logger.exception("Failed to set pipe percent on UI")
            return

    def set_status(self, text: str) -> None:
        ui, pipe_idx = self.ui_and_pipe_index()
        if ui is None:
            return
        try:
            setter = getattr(ui, "set_pipe_status_text", None)
            if callable(setter):
                setter(int(pipe_idx), str(text))
        except Exception:
            logger.exception("Failed to set pipe status text on UI")
            return

    def clear_status(self) -> None:
        ui, pipe_idx = self.ui_and_pipe_index()
        if ui is None:
            return
        try:
            clr = getattr(ui, "clear_pipe_status_text", None)
            if callable(clr):
                clr(int(pipe_idx))
        except Exception:
            logger.exception("Failed to clear pipe status text on UI")
            return

    def begin_transfer(self, *, label: str, total: Optional[int] = None) -> None:
        ui, _ = self.ui_and_pipe_index()
        if ui is None:
            return
        try:
            fn = getattr(ui, "begin_transfer", None)
            if callable(fn):
                fn(label=str(label or "transfer"), total=total)
        except Exception:
            logger.exception("Failed to begin transfer on UI")
            return

    def update_transfer(
        self,
        *,
        label: str,
        completed: Optional[int],
        total: Optional[int] = None
    ) -> None:
        ui, _ = self.ui_and_pipe_index()
        if ui is None:
            return
        try:
            fn = getattr(ui, "update_transfer", None)
            if callable(fn):
                fn(label=str(label or "transfer"), completed=completed, total=total)
        except Exception:
            logger.exception("Failed to update transfer on UI")
            return

    def finish_transfer(self, *, label: str) -> None:
        ui, _ = self.ui_and_pipe_index()
        if ui is None:
            return
        try:
            fn = getattr(ui, "finish_transfer", None)
            if callable(fn):
                fn(label=str(label or "transfer"))
        except Exception:
            logger.exception("Failed to finish transfer on UI")
            return

    def begin_pipe(
        self,
        *,
        total_items: int,
        items_preview: Optional[Sequence[Any]] = None
    ) -> None:
        ui, pipe_idx = self.ui_and_pipe_index()
        if ui is None:
            return
        try:
            fn = getattr(ui, "begin_pipe", None)
            if callable(fn):
                fn(
                    int(pipe_idx),
                    total_items=max(1, int(total_items)),
                    items_preview=list(items_preview or []),
                )
        except Exception:
            logger.exception("Failed to begin pipe on UI")
            return

    def on_emit(self, emitted: Any) -> None:
        """Advance local pipe progress after pipeline_context.emit().

        The shared PipelineExecutor wires on_emit automatically for pipelines.
        Standalone cmdlet runs do not, so cmdlets call this explicitly.
        """

        if self._local_ui is None:
            return
        try:
            self._local_ui.on_emit(0, emitted)
        except Exception:
            logger.exception("Failed to call local UI on_emit")
            return

    def ensure_local_ui(
        self,
        *,
        label: str,
        total_items: int,
        items_preview: Optional[Sequence[Any]] = None
    ) -> bool:
        """Start a local PipelineLiveProgress panel if no shared UI exists."""

        try:
            existing = (
                self._ctx.get_live_progress()
                if hasattr(self._ctx,
                           "get_live_progress") else None
            )
        except Exception:
            logger.exception("Failed to check existing live progress from pipeline context")
            existing = None

        if existing is not None:
            return False

        try:
            from SYS.rich_display import terminal_supports_live_progress
        except Exception:
            return False
        if not terminal_supports_live_progress():
            return False

        try:
            from SYS.models import PipelineLiveProgress

            ui = PipelineLiveProgress([str(label or "pipeline")], enabled=True)
            ui.start()
            try:
                if hasattr(self._ctx, "set_live_progress"):
                    self._ctx.set_live_progress(ui)
                    self._local_attached = True
            except Exception:
                logger.exception("Failed to attach local UI to pipeline context")
                self._local_attached = False

            try:
                ui.begin_pipe(
                    0,
                    total_items=max(1,
                                    int(total_items)),
                    items_preview=list(items_preview or [])
                )
            except Exception:
                logger.exception("Failed to begin_pipe on local UI")

            self._local_ui = ui
            return True
        except Exception:
            logger.exception("Failed to create local PipelineLiveProgress UI")
            self._local_ui = None
            self._local_attached = False
            return False

    def close_local_ui(self, *, force_complete: bool = True) -> None:
        if self._local_ui is None:
            return
        try:
            try:
                self._local_ui.finish_pipe(0, force_complete=bool(force_complete))
            except Exception:
                logger.exception("Failed to finish local UI pipe")
            try:
                self._local_ui.stop()
            except Exception:
                logger.exception("Failed to stop local UI")
        finally:
            self._local_ui = None
            try:
                if self._local_attached and hasattr(self._ctx, "set_live_progress"):
                    self._ctx.set_live_progress(None)
            except Exception:
                logger.exception("Failed to detach local progress from pipeline context")
            self._local_attached = False

    @contextmanager
    def local_ui_if_needed(
        self,
        *,
        label: str,
        total_items: int,
        items_preview: Optional[Sequence[Any]] = None,
    ) -> Iterator["PipelineProgress"]:
        created = self.ensure_local_ui(
            label=label,
            total_items=total_items,
            items_preview=items_preview
        )
        try:
            yield self
        finally:
            if created:
                self.close_local_ui(force_complete=True)
