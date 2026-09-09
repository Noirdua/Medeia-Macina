""" """

from __future__ import annotations

import json
import sqlite3
import time
import os
import re
import datetime
import shutil
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from SYS.logger import log, debug
import logging
logger = logging.getLogger(__name__)
from SYS.utils import expand_path, safe_output_dir
from SYS.database import db, get_config_all, rows_to_config

try:
    from SYS.config_crypto import encrypt_config_value as _encrypt_config_value
except Exception:
    _encrypt_config_value = None

SCRIPT_DIR = Path(__file__).resolve().parent

# Save lock settings (cross-process)
_SAVE_LOCK_DIRNAME = ".medios_save_lock"
_SAVE_LOCK_TIMEOUT = 30.0  # seconds to wait for save lock
_SAVE_LOCK_STALE_SECONDS = 3600  # consider lock stale after 1 hour
_SAVE_LOCK_POLL_INTERVAL = 0.1  # seconds between lock acquisition attempts

_WAL_CHECKPOINT_TIMEOUT = 5.0  # seconds for WAL checkpoint connection

_CONFIG_CACHE: Dict[str, Any] = {}
_LAST_SAVED_CONFIG: Dict[str, Any] = {}
_CONFIG_SUMMARY_PENDING = False
_CONFIG_SAVE_MAX_RETRIES = 5
_CONFIG_SAVE_RETRY_DELAY = 0.15
_CONFIG_SAVE_VERIFY_RETRIES = 3
_CONFIG_MISSING = object()
_PATH_ALIAS_TOKEN_RE = re.compile(r"^\$(?:\((?P<braced>[^)]+)\)|(?P<plain>[A-Za-z0-9_.-]+))$")


class ConfigSaveConflict(Exception):
    """Raised when a save would overwrite external changes present on disk."""
    pass


_DEFAULT_DATE_FORMAT = "YYYY-MM-DD"
_DEFAULT_TAG_PLACEHOLDER_PREFIX = "$"
_LEGACY_TAG_PLACEHOLDER_PREFIXES = ("#",)
DATE_FORMAT_CHOICES = [
    "YYYY-MM-DD",
    "YYYY/MM/DD",
    "DD/MM/YYYY",
    "MM/DD/YYYY",
    "DD-MM-YYYY",
    "MM-DD-YYYY",
    "YY-MM-DD",
    "DD/MM/YY",
    "MM/DD/YY",
    "MMMM DD YYYY",
    "MMM DD YYYY",
]


def get_tag_placeholder_prefix(config: Optional[Dict[str, Any]] = None) -> str:
    raw = ""
    if isinstance(config, dict):
        raw = str(config.get("tag_placeholder_prefix") or "").strip()
    if not raw:
        try:
            loaded = load_config(emit_summary=False) if config is None else {}
            if isinstance(loaded, dict):
                raw = str(loaded.get("tag_placeholder_prefix") or "").strip()
        except Exception:
            raw = ""
    text = (raw or _DEFAULT_TAG_PLACEHOLDER_PREFIX).strip()
    if not text or len(text) > 8:
        return _DEFAULT_TAG_PLACEHOLDER_PREFIX
    return text


def get_tag_placeholder_prefixes(config: Optional[Dict[str, Any]] = None) -> tuple[str, ...]:
    primary = get_tag_placeholder_prefix(config)
    extras = [p for p in _LEGACY_TAG_PLACEHOLDER_PREFIXES if p != primary]
    return (primary, *extras)


def wrap_tag_placeholder(inner: str, config: Optional[Dict[str, Any]] = None) -> str:
    return f"{get_tag_placeholder_prefix(config)}({inner})"


def get_date_format(config: Optional[Dict[str, Any]] = None) -> str:
    raw = ""
    if isinstance(config, dict):
        raw = str(config.get("date_format") or "").strip()
    if not raw:
        try:
            loaded = load_config(emit_summary=False) if config is None else {}
            if isinstance(loaded, dict):
                raw = str(loaded.get("date_format") or "").strip()
        except Exception:
            raw = ""
    text = raw or _DEFAULT_DATE_FORMAT
    if "Y" not in text.upper() and "M" not in text.upper() and "D" not in text.upper():
        return _DEFAULT_DATE_FORMAT
    return text


def global_config() -> List[Dict[str, Any]]:
    """Return configuration schema for global settings."""
    return [
        {
            "key": "debug",
            "label": "Debug Output",
            "group": "Runtime",
            "type": "boolean",
            "default": "false",
            "choices": ["true", "false"],
        },
        {
            "key": "auto_update",
            "label": "Auto-Update",
            "group": "Runtime",
            "type": "boolean",
            "default": "true",
            "choices": ["true", "false"],
        },
        {
            "key": "table_appearance",
            "label": "Table Theme",
            "group": "Appearance",
            "default": "rainbow",
            "choices": [
                "plain",
                "bw-striped",
                "dim-striped",
                "rainbow",
                "nord",
                "dracula",
                "solarized-dark",
                "matrix",
                "ocean",
                "high-contrast",
            ],
        },
        {
            "key": "theme_header_style",
            "label": "Custom Header Style",
            "group": "Appearance",
            "type": "string",
            "default": "",
        },
        {
            "key": "theme_border_style",
            "label": "Custom Border Style",
            "group": "Appearance",
            "type": "string",
            "default": "",
        },
        {
            "key": "theme_panel_style",
            "label": "Custom Panel Style",
            "group": "Appearance",
            "type": "string",
            "default": "",
        },
        {
            "key": "theme_row_styles",
            "label": "Custom Row Styles (JSON)",
            "group": "Appearance",
            "type": "json",
            "default": [],
        },
        {
            "key": "table_box",
            "label": "Table Box Style",
            "group": "Appearance",
            "default": "none",
            "choices": [
                "none",
                "simple",
                "simple-head",
                "rounded",
                "square",
                "heavy",
                "heavy-head",
                "double",
                "ascii",
                "minimal",
                "markdown",
            ],
        },
        {
            "key": "panel_box",
            "label": "Panel Border Style",
            "group": "Appearance",
            "default": "rounded",
            "choices": ["rounded", "square", "heavy", "double", "ascii", "simple", "minimal"],
        },
        {
            "key": "table_show_lines",
            "label": "Table Row Lines",
            "group": "Appearance",
            "type": "boolean",
            "default": "false",
            "choices": ["true", "false"],
        },
        {
            "key": "table_expand",
            "label": "Tables Fill Terminal Width",
            "group": "Appearance",
            "type": "boolean",
            "default": "false",
            "choices": ["true", "false"],
        },
        {
            "key": "table_header_case",
            "label": "Table Header Case",
            "group": "Appearance",
            "default": "upper",
            "choices": ["upper", "title", "as-is"],
        },
        {
            "key": "color_system",
            "label": "Color System",
            "group": "Appearance",
            "default": "auto",
            "choices": ["auto", "standard", "256", "truecolor", "none"],
        },
        {
            "key": "complete_while_typing",
            "label": "Complete While Typing",
            "group": "Runtime",
            "type": "boolean",
            "default": "true",
            "choices": ["true", "false"],
        },
        {
            "key": "date_format",
            "label": "Date Format",
            "group": "Tags",
            "default": "YYYY-MM-DD",
            "choices": list(DATE_FORMAT_CHOICES),
        },
        {
            "key": "tag_placeholder_prefix",
            "label": "Tag Placeholder Prefix",
            "group": "Tags",
            "default": "$",
            "choices": ["$", "#", "`"],
        },
        {
            "key": "autotag_llm_url",
            "label": "Auto-tag LLM URL (optional)",
            "group": "Tags",
            "type": "string",
            "default": "",
        },
        {
            "key": "autotag_llm_model",
            "label": "Auto-tag LLM model",
            "group": "Tags",
            "type": "string",
            "default": "llama3.2",
        },
        {
            "key": "download_path_default",
            "label": "Default Download Path",
            "group": "Downloads",
            "type": "string",
            "default": "",
        },
        {
            "key": "path_aliases",
            "label": "Path Aliases",
            "group": "Downloads",
            "type": "json",
            "default": {},
        },
        {
            "key": "plugin_source",
            "label": "Plugin source repository",
            "group": "Plugins",
            "type": "string",
            "default": "",
        },
        {
            "key": "plugin_source_branch",
            "label": "Plugin source branch",
            "group": "Plugins",
            "type": "string",
            "default": "main",
        },
        {
            "key": "search_plugins",
            "label": "Default search plugins",
            "group": "Plugins",
            "type": "string",
            "default": "",
            "placeholder": "hydrusnetwork, alldebrid",
        },
    ]


