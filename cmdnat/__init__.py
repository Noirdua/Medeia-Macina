from __future__ import annotations

import os
from importlib import import_module
from typing import Any, Callable, Dict, Sequence

from SYS.cmdlet_spec import collect_registered_cmdlet_names

CmdletFn = Callable[[Any, Sequence[str], Dict[str, Any]], int]


def _register_cmdlet_object(cmdlet_obj, registry: Dict[str, CmdletFn]) -> None:
    run_fn = getattr(cmdlet_obj, "exec", None) if hasattr(cmdlet_obj, "exec") else None
    if not callable(run_fn):
        return

    for registered_name in collect_registered_cmdlet_names(cmdlet_obj):
        registry[registered_name] = run_fn


def _iter_legacy_native_module_names() -> list[str]:
    base_dir = os.path.dirname(__file__)
    module_names: list[str] = []
    for filename in os.listdir(base_dir):
        if not (filename.endswith(".py") and not filename.startswith("_")
                and filename != "__init__.py"):
            continue
        module_names.append(filename[:-3])
    return module_names


def register_native_commands(registry: Dict[str, CmdletFn]) -> None:
    """Import legacy local command modules from cmdnat/ and register them."""
    for mod_name in _iter_legacy_native_module_names():
        try:
            module = import_module(f".{mod_name}", __name__)
            cmdlet_obj = getattr(module, "CMDLET", None)
            if cmdlet_obj:
                _register_cmdlet_object(cmdlet_obj, registry)
        except Exception as exc:
            import sys

            print(
                f"Error importing native command '{mod_name}': {exc}",
                file=sys.stderr
            )
            continue
