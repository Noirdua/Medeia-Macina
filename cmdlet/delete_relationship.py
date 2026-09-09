"""Delete file relationships (Currently Disabled)."""

import sys
from typing import Any, Dict, List, Optional

from SYS import pipeline as ctx
from . import _shared as sh

normalize_hash = sh.normalize_hash
get_field = sh.get_field


def _extract_hash(item: Any) -> Optional[str]:
    h = get_field(item,
                  "hash_hex") or get_field(item,
                                           "hash") or get_field(item,
                                                                "file_hash")
    return normalize_hash(str(h)) if h else None


def _run(args: List[str], config: Dict[str, Any]) -> int:
    try:
        parsed, _, _ = sh.parse_cmdlet_args(args)
        if sh.should_show_help(parsed):
            return 0

        # Relationship deletion is currently not implemented for Hydrus.
        # Legacy Folder storage has been removed.
        sh.log("Relationship deletion is currently only supported via Folder storage, which has been removed.", file=sys.stderr)
        sh.log("Hydrus relationship management via Medios-Macina is planned for a future update.", file=sys.stderr)
        return 1

    except Exception as exc:
        sh.log(f"Error in delete-relationship: {exc}", file=sys.stderr)
        return 1


CMDLET = sh.Cmdlet(
    name="delete-relationship",
    summary="Remove relationships from files (Currently Disabled).",
    usage="@1 | delete-relationship --all",
    arg=[
        sh.SharedArgs.PATH,
        sh.SharedArgs.INSTANCE,
        sh.SharedArgs.QUERY,
        sh.CmdletArg(
            "all",
            type="flag",
            description="Delete all relationships for the file(s)."
        ),
        sh.CmdletArg(
            "type",
            type="string",
            description="Delete specific relationship type.",
        ),
    ],
)

CMDLET.exec = _run
CMDLET.register()
