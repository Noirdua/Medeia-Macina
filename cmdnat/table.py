from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from SYS.cmdlet_spec import Cmdlet, CmdletArg, QueryArg, SharedArgs, parse_cmdlet_args
from SYS.item_accessors import get_field
from SYS.logger import log, status_panel
from SYS.result_table import Column, Table
from SYS.rich_display import stdout_console


_NUMERIC_NAMESPACE_HINTS = {
    "track",
    "disk",
    "disc",
    "episode",
    "season",
    "chapter",
    "volume",
    "part",
}
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_ILLEGAL_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*]')
_COUNT_FILTER_RE = re.compile(
    r"^@?([A-Za-z][A-Za-z0-9_. -]*?)\s*(<=|>=|!=|<>|==|=|<|>)\s*(\d+)\s*$"
)
_SORT_SPEC_RE = re.compile(
    r"^@?(?P<column>[A-Za-z][A-Za-z0-9_. -]*?)\s*\(\s*(?P<body>.*)\s*\)\s*$"
)
_SORT_ORDERS = {
    "asc": False,
    "ascending": False,
    "a-z": False,
    "desc": True,
    "descending": True,
    "reverse": True,
    "z-a": True,
}


def _normalize_bool(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on", "y"}


def _strip_sort_namespace_token(raw: str) -> str:
    text = str(raw or "").strip().strip('"').strip("'")
    if text.startswith("$(") and text.endswith(")"):
        text = text[2:-1].strip()
    elif text.startswith("#(") and text.endswith(")"):
        text = text[2:-1].strip()
    elif text.startswith("$") or text.startswith("#"):
        text = text[1:].strip()
    return text.rstrip(":").strip()


def parse_sort_spec(text: str) -> Dict[str, str]:
    """Parse `-sort @Tag($part,asc)` / `@Title(desc)` into column, namespace, order."""
    raw = str(text or "").strip().strip('"').strip("'")
    out = {"column": "", "namespace": "", "order": ""}
    match = _SORT_SPEC_RE.match(raw)
    if not match:
        out["column"] = raw.lstrip("@").strip()
        return out

    out["column"] = str(match.group("column") or "").strip()
    parts = [part.strip() for part in str(match.group("body") or "").split(",") if str(part).strip()]
    if not parts:
        return out

    first = parts[0].strip().lower()
    if first in _SORT_ORDERS and len(parts) == 1:
        out["order"] = first
        return out

    out["namespace"] = _strip_sort_namespace_token(parts[0])
    if len(parts) >= 2:
        out["order"] = parts[1].strip().lower()
    return out


def split_sort_specs(text: str) -> List[str]:
    """Split `@Tag($series),@Tag($episode)` on commas outside parentheses."""
    raw = str(text or "").strip().strip('"').strip("'")
    if not raw:
        return []
    parts: List[str] = []
    current: List[str] = []
    depth = 0
    for ch in raw:
        if ch == "(":
            depth += 1
            current.append(ch)
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            current.append(ch)
            continue
        if ch == "," and depth == 0:
            piece = "".join(current).strip()
            if piece:
                parts.append(piece)
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts or [raw]


def parse_sort_specs(text: str) -> List[Dict[str, str]]:
    return [parse_sort_spec(part) for part in split_sort_specs(text) if str(part).strip()]


def _parse_table_query(query: Any) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    raw = str(query or "").strip()
    if not raw:
        return fields

    for chunk in re.split(r"[;,]+", raw):
        part = str(chunk or "").strip()
        if not part:
            continue
        if _COUNT_FILTER_RE.match(part):
            fields.setdefault("_filters", [])
            if isinstance(fields["_filters"], list):
                fields["_filters"].append(part)
            continue
        sep_index = part.find(":")
        if sep_index < 0:
            sep_index = part.find("=")
        if sep_index <= 0:
            continue
        key = part[:sep_index].strip().lower()
        value = part[sep_index + 1 :].strip().strip('"').strip("'")
        if key:
            fields[key] = value
    return fields


def _active_table_bundle(ctx: Any) -> Tuple[Any, str]:
    display_table = ctx.get_display_table() if hasattr(ctx, "get_display_table") else None
    if display_table is not None:
        return display_table, "display"

    current_stage_table = ctx.get_current_stage_table() if hasattr(ctx, "get_current_stage_table") else None
    if current_stage_table is not None:
        return current_stage_table, "stage"

    last_result_table = ctx.get_last_result_table() if hasattr(ctx, "get_last_result_table") else None
    if last_result_table is not None:
        return last_result_table, "last"

    return None, ""


def _clone_table(source: Any) -> Any:
    if source is None or not isinstance(source, Table):
        return source

    cloned = source.copy_with_title(str(getattr(source, "title", "") or ""))
    for source_row in getattr(source, "rows", []) or []:
        row = cloned.add_row()
        row.columns = [
            Column(col.name, col.value, getattr(col, "width", None))
            for col in getattr(source_row, "columns", []) or []
        ]
        row.selection_args = list(getattr(source_row, "selection_args", []) or []) or None
        row.selection_action = list(getattr(source_row, "selection_action", []) or []) or None
        row.source_index = getattr(source_row, "source_index", None)
        row.payload = getattr(source_row, "payload", None)
    return cloned


def _table_headers(table: Any) -> List[str]:
    names: List[str] = []
    seen: set[str] = set()
    for row in getattr(table, "rows", []) or []:
        for col in getattr(row, "columns", []) or []:
            name = str(getattr(col, "name", "") or "").strip()
            key = name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            names.append(name)
        if names:
            break
    return names


def _resolve_column_header(headers: Sequence[str], wanted: str) -> str:
    raw = str(wanted or "").strip().lstrip("@").strip()
    if not raw:
        return ""
    key = raw.lower()
    for header in headers:
        if header.lower() == key:
            return header
    prefixed = [header for header in headers if header.lower().startswith(key)]
    if len(prefixed) == 1:
        return prefixed[0]
    if key in {"tag", "tags"}:
        for header in headers:
            if header.lower() in {"tag", "tags"} or header.lower().startswith("tag"):
                return header
    return raw


def _parse_count_filter(text: str) -> Optional[Tuple[str, str, int]]:
    match = _COUNT_FILTER_RE.match(str(text or "").strip().strip('"').strip("'"))
    if not match:
        return None
    return match.group(1).strip().lstrip("@").strip().lower(), match.group(2), int(match.group(3))


def _compare_count(count: int, op: str, bound: int) -> bool:
    if op == "<=":
        return count <= bound
    if op == ">=":
        return count >= bound
    if op in {"!=", "<>"}:
        return count != bound
    if op in {"=", "=="}:
        return count == bound
    if op == "<":
        return count < bound
    if op == ">":
        return count > bound
    return False


def _tags_from_payload(payload: Any) -> List[str]:
    tags: Any = None
    try:
        tags = get_field(payload, "tag")
    except Exception:
        tags = None
    if tags is None:
        try:
            tags = get_field(payload, "tags") or getattr(payload, "tag", None)
        except Exception:
            tags = getattr(payload, "tag", None) if payload is not None else None
    if isinstance(tags, str):
        return [part.strip() for part in re.split(r",\s*", tags) if part.strip()]
    if isinstance(tags, (list, tuple, set)):
        return [str(part).strip() for part in tags if str(part).strip()]
    return []


def _column_is_tag_like(name: str) -> bool:
    key = str(name or "").strip().lower()
    return key in {"tag", "tags"} or key.startswith("tag")


def _column_entry_count(row: Any, column_name: str, items: Sequence[Any]) -> int:
    wanted = str(column_name or "").strip()
    wanted_key = wanted.lower()
    payload = getattr(row, "payload", None)
    if payload is None:
        source_index = getattr(row, "source_index", None)
        if isinstance(source_index, int) and 0 <= source_index < len(items):
            payload = items[source_index]

    if _column_is_tag_like(wanted):
        tags = _tags_from_payload(payload)
        if tags:
            return len(tags)

    text = ""
    get_col = getattr(row, "get_column", None)
    if callable(get_col):
        text = str(get_col(wanted) or get_col(wanted_key) or "")
    if _column_is_tag_like(wanted) and not text:
        text = str(getattr(payload, "tag_summary", "") or "")
    cleaned = str(text or "").strip()
    if not cleaned:
        return 0
    extra = re.search(r"\+(\d+)\s+more\b", cleaned, flags=re.IGNORECASE)
    parts = [part.strip() for part in re.split(r",\s*", cleaned) if part.strip()]
    parts = [part for part in parts if not re.match(r"\+\d+\s+more\b", part, flags=re.IGNORECASE)]
    count = len(parts) if ("," in cleaned or extra) else (0 if not cleaned else 1)
    if extra:
        try:
            count += int(extra.group(1))
        except Exception:
            pass
    return count


def _collect_count_filters(filter_arg: str, query_fields: Dict[str, Any]) -> List[Tuple[str, str, int]]:
    specs: List[Tuple[str, str, int]] = []
    chunks: List[str] = []
    if filter_arg:
        chunks.append(str(filter_arg).strip())
    extra = query_fields.get("filter") or query_fields.get("count")
    if extra:
        chunks.append(str(extra).strip())
    raw_list = query_fields.get("_filters")
    if isinstance(raw_list, list):
        chunks.extend(str(item).strip() for item in raw_list if str(item).strip())
    for chunk in chunks:
        parsed = _parse_count_filter(chunk)
        if parsed:
            specs.append(parsed)
    return specs


def _apply_table_filters(
    table: Any,
    filters: Sequence[Tuple[str, str, int]],
    items: Sequence[Any],
) -> None:
    if table is None or not filters or not hasattr(table, "rows"):
        return
    headers = _table_headers(table)
    resolved = [
        (_resolve_column_header(headers, column), op, bound)
        for column, op, bound in filters
    ]
    kept = []
    for row in list(getattr(table, "rows", []) or []):
        if all(
            _compare_count(_column_entry_count(row, column, items), op, bound)
            for column, op, bound in resolved
            if column
        ):
            kept.append(row)
    table.rows = kept


def _column_sort_key(value: Any, *, numeric: bool = False) -> Tuple[int, Any, str]:
    text = str(value or "").strip()
    if not text:
        return (1, float("inf") if numeric else "", "")
    if numeric:
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if match:
            try:
                return (0, float(match.group(0)), text.casefold())
            except Exception:
                pass
        return (0, float("inf"), text.casefold())
    return (0, text.casefold(), text.casefold())


class _ReverseSortKey:
    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __lt__(self, other: Any) -> bool:
        return self.value > getattr(other, "value", other)

    def __eq__(self, other: Any) -> bool:
        return self.value == getattr(other, "value", other)


def _row_tag_blob(row: Any) -> Any:
    payload = getattr(row, "payload", None)
    if isinstance(payload, dict):
        for key in ("tag", "tags", "tag_summary"):
            value = payload.get(key)
            if value:
                return value
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            for key in ("tag", "tags", "tag_summary"):
                value = metadata.get(key)
                if value:
                    return value
    elif payload is not None:
        for key in ("tag", "tags", "tag_summary"):
            value = getattr(payload, key, None)
            if value:
                return value
    try:
        return row.get_column("Tag")
    except Exception:
        return []


def _spec_sort_key(row: Any, spec: Dict[str, str], query_fields: Dict[str, Any]) -> Any:
    wanted_column = str(spec.get("column") or "").strip()
    namespace = str(spec.get("namespace") or "").strip().rstrip(":")
    order = str(spec.get("order") or "asc").strip().lower()
    reverse = _SORT_ORDERS.get(order, False)
    numeric_field = query_fields.get("numeric")
    if numeric_field is not None:
        numeric = _normalize_bool(numeric_field)
    else:
        numeric = namespace.casefold() in _NUMERIC_NAMESPACE_HINTS
    if not wanted_column and namespace:
        wanted_column = "tag"
    if str(wanted_column).strip().lower() == "tag" and namespace:
        from SYS.result_table import _namespace_sort_key

        key = _namespace_sort_key(_row_tag_blob(row), namespace, numeric=bool(numeric))
    else:
        header = wanted_column or "Title"
        key = _column_sort_key(row.get_column(header), numeric=bool(numeric))
    return _ReverseSortKey(key) if reverse else key


def _sort_by_column(table: Any, column_name: str, *, numeric: bool = False, reverse: bool = False) -> None:
    if table is None or not hasattr(table, "rows"):
        return

    wanted = str(column_name or "").strip().lower()
    if not wanted:
        return

    if wanted in {"title", "name"} and hasattr(table, "sort_by_title"):
        table.sort_by_title()
        if reverse and hasattr(table, "rows"):
            table.rows.reverse()
        return

    if wanted == "tag" and hasattr(table, "sort_by_title"):
        table.rows.sort(
            key=lambda row: _column_sort_key(row.get_column("Tag"), numeric=numeric),
            reverse=bool(reverse),
        )
        return

    table.rows.sort(
        key=lambda row: _column_sort_key(row.get_column(column_name), numeric=numeric),
        reverse=bool(reverse),
    )


def _reorder_items_from_table(table: Any, items: List[Any]) -> List[Any]:
    if not items or table is None or not hasattr(table, "rows"):
        return list(items or [])

    payloads: List[Any] = []
    for row in getattr(table, "rows", []) or []:
        payload = getattr(row, "payload", None)
        if payload is None:
            payloads = []
            break
        payloads.append(payload)
    if payloads and len(payloads) == len(getattr(table, "rows", []) or []):
        return payloads

    reordered: List[Any] = []
    for row in getattr(table, "rows", []) or []:
        source_index = getattr(row, "source_index", None)
        if isinstance(source_index, int) and 0 <= source_index < len(items):
            reordered.append(items[source_index])

    if reordered and len(reordered) == len(getattr(table, "rows", []) or []):
        return reordered
    return list(items or [])


def _render_table(table: Any) -> int:
    if table is None:
        log("No active result table", file=sys.stderr)
        return 1

    try:
        setattr(table, "_rendered_by_cmdlet", True)
    except Exception:
        pass

    try:
        if hasattr(table, "to_rich"):
            stdout_console().print(table.to_rich())
            return 0
    except Exception as exc:
        log(f"Failed to render table: {exc}", file=sys.stderr)
        return 1

    try:
        stdout_console().print(table)
        return 0
    except Exception as exc:
        log(f"Failed to print table: {exc}", file=sys.stderr)
        return 1


def _sanitize_filename_base(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return "table"

    s = _ILLEGAL_FILENAME_CHARS_RE.sub(" ", s)
    s = "".join(ch for ch in s if ch.isprintable())
    s = " ".join(s.split()).strip()
    s = s.rstrip(" .")

    if not s:
        s = "table"
    if s.lower() in _WINDOWS_RESERVED_NAMES:
        s = f"_{s}"
    if len(s) > 200:
        s = s[:200].rstrip(" .")
    return s or "table"


def _resolve_output_path(path_arg: str, *, table_title: str) -> Path:
    raw = str(path_arg or "").strip()
    if not raw:
        raise ValueError("-path is required")

    ends_with_sep = raw.endswith(("/", "\\"))
    target = Path(raw)

    if target.exists() and target.is_dir():
        return target / f"{_sanitize_filename_base(table_title)}.svg"

    if (ends_with_sep or not target.suffix) and not target.exists():
        target.mkdir(parents=True, exist_ok=True)
        return target / f"{_sanitize_filename_base(table_title)}.svg"

    if not target.suffix:
        target.parent.mkdir(parents=True, exist_ok=True)
        return target.with_suffix(".svg")
    if target.suffix.lower() != ".svg":
        return target.with_suffix(".svg")
    return target


def _export_table_svg(table: Any, path_arg: str) -> int:
    if table is None:
        log("No table available to export", file=sys.stderr)
        return 1

    title_text = str(getattr(table, "title", None) or "table")

    try:
        out_path = _resolve_output_path(path_arg, table_title=title_text)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        from rich.console import Console

        console = Console(record=True)
        renderable = table.to_rich() if hasattr(table, "to_rich") else table
        console.print(renderable)
        console.save_svg(str(out_path))
        log(f"Saved table SVG: {out_path}")
        return 0
    except Exception as exc:
        log(f"Failed to save table SVG: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _apply_table_sort(table: Any, *, sort_column: str, query_text: str) -> int:
    query_fields = _parse_table_query(query_text)
    specs = parse_sort_specs(sort_column)
    if not specs:
        specs = [parse_sort_spec(str(query_fields.get("sort") or "title"))]
    if query_fields.get("namespace") and len(specs) == 1 and not specs[0].get("namespace"):
        specs[0]["namespace"] = str(query_fields.get("namespace") or "").strip()
    if query_fields.get("format") or query_fields.get("order"):
        if len(specs) == 1 and not specs[0].get("order"):
            specs[0]["order"] = str(query_fields.get("format") or query_fields.get("order") or "asc")

    try:
        if hasattr(table, "preserve_order"):
            table.preserve_order = False
        if hasattr(table, "perseverance"):
            table.perseverance = False
        if len(specs) == 1:
            spec = specs[0]
            wanted_column = str(spec.get("column") or "title").strip()
            namespace = str(spec.get("namespace") or "").strip().rstrip(":")
            order = str(spec.get("order") or "asc").strip().lower()
            reverse = bool(_SORT_ORDERS.get(order, False))
            numeric_field = query_fields.get("numeric")
            if numeric_field is not None:
                numeric = _normalize_bool(numeric_field)
            else:
                numeric = namespace.casefold() in _NUMERIC_NAMESPACE_HINTS
            if not wanted_column and namespace:
                wanted_column = "tag"
            if str(wanted_column).strip().lower() == "tag" and namespace:
                if not hasattr(table, "sort_by_tag_namespace"):
                    log("Current table does not support namespace sorting", file=sys.stderr)
                    return 1
                table.sort_by_tag_namespace(namespace, numeric=numeric, reverse=reverse)
            else:
                _sort_by_column(table, wanted_column, numeric=numeric, reverse=reverse)
        else:
            table.rows.sort(key=lambda row: tuple(_spec_sort_key(row, spec, query_fields) for spec in specs))
    except Exception as exc:
        log(f"Failed to sort table: {exc}", file=sys.stderr)
        return 1

    if hasattr(table, "_perseverance"):
        try:
            table._perseverance(True)
        except Exception:
            pass
    return 0


def _run(piped_result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    _ = piped_result, config

    try:
        from SYS import pipeline as ctx
    except Exception as exc:
        log(f"Failed to import pipeline context: {exc}")
        return 1

    parsed = parse_cmdlet_args(args, CMDLET)
    sort_column = str(parsed.get("sort") or "").strip()
    query_text = str(parsed.get("query") or "").strip()
    filter_arg = str(parsed.get("filter") or "").strip()
    debug_mode = bool(parsed.get("debug", False))
    print_mode = bool(parsed.get("print", False))
    path_arg = str(parsed.get("path") or "").strip()

    active_table, _table_kind = _active_table_bundle(ctx)

    if print_mode or path_arg:
        if not path_arg:
            log("Missing required -path for table export", file=sys.stderr)
            return 1
        return _export_table_svg(active_table, path_arg)

    query_fields = _parse_table_query(query_text)
    count_filters = _collect_count_filters(filter_arg, query_fields)

    if not debug_mode and not sort_column and not query_text and not filter_arg:
        return _render_table(active_table)

    if not debug_mode and (sort_column or query_text or filter_arg):
        base_table = active_table
        if base_table is None:
            log("No active result table to filter", file=sys.stderr)
            return 1

        working_table = _clone_table(base_table)
        items = list(ctx.get_last_result_items() or [])
        if count_filters:
            _apply_table_filters(working_table, count_filters, items)
        should_sort = bool(
            sort_column
            or query_fields.get("sort")
            or query_fields.get("namespace")
            or query_fields.get("format")
            or query_fields.get("order")
        )
        if should_sort:
            rc = _apply_table_sort(working_table, sort_column=sort_column, query_text=query_text)
            if rc != 0:
                return rc

        reordered_items = _reorder_items_from_table(working_table, items)
        subject = ctx.get_last_result_subject() if hasattr(ctx, "get_last_result_subject") else None
        ctx.set_last_result_table_overlay(working_table, reordered_items, subject)
        ctx.set_current_stage_table(working_table)
        return _render_table(working_table)

    state = None
    try:
        state = ctx.get_pipeline_state() if hasattr(ctx, "get_pipeline_state") else None
    except Exception:
        state = None

    rows: List[Tuple[str, Any]] = []

    def _summarize_table(name: str, t: Any) -> None:
        if t is None:
            rows.append((name, "None"))
            return
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
                t.get_table_metadata() if hasattr(t, "get_table_metadata") else getattr(t, "table_metadata", None)
            )
        except Exception:
            meta = None
        meta_keys = list(meta.keys()) if isinstance(meta, dict) else []

        rows.append(
            (
                name,
                f"id={id(t)} class={type(t).__name__} title={title!r} table={table_type!r} "
                f"rows={row_count} source={src_cmd!r} source_args={src_args!r} "
                f"no_choice={no_choice} preserve_order={preserve_order} meta_keys={meta_keys}",
            )
        )

    try:
        _summarize_table("display_table", getattr(state, "display_table", None) if state is not None else None)
        _summarize_table("current_stage_table", getattr(state, "current_stage_table", None) if state is not None else None)
        _summarize_table("last_result_table", getattr(state, "last_result_table", None) if state is not None else None)

        display_items = getattr(state, "display_items", None) if state is not None else None
        last_result_items = getattr(state, "last_result_items", None) if state is not None else None
        hist = getattr(state, "result_table_history", None) if state is not None else None
        fwd = getattr(state, "result_table_forward", None) if state is not None else None
        last_sel = getattr(state, "last_selection", None) if state is not None else None

        rows.append(
            (
                "buffers",
                f"display_items={len(display_items or [])} "
                f"last_result_items={len(last_result_items or [])} "
                f"history={len(hist or [])} "
                f"forward={len(fwd or [])} "
                f"last_selection={list(last_sel or [])}",
            )
        )
    except Exception as exc:
        log(f"Failed to summarize table state: {exc}")
        return 1

    title_text = str(args[0]).strip() if args else ""
    status_panel(f"Table State: {title_text}" if title_text else "Table State", rows)

    # If debug logging is enabled, also emit the richer debug dump.
    try:
        if hasattr(ctx, "debug_table_state"):
            ctx.debug_table_state(title_text or ".table")
    except Exception:
        pass

    return 0


CMDLET = Cmdlet(
    name=".table",
    alias=["table"],
    summary="Render, filter, sort, or inspect the active result table.",
    usage='.table [-filter "@Tag<=3"] [-sort @Tag($part,asc)] [-query "format:desc,namespace:track"] [-print -path <path>]',
    arg=[
        CmdletArg(
            name="filter",
            type="string",
            description='Keep rows by column entry count. Name the header with @, e.g. @Tag<=3 or @Title>0.',
            required=False,
        ),
        CmdletArg(
            name="sort",
            type="string",
            description='Sort by one or more keys, comma-separated. Example: -sort @Tag($series),@Tag($episode).',
            required=False,
        ),
        SharedArgs.QUERY,
        QueryArg(
            "filter",
            description="Same as -filter, e.g. -query \"tag<=3\" or filter:tag>5.",
        ),
        QueryArg(
            "sort",
            description="Column to sort, e.g. sort:title.",
        ),
        QueryArg(
            "format",
            aliases=["order"],
            description="asc or desc.",
            choices=["asc", "desc"],
        ),
        QueryArg(
            "namespace",
            description="Tag namespace to sort by when -sort tag.",
        ),
        CmdletArg(
            name="print",
            type="flag",
            description="Export the active table as an SVG using -path.",
            required=False,
        ),
        SharedArgs.PATH,
        CmdletArg(
            name="debug",
            type="flag",
            description="Dump pipeline table state for debugging instead of rendering the table.",
            required=False,
        ),
        CmdletArg(
            name="label",
            type="string",
            description="Optional label to include in the debug dump",
            required=False,
        ),
    ],
    detail=[
        "Name the column with @Header to match the live table header (case-insensitive).",
        "Tag-like columns count the real tag list, not the truncated display text.",
        "Ops: < <= = == != > >=",
        "Sort a tag namespace with @Tag($part,asc). part/track/episode values sort numerically.",
    ],
    examples=[
        '.table -filter "@Tag<=3"',
        '.table -filter "@Tag>5"',
        '.table -query "@Title=0"',
        '.table -sort title -query "format:desc"',
        '.table -sort @Tag($part,asc)',
        '.table -sort @Tag($series),@Tag($episode)',
        '.table -sort tag -query "namespace:part,format:asc"',
    ],
)

CMDLET.exec = _run
