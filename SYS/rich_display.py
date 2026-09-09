"""Central Rich output helpers.

Opinionated: `rich` is a required dependency.

This module centralizes Console instances so tables/panels render consistently and
so callers can choose stdout vs stderr explicitly (important for pipeline-safe
output).
"""

from __future__ import annotations

import contextlib
import os
import sys
from typing import Any, Iterator, TextIO, List, Dict, Optional, Tuple, cast

from pathlib import Path
from SYS.utils import expand_path

# rich imports are deferred to first Console use to avoid ~100ms startup cost.
# They are loaded the first time any Console function is called.

_STDOUT_CONSOLE: Any = None
_STDERR_CONSOLE: Any = None

_TERMINAL_LIVE_SUPPORT_CACHED: Optional[bool] = None


def terminal_supports_live_progress() -> bool:
    """Return True when stderr is a terminal capable of Rich Live cursor-control.

    Some Linux terminals report ``isatty() == True`` but cannot honour the
    ANSI escape sequences Rich uses for Live (cursor save/restore, alternate
    screen).  This function adds extra guards:

    * ``MM_NO_RICH_LIVE=1``         — force-disable all Rich Live progress
    * ``TERM`` is ``dumb``, unset, or empty (non-Windows) → treat as non-TTY
    """
    global _TERMINAL_LIVE_SUPPORT_CACHED
    if _TERMINAL_LIVE_SUPPORT_CACHED is not None:
        return _TERMINAL_LIVE_SUPPORT_CACHED

    if os.environ.get("MM_NO_RICH_LIVE") in {"1", "true", "yes", "on"}:
        _TERMINAL_LIVE_SUPPORT_CACHED = False
        return False

    try:
        is_tty = bool(getattr(sys.stderr, "isatty", lambda: False)())
    except Exception:
        _TERMINAL_LIVE_SUPPORT_CACHED = False
        return False

    if not is_tty:
        _TERMINAL_LIVE_SUPPORT_CACHED = False
        return False

    if sys.platform != "win32":
        term_value = os.environ.get("TERM")
        if term_value is None:
            _TERMINAL_LIVE_SUPPORT_CACHED = False
            return False
        term_clean = str(term_value).strip().lower()
        if not term_clean or term_clean == "dumb":
            _TERMINAL_LIVE_SUPPORT_CACHED = False
            return False

    _TERMINAL_LIVE_SUPPORT_CACHED = True
    return True


def _ensure_consoles() -> None:
    global _STDOUT_CONSOLE, _STDERR_CONSOLE
    if _STDOUT_CONSOLE is None:
        from rich.console import Console
        from rich.pretty import install as _pretty_install
        try:
            _pretty_install(max_string=100_000, max_length=100_000)
        except TypeError:
            _pretty_install()
        except Exception:
            pass
        console_kwargs: Dict[str, Any] = {}
        try:
            from SYS.config import load_config

            raw = str((load_config(emit_summary=False) or {}).get("color_system") or "auto").strip().lower()
            if raw in {"none", "off", "false"}:
                console_kwargs["color_system"] = None
            elif raw in {"standard", "256", "truecolor", "windows"}:
                console_kwargs["color_system"] = raw
        except Exception:
            pass
        _STDOUT_CONSOLE = Console(file=sys.stdout, **console_kwargs)
        _STDERR_CONSOLE = Console(file=sys.stderr, **console_kwargs)


def stdout_console() -> Any:
    _ensure_consoles()
    return _STDOUT_CONSOLE


def stderr_console() -> Any:
    _ensure_consoles()
    return _STDERR_CONSOLE


def console_for(file: Any) -> Any:
    if file is None or file is sys.stdout:
        _ensure_consoles()
        return _STDOUT_CONSOLE
    if file is sys.stderr:
        _ensure_consoles()
        return _STDERR_CONSOLE
    from rich.console import Console
    return Console(file=file)


def rprint(renderable: Any = "", *, file: TextIO | None = None) -> None:
    console_for(file).print(renderable)


@contextlib.contextmanager
def capture_rich_output(*, stdout: Any, stderr: Any) -> Iterator[None]:
    """Temporarily redirect Rich output helpers to provided streams.

    Note: `stdout_console()` / `stderr_console()` use global Console instances,
    so `contextlib.redirect_stdout` alone will not capture Rich output.
    """

    global _STDOUT_CONSOLE, _STDERR_CONSOLE
    _ensure_consoles()

    previous_stdout = _STDOUT_CONSOLE
    previous_stderr = _STDERR_CONSOLE
    from rich.console import Console
    try:
        _STDOUT_CONSOLE = Console(file=stdout)
        _STDERR_CONSOLE = Console(file=stderr)
        yield
    finally:
        _STDOUT_CONSOLE = previous_stdout
        _STDERR_CONSOLE = previous_stderr


