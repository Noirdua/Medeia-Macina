"""Storage operations, duplicate checking, and metadata for download-file."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence
from collections.abc import Mapping, Sequence as SequenceABC
from pathlib import Path
import sys
import shutil
import webbrowser
from urllib.parse import urlparse
from contextlib import AbstractContextManager, nullcontext

from SYS.logger import log, debug, debug_panel
from SYS.payload_builders import build_file_result_payload, build_table_result_payload
from SYS.result_table import Table, build_display_row
from SYS.rich_display import stderr_console as get_stderr_console
from SYS import pipeline as pipeline_context
from SYS.utils import sha256_file
from rich.prompt import Prompt
from SYS.selection_builder import build_hash_store_selection
from API.HTTP import download_direct_file

from .. import _shared as sh

from .download_core import Download_File

get_field = sh.get_field
resolve_target_dir = sh.resolve_target_dir


@staticmethod
def _iter_duplicate_tag_values(item: Any) -> List[str]:
    def _append_tag(out: List[str], value: Any) -> None:
        text = ""
        if isinstance(value, bytes):
            try:
                text = value.decode("utf-8", errors="ignore")
            except Exception:
                text = str(value)
        elif isinstance(value, str):
            text = value
        if not text:
            return
        cleaned = text.strip()
        if cleaned:
            out.append(cleaned)

    def _collect_current(container: Any, out: List[str]) -> None:
        if isinstance(container, SequenceABC) and not isinstance(container, (str, bytes, bytearray, Mapping)):
            for tag in container:
                _append_tag(out, tag)
            return
        if not isinstance(container, Mapping):
            return
        current = container.get("0")
        if current is None:
            current = container.get(0)
        if isinstance(current, SequenceABC) and not isinstance(current, (str, bytes, bytearray, Mapping)):
            for tag in current:
                _append_tag(out, tag)

    def _collect_service_data(service_data: Any, out: List[str]) -> None:
        if not isinstance(service_data, Mapping):
            return
        for key in (
            "display_tags",
            "display_friendly_tags",
            "display",
            "storage_tags",
            "statuses_to_tags",
            "tags",
        ):
            _collect_current(service_data.get(key), out)

    collected: List[str] = []
    for raw_tags in (
        get_field(item, "tags_flat"),
        get_field(item, "tags"),
        get_field(item, "tag"),
    ):
        if isinstance(raw_tags, str):
            _append_tag(collected, raw_tags)
            continue
        if isinstance(raw_tags, (list, tuple, set)):
            for raw_tag in raw_tags:
                _append_tag(collected, raw_tag)
            continue
        if not isinstance(raw_tags, Mapping):
            continue

        statuses_map = raw_tags.get("service_keys_to_statuses_to_tags")
        if isinstance(statuses_map, Mapping):
            for status_payload in statuses_map.values():
                _collect_current(status_payload, collected)

        names_map = raw_tags.get("service_keys_to_names")
        if isinstance(names_map, Mapping):
            _ = names_map

        _collect_service_data(raw_tags, collected)
        for maybe_service in raw_tags.values():
            _collect_service_data(maybe_service, collected)

    deduped: List[str] = []
    seen: set[str] = set()
    for raw_tag in collected:
        text = str(raw_tag or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        deduped.append(text)
    return deduped


@staticmethod
def _extract_duplicate_namespace_tags(item: Any) -> List[str]:
    tag_values = _iter_duplicate_tag_values(item)

    namespace_tags: List[str] = []
    seen: set[str] = set()
    for raw_tag in tag_values:
        text = str(raw_tag or "").strip()
        if not text:
            continue
        lower = text.lower()
        if ":" not in text or lower.startswith("title:"):
            continue
        if lower in seen:
            continue
        seen.add(lower)
        namespace_tags.append(text)
    return namespace_tags


@staticmethod
def _extract_duplicate_title_tag(item: Any) -> Optional[str]:
    for raw_tag in _iter_duplicate_tag_values(item):
        tag_text = str(raw_tag or "").strip()
        if not tag_text or not tag_text.lower().startswith("title:"):
            continue
        value = tag_text.split(":", 1)[1].strip()
        if value:
            return value
    return None


@classmethod
def _extract_duplicate_title(cls, item: Any) -> str:
    for key in ("title", "name", "filename", "target"):
        value = get_field(item, key)
        text = str(value or "").strip()
        if text:
            return text

    tag_title = _extract_duplicate_title_tag(item)
    if tag_title:
        return tag_title

    path_value = str(get_field(item, "path") or "").strip()
    if path_value and not path_value.lower().startswith(("http://", "https://", "file://")):
        return path_value

    return "(exists)"


@classmethod
def _has_duplicate_title(cls, item: Any) -> bool:
    return cls._extract_duplicate_title(item) != "(exists)"


@classmethod
def _build_duplicate_display_row(
    cls,
    item: Any,
    *,
    backend_name: str,
    original_url: str,
) -> Dict[str, Any]:
    try:
        extracted = build_display_row(item, keys=["title", "store", "hash", "ext", "size"])
    except Exception:
        extracted = {}

    title = extracted.get("title") or cls._extract_duplicate_title(item)
    store_name = extracted.get("store") or get_field(item, "store") or backend_name
    file_hash = extracted.get("hash") or get_field(item, "hash") or get_field(item, "file_hash") or get_field(item, "hash_hex") or ""
    ext_text = str(extracted.get("ext") or get_field(item, "ext") or "").strip()
    size_raw = extracted.get("size")
    if size_raw is None:
        size_raw = get_field(item, "size_bytes")
    if size_raw is None:
        size_raw = get_field(item, "size")

    if not ext_text:
        for candidate in (get_field(item, "path"), get_field(item, "title"), get_field(item, "name")):
            candidate_text = str(candidate or "").strip()
            if not candidate_text:
                continue
            suffix = Path(candidate_text).suffix.lstrip(".")
            if suffix:
                ext_text = suffix
                break

    title_text = str(title)
    tag_text = ", ".join(_extract_duplicate_namespace_tags(item))
    store_text = str(store_name or backend_name)
    file_hash_text = str(file_hash or "")
    selection_args = None
    selection_action = None
    selection_url = None
    if file_hash_text and store_text and file_hash_text.strip().lower() != "unknown":
        selection_args, selection_action = build_hash_store_selection(
            file_hash_text,
            store_text,
        )
        if selection_args and len(selection_args) >= 2:
            normalized_hash = str(selection_args[1]).split("hash:", 1)[-1].strip()
            if normalized_hash:
                file_hash_text = normalized_hash
                selection_url = f"hydrus://{store_text}/{normalized_hash}"

    columns: List[tuple[str, Any]] = [("Title", title_text)]
    if tag_text:
        columns.append(("Tag", tag_text))
    columns.extend(
        [
            ("Instance", store_text),
            ("Size", size_raw),
            ("Ext", ext_text),
            ("URL", original_url),
        ]
    )

    metadata = dict(item) if isinstance(item, dict) else {}
    if file_hash_text:
        metadata.setdefault("hash", file_hash_text)
    if store_text:
        metadata.setdefault("store", store_text)
    if ext_text:
        metadata.setdefault("ext", ext_text)
    if size_raw is not None:
        metadata.setdefault("size", size_raw)
        metadata.setdefault("size_bytes", size_raw)
    metadata.setdefault("url", original_url)
    if selection_url:
        metadata.setdefault("selection_url", selection_url)

    payload = build_table_result_payload(
        title=title_text,
        columns=columns,
        selection_args=selection_args,
        selection_action=selection_action,
        store=store_text,
        hash=file_hash_text,
        ext=ext_text,
        size=size_raw,
        size_bytes=size_raw,
        url=original_url,
        tags_flat=metadata.get("tags_flat"),
        full_metadata=metadata,
    )
    if selection_url:
        payload["path"] = selection_url
        payload["selection_url"] = selection_url
    return payload


@classmethod
def _extract_hash_from_search_hit(cls, hit: Any) -> Optional[str]:
    if not isinstance(hit, dict):
        return None
    for key in ("hash", "hash_hex", "file_hash", "hydrus_hash"):
        v = hit.get(key)
        normalized = sh.normalize_hash(str(v) if v is not None else None)
        if normalized:
            return normalized
    return None


@classmethod
def _fetch_duplicate_metadata_for_hash(
    cls,
    backend: Any,
    *,
    backend_name: str,
    file_hash: str,
) -> Dict[str, Any]:
    metadata: Optional[Dict[str, Any]] = None

    fetcher = getattr(backend, "fetch_file_metadata", None)
    if callable(fetcher):
        try:
            payload = fetcher(file_hash)
        except TypeError:
            try:
                payload = fetcher(file_hash=file_hash)
            except Exception:
                payload = None
        except Exception:
            payload = None

        if isinstance(payload, dict):
            meta_list = payload.get("metadata")
            if isinstance(meta_list, list) and meta_list and isinstance(meta_list[0], dict):
                metadata = dict(meta_list[0])
            else:
                metadata = dict(payload)

    metadata = cls._enrich_duplicate_metadata(
        metadata,
        backend,
        backend_name=backend_name,
        file_hash=file_hash,
    )

    metadata.setdefault("hash", file_hash)
    metadata.setdefault("store", backend_name)
    return metadata


@classmethod
def _enrich_duplicate_metadata(
    cls,
    metadata: Optional[Dict[str, Any]],
    backend: Any,
    *,
    backend_name: str,
    file_hash: str,
) -> Dict[str, Any]:
    result = dict(metadata) if isinstance(metadata, dict) else {}

    if result:
        extractor = getattr(backend, "_extract_title_and_tags", None)
        if callable(extractor):
            file_id_value = get_field(result, "file_id") or file_hash
            try:
                extracted_title, extracted_tags = extractor(result, file_id_value)
            except Exception:
                extracted_title, extracted_tags = None, None

            if not get_field(result, "tags_flat") and isinstance(extracted_tags, SequenceABC) and not isinstance(extracted_tags, (str, bytes, bytearray, Mapping)):
                deduped_tags: List[str] = []
                seen_tags: set[str] = set()
                for raw_tag in extracted_tags:
                    tag_text = str(raw_tag or "").strip()
                    lowered = tag_text.lower()
                    if not tag_text or lowered in seen_tags:
                        continue
                    seen_tags.add(lowered)
                    deduped_tags.append(tag_text)
                if deduped_tags:
                    result["tags_flat"] = deduped_tags

            title_text = str(extracted_title or "").strip()
            generic_title = f"Hydrus File {file_id_value}".strip()
            if title_text and title_text != generic_title:
                result.setdefault("title", title_text)

    if not result:
        getter = getattr(backend, "get_metadata", None)
        if callable(getter):
            try:
                payload = getter(file_hash)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                result = dict(payload)

    getter = getattr(backend, "get_metadata", None)
    if callable(getter) and not cls._has_duplicate_title(result):
        try:
            getter_payload = getter(file_hash)
        except Exception:
            getter_payload = None
        if isinstance(getter_payload, dict):
            for key, value in getter_payload.items():
                current = result.get(key)
                if current not in (None, "", [], {}, ()):
                    continue
                if value in (None, "", [], {}, ()):
                    continue
                result[key] = value

    return result


@classmethod
def _fetch_duplicate_metadata_for_hashes(
    cls,
    backend: Any,
    *,
    backend_name: str,
    file_hashes: Sequence[str],
) -> Dict[str, Dict[str, Any]]:
    normalized_hashes: List[str] = []
    seen_hashes: set[str] = set()
    for raw_hash in file_hashes or []:
        normalized_hash = sh.normalize_hash(str(raw_hash) if raw_hash is not None else None)
        if not normalized_hash or normalized_hash in seen_hashes:
            continue
        seen_hashes.add(normalized_hash)
        normalized_hashes.append(normalized_hash)

    if not normalized_hashes:
        return {}

    metadata_by_hash: Dict[str, Dict[str, Any]] = {}
    try:
        fetcher = getattr(backend, "fetch_files_metadata", None)
    except Exception:
        fetcher = None
    if callable(fetcher):
        try:
            payload = fetcher(
                normalized_hashes,
                include_service_keys_to_tags=True,
                include_file_url=True,
                include_duration=True,
                include_size=True,
                include_mime=True,
            )
        except TypeError:
            try:
                payload = fetcher(
                    file_hashes=normalized_hashes,
                    include_service_keys_to_tags=True,
                    include_file_url=True,
                    include_duration=True,
                    include_size=True,
                    include_mime=True,
                )
            except Exception:
                payload = None
        except Exception:
            payload = None

        if isinstance(payload, dict):
            meta_list = payload.get("metadata")
            if isinstance(meta_list, list):
                for entry in meta_list:
                    if not isinstance(entry, dict):
                        continue
                    entry_hash = sh.normalize_hash(str(entry.get("hash") or entry.get("hash_hex") or entry.get("file_hash") or ""))
                    if not entry_hash:
                        continue
                    metadata_by_hash[entry_hash] = cls._enrich_duplicate_metadata(
                        dict(entry),
                        backend,
                        backend_name=backend_name,
                        file_hash=entry_hash,
                    )

    for normalized_hash in normalized_hashes:
        metadata = metadata_by_hash.get(normalized_hash)
        if metadata is None:
            metadata = cls._fetch_duplicate_metadata_for_hash(
                backend,
                backend_name=backend_name,
                file_hash=normalized_hash,
            )
        metadata.setdefault("hash", normalized_hash)
        metadata.setdefault("store", backend_name)
        metadata_by_hash[normalized_hash] = metadata

    return metadata_by_hash


@staticmethod
def _init_storage(config: Dict[str, Any]) -> tuple[Any, bool]:
    """Initialize store registry and determine whether a Hydrus backend is usable."""
    storage = None
    try:
        from PluginCore.backend_registry import BackendRegistry as _Store

        storage = _Store(config)
    except Exception:
        storage = None

    hydrus_available = False
    if storage is not None:
        try:
            backend_names = list(storage.list_backends() or [])
        except Exception:
            backend_names = []
        for backend_name in backend_names:
            try:
                backend = storage[backend_name]
            except Exception:
                continue
            if str(getattr(backend, "STORE_TYPE", "")).strip().lower() == "hydrusnetwork":
                hydrus_available = True
                break

    return storage, hydrus_available


@staticmethod
def _supports_storage_duplicate_lookup(raw_url: str) -> bool:
    text = str(raw_url or "").strip()
    if not text:
        return False

    try:
        parsed = urlparse(text)
    except Exception:
        parsed = None

    scheme = str(getattr(parsed, "scheme", "") or "").strip().lower()
    return scheme in {"http", "https", "ftp", "ftps"}


@staticmethod
def _filter_supported_urls(raw_urls: Sequence[str]) -> tuple[List[str], List[str]]:
    """Split explicit URLs into supported and unsupported buckets."""
    supported: List[str] = []
    unsupported: List[str] = []
    for raw in raw_urls or []:
        text = str(raw or "").strip()
        if not text:
            continue
        low = text.lower()
        if low.startswith(("http://", "https://", "ftp://", "ftps://", "magnet:")):
            supported.append(text)
        else:
            unsupported.append(text)
    return supported, unsupported


@staticmethod
def _canonicalize_url_for_storage(
    *,
    requested_url: str,
    provider_name: Optional[str] = None,
    provider_instance: Optional[str] = None,
    provider_item: Optional[Any] = None,
) -> str:
    """Return the URL key used for duplicate preflight lookups."""
    return str(requested_url or "").strip()


@staticmethod
def _preflight_url_duplicate(
    *,
    canonical_url: str,
    storage: Any,
    hydrus_available: bool,
    final_output_dir: Path,
    auto_continue_duplicates: bool = True,
    force_prompt_in_pipeline: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Run duplicate URL preflight against configured storage backends."""
    if not canonical_url or storage is None:
        return True
    return not sh.check_url_exists_in_storage(
        urls=[canonical_url],
        storage=storage,
        hydrus_available=hydrus_available,
        final_output_dir=final_output_dir,
        auto_continue_duplicates=auto_continue_duplicates,
        force_prompt_in_pipeline=force_prompt_in_pipeline,
        config=config,
    )


