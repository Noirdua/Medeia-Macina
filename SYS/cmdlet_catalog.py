from __future__ import annotations

import re
import sys
from importlib import import_module, reload as reload_module
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional
import logging
from PluginCore.commands import get_primary_command_object
from PluginCore.registry import get_plugin
logger = logging.getLogger(__name__)

_FLAG_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_VARIADIC_FLAG_ALLOWLIST = {"query", "instance", "plugin", "store"}

try:
    from .config import get_local_storage_path
except Exception:
    get_local_storage_path = None  # type: ignore


def _should_hide_db_args(config: Optional[Dict[str, Any]]) -> bool:
    """Return True when the library root/local DB is not configured."""
    if not isinstance(config, dict):
        return False
    if get_local_storage_path is None:
        return False
    try:
        return not bool(get_local_storage_path(config))
    except Exception:
        return False


_cmdlet_pkg: ModuleType | None = None


def _get_cmdlet_package() -> Optional[ModuleType]:
    global _cmdlet_pkg
    if _cmdlet_pkg is not None:
        return _cmdlet_pkg
    try:
        _cmdlet_pkg = import_module("cmdlet")
    except Exception as exc:
        logger.exception("Failed to import cmdlet package: %s", exc)
        _cmdlet_pkg = None
    return _cmdlet_pkg


def _get_registry() -> Dict[str, Any]:
    pkg = _get_cmdlet_package()
    if pkg is None:
        return {}
    return getattr(pkg, "REGISTRY", {}) or {}


def ensure_registry_loaded(force: bool = False) -> None:
    """Ensure native commands are registered into REGISTRY (idempotent unless force=True)."""
    pkg = _get_cmdlet_package()
    if pkg is None:
        return
    ensure_fn = getattr(pkg, "ensure_cmdlet_modules_loaded", None)
    if callable(ensure_fn):
        try:
            ensure_fn(force=force)
        except Exception as exc:
            logger.exception("ensure_registry_loaded: ensure_cmdlet_modules_loaded failed: %s", exc)


def _normalize_mod_name(mod_name: str) -> str:
    """Normalize a command/module name for import resolution."""
    normalized = (mod_name or "").strip()
    if normalized.startswith("."):
        normalized = normalized.lstrip(".")
    normalized = normalized.replace("-", "_")
    return normalized


def _nested_cmdlet_modules(normalized: str) -> List[str]:
    """Return nested cmdlet module candidates for names like search_file."""
    if not normalized:
        return []

    try:
        cmdlet_dir = Path(__file__).resolve().parent.parent / "cmdlet"
    except Exception:
        return []

    if not cmdlet_dir.is_dir():
        return []

    candidates: List[str] = []
    seen: set[str] = set()
    parts = normalized.split("_", 1)

    try:
        children = sorted(cmdlet_dir.iterdir(), key=lambda path: path.name.lower())
    except Exception:
        return []

    for child in children:
        if not child.is_dir() or not (child / "__init__.py").is_file():
            continue

        direct_file = child / f"{normalized}.py"
        if direct_file.is_file():
            module_name = f"cmdlet.{child.name}.{normalized}"
            if module_name not in seen:
                seen.add(module_name)
                candidates.append(module_name)

        if len(parts) != 2:
            continue

        left, right = parts
        if child.name == right and (child / f"{left}.py").is_file():
            module_name = f"cmdlet.{right}.{left}"
            if module_name not in seen:
                seen.add(module_name)
                candidates.append(module_name)

        if child.name == left and (child / f"{right}.py").is_file():
            module_name = f"cmdlet.{left}.{right}"
            if module_name not in seen:
                seen.add(module_name)
                candidates.append(module_name)

    return candidates


