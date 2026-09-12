from __future__ import annotations

from typing import Any, Dict, Sequence, Optional
import sys

from SYS.detail_view_helpers import create_detail_view, prepare_detail_metadata
from SYS.logger import log
from PluginCore.registry import get_plugin
from SYS.result_table_helpers import add_row_columns
from SYS.selection_builder import build_hash_store_selection
from SYS.result_publication import publish_result_table

from SYS import pipeline as ctx
from .. import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
SharedArgs = sh.SharedArgs
normalize_hash = sh.normalize_hash
get_hash_for_operation = sh.get_hash_for_operation
should_show_help = sh.should_show_help
get_field = sh.get_field

CMDLET = Cmdlet(
    name="get-relationship",
    summary="Print relationships for the selected file (Hydrus).",
    usage='get-relationship [-query "hash:<sha256>"]',
    alias=[],
    arg=[
        SharedArgs.QUERY,
        SharedArgs.INSTANCE,
    ],
    detail=[
        "- Lists relationship data as returned by Hydrus.",
    ],
)


def _run(result: Any, _args: Sequence[str], config: Dict[str, Any]) -> int:
    # Help
    if should_show_help(_args):
        log(f"Cmdlet: {CMDLET.name}\nSummary: {CMDLET.summary}\nUsage: {CMDLET.usage}")
        return 0

    # Parse -query and -instance override
    override_query: str | None = None
    override_store: str | None = None
    args_list = list(_args)
    i = 0
    while i < len(args_list):
        a = args_list[i]
        low = str(a).lower()
        if low in {"-query", "--query", "query"} and i + 1 < len(args_list):
            override_query = str(args_list[i + 1]).strip()
            i += 2
            continue
        if low in {"-instance", "--instance"} and i + 1 < len(args_list):
            override_store = str(args_list[i + 1]).strip()
            i += 2
            continue
        i += 1

    override_hash, query_valid = sh.require_single_hash_query(
        override_query,
        'get-relationship requires -query "hash:<sha256>"',
        log_file=sys.stderr,
    )
    if not query_valid:
        return 1

    # Handle @N selection which creates a list
    if isinstance(result, list):
        if len(result) == 0:
            result = None
        elif len(result) > 1 and not override_hash:
            log(
                'get-relationship expects a single item; select one row (e.g. @1) or pass -query "hash:<sha256>"',
                file=sys.stderr,
            )
            return 1
        else:
            result = result[0]

    # Initialize results collection
    found_relationships = []  # List of dicts: {hash, type, title, path, store}
    source_title = "Unknown"

    # Store/hash-first subject resolution
    store_name: Optional[str] = override_store
    if not store_name:
        store_name = get_field(result, "store")

    hash_hex = (
        normalize_hash(override_hash)
        if override_hash else normalize_hash(get_hash_for_operation(None, result))
    )

    if not source_title or source_title == "Unknown":
        source_title = (
            get_field(result, "title") or get_field(result, "name")
            or (hash_hex[:16] + "..." if hash_hex else "Unknown")
        )

    if not hash_hex:
        log('get-relationship requires -query "hash:<sha256>"', file=sys.stderr)
        return 1

    # Fetch Hydrus relationships if we have a hash.
    hydrus_provider = get_plugin("hydrusnetwork", config)

    hash_hex = (
        normalize_hash(override_hash)
        if override_hash else normalize_hash(get_hash_for_operation(None,
                                                                    result))
    )

    if hash_hex:
        try:
            store_label = "hydrus"
            if store_name:
                store_label = str(store_name)
                if hydrus_provider is None:
                    log(
                        f"Hydrus client unavailable for store '{store_name}'",
                        file=sys.stderr
                    )
                    return 1
                relationships = hydrus_provider.get_relationships(hash_hex, store_name=store_name)
            else:
                relationships = hydrus_provider.get_relationships(hash_hex) if hydrus_provider is not None else None

            def _resolve_related_title(rel_hash: str) -> str:
                h = normalize_hash(rel_hash)
                if not h:
                    return str(rel_hash)
                if hydrus_provider is not None:
                    try:
                        resolved_title = hydrus_provider.get_title(
                            h,
                            store_name=store_label if store_name else None,
                        )
                        if isinstance(resolved_title, str) and resolved_title.strip():
                            return resolved_title.strip()
                    except Exception:
                        pass
                return h[:16] + "..."

            if relationships:
                file_rels = relationships.get("file_relationships",
                                        {})
                this_file_rels = file_rels.get(hash_hex)

                if this_file_rels:
                        # Map Hydrus relationship IDs to names.
                        # For /manage_file_relationships/get_file_relationships, the Hydrus docs define:
                        #   0=potential duplicates, 1=false positives, 3=alternates, 8=duplicates
                        # Additionally, this endpoint includes metadata keys like 'king'/'is_king'.
                        rel_map = {
                            "0": "potential",
                            "1": "false positive",
                            "3": "alternate",
                            "8": "duplicate",
                        }

                        for rel_type_id, rel_value in this_file_rels.items():
                            key = str(rel_type_id)

                            # Handle metadata keys explicitly.
                            if key in {"is_king",
                                       "king_is_on_file_domain",
                                       "king_is_local"}:
                                continue

                            # Some Hydrus responses provide a direct king hash under the 'king' key.
                            if key == "king":
                                king_hash = (
                                    normalize_hash(rel_value)
                                    if isinstance(rel_value,
                                                  str) else None
                                )
                                if king_hash and king_hash != hash_hex:
                                    if not any(str(r.get("hash",
                                                         "")).lower() == king_hash
                                               for r in found_relationships):
                                        found_relationships.append(
                                            {
                                                "hash": king_hash,
                                                "type": "king",
                                                "title":
                                                _resolve_related_title(king_hash),
                                                "path": None,
                                                "store": store_label,
                                            }
                                        )
                                continue

                            rel_name = rel_map.get(key, f"type-{key}")

                            # The relationship value is typically a list of hashes.
                            if isinstance(rel_value, list):
                                for rel_hash in rel_value:
                                    rel_hash_norm = (
                                        normalize_hash(rel_hash)
                                        if isinstance(rel_hash,
                                                      str) else None
                                    )
                                    if not rel_hash_norm or rel_hash_norm == hash_hex:
                                        continue
                                    if not any(str(r.get("hash",
                                                         "")).lower() == rel_hash_norm
                                               for r in found_relationships):
                                        found_relationships.append(
                                            {
                                                "hash":
                                                rel_hash_norm,
                                                "type":
                                                rel_name,
                                                "title":
                                                _resolve_related_title(rel_hash_norm),
                                                "path":
                                                None,
                                                "store":
                                                store_label,
                                            }
                                        )
                            # Defensive: sometimes the API may return a single hash string.
                            elif isinstance(rel_value, str):
                                rel_hash_norm = normalize_hash(rel_value)
                                if rel_hash_norm and rel_hash_norm != hash_hex:
                                    if not any(str(r.get("hash",
                                                         "")).lower() == rel_hash_norm
                                               for r in found_relationships):
                                        found_relationships.append(
                                            {
                                                "hash":
                                                rel_hash_norm,
                                                "type":
                                                rel_name,
                                                "title":
                                                _resolve_related_title(rel_hash_norm),
                                                "path":
                                                None,
                                                "store":
                                                store_label,
                                            }
                                        )
        except Exception as exc:
            # Only log error if we didn't find local relationships either
            if not found_relationships:
                log(f"Hydrus relationships fetch failed: {exc}", file=sys.stderr)

    # Display results
    metadata = prepare_detail_metadata(
        result,
        title=(source_title if source_title and source_title != "Unknown" else None),
        hash_value=hash_hex,
    )

    table = create_detail_view(
        "Relationships",
        metadata,
        init_command=("get-relationship", []),
        value_case=None,
        perseverance=False,
    )

    # Sort by type then title
    # Custom sort order: King first, then Derivative, then others
    def type_sort_key(item):
        t = item["type"].lower()
        if t == "king":
            return 0
        elif t == "derivative":
            return 1
        elif t in {"alternative",
                   "alternate",
                   "alt"}:
            return 2
        elif t == "duplicate":
            return 3
        else:
            return 4

    found_relationships.sort(key=lambda x: (type_sort_key(x), x["title"]))

    pipeline_results = []

    for i, item in enumerate(found_relationships):
        add_row_columns(
            table,
            [
                ("Type", item["type"].title()),
                ("Title", item["title"]),
                ("Store", item["store"]),
            ],
        )

        # Create result object for pipeline
        res_obj = {
            "title": item["title"],
            "hash": item["hash"],
            "file_hash": item["hash"],
            "relationship_type": item["type"],
            "store": item["store"],
        }
        # Target is always hash in store/hash-first mode
        res_obj["target"] = item["hash"]

        pipeline_results.append(res_obj)

        # Set selection args
        selection_args, _selection_action = build_hash_store_selection(
            item["hash"],
            item["store"],
        )
        if selection_args:
            table.set_row_selection_args(i, selection_args)

    # Ensure empty state is still navigable/visible
    publish_result_table(ctx, table, pipeline_results, overlay=True)
    from SYS.rich_display import stdout_console

    stdout_console().print(table)

    if not found_relationships:
        log("No relationships found.")

    return 0


CMDLET.exec = _run
CMDLET.register()
