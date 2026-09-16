"""System-level search predicates such as ``#tags<=3``.

Normal search predicates describe tags (``artist:foo``). System predicates
describe a property of the item itself and use a leading ``#`` so they are
styled differently and translated to the backend's native syntax:

    #tags<=3     three or fewer tags   -> system:number of tags <= 3
    #tags>10     more than ten tags    -> system:number of tags > 10
    #tags=0      untagged items        -> system:number of tags = 0

Hydrus understands ``system:number of tags`` natively, so the predicate is
evaluated server-side. Other plugins are expected to handle the native
``system:`` token themselves (or use their own metadata/tag path). Unknown
``#`` tokens are left in the query untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple


_TAG_COUNT_FIELDS = {
    "tag",
    "tags",
    "tagcount",
    "tag_count",
    "tagamount",
    "tag_amount",
}

_SYSTEM_PREDICATE_RE = re.compile(
    r"(?:^|[\s,])#(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?P<op><=|>=|!=|==|=|<|>)\s*(?P<value>\d+)(?=[\s,]|$)",
)

_OP_DISPLAY = {"==": "="}

_NATIVE_TAG_COUNT_FIELD = "system:number of tags"
_NATIVE_UNTAGGED = "system:untagged"
_NATIVE_HAS_TAGS = "system:has tags"

# System predicate syntax is rendered in a distinct color from tag predicates.
SYSTEM_PREDICATE_STYLE = "bold magenta"


@dataclass(frozen=True)
class SystemPredicate:
    """A parsed ``#field<op>value`` system predicate."""

    field: str
    op: str
    value: int

    @property
    def tag_count(self) -> bool:
        return self.field in _TAG_COUNT_FIELDS

    @property
    def display(self) -> str:
        return f"#{self.field}{_OP_DISPLAY.get(self.op, self.op)}{self.value}"


def parse_system_predicates(query: Any) -> Tuple[str, List[SystemPredicate]]:
    """Split ``#system`` predicates out of a query.

    Returns ``(cleaned_query, predicates)``. When nothing matches, the original
    query string is returned unchanged.
    """
    text = str(query or "")
    if "#" not in text:
        return text, []

    predicates: List[SystemPredicate] = []
    replaced = False

    def _take(match: "re.Match[str]") -> str:
        nonlocal replaced
        field = str(match.group("field") or "").strip().lower()
        if field not in _TAG_COUNT_FIELDS:
            return match.group(0)
        replaced = True
        predicates.append(
            SystemPredicate(
                field=field,
                op=str(match.group("op") or "=").strip(),
                value=int(match.group("value")),
            )
        )
        return " "

    cleaned = _SYSTEM_PREDICATE_RE.sub(_take, text)
    if not replaced:
        return text, []

    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip().strip(",").strip()
    return cleaned, predicates


def to_native_predicates(predicates: Sequence[SystemPredicate]) -> List[str]:
    """Translate tag-count predicates into Hydrus native system predicates.

    Hydrus's ``number of tags`` parser accepts ``< > = != ~=`` but not
    ``<=``/``>=``, so inclusive bounds shift to the strict neighbour. Zero/any
    counts use the dedicated ``system:untagged`` / ``system:has tags`` forms.
    Predicates that are always true (e.g. ``>= 0``) emit no token.
    """
    tokens: List[str] = []
    for pred in predicates:
        if not pred.tag_count:
            continue
        token = _native_tag_count_predicate(
            _OP_DISPLAY.get(pred.op, pred.op), pred.value
        )
        if token:
            tokens.append(token)
    return tokens


def _native_tag_count_predicate(op: str, value: int) -> Optional[str]:
    if value == 0:
        if op in {"=", "<="}:
            return _NATIVE_UNTAGGED
        if op in {">", "!="}:
            return _NATIVE_HAS_TAGS
        if op == ">=":
            return None  # at least zero tags: always true
        return f"{_NATIVE_TAG_COUNT_FIELD} < 0"  # impossible (cannot be negative)
    if value == 1:
        if op == "<":
            return _NATIVE_UNTAGGED
        if op == ">=":
            return _NATIVE_HAS_TAGS
    if op == "<=":
        return f"{_NATIVE_TAG_COUNT_FIELD} < {value + 1}"
    if op == ">=":
        return f"{_NATIVE_TAG_COUNT_FIELD} > {value - 1}"
    return f"{_NATIVE_TAG_COUNT_FIELD} {op} {value}"


def render_system_predicate_line(
    predicates: Sequence[SystemPredicate],
    *,
    style: str = SYSTEM_PREDICATE_STYLE,
) -> Optional[Any]:
    """Build a colored Rich line describing the active system predicates."""
    active = [pred for pred in predicates if pred.tag_count]
    if not active:
        return None

    from rich.text import Text

    line = Text()
    line.append("system ", style="dim")
    line.append("  ".join(pred.display for pred in active), style=style)
    return line


# --- Completion -----------------------------------------------------------

SYSTEM_PREDICATE_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("tags", "number of tags"),
    ("tag", "number of tags"),
    ("tag_count", "number of tags"),
    ("tagcount", "number of tags"),
    ("tag_amount", "number of tags"),
)

SYSTEM_PREDICATE_OPS: Tuple[Tuple[str, str], ...] = (
    ("<=", "at most N tags"),
    (">=", "at least N tags"),
    ("=", "exactly N tags"),
    ("<", "fewer than N tags"),
    (">", "more than N tags"),
    ("!=", "not N tags"),
)

SYSTEM_PREDICATE_VALUES: Tuple[str, ...] = ("0", "1", "2", "3", "5", "10", "20", "50")

_SYSTEM_FRAGMENT_RE = re.compile(r"^#([A-Za-z_][A-Za-z0-9_]*)?([<>=!]*)(\d*),?$")


def _value_helper(op: str, value: str) -> str:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return ""
    noun = "tag" if count == 1 else "tags"
    if op in {"=", "=="}:
        return "no tags" if count == 0 else f"{count} {noun}"
    if op == "<":
        return "no tags" if count <= 1 else f"fewer than {count} tags"
    if op == "<=":
        return "no tags" if count == 0 else f"at most {count} {noun}"
    if op == ">":
        return "has tags" if count == 0 else f"more than {count} {noun}"
    if op == ">=":
        return "has tags" if count == 0 else f"at least {count} {noun}"
    return f"{count} {noun}"


def parse_system_fragment(fragment: Any) -> Optional[Tuple[str, str, str]]:
    """Split a partial ``#tags<=3`` token into ``(field, op, digits)``."""
    match = _SYSTEM_FRAGMENT_RE.match(str(fragment or "").strip())
    if not match:
        return None
    return (match.group(1) or "", match.group(2) or "", match.group(3) or "")


def iter_system_completions(fragment: Any) -> List[Tuple[str, str]]:
    """Return ``(text, helper)`` completion pairs for a partial system token."""
    parsed = parse_system_fragment(fragment)
    if parsed is None:
        return []

    field, op, _digits = parsed

    if not field:
        return [(f"#{name}", helper) for name, helper in SYSTEM_PREDICATE_FIELDS]

    if field not in _TAG_COUNT_FIELDS:
        matches = [
            (f"#{name}", helper)
            for name, helper in SYSTEM_PREDICATE_FIELDS
            if name.startswith(field)
        ]
        return matches

    if not op:
        return [(f"#{field}{token}", helper) for token, helper in SYSTEM_PREDICATE_OPS]

    return [(f"#{field}{op}{value}", _value_helper(op, value)) for value in SYSTEM_PREDICATE_VALUES]


