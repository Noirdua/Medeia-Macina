"""
Pipeline state management for cmdlet.
"""
from __future__ import annotations

import sys
import time
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from contextvars import ContextVar
from typing import Any, Dict, List, Optional, Sequence, Callable
from SYS.models import PipelineStageContext
from SYS.logger import log, debug, debug_panel, is_debug_enabled, status_panel
import logging

logger = logging.getLogger(__name__)

# SYS.worker deferred: ffmpeg+attr+rich (~260ms) loaded lazily on first pipeline run.
_worker_mod: Any = None
# SYS.cli_parsing deferred: prompt_toolkit (~300ms) loaded lazily on first selection.
_cli_parsing_mod: Any = None
# SYS.result_table deferred: textual (~140ms) loaded lazily on first Table use.
_result_table_mod: Any = None


def _worker() -> Any:
    global _worker_mod
    if _worker_mod is None:
        import SYS.worker as _m

        _worker_mod = _m
    return _worker_mod


def _cli_parsing() -> Any:
    global _cli_parsing_mod
    if _cli_parsing_mod is None:
        import SYS.cli_parsing as _m

        _cli_parsing_mod = _m
    return _cli_parsing_mod


def _result_table() -> Any:
    global _result_table_mod
    if _result_table_mod is None:
        from SYS.result_table import Table as _T

        _result_table_mod = _T
    return _result_table_mod


HELP_EXAMPLE_SOURCE_COMMANDS = {
    ".help-example",
    "help-example",
}

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _table_debug_rows(table: Any) -> List[tuple[str, Any]]:
    if table is None:
        return []
    rows: List[tuple[str, Any]] = []
    title = str(getattr(table, "title", "") or "").strip()
    kind = str(getattr(table, "table", "") or "").strip()
    if title:
        rows.append(("table", title))
    if kind:
        rows.append(("table_type", kind))
    meta: Any = None
    try:
        getter = getattr(table, "get_table_metadata", None)
        meta = getter() if callable(getter) else getattr(table, "table_metadata", None)
    except Exception:
        meta = None
    if isinstance(meta, dict):
        for key in ("plugin", "instance", "view", "path"):
            value = meta.get(key)
            if value not in (None, ""):
                rows.append((key, value))
    return rows


def _emit_selection_debug_panel(
    *,
    selection_token: Any,
    selection_indices: Sequence[int],
    item_count: int,
    filtered_count: int,
    stage_table_present: bool,
    display_table_present: bool,
    stage_is_last: bool,
    row_action: Optional[Sequence[Any]] = None,
    downstream_stages: Optional[Sequence[Sequence[Any]]] = None,
    mode: Optional[str] = None,
    source_table: Any = None,
) -> None:
    if not is_debug_enabled():
        return

    try:
        rows: List[tuple[str, Any]] = [
            ("selection", str(selection_token or "")),
            ("indices", [int(idx) + 1 for idx in (selection_indices or [])]),
            ("items", int(item_count)),
            ("filtered", int(filtered_count)),
            ("stage_table", bool(stage_table_present)),
            ("display_table", bool(display_table_present)),
            ("stage_is_last", bool(stage_is_last)),
            ("downstream_stages", len(list(downstream_stages or []))),
        ]
        if mode:
            rows.insert(1, ("mode", str(mode)))
        table_rows = _table_debug_rows(source_table)
        insert_at = 2 if mode else 1
        for offset, row in enumerate(table_rows):
            rows.insert(insert_at + offset, row)
        if row_action:
            rows.append(
                ("row_action", " ".join(str(part) for part in row_action if part is not None))
            )

        debug_panel(
            f"Selection replay {selection_token}",
            rows,
            border_style="magenta",
        )
    except Exception:
        pass


def set_live_progress(progress_ui: Any) -> None:
    state = _get_pipeline_state()
    state.live_progress = progress_ui


def get_live_progress() -> Any:
    state = _get_pipeline_state()
    return state.live_progress


def set_progress_event_callback(callback: Any) -> None:
    state = _get_pipeline_state()
    state.progress_event_callback = callback


def get_progress_event_callback() -> Any:
    state = _get_pipeline_state()
    return state.progress_event_callback


@contextmanager
def suspend_live_progress() -> Any:
    ui = get_live_progress()
    paused = False
    try:
        if ui is not None and hasattr(ui, "pause"):
            try:
                ui.pause()
                paused = True
            except Exception as exc:
                logger.exception("Failed to pause live progress UI: %s", exc)
                paused = False
        yield
    finally:
        if get_pipeline_stop() is None:
            if paused and ui is not None and hasattr(ui, "resume"):
                try:
                    ui.resume()
                except Exception:
                    logger.exception("Failed to resume live progress UI after suspend")


def _is_selectable_table(table: Any) -> bool:
    return table is not None and not getattr(table, "no_choice", False)


