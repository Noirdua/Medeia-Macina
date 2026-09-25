from __future__ import annotations
"""Medeia-Macina CLI.

This module intentionally uses a class-based architecture:
- no legacy procedural entrypoints
- no compatibility shims
- all REPL/pipeline/cmdlet execution state lives on objects
"""

import json
import re
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

        self._pipeline_executor = PipelineExecutor(config_loader=self._config_loader)

    def _run_tokens(self, tokens: List[str], *, exit_on_error: bool = False) -> int:
        code = int(self._pipeline_executor.execute_tokens(list(tokens)) or 0)
        if exit_on_error and code:
            raise typer.Exit(code=code)
        return code

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
                    print(f"Error parsing seeds JSON: {exc}", file=sys.stderr)
                    raise typer.Exit(code=1)

            try:
                from SYS.cli_syntax import validate_pipeline_text

                syntax_error = validate_pipeline_text(command, config=config)
                if syntax_error:
                    print(syntax_error.message, file=sys.stderr)
                    raise typer.Exit(code=1)
            except typer.Exit:
                raise
            except Exception:
                pass

            try:
                from SYS.cli_syntax import split_shell_tokens

                tokens = split_shell_tokens(command)
            except ValueError as exc:
                print(f"Syntax error: {exc}", file=sys.stderr)
                raise typer.Exit(code=1)

            if not tokens:
                raise typer.Exit(code=1)
            self._run_tokens(tokens, exit_on_error=True)

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
                        self._run_tokens([cmd_name, *args], exit_on_error=True)

                    return _handler

                _make_handler(nm)
        except Exception:
            # Don't let failure to register dynamic commands break startup
            pass

        return app

    def run(self) -> None:
        try:
            from SYS.env_check import apply_debug_from_config

            apply_debug_from_config(self.ROOT / "medios.db")
        except Exception:
            pass
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
                "system_hash": "#e5c07b",
                "system_field": "#e06c75",
                "system_op": "#56b6c2",
                "system_number": "#98c379",
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
                    self._run_tokens([".help"])
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
                    from SYS.cli_syntax import split_shell_tokens

                    tokens = split_shell_tokens(user_input)
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
                                        self._run_tokens(
                                            ["search-file", *cleaned_args, "--refresh"]
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
                    if not ("|" in tokens or (tokens and tokens[0].startswith("@"))):
                        if pipeline_ctx_ref is not None:
                            try:
                                pipeline_ctx_ref.clear_pending_pipeline_tail()
                            except Exception:
                                pass
                        cmd_name = tokens[0].replace("_", "-").lower()
                        if any(arg in {"-help", "--help", "-h"} for arg in tokens[1:]):
                            tokens = [".help", cmd_name]
                        else:
                            tokens = [cmd_name, *tokens[1:]]
                    self._run_tokens(tokens)
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
