"""Plugin registry.

Bundled plugins live in the ``plugins`` package. Additional drop-in plugins can
be discovered from external plugin directories configured via the environment or
current working directory.
"""

from __future__ import annotations

from functools import lru_cache
import hashlib
import importlib
import importlib.util
import os
import pkgutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Type
from urllib.parse import urlparse

from SYS.logger import log, debug

from PluginCore.base import Plugin, SearchResult
from PluginCore.inline_utils import collect_choice, resolve_filter


_EXTERNAL_PLUGIN_ENV_VARS: tuple[str, ...] = ("MM_PLUGIN_PATH", "MEDEIA_PLUGIN_PATH")

# Plugin instance cache keyed by (name, config_fingerprint)
_plugin_instance_cache: Dict[Tuple[str, str], Optional[Plugin]] = {}
_plugin_cache_lock = __import__("threading").Lock()


def _config_fingerprint(config: Optional[Dict[str, Any]], plugin_name: str = "") -> str:
    if config is None:
        return "none"
    try:
        import json
        payload: Any = config
        if isinstance(config, dict):
            plugin_block = config.get("plugin")
            if plugin_name and isinstance(plugin_block, dict):
                payload = plugin_block.get(str(plugin_name).strip().lower())
            elif isinstance(plugin_block, dict):
                payload = plugin_block
        normalized = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.md5(normalized.encode()).hexdigest()[:16]
    except Exception:
        return f"id:{id(config)}"


def _class_supports_method(
    plugin_class: Type[Plugin],
    method_name: str,
    base_method: Any,
) -> bool:
    try:
        method = getattr(plugin_class, method_name, None)
    except Exception:
        return False
    return callable(method) and method is not base_method


def _repo_root() -> Path:
    try:
        return Path(__file__).resolve().parents[1]
    except Exception:
        return Path.cwd()


def _iter_external_plugin_dirs() -> Tuple[Path, ...]:
    seen: set[str] = set()
    dirs: List[Path] = []

    candidates: List[Path] = [_repo_root() / "plugins"]
    try:
        cwd_plugins = Path.cwd() / "plugins"
        if cwd_plugins not in candidates:
            candidates.append(cwd_plugins)
    except Exception:
        pass

    for env_name in _EXTERNAL_PLUGIN_ENV_VARS:
        raw_value = str(os.environ.get(env_name, "") or "").strip()
        if not raw_value:
            continue
        for chunk in raw_value.split(os.pathsep):
            text = str(chunk or "").strip().strip('"')
            if not text:
                continue
            candidates.append(Path(text).expanduser())

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if resolved.exists() and resolved.is_dir():
                dirs.append(resolved)
        except Exception:
            continue

    return tuple(dirs)


def _iter_external_plugin_entries(plugin_dir: Path) -> Iterable[Tuple[str, Path, bool]]:
    try:
        children = sorted(plugin_dir.iterdir(), key=lambda entry: entry.name.lower())
    except Exception:
        return ()

    out: List[Tuple[str, Path, bool]] = []
    for child in children:
        name = str(child.name or "").strip()
        if not name or name.startswith("."):
            continue

        if child.is_file() and child.suffix.lower() == ".py" and child.stem != "__init__":
            fingerprint = hashlib.sha1(str(child).encode("utf-8", errors="ignore")).hexdigest()[:10]
            out.append((f"_medeia_plugin_{child.stem}_{fingerprint}", child, False))
            continue

        if child.is_dir():
            init_py = child / "__init__.py"
            if not init_py.exists() or not init_py.is_file():
                continue
            fingerprint = hashlib.sha1(str(child).encode("utf-8", errors="ignore")).hexdigest()[:10]
            out.append((f"_medeia_plugin_pkg_{child.name}_{fingerprint}", init_py, True))

    return tuple(out)

