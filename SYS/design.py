from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

_DEFAULT_MM_PREFIX = "[mm]"
_DEFAULT_QUOTE = "He who is victorious through deceit is defeated by the truth."
_DEFAULT_KAPPA_COLORS = [
    "red",
    "dark_orange",
    "yellow",
    "green",
    "blue",
    "purple",
    "magenta",
]
_DEFAULT_KAPPA_MD = """
# ******************    Medios Macina    ******************
take what you want | keep what you like | share what you love
_____________________________________________________________
_____________________________________________________________
_____________________________________________________________
For suddenly you may be let loose from the net, and thrown out to sea.
Waving around clutching at gnats, unable to lift the heavy anchor. Lost
and without a map, forgotten things from the past by distracting wind storms.
_____________________________________________________________
_____________________________________________________________
_____________________________________________________________
Light shines a straight path to the golden shores.
Come to love it when others take what you share, as there is no greater joy
"""


def design_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "design"


def _read_text(name: str, default: str = "") -> str:
    path = design_dir() / name
    try:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except Exception:
        return default


def _first_content_line(text: str, default: str) -> str:
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped
    return default


def _split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    raw = str(text or "").lstrip("\ufeff")
    if not raw.startswith("---"):
        return {}, raw
    rest = raw[3:]
    if rest.startswith("\n"):
        rest = rest[1:]
    end = rest.find("\n---")
    if end < 0:
        return {}, raw
    block = rest[:end]
    body = rest[end + 4 :]
    if body.startswith("\n"):
        body = body[1:]
    return _parse_simple_yaml(block), body


def _parse_simple_yaml(block: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    current_list: str | None = None
    items: List[str] = []
    for raw_line in str(block or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        if current_list and line.lstrip().startswith("- "):
            items.append(line.lstrip()[2:].strip().strip('"').strip("'"))
            continue
        if current_list:
            data[current_list] = items
            current_list = None
            items = []
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if not value:
            current_list = key
            items = []
            continue
        data[key] = value.strip('"').strip("'")
    if current_list:
        data[current_list] = items
    return data


def load_mm_prefix() -> str:
    return _first_content_line(_read_text("mm.md"), _DEFAULT_MM_PREFIX) or _DEFAULT_MM_PREFIX


def load_quote() -> str:
    lines = [
        line.rstrip()
        for line in _read_text("quote.md", _DEFAULT_QUOTE).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    text = "\n".join(lines).strip()
    return text or _DEFAULT_QUOTE


def load_kappa() -> Dict[str, Any]:
    meta, markdown = _split_frontmatter(_read_text("kappa.md", _DEFAULT_KAPPA_MD))
    colors = meta.get("colors")
    if not isinstance(colors, list) or not colors:
        colors = list(_DEFAULT_KAPPA_COLORS)
    colors = [str(item).strip() for item in colors if str(item).strip()]
    if not str(markdown or "").strip():
        markdown = _DEFAULT_KAPPA_MD
    return {
        "title_left": str(meta.get("title_left") or "DELTA"),
        "title_center": str(meta.get("title_center") or "KAPPA"),
        "title_right": str(meta.get("title_right") or "LAMBDA"),
        "height": int(meta.get("height") or 21),
        "bar_width": int(meta.get("bar_width") or 36),
        "left_ratio": int(meta.get("left_ratio") or 2),
        "center_ratio": int(meta.get("center_ratio") or 8),
        "right_ratio": int(meta.get("right_ratio") or 2),
        "colors": colors or list(_DEFAULT_KAPPA_COLORS),
        "markdown": markdown,
    }
