from __future__ import annotations

import importlib.util
from importlib import import_module
import pkgutil
from typing import Any, Callable, Dict, Iterable, Sequence


CmdletFn = Callable[[Any, Sequence[str], Dict[str, Any]], int]


def iter_command_objects(module: Any) -> list[Any]:
    objects: list[Any] = []

    many = getattr(module, "COMMANDS", None)
    if isinstance(many, (list, tuple)):
        for item in many:
            if item is not None:
                objects.append(item)

    single = getattr(module, "COMMAND", None)
    if single is not None:
        objects.append(single)

    legacy = getattr(module, "CMDLET", None)
    if legacy is not None:
        objects.append(legacy)

    deduped: list[Any] = []
    seen: set[int] = set()
    for item in objects:
        marker = id(item)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(item)
    return deduped


def get_primary_command_object(module: Any) -> Any:
    commands = iter_command_objects(module)
    return commands[0] if commands else None


def _register_command_object(cmdlet_obj: Any, registry: Dict[str, CmdletFn]) -> None:
    run_fn = getattr(cmdlet_obj, "exec", None) if hasattr(cmdlet_obj, "exec") else None
    if not callable(run_fn):
        return

    name = getattr(cmdlet_obj, "name", None)
    if name:
        registry[str(name).replace("_", "-").lower()] = run_fn

    aliases: list[str] = []
    if hasattr(cmdlet_obj, "alias") and getattr(cmdlet_obj, "alias"):
        aliases.extend(getattr(cmdlet_obj, "alias") or [])
    if hasattr(cmdlet_obj, "aliases") and getattr(cmdlet_obj, "aliases"):
        aliases.extend(getattr(cmdlet_obj, "aliases") or [])

    for alias in aliases:
        text = str(alias or "").strip()
        if text:
            registry[text.replace("_", "-").lower()] = run_fn


def iter_plugin_command_module_names() -> list[str]:
    try:
        package = import_module("plugins")
    except Exception:
        return []

    package_path = getattr(package, "__path__", None)
    if not package_path:
        return []

    module_names: list[str] = []
    seen: set[str] = set()
    for _, module_name, is_package in pkgutil.iter_modules(package_path):
        if not is_package or module_name.startswith("_"):
            continue
        commands_module = f"plugins.{module_name}.commands"
        try:
            if importlib.util.find_spec(commands_module) is None:
                continue
        except Exception:
            continue
        if commands_module in seen:
            continue
        seen.add(commands_module)
        module_names.append(commands_module)
    return module_names


def register_plugin_commands(registry: Dict[str, CmdletFn]) -> None:
    for module_name in iter_plugin_command_module_names():
        try:
            module = import_module(module_name)
            for cmdlet_obj in iter_command_objects(module):
                _register_command_object(cmdlet_obj, registry)
        except Exception as exc:
            import sys

            print(
                f"Error importing plugin command '{module_name}': {exc}",
                file=sys.stderr,
            )
            continue