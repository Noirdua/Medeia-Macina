from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Type

from PluginCore.base import Plugin

KNOWN_CMDLETS = frozenset({
    "search-file",
    "add-file",
    "delete-file",
    "download-file",
    "get-file",
    "get-metadata",
    "tag",
    "get-tag",
    "add-url",
    "delete-url",
    "get-url",
    "add-note",
    "set-note",
    "get-note",
    "delete-note",
    "add-relationship",
    "get-relationship",
    "delete-relationship",
    "auto-tag",
})

_CMDLET_METHODS = {
    "search-file": ("search",),
    "download-file": ("download", "download_url", "handle_url"),
    "add-file": ("upload", "resolve_pipe_result_download"),
    "delete-file": ("delete_file",),
    "get-tag": ("get_tag",),
    "tag": ("add_tag", "get_tag"),
    "get-url": ("get_url",),
    "add-url": ("add_url",),
    "delete-url": ("delete_url",),
    "get-note": ("get_note",),
    "add-note": ("set_note",),
    "set-note": ("set_note",),
    "delete-note": ("delete_note",),
}


@dataclass
class Issue:
    level: str
    code: str
    message: str


@dataclass
class PluginReport:
    name: str
    path: Path
    kind: str
    plugin_class: str = ""
    version: str = ""
    author: str = ""
    description: str = ""
    issues: List[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.level == "error" for issue in self.issues)

    @property
    def status(self) -> str:
        if any(issue.level == "error" for issue in self.issues):
            return "FAIL"
        if any(issue.level == "warning" for issue in self.issues):
            return "WARN"
        return "PASS"


def _issue(report: PluginReport, level: str, code: str, message: str) -> None:
    report.issues.append(Issue(level=level, code=code, message=message))


def _plugin_roots() -> Tuple[Path, ...]:
    from PluginCore.registry import _iter_external_plugin_dirs

    return _iter_external_plugin_dirs()


def iter_plugin_entries() -> List[Tuple[str, Path]]:
    seen: set[str] = set()
    entries: List[Tuple[str, Path]] = []
    for root in _plugin_roots():
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except Exception:
            continue
        for child in children:
            name = str(child.name or "").strip()
            if not name or name.startswith(".") or name.startswith("_"):
                continue
            if name.lower() in {"readme.md", "readme.txt"}:
                continue
            key = name.lower()
            if key in seen:
                continue
            if child.is_dir() and (child / "__init__.py").exists():
                seen.add(key)
                entries.append((child.name, child))
            elif child.is_file() and child.suffix.lower() == ".py" and child.stem != "__init__":
                seen.add(key)
                entries.append((child.stem, child))
    return entries


