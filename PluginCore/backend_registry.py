"""Configured plugin-backed backend registry.

Backends are discovered from their owning plugins and instantiated from config.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import re
from typing import Any, Dict, Optional, Type

from SYS.logger import debug
from SYS.utils import expand_path

from PluginCore.backend_base import BackendBase

_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_PLUGIN_DISCOVERED_CLASSES_CACHE: Dict[str, Optional[Type[BackendBase]]] = {}

# Backends that failed to initialize earlier in the current process.
# Keyed by (backend_type, instance_key) where instance_key is the configured name
# under config.plugin.<type>.<instance_key>.
_FAILED_BACKEND_CACHE: Dict[tuple[str, str], str] = {}


def _normalize_backend_type(value: str) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _normalize_config_key(value: str) -> str:
    return str(value or "").strip().upper()


def _get_case_insensitive(mapping: Dict[str, Any], key: str) -> Any:
    if key in mapping:
        return mapping[key]
    desired = _normalize_config_key(key)
    for current_key, value in mapping.items():
        if _normalize_config_key(current_key) == desired:
            return value
    return None


def _extract_backend_classes(owner: Any) -> Dict[str, Type[BackendBase]]:
    discovered: Dict[str, Type[BackendBase]] = {}

    def _add_candidate(key: Any, candidate: Any) -> None:
        if not inspect.isclass(candidate):
            return
        if candidate is BackendBase:
            return
        if not issubclass(candidate, BackendBase):
            return
        normalized = _normalize_backend_type(str(key or candidate.__name__))
        if normalized:
            discovered[normalized] = candidate

    if owner is None:
        return discovered

    if inspect.isclass(owner):
        _add_candidate(None, owner)
        return discovered

    if isinstance(owner, dict):
        for key, candidate in owner.items():
            _add_candidate(key, candidate)
        return discovered

    if isinstance(owner, (list, tuple, set, frozenset)):
        for candidate in owner:
            _add_candidate(None, candidate)
        return discovered

    try:
        for key, candidate in vars(owner).items():
            _add_candidate(key, candidate)
    except Exception:
        pass
    return discovered


def _discover_plugin_backend_class(backend_type: str) -> Optional[Type[BackendBase]]:
    normalized = _normalize_backend_type(backend_type)
    if not normalized:
        return None

    cached = _PLUGIN_DISCOVERED_CLASSES_CACHE.get(normalized, None)
    if normalized in _PLUGIN_DISCOVERED_CLASSES_CACHE:
        return cached

    try:
        plugin_module = importlib.import_module(f"plugins.{normalized}")
    except Exception:
        _PLUGIN_DISCOVERED_CLASSES_CACHE[normalized] = None
        return None

    discovered: Dict[str, Type[BackendBase]] = {}

    backend_hook = getattr(plugin_module, "get_store_backend_classes", None)
    if callable(backend_hook):
        try:
            discovered.update(_extract_backend_classes(backend_hook()))
        except Exception as exc:
            debug(f"[BackendRegistry] Failed to load plugin backends for '{normalized}': {exc}")

    discovered.update(_extract_backend_classes(getattr(plugin_module, "STORE_BACKENDS", None)))

    if normalized not in discovered:
        discovered.update(_extract_backend_classes(plugin_module))

    resolved = discovered.get(normalized)
    if resolved is None and len(discovered) == 1:
        resolved = next(iter(discovered.values()))

    _PLUGIN_DISCOVERED_CLASSES_CACHE[normalized] = resolved
    return resolved


def _plugin_module_exists(plugin_name: str) -> bool:
    normalized = _normalize_backend_type(plugin_name)
    if not normalized:
        return False
    try:
        return importlib.util.find_spec(f"plugins.{normalized}") is not None
    except Exception:
        return False


def _resolve_backend_class(backend_type: str) -> Optional[Type[BackendBase]]:
    normalized = _normalize_backend_type(backend_type)
    if not normalized:
        return None
    return _discover_plugin_backend_class(normalized)


def _required_keys_for(backend_cls: Type[BackendBase]) -> list[str]:
    if hasattr(backend_cls, "config_schema") and callable(backend_cls.config_schema):
        try:
            schema = backend_cls.config_schema()
            keys = []
            if isinstance(schema, list):
                for field in schema:
                    if isinstance(field, dict) and field.get("required"):
                        key = field.get("key")
                        if key:
                            keys.append(str(key))
            if keys:
                return keys
        except Exception:
            pass
    return []


_PROVIDER_ONLY_BACKEND_NAMES = frozenset(("debrid", "alldebrid"))


def _build_kwargs(backend_cls: Type[BackendBase], instance_name: str, instance_config: Any) -> Dict[str, Any]:
    cfg_dict = dict(instance_config) if isinstance(instance_config, dict) else {}

    # Multi-instance plugins use the config key as the instance name. NAME may be
    # omitted from the UI schema, but backends still require it at construction.
    if _get_case_insensitive(cfg_dict, "NAME") in (None, ""):
        cfg_dict["NAME"] = str(instance_name)

    required = _required_keys_for(backend_cls)
    keys: list[str] = list(required)
    if not any(_normalize_config_key(key) == "NAME" for key in keys):
        keys.append("NAME")

    kwargs: Dict[str, Any] = {}
    missing: list[str] = []
    required_norm = {_normalize_config_key(key) for key in required}
    for key in keys:
        value = _get_case_insensitive(cfg_dict, key)
        key_norm = _normalize_config_key(key)
        if value is None or value == "":
            if key_norm == "NAME":
                value = str(instance_name)
            else:
                if key_norm in required_norm:
                    missing.append(str(key))
                continue
        # Prefer canonical NAME/URL/API casing when present on the constructor.
        kwargs_key = str(key)
        if key_norm == "NAME":
            kwargs_key = "NAME"
        kwargs[kwargs_key] = value

    if missing:
        raise ValueError(
            f"Missing required keys for {backend_cls.__name__}: {', '.join(missing)}"
        )

    return kwargs


class BackendRegistry:

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        suppress_debug: bool = False,
    ) -> None:
        self._config = config or {}
        self._suppress_debug = suppress_debug
        self._backends: Dict[str, BackendBase] = {}
        self._backend_errors: Dict[str, str] = {}
        self._backend_types: Dict[str, str] = {}
        self._load_backends()

    def _load_backends(self) -> None:
        plugin_cfg = self._config.get("plugin")
        if not isinstance(plugin_cfg, dict):
            plugin_cfg = {}

        self._backend_types = {}
        for raw_backend_type, instances in plugin_cfg.items():
            if not isinstance(instances, dict):
                continue

            backend_type = _normalize_backend_type(str(raw_backend_type))
            if backend_type == "folder":
                continue
            backend_cls = _resolve_backend_class(backend_type)
            if backend_cls is None:
                if (
                    backend_type not in _PROVIDER_ONLY_BACKEND_NAMES
                    and not self._suppress_debug
                    and not _plugin_module_exists(backend_type)
                ):
                    debug(f"[BackendRegistry] Unknown backend type '{raw_backend_type}'")
                continue

            for instance_name, instance_config in instances.items():
                backend_name = str(instance_name)
                cache_key = (backend_type, str(instance_name))

                try:
                    kwargs = _build_kwargs(
                        backend_cls,
                        str(instance_name),
                        instance_config,
                    )

                    for key in list(kwargs.keys()):
                        if _normalize_config_key(key) in {"PATH", "LOCATION"}:
                            kwargs[key] = str(expand_path(kwargs[key]))

                    backend = backend_cls(**kwargs)

                    backend_name = str(kwargs.get("NAME") or instance_name)
                    self._backends[backend_name] = backend
                    self._backend_types[backend_name] = backend_type
                    _FAILED_BACKEND_CACHE.pop(cache_key, None)
                except Exception as exc:
                    err_text = str(exc)
                    self._backend_errors[str(instance_name)] = err_text
                    if isinstance(instance_config, dict):
                        override_name = _get_case_insensitive(dict(instance_config), "NAME")
                        if override_name:
                            self._backend_errors[str(override_name)] = err_text
                    _FAILED_BACKEND_CACHE[cache_key] = err_text
                    if not self._suppress_debug:
                        debug(
                            f"[BackendRegistry] Failed to register {backend_cls.__name__} instance '{instance_name}': {exc}"
                        )

    def _resolve_backend_name(self, backend_name: str) -> tuple[Optional[str], Optional[str]]:
        requested = str(backend_name or "")
        if requested in self._backends:
            return requested, None

        requested_norm = _normalize_backend_type(requested)

        ci_matches = [
            name for name in self._backends
            if _normalize_backend_type(name) == requested_norm
        ]
        if len(ci_matches) == 1:
            return ci_matches[0], None
        if len(ci_matches) > 1:
            return None, f"Ambiguous backend alias '{backend_name}' matches {ci_matches}"

        type_matches = [
            name for name, backend_type in self._backend_types.items()
            if backend_type == requested_norm
        ]
        if len(type_matches) == 1:
            return type_matches[0], None
        if len(type_matches) > 1:
            return None, (
                f"Ambiguous backend alias '{backend_name}' matches type '{requested_norm}': {type_matches}"
            )

        prefix_matches = [
            name for name, backend_type in self._backend_types.items()
            if backend_type.startswith(requested_norm)
        ]
        if len(prefix_matches) == 1:
            return prefix_matches[0], None
        if len(prefix_matches) > 1:
            return None, (
                f"Ambiguous backend alias '{backend_name}' matches type prefix '{requested_norm}': {prefix_matches}"
            )

        return None, None

    def get_backend_error(self, backend_name: str) -> Optional[str]:
        return self._backend_errors.get(str(backend_name))

    def list_backends(self) -> list[str]:
        return sorted(self._backends.keys())

    def list_searchable_backends(self) -> list[str]:
        def _rank(name: str) -> int:
            normalized_name = str(name or "").strip().lower()
            if normalized_name == "temp":
                return 0
            if normalized_name == "default":
                return 2
            return 1

        chosen: Dict[int, str] = {}
        for name, backend in self._backends.items():
            if type(backend).search is BackendBase.search:
                continue
            key = id(backend)
            prev = chosen.get(key)
            if prev is None or _rank(name) < _rank(prev):
                chosen[key] = name
        return sorted(chosen.values())

    def __getitem__(self, backend_name: str) -> BackendBase:
        resolved, err = self._resolve_backend_name(backend_name)
        if resolved:
            return self._backends[resolved]
        if err:
            raise KeyError(f"Unknown backend: {backend_name}. {err}")
        raise KeyError(f"Unknown backend: {backend_name}. Available: {list(self._backends.keys())}")

    def is_available(self, backend_name: str) -> bool:
        resolved, _err = self._resolve_backend_name(backend_name)
        return resolved is not None

    def try_add_url_for_pipe_object(self, pipe_obj: Any, url: str) -> bool:
        """Best-effort helper: if `pipe_obj` contains `store` + `hash`, add `url` to that backend."""
        try:
            url_text = str(url or "").strip()
            if not url_text:
                return False

            store_name = None
            file_hash = None
            if isinstance(pipe_obj, dict):
                store_name = pipe_obj.get("store")
                file_hash = pipe_obj.get("hash")
            else:
                store_name = getattr(pipe_obj, "store", None)
                file_hash = getattr(pipe_obj, "hash", None)

            store_name = str(store_name).strip() if store_name is not None else ""
            file_hash = str(file_hash).strip() if file_hash is not None else ""
            if not store_name or not file_hash:
                return False

            if not _SHA256_HEX_RE.fullmatch(file_hash):
                return False

            backend = self[store_name]
            add_url = getattr(backend, "add_url", None)
            if not callable(add_url):
                return False

            ok = add_url(file_hash.lower(), [url_text])
            return bool(ok) if ok is not None else True
        except Exception:
            return False


def list_configured_backend_names(config: Optional[Dict[str, Any]]) -> list[str]:
    """Return configured backend instance names without instantiating backends."""
    try:
        names: list[str] = []
        for section_name in ("plugin",):
            section_cfg = (config or {}).get(section_name) or {}
            if not isinstance(section_cfg, dict):
                continue

            for raw_backend_type, instances in section_cfg.items():
                if not isinstance(instances, dict):
                    continue
                backend_type = _normalize_backend_type(str(raw_backend_type))
                if backend_type == "folder" or backend_type in _PROVIDER_ONLY_BACKEND_NAMES:
                    continue

                backend_cls = _resolve_backend_class(backend_type)
                if backend_cls is None:
                    continue

                for instance_name, instance_config in instances.items():
                    try:
                        _build_kwargs(backend_cls, str(instance_name), instance_config)
                    except Exception:
                        continue
                    if isinstance(instance_config, dict):
                        override_name = _get_case_insensitive(dict(instance_config), "NAME")
                        if override_name:
                            names.append(str(override_name))
                        else:
                            names.append(str(instance_name))
                    else:
                        names.append(str(instance_name))

        return sorted(set(names))
    except Exception:
        return []


def get_backend_instance(
    config: Optional[Dict[str, Any]],
    backend_name: str,
    *,
    suppress_debug: bool = False,
) -> Optional[BackendBase]:
    """Instantiate and return a single configured backend by name."""
    if not backend_name:
        return None
    desired = str(backend_name or "").strip().lower()

    for section_name in ("plugin",):
        section_cfg = (config or {}).get(section_name) or {}
        if not isinstance(section_cfg, dict):
            continue

        for raw_backend_type, instances in section_cfg.items():
            if not isinstance(instances, dict):
                continue
            backend_type = _normalize_backend_type(str(raw_backend_type))
            backend_cls = _resolve_backend_class(backend_type)
            if backend_cls is None:
                continue

            for instance_name, instance_cfg in instances.items():
                candidate_alias = None
                if isinstance(instance_cfg, dict):
                    candidate_alias = instance_cfg.get("NAME") or instance_cfg.get("name")
                candidate_alias = str(candidate_alias or instance_name).strip()
                if candidate_alias.lower() != desired:
                    continue
                try:
                    kwargs = _build_kwargs(backend_cls, str(instance_name), instance_cfg)
                except Exception as exc:
                    if not suppress_debug:
                        debug(
                            f"[BackendRegistry] Can't build kwargs for '{instance_name}' ({backend_type}/{section_name}): {exc}"
                        )
                    return None
                try:
                    for key in list(kwargs.keys()):
                        if _normalize_config_key(key) in {"PATH", "LOCATION"}:
                            kwargs[key] = str(expand_path(kwargs[key]))
                except Exception:
                    pass
                try:
                    return backend_cls(**kwargs)
                except Exception as exc:
                    if not suppress_debug:
                        debug(f"[BackendRegistry] Failed to instantiate backend '{candidate_alias}': {exc}")
                    return None

            for instance_name, instance_cfg in instances.items():
                try:
                    kwargs = _build_kwargs(backend_cls, str(instance_name), instance_cfg)
                except Exception:
                    continue
                alias = str(kwargs.get("NAME") or instance_name).strip()
                if alias.lower() != desired:
                    continue
                try:
                    for key in list(kwargs.keys()):
                        if _normalize_config_key(key) in {"PATH", "LOCATION"}:
                            kwargs[key] = str(expand_path(kwargs[key]))
                except Exception:
                    pass
                try:
                    return backend_cls(**kwargs)
                except Exception as exc:
                    if not suppress_debug:
                        debug(f"[BackendRegistry] Failed to instantiate backend '{alias}': {exc}")
                    return None

    if not suppress_debug:
        debug(f"[BackendRegistry] Backend '{backend_name}' not found in config")
    return None


__all__ = [
    "BackendBase",
    "BackendRegistry",
    "get_backend_instance",
    "list_configured_backend_names",
]