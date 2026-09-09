from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from SYS.logger import log


@dataclass
class CmdletArg:
    """Represents a single cmdlet argument with optional enum choices."""

    name: str
    type: str = "string"
    required: bool = False
    description: str = ""
    choices: List[str] = field(default_factory=list)
    alias: str = ""
    handler: Optional[Any] = None
    variadic: bool = False
    usage: str = ""
    requires_db: bool = False
    query_key: Optional[str] = None
    query_aliases: List[str] = field(default_factory=list)
    query_only: bool = False

    def resolve(self, value: Any) -> Any:
        if self.handler is not None and callable(self.handler):
            return self.handler(value)
        return value

    def to_flags(self) -> tuple[str, ...]:
        normalized_name = str(self.name or "").lstrip("-")
        if not normalized_name:
            return tuple()

        flags = [f"--{normalized_name}", f"-{normalized_name}"]
        if self.alias:
            flags.append(f"-{self.alias}")

        if self.type == "flag":
            flags.append(f"--no-{normalized_name}")
            flags.append(f"-no{normalized_name}")
            if self.alias:
                flags.append(f"-n{self.alias}")

        return tuple(flags)


def QueryArg(
    name: str,
    *,
    key: Optional[str] = None,
    aliases: Optional[Sequence[str]] = None,
    type: str = "string",
    required: bool = False,
    description: str = "",
    choices: Optional[Sequence[str]] = None,
    handler: Optional[Any] = None,
    query_only: bool = True,
) -> CmdletArg:
    """Create an argument that can be populated from `-query` fields."""

    return CmdletArg(
        name=str(name),
        type=str(type or "string"),
        required=bool(required),
        description=str(description or ""),
        choices=list(choices or []),
        handler=handler,
        query_key=str(key or name).strip().lower() if str(key or name).strip() else None,
        query_aliases=[str(a).strip().lower() for a in (aliases or []) if str(a).strip()],
        query_only=bool(query_only),
    )


def collect_registered_cmdlet_names(
    cmdlet_obj: Any,
    *,
    fallback_name: Optional[str] = None,
) -> List[str]:
    """Return normalized registration keys for a cmdlet object.

    Prefers the cmdlet object's own `_collect_names()` implementation when
    available, then normalizes names to the registry key form used by callers.
    """

    raw_names: List[Any] = []

    collector = getattr(cmdlet_obj, "_collect_names", None)
    if callable(collector):
        try:
            raw_names.extend(list(collector() or []))
        except Exception:
            pass

    if fallback_name:
        raw_names.append(fallback_name)

    if not raw_names:
        raw_name = getattr(cmdlet_obj, "name", None)
        if raw_name:
            raw_names.append(raw_name)
        for alias_attr in ("alias", "aliases"):
            alias_values = getattr(cmdlet_obj, alias_attr, None)
            if not alias_values:
                continue
            raw_names.extend(list(alias_values))

    seen: Set[str] = set()
    normalized_names: List[str] = []
    for raw_name in raw_names:
        key = str(raw_name or "").replace("_", "-").lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        normalized_names.append(key)
    return normalized_names


