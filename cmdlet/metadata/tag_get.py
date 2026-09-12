"""Get tags from Hydrus or local sidecar metadata.

This cmdlet retrieves tags for a selected result, supporting both:
- Hydrus Network (for files with hash)
- Local sidecar files (.tag)

In interactive mode: navigate with numbers, add/delete tags
In pipeline mode: display tags as read-only table, emit as structured JSON
"""

from __future__ import annotations

import sys

from SYS.logger import log, debug

# plugins.metadata_plus is deferred: it transitively loads yt_dlp, Cryptodome,
# imdbinfo, musicbrainzngs and ~1400 modules (~1.5s). Import lazily on first use.
_METADATA_PLUGIN_MOD: Any = None
_METADATA_PLUGIN_LOADED = False


def _mp() -> Any:
    """Return the (lazily imported) metadata+ module, or None."""
    global _METADATA_PLUGIN_MOD, _METADATA_PLUGIN_LOADED
    if not _METADATA_PLUGIN_LOADED:
        from PluginCore.registry import import_plugin_module

        _METADATA_PLUGIN_MOD = import_plugin_module("metadata_plus") or import_plugin_module(
            "metadata_plugin"
        )
        _METADATA_PLUGIN_LOADED = True
    return _METADATA_PLUGIN_MOD


from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from SYS import pipeline as ctx
from SYS.pipeline_progress import PipelineProgress
from SYS.result_publication import publish_result_table
from SYS.result_table_helpers import add_row_columns
from .. import _shared as sh

normalize_hash = sh.normalize_hash
looks_like_hash = sh.looks_like_hash
get_field = sh.get_field
merge_sequences = sh.merge_sequences
Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
SharedArgs = sh.SharedArgs
parse_cmdlet_args = sh.parse_cmdlet_args


def _dedup_tags_preserve_order(tags: List[str]) -> List[str]:
    return merge_sequences(tags, case_sensitive=False)


# Tag item for ResultTable display and piping
from dataclasses import dataclass


@dataclass
class TagItem:
    """Tag item for display in ResultTable and piping to other cmdlet.

    Allows tags to be selected and piped like:
    - delete-tag @{3,4,9}  (delete tags at indices 3, 4, 9)
    - add-tag @"namespace:value"  (add this tag)
    """

    tag_name: str
    tag_index: int  # 1-based index for user reference
    hash: Optional[str] = None
    store: Optional[str] = None
    instance: str = "hydrus"
    service_name: Optional[str] = None
    path: Optional[str] = None

    def __post_init__(self) -> None:
        self.detail = f"Tag #{self.tag_index}"
        self.target = self.tag_name
        self.media_kind = "tag"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tag_name": self.tag_name,
            "tag_index": self.tag_index,
            "hash": self.hash,
            "store": self.store,
            "path": self.path,
            "service_name": self.service_name,
        }


