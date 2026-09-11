"""Merge multiple files into a single output file."""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, List
from pathlib import Path
import sys

from SYS.logger import debug, debug_panel, log


def _merge_debug(title: str, **fields: Any) -> None:
    debug_panel(title, list(fields.items()))
import subprocess as _subprocess
import shutil as _shutil
import re as _re

from SYS.config import resolve_output_dir

from .. import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
SharedArgs = sh.SharedArgs
create_pipe_object_result = sh.create_pipe_object_result
get_field = sh.get_field
get_pipe_object_hash = sh.get_pipe_object_hash
get_pipe_object_path = sh.get_pipe_object_path
normalize_result_input = sh.normalize_result_input
parse_cmdlet_args = sh.parse_cmdlet_args
should_show_help = sh.should_show_help

from SYS import pipeline as ctx

_CHAPTER_TITLE_SPLIT_RE = _re.compile(r"^(?P<prefix>.+?)\s+-\s+(?P<chapter>.+)$")
_FFMPEG_TIME_RE = _re.compile(r"time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})")

try:
    from pypdf import PdfWriter, PdfReader

    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False
    PdfWriter = None
    PdfReader = None

# Stub fallbacks used before SYS.metadata is lazily imported (or if unavailable).
HAS_METADATA_API: bool = False
_metadata_loaded: bool = False


def read_tags_from_file(file_path: Path) -> List[str]:
    return []


def merge_multiple_tag_lists(sources: List[List[str]],
                              strategy: str = "first") -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for src in sources:
        for t in src or []:
            s = str(t)
            if s and s not in seen:
                out.append(s)
                seen.add(s)
    return out


def _ensure_metadata_imports() -> None:
    """Lazily import SYS.metadata to avoid loading Cryptodome (~1s) at startup."""
    global _metadata_loaded, HAS_METADATA_API, read_tags_from_file, merge_multiple_tag_lists
    if _metadata_loaded:
        return
    _metadata_loaded = True
    try:
        from SYS.metadata import (  # type: ignore[assignment]
            read_tags_from_file as _rtf,
            merge_multiple_tag_lists as _mml,
        )
        read_tags_from_file = _rtf  # type: ignore[assignment]
        merge_multiple_tag_lists = _mml  # type: ignore[assignment]
        HAS_METADATA_API = True
    except ImportError:
        HAS_METADATA_API = False


