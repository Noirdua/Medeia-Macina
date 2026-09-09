"""Add file relationships in Hydrus based on relationship tags in sidecar."""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence
import re
from pathlib import Path
import sys

from SYS.logger import log
from SYS.item_accessors import get_sha256_hex, get_store_name
from PluginCore.registry import get_plugin
from SYS.metadata import _derive_sidecar_path as find_sidecar
from SYS.metadata import _read_sidecar_metadata as read_sidecar

from SYS import pipeline as ctx
from .. import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
SharedArgs = sh.SharedArgs
parse_cmdlet_args = sh.parse_cmdlet_args
normalize_result_input = sh.normalize_result_input
should_show_help = sh.should_show_help
get_field = sh.get_field

CMDLET = Cmdlet(
    name="add-relationship",
    summary=
    "Associate file relationships (king/alt/related) in Hydrus based on relationship tags in sidecar.",
    usage=
    "@1-3 | add-relationship -king @4    OR    add-relationship -path <file>    OR    @1,@2,@3 | add-relationship",
    arg=[
        CmdletArg(
            "path",
            type="string",
            description="Specify the local file path (if not piping a result).",
        ),
        SharedArgs.INSTANCE,
        SharedArgs.QUERY,
        CmdletArg(
            "-king",
            type="string",
            description=
            "Explicitly set the king hash/file for relationships (e.g., -king @4 or -king hash)",
        ),
        CmdletArg(
            "-alt",
            type="string",
            description=
            "Explicitly select alt item(s) by @ selection or hash list (e.g., -alt @3-5 or -alt <hash>,<hash>)",
        ),
        CmdletArg(
            "-type",
            type="string",
            description=
            "Relationship type for piped items (default: 'alt', options: 'king', 'alt', 'related')",
        ),
    ],
    detail=[
        "- Mode 1: Pipe multiple items, first becomes king, rest become alts (default)",
        "- Mode 2: Use -king to explicitly set which item/hash is the king: @1-3 | add-relationship -king @4",
        "- Mode 2b: Use -king and -alt to select both sides from the last table: add-relationship -king @1 -alt @3-5",
        "- Mode 3: Read relationships from sidecar tags:",
        "    - Format: 'relationship: <KING_HASH>,<ALT_HASH>,<ALT_HASH>' (first hash is king)",
        "- Supports three relationship types: king (primary), alt (alternative), related (other versions)",
        "- When using -king, all piped items become the specified relationship type to the king",
    ],
)


_normalize_hash_hex = sh.normalize_hash


def _extract_relationships_from_tag(tag_value: str) -> Dict[str, list[str]]:
    """Parse relationship tags.

    Format: relationship: <KING_HASH>,<ALT_HASH>,<ALT_HASH>
    Returns a dict like {"king": ["HASH1"], "alt": ["HASH2"], ...}
    """
    result: Dict[str,
                 list[str]] = {}
    if not isinstance(tag_value, str):
        return result

    # Extract hashes; first is king
    hashes = re.findall(r"\b[a-fA-F0-9]{64}\b", tag_value)
    hashes = [h.strip().lower() for h in hashes if isinstance(h, str)]
    if not hashes:
        return result
    king = _normalize_hash_hex(hashes[0])
    if not king:
        return result
    result["king"] = [king]
    alts: list[str] = []
    for h in hashes[1:]:
        normalized = _normalize_hash_hex(h)
        if normalized and normalized != king:
            alts.append(normalized)
    if alts:
        result["alt"] = alts
    return result


