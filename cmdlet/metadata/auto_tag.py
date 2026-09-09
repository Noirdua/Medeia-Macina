from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from SYS import pipeline as ctx
from SYS.item_accessors import extract_item_tags, get_field, set_field
from SYS.logger import log
from .. import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
parse_cmdlet_args = sh.parse_cmdlet_args
normalize_hash = sh.normalize_hash
normalize_result_input = sh.normalize_result_input

_DATE_LEAD = re.compile(
    r"^\s*(\d{1,4})[.\-/ ]+(\d{1,2})[.\-/ ]+(\d{1,4})\b\s*(.*)$"
)
_CREATOR_SPLIT = re.compile(r"\s+(?:w\/?|by)\s+(.+)$", re.I)
_ARTIST_TITLE_SPLIT = re.compile(
    r"^(?P<artist>.+?)\s+[-~—–]+\s+(?P<title>.+)$"
)
_AUDIO_EXT = frozenset({
    "flac",
    "mp3",
    "m4a",
    "ogg",
    "opus",
    "wav",
    "aac",
    "wma",
    "aiff",
    "ape",
    "alac",
})
_URL_SCRAPE_SKIP_NS = frozenset({"subs", "subs_auto", "source"})
_DETAIL_PANEL_LIMIT = 9
_BOOK_EXT = frozenset({
    "pdf",
    "epub",
    "mobi",
    "djvu",
    "azw",
    "azw3",
    "fb2",
    "cbz",
    "cbr",
    "txt",
    "doc",
    "docx",
})


