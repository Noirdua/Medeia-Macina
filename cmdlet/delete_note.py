from __future__ import annotations

from typing import Any, Dict, Sequence
import sys

from SYS.logger import log

from SYS import pipeline as ctx
from . import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
SharedArgs = sh.SharedArgs
normalize_hash = sh.normalize_hash
parse_cmdlet_args = sh.parse_cmdlet_args
normalize_result_input = sh.normalize_result_input
get_field = sh.get_field
should_show_help = sh.should_show_help


class Delete_Note(Cmdlet):

    def __init__(self) -> None:
        super().__init__(
            name="delete-note",
            summary="Delete a named note from a file in an instance.",
            usage='delete-note -instance <instance> [-query "hash:<sha256>"] <name>',
            alias=["del-note"],
            arg=[
                SharedArgs.INSTANCE,
                SharedArgs.QUERY,
                CmdletArg(
                    "name",
                    type="string",
                    required=True,
                    description="The note name/key to delete."
                ),
            ],
            detail=[
                "- Deletes the named note from the selected store backend.",
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
            "[delete_note] Error: -query must be of the form hash:<sha256>",
            log_file=sys.stderr,
        )
        if not query_valid:
            return 1
        note_name_override = str(parsed.get("name") or "").strip()
        # Allow piping note rows from get-note: the selected item carries note_name.
        inferred_note_name = str(get_field(result, "note_name") or "").strip()
        if not note_name_override and not inferred_note_name:
            log(
                "[delete_note] Error: Requires <name> (or pipe a note row that provides note_name)",
                file=sys.stderr,
            )
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
                    '[delete_note] Error: Requires piped item(s) or -instance and -query "hash:<sha256>"',
                    file=sys.stderr,
                )
                return 1

        store_registry = None
        deleted = 0

        for res in results:
            if not isinstance(res, dict):
                ctx.emit(res)
                continue

            # Resolve which note name to delete for this item.
            note_name = (
                note_name_override or str(res.get("note_name") or "").strip()
                or inferred_note_name
            )
            if not note_name:
                log(
                    "[delete_note] Error: Missing note name (pass <name> or pipe a note row)",
                    file=sys.stderr,
                )
                return 1

            store_name, resolved_hash = sh.resolve_item_store_hash(
                res,
                override_store=str(store_override) if store_override else None,
                override_hash=str(query_hash) if query_hash else None,
                path_fields=("path",),
            )

            if not store_name:
                log(
                    "[delete_note] Error: Missing -instance and item has no store field",
                    file=sys.stderr,
                )
                return 1

            if not resolved_hash:
                ctx.emit(res)
                continue

            backend, store_registry, exc = sh.get_store_backend(
                config,
                store_name,
                store_registry=store_registry,
            )
            if backend is None:
                log(
                    f"[delete_note] Error: Unknown store '{store_name}': {exc}",
                    file=sys.stderr
                )
                return 1

            ok = False
            try:
                ok = bool(backend.delete_note(resolved_hash, note_name, config=config))
            except Exception as exc:
                log(
                    f"[delete_note] Error: Failed to delete note: {exc}",
                    file=sys.stderr
                )
                ok = False

            if ok:
                deleted += 1

            ctx.emit(res)

        log(f"[delete_note] Deleted note on {deleted} item(s)", file=sys.stderr)
        return 0 if deleted > 0 else 1


CMDLET = Delete_Note()
