"""Data models for the pipeline."""

from __future__ import annotations

import datetime
import hashlib
import inspect
import json
import os
import shutil
import sys
import time
import logging
from threading import RLock

logger = logging.getLogger(__name__)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, TextIO, TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console, ConsoleOptions, Group
    from rich.live import Live
    from rich.progress import Progress, TaskID

# rich imports are deferred to avoid ~100ms startup cost.
# Classes in this module that use rich types (ProgressBar, PipelineLiveProgress)
# import them lazily inside their method bodies at first use.


@dataclass(slots=True)
class PipeObject:
    """Unified pipeline object for tracking files, metadata, tag values, and relationships through the pipeline.

    This is the single source of truth for all result data in the pipeline. Uses the hash+store
    canonical pattern for file identification.

    Attributes:
        hash: SHA-256 hash of the file (canonical identifier)
        store: Storage backend name (e.g., 'default', 'hydrus', 'test', 'home')
        tag: List of extracted or assigned tag values
        title: Human-readable title if applicable
        source_url: URL where the object came from
        duration: Duration in seconds if applicable
        metadata: Full metadata dictionary from source
        warnings: Any warnings or issues encountered
        path: Path to the file if this object represents a file
        relationships: Relationship data (king/alt/related hashes)
        is_temp: If True, this is a temporary/intermediate artifact that may be cleaned up
        action: The cmdlet that created this object (format: 'cmdlet:cmdlet_name')
        parent_hash: Hash of the parent file in the pipeline chain (for tracking provenance/lineage)
        extra: Additional fields not covered above
    """

    hash: str
    store: str
    plugin: Optional[str] = None
    tag: List[str] = field(default_factory=list)
    title: Optional[str] = None
    url: Optional[str] = None
    source_url: Optional[str] = None
    duration: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    path: Optional[str] = None
    relationships: Dict[str, Any] = field(default_factory=dict)
    is_temp: bool = False
    action: Optional[str] = None
    parent_hash: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def add_relationship(self, rel_type: str, rel_hash: str) -> None:
        """Add a relationship hash.

        Args:
            rel_type: Relationship type ('king', 'alt', 'related')
            rel_hash: Hash to add to the relationship
        """
        if rel_type not in self.relationships:
            self.relationships[rel_type] = []

        if isinstance(self.relationships[rel_type], list):
            if rel_hash not in self.relationships[rel_type]:
                self.relationships[rel_type].append(rel_hash)
        else:
            # Single value (e.g., king), convert to that value
            self.relationships[rel_type] = rel_hash

    def get_relationships(self) -> Dict[str, Any]:
        """Get all relationships for this object."""
        return self.relationships.copy() if self.relationships else {}

    def debug_table(self) -> None:
        """Rich-inspect the PipeObject when debug logging is enabled."""
        try:
            from SYS.logger import is_debug_enabled, debug_inspect
        except Exception:
            return

        if not is_debug_enabled():
            return

        # Prefer a stable, human-friendly title:
        #   "1 - download-file", "2 - download-file", ...
        # The index is preserved when possible via `pipe_index` in the PipeObject's extra.
        idx = None
        try:
            if isinstance(self.extra, dict):
                idx = self.extra.get("pipe_index")
        except Exception:
            idx = None

        cmdlet_name = "PipeObject"
        try:
            from SYS import pipeline as ctx

            current = (
                ctx.get_current_cmdlet_name("")
                if hasattr(ctx,
                           "get_current_cmdlet_name") else ""
            )
            if current:
                cmdlet_name = current
            else:
                action = str(self.action or "").strip()
                if action.lower().startswith("cmdlet:"):
                    cmdlet_name = action.split(":", 1)[1].strip() or cmdlet_name
                elif action:
                    cmdlet_name = action
        except Exception:
            cmdlet_name = "PipeObject"

        title_text = cmdlet_name
        try:
            if idx is not None and str(idx).strip():
                title_text = f"{idx} - {cmdlet_name}"
        except Exception:
            title_text = cmdlet_name

        # Color the title (requested: yellow instead of Rich's default blue-ish title).
        # We disable value=False to avoid duplicating the object repr which is redundant with the attribute listing
        debug_inspect(self, title=f"[yellow]{title_text}[/yellow]", value=False)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary, excluding None and empty values."""
        data: Dict[str,
                   Any] = {
                       "hash": self.hash,
                       "store": self.store,
                   }

        if self.plugin:
            data["plugin"] = self.plugin

        if self.tag:
            data["tag"] = self.tag
        if self.title:
            data["title"] = self.title
        if self.url:
            data["url"] = self.url
        if self.source_url:
            data["source_url"] = self.source_url
        if self.duration is not None:
            data["duration"] = self.duration
        if self.metadata:
            data["metadata"] = self.metadata
        if self.warnings:
            data["warnings"] = self.warnings
        if self.path:
            data["path"] = self.path
        if self.relationships:
            data["relationships"] = self.relationships
        if self.is_temp:
            data["is_temp"] = self.is_temp
        if self.action:
            data["action"] = self.action
        if self.parent_hash:
            data["parent_hash"] = self.parent_hash

        # Add extra fields
        data.update({
            k: v
            for k, v in self.extra.items() if v is not None
        })
        return data


class FileRelationshipTracker:
    """Track relationships between files for sidecar creation.

    Allows tagging files with their relationships to other files:
    - king: The primary/master version of a file
    - alt: Alternate versions of the same content
    - related: Related files (e.g., screenshots of a book)
    """

    def __init__(self) -> None:
        self.relationships: Dict[str,
                                 Dict[str,
                                      Any]] = {}

    def register_king(self, file_path: str, file_hash: str) -> None:
        """Register a file as the king (primary) version."""
        if file_path not in self.relationships:
            self.relationships[file_path] = {}
        self.relationships[file_path]["king"] = file_hash

    def add_alt(self, file_path: str, alt_hash: str) -> None:
        """Add an alternate version of a file."""
        if file_path not in self.relationships:
            self.relationships[file_path] = {}
        if "alt" not in self.relationships[file_path]:
            self.relationships[file_path]["alt"] = []
        if alt_hash not in self.relationships[file_path]["alt"]:
            self.relationships[file_path]["alt"].append(alt_hash)

    def add_related(self, file_path: str, related_hash: str) -> None:
        """Add a related file."""
        if file_path not in self.relationships:
            self.relationships[file_path] = {}
        if "related" not in self.relationships[file_path]:
            self.relationships[file_path]["related"] = []
        if related_hash not in self.relationships[file_path]["related"]:
            self.relationships[file_path]["related"].append(related_hash)

    def get_relationships(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Get relationships for a file."""
        return self.relationships.get(file_path)

    def link_files(self, primary_path: str, king_hash: str, *alt_paths: str) -> None:
        """Link files together with primary as king and others as alternates.

        Args:
            primary_path: Path to the primary file (will be marked as 'king')
            king_hash: Hash of the primary file
            alt_paths: Paths to alternate versions (will be marked as 'alt')
        """
        self.register_king(primary_path, king_hash)
        for alt_path in alt_paths:
            try:
                alt_hash = _get_file_hash(alt_path)
                self.add_alt(primary_path, alt_hash)
            except Exception as e:
                import sys

                print(f"Error hashing {alt_path}: {e}", file=sys.stderr)


