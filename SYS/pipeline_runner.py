"""Shared pipeline runner utilities.

This module wraps the canonical CLI pipeline executor so non-CLI callers can
execute pipelines and capture the resulting table/items without depending on
the discontinued Textual UI package.
"""

from __future__ import annotations

import contextlib
import io
import shlex
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from CLI import ConfigLoader
from SYS import pipeline as ctx
from SYS.logger import debug, set_debug
from SYS.pipeline import PipelineExecutor
from SYS.result_table import Table
from SYS.rich_display import capture_rich_output
from SYS.worker import WorkerManagerRegistry


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class PipelineStageResult:
    """Summary for a single pipeline stage."""

    name: str
    args: Sequence[str]
    emitted: List[Any] = field(default_factory=list)
    result_table: Optional[Any] = None
    status: str = "pending"
    error: Optional[str] = None


@dataclass(slots=True)
class PipelineRunResult:
    """Aggregate result for a pipeline run."""

    pipeline: str
    success: bool
    stages: List[PipelineStageResult] = field(default_factory=list)
    emitted: List[Any] = field(default_factory=list)
    result_table: Optional[Any] = None
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None

    def to_summary(self) -> Dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "success": self.success,
            "error": self.error,
            "stages": [
                {
                    "name": stage.name,
                    "status": stage.status,
                    "error": stage.error,
                    "emitted": len(stage.emitted),
                }
                for stage in self.stages
            ],
        }


