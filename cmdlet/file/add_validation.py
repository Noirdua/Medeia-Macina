"""File validation, path resolution, and source fetching for add-file."""

from __future__ import annotations

from typing import Any, Dict, Optional, List
from pathlib import Path
import sys
import tempfile
import shutil
from urllib.parse import urlparse

from SYS import models
from SYS import pipeline as ctx
from SYS.logger import log, debug
from SYS.pipeline_progress import PipelineProgress
from SYS.utils import sha256_file, sanitize_filename
from API.HTTP import download_direct_file

from .. import _shared as sh

# Import from add_core (safe: add_core defines these before importing this module)
from .add_core import (
    Add_File,
    _CommandDependencies,
    SUPPORTED_MEDIA_EXTENSIONS,
    _REMOTE_URL_PREFIXES,
)

coerce_to_path = sh.coerce_to_path
get_field = sh.get_field


def _resolve_backend_by_name(instance: Any, backend_name: str) -> Optional[Any]:
    if not instance or not backend_name:
        return None
    try:
        return instance[backend_name]
    except Exception:
        pass
    target = str(backend_name or "").strip().lower()
    if not target:
        return None
    try:
        for candidate in instance.list_backends():
            if isinstance(candidate, str) and candidate.strip().lower() == target:
                try:
                    return instance[candidate]
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _build_provider_filename(
    pipe_obj: models.PipeObject,
    fallback_hash: Optional[str] = None,
    source_url: Optional[str] = None,
) -> str:
    title_candidates: List[str] = []
    title_value = getattr(pipe_obj, "title", "")
    if title_value:
        title_candidates.append(str(title_value))

    extra = getattr(pipe_obj, "extra", {})
    if isinstance(extra, dict):
        candid = extra.get("name") or extra.get("title")
        if candid:
            title_candidates.append(str(candid))

    metadata = getattr(pipe_obj, "metadata", {})
    if isinstance(metadata, dict):
        meta_name = metadata.get("title") or metadata.get("name")
        if meta_name:
            title_candidates.append(str(meta_name))

    text = ""
    for candidate in title_candidates:
        if candidate:
            text = candidate.strip()
            if text:
                break

    if not text and fallback_hash:
        text = fallback_hash[:8]

    safe_name = sanitize_filename(text or "download")

    ext = ""
    if isinstance(metadata, dict):
        ext = metadata.get("ext") or metadata.get("extension") or ""
    if not ext and isinstance(extra, dict):
        ext = extra.get("ext") or ""
    if not ext and source_url:
        try:
            parsed = urlparse(source_url)
            ext = Path(parsed.path).suffix.lstrip(".")
        except Exception:
            ext = ""

    if ext:
        ext_text = str(ext)
        if not ext_text.startswith("."):
            ext_text = "." + ext_text.lstrip(".")
        if not safe_name.lower().endswith(ext_text.lower()):
            safe_name = f"{safe_name}{ext_text}"

    return safe_name or "download"


