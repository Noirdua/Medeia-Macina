"""Inline query helpers for plugins (choice normalization and filter resolution)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _normalize_choice(entry: Any) -> Optional[Dict[str, Any]]:
    if entry is None:
        return None
    if isinstance(entry, dict):
        value = entry.get("value")
        text = entry.get("text") or entry.get("label") or value
        aliases = entry.get("alias") or entry.get("aliases") or []
        value_str = str(value) if value is not None else (str(text) if text is not None else None)
        text_str = str(text) if text is not None else value_str
        if not value_str or not text_str:
            return None
        alias_list = [str(a) for a in aliases if a is not None]
        return {"value": value_str, "text": text_str, "aliases": alias_list}
    return {"value": str(entry), "text": str(entry), "aliases": []}


def collect_choice(provider: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Collect normalized inline/query argument choice entries from a provider.

    Supports QUERY_ARG_CHOICES, INLINE_QUERY_FIELD_CHOICES, and the
    helper methods exposed by plugins (`query_field_choices` /
    `inline_query_field_choices`). Each choice is normalized to {value,text,aliases}.
    """

    mapping: Dict[str, List[Dict[str, Any]]] = {}

    def _ingest(source: Any, target_key: str) -> None:
        normalized: List[Dict[str, Any]] = []
        seq = source
        try:
            if callable(seq):
                seq = seq()
        except Exception:
            seq = source
        if isinstance(seq, dict):
            seq = seq.get("choices") or seq.get("values") or seq
        declared = isinstance(seq, (list, tuple, set))
        if declared:
            for entry in seq:
                n = _normalize_choice(entry)
                if n:
                    normalized.append(n)
        if declared:
            mapping[target_key] = normalized

    def _merge_mapping(source: Any) -> None:
        if not isinstance(source, dict):
            return
        for k, v in source.items():
            key_norm = str(k).strip().lower()
            if not key_norm:
                continue
            _ingest(v, key_norm)

    base = getattr(provider, "QUERY_ARG_CHOICES", None)
    if not isinstance(base, dict):
        base = getattr(provider, "INLINE_QUERY_FIELD_CHOICES", None)
    _merge_mapping(base)

    try:
        fn = getattr(provider, "query_field_choices", None)
        if callable(fn):
            _merge_mapping(fn())
    except Exception:
        pass

    try:
        fn = getattr(provider, "inline_query_field_choices", None)
        if callable(fn):
            _merge_mapping(fn())
    except Exception:
        pass

    return mapping


def resolve_filter(
    provider: Any,
    inline_args: Dict[str, Any],
    *,
    field_transforms: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Map inline query args to provider filter values using declared choices.

    - Uses provider choice mapping (value/text/aliases) to resolve user text.
    - Applies optional per-field transforms (e.g., str.upper).
    - Returns normalized filters suitable for provider.search.
    """

    filters: Dict[str, str] = {}
    if not inline_args:
        return filters

    mapping = collect_choice(provider)
    transforms = field_transforms or {}

    for raw_key, raw_val in inline_args.items():
        if raw_val is None:
            continue
        key = str(raw_key or "").strip().lower()
        val_str = str(raw_val).strip()
        if not key or not val_str:
            continue

        entries = mapping.get(key, [])
        resolved: Optional[str] = None
        val_lower = val_str.lower()
        for entry in entries:
            text = str(entry.get("text") or "").strip()
            value = str(entry.get("value") or "").strip()
            aliases = [str(a).strip() for a in entry.get("aliases", []) if a is not None]
            alias_lowers = {a.lower() for a in aliases}
            if val_lower in {text.lower(), value.lower()} or val_lower in alias_lowers:
                resolved = value or text or val_str
                break

        if resolved is None:
            resolved = val_str

        transform = transforms.get(key)
        if callable(transform):
            try:
                resolved = transform(resolved)
            except Exception:
                pass
        if resolved:
            filters[key] = str(resolved)

    return filters
