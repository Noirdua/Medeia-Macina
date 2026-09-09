"""General-purpose helpers used across the downlow CLI."""

from __future__ import annotations

import json
import hashlib
import shutil
import os
import base64
import logging
import tempfile
from pathlib import Path
from typing import Any, Iterable, List, Sequence
from datetime import datetime
from dataclasses import dataclass, field
from fnmatch import fnmatch
from urllib.parse import urlparse

from SYS.utils_constant import mime_maps

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
    if size_bytes is None:
        return ""
    try:
        size = int(size_bytes)
    except (TypeError, ValueError):
        return str(size_bytes or "")
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


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


def sanitize_metadata_value(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.replace("\x00", " ").replace("\r", " ").replace("\n", " ").strip()
    if not value:
        return None
    return value


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
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


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


def unique_preserve_order(values: Iterable[str]) -> list[str]:
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


def create_metadata_sidecar(file_path: Path, metadata: dict) -> None:
    """Create a .metadata sidecar file with JSON metadata.

    The metadata dict should contain title. If not present, it will be derived from
    the filename. This ensures the .metadata file can be matched during batch import.

    Args:
        file_path: Path to the exported file
        metadata: Dictionary of metadata to save
    """
    if not metadata:
        return
    file_name = file_path.stem
    file_ext = file_path.suffix.lower()
    # Ensure metadata has a title field that matches the filename (without extension)
    # This allows the sidecar to be matched and imported properly during batch import
    if "title" not in metadata or not metadata.get("title"):
        metadata["title"] = file_name
    metadata["hash"] = sha256_file(file_path)
    metadata["size"] = Path(file_path).stat().st_size
    format_found = False
    for mime_type, ext_map in mime_maps.items():
        for key, info in ext_map.items():
            if info.get("ext") == file_ext:
                metadata["type"] = mime_type
                format_found = True
                break
        if format_found:
            break
    else:
        metadata["type"] = "unknown"
    metadata.update(ffprobe(str(file_path)))

    metadata_path = file_path.with_suffix(file_path.suffix + ".metadata")
    try:
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise RuntimeError(
            f"Failed to write metadata sidecar {metadata_path}: {exc}"
        ) from exc


def create_tags_sidecar(file_path: Path, tags: set) -> None:
    """Create a .tag sidecar file with tags (one per line).

    Args:
        file_path: Path to the exported file
        tags: Set of tag strings
    """
    if not tags:
        return

    tags_path = file_path.with_suffix(file_path.suffix + ".tag")
    try:
        with open(tags_path, "w", encoding="utf-8") as f:
            for tag in sorted(tags):
                f.write(f"{str(tag).strip().lower()}\n")
    except Exception as e:
        raise RuntimeError(f"Failed to create tags sidecar {tags_path}: {e}") from e


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
    if bytes_value is None or bytes_value <= 0:
        return "0 B"

    if isinstance(bytes_value, (int, float)):
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if bytes_value < 1024:
                if unit == "B":
                    return f"{int(bytes_value)} {unit}"
                return f"{bytes_value:.1f} {unit}"
            bytes_value /= 1024
        return f"{bytes_value:.1f} PB"
    return str(bytes_value)


def format_duration(seconds) -> str:
    """Format duration in seconds to human-readable format (e.g., '1h 23m 5s', '5m 30s').

    Args:
        seconds: Duration in seconds (int or float)

    Returns:
        Formatted string like '1:23:45' or '5:30'
    """
    if seconds is None or seconds == "":
        return "N/A"

    if isinstance(seconds, str):
        try:
            seconds = float(seconds)
        except ValueError:
            return str(seconds)

    if not isinstance(seconds, (int, float)):
        return str(seconds)

    total_seconds = int(seconds)
    if total_seconds < 0:
        return "N/A"

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    elif minutes > 0:
        return f"{minutes}:{secs:02d}"
    else:
        return f"{secs}s"


def format_timestamp(timestamp_str) -> str:
    """Format ISO timestamp to readable format.

    Args:
        timestamp_str: ISO format timestamp string or None

    Returns:
        Formatted string like "2025-10-28 19:36:01" or original string if parsing fails
    """
    if not timestamp_str:
        return "N/A"

    try:
        # Handle ISO format timestamps
        if isinstance(timestamp_str, str):
            # Try parsing ISO format
            if "T" in timestamp_str:
                dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            else:
                # Try other common formats
                dt = datetime.fromisoformat(timestamp_str)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        _format_logger.debug(f"Could not parse timestamp '{timestamp_str}': {e}")

    return str(timestamp_str)


def format_metadata_value(key: str, value) -> str:
    """Format a metadata value based on its key for display.

    This is the central formatting rule for all metadata display.

    Args:
        key: Metadata field name
        value: Value to format

    Returns:
        Formatted string for display
    """
    if value is None or value == "":
        return "N/A"

    # Apply field-specific formatting
    if key in ("size", "file_size"):
        return format_bytes(value)
    elif key in ("duration", "length"):
        return format_duration(value)
    elif key in (
            "time_modified",
            "time_imported",
            "created_at",
            "updated_at",
            "indexed_at",
            "timestamp",
    ):
        return format_timestamp(value)
    else:
        return str(value)


# ============================================================================
# Link Utilities - Consolidated from link_utils.py
# ============================================================================
"""Link utilities - Extract and process url from various sources."""


def extract_link_from_args(args: Iterable[str]) -> Any | None:
    """Extract HTTP/HTTPS URL from command arguments.

    Args:
        args: Command arguments

    Returns:
        URL string if found, None otherwise
    """
    args_list = list(args) if not isinstance(args, (list, tuple)) else args
    if not args_list or len(args_list) == 0:
        return None

    potential_link = str(args_list[0])
    if potential_link.startswith(("http://", "https://")):
        return potential_link

    return None


def extract_link_from_result(result: Any) -> Any | None:
    """Extract URL from a result object (dict or object with attributes).

    Args:
        result: Result object from pipeline (dict or object)

    Returns:
        URL string if found, None otherwise
    """
    if isinstance(result, dict):
        return result.get("url") or result.get("link") or result.get("href")

    return (
        getattr(result,
                "url",
                None) or getattr(result,
                                 "link",
                                 None) or getattr(result,
                                                  "href",
                                                  None)
    )


def extract_link(result: Any, args: Iterable[str]) -> Any | None:
    """Extract link from args or result (args take priority).

    Args:
        result: Pipeline result object
        args: Command arguments

    Returns:
        URL string if found, None otherwise
    """
    # Try args first
    link = extract_link_from_args(args)
    if link:
        return link

    # Fall back to result
    return extract_link_from_result(result)


def get_api_key(config: dict[str, Any], service: str, key_path: str) -> str | None:
    """Get API key from a dot-notation config path.

    Args:
        config: Configuration dictionary
        service: Service name for logging
        key_path: Dot-notation path to key (e.g., "plugin.alldebrid.api_key")

    Returns:
        API key if found and not empty, None otherwise
    """
    try:
        parts = key_path.split(".")
        value: Any = config
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None

        if isinstance(value, str):
            return value.strip() or None

        return None
    except Exception:
        _format_logger.exception("Failed to resolve nested config key '%s'", key_path)
        return None


def add_direct_link_to_result(
    result: Any,
    direct_link: str,
    original_link: str
) -> None:
    """Add direct link information to result object.

    Args:
        result: Result object to modify (dict or object)
        direct_link: The unlocked/direct URL
        original_link: The original restricted URL
    """
    if isinstance(result, dict):
        result["direct_link"] = direct_link
        result["original_link"] = original_link
    else:
        setattr(result, "direct_link", direct_link)
        setattr(result, "original_link", original_link)


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
        import re
        match = re.search(r"[?&]hash=([0-9a-fA-F]+)", str(url or ""))
        if match:
            hash_hex = match.group(1).strip().lower()
            # Validate SHA256 (exactly 64 hex chars)
            if re.fullmatch(r"[0-9a-f]{64}", hash_hex):
                return hash_hex
    except Exception:
        pass
    return None


# ============================================================================
# URL Policy Resolution - Consolidated from url_parser.py
# ============================================================================
"""URL policy resolution for downlow workflows."""


@dataclass(slots=True)
class UrlPolicy:
    """Describe how a URL should be handled by download and screenshot flows."""

    skip_download: bool = False
    skip_metadata: bool = False
    force_screenshot: bool = False
    extra_tags: list[str] = field(default_factory=list)

    def apply_tags(self, sources: Iterable[str]) -> list[str]:
        tags = [tag.strip() for tag in self.extra_tags if tag and tag.strip()]
        for value in sources:
            text = str(value).strip()
            if text:
                tags.append(text)
        return tags


def _normalize_rule(rule: dict[str, Any]) -> dict[str, Any] | None:
    pattern = str(rule.get("pattern") or rule.get("host") or "").strip()
    if not pattern:
        return None
    skip_download = bool(rule.get("skip_download"))
    skip_metadata = bool(rule.get("skip_metadata"))
    force_screenshot = bool(rule.get("force_screenshot"))
    extra_tags_raw = rule.get("extra_tags")
    if isinstance(extra_tags_raw, str):
        extra_tags = [
            part.strip() for part in extra_tags_raw.split(",") if part.strip()
        ]
    elif isinstance(extra_tags_raw, (list, tuple, set)):
        extra_tags = [str(item).strip() for item in extra_tags_raw if str(item).strip()]
    else:
        extra_tags = []
    return {
        "pattern": pattern,
        "skip_download": skip_download,
        "skip_metadata": skip_metadata,
        "force_screenshot": force_screenshot,
        "extra_tags": extra_tags,
    }


def resolve_url_policy(config: dict[str, Any], url: str) -> UrlPolicy:
    policies_raw = config.get("url_policies")
    if not policies_raw:
        return UrlPolicy()
    if not isinstance(policies_raw, list):
        return UrlPolicy()
    parsed = urlparse(url)
    subject = f"{parsed.netloc}{parsed.path}"
    host = parsed.netloc
    resolved = UrlPolicy()
    for rule_raw in policies_raw:
        if not isinstance(rule_raw, dict):
            continue
        rule = _normalize_rule(rule_raw)
        if rule is None:
            continue
        pattern = rule["pattern"]
        if not (fnmatch(host, pattern) or fnmatch(subject, pattern)):
            continue
        if rule["skip_download"]:
            resolved.skip_download = True
        if rule["skip_metadata"]:
            resolved.skip_metadata = True
        if rule["force_screenshot"]:
            resolved.force_screenshot = True
        if rule["extra_tags"]:
            for tag in rule["extra_tags"]:
                if tag not in resolved.extra_tags:
                    resolved.extra_tags.append(tag)
    return resolved
