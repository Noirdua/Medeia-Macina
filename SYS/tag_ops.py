from __future__ import annotations

from typing import Any, Dict, List, Sequence, Set, Tuple

UNIQUE_TAG_NAMESPACES = frozenset({"title"})


def compute_namespaced_tag_overwrite(
    existing_tags: Sequence[Any],
    incoming_tags: Sequence[Any],
) -> Tuple[List[str], List[str], List[str]]:
    def _clean(values: Sequence[Any]) -> List[str]:
        out: List[str] = []
        for value in values or []:
            if not isinstance(value, str):
                continue
            text = value.strip()
            if text:
                out.append(text.lower())
        return out

    def _ns_of(tag: str) -> str:
        if ":" not in tag:
            return ""
        return tag.split(":", 1)[0].strip().lower()

    existing = _clean(existing_tags)
    incoming = _clean(incoming_tags)
    if not incoming:
        return [], [], existing

    unique_last: Dict[str, str] = {}
    collapsed_incoming: List[str] = []
    for tag in incoming:
        namespace = _ns_of(tag)
        if namespace in UNIQUE_TAG_NAMESPACES:
            unique_last[namespace] = tag
            continue
        collapsed_incoming.append(tag)
    collapsed_incoming.extend(unique_last.values())
    incoming = collapsed_incoming

    namespaces_to_replace: Set[str] = set()
    for tag in incoming:
        namespace = _ns_of(tag)
        if namespace:
            namespaces_to_replace.add(namespace)

    kept_existing: List[str] = []
    kept_existing_lower: Set[str] = set()
    tags_to_remove: List[str] = []
    for tag in existing:
        namespace = _ns_of(tag)
        if namespace and namespace in namespaces_to_replace:
            if tag in incoming:
                kept_existing.append(tag)
                kept_existing_lower.add(tag.lower())
            else:
                tags_to_remove.append(tag)
            continue
        kept_existing.append(tag)
        kept_existing_lower.add(tag.lower())

    tags_to_add: List[str] = []
    added_lower: Set[str] = set()
    for tag in incoming:
        lowered = tag.lower()
        if lowered in kept_existing_lower or lowered in added_lower:
            continue
        tags_to_add.append(tag)
        added_lower.add(lowered)

    return tags_to_remove, tags_to_add, kept_existing + tags_to_add
