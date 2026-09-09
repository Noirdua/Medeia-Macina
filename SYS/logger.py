"""Unified logging utility for automatic file and function name tracking."""

import sys
import logging
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

# SYS.rich_display deferred: rich (~100ms) loaded lazily on first log output.
_rich_display_mod: Any = None


def _console_for(file):
    global _rich_display_mod
    if _rich_display_mod is None:
        import SYS.rich_display as _m
        _rich_display_mod = _m
    return _rich_display_mod.console_for(file)

logger = logging.getLogger(__name__)

# Global DB logger set later to avoid circular imports
_DB_LOGGER = None

def set_db_logger(func):
    global _DB_LOGGER
    _DB_LOGGER = func

_DEBUG_ENABLED = False
_thread_local = threading.local()


def set_thread_stream(stream):
    """Set a custom output stream for the current thread."""
    _thread_local.stream = stream


def get_thread_stream():
    """Get the custom output stream for the current thread, if any."""
    return getattr(_thread_local, "stream", None)


def set_debug(enabled: bool) -> None:
    """Enable or disable debug logging."""
    global _DEBUG_ENABLED
    _DEBUG_ENABLED = enabled


def is_debug_enabled() -> bool:
    """Check if debug logging is enabled."""
    return _DEBUG_ENABLED


def _debug_output_suppressed() -> bool:
    try:
        stderr_name = getattr(sys.stderr, "name", "")
        return "nul" in str(stderr_name).lower() or "/dev/null" in str(stderr_name)
    except Exception:
        return False


def _debug_output_file(file=None):
    stream = get_thread_stream()
    if stream is not None:
        return stream
    if file is not None:
        return file
    return sys.stderr


def _is_rich_renderable(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, bytes, bytearray)):
        return False
    return bool(
        hasattr(value, "__rich_console__")
        or hasattr(value, "__rich__")
        or value.__class__.__module__.startswith("rich.")
    )


def _caller_location(depth: int = 1) -> tuple[str, str]:
    import inspect as _inspect
    frame = _inspect.currentframe()
    current = frame
    try:
        for _ in range(max(0, int(depth))):
            if current is None:
                break
            current = current.f_back

        if current is None:
            return "", ""

        return Path(current.f_code.co_filename).stem, f"{current.f_code.co_name}:{current.f_lineno}"
    finally:
        del frame


def _debug_db_log(*, caller_name: str, message: str, level: str = "DEBUG") -> None:
    if not _DB_LOGGER:
        return
    try:
        _DB_LOGGER(level, caller_name, message)
    except Exception:
        pass


def debug_panel(
    title: str,
    rows: Sequence[tuple[str, Any]],
    *,
    file=None,
    border_style: str = "cyan",
) -> None:
    """Render a compact key/value debug panel when debug logging is enabled."""
    if not _DEBUG_ENABLED or _debug_output_suppressed():
        return

    target_file = _debug_output_file(file)
    live = None
    paused = False
    try:
        from SYS import pipeline as _pipeline_ctx

        live = (
            _pipeline_ctx.get_live_progress()
            if hasattr(_pipeline_ctx, "get_live_progress")
            else None
        )
        if live is not None and getattr(live, "_live", None) is not None:
            live.pause()
            paused = True
    except Exception:
        live = None
        paused = False
    try:
        from rich.table import Table as RichTable
        from SYS.result_table import theme_styles, themed_panel

        styles = theme_styles()
        grid = RichTable.grid(padding=(0, 1))
        grid.add_column("Key", style=styles["accent"], no_wrap=True)
        grid.add_column("Value", style=styles["value"])
        for key, val in rows:
            try:
                grid.add_row(str(key), str(val))
            except Exception:
                grid.add_row(str(key), "<unprintable>")

        debug(
            themed_panel(grid, title=str(title or "Debug"), expand=False),
            file=target_file,
        )

        file_name, func_name = _caller_location(depth=2)
        caller_name = f"{file_name}.{func_name}" if file_name and func_name else ""
        _debug_db_log(
            caller_name=caller_name,
            message=f"[{title}] " + "; ".join(f"{k}={v}" for k, v in rows),
        )
    except Exception:
        debug(title, list(rows), file=target_file)
    finally:
        if paused and live is not None:
            try:
                live.resume()
            except Exception:
                pass


def status_panel(
    title: str,
    rows: Sequence[tuple[str, Any]],
    *,
    file=None,
) -> None:
    """Render a themed key/value panel for user-facing status messages.

    Unlike :func:`debug_panel`, this always renders (it is not gated on debug
    mode). It defaults to stdout.
    """
    target_file = file if file is not None else sys.stdout
    try:
        from rich.table import Table as RichTable
        from SYS.result_table import theme_styles, themed_panel

        styles = theme_styles()
        grid = RichTable.grid(padding=(0, 1))
        grid.add_column("Key", style=styles["accent"], no_wrap=True)
        grid.add_column("Value", style=styles["value"])
        for key, val in rows:
            try:
                grid.add_row(str(key), str(val))
            except Exception:
                grid.add_row(str(key), "<unprintable>")
        _console_for(target_file).print(
            themed_panel(grid, title=str(title or "Status"), expand=False)
        )
    except Exception:
        _console_for(target_file).print(
            f"{title}: " + "; ".join(f"{k}={v}" for k, v in rows)
        )


