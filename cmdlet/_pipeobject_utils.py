"""PipeObject utilities for chainable cmdlet and multi-action pipelines.

Contains helpers for creating, normalizing, mutating, and extracting data from
PipeObject instances and result dicts. Also includes the canonical
``merge_sequences`` dedup utility used across cmdlets.
"""

from __future__ import annotations

from collections.abc import Iterable as IterableABC
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from SYS import models
from SYS.item_accessors import get_field as _item_accessor_get_field
from SYS.payload_builders import build_file_result_payload
from ._store_utils import resolve_hash_for_cmdlet

__all__ = [
    "get_field",
    "merge_sequences",
    "normalize_result_input",
    "normalize_result_items",
    "value_has_content",
    "filter_results_by_temp",
    "create_pipe_object_result",
    "mark_as_temp",
    "set_parent_hash",
    "get_pipe_object_path",
    "get_pipe_object_hash",
    "coerce_to_pipe_object",
    "resolve_item_store_hash",
    "propagate_metadata",
    "extract_relationships",
    "extract_duration",
    "pipeline_item_local_path",
]


def get_field(obj: Any, field: str, default: Optional[Any] = None) -> Any:
    """Extract a field from either a dict or object with fallback default.

    Handles both dict.get(field) and getattr(obj, field) access patterns.
    Also handles lists by accessing the first element.
    For PipeObjects, checks the extra field as well.
    Used throughout cmdlet to uniformly access fields from mixed types.

    Args:
            obj: Dict, object, or list to extract from
            field: Field name to retrieve
            default: Value to return if field not found (default: None)

    Returns:
            Field value if found, otherwise the default value

    Examples:
            get_field(result, "hash")          # From dict or object
            get_field(result, "table", "unknown")  # With default
    """
    return _item_accessor_get_field(obj, field, default)


def merge_sequences(*sources: Optional[Iterable[Any]],
                    case_sensitive: bool = True) -> list[str]:
    """Merge iterable sources while preserving order and removing duplicates."""
    seen: set[str] = set()
    merged: list[str] = []
    for source in sources:
        if not source:
            continue
        if isinstance(source, str) or not isinstance(source, IterableABC):
            iterable = [source]
        else:
            iterable = source
        for value in iterable:
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            key = text if case_sensitive else text.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(text)
    return merged


def normalize_result_input(result: Any) -> List[Dict[str, Any]]:
    """Normalize input result to a list of dicts.

    Handles:
    - None -> []
    - Dict -> [dict]
    - List of dicts -> list as-is
    - PipeObject -> [dict]
    - List of PipeObjects -> list of dicts

    Args:
            result: Result from piped input

    Returns:
            List of result dicts (may be empty)
    """
    if result is None:
        return []

    if isinstance(result, dict):
        return [result]

    if isinstance(result, list):
        output = []
        for item in result:
            if isinstance(item, dict):
                output.append(item)
            elif hasattr(item, "to_dict"):
                output.append(item.to_dict())
            else:
                output.append(item)
        return output

    if hasattr(result, "to_dict"):
        return [result.to_dict()]

    if isinstance(result, dict):
        return [result]

    return []


def normalize_result_items(
    result: Any,
    *,
    include_falsey_single: bool = False,
) -> List[Any]:
    """Normalize piped input to a raw item list without converting item types."""
    if isinstance(result, list):
        return list(result)
    if result is None:
        return []
    if include_falsey_single or result:
        return [result]
    return []