class SharedArgs:
    """Registry of shared CmdletArg definitions used across multiple cmdlet."""

    STORE = CmdletArg(
        name="store",
        type="enum",
        choices=[],
        description="Selects store",
        query_key="store",
    )

    URL = CmdletArg(
        name="url",
        type="string",
        description="http parser",
    )

    PLUGIN = CmdletArg(
        name="plugin",
        type="string",
        description="selects plugin",
    )

    INSTANCE = CmdletArg(
        name="instance",
        type="string",
        description='Plugin instance via -instance NAME or -query "instance:NAME"',
        query_key="instance",
        query_only=False,
    )

    @staticmethod
    def get_store_choices(config: Optional[Dict[str, Any]] = None, force: bool = False) -> List[str]:
        if not force and hasattr(SharedArgs, "_cached_available_stores"):
            return SharedArgs._cached_available_stores or []

        SharedArgs._refresh_store_choices_cache(config, skip_instantiation=False)
        return SharedArgs._cached_available_stores or []

    @staticmethod
    def _refresh_store_choices_cache(config: Optional[Dict[str, Any]] = None, skip_instantiation: bool = False) -> None:
        try:
            if config is None:
                try:
                    from SYS.config import load_config

                    config = load_config(emit_summary=False)
                except Exception:
                    SharedArgs._cached_available_stores = []
                    return

            SharedArgs._cached_available_stores = []
            if skip_instantiation:
                return

            try:
                from PluginCore.backend_registry import BackendRegistry

                registry = BackendRegistry(config=config, suppress_debug=True)
                available = registry.list_backends()
                if available:
                    SharedArgs._cached_available_stores = available
            except Exception:
                pass
        except Exception:
            SharedArgs._cached_available_stores = []

    LOCATION = CmdletArg(
        "location",
        type="enum",
        choices=["hydrus", "0x0"],
        required=True,
        description="Destination location",
    )

    DELETE = CmdletArg(
        "delete",
        type="flag",
        description="Delete the file after successful operation.",
    )

    ARTIST = CmdletArg(
        "artist",
        type="string",
        description="Filter by artist name (case-insensitive, partial match).",
    )

    ALBUM = CmdletArg(
        "album",
        type="string",
        description="Filter by album name (case-insensitive, partial match).",
    )

    TRACK = CmdletArg(
        "track",
        type="string",
        description="Filter by track title (case-insensitive, partial match).",
    )

    LIBRARY = CmdletArg(
        "library",
        type="string",
        choices=["hydrus", "local", "soulseek", "libgen", "ftp"],
        description="Search library or source location.",
    )

    TIMEOUT = CmdletArg(
        "timeout",
        type="integer",
        description="Search or operation timeout in seconds.",
    )

    LIMIT = QueryArg(
        "limit",
        type="integer",
        description='Max results via -query, e.g. -query "limit:30"',
        choices=["10", "30", "50", "100", "200"],
    )

    PATH = CmdletArg("path", type="string", description="File or directory path.")

    QUERY = CmdletArg(
        "query",
        type="string",
        description="Unified query string (e.g., hash:<sha256>, hash:{<h1>,<h2>}).",
    )

    REASON = CmdletArg(
        "reason",
        type="string",
        description="Reason or explanation for the operation.",
    )

    ARCHIVE = CmdletArg(
        "archive",
        type="flag",
        description="Archive the URL to Wayback Machine, Archive.today, and Archive.ph (requires URL argument in cmdlet).",
        alias="arch",
    )

    @staticmethod
    def resolve_storage(
        storage_value: Optional[str],
        default: Optional[Path] = None,
    ) -> Path:
        _ = storage_value
        if default is not None:
            return default
        return Path(tempfile.gettempdir())

    @classmethod
    def get(cls, name: str) -> Optional[CmdletArg]:
        try:
            return getattr(cls, name.upper())
        except AttributeError:
            return None


@dataclass
class Cmdlet:
    """Represents a cmdlet with metadata and arguments."""

    name: str
    summary: str
    usage: str
    alias: List[str] = field(default_factory=list)
    arg: List[CmdletArg] = field(default_factory=list)
    detail: List[str] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)
    exec: Optional[Callable[[Any, Sequence[str], Dict[str, Any]], int]] = field(default=None)

    def _collect_names(self) -> List[str]:
        names: List[str] = []
        if self.name:
            names.append(self.name)
        for alias in self.alias or []:
            if alias:
                names.append(alias)
        for alias in getattr(self, "aliases", None) or []:
            if alias:
                names.append(alias)

        seen: Set[str] = set()
        deduped: List[str] = []
        for name in names:
            key = name.replace("_", "-").lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(name)
        return deduped

    def register(self) -> "Cmdlet":
        if not callable(self.exec):
            return self
        try:
            from cmdlet import register_callable as _register_callable
        except Exception:
            return self

        names = self._collect_names()
        if not names:
            return self

        _register_callable(names, self.exec)
        return self

    def get_flags(self, arg_name: str) -> set[str]:
        return {f"-{arg_name}", f"--{arg_name}"}

    def build_flag_registry(self) -> Dict[str, set[str]]:
        registry: Dict[str, set[str]] = {}
        for arg in self.arg:
            try:
                registry[arg.name] = {str(flag).lower() for flag in arg.to_flags()}
            except Exception:
                registry[arg.name] = {flag.lower() for flag in self.get_flags(arg.name)}
        return registry


