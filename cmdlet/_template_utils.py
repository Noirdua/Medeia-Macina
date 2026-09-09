"""Template rendering and pipeline preview utilities.

Contains helpers for constructing pipeline progress previews and any
template-related utilities that don't belong in tag-specific modules.
"""

from __future__ import annotations

from typing import Any, List, Sequence

from SYS.item_accessors import get_field

__all__ = [
    "build_pipeline_preview",
]


def build_pipeline_preview(raw_urls: Sequence[str], piped_items: Sequence[Any]) -> List[str]:
    """Construct a short preview list for pipeline/cmdlet progress UI."""
    preview: List[str] = []

    try:
        for u in (raw_urls or [])[:3]:
            if u:
                preview.append(str(u))
    except Exception:
        pass

    if len(preview) < 5:
        try:
            for item in (piped_items or [])[:5]:
                if len(preview) >= 5:
                    break
                title = get_field(item, "title") or get_field(item, "target") or "Piped item"
                preview.append(str(title))
        except Exception:
            pass

    if not preview:
        total = len(raw_urls or []) + len(piped_items or [])
        if total:
            preview.append(f"Processing {total} item(s)...")

    return preview