def _ns_map(tags: Sequence[str]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for raw in tags or []:
        text = str(raw or "").strip()
        if not text or ":" not in text:
            continue
        ns, val = text.split(":", 1)
        key = ns.strip().lower()
        value = val.strip()
        if not key or not value:
            continue
        out.setdefault(key, [])
        if value not in out[key]:
            out[key].append(value)
    return out


def _readable_title(text: str) -> str:
    cleaned = str(text or "").replace("_", " ").strip()
    return re.sub(r"\s+", " ", cleaned).strip()


def _item_title(item: Any, have: Dict[str, List[str]]) -> str:
    for key in ("title", "name"):
        vals = have.get(key) or []
        for val in vals:
            text = _readable_title(val)
            if text and not _looks_like_hash_title(text):
                return text
    for field in ("title", "name"):
        try:
            text = _readable_title(get_field(item, field) or "")
        except Exception:
            text = ""
        if text and not _looks_like_hash_title(text):
            return text
        if text:
            continue
    return ""


def _tags_from_item(item: Any) -> List[str]:
    tags: List[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        tags.append(text)

    for tag in extract_item_tags(item) or []:
        _add(tag)
    blob = None
    try:
        blob = get_field(item, "tag")
    except Exception:
        blob = None
    if isinstance(blob, str) and ":" in blob:
        for part in re.split(r",\s*", blob):
            if ":" in part:
                _add(part)
    try:
        display_title = str(get_field(item, "title") or "").strip()
    except Exception:
        display_title = ""
    if display_title and not any(str(t).lower().startswith("title:") for t in tags):
        _add(f"title:{display_title}")
    return tags


def _title_values(tags: Sequence[str]) -> List[str]:
    out: List[str] = []
    for raw in tags or []:
        text = str(raw or "").strip()
        if not text.lower().startswith("title:"):
            continue
        value = text.split(":", 1)[1].strip()
        if value:
            out.append(value)
    return out


_HASHISH_TITLE = re.compile(r"^[a-fA-F0-9]{8,64}$")


def _looks_like_hash_title(text: str) -> bool:
    return bool(_HASHISH_TITLE.fullmatch(str(text or "").strip()))


def _best_title(tags: Sequence[str]) -> str:
    values = _title_values(tags)
    if not values:
        return ""
    ranked = [
        value for value in values
        if not str(value).rstrip().endswith("-")
        and len(str(value).strip()) > 2
        and not _looks_like_hash_title(value)
    ]
    pool = ranked or [value for value in values if not _looks_like_hash_title(value)]
    if not pool:
        return ""

    def _score(value: str) -> tuple:
        text = str(value or "").strip()
        underscored = "_" in text and " " not in text
        return (
            0 if underscored else 1,
            1 if any(ch.isupper() for ch in text) else 0,
            text.count(" "),
            len(_readable_title(text)),
        )

    return _readable_title(max(pool, key=_score))


def _without_title_tags(tags: Sequence[str]) -> List[str]:
    return [str(t) for t in tags if not str(t).strip().lower().startswith("title:")]


def _with_single_title(tags: Sequence[str], title: str = "") -> List[str]:
    chosen = str(title or _best_title(tags) or "").strip()
    out = _without_title_tags(tags)
    if chosen:
        out.insert(0, f"title:{chosen}")
    return out


def _column_value(item: Any, *names: str) -> str:
    wanted = {str(name).strip().lower() for name in names}
    cols = getattr(item, "columns", None)
    if cols is None and isinstance(item, dict):
        cols = item.get("columns")
    if isinstance(cols, list):
        for col in cols:
            label = ""
            value = ""
            if isinstance(col, (tuple, list)) and len(col) >= 2:
                label, value = str(col[0]), col[1]
            else:
                label = str(getattr(col, "name", "") or "")
                value = getattr(col, "value", None)
            if label.strip().lower() in wanted and value not in (None, ""):
                return str(value).strip()
    return ""


def _item_ext(item: Any) -> str:
    for field in ("ext", "extension"):
        try:
            text = str(get_field(item, field) or "").lower().lstrip(".")
        except Exception:
            text = ""
        if text:
            return text
    from_col = _column_value(item, "ext", "Ext")
    if from_col:
        return from_col.lower().lstrip(".")
    try:
        path = str(get_field(item, "path") or "")
    except Exception:
        path = ""
    return Path(path).suffix.lower().lstrip(".") if path else ""


def _item_duration_seconds(item: Any) -> Optional[float]:
    for field in ("duration_seconds", "duration"):
        try:
            raw = get_field(item, field)
        except Exception:
            raw = None
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except Exception:
            text = str(raw)
            parts = re.findall(r"\d+", text)
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
    return None


def _split_artist_title(title: str) -> Optional[Tuple[str, str]]:
    text = str(title or "").strip()
    if not text:
        return None
    match = _ARTIST_TITLE_SPLIT.match(text)
    if not match:
        return None
    artist = match.group("artist").strip(" -~")
    track = match.group("title").strip(" -~")
    if len(artist) < 2 or len(track) < 1:
        return None
    if _DATE_LEAD.match(text):
        return None
    return artist, track


def suggest_tags(title: str, existing: Sequence[str], config: Optional[Dict[str, Any]] = None, item: Any = None) -> List[str]:
    have = _ns_map(existing)
    existing_lower = {str(t).strip().lower() for t in existing if str(t).strip()}
    proposed: List[str] = []

    rest = str(title or "").strip()
    match = _DATE_LEAD.match(rest)
    if match and "date" not in have:
        a, b, c, rest = match.group(1), match.group(2), match.group(3), match.group(4).strip()
        from cmdlet._tag_utils import _format_tag_date, _parse_tag_date

        raw = f"{a} {b} {c}"
        parsed = _parse_tag_date(raw)
        if parsed is None:
            parsed = _parse_tag_date(f"{a}-{b}-{c}")
        if parsed is not None:
            try:
                from SYS.config import get_date_format

                fmt = get_date_format(config)
            except Exception:
                fmt = "YYYY-MM-DD"
            proposed.append(f"date:{_format_tag_date(parsed, fmt)}")

    creator_match = _CREATOR_SPLIT.search(rest)
    if creator_match and "creator" not in have:
        creator = re.sub(r"\s+\d+$", "", creator_match.group(1).strip()).strip()
        if creator:
            proposed.append(f"creator:{creator}")
        rest = rest[: creator_match.start()].strip()

    rest = re.sub(r"\s+broadcast\s*$", "", rest, flags=re.I).strip(" -")
    existing_titles = [str(v).strip() for v in (have.get("title") or []) if str(v).strip()]
    best_existing = max(existing_titles, key=len) if existing_titles else ""
    if rest and not _looks_like_hash_title(rest) and (not best_existing or len(rest) > len(best_existing)):
        proposed.append(f"title:{rest}")

    split = _split_artist_title(rest or title)
    audioish = False
    if item is not None:
        audioish = _looks_audio(item, have) or _item_ext(item) in _AUDIO_EXT
        secs = _item_duration_seconds(item)
        if secs is not None and 20 <= secs <= 20 * 60:
            audioish = True
    if split and audioish:
        artist, track = split
        if "artist" not in have:
            proposed.append(f"artist:{artist}")
        if "track" not in have:
            proposed.append(f"track:{track}")

    if "series" not in have:
        for ns in ("channel", "podcast", "series"):
            values = have.get(ns) or []
            if values:
                proposed.append(f"series:{values[0]}")
                break

    out: List[str] = []
    seen = set(existing_lower)
    for tag in proposed:
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def _looks_book(item: Any, have: Dict[str, List[str]]) -> bool:
    ext = _item_ext(item)
    mime = ""
    try:
        mime = str(get_field(item, "mime") or "").lower()
    except Exception:
        mime = ""
    bookish = (ext in _BOOK_EXT) or mime in {
        "application/pdf",
        "application/epub+zip",
        "application/x-mobipocket-ebook",
    }
    if ext and ext not in _BOOK_EXT and not bookish:
        return False
    if have.get("author") or have.get("authors") or have.get("creator"):
        return True
    return bool(bookish and (have.get("title") or _item_title(item, have)))


def _norm_book_author(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _author_matches(item_author: str, candidate_author: str) -> bool:
    a = _norm_book_author(item_author)
    if not a:
        return True
    ca = _norm_book_author(candidate_author)
    if not ca:
        return False
    if a in ca or ca in a:
        return True
    return bool(set(a.split()) & set(ca.split()))


def _looks_audio(item: Any, have: Dict[str, List[str]]) -> bool:
    if have.get("artist") or have.get("album") or have.get("track") or have.get("albumartist"):
        return True
    ext = _item_ext(item)
    if ext in _AUDIO_EXT:
        return True
    mime = ""
    try:
        mime = str(get_field(item, "mime") or "").lower()
    except Exception:
        mime = ""
    if mime.startswith("audio/"):
        return True
    secs = _item_duration_seconds(item)
    title = _item_title(item, have)
    if _split_artist_title(title) and secs is not None and 20 <= secs <= 20 * 60:
        return True
    return False


def _mutagen_tags(path: str) -> List[str]:
    file_path = Path(path)
    if not file_path.is_file():
        return []
    try:
        import mutagen

        audio = mutagen.File(str(file_path), easy=True)
    except Exception:
        return []
    if audio is None:
        return []
    mapping = {
        "artist": "artist",
        "albumartist": "albumartist",
        "album": "album",
        "title": "title",
        "genre": "genre",
        "date": "year",
        "tracknumber": "track",
    }
    out: List[str] = []
    for key, ns in mapping.items():
        try:
            values = audio.get(key) or []
        except Exception:
            values = []
        for raw in values:
            text = str(raw or "").strip()
            if text:
                if ns == "track":
                    text = text.split("/")[0].strip()
                if ns == "year":
                    text = text[:4]
                out.append(f"{ns}:{text}")
    return out


@lru_cache(maxsize=256)
def _musicbrainz_tags(artist: str, track: str) -> List[str]:
    artist = str(artist or "").strip()
    track = str(track or "").strip()
    if not artist or not track:
        return []
    try:
        from PluginCore.registry import plugin_attr

        MusicBrainzMetadataPlugin = plugin_attr("metadata_plus", "MusicBrainzMetadataPlugin") or plugin_attr(
            "metadata_plugin", "MusicBrainzMetadataPlugin"
        )
        if MusicBrainzMetadataPlugin is None:
            return []
        plugin = MusicBrainzMetadataPlugin()
        query = plugin.combined_query(title_hint=track, artist_hint=artist) or f"{artist} {track}"
        hits = plugin.search(query, limit=1)
        if not hits:
            return []
        tags = plugin.to_tags(hits[0])
        extra: List[str] = []
        hit = hits[0]
        if hit.get("artist"):
            extra.append(f"artist:{hit['artist']}")
        if hit.get("album"):
            extra.append(f"album:{hit['album']}")
        if hit.get("title"):
            extra.append(f"title:{hit['title']}")
            extra.append(f"track:{hit['title']}")
        return extra + tags
    except Exception:
        return []


def _music_offshoot_tags(item: Any, existing: Sequence[str]) -> List[str]:
    have = _ns_map(existing)
    if not _looks_audio(item, have) and not have.get("musicbrainz"):
        return []
    out: List[str] = []
    path = ""
    try:
        path = str(get_field(item, "path") or "")
    except Exception:
        path = ""
    if path:
        out.extend(_mutagen_tags(path))
    artist = (have.get("artist") or have.get("albumartist") or have.get("creator") or [""])[0]
    track = (have.get("track") or have.get("title") or [""])[0]
    if not track:
        track = _item_title(item, have)
    if artist and track:
        out.extend(_musicbrainz_tags(artist, track))
    return out


@lru_cache(maxsize=256)
def _fetch_lyrics(artist: str, track: str, album: str = "") -> str:
    artist = str(artist or "").strip()
    track = str(track or "").strip()
    if not artist or not track:
        return ""
    try:
        import httpx

        params: Dict[str, Any] = {"artist_name": artist, "track_name": track}
        if album:
            params["album_name"] = str(album).strip()
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            response = client.get("https://lrclib.net/api/get", params=params)
            data: Any = None
            if response.status_code == 200:
                data = response.json()
            else:
                search = client.get(
                    "https://lrclib.net/api/search",
                    params={"artist_name": artist, "track_name": track},
                )
                if search.status_code == 200:
                    payload = search.json()
                    if isinstance(payload, list) and payload:
                        data = payload[0]
        if not isinstance(data, dict):
            return ""
        text = str(data.get("plainLyrics") or data.get("syncedLyrics") or "").strip()
        return text
    except Exception:
        return ""


def _is_probe_url(url: str) -> bool:
    text = str(url or "").strip()
    if not text.startswith(("http://", "https://")):
        return False
    low = text.lower()
    if (
        "hydrus-client-api" in low
        or "/get_files/" in low
        or "/add_urls/" in low
        or "/view_file" in low
        or "/thumbnail" in low
        or "access_key=" in low
    ):
        return False
    if "localhost" in low or "127.0.0.1" in low:
        return False
    return True


def _urls_from_hydrus_meta(meta: Any) -> List[str]:
    if not isinstance(meta, dict):
        return []
    raw_urls = meta.get("known_urls") or meta.get("urls") or meta.get("url") or []
    if isinstance(raw_urls, str):
        return [raw_urls.strip()] if raw_urls.strip() else []
    if not isinstance(raw_urls, (list, tuple, set)):
        return []
    out: List[str] = []
    for value in raw_urls:
        text = str(value or "").strip()
        if text:
            out.append(text)
    return out


def _prefetch_hydrus_urls(items: Sequence[Any], config: Dict[str, Any]) -> Dict[Tuple[str, str], List[str]]:
    grouped: Dict[str, List[str]] = {}
    for item in items:
        file_hash, store = _item_hash_store(item)
        if not file_hash or not store:
            continue
        grouped.setdefault(store, []).append(file_hash)
    if not grouped:
        return {}
    try:
        from PluginCore.registry import get_plugin

        hydrus = get_plugin("hydrusnetwork", config)
    except Exception:
        hydrus = None
    if hydrus is None:
        return {}
    cached: Dict[Tuple[str, str], List[str]] = {}
    for store, hashes in grouped.items():
        try:
            if not hydrus.is_store_name(store):
                continue
            _name, backend = hydrus.resolve_backend(store)
            fetcher = getattr(backend, "fetch_files_metadata", None)
            if not callable(fetcher):
                continue
            payload = fetcher(hashes, include_file_url=True)
        except Exception:
            continue
        entries = payload.get("metadata") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            continue
        for meta in entries:
            if not isinstance(meta, dict):
                continue
            hash_text = str(meta.get("hash") or "").strip().lower()
            if not hash_text:
                continue
            cached[(store, hash_text)] = _urls_from_hydrus_meta(meta)
    return cached


def _collect_item_urls(
    item: Any,
    existing: Sequence[str],
    config: Dict[str, Any],
    known_urls: Optional[Dict[Tuple[str, str], List[str]]] = None,
) -> List[str]:
    urls: List[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        values = raw if isinstance(raw, (list, tuple, set)) else [raw]
        for value in values:
            text = str(value or "").strip()
            if not _is_probe_url(text):
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            urls.append(text)

    for field in ("url", "webpage_url", "source_url", "known_urls"):
        try:
            _add(get_field(item, field))
        except Exception:
            pass
    extra = getattr(item, "extra", None) if not isinstance(item, dict) else item.get("extra")
    if isinstance(extra, dict):
        _add(extra.get("url") or extra.get("known_urls"))
    for tag in existing:
        text = str(tag or "")
        low = text.lower()
        if low.startswith("url:"):
            _add(text.split(":", 1)[1].strip())
        elif low.startswith("internet_archive:") or low.startswith("ocaid:"):
            ident = text.split(":", 1)[1].strip()
            if ident:
                _add(f"https://archive.org/details/{ident}")
    file_hash, store = _item_hash_store(item)
    if file_hash and store:
        cached = (known_urls or {}).get((store, file_hash.lower()))
        if cached is not None:
            _add(cached)
        else:
            try:
                from PluginCore.registry import get_plugin

                hydrus = get_plugin("hydrusnetwork", config)
                if hydrus is not None and hydrus.is_store_name(store):
                    _name, backend = hydrus.resolve_backend(store)
                    getter = getattr(backend, "get_url", None)
                    if callable(getter):
                        _add(getter(file_hash))
            except Exception:
                pass
    return urls


@lru_cache(maxsize=64)
def _scrape_url_tags(url: str) -> Tuple[str, ...]:
    try:
        from PluginCore.registry import plugin_attr

        get_metadata_plugin_for_url = plugin_attr("metadata_plus", "get_metadata_plugin_for_url") or plugin_attr(
            "metadata_plugin", "get_metadata_plugin_for_url"
        )
        if get_metadata_plugin_for_url is None:
            return ()
        plugin = get_metadata_plugin_for_url(url)
        if plugin is None:
            return ()
        payload = plugin.scrape_url_payload(url)
    except Exception:
        return ()
    if not isinstance(payload, dict):
        return ()
    tags = payload.get("tag") or []
    out: List[str] = []
    if payload.get("title"):
        out.append(f"title:{payload['title']}")
    for tag in tags:
        text = str(tag or "").strip()
        if not text:
            continue
        ns = text.split(":", 1)[0].strip().lower() if ":" in text else ""
        if ns in _URL_SCRAPE_SKIP_NS:
            continue
        out.append(text)
    return tuple(out)


def _url_offshoot_tags(
    item: Any,
    existing: Sequence[str],
    config: Dict[str, Any],
    known_urls: Optional[Dict[Tuple[str, str], List[str]]] = None,
) -> List[str]:
    collected: List[str] = []
    for url in _collect_item_urls(item, existing, config, known_urls=known_urls):
        scraped = list(_scrape_url_tags(url))
        if scraped:
            collected.extend(scraped)
            break
    return collected


def _openlibrary_isbn_from_edition(session: Any, olid: str) -> str:
    olid = str(olid or "").strip()
    if not olid:
        return ""
    try:
        from PluginCore.registry import plugin_attr

        openlibrary_json_urls = plugin_attr("metadata_plus", "openlibrary_json_urls") or plugin_attr(
            "metadata_plugin", "openlibrary_json_urls"
        )
        if openlibrary_json_urls is None:
            return ""
        data = None
        for url in openlibrary_json_urls(olid):
            try:
                resp = session.get(url, timeout=10)
                resp.raise_for_status()
                payload = resp.json()
            except Exception:
                continue
            if isinstance(payload, dict) and payload:
                data = payload
                break
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    for field in ("isbn_13", "isbn_10"):
        values = data.get(field)
        if isinstance(values, list):
            for v in values:
                if str(v).strip():
                    return str(v).strip()
        elif isinstance(values, str) and values.strip():
            return values.strip()
    return ""


def _archive_ids_from_tags(existing: Sequence[str]) -> List[str]:
    ids: List[str] = []
    seen: set[str] = set()
    for tag in existing or []:
        text = str(tag or "").strip()
        low = text.lower()
        ident = ""
        if low.startswith("internet_archive:") or low.startswith("ocaid:"):
            ident = text.split(":", 1)[1].strip()
        if ident:
            key = ident.lower()
            if key not in seen:
                seen.add(key)
                ids.append(ident)
    return ids


def _book_isbn_candidates(
    title: str,
    author: str,
    archive_ids: Optional[Sequence[str]] = None,
) -> Tuple[Dict[str, Any], ...]:
    title = _readable_title(title)
    author = str(author or "").strip()
    if not title and not archive_ids:
        return ()
    try:
        from API.requests_client import get_requests_session
        from PluginCore.registry import plugin_attr

        OpenLibraryMetadataPlugin = plugin_attr("metadata_plus", "OpenLibraryMetadataPlugin") or plugin_attr(
            "metadata_plugin", "OpenLibraryMetadataPlugin"
        )
        if OpenLibraryMetadataPlugin is None:
            return ()
        session = get_requests_session()
        plugin = OpenLibraryMetadataPlugin({})
        queries = []
        for ident in archive_ids or []:
            ident = str(ident or "").strip()
            if ident:
                queries.append(f"ia:{ident}")
                queries.append(ident)
        if title and author:
            queries.append(f'title:"{title}" author:"{author}"')
            queries.append(f"{title} {author}")
        if title:
            queries.append(title)
        items: List[Any] = []
        seen_keys: set[str] = set()
        for query in queries:
            for it in plugin.search(query, limit=8) or []:
                ids = it.get("identifiers") or {}
                key = str(ids.get("openlibrary") or ids.get("isbn_13") or ids.get("isbn_10") or it.get("title") or "").strip()
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                items.append(it)
            if items:
                break
    except Exception as exc:
        log(f"OpenLibrary book lookup failed: {exc}", file=sys.stderr)
        return ()
    candidates: List[Dict[str, Any]] = []
    seen_id: set[str] = set()
    for it in items:
        ids = it.get("identifiers") or {}
        isbn = str(ids.get("isbn_13") or ids.get("isbn_10") or "").strip()
        olid = str(ids.get("openlibrary") or "").strip()
        if not isbn and olid:
            isbn = _openlibrary_isbn_from_edition(session, olid)
        key = (isbn.replace("-", "") or olid or str(it.get("title") or "")).strip()
        if not key or key in seen_id:
            continue
        seen_id.add(key)
        authors = [str(a).strip() for a in (it.get("authors") or []) if str(a).strip()]
        tags = list(plugin.to_tags(it))
        if isbn and not any(str(t).lower().startswith(("isbn_13:", "isbn_10:")) for t in tags):
            tags.append(f"isbn_13:{isbn.replace('-', '')}" if len(isbn.replace("-", "")) == 13 else f"isbn_10:{isbn.replace('-', '')}")
        candidates.append(
            {
                "title": str(it.get("title") or "").strip(),
                "author": ", ".join(authors),
                "year": str(it.get("year") or "").strip(),
                "isbn": isbn.replace("-", ""),
                "openlibrary": olid,
                "tags": tags,
            }
        )
    return tuple(candidates)


def _publish_book_candidates(
    ambiguous: Sequence[Dict[str, Any]],
    config: Dict[str, Any],
) -> bool:
    if not ambiguous:
        return False
    try:
        from SYS.result_publication import publish_result_table
        from SYS.result_table import Table
    except Exception:
        return False

    table = Table("ISBN matches")
    table.set_table("metadata.isbn")
    table.set_source_command("metadata", ["-add"])
    selection_payload: List[Dict[str, Any]] = []
    for entry in ambiguous:
        item_title = str(entry.get("item_title") or "").strip()
        file_hash = str(entry.get("hash") or "").strip()
        store = str(entry.get("store") or entry.get("instance") or "").strip()
        plugin = str(entry.get("plugin") or "hydrusnetwork").strip()
        for candidate in entry.get("candidates") or []:
            tags = [str(t) for t in candidate.get("tags") or [] if str(t).strip()]
            title = str(candidate.get("title") or item_title)
            payload = {
                "tag": tags,
                "hash": file_hash,
                "store": store,
                "instance": store,
                "plugin": plugin,
                "is_temp": False,
                "title": title,
                "author": str(candidate.get("author") or ""),
                "isbn": str(candidate.get("isbn") or ""),
                "openlibrary": str(candidate.get("openlibrary") or ""),
                "columns": [
                    ("Title", title),
                    ("Author", str(candidate.get("author") or "")),
                    ("Year", str(candidate.get("year") or "")),
                    ("ISBN", str(candidate.get("isbn") or "")),
                ],
                "extra": {"tag": tags, "store": store, "hash": file_hash},
                "_selection_action": ["metadata", "-add"],
                "_selection_args": [],
            }
            table.add_result(payload)
            selection_payload.append(payload)
    if not selection_payload:
        return False
    publish_result_table(ctx, table, selection_payload, overlay=False)
    try:
        ctx.set_current_stage_table(table)
    except Exception:
        pass
    return True


def _llm_extra_tags(
    title: str,
    existing: Sequence[str],
    config: Dict[str, Any],
) -> List[str]:
    url = str((config or {}).get("autotag_llm_url") or "").strip()
    if not url:
        plugins = (config or {}).get("plugin") or {}
        block = plugins.get("autotag") if isinstance(plugins, dict) else {}
        if isinstance(block, dict):
            url = str(block.get("url") or block.get("URL") or "").strip()
    if not url:
        return []
    model = str((config or {}).get("autotag_llm_model") or "").strip()
    if not model:
        plugins = (config or {}).get("plugin") or {}
        block = plugins.get("autotag") if isinstance(plugins, dict) else {}
        if isinstance(block, dict):
            model = str(block.get("model") or "").strip()
    model = model or "llama3.2"
    prompt = (
        "Given this media title and existing tags, return ONLY a JSON array of extra "
        "namespace:value tags to add. Use lowercase namespaces. Do not repeat existing tags. "
        "No commentary.\n"
        f"Title: {title}\n"
        f"Tags: {', '.join(existing)}\n"
    )
    try:
        import httpx

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        with httpx.Client(timeout=20.0) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        text = ""
        choices = data.get("choices") if isinstance(data, dict) else None
        if isinstance(choices, list) and choices:
            msg = (choices[0] or {}).get("message") or {}
            text = str(msg.get("content") or "")
        if not text and isinstance(data, dict):
            text = str(data.get("response") or "")
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            return []
        parsed = json.loads(text[start : end + 1])
        out: List[str] = []
        for item in parsed if isinstance(parsed, list) else []:
            tag = str(item or "").strip()
            if ":" in tag:
                out.append(tag)
        return out
    except Exception:
        return []


def _display_auto_tag_items(items: List[Any]) -> None:
    if not items:
        return
    try:
        stage_ctx = ctx.get_stage_context()
        is_last = (stage_ctx is None) or bool(getattr(stage_ctx, "is_last_stage", False))
    except Exception:
        is_last = True
    if not is_last:
        return
    try:
        live_progress = ctx.get_live_progress()
    except Exception:
        live_progress = None
    if live_progress is not None:
        try:
            live_progress.stop()
        except Exception:
            pass
        try:
            if hasattr(ctx, "set_live_progress"):
                ctx.set_live_progress(None)
        except Exception:
            pass
    try:
        subject = items[0] if len(items) == 1 else list(items)
        display_type = "item" if len(items) <= _DETAIL_PANEL_LIMIT else "custom"
        sh.display_and_persist_items(
            list(items),
            title="Result",
            subject=subject,
            display_type=display_type,
        )
    except Exception:
        pass


def _item_hash_store(item: Any) -> Tuple[str, str]:
    hash_value = normalize_hash(
        get_field(item, "hash")
        or get_field(item, "hash_hex")
        or get_field(item, "file_hash")
        or ""
    )
    store = str(
        get_field(item, "instance")
        or get_field(item, "store")
        or get_field(item, "plugin_instance")
        or ""
    ).strip()
    meta = get_field(item, "full_metadata")
    if not isinstance(meta, dict):
        meta = get_field(item, "metadata")
    if isinstance(meta, dict):
        if not hash_value:
            hash_value = normalize_hash(
                meta.get("hash") or meta.get("hash_hex") or meta.get("file_hash") or ""
            )
        if not store or store.lower() in {"path", "url", "local"}:
            store = str(meta.get("instance") or meta.get("store") or store).strip()
    path_text = str(get_field(item, "path") or get_field(item, "url") or "").strip()
    if path_text.lower().startswith("hydrus://"):
        parts = path_text.split("://", 1)[-1].split("/", 1)
        if parts and not store:
            store = str(parts[0] or "").strip()
        if len(parts) > 1 and not hash_value:
            hash_value = normalize_hash(parts[1])
    if store.lower() in {"path", "url", "local"}:
        store = str(get_field(item, "instance") or "").strip() or store
    return str(hash_value or ""), store


class Auto_Tag(Cmdlet):
    def __init__(self) -> None:
        super().__init__(
            name="auto-tag",
            summary="Suggest and add tags from title and existing text metadata.",
            usage="metadata -auto [-preview]",
            arg=[
                CmdletArg(
                    "-preview",
                    type="flag",
                    required=False,
                    description="Show suggested tags without writing them",
                ),
                CmdletArg(
                    "-refresh",
                    type="flag",
                    required=False,
                    description="Replace existing tags with the new set, only if lookup succeeds",
                ),
            ],
            detail=[
                "Reads title and current tags (text only). No image models.",
                "Fills missing date/title/creator/series from the title when possible.",
                "Optional LLM: set Preferences autotag_llm_url to an OpenAI-compatible endpoint (Ollama).",
            ],
            examples=[
                "@1-20 | metadata -auto -preview",
                "@1-20 | metadata -auto",
                "@1-20 | metadata -auto -refresh",
            ],
            exec=self.run,
        )

    def run(self, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        parsed = parse_cmdlet_args(args, self)
        preview = bool(parsed.get("preview"))
        refresh = bool(parsed.get("refresh"))
        items = normalize_result_input(result)
        if not items:
            try:
                items = list(ctx.get_last_result_items() or [])
            except Exception:
                items = []
        if not items:
            log("metadata -auto: no piped rows. Use @N | metadata -auto", file=sys.stderr)
            return 1

        from SYS.pipeline_progress import PipelineProgress

        progress = PipelineProgress(ctx)
        total = len(items)
        progress.ensure_local_ui(
            label="auto-tag",
            total_items=total,
            items_preview=[str(get_field(item, "title") or "")[:40] for item in items[:5]],
        )
        try:
            progress.begin_pipe(total_items=total)
        except Exception:
            pass

        def _close_progress() -> None:
            try:
                progress.close_local_ui(force_complete=True)
            except Exception:
                pass
            try:
                live_progress = ctx.get_live_progress()
            except Exception:
                live_progress = None
            if live_progress is not None:
                try:
                    live_progress.stop()
                except Exception:
                    pass
                try:
                    ctx.set_live_progress(None)
                except Exception:
                    pass

        pending: Dict[str, List[Tuple[str, List[str], List[str]]]] = {}
        pending_notes: Dict[str, List[Tuple[str, str, str]]] = {}
        display_items: List[Any] = []
        ambiguous_books: List[Dict[str, Any]] = []
        try:
            progress.set_status("loading hydrus urls")
        except Exception:
            pass
        known_urls = _prefetch_hydrus_urls(items, config or {})
        for idx, item in enumerate(items, 1):
            existing = _tags_from_item(item)
            suggested: List[str] = []
            offshoot_ok = False
            try:
                from cmdlet.metadata.tag_get import _extract_scrapable_identifiers, _perform_scraping

                if _extract_scrapable_identifiers(existing):
                    scraped = _perform_scraping(existing)
                    if scraped:
                        offshoot_ok = True
                    for extra in scraped:
                        if extra.lower() not in {t.lower() for t in existing + suggested}:
                            suggested.append(extra)
            except Exception:
                pass
            title = _item_title(item, _ns_map(existing + suggested))
            if _looks_like_hash_title(title):
                recovered = _best_title(suggested)
                if recovered:
                    title = recovered
            try:
                progress.set_percent(int(round(((idx - 1) / max(1, total)) * 100)))
                progress.set_status(f"auto-tag {idx}/{total}: {title[:48] or 'item'}")
            except Exception:
                pass
            for extra in suggest_tags(title, existing + suggested, config, item=item):
                if extra.lower() not in {t.lower() for t in existing + suggested}:
                    suggested.append(extra)
            try:
                progress.set_status(f"auto-tag {idx}/{total}: probing urls")
            except Exception:
                pass
            url_tags = _url_offshoot_tags(
                item, existing + suggested, config or {}, known_urls=known_urls
            )
            if url_tags:
                offshoot_ok = True
            for extra in url_tags:
                if extra.lower() not in {t.lower() for t in existing + suggested}:
                    suggested.append(extra)
            music_tags = _music_offshoot_tags(item, existing + suggested)
            if any(str(t).lower().startswith(("musicbrainz:", "album:", "artist:")) for t in music_tags):
                offshoot_ok = True
            for extra in music_tags:
                if extra.lower() not in {t.lower() for t in existing + suggested}:
                    suggested.append(extra)
            book_candidates: List[Dict[str, Any]] = []
            have_now_early = _ns_map(existing + suggested)
            picker_choice = bool(
                str(get_field(item, "openlibrary") or "").strip()
                or str(get_field(item, "isbn") or "").strip()
            )
            if picker_choice:
                for extra in extract_item_tags(item) or []:
                    if extra.lower() not in {t.lower() for t in existing + suggested}:
                        suggested.append(extra)
            elif (
                not have_now_early.get("openlibrary")
                and not have_now_early.get("isbn")
                and not have_now_early.get("isbn_13")
                and not any(str(t).lower().startswith("isbn") for t in existing + suggested)
                and _looks_book(item, have_now_early)
            ):
                author = (have_now_early.get("author") or have_now_early.get("creator") or [""])[0]
                candidates = _book_isbn_candidates(
                    title,
                    author,
                    archive_ids=_archive_ids_from_tags(existing + suggested),
                )
                matched = [
                    c for c in candidates
                    if not author or _author_matches(author, str(c.get("author") or ""))
                ]
                unique = matched if len(matched) == 1 else (list(candidates) if len(candidates) == 1 else [])
                if unique:
                    offshoot_ok = True
                    for extra in unique[0]["tags"]:
                        if str(extra).lower().startswith("source:"):
                            continue
                        if extra.lower() not in {t.lower() for t in existing + suggested}:
                            suggested.append(extra)
                elif len(candidates) > 1:
                    ordered = matched + [c for c in candidates if c not in matched]
                    file_hash_b, store_b = _item_hash_store(item)
                    book_candidates.append(
                        {
                            "item_title": title,
                            "hash": file_hash_b,
                            "store": store_b,
                            "instance": store_b or str(get_field(item, "instance") or "").strip(),
                            "plugin": str(get_field(item, "plugin") or "hydrusnetwork").strip(),
                            "candidates": list(ordered),
                        }
                    )
            if book_candidates:
                ambiguous_books.extend(book_candidates)
            mb_title = ""
            for extra in music_tags:
                if str(extra).lower().startswith("title:"):
                    mb_title = extra.split(":", 1)[1].strip()
                    break
            removes: List[str] = []
            if mb_title:
                removes.extend(
                    t for t in extract_item_tags(item) or []
                    if str(t).lower().startswith("title:")
                )
                suggested = [t for t in suggested if not str(t).lower().startswith("title:")]
                suggested.insert(0, f"title:{mb_title}")
                try:
                    from cmdlet.metadata.tag_add import _apply_title_to_result

                    _apply_title_to_result(item, mb_title)
                except Exception:
                    pass
                title = mb_title
            for extra in _llm_extra_tags(title, existing + suggested, config or {}):
                if extra.lower() not in {t.lower() for t in existing + suggested}:
                    suggested.append(extra)
            lyrics_text = ""
            have_now = _ns_map(existing + suggested)
            if _looks_audio(item, have_now):
                artist = (have_now.get("artist") or have_now.get("albumartist") or [""])[0]
                track = (have_now.get("track") or have_now.get("title") or [""])[0] or title
                album = (have_now.get("album") or [""])[0]
                lyrics_text = _fetch_lyrics(artist, track, album)
            real_tags = extract_item_tags(item) or []
            chosen_title = _best_title(existing + suggested)
            suggested = _with_single_title(suggested, chosen_title)
            old_titles = [
                t for t in (real_tags or existing)
                if str(t).strip().lower().startswith("title:")
            ]
            chosen_tag = f"title:{chosen_title}" if chosen_title else ""
            if chosen_tag:
                suggested = [
                    t for t in suggested
                    if not str(t).strip().lower().startswith("title:")
                ]
                if chosen_tag.lower() not in {str(t).strip().lower() for t in old_titles}:
                    suggested.insert(0, chosen_tag)
                for old in old_titles:
                    if old.lower() != chosen_tag.lower() and old not in removes:
                        removes.append(old)
            merged = _with_single_title(existing + suggested, chosen_title)
            try:
                set_field(item, "tag", merged)
            except Exception:
                pass
            if chosen_title:
                try:
                    from cmdlet.metadata.tag_add import _apply_title_to_result

                    _apply_title_to_result(item, chosen_title)
                except Exception:
                    pass
            display_items.append(item)
            file_hash, store = _item_hash_store(item)
            try:
                ctx.patch_cached_result_items(
                    file_hash=file_hash,
                    instance=store,
                    title=chosen_title or None,
                    tags=merged,
                )
            except Exception:
                pass
            if preview:
                continue
            if refresh and offshoot_ok and file_hash and store:
                pending.setdefault(store, []).append((file_hash, suggested, list(real_tags)))
            elif file_hash and store and (suggested or removes):
                pending.setdefault(store, []).append((file_hash, suggested, removes))
            if file_hash and store and lyrics_text:
                pending_notes.setdefault(store, []).append((file_hash, "lyrics", lyrics_text))

        if preview:
            try:
                progress.set_percent(100)
                progress.clear_status()
            except Exception:
                pass
            _close_progress()
            _display_auto_tag_items(display_items)
            return 0

        from PluginCore.registry import get_plugin

        hydrus = get_plugin("hydrusnetwork", config)
        try:
            progress.set_status("writing tags")
        except Exception:
            pass
        added = 0
        for store, entries in pending.items():
            backend = None
            if hydrus is not None:
                try:
                    if hydrus.is_store_name(store):
                        _name, backend = hydrus.resolve_backend(store)
                except Exception:
                    backend = None
            bulk = getattr(backend, "add_tags_bulk", None) if backend is not None else None
            if callable(bulk):
                try:
                    bulk([(h, add_tags, remove_tags) for h, add_tags, remove_tags in entries])
                    added += sum(len(add_tags) for _h, add_tags, _r in entries)
                    continue
                except Exception:
                    pass
            add_one = getattr(backend, "add_tags", None) if backend is not None else None
            del_one = getattr(backend, "delete_tag", None) if backend is not None else None
            for file_hash, add_tags, remove_tags in entries:
                if callable(del_one) and remove_tags:
                    try:
                        del_one(file_hash, remove_tags)
                    except Exception:
                        pass
                if callable(add_one) and add_tags:
                    try:
                        add_one(file_hash, add_tags)
                        added += len(add_tags)
                    except Exception:
                        continue
        notes_set = 0
        for store, notes in pending_notes.items():
            backend = None
            if hydrus is not None:
                try:
                    if hydrus.is_store_name(store):
                        _name, backend = hydrus.resolve_backend(store)
                except Exception:
                    backend = None
            setter = getattr(backend, "set_note", None) if backend is not None else None
            if not callable(setter):
                continue
            for file_hash, name, text in notes:
                try:
                    if setter(file_hash, name, text):
                        notes_set += 1
                except Exception:
                    continue
        try:
            progress.set_percent(100)
            progress.clear_status()
        except Exception:
            pass
        if added or notes_set:
            log(
                f"metadata -auto: added {added} tag(s) and {notes_set} lyrics note(s) across {len(items)} row(s)"
            )
        if ambiguous_books and _publish_book_candidates(ambiguous_books, config or {}):
            _close_progress()
            log(
                "Multiple ISBN matches found. Pick one with @N.",
                file=sys.stderr,
            )
            return 0
        _close_progress()
        _display_auto_tag_items(display_items)
        return 0


CMDLET = Auto_Tag()
Auto_Tag_CMDLET = CMDLET
