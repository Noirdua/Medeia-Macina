from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Sequence

from SYS.cmdlet_spec import Cmdlet, CmdletArg
from SYS import pipeline as ctx
from SYS.result_table import Table
from SYS.result_table_helpers import add_row_columns
from cmdnat._parsing import extract_arg_value, has_flag
from PluginCore.validate import validate_plugins
from PluginCore.lifecycle import uninstall_plugins
from PluginCore.source import (
    available_plugins,
    install_from_catalog,
    plugin_source_branch,
    plugin_source_url,
    sync_plugin_source,
)

CMDLET = Cmdlet(
    name=".plugin",
    alias=["plugin", "plugins"],
    summary="List, install, validate, or uninstall plugins",
    usage=".plugin [name ...] [-available] [-add|-install] [-update] [-source URL] [-remove|-uninstall] [-orphans] [-files] [-validate]",
    arg=[
        CmdletArg("-available", type="flag", description="List plugins in the configured source repository"),
        CmdletArg("-add", type="flag", alias="install", description="Install named plugins from the source repository"),
        CmdletArg("-update", type="flag", description="Refresh the source cache and reinstall named (or installed catalog) plugins"),
        CmdletArg("-force", type="flag", description="With -update/-add: recopy even if fingerprints match"),
        CmdletArg("-source", type="string", description="Show or set the plugin source git URL"),
        CmdletArg("-validate", type="flag", description="Validate plugin layout and contract"),
        CmdletArg(
            "-remove",
            type="flag",
            alias="uninstall",
            description="Remove plugin config (use -orphans for leftover config, -files to delete the plugin folder)",
        ),
        CmdletArg("-orphans", type="flag", description="With -remove: drop config for plugins that are not installed"),
        CmdletArg("-files", type="flag", description="With -remove: also delete the plugin folder"),
    ],
)


def _flag(args: Sequence[str], *flags: str) -> bool:
    return any(has_flag(args, flag) for flag in flags)


def _print_table(table: Table, items: Optional[List[Any]] = None) -> None:
    try:
        table.set_source_command(".plugin")
    except Exception:
        pass
    ctx.set_current_stage_table(table)
    try:
        ctx.set_last_result_table(table, items or [])
    except Exception:
        pass


def _name_from_item(item: Any) -> Optional[str]:
    if item is None:
        return None
    if isinstance(item, str):
        text = item.strip()
        if text and not text.startswith("-"):
            return text
        return None
    extra = getattr(item, "extra", None)
    if isinstance(extra, dict):
        for key in ("Plugin", "plugin"):
            value = extra.get(key)
            if value:
                return str(value).strip()
    if isinstance(item, dict):
        for key in ("Plugin", "plugin"):
            value = item.get(key)
            if value:
                return str(value).strip()
        args = item.get("_selection_args")
        if isinstance(args, (list, tuple)):
            for token in args:
                text = str(token or "").strip()
                if text and not text.startswith("-"):
                    return text
    getter = getattr(item, "get_column", None)
    if callable(getter):
        for key in ("Plugin", "plugin"):
            value = getter(key)
            if value:
                return str(value).strip()
    args = getattr(item, "selection_args", None)
    if isinstance(args, (list, tuple)):
        for token in args:
            text = str(token or "").strip()
            if text and not text.startswith("-"):
                return text
    return None


