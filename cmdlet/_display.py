"""UI/display helpers for Rich output, saved-output panels, and result persistence.

Contains utilities for:
- Printing to stderr safely without disrupting Rich Live display
- Rendering saved output panels when ``-path`` is used
- Persisting items for ``@N`` selection across command boundaries
- Formatting byte sizes for display
- Moving emitted temp/PATH artifacts to user-specified destinations
"""

from __future__ import annotations

import shutil
import sys
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from SYS.logger import log
from SYS.result_publication import publish_result_table
from SYS.result_table import Table
from SYS.rich_display import stderr_console as get_stderr_console
from SYS.command_parsing import extract_arg_value
from SYS.utils import format_bytes, unique_path
from ._pipeobject_utils import get_field, get_pipe_object_path

__all__ = [
    "_print_live_safe_stderr",
    "_print_saved_output_panel",
    "_apply_saved_path_update",
    "_unique_destination_path",
    "_extract_flag_value",
    "apply_output_path_from_pipeobjects",
    "display_and_persist_items",
    "fmt_bytes",
]


def fmt_bytes(n: Optional[int]) -> str:
    if n is None or n < 0:
        return "unknown"
    return format_bytes(n)


def _extract_flag_value(args: Sequence[str], *flags: str) -> Optional[str]:
    return extract_arg_value(args, flags=flags, reject_flag_values=True)


def _unique_destination_path(dest: Path) -> Path:
    return unique_path(dest)


def _print_live_safe_stderr(message: str) -> None:
    """Print to stderr without breaking Rich Live progress output."""
    try:
        from SYS.rich_display import stderr_console  # type: ignore
    except Exception:
        return

    cm: AbstractContextManager[Any] | None = None
    try:
        from SYS import pipeline as _pipeline_ctx  # type: ignore

        suspend = getattr(_pipeline_ctx, "suspend_live_progress", None)
        candidate = suspend() if callable(suspend) else None
        if isinstance(candidate, AbstractContextManager):
            cm = candidate
        elif candidate is not None and hasattr(candidate, "__enter__") and hasattr(candidate, "__exit__"):
            cm = candidate  # type: ignore[arg-type]
    except Exception:
        cm = None

    if cm is None:
        cm = nullcontext()

    try:
        console = stderr_console()
        print_func = getattr(console, "print", None)
    except Exception:
        return

    if not callable(print_func):
        return

    try:
        with cm:
            print_func(str(message))
    except Exception:
        return


def _print_saved_output_panel(item: Any, final_path: Path) -> None:
    """When -path is used, print a Rich panel summarizing the saved output.

    Shows: Title, Location, Hash.
    Best-effort: reads existing fields first to avoid recomputing hashes.
    """
    try:
        from rich.panel import Panel  # type: ignore
        from rich.table import Table as RichTable  # type: ignore
        from SYS.rich_display import stderr_console  # type: ignore
    except Exception:
        return

    cm: AbstractContextManager[Any] | None = None
    try:
        from SYS import pipeline as _pipeline_ctx  # type: ignore

        suspend = getattr(_pipeline_ctx, "suspend_live_progress", None)
        cm_candidate = suspend() if callable(suspend) else None
        if isinstance(cm_candidate, AbstractContextManager):
            cm = cm_candidate
        elif cm_candidate is not None and hasattr(cm_candidate, "__enter__") and hasattr(cm_candidate, "__exit__"):
            cm = cm_candidate  # type: ignore[arg-type]
    except Exception:
        cm = None

    if cm is None:
        cm = nullcontext()

    try:
        location = str(final_path)
    except Exception:
        location = ""

    title = ""
    try:
        title = str(get_field(item, "title") or get_field(item, "name") or "").strip()
    except Exception:
        title = ""
    if not title:
        try:
            title = str(final_path.stem or final_path.name)
        except Exception:
            title = ""

    file_hash = ""
    try:
        file_hash = str(get_field(item, "hash") or get_field(item, "sha256") or "").strip()
    except Exception:
        file_hash = ""
    if not file_hash:
        try:
            from SYS.utils import sha256_file  # type: ignore

            file_hash = str(sha256_file(final_path) or "").strip()
        except Exception:
            file_hash = ""

    grid = RichTable.grid(padding=(0, 1))
    grid.add_column(justify="right", style="bold")
    grid.add_column()
    grid.add_row("Title", title or "(unknown)")
    grid.add_row("Location", location or "(unknown)")
    grid.add_row("Hash", file_hash or "(unknown)")

    try:
        console = stderr_console()
        print_func = getattr(console, "print", None)
    except Exception:
        return

    if not callable(print_func):
        return

    try:
        with cm:
            print_func(Panel(grid, title="Saved", expand=False))
    except Exception:
        return


