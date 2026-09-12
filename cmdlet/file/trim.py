"""Trim a media file using ffmpeg."""

from __future__ import annotations

from typing import Any, Dict, Sequence, Optional
from pathlib import Path
import sys
import subprocess
import shutil
import re
import time
from urllib.parse import urlparse

from SYS.logger import log, debug
from SYS.item_accessors import get_store_name
from SYS.utils import sanitize_filename, sha256_file
from PluginCore.backend_registry import BackendRegistry
from .. import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
parse_cmdlet_args = sh.parse_cmdlet_args
normalize_result_input = sh.normalize_result_input
extract_title_from_result = sh.extract_title_from_result
extract_url_from_result = sh.extract_url_from_result
get_field = sh.get_field
from SYS import pipeline as ctx

CMDLET = Cmdlet(
    name="trim-file",
    summary="Trim a media file using ffmpeg.",
    usage=
    "trim-file [-path <path>] [-input <path-or-url>] -range <start-end> [-outdir <dir>] [-delete]",
    arg=[
        CmdletArg("-path",
                  description="Path to the file (optional if piped)."),
        CmdletArg(
            "-input",
            description=
            "Override input media source (path or URL). Useful when piping store metadata but trimming from an mpv stream URL.",
        ),
        CmdletArg(
            "-range",
            required=True,
            description=
            "Time range to trim (e.g. '3:45-3:55', '00:03:45-00:03:55', or '1h3m-1h10m30s').",
        ),
        CmdletArg(
            "-outdir",
            description=
            "Output directory for the clip (defaults to source folder for local files; otherwise uses system temp).",
        ),
        CmdletArg(
            "-delete",
            type="flag",
            description="Delete the original file after trimming."
        ),
    ],
    detail=[
        "Creates a new file with 'clip_' prefix in the filename.",
        "Adds the trim range to the title as: [1h3m-1h3m10s] - <title>.",
        "Inherits tag values from the source file.",
        "Adds a relationship to the source file (if hash is available).",
        "Output can be piped to add-file.",
    ],
)


def _format_hms(total_seconds: float) -> str:
    """Format seconds as compact h/m/s (no colons), e.g. 1h3m10s, 3m5s, 2s."""
    try:
        total = int(round(float(total_seconds)))
    except Exception:
        total = 0
    if total < 0:
        total = 0

    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60

    parts: list[str] = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0:
        parts.append(f"{seconds}s")

    # Ensure we always output something.
    if not parts:
        return "0s"
    return "".join(parts)


def _is_url(value: str) -> bool:
    try:
        p = urlparse(str(value))
        return bool(p.scheme and p.netloc)
    except Exception:
        return False


def _parse_time(time_str: str) -> float:
    """Convert time string into seconds.

    Supports:
    - HH:MM:SS(.sss)
    - MM:SS(.sss)
    - SS(.sss)
    - 1h3m53s (also 1h3m, 3m53s, 53s)
    """
    raw = str(time_str or "").strip()
    if not raw:
        raise ValueError("Empty time")

    # h/m/s format (case-insensitive)
    hms = re.fullmatch(
        r"(?i)\s*(?:(?P<h>\d+(?:\.\d+)?)h)?(?:(?P<m>\d+(?:\.\d+)?)m)?(?:(?P<s>\d+(?:\.\d+)?)s)?\s*",
        raw,
    )
    if hms and (hms.group("h") or hms.group("m") or hms.group("s")):
        hours = float(hms.group("h") or 0)
        minutes = float(hms.group("m") or 0)
        seconds = float(hms.group("s") or 0)
        total = hours * 3600 + minutes * 60 + seconds
        return float(total)

    # Colon-separated
    parts = [p.strip() for p in raw.split(":")]
    if len(parts) == 3:
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return float(parts[0]) * 60 + float(parts[1])
    if len(parts) == 1:
        return float(parts[0])

    raise ValueError(f"Invalid time format: {time_str}")


def _extract_store_name(item: Any) -> Optional[str]:
    return get_store_name(item, "store")


def _persist_alt_relationship(
    *,
    config: Dict[str,
                 Any],
    store_name: str,
    alt_hash: str,
    king_hash: str
) -> None:
    """Persist directional alt -> king relationship in the given backend."""
    try:
        backend_registry = BackendRegistry(config)
        backend: Any = backend_registry[str(store_name)]
    except Exception:
        return

    alt_norm = str(alt_hash or "").strip().lower()
    king_norm = str(king_hash or "").strip().lower()
    if len(alt_norm) != 64 or len(king_norm) != 64 or alt_norm == king_norm:
        return

    # Hydrus-like backend
    try:
        client = getattr(backend, "_client", None)
        if client is not None and hasattr(client, "set_relationship"):
            client.set_relationship(alt_norm, king_norm, "alt")
    except Exception:
        return


