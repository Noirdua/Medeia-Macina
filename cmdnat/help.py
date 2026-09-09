from __future__ import annotations

from typing import Any, Dict, Sequence, List, Optional, Tuple
import shlex
import sys

from SYS.cmdlet_spec import Cmdlet, CmdletArg, collect_registered_cmdlet_names, parse_cmdlet_args
from cmdlet import REGISTRY as CMDLET_REGISTRY, ensure_cmdlet_modules_loaded
from SYS.logger import log
from SYS.result_table import Table
from SYS import pipeline as ctx


def _normalize_choice_list(arg_names: Optional[List[str]]) -> List[str]:
    return sorted(set(arg_names or []))


_HELP_EXAMPLE_SOURCE_COMMAND = ".help-example"
_METADATA_CACHE_KEY: Optional[Tuple[int, int]] = None
_METADATA_CACHE_VALUE: Optional[Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]] = None

# Implementation cmdlets stay runnable; they are not listed in the index because
# `file` and `metadata` already cover them.
_HELP_INDEX_HIDDEN = {
    "search-file",
    "add-file",
    "delete-file",
    "download-file",
    "merge-file",
    "convert-file",
    "trim-file",
    "archive-file",
    "screen-shot",
    "screenshot",
    "tag",
    "get-tag",
    "add-url",
    "delete-url",
    "get-url",
    "add-note",
    "get-note",
    "delete-note",
    "add-relationship",
    "get-relationship",
    "delete-relationship",
    "get-metadata",
    "auto-tag",
}

_HELP_GROUP_ORDER = ("native", "file", "metadata", "plugin")

_HELP_IMPL_TO_PUBLIC = {
    "search-file": "file",
    "add-file": "file",
    "delete-file": "file",
    "download-file": "file",
    "merge-file": "file",
    "convert-file": "file",
    "trim-file": "file",
    "archive-file": "file",
    "screen-shot": "file",
    "screenshot": "file",
    "tag": "metadata",
    "get-tag": "metadata",
    "add-url": "metadata",
    "delete-url": "metadata",
    "get-url": "metadata",
    "add-note": "metadata",
    "get-note": "metadata",
    "delete-note": "metadata",
    "add-relationship": "metadata",
    "get-relationship": "metadata",
    "delete-relationship": "metadata",
    "get-metadata": "metadata",
    "auto-tag": "metadata",
}


def _help_group(name: str) -> str:
    key = _normalize_cmdlet_key(name)
    if key in {".help", ".config", ".status", ".worker", ".table", ".adjective", ".plugin"}:
        return "native"
    if key == "file":
        return "file"
    if key == "metadata":
        return "metadata"
    return "plugin"


def _example_for_cmd(name: str) -> List[str]:
    """Return example invocations for a given command (best-effort)."""
    lookup = {
        ".adjective": [
            '.adjective -add "example"',
            '.adjective -delete "example"',
        ],
    }

    key = name.replace("_", "-").lower()
    return lookup.get(key, [])


def _parse_example_tokens(example: str) -> List[str]:
    """Split an example string into CLI tokens suitable for @N selection."""

    text = str(example or "").strip()
    if not text:
        return []

    try:
        tokens = shlex.split(text)
    except Exception:
        tokens = text.split()

    return [token for token in tokens if token]


def _normalize_cmdlet_key(name: Optional[str]) -> str:
    return str(name or "").replace("_", "-").lower().strip()


def _cmdlet_aliases(cmdlet_obj: Cmdlet) -> List[str]:
    canonical_name = _normalize_cmdlet_key(getattr(cmdlet_obj, "name", None))
    return [
        registered_name
        for registered_name in collect_registered_cmdlet_names(cmdlet_obj)
        if registered_name != canonical_name
    ]


def _cmdlet_arg_to_dict(arg: CmdletArg) -> Dict[str, Any]:
    return {
        "name": str(getattr(arg, "name", "") or ""),
        "type": str(getattr(arg, "type", "") or ""),
        "required": bool(getattr(arg, "required", False)),
        "description": str(getattr(arg, "description", "") or ""),
        "choices": [str(c) for c in list(getattr(arg, "choices", []) or [])],
        "alias": str(getattr(arg, "alias", "") or ""),
        "variadic": bool(getattr(arg, "variadic", False)),
        "usage": str(getattr(arg, "usage", "") or ""),
        "query_key": getattr(arg, "query_key", None),
        "query_aliases": [str(c) for c in list(getattr(arg, "query_aliases", []) or [])],
        "query_only": bool(getattr(arg, "query_only", False)),
        "requires_db": bool(getattr(arg, "requires_db", False)),
    }