# ---------------------------------------------------------------------------
# PipelineState
# ---------------------------------------------------------------------------


@dataclass
class PipelineState:
    current_context: Optional[PipelineStageContext] = None
    last_search_query: Optional[str] = None
    pipeline_refreshed: bool = False
    last_items: List[Any] = field(default_factory=list)
    last_result_table: Optional[Any] = None
    last_result_items: List[Any] = field(default_factory=list)
    last_result_subject: Optional[Any] = None
    result_table_history: List[tuple[Optional[Any], List[Any], Optional[Any]]] = field(
        default_factory=list
    )
    result_table_forward: List[tuple[Optional[Any], List[Any], Optional[Any]]] = field(
        default_factory=list
    )
    current_stage_table: Optional[Any] = None
    display_items: List[Any] = field(default_factory=list)
    display_table: Optional[Any] = None
    display_subject: Optional[Any] = None
    last_selection: List[int] = field(default_factory=list)
    pipeline_command_text: str = ""
    current_cmdlet_name: str = ""
    current_stage_text: str = ""
    pipeline_values: Dict[str, Any] = field(default_factory=dict)
    pending_pipeline_tail: List[List[str]] = field(default_factory=list)
    pending_pipeline_source: Optional[str] = None
    ui_library_refresh_callback: Optional[Any] = None
    pipeline_stop: Optional[Dict[str, Any]] = None
    live_progress: Any = None
    last_execution_result: Dict[str, Any] = field(default_factory=dict)
    progress_event_callback: Any = None

    def reset(self) -> None:
        self.current_context = None
        self.last_search_query = None
        self.pipeline_refreshed = False
        self.last_items = []
        self.last_result_table = None
        self.last_result_items = []
        self.last_result_subject = None
        self.result_table_history = []
        self.result_table_forward = []
        self.current_stage_table = None
        self.display_items = []
        self.display_table = None
        self.display_subject = None
        self.last_selection = []
        self.pipeline_command_text = ""
        self.current_cmdlet_name = ""
        self.current_stage_text = ""
        self.pipeline_values = {}
        self.pending_pipeline_tail = []
        self.pending_pipeline_source = None
        self.ui_library_refresh_callback = None
        self.pipeline_stop = None
        self.live_progress = None
        self.last_execution_result = {}
        self.progress_event_callback = None


# ---------------------------------------------------------------------------
# ContextVar and global fallback
# ---------------------------------------------------------------------------

_CTX_STATE: ContextVar[Optional[PipelineState]] = ContextVar("_pipeline_state", default=None)
_GLOBAL_STATE: PipelineState = PipelineState()


def _get_pipeline_state() -> PipelineState:
    state = _CTX_STATE.get()
    return state if state is not None else _GLOBAL_STATE


@contextmanager
def new_pipeline_state() -> Any:
    token = _CTX_STATE.set(PipelineState())
    try:
        yield _CTX_STATE.get()
    finally:
        _CTX_STATE.reset(token)


def get_pipeline_state() -> PipelineState:
    return _get_pipeline_state()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RESULT_TABLE_HISTORY = 20
PIPELINE_MISSING = object()


# ---------------------------------------------------------------------------
# Pipeline stop
# ---------------------------------------------------------------------------


def request_pipeline_stop(*, reason: str = "", exit_code: int = 0) -> None:
    state = _get_pipeline_state()
    state.pipeline_stop = {
        "reason": str(reason or "").strip(),
        "exit_code": int(exit_code),
    }


def get_pipeline_stop() -> Optional[Dict[str, Any]]:
    state = _get_pipeline_state()
    return state.pipeline_stop


def clear_pipeline_stop() -> None:
    state = _get_pipeline_state()
    state.pipeline_stop = None


# ---------------------------------------------------------------------------
# Stage context
# ---------------------------------------------------------------------------


def set_stage_context(context: Optional[PipelineStageContext]) -> None:
    state = _get_pipeline_state()
    state.current_context = context


def get_stage_context() -> Optional[PipelineStageContext]:
    state = _get_pipeline_state()
    return state.current_context


# ---------------------------------------------------------------------------
# Emit helpers
# ---------------------------------------------------------------------------


def emit(obj: Any) -> None:
    ctx = _get_pipeline_state().current_context
    if ctx is not None:
        ctx.emit(obj)


def emit_list(objects: List[Any]) -> None:
    ctx = _get_pipeline_state().current_context
    if ctx is not None:
        ctx.emit(objects)


def print_if_visible(*args: Any, file: Any = None, **kwargs: Any) -> None:
    try:
        ctx = _get_pipeline_state().current_context
        should_print = (ctx is None) or (ctx and ctx.is_last_stage)
        if file is not None:
            should_print = True
        if should_print:
            log(*args, **kwargs) if file is None else log(*args, file=file, **kwargs)
    except Exception:
        logger.exception("Error in print_if_visible")