def _get_file_hash(filepath: str) -> str:
    """Calculate SHA256 hash of a file."""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


# ============= Download Module Classes =============


class DownloadError(RuntimeError):
    """Raised when the download or Hydrus import fails."""


@dataclass(slots=True)
class DownloadOptions:
    """Configuration for downloading media.

    Use the add-file cmdlet separately for Hydrus import.
    """

    url: str
    mode: str  # "audio" or "video"
    output_dir: Path
    cookies_path: Optional[Path] = None
    ytdl_format: Optional[str] = None
    extra_tags: Optional[List[str]] = None
    debug_log: Optional[Path] = None
    native_progress: bool = False
    clip_sections: Optional[str] = None
    playlist_items: Optional[
        str] = None  # yt-dlp --playlist-items format (e.g., "1-3,5,8")
    no_playlist: bool = False  # If True, pass --no-playlist to yt-dlp
    quiet: bool = False  # If True, suppress all console output (progress, debug logs)
    embed_chapters: bool = False  # If True, pass yt-dlp --embed-chapters / embedchapters
    write_sub: bool = False  # If True, download subtitles (writesubtitles/writeautomaticsub)


class SendFunc(Protocol):
    """Protocol for event sender function."""

    def __call__(self, event: str, **payload: Any) -> None:
        ...


@dataclass(slots=True)
class DownloadMediaResult:
    """Result of a successful media download."""

    path: Path
    info: Dict[str, Any]
    tag: List[str]
    source_url: Optional[str]
    hash_value: Optional[str] = None
    paths: Optional[List[Path]] = None  # For multiple files (e.g., section downloads)


@dataclass(slots=True)
class DebugLogger:
    """Logs events to a JSON debug file for troubleshooting downloads."""

    path: Path
    file: Optional[TextIO] = None
    session_started: bool = False

    def ensure_open(self) -> None:
        """Open the debug log file if not already open."""
        if self.file is not None:
            return
        try:
            parent = self.path.parent
            if parent and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
            self.file = self.path.open("a", encoding="utf-8")
        except OSError as exc:  # pragma: no cover - surfaces to stderr
            print(f"Failed to open debug log {self.path}: {exc}", file=sys.stderr)
            self.file = None
            return
        self._write_session_header()

    def _write_session_header(self) -> None:
        """Write session start marker to log."""
        if self.session_started:
            return
        self.session_started = True
        self.write_record("session-start",
                          {
                              "pid": os.getpid(),
                              "exe": sys.executable
                          })

    def write_raw(self, text: str) -> None:
        """Write raw text to debug log."""
        self.ensure_open()
        if self.file is None:
            return
        self.file.write(text + "\n")
        self.file.flush()

    def write_record(
        self,
        event: str,
        payload: Optional[Dict[str,
                               Any]] = None
    ) -> None:
        """Write a structured event record to debug log."""
        record = {
            "timestamp": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "event": event,
            "payload": payload,
        }
        self.write_raw(json.dumps(_sanitise_for_json(record), ensure_ascii=False))

    def close(self) -> None:
        """Close the debug log file."""
        if self.file is None:
            return
        try:
            self.file.close()
        finally:
            self.file = None


