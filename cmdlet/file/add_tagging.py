"""Tag resolution, FlorenceVision integration, and metadata extraction for add-file."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple
from pathlib import Path
import sys
import re

from SYS import models
from SYS.logger import log, debug

from .. import _shared as sh

# Import from add_core (safe: add_core defines these before importing this module)
from .add_core import (
    Add_File,
    _SCREENSHOT_TIME_SUFFIX_RE,
    _REMOTE_URL_PREFIXES,
)

extract_tag_from_result = sh.extract_tag_from_result
extract_title_from_result = sh.extract_title_from_result
extract_url_from_result = sh.extract_url_from_result
merge_sequences = sh.merge_sequences
extract_relationships = sh.extract_relationships
extract_duration = sh.extract_duration
collapse_namespace_tags = sh.collapse_namespace_tags
resolve_media_kind_by_extension = sh.resolve_media_kind_by_extension
get_field = sh.get_field


def _maybe_apply_florencevision_tags(
    media_path: Path,
    tags: List[str],
    config: Dict[str, Any],
    pipe_obj: Optional[models.PipeObject] = None,
) -> List[str]:
    """Optionally auto-tag images using the FlorenceVision plugin helper.

    Controlled via config:
      [plugin=florencevision]
      enabled=true
      strict=false

    If strict=false (default), failures log a warning and return the original tags.
    If strict=true, failures raise to abort the ingest.
    """
    strict = False
    try:
        plugin_block = (config or {}).get("plugin")
        fv_block = plugin_block.get("florencevision") if isinstance(plugin_block, dict) else None
        enabled = False
        if isinstance(fv_block, dict):
            enabled = bool(fv_block.get("enabled"))
            strict = bool(fv_block.get("strict"))
        if not enabled:
            return tags

        from PluginCore.registry import plugin_attr

        FlorenceVisionTool = plugin_attr("florencevision", "FlorenceVisionTool")
        if FlorenceVisionTool is None:
            return tags

        cfg_for_tool: Dict[str, Any] = config
        try:
            action = str(getattr(pipe_obj, "action", "") or "") if pipe_obj is not None else ""
            cmdlet_name = ""
            if action.lower().startswith("cmdlet:"):
                cmdlet_name = action.split(":", 1)[1].strip().lower()
            if cmdlet_name in {"screen-shot", "screen_shot", "screenshot"}:
                plugin_block2 = dict((config or {}).get("plugin") or {})
                fv_block2 = dict(plugin_block2.get("florencevision") or {})
                fv_block2["task"] = "ocr"
                plugin_block2["florencevision"] = fv_block2
                cfg_for_tool = dict(config or {})
                cfg_for_tool["plugin"] = plugin_block2
        except Exception:
            cfg_for_tool = config

        fv = FlorenceVisionTool(cfg_for_tool)
        if not fv.enabled() or not fv.applicable_path(media_path):
            return tags

        auto_tags = fv.tags_for_file(media_path)

        try:
            caption_text = getattr(fv, "last_caption", None)
            if caption_text and pipe_obj is not None:
                if not isinstance(pipe_obj.extra, dict):
                    pipe_obj.extra = {}
                notes = pipe_obj.extra.get("notes")
                if not isinstance(notes, dict):
                    notes = {}
                notes.setdefault("caption", caption_text)
                pipe_obj.extra["notes"] = notes
        except Exception:
            pass

        if not auto_tags:
            return tags

        merged = merge_sequences(tags or [], auto_tags, case_sensitive=False)
        debug(f"[add-file] FlorenceVision added {len(auto_tags)} tag(s)")
        return merged
    except Exception as exc:
        strict2 = False
        try:
            plugin_block = (config or {}).get("plugin")
            tool_block = (config or {}).get("tool")
            fv_block = None
            if isinstance(plugin_block, dict):
                fv_block = plugin_block.get("florencevision")
            if fv_block is None and isinstance(tool_block, dict):
                fv_block = tool_block.get("florencevision")
            strict2 = bool(fv_block.get("strict")) if isinstance(fv_block, dict) else False
        except Exception:
            strict2 = False

        if strict or strict2:
            raise
        log(f"[add-file] Warning: FlorenceVision tagging failed: {exc}", file=sys.stderr)
        return tags


def _normalize_hash_candidate(value: Any) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64:
        return ""
    if any(ch not in "0123456789abcdef" for ch in text):
        return ""
    return text


def _parse_relationship_tag_king_alts(
    tag_value: str
) -> tuple[Optional[str], List[str]]:
    """Parse a relationship tag into (king_hash, alt_hashes).

    Supported formats:
    - New:  relationship: <KING_HASH>,<ALT_HASH>,<ALT_HASH>
    - Old:  relationship: hash(king)<KING_HASH>,hash(alt)<ALT_HASH>...
           relationship: hash(king)KING,hash(alt)ALT

    For the local DB we treat the first hash listed as the king.
    """
    if not isinstance(tag_value, str):
        return None, []

    raw = tag_value.strip()
    if not raw:
        return None, []

    rhs = raw
    if ":" in raw:
        prefix, rest = raw.split(":", 1)
        if prefix.strip().lower() == "relationship":
            rhs = rest.strip()

    typed = re.findall(r"hash\((\w+)\)<?([a-fA-F0-9]{64})>?", rhs)
    if typed:
        king: Optional[str] = None
        alts: List[str] = []
        for rel_type, h in typed:
            h_norm = str(h).strip().lower()
            if rel_type.strip().lower() == "king":
                king = h_norm
            elif rel_type.strip().lower() in {"alt", "related"}:
                alts.append(h_norm)
        if not king:
            all_hashes = [str(h).strip().lower() for _, h in typed]
            king = all_hashes[0] if all_hashes else None
            alts = [h for h in all_hashes[1:] if h]
        seen: set[str] = set()
        alts = [
            h for h in alts
            if h and len(h) == 64 and not (h in seen or seen.add(h))
        ]
        if king and len(king) == 64:
            return king, [h for h in alts if h != king]
        return None, []

    hashes = re.findall(r"\b[a-fA-F0-9]{64}\b", rhs)
    hashes = [h.strip().lower() for h in hashes if isinstance(h, str)]
    if not hashes:
        return None, []
    king = hashes[0]
    alts = hashes[1:]
    seen2: set[str] = set()
    alts = [
        h for h in alts if h and len(h) == 64 and not (h in seen2 or seen2.add(h))
    ]
    return king, [h for h in alts if h != king]


def _parse_relationships_king_alts(
    relationships: Dict[str, Any],
) -> tuple[Optional[str], List[str]]:
    """Parse a PipeObject.relationships dict into (king_hash, alt_hashes).

    Supported shapes:
    - {"king": [KING], "alt": [ALT1, ALT2]}
    - {"king": KING, "alt": ALT} (strings)
    - Also treats "related" hashes as alts for persistence purposes.
    """
    if not isinstance(relationships, dict) or not relationships:
        return None, []

    def _first_hash(val: Any) -> Optional[str]:
        if isinstance(val, str):
            h = val.strip().lower()
            return h if len(h) == 64 else None
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    h = item.strip().lower()
                    if len(h) == 64:
                        return h
        return None

    def _many_hashes(val: Any) -> List[str]:
        out: List[str] = []
        if isinstance(val, str):
            h = val.strip().lower()
            if len(h) == 64:
                out.append(h)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    h = item.strip().lower()
                    if len(h) == 64:
                        out.append(h)
        return out

    king = _first_hash(relationships.get("king"))
    if not king:
        return None, []

    alts = _many_hashes(relationships.get("alt"))
    alts.extend(_many_hashes(relationships.get("related")))

    seen: set[str] = set()
    alts = [h for h in alts if h and h != king and not (h in seen or seen.add(h))]
    return king, alts


def _get_url(result: Any, pipe_obj: models.PipeObject) -> List[str]:
    """Extract valid URLs from pipe object or result dict."""
    from SYS.metadata import normalize_urls

    candidates: List[str] = []

    if pipe_obj.url:
        candidates.append(pipe_obj.url)
    if pipe_obj.source_url:
        candidates.append(pipe_obj.source_url)

    if isinstance(pipe_obj.extra, dict):
        u = pipe_obj.extra.get("url")
        if isinstance(u, list):
            candidates.extend(str(x) for x in u if x)
        elif isinstance(u, str):
            candidates.append(u)

    raw_from_result = extract_url_from_result(result)
    if raw_from_result:
        candidates.extend(raw_from_result)

    normalized = normalize_urls(candidates)
    return [u for u in normalized if Add_File._is_probable_url(u)]


def _get_relationships(result: Any, pipe_obj: models.PipeObject) -> Optional[Dict[str, Any]]:
    try:
        rels = pipe_obj.get_relationships()
        if rels:
            return rels
    except Exception:
        pass
    if isinstance(result, dict) and result.get("relationships"):
        return result.get("relationships")
    try:
        return extract_relationships(result)
    except Exception:
        return None


def _get_duration(result: Any, pipe_obj: models.PipeObject) -> Optional[float]:

    def _parse_duration(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value) if value > 0 else None
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            try:
                candidate = float(s)
                return candidate if candidate > 0 else None
            except ValueError:
                pass
            if ":" in s:
                parts = [p.strip() for p in s.split(":") if p.strip()]
                if len(parts) in {2, 3} and all(p.isdigit() for p in parts):
                    nums = [int(p) for p in parts]
                    if len(nums) == 2:
                        minutes, seconds = nums
                        return float(minutes * 60 + seconds)
                    hours, minutes, seconds = nums
                    return float(hours * 3600 + minutes * 60 + seconds)
        return None

    parsed = _parse_duration(getattr(pipe_obj, "duration", None))
    if parsed is not None:
        return parsed
    try:
        return _parse_duration(extract_duration(result))
    except Exception:
        return None


def _get_note_text(result: Any, pipe_obj: models.PipeObject, note_name: str) -> Optional[str]:
    """Extract a named note text from a piped item.

    Supports:
    - pipe_obj.extra["notes"][note_name]
    - result["notes"][note_name] for dict results
    - pipe_obj.extra[note_name] / result[note_name] as fallback
    """

    def _normalize(val: Any) -> Optional[str]:
        if val is None:
            return None
        if isinstance(val, bytes):
            try:
                val = val.decode("utf-8", errors="ignore")
            except Exception:
                val = str(val)
        if isinstance(val, str):
            text = val.strip()
            return text if text else None
        try:
            text = str(val).strip()
            return text if text else None
        except Exception:
            return None

    note_key = str(note_name or "").strip()
    if not note_key:
        return None

    try:
        if isinstance(pipe_obj.extra, dict):
            notes_val = pipe_obj.extra.get("notes")
            if isinstance(notes_val, dict) and note_key in notes_val:
                return _normalize(notes_val.get(note_key))
            if note_key in pipe_obj.extra:
                return _normalize(pipe_obj.extra.get(note_key))
    except Exception:
        pass

    if isinstance(result, dict):
        try:
            notes_val = result.get("notes")
            if isinstance(notes_val, dict) and note_key in notes_val:
                return _normalize(notes_val.get(note_key))
            if note_key in result:
                return _normalize(result.get(note_key))
        except Exception:
            pass

    return None


def _load_sidecar_bundle(
    media_path: Path,
    instance: Optional[str],
    config: Dict[str, Any],
) -> Tuple[Optional[Path], Optional[str], List[str], List[str]]:
    """Load sidecar metadata (placeholder — overridden by active plugins)."""
    return None, None, [], []


def _resolve_file_hash(
    result: Any,
    media_path: Path,
    pipe_obj: models.PipeObject,
    fallback_hash: Optional[str],
) -> Optional[str]:
    from SYS.utils import sha256_file

    if pipe_obj.hash and pipe_obj.hash != "unknown":
        return pipe_obj.hash
    if fallback_hash:
        return fallback_hash

    if isinstance(result, dict):
        candidate = result.get("hash")
        if candidate:
            return str(candidate)

    try:
        return sha256_file(media_path)
    except Exception:
        return None


def _resolve_media_kind(path: Path) -> str:
    return resolve_media_kind_by_extension(path)


def _prepare_metadata(
    result: Any,
    media_path: Path,
    pipe_obj: models.PipeObject,
    config: Dict[str, Any],
) -> Tuple[List[str], List[str], Optional[str], Optional[str]]:
    """
    Prepare tags, url, and title for the file.
    Returns (tags, url, preferred_title, file_hash)
    """
    tags_from_result = list(pipe_obj.tag or [])
    if not tags_from_result:
        try:
            tags_from_result = list(extract_tag_from_result(result) or [])
        except Exception:
            tags_from_result = []

    url_from_result = Add_File._get_url(result, pipe_obj)

    def _has_namespace_tag(tags: Sequence[str], namespace: str) -> bool:
        namespace_text = str(namespace or "").strip().lower()
        if not namespace_text:
            return False
        prefix = f"{namespace_text}:"
        for tag in tags or []:
            text = str(tag or "").strip().lower()
            if text.startswith(prefix):
                return True
        return False

    def _extract_screenshot_time_title() -> tuple[Optional[str], Optional[str]]:
        current_title = str(preferred_title or "").strip()
        filename_title = str(media_path.stem or "").strip()
        if current_title and current_title != filename_title:
            return None, None
        if not url_from_result:
            return None, None
        suffix = str(media_path.suffix or "").strip().lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif", ".mhtml"}:
            return None, None
        match = _SCREENSHOT_TIME_SUFFIX_RE.match(str(media_path.stem or "").strip())
        if not match:
            return None, None
        title_text = str(match.group("title") or "").strip().replace("_", " ").strip()
        label_text = str(match.group("label") or "").strip().lower()
        if not title_text or not label_text:
            return None, None
        return title_text, label_text

    preferred_title = pipe_obj.title
    if not preferred_title:
        for t in tags_from_result:
            if str(t).strip().lower().startswith("title:"):
                candidate = t.split(":", 1)[1].strip().replace("_", " ").strip()
                if candidate:
                    preferred_title = candidate
                    break
    if not preferred_title:
        preferred_title = extract_title_from_result(result)
        if preferred_title:
            preferred_title = preferred_title.replace("_", " ").strip()

    derived_screenshot_title, derived_time_tag = _extract_screenshot_time_title()
    if derived_screenshot_title and (
        not preferred_title or str(preferred_title or "").strip() == str(media_path.stem or "").strip()
    ):
        preferred_title = derived_screenshot_title

    store = getattr(pipe_obj, "store", None)
    _, sidecar_hash, sidecar_tags, sidecar_url = Add_File._load_sidecar_bundle(
        media_path, store, config
    )

    def normalize_title_tag(tag: str) -> str:
        if str(tag).strip().lower().startswith("title:"):
            parts = tag.split(":", 1)
            if len(parts) == 2:
                value = parts[1].replace("_", " ").strip()
                return f"title:{value}"
        return tag

    tags_from_result_no_title = [
        t for t in tags_from_result
        if not str(t).strip().lower().startswith("title:")
    ]
    sidecar_tags = collapse_namespace_tags(
        [normalize_title_tag(t) for t in sidecar_tags],
        "title",
        prefer="last"
    )
    sidecar_tags_filtered = [
        t for t in sidecar_tags if not str(t).strip().lower().startswith("title:")
    ]

    merged_tags = merge_sequences(
        tags_from_result_no_title,
        sidecar_tags_filtered,
        case_sensitive=True
    )

    if derived_time_tag and not _has_namespace_tag(merged_tags, "time") and not _has_namespace_tag(merged_tags, "timestamp"):
        merged_tags.append(f"time:{derived_time_tag}")

    if preferred_title:
        merged_tags.append(f"title:{preferred_title}")

    merged_url = merge_sequences(url_from_result, sidecar_url, case_sensitive=False)
    merged_url = [u for u in merged_url if Add_File._is_probable_url(u)]

    file_hash = Add_File._resolve_file_hash(
        result,
        media_path,
        pipe_obj,
        sidecar_hash
    )

    relationship_tags = [
        t for t in merged_tags
        if isinstance(t, str) and t.strip().lower().startswith("relationship:")
    ]
    if relationship_tags:
        try:
            if (not isinstance(getattr(pipe_obj, "relationships", None), dict) or not pipe_obj.relationships):
                king: Optional[str] = None
                alts: List[str] = []
                for rel_tag in relationship_tags:
                    k, a = _parse_relationship_tag_king_alts(rel_tag)
                    if k and not king:
                        king = k
                    if a:
                        alts.extend(a)
                if king:
                    seen_alt: set[str] = set()
                    alts = [
                        h for h in alts if h and h != king and len(h) == 64
                        and not (h in seen_alt or seen_alt.add(h))
                    ]
                    payload: Dict[str, Any] = {"king": [king]}
                    if alts:
                        payload["alt"] = alts
                    pipe_obj.relationships = payload
        except Exception:
            pass

    merged_tags = [
        t for t in merged_tags if
        not (isinstance(t, str) and t.strip().lower().startswith("relationship:"))
    ]

    pipe_obj.tag = merged_tags
    if preferred_title and not pipe_obj.title:
        pipe_obj.title = preferred_title
    if file_hash and not pipe_obj.hash:
        pipe_obj.hash = file_hash
    if isinstance(pipe_obj.extra, dict):
        pipe_obj.extra["url"] = merged_url
    return merged_tags, merged_url, preferred_title, file_hash