def _apply_relationships_from_tags(
    relationship_tags: Sequence[str],
    *,
    hydrus_client: Any,
    use_local_storage: bool,
    local_storage_path: Optional[Path],
    config: Dict[str,
                 Any],
) -> int:
    """Persist relationship tags into Hydrus or local DB.

    Local DB semantics:
    - Treat the first hash (king) as the king.
    - Store directional alt -> king relationships (no reverse edge).
    """
    rel_tags = [
        t for t in relationship_tags
        if isinstance(t, str) and t.strip().lower().startswith("relationship:")
    ]
    if not rel_tags:
        return 0

    # Prefer Hydrus if available (hash-based relationships map naturally).
    if hydrus_client is not None and hasattr(hydrus_client, "set_relationship"):
        processed: set[tuple[str, str, str]] = set()
        for tag in rel_tags:
            rels = _extract_relationships_from_tag(tag)
            king = (rels.get("king") or [None])[0]
            if not king:
                continue
            king_norm = _normalize_hash_hex(king)
            if not king_norm:
                continue

            for rel_type in ("alt", "related"):
                for other in rels.get(rel_type, []) or []:
                    other_norm = _normalize_hash_hex(other)
                    if not other_norm or other_norm == king_norm:
                        continue
                    key = (other_norm, king_norm, rel_type)
                    if key in processed:
                        continue
                    try:
                        hydrus_client.set_relationship(other_norm, king_norm, rel_type)
                        processed.add(key)
                    except Exception:
                        pass
        return 0

    # Relationship persistence currently requires a Hydrus-backed store; local/folder
    # stores have no relationship database to write to.
    if use_local_storage and local_storage_path is not None:
        log(
            "Relationships require a Hydrus-backed store; local storage does not support them",
            file=sys.stderr,
        )
        return 1

    return 0

    return 0


def _parse_at_selection(token: str) -> Optional[list[int]]:
    """Parse standard @ selection syntax into a list of 0-based indices.

    Supports: @2, @2-5, @{1,3,5}, @3,5,7, @3-6,8, @*
    """
    if not isinstance(token, str):
        return None
    t = token.strip()
    if not t.startswith("@"):
        return None
    if t == "@*":
        return []  # special sentinel: caller interprets as "all"

    selector = t[1:].strip()
    if not selector:
        return None
    if selector.startswith("{") and selector.endswith("}"):
        selector = selector[1:-1].strip()

    parts = [p.strip() for p in selector.split(",") if p.strip()]
    if not parts:
        return None

    indices_1based: set[int] = set()
    for part in parts:
        try:
            if "-" in part:
                start_s, end_s = part.split("-", 1)
                start = int(start_s.strip())
                end = int(end_s.strip())
                if start <= 0 or end <= 0 or start > end:
                    return None
                for i in range(start, end + 1):
                    indices_1based.add(i)
            else:
                num = int(part)
                if num <= 0:
                    return None
                indices_1based.add(num)
        except Exception:
            return None

    return sorted(i - 1 for i in indices_1based)


def _resolve_items_from_at(token: str) -> Optional[list[Any]]:
    """Resolve @ selection token into actual items from the current result context."""
    items = ctx.get_last_result_items()
    if not items:
        return None
    parsed = _parse_at_selection(token)
    if parsed is None:
        return None
    if token.strip() == "@*":
        return list(items)
    selected: list[Any] = []
    for idx in parsed:
        if 0 <= idx < len(items):
            selected.append(items[idx])
    return selected


def _extract_hash_and_store(item: Any) -> tuple[Optional[str], Optional[str]]:
    """Extract (hash_hex, store) from a result item (dict/object)."""
    try:
        return (
            get_sha256_hex(item, "hash_hex", "hash", "file_hash"),
            get_store_name(item, "store"),
        )
    except Exception:
        return None, None


def _hydrus_hash_exists(hydrus_client: Any, hash_hex: str) -> bool:
    """Best-effort check whether a hash exists in the connected Hydrus backend."""
    try:
        if hydrus_client is None or not hasattr(hydrus_client, "fetch_file_metadata"):
            return False
        payload = hydrus_client.fetch_file_metadata(
            hashes=[hash_hex],
            include_service_keys_to_tags=False,
            include_file_url=False,
            include_duration=False,
            include_size=False,
            include_mime=False,
            include_notes=False,
        )
        meta = payload.get("metadata") if isinstance(payload, dict) else None
        return bool(isinstance(meta, list) and meta)
    except Exception:
        return False


def _resolve_king_reference(king_arg: str) -> Optional[str]:
    """Resolve a king reference like '@4' to its actual hash.

    Store/hash mode intentionally avoids file-path dependency.
    """
    if not king_arg:
        return None

    # Check if it's already a valid hash
    normalized = _normalize_hash_hex(king_arg)
    if normalized:
        return normalized

    # Try to resolve as @ selection from pipeline context
    if king_arg.startswith("@"):
        selected = _resolve_items_from_at(king_arg)
        if not selected:
            log(f"Cannot resolve {king_arg}: no selection context", file=sys.stderr)
            return None
        if len(selected) != 1:
            log(
                f"{king_arg} selects {len(selected)} items; -king requires exactly 1",
                file=sys.stderr,
            )
            return None

        item = selected[0]
        item_hash = (
            get_field(item,
                      "hash_hex") or get_field(item,
                                               "hash") or get_field(item,
                                                                    "file_hash")
        )

        if item_hash:
            normalized = _normalize_hash_hex(str(item_hash))
            if normalized:
                return normalized

        log(f"Item {king_arg} has no hash information", file=sys.stderr)
        return None

    return None


