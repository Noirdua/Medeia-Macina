from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple, List
from pathlib import Path
import sys
import re
import shutil
from urllib.parse import urlparse

from SYS import models
from SYS import pipeline as ctx
from SYS.logger import log, debug, debug_panel
from SYS.payload_builders import build_table_result_payload
from SYS.pipeline_progress import PipelineProgress
from SYS.result_publication import overlay_existing_result_table, publish_result_table
from SYS.rich_display import show_available_plugins_panel, show_plugin_config_panel
from SYS.utils_constant import ALL_SUPPORTED_EXTENSIONS
from PluginCore.backend_registry import BackendRegistry
from .. import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
parse_cmdlet_args = sh.parse_cmdlet_args
SharedArgs = sh.SharedArgs
extract_tag_from_result = sh.extract_tag_from_result
extract_title_from_result = sh.extract_title_from_result
extract_url_from_result = sh.extract_url_from_result
merge_sequences = sh.merge_sequences
extract_relationships = sh.extract_relationships
extract_duration = sh.extract_duration
coerce_to_pipe_object = sh.coerce_to_pipe_object
collapse_namespace_tags = sh.collapse_namespace_tags
resolve_target_dir = sh.resolve_target_dir
build_pipeline_preview = sh.build_pipeline_preview
get_field = sh.get_field

from SYS.utils import sha256_file, unique_path, sanitize_filename

SUPPORTED_MEDIA_EXTENSIONS = ALL_SUPPORTED_EXTENSIONS
_SCREENSHOT_TIME_SUFFIX_RE = re.compile(
    r"^(?P<title>.+?)_(?P<label>(?:\d+h)?(?:\d+m)?\d+s)$",
    flags=re.IGNORECASE,
)

_REMOTE_URL_PREFIXES: tuple[str, ...] = (
    "http://", "https://", "ftp://", "ftps://", "magnet:", "torrent:", "tidal:", "hydrus:",
)