def value_has_content(value: Any) -> bool:
    """Return True when a value should be treated as present for payload building."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return len(value) > 0
    return True


def filter_results_by_temp(results: List[Any], include_temp: bool = False) -> List[Any]:
    """Filter results by temporary status.

    Args:
            results: List of result dicts or PipeObjects
            include_temp: If True, keep temp files; if False, exclude them

    Returns:
            Filtered list
    """
    if include_temp:
        return results

    filtered = []
    for result in results:
        is_temp = False

        if hasattr(result, "is_temp"):
            is_temp = result.is_temp
        elif isinstance(result, dict):
            is_temp = result.get("is_temp", False)

        if not is_temp:
            filtered.append(result)

    return filtered


def create_pipe_object_result(
    source: str,
    identifier: str,
    file_path: str,
    cmdlet_name: str,
    title: Optional[str] = None,
    hash_value: Optional[str] = None,
    is_temp: bool = False,
    parent_hash: Optional[str] = None,
    tag: Optional[List[str]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Create a PipeObject-compatible result dict for pipeline chaining.

    This is a helper to emit results in the standard format that downstream
    cmdlet can process (filter, tag, cleanup, etc.).

    Args:
            source: Source system (e.g., 'local', 'hydrus', 'download')
            identifier: Unique ID from source
            file_path: Path to the file
            cmdlet_name: Name of the cmdlet that created this (e.g., 'download-data', 'screen-shot')
            title: Human-readable title
            hash_value: SHA-256 hash of file (for integrity)
            is_temp: If True, this is a temporary/intermediate artifact
            parent_hash: Hash of the parent file in the chain (for provenance)
            tag: List of tag values to apply
            **extra: Additional fields

    Returns:
            Dict with all PipeObject fields for emission
    """
    store_override = extra.pop("store", None)

    result = build_file_result_payload(
        title=title,
        path=file_path,
        hash_value=hash_value,
        store=store_override if store_override is not None else source,
        tag=tag,
        source=source,
        id=identifier,
        action=f"cmdlet:{cmdlet_name}",
        **extra,
    )

    if is_temp:
        result["is_temp"] = True
    if parent_hash:
        result["parent_hash"] = parent_hash

    return result


def mark_as_temp(pipe_object: Dict[str, Any]) -> Dict[str, Any]:
    """Mark a PipeObject dict as temporary (intermediate artifact).

    Args:
            pipe_object: Result dict from cmdlet emission

    Returns:
            Modified dict with is_temp=True
    """
    pipe_object["is_temp"] = True
    return pipe_object


def set_parent_hash(pipe_object: Dict[str, Any], parent_hash: str) -> Dict[str, Any]:
    """Set the parent_hash for provenance tracking.

    Args:
            pipe_object: Result dict
            parent_hash: Parent file's hash

    Returns:
            Modified dict with parent_hash set to the hash
    """
    pipe_object["parent_hash"] = parent_hash
    return pipe_object


def get_pipe_object_path(pipe_object: Any) -> Optional[str]:
    """Extract file path from PipeObject, dict, or pipeline-friendly object."""
    if pipe_object is None:
        return None
    for attr in ("path", "target"):
        if hasattr(pipe_object, attr):
            value = getattr(pipe_object, attr)
            if value:
                return value
    if isinstance(pipe_object, dict):
        for key in ("path", "target"):
            value = pipe_object.get(key)
            if value:
                return value
    return None


def get_pipe_object_hash(pipe_object: Any) -> Optional[str]:
    """Extract file hash from PipeObject, dict, or pipeline-friendly object."""
    if pipe_object is None:
        return None
    for attr in ("hash",):
        if hasattr(pipe_object, attr):
            value = getattr(pipe_object, attr)
            if value:
                return value
    if isinstance(pipe_object, dict):
        for key in ("hash",):
            value = pipe_object.get(key)
            if value:
                return value
    return None


def resolve_item_store_hash(
    item: Any,
    *,
    override_store: Optional[str] = None,
    override_hash: Optional[str] = None,
    hash_field: str = "hash",
    store_field: str = "store",
    path_fields: Sequence[str] = ("path", "target"),
) -> Tuple[str, Optional[str]]:
    """Resolve store name and normalized hash from a result item."""
    store_name = str(override_store or get_field(item, store_field) or "").strip()
    raw_hash = get_field(item, hash_field)

    raw_path = None
    for field_name in path_fields:
        candidate = get_field(item, field_name)
        if candidate:
            raw_path = candidate
            break

    resolved_hash = resolve_hash_for_cmdlet(
        str(raw_hash) if raw_hash else None,
        str(raw_path) if raw_path else None,
        str(override_hash) if override_hash else None,
    )
    return store_name, resolved_hash