def _refresh_relationship_view_if_current(
    target_hash: Optional[str],
    target_path: Optional[str],
    other: Optional[str],
    config: Dict[str,
                 Any],
) -> None:
    """If the current subject matches the target, refresh relationships via get-relationship."""
    try:
        from cmdlet import get as get_cmdlet  # type: ignore
    except Exception:
        return

    get_relationship = None
    try:
        get_relationship = get_cmdlet("get-relationship")
    except Exception:
        get_relationship = None
    if not callable(get_relationship):
        return

    try:
        subject = ctx.get_last_result_subject()
        if subject is None:
            return

        def norm(val: Any) -> str:
            return str(val).lower()

        target_hashes = [norm(v) for v in [target_hash, other] if v]
        target_paths = [norm(v) for v in [target_path, other] if v]

        subj_hashes: list[str] = []
        subj_paths: list[str] = []
        if isinstance(subject, dict):
            subj_hashes = [
                norm(v) for v in [
                    subject.get("hydrus_hash"),
                    subject.get("hash"),
                    subject.get("hash_hex"),
                    subject.get("file_hash"), ] if v
            ]
            subj_paths = [
                norm(v) for v in
                [subject.get("file_path"), subject.get("path"), subject.get("target")]
                if v
            ]
        else:
            subj_hashes = [
                norm(getattr(subject,
                             f,
                             None))
                for f in ("hydrus_hash", "hash", "hash_hex", "file_hash")
                if getattr(subject, f, None)
            ]
            subj_paths = [
                norm(getattr(subject,
                             f,
                             None)) for f in ("file_path", "path", "target")
                if getattr(subject, f, None)
            ]

        is_match = False
        if target_hashes and any(h in subj_hashes for h in target_hashes):
            is_match = True
        if target_paths and any(p in subj_paths for p in target_paths):
            is_match = True
        if not is_match:
            return

        refresh_args: list[str] = []
        if target_hash:
            refresh_args.extend(["-query", f"hash:{target_hash}"])
        get_relationship(subject, refresh_args, config)
    except Exception:
        pass


