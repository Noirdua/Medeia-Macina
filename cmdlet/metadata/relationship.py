from __future__ import annotations

from typing import Any, Dict, Sequence


def run_relationship_action(action: str, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Route metadata relationship actions to relationship cmdlets."""
    act = str(action or "").strip().lower()

    if act == "add":
        from cmdlet.metadata.relationship_add import _run as run_add_relationship

        return int(run_add_relationship(result, args, config))

    if act == "delete":
        from cmdlet.delete_relationship import _run as run_delete_relationship

        return int(run_delete_relationship(list(args), config))

    if act == "get":
        from cmdlet.metadata.get_relationship import _run as run_get_relationship

        return int(run_get_relationship(result, args, config))

    return 1