@dataclass(frozen=True)
class PluginInfo:
    """Metadata about a single plugin entry."""

    canonical_name: str
    plugin_class: Type[Plugin]
    module: str
    alias_names: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def supports_search(self) -> bool:
        return _class_supports_method(self.plugin_class, "search", Plugin.search)

    @property
    def supports_upload(self) -> bool:
        try:
            exposed = bool(getattr(self.plugin_class, "EXPOSE_AS_FILE_PROVIDER", True))
        except Exception:
            exposed = True
        return exposed and _class_supports_method(self.plugin_class, "upload", Plugin.upload)

    @property
    def supports_download(self) -> bool:
        return (
            _class_supports_method(self.plugin_class, "handle_url", Plugin.handle_url)
            or _class_supports_method(self.plugin_class, "download_url", Plugin.download_url)
            or _class_supports_method(self.plugin_class, "download", Plugin.download)
        )

    @property
    def is_multi_instance(self) -> bool:
        """True if the plugin declares MULTI_INSTANCE = True."""
        return bool(getattr(self.plugin_class, "MULTI_INSTANCE", False))

    @property
    def supported_cmdlets(self) -> frozenset:
        """Frozenset of cmdlet names this plugin declares support for."""
        raw = getattr(self.plugin_class, "SUPPORTED_CMDLETS", frozenset())
        try:
            return frozenset(str(c) for c in raw)
        except Exception:
            return frozenset()


