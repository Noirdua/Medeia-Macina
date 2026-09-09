from __future__ import annotations

from typing import Any, Dict, Sequence, Optional
from pathlib import Path
import sys
import shutil
import subprocess

from SYS.logger import log, debug
from SYS.payload_builders import build_file_result_payload
from SYS.utils import sha256_file
from .. import _shared as sh
from SYS import pipeline as ctx

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
QueryArg = sh.QueryArg
SharedArgs = sh.SharedArgs
parse_cmdlet_args = sh.parse_cmdlet_args
normalize_result_input = sh.normalize_result_input
extract_title_from_result = sh.extract_title_from_result


VIDEO_EXTS = {
    "mp4",
    "mkv",
    "webm",
    "mov",
    "avi",
    "flv",
    "mpeg",
    "mpg",
    "m4v",
}

AUDIO_EXTS = {
    "mp3",
    "m4a",
    "m4b",
    "aac",
    "flac",
    "wav",
    "ogg",
    "opus",
    "mka",
}

IMAGE_EXTS = {
    "png",
    "jpg",
    "jpeg",
    "webp",
    "bmp",
    "tif",
    "tiff",
    "gif",
}

DOC_EXTS = {
    "pdf",
    "mobi",
    "epub",
    "azw3",
    "txt",
    "rtf",
    "html",
    "htm",
    "md",
    "doc",
    "docx",
}


def _detect_kind(ext: str) -> str:
    e = ext.lower().lstrip(".")
    if e in VIDEO_EXTS:
        return "video"
    if e in AUDIO_EXTS:
        return "audio"
    if e in IMAGE_EXTS:
        return "image"
    if e in DOC_EXTS:
        return "doc"
    return "unknown"


def _allowed(source_kind: str, target_kind: str, target_ext: str = "") -> bool:
    if source_kind == target_kind:
        return True
    if source_kind == "video" and target_kind == "audio":
        return True
    if source_kind == "video" and target_kind == "image" and target_ext.lower().lstrip(".") == "gif":
        return True
    return False