def _maybe_download_backend_file(
    backend: Any,
    file_hash: str,
    pipe_obj: models.PipeObject,
    *,
    output_dir: Optional[Path] = None,
) -> tuple[Optional[Path], Optional[Path]]:
    """Best-effort fetch of a backend file when get_file returns a URL.

    Returns (downloaded_path, temp_dir_to_cleanup).
    """

    downloader = getattr(backend, "download_to_temp", None)
    if not callable(downloader):
        return None, None

    tmp_dir: Optional[Path] = None
    try:
        suffix = None
        if pipe_obj.path:
            try:
                suffix = Path(pipe_obj.path).suffix
            except Exception:
                pass

        if not suffix:
            metadata = getattr(pipe_obj, "metadata", {})
            if isinstance(metadata, dict):
                suffix = metadata.get("ext")

        download_root = output_dir
        if download_root is None:
            tmp_dir = Path(tempfile.mkdtemp(prefix="add-file-src-"))
            download_root = tmp_dir
        if download_root is None:
            return None, None

        import inspect

        sig = inspect.signature(downloader)
        kwargs = {"temp_root": download_root}
        if "suffix" in sig.parameters:
            kwargs["suffix"] = suffix

        pipeline_progress = PipelineProgress(ctx)
        transfer_label = "peer transfer"
        try:
            transfer_label = str(getattr(pipe_obj, "title", "") or "").strip() or transfer_label
        except Exception:
            transfer_label = "peer transfer"
        if "pipeline_progress" in sig.parameters:
            kwargs["pipeline_progress"] = pipeline_progress
        if "transfer_label" in sig.parameters:
            kwargs["transfer_label"] = transfer_label
        if "progress_callback" in sig.parameters:

            def _cb(done, total):
                try:
                    total_val = int(total) if total is not None else None
                except Exception:
                    total_val = None
                try:
                    if int(done or 0) <= 0:
                        pipeline_progress.begin_transfer(
                            label=transfer_label,
                            total=total_val,
                        )
                except Exception:
                    pass
                try:
                    pipeline_progress.update_transfer(
                        label=transfer_label,
                        completed=int(done or 0),
                        total=total_val,
                    )
                except Exception:
                    pass

            kwargs["progress_callback"] = _cb

        downloaded = downloader(str(file_hash), **kwargs)

        if isinstance(downloaded, Path) and downloaded.exists():
            if output_dir is not None:
                pipe_obj.is_temp = False
                if isinstance(pipe_obj.extra, dict):
                    pipe_obj.extra["_direct_export_download"] = True
                else:
                    pipe_obj.extra = {"_direct_export_download": True}
                return downloaded, None
            pipe_obj.is_temp = True
            return downloaded, tmp_dir
    except Exception:
        pass

    if tmp_dir is not None:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
    return None, None


def _download_remote_backend_url(
    remote_url: str,
    pipe_obj: models.PipeObject,
    *,
    file_hash: Optional[str] = None,
    output_dir: Optional[Path] = None,
) -> tuple[Optional[Path], Optional[Path]]:
    """Best-effort fetch of a remote backend URL.

    Returns (downloaded_path, temp_dir_to_cleanup).
    When ``output_dir`` is provided, the file is downloaded directly there and no
    temp cleanup path is returned.
    """

    url_text = str(remote_url or "").strip()
    if not url_text:
        return None, None
    if not url_text.lower().startswith(_REMOTE_URL_PREFIXES):
        return None, None
    if not url_text.lower().startswith(("http://", "https://")):
        return None, None

    tmp_dir: Optional[Path] = None
    try:
        download_root = output_dir
        if download_root is None:
            tmp_dir = Path(tempfile.mkdtemp(prefix="add-file-src-"))
            download_root = tmp_dir

        suggested_name = _build_provider_filename(
            pipe_obj,
            fallback_hash=file_hash,
            source_url=url_text,
        )
        pipeline_progress = PipelineProgress(ctx)
        try:
            destination_label = str(download_root) if download_root is not None else "temporary workspace"
            pipeline_progress.set_status(f"downloading {suggested_name} to {destination_label}")
        except Exception:
            pass

        downloaded = download_direct_file(
            url_text,
            download_root,
            quiet=False,
            suggested_filename=suggested_name,
            pipeline_progress=pipeline_progress,
        )
        downloaded_path = getattr(downloaded, "path", None)
        if isinstance(downloaded_path, Path) and downloaded_path.exists():
            if output_dir is not None:
                pipe_obj.is_temp = False
                if isinstance(pipe_obj.extra, dict):
                    pipe_obj.extra["_direct_export_download"] = True
                else:
                    pipe_obj.extra = {"_direct_export_download": True}
                return downloaded_path, None

            pipe_obj.is_temp = True
            return downloaded_path, tmp_dir
    except Exception:
        pass
    finally:
        try:
            PipelineProgress(ctx).clear_status()
        except Exception:
            pass

    if tmp_dir is not None:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
    return None, None