class PipelineRunner:
    """Wrapper around the canonical CLI pipeline executor."""

    def __init__(
        self,
        config_loader: Optional[Any] = None,
        executor: Optional[Any] = None,
    ) -> None:
        self._config_loader = (
            config_loader
            if config_loader is not None
            else ConfigLoader(root=REPO_ROOT)
        )
        self._executor = (
            executor
            if executor is not None
            else PipelineExecutor(config_loader=self._config_loader)
        )
        self._worker_manager = None

    @property
    def worker_manager(self):
        return self._worker_manager

    def run_pipeline(
        self,
        pipeline_text: str,
        *,
        seeds: Optional[Any] = None,
        seed_table: Optional[Any] = None,
        isolate: bool = False,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> PipelineRunResult:
        snapshot: Optional[Dict[str, Any]] = None
        if isolate:
            snapshot = self._snapshot_ctx_state()

        normalized = str(pipeline_text or "").strip()
        result = PipelineRunResult(pipeline=normalized, success=False)
        if not normalized:
            result.error = "Pipeline is empty"
            return result

        try:
            from SYS.cli_syntax import validate_pipeline_text

            syntax_error = validate_pipeline_text(normalized)
            if syntax_error:
                result.error = syntax_error.message
                result.stderr = syntax_error.message
                return result
        except Exception:
            debug(traceback.format_exc())

        try:
            tokens = shlex.split(normalized)
        except Exception as exc:
            result.error = f"Syntax error: {exc}"
            result.stderr = result.error
            return result

        if not tokens:
            result.error = "Pipeline contains no tokens"
            return result

        config = self._config_loader.load()
        try:
            set_debug(bool(config.get("debug", False)))
        except Exception:
            debug(traceback.format_exc())

        try:
            self._worker_manager = WorkerManagerRegistry.ensure(config)
        except Exception:
            debug(traceback.format_exc())
            self._worker_manager = None

        ctx.reset()
        ctx.set_current_command_text(normalized)

        if seeds is not None:
            try:
                if not isinstance(seeds, list):
                    seeds = [seeds]
                ctx.set_last_result_items_only(list(seeds))
            except Exception:
                debug(traceback.format_exc())

        if seed_table is not None:
            try:
                ctx.set_current_stage_table(seed_table)
            except Exception:
                debug(traceback.format_exc())

        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()

        try:
            with capture_rich_output(stdout=stdout_buffer, stderr=stderr_buffer):
                with (
                    contextlib.redirect_stdout(stdout_buffer),
                    contextlib.redirect_stderr(stderr_buffer),
                ):
                    if on_log:
                        on_log("Executing pipeline via CLI executor...")
                    self._executor.execute_tokens(list(tokens))
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                ctx.clear_current_command_text()
            except Exception:
                debug(traceback.format_exc())
            result.stdout = stdout_buffer.getvalue()
            result.stderr = stderr_buffer.getvalue()

        table = None
        try:
            table = (
                ctx.get_display_table()
                or ctx.get_current_stage_table()
                or ctx.get_last_result_table()
            )
        except Exception:
            table = None

        items: List[Any] = []
        try:
            items = list(ctx.get_last_result_items() or [])
        except Exception:
            items = []

        if table is None and items:
            try:
                synth = Table("Results")
                for item in items:
                    synth.add_result(item)
                table = synth
            except Exception:
                table = None

        result.emitted = items
        result.result_table = table

        combined = (result.stdout + "\n" + result.stderr).strip().lower()
        failure_markers = (
            "unknown command:",
            "pipeline order error:",
            "invalid selection:",
            "invalid pipeline syntax",
            "failed to execute pipeline",
            "[error]",
        )
        if result.error:
            result.success = False
        elif any(marker in combined for marker in failure_markers):
            result.success = False
            if not result.error:
                result.error = "Pipeline failed"
        else:
            result.success = True

        if isolate and snapshot is not None:
            try:
                self._restore_ctx_state(snapshot)
            except Exception:
                pass

        return result

    @staticmethod
    def _snapshot_ctx_state() -> Dict[str, Any]:
        def _copy(value: Any) -> Any:
            if isinstance(value, list):
                return value.copy()
            if isinstance(value, dict):
                return value.copy()
            return value

        state = ctx.get_pipeline_state()
        snapshot: Dict[str, Any] = {}
        snapshot["live_progress"] = _copy(state.live_progress)
        snapshot["current_context"] = state.current_context
        snapshot["last_search_query"] = state.last_search_query
        snapshot["pipeline_refreshed"] = state.pipeline_refreshed
        snapshot["last_items"] = _copy(state.last_items)
        snapshot["last_result_table"] = state.last_result_table
        snapshot["last_result_items"] = _copy(state.last_result_items)
        snapshot["last_result_subject"] = state.last_result_subject

        def _copy_history(history: Optional[List[tuple]]) -> List[tuple]:
            out: List[tuple] = []
            try:
                for table_value, items, subject in list(history or []):
                    if isinstance(items, list):
                        items_copy = items.copy()
                    elif items:
                        items_copy = list(items)
                    else:
                        items_copy = []
                    out.append((table_value, items_copy, subject))
            except Exception:
                debug(traceback.format_exc())
            return out

        snapshot["result_table_history"] = _copy_history(state.result_table_history)
        snapshot["result_table_forward"] = _copy_history(state.result_table_forward)
        snapshot["current_stage_table"] = state.current_stage_table
        snapshot["display_items"] = _copy(state.display_items)
        snapshot["display_table"] = state.display_table
        snapshot["display_subject"] = state.display_subject
        snapshot["last_selection"] = _copy(state.last_selection)
        snapshot["pipeline_command_text"] = state.pipeline_command_text
        snapshot["current_cmdlet_name"] = state.current_cmdlet_name
        snapshot["current_stage_text"] = state.current_stage_text
        snapshot["pipeline_values"] = (
            _copy(state.pipeline_values)
            if isinstance(state.pipeline_values, dict)
            else state.pipeline_values
        )
        snapshot["pending_pipeline_tail"] = [
            list(stage) for stage in (state.pending_pipeline_tail or [])
        ]
        snapshot["pending_pipeline_source"] = state.pending_pipeline_source
        snapshot["ui_library_refresh_callback"] = state.ui_library_refresh_callback
        snapshot["pipeline_stop"] = state.pipeline_stop
        return snapshot

    @staticmethod
    def _restore_ctx_state(snapshot: Dict[str, Any]) -> None:
        if not snapshot:
            return

        state = ctx.get_pipeline_state()

        def _restore_history(key: str, value: Any) -> None:
            try:
                if not isinstance(value, list):
                    return
                restored: List[tuple] = []
                for table_value, items, subject in value:
                    if isinstance(items, list):
                        items_copy = items.copy()
                    elif items:
                        items_copy = list(items)
                    else:
                        items_copy = []
                    restored.append((table_value, items_copy, subject))
                setattr(state, key, restored)
            except Exception:
                debug(traceback.format_exc())

        try:
            if "live_progress" in snapshot:
                state.live_progress = snapshot["live_progress"]
            if "current_context" in snapshot:
                state.current_context = snapshot["current_context"]
            if "last_search_query" in snapshot:
                state.last_search_query = snapshot["last_search_query"]
            if "pipeline_refreshed" in snapshot:
                state.pipeline_refreshed = snapshot["pipeline_refreshed"]
            if "last_items" in snapshot:
                state.last_items = snapshot["last_items"] or []
            if "last_result_table" in snapshot:
                state.last_result_table = snapshot["last_result_table"]
            if "last_result_items" in snapshot:
                state.last_result_items = snapshot["last_result_items"] or []
            if "last_result_subject" in snapshot:
                state.last_result_subject = snapshot["last_result_subject"]
            if "result_table_history" in snapshot:
                _restore_history("result_table_history", snapshot["result_table_history"])
            if "result_table_forward" in snapshot:
                _restore_history("result_table_forward", snapshot["result_table_forward"])
            if "current_stage_table" in snapshot:
                state.current_stage_table = snapshot["current_stage_table"]
            if "display_items" in snapshot:
                state.display_items = snapshot["display_items"] or []
            if "display_table" in snapshot:
                state.display_table = snapshot["display_table"]
            if "display_subject" in snapshot:
                state.display_subject = snapshot["display_subject"]
            if "last_selection" in snapshot:
                state.last_selection = snapshot["last_selection"] or []
            if "pipeline_command_text" in snapshot:
                state.pipeline_command_text = snapshot["pipeline_command_text"] or ""
            if "current_cmdlet_name" in snapshot:
                state.current_cmdlet_name = snapshot["current_cmdlet_name"] or ""
            if "current_stage_text" in snapshot:
                state.current_stage_text = snapshot["current_stage_text"] or ""
            if "pipeline_values" in snapshot:
                state.pipeline_values = snapshot["pipeline_values"] or {}
            if "pending_pipeline_tail" in snapshot:
                state.pending_pipeline_tail = snapshot["pending_pipeline_tail"] or []
            if "pending_pipeline_source" in snapshot:
                state.pending_pipeline_source = snapshot["pending_pipeline_source"]
            if "ui_library_refresh_callback" in snapshot:
                state.ui_library_refresh_callback = snapshot["ui_library_refresh_callback"]
            if "pipeline_stop" in snapshot:
                state.pipeline_stop = snapshot["pipeline_stop"]
        except Exception:
            pass