def _names_from_result(result: Any) -> List[str]:
    if result is None:
        return []
    items = result if isinstance(result, (list, tuple)) else [result]
    names: List[str] = []
    seen: set[str] = set()
    for item in items:
        name = _name_from_item(item)
        key = str(name or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(str(name).strip())
    return names


def _merge_names(*groups: Sequence[str]) -> List[str]:
    names: List[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group or ():
            text = str(raw or "").strip()
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            names.append(text)
    return names


def _add_named_row(table: Table, columns: List[tuple[str, Any]], name: str) -> Dict[str, Any]:
    add_row_columns(table, columns)
    idx = len(table.rows) - 1
    try:
        table.set_row_selection_args(idx, [name])
    except Exception:
        pass
    return {"Plugin": name, "_selection_args": [name]}


def _positional_names(args: Sequence[str]) -> List[str]:
    skip_next = False
    names: List[str] = []
    value_flags = {"-source", "--source", "-branch", "--branch"}
    for token in args or []:
        text = str(token).strip()
        if skip_next:
            skip_next = False
            continue
        if not text:
            continue
        low = text.lower()
        if low in value_flags:
            skip_next = True
            continue
        if low.startswith("-"):
            continue
        names.append(text)
    return names


def _run_remove(names: List[str], args: List[str], config: Dict[str, Any]) -> int:
    orphans = _flag(args, "-orphans", "--orphans")
    delete_files = _flag(args, "-files", "--files")
    if not names and not orphans:
        from SYS.logger import log

        log("Specify plugin names, pipe @N rows, or use -orphans", file=sys.stderr)
        return 1
    results = uninstall_plugins(
        config,
        names,
        orphans=orphans,
        delete_files=delete_files or bool(names),
    )
    table = Table("Uninstalled plugins")
    table._interactive(True)._perseverance(True)
    items: List[Any] = []
    if not results:
        add_row_columns(table, [("Detail", "Nothing to remove")])
    for row in results:
        name = str(row.get("name") or "").strip()
        items.append(
            _add_named_row(
                table,
                [
                    ("Plugin", name),
                    ("Config", "yes" if row.get("config") else "no"),
                    ("Files", "yes" if row.get("files") else "no"),
                    ("Detail", row.get("detail") or ""),
                ],
                name or "plugin",
            )
        )
    _print_table(table, items)
    try:
        from PluginCore.registry import refresh_discovered_plugins

        refresh_discovered_plugins()
    except Exception:
        pass
    return 0


def _run_source(args: List[str], config: Dict[str, Any]) -> int:
    from SYS.config import save_config_and_verify

    url = extract_arg_value(args, flags=["-source", "--source"])
    branch = extract_arg_value(args, flags=["-branch", "--branch"])
    changed = False
    if url:
        config["plugin_source"] = url
        changed = True
    if branch:
        config["plugin_source_branch"] = branch
        changed = True
    if changed:
        save_config_and_verify(config)
    table = Table("Plugin source")
    table._interactive(True)._perseverance(True)
    add_row_columns(
        table,
        [
            ("URL", plugin_source_url(config)),
            ("Branch", plugin_source_branch(config)),
        ],
    )
    _print_table(table)
    return 0


def _run_available(config: Dict[str, Any]) -> int:
    try:
        rows = available_plugins(config)
    except Exception as exc:
        from SYS.logger import log

        log(f"Plugin source update failed: {exc}", file=sys.stderr)
        return 1
    table = Table("Available plugins")
    table._interactive(True)._perseverance(True)
    items: List[Any] = []
    if not rows:
        add_row_columns(
            table,
            [
                ("Source", plugin_source_url(config)),
                ("Detail", "No plugin folders in the git source yet"),
            ],
        )
    for row in rows:
        name = str(row.get("name") or "").strip()
        items.append(
            _add_named_row(
                table,
                [
                    ("Plugin", name),
                    ("Version", row.get("version") or ""),
                    ("Author", row.get("author") or ""),
                    ("Description", row.get("description") or ""),
                    ("Installed", "yes" if row.get("installed") else "no"),
                ],
                name,
            )
        )
    _print_table(table, items)
    return 0


def _run_install(names: List[str], config: Dict[str, Any], *, update: bool, force: bool = False) -> int:
    from PluginCore.lifecycle import installed_plugin_keys

    if update:
        try:
            sync_plugin_source(config)
        except Exception as exc:
            from SYS.logger import log

            log(f"Plugin source update failed: {exc}", file=sys.stderr)
            return 1
        if not names:
            catalog_names = [row["name"] for row in available_plugins(config)]
            installed = installed_plugin_keys()
            names = [name for name in catalog_names if name.lower() in installed]
    if not names:
        from SYS.logger import log

        log("Specify plugin names or pipe @N rows from .plugin -available", file=sys.stderr)
        return 1
    try:
        from PluginCore.source import missing_plugin_depends

        missing = missing_plugin_depends(names, config)
    except Exception:
        missing = []
    if missing:
        from SYS.logger import log
        import sys

        needed = ", ".join(missing)
        log(
            f"This plugin depends on: {needed}. Install those too, or cancel.",
            file=sys.stderr,
        )
        if sys.stdin.isatty():
            try:
                answer = input("Install missing plugins? [Y/n]: ").strip().lower()
            except Exception:
                answer = "n"
            if answer in {"n", "no"}:
                log("Cancelled.", file=sys.stderr)
                return 1
        names = list(missing) + list(names)
    try:
        results = install_from_catalog(names, config, force=force or not update)
    except Exception as exc:
        from SYS.logger import log

        log(f"Plugin install failed: {exc}", file=sys.stderr)
        return 1
    table = Table("Installed plugins" if not update else "Updated plugins")
    table._interactive(True)._perseverance(True)
    items: List[Any] = []
    for row in results:
        name = str(row.get("name") or "").strip()
        items.append(
            _add_named_row(
                table,
                [
                    ("Plugin", name),
                    ("Status", row.get("status") or ("ok" if row.get("ok") else "fail")),
                    ("From", row.get("from") or ""),
                    ("To", row.get("to") or ""),
                    ("Source", row.get("source") or ""),
                    ("Detail", row.get("detail") or ""),
                ],
                name or "plugin",
            )
        )
    _print_table(table, items)
    try:
        from PluginCore.registry import refresh_discovered_plugins

        refresh_discovered_plugins()
    except Exception:
        pass
    try:
        from SYS.cli_completer import clear_completer_plugin_caches

        clear_completer_plugin_caches()
    except Exception:
        pass
    return 0 if all(row.get("ok") for row in results) else 1


def _run(result: Any, args: List[str], config: Dict[str, Any]) -> int:
    names = _merge_names(_positional_names(args), _names_from_result(result))
    if _flag(args, "-remove", "--remove", "-uninstall", "--uninstall"):
        return _run_remove(names, list(args), config)
    if _flag(args, "-source", "--source") and not _flag(args, "-add", "--add", "-install", "--install", "-available", "--available"):
        return _run_source(list(args), config)
    if _flag(args, "-available", "--available"):
        return _run_available(config)
    force = _flag(args, "-force", "--force")
    if _flag(args, "-add", "--add", "-install", "--install"):
        return _run_install(names, config, update=False, force=force)
    if _flag(args, "-update", "--update"):
        return _run_install(names, config, update=True, force=force)

    reports = validate_plugins(names or None)
    table = Table("Plugins")
    table._interactive(True)._perseverance(True)
    items: List[Any] = []
    for report in reports:
        notes = "; ".join(
            issue.message
            for issue in report.issues
            if issue.level in {"error", "warning"}
        )
        if not notes:
            notes = report.plugin_class or report.kind
        items.append(
            _add_named_row(
                table,
                [
                    ("Plugin", report.name),
                    ("Version", report.version),
                    ("Author", report.author),
                    ("Description", report.description),
                    ("Status", report.status),
                    ("Notes", notes),
                ],
                report.name,
            )
        )
    _print_table(table, items)
    return 0 if all(report.ok for report in reports) else 1


CMDLET.exec = _run