def show_plugin_config_panel(
    plugin_names: str | List[str],
    config: Optional[Dict[str, Any]] = None,
) -> None:
    """Show which required plugin settings are missing."""
    from rich.table import Table as RichTable
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text
    from PluginCore.registry import get_plugin_class

    if isinstance(plugin_names, str):
        plugins = [p.strip() for p in plugin_names.split(",") if p.strip()]
    else:
        plugins = [str(p).strip() for p in (plugin_names or []) if str(p).strip()]

    table = RichTable.grid(padding=(0, 1))
    table.add_column(style="bold red")
    table.add_column()

    for plugin in plugins:
        detail = "not configured"
        plugin_cls = get_plugin_class(plugin)
        if plugin_cls is None:
            detail = "not installed (.plugin -add)"
        else:
            missing_depends: List[str] = []
            try:
                missing_depends = list(plugin_cls.missing_plugin_depends() or [])
            except Exception:
                missing_depends = []
            if missing_depends:
                needed = ", ".join(missing_depends)
                detail = f"requires {needed} (.plugin -add {needed})"
                table.add_row(f" • {plugin}", detail)
                continue
            try:
                missing = plugin_cls(config or {}).missing_required_config()
            except Exception:
                missing = []
            if missing:
                parts = []
                for field in missing:
                    label = str(field.get("label") or "").strip()
                    key = str(field.get("key") or "").strip()
                    if label and key and label.lower() != key.lower():
                        parts.append(f"{label} ({key})")
                    else:
                        parts.append(label or key)
                detail = "missing: " + ", ".join(parts)
        table.add_row(f" • {plugin}", detail)

    group = Group(
        Text("The following plugins are unavailable:\n"),
        table,
        Text.from_markup(
            "\nInstall missing plugins with [bold cyan].plugin -add NAME[/bold cyan]. "
            "Set credentials with [bold cyan].config[/bold cyan]."
        ),
    )

    panel = Panel(
        group,
        title="[bold red]Configuration Required[/bold red]",
        border_style="red",
        padding=(1, 2)
    )
    
    stdout_console().print()
    stdout_console().print(panel)


def show_no_search_plugins_panel() -> None:
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    group = Group(
        Text("No plugins found for searching.\n"),
        Text.from_markup(
            "Install one with [bold cyan].plugin -available[/bold cyan] then "
            "[bold cyan].plugin -add NAME[/bold cyan], or pass "
            "[bold cyan]-plugin NAME[/bold cyan] / [bold cyan]-instance NAME[/bold cyan]."
        ),
    )
    panel = Panel(
        group,
        title="[bold yellow]No Search Plugins[/bold yellow]",
        border_style="yellow",
        padding=(1, 2),
    )
    stdout_console().print()
    stdout_console().print(panel)


def show_store_config_panel(
    store_names: str | List[str],
) -> None:
    """Show a panel when a plugin or instance is missing. Legacy name."""
    from rich.table import Table as RichTable
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    if isinstance(store_names, str):
        names = [s.strip() for s in store_names.split(",") if s.strip()]
    else:
        names = [str(s).strip() for s in (store_names or []) if str(s).strip()]

    table = RichTable.grid(padding=(0, 1))
    table.add_column(style="bold yellow")
    for name in names:
        table.add_row(f" • {name}")

    group = Group(
        Text("The following plugins or instances are not configured or available:\n"),
        table,
        Text.from_markup(
            "\nInstall with [bold cyan].plugin -add NAME[/bold cyan] "
            "and configure instances in [bold cyan].config[/bold cyan]."
        ),
    )
    panel = Panel(
        group,
        title="[bold yellow]Plugin Not Available[/bold yellow]",
        border_style="yellow",
        padding=(1, 2),
    )
    stdout_console().print()
    stdout_console().print(panel)