class _CommandDependencies:
    """Command-scope cache for the backend registry and plugin instances."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self._backend_registry: Optional[BackendRegistry] = None
        self._plugins: Dict[str, Any] = {}

    def get_backend_registry(self) -> Optional[BackendRegistry]:
        """Lazily initialize and return the command-scope backend registry."""
        if self._backend_registry is None:
            try:
                self._backend_registry = BackendRegistry(self.config)
            except Exception:
                self._backend_registry = None
        return self._backend_registry

    def get_plugin(self, name: str) -> Optional[Any]:
        """Cached plugin lookup by name."""
        from PluginCore.registry import get_plugin

        norm_name = str(name or "").strip().lower()
        if not norm_name:
            return None
        if norm_name in self._plugins:
            return self._plugins[norm_name]

        plugin = get_plugin(norm_name, self.config)
        self._plugins[norm_name] = plugin
        return plugin

    def get_plugin_for_cmdlet(self, name: str, cmdlet_name: str) -> Optional[Any]:
        """Cached plugin lookup with explicit cmdlet support check."""
        from PluginCore.registry import get_plugin_for_cmdlet

        norm_name = str(name or "").strip().lower()
        if not norm_name:
            return None

        cache_key = f"{norm_name}#{cmdlet_name}"
        if cache_key in self._plugins:
            return self._plugins[cache_key]

        plugin = get_plugin_for_cmdlet(norm_name, cmdlet_name, self.config)
        self._plugins[cache_key] = plugin
        return plugin


class Add_File(Cmdlet):
    """Add file into the DB"""

    def __init__(self) -> None:
        """Initialize add-file cmdlet."""
        super().__init__(
            name="add-file",
            summary="Save a piped/remote file to disk, or ingest into Hydrus/a host.",
            usage=
            "file -add [-plugin NAME] [-query \"instance:NAME\"] [-path <file|dir>] [<piped>]",
            arg=[
                SharedArgs.PLUGIN,
                SharedArgs.QUERY,
                SharedArgs.INSTANCE,
                SharedArgs.PATH,
                SharedArgs.URL,
                CmdletArg(
                    name="write-metadata",
                    type="enum",
                    choices=["true", "false"],
                    required=False,
                    description="Write a .metadata sidecar next to the file (default true).",
                ),
            ],
            detail=[
                "- Piped row + -path DIR saves to that folder (same fetch engine as file -download).",
                "- -plugin hydrusnetwork / ftp / ... ingests into a store or host.",
                "- Without a pipe, -path is the source file/directory to ingest.",
                "- file -download remains for -scrape, yt-dlp, and store export.",
            ],
            examples=[
                '@1 | file -add -path ~/Downloads',
                '@1 | file -add -plugin hydrusnetwork -query "instance:tutorial"',
                'file -add C:\\Media\\report.pdf -plugin ftp -query "instance:archive"',
            ],
            exec=self.run,
        )
        self.register()

    @staticmethod
    def _normalize_plugin_key(value: Optional[Any]) -> Optional[str]:
        if value is None:
            return None
        try:
            normalized = str(value).strip().lower()
        except Exception:
            return None
        if not normalized:
            return None
        if "." in normalized:
            normalized = normalized.split(".", 1)[0]
        return normalized

    @staticmethod
    def _wants_disk_fetch(parsed: Dict[str, Any], result: Any) -> bool:
        plugin = Add_File._normalize_plugin_key(parsed.get("plugin"))
        if plugin and plugin != "local":
            return False
        if plugin == "local":
            return False
        if parsed.get("instance") and not parsed.get("path"):
            return False
        items = result if isinstance(result, list) else ([result] if result is not None else [])
        if not any(item is not None for item in items):
            return False
        path_arg = parsed.get("path") or parsed.get("source")
        if not path_arg:
            return False
        try:
            dest = Path(str(path_arg)).expanduser()
        except Exception:
            return False
        return not dest.is_file()

    def run(self, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        """Main execution entry point."""
        parsed = parse_cmdlet_args(args, self)
        if self._wants_disk_fetch(parsed, result):
            from cmdlet.file.download import CMDLET as download_cmdlet

            return int(download_cmdlet.exec(result, args, config))
        progress = PipelineProgress(ctx)

        deps = _CommandDependencies(config)
        storage_registry: Optional[BackendRegistry] = None

        path_arg = parsed.get("path") or parsed.get("source")
        plugin_name = parsed.get("plugin")
        source_arg = path_arg
        folder_name = parsed.get("folder")
        plugin_key = Add_File._normalize_plugin_key(plugin_name)
        if path_arg and plugin_key == "local":
            try:
                path_obj = Path(str(path_arg))
            except Exception:
                path_obj = None
            if path_obj is None or not path_obj.is_file():
                folder_name = folder_name or path_arg
                if path_obj is None or not path_obj.exists():
                    source_arg = None
        location = parsed.get("instance")
        plugin_instance = parsed.get("instance")
        source_url_arg = parsed.get("url")
        delete_after = bool(parsed.get("delete", False))
        from SYS.utils import coerce_bool as _coerce_bool

        write_metadata = _coerce_bool(parsed.get("write-metadata"), True)
        local_export_destination: Optional[str] = None
        piped_items = result if isinstance(result, list) else ([result] if result is not None else [])
        has_pipe = any(item is not None for item in piped_items)
        if path_arg and not plugin_name and has_pipe:
            try:
                dest_candidate = Path(str(path_arg)).expanduser()
            except Exception:
                dest_candidate = None
            if dest_candidate is not None and not dest_candidate.is_file():
                local_export_destination = str(dest_candidate)
                source_arg = None
        if plugin_name and not plugin_instance and location:
            plugin_instance = location

        stage_ctx = ctx.get_stage_context()
        is_last_stage = (stage_ctx
                         is None) or bool(getattr(stage_ctx,
                                                  "is_last_stage",
                                                  False))
        has_downstream_stage = bool(stage_ctx is not None and not is_last_stage)

        dir_scan_mode = False
        dir_scan_results: Optional[List[Dict[str, Any]]] = None
        explicit_source_list_results: Optional[List[Dict[str, Any]]] = None

        if source_arg and location and not plugin_name:
            try:
                source_text = str(source_arg)
            except Exception:
                source_text = ""

            if "," in source_text:
                parts = [p.strip().strip('"') for p in source_text.split(",")]
                parts = [p for p in parts if p]

                batch: List[Dict[str, Any]] = []
                for p in parts:
                    try:
                        file_path = Path(p)
                    except Exception:
                        continue
                    if not file_path.exists() or not file_path.is_file():
                        continue
                    ext = file_path.suffix.lower()
                    if ext not in SUPPORTED_MEDIA_EXTENSIONS:
                        continue
                    try:
                        hv = sha256_file(file_path)
                    except Exception:
                        continue
                    try:
                        size = file_path.stat().st_size
                    except Exception:
                        size = 0
                    batch.append(
                        {
                            "path": file_path,
                            "name": file_path.name,
                            "hash": hv,
                            "size": size,
                            "ext": ext,
                        }
                    )

                if batch:
                    explicit_source_list_results = batch
                    source_arg = None
            else:
                try:
                    candidate_dir = Path(str(source_arg))
                    if candidate_dir.exists() and candidate_dir.is_dir():
                        dir_scan_mode = True
                        debug(
                            f"[add-file] Scanning directory for batch add: {candidate_dir}"
                        )
                        dir_scan_results = Add_File._scan_directory_for_files(
                            candidate_dir
                        )
                        if dir_scan_results:
                            debug(
                                f"[add-file] Found {len(dir_scan_results)} supported files in directory"
                            )
                        source_arg = None
                except Exception as exc:
                    debug(f"[add-file] Directory scan failed: {exc}")

        if result is None and not source_arg and not explicit_source_list_results and not dir_scan_results:
            try:
                if ctx.get_stage_context() is not None:
                    return 0
            except Exception:
                pass

        is_storage_backend_location = False
        if location and not plugin_name:
            try:
                backend_registry_for_lookup = storage_registry or deps.get_backend_registry()
                storage_registry = backend_registry_for_lookup
                is_storage_backend_location = Add_File._resolve_backend_by_name(backend_registry_for_lookup, str(location)) is not None
            except Exception:
                is_storage_backend_location = False

        if location and not plugin_name and not is_storage_backend_location:
            resolved_local_instance, resolved_local_path = Add_File._resolve_local_export_plugin_target(
                location,
                config,
                deps=deps,
                require_explicit=True,
            )
            if resolved_local_path:
                plugin_name = "local"
                plugin_instance = resolved_local_instance or str(location)
                location = None
                local_export_destination = resolved_local_path
            else:
                log(
                    f"Storage backend '{location}' not found. Use -plugin local -instance <name|path> for local export or configure that store backend.",
                    file=sys.stderr,
                )
                return 1

        normalized_plugin_name = Add_File._normalize_plugin_key(plugin_name)
        if normalized_plugin_name == "local":
            resolved_local_instance, resolved_local_path = Add_File._resolve_local_export_plugin_target(
                plugin_instance or location,
                config,
                deps=deps,
                require_explicit=bool(plugin_instance or location),
            )
            if not resolved_local_path:
                requested_local = str(plugin_instance or location or "").strip() or "<default>"
                log(
                    f"Local destination '{requested_local}' is not configured. Use -plugin local -instance <name|path>.",
                    file=sys.stderr,
                )
                return 1
            plugin_name = "local"
            plugin_instance = resolved_local_instance or str(plugin_instance or location or "").strip() or None
            location = None
            local_export_destination = resolved_local_path

        plugin_storage_backend = None
        if plugin_name:
            plugin_storage_backend = Add_File._resolve_plugin_storage_backend(
                plugin_name,
                plugin_instance,
                config,
                store_instance=storage_registry,
                deps=deps,
            )
            if plugin_storage_backend and storage_registry is None:
                storage_registry = deps.get_backend_registry()

        effective_storage_backend_name = plugin_storage_backend or (
            str(location) if location and is_storage_backend_location else None
        )

        if explicit_source_list_results:
            items_to_process = explicit_source_list_results
            debug(f"[add-file] Using {len(items_to_process)} files from source list")
        elif dir_scan_results:
            items_to_process = dir_scan_results
            debug(f"[add-file] Using {len(items_to_process)} files from directory scan")
        elif source_arg:
            items_to_process: List[Any] = [result]
        elif isinstance(result, list) and result:
            items_to_process = list(result)
        else:
            items_to_process = [result]

        total_items = len(items_to_process) if isinstance(items_to_process, list) else 0
        processed_items = 0
        try:
            ui, _ = progress.ui_and_pipe_index()
            if ui is not None and total_items:
                preview_items = (
                    list(items_to_process)
                    if isinstance(items_to_process, list) else [items_to_process]
                )
                progress.begin_pipe(
                    total_items=total_items,
                    items_preview=preview_items,
                )
        except Exception:
            pass
        try:
            if total_items:
                progress.set_percent(0)
        except Exception:
            pass

        use_steps = False
        steps_started = False
        try:
            ui, _ = progress.ui_and_pipe_index()
            use_steps = (ui is not None) and (len(items_to_process) == 1)
            if use_steps:
                progress.begin_steps(5)
                steps_started = True
        except Exception:
            use_steps = False

        should_present_directory_selector = bool(dir_scan_mode and not has_downstream_stage)
        if dir_scan_mode and has_downstream_stage:
            debug(
                "[add-file] Continuing with directory batch ingest because downstream stages exist"
            )

        if should_present_directory_selector:
            try:
                from SYS.result_table import Table
                from pathlib import Path as _Path

                base_args: List[str] = []
                if plugin_name:
                    base_args.extend(["-plugin", str(plugin_name)])
                if location:
                    base_args.extend(["-instance", str(location)])
                if source_url_arg:
                    base_args.extend(["-url", str(source_url_arg)])
                if bool(delete_after):
                    base_args.append("-delete")

                table = Table(title="Files in Directory", preserve_order=True)
                table.set_table("add-file.directory")
                table.set_source_command("add-file", base_args)

                rows: List[Dict[str, Any]] = []
                for file_info in dir_scan_results or []:
                    p = file_info.get("path")
                    hp = str(file_info.get("hash") or "")
                    name = str(file_info.get("name") or "unknown")
                    try:
                        clean_title = _Path(name).stem
                    except Exception:
                        clean_title = name
                    ext = str(file_info.get("ext") or "").lstrip(".")
                    size = file_info.get("size", 0)

                    row_item = build_table_result_payload(
                        title=clean_title,
                        columns=[
                            ("Title", clean_title),
                            ("Hash", hp),
                            ("Size", size),
                            ("Ext", ext),
                        ],
                        selection_args=[str(p) if p is not None else ""],
                        path=str(p) if p is not None else "",
                        hash=hp,
                    )
                    rows.append(row_item)
                    table.add_result(row_item)

                ctx.set_current_stage_table(table)
                ctx.set_last_result_table(
                    table,
                    rows,
                    subject={
                        "table": "add-file.directory"
                    }
                )
                log(f"✓ Found {len(rows)} files. Select with @N (e.g., @1 or @1-3).")
                return 0
            except Exception as exc:
                debug(
                    f"[add-file] Failed to display directory scan result table: {exc}"
                )

        collected_payloads: List[Dict[str, Any]] = []
        pending_relationship_pairs: Dict[str,
                                         set[tuple[str,
                                                   str]]] = {}
        pending_url_associations: Dict[str,
                                       List[tuple[str,
                                                  List[str]]]] = {}
        pending_tag_associations: Dict[str,
                                       List[tuple[str,
                                                  List[str]]]] = {}
        successes = 0
        failures = 0

        live_progress = None
        try:
            live_progress = ctx.get_live_progress()
        except Exception:
            live_progress = None

        want_final_search_file = (
            bool(is_last_stage)
            and bool(effective_storage_backend_name)
            and bool(live_progress)
        )
        auto_search_file_after_add = False

        defer_url_association = (
            bool(effective_storage_backend_name)
            and len(items_to_process) > 1
        )

        for idx, item in enumerate(items_to_process, 1):
            pipe_obj = coerce_to_pipe_object(item, source_arg)

            if source_url_arg:
                try:
                    from SYS.metadata import normalize_urls

                    cli_urls = [u.strip() for u in str(source_url_arg).split(",") if u and u.strip()]
                    merged_urls: List[str] = []

                    if isinstance(getattr(pipe_obj, "extra", None), dict):
                        existing_url = pipe_obj.extra.get("url")
                        if isinstance(existing_url, list):
                            merged_urls.extend(str(u) for u in existing_url if u)
                        elif isinstance(existing_url, str) and existing_url.strip():
                            merged_urls.append(existing_url.strip())
                    else:
                        pipe_obj.extra = {}

                    merged_urls = sh.merge_urls(merged_urls, cli_urls)
                    if merged_urls:
                        pipe_obj.extra["url"] = merged_urls
                except Exception:
                    pass

            try:
                label = pipe_obj.title
                if not label and pipe_obj.path:
                    try:
                        label = Path(str(pipe_obj.path)).name
                    except Exception:
                        label = pipe_obj.path
                if not label:
                    label = "file"
                if total_items:
                    pending_pct = int(round(((idx - 1) / max(1, total_items)) * 100))
                    progress.set_percent(pending_pct)
                    progress.set_status(f"adding {idx}/{total_items}: {label}")
            except Exception:
                pass

            temp_dir_to_cleanup: Optional[Path] = None
            delete_after_item = delete_after
            try:
                if use_steps and steps_started:
                    progress.step("resolving source")

                # Only use a local export destination for explicit local exports.
                # When -plugin/-instance target a store backend (e.g. hydrusnetwork),
                # -instance is the backend name — never a filesystem path.
                export_destination = None
                if local_export_destination:
                    export_destination = Path(local_export_destination)
                elif (
                    location
                    and not plugin_name
                    and not is_storage_backend_location
                    and not effective_storage_backend_name
                ):
                    export_destination = Path(location)
                media_path, file_hash, temp_dir_to_cleanup = self._resolve_source(
                    item,
                    source_arg,
                    pipe_obj,
                    config,
                    export_destination=export_destination,
                    store_instance=storage_registry,
                    deps=deps,
                )
                if not media_path and plugin_name:
                    media_path, file_hash, temp_dir_to_cleanup = Add_File._download_piped_source(
                        pipe_obj, config, storage_registry, deps=deps
                    )
                if not media_path:
                    failures += 1
                    continue

                pipe_obj.path = str(media_path)

                allow_all_files = not bool(effective_storage_backend_name)
                if not self._validate_source(media_path, allow_all_extensions=allow_all_files):
                    failures += 1
                    continue

                if use_steps and steps_started:
                    if not file_hash:
                        progress.step("hashing file")
                    progress.step("ingesting file")

                if plugin_name:
                    if effective_storage_backend_name:
                        code = self._handle_storage_backend(
                            item,
                            media_path,
                            effective_storage_backend_name,
                            pipe_obj,
                            config,
                            delete_after_item,
                            collect_payloads=collected_payloads,
                            collect_relationship_pairs=pending_relationship_pairs,
                            defer_url_association=defer_url_association,
                            pending_url_associations=pending_url_associations,
                            defer_tag_association=defer_url_association,
                            pending_tag_associations=pending_tag_associations,
                            suppress_last_stage_overlay=want_final_search_file,
                            auto_search_file=auto_search_file_after_add,
                            store_instance=storage_registry,
                        )
                    else:
                        code = self._handle_plugin_upload(
                            media_path,
                            plugin_name,
                            plugin_instance,
                            pipe_obj,
                            config,
                            delete_after_item,
                            folder_name=folder_name,
                            write_metadata=write_metadata,
                        )
                    if code == 0:
                        successes += 1
                    else:
                        failures += 1
                    continue

                if local_export_destination and not plugin_name and not location:
                    dest_dir = Path(str(local_export_destination)).expanduser()
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest_file = dest_dir / Path(str(media_path)).name
                    already_there = False
                    try:
                        already_there = media_path.resolve() == dest_file.resolve() or dest_dir in media_path.parents
                    except Exception:
                        already_there = False
                    if not already_there:
                        dest_file = unique_path(dest_file)
                        shutil.copy2(media_path, dest_file)
                        pipe_obj.path = str(dest_file)
                    final_path = Path(str(pipe_obj.path or dest_file))
                    extra = pipe_obj.extra if isinstance(getattr(pipe_obj, "extra", None), dict) else {}
                    payload = {
                        "title": getattr(pipe_obj, "title", None) or final_path.name,
                        "path": str(final_path),
                        "url": extra.get("url") or extra.get("source_url") or get_field(item, "url"),
                    }
                    try:
                        payload["size"] = final_path.stat().st_size
                    except Exception:
                        pass
                    collected_payloads.append(payload)
                    successes += 1
                    continue

                if location:
                    try:
                        backend_registry = storage_registry or deps.get_backend_registry()
                        resolved_backend = Add_File._resolve_backend_by_name(backend_registry, str(location))
                        if resolved_backend is not None:
                            code = self._handle_storage_backend(
                                item,
                                media_path,
                                location,
                                pipe_obj,
                                config,
                                delete_after_item,
                                collect_payloads=collected_payloads,
                                collect_relationship_pairs=pending_relationship_pairs,
                                defer_url_association=defer_url_association,
                                pending_url_associations=pending_url_associations,
                                defer_tag_association=defer_url_association,
                                pending_tag_associations=pending_tag_associations,
                                suppress_last_stage_overlay=want_final_search_file,
                                auto_search_file=auto_search_file_after_add,
                                store_instance=storage_registry,
                            )
                        else:
                            log(f"Invalid storage backend: {location}", file=sys.stderr)
                            code = 1
                    except Exception as exc:
                        debug(f"[add-file] ERROR: Failed to resolve location: {exc}")
                        log(f"Invalid location: {location}", file=sys.stderr)
                        failures += 1
                        continue

                    if code == 0:
                        successes += 1
                    else:
                        failures += 1
                    continue

                log("No destination specified", file=sys.stderr)
                failures += 1
            finally:
                if temp_dir_to_cleanup is not None:
                    try:
                        shutil.rmtree(temp_dir_to_cleanup, ignore_errors=True)
                    except Exception:
                        pass
                processed_items += 1
                try:
                    pct = int(round((processed_items / max(1, total_items)) * 100))
                    progress.set_percent(pct)
                    if processed_items >= total_items:
                        progress.clear_status()
                except Exception:
                    pass

        if pending_url_associations:
            try:
                Add_File._apply_pending_url_associations(
                    pending_url_associations,
                    config,
                    store_instance=storage_registry
                )
            except Exception:
                pass

        if pending_tag_associations:
            try:
                Add_File._apply_pending_tag_associations(
                    pending_tag_associations,
                    config,
                    store_instance=storage_registry
                )
            except Exception:
                pass

        if collected_payloads and is_last_stage and (
            want_final_search_file or local_export_destination
        ):
            try:
                from SYS.rich_display import render_item_details_panel
                from SYS.result_table import Table

                Add_File._stop_live_progress_for_terminal_render()

                subject = collected_payloads[0] if len(collected_payloads) == 1 else collected_payloads
                from .._shared import display_and_persist_items
                display_and_persist_items(collected_payloads, title="Result", subject=subject)

                try:
                    ctx.set_last_result_items_only(list(collected_payloads))
                except Exception:
                    pass
            except Exception:
                pass

        if pending_relationship_pairs:
            try:
                Add_File._apply_pending_relationships(
                    pending_relationship_pairs,
                    config,
                    store_instance=storage_registry,
                    deps=deps
                )
            except Exception:
                pass

        if use_steps and steps_started:
            progress.step("finalized")
            progress.clear_status()

        if successes > 0:
            return 0
        return 1

    @staticmethod
    def validate_preflight_args(
        args: Sequence[str],
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        from PluginCore.registry import PLUGIN_REGISTRY

        cfg = config if isinstance(config, dict) else {}

        try:
            parsed = parse_cmdlet_args(args, CMDLET)
        except Exception as exc:
            return f"Pipeline error: invalid add-file arguments: {exc}"

        deps = _CommandDependencies(cfg)
        storage_registry: Optional[BackendRegistry] = None

        location = parsed.get("instance")
        plugin_instance = parsed.get("instance")
        plugin_name = parsed.get("plugin")

        is_storage_backend_location = False
        if location and not plugin_name:
            try:
                backend_registry_for_lookup = storage_registry or deps.get_backend_registry()
                storage_registry = backend_registry_for_lookup
                is_storage_backend_location = Add_File._resolve_backend_by_name(
                    backend_registry_for_lookup,
                    str(location),
                ) is not None
            except Exception:
                is_storage_backend_location = False

        if location and not plugin_name and not is_storage_backend_location:
            resolved_local_instance, resolved_local_path = Add_File._resolve_local_export_plugin_target(
                location,
                cfg,
                deps=deps,
                require_explicit=True,
            )
            if resolved_local_path:
                return None
            return (
                f"Pipeline error: storage backend '{location}' not found. "
                "Use -plugin local -instance <name|path> for local export or configure that store backend."
            )

        normalized_plugin_name = Add_File._normalize_plugin_key(plugin_name)
        if normalized_plugin_name:
            upload_plugin = deps.get_plugin_for_cmdlet(normalized_plugin_name, "add-file")
            if upload_plugin is None:
                plugin_info = PLUGIN_REGISTRY.get(normalized_plugin_name)
                if plugin_info is not None:
                    canonical_plugin_name = str(plugin_info.canonical_name or normalized_plugin_name).strip().lower()
                    if canonical_plugin_name == "loc":
                        return (
                            "Pipeline error: plugin 'loc' does not support add-file. "
                            "Use -plugin local -instance <name|path> for local export."
                        )
                    if "add-file" not in plugin_info.supported_cmdlets:
                        return f"Pipeline error: plugin '{canonical_plugin_name}' does not support add-file."
                    return f"Pipeline error: plugin '{canonical_plugin_name}' is not configured or not available for add-file."
                return f"Pipeline error: unknown add-file plugin '{plugin_name}'."

        if normalized_plugin_name == "local":
            requested_local = str(plugin_instance or location or "").strip() or "<default>"
            resolved_local_instance, resolved_local_path = Add_File._resolve_local_export_plugin_target(
                plugin_instance or location,
                cfg,
                deps=deps,
                require_explicit=bool(plugin_instance or location),
            )
            if not resolved_local_path:
                return (
                    f"Pipeline error: local destination '{requested_local}' is not configured. "
                    "Use -plugin local -instance <name|path>."
                )

        return None

    @staticmethod
    def _resolve_plugin_storage_backend(
        plugin_name: Optional[Any],
        instance_name: Optional[Any],
        config: Dict[str, Any],
        *,
        store_instance: Optional[Any] = None,
        deps: Optional[_CommandDependencies] = None,
    ) -> Optional[str]:
        plugin_key = Add_File._normalize_plugin_key(plugin_name)
        if not plugin_key:
            return None

        if deps is None:
            deps = _CommandDependencies(config)

        file_provider = deps.get_plugin_for_cmdlet(plugin_key, "add-file")
        if file_provider is None:
            return None

        resolver = getattr(file_provider, "resolve_backend", None)
        if not callable(resolver):
            return None

        explicit_instance = str(instance_name or "").strip() or None
        try:
            backend_registry = store_instance if store_instance is not None else deps.get_backend_registry()
        except Exception:
            backend_registry = None

        try:
            resolved_name, backend = resolver(
                explicit_instance,
                storage=backend_registry,
                require_explicit=bool(explicit_instance),
            )
        except TypeError:
            try:
                resolved_name, backend = resolver(explicit_instance)
            except Exception:
                return None
        except Exception:
            return None

        if backend is None:
            return None

        resolved_text = str(resolved_name or explicit_instance or "").strip()
        if not resolved_text:
            return None

        checker = getattr(file_provider, "is_backend", None)
        if callable(checker):
            try:
                if not checker(backend, resolved_text):
                    return None
            except Exception:
                return None

        return resolved_text

    @staticmethod
    def _local_export_from_config(
        requested: Optional[str],
        config: Dict[str, Any],
        *,
        require_explicit: bool = False,
    ) -> tuple[Optional[str], Dict[str, Any]]:
        branch: Any = None
        try:
            plugins = config.get("plugin") if isinstance(config, dict) else None
            if isinstance(plugins, dict):
                branch = plugins.get("local")
        except Exception:
            branch = None
        try:
            from SYS.config import normalize_multi_instance_plugin_block

            instances = normalize_multi_instance_plugin_block("local", branch)
        except Exception:
            instances = {}
        if not instances and isinstance(branch, dict):
            instances = {
                str(name): dict(value)
                for name, value in branch.items()
                if str(name or "").strip() and isinstance(value, dict)
            }
        if not instances:
            return None, {}

        def _path_from_settings(settings: Dict[str, Any]) -> str:
            for key in ("path", "PATH", "destination", "dest", "destination_path"):
                text = str(settings.get(key) or "").strip()
                if text:
                    return text
            return ""

        requested_text = str(requested or "").strip()
        if requested_text:
            requested_lower = requested_text.lower()
            for name, settings in instances.items():
                aliases = {str(name or "").strip().lower()}
                for key in ("NAME", "Name", "name", "_instance_name"):
                    alias = str(settings.get(key) or "").strip().lower()
                    if alias:
                        aliases.add(alias)
                if requested_lower in aliases:
                    path_value = _path_from_settings(settings)
                    if path_value:
                        return str(name), {"path": path_value}
            return None, {}

        if require_explicit:
            return None, {}
        for name, settings in instances.items():
            path_value = _path_from_settings(settings)
            if path_value:
                return str(name), {"path": path_value}
        return None, {}

    @staticmethod
    def _resolve_local_export_plugin_target(
        requested: Optional[Any],
        config: Dict[str, Any],
        *,
        deps: Optional[_CommandDependencies] = None,
        require_explicit: bool = False,
    ) -> tuple[Optional[str], Optional[str]]:
        requested_text = str(requested or "").strip() or None
        resolved_name, settings = Add_File._local_export_from_config(
            requested_text,
            config,
            require_explicit=require_explicit,
        )
        path_value = str((settings or {}).get("path") or "").strip()
        if path_value:
            resolved_text = str(resolved_name or requested_text or "").strip() or None
            return resolved_text, path_value

        if deps is None:
            deps = _CommandDependencies(config)

        file_provider = deps.get_plugin_for_cmdlet("local", "add-file")
        if file_provider is None:
            return None, None

        resolver = getattr(file_provider, "resolve_destination", None)
        if not callable(resolver):
            return None, None

        try:
            resolved_name, settings = resolver(
                requested_text,
                require_explicit=require_explicit,
            )
        except TypeError:
            try:
                resolved_name, settings = resolver(requested_text)
            except Exception:
                return None, None
        except Exception:
            return None, None

        path_value = str((settings or {}).get("path") or "").strip()
        if not path_value:
            return None, None
        resolved_text = str(resolved_name or requested_text or "").strip() or None
        return resolved_text, path_value


# Late-binding: import sub-module functions and attach them as static methods.
# The imports MUST happen after the class is fully defined to avoid circular imports,
# since sub-modules import _CommandDependencies from this module.

from .add_validation import (  # noqa: E402
    _resolve_source,
    _validate_source,
    _scan_directory_for_files,
    _is_probable_url,
    _resolve_backend_by_name,
    _download_piped_source,
    _maybe_download_plugin_result,
    _build_plugin_filename,
    _maybe_download_backend_file,
    _download_remote_backend_url,
)

from .add_tagging import (  # noqa: E402
    _maybe_apply_florencevision_tags,
    _prepare_metadata,
    _resolve_file_hash,
    _load_sidecar_bundle,
    _get_url,
    _get_relationships,
    _get_duration,
    _get_note_text,
    _parse_relationship_tag_king_alts,
    _parse_relationships_king_alts,
    _normalize_hash_candidate,
    _resolve_media_kind,
)

from .add_storage import (  # noqa: E402
    _handle_storage_backend,
    _handle_plugin_upload,
    _emit_plugin_upload_payload,
    _emit_storage_result,
    _emit_pipe_object,
    _update_pipe_object_destination,
    _find_existing_hash_by_urls,
    _try_emit_search_file_by_hash,
    _try_emit_search_file_by_hashes,
    _apply_pending_url_associations,
    _apply_pending_tag_associations,
    _apply_pending_relationships,
    _cleanup_after_success,
    _cleanup_sidecar_files,
    _copy_sidecars,
    _persist_local_metadata,
    _stop_live_progress_for_terminal_render,
)

# Attach sub-module functions as static methods on Add_File
Add_File._resolve_source = staticmethod(_resolve_source)
Add_File._validate_source = staticmethod(_validate_source)
Add_File._scan_directory_for_files = staticmethod(_scan_directory_for_files)
Add_File._is_probable_url = staticmethod(_is_probable_url)
Add_File._resolve_backend_by_name = staticmethod(_resolve_backend_by_name)
Add_File._download_piped_source = staticmethod(_download_piped_source)
Add_File._maybe_download_plugin_result = staticmethod(_maybe_download_plugin_result)
Add_File._build_plugin_filename = staticmethod(_build_plugin_filename)
Add_File._maybe_download_backend_file = staticmethod(_maybe_download_backend_file)
Add_File._download_remote_backend_url = staticmethod(_download_remote_backend_url)

Add_File._prepare_metadata = staticmethod(_prepare_metadata)
Add_File._resolve_file_hash = staticmethod(_resolve_file_hash)
Add_File._load_sidecar_bundle = staticmethod(_load_sidecar_bundle)
Add_File._get_url = staticmethod(_get_url)
Add_File._get_relationships = staticmethod(_get_relationships)
Add_File._get_duration = staticmethod(_get_duration)
Add_File._get_note_text = staticmethod(_get_note_text)
Add_File._parse_relationship_tag_king_alts = staticmethod(_parse_relationship_tag_king_alts)
Add_File._parse_relationships_king_alts = staticmethod(_parse_relationships_king_alts)
Add_File._normalize_hash_candidate = staticmethod(_normalize_hash_candidate)
Add_File._resolve_media_kind = staticmethod(_resolve_media_kind)

Add_File._handle_storage_backend = staticmethod(_handle_storage_backend)
Add_File._handle_plugin_upload = staticmethod(_handle_plugin_upload)
Add_File._emit_plugin_upload_payload = staticmethod(_emit_plugin_upload_payload)
Add_File._emit_storage_result = staticmethod(_emit_storage_result)
Add_File._emit_pipe_object = staticmethod(_emit_pipe_object)
Add_File._update_pipe_object_destination = staticmethod(_update_pipe_object_destination)
Add_File._find_existing_hash_by_urls = staticmethod(_find_existing_hash_by_urls)
Add_File._try_emit_search_file_by_hash = staticmethod(_try_emit_search_file_by_hash)
Add_File._try_emit_search_file_by_hashes = staticmethod(_try_emit_search_file_by_hashes)
Add_File._apply_pending_url_associations = staticmethod(_apply_pending_url_associations)
Add_File._apply_pending_tag_associations = staticmethod(_apply_pending_tag_associations)
Add_File._apply_pending_relationships = staticmethod(_apply_pending_relationships)
Add_File._cleanup_after_success = staticmethod(_cleanup_after_success)
Add_File._cleanup_sidecar_files = staticmethod(_cleanup_sidecar_files)
Add_File._copy_sidecars = staticmethod(_copy_sidecars)
Add_File._persist_local_metadata = staticmethod(_persist_local_metadata)
Add_File._stop_live_progress_for_terminal_render = staticmethod(_stop_live_progress_for_terminal_render)


CMDLET = Add_File()
