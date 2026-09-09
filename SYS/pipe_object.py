from __future__ import annotations

from typing import Any, Dict, Optional

from SYS import models


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

    # Convert common object-like results into a dict so we can preserve fields like
    # hash/store/url when they come from result tables (e.g., get-url emits UrlItem).
    #
    # Priority:
    # 1) explicit to_dict()
    # 2) best-effort attribute extraction for known PipeObject-ish fields
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
        # Extract hash and store (canonical identifiers)
        hash_val = value.get("hash") or value.get("hash_hex") or value.get("file_hash")
        store_val = value.get("store") or value.get("instance") or "PATH"
        if not store_val or store_val == "PATH":
            try:
                extra_store = value.get("extra", {}).get("store")
            except Exception:
                extra_store = None
            if extra_store:
                store_val = extra_store

        # If no hash, try to compute from path or use placeholder
        if not hash_val:
            path_val = value.get("path")
            if path_val:
                try:
                    from pathlib import Path

                    from SYS.utils import sha256_file

                    hash_val = sha256_file(Path(path_val))
                except Exception:
                    hash_val = "unknown"
            else:
                hash_val = "unknown"

        # Extract title from filename if not provided
        title_val = value.get("title")
        if not title_val:
            path_val = value.get("path")
            if path_val:
                try:
                    from pathlib import Path

                    title_val = Path(path_val).stem
                except Exception:
                    pass

        extra = {k: v for k, v in value.items() if k not in known_keys}

        # Extract URL: prefer direct url field, then url list
        from SYS.metadata import normalize_urls

        url_list = normalize_urls(value.get("url"))
        url_val = url_list[0] if url_list else None
        if len(url_list) > 1:
            extra["url"] = url_list

        # Extract relationships
        rels = value.get("relationships") or {}

        # Canonical tag: accept list or single string
        tag_val: list[str] = []
        if "tag" in value:
            raw_tag = value["tag"]
            if isinstance(raw_tag, list):
                tag_val = [str(t) for t in raw_tag if t is not None]
            elif isinstance(raw_tag, str):
                tag_val = [raw_tag]

        # Consolidate path: prefer explicit path key, but NOT target if it's a URL
        path_val = value.get("path")
        # Only use target as path if it's not a URL (url should stay in url field)
        if not path_val and "target" in value:
            target = value["target"]
            if target and not (
                isinstance(target, str)
                and (target.startswith("http://") or target.startswith("https://"))
            ):
                path_val = target

        # If the path value is actually a URL, move it to url_val and clear path_val
        try:
            if isinstance(path_val, str) and (
                path_val.startswith("http://") or path_val.startswith("https://")
            ):
                # Prefer existing url_val if present, otherwise move path_val into url_val
                if not url_val:
                    url_val = path_val
                path_val = None
        except Exception:
            pass

        # Extract media_kind if available
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
            ).strip()
            or None,
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

    # Fallback: build from path argument or bare value
    hash_val = "unknown"
    path_val = default_path or getattr(value, "path", None)
    url_val: Optional[str] = None
    title_val = None

    # If the raw value is a string, treat it as either a URL or a file path.
    # This is important for @-selection results that are plain URL strings.
    if isinstance(value, str):
        s = value.strip()
        if s.lower().startswith(("http://", "https://")):
            url_val = s
            path_val = None
        else:
            path_val = s

    if path_val and path_val != "unknown":
        try:
            from pathlib import Path

            from SYS.utils import sha256_file

            path_obj = Path(path_val)
            hash_val = sha256_file(path_obj)
            # Extract title from filename (without extension)
            title_val = path_obj.stem
        except Exception:
            pass

    # When coming from a raw URL string, mark it explicitly as URL.
    # Otherwise treat it as a local path.
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