def parse_cmdlet_args(
    args: Sequence[str],
    cmdlet_spec: Dict[str, Any] | Cmdlet,
) -> Dict[str, Any]:
    """Parse command-line arguments based on cmdlet specification."""

    result: Dict[str, Any] = {}

    arg_specs_raw = getattr(cmdlet_spec, "arg", None)
    if arg_specs_raw is None or not isinstance(arg_specs_raw, (list, tuple)):
        raise TypeError(
            f"Expected cmdlet-like object with an 'arg' list, got {type(cmdlet_spec).__name__}"
        )

    arg_specs: List[Any] = list(arg_specs_raw)
    positional_args: List[CmdletArg] = []
    query_mapped_args: List[CmdletArg] = []

    arg_spec_map: Dict[str, str] = {}
    arg_spec_by_canonical: Dict[str, Any] = {}

    for spec in arg_specs:
        name = getattr(spec, "name", None)
        if not name:
            continue

        try:
            if getattr(spec, "query_key", None):
                query_mapped_args.append(spec)
        except Exception:
            pass

        name_str = str(name)
        canonical_name = name_str.lstrip("-")
        canonical_key = canonical_name.lower()

        try:
            if bool(getattr(spec, "query_only", False)):
                continue
        except Exception:
            pass

        arg_spec_by_canonical[canonical_key] = spec

        if "-" not in name_str:
            positional_args.append(spec)

        arg_spec_map[canonical_key] = canonical_name
        try:
            for flag in spec.to_flags():
                arg_spec_map[str(flag).lower()] = canonical_name
        except Exception:
            arg_spec_map[f"-{canonical_name}".lower()] = canonical_name
            arg_spec_map[f"--{canonical_name}".lower()] = canonical_name

    query_only_flag_map: Dict[str, str] = {}
    for spec in query_mapped_args:
        if not bool(getattr(spec, "query_only", False)):
            continue
        canonical = str(getattr(spec, "name", "") or "").lstrip("-")
        if not canonical:
            continue
        query_only_flag_map[f"-{canonical}".lower()] = canonical
        query_only_flag_map[f"--{canonical}".lower()] = canonical

    i = 0
    positional_index = 0

    while i < len(args):
        token = str(args[i])
        token_lower = token.lower()

        if token_lower in {"-hash", "--hash"} and token_lower not in arg_spec_map:
            try:
                log(
                    'Legacy flag -hash is no longer supported. Use: -query "hash:<sha256>"',
                    file=sys.stderr,
                )
            except Exception:
                pass
            i += 1
            continue

        if token_lower in arg_spec_map:
            canonical_name = arg_spec_map[token_lower]
            spec = arg_spec_by_canonical.get(canonical_name.lower())

            is_flag = bool(spec and str(getattr(spec, "type", "")).lower() == "flag")

            if is_flag:
                result[canonical_name] = True
                i += 1
            else:
                if i + 1 < len(args) and not str(args[i + 1]).startswith("-"):
                    value = args[i + 1]

                    is_variadic = bool(spec and getattr(spec, "variadic", False))
                    if is_variadic:
                        if canonical_name not in result:
                            result[canonical_name] = []
                        elif not isinstance(result[canonical_name], list):
                            result[canonical_name] = [result[canonical_name]]
                        result[canonical_name].append(value)
                    else:
                        result[canonical_name] = value
                    i += 2
                else:
                    i += 1
        elif token_lower in query_only_flag_map:
            canonical_name = query_only_flag_map[token_lower]
            if i + 1 < len(args) and not str(args[i + 1]).startswith("-"):
                result[canonical_name] = args[i + 1]
                i += 2
            else:
                i += 1
        elif positional_index < len(positional_args):
            positional_spec = positional_args[positional_index]
            canonical_name = str(getattr(positional_spec, "name", "")).lstrip("-")
            is_variadic = bool(getattr(positional_spec, "variadic", False))

            if is_variadic:
                if canonical_name not in result:
                    result[canonical_name] = []
                elif not isinstance(result[canonical_name], list):
                    result[canonical_name] = [result[canonical_name]]

                result[canonical_name].append(token)
                i += 1
            else:
                result[canonical_name] = token
                positional_index += 1
                i += 1
        else:
            i += 1

    try:
        raw_query = result.get("query")
    except Exception:
        raw_query = None

    if query_mapped_args and raw_query is not None:
        try:
            from SYS.cli_syntax import parse_query as _parse_query
            from PluginCore.base import parse_inline_query_arguments

            raw_query_text = str(raw_query)
            # Inline key:value tokens (supports comma-separated fields).
            _leftover, inline_fields = parse_inline_query_arguments(raw_query_text)
            norm_fields = {
                str(k).strip().lower(): v
                for k, v in dict(inline_fields or {}).items()
                if str(k or "").strip()
            }
            # Overlay quoted parse_query fields when values are clean.
            parsed_query = _parse_query(raw_query_text)
            fields = parsed_query.get("fields", {}) if isinstance(parsed_query, dict) else {}
            if isinstance(fields, dict):
                for key, value in fields.items():
                    value_text = str(value or "").strip()
                    if not value_text or "," in value_text:
                        continue
                    norm_fields.setdefault(str(key).strip().lower(), value_text)
        except Exception:
            norm_fields = {}

        for spec in query_mapped_args:
            canonical_name = str(getattr(spec, "name", "") or "").lstrip("-")
            if not canonical_name:
                continue
            if canonical_name in result and result.get(canonical_name) not in (None, ""):
                continue
            try:
                key = str(getattr(spec, "query_key", "") or "").strip().lower()
                aliases = getattr(spec, "query_aliases", None)
                alias_list = [str(a).strip().lower() for a in (aliases or []) if str(a).strip()]
            except Exception:
                key = ""
                alias_list = []
            candidates = [k for k in [key, canonical_name] + alias_list if k]
            val = None
            for k in candidates:
                if k in norm_fields:
                    val = norm_fields.get(k)
                    break
            if val is None:
                continue
            try:
                result[canonical_name] = spec.resolve(val)
            except Exception:
                result[canonical_name] = val

    return result
