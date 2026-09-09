import datetime
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Sequence, Tuple

from SYS.cmdlet_spec import Cmdlet, CmdletArg
from SYS.config import (
    global_config,
    load_config,
    normalize_multi_instance_plugin_block,
    save_config,
    save_config_and_verify,
    set_nested_config_value,
)
from SYS.database import LOG_DB_PATH, db
from SYS.logger import log, status_panel
from SYS.plugin_config import (
    build_default_plugin_config,
    build_default_store_config,
    get_configurable_plugin_types,
    get_configurable_store_types,
    get_plugin_schema,
)
from SYS import pipeline as ctx
from SYS.result_table import Table
from cmdnat._parsing import (
    VALUE_ARG_FLAGS,
    extract_piped_value as _extract_piped_value,
    extract_arg_value as _extract_arg_value,
    extract_value_arg as _extract_value_arg,
    has_flag as _has_flag,
)


_PREFERENCES_BROWSE_PATH = "__preferences__"
_APPEARANCE_BROWSE_PATH = "__appearance__"
_PLUGINS_BROWSE_PATH = "__plugins__"
_PLUGIN_CATEGORY_KEYS = ("plugin",)
_CREATE_INSTANCE_FLAG = "-create-instance"
_CHOOSE_FLAG = "-choose"
_DELETE_FLAGS = frozenset({"-delete", "--delete", "-del", "--del", "-rm", "--rm"})
_KNOWN_SECTION_LABELS = {
    "plugin": "Plugins",
}
_KNOWN_SECTION_DESCRIPTIONS = {
    _PREFERENCES_BROWSE_PATH: "Global preferences and simple values",
    _APPEARANCE_BROWSE_PATH: "Table theme, colors, and panel styling",
    _PLUGINS_BROWSE_PATH: "All configured plugins and plugin instances",
    "plugin": "Plugin configuration",
}
_SENSITIVE_CONFIG_KEYS = {
    "access_key",
    "access_token",
    "api",
    "api_hash",
    "api_key",
    "apikey",
    "authorization",
    "bearer_token",
    "bot_token",
    "client_secret",
    "cookie",
    "cookies",
    "password",
    "passphrase",
    "private_key",
    "secret",
    "secret_key",
    "session_key",
    "token",
}
_SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "secret",
    "token",
    "apikey",
    "api_key",
    "access_key",
)
_CONFIG_ITEM_FIELDS = (
    "kind",
    "key",
    "title",
    "browse_path",
    "name",
    "value",
    "value_display",
    "type",
    "display_path",
    "instance_target",
    "choices",
)

CMDLET = Cmdlet(
    name=".config",
    summary="Manage configuration settings",
    usage='.config [key] [value] | @N | .config -delete | .config -upload | .config -log [count]',
    arg=[
        CmdletArg(
            name="key",
            description="Configuration key to update (dot-separated)",
            required=False
        ),
        CmdletArg(
            name="value",
            description="New value for the configuration key",
            required=False
        ),
        CmdletArg(
            name="delete",
            type="flag",
            description="Delete the selected multi-instance plugin instance (@N | .config -delete)",
            required=False,
        ),
        CmdletArg(
            name="log",
            type="flag",
            description="Show recent configuration save logs",
            required=False,
        ),
        CmdletArg(
            name="upload",
            type="flag",
            description="Open a file picker; the target plugin decides where the file is stored",
            required=False,
        ),
    ],
)


def _extract_log_limit(args: Sequence[str], default: int = 30) -> int:
    try:
        tokens = [str(arg).strip() for arg in (args or []) if str(arg).strip()]
    except Exception:
        return default

    for idx, token in enumerate(tokens):
        lowered = token.lower()
        if lowered in {"-log", "--log"}:
            if idx + 1 < len(tokens):
                candidate = tokens[idx + 1]
                if candidate and not candidate.startswith("-"):
                    try:
                        return max(1, min(200, int(candidate)))
                    except Exception:
                        return default
            return default
        if lowered.startswith("-log=") or lowered.startswith("--log="):
            _, value = lowered.split("=", 1)
            try:
                return max(1, min(200, int(value)))
            except Exception:
                return default
    return default


def _fallback_log_path() -> Path:
    return Path(db.db_path).with_name("logs") / "log_fallback.txt"


