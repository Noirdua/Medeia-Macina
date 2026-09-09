"""Rich-only progress helpers.

These functions preserve the legacy call signatures used around the codebase,
but all rendering is performed via Rich (no ASCII progress bars).
"""

from __future__ import annotations

import sys

from SYS.models import ProgressBar

_BAR = ProgressBar()


def print_progress(
    filename: str,
    current: int,
    total: int,
    speed: float = 0,
    end: str = "\r"
) -> None:
    _BAR.update(
        downloaded=int(current),
        total=int(total) if total else None,
        label=str(filename or "progress"),
        file=sys.stderr,
    )


def print_final_progress(filename: str, total: int, elapsed: float) -> None:
    _BAR.finish()
