from __future__ import annotations

from typing import Any, Dict, Sequence


def run_url_action(action: str, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Route metadata URL actions to URL cmdlets."""
    act = str(action or "").strip().lower()

    if act == "add":
        from cmdlet.metadata.url_add import CMDLET as ADD_URL_CMDLET

        return int(ADD_URL_CMDLET.run(result, args, config))

    if act == "delete":
        from cmdlet.delete_url import CMDLET as DELETE_URL_CMDLET

        return int(DELETE_URL_CMDLET.run(result, args, config))

    if act == "get":
        from cmdlet.get_url import CMDLET as GET_URL_CMDLET

        return int(GET_URL_CMDLET.run(result, args, config))

    return 1
