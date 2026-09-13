from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from SYS.cmdlet_catalog import (
    get_cmdlet_arg_choices,
    get_cmdlet_arg_flags,
    get_cmdlet_arg_flags_from_object,
    get_cmdlet_metadata,
    list_cmdlet_metadata,
    list_cmdlet_names,
)
from PluginCore.registry import plugin_inline_query_choices, plugin_query_field_map

_ACTIVE_COMPLETER: Optional["CmdletCompleter"] = None


def clear_completer_plugin_caches() -> None:
    completer = _ACTIVE_COMPLETER
    if completer is not None:
        completer.clear_plugin_caches()


class CmdletIntrospection:

    @staticmethod
    def cmdlet_names(force: bool = False) -> List[str]:
        try:
            return list_cmdlet_names(force=force) or []
        except Exception:
            return []

    @staticmethod
    def cmdlet_args(cmd_name: str,
                    config: Optional[Dict[str,
                                          Any]] = None) -> List[str]:
        try:
            return get_cmdlet_arg_flags(cmd_name, config=config) or []
        except Exception:
            return []

    @staticmethod
    def instance_choices(config: Dict[str, Any], force: bool = False) -> List[str]:
        try:
            from SYS.cmdlet_spec import SharedArgs
            return SharedArgs.get_instance_choices(config, force=force)
        except Exception:
            return []

    @classmethod
    def arg_choices(cls,
                    *,
                    cmd_name: str,
                    arg_name: str,
                    config: Dict[str,
                                 Any],
                    force: bool = False) -> List[str]:
        try:
            normalized_arg = (arg_name or "").lstrip("-").strip().lower()

            if normalized_arg in ("storage", "store", "instance"):
                # Use cached/lightweight names for completions to avoid instantiating backends
                # (instantiating backends may perform heavy initialization).
                backends = cls.instance_choices(config, force=False)
                if backends:
                    return backends

            if normalized_arg == "plugin":
                canonical_cmd = (cmd_name or "").replace("_", "-").lower()
                try:
                    from PluginCore.registry import list_plugin_names_for_cmdlet
                except Exception:
                    list_plugin_names_for_cmdlet = None  # type: ignore

                plugin_choices: List[str] = []

                def _merge_choice_groups(*groups: Sequence[str]) -> List[str]:
                    seen: Set[str] = set()
                    merged: List[str] = []
                    for group in groups:
                        for entry in group or []:
                            key = str(entry or "").strip().lower()
                            if not key or key in seen:
                                continue
                            seen.add(key)
                            merged.append(str(entry))
                    return merged

                if canonical_cmd == "file" and list_plugin_names_for_cmdlet is not None:
                    configured_add = list_plugin_names_for_cmdlet(
                        "add-file",
                        config,
                        configured_only=True,
                    ) or []
                    available_add = list_plugin_names_for_cmdlet(
                        "add-file",
                        config,
                        configured_only=False,
                    ) or []
                    configured_search = list_plugin_names_for_cmdlet(
                        "search-file",
                        config,
                        configured_only=True,
                    ) or []
                    available_search = list_plugin_names_for_cmdlet(
                        "search-file",
                        config,
                        configured_only=False,
                    ) or []
                    plugin_choices = _merge_choice_groups(
                        configured_add,
                        available_add,
                        configured_search,
                        available_search,
                    )
                elif list_plugin_names_for_cmdlet is not None:
                    configured = list_plugin_names_for_cmdlet(
                        canonical_cmd,
                        config,
                        configured_only=True,
                    ) or []
                    available = list_plugin_names_for_cmdlet(
                        canonical_cmd,
                        config,
                        configured_only=False,
                    ) or []
                    # Prefer configured plugins first, but still show valid plugin options.
                    plugin_choices = _merge_choice_groups(configured, available)

                if plugin_choices:
                    return plugin_choices

            if normalized_arg == "scrape":
                try:
                    from PluginCore.registry import plugin_attr

                    list_metadata_plugins = plugin_attr("metadata_plus", "list_metadata_plugins") or plugin_attr(
                        "metadata_plugin", "list_metadata_plugins"
                    )
                    metadata_plugins = (list_metadata_plugins(config) or {}) if callable(list_metadata_plugins) else {}
                    if metadata_plugins:
                        return sorted(metadata_plugins.keys())
                except Exception:
                    pass

            return get_cmdlet_arg_choices(cmd_name, arg_name) or []
        except Exception:
            return []

    @staticmethod
    def query_args(cmd_name: str,
                   config: Optional[Dict[str,
                                         Any]] = None) -> List[Dict[str,
                                                                    Any]]:
        try:
            meta = get_cmdlet_metadata(cmd_name, config=config) or {}
        except Exception:
            return []

        args = meta.get("args", []) if isinstance(meta, dict) else []
        if not isinstance(args, list):
            return []

        query_args: List[Dict[str, Any]] = []
        for arg in args:
            if not isinstance(arg, dict):
                continue
            key = str(arg.get("query_key") or "").strip().lower()
            aliases = [
                str(value).strip().lower()
                for value in (arg.get("query_aliases") or [])
                if str(value).strip()
            ]
            if not key and not aliases:
                continue
            query_args.append(arg)
        return query_args

    @staticmethod
    def plugin_names_for_cmdlet(
        cmd_name: str,
        config: Optional[Dict[str, Any]] = None,
        *,
        configured_only: bool = False,
    ) -> List[str]:
        try:
            from PluginCore.registry import list_plugin_names_for_cmdlet

            return list_plugin_names_for_cmdlet(
                cmd_name,
                config,
                configured_only=configured_only,
            ) or []
        except Exception:
            return []