def import_cmd_module(mod_name: str, *, reload_loaded: bool = False):
    """Import a cmdlet/command module from legacy or plugin-owned packages."""
    normalized = _normalize_mod_name(mod_name)
    if not normalized:
        return None
    qualified_names = [
        f"plugins.{normalized}.commands",
        f"cmdnat.{normalized}",
        f"cmdlet.{normalized}",
        *_nested_cmdlet_modules(normalized),
        normalized,
    ]

    seen: set[str] = set()
    for qualified in qualified_names:
        if qualified in seen:
            continue
        seen.add(qualified)
        try:
            # When attempting a bare import (package is None), prefer the repo-local
            # `MPV` package for the `mpv` module name so we don't accidentally
            # import the third-party `mpv` package (python-mpv) which can raise
            # OSError if system libmpv is missing.
            if qualified == normalized and normalized == "mpv":
                try:
                    if reload_loaded and "MPV" in sys.modules:
                        return reload_module(sys.modules["MPV"])
                    return import_module("MPV")
                except ModuleNotFoundError:
                    # Local MPV package not present; fall back to the normal bare import.
                    pass

            if reload_loaded and qualified in sys.modules:
                return reload_module(sys.modules[qualified])
            return import_module(qualified)
        except ModuleNotFoundError:
            # Module not available in this package prefix; try the next.
            continue
        except (ImportError, OSError) as exc:
            # Some native/binary-backed packages (e.g., mpv) raise ImportError/OSError
            # on systems missing shared libraries. These are optional; log a short
            # warning but avoid spamming the console with a full traceback.
            logger.warning("Optional module %s failed to import: %s", qualified, exc)
            continue
        except Exception:
            # Unexpected errors should be loud and include a traceback to aid debugging.
            logger.exception("Unexpected error importing module %s", qualified)
            continue
    return None


def _normalize_arg(arg: Any) -> Dict[str, Any]:
    """Convert a CmdletArg/dict into a plain metadata dict."""
    if isinstance(arg, dict):
        name = arg.get("name", "")
        return {
            "name": str(name).lstrip("-"),
            "type": arg.get("type", "string"),
            "required": bool(arg.get("required", False)),
            "description": arg.get("description", ""),
            "choices": arg.get("choices", []) or [],
            "alias": arg.get("alias", ""),
            "variadic": arg.get("variadic", False),
            "query_key": arg.get("query_key", None),
            "query_aliases": arg.get("query_aliases", []) or [],
            "query_only": bool(arg.get("query_only", False)),
            "requires_db": bool(arg.get("requires_db", False)),
        }

    name = getattr(arg, "name", "") or ""
    return {
        "name": str(name).lstrip("-"),
        "type": getattr(arg, "type", "string"),
        "required": bool(getattr(arg, "required", False)),
        "description": getattr(arg, "description", ""),
        "choices": getattr(arg, "choices", []) or [],
        "alias": getattr(arg, "alias", ""),
        "variadic": getattr(arg, "variadic", False),
        "query_key": getattr(arg, "query_key", None),
        "query_aliases": getattr(arg, "query_aliases", []) or [],
        "query_only": bool(getattr(arg, "query_only", False)),
        "requires_db": bool(getattr(arg, "requires_db", False)),
    }