def _trim_media(
    input_source: str,
    output_path: Path,
    start_seconds: float,
    duration_seconds: float
) -> bool:
    """Trim media using ffmpeg.

    input_source may be a local path or a URL.
    """
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        log("ffmpeg not found in PATH", file=sys.stderr)
        return False

    try:
        if duration_seconds <= 0:
            log(f"Invalid range: duration <= 0 ({duration_seconds})", file=sys.stderr)
            return False

        cmd = [
            ffmpeg_path,
            "-y",
            "-ss",
            str(float(start_seconds)),
            "-i",
            str(input_source),
            "-t",
            str(float(duration_seconds)),
            "-c",
            "copy",
            "-map_metadata",
            "0",
            str(output_path),
        ]

        debug(f"Running ffmpeg: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            log(f"ffmpeg error: {result.stderr}", file=sys.stderr)
            return False

        return True
    except Exception as e:
        log(f"Error parsing time or running ffmpeg: {e}", file=sys.stderr)
        return False


def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Trim a media file."""
    # Parse arguments
    parsed = parse_cmdlet_args(args, CMDLET)

    range_arg = parsed.get("range")
    if not range_arg or "-" not in range_arg:
        log("Error: -range argument required (format: start-end)", file=sys.stderr)
        return 1

    start_str, end_str = [s.strip() for s in range_arg.split("-", 1)]
    if not start_str or not end_str:
        log("Error: -range must be start-end", file=sys.stderr)
        return 1

    try:
        start_seconds = _parse_time(start_str)
        end_seconds = _parse_time(end_str)
    except Exception as exc:
        log(f"Error parsing -range: {exc}", file=sys.stderr)
        return 1

    duration_seconds = end_seconds - start_seconds
    if duration_seconds <= 0:
        log(f"Invalid range: start {start_str} >= end {end_str}", file=sys.stderr)
        return 1

    delete_original = parsed.get("delete", False)
    path_arg = parsed.get("path")
    input_override = parsed.get("input")
    outdir_arg = parsed.get("outdir")

    # Collect inputs
    inputs = normalize_result_input(result)

    # If path arg provided, add it to inputs
    if path_arg:
        inputs.append({
            "path": path_arg
        })

    if not inputs:
        log("No input files provided.", file=sys.stderr)
        return 1

    success_count = 0

    for item in inputs:
        store_name = _extract_store_name(item)

        # Resolve file path
        file_path: Optional[str] = None
        if isinstance(item, dict):
            file_path = item.get("path") or item.get("target")
        elif hasattr(item, "path"):
            file_path = item.path
        elif isinstance(item, str):
            file_path = item

        if not file_path and not input_override:
            continue

        media_source = str(input_override or file_path)
        is_url = _is_url(media_source)

        path_obj: Optional[Path] = None
        if not is_url:
            try:
                path_obj = Path(str(media_source))
            except Exception:
                path_obj = None
            if not path_obj or not path_obj.exists():
                log(f"File not found: {media_source}", file=sys.stderr)
                continue

        # Determine output directory
        output_dir: Path
        if outdir_arg:
            output_dir = Path(str(outdir_arg)).expanduser()
        elif store_name:
            from SYS.config import resolve_output_dir

            output_dir = resolve_output_dir(config or {})
        elif path_obj is not None:
            output_dir = path_obj.parent
        else:
            from SYS.config import resolve_output_dir

            output_dir = resolve_output_dir(config or {})

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # Determine output filename
        output_ext = ""
        if path_obj is not None:
            output_ext = path_obj.suffix
            base_name = path_obj.stem
        else:
            # Prefer title from metadata if present
            title = extract_title_from_result(item)
            if title:
                base_name = sanitize_filename(str(title), max_len=140, fallback="clip")
            else:
                base_name = time.strftime("%Y%m%d-%H%M%S")

            if base_name.lower().startswith("clip_"):
                base_name = base_name[5:] or base_name

            try:
                p = urlparse(str(media_source))
                last = (p.path or "").split("/")[-1]
                if last and "." in last:
                    output_ext = "." + last.split(".")[-1]
            except Exception:
                pass

            if not output_ext or len(output_ext) > 8:
                output_ext = ".mkv"

        new_filename = f"clip_{base_name}{output_ext}"
        output_path = output_dir / new_filename

        # Avoid clobbering existing files
        if output_path.exists():
            stem = output_path.stem
            suffix = output_path.suffix
            for i in range(1, 1000):
                candidate = output_dir / f"{stem}_{i}{suffix}"
                if not candidate.exists():
                    output_path = candidate
                    break

        # Trim
        source_label = path_obj.name if path_obj is not None else str(media_source)
        log(f"Trimming {source_label} ({start_str} to {end_str})...", file=sys.stderr)
        if _trim_media(str(media_source), output_path, start_seconds, duration_seconds):
            log(f"Created clip: {output_path}", file=sys.stderr)
            success_count += 1

            # Prepare result for pipeline

            # 1. Get source hash for relationship
            source_hash = None
            if isinstance(item, dict):
                source_hash = item.get("hash")
            elif hasattr(item, "hash"):
                source_hash = item.hash

            if not source_hash:
                if path_obj is not None:
                    try:
                        source_hash = sha256_file(path_obj)
                    except Exception:
                        pass

            # 2. Get tag values
            # Do not inherit tags from the source (per UX request).
            new_tags: list[str] = []

            # Copy URL(s) when present.
            urls: list[str] = []
            try:
                urls = extract_url_from_result(item) or []
            except Exception:
                urls = []
            try:
                src_u = get_field(item, "source_url")
                if isinstance(src_u, str) and src_u.strip():
                    if src_u.strip() not in urls:
                        urls.append(src_u.strip())
            except Exception:
                pass

            # 3. Get title and modify it
            title = extract_title_from_result(item)
            if not title:
                title = path_obj.stem if path_obj is not None else base_name

            range_hms = f"{_format_hms(start_seconds)}-{_format_hms(end_seconds)}"
            new_title = f"[{range_hms}] - {title}"

            # 4. Calculate clip hash
            clip_hash = None
            try:
                clip_hash = sha256_file(output_path)
            except Exception:
                pass

            # If this was a store item, ingest the clip into the same store.
            stored_instance: Optional[str] = None
            stored_hash: Optional[str] = None
            stored_path: Optional[str] = None

            if store_name:
                try:
                    backend, _store_registry, _exc = sh.get_store_backend(
                        config,
                        store_name,
                    )
                    if backend is not None:
                        stored_hash = backend.add_file(
                            Path(str(output_path)),
                            title=new_title,
                            tag=new_tags,
                            url=urls,
                            move=False,
                        )
                        stored_store = store_name
                except Exception as exc:
                    log(
                        f"Failed to add clip to store '{store_name}': {exc}",
                        file=sys.stderr
                    )

            # If we stored it, persist relationship alt -> king in that store.
            if stored_store and stored_hash and source_hash:
                _persist_alt_relationship(
                    config=config,
                    store_name=stored_store,
                    alt_hash=stored_hash,
                    king_hash=str(source_hash),
                )

            if stored_hash:
                clip_hash = stored_hash

            # 5. Construct result
            result_dict = {
                "path": stored_path or str(output_path),
                "title": new_title,
                "tag": new_tags,
                "url": urls,
                "media_kind": "video",  # Assumption, or derive
                "hash": clip_hash,  # Pass calculated hash
                "store": stored_store,
                "relationships": {
                    # Clip is an ALT of the source; store semantics are directional alt -> king.
                    # Provide both keys so downstream (e.g. add-file) can persist relationships.
                    "king": [source_hash] if source_hash else [],
                    "alt": [clip_hash] if (source_hash and clip_hash) else [],
                },
            }

            # Emit result
            ctx.emit(result_dict)

            # Delete original if requested
            if delete_original:
                try:
                    if path_obj is not None:
                        path_obj.unlink()
                        log(f"Deleted original file: {path_obj}", file=sys.stderr)
                    # Also try to delete sidecars?
                    # Maybe leave that to user or cleanup cmdlet
                except Exception as e:
                    log(f"Failed to delete original: {e}", file=sys.stderr)

        else:
            failed_label = path_obj.name if path_obj is not None else str(media_source)
            log(f"Failed to trim {failed_label}", file=sys.stderr)

    return 0 if success_count > 0 else 1


# Register cmdlet (no legacy decorator)
CMDLET.exec = _run
CMDLET.register()