def coerce_to_pipe_object(
    value: Any,
    default_path: Optional[str] = None,
) -> models.PipeObject:
    """Normalize any incoming result to a PipeObject for single-source-of-truth state.

    Uses hash+store canonical pattern.
    """
    if isinstance(value, models.PipeObject):
        return value

    known_keys = {
        "hash",
        "store",
        "tag",
        "title",
        "url",
        "source_url",
        "duration",
        "metadata",
        "warnings",
        "path",
        "relationships",
        "is_temp",
        "action",
        "parent_hash",
    }

    if hasattr(value, "to_dict"):
        value = value.to_dict()
    elif not isinstance(value, dict):
        try:
            obj_map: Dict[str, Any] = {}
            for k in (
                    "hash",
                    "store",
                    "provider",
                    "prov",
                    "tag",
                    "title",
                    "url",
                    "source_url",
                    "duration",
                    "duration_seconds",
                    "metadata",
                    "full_metadata",
                    "warnings",
                    "path",
                    "target",
                    "relationships",
                    "is_temp",
                    "action",
                    "parent_hash",
                    "extra",
                    "media_kind",
            ):
                if hasattr(value, k):
                    obj_map[k] = getattr(value, k)
            if obj_map:
                value = obj_map
        except Exception:
            pass

    if isinstance(value, dict):
        hash_val = value.get("hash") or value.get("hash_hex") or value.get("file_hash")
        store_val = value.get("store") or value.get("instance") or "PATH"
        if not store_val or store_val == "PATH":
            try:
                extra_store = value.get("extra", {}).get("store")
            except Exception:
                extra_store = None
            if extra_store:
                store_val = extra_store

        if not hash_val:
            path_val = value.get("path")
            if path_val:
                try:
                    from SYS.utils import sha256_file

                    hash_val = sha256_file(Path(path_val))
                except Exception:
                    hash_val = "unknown"
            else:
                hash_val = "unknown"

        title_val = value.get("title")
        if not title_val:
            path_val = value.get("path")
            if path_val:
                try:
                    title_val = Path(path_val).stem
                except Exception:
                    pass

        extra = {
            k: v
            for k, v in value.items() if k not in known_keys
        }

        from SYS.metadata import normalize_urls

        url_list = normalize_urls(value.get("url"))
        url_val = url_list[0] if url_list else None
        if len(url_list) > 1:
            extra["url"] = url_list

        rels = value.get("relationships") or {}

        tag_val: list[str] = []
        if "tag" in value:
            raw_tag = value["tag"]
            if isinstance(raw_tag, list):
                tag_val = [str(t) for t in raw_tag if t is not None]
            elif isinstance(raw_tag, str):
                tag_val = [raw_tag]

        path_val = value.get("path")
        if not path_val and "target" in value:
            target = value["target"]
            if target and not (isinstance(target, str) and (target.startswith("http://")
                                                            or target.startswith("https://"))):
                path_val = target

        try:
            if isinstance(path_val, str) and (path_val.startswith("http://")
                                              or path_val.startswith("https://")):
                if not url_val:
                    url_val = path_val
                path_val = None
        except Exception:
            pass

        if "media_kind" in value:
            extra["media_kind"] = value["media_kind"]

        pipe_obj = models.PipeObject(
            hash=hash_val,
            store=store_val,
            plugin=str(
                value.get("plugin")
                or value.get("prov")
                or value.get("source")
                or extra.get("plugin")
                or extra.get("source")
                or ""
            ).strip() or None,
            tag=tag_val,
            title=title_val,
            url=url_val,
            source_url=value.get("source_url"),
            duration=value.get("duration") or value.get("duration_seconds"),
            metadata=value.get("metadata") or value.get("full_metadata") or {},
            warnings=list(value.get("warnings") or []),
            path=path_val,
            relationships=rels,
            is_temp=bool(value.get("is_temp", False)),
            action=value.get("action"),
            parent_hash=value.get("parent_hash"),
            extra=extra,
        )

        return pipe_obj

    hash_val = "unknown"
    path_val = default_path or getattr(value, "path", None)
    url_val: Optional[str] = None
    title_val = None

    if isinstance(value, str):
        s = value.strip()
        if s.lower().startswith(("http://", "https://")):
            url_val = s
            path_val = None
        else:
            path_val = s

    if path_val and path_val != "unknown":
        try:
            from SYS.utils import sha256_file

            path_obj = Path(path_val)
            hash_val = sha256_file(path_obj)
            title_val = path_obj.stem
        except Exception:
            pass

    store_val = "URL" if url_val else "PATH"

    pipe_obj = models.PipeObject(
        hash=hash_val,
        store=store_val,
        plugin=None,
        path=str(path_val) if path_val and path_val != "unknown" else None,
        title=title_val,
        url=url_val,
        source_url=url_val,
        tag=[],
        extra={},
    )

    return pipe_obj


