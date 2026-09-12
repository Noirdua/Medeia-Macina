"""Generic file/stream downloader — core orchestration.

Supports:
- Direct HTTP file URLs (PDFs, images, documents; non-yt-dlp)
- Piped plugin items (uses plugin.download when available)
- Streaming sites via yt-dlp (YouTube, Bandcamp, etc.)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence as SequenceABC
import sys
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse
from contextlib import AbstractContextManager, nullcontext
import shutil
import webbrowser

from API.HTTP import download_direct_file
from SYS.models import DownloadError, DownloadOptions, DownloadMediaResult
from SYS.logger import log, debug_panel, is_debug_enabled
from SYS.payload_builders import build_file_result_payload, build_table_result_payload
from SYS.pipeline_progress import PipelineProgress
from SYS.result_table import Table, build_display_row
from SYS.rich_display import stderr_console as get_stderr_console
from SYS import pipeline as pipeline_context
from SYS.item_accessors import get_result_title
from rich.prompt import Prompt
from SYS.selection_builder import (
    build_hash_store_selection,
    extract_selection_fields,
    extract_urls_from_selection_args,
    selection_args_have_url,
)
from SYS.utils import sanitize_filename, sha256_file, unique_path

from PluginCore.registry import plugin_attr

YtDlpTool = plugin_attr("ytdlp", "YtDlpTool")

from .. import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
SharedArgs = sh.SharedArgs
QueryArg = sh.QueryArg
parse_cmdlet_args = sh.parse_cmdlet_args
get_field = sh.get_field
resolve_target_dir = sh.resolve_target_dir
coerce_to_path = sh.coerce_to_path
build_pipeline_preview = sh.build_pipeline_preview


class Download_File(Cmdlet):
    """Class-based download-file cmdlet - direct HTTP downloads."""

    def __init__(self) -> None:
        """Initialize download-file cmdlet."""
        super().__init__(
            name="download-file",
            summary="Fetch a URL or export a store file to disk (does not import into Hydrus).",
            usage=
            "download-file <url|path> [-plugin NAME] [-instance NAME] [-path DIR] [options] OR @N | download-file [-plugin NAME] [-instance NAME] [-path DIR] [options] OR download-file -query \"hash:<sha256>\" -instance <instance> [-browser]",
            alias=["dl-file",
                   "download-http"],
            arg=[
                SharedArgs.URL,
                SharedArgs.PLUGIN,
                SharedArgs.INSTANCE,
                SharedArgs.PATH,
                SharedArgs.QUERY,
                CmdletArg(
                    name="name",
                    type="string",
                    description="Output filename override for instance exports.",
                ),
                CmdletArg(
                    name="browser",
                    type="flag",
                    description="Open a backend-provided browser URL instead of exporting to disk when available.",
                ),
                QueryArg(
                    "clip",
                    key="clip",
                    aliases=["range",
                             "section",
                             "sections"],
                    type="string",
                    required=False,
                    description=(
                        "Clip time ranges via -query keyed fields (e.g. clip:1m-2m or clip:00:01-00:10). "
                        "Comma-separated values supported."
                    ),
                    query_only=True,
                ),
                QueryArg(
                    "type",
                    key="type",
                    aliases=["ext", "filetype"],
                    type="string",
                    required=False,
                    description="When scraping a webpage, show only this file type (image, pdf, audio, video, ...).",
                    query_only=True,
                ),
                CmdletArg(
                    name="scrape",
                    type="flag",
                    description="Scrape a webpage URL and list downloadable file types instead of saving the page.",
                ),
                CmdletArg(
                    name="item",
                    type="string",
                    description="Item selection for playlists/formats",
                ),
            ],
            detail=[
                "Download files directly via HTTP or streaming media via yt-dlp.",
                "Also exports instance-backed files via hash+instance selection or -query \"hash:<sha256>\" -instance <instance>.",
                "Use -plugin with -instance to target a named plugin config when a plugin exposes multiple instances.",
                "For Internet Archive item pages (archive.org/details/...), shows a selectable file/format list; pick with @N to download.",
                "For ordinary webpages, scrapes downloadable file types (images, PDFs, etc.) and lists them for selection.",
            ],
            exec=self.run,
        )
        self.register()

    def run(self, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        """Main execution method."""
        try:
            debug_panel(
                "download-file",
                [
                    ("args", list(args)),
                    ("has_piped_input", bool(result)),
                ],
                border_style="cyan",
            )
        except Exception:
            pass
        return self._run_impl(result, args, config)

    @staticmethod
    def _path_from_download_result(result_obj: Any) -> Path:
        """Normalize downloader return values to a concrete filesystem path."""
        resolved = coerce_to_path(result_obj)
        if resolved is None:
            raise DownloadError("Could not determine downloaded file path")
        return resolved

    @staticmethod
    def _selection_run_label(
        run_args: Sequence[str],
        *,
        extra_url_prefixes: Sequence[str] = (),
    ) -> str:
        try:
            urls = extract_urls_from_selection_args(
                run_args,
                extra_url_prefixes=extra_url_prefixes,
            )
            if urls:
                return str(urls[0])
        except Exception:
            pass

        for arg in run_args:
            text = str(arg or "").strip()
            if text and not text.startswith("-"):
                return text
        return "item"

    @staticmethod
    def _batch_progress_state(config: Optional[Dict[str, Any]]) -> tuple[bool, int, int, str]:
        if not isinstance(config, dict):
            return False, 0, 0, ""

        suppress_nested = bool(config.get("_download_file_suppress_nested_pipe_progress"))
        if not suppress_nested:
            return False, 0, 0, ""

        try:
            total = max(0, int(config.get("_download_file_batch_total") or 0))
        except Exception:
            total = 0
        try:
            index = max(0, int(config.get("_download_file_batch_index") or 0))
        except Exception:
            index = 0
        try:
            label = str(config.get("_download_file_batch_label") or "").strip()
        except Exception:
            label = ""

        return True, total, index, label

    @staticmethod
    def _selection_url_prefixes(registry: Dict[str, Any]) -> List[str]:
        loader = registry.get("list_selection_url_prefixes")
        if not callable(loader):
            return []
        try:
            values = loader() or []
        except Exception:
            return []
        return [str(value).strip().lower() for value in values if str(value or "").strip()]

    def _normalize_plugin_key(self, value: Optional[Any]) -> Optional[str]:
        if value is None:
            return None
        try:
            normalized = str(value).strip()
        except Exception:
            return None
        if not normalized:
            return None
        if "." in normalized:
            normalized = normalized.split(".", 1)[0]
        return normalized.lower()

    def _plugin_key_from_item(self, item: Any) -> Optional[str]:
        table_hint = get_field(item, "table")
        key = self._normalize_plugin_key(table_hint)
        if key:
            return key
        plugin_hint = get_field(item, "plugin")
        key = self._normalize_plugin_key(plugin_hint)
        if key:
            return key
        return self._normalize_plugin_key(get_field(item, "source"))

    @staticmethod
    def _path_looks_local(value: Any) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        if text.startswith(("http://", "https://", "ftp://", "ftps://", "magnet:", "torrent:")):
            return False
        if len(text) >= 2 and text[1] == ":":
            return True
        if text.startswith(("\\", "/", ".", "~")):
            return True
        return Path(text).exists()

    @staticmethod
    def _resolve_display_title(result: Any, metadata: Optional[Dict[str, Any]]) -> str:
        candidates = [
            get_result_title(result, "title", "name", "filename"),
            get_result_title(metadata or {}, "title", "name", "filename"),
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            text = str(candidate).strip()
            if text:
                return text
        return ""

    @staticmethod
    def _sanitize_export_filename(name: str) -> str:
        return sanitize_filename(name, fallback="export")

    @staticmethod
    def _unique_export_path(path: Path) -> Path:
        return unique_path(path)

    @staticmethod
    def _load_provider_registry() -> Dict[str, Any]:
        """Lightweight accessor for plugin helpers without hard dependencies."""
        try:
            from PluginCore import registry as provider_registry  # type: ignore
            from PluginCore.base import SearchResult  # type: ignore

            return {
                "get_plugin": getattr(provider_registry, "get_plugin", None),
                "match_plugin_name_for_url": getattr(provider_registry, "match_plugin_name_for_url", None),
                "list_selection_url_prefixes": getattr(provider_registry, "list_selection_url_prefixes", None),
                "SearchResult": SearchResult,
            }
        except Exception:
            return {
                "get_plugin": None,
                "match_plugin_name_for_url": None,
                "list_selection_url_prefixes": None,
                "SearchResult": None,
            }

    @classmethod
    def _normalize_duplicate_preflight_policy(cls, value: Any) -> Optional[str]:
        text = str(value or "").strip().lower()
        if not text:
            return "skip"
        mapping = {
            "i": "ignore",
            "ignore": "ignore",
            "s": "skip",
            "skip": "skip",
            "c": "cancel",
            "cancel": "cancel",
        }
        return mapping.get(text)

    def _record_download_item(self, item: Any) -> None:
        if not isinstance(item, dict):
            return
        bucket = getattr(self, "_downloaded_detail_items", None)
        if not isinstance(bucket, list):
            self._downloaded_detail_items = [item]
            return
        bucket.append(item)

    def _maybe_render_download_details(self, *, config: Dict[str, Any]) -> None:
        try:
            stage_ctx = pipeline_context.get_stage_context()
        except Exception:
            stage_ctx = None

        is_last_stage = (stage_ctx is None) or bool(getattr(stage_ctx, "is_last_stage", False))
        if not is_last_stage:
            return

        try:
            quiet_mode = bool(config.get("_quiet_background_output")) if isinstance(config, dict) else False
        except Exception:
            quiet_mode = False
        if quiet_mode:
            return

        emitted_items: List[Any] = []
        try:
            recorded = getattr(self, "_downloaded_detail_items", None)
            if isinstance(recorded, list) and recorded:
                emitted_items = list(recorded)
        except Exception:
            emitted_items = []
        if not emitted_items:
            try:
                emitted_items = list(getattr(stage_ctx, "emits", None) or []) if stage_ctx is not None else []
            except Exception:
                emitted_items = []

        if not emitted_items:
            return

        try:
            live_progress = pipeline_context.get_live_progress()
        except Exception:
            live_progress = None

        if live_progress is not None:
            try:
                pipe_idx = getattr(stage_ctx, "pipe_index", None) if stage_ctx is not None else None
                if isinstance(pipe_idx, int):
                    live_progress.finish_pipe(int(pipe_idx), force_complete=True)
            except Exception:
                pass
            try:
                live_progress.stop()
            except Exception:
                pass
            try:
                if hasattr(pipeline_context, "set_live_progress"):
                    pipeline_context.set_live_progress(None)
            except Exception:
                pass

        try:
            subject = emitted_items[0] if len(emitted_items) == 1 else list(emitted_items)
            from .._shared import display_and_persist_items
            display_and_persist_items(list(emitted_items), title="Result", subject=subject)
        except Exception:
            pass

    def _maybe_show_provider_picker(
        self,
        *,
        raw_urls: Sequence[str],
        piped_items: Sequence[Any],
        parsed: Dict[str, Any],
        config: Dict[str, Any],
        registry: Dict[str, Any],
    ) -> Optional[int]:
        """Generic hook for providers to show a selection table (e.g. Internet Archive format picker)."""
        total_inputs = len(raw_urls or []) + len(piped_items or [])
        if total_inputs != 1:
            return None

        target_url = None
        if raw_urls:
            target_url = str(raw_urls[0])
        elif piped_items:
            target_url = str(get_field(piped_items[0], "path") or get_field(piped_items[0], "url") or "")

        if not target_url:
            return None

        match_provider_name_for_url = registry.get("match_plugin_name_for_url")
        get_provider = registry.get("get_plugin")

        provider_name = None
        if match_provider_name_for_url:
            try:
                provider_name = match_provider_name_for_url(target_url)
            except Exception:
                pass

        if provider_name and get_provider:
            provider = get_provider(provider_name, config)
            if provider and hasattr(provider, "maybe_show_picker"):
                try:
                    quiet_mode = bool(config.get("_quiet_background_output"))
                    res = provider.maybe_show_picker(
                        url=target_url,
                        item=piped_items[0] if piped_items else None,
                        parsed=parsed,
                        config=config,
                        quiet_mode=quiet_mode,
                    )
                    if res is not None:
                        return int(res)
                except Exception as e:
                    debug_panel(
                        "download-file picker error",
                        [
                            ("plugin", provider_name),
                            ("url", target_url),
                            ("error", e),
                        ],
                        border_style="yellow",
                    )

        return None

    def _maybe_show_page_scrape_picker(
        self,
        *,
        raw_urls: Sequence[str],
        piped_items: Sequence[Any],
        parsed: Dict[str, Any],
        registry: Dict[str, Any],
    ) -> Optional[int]:
        total_inputs = len(raw_urls or []) + len(piped_items or [])
        if total_inputs != 1:
            return None

        target_url = ""
        if raw_urls:
            target_url = str(raw_urls[0] or "").strip()
        elif piped_items:
            target_url = str(get_field(piped_items[0], "path") or get_field(piped_items[0], "url") or "").strip()
        if not target_url.lower().startswith(("http://", "https://")):
            return None

        from .search_engines import (
            _scrape_page,
            assemble_scrape_result_rows,
            parse_scrape_filters,
            url_looks_like_direct_file,
        )

        if url_looks_like_direct_file(target_url):
            return None

        match_provider_name_for_url = registry.get("match_plugin_name_for_url")
        if callable(match_provider_name_for_url):
            try:
                if match_provider_name_for_url(target_url):
                    return None
            except Exception:
                pass

        query_text = str(parsed.get("query") or "").strip()
        type_group, requested_type = parse_scrape_filters(query_text)
        scrape_requested = bool(parsed.get("scrape")) or bool(type_group or requested_type)
        if not scrape_requested:
            return None

        kind, assets = _scrape_page(target_url)
        if kind != "html":
            return None

        if not assets:
            log(f"No downloadable files found on {target_url}", file=sys.stderr)
            return 0

        base_args: List[str] = []
        out_arg = parsed.get("path")
        if out_arg:
            base_args.extend(["-path", str(out_arg)])

        show_types, table_name, rows, empty_message = assemble_scrape_result_rows(
            page_url=target_url,
            assets=assets,
            type_group=type_group,
            filetype=requested_type,
            command="download-file",
            extra_args=base_args,
            limit=500,
        )
        if not rows:
            log(empty_message, file=sys.stderr)
            return 0

        try:
            from SYS.result_table import Table
            from SYS import pipeline as pipeline_context
        except Exception:
            return None

        table_title = f"Scrape: {target_url}"
        table = Table(table_title)._perseverance(True)
        table.set_table(table_name)
        table.set_source_command("download-file", base_args)

        for row_item in rows:
            table.add_result(row_item)

        try:
            pipeline_context.set_last_result_table(table, rows, subject=target_url)
            pipeline_context.set_current_stage_table(table)
        except Exception:
            pass

        if show_types:
            log("Webpage detected: select a file type with @N, then pick files to download", file=sys.stderr)
        else:
            log("Select a file with @N to download", file=sys.stderr)
        return 0

    def _run_impl(
        self,
        result: Any,
        args: Sequence[str],
        config: Dict[str, Any]
    ) -> int:
        """Main download implementation for direct HTTP files."""
        progress = PipelineProgress(pipeline_context)
        prev_progress = None
        had_progress_key = False
        try:
            self._download_run_depth = int(getattr(self, "_download_run_depth", 0) or 0) + 1
        except Exception:
            self._download_run_depth = 1
        if int(getattr(self, "_download_run_depth", 1) or 1) == 1:
            self._downloaded_detail_items = []
        try:
            try:
                if isinstance(config, dict):
                    had_progress_key = "_pipeline_progress" in config
                    prev_progress = config.get("_pipeline_progress")
                    config["_pipeline_progress"] = progress
            except Exception:
                pass

            parsed = parse_cmdlet_args(args, self)
            registry = self._load_provider_registry()
            selection_url_prefixes = self._selection_url_prefixes(registry)
            explicit_input = parsed.get("url")

            url_candidates = parsed.get("url") or [
                a for a in parsed.get("args", [])
                if isinstance(a, str) and (
                    a.startswith("http") or "://" in a or ":" in a
                    or "\U0001f9f2" in a
                    and not a.startswith("-")
                )
            ]
            from SYS.metadata import normalize_urls as normalize_url_list  # lazy: avoids Cryptodome at startup
            raw_url = normalize_url_list(url_candidates)
            local_source_inputs: List[str] = []
            if not raw_url and isinstance(explicit_input, str) and self._path_looks_local(explicit_input):
                local_source_inputs = [str(explicit_input)]

            quiet_mode = bool(config.get("_quiet_background_output")) if isinstance(config, dict) else False

            piped_items = []
            if not raw_url:
                piped_items = sh.normalize_result_items(
                    result,
                    include_falsey_single=True,
                )

            if piped_items and not raw_url:
                selection_runs: List[List[str]] = []
                residual_items: List[Any] = []

                for item in piped_items:
                    handled = False
                    try:
                        normalized_args, _normalized_action, item_url = extract_selection_fields(
                            item,
                            extra_url_prefixes=selection_url_prefixes,
                        )
                        if normalized_args:
                            if selection_args_have_url(
                                normalized_args,
                                extra_url_prefixes=selection_url_prefixes,
                            ):
                                selection_runs.append(list(normalized_args))
                                handled = True
                            elif item_url:
                                selection_runs.append([str(item_url)] + list(normalized_args))
                                handled = True
                    except Exception as e:
                        debug_panel(
                            "download-file selection args failed",
                            [("error", e)],
                            border_style="yellow",
                        )
                        handled = False
                    if not handled:
                        residual_items.append(item)
                if selection_runs:
                    selection_urls: List[str] = []

                    for run_args in selection_runs:
                        for u in extract_urls_from_selection_args(
                            run_args,
                            extra_url_prefixes=selection_url_prefixes,
                        ):
                            if u not in selection_urls:
                                selection_urls.append(u)

                    original_skip_preflight = None
                    original_timeout = None
                    original_skip_direct = None
                    original_batch_total = None
                    original_batch_index = None
                    original_batch_label = None
                    original_suppress_nested = None
                    try:
                        if isinstance(config, dict):
                            original_skip_preflight = config.get("_skip_url_preflight")
                            original_timeout = config.get("_pipeobject_timeout_seconds")
                            original_skip_direct = config.get("_skip_direct_on_streaming_failure")
                            original_batch_total = config.get("_download_file_batch_total")
                            original_batch_index = config.get("_download_file_batch_index")
                            original_batch_label = config.get("_download_file_batch_label")
                            original_suppress_nested = config.get("_download_file_suppress_nested_pipe_progress")
                    except Exception:
                        original_skip_preflight = None
                        original_timeout = None
                        original_batch_total = None
                        original_batch_index = None
                        original_batch_label = None
                        original_suppress_nested = None

                    try:
                        if selection_urls:
                            if isinstance(config, dict):
                                config["_skip_url_preflight"] = True
                                config["_skip_direct_on_streaming_failure"] = True

                        if isinstance(config, dict) and config.get("_pipeobject_timeout_seconds") is None:
                            config["_pipeobject_timeout_seconds"] = 600

                        successes = 0
                        failures = 0
                        last_code = 0
                        total_selection = len(selection_runs)
                        preview_items = list(selection_urls[:5]) or [
                            self._selection_run_label(
                                run_args,
                                extra_url_prefixes=selection_url_prefixes,
                            )
                            for run_args in selection_runs[:5]
                        ]
                        try:
                            progress.ensure_local_ui(
                                label="download-file",
                                total_items=total_selection,
                                items_preview=preview_items,
                            )
                        except Exception:
                            pass
                        try:
                            progress.begin_pipe(
                                total_items=total_selection,
                                items_preview=preview_items,
                            )
                        except Exception:
                            pass
                        for idx, run_args in enumerate(selection_runs, 1):
                            run_label = self._selection_run_label(
                                run_args,
                                extra_url_prefixes=selection_url_prefixes,
                            )
                            try:
                                progress.set_status(
                                    f"downloading {idx}/{total_selection}: {run_label}"
                                )
                            except Exception:
                                pass
                            try:
                                if isinstance(config, dict):
                                    config["_download_file_batch_total"] = total_selection
                                    config["_download_file_batch_index"] = idx
                                    config["_download_file_batch_label"] = run_label
                                    config["_download_file_suppress_nested_pipe_progress"] = True
                            except Exception:
                                pass
                            nested_args = list(run_args)
                            path_val = parsed.get("path")
                            flags = {str(tok).strip().lower() for tok in nested_args}
                            if path_val and "-path" not in flags and "--path" not in flags:
                                nested_args.extend(["-path", str(path_val)])
                            exit_code = self._run_impl(None, nested_args, config)
                            if exit_code == 0:
                                successes += 1
                            else:
                                failures += 1
                                last_code = exit_code

                        piped_items = residual_items
                        if not piped_items:
                            if successes > 0:
                                return 0
                            return last_code or 1
                    finally:
                        try:
                            if isinstance(config, dict):
                                if original_skip_preflight is None:
                                    config.pop("_skip_url_preflight", None)
                                else:
                                    config["_skip_url_preflight"] = original_skip_preflight
                                if original_timeout is None:
                                    config.pop("_pipeobject_timeout_seconds", None)
                                else:
                                    config["_pipeobject_timeout_seconds"] = original_timeout
                                if original_skip_direct is None:
                                    config.pop("_skip_direct_on_streaming_failure", None)
                                else:
                                    config["_skip_direct_on_streaming_failure"] = original_skip_direct
                                if original_batch_total is None:
                                    config.pop("_download_file_batch_total", None)
                                else:
                                    config["_download_file_batch_total"] = original_batch_total
                                if original_batch_index is None:
                                    config.pop("_download_file_batch_index", None)
                                else:
                                    config["_download_file_batch_index"] = original_batch_index
                                if original_batch_label is None:
                                    config.pop("_download_file_batch_label", None)
                                else:
                                    config["_download_file_batch_label"] = original_batch_label
                                if original_suppress_nested is None:
                                    config.pop("_download_file_suppress_nested_pipe_progress", None)
                                else:
                                    config["_download_file_suppress_nested_pipe_progress"] = original_suppress_nested
                        except Exception:
                            pass

            had_piped_input = False
            try:
                if isinstance(result, list):
                    had_piped_input = bool(result)
                else:
                    had_piped_input = bool(result)
            except Exception:
                had_piped_input = False

            if (had_piped_input and raw_url and len(raw_url) == 1
                    and (not parsed.get("path"))):
                candidate = str(raw_url[0] or "").strip()
                low = candidate.lower()
                looks_like_url = low.startswith(
                    ("http://", "https://", "ftp://", "magnet:", "torrent:")
                    + tuple(selection_url_prefixes)
                )
                looks_like_provider = (
                    ":" in candidate and not candidate.startswith(
                        ("http:", "https:", "ftp:", "ftps:", "file:")
                        + tuple(selection_url_prefixes)
                    )
                )
                looks_like_windows_path = (
                    (len(candidate) >= 2 and candidate[1] == ":")
                    or candidate.startswith("\\\\") or candidate.startswith("\\")
                    or candidate.endswith(("\\", "/"))
                )
                if (not looks_like_url) and (
                        not looks_like_provider) and looks_like_windows_path:
                    parsed["path"] = candidate
                    raw_url = []
                    piped_items = sh.normalize_result_items(result, include_falsey_single=True)

            if not raw_url and not piped_items and not local_source_inputs:
                log("No url or piped items to download", file=sys.stderr)
                return 1

            picker_result = self._maybe_show_provider_picker(
                raw_urls=raw_url,
                piped_items=piped_items,
                parsed=parsed,
                config=config,
                registry=registry,
            )
            if picker_result is not None:
                return int(picker_result)

            scrape_picker = self._maybe_show_page_scrape_picker(
                raw_urls=raw_url,
                piped_items=piped_items,
                parsed=parsed,
                registry=registry,
            )
            if scrape_picker is not None:
                return int(scrape_picker)

            final_output_dir = resolve_target_dir(parsed, config)
            if not final_output_dir:
                return 1

            try:
                debug_panel(
                    "download-file plan",
                    [
                        ("output_dir", final_output_dir),
                        ("remaining_urls", len(raw_url)),
                        ("piped_items", len(piped_items) if isinstance(piped_items, list) else int(bool(piped_items))),
                    ],
                    border_style="cyan",
                )
            except Exception:
                pass

            raw_url, preflight_exit, skipped_dupe_count = self._preflight_explicit_url_duplicates(
                raw_urls=raw_url,
                config=config,
            )
            if preflight_exit is not None:
                return int(preflight_exit)

            total_items = max(1, len(raw_url or []) + len(piped_items or []))
            preview = build_pipeline_preview(raw_url, piped_items)

            progress.ensure_local_ui(
                label="download-file",
                total_items=total_items,
                items_preview=preview
            )

            downloaded_count = 0

            if local_source_inputs:
                downloaded_count += self._process_explicit_local_sources(
                    local_sources=local_source_inputs,
                    final_output_dir=final_output_dir,
                    parsed=parsed,
                    progress=progress,
                    config=config,
                )

            storage_downloaded, piped_items, storage_exit = self._process_storage_items(
                piped_items=piped_items,
                parsed=parsed,
                config=config,
                final_output_dir=final_output_dir,
            )
            downloaded_count += int(storage_downloaded)
            if storage_exit is not None:
                return int(storage_exit)

            if skipped_dupe_count and not raw_url and not piped_items:
                return 0 if downloaded_count > 0 else 0

            urls_downloaded, early_exit = self._process_explicit_urls(
                raw_urls=raw_url,
                final_output_dir=final_output_dir,
                config=config,
                quiet_mode=quiet_mode,
                registry=registry,
                progress=progress,
                parsed=parsed,
                args=args,
                context_items=(result if isinstance(result, list) else ([result] if result else [])),
            )
            downloaded_count += int(urls_downloaded)
            if early_exit is not None:
                return int(early_exit)

            provider_downloaded, magnet_submissions = self._process_provider_items(
                piped_items=piped_items,
                final_output_dir=final_output_dir,
                config=config,
                quiet_mode=quiet_mode,
                registry=registry,
                progress=progress,
            )
            downloaded_count += provider_downloaded

            if downloaded_count > 0 or magnet_submissions > 0:
                return 0

            return 1

        except Exception as e:
            log(f"Error in download-file: {e}", file=sys.stderr)
            return 1

        finally:
            try:
                if isinstance(config, dict):
                    if had_progress_key:
                        config["_pipeline_progress"] = prev_progress
                    else:
                        config.pop("_pipeline_progress", None)
            except Exception:
                pass
            progress.close_local_ui(force_complete=True)
            try:
                self._download_run_depth = max(
                    0, int(getattr(self, "_download_run_depth", 1) or 1) - 1
                )
            except Exception:
                self._download_run_depth = 0
            if not getattr(self, "_download_run_depth", 0):
                try:
                    self._maybe_render_download_details(config=config)
                except Exception:
                    pass


# Late-binding: import sub-module functions and attach them as methods.
from .download_fetch import (  # noqa: E402
    _emit_plugin_items,
    _consume_plugin_download_result,
    _process_explicit_urls,
    _expand_provider_items,
    _process_provider_items,
    _download_provider_items,
    _emit_local_file,
    _resolve_provider_preflight_items,
    _build_provider_playlist_item_selector,
    _format_timecode,
    _rebase_subtitle_timestamp_text,
    _format_clip_range,
    _apply_clip_decorations,
    _parse_clip_spec_to_ranges,
)

from .download_storage import (  # noqa: E402
    _iter_storage_export_refs,
    _export_store_file,
    _process_storage_items,
    _process_explicit_local_sources,
    _extract_hash_from_search_hit,
    _iter_duplicate_tag_values,
    _extract_duplicate_namespace_tags,
    _extract_duplicate_title_tag,
    _extract_duplicate_title,
    _has_duplicate_title,
    _build_duplicate_display_row,
    _fetch_duplicate_metadata_for_hash,
    _enrich_duplicate_metadata,
    _fetch_duplicate_metadata_for_hashes,
    _collect_existing_url_match_refs_for_url,
    _find_existing_url_matches_for_url,
    _find_existing_hash_for_url,
    _find_existing_hashes_for_url,
    _init_storage,
    _supports_storage_duplicate_lookup,
    _filter_supported_urls,
    _canonicalize_url_for_storage,
    _preflight_url_duplicate,
    _preflight_explicit_url_duplicates,
    _download_supported_urls,
    _maybe_show_playlist_table,
    _maybe_show_format_table_for_single_url,
    _run_streaming_urls,
)

Download_File._emit_plugin_items = _emit_plugin_items
Download_File._consume_plugin_download_result = _consume_plugin_download_result
Download_File._process_explicit_urls = _process_explicit_urls
Download_File._expand_provider_items = _expand_provider_items
Download_File._process_provider_items = _process_provider_items
Download_File._download_provider_items = _download_provider_items
Download_File._emit_local_file = _emit_local_file
Download_File._resolve_provider_preflight_items = staticmethod(_resolve_provider_preflight_items)
Download_File._build_provider_playlist_item_selector = staticmethod(_build_provider_playlist_item_selector)
Download_File._format_timecode = staticmethod(_format_timecode)
Download_File._rebase_subtitle_timestamp_text = staticmethod(_rebase_subtitle_timestamp_text)
Download_File._format_clip_range = _format_clip_range
Download_File._apply_clip_decorations = _apply_clip_decorations
Download_File._parse_clip_spec_to_ranges = staticmethod(_parse_clip_spec_to_ranges)

Download_File._iter_storage_export_refs = staticmethod(_iter_storage_export_refs)
Download_File._export_store_file = _export_store_file
Download_File._process_storage_items = _process_storage_items
Download_File._process_explicit_local_sources = _process_explicit_local_sources
Download_File._extract_hash_from_search_hit = _extract_hash_from_search_hit
Download_File._iter_duplicate_tag_values = staticmethod(_iter_duplicate_tag_values)
Download_File._extract_duplicate_namespace_tags = staticmethod(_extract_duplicate_namespace_tags)
Download_File._extract_duplicate_title_tag = staticmethod(_extract_duplicate_title_tag)
Download_File._extract_duplicate_title = _extract_duplicate_title
Download_File._has_duplicate_title = _has_duplicate_title
Download_File._build_duplicate_display_row = _build_duplicate_display_row
Download_File._fetch_duplicate_metadata_for_hash = _fetch_duplicate_metadata_for_hash
Download_File._enrich_duplicate_metadata = _enrich_duplicate_metadata
Download_File._fetch_duplicate_metadata_for_hashes = _fetch_duplicate_metadata_for_hashes
Download_File._collect_existing_url_match_refs_for_url = _collect_existing_url_match_refs_for_url
Download_File._find_existing_url_matches_for_url = _find_existing_url_matches_for_url
Download_File._find_existing_hash_for_url = _find_existing_hash_for_url
Download_File._find_existing_hashes_for_url = _find_existing_hashes_for_url
Download_File._init_storage = staticmethod(_init_storage)
Download_File._supports_storage_duplicate_lookup = staticmethod(_supports_storage_duplicate_lookup)
Download_File._filter_supported_urls = staticmethod(_filter_supported_urls)
Download_File._canonicalize_url_for_storage = staticmethod(_canonicalize_url_for_storage)
Download_File._preflight_url_duplicate = staticmethod(_preflight_url_duplicate)
Download_File._preflight_explicit_url_duplicates = _preflight_explicit_url_duplicates
Download_File._download_supported_urls = _download_supported_urls
Download_File._maybe_show_playlist_table = _maybe_show_playlist_table
Download_File._maybe_show_format_table_for_single_url = _maybe_show_format_table_for_single_url
Download_File._run_streaming_urls = _run_streaming_urls

CMDLET = Download_File()
