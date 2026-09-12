from __future__ import annotations
"""Medeia-Macina CLI.

This module intentionally uses a class-based architecture:
- no legacy procedural entrypoints
- no compatibility shims
- all REPL/pipeline/cmdlet execution state lives on objects
"""

# When running the CLI directly (not via the 'mm' launcher), honor the
# repository config `debug` flag by enabling `MM_DEBUG` so import-time
# diagnostics and bootstrap debug output are visible without setting the
# environment variable manually.
import os
from pathlib import Path
if not os.environ.get("MM_DEBUG"):
    try:
        # Check database first
        db_path = Path(__file__).resolve().parent / "medios.db"
        if db_path.exists():
            import sqlite3
            with sqlite3.connect(str(db_path), timeout=30.0) as conn:
                cur = conn.cursor()
                # Check for global debug key
                cur.execute("SELECT value FROM config WHERE key = 'debug' AND category = 'global'")
                row = cur.fetchone()
                if row:
                    val = str(row[0]).strip().lower()
                    if val in ("1", "true", "yes", "on"):
                        os.environ["MM_DEBUG"] = "1"
    except Exception:
        import logging as _logging
        _logger = _logging.getLogger("CLI")
        _logger.debug("Failed to check database for debug flag on startup", exc_info=True)

import json
import re
import shlex
import sys
import threading
import time
import uuid
from copy import deepcopy

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, cast

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.markdown import Markdown
from rich.bar import Bar
from rich.table import Table as RichTable
from SYS.rich_display import (
    stderr_console,
    stdout_console,
)
from cmdnat._status_shared import (
    add_startup_check as _shared_add_startup_check,
    collect_plugin_startup_checks as _collect_plugin_startup_checks,
    has_plugin as _has_plugin,
)


def _install_rich_traceback(*, show_locals: bool = False) -> None:
    """Install Rich traceback handler as the default excepthook.

    This keeps uncaught exceptions readable in the terminal.
    """
    try:
        from rich.traceback import install as rich_traceback_install

        rich_traceback_install(show_locals=bool(show_locals))
    except Exception:
        # Fall back to the standard Python traceback if Rich isn't available.
        return


# Default to Rich tracebacks for the whole process.
_install_rich_traceback(show_locals=False)

from SYS.logger import debug, set_debug
from SYS.repl_queue import clear_repl_state, pop_repl_commands, touch_repl_state
from SYS.worker_manager import WorkerManager

from SYS.cmdlet_catalog import (
    get_cmdlet_arg_choices,
    get_cmdlet_arg_flags,
    get_cmdlet_arg_flags_from_object,
    get_cmdlet_metadata,
    import_cmd_module,
    list_cmdlet_metadata,
    list_cmdlet_names,
)
from SYS.config import load_config
from SYS.result_table import Table

from SYS.worker import WorkerManagerRegistry, WorkerStages, WorkerOutputMirror, WorkerStageSession
from SYS.pipeline import PipelineExecutor
from PluginCore.registry import plugin_inline_query_choices, plugin_query_field_map



# Selection parsing and REPL lexer moved to SYS.cli_parsing
from SYS.cli_parsing import SelectionSyntax, SelectionFilterSyntax, MedeiaLexer


# SelectionFilterSyntax moved to SYS.cli_parsing (imported above)












def _send_mpv_ipc_command(
    command: List[Any],
    *,
    ipc_path: Optional[str] = None,
    timeout: float = 0.75,
    wait_for_response: bool = True,
) -> bool:
    if not isinstance(command, list) or not command:
        return False

    try:
        from PluginCore.registry import import_plugin_module

        mpv_ipc = import_plugin_module("mpv.mpv_ipc")
        if mpv_ipc is None:
            return False
        MPVIPCClient = mpv_ipc.MPVIPCClient
        get_ipc_pipe_path = mpv_ipc.get_ipc_pipe_path

        client = MPVIPCClient(
            socket_path=str(ipc_path or get_ipc_pipe_path()),
            timeout=max(0.1, float(timeout or 0.75)),
            silent=True,
        )
        try:
            response = client.send_command({
                "command": command,
            }, wait=bool(wait_for_response))
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

        if not wait_for_response:
            return bool(response and (response.get("async") or response.get("request_id") is not None))

        return bool(response and response.get("error") == "success")
    except Exception as exc:
        debug(f"mpv ipc command failed: {exc}")
        return False


def _notify_mpv_osd(text: str, *, duration_ms: int = 3500, ipc_path: Optional[str] = None) -> bool:
    message = str(text or "").strip()
    if not message:
        return False

    return _send_mpv_ipc_command(
        [
            "show-text",
            message,
            max(0, int(duration_ms)),
        ],
        ipc_path=ipc_path,
        wait_for_response=False,
    )