def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Merge multiple files into one."""
    _ensure_metadata_imports()

    # Parse help
    if should_show_help(args):
        log(f"Cmdlet: {CMDLET.name}\nSummary: {CMDLET.summary}\nUsage: {CMDLET.usage}")
        return 0

    # Parse arguments
    parsed = parse_cmdlet_args(args, CMDLET)
    delete_after = parsed.get("delete", False)

    output_override: Optional[Path] = None
    output_arg = parsed.get("path")
    if output_arg:
        try:
            output_override = Path(str(output_arg)).expanduser()
        except Exception:
            output_override = None

    format_spec = parsed.get("format")
    if format_spec:
        format_spec = str(format_spec).lower().strip()

    # Collect files from piped results
    # Use normalize_result_input to handle both single items and lists
    files_to_merge: List[Dict[str, Any]] = normalize_result_input(result)

    if not files_to_merge:
        log("No files provided to merge", file=sys.stderr)
        return 1

    if len(files_to_merge) < 2:
        # Only 1 file - pass it through unchanged
        # (merge only happens when multiple files are collected)
        item = files_to_merge[0]
        ctx.emit(item)
        return 0

    def _resolve_existing_path(item: Dict[str, Any]) -> Optional[Path]:
        raw_path = get_pipe_object_path(item)
        target_path: Optional[Path] = None
        if isinstance(raw_path, Path):
            target_path = raw_path
        elif isinstance(raw_path, str) and raw_path.strip():
            text = raw_path.strip()
            low = text.lower()
            if not (low.startswith("http://") or low.startswith("https://")):
                candidate = Path(text).expanduser()
                if candidate.exists():
                    target_path = candidate
        if target_path and target_path.exists():
            return target_path
        try:
            from .archive import _resolve_existing_or_fetch_path

            fetched, _temp = _resolve_existing_or_fetch_path(item, config)
        except Exception:
            fetched = None
        if fetched is not None and fetched.exists():
            return fetched
        return None

    def _extract_url(item: Dict[str, Any]) -> Optional[str]:
        u = get_field(item, "url") or get_field(item, "target")
        if isinstance(u, str):
            s = u.strip()
            if s.lower().startswith(("http://", "https://")):
                return s
        return None

    # If the user piped URL-only playlist selections (no local paths yet), download first.
    # This keeps the pipeline order intuitive:
    #   @* | merge-file | add-file -instance ...
    urls_to_download: List[str] = []
    for it in files_to_merge:
        if _resolve_existing_path(it) is not None:
            continue
        u = _extract_url(it)
        if u:
            urls_to_download.append(u)

    if urls_to_download and len(urls_to_download) >= 2:
        try:
            from PluginCore.registry import get_plugin_for_url

            expanded: List[Dict[str, Any]] = []
            downloaded_any = False
            for it in files_to_merge:
                if _resolve_existing_path(it) is not None:
                    expanded.append(it)
                    continue
                u = _extract_url(it)
                if not u:
                    expanded.append(it)
                    continue

                downloaded = []
                try:
                    plugin = get_plugin_for_url(u, config)
                except Exception:
                    plugin = None
                if plugin is not None and hasattr(plugin, "download_url_as_pipe_objects"):
                    try:
                        downloaded = plugin.download_url_as_pipe_objects(u)
                    except TypeError:
                        downloaded = plugin.download_url_as_pipe_objects(u, output_dir=None)
                    except Exception:
                        downloaded = []
                if downloaded:
                    expanded.extend(downloaded)
                    downloaded_any = True
                else:
                    expanded.append(it)

            if downloaded_any:
                files_to_merge = expanded
        except Exception:
            # If downloads fail, we fall back to the existing path-based merge behavior.
            pass

    # Extract file paths and metadata from result objects
    source_files: List[Path] = []
    source_hashes: List[str] = []
    source_url: List[str] = []
    source_tags: List[str] = []  # tags read from .tag sidecars
    source_item_tag_lists: List[List[str]] = []  # tags carried in-memory on piped items
    for item in files_to_merge:
        target_path = _resolve_existing_path(item)

        if target_path and target_path.exists():
            source_files.append(target_path)

            # Track tags carried in the piped items (e.g. add-tag stage) so they survive merge.
            try:
                raw_tags = get_field(item, "tag", [])
                if isinstance(raw_tags, str) and raw_tags.strip():
                    source_item_tag_lists.append([raw_tags.strip()])
                elif isinstance(raw_tags, list):
                    source_item_tag_lists.append(
                        [str(t) for t in raw_tags if t is not None and str(t).strip()]
                    )
            except Exception:
                pass

            # Track tags from the .tag sidecar for this source (if present)
            tags_file = target_path.with_suffix(target_path.suffix + ".tag")
            if tags_file.exists() and HAS_METADATA_API:
                try:
                    source_tags.extend(read_tags_from_file(tags_file) or [])
                except Exception:
                    pass

            # Extract hash if available in item (as fallback)
            hash_value = get_pipe_object_hash(item)
            if hash_value and hash_value not in source_hashes:
                source_hashes.append(str(hash_value))

            # Extract known url if available
            url = get_field(item, "url", [])
            if isinstance(url, str):
                source_url.append(url)
            elif isinstance(url, list):
                source_url.extend(url)
        else:
            title = get_field(item,
                              "title",
                              "unknown") or get_field(item,
                                                      "id",
                                                      "unknown")
            log(f"Warning: Could not locate file for item: {title}", file=sys.stderr)

    if len(source_files) < 2:
        log("At least 2 valid files required to merge", file=sys.stderr)
        return 1

    # Detect file types
    file_types = set()
    for f in source_files:
        suffix = f.suffix.lower()
        if suffix in {".mp3",
                      ".flac",
                      ".wav",
                      ".m4a",
                      ".aac",
                      ".ogg",
                      ".opus",
                      ".mka"}:
            file_types.add("audio")
        elif suffix in {
                ".mp4",
                ".mkv",
                ".webm",
                ".mov",
                ".avi",
                ".flv",
                ".mpg",
                ".mpeg",
                ".ts",
                ".m4v",
                ".wmv",
        }:
            file_types.add("video")
        elif suffix in {".pdf"}:
            file_types.add("pdf")
        elif suffix in {".txt",
                        ".srt",
                        ".vtt",
                        ".md",
                        ".log"}:
            file_types.add("text")
        else:
            file_types.add("other")

    if len(file_types) > 1 and "other" not in file_types:
        log(
            f"Mixed file types detected: {', '.join(sorted(file_types))}",
            file=sys.stderr
        )
        log("Can only merge files of the same type", file=sys.stderr)
        return 1

    file_kind = list(file_types)[0] if file_types else "other"

    # Determine output format
    output_format = format_spec or "auto"
    if output_format == "auto":
        if file_kind == "audio":
            output_format = "mka"  # Default audio codec - mka supports chapters and stream copy
        elif file_kind == "video":
            output_format = "mp4"  # Default video codec
        elif file_kind == "pdf":
            output_format = "pdf"
        else:
            output_format = "txt"

    # Determine output path
    if output_override:
        if output_override.is_dir():
            base_title = get_field(files_to_merge[0], "title", "merged")
            base_name = _sanitize_name(str(base_title or "merged"))
            output_path = output_override / f"{base_name} (merged).{_ext_for_format(output_format)}"
        else:
            output_path = output_override
    else:
        first_file = source_files[0]
        try:
            base_dir = resolve_output_dir(config)
        except Exception:
            base_dir = first_file.parent
        output_path = (
            Path(base_dir) /
            f"{first_file.stem} (merged).{_ext_for_format(output_format)}"
        )

    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Perform merge based on file type
    if file_kind == "audio":
        success = _merge_audio(source_files, output_path, output_format)
    elif file_kind == "video":
        success = _merge_video(source_files, output_path, output_format)
    elif file_kind == "pdf":
        success = _merge_pdf(source_files, output_path)
    elif file_kind == "text":
        success = _merge_text(source_files, output_path)
    else:
        log(f"Unsupported file type: {file_kind}", file=sys.stderr)
        return 1

    if not success:
        log("Merge failed", file=sys.stderr)
        return 1

    _merge_debug("merge-file", files=len(source_files), output=str(output_path))

    def _namespaced_tag(tags: List[str], key: str) -> Optional[str]:
        prefix = f"{str(key or '').strip().lower()}:"
        if prefix == ":":
            return None
        for t in tags:
            try:
                s = str(t)
            except Exception:
                continue
            if s.lower().startswith(prefix):
                val = s.split(":", 1)[1].strip()
                return val or None
        return None

    def _title_value_from_tags(tags: List[str]) -> Optional[str]:
        return _namespaced_tag(tags, "title")

    shared_title: Optional[str] = None
    try:
        if source_item_tag_lists:
            per_item_titles: List[Optional[str]] = [
                _title_value_from_tags(tl) for tl in source_item_tag_lists
            ]
            non_empty = [t for t in per_item_titles if t]
            if non_empty and all((t == non_empty[0]) for t in non_empty):
                shared_title = non_empty[0]
            albums = [_namespaced_tag(tl, "album") for tl in source_item_tag_lists]
            album_vals = [a for a in albums if a]
            if album_vals and all(a.lower() == album_vals[0].lower() for a in album_vals):
                if (not shared_title) or len(set(t.lower() for t in non_empty)) > 1:
                    shared_title = album_vals[0]
    except Exception:
        shared_title = None

    merged_title = shared_title or output_path.stem

    # Merge tags from:
    # - in-memory PipeObject tags (from add-tag etc)
    # - .tag sidecars (if present)
    # Keep all unique plain tags, and keep the first value for namespaced tags.
    merged_tags = merge_multiple_tag_lists(
        source_item_tag_lists + ([source_tags] if source_tags else []),
        strategy="combine"
    )

    # Ensure we always have a title tag (and make sure it's the chosen title)
    merged_tags = [t for t in merged_tags if not str(t).lower().startswith("title:")]
    merged_tags.insert(0, f"title:{merged_title}")

    # Emit a PipeObject-compatible dict so the merged file can be piped to next command
    try:
        from SYS.utils import sha256_file

        merged_hash = sha256_file(output_path)
        merged_item = create_pipe_object_result(
            source="local",
            identifier=output_path.name,
            file_path=str(output_path),
            cmdlet_name="merge-file",
            title=merged_title,
            hash_value=merged_hash,
            tag=merged_tags,
            url=source_url,
            media_kind=file_kind,
            store="PATH",
        )
        # Clear previous results to ensure only the merged file is passed down
        ctx.clear_last_result()
        ctx.emit(merged_item)
    except Exception as e:
        log(f"Warning: Could not emit pipeline item: {e}", file=sys.stderr)
        # Still emit a string representation for feedback
        ctx.emit(f"Merged: {output_path}")

    # Cleanup
    # - Delete source files only when -delete is set.
    if delete_after:
        for f in source_files:
            try:
                # Delete sidecar tags for the source (if any)
                tag_file = f.with_suffix(f.suffix + ".tag")
                if tag_file.exists():
                    try:
                        tag_file.unlink()
                        log(f"Deleted: {tag_file.name}", file=sys.stderr)
                    except Exception as e:
                        log(
                            f"Warning: Could not delete {tag_file.name}: {e}",
                            file=sys.stderr
                        )
            except Exception:
                pass

            try:
                if f.exists():
                    f.unlink()
                    log(f"Deleted: {f.name}", file=sys.stderr)
            except Exception as e:
                log(f"Warning: Could not delete {f.name}: {e}", file=sys.stderr)

    return 0


def _sanitize_name(text: str) -> str:
    """Sanitize filename."""
    allowed = []
    for ch in text:
        allowed.append(ch if (ch.isalnum() or ch in {"-",
                                                     "_",
                                                     " ",
                                                     "."}) else " ")
    return (" ".join("".join(allowed).split()) or "merged").strip()


def _ext_for_format(fmt: str) -> str:
    """Get file extension for format."""
    format_map = {
        "mp3": "mp3",
        "m4a": "m4a",
        "m4b": "m4b",
        "aac": "aac",
        "opus": "opus",
        "mka": "mka",  # Matroska Audio - EXCELLENT chapter support (recommended)
        "mkv": "mkv",
        "mp4": "mp4",
        "webm": "webm",
        "pdf": "pdf",
        "txt": "txt",
        "auto": "mka",  # Default - MKA for chapters
    }
    return format_map.get(fmt.lower(), "mka")


def _merge_audio(files: List[Path], output: Path, output_format: str) -> bool:
    """Merge audio files with chapters based on file boundaries."""
    import logging

    logger = logging.getLogger(__name__)

    ffmpeg_path = _shutil.which("ffmpeg")
    if not ffmpeg_path:
        log("ffmpeg not found in PATH", file=sys.stderr)
        return False

    try:
        # Step 1: Get duration of each file to calculate chapter timestamps
        chapters = []
        current_time_ms = 0

        _merge_debug("merge-file chapters", files=len(files), step="analyze")
        logger.info("[merge-file] Analyzing files for chapters")

        for file_path in files:
            # Get duration using ffprobe
            try:
                ffprobe_cmd = [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-print_format",
                    "default=noprint_wrappers=1:nokey=1",
                    str(file_path),
                ]

                probe_result = _subprocess.run(
                    ffprobe_cmd,
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if probe_result.returncode == 0 and probe_result.stdout.strip():
                    try:
                        duration_sec = float(probe_result.stdout.strip())
                    except ValueError:
                        logger.warning(
                            f"[merge-file] Could not parse duration from ffprobe output: {probe_result.stdout}"
                        )
                        duration_sec = 0
                else:
                    logger.warning(
                        f"[merge-file] ffprobe failed for {file_path.name}: {probe_result.stderr}"
                    )
                    duration_sec = 0
            except Exception as e:
                logger.warning(
                    f"[merge-file] Could not get duration for {file_path.name}: {e}"
                )
                duration_sec = 0

            # Create chapter entry - use title: tag from metadata if available
            title = file_path.stem  # Default to filename without extension
            if HAS_METADATA_API:
                try:
                    # Try to read tags from .tag sidecar file
                    tags_file = file_path.with_suffix(file_path.suffix + ".tag")
                    if tags_file.exists():
                        tags = read_tags_from_file(tags_file)
                        if tags:
                            # Look for title: tag
                            for tag in tags:
                                if isinstance(tag,
                                              str) and tag.lower().startswith("title:"):
                                    # Extract the title value after the colon
                                    title = tag.split(":", 1)[1].strip()
                                    break
                except Exception as e:
                    logger.debug(
                        f"[merge-file] Could not read metadata for {file_path.name}: {e}"
                    )
                    pass  # Fall back to filename

            # Convert seconds to HH:MM:SS.mmm format
            hours = int(current_time_ms // 3600000)
            minutes = int((current_time_ms % 3600000) // 60000)
            seconds = int((current_time_ms % 60000) // 1000)
            millis = int(current_time_ms % 1000)

            chapters.append(
                {
                    "time_ms": current_time_ms,
                    "time_str": f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}",
                    "title": title,
                    "duration_sec": duration_sec,
                }
            )

            logger.info(
                f"[merge-file] Chapter: {title} @ {chapters[-1]['time_str']} (duration: {duration_sec:.2f}s)"
            )
            current_time_ms += int(duration_sec * 1000)

        # If these came from a playlist/album, titles often look like:
        #   "Book Name - Chapter"
        # If *all* titles share the same "Book Name" prefix, strip it.
        if len(chapters) >= 2:
            prefixes: List[str] = []
            stripped_titles: List[str] = []
            all_match = True
            for ch in chapters:
                raw_title = str(ch.get("title") or "").strip()
                m = _CHAPTER_TITLE_SPLIT_RE.match(raw_title)
                if not m:
                    all_match = False
                    break
                prefix = m.group("prefix").strip()
                chapter_title = m.group("chapter").strip()
                if not prefix or not chapter_title:
                    all_match = False
                    break
                prefixes.append(prefix.casefold())
                stripped_titles.append(chapter_title)

            if all_match and prefixes and len(set(prefixes)) == 1:
                for idx, ch in enumerate(chapters):
                    ch["title"] = stripped_titles[idx]
                logger.info(
                    f"[merge-file] Stripped common title prefix for chapters: {prefixes[0]}"
                )

        # Step 2: Create concat demuxer file
        concat_file = output.parent / f".concat_{output.stem}.txt"
        concat_lines = []
        for f in files:
            # Escape quotes in path
            safe_path = str(f).replace("'", "'\\''")
            concat_lines.append(f"file '{safe_path}'")

        concat_file.write_text("\n".join(concat_lines), encoding="utf-8")

        # Step 3: Create FFmpeg metadata file with chapters
        metadata_file = output.parent / f".metadata_{output.stem}.txt"
        metadata_lines = [";FFMETADATA1"]

        for i, chapter in enumerate(chapters):
            # FFMetadata format for chapters (note: [CHAPTER] not [CHAPTER01])
            metadata_lines.append("[CHAPTER]")
            metadata_lines.append("TIMEBASE=1/1000")
            metadata_lines.append(f'START={chapter["time_ms"]}')
            # Calculate end time (start of next chapter or end of file)
            if i < len(chapters) - 1:
                metadata_lines.append(f'END={chapters[i+1]["time_ms"]}')
            else:
                metadata_lines.append(f"END={current_time_ms}")
            metadata_lines.append(f'title={chapter["title"]}')

        metadata_file.write_text("\n".join(metadata_lines), encoding="utf-8")
        _merge_debug("merge-file chapters", chapters=len(chapters), step="wrote metadata")
        logger.info(f"[merge-file] Created {len(chapters)} chapters")

        # Step 4: Build FFmpeg command to merge and embed chapters
        # Strategy: First merge audio, then add metadata in separate pass
        cmd = [ffmpeg_path, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file)]

        # Add threading options for speed
        cmd.extend(["-threads", "0"])  # Use all available threads

        # Audio codec selection for first input
        if output_format == "mp3":
            cmd.extend(["-c:a", "libmp3lame", "-q:a", "2"])
        elif output_format in {"m4a",
                               "m4b"}:
            # Use copy if possible (much faster), otherwise re-encode
            # Check if inputs are already AAC/M4A to avoid re-encoding
            # For now, default to copy if format matches, otherwise re-encode
            # But since we are merging potentially different codecs, re-encoding is safer
            # To speed up re-encoding, we can use a faster preset or hardware accel if available
            cmd.extend(["-c:a", "aac", "-b:a", "256k"])  # M4A with better quality
        elif output_format == "aac":
            cmd.extend(["-c:a", "aac", "-b:a", "192k"])
        elif output_format == "opus":
            cmd.extend(["-c:a", "libopus", "-b:a", "128k"])
        elif output_format == "mka":
            # FLAC is fast to encode but large. Copy is fastest if inputs are compatible.
            # If we want speed, copy is best. If we want compatibility, re-encode.
            # Let's try copy first if inputs are same format, but that's hard to detect here.
            # Defaulting to copy for MKA as it's a container that supports many codecs
            cmd.extend(["-c:a", "copy"])
        else:
            cmd.extend(["-c:a", "copy"])  # Copy without re-encoding

        # Add the output file
        cmd.append(str(output))

        _merge_debug("merge-file", files=len(files), format=output_format, step="mux")
        logger.info(f"[merge-file] Running ffmpeg merge: {' '.join(cmd)}")

        # Run ffmpeg with progress monitoring
        try:
            from SYS.progress import print_progress, print_final_progress
            import re

            process = _subprocess.Popen(
                cmd,
                stdout=_subprocess.PIPE,
                stderr=_subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            # Monitor progress
            total_duration_sec = current_time_ms / 1000.0

            while True:
                # Read stderr line by line (ffmpeg writes progress to stderr)
                if process.stderr:
                    line = process.stderr.readline()
                    if not line and process.poll() is not None:
                        break

                    if line:
                        # Parse time=HH:MM:SS.mm
                        match = _FFMPEG_TIME_RE.search(line)
                        if match and total_duration_sec > 0:
                            h, m, s, cs = map(int, match.groups())
                            current_sec = h * 3600 + m * 60 + s + cs / 100.0

                            # Calculate speed/bitrate if available (optional)
                            # For now just show percentage
                            print_progress(
                                output.name,
                                int(current_sec * 1000),  # Use ms as "bytes" for progress bar
                                int(total_duration_sec * 1000),
                                speed=0,
                            )
                else:
                    break

            # Wait for completion
            stdout, stderr = process.communicate()

            if process.returncode != 0:
                log(f"FFmpeg error: {stderr}", file=sys.stderr)
                raise _subprocess.CalledProcessError(
                    process.returncode,
                    cmd,
                    output=stdout,
                    stderr=stderr
                )

            print_final_progress(output.name, int(total_duration_sec * 1000), 0)

        except Exception as e:
            logger.exception(f"[merge-file] ffmpeg process error: {e}")
            raise

        _merge_debug("merge-file", step="add chapters")

        # Step 5: Embed chapters into container (MKA, MP4/M4A, or note limitation)
        if output_format == "mka" or output.suffix.lower() == ".mka":
            # MKA/MKV format has native chapter support via FFMetadata
            # Re-mux the file with chapters embedded (copy streams, no re-encode)
            _merge_debug("merge-file", step="embed chapters (matroska)")
            logger.info("[merge-file] Adding chapters to MKA file via FFMetadata")

            temp_output = output.parent / f".temp_{output.stem}.mka"

            # Use mkvmerge if available (best for MKA chapters), otherwise fall back to ffmpeg
            mkvmerge_path = _shutil.which("mkvmerge")

            if mkvmerge_path:
                # mkvmerge is the best tool for embedding chapters in Matroska files
                log("Using mkvmerge for optimal chapter embedding...", file=sys.stderr)
                cmd2 = [
                    mkvmerge_path,
                    "-o",
                    str(temp_output),
                    "--chapters",
                    str(metadata_file),
                    str(output),
                ]
            else:
                # Fallback to ffmpeg with proper chapter embedding for Matroska
                _merge_debug("merge-file", step="embed chapters (ffmpeg)")
                # For Matroska files, the metadata must be provided via -f ffmetadata input
                cmd2 = [
                    ffmpeg_path,
                    "-y",
                    "-i",
                    str(output),  # Input: merged audio
                    "-i",
                    str(metadata_file),  # Input: FFMetadata file
                    "-c:a",
                    "copy",  # Copy audio without re-encoding
                    "-threads",
                    "0",  # Use all threads
                    "-map",
                    "0",  # Map all from first input
                    "-map_chapters",
                    "1",  # Map CHAPTERS from second input (FFMetadata)
                    str(temp_output),  # Output
                ]

            logger.info(f"[merge-file] Running chapter embedding: {' '.join(cmd2)}")

            try:
                # Run chapter embedding silently (progress handled by worker thread)
                _subprocess.run(
                    cmd2,
                    capture_output=True,
                    text=True,
                    stdin=_subprocess.DEVNULL,
                    timeout=600,
                    check=False,
                )

                # Replace original with temp if successful
                if temp_output.exists() and temp_output.stat().st_size > 0:
                    try:
                        import shutil

                        if output.exists():
                            output.unlink()
                        shutil.move(str(temp_output), str(output))
                        _merge_debug("merge-file", step="chapters embedded")
                        logger.info("[merge-file] Chapters embedded successfully")
                    except Exception as e:
                        logger.warning(f"[merge-file] Could not replace file: {e}")
                        log(
                            "Warning: Could not embed chapters, using merge without chapters",
                            file=sys.stderr,
                        )
                        try:
                            temp_output.unlink()
                        except Exception:
                            pass
                else:
                    logger.warning(
                        "[merge-file] Chapter embedding did not create output"
                    )
            except Exception as e:
                logger.exception(f"[merge-file] Chapter embedding failed: {e}")
                log(
                    "Warning: Chapter embedding failed, using merge without chapters",
                    file=sys.stderr,
                )
        elif output_format in {"m4a",
                               "m4b"} or output.suffix.lower() in [".m4a",
                                                                   ".m4b",
                                                                   ".mp4"]:
            # MP4/M4A format has native chapter support via iTunes metadata atoms
            log("Embedding chapters into MP4 container...", file=sys.stderr)
            logger.info(
                "[merge-file] Adding chapters to M4A/MP4 file via iTunes metadata"
            )

            temp_output = output.parent / f".temp_{output.stem}{output.suffix}"

            # ffmpeg embeds chapters in MP4 using -map_metadata and -map_chapters
            log("Using ffmpeg for MP4 chapter embedding...", file=sys.stderr)
            cmd2 = [
                ffmpeg_path,
                "-y",
                "-i",
                str(output),  # Input: merged audio
                "-i",
                str(metadata_file),  # Input: FFMetadata file
                "-c:a",
                "copy",  # Copy audio without re-encoding
                "-threads",
                "0",  # Use all threads
                "-map",
                "0",  # Map all from first input
                "-map_metadata",
                "1",  # Map metadata from second input (FFMetadata)
                "-map_chapters",
                "1",  # Map CHAPTERS from second input (FFMetadata)
                str(temp_output),  # Output
            ]

            logger.info(f"[merge-file] Running MP4 chapter embedding: {' '.join(cmd2)}")

            try:
                # Run MP4 chapter embedding silently (progress handled by worker thread)
                _subprocess.run(
                    cmd2,
                    capture_output=True,
                    text=True,
                    stdin=_subprocess.DEVNULL,
                    timeout=600,
                    check=False,
                )

                # Replace original with temp if successful
                if temp_output.exists() and temp_output.stat().st_size > 0:
                    try:
                        import shutil

                        if output.exists():
                            output.unlink()
                        shutil.move(str(temp_output), str(output))
                        log(
                            "✓ Chapters successfully embedded in MP4!",
                            file=sys.stderr
                        )
                        logger.info("[merge-file] MP4 chapters embedded successfully")
                    except Exception as e:
                        logger.warning(f"[merge-file] Could not replace file: {e}")
                        log(
                            "Warning: Could not embed chapters, using merge without chapters",
                            file=sys.stderr,
                        )
                        try:
                            temp_output.unlink()
                        except Exception:
                            pass
                else:
                    logger.warning(
                        "[merge-file] MP4 chapter embedding did not create output"
                    )
            except Exception as e:
                logger.exception(f"[merge-file] MP4 chapter embedding failed: {e}")
                log(
                    "Warning: MP4 chapter embedding failed, using merge without chapters",
                    file=sys.stderr,
                )
        else:
            # For other formats, chapters would require external tools
            logger.info(
                f"[merge-file] Format {output_format} does not have native chapter support"
            )
            log("Note: For chapter support, use MKA or M4A format", file=sys.stderr)

        # Clean up temp files
        try:
            concat_file.unlink()
        except Exception:
            pass
        try:
            metadata_file.unlink()
        except Exception:
            pass

        return True

    except Exception as e:
        log(f"Audio merge error: {e}", file=sys.stderr)
        logger.error(f"[merge-file] Audio merge error: {e}", exc_info=True)
        return False


def _merge_video(files: List[Path], output: Path, output_format: str) -> bool:
    """Merge video files."""
    ffmpeg_path = _shutil.which("ffmpeg")
    if not ffmpeg_path:
        log("ffmpeg not found in PATH", file=sys.stderr)
        return False

    try:
        # Create concat demuxer file
        concat_file = output.parent / f".concat_{output.stem}.txt"
        concat_lines = []
        for f in files:
            safe_path = str(f).replace("'", "'\\''")
            concat_lines.append(f"file '{safe_path}'")

        concat_file.write_text("\n".join(concat_lines), encoding="utf-8")

        # Build FFmpeg command for video merge
        cmd = [ffmpeg_path, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file)]

        # Video codec selection
        if output_format == "mp4":
            cmd.extend(
                [
                    "-c:v",
                    "libx265",
                    "-preset",
                    "fast",
                    "-tag:v",
                    "hvc1",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                ]
            )
        elif output_format == "mkv":
            cmd.extend(
                ["-c:v",
                 "libx265",
                 "-preset",
                 "fast",
                 "-c:a",
                 "aac",
                 "-b:a",
                 "192k"]
            )
        else:
            cmd.extend(["-c", "copy"])  # Copy without re-encoding

        cmd.append(str(output))

        log(f"Merging {len(files)} video files...", file=sys.stderr)
        result = _subprocess.run(cmd, capture_output=True, text=True)

        # Clean up concat file
        try:
            concat_file.unlink()
        except Exception:
            pass

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            log(f"FFmpeg error: {stderr}", file=sys.stderr)
            return False

        return True

    except Exception as e:
        log(f"Video merge error: {e}", file=sys.stderr)
        return False


def _merge_text(files: List[Path], output: Path) -> bool:
    """Merge text files."""
    try:
        with open(output, "w", encoding="utf-8") as outf:
            for i, f in enumerate(files):
                if i > 0:
                    outf.write("\n---\n")  # Separator between files
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    outf.write(content)
                except Exception as e:
                    log(f"Warning reading {f.name}: {e}", file=sys.stderr)

        return True

    except Exception as e:
        log(f"Text merge error: {e}", file=sys.stderr)
        return False


def _merge_pdf(files: List[Path], output: Path) -> bool:
    """Merge PDF files."""
    if (not HAS_PYPDF) or (PdfWriter is None) or (PdfReader is None):
        log(
            "pypdf is required for PDF merging. Install with: pip install pypdf",
            file=sys.stderr
        )
        return False

    try:
        writer = PdfWriter()

        for f in files:
            try:
                reader = PdfReader(f)
                for page in reader.pages:
                    writer.add_page(page)
                log(f"Added {len(reader.pages)} pages from {f.name}", file=sys.stderr)
            except Exception as e:
                log(f"Error reading PDF {f.name}: {e}", file=sys.stderr)
                return False

        with open(output, "wb") as outf:
            writer.write(outf)

        return True

    except Exception as e:
        log(f"PDF merge error: {e}", file=sys.stderr)
        return False


CMDLET = Cmdlet(
    name="merge-file",
    summary=
    "Merge multiple files into a single output file. Supports audio, video, PDF, and text merging with optional cleanup.",
    usage=
    "merge-file [-delete] [-path <path>] [-format <auto|mka|m4a|m4b|mp3|aac|opus|mp4|mkv|pdf|txt>]",
    arg=[
        CmdletArg(
            "-delete",
            type="flag",
            description="Delete source files after successful merge."
        ),
        SharedArgs.PATH,
        CmdletArg(
            "-format",
            description=
            "Output format (auto/mka/m4a/m4b/mp3/aac/opus/mp4/mkv/pdf/txt). Default: auto-detect from first file.",
        ),
    ],
    detail=[
        "- Pipe multiple files: search-file query | [1,2,3] | merge-file",
        "- Audio files merge with minimal quality loss using specified codec.",
        "- Video files merge into MP4 or MKV containers.",
        "- PDF files merge into a single PDF document.",
        "- Text/document files are concatenated.",
        "- Output name derived from first file with ' (merged)' suffix.",
        "- -delete flag removes all source files after successful merge.",
    ],
)

CMDLET.exec = _run
CMDLET.register()