def _run(result: Any, _args: Sequence[str], config: Dict[str, Any]) -> int:
    """Associate file relationships in Hydrus.

    Two modes of operation:
    1. Read from sidecar: Looks for relationship tags in the file's sidecar (format: "relationship: hash(king)<HASH>,hash(alt)<HASH>")
    2. Pipeline mode: When piping multiple results, the first becomes "king" and subsequent items become "alt"

    Returns 0 on success, non-zero on failure.
    """
    # Help
    if should_show_help(_args):
        log(f"Cmdlet: {CMDLET.name}\nSummary: {CMDLET.summary}\nUsage: {CMDLET.usage}")
        return 0

    # Parse arguments using CMDLET spec
    parsed = parse_cmdlet_args(_args, CMDLET)
    arg_path: Optional[Path] = None
    override_store = parsed.get("instance")
    override_hashes, query_valid = sh.require_hash_query(
        parsed.get("query"),
        "Invalid -query value (expected hash:<sha256>)",
        log_file=sys.stderr,
    )
    if not query_valid:
        return 1
    king_arg = parsed.get("king")
    alt_arg = parsed.get("alt")
    rel_type = parsed.get("type", "alt")

    raw_path = parsed.get("path")
    if raw_path:
        try:
            arg_path = Path(str(raw_path)).expanduser()
        except Exception:
            arg_path = Path(str(raw_path))

    # Handle @N selection which creates a list
    # Use normalize_result_input to handle both single items and lists
    items_to_process = normalize_result_input(result)

    # Allow selecting alt items directly from the last table via -alt @...
    # This enables: add-relationship -king @1 -alt @3-5
    if alt_arg:
        alt_text = str(alt_arg).strip()
        resolved_alt_items: list[Any] = []
        if alt_text.startswith("@"):
            selected = _resolve_items_from_at(alt_text)
            if not selected:
                log(
                    f"Failed to resolve -alt {alt_text}: no selection context",
                    file=sys.stderr
                )
                return 1
            resolved_alt_items = selected
        else:
            # Treat as comma/semicolon-separated list of hashes
            parts = [
                p.strip() for p in alt_text.replace(";", ",").split(",") if p.strip()
            ]
            hashes = [h for h in (_normalize_hash_hex(p) for p in parts) if h]
            if not hashes:
                log(
                    "Invalid -alt value (expected @ selection or 64-hex sha256 hash list)",
                    file=sys.stderr,
                )
                return 1
            if not override_store:
                log(
                    "-instance is required when using -alt with a raw hash list",
                    file=sys.stderr
                )
                return 1
            resolved_alt_items = [
                {
                    "hash": h,
                    "store": str(override_store)
                } for h in hashes
            ]
        items_to_process = normalize_result_input(resolved_alt_items)

    # Allow explicit store/hash-first operation via -query "hash:<sha256>" (supports multiple hash: tokens)
    if (not items_to_process) and override_hashes:
        if not override_store:
            log(
                "-instance is required when using -query without piped items",
                file=sys.stderr
            )
            return 1
        items_to_process = [
            {
                "hash": h,
                "store": str(override_store)
            } for h in override_hashes
        ]

    if not items_to_process and not arg_path:
        log(
            "No items provided to add-relationship (no piped result and no -path)",
            file=sys.stderr
        )
        return 1

    # If no items from pipeline, just process the -path arg
    if not items_to_process and arg_path:
        items_to_process = [{
            "file_path": arg_path
        }]

    # Resolve the king reference once (if provided)
    king_hash: Optional[str] = None
    king_store: Optional[str] = None
    if king_arg:
        king_text = str(king_arg).strip()
        if king_text.startswith("@"):
            selected = _resolve_items_from_at(king_text)
            if not selected:
                log(
                    f"Cannot resolve {king_text}: no selection context",
                    file=sys.stderr
                )
                return 1
            if len(selected) != 1:
                log(
                    f"{king_text} selects {len(selected)} items; -king requires exactly 1",
                    file=sys.stderr,
                )
                return 1
            king_hash, king_store = _extract_hash_and_store(selected[0])
            if not king_hash:
                log(f"Item {king_text} has no hash information", file=sys.stderr)
                return 1
        else:
            king_hash = _resolve_king_reference(king_text)
            if not king_hash:
                log(f"Failed to resolve king argument: {king_text}", file=sys.stderr)
                return 1

    # Decide target instance: override_store > (king store + piped item stores) (must be consistent)
    store_name: Optional[str] = str(override_store).strip() if override_store else None
    if not store_name:
        stores = set()
        if king_store:
            stores.add(str(king_store))
        for item in items_to_process:
            s = get_field(item, "store")
            if s:
                stores.add(str(s))
        if len(stores) == 1:
            store_name = next(iter(stores))
        elif len(stores) > 1:
            log(
                "Multiple stores detected (king/alt across stores); use -instance and ensure all selections are from the same store",
                file=sys.stderr,
            )
            return 1

    # Enforce same-instance relationships when store context is available.
    if king_store and store_name and str(king_store) != str(store_name):
        log(
            f"Cross-instance relationship blocked: king is in store '{king_store}' but -instance is '{store_name}'",
            file=sys.stderr,
        )
        return 1
    if store_name:
        for item in items_to_process:
            s = get_field(item, "store")
            if s and str(s) != str(store_name):
                log(
                    f"Cross-instance relationship blocked: alt item store '{s}' != '{store_name}'",
                    file=sys.stderr,
                )
                return 1

    # Resolve backend for store/hash operations
    backend = None
    is_folder_store = False
    store_root: Optional[Path] = None
    if store_name:
        backend, _store_registry, _exc = sh.get_store_backend(config, str(store_name))
        if backend is not None:
            if not bool(getattr(backend, "supports_relationship_association", False)):
                log(
                    f"Store '{store_name}' does not support relationships",
                    file=sys.stderr,
                )
                return 1
            loc = getattr(backend, "location", None)
            if callable(loc):
                is_folder_store = True
                store_root = Path(str(loc()))
        else:
            backend = None
            is_folder_store = False
            store_root = None

    # Select Hydrus client:
    # - If a store is specified and maps to a HydrusNetwork backend, use that backend's client.
    # - If no store is specified, use the default Hydrus client.
    # NOTE: When a store is specified, we do not fall back to a global/default Hydrus client.
    hydrus_client = None
    hydrus_provider = get_plugin("hydrusnetwork", config)
    if store_name and (not is_folder_store) and backend is not None:
        try:
            if hydrus_provider is not None:
                hydrus_client = hydrus_provider.get_client(
                    store_name=str(store_name),
                    allow_default=False,
                )
        except Exception:
            hydrus_client = None
    elif not store_name:
        try:
            if hydrus_provider is not None:
                hydrus_client = hydrus_provider.get_client()
        except Exception:
            hydrus_client = None

    # Use the selected store root when available; otherwise use the configured
    # local plugin root for sidecar/tag import lookup.
    from SYS.config import get_local_storage_path

    local_storage_root: Optional[Path] = None
    if store_root is not None:
        local_storage_root = store_root
    else:
        try:
            p = get_local_storage_path(config) if config else None
            local_storage_root = Path(p) if p else None
        except Exception:
            local_storage_root = None

    use_local_storage = local_storage_root is not None

    if king_hash:
        log(f"Using king hash: {king_hash}", file=sys.stderr)

    # If -path is provided, try reading relationship tags from its sidecar and persisting them.
    if arg_path is not None and arg_path.exists() and arg_path.is_file():
        try:
            sidecar_path = find_sidecar(arg_path)
            if sidecar_path is not None and sidecar_path.exists():
                _, tags, _ = read_sidecar(sidecar_path)
                relationship_tags = [
                    t for t in (tags or [])
                    if isinstance(t, str) and t.lower().startswith("relationship:")
                ]
                if relationship_tags:
                    code = _apply_relationships_from_tags(
                        relationship_tags,
                        hydrus_client=hydrus_client,
                        use_local_storage=use_local_storage,
                        local_storage_path=local_storage_root,
                        config=config,
                    )
                    return 0 if code == 0 else 1
        except Exception:
            pass

    # If piped items include relationship tags, persist them (one pass) then exit.
    try:
        rel_tags_from_pipe: list[str] = []
        for item in items_to_process:
            tags_val = None
            if isinstance(item, dict):
                tags_val = item.get("tag") or item.get("tags")
            else:
                tags_val = getattr(item, "tag", None)
            if isinstance(tags_val, list):
                rel_tags_from_pipe.extend(
                    [
                        t for t in tags_val
                        if isinstance(t, str) and t.lower().startswith("relationship:")
                    ]
                )
            elif isinstance(tags_val,
                            str) and tags_val.lower().startswith("relationship:"):
                rel_tags_from_pipe.append(tags_val)

        if rel_tags_from_pipe:
            code = _apply_relationships_from_tags(
                rel_tags_from_pipe,
                hydrus_client=hydrus_client,
                use_local_storage=use_local_storage,
                local_storage_path=local_storage_root,
                config=config,
            )
            return 0 if code == 0 else 1
    except Exception:
        pass

    # STORE/HASH MODE (preferred): use -instance and hashes; do not require file paths.
    if store_name and is_folder_store and store_root is not None:
        log(
            f"Relationships are not supported for local/folder store '{store_name}'",
            file=sys.stderr,
        )
        return 1

    if store_name and (not is_folder_store):
        # Hydrus store/hash mode
        if hydrus_client is None:
            log("Hydrus client unavailable for this store", file=sys.stderr)
            return 1

        # Verify hashes exist in this Hydrus backend to prevent cross-instance edges.
        if king_hash and (not _hydrus_hash_exists(hydrus_client, king_hash)):
            log(
                f"Cross-instance relationship blocked: king hash not found in store '{store_name}'",
                file=sys.stderr,
            )
            return 1

        # Mode 1: first is king
        if not king_hash:
            first_hash = None
            for item in items_to_process:
                h, item_store = _extract_hash_and_store(item)
                if item_store and store_name and str(item_store) != str(store_name):
                    log(
                        f"Cross-instance relationship blocked: item store '{item_store}' != '{store_name}'",
                        file=sys.stderr,
                    )
                    return 1
                if not h:
                    continue
                if not first_hash:
                    first_hash = h
                    if not _hydrus_hash_exists(hydrus_client, first_hash):
                        log(
                            f"Cross-instance relationship blocked: hash not found in store '{store_name}'",
                            file=sys.stderr,
                        )
                        return 1
                    continue
                if h != first_hash:
                    if not _hydrus_hash_exists(hydrus_client, h):
                        log(
                            f"Cross-instance relationship blocked: hash not found in store '{store_name}'",
                            file=sys.stderr,
                        )
                        return 1
                    hydrus_client.set_relationship(h, first_hash, str(rel_type))
            return 0

        # Mode 2: explicit king
        for item in items_to_process:
            h, item_store = _extract_hash_and_store(item)
            if item_store and store_name and str(item_store) != str(store_name):
                log(
                    f"Cross-instance relationship blocked: item store '{item_store}' != '{store_name}'",
                    file=sys.stderr,
                )
                return 1
            if not h or h == king_hash:
                continue
            if not _hydrus_hash_exists(hydrus_client, h):
                log(
                    f"Cross-instance relationship blocked: hash not found in store '{store_name}'",
                    file=sys.stderr,
                )
                return 1
            hydrus_client.set_relationship(h, king_hash, str(rel_type))
        return 0

    # Process each item in the list (path-based mode)
    for item in items_to_process:
        # Extract hash and path from current item
        file_hash = None
        file_path_from_result = None

        if isinstance(item, dict):
            file_hash = item.get("hash_hex") or item.get("hash")
            file_path_from_result = item.get("file_path") or item.get(
                "path"
            ) or item.get("target")
        else:
            file_hash = getattr(item, "hash_hex", None) or getattr(item, "hash", None)
            file_path_from_result = getattr(item,
                                            "file_path",
                                            None) or getattr(item,
                                                             "path",
                                                             None)

        # Handle relationships for local-file results using the configured
        # local plugin root when available.
        local_storage_path = get_local_storage_path(config) if config else None
        use_local_storage = bool(local_storage_path)

        if use_local_storage and file_path_from_result and not file_hash:
            log(
                "Relationships require a Hydrus-backed store; local storage does not support them",
                file=sys.stderr,
            )
            return 1

        # PIPELINE MODE with Hydrus: Track relationships using hash
        if file_hash and hydrus_client:
            file_hash = _normalize_hash_hex(
                str(file_hash) if file_hash is not None else None
            )
            if not file_hash:
                log("Invalid file hash format", file=sys.stderr)
                return 1

            # If explicit -king provided, use it
            if king_hash:
                try:
                    hydrus_client.set_relationship(file_hash, king_hash, rel_type)
                    log(
                        f"[add-relationship] Set {rel_type} relationship: {file_hash} <-> {king_hash}",
                        file=sys.stderr,
                    )
                    _refresh_relationship_view_if_current(
                        file_hash,
                        str(file_path_from_result)
                        if file_path_from_result is not None else None,
                        king_hash,
                        config,
                    )
                except Exception as exc:
                    log(f"Failed to set relationship: {exc}", file=sys.stderr)
                    return 1
            else:
                # Original behavior: no explicit king, first becomes king, rest become alts
                try:
                    existing_king = ctx.load_value("relationship_king")
                except Exception:
                    existing_king = None

                # If this is the first item, make it the king
                if not existing_king:
                    try:
                        ctx.store_value("relationship_king", file_hash)
                        log(f"Established king hash: {file_hash}", file=sys.stderr)
                        continue  # Move to next item
                    except Exception:
                        pass

                # If we already have a king and this is a different hash, link them
                if existing_king and existing_king != file_hash:
                    try:
                        hydrus_client.set_relationship(
                            file_hash,
                            existing_king,
                            rel_type
                        )
                        log(
                            f"[add-relationship] Set {rel_type} relationship: {file_hash} <-> {existing_king}",
                            file=sys.stderr,
                        )
                        _refresh_relationship_view_if_current(
                            file_hash,
                            (
                                str(file_path_from_result)
                                if file_path_from_result is not None else None
                            ),
                            existing_king,
                            config,
                        )
                    except Exception as exc:
                        log(f"Failed to set relationship: {exc}", file=sys.stderr)
                        return 1

        # If we get here, we didn't have a usable local path and Hydrus isn't available/usable.

    return 0


# Register cmdlet (no legacy decorator)
CMDLET.exec = _run
CMDLET.alias = ["add-rel"]
CMDLET.register()
