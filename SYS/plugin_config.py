from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from SYS.config import global_config
from PluginCore.registry import get_plugin_class

logger = logging.getLogger(__name__)


ConfigField = Dict[str, Any]

_UPLOAD_SIZE_LIMIT_FIELD: ConfigField = {
    "key": "size_limit",
    "label": "Max upload size (MB)",
    "default": "",
    "required": False,
    "help": "Skip files larger than this for file -add. Leave blank for no limit.",
}


def _import_plugin_support_module(plugin_name: str) -> Optional[Any]:
    from PluginCore.registry import import_plugin_module

    normalized = str(plugin_name or "").strip()
    if not normalized:
        return None
    module = import_plugin_module(normalized)
    if module is not None:
        return module
    compact = normalized.replace(".", "")
    if compact and compact != normalized:
        return import_plugin_module(compact)
    return None


def _iter_plugin_module_names() -> List[str]:
    from pathlib import Path

    names: List[str] = []
    seen: set[str] = set()
    roots = [Path(__file__).resolve().parents[1] / "plugins"]
    try:
        roots.append(Path.cwd() / "plugins")
    except Exception:
        pass
    for plugin_dir in roots:
        try:
            if not plugin_dir.is_dir():
                continue
            for child in plugin_dir.iterdir():
                name = str(child.name or "").strip()
                if not name or name.startswith("_") or name in seen:
                    continue
                if child.is_dir() and (child / "__init__.py").exists():
                    seen.add(name)
                    names.append(name)
                elif child.is_file() and child.suffix == ".py" and child.stem != "__init__":
                    seen.add(child.stem)
                    names.append(child.stem)
        except Exception:
            continue
    return names


def _normalize_schema(fields: Optional[Iterable[Any]]) -> List[ConfigField]:
    normalized: List[ConfigField] = []
    seen: set[str] = set()
    for raw_field in list(fields or []):
        if not isinstance(raw_field, dict):
            continue
        key = str(raw_field.get("key") or "").strip()
        if not key:
            continue
        key_upper = key.upper()
        if key_upper in seen:
            continue
        seen.add(key_upper)

        field = dict(raw_field)
        field["key"] = key
        if "label" in field and field.get("label") is not None:
            field["label"] = str(field.get("label") or "")
        choices = field.get("choices")
        if choices is not None and not isinstance(choices, (list, tuple)):
            field["choices"] = [choices]
        elif isinstance(choices, tuple):
            field["choices"] = list(choices)
        normalized.append(field)
    return normalized


def _call_schema(owner: Any, label: str) -> List[ConfigField]:
    schema_fn = getattr(owner, "config_schema", None)
    if not callable(schema_fn):
        return []
    try:
        return _normalize_schema(schema_fn())
    except Exception:
        logger.exception("Failed to load config schema for %s", label)
        return []


def get_store_schema(store_type: str) -> List[ConfigField]:
    """Return config schema for a store type.

    Store types are now plugins. We look up the plugin schema by name; if not
    found we return an empty list.
    """
    return get_plugin_schema(str(store_type or "").strip())


def get_plugin_schema(plugin_name: str) -> List[ConfigField]:
    normalized_name = str(plugin_name or "").strip()
    if not normalized_name:
        return []

    plugin_class = get_plugin_class(normalized_name)
    if plugin_class is not None:
        schema = list(_call_schema(plugin_class, f"plugin '{normalized_name}'") or [])
        try:
            if plugin_class.supports_add_file():
                keys = {
                    str(field.get("key") or "").strip().lower()
                    for field in schema
                    if isinstance(field, dict)
                }
                if "size_limit" not in keys:
                    schema.append(_normalize_schema([_UPLOAD_SIZE_LIMIT_FIELD])[0])
        except Exception:
            pass
        if schema:
            return schema

    module = _import_plugin_support_module(normalized_name)
    if module is None:
        return []
    return _call_schema(module, f"plugin support '{normalized_name}'")


def get_item_schema(item_type: str, item_name: str) -> List[ConfigField]:
    normalized_type = str(item_type or "").strip()
    normalized_name = str(item_name or "").strip()
    if normalized_type.startswith("plugin-"):
        # Multi-instance plugin: plugin-{ptype}; item_name is the instance name
        ptype = normalized_type[len("plugin-"):]
        return get_plugin_schema(ptype)
    if normalized_type == "plugin":
        return get_plugin_schema(normalized_name)
    return []


