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
    """Translate tag-count predicates into Hydrus native system predicates."""
    tokens: List[str] = []
    for pred in predicates:
        if not pred.tag_count:
            continue
        op = _OP_DISPLAY.get(pred.op, pred.op)
        tokens.append(f"{_NATIVE_TAG_COUNT_FIELD} {op} {pred.value}")
    return tokens


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