def _send_mpv_callback_event(metadata: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    callback = metadata.get("mpv_callback") if isinstance(metadata, dict) else None
    if not isinstance(callback, dict):
        return False

    script_name = str(callback.get("script") or "").strip()
    message_name = str(callback.get("message") or "").strip()
    ipc_path = str(callback.get("ipc_path") or "").strip() or None
    if not script_name or not message_name:
        return False

    event_payload = {
        "kind": str(metadata.get("kind") or "").strip(),
    }
    if isinstance(payload, dict):
        event_payload.update(payload)

    return _send_mpv_ipc_command(
        [
            "script-message-to",
            script_name,
            message_name,
            json.dumps(event_payload, ensure_ascii=False),
        ],
        ipc_path=ipc_path,
        wait_for_response=False,
    )


def _notify_mpv_callback(metadata: Dict[str, Any], execution_result: Dict[str, Any]) -> bool:
    return _send_mpv_callback_event(
        metadata,
        {
            "phase": "completed",
            "success": bool(execution_result.get("success")),
            "status": str(execution_result.get("status") or "completed"),
            "error": str(execution_result.get("error") or "").strip(),
            "command_text": str(execution_result.get("command_text") or "").strip(),
        },
    )


def _build_mpv_progress_callback(metadata: Dict[str, Any]) -> Optional[Any]:
    callback = metadata.get("mpv_callback") if isinstance(metadata, dict) else None
    if not isinstance(callback, dict):
        return None

    last_sent_at: Dict[str, float] = {}
    last_percent: Dict[str, int] = {}
    last_text: Dict[str, str] = {}

    def emit(payload: Dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False

        event_name = str(payload.get("event") or "").strip().lower()
        now = time.monotonic()
        throttle_key = event_name or "progress"

        if event_name == "pipe-percent":
            pipe_index = int(payload.get("pipe_index") or 0)
            percent = max(0, min(100, int(payload.get("percent") or 0)))
            throttle_key = f"pipe-percent:{pipe_index}"
            prev = last_percent.get(throttle_key)
            if prev == percent:
                return False
            if prev is not None and percent < 100 and (percent - prev) < 5 and (now - last_sent_at.get(throttle_key, 0.0)) < 0.35:
                return False
            last_percent[throttle_key] = percent
            payload = dict(payload)
            payload["percent"] = percent
        elif event_name == "transfer":
            label = str(payload.get("label") or "transfer").strip() or "transfer"
            throttle_key = f"transfer:{label}"
            completed = payload.get("completed")
            total = payload.get("total")
            percent = None
            try:
                if total is not None and int(total) > 0 and completed is not None:
                    percent = max(0, min(100, int(round((int(completed) / max(1, int(total))) * 100.0))))
            except Exception:
                percent = None
            if percent is not None:
                prev = last_percent.get(throttle_key)
                if prev == percent:
                    return False
                if prev is not None and percent < 100 and (percent - prev) < 3 and (now - last_sent_at.get(throttle_key, 0.0)) < 0.35:
                    return False
                last_percent[throttle_key] = percent
                payload = dict(payload)
                payload["percent"] = percent
        elif event_name == "status":
            pipe_index = int(payload.get("pipe_index") or 0)
            throttle_key = f"status:{pipe_index}"
            text = str(payload.get("text") or "").strip()
            if last_text.get(throttle_key) == text and (now - last_sent_at.get(throttle_key, 0.0)) < 0.5:
                return False
            last_text[throttle_key] = text

        last_sent_at[throttle_key] = now
        event_payload = dict(payload)
        event_payload.setdefault("phase", "progress")
        return _send_mpv_callback_event(metadata, event_payload)

    return emit


def _notify_mpv_completion(metadata: Dict[str, Any], execution_result: Dict[str, Any]) -> bool:
    callback_sent = _notify_mpv_callback(metadata, execution_result)

    notify = metadata.get("mpv_notify") if isinstance(metadata, dict) else None
    if not isinstance(notify, dict):
        return callback_sent

    success = bool(execution_result.get("success"))
    error_text = str(execution_result.get("error") or "").strip()
    if success:
        message = str(notify.get("success_text") or "").strip()
    else:
        failure_prefix = str(notify.get("failure_text") or "").strip()
        message = failure_prefix
        if error_text:
            if message:
                message = f"{message}: {error_text}"
            else:
                message = error_text

    if not message:
        return callback_sent

    try:
        duration_ms = int(notify.get("duration_ms") or 3500)
    except Exception:
        duration_ms = 3500

    ipc_path = str(notify.get("ipc_path") or "").strip() or None
    notified = _notify_mpv_osd(message, duration_ms=duration_ms, ipc_path=ipc_path)
    return bool(callback_sent or notified)


from SYS.cli_completer import CmdletCompleter, CmdletIntrospection

class ConfigLoader:

    def __init__(self, *, root: Path) -> None:
        self._root = root

    def load_shared(self) -> Dict[str, Any]:
        try:
            return load_config(emit_summary=False)
        except Exception:
            return {}

    def load(self) -> Dict[str, Any]:
        try:
            return deepcopy(self.load_shared())
        except Exception:
            return {}


class CmdletHelp:

    @staticmethod
    def show_cmdlet_list() -> None:
        try:
            metadata = list_cmdlet_metadata() or {}
            from rich.box import SIMPLE
            from rich.panel import Panel
            from rich.table import Table as RichTable

            table = RichTable(
                show_header=True,
                header_style="bold",
                box=SIMPLE,
                expand=True
            )
            table.add_column("Cmdlet", no_wrap=True)
            table.add_column("Aliases")
            table.add_column("Args")
            table.add_column("Summary")

            for cmd_name in sorted(metadata.keys()):
                info = metadata[cmd_name]
                aliases = info.get("aliases", [])
                args = info.get("args", [])
                summary = info.get("summary") or ""
                alias_str = ", ".join(
                    [str(a) for a in (aliases or []) if str(a).strip()]
                )
                arg_names = [
                    a.get("name") for a in (args or [])
                    if isinstance(a, dict) and a.get("name")
                ]
                args_str = ", ".join([str(a) for a in arg_names if str(a).strip()])
                table.add_row(str(cmd_name), alias_str, args_str, str(summary))

            stdout_console().print(Panel(table, title="Cmdlets", expand=False))
        except Exception as exc:
            from rich.panel import Panel
            from rich.text import Text

            stderr_console().print(
                Panel(Text(f"Error: {exc}"),
                      title="Error",
                      expand=False)
            )

    @staticmethod
    def show_cmdlet_help(cmd_name: str) -> None:
        try:
            meta = get_cmdlet_metadata(cmd_name)
            if meta:
                CmdletHelp._print_metadata(cmd_name, meta)
                return
            print(f"Unknown command: {cmd_name}\n")
        except Exception as exc:
            print(f"Error: {exc}\n")

    @staticmethod
    def _print_metadata(cmd_name: str, data: Any) -> None:
        d = data.to_dict() if hasattr(data, "to_dict") else data
        if not isinstance(d, dict):
            from rich.panel import Panel
            from rich.text import Text

            stderr_console().print(
                Panel(
                    Text(f"Invalid metadata for {cmd_name}"),
                    title="Error",
                    expand=False
                )
            )
            return

        name = d.get("name", cmd_name)
        summary = d.get("summary", "")
        usage = d.get("usage", "")
        description = d.get("description", "")
        args = d.get("args", [])
        details = d.get("details", [])

        from rich.box import SIMPLE
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table as RichTable
        from rich.text import Text

        header = Text.assemble((str(name), "bold"))
        synopsis = Text(str(usage or name))
        stdout_console().print(
            Panel(Group(header,
                        synopsis),
                  title="Help",
                  expand=False)
        )

        if summary or description:
            desc_bits: List[Text] = []
            if summary:
                desc_bits.append(Text(str(summary)))
            if description:
                desc_bits.append(Text(str(description)))
            stdout_console().print(
                Panel(Group(*desc_bits),
                      title="Description",
                      expand=False)
            )

        if args and isinstance(args, list):
            param_table = RichTable(
                show_header=True,
                header_style="bold",
                box=SIMPLE,
                expand=True
            )
            param_table.add_column("Arg", no_wrap=True)
            param_table.add_column("Type", no_wrap=True)
            param_table.add_column("Required", no_wrap=True)
            param_table.add_column("Description")
            for arg in args:
                if isinstance(arg, dict):
                    name_str = arg.get("name", "?")
                    typ = arg.get("type", "string")
                    required = bool(arg.get("required", False))
                    desc = arg.get("description", "")
                else:
                    name_str = getattr(arg, "name", "?")
                    typ = getattr(arg, "type", "string")
                    required = bool(getattr(arg, "required", False))
                    desc = getattr(arg, "description", "")

                param_table.add_row(
                    f"-{name_str}",
                    str(typ),
                    "yes" if required else "no",
                    str(desc or "")
                )

            stdout_console().print(Panel(param_table, title="Parameters", expand=False))

        if details:
            stdout_console().print(
                Panel(
                    Group(*[Text(str(x)) for x in details]),
                    title="Remarks",
                    expand=False
                )
            )


class CmdletExecutor:

    def __init__(self, *, config_loader: ConfigLoader) -> None:
        self._config_loader = config_loader

    @staticmethod
    def _get_table_title_for_command(
        cmd_name: str,
        emitted_items: Optional[List[Any]] = None,
        cmd_args: Optional[List[str]] = None,
    ) -> str:
        normalized_cmd = str(cmd_name or "").replace("_", "-").lower().strip()
        mapped_cmd = CmdletCompleter._effective_cmd_name(normalized_cmd, cmd_args or [])

        title_map = {
            "search-file": "Results",
            "search_file": "Results",
            "download-data": "Downloads",
            "download_data": "Downloads",
            "download-file": "Downloads",
            "download_file": "Downloads",
            "metadata": "Tags",
            "add-url": "Results",
            "add_url": "Results",
            "get-url": "url",
            "get_url": "url",
            "delete-url": "Results",
            "delete_url": "Results",
            "get-note": "Notes",
            "get_note": "Notes",
            "add-note": "Results",
            "add_note": "Results",
            "delete-note": "Results",
            "delete_note": "Results",
            "get-relationship": "Relationships",
            "get_relationship": "Relationships",
            "add-relationship": "Results",
            "add_relationship": "Results",
            "add-file": "Results",
            "add_file": "Results",
            "delete-file": "Results",
            "delete_file": "Results",
            "get-metadata": None,
            "get_metadata": None,
        }
        mapped = title_map.get(mapped_cmd or normalized_cmd, "Results")
        if mapped is not None:
            return mapped

        if emitted_items:
            first = emitted_items[0]
            try:
                if isinstance(first, dict) and first.get("title"):
                    return str(first.get("title"))
                if hasattr(first, "title") and getattr(first, "title"):
                    return str(getattr(first, "title"))
            except Exception:
                pass
        return "Results"

    def execute(self, cmd_name: str, args: List[str]) -> None:
        from SYS import pipeline as ctx
        from cmdlet import REGISTRY

        # REPL guard: stage-local selection tables should not leak across independent
        # commands. @ selection can always re-seed from the last result table.
        try:
            if hasattr(ctx, "set_current_stage_table"):
                ctx.set_current_stage_table(None)
        except Exception:
            pass

        cmd_fn = REGISTRY.get(cmd_name)
        try:
            mod = import_cmd_module(cmd_name, reload_loaded=True)
            data = getattr(mod, "CMDLET", None) if mod else None
            if data and hasattr(data, "exec") and callable(getattr(data, "exec")):
                from SYS.cmdlet_spec import collect_registered_cmdlet_names

                run_fn = getattr(data, "exec")
                for registered_name in collect_registered_cmdlet_names(data, fallback_name=cmd_name):
                    REGISTRY[registered_name] = run_fn
                cmd_fn = run_fn
        except Exception:
            pass

        if not cmd_fn:
            # Lazy-import module and register its CMDLET.
            try:
                mod = import_cmd_module(cmd_name)
                data = getattr(mod, "CMDLET", None) if mod else None
                if data and hasattr(data, "exec") and callable(getattr(data, "exec")):
                    run_fn = getattr(data, "exec")
                    REGISTRY[cmd_name] = run_fn
                    cmd_fn = run_fn
            except Exception:
                cmd_fn = None

        if not cmd_fn:
            print(f"Unknown command: {cmd_name}\n")
            try:
                ctx.set_last_execution_result(
                    status="failed",
                    error=f"Unknown command: {cmd_name}",
                    command_text=" ".join([cmd_name, *args]).strip() or cmd_name,
                )
            except Exception:
                pass
            return

        config = self._config_loader.load()

        # ------------------------------------------------------------------
        # Single-command Live pipeline progress (match REPL behavior)
        # ------------------------------------------------------------------
        progress_ui = None
        pipe_idx: Optional[int] = None

        def _maybe_start_single_live_progress(
            *,
            cmd_name_norm: str,
            filtered_args: List[str],
            piped_input: Any,
            config: Any,
        ) -> None:
            nonlocal progress_ui, pipe_idx

            effective_cmd = CmdletCompleter._effective_cmd_name(cmd_name_norm, filtered_args)

            # Keep behavior consistent with pipeline runner exclusions.
            # Some commands render their own Rich UI (tables/panels) and don't
            # play nicely with Live cursor control.
            if effective_cmd in {
                    "get-relationship",
                    "get-rel",
                    ".pipe",
                    ".mpv",
                    ".matrix",
                    ".telegram",
                    "telegram",
                    "delete-file",
                    "del-file",
                    ".help",
                    "help",
                    "?",
                    ".config",
                    ".status",
                    ".table",
                    ".worker",
                    ".adjective",
            }:
                return

            # add-file directory selector mode: show only the selection table, no Live progress.
            if effective_cmd in {"add-file", "add_file"}:
                try:
                    from pathlib import Path as _Path

                    toks = list(filtered_args or [])
                    i = 0
                    while i < len(toks):
                        t = str(toks[i])
                        low = t.lower().strip()
                        if low in {"-path",
                                   "--path",
                                   "-p"} and i + 1 < len(toks):
                            nxt = str(toks[i + 1])
                            if nxt and ("," not in nxt):
                                p = _Path(nxt)
                                if p.exists() and p.is_dir():
                                    return
                            i += 2
                            continue
                        i += 1
                except Exception:
                    pass

            try:
                quiet_mode = (
                    bool(config.get("_quiet_background_output"))
                    if isinstance(config,
                                  dict) else False
                )
            except Exception:
                quiet_mode = False
            if quiet_mode:
                return

            try:
                import sys as _sys

                if not bool(getattr(_sys.stderr, "isatty", lambda: False)()):
                    return
            except Exception:
                return

            try:
                from SYS.models import PipelineLiveProgress

                progress_ui = PipelineLiveProgress([cmd_name_norm], enabled=True)
                progress_ui.start()
                try:
                    if hasattr(ctx, "set_live_progress"):
                        ctx.set_live_progress(progress_ui)
                except Exception:
                    pass
                try:
                    progress_cb = (
                        ctx.get_progress_event_callback()
                        if hasattr(ctx, "get_progress_event_callback") else None
                    )
                    if callable(progress_cb) and hasattr(progress_ui, "set_event_callback"):
                        progress_ui.set_event_callback(progress_cb)
                except Exception:
                    pass

                pipe_idx = 0

                # Estimate per-item task count for the single pipe.
                total_items = 1
                preview_items: Optional[List[Any]] = None
                try:
                    if isinstance(piped_input, list):
                        total_items = max(1, int(len(piped_input)))
                        preview_items = list(piped_input)
                    elif piped_input is not None:
                        total_items = 1
                        preview_items = [piped_input]
                    else:
                        preview: List[Any] = []
                        toks = list(filtered_args or [])
                        i = 0
                        while i < len(toks):
                            t = str(toks[i])
                            low = t.lower().strip()
                            if (effective_cmd in {"add-file", "add_file"} and low in {"-path",
                                                                          "--path",
                                                                          "-p"}
                                    and i + 1 < len(toks)):
                                nxt = str(toks[i + 1])
                                if nxt:
                                    if "," in nxt:
                                        parts = [
                                            p.strip().strip("\"'")
                                            for p in nxt.split(",")
                                        ]
                                        parts = [p for p in parts if p]
                                        if parts:
                                            preview.extend(parts)
                                            i += 2
                                            continue
                                    else:
                                        preview.append(nxt)
                                        i += 2
                                        continue
                            if low in {"-url",
                                       "--url"} and i + 1 < len(toks):
                                nxt = str(toks[i + 1])
                                if nxt and not nxt.startswith("-"):
                                    preview.append(nxt)
                                i += 2
                                continue
                            if (not t.startswith("-")) and ("://" in low
                                                            or low.startswith(
                                                                ("magnet:",
                                                                 "torrent:"))):
                                preview.append(t)
                            i += 1
                        preview_items = preview if preview else None
                        total_items = max(1, int(len(preview)) if preview else 1)
                except Exception:
                    total_items = 1
                    preview_items = None

                try:
                    progress_ui.begin_pipe(
                        0,
                        total_items=int(total_items),
                        items_preview=preview_items
                    )
                except Exception:
                    pass
            except Exception:
                progress_ui = None
                pipe_idx = None

        filtered_args: List[str] = []
        selected_indices: List[int] = []
        select_all = False
        selection_filters: List[List[Tuple[str, str]]] = []

        value_flags: Set[str] = set()
        try:
            meta = get_cmdlet_metadata(cmd_name)
            raw = meta.get("raw") if isinstance(meta, dict) else None
            arg_specs = getattr(raw, "arg", None) if raw is not None else None
            if isinstance(arg_specs, list):
                for spec in arg_specs:
                    spec_type = str(getattr(spec,
                                            "type",
                                            "string") or "string").strip().lower()
                    if spec_type == "flag":
                        continue
                    spec_name = str(getattr(spec, "name", "") or "")
                    canonical = spec_name.lstrip("-").strip()
                    if not canonical:
                        continue
                    value_flags.add(f"-{canonical}".lower())
                    value_flags.add(f"--{canonical}".lower())
                    alias = str(getattr(spec, "alias", "") or "").strip()
                    if alias:
                        value_flags.add(f"-{alias}".lower())
        except Exception:
            value_flags = set()

        for i, arg in enumerate(args):
            if isinstance(arg, str) and arg.startswith("@"):  # selection candidate
                prev = str(args[i - 1]).lower() if i > 0 else ""
                if prev in value_flags:
                    filtered_args.append(arg)
                    continue

                # Universal selection filter: @"COL:expr" (quotes may be stripped by tokenization)
                filter_spec = SelectionFilterSyntax.parse(arg)
                if filter_spec is not None:
                    selection_filters.append(filter_spec)
                    continue

                if arg.strip() == "@*":
                    select_all = True
                    continue

                selection = SelectionSyntax.parse(arg)
                if selection is not None:
                    zero_based = [idx - 1 for idx in selection]
                    for idx in zero_based:
                        if idx not in selected_indices:
                            selected_indices.append(idx)
                    continue

                filtered_args.append(arg)
                continue

            filtered_args.append(str(arg))

        # IMPORTANT: Do not implicitly feed the previous command's results into
        # a new command unless the user explicitly selected items via @ syntax.
        # Piping should require `|` (or an explicit @ selection).
        piped_items = ctx.get_last_result_items()
        result: Any = None
        effective_selected_indices: List[int] = []
        if piped_items and (select_all or selected_indices or selection_filters):
            candidate_idxs = list(range(len(piped_items)))
            for spec in selection_filters:
                candidate_idxs = [
                    i for i in candidate_idxs
                    if SelectionFilterSyntax.matches(piped_items[i], spec)
                ]

            if select_all:
                effective_selected_indices = list(candidate_idxs)
            elif selected_indices:
                effective_selected_indices = [
                    candidate_idxs[i] for i in selected_indices
                    if 0 <= i < len(candidate_idxs)
                ]
            else:
                effective_selected_indices = list(candidate_idxs)

            result = [piped_items[i] for i in effective_selected_indices]

        worker_manager = WorkerManagerRegistry.ensure(config)
        stage_session = WorkerStages.begin_stage(
            worker_manager,
            cmd_name=cmd_name,
            stage_tokens=[cmd_name,
                          *filtered_args],
            config=config,
            command_text=" ".join([cmd_name,
                                   *filtered_args]).strip() or cmd_name,
        )

        stage_worker_id = stage_session.worker_id if stage_session else None

        # Start live progress after we know the effective cmd + args + piped input.
        cmd_norm = str(cmd_name or "").replace("_", "-").strip().lower()
        _maybe_start_single_live_progress(
            cmd_name_norm=cmd_norm or str(cmd_name or "").strip().lower(),
            filtered_args=filtered_args,
            piped_input=result,
            config=config,
        )

        on_emit = None
        if progress_ui is not None and pipe_idx is not None:
            _ui = progress_ui

            def _on_emit(obj: Any, _progress=_ui) -> None:
                try:
                    _progress.on_emit(0, obj)
                except Exception:
                    pass

            on_emit = _on_emit

        pipeline_ctx = ctx.PipelineStageContext(
            stage_index=0,
            total_stages=1,
            pipe_index=pipe_idx if pipe_idx is not None else 0,
            worker_id=stage_worker_id,
            on_emit=on_emit,
        )
        ctx.set_stage_context(pipeline_ctx)
        stage_status = "completed"
        stage_error = ""

        ctx.set_last_selection(effective_selected_indices)
        try:
            try:
                if hasattr(ctx, "set_current_cmdlet_name"):
                    ctx.set_current_cmdlet_name(cmd_name)
            except Exception:
                pass

            try:
                if hasattr(ctx, "set_current_stage_text"):
                    raw_stage = ""
                    try:
                        raw_stage = (
                            ctx.get_current_command_text("")
                            if hasattr(ctx,
                                       "get_current_command_text") else ""
                        )
                    except Exception:
                        raw_stage = ""
                    if raw_stage:
                        ctx.set_current_stage_text(raw_stage)
                    else:
                        ctx.set_current_stage_text(
                            " ".join([cmd_name,
                                      *filtered_args]).strip() or cmd_name
                        )
            except Exception:
                pass

            ret_code = cmd_fn(result, filtered_args, config)

            if getattr(pipeline_ctx, "emits", None):
                emits = list(pipeline_ctx.emits)

                # Shared `-path` behavior: if the cmdlet emitted temp/PATH file artifacts,
                # move them to the user-specified destination and update emitted paths.
                try:
                    from cmdlet import _shared as sh

                    emits = sh.apply_output_path_from_pipeobjects(
                        cmd_name=cmd_name,
                        args=filtered_args,
                        emits=emits
                    )
                    try:
                        pipeline_ctx.emits = list(emits)
                    except Exception:
                        pass
                except Exception:
                    pass

                # Detect format-selection emits and skip printing (user selects with @N).
                is_format_selection = False
                if emits:
                    first_emit = emits[0]
                    if isinstance(first_emit, dict) and "format_id" in first_emit:
                        is_format_selection = True

                if is_format_selection:
                    ctx.set_last_result_items_only(emits)
                else:
                    table_title = self._get_table_title_for_command(
                        cmd_name,
                        emits,
                        filtered_args
                    )

                    effective_cmd = CmdletCompleter._effective_cmd_name(cmd_name, filtered_args)

                    selectable_commands = {
                        "search-file",
                        "download-data",
                        "download-file",
                        "search_file",
                        "download_data",
                        "download_file",
                        ".config",
                        ".worker",
                    }
                    display_only_commands = {
                        "get-url",
                        "get_url",
                        "get-note",
                        "get_note",
                        "get-relationship",
                        "get_relationship",
                        "get-metadata",
                        "get_metadata",
                    }
                    self_managing_commands = {
                        "tag",
                        "tags",
                        "get-metadata",
                        "get_metadata",
                        "get-url",
                        "get_url",
                        "search-file",
                        "search_file",
                        "add-file",
                        "add_file",
                        "screen-shot",
                        "screenshot",
                        "file",
                    }

                    if effective_cmd in self_managing_commands:
                        table = (
                            ctx.get_display_table()
                            if hasattr(ctx, "get_display_table") else None
                        )
                        if table is None:
                            table = ctx.get_last_result_table()
                        if table is None:
                            table = Table(table_title)
                            for emitted in emits:
                                table.add_result(emitted)
                    else:
                        table = Table(table_title)
                        for emitted in emits:
                            table.add_result(emitted)

                        if effective_cmd in selectable_commands:
                            table.set_source_command(effective_cmd, filtered_args)
                            ctx.set_last_result_table(table, emits)
                            ctx.set_current_stage_table(None)
                        elif effective_cmd in display_only_commands or cmd_name in display_only_commands:
                            ctx.set_last_result_items_only(emits)
                        else:
                            ctx.set_last_result_items_only(emits)

                    # Stop Live progress before printing tables.
                    if progress_ui is not None:
                        try:
                            if pipe_idx is not None:
                                progress_ui.finish_pipe(
                                    int(pipe_idx),
                                    force_complete=(stage_status == "completed")
                                )
                        except Exception:
                            pass
                        try:
                            progress_ui.complete_all_pipes()
                        except Exception:
                            pass
                        try:
                            progress_ui.stop()
                        except Exception:
                            pass
                        try:
                            if hasattr(ctx, "set_live_progress"):
                                ctx.set_live_progress(None)
                        except Exception:
                            pass
                        progress_ui = None
                        pipe_idx = None

                    if not getattr(table, "_rendered_by_cmdlet", False):
                        stdout_console().print()
                        stdout_console().print(table)

            # If the cmdlet produced a current-stage table without emits (e.g. format selection),
            # render it here for parity with REPL pipeline runner.
            if (not getattr(pipeline_ctx,
                            "emits",
                            None)) and hasattr(ctx,
                                               "get_current_stage_table"):
                try:
                    stage_table = ctx.get_current_stage_table()
                except Exception:
                    stage_table = None
                if stage_table is not None:
                    try:
                        already_rendered = bool(
                            getattr(stage_table,
                                    "_rendered_by_cmdlet",
                                    False)
                        )
                    except Exception:
                        already_rendered = False

                    if already_rendered:
                        if progress_ui is not None:
                            try:
                                if pipe_idx is not None:
                                    progress_ui.finish_pipe(
                                        int(pipe_idx),
                                        force_complete=(stage_status == "completed"),
                                    )
                            except Exception:
                                pass
                            try:
                                progress_ui.complete_all_pipes()
                            except Exception:
                                pass
                            try:
                                progress_ui.stop()
                            except Exception:
                                pass
                            try:
                                if hasattr(ctx, "set_live_progress"):
                                    ctx.set_live_progress(None)
                            except Exception:
                                pass
                            progress_ui = None
                            pipe_idx = None
                        try:
                            ctx.set_last_execution_result(
                                status=stage_status,
                                error=stage_error,
                                command_text=" ".join([cmd_name, *filtered_args]).strip() or cmd_name,
                            )
                        except Exception:
                            pass
                        return

                    if progress_ui is not None:
                        try:
                            if pipe_idx is not None:
                                progress_ui.finish_pipe(
                                    int(pipe_idx),
                                    force_complete=(stage_status == "completed")
                                )
                        except Exception:
                            pass
                        try:
                            progress_ui.complete_all_pipes()
                        except Exception:
                            pass
                        try:
                            progress_ui.stop()
                        except Exception:
                            pass
                        try:
                            if hasattr(ctx, "set_live_progress"):
                                ctx.set_live_progress(None)
                        except Exception:
                            pass
                        progress_ui = None
                        pipe_idx = None
                    stdout_console().print()
                    stdout_console().print(stage_table)

            if ret_code != 0:
                stage_status = "failed"
                stage_error = f"exit code {ret_code}"
                # No print here - we want to keep output clean and avoid redundant "exit code" notices.
        except Exception as exc:
            stage_status = "failed"
            stage_error = f"{type(exc).__name__}: {exc}"
            print(f"[error] {type(exc).__name__}: {exc}\n")
        finally:
            if progress_ui is not None:
                try:
                    if pipe_idx is not None:
                        progress_ui.finish_pipe(
                            int(pipe_idx),
                            force_complete=(stage_status == "completed")
                        )
                except Exception:
                    pass
                try:
                    progress_ui.complete_all_pipes()
                except Exception:
                    pass
                try:
                    progress_ui.stop()
                except Exception:
                    pass
                try:
                    if hasattr(ctx, "set_live_progress"):
                        ctx.set_live_progress(None)
                except Exception:
                    pass
            # Do not keep stage tables around after a single command; it can cause
            # later @ selections to bind to stale tables (e.g. old add-file scans).
            try:
                if hasattr(ctx, "set_last_execution_result"):
                    ctx.set_last_execution_result(
                        status=stage_status,
                        error=stage_error,
                        command_text=" ".join([cmd_name, *filtered_args]).strip() or cmd_name,
                    )
            except Exception:
                pass
            try:
                if hasattr(ctx, "set_current_stage_table"):
                    ctx.set_current_stage_table(None)
            except Exception:
                pass
            try:
                if hasattr(ctx, "clear_current_cmdlet_name"):
                    ctx.clear_current_cmdlet_name()
            except Exception:
                pass
            try:
                if hasattr(ctx, "clear_current_stage_text"):
                    ctx.clear_current_stage_text()
            except Exception:
                pass
            ctx.clear_last_selection()
            if stage_session:
                stage_session.close(status=stage_status, error_msg=stage_error)



console = Console()


class CLI:
    """Main CLI application object."""

    ROOT = Path(__file__).resolve().parent

    def __init__(self) -> None:
        self._config_loader = ConfigLoader(root=self.ROOT)

        # Optional dependency auto-install for configured tools (best-effort).
        try:
            from SYS.optional_deps import maybe_auto_install_configured_tools

            maybe_auto_install_configured_tools(self._config_loader.load())
        except Exception:
            pass

        # Initialize the instance choices cache at startup
        try:
            from SYS.cmdlet_spec import SharedArgs
            config = self._config_loader.load()
            SharedArgs._refresh_instance_choices_cache(config)
        except Exception:
            pass

        self._cmdlet_executor = CmdletExecutor(config_loader=self._config_loader)
        self._pipeline_executor = PipelineExecutor(config_loader=self._config_loader)

    @staticmethod
    def parse_selection_syntax(token: str) -> Optional[List[int]]:
        return SelectionSyntax.parse(token)

    def build_app(self) -> typer.Typer:
        app = typer.Typer(help="Medeia-Macina CLI")

        def _validate_pipeline_option(
            ctx: typer.Context,
            param: typer.CallbackParam,
            value: str
        ):
            try:
                from SYS.cli_syntax import validate_pipeline_text

                syntax_error = validate_pipeline_text(
                    value,
                    config=self._config_loader.load(),
                )
                if syntax_error:
                    raise typer.BadParameter(syntax_error.message)
            except typer.BadParameter:
                raise
            except Exception:
                pass
            return value

        @app.command("pipeline")
        def pipeline(
            command: str = typer.Option(
                ...,
                "--pipeline",
                "-p",
                help="Pipeline command string to execute",
                callback=_validate_pipeline_option,
            ),
            seeds_json: Optional[str] = typer.Option(
                None,
                "--seeds-json",
                "-s",
                help="JSON string of seed items"
            ),
        ) -> None:
            from SYS import pipeline as ctx

            config = self._config_loader.load()
            debug_enabled = bool(config.get("debug", False))
            set_debug(debug_enabled)

            if seeds_json:
                try:
                    seeds = json.loads(seeds_json)
                    if not isinstance(seeds, list):
                        seeds = [seeds]
                    ctx.set_last_result_items_only(seeds)
                except Exception as exc:
                    print(f"Error parsing seeds JSON: {exc}")
                    return

            try:
                from SYS.cli_syntax import validate_pipeline_text

                syntax_error = validate_pipeline_text(command, config=config)
                if syntax_error:
                    print(syntax_error.message, file=sys.stderr)
                    return
            except Exception:
                pass

            try:
                tokens = shlex.split(command)
            except ValueError as exc:
                print(f"Syntax error: {exc}", file=sys.stderr)
                return

            if not tokens:
                return
            self._pipeline_executor.execute_tokens(tokens)

        @app.command("repl")
        def repl() -> None:
            self.run_repl()

        @app.callback(invoke_without_command=True)
        def main_callback(ctx: typer.Context) -> None:
            if ctx.invoked_subcommand is None:
                self.run_repl()

        _ = (pipeline, repl, main_callback)

        # Dynamically register all cmdlets as top-level Typer commands so users can
        # invoke `mm <cmdlet> [args]` directly from the shell. We use Click/Typer
        # context settings to allow arbitrary flags and options to pass through to
        # the cmdlet system without Typer trying to parse them.
        try:
            names = list_cmdlet_names()
            skip = {"pipeline", "repl"}
            for nm in names:
                if not nm or nm in skip:
                    continue

                # create a scoped handler to capture the command name
                def _make_handler(cmd_name: str):

                    @app.command(
                        cmd_name,
                        context_settings={
                            "ignore_unknown_options": True,
                            "allow_extra_args": True,
                        },
                    )
                    def _handler(ctx: typer.Context):
                        try:
                            args = list(ctx.args or [])
                        except Exception:
                            args = []
                        self._cmdlet_executor.execute(cmd_name, args)

                    return _handler

                _make_handler(nm)
        except Exception:
            # Don't let failure to register dynamic commands break startup
            pass

        return app

    def run(self) -> None:
        # Ensure Rich tracebacks are active even when invoking subcommands.
        try:
            config = self._config_loader.load()
            debug_enabled = bool(config.get("debug",
                                            False)
                                 ) if isinstance(config,
                                                 dict) else False
        except Exception:
            debug_enabled = False

        set_debug(debug_enabled)
        _install_rich_traceback(show_locals=debug_enabled)

        self.build_app()()

    def run_repl(self) -> None:
        # console = Console(width=100)

        from SYS.design import load_kappa, load_quote

        kappa = load_kappa()
        startup_quote = load_quote()
        rainbow = list(kappa.get("colors") or [])
        pillar_height = int(kappa.get("height") or 21)
        bar_width = int(kappa.get("bar_width") or 36)

        def rainbow_pillar(colors, height=21, bar_width=36):
            table = RichTable.grid(padding=0)
            table.add_column(no_wrap=True)

            for i in range(height):
                color = colors[i % len(colors)]
                table.add_row(Bar(size=1, begin=0, end=1, width=bar_width, color=color))

            return table

        root = Layout(name="root")
        root.split_row(
            Layout(name="left",
                   ratio=int(kappa.get("left_ratio") or 2)),
            Layout(name="center",
                   ratio=int(kappa.get("center_ratio") or 8)),
            Layout(name="right",
                   ratio=int(kappa.get("right_ratio") or 2)),
        )

        root["left"].update(
            Panel(rainbow_pillar(rainbow,
                                 height=pillar_height,
                                 bar_width=bar_width),
                  title=str(kappa.get("title_left") or "DELTA"))
        )

        root["right"].update(
            Panel(
                rainbow_pillar(list(reversed(rainbow)),
                               height=pillar_height,
                               bar_width=bar_width),
                title=str(kappa.get("title_right") or "LAMBDA")
            )
        )

        center_md = Markdown(str(kappa.get("markdown") or ""))
        root["center"].update(
            Panel(
                center_md,
                title=str(kappa.get("title_center") or "KAPPA"),
                height=pillar_height,
            )
        )

        console.print(root)

        prompt_text = "<🜂🜄|🜁🜃>"

        startup_table = Table(
            "*********<IGNITIO>*********<NOUSEMPEH>*********<RUGRAPOG>*********<OMEGHAU>*********"
        )
        startup_table._interactive(True)._perseverance(True)
        startup_table.set_value_case("upper")

        def _add_startup_check(
            status: str,
            name: str,
            *,
            plugin: str = "",
            instance: str = "",
            files: int | str | None = None,
            detail: str = "",
        ) -> None:
            _shared_add_startup_check(
                startup_table,
                status,
                name,
                plugin=plugin,
                instance=instance,
                files=files,
                detail=detail,
            )

        config = self._config_loader.load()
        debug_enabled = bool(config.get("debug", False))
        set_debug(debug_enabled)
        _install_rich_traceback(show_locals=debug_enabled)
        _add_startup_check("ENABLED" if debug_enabled else "DISABLED", "DEBUGGING")

        try:
            from PluginCore.registry import plugin_attr

            for check in _collect_plugin_startup_checks(config):
                _add_startup_check(
                    str(check.get("status") or "UNKNOWN"),
                    str(check.get("name") or "Plugin"),
                    plugin=str(check.get("plugin") or ""),
                    instance=str(check.get("instance") or ""),
                    detail=str(check.get("detail") or ""),
                    files=check.get("files"),
                )

            # Plugin support checks (configured via [plugin=...])
            if _has_plugin(config, "florencevision") and plugin_attr("florencevision", "FlorenceVisionTool") is not None:
                try:
                    plugin_cfg = config.get("plugin")
                    fv_cfg = plugin_cfg.get("florencevision") if isinstance(plugin_cfg, dict) else None
                    enabled = bool(fv_cfg.get("enabled")) if isinstance(fv_cfg, dict) else False
                    if not enabled:
                        _add_startup_check(
                            "DISABLED",
                            "FlorenceVision",
                            plugin="plugin",
                            detail="Not enabled",
                        )
                    else:
                        from SYS.optional_deps import florencevision_missing_modules

                        missing = florencevision_missing_modules()
                        if missing:
                            _add_startup_check(
                                "DISABLED",
                                "FlorenceVision",
                                plugin="plugin",
                                detail="Missing: " + ", ".join(missing),
                            )
                        else:
                            _add_startup_check(
                                "ENABLED",
                                "FlorenceVision",
                                plugin="plugin",
                                detail="Ready",
                            )
                except Exception as exc:
                    _add_startup_check(
                        "DISABLED",
                        "FlorenceVision",
                        plugin="plugin",
                        detail=str(exc),
                    )
        except Exception as exc:
            _add_startup_check("ERROR", "STARTUP", detail=str(exc))

        if startup_table.rows:
            stdout_console().print()
            stdout_console().print(startup_table)

        style = Style.from_dict(
            {
                "cmdlet": "#ffffff",
                "argument": "#3b8eea",
                "value": "#9a3209",
                "string": "#6d0d93",
                "pipe": "#4caf50",
                "selection_at": "#f1c40f",
                "selection_range": "#4caf50",
                "template_func": "#56b6c2",
                "template_bracket": "#e5c07b",
                "template_or": "#d19a66",
                "template_placeholder": "#98c379",
                "template_field": "#c678dd",
                "bottom-toolbar": "noreverse",
            }
        )

        class ToolbarState:
            text: str = ""
            last_update_time: float = 0.0
            clear_timer: Optional[threading.Timer] = None

        toolbar_state = ToolbarState()
        session: Optional[PromptSession] = None

        def get_toolbar() -> Optional[str]:
            if not toolbar_state.text or not toolbar_state.text.strip():
                return None
            if time.time() - toolbar_state.last_update_time > 3:
                toolbar_state.text = ""
                return None
            return toolbar_state.text

        def update_toolbar(text: str) -> None:
            nonlocal session
            text = text.strip()
            toolbar_state.text = text
            toolbar_state.last_update_time = time.time()

            if toolbar_state.clear_timer:
                toolbar_state.clear_timer.cancel()
                toolbar_state.clear_timer = None

            if text:

                def clear_toolbar() -> None:
                    toolbar_state.text = ""
                    toolbar_state.clear_timer = None
                    if session is not None and hasattr(
                            session,
                            "app") and session.app.is_running:
                        session.app.invalidate()

                toolbar_state.clear_timer = threading.Timer(3.0, clear_toolbar)
                toolbar_state.clear_timer.daemon = True
                toolbar_state.clear_timer.start()

            if session is not None and hasattr(session,
                                               "app") and session.app.is_running:
                session.app.invalidate()

        self._pipeline_executor.set_toolbar_output(update_toolbar)

        completer = CmdletCompleter(config_loader=self._config_loader)
        try:
            from SYS.utils import coerce_bool as _coerce_bool

            repl_cfg = self._config_loader.load_shared()
            complete_while_typing = _coerce_bool(repl_cfg.get("complete_while_typing"), True)
        except Exception:
            complete_while_typing = True
        session = PromptSession(
            completer=cast(Any,
                           completer),
            lexer=MedeiaLexer(),
            style=style,
            bottom_toolbar=get_toolbar,
            refresh_interval=0.5,
            complete_while_typing=complete_while_typing,
        )

        queued_inputs: List[Dict[str, Any]] = []
        queued_inputs_lock = threading.Lock()
        repl_queue_stop = threading.Event()
        injected_payload: Optional[Dict[str, Any]] = None
        repl_session_id = uuid.uuid4().hex

        def _drain_repl_queue() -> None:
            try:
                pending = pop_repl_commands(self.ROOT, limit=8)
            except Exception:
                pending = []
            if not pending:
                return
            with queued_inputs_lock:
                queued_inputs.extend(pending)

        def _inject_repl_command(payload: Dict[str, Any]) -> bool:
            nonlocal session, injected_payload
            command_text = str(payload.get("command") or "").strip()
            source_text = str(payload.get("source") or "external").strip() or "external"
            if not command_text or session is None:
                return False

            update_toolbar(f"queued from {source_text}: {command_text[:96]}")
            app = getattr(session, "app", None)
            if app is None or not getattr(app, "is_running", False):
                return False

            injected = False

            def _apply() -> None:
                nonlocal injected, injected_payload
                try:
                    buffer = getattr(session, "default_buffer", None)
                    if buffer is not None:
                        with queued_inputs_lock:
                            injected_payload = payload
                        buffer.document = Document(text=command_text, cursor_position=len(command_text))
                        try:
                            buffer.validate_and_handle()
                            injected = True
                            return
                        except Exception:
                            pass
                    with queued_inputs_lock:
                        injected_payload = payload
                    app.exit(result=command_text)
                    injected = True
                except Exception:
                    with queued_inputs_lock:
                        injected_payload = None
                    injected = False

            try:
                loop = getattr(app, "loop", None)
                if loop is not None and hasattr(loop, "call_soon_threadsafe"):
                    loop.call_soon_threadsafe(_apply)
                    return True
            except Exception:
                pass

            try:
                _apply()
            except Exception:
                injected = False
            return injected

        def _queue_poll_loop() -> None:
            while not repl_queue_stop.is_set():
                try:
                    touch_repl_state(self.ROOT, session_id=repl_session_id)
                except Exception:
                    pass
                _drain_repl_queue()
                with queued_inputs_lock:
                    next_payload = queued_inputs[0] if queued_inputs else None
                if next_payload and _inject_repl_command(next_payload):
                    with queued_inputs_lock:
                        if queued_inputs and queued_inputs[0] is next_payload:
                            queued_inputs.pop(0)
                repl_queue_stop.wait(0.25)

        try:
            touch_repl_state(self.ROOT, session_id=repl_session_id)
        except Exception:
            pass

        _drain_repl_queue()
        repl_queue_thread = threading.Thread(
            target=_queue_poll_loop,
            name="medeia-repl-queue",
            daemon=True,
        )
        repl_queue_thread.start()

        try:
            while True:
                try:
                    with queued_inputs_lock:
                        queued_payload = queued_inputs.pop(0) if queued_inputs else None

                    if queued_payload is not None:
                        source_text = str(queued_payload.get("source") or "external").strip() or "external"
                        user_input = str(queued_payload.get("command") or "").strip()
                        if user_input:
                            print(f"{prompt_text}{user_input}  [queued:{source_text}]")
                        else:
                            user_input = ""
                    else:
                        user_input = session.prompt(prompt_text).strip()
                        if user_input:
                            with queued_inputs_lock:
                                if injected_payload is not None:
                                    queued_payload = injected_payload
                                    injected_payload = None
                except (EOFError, KeyboardInterrupt):
                    print(startup_quote)
                    break

                if not user_input:
                    continue

                low = user_input.lower()
                if low in {"exit",
                           "quit",
                           "q"}:
                    print(startup_quote)
                    break
                if low in {"help",
                           "?"}:
                    self._cmdlet_executor.execute(".help", [])
                    continue

                pipeline_ctx_ref = None
                queued_metadata = (
                    queued_payload.get("metadata")
                    if isinstance(queued_payload, dict) and isinstance(queued_payload.get("metadata"), dict)
                    else None
                )
                progress_event_callback = _build_mpv_progress_callback(queued_metadata) if queued_metadata else None
                try:
                    from SYS import pipeline as ctx

                    ctx.set_current_command_text(user_input)
                    if hasattr(ctx, "set_progress_event_callback"):
                        ctx.set_progress_event_callback(progress_event_callback)
                    pipeline_ctx_ref = ctx
                except Exception:
                    pipeline_ctx_ref = None

                if queued_metadata:
                    try:
                        _send_mpv_callback_event(
                            queued_metadata,
                            {
                                "phase": "started",
                                "event": "command-started",
                                "command_text": user_input,
                            },
                        )
                    except Exception:
                        pass

                execution_result: Dict[str, Any] = {
                    "status": "completed",
                    "success": True,
                    "error": "",
                    "command_text": user_input,
                }

                try:
                    from SYS.cli_syntax import validate_pipeline_text

                    syntax_error = validate_pipeline_text(user_input, config=config)
                    if syntax_error:
                        execution_result = {
                            "status": "failed",
                            "success": False,
                            "error": str(syntax_error.message or "syntax error"),
                            "command_text": user_input,
                        }
                        print(syntax_error.message, file=sys.stderr)
                        if queued_metadata:
                            try:
                                _notify_mpv_completion(queued_metadata, execution_result)
                            except Exception:
                                pass
                        continue
                except Exception:
                    pass

                try:
                    tokens = shlex.split(user_input)
                except ValueError as exc:
                    execution_result = {
                        "status": "failed",
                        "success": False,
                        "error": str(exc),
                        "command_text": user_input,
                    }
                    print(f"Syntax error: {exc}", file=sys.stderr)
                    if queued_metadata:
                        try:
                            _notify_mpv_completion(queued_metadata, execution_result)
                        except Exception:
                            pass
                    continue

                if not tokens:
                    continue

                if len(tokens) == 1 and tokens[0] == "@,,":
                    try:
                        from SYS import pipeline as ctx

                        if ctx.restore_next_result_table():
                            last_table = (
                                ctx.get_display_table()
                                if hasattr(ctx,
                                           "get_display_table") else None
                            )
                            if last_table is None:
                                last_table = ctx.get_last_result_table()
                            if last_table:
                                stdout_console().print()
                                ctx.set_current_stage_table(last_table)
                                stdout_console().print(last_table)
                            else:
                                items = ctx.get_last_result_items()
                                if items:
                                    ctx.set_current_stage_table(None)
                                    print(
                                        f"Restored {len(items)} items (no table format available)"
                                    )
                                else:
                                    print("No forward history available", file=sys.stderr)
                        else:
                            print("No forward history available", file=sys.stderr)
                    except Exception as exc:
                        print(f"Error restoring next table: {exc}", file=sys.stderr)
                    continue

                if len(tokens) == 1 and tokens[0] == "@..":
                    try:
                        from SYS import pipeline as ctx

                        if ctx.restore_previous_result_table():
                            last_table = (
                                ctx.get_display_table()
                                if hasattr(ctx,
                                           "get_display_table") else None
                            )
                            if last_table is None:
                                last_table = ctx.get_last_result_table()

                            # Auto-refresh search-file tables when navigating back,
                            # so row payloads (titles/tags) reflect latest store state.
                            try:
                                src_cmd = (
                                    getattr(last_table,
                                            "source_command",
                                            None) if last_table else None
                                )
                                if (isinstance(src_cmd,
                                               str)
                                        and src_cmd.lower().replace("_",
                                                                    "-") == "search-file"):
                                    src_args = (
                                        getattr(last_table,
                                                "source_args",
                                                None) if last_table else None
                                    )
                                    base_args = list(src_args
                                                     ) if isinstance(src_args,
                                                                     list) else []
                                    cleaned_args = [
                                        str(a) for a in base_args if str(a).strip().lower()
                                        not in {"--refresh", "-refresh"}
                                    ]
                                    if hasattr(ctx, "set_current_command_text"):
                                        try:
                                            title_text = (
                                                getattr(last_table,
                                                        "title",
                                                        None) if last_table else None
                                            )
                                            if isinstance(title_text,
                                                          str) and title_text.strip():
                                                ctx.set_current_command_text(
                                                    title_text.strip()
                                                )
                                            else:
                                                ctx.set_current_command_text(
                                                    " ".join(
                                                        ["search-file",
                                                         *cleaned_args]
                                                    ).strip()
                                                )
                                        except Exception:
                                            pass
                                    try:
                                        self._cmdlet_executor.execute(
                                            "search-file",
                                            cleaned_args + ["--refresh"]
                                        )
                                    finally:
                                        if hasattr(ctx, "clear_current_command_text"):
                                            try:
                                                ctx.clear_current_command_text()
                                            except Exception:
                                                pass
                                    continue
                            except Exception as exc:
                                print(
                                    f"Error refreshing search-file table: {exc}",
                                    file=sys.stderr
                                )

                            if last_table:
                                stdout_console().print()
                                ctx.set_current_stage_table(last_table)
                                stdout_console().print(last_table)
                            else:
                                items = ctx.get_last_result_items()
                                if items:
                                    ctx.set_current_stage_table(None)
                                    print(
                                        f"Restored {len(items)} items (no table format available)"
                                    )
                                else:
                                    print("No previous result table. @.. only works after you leave a table.")
                        else:
                            print("Result table history is empty")
                    except Exception as exc:
                        print(f"Error restoring previous result table: {exc}")
                    continue

                try:
                    if "|" in tokens or (tokens and tokens[0].startswith("@")):
                        self._pipeline_executor.execute_tokens(tokens)
                    else:
                        if pipeline_ctx_ref is not None:
                            try:
                                pipeline_ctx_ref.clear_pending_pipeline_tail()
                            except Exception:
                                pass
                        cmd_name = tokens[0].replace("_", "-").lower()
                        is_help = any(
                            arg in {"-help",
                                    "--help",
                                    "-h"} for arg in tokens[1:]
                        )
                        if is_help:
                            self._cmdlet_executor.execute(".help", [cmd_name])
                        else:
                            self._cmdlet_executor.execute(cmd_name, tokens[1:])
                finally:
                    if pipeline_ctx_ref and hasattr(pipeline_ctx_ref, "get_last_execution_result"):
                        try:
                            latest = pipeline_ctx_ref.get_last_execution_result()
                            if isinstance(latest, dict) and latest:
                                execution_result = latest
                        except Exception:
                            pass
                    if queued_metadata:
                        try:
                            _notify_mpv_completion(queued_metadata, execution_result)
                        except Exception:
                            pass
                    if pipeline_ctx_ref:
                        pipeline_ctx_ref.clear_current_command_text()
                        if hasattr(pipeline_ctx_ref, "set_progress_event_callback"):
                            try:
                                pipeline_ctx_ref.set_progress_event_callback(None)
                            except Exception:
                                pass
        finally:
            repl_queue_stop.set()
            try:
                repl_queue_thread.join(timeout=1.0)
            except Exception:
                pass
            try:
                clear_repl_state(self.ROOT)
            except Exception:
                pass