def _emit_tag_payload(
    source: str,
    tags_list: List[str],
    *,
    hash_value: Optional[str] = None,
    store_label: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> int:
    tags = [str(tag).strip() for tag in tags_list or [] if str(tag or "").strip()]
    payload: Dict[str, Any] = {
        "source": str(source or "").strip() or "tag",
        "tag": tags,
        "tags": list(tags),
        "hash": hash_value,
    }
    if isinstance(extra, dict) and extra:
        payload["extra"] = dict(extra)

    label = str(store_label or "").strip() if store_label else ""
    if not label and ctx.get_stage_context() is not None:
        label = "tag"
    if label:
        ctx.store_value(label, payload)

    if ctx.get_stage_context() is not None:
        for idx, tag_name in enumerate(tags, start=1):
            ctx.emit(
                TagItem(
                    tag_name=tag_name,
                    tag_index=idx,
                    hash=hash_value,
                    store=str(source or "tag"),
                    service_name=None,
                )
            )
    else:
        ctx.emit(payload)

    return 0


def _emit_tags_as_table(
    tags_list: Sequence[str],
    *,
    file_hash: Optional[str] = None,
    store: Optional[str] = None,
    service_name: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    item_title: Optional[str] = None,
    path: Optional[str] = None,
    subject: Any = None,
    quiet: bool = False,
) -> None:
    """Render a tag list as a detail view/table and persist it for `@N` selection."""
    tags = [str(t) for t in (tags_list or []) if t is not None]

    payload: Dict[str, Any] = dict(subject) if isinstance(subject, dict) else {}
    payload["tag"] = tags
    payload.setdefault("tags", list(tags))
    if file_hash:
        payload["hash"] = file_hash
    if store:
        payload["store"] = store
    if service_name:
        payload["service_name"] = service_name
    if item_title:
        payload.setdefault("title", item_title)
    if path:
        payload.setdefault("path", path)
    extra = dict(payload.get("extra") or {})
    extra["tag"] = tags
    payload["extra"] = extra

    title = f"Tags: {item_title}" if item_title else "Tags"
    sh.display_and_persist_items(
        [payload],
        title=title,
        subject=subject if subject is not None else payload,
        display_type="custom" if quiet else "tag",
    )


def _finalize_pipeline_progress() -> None:
    """Ensure the pipeline UI shows the stage as complete."""
    try:
        progress = PipelineProgress(ctx)
        progress.clear_status()
        progress.set_percent(100)
    except Exception:
        pass


def _extract_scrapable_identifiers(tags_list: List[str]) -> Dict[str, str]:
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


def _extract_tag_value(tags_list: List[str], namespace: str) -> Optional[str]:
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


def _scrape_openlibrary_metadata(olid: str) -> List[str]:
    try:
        return list(_mp().scrape_openlibrary_metadata(olid))
    except Exception as e:
        log(f"OpenLibrary scraping error: {e}", file=sys.stderr)
        return []


def _scrape_isbn_metadata(isbn: str) -> List[str]:
    try:
        return list(_mp().scrape_isbn_metadata(isbn))
    except Exception as e:
        log(f"ISBN scraping error: {e}", file=sys.stderr)
        return []


def _perform_scraping(tags_list: List[str]) -> List[str]:
    """Perform scraping based on identifiers in tags.

    Priority order:
    1. openlibrary: (preferred - more complete metadata)
    2. isbn_10 or isbn (fallback)
    """
    identifiers = _extract_scrapable_identifiers(tags_list)

    if not identifiers:
        return []

    new_tags = []

    if "openlibrary" in identifiers:
        olid = identifiers["openlibrary"]
        if olid:
            new_tags.extend(_scrape_openlibrary_metadata(olid))
    has_scraped_title = any(str(t).lower().startswith("title:") for t in new_tags)
    if not has_scraped_title and (
        "isbn_13" in identifiers or "isbn_10" in identifiers or "isbn" in identifiers
    ):
        isbn = identifiers.get("isbn_13") or identifiers.get(
            "isbn_10"
        ) or identifiers.get("isbn")
        if isbn:
            new_tags.extend(_scrape_isbn_metadata(isbn))

    existing_tags_lower = {tag.lower()
                           for tag in tags_list}
    scraped_unique = []
    seen = set()
    for tag in new_tags:
        tag_lower = tag.lower()
        if tag_lower not in existing_tags_lower and tag_lower not in seen:
            scraped_unique.append(tag)
            seen.add(tag_lower)

    if scraped_unique:
        log(f"Added {len(scraped_unique)} new tag(s) from scraping")

    return scraped_unique


def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Get tags from Hydrus, local sidecar, or URL metadata.

    Usage:
        metadata -get [-query "hash:<sha256>"] [--instance <key>] [--emit]
        metadata -get -scrape <url|provider>

    Options:
        -query "hash:<sha256>": Override hash to use instead of result's hash
        --instance <key>: Store result to this key for pipeline
        --emit: Emit result without interactive prompt (quiet mode)
        -scrape <url|provider>: Scrape metadata from URL or provider name (itunes, musicbrainz, imdb, openlibrary, googlebooks, isbnsearch, ytdlp, tidal)
    """
    try:
        return _run_impl(result, args, config)
    finally:
        _finalize_pipeline_progress()


def _run_impl(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Internal implementation details for metadata -get."""
    emit_mode = False
    is_store_backed = False
    args_list = [str(arg) for arg in (args or [])]

    # Support numeric selection tokens (e.g., "@1" leading to argument "1") without treating
    # them as hash overrides. This lets users pick from the most recent table overlay/results.
    if len(args_list) == 1:
        token = args_list[0]
        if not token.startswith("-") and token.isdigit():
            try:
                idx = int(token) - 1
                items_pool = ctx.get_last_result_items()
                if 0 <= idx < len(items_pool):
                    result = items_pool[idx]
                    args_list = []
                    debug(
                        f"[get_tag] Resolved numeric selection arg {token} -> last_result_items[{idx}]"
                    )
                else:
                    debug(
                        f"[get_tag] Numeric selection arg {token} out of range (items={len(items_pool)})"
                    )
            except Exception as exc:
                debug(
                    f"[get_tag] Failed to resolve numeric selection arg {token}: {exc}"
                )

    # Parse arguments using shared parser
    parsed_args = parse_cmdlet_args(args_list, Get_Tag(register_cmdlet=False))

    # Detect if -scrape flag was provided without a value (parse_cmdlet_args skips missing values)
    scrape_flag_present = any(
        str(arg).lower() in {"-scrape",
                             "--scrape"} for arg in args_list
    )

    # Extract values
    query_raw = parsed_args.get("query")
    hash_override, query_valid = sh.require_single_hash_query(
        query_raw,
        "Invalid -query value (expected hash:<sha256>)",
        log_file=sys.stderr,
    )
    if not query_valid:
        return 1
    store_key = parsed_args.get("instance") or parsed_args.get("store")
    emit_requested = parsed_args.get("emit", False)
    
    # Only use emit mode if explicitly requested with --emit flag, not just because we're in a pipeline
    # This allows interactive REPL to work even in pipelines
    emit_mode = emit_requested or bool(store_key)
    store_label = store_key.strip() if store_key and store_key.strip() else None

    # Handle @N selection which creates a list - extract the first item
    if isinstance(result, list) and len(result) > 0:
        result = result[0]

    try:
        display_subject = ctx.get_last_result_subject()
    except Exception:
        display_subject = None

    def _resolve_subject_value(*keys: str) -> Any:
        for key in keys:
            val = get_field(result, key, None)
            if sh.value_has_content(val):
                return val
        if display_subject is None:
            return None
        for key in keys:
            val = get_field(display_subject, key, None)
            if sh.value_has_content(val):
                return val
        return None

    # Resolve core identity early so it's available for all branches
    hash_from_result = normalize_hash(_resolve_subject_value("hash"))
    file_hash = hash_override or hash_from_result
    
    store_value = _resolve_subject_value("store")
    store_name = (store_key or str(store_value).strip()) if store_value is not None else store_key
    
    subject_path = _resolve_subject_value("path", "target", "filename")
    item_title = _resolve_subject_value("title", "name", "filename")

    # Identify if the subject is store-backed. If so, we prioritize fresh data over cached tags.
    # Note: PATH, URL, and LOCAL stores are transient and don't support backend get-tag refreshes.
    is_store_backed = bool(file_hash and store_name and 
                           str(store_name).upper() not in {"PATH", "URL", "LOCAL"})

    scrape_url = parsed_args.get("scrape")
    scrape_requested = scrape_flag_present or scrape_url is not None

    # Handle URL or metadata-plugin scraping mode.
    if scrape_requested:
        import json as json_module

        scrape_target = str(scrape_url or "").strip() if scrape_url is not None else ""
        plugin = None
        mp = _mp()
        if mp is None:
            log("metadata+ plugin is not installed", file=sys.stderr)
            return 1
        if scrape_target.startswith(("http://", "https://")):
            plugin = mp.get_metadata_plugin_for_url(scrape_target, config)
            if plugin is None:
                log("No metadata plugin can scrape this URL", file=sys.stderr)
                return 1
            payload = plugin.scrape_url_payload(scrape_target)
            if not isinstance(payload, dict):
                log(f"No metadata extracted from URL via {plugin.name}", file=sys.stderr)
                return 1
            print(json_module.dumps(payload, ensure_ascii=False))
            return 0

        if scrape_target:
            plugin = mp.get_metadata_plugin(scrape_target, config)
        else:
            plugin = mp.get_default_subject_scrape_plugin(config)
        if plugin is None:
            if scrape_target:
                log(f"Unknown metadata plugin: {scrape_target}", file=sys.stderr)
            else:
                log("No default metadata plugin is available for subject scraping", file=sys.stderr)
            return 1

        backend = None
        if is_store_backed:
            try:
                from PluginCore.backend_registry import BackendRegistry

                storage = BackendRegistry(config, suppress_debug=True)
                backend = storage[str(store_name)]
            except Exception:
                backend = None

        # Prefer identifier tags (ISBN/OLID/etc.) when available; fallback to title/filename.
        # IMPORTANT: do not rely on `result.tag` for this because it can be stale (cached on
        # the piped PipeObject). Always prefer the current store-backed tags when possible.
        identifier_tags: List[str] = []
        file_hash_for_scrape = normalize_hash(hash_override) or normalize_hash(
            get_field(result,
                      "hash",
                      None)
        )
        store_for_scrape = get_field(result, "store", None)
        if file_hash_for_scrape and store_for_scrape:
            try:
                from PluginCore.backend_registry import BackendRegistry

                storage = BackendRegistry(config, suppress_debug=True)
                backend = storage[str(store_for_scrape)]
                current_tags, _src = backend.get_tag(file_hash_for_scrape, config=config)
                if isinstance(current_tags, (list, tuple, set)) and current_tags:
                    identifier_tags = [
                        str(t) for t in current_tags if isinstance(t, (str, bytes))
                    ]
            except Exception:
                # Fall back to whatever is present on the piped result if store lookup fails.
                pass

        # Fall back to tags carried on the result (may be stale).
        if not identifier_tags:
            result_tags = get_field(result, "tag", None)
            if isinstance(result_tags, list):
                identifier_tags = [
                    str(t) for t in result_tags if isinstance(t, (str, bytes))
                ]

        # As a last resort, try local sidecar only when the item is not store-backed.
        if not identifier_tags and (not file_hash_for_scrape or not store_for_scrape):
            file_path = (
                get_field(result,
                          "target",
                          None) or get_field(result,
                                             "path",
                                             None)
                or get_field(result,
                             "filename",
                             None)
            )
            if (isinstance(file_path,
                           str) and file_path and not file_path.lower().startswith(
                               ("http://",
                                "https://"))):
                pass

        title_from_tags = _extract_tag_value(identifier_tags, "title")
        artist_from_tags = _extract_tag_value(identifier_tags, "artist")

        identifiers = _extract_scrapable_identifiers(identifier_tags)
        identifier_query: Optional[str] = None
        if identifiers:
            try:
                identifier_query = plugin.identifier_query(identifiers)
            except Exception:
                identifier_query = None

        # Determine query from identifier first, else title on the result or filename
        title_hint = (
            title_from_tags or get_field(result,
                                         "title",
                                         None) or get_field(result,
                                                            "name",
                                                            None)
        )
        if not title_hint:
            file_path = get_field(result,
                                  "path",
                                  None) or get_field(result,
                                                     "filename",
                                                     None)
            if file_path:
                title_hint = Path(str(file_path)).stem
        artist_hint = (
            artist_from_tags or get_field(result,
                                          "artist",
                                          None) or get_field(result,
                                                             "uploader",
                                                             None)
        )
        if not artist_hint:
            meta_field = get_field(result, "metadata", None)
            if isinstance(meta_field, dict):
                meta_artist = meta_field.get("artist") or meta_field.get("uploader")
                if meta_artist:
                    artist_hint = str(meta_artist)

        combined_query: Optional[str] = None
        if not identifier_query and title_hint and artist_hint:
            try:
                combined_query = plugin.combined_query(
                    title_hint=str(title_hint),
                    artist_hint=str(artist_hint),
                )
            except Exception:
                combined_query = None

        resolved_subject_query: Optional[str] = None
        try:
            resolved_subject_query = plugin.resolve_subject_query(
                result,
                get_field,
                backend=backend,
                file_hash=file_hash_for_scrape,
            )
        except Exception:
            resolved_subject_query = None

        query_hint = resolved_subject_query or identifier_query or combined_query or title_hint
        if not query_hint:
            log(
                f"No query could be resolved for metadata plugin '{plugin.name}'",
                file=sys.stderr
            )
            return 1

        if identifier_query:
            log(f"Using identifier for metadata search: {identifier_query}")
        elif combined_query:
            log(f"Using title+artist for metadata search: {title_hint} - {artist_hint}")
        else:
            log(f"Using title for metadata search: {query_hint}")

        items = plugin.search(query_hint, limit=10)
        if not items:
            log("No metadata results found", file=sys.stderr)
            return 1

        # Some providers emit tags directly instead of presenting a metadata selection table.
        emit_direct = False
        try:
            emit_direct = bool(plugin.emits_direct_tags())
        except Exception:
            emit_direct = False
        if emit_direct:
            try:
                tags = [str(t) for t in plugin.to_tags(items[0]) if t is not None]
            except Exception:
                tags = []
            tags = _dedup_tags_preserve_order(tags)
            if not tags:
                log(f"No tags extracted from {plugin.name} metadata", file=sys.stderr)
                return 1

            overwrite_store = False
            try:
                overwrite_store = bool(is_store_backed and plugin.prefers_store_tag_overwrite())
            except Exception:
                overwrite_store = False

            if overwrite_store:
                if backend is None or not file_hash or not store_name:
                    log(
                        f"Failed to resolve store backend for provider '{plugin.name}'",
                        file=sys.stderr,
                    )
                    return 1

                try:
                    existing_tags, _src = backend.get_tag(file_hash, config=config)
                except Exception:
                    existing_tags = []
                try:
                    if existing_tags:
                        backend.delete_tag(file_hash, list(existing_tags), config=config)
                except Exception as exc:
                    debug(f"[get_tag] {plugin.name} overwrite delete_tag failed: {exc}")
                try:
                    backend.add_tag(file_hash, list(tags), config=config)
                except Exception as exc:
                    log(f"Failed to apply {plugin.name} tags: {exc}", file=sys.stderr)
                    return 1

                try:
                    updated_tags, _src = backend.get_tag(file_hash, config=config)
                except Exception:
                    updated_tags = tags
                if not updated_tags:
                    updated_tags = tags

                _emit_tags_as_table(
                    tags_list=list(updated_tags),
                    file_hash=file_hash,
                    store=str(store_name),
                    service_name=None,
                    config=config,
                    item_title=str(item_title or plugin.name),
                    path=str(subject_path) if subject_path else None,
                    subject={
                        "hash": file_hash,
                        "store": str(store_name),
                        "path": str(subject_path) if subject_path else None,
                        "title": item_title,
                        "extra": {
                            "applied_provider": plugin.name,
                            "scrape_url": str(query_hint),
                        },
                    },
                    quiet=emit_mode,
                )
                return 0

            _emit_tags_as_table(
                tags_list=list(tags),
                file_hash=None,
                store="url",
                service_name=None,
                config=config,
                item_title=str(items[0].get("title") or plugin.name),
                path=None,
                subject={
                    "plugin": plugin.name,
                    "url": str(query_hint)
                },
                quiet=emit_mode,
            )
            return 0

        from SYS.result_table import Table

        table = Table(f"Metadata: {plugin.name}")
        table.set_table(f"metadata.{plugin.name}")
        table.set_source_command("metadata", ["-get"])
        selection_payload = []
        hash_for_payload = normalize_hash(hash_override) or normalize_hash(
            get_field(result,
                      "hash",
                      None)
        )
        store_for_payload = get_field(result, "store", None)
        # Preserve a consistent path field when present so selecting a metadata row
        # keeps referring to the original file.
        path_for_payload = (
            get_field(result,
                      "path",
                      None) or get_field(result,
                                         "target",
                                         None) or get_field(result,
                                                            "filename",
                                                            None)
        )
        for idx, item in enumerate(items):
            tags = plugin.filter_tags_for_selection(plugin.to_tags(item))
            add_row_columns(
                table,
                [
                    ("Title", item.get("title", "")),
                    ("Artist", item.get("artist", "")),
                    ("Album", item.get("album", "")),
                    ("Year", item.get("year", "")),
                ],
            )
            payload = {
                "tag": tags,
                "plugin": plugin.name,
                "title": item.get("title"),
                "artist": item.get("artist"),
                "album": item.get("album"),
                "year": item.get("year"),
                "hash": hash_for_payload,
                "store": store_for_payload,
                "path": path_for_payload,
                "extra": {
                    "tag": tags,
                    "plugin": plugin.name,
                },
            }
            selection_payload.append(payload)
            table.set_row_selection_args(idx, [str(idx + 1)])

        # Store an overlay so that a subsequent `@N` selects from THIS metadata table,
        # not from the previous searchable table.
        publish_result_table(ctx, table, selection_payload, overlay=True)
        ctx.set_current_stage_table(table)
        return 0

    # If -scrape was requested but no URL, that's an error
    if scrape_requested and not scrape_url:
        log("-scrape requires a URL argument", file=sys.stderr)
        return 1

    # If the current result already carries a tag list (e.g. a selected metadata
    # row from get-tag -scrape itunes), APPLY those tags to the file in the store.
    result_provider = get_field(result, "plugin", None)
    result_tags = get_field(result, "tag", None)
    
    if result_provider and isinstance(result_tags, list) and result_tags:
        if not file_hash or not store_name:
            log(
                "Selected metadata row is missing hash/store; cannot apply tags",
                file=sys.stderr
            )
            _emit_tags_as_table(
                tags_list=[str(t) for t in result_tags if t is not None],
                file_hash=file_hash,
                store=str(store_name or "local"),
                service_name=None,
                config=config,
                item_title=str(get_field(result,
                                         "title",
                                         None) or result_provider),
                path=str(subject_path) if subject_path else None,
                subject=result,
                quiet=emit_mode,
            )
            _emit_tag_payload(
                str(result_provider),
                [str(t) for t in result_tags if t is not None],
                hash_value=file_hash,
            )
            return 0

        mp = _mp()
        plugin_for_apply = mp.get_metadata_plugin(str(result_provider), config) if mp is not None else None
        if plugin_for_apply is not None:
            apply_tags = plugin_for_apply.filter_tags_for_store_apply(
                [str(t) for t in result_tags if t is not None]
            )
        elif mp is not None:
            apply_tags = mp._filter_default_scraped_tags(
                [str(t) for t in result_tags if t is not None]
            )
        else:
            apply_tags = [str(t) for t in result_tags if t is not None]
        if not apply_tags:
            log(
                "No applicable scraped tags to apply (title:/artist:/source: are skipped)",
                file=sys.stderr,
            )
            return 0
        try:
            from PluginCore.backend_registry import BackendRegistry

            storage = BackendRegistry(config, suppress_debug=True)
            backend = storage[str(store_name)]
            ok = bool(backend.add_tag(file_hash, apply_tags, config=config))
            if not ok:
                log(f"Failed to apply tags to store '{store_name}'", file=sys.stderr)
        except Exception as exc:
            log(f"Failed to apply tags: {exc}", file=sys.stderr)
            return 1

        # Show updated tags after applying.
        try:
            updated_tags, _src = backend.get_tag(file_hash, config=config)
        except Exception:
            updated_tags = apply_tags
        if not updated_tags:
            updated_tags = apply_tags

        _emit_tags_as_table(
            tags_list=list(updated_tags),
            file_hash=file_hash,
            store=str(store_name),
            service_name=None,
            config=config,
            item_title=str(
                get_field(result,
                          "title",
                          None) or get_field(result,
                                             "name",
                                             None) or str(result_provider)
            ),
            path=str(subject_path) if subject_path else None,
            subject={
                "hash": file_hash,
                "store": str(store_name),
                "path": str(subject_path) if subject_path else None,
                "title": get_field(result,
                                   "title",
                                   None) or get_field(result,
                                                      "name",
                                                      None),
                "extra": {
                    "applied_provider": str(result_provider)
                },
            },
            quiet=emit_mode,
        )
        _emit_tag_payload(
            str(store_name),
            list(updated_tags),
            hash_value=file_hash,
            extra={"applied_provider": str(result_provider)},
        )
        return 0

    if not file_hash:
        log("No hash available in result", file=sys.stderr)
        return 1

    if not store_name:
        log("No store specified in result", file=sys.stderr)
        return 1

    subject_store = store_name
    subject_path_value = (
        _resolve_subject_value("path", "target", "filename")
    )
    subject_path = None
    if subject_path_value is not None:
        try:
            subject_path = str(subject_path_value)
        except Exception:
            subject_path = None

    service_name = ""
    subject_payload_base: Dict[str, Any] = {
        "tag": [],
        "title": item_title,
        "name": item_title,
        "store": subject_store,
        "service_name": service_name,
        "extra": {
            "tag": [],
        },
    }
    if file_hash:
        subject_payload_base["hash"] = file_hash
    if subject_path:
        subject_payload_base["path"] = subject_path

    def _subject_payload_with(
        tags: Sequence[str],
        service_name_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = dict(subject_payload_base)
        payload["tag"] = list(tags)
        extra = {"tag": list(tags)}
        payload["extra"] = extra
        if service_name_override is not None:
            payload["service_name"] = service_name_override
        return payload

    raw_result_tags = _resolve_subject_value("tag", "tags")
    display_tags: List[str] = []
    if isinstance(raw_result_tags, list):
        display_tags = [str(t) for t in raw_result_tags if t is not None]
        
    # Only use cached tags if the item is NOT store-backed.
    # For store-backed items (Hydrus/Folders), we want the latest state.
    if display_tags and not emit_mode and not is_store_backed:
        subject_payload = _subject_payload_with(display_tags)
        # Merge the full result object into subject_payload so all original metadata is preserved
        if isinstance(result, dict):
            for key, value in result.items():
                if key not in subject_payload and not key.startswith("_"):
                    subject_payload[key] = value
        _emit_tags_as_table(
            display_tags,
            file_hash=file_hash,
            store=str(subject_store),
            service_name=None,
            config=config,
            item_title=item_title,
            path=subject_path,
            subject=subject_payload,
            quiet=emit_mode,
        )
        return 0

    # Get tags using storage backend
    try:
        from PluginCore.backend_registry import BackendRegistry

        storage = BackendRegistry(config, suppress_debug=True)
        backend = storage[store_name]
        current, source = backend.get_tag(file_hash, config=config)
        current = list(current or [])

        service_name = ""
    except KeyError:
        log(f"Store '{store_name}' not found", file=sys.stderr)
        return 1
    except Exception as exc:
        log(f"Failed to get tags: {exc}", file=sys.stderr)
        return 1

    subject_payload = _subject_payload_with(
        current,
        service_name if source == "hydrus" else None,
    )
    # Merge the full result object into subject_payload so all original metadata is preserved
    # (e.g., url, source_url, etc. from search results)
    if isinstance(result, dict):
        for key, value in result.items():
            if key not in subject_payload and not key.startswith("_"):
                subject_payload[key] = value
    _emit_tags_as_table(
        current,
        file_hash=file_hash,
        store=str(subject_store),
        service_name=service_name if source == "hydrus" else None,
        config=config,
        item_title=item_title,
        path=subject_path,
        subject=subject_payload,
        quiet=emit_mode,
    )

    # If emit requested or store key provided, emit payload
    if emit_mode:
        _emit_tag_payload(
            source,
            current,
            hash_value=file_hash,
            store_label=store_label
        )

    return 0


_SCRAPE_CHOICES = [
    "itunes",
    "openlibrary",
    "googlebooks",
    "google",
    "isbnsearch",
    "musicbrainz",
    "imdb",
    "ytdlp",
    "tidal",
]


class Get_Tag(Cmdlet):
    """Class-based metadata -get tag handler with self-registration."""

    def __init__(self, *, register_cmdlet: bool = True) -> None:
        """Initialize metadata -get handler."""
        super().__init__(
            name="tag",
            summary="Get tag values from Hydrus or local sidecar metadata",
            usage=
            'metadata -get [-query "hash:<sha256>"] [--instance <key>] [--emit] [-scrape <url|plugin>]',
            alias=[],
            arg=[
                SharedArgs.QUERY,
                CmdletArg(
                    name="-instance",
                    type="string",
                    description="Store result to this key for pipeline",
                    alias="store",
                ),
                CmdletArg(
                    name="-emit",
                    type="flag",
                    description="Emit result without interactive prompt (quiet mode)",
                    alias="emit-only",
                ),
                CmdletArg(
                    name="-scrape",
                    type="string",
                    description=
                    "Scrape metadata from a URL or plugin; with no value, use the default subject-scrape plugin",
                    required=False,
                    choices=_SCRAPE_CHOICES,
                ),
            ],
            detail=[
                "- Retrieves tags for a file from:",
                "    Hydrus: Using file hash if available",
                "    Local: From sidecar files or local library database",
                "- Options:",
                '    -query: Override hash to look up in Hydrus (use: -query "hash:<sha256>")',
                "    -instance: Store result to key for downstream pipeline",
                "    -emit: Quiet mode (no interactive selection)",
                "    -scrape: Scrape metadata from URL or metadata plugin",
            ],
            exec=self.run,
        )
        if register_cmdlet:
            self.register()

    def run(self, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        """Execute metadata -get."""
        return _run(result, args, config)


CMDLET = Get_Tag(register_cmdlet=False)