# ---------------------------------------------------------------------------
# Pipeline values
# ---------------------------------------------------------------------------


def store_value(key: str, value: Any) -> None:
    if not isinstance(key, str):
        return
    text = key.strip().lower()
    if not text:
        return
    try:
        state = _get_pipeline_state()
        state.pipeline_values[text] = value
    except Exception:
        logger.exception("Failed to store pipeline value '%s'", key)


def load_value(key: str, default: Any = None) -> Any:
    if not isinstance(key, str):
        return default
    text = key.strip()
    if not text:
        return default
    parts = [segment.strip() for segment in text.split(".") if segment.strip()]
    if not parts:
        return default
    root_key = parts[0].lower()
    state = _get_pipeline_state()
    container = state.pipeline_values.get(root_key, PIPELINE_MISSING)
    if container is PIPELINE_MISSING:
        return default
    if len(parts) == 1:
        return container

    current: Any = container
    for fragment in parts[1:]:
        if isinstance(current, dict):
            fragment_lower = fragment.lower()
            if fragment in current:
                current = current[fragment]
                continue
            match = PIPELINE_MISSING
            for key_name, value in current.items():
                if isinstance(key_name, str) and key_name.lower() == fragment_lower:
                    match = value
                    break
            if match is PIPELINE_MISSING:
                return default
            current = match
            continue
        if isinstance(current, (list, tuple)):
            if fragment.isdigit():
                try:
                    idx = int(fragment)
                except ValueError:
                    return default
                if 0 <= idx < len(current):
                    current = current[idx]
                    continue
            return default
        if hasattr(current, fragment):
            try:
                current = getattr(current, fragment)
                continue
            except Exception:
                return default
        return default
    return current


# ---------------------------------------------------------------------------
# Execution result
# ---------------------------------------------------------------------------


def set_last_execution_result(
    *,
    status: str,
    error: str = "",
    command_text: str = "",
) -> None:
    state = _get_pipeline_state()
    text_status = str(status or "").strip().lower() or "unknown"
    state.last_execution_result = {
        "status": text_status,
        "success": text_status == "completed",
        "error": str(error or "").strip(),
        "command_text": str(command_text or "").strip(),
        "finished_at": time.time(),
    }


def get_last_execution_result() -> Dict[str, Any]:
    state = _get_pipeline_state()
    payload = state.last_execution_result
    return dict(payload) if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# Pending pipeline tail
# ---------------------------------------------------------------------------


def set_pending_pipeline_tail(
    stages: Optional[Sequence[Sequence[str]]],
    source_command: Optional[str] = None,
) -> None:
    state = _get_pipeline_state()
    try:
        pending: List[List[str]] = []
        for stage in stages or []:
            if isinstance(stage, (list, tuple)):
                pending.append([str(token) for token in stage])
        state.pending_pipeline_tail = pending
        clean_source = (source_command or "").strip()
        state.pending_pipeline_source = clean_source if clean_source else None
    except Exception:
        logger.exception("Failed to set pending pipeline tail; keeping existing pending tail")


def get_pending_pipeline_tail() -> List[List[str]]:
    state = _get_pipeline_state()
    return [list(stage) for stage in state.pending_pipeline_tail]


def get_pending_pipeline_source() -> Optional[str]:
    state = _get_pipeline_state()
    return state.pending_pipeline_source


def clear_pending_pipeline_tail() -> None:
    state = _get_pipeline_state()
    state.pending_pipeline_tail = []
    state.pending_pipeline_source = None


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def reset() -> None:
    state = _get_pipeline_state()
    state.reset()


# ---------------------------------------------------------------------------
# Emitted items
# ---------------------------------------------------------------------------


def get_emitted_items() -> List[Any]:
    state = _get_pipeline_state()
    ctx = state.current_context
    if ctx is not None:
        return list(ctx.emits)
    return []


def clear_emits() -> None:
    state = _get_pipeline_state()
    ctx = state.current_context
    if ctx is not None:
        ctx.emits.clear()


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def set_last_selection(indices: Sequence[int]) -> None:
    state = _get_pipeline_state()
    state.last_selection = list(indices or [])


def get_last_selection() -> List[int]:
    state = _get_pipeline_state()
    return list(state.last_selection)


def clear_last_selection() -> None:
    state = _get_pipeline_state()
    state.last_selection = []


# ---------------------------------------------------------------------------
# Command text
# ---------------------------------------------------------------------------


def set_current_command_text(command_text: Optional[str]) -> None:
    state = _get_pipeline_state()
    state.pipeline_command_text = (command_text or "").strip()


def get_current_command_text(default: str = "") -> str:
    state = _get_pipeline_state()
    text = state.pipeline_command_text.strip()
    return text if text else default


def clear_current_command_text() -> None:
    state = _get_pipeline_state()
    state.pipeline_command_text = ""