def get_item_schema_map(item_type: str, item_name: str) -> Dict[str, ConfigField]:
    return {field["key"].upper(): field for field in get_item_schema(item_type, item_name)}


def get_global_schema() -> List[ConfigField]:
    return _normalize_schema(global_config())


def get_global_schema_map() -> Dict[str, ConfigField]:
    return {field["key"].upper(): field for field in get_global_schema()}


def build_default_store_config(store_type: str, instance_name: str) -> Dict[str, Any]:
    """Build a default config dict for a new store/multi-instance plugin entry."""
    config: Dict[str, Any] = {"NAME": str(instance_name or "").strip()}
    schema = get_store_schema(store_type) or get_plugin_schema(store_type)
    for field in schema:
        key = str(field.get("key") or "").strip()
        if not key:
            continue
        if key.upper() == "NAME":
            continue
        config[key] = field.get("default", "")
    return config


def build_default_plugin_config(plugin_name: str) -> Dict[str, Any]:
    config: Dict[str, Any] = {}
    schema = get_plugin_schema(plugin_name)
    if schema:
        for field in schema:
            config[field["key"]] = field.get("default", "")
        return config

    plugin_class = get_plugin_class(str(plugin_name or "").strip())
    if plugin_class is None:
        return config
    try:
        for required_key in plugin_class.required_config_keys():
            config[str(required_key)] = ""
    except Exception:
        logger.exception("Failed to load legacy required config keys for plugin '%s'", plugin_name)
    return config


def get_required_config_keys(item_type: str, item_name: str) -> List[str]:
    normalized_type = str(item_type or "").strip()
    normalized_name = str(item_name or "").strip()
    required_keys: List[str] = []
    seen: set[str] = set()

    def _add_key(value: Any) -> None:
        key = str(value or "").strip()
        if not key:
            return
        key_upper = key.upper()
        if key_upper in seen:
            return
        seen.add(key_upper)
        required_keys.append(key)

    for field in get_item_schema(normalized_type, normalized_name):
        if field.get("required"):
            _add_key(field.get("key"))

    if normalized_type.startswith("plugin-"):
        # Multi-instance plugin (plugin-{ptype}): look up by plugin name.
        ptype = normalized_type.replace("plugin-", "", 1)
        plugin_class = get_plugin_class(ptype)
        if plugin_class is not None:
            try:
                for required_key in plugin_class.required_config_keys():
                    _add_key(required_key)
            except Exception:
                logger.exception("Failed to load required config keys for plugin '%s'", ptype)
    elif normalized_type == "plugin":
        plugin_class = get_plugin_class(normalized_name)
        if plugin_class is not None:
            try:
                for required_key in plugin_class.required_config_keys():
                    _add_key(required_key)
            except Exception:
                logger.exception("Failed to load required config keys for plugin '%s'", normalized_name)

    return required_keys


def get_configurable_store_types() -> List[str]:
    """Return configurable multi-instance plugin types (formerly 'store types')."""
    from PluginCore.registry import REGISTRY
    options: List[str] = []
    for info in REGISTRY.iter_plugins():
        plugin_cls = info.plugin_class
        if getattr(plugin_cls, 'MULTI_INSTANCE', False) and get_plugin_schema(info.canonical_name):
            options.append(info.canonical_name)
    return sorted(set(options))


def get_configurable_plugin_types() -> List[str]:
    """Return all plugin types that can be configured: those with a schema or MULTI_INSTANCE flag."""
    from PluginCore.registry import REGISTRY
    options: List[str] = []
    registered_names: set[str] = set()
    for info in REGISTRY.iter_plugins():
        registered_names.add(info.canonical_name)
        registered_names.update(str(alias).strip().lower() for alias in (info.alias_names or ()) if str(alias).strip())
        plugin_cls = info.plugin_class
        if get_plugin_schema(info.canonical_name) or getattr(plugin_cls, 'MULTI_INSTANCE', False):
            options.append(info.canonical_name)
    for module_name in _iter_plugin_module_names():
        if module_name in registered_names:
            continue
        if get_plugin_schema(module_name):
            options.append(module_name)
    return sorted(set(options))