def _load_from_spec(module_name: str, file_path: Path, package_dir: Optional[Path] = None) -> Any:
    spec = importlib.util.spec_from_file_location(
        module_name,
        file_path,
        submodule_search_locations=[str(package_dir)] if package_dir is not None else None,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _in_plugin_root(path: Path) -> bool:
    try:
        resolved = path.resolve()
        return any(resolved.parent == root.resolve() for root in _plugin_roots())
    except Exception:
        return False


def _load_module(name: str, path: Path) -> Tuple[Optional[Any], Optional[str]]:
    try:
        if path.is_file():
            return _load_from_spec(f"_mm_plugin_validate_{name}", path), None
        if _in_plugin_root(path):
            return importlib.import_module(f"plugins.{path.name}"), None
        return _load_from_spec(
            f"_mm_plugin_validate_{path.name}",
            path / "__init__.py",
            path,
        ), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _is_plugin_class(candidate: Any) -> bool:
    return (
        inspect.isclass(candidate)
        and issubclass(candidate, Plugin)
        and candidate is not Plugin
    )


def _overrides(plugin_class: Type[Plugin], method_name: str) -> bool:
    try:
        method = getattr(plugin_class, method_name, None)
        base = getattr(Plugin, method_name, None)
    except Exception:
        return False
    return callable(method) and method is not base


def _check_name(plugin_class: Type[Plugin], folder_name: str, report: PluginReport) -> None:
    raw = str(getattr(plugin_class, "PLUGIN_NAME", "") or "").strip()
    folder_key = str(folder_name or "").strip().lower()
    class_key = plugin_class.__name__.strip().lower()
    if not raw:
        if folder_key and class_key not in {folder_key, folder_key.replace("_", "")}:
            _issue(
                report,
                "warning",
                "plugin_name",
                "PLUGIN_NAME is empty; the class name will be used instead",
            )
        return
    lowered = raw.lower()
    if any(ch in raw for ch in "\\/ "):
        _issue(report, "error", "plugin_name", f"PLUGIN_NAME {raw!r} contains spaces or path separators")
        return
    folder_key = str(folder_name or "").strip().lower()
    if folder_key and lowered not in {folder_key, folder_key.replace("_", ""), folder_key.replace("-", "")}:
        aliases = {
            str(alias).strip().lower()
            for alias in (getattr(plugin_class, "PLUGIN_ALIASES", ()) or ())
            if str(alias).strip()
        }
        if folder_key not in aliases and lowered != folder_key:
            _issue(
                report,
                "warning",
                "plugin_name",
                f"PLUGIN_NAME {raw!r} does not match folder {folder_name!r}",
            )


def _has_cmdlet_method(plugin_class: Type[Plugin], methods: Sequence[str], instance: Any = None) -> bool:
    if any(_overrides(plugin_class, method) for method in methods):
        return True
    if instance is None:
        return False
    for method in methods:
        try:
            value = getattr(instance, method)
        except AttributeError:
            value = None
        except Exception:
            value = None
        if callable(value):
            return True
    return False


def _check_cmdlets(plugin_class: Type[Plugin], report: PluginReport, instance: Any = None) -> None:
    raw = getattr(plugin_class, "SUPPORTED_CMDLETS", frozenset())
    try:
        cmdlets = [str(item).strip().lower() for item in (raw or ()) if str(item).strip()]
    except Exception:
        _issue(report, "error", "cmdlets", "SUPPORTED_CMDLETS must be an iterable of command names")
        return
    if not cmdlets:
        _issue(report, "warning", "cmdlets", "SUPPORTED_CMDLETS is empty")
        return
    unknown = [name for name in cmdlets if name not in KNOWN_CMDLETS]
    if unknown:
        _issue(
            report,
            "warning",
            "cmdlets",
            "unknown cmdlet names: " + ", ".join(sorted(unknown)),
        )
    for cmdlet_name, methods in _CMDLET_METHODS.items():
        if cmdlet_name not in cmdlets:
            continue
        if _has_cmdlet_method(plugin_class, methods, instance):
            continue
        _issue(
            report,
            "warning",
            "cmdlets",
            f"declares {cmdlet_name} but does not override {' / '.join(methods)}",
        )


def _check_schema(plugin_class: Type[Plugin], report: PluginReport) -> None:
    schema_fn = getattr(plugin_class, "config_schema", None)
    if not callable(schema_fn):
        return
    try:
        schema = schema_fn()
    except Exception as exc:
        _issue(report, "error", "schema", f"config_schema() raised {type(exc).__name__}: {exc}")
        return
    if schema in (None, []):
        if bool(getattr(plugin_class, "MULTI_INSTANCE", False)):
            _issue(report, "warning", "schema", "MULTI_INSTANCE is set but config_schema() is empty")
        return
    if not isinstance(schema, (list, tuple)):
        _issue(report, "error", "schema", "config_schema() must return a list")
        return
    for index, field in enumerate(schema):
        if not isinstance(field, dict) or not str(field.get("key") or "").strip():
            _issue(report, "error", "schema", f"config_schema()[{index}] needs a key")
            break


def _check_detail_fields(plugin_class: Type[Plugin], report: PluginReport) -> None:
    rows = getattr(plugin_class, "ITEM_DETAIL_FIELDS", ()) or ()
    try:
        items = list(rows)
    except Exception:
        _issue(report, "error", "detail", "ITEM_DETAIL_FIELDS must be a sequence")
        return
    for index, spec in enumerate(items):
        if not isinstance(spec, (list, tuple)) or not (2 <= len(spec) <= 3):
            _issue(
                report,
                "error",
                "detail",
                f"ITEM_DETAIL_FIELDS[{index}] must be (label, key) or (label, key, after)",
            )
            return


def _check_url_decls(plugin_class: Type[Plugin], report: PluginReport) -> None:
    url = getattr(plugin_class, "URL", ())
    if url not in (None, ()) and not isinstance(url, (list, tuple, str)):
        _issue(report, "warning", "url", "URL should be a string or sequence of strings")
    domains = getattr(plugin_class, "URL_DOMAINS", ())
    if domains not in (None, ()) and not isinstance(domains, (list, tuple)):
        _issue(report, "warning", "url", "URL_DOMAINS should be a sequence of hosts")


def _validate_plugin_class(
    plugin_class: Type[Plugin],
    folder_name: str,
    report: PluginReport,
) -> None:
    report.plugin_class = plugin_class.__name__
    report.version = str(getattr(plugin_class, "PLUGIN_VERSION", "") or "").strip()
    report.author = str(getattr(plugin_class, "PLUGIN_AUTHOR", "") or "").strip()
    report.description = str(getattr(plugin_class, "PLUGIN_DESCRIPTION", "") or "").strip()
    _check_name(plugin_class, folder_name, report)
    _check_schema(plugin_class, report)
    _check_detail_fields(plugin_class, report)
    _check_url_decls(plugin_class, report)
    try:
        instance = plugin_class({})
    except Exception as exc:
        _issue(report, "error", "init", f"Plugin() raised {type(exc).__name__}: {exc}")
        _check_cmdlets(plugin_class, report)
        return
    _check_cmdlets(plugin_class, report, instance)
    try:
        ready = instance.validate()
    except Exception as exc:
        _issue(report, "warning", "validate", f"validate() raised {type(exc).__name__}: {exc}")
        return
    if not isinstance(ready, bool):
        _issue(report, "warning", "validate", "validate() should return bool")


def _check_command_help(cmdlet_obj: Any, report: PluginReport) -> None:
    name = str(getattr(cmdlet_obj, "name", "") or "").strip() or "command"
    if not str(getattr(cmdlet_obj, "summary", "") or "").strip():
        _issue(report, "warning", "help", f"{name} is missing summary")
    if not str(getattr(cmdlet_obj, "usage", "") or "").strip():
        _issue(report, "warning", "help", f"{name} is missing usage")
    examples = getattr(cmdlet_obj, "examples", None) or []
    if not examples:
        _issue(report, "warning", "help", f"{name} is missing examples")


def _check_plugin_commands(path: Path, plugin_class: Type[Plugin], report: PluginReport) -> None:
    from PluginCore.commands import iter_command_objects

    commands_path = path / "commands.py" if path.is_dir() else None
    if commands_path is not None and commands_path.exists():
        try:
            if _in_plugin_root(path):
                module = importlib.import_module(f"plugins.{path.name}.commands")
            else:
                module = _load_from_spec(
                    f"_mm_plugin_validate_{path.name}_commands",
                    commands_path,
                    path,
                )
            for obj in iter_command_objects(module):
                _check_command_help(obj, report)
        except Exception as exc:
            _issue(report, "warning", "help", f"commands.py failed to load: {exc}")
    actions: Dict[str, Any] = {}
    file_actions = getattr(plugin_class, "FILE_ACTIONS", None) or {}
    if isinstance(file_actions, dict):
        actions.update(file_actions)
    metadata_actions = getattr(plugin_class, "METADATA_ACTIONS", None) or {}
    if isinstance(metadata_actions, dict):
        actions.update(metadata_actions)
    if not actions:
        return
    for spec in actions.values():
        if not isinstance(spec, dict):
            continue
        module_name = str(spec.get("module") or "").strip()
        if not module_name:
            continue
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        obj = getattr(module, "CMDLET", None)
        if obj is not None:
            _check_command_help(obj, report)


def validate_entry(name: str, path: Path) -> PluginReport:
    report = PluginReport(name=name, path=path, kind="plugin")
    if path.is_file():
        _issue(
            report,
            "warning",
            "layout",
            "single-file plugin; prefer plugins/<name>/__init__.py",
        )
    elif not (path / "__init__.py").exists():
        _issue(report, "error", "layout", "plugin folder is missing __init__.py")
        report.kind = "missing"
        return report

    module, error = _load_module(name, path)
    if error:
        _issue(report, "error", "import", error)
        return report

    owned: List[Type[Plugin]] = []
    imported: List[Type[Plugin]] = []
    seen: set[int] = set()
    for candidate in vars(module).values():
        if not _is_plugin_class(candidate):
            continue
        marker = id(candidate)
        if marker in seen:
            continue
        seen.add(marker)
        if getattr(candidate, "__module__", "") == getattr(module, "__name__", ""):
            owned.append(candidate)
        else:
            imported.append(candidate)

    folder_name = path.stem if path.is_file() else path.name
    if owned:
        if len(owned) > 1:
            _issue(
                report,
                "warning",
                "class",
                "multiple Plugin subclasses in one module: "
                + ", ".join(cls.__name__ for cls in owned),
            )
        _validate_plugin_class(owned[0], folder_name, report)
        _check_plugin_commands(path, owned[0], report)
        return report

    folder_key = str(folder_name or "").strip().lower()
    aliases = []
    for cls in imported:
        names = {
            str(getattr(cls, "PLUGIN_NAME", "") or "").strip().lower(),
            cls.__name__.lower(),
        }
        names.update(
            str(alias).strip().lower()
            for alias in (getattr(cls, "PLUGIN_ALIASES", ()) or ())
            if str(alias).strip()
        )
        if folder_key in names:
            aliases.append(cls)
    if aliases:
        report.kind = "alias"
        report.plugin_class = aliases[0].__name__
        _issue(
            report,
            "info",
            "alias",
            "re-exports " + ", ".join(cls.__name__ for cls in aliases),
        )
        return report

    report.kind = "support"
    _issue(report, "info", "support", "no Plugin subclass; treated as a support package")
    return report


def resolve_targets(names: Sequence[str] | None = None) -> List[Tuple[str, Path]]:
    discovered = {key.lower(): (key, path) for key, path in iter_plugin_entries()}
    if not names:
        return [(key, path) for key, path in discovered.values()]

    resolved: List[Tuple[str, Path]] = []
    for raw in names:
        text = str(raw or "").strip()
        if not text:
            continue
        candidate = Path(text).expanduser()
        try:
            if candidate.exists():
                label = candidate.stem if candidate.is_file() else candidate.name
                resolved.append((label, candidate.resolve()))
                continue
        except Exception:
            pass
        hit = discovered.get(text.lower())
        if hit is None:
            resolved.append((text, Path(text)))
            continue
        resolved.append(hit)
    return resolved


def validate_plugins(names: Sequence[str] | None = None) -> List[PluginReport]:
    reports: List[PluginReport] = []
    for name, path in resolve_targets(names):
        if not path.exists():
            report = PluginReport(name=name, path=path, kind="missing")
            _issue(report, "error", "missing", f"plugin not found: {name}")
            reports.append(report)
            continue
        reports.append(validate_entry(name, path))
    return reports


def format_reports(reports: Iterable[PluginReport]) -> str:
    rows = list(reports)
    if not rows:
        return "No plugins found."
    lines = ["PLUGIN                  KIND      STATUS  NOTES"]
    for report in rows:
        notes = "; ".join(
            f"{issue.level}:{issue.message}"
            for issue in report.issues
            if issue.level != "info" or report.kind != "plugin"
        )
        if report.kind == "plugin" and report.ok and not notes:
            notes = report.plugin_class or "ok"
        lines.append(
            f"{report.name:<22} {report.kind:<9} {report.status:<7} {notes}".rstrip()
        )
    failed = sum(1 for report in rows if not report.ok)
    warned = sum(1 for report in rows if report.ok and report.status == "WARN")
    lines.append(f"{len(rows)} checked, {failed} failed, {warned} warned")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m PluginCore.validate",
        description="Validate Medeia-Macina plugins (layout, Plugin subclass, cmdlets, schema).",
    )
    parser.add_argument(
        "names",
        nargs="*",
        help="Plugin names or paths. Default: every discovered plugin folder.",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Only print failures")
    args = parser.parse_args(list(argv) if argv is not None else None)
    reports = validate_plugins(args.names)
    visible = [report for report in reports if not report.ok] if args.quiet else reports
    print(format_reports(visible if args.quiet else reports))
    return 1 if any(not report.ok for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
