"""Create a single .tar.zst archive from piped file selections."""

from __future__ import annotations

import re
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Sequence, Set
from urllib.parse import parse_qs, urlparse

from SYS.logger import log
from PluginCore.registry import get_plugin, plugin_for_storage
from SYS.item_accessors import get_http_url, get_sha256_hex, get_store_name
from SYS.utils import extract_hydrus_hash_from_url

from SYS import pipeline as ctx
from SYS.config import resolve_output_dir
from .. import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
SharedArgs = sh.SharedArgs
coerce_to_pipe_object = sh.coerce_to_pipe_object
create_pipe_object_result = sh.create_pipe_object_result
parse_cmdlet_args = sh.parse_cmdlet_args
should_show_help = sh.should_show_help

def _extract_sha256_hex(item: Any) -> str:
    return get_sha256_hex(item, "hash") or ""


def _extract_store_name(item: Any) -> str:
    return get_store_name(item, "store", "instance") or ""


def _extract_url(item: Any) -> str:
    return get_http_url(item, "url", "target") or ""


def _extract_hash_from_hydrus_file_url(url: str) -> str:
    """Extract hash from Hydrus URL using centralized utility."""
    return extract_hydrus_hash_from_url(url) or ""


def _hydrus_instance_names(config: Dict[str, Any]) -> Set[str]:
    instances: Set[str] = set()
    try:
        plugin_cfg = config.get("plugin") if isinstance(config, dict) else None
        if isinstance(plugin_cfg, dict):
            hydrus_cfg = plugin_cfg.get("hydrusnetwork")
            if isinstance(hydrus_cfg, dict):
                instances = {
                    str(k).strip().lower()
                    for k in hydrus_cfg.keys() if str(k).strip()
                }
    except Exception:
        instances = set()
    return instances


def _maybe_download_hydrus_item(
    item: Any,
    config: Dict[str,
                 Any],
    output_dir: Path
) -> Path | None:
    """Download a Hydrus-backed item to a local temp path (best-effort).

    This is intentionally side-effect free except for writing the local temp file.
    """
    hydrus_provider = plugin_for_storage(config, item=item)
    if hydrus_provider is None:
        return None

    store_name = _extract_store_name(item)
    store_lower = store_name.lower()
    hydrus_instances = _hydrus_instance_names(config)
    store_hint = store_lower in {"hydrus",
                                 "hydrusnetwork"} or (store_lower in hydrus_instances)

    url = _extract_url(item)
    file_hash = _extract_sha256_hex(item) or (
        _extract_hash_from_hydrus_file_url(url) if url else ""
    )
    if not file_hash:
        return None

    # Only treat it as Hydrus when we have an explicit Hydrus file URL OR the store suggests it.
    is_hydrus_url = False
    if url:
        try:
            parsed = urlparse(url)
            is_hydrus_url = (parsed.path or "").endswith(
                "/get_files/file"
            ) and _extract_hash_from_hydrus_file_url(url) == file_hash
        except Exception:
            is_hydrus_url = False
    if not (is_hydrus_url or store_hint):
        return None
    preferred_store = store_name or None
    if url and is_hydrus_url:
        return hydrus_provider.download_url(url, output_dir)
    return hydrus_provider.download_hash_to_temp(file_hash, store_name=preferred_store, temp_root=output_dir)


def _resolve_existing_or_fetch_path(item: Any,
                                    config: Dict[str,
                                                 Any]) -> tuple[Path | None,
                                                                Path | None]:
    """Return (path, temp_path) where temp_path is non-None only for files we downloaded."""
    # 1) Direct local path
    try:
        po = coerce_to_pipe_object(item, None)
        raw_path = (
            getattr(po,
                    "path",
                    None) or getattr(po,
                                     "target",
                                     None) or sh.get_pipe_object_path(item)
        )
        if raw_path:
            p = Path(str(raw_path)).expanduser()
            if p.exists():
                return p, None
    except Exception:
        pass

    # 2) Store-backed path
    file_hash = _extract_sha256_hex(item)
    store_name = _extract_store_name(item)
    if file_hash and store_name:
        try:
            backend, _store_registry, _exc = sh.get_store_backend(config, store_name)
            if backend is None:
                return None, None
            src = backend.get_file(file_hash)
            if isinstance(src, Path):
                if src.exists():
                    return src, None
            elif isinstance(src, str) and src.strip():
                cand = Path(src).expanduser()
                if cand.exists():
                    return cand, None
                # If the backend returns a URL (HydrusNetwork), download it.
                if src.strip().lower().startswith(("http://", "https://")):
                    tmp_base = None
                    try:
                        tmp_base = config.get("temp"
                                              ) if isinstance(config,
                                                              dict) else None
                    except Exception:
                        tmp_base = None
                    out_dir = (
                        Path(str(tmp_base)).expanduser() if tmp_base else
                        (Path(tempfile.gettempdir()) / "Medios-Macina")
                    )
                    out_dir = out_dir / "archive" / "hydrus"
                    downloaded = _maybe_download_hydrus_item(
                        {
                            "hash": file_hash,
                            "store": store_name,
                            "url": src.strip()
                        },
                        config,
                        out_dir,
                    )
                    if downloaded is not None:
                        return downloaded, downloaded
        except Exception:
            pass

    # 3) Hydrus-backed items without backend.get_file path.
    try:
        tmp_base = config.get("temp") if isinstance(config, dict) else None
    except Exception:
        tmp_base = None
    out_dir = (
        Path(str(tmp_base)).expanduser() if tmp_base else
        (Path(tempfile.gettempdir()) / "Medios-Macina")
    )
    out_dir = out_dir / "archive" / "hydrus"
    downloaded = _maybe_download_hydrus_item(item, config, out_dir)
    if downloaded is not None:
        return downloaded, downloaded

    return None, None


