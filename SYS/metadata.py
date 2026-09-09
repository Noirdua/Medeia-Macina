import json
import re
import subprocess
import sys
import shutil
from SYS.logger import log, debug
import logging

logger = logging.getLogger(__name__)

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from PluginCore.registry import get_plugin
from SYS.yt_metadata import extract_ytdlp_tags

try:  # Optional; used when available for richer metadata fetches
    import yt_dlp
except Exception:  # pragma: no cover - optional dependency
    yt_dlp = None
try:  # Optional; used for IMDb lookup without API key
    from imdbinfo.services import search_title  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    search_title = None  # type: ignore[assignment]
try:
    import mutagen
except ImportError:
    mutagen = None
try:
    import musicbrainzngs
except ImportError:
    musicbrainzngs = None


def value_normalize(value: Any) -> str:
    text = str(value).strip()
    return text.lower() if text else ""


def _append_unique(target: List[str], seen: Set[str], value: Any) -> None:
    normalized = value_normalize(str(value))
    if not normalized or normalized in seen:
        return
    seen.add(normalized)
    target.append(normalized)


def _normalize_tag(tag: Any) -> Optional[str]:
    if tag is None:
        return None
    normalized = value_normalize(tag)
    return normalized or None


def _extend_namespaced(
    target: List[str],
    seen: Set[str],
    namespace: str,
    values: Iterable[Optional[str]]
) -> None:
    """Append namespaced values if not already in seen set."""
    for val in values:
        if val:
            _append_unique(target, seen, f"{namespace}:{val}")


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


def _coerce_duration(metadata: Dict[str, Any]) -> Optional[float]:
    for key in ("duration", "duration_seconds", "length", "duration_sec"):
        value = metadata.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            if value > 0:
                return float(value)
        elif isinstance(value, str):
            try:
                candidate = float(value.strip())
            except ValueError:
                continue
            if candidate > 0:
                return candidate
    return None


def _sanitize_url(value: Optional[str]) -> Optional[str]:
    """Sanitize URL: normalize and remove ytdl:// prefix."""
    if value is None:
        return None
    cleaned = value_normalize(str(value))
    if not cleaned:
        return None
    if cleaned.lower().startswith("ytdl://"):
        cleaned = cleaned[7:]
    return cleaned


def sanitize_metadata_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value if v)
    return str(value).strip().replace("\n", " ").replace("\r", " ")


def unique_preserve_order(items: Iterable[Any]) -> list[Any]:
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def fetch_musicbrainz_tags(mbid: str, entity: str = "release") -> Dict[str, Any]:
    if not musicbrainzngs:
        return {"tag": []}

    musicbrainzngs.set_useragent("Medeia-Macina", "0.1")
    tags: list[str] = []
    try:
        if entity == "release":
            res = musicbrainzngs.get_release_by_id(mbid, includes=["tags"])
            tags_list = res.get("release", {}).get("tag-list", [])
        elif entity == "recording":
            res = musicbrainzngs.get_recording_by_id(mbid, includes=["tags"])
            tags_list = res.get("recording", {}).get("tag-list", [])
        elif entity == "artist":
            res = musicbrainzngs.get_artist_by_id(mbid, includes=["tags"])
            tags_list = res.get("artist", {}).get("tag-list", [])
        else:
            return {"tag": []}

        for t in tags_list:
            if isinstance(t, dict) and "name" in t:
                tags.append(t["name"])
    except Exception as exc:
        debug(f"MusicBrainz lookup failed: {exc}")

    return {"tag": tags}


def _clean_existing_tags(existing: Any) -> List[str]:
    tags: List[str] = []
    seen: Set[str] = set()
    if isinstance(existing, (list, tuple, set)):
        iterable = existing
    elif existing is None:
        iterable = []
    else:
        iterable = [existing]
    for tag in iterable:
        _append_unique(tags, seen, tag)
    return tags


def _should_fetch_url(url: Optional[str]) -> bool:
    if not url or not isinstance(url, str):
        return False
    return url.lower().startswith(("http://", "https://"))


def fetch_remote_metadata(url: str,
                          options: Dict[str,
                                        Any]) -> Tuple[Optional[Dict[str,
                                                                     Any]],
                                                       List[str]]:
    warnings: List[str] = []
    info: Optional[Dict[str, Any]] = None
    if yt_dlp is not None:
        try:  # pragma: no cover - depends on runtime availability
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[attr-defined]
                info_dict = ydl.extract_info(url, download=False)
                if info_dict is not None:
                    info = dict(info_dict)
        except Exception as exc:  # pragma: no cover - best effort
            warnings.append(f"yt_dlp extract failed: {exc}")
    if info is None:
        executable = str(options.get("ytdlp_path") or "yt-dlp")
        extra_args = options.get("ytdlp_args") or []
        if isinstance(extra_args, (str, bytes)):
            extra_args = [extra_args]
        cmd = [
            executable,
            "--dump-single-json",
            "--no-playlist",
            "--skip-download",
            "--no-warnings",
        ]
        cmd.extend(str(arg) for arg in extra_args)
        cmd.append(url)
        timeout = float(options.get("timeout") or 45.0)
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout
            )
        except Exception as exc:  # pragma: no cover - subprocess failure
            warnings.append(f"yt-dlp invocation failed: {exc}")
            return None, warnings
        if completed.returncode != 0:
            message = (
                completed.stderr.strip() or completed.stdout.strip()
                or f"status {completed.returncode}"
            )
            warnings.append(message)
            return None, warnings
        try:
            info = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:  # pragma: no cover - parse failure
            warnings.append(f"invalid JSON from yt-dlp: {exc}")
            return None, warnings
    if isinstance(info, dict) and "entries" in info:
        entries = info.get("entries")
        if isinstance(entries, list) and entries:
            info = entries[0]
    if isinstance(info, dict):
        info.setdefault("source_url", url)
    return info if isinstance(info, dict) else None, warnings


def resolve_remote_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
    options_raw = payload.get("options")
    options: Dict[str,
                  Any] = options_raw if isinstance(options_raw,
                                                   dict) else {}
    source_url = payload.get("source_url")
    sanitized = _sanitize_url(source_url) or source_url
    existing_tags = _clean_existing_tags(payload.get("existing_tags"))
    metadata_sources: List[Dict[str, Any]] = []
    for key in ("metadata", "mpv_metadata", "remote_metadata", "info"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            metadata_sources.append(candidate)
    remote_info: Optional[Dict[str, Any]] = None
    warnings: List[str] = []
    if not options.get("no_fetch"):
        fetch_url = sanitized
        if _should_fetch_url(fetch_url):
            remote_info, fetch_warnings = fetch_remote_metadata(fetch_url or "", options)
            warnings.extend(fetch_warnings)
            if remote_info:
                metadata_sources.append(remote_info)
    combined_metadata = {}
    for source in metadata_sources:
        if isinstance(source, dict):
            combined_metadata.update(source)
    context = {
        "source_url": sanitized
    }
    bundle = build_remote_bundle(combined_metadata, existing_tags, context)
    merged_metadata = {
        **combined_metadata,
        **(bundle.get("metadata") or {})
    }
    bundle["metadata"] = merged_metadata
    if not bundle.get("source_url"):
        bundle["source_url"] = sanitized
    mpv_meta_candidate = payload.get("mpv_metadata")
    mpv_metadata = mpv_meta_candidate if isinstance(mpv_meta_candidate, dict) else None
    result_tags = bundle.get("tags") or existing_tags
    result = {
        "source": "remote-metadata",
        "id": sanitized or "unknown",
        "tags": result_tags,
        "title": bundle.get("title"),
        "source_url": bundle.get("source_url") or sanitized,
        "duration": bundle.get("duration"),
        "metadata": merged_metadata,
        "remote_metadata": remote_info,
        "warnings": warnings,
        "mpv_metadata": mpv_metadata,
    }
    return result


def imdb_tag(imdb_id: str, timeout: float = 10.0) -> Dict[str, Any]:
    """Fetch IMDb data using imdbinfo (no API key required).

    Returns at minimum an imdb:<id> tag. When imdbinfo is installed, enriches
    with title/year/type/rating from the first search result for the id.
    """
    normalized = value_normalize(imdb_id)
    if not normalized:
        raise ValueError("imdb_id is required")
    if not normalized.startswith("tt"):
        normalized = f"tt{normalized}"

    tags: List[str] = []
    seen: Set[str] = set()
    _append_unique(tags, seen, f"imdb:{normalized}")

    result: Dict[str, Any] = {
        "id": normalized,
        "tag": tags,
    }

    if search_title is None:
        result["warnings"] = ["imdbinfo is not installed; returning minimal IMDb tag"]
        return result

    try:
        search_result = search_title(normalized, timeout=timeout)
    except Exception as exc:  # pragma: no cover - network dependent
        result["warnings"] = [f"IMDb lookup failed: {exc}"]
        return result

    titles = getattr(search_result, "titles", None) or []
    if not titles:
        result["warnings"] = ["IMDb lookup returned no data"]
        return result

    entry = titles[0]
    title = getattr(entry, "title", None) or getattr(entry, "title_localized", None)
    year = getattr(entry, "year", None)
    kind = getattr(entry, "kind", None)
    rating = getattr(entry, "rating", None)

    if title:
        _append_unique(tags, seen, f"title:{title}")
    if year:
        _append_unique(tags, seen, f"year:{year}")
    if kind:
        _append_unique(tags, seen, f"type:{kind}")
    if rating:
        _append_unique(tags, seen, f"rating:{rating}")

    result["metadata"] = {
        "title": title,
        "year": year,
        "type": kind,
        "rating": rating,
    }
    result["tag"] = tags
    return result

def normalize_urls(value: Any) -> List[str]:
    """Normalize a URL field into a stable, deduplicated list.

    Accepts:
    - None
    - a single URL string (optionally containing multiple URLs)
    - a list/tuple/set of URL strings

    This helper is used by cmdlets/stores/pipeline to keep `url` consistent.
    """

    def _iter_raw_urls(raw: Any) -> Iterable[str]:
        if raw is None:
            return

        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return

            # Prefer extracting obvious URLs to avoid splitting inside query strings.
            matches = re.findall(r"https?://[^\s,]+", text, flags=re.IGNORECASE)
            if matches:
                for m in matches:
                    yield m
                return

            # Fallback: split on commas/whitespace.
            for token in text.replace("\n",
                                      " ").replace("\r",
                                                   " ").replace(",",
                                                                " ").split():
                if token:
                    t_low = token.lower()
                    # Heuristic: only yield tokens that look like URLs or common address patterns.
                    # This prevents plain tags (e.g. "tag1, tag2") from leaking into URL fields.
                    is_p_url = t_low.startswith(("http://",
                                                 "https://",
                                                 "magnet:",
                                                 "torrent:",
                                                 "ytdl://",
                                                 "tidal:",
                                                 "data:",
                                                 "ftp:",
                                                 "sftp:",
                                                 "alldebrid:",
                                                 "alldebrid🧲"))
                    is_struct_url = ("." in token and "/" in token
                                     and not token.startswith((".",
                                                               "/")))
                    if is_p_url or is_struct_url:
                        yield token
            return

        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                if item is None:
                    continue
                if isinstance(item, str):
                    if item.strip():
                        yield item
                else:
                    text = str(item).strip()
                    if text:
                        yield text
            return

        # Last resort: string-coerce.
        text = str(raw).strip()
        if text:
            yield text

    def _canonicalize(url_text: str) -> Optional[str]:
        u = str(url_text or "").strip()
        if not u:
            return None

        # Trim common wrappers and trailing punctuation.
        u = u.strip("<>\"' ")
        u = u.rstrip(')].,;"')
        if not u:
            return None

        # --- HEURISTIC FILTER ---
        # Ensure it actually looks like a URL/identifier to avoid tag leakage.
        # This prevents plain tags ("adam22", "10 books") from entering the URL list.
        low = u.lower()
        has_scheme = low.startswith((
            "http://", "https://", "magnet:", "torrent:", "tidal:",
            "hydrus:", "ytdl:", "soulseek:", "matrix:", "file:",
            "alldebrid:", "alldebrid🧲"
        ))
        if not (has_scheme or "://" in low):
            return None

        # IMPORTANT: URLs can be case-sensitive in the path/query on some hosts
        # (e.g., https://0x0.st/PzGY.webp). Do not lowercase or otherwise rewrite
        # the URL here; preserve exact casing and percent-encoding.
        return u

    seen: Set[str] = set()
    out: List[str] = []
    for raw_url in _iter_raw_urls(value):
        canonical = _canonicalize(raw_url)
        if not canonical:
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)

    return out