class CmdletCompleter(Completer):
    """Prompt-toolkit completer for the Medeia cmdlet REPL."""

    _CMDLET_NAME_REFRESH_SECONDS = 2.0
    _FILE_STAGE_ACTION_CMDLETS: Tuple[Tuple[str, str], ...] = (
        ("search", "search-file"),
        ("add", "add-file"),
        ("delete", "delete-file"),
        ("merge", "merge-file"),
        ("download", "download-file"),
        ("convert", "convert-file"),
        ("trim", "trim-file"),
        ("archive", "archive-file"),
    )
    _METADATA_COMMANDS = frozenset({"metadata", "meta"})

    def __init__(self, *, config_loader: Any) -> None:
        self._config_loader = config_loader
        self.cmdlet_names = CmdletIntrospection.cmdlet_names()
        self._cmdlet_names_refreshed_at = time.monotonic()
        self._cmdlet_args_cache: Dict[Tuple[str, int], List[str]] = {}
        self._query_args_cache: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
        self._arg_choices_cache: Dict[Tuple[str, str, int], List[str]] = {}
        self._inline_query_choices_cache: Dict[Tuple[str, str, int], List[str]] = {}
        self._plugins_for_cmdlet_cache: Dict[Tuple[str, int, bool], List[str]] = {}
        global _ACTIVE_COMPLETER
        _ACTIVE_COMPLETER = self

    def clear_plugin_caches(self) -> None:
        self._arg_choices_cache.clear()
        self._inline_query_choices_cache.clear()
        self._plugins_for_cmdlet_cache.clear()

    def _refresh_cmdlet_names(self) -> None:
        now = time.monotonic()
        if self.cmdlet_names and (now - self._cmdlet_names_refreshed_at) < self._CMDLET_NAME_REFRESH_SECONDS:
            return
        self.cmdlet_names = CmdletIntrospection.cmdlet_names(force=False)
        self._cmdlet_names_refreshed_at = now

    @staticmethod
    def _config_cache_key(config: Dict[str, Any]) -> int:
        return id(config) if isinstance(config, dict) else 0

    def _cmdlet_args(self, cmd_name: str, config: Dict[str, Any]) -> List[str]:
        key = (str(cmd_name or "").lower(), self._config_cache_key(config))
        cached = self._cmdlet_args_cache.get(key)
        if cached is not None:
            return cached
        value = CmdletIntrospection.cmdlet_args(cmd_name, config)
        self._cmdlet_args_cache[key] = value
        return value

    def _query_args(self, cmd_name: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
        key = (str(cmd_name or "").lower(), self._config_cache_key(config))
        cached = self._query_args_cache.get(key)
        if cached is not None:
            return cached
        value = CmdletIntrospection.query_args(cmd_name, config)
        self._query_args_cache[key] = value
        return value

    def _arg_choices(
        self,
        *,
        cmd_name: str,
        arg_name: str,
        config: Dict[str, Any],
        force: bool = False,
    ) -> List[str]:
        key = (
            str(cmd_name or "").lower(),
            str(arg_name or "").lower(),
            self._config_cache_key(config),
        )
        if not force:
            cached = self._arg_choices_cache.get(key)
            if cached is not None:
                return cached
        value = CmdletIntrospection.arg_choices(
            cmd_name=cmd_name,
            arg_name=arg_name,
            config=config,
            force=force,
        )
        self._arg_choices_cache[key] = value
        return value

    def _inline_query_choices(
        self,
        provider_name: str,
        field_name: str,
        config: Dict[str, Any],
    ) -> List[str]:
        key = (
            str(provider_name or "").lower(),
            str(field_name or "").lower(),
            self._config_cache_key(config),
        )
        cached = self._inline_query_choices_cache.get(key)
        if cached is not None:
            return cached
        value = plugin_inline_query_choices(provider_name, field_name, config)
        self._inline_query_choices_cache[key] = value
        return value

    def _plugins_for_cmdlet(
        self,
        cmd_name: str,
        config: Dict[str, Any],
        *,
        configured_only: bool = False,
    ) -> List[str]:
        key = (
            str(cmd_name or "").lower(),
            self._config_cache_key(config),
            bool(configured_only),
        )
        cached = self._plugins_for_cmdlet_cache.get(key)
        if cached is not None:
            return cached
        value = CmdletIntrospection.plugin_names_for_cmdlet(
            cmd_name,
            config,
            configured_only=configured_only,
        )
        self._plugins_for_cmdlet_cache[key] = value
        return value

    def _used_arg_logicals(
        self,
        cmd_name: str,
        stage_tokens: List[str],
        config: Dict[str,
                     Any],
        arg_flags: Optional[List[str]] = None,
    ) -> Set[str]:
        """Return logical argument names already used in this cmdlet stage.

        Example: if the user has typed `download-file -url ...`, then `url`
        is considered used and should not be suggested again (even as `--url`).
        """
        flags = arg_flags if arg_flags is not None else self._cmdlet_args(cmd_name, config)
        allowed = {a.lstrip("-").strip().lower()
                   for a in flags if a}
        if not allowed:
            return set()

        used: Set[str] = set()
        for tok in stage_tokens[1:]:
            if not tok or not tok.startswith("-"):
                continue
            if tok in {"-",
                       "--"}:
                continue
            # Handle common `-arg=value` form.
            raw = tok.split("=", 1)[0]
            logical = raw.lstrip("-").strip().lower()
            if logical and logical in allowed:
                used.add(logical)

        return used

    @staticmethod
    def _flag_value(tokens: Sequence[str], *flags: str) -> Optional[str]:
        want = {str(f).strip().lower() for f in flags if str(f).strip()}
        if not want:
            return None
        for idx, tok in enumerate(tokens):
            low = str(tok or "").strip().lower()
            if "=" in low:
                head, _ = low.split("=", 1)
                if head in want:
                    return tok.split("=", 1)[1]
            if low in want and idx + 1 < len(tokens):
                try:
                    from SYS.utils import consume_bracket_list

                    joined, _end = consume_bracket_list(tokens, idx)
                    return joined or tokens[idx + 1]
                except Exception:
                    return tokens[idx + 1]
        return None

    @staticmethod
    def _effective_cmd_name(cmd_name: str, stage_tokens: Sequence[str]) -> str:
        canonical_cmd = str(cmd_name or "").replace("_", "-").strip().lower()
        if canonical_cmd != "file":
            return canonical_cmd
        try:
            from cmdlet.file_cmdlet import File

            return File.resolved_cmdlet_name(list(stage_tokens or []))
        except Exception:
            return canonical_cmd

    @staticmethod
    def _selected_plugin_name(cmd_name: str, stage_tokens: Sequence[str]) -> Optional[str]:
        canonical_cmd = CmdletCompleter._effective_cmd_name(cmd_name, stage_tokens)
        if canonical_cmd in {".matrix", "matrix", "rooms"}:
            return "matrix"
        if canonical_cmd not in {"file", "search-file", "add-file", "download-file"}:
            return None
        raw_plugin = CmdletCompleter._flag_value(stage_tokens, "-plugin", "--plugin")
        if raw_plugin:
            stripped = CmdletCompleter._strip_quotes(str(raw_plugin or ""))
            if not stripped:
                return None
            try:
                from SYS.utils import split_instance_names

                names = split_instance_names(stripped)
            except Exception:
                names = []
            if names:
                return str(names[0]).strip().lower()
            plugin_name = stripped.strip().lower().strip("[]")
            if plugin_name.startswith("[") or not plugin_name:
                return None
            return plugin_name
        return None

    @staticmethod
    def _plugin_instance_choices(plugin_name: Optional[str], config: Dict[str, Any]) -> List[str]:
        plugin_key = str(plugin_name or "").strip().lower()
        if not plugin_key:
            return []

        try:
            from PluginCore.registry import get_plugin_class
        except Exception:
            return []

        plugin_class = get_plugin_class(plugin_key)
        if plugin_class is None:
            return []

        try:
            plugin = plugin_class(config)
        except Exception:
            return []

        try:
            instances = plugin.configured_instances()
        except Exception:
            return []

        out: List[str] = []
        seen: Set[str] = set()
        for value in instances or []:
            text = str(value or "").strip()
            lowered = text.lower()
            if not text or lowered in seen:
                continue
            seen.add(lowered)
            out.append(text)
        return out

    @staticmethod
    def _plugin_instance_accepts_direct_path(plugin_name: Optional[str]) -> bool:
        return str(plugin_name or "").strip().lower() == "local"

    @staticmethod
    def _looks_like_path_fragment(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        if text[:1] in {"'", '"'}:
            text = text[1:]
        if not text:
            return False
        if text.startswith((".", "~", "\\", "/")):
            return True
        if "\\" in text or "/" in text:
            return True
        if len(text) >= 2 and text[1] == ":":
            return True
        return False

    @staticmethod
    def _path_instance_choices(current_token: str) -> List[str]:
        raw = str(current_token or "")
        if not CmdletCompleter._looks_like_path_fragment(raw):
            return []

        quote_prefix = raw[:1] if raw[:1] in {"'", '"'} else ""
        fragment = raw[1:] if quote_prefix else raw
        if not fragment:
            return []

        expanded = os.path.expanduser(fragment)
        candidate = Path(expanded)

        if fragment.endswith(("\\", "/")):
            parent = candidate
            prefix = ""
        else:
            parent = candidate.parent if str(candidate.parent) not in {"", "."} else Path.cwd()
            prefix = candidate.name

        try:
            if not parent.exists() or not parent.is_dir():
                return []
        except Exception:
            return []

        out: List[str] = []
        seen: Set[str] = set()
        prefix_lower = prefix.lower()
        try:
            entries = sorted(parent.iterdir(), key=lambda item: item.name.lower())
        except Exception:
            return []

        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
                if prefix_lower and not entry.name.lower().startswith(prefix_lower):
                    continue
                suggestion = str(entry)
                if quote_prefix:
                    suggestion = quote_prefix + suggestion
                elif " " in suggestion:
                    suggestion = f'"{suggestion}"'
            except Exception:
                continue

            lowered = suggestion.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            out.append(suggestion)

        return out

    def _file_stage_order(
        self,
        *,
        stage_tokens: Sequence[str],
        config: Dict[str, Any],
    ) -> Optional[List[str]]:
        if self._effective_cmd_name("file", stage_tokens) != "file":
            return None

        try:
            from cmdlet.file_cmdlet import File

            action_names = list(File._ACTION_CMDLET.keys())
            plugin_scoped = set(File._PLUGIN_SCOPED_ACTIONS)
        except Exception:
            action_names = [logical for logical, _ in self._FILE_STAGE_ACTION_CMDLETS]
            plugin_scoped = {"search", "add", "download"}

        plugin_name = self._selected_plugin_name("file", stage_tokens)
        if not plugin_name:
            return list(action_names)

        plugin_key = str(plugin_name or "").strip().lower()
        ordered: List[str] = []

        action_to_cmd = dict(self._FILE_STAGE_ACTION_CMDLETS)
        try:
            from cmdlet.file_cmdlet import File

            action_to_cmd = dict(File._ACTION_CMDLET)
        except Exception:
            pass

        for logical in action_names:
            if logical in plugin_scoped:
                target_cmd = action_to_cmd.get(logical, f"{logical}-file")
                supported = plugin_key in {
                    str(name or "").strip().lower()
                    for name in self._plugins_for_cmdlet(target_cmd, config)
                }
                if not supported:
                    continue
            if logical not in ordered:
                ordered.append(logical)

        return ordered or list(action_names)

    def _file_search_stage_order(
        self,
        *,
        stage_tokens: Sequence[str],
        config: Dict[str, Any],
    ) -> List[str]:
        # Best practice: choose -plugin (and -instance when needed) before -query.
        # limit is a -query field (limit:N), not a top-level flag.
        return ["plugin", "query"]

    _METADATA_PRIMARY_ACTIONS = ("add", "delete")
    _METADATA_PRIMARY_DOMAINS = ("tag", "url", "relationship", "note")
    _METADATA_HIDDEN_FLAGS = frozenset({"get", "inspect", "info"})
    _METADATA_COMPLETER_ALIASES = frozenset({
        "rel",
        "del",
        "info",
        "file",
        "autotag",
        "tags",
        "urls",
        "notes",
        "relationships",
    })

    @classmethod
    def _metadata_primary_action(cls, logicals: Set[str]) -> Optional[str]:
        lowered = {str(item or "").lstrip("-").strip().lower() for item in logicals}
        try:
            from cmdlet.metadata_cmdlet import Metadata

            mapping = Metadata._all_action_flags()
        except Exception:
            mapping = {
                "add": {"-add", "--add"},
                "delete": {"-delete", "--delete", "-del", "--del"},
                "get": {"-get", "--get"},
                "inspect": {"-inspect", "--inspect", "-info", "--info", "-file", "--file"},
            }
        for name, variants in mapping.items():
            keys = {str(name)} | {str(item).lstrip("-").strip().lower() for item in variants}
            if lowered & keys:
                return str(name)
        return None

    @staticmethod
    def _metadata_action_logicals() -> Set[str]:
        try:
            from cmdlet.metadata_cmdlet import Metadata

            return set(Metadata.action_logicals())
        except Exception:
            return {"add", "delete", "del", "get", "inspect", "info", "file"}

    @staticmethod
    def _metadata_domain_logicals() -> Set[str]:
        try:
            from cmdlet.metadata_cmdlet import Metadata

            return set(Metadata.domain_logicals())
        except Exception:
            return {
                "tag",
                "tags",
                "url",
                "urls",
                "relationship",
                "relationships",
                "rel",
                "note",
                "notes",
                "file",
            }

    @staticmethod
    def _dispatched_metadata_cmdlet(stage_tokens: Sequence[str]) -> Any:
        try:
            from cmdlet.metadata_cmdlet import Metadata
        except Exception:
            return None
        args = list(stage_tokens[1:] if stage_tokens else [])
        try:
            return Metadata.dispatched_cmdlet(args)
        except Exception:
            return None

    @staticmethod
    def _arg_choices_from_object(cmdlet_obj: Any, arg_name: str) -> List[str]:
        target = str(arg_name or "").lstrip("-").strip().lower()
        if not target or cmdlet_obj is None:
            return []
        out: List[str] = []
        for arg in getattr(cmdlet_obj, "arg", None) or []:
            name = str(getattr(arg, "name", "") or "").lstrip("-").strip().lower()
            alias = str(getattr(arg, "alias", "") or "").lstrip("-").strip().lower()
            if target not in {name, alias}:
                continue
            for choice in getattr(arg, "choices", None) or []:
                text = str(choice or "").strip()
                if text:
                    out.append(text)
            break
        return out

    def _metadata_leaf_committed(self, stage_tokens: Sequence[str]) -> bool:
        switch = self._metadata_action_logicals() | self._metadata_domain_logicals()
        for tok in list(stage_tokens or [])[1:]:
            raw = str(tok or "").strip()
            if not raw:
                continue
            logical = raw.split("=", 1)[0].lstrip("-").strip().lower()
            if raw in {"-", "--"} or not logical:
                continue
            if raw.startswith("-") and logical in switch:
                continue
            return True
        return False

    def _flag_expects_value(
        self,
        cmd_name: str,
        flag: str,
        config: Dict[str, Any],
        stage_tokens: Sequence[str],
    ) -> bool:
        logical = str(flag or "").split("=", 1)[0].lstrip("-").strip().lower()
        if not logical:
            return False

        specs: List[Any] = []
        if str(cmd_name or "").replace("_", "-").strip().lower() in self._METADATA_COMMANDS:
            target = self._dispatched_metadata_cmdlet(stage_tokens)
            if target is not None:
                specs = list(getattr(target, "arg", None) or [])
        if not specs:
            try:
                meta = get_cmdlet_metadata(cmd_name, config=config) or {}
            except Exception:
                meta = {}
            specs = list(meta.get("args") or [])

        for arg in specs:
            if isinstance(arg, dict):
                name = str(arg.get("name") or "").lstrip("-").strip().lower()
                alias = str(arg.get("alias") or "").lstrip("-").strip().lower()
                arg_type = str(arg.get("type") or "string").strip().lower()
            else:
                name = str(getattr(arg, "name", "") or "").lstrip("-").strip().lower()
                alias = str(getattr(arg, "alias", "") or "").lstrip("-").strip().lower()
                arg_type = str(getattr(arg, "type", "") or "string").strip().lower()
            if logical not in {name, alias}:
                continue
            return arg_type not in {"flag", "bool", "boolean"}
        return False

    @staticmethod
    def _selection_indices_from_line(text: str) -> List[int]:
        first = str(text or "").split("|", 1)[0].strip()
        if not first.startswith("@"):
            return []
        token = first.split(None, 1)[0]
        try:
            from SYS.cli_parsing import SelectionSyntax

            parsed = SelectionSyntax.parse(token)
        except Exception:
            parsed = None
        if not parsed:
            return []
        return [int(i) - 1 for i in parsed if int(i) > 0]

    @staticmethod
    def _namespaces_from_tag_blob(blob: str) -> List[str]:
        names: List[str] = []
        seen: Set[str] = set()
        for match in re.finditer(r"(?:^|,\s*)([A-Za-z][A-Za-z0-9_]*):", str(blob or "")):
            ns = match.group(1).strip().lower()
            if not ns or ns in seen:
                continue
            seen.add(ns)
            names.append(ns)
        return names

    def _tag_namespaces_from_context(self, line: str = "") -> List[str]:
        names: List[str] = []
        seen: Set[str] = set()

        def _add(ns: str) -> None:
            key = str(ns or "").strip().lower()
            if not key or key in seen:
                return
            seen.add(key)
            names.append(key)

        def _consume(raw: Any) -> None:
            if isinstance(raw, str):
                if ":" in raw and "," in raw:
                    for ns in self._namespaces_from_tag_blob(raw):
                        _add(ns)
                    return
                if ":" in raw:
                    _add(raw.split(":", 1)[0])
                    return
                return
            if isinstance(raw, (list, tuple, set)):
                for part in raw:
                    _consume(part)

        try:
            from SYS import pipeline as ctx
            from SYS.item_accessors import get_field

            items = list(ctx.get_last_result_items() or [])
            table = ctx.get_last_result_table()
        except Exception:
            items = []
            table = None

        indices = self._selection_indices_from_line(line)
        chosen: List[Any] = []
        if indices:
            for idx in indices:
                if 0 <= idx < len(items):
                    chosen.append(items[idx])
                elif table is not None:
                    rows = getattr(table, "rows", None) or []
                    if 0 <= idx < len(rows):
                        chosen.append(rows[idx])
        else:
            chosen = items[:80]

        for item in chosen:
            tags = None
            try:
                tags = get_field(item, "tag")
            except Exception:
                tags = None
            if tags is None:
                try:
                    tags = get_field(item, "tags")
                except Exception:
                    tags = None
            if tags is None and isinstance(item, dict):
                tags = item.get("tag") or item.get("tags") or item.get("tag_summary")
            if tags is None:
                try:
                    tags = getattr(item, "tag_summary", None) or getattr(item, "tag", None)
                except Exception:
                    tags = None
            _consume(tags)
            cols = getattr(item, "columns", None)
            if cols is None and isinstance(item, dict):
                cols = item.get("columns")
            if isinstance(cols, list):
                for col in cols:
                    label = ""
                    value = ""
                    if isinstance(col, (tuple, list)) and len(col) >= 2:
                        label, value = str(col[0]), col[1]
                    else:
                        label = str(getattr(col, "name", "") or "")
                        value = getattr(col, "value", None)
                    if label.lower() in {"tag", "tags"}:
                        _consume(value)
            get_col = getattr(item, "get_column", None)
            if callable(get_col):
                try:
                    _consume(get_col("Tag") or get_col("tag"))
                except Exception:
                    pass
        return names

    @staticmethod
    def _table_column_completions(token: str):
        partial = str(token or "").lstrip("@").strip()
        headers: List[str] = []
        try:
            from SYS import pipeline as ctx

            table = ctx.get_display_table() or ctx.get_current_stage_table() or ctx.get_last_result_table()
            rows = getattr(table, "rows", None) or []
            seen: Set[str] = set()
            for row in rows[:5]:
                for col in getattr(row, "columns", []) or []:
                    name = str(getattr(col, "name", "") or "").strip()
                    key = name.lower()
                    if not name or key in seen:
                        continue
                    seen.add(key)
                    headers.append(name)
                if headers:
                    break
        except Exception:
            headers = []
        for name in headers:
            if partial and not name.lower().startswith(partial.lower()):
                continue
            yield Completion(
                f"@{name}",
                start_position=-len(str(token or "")),
                display_meta="column",
            )

    def _table_sort_completions(self, token: str, line: str = ""):
        hay = str(token or "")
        fragment = hay
        if "," in hay:
            depth = 0
            last = 0
            for index, ch in enumerate(hay):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth = max(0, depth - 1)
                elif ch == "," and depth == 0:
                    last = index + 1
            fragment = hay[last:]
        open_paren = fragment.find("(")
        if fragment.startswith("@") and open_paren > 0:
            inner = fragment[open_paren + 1:]
            if inner.endswith(")"):
                inner = inner[:-1]
            if "," in inner:
                _ns_raw, order_raw = inner.split(",", 1)
                stripped = order_raw.lstrip()
                start_pos = -len(stripped) if stripped else 0
                partial = stripped.lower()
                for order in ("asc", "desc"):
                    if partial and not order.startswith(partial):
                        continue
                    yield Completion(order, start_position=start_pos, display_meta="order")
                return
            try:
                from SYS.config import get_tag_placeholder_prefix

                tag_prefix = get_tag_placeholder_prefix()
            except Exception:
                tag_prefix = "$"
            stripped = inner.lstrip()
            start_pos = -len(stripped) if stripped else 0
            used_prefix = tag_prefix
            ns_partial = stripped
            if stripped.startswith("$") or stripped.startswith("#"):
                used_prefix = stripped[0]
                ns_partial = stripped[1:]
            namespaces = self._tag_namespaces_from_context(line)
            if not namespaces:
                namespaces = ["part", "track", "series", "title", "artist", "album", "episode"]
            for ns in namespaces:
                if ns_partial and not ns.lower().startswith(ns_partial.lower()):
                    continue
                yield Completion(
                    f"{used_prefix}{ns}",
                    start_position=start_pos,
                    display_meta="tag",
                )
            return

        for completion in self._table_column_completions(fragment):
            yield completion

    def _tag_placeholder_completions(self, token: str, line: str = ""):
        hay = str(token or "")
        if not hay:
            return
        try:
            from SYS.config import (
                DATE_FORMAT_CHOICES,
                get_tag_placeholder_prefix,
                get_tag_placeholder_prefixes,
            )

            prefixes = list(get_tag_placeholder_prefixes())
            primary = get_tag_placeholder_prefix()
        except Exception:
            DATE_FORMAT_CHOICES = ["YYYY-MM-DD", "MM/DD/YYYY", "DD/MM/YYYY", "MM/DD/YY"]
            prefixes = ["$", "#"]
            primary = "$"
        namespaces = self._tag_namespaces_from_context(line)
        if hay.endswith(primary) and not hay.endswith(f"{primary}("):
            start_pos = -len(primary)
            if namespaces:
                for ns in namespaces:
                    yield Completion(
                        f"{primary}({ns})",
                        start_position=start_pos,
                        display_meta="tag",
                    )
                return
            yield Completion(f"{primary}(", start_position=start_pos, display_meta="tag placeholder")
            return
        pos = -1
        used_prefix = ""
        for prefix in prefixes:
            marker = f"{prefix}("
            found = hay.rfind(marker)
            if found > pos:
                pos = found
                used_prefix = prefix
        if pos < 0:
            lt = hay.lower().rfind("<date(")
            if lt >= 0:
                pos = lt
                used_prefix = "<date"
        if pos < 0:
            return
        fragment = hay[pos:]
        inner = fragment.split("(", 1)[-1]
        start_pos = -(len(hay) - pos)
        if "," in inner:
            ns = inner.split(",", 1)[0].strip().lower()
            partial = inner.split(",", 1)[1].strip().strip("'\"")
            if ns in {"date", "airdate", "pubdate"}:
                for fmt in DATE_FORMAT_CHOICES:
                    if partial and not (
                        fmt.lower().startswith(partial.lower()) or partial.lower() in fmt.lower()
                    ):
                        continue
                    if used_prefix == "<date":
                        suggestion = f"<date({primary}({ns}), {fmt})"
                    else:
                        suggestion = f"{used_prefix}({ns}, {fmt})"
                    yield Completion(suggestion, start_position=start_pos, display_meta="date format")
            return
        partial = inner.strip().lower()
        if not namespaces:
            namespaces = self._tag_namespaces_from_context(line)
        if not namespaces:
            namespaces = ["date", "title", "series", "artist", "album", "creator"]
        for ns in namespaces:
            if partial and not ns.startswith(partial):
                continue
            yield Completion(
                f"{primary}({ns})",
                start_position=start_pos,
                display_meta="tag",
            )

    def _tag_function_completions(self, token: str, line: str = ""):
        try:
            from cmdlet._tag_utils import (
                find_open_tag_function,
                tag_function_arg_kinds,
                tag_function_completions,
                tag_function_signature,
                tag_function_variadic,
            )
        except Exception:
            return
        hay = str(token or "")
        has_quote = hay[:1] in {"'", '"'}
        if has_quote:
            hay = hay[1:]

        open_call = find_open_tag_function(hay)
        if open_call is not None:
            canonical, arg_index, partial = open_call
            try:
                from SYS.config import get_tag_placeholder_prefix

                primary = get_tag_placeholder_prefix()
            except Exception:
                primary = "$"
            signature = tag_function_signature(canonical) or canonical
            kinds = tag_function_arg_kinds(canonical)
            variadic = tag_function_variadic(canonical)
            if not kinds:
                return
            if arg_index >= len(kinds) and not variadic:
                return
            kind = kinds[arg_index] if arg_index < len(kinds) else kinds[-1]
            if kind == "number":
                start_pos = -len(partial) if partial else 0
                if partial and not all(ch.isdigit() or ch in "+-" for ch in partial):
                    return
                for n in ("1", "2", "3", "8", "10", "27"):
                    if partial and not n.startswith(partial):
                        continue
                    yield Completion(
                        n,
                        start_position=start_pos,
                        display_meta=signature,
                        style="fg:#d19a66",
                    )
                return
            if kind == "format":
                try:
                    from SYS.config import DATE_FORMAT_CHOICES
                except Exception:
                    DATE_FORMAT_CHOICES = ["YYYY-MM-DD", "MM/DD/YYYY", "DD/MM/YYYY", "MM/DD/YY"]
                start_pos = -len(partial) if partial else 0
                yielded = False
                for fmt in DATE_FORMAT_CHOICES:
                    if partial and not str(fmt).lower().startswith(partial.lower()):
                        continue
                    yield Completion(str(fmt), start_position=start_pos, display_meta=signature)
                    yielded = True
                if yielded:
                    return
                return
            namespaces = self._tag_namespaces_from_context(line)
            if not namespaces:
                namespaces = ["date", "title", "series", "artist", "album", "creator"]
            start_pos = -len(partial) if partial else 0
            yielded = False
            for ns in namespaces:
                if partial and not ns.lower().startswith(partial.lower()):
                    continue
                yield Completion(
                    f"{primary}({ns})",
                    start_position=start_pos,
                    display_meta=signature,
                    style="fg:#98c379",
                )
                yielded = True
            if yielded:
                return
            return

        if "$(" in hay or "#(" in hay or "<" in hay:
            return
        if ":" in hay:
            namespace, value = hay.rsplit(":", 1)
            if not namespace.strip():
                return
        else:
            value = hay
        value = value.lstrip()
        if value.startswith("-"):
            return
        partial = value
        matches = tag_function_completions()
        filtered = [
            match for match in matches
            if not partial or match[0].startswith(partial.lower())
        ]
        if not filtered:
            return
        if not partial and not has_quote and ":" not in hay:
            return
        start_pos = -len(partial) if partial else 0
        for name, signature, help_text in filtered:
            meta = f"{signature} — {help_text}" if help_text else signature
            yield Completion(
                f"{name}(",
                start_position=start_pos,
                display_meta=meta,
                style="fg:#56b6c2",
            )

    def _extract_syntax_completions(self, token: str, stage_tokens: Sequence[str]):
        hay = str(token or "")
        has_quote = hay[:1] in {"'", '"'}
        if has_quote:
            hay = hay[1:]
        extract_ctx = any(
            str(tok).lstrip("-").strip().lower() == "extract"
            for tok in stage_tokens or []
        ) or ("(" in hay and "[" in hay) or hay.endswith("[") or "[" in hay
        if not extract_ctx:
            return
        last_open = hay.rfind("[")
        last_close = hay.rfind("]")
        if last_open > last_close:
            inner = hay[last_open + 1:]
            partial = inner.strip()
            start_pos = -len(inner)
            examples = (
                ("-|:", "dash or colon"),
                (" - | : ", "spaced dash or colon"),
                (":|-", "colon or dash"),
            )
            for text, meta in examples:
                if partial and not text.lstrip().startswith(partial) and partial not in text:
                    continue
                yield Completion(
                    text,
                    start_position=start_pos,
                    display_meta=meta,
                    style="fg:#e5c07b",
                )
            return
        if hay.rstrip().endswith("]"):
            for field in ("episode", "name", "title", "track", "series", "part"):
                yield Completion(
                    f"({field})",
                    start_position=0,
                    display_meta="extract field",
                    style="fg:#c678dd",
                )

    def _metadata_arg_names(
        self,
        stage_tokens: Sequence[str],
        config: Dict[str, Any],
    ) -> List[str]:
        umbrella = list(self._cmdlet_args("metadata", config))
        target = self._dispatched_metadata_cmdlet(stage_tokens)
        if target is None:
            return umbrella

        leaf_flags = get_cmdlet_arg_flags_from_object(target)
        if self._metadata_leaf_committed(stage_tokens):
            return leaf_flags

        flags = list(umbrella)
        seen = {str(flag or "").lower() for flag in flags}
        for flag in leaf_flags:
            key = str(flag or "").lower()
            if key in seen:
                continue
            flags.append(flag)
            seen.add(key)
        return flags

    def _stage_completion_args(
        self,
        *,
        cmd_name: str,
        stage_tokens: Sequence[str],
        config: Dict[str, Any],
    ) -> List[str]:
        source_cmd = str(cmd_name or "").replace("_", "-").strip().lower()
        if source_cmd in self._METADATA_COMMANDS:
            arg_names = self._metadata_arg_names(stage_tokens, config)
        else:
            effective_cmd = self._effective_cmd_name(source_cmd, stage_tokens)
            arg_names = self._cmdlet_args(effective_cmd, config)
            if (
                source_cmd == "file"
                and effective_cmd != "file"
                and not arg_names
            ):
                plugin_cmds = {"search-file", "add-file", "download-file"}
                try:
                    from cmdlet.file_cmdlet import File

                    plugin_cmds = {
                        str(File._ACTION_CMDLET[name])
                        for name in File._PLUGIN_SCOPED_ACTIONS
                    }
                except Exception:
                    pass
                if effective_cmd in plugin_cmds:
                    arg_names = ["-plugin", "-query"]
        return self._filter_stage_arg_names(
            cmd_name=source_cmd,
            stage_tokens=stage_tokens,
            config=config,
            arg_names=arg_names,
        )

    def _filter_stage_arg_names(
        self,
        *,
        cmd_name: str,
        stage_tokens: Sequence[str],
        config: Dict[str, Any],
        arg_names: List[str],
    ) -> List[str]:
        if not arg_names:
            return []

        source_cmd = (
            str(stage_tokens[0] or "").replace("_", "-").strip().lower()
            if stage_tokens else str(cmd_name or "").replace("_", "-").strip().lower()
        )
        canonical_cmd = self._effective_cmd_name(source_cmd, stage_tokens)
        plugin_name = self._selected_plugin_name(canonical_cmd, stage_tokens)
        instance_choices = self._plugin_instance_choices(plugin_name, config)
        has_named_instances = bool(instance_choices)
        accepts_direct_path = self._plugin_instance_accepts_direct_path(plugin_name)
        allow_instance_without_plugin = (
            (canonical_cmd == "search-file" and source_cmd != "file")
            or source_cmd in self._METADATA_COMMANDS
        )

        file_stage_order = None
        file_stage_rank: Dict[str, int] = {}
        if source_cmd == "file" and canonical_cmd == "file":
            file_stage_order = self._file_stage_order(
                stage_tokens=stage_tokens,
                config=config,
            )
            if file_stage_order is not None:
                file_stage_rank = {
                    logical: idx for idx, logical in enumerate(file_stage_order)
                }
        elif source_cmd == "file" and canonical_cmd == "search-file":
            file_stage_order = self._file_search_stage_order(
                stage_tokens=stage_tokens,
                config=config,
            )
            file_stage_rank = {
                logical: idx for idx, logical in enumerate(file_stage_order)
            }

        file_action_logicals = {logical for logical, _ in self._FILE_STAGE_ACTION_CMDLETS}
        chosen_file_actions: Set[str] = set()
        chosen_meta_actions: Set[str] = set()
        chosen_meta_domains: Set[str] = set()
        is_metadata = source_cmd in self._METADATA_COMMANDS
        metadata_action_logicals = self._metadata_action_logicals() if is_metadata else set()
        metadata_domain_logicals = self._metadata_domain_logicals() if is_metadata else set()
        for token in stage_tokens:
            logical = str(token).lstrip("-").strip().lower()
            if logical in file_action_logicals:
                chosen_file_actions.add(logical)
            if is_metadata and logical in metadata_action_logicals:
                chosen_meta_actions.add(logical)
            if is_metadata and logical in metadata_domain_logicals:
                chosen_meta_domains.add(logical)

        metadata_primary_action = (
            self._metadata_primary_action(chosen_meta_actions) if is_metadata else None
        )
        metadata_stage_order: Optional[List[str]] = None
        metadata_stage_rank: Dict[str, int] = {}
        if is_metadata and metadata_primary_action is None:
            metadata_stage_order = list(self._METADATA_PRIMARY_ACTIONS)
            metadata_stage_rank = {
                name: idx for idx, name in enumerate(metadata_stage_order)
            }
        elif is_metadata:
            ranked: List[str] = []
            if metadata_primary_action not in {"inspect", "auto"}:
                ranked.extend(self._METADATA_PRIMARY_DOMAINS)
            if metadata_primary_action == "add" and "auto" in metadata_action_logicals:
                ranked.append("auto")
            ranked.extend(["extract", "duplicate"])
            ranked.append("query")
            metadata_stage_rank = {name: idx for idx, name in enumerate(ranked)}

        filtered: List[Tuple[int, int, str]] = []
        for index, arg in enumerate(arg_names):
            logical = str(arg or "").lstrip("-").strip().lower()
            if file_stage_order is not None and logical not in file_stage_rank:
                continue
            if metadata_stage_order is not None and logical not in metadata_stage_rank:
                continue
            if chosen_file_actions and logical in file_action_logicals and logical not in chosen_file_actions:
                continue
            if chosen_meta_actions and logical in metadata_action_logicals and logical not in chosen_meta_actions:
                if not (
                    logical in {"auto", "autotag"}
                    and metadata_primary_action == "add"
                    and "auto" in metadata_action_logicals
                ):
                    continue
            if chosen_meta_domains and logical in metadata_domain_logicals and logical not in chosen_meta_domains:
                continue
            if is_metadata and logical in self._METADATA_HIDDEN_FLAGS:
                continue
            if is_metadata and logical in self._METADATA_COMPLETER_ALIASES:
                continue
            if is_metadata and logical == "instance":
                continue
            if (
                is_metadata
                and metadata_primary_action in {"inspect", "auto"}
                and logical in self._METADATA_PRIMARY_DOMAINS
            ):
                continue
            if logical in {"extract", "duplicate", "extract-debug"}:
                if chosen_meta_domains and "tag" not in chosen_meta_domains and "tags" not in chosen_meta_domains:
                    continue
                if metadata_primary_action not in {None, "add"}:
                    continue
            if logical == "open":
                continue
            if logical == "instance":
                if allow_instance_without_plugin:
                    pass
                elif not plugin_name:
                    continue
                if not allow_instance_without_plugin and not has_named_instances and not accepts_direct_path:
                    continue
            rank = file_stage_rank.get(logical)
            if rank is None:
                rank = metadata_stage_rank.get(logical, len(metadata_stage_rank) or len(file_stage_rank))
            filtered.append((rank, index, arg))

        if file_stage_order is not None or metadata_stage_rank:
            filtered.sort(key=lambda item: (item[0], item[1]))

        return [arg for _, _, arg in filtered]

    @staticmethod
    def _tokenize_quoted(text: str) -> List[str]:
        """Tokenize text preserving quoted strings as single tokens.
        
        Handles pipes as pipeline separators and preserves quoted strings
        (single or double quotes) as atomic tokens.
        """
        tokens = []
        current = ""
        in_quote = None  # None, "'", or '"'
        i = 0
        
        while i < len(text):
            char = text[i]
            
            if in_quote:
                current += char
                if char == in_quote and (i == 0 or text[i - 1] != "\\"):
                    in_quote = None
            elif char in ("'", '"'):
                in_quote = char
                current += char
            elif char == "|":
                if current.strip():
                    tokens.append(current.strip())
                tokens.append("|")
                current = ""
            elif char.isspace():
                if current.strip():
                    tokens.append(current.strip())
                current = ""
            else:
                current += char
            
            i += 1
        
        if current.strip():
            tokens.append(current.strip())
        
        return tokens

    @staticmethod
    def _hidden_impl_cmdlet_names() -> Set[str]:
        try:
            from cmdnat.help import _HELP_INDEX_HIDDEN

            return set(_HELP_INDEX_HIDDEN)
        except Exception:
            try:
                from cmdlet.file_cmdlet import File

                return set(File._ACTION_CMDLET.values())
            except Exception:
                return set()

    @staticmethod
    def _cmdlet_name_matches(name: str, prefix: str) -> bool:
        text = str(name or "").replace("_", "-").strip().lower()
        needle = str(prefix or "").replace("_", "-").strip().lower()
        if not needle:
            return True
        return text.startswith(needle)

    def _complete_cmdlet_names(self, prefix: str) -> List[str]:
        needle = str(prefix or "").replace("_", "-").strip().lower()
        hidden = self._hidden_impl_cmdlet_names()
        try:
            entries = list_cmdlet_metadata()
        except Exception:
            entries = {}
        out: List[str] = []
        seen: Set[str] = set()
        for canonical, meta in (entries or {}).items():
            name = str(canonical or (meta or {}).get("name") or "").replace("_", "-").strip().lower()
            if not name or name in hidden or name in seen:
                continue
            aliases = [
                str(alias).replace("_", "-").strip().lower()
                for alias in ((meta or {}).get("aliases") or [])
                if alias
            ]
            if needle and not any(self._cmdlet_name_matches(candidate, needle) for candidate in [name, *aliases]):
                continue
            seen.add(name)
            out.append(name)
        return sorted(out)

    def _cmdlet_display_meta(self, name: str) -> str:
        key = str(name or "").replace("_", "-").strip().lower()
        try:
            meta = get_cmdlet_metadata(key) or {}
        except Exception:
            meta = {}
        summary = str((meta or {}).get("summary") or "").strip()
        if summary:
            return summary
        try:
            from cmdlet.file_cmdlet import File

            for action, cmdlet_name in File._ACTION_CMDLET.items():
                if key == cmdlet_name:
                    return f"file -{action}"
            if key in {"del-file"}:
                return "file -delete"
            plugin_actions = File._plugin_file_actions()
            if key in plugin_actions:
                return f"file -{key}"
            cmdlet_name = str((plugin_actions.get(key) or {}).get("cmdlet") or "")
            if cmdlet_name and key == cmdlet_name:
                return f"file -{key}"
        except Exception:
            pass
        return ""

    def _arg_descriptions(self, cmd_name: str, config: Dict[str, Any]) -> Dict[str, str]:
        try:
            meta = get_cmdlet_metadata(cmd_name, config=config) or {}
        except Exception:
            return {}
        out: Dict[str, str] = {}
        for arg in meta.get("args") or []:
            if not isinstance(arg, dict):
                continue
            logical = str(arg.get("name") or "").lstrip("-").strip().lower()
            desc = str(arg.get("description") or "").strip()
            if not logical or not desc:
                continue
            out[logical] = desc
            alias = str(arg.get("alias") or "").lstrip("-").strip().lower()
            if alias:
                out[alias] = desc
        return out

    @staticmethod
    def _is_closed_quoted_token(token: str) -> bool:
        text = str(token or "")
        if len(text) < 2 or text[0] not in {"'", '"'}:
            return False
        return text[-1] == text[0]

    @staticmethod
    def _strip_quotes(token: str) -> str:
        """Remove surrounding quotes from a token if present.
        
        Preserves internal content exactly. Only removes matching outer quotes.
        """
        token = str(token or "").strip()
        if len(token) >= 2:
            if (token[0] == '"' and token[-1] == '"') or (token[0] == "'" and token[-1] == "'"):
                return token[1:-1]
        return token

    def get_completions(
        self,
        document: Document,
        complete_event
    ):  # type: ignore[override]
        try:
            yield from self._iter_completions(document, complete_event)
        except Exception:
            return

    def _iter_completions(
        self,
        document: Document,
        complete_event
    ):
        self._refresh_cmdlet_names()

        text = document.text_before_cursor
        tokens = self._tokenize_quoted(text)
        ends_with_space = bool(text) and text[-1].isspace()

        last_pipe = -1
        for idx, tok in enumerate(tokens):
            if tok == "|":
                last_pipe = idx
        stage_tokens = tokens[last_pipe + 1:] if last_pipe >= 0 else tokens

        if not stage_tokens:
            for cmd in self._complete_cmdlet_names(""):
                yield Completion(cmd, start_position=0, display_meta=self._cmdlet_display_meta(cmd))
            return

        if len(stage_tokens) == 1:
            current = stage_tokens[0].lower()

            if ends_with_space:
                cmd_name = current.replace("_", "-")

                config = self._config_loader.load_shared()

                if cmd_name in {"help", ".help", "?"}:
                    for cmd in self._complete_cmdlet_names(""):
                        yield Completion(cmd, start_position=0, display_meta=self._cmdlet_display_meta(cmd))
                    return

                if cmd_name not in self.cmdlet_names:
                    return

                arg_names = self._stage_completion_args(
                    cmd_name=cmd_name,
                    stage_tokens=stage_tokens,
                    config=config,
                )
                descriptions = self._arg_descriptions(self._effective_cmd_name(cmd_name, stage_tokens), config)
                seen_logicals: Set[str] = set()
                for arg in arg_names:
                    arg_low = arg.lower()
                    if arg_low.startswith("--"):
                        continue
                    logical = arg.lstrip("-").lower()
                    if logical in seen_logicals:
                        continue
                    yield Completion(
                        arg,
                        start_position=0,
                        display_meta=descriptions.get(logical, ""),
                    )
                    seen_logicals.add(logical)
                return

            for cmd in self._complete_cmdlet_names(current):
                yield Completion(
                    cmd,
                    start_position=-len(current),
                    display_meta=self._cmdlet_display_meta(cmd),
                )
            for keyword in ("help", "exit", "quit"):
                if keyword.startswith(current):
                    yield Completion(keyword, start_position=-len(current))
            return

        cmd_name = stage_tokens[0].replace("_", "-").lower()
        effective_cmd = self._effective_cmd_name(cmd_name, stage_tokens)
        if ends_with_space:
            raw_current_token = ""
            current_token = ""
            prev_token = stage_tokens[-1].lower()
        else:
            raw_current_token = stage_tokens[-1]
            current_token = raw_current_token.lower()
            prev_token = stage_tokens[-2].lower() if len(stage_tokens) > 1 else ""

        config = self._config_loader.load_shared()

        provider_name = None
        if effective_cmd == "search-file":
            raw_provider = self._flag_value(stage_tokens, "-plugin", "--plugin")
            if raw_provider:
                stripped = self._strip_quotes(str(raw_provider or ""))
                provider_name = stripped.strip().lower() if stripped else None

        selected_plugin = self._selected_plugin_name(effective_cmd, stage_tokens)

        query_specs = self._query_args(effective_cmd, config)
        query_flag_index = -1
        for idx, tok in enumerate(stage_tokens):
            if str(tok or "").strip().lower() in {"-query", "--query"}:
                query_flag_index = idx

        if query_flag_index >= 0 and (query_specs or selected_plugin):
            query_parts = stage_tokens[query_flag_index + 1:]
            query_started_quoted = bool(query_parts and str(query_parts[0] or "")[:1] in {"'", '"'})

            query_closed = bool(
                (
                    ends_with_space
                    and query_parts
                    and self._is_closed_quoted_token(str(query_parts[-1] or ""))
                )
                or (
                    not ends_with_space
                    and self._is_closed_quoted_token(raw_current_token or current_token)
                )
            )
            query_fragment: Optional[str] = None
            if query_closed:
                query_fragment = None
            elif prev_token in {"-query", "--query"} and current_token[:1] in {"'", '"'}:
                query_fragment = current_token
            elif query_started_quoted and not ends_with_space and not current_token.startswith("-"):
                if not prev_token.startswith("-"):
                    query_fragment = current_token
            elif (
                query_started_quoted
                and ends_with_space
                and ":" in prev_token
                and not self._is_closed_quoted_token(prev_token)
            ):
                query_fragment = ""
            elif (
                query_fragment is None
                and not query_closed
                and prev_token in {"-query", "--query"}
                and not str(current_token or "").startswith("-")
            ):
                query_fragment = current_token or ""

            if query_fragment is not None:
                field_choices: Dict[str, List[str]] = {}
                ordered_fields: List[str] = []
                query_only_field_names: set = set()
                for spec in query_specs:
                    key = str(spec.get("query_key") or spec.get("name") or "").strip().lower()
                    if not key:
                        continue
                    if spec.get("query_only"):
                        query_only_field_names.add(key)
                    if key not in field_choices:
                        ordered_fields.append(key)
                    field_choices[key] = [str(choice) for choice in list(spec.get("choices", []) or [])]
                    for alias in spec.get("query_aliases", []) or []:
                        alias_text = str(alias or "").strip().lower()
                        if not alias_text:
                            continue
                        field_choices.setdefault(alias_text, field_choices[key])

                plugin_field_map: Dict[str, List[str]] = {}
                if selected_plugin:
                    try:
                        plugin_field_map = plugin_query_field_map(selected_plugin, config)
                    except Exception:
                        plugin_field_map = {}
                    if plugin_field_map:
                        for field_name, choices in plugin_field_map.items():
                            if field_name not in field_choices:
                                ordered_fields.append(field_name)
                            field_choices[field_name] = choices

                file_like = effective_cmd in {
                    "file",
                    "search-file",
                    "add-file",
                    "download-file",
                } or cmd_name == "file"
                plugin_has_instances = bool(
                    selected_plugin and self._plugin_instance_choices(selected_plugin, config)
                )
                if file_like and not plugin_has_instances:
                    ordered_fields = [name for name in ordered_fields if name not in {"instance", "store"}]
                    field_choices.pop("instance", None)
                    field_choices.pop("store", None)

                raw_fragment = str(query_fragment or "")
                segment = raw_fragment[1:] if raw_fragment[:1] in {"'", '"'} else raw_fragment
                query_body = segment
                open_instances = re.search(r"instance:\s*\[([^\]]*)$", segment, flags=re.IGNORECASE)
                if open_instances and selected_plugin:
                    inner = open_instances.group(1)
                    bits = inner.split(",")
                    if inner.endswith(",") or not inner.strip():
                        already = {b.strip().lower() for b in bits if b.strip()}
                        current_name = ""
                    else:
                        already = {b.strip().lower() for b in bits[:-1] if b.strip()}
                        current_name = bits[-1].strip() if bits else ""
                    instance_choices = [
                        name for name in (self._plugin_instance_choices(selected_plugin, config) or [])
                        if str(name).strip().lower() not in already
                    ]
                    current_lower = current_name.lower()
                    filtered = (
                        [name for name in instance_choices if current_lower in str(name).lower()]
                        if current_lower else list(instance_choices)
                    )
                    for name in (filtered or instance_choices):
                        yield Completion(str(name), start_position=-len(current_name), display_meta="instance")
                    if instance_choices:
                        return
                if "," in segment:
                    segment = segment.rsplit(",", 1)[-1].lstrip()
                segment = segment.lstrip()

                if ":" in segment:
                    field, partial = segment.split(":", 1)
                    field = field.strip().lower()
                    partial_lower = partial.strip().lower()

                    inline_choices = []
                    if selected_plugin and effective_cmd in {
                        "search-file",
                        "download-file",
                        "add-file",
                        "file",
                        ".matrix",
                        "matrix",
                        "rooms",
                    }:
                        inline_choices = self._inline_query_choices(selected_plugin, field, config)
                    if field == "room" and selected_plugin == "matrix":
                        instance_hint = None
                        try:
                            match = re.search(
                                r"(?:^|[\s,])instance:([^,\s]+)",
                                query_body,
                                flags=re.IGNORECASE,
                            )
                            if match:
                                instance_hint = str(match.group(1) or "").strip()
                        except Exception:
                            instance_hint = None
                        try:
                            from PluginCore.registry import get_plugin

                            matrix_plugin = get_plugin("matrix", config)
                            if matrix_plugin is not None:
                                names = matrix_plugin.room_choice_names(instance_hint or None)
                                if names:
                                    inline_choices = names
                        except Exception:
                            pass
                    if field in {"instance", "store"}:
                        instance_choices = []
                        if selected_plugin:
                            instance_choices = self._plugin_instance_choices(selected_plugin, config)
                        file_like = effective_cmd in {
                            "file",
                            "search-file",
                            "add-file",
                            "download-file",
                        } or cmd_name == "file"
                        if not instance_choices and not file_like:
                            instance_choices = CmdletIntrospection.instance_choices(config)
                        list_partial = str(partial or "")
                        already: List[str] = []
                        current_name = list_partial
                        if list_partial.startswith("["):
                            inner = list_partial[1:]
                            bits = inner.split(",")
                            if inner.endswith(",") or inner == "":
                                already = [b.strip().lower() for b in bits if b.strip()]
                                current_name = ""
                            else:
                                already = [b.strip().lower() for b in bits[:-1] if b.strip()]
                                current_name = bits[-1].strip() if bits else ""
                        elif "+" in list_partial:
                            bits = list_partial.split("+")
                            already = [b.strip().lower() for b in bits[:-1] if b.strip()]
                            current_name = bits[-1].strip() if bits else ""
                        if instance_choices:
                            instance_choices = [
                                name for name in instance_choices
                                if str(name).strip().lower() not in set(already)
                            ]
                            inline_choices = instance_choices
                            partial = current_name
                            partial_lower = current_name.lower()

                    choice_pool = inline_choices or field_choices.get(field, [])
                    if choice_pool:
                        filtered = (
                            [choice for choice in choice_pool if partial_lower in str(choice).lower()]
                            if partial_lower else list(choice_pool)
                        )
                        for choice in (filtered or choice_pool):
                            yield Completion(str(choice), start_position=-len(partial))
                        return
                else:
                    partial_lower = segment.strip().lower()
                    field_pool = ordered_fields
                    filtered_fields = (
                        [field for field in field_pool if field.startswith(partial_lower)]
                        if partial_lower else field_pool
                    )
                    for field in (filtered_fields or field_pool):
                        yield Completion(f"{field}:", start_position=-len(segment))
                    if filtered_fields or field_pool:
                        return

        if (
            effective_cmd == "search-file"
            and provider_name
            and not ends_with_space
            and ":" in current_token
            and not current_token.startswith("-")
        ):
            # Allow quoted tokens like "system:g
            quote_prefix = current_token[0] if current_token[:1] in {"'", '"'} else ""
            inline_token = current_token[1:] if quote_prefix else current_token
            if inline_token.endswith(quote_prefix) and len(inline_token) > 1:
                inline_token = inline_token[:-1]

            # Allow comma-separated inline specs; operate on the last segment only.
            if "," in inline_token:
                inline_token = inline_token.split(",")[-1].lstrip()

            if ":" not in inline_token:
                return

            field, partial = inline_token.split(":", 1)
            field = field.strip().lower()
            partial_lower = partial.strip().lower()
            inline_choices = self._inline_query_choices(provider_name, field, config)
            if inline_choices:
                filtered = (
                    [c for c in inline_choices if partial_lower in str(c).lower()]
                    if partial_lower
                    else list(inline_choices)
                )
                for choice in (filtered or inline_choices):
                    # Replace only the partial after the colon; keep the field prefix and quotes as typed.
                    start_pos = -len(partial)
                    suggestion = str(choice)
                    yield Completion(suggestion, start_position=start_pos)
                return

        if cmd_name in self._METADATA_COMMANDS:
            yielded_extract = False
            for completion in self._extract_syntax_completions(
                raw_current_token or current_token,
                stage_tokens,
            ):
                yielded_extract = True
                yield completion
            if yielded_extract:
                return
            yielded_placeholder = False
            for completion in self._tag_placeholder_completions(
                raw_current_token or current_token,
                document.text_before_cursor,
            ):
                yielded_placeholder = True
                yield completion
            if yielded_placeholder:
                return
            yielded_function = False
            for completion in self._tag_function_completions(
                raw_current_token or current_token,
                document.text_before_cursor,
            ):
                yielded_function = True
                yield completion
            if yielded_function:
                return

        normalized_prev = prev_token.lstrip("-").strip().lower()
        choices: List[str] = []
        if normalized_prev == "instance" and selected_plugin:
            choices = self._plugin_instance_choices(selected_plugin, config)
            if self._plugin_instance_accepts_direct_path(selected_plugin):
                path_choices = self._path_instance_choices(raw_current_token)
                if path_choices:
                    seen_choice_values = {str(choice).lower() for choice in choices}
                    for choice in path_choices:
                        lowered = str(choice).lower()
                        if lowered in seen_choice_values:
                            continue
                        choices.append(choice)
                        seen_choice_values.add(lowered)
        if not choices and cmd_name in self._METADATA_COMMANDS:
            dispatched = self._dispatched_metadata_cmdlet(stage_tokens)
            if dispatched is not None:
                choices = self._arg_choices_from_object(dispatched, prev_token)
        if not choices:
            choices = self._arg_choices(
                cmd_name=effective_cmd,
                arg_name=prev_token,
                config=config,
                force=False,
            )
        if choices:
            choice_list = choices
            if normalized_prev == "plugin" and current_token:
                raw_plugin = self._strip_quotes(raw_current_token or current_token)
                already: List[str] = []
                current_name = raw_plugin
                if raw_plugin.startswith("["):
                    inner = raw_plugin[1:]
                    bits = inner.split(",")
                    if inner.endswith(",") or inner == "":
                        already = [b.strip().lower() for b in bits if b.strip()]
                        current_name = ""
                    else:
                        already = [b.strip().lower() for b in bits[:-1] if b.strip()]
                        current_name = bits[-1].strip() if bits else ""
                elif "," in raw_plugin or "+" in raw_plugin:
                    sep = "," if "," in raw_plugin else "+"
                    bits = raw_plugin.split(sep)
                    already = [b.strip().lower() for b in bits[:-1] if b.strip()]
                    current_name = bits[-1].strip() if bits else ""
                current_lower = current_name.lower()
                filtered = [
                    name for name in choices
                    if str(name).strip().lower() not in set(already)
                    and (not current_lower or str(name).lower().startswith(current_lower))
                ]
                if filtered:
                    choice_list = filtered
                    raw_current_token = current_name

            if normalized_prev == "instance" and current_token:
                current_lower = current_token.lower()
                filtered = [c for c in choice_list if current_lower in c.lower()]
                if filtered:
                    choice_list = filtered

            for choice in choice_list:
                yield Completion(choice, start_position=-len(raw_current_token))
            return

        if cmd_name in {".table", "table"} and (
            normalized_prev in {"filter", "sort"}
            or str(current_token or "").startswith("@")
        ):
            yielded_col = False
            sort_token = raw_current_token or current_token
            if normalized_prev == "sort":
                for completion in self._table_sort_completions(
                    sort_token,
                    document.text_before_cursor,
                ):
                    yielded_col = True
                    yield completion
            else:
                for completion in self._table_column_completions(sort_token):
                    yielded_col = True
                    yield completion
            if yielded_col:
                return

        if (
            prev_token.startswith("-")
            and not str(current_token or "").startswith("-")
            and self._flag_expects_value(cmd_name, prev_token, config, stage_tokens)
        ):
            return

        arg_names = self._stage_completion_args(
            cmd_name=cmd_name,
            stage_tokens=stage_tokens,
            config=config,
        )
        used_logicals = self._used_arg_logicals(
            effective_cmd,
            stage_tokens,
            config,
            arg_flags=arg_names,
        )
        descriptions = self._arg_descriptions(effective_cmd, config)
        logical_seen: Set[str] = set()
        for arg in arg_names:
            arg_low = arg.lower()
            prefer_single_dash = current_token in {"",
                                                   "-"}
            if prefer_single_dash and arg_low.startswith("--"):
                continue
            logical = arg.lstrip("-").lower()
            if logical in used_logicals:
                continue
            if prefer_single_dash and logical in logical_seen:
                continue
            if arg_low.startswith(current_token):
                yield Completion(
                    arg,
                    start_position=-len(current_token),
                    display_meta=descriptions.get(logical, ""),
                )
                if prefer_single_dash:
                    logical_seen.add(logical)