def propagate_metadata(
    previous_items: Sequence[Any],
    new_items: Sequence[Any],
) -> List[Any]:
    """Merge metadata/tags from previous pipeline stage into new items.

    Implements "sticky metadata": items generated by a transformation (download, convert)
    should inherit rich info (lyrics, art, tags) from their source.

    Strategies:
    A. Hash Match: If inputs/outputs share a hash, they are the same item.
    B. Index Match: If lists are same length, assume 1:1 mapping (heuristic).
    C. Explicit Parent: If output has `parent_hash`, link to input with that hash.
    """
    if not previous_items or not new_items:
        return list(new_items)

    try:
        prev_normalized = [coerce_to_pipe_object(p) for p in previous_items]
    except Exception:
        return list(new_items)

    prev_by_hash: Dict[str, models.PipeObject] = {}
    for p_obj in prev_normalized:
        if p_obj.hash and p_obj.hash != "unknown":
            prev_by_hash[p_obj.hash] = p_obj

    normalized: List[Any] = []

    is_same_length = len(new_items) == len(prev_normalized)

    for i, item in enumerate(new_items):
        if isinstance(item, dict) and item.get("_skip_metadata_propagation"):
            normalized.append(item)
            continue
        try:
            obj = coerce_to_pipe_object(item)
        except Exception:
            normalized.append(item)
            continue

        parent: Optional[models.PipeObject] = None

        if obj.hash in prev_by_hash:
            parent = prev_by_hash[obj.hash]

        if not parent and is_same_length:
            parent = prev_normalized[i]

        if not parent and obj.parent_hash and obj.parent_hash in prev_by_hash:
            parent = prev_by_hash[obj.parent_hash]

        if parent:
            if parent.tag:
                if not obj.tag:
                    obj.tag = list(parent.tag)
                else:
                    curr_tags = {str(t).lower() for t in obj.tag}
                    for pt in parent.tag:
                        if str(pt).lower() not in curr_tags:
                            obj.tag.append(pt)

            if parent.metadata:
                if not obj.metadata:
                    obj.metadata = parent.metadata.copy()
                else:
                    for mk, mv in parent.metadata.items():
                        if mk not in obj.metadata:
                            obj.metadata[mk] = mv

            if parent.source_url and not obj.source_url:
                obj.source_url = parent.source_url
            elif parent.url and not obj.source_url and not obj.url:
                obj.source_url = parent.url

            if parent.relationships:
                if not obj.relationships:
                    obj.relationships = parent.relationships.copy()
                else:
                    for rk, rv in parent.relationships.items():
                        if rk not in obj.relationships:
                            obj.relationships[rk] = rv

            if parent.extra:
                if not obj.extra:
                    obj.extra = parent.extra.copy()
                else:
                    for ek, ev in parent.extra.items():
                        if ek not in obj.extra:
                            obj.extra[ek] = ev
                        elif ek == "notes" and isinstance(ev, dict) and isinstance(obj.extra[ek], dict):
                            for nk, nv in ev.items():
                                if nk not in obj.extra[ek]:
                                    obj.extra[ek][nk] = nv

        normalized.append(obj)

    return normalized


def pipeline_item_local_path(item: Any) -> Optional[str]:
    """Extract local file path from a pipeline item.

    Supports both dataclass objects with .path attribute and dicts.
    Returns None for HTTP/HTTPS url.

    Args:
            item: Pipeline item (PipelineItem dataclass, dict, or other)

    Returns:
            Local file path string, or None if item is not a local file
    """
    path_value: Optional[str] = None
    if hasattr(item, "path"):
        path_value = getattr(item, "path", None)
    elif isinstance(item, dict):
        raw = item.get("path") or item.get("url")
        path_value = str(raw) if raw is not None else None
    if not isinstance(path_value, str):
        return None
    text = path_value.strip()
    if not text:
        return None
    if text.lower().startswith(("http://", "https://")):
        return None
    return text


def extract_relationships(result: Any) -> Optional[Dict[str, Any]]:
    if isinstance(result, models.PipeObject):
        relationships = result.get_relationships()
        return relationships or None
    if isinstance(result, dict):
        relationships = result.get("relationships")
        if isinstance(relationships, dict) and relationships:
            return relationships
    return None


def extract_duration(result: Any) -> Optional[float]:
    duration = None
    if isinstance(result, models.PipeObject):
        duration = result.duration
    elif isinstance(result, dict):
        duration = result.get("duration")
        if duration is None:
            metadata = result.get("metadata")
            if isinstance(metadata, dict):
                duration = metadata.get("duration")
    if duration is None:
        return None
    try:
        return float(duration)
    except (TypeError, ValueError):
        return None