# ---------------------------------------------------------------------------
# Pipeline text splitting
# ---------------------------------------------------------------------------


def split_pipeline_text(pipeline_text: str) -> List[str]:
    text = str(pipeline_text or "")
    if not text:
        return []

    stages: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    escape = False

    for ch in text:
        if escape:
            buf.append(ch)
            escape = False
            continue

        if ch == "\\" and quote is not None:
            buf.append(ch)
            escape = True
            continue

        if ch in ('"', "'"):
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None
            buf.append(ch)
            continue

        if ch == "|" and quote is None:
            stages.append("".join(buf).strip())
            buf = []
            continue

        buf.append(ch)

    tail = "".join(buf).strip()
    if tail:
        stages.append(tail)
    return [s for s in stages if s]


def get_current_command_stages() -> List[str]:
    return split_pipeline_text(get_current_command_text(""))


# ---------------------------------------------------------------------------
# Stage text
# ---------------------------------------------------------------------------


def set_current_stage_text(stage_text: Optional[str]) -> None:
    state = _get_pipeline_state()
    state.current_stage_text = str(stage_text or "").strip()


def get_current_stage_text(default: str = "") -> str:
    state = _get_pipeline_state()
    text = state.current_stage_text.strip()
    return text if text else default


def clear_current_stage_text() -> None:
    state = _get_pipeline_state()
    state.current_stage_text = ""


# ---------------------------------------------------------------------------
# Cmdlet name
# ---------------------------------------------------------------------------


def set_current_cmdlet_name(cmdlet_name: Optional[str]) -> None:
    state = _get_pipeline_state()
    state.current_cmdlet_name = str(cmdlet_name or "").strip()


def get_current_cmdlet_name(default: str = "") -> str:
    state = _get_pipeline_state()
    text = state.current_cmdlet_name.strip()
    return text if text else default


def clear_current_cmdlet_name() -> None:
    state = _get_pipeline_state()
    state.current_cmdlet_name = ""


# ---------------------------------------------------------------------------
# Search query
# ---------------------------------------------------------------------------


def set_search_query(query: Optional[str]) -> None:
    state = _get_pipeline_state()
    state.last_search_query = query


def get_search_query() -> Optional[str]:
    state = _get_pipeline_state()
    return state.last_search_query


# ---------------------------------------------------------------------------
# Refresh tracking
# ---------------------------------------------------------------------------


def set_pipeline_refreshed(refreshed: bool) -> None:
    state = _get_pipeline_state()
    state.pipeline_refreshed = refreshed


def was_pipeline_refreshed() -> bool:
    state = _get_pipeline_state()
    return state.pipeline_refreshed


# ---------------------------------------------------------------------------
# Last items
# ---------------------------------------------------------------------------


def set_last_items(items: list) -> None:
    state = _get_pipeline_state()
    state.last_items = list(items) if items else []


def get_last_items() -> List[Any]:
    state = _get_pipeline_state()
    return list(state.last_items)


# ---------------------------------------------------------------------------
# UI library refresh callback
# ---------------------------------------------------------------------------


def set_ui_library_refresh_callback(callback: Any) -> None:
    state = _get_pipeline_state()
    state.ui_library_refresh_callback = callback


def get_ui_library_refresh_callback() -> Optional[Any]:
    state = _get_pipeline_state()
    return state.ui_library_refresh_callback


