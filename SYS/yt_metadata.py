import re
from typing import Any, Dict, List, Optional, Set


def value_normalize(value: Any) -> str:
    text = str(value).strip()
    return text.lower() if text else ""


def _add_tag(tags: List[str], namespace: str, value: str) -> None:
    """Add a namespaced tag if not already present."""
    if not namespace or not value:
        return
    normalized_value = value_normalize(value)
    if not normalized_value:
        return
    candidate = f"{namespace}:{normalized_value}"
    if candidate not in tags:
        tags.append(candidate)


def _extract_channel_from_tag(tag_value: str) -> Optional[str]:
    """Return the channel value if tag_value is namespaced with channel."""
    if not tag_value:
        return None
    normalized = tag_value.strip().lower()
    if not normalized.startswith("channel:"):
        return None
    _, _, remainder = normalized.partition(":")
    remainder = remainder.strip()
    return remainder or None


def extract_ytdlp_tags(entry: Dict[str, Any]) -> List[str]:
    """ """
    tags: List[str] = []
    seen_namespaces: Set[str] = set()

    # Meaningful yt-dlp fields that should become tags
    # This mapping excludes technical fields: filesize, duration, format_id, vcodec, acodec, ext, etc.
    field_to_namespace = {
        "artist": "artist",
        "album": "album",
        "creator": "creator",
        "uploader": "creator",  # Map uploader to creator (deduplicate)
        "uploader_id": "creator",
        "channel": "channel",
        "genre": "genre",
        "track": "track",
        "track_number": "track_number",
        "release_date": "release_date",
        "upload_date": "upload_date",
        "title": "title",
        "license": "license",
        "location": "location",
    }

    # Extract simple field mappings
    for yt_field, namespace in field_to_namespace.items():
        value = entry.get(yt_field)
        if value is not None:
            value_str = value_normalize(str(value))
            if value_str:
                # Prevent duplicate creator tags (only use first creator)
                if namespace == "creator":
                    if "creator" in seen_namespaces:
                        continue
                    seen_namespaces.add("creator")

                _add_tag(tags, namespace, value_str)

    # Handle tags field specially (could be list, dict, or string)
    # For list/sequence tags, capture as freeform (no namespace prefix)
    tags_field = entry.get("tags")
    if tags_field is not None:
        if isinstance(tags_field, list):
            # Tags is list: ["tag1", "tag2", ...] → capture as freeform tags (no "tag:" prefix)
            # These are typically genre/category tags from the source (BandCamp genres, etc.)
            for tag_value in tags_field:
                if tag_value:
                    normalized = value_normalize(str(tag_value))
                    if not normalized:
                        continue
                    channel_candidate = _extract_channel_from_tag(normalized)
                    if channel_candidate:
                        _add_tag(tags, "channel", channel_candidate)
                    if normalized not in tags:
                        tags.append(normalized)
        elif isinstance(tags_field, dict):
            # Tags is dict: {"key": "val"} → tag:key:val
            for key, val in tags_field.items():
                if key and val:
                    key_normalized = value_normalize(str(key))
                    val_normalized = value_normalize(str(val))
                    if key_normalized and val_normalized:
                        _add_tag(tags, f"tag:{key_normalized}", val_normalized)
        else:
            # Tags is string: "tag1,tag2" → split and capture as freeform
            tag_str = str(tags_field).strip()
            if tag_str:
                for tag_value in re.split(r'[,\s]+', tag_str):
                    tag_value = tag_value.strip()
                    if not tag_value:
                        continue
                    normalized = value_normalize(tag_value)
                    if not normalized:
                        continue
                    channel_candidate = _extract_channel_from_tag(normalized)
                    if channel_candidate:
                        _add_tag(tags, "channel", channel_candidate)
                    if normalized not in tags:
                        tags.append(normalized)

    # Extract chapters as tags if present
    chapters = entry.get("chapters")
    if chapters and isinstance(chapters, list):
        for chapter in chapters:
            if isinstance(chapter, dict):
                title = chapter.get("title")
                if title:
                    title_norm = value_normalize(str(title))
                    if title_norm and title_norm not in tags:
                        tags.append(title_norm)

    return tags