def _build_alias_map_from_metadata(metadata: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for name, meta in metadata.items():
        canonical = _normalize_cmdlet_key(name)
        if canonical:
            mapping[canonical] = name
        for alias in meta.get("aliases", []) or []:
            alias_key = _normalize_cmdlet_key(alias)
            if alias_key:
                mapping[alias_key] = name
    return mapping


def _gather_metadata_from_cmdlet_classes() -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    global _METADATA_CACHE_KEY, _METADATA_CACHE_VALUE

    cache_key = (len(sys.modules), len(CMDLET_REGISTRY))
    if _METADATA_CACHE_KEY == cache_key and _METADATA_CACHE_VALUE is not None:
        cached_metadata, cached_alias = _METADATA_CACHE_VALUE
        return dict(cached_metadata), dict(cached_alias)

    metadata: Dict[str, Dict[str, Any]] = {}
    alias_map: Dict[str, str] = {}
    try:
        ensure_cmdlet_modules_loaded()
    except Exception:
        pass

    for module in list(sys.modules.values()):
        mod_name = getattr(module, "__name__", "") or ""
        if not (mod_name.startswith("cmdlet.") or mod_name == "cmdlet" or mod_name.startswith("cmdnat.")):
            continue
        cmdlet_obj = getattr(module, "CMDLET", None)
        if cmdlet_obj is None or not hasattr(cmdlet_obj, "name") or not hasattr(cmdlet_obj, "arg"):
            continue
        canonical_key = _normalize_cmdlet_key(getattr(cmdlet_obj, "name", None) or "")
        if not canonical_key:
            continue
        example_entries: List[str] = []
        seen_example_entries: set[str] = set()
        for attr in ("examples", "example"):
            for value in (getattr(cmdlet_obj, attr, []) or []):
                text = str(value or "").strip()
                if not text or text in seen_example_entries:
                    continue
                seen_example_entries.add(text)
                example_entries.append(text)

        entry = {
            "name": str(getattr(cmdlet_obj, "name", "") or canonical_key),
            "summary": str(getattr(cmdlet_obj, "summary", "") or ""),
            "usage": str(getattr(cmdlet_obj, "usage", "") or ""),
            "aliases": _cmdlet_aliases(cmdlet_obj),
            "details": list(getattr(cmdlet_obj, "detail", []) or []),
            "args": [_cmdlet_arg_to_dict(a) for a in getattr(cmdlet_obj, "arg", []) or []],
            "examples": example_entries,
            "raw": cmdlet_obj,
        }
        metadata[canonical_key] = entry
        alias_map[canonical_key] = canonical_key
        for alias in entry["aliases"]:
            alias_key = _normalize_cmdlet_key(alias)
            if alias_key:
                alias_map[alias_key] = canonical_key

    covered_execs: set[Any] = set()
    for meta in metadata.values():
        raw = meta.get("raw")
        run_fn = getattr(raw, "exec", None) if raw is not None else None
        if callable(run_fn):
            covered_execs.add(run_fn)
        elif callable(raw):
            covered_execs.add(raw)

    for registry_name, run_fn in list(CMDLET_REGISTRY.items()):
        normalized = _normalize_cmdlet_key(registry_name)
        if not normalized:
            continue
        if normalized in alias_map:
            continue
        if callable(run_fn) and run_fn in covered_execs:
            owner = None
            for canonical, meta in metadata.items():
                raw = meta.get("raw")
                owner_fn = getattr(raw, "exec", None) if raw is not None else raw
                if owner_fn is run_fn:
                    owner = canonical
                    break
            if owner:
                aliases = list(metadata[owner].get("aliases") or [])
                if normalized not in aliases and normalized != owner:
                    aliases.append(normalized)
                    metadata[owner]["aliases"] = sorted(aliases)
                alias_map[normalized] = owner
            continue
        alias_map[normalized] = normalized
        metadata.setdefault(
            normalized,
            {
                "name": normalized,
                "aliases": [],
                "usage": "",
                "summary": "",
                "details": [],
                "args": [],
                "raw": None,
            },
        )

    _METADATA_CACHE_KEY = cache_key
    _METADATA_CACHE_VALUE = (dict(metadata), dict(alias_map))

    return metadata, alias_map


def _find_cmd_metadata(name: str,
                       metadata: Dict[str,
                                      Dict[str,
                                           Any]]) -> Optional[Dict[str,
                                                                   Any]]:
    target = name.replace("_", "-").lower()
    public = _HELP_IMPL_TO_PUBLIC.get(target)
    if public:
        target = public
    for cmd_name, meta in metadata.items():
        if target == cmd_name:
            return meta
        aliases = meta.get("aliases", []) or []
        if target in aliases:
            return meta
    return None


def _render_list(
    metadata: Dict[str,
                   Dict[str,
                        Any]],
    filter_text: Optional[str],
    args: Sequence[str]
) -> None:
    table = Table("Help")
    table.set_header_line("Select @N for details. Use file -search / file -add and metadata -add for most work.")
    table.set_source_command(".help", list(args))

    items: List[Dict[str, Any]] = []
    needle = (filter_text or "").lower().strip()

    grouped: Dict[str, List[str]] = {key: [] for key in _HELP_GROUP_ORDER}
    for name in metadata.keys():
        canonical = _normalize_cmdlet_key(name)
        if canonical in _HELP_INDEX_HIDDEN:
            continue
        grouped.setdefault(_help_group(canonical), []).append(name)

    ordered_names: List[str] = []
    for group in _HELP_GROUP_ORDER:
        ordered_names.extend(sorted(grouped.get(group) or []))

    for name in ordered_names:
        meta = metadata[name]
        summary = meta.get("summary", "") or ""
        aliases = [a for a in (meta.get("aliases", []) or []) if a and a != name]
        haystack = " ".join([name, summary, " ".join(aliases)]).lower()
        if needle and needle not in haystack:
            continue

        row = table.add_row()
        row.add_column("Group", _help_group(name))
        row.add_column("Cmd", name)
        row.add_column("Aliases", ", ".join(aliases))
        row.add_column("Summary", summary)
        table.set_row_selection_args(len(table.rows) - 1, ["-cmd", name])
        items.append(meta)

    ctx.set_last_result_table(table, items)
    ctx.set_current_stage_table(table)
    setattr(table, "_rendered_by_cmdlet", True)
    from SYS.rich_display import stdout_console

    stdout_console().print(table)


def _render_detail(meta: Dict[str, Any], _args: Sequence[str]) -> None:
    cmd_name = str(meta.get("name", "") or "cmd")
    title = f"Help: {cmd_name}"
    summary = meta.get("summary", "")
    usage = meta.get("usage", "")
    aliases = meta.get("aliases", []) or []
    details = meta.get("details", []) or []
    seen_examples: set[str] = set()
    explicit_example: List[str] = []
    for attr in ("examples", "example"):
        for value in (meta.get(attr, []) or []):
            text = str(value or "").strip()
            if not text or text in seen_examples:
                continue
            seen_examples.add(text)
            explicit_example.append(text)

    fallback_example = _example_for_cmd(cmd_name)
    for fallback in fallback_example:
        text = str(fallback or "").strip()
        if not text or text in seen_examples:
            continue
        seen_examples.add(text)
        explicit_example.append(text)

    header_lines: List[str] = []
    if summary:
        header_lines.append(summary)
    if usage:
        header_lines.append(f"Usage: {usage}")
    if aliases:
        header_lines.append("Aliases: " + ", ".join(aliases))
    if details:
        header_lines.extend(str(line) for line in details if str(line).strip())
    if explicit_example:
        header_lines.append("Examples available below")

    args_meta = meta.get("args", []) or []

    args_table = Table(title)
    if header_lines:
        args_table.set_header_lines(header_lines)
    args_table._perseverance(True)
    args_table._interactive(True)

    if not args_meta:
        row = args_table.add_row()
        row.add_column("Arg", "(none)")
        row.add_column("Type", "")
        row.add_column("Req", "")
        row.add_column("Description", "")
    else:
        for arg in args_meta:
            row = args_table.add_row()
            name = arg.get("name") or ""
            row.add_column("Arg", f"-{name}" if name else "")
            row.add_column("Type", arg.get("type", ""))
            row.add_column("Req", "yes" if arg.get("required") else "")
            desc = arg.get("description", "") or ""
            choices = arg.get("choices", []) or []
            if choices:
                choice_text = f"choices: {', '.join(choices)}"
                desc = f"{desc} ({choice_text})" if desc else choice_text
            row.add_column("Description", desc)

    example_table = Table(f"{cmd_name} Examples")
    example_table._perseverance(True)
    example_table.set_header_line("Select @N to insert the example command into the REPL.")

    example_items: List[str] = []
    if explicit_example:
        for idx, example_cmd in enumerate(explicit_example):
            example_text = str(example_cmd or "").strip()
            row = example_table.add_row()
            row.add_column("Example", example_text or "(empty example)")
            example_items.append(example_text)
            if example_text:
                tokens = _parse_example_tokens(example_text)
                if tokens:
                    example_table.set_row_selection_args(idx, tokens)
    else:
        example_table._interactive(True)
        row = example_table.add_row()
        row.add_column("Example", "(no examples available)")

    ctx.set_last_result_table(example_table, example_items)
    ctx.set_current_stage_table(example_table)
    setattr(example_table, "_rendered_by_cmdlet", True)
    example_table.set_source_command(_HELP_EXAMPLE_SOURCE_COMMAND)
    from SYS.rich_display import stdout_console

    stdout_console().print()
    stdout_console().print(args_table)
    stdout_console().print()
    stdout_console().print(example_table)
    stdout_console().print()


def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    catalog: Any | None = None
    cmdlet_names: List[str] = []
    metadata: Dict[str, Dict[str, Any]] = {}
    alias_map: Dict[str, str] = {}

    try:
        from SYS import cmdlet_catalog as _catalog

        catalog = _catalog
    except Exception:
        catalog = None

    if catalog is not None:
        try:
            cmdlet_names = catalog.list_cmdlet_names(config=config)
        except Exception:
            cmdlet_names = []
        try:
            metadata = catalog.list_cmdlet_metadata(config=config)
        except Exception:
            metadata = {}

    if metadata:
        alias_map = _build_alias_map_from_metadata(metadata)
    else:
        metadata, alias_map = _gather_metadata_from_cmdlet_classes()

    if not metadata:
        fallback_names = sorted(set(cmdlet_names or list(CMDLET_REGISTRY.keys())))
        if fallback_names:
            base_meta: Dict[str, Dict[str, Any]] = {}
            for cmdname in fallback_names:
                canonical = str(cmdname or "").replace("_", "-").lower()
                entry: Dict[str, Any]
                candidate: Dict[str, Any] | None = None
                if catalog is not None:
                    try:
                        candidate = catalog.get_cmdlet_metadata(cmdname, config=config)
                    except Exception:
                        candidate = None
                if candidate:
                    canonical = candidate.get("name", canonical)
                    entry = candidate
                else:
                    entry = {
                        "name": canonical,
                        "aliases": [],
                        "usage": "",
                        "summary": "",
                        "details": [],
                        "args": [],
                        "raw": None,
                    }
                base = base_meta.setdefault(
                    canonical,
                    {
                        "name": canonical,
                        "aliases": [],
                        "usage": "",
                        "summary": "",
                        "details": [],
                        "args": [],
                        "raw": entry.get("raw"),
                    },
                )
                if entry.get("aliases"):
                    base_aliases = set(base.get("aliases", []))
                    base_aliases.update([a for a in entry.get("aliases", []) if a])
                    base["aliases"] = sorted(base_aliases)
                if not base.get("usage") and entry.get("usage"):
                    base["usage"] = entry["usage"]
                if not base.get("summary") and entry.get("summary"):
                    base["summary"] = entry["summary"]
                if not base.get("details") and entry.get("details"):
                    base["details"] = entry["details"]
                if not base.get("args") and entry.get("args"):
                    base["args"] = entry["args"]
                if not base.get("raw") and entry.get("raw"):
                    base["raw"] = entry["raw"]
            metadata = base_meta
            alias_map = _build_alias_map_from_metadata(metadata)

    choice_candidates = list(alias_map.keys()) if alias_map else list(metadata.keys())
    CMDLET.arg[0].choices = _normalize_choice_list(choice_candidates)
    parsed = parse_cmdlet_args(args, CMDLET)

    filter_text = parsed.get("filter")
    cmd_arg = parsed.get("cmd")

    if cmd_arg:
        target_meta = _find_cmd_metadata(str(cmd_arg), metadata)
        if not target_meta:
            log(f"Unknown command: {cmd_arg}", file=sys.stderr)
            return 1
        _render_detail(target_meta, args)
        return 0

    _render_list(metadata, filter_text, args)
    return 0


CMDLET = Cmdlet(
    name=".help",
    alias=["help",
           "?"],
    summary="List commands, or details for one command",
    usage=".help [cmd] [-filter text]",
    arg=[
        CmdletArg(
            name="cmd",
            type="string",
            description="Cmdlet name to show detailed help",
            required=False,
            choices=[],
        ),
        CmdletArg(
            name="-filter",
            type="string",
            description="Filter cmdlet by substring",
            required=False,
        ),
    ],
)

CMDLET.exec = _run
