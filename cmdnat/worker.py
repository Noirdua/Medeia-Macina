"""Worker cmdlet: Display workers table in ResultTable format."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Sequence, List

from cmdlet import register
from SYS.cmdlet_spec import Cmdlet, CmdletArg
from SYS.cli_syntax import parse_query
from SYS import pipeline as ctx
from SYS.logger import log
from SYS.database import db as _db, get_worker_stdout
from cmdnat._parsing import extract_arg_value, has_flag
from PluginCore.base import parse_inline_query_arguments
from SYS.cmdlet_spec import QueryArg

DEFAULT_LIMIT = 100
WORKER_STATUS_FILTERS = {"running", "completed", "error", "cancelled"}
HELP_FLAGS = {"-?", "/?", "--help", "-h", "help", "--cmdlet"}
QUERY_ARG_CHOICES = {
    "limit": ["10", "30", "50", "100", "200"],
    "status": sorted(WORKER_STATUS_FILTERS),
}

CMDLET = Cmdlet(
    name=".worker",
    alias=["worker", "workers"],
    summary="Show background jobs (search, download, add).",
    usage='.worker [running|completed|error|cancelled] [-query "limit:30"] [@N]',
    arg=[
        CmdletArg(
            "status",
            description="Filter by status: running, completed, error, cancelled (default: all)",
            requires_db=True,
            choices=sorted(WORKER_STATUS_FILTERS),
        ),
        QueryArg(
            "limit",
            type="integer",
            description="Max workers to show (default: 100)",
            choices=QUERY_ARG_CHOICES["limit"],
        ),
        QueryArg(
            "status",
            key="status",
            description="Filter by worker status via -query",
            choices=QUERY_ARG_CHOICES["status"],
        ),
        CmdletArg(
            name="query",
            description='Inline query fields, e.g. -query "limit:30" or -query "status:running,limit:50"',
            requires_db=True,
        ),
        CmdletArg(
            "@N",
            description="Select worker by index (1-based) and display full logs",
            requires_db=True,
        ),
        CmdletArg(
            "-id",
            description="Show full logs for a specific worker",
            requires_db=True
        ),
        CmdletArg(
            "-clear",
            type="flag",
            description="Remove completed workers from the database",
            requires_db=True,
        ),
    ],
    detail=[
        "- One command: .worker (aliases: worker, workers).",
        "- Filter with a positional status or -query status:<name>.",
        '- Limit rows with -query "limit:N" (default 100).',
        "- Use @N to open full logs for that row.",
        "- .worker -clear removes finished jobs from the database.",
    ],
    examples=[
        ".worker",
        ".worker running",
        '.worker -query "limit:30"',
        ".worker @3",
        ".worker -clear",
    ],
)


def _normalize_worker_row(row: Dict[str, Any]) -> Dict[str, Any]:
    worker_id = row.get("id")
    created = row.get("created_at") or ""
    updated = row.get("updated_at") or ""
    payload = dict(row)
    payload["worker_id"] = worker_id
    payload["started_at"] = created
    payload["last_updated"] = updated
    payload["completed_at"] = updated
    payload["pipe"] = row.get("details") or row.get("title") or ""
    return payload


class _WorkerDB:
    def clear_finished_workers(self) -> int:
        try:
            cur = _db.execute("DELETE FROM workers WHERE status != 'running'")
            return int(getattr(cur, "rowcount", 0) or 0)
        except Exception:
            return 0

    def get_worker(self, worker_id: str) -> Dict[str, Any] | None:
        row = _db.fetchone("SELECT * FROM workers WHERE id = ?", (worker_id,))
        if not row:
            return None
        worker = _normalize_worker_row(dict(row))
        try:
            worker["stdout"] = get_worker_stdout(worker_id)
        except Exception:
            worker["stdout"] = ""
        return worker

    def get_worker_events(self, worker_id: str) -> List[Dict[str, Any]]:
        try:
            rows = _db.fetchall(
                "SELECT content, channel, timestamp FROM worker_stdout WHERE worker_id = ? ORDER BY timestamp ASC",
                (worker_id,),
            )
        except Exception:
            rows = []
        events: List[Dict[str, Any]] = []
        for row in rows:
            try:
                events.append({
                    "message": row.get("content"),
                    "channel": row.get("channel") or "stdout",
                    "created_at": row.get("timestamp"),
                })
            except Exception:
                continue
        return events

    def get_all_workers(self, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            rows = _db.fetchall(
                "SELECT * FROM workers ORDER BY created_at DESC LIMIT ?",
                (int(limit or 100),),
            )
        except Exception:
            rows = []
        return [_normalize_worker_row(dict(row)) for row in rows]


def _has_help_flag(args_list: Sequence[str]) -> bool:
    return any(str(arg).lower() in HELP_FLAGS for arg in args_list)


@dataclass
class WorkerCommandOptions:
    status: str | None = None
    limit: int = DEFAULT_LIMIT
    worker_id: str | None = None
    clear: bool = False


@register([".worker", "worker", "workers"])
def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Display workers table or show detailed logs for a specific worker."""
    args_list = [str(arg) for arg in (args or [])]
    selection_indices = ctx.get_last_selection()
    selection_requested = bool(selection_indices) and isinstance(result,
                                                                 list
                                                                 ) and len(result) > 0

    if _has_help_flag(args_list):
        ctx.emit(CMDLET.__dict__)
        return 0

    options = _parse_worker_args(args_list)

    try:
        db = _WorkerDB()

        if options.clear:
            count = db.clear_finished_workers()
            log(f"Cleared {count} finished workers.")
            return 0

        if options.worker_id:
            worker = db.get_worker(options.worker_id)
            if worker:
                events: List[Dict[str, Any]] = []
                try:
                    wid = worker.get("worker_id")
                    if wid:
                        events = db.get_worker_events(wid)
                except Exception:
                    pass
                _emit_worker_detail(worker, events)
                return 0
            log(f"Worker not found: {options.worker_id}", file=sys.stderr)
            return 1

        if selection_requested:
            return _render_worker_selection(db, result)

        return _render_worker_list(db, options.status, options.limit)
    except Exception as exc:
        log(f"Workers query failed: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        return 1


def _parse_worker_args(args_list: Sequence[str]) -> WorkerCommandOptions:
    options = WorkerCommandOptions()
    query_raw = extract_arg_value(args_list, flags={"-query", "--query"}, allow_positional=False)
    if query_raw:
        _apply_worker_query(options, query_raw)

    if has_flag(args_list, "-clear") or has_flag(args_list, "--clear"):
        options.clear = True

    i = 0
    while i < len(args_list):
        arg = args_list[i]
        low = arg.lower()
        if low in {"-query", "--query"}:
            i += 2 if i + 1 < len(args_list) else 1
            continue
        if low.startswith("-query=") or low.startswith("--query="):
            i += 1
            continue
        if low in {"-id", "--id"} and i + 1 < len(args_list):
            options.worker_id = args_list[i + 1]
            i += 2
            continue
        if low in {"-clear", "--clear"}:
            i += 1
            continue
        if low in {"-status", "--status"} and i + 1 < len(args_list):
            # Prefer -query status:, but keep -status as a thin alias.
            options.status = _normalize_status(args_list[i + 1]) or options.status
            i += 2
            continue
        if low in WORKER_STATUS_FILTERS:
            options.status = low
            i += 1
            continue
        if not arg.startswith("-"):
            # Positional free text may be a status filter.
            status = _normalize_status(arg)
            if status:
                options.status = status
            i += 1
            continue
        i += 1
    return options


def _apply_worker_query(options: WorkerCommandOptions, query_raw: str) -> None:
    """Apply plugin-style -query fields (limit:, status:)."""
    text = str(query_raw or "").strip()
    if not text:
        return

    # Prefer plugin inline parsing so comma-separated fields work:
    #   status:running,limit:50
    leftover, fields = parse_inline_query_arguments(text)

    # Also accept quoted parse_query forms (status:"running" limit:"50").
    parsed = parse_query(text)
    for key, value in dict(parsed.get("fields") or {}).items():
        value_text = str(value or "").strip()
        # Ignore greedy parse_query matches that swallowed comma-separated pairs.
        if not value_text or "," in value_text:
            continue
        fields.setdefault(str(key).strip().lower(), value_text)

    free_text = " ".join(
        part for part in (leftover, str(parsed.get("text") or "").strip()) if part
    ).strip()
    status_from_text = _normalize_status(free_text)
    if status_from_text and not options.status:
        options.status = status_from_text

    limit_value = fields.get("limit")
    if limit_value is not None:
        options.limit = _normalize_limit(limit_value)

    status_value = fields.get("status")
    if status_value is not None:
        normalized = _normalize_status(status_value)
        if normalized:
            options.status = normalized


def _normalize_status(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in WORKER_STATUS_FILTERS:
        return text
    return None


def _normalize_limit(value: Any) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def _render_worker_list(db, status_filter: str | None, limit: int) -> int:
    workers = db.get_all_workers(limit=limit)
    if status_filter:
        workers = [
            w for w in workers if str(w.get("status", "")).lower() == status_filter
        ]

    if not workers:
        log("No workers found", file=sys.stderr)
        return 0

    for worker in workers:
        started = worker.get("started_at", "")
        ended = worker.get("completed_at", worker.get("last_updated", ""))

        date_str = _extract_date(started)
        start_time = _format_event_timestamp(started)
        end_time = _format_event_timestamp(ended)
        worker_id_value = worker.get("worker_id") or worker.get("id")
        worker_id = str(worker_id_value) if worker_id_value is not None else ""
        status = str(worker.get("status") or "unknown")
        result_state = str(worker.get("result") or "")
        status_label = status
        if result_state and result_state.lower() not in {"", status.lower()}:
            status_label = f"{status_label} ({result_state})"
        pipe_display = _summarize_pipe(worker.get("pipe"))
        error_message = _normalize_text(worker.get("error_message"))
        description = _normalize_text(worker.get("description"))

        columns = [
            ("ID", worker_id[:8]),
            ("Status", status_label),
            ("Pipe", pipe_display),
            ("Date", date_str),
            ("Start", start_time),
            ("End", end_time),
        ]
        if error_message:
            brief = error_message.splitlines()[0][:120]
            loc = ""
            for line in error_message.splitlines():
                stripped = line.strip()
                if stripped.startswith("File ") and ", line " in stripped:
                    loc = stripped.replace("File ", "").replace('"', "")
            columns.append(("Error", brief))
            if loc:
                columns.append(("At", loc[-80:]))
        if description and description != error_message:
            columns.append(("Details", description[:200]))

        selection_args = None
        if worker_id:
            selection_args = ["-id", worker_id]
        item = {
            "columns": columns,
            "__worker_metadata": worker,
            "worker_id": worker_id,
        }
        if selection_args:
            item["_selection_args"] = list(selection_args)
            item["selection_args"] = list(selection_args)
        ctx.emit(item)
    return 0


def _render_worker_selection(db, selected_items: Any) -> int:
    if not isinstance(selected_items, list):
        log("Selection payload missing", file=sys.stderr)
        return 1

    emitted = False
    for item in selected_items:
        worker = _resolve_worker_record(db, item)
        if not worker:
            continue
        events: List[Dict[str, Any]] = []
        try:
            events = (
                db.get_worker_events(worker.get("worker_id"))
                if hasattr(db,
                           "get_worker_events") else []
            )
        except Exception:
            events = []
        _emit_worker_detail(worker, events)
        emitted = True
    if not emitted:
        log("Selected rows no longer exist", file=sys.stderr)
        return 1
    return 0


def _resolve_worker_record(db, payload: Any) -> Dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    worker_data = payload.get("__worker_metadata")
    worker_id = None
    if isinstance(worker_data, dict):
        worker_id = worker_data.get("worker_id")
    else:
        worker_id = payload.get("worker_id")
        worker_data = None
    if worker_id:
        fresh = db.get_worker(worker_id)
        if fresh:
            return fresh
    return worker_data if isinstance(worker_data, dict) else None


def _emit_worker_detail(worker: Dict[str, Any], events: List[Dict[str, Any]]) -> None:
    rows_emitted = False

    def _emit_columns(columns: List[tuple[str, str]]) -> None:
        nonlocal rows_emitted
        payload = {
            "columns": columns,
            "_skip_metadata_propagation": True,
        }
        ctx.emit(payload)
        rows_emitted = True

    if events:
        for event in events:
            message = _normalize_text(event.get("message"))
            if not message:
                continue

            level = _normalize_text(event.get("event_type") or event.get("channel") or "INFO")
            step = _normalize_text(event.get("step"))
            if step:
                message = f"[{step}] {message}"

            timestamp = _format_event_timestamp(event.get("created_at") or "")

            _emit_columns([
                ("Time", timestamp),
                ("Level", level or "INFO"),
                ("Message", message),
            ])

    if not rows_emitted:
        stdout_content = worker.get("stdout", "") or ""
        lines = stdout_content.splitlines()

        for line in lines:
            line = line.strip()
            if not line:
                continue

            timestamp = ""
            level = "INFO"
            message = line

            try:
                parts = line.split(" - ", 3)
                if len(parts) >= 4:
                    ts_str, _, lvl, msg = parts
                    timestamp = _format_event_timestamp(ts_str)
                    level = lvl
                    message = msg
                elif len(parts) == 3:
                    ts_str, lvl, msg = parts
                    timestamp = _format_event_timestamp(ts_str)
                    level = lvl
                    message = msg
            except Exception:
                pass

            _emit_columns([
                ("Time", timestamp),
                ("Level", level),
                ("Message", message),
            ])

    if not rows_emitted:
        fallback = (
            _normalize_text(worker.get("error_message"))
            or _normalize_text(worker.get("description"))
            or "No log output captured for this worker."
        )
        _emit_columns([
            ("Time", ""),
            ("Level", "INFO"),
            ("Message", fallback),
        ])


def _summarize_pipe(pipe_value: Any, limit: int = 200) -> str:
    text = _normalize_text(pipe_value)
    if not text:
        return "(none)"

    stage_count = text.count("|") + 1 if text else 0
    display = text
    if len(display) > limit:
        trimmed = display[:max(limit - 3, 0)].rstrip()
        if not trimmed:
            trimmed = display[:limit]
        display = f"{trimmed}..."
    if stage_count > 1:
        suffix = f" ({stage_count} stages)"
        if not display.endswith("..."):
            display = f"{display}{suffix}"
        else:
            display = f"{display}{suffix}"
    return display


def _format_event_timestamp(raw_timestamp: Any) -> str:
    dt = _parse_to_local(raw_timestamp)
    if dt:
        return dt.strftime("%H:%M:%S")

    if not raw_timestamp:
        return "--:--:--"
    text = str(raw_timestamp)
    if "T" in text:
        time_part = text.split("T", 1)[1]
    elif " " in text:
        time_part = text.split(" ", 1)[1]
    else:
        time_part = text
    return time_part[:8] if len(time_part) >= 8 else time_part


def _parse_to_local(timestamp_str: Any) -> datetime | None:
    if not timestamp_str:
        return None
    text = str(timestamp_str).strip()
    if not text:
        return None

    try:
        if "T" in text:
            return datetime.fromisoformat(text)
        if " " in text:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone()
    except Exception:
        pass
    return None


def _extract_date(raw_timestamp: Any) -> str:
    dt = _parse_to_local(raw_timestamp)
    if dt:
        return dt.strftime("%m-%d-%y")

    if not raw_timestamp:
        return ""
    text = str(raw_timestamp)
    date_part = ""
    if "T" in text:
        date_part = text.split("T", 1)[0]
    elif " " in text:
        date_part = text.split(" ", 1)[0]
    else:
        date_part = text

    try:
        parts = date_part.split("-")
        if len(parts) == 3:
            year, month, day = parts
            return f"{month}-{day}-{year[2:]}"
    except Exception:
        pass
    return date_part


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # collapse whitespace to keep table columns aligned
    normalized = re.sub(r"\s+", " ", text)
    return normalized


def _truncate_text(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    cutoff = max(limit - 3, 0)
    trimmed = value[:cutoff].rstrip()
    if not trimmed:
        return value[:limit]
    return f"{trimmed}..."