def _apply_saved_path_update(item: Any, *, old_path: str, new_path: str) -> None:
    """Update a PipeObject-like item after its backing file has moved."""
    old_str = str(old_path)
    new_str = str(new_path)
    if isinstance(item, dict):
        try:
            if str(item.get("path") or "") == old_str:
                item["path"] = new_str
        except Exception:
            pass
        try:
            if str(item.get("target") or "") == old_str:
                item["target"] = new_str
        except Exception:
            pass
        try:
            extra = item.get("extra")
            if isinstance(extra, dict):
                if str(extra.get("target") or "") == old_str:
                    extra["target"] = new_str
                if str(extra.get("path") or "") == old_str:
                    extra["path"] = new_str
        except Exception:
            pass
        return

    try:
        if getattr(item, "path", None) == old_str:
            setattr(item, "path", new_str)
    except Exception:
        pass
    try:
        extra = getattr(item, "extra", None)
        if isinstance(extra, dict):
            if str(extra.get("target") or "") == old_str:
                extra["target"] = new_str
            if str(extra.get("path") or "") == old_str:
                extra["path"] = new_str
    except Exception:
        pass


def apply_output_path_from_pipeobjects(
    *,
    cmd_name: str,
    args: Sequence[str],
    emits: Sequence[Any],
) -> List[Any]:
    """If the user supplied `-path`, move emitted temp/PATH files there.

    This enables a dynamic pattern:
    - Any cmdlet can include `SharedArgs.PATH`.
    - If it emits a file-backed PipeObject (`path` exists on disk) and the item is
      a temp/PATH artifact, then `-path <dest>` will save it to that location.

    Rules:
    - Only affects items whose `action` matches the current cmdlet.
    - Only affects items that look like local artifacts (`is_temp` True or `store` == PATH).
    - Updates the emitted object's `path` (and `target` when it points at the same file).
    """
    dest_raw = _extract_flag_value(args, "-path", "--path")
    if not dest_raw:
        return list(emits or [])

    try:
        dest_str = str(dest_raw).strip()
        if "://" in dest_str:
            _print_live_safe_stderr(
                f"Ignoring -path value that looks like a URL: {dest_str}"
            )
            return list(emits or [])
    except Exception:
        pass

    cmd_norm = str(cmd_name or "").replace("_", "-").strip().lower()
    if not cmd_norm:
        return list(emits or [])

    try:
        dest_hint_dir = str(dest_raw).endswith(("/", "\\"))
    except Exception:
        dest_hint_dir = False

    try:
        dest_path = Path(str(dest_raw)).expanduser()
    except Exception:
        return list(emits or [])

    items = list(emits or [])
    artifact_indices: List[int] = []
    artifact_paths: List[Path] = []
    for idx, item in enumerate(items):
        action = str(get_field(item, "action", "") or "").strip().lower()
        if not action.startswith("cmdlet:"):
            continue
        action_name = action.split(":", 1)[-1].strip().lower()
        if action_name != cmd_norm:
            continue

        store = str(get_field(item, "store", "") or "").strip().lower()
        is_temp = bool(get_field(item, "is_temp", False))
        if not (is_temp or store == "path"):
            continue

        src_str = get_pipe_object_path(item)
        if not src_str:
            continue
        try:
            src = Path(str(src_str)).expanduser()
        except Exception:
            continue
        try:
            if not src.exists() or not src.is_file():
                continue
        except Exception:
            continue

        artifact_indices.append(idx)
        artifact_paths.append(src)

    if not artifact_indices:
        return items

    if len(artifact_indices) > 1:
        if dest_path.suffix:
            dest_dir = dest_path.parent
        else:
            dest_dir = dest_path
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            _print_live_safe_stderr(
                f"Failed to create destination directory: {dest_dir} ({exc})"
            )
            return items

        for idx, src in zip(artifact_indices, artifact_paths):
            final = dest_dir / src.name
            final = _unique_destination_path(final)
            try:
                if src.resolve() == final.resolve():
                    _apply_saved_path_update(
                        items[idx],
                        old_path=str(src),
                        new_path=str(final),
                    )
                    _print_saved_output_panel(items[idx], final)
                    continue
            except Exception:
                pass
            try:
                shutil.move(str(src), str(final))
            except Exception as exc:
                _print_live_safe_stderr(f"Failed to save output to {final}: {exc}")
                continue
            _apply_saved_path_update(items[idx], old_path=str(src), new_path=str(final))
            _print_saved_output_panel(items[idx], final)

        return items

    src = artifact_paths[0]
    idx = artifact_indices[0]
    final: Path
    try:
        if dest_hint_dir or (dest_path.exists() and dest_path.is_dir()):
            final = dest_path / src.name
        else:
            final = dest_path
    except Exception:
        final = dest_path

    try:
        final.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        _print_live_safe_stderr(
            f"Failed to create destination directory: {final.parent} ({exc})"
        )
        return items

    final = _unique_destination_path(final)
    try:
        if src.resolve() != final.resolve():
            shutil.move(str(src), str(final))
    except Exception as exc:
        _print_live_safe_stderr(f"Failed to save output to {final}: {exc}")
        return items

    _apply_saved_path_update(items[idx], old_path=str(src), new_path=str(final))
    _print_saved_output_panel(items[idx], final)
    return items