def trigger_ui_library_refresh(library_filter: str = "local") -> None:
    callback = get_ui_library_refresh_callback()
    if callback:
        try:
            callback(library_filter)
        except Exception as e:
            print(
                f"[trigger_ui_library_refresh] Error calling refresh callback: {e}",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Result table: set (push to history)
# ---------------------------------------------------------------------------


def set_last_result_table(
    result_table: Optional[Any],
    items: Optional[List[Any]] = None,
    subject: Optional[Any] = None,
) -> None:
    state = _get_pipeline_state()

    if state.last_result_table is not None:
        state.result_table_history.append(
            (
                state.last_result_table,
                state.last_result_items,
                state.last_result_subject,
            )
        )
        if len(state.result_table_history) > MAX_RESULT_TABLE_HISTORY:
            state.result_table_history.pop(0)

    state.display_items = []
    state.display_table = None
    state.display_subject = None
    state.last_result_table = result_table
    state.last_result_items = items or []
    state.last_result_subject = subject

    if (
        result_table is not None
        and hasattr(result_table, "sort_by_title")
        and not getattr(result_table, "preserve_order", False)
    ):
        try:
            result_table.sort_by_title()
            if state.last_result_items and hasattr(result_table, "rows"):
                sorted_items: List[Any] = []
                for row in result_table.rows:
                    src_idx = getattr(row, "source_index", None)
                    if isinstance(src_idx, int) and 0 <= src_idx < len(state.last_result_items):
                        sorted_items.append(state.last_result_items[src_idx])
                if result_table.rows and len(sorted_items) == len(result_table.rows):
                    state.last_result_items = sorted_items
        except Exception:
            logger.exception("Failed to sort result_table and reorder items")


# ---------------------------------------------------------------------------
# Result table: overlay (no history push)
# ---------------------------------------------------------------------------


def set_last_result_table_overlay(
    result_table: Optional[Any],
    items: Optional[List[Any]] = None,
    subject: Optional[Any] = None,
) -> None:
    state = _get_pipeline_state()
    state.display_table = result_table
    state.display_items = items or []
    state.display_subject = subject


# ---------------------------------------------------------------------------
# Result table: items only (no history, no table)
# ---------------------------------------------------------------------------


def set_last_result_items_only(items: Optional[List[Any]]) -> None:
    state = _get_pipeline_state()
    state.display_items = items or []
    state.display_table = None
    state.display_subject = None


# ---------------------------------------------------------------------------
# Result table history navigation (consolidated)
# ---------------------------------------------------------------------------


def _restore_result_table(forward: bool) -> bool:
    """Consolidated implementation for restore_previous/restore_next_result_table."""
    state = _get_pipeline_state()
    pop_stack = state.result_table_forward if forward else state.result_table_history
    push_stack = state.result_table_history if forward else state.result_table_forward
    label = "restore_next_result_table" if forward else "restore_previous_result_table"

    if state.display_items or state.display_table or state.display_subject is not None:
        state.display_items = []
        state.display_table = None
        state.display_subject = None
        if state.last_result_table is not None:
            state.current_stage_table = state.last_result_table
            return True
        if not pop_stack:
            state.current_stage_table = state.last_result_table
            return True

    if not pop_stack:
        return False

    push_stack.append(
        (state.last_result_table, state.last_result_items, state.last_result_subject)
    )

    prev = pop_stack.pop()
    if isinstance(prev, tuple) and len(prev) >= 3:
        state.last_result_table, state.last_result_items, state.last_result_subject = (
            prev[0],
            prev[1],
            prev[2],
        )
    elif isinstance(prev, tuple) and len(prev) == 2:
        state.last_result_table, state.last_result_items = prev
        state.last_result_subject = None
    else:
        state.last_result_table, state.last_result_items, state.last_result_subject = None, [], None

    state.display_items = []
    state.display_table = None
    state.display_subject = None
    state.current_stage_table = state.last_result_table

    try:
        debug_table_state(label)
    except Exception:
        logger.exception("Failed to debug_table_state during %s", label)

    return True


def restore_previous_result_table() -> bool:
    return _restore_result_table(forward=False)


def restore_next_result_table() -> bool:
    return _restore_result_table(forward=True)


def _cached_item_field(item: Any, *names: str) -> str:
    for name in names:
        try:
            if isinstance(item, dict):
                value = item.get(name)
            else:
                value = getattr(item, name, None)
                if value is None and hasattr(item, "get"):
                    try:
                        value = item.get(name)
                    except Exception:
                        value = None
        except Exception:
            value = None
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _cached_item_matches(
    item: Any,
    *,
    file_hash: str = "",
    instance: str = "",
    path: str = "",
) -> bool:
    if item is None:
        return False
    want_hash = str(file_hash or "").strip().lower()
    want_store = str(instance or "").strip().lower()
    want_path = str(path or "").strip().lower()
    have_hash = _cached_item_field(item, "hash").lower()
    have_store = _cached_item_field(item, "store", "instance").lower()
    have_path = _cached_item_field(item, "path").lower()
    if want_store and have_store and have_store != want_store:
        return False
    if want_hash and have_hash == want_hash:
        return True
    if want_path and have_path == want_path:
        return True
    return False


def _format_cached_tag_column(tags: Sequence[Any]) -> str:
    parts: List[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        text = str(raw or "").strip()
        if not text or ":" not in text:
            continue
        if text.lower().startswith("title:"):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        parts.append(text)
    return ", ".join(parts)


def _set_cached_item_title(item: Any, title: str) -> None:
    if isinstance(item, dict):
        item["title"] = title
        item["name"] = title
        cols = item.get("columns")
    else:
        try:
            setattr(item, "title", title)
        except Exception:
            pass
        cols = getattr(item, "columns", None)
    if not isinstance(cols, list):
        return
    updated = []
    changed = False
    for col in cols:
        if isinstance(col, (tuple, list)) and len(col) >= 2:
            label, _val = col[0], col[1]
            if str(label).lower() == "title":
                updated.append((label, title, *list(col[2:])))
                changed = True
            else:
                updated.append(col)
        else:
            updated.append(col)
    if changed and isinstance(item, dict):
        item["columns"] = updated
    elif changed:
        try:
            item.columns = updated
        except Exception:
            pass


def _set_cached_item_tags(item: Any, tags: Sequence[Any]) -> None:
    cleaned = [str(t).strip() for t in tags or [] if str(t).strip()]
    if isinstance(item, dict):
        item["tag"] = list(cleaned)
        cols = item.get("columns")
    else:
        try:
            current = getattr(item, "tag", None)
            if isinstance(current, set):
                keep = {str(x) for x in current if ":" not in str(x)}
                setattr(item, "tag", keep | set(cleaned))
            else:
                setattr(item, "tag", list(cleaned))
        except Exception:
            pass
        cols = getattr(item, "columns", None)
    tag_text = _format_cached_tag_column(cleaned)
    if not isinstance(cols, list):
        return
    updated = []
    changed = False
    for col in cols:
        if isinstance(col, (tuple, list)) and len(col) >= 2:
            label = col[0]
            if str(label).lower() in {"tag", "tags"}:
                updated.append((label, tag_text, *list(col[2:])))
                changed = True
            else:
                updated.append(col)
        else:
            updated.append(col)
    if changed and isinstance(item, dict):
        item["columns"] = updated
    elif changed:
        try:
            item.columns = updated
        except Exception:
            pass


def _set_row_column(row: Any, name: str, value: str) -> None:
    want = str(name or "").strip().lower()
    cols = getattr(row, "columns", None)
    if not isinstance(cols, list):
        return
    for col in cols:
        label = str(getattr(col, "name", "") or "").lower()
        if label == want:
            try:
                col.value = value
            except Exception:
                pass
            return


def patch_cached_result_items(
    *,
    file_hash: Optional[str] = None,
    instance: Optional[str] = None,
    path: Optional[str] = None,
    title: Optional[str] = None,
    tags: Optional[Sequence[Any]] = None,
) -> int:
    """Update matching rows in the current table, overlay, and @.. history in place."""
    title_text = str(title or "").strip()
    tag_list = None if tags is None else [str(t).strip() for t in tags if str(t).strip()]
    if not title_text and tag_list is None:
        return 0

    state = _get_pipeline_state()
    stacks = [
        (state.last_result_table, state.last_result_items),
        (state.display_table, state.display_items),
        (state.current_stage_table, None),
    ]
    for entry in list(state.result_table_history or []) + list(state.result_table_forward or []):
        if isinstance(entry, tuple) and len(entry) >= 2:
            stacks.append((entry[0], entry[1]))

    patched = 0
    seen: set[int] = set()

    def _touch(item: Any) -> bool:
        if item is None:
            return False
        marker = id(item)
        if marker in seen:
            return True
        if not _cached_item_matches(
            item, file_hash=file_hash or "", instance=instance or "", path=path or ""
        ):
            return False
        seen.add(marker)
        if title_text:
            _set_cached_item_title(item, title_text)
        if tag_list is not None:
            _set_cached_item_tags(item, tag_list)
        return True

    for table, items in stacks:
        if isinstance(items, list):
            for item in items:
                if _touch(item):
                    patched += 1
        rows = getattr(table, "rows", None) if table is not None else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            payload = getattr(row, "payload", None)
            hit = _touch(payload)
            if not hit and not _cached_item_matches(
                row, file_hash=file_hash or "", instance=instance or "", path=path or ""
            ):
                continue
            if title_text:
                _set_row_column(row, "Title", title_text)
            if tag_list is not None:
                _set_row_column(row, "Tag", _format_cached_tag_column(tag_list))
            if not hit:
                patched += 1
    return patched


# ---------------------------------------------------------------------------
# Display table accessors
# ---------------------------------------------------------------------------


def get_display_table() -> Optional[Any]:
    state = _get_pipeline_state()
    return state.display_table


def get_last_result_subject() -> Optional[Any]:
    state = _get_pipeline_state()
    if state.display_subject is not None:
        return state.display_subject
    return state.last_result_subject


def get_last_result_table() -> Optional[Any]:
    state = _get_pipeline_state()
    return state.last_result_table


def get_last_result_items() -> List[Any]:
    state = _get_pipeline_state()
    if state.display_items:
        if state.display_table is not None and not _is_selectable_table(state.display_table):
            return []
        return state.display_items
    if state.last_result_table is None:
        return state.last_result_items
    if _is_selectable_table(state.last_result_table):
        return state.last_result_items
    return []


# ---------------------------------------------------------------------------
# Debug table state
# ---------------------------------------------------------------------------


def debug_table_state(label: str = "") -> None:
    if not is_debug_enabled():
        return

    state = _get_pipeline_state()

    def _tbl_summary(name: str, t: Any) -> tuple[str, str]:
        if t is None:
            return (name, "None")
        try:
            table_type = getattr(t, "table", None)
        except Exception:
            table_type = None
        try:
            title = getattr(t, "title", None)
        except Exception:
            title = None
        try:
            src_cmd = getattr(t, "source_command", None)
        except Exception:
            src_cmd = None
        try:
            src_args = getattr(t, "source_args", None)
        except Exception:
            src_args = None
        try:
            no_choice = bool(getattr(t, "no_choice", False))
        except Exception:
            no_choice = False
        try:
            preserve_order = bool(getattr(t, "preserve_order", False))
        except Exception:
            preserve_order = False
        try:
            row_count = len(getattr(t, "rows", []) or [])
        except Exception:
            row_count = 0
        try:
            meta = (
                t.get_table_metadata()
                if hasattr(t, "get_table_metadata")
                else getattr(t, "table_metadata", None)
            )
        except Exception:
            meta = None
        meta_keys = list(meta.keys()) if isinstance(meta, dict) else []

        summary = (
            f"id={id(t)} class={type(t).__name__} title={title!r} table={table_type!r} "
            f"rows={row_count} source={src_cmd!r} source_args={src_args!r} "
            f"no_choice={no_choice} preserve_order={preserve_order} meta_keys={meta_keys}"
        )
        return (name, summary)

    rows: List[tuple[str, Any]] = []
    if label:
        rows.append(("state", label))
    rows.append(_tbl_summary("display_table", getattr(state, "display_table", None)))
    rows.append(_tbl_summary("current_stage_table", getattr(state, "current_stage_table", None)))
    rows.append(_tbl_summary("last_result_table", getattr(state, "last_result_table", None)))

    try:
        buffers = (
            f"display_items={len(state.display_items or [])} "
            f"last_result_items={len(state.last_result_items or [])} "
            f"history={len(state.result_table_history or [])} "
            f"forward={len(state.result_table_forward or [])} "
            f"last_selection={list(state.last_selection or [])}"
        )
    except Exception:
        buffers = "<unavailable>"
    rows.append(("buffers", buffers))

    status_panel("Table State", rows)


# ---------------------------------------------------------------------------
# Selectable result items
# ---------------------------------------------------------------------------


def get_last_selectable_result_items() -> List[Any]:
    state = _get_pipeline_state()
    if state.last_result_table is None:
        return list(state.last_result_items)
    if _is_selectable_table(state.last_result_table):
        return list(state.last_result_items)
    return []


# ---------------------------------------------------------------------------
# Table source command/args accessors (consolidated helpers)
# ---------------------------------------------------------------------------


def _get_table_source_command(table: Any) -> Optional[str]:
    if table is not None and _is_selectable_table(table) and hasattr(table, "source_command"):
        return getattr(table, "source_command")
    return None


def _get_table_source_args(table: Any) -> List[str]:
    if table is not None and _is_selectable_table(table) and hasattr(table, "source_args"):
        return getattr(table, "source_args") or []
    return []


def _get_table_row_selection_args(table: Any, row_index: int) -> Optional[List[str]]:
    if table is not None and _is_selectable_table(table) and hasattr(table, "rows"):
        rows = table.rows
        if 0 <= row_index < len(rows):
            row = rows[row_index]
            if hasattr(row, "selection_args"):
                return getattr(row, "selection_args")
    return None


def _get_table_row_selection_action(table: Any, row_index: int) -> Optional[List[str]]:
    if table is not None and _is_selectable_table(table) and hasattr(table, "rows"):
        rows = table.rows
        if 0 <= row_index < len(rows):
            row = rows[row_index]
            if hasattr(row, "selection_action"):
                return getattr(row, "selection_action")
    return None


# ---------------------------------------------------------------------------
# Public table accessor functions (delegate to helpers)
# ---------------------------------------------------------------------------


def get_last_result_table_source_command() -> Optional[str]:
    state = _get_pipeline_state()
    return _get_table_source_command(state.last_result_table)


def get_last_result_table_source_args() -> List[str]:
    state = _get_pipeline_state()
    return _get_table_source_args(state.last_result_table)


def get_last_result_table_row_selection_args(row_index: int) -> Optional[List[str]]:
    state = _get_pipeline_state()
    return _get_table_row_selection_args(state.last_result_table, row_index)


def get_last_result_table_row_selection_action(row_index: int) -> Optional[List[str]]:
    state = _get_pipeline_state()
    return _get_table_row_selection_action(state.last_result_table, row_index)


def get_current_stage_table() -> Optional[Any]:
    state = _get_pipeline_state()
    return state.current_stage_table


def set_current_stage_table(result_table: Optional[Any]) -> None:
    state = _get_pipeline_state()
    state.current_stage_table = result_table


def get_current_stage_table_source_command() -> Optional[str]:
    state = _get_pipeline_state()
    return _get_table_source_command(state.current_stage_table)


def get_current_stage_table_source_args() -> List[str]:
    state = _get_pipeline_state()
    return _get_table_source_args(state.current_stage_table)


def get_current_stage_table_row_selection_args(row_index: int) -> Optional[List[str]]:
    state = _get_pipeline_state()
    return _get_table_row_selection_args(state.current_stage_table, row_index)


def get_current_stage_table_row_selection_action(row_index: int) -> Optional[List[str]]:
    state = _get_pipeline_state()
    return _get_table_row_selection_action(state.current_stage_table, row_index)


def get_current_stage_table_row_source_index(row_index: int) -> Optional[int]:
    state = _get_pipeline_state()
    table = state.current_stage_table
    if table is not None and _is_selectable_table(table) and hasattr(table, "rows"):
        rows = table.rows
        if 0 <= row_index < len(rows):
            row = rows[row_index]
            return getattr(row, "source_index", None)
    return None


def clear_last_result() -> None:
    state = _get_pipeline_state()
    state.last_result_table = None
    state.last_result_items = []
    state.last_result_subject = None


# ---------------------------------------------------------------------------
# Pipeline token splitting (used by executor)
# ---------------------------------------------------------------------------


def _split_pipeline_tokens(tokens: Sequence[str]) -> List[List[str]]:
    stages: List[List[str]] = []
    current: List[str] = []
    for token in tokens:
        if token == "|":
            if current:
                stages.append(current)
                current = []
            continue
        current.append(str(token))
    if current:
        stages.append(current)
    return [stage for stage in stages if stage]


# ---------------------------------------------------------------------------
# Module __all__
# ---------------------------------------------------------------------------

__all__ = [
    # State class
    "PipelineState",
    # ContextVar and factories
    "_CTX_STATE",
    "_GLOBAL_STATE",
    "_get_pipeline_state",
    "get_pipeline_state",
    "new_pipeline_state",
    # Constants
    "MAX_RESULT_TABLE_HISTORY",
    "PIPELINE_MISSING",
    "HELP_EXAMPLE_SOURCE_COMMANDS",
    # Deferred module loaders
    "_worker",
    "_cli_parsing",
    "_result_table",
    # Live progress
    "set_live_progress",
    "get_live_progress",
    "set_progress_event_callback",
    "get_progress_event_callback",
    "suspend_live_progress",
    # Pipeline stop
    "request_pipeline_stop",
    "get_pipeline_stop",
    "clear_pipeline_stop",
    # Stage context
    "set_stage_context",
    "get_stage_context",
    "PipelineStageContext",
    # Emit helpers
    "emit",
    "emit_list",
    "print_if_visible",
    # Pipeline values
    "store_value",
    "load_value",
    # Execution result
    "set_last_execution_result",
    "get_last_execution_result",
    # Pending pipeline tail
    "set_pending_pipeline_tail",
    "get_pending_pipeline_tail",
    "get_pending_pipeline_source",
    "clear_pending_pipeline_tail",
    # Reset
    "reset",
    # Emitted items
    "get_emitted_items",
    "clear_emits",
    # Selection
    "set_last_selection",
    "get_last_selection",
    "clear_last_selection",
    # Command text
    "set_current_command_text",
    "get_current_command_text",
    "clear_current_command_text",
    # Pipeline splitting
    "split_pipeline_text",
    "get_current_command_stages",
    "_split_pipeline_tokens",
    # Stage text
    "set_current_stage_text",
    "get_current_stage_text",
    "clear_current_stage_text",
    # Cmdlet name
    "set_current_cmdlet_name",
    "get_current_cmdlet_name",
    "clear_current_cmdlet_name",
    # Search query
    "set_search_query",
    "get_search_query",
    # Refresh
    "set_pipeline_refreshed",
    "was_pipeline_refreshed",
    # Last items
    "set_last_items",
    "get_last_items",
    # UI library
    "set_ui_library_refresh_callback",
    "get_ui_library_refresh_callback",
    "trigger_ui_library_refresh",
    # Result table setters
    "set_last_result_table",
    "set_last_result_table_overlay",
    "set_last_result_items_only",
    # History navigation
    "restore_previous_result_table",
    "restore_next_result_table",
    "patch_cached_result_items",
    # Display getters
    "get_display_table",
    "get_last_result_subject",
    "get_last_result_table",
    "get_last_result_items",
    "get_last_selectable_result_items",
    # Table accessors
    "get_last_result_table_source_command",
    "get_last_result_table_source_args",
    "get_last_result_table_row_selection_args",
    "get_last_result_table_row_selection_action",
    "get_current_stage_table",
    "set_current_stage_table",
    "get_current_stage_table_source_command",
    "get_current_stage_table_source_args",
    "get_current_stage_table_row_selection_args",
    "get_current_stage_table_row_selection_action",
    "get_current_stage_table_row_source_index",
    # Clear
    "clear_last_result",
    # Debug
    "debug_table_state",
    # Helpers (used internally by executor)
    "_emit_selection_debug_panel",
    "_is_selectable_table",
    "_get_table_source_command",
    "_get_table_source_args",
    "_get_table_row_selection_args",
    "_get_table_row_selection_action",
]
