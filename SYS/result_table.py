"""Unified result table formatter for CLI display.

Provides a structured way to convert search results, metadata, and pipeline objects
into formatted tables suitable for display in the REPL and CLI output.

Features:
- Format results as aligned tables with row numbers
- Support multiple selection formats (single, ranges, lists, combined)
- Interactive selection with user input
- Input options for cmdlet arguments (location, source selection, etc)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable, Set, Tuple
from pathlib import Path
from urllib.parse import urlparse
import json
import re

# rich imports are deferred to avoid ~100ms startup cost.
# All rich types are only needed inside method bodies, so we lazily import on first use.
_rich_mod: Any = None


def _rich():
    global _rich_mod
    if _rich_mod is None:
        import types as _types
        _m = _types.SimpleNamespace()
        from rich.box import SIMPLE as _SIMPLE
        from rich.console import Group as _Group
        from rich.panel import Panel as _Panel
        from rich.prompt import Prompt as _Prompt
        from rich.table import Table as _RichTable
        from rich.text import Text as _Text
        _m.SIMPLE = _SIMPLE
        _m.Group = _Group
        _m.Panel = _Panel
        _m.Prompt = _Prompt
        _m.RichTable = _RichTable
        _m.Text = _Text
        _rich_mod = _m
    return _rich_mod



# Reuse the existing format_bytes helper under a clearer alias
from SYS.utils import format_bytes as format_mb

import logging
logger = logging.getLogger(__name__)


def _normalize_detail_tags(tags: Any) -> List[str]:
    if not tags:
        return []
    if isinstance(tags, str):
        source = [part.strip() for part in tags.split(",")]
    elif isinstance(tags, (list, tuple, set)):
        source = [str(part or "").strip() for part in tags]
    else:
        source = [str(tags).strip()]

    seen: set[str] = set()
    normalized: List[str] = []
    for tag in source:
        text = str(tag or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _tag_namespace_link(namespace: str, value: str) -> Optional[str]:
    ns = str(namespace or "").strip().casefold()
    val = str(value or "").strip()
    if not ns or not val:
        return None
    if ns in {"openlibrary", "ol", "olid"}:
        return f"https://openlibrary.org/{val}"
    if ns in {"isbn", "isbn_13", "isbn_10"}:
        cleaned = re.sub(r"[^0-9Xx]", "", val)
        if cleaned:
            return f"https://openlibrary.org/isbn/{cleaned}"
    if ns == "musicbrainz":
        return f"https://musicbrainz.org/recording/{val}"
    return None


def _partition_detail_tags(tags: Any) -> tuple[List[str], List[str]]:
    normalized = _normalize_detail_tags(tags)
    namespace_tags: List[str] = []
    freeform_tags: List[str] = []
    for tag in normalized:
        namespace, sep, value = str(tag).partition(":")
        if sep and namespace.strip() and value.strip():
            namespace_tags.append(tag)
        else:
            freeform_tags.append(tag)

    namespace_tags.sort(
        key=lambda value: (
            str(value).partition(":")[0].casefold(),
            str(value).partition(":")[2].casefold(),
            str(value).casefold(),
        )
    )
    freeform_tags.sort(key=lambda value: str(value).casefold())
    return namespace_tags, freeform_tags


def _extract_namespace_sort_values(tags: Any, namespace: str) -> List[str]:
    wanted = str(namespace or "").strip().casefold()
    if not wanted:
        return []

    values: List[str] = []
    for tag in _normalize_detail_tags(tags):
        ns, sep, value = str(tag).partition(":")
        if not sep:
            continue
        if ns.strip().casefold() != wanted:
            continue
        clean_value = value.strip()
        if clean_value:
            values.append(clean_value)
    return values


def _namespace_sort_key(tags: Any, namespace: str, *, numeric: bool = False) -> Tuple[int, Any, str]:
    values = _extract_namespace_sort_values(tags, namespace)
    if not values:
        return (1, float("inf") if numeric else "", "")

    primary = values[0]
    if numeric:
        match = re.search(r"-?\d+(?:\.\d+)?", primary)
        if match:
            try:
                return (0, float(match.group(0)), primary.casefold())
            except Exception:
                pass
        return (0, float("inf"), primary.casefold())

    return (0, primary.casefold(), primary.casefold())


def _chunk_detail_tags(tags: List[str], columns: int) -> List[List[str]]:
    column_count = max(1, int(columns or 1))
    rows: List[List[str]] = []
    for index in range(0, len(tags), column_count):
        rows.append(tags[index:index + column_count])
    return rows


_TABLE_THEMES: Dict[str, Dict[str, Any]] = {
    "rainbow": {
        "header": "bold #000000 on #ffffff",
        "border": "#000000 on #ffffff",
        "panel": "on #ffffff",
        "rows": [
            ("#ff0000", "#8f00ff"),
            ("#ffa500", "#800080"),
            ("#ffff00", "#0000ff"),
            ("#808080", "#008000"),
            ("#008000", "#808080"),
            ("#0000ff", "#ffff00"),
            ("#800080", "#ffa500"),
            ("#8f00ff", "#ff0000"),
            ("#ffffff", "#000000"),
        ],
    },
    "plain": {
        "header": "bold",
        "border": "",
        "panel": "",
        "rows": [],
    },
    "bw-striped": {
        "header": "bold #000000 on #ffffff",
        "border": "#000000 on #ffffff",
        "panel": "on #ffffff",
        "rows": [("#000000", "#ffffff"), ("#ffffff", "#000000")],
    },
    "dim-striped": {
        "header": "bold",
        "border": "dim",
        "panel": "",
        "rows": [("#d0d0d0", "#1c1c1c"), ("#e8e8e8", "#2a2a2a")],
    },
    "nord": {
        "header": "bold #eceff4 on #3b4252",
        "border": "#81a1c1",
        "panel": "",
        "rows": [("#eceff4", "#2e3440"), ("#d8dee9", "#3b4252")],
    },
    "dracula": {
        "header": "bold #f8f8f2 on #44475a",
        "border": "#bd93f9",
        "panel": "",
        "rows": [("#f8f8f2", "#282a36"), ("#f8f8f2", "#44475a")],
    },
    "solarized-dark": {
        "header": "bold #fdf6e3 on #073642",
        "border": "#268bd2",
        "panel": "",
        "rows": [("#839496", "#002b36"), ("#93a1a1", "#073642")],
    },
    "matrix": {
        "header": "bold #00ff41 on #003b00",
        "border": "#00ff41",
        "panel": "",
        "rows": [("#00ff41", "#0d0d0d"), ("#00cc33", "#001a00")],
    },
    "ocean": {
        "header": "bold #e0f7fa on #006064",
        "border": "#4dd0e1",
        "panel": "",
        "rows": [("#e0f7fa", "#004d40"), ("#b2ebf2", "#00695c")],
    },
    "high-contrast": {
        "header": "bold #ffff00 on #000000",
        "border": "#ffffff",
        "panel": "",
        "rows": [("#ffffff", "#000000"), ("#000000", "#ffff00")],
    },
}

_TABLE_BOX_NAMES: Dict[str, Optional[str]] = {
    "none": None,
    "off": None,
    "simple": "SIMPLE",
    "simple-head": "SIMPLE_HEAD",
    "simple-heavy": "SIMPLE_HEAVY",
    "rounded": "ROUNDED",
    "square": "SQUARE",
    "heavy": "HEAVY",
    "heavy-head": "HEAVY_HEAD",
    "double": "DOUBLE",
    "ascii": "ASCII",
    "minimal": "MINIMAL",
    "minimal-heavy": "MINIMAL_HEAVY_HEAD",
    "markdown": "MARKDOWN",
}

RESULT_TABLE_HEADER_STYLE = "bold #000000 on #ffffff"
RESULT_TABLE_BORDER_STYLE = "#000000 on #ffffff"
RESULT_TABLE_PLAIN_HEADER_STYLE = "bold"

_RESULT_TABLE_APPEARANCE_ALIASES: Dict[str, str] = {
    "": "rainbow",
    "rainbow": "rainbow",
    "default": "rainbow",
    "plain": "plain",
    "none": "plain",
    "bw": "bw-striped",
    "b-w": "bw-striped",
    "b-w-striped": "bw-striped",
    "bw-striped": "bw-striped",
    "b-w-stripes": "bw-striped",
    "bw-stripes": "bw-striped",
    "black-white": "bw-striped",
    "black-white-striped": "bw-striped",
    "black-white-stripes": "bw-striped",
    "black-and-white": "bw-striped",
    "black-and-white-striped": "bw-striped",
    "black-and-white-stripes": "bw-striped",
    "dim": "dim-striped",
    "dim-striped": "dim-striped",
    "dim-stripes": "dim-striped",
    "zebra": "dim-striped",
    "nord": "nord",
    "dracula": "dracula",
    "solarized": "solarized-dark",
    "solarized-dark": "solarized-dark",
    "matrix": "matrix",
    "ocean": "ocean",
    "high-contrast": "high-contrast",
    "highcontrast": "high-contrast",
}


def normalize_result_table_appearance_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "rainbow"

    collapsed = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if collapsed in _TABLE_THEMES:
        return collapsed
    return _RESULT_TABLE_APPEARANCE_ALIASES.get(collapsed, "rainbow")


def get_result_table_appearance_mode(config: Optional[Dict[str, Any]] = None) -> str:
    cfg = _display_config(config)
    return normalize_result_table_appearance_mode(cfg.get("table_appearance"))


def _parse_custom_row_styles(raw: Any) -> List[tuple[str, str]]:
    value = raw
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except Exception:
            return []
    if not isinstance(value, list):
        return []
    out: List[tuple[str, str]] = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            fg = str(item[0] or "").strip() or "default"
            bg = str(item[1] or "").strip() or "default"
            out.append((fg, bg))
            continue
        text = str(item or "").strip()
        if " on " in text:
            fg, bg = text.split(" on ", 1)
            out.append((fg.strip() or "default", bg.strip() or "default"))
    return out


def resolve_table_theme(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = _display_config(config)
    mode = get_result_table_appearance_mode(cfg)
    theme = dict(_TABLE_THEMES.get(mode) or _TABLE_THEMES["rainbow"])
    header = str(cfg.get("theme_header_style") or "").strip()
    border = str(cfg.get("theme_border_style") or "").strip()
    panel = str(cfg.get("theme_panel_style") or "").strip()
    rows = _parse_custom_row_styles(cfg.get("theme_row_styles"))
    if header:
        theme["header"] = header
    if border:
        theme["border"] = border
    if panel:
        theme["panel"] = panel
    if rows:
        theme["rows"] = rows
    return theme


def get_result_table_header_style(config: Optional[Dict[str, Any]] = None) -> str:
    return str(resolve_table_theme(config).get("header") or RESULT_TABLE_PLAIN_HEADER_STYLE)


def get_result_table_border_style(config: Optional[Dict[str, Any]] = None) -> str:
    return str(resolve_table_theme(config).get("border") or "")


def get_result_table_panel_style(config: Optional[Dict[str, Any]] = None) -> str:
    return str(resolve_table_theme(config).get("panel") or "")


def theme_styles(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    theme = resolve_table_theme(config)
    rows = theme.get("rows") or []
    value_fg = str(rows[0][0]).strip() if rows else "default"
    border = str(theme.get("border") or "").strip()
    accent = border.split(" on ", 1)[0].strip() or value_fg or "cyan"
    return {
        "header": str(theme.get("header") or "bold"),
        "border": border,
        "panel": str(theme.get("panel") or ""),
        "accent": accent,
        "value": value_fg or "default",
        "box": get_result_panel_box(config),
        "table_box": get_result_table_box(config),
        "show_lines": get_result_table_show_lines(config),
        "expand": get_result_table_expand(config),
    }


def themed_panel(
    renderable: Any,
    *,
    title: Optional[str] = None,
    expand: Optional[bool] = None,
    padding: Tuple[int, int] = (0, 0),
    config: Optional[Dict[str, Any]] = None,
) -> Any:
    styles = theme_styles(config)
    title_render = _rich().Text(str(title), style=styles["header"]) if title else None
    return _rich().Panel(
        renderable,
        title=title_render,
        border_style=styles["border"],
        box=styles["box"],
        style=styles["panel"],
        expand=bool(styles["expand"] if expand is None else expand),
        padding=padding,
    )


def get_result_table_row_style(
    row_index: int,
    appearance_mode: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    theme = resolve_table_theme(config)
    if appearance_mode:
        base = dict(_TABLE_THEMES.get(appearance_mode) or theme)
        custom_rows = theme.get("rows")
        theme = base
        if custom_rows:
            theme["rows"] = custom_rows
    rows = theme.get("rows") or []
    if not rows:
        return ""
    text_color, bg_color = rows[row_index % len(rows)]
    return f"{text_color} on {bg_color}"


def _display_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if isinstance(config, dict):
        return config
    try:
        from SYS.config import load_config

        return load_config(emit_summary=False) or {}
    except Exception:
        return {}


def _normalize_box_key(value: Any, default: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return text or default


def resolve_rich_box(value: Any, *, default: str = "none") -> Any:
    key = _normalize_box_key(value, default)
    attr = _TABLE_BOX_NAMES.get(key)
    if attr is None:
        if key in _TABLE_BOX_NAMES:
            return None
        attr = _TABLE_BOX_NAMES.get(default)
        if attr is None:
            return None
    try:
        import rich.box as box

        return getattr(box, attr, None)
    except Exception:
        return None


def get_result_table_box(config: Optional[Dict[str, Any]] = None) -> Any:
    cfg = _display_config(config)
    return resolve_rich_box(cfg.get("table_box"), default="none")


def get_result_panel_box(config: Optional[Dict[str, Any]] = None) -> Any:
    cfg = _display_config(config)
    box_obj = resolve_rich_box(cfg.get("panel_box"), default="rounded")
    if box_obj is None:
        return resolve_rich_box("rounded", default="rounded")
    return box_obj


def get_result_table_show_lines(config: Optional[Dict[str, Any]] = None) -> bool:
    from SYS.utils import coerce_bool

    return coerce_bool(_display_config(config).get("table_show_lines"), False)


def get_result_table_expand(config: Optional[Dict[str, Any]] = None) -> bool:
    from SYS.utils import coerce_bool

    return coerce_bool(_display_config(config).get("table_expand"), False)


def get_result_table_header_case(config: Optional[Dict[str, Any]] = None) -> str:
    text = str(_display_config(config).get("table_header_case") or "upper").strip().lower()
    if text in {"title", "as-is", "asis", "preserve"}:
        return "title" if text == "title" else "as-is"
    return "upper"


def format_result_table_header(name: str, config: Optional[Dict[str, Any]] = None) -> str:
    text = str(name or "")
    mode = get_result_table_header_case(config)
    if mode == "title":
        return text.replace("_", " ").title()
    if mode == "as-is":
        return text
    return text.upper()


def apply_result_table_layout(table: Any) -> None:
    """Apply compact, flush column layout options to a Rich table."""
    table.padding = (0, 1)
    if hasattr(table, "pad_edge"):
        table.pad_edge = False
    if hasattr(table, "collapse_padding"):
        table.collapse_padding = True


def _sanitize_cell_text(value: Any) -> str:
    """Coerce to a single-line, tab-free string suitable for terminal display."""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    return text.replace("\r\n",
                        " ").replace("\n",
                                     " ").replace("\r",
                                                  " ").replace("\t",
                                                               " ")


def _format_duration_hms(duration: Any) -> str:
    """Format a duration in seconds into a compact h/m/s string.

    Examples:
        3150 -> "52m30s"
        59 -> "59s"
        3600 -> "1h0m0s"

    If the value is not numeric, returns an empty string.
    """
    if duration is None:
        return ""
    try:
        if isinstance(duration, str):
            s = duration.strip()
            if not s:
                return ""
            # If it's already formatted (contains letters/colon), leave it to caller.
            if any(ch.isalpha() for ch in s) or ":" in s:
                return ""
            seconds = float(s)
        else:
            seconds = float(duration)
    except Exception:
        logger.debug("Failed to format duration '%s' to hms", duration, exc_info=True)
        return ""

    if seconds < 0:
        return ""

    total_seconds = int(seconds)
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)

    parts: List[str] = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return "".join(parts)


@dataclass(frozen=True)
class TableColumn:
    """Reusable column specification.

    This is intentionally separate from `ResultColumn`:
    - `ResultColumn` is a rendered (name,value) pair attached to a single row.
    - `TableColumn` is a reusable extractor/formatter used to build rows consistently
      across cmdlets and stores.
    """

    key: str
    header: str
    extractor: Callable[[Any], Any]

    def extract(self, item: Any) -> Any:
        try:
            return self.extractor(item)
        except Exception:
            logger.exception("TableColumn.extract failed for key '%s'", self.key)
            return None


def _get_first_dict_value(data: Dict[str, Any], keys: List[str]) -> Any:
    for k in keys:
        if k in data:
            v = data.get(k)
            if v is not None and str(v).strip() != "":
                return v
    return None


def _as_dict(item: Any) -> Optional[Dict[str, Any]]:
    """Convert any object to dictionary representation.
    
    Handles:
    - Dict objects (returned as-is)
    - Objects with __dict__ attribute (converted to dict)
    - None or conversion failures (returns None)
    
    Args:
        item: Object to convert (dict, dataclass, object, etc.)
        
    Returns:
        Dictionary representation or None if conversion fails
    """
    if isinstance(item, dict):
        return item
    try:
        if hasattr(item, "__dict__"):
            return dict(getattr(item, "__dict__"))
    except Exception:
        logger.exception("Failed to convert %s to dict in _as_dict", type(item))
        return None
    return None


def extract_store_value(item: Any) -> str:
    """Extract storage backend name from item.

    Searches item for store identifier using field names:
    store, table.

    Args:
        item: Object or dict with store information

    Returns:
        Store name as string (e.g., "hydrus", "local", "") if not found
    """
    data = _as_dict(item) or {}
    instance = _get_first_dict_value(data, ["instance"])
    if instance:
        return str(instance).strip()
    store = _get_first_dict_value(
        data,
        ["store",
         "table"]
    )
    return str(store or "").strip()


def extract_hash_value(item: Any) -> str:
    data = _as_dict(item) or {}
    hv = _get_first_dict_value(data, ["hash", "hash_hex", "file_hash", "sha256"])
    return str(hv or "").strip()


def extract_title_value(item: Any) -> str:
    data = _as_dict(item) or {}
    if not isinstance(data, dict):
        data = {}
    title = _get_first_dict_value(data, ["title", "name", "filename"])
    if not title:
        title = _get_first_dict_value(
            data,
            ["target",
             "path",
             "url"]
        )  # last resort display
    return str(title or "").strip()


def extract_ext_value(item: Any) -> str:
    data = _as_dict(item) or {}
    if not isinstance(data, dict):
        data = {}

    _md = data.get("metadata")
    meta: Dict[str, Any] = _md if isinstance(_md, dict) else {}
    raw_path = data.get("path") or data.get("target") or data.get(
        "filename"
    ) or data.get("title")

    ext = _get_first_dict_value(data,
                                ["ext",
                                 "file_ext",
                                 "extension"]) or _get_first_dict_value(
                                     meta,
                                     ["ext",
                                      "file_ext",
                                      "extension"]
                                 )

    if (not ext) and raw_path:
        try:
            suf = Path(str(raw_path)).suffix
            if suf:
                ext = suf.lstrip(".")
        except Exception:
            logger.debug("Failed to extract suffix from raw_path: %r", raw_path, exc_info=True)
            ext = ""

    ext_str = str(ext or "").strip().lstrip(".")
    for idx, ch in enumerate(ext_str):
        if not ch.isalnum():
            ext_str = ext_str[:idx]
            break
    return ext_str[:5]


def extract_size_bytes_value(item: Any) -> Optional[int]:
    data = _as_dict(item) or {}
    if not isinstance(data, dict):
        data = {}
    _md = data.get("metadata")
    meta: Dict[str, Any] = _md if isinstance(_md, dict) else {}

    size_val = _get_first_dict_value(
        data,
        ["size_bytes",
         "size",
         "file_size",
         "bytes",
         "filesize"]
    ) or _get_first_dict_value(
        meta,
        ["size_bytes",
         "size",
         "file_size",
         "bytes",
         "filesize"]
    )
    if size_val is None:
        return None
    try:
        s = str(size_val).strip()
        if not s:
            return None
        # Some sources might provide floats or numeric strings
        return int(float(s))
    except Exception:
        logger.debug("Failed to parse size value '%r' to int", size_val, exc_info=True)
        return None


COMMON_COLUMNS: Dict[str,
                     TableColumn] = {
                         "title": TableColumn("title",
                                              "Title",
                                              extract_title_value),
                         "store": TableColumn("store",
                                              "Store",
                                              extract_store_value),
                         "hash": TableColumn("hash",
                                             "Hash",
                                             extract_hash_value),
                         "ext": TableColumn("ext",
                                            "Ext",
                                            extract_ext_value),
                         "size": TableColumn("size",
                                             "Size",
                                             extract_size_bytes_value),
                     }


def build_display_row(item: Any, *, keys: List[str]) -> Dict[str, Any]:
    """Build a dict suitable for `ResultTable.add_result()` using shared column specs."""
    out: Dict[str,
              Any] = {}
    for k in keys:
        spec = COMMON_COLUMNS.get(k)
        if spec is None:
            continue
        val = spec.extract(item)
        out[spec.key] = val
    return out


@dataclass
class InputOption:
    """Represents an interactive input option (cmdlet argument) in a table.

    Allows users to select options that translate to cmdlet arguments,
    enabling interactive configuration right from the result table.

    Example:
        # Create an option for location selection
        location_opt = InputOption(
            "location",
            type="enum",
            choices=["local", "hydrus", "0x0"],
            description="Download destination"
        )

        # Use in result table
        table.add_input_option(location_opt)
        selected = table.select_option("location")  # Returns user choice
    """

    name: str
    """Option name (maps to cmdlet argument)"""
    type: str = "string"
    """Option type: 'string', 'enum', 'flag', 'integer'"""
    choices: List[str] = field(default_factory=list)
    """Valid choices for enum type"""
    default: Optional[str] = None
    """Default value if not specified"""
    description: str = ""
    """Description of what this option does"""
    validator: Optional[Callable[[str], bool]] = None
    """Optional validator function: takes value, returns True if valid"""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "type": self.type,
            "choices": self.choices if self.choices else None,
            "default": self.default,
            "description": self.description,
        }



@dataclass
class Column:
    """Represents a single column in a result table."""

    name: str
    value: str
    width: Optional[int] = None

    def __str__(self) -> str:
        """String representation of the column."""
        return f"{self.name}: {self.value}"

    def to_dict(self) -> Dict[str, str]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "value": self.value
        }


@dataclass
class Row:
    """Represents a single row in a result table."""

    columns: List[Column] = field(default_factory=list)
    selection_args: Optional[List[str]] = None
    """Arguments to use for this row when selected via @N syntax (e.g., ['-item', '3'])"""
    selection_action: Optional[List[str]] = None
    """Full expanded stage tokens that should run when this row is selected."""
    source_index: Optional[int] = None
    """Original insertion order index (used to map sorted views back to source items)."""
    payload: Optional[Any] = None
    """Original object that contributed to this row."""

    def add_column(self, name: str, value: Any) -> None:
        """Add a column to this row."""
        # Normalize column header names.
        normalized_name = str(name or "").strip()
        if normalized_name.lower() == "name":
            normalized_name = "Title"

        str_value = _sanitize_cell_text(value)

        # Normalize extension columns globally and cap to 5 characters
        if normalized_name.lower() == "ext":
            str_value = str_value.strip().lstrip(".")
            for idx, ch in enumerate(str_value):
                if not ch.isalnum():
                    str_value = str_value[:idx]
                    break
            str_value = str_value[:5]

        # Normalize Duration columns: providers often pass raw seconds.
        if normalized_name.lower() == "duration":
            formatted = _format_duration_hms(value)
            if formatted:
                str_value = formatted

        self.columns.append(Column(normalized_name, str_value))

    def get_column(self, name: str) -> Optional[str]:
        """Get column value by name."""
        for col in self.columns:
            if col.name.lower() == name.lower():
                return col.value
        return None

    def to_dict(self) -> List[Dict[str, str]]:
        """Convert to list of column dicts."""
        return [col.to_dict() for col in self.columns]

    def to_list(self) -> List[tuple[str, str]]:
        """Convert to list of (name, value) tuples."""
        return [(col.name, col.value) for col in self.columns]

    def __str__(self) -> str:
        """String representation of the row."""
        return " | ".join(str(col) for col in self.columns)


class Table:
    """Unified table formatter for search results, metadata, and pipeline objects.

    Provides a structured way to display results in the CLI with consistent formatting.
    Handles conversion from various result types (SearchResult, PipeObject, dicts) into
    a formatted table with rows and columns.

    Example:
        >>> result_table = ResultTable("Search Results")
        >>> row = result_table.add_row()
        >>> row.add_column("File", "document.pdf")
        >>> row.add_column("Size", "2.5 MB")
        >>> row.add_column("Tag", "pdf, document")
        >>> print(result_table)
    """

    def __init__(
        self,
        title: str = "",
        title_width: int = 80,
        max_columns: Optional[int] = None,
        preserve_order: bool = False,
    ):
        """Initialize a result table.

        Args:
            title: Optional title for the table
            title_width: Width for formatting the title line
            max_columns: Maximum number of columns to display (None for unlimited, default: 5 for search results)
            preserve_order: When True, skip automatic sorting so row order matches source
        """
        self.title = title
        try:
            from SYS import pipeline as ctx

            cmdlet_name = ""
            try:
                cmdlet_name = (
                    ctx.get_current_cmdlet_name("")
                    if hasattr(ctx,
                               "get_current_cmdlet_name") else ""
                )
            except Exception:
                logger.debug("Failed to get current cmdlet name from pipeline context", exc_info=True)
                cmdlet_name = ""

            stage_text = ""
            try:
                stage_text = (
                    ctx.get_current_stage_text("")
                    if hasattr(ctx,
                               "get_current_stage_text") else ""
                )
            except Exception:
                logger.debug("Failed to get current stage text from pipeline context", exc_info=True)
                stage_text = ""

            if cmdlet_name and stage_text:
                normalized_cmd = str(cmdlet_name).replace("_", "-").strip().lower()
                normalized_title = str(self.title or "").strip().lower()
                normalized_stage = str(stage_text).strip()
                if normalized_stage and normalized_stage.lower().startswith(
                        normalized_cmd):
                    if (not normalized_title) or normalized_title.replace(
                            "_",
                            "-").startswith(normalized_cmd):
                        self.title = normalized_stage
        except Exception:
            logger.exception("Failed to introspect pipeline context to set ResultTable title")
        self.title_width = title_width
        self.max_columns = (
            max_columns if max_columns is not None else 5
        )  # Default 5 for cleaner display
        self.rows: List[Row] = []
        self.column_widths: Dict[str,
                                 int] = {}
        self.input_options: Dict[str,
                                 InputOption] = {}
        """Options available for user input (cmdlet arguments)"""
        self.source_command: Optional[str] = None
        """Command that generated this table (e.g., 'download-file URL')"""
        self.source_args: List[str] = []
        """Base arguments for the source command"""
        self.header_lines: List[str] = []
        """Optional metadata lines rendered under the title"""
        self.preserve_order: bool = bool(preserve_order)
        """If True, skip automatic sorting so display order matches input order."""
        self.perseverance: bool = preserve_order
        """If True, skip automatic sorting so display order matches input order."""
        self.interactive: bool = False
        """When True, suppress row numbers/selection to make the table non-interactive."""
        self.table: Optional[str] = None
        """Table type (e.g., 'youtube', 'soulseek') for context-aware selection logic."""

        self.table_metadata: Dict[str, Any] = {}
        """Optional plugin/table metadata (e.g., plugin name, view)."""

        self.value_case: str = "preserve"
        """Display-only value casing: 'lower', 'upper', or 'preserve' (default)."""

    def set_value_case(self, value_case: str) -> "Table":
        """Configure display-only casing for rendered cell values."""
        case = str(value_case or "").strip().lower()
        if case not in {"lower",
                        "upper",
                        "preserve"}:
            case = "lower"
        self.value_case = case
        return self

    def _apply_value_case(self, text: str) -> str:
        if not text:
            return ""
        if self.value_case == "upper":
            return text.upper()
        if self.value_case == "preserve":
            return text
        return text.lower()

    def set_table(self, table: str) -> "Table":
        """Set the table type for context-aware selection logic."""
        self.table = table
        return self

    def set_table_metadata(self, metadata: Optional[Dict[str, Any]]) -> "Table":
        """Attach plugin/table metadata for downstream selection logic."""
        self.table_metadata = dict(metadata or {})
        return self

    def get_table_metadata(self) -> Dict[str, Any]:
        """Return attached plugin/table metadata (copy to avoid mutation)."""
        try:
            return dict(self.table_metadata)
        except Exception:
            logger.exception("Failed to copy table metadata")
            return {}

    def _interactive(self, interactive: bool = True) -> "Table":
        """Mark the table as non-interactive (no row numbers, no selection parsing)."""
        self.interactive = bool(interactive)
        return self

    def _perseverance(self, perseverance: bool = True) -> "Table":
        """Configure whether this table should skip automatic sorting."""
        keep_order = bool(perseverance)
        self.perseverance = keep_order
        self.preserve_order = keep_order
        return self

    def add_row(self) -> Row:
        """Add a new row to the table and return it for configuration."""
        row = Row()
        row.source_index = len(self.rows)
        self.rows.append(row)
        return row

    def set_source_command(
        self,
        command: str,
        args: Optional[List[str]] = None
    ) -> "Table":
        """Set the source command that generated this table.

        This is used for @N expansion: when user runs @2 | next-cmd, it will expand to:
        source_command + source_args + row_selection_args | next-cmd

        Args:
            command: Command name (e.g., 'download-file')
            args: Base arguments for the command (e.g., ['URL'])

        Returns:
            Self for chaining
        """
        self.source_command = command
        self.source_args = args or []
        return self

    def init_command(
        self,
        title: str,
        command: str,
        args: Optional[List[str]] = None,
        preserve_order: bool = False,
    ) -> "Table":
        """Initialize table with title, command, args, and preserve_order in one call.

        Consolidates common initialization pattern: ResultTable(title) + set_source_command(cmd, args) + set_preserve_order(preserve_order)

        Args:
            title: Table title
            command: Source command name
            args: Command arguments
            preserve_order: Whether to preserve input row order

        Returns:
            self for method chaining
        """
        self.title = title
        self.source_command = command
        self.source_args = args or []
        self.perseverance = bool(preserve_order)
        self.preserve_order = bool(preserve_order)
        return self

    def copy_with_title(self, new_title: str) -> "Table":
        """Create a new table copying settings from this one but with a new title.

        Consolidates pattern: new_table = ResultTable(title); new_table.set_source_command(...)
        Useful for intermediate processing that needs to preserve source command but update display title.

        Args:
            new_title: New title for the copied table

        Returns:
            New ResultTable with copied settings and new title
        """
        new_table = Table(
            title=new_title,
            title_width=self.title_width,
            max_columns=self.max_columns,
            preserve_order=self.perseverance,
        )
        new_table.source_command = self.source_command
        new_table.source_args = list(self.source_args) if self.source_args else []
        new_table.input_options = dict(self.input_options) if self.input_options else {}
        new_table.interactive = self.interactive
        new_table.table = self.table
        new_table.table_metadata = (
            dict(self.table_metadata) if getattr(self, "table_metadata", None) else {}
        )
        new_table.header_lines = list(self.header_lines) if self.header_lines else []
        return new_table

    def set_row_selection_args(self, row_index: int, selection_args: List[str]) -> None:
        """Set the selection arguments for a specific row.

        When user selects this row via @N, these arguments will be appended to the
        source command to re-execute with that item selected.

        Args:
            row_index: Index of the row (0-based)
            selection_args: Arguments to use (e.g., ['-item', '3'])
        """
        if 0 <= row_index < len(self.rows):
            self.rows[row_index].selection_args = selection_args

    def set_row_selection_action(self, row_index: int, selection_action: List[str]) -> None:
        """Specify the entire stage tokens to run for this row on @N."""
        if 0 <= row_index < len(self.rows):
            self.rows[row_index].selection_action = selection_action

    def set_header_lines(self, lines: List[str]) -> "Table":
        """Attach metadata lines that render beneath the title."""
        self.header_lines = [line for line in lines if line]
        return self

    def set_header_line(self, line: str) -> "Table":
        """Attach a single metadata line beneath the title."""
        return self.set_header_lines([line] if line else [])

    def set_storage_summary(
        self,
        storage_counts: Dict[str,
                             int],
        filter_text: Optional[str] = None,
        inline: bool = False,
    ) -> str:
        """Render a storage count summary (e.g., "Hydrus:0 Local:1 | filter: \"q\"").

        Returns the summary string so callers can place it inline with the title if desired.
        """
        summary_parts: List[str] = []

        if storage_counts:
            summary_parts.append(
                " ".join(f"{name}:{count}" for name, count in storage_counts.items())
            )

        if filter_text:
            safe_filter = filter_text.replace('"', '\\"')
            summary_parts.append(f'filter: "{safe_filter}"')

        summary = " | ".join(summary_parts)
        if not inline:
            self.set_header_line(summary)
        return summary

    def sort_by_title(self) -> "Table":
        """Sort rows alphabetically by Title or Name column.

        Looks for columns named 'Title', 'Name', or 'Tag' (in that order).
        Case-insensitive sort. Returns self for chaining.

        NOTE: This only affects display order. Each row keeps its original
        `source_index` (insertion order) for callers that need stable mapping.
        """
        if getattr(self, "preserve_order", False):
            return self
        # Find the title column (try Title, Name, Tag in order)
        title_col_idx = None
        for row in self.rows:
            if not row.columns:
                continue
            for idx, col in enumerate(row.columns):
                col_lower = col.name.lower()
                if col_lower in ("title", "name", "tag"):
                    title_col_idx = idx
                    break
            if title_col_idx is not None:
                break

        if title_col_idx is None:
            # No title column found, return unchanged
            return self

        # Sort rows by the title column value (case-insensitive)
        self.rows.sort(
            key=lambda row: (
                row.columns[title_col_idx].value.lower()
                if title_col_idx < len(row.columns) else ""
            )
        )

        return self

    def sort_by_tag_namespace(self, namespace: str, *, numeric: bool = False, reverse: bool = False) -> "Table":
        """Sort rows by the first value found for a tag namespace.

        Looks first at row payload tag metadata, then falls back to the visible Tag column.
        When ``numeric`` is True, the first numeric token inside the namespace value is used.
        """
        if getattr(self, "preserve_order", False):
            return self

        wanted = str(namespace or "").strip()
        if not wanted:
            return self

        def _row_tags(row: Row) -> Any:
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
            tag_column = row.get_column("Tag")
            return tag_column or []

        self.rows.sort(
            key=lambda row: _namespace_sort_key(_row_tags(row), wanted, numeric=bool(numeric)),
            reverse=bool(reverse),
        )
        return self

    def add_result(self, result: Any) -> "Table":
        """Add a result object (SearchResult, PipeObject, ResultItem, TagItem, or dict) as a row.

        Args:
            result: Result object to add

        Returns:
            Self for chaining
        """
        row = self.add_row()
        row.payload = result

        # Handle TagItem from get_tag.py (tag display with index)
        if hasattr(result, "__class__") and result.__class__.__name__ == "TagItem":
            self._add_tag_item(row, result)
        # Handle ResultItem from search_file.py (compact display)
        elif hasattr(result, "__class__") and result.__class__.__name__ == "ResultItem":
            self._add_result_item(row, result)
        # Handle SearchResult from search_file.py
        elif hasattr(result,
                     "__class__") and result.__class__.__name__ == "SearchResult":
            self._add_search_result(row, result)
        # Handle PipeObject from models.py
        elif hasattr(result, "__class__") and result.__class__.__name__ == "PipeObject":
            self._add_pipe_object(row, result)
        # Handle dict
        elif isinstance(result, dict):
            self._add_dict(row, result)
        # Handle generic objects with __dict__
        elif hasattr(result, "__dict__"):
            self._add_generic_object(row, result)
        # Handle strings (simple text result)
        elif isinstance(result, str):
            row.add_column("Result", result)

        # Extract selection metadata from payload if available (for @N expansion)
        if isinstance(result, dict):
            sel_args = result.get("_selection_args")
            if isinstance(sel_args, (list, tuple)):
                row.selection_args = [str(x) for x in sel_args if x is not None]
            
            sel_action = result.get("_selection_action")
            if isinstance(sel_action, (list, tuple)):
                row.selection_action = [str(x) for x in sel_action if x is not None]

        return self

    def get_row_payload(self, row_index: int) -> Optional[Any]:
        """Return the original payload for the row at ``row_index`` if available."""
        if 0 <= row_index < len(self.rows):
            return getattr(self.rows[row_index], "payload", None)
        return None

    def get_row_selection_args(self, row_index: int) -> Optional[List[str]]:
        """Return selection arguments for the row at ``row_index`` from its payload."""
        payload = self.get_row_payload(row_index)
        if isinstance(payload, dict):
            args = payload.get("_selection_args")
            if isinstance(args, (list, tuple)):
                return [str(x) for x in args if x is not None]
        return None

    def get_row_selection_action(self, row_index: int) -> Optional[List[str]]:
        """Return primary selection action for the row at ``row_index`` from its payload."""
        payload = self.get_row_payload(row_index)
        if isinstance(payload, dict):
            action = payload.get("_selection_action")
            if isinstance(action, (list, tuple)):
                return [str(x) for x in action if x is not None]
        return None

    def get_payloads(self) -> List[Any]:
        """Return the payloads for every row, preserving table order."""
        payloads: List[Any] = []
        for row in self.rows:
            payload = getattr(row, "payload", None)
            if payload is not None:
                payloads.append(payload)
        return payloads

    def _add_search_result(self, row: Row, result: Any) -> None:
        """Extract and add SearchResult fields to row."""
        cols = getattr(result, "columns", None)
        used_explicit_columns = False
        if cols:
            used_explicit_columns = True
            for name, value in cols:
                row.add_column(name, value)
        else:
            # Core fields (legacy fallback)
            title = getattr(result, "title", "")
            table = str(getattr(result, "table", "") or "").lower()

            # Handle extension separation for local files
            extension = ""
            if title and table == "local":
                path_obj = Path(title)
                if path_obj.suffix:
                    extension = path_obj.suffix.lstrip(".")
                    title = path_obj.stem

            if title:
                row.add_column("Title", title)

            # Extension column
            row.add_column("Ext", extension)

            if hasattr(result, "table") and getattr(result, "table", None):
                row.add_column("Source", str(getattr(result, "table")))

            if hasattr(result, "detail") and result.detail:
                row.add_column("Detail", result.detail)

            if hasattr(result, "media_kind") and result.media_kind:
                row.add_column("Type", result.media_kind)

            # Tag summary
            if hasattr(result, "tag_summary") and result.tag_summary:
                row.add_column("Tag", str(result.tag_summary))

            # Duration (for media)
            if hasattr(result, "duration_seconds") and result.duration_seconds:
                dur = _format_duration_hms(result.duration_seconds)
                row.add_column("Duration", dur or str(result.duration_seconds))

            # Size (for files)
            if hasattr(result, "size_bytes") and result.size_bytes:
                row.add_column("Size", _format_size(result.size_bytes, integer_only=False))

            # Annotations
            if hasattr(result, "annotations") and result.annotations:
                row.add_column("Annotations", ", ".join(str(a) for a in result.annotations))

        try:
            md = getattr(result, "full_metadata", None)
            md_dict = dict(md) if isinstance(md, dict) else {}
        except Exception:
            logger.debug("Failed to extract full_metadata for result of type %s", type(result), exc_info=True)
            md_dict = {}

        try:
            selection_args = getattr(result, "selection_args", None)
        except Exception:
            logger.debug("Failed to get selection_args from result of type %s", type(result), exc_info=True)
            selection_args = None
        if selection_args is None:
            selection_args = md_dict.get("_selection_args") or md_dict.get("selection_args")
        if selection_args:
            row.selection_args = [str(a) for a in selection_args if a is not None]

        try:
            selection_action = getattr(result, "selection_action", None)
        except Exception:
            logger.debug("Failed to get selection_action from result of type %s", type(result), exc_info=True)
            selection_action = None
        if selection_action is None:
            selection_action = md_dict.get("_selection_action") or md_dict.get("selection_action")
        if selection_action:
            row.selection_action = [str(a) for a in selection_action if a is not None]

    def _add_result_item(self, row: Row, item: Any) -> None:
        """Extract and add ResultItem fields to row (compact display for search results).

        Shows only essential columns:
        - Title (required)
        - Ext (extension)
        - Storage (source backend)
        - Size (formatted MB, integer only)

        All other fields are stored in item but not displayed to keep table compact.
        Use @row# syntax to pipe full item data to next command.
        """
        # Title (required)
        title = getattr(item, "title", None) or "Unknown"
        table = str(getattr(item,
                            "table",
                            "") or getattr(item,
                                           "store",
                                           "") or "").lower()

        # Handle extension separation for local files
        extension = ""
        if title and table == "local":
            # Try to split extension
            path_obj = Path(title)
            if path_obj.suffix:
                extension = path_obj.suffix.lstrip(".")
                title = path_obj.stem

        if title:
            row.add_column("Title", title)

        # Extension column - always add to maintain column order
        row.add_column("Ext", extension)

        # Storage (source backend - hydrus, local, debrid, etc)
        if getattr(item, "table", None):
            row.add_column("Storage", str(getattr(item, "table")))
        elif getattr(item, "store", None):
            row.add_column("Storage", str(getattr(item, "store")))

        # Size (for files)
        if hasattr(item, "size_bytes") and item.size_bytes:
            row.add_column("Size", _format_size(item.size_bytes, integer_only=False))

    def _add_tag_item(self, row: Row, item: Any) -> None:
        """Extract and add TagItem fields to row (compact tag display).

        Shows the Tag column with the tag name and Source column to identify
        which storage backend the tag values come from (Hydrus, local, etc.).
        All data preserved in TagItem for piping and operations.
        Tag row selection is handled by the CLI pipeline (e.g. `@N | ...`).
        """
        # Tag name
        if hasattr(item, "tag_name") and item.tag_name:
            row.add_column("Tag", item.tag_name)

        # Source/Store (where the tag values come from)
        source_val = getattr(item, "store", None)
        if source_val:
            row.add_column("Instance", source_val)

    def _add_pipe_object(self, row: Row, obj: Any) -> None:
        """Extract and add PipeObject fields to row."""
        # Source and identifier
        if hasattr(obj, "source") and obj.source:
            row.add_column("Source", obj.source)

        # Title
        if hasattr(obj, "title") and obj.title:
            row.add_column("Title", obj.title)

        # File info
        if hasattr(obj, "path") and obj.path:
            row.add_column("Path", str(obj.path))

        # Tag
        if hasattr(obj, "tag") and obj.tag:
            tag_str = ", ".join(obj.tag[:3])  # First 3 tag values
            if len(obj.tag) > 3:
                tag_str += f", +{len(obj.tag) - 3} more"
            row.add_column("Tag", tag_str)

        # Duration
        if hasattr(obj, "duration") and obj.duration:
            dur = _format_duration_hms(obj.duration)
            row.add_column("Duration", dur or str(obj.duration))

        # Warnings
        if hasattr(obj, "warnings") and obj.warnings:
            warnings_str = "; ".join(obj.warnings[:2])
            if len(obj.warnings) > 2:
                warnings_str += f" (+{len(obj.warnings) - 2} more)"
            row.add_column("Warnings", warnings_str)

    def _add_dict(self, row: Row, data: Dict[str, Any]) -> None:
        """Extract and add dict fields to row using first-match priority groups.

        Respects max_columns limit to keep table compact and readable.

        Special handling for 'columns' field: if present, uses it to populate row columns
        instead of treating it as a regular field. This allows dynamic column definitions
        from search providers.

        Priority field groups (first match per group):
        - title | name | filename
        - store | table | source
        - size | size_bytes
        - ext
        """

        # Helper to determine if a field should be hidden from display
        def is_hidden_field(field_name: Any) -> bool:
            # Hide internal/metadata fields
            hidden_fields = {
                "__",
                "id",
                "action",
                "parent_id",
                "is_temp",
                "path",
                "extra",
                "target",
                "hash",
                "hash_hex",
                "file_hash",
                "tag_summary",
            }
            if isinstance(field_name, str):
                if field_name.startswith("__"):
                    return True
                if field_name in hidden_fields:
                    return True
            return False

        # Strip out hidden metadata fields (prefixed with __)
        visible_data = {
            k: v
            for k, v in data.items() if not is_hidden_field(k)
        }

        # Normalize common fields using shared extractors so nested metadata/path values work.
        # This keeps Ext/Size/Store consistent across all dict-based result sources.
        try:
            store_extracted = extract_store_value(data)
            if (store_extracted and "store" not in visible_data
                    and "table" not in visible_data and "source" not in visible_data):
                visible_data["store"] = store_extracted
        except Exception as e:
            from SYS.logger import log
            log(f"Failed to extract store value for item: {data!r}. Error: {e}")

        try:
            ext_extracted = extract_ext_value(data)
            # Always ensure `ext` exists so priority_groups keeps a stable column.
            visible_data["ext"] = str(ext_extracted or "")
        except Exception as e:
            from SYS.logger import log
            log(f"Failed to extract ext value for item: {data!r}. Error: {e}")
            visible_data.setdefault("ext", "")

        try:
            size_extracted = extract_size_bytes_value(data)
            if (size_extracted is not None and "size_bytes" not in visible_data
                    and "size" not in visible_data):
                visible_data["size_bytes"] = size_extracted
        except Exception as e:
            from SYS.logger import log
            log(f"Failed to extract size bytes for item: {data!r}. Error: {e}")

        # Handle extension separation for local files
        store_val = str(
            visible_data.get("store",
                             "") or visible_data.get("table",
                                                     "")
            or visible_data.get("source",
                                "")
        ).lower()

        if store_val == "local":
            # Find title field
            title_field = next(
                (f for f in ["title", "name", "filename"] if f in visible_data),
                None
            )
            if title_field:
                title_val = str(visible_data[title_field])
                path_obj = Path(title_val)
                if path_obj.suffix:
                    extension = path_obj.suffix.lstrip(".")
                    visible_data[title_field] = path_obj.stem
                    # Preserve ext extracted from payload/metadata when present.
                    # Only use title suffix as fallback when ext is missing.
                    if not str(visible_data.get("ext") or "").strip():
                        visible_data["ext"] = extension
        # Ensure 'ext' is present so it gets picked up by priority_groups in correct order
        if "ext" not in visible_data:
            visible_data["ext"] = ""

        # Track which fields we've already added to avoid duplicates
        added_fields = set()
        column_count = 0  # Track total columns added

        # Helper function to format values
        def format_value(value: Any) -> str:
            if isinstance(value, list):
                formatted = ", ".join(str(v) for v in value[:3])
                if len(value) > 3:
                    formatted += f", +{len(value) - 3} more"
                return formatted
            return str(value)

        # Special handling for 'columns' field from search providers
        # If present, use it to populate row columns dynamically
        if ("columns" in visible_data and isinstance(visible_data["columns"],
                                                     list) and visible_data["columns"]):
            try:
                for col_name, col_value in visible_data["columns"]:
                    # Skip the "#" column as ResultTable already adds row numbers
                    if col_name == "#":
                        continue
                    if column_count >= self.max_columns:
                        break
                    # When providers supply raw numeric fields, keep formatting consistent.
                    if isinstance(col_name, str) and col_name.strip().lower() == "size":
                        try:
                            if col_value is None or str(col_value).strip() == "":
                                col_value_str = ""
                            else:
                                col_value_str = _format_size(
                                    col_value,
                                    integer_only=False
                                )
                        except Exception as exc:
                            logger.debug("Failed to format 'size' column value: %r", col_value, exc_info=True)
                            col_value_str = format_value(col_value)
                    elif isinstance(col_name,
                                    str) and col_name.strip().lower() == "duration":
                        try:
                            if col_value is None or str(col_value).strip() == "":
                                col_value_str = ""
                            else:
                                dur = _format_duration_hms(col_value)
                                col_value_str = dur or format_value(col_value)
                        except Exception as exc:
                            logger.debug("Failed to format 'duration' column value: %r", col_value, exc_info=True)
                            col_value_str = format_value(col_value)
                    else:
                        col_value_str = format_value(col_value)
                    row.add_column(col_name, col_value_str)
                    added_fields.add(col_name.lower())
                    column_count += 1
                # Mark 'columns' as handled so we don't add it as a field
                added_fields.add("columns")
                # Also mark common fields that shouldn't be re-displayed if they're in columns
                # This prevents showing both "Store" (from columns) and "Store" (from data fields)
                added_fields.add("table")
                added_fields.add("source")
                added_fields.add("target")
                added_fields.add("path")
                added_fields.add("media_kind")
                added_fields.add("detail")
                added_fields.add("annotations")
                added_fields.add(
                    "full_metadata"
                )  # Don't display full metadata as column
            except Exception:
                # Fall back to regular field handling if columns format is unexpected
                logger.exception("Failed to process 'columns' dynamic field list: %r", visible_data.get("columns"))

        # Only add priority groups if we haven't already filled columns from 'columns' field
        if column_count == 0:
            # Explicitly set which columns to display in order
            priority_groups = [
                ("title",
                 ["title",
                  "name",
                  "filename"]),
                ("tag",
                 ["tag",
                  "tags"]),
                ("store",
                 ["instance",
                  "store",
                  "table",
                  "source"]),
                ("plugin",
                 ["plugin"]),
                ("size",
                 ["size",
                  "size_bytes"]),
                ("ext",
                 ["ext"]),
            ]

            # Add priority field groups first - use first match in each group
            for _group_label, field_options in priority_groups:
                if column_count >= self.max_columns:
                    break
                for field in field_options:
                    if field in visible_data and field not in added_fields:
                        # Special handling for size fields - format with unit and decimals
                        if field in ["size", "size_bytes"]:
                            value_str = _format_size(
                                visible_data[field],
                                integer_only=False
                            )
                        else:
                            value_str = format_value(visible_data[field])

                        # Map field names to display column names
                        if field in ["instance", "store", "table", "source"]:
                            col_name = "Instance"
                        elif field == "plugin":
                            col_name = "Plugin"
                        elif field in ["size", "size_bytes"]:
                            col_name = "Size"
                        elif field in ["title", "name", "filename"]:
                            col_name = "Title"
                        else:
                            col_name = field.replace("_", " ").title()

                        row.add_column(col_name, value_str)
                        added_fields.add(field)
                        column_count += 1
                        break  # Use first match in this group, skip rest

            # Add remaining fields only if we haven't hit max_columns (and no explicit columns were set)
            # Don't add any remaining fields - only use priority_groups for dict results

        # Check for selection args
        if "_selection_args" in data:
            row.selection_args = data["_selection_args"]
            # Don't display it
            added_fields.add("_selection_args")

    def _add_generic_object(self, row: Row, obj: Any) -> None:
        """Extract and add fields from generic objects."""
        if hasattr(obj, "__dict__"):
            for key, value in obj.__dict__.items():
                if key.startswith("_"):  # Skip private attributes
                    continue

                row.add_column(key.replace("_", " ").title(), str(value))

    def to_rich(self):
        """Return a Rich renderable representing this table."""
        appearance_mode = get_result_table_appearance_mode()
        header_style = get_result_table_header_style({"table_appearance": appearance_mode})
        border_style = get_result_table_border_style({"table_appearance": appearance_mode})
        panel_style = get_result_table_panel_style({"table_appearance": appearance_mode})
        table_box = get_result_table_box()
        panel_box = get_result_panel_box()
        show_lines = get_result_table_show_lines()
        expand = get_result_table_expand()

        if not self.rows:
            empty = _rich().Text("No results")
            return (
                _rich().Panel(
                    empty,
                    title=_rich().Text(str(self.title), style=header_style),
                    border_style=border_style,
                    box=panel_box,
                    padding=(0, 0),
                    expand=expand,
                    style=panel_style,
                )
                if self.title
                else empty
            )

        col_names: List[str] = []
        seen: Set[str] = set()
        for row in self.rows:
            for col in row.columns:
                if col.name not in seen:
                    seen.add(col.name)
                    col_names.append(col.name)

        table = _rich().RichTable(
            show_header=True,
            header_style=header_style,
            border_style=border_style,
            box=table_box,
            expand=expand,
            show_lines=show_lines,
            padding=(0, 1),
            pad_edge=False,
            collapse_padding=True,
        )
        apply_result_table_layout(table)

        table.add_column("#", justify="right", no_wrap=True)

        for name in col_names:
            header = format_result_table_header(name)
            if name.lower() == "ext":
                table.add_column(header, no_wrap=True)
            elif name.lower() == "tag":
                table.add_column(header, overflow="fold")
            else:
                table.add_column(header)

        for row_idx, row in enumerate(self.rows, 1):
            cells: List[str] = []
            cells.append(str(row_idx))
            for name in col_names:
                val = row.get_column(name) or ""
                cells.append(self._apply_value_case(_sanitize_cell_text(val)))
            table.add_row(
                *cells,
                style=get_result_table_row_style(
                    row_idx - 1,
                    appearance_mode=appearance_mode,
                ),
            )

        if self.title or self.header_lines:
            header_bits = [_rich().Text(line) for line in (self.header_lines or [])]
            renderable = _rich().Group(*header_bits, table) if header_bits else table
            return (
                _rich().Panel(
                    renderable,
                    title=_rich().Text(str(self.title), style=header_style),
                    border_style=border_style,
                    box=panel_box,
                    padding=(0, 0),
                    expand=expand,
                    style=panel_style,
                )
                if self.title
                else renderable
            )

        return table

    def format_compact(self) -> str:
        """Format table in compact form (one line per row).

        Returns:
            Formatted table string
        """
        lines = []

        if self.title:
            lines.append(f"\n{self.title}")
            lines.append("-" * len(self.title))

        for i, row in enumerate(self.rows, 1):
            row_str = " | ".join(str(col) for col in row.columns)
            lines.append(f"{i}. {row_str}")

        return "\n".join(lines)

    def format_json(self) -> str:
        """Format table as JSON.

        Returns:
            JSON string
        """
        data = {
            "title": self.title,
            "row_count": len(self.rows),
            "rows": [row.to_list() for row in self.rows],
        }
        return json.dumps(data, indent=2)

    def to_dict(self) -> Dict[str, Any]:
        """Convert table to dictionary.

        Returns:
            Dictionary representation
        """
        return {
            "title": self.title,
            "rows": [row.to_list() for row in self.rows]
        }

    def __str__(self) -> str:
        """String representation.

        Rich is the primary rendering path. This keeps accidental `print(table)`
        usage from emitting ASCII box-drawn tables.
        """
        label = self.title or "ResultTable"
        return f"{label} ({len(self.rows)} rows)"

    def __rich__(self):
        return self.to_rich()

    def __repr__(self) -> str:
        """Developer representation."""
        return f"ResultTable(title={self.title!r}, rows={len(self.rows)})"

    def __len__(self) -> int:
        """Number of rows in the table."""
        return len(self.rows)

    def __iter__(self):
        """Iterate over rows."""
        return iter(self.rows)

    def __getitem__(self, index: int) -> Row:
        """Get row by index."""
        return self.rows[index]

    def select_interactive(
        self,
        prompt: str = "Select an item",
        accept_args: bool = False
    ) -> Optional[List[int]] | dict:
        """Display table and get interactive user selection (single or multiple).

        Supports multiple input formats:
        - Single: "5" or "q" to quit
        - Range: "3-5" (selects items 3, 4, 5)
        - Multiple: "3,5,13" (selects items 3, 5, and 13)
        - Combined: "1-3,7,9-11" (selects 1,2,3,7,9,10,11)

        If accept_args=True, also supports cmdlet arguments:
        - "5 -storage hydrus" → returns indices [4] + args {"-storage": "hydrus"}
        - "2-4 -storage hydrus -tag important" → returns indices [1,2,3] + multiple args

        Args:
            prompt: Custom prompt text
            accept_args: If True, parse and return cmdlet arguments from input

        Returns:
            If accept_args=False: List of 0-based indices, or None if cancelled
            If accept_args=True: Dict with "indices" and "args" keys, or None if cancelled
        """
        if self.interactive:
            from SYS.rich_display import stdout_console

            stdout_console().print(self)
            stdout_console().print(_rich().Panel(_rich().Text("Selection is disabled for this table.")))
            return None

        # Display the table
        from SYS.rich_display import stdout_console

        stdout_console().print(self)

        # Get user input
        while True:
            try:
                if accept_args:
                    choice = _rich().Prompt.ask(
                        f"{prompt} (e.g., '5' or '2 -storage hydrus' or 'q' to quit)"
                    ).strip()
                else:
                    choice = _rich().Prompt.ask(
                        f"{prompt} (e.g., '5' or '3-5' or '1,3,5' or 'q' to quit)"
                    ).strip()

                if choice.lower() == "q":
                    return None

                if accept_args:
                    # Parse selection and arguments
                    result = self._parse_selection_with_args(choice)
                    if result is not None:
                        return result
                    stdout_console().print(
                        _rich().Panel(
                            _rich().Text(
                                "Invalid format. Use: selection (5 or 3-5 or 1,3,5) optionally followed by flags (e.g., '5 -storage hydrus')."
                            )
                        )
                    )
                else:
                    # Parse just the selection
                    selected_indices = self._parse_selection(choice)
                    if selected_indices is not None:
                        return selected_indices
                    stdout_console().print(
                        _rich().Panel(
                            _rich().Text(
                                "Invalid format. Use: single (5), range (3-5), list (1,3,5), combined (1-3,7,9-11), or 'q' to quit."
                            )
                        )
                    )
            except (ValueError, EOFError):
                if accept_args:
                    stdout_console().print(
                        _rich().Panel(
                            _rich().Text(
                                "Invalid format. Use: selection (5 or 3-5 or 1,3,5) optionally followed by flags (e.g., '5 -storage hydrus')."
                            )
                        )
                    )
                else:
                    stdout_console().print(
                        _rich().Panel(
                            _rich().Text(
                                "Invalid format. Use: single (5), range (3-5), list (1,3,5), combined (1-3,7,9-11), or 'q' to quit."
                            )
                        )
                    )

    def _parse_selection(self, selection_str: str) -> Optional[List[int]]:
        """Parse user selection string into list of 0-based indices.

        Supports:
        - Single: "5" → [4]
        - Range: "3-5" → [2, 3, 4]
        - Multiple: "3,5,13" → [2, 4, 12]
        - Combined: "1-3,7,9-11" → [0, 1, 2, 6, 8, 9, 10]

        Args:
            selection_str: User input string

        Returns:
            List of 0-based indices, or None if invalid
        """
        if self.interactive:
            return None

        indices = set()

        # Split by comma for multiple selections
        parts = selection_str.split(",")

        for part in parts:
            part = part.strip()
            if not part:
                continue

            # Check if it's a range (contains dash)
            if "-" in part:
                # Handle ranges like "3-5"
                try:
                    range_parts = part.split("-")
                    if len(range_parts) != 2:
                        return None

                    start = int(range_parts[0].strip())
                    end = int(range_parts[1].strip())

                    # Validate range
                    if start < 1 or end < 1 or start > len(self.rows) or end > len(
                            self.rows):
                        return None

                    if start > end:
                        start, end = end, start

                    # Add all indices in range (convert to 0-based)
                    for i in range(start, end + 1):
                        indices.add(i - 1)

                except (ValueError, IndexError):
                    return None
            else:
                # Single number
                try:
                    num = int(part)
                    if num < 1 or num > len(self.rows):
                        return None
                    indices.add(num - 1)  # Convert to 0-based
                except ValueError:
                    return None

        if not indices:
            return None

        # Return sorted list
        return sorted(list(indices))

    def _parse_selection_with_args(self, input_str: str) -> Optional[dict]:
        """Parse user input into selection indices and cmdlet arguments.

        Supports formats like:
        - "5" → {"indices": [4], "args": {}}
        - "2 -storage hydrus" → {"indices": [1], "args": {"-storage": "hydrus"}}
        - "3-5 -storage hydrus -tag important" → {"indices": [2,3,4], "args": {"-storage": "hydrus", "-tag": "important"}}

        Args:
            input_str: User input string with selection and optional flags

        Returns:
            Dict with "indices" and "args" keys, or None if invalid
        """
        parts = input_str.split()
        if not parts:
            return None

        # First part should be the selection
        selection_str = parts[0]
        selected_indices = self._parse_selection(selection_str)

        if selected_indices is None:
            return None

        # Remaining parts are cmdlet arguments
        cmdlet_args: dict[str, Any] = {}
        i = 1
        while i < len(parts):
            part = parts[i]

            # Check if it's a flag (starts with -)
            if part.startswith("-"):
                flag = part
                value = None

                # Get the value if it exists and doesn't start with -
                if i + 1 < len(parts) and not parts[i + 1].startswith("-"):
                    value = parts[i + 1]
                    i += 2
                else:
                    i += 1

                # Store the flag
                if value is not None:
                    cmdlet_args[flag] = value
                else:
                    cmdlet_args[flag] = True  # Flag without value
            else:
                i += 1

        return {
            "indices": selected_indices,
            "args": cmdlet_args
        }

    def add_input_option(self, option: InputOption) -> "Table":
        """Add an interactive input option to the table.

        Input options allow users to specify cmdlet arguments interactively,
        like choosing a download location or source.

        Args:
            option: InputOption definition

        Returns:
            Self for chaining
        """
        self.input_options[option.name] = option
        return self

    def select_option(self, option_name: str, prompt: str = "") -> Optional[str]:
        """Interactively get user input for a specific option.

        Displays the option choices (if enum) and prompts user for input.

        Args:
            option_name: Name of the option to get input for
            prompt: Custom prompt text (uses option description if not provided)

        Returns:
            User's selected/entered value, or None if cancelled
        """
        if option_name not in self.input_options:
            print(f"Unknown option: {option_name}")
            return None

        option = self.input_options[option_name]
        prompt_text = prompt or option.description or option_name

        while True:
            try:
                # For enum options, show choices
                if option.type == "enum" and option.choices:
                    print(f"\n{prompt_text}")
                    for i, choice in enumerate(option.choices, 1):
                        print(f"  {i}. {choice}")

                    choice_input = input(
                        f"Select {option_name} (1-{len(option.choices)}, or 'q' to cancel): "
                    ).strip()

                    if choice_input.lower() == "q":
                        return None

                    try:
                        idx = int(choice_input) - 1
                        if 0 <= idx < len(option.choices):
                            return option.choices[idx]
                        print(f"Invalid choice. Enter 1-{len(option.choices)}")
                    except ValueError:
                        print(f"Invalid choice. Enter 1-{len(option.choices)}")

                # For string/integer options, get direct input
                elif option.type in ("string", "integer"):
                    value = input(f"{prompt_text} (or 'q' to cancel): ").strip()

                    if value.lower() == "q":
                        return None

                    # Validate if validator provided
                    if option.validator and not option.validator(value):
                        print(f"Invalid value for {option_name}")
                        continue

                    # Type conversion
                    if option.type == "integer":
                        try:
                            int(value)
                        except ValueError:
                            print("Must be an integer")
                            continue

                    return value

                # For flag options
                elif option.type == "flag":
                    response = input(f"{prompt_text} (y/n): ").strip().lower()
                    if response == "q":
                        return None
                    return "true" if response in ("y", "yes", "true") else "false"

            except (ValueError, EOFError):
                return None

    def get_all_options(self) -> Dict[str, str]:
        """Get all input options at once with user prompts.

        Interactively prompts user for all registered options.

        Returns:
            Dictionary mapping option names to selected values
        """
        result = {}
        for name, _option in self.input_options.items():
            value = self.select_option(name)
            if value is not None:
                result[name] = value
        return result

    def select_by_index(self, index: int) -> Optional[Row]:
        """Get a row by 1-based index (user-friendly).

        Args:
            index: 1-based index

        Returns:
            ResultRow if valid, None otherwise
        """
        idx = index - 1
        if 0 <= idx < len(self.rows):
            return self.rows[idx]
        return None


def _format_size(size: Any, integer_only: bool = False) -> str:
    """Format file size as human-readable string.

    Args:
        size: Size in bytes or already formatted string
        integer_only: If True, show MB as an integer (e.g., "250 MB")

    Returns:
        Formatted size string with units (e.g., "3.53 MB", "0.57 MB", "1.2 GB")
    """
    if isinstance(size, str):
        return size if size else ""

    try:
        bytes_val = int(size)
        if bytes_val < 0:
            return ""

        # Keep display consistent with the CLI expectation: show MB with unit
        # (including values under 1 MB as fractional MB), and show GB for very
        # large sizes.
        if bytes_val >= 1024**3:
            value = bytes_val / (1024**3)
            unit = "GB"
        else:
            value = bytes_val / (1024**2)
            unit = "MB"

        if integer_only:
            return f"{int(round(value))} {unit}"

        num = f"{value:.2f}".rstrip("0").rstrip(".")
        return f"{num} {unit}"
    except (ValueError, TypeError):
        return ""


def format_result(result: Any, title: str = "") -> str:
    """Quick function to format a single result or list of results.

    Args:
        result: Result object, list of results, or dict
        title: Optional title for the table

    Returns:
        Formatted string
    """
    table = Table(title)

    if isinstance(result, list):
        for item in result:
            table.add_result(item)
    else:
        table.add_result(result)

    return str(table)

def extract_item_metadata(item: Any) -> Dict[str, Any]:
    """Extract a comprehensive set of metadata from an item for the ItemDetailView.
    
    Converts items (ResultModel, dicts, objects) into normalized metadata dict.
    Extracts all relevant fields for display: Title, Hash, Store, Path, Ext, Size,
    Duration, URL, Relations, Tags.
    
    Optimization:
    - Calls _as_dict() only once and reuses throughout
    - Handles both ResultModel objects and legacy dicts/objects
    
    Example output:
        {
            "Title": "video.mp4",
            "Hash": "abc123def456...",
            "Store": "hydrus",
            "Path": "/mnt/media/video.mp4",
            "Ext": "mp4",
            "Size": "1.2 GB",
            "Duration": "1h23m",
            "Url": "https://example.com/video.mp4",
            "Relations": <null>,
            "Tags": "movie, comedy"
        }
    
    Args:
        item: Object to extract metadata from (ResultModel, dict, or any object)
        
    Returns:
        Dictionary with standardized metadata fields (empty dict if None input)
    """
    if item is None:
        return {}
    
    out = {}

    def _merge_columns(columns_value: Any) -> None:
        if not isinstance(columns_value, (list, tuple)):
            return
        for column in columns_value:
            label = None
            value = None
            if isinstance(column, (list, tuple)) and len(column) >= 2:
                label, value = column[0], column[1]
            elif isinstance(column, dict):
                label = column.get("name") or column.get("label") or column.get("key")
                value = column.get("value")
            else:
                label = getattr(column, "name", None)
                value = getattr(column, "value", None)

            label_text = str(label or "").strip()
            if not label_text or value is None:
                continue

            value_text = str(value).strip()
            if not value_text:
                continue

            normalized = label_text.lower()
            if any(str(existing or "").strip().lower() == normalized for existing in out):
                continue
            out[label_text] = value_text

    # Fallback to existing extraction logic for legacy objects/dicts
    # Convert once and reuse throughout to avoid repeated _as_dict() calls
    data = _as_dict(item) or {}
    _merge_columns(data.get("columns"))
    store_col = out.pop("Store", None) or out.pop("store", None)
    
    # Use existing extractors from match-standard result table columns
    title = extract_title_value(item)
    if title: 
        out["Title"] = title
    else:
        # Fallback for raw dicts
        t = data.get("title") or data.get("name") or data.get("TITLE")
        if t: out["Title"] = t
    
    hv = extract_hash_value(item)
    if hv: out["Hash"] = hv
    
    store = extract_store_value(item) or str(store_col or "").strip()
    plugin = _get_first_dict_value(data, ["plugin"])
    if plugin:
        out["Plugin"] = str(plugin).strip()
    instance = _get_first_dict_value(data, ["instance"])
    if instance:
        out["Instance"] = str(instance).strip()
    elif store:
        out["Instance"] = store
    
    # Path/Target — check top-level then nested metadata/full_metadata
    path = data.get("path") or data.get("target") or data.get("filename")
    if not path:
        for meta_key in ("metadata", "full_metadata"):
            nested = data.get(meta_key)
            if isinstance(nested, dict):
                path = nested.get("path") or nested.get("target") or nested.get("filename") or nested.get("local_path")
                if path:
                    break
    if not path:
        hash_val = data.get("hash") or data.get("hash_hex") or data.get("file_hash")
        store_val = data.get("store") or data.get("source") or store
        if hash_val and store_val:
            try:
                from SYS.config import load_config
                from PluginCore.backend_registry import get_or_create_registry
                cfg = load_config(emit_summary=False)
                br = get_or_create_registry(cfg, suppress_debug=True)
                if br.is_available(str(store_val)):
                    backend = br[str(store_val)]
                    if not out.get("Plugin"):
                        store_type = str(getattr(backend, "STORE_TYPE", "") or "").strip()
                        if store_type:
                            out["Plugin"] = store_type
                    candidate_url = getattr(backend, "file_url", None)
                    if callable(candidate_url):
                        path = candidate_url(str(hash_val))
                    elif isinstance(candidate_url, str):
                        path = candidate_url.format(hash=str(hash_val))
            except Exception:
                pass
    if path:
        out["Path"] = str(path) if not isinstance(path, (list, tuple)) else str(path[0]) if path else ""
    
    ext = extract_ext_value(item)
    if ext: 
        out["Ext"] = ext
    else:
        e = data.get("ext") or data.get("extension") or data.get("file_ext")
        if e: out["Ext"] = e

     
    size = extract_size_bytes_value(item)
    if size is not None:
        out["Size"] = format_mb(size)
    else:
        s = data.get("size") or data.get("size_bytes")
        if s is not None:
            out["Size"] = str(s)

    # Duration
    dur = _get_first_dict_value(data, ["duration_seconds", "duration"])
    if dur:
        out["Duration"] = _format_duration_hms(dur)

    # URL
    url = _get_first_dict_value(data, ["url", "URL"])
    out["Url"] = str(url) if url else ""

    # Relationships
    rels = _get_first_dict_value(data, ["relationships", "rel"])
    out["Relations"] = str(rels) if rels else ""

    tags = _get_first_dict_value(data, ["tags", "tag"])
    if tags:
        if isinstance(tags, (list, tuple, set)):
            out["Tags"] = [str(t).strip() for t in tags if str(t).strip()]
        else:
            out["Tags"] = tags

    extras = list(_plugin_item_detail_extras(item, out, data) or [])
    raw_extras = data.get("_detail_extras")
    if isinstance(raw_extras, (list, tuple)):
        for row in raw_extras:
            if isinstance(row, dict):
                extras.append(row)
            elif isinstance(row, (list, tuple)) and len(row) >= 2:
                extras.append(
                    {
                        "label": row[0],
                        "value": row[1],
                        "after": row[2] if len(row) > 2 else "Title",
                    }
                )
    if extras:
        out["_detail_extras"] = extras
    
    return out


def _plugin_item_detail_extras(
    item: Any,
    metadata: Dict[str, Any],
    data: Dict[str, Any],
) -> List[Dict[str, Any]]:
    plugin_name = (
        metadata.get("Plugin")
        or _get_first_dict_value(data, ["plugin", "table", "provider"])
    )
    if not plugin_name:
        return []
    try:
        from PluginCore.registry import get_plugin_class

        plugin_cls = get_plugin_class(str(plugin_name))
    except Exception:
        return []
    if plugin_cls is None:
        return []
    try:
        rows = plugin_cls({}).item_detail_fields(item) or []
    except Exception:
        return []
    extras: List[Dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            label = row.get("label") or row.get("name")
            value = row.get("value")
            after = row.get("after") or "Hash"
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            label, value = row[0], row[1]
            after = row[2] if len(row) > 2 else "Hash"
        else:
            continue
        if label is None or value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        extras.append({"label": str(label), "value": value, "after": str(after or "Hash")})
    return extras


class ItemDetailView(Table):
    """A specialized view that displays item details alongside a list of related items (tags, urls, etc).

    This is used for 'get-tag', 'get-url' and similar cmdlets where we want to contextually show
    what is being operated on (the main item) along with the selection list.
    
    Display structure:
        ┌─ Item Details Panel ─────────────────────────────┐
        │ Title:     video.mp4                              │
        │ Hash:      abc123def456789...                     │
        │ Store:     hydrus                                 │
        │ Path:      /media/video.mp4                       │
        │ Ext:       mp4                                    │
        │ Url:       https://example.com/video.mp4          │
        └────────────────────────────────────────────────────┘
        
        # TAGS              Value
        1  .jpg            
        2  .png            
        3  .webp           
    
    Used by action cmdlets that operate on an item and show its related details.
    """

    def __init__(
        self,
        title: str = "",
        item_metadata: Optional[Dict[str, Any]] = None,
        detail_title: Optional[str] = None,
        exclude_tags: bool = False,
        detail_order: Optional[List[str]] = None,
        **kwargs
    ):
        super().__init__(title, **kwargs)
        self.item_metadata = item_metadata or {}
        self.detail_title = detail_title
        self.exclude_tags = exclude_tags
        self.detail_order = [str(value) for value in (detail_order or []) if str(value or "").strip()]

    def to_rich(self):
        """Render the item details panel above the standard results table."""
        from rich.table import Table as RichTable
        from rich.panel import Panel
        from rich.console import Group
        from rich.text import Text

        # 1. Create Detail Grid (matching rich_display.py style)
        styles = theme_styles()

        def _kv_grid() -> Any:
            grid = _rich().RichTable.grid(expand=True, padding=(0, 2))
            grid.add_column("Key", style=styles["accent"], justify="right", width=12)
            grid.add_column("Value", style=styles["value"], ratio=1)
            return grid

        def _render_tag_text(tag_value: Any, *, prefix: str = "") -> Text:
            tag_text = _rich().Text()
            if prefix:
                tag_text.append(str(prefix), style="dim")
            raw = str(tag_value or "")
            namespace, sep, value = raw.partition(":")
            if sep and namespace:
                tag_text.append(namespace, style=styles["accent"])
                tag_text.append(sep, style=styles["accent"])
                if value:
                    link = _tag_namespace_link(namespace, value)
                    if link:
                        tag_text.append(value, style=f"underline cyan link {link}")
                    else:
                        tag_text.append(value, style=styles["value"])
            else:
                tag_text.append(raw, style=styles["value"])
            return tag_text

        def _tag_grid(tags: List[str], columns: int, *, prefix: str = "") -> Any:
            grid = _rich().RichTable.grid(expand=True, padding=(0, 2))
            for _ in range(max(1, columns)):
                grid.add_column(ratio=1)
            for row_values in _chunk_detail_tags(tags, columns):
                cells = [_render_tag_text(tag, prefix=prefix) for tag in row_values]
                while len(cells) < columns:
                    cells.append(_rich().Text(""))
                grid.add_row(*cells)
            return grid

        def _has_renderable_value(value: Any) -> bool:
            if value is None:
                return False
            if isinstance(value, str):
                text = value.strip()
                return bool(text and text.lower() not in {"<null>", "null", "none"})
            if isinstance(value, (list, tuple, set)):
                return any(_has_renderable_value(item) for item in value)
            return True

        def _looks_like_http_url(value: Any) -> bool:
            text = str(value or "").strip().lower()
            return text.startswith("http://") or text.startswith("https://")

        def _short_link_label(url_value: str, *, max_len: int = 68) -> str:
            text = str(url_value or "").strip()
            if not text:
                return ""

            parsed = urlparse(text)
            host = (parsed.netloc or "").strip()
            path = (parsed.path or "").strip()
            leaf = path.rstrip("/").split("/")[-1] if path else ""

            if host and leaf:
                label = f"{host}/.../{leaf}"
            elif host and path:
                label = f"{host}{path}"
            elif host:
                label = host
            else:
                label = text

            if parsed.query:
                label = f"{label}?..."

            if len(label) > max_len:
                return f"{label[:max_len - 3]}..."
            return label

        def _looks_like_local_path(value: Any) -> bool:
            text = str(value or "").strip()
            if not text:
                return False
            try:
                p = Path(text)
                if p.exists():
                    return True
                return text.startswith("/") or text.startswith("\\\\") or (len(text) >= 2 and text[1] == ":" and text[2:3] in ("\\", "/"))
            except Exception:
                return False

        def _short_path_label(path_value: str, *, max_len: int = 68) -> str:
            text = str(path_value or "").strip()
            if not text:
                return ""
            if len(text) <= max_len:
                return text
            parts = text.replace("\\", "/").rstrip("/").split("/")
            if len(parts) >= 3:
                head = parts[0]
                tail = parts[-1]
                mid = "..."
                label = f"{head}/{mid}/{tail}"
                if len(label) > max_len:
                    return f"{text[:max_len - 3]}..."
                return label
            return f"{text[:max_len - 3]}..."

        def _render_detail_value(key: str, value: Any) -> Any:
            key_lower = str(key or "").strip().lower()
            if key_lower == "path" and _looks_like_local_path(value):
                full_path = str(value).strip()
                try:
                    file_uri = Path(full_path).as_uri()
                except Exception:
                    return str(value)
                label = _short_path_label(full_path)
                return _rich().Text(label, style=f"underline cyan link {file_uri}")
            if key_lower in {"path", "url"} and _looks_like_http_url(value):
                full_url = str(value).strip()
                label = _short_link_label(full_url)
                return _rich().Text(label, style=f"underline cyan link {full_url}")
            return str(value)

        def _lookup(*keys: str) -> Any:
            for key in keys:
                if key in self.item_metadata and _has_renderable_value(self.item_metadata.get(key)):
                    return self.item_metadata.get(key)
                lowered = key.lower()
                for existing, value in self.item_metadata.items():
                    if str(existing).lower() == lowered and _has_renderable_value(value):
                        return value
            return None

        def _first_url(value: Any) -> Optional[str]:
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    text = str(item or "").strip().strip("'\"")
                    if text:
                        return text
                return None
            text = str(value or "").strip()
            if text.startswith("[") and text.endswith("]"):
                inner = text[1:-1].strip().strip("'\"")
                return inner or None
            return text or None

        extras_map: Dict[str, List[Tuple[str, Any]]] = {}
        for extra in self.item_metadata.get("_detail_extras") or []:
            if isinstance(extra, dict):
                extra_label = extra.get("label") or extra.get("name")
                extra_value = extra.get("value")
                extra_after = extra.get("after") or "Hash"
            elif isinstance(extra, (list, tuple)) and len(extra) >= 2:
                extra_label, extra_value = extra[0], extra[1]
                extra_after = extra[2] if len(extra) > 2 else "Hash"
            else:
                continue
            if extra_label is None or not _has_renderable_value(extra_value):
                continue
            extras_map.setdefault(str(extra_after or "Hash").strip().lower(), []).append(
                (str(extra_label), extra_value)
            )
        used_after: Set[str] = set()

        def _add_extras(after_label: str, grid: Any) -> bool:
            key = str(after_label or "").strip().lower()
            rows = extras_map.get(key) or []
            added = False
            for extra_label, extra_value in rows:
                grid.add_row(f"{extra_label}:", _render_detail_value(extra_label, extra_value))
                added = True
            if key in extras_map:
                used_after.add(key)
            return added

        identity = _kv_grid()
        has_identity = False
        for label in ("Plugin", "Instance", "Path", "Hash"):
            val = _lookup(label)
            if _has_renderable_value(val):
                identity.add_row(f"{label}:", _render_detail_value(label, val))
                has_identity = True
            if _add_extras(label, identity):
                has_identity = True

        content = _kv_grid()
        has_content = False
        title_val = _lookup("Title")
        if _has_renderable_value(title_val):
            content.add_row("Title:", _rich().Text(str(title_val), style="bold"))
            has_content = True
        if _add_extras("Title", content):
            has_content = True
        url_val = _first_url(_lookup("Url"))
        if _has_renderable_value(url_val):
            content.add_row("Url:", _render_detail_value("Url", url_val))
            has_content = True
        if _add_extras("Url", content):
            has_content = True
        size_val = _lookup("Size")
        ext_val = _lookup("Ext")
        if size_val is not None and isinstance(size_val, (int, float, str)) and str(size_val).isdigit():
            try:
                size_val = _format_size(int(size_val), integer_only=False)
            except Exception:
                pass
        if _has_renderable_value(size_val) or _has_renderable_value(ext_val):
            size_line = _rich().Text()
            if _has_renderable_value(size_val):
                size_line.append(str(size_val), style=styles["value"])
            if _has_renderable_value(size_val) and _has_renderable_value(ext_val):
                size_line.append("  |  ", style="dim")
                size_line.append("Ext: ", style=styles["accent"])
                size_line.append(str(ext_val), style=styles["value"])
                content.add_row("Size:", size_line)
            elif _has_renderable_value(size_val):
                content.add_row("Size:", size_line)
            else:
                content.add_row("Ext:", str(ext_val))
            has_content = True
        if _add_extras("Size", content) or _add_extras("Ext", content):
            has_content = True

        for leftover in list(extras_map):
            if leftover in used_after:
                continue
            if _add_extras(leftover, identity):
                has_identity = True

        ns_grid = None
        freeform_grid = None
        tags = self.item_metadata.get("Tags") or self.item_metadata.get("tags") or self.item_metadata.get("tag")
        if not self.exclude_tags and tags:
            namespace_tags, freeform_tags = _partition_detail_tags(tags)
            if namespace_tags:
                ns_grid = _tag_grid(namespace_tags, 2, prefix="$")
            if freeform_tags:
                freeform_grid = _tag_grid(freeform_tags, 3, prefix="#")

        blocks: List[Any] = []
        if has_identity:
            blocks.append(identity)
        if has_content:
            if blocks:
                blocks.append(_rich().Text(""))
            blocks.append(content)
        if ns_grid is not None:
            if blocks:
                blocks.append(_rich().Text(""))
            blocks.append(ns_grid)
        if freeform_grid is not None:
            if blocks:
                blocks.append(_rich().Text(""))
            blocks.append(freeform_grid)
        has_details = bool(blocks)
        details_table = _rich().Group(*blocks) if blocks else _kv_grid()

        # 2. Get the standard table render (if there are rows or a specific title)
        original_title = self.title
        original_header_lines = self.header_lines
        self.title = ""
        self.header_lines = []
        
        results_renderable = None
        # We only show the results panel if there's data OR if the user explicitly set a title (cmdlet mode)
        if self.rows or original_title:
            self.title = original_title
            try:
                results_renderable = super().to_rich()
            finally:
                self.title = "" # Keep it clean for element assembly
                    
        # 3. Assemble components
        elements = []

        if has_details:
             detail_title = str(self.detail_title or "Item Details").strip() or "Item Details"
             elements.append(themed_panel(details_table, title=detail_title, padding=(1, 2)))

        if results_renderable:
            if isinstance(results_renderable, _rich().Panel):
                wrap = theme_styles()
                results_renderable.border_style = wrap["border"]
                results_renderable.style = wrap["panel"]
                if results_renderable.title:
                    results_renderable.title = _rich().Text(
                        str(results_renderable.title),
                        style=wrap["header"],
                    )
                elements.append(results_renderable)
            else:
                display_title = original_title or "Items"
                results_group = _rich().Group(_rich().Text(""), results_renderable, _rich().Text(""))
                elements.append(themed_panel(results_group, title=str(display_title)))

        return _rich().Group(*elements)