def display_and_persist_items(
    items: List[Any],
    title: str = "Result",
    subject: Optional[Any] = None,
    display_type: str = "item",
    table: Optional[Table] = None,
) -> None:
    """Display detail panels and persist items for @N selection.

    This helper function:
    1. Renders individual detail panels for each item
    2. Creates a result table with all items (or uses provided table)
    3. Persists the table so @N selection works across command boundaries

    Args:
        items: List of items/dicts to display and make selectable
        title: Title for the result table (default: "Result")
        subject: Optional subject to associate with the results (usually first item or list)
        display_type: Type of display - "item" (default), "tag", or "custom" (no panels)
        table: Optional pre-built Table object (if None, creates new Table with title)
    """
    if not items:
        return

    try:
        from SYS import pipeline as pipeline_context

        if table is None:
            table = Table(title=title)

        shown = False
        if display_type != "custom":
            try:
                from SYS.rich_display import render_item_details_panel

                panel_prefix = "Tag" if display_type == "tag" else "Item"

                for idx, item in enumerate(items, 1):
                    try:
                        render_item_details_panel(item, title=f"#{idx} {panel_prefix} Details")
                        shown = True
                    except Exception:
                        pass
            except Exception:
                pass

        if not getattr(table, "_items_added", False):
            for item in items:
                table.add_result(item)

        publish_result_table(pipeline_context, table, items, subject=subject)
        if not shown:
            try:
                from SYS.rich_display import stdout_console

                stdout_console().print(table)
                shown = True
            except Exception:
                pass
        setattr(table, "_rendered_by_cmdlet", shown)
    except Exception:
        pass
