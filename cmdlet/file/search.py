"""search-file cmdlet: Search plugin instances and search-capable plugins."""

from __future__ import annotations

from typing import Any, Dict, Sequence, List, Optional, Tuple
import uuid
from pathlib import Path
import re
import sys

from SYS.logger import log, debug, debug_panel
from SYS.payload_builders import build_file_result_payload, normalize_file_extension
from PluginCore.registry import REGISTRY, get_plugin_for_cmdlet, list_plugins_for_cmdlet
from SYS.rich_display import (
    show_plugin_config_panel,
    show_available_plugins_panel,
    show_no_search_plugins_panel,
)
from SYS.database import insert_worker, update_worker, append_worker_stdout
from SYS.item_accessors import get_extension_field, get_int_field, get_result_title
from SYS.selection_builder import build_default_selection
from SYS.result_publication import publish_result_table

from .._shared import (
    Cmdlet,
    CmdletArg,
    SharedArgs,
    get_field,
    get_preferred_store_backend,
    should_show_help,
    normalize_hash,
    first_title_tag,
    parse_hash_query,
)
from SYS import pipeline as ctx

# Web search engine functions extracted to search_engines.py
from .search_engines import (
    _WHITESPACE_RE,
    query_web_search,
    crawl_site_for_extension,
    build_web_search_plan,
    _normalize_extension,
    scrape_page_assets,
    assemble_scrape_result_rows,
)


_LIMIT_INLINE_RE: re.Pattern = re.compile(
    r"(?:^|[\s,])limit\s*[=:]\s*(\d+)(?=[\s,]|$)",
    flags=re.IGNORECASE,
)


def _extract_limit_from_query(query: str) -> Tuple[str, int, bool]:
    """Pull limit:N out of a -query string; return cleaned query, limit, and whether set.

    Only the ``limit`` token is extracted. Everything else is preserved verbatim so
    namespace tag searches with spaces (e.g. ``channel:kabbalah for heretics``) are
    not reordered or truncated into a bare ``channel:kabbalah`` tag.
    """
    text = str(query or "").strip()
    if not text:
        return "", 100, False

    limit_set = False
    limit = 100

    match = _LIMIT_INLINE_RE.search(text)
    if match:
        try:
            limit = max(1, int(match.group(1)))
            limit_set = True
        except (TypeError, ValueError):
            limit = 100
            limit_set = False
        text = _LIMIT_INLINE_RE.sub(" ", text)

    cleaned = re.sub(r"\s{2,}", " ", text).strip().strip(",")
    return cleaned, limit, limit_set

_STORE_FILTER_RE: re.Pattern = re.compile(
    r"\binstance:(\[[^\]]*\]|[^\s,]+)",
    flags=re.IGNORECASE,
)
_STORE_FILTER_REMOVE_RE: re.Pattern = re.compile(
    r"\s*,?\s*instance:(?:\[[^\]]*\]|[^\s,]+)",
    flags=re.IGNORECASE,
)


def _configured_search_plugins(config: Optional[Dict[str, Any]]) -> List[str]:
    raw = (config or {}).get("search_plugins")
    if isinstance(raw, str):
        return [part.strip() for part in re.split(r"[\s,]+", raw) if part.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(part).strip() for part in raw if str(part).strip()]
    return []


def _declared_search_plugins() -> List[str]:
    names: List[str] = []
    try:
        REGISTRY.discover()
        for info in REGISTRY.get_plugins_for_cmdlet("search-file"):
            name = str(getattr(info, "canonical_name", "") or "").strip()
            if name:
                names.append(name)
    except Exception:
        return names
    return names


class _WorkerLogger:
    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id

    def __enter__(self) -> "_WorkerLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        return None

    def insert_worker(
        self,
        worker_id: str,
        worker_type: str,
        title: str = "",
        description: str = "",
        **kwargs: Any,
    ) -> None:
        try:
            insert_worker(worker_id, worker_type, title=title, description=description)
        except Exception:
            pass

    def update_worker_status(self, worker_id: str, status: str) -> None:
        try:
            normalized = (status or "").lower()
            kwargs: dict[str, str] = {"status": status}
            if normalized in {"completed", "error", "cancelled"}:
                kwargs["result"] = normalized
            update_worker(worker_id, **kwargs)
        except Exception:
            pass

    def append_worker_stdout(self, worker_id: str, content: str) -> None:
        try:
            append_worker_stdout(worker_id, content)
        except Exception:
            pass


def _truncate_worker_text(value: Any, max_len: int = 120) -> str:
    text = str(value or "").strip()
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return f"{text[:max_len - 3].rstrip()}..."


def _summarize_worker_result(item: Dict[str, Any]) -> str:
    title = (
        item.get("title")
        or item.get("name")
        or item.get("path")
        or item.get("url")
        or item.get("hash")
        or "Result"
    )
    details: list[str] = []

    store_val = str(item.get("store") or item.get("source") or "").strip()
    if store_val:
        details.append(store_val)

    ext_val = str(item.get("ext") or item.get("mime") or "").strip()
    if ext_val:
        details.append(ext_val)

    hash_val = str(
        item.get("hash") or item.get("file_hash") or item.get("hash_hex") or ""
    ).strip()
    if hash_val:
        details.append(hash_val[:12])

    suffix = f" [{' | '.join(details)}]" if details else ""
    return f"- {_truncate_worker_text(title)}{suffix}"


def _summarize_worker_results(results: Sequence[Dict[str, Any]], preview_limit: int = 8) -> str:
    count = len(results)
    lines = [f"{count} result(s)"]
    if count <= 0:
        return lines[0]

    for item in results[:preview_limit]:
        lines.append(_summarize_worker_result(item))

    remaining = count - min(count, preview_limit)
    if remaining > 0:
        lines.append(f"... {remaining} more")

    return "\n".join(lines)