class PluginRegistry:
    """Handles discovery, registration, and lookup of built-in and external plugins."""

    def __init__(self, package_name: str) -> None:
        self.package_name = (package_name or "").strip()
        self._infos: Dict[str, PluginInfo] = {}
        self._lookup: Dict[str, PluginInfo] = {}
        self._modules: set[str] = set()
        self._external_modules: set[str] = set()
        self._builtin_package_dirs: Tuple[Path, ...] = ()
        self._discovered = False
        self._external_dirs_scanned = False

    def _ensure_builtin_package_dirs(self) -> None:
        if self._builtin_package_dirs or not self.package_name:
            return
        try:
            package = importlib.import_module(self.package_name)
        except Exception:
            return

        package_path = getattr(package, "__path__", None)
        if not package_path:
            return

        builtin_dirs: List[Path] = []
        for entry in package_path:
            try:
                builtin_dirs.append(Path(str(entry)).resolve())
            except Exception:
                builtin_dirs.append(Path(str(entry)))
        self._builtin_package_dirs = tuple(builtin_dirs)

    def _is_builtin_package_dir(self, candidate: Path) -> bool:
        self._ensure_builtin_package_dirs()
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        for package_dir in self._builtin_package_dirs:
            try:
                if resolved == package_dir:
                    return True
            except Exception:
                continue
        return False

    def _normalize(self, value: Any) -> str:
        return str(value or "").strip().lower()

    def _candidate_names(self,
                         plugin_class: Type[Plugin],
                         override_name: Optional[str]) -> List[str]:
        names: List[str] = []
        seen: set[str] = set()

        def _add(value: Any) -> None:
            text = str(value or "").strip()
            normalized = text.lower()
            if not text or normalized in seen:
                return
            seen.add(normalized)
            names.append(text)

        if override_name:
            _add(override_name)
        else:
            _add(getattr(plugin_class, "PLUGIN_NAME", None))
            _add(getattr(plugin_class, "__name__", None))

        for alias in getattr(plugin_class, "PLUGIN_ALIASES", ()) or ():
            _add(alias)

        return names

    def register(
        self,
        plugin_class: Type[Plugin],
        *,
        override_name: Optional[str] = None,
        extra_aliases: Optional[Sequence[str]] = None,
        module_name: Optional[str] = None,
        replace: bool = False,
    ) -> PluginInfo:
        """Register a plugin class with canonical and alias names."""
        candidates = self._candidate_names(plugin_class, override_name)
        if not candidates:
            raise ValueError("plugin name candidates are required")

        canonical = self._normalize(candidates[0])
        if not canonical:
            raise ValueError("plugin name must not be empty")

        alias_names: List[str] = []
        alias_seen: set[str] = set()

        for candidate in candidates[1:]:
            normalized = self._normalize(candidate)
            if not normalized or normalized == canonical or normalized in alias_seen:
                continue
            alias_seen.add(normalized)
            alias_names.append(normalized)

        for alias in extra_aliases or ():
            normalized = self._normalize(alias)
            if not normalized or normalized == canonical or normalized in alias_seen:
                continue
            alias_seen.add(normalized)
            alias_names.append(normalized)

        info = PluginInfo(
            canonical_name=canonical,
            plugin_class=plugin_class,
            module=module_name or getattr(plugin_class, "__module__", "") or "",
            alias_names=tuple(alias_names),
        )

        existing = self._infos.get(canonical)
        if existing is not None and not replace:
            return existing

        self._infos[canonical] = info
        for lookup in (canonical,) + tuple(alias_names):
            self._lookup[lookup] = info
        return info

    def _register_module(self, module: ModuleType, *, replace: bool = False) -> None:
        module_name = getattr(module, "__name__", "")
        if not module_name:
            return
        if module_name in self._modules and not replace:
            return
        self._modules.add(module_name)

        # Iterate module dict directly (faster than dir()+getattr()).
        for candidate in vars(module).values():
            if not isinstance(candidate, type):
                continue
            if not issubclass(candidate, Plugin):
                continue
            if candidate is Plugin:
                continue
            if getattr(candidate, "__module__", "") != module_name:
                continue
            try:
                self.register(candidate, module_name=module_name, replace=replace)
            except Exception as exc:
                log(f"[plugin] Failed to register {module_name}.{candidate.__name__}: {exc}", file=sys.stderr)

    def _discover_external_plugins(self) -> None:
        if self._external_dirs_scanned:
            return
        self._external_dirs_scanned = True
        self._ensure_builtin_package_dirs()

        for plugin_dir in _iter_external_plugin_dirs():
            if self._is_builtin_package_dir(plugin_dir):
                continue

            plugin_dir_str = str(plugin_dir)
            path_added = False
            if plugin_dir_str and plugin_dir_str not in sys.path:
                sys.path.insert(0, plugin_dir_str)
                path_added = True

            try:
                for module_name, module_path, is_package in _iter_external_plugin_entries(plugin_dir):
                    if module_name in self._external_modules:
                        continue

                    try:
                        if is_package:
                            spec = importlib.util.spec_from_file_location(
                                module_name,
                                str(module_path),
                                submodule_search_locations=[str(module_path.parent)],
                            )
                        else:
                            spec = importlib.util.spec_from_file_location(module_name, str(module_path))
                        if spec is None or spec.loader is None:
                            raise ImportError("missing module spec loader")

                        module = importlib.util.module_from_spec(spec)
                        sys.modules[module_name] = module
                        spec.loader.exec_module(module)
                        self._external_modules.add(module_name)
                        self._register_module(module)
                    except Exception as exc:
                        log(f"[plugin] Failed to load external plugin {module_path}: {exc}", file=sys.stderr)
            finally:
                if path_added:
                    try:
                        sys.path.remove(plugin_dir_str)
                    except ValueError:
                        pass

    def discover(self) -> None:
        """Import and register plugins from the package."""

        if self._discovered or not self.package_name:
            return
        self._discovered = True

        try:
            package = importlib.import_module(self.package_name)
        except Exception as exc:
            log(f"[plugin] Failed to import package {self.package_name}: {exc}", file=sys.stderr)
            return

        self._register_module(package)
        self._ensure_builtin_package_dirs()
        package_path = getattr(package, "__path__", None)
        if not package_path:
            self._discover_external_plugins()
            return

        for finder, module_name, _ in pkgutil.iter_modules(package_path):
            if module_name.startswith("_"):
                continue
            module_path = f"{self.package_name}.{module_name}"
            try:
                module = importlib.import_module(module_path)
            except Exception as exc:
                log(f"[plugin] Failed to load {module_path}: {exc}", file=sys.stderr)
                continue
            self._register_module(module)

        # Pick up any Plugin subclasses loaded via other mechanisms.
        self._sync_subclasses()
        self._discover_external_plugins()

    def _try_import_for_name(self, normalized_name: str) -> None:
        """Best-effort import for a single plugin module.

        This avoids importing every provider module when the caller only needs
        one plugin (common for CLI usage).
        """
        name = str(normalized_name or "").strip().lower()
        if not name or not self.package_name:
            return

        candidates: List[str] = [name]
        if "-" in name:
            candidates.append(name.replace("-", "_"))
        if "." in name:
            candidates.append(name.split(".", 1)[0])
            compact = name.replace(".", "")
            if compact and compact not in candidates:
                candidates.append(compact)

        for mod_name in candidates:
            if not mod_name:
                continue
            module_path = f"{self.package_name}.{mod_name}"
            if module_path in self._modules:
                continue
            try:
                module = importlib.import_module(module_path)
            except Exception:
                continue
            self._register_module(module)
            # Pick up subclasses in case the module registers indirectly.
            self._sync_subclasses()
            return

    def get(self, name: str) -> Optional[PluginInfo]:
        if not name:
            return None

        normalized = self._normalize(name)
        info = self._lookup.get(normalized)
        if info is not None:
            return info

        # If we haven't done a full discovery yet, try importing just the
        # module that matches the requested name.
        if not self._discovered:
            self._try_import_for_name(normalized)
            self._discover_external_plugins()
            info = self._lookup.get(normalized)
            if info is not None:
                return info

        # Fall back to full package scan.
        self.discover()
        return self._lookup.get(normalized)

    def iter_plugins(self) -> Iterable[PluginInfo]:
        self.discover()
        return tuple(self._infos.values())

    def has_name(self, name: str) -> bool:
        return self.get(name) is not None

    def get_plugins_for_cmdlet(self, cmdlet_name: str) -> List[PluginInfo]:
        """Return all plugins that declare support for the given cmdlet name."""
        self.discover()
        target = str(cmdlet_name or "").strip().lower()
        return [
            info for info in self._infos.values()
            if target in info.supported_cmdlets
        ]

    def list_storage_plugin_instances(
        self,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, List[str]]:
        """Return {plugin_name: [instance_name, ...]} for all MULTI_INSTANCE storage plugins.

        Instance names come from the plugin's resolved config (plugin/provider/store sections).
        Plugins with no configured instances are omitted.
        """
        self.discover()
        result: Dict[str, List[str]] = {}
        for info in self._infos.values():
            if not info.is_multi_instance:
                continue
            if not info.supported_cmdlets.intersection(
                {"add-file", "download-file", "tag"}
            ):
                continue
            try:
                instance = info.plugin_class(config or {})
                instances = instance.configured_instances()
                if instances:
                    result[info.canonical_name] = instances
            except Exception:
                pass
        return result

    def _sync_subclasses(self) -> None:
        """Walk all plugin subclasses in memory and register them."""
        def _walk(cls: Type[Plugin]) -> None:
            for sub in cls.__subclasses__():
                try:
                    self.register(sub)
                except Exception:
                    pass
                _walk(sub)
        _walk(Plugin)

