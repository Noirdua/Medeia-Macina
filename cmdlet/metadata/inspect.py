from __future__ import annotations

from typing import Any, Dict, Sequence


def run_inspect_action(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Route metadata inspect to get-metadata implementation."""
    from cmdlet.get_metadata import CMDLET as GET_METADATA_CMDLET

    exec_fn = getattr(GET_METADATA_CMDLET, "exec", None)
    if callable(exec_fn):
        return int(exec_fn(result, args, config))
    return int(GET_METADATA_CMDLET.run(result, args, config))
