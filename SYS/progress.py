from __future__ import annotations

from typing import Any, Optional


class TransferProgress:
    """One adapter over pipeline progress objects and plain callbacks."""

    def __init__(self, progress: Any, label: str = "download") -> None:
        self.progress = progress
        self.label = str(label or "download")
        self.started = False
        self.finished = False

    def begin(self, total: Optional[int] = None) -> None:
        if self.progress is None or self.started:
            return
        fn = getattr(self.progress, "begin_transfer", None)
        if not callable(fn):
            return
        try:
            fn(label=self.label, total=total)
        except TypeError:
            try:
                fn(self.label)
            except Exception:
                return
        except Exception:
            return
        self.started = True
        self.finished = False

    def update(self, completed: Optional[int], total: Optional[int] = None) -> None:
        if self.progress is None:
            return
        if not self.started:
            self.begin(total)
        fn = getattr(self.progress, "update_transfer", None)
        if callable(fn):
            try:
                fn(label=self.label, completed=completed, total=total)
                return
            except TypeError:
                try:
                    fn(completed, total)
                    return
                except Exception:
                    return
            except Exception:
                return
        if callable(self.progress):
            try:
                self.progress("bytes", completed, total, self.label)
            except TypeError:
                try:
                    self.progress(completed, total)
                except Exception:
                    return
            except Exception:
                return

    def finish(self) -> None:
        if self.progress is None or not self.started or self.finished:
            return
        fn = getattr(self.progress, "finish_transfer", None)
        if callable(fn):
            try:
                fn(label=self.label)
            except TypeError:
                try:
                    fn(self.label)
                except Exception:
                    return
            except Exception:
                return
        self.finished = True
        self.started = False
