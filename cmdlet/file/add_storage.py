"""Storage operations, duplicate checking, and emission for add-file."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple
from pathlib import Path
import sys
import shutil
import traceback

from SYS import models
from SYS import pipeline as ctx
from SYS.logger import log, debug, debug_panel
from SYS.pipeline_progress import PipelineProgress
from SYS.result_publication import publish_result_table, overlay_existing_result_table
from PluginCore.backend_registry import BackendRegistry

from .. import _shared as sh

# Import from add_core (safe: add_core defines these before importing this module)
from .add_core import (
    Add_File,
    _CommandDependencies,
)

# Import module-level function from add_tagging
from .add_tagging import _maybe_apply_florencevision_tags

get_field = sh.get_field


def _update_pipe_object_destination(
    pipe_obj: models.PipeObject,
    *,
    hash_value: str,
    store: str,
    plugin: Optional[str] = None,
    path: Optional[str],
    tag: List[str],
    title: Optional[str],
    extra_updates: Optional[Dict[str, Any]] = None,
) -> None:
    pipe_obj.hash = hash_value
    pipe_obj.store = store
    pipe_obj.plugin = plugin
    pipe_obj.is_temp = False
    pipe_obj.path = path
    pipe_obj.tag = tag
    if title:
        pipe_obj.title = title
    merged = dict(extra_updates or {})
    merged["store"] = store
    if plugin:
        merged["plugin"] = plugin
    if path:
        merged["path"] = path
    if isinstance(pipe_obj.extra, dict):
        pipe_obj.extra.update(merged)
    else:
        pipe_obj.extra = merged


def _stop_live_progress_for_terminal_render() -> None:
    try:
        live_progress = ctx.get_live_progress()
    except Exception:
        live_progress = None

    if live_progress is None:
        return

    try:
        stage_ctx = ctx.get_stage_context()
        pipe_idx = getattr(stage_ctx, "pipe_index", None)
        if isinstance(pipe_idx, int):
            live_progress.finish_pipe(int(pipe_idx), force_complete=True)
    except Exception:
        pass

    try:
        live_progress.stop()
    except Exception:
        pass

    try:
        if hasattr(ctx, "set_live_progress"):
            ctx.set_live_progress(None)
    except Exception:
        pass


def _emit_pipe_object(pipe_obj: models.PipeObject) -> None:
    payload = pipe_obj.to_dict()
    ctx.emit(payload)
    ctx.set_current_stage_table(None)

    stage_ctx = ctx.get_stage_context()
    is_last = (stage_ctx is None) or bool(getattr(stage_ctx, "is_last_stage", False))
    if not is_last:
        return

    try:
        _stop_live_progress_for_terminal_render()
        from .._shared import display_and_persist_items

        display_and_persist_items([payload], title="Result", subject=payload)
    except Exception:
        pass


def _emit_storage_result(
    payload: Dict[str, Any],
    *,
    overlay: bool = True,
    emit: bool = True
) -> None:
    """Emit a storage-style result payload.

    - Always emits the dict downstream (when in a pipeline).
    - If this is the last stage (or not in a pipeline), prints a search-file-like table
      and sets an overlay table/items for @N selection.
    """
    if emit:
        ctx.emit(payload)

    stage_ctx = ctx.get_stage_context()
    is_last = (stage_ctx is None) or bool(getattr(stage_ctx, "is_last_stage", False))
    if not is_last or not overlay:
        return

    try:
        from SYS.result_table import Table

        table = Table("Result")
        table.add_result(payload)
        publish_result_table(ctx, table, [payload], subject=payload, overlay=True)
    except Exception:
        try:
            ctx.set_last_result_items_only([payload])
        except Exception:
            pass


def _try_emit_search_file_by_hash(
    *,
    instance: str,
    hash_value: str,
    config: Dict[str, Any]
) -> Optional[List[Any]]:
    """Run search-file for a single hash so the final table/payload is consistent."""
    try:
        from cmdlet.file.search import CMDLET as search_file_cmdlet

        args = ["-instance", str(instance), f"hash:{str(hash_value)}"]

        prev_ctx = ctx.get_stage_context()
        temp_ctx = ctx.PipelineStageContext(
            stage_index=0,
            total_stages=1,
            pipe_index=0,
            worker_id=getattr(prev_ctx, "worker_id", None),
        )
        ctx.set_stage_context(temp_ctx)
        try:
            code = search_file_cmdlet.run(None, args, config)
            emitted_items = list(getattr(temp_ctx, "emits", []) or [])
        finally:
            ctx.set_stage_context(prev_ctx)
        if code != 0:
            return None

        stage_ctx = ctx.get_stage_context()
        is_last = (stage_ctx is None) or bool(getattr(stage_ctx, "is_last_stage", False))
        if is_last:
            try:
                overlay_existing_result_table(
                    ctx,
                    subject={
                        "store": instance,
                        "hash": hash_value
                    },
                )
            except Exception:
                pass

        return emitted_items
    except Exception as exc:
        debug(
            f"[add-file] Failed to run search-file after add-file: {type(exc).__name__}: {exc}"
        )
        return None


def _try_emit_search_file_by_hashes(
    *,
    instance: str,
    hash_values: List[str],
    config: Dict[str, Any],
    store_instance: Optional[BackendRegistry] = None,
) -> Optional[List[Any]]:
    """Run search-file for a list of hashes and promote the table to a display overlay.

    Returns the emitted search-file payload items on success, else None.
    """
    hashes = [h for h in (hash_values or []) if isinstance(h, str) and len(h) == 64]
    if not instance or not hashes:
        return None

    try:
        from cmdlet.file.search import CMDLET as search_file_cmdlet

        query = "hash:" + ",".join(hashes)
        args = ["-instance", str(instance), "-internal-refresh", query]
        debug(f'[add-file] Refresh: search-file -instance {instance} "{query}"')

        prev_ctx = ctx.get_stage_context()
        temp_ctx = ctx.PipelineStageContext(
            stage_index=0,
            total_stages=1,
            pipe_index=0,
            worker_id=getattr(prev_ctx, "worker_id", None),
        )
        ctx.set_stage_context(temp_ctx)
        try:
            code = search_file_cmdlet.run(None, args, config)
            emitted_items = list(getattr(temp_ctx, "emits", []) or [])
        finally:
            ctx.set_stage_context(prev_ctx)

        if code != 0:
            return None

        stage_ctx = ctx.get_stage_context()
        is_last = (stage_ctx is None) or bool(getattr(stage_ctx, "is_last_stage", False))
        if is_last:
            try:
                table = ctx.get_last_result_table()
                items = ctx.get_last_result_items()
                if table is not None and items:
                    if len(items) == 1:
                        try:
                            from SYS.rich_display import render_item_details_panel
                            render_item_details_panel(items[0])
                            setattr(table, "_rendered_by_cmdlet", True)
                        except Exception as exc:
                            debug(f"[add-file] Item details render failed: {exc}")

                    publish_result_table(
                        ctx,
                        table,
                        items,
                        subject={
                            "store": instance,
                            "hash": hashes
                        },
                        overlay=True,
                    )
            except Exception:
                pass

        return emitted_items
    except Exception as exc:
        debug(
            f"[add-file] Failed to run search-file after add-file: {type(exc).__name__}: {exc}"
        )
        return None


def _find_existing_hash_by_urls(
    backend: Any,
    urls: Sequence[str],
) -> Optional[str]:
    """Best-effort duplicate detection by URL before ingesting file bytes."""
    url_candidates: List[str] = []
    for raw in urls or []:
        text = str(raw or "").strip()
        if not text or not Add_File._is_probable_url(text):
            continue
        if text not in url_candidates:
            url_candidates.append(text)

    if not url_candidates:
        return None

    try:
        lookup_exact = getattr(backend, "find_hashes_by_url", None)
    except Exception:
        lookup_exact = None
    if callable(lookup_exact):
        for candidate_url in url_candidates:
            try:
                hashes = lookup_exact(candidate_url) or []
            except Exception:
                continue
            if not isinstance(hashes, (list, tuple, set)):
                continue
            for item in hashes:
                normalized = Add_File._normalize_hash_candidate(item)
                if normalized:
                    return normalized

    searcher = getattr(backend, "search", None)
    if callable(searcher):
        for candidate_url in url_candidates:
            try:
                hits = searcher(f"url:{candidate_url}", limit=1, minimal=True) or []
            except Exception:
                continue
            if not isinstance(hits, list) or not hits:
                continue
            hit = hits[0]
            for key in ("hash", "file_hash", "sha256"):
                normalized = Add_File._normalize_hash_candidate(get_field(hit, key))
                if normalized:
                    return normalized

    return None


def _emit_plugin_upload_payload(
    upload_payload: Dict[str, Any],
    plugin_name: str,
    instance_name: Optional[str],
    pipe_obj: models.PipeObject,
    media_path: Path,
    delete_after: bool,
) -> int:
    payload = dict(upload_payload or {})
    extra_updates: Dict[str, Any] = {}
    raw_extra = payload.get("extra")
    if isinstance(raw_extra, dict):
        extra_updates.update(raw_extra)

    if plugin_name:
        extra_updates.setdefault("plugin", plugin_name)
    if instance_name:
        extra_updates.setdefault("instance", instance_name)

    raw_urls = payload.get("url")
    if isinstance(raw_urls, str):
        url_values = [raw_urls.strip()] if raw_urls.strip() else []
        extra_updates["url"] = url_values
    elif isinstance(raw_urls, (list, tuple, set)):
        url_values = [str(item).strip() for item in raw_urls if str(item).strip()]
        extra_updates["url"] = url_values

    relationships = payload.get("relationships")
    if relationships:
        try:
            pipe_obj.relationships = relationships
        except Exception:
            pass

    tags = payload.get("tag")
    if isinstance(tags, list):
        tag_values = [str(tag) for tag in tags]
    else:
        tag_values = list(pipe_obj.tag or [])

    title_value = str(payload.get("title") or pipe_obj.title or media_path.name).strip() or media_path.name
    path_value = str(payload.get("path") or pipe_obj.path or media_path).strip()
    hash_value = str(
        payload.get("hash")
        or payload.get("file_hash")
        or getattr(pipe_obj, "hash", None)
        or "unknown"
    ).strip() or "unknown"
    store_value = str(payload.get("store") or "").strip()
    plugin_value = payload.get("plugin")
    if plugin_value is None and plugin_name:
        plugin_value = plugin_name

    _update_pipe_object_destination(
        pipe_obj,
        hash_value=hash_value,
        store=store_value,
        plugin=str(plugin_value) if plugin_value else None,
        path=path_value,
        tag=tag_values,
        title=title_value,
        extra_updates=extra_updates,
    )
    _emit_pipe_object(pipe_obj)

    _cleanup_after_success(media_path, delete_source=delete_after)
    return 0


def _handle_plugin_upload(
    media_path: Path,
    plugin_name: str,
    instance_name: Optional[str],
    pipe_obj: models.PipeObject,
    config: Dict[str, Any],
    delete_after: bool,
    folder_name: Optional[str] = None,
    write_metadata: bool = True,
) -> int:
    """Handle uploading via an add-file plugin (e.g. 0x0)."""
    from PluginCore.registry import (
        PLUGIN_REGISTRY,
        get_plugin_for_cmdlet,
        list_plugins_for_cmdlet,
    )

    try:
        file_provider = get_plugin_for_cmdlet(plugin_name, "add-file", config)
        if not file_provider:
            available_map = list_plugins_for_cmdlet("add-file", config)
            available_add_plugins = [n for n, e in available_map.items() if e]
            requested_plugin_info = PLUGIN_REGISTRY.get(plugin_name)

            if requested_plugin_info is not None and "add-file" in requested_plugin_info.supported_cmdlets:
                from SYS.rich_display import show_plugin_config_panel
                show_plugin_config_panel([requested_plugin_info.canonical_name], config)
            else:
                log(f"Add-file plugin '{plugin_name}' is not available or does not support add-file", file=sys.stderr)

            if available_add_plugins:
                from SYS.rich_display import show_available_plugins_panel
                show_available_plugins_panel(sorted(available_add_plugins))
            return 1

        upload_kwargs: Dict[str, Any] = {
            "pipe_obj": pipe_obj,
            "instance": instance_name,
        }
        pipeline_progress = PipelineProgress(ctx)
        normalized_plugin_name = Add_File._normalize_plugin_key(plugin_name)
        f_hash = Add_File._resolve_file_hash(None, media_path, pipe_obj, None)
        result = None
        tags, urls, title, f_hash = Add_File._prepare_metadata(result, media_path, pipe_obj, config)
        relationships = Add_File._get_relationships(result, pipe_obj)
        upload_kwargs.update(
            {
                "title": title,
                "tags": tags,
                "urls": urls,
                "hash_value": f_hash,
                "relationships": relationships,
                "pipeline_progress": pipeline_progress,
                "write_metadata": write_metadata,
            }
        )
        if normalized_plugin_name == "local":
            direct_export_download = False
            try:
                if isinstance(pipe_obj.extra, dict):
                    direct_export_download = bool(pipe_obj.extra.pop("_direct_export_download", False))
            except Exception:
                direct_export_download = False
            upload_kwargs["direct_export_download"] = direct_export_download
            if folder_name is not None and str(folder_name).strip():
                upload_kwargs["folder_name"] = str(folder_name).strip()

        upload_result = file_provider.upload(
            str(media_path),
            **upload_kwargs,
        )

        duplicate_upload = False
        duplicate_rule = ""
        duplicate_target = ""
        try:
            if isinstance(getattr(pipe_obj, "extra", None), dict):
                duplicate_upload = bool(pipe_obj.extra.get("upload_duplicate"))
                duplicate_rule = str(pipe_obj.extra.get("upload_duplicate_rule") or "").strip()
                duplicate_target = str(pipe_obj.extra.get("upload_duplicate_target") or "").strip()
        except Exception:
            duplicate_upload = False
            duplicate_rule = ""
            duplicate_target = ""

    except Exception as exc:
        log(f"Upload failed: {exc}", file=sys.stderr)
        return 1

    if isinstance(upload_result, dict):
        return _emit_plugin_upload_payload(
            upload_result,
            plugin_name,
            instance_name,
            pipe_obj,
            media_path,
            delete_after,
        )

    hoster_url = str(upload_result or "").strip()

    extra_updates: Dict[str, Any] = {
        "plugin": plugin_name,
        "instance": instance_name,
        "plugin_url": hoster_url,
        "store": instance_name or plugin_name,
    }
    if isinstance(pipe_obj.extra, dict):
        existing_known = list(pipe_obj.extra.get("url") or [])
        if hoster_url and hoster_url not in existing_known:
            existing_known.append(hoster_url)
        extra_updates["url"] = existing_known

    file_path = hoster_url or pipe_obj.path or (str(media_path) if media_path else None) or ""
    _update_pipe_object_destination(
        pipe_obj,
        hash_value=f_hash or "unknown",
        store=str(instance_name or plugin_name or "").strip(),
        plugin=plugin_name or None,
        path=file_path,
        tag=list(pipe_obj.tag or []),
        title=pipe_obj.title or (media_path.name if media_path else None),
        extra_updates=extra_updates,
    )
    _emit_pipe_object(pipe_obj)

    _cleanup_after_success(media_path, delete_source=delete_after)
    return 0


def _handle_storage_backend(
    result: Any,
    media_path: Path,
    backend_name: str,
    pipe_obj: models.PipeObject,
    config: Dict[str, Any],
    delete_after: bool,
    *,
    collect_payloads: Optional[List[Dict[str, Any]]] = None,
    collect_relationship_pairs: Optional[Dict[str, set[tuple[str, str]]]] = None,
    defer_url_association: bool = False,
    pending_url_associations: Optional[Dict[str, List[tuple[str, List[str]]]]] = None,
    defer_tag_association: bool = False,
    pending_tag_associations: Optional[Dict[str, List[tuple[str, List[str]]]]] = None,
    suppress_last_stage_overlay: bool = False,
    auto_search_file: bool = True,
    store_instance: Optional[BackendRegistry] = None,
) -> int:
    """Handle uploading to a registered storage backend (e.g., 'test' folder store, 'hydrus', etc.)."""
    pipeline_progress = PipelineProgress(ctx)

    def _set_status(text: str) -> None:
        try:
            pipeline_progress.set_status(f"{backend_name}: {text}")
        except Exception:
            pass

    def _clear_status() -> None:
        try:
            pipeline_progress.clear_status()
        except Exception:
            pass

    delete_after_effective = bool(delete_after)
    if not delete_after_effective:
        try:
            if (str(backend_name or "").strip().lower() != "temp"
                    and getattr(pipe_obj, "is_temp", False)
                    and getattr(pipe_obj, "action", None) == "cmdlet:download-media"):
                from SYS.config import resolve_output_dir

                temp_dir = resolve_output_dir(config)
                try:
                    if media_path.resolve().is_relative_to(
                            temp_dir.expanduser().resolve()):
                        delete_after_effective = True
                        debug(
                            f"[add-file] Auto-delete temp source after ingest: {media_path}"
                        )
                except Exception:
                    pass
        except Exception:
            pass

    try:
        backend_registry = store_instance if store_instance is not None else BackendRegistry(config)
        backend, backend_registry, backend_exc = sh.get_preferred_store_backend(
            config,
            backend_name,
            store_registry=backend_registry,
            suppress_debug=True,
        )
        if backend is None:
            raise backend_exc or KeyError(f"Unknown store backend: {backend_name}")

        is_remote_backend = getattr(backend, "is_remote", False)
        prefer_defer_tags = getattr(backend, "prefer_defer_tags", False)
        supports_url_association = bool(getattr(backend, "supports_url_association", False))
        supports_note_association = bool(getattr(backend, "supports_note_association", False))
        supports_relationship_association = bool(getattr(backend, "supports_relationship_association", False))

        tags, url, title, f_hash = Add_File._prepare_metadata(
            result, media_path, pipe_obj, config
        )

        try:
            from SYS.metadata import normalize_urls

            source_store = None
            source_hash = None
            if isinstance(result, dict):
                source_store = result.get("store")
                source_hash = result.get("hash")
            if not source_store:
                source_store = getattr(pipe_obj, "store", None)
            if not source_hash:
                source_hash = getattr(pipe_obj, "hash", None)
                if (not source_hash) and isinstance(pipe_obj.extra, dict):
                    source_hash = pipe_obj.extra.get("hash")

            source_store = str(source_store or "").strip()
            source_hash = str(source_hash or "").strip().lower()
            if (source_store and source_hash and len(source_hash) == 64
                    and source_store.lower() != str(backend_name or "").strip().lower()):
                source_backend = None
                try:
                    store = backend_registry
                    if source_store in store.list_backends():
                        source_backend = store[source_store]
                except Exception:
                    source_backend = None

                if source_backend is not None:
                    try:
                        src_urls = normalize_urls(
                            source_backend.get_url(source_hash) or []
                        )
                    except Exception:
                        src_urls = []

                    try:
                        dst_urls = normalize_urls(url or [])
                    except Exception:
                        dst_urls = []

                    merged: list[str] = []
                    seen: set[str] = set()
                    for u in list(dst_urls or []) + list(src_urls or []):
                        if not u:
                            continue
                        if u in seen:
                            continue
                        seen.add(u)
                        merged.append(u)
                    url = merged
        except Exception:
            pass

        if collect_relationship_pairs is not None and supports_relationship_association:
            rels = Add_File._get_relationships(result, pipe_obj)
            if isinstance(rels, dict) and rels:
                king_hash, alt_hashes = Add_File._parse_relationships_king_alts(rels)
                if king_hash and alt_hashes:
                    bucket = collect_relationship_pairs.setdefault(
                        str(backend_name),
                        set()
                    )
                    for alt_hash in alt_hashes:
                        if alt_hash and alt_hash != king_hash:
                            bucket.add((alt_hash, king_hash))

        if isinstance(tags, list) and tags:
            tags = [
                t for t in tags if not (
                    isinstance(t, str)
                    and t.strip().lower().startswith("relationship:")
                )
            ]

        try:
            tags = _maybe_apply_florencevision_tags(media_path, list(tags or []), config, pipe_obj=pipe_obj)
            pipe_obj.tag = list(tags or [])
        except Exception as exc:
            log(f"[add-file] FlorenceVision tagging error: {exc}", file=sys.stderr)
            return 1

        upload_tags = tags
        if prefer_defer_tags and upload_tags:
            upload_tags = []
        try:
            debug_panel(
                "add-file store",
                [
                    ("backend", backend_name),
                    ("path", media_path),
                    ("title", title),
                    ("hash_hint", f_hash[:12] if f_hash else "N/A"),
                    ("defer_tags", bool(prefer_defer_tags and tags)),
                ],
                border_style="yellow",
            )
        except Exception:
            pass

        duplicate_hash = Add_File._find_existing_hash_by_urls(backend, url)
        skip_upload = False
        local_hash = str(f_hash or "").strip().lower()
        dup_hash = str(duplicate_hash or "").strip().lower()
        if (
            duplicate_hash
            and local_hash
            and len(local_hash) == 64
            and dup_hash != local_hash
        ):
            debug(
                f"[add-file] URL exists as {dup_hash[:12]} in '{backend_name}' "
                f"but local file hash is {local_hash[:12]}; uploading new bytes"
            )
            duplicate_hash = None
        if duplicate_hash:
            has_file = True
            checker = getattr(backend, "has_file", None)
            if callable(checker):
                try:
                    has_file = bool(checker(duplicate_hash))
                except Exception:
                    has_file = False
            if has_file:
                debug(
                    f"[add-file] URL duplicate detected in '{backend_name}', skipping upload and reusing hash {duplicate_hash[:12]}..."
                )
                file_identifier = duplicate_hash
                skip_upload = True
            else:
                debug(
                    f"[add-file] URL points at hash {duplicate_hash[:12]} in '{backend_name}' but file bytes are missing; uploading from source"
                )

        if not skip_upload:
            file_identifier = backend.add_file(
                media_path,
                title=title,
                tag=upload_tags,
                url=[] if ((defer_url_association and url) or (not supports_url_association)) else url,
                file_hash=f_hash,
                pipeline_progress=pipeline_progress,
                transfer_label=title or media_path.name,
            )

        stored_path: Optional[str] = None
        try:
            if not is_remote_backend:
                maybe_path = backend.get_file(file_identifier)
                if isinstance(maybe_path, Path):
                    stored_path = str(maybe_path)
                elif isinstance(maybe_path, str) and maybe_path:
                    stored_path = maybe_path
        except Exception:
            stored_path = None

        if isinstance(file_identifier, str) and len(file_identifier) == 64:
            chosen_hash = file_identifier
        else:
            chosen_hash = f_hash or (str(file_identifier) if file_identifier is not None else "unknown")

        _update_pipe_object_destination(
            pipe_obj,
            hash_value=chosen_hash,
            store=backend_name,
            path=stored_path,
            tag=tags,
            title=title or pipe_obj.title or media_path.name,
            extra_updates={
                "url": url,
            },
        )

        resolved_hash = chosen_hash

        if prefer_defer_tags and tags:
            if defer_tag_association and pending_tag_associations is not None:
                try:
                    pending_tag_associations.setdefault(str(backend_name), []).append((str(resolved_hash), list(tags)))
                except Exception:
                    pass
            else:
                try:
                    adder = getattr(backend, "add_tag", None)
                    if callable(adder):
                        _set_status("applying deferred tags")
                        adder(resolved_hash, list(tags))
                except Exception as exc:
                    log(f"[add-file] Post-upload tagging failed for {backend_name}: {exc}", file=sys.stderr)

        if url and supports_url_association:
            if defer_url_association and pending_url_associations is not None:
                try:
                    pending_url_associations.setdefault(
                        str(backend_name),
                        []
                    ).append((str(resolved_hash), list(url)))
                except Exception:
                    pass
            else:
                try:
                    is_folder_backend = getattr(backend, "STORE_TYPE", "") == "folder"
                    if not is_folder_backend:
                        _set_status("associating urls")
                        backend.add_url(resolved_hash, list(url))
                except Exception:
                    pass

        def _write_note(note_name: str, note_text: Optional[str]) -> None:
            if not note_text or not supports_note_association:
                return
            try:
                setter = getattr(backend, "set_note", None)
                if callable(setter):
                    _set_status(f"writing {note_name} note")
                    setter(resolved_hash, note_name, note_text)
            except Exception as exc:
                debug_panel(
                    "add-file note write failed",
                    [
                        ("store", backend_name),
                        ("hash", resolved_hash),
                        ("note", note_name),
                        ("error", exc),
                    ],
                    border_style="yellow",
                )

        _write_note("sub", Add_File._get_note_text(result, pipe_obj, "sub"))
        _write_note("lyric", Add_File._get_note_text(result, pipe_obj, "lyric"))
        _write_note("chapters", Add_File._get_note_text(result, pipe_obj, "chapters"))
        _write_note("caption", Add_File._get_note_text(result, pipe_obj, "caption"))
        _write_note("chat", Add_File._get_note_text(result, pipe_obj, "chat"))

        meta: Dict[str, Any] = {}
        try:
            is_folder_backend_meta = getattr(backend, "STORE_TYPE", "") == "folder"
            if not is_folder_backend_meta:
                _set_status("loading stored metadata")
                meta = backend.get_metadata(resolved_hash) or {}
        except Exception:
            meta = {}

        size_bytes: Optional[int] = None
        for key in ("size_bytes", "size", "filesize", "file_size"):
            try:
                raw_size = meta.get(key)
                if raw_size is not None:
                    val = int(raw_size)
                    if val > 0:
                        size_bytes = val
                        break
            except Exception:
                pass
        if not size_bytes:
            try:
                size_bytes = int(media_path.stat().st_size)
            except Exception:
                size_bytes = None

        title_out = (
            meta.get("title") or title or pipe_obj.title or media_path.stem
            or media_path.name
        )
        ext_out = meta.get("ext") or media_path.suffix.lstrip(".")

        payload: Dict[str, Any] = {
            "title": title_out,
            "ext": str(ext_out or ""),
            "size_bytes": size_bytes,
            "store": backend_name,
            "hash": resolved_hash,
            "path": stored_path,
            "tag": list(tags or []),
            "url": list(url or []),
        }
        if collect_payloads is not None:
            try:
                collect_payloads.append(payload)
            except Exception:
                pass

        if auto_search_file and resolved_hash and resolved_hash != "unknown":
            _emit_storage_result(
                payload,
                overlay=not suppress_last_stage_overlay,
                emit=False
            )

            refreshed_items = _try_emit_search_file_by_hash(
                instance=backend_name,
                hash_value=resolved_hash,
                config=config,
            )
            if refreshed_items:
                for emitted in refreshed_items:
                    ctx.emit(emitted)
            else:
                ctx.emit(payload)
        else:
            _emit_storage_result(
                payload,
                overlay=not suppress_last_stage_overlay,
                emit=True
            )

        try:
            _maybe_auto_tag_soulseek_audio(pipe_obj, resolved_hash, backend_name, config)
        except Exception:
            pass

        _cleanup_after_success(
            media_path,
            delete_source=delete_after_effective
        )
        _clear_status()
        return 0

    except Exception as exc:
        _clear_status()
        log(
            f"❌ Failed to add file to backend '{backend_name}': {exc}",
            file=sys.stderr
        )
        traceback.print_exc(file=sys.stderr)
        return 1


def _maybe_auto_tag_soulseek_audio(
    pipe_obj: models.PipeObject,
    resolved_hash: str,
    backend_name: str,
    config: Dict[str, Any],
) -> None:
    """Run `metadata -auto` enrichment after adding a Soulseek-sourced audio file."""
    try:
        tags = list(getattr(pipe_obj, "tag", None) or [])
        if not any(str(t).lower().startswith("source:soulseek") for t in tags):
            return
        payload: Dict[str, Any] = {
            "hash": resolved_hash,
            "store": backend_name,
            "tag": tags,
            "title": getattr(pipe_obj, "title", None) or "",
        }
        from cmdlet.metadata.auto_tag import Auto_Tag

        Auto_Tag().run(payload, [], config)
    except Exception as exc:
        debug(f"[add-file] Soulseek auto-tag skipped: {exc}")


def _apply_pending_url_associations(
    pending: Dict[str, List[tuple[str, List[str]]]],
    config: Dict[str, Any],
    store_instance: Optional[BackendRegistry] = None,
) -> None:
    """Apply deferred URL associations in bulk, grouped per backend."""

    try:
        backend_registry = store_instance if store_instance is not None else BackendRegistry(config)
    except Exception:
        return

    for backend_name, pairs in (pending or {}).items():
        if not pairs:
            continue
        try:
            backend, backend_registry, _exc = sh.get_store_backend(
                config,
                backend_name,
                store_registry=backend_registry,
            )
            if backend is None:
                continue

            if not bool(getattr(backend, "supports_url_association", False)):
                continue

            items = sh.coalesce_hash_value_pairs(pairs)
            if not items:
                continue

            bulk = getattr(backend, "add_url_bulk", None)
            if callable(bulk):
                try:
                    bulk(items)
                    continue
                except Exception:
                    pass

            single = getattr(backend, "add_url", None)
            if callable(single):
                for h, u in items:
                    try:
                        single(h, u)
                    except Exception:
                        continue
        except Exception:
            continue


def _apply_pending_tag_associations(
    pending: Dict[str, List[tuple[str, List[str]]]],
    config: Dict[str, Any],
    store_instance: Optional[BackendRegistry] = None,
) -> None:
    """Apply deferred tag associations in bulk, grouped per backend."""

    try:
        backend_registry = store_instance if store_instance is not None else BackendRegistry(config)
    except Exception:
        return

    sh.run_store_hash_value_batches(
        config,
        pending or {},
        bulk_method_name="add_tags_bulk",
        single_method_name="add_tag",
        store_registry=backend_registry,
        pass_config_to_bulk=False,
        pass_config_to_single=False,
    )


def _apply_pending_relationships(
    pending: Dict[str, set[tuple[str, str]]],
    config: Dict[str, Any],
    store_instance: Optional[BackendRegistry] = None,
    deps: Optional[_CommandDependencies] = None,
) -> None:
    """Persist relationships to backends that support relationships.

    This delegates to an optional backend method: `set_relationship(alt, king, kind)`.
    """
    if not pending:
        return

    if deps is None:
        deps = _CommandDependencies(config)

    try:
        backend_registry = store_instance if store_instance is not None else deps.get_backend_registry()
    except Exception:
        return

    for backend_name, pairs in pending.items():
        if not pairs:
            continue

        try:
            backend = backend_registry[str(backend_name)]
        except Exception:
            continue

        if not bool(getattr(backend, "supports_relationship_association", False)):
            continue

        setter = getattr(backend, "set_relationship", None)
        if not callable(setter):
            continue

        processed_pairs: set[tuple[str, str]] = set()
        for alt_hash, king_hash in sorted(pairs):
            if not alt_hash or not king_hash or alt_hash == king_hash:
                continue
            if (alt_hash, king_hash) in processed_pairs:
                continue
            alt_norm = str(alt_hash).strip().lower()
            king_norm = str(king_hash).strip().lower()
            if len(alt_norm) != 64 or len(king_norm) != 64:
                continue
            try:
                setter(alt_norm, king_norm, "alt")
                processed_pairs.add((alt_hash, king_hash))
            except Exception:
                continue


def _cleanup_after_success(media_path: Path, delete_source: bool):
    is_temp_merge = "(merged)" in media_path.name or ".dlhx_" in media_path.name

    if not delete_source and not is_temp_merge:
        return

    try:
        media_path.unlink()
        _cleanup_sidecar_files(media_path)
    except Exception as exc:
        log(f"⚠️  Could not delete file: {exc}", file=sys.stderr)


def _cleanup_sidecar_files(media_path: Path):
    targets = [
        media_path.parent / (media_path.name + ".metadata"),
        media_path.parent / (media_path.name + ".notes"),
        media_path.parent / (media_path.name + ".tag"),
    ]
    for target in targets:
        try:
            if target.exists():
                target.unlink()
        except Exception:
            pass


def _copy_sidecars(source_path: Path, target_path: Path):
    possible_sidecars = [
        source_path.with_suffix(source_path.suffix + ".json"),
        source_path.with_name(source_path.name + ".tag"),
        source_path.with_name(source_path.name + ".metadata"),
        source_path.with_name(source_path.name + ".notes"),
    ]
    for sc in possible_sidecars:
        try:
            if sc.exists():
                suffix_part = sc.name.replace(source_path.name, "", 1)
                dest_sidecar = target_path.parent / f"{target_path.name}{suffix_part}"
                dest_sidecar.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(sc), dest_sidecar)
        except Exception:
            pass


def _persist_local_metadata(
    library_root: Path,
    dest_path: Path,
    tags: List[str],
    url: List[str],
    f_hash: Optional[str],
    relationships: Any,
    duration: Any,
    media_kind: str,
):
    pass
