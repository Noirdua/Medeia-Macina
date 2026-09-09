from __future__ import annotations

import re

from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Callable


@dataclass
class SearchResult:
    """Unified search result format across all search plugins."""

    table: str  # Plugin name: "libgen", "soulseek", "bandcamp", "youtube", etc.
    title: str  # Display title/filename
    path: str  # Download target (URL, path, magnet, identifier)

    detail: str = ""  # Additional description
    annotations: List[str] = field(
        default_factory=list
    )  # Tags: ["120MB", "flac", "ready"]
    media_kind: str = "other"  # Type: "book", "audio", "video", "game", "magnet"
    size_bytes: Optional[int] = None
    tag: set[str] = field(default_factory=set)  # Searchable tag values
    columns: List[Tuple[str, str]] = field(default_factory=list)  # Display columns
    selection_action: Optional[List[str]] = None
    selection_args: Optional[List[str]] = None
    full_metadata: Dict[str, Any] = field(default_factory=dict)  # Extra metadata

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for pipeline processing."""
        full_metadata = self.full_metadata if isinstance(self.full_metadata, dict) else {}
        out = {
            "table": self.table,
            "title": self.title,
            "path": self.path,
            "detail": self.detail,
            "annotations": self.annotations,
            "media_kind": self.media_kind,
            "size_bytes": self.size_bytes,
            "tag": list(self.tag),
            "columns": list(self.columns),
            "full_metadata": full_metadata,
        }

        for key in (
            "url",
            "hash",
            "hash_hex",
            "store",
            "name",
            "mime",
            "file_id",
            "ext",
            "size",
        ):
            value = None
            try:
                value = getattr(self, key, None)
            except Exception:
                value = None
            if value is None and key in full_metadata:
                value = full_metadata.get(key)
            if value is not None:
                out[key] = value

        try:
            selection_args = getattr(self, "selection_args", None)
        except Exception:
            selection_args = None
        if selection_args is None:
            try:
                fm = getattr(self, "full_metadata", None)
                if isinstance(fm, dict):
                    selection_args = fm.get("_selection_args") or fm.get("selection_args")
            except Exception:
                selection_args = None
        if selection_args:
            out["_selection_args"] = selection_args

        try:
            selection_action = getattr(self, "selection_action", None)
        except Exception:
            selection_action = None
        if selection_action is None:
            try:
                fm = getattr(self, "full_metadata", None)
                if isinstance(fm, dict):
                    selection_action = fm.get("_selection_action") or fm.get("selection_action")
            except Exception:
                selection_action = None
        if selection_action:
            normalized = [str(x) for x in selection_action if x is not None]
            if normalized:
                out["_selection_action"] = normalized

        return out


def parse_inline_query_arguments(raw_query: str) -> Tuple[str, Dict[str, str]]:
    """Extract inline key:value arguments from a plugin search query."""

    query_text = str(raw_query or "").strip()
    if not query_text:
        return "", {}

    tokens = re.split(r"[,\s]+", query_text)
    leftover: List[str] = []
    parsed_args: Dict[str, str] = {}

    for token in tokens:
        if not token:
            continue
        sep_index = token.find(":")
        if sep_index < 0:
            sep_index = token.find("=")
        if sep_index > 0:
            key = token[:sep_index].strip().lower()
            value = token[sep_index + 1 :].strip()
            if key and value:
                parsed_args[key] = value
                continue
        leftover.append(token)

    return " ".join(leftover).strip(), parsed_args


class Plugin(ABC):
    """Unified plugin base class.

    Concrete plugins may implement any subset of:
    - search(query, ...)
    - download(result, output_dir)
    - upload(file_path, ...)
    - login(...)
    - validate()
    """

    URL: Sequence[str] = ()
    PLUGIN_NAME: str = ""
    PLUGIN_VERSION: str = ""
    PLUGIN_DESCRIPTION: str = ""
    PLUGIN_AUTHOR: str = ""
    PLUGIN_REQUIRES: Sequence[str] = ()
    PLUGIN_SYSTEM_REQUIRES: Sequence[str] = ()
    PLUGIN_DEPENDS: Sequence[str] = ()
    PLUGIN_ALIASES: Sequence[str] = ()

    # Optional plugin-driven defaults for what to do when a user selects @N from a
    # plugin table. The CLI uses this to auto-insert stages (e.g. download-file)
    # without hardcoding table names.
    #
    # Example:
    #   TABLE_AUTO_STAGES = {"youtube": ["download-file"]}
    #   TABLE_AUTO_PREFIXES = {"tidal": ["download-file"]}  # matches tidal.*
    TABLE_AUTO_STAGES: Dict[str, Sequence[str]] = {}
    TABLE_AUTO_PREFIXES: Dict[str, Sequence[str]] = {}
    AUTO_STAGE_USE_SELECTION_ARGS: bool = False

    # Optional plugin-declared configuration keys.
    # Used for dynamically generating config panels (e.g., missing credentials).
    REQUIRED_CONFIG_KEYS: Sequence[str] = ()

    # Some plugins implement `upload()` but are not intended to be used as
    # generic "file host" plugins via `add-file -plugin ...`.
    EXPOSE_AS_FILE_PROVIDER: bool = True

    # Set to True for plugins that support multiple named instances in config.
    # When True, config is expected at config["plugin"][<PLUGIN_NAME>][<instance_name>]
    # rather than config["plugin"][<PLUGIN_NAME>] directly.
    # Examples: hydrusnetwork (home/work), matrix (personal/work), ftp.
    MULTI_INSTANCE: bool = False

    # Declare which top-level cmdlet names this plugin handles.
    # Cmdlet dispatch and capability discovery use this to route operations.
    # Example: frozenset({"add-file", "download-file", "get-tag", "search-file"})
    SUPPORTED_CMDLETS: frozenset = frozenset()

    # Extra `file -<action>` flags contributed by an installed plugin:
    # {"screenshot": {"flags": ("-screenshot", "-shot"), "module": "plugins.playwright.screenshot", "cmdlet": "screen-shot", "description": "..."}}
    FILE_ACTIONS: Dict[str, Dict[str, Any]] = {}

    # Extra `metadata -<action>` flags contributed by an installed plugin:
    # {"auto": {"flags": ("-auto", "-autotag"), "module": "cmdlet.metadata.auto_tag", "description": "..."}}
    METADATA_ACTIONS: Dict[str, Dict[str, Any]] = {}

    # Lines shown in `.config` when this plugin's section is open.
    # String or sequence of strings. Example: ("Get an API key at https://example.com/api",)
    CONFIG_HELP: Sequence[str] | str = ()

    # Files `.config -upload` may install into this plugin folder.
    # Each entry: dest filename, optional suffixes, optional name needles.
    # Example: ({"dest": "cookies.txt", "suffixes": (".txt",), "needles": ("cookie",)},)
    PLUGIN_UPLOADS: Sequence[Dict[str, Any]] = ()

    # Extra item-detail rows: (label, source_key, after_label).
    # Shown in the #1 item panel only when the source key has a value.
    # Example: (("File ID", "file_id", "Hash"),)
    ITEM_DETAIL_FIELDS: Sequence[Tuple[str, str, str]] = ()

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.name = str(
            getattr(self, "PLUGIN_NAME", None)
            or self.__class__.__name__
        ).lower()

    @property
    def label(self) -> str:
        """Friendly display name for the plugin."""
        name = str(getattr(self, "PLUGIN_NAME", None) or self.__class__.__name__)

        if name:
            if name.lower() == "loc":
                return "LoC"
            if name.lower() in {"archive.org", "archiveorg"}:
                return "Archive.org"
            if name.lower() == "openlibrary":
                return "OpenLibrary"
            if name.lower() == "internetarchive":
                return "Internet Archive"
            if name.lower() == "alldebrid":
                return "AllDebrid"
            return name[:1].upper() + name[1:]
        return self.__class__.__name__

    @property
    def preserve_order(self) -> bool:
        """True if search result order is significant and should be preserved in displays."""
        return False

    def get_table_type(self, query: str, filters: Optional[Dict[str, Any]] = None) -> str:
        """Return the table type identifier for results from this plugin."""
        return self.name

    def get_table_title(self, query: str, filters: Optional[Dict[str, Any]] = None) -> str:
        """Return a descriptive title for the results table."""
        q = str(query or "").strip() or "*"
        return f"{self.label}: {q}"

    def get_table_metadata(self, query: str, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Return metadata for the results table."""
        return {"plugin": self.name}

    def item_detail_fields(self, item: Any) -> List[Tuple[str, Any, str]]:
        """Return extra item-detail rows: (label, value, after_label)."""
        rows: List[Tuple[str, Any, str]] = []
        for spec in self.ITEM_DETAIL_FIELDS or ():
            if not spec:
                continue
            if len(spec) >= 3:
                label, key, after = spec[0], spec[1], spec[2]
            elif len(spec) == 2:
                label, key, after = spec[0], spec[1], "Hash"
            else:
                continue
            value = self._item_detail_field_value(item, str(key))
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            rows.append((str(label), value, str(after or "Hash")))
        return rows

    @staticmethod
    def _item_detail_field_value(item: Any, key: str) -> Any:
        if not key:
            return None
        if isinstance(item, dict):
            if item.get(key) is not None:
                return item.get(key)
            nested = item.get("full_metadata") or item.get("metadata")
            if isinstance(nested, dict) and nested.get(key) is not None:
                return nested.get(key)
            return None
        try:
            value = getattr(item, key, None)
        except Exception:
            value = None
        if value is not None:
            return value
        try:
            nested = getattr(item, "full_metadata", None) or getattr(item, "metadata", None)
        except Exception:
            nested = None
        if isinstance(nested, dict) and nested.get(key) is not None:
            return nested.get(key)
        to_dict = getattr(item, "to_dict", None)
        if callable(to_dict):
            try:
                payload = to_dict()
            except Exception:
                payload = None
            if isinstance(payload, dict):
                return Plugin._item_detail_field_value(payload, key)
        return None

    def get_source_command(self, args_list: List[str]) -> Tuple[str, List[str]]:
        """Return the command and arguments that produced this search result.
        
        Used for @N expansion to re-run the search if needed.
        """
        return "search-file", list(args_list)

    def resolve_pipe_item_context(
        self,
        item: Any,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        store: Optional[str] = None,
        file_hash: Optional[str] = None,
        targets: Optional[Sequence[str]] = None,
    ) -> Optional[Tuple[Optional[str], Optional[str]]]:
        """Optionally normalize store/hash context for pipe playback helpers."""
        _ = item, metadata, store, file_hash, targets
        return None

    def infer_playlist_store(
        self,
        item: Any,
        *,
        target: str,
        file_storage: Any = None,
    ) -> Optional[str]:
        """Optionally infer a friendly store label for an MPV playlist entry."""
        _ = item, target, file_storage
        return None

    @property
    def prefers_transfer_progress(self) -> bool:
        """True if this plugin prefers explicit transfer progress tracking (begin/finish) during download."""
        return False

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        """Return configuration schema for this plugin.
        
        Returns a list of dicts, each defining a field:
        {
            "key": "api_key",
            "label": "API Key",
            "group": "Authentication",
            "type": "text",  # text|boolean|integer|float|path|secret|multiline
            "default": "",
            "required": True,
            "secret": True,
            "choices": ["Option 1", "Option 2"],
            "placeholder": "Paste value here"
        }
        """
        return []

    @classmethod
    def required_config_keys(cls) -> List[str]:
        keys = getattr(cls, "REQUIRED_CONFIG_KEYS", None)
        out: List[str] = []
        seen: set[str] = set()
        try:
            for k in list(keys or []):
                s = str(k or "").strip()
                low = s.lower()
                if s and low not in seen:
                    seen.add(low)
                    out.append(s)
        except Exception:
            pass
        try:
            for field in cls.config_schema() or []:
                if not isinstance(field, dict) or not field.get("required"):
                    continue
                s = str(field.get("key") or "").strip()
                low = s.lower()
                if s and low not in seen:
                    seen.add(low)
                    out.append(s)
        except Exception:
            pass
        return out

    @classmethod
    def required_config_fields(cls) -> List[Dict[str, str]]:
        fields: List[Dict[str, str]] = []
        seen: set[str] = set()
        try:
            for field in cls.config_schema() or []:
                if not isinstance(field, dict) or not field.get("required"):
                    continue
                key = str(field.get("key") or "").strip()
                if not key or key.lower() in seen:
                    continue
                seen.add(key.lower())
                fields.append(
                    {
                        "key": key,
                        "label": str(field.get("label") or key).strip() or key,
                    }
                )
        except Exception:
            pass
        for key in cls.required_config_keys():
            if key.lower() in seen:
                continue
            seen.add(key.lower())
            fields.append({"key": key, "label": key})
        return fields

    def missing_required_config(self) -> List[Dict[str, str]]:
        required = self.required_config_fields()
        bags: List[Dict[str, Any]] = []
        try:
            instances = self.plugin_instance_configs()
        except Exception:
            instances = {}
        if instances:
            bags.extend(value for value in instances.values() if isinstance(value, dict))
        try:
            root = self.plugin_config_root()
        except Exception:
            root = {}
        if isinstance(root, dict) and root:
            bags.append(root)
        if isinstance(self.config, dict):
            bags.append(self.config)

        def _has_value(bag: Dict[str, Any], key: str) -> bool:
            want = key.strip().lower()
            for raw_key, raw_value in bag.items():
                if str(raw_key or "").strip().lower() != want:
                    continue
                if raw_value in (None, "", [], {}, False):
                    continue
                if isinstance(raw_value, str) and not raw_value.strip():
                    continue
                return True
            return False

        missing: List[Dict[str, str]] = []
        for field in required:
            if any(_has_value(bag, field["key"]) for bag in bags):
                continue
            missing.append(field)
        return missing

    @classmethod
    def plugin_config_key(cls) -> str:
        return str(getattr(cls, "PLUGIN_NAME", None) or cls.__name__ or "").strip().lower()

    @classmethod
    def plugin_instance_filter_keys(cls) -> Tuple[str, ...]:
        return ("instance", "store")

    @classmethod
    def plugin_config_field_keys(cls) -> set[str]:
        keys: set[str] = set()
        try:
            for field in cls.config_schema() or []:
                if not isinstance(field, dict):
                    continue
                key = str(field.get("key") or "").strip().lower()
                if key:
                    keys.add(key)
        except Exception:
            return set()
        return keys

    def requested_instance_name(
        self,
        filters: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Optional[str]:
        for key in self.plugin_instance_filter_keys():
            value = kwargs.get(key)
            if value in (None, "") and isinstance(filters, dict):
                value = filters.get(key)
            text = str(value or "").strip()
            if text:
                return text
        return None

    def plugin_config_root(self) -> Dict[str, Any]:
        if not isinstance(self.config, dict):
            return {}
        section_cfg = self.config.get("plugin")
        if isinstance(section_cfg, dict):
            entry = section_cfg.get(self.plugin_config_key())
            if isinstance(entry, dict):
                return dict(entry)
        return {}

    def plugin_instance_configs(self) -> Dict[str, Dict[str, Any]]:
        entry = self.plugin_config_root()
        if not entry:
            return {}

        if bool(getattr(self, "MULTI_INSTANCE", False)):
            try:
                from SYS.config import normalize_multi_instance_plugin_block

                plugin_name = str(
                    getattr(self, "PLUGIN_NAME", None)
                    or getattr(self, "plugin_name", None)
                    or ""
                ).strip()
                return normalize_multi_instance_plugin_block(plugin_name, entry)
            except Exception:
                pass

        schema_keys = self.plugin_config_field_keys()
        entry_keys = {str(key or "").strip().lower() for key in entry.keys()}
        has_nested = any(isinstance(value, dict) for value in entry.values())
        has_scalar = any(not isinstance(value, dict) for value in entry.values())
        # Hybrid flat+nested is multi-instance shaped, not a single default bag.
        if has_nested and has_scalar:
            looks_like_single = False
        elif has_nested and all(isinstance(value, dict) for value in entry.values()):
            looks_like_single = False
        else:
            looks_like_single = bool(schema_keys and entry_keys.intersection(schema_keys))

        if looks_like_single:
            return {"default": dict(entry)}

        instances: Dict[str, Dict[str, Any]] = {}
        for raw_name, raw_value in entry.items():
            if not isinstance(raw_value, dict):
                continue
            name = str(raw_name or "").strip()
            if not name:
                continue
            instances[name] = dict(raw_value)
        return instances

    def configured_instances(self) -> List[str]:
        instances = self.plugin_instance_configs()
        if not instances:
            return []
        if set(instances.keys()) == {"default"}:
            return []
        return list(instances.keys())

    def resolve_plugin_instance(
        self,
        instance_name: Optional[str] = None,
        *,
        require_explicit: bool = False,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        instances = self.plugin_instance_configs()
        if not instances:
            return None, {}

        requested = str(instance_name or "").strip()
        if not requested:
            first_name = next(iter(instances.keys()))
            resolved = dict(instances[first_name])
            if first_name != "default":
                resolved.setdefault("_instance_name", first_name)
            return (None if first_name == "default" else first_name), resolved

        requested_lower = requested.lower()
        for name, cfg in instances.items():
            aliases = {str(name).strip().lower()}
            explicit_name = str(cfg.get("_instance_name") or cfg.get("instance") or cfg.get("name") or "").strip().lower()
            if explicit_name:
                aliases.add(explicit_name)
            if requested_lower in aliases:
                resolved = dict(cfg)
                if name != "default":
                    resolved.setdefault("_instance_name", name)
                return (None if name == "default" else name), resolved

        if require_explicit:
            return None, {}

        first_name = next(iter(instances.keys()))
        resolved = dict(instances[first_name])
        if first_name != "default":
            resolved.setdefault("_instance_name", first_name)
        return (None if first_name == "default" else first_name), resolved

    def extract_query_arguments(self, query: str) -> Tuple[str, Dict[str, Any]]:
        """Allow plugins to normalize query text and parse inline arguments."""

        normalized = str(query or "").strip()
        return normalized, {}

    def postprocess_search_results(
        self,
        *,
        query: str,
        results: List[SearchResult],
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 50,
        table_type: str = "",
        table_meta: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[SearchResult], Optional[str], Optional[Dict[str, Any]]]:
        """Optional hook for plugin-specific result transforms.

        Cmdlets should avoid hardcoding plugin quirks. Plugins can override
        this to:
        - expand/replace result sets (e.g., artist -> albums)
        - override the table type
        - override table metadata

        Returns:
          (results, table_type_override, table_meta_override)
        """

        _ = query
        _ = filters
        _ = limit
        _ = table_type
        _ = table_meta
        return results, None, None

    # Standard lifecycle/auth hook.
    def login(self, **_kwargs: Any) -> bool:
        return True

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str,
                               Any]] = None,
        **kwargs: Any,
    ) -> List[SearchResult]:
        """Search for items matching the query."""
        raise NotImplementedError(f"Plugin '{self.name}' does not support search")

    def download(self, result: SearchResult, output_dir: Path) -> Optional[Path]:
        """Download an item from a search result."""

        return None

    def download_items(
        self,
        result: SearchResult,
        output_dir: Path,
        *,
        emit: Callable[[Path, str, str, Dict[str, Any]], None],
        progress: Any,
        quiet_mode: bool,
        path_from_result: Callable[[Any], Path],
        config: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Optional multi-item download hook (default no-op)."""

        _ = result
        _ = output_dir
        _ = emit
        _ = progress
        _ = quiet_mode
        _ = path_from_result
        _ = config
        return 0

    def resolve_pipe_result_download(
        self,
        result: Any,
        pipe_obj: Any,
    ) -> Tuple[Optional[Path], Optional[str], Optional[Path]]:
        """Materialize a piped plugin result into a local file for add-file."""

        _ = result
        _ = pipe_obj
        return None, None, None

    def expand_selection(
        self,
        selected_items: List[Any],
        *,
        ctx: Any,
        stage_is_last: bool = True,
        table_type: str = "",
        **_kwargs: Any,
    ) -> Optional[List[Any]]:
        """Optionally expand a selection into downstream items for non-terminal pipelines."""

        _ = selected_items
        _ = ctx
        _ = stage_is_last
        _ = table_type
        return None

    def status_summary(self) -> Dict[str, Any]:
        """Return plugin-owned status details for startup/status views."""

        enabled = False
        try:
            enabled = bool(self.validate())
        except Exception:
            enabled = False
        return {
            "status": "ENABLED" if enabled else "DISABLED",
            "name": self.label,
            "plugin": self.name,
            "detail": "Configured" if enabled else "Not configured",
        }

    def handle_url(self, url: str, *, output_dir: Optional[Path] = None) -> Tuple[bool, Optional[Path | Dict[str, Any]]]:
        """Optional plugin override to parse and act on URLs."""

        _ = url
        _ = output_dir
        return False, None

    def download_url(self, url: str, output_dir: Path, **_kwargs: Any) -> Optional[Any]:
        """Optional direct-URL download hook used by generic cmdlets."""

        _ = url
        _ = output_dir
        return None

    def resolve_url(self, url: str, **_kwargs: Any) -> str:
        """Optionally normalize or exchange a URL before downstream use."""

        return str(url or "")

    def resolve_playback_path(self, item: Any, **_kwargs: Any) -> Optional[str]:
        """Optionally turn a plugin-owned item into a playable local path or URL."""

        _ = item
        return None

    def list_url_formats(self, url: str, **_kwargs: Any) -> Optional[List[Dict[str, Any]]]:
        """Optionally return picker-friendly format metadata for a URL."""

        _ = url
        return None

    def filter_picker_formats(
        self,
        formats: List[Dict[str, Any]],
        **_kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Optionally filter or reorder raw format rows before UI display."""

        return list(formats or [])

    def enrich_playlist_entries(
        self,
        entries: List[Dict[str, Any]],
        **_kwargs: Any,
    ) -> Optional[List[Dict[str, Any]]]:
        """Optionally expand lightweight playlist entries with richer metadata."""

        _ = entries
        return None

    def maybe_show_picker(
        self,
        *,
        url: str,
        item: Optional[Any] = None,
        parsed: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None,
        quiet_mode: bool = False,
    ) -> Optional[int]:
        """Optional hook for plugins that want to render an interactive picker/table."""

        _ = url
        _ = item
        _ = parsed
        _ = config
        _ = quiet_mode
        return None

    def config_helper_text(self) -> str:
        """Optional helper text shown in the config editor."""

        return ""

    @classmethod
    def config_header_lines(cls, *, instance_name: Optional[str] = None) -> List[str]:
        _ = instance_name
        lines: List[str] = []
        seen: set[str] = set()

        def _add(raw: Any) -> None:
            for chunk in str(raw or "").splitlines():
                text = chunk.strip()
                if text and text not in seen:
                    seen.add(text)
                    lines.append(text)

        help_attr = getattr(cls, "CONFIG_HELP", None)
        if isinstance(help_attr, str):
            _add(help_attr)
        elif isinstance(help_attr, (list, tuple)):
            for item in help_attr:
                _add(item)
        try:
            _add(cls({}).config_helper_text())
        except Exception:
            pass
        try:
            for field in cls.config_schema() or []:
                if not isinstance(field, dict):
                    continue
                hint = str(field.get("help") or "").strip()
                if not hint:
                    continue
                label = str(field.get("label") or field.get("key") or "").strip()
                if label and not hint.lower().startswith(label.lower()):
                    _add(f"{label}: {hint}")
                else:
                    _add(hint)
        except Exception:
            pass
        return lines

    def config_actions(self) -> List[Dict[str, Any]]:
        """Optional actions exposed in the config editor for this plugin."""

        return []

    def run_config_action(self, action_id: str, **_kwargs: Any) -> Dict[str, Any]:
        """Execute a plugin-owned config action from the config editor."""

        return {
            "ok": False,
            "message": f"Plugin '{self.name}' does not support config action '{action_id}'.",
        }

    def upload(self, file_path: str, **kwargs: Any) -> str:
        """Upload a file and return a URL or identifier."""
        raise NotImplementedError(f"Plugin '{self.name}' does not support upload")

    # -----------------------------------------------------------------------
    # Storage interface — mirrors PluginCore.backend_base.BackendBase.
    # Plugins that act as file repositories override these methods.
    # All raise NotImplementedError by default; override selectively.
    # -----------------------------------------------------------------------

    @property
    def is_remote(self) -> bool:
        """True if this plugin stores files on a remote service."""
        return False

    @property
    def prefer_defer_tags(self) -> bool:
        """True if tag writes should be deferred until after file ingest."""
        return False

    @property
    def supports_url_association(self) -> bool:
        """True when this provider supports associating URLs to files."""
        return False

    @property
    def supports_note_association(self) -> bool:
        """True when this provider supports per-file named notes."""
        return False

    @property
    def supports_relationship_association(self) -> bool:
        """True when this provider supports file relationship links (king/alt/related)."""
        return False

    def add_file(self, file_path: Path, **kwargs: Any) -> str:
        """Ingest a file and return its canonical hash."""
        raise NotImplementedError(f"Plugin '{self.name}' does not support add_file")

    def get_file(self, file_hash: str, **kwargs: Any) -> Optional[Path]:
        """Retrieve a stored file by hash, returning a local Path or None."""
        raise NotImplementedError(f"Plugin '{self.name}' does not support get_file")

    def get_metadata(self, file_hash: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Return metadata dict for a stored file."""
        raise NotImplementedError(f"Plugin '{self.name}' does not support get_metadata")

    def get_tag(self, file_identifier: str, **kwargs: Any) -> Tuple[List[str], str]:
        """Return (tags, hash) for a stored file identifier."""
        raise NotImplementedError(f"Plugin '{self.name}' does not support get_tag")

    def add_tag(self, file_identifier: str, tags: List[str], **kwargs: Any) -> bool:
        """Add tags to a stored file. Returns True on success."""
        raise NotImplementedError(f"Plugin '{self.name}' does not support add_tag")

    def delete_tag(self, file_identifier: str, tags: List[str], **kwargs: Any) -> bool:
        """Remove tags from a stored file. Returns True on success."""
        raise NotImplementedError(f"Plugin '{self.name}' does not support delete_tag")

    def get_url(self, file_identifier: str, **kwargs: Any) -> List[str]:
        """Return associated URLs for a stored file."""
        raise NotImplementedError(f"Plugin '{self.name}' does not support get_url")

    def add_url(self, file_identifier: str, urls: List[str], **kwargs: Any) -> bool:
        """Associate URLs with a stored file. Returns True on success."""
        raise NotImplementedError(f"Plugin '{self.name}' does not support add_url")

    def delete_url(self, file_identifier: str, urls: List[str], **kwargs: Any) -> bool:
        """Remove URL associations from a stored file. Returns True on success."""
        raise NotImplementedError(f"Plugin '{self.name}' does not support delete_url")

    def get_note(self, file_identifier: str, **kwargs: Any) -> Dict[str, str]:
        """Return notes dict (name -> text) for a stored file."""
        raise NotImplementedError(f"Plugin '{self.name}' does not support get_note")

    def set_note(self, file_identifier: str, name: str, text: str, **kwargs: Any) -> bool:
        """Write a named note on a stored file. Returns True on success."""
        raise NotImplementedError(f"Plugin '{self.name}' does not support set_note")

    def delete_note(self, file_identifier: str, name: str, **kwargs: Any) -> bool:
        """Delete a named note from a stored file. Returns True on success."""
        raise NotImplementedError(f"Plugin '{self.name}' does not support delete_note")

    @classmethod
    def missing_plugin_depends(cls) -> List[str]:
        from PluginCore.registry import import_plugin_module

        missing: List[str] = []
        for dep in getattr(cls, "PLUGIN_DEPENDS", ()) or ():
            name = str(dep or "").strip()
            if name and import_plugin_module(name) is None:
                missing.append(name)
        return missing

    def validate(self) -> bool:
        """Check if the plugin is available and properly configured."""
        return not self.missing_plugin_depends()

    def selector(
        self,
        selected_items: List[Any],
        *,
        ctx: Any,
        stage_is_last: bool = True,
        **_kwargs: Any
    ) -> bool:
        """Optional hook for handling `@N` selection semantics.

        The CLI can delegate selection behavior to a plugin/store instead of
        applying the default selection filtering.

        Return True if the selection was handled and default behavior should be skipped.
        """

        _ = selected_items
        _ = ctx
        _ = stage_is_last
        return False

    def show_selection_details(
        self,
        selected_items: List[Any],
        *,
        ctx: Any,
        stage_is_last: bool = True,
        source_command: str = "",
        table_type: str = "",
        table_metadata: Optional[Dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> bool:
        """Optionally render a terminal detail view for a selected plugin row."""

        _selected_item, payload, _metadata = self.resolve_selection_detail_subject(
            selected_items,
            stage_is_last=stage_is_last,
            source_command=source_command,
        )
        _ = table_type
        _ = table_metadata
        if not isinstance(payload, dict):
            return False

        detail_title = self.label
        item_title = str(payload.get("title") or "").strip()
        if item_title:
            detail_title = f"{self.label}: {item_title}"

        try:
            from SYS.rich_display import render_item_details_panel

            render_item_details_panel(payload, title=detail_title)
        except Exception:
            return False

        try:
            if hasattr(ctx, "set_last_result_items_only"):
                ctx.set_last_result_items_only([payload])
        except Exception:
            pass

        return True

    def resolve_selection_detail_subject(
        self,
        selected_items: List[Any],
        *,
        stage_is_last: bool = True,
        source_command: str = "",
        require_media_kind: Optional[str] = None,
    ) -> Tuple[Optional[Any], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Normalize a terminal `@N` selection into `(item, payload, metadata)`.

        Custom plugin detail hooks can use this to share the common preconditions
        for item panels instead of re-checking terminal/single-row/search-file
        state in each plugin.
        """

        if not stage_is_last or len(selected_items or []) != 1:
            return None, None, None

        normalized_source = str(source_command or "").replace("_", "-").strip().lower()
        if normalized_source != "search-file":
            return None, None, None

        item: Any = selected_items[0]
        payload: Optional[Dict[str, Any]]
        if isinstance(item, dict):
            payload = item
        else:
            payload = None
            to_dict = getattr(item, "to_dict", None)
            if callable(to_dict):
                try:
                    maybe = to_dict()
                except Exception:
                    maybe = None
                if isinstance(maybe, dict):
                    payload = maybe

        if not isinstance(payload, dict):
            return item, None, None

        meta: Dict[str, Any] = {}
        nested = payload.get("full_metadata") or payload.get("metadata")
        if isinstance(nested, dict):
            meta = nested

        if require_media_kind:
            media_kind = str(payload.get("media_kind") or meta.get("media_kind") or "").strip().lower()
            if media_kind != str(require_media_kind or "").strip().lower():
                return item, None, None

        return item, payload, meta

    @classmethod
    def selection_auto_stage(
        cls,
        table_type: str,
        stage_args: Optional[Sequence[str]] = None,
    ) -> Optional[List[str]]:
        """Return a stage to auto-run after selecting from `table_type`.

        This is used by the CLI to auto-insert default stages for plugin tables
        (e.g. select a YouTube row -> auto-run download-file).

        Plugins can implement this via class attributes (TABLE_AUTO_STAGES /
        TABLE_AUTO_PREFIXES) or by overriding this method.
        """
        t = str(table_type or "").strip().lower()
        if not t:
            return None

        stage: Optional[Sequence[str]] = None
        try:
            stage = cls.TABLE_AUTO_STAGES.get(t)
        except Exception:
            stage = None

        if stage is None:
            try:
                for prefix, cmd in (cls.TABLE_AUTO_PREFIXES or {}).items():
                    p = str(prefix or "").strip().lower()
                    if not p:
                        continue
                    if t == p or t.startswith(p + ".") or t.startswith(p):
                        stage = cmd
                        break
            except Exception:
                stage = None

        if not stage:
            return None

        out = [str(x) for x in stage if str(x or "").strip()]
        if not out:
            return None

        if cls.AUTO_STAGE_USE_SELECTION_ARGS and stage_args:
            try:
                out.extend([str(x) for x in stage_args if str(x or "").strip()])
            except Exception:
                pass
        return out

    @classmethod
    def url_patterns(cls) -> Tuple[str, ...]:
        """Return normalized URL patterns that this plugin handles."""
        patterns: List[str] = []
        maybe_urls = getattr(cls, "URL", None)
        if isinstance(maybe_urls, (list, tuple)):
            for entry in maybe_urls:
                try:
                    candidate = str(entry or "").strip().lower()
                except Exception:
                    continue
                if candidate:
                    patterns.append(candidate)
        maybe_domains = getattr(cls, "URL_DOMAINS", None)
        if isinstance(maybe_domains, (list, tuple)):
            for entry in maybe_domains:
                try:
                    candidate = str(entry or "").strip().lower()
                except Exception:
                    continue
                if candidate and candidate not in patterns:
                    patterns.append(candidate)
        return tuple(patterns)

    @classmethod
    def selection_url_prefixes(cls) -> Tuple[str, ...]:
        """Return URL-like prefixes that selection parsing should treat as URLs."""

        prefixes: List[str] = []
        seen: set[str] = set()
        for pattern in cls.url_patterns():
            try:
                candidate = str(pattern or "").strip().lower()
            except Exception:
                continue
            if not candidate:
                continue
            if "://" in candidate or candidate.endswith(":") or "🧲" in candidate:
                if candidate not in seen:
                    seen.add(candidate)
                    prefixes.append(candidate)
        return tuple(prefixes)


Provider = Plugin