def _ffmpeg_convert(
    input_path: Path,
    output_path: Path,
    target_kind: str,
    copy_metadata: bool,
) -> bool:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        log("ffmpeg not found in PATH", file=sys.stderr)
        return False

    cmd = [ffmpeg_path, "-y", "-i", str(input_path)]

    if target_kind == "audio":
        cmd.extend(["-vn"])

    if copy_metadata:
        cmd.extend(["-map_metadata", "0"])

    cmd.append(str(output_path))

    debug(f"[convert-file] Running ffmpeg: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        log(f"ffmpeg error: {proc.stderr}", file=sys.stderr)
        return False
    return True


def _doc_convert(input_path: Path, output_path: Path) -> bool:
    try:
        import pypandoc  # type: ignore
    except Exception:
        log("pypandoc is required for document conversion; install pypandoc-binary", file=sys.stderr)
        return False

    target_fmt = output_path.suffix.lstrip(".").lower() or "pdf"
    extra_args = []

    if target_fmt == "pdf":
        tectonic_path = shutil.which("tectonic")
        if not tectonic_path:
            log(
                "tectonic is required for PDF output; install with `pip install tectonic`",
                file=sys.stderr,
            )
            return False
        extra_args = ["--pdf-engine=tectonic"]

    try:
        pypandoc.convert_file(
            str(input_path),
            to=target_fmt,
            outputfile=str(output_path),
            extra_args=extra_args,
        )
    except OSError as exc:
        log(f"pandoc is missing or failed to run: {exc}", file=sys.stderr)
        return False
    except Exception as exc:
        log(f"pypandoc conversion failed: {exc}", file=sys.stderr)
        return False

    if not output_path.exists():
        log("pypandoc conversion did not produce an output file", file=sys.stderr)
        return False

    return True


CMDLET = Cmdlet(
    name="convert-file",
    summary="Convert files between media/container formats (video, audio, image, documents).",
    usage="convert-file -to <format> [-path <file|dir>] [-delete] [-query format:<fmt>]",
    arg=[
        QueryArg("to", key="format", query_only=False, required=True,
                 description="Target format/extension (e.g., mp4, mp3, wav, jpg, pdf)."),
        SharedArgs.PATH,
        SharedArgs.QUERY,
        SharedArgs.DELETE,
    ],
    detail=[
        "Allows video↔video, audio↔audio, image↔image, doc↔doc, video→audio, and video→gif conversions.",
        "Disallows incompatible conversions (e.g., video→pdf).",
        "Uses ffmpeg for media and pypandoc-binary (bundled pandoc) for document formats (mobi/epub→pdf/txt/etc); PDF output uses the tectonic LaTeX engine when available.",
    ],
)


def _resolve_output_path(input_path: Path, outdir: Optional[Path], target_ext: str) -> Path:
    base = input_path.stem
    directory = outdir if outdir is not None else input_path.parent
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{base}.{target_ext}"
    if candidate.exists():
        for i in range(1, 1000):
            alt = directory / f"{base}_{i}.{target_ext}"
            if not alt.exists():
                candidate = alt
                break
    return candidate


def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    parsed = parse_cmdlet_args(args, CMDLET)

    target_fmt_raw = parsed.get("to") or parsed.get("format")
    if not target_fmt_raw:
        log("-to <format> is required", file=sys.stderr)
        return 1
    target_fmt = str(target_fmt_raw).lower().lstrip(".")
    target_kind = _detect_kind(target_fmt)
    if target_kind == "unknown":
        log(f"Unsupported target format: {target_fmt}", file=sys.stderr)
        return 1

    delete_src = bool(parsed.get("delete", False))

    inputs = normalize_result_input(result)
    path_arg = parsed.get("path")

    outdir_override: Optional[Path] = None
    if path_arg:
        try:
            p = Path(str(path_arg)).expanduser()
            if p.exists() and p.is_dir():
                outdir_override = p
            else:
                inputs.append({"path": p})
        except Exception:
            inputs.append({"path": path_arg})

    if not inputs:
        log("No input provided to convert-file", file=sys.stderr)
        return 1

    success = 0

    for item in inputs:
        input_path: Optional[Path] = None
        if isinstance(item, dict):
            p = item.get("path") or item.get("target")
        elif hasattr(item, "path"):
            p = getattr(item, "path")
        else:
            p = item

        try:
            input_path = Path(str(p)) if p else None
        except Exception:
            input_path = None

        if not input_path or not input_path.exists() or not input_path.is_file():
            log("convert-file: input path missing or not found", file=sys.stderr)
            continue

        source_ext = input_path.suffix.lower().lstrip(".")
        source_kind = _detect_kind(source_ext)

        if not _allowed(source_kind, target_kind, target_fmt):
            log(
                f"Conversion from {source_kind or 'unknown'} to {target_kind} is not allowed",
                file=sys.stderr,
            )
            continue

        output_path = _resolve_output_path(input_path, outdir_override, target_fmt)

        converted = False
        if target_kind in {"video", "audio", "image"}:
            converted = _ffmpeg_convert(input_path, output_path, target_kind, copy_metadata=True)
        elif target_kind == "doc":
            converted = _doc_convert(input_path, output_path)
        else:
            log(f"No converter for target kind {target_kind}", file=sys.stderr)

        if not converted:
            continue

        try:
            out_hash = sha256_file(output_path)
        except Exception:
            out_hash = None

        title = extract_title_from_result(item) or output_path.stem

        ctx.emit(
            build_file_result_payload(
                title=title,
                path=str(output_path),
                hash_value=out_hash,
                media_kind=target_kind,
                source_path=str(input_path),
            )
        )

        if delete_src:
            try:
                input_path.unlink()
                log(f"Deleted source file: {input_path}", file=sys.stderr)
            except Exception as exc:
                log(f"Failed to delete source {input_path}: {exc}", file=sys.stderr)

        success += 1

    return 0 if success else 1


CMDLET.exec = _run
CMDLET.register()
