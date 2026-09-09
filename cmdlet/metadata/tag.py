from __future__ import annotations

from typing import Any, Dict, Sequence


def run_tag_action(action: str, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Route metadata tag actions to the existing tag implementations."""
    act = str(action or "").strip().lower()

    if act == "add":
        from cmdlet.metadata.tag_add import CMDLET as ADD_TAG_CMDLET

        return int(ADD_TAG_CMDLET.run(result, args, config))

    if act == "delete":
        from cmdlet.metadata.tag_delete import _run as run_delete

        return run_delete(result, args, config)

    if act == "get":
        from cmdlet.metadata.tag_get import _run as run_get

        return run_get(result, args, config)

    return 1