REGISTRY = PluginRegistry("plugins")
PLUGIN_REGISTRY = REGISTRY


@lru_cache(maxsize=512)
def _plugin_url_patterns(plugin_class: Type[Plugin]) -> Sequence[str]:
    try:
        return list(plugin_class.url_patterns())
    except Exception:
        return []


def register_plugin(
    plugin_class: Type[Plugin],
    *,
    name: Optional[str] = None,
    aliases: Optional[Sequence[str]] = None,
    module_name: Optional[str] = None,
    replace: bool = False,
) -> PluginInfo:
    return REGISTRY.register(
        plugin_class,
        override_name=name,
        extra_aliases=aliases,
        module_name=module_name,
        replace=replace,
    )


def get_plugin_class(name: str) -> Optional[Type[Plugin]]:
    raw_name = str(name or "").strip()
    if not raw_name or raw_name[:1] in {"[", "("} or "," in raw_name:
        return None
    info = REGISTRY.get(name)
    if info is None:
        return None
    return info.plugin_class


def get_plugin_capabilities(
    name: str,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a normalized capability summary for a plugin name."""
    info = REGISTRY.get(name)
    if info is None:
        return {
            "name": str(name or "").strip().lower(),
            "supported_cmdlets": [],
            "supports_search": False,
            "supports_upload": False,
            "supports_download": False,
            "supports_pipe_download": False,
            "supports_delete_file": False,
            "supports_url_association": False,
            "supports_note_association": False,
            "supports_relationship_association": False,
            "is_multi_instance": False,
            "configured_instances": [],
        }

    supported_cmdlets = sorted(str(c) for c in info.supported_cmdlets)
    supports_pipe_download = _class_supports_method(
        info.plugin_class,
        "resolve_pipe_result_download",
        Plugin.resolve_pipe_result_download,
    )
    delete_method = getattr(info.plugin_class, "delete_file", None)
    base_delete_method = getattr(Plugin, "delete_file", None)
    supports_delete_file = callable(delete_method) and delete_method is not base_delete_method

    configured_instances: List[str] = []
    supports_url_association = False
    supports_note_association = False
    supports_relationship_association = False
    try:
        plugin_obj = info.plugin_class(config or {})
        configured_instances = [str(v) for v in (plugin_obj.configured_instances() or []) if str(v).strip()]
        supports_url_association = bool(getattr(plugin_obj, "supports_url_association", False))
        supports_note_association = bool(getattr(plugin_obj, "supports_note_association", False))
        supports_relationship_association = bool(getattr(plugin_obj, "supports_relationship_association", False))
    except Exception:
        configured_instances = []
        supports_url_association = False
        supports_note_association = False
        supports_relationship_association = False

    return {
        "name": info.canonical_name,
        "supported_cmdlets": supported_cmdlets,
        "supports_search": bool(info.supports_search),
        "supports_upload": bool(info.supports_upload),
        "supports_download": bool(info.supports_download),
        "supports_pipe_download": bool(supports_pipe_download),
        "supports_delete_file": bool(supports_delete_file),
        "supports_url_association": bool(supports_url_association),
        "supports_note_association": bool(supports_note_association),
        "supports_relationship_association": bool(supports_relationship_association),
        "is_multi_instance": bool(info.is_multi_instance),
        "configured_instances": configured_instances,
    }


def selection_auto_stage_for_table(
    table_type: str,
    stage_args: Optional[Sequence[str]] = None,
) -> Optional[list[str]]:
    t = str(table_type or "").strip().lower()
    if not t:
        return None

    plugin_key = t.split(".", 1)[0] if "." in t else t
    plugin_class = get_plugin_class(plugin_key) or get_plugin_class(t)
    if plugin_class is None:
        return None

    try:
        return plugin_class.selection_auto_stage(t, stage_args)
    except Exception:
        return None


def is_known_plugin_name(name: str) -> bool:
    return REGISTRY.has_name(name)


def import_plugin_module(name: str) -> Optional[ModuleType]:
    """Import ``plugins.<name>`` when that plugin is installed.

    Core code should use this (or ``plugin_attr``) instead of
    ``from plugins.foo import ...`` so the app still starts with an empty
    plugins folder. Missing plugins return None.
    """
    normalized = str(name or "").strip()
    if not normalized or normalized.startswith("."):
        return None
    module_name = normalized if normalized.startswith("plugins.") else f"plugins.{normalized}"
    try:
        return importlib.import_module(module_name)
    except Exception:
        return None


def plugin_attr(name: str, attr: str, default: Any = None) -> Any:
    """Return an attribute from an installed plugin module, else ``default``."""
    module = import_plugin_module(name)
    if module is None:
        return default
    return getattr(module, attr, default)


def plugin_for_storage(
    config: Optional[Dict[str, Any]] = None,
    *,
    name: Optional[str] = None,
    item: Any = None,
    backend: Any = None,
) -> Optional[Plugin]:
    plugin_name = str(name or "").strip()
    if not plugin_name and backend is not None:
        plugin_name = str(getattr(backend, "STORE_TYPE", "") or "").strip()
    if not plugin_name and item is not None:
        try:
            from SYS.item_accessors import get_field

            plugin_name = str(get_field(item, "plugin") or "").strip()
        except Exception:
            plugin_name = ""
    if not plugin_name:
        return None
    return get_plugin(plugin_name, config)


def refresh_local_plugin(name: str) -> bool:
    """Import or reload ``plugins.<name>`` and register it."""
    normalized = str(name or "").strip()
    if not normalized:
        return False
    module_name = f"plugins.{normalized}"
    REGISTRY.discover()
    REGISTRY._modules.discard(module_name)
    try:
        if module_name in sys.modules:
            module = importlib.reload(sys.modules[module_name])
        else:
            module = importlib.import_module(module_name)
    except Exception as exc:
        debug(f"[plugin] Failed to refresh '{normalized}': {exc}")
        return False
    REGISTRY._register_module(module, replace=True)
    clear_plugin_cache()
    return True


def get_plugin(name: str, config: Optional[Dict[str, Any]] = None) -> Optional[Plugin]:
    info = REGISTRY.get(name)
    if info is None:
        return None

    # Check cache first
    cache_key = (str(name).strip().lower(), _config_fingerprint(config, str(name)))
    with _plugin_cache_lock:
        if cache_key in _plugin_instance_cache:
            return _plugin_instance_cache[cache_key]

    try:
        missing_depends = []
        try:
            missing_depends = list(info.plugin_class.missing_plugin_depends() or [])
        except Exception:
            missing_depends = []
        if missing_depends:
            needed = ", ".join(missing_depends)
            log(
                f"Plugin '{name}' requires {needed}. Install with .plugin -add {needed}",
                file=sys.stderr,
            )
            return None
        plugin = info.plugin_class(config)
        try:
            plugin.requested_name = str(name or "").strip().lower()
        except Exception:
            pass
        if not plugin.validate():
            debug(f"[plugin] Plugin '{name}' is not available")
            # Do not cache negative validate results — config/instances may become
            # valid later in the same session (e.g. after .config edits).
            return None
        with _plugin_cache_lock:
            _plugin_instance_cache[cache_key] = plugin
        return plugin
    except Exception as exc:
        debug(f"[plugin] Error initializing '{name}': {exc}")
        # Avoid sticky None cache for transient init failures.
        return None


def list_plugins(config: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
    availability: Dict[str, bool] = {}
    for info in REGISTRY.iter_plugins():
        try:
            if info.plugin_class.missing_plugin_depends():
                availability[info.canonical_name] = False
                continue
            plugin = info.plugin_class(config)
            availability[info.canonical_name] = plugin.validate()
        except Exception:
            availability[info.canonical_name] = False
    return availability


def get_plugin_for_cmdlet(
    name: str,
    cmdlet_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[Plugin]:
    raw_name = str(name or "").strip()
    if not raw_name or raw_name[:1] in {"[", "("} or "," in raw_name:
        return None
    info = REGISTRY.get(name)
    if info is None:
        return None

    cmd = str(cmdlet_name or "").strip().lower()
    if not cmd or cmd not in info.supported_cmdlets:
        debug(f"[plugin] Plugin '{name}' does not declare cmdlet '{cmdlet_name}'")
        return None

    return get_plugin(name, config)


def list_plugins_for_cmdlet(
    cmdlet_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, bool]:
    availability: Dict[str, bool] = {}
    for info in REGISTRY.get_plugins_for_cmdlet(cmdlet_name):
        try:
            if info.plugin_class.missing_plugin_depends():
                availability[info.canonical_name] = False
                continue
            plugin = info.plugin_class(config)
            availability[info.canonical_name] = plugin.validate()
        except Exception:
            availability[info.canonical_name] = False
    return availability


def _info_has_configured_plugin_entry(
    info: PluginInfo,
    cfg: Optional[Dict[str, Any]] = None,
    plugin_section: Optional[Dict[str, Any]] = None,
) -> bool:
    config_dict = cfg or {}
    section: Dict[str, Any] = (
        plugin_section if isinstance(plugin_section, dict)
        else (config_dict.get("plugin") or {})  # type: ignore[assignment]
    )

    entry = section.get(info.canonical_name.lower())
    return isinstance(entry, dict) and bool(entry)


def list_plugin_names_for_cmdlet(
    cmdlet_name: str,
    config: Optional[Dict[str, Any]] = None,
    *,
    configured_only: bool = False,
) -> List[str]:
    """Return plugin names that explicitly declare support for a cmdlet."""
    cmd = str(cmdlet_name or "").strip().lower()
    if not cmd:
        return []

    supported_infos = list(REGISTRY.get_plugins_for_cmdlet(cmd))
    supported = {info.canonical_name for info in supported_infos}

    if not configured_only:
        return sorted(supported)

    cfg = config or {}
    plugin_section: Dict[str, Any] = cfg.get("plugin") or {}  # type: ignore[assignment]
    return sorted(
        info.canonical_name
        for info in supported_infos
        if _info_has_configured_plugin_entry(info, cfg, plugin_section)
    )


def match_plugin_name_for_url(url: str) -> Optional[str]:
    raw_url = str(url or "").strip()
    raw_url_lower = raw_url.lower()
    try:
        parsed = urlparse(raw_url)
        host = (parsed.hostname or "").strip().lower()
        path = (parsed.path or "").strip()
    except Exception:
        host = ""
        path = ""

    def _norm_host(h: str) -> str:
        h_norm = str(h or "").strip().lower()
        if h_norm.startswith("www."):
            h_norm = h_norm[4:]
        return h_norm

    host_norm = _norm_host(host)

    if host_norm:
        if host_norm == "openlibrary.org" or host_norm.endswith(".openlibrary.org"):
            if REGISTRY.has_name("archiveorg"):
                return "archiveorg"
            if REGISTRY.has_name("archive.org"):
                return "archive.org"
            return "openlibrary" if REGISTRY.has_name("openlibrary") else None

        if host_norm == "archive.org" or host_norm.endswith(".archive.org"):
            if REGISTRY.has_name("archiveorg"):
                return "archiveorg"
            if REGISTRY.has_name("archive.org"):
                return "archive.org"
            low_path = str(path or "").lower()
            is_borrowish = (
                low_path.startswith("/borrow/")
                or low_path.startswith("/stream/")
                or low_path.startswith("/services/loans/")
                or "/services/loans/" in low_path
            )
            if is_borrowish:
                return "openlibrary" if REGISTRY.has_name("openlibrary") else None
            return "internetarchive" if REGISTRY.has_name("internetarchive") else None

    for info in REGISTRY.iter_plugins():
        domains = _plugin_url_patterns(info.plugin_class)
        if not domains:
            continue
        for domain in domains:
            dom_raw = str(domain or "").strip()
            dom = dom_raw.lower()
            if not dom:
                continue
            if "://" in dom or dom.startswith("magnet:") or dom.endswith(":") or "🧲" in dom:
                if raw_url_lower.startswith(dom):
                    return info.canonical_name
                continue

            dom_norm = _norm_host(dom)
            if not dom_norm or not host_norm:
                continue
            if host_norm == dom_norm or host_norm.endswith("." + dom_norm):
                return info.canonical_name

    return None


def plugin_inline_query_choices(
    plugin_name: str,
    field_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Return plugin-declared inline query choices for a field (e.g., system:GBA).

    Plugins can expose a mapping via ``QUERY_ARG_CHOICES`` (preferred) or
    ``INLINE_QUERY_FIELD_CHOICES`` / ``inline_query_field_choices()``. The helper
    keeps completion logic simple and reusable.
    """

    pname = str(plugin_name or "").strip().lower()
    field = str(field_name or "").strip().lower()
    if not pname or not field:
        return []

    try:
        mapping: Dict[str, List[Dict[str, Any]]] = {}
        info = REGISTRY.get(pname)
        if info is not None:
            mapping = collect_choice(info.plugin_class)

        if not mapping:
            plugin = get_plugin(pname, config)
            if plugin is None:
                return []
            mapping = collect_choice(plugin)

        if not mapping:
            return []

        entries = mapping.get(field, [])
        if not entries:
            return []

        seen: set[str] = set()
        out: List[str] = []
        for entry in entries:
            text = entry.get("text") or entry.get("value")
            if not text:
                continue
            text_str = str(text)
            if text_str in seen:
                continue
            seen.add(text_str)
            out.append(text_str)
            for alias in entry.get("aliases", []):
                alias_str = str(alias)
                if alias_str and alias_str not in seen:
                    seen.add(alias_str)
                    out.append(alias_str)
        return out
    except Exception:
        return []


def plugin_query_field_map(
    plugin_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[str]]:
    pname = str(plugin_name or "").strip().lower()
    if not pname:
        return {}
    try:
        info = REGISTRY.get(pname)
        if info is not None:
            mapping = collect_choice(info.plugin_class)
        else:
            plugin = get_plugin(pname, config)
            if plugin is None:
                return {}
            mapping = collect_choice(plugin)
        result: Dict[str, List[str]] = {}
        for field, choices in mapping.items():
            texts = []
            for c in choices:
                text = str(c.get("text") or c.get("value") or "")
                if text:
                    texts.append(text)
            result[str(field)] = texts
        return result
    except Exception:
        return {}


def get_plugin_for_url(url: str,
                       config: Optional[Dict[str, Any]] = None) -> Optional[Plugin]:
    name = match_plugin_name_for_url(url)
    if not name:
        return None
    return get_plugin(name, config)


def list_selection_url_prefixes() -> List[str]:
    prefixes: List[str] = []
    seen: set[str] = set()
    for info in REGISTRY.iter_plugins():
        try:
            values = info.plugin_class.selection_url_prefixes()
        except Exception:
            values = ()
        for value in values or ():
            try:
                normalized = str(value or "").strip().lower()
            except Exception:
                continue
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            prefixes.append(normalized)
    return prefixes


def resolve_inline_filters(
    plugin: Plugin,
    inline_args: Dict[str, Any],
    *,
    field_transforms: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Map inline query args to plugin filter values using the canonical helper."""

    return resolve_filter(plugin, inline_args, field_transforms=field_transforms)


def clear_plugin_cache() -> None:
    """Clear the plugin instance cache. Useful for testing or config reloads."""
    global _plugin_instance_cache
    with _plugin_cache_lock:
        _plugin_instance_cache.clear()
    try:
        from SYS.config import _multi_instance_plugin_names

        _multi_instance_plugin_names.cache_clear()
    except Exception:
        pass
    try:
        from PluginCore.backend_registry import clear_registry_cache

        clear_registry_cache()
    except Exception:
        pass


def refresh_discovered_plugins() -> None:
    """Re-scan plugin folders so newly installed plugins are completable without restart."""
    import importlib
    import pkgutil

    importlib.invalidate_caches()
    clear_plugin_cache()
    REGISTRY._external_dirs_scanned = False
    try:
        package = importlib.import_module(REGISTRY.package_name)
        package_path = getattr(package, "__path__", None)
        if package_path:
            for _finder, module_name, _ispkg in pkgutil.iter_modules(list(package_path)):
                if str(module_name).startswith("_"):
                    continue
                if REGISTRY._lookup.get(str(module_name).strip().lower()):
                    continue
                module_path = f"{REGISTRY.package_name}.{module_name}"
                try:
                    module = importlib.import_module(module_path)
                    REGISTRY._register_module(module)
                except Exception as exc:
                    log(f"[plugin] Failed to load {module_path}: {exc}", file=sys.stderr)
        REGISTRY._sync_subclasses()
    except Exception:
        REGISTRY._discovered = False
        REGISTRY.discover()
    REGISTRY._discover_external_plugins()
    try:
        from SYS.cli_completer import clear_completer_plugin_caches

        clear_completer_plugin_caches()
    except Exception:
        pass


__all__ = [
    "PluginInfo",
    "Plugin",
    "SearchResult",
    "PluginRegistry",
    "PLUGIN_REGISTRY",
    "register_plugin",
    "get_plugin",
    "list_plugins",
    "get_plugin_for_cmdlet",
    "list_plugins_for_cmdlet",
    "list_plugin_names_for_cmdlet",
    "match_plugin_name_for_url",
    "get_plugin_for_url",
    "list_selection_url_prefixes",
    "get_plugin_class",
    "get_plugin_capabilities",
    "selection_auto_stage_for_table",
    "plugin_inline_query_choices",
    "is_known_plugin_name",
    "clear_plugin_cache",
    "refresh_discovered_plugins",
]