def show_available_plugins_panel(plugin_names: List[str]) -> None:
    """Show a Rich panel listing available/configured plugins."""
    from rich.columns import Columns
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    if not plugin_names:
        return

    # Use Columns to display them efficiently in the panel
    cols = Columns(
        [f"[bold green] \u2713 [/bold green]{p}" for p in sorted(plugin_names)],
        equal=True,
        column_first=True,
        expand=True
    )

    group = Group(
        Text("The following plugins are configured and ready to use:\n"),
        cols
    )

    panel = Panel(
        group,
        title="[bold green]Configured Plugins[/bold green]",
        border_style="green",
        padding=(1, 2)
    )
    
    stdout_console().print()
    stdout_console().print(panel)


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}


def render_image_to_console(image_path: str | Path, max_width: int | None = None) -> None:
    """Render an image to the console using Rich and half-block characters.

    Requires Pillow (PIL). If not available or loading fails, does nothing.
    """
    try:
        from PIL import Image
        from rich.color import Color
        from rich.style import Style
        from rich.text import Text

        # Ensure we expand environment variables and resolve to an absolute path.
        path = expand_path(image_path).resolve()
        if not path.exists() or not path.is_file():
            # If absolute path fails, try relative to current directory as a last resort.
            path = Path(image_path).resolve()
            if not path.exists() or not path.is_file():
                return

        with Image.open(path) as opened_img:
            img = opened_img.convert("RGB")
            orig_w, orig_h = img.size

            # Determine target dimensions
            console = stdout_console()
            if max_width is None:
                max_width = console.width - 4
            
            # Ensure max_width is at least something reasonable
            if max_width < 10:
                max_width = 80

            # Terminal cells are approx 2x taller than wide.
            # Half-block characters (upper half block) let us treat one cell as two vertical pixels.
            aspect = orig_h / orig_w
            target_w = min(orig_w, max_width)
            
            # Ensure target_h is even so we always have pairs of vertical pixels
            target_h = int(target_w * aspect)
            if target_h < 2:
                target_h = 2
            target_h = (target_h // 2) * 2

            # Ensure we don't exceed a reasonable height either (e.g. 40 characters)
            max_height_chars = 40
            if (target_h // 2) > max_height_chars:
                target_h = max_height_chars * 2
                target_w = int(target_h / aspect)
                target_h = (target_h // 2) * 2  # Re-ensure even

            if target_w < 1: target_w = 1

            img = img.resize((target_w, target_h), Image.Resampling.BILINEAR)
            pixels = img.load()
            if pixels is None:
                return

            # Render using upper half block (U+2580)
            # Each character row in terminal represents 2 pixel rows in image.
            for y in range(0, target_h - 1, 2):
                line = Text()
                for x in range(target_w):
                    rgb1 = cast(tuple, pixels[x, y])
                    rgb2 = cast(tuple, pixels[x, y + 1])
                    try:
                        r1, g1, b1 = int(rgb1[0]), int(rgb1[1]), int(rgb1[2])
                        r2, g2, b2 = int(rgb2[0]), int(rgb2[1]), int(rgb2[2])
                    except Exception:
                        r1 = g1 = b1 = r2 = g2 = b2 = 0
                    # Foreground is top pixel, background is bottom pixel
                    line.append(
                        "▀",
                        style=Style(
                            color=Color.from_rgb(r1, g1, b1),
                            bgcolor=Color.from_rgb(r2, g2, b2),
                        ),
                    )
                console.print(line)

    except Exception:
        # Emit logs to help diagnose rendering failures (PIL missing, corrupt file, terminal limitations)
        from SYS.logger import logger
        logger.exception("Failed to render image to console: %s", image_path)
        return


def render_item_details_panel(item: Dict[str, Any], *, title: Optional[str] = None) -> None:
    """Render a comprehensive details panel for a result item using unified ItemDetailView."""
    from SYS.result_table import ItemDetailView, extract_item_metadata
    
    metadata = extract_item_metadata(item)
    
    # Create a specialized view with no results rows (only the metadata panel)
    # We set no_choice=True to hide the "#" column (not that there are any rows).
    view = ItemDetailView(item_metadata=metadata, detail_title=title)._interactive(True)
    # Ensure no title leaks in (prevents an empty "No results" table from rendering).
    try:
        view.title = ""
        view.header_lines = []
    except Exception:
        from SYS.logger import logger
        logger.exception("Failed to sanitize ItemDetailView title/header before printing")
    
    # We want to print ONLY the elements from ItemDetailView, so we don't use stdout_console().print(view)
    # as that would include the (empty) results panel.
    # Actually, let's just use to_rich and print it.
    
    stdout_console().print()
    stdout_console().print(view.to_rich())