def _sanitise_for_json(
    value: Any,
    *,
    max_depth: int = 8,
    _seen: Optional[set[int]] = None
) -> Any:
    """Best-effort conversion to JSON-serialisable types without raising on cycles."""
    import math
    from dataclasses import asdict, is_dataclass

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return repr(value)
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        try:
            return value.decode()
        except Exception:
            return value.hex()

    if max_depth <= 0:
        return repr(value)

    if _seen is None:
        _seen = set()

    obj_id = id(value)
    if obj_id in _seen:
        return "<circular>"

    _seen.add(obj_id)
    try:
        if isinstance(value, dict):
            return {
                str(key): _sanitise_for_json(val,
                                             max_depth=max_depth - 1,
                                             _seen=_seen)
                for key, val in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            iterable = value if not isinstance(value, set) else list(value)
            return [
                _sanitise_for_json(item,
                                   max_depth=max_depth - 1,
                                   _seen=_seen) for item in iterable
            ]
        if is_dataclass(value) and not isinstance(value, type):
            return _sanitise_for_json(
                asdict(value),
                max_depth=max_depth - 1,
                _seen=_seen
            )
    finally:
        _seen.discard(obj_id)

    return repr(value)


def _import_rich() -> Any:
    """Lazy-load rich types used by ProgressBar and PipelineLiveProgress."""
    import rich.console as _rc
    import rich.live as _rl
    import rich.panel as _rp
    import rich.progress as _rprog
    # Return a namespace-like object with the types we need
    class _Rich:
        Console = _rc.Console
        ConsoleOptions = _rc.ConsoleOptions
        Group = _rc.Group
        Live = _rl.Live
        Panel = _rp.Panel
        Progress = _rprog.Progress
        BarColumn = _rprog.BarColumn
        DownloadColumn = _rprog.DownloadColumn
        SpinnerColumn = _rprog.SpinnerColumn
        TaskID = _rprog.TaskID
        TaskProgressColumn = _rprog.TaskProgressColumn
        TextColumn = _rprog.TextColumn
        TimeRemainingColumn = _rprog.TimeRemainingColumn
        TimeElapsedColumn = _rprog.TimeElapsedColumn
        TransferSpeedColumn = _rprog.TransferSpeedColumn
    return _Rich


_rich: Any = None  # cached after first call


def _r() -> Any:
    global _rich
    if _rich is None:
        _rich = _import_rich()
    return _rich


class ProgressBar:
    """Rich progress helper for byte-based transfers.

    Opinionated: requires `rich` and always renders via Rich.
    """

    def __init__(self, width: Optional[int] = None):
        """Initialize progress bar with optional custom width."""
        if width is None:
            width = shutil.get_terminal_size((80, 20))[0]
        self.width = max(40, width)  # Minimum 40 chars for readability

        self._console: Optional[Console] = None
        self._progress: Optional[Progress] = None
        self._task_id: Optional[TaskID] = None

        # Optional: when a PipelineLiveProgress is active, prefer rendering
        # transfers inside it instead of creating a nested Rich Progress.
        self._pipeline_ui: Any = None
        self._pipeline_label: Optional[str] = None

    def _ensure_started(
        self,
        *,
        label: str,
        total: Optional[int],
        file: Any = None
    ) -> None:
        if self._pipeline_ui is not None and self._pipeline_label:
            # Pipeline-backed transfer task is already registered; update its total if needed.
            try:
                if total is not None and total > 0:
                    self._pipeline_ui.update_transfer(
                        label=self._pipeline_label,
                        completed=None,
                        total=int(total)
                    )
            except Exception:
                logger.exception("Failed to update pipeline UI transfer in ProgressBar._ensure_started")
            return

        if self._progress is not None and self._task_id is not None:
            if total is not None and total > 0:
                self._progress.update(self._task_id, total=int(total))
            return

        # Prefer integrating with the pipeline Live UI to avoid nested Rich Live instances.
        try:
            from SYS import pipeline as pipeline_context

            ui = pipeline_context.get_live_progress()
            if ui is not None and hasattr(ui,
                                          "begin_transfer") and hasattr(
                                              ui,
                                              "update_transfer"):
                self._pipeline_ui = ui
                self._pipeline_label = str(label or "download")
                try:
                    ui.begin_transfer(
                        label=self._pipeline_label,
                        total=int(total) if isinstance(total,
                                                       int) and total > 0 else None,
                    )
                except Exception:
                    # If pipeline integration fails, fall back to standalone progress.
                    self._pipeline_ui = None
                    self._pipeline_label = None
                else:
                    return
        except Exception:
            logger.exception("Failed to initialize pipeline Live UI integration in ProgressBar._ensure_started")
        
        stream = file if file is not None else sys.stderr
        # Use shared stderr console when rendering to stderr (cooperates with PipelineLiveProgress).
        if stream is sys.stderr:
            try:
                from SYS.rich_display import stderr_console

                console = stderr_console()
            except Exception:
                logger.exception("Failed to acquire shared stderr Console from SYS.rich_display; using fallback Console")
                console = _r().Console(file=stream)
        else:
            console = _r().Console(file=stream)
        progress = _r().Progress(
            _r().TextColumn("[progress.description]{task.description}"),
            _r().BarColumn(),
            _r().TaskProgressColumn(),
            _r().DownloadColumn(),
            _r().TransferSpeedColumn(),
            _r().TimeRemainingColumn(),
            console=console,
            transient=True,
        )
        progress.start()

        task_total = int(total) if isinstance(total, int) and total > 0 else None
        task_id: TaskID = progress.add_task(str(label or "download"), total=task_total)

        self._console = console
        self._progress = progress
        self._task_id = task_id

    def update(
        self,
        *,
        downloaded: Optional[int],
        total: Optional[int],
        label: str = "download",
        file: Any = None,
    ) -> None:
        if downloaded is None and total is None:
            return
        self._ensure_started(label=label, total=total, file=file)
        if self._pipeline_ui is not None and self._pipeline_label:
            try:
                self._pipeline_ui.update_transfer(
                    label=self._pipeline_label,
                    completed=int(downloaded or 0) if downloaded is not None else None,
                    total=int(total) if isinstance(total,
                                                   int) and total > 0 else None,
                )
            except Exception:
                logger.exception("Failed to update pipeline UI transfer in ProgressBar.update")
            return

        if self._progress is None or self._task_id is None:
            return
        if total is not None and total > 0:
            self._progress.update(
                self._task_id,
                completed=int(downloaded or 0),
                total=int(total),
                refresh=True
            )
        else:
            self._progress.update(
                self._task_id,
                completed=int(downloaded or 0),
                refresh=True
            )

    def finish(self) -> None:
        if self._pipeline_ui is not None and self._pipeline_label:
            try:
                self._pipeline_ui.finish_transfer(label=self._pipeline_label)
            except Exception:
                logger.exception("Failed to finish pipeline UI transfer in ProgressBar.finish")
            finally:
                self._pipeline_ui = None
                self._pipeline_label = None
            return
        if self._progress is None:
            return
        try:
            self._progress.stop()
        finally:
            self._console = None
            self._progress = None
            self._task_id = None

    def format_bytes(self, bytes_val: Optional[float]) -> str:
        """Format bytes to human-readable size.

        Args:
            bytes_val: Number of bytes or None.

        Returns:
            Formatted string (e.g., "123.4 MB", "1.2 GB").
        """
        if bytes_val is None or bytes_val <= 0:
            return "?.? B"

        for unit in ("B", "KB", "MB", "GB", "TB"):
            if bytes_val < 1024:
                return f"{bytes_val:.1f} {unit}"
            bytes_val /= 1024

        return f"{bytes_val:.1f} PB"

    # NOTE: rich.Progress handles the visual formatting; format_bytes remains as a general utility.


class ProgressFileReader:
    """File-like wrapper that prints a ProgressBar as bytes are read.

    Intended for uploads: pass this wrapper as the file object to httpx/requests.
    Progress is written to stderr (so pipelines remain clean).
    """

    def __init__(
        self,
        fileobj: Any,
        *,
        total_bytes: Optional[int],
        label: str = "upload",
        min_interval_s: float = 0.25,
    ):
        self._f = fileobj
        if total_bytes is None:
            self._total = 0
        else:
            try:
                self._total = int(total_bytes)
            except Exception:
                self._total = 0
        self._label = str(label or "upload")
        self._min_interval_s = max(0.05, float(min_interval_s))
        self._bar = ProgressBar()
        self._start = time.time()
        self._last = self._start
        self._read = 0
        self._done = False

    def _render(self) -> None:
        if self._done:
            return
        if self._total <= 0:
            return
        now = time.time()
        if now - self._last < self._min_interval_s:
            return
        self._bar.update(
            downloaded=int(self._read),
            total=int(self._total),
            label=str(self._label or "upload"),
            file=sys.stderr,
        )
        self._last = now

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        self._bar.finish()

    def read(self, size: int = -1) -> Any:
        chunk = self._f.read(size)
        try:
            if chunk:
                self._read += len(chunk)
                self._render()
            else:
                # EOF
                self._finish()
        except Exception:
            logger.exception("Error while reading and updating ProgressFileReader")
        return chunk

    def seek(self, offset: int, whence: int = 0) -> Any:
        out = self._f.seek(offset, whence)
        try:
            pos = int(self._f.tell())
            if pos <= 0:
                self._read = 0
                self._start = time.time()
                self._last = self._start
            else:
                self._read = pos
        except Exception:
            logger.exception("Failed to determine file position in ProgressFileReader.seek")
        return out

    def tell(self) -> Any:
        return self._f.tell()

    def close(self) -> None:
        try:
            self._finish()
        except Exception:
            logger.exception("Failed to finish ProgressFileReader progress in close")
        return self._f.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._f, name)


# ============================================================================
# PIPELINE EXECUTION CONTEXT
# Consolidated from pipeline_context.py
# ============================================================================
# Note: Pipeline functions and state variables moved to pipeline.py


