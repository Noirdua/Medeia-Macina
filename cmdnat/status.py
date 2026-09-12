from __future__ import annotations

from typing import Any, Dict, List

from SYS.cmdlet_spec import Cmdlet
from SYS import pipeline as ctx
from SYS.result_table import Table
from SYS.logger import set_debug
from cmdnat._status_shared import (
    add_startup_check as _add_startup_check,
    collect_plugin_startup_checks as _collect_plugin_startup_checks,
)

CMDLET = Cmdlet(
    name=".status",
    summary="Check and display service/plugin status",
    usage=".status",
    arg=[],
)

def _run(result: Any, args: List[str], config: Dict[str, Any]) -> int:
    startup_table = Table(
        "*********<IGNITIO>*********<NOUSEMPEH>*********<RUGRAPOG>*********<OMEGHAU>*********"
    )
    startup_table._interactive(True)._perseverance(True)
    startup_table.set_value_case("upper")

    debug_enabled = bool(config.get("debug", False))
    try:
        # Ensure global debug state follows config so HTTPClient and other helpers
        # emit debug-level information during the status check.
        set_debug(debug_enabled)
    except Exception:
        pass
    _add_startup_check(startup_table, "ENABLED" if debug_enabled else "DISABLED", "DEBUGGING")

    try:
        for check in _collect_plugin_startup_checks(config):
            _add_startup_check(
                startup_table,
                str(check.get("status") or "UNKNOWN"),
                str(check.get("name") or "Plugin"),
                plugin=str(check.get("plugin") or ""),
                instance=str(check.get("instance") or ""),
                files=check.get("files"),
                detail=str(check.get("detail") or ""),
            )

    except Exception as exc:
        _add_startup_check(startup_table, "ERROR", "STATUS", detail=str(exc))

    if startup_table.rows:
        # Mark as rendered to prevent CLI.py from auto-printing it to stdout
        setattr(startup_table, "_rendered_by_cmdlet", True)
        ctx.set_current_stage_table(startup_table)
    
    return 0

CMDLET.exec = _run