def _maybe_download_plugin_result(
    result: Any,
    pipe_obj: models.PipeObject,
    config: Dict[str, Any],
    deps: Optional[_CommandDependencies] = None,
) -> tuple[Optional[Path], Optional[str], Optional[Path]]:
    plugin_key = None
    extra = getattr(pipe_obj, "extra", None) if pipe_obj is not None else None
    extra_table = extra.get("table") if isinstance(extra, dict) else None
    for source in (
        pipe_obj.plugin if pipe_obj is not None else None,
        get_field(result, "plugin"),
        get_field(result, "table"),
        extra_table,
    ):
        candidate = Add_File._normalize_provider_key(source)
        if candidate:
            plugin_key = candidate
            break

    if not plugin_key:
        url_hint = _remote_url_text(
            getattr(pipe_obj, "path", None)
            or getattr(pipe_obj, "url", None)
            or get_field(result, "path")
            or get_field(result, "url")
        )
        if url_hint:
            try:
                from PluginCore.registry import match_plugin_name_for_url

                plugin_key = Add_File._normalize_provider_key(match_plugin_name_for_url(url_hint))
            except Exception:
                plugin_key = None
    if not plugin_key:
        return None, None, None

    if deps is None:
        deps = _CommandDependencies(config)

    plugin = deps.get_plugin(plugin_key)
    if plugin is None:
        return None, None, None

    try:
        return plugin.resolve_pipe_result_download(result, pipe_obj)
    except Exception as exc:
        debug(f"[add-file] Plugin '{plugin_key}' download helper failed: {exc}")

    return None, None, None


