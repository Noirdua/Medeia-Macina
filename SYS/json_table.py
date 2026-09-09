"""Helper utilities for normalizing JSON result tables.

This mirrors the intent of the existing `SYS.html_table` helper but operates on
JSON payloads (API responses, JSON APIs, etc.). It exposes:

- `extract_records` for locating and normalizing the first list of record dicts
  from a JSON document.
- `normalize_record` for coercing arbitrary values into printable strings.

These helpers make it easy for providers that consume JSON to populate
`ResultModel` metadata without hand-writing ad-hoc sanitizers.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

_DEFAULT_LIST_KEYS: Tuple[str, ...] = ("results", "items", "docs", "records")


def _coerce_value(value: Any) -> str:
    """Convert a JSON value into a compact string representation."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        parts_list = [_coerce_value(v) for v in value]
        cleaned = [part for part in parts_list if part]
        return ", ".join(cleaned)
    if isinstance(value, dict):
        dict_parts: List[str] = []
        for subkey, subvalue in value.items():
            part = _coerce_value(subvalue)
            if part:
                dict_parts.append(f"{subkey}:{part}")
        return ", ".join(dict_parts)
    try:
        return str(value).strip()
    except Exception:
        return ""


def normalize_record(record: Dict[str, Any]) -> Dict[str, str]:
    """Return a copy of ``record`` with keys lowered and values coerced to strings."""
    out: Dict[str, str] = {}
    if not isinstance(record, dict):
        return out
    for key, value in record.items():
        normalized_key = str(key or "").strip().lower()
        if not normalized_key:
            continue
        normalized_value = _coerce_value(value)
        if normalized_value:
            out[normalized_key] = normalized_value
    return out


def _traverse(data: Any, path: Sequence[str]) -> Optional[Any]:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def extract_records(
    data: Any,
    *,
    path: Optional[Sequence[str]] = None,
    list_keys: Optional[Sequence[str]] = None,
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Extract normalized record dicts from ``data``.

    Args:
        data: JSON document (dict/list) that may contain tabular records.
        path: optional key path to traverse before looking for a list.
        list_keys: candidate keys to inspect when ``path`` is not provided.

    Returns:
        (records, chosen_path) where ``records`` is the list of normalized dicts
        and ``chosen_path`` is either the traversed path or the key that matched.
    """
    list_keys = list_keys or _DEFAULT_LIST_KEYS
    chosen_path: Optional[str] = None
    candidates: List[Any] = []

    if path:
        found = _traverse(data, path)
        if isinstance(found, list):
            candidates = found
            chosen_path = ".".join(path)

    if not candidates and isinstance(data, dict):
        for key in list_keys:
            found = data.get(key)
            if isinstance(found, list):
                candidates = found
                chosen_path = key
                break

    if not candidates and isinstance(data, list):
        candidates = data
        chosen_path = ""

    records: List[Dict[str, str]] = []
    for entry in candidates:
        if isinstance(entry, dict):
            records.append(normalize_record(entry))
    return records, chosen_path
