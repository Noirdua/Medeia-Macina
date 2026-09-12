"""General-purpose helpers used across the downlow CLI."""

from __future__ import annotations

import json
import hashlib
import shutil
import os
import base64
import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, List, Sequence




def split_instance_names(value: Any) -> List[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        text = text[1:]
        if "]" in text:
            text = text[: text.index("]")]
        else:
            text = text.rstrip("]")
    else:
        text = text.replace("+", ",")
    names: List[str] = []
    seen: set[str] = set()
    for part in text.split(","):
        name = part.strip().strip("'\"").strip("[]")
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def consume_bracket_list(args: Sequence[Any], flag_index: int) -> tuple[str, int]:
    """Read a -flag value, joining tokens until a [list] is closed."""
    if flag_index < 0 or flag_index + 1 >= len(args):
        return "", flag_index + 1
    i = flag_index + 1
    value = str(args[i] or "")
    i += 1
    while i < len(args) and value.count("[") > value.count("]"):
        nxt = str(args[i] or "")
        if nxt.startswith("-") and nxt.lstrip("-")[:1].isalpha():
            break
        sep = "" if value.endswith(",") or nxt.startswith(",") else ","
        value = f"{value}{sep}{nxt}"
        i += 1
    return value, i


def format_byte_size(size_bytes: Any) -> str:
    return format_bytes(size_bytes)


_ffmpeg_mod: Any = None
_ffmpeg_checked = False


def _get_ffmpeg():
    """Lazily return the ffmpeg module, or None if unavailable."""
    global _ffmpeg_mod, _ffmpeg_checked
    if not _ffmpeg_checked:
        try:
            import ffmpeg as _f  # type: ignore
            _ffmpeg_mod = _f
        except Exception:
            _ffmpeg_mod = None
        _ffmpeg_checked = True
    return _ffmpeg_mod

try:
    import cbor2
except ImportError:
    cbor2 = None  # type: ignore

CHUNK_SIZE = 1024 * 1024  # 1 MiB
_format_logger = logging.getLogger(__name__)


def default_staging_dir() -> Path:
    raw = Path(tempfile.gettempdir())
    try:
        resolved_tmp = raw.resolve()
    except Exception:
        resolved_tmp = raw
    candidates = []
    if resolved_tmp.parent != resolved_tmp:
        candidates.append(resolved_tmp / "medios")
    try:
        candidates.append(Path.home() / ".cache" / "medios")
    except Exception:
        pass
    for path in candidates:
        try:
            if path.parent == path:
                continue
            path.mkdir(parents=True, exist_ok=True)
            return path
        except Exception:
            continue
    path = Path.home() / ".medios-tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_unsafe_output_dir(path: str | Path | None) -> bool:
    if path is None:
        return True
    text = str(path).strip()
    if not text or text in {".", ".."}:
        return True
    try:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            return True
        resolved = candidate.resolve()
    except Exception:
        return True
    if resolved.parent == resolved:
        return True
    return False


def safe_output_dir(path: str | Path | None) -> Path:
    text = str(path or "").strip()
    if not text or text in {".", ".."}:
        return default_staging_dir()
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        return (default_staging_dir() / candidate).resolve()
    try:
        resolved = candidate.resolve()
    except Exception:
        return default_staging_dir()
    if resolved.parent == resolved:
        return default_staging_dir()
    return resolved


def expand_path(p: str | Path | None) -> Path:
    """Expand ~ and environment variables in path."""
    if p is None:
        return None  # type: ignore
    s = str(p)
    # Courtesy check for $home -> $HOME if we're on a POSIX-like system
    # (where env vars are case-sensitive)
    if os.name != 'nt' and '$home' in s and '$HOME' not in os.environ:
         # If $home is literally used in config but only HOME is defined
         if 'HOME' in os.environ:
             s = s.replace('$home', '$HOME')
    
    expanded = os.path.expandvars(s)
    return Path(expanded).expanduser()


def ensure_directory(path: Path) -> None:
    """Ensure *path* exists as a directory."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - surfaced to caller
        raise RuntimeError(f"Failed to create directory {path}: {exc}") from exc


def unique_path(path: Path) -> Path:
    """Return a unique path by appending " (n)" if needed."""
    try:
        if not path.exists():
            return path
    except Exception:
        return path
    stem = path.stem or "download"
    suffix = path.suffix
    parent = path.parent
    for counter in range(1, 10_000):
        candidate = parent / f"{stem} ({counter}){suffix}"
        try:
            if not candidate.exists():
                return candidate
        except Exception:
            return candidate
    return parent / f"{stem} (copy){suffix}"


_WINDOWS_RESERVED_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
})


def coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "enable", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disable", "disabled"}:
        return False
    return default


SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def normalize_sha256_hex(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or SHA256_HEX_RE.fullmatch(text) is None:
        return None
    return text.lower()


def is_sha256_hex(value: Any) -> bool:
    return normalize_sha256_hex(value) is not None


def value_normalize(value: Any) -> str:
    text = str(value).strip()
    return text.lower() if text else ""


def sanitize_filename(name: str, *, max_len: int = 150, fallback: str = "download") -> str:
    """Return a filesystem-safe filename derived from *name*."""
    text = str(name or "").strip()
    if not text:
        return fallback

    forbidden = set('<>:"/\\|?*')
    cleaned_chars: list[str] = []
    for ch in text:
        if ord(ch) < 32 or ch in forbidden:
            cleaned_chars.append(" ")
            continue
        cleaned_chars.append(ch)
    cleaned = " ".join("".join(cleaned_chars).split()).strip().strip(".")
    if not cleaned:
        cleaned = fallback
    stem = cleaned.rsplit(".", 1)[0].casefold()
    if stem in _WINDOWS_RESERVED_NAMES:
        cleaned = "_" + cleaned
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
    return cleaned or fallback


def unique_preserve_order(values: Iterable[Any]) -> list[Any]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def sha256_file(file_path: Path) -> str:
    """Return the SHA-256 hex digest of *path*."""
    hasher = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def ffprobe(file_path: str) -> dict:
    """Probe a media file and return a metadata dictionary.

    This function prefers the python `ffmpeg` module (ffmpeg-python) when available.
    If that is not present, it will attempt to call the external `ffprobe` binary if found
    on PATH. If neither is available or probing fails, an empty dict is returned.
    """
    probe = None

    # Try python ffmpeg module first
    ffmpeg = _get_ffmpeg()
    if ffmpeg is not None:
        try:
            probe = ffmpeg.probe(file_path)
        except Exception as exc:  # pragma: no cover - environment dependent
            _format_logger.debug("ffmpeg.probe failed: %s", exc)
            probe = None

    # Fall back to external ffprobe if available
    if probe is None:
        ffprobe_cmd = shutil.which("ffprobe")
        if ffprobe_cmd:
            try:
                import subprocess as _subprocess
                proc = _subprocess.run(
                    [
                        ffprobe_cmd,
                        "-v",
                        "quiet",
                        "-print_format",
                        "json",
                        "-show_format",
                        "-show_streams",
                        str(file_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                probe = json.loads(proc.stdout)
            except Exception as exc:  # pragma: no cover - environment dependent
                _format_logger.debug("External ffprobe failed: %s", exc)
                probe = None
        else:
            _format_logger.debug("No ffmpeg Python module and no ffprobe binary found")
            return {}

    if not isinstance(probe, dict):
        return {}

    metadata = {}
    fmt = probe.get("format",
                    {})
    metadata["duration"] = float(fmt.get("duration", 0)) if "duration" in fmt else None
    metadata["size"] = int(fmt.get("size", 0)) if "size" in fmt else None
    metadata["format_name"] = fmt.get("format_name", None)

    # Stream-level info
    for stream in probe.get("streams", []):
        codec_type = stream.get("codec_type")
        if codec_type == "audio":
            metadata["audio_codec"] = stream.get("codec_name")
            metadata["bitrate"] = int(
                stream.get("bit_rate",
                           0)
            ) if "bit_rate" in stream else None
            metadata["samplerate"] = (
                int(stream.get("sample_rate",
                               0)) if "sample_rate" in stream else None
            )
            metadata["channels"] = int(
                stream.get("channels",
                           0)
            ) if "channels" in stream else None
        elif codec_type == "video":
            metadata["video_codec"] = stream.get("codec_name")
            metadata["width"] = int(
                stream.get("width",
                           0)
            ) if "width" in stream else None
            metadata["height"] = int(
                stream.get("height",
                           0)
            ) if "height" in stream else None
        elif codec_type == "image":
            metadata["image_codec"] = stream.get("codec_name")
            metadata["width"] = int(
                stream.get("width",
                           0)
            ) if "width" in stream else None
            metadata["height"] = int(
                stream.get("height",
                           0)
            ) if "height" in stream else None

    return metadata


# ============================================================================
# CBOR Utilities - Consolidated from cbor.py
# ============================================================================
"""CBOR utilities backed by the `cbor2` library."""


def decode_cbor(data: bytes) -> Any:
    """Decode *data* from CBOR into native Python objects."""
    if not data:
        return None
    if cbor2 is None:
        raise ImportError("cbor2 library is required for CBOR decoding")
    return cbor2.loads(data)


def jsonify(value: Any) -> Any:
    """Convert *value* into a JSON-friendly structure."""
    if isinstance(value, dict):
        return {
            str(key): jsonify(val)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [jsonify(item) for item in value]
    if isinstance(value, bytes):
        return {
            "__bytes__": base64.b64encode(value).decode("ascii")
        }
    return value


# ============================================================================
# Format Utilities - Consolidated from format_utils.py
# ============================================================================
"""Formatting utilities for displaying metadata consistently across the application."""


def format_bytes(bytes_value) -> str:
    """Format bytes to human-readable format (e.g., '1.5 MB', '250 KB').

    Args:
        bytes_value: Size in bytes (int or float)

    Returns:
        Formatted string like '1.5 MB' or '756 MB'
    """
    if bytes_value is None:
        return ""
    if not isinstance(bytes_value, (int, float)):
        try:
            bytes_value = float(bytes_value)
        except (TypeError, ValueError):
            return str(bytes_value or "")
    if bytes_value <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if bytes_value < 1024:
            if unit == "B":
                return f"{int(bytes_value)} {unit}"
            return f"{bytes_value:.1f} {unit}"
        bytes_value /= 1024
    return f"{bytes_value:.1f} PB"



def extract_hydrus_hash_from_url(url: str) -> str | None:
    """Extract SHA256 hash from Hydrus API URL.
    
    Handles URLs like:
    - http://localhost:45869/get_files/file?hash=abc123...
    - URLs with &hash=abc123...
    
    Args:
        url: URL string to extract hash from
    
    Returns:
        Hash hex string (lowercase, 64 chars) if valid SHA256, None otherwise
    """
    try:
        match = re.search(r"[?&]hash=([0-9a-fA-F]+)", str(url or ""))
        if match:
            return normalize_sha256_hex(match.group(1))
    except Exception:
        pass
    return None