def clear_config_cache() -> None:
    """Clear the configuration cache and baseline snapshot."""
    global _CONFIG_CACHE, _LAST_SAVED_CONFIG, _CONFIG_SUMMARY_PENDING
    _CONFIG_CACHE = {}
    _LAST_SAVED_CONFIG = {}
    _CONFIG_SUMMARY_PENDING = False


def _log_config_load_summary(config: Dict[str, Any]) -> None:
    try:
        plugin_block = config.get("plugin")
        if isinstance(plugin_block, dict):
            # Count distinct plugin names; note multi-instance plugins appear once per name
            plugin_names = list(plugin_block.keys())
            # Count total configured instances across all plugins
            total_instances = sum(
                len(v) if isinstance(v, dict) and all(isinstance(x, dict) for x in v.values()) else 1
                for v in plugin_block.values()
                if isinstance(v, dict)
            )
        else:
            plugin_names, total_instances = [], 0
        mtime = None
        try:
            mtime = datetime.datetime.fromtimestamp(db.db_path.stat().st_mtime, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
        except Exception:
            mtime = None
        plugins_str = ', '.join(plugin_names[:10]) + ('...' if len(plugin_names) > 10 else '')
        summary = (
            f"Loaded config from {db.db_path.name}: "
            f"plugins={len(plugin_names)} ({plugins_str}), "
            f"instances={total_instances}, mtime={mtime}"
        )
        log(summary)
    except Exception:
        logger.exception("Failed to build config load summary from %s", db.db_path)


def get_nested_config_value(config: Dict[str, Any], *path: str) -> Any:
    cur: Any = config
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _normalize_path_alias_name(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None

    match = _PATH_ALIAS_TOKEN_RE.match(raw)
    if match:
        raw = str(match.group("braced") or match.group("plain") or "").strip()

    candidate = raw.strip().strip("()")
    if not candidate:
        return None
    return candidate.lower()


def get_path_aliases(config: Dict[str, Any]) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    if not isinstance(config, dict):
        return aliases

    for block_name in ("path_aliases", "download_paths"):
        block = config.get(block_name)
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            alias = _normalize_path_alias_name(key)
            if not alias:
                continue
            if isinstance(value, str) and value.strip():
                aliases[alias] = value.strip()

    return aliases


def resolve_path_alias(config: Dict[str, Any], value: Any) -> Optional[Path]:
    raw = str(value or "").strip()
    if not raw.startswith("$"):
        return None

    alias = _normalize_path_alias_name(raw)
    if not alias:
        return None

    target = get_path_aliases(config).get(alias)
    if not target:
        return None

    return expand_path(target)


def coerce_config_value(
    value: Any,
    existing_value: Any = _CONFIG_MISSING,
    *,
    on_error: Optional[Callable[[str], None]] = None,
) -> Any:
    if not isinstance(value, str):
        return value

    text = value.strip()
    lowered = text.lower()

    if existing_value is _CONFIG_MISSING:
        if lowered in {"true", "false"}:
            return lowered == "true"
        if text.isdigit():
            return int(text)
        return value

    if isinstance(existing_value, bool):
        if lowered in {"true", "yes", "1", "on"}:
            return True
        if lowered in {"false", "no", "0", "off"}:
            return False
        if on_error is not None:
            on_error(f"Warning: Could not convert '{value}' to boolean. Using string.")
        return value

    if isinstance(existing_value, int) and not isinstance(existing_value, bool):
        try:
            return int(text)
        except ValueError:
            if on_error is not None:
                on_error(f"Warning: Could not convert '{value}' to int. Using string.")
            return value

    if isinstance(existing_value, float):
        try:
            return float(text)
        except ValueError:
            if on_error is not None:
                on_error(f"Warning: Could not convert '{value}' to float. Using string.")
            return value

    return value


def set_nested_config_value(
    config: Dict[str, Any],
    key_path: str | Sequence[str],
    value: Any,
    *,
    on_error: Optional[Callable[[str], None]] = None,
) -> bool:
    if not isinstance(config, dict):
        return False

    if isinstance(key_path, str):
        keys = [part for part in key_path.split(".") if part]
    else:
        keys = [str(part) for part in (key_path or []) if str(part)]

    if not keys:
        return False

    def _resolve_child(mapping: Dict[str, Any], key: str) -> tuple[str, Any, bool]:
        if key in mapping:
            return key, mapping.get(key), True
        target = str(key or "").strip().lower()
        for raw_key, raw_value in mapping.items():
            if str(raw_key or "").strip().lower() == target:
                return str(raw_key), raw_value, True
        return key, None, False

    current = config
    for key in keys[:-1]:
        actual_key, next_value, found = _resolve_child(current, key)
        if not found or not isinstance(next_value, dict):
            next_value = {}
            current[key] = next_value
            actual_key = key
        current = next_value

    last_key = keys[-1]
    actual_last, existing_value, found_last = _resolve_child(current, last_key)
    if not found_last:
        actual_last = last_key
        existing_value = _CONFIG_MISSING
    current[actual_last] = coerce_config_value(value, existing_value, on_error=on_error)
    return True


def get_plugin_instance(
    config: Dict[str, Any],
    plugin_name: str,
    instance_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return one instance dict from plugin.<name>.<instance>."""
    block = get_plugin_block(config, plugin_name)
    if not isinstance(block, dict) or not block:
        return None
    if instance_name:
        direct = block.get(instance_name)
        if isinstance(direct, dict):
            return direct
        target = str(instance_name).strip().lower()
        for name, conf in block.items():
            if isinstance(conf, dict) and str(name).strip().lower() == target:
                return conf
    nested = [conf for conf in block.values() if isinstance(conf, dict)]
    if nested and any(isinstance(v, dict) for v in block.values()):
        keys = sorted(str(k) for k in block.keys() if isinstance(block.get(k), dict))
        for key in keys:
            if not key.startswith("new_"):
                candidate = block.get(key)
                if isinstance(candidate, dict):
                    return candidate
        if keys:
            candidate = block.get(keys[0])
            return candidate if isinstance(candidate, dict) else None
    return block


def get_plugin_instance_value(
    config: Dict[str, Any],
    plugin_name: str,
    key: str,
    instance_name: Optional[str] = None,
) -> Optional[str]:
    instance = get_plugin_instance(config, plugin_name, instance_name)
    if not isinstance(instance, dict):
        return None
    want = str(key or "").strip().lower()
    for raw_key, raw_value in instance.items():
        if str(raw_key or "").strip().lower() != want:
            continue
        if raw_value in (None, ""):
            return None
        return str(raw_value).strip()
    return None


_PLUGIN_CONFIG_ALIASES = {
    "file.io": "fileio",
    "archive.org": "archiveorg",
    "openlibrary": "archiveorg",
    "internetarchive": "archiveorg",
    "ia": "archiveorg",
}


def get_plugin_block(config: Dict[str, Any], name: str) -> Dict[str, Any]:
    _canonicalize_plugin_config(config)
    plugin_cfg = config.get("plugin")
    if not isinstance(plugin_cfg, dict):
        return {}
    normalized = _normalize_provider_name(name)
    names = [normalized] if normalized else []
    alias_target = _PLUGIN_CONFIG_ALIASES.get(normalized or "")
    if alias_target and alias_target not in names:
        names.append(alias_target)
    for candidate in names:
        block = plugin_cfg.get(candidate)
        if isinstance(block, dict):
            return block
    for key, block in plugin_cfg.items():
        if not isinstance(block, dict):
            continue
        if _normalize_provider_name(key) in names:
            return block
    return {}


def get_soulseek_username(config: Dict[str, Any]) -> Optional[str]:
    block = get_plugin_block(config, "soulseek")
    val = block.get("username") or block.get("USERNAME")
    return str(val).strip() if val else None


def get_soulseek_password(config: Dict[str, Any]) -> Optional[str]:
    block = get_plugin_block(config, "soulseek")
    val = block.get("password") or block.get("PASSWORD")
    return str(val).strip() if val else None


def resolve_output_dir(config: Dict[str, Any]) -> Path:
    """Resolve output directory from config with single source of truth.

    Priority:
    1. config["download_path_default"] - default download/output directory
    2. config["temp"] - explicitly set temp/output directory
    3. config["outfile"] - fallback to outfile setting
    4. System Temp - default fallback directory

    Returns:
        Path to output directory
    """
    default_output = config.get("download_path_default")
    if default_output:
        try:
            aliased = resolve_path_alias(config, default_output)
            path = aliased if aliased is not None else expand_path(default_output)
            if path.exists() or path.parent.exists():
                return safe_output_dir(path)
        except Exception as exc:
            logger.debug("resolve_output_dir: failed to expand download_path_default value %r: %s", default_output, exc, exc_info=True)

    # First try explicit temp setting from config
    temp_value = config.get("temp")
    if temp_value:
        try:
            path = expand_path(temp_value)
            # Verify we can access it (not a system directory with permission issues)
            if path.exists() or path.parent.exists():
                return safe_output_dir(path)
        except Exception as exc:
            logger.debug("resolve_output_dir: failed to expand temp value %r: %s", temp_value, exc, exc_info=True)

    # Then try outfile setting
    outfile_value = config.get("outfile")
    if outfile_value:
        try:
            return safe_output_dir(expand_path(outfile_value))
        except Exception as exc:
            logger.debug("resolve_output_dir: failed to expand outfile value %r: %s", outfile_value, exc, exc_info=True)

    # Fallback to system temp directory
    return safe_output_dir(Path(tempfile.gettempdir()))


def get_local_storage_path(config: Dict[str, Any]) -> Optional[Path]:
    """Return the configured default local plugin destination path.

    This helper is intentionally narrow: it reports a real local library/export
    root only when the canonical `plugin.local` config defines one. Callers that
    want a staging/output directory should use `resolve_output_dir(...)` instead.
    """
    local_block = get_plugin_block(config, "local")
    if not isinstance(local_block, dict) or not local_block:
        return None

    if _is_multi_instance_plugin_config(local_block):
        if "default" in local_block and isinstance(local_block.get("default"), dict):
            local_config = local_block.get("default")
        else:
            local_config = next(
                (value for value in local_block.values() if isinstance(value, dict)),
                None,
            )
    else:
        local_config = local_block

    if not isinstance(local_config, dict):
        return None

    path_str = local_config.get("path") or local_config.get("PATH")
    if path_str:
        return expand_path(path_str)

    return None


def get_debrid_api_key(config: Dict[str, Any], service: str = "All-debrid") -> Optional[str]:
    """Get Debrid API key from config.

    Checks the plugin block first (canonical format).

    Args:
        config: Configuration dict
        service: Service name (default: "All-debrid")

    Returns:
        API key string if found, None otherwise
    """
    _canonicalize_plugin_config(config)

    # 1) Canonical plugin block: config["plugin"]["alldebrid"]["api_key"]
    plugin_block = config.get("plugin")
    if isinstance(plugin_block, dict):
        alldebrid_entry = plugin_block.get("alldebrid")
        if isinstance(alldebrid_entry, dict):
            for k in ("api_key", "API_KEY", "apikey", "APIKEY"):
                val = alldebrid_entry.get(k)
                if isinstance(val, str) and val.strip():
                    return val.strip()

    return None


def get_plugin_credentials(config: Dict[str, Any], provider: str) -> Optional[Dict[str, str]]:
    """Get plugin credentials (email/password) from config.

    Args:
        config: Configuration dict
        provider: Provider name (e.g., "openlibrary", "soulseek")

    Returns:
        Dict with credentials if found, None otherwise
    """
    _canonicalize_plugin_config(config)

    plugin_config = config.get("plugin", {})
    if isinstance(plugin_config, dict):
        creds = plugin_config.get(provider.lower(), {})
        if isinstance(creds, dict) and creds:
            return creds

    return None


def resolve_cookies_path(
    config: Dict[str, Any], script_dir: Optional[Path] = None
) -> Optional[Path]:
    """Resolve yt-dlp cookies from ``plugins/ytdlp/`` only.

    Drop Netscape cookie files into the plugin folder (prefer ``cookies.txt``).
    Config path overrides are intentionally not supported.
    """
    base_dir = _resolve_app_root(script_dir)
    plugin_file = _resolve_ytdlp_plugin_cookie_path(base_dir)
    if plugin_file is not None:
        return plugin_file
    if isinstance(config, dict):
        plugin_block = config.get("plugin")
        ytdlp_block = plugin_block.get("ytdlp") if isinstance(plugin_block, dict) else None
        if isinstance(ytdlp_block, dict):
            raw = str(ytdlp_block.get("cookie_file") or ytdlp_block.get("cookies") or "").strip()
            if raw:
                try:
                    candidate = Path(raw).expanduser()
                    if candidate.is_file():
                        return candidate
                except Exception:
                    pass
    return None


def _resolve_ytdlp_plugin_cookie_path(base_dir: Path) -> Optional[Path]:
    plugin_dir = _resolve_app_root(base_dir) / "plugins" / "ytdlp"
    preferred = resolve_plugin_asset_path("ytdlp", "cookies.txt", script_dir=base_dir)
    if preferred is not None and preferred.is_file():
        return preferred

    if plugin_dir.is_dir():
        preferred_in_dir = plugin_dir / "cookies.txt"
        if preferred_in_dir.is_file():
            return preferred_in_dir

        # Accept other cookie dumps dropped into the plugin folder.
        candidates: List[Path] = []
        try:
            for path in plugin_dir.iterdir():
                if not path.is_file():
                    continue
                name = path.name.lower()
                if name.startswith("."):
                    continue
                if "cookie" in name and path.suffix.lower() in {".txt", ".cookies", ""}:
                    candidates.append(path)
                elif name.endswith(".cookies.txt"):
                    candidates.append(path)
        except Exception:
            candidates = []
        if candidates:
            candidates.sort(key=lambda p: p.name.lower())
            return candidates[0]

    # One-time migration from legacy repo-root cookies.txt into the plugin folder.
    legacy_cookie = _resolve_app_root(base_dir) / "cookies.txt"
    plugin_cookie = plugin_dir / "cookies.txt"
    try:
        if legacy_cookie.is_file() and not plugin_cookie.exists():
            plugin_cookie.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_cookie, plugin_cookie)
            return plugin_cookie
    except Exception:
        logger.debug("Failed migrating legacy cookies.txt into plugins/ytdlp", exc_info=True)

    return None


def resolve_debug_log(config: Dict[str, Any]) -> Optional[Path]:
    value = config.get("download_debug_log")
    if not value:
        return None
    path = expand_path(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path

def _normalize_provider_name(value: Any) -> Optional[str]:
    candidate = str(value or "").strip().lower()
    return candidate if candidate else None


def _resolve_app_root(script_dir: Optional[Path] = None) -> Path:
    if script_dir is not None:
        try:
            candidate = expand_path(script_dir)
        except Exception:
            candidate = Path(script_dir)
        return candidate if candidate.is_dir() else candidate.parent
    return SCRIPT_DIR.parent


def resolve_plugin_asset_path(
    plugin_name: str,
    *relative_parts: str,
    script_dir: Optional[Path] = None,
) -> Optional[Path]:
    normalized = _normalize_provider_name(plugin_name)
    if not normalized:
        return None

    plugin_dir = _resolve_app_root(script_dir) / "plugins" / normalized
    if not plugin_dir.is_dir():
        return None

    for part in relative_parts:
        text = str(part or "").strip().strip("/\\")
        if not text:
            continue
        candidate = plugin_dir / text
        if candidate.is_file():
            return candidate
    return None


def _multi_instance_plugin_names() -> set[str]:
    """Return canonical multi-instance plugin names (best-effort)."""
    try:
        from SYS.plugin_config import get_configurable_store_types

        return {
            str(name).strip().lower()
            for name in (get_configurable_store_types() or [])
            if str(name).strip()
        }
    except Exception:
        return set()


def _plugin_schema_field_keys(plugin_name: str) -> set[str]:
    try:
        from SYS.plugin_config import get_plugin_schema

        schema = get_plugin_schema(plugin_name) or []
    except Exception:
        schema = []
    keys = {
        str(field.get("key") or "").strip().lower()
        for field in schema
        if str(field.get("key") or "").strip()
    }
    keys.add("name")
    return keys


def _dict_looks_like_instance_settings(value: Any, schema_keys: set[str]) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    if all(isinstance(v, dict) for v in value.values()):
        return False
    # Bags that still contain nested instance-shaped children are containers, not
    # a single instance payload.
    if any(isinstance(v, dict) for v in value.values()):
        nested_instance_like = any(
            isinstance(v, dict)
            and (
                (schema_keys and {str(k or "").strip().lower() for k in v.keys()} & schema_keys)
                or not any(isinstance(inner, dict) for inner in v.values())
            )
            for v in value.values()
        )
        if nested_instance_like:
            return False
    entry_keys = {str(k or "").strip().lower() for k in value.keys() if str(k or "").strip()}
    if schema_keys and entry_keys.intersection(schema_keys):
        return True
    return not any(isinstance(v, dict) for v in value.values())


def _instance_settings_have_content(settings: Dict[str, Any], schema_keys: set[str]) -> bool:
    for key, value in settings.items():
        key_l = str(key or "").strip().lower()
        if key_l == "name":
            continue
        if isinstance(value, str):
            if value.strip():
                return True
            continue
        if value is None:
            continue
        if isinstance(value, (list, tuple, set, dict)):
            if value:
                return True
            continue
        if isinstance(value, bool):
            return True
        if value != 0 and value != "":
            return True
    return False


def normalize_multi_instance_plugin_block(
    plugin_name: str,
    branch: Any,
    *,
    schema_keys: Optional[set[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Normalize a plugin config branch into ``{instance_name: settings}``.

    Repairs hybrid/legacy shapes that mixed flat fields with nested instances::

        {"URL": "...", "API": "...", "Typhon": {"URL": "...", "API": "..."}}
        {"default": {"URL": "...", "Typhon": {...}}}
    """
    if not isinstance(branch, dict) or not branch:
        return {}

    keys = schema_keys if schema_keys is not None else _plugin_schema_field_keys(plugin_name)
    instances: Dict[str, Dict[str, Any]] = {}
    orphan_fields: Dict[str, Any] = {}

    def _add_instance(raw_name: Any, settings: Dict[str, Any]) -> None:
        name = str(raw_name or "").strip()
        if not name:
            return

        nested_instances = {
            str(k): dict(v)
            for k, v in settings.items()
            if isinstance(v, dict) and _dict_looks_like_instance_settings(v, keys)
        }
        cleaned = {
            str(k): v
            for k, v in settings.items()
            if not (isinstance(v, dict) and _dict_looks_like_instance_settings(v, keys))
        }
        for nested_name, nested_settings in nested_instances.items():
            _add_instance(nested_name, nested_settings)

        # Multi-instance plugins never keep a synthetic/legacy "default" bag once
        # real named instances exist (or when nested instances were lifted out).
        if name.lower() == "default":
            if nested_instances:
                if _instance_settings_have_content(cleaned, keys) and not instances:
                    # Residual flat fields only — promote via NAME if present.
                    promoted = None
                    for name_key in ("NAME", "Name", "name"):
                        candidate = cleaned.get(name_key)
                        if isinstance(candidate, str) and candidate.strip():
                            promoted = candidate.strip()
                            break
                    if promoted:
                        _add_instance(promoted, cleaned)
                return
            if not _instance_settings_have_content(cleaned, keys):
                return
            # Pure legacy flat default with content: promote via NAME, else drop.
            promoted = None
            for name_key in ("NAME", "Name", "name"):
                candidate = cleaned.get(name_key)
                if isinstance(candidate, str) and candidate.strip() and candidate.strip().lower() != "default":
                    promoted = candidate.strip()
                    break
            if not promoted:
                return
            name = promoted
            # fall through and store under promoted name

        resolved_name = name
        existing = None
        for existing_name, existing_settings in instances.items():
            if existing_name.lower() == name.lower():
                resolved_name = existing_name
                existing = existing_settings
                break

        if existing is None:
            instances[resolved_name] = dict(cleaned)
        else:
            merged = dict(existing)
            merged.update(cleaned)
            instances[resolved_name] = merged

        if any(str(k).lower() == "name" for k in instances[resolved_name].keys()) or "name" in keys:
            name_key = next(
                (k for k in instances[resolved_name].keys() if str(k).lower() == "name"),
                "NAME",
            )
            if not str(instances[resolved_name].get(name_key) or "").strip():
                instances[resolved_name][name_key] = resolved_name

    for raw_key, raw_value in list(branch.items()):
        key = str(raw_key or "").strip()
        if not key:
            continue

        if isinstance(raw_value, dict):
            if raw_value and all(isinstance(v, dict) for v in raw_value.values()):
                for nested_name, nested_val in raw_value.items():
                    if isinstance(nested_val, dict):
                        _add_instance(nested_name, dict(nested_val))
                continue

            # Container bags (default/hybrid) and normal instance payloads.
            if key.lower() == "default" or not _dict_looks_like_instance_settings(raw_value, keys):
                # Still attempt lift via _add_instance which extracts nested instances.
                if key.lower() == "default" or any(isinstance(v, dict) for v in raw_value.values()):
                    _add_instance(key, dict(raw_value))
                    continue

            if _dict_looks_like_instance_settings(raw_value, keys):
                _add_instance(key, dict(raw_value))
                continue

            _add_instance(key, dict(raw_value))
            continue

        orphan_fields[key] = raw_value

    if orphan_fields:
        promoted_name = None
        for name_key in ("NAME", "Name", "name"):
            candidate = orphan_fields.get(name_key)
            if isinstance(candidate, str) and candidate.strip():
                promoted_name = candidate.strip()
                break
        if len(instances) >= 1:
            # Merge residual flat fields into the sole/first instance when empty there.
            target_name = next(iter(instances.keys()))
            if promoted_name:
                for existing_name in instances.keys():
                    if existing_name.lower() == promoted_name.lower():
                        target_name = existing_name
                        break
            merged = dict(instances[target_name])
            for k, v in orphan_fields.items():
                if str(k).lower() == "name" and not str(v or "").strip():
                    continue
                if k not in merged or not str(merged.get(k) or "").strip():
                    merged[k] = v
            instances[target_name] = merged
        elif _instance_settings_have_content(orphan_fields, keys) and promoted_name:
            _add_instance(promoted_name, dict(orphan_fields))
        # else: ignore unnamed flat leftovers for multi-instance plugins

    for name in list(instances.keys()):
        if name.lower() == "default":
            instances.pop(name, None)

    return instances


def _merge_plugin_branch_values(existing: Any, incoming: Any) -> Any:
    """Merge two plugin config branches; existing values win on conflict."""
    if not isinstance(existing, dict):
        return incoming if isinstance(incoming, dict) else existing
    if not isinstance(incoming, dict):
        return existing

    merged = dict(existing)
    for key, value in incoming.items():
        if key not in merged:
            merged[key] = value
            continue
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            nested = dict(current)
            for nested_key, nested_value in value.items():
                if nested_key not in nested:
                    nested[nested_key] = nested_value
            merged[key] = nested
    return merged


def _canonicalize_plugin_config(config: Dict[str, Any]) -> None:
    if not isinstance(config, dict):
        return

    config.pop("provider", None)
    config.pop("store", None)

    # Legacy "tool" namespace is retired — fold into plugin and drop.
    legacy_tool = config.pop("tool", None)
    plugin_block = config.get("plugin")
    if not isinstance(plugin_block, dict):
        plugin_block = {}
    if isinstance(legacy_tool, dict):
        for key, value in legacy_tool.items():
            normalized_key = _normalize_provider_name(key)
            if not normalized_key:
                continue
            if normalized_key not in plugin_block:
                plugin_block[normalized_key] = value
            else:
                plugin_block[normalized_key] = _merge_plugin_branch_values(
                    plugin_block.get(normalized_key),
                    value,
                )

    normalized_plugin: Dict[str, Any] = {}
    multi_names = _multi_instance_plugin_names()

    if isinstance(plugin_block, dict):
        for key, value in plugin_block.items():
            normalized_key = _normalize_provider_name(key)
            if not normalized_key:
                continue
            if not isinstance(value, dict):
                normalized_plugin[normalized_key] = value
                continue

            force_multi = normalized_key in multi_names
            if not force_multi:
                try:
                    from SYS.plugin_config import get_plugin_class

                    plugin_cls = get_plugin_class(normalized_key)
                    force_multi = bool(plugin_cls and getattr(plugin_cls, "MULTI_INSTANCE", False))
                except Exception:
                    force_multi = False
            # Hybrid flat+nested payloads must be repaired even if discovery failed.
            if not force_multi and value:
                has_nested = any(isinstance(v, dict) for v in value.values())
                has_scalar = any(not isinstance(v, dict) for v in value.values())
                force_multi = has_nested and has_scalar

            if force_multi:
                normalized_plugin[normalized_key] = normalize_multi_instance_plugin_block(
                    normalized_key,
                    value,
                )
            else:
                normalized_plugin[normalized_key] = value

    for alias, canonical in (
        ("file.io", "fileio"),
        ("archive.org", "archiveorg"),
        ("openlibrary", "archiveorg"),
        ("internetarchive", "archiveorg"),
        ("ia", "archiveorg"),
    ):
        if alias not in normalized_plugin:
            continue
        incoming = normalized_plugin.pop(alias)
        existing = normalized_plugin.get(canonical)
        if isinstance(existing, dict) or isinstance(incoming, dict):
            normalized_plugin[canonical] = _merge_plugin_branch_values(existing, incoming)
        elif canonical not in normalized_plugin:
            normalized_plugin[canonical] = incoming

    # yt-dlp cookies are file-based under plugins/ytdlp/; drop legacy path keys.
    ytdlp_cfg = normalized_plugin.get("ytdlp")
    if isinstance(ytdlp_cfg, dict):
        for retired_key in ("cookies", "cookiefile", "cookie_file", "cookie_path"):
            ytdlp_cfg.pop(retired_key, None)

    if normalized_plugin:
        config["plugin"] = normalized_plugin
    else:
        config.pop("plugin", None)


def _extract_api_key(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("api_key", "API_KEY", "apikey", "APIKEY"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    elif isinstance(value, str):
        trimmed = value.strip()
        if trimmed:
            return trimmed
    return None


def _sync_alldebrid_api_key(config: Dict[str, Any]) -> None:
    """Ensure AllDebrid API key is consistently stored in config[\"plugin\"][\"alldebrid\"].

    Previously this function also synced to config[\"store\"][\"debrid\"].  That path
    is no longer used; only the plugin namespace is written.
    """
    if not isinstance(config, dict):
        return

    _canonicalize_plugin_config(config)

    plugins = config.get("plugin")
    if not isinstance(plugins, dict):
        plugins = {}
        config["plugin"] = plugins

    plugin_entry = plugins.get("alldebrid")
    plugin_section: Dict[str, Any] | None = None
    plugin_key = None
    if isinstance(plugin_entry, dict):
        plugin_section = plugin_entry
        plugin_key = _extract_api_key(plugin_section)
    elif isinstance(plugin_entry, str):
        plugin_key = plugin_entry.strip()
        if plugin_key:
            plugin_section = {"api_key": plugin_key}
            plugins["alldebrid"] = plugin_section

def _is_multi_instance_plugin_config(value: Any) -> bool:
    """Return True if `value` looks like a multi-instance plugin config (dict-of-dicts).

    Multi-instance plugins store their configuration as::

        {<instance_name>: {key: value, ...}, ...}

    Single-instance plugins store their config as a flat dict::

        {key: value, ...}

    We detect multi-instance by checking whether ALL values are themselves dicts
    (and the outer dict is non-empty).  An empty dict is treated as single-instance.
    """
    if not isinstance(value, dict) or not value:
        return False
    return all(isinstance(v, dict) for v in value.values())


def _flatten_config_entries(config: Dict[str, Any]) -> Dict[Tuple[str, str, str, str], Any]:
    entries: Dict[Tuple[str, str, str, str], Any] = {}
    _canonicalize_plugin_config(config)
    multi_names = _multi_instance_plugin_names()
    for key, value in config.items():
        if key == 'plugin' and isinstance(value, dict):
            for subtype, plugin_cfg in value.items():
                if not isinstance(plugin_cfg, dict):
                    continue
                subtype_l = str(subtype or "").strip().lower()
                effective_cfg = plugin_cfg
                if subtype_l in multi_names:
                    effective_cfg = normalize_multi_instance_plugin_block(subtype_l, plugin_cfg)
                if subtype_l in multi_names or _is_multi_instance_plugin_config(effective_cfg):
                    # Multi-instance: {instance_name: {key: val}}
                    for instance_name, settings in effective_cfg.items():
                        if not isinstance(settings, dict):
                            continue
                        for k, v in settings.items():
                            if isinstance(v, dict):
                                continue
                            entries[('plugin', subtype, instance_name, k)] = v
                else:
                    # Single-instance: {key: val}
                    for k, v in plugin_cfg.items():
                        if isinstance(v, dict):
                            continue
                        entries[('plugin', subtype, 'default', k)] = v
        elif not key.startswith('_') and value is not None:
            # Skip retired namespaces that may still appear before canonicalize.
            if key in {"tool", "provider", "store"}:
                continue
            entries[('global', 'none', 'none', key)] = value
    return entries


def _count_changed_entries(old_config: Dict[str, Any], new_config: Dict[str, Any]) -> int:
    old_entries = _flatten_config_entries(old_config or {})
    new_entries = _flatten_config_entries(new_config or {})
    changed = {k for k, v in new_entries.items() if old_entries.get(k) != v}
    removed = {k for k in old_entries if k not in new_entries}
    return len(changed) + len(removed)


def _changed_entry_keys(old_config: Dict[str, Any], new_config: Dict[str, Any]) -> set[Tuple[str, str, str, str]]:
    old_entries = _flatten_config_entries(old_config or {})
    new_entries = _flatten_config_entries(new_config or {})
    keys = set(old_entries) | set(new_entries)
    return {key for key in keys if old_entries.get(key, _CONFIG_MISSING) != new_entries.get(key, _CONFIG_MISSING)}


def _config_from_flattened_entries(
    entries: Dict[Tuple[str, str, str, str], Any],
) -> Dict[str, Any]:
    config: Dict[str, Any] = {}
    for (category, subtype, item_name, key), value in entries.items():
        if category == "global":
            config[key] = value
            continue

        if category == "plugin":
            plugin_block = config.setdefault("plugin", {})
            subtype_block = plugin_block.setdefault(subtype, {})
            if item_name == "default":
                subtype_block[key] = value
            else:
                item_block = subtype_block.setdefault(item_name, {})
                item_block[key] = value
            continue

        if category == "tool":
            # Retired namespace: fold into plugin during rebuild.
            plugin_block = config.setdefault("plugin", {})
            subtype_block = plugin_block.setdefault(subtype, {})
            if item_name == "default":
                subtype_block[key] = value
            else:
                item_block = subtype_block.setdefault(item_name, {})
                item_block[key] = value
            continue

        if category in {"provider", "store"}:
            continue

        category_block = config.setdefault(category, {})
        if isinstance(category_block, dict):
            subtype_block = category_block.setdefault(subtype, {})
            if isinstance(subtype_block, dict):
                item_block = subtype_block.setdefault(item_name, {})
                if isinstance(item_block, dict):
                    item_block[key] = value

    _canonicalize_plugin_config(config)
    _sync_alldebrid_api_key(config)
    return config


def _merge_non_conflicting_config_changes(
    base_config: Dict[str, Any],
    disk_config: Dict[str, Any],
    local_config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    local_changed = _changed_entry_keys(base_config, local_config)
    if not local_changed:
        return deepcopy(disk_config)

    disk_changed = _changed_entry_keys(base_config, disk_config)
    if local_changed & disk_changed:
        return None

    merged_entries = dict(_flatten_config_entries(disk_config or {}))
    local_entries = _flatten_config_entries(local_config or {})
    for key in local_changed:
        if key in local_entries:
            merged_entries[key] = local_entries[key]
        else:
            merged_entries.pop(key, None)
    return _config_from_flattened_entries(merged_entries)


def _extract_expected_alldebrid_key(config: Dict[str, Any]) -> Optional[str]:
    expected_key = None
    try:
        plugins = config.get("plugin", {}) if isinstance(config, dict) else {}
        if isinstance(plugins, dict):
            entry = plugins.get("alldebrid")
            if entry is not None:
                if isinstance(entry, dict):
                    for k in ("api_key", "API_KEY", "apikey", "APIKEY"):
                        v = entry.get(k)
                        if isinstance(v, str) and v.strip():
                            expected_key = v.strip()
                            break
                elif isinstance(entry, str) and entry.strip():
                    expected_key = entry.strip()
        if not expected_key:
            expected_key = get_debrid_api_key(config, service="All-debrid")
    except Exception as exc:
        logger.debug("Failed to determine expected AllDebrid key: %s", exc, exc_info=True)
        expected_key = None
    return expected_key


def load_config(*, emit_summary: bool = False) -> Dict[str, Any]:
    global _CONFIG_CACHE, _LAST_SAVED_CONFIG, _CONFIG_SUMMARY_PENDING
    if _CONFIG_CACHE:
        if emit_summary and _CONFIG_SUMMARY_PENDING:
            _log_config_load_summary(_CONFIG_CACHE)
            _CONFIG_SUMMARY_PENDING = False
        return _CONFIG_CACHE

    # Load strictly from database
    db_config = get_config_all()
    if db_config:
        _canonicalize_plugin_config(db_config)
        _sync_alldebrid_api_key(db_config)
        _CONFIG_CACHE = db_config
        _LAST_SAVED_CONFIG = deepcopy(db_config)
        if emit_summary:
            _log_config_load_summary(db_config)
            _CONFIG_SUMMARY_PENDING = False
        else:
            _CONFIG_SUMMARY_PENDING = True

        # Forensics disabled: audit/mismatch/backup detection removed to simplify code.
        return db_config

    _LAST_SAVED_CONFIG = {}
    return {}


def reload_config() -> Dict[str, Any]:
    clear_config_cache()
    return load_config()


def _acquire_save_lock(timeout: float = _SAVE_LOCK_TIMEOUT):
    """Acquire a cross-process save lock implemented as a directory.

    Returns the Path to the created lock directory. Raises ConfigSaveConflict
    if the lock cannot be acquired within the timeout.
    """
    lock_dir = Path(db.db_path).with_name(_SAVE_LOCK_DIRNAME)
    start = time.time()
    while True:
        try:
            lock_dir.mkdir(exist_ok=False)
            # Write owner metadata for diagnostics
            try:
                (lock_dir / "owner.json").write_text(json.dumps({
                    "pid": os.getpid(),
                    "ts": time.time(),
                    "cmdline": " ".join(sys.argv),
                }))
            except Exception as exc:
                logger.exception("Failed to write save lock owner metadata %s: %s", lock_dir, exc)
            return lock_dir
        except FileExistsError:
            # Check for stale lock
            try:
                owner = lock_dir / "owner.json"
                if owner.exists():
                    data = json.loads(owner.read_text())
                    ts = data.get("ts") or 0
                    if time.time() - ts > _SAVE_LOCK_STALE_SECONDS:
                        try:
                            import shutil
                            shutil.rmtree(lock_dir)
                            continue
                        except Exception as exc:
                            logger.exception("Failed to remove stale save lock dir %s", lock_dir)
                else:
                    # No owner file; if directory is old enough consider it stale
                    try:
                        if time.time() - lock_dir.stat().st_mtime > _SAVE_LOCK_STALE_SECONDS:
                            import shutil
                            shutil.rmtree(lock_dir)
                            continue
                    except Exception as exc:
                        logger.exception("Failed to stat/remove stale save lock dir %s", lock_dir)
            except Exception as exc:
                logger.exception("Failed to inspect save lock directory %s: %s", lock_dir, exc)
            if time.time() - start > timeout:
                raise ConfigSaveConflict("Save lock busy; could not acquire in time")
            time.sleep(_SAVE_LOCK_POLL_INTERVAL)


def _release_save_lock(lock_dir: Path) -> None:
    try:
        owner = lock_dir / "owner.json"
        try:
            if owner.exists():
                owner.unlink()
        except Exception:
            logger.exception("Failed to remove save lock owner file %s", owner)
        lock_dir.rmdir()
    except Exception:
        logger.exception("Failed to release save lock directory %s", lock_dir)


def save_config(config: Dict[str, Any]) -> int:
    global _CONFIG_CACHE, _LAST_SAVED_CONFIG
    _canonicalize_plugin_config(config)
    _sync_alldebrid_api_key(config)

    # Acquire cross-process save lock to avoid concurrent saves from different
    # processes which can lead to race conditions and DB-level overwrite.
    lock_dir = None
    try:
        lock_dir = _acquire_save_lock()
    except ConfigSaveConflict:
        # Surface a clear exception to callers so they can retry or handle it.
        raise

    previous_config = deepcopy(_LAST_SAVED_CONFIG)
    changed_count = _count_changed_entries(previous_config, config)

    def _prepare_disk_snapshot(raw_disk: Any) -> Dict[str, Any]:
        disk_config = raw_disk if isinstance(raw_disk, dict) else {}
        _canonicalize_plugin_config(disk_config)
        _sync_alldebrid_api_key(disk_config)
        return disk_config

    def _write_entries() -> int:
        global _CONFIG_CACHE, _LAST_SAVED_CONFIG
        count = 0
        config_to_write = config
        # Use the transaction-provided connection directly to avoid re-acquiring
        # the connection lock via db.* helpers which can lead to deadlock.
        with db.transaction() as conn:
            # Detect concurrent changes by reading the current DB state inside the
            # same transaction before mutating it. Use the transaction connection
            # directly to avoid acquiring the connection lock again (deadlock).
            try:
                cur = conn.cursor()
                cur.execute("SELECT category, subtype, item_name, key, value FROM config")
                rows = cur.fetchall()
                current_disk = _prepare_disk_snapshot(rows_to_config(rows))
                cur.close()
            except Exception:
                current_disk = {}

            # Compare logical entries, not raw nested dict identity/shape. Save writes
            # encrypt/normalize values, so a naive dict compare false-conflicts after
            # create-then-edit flows.
            disk_drifted = bool(_changed_entry_keys(_LAST_SAVED_CONFIG or {}, current_disk))
            if disk_drifted:
                # If we have no local changes, refresh caches and skip the write.
                if changed_count == 0:
                    log("Skip save: disk configuration changed since last load and no local changes; not writing to DB.")
                    # Refresh local caches to match the disk
                    _CONFIG_CACHE = current_disk
                    _LAST_SAVED_CONFIG = deepcopy(current_disk)
                    return 0
                merged_config = _merge_non_conflicting_config_changes(
                    previous_config,
                    current_disk,
                    config,
                )
                if merged_config is None:
                    # Otherwise, abort to avoid overwriting external changes
                    raise ConfigSaveConflict(
                        "Configuration on disk changed since you started editing; save aborted to prevent overwrite. Reload and reapply your changes."
                    )
                config_to_write = merged_config
                log("Config save rebased local changes onto newer disk configuration.")

            # Proceed with writing when no conflicting external changes detected
            conn.execute("DELETE FROM config")
            for key, value in config_to_write.items():
                if key == "tool":
                    # Retired; never persist tool rows.
                    continue
                if key == "plugin" and isinstance(value, dict):
                    for subtype, instances in value.items():
                        if not isinstance(instances, dict):
                            continue
                        normalized_subtype = _normalize_provider_name(subtype)
                        if not normalized_subtype:
                            continue
                        write_block = instances
                        force_multi = normalized_subtype in _multi_instance_plugin_names()
                        if force_multi:
                            write_block = normalize_multi_instance_plugin_block(
                                normalized_subtype,
                                instances,
                            )
                            # Keep in-memory tree aligned with what we persist.
                            if isinstance(config_to_write.get("plugin"), dict):
                                config_to_write["plugin"][normalized_subtype] = write_block
                        if force_multi or _is_multi_instance_plugin_config(write_block):
                            for name, settings in write_block.items():
                                if not isinstance(settings, dict):
                                    continue
                                for k, v in settings.items():
                                    if isinstance(v, dict):
                                        # Never persist nested instance maps as field values.
                                        continue
                                    if _encrypt_config_value is not None:
                                        val_str = _encrypt_config_value(config_to_write, "plugin", normalized_subtype, k, v)
                                    else:
                                        val_str = json.dumps(v) if not isinstance(v, str) else v
                                    conn.execute(
                                        "INSERT OR REPLACE INTO config (category, subtype, item_name, key, value) VALUES (?, ?, ?, ?, ?)",
                                        ("plugin", normalized_subtype, name, k, val_str),
                                    )
                                    count += 1
                        else:
                            for k, v in write_block.items():
                                if isinstance(v, dict):
                                    continue
                                if _encrypt_config_value is not None:
                                    val_str = _encrypt_config_value(config_to_write, "plugin", normalized_subtype, k, v)
                                else:
                                    val_str = json.dumps(v) if not isinstance(v, str) else v
                                conn.execute(
                                    "INSERT OR REPLACE INTO config (category, subtype, item_name, key, value) VALUES (?, ?, ?, ?, ?)",
                                    ("plugin", normalized_subtype, "default", k, val_str),
                                )
                                count += 1
                else:
                    if key in {"provider", "store"}:
                        continue
                    if not key.startswith("_") and value is not None:
                        val_str = json.dumps(value) if not isinstance(value, str) else value
                        conn.execute(
                            "INSERT OR REPLACE INTO config (category, subtype, item_name, key, value) VALUES (?, ?, ?, ?, ?)",
                            ("global", "none", "none", key, val_str),
                        )
                        count += 1

            # Re-read inside the same transaction so the baseline matches the
            # encrypted/round-tripped representation that future saves will see.
            try:
                cur = conn.cursor()
                cur.execute("SELECT category, subtype, item_name, key, value FROM config")
                refreshed_rows = cur.fetchall()
                refreshed = _prepare_disk_snapshot(rows_to_config(refreshed_rows))
                cur.close()
            except Exception:
                refreshed = deepcopy(config_to_write)
                _canonicalize_plugin_config(refreshed)
                _sync_alldebrid_api_key(refreshed)

        _CONFIG_CACHE = refreshed
        _LAST_SAVED_CONFIG = deepcopy(refreshed)
        # Keep the caller-provided object aligned with the saved baseline so a
        # follow-up edit in the same process does not save a stale tree.
        if isinstance(config, dict) and config is not refreshed:
            config.clear()
            config.update(deepcopy(refreshed))
        return count


    saved_entries = 0
    attempts = 0
    while True:
        try:
            saved_entries = _write_entries()
            # Central log entry (debug-only so normal saves stay quiet).
            debug(
                f"Synced {saved_entries} entries to {db.db_path} "
                f"({changed_count} changed entries)"
            )

            # Try to checkpoint WAL to ensure main DB file reflects latest state.
            # Use a separate short-lived connection to perform the checkpoint so
            # we don't contend with our main connection lock or active transactions.
            try:
                try:
                    with sqlite3.connect(str(db.db_path), timeout=_WAL_CHECKPOINT_TIMEOUT) as _con:
                        _con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    with sqlite3.connect(str(db.db_path), timeout=_WAL_CHECKPOINT_TIMEOUT) as _con:
                        _con.execute("PRAGMA wal_checkpoint")
            except Exception as exc:
                log(f"Warning: WAL checkpoint failed: {exc}")

            # Forensics disabled: audit/logs/backups removed to keep save lean.
            # Release the save lock we acquired earlier
            try:
                if lock_dir is not None and lock_dir.exists():
                    _release_save_lock(lock_dir)
            except Exception as exc:
                logger.exception("Failed to release save lock during save flow: %s", exc)

            break
        except sqlite3.OperationalError as exc:
            attempts += 1
            locked_error = "locked" in str(exc).lower()
            if not locked_error or attempts >= _CONFIG_SAVE_MAX_RETRIES:
                log(f"CRITICAL: Database write failed: {exc}")
                # Ensure we release potential save lock before bubbling error
                try:
                    if lock_dir is not None and lock_dir.exists():
                        _release_save_lock(lock_dir)
                except Exception as exc:
                    logger.exception("Failed to release save lock after DB write failure: %s", exc)
                raise
            delay = _CONFIG_SAVE_RETRY_DELAY * attempts
            log(f"Database locked; retry {attempts}/{_CONFIG_SAVE_MAX_RETRIES} in {delay:.2f}s")
            time.sleep(delay)
        except Exception as exc:
            log(f"CRITICAL: Configuration save failed: {exc}")
            try:
                if lock_dir is not None and lock_dir.exists():
                    _release_save_lock(lock_dir)
            except Exception as exc:
                logger.exception("Failed to release save lock after CRITICAL configuration save failure: %s", exc)
            raise

    return saved_entries


def load() -> Dict[str, Any]:
    """Return the parsed downlow configuration."""
    return load_config()


def save(config: Dict[str, Any]) -> int:
    """Persist *config* back to disk."""
    return save_config(config)


def save_config_and_verify(config: Dict[str, Any], retries: int = _CONFIG_SAVE_VERIFY_RETRIES, delay: float = _CONFIG_SAVE_RETRY_DELAY) -> int:
    """Save configuration and verify crucial keys persisted to disk.

    This helper performs a best-effort verification loop that reloads the
    configuration from disk and confirms that modified API key entries (e.g.
    AllDebrid) were written successfully. If verification fails after the
    configured number of retries, a RuntimeError is raised.
    """
    # Only perform the extra verification loop when the AllDebrid key actually changed.
    expected_key = _extract_expected_alldebrid_key(config)
    baseline_key = _extract_expected_alldebrid_key(_LAST_SAVED_CONFIG)
    if expected_key == baseline_key:
        expected_key = None

    last_exc: Exception | None = None
    for attempt in range(1, max(1, int(retries)) + 1):
        try:
            saved = save_config(config)
            if not expected_key:
                # Nothing special to verify; return success.
                return saved

            # Reload directly from disk and compare the canonical plugin key.
            clear_config_cache()
            reloaded = load_config()
            try:
                reloaded_key = _extract_expected_alldebrid_key(reloaded)
            except Exception:
                reloaded_key = None

            if reloaded_key == expected_key:
                try:
                    # Log a short, masked fingerprint to aid debugging without exposing the key itself
                    import hashlib

                    fp = hashlib.sha256(expected_key.encode("utf-8")).hexdigest()[:8]
                    debug(f"Verified AllDebrid API key persisted (fingerprint={fp})")
                except Exception:
                    # If hashing/logging fails, don't abort the save
                    pass
                return saved

            # Not yet persisted; log and retry
            log(f"Warning: Post-save verification attempt {attempt} failed (expected key not found in DB). Retrying...")
            time.sleep(delay * attempt)
        except Exception as exc:
            last_exc = exc
            log(f"Warning: save and verify attempt {attempt} failed: {exc}")
            time.sleep(delay * attempt)

    # All retries exhausted
    raise RuntimeError(f"Post-save verification failed after {retries} attempts: {last_exc}")


def count_changed_entries(config: Dict[str, Any]) -> int:
    """Return the number of changed configuration entries compared to the last saved snapshot.

    This is useful for user-facing messages that want to indicate how many entries
    were actually modified, not the total number of rows persisted to the database.
    """
    return _count_changed_entries(_LAST_SAVED_CONFIG, config)