def _pipeline_progress_item_label(value: Any, *, max_len: int = 72) -> str:

    def _clip(text: str) -> str:
        text = str(text or "").strip()
        if not text:
            return "(item)"
        if len(text) <= max_len:
            return text
        return text[:max(0, max_len - 1)] + "…"

    try:
        if isinstance(value, PipeObject):
            if value.title:
                return _clip(value.title)
            if value.url:
                return _clip(value.url)
            if value.source_url:
                return _clip(value.source_url)
            if value.path:
                return _clip(value.path)
            if value.hash:
                return _clip(value.hash)
        if isinstance(value, dict):
            for key in ("title", "url", "source_url", "path", "hash", "target"):
                raw = value.get(key)
                if raw is not None and str(raw).strip():
                    return _clip(str(raw))
        return _clip(str(value))
    except Exception:
        return "(item)"


class PipelineLiveProgress:
    """Multi-level pipeline progress UI.

    - Each pipeline step (pipe) is a persistent bar.
    - Each per-item operation is shown as a transient sub-task (spinner).

    Designed to render to stderr so pipelines remain clean.
    """

    def __init__(self, pipe_labels: List[str], *, enabled: bool = True) -> None:
        self._enabled = bool(enabled)
        self._stopped = False
        self._pipe_labels = [str(x) for x in (pipe_labels or [])]
        self._lock = RLock()
        self._event_callback: Any = None

        self._console: Optional[Console] = None
        self._live: Optional[Live] = None

        self._overall: Optional[Progress] = None
        self._pipe_progress: Optional[Progress] = None
        self._subtasks: Optional[Progress] = None
        self._status: Optional[Progress] = None
        self._transfers: Optional[Progress] = None

        self._overall_task: Optional[TaskID] = None
        self._pipe_tasks: List[TaskID] = []

        self._transfer_tasks: Dict[str,
                                   TaskID] = {}

        # Per-pipe status line shown below the pipe bars.
        self._status_tasks: Dict[int,
                                 TaskID] = {}

        # When a pipe is operating on a single item, allow percent-based progress
        # updates on the pipe bar (0..100) so it doesn't sit at 0% until emit().
        self._pipe_percent_mode: Dict[int,
                                      bool] = {}

        # Per-pipe step counters used for status lines and percent mapping.
        self._pipe_step_total: Dict[int,
                                    int] = {}
        self._pipe_step_done: Dict[int,
                                   int] = {}

        # Per-pipe state
        self._pipe_totals: List[int] = [0 for _ in self._pipe_labels]
        self._pipe_done: List[int] = [0 for _ in self._pipe_labels]
        self._subtask_ids: List[List[TaskID]] = [[] for _ in self._pipe_labels]
        self._subtask_active_index: List[int] = [0 for _ in self._pipe_labels]

        # Title line state (active per-item context)
        self._active_subtask_text: Optional[str] = None

    def set_event_callback(self, callback: Any) -> None:
        with self._lock:
            self._event_callback = callback

    def _emit_event(self, event: str, **payload: Any) -> None:
        callback = self._event_callback
        if not callable(callback):
            return
        data: Dict[str, Any] = {
            "phase": "progress",
            "event": str(event or "").strip(),
        }
        data.update(payload)
        try:
            callback(data)
        except Exception:
            logger.exception("Failed to emit PipelineLiveProgress event '%s'", event)

    def _title_text(self) -> str:
        """Compute the Pipeline panel title.

        The title remains stable ("Pipeline"). Per-item step detail is rendered
        using a dedicated progress bar within the panel.
        """

        return "Pipeline"

    def set_active_subtask_text(self, text: Optional[str]) -> None:
        """Update the Pipeline panel title to reflect the current in-item step.

        This is intentionally lightweight: it does not affect pipe counters.
        Cmdlets may call this to surface step-level progress for long-running
        single-item work (e.g. Playwright page load -> capture -> convert).
        """
        if not self._enabled:
            return
        try:
            value = str(text or "").strip()
        except Exception:
            logger.exception("Failed to compute active subtask text in PipelineLiveProgress.set_active_subtask_text")
            value = ""
        self._active_subtask_text = value or None

    def __rich_console__(self, console: "Console", options: "ConsoleOptions"):
        """Renderable hook used by Rich Live.

        Using a dynamic renderable keeps the panel title up to date and animates
        the spinner without needing manual Live.update() calls.
        """

        with self._lock:
            pipe_progress = self._pipe_progress
            status = self._status
            transfers = self._transfers
            overall = self._overall
            if pipe_progress is None or transfers is None or overall is None:
                # Not started (or stopped).
                yield _r().Panel("", title="Pipeline", expand=False)
                return

            body_parts: List[Any] = [pipe_progress]
            if status is not None and self._status_tasks:
                body_parts.append(status)
            body_parts.append(transfers)

            yield _r().Group(
                _r().Panel(_r().Group(*body_parts),
                      title=self._title_text(),
                      expand=False),
                overall
            )

    def _render_group(self) -> Group:
        # Backward-compatible helper (some callers may still expect a Group).
        pipe_progress = self._pipe_progress
        status = self._status
        transfers = self._transfers
        overall = self._overall
        assert pipe_progress is not None
        assert transfers is not None
        assert overall is not None
        body_parts: List[Any] = [pipe_progress]
        if status is not None and self._status_tasks:
            body_parts.append(status)
        body_parts.append(transfers)
        return _r().Group(
            _r().Panel(_r().Group(*body_parts),
                  title=self._title_text(),
                  expand=False),
            overall
        )

    def start(self) -> None:
        if not self._enabled or getattr(self, "_stopped", False):
            return
        if self._live is not None:
            return

        # IMPORTANT: use the shared stderr Console instance so that any
        # `stderr_console().print(...)` calls from inside cmdlets (e.g. preflight
        # tables/prompts in download-file) cooperate with Rich Live rendering.
        # If we create a separate _r().Console(file=sys.stderr), output will fight for
        # terminal cursor control and appear "blocked"/truncated.
        from SYS.rich_display import stderr_console

        self._console = stderr_console()

        # Persistent per-pipe bars.
        self._pipe_progress = _r().Progress(
            _r().TextColumn("{task.description}"),
            _r().TimeElapsedColumn(),
            _r().BarColumn(),
            _r().TaskProgressColumn(),
            console=self._console,
            transient=False,
        )

        # Transient, per-item spinner for the currently-active subtask.
        self._subtasks = _r().Progress(
            _r().TextColumn("  "),
            _r().SpinnerColumn("simpleDots"),
            _r().TextColumn("{task.description}"),
            console=self._console,
            transient=False,
        )

        # Status line below the pipe bars. Kept simple (no extra bar) so it
        # doesn't visually offset the main pipe bar columns.
        self._status = _r().Progress(
            _r().TextColumn("  [bold]└─ {task.description}[/bold]"),
            console=self._console,
            transient=False,
        )

        # Byte-based transfer bars (download/upload) integrated into the Live view.
        self._transfers = _r().Progress(
            _r().TextColumn("  {task.description}"),
            _r().BarColumn(),
            _r().TaskProgressColumn(),
            _r().DownloadColumn(),
            _r().TransferSpeedColumn(),
            _r().TimeRemainingColumn(),
            console=self._console,
            transient=False,
        )

        self._overall = _r().Progress(
            _r().TimeElapsedColumn(),
            _r().BarColumn(),
            _r().TextColumn("{task.description}"),
            console=self._console,
            transient=False,
        )

        # Create pipe tasks up-front so the user sees the pipe structure immediately.
        self._pipe_tasks = []
        for idx, label in enumerate(self._pipe_labels):
            # Start timers only when the pipe actually begins.
            task_id = self._pipe_progress.add_task(
                f"{idx + 1}/{len(self._pipe_labels)} {label}",
                total=1,
                start=False,
            )
            self._pipe_progress.update(task_id, completed=0, total=1)
            self._pipe_tasks.append(task_id)

        self._overall_task = self._overall.add_task(
            f"Pipeline: 0/{len(self._pipe_labels)} pipes completed",
            total=max(1,
                      len(self._pipe_labels)),
        )

        self._live = _r().Live(
            self,
            console=self._console,
            refresh_per_second=10,
            transient=True
        )
        self._live.start()

    def pause(self) -> None:
        """Temporarily stop Live rendering without losing progress state."""
        if self._live is None:
            return
        try:
            self._live.stop()
        finally:
            self._live = None

    def resume(self) -> None:
        """Resume Live rendering after pause()."""
        if not self._enabled or getattr(self, "_stopped", False):
            return
        if self._live is not None:
            return
        if (self._console is None or self._pipe_progress is None
                or self._subtasks is None or self._transfers is None
                or self._overall is None):
            # Not initialized yet; start fresh.
            self.start()
            return
        self._live = _r().Live(
            self,
            console=self._console,
            refresh_per_second=10,
            transient=True
        )
        self._live.start()

    def stop(self) -> None:
        self._stopped = True
        # Safe to call whether Live is running or paused.
        if self._live is not None:
            stop_fn = self._live.stop
            has_clear = False
            try:
                signature = inspect.signature(stop_fn)
                has_clear = "clear" in signature.parameters
            except (ValueError, TypeError):
                pass

            try:
                if has_clear:
                    stop_fn(clear=True)
                else:
                    stop_fn()
            except Exception:
                logger.exception("Failed to stop Live with clear parameter; retrying without clear")
                try:
                    stop_fn()
                except Exception:
                    logger.exception("Failed to stop Live on retry")

        self._live = None
        self._console = None
        self._overall = None
        self._pipe_progress = None
        self._subtasks = None
        self._status = None
        self._transfers = None
        self._overall_task = None
        self._pipe_tasks = []
        self._transfer_tasks = {}
        self._status_tasks = {}
        self._pipe_percent_mode = {}
        self._pipe_step_total = {}
        self._pipe_step_done = {}
        self._active_subtask_text = None

    def _hide_pipe_subtasks(self, pipe_index: int) -> None:
        """Hide any visible per-item spinner rows for a pipe."""
        subtasks = self._subtasks
        if subtasks is None:
            return
        try:
            for sub_id in self._subtask_ids[int(pipe_index)]:
                try:
                    subtasks.stop_task(sub_id)
                    subtasks.update(sub_id, visible=False)
                except Exception:
                    logger.exception("Failed to stop or hide subtask %s in PipelineLiveProgress._hide_pipe_subtasks", sub_id)
        except Exception:
            logger.exception("Failed to hide pipe subtasks for index %s", pipe_index)

    def set_pipe_status_text(self, pipe_index: int, text: str) -> None:
        """Set a status line under the pipe bars for the given pipe."""
        if not self._enabled:
            return
        if not self._ensure_pipe(int(pipe_index)):
            return
        
        with self._lock:
            prog = self._status
            if prog is None:
                return

            try:
                pidx = int(pipe_index)
                msg = str(text or "").strip()
            except Exception:
                return

            # For long single-item work, hide the per-item spinner line and use this
            # dedicated status line instead.
            if self._pipe_percent_mode.get(pidx, False):
                try:
                    self._hide_pipe_subtasks(pidx)
                except Exception:
                    logger.exception("Failed to hide pipe subtasks while setting status text for pipe %s", pidx)

            task_id = self._status_tasks.get(pidx)
            if task_id is None:
                try:
                    task_id = prog.add_task(msg)
                except Exception:
                    logger.exception("Failed to add status task for pipe %s in set_pipe_status_text", pidx)
                    return
                self._status_tasks[pidx] = task_id

            try:
                prog.update(task_id, description=msg, refresh=True)
            except Exception:
                logger.exception("Failed to update status task %s in set_pipe_status_text", task_id)
            self._emit_event(
                "status",
                pipe_index=pidx,
                pipe_label=self._pipe_labels[pidx] if 0 <= pidx < len(self._pipe_labels) else str(pidx),
                text=msg,
            )

    def clear_pipe_status_text(self, pipe_index: int) -> None:
        if not self._enabled:
            return
        
        with self._lock:
            prog = self._status
            if prog is None:
                return
            try:
                pidx = int(pipe_index)
            except Exception:
                return
            task_id = self._status_tasks.pop(pidx, None)
            if task_id is None:
                return
            try:
                prog.remove_task(task_id)
            except Exception:
                logger.exception("Failed to remove pipe status task %s in clear_pipe_status_text", task_id)

    def set_pipe_percent(self, pipe_index: int, percent: int) -> None:
        """Update the pipe bar as a percent (only when single-item mode is enabled)."""
        if not self._enabled:
            return
        if not self._ensure_pipe(int(pipe_index)):
            return
        pipe_progress = self._pipe_progress
        if pipe_progress is None:
            return
        try:
            pidx = int(pipe_index)
        except Exception:
            return
        if not self._pipe_percent_mode.get(pidx, False):
            return
        try:
            pct = max(0, min(100, int(percent)))
            pipe_task = self._pipe_tasks[pidx]
            pipe_progress.update(pipe_task, completed=pct, total=100, refresh=True)
            self._update_overall()
            self._emit_event(
                "pipe-percent",
                pipe_index=pidx,
                pipe_label=self._pipe_labels[pidx] if 0 <= pidx < len(self._pipe_labels) else str(pidx),
                percent=pct,
            )
        except Exception:
            logger.exception("Failed to set pipe percent for pipe %s in set_pipe_percent", pipe_index)

    def _update_overall(self) -> None:
        """Update the overall pipeline progress task."""
        if self._overall is None or self._overall_task is None:
            return

        completed = 0
        try:
            # Count a pipe as completed if its 'done' count matches or exceeds the advertised total.
            completed = sum(
                1 for i in range(len(self._pipe_labels))
                if self._pipe_done[i] >= max(1, self._pipe_totals[i])
                and (
                    not self._pipe_percent_mode.get(i, False)
                    or self._pipe_step_total.get(i, 0) <= 0
                    or self._pipe_step_done.get(i, 0) >= self._pipe_step_total.get(i, 1)
                )
            )
        except Exception:
            logger.exception("Failed to compute completed pipes in _update_overall")
            completed = 0

        try:
            self._overall.update(
                self._overall_task,
                completed=min(completed, max(1, len(self._pipe_labels))),
                description=f"Pipeline: {completed}/{len(self._pipe_labels)} pipes completed",
            )
        except Exception:
            logger.exception("Failed to update overall pipeline task in _update_overall")

        # Auto-stop Live rendering once all pipes are complete so the progress
        # UI clears itself even if callers forget to stop it explicitly.
        try:
            if self._live is not None and self._pipe_labels:
                total_pipes = len(self._pipe_labels)
                if total_pipes > 0 and completed >= total_pipes:
                    self.stop()
        except Exception:
            logger.exception("Failed to auto-stop Live UI after all pipes completed")

    def begin_pipe_steps(self, pipe_index: int, *, total_steps: int) -> None:
        """Initialize step tracking for a pipe.

        The cmdlet must call this once up-front so we can map steps to percent.
        """
        if not self._enabled:
            return
        if not self._ensure_pipe(int(pipe_index)):
            return

        with self._lock:
            try:
                pidx = int(pipe_index)
                tot = max(1, int(total_steps))
            except Exception:
                return

            self._pipe_step_total[pidx] = tot
            self._pipe_step_done[pidx] = 0

            # Reset status line and percent.
            try:
                self.clear_pipe_status_text(pidx)
            except Exception:
                logger.exception("Failed to clear pipe status text in begin_pipe_steps for %s", pidx)
            try:
                self.set_pipe_percent(pidx, 0)
            except Exception:
                logger.exception("Failed to set initial pipe percent in begin_pipe_steps for %s", pidx)

    def advance_pipe_step(self, pipe_index: int, text: str) -> None:
        """Advance the pipe's step counter by one.

        Each call is treated as a new step (no in-place text rewrites).
        Updates:
        - status line: "i/N step: {text}"
        - pipe percent (single-item pipes only): round(i/N*100)
        """
        if not self._enabled:
            return
        if not self._ensure_pipe(int(pipe_index)):
            return

        try:
            pidx = int(pipe_index)
        except Exception:
            return

        total = int(self._pipe_step_total.get(pidx, 0) or 0)
        if total <= 0:
            # If steps weren't declared, treat as a single-step operation.
            total = 1
            self._pipe_step_total[pidx] = total

        done = int(self._pipe_step_done.get(pidx, 0) or 0) + 1
        done = min(done, total)
        self._pipe_step_done[pidx] = done

        msg = str(text or "").strip()
        line = f"{done}/{total} step: {msg}" if msg else f"{done}/{total} step"
        try:
            self.set_pipe_status_text(pidx, line)
        except Exception:
            logger.exception("Failed to set pipe status text in advance_pipe_step for pipe %s", pidx)

    def set_pipe_step(self, pipe_index: int, done: int, text: str) -> None:
        """Set the pipe step counter to an absolute index (1-based)."""
        if not self._enabled:
            return
        if not self._ensure_pipe(int(pipe_index)):
            return
        try:
            pidx = int(pipe_index)
        except Exception:
            return
        total = int(self._pipe_step_total.get(pidx, 0) or 0)
        if total <= 0:
            total = 1
            self._pipe_step_total[pidx] = total
        try:
            done_n = max(1, min(int(done), total))
        except Exception:
            done_n = 1
        self._pipe_step_done[pidx] = done_n
        msg = str(text or "").strip()
        line = f"{done_n}/{total} step: {msg}" if msg else f"{done_n}/{total} step"
        try:
            self.set_pipe_status_text(pidx, line)
        except Exception:
            logger.exception("Failed to set pipe status text in set_pipe_step for pipe %s", pidx)
        try:
            pct = 100 if done_n >= total else int(round((done_n / max(1, total)) * 100.0))
            self.set_pipe_percent(pidx, pct)
        except Exception:
            logger.exception("Failed to set pipe percent in set_pipe_step for pipe %s", pidx)

        # Percent mapping only applies when the pipe is in percent mode (single-item).
        try:
            pct = 100 if done >= total else int(round((done / max(1, total)) * 100.0))
            self.set_pipe_percent(pidx, pct)
        except Exception:
            logger.exception("Failed to set pipe percent in advance_pipe_step for pipe %s", pidx)

    def begin_transfer(self, *, label: str, total: Optional[int] = None) -> None:
        if not self._enabled:
            return
        if self._transfers is None:
            return
        key = str(label or "transfer")
        if key in self._transfer_tasks:
            # If it already exists, treat as an update to total.
            try:
                if total is not None and total > 0:
                    self._transfers.update(self._transfer_tasks[key], total=int(total))
            except Exception:
                logger.exception("Failed to update existing transfer task total for %s in begin_transfer", key)
            return
        task_total = int(total) if isinstance(total, int) and total > 0 else None
        try:
            task_id = self._transfers.add_task(key, total=task_total)
            self._transfer_tasks[key] = task_id
            self._emit_event("transfer-begin", label=key, total=task_total)
        except Exception:
            logger.exception("Failed to add transfer task %s in begin_transfer", key)

    def update_transfer(
        self,
        *,
        label: str,
        completed: Optional[int],
        total: Optional[int] = None
    ) -> None:
        if not self._enabled:
            return
        if self._transfers is None:
            return
        key = str(label or "transfer")
        if key not in self._transfer_tasks:
            self.begin_transfer(label=key, total=total)
        task_id = self._transfer_tasks.get(key)
        if task_id is None:
            return
        try:
            kwargs: Dict[str,
                         Any] = {}
            if completed is not None:
                kwargs["completed"] = int(completed)
            if total is not None and total > 0:
                kwargs["total"] = int(total)
            self._transfers.update(task_id, refresh=True, **kwargs)
            self._emit_event(
                "transfer",
                label=key,
                completed=(int(completed) if completed is not None else None),
                total=(int(total) if total is not None and total > 0 else None),
            )
        except Exception:
            logger.exception("Failed to update transfer '%s'", key)

    def finish_transfer(self, *, label: str) -> None:
        if self._transfers is None:
            return
        key = str(label or "transfer")
        task_id = self._transfer_tasks.pop(key, None)
        if task_id is None:
            return
        try:
            self._transfers.remove_task(task_id)
            self._emit_event("transfer-finish", label=key)
        except Exception:
            logger.exception("Failed to remove transfer task '%s' in finish_transfer", key)

    def _ensure_pipe(self, pipe_index: int) -> bool:
        if not self._enabled:
            return False
        if self._pipe_progress is None or self._subtasks is None or self._overall is None:
            return False
        if pipe_index < 0 or pipe_index >= len(self._pipe_labels):
            return False
        return True

    def begin_pipe(
        self,
        pipe_index: int,
        *,
        total_items: int,
        items_preview: Optional[List[Any]] = None
    ) -> None:
        if not self._ensure_pipe(pipe_index):
            return
        pipe_progress = self._pipe_progress
        subtasks = self._subtasks
        assert pipe_progress is not None
        assert subtasks is not None

        total_items = int(total_items) if isinstance(total_items, int) else 0
        total_items = max(1, total_items)
        self._pipe_totals[pipe_index] = total_items
        self._pipe_done[pipe_index] = 0
        self._subtask_active_index[pipe_index] = 0
        self._subtask_ids[pipe_index] = []

        # Reset per-item step progress for this pipe.
        try:
            self.clear_pipe_status_text(pipe_index)
        except Exception:
            logger.exception("Failed to clear pipe status text during begin_pipe for %s", pipe_index)
        try:
            self._pipe_step_total.pop(pipe_index, None)
            self._pipe_step_done.pop(pipe_index, None)
        except Exception:
            logger.exception("Failed to reset pipe step totals during begin_pipe for %s", pipe_index)

        # If this pipe will process exactly one item, allow percent-based updates.
        percent_mode = bool(int(total_items) == 1)
        self._pipe_percent_mode[pipe_index] = percent_mode

        pipe_task = self._pipe_tasks[pipe_index]
        pipe_progress.update(
            pipe_task,
            completed=0,
            total=(100 if percent_mode else total_items)
        )
        # Start the per-pipe timer now that the pipe is actually running.
        try:
            pipe_progress.start_task(pipe_task)
        except Exception:
            logger.exception("Failed to start pipe task timer in begin_pipe for %s", pipe_index)

        self._update_overall()
        self._emit_event(
            "pipe-begin",
            pipe_index=pipe_index,
            pipe_label=self._pipe_labels[pipe_index] if 0 <= pipe_index < len(self._pipe_labels) else str(pipe_index),
            total_items=total_items,
            percent_mode=percent_mode,
        )

        labels: List[str] = []
        if isinstance(items_preview, list) and items_preview:
            labels = [_pipeline_progress_item_label(x) for x in items_preview]

        # For single-item pipes, keep the UI clean: don't show a spinner row.
        if percent_mode:
            self._subtask_ids[pipe_index] = []
            self._active_subtask_text = None
            return

        for i in range(total_items):
            suffix = labels[i] if i < len(labels) else f"item {i + 1}/{total_items}"
            # Use start=False so elapsed time starts when we explicitly start_task().
            sub_id = subtasks.add_task(
                f"{self._pipe_labels[pipe_index]}: {suffix}",
                start=False
            )
            subtasks.update(sub_id, visible=False)
            self._subtask_ids[pipe_index].append(sub_id)

        # Show the first subtask spinner.
        if self._subtask_ids[pipe_index]:
            first = self._subtask_ids[pipe_index][0]
            subtasks.update(first, visible=True)
            subtasks.start_task(first)
            try:
                t = subtasks.tasks[first]
                self._active_subtask_text = str(getattr(t,
                                                        "description",
                                                        "") or "").strip() or None
            except Exception:
                logger.exception("Failed to set active subtask text for first subtask %s in begin_pipe", first)
                self._active_subtask_text = None

    def on_emit(self, pipe_index: int, emitted: Any) -> None:
        if not self._ensure_pipe(pipe_index):
            return

        pipe_progress = self._pipe_progress
        subtasks = self._subtasks
        assert pipe_progress is not None
        assert subtasks is not None

        done = self._pipe_done[pipe_index]
        total = self._pipe_totals[pipe_index]
        active = self._subtask_active_index[pipe_index]

        _steps_active = (
            self._pipe_percent_mode.get(pipe_index, False)
            and self._pipe_step_total.get(pipe_index, 0) > 0
            and self._pipe_step_done.get(pipe_index, 0) < self._pipe_step_total.get(pipe_index, 0)
        )

        # If a stage emits more than expected, or if we have no subtasks yet, extend dynamically.
        # Skip dynamic extension when percent-mode pipe has active steps (tracked elsewhere).
        if not _steps_active and ((done >= total) or (not self._subtask_ids[pipe_index])):
            if done >= total:
                total = done + 1
                self._pipe_totals[pipe_index] = total
                pipe_task = self._pipe_tasks[pipe_index]
                pipe_progress.update(pipe_task, total=total)

            # Add a placeholder/next subtask.
            label = _pipeline_progress_item_label(emitted)
            sub_id = subtasks.add_task(
                f"{self._pipe_labels[pipe_index]}: {label}"
            )
            subtasks.stop_task(sub_id)
            subtasks.update(sub_id, visible=False)
            self._subtask_ids[pipe_index].append(sub_id)

        # Complete & hide current active subtask.
        if active < len(self._subtask_ids[pipe_index]):
            current = self._subtask_ids[pipe_index][active]
            try:
                # If we didn’t have a preview label, set it now.
                subtasks.update(
                    current,
                    description=
                    f"{self._pipe_labels[pipe_index]}: {_pipeline_progress_item_label(emitted)}",
                )
            except Exception:
                logger.exception("Failed to update subtask description for current %s in on_emit", current)
            subtasks.stop_task(current)
            subtasks.update(current, visible=False)

        done += 1
        self._pipe_done[pipe_index] = done

        pipe_task = self._pipe_tasks[pipe_index]
        if self._pipe_percent_mode.get(pipe_index, False):
            if not _steps_active:
                pipe_progress.update(pipe_task, completed=100, total=100)
        else:
            pipe_progress.update(pipe_task, completed=done)

        self._emit_event(
            "pipe-emit",
            pipe_index=pipe_index,
            pipe_label=self._pipe_labels[pipe_index] if 0 <= pipe_index < len(self._pipe_labels) else str(pipe_index),
            completed=done,
            total=self._pipe_totals[pipe_index],
            item_label=_pipeline_progress_item_label(emitted),
        )

        self._update_overall()

        # Clear any status line now that it emitted.
        try:
            self.clear_pipe_status_text(pipe_index)
        except Exception:
            logger.exception("Failed to clear pipe status text after emit for %s", pipe_index)
        try:
            if not _steps_active:
                self._pipe_step_total.pop(pipe_index, None)
                self._pipe_step_done.pop(pipe_index, None)
        except Exception:
            logger.exception("Failed to pop pipe step totals after emit for %s", pipe_index)

        # Start next subtask spinner.
        next_index = active + 1
        self._subtask_active_index[pipe_index] = next_index
        if next_index < len(self._subtask_ids[pipe_index]):
            nxt = self._subtask_ids[pipe_index][next_index]
            subtasks.update(nxt, visible=True)
            subtasks.start_task(nxt)
            try:
                t = subtasks.tasks[nxt]
                self._active_subtask_text = str(getattr(t,
                                                        "description",
                                                        "") or "").strip() or None
            except Exception:
                logger.exception("Failed to set active subtask text for next subtask %s in on_emit", nxt)
                self._active_subtask_text = None
        else:
            self._active_subtask_text = None

    def finish_pipe(self, pipe_index: int, *, force_complete: bool = True) -> None:
        if not self._ensure_pipe(pipe_index):
            return

        pipe_progress = self._pipe_progress
        subtasks = self._subtasks
        overall = self._overall
        assert pipe_progress is not None
        assert subtasks is not None
        assert overall is not None

        total = self._pipe_totals[pipe_index]
        if total < 1:
            total = 1
            self._pipe_totals[pipe_index] = total
        done = self._pipe_done[pipe_index]

        # Ensure the pipe bar finishes even if cmdlet didn’t emit per item.
        if force_complete and done < total:
            pipe_task = self._pipe_tasks[pipe_index]
            if self._pipe_percent_mode.get(pipe_index, False):
                pipe_progress.update(pipe_task, completed=100, total=100)
            else:
                pipe_progress.update(pipe_task, completed=total)
            self._pipe_done[pipe_index] = total

        # Hide any remaining subtask spinners.
        for sub_id in self._subtask_ids[pipe_index]:
            try:
                subtasks.stop_task(sub_id)
                subtasks.update(sub_id, visible=False)
            except Exception:
                logger.exception("Failed to stop or hide subtask %s during finish_pipe for pipe %s", sub_id, pipe_index)

            # If we just finished the active pipe, clear the title context.
            self._active_subtask_text = None

        # Ensure status line is cleared when a pipe finishes.
        try:
            self.clear_pipe_status_text(pipe_index)
        except Exception:
            logger.exception("Failed to clear pipe status text during finish_pipe for %s", pipe_index)
        try:
            self._pipe_step_total.pop(pipe_index, None)
            self._pipe_step_done.pop(pipe_index, None)
        except Exception:
            logger.exception("Failed to pop pipe step totals during finish_pipe for %s", pipe_index)

        # Stop the per-pipe timer once the pipe is finished.
        try:
            pipe_task = self._pipe_tasks[pipe_index]
            pipe_progress.stop_task(pipe_task)
        except Exception:
            logger.exception("Failed to stop pipe task %s during finish_pipe", pipe_index)

        self._emit_event(
            "pipe-finish",
            pipe_index=pipe_index,
            pipe_label=self._pipe_labels[pipe_index] if 0 <= pipe_index < len(self._pipe_labels) else str(pipe_index),
            completed=self._pipe_done[pipe_index],
            total=self._pipe_totals[pipe_index],
        )

        self._update_overall()

    def complete_all_pipes(self) -> None:
        """Mark every configured pipe as finished so UI bars reach 100%."""
        if not self._enabled:
            return
        for idx in range(len(self._pipe_labels)):
            try:
                self.finish_pipe(idx)
            except Exception:
                logger.exception("Failed to finish pipe %s in complete_all_pipes", idx)