def _unique_arcname(name: str, seen: Set[str]) -> str:
    base = str(name or "").replace("\\", "/")
    base = base.lstrip("/")
    if not base:
        base = "file"
    if base not in seen:
        seen.add(base)
        return base

    stem = base
    suffix = ""
    if "/" not in base:
        p = Path(base)
        stem = p.stem
        suffix = p.suffix

    n = 2
    while True:
        candidate = f"{stem} ({n}){suffix}" if stem else f"file ({n}){suffix}"
        if candidate not in seen:
            seen.add(candidate)
            return candidate
        n += 1


def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    if should_show_help(args):
        log(f"Cmdlet: {CMDLET.name}\nSummary: {CMDLET.summary}\nUsage: {CMDLET.usage}")
        return 0

    parsed = parse_cmdlet_args(args, CMDLET)

    level_raw = parsed.get("level")
    try:
        level = int(level_raw) if level_raw is not None else 11
    except Exception:
        level = 11
    if level < 1:
        level = 1
    if level > 22:
        level = 22

    # Output destination is controlled by the shared -path behavior in the pipeline runner.
    # This cmdlet always creates the archive in the configured output directory and emits it.

    # Collect piped items; archive-file is a batch command (single output).
    items: List[Any] = sh.normalize_result_items(
        result,
        include_falsey_single=True,
    )

    if not items:
        log("No piped items provided to archive-file", file=sys.stderr)
        return 1

    temp_downloads: List[Path] = []
    try:
        paths: List[Path] = []
        for it in items:
            p, tmp = _resolve_existing_or_fetch_path(it, config)
            if p is None:
                continue
            paths.append(p)
            if tmp is not None:
                temp_downloads.append(tmp)

        # Keep stable order, remove duplicates.
        uniq: List[Path] = []
        seen_paths: Set[str] = set()
        for p in paths:
            key = str(p.resolve()) if p.exists() else str(p)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            uniq.append(p)
        paths = uniq

        if not paths:
            log("No existing file paths found in piped items", file=sys.stderr)
            return 1

        out_dir = resolve_output_dir(config)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        stamp = time.strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"archive_{stamp}.tar.zst"
        try:
            out_path = sh._unique_destination_path(
                out_path
            )  # type: ignore[attr-defined]
        except Exception:
            pass

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            log(
                f"Failed to create output directory: {out_path.parent} ({exc})",
                file=sys.stderr
            )
            return 1

        # Import zstandard lazily so the rest of the CLI still runs without it.
        try:
            import zstandard as zstd  # type: ignore
        except Exception:
            log(
                "Missing dependency: zstandard (pip install zstandard)",
                file=sys.stderr
            )
            return 1

        # Write tar stream into zstd stream.
        try:
            with open(out_path, "wb") as out_handle:
                cctx = zstd.ZstdCompressor(level=level)
                with cctx.stream_writer(out_handle) as compressor:
                    with tarfile.open(fileobj=compressor,
                                      mode="w|",
                                      format=tarfile.PAX_FORMAT) as tf:
                        seen_names: Set[str] = set()
                        for p in paths:
                            arcname = _unique_arcname(p.name, seen_names)
                            # For directories, tarfile will include contents when recursive=True.
                            try:
                                tf.add(str(p), arcname=arcname, recursive=True)
                            except Exception as exc:
                                log(
                                    f"Failed to add to archive: {p} ({exc})",
                                    file=sys.stderr
                                )
        except Exception as exc:
            log(f"Archive creation failed: {exc}", file=sys.stderr)
            return 1

        # Emit a single artifact downstream.
        hash_value = None
        try:
            from SYS.utils import sha256_file

            hash_value = sha256_file(out_path)
        except Exception:
            hash_value = None

        pipe_obj = create_pipe_object_result(
            source="archive",
            identifier=out_path.stem,
            file_path=str(out_path),
            cmdlet_name="archive-file",
            title=out_path.name,
            hash_value=hash_value,
            is_temp=True,
            store="PATH",
            extra={
                "target": str(out_path),
                "archive_format": "tar.zst",
                "compression": "zstd",
                "level": level,
                "source_count": len(paths),
                "source_paths": [str(p) for p in paths],
            },
        )
        ctx.emit(pipe_obj)
        return 0
    finally:
        # Best-effort cleanup of any temp Hydrus downloads we created.
        for tmp in temp_downloads:
            try:
                tmp.unlink(missing_ok=True)  # type: ignore[arg-type]
            except TypeError:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass
            except Exception:
                pass


CMDLET = Cmdlet(
    name="archive-file",
    summary="Archive piped files into a single .tar.zst.",
    usage="@N | archive-file [-level <1-22>] [-path <path>]",
    arg=[
        CmdletArg(
            "-level",
            type="integer",
            description="Zstandard compression level (default: 11)."
        ),
        SharedArgs.PATH,
    ],
    detail=[
        "- Example: @1-5 | archive-file",
        "- Default zstd level is 11.",
        "- Emits one output item (the archive) for downstream piping.",
    ],
)

CMDLET.exec = _run
CMDLET.register()