def get_cmdlet_metadata(
    cmd_name: str, config: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Return normalized metadata for a cmdlet, if available (aliases supported)."""
    ensure_registry_loaded()
    normalized = cmd_name.replace("-", "_")
    mod = import_cmd_module(normalized)
    data = get_primary_command_object(mod) if mod else None

    if data is None:
        try:
            registry = _get_registry()
            reg_fn = registry.get(cmd_name.replace("_", "-").lower())
            if reg_fn:
                owner_mod = getattr(reg_fn, "__module__", "")
                if owner_mod:
                    owner = import_module(owner_mod)
                    data = get_primary_command_object(owner)
        except Exception as exc:
            logger.exception("Registry fallback failed while resolving cmdlet %s: %s", cmd_name, exc)
            data = None

    if not data:
        return None

    if hasattr(data, "to_dict"):
        base = data.to_dict()
    elif isinstance(data, dict):
        base = data
    else:
        base = {}

    name = getattr(data, "name", base.get("name", cmd_name)) or cmd_name
    aliases = getattr(data, "alias", base.get("alias", [])) or []
    usage = getattr(data, "usage", base.get("usage", ""))
    summary = getattr(data, "summary", base.get("summary", ""))
    details = getattr(data, "detail", base.get("detail", [])) or []
    args_list = list(getattr(data, "arg", base.get("arg", [])) or [])
    extra_args = getattr(data, "plugin_contributed_args", None)
    if callable(extra_args):
        try:
            args_list.extend(extra_args() or [])
        except Exception:
            pass
    args = [_normalize_arg(arg) for arg in args_list]
    examples_list = getattr(data, "examples", base.get("examples", [])) or []
    if not examples_list:
        examples_list = getattr(data, "example", base.get("example", [])) or []
    examples = []
    for example in examples_list:
        text = str(example or "").strip()
        if text:
            examples.append(text)
    extra_examples = getattr(data, "plugin_contributed_examples", None)
    if callable(extra_examples):
        try:
            for example in extra_examples() or []:
                text = str(example or "").strip()
                if text and text not in examples:
                    examples.append(text)
        except Exception:
            pass

    if _should_hide_db_args(config):
        args = [a for a in args if not a.get("requires_db")]

    return {
        "name": str(name).replace("_", "-").lower(),
        "aliases": [str(a).replace("_", "-").lower() for a in aliases if a],
        "usage": usage,
        "summary": summary,
        "details": details,
        "args": args,
        "examples": examples,
        "raw": data,
    }


def list_cmdlet_metadata(
    force: bool = False, config: Optional[Dict[str, Any]] = None
) -> Dict[str, Dict[str, Any]]:
    """Collect metadata for all registered cmdlet keyed by canonical name."""
    ensure_registry_loaded(force=force)
    entries: Dict[str, Dict[str, Any]] = {}
    registry = _get_registry()
    for reg_name in registry.keys():
        meta = get_cmdlet_metadata(reg_name, config=config)
        canonical = str(reg_name).replace("_", "-").lower()

        if meta:
            canonical = meta.get("name", canonical)
            aliases = meta.get("aliases", [])
            base = entries.get(
                canonical,
                {
                    "name": canonical,
                    "aliases": [],
                    "usage": "",
                    "summary": "",
                    "details": [],
                    "args": [],
                    "examples": meta.get("examples", []),
                    "raw": meta.get("raw"),
                },
            )
            merged_aliases = set(base.get("aliases", [])) | set(aliases)
            if canonical != reg_name:
                merged_aliases.add(reg_name)
            base["aliases"] = sorted(a for a in merged_aliases if a and a != canonical)
            if not base.get("usage") and meta.get("usage"):
                base["usage"] = meta["usage"]
            if not base.get("summary") and meta.get("summary"):
                base["summary"] = meta["summary"]
            if not base.get("details") and meta.get("details"):
                base["details"] = meta["details"]
            if not base.get("args") and meta.get("args"):
                base["args"] = meta["args"]
            example_sources: List[str] = []
            for attr in ("examples", "example"):
                values = meta.get(attr, []) if isinstance(meta, dict) else []
                example_sources.extend(values or [])
            merged_examples = [e for e in base.get("examples", []) or []]
            for example_entry in example_sources:
                if example_entry not in merged_examples:
                    merged_examples.append(example_entry)
            base["examples"] = merged_examples
            if not base.get("raw"):
                base["raw"] = meta.get("raw")
            entries[canonical] = base
        else:
            entries.setdefault(
                canonical,
                {
                    "name": canonical,
                    "aliases": [],
                    "usage": "",
                    "summary": "",
                    "details": [],
                    "args": [],
                    "examples": [],
                    "raw": None,
                },
            )
    return entries


def list_cmdlet_names(
    include_aliases: bool = True,
    force: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Return sorted cmdlet names (optionally including aliases)."""
    ensure_registry_loaded(force=force)
    entries = list_cmdlet_metadata(force=force, config=config)
    names = set()
    for meta in entries.values():
        names.add(meta.get("name", ""))
        if include_aliases:
            for alias in meta.get("aliases", []):
                names.add(alias)
    return sorted(n for n in names if n)


def _is_completable_flag_name(name: str) -> bool:
    text = str(name or "").strip().lstrip("-")
    if not text or text.startswith("@"):
        return False
    return bool(_FLAG_NAME_RE.fullmatch(text))


def _append_arg_flag_candidates(
    flags: List[str],
    seen: set[str],
    *,
    name: str,
    alias: str = "",
) -> None:
    logical = str(name or "").strip().lstrip("-")
    if not _is_completable_flag_name(logical):
        return
    candidates = [f"-{logical}", f"--{logical}"]
    alias_logical = str(alias or "").strip().lstrip("-")
    if alias_logical and _is_completable_flag_name(alias_logical):
        candidates.append(f"-{alias_logical}")
    for candidate in candidates:
        if candidate not in seen:
            flags.append(candidate)
            seen.add(candidate)


def get_cmdlet_arg_flags_from_object(cmdlet_obj: Any) -> List[str]:
    """Return completable flag variants from a live cmdlet object's arg specs."""
    args_list = getattr(cmdlet_obj, "arg", None) or []
    flags: List[str] = []
    seen: set[str] = set()
    for arg in args_list:
        if bool(getattr(arg, "query_only", False)):
            continue
        raw_name = str(getattr(arg, "name", "") or "").strip()
        if not _is_completable_flag_name(raw_name):
            continue
        if bool(getattr(arg, "variadic", False)) and not raw_name.startswith("-"):
            continue
        _append_arg_flag_candidates(
            flags,
            seen,
            name=raw_name,
            alias=str(getattr(arg, "alias", "") or ""),
        )
    return flags


def get_cmdlet_arg_flags(cmd_name: str, config: Optional[Dict[str, Any]] = None) -> List[str]:
    """Return flag variants for cmdlet arguments (e.g., -name/--name)."""
    meta = get_cmdlet_metadata(cmd_name, config=config)
    if not meta:
        return []

    flags: List[str] = []
    seen: set[str] = set()

    for arg in meta.get("args", []):
        # Query-only fields (e.g. limit:) are completed inside -query, not as flags.
        if bool(arg.get("query_only", False)):
            continue
        name = str(arg.get("name") or "").strip()
        if not _is_completable_flag_name(name):
            continue
        logical = name.lstrip("-").lower()
        if bool(arg.get("variadic", False)) and logical not in _VARIADIC_FLAG_ALLOWLIST:
            continue
        if logical in {"@n"} or name.startswith("@"):
            continue
        _append_arg_flag_candidates(
            flags,
            seen,
            name=name,
            alias=str(arg.get("alias") or ""),
        )

    return flags


def get_cmdlet_arg_choices(
    cmd_name: str, arg_name: str, config: Optional[Dict[str, Any]] = None
) -> List[str]:
    """Return declared choices for a cmdlet argument.

    Special-cases dynamic choices for certain arguments (e.g., Matrix -room)
    which may be populated from configuration or provider queries.
    """
    meta = get_cmdlet_metadata(cmd_name, config=config)
    if not meta:
        return []
    target = arg_name.lstrip("-")

    # Dynamic handling for Matrix room choices
    try:
        canonical = (meta.get("name") or str(cmd_name)).replace("_", "-")
    except Exception:
        canonical = str(cmd_name)

    if target == "room" and canonical in (".matrix", "matrix"):
        # Load default room IDs from configuration and attempt to resolve display names
        try:
            if config is None:
                from SYS.config import load_config

                config = load_config(emit_summary=False)
        except Exception as exc:
            logger.exception("Failed to load config for matrix default choices: %s", exc)
            config = config or {}

        matrix_conf = {}
        try:
            plugins = config.get("plugin") or {}
            matrix_conf = plugins.get("matrix") or {}
        except Exception as exc:
            logger.exception("Failed to read matrix plugin config: %s", exc)
            matrix_conf = {}

        raw = None
        for key in ("room", "room_id", "rooms", "room_ids"):
            if key in matrix_conf:
                raw = matrix_conf.get(key)
                break
        ids: List[str] = []
        try:
            if isinstance(raw, (list, tuple, set)):
                ids = [str(v).strip() for v in raw if str(v).strip()]
            else:
                text = str(raw or "").strip()
                if text:
                    import re

                    ids = [p.strip() for p in re.split(r"[,\s]+", text) if p and p.strip()]
        except Exception as exc:
            logger.exception("Failed to parse matrix room ids from config: %r", raw)
            ids = []

        if ids:
            # Try to resolve names via the Matrix plugin if config provides auth info
            try:
                hs = matrix_conf.get("homeserver")
                token = matrix_conf.get("access_token")
                if hs and token:
                    try:
                        provider = get_plugin("matrix", config)
                        if provider is not None:
                            try:
                                rooms = provider.list_rooms(room_ids=ids)
                                choices = []
                                for r in rooms or []:
                                    name = str(r.get("name") or "").strip()
                                    rid = str(r.get("room_id") or "").strip()
                                    choices.append(name or rid)
                                if choices:
                                    return choices
                            except Exception as exc:
                                logger.exception("Matrix plugin failed while listing rooms: %s", exc)
                    except Exception as exc:
                        logger.exception("Failed to initialize Matrix plugin: %s", exc)
            except Exception as exc:
                logger.exception("Failed to resolve matrix rooms: %s", exc)

            # Fallback: return raw ids as choices
            return ids

    # Default static choices from metadata
    for arg in meta.get("args", []):
        if arg.get("name") == target:
            return list(arg.get("choices", []) or [])
    return []
