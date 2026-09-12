from __future__ import annotations

from importlib import import_module
from typing import Any, Dict, List, Sequence
import sys

from SYS.logger import log
from . import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
SharedArgs = sh.SharedArgs


class File(Cmdlet):
    """Unified file command: file -search|-add|-delete|-download|-merge|..."""

    _ACTION_FLAGS = {
        "search": {"-search", "--search"},
        "add": {"-add", "--add"},
        "delete": {"-delete", "--delete", "-del", "--del"},
        "merge": {"-merge", "--merge"},
        "download": {"-download", "--download", "-dl", "--dl"},
        "convert": {"-convert", "--convert"},
        "trim": {"-trim", "--trim"},
        "archive": {"-archive", "--archive"},
    }

    _ACTION_MODULE = {
        "add": "cmdlet.file.add",
        "delete": "cmdlet.file.delete",
        "merge": "cmdlet.file.merge",
        "download": "cmdlet.file.download",
        "search": "cmdlet.file.search",
        "convert": "cmdlet.file.convert",
        "trim": "cmdlet.file.trim",
        "archive": "cmdlet.file.archive",
    }

    _ACTION_CMDLET = {
        "search": "search-file",
        "add": "add-file",
        "delete": "delete-file",
        "merge": "merge-file",
        "download": "download-file",
        "convert": "convert-file",
        "trim": "trim-file",
        "archive": "archive-file",
    }
    _PLUGIN_SCOPED_ACTIONS = frozenset({"search", "add", "download"})

    @classmethod
    def resolved_cmdlet_name(cls, args: Sequence[str] | None = None) -> str:
        action, _passthrough, _seen = cls._extract_action(args or [])
        if not action:
            return "file"
        return str(cls._ACTION_CMDLET.get(action) or "file")

    def __init__(self) -> None:
        super().__init__(
            name="file",
            summary="Search, add, download, and manage files",
            usage='file (-search|-add|-delete|-download|...) [-plugin NAME] [-query "..."]',
            arg=[
                CmdletArg("-search", type="flag", required=False, description="Search plugins or scrape a page"),
                CmdletArg("-all", type="flag", required=False, description="With -search: list all files, not only those with .metadata"),
                CmdletArg("-add", type="flag", required=False, description="Save a piped/remote file to -path, or ingest into a plugin instance"),
                CmdletArg("-delete", type="flag", required=False, description="Delete from Hydrus or disk", alias="del"),
                CmdletArg("-merge", type="flag", required=False, description="Merge files"),
                CmdletArg("-download", type="flag", required=False, description="Fetch/scrape/export to disk (alias of file -add -path for piped rows)", alias="dl"),
                CmdletArg("-convert", type="flag", required=False, description="Convert format"),
                CmdletArg("-trim", type="flag", required=False, description="Trim media duration"),
                CmdletArg("-archive", type="flag", required=False, description="Archive files"),
                SharedArgs.PLUGIN,
                SharedArgs.INSTANCE,
                SharedArgs.QUERY,
                SharedArgs.LIMIT,
            ],
            detail=[
                "- @N | file -add -path DIR saves the row to disk. @N | file -add -plugin hydrusnetwork ingests into an instance.",
                "- file -download is the fetch/scrape engine; piped -add -path uses it.",
                "- Prefer: file -search|-add|-delete|... then options (-plugin, -query, ...).",
                '- Limit via -query field: -query \"cats limit:30\" (not -limit).',
                "- Bare -query still runs file -search.",
                "- Examples:",
                '    file -search -plugin ytdlp -query "tutorial limit:20"',
                '    file -search "https://example.com/gallery"',
                "    file -download -scrape <url>",
                '    file -add -plugin hydrusnetwork -query "instance:<name>"',
                "    @N | file -download",
            ],
            examples=[
                'file -search -plugin ytdlp -query "tutorial limit:20"',
                'file -search "https://example.com/gallery"',
                "file -download -scrape https://example.com/gallery",
                'file -add -plugin hydrusnetwork -query "instance:Typhon"',
                "@N | file -download",
            ],
            exec=self.run,
        )
        self.register()

    @staticmethod
    def _has_query_arg(args: Sequence[str]) -> bool:
        query_flags = {"-query", "--query"}
        for token in args or []:
            text = str(token or "").strip().lower()
            if text in query_flags:
                return True
            if any(text.startswith(f"{flag}=") for flag in query_flags):
                return True
        return False

    @classmethod
    def _plugin_file_actions(cls) -> Dict[str, Dict[str, Any]]:
        extra: Dict[str, Dict[str, Any]] = {}
        try:
            from PluginCore.registry import REGISTRY

            REGISTRY.discover()
            for info in REGISTRY.iter_plugins():
                spec = getattr(info.plugin_class, "FILE_ACTIONS", None) or {}
                if not isinstance(spec, dict):
                    continue
                for name, payload in spec.items():
                    key = str(name or "").strip().lower()
                    if key and isinstance(payload, dict):
                        extra[key] = payload
        except Exception:
            return extra
        return extra

    @classmethod
    def _all_action_flags(cls) -> Dict[str, set[str]]:
        flags = {name: set(variants) for name, variants in cls._ACTION_FLAGS.items()}
        for name, spec in cls._plugin_file_actions().items():
            variants = spec.get("flags") or (f"-{name}", f"--{name}")
            flags[name] = {str(item).strip().lower() for item in variants if str(item).strip()}
        return flags

    @classmethod
    def _extract_action(cls, args: Sequence[str]) -> tuple[str | None, List[str], List[str]]:
        matched_actions: List[str] = []
        passthrough: List[str] = []
        action_flags = cls._all_action_flags()

        for token in args or []:
            text = str(token or "")
            lower = text.strip().lower()
            matched = None
            for action_name, variants in action_flags.items():
                if lower in variants:
                    matched = action_name
                    break
            if matched:
                matched_actions.append(matched)
                continue
            passthrough.append(text)

        unique_actions: List[str] = []
        for action in matched_actions:
            if action not in unique_actions:
                unique_actions.append(action)

        if not unique_actions and cls._has_query_arg(passthrough):
            return "search", passthrough, unique_actions

        if len(unique_actions) != 1:
            return None, passthrough, unique_actions
        return unique_actions[0], passthrough, unique_actions

    @classmethod
    def _dispatch(cls, action: str, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        module_name = cls._ACTION_MODULE.get(action)
        if not module_name:
            spec = cls._plugin_file_actions().get(action) or {}
            module_name = str(spec.get("module") or "").strip()
        if not module_name:
            log(f"file: unsupported action '{action}'", file=sys.stderr)
            return 1

        module = import_module(module_name)

        cmdlet_obj = getattr(module, "CMDLET", None)
        if cmdlet_obj is not None:
            exec_fn = getattr(cmdlet_obj, "exec", None)
            if callable(exec_fn):
                return int(exec_fn(result, args, config))

        log(f"file: cannot dispatch action '{action}' via module '{module_name}'", file=sys.stderr)
        return 1

    def run(self, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        action, passthrough_args, seen = self._extract_action(args)

        if action is None:
            if not seen:
                log(
                    "file: missing action; use -search/-query for search or one of "
                    + ", ".join(f"-{name}" for name in list(self._ACTION_FLAGS) + list(self._plugin_file_actions())),
                    file=sys.stderr,
                )
            else:
                rendered = ", ".join(f"-{name}" for name in seen)
                log(f"file: conflicting actions ({rendered}); choose exactly one", file=sys.stderr)
            return 1

        if action in self._PLUGIN_SCOPED_ACTIONS:
            try:
                from SYS.instance_chooser import maybe_publish_instance_chooser

                if maybe_publish_instance_chooser(args, config, command="file"):
                    return 0
            except Exception:
                pass

        return self._dispatch(action, result, passthrough_args, config)


CMDLET = File()
