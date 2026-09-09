from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence
import sys
from importlib import import_module

from PluginCore.commands import get_primary_command_object
from SYS.logger import log
from . import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
SharedArgs = sh.SharedArgs


class Metadata(Cmdlet):
    """Unified metadata command with domain + action routing."""

    _ACTION_FLAGS = {
        "add": {"-add", "--add"},
        "delete": {"-delete", "--delete", "-del", "--del"},
        "get": {"-get", "--get"},
        "inspect": {"-inspect", "--inspect", "-info", "--info", "-file", "--file"},
    }
    _PLUGIN_AUTO_FLAGS = frozenset({"-auto", "--auto", "-autotag", "--autotag"})

    _DOMAIN_FLAGS = {
        "tag": {"-tag", "--tag", "-tags", "--tags"},
        "url": {"-url", "--url", "-urls", "--urls"},
        "relationship": {
            "-relationship",
            "--relationship",
            "-relationships",
            "--relationships",
            "-rel",
            "--rel",
        },
        "note": {"-note", "--note", "-notes", "--notes"},
        "file": {"-file", "--file"},
    }

    _DISPATCH = {
        ("tag", "add"): "cmdlet.metadata.tag_add",
        ("tag", "delete"): "cmdlet.metadata.tag_delete",
        ("tag", "get"): "cmdlet.metadata.tag_get",
        ("url", "add"): "cmdlet.metadata.url_add",
        ("url", "delete"): "cmdlet.delete_url",
        ("url", "get"): "cmdlet.get_url",
        ("note", "add"): "cmdlet.metadata.note_add",
        ("note", "delete"): "cmdlet.delete_note",
        ("note", "get"): "cmdlet.metadata.get_note",
        ("relationship", "add"): "cmdlet.metadata.relationship_add",
        ("relationship", "delete"): "cmdlet.delete_relationship",
        ("relationship", "get"): "cmdlet.metadata.get_relationship",
        ("inspect", None): "cmdlet.get_metadata",
    }

    def __init__(self) -> None:
        super().__init__(
            name="metadata",
            summary="Change file tags and metadata",
            usage='metadata [-tag|-url|-relationship|-note] (-add|-delete|-get) [args]  OR  metadata -inspect [args]',
            alias=["meta"],
            arg=[
                SharedArgs.QUERY,
                SharedArgs.INSTANCE,
                CmdletArg("-tag", type="flag", required=False, description="Metadata tag domain (default if omitted)"),
                CmdletArg("-url", type="flag", required=False, description="URL metadata domain"),
                CmdletArg("-relationship", type="flag", required=False, description="Relationship metadata domain", alias="rel"),
                CmdletArg("-note", type="flag", required=False, description="Note metadata domain"),
                CmdletArg("-add", type="flag", required=False, description="Add metadata tag value(s)"),
                CmdletArg("-delete", type="flag", required=False, description="Delete metadata tag value(s)", alias="del"),
                CmdletArg("-get", type="flag", required=False, description="Read metadata values for selected domain"),
                CmdletArg("-inspect", type="flag", required=False, description="Inspect file metadata details", alias="info"),
                CmdletArg(
                    "-extract",
                    type="string",
                    required=False,
                    description='Extract namespaced tags from the title. Implies -add. Example: -extract "(artist) - (title)"',
                ),
                CmdletArg(
                    "-duplicate",
                    type="string",
                    required=False,
                    description="Copy existing tag values to new namespaces. Implies -add.",
                ),
            ],
            detail=[
                "- Actions: -add, -delete, -get, -inspect. Domain defaults to -tag.",
                "- Domains: -tag (default), -url, -relationship, -note.",
                "- Pipe rows for bulk edits: @1-50 | metadata -add \"namespace:value\".",
                "- Templates: metadata -add \"title:$(track) - $(series)\".",
                "- Regex: metadata -add \"channel:<regex($(channel),'^prefix ','')>\".",
                "- Transforms: padding, default, replace, regex, increment.",
                '- Extract from title: metadata -tag -extract "(artist) - (title)" (implies -add).',
                "- Copy namespaces: metadata -add -duplicate title:album,artist.",
                "- See docs/tag_template_syntax.md for the full template syntax.",
            ],
            examples=[
                "metadata -get",
                'metadata -add "series:example"',
                'metadata -add "title:$(track) - $(series)"',
                "metadata -tag -extract \"(artist) - (title)\"",
                "metadata -add -extract \"(artist) - (title)\"",
                "metadata -add -duplicate title:album,artist",
                "metadata -add \"channel:<regex(#(channel),'^kabbalah ','')>\"",
                "@1-20 | metadata -add \"source:batch\"",
                "metadata -url -add https://example.com",
                "metadata -relationship -get",
                "metadata -inspect",
            ],
            exec=self.run,
        )
        self.register()

    @classmethod
    def _plugin_metadata_actions(cls) -> Dict[str, Dict[str, Any]]:
        extra: Dict[str, Dict[str, Any]] = {}

        def _ingest(spec: Any) -> None:
            if not isinstance(spec, dict):
                return
            for name, payload in spec.items():
                key = str(name or "").strip().lower()
                if key and isinstance(payload, dict):
                    extra[key] = payload

        try:
            from PluginCore.registry import REGISTRY, import_plugin_module

            REGISTRY.discover()
            for info in REGISTRY.iter_plugins():
                _ingest(getattr(info.plugin_class, "METADATA_ACTIONS", None))
            if "auto" not in extra:
                for mod_name in ("metadata_plus", "metadata_plugin"):
                    mod = import_plugin_module(mod_name)
                    if mod is None:
                        continue
                    plugin_cls = getattr(mod, "MetadataPlus", None)
                    _ingest(getattr(plugin_cls, "METADATA_ACTIONS", None) if plugin_cls else None)
                    if "auto" in extra:
                        break
        except Exception:
            return extra
        return extra

    @classmethod
    def _all_action_flags(cls) -> Dict[str, set[str]]:
        flags = {name: set(variants) for name, variants in cls._ACTION_FLAGS.items()}
        for name, spec in cls._plugin_metadata_actions().items():
            variants = spec.get("flags") or (f"-{name}", f"--{name}")
            flags[name] = {str(item).strip().lower() for item in variants if str(item).strip()}
        return flags

    def plugin_contributed_args(self) -> List[Any]:
        extra: List[Any] = []
        for name, spec in self._plugin_metadata_actions().items():
            extra.append(
                CmdletArg(
                    f"-{name}",
                    type="flag",
                    required=False,
                    description=str(spec.get("description") or name),
                    alias=str(spec.get("alias") or ""),
                )
            )
        return extra

    def plugin_contributed_examples(self) -> List[str]:
        extra: List[str] = []
        for spec in self._plugin_metadata_actions().values():
            for example in spec.get("examples") or ():
                text = str(example or "").strip()
                if text:
                    extra.append(text)
        return extra

    @classmethod
    def _extract_parts(
        cls,
        args: Sequence[str],
    ) -> tuple[str | None, str, List[str], List[str], List[str]]:
        matched_actions: List[str] = []
        matched_domains: List[str] = []
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

            matched_domain = None
            for domain_name, variants in cls._DOMAIN_FLAGS.items():
                if lower in variants:
                    matched_domain = domain_name
                    break
            if matched_domain:
                matched_domains.append(matched_domain)
                continue

            passthrough.append(text)

        unique_actions: List[str] = []
        for action in matched_actions:
            if action not in unique_actions:
                unique_actions.append(action)

        unique_domains: List[str] = []
        for domain in matched_domains:
            if domain not in unique_domains:
                unique_domains.append(domain)

        if "auto" in unique_actions and "delete" not in unique_actions:
            unique_actions = ["auto"]
        action = unique_actions[0] if len(unique_actions) == 1 else None
        domain = unique_domains[0] if len(unique_domains) == 1 else "tag"
        if action is None and domain == "tag":
            implied = False
            for token in passthrough:
                logical = str(token or "").strip().lower().lstrip("-").split("=", 1)[0]
                if logical in {"extract", "duplicate", "extract-debug"}:
                    implied = True
                    break
            if implied:
                action = "add"
                unique_actions = ["add"]
        return action, domain, passthrough, unique_actions, unique_domains

    @staticmethod
    def _flag_logicals(flag_map: Dict[str, Any]) -> frozenset:
        out: set[str] = set()
        for name, variants in (flag_map or {}).items():
            key = str(name or "").strip().lower()
            if key:
                out.add(key)
            for variant in variants or ():
                logical = str(variant or "").lstrip("-").strip().lower()
                if logical:
                    out.add(logical)
        return frozenset(out)

    @classmethod
    def action_logicals(cls) -> frozenset:
        return cls._flag_logicals(cls._all_action_flags())

    @classmethod
    def domain_logicals(cls) -> frozenset:
        return cls._flag_logicals(cls._DOMAIN_FLAGS)

    @classmethod
    def dispatched_cmdlet(cls, args: Sequence[str]) -> Optional[Any]:
        """Return the registered leaf cmdlet for the current metadata action/domain."""
        action, domain, _passthrough, _seen_actions, _seen_domains = cls._extract_parts(args)
        if action is None:
            return None
        plugin_spec = cls._plugin_metadata_actions().get(action) or {}
        module_name = str(plugin_spec.get("module") or "").strip()
        if not module_name:
            if action == "inspect":
                module_name = cls._DISPATCH.get(("inspect", None))
            else:
                module_name = cls._DISPATCH.get((domain, action))
        if not module_name:
            return None
        try:
            module = import_module(module_name)
        except Exception:
            return None
        return get_primary_command_object(module)

    @classmethod
    def _auto_flag_present(cls, args: Sequence[str]) -> bool:
        return any(
            str(token or "").strip().lower() in cls._PLUGIN_AUTO_FLAGS
            for token in (args or [])
        )

    def run(self, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        action, domain, passthrough_args, seen_actions, seen_domains = self._extract_parts(args)

        if self._auto_flag_present(args) and "auto" not in self._plugin_metadata_actions():
            log(
                "metadata -auto requires the metadata+ plugin. Install with .plugin -add metadata_plus",
                file=sys.stderr,
            )
            return 1

        if action is None:
            if not seen_actions:
                available = ", ".join(f"-{name}" for name in self._all_action_flags())
                log(
                    f"metadata: missing action flag; choose exactly one of {available}",
                    file=sys.stderr,
                )
            else:
                rendered = ", ".join(f"-{name}" for name in seen_actions)
                log(f"metadata: conflicting actions ({rendered}); choose exactly one", file=sys.stderr)
            return 1

        if len(seen_domains) > 1:
            rendered_domains = ", ".join(f"-{name}" for name in seen_domains)
            log(f"metadata: conflicting domains ({rendered_domains}); choose one domain", file=sys.stderr)
            return 1

        if domain == "file" and action != "inspect":
            log("metadata: -file only supports -inspect; use metadata -inspect", file=sys.stderr)
            return 1

        target = self.dispatched_cmdlet(args)
        exec_fn = getattr(target, "exec", None) if target is not None else None
        if not callable(exec_fn):
            exec_fn = getattr(target, "run", None) if target is not None else None
        if callable(exec_fn):
            return int(exec_fn(result, passthrough_args, config))

        log(f"metadata: unsupported domain '{domain}'", file=sys.stderr)
        return 1


CMDLET = Metadata()
