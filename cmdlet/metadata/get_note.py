from __future__ import annotations

from typing import Any, Dict, List, Sequence
import sys

from SYS.logger import log
from SYS.detail_view_helpers import create_detail_view, prepare_detail_metadata
from SYS.payload_builders import build_table_result_payload
from SYS.result_publication import publish_result_table
from SYS.result_table_helpers import add_row_columns

from SYS import pipeline as ctx
from .. import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
SharedArgs = sh.SharedArgs
normalize_hash = sh.normalize_hash
parse_cmdlet_args = sh.parse_cmdlet_args
normalize_result_input = sh.normalize_result_input
should_show_help = sh.should_show_help


class Get_Note(Cmdlet):

    def __init__(self) -> None:
        super().__init__(
            name="get-note",
            summary="List notes on a file in an instance.",
            usage='get-note -instance <instance> [-query "hash:<sha256>"]',
            alias=["get-notes",
                   "get_note"],
            arg=[
                SharedArgs.INSTANCE,
                SharedArgs.QUERY,
            ],
            detail=[
                "- Notes are retrieved via the selected instance.",
                "- Lyrics are stored in a note named 'lyric'.",
            ],
            exec=self.run,
        )
        try:
            SharedArgs.INSTANCE.choices = SharedArgs.get_instance_choices(None)
        except Exception:
            pass
        self.register()

    def run(self, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        if should_show_help(args):
            log(f"Cmdlet: {self.name}\nSummary: {self.summary}\nUsage: {self.usage}")
            return 0

        parsed = parse_cmdlet_args(args, self)
        store_override = parsed.get("instance")
        query_hash, query_valid = sh.require_single_hash_query(
            parsed.get("query"),
            "[get_note] Error: -query must be of the form hash:<sha256>",
            log_file=sys.stderr,
        )
        if not query_valid:
            return 1

        results = normalize_result_input(result)
        if not results:
            if store_override and query_hash:
                results = [{
                    "store": str(store_override),
                    "hash": query_hash
                }]
            else:
                log(
                    '[get_note] Error: Requires piped item(s) or -instance and -query "hash:<sha256>"',
                    file=sys.stderr,
                )
                return 1

        store_registry = None
        any_notes = False
        display_items: List[Dict[str, Any]] = []
        
        # We assume single subject for get-note detail view
        main_res = results[0]
        
        metadata = prepare_detail_metadata(main_res)

        note_table = create_detail_view(
            "Notes",
            metadata,
            table_name="note",
            source_command=("get-note", []),
        )

        for res in results:
            if not isinstance(res, dict):
                continue

            store_name, resolved_hash = sh.resolve_item_store_hash(
                res,
                override_store=str(store_override) if store_override else None,
                override_hash=str(query_hash) if query_hash else None,
                path_fields=("path",),
            )

            if not store_name:
                log(
                    "[get_note] Error: Missing -instance",
                    file=sys.stderr
                )
                return 1

            if not resolved_hash:
                continue
                
            # Update metadata if we resolved a hash that wasn't in source
            if resolved_hash and not metadata.get("Hash"):
                metadata["Hash"] = resolved_hash
            if store_name and not metadata.get("Store"):
                metadata["Store"] = store_name

            backend, store_registry, exc = sh.get_store_backend(
                config,
                store_name,
                store_registry=store_registry,
            )
            if backend is None:
                log(
                    f"[get_note] Error: Unknown store '{store_name}': {exc}",
                    file=sys.stderr
                )
                return 1

            notes = {}
            try:
                notes = backend.get_note(
                    resolved_hash,
                    config=config
                ) or {}
            except Exception:
                notes = {}

            if not notes:
                continue

            any_notes = True
            # Emit each note as its own row so CLI renders a proper note table
            for k in sorted(notes.keys(), key=lambda x: str(x).lower()):
                v = notes.get(k)
                raw_text = str(v or "")
                # Keep payload small for IPC/pipes.
                raw_text = raw_text[:999]
                preview = " ".join(raw_text.replace("\r", "").split("\n"))
                payload = build_table_result_payload(
                    columns=[
                        ("Name", str(k)),
                        ("Text", preview.strip()),
                    ],
                    store=store_name,
                    hash=resolved_hash,
                    note_name=str(k),
                    note_text=raw_text,
                )
                display_items.append(payload)
                if note_table is not None:
                    add_row_columns(
                        note_table,
                        [("Name", str(k)), ("Text", preview.strip())],
                    )
                    
                ctx.emit(payload)

        # Always set the table overlay even if empty to show item details
        publish_result_table(ctx, note_table, display_items, subject=result, overlay=True)
        
        if not any_notes:
            log("No notes found.")
            
        return 0


CMDLET = Get_Note()