def error_panel(
    title: str,
    rows: Sequence[tuple[str, Any]],
    *,
    file=None,
) -> None:
    """Render a red Error panel for user-facing failures."""
    target_file = file if file is not None else sys.stderr
    try:
        from rich.panel import Panel
        from rich.table import Table as RichTable
        from SYS.result_table import theme_styles

        styles = theme_styles()
        grid = RichTable.grid(padding=(0, 1))
        grid.add_column("Key", style="bold red", no_wrap=True)
        grid.add_column("Value", style=styles["value"])
        for key, val in rows:
            try:
                grid.add_row(str(key), str(val))
            except Exception:
                grid.add_row(str(key), "<unprintable>")
        _console_for(target_file).print(
            Panel(
                grid,
                title=str(title or "Error"),
                border_style="red",
                expand=False,
            )
        )
    except Exception:
        _console_for(target_file).print(
            f"{title}: " + "; ".join(f"{k}={v}" for k, v in rows)
        )


def debug(*args, **kwargs) -> None:
    """Print debug message if debug logging is enabled.

    Automatically routes through log() so debug output keeps the caller prefix.
    """
    if not _DEBUG_ENABLED:
        return

    # Check if stderr has been redirected to /dev/null (quiet mode)
    # If so, skip output to avoid queuing in background worker's capture
    if _debug_output_suppressed():
        return

    target_file = _debug_output_file(kwargs.pop("file", None))

    if len(args) == 1 and _is_rich_renderable(args[0]):
        renderable = args[0]
        _console_for(target_file).print(renderable)
        file_name, func_name = _caller_location(depth=1)
        caller_name = f"{file_name}.{func_name}" if file_name and func_name else ""
        _debug_db_log(caller_name=caller_name, message=f"<rich:{type(renderable).__name__}>")
        return

    # Use the same logic as log()
    log(*args, file=target_file, **kwargs)


def debug_inspect(
    obj,
    *,
    title: str | None = None,
    file=None,
    methods: bool = False,
    docs: bool = False,
    private: bool = False,
    dunder: bool = False,
    sort: bool = True,
    all: bool = False,
    value: bool = True,
) -> None:
    """Rich-inspect an object when debug logging is enabled.

    Uses the same stream / quiet-mode behavior as `debug()` and prepends a
    `[file.function]` prefix when debug is enabled.
    """
    if not _DEBUG_ENABLED:
        return

    # Mirror debug() quiet-mode guard.
    if _debug_output_suppressed():
        return

    # Resolve destination stream.
    file = _debug_output_file(file)

    # Compute caller prefix (same as log()).
    prefix = None
    import inspect as _inspect
    frame = _inspect.currentframe()
    if frame is not None and frame.f_back is not None:
        caller_frame = frame.f_back
        try:
            file_name = Path(caller_frame.f_code.co_filename).stem
            func_name = caller_frame.f_code.co_name
            prefix = f"[{file_name}.{func_name}]"
        finally:
            del caller_frame
    if frame is not None:
        del frame

    # Render.
    from rich import inspect as rich_inspect

    console = _console_for(file)
    # If the caller provides a title, treat it as authoritative.
    # Only fall back to the automatic [file.func] prefix when no title is supplied.
    effective_title = title
    if not effective_title and prefix:
        effective_title = prefix

    # Show full identifiers (hashes/paths) without Rich shortening.
    # Guard for older Rich versions which may not support max_* parameters.
    try:
        rich_inspect(
            obj,
            console=console,
            title=effective_title,
            methods=methods,
            docs=docs,
            private=private,
            dunder=dunder,
            sort=sort,
            all=all,
            value=value,
            max_string=100_000,
            max_length=100_000,
        )  # type: ignore[call-arg]
    except TypeError:
        rich_inspect(
            obj,
            console=console,
            title=effective_title,
            methods=methods,
            docs=docs,
            private=private,
            dunder=dunder,
            sort=sort,
            all=all,
            value=value,
        )

def log(*args, **kwargs) -> None:
    """Print with automatic file.function prefix.

    Automatically prepends [filename.function_name] to all output.
    Defaults to stdout if not specified.

    Example:
        log("Upload started")  # Output: [add_file.run] Upload started
    """
    # When debug is disabled, suppress the automatic prefix for cleaner user-facing output.
    add_prefix = _DEBUG_ENABLED

    # Get the calling frame
    import inspect as _inspect
    frame = _inspect.currentframe()
    if frame is None:
        file = kwargs.pop("file", sys.stdout)
        sep = kwargs.pop("sep", " ")
        end = kwargs.pop("end", "\n")
        _console_for(file).print(*args, sep=sep, end=end)
        return

    caller_frame = frame.f_back
    if caller_frame is None:
        file = kwargs.pop("file", sys.stdout)
        sep = kwargs.pop("sep", " ")
        end = kwargs.pop("end", "\n")
        _console_for(file).print(*args, sep=sep, end=end)
        return

    try:
        # Get file name without extension
        file_name = Path(caller_frame.f_code.co_filename).stem

        # Get function name
        func_name = caller_frame.f_code.co_name

        # Check for thread-local stream first
        stream = get_thread_stream()
        if stream:
            kwargs["file"] = stream
        # Set default to stdout if not specified
        elif "file" not in kwargs:
            kwargs["file"] = sys.stdout

        file = kwargs.pop("file", sys.stdout)
        sep = kwargs.pop("sep", " ")
        end = kwargs.pop("end", "\n")
        if add_prefix:
            prefix = f"[{file_name}.{func_name}:{caller_frame.f_lineno}]"
            _console_for(file).print(prefix, *args, sep=sep, end=end)
        else:
            _console_for(file).print(*args, sep=sep, end=end)

        # Log to database if available
        if _DB_LOGGER:
            try:
                msg = sep.join(map(str, args))
                level = "DEBUG" if add_prefix else "INFO"
                _DB_LOGGER(level, f"{file_name}.{func_name}", msg)
            except Exception:
                pass
    finally:
        del frame
        del caller_frame