def _download_piped_source(
    pipe_obj: models.PipeObject,
    config: Dict[str, Any],
    store_instance: Optional[Any],
    deps: Optional[_CommandDependencies] = None,
) -> tuple[Optional[Path], Optional[str], Optional[Path]]:
    r_hash = str(getattr(pipe_obj, "hash", None) or getattr(pipe_obj, "file_hash", None) or "").strip()
    r_store = str(getattr(pipe_obj, "store", None) or "").strip()
    if not (r_hash and r_store):
        return None, None, None

    if deps is None:
        deps = _CommandDependencies(config)

    backend_registry = store_instance or deps.get_backend_registry()
    backend = _resolve_backend_by_name(backend_registry, r_store) if backend_registry is not None else None
    if backend is None:
        return None, None, None

    try:
        source = backend.get_file(r_hash.lower())
        if isinstance(source, Path) and source.exists():
            pipe_obj.path = str(source)
            return source, str(r_hash), None
        if isinstance(source, str) and source.strip():
            dl_path, tmp_dir = _maybe_download_backend_file(
                backend, str(r_hash), pipe_obj
            )
            if dl_path and dl_path.exists():
                return dl_path, str(r_hash), tmp_dir
            source_url = str(source).strip()
            if source_url.lower().startswith(("http://", "https://")):
                download_dir = Path(tempfile.mkdtemp(prefix="add-file-src-"))
                try:
                    filename = _build_provider_filename(
                        pipe_obj,
                        str(r_hash),
                        source_url,
                    )
                    downloaded = download_direct_file(
                        source_url,
                        download_dir,
                        quiet=True,
                        suggested_filename=filename,
                    )
                    downloaded_path = downloaded.path
                    if downloaded_path and downloaded_path.exists():
                        pipe_obj.is_temp = True
                        pipe_obj.path = str(downloaded_path)
                        return downloaded_path, str(r_hash), download_dir
                except Exception as exc:
                    debug(f"[add-file] Provider download failed: {exc}")
                try:
                    shutil.rmtree(download_dir, ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass

    return None, None, None


def _scan_directory_for_files(directory: Path, compute_hash: bool = True) -> List[Dict[str, Any]]:
    """Scan a directory for supported media files and return list of file info dicts.

    Each dict contains:
    - path: Path object
    - name: filename
    - hash: sha256 hash (or None if compute_hash=False)
    - size: file size in bytes
    - ext: file extension
    """
    if not directory.exists() or not directory.is_dir():
        return []

    files_info: List[Dict[str, Any]] = []

    try:
        for item in directory.iterdir():
            if not item.is_file():
                continue

            ext = item.suffix.lower()
            if ext not in SUPPORTED_MEDIA_EXTENSIONS:
                continue

            file_hash = None
            if compute_hash:
                try:
                    file_hash = sha256_file(item)
                except Exception as exc:
                    debug(f"Failed to hash {item}: {exc}")
                    continue

            try:
                size = item.stat().st_size
            except Exception:
                size = 0

            files_info.append(
                {
                    "path": item,
                    "name": item.name,
                    "hash": file_hash,
                    "size": size,
                    "ext": ext,
                }
            )
    except Exception as exc:
        debug(f"Error scanning directory {directory}: {exc}")

    return files_info


def _validate_source(media_path: Optional[Path], allow_all_extensions: bool = False) -> bool:
    """Validate that the source file exists and is supported.

    Args:
        media_path: Path to the file to validate
        allow_all_extensions: If True, skip file type filtering for non-backend exports.
                             If False, only allow SUPPORTED_MEDIA_EXTENSIONS for backend ingest.
    """
    if media_path is None:
        return False

    if not media_path.exists() or not media_path.is_file():
        log(f"File not found: {media_path}")
        return False

    if not allow_all_extensions:
        file_extension = media_path.suffix.lower()
        if file_extension not in SUPPORTED_MEDIA_EXTENSIONS:
            log(f"❌ Unsupported file type: {file_extension}", file=sys.stderr)
            return False

    return True


def _is_probable_url(s: Any) -> bool:
    """Check if a string looks like a URL/magnet/identifier (vs a tag/title)."""
    return _remote_url_text(s) is not None


def _remote_url_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    restored = text.replace("\\", "/")
    restored_low = restored.lower()
    if restored_low.startswith(("http://", "https://", "ftp://", "ftps://")):
        return restored
    low = text.lower()
    if low.startswith(_REMOTE_URL_PREFIXES) or "://" in text:
        return text
    return None


def _iter_item_locations(result: Any, pipe_obj: models.PipeObject) -> List[Any]:
    locs: List[Any] = [
        getattr(pipe_obj, "path", None),
        getattr(pipe_obj, "url", None),
        getattr(pipe_obj, "source_url", None),
        get_field(result, "path"),
        get_field(result, "url"),
        get_field(result, "source_url"),
        get_field(result, "local_path"),
        get_field(result, "target"),
    ]
    bags: List[Any] = []
    for obj in (pipe_obj, result):
        if obj is None:
            continue
        extra = getattr(obj, "extra", None)
        if isinstance(extra, dict):
            bags.append(extra)
        meta = getattr(obj, "metadata", None)
        if isinstance(meta, dict):
            bags.append(meta)
            nested = meta.get("full_metadata") if isinstance(meta.get("full_metadata"), dict) else None
            if nested:
                bags.append(nested)
        if isinstance(obj, dict):
            bags.append(obj)
            fm = obj.get("full_metadata")
            if isinstance(fm, dict):
                bags.append(fm)
    for bag in bags:
        locs.extend(
            [
                bag.get("path"),
                bag.get("url"),
                bag.get("source_url"),
                bag.get("target"),
            ]
        )
        args = bag.get("_selection_args") or bag.get("selection_args")
        if isinstance(args, (list, tuple)):
            i = 0
            while i < len(args):
                tok = str(args[i] or "").strip()
                low = tok.lower()
                if low in {"-url", "--url"} and i + 1 < len(args):
                    locs.append(args[i + 1])
                    i += 2
                    continue
                locs.append(tok)
                i += 1
    return locs


def _resolve_source(
    result: Any,
    source_arg: Optional[str],
    pipe_obj: models.PipeObject,
    config: Dict[str, Any],
    export_destination: Optional[Path] = None,
    store_instance: Optional[Any] = None,
    deps: Optional[_CommandDependencies] = None,
) -> tuple[Optional[Path], Optional[str], Optional[Path]]:
    """Resolve the source file path from the positional source arg or pipeline result.

    Returns (media_path, file_hash, temp_dir_to_cleanup).
    """
    # PRIORITY 1a: Prefer an explicit local path when it exists.
    if isinstance(result, dict):
        r_path = result.get("path")
        r_hash = result.get("hash")
        if r_path and not _remote_url_text(r_path):
            try:
                p = coerce_to_path(r_path)
                if p.exists() and p.is_file():
                    pipe_obj.path = str(p)
                    return p, str(r_hash) if r_hash else None, None
            except Exception:
                pass

    locations = _iter_item_locations(result, pipe_obj)
    hash_hint = get_field(result, "hash") or get_field(result, "file_hash") or getattr(pipe_obj, "hash", None)
    for loc in locations:
        if _remote_url_text(loc):
            continue
        text = str(loc or "").strip()
        if not text:
            continue
        try:
            local_path = Path(text).expanduser()
        except Exception:
            continue
        try:
            if local_path.exists() and local_path.is_file():
                pipe_obj.path = str(local_path)
                return local_path, str(hash_hint) if hash_hint else None, None
        except Exception:
            continue

    # PRIORITY 1b: Try hash+store from result (fetch from backend)
    r_hash = get_field(result, "hash") or get_field(result, "file_hash")
    r_store = get_field(result, "store")

    if r_hash and r_store:
        try:
            if deps is None:
                deps = _CommandDependencies(config)
            backend_registry = store_instance or deps.get_backend_registry()

            backend = _resolve_backend_by_name(backend_registry, r_store)
            if backend is not None:
                mp = backend.get_file(r_hash)
                if isinstance(mp, Path) and mp.exists():
                    pipe_obj.path = str(mp)
                    return mp, str(r_hash), None
                if isinstance(mp, str) and mp.strip():
                    try:
                        mp_path = Path(str(mp))
                    except Exception:
                        mp_path = None
                    if mp_path is not None and mp_path.exists() and mp_path.is_file():
                        pipe_obj.path = str(mp_path)
                        return mp_path, str(r_hash), None

                    dl_path, tmp_dir = _maybe_download_backend_file(
                        backend,
                        str(r_hash),
                        pipe_obj,
                        output_dir=export_destination,
                    )
                    if dl_path and dl_path.exists():
                        pipe_obj.path = str(dl_path)
                        return dl_path, str(r_hash), tmp_dir

                    dl_path, tmp_dir = _download_remote_backend_url(
                        str(mp),
                        pipe_obj,
                        file_hash=str(r_hash),
                        output_dir=export_destination,
                    )
                    if dl_path and dl_path.exists():
                        pipe_obj.path = str(dl_path)
                        return dl_path, str(r_hash), tmp_dir
        except Exception as exc:
            debug(f"[add-file] _resolve_source backend fetch failed for {r_store}/{r_hash}: {exc}")

    remote_url = next((u for u in (_remote_url_text(loc) for loc in locations) if u), None)
    if remote_url:
        if not getattr(pipe_obj, "url", None):
            pipe_obj.url = remote_url
        if not getattr(pipe_obj, "path", None):
            pipe_obj.path = remote_url
        downloaded_path, hash_hint, tmp_dir = _maybe_download_plugin_result(
            result,
            pipe_obj,
            config,
            deps=deps,
        )
        if downloaded_path:
            pipe_obj.path = str(downloaded_path)
            return downloaded_path, hash_hint, tmp_dir
        dl_path, tmp_dir = _download_remote_backend_url(
            remote_url,
            pipe_obj,
            file_hash=get_field(result, "hash") or get_field(result, "file_hash"),
            output_dir=export_destination,
        )
        if dl_path:
            pipe_obj.path = str(dl_path)
            hash_hint = get_field(result, "hash") or get_field(result, "file_hash")
            return dl_path, hash_hint, tmp_dir
        log("add-file could not auto-fetch remote source. Use file -download first.", file=sys.stderr)
        return None, None, None

    downloaded_path, hash_hint, tmp_dir = _maybe_download_plugin_result(
        result,
        pipe_obj,
        config,
        deps=deps,
    )
    if downloaded_path:
        pipe_obj.path = str(downloaded_path)
        return downloaded_path, hash_hint, tmp_dir

    def _existing_file(value: Any) -> Optional[Path]:
        if _remote_url_text(value):
            return None
        text = str(value or "").strip()
        if not text:
            return None
        try:
            path = Path(text).expanduser()
        except Exception:
            return None
        if path.exists() and path.is_file():
            return path
        return None

    candidate = None
    if source_arg:
        candidate = _existing_file(source_arg)
    if candidate is None:
        for loc in locations:
            candidate = _existing_file(loc)
            if candidate is not None:
                break

    if candidate:
        pipe_obj.path = str(candidate)
        hash_hint = get_field(result, "hash") or get_field(result, "file_hash") or getattr(pipe_obj, "hash", None)
        return candidate, hash_hint, None

    debug(f"No resolution path matched. result type={type(result).__name__}")
    log("File path could not be resolved")
    return None, None, None
