from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


_REPL_STATE_FILENAME = "medeia-repl-state.json"


def repl_queue_dir(root: Path) -> Path:
    return Path(root) / "Log" / "repl_queue"


def repl_state_path(root: Path) -> Path:
    return Path(root) / "Log" / _REPL_STATE_FILENAME


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(path)
    return path


def touch_repl_state(
    root: Path,
    *,
    session_id: Optional[str] = None,
    pid: Optional[int] = None,
    status: str = "running",
) -> Path:
    payload: Dict[str, Any] = {
        "status": str(status or "running").strip() or "running",
        "updated_at": time.time(),
        "pid": int(pid) if pid is not None else int(os.getpid()),
    }
    if isinstance(session_id, str) and session_id.strip():
        payload["session_id"] = session_id.strip()
    return _write_json_atomic(repl_state_path(root), payload)


def read_repl_state(root: Path) -> Optional[Dict[str, Any]]:
    path = repl_state_path(root)
    try:
        if not path.exists() or not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def clear_repl_state(root: Path) -> None:
    path = repl_state_path(root)
    try:
        path.unlink()
    except Exception:
        return


def repl_state_is_alive(root: Path, *, max_age_seconds: float = 3.0) -> bool:
    payload = read_repl_state(root)
    if not isinstance(payload, dict):
        return False
    if str(payload.get("status") or "").strip().lower() != "running":
        return False
    try:
        updated_at = float(payload.get("updated_at") or 0.0)
    except Exception:
        return False
    if updated_at <= 0:
        return False
    try:
        return (time.time() - updated_at) <= max(0.0, float(max_age_seconds))
    except Exception:
        return False


def _legacy_repl_queue_glob(root: Path) -> list[Path]:
    log_dir = Path(root) / "Log"
    if not log_dir.exists():
        return []
    return list(log_dir.glob("medeia-repl-queue-*.json"))


def enqueue_repl_command(
    root: Path,
    command: str,
    *,
    source: str = "external",
    metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    queue_dir = repl_queue_dir(root)
    queue_dir.mkdir(parents=True, exist_ok=True)

    payload: Dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "command": str(command or "").strip(),
        "source": str(source or "external").strip() or "external",
        "created_at": time.time(),
    }
    if isinstance(metadata, dict) and metadata:
        payload["metadata"] = metadata

    stamp = int(time.time() * 1000)
    token = payload["id"][:8]
    final_path = queue_dir / f"{stamp:013d}-{token}.json"
    temp_path = final_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(final_path)
    return final_path


def pop_repl_commands(root: Path, *, limit: int = 8) -> List[Dict[str, Any]]:
    queue_dir = repl_queue_dir(root)
    legacy_entries = _legacy_repl_queue_glob(root)
    if not queue_dir.exists() and not legacy_entries:
        return []

    items: List[Dict[str, Any]] = []
    entries: List[Path] = []
    if queue_dir.exists():
        entries.extend(queue_dir.glob("*.json"))
    entries.extend(legacy_entries)

    def _sort_key(path: Path) -> tuple[float, str]:
        try:
            ts = float(path.stat().st_mtime)
        except Exception:
            ts = 0.0
        return (ts, path.name)

    for entry in sorted(entries, key=_sort_key)[: max(1, int(limit or 1))]:
        try:
            payload = json.loads(entry.read_text(encoding="utf-8"))
        except Exception:
            payload = {
                "id": entry.stem,
                "command": "",
                "source": "invalid",
                "created_at": entry.stat().st_mtime,
            }
        try:
            entry.unlink()
        except Exception:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return items