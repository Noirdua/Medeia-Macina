"""URL existence checks, URL merging/removal helpers, and URL preflight logic.

Contains ``check_url_exists_in_storage`` with all its nested closures that
perform bulk preflight checks across storage backends to determine whether
URLs already exist before downloading.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from SYS.logger import log, debug, debug_panel
from SYS import pipeline as pipeline_context
from SYS.item_accessors import get_field
from SYS.payload_builders import build_table_result_payload
from SYS.result_table import Table
from SYS.rich_display import stderr_console as get_stderr_console
from rich.prompt import Confirm
from contextlib import AbstractContextManager, nullcontext

__all__ = [
    "check_url_exists_in_storage",
    "merge_urls",
    "remove_urls",
    "set_item_urls",
    "register_url_with_local_library",
]


def register_url_with_local_library(
    pipe_obj: Any,
    config: Dict[str, Any],
) -> bool:
    """Register url with a file in the local library database.

    This is called automatically by download cmdlet to ensure url are persisted
    without requiring a separate add-url step in the pipeline.

    Args:
        pipe_obj: PipeObject with path and url
        config: Config dict containing local library path

    Returns:
        True if url were registered, False otherwise
    """
    return False


def merge_urls(existing: Any, incoming: Sequence[Any]) -> list[str]:
    """Merge URL values into a normalized, de-duplicated list."""
    from SYS.metadata import normalize_urls

    merged: list[str] = []
    for value in normalize_urls(existing):
        if value not in merged:
            merged.append(value)
    for value in normalize_urls(list(incoming or [])):
        if value not in merged:
            merged.append(value)
    return merged


def remove_urls(existing: Any, remove: Sequence[Any]) -> list[str]:
    """Remove URL values from an existing URL field and return survivors."""
    from SYS.metadata import normalize_urls

    current = normalize_urls(existing)
    remove_set = {value for value in normalize_urls(list(remove or [])) if value}
    if not remove_set:
        return current
    return [value for value in current if value not in remove_set]


def set_item_urls(item: Any, urls: Sequence[Any]) -> None:
    """Persist normalized URL values back onto a dict/object result item."""
    normalized = merge_urls([], list(urls or []))
    payload: Any = normalized[0] if len(normalized) == 1 else list(normalized)

    try:
        if isinstance(item, dict):
            item["url"] = payload
            return
        if hasattr(item, "url"):
            setattr(item, "url", payload)
    except Exception:
        return


def check_url_exists_in_storage(
    urls: Sequence[str],
    storage: Any,
    hydrus_available: bool,
    final_output_dir: Optional[Path] = None,
    *,
    auto_continue_duplicates: bool = True,
    force_prompt_in_pipeline: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Pre-flight check to see if URLs already exist in storage.

    Args:
        urls: List of URLs to check
        storage: The storage interface
        hydrus_available: Whether Hydrus is available
        final_output_dir: Final output directory (to skip if same as storage)

    Returns:
        True if check passed (user said yes or no dups), False if user said no (stop).
    """
    if storage is None:
        debug("Bulk URL preflight skipped: storage unavailable")
        return True

    try:
        current_cmd_text = pipeline_context.get_current_command_text("")
    except Exception:
        current_cmd_text = ""

    try:
        stage_ctx = pipeline_context.get_stage_context()
    except Exception:
        stage_ctx = None

    in_pipeline = bool(stage_ctx is not None or ("|" in str(current_cmd_text or "")))
    start_time = time.monotonic()
    time_budget = 45.0
    if in_pipeline:
        try:
            already_checked = bool(
                pipeline_context.load_value(
                    "preflight.url_duplicates.checked", default=False
                )
            )
        except Exception:
            already_checked = False

        if already_checked:
            debug("Bulk URL preflight: already checked in pipeline; skipping duplicate check")
            return True

    def _load_preflight_cache() -> Dict[str, Any]:
        try:
            existing = pipeline_context.load_value("preflight", default=None)
        except Exception:
            existing = None
        return existing if isinstance(existing, dict) else {}

    def _store_preflight_cache(cache: Dict[str, Any]) -> None:
        try:
            pipeline_context.store_value("preflight", cache)
        except Exception:
            pass

    def _mark_preflight_checked() -> None:
        if not in_pipeline:
            return
        try:
            pipeline_context.store_value("preflight.url_duplicates.checked", True)
        except Exception:
            pass
        preflight_cache = _load_preflight_cache()
        preflight_cache["url_duplicates_checked"] = True
        url_dup_cache = preflight_cache.get("url_duplicates")
        if not isinstance(url_dup_cache, dict):
            url_dup_cache = {}
        url_dup_cache["checked"] = True
        preflight_cache["url_duplicates"] = url_dup_cache
        _store_preflight_cache(preflight_cache)

    def _timed_out(reason: str) -> bool:
        try:
            if (time.monotonic() - start_time) >= time_budget:
                debug(
                    f"Bulk URL preflight timed out after {time_budget:.0f}s ({reason}); continuing"
                )
                _mark_preflight_checked()
                return True
        except Exception:
            return False
        return False

    if in_pipeline and auto_continue_duplicates:
        try:
            cached_cmd = pipeline_context.load_value("preflight.url_duplicates.command", default="")
            cached_decision = pipeline_context.load_value("preflight.url_duplicates.continue", default=None)
        except Exception:
            cached_cmd = ""
            cached_decision = None

        if (not force_prompt_in_pipeline) and cached_decision is not None and str(cached_cmd or "") == str(current_cmd_text or ""):
            _mark_preflight_checked()
            if bool(cached_decision):
                return True
            try:
                pipeline_context.request_pipeline_stop(reason="duplicate-url declined", exit_code=0)
            except Exception:
                pass
            return False

    unique_urls: List[str] = []
    for u in urls or []:
        s = str(u or "").strip()
        if s and s not in unique_urls:
            unique_urls.append(s)
    if len(unique_urls) == 0:
        return True

    try:
        from SYS.metadata import normalize_urls
    except Exception:
        normalize_urls = None  # type: ignore[assignment]

    def _httpish(value: str) -> bool:
        try:
            return bool(value) and (value.startswith("http://") or value.startswith("https://"))
        except Exception:
            return False

    def _normalize_url_for_search(value: str) -> str:
        url = str(value or "").strip()

        url = url.split("#", 1)[0]

        try:
            parsed = urlparse(url)
        except Exception:
            parsed = None

        if parsed is not None and parsed.query:
            time_keys = {"t", "start", "time_continue", "timestamp", "time", "begin"}
            tracking_prefixes = ("utm_",)
            try:
                pairs = parse_qsl(parsed.query, keep_blank_values=True)
                filtered = []
                for key, val in pairs:
                    key_norm = str(key or "").lower()
                    if key_norm in time_keys:
                        continue
                    if key_norm.startswith(tracking_prefixes):
                        continue
                    filtered.append((key, val))
                if filtered:
                    url = urlunparse(parsed._replace(query=urlencode(filtered, doseq=True)))
                else:
                    url = urlunparse(parsed._replace(query=""))
            except Exception:
                pass

        url = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url, flags=re.IGNORECASE)

        url = re.sub(r"^www\.", "", url, flags=re.IGNORECASE)

        return url.lower()

    def _expand_url_variants(value: str) -> List[str]:
        if not _httpish(value):
            return []

        try:
            parsed = urlparse(value)
        except Exception:
            return []

        if parsed.scheme.lower() not in {"http", "https"}:
            return []

        out: List[str] = []

        def _add_variant(candidate: str) -> None:
            _maybe_add(candidate)
            try:
                lower = str(candidate or "").lower()
            except Exception:
                lower = ""
            if lower and lower != candidate:
                _maybe_add(lower)

            try:
                parsed_candidate = urlparse(candidate)
            except Exception:
                parsed_candidate = None

            if parsed_candidate is None:
                return

            host = (parsed_candidate.hostname or "").strip().lower()
            if host.startswith("www."):
                host = host[4:]
                if host:
                    netloc = host
                    try:
                        if parsed_candidate.port:
                            netloc = f"{netloc}:{parsed_candidate.port}"
                    except Exception:
                        pass
                    try:
                        if parsed_candidate.username or parsed_candidate.password:
                            userinfo = parsed_candidate.username or ""
                            if parsed_candidate.password:
                                userinfo = f"{userinfo}:{parsed_candidate.password}"
                            if userinfo:
                                netloc = f"{userinfo}@{netloc}"
                    except Exception:
                        pass
                    alt = urlunparse(parsed_candidate._replace(netloc=netloc))
                    _maybe_add(alt)
                    try:
                        lower_alt = alt.lower()
                    except Exception:
                        lower_alt = ""
                    if lower_alt and lower_alt != alt:
                        _maybe_add(lower_alt)

        def _maybe_add(candidate: str) -> None:
            if not candidate or candidate == value:
                return
            if candidate not in out:
                out.append(candidate)

        if parsed.fragment:
            _add_variant(urlunparse(parsed._replace(fragment="")))

        time_keys = {"t", "start", "time_continue", "timestamp", "time", "begin"}
        tracking_prefixes = ("utm_",)

        try:
            query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        except Exception:
            query_pairs = []

        if query_pairs or parsed.fragment:
            filtered_pairs = []
            removed = False
            for key, val in query_pairs:
                key_norm = str(key or "").lower()
                if key_norm in time_keys:
                    removed = True
                    continue
                if key_norm.startswith(tracking_prefixes):
                    removed = True
                    continue
                filtered_pairs.append((key, val))

            if removed:
                new_query = urlencode(filtered_pairs, doseq=True) if filtered_pairs else ""
                _add_variant(urlunparse(parsed._replace(query=new_query, fragment="")))

        return out

    def _dedupe_needles(raw_needles: Sequence[str]) -> List[str]:
        output: List[str] = []
        seen: set[str] = set()
        for candidate in (raw_needles or []):
            candidate_text = str(candidate or "").strip()
            if not candidate_text:
                continue
            key = candidate_text.lower()
            if key in seen:
                continue
            seen.add(key)
            output.append(candidate_text)
        return output

    url_needles: Dict[str, List[str]] = {}
    for u in unique_urls:
        needles: List[str] = []
        if normalize_urls is not None:
            try:
                needles.extend([n for n in (normalize_urls(u) or []) if isinstance(n, str)])
            except Exception:
                needles = []
        if not needles:
            needles = [u]
        filtered: List[str] = []
        for n in needles:
            n2 = str(n or "").strip()
            if not n2:
                continue
            if not _httpish(n2):
                continue
            if n2 not in filtered:
                filtered.append(n2)
        lowered: List[str] = []
        for n2 in filtered:
            try:
                lower = n2.lower()
            except Exception:
                lower = ""
            if lower and lower != n2 and lower not in filtered and lower not in lowered:
                lowered.append(lower)
        normalized: List[str] = []
        for n2 in filtered:
            norm = _normalize_url_for_search(n2)
            if norm and norm not in normalized and norm not in filtered:
                normalized.append(norm)
        expanded: List[str] = []
        for n2 in filtered:
            for extra in _expand_url_variants(n2):
                if extra not in expanded and extra not in filtered and extra not in lowered:
                    expanded.append(extra)
                norm_extra = _normalize_url_for_search(extra)
                if (
                    norm_extra
                    and norm_extra not in normalized
                    and norm_extra not in filtered
                    and norm_extra not in expanded
                    and norm_extra not in lowered
                ):
                    normalized.append(norm_extra)

        combined = filtered + expanded + lowered + normalized
        deduped = _dedupe_needles(combined)
        url_needles[u] = deduped if deduped else [u]

    if in_pipeline:
        preflight_cache = _load_preflight_cache()
        url_dup_cache = preflight_cache.get("url_duplicates")
        if not isinstance(url_dup_cache, dict):
            url_dup_cache = {}
        cached_urls = url_dup_cache.get("urls")
        cached_set = {str(u) for u in cached_urls} if isinstance(cached_urls, list) else set()

        if cached_set:
            all_cached = True
            for original_url, needles in url_needles.items():
                original_cached = str(original_url or "") in cached_set
                needles_cached = True
                if original_cached:
                    for needle in (needles or []):
                        needle_text = str(needle or "")
                        if not needle_text:
                            continue
                        if needle_text not in cached_set:
                            needles_cached = False
                            break
                else:
                    needles_cached = False

                if original_cached and needles_cached:
                    continue

                all_cached = False
                break

            if all_cached:
                debug("Bulk URL preflight: cached for pipeline; skipping duplicate check")
                _mark_preflight_checked()
                return True

    if _timed_out("before backend scan"):
        return True

    bulk_mode = len(unique_urls) > 3

    def _build_bulk_patterns(needles_map: Dict[str, List[str]], max_per_url: int = 3, max_total: int = 240) -> List[str]:
        patterns: List[str] = []
        for _original, needles in needles_map.items():
            for needle in (needles or [])[:max_per_url]:
                needle_text = str(needle or "").strip()
                if not needle_text:
                    continue
                if needle_text not in patterns:
                    patterns.append(needle_text)
                if len(patterns) >= max_total:
                    return patterns
        return patterns

    bulk_patterns = _build_bulk_patterns(url_needles)

    def _match_normalized_url(pattern_text: str, candidate_url: str) -> bool:
        pattern_norm = _normalize_url_for_search(pattern_text)
        candidate_norm = _normalize_url_for_search(candidate_url)
        if not pattern_norm or not candidate_norm:
            return False
        if pattern_norm == candidate_norm:
            return True
        return pattern_norm in candidate_norm

    def _extract_urls_from_hit(
        hit: Any,
        backend: Any,
        *,
        allow_backend_lookup: bool = True,
    ) -> List[str]:
        url_values: List[str] = []
        try:
            raw_urls = get_field(hit, "known_urls") or get_field(hit, "urls") or get_field(hit, "url")
            if isinstance(raw_urls, str) and raw_urls.strip():
                url_values.append(raw_urls.strip())
            elif isinstance(raw_urls, (list, tuple, set)):
                for item in raw_urls:
                    if isinstance(item, str) and item.strip():
                        url_values.append(item.strip())
        except Exception:
            url_values = []

        if url_values or not allow_backend_lookup:
            return url_values

        try:
            file_hash = get_field(hit, "hash") or get_field(hit, "file_hash") or get_field(hit, "sha256") or ""
        except Exception:
            file_hash = ""

        if file_hash:
            try:
                fetched = backend.get_url(str(file_hash))
                if isinstance(fetched, str) and fetched.strip():
                    url_values.append(fetched.strip())
                elif isinstance(fetched, (list, tuple, set)):
                    for item in fetched:
                        if isinstance(item, str) and item.strip():
                            url_values.append(item.strip())
            except Exception:
                pass

        return url_values

    def _build_display_row_for_hit(
        hit: Any,
        backend_name: str,
        original_url: str,
    ) -> Dict[str, Any]:
        try:
            from SYS.result_table import build_display_row
            extracted = build_display_row(hit, keys=["title", "store", "hash", "ext", "size"])
        except Exception:
            extracted = {}

        try:
            title = extracted.get("title") or get_field(hit, "title") or get_field(hit, "name") or get_field(hit, "target") or get_field(hit, "path") or "(exists)"
        except Exception:
            title = "(exists)"

        try:
            file_hash = extracted.get("hash") or get_field(hit, "hash") or get_field(hit, "file_hash") or get_field(hit, "sha256") or ""
        except Exception:
            file_hash = ""

        ext = extracted.get("ext") if isinstance(extracted, dict) else ""
        size_val = extracted.get("size") if isinstance(extracted, dict) else None

        return build_table_result_payload(
            title=str(title),
            columns=[
                ("Title", str(title)),
                ("Store", str(get_field(hit, "store") or backend_name)),
                ("Hash", str(file_hash or "")),
                ("Ext", str(ext or "")),
                ("Size", size_val),
                ("URL", original_url),
            ],
            store=str(get_field(hit, "store") or backend_name),
            hash=str(file_hash or ""),
            ext=str(ext or ""),
            size=size_val,
            url=original_url,
        )

    def _search_backend_url_hits(
        backend: Any,
        backend_name: str,
        original_url: str,
        needles: Sequence[str],
    ) -> Optional[Dict[str, Any]]:
        backend_hits: List[Dict[str, Any]] = []

        for needle in (needles or [])[:5]:
            needle_stripped = str(needle or "").strip()
            if not needle_stripped or not _httpish(needle_stripped):
                continue
            try:
                query = f"url:{needle_stripped}"
                backend_hits = backend.search(query, limit=1, minimal=True) or []
                if backend_hits:
                    return _build_display_row_for_hit(backend_hits[0], backend_name, original_url)
            except Exception:
                continue

        for needle in (needles or [])[:3]:
            needle_text = str(needle or "").strip()
            if not needle_text:
                continue
            search_needle = _normalize_url_for_search(needle_text) or needle_text
            query = f"url:*{search_needle}*"
            try:
                backend_hits = backend.search(query, limit=1, minimal=True) or []
                if backend_hits:
                    break
            except Exception:
                continue

        if not backend_hits:
            return None

        hit = backend_hits[0]
        return _build_display_row_for_hit(hit, backend_name, original_url)

    backend_names: List[str] = []
    try:
        backend_names_all = storage.list_searchable_backends()
    except Exception:
        backend_names_all = []

    for backend_name in backend_names_all:
        try:
            backend = storage[backend_name]
        except Exception:
            continue

        try:
            if str(backend_name).strip().lower() == "temp":
                continue
        except Exception:
            pass

        try:
            backend_location = getattr(backend, "_location", None)
            if backend_location and final_output_dir:
                backend_path = Path(str(backend_location)).expanduser().resolve()
                temp_path = Path(str(final_output_dir)).expanduser().resolve()
                if backend_path == temp_path:
                    continue
        except Exception:
            pass

        backend_names.append(backend_name)

    try:
        from SYS.instance_chooser import filter_backends_to_pipeline_instances

        backend_names = filter_backends_to_pipeline_instances(backend_names)
    except Exception:
        pass

    if not backend_names:
        debug("Bulk URL preflight skipped: no searchable backends")
        return True

    try:
        debug_panel(
            "URL preflight",
            [
                ("url_count", len(unique_urls)),
                ("pipeline", in_pipeline),
                ("bulk_mode", bulk_mode),
                ("backends", ", ".join(str(name) for name in backend_names)),
            ],
            border_style="yellow",
        )
    except Exception:
        pass

    seen_pairs: set[tuple[str, str]] = set()
    matched_urls: set[str] = set()
    match_rows: List[Dict[str, Any]] = []
    max_rows = 200

    hydrus_provider = None
    try:
        from PluginCore.registry import get_plugin

        hydrus_provider = get_plugin("hydrusnetwork", config or {})
    except Exception:
        hydrus_provider = None

    for backend_name in backend_names:
        if _timed_out("backend scan"):
            return True
        if len(match_rows) >= max_rows:
            break
        try:
            backend = storage[backend_name]
        except Exception:
            continue

        is_hydrus_backend = False
        try:
            is_hydrus_backend = bool(hydrus_provider and hydrus_provider.is_backend(backend, str(backend_name)))
        except Exception:
            is_hydrus_backend = False
        if not is_hydrus_backend:
            try:
                is_hydrus_backend = str(getattr(backend, "STORE_TYPE", "")).strip().lower() == "hydrusnetwork"
            except Exception:
                is_hydrus_backend = False

        if is_hydrus_backend:
            if not hydrus_available:
                debug("Bulk URL preflight: global Hydrus availability check failed; attempting per-backend best-effort lookup")

            if _timed_out("hydrus scan"):
                return True

            for original_url, needles in url_needles.items():
                if _timed_out("hydrus per-url scan"):
                    return True
                if len(match_rows) >= max_rows:
                    break
                if (original_url, str(backend_name)) in seen_pairs:
                    continue

                found_hash: Optional[str] = None
                found = False
                try:
                    lookup_exact = getattr(backend, "find_hashes_by_url", None)
                except Exception:
                    lookup_exact = None
                if callable(lookup_exact):
                    for needle in [original_url, *(needles or [])][:7]:
                        needle_text = str(needle or "").strip()
                        if not _httpish(needle_text):
                            continue
                        try:
                            exact_hashes = lookup_exact(needle_text) or []
                        except Exception:
                            continue
                        if not isinstance(exact_hashes, list) or not exact_hashes:
                            continue
                        try:
                            found_hash = str(exact_hashes[0] or "").strip().lower()
                        except Exception:
                            found_hash = None
                        found = True
                        break

                if not found:
                    continue

                seen_pairs.add((original_url, str(backend_name)))
                matched_urls.add(original_url)
                display_row = build_table_result_payload(
                    title="(exists)",
                    columns=[
                        ("Title", "(exists)"),
                        ("Store", str(backend_name)),
                        ("Hash", found_hash or ""),
                        ("URL", original_url),
                    ],
                    store=str(backend_name),
                    hash=found_hash or "",
                    url=original_url,
                )
                match_rows.append(display_row)
            continue

        if bulk_mode and bulk_patterns:
            bulk_hits: Optional[List[Any]] = None
            bulk_limit = min(2000, max(200, len(unique_urls) * 8))
            try:
                bulk_hits = backend.search(
                    "url:*",
                    limit=bulk_limit,
                    pattern_hint=bulk_patterns,
                ) or []
            except Exception:
                try:
                    bulk_hits = backend.search("url:*", limit=bulk_limit) or []
                except Exception:
                    bulk_hits = None

            if bulk_hits is not None:
                for hit in bulk_hits:
                    if _timed_out("backend bulk scan"):
                        return True
                    if len(match_rows) >= max_rows:
                        break
                    url_values = _extract_urls_from_hit(hit, backend, allow_backend_lookup=False)
                    if not url_values:
                        continue

                    for original_url, needles in url_needles.items():
                        if _timed_out("backend bulk scan"):
                            return True
                        if len(match_rows) >= max_rows:
                            break
                        if (original_url, str(backend_name)) in seen_pairs:
                            continue

                        matched = False
                        for url_value in url_values:
                            for needle in (needles or []):
                                if _match_normalized_url(str(needle or ""), str(url_value or "")):
                                    matched = True
                                    break
                            if matched:
                                break

                        if not matched:
                            continue

                        seen_pairs.add((original_url, str(backend_name)))
                        matched_urls.add(original_url)
                        match_rows.append(
                            _build_display_row_for_hit(hit, str(backend_name), original_url)
                        )
                continue

        for original_url, needles in url_needles.items():
            if _timed_out("backend per-url scan"):
                return True
            if len(match_rows) >= max_rows:
                break
            if (original_url, str(backend_name)) in seen_pairs:
                continue

            display_row = _search_backend_url_hits(backend, str(backend_name), original_url, needles)
            if not display_row:
                continue

            seen_pairs.add((original_url, str(backend_name)))
            matched_urls.add(original_url)
            match_rows.append(display_row)

    if not match_rows:
        if in_pipeline:
            preflight_cache = _load_preflight_cache()
            url_dup_cache = preflight_cache.get("url_duplicates")
            if not isinstance(url_dup_cache, dict):
                url_dup_cache = {}

            cached_urls = url_dup_cache.get("urls")
            cached_set = {str(u) for u in cached_urls} if isinstance(cached_urls, list) else set()

            for original_url, needles in url_needles.items():
                cached_set.add(original_url)
                for needle in needles or []:
                    cached_set.add(str(needle))

            url_dup_cache["urls"] = sorted(cached_set)
            preflight_cache["url_duplicates"] = url_dup_cache
            _store_preflight_cache(preflight_cache)
            _mark_preflight_checked()
        return True

    table = Table(f"URL already exists ({len(matched_urls)} url(s))", max_columns=10)
    table._interactive(True)
    try:
        table._perseverance(True)
    except Exception:
        pass

    for row in match_rows:
        table.add_result(row)

    try:
        pipeline_context.set_last_result_table_overlay(table, match_rows)
    except Exception:
        pass

    suspend = getattr(pipeline_context, "suspend_live_progress", None)
    cm: AbstractContextManager[Any] = nullcontext()
    if callable(suspend):
        try:
            maybe_cm = suspend()
            if maybe_cm is not None:
                cm = maybe_cm  # type: ignore[assignment]
        except Exception:
            cm = nullcontext()

    auto_confirm_reason: Optional[str] = None
    if in_pipeline and stage_ctx is not None:
        try:
            total_stages = int(getattr(stage_ctx, "total_stages", 0))
        except Exception:
            total_stages = 0
        try:
            is_last_stage = bool(getattr(stage_ctx, "is_last_stage", False))
        except Exception:
            is_last_stage = False
        if total_stages > 1 and not is_last_stage and not force_prompt_in_pipeline:
            auto_confirm_reason = "pipeline stage (pre-last)"
    if auto_confirm_reason is None:
        try:
            stdin_interactive = bool(sys.stdin and sys.stdin.isatty())
        except Exception:
            stdin_interactive = False
        if not stdin_interactive:
            auto_confirm_reason = "non-interactive stdin"

    answered_yes = True
    auto_declined = False
    with cm:
        get_stderr_console().print(table)
        setattr(table, "_rendered_by_cmdlet", True)
        if auto_confirm_reason is None:
            answered_yes = bool(Confirm.ask("Continue?", default=False, console=get_stderr_console()))
        else:
            answered_yes = bool(auto_continue_duplicates)
            auto_declined = not answered_yes
            if answered_yes:
                debug(
                    f"Bulk URL preflight auto-confirmed duplicates ({auto_confirm_reason}); continuing without user input."
                )
                try:
                    log(
                        f"Auto-confirmed duplicate URL warning ({auto_confirm_reason}). Continuing...",
                        file=sys.stderr,
                    )
                except Exception:
                    pass
            else:
                debug(
                    f"Bulk URL preflight auto-skipped duplicates ({auto_confirm_reason}); skipping without user input."
                )
                try:
                    log(
                        f"Duplicate URL detected ({auto_confirm_reason}). Skipping download.",
                        file=sys.stderr,
                    )
                except Exception:
                    pass

        if in_pipeline and auto_continue_duplicates:
            try:
                existing = pipeline_context.load_value("preflight", default=None)
            except Exception:
                existing = None
            preflight_cache: Dict[str, Any] = existing if isinstance(existing, dict) else {}
            url_dup_cache = preflight_cache.get("url_duplicates")
            if not isinstance(url_dup_cache, dict):
                url_dup_cache = {}
            url_dup_cache["command"] = str(current_cmd_text or "")
            url_dup_cache["continue"] = bool(answered_yes)
            cached_urls = url_dup_cache.get("urls")
            cached_set = {str(u) for u in cached_urls} if isinstance(cached_urls, list) else set()
            for original_url, needles in url_needles.items():
                cached_set.add(original_url)
                for needle in needles or []:
                    cached_set.add(str(needle))
            url_dup_cache["urls"] = sorted(cached_set)
            preflight_cache["url_duplicates"] = url_dup_cache
            try:
                pipeline_context.store_value("preflight", preflight_cache)
            except Exception:
                pass

        if not answered_yes:
            if in_pipeline and not auto_declined:
                 try:
                    pipeline_context.request_pipeline_stop(reason="duplicate-url declined", exit_code=0)
                 except Exception:
                    pass
            _mark_preflight_checked()
            return False
        _mark_preflight_checked()
    return True
