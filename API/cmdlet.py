from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from SYS import pipeline as ctx
from SYS.models import PipelineStageContext
from SYS.rich_display import capture_rich_output


CmdletCallable = Callable[[Any, Sequence[str], Dict[str, Any]], int]


@dataclass(slots=True)
class CmdletRunResult:
    """Programmatic result for a single cmdlet invocation."""

    name: str
    args: Sequence[str]
    exit_code: int = 0
    emitted: List[Any] = field(default_factory=list)

    # Best-effort: cmdlets can publish tables/items via pipeline state even when
    # they don't emit pipeline items.
    result_table: Optional[Any] = None
    result_items: List[Any] = field(default_factory=list)
    result_subject: Optional[Any] = None

    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None


def _normalize_cmd_name(name: str) -> str:
    return str(name or "").replace("_", "-").strip().lower()


def resolve_cmdlet(cmd_name: str) -> Optional[CmdletCallable]:
    """Resolve a cmdlet callable by name from the registry (aliases supported)."""
    try:
        from SYS.cmdlet_catalog import ensure_registry_loaded

        ensure_registry_loaded()
    except Exception:
        pass

    try:
        import cmdlet as cmdlet_pkg

        return cmdlet_pkg.get(cmd_name)
    except Exception:
        return None


def run_cmdlet(
    cmd: str | CmdletCallable,
    args: Sequence[str] | None,
    config: Dict[str, Any],
    *,
    piped: Any = None,
    isolate: bool = True,
    capture_output: bool = True,
    stage_index: int = 0,
    total_stages: int = 1,
    pipe_index: Optional[int] = None,
    worker_id: Optional[str] = None,
) -> CmdletRunResult:
    """Run a single cmdlet programmatically and return structured results.

    This is intended for programmatic consumers that want cmdlet behavior without
    going through the interactive CLI loop.

    Notes:
    - When `isolate=True` (default) this runs inside `ctx.new_pipeline_state()` so
      global CLI pipeline state is not mutated.
    - Output capturing covers both normal `print()` and Rich output via
      `capture_rich_output()`.
    """

    normalized_args: Sequence[str] = list(args or [])

    if isinstance(cmd, str):
        name = _normalize_cmd_name(cmd)
        cmd_fn = resolve_cmdlet(name)
    else:
        name = getattr(cmd, "__name__", "cmdlet")
        cmd_fn = cmd

    result = CmdletRunResult(name=name, args=normalized_args)

    if not callable(cmd_fn):
        result.exit_code = 1
        result.error = f"Unknown command: {name}"
        result.stderr = result.error
        return result

    stage_ctx = PipelineStageContext(
        stage_index=int(stage_index),
        total_stages=int(total_stages),
        pipe_index=pipe_index,
        worker_id=worker_id,
    )

    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()

    stage_text = " ".join([name, *list(normalized_args)]).strip()

    state_cm = ctx.new_pipeline_state() if isolate else contextlib.nullcontext()

    with state_cm:
        # Keep behavior predictable: start from a clean slate.
        try:
            ctx.reset()
        except Exception:
            pass

        try:
            ctx.set_stage_context(stage_ctx)
        except Exception:
            pass

        try:
            ctx.set_current_cmdlet_name(name)
        except Exception:
            pass

        try:
            ctx.set_current_stage_text(stage_text)
        except Exception:
            pass

        try:
            ctx.set_current_command_text(stage_text)
        except Exception:
            pass

        try:
            run_cm = (
                capture_rich_output(stdout=stdout_buffer, stderr=stderr_buffer)
                if capture_output
                else contextlib.nullcontext()
            )
            with run_cm:
                with (
                    contextlib.redirect_stdout(stdout_buffer)
                    if capture_output
                    else contextlib.nullcontext()
                ):
                    with (
                        contextlib.redirect_stderr(stderr_buffer)
                        if capture_output
                        else contextlib.nullcontext()
                    ):
                        result.exit_code = int(cmd_fn(piped, list(normalized_args), config))
        except Exception as exc:
            result.exit_code = 1
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            result.stdout = stdout_buffer.getvalue()
            result.stderr = stderr_buffer.getvalue()

        # Prefer cmdlet emits (pipeline semantics).
        try:
            result.emitted = list(stage_ctx.emits or [])
        except Exception:
            result.emitted = []

        # Mirror CLI behavior: if cmdlet emitted items and there is no overlay table,
        # make emitted items the last result items for downstream consumers.
        try:
            has_overlay = bool(ctx.get_display_table())
        except Exception:
            has_overlay = False

        if result.emitted and not has_overlay:
            try:
                ctx.set_last_result_items_only(list(result.emitted))
            except Exception:
                pass

        # Best-effort snapshot of visible results.
        try:
            result.result_table = (
                ctx.get_display_table() or ctx.get_current_stage_table() or ctx.get_last_result_table()
            )
        except Exception:
            result.result_table = None

        try:
            result.result_items = list(ctx.get_last_result_items() or [])
        except Exception:
            result.result_items = []

        try:
            result.result_subject = ctx.get_last_result_subject()
        except Exception:
            result.result_subject = None

        # Cleanup stage-local markers.
        try:
            ctx.clear_current_stage_text()
        except Exception:
            pass
        try:
            ctx.clear_current_cmdlet_name()
        except Exception:
            pass
        try:
            ctx.clear_current_command_text()
        except Exception:
            pass
        try:
            ctx.set_stage_context(None)
        except Exception:
            pass

    return result