class PipelineStageContext:
    """Context information for the current pipeline stage."""

    def __init__(
        self,
        stage_index: int,
        total_stages: int,
        pipe_index: Optional[int] = None,
        worker_id: Optional[str] = None,
        on_emit: Optional[Callable[[Any],
                                   None]] = None,
    ):
        self.stage_index = stage_index
        self.total_stages = total_stages
        self.is_last_stage = stage_index == total_stages - 1
        self.pipe_index = int(pipe_index) if pipe_index is not None else None
        self.worker_id = worker_id
        self._on_emit = on_emit
        self.emits: List[Any] = []

    def emit(self, obj: Any) -> None:
        """Emit an object to the next pipeline stage."""
        self.emits.append(obj)
        cb = getattr(self, "_on_emit", None)
        if cb:
            try:
                cb(obj)
            except Exception:
                logger.exception("Error in PipelineStageContext.emit callback")

    def get_current_command_text(self) -> str:
        """Get the current command text (for backward compatibility)."""
        # This is maintained for backward compatibility with old code
        # In a real implementation, this would come from the stage context
        return ""

    def __repr__(self) -> str:
        return (
            f"PipelineStageContext(stage={self.stage_index}/{self.total_stages}, "
            f"pipe_index={self.pipe_index}, is_last={self.is_last_stage}, worker_id={self.worker_id})"
        )