class search_file(Cmdlet):
    """Class-based search-file cmdlet for searching backends and plugins."""

    def __init__(self) -> None:
        super().__init__(
            name="search-file",
            summary="Search configured instances or search-capable plugins.",
            usage='search-file [-plugin NAME] [-query "text instance:NAME limit:30"]',
            arg=[
                SharedArgs.PLUGIN,
                SharedArgs.INSTANCE,
                SharedArgs.QUERY,
                SharedArgs.LIMIT,
                CmdletArg(
                    "open",
                    type="integer",
                    description="(alldebrid) Open folder/magnet by ID and list its files",
                ),
                CmdletArg(
                    "-all",
                    type="flag",
                    required=False,
                    description="List every file, not only those with a .metadata sidecar",
                ),
            ],
            detail=[
                "Search across configured storage backends or plugins.",
                "Preferred order: -plugin, then -query.",
                'Limit results with -query "limit:N" (default 100; plugin default 50).',
                'Target instances with -query "instance:NAME" or "instance:[rpi,Typhon]".',
                'Exclude: !(green thumbs) or !green (Hydrus uses -tag).',
                "Multiple plugins: -plugin [ytdlp,soulseek] (one table, Plugin column).",
                "Use -plugin with instance: to target a named plugin config.",
                "URL search: url:* (any URL) or url:<value> (URL substring)",
                "Extension search: ext:<value> (e.g., ext:png)",
                "Hydrus-style extension: system:filetype = png",
                "Results include hash for downstream commands (download-file, add-tag, etc.)",
                "Examples:",
                "search-file -query foo                              # Search all storage backends",
                'search-file -query "video limit:30"                 # Limit to 30 results',
                "search-file -instance home -query '*'               # Search 'home' Hydrus instance",
                "search-file -instance home -query 'video'           # Search 'home' Hydrus instance",
                "search-file -query 'hash:deadbeef...'               # Search by SHA256 hash",
                "search-file -query 'url:*'                          # Files that have any URL",
                "search-file -query 'url:youtube.com'                # Files whose URL contains substring",
                "search-file -query 'ext:png'                        # Files whose metadata ext is png",
                "search-file -query 'system:filetype = png'          # Hydrus: native",
                "search-file -query 'channel:kabbalah for heretics'   # Namespaced tag (multi-word value)",
                "search-file 'example.com/path' -query 'ext:pdf'     # Web: site:example.com filetype:pdf",
                "search-file -query 'site:example.com filetype:epub history'",
                'search-file "https://example.com/gallery"           # Scrape a page for downloadable file types',
                'search-file "https://example.com/gallery" -query "type:image"',
                "",
                "Plugin search (-plugin):",
                "search-file -plugin ytdlp -query 'search:tutorial'",
                'search-file -plugin ytdlp -query "tutorial limit:20"',
                "search-file -plugin ftp -instance work -query '*'",
                "search-file -plugin alldebrid -query '*'",
                "search-file -plugin alldebrid -open 123 -query '*'",
            ],
            exec=self.run,
        )
        self.register()

    # --- Helper methods from search_engines.py are patched below ---
    # The run() method uses self._build_web_search_plan(), self._run_web_search(), etc.
    # These are assigned via late-binding at module bottom.

    @staticmethod
    def _normalize_lookup_target(value: Optional[str]) -> str:
        """Normalize candidate names for store/provider matching."""
        raw = str(value or "").strip().lower()
        return "".join(ch for ch in raw if ch.isalnum())

    @staticmethod
    def _extract_namespace_tags(payload: Dict[str, Any]) -> List[str]:
        """Return deduplicated namespace tags from payload, excluding title:* tags."""
        candidates: List[str] = []

        def _add_candidate(value: Any) -> None:
            if isinstance(value, str):
                text = value.strip()
                if text:
                    parts = re.split(r"[,;\n\r]+", text)
                    for part in parts:
                        token = part.strip().strip("[](){}\"'#")
                        if token:
                            candidates.append(token)
            elif isinstance(value, dict):
                for nested in value.values():
                    _add_candidate(nested)
            elif isinstance(value, (list, tuple, set)):
                for item in value:
                    _add_candidate(item)

        _add_candidate(payload.get("tag"))
        _add_candidate(payload.get("tags"))
        _add_candidate(payload.get("tag_summary"))

        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            _add_candidate(metadata.get("tag"))
            _add_candidate(metadata.get("tags"))

            meta_tags = metadata.get("tags")
            if isinstance(meta_tags, dict):
                for service_data in meta_tags.values():
                    if not isinstance(service_data, dict):
                        continue
                    display_tags = service_data.get("display_tags")
                    if isinstance(display_tags, dict):
                        for ns_name, tag_list in display_tags.items():
                            if isinstance(tag_list, list):
                                ns_text = str(ns_name or "").strip()
                                for tag_item in tag_list:
                                    item_text = str(tag_item or "").strip()
                                    if not item_text:
                                        continue
                                    if ":" in item_text:
                                        candidates.append(item_text)
                                        continue
                                    if ns_text:
                                        candidates.append(f"{ns_text}:{item_text}")
                                    else:
                                        candidates.append(item_text)
                            else:
                                _add_candidate(tag_list)

        namespace_tags: List[str] = []
        seen: set[str] = set()
        for raw in candidates:
            candidate = str(raw or "").strip()
            if not candidate or ":" not in candidate:
                continue

            ns, value = candidate.split(":", 1)
            ns_norm = ns.strip().lower()
            value_norm = value.strip()
            if not value_norm:
                continue
            if ns_norm == "title":
                continue

            normalized = f"{ns_norm}:{value_norm}"

            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            namespace_tags.append(normalized)

        return namespace_tags

    def _set_storage_display_columns(self, payload: Dict[str, Any]) -> None:
        """Set explicit display columns for store search results."""
        title_text = str(payload.get("title") or payload.get("name") or payload.get("filename") or "Result")
        namespace_tags = self._extract_namespace_tags(payload)
        tag_text = ", ".join(namespace_tags)

        store_text = str(payload.get("store") or payload.get("table") or payload.get("source") or "")
        size_raw = payload.get("size_bytes")
        if size_raw is None:
            size_raw = payload.get("size")
        ext_text = str(payload.get("ext") or "")

        payload["columns"] = [
            ("Title", title_text),
            ("Tag", tag_text),
            ("Instance", store_text),
            ("Size", size_raw),
            ("Ext", ext_text),
        ]

    def _ensure_storage_columns(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure storage results have the necessary fields for result_table display."""

        if "title" not in payload:
            payload["title"] = (
                payload.get("name") or payload.get("target") or payload.get("path")
                or "Result"
            )

        if ("ext" not in payload) or (not str(payload.get("ext") or "").strip()):
            title = str(payload.get("title", ""))
            path_obj = Path(title)
            if path_obj.suffix:
                payload["ext"] = _normalize_extension(path_obj.suffix.lstrip("."))
            else:
                payload["ext"] = payload.get("ext", "")

        self._set_storage_display_columns(payload)
        return payload

    def _run_multi_plugin_search(
        self,
        *,
        plugin_names: List[str],
        original_plugin_arg: str,
        instance_name: Optional[str],
        query: str,
        limit: int,
        limit_set: bool,
        open_id: Optional[int],
        args_list: List[str],
        refresh_mode: bool,
        config: Dict[str, Any],
    ) -> int:
        if not plugin_names or not query:
            from SYS import pipeline as ctx_mod
            progress = None
            if hasattr(ctx_mod, "get_pipeline_state"):
                progress = ctx_mod.get_pipeline_state().live_progress
            if progress:
                try:
                    progress.stop()
                except Exception:
                    pass
            log("Error: search-file -plugin requires both plugin and query", file=sys.stderr)
            log(f"Usage: {self.usage}", file=sys.stderr)
            return 1

        if not limit_set:
            limit = 50

        from SYS import pipeline as ctx_mod
        progress = None
        if hasattr(ctx_mod, "get_pipeline_state"):
            progress = ctx_mod.get_pipeline_state().live_progress

        providers_map = list_plugins_for_cmdlet("search-file", config)
        valid_plugins: List[Tuple[str, Any]] = []
        failed_names: List[str] = []

        for pname in plugin_names:
            provider = get_plugin_for_cmdlet(pname, "search-file", config)
            resolved = str(getattr(provider, "name", "") or pname).strip().lower()
            if not provider or not providers_map.get(resolved, False):
                failed_names.append(pname)
                continue
            valid_plugins.append((pname, provider))

        if not valid_plugins:
            if progress:
                try:
                    progress.stop()
                except Exception:
                    pass
            show_plugin_config_panel(failed_names or plugin_names, config)
            available = [n for n, a in providers_map.items() if a]
            if available:
                show_available_plugins_panel(available)
            return 1

        if failed_names:
            log(
                f"Warning: Skipping unconfigured plugins: {', '.join(failed_names)}",
                file=sys.stderr,
            )

        worker_id = str(uuid.uuid4())
        try:
            insert_worker(
                worker_id,
                "search-file",
                title=f"Search: {query}",
                description=f"Plugins: {', '.join(plugin_names)}, Query: {query}",
            )
        except Exception:
            pass

        try:
            results_list: List[Dict[str, Any]] = []
            from SYS.result_table import Table

            table_title = f"Combined Search: {query}"
            table = Table(table_title)._perseverance(True)
            table_type = "search"
            table.set_table(table_type)
            table.set_source_command("search-file", args_list)

            total_results = 0
            errors: List[str] = []

            for pname, provider in valid_plugins:
                try:
                    normalized_query = str(query or "").strip()
                    provider_filters: Dict[str, Any] = {}
                    try:
                        normalized_query, provider_filters = provider.extract_query_arguments(query)
                    except Exception:
                        provider_filters = {}

                    normalized_query = (normalized_query or "").strip()
                    effective_query = normalized_query or "*"
                    search_filters = dict(provider_filters or {})
                    if instance_name and not search_filters.get("instance"):
                        search_filters["instance"] = str(instance_name).strip()
                    if want_all:
                        search_filters["all"] = True

                    debug_panel(
                        f"search-file plugin request ({pname})",
                        [
                            ("plugin", pname),
                            ("instance", search_filters.get("instance") or "<default>"),
                            ("query", effective_query),
                            ("limit", limit),
                            ("filters", search_filters or "<none>"),
                        ],
                        border_style="cyan",
                    )

                    results = provider.search(
                        effective_query, limit=limit, filters=search_filters or None
                    )

                    debug_panel(
                        f"search-file plugin response ({pname})",
                        [
                            ("plugin", pname),
                            ("results", len(results or [])),
                        ],
                        border_style="cyan",
                    )

                    if not results:
                        continue

                    try:
                        post = getattr(provider, "postprocess_search_results", None)
                        if callable(post) and isinstance(results, list):
                            results, _to, _tmo = post(
                                query=effective_query,
                                results=results,
                                filters=search_filters or None,
                                limit=int(limit or 0),
                                table_type="search",
                                table_meta=None,
                            )
                    except Exception:
                        pass

                    for search_result in results:
                        cols = getattr(search_result, "columns", None)
                        if isinstance(cols, list):
                            has_plugin_col = any(
                                str(name or "").strip().lower() == "plugin" for name, _value in cols
                            )
                            if not has_plugin_col:
                                search_result.columns = [("Plugin", pname), *cols]
                        item_dict = (
                            search_result.to_dict()
                            if hasattr(search_result, "to_dict")
                            else dict(search_result)
                            if isinstance(search_result, dict)
                            else {"title": str(search_result)}
                        )

                        if "table" not in item_dict:
                            item_dict["table"] = table_type
                        if "source" not in item_dict:
                            item_dict["source"] = pname
                        item_dict["plugin"] = pname

                        table.add_result(search_result)
                        results_list.append(item_dict)
                        ctx.emit(item_dict)
                        total_results += 1

                except Exception as exc:
                    log(f"Error searching plugin '{pname}': {exc}", file=sys.stderr)
                    errors.append(pname)

            if not results_list:
                log(f"No results found for query: {query}", file=sys.stderr)
                try:
                    append_worker_stdout(worker_id, _summarize_worker_results([]))
                    update_worker(worker_id, status="completed")
                except Exception:
                    pass
                return 0

            publish_result_table(ctx, table, results_list, overlay=refresh_mode)
            ctx.set_current_stage_table(table)

            try:
                append_worker_stdout(worker_id, _summarize_worker_results(results_list))
                update_worker(worker_id, status="completed")
            except Exception:
                pass

            return 0

        except Exception as exc:
            log(f"Error during multi-plugin search: {exc}", file=sys.stderr)
            import traceback
            debug(traceback.format_exc())
            try:
                update_worker(worker_id, status="error")
            except Exception:
                pass
            return 1

    def _run_plugin_search(
        self,
        *,
        plugin_name: str,
        instance_name: Optional[str],
        query: str,
        limit: int,
        limit_set: bool,
        open_id: Optional[int],
        args_list: List[str],
        refresh_mode: bool,
        config: Dict[str, Any],
    ) -> int:
        """Execute external plugin search."""

        if not plugin_name or not query:
            from SYS import pipeline as ctx_mod
            progress = None
            if hasattr(ctx_mod, "get_pipeline_state"):
                progress = ctx_mod.get_pipeline_state().live_progress
            if progress:
                try:
                    progress.stop()
                except Exception:
                    pass

            log("Error: search-file -plugin requires both plugin and query", file=sys.stderr)
            log(f"Usage: {self.usage}", file=sys.stderr)

            providers_map = list_plugins_for_cmdlet("search-file", config)
            available = [n for n, a in providers_map.items() if a]
            unconfigured = [n for n, a in providers_map.items() if not a]

            if unconfigured:
                show_plugin_config_panel(unconfigured, config)

            if available:
                show_available_plugins_panel(available)

            return 1

        if not limit_set:
            limit = 50

        from SYS import pipeline as ctx_mod
        progress = None
        if hasattr(ctx_mod, "get_pipeline_state"):
            progress = ctx_mod.get_pipeline_state().live_progress

        providers_map = list_plugins_for_cmdlet("search-file", config)
        provider = get_plugin_for_cmdlet(plugin_name, "search-file", config)
        resolved_plugin_name = str(getattr(provider, "name", "") or plugin_name).strip().lower()
        if not provider or not providers_map.get(resolved_plugin_name, False):
            if progress:
                try:
                    progress.stop()
                except Exception:
                    pass

            show_plugin_config_panel([plugin_name], config)

            available = [n for n, a in providers_map.items() if a]
            if available:
                show_available_plugins_panel(available)
            return 1

        worker_id = str(uuid.uuid4())
        try:
            insert_worker(
                worker_id,
                "search-file",
                title=f"Search: {query}",
                description=f"Plugin: {plugin_name}, Query: {query}",
            )
        except Exception:
            pass

        try:
            results_list: List[Dict[str, Any]] = []

            from SYS.result_table import Table

            provider_text = str(plugin_name or "").strip()
            provider_lower = provider_text.lower()

            normalized_query = str(query or "").strip()
            provider_filters: Dict[str, Any] = {}
            try:
                normalized_query, provider_filters = provider.extract_query_arguments(query)
            except Exception:
                provider_filters = {}

            normalized_query = (normalized_query or "").strip()
            query = normalized_query or "*"
            search_filters = dict(provider_filters or {})
            if instance_name and not search_filters.get("instance"):
                search_filters["instance"] = str(instance_name).strip()

            table_title = provider.get_table_title(query, search_filters).strip().rstrip(":")
            table_type = provider.get_table_type(query, search_filters)
            table_meta = provider.get_table_metadata(query, search_filters)
            preserve_order = provider.preserve_order

            table = Table(table_title)._perseverance(preserve_order)
            table.set_table(table_type)
            try:
                table.set_table_metadata(table_meta)
            except Exception:
                pass

            source_cmd, source_args = provider.get_source_command(args_list)
            table.set_source_command(source_cmd, source_args)

            debug_panel(
                "search-file plugin request",
                [
                    ("plugin", plugin_name),
                    ("instance", search_filters.get("instance") or "<default>"),
                    ("query", query),
                    ("limit", limit),
                    ("filters", search_filters or "<none>"),
                ],
                border_style="cyan",
            )
            results = provider.search(query, limit=limit, filters=search_filters or None)
            debug_panel(
                "search-file plugin response",
                [
                    ("plugin", plugin_name),
                    ("results", len(results or [])),
                    ("table", table_type),
                ],
                border_style="cyan",
            )

            try:
                post = getattr(provider, "postprocess_search_results", None)
                if callable(post) and isinstance(results, list):
                    results, table_type_override, table_meta_override = post(
                        query=query,
                        results=results,
                        filters=search_filters or None,
                        limit=int(limit or 0),
                        table_type=str(table_type or ""),
                        table_meta=dict(table_meta) if isinstance(table_meta, dict) else None,
                    )
                    if table_type_override:
                        table_type = str(table_type_override)
                        table.set_table(table_type)
                    if isinstance(table_meta_override, dict) and table_meta_override:
                        table_meta = dict(table_meta_override)
                        try:
                            table.set_table_metadata(table_meta)
                        except Exception:
                            pass
            except Exception:
                pass

            if not results:
                log(f"No results found for query: {query}", file=sys.stderr)
                try:
                    append_worker_stdout(worker_id, _summarize_worker_results([]))
                    update_worker(worker_id, status="completed")
                except Exception:
                    pass
                return 0

            for search_result in results:
                item_dict = (
                    search_result.to_dict()
                    if hasattr(search_result, "to_dict")
                    else dict(search_result)
                    if isinstance(search_result, dict)
                    else {"title": str(search_result)}
                )

                if "table" not in item_dict:
                    item_dict["table"] = table_type

                if "source" not in item_dict:
                    item_dict["source"] = plugin_name

                table.add_result(search_result)

                results_list.append(item_dict)
                ctx.emit(item_dict)

            publish_result_table(ctx, table, results_list, overlay=refresh_mode)

            ctx.set_current_stage_table(table)

            try:
                append_worker_stdout(worker_id, _summarize_worker_results(results_list))
                update_worker(worker_id, status="completed")
            except Exception:
                pass

            return 0

        except Exception as exc:
            log(f"Error searching plugin '{plugin_name}': {exc}", file=sys.stderr)
            import traceback

            debug(traceback.format_exc())
            try:
                update_worker(worker_id, status="error")
            except Exception:
                pass
            return 1

    # --- Execution ------------------------------------------------------
    def run(self, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        """Search storage backends for files by various criteria.

        Supports searching by:
        - Hash (-query "hash:...")
        - Title (-query "title:...")
        - Tag (-query "tag:...")
        - URL (-query "url:...")
        - Other backend-specific fields

        Optimizations:
        - Extracts tags from metadata response (avoids duplicate API calls)
        - Only calls get_tag() separately for backends that don't include tags

        Args:
            result: Piped input (typically empty for new search)
            args: Search criteria and options
            config: Application configuration

        Returns:
            0 on success, 1 on error
        """
        if should_show_help(args):
            log(f"Cmdlet: {self.name}\nSummary: {self.summary}\nUsage: {self.usage}")
            return 0

        args_list = [str(arg) for arg in (args or [])]

        refresh_mode = any(
            str(a).strip().lower() in {"--refresh", "-refresh", "-internal-refresh"}
            for a in args_list
        )

        def _format_command_title(command: str, raw_args: List[str]) -> str:

            def _quote(value: str) -> str:
                text = str(value)
                if not text:
                    return '""'
                needs_quotes = any(ch.isspace() for ch in text) or '"' in text
                if not needs_quotes:
                    return text
                return '"' + text.replace('"', '\\"') + '"'

            cleaned = [
                str(a) for a in (raw_args or [])
                if str(a).strip().lower() not in {"--refresh", "-refresh", "-internal-refresh"}
            ]
            if not cleaned:
                return command
            return " ".join([command, *[_quote(a) for a in cleaned]])

        raw_title = None
        try:
            raw_title = (
                ctx.get_current_stage_text("")
                if hasattr(ctx, "get_current_stage_text") else None
            )
        except Exception:
            raw_title = None

        command_title = (str(raw_title).strip() if raw_title else
                         "") or _format_command_title("search-file",
                                                       list(args_list))

        flag_registry = self.build_flag_registry()
        query_flags = {
            f.lower()
            for f in (flag_registry.get("query") or {"-query", "--query"})
        }
        instance_flags = {
            f.lower()
            for f in (flag_registry.get("instance") or {"-instance", "--instance"})
        }
        plugin_flags = {
            f.lower()
            for f in (flag_registry.get("plugin") or {"-plugin", "--plugin"})
        }
        open_flags = {
            f.lower()
            for f in (flag_registry.get("open") or {"-open", "--open"})
        }
        valued_flags = (
            query_flags
            | instance_flags
            | plugin_flags
            | open_flags
        )

        query = ""
        storage_backend: Optional[str] = None
        instance_name: Optional[str] = None
        plugin_name: Optional[str] = None
        open_id: Optional[int] = None
        want_all = False
        limit = 100
        limit_set = False
        searched_backends: List[str] = []
        positional_args: List[str] = []

        i = 0
        while i < len(args_list):
            arg = args_list[i]
            low = arg.lower()
            next_arg = args_list[i + 1] if i + 1 < len(args_list) else None
            next_low = str(next_arg or "").lower()
            next_is_flag = bool(next_arg) and next_low in valued_flags

            if low in query_flags:
                if next_arg is not None and not next_is_flag:
                    chunk = next_arg
                    query = f"{query} {chunk}".strip() if query else chunk
                    i += 2
                    continue
                i += 1
                continue
            if low in plugin_flags:
                if next_arg is not None and not next_is_flag:
                    from SYS.utils import consume_bracket_list

                    plugin_name, i = consume_bracket_list(args_list, i)
                    continue
                i += 1
                continue
            if low in instance_flags:
                if next_arg is not None and not next_is_flag:
                    instance_name = next_arg
                    i += 2
                    continue
                i += 1
                continue
            if low in open_flags:
                if next_arg is not None and not next_is_flag:
                    try:
                        open_id = int(next_arg)
                    except ValueError:
                        log(
                            f"Warning: Invalid open value '{next_arg}', ignoring",
                            file=sys.stderr,
                        )
                        open_id = None
                    i += 2
                    continue
                i += 1
                continue
            if low in {"-all", "--all"}:
                want_all = True
                i += 1
                continue
            if low in {"-limit", "--limit"}:
                # Retired flag: prefer -query "limit:N". Keep a soft warning once.
                log(
                    'Warning: -limit is deprecated; use -query "limit:N" instead',
                    file=sys.stderr,
                )
                if next_arg is not None and not next_is_flag and not str(next_arg).startswith("-"):
                    i += 2
                else:
                    i += 1
                continue
            if not arg.startswith("-"):
                positional_args.append(arg)
                query = f"{query} {arg}".strip() if query else arg
                i += 1
            else:
                if arg.startswith("-"):
                    log(f"Warning: unrecognized flag '{arg}'", file=sys.stderr)
                i += 1

        query = query.strip()
        query, query_limit, query_limit_set = _extract_limit_from_query(query)
        if query_limit_set:
            limit = query_limit
            limit_set = True

        if not plugin_name and instance_name and not storage_backend:
            storage_backend = instance_name

        if not plugin_name:
            defaults = _configured_search_plugins(config)
            if defaults:
                plugin_name = ",".join(defaults)

        if plugin_name:
            if storage_backend and not instance_name:
                instance_name = storage_backend
            from SYS.utils import split_instance_names

            plugin_names = split_instance_names(plugin_name)
            if len(plugin_names) <= 1 and "[" not in str(plugin_name or ""):
                plugin_names = [p.strip() for p in re.split(r"[\s,]+", plugin_name) if p.strip()]
            if len(plugin_names) > 1:
                return self._run_multi_plugin_search(
                    plugin_names=plugin_names,
                    original_plugin_arg=plugin_name,
                    instance_name=instance_name,
                    query=query,
                    limit=limit,
                    limit_set=limit_set,
                    open_id=open_id,
                    args_list=args_list,
                    refresh_mode=refresh_mode,
                    config=config,
                )
            return self._run_plugin_search(
                plugin_name=plugin_names[0] if plugin_names else plugin_name,
                instance_name=instance_name,
                query=query,
                limit=limit,
                limit_set=limit_set,
                open_id=open_id,
                args_list=args_list,
                refresh_mode=refresh_mode,
                config=config,
            )

        store_filter: Optional[str] = None
        if query:
            match = _STORE_FILTER_RE.search(query)
            if match:
                store_filter = match.group(1).strip() or None
            query = _STORE_FILTER_REMOVE_RE.sub(" ", query)
            query = _WHITESPACE_RE.sub(" ", query)
            query = query.strip().strip(",")

        if store_filter and not storage_backend:
            storage_backend = store_filter

        hash_query = parse_hash_query(query)

        web_plan = self._build_web_search_plan(
            query=query,
            positional_args=positional_args,
            storage_backend=storage_backend,
            store_filter=store_filter,
            hash_query=hash_query,
        )
        if web_plan is not None:
            return self._run_web_search(
                web_plan=web_plan,
                limit=limit,
                args_list=args_list,
                refresh_mode=refresh_mode,
                command_title=command_title,
            )

        if not query:
            log("Provide a search query", file=sys.stderr)
            return 1

        worker_id = str(uuid.uuid4())

        from PluginCore.backend_registry import BackendRegistry
        storage_registry = BackendRegistry(config=config or {})

        if not storage_registry.list_backends():
            if "-internal-refresh" in args_list:
                return 1

            from SYS import pipeline as ctx_mod
            progress = None
            if hasattr(ctx_mod, "get_pipeline_state"):
                progress = ctx_mod.get_pipeline_state().live_progress
            if progress:
                try:
                    progress.stop()
                except Exception:
                    pass
            providers_map = list_plugins_for_cmdlet("search-file", config or {})
            available = [name for name, ok in providers_map.items() if ok]
            declared = _declared_search_plugins()
            if available:
                show_available_plugins_panel(available)
                log(
                    "No default search plugins. Set search_plugins in .config or pass -plugin NAME.",
                    file=sys.stderr,
                )
            elif declared:
                show_plugin_config_panel(declared, config or {})
                log(
                    "Search plugins are installed but not configured. Set them in .config, then search_plugins for defaults.",
                    file=sys.stderr,
                )
            else:
                show_no_search_plugins_panel()
            return 1

        with _WorkerLogger(worker_id) as db:
            try:
                if "-internal-refresh" not in args_list:
                    db.insert_worker(
                        worker_id,
                        "search-file",
                        title=f"Search: {query}",
                        description=f"Query: {query}",
                        pipe=ctx.get_current_command_text(),
                    )

                results_list = []
                from SYS.result_table import Table

                table = Table(command_title)
                try:
                    table.set_source_command("search-file", list(args_list))
                except Exception:
                    pass
                if hash_query:
                    try:
                        table._perseverance(True)
                    except Exception:
                        pass

                from PluginCore.backend_registry import list_configured_backend_names, get_backend_instance
                from PluginCore.backend_base import BackendBase

                backend_to_search = storage_backend or None

                if hash_query:
                    backends_to_try: List[str] = []
                    if backend_to_search:
                        backends_to_try = [backend_to_search]
                    else:
                        backends_to_try = list_configured_backend_names(config or {})

                    found_any = False
                    backend_registry_cache = None
                    for h in hash_query:
                        resolved_backend_name: Optional[str] = None
                        resolved_backend = None

                        for backend_name in backends_to_try:
                            backend, backend_registry_cache, _exc = get_preferred_store_backend(
                                config,
                                backend_name,
                                store_registry=backend_registry_cache,
                                suppress_debug=True,
                            )
                            if backend is None:
                                continue
                            try:
                                meta = backend.get_metadata(h)
                                if meta is None:
                                    continue
                                resolved_backend_name = backend_name
                                resolved_backend = backend
                                break
                            except Exception:
                                continue

                        if resolved_backend_name is None or resolved_backend is None:
                            continue

                        found_any = True
                        searched_backends.append(resolved_backend_name)

                        path_str: Optional[str] = None

                        meta_obj: Dict[str, Any] = {}
                        try:
                            meta_obj = resolved_backend.get_metadata(h) or {}
                        except Exception:
                            meta_obj = {}

                        tags_list: List[str] = []
                        maybe_flat = meta_obj.get("tag")
                        if isinstance(maybe_flat, list):
                            tags_list = [
                                str(tag_item).strip()
                                for tag_item in maybe_flat
                                if str(tag_item or "").strip()
                            ]

                        metadata_tags = meta_obj.get("tags")
                        if isinstance(metadata_tags, dict):
                            collected_tags: List[str] = []
                            for service_data in metadata_tags.values():
                                if not isinstance(service_data, dict):
                                    continue
                                for container_key in ("storage_tags", "display_tags", "statuses_to_tags"):
                                    container = service_data.get(container_key)
                                    if isinstance(container, dict):
                                        for ns_name, tag_list in container.items():
                                            if not isinstance(tag_list, list):
                                                continue
                                            ns_text = str(ns_name or "").strip()
                                            if ns_text in {"0", "1", "2", "3"}:
                                                ns_text = ""
                                            for tag_item in tag_list:
                                                tag_text = str(tag_item or "").strip()
                                                if not tag_text:
                                                    continue
                                                if ":" in tag_text or not ns_text:
                                                    collected_tags.append(tag_text)
                                                else:
                                                    collected_tags.append(f"{ns_text}:{tag_text}")
                                    elif isinstance(container, list):
                                        collected_tags.extend(
                                            str(tag_item).strip()
                                            for tag_item in container
                                            if str(tag_item or "").strip()
                                        )
                            if collected_tags:
                                dedup: List[str] = []
                                seen_tags: set[str] = set()
                                for tag_text in collected_tags:
                                    key = tag_text.lower()
                                    if key in seen_tags:
                                        continue
                                    seen_tags.add(key)
                                    dedup.append(tag_text)
                                tags_list = dedup

                        if not tags_list:
                            try:
                                tag_result = resolved_backend.get_tag(h)
                                if isinstance(tag_result, tuple) and tag_result:
                                    maybe_tags = tag_result[0]
                                else:
                                    maybe_tags = tag_result
                                if isinstance(maybe_tags, list):
                                    tags_list = [
                                        str(t).strip() for t in maybe_tags
                                        if isinstance(t, str) and str(t).strip()
                                    ]
                            except Exception:
                                tags_list = []

                        title_from_tag: Optional[str] = None
                        try:
                            title_tag = first_title_tag(tags_list)
                            if title_tag and ":" in title_tag:
                                title_from_tag = title_tag.split(":", 1)[1].strip()
                        except Exception:
                            title_from_tag = None

                        title = title_from_tag or get_result_title(meta_obj, "title", "name")
                        if not title and path_str:
                            try:
                                title = Path(path_str).stem
                            except Exception:
                                title = path_str

                        ext_val = get_extension_field(meta_obj, "ext", "extension")
                        if not ext_val and path_str:
                            try:
                                ext_val = Path(path_str).suffix
                            except Exception:
                                ext_val = None
                        if not ext_val and title:
                            try:
                                ext_val = Path(str(title)).suffix
                            except Exception:
                                ext_val = None

                        size_bytes_int = get_int_field(meta_obj, "size", "size_bytes")

                        payload = build_file_result_payload(
                            title=title,
                            fallback_title=h,
                            hash_value=h,
                            store=resolved_backend_name,
                            path=path_str,
                            ext=ext_val,
                            size_bytes=size_bytes_int,
                            tag=tags_list,
                            url=meta_obj.get("url") or [],
                        )

                        self._set_storage_display_columns(payload)

                        table.add_result(payload)
                        results_list.append(payload)
                        ctx.emit(payload)

                    if found_any:
                        table.title = command_title

                        if refresh_mode and len(results_list) == 1:
                            try:
                                from SYS.rich_display import render_item_details_panel
                                render_item_details_panel(results_list[0])
                                table._rendered_by_cmdlet = True
                            except Exception:
                                pass

                        if refresh_mode:
                            ctx.set_last_result_table_overlay(
                                table,
                                results_list
                            )
                        else:
                            ctx.set_last_result_table(table, results_list)
                        db.append_worker_stdout(
                            worker_id,
                            _summarize_worker_results(results_list)
                        )
                        db.update_worker_status(worker_id, "completed")
                        return 0

                    log("No results found", file=sys.stderr)
                    if refresh_mode:
                        try:
                            table.title = command_title
                            ctx.set_last_result_table_overlay(table, [])
                        except Exception:
                            pass
                    db.append_worker_stdout(worker_id, _summarize_worker_results([]))
                    db.update_worker_status(worker_id, "completed")
                    return 0

                if backend_to_search:
                    searched_backends.append(backend_to_search)
                    target_backend, _store_registry, exc = get_preferred_store_backend(
                        config,
                        backend_to_search,
                        suppress_debug=True,
                    )
                    if target_backend is None:
                        if exc is not None:
                            log(f"Backend '{backend_to_search}' not found: {exc}", file=sys.stderr)
                            db.update_worker_status(worker_id, "error")
                            return 1
                        debug(f"[search-file] Requested backend '{backend_to_search}' not found")
                        return 1
                    try:
                        pass
                    except Exception as exc:
                        log(f"Backend '{backend_to_search}' not found: {exc}", file=sys.stderr)
                        db.update_worker_status(worker_id, "error")
                        return 1

                    if type(target_backend).search is BackendBase.search:
                        log(
                            f"Backend '{backend_to_search}' does not support searching",
                            file=sys.stderr,
                        )
                        db.update_worker_status(worker_id, "error")
                        return 1
                    results = target_backend.search(query, limit=limit)
                else:
                    all_results = []
                    backend_registry_cache = None
                    for backend_name in list_configured_backend_names(config or {}):
                        try:
                            backend, backend_registry_cache, _exc = get_preferred_store_backend(
                                config,
                                backend_name,
                                store_registry=backend_registry_cache,
                                suppress_debug=True,
                            )
                            if backend is None:
                                continue

                            searched_backends.append(backend_name)

                            if type(backend).search is BackendBase.search:
                                continue

                            backend_results = backend.search(
                                query,
                                limit=limit - len(all_results)
                            )
                            if backend_results:
                                all_results.extend(backend_results)
                            if len(all_results) >= limit:
                                break
                        except Exception as exc:
                            log(
                                f"Backend {backend_name} search failed: {exc}",
                                file=sys.stderr
                            )
                    results = all_results[:limit]

                if results:
                    for item in results:

                        def _as_dict(obj: Any) -> Dict[str, Any]:
                            if isinstance(obj, dict):
                                return dict(obj)
                            if hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
                                return obj.to_dict()  # type: ignore[arg-type]
                            return {
                                "title": str(obj)
                            }

                        item_dict = _as_dict(item)
                        if store_filter:
                            store_val = str(item_dict.get("store") or "").lower()
                            if store_filter != store_val:
                                continue

                        normalized = self._ensure_storage_columns(item_dict)

                        if "title" not in normalized:
                            normalized["title"] = (
                                item_dict.get("title") or item_dict.get("name") or
                                item_dict.get("path") or item_dict.get("target") or "Result"
                            )
                        if "ext" not in normalized:
                            t = str(normalized.get("title", ""))
                            if "." in t:
                                normalized["ext"] = t.split(".")[-1].lower()[:5]

                        hash_val = normalized.get("hash")
                        store_val = normalized.get("store") or item_dict.get("store") or backend_to_search
                        if hash_val and not normalized.get("hash"):
                            normalized["hash"] = hash_val
                        if store_val and not normalized.get("store"):
                            normalized["store"] = store_val

                        try:
                            sel_args, sel_action = build_default_selection(
                                path_value=normalized.get("path") or normalized.get("target") or normalized.get("url"),
                                hash_value=normalized.get("hash") or normalized.get("file_hash") or normalized.get("hash_hex"),
                                store_value=normalized.get("store"),
                            )
                            if sel_args:
                                normalized["_selection_args"] = [str(x) for x in sel_args]
                            if sel_action:
                                normalized["_selection_action"] = [str(x) for x in sel_action]
                        except Exception:
                            pass

                        table.add_result(normalized)

                        results_list.append(normalized)
                        ctx.emit(normalized)

                    table.title = command_title

                    if refresh_mode and len(results_list) == 1:
                        try:
                            from SYS.rich_display import render_item_details_panel
                            render_item_details_panel(results_list[0])
                            table._rendered_by_cmdlet = True
                        except Exception:
                            pass

                    if refresh_mode:
                        try:
                            subject_context = None
                            if "hash:" in query:
                                subject_hash = query.split("hash:")[1].split(",")[0].strip()
                                subject_context = {"store": backend_to_search, "hash": subject_hash}

                            publish_result_table(
                                ctx,
                                table,
                                results_list,
                                subject=subject_context,
                                overlay=True,
                            )
                        except Exception:
                            publish_result_table(ctx, table, results_list, overlay=True)
                    else:
                        publish_result_table(ctx, table, results_list)
                    db.append_worker_stdout(
                        worker_id,
                        _summarize_worker_results(results_list)
                    )
                else:
                    log("No results found", file=sys.stderr)
                    if refresh_mode:
                        try:
                            table.title = command_title
                            ctx.set_last_result_table_overlay(table, [])
                        except Exception:
                            pass
                    db.append_worker_stdout(worker_id, _summarize_worker_results([]))

                db.update_worker_status(worker_id, "completed")
                return 0

            except Exception as exc:
                log(f"Search failed: {exc}", file=sys.stderr)
                import traceback

                traceback.print_exc(file=sys.stderr)
                try:
                    db.update_worker_status(worker_id, "error")
                except Exception:
                    pass
                return 1

    def _run_page_scrape(
        self,
        *,
        page_url: str,
        site_host: str,
        type_group: str,
        requested_type: str,
        limit: int,
        args_list: List[str],
        refresh_mode: bool,
        command_title: str,
    ) -> int:
        worker_id = str(uuid.uuid4())
        try:
            insert_worker(
                worker_id,
                "search-file",
                title=f"Scrape: {page_url}",
                description=f"Site: {site_host}",
            )
        except Exception:
            pass

        try:
            from SYS.result_table import Table

            assets = scrape_page_assets(page_url)
            show_types, table_name, results_list, empty_message = assemble_scrape_result_rows(
                page_url=page_url,
                assets=assets,
                type_group=type_group,
                filetype=requested_type,
                command="search-file",
                site_host=site_host,
                limit=limit,
            )
            table = Table(command_title)
            table.set_table(table_name)
            table.set_source_command("search-file", list(args_list))
            try:
                table.set_table_metadata(
                    {
                        "plugin": "web",
                        "site": site_host,
                        "query": page_url,
                        "scrape_url": page_url,
                        "filetype": requested_type,
                        "type_group": type_group,
                    }
                )
            except Exception:
                pass

            if not results_list:
                log(empty_message, file=sys.stderr)
                if refresh_mode:
                    try:
                        ctx.set_last_result_table_overlay(table, [])
                    except Exception:
                        pass
                try:
                    append_worker_stdout(worker_id, _summarize_worker_results([]))
                    update_worker(worker_id, status="completed")
                except Exception:
                    pass
                return 0

            for payload in results_list:
                table.add_result(payload)
                ctx.emit(payload)

            publish_result_table(ctx, table, results_list, overlay=refresh_mode)
            ctx.set_current_stage_table(table)
            try:
                append_worker_stdout(worker_id, _summarize_worker_results(results_list))
                update_worker(worker_id, status="completed")
            except Exception:
                pass
            return 0
        except Exception as exc:
            log(f"Page scrape failed: {exc}", file=sys.stderr)
            try:
                update_worker(worker_id, status="error")
            except Exception:
                pass
            return 1

    def _run_web_search(
        self,
        *,
        web_plan: Dict[str, Any],
        limit: int,
        args_list: List[str],
        refresh_mode: bool,
        command_title: str,
    ) -> int:
        """Execute URL-scoped web search and emit downloadable table rows."""
        site_host = str(web_plan.get("site_host") or "").strip().lower()
        search_query = str(web_plan.get("search_query") or "").strip()
        requested_type = _normalize_extension(web_plan.get("filetype") or "")
        seed_url = str(web_plan.get("seed_url") or "").strip()
        scrape_url = str(web_plan.get("scrape_url") or "").strip()
        type_group = str(web_plan.get("type_group") or "").strip().lower()

        if not site_host or not search_query:
            log("Error: invalid website search request", file=sys.stderr)
            return 1

        if scrape_url:
            return self._run_page_scrape(
                page_url=scrape_url,
                site_host=site_host,
                type_group=type_group,
                requested_type=requested_type,
                limit=limit,
                args_list=args_list,
                refresh_mode=refresh_mode,
                command_title=command_title,
            )

        worker_id = str(uuid.uuid4())
        try:
            insert_worker(
                worker_id,
                "search-file",
                title=f"Web Search: {search_query}",
                description=f"Site: {site_host}",
            )
        except Exception:
            pass

        try:
            from SYS.result_table import Table
            from pathlib import PurePosixPath as _PP

            rows = query_web_search(
                search_query=search_query,
                site_host=site_host,
                limit=limit,
            )

            if not rows and requested_type:
                debug(
                    "Web search returned 0 rows; falling back to in-site crawl",
                    {"site": site_host, "ext": requested_type, "seed_url": seed_url},
                )
                rows = crawl_site_for_extension(
                    seed_url=seed_url or f"https://{site_host}/",
                    site_host=site_host,
                    extension=requested_type,
                    limit=limit,
                    max_duration_seconds=10.0,
                )

            table = Table(command_title)
            table.set_table("web.search")
            table.set_source_command("search-file", list(args_list))
            try:
                table.set_table_metadata(
                    {
                        "plugin": "web",
                        "site": site_host,
                        "query": search_query,
                        "filetype": requested_type,
                    }
                )
            except Exception:
                pass

            if not rows:
                log(f"No web results found for query: {search_query}", file=sys.stderr)
                if refresh_mode:
                    try:
                        ctx.set_last_result_table_overlay(table, [])
                    except Exception:
                        pass
                try:
                    append_worker_stdout(worker_id, _summarize_worker_results([]))
                    update_worker(worker_id, status="completed")
                except Exception:
                    pass
                return 0

            results_list: List[Dict[str, Any]] = []
            for row in rows:
                target_url = str(row.get("url") or "").strip()
                if not target_url:
                    continue

                source_title = str(row.get("title") or "").strip()
                title = source_title or target_url
                snippet = _WHITESPACE_RE.sub(" ", str(row.get("snippet") or "")).strip()
                if len(snippet) > 120:
                    snippet = f"{snippet[:117].rstrip()}..."

                detected_ext = requested_type
                file_name = ""
                if not detected_ext:
                    try:
                        from urllib.parse import urlparse as _urlparse, unquote as _unquote
                        parsed_path = _PP(_urlparse(target_url).path)
                        file_name = _PP(_unquote(str(parsed_path))).name
                        detected_ext = _normalize_extension(parsed_path.suffix)
                    except Exception:
                        detected_ext = ""
                else:
                    try:
                        from urllib.parse import urlparse as _urlparse, unquote as _unquote
                        file_name = _PP(_unquote(_urlparse(target_url).path)).name
                    except Exception:
                        file_name = ""

                if file_name:
                    title = file_name

                payload = build_file_result_payload(
                    title=title,
                    path=target_url,
                    url=target_url,
                    source="web",
                    store="web",
                    table="web.search",
                    ext=detected_ext,
                    detail=snippet,
                    tag=[f"site:{site_host}"] + ([f"type:{detected_ext}"] if detected_ext else []),
                    columns=[
                        ("Title", title),
                        ("Type", detected_ext),
                        ("URL", target_url),
                    ],
                    _selection_args=["-url", target_url],
                    _selection_action=["download-file", "-url", target_url],
                )

                table.add_result(payload)
                results_list.append(payload)
                ctx.emit(payload)

                publish_result_table(ctx, table, results_list, overlay=refresh_mode)

            ctx.set_current_stage_table(table)

            try:
                append_worker_stdout(worker_id, _summarize_worker_results(results_list))
                update_worker(worker_id, status="completed")
            except Exception:
                pass

            return 0

        except Exception as exc:
            log(f"Web search failed: {exc}", file=sys.stderr)
            try:
                update_worker(worker_id, status="error")
            except Exception:
                pass
            return 1


# Late-binding: attach search engine functions as classmethod/staticmethod
from .search_engines import (  # noqa: E402
    _normalize_host,
    _extract_site_host,
    _normalize_space,
    _normalize_seed_url,
    _is_probable_html_path,
    _extract_html_links,
    _extract_duckduckgo_target_url,
    _extract_yahoo_target_url,
    _url_matches_site,
    _itertext_join,
    _html_fragment_to_text,
    _append_web_result,
    _parse_web_results_with_fallback,
    parse_duckduckgo_results as _parse_duckduckgo_results,
    parse_yahoo_results as _parse_yahoo_results,
    query_yahoo as _query_yahoo,
    parse_bing_results as _parse_bing_results,
    query_bing as _query_bing,
)

search_file._normalize_host = staticmethod(_normalize_host)
search_file._extract_site_host = classmethod(_extract_site_host)
search_file._normalize_space = staticmethod(_normalize_space)
search_file._build_web_search_plan = classmethod(build_web_search_plan)
search_file._normalize_seed_url = classmethod(_normalize_seed_url)
search_file._is_probable_html_path = staticmethod(_is_probable_html_path)
search_file._extract_html_links = classmethod(_extract_html_links)
search_file._crawl_site_for_extension = classmethod(crawl_site_for_extension)
search_file._extract_duckduckgo_target_url = staticmethod(_extract_duckduckgo_target_url)
search_file._extract_yahoo_target_url = staticmethod(_extract_yahoo_target_url)
search_file._url_matches_site = classmethod(_url_matches_site)
search_file._itertext_join = staticmethod(_itertext_join)
search_file._html_fragment_to_text = staticmethod(_html_fragment_to_text)
search_file._append_web_result = classmethod(_append_web_result)
search_file._parse_web_results_with_fallback = classmethod(_parse_web_results_with_fallback)
search_file._parse_duckduckgo_results = classmethod(_parse_duckduckgo_results)
search_file._parse_yahoo_results = classmethod(_parse_yahoo_results)
search_file._query_yahoo = classmethod(_query_yahoo)
search_file._parse_bing_results = classmethod(_parse_bing_results)
search_file._query_web_search = classmethod(query_web_search)
search_file._query_bing = classmethod(_query_bing)


CMDLET = search_file()
