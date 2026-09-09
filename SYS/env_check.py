"""Environment compatibility checks for known packaging issues.

This module provides a focused check for `urllib3` correctness and a
helpful, actionable error message when the environment looks broken
(e.g., due to `urllib3-future` installing a site-packages hook).

It is intentionally lightweight and safe to import early at process
startup so the CLI can detect and surface environment problems before
trying to import cmdlets or other modules.
"""

from __future__ import annotations

import importlib
import site
import sys
from pathlib import Path
from typing import Tuple

from SYS.logger import log, debug


def _find_potential_urllib3_pth() -> list[str]:
    """Return a list of path strings that look like interfering .pth files."""
    found: list[str] = []
    try:
        paths = site.getsitepackages() or []
    except Exception:
        paths = []

    for sp in set(paths):
        try:
            candidate = Path(sp) / "urllib3_future.pth"
            if candidate.exists():
                found.append(str(candidate))
        except Exception:
            continue
    return found


def check_urllib3_compat() -> Tuple[bool, str]:
    """Quick check whether `urllib3` looks usable.

    Returns (True, "OK") when everything seems fine. When a problem is
    detected the returned tuple is (False, <actionable message>) where the
    message contains steps the user can run to fix the environment.
    """
    try:
        import urllib3  # type: ignore
    except Exception as exc:  # pragma: no cover - hard to reliably simulate ImportError across envs
        pths = _find_potential_urllib3_pth()
        lines = [
            "Your Python environment appears to have a broken or incomplete 'urllib3' installation.",
            f"ImportError: {exc!s}",
        ]
        if pths:
            lines.append(f"Found potential interfering .pth file(s): {', '.join(pths)}")
        lines.extend(
            [
                "Recommended fixes (activate the project's virtualenv first):",
                "  python -m pip uninstall urllib3-future -y",
                "  python -m pip install --upgrade --force-reinstall urllib3",
                "  python -m pip install niquests -U",
                "You may also re-run the bootstrap script: scripts\\bootstrap.ps1 (Windows) or scripts/bootstrap.sh (POSIX).",
            ]
        )
        return False, "\n".join(lines)

    # Basic sanity checks on the *imported* urllib3 module
    problems: list[str] = []
    if not getattr(urllib3, "__version__", None):
        problems.append("missing urllib3.__version__")
    if not hasattr(urllib3, "exceptions"):
        problems.append("missing urllib3.exceptions")

    try:
        spec = importlib.util.find_spec("urllib3.exceptions")
        if spec is None or not getattr(spec, "origin", None):
            problems.append("urllib3.exceptions not importable")
    except Exception:
        problems.append("urllib3.exceptions not importable (importlib check failed)")

    if problems:
        pths = _find_potential_urllib3_pth()
        lines = [
            "Your Python environment appears to have a broken 'urllib3' package:",
            f"Problems found: {', '.join(problems)}",
        ]
        if pths:
            lines.append(f"Found potential interfering .pth file(s): {', '.join(pths)}")
        lines.extend(
            [
                "Recommended fixes (activate the project's virtualenv first):",
                "  python -m pip uninstall urllib3-future -y",
                "  python -m pip install --upgrade --force-reinstall urllib3",
                "  python -m pip install niquests -U",
                "You may also re-run the bootstrap script: scripts\\bootstrap.ps1 (Windows) or scripts/bootstrap.sh (POSIX).",
            ]
        )
        return False, "\n".join(lines)

    # Looks good
    debug(
        "urllib3 appears usable: version=%s, exceptions=%s",
        getattr(urllib3,
                "__version__",
                "<unknown>"),
        hasattr(urllib3,
                "exceptions"),
    )
    return True, "OK"


def ensure_urllib3_ok(exit_on_error: bool = True) -> bool:
    """Ensure urllib3 is usable and print an actionable message if not.

    - If `exit_on_error` is True (default) this will call `sys.exit(2)` when
      a problem is detected so callers that call this early in process
      startup won't continue with a partially-broken environment.
    - If `exit_on_error` is False the function will print the message and
      return False so the caller can decide how to proceed.
    """
    ok, message = check_urllib3_compat()
    if ok:
        return True

    # Prominent user-facing output
    border = "=" * 80
    log(border)
    log("ENVIRONMENT PROBLEM DETECTED: Broken 'urllib3' package")
    log(message)
    log(border)

    if exit_on_error:
        log(
            "Please follow the steps above to fix your environment, then re-run this command."
        )
        try:
            sys.exit(2)
        except SystemExit:
            raise
    return False


if __name__ == "__main__":  # pragma: no cover - manual debugging helper
    ok, message = check_urllib3_compat()
    print(message)
    sys.exit(0 if ok else 2)
