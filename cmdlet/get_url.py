from __future__ import annotations

from queue import SimpleQueue
from threading import Thread
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Optional, Set, Tuple
import sys
import re
from fnmatch import fnmatch
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from ._shared import (
    Cmdlet,
    SharedArgs,
    parse_cmdlet_args,
    get_field,
    normalize_hash,
)
from . import _shared as sh
from SYS.detail_view_helpers import create_detail_view, prepare_detail_metadata
from SYS.item_accessors import get_extension_field, get_int_field, get_result_title
from SYS.logger import log
from SYS.payload_builders import build_file_result_payload
from SYS.result_publication import publish_result_table
from SYS.result_table import Table
from PluginCore.backend_registry import BackendRegistry
from SYS import pipeline as ctx


@dataclass
class UrlItem:
    url: str
    hash: str
    instance: str
    title: str = ""
    size: int | None = None
    ext: str = ""


class Get_Url(Cmdlet):
    """Get url associated with files via hash+store, or search urls by pattern."""

    STORE_SEARCH_TIMEOUT_SECONDS = 6.0

    def __init__(self) -> None:
        super().__init__(
            name="get-url",
            summary="List url associated with a file, or search urls by pattern",
            usage='@1 | get-url  OR  get-url -url "https://www.youtube.com/watch?v=xx"',
            arg=[SharedArgs.QUERY,
                 SharedArgs.INSTANCE,
                 SharedArgs.URL],
            detail=[
                "- Get url for file: @1 | get-url (requires hash+instance from result)",
                '- Search url across instances: get-url -url "www.google.com" (strips protocol & www prefix)',
                '- Wildcard matching: get-url -url "youtube.com*" (matches all youtube.com urls)',
                "- Pattern matching: domain matching ignores protocol (https://, http://, ftp://)",
            ],
            exec=self.run,
        )
        self.register()

    @staticmethod
    def _normalize_url_for_search(url: str) -> str:
        """Strip protocol and www prefix from URL for searching.

        Examples:
            https://www.youtube.com/watch?v=xx -> youtube.com/watch?v=xx
            http://www.google.com -> google.com
            ftp://files.example.com -> files.example.com
        """
        url = str(url or "").strip()

        # Strip fragment (e.g., #t=10) before matching
        url = url.split("#", 1)[0]

        # Strip common time/tracking query params for matching
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

        # Remove protocol (http://, https://, ftp://, etc.)
        url = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url, flags=re.IGNORECASE)

        # Remove www. prefix (case-insensitive)
        url = re.sub(r"^www\.", "", url, flags=re.IGNORECASE)

        return url.lower()

    @staticmethod
    def _looks_like_url_pattern(value: str) -> bool:
        v = str(value or "").strip().lower()
        if not v:
            return False
        if "://" in v:
            return True
        if v.startswith(("magnet:", "torrent:", "ytdl:", "tidal:", "ftp:", "sftp:", "file:")):
            return True
        return "." in v and "/" in v

    @staticmethod
    def _match_url_pattern(url: str, pattern: str) -> bool:
        """Match URL against pattern with wildcard support.

        Strips protocol/www from both URL and pattern before matching.
        Supports * and ? wildcards.
        """
        raw_pattern = str(pattern or "").strip()
        normalized_url = Get_Url._normalize_url_for_search(url)
        normalized_pattern = Get_Url._normalize_url_for_search(raw_pattern)

        looks_like_url = Get_Url._looks_like_url_pattern(raw_pattern)
        has_wildcards = "*" in normalized_pattern or (
            not looks_like_url and "?" in normalized_pattern
        )
        if has_wildcards:
            return fnmatch(normalized_url, normalized_pattern)

        normalized_url_no_slash = normalized_url.rstrip("/")
        normalized_pattern_no_slash = normalized_pattern.rstrip("/")
        if normalized_pattern_no_slash and normalized_pattern_no_slash == normalized_url_no_slash:
            return True

        return normalized_pattern in normalized_url

    def _execute_search_with_timeout(
        self,
        backend: Any,
        query: str,
        limit: int,
        store_name: str,
        **kwargs: Any,
    ) -> Optional[List[Any]]:
        queue: SimpleQueue[tuple[str, Any]] = SimpleQueue()

        def _worker() -> None:
            try:
                queue.put(("ok", backend.search(query, limit=limit, **kwargs)))
            except Exception as exc:
                queue.put(("err", exc))

        worker = Thread(target=_worker, daemon=True)
        worker.start()
        worker.join(timeout=self.STORE_SEARCH_TIMEOUT_SECONDS)

        if worker.is_alive():
            debug(
                f"Store '{store_name}' search timed out after {self.STORE_SEARCH_TIMEOUT_SECONDS}s",
                file=sys.stderr,
            )
            return None

        if queue.empty():
            return []

        status, payload = queue.get()
        if status == "err":
            debug(
                f"Store '{store_name}' search failed: {payload}",
                file=sys.stderr,
            )
            return []

        return payload or []

    @staticmethod
    def _extract_first_url(value: Any) -> Optional[str]:
        if isinstance(value, str):
            v = value.strip()
            return v or None
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
        return None

    @staticmethod
    def _extract_urls_from_hit(hit: Any) -> List[str]:
        """Extract candidate URLs directly from a search hit, if present."""
        raw = None
        try:
            raw = get_field(hit, "known_urls")
            if not raw:
                raw = get_field(hit, "urls")
            if not raw:
                raw = get_field(hit, "url")
            if not raw:
                raw = get_field(hit, "source_url") or get_field(hit, "source_urls")
        except Exception:
            raw = None

        if isinstance(raw, str):
            val = raw.strip()
            return [val] if val else []
        if isinstance(raw, (list, tuple)):
            out: list[str] = []
            for item in raw:
                if not isinstance(item, str):
                    continue
                v = item.strip()
                if v:
                    out.append(v)
            return out
        return []

    @staticmethod
    def _extract_title_from_result(result: Any) -> Optional[str]:
        return get_result_title(result, "title", "name", "filename")

    @staticmethod
    def _extract_size_from_hit(hit: Any) -> int | None:
        return get_int_field(hit, "size", "file_size", "filesize", "size_bytes")

    @staticmethod
    def _extract_ext_from_hit(hit: Any) -> str:
        return get_extension_field(hit, "ext", "extension")

    def _search_urls_across_stores(self,
                                   pattern: str,
                                   config: Dict[str,
                                                Any]) -> Tuple[List[UrlItem],
                                                               List[str]]:
        """Search for URLs matching pattern across all stores.

        Returns:
            Tuple of (matching_items, found_stores)
        """
        items: List[UrlItem] = []
        found_stores: Set[str] = set()
        MAX_RESULTS = 256

        try:
            storage = BackendRegistry(config)
            store_names = storage.list_backends() if hasattr(storage,
                                                             "list_backends") else []

            if not store_names:
                log("Error: No stores configured", file=sys.stderr)
                return items, list(found_stores)

            for store_name in store_names:
                if len(items) >= MAX_RESULTS:
                    break
                try:
                    backend = storage[store_name]

                    # Search only URL-bearing records using the backend's URL search capability.
                    # This avoids the expensive/incorrect "search('*')" scan.
                    try:
                        raw_pattern = str(pattern or "").strip()
                        looks_like_url = self._looks_like_url_pattern(raw_pattern)
                        has_wildcards = "*" in raw_pattern or (
                            not looks_like_url and "?" in raw_pattern
                        )

                        # If this is a Hydrus backend and the pattern is a single URL,
                        # normalize it through the official API. Skip for bare domains.
                        normalized_url = None
                        normalized_search_pattern = None
                        if not has_wildcards and looks_like_url:
                            normalized_search_pattern = self._normalize_url_for_search(
                                raw_pattern
                            )
                            if (
                                normalized_search_pattern
                                and normalized_search_pattern != raw_pattern
                            ):
                                debug(
                                    "get-url normalized raw pattern: %s -> %s",
                                    raw_pattern,
                                    normalized_search_pattern,
                                )
                            if hasattr(backend, "get_url_info"):
                                try:
                                    info = backend.get_url_info(raw_pattern)  # type: ignore[attr-defined]
                                    if isinstance(info, dict):
                                        norm = (
                                            info.get("normalized_url")
                                            or info.get("normalized_url")
                                        )
                                        if isinstance(norm, str) and norm.strip():
                                            normalized_url = self._normalize_url_for_search(
                                                norm.strip()
                                            )
                                except Exception:
                                    pass
                            if (
                                normalized_url
                                and normalized_url != normalized_search_pattern
                                and normalized_url != raw_pattern
                            ):
                                debug(
                                    "get-url normalized backend result: %s -> %s",
                                    raw_pattern,
                                    normalized_url,
                                )

                        target_pattern = (
                            normalized_url
                            or normalized_search_pattern
                            or raw_pattern
                        )
                        if has_wildcards or not target_pattern:
                            search_query = "url:*"
                        else:
                            wrapped_pattern = f"*{target_pattern}*"
                            search_query = f"url:{wrapped_pattern}"
                        search_limit = max(1, min(MAX_RESULTS, 1000))
                        search_results = self._execute_search_with_timeout(
                            backend,
                            search_query,
                            search_limit,
                            store_name,
                            pattern_hint=target_pattern,
                            minimal=True,
                            url_only=True,
                        )
                        if search_results is None:
                            continue

                        search_results = search_results or []

                        for hit in (search_results or []):
                            if len(items) >= MAX_RESULTS:
                                break
                            file_hash = None
                            if isinstance(hit, dict):
                                file_hash = hit.get("hash") or hit.get("file_hash")
                            if not file_hash:
                                continue

                            file_hash = str(file_hash)

                            title = self._extract_title_from_result(hit) or ""
                            size = self._extract_size_from_hit(hit)
                            ext = self._extract_ext_from_hit(hit)

                            urls = self._extract_urls_from_hit(hit)
                            if not urls:
                                try:
                                    urls = backend.get_url(file_hash)
                                except Exception:
                                    urls = []

                            hit_added = False
                            for url in (urls or []):
                                if len(items) >= MAX_RESULTS:
                                    break
                                if not self._match_url_pattern(str(url), raw_pattern):
                                    continue

                                from SYS.metadata import normalize_urls
                                valid = normalize_urls([str(url)])
                                if not valid:
                                    continue

                                items.append(
                                    UrlItem(
                                        url=str(url),
                                        hash=str(file_hash),
                                        store=str(store_name),
                                        title=str(title or ""),
                                        size=size,
                                        ext=str(ext or ""),
                                    )
                                )
                                hit_added = True
                            if hit_added:
                                found_stores.add(str(store_name))
                            if len(items) >= MAX_RESULTS:
                                break
                    except Exception as exc:
                        debug(
                            f"Error searching store '{store_name}': {exc}",
                            file=sys.stderr
                        )
                        continue

                except KeyError:
                    continue
                except Exception as exc:
                    debug(
                        f"Error searching store '{store_name}': {exc}",
                        file=sys.stderr
                    )
                    continue

            return items, list(found_stores)

        except Exception as exc:
            log(f"Error searching stores: {exc}", file=sys.stderr)
            return items, []

    def run(self, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        """Get url for file via hash+store, or search urls by pattern."""
        parsed = parse_cmdlet_args(args, self)

        # Check if user provided a URL pattern to search for
        search_pattern = parsed.get("url")

        # Support positional URL search or "url:" query prefix
        if not search_pattern:
            query = parsed.get("query")
            if query:
                if str(query).lower().startswith("url:"):
                    search_pattern = query[4:].strip()
                elif self._looks_like_url_pattern(query) or (
                    "." in str(query) and len(str(query)) < 64
                ):
                    # If it looks like a domain or URL, and isn't a long hash,
                    # treat a positional query as a search pattern.
                    search_pattern = query

        if search_pattern:
            # URL search mode: find all files with matching URLs across stores
            items, stores_searched = self._search_urls_across_stores(search_pattern, config)

            if not items:
                log(f"No urls matching pattern: {search_pattern}", file=sys.stderr)
                return 1

            # NOTE: The CLI can auto-render tables from emitted items. When emitting
            # dataclass objects, the generic-object renderer will include `hash` as a
            # visible column. To keep HASH available for chaining but hidden from the
            # table, emit dicts (dict rendering hides `hash`) and provide an explicit
            # `columns` list to force display order and size formatting.
            display_items: List[Dict[str, Any]] = []

            table = (
                Table(
                    "url",
                    max_columns=5
                )._perseverance(True).set_table("url").set_value_case("preserve")
            )
            table.set_source_command("get-url", ["-url", search_pattern])

            for item in items:
                payload = build_file_result_payload(
                    title=item.title,
                    hash_value=item.hash,
                    store=item.store,
                    url=item.url,
                    ext=item.ext,
                    columns=[
                        ("Title", item.title),
                        ("Url", item.url),
                        ("Size", item.size),
                        ("Ext", item.ext),
                        ("Store", item.store),
                    ],
                    size=item.size,
                )
                display_items.append(payload)
                table.add_result(payload)

            publish_result_table(ctx, table if display_items else None, display_items, subject=result)
            
            # Emit after table state is finalized
            for d in display_items:
                ctx.emit(d)

            log(
                f"Found {len(items)} matching url(s) in {len(stores_searched)} store(s)"
            )
            return 0

        # Original mode: Get URLs for a specific file by hash+store
        query_hash, query_valid = sh.require_single_hash_query(
            parsed.get("query"),
            "Error: -query must be of the form hash:<sha256>",
        )
        if not query_valid:
            return 1

        # Extract hash and store from result or args
        file_hash = query_hash or get_field(result, "hash")
        store_name = parsed.get("instance") or get_field(result, "store")

        if not file_hash:
            log(
                'Error: No file hash provided (pipe an item or use -query "hash:<sha256>")'
            )
            return 1

        if not store_name:
            log("Error: No instance name provided")
            return 1

        # Get backend and retrieve url
        try:
            storage = BackendRegistry(config)
            backend = storage[store_name]

            urls = backend.get_url(file_hash)
            
            # Filter URLs to avoid data leakage from dirty DBs
            from SYS.metadata import normalize_urls
            urls = normalize_urls(urls)

            # Prepare metadata for the detail view
            metadata = prepare_detail_metadata(result)
            tag_values = None
            
            # Enrich the metadata with tags if missing
            if not metadata.get("Tags"):
                try:
                    item_tags = get_field(result, "tag") or get_field(result, "tags") or []
                    row_tags = []
                    if isinstance(item_tags, list):
                        row_tags.extend([str(t) for t in item_tags])
                    elif isinstance(item_tags, str):
                        row_tags.append(item_tags)
                    
                    # Also collect from backend
                    if file_hash and store_name:
                        try:
                            # Re-use existing backend variable
                            if backend and hasattr(backend, "get_tag"):
                                b_tags, _ = backend.get_tag(file_hash)
                                if b_tags:
                                    row_tags.extend([str(t) for t in b_tags])
                        except Exception:
                            pass
                    
                    if row_tags:
                        tag_values = sorted(list(set(row_tags)))
                except Exception:
                    pass

            metadata = prepare_detail_metadata(
                result,
                hash_value=file_hash,
                store=store_name,
                tags=tag_values,
            )

            table = create_detail_view(
                "Urls",
                metadata,
                max_columns=1,
                table_name="url",
                source_command=("get-url", []),
            )

            items: List[UrlItem] = []
            for u in list(urls or []):
                u = str(u or "").strip()
                if not u:
                    continue
                row = table.add_row()
                row.add_column("Url", u)
                item = UrlItem(url=u, hash=file_hash, store=str(store_name))
                items.append(item)

            # Use overlay mode to avoid "merging" with the previous status/table state.
            # This is idiomatic for detail views and prevents the search table from being
            # contaminated by partial re-renders.
            publish_result_table(ctx, table, items, subject=result, overlay=True)

            # Emit items at the end for pipeline continuity
            for item in items:
                ctx.emit(item)

            if not items:
                # Still log it but the panel will show the item context
                log("No url found", file=sys.stderr)

            return 0

        except KeyError:
            log(f"Error: Storage backend '{store_name}' not configured")
            return 1
        except Exception as exc:
            log(f"Error retrieving url: {exc}", file=sys.stderr)
            return 1


# Import debug function from logger if available
try:
    from SYS.logger import debug
except ImportError:

    def debug(*args, **kwargs):
        pass  # Fallback no-op


CMDLET = Get_Url()