# ============================================================================
# RESULT TABLE CLASSES
# Consolidated from result_table.py
# ============================================================================


@dataclass
class InputOption:
    """Represents an interactive input option (cmdlet argument) in a table.

    Allows users to select options that translate to cmdlet arguments,
    enabling interactive configuration right from the result table.

    Example:
        # Create an option for location selection
        location_opt = InputOption(
            "location",
            type="enum",
            choices=["local", "hydrus", "0x0"],
            description="Download destination"
        )

        # Use in result table
        table.add_input_option(location_opt)
        selected = table.select_option("location")  # Returns user choice
    """

    name: str
    """Option name (maps to cmdlet argument)"""
    type: str = "string"
    """Option type: 'string', 'enum', 'flag', 'integer'"""
    choices: List[str] = field(default_factory=list)
    """Valid choices for enum type"""
    default: Optional[str] = None
    """Default value if not specified"""
    description: str = ""
    """Description of what this option does"""
    validator: Optional[Callable[[str], bool]] = None
    """Optional validator function: takes value, returns True if valid"""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "type": self.type,
            "choices": self.choices if self.choices else None,
            "default": self.default,
            "description": self.description,
        }


@dataclass
class TUIResultCard:
    """Represents a result as a UI card with title, metadata, and actions.

    Used in hub-ui and TUI contexts to render individual search results
    as grouped components with visual structure.
    """

    title: str
    subtitle: Optional[str] = None
    metadata: Optional[Dict[str, str]] = None
    media_kind: Optional[str] = None
    tag: Optional[List[str]] = None
    file_hash: Optional[str] = None
    file_size: Optional[str] = None
    duration: Optional[str] = None

    def __post_init__(self):
        """Initialize default values."""
        if self.metadata is None:
            self.metadata = {}
        if self.tag is None:
            self.tag = []


@dataclass
class ResultColumn:
    """Represents a single column in a result table."""

    name: str
    value: str
    width: Optional[int] = None

    def __str__(self) -> str:
        """String representation of the column."""
        return f"{self.name}: {self.value}"

    def to_dict(self) -> Dict[str, str]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "value": self.value
        }


@dataclass
class ResultRow:
    """Represents a single row in a result table."""

    columns: List[ResultColumn] = field(default_factory=list)

    def add_column(self, name: str, value: Any) -> None:
        """Add a column to this row."""
        str_value = str(value) if value is not None else ""
        self.columns.append(ResultColumn(name, str_value))

    def get_column(self, name: str) -> Optional[str]:
        """Get column value by name."""
        for col in self.columns:
            if col.name.lower() == name.lower():
                return col.value
        return None

    def to_dict(self) -> List[Dict[str, str]]:
        """Convert to list of column dicts."""
        return [col.to_dict() for col in self.columns]

    def to_list(self) -> List[tuple[str, str]]:
        """Convert to list of (name, value) tuples."""
        return [(col.name, col.value) for col in self.columns]

    def __str__(self) -> str:
        """String representation of the row."""
        return " | ".join(str(col) for col in self.columns)
