from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

_lock = threading.RLock()
_seq = 0
_jobs: Dict[str, "PluginJob"] = {}
_order: List[str] = []


@dataclass
class PluginJob:
    job_id: str
    plugin: str
    title: str
    pause_fn: Optional[Callable[[], bool]] = None
    resume_fn: Optional[Callable[[], bool]] = None
    cancel_fn: Optional[Callable[[], bool]] = None
    snapshot_fn: Optional[Callable[[], Dict[str, Any]]] = None

    def snapshot(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.job_id,
            "plugin": self.plugin,
            "title": self.title,
            "table": "plugin.jobs",
            "_selection_action": [".jobs"],
            "_selection_args": ["-id", self.job_id],
        }
        if self.snapshot_fn is not None:
            try:
                extra = self.snapshot_fn() or {}
            except Exception:
                extra = {}
            if isinstance(extra, dict):
                data.update(extra)
                data["id"] = self.job_id
                data["plugin"] = self.plugin
        return data


def submit(
    plugin: str,
    title: str,
    *,
    job_id: Optional[str] = None,
    pause: Optional[Callable[[], bool]] = None,
    resume: Optional[Callable[[], bool]] = None,
    cancel: Optional[Callable[[], bool]] = None,
    snapshot: Optional[Callable[[], Dict[str, Any]]] = None,
) -> str:
    global _seq
    with _lock:
        if job_id:
            key = str(job_id).strip()
        else:
            _seq += 1
            key = str(_seq)
        job = PluginJob(
            job_id=key,
            plugin=str(plugin or "").strip() or "plugin",
            title=str(title or "").strip() or key,
            pause_fn=pause,
            resume_fn=resume,
            cancel_fn=cancel,
            snapshot_fn=snapshot,
        )
        if key not in _jobs:
            _order.append(key)
        _jobs[key] = job
        return key


def list_jobs(plugin: Optional[str] = None) -> List[Dict[str, Any]]:
    wanted = str(plugin or "").strip().lower()
    with _lock:
        jobs = [_jobs[i] for i in _order if i in _jobs]
    out: List[Dict[str, Any]] = []
    for job in jobs:
        if wanted and job.plugin.lower() != wanted:
            continue
        out.append(job.snapshot())
    return out


def get(job_id: str) -> Optional[PluginJob]:
    with _lock:
        return _jobs.get(str(job_id or "").strip())


def pause(job_id: str) -> bool:
    job = get(job_id)
    if job is None or job.pause_fn is None:
        return False
    try:
        return bool(job.pause_fn())
    except Exception:
        return False


def resume(job_id: str) -> bool:
    job = get(job_id)
    if job is None or job.resume_fn is None:
        return False
    try:
        return bool(job.resume_fn())
    except Exception:
        return False


def extract_job_ids(result: Any, args: Optional[List[str]] = None) -> List[str]:
    ids: List[str] = []
    tokens = [str(t or "").strip() for t in (args or [])]
    for idx, tok in enumerate(tokens):
        low = tok.lower()
        if low in {"-id", "--id"} and idx + 1 < len(tokens):
            nxt = tokens[idx + 1]
            if nxt and not nxt.startswith("-"):
                ids.append(nxt)
        elif low.startswith("-id="):
            ids.append(tok.split("=", 1)[1].strip())

    items = result if isinstance(result, list) else ([result] if result is not None else [])
    for item in items:
        if item is None:
            continue
        data = item
        if not isinstance(data, dict) and hasattr(data, "to_dict"):
            try:
                data = data.to_dict()
            except Exception:
                data = item
        extra = {}
        if not isinstance(data, dict):
            extra = getattr(item, "extra", None) or {}
            data = {
                "id": getattr(item, "id", None),
                "job_id": getattr(item, "job_id", None),
            }
        if isinstance(data, dict):
            extra = data.get("extra") if isinstance(data.get("extra"), dict) else extra
            for key in ("id", "job_id"):
                val = str(data.get(key) or "").strip()
                if val:
                    ids.append(val)
            if isinstance(extra, dict):
                for key in ("id", "job_id"):
                    val = str(extra.get(key) or "").strip()
                    if val:
                        ids.append(val)
            sel = data.get("_selection_args") if isinstance(data, dict) else None
            if not sel and isinstance(extra, dict):
                sel = extra.get("_selection_args")
            if isinstance(sel, (list, tuple)):
                sel_tokens = [str(t or "").strip() for t in sel]
                for idx, tok in enumerate(sel_tokens):
                    if tok.lower() in {"-id", "--id"} and idx + 1 < len(sel_tokens):
                        ids.append(sel_tokens[idx + 1])
    seen: set[str] = set()
    out: List[str] = []
    for job_id in ids:
        if job_id and job_id not in seen:
            seen.add(job_id)
            out.append(job_id)
    return out


def cancel(job_id: str) -> bool:
    job = get(job_id)
    if job is None:
        return False
    ok = True
    if job.cancel_fn is not None:
        try:
            ok = bool(job.cancel_fn())
        except Exception:
            ok = False
    with _lock:
        _jobs.pop(str(job_id), None)
        if str(job_id) in _order:
            _order.remove(str(job_id))
    return ok
