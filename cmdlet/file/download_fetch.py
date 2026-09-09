"""URL/plugin fetching, streaming, and emission for download-file."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence
from collections.abc import Sequence as SequenceABC
from pathlib import Path
import sys

from SYS.logger import log, debug_panel
from SYS.pipeline_progress import PipelineProgress
from SYS import pipeline as pipeline_context
from SYS.utils import sha256_file
from API.HTTP import download_direct_file

from .. import _shared as sh

from .download_core import Download_File

coerce_to_path = sh.coerce_to_path
coerce_to_pipe_object = sh.coerce_to_pipe_object
register_url_with_local_library = sh.register_url_with_local_library
get_field = sh.get_field


def _emit_plugin_items(
    self,
    *,
    items: Sequence[Any],
    config: Dict[str, Any],
) -> int:
    emitted = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        pipeline_context.emit(item)
        if item.get("url"):
            try:
                pipe_obj = coerce_to_pipe_object(item)
                register_url_with_local_library(pipe_obj, config)
            except Exception:
                pass
        emitted += 1
        try:
            self._record_download_item(item)
        except Exception:
            pass
    return emitted


def _consume_plugin_download_result(
    self,
    *,
    result: Any,
    config: Dict[str, Any],
) -> tuple[int, Optional[int], bool]:
    if result is None:
        return 0, None, False

    if isinstance(result, list):
        if result and all(isinstance(item, dict) for item in result):
            return _emit_plugin_items(self, items=result, config=config), 0, True
        return 0, None, False

    if not isinstance(result, dict):
        return 0, None, False

    action = str(
        result.get("action")
        or result.get("plugin_action")
        or ""
    ).strip().lower()

    if action in {"emit_items", "emit_pipe_objects"}:
        items = result.get("items") or []
        exit_code = result.get("exit_code")
        emitted = _emit_plugin_items(
            self,
            items=items if isinstance(items, list) else [],
            config=config,
        )
        try:
            normalized_exit = int(exit_code) if exit_code is not None else 0
        except Exception:
            normalized_exit = 0
        return emitted, normalized_exit, True

    if action == "handled":
        exit_code = result.get("exit_code")
        try:
            normalized_exit = int(exit_code) if exit_code is not None else 0
        except Exception:
            normalized_exit = 0
        try:
            downloaded = int(result.get("downloaded") or 0)
        except Exception:
            downloaded = 0
        return downloaded, normalized_exit, True

    return 0, None, False


def _emit_local_file(
    self,
    *,
    downloaded_path: Path,
    source: Optional[str],
    title_hint: Optional[str],
    tags_hint: Optional[List[str]],
    media_kind_hint: Optional[str],
    full_metadata: Optional[Dict[str, Any]],
    progress: PipelineProgress,
    config: Dict[str, Any],
    provider_hint: Optional[str] = None,
) -> None:
    title_val = (title_hint or downloaded_path.stem or "Unknown").strip() or downloaded_path.stem
    hash_value = sha256_file(downloaded_path)
    notes: Optional[Dict[str, str]] = None
    try:
        if isinstance(full_metadata, dict):
            _provider_notes = full_metadata.get("_notes")
            if isinstance(_provider_notes, dict) and _provider_notes:
                notes = {str(k): str(v) for k, v in _provider_notes.items() if k and v}
    except Exception:
        notes = None
    tag: List[str] = []
    if tags_hint:
        tag.extend([str(t) for t in tags_hint if t])
    if not any(str(t).lower().startswith("title:") for t in tag):
        tag.insert(0, f"title:{title_val}")

    payload: Dict[str, Any] = {
        "path": str(downloaded_path),
        "hash": hash_value,
        "title": title_val,
        "action": "cmdlet:download-file",
        "download_mode": "file",
        "store": "local",
        "media_kind": media_kind_hint or "file",
        "tag": tag,
    }
    if provider_hint:
        payload["plugin"] = str(provider_hint)
    if full_metadata:
        payload["metadata"] = full_metadata
    if notes:
        payload["notes"] = notes
    if source and str(source).startswith("http"):
        payload["url"] = source
    elif source:
        payload["source_url"] = source

    pipeline_context.emit(payload)
    try:
        self._record_download_item(payload)
    except Exception:
        pass


def _expand_provider_items(
    self,
    *,
    piped_items: Sequence[Any],
    registry: Dict[str, Any],
    config: Dict[str, Any],
) -> List[Any]:
    get_provider = registry.get("get_plugin")
    expanded_items: List[Any] = []

    for item in piped_items:
        try:
            provider_key = self._provider_key_from_item(item)
            provider = get_provider(provider_key, config) if provider_key and get_provider else None

            if provider and hasattr(provider, "expand_item") and callable(provider.expand_item):
                try:
                    sub_items = provider.expand_item(item)
                    if sub_items:
                        expanded_items.extend(sub_items)
                        continue
                except Exception as e:
                    debug_panel(
                        "download-file expand_item failed",
                        [
                            ("plugin", provider_key),
                            ("error", e),
                        ],
                        border_style="yellow",
                    )

            expanded_items.append(item)
        except Exception:
            expanded_items.append(item)

    return expanded_items


def _download_provider_items(
    self,
    *,
    provider: Any,
    provider_name: str,
    search_result: Any,
    output_dir: Path,
    progress: PipelineProgress,
    quiet_mode: bool,
    config: Dict[str, Any],
) -> int:
    if provider is None or not hasattr(provider, "download_items"):
        return 0

    def _on_emit(path: Path, file_url: str, relpath: str, metadata: Dict[str, Any]) -> None:
        title_hint = None
        try:
            title_hint = metadata.get("name") or relpath
        except Exception:
            title_hint = relpath
        title_hint = title_hint or (Path(path).name if path else "download")

        _emit_local_file(
            self,
            downloaded_path=path,
            source=file_url,
            title_hint=title_hint,
            tags_hint=None,
            media_kind_hint="file",
            full_metadata=metadata if isinstance(metadata, dict) else None,
            progress=progress,
            config=config,
            provider_hint=provider_name,
        )

    try:
        downloaded_count = provider.download_items(
            search_result,
            output_dir,
            emit=_on_emit,
            progress=progress,
            quiet_mode=quiet_mode,
            path_from_result=coerce_to_path,
            config=config,
        )
    except TypeError:
        downloaded_count = provider.download_items(
            search_result,
            output_dir,
            emit=_on_emit,
            progress=progress,
            quiet_mode=quiet_mode,
            path_from_result=coerce_to_path,
        )
    except Exception as exc:
        log(f"Provider {provider_name} download_items error: {exc}", file=sys.stderr)
        return 0

    try:
        return int(downloaded_count or 0)
    except Exception:
        return 0


def _process_explicit_urls(
    self,
    *,
    raw_urls: Sequence[str],
    final_output_dir: Path,
    config: Dict[str, Any],
    quiet_mode: bool,
    registry: Dict[str, Any],
    progress: PipelineProgress,
    parsed: Dict[str, Any],
    args: Sequence[str],
    context_items: Sequence[Any] = (),
) -> tuple[int, Optional[int]]:
    downloaded_count = 0
    skipped_duplicate_only = 0
    attempted_download = False
    suppress_nested, batch_total, batch_index, batch_label = self._batch_progress_state(config)
    total_urls = len(raw_urls or [])

    try:
        if total_urls > 1 and not suppress_nested:
            progress.begin_pipe(total_items=total_urls, items_preview=list(raw_urls[:5]))
    except Exception:
        pass

    SearchResult = registry.get("SearchResult")
    get_plugin = registry.get("get_plugin")
    match_plugin_name_for_url = registry.get("match_plugin_name_for_url")

    for idx, url in enumerate(raw_urls, 1):
        try:
            try:
                display_total = batch_total if batch_total > 0 else total_urls
                display_index = batch_index if batch_total > 0 else idx
                display_label = batch_label or str(url)
                if display_total > 0:
                    progress.set_status(
                        f"downloading {display_index}/{display_total}: {display_label}"
                    )
            except Exception:
                pass

            provider_name = None
            if match_plugin_name_for_url:
                try:
                    provider_name = match_plugin_name_for_url(str(url))
                except Exception:
                    pass

            provider = None
            if provider_name and get_plugin:
                provider = get_plugin(provider_name, config)

            if provider:
                try:
                    handled = False
                    if hasattr(provider, "handle_url"):
                        try:
                            handled, path = provider.handle_url(str(url), output_dir=final_output_dir)
                            if handled:
                                extra_meta = None
                                title_hint = None
                                tags_hint: Optional[List[str]] = None
                                media_kind_hint = None
                                path_value: Optional[Any] = path

                                if isinstance(path, dict):
                                    plugin_action = str(
                                        path.get("action")
                                        or path.get("plugin_action")
                                        or ""
                                    ).strip().lower()
                                    if plugin_action == "download_items" or bool(path.get("download_items")):
                                        request_metadata = path.get("metadata") or path.get("full_metadata") or {}
                                        if not isinstance(request_metadata, dict):
                                            request_metadata = {}
                                        magnet_id = path.get("magnet_id") or request_metadata.get("magnet_id")
                                        if magnet_id is not None:
                                            request_metadata.setdefault("magnet_id", magnet_id)

                                        if SearchResult is None:
                                            continue

                                        sr = SearchResult(
                                            table=str(provider_name),
                                            title=str(path.get("title") or path.get("name") or f"{provider_name} item"),
                                            path=str(path.get("path") or path.get("url") or url),
                                            full_metadata=request_metadata,
                                        )
                                        downloaded_extra = _download_provider_items(
                                            self,
                                            provider=provider,
                                            provider_name=str(provider_name),
                                            search_result=sr,
                                            output_dir=final_output_dir,
                                            progress=progress,
                                            quiet_mode=quiet_mode,
                                            config=config,
                                        )
                                        if downloaded_extra:
                                            downloaded_count += int(downloaded_extra)
                                        continue

                                    plugin_downloaded, plugin_exit, plugin_handled = _consume_plugin_download_result(
                                        self,
                                        result=path,
                                        config=config,
                                    )
                                    if plugin_handled:
                                        downloaded_count += plugin_downloaded
                                        if plugin_exit is not None and plugin_downloaded == 0:
                                            return downloaded_count, int(plugin_exit)
                                        if plugin_downloaded:
                                            continue

                                    path_value = path.get("path") or path.get("file_path")
                                    extra_meta = path.get("metadata") or path.get("full_metadata")
                                    title_hint = path.get("title") or path.get("name")
                                    media_kind_hint = path.get("media_kind")
                                    tags_val = path.get("tags") or path.get("tag")
                                    if isinstance(tags_val, (list, tuple, set)):
                                        tags_hint = [str(t) for t in tags_val if t]
                                    elif isinstance(tags_val, str) and tags_val.strip():
                                        tags_hint = [str(tags_val).strip()]

                                if path_value:
                                    p_val = Path(str(path_value))
                                    if not title_hint and isinstance(extra_meta, dict):
                                        title_hint = extra_meta.get("title") or extra_meta.get("name")

                                    _emit_local_file(
                                        self,
                                        downloaded_path=p_val,
                                        source=str(url),
                                        title_hint=str(title_hint) if title_hint else p_val.stem,
                                        tags_hint=tags_hint,
                                        media_kind_hint=str(media_kind_hint) if media_kind_hint else "file",
                                        full_metadata=extra_meta,
                                        progress=progress,
                                        config=config,
                                        provider_hint=provider_name
                                    )
                                    downloaded_count += 1
                                continue
                        except Exception as e:
                            debug_panel(
                                "download-file provider error",
                                [
                                    ("plugin", provider_name),
                                    ("url", url),
                                    ("operation", "handle_url"),
                                    ("error", e),
                                ],
                                border_style="yellow",
                            )

                    if not handled and hasattr(provider, "download_url"):
                        parsed_for_provider = parsed
                        provider_preflight_items = _resolve_provider_preflight_items(
                            provider,
                            url=str(url),
                            parsed=parsed,
                            args=args,
                        )
                        if provider_preflight_items:
                            provider_preflight_urls = [
                                str(item.get("url") or "").strip()
                                for item in provider_preflight_items
                                if str(item.get("url") or "").strip()
                            ]
                            provider_preflight_urls, preflight_exit, provider_skipped = self._preflight_explicit_url_duplicates(
                                raw_urls=provider_preflight_urls,
                                config=config,
                            )
                            if preflight_exit is not None:
                                return downloaded_count, int(preflight_exit)
                            if provider_skipped:
                                if not provider_preflight_urls:
                                    skipped_duplicate_only += 1
                                    continue
                                selector = _build_provider_playlist_item_selector(
                                    provider_preflight_items,
                                    remaining_urls=provider_preflight_urls,
                                )
                                if selector:
                                    parsed_for_provider = dict(parsed)
                                    parsed_for_provider["item"] = selector
                        try:
                            attempted_download = True
                            res = provider.download_url(
                                str(url),
                                final_output_dir,
                                parsed=parsed_for_provider,
                                args=list(args),
                                progress=progress,
                                quiet_mode=quiet_mode,
                                context_items=list(context_items or []),
                            )
                        except TypeError:
                            attempted_download = True
                            res = provider.download_url(str(url), final_output_dir)

                        plugin_downloaded, plugin_exit, plugin_handled = _consume_plugin_download_result(
                            self,
                            result=res,
                            config=config,
                        )
                        if plugin_handled:
                            downloaded_count += plugin_downloaded
                            if plugin_exit is not None and plugin_downloaded == 0:
                                return downloaded_count, int(plugin_exit)
                            if plugin_downloaded:
                                continue

                        if res:
                            p_val = None
                            extra_meta = None
                            if isinstance(res, (str, Path)):
                                p_val = Path(res)
                            elif isinstance(res, tuple) and len(res) > 0:
                                p_val = Path(res[0])
                                if len(res) > 1 and isinstance(res[1], dict):
                                    extra_meta = res[1]
                            elif isinstance(res, dict):
                                path_candidate = res.get("path") or res.get("file_path")
                                if path_candidate:
                                    p_val = Path(path_candidate)
                                    extra_meta = res

                            if p_val:
                                _emit_local_file(
                                    self,
                                    downloaded_path=p_val,
                                    source=str(url),
                                    title_hint=p_val.stem,
                                    tags_hint=None,
                                    media_kind_hint=extra_meta.get("media_kind") if extra_meta else "file",
                                    full_metadata=extra_meta,
                                    provider_hint=provider_name,
                                    progress=progress,
                                    config=config,
                                )
                                downloaded_count += 1
                                continue

                except Exception as e:
                    log(f"Provider {provider_name} error handling {url}: {e}", file=sys.stderr)
                    pass

                if not handled:
                    continue

            # Direct Download Fallback
            attempted_download = True
            result_obj = download_direct_file(
                str(url),
                final_output_dir,
                quiet=quiet_mode,
                pipeline_progress=progress,
            )
            downloaded_path = self._path_from_download_result(result_obj)

            _emit_local_file(
                self,
                downloaded_path=downloaded_path,
                source=str(url),
                title_hint=downloaded_path.stem,
                tags_hint=[f"title:{downloaded_path.stem}"],
                media_kind_hint="file",
                full_metadata=None,
                progress=progress,
                config=config,
            )
            downloaded_count += 1

        except Exception as e:
            log(f"Download failed for {url}: {e}", file=sys.stderr)

    if downloaded_count == 0 and skipped_duplicate_only > 0 and not attempted_download:
        return downloaded_count, 0
    return downloaded_count, None


def _process_provider_items(self,
        *,
        piped_items: Sequence[Any],
        final_output_dir: Path,
        config: Dict[str, Any],
        quiet_mode: bool,
        registry: Dict[str, Any],
        progress: PipelineProgress,
    ) -> tuple[int, int]:
    downloaded_count = 0
    queued_magnet_submissions = 0
    get_provider = registry.get("get_plugin")
    SearchResult = registry.get("SearchResult")

    expanded_items = _expand_provider_items(
        self,
        piped_items=piped_items,
        registry=registry,
        config=config
    )

    total_items = len(expanded_items)
    processed_items = 0

    try:
        if total_items:
            progress.set_percent(0)
    except Exception:
        pass

    for idx, item in enumerate(expanded_items, 1):
        try:
            label = "item"
            table = get_field(item, "table")
            title = get_field(item, "title")
            target = get_field(item, "path") or get_field(item, "url")

            media_kind = get_field(item, "media_kind")
            tags_val = get_field(item, "tag")
            tags_list: Optional[List[str]]
            if isinstance(tags_val, (list, set)):
                tags_list = sorted([str(t) for t in tags_val if t])
            else:
                tags_list = None

            full_metadata = get_field(item, "full_metadata")
            if ((not full_metadata) and isinstance(item, dict)
                    and isinstance(item.get("extra"), dict)):
                extra_md = item["extra"].get("full_metadata")
                if isinstance(extra_md, dict):
                    full_metadata = extra_md

            try:
                label = title or target
                label = str(label or "item").strip()
                if total_items:
                    pct = int(round((processed_items / max(1, total_items)) * 100))
                    progress.set_percent(pct)
                    progress.set_status(
                        f"downloading {processed_items + 1}/{total_items}: {label}"
                    )
            except Exception:
                pass

            transfer_label = label

            downloaded_path: Optional[Path] = None
            attempted_provider_download = False
            provider_sr = None
            provider_obj = None
            provider_key = self._provider_key_from_item(item)
            if provider_key and get_provider and SearchResult:
                provider_obj = get_provider(provider_key, config)

            if provider_obj is not None and getattr(provider_obj, "prefers_transfer_progress", False):
                try:
                    progress.begin_transfer(label=transfer_label, total=None)
                except Exception:
                    pass

            if provider_obj is not None:
                attempted_provider_download = True
                sr = SearchResult(
                    table=str(table),
                    title=str(title or "Unknown"),
                    path=str(target or ""),
                    tag=set(tags_list) if tags_list else set(),
                    media_kind=str(media_kind or "file"),
                    full_metadata=full_metadata
                    if isinstance(full_metadata, dict) else {},
                )

                output_dir = final_output_dir

                downloaded_path = provider_obj.download(sr, output_dir)
                provider_sr = sr

                if downloaded_path is None:
                    try:
                        downloaded_extra = _download_provider_items(
                            self,
                            provider=provider_obj,
                            provider_name=str(provider_key),
                            search_result=sr,
                            output_dir=output_dir,
                            progress=progress,
                            quiet_mode=quiet_mode,
                            config=config,
                        )
                    except Exception:
                        downloaded_extra = 0

                    if downloaded_extra:
                        downloaded_count += int(downloaded_extra)
                        continue

            magnet_spec = None
            try:
                cand = str(target or "").strip()
                if cand.lower().startswith("magnet:"):
                    magnet_spec = cand
                elif isinstance(full_metadata, dict):
                    extra_mag = str(full_metadata.get("magnet") or "").strip()
                    if extra_mag.lower().startswith("magnet:"):
                        magnet_spec = extra_mag
            except Exception:
                magnet_spec = None

            if (
                downloaded_path is None
                and magnet_spec
                and provider_obj is not None
                and bool(getattr(provider_obj, "_libtorrent_queued", False))
            ):
                queued_magnet_submissions += 1
                try:
                    provider_obj._libtorrent_queued = False
                except Exception:
                    pass
                continue

            if downloaded_path is None and magnet_spec and get_provider:
                ald = None
                try:
                    ald = get_provider("alldebrid", config)
                except Exception:
                    ald = None
                if ald is not None and hasattr(ald, "handle_url"):
                    try:
                        handled, payload = ald.handle_url(
                            magnet_spec,
                            output_dir=final_output_dir,
                        )
                    except TypeError:
                        handled, payload = ald.handle_url(magnet_spec)
                    except Exception:
                        handled, payload = False, None
                    if handled:
                        plugin_downloaded, plugin_exit, plugin_handled = _consume_plugin_download_result(
                            self,
                            result=payload,
                            config=config,
                        )
                        if plugin_handled:
                            downloaded_count += plugin_downloaded
                            if plugin_downloaded == 0:
                                queued_magnet_submissions += 1
                            continue

            if (downloaded_path is None and not attempted_provider_download
                    and isinstance(target, str) and target.startswith("http")):

                suggested_name = str(title).strip() if title is not None else None
                result_obj = download_direct_file(
                    target,
                    final_output_dir,
                    quiet=quiet_mode,
                    suggested_filename=suggested_name,
                    pipeline_progress=progress,
                )
                downloaded_path = coerce_to_path(result_obj)

            if downloaded_path is None:
                debug_panel(
                    "download",
                    [
                        ("error", "no provider handler / unsupported target"),
                        ("title", title or ""),
                        ("target", target),
                    ],
                )
                continue

            if provider_sr is not None:
                try:
                    sr_md = getattr(provider_sr, "full_metadata", None)
                    if isinstance(sr_md, dict) and sr_md:
                        full_metadata = sr_md
                except Exception:
                    pass

                try:
                    if isinstance(full_metadata, dict):
                        t = str(full_metadata.get("title") or "").strip()
                        if t:
                            title = t
                except Exception:
                    pass

                try:
                    sr_tags = getattr(provider_sr, "tag", None)
                    if isinstance(sr_tags, (set, list)) and sr_tags:
                        tags_list = sorted([str(t) for t in sr_tags if t])
                except Exception:
                    pass

            _emit_local_file(
                self,
                downloaded_path=downloaded_path,
                source=str(target) if target else None,
                title_hint=str(title) if title else downloaded_path.stem,
                tags_hint=tags_list,
                media_kind_hint=str(media_kind) if media_kind else None,
                full_metadata=full_metadata if isinstance(full_metadata, dict) else None,
                progress=progress,
                config=config,
                provider_hint=provider_key
            )
            downloaded_count += 1

        except Exception as e:
            log(f"Download failed: {e}", file=sys.stderr)
        finally:
            if provider_obj is not None and getattr(provider_obj, "prefers_transfer_progress", False):
                try:
                    progress.finish_transfer(label=transfer_label)
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

    return downloaded_count, queued_magnet_submissions


@staticmethod
def _resolve_provider_preflight_items(
    provider: Any,
    *,
    url: str,
    parsed: Dict[str, Any],
    args: Sequence[str],
) -> List[Dict[str, Any]]:
    resolver = getattr(provider, "resolve_preflight_items", None)
    if not callable(resolver):
        return []

    try:
        items = resolver(url, parsed=parsed, args=list(args))
    except TypeError:
        try:
            items = resolver(url)
        except Exception:
            items = None
    except Exception:
        items = None

    if not isinstance(items, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        item_url = str(item.get("url") or "").strip()
        if not item_url:
            continue
        playlist_index = item.get("playlist_index")
        try:
            playlist_index_value = int(playlist_index)
        except Exception:
            playlist_index_value = idx
        normalized.append(
            {
                "url": item_url,
                "playlist_index": playlist_index_value,
            }
        )

    return normalized


@staticmethod
def _build_provider_playlist_item_selector(
    items: Sequence[Dict[str, Any]],
    *,
    remaining_urls: Sequence[str],
) -> Optional[str]:
    allowed_urls = {
        str(url or "").strip() for url in (remaining_urls or []) if str(url or "").strip()
    }
    if not allowed_urls:
        return None

    selectors: List[str] = []
    for idx, item in enumerate(items, 1):
        item_url = str(item.get("url") or "").strip()
        if not item_url or item_url not in allowed_urls:
            continue
        playlist_index = item.get("playlist_index")
        try:
            playlist_index_value = int(playlist_index)
        except Exception:
            playlist_index_value = idx
        if playlist_index_value <= 0:
            continue
        selectors.append(str(playlist_index_value))

    if not selectors:
        return None
    return ",".join(selectors)


@staticmethod
def _format_timecode(seconds: int, *, force_hours: bool) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if force_hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


@staticmethod
def _rebase_subtitle_timestamp_text(text: str, offset_seconds: int) -> str:
    import re as _re

    if not text:
        return text

    try:
        offset_value = float(offset_seconds)
    except Exception:
        return text

    if offset_value <= 0:
        return text

    timestamp_re = _re.compile(r"(?<!\d)(?P<ts>(?:\d{2}:)?\d{2}:\d{2}(?:[\\.,]\d{1,3})?)(?!\d)")

    def _shift(match) -> str:
        original = str(match.group("ts") or "")
        if not original:
            return original

        frac_sep = "."
        frac_digits = 0
        base = original
        frac_seconds = 0.0
        if "." in original:
            base, frac = original.split(".", 1)
            frac_sep = "."
            frac_digits = len(frac)
            try:
                frac_seconds = float(f"0.{frac}") if frac else 0.0
            except Exception:
                frac_seconds = 0.0
        elif "," in original:
            base, frac = original.split(",", 1)
            frac_sep = ","
            frac_digits = len(frac)
            try:
                frac_seconds = float(f"0.{frac}") if frac else 0.0
            except Exception:
                frac_seconds = 0.0

        parts = base.split(":")
        if len(parts) == 3:
            hours_s, minutes_s, seconds_s = parts
            include_hours = True
        elif len(parts) == 2:
            hours_s = "0"
            minutes_s, seconds_s = parts
            include_hours = False
        else:
            return original

        try:
            total = (
                (int(hours_s) * 3600)
                + (int(minutes_s) * 60)
                + int(seconds_s)
                + frac_seconds
                + offset_value
            )
        except Exception:
            return original

        total = max(0.0, total)
        whole_seconds = int(total)
        fraction = total - whole_seconds
        hours, remainder = divmod(whole_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        if frac_digits > 0:
            scale = 10 ** frac_digits
            frac_value = int(round(fraction * scale))
            if frac_value >= scale:
                frac_value = 0
                seconds += 1
                if seconds >= 60:
                    seconds = 0
                    minutes += 1
                    if minutes >= 60:
                        minutes = 0
                        hours += 1
            frac_text = f"{frac_value:0{frac_digits}d}"
            if include_hours or hours > 0:
                return f"{hours:02d}:{minutes:02d}:{seconds:02d}{frac_sep}{frac_text}"
            return f"{minutes:02d}:{seconds:02d}{frac_sep}{frac_text}"

        if include_hours or hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    try:
        return timestamp_re.sub(_shift, str(text))
    except Exception:
        return text


@classmethod
def _format_clip_range(cls, start_s: int, end_s: int) -> str:
    force_hours = bool(start_s >= 3600 or end_s >= 3600)
    return f"{_format_timecode(start_s, force_hours=force_hours)}-{_format_timecode(end_s, force_hours=force_hours)}"


@classmethod
def _apply_clip_decorations(
    cls, pipe_objects: List[Dict[str, Any]], clip_ranges: List[tuple[int, int]], *, source_king_hash: Optional[str]
) -> None:
    if not pipe_objects or len(pipe_objects) != len(clip_ranges):
        return

    for po, (start_s, end_s) in zip(pipe_objects, clip_ranges):
        clip_range = cls._format_clip_range(start_s, end_s)
        clip_tag = f"clip:{clip_range}"

        po["title"] = clip_tag

        tags = po.get("tag")
        if not isinstance(tags, list):
            tags = []

        tags = [t for t in tags if not str(t).strip().lower().startswith("title:")]
        tags = [t for t in tags if not str(t).strip().lower().startswith("relationship:")]
        tags.insert(0, f"title:{clip_tag}")

        if clip_tag not in tags:
            tags.append(clip_tag)

        po["tag"] = tags

        notes = po.get("notes")
        if isinstance(notes, dict):
            sub_text = notes.get("sub")
            if isinstance(sub_text, str) and sub_text.strip():
                notes["sub"] = _rebase_subtitle_timestamp_text(sub_text, start_s)
                po["notes"] = notes

    if len(pipe_objects) < 2:
        return

    hashes: List[str] = []
    for po in pipe_objects:
        h_val = sh.normalize_hash(str(po.get("hash") or ""))
        hashes.append(h_val or "")

    king_hash = sh.normalize_hash(source_king_hash) if source_king_hash else None
    if not king_hash:
        king_hash = hashes[0] if hashes and hashes[0] else None
    if not king_hash:
        return

    alt_hashes: List[str] = [h for h in hashes if h and h != king_hash]
    if not alt_hashes:
        return

    for po in pipe_objects:
        po["relationships"] = {"king": [king_hash], "alt": list(alt_hashes)}


@staticmethod
def _parse_clip_spec_to_ranges(clip_spec: Optional[str]) -> Optional[List[tuple[int, int]]]:
    """Parse clip spec strings like '2m-2m20s,5m-6m'."""
    text = str(clip_spec or "").strip()
    if not text:
        return None

    def _parse_time(value: str) -> Optional[int]:
        s = str(value or "").strip().lower()
        if not s:
            return None
        try:
            if ":" in s:
                parts = [int(p) for p in s.split(":")]
                if len(parts) == 2:
                    return (parts[0] * 60) + parts[1]
                if len(parts) == 3:
                    return (parts[0] * 3600) + (parts[1] * 60) + parts[2]
                return None
            total = 0
            number = ""
            units_seen = False
            for ch in s:
                if ch.isdigit():
                    number += ch
                    continue
                if ch in {"h", "m", "s"} and number:
                    units_seen = True
                    val = int(number)
                    if ch == "h":
                        total += val * 3600
                    elif ch == "m":
                        total += val * 60
                    else:
                        total += val
                    number = ""
                    continue
                return None
            if number:
                total += int(number)
            if total == 0 and units_seen:
                return 0
            return total if total >= 0 else None
        except Exception:
            return None

    ranges: List[tuple[int, int]] = []
    for chunk in [c.strip() for c in text.split(",") if c.strip()]:
        if "-" not in chunk:
            return None
        left, right = chunk.split("-", 1)
        start = _parse_time(left)
        end = _parse_time(right)
        if start is None or end is None or end < start:
            return None
        ranges.append((start, end))
    return ranges or None
