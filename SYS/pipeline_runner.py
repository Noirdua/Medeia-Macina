"""Shared pipeline runner utilities.

This module wraps the canonical CLI pipeline executor so non-CLI callers can
execute pipelines and capture the resulting table/items without depending on
the discontinued Textual UI package.
"""

from __future__ import annotations

import contextlib
import io
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
        if isolate:
            from SYS.pipeline_state import new_pipeline_state

            with new_pipeline_state():
                return self.run_pipeline(
                    pipeline_text,
                    seeds=seeds,
                    seed_table=seed_table,
                    isolate=False,
                    on_log=on_log,
                )

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
            from SYS.cli_syntax import split_shell_tokens

            tokens = split_shell_tokens(normalized)
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

        recorded = {}
        try:
            recorded = ctx.get_last_execution_result()
        except Exception:
            recorded = {}
        if result.error:
            result.success = False
        elif isinstance(recorded, dict) and recorded:
            result.success = bool(recorded.get("success"))
            if not result.success and not result.error:
                result.error = str(recorded.get("error") or "Pipeline failed")
        else:
            result.success = False
            if not result.error:
                result.error = "Pipeline failed"

        return result