def _normalize_string_list(values: Optional[Iterable[Any]]) -> List[str]:
    if not values:
        return []
    seen: Set[str] = set()
    items: List[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip().lower()
        if not text:
            continue
        if text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def _derive_sidecar_path(media_path: Path) -> Path:
    """Return sidecar path (.tag)."""
    try:
        preferred = media_path.parent / (media_path.name + ".tag")
    except ValueError:
        preferred = media_path.with_name(media_path.name + ".tag")
    return preferred


def is_sidecar_filename(name: str) -> bool:
    lower = str(name or "").strip().lower()
    return lower.endswith(".tag") or lower.endswith(".metadata")


def parse_sidecar_text(raw: str) -> tuple[Optional[str], List[str], List[str]]:
    hash_value: Optional[str] = None
    tags: List[str] = []
    urls: List[str] = []
    in_tags = False
    saw_tags_section = False
    for raw_line in str(raw or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        lower = line.lower()
        if lower in {"[tags]", "[tag]"}:
            in_tags = True
            saw_tags_section = True
            continue
        if lower.startswith("[") and lower.endswith("]"):
            in_tags = False
            continue
        if in_tags:
            tags.append(line.lower())
            continue
        if lower.startswith("hash:"):
            hash_value = line.split(":", 1)[1].strip() if ":" in line else ""
        elif lower.startswith("url:"):
            url_part = line.split(":", 1)[1].strip() if ":" in line else ""
            if url_part:
                for url_segment in url_part.split(","):
                    for url_token in url_segment.split():
                        url_clean = url_token.strip()
                        if url_clean and url_clean not in urls:
                            urls.append(url_clean)
        elif saw_tags_section:
            continue
        else:
            tags.append(line.lower())
    return hash_value, tags, urls


def _read_sidecar_metadata(
    sidecar_path: Path,
) -> tuple[Optional[str],
           List[str],
           List[str]]:  # pyright: ignore[reportUnusedFunction]
    """Read hash, tags, and url from sidecar file.

    Consolidated with read_tags_from_file - this extracts extra metadata (hash, url).
    """
    if not sidecar_path.exists():
        return None, [], []
    try:
        raw = sidecar_path.read_text(encoding="utf-8")
    except OSError:
        return None, [], []
    return parse_sidecar_text(raw)


def rename(file_path: Path, tags: Iterable[str]) -> Optional[Path]:
    """Rename a file based on a title: tag.

    If a title: tag is present, renames the file and any .tag/.metadata sidecars.
    """

    new_title: Optional[str] = None
    for tag in tags:
        if isinstance(tag, str) and tag.lower().startswith("title:"):
            new_title = tag.split(":", 1)[1].strip()
            break

    if not new_title or not file_path.exists():
        return None

    old_name = file_path.name
    old_suffix = file_path.suffix
    new_name = f"{new_title}{old_suffix}"
    new_path = file_path.with_name(new_name)

    if new_path == file_path:
        return None

    def _rename_sidecar(ext: str) -> None:
        old_sidecar = file_path.parent / (old_name + ext)
        if not old_sidecar.exists():
            return
        new_sidecar = file_path.parent / (new_name + ext)
        if new_sidecar.exists():
            try:
                new_sidecar.unlink()
            except Exception as exc:
                debug(
                    f"Warning: Could not replace target sidecar {new_sidecar.name}: {exc}",
                    file=sys.stderr,
                )
                return
        old_sidecar.rename(new_sidecar)
        debug(
            f"Renamed sidecar: {old_sidecar.name} -> {new_sidecar.name}",
            file=sys.stderr
        )

    try:
        if new_path.exists():
            try:
                new_path.unlink()
                debug(f"Replaced existing file: {new_name}", file=sys.stderr)
            except Exception as exc:
                debug(
                    f"Warning: Could not replace target file {new_name}: {exc}",
                    file=sys.stderr
                )
                return None

        file_path.rename(new_path)
        debug(f"Renamed file: {old_name} -> {new_name}", file=sys.stderr)

        _rename_sidecar(".tag")
        _rename_sidecar(".metadata")

        return new_path
    except Exception as exc:
        debug(f"Warning: Failed to rename file: {exc}", file=sys.stderr)
        return None


def write_tags(
    media_path: Path,
    tags: Iterable[str],
    url: Iterable[str],
    hash_value: Optional[str] = None,
    db=None,
    *,
    emit_debug: bool = True,
) -> None:
    """Write tags to database or sidecar file (tags only).

    Hash/URL data is no longer written to the tag sidecar; it belongs in metadata.
    If db is provided, inserts tags only into LocalLibraryDB. Otherwise, writes .tag sidecar.
    """
    if media_path.exists() and media_path.is_dir():
        raise ValueError(f"write_tags_sidecar: media_path is a directory: {media_path}")

    # Prepare tags lines and convert to list if needed (tags only)
    tag_list = list(tags) if not isinstance(tags, list) else tags
    tag_list = [str(tag).strip().lower() for tag in tag_list if str(tag).strip()]

    # If database provided, insert directly and skip sidecar
    if db is not None:
        try:
            db_tags = [str(tag).strip().lower() for tag in tag_list if str(tag).strip()]

            if db_tags:
                db.add_tags(media_path, db_tags)
                debug(f"Added tags to database for {media_path.name}")
            return
        except Exception as e:
            debug(f"Failed to add tags to database: {e}", file=sys.stderr)
            return


def write_metadata(
    media_path: Path,
    hash_value: Optional[str] = None,
    url: Optional[Iterable[str]] = None,
    relationships: Optional[Iterable[str]] = None,
    db=None,
    *,
    emit_debug: bool = True,
) -> None:
    """Write metadata to database or sidecar file.

    If db is provided, inserts into LocalLibraryDB and skips sidecar file creation.
    Otherwise, creates .metadata sidecar file with hash, url, and relationships.

    Args:
        media_path: Path to the media file
        hash_value: Optional hash value for the file
        url: Optional iterable of known URL strings
        relationships: Optional iterable of relationship strings
        db: Optional LocalLibraryDB instance. If provided, skips sidecar creation.
    """
    if media_path.exists() and media_path.is_dir():
        raise ValueError(
            f"write_metadata_sidecar: media_path is a directory: {media_path}"
        )

    # Prepare metadata lines
    url_list = list(url) if url else []
    rel_list = list(relationships) if relationships else []

    # If database provided, insert directly and skip sidecar
    if db is not None:
        try:
            # Build metadata tag list
            db_tags = []
            if hash_value:
                db_tags.append(f"hash:{hash_value}")
            for url in url_list:
                if str(url).strip():
                    clean = str(url).strip()
                    db_tags.append(f"url:{clean}")
            for rel in rel_list:
                if str(rel).strip():
                    db_tags.append(f"relationship:{str(rel).strip()}")

            if db_tags:
                db.add_tags(media_path, db_tags)
                debug(f"Added metadata to database for {media_path.name}")
            return
        except Exception as e:
            debug(f"Failed to add metadata to database: {e}", file=sys.stderr)
            return


def extract_title(tags: Iterable[str]) -> Optional[str]:
    """
    Extracts a title from a list of tags (looks for 'title:...').
    """
    for tag in tags:

        tag = tag.strip()

        if tag.lower().startswith("title:"):
            title_tag = tag.split(":", 1)[1].strip()
            if title_tag:
                return title_tag
    return None


def _sanitize_title_for_filename(title: str) -> str:
    # Allow alnum, hyphen, underscore, and space; replace other chars with space
    temp = []
    for ch in title:
        if ch.isalnum() or ch in {"-",
                                  "_",
                                  " "}:
            temp.append(ch)
        else:
            temp.append(" ")
    # Collapse whitespace and trim hyphens/underscores around words
    rough = "".join(temp)
    tokens = []
    for seg in rough.split():
        cleaned = seg.strip("-_ ")
        if cleaned:
            tokens.append(cleaned)
    sanitized = "_".join(tokens)
    sanitized = sanitized.strip("-_")
    return sanitized or "untitled"


def apply_title_to_path(media_path: Path, tags: Iterable[str]) -> Path:
    """
    If a title tag is present, returns a new Path with the title as filename; else returns original path.
    """
    title = extract_title(tags)
    if not title:
        return media_path
    parent = media_path.parent
    sanitized = _sanitize_title_for_filename(title)
    destination = parent / f"{sanitized}{media_path.suffix}"
    return destination


def sync_sidecar(payload: Dict[str, Any]) -> Dict[str, Any]:
    path_value = payload.get("path")
    if not path_value:
        raise ValueError("path is required to synchronise sidecar")

    candidate = Path(str(path_value)).expanduser()
    if candidate.suffix.lower() == ".tag":
        sidecar_path = candidate
    else:
        sidecar_path = _derive_sidecar_path(candidate)

    tags = _normalize_string_list(payload.get("tag"))
    if not tags and sidecar_path.exists():
        tags = read_tags_from_file(sidecar_path)

    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    if tags:
        sidecar_path.write_text("\n".join(tags) + "\n", encoding="utf-8")
        return {
            "path": str(sidecar_path),
            "tag": tags,
        }

    try:
        sidecar_path.unlink()
    except FileNotFoundError:
        pass
    return {
        "path": str(sidecar_path),
        "tag": [],
        "deleted": True,
    }


def apply_tag_mutation(payload: Dict[str,
                                     Any],
                       operation: str = "add") -> Dict[str,
                                                       Any]:
    """Unified tag mutation for add and update operations (Hydrus and local).

    Consolidates: add_tag, update_tag, _add_local_tag, _update_local_tag

    Args:
        payload: Mutation payload with type, tags, old_tag, new_tag
        operation: 'add' or 'update'

    Returns:
        Dict with tags and operation result
    """
    file_type = str(payload.get("type", "local")).lower()

    if file_type == "hydrus":
        raise ValueError(
            "Hydrus tag mutation is handled by the store backend "
            "(plugins.hydrusnetwork), not SYS.metadata.apply_tag_mutation"
        )
    else:  # local
        tag = _clean_existing_tags(payload.get("tag"))

        if operation == "add":
            new_tag = _normalize_tag(payload.get("new_tag"))
            if not new_tag:
                raise ValueError("new_tag is required")
            added = new_tag not in tag
            if added:
                tag.append(new_tag)
            return {
                "tag": tag,
                "added": added
            }

        else:  # update
            old_tag = _normalize_tag(payload.get("old_tag"))
            new_tag = _normalize_tag(payload.get("new_tag"))
            if not old_tag:
                raise ValueError("old_tag is required")

            remaining = []
            removed_count = 0
            for item in tag:
                if item == old_tag:
                    removed_count += 1
                else:
                    remaining.append(item)

            if new_tag and removed_count > 0:
                remaining.extend([new_tag] * removed_count)

            updated = removed_count > 0 or (bool(new_tag) and new_tag not in tag)
            return {
                "tag": remaining,
                "updated": updated,
                "removed_count": removed_count
            }





def dedup_tags_by_namespace(tags: List[str], keep_first: bool = True) -> List[str]:
    """Deduplicate tags by namespace, keeping consistent order.

    This is the UNIFIED API for tag deduplication used across all cmdlet.
    Replaces custom deduplication logic in merge_file.py and other modules.

    Groups tags by namespace (e.g., "artist", "album", "tag") and keeps
    either the first or last occurrence of each namespace, then preserves
    order based on first appearance.

    Args:
        tags: List of tags (with or without namespace prefixes)
        keep_first: If True, keep first occurrence per namespace (default).
                   If False, keep last occurrence per namespace.

    Returns:
        Deduplicated tag list with consistent order

    Example:
        >>> tags = [
        ...     'artist:Beatles', 'album:Abbey Road',
        ...     'artist:Beatles', 'tag:rock',
        ...     'album:Abbey Road', 'artist:Beatles'
        ... ]
        >>> dedup = dedup_tags_by_namespace(tags)
        >>> debug(dedup)
        ['artist:Beatles', 'album:Abbey Road', 'tag:rock']
    """
    if not tags:
        return []

    # Group tags by namespace
    namespace_to_tags: Dict[Optional[str],
                            List[Tuple[int,
                                       str]]] = (
                                           {}
                                       )  # namespace → [(index, full_tag), ...]
    first_appearance: Dict[Optional[str],
                           int] = {}  # namespace → first_index

    for idx, tag in enumerate(tags):
        # Extract namespace (part before ':')
        if ":" in tag:
            namespace: Optional[str] = tag.split(":", 1)[0]
        else:
            namespace = None  # No namespace

        # Track first appearance
        if namespace not in first_appearance:
            first_appearance[namespace] = idx

        # Store tag with its index
        if namespace not in namespace_to_tags:
            namespace_to_tags[namespace] = []
        namespace_to_tags[namespace].append((idx, tag))

    # Build result: keep first or last occurrence per namespace
    result: List[Tuple[int, str]] = []  # (first_appearance_index, tag)

    for namespace, tag_list in namespace_to_tags.items():
        if keep_first:
            chosen_tag = tag_list[0][1]  # First occurrence
        else:
            chosen_tag = tag_list[-1][1]  # Last occurrence

        result.append((first_appearance[namespace], chosen_tag))

    # Sort by first appearance order, then extract tags
    result.sort(key=lambda x: x[0])
    return [tag for _, tag in result]


def merge_multiple_tag_lists(sources: List[List[str]],
                             strategy: str = "first") -> List[str]:
    """Intelligently merge multiple tag lists with smart deduplication.

    This is the UNIFIED API for merging tags from multiple sources
    (e.g., when merging multiple files or combining metadata sources).

    Strategies:
    - 'first': Keep first occurrence of each namespace (default)
    - 'all': Keep all different values (different artists possible)
    - 'combine': For non-namespace tags, combine all unique values

    Args:
        sources: List of tag lists to merge
        strategy: Merge strategy - 'first', 'all', or 'combine'

    Returns:
        Merged and deduplicated tag list

    Example:
        >>> list1 = ['artist:Beatles', 'album:Abbey Road']
        >>> list2 = ['artist:Beatles', 'album:Abbey Road', 'tag:rock']
        >>> merged = merge_multiple_tag_lists([list1, list2])
        >>> debug(merged)
        ['artist:Beatles', 'album:Abbey Road', 'tag:rock']
    """
    if not sources:
        return []

    if strategy == "first":
        # Concatenate all lists and deduplicate by namespace
        all_tags = []
        for tag_list in sources:
            all_tags.extend(tag_list or [])
        return dedup_tags_by_namespace(all_tags, keep_first=True)

    elif strategy == "all":
        # Keep all different values per namespace
        namespace_to_values: Dict[Optional[str],
                                  Set[str]] = {}
        order: List[Tuple[int, str, str]] = []  # (first_index, namespace, value)
        global_index = 0

        for source in sources:
            if not source:
                continue
            for tag in source:
                if ":" in tag:
                    namespace: Optional[str] = tag.split(":", 1)[0]
                    value = tag.split(":", 1)[1]
                else:
                    namespace = None
                    value = tag

                if namespace not in namespace_to_values:
                    namespace_to_values[namespace] = set()
                    order.append((global_index, namespace or "", tag))
                elif value not in namespace_to_values[namespace]:
                    order.append((global_index, namespace or "", tag))

                namespace_to_values[namespace].add(value)
                global_index += 1

        # Sort by order of first appearance and extract
        order.sort(key=lambda x: x[0])
        return [tag for _, _, tag in order]

    elif strategy == "combine":
        # Combine all unique plain (non-namespace) tags
        all_tags = []
        namespaced: Dict[str,
                         str] = {}  # namespace → tag (first occurrence)

        for source in sources:
            if not source:
                continue
            for tag in source:
                if ":" in tag:
                    namespace = tag.split(":", 1)[0]
                    if namespace not in namespaced:
                        namespaced[namespace] = tag
                        all_tags.append(tag)
                else:
                    if tag not in all_tags:
                        all_tags.append(tag)

        return all_tags

    else:
        raise ValueError(f"Unknown merge strategy: {strategy}")


def read_tags_from_file(file_path: Path) -> List[str]:
    """Read and normalize tags from .tag sidecar file.

    This is the UNIFIED API for reading .tag files across all cmdlet.
    Handles normalization, deduplication, and format validation.

    Args:
        file_path: Path to .tag sidecar file

    Returns:
        List of normalized tag strings

    Raises:
        FileNotFoundError: If file doesn't exist

    Example:
        >>> tags = read_tags_from_file(Path('file.txt.tag'))
        >>> debug(tags)
        ['artist:Beatles', 'album:Abbey Road']
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Tag file not found: {file_path}")

    tags: List[str] = []
    seen: Set[str] = set()

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                # Strip whitespace and skip empty lines
                line = line.strip()
                if not line:
                    continue

                # Skip comment lines
                if line.startswith("#"):
                    continue

                # Normalize the tag
                normalized = value_normalize(line).lower()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    tags.append(normalized)
    except Exception as exc:
        raise ValueError(f"Error reading tag file {file_path}: {exc}")

    return tags


def embed_metadata_in_file(
    file_path: Path,
    tags: List[str],
    file_kind: str = ""
) -> bool:
    """ """
    if not tags:
        return True

    file_path = Path(file_path)

    # Tag namespace to FFmpeg metadata key mapping
    tag_map = {
        "title": "title",
        "artist": "artist",
        "album": "album",
        "track": "track",
        "track_number": "track",
        "date": "date",
        "year": "date",
        "genre": "genre",
        "composer": "composer",
        "comment": "comment",
        "url": "comment",  # Embed known url in comment field
        "creator": "artist",  # Map creator to artist
        "channel": "album_artist",  # Map channel to album_artist
    }

    # Extract metadata from tags
    metadata = {}
    comments = []  # Collect comments (including url)
    for tag in tags:
        tag_str = str(tag).strip()
        if ":" in tag_str:
            namespace, value = tag_str.split(":", 1)
            namespace = namespace.lower().strip()
            value = value.strip()
            if namespace in tag_map and value:
                ffmpeg_key = tag_map[namespace]
                if namespace == "url":
                    # Collect url as comments
                    comments.append(f"URL: {value}")
                elif ffmpeg_key == "comment":
                    # Collect other comment-type tags
                    comments.append(value)
                elif ffmpeg_key not in metadata:
                    # Don't overwrite if already set from earlier tag
                    metadata[ffmpeg_key] = value

    # Add collected comments to metadata
    if comments:
        if "comment" in metadata:
            metadata["comment"] = metadata["comment"] + " | " + " | ".join(comments)
        else:
            metadata["comment"] = " | ".join(comments)

    # Apply sensible defaults for audio files
    if file_kind == "audio" or (not file_kind and file_path.suffix.lower() in {".mp3",
                                                                               ".flac",
                                                                               ".wav",
                                                                               ".m4a",
                                                                               ".aac",
                                                                               ".ogg",
                                                                               ".opus",
                                                                               ".mka"}):
        # If no album, use title as album
        if "album" not in metadata and "title" in metadata:
            metadata["album"] = metadata["title"]
        # If no track, default to 1
        if "track" not in metadata:
            metadata["track"] = "1"
        # If no album_artist, use artist
        if "artist" in metadata:
            metadata["album_artist"] = metadata["artist"]

    if not metadata:
        return True

    # Check if FFmpeg is available
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        debug(
            f"⚠️  FFmpeg not found; cannot embed metadata in {file_path.name}",
            file=sys.stderr
        )
        return False

    # Create temporary file for output
    temp_file = file_path.parent / f"{file_path.stem}.ffmpeg_tmp{file_path.suffix}"
    try:
        cmd = [ffmpeg_path, "-y", "-i", str(file_path)]
        for key, value in metadata.items():
            cmd.extend(["-metadata", f"{key}={value}"])
        cmd.extend(["-c", "copy", str(temp_file)])

        # Run ffmpeg with error handling for non-UTF8 output
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,  # Don't decode as text - ffmpeg may output binary data
            timeout=30,
        )
        if result.returncode == 0 and temp_file.exists():
            # Replace original with temp file
            file_path.unlink()
            temp_file.rename(file_path)
            debug(f"Embedded metadata in file: {file_path.name}", file=sys.stderr)
            return True
        else:
            # Clean up temp file if it exists
            if temp_file.exists():
                temp_file.unlink()
            debug(
                f"❌ FFmpeg metadata embedding failed for {file_path.name}",
                file=sys.stderr
            )
            if result.stderr:
                # Safely decode stderr, ignoring invalid UTF-8 bytes
                try:
                    stderr_text = result.stderr.decode("utf-8", errors="replace")[:200]
                    debug(f"FFmpeg stderr: {stderr_text}", file=sys.stderr)
                except Exception:
                    logger.exception("Failed to decode FFmpeg stderr for %s", file_path)
            return False
    except Exception as exc:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except Exception:
                logger.exception("Failed to remove FFmpeg temp file %s after error", temp_file)
        debug(f"❌ Error embedding metadata: {exc}", file=sys.stderr)
        logger.exception("Error embedding metadata into %s", file_path)
        return False


def build_sidecar_payloads(
    *,
    tags: Optional[Iterable[str]] = None,
    urls: Optional[Iterable[str]] = None,
    hash_value: Optional[str] = None,
    relationships: Optional[Iterable[str]] = None,
) -> Dict[str, str]:
    tag_lines = [str(tag).strip().lower() for tag in (tags or []) if str(tag).strip()]
    url_list = [str(item).strip() for item in (urls or []) if str(item).strip()]
    rel_list = [str(item).strip() for item in (relationships or []) if str(item).strip()]
    lines: List[str] = []
    if hash_value:
        lines.append(f"hash:{hash_value}")
    for url in url_list:
        lines.append(f"url:{url}")
    for rel in rel_list:
        lines.append(f"relationship:{rel}")
    if tag_lines:
        if lines:
            lines.append("")
        lines.append("[tags]")
        lines.extend(tag_lines)
    if not lines:
        return {}
    return {".metadata": "\n".join(lines) + "\n"}


def write_merged_sidecar(
    media_path: Path,
    *,
    tags: Optional[Iterable[str]] = None,
    urls: Optional[Iterable[str]] = None,
    hash_value: Optional[str] = None,
    relationships: Optional[Iterable[str]] = None,
) -> Optional[Path]:
    payloads = build_sidecar_payloads(
        tags=tags,
        urls=urls,
        hash_value=hash_value,
        relationships=relationships,
    )
    body = payloads.get(".metadata")
    if not body:
        return None
    sidecar = Path(media_path).parent / (Path(media_path).name + ".metadata")
    sidecar.write_text(body, encoding="utf-8")
    return sidecar


def write_tags_to_file(
    file_path: Path,
    tags: List[str],
    source_hashes: Optional[List[str]] = None,
    url: Optional[List[str]] = None,
    append: bool = False,
) -> bool:
    """Write tags to .tag sidecar file.

    This is the UNIFIED API for writing .tag files across all cmdlet.
    Uses consistent format and handles file creation/overwriting.

    Args:
        file_path: Path to .tag file (will be created if doesn't exist)
        tags: List of tags to write
        source_hashes: Optional source file hashes (written as source:hash1,hash2)
        url: Optional known url (each written on separate line as url:url)
        append: If True, append to existing file; if False, overwrite (default)

    Returns:
        True if successful

    Raises:
        Exception: If file write fails

    Example:
        >>> tags = ['artist:Beatles', 'album:Abbey Road']
        >>> write_tags_to_file(Path('file.txt.tag'), tags)
        True
    """
    file_path = Path(file_path)

    try:
        # Prepare content
        content_lines: List[str] = []

        # Add source hashes if provided
        if source_hashes:
            content_lines.append(f"source:{','.join(source_hashes)}")

        # Add known url if provided - each on separate line to prevent corruption
        if url:
            for url_item in url:
                content_lines.append(f"url:{url_item}")

        # Add tags
        if tags:
            content_lines.extend(
                [str(t).strip().lower() for t in tags if str(t).strip()]
            )

        # Write to file
        mode = "a" if (append and file_path.exists()) else "w"
        with open(file_path, mode, encoding="utf-8") as f:
            for line in content_lines:
                f.write(line + "\n")

        return True
    except Exception as exc:
        raise ValueError(f"Error writing tag file {file_path}: {exc}")


def normalize_tags_from_source(source_data: Any,
                               source_type: str = "auto") -> List[str]:
    """Normalize tags from any source format.

    Universal function to normalize tags from different sources:
    - yt-dlp entry dicts
    - Raw tag lists
    - .tag file content strings
    - Metadata dictionaries

    Args:
        source_data: Source data (type determined by source_type or auto-detected)
        source_type: One of 'auto', 'ytdlp', 'list', 'text', 'dict'
                     'auto' attempts to auto-detect the type

    Returns:
        Normalized, deduplicated tag list

    Example:
        >>> entry = {'artist': 'Beatles', 'album': 'Abbey Road'}
        >>> tags = normalize_tags_from_source(entry, 'ytdlp')
        >>> debug(tags)
        ['artist:Beatles', 'album:Abbey Road']
    """
    if source_type == "auto":
        # Auto-detect source type
        if isinstance(source_data, dict):
            # Check if it looks like a yt-dlp entry (has id, title, url, etc.)
            if "id" in source_data or "title" in source_data or "uploader" in source_data:
                source_type = "ytdlp"
            else:
                source_type = "dict"
        elif isinstance(source_data, list):
            source_type = "list"
        elif isinstance(source_data, str):
            source_type = "text"
        else:
            source_type = "dict"

    # Process based on detected/specified type
    if source_type == "ytdlp":
        if not isinstance(source_data, dict):
            raise ValueError("ytdlp source must be a dict")
        return extract_ytdlp_tags(source_data)

    elif source_type == "list":
        if not isinstance(source_data, (list, tuple)):
            raise ValueError("list source must be a list or tuple")
        # Normalize each tag in the list
        result = []
        for tag in source_data:
            normalized = value_normalize(str(tag))
            if normalized:
                result.append(normalized)
        return result

    elif source_type == "text":
        if not isinstance(source_data, str):
            raise ValueError("text source must be a string")
        # Split by lines and normalize
        lines = source_data.split("\n")
        result = []
        seen = set()
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                normalized = value_normalize(line)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    result.append(normalized)
        return result

    elif source_type == "dict":
        if not isinstance(source_data, dict):
            raise ValueError("dict source must be a dict")
        # Extract as generic metadata (similar to yt-dlp but from any dict)
        return extract_ytdlp_tags(source_data)

    else:
        raise ValueError(f"Unknown source type: {source_type}")


def detect_metadata_request(tag: str) -> Optional[Dict[str, str]]:
    trimmed = value_normalize(tag)
    if not trimmed:
        return None
    lower = trimmed.lower()
    imdb_match = re.match(r"^imdb:\s*(tt[\w]+)$", lower)
    if imdb_match:
        imdb_id = imdb_match.group(1)
        return {
            "source": "imdb",
            "id": imdb_id,
            "base": f"imdb:{imdb_id}",
        }
    remainder = re.match(r"^musicbrainz:\s*(.+)$", lower)
    if remainder:
        raw = remainder.group(1)
        entity = "release"
        identifier = raw
        specific = re.match(r"^(?P<entity>[a-zA-Z]+)\s*:\s*(?P<id>[\w-]+)$", raw)
        if specific:
            entity = specific.group("entity")
            identifier = specific.group("id")
        identifier = identifier.replace(" ", "")
        if identifier:
            return {
                "source": "musicbrainz",
                "entity": entity.lower(),
                "id": identifier,
                "base": f"musicbrainz:{identifier}",
            }
    return None


def expand_metadata_tag(payload: Dict[str, Any]) -> Dict[str, Any]:
    tag = payload.get("tag")
    if not isinstance(tag, str):
        return {
            "tag": []
        }
    trimmed = value_normalize(tag)
    if not trimmed:
        return {
            "tag": []
        }
    request = detect_metadata_request(trimmed)
    tags: List[str] = []
    seen: Set[str] = set()
    if request:
        _append_unique(tags, seen, request["base"])
    else:
        _append_unique(tags, seen, trimmed)
        return {
            "tag": tags
        }
    try:
        if request["source"] == "imdb":
            data = imdb_tag(request["id"])
        else:
            data = fetch_musicbrainz_tags(request["id"], request["entity"])
    except Exception as exc:  # pragma: no cover - network/service errors
        return {
            "tag": tags,
            "error": str(exc)
        }
    # Add tags from fetched data (no namespace, just unique append)
    raw_tags = data.get("tag") if isinstance(data, dict) else None
    if isinstance(raw_tags, str):
        tag_iter: Iterable[str] = [raw_tags]
    elif isinstance(raw_tags, (list, tuple, set)):
        tag_iter = [t for t in raw_tags if isinstance(t, str)]
    else:
        tag_iter = []
    for tag_value in tag_iter:
        _append_unique(tags, seen, tag_value)
    result = {
        "tag": tags,
        "source": request["source"],
        "id": request["id"],
    }
    if request["source"] == "musicbrainz":
        result["entity"] = request["entity"]
    return result


def build_remote_bundle(
    metadata: Optional[Dict[str,
                            Any]],
    existing: Optional[Sequence[str]] = None,
    context: Optional[Dict[str,
                           Any]] = None,
) -> Dict[str,
          Any]:
    metadata = metadata or {}
    context = context or {}
    tags: List[str] = []
    seen: Set[str] = set()
    if existing:
        for tag in existing:
            _append_unique(tags, seen, tag)

    # Add tags from various sources
    for tag in metadata.get("tag") or []:
        _append_unique(tags, seen, tag)
    for tag in metadata.get("categories") or []:
        _append_unique(tags, seen, tag)

    # Extract and namespace genres
    raw_genres = metadata.get("genres")
    keywords = metadata.get("keywords")
    if isinstance(keywords, str):
        for token in keywords.split(","):
            _append_unique(tags, seen, token)
    if raw_genres:
        for genre in (raw_genres if isinstance(raw_genres,
                                               (list,
                                                tuple)) else [raw_genres]):
            if genre:
                _append_unique(tags, seen, f"genre:{genre}")

    # Extract creators/artists
    artists = metadata.get("artists") or metadata.get("artist")
    if artists:
        artist_list = artists if isinstance(artists, (list, tuple)) else [artists]
        for artist in artist_list:
            if artist:
                _append_unique(tags, seen, f"creator:{artist}")

    creator = (
        metadata.get("uploader") or metadata.get("channel") or metadata.get("artist")
        or metadata.get("creator")
    )
    if creator:
        _append_unique(tags, seen, f"creator:{creator}")

    # Extract title
    title_value = metadata.get("title")
    if title_value:
        _extend_namespaced(tags, seen, "title", [title_value])
    source_url = (
        context.get("source_url") or metadata.get("original_url")
        or metadata.get("webpage_url") or metadata.get("url")
    )
    clean_title = value_normalize(str(title_value)) if title_value is not None else None
    result = {
        "tag": tags,
        "title": clean_title,
        "source_url": _sanitize_url(source_url),
        "duration": _coerce_duration(metadata),
        "metadata": metadata,
    }
    return result


def _load_payload(value: Optional[str]) -> Dict[str, Any]:
    text = value
    if text is None:
        text = sys.stdin.read()
    if text is None or text.strip() == "":
        raise ValueError("Expected JSON payload")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Payload must be a JSON object")
    return data


import typer

app = typer.Typer(help="Fetch metadata tags for known services")


@app.command(help="Lookup an IMDb title")
def imdb(imdb_id: str = typer.Argument(..., help="IMDb identifier (ttXXXXXXX)")):
    """Lookup an IMDb title."""
    try:
        result = imdb_tag(imdb_id)
        debug(json.dumps(result, ensure_ascii=False), flush=True)
    except Exception as exc:
        error_payload = {
            "error": str(exc)
        }
        debug(json.dumps(error_payload, ensure_ascii=False), flush=True)
        raise typer.Exit(code=1)


@app.command(help="Lookup a MusicBrainz entity")
def musicbrainz(
    mbid: str = typer.Argument(...,
                               help="MusicBrainz identifier (UUID)"),
    entity: str = typer.Option(
        "release",
        help="Entity type (release, recording, artist)"
    ),
):
    """Lookup a MusicBrainz entity."""
    try:
        result = fetch_musicbrainz_tags(mbid, entity)
        debug(json.dumps(result, ensure_ascii=False), flush=True)
    except Exception as exc:
        error_payload = {
            "error": str(exc)
        }
        debug(json.dumps(error_payload, ensure_ascii=False), flush=True)
        raise typer.Exit(code=1)


@app.command(name="remote-tags", help="Normalize a remote metadata payload")
def remote_tags(
    payload: Optional[str] = typer.Option(
        None,
        "--payload",
        help="JSON payload; reads stdin if omitted"
    )
):
    """Normalize a remote metadata payload."""
    try:
        payload_data = _load_payload(payload)
        metadata = payload_data.get("metadata") or {}
        existing = payload_data.get("existing_tags") or []
        context = payload_data.get("context") or {}
        if not isinstance(existing, list):
            raise ValueError("existing_tags must be a list")
        if context and not isinstance(context, dict):
            raise ValueError("context must be an object")
        result = build_remote_bundle(metadata, existing, context)
        debug(json.dumps(result, ensure_ascii=False), flush=True)
    except Exception as exc:
        error_payload = {
            "error": str(exc)
        }
        debug(json.dumps(error_payload, ensure_ascii=False), flush=True)
        raise typer.Exit(code=1)


@app.command(name="remote-fetch", help="Resolve remote metadata bundle")
def remote_fetch(
    payload: Optional[str] = typer.Option(
        None,
        "--payload",
        help="JSON payload; reads stdin if omitted"
    )
):
    """Resolve remote metadata bundle."""
    try:
        payload_data = _load_payload(payload)
        result = resolve_remote_metadata(payload_data)
        debug(json.dumps(result, ensure_ascii=False), flush=True)
    except Exception as exc:
        error_payload = {
            "error": str(exc)
        }
        debug(json.dumps(error_payload, ensure_ascii=False), flush=True)
        raise typer.Exit(code=1)


@app.command(name="expand-tag", help="Expand metadata references into tags")
def expand_tag(
    payload: Optional[str] = typer.Option(
        None,
        "--payload",
        help="JSON payload; reads stdin if omitted"
    )
):
    """Expand metadata references into tags."""
    try:
        payload_data = _load_payload(payload)
        result = expand_metadata_tag(payload_data)
        debug(json.dumps(result, ensure_ascii=False), flush=True)
    except Exception as exc:
        error_payload = {
            "error": str(exc)
        }
        debug(json.dumps(error_payload, ensure_ascii=False), flush=True)
        raise typer.Exit(code=1)


@app.command(name="sync-sidecar", help="Synchronise .tag sidecar with supplied data")
def sync_sidecar_cmd(
    payload: Optional[str] = typer.Option(
        None,
        "--payload",
        help="JSON payload; reads stdin if omitted"
    )
):
    """Synchronise .tag sidecar with supplied data."""
    try:
        payload_data = _load_payload(payload)
        result = sync_sidecar(payload_data)
        debug(json.dumps(result, ensure_ascii=False), flush=True)
    except Exception as exc:
        error_payload = {
            "error": str(exc)
        }
        debug(json.dumps(error_payload, ensure_ascii=False), flush=True)
        raise typer.Exit(code=1)


@app.command(name="update-tag", help="Update or rename a tag")
def update_tag_cmd(
    payload: Optional[str] = typer.Option(
        None,
        "--payload",
        help="JSON payload; reads stdin if omitted"
    )
):
    """Update or rename a tag."""
    try:
        payload_data = _load_payload(payload)
        result = apply_tag_mutation(payload_data, "update")
        debug(json.dumps(result, ensure_ascii=False), flush=True)
    except Exception as exc:
        error_payload = {
            "error": str(exc)
        }
        debug(json.dumps(error_payload, ensure_ascii=False), flush=True)
        raise typer.Exit(code=1)


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point using Typer."""
    try:
        app(argv, standalone_mode=False)
        return 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


# ============================================================================
# TAG OPERATIONS - Consolidated from tag_operations.py and tag_helpers.py
# ============================================================================


def sort_tags(tags: List[str]) -> List[str]:
    """
    Sort tags into namespace tags and freeform tags, then alphabetically.

    Args:
        tags: List of tag strings

    Returns:
        Sorted list with namespace tags first, then freeform tags
    """
    if not tags:
        return []

    namespace_tags = []
    freeform_tags = []

    for tag in tags:
        if isinstance(tag, str):
            if ":" in tag:
                namespace_tags.append(tag)
            else:
                freeform_tags.append(tag)

    namespace_tags.sort()
    freeform_tags.sort()

    return namespace_tags + freeform_tags


def format_tags_display(tags: List[str],
                        namespace_filter: Optional[str] = None) -> List[str]:
    """
    Format tags for display, optionally filtered by namespace.

    Args:
        tags: List of tags
        namespace_filter: Optional namespace to filter by (e.g., "creator:")

    Returns:
        Formatted list of tags
    """
    if not tags:
        return []

    if namespace_filter:
        filtered = [t for t in tags if t.startswith(namespace_filter)]
        return sort_tags(filtered)

    return sort_tags(tags)


def split_tag(tag: str) -> tuple[str, str]:
    """
    Split a tag into namespace and value.

    Args:
        tag: Tag string (e.g., "creator:Author Name" or "freeform tag")

    Returns:
        Tuple of (namespace, value). For freeform tags, namespace is empty string.
    """
    if ":" in tag:
        parts = tag.split(":", 1)
        return parts[0], parts[1]
    return "", tag


def filter_tags_by_namespace(tags: List[str], namespace: str) -> List[str]:
    """
    Get all tags in a specific namespace.

    Args:
        tags: List of tags
        namespace: Namespace to filter by

    Returns:
        List of values in that namespace
    """
    prefix = namespace + ":"
    return [split_tag(t)[1] for t in tags if t.startswith(prefix)]


def ensure_title_tag(tags: List[str], title: str) -> List[str]:
    """
    Ensure there's a title: tag with the given title.

    Args:
        tags: List of existing tags
        title: Title to ensure exists

    Returns:
        Updated tag list
    """
    if not title:
        return tags

    # Remove any existing title tags
    filtered = [t for t in tags if not t.startswith("title:")]

    # Add new title tag
    new_tags = filtered + [f"title:{title}"]

    return sort_tags(new_tags)


def remove_title_tags(tags: List[str]) -> List[str]:
    """Remove all title: tags."""
    return [t for t in tags if not t.startswith("title:")]


def is_namespace_tag(tag: str) -> bool:
    """Check if a tag is a namespace tag (contains :)."""
    return ":" in tag if isinstance(tag, str) else False


def validate_tag(tag: str) -> bool:
    """
    Validate that a tag is properly formatted.

    Args:
        tag: Tag to validate

    Returns:
        True if tag is valid
    """
    if not isinstance(tag, str) or not tag.strip():
        return False

    # Tag shouldn't have leading/trailing whitespace
    if tag != tag.strip():
        return False

    # Tag shouldn't be empty
    if not tag:
        return False

    return True


def normalize_tags(tags: List[Any]) -> List[str]:
    """
    Normalize a tag list by filtering and cleaning.

    Args:
        tags: List of tags (may contain invalid entries)

    Returns:
        Cleaned list of valid tags
    """
    if not tags:
        return []

    normalized = []
    for tag in tags:
        if isinstance(tag, str):
            trimmed = tag.strip()
            if trimmed and validate_tag(trimmed):
                normalized.append(trimmed)

    return sort_tags(normalized)


UNIQUE_TAG_NAMESPACES = frozenset({"title"})


def ensure_single_title_tag(
    tags: Sequence[Any],
    preferred: Optional[str] = None,
) -> List[str]:
    """Keep at most one title: tag. preferred (value or title:value) replaces any existing title."""
    preferred_text = str(preferred or "").strip()
    if preferred_text.lower().startswith("title:"):
        preferred_text = preferred_text.split(":", 1)[1].strip()
    kept: List[str] = []
    last_title = ""
    for raw in tags or []:
        text = str(raw or "").strip()
        if not text:
            continue
        if text.lower().startswith("title:"):
            value = text.split(":", 1)[1].strip()
            if value:
                last_title = value
            continue
        kept.append(text)
    chosen = preferred_text or last_title
    if chosen:
        kept.insert(0, f"title:{chosen}")
    return kept


def compute_namespaced_tag_overwrite(
    existing_tags: Sequence[Any],
    incoming_tags: Sequence[Any],
) -> Tuple[List[str],
           List[str],
           List[str]]:
    """Compute a tag mutation with namespace overwrite semantics.

    Rules:
    - Incoming namespaced tags ("ns:value") overwrite any existing tags in that namespace.
    - Overwrite is based on namespace match (case-insensitive).
    - Additions are deduped case-insensitively against kept existing tags and within the incoming list.
    - If an existing tag matches an incoming tag exactly, it is kept (no remove/add).

    Returns:
        (tags_to_remove, tags_to_add, merged_tags)

    Notes:
        This is intentionally store-agnostic: stores decide how to persist/apply
        the returned mutation (DB merge write, Hydrus delete/add, etc.).
    """

    def _clean(values: Sequence[Any]) -> List[str]:
        out: List[str] = []
        for v in values or []:
            if not isinstance(v, str):
                continue
            t = v.strip()
            if t:
                out.append(t.lower())
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
    for t in incoming:
        ns = _ns_of(t)
        if ns in UNIQUE_TAG_NAMESPACES:
            unique_last[ns] = t
            continue
        collapsed_incoming.append(t)
    collapsed_incoming.extend(unique_last.values())
    incoming = collapsed_incoming

    namespaces_to_replace: Set[str] = set()
    for t in incoming:
        ns = _ns_of(t)
        if ns:
            namespaces_to_replace.add(ns)

    kept_existing: List[str] = []
    kept_existing_lower: Set[str] = set()
    tags_to_remove: List[str] = []

    for t in existing:
        ns = _ns_of(t)
        if ns and ns in namespaces_to_replace:
            # If it matches exactly, keep it; otherwise remove it.
            if t in incoming:
                kept_existing.append(t)
                kept_existing_lower.add(t.lower())
            else:
                # If incoming has the same tag value but different casing, treat as replace.
                tags_to_remove.append(t)
            continue

        kept_existing.append(t)
        kept_existing_lower.add(t.lower())

    tags_to_add: List[str] = []
    added_lower: Set[str] = set()
    for t in incoming:
        tl = t.lower()
        if tl in kept_existing_lower:
            continue
        if tl in added_lower:
            continue
        tags_to_add.append(t)
        added_lower.add(tl)

    merged = kept_existing + tags_to_add
    return tags_to_remove, tags_to_add, merged


def merge_tag_lists(*tag_lists: List[str]) -> List[str]:
    """
    Merge multiple tag lists, removing duplicates.

    Args:
        *tag_lists: Variable number of tag lists

    Returns:
        Merged, deduplicated, sorted list
    """
    merged = set()
    for tag_list in tag_lists:
        if isinstance(tag_list, list):
            merged.update(tag_list)

    return sort_tags(list(merged))


def tag_diff(old_tags: List[str], new_tags: List[str]) -> Dict[str, List[str]]:
    """
    Calculate the difference between two tag lists.

    Args:
        old_tags: Original tags
        new_tags: New tags

    Returns:
        Dict with 'added' and 'removed' keys
    """
    old_set = set(old_tags) if old_tags else set()
    new_set = set(new_tags) if new_tags else set()

    return {
        "added": sorted(list(new_set - old_set)),
        "removed": sorted(list(old_set - new_set))
    }


def expand_tag_lists(tags_set: Set[str]) -> Set[str]:
    """Expand tag list references like {psychology} to actual tags from adjective.json.

    Removes the reference after expansion (e.g., {psychology} is deleted, psychology tags added).

    Args:
        tags_set: Set of tag strings that may include {list_name} references

    Returns:
        Set of expanded tags with all {list_name} references replaced with actual tags
    """
    # Load adjective.json from workspace root
    adjective_path = Path(__file__).parent / "adjective.json"
    if not adjective_path.exists():
        debug(f"adjective.json not found at {adjective_path}")
        return tags_set

    try:
        with open(adjective_path, "r", encoding="utf-8") as f:
            adjective_lists = json.load(f)
    except Exception as e:
        debug(f"Error loading adjective.json: {e}")
        return tags_set

    expanded_tags = set()
    for tag in tags_set:
        # Check if tag is a list reference like {psychology}
        if tag.startswith("{") and tag.endswith("}"):
            list_name = tag[1:-1].lower()  # Extract name, make lowercase

            # Find matching list (case-insensitive)
            matched_list = None
            for key in adjective_lists.keys():
                if key.lower() == list_name:
                    matched_list = adjective_lists[key]
                    break

            if matched_list:
                # Add all tags from the list
                expanded_tags.update(matched_list)
                debug(f"Expanded {tag} to {len(matched_list)} tags")
            else:
                # List not found, log warning but don't add the reference
                debug(f"Tag list '{list_name}' not found in adjective.json")
        else:
            # Regular tag, keep as is
            expanded_tags.add(tag)

    return expanded_tags


def process_tags_from_string(tags_str: str, expand_lists: bool = False) -> Set[str]:
    """Process a tag string into a set of tags.

    Handles:
    - Multiple formats: comma-separated, newline-separated, space-separated
    - Tag list expansion: {psychology} -> psychology tags (if expand_lists=True)
    - Whitespace trimming

    Args:
        tags_str: Raw tag string
        expand_lists: If True, expand {list_name} references using adjective.json

    Returns:
        Set of processed tags
    """
    if not tags_str:
        return set()

    # Try to detect delimiter and split accordingly
    # Prefer newlines, then commas, then spaces
    if "\n" in tags_str:
        delimiter = "\n"
    elif "," in tags_str:
        delimiter = ","
    else:
        delimiter = " "

    # Split and clean tags
    tags_set = set()
    for tag in tags_str.split(delimiter):
        tag = tag.strip()
        if tag:
            tags_set.add(tag)

    # Expand list references if requested
    if expand_lists:
        tags_set = expand_tag_lists(tags_set)

    return tags_set


def build_book_tags(
    *,
    title: Optional[str] = None,
    author: Optional[str] = None,
    isbn: Optional[str] = None,
    year: Optional[str] = None,
    source: Optional[str] = None,
    extra: Optional[Sequence[str]] = None,
) -> List[str]:
    """Build consistent book tags for downloads (LibGen, OpenLibrary, etc.)."""
    tags: List[str] = ["book"]

    def _add(tag: Optional[str]) -> None:
        if tag and isinstance(tag, str) and tag.strip():
            tags.append(tag.strip())

    _add(source)
    if title:
        _add(f"title:{title}")
    if author:
        _add(f"author:{author}")
    if isbn:
        _add(f"isbn:{isbn}")
    if year:
        _add(f"year:{year}")
    if extra:
        for tag in extra:
            _add(tag)

    # Deduplicate while preserving order
    deduped = list(dict.fromkeys(tags))
    return deduped


def enrich_playlist_entries(entries: list, extractor: str) -> list:
    """Enrich playlist entries with full metadata by fetching individual entry info.

    When extract_flat is used, entries contain minimal info (title, id, url).
    This function fetches full metadata for each entry.

    Args:
        entries: List of entry dicts from probe_url
        extractor: Extractor name

    Returns:
        List of enriched entry dicts
    """
    if not entries:
        return entries

    plugin = get_plugin("ytdlp", {})
    if plugin is None:
        return entries

    try:
        enriched = plugin.enrich_playlist_entries(entries, extractor=extractor)
    except Exception:
        logger.exception("Failed to enrich playlist entries for extractor: %s", extractor)
        return entries

    return enriched if isinstance(enriched, list) else entries


def format_playlist_entry(entry: Dict[str,
                                      Any],
                          index: int,
                          extractor: str) -> Dict[str,
                                                  Any]:
    """Format a playlist entry for display in result table.

    Args:
        entry: Single playlist entry from yt-dlp (fully enriched if possible)
        index: 1-based track number
        extractor: Extractor name (youtube, bandcamp, spotify, etc.)

    Returns:
        Dict with displayable fields for result table
    """
    result = {
        "index": index,
        "title": entry.get("title",
                           "Unknown"),
        "duration": entry.get("duration") or entry.get("length") or 0,
        "uploader": entry.get("uploader") or entry.get("creator") or "",
        "artist": entry.get("artist") or entry.get("uploader") or entry.get("creator")
        or "",
        "album": entry.get("album") or "",
        "track_number": entry.get("track_number") or index,
    }

    # Normalize extractor for comparison
    ext_lower = extractor.lower().replace(":", "").replace(" ", "")

    # Add site-specific fields
    if "youtube" in ext_lower:
        result["video_id"] = entry.get("id", "")
        result["channel"] = entry.get("uploader") or entry.get("channel", "")
        result["views"] = entry.get("view_count", 0)

    elif "bandcamp" in ext_lower:
        result["track_number"] = entry.get("track_number") or index
        # For Bandcamp album entries, track info may be in different fields
        result["artist"] = entry.get("artist") or entry.get("uploader", "")
        result["album"] = entry.get("album") or ""

    elif "spotify" in ext_lower:
        result["artists"] = entry.get("creator") or entry.get("uploader", "")
        result["album"] = entry.get("album", "")
        result["release_date"] = entry.get("release_date", "")

    return result


# ============================================================================
# Metadata helper functions for tag processing and scraping
# ============================================================================


def extract_title_from_tags(tags_list: List[str]) -> Optional[str]:
    """Extract title from tags list."""
    try:
        extracted = extract_title(tags_list)
        if extracted:
            return extracted
    except Exception:
        logger.exception("extract_title failed while extracting title from tags")

    for t in tags_list:
        if isinstance(t, str) and t.lower().startswith("title:"):
            val = t.split(":", 1)[1].strip()
            if val:
                return val
    return None


def summarize_tags(tags_list: List[str], limit: int = 8) -> str:
    """Create a summary of tags for display."""
    shown = [t for t in tags_list[:limit] if t]
    summary = ", ".join(shown)
    remaining = max(0, len(tags_list) - len(shown))
    if remaining > 0:
        summary = f"{summary} (+{remaining} more)" if summary else f"(+{remaining} more)"
    if len(summary) > 200:
        summary = summary[:197] + "..."
    return summary


def extract_scrapable_identifiers(tags_list: List[str]) -> Dict[str, str]:
    """Extract scrapable identifiers from tags."""
    identifiers = {}
    scrapable_prefixes = {
        "openlibrary",
        "isbn",
        "isbn_10",
        "isbn_13",
        "musicbrainz",
        "musicbrainzalbum",
        "imdb",
        "tmdb",
        "tvdb",
    }

    for tag in tags_list:
        if not isinstance(tag, str) or ":" not in tag:
            continue

        parts = tag.split(":", 1)
        if len(parts) != 2:
            continue

        key_raw = parts[0].strip().lower()
        key = key_raw.replace("-", "_")
        if key == "isbn10":
            key = "isbn_10"
        elif key == "isbn13":
            key = "isbn_13"
        value = parts[1].strip()

        # Normalize ISBN values by removing hyphens for API friendliness
        if key.startswith("isbn"):
            value = value.replace("-", "")

        if key in scrapable_prefixes and value:
            identifiers[key] = value

    return identifiers


def extract_tag_value(tags_list: List[str], namespace: str) -> Optional[str]:
    """Get first tag value for a namespace (e.g., artist:, title:)."""
    ns = namespace.lower()
    for tag in tags_list:
        if not isinstance(tag, str) or ":" not in tag:
            continue
        prefix, _, value = tag.partition(":")
        if prefix.strip().lower() != ns:
            continue
        candidate = value.strip()
        if candidate:
            return candidate
    return None


def scrape_url_metadata(
    url: str,
) -> Tuple[Optional[str],
           List[str],
           List[Tuple[str,
                      str]],
           List[Dict[str,
                     Any]]]:
    """Scrape metadata from a URL using yt-dlp.

    Returns:
        (title, tags, formats, playlist_items) tuple where:
        - title: Video/content title
        - tags: List of extracted tags (both namespaced and freeform)
        - formats: List of (display_label, format_id) tuples
        - playlist_items: List of playlist entry dicts (empty if not a playlist)
    """
    try:
        import json as json_module

        # Build yt-dlp command with playlist support
        # IMPORTANT: Do NOT use --flat-playlist! It strips metadata like artist, album, uploader, genre
        # Without it, yt-dlp gives us full metadata in an 'entries' array within a single JSON object
        # This ensures we get album-level metadata from sources like BandCamp, YouTube Music, etc.
        cmd = [
            "yt-dlp",
            "-j",  # Output JSON
            "--no-warnings",
            "--playlist-items",
            "1-10",  # Get first 10 items if it's a playlist (provides entries)
            "-f",
            "best",
            url,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            log(f"yt-dlp error: {result.stderr}", file=sys.stderr)
            return None, [], [], []

        # Parse JSON output - WITHOUT --flat-playlist, we get ONE JSON object with 'entries' array
        # This gives us full metadata instead of flat format
        lines = result.stdout.strip().split("\n")
        if not lines or not lines[0]:
            log("yt-dlp returned empty output", file=sys.stderr)
            return None, [], [], []

        # Parse the single JSON object
        try:
            data = json_module.loads(lines[0])
        except json_module.JSONDecodeError as e:
            log(f"Failed to parse yt-dlp JSON: {e}", file=sys.stderr)
            return None, [], [], []

        # Extract title - use the main title
        title = data.get("title", "Unknown")

        # Determine if this is a playlist/album (has entries array)
        # is_playlist = 'entries' in data and isinstance(data.get('entries'), list)

        # Extract tags and playlist items
        tags: List[str] = []
        playlist_items: List[Dict[str, Any]] = []

        # IMPORTANT: Extract album/playlist-level tags FIRST (before processing entries)
        # This ensures we get metadata about the collection, not just individual tracks
        album_tags = extract_ytdlp_tags(data)
        tags.extend(album_tags)

        # Case 1: Entries are nested in the main object (standard playlist structure)
        if "entries" in data and isinstance(data.get("entries"), list):
            entries = data["entries"]
            # Build playlist items with title and duration
            for idx, entry in enumerate(entries, 1):
                if isinstance(entry, dict):
                    item_title = entry.get("title", entry.get("id", f"Track {idx}"))
                    item_duration = entry.get("duration", 0)
                    playlist_items.append(
                        {
                            "index": idx,
                            "id": entry.get("id",
                                            f"track_{idx}"),
                            "title": item_title,
                            "duration": item_duration,
                            "url": entry.get("url") or entry.get("webpage_url",
                                                                 ""),
                        }
                    )

                    # Extract tags from each entry and merge (but don't duplicate album-level tags)
                    # Only merge entry tags that are multi-value prefixes (not single-value like title:, artist:, etc.)
                    entry_tags = extract_ytdlp_tags(entry)

                    # Single-value namespaces that should not be duplicated from entries
                    single_value_namespaces = {
                        "title",
                        "artist",
                        "album",
                        "creator",
                        "channel",
                        "release_date",
                        "upload_date",
                        "license",
                        "location",
                    }

                    for tag in entry_tags:
                        # Extract the namespace (part before the colon)
                        tag_namespace = tag.split(":",
                                                  1)[0].lower(
                                                  ) if ":" in tag else None

                        # Skip if this namespace already exists in tags (from album level)
                        if tag_namespace and tag_namespace in single_value_namespaces:
                            # Check if any tag with this namespace already exists in tags
                            already_has_namespace = any(
                                t.split(":",
                                        1)[0].lower() == tag_namespace for t in tags
                                if ":" in t
                            )
                            if already_has_namespace:
                                continue  # Skip this tag, keep the album-level one

                        if tag not in tags:  # Avoid exact duplicates
                            tags.append(tag)

        # Case 2: Playlist detected by playlist_count field (BandCamp albums, etc.)
        # These need a separate call with --flat-playlist to get the actual entries
        elif (data.get("playlist_count") or 0) > 0 and "entries" not in data:
            try:
                # Make a second call with --flat-playlist to get the actual tracks
                flat_cmd = [
                    "yt-dlp",
                    "-j",
                    "--no-warnings",
                    "--flat-playlist",
                    "-f",
                    "best",
                    url
                ]
                flat_result = subprocess.run(
                    flat_cmd,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                if flat_result.returncode == 0:
                    flat_lines = flat_result.stdout.strip().split("\n")
                    # With --flat-playlist, each line is a separate track JSON object
                    # (not nested in a playlist container), so process ALL lines
                    for idx, line in enumerate(flat_lines, 1):
                        if line.strip().startswith("{"):
                            try:
                                entry = json_module.loads(line)
                                item_title = entry.get(
                                    "title",
                                    entry.get("id",
                                              f"Track {idx}")
                                )
                                item_duration = entry.get("duration", 0)
                                playlist_items.append(
                                    {
                                        "index":
                                        idx,
                                        "id":
                                        entry.get("id",
                                                  f"track_{idx}"),
                                        "title":
                                        item_title,
                                        "duration":
                                        item_duration,
                                        "url":
                                        entry.get("url")
                                        or entry.get("webpage_url",
                                                     ""),
                                    }
                                )
                            except json_module.JSONDecodeError:
                                logger.debug("Failed to decode flat playlist line %d as JSON: %r", idx, line[:200])
            except Exception:
                logger.exception("yt-dlp flat-playlist extraction failed for URL: %s", url)

        # Fallback: if still no tags detected, get from first item
        if not tags:
            tags = extract_ytdlp_tags(data)

        # Extract formats from the main data object
        formats = []
        if "formats" in data:
            formats = extract_url_formats(data.get("formats", []))

        # Deduplicate tags by namespace to prevent duplicate title:, artist:, etc.
        tags = dedup_tags_by_namespace(tags, keep_first=True)

        return title, tags, formats, playlist_items

    except subprocess.TimeoutExpired:
        log("yt-dlp timeout (>30s)", file=sys.stderr)
        return None, [], [], []
    except Exception as e:
        log(f"URL scraping error: {e}", file=sys.stderr)
        return None, [], [], []


def extract_url_formats(formats: list) -> List[Tuple[str, str]]:
    """Extract best formats from yt-dlp formats list.

    Returns list of (display_label, format_id) tuples.
    """
    try:
        video_formats: Dict[str, Dict[str, Any]] = {}  # {resolution: format_data}
        audio_formats: Dict[str, Dict[str, Any]] = {}  # {quality_label: format_data}

        for fmt in formats:
            vcodec = fmt.get("vcodec", "none")
            acodec = fmt.get("acodec", "none")
            height = fmt.get("height")
            ext = fmt.get("ext", "unknown")
            format_id = fmt.get("format_id", "")
            tbr = fmt.get("tbr", 0)
            abr = fmt.get("abr", 0)

            # Video format
            if vcodec and vcodec != "none" and height:
                if height < 480:
                    continue
                res_key = f"{height}p"
                if res_key not in video_formats or tbr > video_formats[res_key].get(
                        "tbr",
                        0):
                    video_formats[res_key] = {
                        "label": f"{height}p ({ext})",
                        "format_id": format_id,
                        "tbr": tbr,
                    }

            # Audio-only format
            elif acodec and acodec != "none" and (not vcodec or vcodec == "none"):
                audio_key = f"audio_{abr}"
                if audio_key not in audio_formats or abr > audio_formats[audio_key].get(
                        "abr",
                        0):
                    audio_formats[audio_key] = {
                        "label": f"audio ({ext})",
                        "format_id": format_id,
                        "abr": abr,
                    }

        result: List[Tuple[str, str]] = []

        # Add video formats in descending resolution order
        for res in sorted(video_formats.keys(),
                          key=lambda x: int(x.replace("p", "")),
                          reverse=True):
            fmt = video_formats[res]
            result.append((fmt["label"], fmt["format_id"]))

        # Add best audio format
        if audio_formats:
            best_audio = max(audio_formats.values(), key=lambda x: x.get("abr", 0))
            result.append((best_audio["label"], best_audio["format_id"]))

        return result

    except Exception as e:
        log(f"Error extracting formats: {e}", file=sys.stderr)
        return []

def prepare_ffmpeg_metadata(payload: Optional[dict[str, Any]]) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    metadata: dict[str, str] = {}

    def set_field(key: str, raw: Any, limit: int = 2000) -> None:
        sanitized = sanitize_metadata_value(raw)
        if not sanitized:
            return
        if len(sanitized) > limit:
            sanitized = sanitized[:limit]
        metadata[key] = sanitized

    set_field("title", payload.get("title"))
    set_field("artist", payload.get("artist"), 512)
    set_field("album", payload.get("album"), 512)
    set_field("date", payload.get("year") or payload.get("date"), 20)
    comment = payload.get("comment")
    tags_value = payload.get("tags")
    tag_strings: list[str] = []
    artists_from_tags: list[str] = []
    albums_from_tags: list[str] = []
    genres_from_tags: list[str] = []
    if isinstance(tags_value, list):
        for raw_tag in tags_value:
            if raw_tag is None:
                continue
            if not isinstance(raw_tag, str):
                raw_tag = str(raw_tag)
            tag = raw_tag.strip()
            if not tag:
                continue
            tag_strings.append(tag)
            namespace, sep, value = tag.partition(":")
            if sep and value:
                ns = namespace.strip().lower()
                value = value.strip()
                if ns in {"artist", "creator", "author", "performer"}:
                    artists_from_tags.append(value)
                elif ns in {"album", "series", "collection", "group"}:
                    albums_from_tags.append(value)
                elif ns in {"genre", "rating"}:
                    genres_from_tags.append(value)
                elif ns in {"comment", "description"} and not comment:
                    comment = value
                elif ns in {"year", "date"} and not (payload.get("year") or payload.get("date")):
                    set_field("date", value, 20)
            else:
                genres_from_tags.append(tag)
    if "artist" not in metadata and artists_from_tags:
        set_field("artist", ", ".join(unique_preserve_order(artists_from_tags)[:3]), 512)
    if "album" not in metadata and albums_from_tags:
        set_field("album", unique_preserve_order(albums_from_tags)[0], 512)
    if genres_from_tags:
        set_field("genre", ", ".join(unique_preserve_order(genres_from_tags)[:5]), 256)
    if tag_strings:
        joined_tags = ", ".join(tag_strings[:50])
        set_field("keywords", joined_tags, 2000)
        if not comment:
            comment = joined_tags
    if comment:
        set_field("comment", str(comment), 2000)
        set_field("description", str(comment), 2000)
    return metadata


def apply_mutagen_metadata(path: Path, metadata: dict[str, str], fmt: str) -> None:
    if fmt != "audio":
        return
    if not metadata:
        return
    if mutagen is None:
        return
    try:
        audio = mutagen.File(path, easy=True)  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - best effort only
        log(f"mutagen load failed: {exc}", file=sys.stderr)
        return
    if audio is None:
        return
    field_map = {
        "title": "title",
        "artist": "artist",
        "album": "album",
        "genre": "genre",
        "comment": "comment",
        "description": "comment",
        "date": "date",
    }
    changed = False
    for source_key, target_key in field_map.items():
        value = metadata.get(source_key)
        if not value:
            continue
        try:
            audio[target_key] = [value]
            changed = True
        except Exception:  # pragma: no cover - best effort only
            logger.exception("mutagen: failed to set field %s for %s", target_key, path)
            continue
    if not changed:
        return
    try:
        audio.save()
    except Exception as exc:  # pragma: no cover - best effort only
        log(f"mutagen save failed: {exc}", file=sys.stderr)
        logger.exception("mutagen save failed for %s", path)


def build_ffmpeg_command(
    ffmpeg_path: str,
    input_path: Path,
    output_path: Path,
    fmt: str,
    max_width: int,
    metadata: Optional[dict[str, str]] = None,
) -> list[str]:
    cmd = [ffmpeg_path, "-y", "-i", str(input_path)]
    if fmt in {"mp4", "webm"} and max_width and max_width > 0:
        cmd.extend(["-vf", f"scale='min({max_width},iw)':-2"])
    if metadata:
        for key, value in metadata.items():
            cmd.extend(["-metadata", f"{key}={value}"])

    # Video formats
    if fmt == "mp4":
        cmd.extend([
            "-c:v",
            "libx265",
            "-preset",
            "medium",
            "-crf",
            "26",
            "-tag:v",
            "hvc1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
        ])
    elif fmt == "webm":
        cmd.extend([
            "-c:v",
            "libvpx-vp9",
            "-b:v",
            "0",
            "-crf",
            "32",
            "-c:a",
            "libopus",
            "-b:a",
            "160k",
        ])
        cmd.extend(["-f", "webm"])

    # Audio formats
    elif fmt == "mp3":
        cmd.extend([
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
        ])
        cmd.extend(["-f", "mp3"])
    elif fmt == "flac":
        cmd.extend([
            "-vn",
            "-c:a",
            "flac",
        ])
        cmd.extend(["-f", "flac"])
    elif fmt == "wav":
        cmd.extend([
            "-vn",
            "-c:a",
            "pcm_s16le",
        ])
        cmd.extend(["-f", "wav"])
    elif fmt == "aac":
        cmd.extend([
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ])
        cmd.extend(["-f", "adts"])
    elif fmt == "m4a":
        cmd.extend([
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ])
        cmd.extend(["-f", "ipod"])
    elif fmt == "ogg":
        cmd.extend([
            "-vn",
            "-c:a",
            "libvorbis",
            "-b:a",
            "192k",
        ])
        cmd.extend(["-f", "ogg"])
    elif fmt == "opus":
        cmd.extend([
            "-vn",
            "-c:a",
            "libopus",
            "-b:a",
            "192k",
        ])
        cmd.extend(["-f", "opus"])
    if fmt not in ("mp4", "webm", "mp3", "flac", "wav", "aac", "m4a", "ogg", "opus", "copy"):
        raise ValueError(f"Unsupported format: {fmt}")

    cmd.append(str(output_path))
    return cmd