@classmethod
def _collect_existing_url_match_refs_for_url(
    cls,
    storage: Any,
    canonical_url: str,
    *,
    hydrus_available: bool,
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    if not canonical_url:
        return []
    if not _supports_storage_duplicate_lookup(canonical_url):
        return []

    config_dict = config if isinstance(config, dict) else {}
    refs: List[Dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    seen_backends: set[str] = set()

    def _append_ref(backend_name: str, backend: Any, *, item: Any = None, file_hash_hint: Optional[str] = None, is_exact: bool = False) -> None:
        normalized_hash = sh.normalize_hash(str(file_hash_hint) if file_hash_hint is not None else None)
        if not normalized_hash:
            normalized_hash = cls._extract_hash_from_search_hit(item)
        pair_key = (str(backend_name or "").strip().lower(), str(normalized_hash or ""))
        if pair_key in seen_pairs:
            return
        seen_pairs.add(pair_key)
        refs.append(
            {
                "backend_name": str(backend_name or "").strip(),
                "backend": backend,
                "hash": normalized_hash,
                "item": dict(item) if isinstance(item, dict) else item,
                "is_exact": bool(is_exact),
            }
        )

    def _iter_backends() -> List[tuple[str, Any]]:
        backends: List[tuple[str, Any]] = []
        if storage is not None:
            try:
                backend_names = list(storage.list_searchable_backends() or [])
            except Exception:
                backend_names = []

            for backend_name in backend_names:
                try:
                    backend = storage[backend_name]
                except Exception:
                    continue
                name_text = str(backend_name).strip()
                if not name_text or name_text.lower() == "temp":
                    continue
                key = name_text.lower()
                if key in seen_backends:
                    continue
                seen_backends.add(key)
                backends.append((name_text, backend))

        try:
            registry_helpers = Download_File._load_provider_registry()
            get_plugin = registry_helpers.get("get_plugin")
            hydrus_provider = get_plugin("hydrusnetwork", config_dict) if callable(get_plugin) else None
            if hydrus_provider is not None:
                for backend_name, backend in hydrus_provider.iter_backends():
                    name_text = str(backend_name or "").strip()
                    if not name_text:
                        continue
                    key = name_text.lower()
                    if key in seen_backends:
                        continue
                    seen_backends.add(key)
                    backends.append((name_text, backend))
        except Exception:
            pass

        try:
            from SYS.instance_chooser import filter_backends_to_pipeline_instances

            allowed = {
                str(n).strip().lower()
                for n in filter_backends_to_pipeline_instances([name for name, _backend in backends])
            }
            if allowed:
                backends = [
                    (name, backend)
                    for name, backend in backends
                    if str(name).strip().lower() in allowed
                ]
        except Exception:
            pass

        return backends

    for backend_name, backend in _iter_backends():
        try:
            if not hydrus_available and str(getattr(backend, "STORE_TYPE", "")).strip().lower() == "hydrusnetwork":
                continue
        except Exception:
            pass

        found_exact = False
        try:
            lookup_exact = getattr(backend, "find_hashes_by_url", None)
        except Exception:
            lookup_exact = None
        if callable(lookup_exact):
            try:
                hashes = lookup_exact(canonical_url) or []
            except Exception:
                hashes = []
            if isinstance(hashes, (list, tuple, set)):
                for existing_hash in hashes:
                    normalized_hash = sh.normalize_hash(str(existing_hash) if existing_hash is not None else None)
                    if not normalized_hash:
                        continue
                    found_exact = True
                    _append_ref(
                        backend_name,
                        backend,
                        file_hash_hint=normalized_hash,
                        is_exact=True,
                    )
            if found_exact:
                continue

        try:
            searcher = getattr(backend, "search", None)
        except Exception:
            searcher = None
        if callable(searcher):
            try:
                hits = searcher(f"url:{canonical_url}", limit=5, minimal=True) or []
            except Exception:
                hits = []
            for hit in hits:
                _append_ref(backend_name, backend, item=hit)

    return refs


@classmethod
def _find_existing_url_matches_for_url(
    cls,
    storage: Any,
    canonical_url: str,
    *,
    hydrus_available: bool,
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    refs = cls._collect_existing_url_match_refs_for_url(
        storage,
        canonical_url,
        hydrus_available=hydrus_available,
        config=config,
    )
    if not refs:
        return []

    matches: List[Dict[str, Any]] = []
    exact_hashes_by_backend: Dict[str, Dict[str, Any]] = {}
    prefetched_metadata: Dict[tuple[str, str], Dict[str, Any]] = {}

    for ref in refs:
        if not ref.get("is_exact"):
            continue
        backend_name = str(ref.get("backend_name") or "").strip()
        backend_key = backend_name.lower()
        normalized_hash = sh.normalize_hash(str(ref.get("hash") or ""))
        if not backend_key or not normalized_hash:
            continue
        bucket = exact_hashes_by_backend.setdefault(
            backend_key,
            {
                "backend_name": backend_name,
                "backend": ref.get("backend"),
                "hashes": [],
            },
        )
        if normalized_hash not in bucket["hashes"]:
            bucket["hashes"].append(normalized_hash)

    for backend_key, bucket in exact_hashes_by_backend.items():
        metadata_map = cls._fetch_duplicate_metadata_for_hashes(
            bucket.get("backend"),
            backend_name=str(bucket.get("backend_name") or backend_key),
            file_hashes=list(bucket.get("hashes") or []),
        )
        for normalized_hash, metadata in metadata_map.items():
            prefetched_metadata[(backend_key, normalized_hash)] = metadata

    for ref in refs:
        backend_name = str(ref.get("backend_name") or "").strip()
        backend_key = backend_name.lower()
        normalized_hash = sh.normalize_hash(str(ref.get("hash") or ""))
        if ref.get("is_exact") and normalized_hash:
            candidate = prefetched_metadata.get((backend_key, normalized_hash))
            if candidate is None:
                candidate = cls._fetch_duplicate_metadata_for_hash(
                    ref.get("backend"),
                    backend_name=backend_name,
                    file_hash=normalized_hash,
                )
        else:
            item = ref.get("item")
            candidate = dict(item) if isinstance(item, dict) else {"hash": normalized_hash or "", "store": backend_name}

        if normalized_hash:
            candidate.setdefault("hash", normalized_hash)
        candidate.setdefault("store", backend_name)
        matches.append(
            cls._build_duplicate_display_row(
                candidate,
                backend_name=backend_name,
                original_url=canonical_url,
            )
        )

    return matches


@classmethod
def _find_existing_hash_for_url(
    cls, storage: Any, canonical_url: str, *, hydrus_available: bool
) -> Optional[str]:
    hashes = cls._find_existing_hashes_for_url(
        storage,
        canonical_url,
        hydrus_available=hydrus_available,
        config={},
    )
    return hashes[0] if hashes else None


@classmethod
def _find_existing_hashes_for_url(
    cls,
    storage: Any,
    canonical_url: str,
    *,
    hydrus_available: bool,
    config: Optional[Dict[str, Any]] = None,
) -> List[str]:
    refs = cls._collect_existing_url_match_refs_for_url(
        storage,
        canonical_url,
        hydrus_available=hydrus_available,
        config=config,
    )
    hashes: List[str] = []
    seen_hashes: set[str] = set()
    for ref in refs:
        normalized = sh.normalize_hash(str(ref.get("hash") or ""))
        if not normalized or normalized in seen_hashes:
            continue
        seen_hashes.add(normalized)
        hashes.append(normalized)
    return hashes


def _preflight_explicit_url_duplicates(
    self,
    *,
    raw_urls: Sequence[str],
    config: Dict[str, Any],
) -> tuple[List[str], Optional[int], int]:
    """Return (urls_to_process, early_exit, skipped_count)."""
    urls = [str(u or "").strip() for u in (raw_urls or []) if str(u or "").strip()]
    if not urls:
        return [], None, 0

    if bool(config.get("_skip_url_preflight")):
        return urls, None, 0

    storage, hydrus_available = _init_storage(config)
    duplicate_refs: Dict[str, List[Dict[str, Any]]] = {}
    exact_hashes_by_backend: Dict[str, Dict[str, Any]] = {}
    for url in urls:
        refs = Download_File._collect_existing_url_match_refs_for_url(
            storage,
            url,
            hydrus_available=hydrus_available,
            config=config,
        )
        if not refs:
            continue
        duplicate_refs[url] = refs
        for ref in refs:
            if not ref.get("is_exact"):
                continue
            backend_name = str(ref.get("backend_name") or "").strip()
            backend_key = backend_name.lower()
            normalized_hash = sh.normalize_hash(str(ref.get("hash") or ""))
            if not backend_key or not normalized_hash:
                continue
            bucket = exact_hashes_by_backend.setdefault(
                backend_key,
                {
                    "backend_name": backend_name,
                    "backend": ref.get("backend"),
                    "hashes": [],
                },
            )
            if normalized_hash not in bucket["hashes"]:
                bucket["hashes"].append(normalized_hash)

    if not duplicate_refs:
        return urls, None, 0

    prefetched_metadata: Dict[tuple[str, str], Dict[str, Any]] = {}
    for backend_key, bucket in exact_hashes_by_backend.items():
        metadata_map = Download_File._fetch_duplicate_metadata_for_hashes(
            bucket.get("backend"),
            backend_name=str(bucket.get("backend_name") or backend_key),
            file_hashes=list(bucket.get("hashes") or []),
        )
        for normalized_hash, metadata in metadata_map.items():
            prefetched_metadata[(backend_key, normalized_hash)] = metadata

    duplicates: Dict[str, List[Dict[str, Any]]] = {}
    for url, refs in duplicate_refs.items():
        rows: List[Dict[str, Any]] = []
        for ref in refs:
            backend_name = str(ref.get("backend_name") or "").strip()
            backend_key = backend_name.lower()
            normalized_hash = sh.normalize_hash(str(ref.get("hash") or ""))
            if ref.get("is_exact") and normalized_hash:
                candidate = prefetched_metadata.get((backend_key, normalized_hash))
                if candidate is None:
                    candidate = Download_File._fetch_duplicate_metadata_for_hash(
                        ref.get("backend"),
                        backend_name=backend_name,
                        file_hash=normalized_hash,
                    )
            else:
                item = ref.get("item")
                candidate = dict(item) if isinstance(item, dict) else {"hash": normalized_hash or "", "store": backend_name}

            if normalized_hash:
                candidate.setdefault("hash", normalized_hash)
            candidate.setdefault("store", backend_name)
            rows.append(
                Download_File._build_duplicate_display_row(
                    candidate,
                    backend_name=backend_name,
                    original_url=url,
                )
            )
        if rows:
            duplicates[url] = rows

    duplicate_count = len(duplicates)
    total_count = len(urls)
    try:
        debug_panel(
            "download-file duplicate preflight",
            [
                ("total_urls", total_count),
                ("duplicate_urls", duplicate_count),
            ],
            border_style="yellow",
        )
    except Exception:
        pass

    table = Table(f"Duplicate URLs detected ({duplicate_count}/{total_count})", max_columns=12)
    table._interactive(False)
    duplicate_rows: List[Dict[str, Any]] = []
    for _url, rows in duplicates.items():
        for row in rows:
            payload = dict(row) if isinstance(row, dict) else {}
            duplicate_rows.append(payload)
            table.add_result(payload)

    try:
        pipeline_context.set_last_result_table_overlay(table, duplicate_rows)
    except Exception:
        pass

    try:
        stdin_interactive = bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        stdin_interactive = False

    suspend = getattr(pipeline_context, "suspend_live_progress", None)
    cm: AbstractContextManager[Any] = nullcontext()
    if callable(suspend):
        try:
            maybe_cm = suspend()
            if maybe_cm is not None:
                cm = maybe_cm  # type: ignore[assignment]
        except Exception:
            cm = nullcontext()

    policy = "skip"
    with cm:
        console = get_stderr_console()
        try:
            console.print(table)
        except Exception:
            pass
        setattr(table, "_rendered_by_cmdlet", True)

        if stdin_interactive:
            while True:
                try:
                    raw_policy = Prompt.ask(
                        "Duplicate URLs found. Action? [I]gnore/[S]kip/[C]ancel",
                        default="skip",
                        console=console,
                    )
                except (EOFError, KeyboardInterrupt):
                    policy = "cancel"
                    break

                normalized_policy = Download_File._normalize_duplicate_preflight_policy(raw_policy)
                if normalized_policy is not None:
                    policy = normalized_policy
                    break

                try:
                    console.print("Please select one of: I, S, C, ignore, skip, cancel")
                except Exception:
                    pass
        else:
            policy = "skip"

    if policy == "cancel":
        try:
            pipeline_context.request_pipeline_stop(reason="duplicate-url cancelled", exit_code=0)
        except Exception:
            pass
        return [], 0, 0

    if policy == "ignore":
        return urls, None, 0

    filtered = [u for u in urls if u not in duplicates]
    skipped = len(urls) - len(filtered)
    if skipped:
        debug(
            f"Skipped {skipped} duplicate URL(s); processing remaining {len(filtered)}."
        )
    return filtered, None, skipped


@staticmethod
def _iter_storage_export_refs(
    parsed: Dict[str, Any],
    piped_items: Sequence[Any],
) -> tuple[List[Dict[str, Any]], List[Any], Optional[int]]:
    refs: List[Dict[str, Any]] = []
    residual_items: List[Any] = []

    query_text = str(parsed.get("query") or "").strip()
    query_hash: Optional[str] = None
    if query_text:
        query_hash = sh.parse_single_hash_query(query_text)
        if query_text.lower().startswith("hash") and not query_hash:
            log('Error: -query must be of the form hash:<sha256>', file=sys.stderr)
            return [], list(piped_items or []), 1

    explicit_store = str(parsed.get("instance") or "").strip()
    if query_hash:
        if not explicit_store:
            log('Error: No instance name provided', file=sys.stderr)
            return [], list(piped_items or []), 1
        refs.append(
            {
                "hash": query_hash,
                "store": explicit_store,
                "result": None,
            }
        )

    for item in piped_items or []:
        normalized_hash = sh.normalize_hash(
            str(get_field(item, "hash") or get_field(item, "file_hash") or get_field(item, "hash_hex") or "")
        )
        store_name = str(parsed.get("instance") or get_field(item, "store") or "").strip()
        if store_name.upper() in {"PATH", "URL", "LOCAL"}:
            residual_items.append(item)
            continue
        if normalized_hash and store_name:
            refs.append(
                {
                    "hash": normalized_hash,
                    "store": store_name,
                    "result": item,
                }
            )
        else:
            residual_items.append(item)

    return refs, residual_items, None


def _export_store_file(
    self,
    *,
    file_hash: str,
    store_name: str,
    result: Any,
    parsed: Dict[str, Any],
    config: Dict[str, Any],
    final_output_dir: Path,
) -> int:
    output_path = parsed.get("path")
    explicit_output_requested = bool(output_path)
    output_name = parsed.get("name")
    browser_flag = bool(parsed.get("browser"))

    backend, _store_registry, _exc = sh.get_preferred_store_backend(
        config,
        store_name,
        suppress_debug=True,
    )
    if backend is None:
        log(f"Error: Storage backend '{store_name}' not found", file=sys.stderr)
        return 1

    metadata = backend.get_metadata(file_hash)
    if not metadata:
        log(f"Error: File metadata not found for hash {file_hash}", file=sys.stderr)
        return 1

    try:
        debug_panel(
            "download-file store export",
            [
                ("hash", file_hash),
                ("instance", store_name),
                ("output_path", output_path or "<default>"),
                ("output_name", output_name or "<auto>"),
                ("browser", browser_flag),
            ],
            border_style="blue",
        )
    except Exception:
        pass

    want_url = browser_flag
    source_path = backend.get_file(file_hash, url=want_url)
    download_url = None
    if isinstance(source_path, str):
        if source_path.startswith(("http://", "https://")):
            download_url = source_path
        else:
            source_path = Path(source_path)

    if download_url and (browser_flag or not explicit_output_requested):
        try:
            webbrowser.open(download_url)
        except Exception as exc:
            log(f"Error opening browser: {exc}", file=sys.stderr)
            return 1

        pipeline_context.emit(
            build_file_result_payload(
                title=self._resolve_display_title(result, metadata) or "Opened",
                hash_value=file_hash,
                store=store_name,
                url=download_url,
            )
        )
        return 0

    if download_url is None:
        if not source_path or not Path(source_path).exists():
            log(f"Error: Backend could not retrieve file for hash {file_hash}", file=sys.stderr)
            return 1

    filename = str(output_name or "").strip()
    if not filename:
        title = (metadata.get("title") if isinstance(metadata, dict) else None) or self._resolve_display_title(result, metadata) or "export"
        filename = self._sanitize_export_filename(str(title))

    ext = metadata.get("ext") if isinstance(metadata, dict) else None
    if ext and not filename.endswith(str(ext)):
        ext_text = str(ext)
        if not ext_text.startswith("."):
            ext_text = "." + ext_text
        filename += ext_text

    if download_url:
        result_obj = download_direct_file(
            download_url,
            final_output_dir,
            quiet=True,
            suggested_filename=filename,
            pipeline_progress=config.get("_pipeline_progress") if isinstance(config, dict) else None,
        )
        dest_path = Download_File._path_from_download_result(result_obj)
    else:
        dest_path = self._unique_export_path(final_output_dir / filename)
        shutil.copy2(Path(source_path), dest_path)

    pipeline_context.emit(
        build_file_result_payload(
            title=filename,
            hash_value=file_hash,
            store=store_name,
            path=str(dest_path),
        )
    )
    return 0


def _process_storage_items(
    self,
    *,
    piped_items: Sequence[Any],
    parsed: Dict[str, Any],
    config: Dict[str, Any],
    final_output_dir: Path,
) -> tuple[int, List[Any], Optional[int]]:
    refs, residual_items, early_exit = _iter_storage_export_refs(parsed, piped_items)
    if early_exit is not None:
        return 0, list(residual_items), early_exit
    if not refs:
        return 0, list(residual_items), None

    successes = 0
    for ref in refs:
        exit_code = _export_store_file(
            self,
            file_hash=str(ref.get("hash") or ""),
            store_name=str(ref.get("store") or ""),
            result=ref.get("result"),
            parsed=parsed,
            config=config,
            final_output_dir=final_output_dir,
        )
        if exit_code != 0:
            return successes, list(residual_items), exit_code
        successes += 1

    return successes, list(residual_items), None


def _process_explicit_local_sources(
    self,
    *,
    local_sources: Sequence[str],
    final_output_dir: Path,
    parsed: Dict[str, Any],
    progress: Any,
    config: Dict[str, Any],
) -> int:
    from .download_fetch import _emit_local_file as _emit

    explicit_output_requested = bool(parsed.get("path"))
    downloaded_count = 0
    for raw_source in local_sources or []:
        source_path = Path(str(raw_source or "")).expanduser()
        if not source_path.exists() or not source_path.is_file():
            log(f"File not found: {source_path}", file=sys.stderr)
            continue

        if explicit_output_requested:
            destination = final_output_dir / source_path.name
            destination = self._unique_export_path(destination)
            shutil.copy2(source_path, destination)
            emit_path = destination
        else:
            emit_path = source_path

        _emit(
            self,
            downloaded_path=emit_path,
            source=str(source_path),
            title_hint=emit_path.stem,
            tags_hint=None,
            media_kind_hint="file",
            full_metadata=None,
            progress=progress,
            config=config,
        )
        downloaded_count += 1
    return downloaded_count


# Test-compat wrappers
def _download_supported_urls(self, **kwargs: Any) -> int:
    """Download pre-validated streaming URLs (wrapper used by tests)."""
    urls = list(kwargs.get("supported_url") or [])
    storage = kwargs.get("storage")
    hydrus_available = bool(kwargs.get("hydrus_available"))
    final_output_dir = kwargs.get("final_output_dir")
    skip_preflight = bool(kwargs.get("skip_per_url_preflight"))

    if not urls:
        return 1

    for requested_url in urls:
        canonical = _canonicalize_url_for_storage(requested_url=requested_url)
        if skip_preflight:
            continue
        ok = _preflight_url_duplicate(
            canonical_url=canonical,
            storage=storage,
            hydrus_available=hydrus_available,
            final_output_dir=Path(final_output_dir) if final_output_dir else Path.cwd(),
            config=kwargs.get("config"),
        )
        if not ok:
            continue

    return 0


def _maybe_show_playlist_table(self, **kwargs: Any) -> bool:
    """Compat hook used by tests; playlist table rendering is handled elsewhere."""
    return False


def _maybe_show_format_table_for_single_url(self, **kwargs: Any) -> Optional[int]:
    """Compat hook used by tests; format table rendering is handled elsewhere."""
    return None


def _run_streaming_urls(
    self,
    *,
    streaming_urls: Sequence[str],
    args: Sequence[str],
    config: Dict[str, Any],
    parsed: Dict[str, Any],
) -> int:
    """Compat wrapper for tests that exercise legacy streaming dispatch flow."""
    from PluginCore.registry import plugin_attr

    YtDlpTool = plugin_attr("ytdlp", "YtDlpTool")
    if YtDlpTool is None:
        return 1

    storage, hydrus_available = _init_storage(config)
    supported_url, _unsupported = _filter_supported_urls(streaming_urls)
    if not supported_url:
        return 1

    final_output_dir = resolve_target_dir(parsed, config)
    if final_output_dir is None:
        return 1

    query_text = str(parsed.get("query") or "")
    clip_spec = None
    for token in [t.strip() for t in query_text.split(",") if t.strip()]:
        if token.lower().startswith("clip:"):
            clip_spec = token.split(":", 1)[1].strip()
            break
    clip_ranges = Download_File._parse_clip_spec_to_ranges(clip_spec)

    ytdlp_tool = YtDlpTool(config) if callable(YtDlpTool) else None
    playlist_items = parsed.get("item")

    return _download_supported_urls(
        self,
        supported_url=supported_url,
        ytdlp_tool=ytdlp_tool,
        args=list(args),
        config=config,
        final_output_dir=final_output_dir,
        mode="audio",
        clip_spec=clip_spec,
        clip_ranges=clip_ranges,
        query_hash_override=None,
        embed_chapters=False,
        write_sub=False,
        quiet_mode=bool(config.get("_quiet_background_output")) if isinstance(config, dict) else False,
        playlist_items=playlist_items,
        ytdl_format=(ytdlp_tool.default_format("audio") if ytdlp_tool and hasattr(ytdlp_tool, "default_format") else "best"),
        skip_per_url_preflight=False,
        forced_single_format_id=None,
        forced_single_format_for_batch=False,
        formats_cache={},
        storage=storage,
        hydrus_available=hydrus_available,
        download_timeout_seconds=int(config.get("_pipeobject_timeout_seconds") or 300) if isinstance(config, dict) else 300,
    )