def _load_recent_config_logs(limit: int = 30) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    sql = """
        SELECT timestamp, level, module, message
        FROM logs
        WHERE lower(module) LIKE ?
           OR lower(message) LIKE ?
           OR lower(message) LIKE ?
           OR lower(message) LIKE ?
        ORDER BY id DESC
        LIMIT ?
    """
    params = (
        "%config%",
        "%config%",
        "%save failed%",
        "%saving configuration failed%",
        int(limit),
    )

    try:
        with sqlite3.connect(str(LOG_DB_PATH), timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(sql, params)
            fetched = cur.fetchall()
            cur.close()
        for row in fetched:
            rows.append(
                {
                    "timestamp": str(row["timestamp"] or ""),
                    "level": str(row["level"] or ""),
                    "module": str(row["module"] or ""),
                    "message": str(row["message"] or ""),
                }
            )
    except Exception:
        rows = []

    if rows:
        return rows

    fallback = _fallback_log_path()
    try:
        if not fallback.exists():
            return []
        lines = fallback.read_text(encoding="utf-8", errors="replace").splitlines()
        matches = [
            line for line in lines
            if any(term in line.lower() for term in ("config", "save failed", "saving configuration failed"))
        ]
        for line in reversed(matches[-limit:]):
            rows.append(
                {
                    "timestamp": "",
                    "level": "FALLBACK",
                    "module": "fallback",
                    "message": line,
                }
            )
    except Exception:
        return []
    return rows


def _format_log_timestamp_local(raw_value: str) -> str:
    text = str(raw_value or "").strip()
    if not text:
        return ""
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            parsed = datetime.datetime.strptime(text, pattern).replace(tzinfo=datetime.timezone.utc)
            return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
    return text


def _show_config_logs(args: Sequence[str]) -> int:
    limit = _extract_log_limit(args)
    rows = _load_recent_config_logs(limit=limit)
    if not rows:
        status_panel(
            "Configuration Logs",
            [("status", f"No recent config/save logs found in {LOG_DB_PATH.name}.")],
        )
        return 0

    table = Table("Configuration Logs")
    table.set_table("config.logs")
    table.set_source_command(".config", ["-log", str(limit)])

    for row_data in rows:
        row = table.add_row()
        row.add_column("Time (local)", _format_log_timestamp_local(row_data.get("timestamp", "")))
        row.add_column("Level", row_data.get("level", ""))
        row.add_column("Module", row_data.get("module", ""))
        row.add_column("Message", row_data.get("message", ""))

    ctx.set_last_result_table_overlay(table, rows)
    ctx.set_current_stage_table(table)
    return 0


def set_nested_config(config: Dict[str, Any], key: str, value: str) -> bool:
    return set_nested_config_value(
        config,
        key,
        value,
        on_error=lambda msg: status_panel("config", [("error", msg)]),
    )


def _visible_config_entries(config_data: Any) -> List[tuple[str, Any]]:
    if not isinstance(config_data, dict):
        return []
    return [
        (str(key), value)
        for key, value in config_data.items()
        if isinstance(key, str) and not key.startswith("_")
    ]


def _format_config_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "Configuration"
    if text == _PREFERENCES_BROWSE_PATH:
        return "Preferences"
    if text == _APPEARANCE_BROWSE_PATH:
        return "Appearance"
    if text == _PLUGINS_BROWSE_PATH:
        return "Plugins"
    overrides = {
        "archiveorg": "Archive.org",
        "archive.org": "Archive.org",
        "openlibrary": "Archive.org",
        "internetarchive": "Archive.org",
        "ytdlp": "yt-dlp",
    }
    mapped = overrides.get(text.lower())
    if mapped:
        return mapped
    return text.replace("_", " ").replace("-", " ").strip().title()


def _format_config_path_label(browse_path: Optional[str]) -> str:
    text = str(browse_path or "").strip()
    if not text:
        return "Root"
    if text == _PREFERENCES_BROWSE_PATH:
        return "Preferences"
    if text == _APPEARANCE_BROWSE_PATH:
        return "Appearance"
    if text == _PLUGINS_BROWSE_PATH:
        return "Plugins"
    parts = [part for part in text.split(".") if part]
    formatted: List[str] = []
    for idx, part in enumerate(parts):
        if idx == 0 and part in _PLUGIN_CATEGORY_KEYS:
            formatted.append("Plugins")
        else:
            formatted.append(_format_config_label(part))
    return " / ".join(formatted)


def _format_config_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _is_sensitive_config_key(key_path: str) -> bool:
    leaf = str(key_path or "").split(".")[-1].strip().lower()
    if leaf in _SENSITIVE_CONFIG_KEYS:
        return True
    if any(fragment in leaf for fragment in _SENSITIVE_KEY_FRAGMENTS):
        return True
    parts = _split_config_path(key_path)
    if len(parts) >= 2 and parts[0] == "plugin":
        try:
            schema = get_plugin_schema(parts[1]) or []
        except Exception:
            schema = []
        for field in schema:
            if str(field.get("key") or "").strip().lower() != leaf:
                continue
            if field.get("secret") or str(field.get("type") or "").strip().lower() == "secret":
                return True
    return False


def _mask_secret_display(value: Any) -> str:
    if value is None or not str(value).strip():
        return "(unset)"
    return "********"


def _format_config_entry_count(value: Any) -> str:
    count = len(_visible_config_entries(value)) if isinstance(value, dict) else 0
    if count == 1:
        return "1 entry"
    return f"{count} entries"


def _get_configurable_plugin_names() -> List[str]:
    try:
        return [
            str(name).strip().lower()
            for name in (get_configurable_plugin_types() or [])
            if str(name).strip()
        ]
    except Exception:
        return []


def _get_multi_instance_plugin_names() -> set[str]:
    try:
        return {
            str(name).strip().lower()
            for name in (get_configurable_store_types() or [])
            if str(name).strip()
        }
    except Exception:
        return set()


def _split_config_path(value: Optional[str]) -> List[str]:
    return [part for part in str(value or "").split(".") if part]


def _is_multi_instance_plugin_name(name: str) -> bool:
    normalized = str(name or "").strip().lower()
    if not normalized:
        return False
    if normalized in _get_multi_instance_plugin_names():
        return True
    try:
        from SYS.plugin_config import get_plugin_class

        plugin_cls = get_plugin_class(normalized)
        return bool(plugin_cls and getattr(plugin_cls, "MULTI_INSTANCE", False))
    except Exception:
        return False


def _is_multi_instance_plugin_root_path(browse_path: Optional[str]) -> bool:
    parts = _split_config_path(browse_path)
    if len(parts) != 2 or parts[0] != "plugin":
        return False
    if _is_multi_instance_plugin_name(parts[1]):
        return True
    # Fallback: already-normalized dict-of-instances (or empty) under a plugin
    # that was previously saved as multi-instance.
    return False


def _plugin_schema_field_keys(plugin_name: str) -> set[str]:
    defaults = build_default_plugin_config(plugin_name)
    if not isinstance(defaults, dict):
        return set()
    return {
        str(key or "").strip().lower()
        for key in defaults.keys()
        if str(key or "").strip()
    }


def _looks_like_single_instance_branch(plugin_name: str, branch: Any) -> bool:
    if not isinstance(branch, dict) or not branch:
        return False
    # Hybrid branches (flat fields + nested instances) are multi-instance shaped
    # after normalization — not single-instance.
    has_nested_instance = any(isinstance(v, dict) for v in branch.values())
    if has_nested_instance and any(not isinstance(v, dict) for v in branch.values()):
        return False
    if all(isinstance(v, dict) for v in branch.values()):
        return False

    schema_keys = _plugin_schema_field_keys(plugin_name)
    entry_keys = {str(key or "").strip().lower() for key in branch.keys()}
    return bool(schema_keys and entry_keys.intersection(schema_keys))


def _normalize_multi_instance_branch(plugin_name: str, branch: Any) -> Dict[str, Any]:
    return normalize_multi_instance_plugin_block(plugin_name, branch)


def _dict_get_ci(mapping: Dict[str, Any], key: str) -> tuple[Optional[str], Any]:
    """Case-insensitive dict get. Returns (actual_key, value) or (None, None)."""
    if not isinstance(mapping, dict):
        return None, None
    if key in mapping:
        return key, mapping.get(key)
    target = str(key or "").strip().lower()
    if not target:
        return None, None
    for raw_key, raw_value in mapping.items():
        if str(raw_key or "").strip().lower() == target:
            return str(raw_key), raw_value
    return None, None


def _is_multi_instance_name_field(key_name: str) -> bool:
    return str(key_name or "").strip().lower() == "name"


def _is_multi_instance_instance_path(browse_path: Optional[str]) -> bool:
    parts = _split_config_path(browse_path)
    return (
        len(parts) >= 3
        and parts[0] == "plugin"
        and _is_multi_instance_plugin_name(parts[1])
    )


def _plugin_field_label(plugin_name: str, key_name: str) -> Optional[str]:
    target = str(key_name or "").strip().lower()
    if not target:
        return None
    try:
        schema = get_plugin_schema(plugin_name) or []
    except Exception:
        return None
    for field in schema:
        if str(field.get("key") or "").strip().lower() != target:
            continue
        label = str(field.get("label") or "").strip()
        return label or None
    return None


def _build_create_instance_item(category: str, plugin_name: str) -> Dict[str, Any]:
    target = f"{category}.{plugin_name}"
    return {
        "kind": "create_instance",
        "key": f"{target}.__new_instance__",
        "title": "Add Instance",
        "name": "add_instance",
        "value": None,
        "value_display": "Create with @N | .config <name>",
        "display_path": f"{_format_config_path_label(target)} / Add Instance",
        "type": "action",
        "instance_target": target,
    }


def _build_synthetic_plugin_branch(category: str, name: str) -> Optional[Dict[str, Any]]:
    normalized_category = str(category or "").strip().lower()
    normalized_name = str(name or "").strip().lower()
    if not normalized_name:
        return None

    # Multi-instance plugins start empty: only real instances + Add Instance.
    if normalized_name in _get_multi_instance_plugin_names():
        return {}

    branch = build_default_plugin_config(normalized_name)
    if not isinstance(branch, dict):
        return None
    return dict(branch)


def _find_configured_plugin_branch(
    config_data: Dict[str, Any],
    category: str,
    name: str,
) -> Optional[tuple[str, Dict[str, Any]]]:
    category_block = config_data.get(category)
    if not isinstance(category_block, dict):
        return None

    target = str(name or "").strip().lower()
    for raw_name, raw_value in _visible_config_entries(category_block):
        if str(raw_name or "").strip().lower() != target or not isinstance(raw_value, dict):
            continue
        return raw_name, raw_value
    return None


def _resolve_plugin_branch(
    config_data: Dict[str, Any],
    category: str,
    name: str,
) -> Optional[tuple[str, Dict[str, Any], bool]]:
    found = _find_configured_plugin_branch(config_data, category, name)
    if found is not None:
        resolved_name, resolved_value = found
        return resolved_name, resolved_value, True

    normalized_category = str(category or "").strip().lower()
    normalized_name = str(name or "").strip().lower()
    if not normalized_name:
        return None

    if normalized_name not in _get_configurable_plugin_names():
        return None

    synthetic = _build_synthetic_plugin_branch(normalized_category, normalized_name)
    if synthetic is None:
        return None
    return normalized_name, synthetic, False


def _iter_plugin_branches(config_data: Dict[str, Any]) -> List[tuple[str, str, Any]]:
    branches: List[tuple[str, str, Any]] = []
    if not isinstance(config_data, dict):
        return branches

    for category in _PLUGIN_CATEGORY_KEYS:
        category_block = config_data.get(category)
        if not isinstance(category_block, dict):
            continue
        for name, value in _visible_config_entries(category_block):
            branches.append((category, name, value))
    return branches


def _canonical_plugin_list_name(name: str) -> str:
    normalized = str(name or "").strip().lower()
    if not normalized:
        return ""
    try:
        from PluginCore.registry import REGISTRY

        info = REGISTRY.get(normalized)
        if info is not None and getattr(info, "canonical_name", None):
            return str(info.canonical_name).strip().lower()
    except Exception:
        pass
    try:
        from SYS.config import _PLUGIN_CONFIG_ALIASES

        return str(_PLUGIN_CONFIG_ALIASES.get(normalized) or normalized)
    except Exception:
        return normalized


def _iter_available_plugin_branches(config_data: Dict[str, Any]) -> List[tuple[str, str, Any, bool]]:
    branches: List[tuple[str, str, Any, bool]] = []
    seen: set[str] = set()

    for category, name, value in _iter_plugin_branches(config_data):
        normalized_name = str(name or "").strip().lower()
        if not normalized_name:
            continue
        list_name = _canonical_plugin_list_name(normalized_name) or normalized_name
        if list_name in seen:
            continue
        branches.append((category, list_name, value, True))
        seen.add(list_name)
        seen.add(normalized_name)

    for name in _get_configurable_plugin_names():
        list_name = _canonical_plugin_list_name(name) or name
        if list_name in seen or name in seen:
            continue
        synthetic = _build_synthetic_plugin_branch("plugin", list_name)
        if synthetic is None:
            continue
        branches.append(("plugin", list_name, synthetic, False))
        seen.add(list_name)
        seen.add(name)

    return branches


def _collect_plugin_root_items(config_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    plugin_items: Dict[str, Dict[str, Any]] = {}
    for category, name, value, is_configured in _iter_available_plugin_branches(config_data):
        key = str(name or "").strip().lower()
        if not key:
            continue
        existing = plugin_items.get(key)
        if existing is None:
            plugin_items[key] = {
                "kind": "section",
                "title": _format_config_label(name),
                "browse_path": f"{category}.{name}",
                "summary": _format_config_entry_count(value),
                "type": "section",
                "description": "Plugin configuration" if is_configured else "Plugin configuration (available to configure)",
            }
            continue

        if str(category) == "plugin" and not str(existing.get("browse_path") or "").startswith("plugin."):
            existing["browse_path"] = f"{category}.{name}"
        try:
            current_count = int(str(existing.get("summary") or "0").split()[0])
        except Exception:
            current_count = 0
        extra_count = len(_visible_config_entries(value)) if isinstance(value, dict) else 0
        merged_count = current_count + extra_count
        existing["summary"] = "1 entry" if merged_count == 1 else f"{merged_count} entries"

    return sorted(plugin_items.values(), key=lambda item: str(item.get("title") or "").lower())


def _resolve_config_branch(
    config_data: Dict[str, Any],
    browse_path: Optional[str],
) -> Optional[Dict[str, Any]]:
    text = str(browse_path or "").strip()
    if not text:
        return config_data if isinstance(config_data, dict) else None

    if text == _PREFERENCES_BROWSE_PATH:
        return _preference_branch(config_data)

    if text == _APPEARANCE_BROWSE_PATH:
        return _appearance_branch(config_data)

    if text == _PLUGINS_BROWSE_PATH:
        return {
            str(item.get("title") or ""): item
            for item in _collect_plugin_root_items(config_data)
        }

    parts = [part for part in text.split(".") if part]
    if len(parts) >= 2 and parts[0] in _PLUGIN_CATEGORY_KEYS:
        resolved = _resolve_plugin_branch(config_data, parts[0], parts[1])
        if resolved is None:
            return None
        _, current, _ = resolved
        if parts[0] == "plugin" and _is_multi_instance_plugin_name(parts[1]):
            current = _normalize_multi_instance_branch(parts[1], current)
        for part in parts[2:]:
            if not isinstance(current, dict):
                return None
            _actual, current = _dict_get_ci(current, part)
            if _actual is None:
                return None
        return current if isinstance(current, dict) else None

    current: Any = config_data
    for part in parts:
        if not isinstance(current, dict):
            return None
        _actual, current = _dict_get_ci(current, part)
        if _actual is None:
            return None
    return current if isinstance(current, dict) else None


def _build_section_item(
    *,
    title: str,
    browse_path: str,
    value: Any,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "kind": "section",
        "title": title,
        "browse_path": browse_path,
        "summary": _format_config_entry_count(value),
        "type": "section",
        "description": str(description or "").strip() or _KNOWN_SECTION_DESCRIPTIONS.get(browse_path, ""),
    }


def _build_value_item(
    *,
    key_path: str,
    name: str,
    value: Any,
    choices: Any = None,
) -> Dict[str, Any]:
    sensitive = _is_sensitive_config_key(key_path)
    display_value = _mask_secret_display(value) if sensitive else _format_config_value(value)
    path_parts = [part for part in str(key_path or "").split(".") if part]
    display_path = " / ".join(
        [_format_config_path_label(".".join(path_parts[:-1]))] if len(path_parts) > 1 else []
        + [_format_config_label(path_parts[-1])] if path_parts else [_format_config_label(name)]
    )
    choice_list = list(choices) if isinstance(choices, (list, tuple)) else []
    return {
        "kind": "value",
        "key": key_path,
        "name": name,
        "title": _format_config_label(name),
        "value": "" if sensitive else value,
        "value_display": display_value,
        "secret": sensitive,
        "display_path": display_path,
        "type": type(value).__name__,
        "choices": choice_list,
    }


def _create_or_get_plugin_instance(
    config_data: Dict[str, Any],
    instance_target: str,
    instance_name: str,
) -> tuple[str, bool]:
    parts = _split_config_path(instance_target)
    if len(parts) != 2 or parts[0] != "plugin":
        raise ValueError(f"Unsupported instance target '{instance_target}'")

    category, plugin_name = parts
    raw_instance_name = str(instance_name or "").strip()
    if not raw_instance_name:
        raise ValueError("Instance name is required")
    if raw_instance_name.startswith("_"):
        raise ValueError("Instance names cannot start with '_' characters")
    if raw_instance_name.lower() == "default":
        raise ValueError("Instance name 'default' is reserved; choose a real name")

    category_block = config_data.get(category)
    if not isinstance(category_block, dict):
        category_block = {}
        config_data[category] = category_block

    # Resolve plugin key case-insensitively.
    actual_plugin_key, plugin_block = _dict_get_ci(category_block, plugin_name)
    if actual_plugin_key is None or not isinstance(plugin_block, dict):
        actual_plugin_key = plugin_name
        plugin_block = {}
    plugin_name = actual_plugin_key

    # Repair hybrid/legacy shapes before mutating.
    if _is_multi_instance_plugin_name(plugin_name):
        plugin_block = normalize_multi_instance_plugin_block(plugin_name, plugin_block)
    elif not isinstance(plugin_block, dict):
        plugin_block = {}
    category_block[plugin_name] = plugin_block

    target_key = None
    lowered_target = raw_instance_name.lower()
    for existing_key in plugin_block.keys():
        if str(existing_key or "").strip().lower() == lowered_target:
            target_key = str(existing_key)
            break

    if target_key is not None and isinstance(plugin_block.get(target_key), dict):
        return f"{category}.{plugin_name}.{target_key}", False

    if _is_multi_instance_plugin_name(plugin_name):
        # Sets NAME from the instance key when the store schema still includes it.
        instance_cfg = dict(build_default_store_config(plugin_name, raw_instance_name))
        # Ensure schema defaults exist even if store helper returned a sparse dict.
        for key, value in dict(build_default_plugin_config(plugin_name)).items():
            if key not in instance_cfg:
                instance_cfg[key] = value
        instance_cfg.setdefault("NAME", raw_instance_name)
    else:
        instance_cfg = dict(build_default_plugin_config(plugin_name))

    if not instance_cfg:
        raise ValueError(
            f"No configurable fields found for plugin '{plugin_name}'. "
            "Check that the plugin is installed and exposes a config schema."
        )

    plugin_block[raw_instance_name] = instance_cfg
    category_block[plugin_name] = plugin_block
    return f"{category}.{plugin_name}.{raw_instance_name}", True


def _is_multi_instance_instance_browse_path(browse_path: Optional[str]) -> bool:
    parts = _split_config_path(browse_path)
    return (
        len(parts) == 3
        and parts[0] == "plugin"
        and _is_multi_instance_plugin_name(parts[1])
        and bool(str(parts[2] or "").strip())
        and str(parts[2]).strip().lower() != "default"
    )


def _delete_plugin_instance(
    config_data: Dict[str, Any],
    instance_path: str,
) -> tuple[str, str]:
    """Delete a multi-instance plugin instance.

    Returns ``(deleted_instance_name, parent_browse_path)``.
    """
    parts = _split_config_path(instance_path)
    if len(parts) != 3 or parts[0] != "plugin":
        raise ValueError(
            "Delete expects a plugin instance path like plugin.<name>.<instance>"
        )
    category, plugin_name, instance_name = parts
    if str(instance_name).strip().lower() == "default":
        raise ValueError("Cannot delete the reserved 'default' instance name")

    category_block = config_data.get(category)
    if not isinstance(category_block, dict):
        raise ValueError(f"No plugin configuration found for '{plugin_name}'")

    actual_plugin_key, plugin_block = _dict_get_ci(category_block, plugin_name)
    if actual_plugin_key is None or not isinstance(plugin_block, dict):
        raise ValueError(f"Plugin '{plugin_name}' is not configured")

    looks_multi = (
        _is_multi_instance_plugin_name(plugin_name)
        or (
            bool(plugin_block)
            and all(isinstance(v, dict) for v in plugin_block.values())
        )
    )
    if not looks_multi:
        raise ValueError(
            f"Plugin '{plugin_name}' is not multi-instance; only named instances can be deleted this way"
        )

    plugin_block = normalize_multi_instance_plugin_block(actual_plugin_key, plugin_block)
    category_block[actual_plugin_key] = plugin_block

    actual_instance_key, instance_cfg = _dict_get_ci(plugin_block, instance_name)
    if actual_instance_key is None or not isinstance(instance_cfg, dict):
        raise ValueError(
            f"Instance '{instance_name}' not found under '{_format_config_path_label(f'{category}.{actual_plugin_key}')}'"
        )

    del plugin_block[actual_instance_key]
    if plugin_block:
        category_block[actual_plugin_key] = plugin_block
    else:
        # Keep an empty multi-instance map so the plugin remains browsable.
        category_block[actual_plugin_key] = {}

    parent_path = f"{category}.{actual_plugin_key}"
    return actual_instance_key, parent_path


def _current_config_browse_path() -> Optional[str]:
    """Best-effort browse path from the active config table source command."""
    try:
        source_cmd = ctx.get_current_stage_table_source_command()
        source_args = ctx.get_current_stage_table_source_args()
    except Exception:
        source_cmd = None
        source_args = None
    if str(source_cmd or "").replace("_", "-").strip().lower() not in {".config", "config"}:
        return None
    return _extract_browse_arg(list(source_args or []))


def _resolve_delete_target(
    *,
    args: Sequence[str],
    selection_item: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Resolve which config path to delete from flags and/or selection."""
    wants_delete = any(
        str(arg or "").strip().lower() in _DELETE_FLAGS for arg in (args or [])
    )
    if not wants_delete:
        return None

    # Optional explicit path: .config -delete plugin.hydrusnetwork.Typhon
    tokens = [str(arg).strip() for arg in (args or []) if str(arg).strip()]
    positional = [
        token
        for token in tokens
        if not token.startswith("-") and token.lower() not in _DELETE_FLAGS
    ]
    if positional:
        return positional[0]

    selection_kind = str((selection_item or {}).get("kind") or "").strip().lower()
    selection_browse_path = str((selection_item or {}).get("browse_path") or "").strip()
    selection_key = str((selection_item or {}).get("key") or "").strip()

    if selection_kind == "section" and selection_browse_path:
        return selection_browse_path
    if selection_kind == "value" and selection_key:
        # Deleting from inside an instance page: delete the whole instance.
        parent = _parent_browse_path_for_key(selection_key)
        if parent and _is_multi_instance_instance_browse_path(parent):
            return parent
        return selection_key
    if selection_browse_path:
        return selection_browse_path
    if selection_key:
        return selection_key

    # No row selected: if the open table is an instance page, delete that instance.
    current_browse = _current_config_browse_path()
    if current_browse and _is_multi_instance_instance_browse_path(current_browse):
        return current_browse
    return ""


def _resolve_update_key(config_data: Dict[str, Any], selection_key: str) -> str:
    parts = _split_config_path(selection_key)
    if (
        len(parts) >= 4
        and parts[0] == "plugin"
        and parts[2].lower() == "default"
        and _is_multi_instance_plugin_name(parts[1])
    ):
        category_block = config_data.get(parts[0])
        plugin_block = category_block.get(parts[1]) if isinstance(category_block, dict) else None
        if _looks_like_single_instance_branch(parts[1], plugin_block):
            return ".".join([parts[0], parts[1], *parts[3:]])
    return selection_key


def _build_root_config_items(config_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    visible_entries = _visible_config_entries(config_data)

    preferences = _preference_branch(config_data)
    if preferences:
        items.append(
            _build_section_item(
                title="Preferences",
                browse_path=_PREFERENCES_BROWSE_PATH,
                value=preferences,
            )
        )

    appearance = _appearance_branch(config_data)
    if appearance:
        items.append(
            _build_section_item(
                title="Appearance",
                browse_path=_APPEARANCE_BROWSE_PATH,
                value=appearance,
            )
        )

    plugin_items = _collect_plugin_root_items(config_data)
    if plugin_items:
        items.append(
            _build_section_item(
                title="Plugins",
                browse_path=_PLUGINS_BROWSE_PATH,
                value={item["title"]: item for item in plugin_items},
            )
        )

    other_sections: List[Dict[str, Any]] = []
    # Retired top-level namespaces must never appear beside Plugins.
    _hidden_root_sections = set(_PLUGIN_CATEGORY_KEYS) | {"tool", "provider", "store"}
    for key, value in visible_entries:
        if key in _hidden_root_sections or not isinstance(value, dict):
            continue
        other_sections.append(
            _build_section_item(
                title=_format_config_label(key),
                browse_path=key,
                value=value,
            )
        )

    other_sections.sort(key=lambda item: str(item.get("title") or "").lower())
    items.extend(other_sections)
    return items


def _global_schema_fields() -> List[Dict[str, Any]]:
    try:
        fields = global_config() or []
    except Exception:
        return []
    return [field for field in fields if isinstance(field, dict) and field.get("key")]


def _global_schema_field(key_name: str) -> Optional[Dict[str, Any]]:
    target = str(key_name or "").strip().lower()
    if not target:
        return None
    for field in _global_schema_fields():
        if str(field.get("key") or "").strip().lower() == target:
            return field
    return None


def _normalize_choice_list(choices: Any) -> List[str]:
    if not isinstance(choices, (list, tuple)):
        return []
    normalized: List[str] = []
    seen: set[str] = set()
    for choice in choices:
        text = str(choice).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _lookup_plugin_choices(browse_path: str, key_name: str) -> Optional[List[str]]:
    parts = _split_config_path(browse_path)
    if len(parts) < 2 or parts[0] != "plugin":
        return None
    plugin_name = parts[1]
    try:
        from SYS.plugin_config import get_plugin_schema
        schema = get_plugin_schema(plugin_name)
    except Exception:
        return None
    target = str(key_name or "").strip().lower()
    for field in schema or []:
        if str(field.get("key") or "").strip().lower() != target:
            continue
        choices = _normalize_choice_list(field.get("choices"))
        return choices or None
    return None


def _lookup_config_choices(key_path: str, field_name: Optional[str] = None) -> List[str]:
    parts = _split_config_path(key_path)
    leaf = str(field_name or (parts[-1] if parts else "") or "").strip()
    if not leaf:
        return []

    if len(parts) <= 1:
        field = _global_schema_field(leaf)
        if field is not None:
            return _normalize_choice_list(field.get("choices"))
        return []

    if parts[0] == "plugin":
        parent = ".".join(parts[:-1]) if len(parts) > 1 else str(key_path)
        return list(_lookup_plugin_choices(parent, leaf) or [])

    return []


def _schema_keys_for_group(group: str) -> set[str]:
    target = str(group or "").strip().lower()
    keys: set[str] = set()
    if not target:
        return keys
    for field in _global_schema_fields():
        if str(field.get("group") or "").strip().lower() != target:
            continue
        key = str(field.get("key") or "").strip()
        if key:
            keys.add(key)
    return keys


def _appearance_keys() -> set[str]:
    return _schema_keys_for_group("Appearance")


def _preference_branch(config_data: Dict[str, Any]) -> Dict[str, Any]:
    skip = _appearance_keys()
    branch: Dict[str, Any] = {
        key: value
        for key, value in _visible_config_entries(config_data)
        if not isinstance(value, dict) and key not in skip
    }
    for field in _global_schema_fields():
        key = str(field.get("key") or "").strip()
        if not key or key in branch or key in skip:
            continue
        default = field.get("default", "")
        if isinstance(default, dict):
            continue
        branch[key] = default
    return branch


def _appearance_branch(config_data: Dict[str, Any]) -> Dict[str, Any]:
    keep = _appearance_keys()
    branch: Dict[str, Any] = {
        key: value
        for key, value in _visible_config_entries(config_data)
        if key in keep and not isinstance(value, dict)
    }
    for field in _global_schema_fields():
        key = str(field.get("key") or "").strip()
        if not key or key not in keep or key in branch:
            continue
        default = field.get("default", "")
        if isinstance(default, dict):
            continue
        branch[key] = default
    return branch


def _value_item_title(key_path: str, name: str) -> str:
    parts = _split_config_path(key_path)
    if len(parts) <= 1:
        field = _global_schema_field(name)
        label = str((field or {}).get("label") or "").strip()
        if label:
            return label
    return _format_config_label(name)


def _build_nested_config_items(
    config_data: Dict[str, Any],
    browse_path: str,
) -> List[Dict[str, Any]]:
    if browse_path == _PLUGINS_BROWSE_PATH:
        return _collect_plugin_root_items(config_data)

    branch = _resolve_config_branch(config_data, browse_path)
    if branch is None:
        return []

    section_items: List[Dict[str, Any]] = []
    value_items: List[Dict[str, Any]] = []
    action_items: List[Dict[str, Any]] = []
    is_flat_view = browse_path in {_PREFERENCES_BROWSE_PATH, _APPEARANCE_BROWSE_PATH}
    parts = _split_config_path(browse_path)
    is_multi_instance_root = _is_multi_instance_plugin_root_path(browse_path)

    if is_multi_instance_root:
        branch = _normalize_multi_instance_branch(parts[1], branch)

    hide_instance_name_field = _is_multi_instance_instance_path(browse_path)

    # Surface full plugin schema even if some keys were never written yet.
    effective_branch = dict(branch) if isinstance(branch, dict) else {}
    merge_schema = (
        len(parts) >= 2
        and parts[0] == "plugin"
        and (hide_instance_name_field or not is_multi_instance_root)
    )
    if merge_schema:
        try:
            schema_defaults = build_default_plugin_config(parts[1]) or {}
        except Exception:
            schema_defaults = {}
        if isinstance(schema_defaults, dict):
            for key, default in schema_defaults.items():
                if key not in effective_branch:
                    effective_branch[key] = default

    for key, value in _visible_config_entries(effective_branch):
        if hide_instance_name_field and _is_multi_instance_name_field(key):
            # Instance name is the table path key; a separate NAME field is redundant.
            continue
        full_key = key if is_flat_view else f"{browse_path}.{key}"
        if isinstance(value, dict):
            # Nested dicts are not valid fields on an instance page.
            if hide_instance_name_field:
                continue
            section_items.append(
                _build_section_item(
                    title=_format_config_label(key),
                    browse_path=full_key,
                    value=value,
                )
            )
        else:
            display_value = value
            if (
                not str(value or "").strip()
                and len(parts) >= 2
                and parts[0] == "plugin"
                and str(key).strip().lower() in {"cookie_file", "cookies"}
            ):
                try:
                    from SYS.config import resolve_cookies_path

                    resolved = resolve_cookies_path(config_data)
                    if resolved is not None:
                        display_value = str(resolved)
                except Exception:
                    display_value = value
            item = _build_value_item(
                key_path=full_key,
                name=key,
                value=display_value,
                choices=_lookup_config_choices(full_key, key),
            )
            item["title"] = _value_item_title(full_key, key)
            if len(parts) >= 2 and parts[0] == "plugin":
                plugin_label = _plugin_field_label(parts[1], key)
                if plugin_label:
                    item["title"] = plugin_label
            value_items.append(item)

    section_items.sort(key=lambda item: str(item.get("title") or "").lower())
    value_items.sort(key=lambda item: str(item.get("title") or item.get("name") or "").lower())
    if is_multi_instance_root:
        action_items.append(_build_create_instance_item(parts[0], parts[1]))
    return section_items + value_items + action_items


def _build_config_items(
    config_data: Dict[str, Any],
    browse_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    text = str(browse_path or "").strip()
    if not text:
        return _build_root_config_items(config_data)
    return _build_nested_config_items(config_data, text)


def _build_config_table_title(browse_path: Optional[str]) -> str:
    text = str(browse_path or "").strip()
    if not text:
        return "Configuration"
    return f"Configuration: {_format_config_path_label(text)}"


def _plugin_config_header_lines(browse_path: Optional[str]) -> List[str]:
    parts = _split_config_path(browse_path)
    if len(parts) < 2 or parts[0] != "plugin":
        return []
    plugin_name = parts[1]
    instance_name = parts[2] if len(parts) >= 3 else None
    try:
        from SYS.plugin_config import get_plugin_class

        plugin_cls = get_plugin_class(plugin_name)
        if plugin_cls is None:
            return []
        header_fn = getattr(plugin_cls, "config_header_lines", None)
        if not callable(header_fn):
            return []
        extra = header_fn(instance_name=instance_name)
        return [str(line).strip() for line in (extra or []) if str(line).strip()]
    except Exception:
        return []


def _build_config_header_lines(browse_path: Optional[str]) -> List[str]:
    text = str(browse_path or "").strip()
    if not text:
        return [
            "Use @N on a section to drill in. Use @.. to go back.",
        ]
    path_line = f"Path: {_format_config_path_label(text)}"
    plugin_lines = _plugin_config_header_lines(text)
    parts = _split_config_path(text)
    if text in {_PREFERENCES_BROWSE_PATH, _APPEARANCE_BROWSE_PATH}:
        nav = "Use @N on a setting to pick from available options (or see how to set free-text values). Use @N | .config <value> to set directly. Use @.. to go back."
    elif _is_multi_instance_plugin_root_path(text):
        nav = "Use @N on an instance to open it. On Add Instance: @N | .config <name>. Delete an instance: @N | .config -delete. Use @.. to go back."
    elif len(parts) == 3 and parts[0] == "plugin" and _is_multi_instance_plugin_name(parts[1]):
        nav = "Use @N on a setting to choose/edit it, or @N | .config <value> to set it. Delete this instance: .config -delete. Use @.. to go back."
    else:
        nav = "Use @N on a section to drill in. Use @N on a setting to choose/edit it, or @N | .config <value> to set directly. Use @.. to go back."
    return [path_line, *plugin_lines, nav]


def _parent_browse_path_for_key(key_path: str) -> Optional[str]:
    parts = _split_config_path(key_path)
    if not parts:
        return None
    if len(parts) == 1:
        if parts[0] in _appearance_keys():
            return _APPEARANCE_BROWSE_PATH
        return _PREFERENCES_BROWSE_PATH
    return ".".join(parts[:-1])


def _format_current_choice_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value).strip()


def _extract_create_instance_target(args: Sequence[str]) -> Optional[str]:
    return _extract_arg_value(args, flags={_CREATE_INSTANCE_FLAG, "--create-instance"}, allow_positional=False)


def _extract_browse_arg(args: Sequence[str]) -> Optional[str]:
    return _extract_arg_value(args, flags={"-browse", "--browse"}, allow_positional=False)


def _extract_choose_arg(args: Sequence[str]) -> Optional[str]:
    return _extract_arg_value(args, flags={_CHOOSE_FLAG}, allow_positional=False)


def _resolve_config_value_raw(config: Dict[str, Any], key_path: str) -> Any:
    parts = [p for p in str(key_path or "").split(".") if p]
    current: Any = config
    for part in parts:
        if not isinstance(current, dict):
            return None
        _actual, current = _dict_get_ci(current, part)
        if _actual is None:
            return None
    return current


def _extract_selected_update_value(args: Sequence[str]) -> Optional[str]:
    explicit = _extract_arg_value(args, flags=VALUE_ARG_FLAGS, allow_positional=False)
    if explicit is not None:
        return explicit

    tokens = [str(arg).strip() for arg in (args or []) if str(arg).strip()]
    positional = [token for token in tokens if not token.startswith("-")]
    if len(positional) == 1:
        return positional[0]
    return None


def _get_selected_config_item() -> Optional[Dict[str, Any]]:
    try:
        indices = ctx.get_last_selection() or []
    except Exception:
        indices = []
    try:
        items = ctx.get_last_result_items() or []
    except Exception:
        items = []
    if not indices or not items:
        return None
    idx = indices[0]
    if idx < 0 or idx >= len(items):
        return None
    return _normalize_config_item(items[idx])


def _normalize_config_item(candidate: Any) -> Optional[Dict[str, Any]]:
    if candidate is None:
        return None

    normalized: Dict[str, Any] = {}
    sources: List[Any] = [candidate]

    if isinstance(candidate, dict):
        extra = candidate.get("extra")
        if isinstance(extra, dict):
            sources.append(extra)
    else:
        try:
            extra = getattr(candidate, "extra", None)
        except Exception:
            extra = None
        if isinstance(extra, dict):
            sources.append(extra)

    for source in sources:
        if isinstance(source, dict):
            getter = source.get
            for key in _CONFIG_ITEM_FIELDS:
                if key in normalized:
                    continue
                value = getter(key)
                if value is not None:
                    normalized[key] = value
            continue

        for key in _CONFIG_ITEM_FIELDS:
            if key in normalized:
                continue
            try:
                value = getattr(source, key, None)
            except Exception:
                value = None
            if value is not None:
                normalized[key] = value

    return normalized or None


def _show_choice_table(
    target_key: str,
    choices: List[str],
    current_value: str,
    display_name: str,
) -> int:
    current_norm = str(current_value or "").strip().lower()
    items: List[Dict[str, Any]] = []
    for choice in choices:
        choice_str = str(choice)
        marker = "  ← current" if choice_str.strip().lower() == current_norm else ""
        items.append({
            "kind": "choice",
            "key": target_key,
            "title": choice_str,
            "value": choice_str,
            "display": f"{choice_str}{marker}",
        })

    parent_path = _parent_browse_path_for_key(target_key)
    table = Table(f"Configuration: {display_name}", preserve_order=True)
    table.set_table("config")
    if parent_path:
        table.set_source_command(".config", ["-browse", parent_path])
    else:
        table.set_source_command(".config", [])
    current_display = (
        _mask_secret_display(current_value)
        if _is_sensitive_config_key(target_key)
        else (current_value if str(current_value or "").strip() else "(unset)")
    )
    table.set_header_lines([
        f"Choose a value for {display_name}. Current: {current_display}",
        "Use @N to apply a value. Use @.. to go back.",
    ])

    for idx, item in enumerate(items):
        row = table.add_row()
        row.add_column("Option", item.get("display", item.get("title", "")))
        table.set_row_selection_action(idx, [".config", str(item.get("key")), str(item.get("value"))])

    ctx.set_last_result_table(table, items)
    ctx.set_current_stage_table(table)
    return 0


def _show_value_edit_help(
    target_key: str,
    current_value: Any,
    display_name: str,
) -> int:
    current_display = (
        _mask_secret_display(current_value)
        if _is_sensitive_config_key(target_key)
        else _format_config_value(current_value)
    )
    parent_path = _parent_browse_path_for_key(target_key)
    items = [{
        "kind": "value",
        "key": target_key,
        "title": display_name,
        "name": target_key.split(".")[-1] if target_key else display_name,
        "value": "" if _is_sensitive_config_key(target_key) else current_value,
        "value_display": current_display,
        "display_path": display_name,
        "type": type(current_value).__name__ if current_value is not None else "unset",
        "choices": [],
    }]

    table = Table(f"Configuration: {display_name}", preserve_order=True)
    table.set_table("config")
    if parent_path:
        table.set_source_command(".config", ["-browse", parent_path])
    else:
        table.set_source_command(".config", [])
    table.set_header_lines([
        f"Free-text setting: {display_name}",
        f"Current value: {current_display if str(current_display or '').strip() else '(empty)'}",
        f"Set with: @1 | .config <new_value>   or   .config {target_key} <new_value>",
        "Use @.. to go back.",
    ])

    row = table.add_row()
    row.add_column("Name", display_name)
    row.add_column("Value", current_display)
    row.add_column("Key", target_key)

    ctx.set_last_result_table(table, items)
    ctx.set_current_stage_table(table)
    return 0


def _show_config_table(
    config_data: Dict[str, Any],
    *,
    browse_path: Optional[str] = None,
) -> int:
    items = _build_config_items(config_data, browse_path=browse_path)
    if not items:
        path_text = _format_config_path_label(browse_path)
        status_panel("Configuration", [("status", f"No configuration entries available for {path_text}.")])
        return 0

    table = Table(_build_config_table_title(browse_path), preserve_order=True)
    table.set_table("config")
    if browse_path:
        table.set_source_command(".config", ["-browse", str(browse_path)])
    else:
        table.set_source_command(".config", [])
    table.set_header_lines(_build_config_header_lines(browse_path))

    for idx, item in enumerate(items):
        row = table.add_row()
        row.add_column("Name", item.get("title", ""))
        value_text = item.get("summary") or item.get("value_display", "")
        choices = item.get("choices")
        if choices and isinstance(choices, list) and len(choices) > 0:
            choices_repr = ", ".join(str(c) for c in choices)
            value_text = f"{value_text}  [{choices_repr}]"
        row.add_column("Value", value_text)
        row.add_column("Type", item.get("type", ""))
        if item.get("kind") == "section" and item.get("browse_path"):
            table.set_row_selection_action(
                idx,
                [".config", "-browse", str(item.get("browse_path"))],
            )
        elif item.get("kind") == "value" and item.get("key"):
            table.set_row_selection_action(
                idx,
                [".config", "-choose", str(item.get("key"))],
            )
        elif item.get("kind") == "create_instance" and item.get("instance_target"):
            table.set_row_selection_action(
                idx,
                [".config", _CREATE_INSTANCE_FLAG, str(item.get("instance_target"))],
            )

    ctx.set_last_result_table(table, items)
    ctx.set_current_stage_table(table)
    return 0


def _save_updated_config(config_data: Dict[str, Any], key_path: str) -> None:
    try:
        key_l = str(key_path or "").lower()
    except Exception:
        key_l = ""
    if "alldebrid" in key_l or "all-debrid" in key_l:
        save_config_and_verify(config_data)
        return
    save_config(config_data)


def _resolve_direct_browse_path(
    config_data: Dict[str, Any],
    token: str,
) -> Optional[str]:
    text = str(token or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in {"preferences", "prefs"}:
        return _PREFERENCES_BROWSE_PATH
    if lowered in {"appearance", "theme", "display"}:
        return _APPEARANCE_BROWSE_PATH
    if lowered in {"plugins", "plugin"}:
        return _PLUGINS_BROWSE_PATH

    plugin_branch = _resolve_plugin_branch(config_data, "plugin", lowered)
    if plugin_branch is not None:
        return f"plugin.{plugin_branch[0]}"

    branch = _resolve_config_branch(config_data, text)
    if isinstance(branch, dict):
        return text
    return None


def _strip_value_quotes(value: str) -> str:
    if not value:
        return value
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def _plugins_dir() -> Path:
    try:
        from PluginCore.source import install_plugins_dir

        return install_plugins_dir()
    except Exception:
        return Path.cwd() / "plugins"


def _pick_open_file(title: str, filters: List[Tuple[str, str]], ps_filter: str) -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.wm_attributes("-topmost", True)
        except Exception:
            pass
        path = filedialog.askopenfilename(title=title, filetypes=filters or [("All files", "*.*")])
        try:
            root.destroy()
        except Exception:
            pass
        return str(path or "").strip()
    except Exception:
        pass
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-STA",
                "-NoProfile",
                "-Command",
                (
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "$d = New-Object System.Windows.Forms.OpenFileDialog; "
                    f"$d.Title = {json.dumps(title)}; "
                    f"$d.Filter = {json.dumps(ps_filter)}; "
                    "$d.CheckFileExists = $true; "
                    "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $d.FileName }"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(completed.stdout or "").strip()
    except Exception:
        return ""


def _plugin_upload_rules(plugin_name: str) -> List[Dict[str, Any]]:
    try:
        from PluginCore.registry import get_plugin_class

        cls = get_plugin_class(plugin_name)
    except Exception:
        cls = None
    raw = getattr(cls, "PLUGIN_UPLOADS", ()) if cls is not None else ()
    rules: List[Dict[str, Any]] = []
    for entry in raw or ():
        if isinstance(entry, dict) and str(entry.get("dest") or "").strip():
            rules.append(entry)
    return rules


def _upload_rule_matches(rule: Dict[str, Any], source: Path) -> bool:
    name = source.name.lower()
    suffixes = tuple(str(s).lower() for s in (rule.get("suffixes") or ()))
    needles = tuple(str(n).lower() for n in (rule.get("needles") or ()))
    if suffixes and source.suffix.lower() not in suffixes:
        return False
    if needles and not any(needle in name for needle in needles):
        return False
    return bool(suffixes or needles or True)


def _dest_for_upload(plugin_name: str, source: Path) -> Optional[Path]:
    plugin = str(plugin_name or "").strip().lower()
    if not plugin:
        return None
    for rule in _plugin_upload_rules(plugin):
        if not _upload_rule_matches(rule, source):
            continue
        dest_name = str(rule.get("dest") or "").strip()
        if dest_name:
            return _plugins_dir() / plugin / dest_name
    return None


def _plugin_name_from_context(piped_result: Any, extra_token: str) -> str:
    token = str(extra_token or "").strip().lower()
    if token and not Path(extra_token).expanduser().exists():
        try:
            from PluginCore.registry import get_plugin_class

            if get_plugin_class(token) is not None:
                return token
        except Exception:
            pass
    item = _normalize_config_item(piped_result) or {}
    for key in ("plugin", "table", "name"):
        value = str(item.get(key) or "").strip().lower()
        if value:
            return value
    browse = str(item.get("browse_path") or item.get("key") or "").strip()
    parts = _split_config_path(browse)
    if len(parts) >= 2 and parts[0] == "plugin":
        return parts[1]
    getter = getattr(piped_result, "get_column", None)
    if callable(getter):
        for key in ("Plugin", "plugin", "Name"):
            value = str(getter(key) or "").strip().lower()
            if value:
                return value
    current = _current_config_browse_path() or ""
    parts = _split_config_path(current)
    if len(parts) >= 2 and parts[0] == "plugin":
        return parts[1]
    return ""


def _match_upload_plugin(source: Path, preferred: str) -> str:
    if preferred and _dest_for_upload(preferred, source) is not None:
        return preferred
    names: List[str] = []
    root = _plugins_dir()
    if root.is_dir():
        names = [p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    for name in names:
        if _dest_for_upload(name, source) is not None:
            return str(name)
    return preferred


def _run_upload(piped_result: Any, args: List[str]) -> int:
    tokens = [str(arg).strip() for arg in (args or []) if str(arg).strip()]
    rest: List[str] = []
    for idx, token in enumerate(tokens):
        if token.lower() in {"-upload", "--upload"}:
            rest = tokens[idx + 1 :]
            break
    extra_token = rest[0] if rest else ""
    source_text = ""
    if extra_token and Path(extra_token).expanduser().is_file():
        source_text = extra_token
        extra_token = ""
    elif len(rest) > 1:
        source_text = " ".join(rest[1:]).strip().strip('"')

    if not source_text:
        piped_path = _extract_piped_value(piped_result)
        if piped_path and Path(str(piped_path)).expanduser().is_file():
            source_text = str(piped_path)

    if not source_text:
        source_text = _pick_open_file(
            "Upload file",
            [("All files", "*.*")],
            "All files (*.*)|*.*",
        )
    if not source_text:
        status_panel("Upload", [("status", "cancelled")])
        return 0
    source = Path(source_text).expanduser()
    if not source.is_file():
        status_panel("Upload", [("error", f"File not found: {source}")])
        return 1

    plugin_name = _match_upload_plugin(source, _plugin_name_from_context(piped_result, extra_token))
    dest = _dest_for_upload(plugin_name, source)
    if dest is None:
        status_panel("Upload", [("error", "No plugin accepted this file")])
        return 1
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        if dest.name.lower() in {"cookies.txt", "cookies"}:
            try:
                current = load_config() or {}
                set_nested_config_value(current, f"plugin.{plugin_name}.cookie_file", str(dest))
                save_config(current)
            except Exception:
                pass
        status_panel(
            "Upload",
            [
                ("plugin", plugin_name),
                ("from", str(source)),
                ("to", str(dest)),
            ],
        )
        return 0
    except Exception as exc:
        status_panel("Upload", [("error", str(exc))])
        return 1


def _run(piped_result: Any, args: List[str], config: Dict[str, Any]) -> int:
    import sys

    if _has_flag(args, "-log") or _has_flag(args, "--log"):
        return _show_config_logs(args)

    if _has_flag(args, "-upload") or _has_flag(args, "--upload"):
        return _run_upload(piped_result, args)

    # Load configuration from the database
    current_config = load_config()

    browse_path = _extract_browse_arg(args)
    if browse_path:
        return _show_config_table(current_config, browse_path=browse_path)

    choose_key = _extract_choose_arg(args)
    if choose_key:
        nested_branch = _resolve_config_branch(current_config, choose_key)
        if isinstance(nested_branch, dict):
            return _show_config_table(current_config, browse_path=choose_key)

        choices = _lookup_config_choices(choose_key)
        try:
            raw_current = _resolve_config_value_raw(current_config, choose_key)
        except Exception:
            raw_current = None
        if raw_current is None and len(_split_config_path(choose_key)) <= 1:
            field = _global_schema_field(choose_key)
            if field is not None:
                raw_current = field.get("default")

        display_name = _value_item_title(choose_key, choose_key.split(".")[-1])
        if choices:
            return _show_choice_table(
                choose_key,
                choices,
                _format_current_choice_value(raw_current),
                display_name,
            )
        return _show_value_edit_help(choose_key, raw_current, display_name)

    selection_item = _get_selected_config_item() or _normalize_config_item(piped_result)

    delete_target = _resolve_delete_target(args=args, selection_item=selection_item)
    if delete_target is not None:
        if not str(delete_target).strip():
            status_panel(
                "Configuration",
                [
                    (
                        "status",
                        "Select a plugin instance row, then run @N | .config -delete "
                        "(or .config -delete plugin.<name>.<instance>).",
                    )
                ],
            )
            return 0
        try:
            deleted_name, parent_path = _delete_plugin_instance(current_config, delete_target)
            _save_updated_config(current_config, delete_target)
            current_config = load_config()
            status_panel(
                "Configuration",
                [
                    ("deleted", str(deleted_name)),
                    ("parent", _format_config_path_label(parent_path)),
                ],
            )
            return _show_config_table(current_config, browse_path=parent_path)
        except Exception as exc:
            log(f"Error deleting config instance '{delete_target}': {exc}")
            status_panel("config", [("error", str(exc))])
            return 1

    create_instance_target = _extract_create_instance_target(args)
    if create_instance_target:
        status_panel(
            "Configuration",
            [
                (
                    "status",
                    f"Use @N | .config <instance_name> to create a new instance under "
                    f"'{_format_config_path_label(create_instance_target)}', then set its "
                    "fields in the table that opens.",
                )
            ],
        )
        return 0

    value_from_pipe = _extract_piped_value(piped_result)
    selection_kind = str((selection_item or {}).get("kind") or "").strip().lower()
    selection_key = str((selection_item or {}).get("key") or "").strip() or None
    selection_browse_path = str((selection_item or {}).get("browse_path") or "").strip() or None
    selection_display_path = str((selection_item or {}).get("display_path") or selection_key or "").strip() or selection_key
    selection_instance_target = str((selection_item or {}).get("instance_target") or "").strip() or None

    if selection_kind == "section" and selection_browse_path and not args and value_from_pipe is None:
        return _show_config_table(current_config, browse_path=selection_browse_path)

    if selection_kind == "create_instance" and selection_instance_target:
        new_instance_name = value_from_pipe or _extract_selected_update_value(args)
        if new_instance_name is None:
            status_panel(
                "Configuration",
                [
                    (
                        "status",
                        f"Use @N | .config <instance_name> to create a new instance under "
                        f"'{_format_config_path_label(selection_instance_target)}', then set "
                        "its fields in the table that opens.",
                    )
                ],
            )
            return 0
        new_instance_name = _strip_value_quotes(new_instance_name)
        try:
            new_browse_path, created = _create_or_get_plugin_instance(
                current_config,
                selection_instance_target,
                new_instance_name,
            )
            _save_updated_config(current_config, new_browse_path)
            # Re-load so browse uses the canonicalized multi-instance tree.
            current_config = load_config()
            # Resolve actual instance key casing after normalize/save.
            path_parts = _split_config_path(new_browse_path)
            if len(path_parts) >= 3:
                plugin_block = (
                    (current_config.get(path_parts[0]) or {}).get(path_parts[1])
                    if isinstance(current_config.get(path_parts[0]), dict)
                    else None
                )
                if isinstance(plugin_block, dict):
                    actual_key, _ = _dict_get_ci(plugin_block, path_parts[2])
                    if actual_key:
                        new_browse_path = ".".join([path_parts[0], path_parts[1], actual_key])
            status_text = "Created" if created else "Using existing"
            status_panel(
                "Configuration",
                [
                    ("status", status_text),
                    ("instance", str(new_instance_name)),
                    ("path", _format_config_path_label(new_browse_path)),
                    ("note", "Configure its fields in the table below."),
                ],
            )
            return _show_config_table(current_config, browse_path=new_browse_path)
        except Exception as exc:
            log(f"Error creating config instance '{selection_instance_target}': {exc}")
            status_panel("config", [("error", str(exc))])
            return 1

    if selection_kind == "value" and selection_key:
        new_value = value_from_pipe or _extract_selected_update_value(args)
        if new_value is not None:
            new_value = _strip_value_quotes(new_value)
            target_key = _resolve_update_key(current_config, selection_key)
            try:
                set_nested_config(current_config, target_key, new_value)
                _save_updated_config(current_config, target_key)
                status_panel("Configuration", [("updated", selection_display_path), ("value", str(new_value))])
                current_config = load_config()
                parent_path = _parent_browse_path_for_key(target_key)
                if parent_path and _resolve_config_branch(current_config, parent_path) is not None:
                    return _show_config_table(current_config, browse_path=parent_path)
                return 0
            except Exception as exc:
                log(f"Error updating config '{target_key}': {exc}")
                status_panel("config", [("error", str(exc))])
                return 1
        choices = _normalize_choice_list((selection_item or {}).get("choices")) or _lookup_config_choices(selection_key)
        current_raw = (selection_item or {}).get("value")
        display_name = selection_display_path or _value_item_title(selection_key, selection_key.split(".")[-1])
        if choices:
            return _show_choice_table(
                selection_key,
                choices,
                _format_current_choice_value(current_raw),
                display_name,
            )
        return _show_value_edit_help(selection_key, current_raw, display_name)

    if selection_kind == "choice" and selection_key:
        choice_value = str((selection_item or {}).get("value") or "").strip()
        if choice_value:
            try:
                target_key = _resolve_update_key(current_config, selection_key)
                set_nested_config(current_config, target_key, choice_value)
                _save_updated_config(current_config, target_key)
                status_panel("Configuration", [("updated", _format_config_path_label(selection_key)), ("value", choice_value)])
                current_config = load_config()
                parent_path = _parent_browse_path_for_key(selection_key)
                if parent_path and _resolve_config_branch(current_config, parent_path) is not None:
                    return _show_config_table(current_config, browse_path=parent_path)
                return 0
            except Exception as exc:
                log(f"Error updating config '{selection_key}': {exc}")
                status_panel("config", [("error", str(exc))])
                return 1

    if not args:
        return _show_config_table(current_config)

    key = args[0]
    if len(args) < 2:
        browse_target = _resolve_direct_browse_path(current_config, key)
        if browse_target:
            return _show_config_table(current_config, browse_path=browse_target)
        status_panel("Configuration", [("error", f"Value required for key '{key}'")])
        return 1

    value = _strip_value_quotes(" ".join(args[1:]))
    try:
        set_nested_config(current_config, key, value)
        _save_updated_config(current_config, key)
        status_panel("Configuration", [("updated", key), ("value", value)])
        current_config = load_config()
        parent_path = _parent_browse_path_for_key(key)
        if parent_path and _resolve_config_branch(current_config, parent_path) is None:
            parent_path = _resolve_direct_browse_path(current_config, key)
        if parent_path and _resolve_config_branch(current_config, parent_path) is not None:
            return _show_config_table(current_config, browse_path=parent_path)
        return 0
    except Exception as exc:
        log(f"Error updating config '{key}': {exc}")
        status_panel("config", [("error", str(exc))])
        return 1


CMDLET.exec = _run
