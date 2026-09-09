from __future__ import annotations

from typing import Any, Dict, List, Sequence

from SYS.cmdlet_spec import Cmdlet, CmdletArg
from cmdlet._shared import display_and_persist_items, should_show_help

CMDLET = Cmdlet(
    name=".jobs",
    summary="Background plugin jobs (torrents and other long-running work).",
    usage=".jobs [-plugin NAME] [-pause] [-resume] [-remove] [-id ID]",
    alias=["jobs"],
    arg=[
        CmdletArg("-plugin", type="string", required=False, description="Filter by plugin"),
        CmdletArg("-pause", type="flag", required=False, description="Pause selected or -id job"),
        CmdletArg("-resume", type="flag", required=False, description="Resume selected or -id job"),
        CmdletArg("-remove", type="flag", required=False, description="Remove/cancel selected or -id job"),
        CmdletArg("-id", type="string", required=False, description="Job id"),
    ],
    examples=[
        ".jobs",
        ".jobs -plugin torrent",
        "@1 | .jobs -pause",
        ".jobs -resume -id 1",
    ],
)


def _ids(result: Any, args: Sequence[str]) -> List[str]:
    from SYS.plugin_jobs import extract_job_ids

    return extract_job_ids(result, list(args or []))


def _plugin_filter(args: Sequence[str]) -> str:
    tokens = [str(t or "").strip() for t in (args or [])]
    for idx, tok in enumerate(tokens):
        if tok.lower() in {"-plugin", "--plugin"} and idx + 1 < len(tokens):
            return tokens[idx + 1]
    return ""


def _publish(plugin: str = "") -> int:
    from SYS import plugin_jobs
    from SYS.result_table import Table
    from SYS import pipeline as ctx
    from SYS.result_publication import publish_result_table

    try:
        from plugins.torrent.engine import get_engine

        get_engine()
    except Exception:
        pass
    rows = plugin_jobs.list_jobs(plugin or None)
    table = Table("Background jobs")
    table.set_table("plugin.jobs")
    for snap in rows:
        table.add_result(
            {
                **snap,
                "columns": [
                    ("Plugin", snap.get("plugin") or ""),
                    ("Title", snap.get("title") or ""),
                    ("Status", snap.get("status") or ""),
                    ("Progress", snap.get("progress") or ""),
                    ("Down", snap.get("down") or ""),
                    ("Up", snap.get("up") or ""),
                    ("Seeds", snap.get("seeds") or ""),
                    ("Leechers", snap.get("leechers") or ""),
                    ("Size", snap.get("size") or ""),
                ],
            }
        )
    if not rows:
        table.add_result(
            {
                "plugin": "",
                "title": "(none)",
                "status": "idle",
                "progress": "—",
                "down": "—",
                "peers": "—",
                "size": "—",
                "columns": [
                    ("Plugin", "—"),
                    ("Title", "(none)"),
                    ("Status", "idle"),
                    ("Progress", "—"),
                    ("Down", "—"),
                    ("Up", "—"),
                    ("Seeds", "—"),
                    ("Leechers", "—"),
                    ("Size", "—"),
                ],
            }
        )
    setattr(table, "_items_added", True)
    publish_result_table(ctx, table, rows, overlay=False)
    display_and_persist_items(
        rows or [{"title": "(none)", "status": "idle"}],
        title="Background jobs",
        subject=rows,
        display_type="custom",
        table=table,
    )
    return 0


def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    if should_show_help(args):
        return 0
    from SYS import plugin_jobs
    from SYS.logger import log
    import sys

    tokens = [str(t or "").strip().lower() for t in (args or [])]
    pause = "-pause" in tokens
    resume = "-resume" in tokens
    remove = "-remove" in tokens
    plugin = _plugin_filter(args)
    ids = _ids(result, args)
    if pause or resume or remove:
        if not ids:
            log(".jobs: no job id (use @N or -id)", file=sys.stderr)
            return 1
        ok = True
        for job_id in ids:
            if pause:
                ok = plugin_jobs.pause(job_id) and ok
            elif resume:
                ok = plugin_jobs.resume(job_id) and ok
            else:
                ok = plugin_jobs.cancel(job_id) and ok
        _publish(plugin)
        return 0 if ok else 1
    return _publish(plugin)


CMDLET.exec = _run
