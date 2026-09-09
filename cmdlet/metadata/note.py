from __future__ import annotations

from typing import Any, Dict, Sequence


def run_note_action(action: str, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Route metadata note actions to note cmdlets."""
    act = str(action or "").strip().lower()

    if act == "add":
        from cmdlet.metadata.note_add import CMDLET as ADD_NOTE_CMDLET

        return int(ADD_NOTE_CMDLET.run(result, args, config))

    if act == "delete":
        from cmdlet.delete_note import CMDLET as DELETE_NOTE_CMDLET

        return int(DELETE_NOTE_CMDLET.run(result, args, config))

    if act == "get":
        from cmdlet.metadata.get_note import CMDLET as GET_NOTE_CMDLET

        return int(GET_NOTE_CMDLET.run(result, args, config))

    return 1
