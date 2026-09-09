from __future__ import annotations

import os
import sys
from typing import Any, Callable, Dict, Iterable, Iterator, Sequence
from importlib import import_module as _import_module

# A cmdlet is a callable taking (result, args, config) -> int
Cmdlet = Callable[[Any, Sequence[str], Dict[str, Any]], int]

# Registry of command-name -> cmdlet function
REGISTRY: Dict[str,
               Cmdlet] = {}


def _normalize_cmd_name(name: str) -> str:
    return str(name or "").replace("_", "-").lower().strip()


def register_callable(names: Iterable[str], fn: Cmdlet) -> Cmdlet:
    """Register a callable under one or more command names.

    This is the single registration mechanism used by both:
    - legacy function cmdlet (decorator form)
    - class-based cmdlet (Cmdlet.register())
    """
    for name in names:
        key = _normalize_cmd_name(name)
        if key:
            REGISTRY[key] = fn
    return fn


def register(names: Iterable[str]):
    """Decorator to register a function under one or more command names.

    Usage:
        @register(["add-tags"])
        def _run(result, args, config) -> int: ...
    """

    def _wrap(fn: Cmdlet) -> Cmdlet:
        return register_callable(names, fn)

    return _wrap


def get(cmd_name: str) -> Cmdlet | None:
    return REGISTRY.get(_normalize_cmd_name(cmd_name))


_MODULES_LOADED = False

def _iter_cmdlet_module_names() -> Iterator[str]:
    cmdlet_dir = os.path.dirname(__file__)
    try:
        entries = os.listdir(cmdlet_dir)
    except Exception:
        return iter(())

    def _generator() -> Iterator[str]:
        for filename in entries:
            if not (filename.endswith(".py") and not filename.startswith("_")
                    and filename != "__init__.py"):
                continue
            mod_name = filename[:-3]
            if "_" not in mod_name:
                continue
            yield mod_name

    return _generator()


def _load_cmdlet_module(mod_name: str) -> None:
    try:
        _import_module(f".{mod_name}", __name__)
    except Exception as exc:
        print(f"Error importing cmdlet '{mod_name}': {exc}", file=sys.stderr)


def _load_root_modules() -> None:
    for root in ("select_cmdlet",):
        try:
            _import_module(root)
        except Exception:
            continue


def _load_helper_modules() -> None:
    from importlib import import_module as _import_abs

    qualified: list[str] = []
    try:
        from cmdlet.file_cmdlet import File

        qualified.extend(File._ACTION_MODULE.values())
    except Exception:
        pass
    try:
        from cmdlet.metadata_cmdlet import Metadata

        qualified.extend(Metadata._DISPATCH.values())
    except Exception:
        pass

    seen: set[str] = set()
    for name in qualified:
        key = str(name or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            _import_abs(key)
        except Exception as exc:
            print(f"Error importing cmdlet helper '{key}': {exc}", file=sys.stderr)


def _register_native_commands() -> None:
    try:
        from cmdnat import register_native_commands
    except Exception:
        return
    try:
        register_native_commands(REGISTRY)
    except Exception:
        pass


def _register_plugin_commands() -> None:
    try:
        from PluginCore.commands import register_plugin_commands
    except Exception:
        return
    try:
        register_plugin_commands(REGISTRY)
    except Exception:
        pass


def ensure_cmdlet_modules_loaded(force: bool = False) -> None:
    global _MODULES_LOADED

    if _MODULES_LOADED and not force:
        return

    for mod_name in _iter_cmdlet_module_names():
        _load_cmdlet_module(mod_name)

    _load_root_modules()
    _load_helper_modules()
    _register_native_commands()
    _register_plugin_commands()
    _MODULES_LOADED = True
