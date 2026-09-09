"""Packaged CLI entrypoint used by installers and console scripts.

This module provides the `main` entrypoint for `mm`/`medeia` and supports
running from a development checkout (by importing the top-level
`CLI.MedeiaCLI`) or when running tests that inject a legacy
`medeia_entry` shim into `sys.modules`.
"""

from __future__ import annotations

from typing import Optional, List
import importlib
import importlib.util
import os
import sys
from pathlib import Path
import shlex
from types import ModuleType


def _ensure_repo_root_on_sys_path(pkg_file: Optional[Path] = None) -> Optional[Path]:
    """Ensure the repository root (where top-level modules live) is importable.

    The project currently keeps key modules like `CLI.py` at the repo root.
    When `mm` is invoked from a different working directory, that repo root is
    not necessarily on `sys.path`, which breaks `import CLI`.

    We infer the repo root by walking up from this package location and looking
    for a sibling `CLI.py` or checking parent directories.

    `pkg_file` exists for unit tests; production uses this module's `__file__`.
    """
    try:
        pkg_dir = (pkg_file or Path(__file__)).resolve().parent
    except Exception:
        return

    # Strategy 1: Look for CLI.py in parent directories (starting from scripts parent)
    for parent in pkg_dir.parents:
        try:
            if (parent / "CLI.py").exists():
                parent_str = str(parent)
                if parent_str not in sys.path:
                    sys.path.insert(0, parent_str)
                return parent
        except Exception:
            continue
    
    # Strategy 2: If in a venv, check the .venv/.. path (project root)
    try:
        # If this file is in .../venv/lib/python3.x/site-packages/scripts/
        # then we want to go up to find the project root
        current = pkg_dir.resolve()
        for _ in range(20):  # Safety limit
            current = current.parent
            if (current / "CLI.py").exists():
                parent_str = str(current)
                if parent_str not in sys.path:
                    sys.path.insert(0, parent_str)
                return current
    except Exception:
        pass
    
    return None


def _strip_mode_args(args: List[str]) -> List[str]:
    """Strip --gui/--cli/--mode flags from argument list.

    The GUI/TUI mode has been discontinued. Any mode flags are silently
    consumed so they don't interfere with remaining argument parsing.
    """
    out: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        la = a.lower()
        if la in ("--gui", "-g", "--cli", "-c"):
            i += 1
            continue
        if la.startswith("--gui=") or la.startswith("--cli="):
            i += 1
            continue
        if la.startswith("--mode="):
            i += 1
            continue
        if la == "--mode":
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def _run_cli(clean_args: List[str]) -> int:
    """Run the CLI runner (MedeiaCLI) with cleaned argv list.

    The function supports three modes (in order):
      1) If a `medeia_entry` module is present in sys.modules (for tests/legacy
         scenarios), use it as the source of `MedeiaCLI`.
      2) If running from a development checkout, import the top-level `CLI` and
         use `MedeiaCLI` from there.
      3) Otherwise fail with a helpful message suggesting an editable install.
    """
    try:
        sys.argv[1:] = list(clean_args)
    except Exception:
        pass

    # 1) Check for an in-memory 'medeia_entry' module (tests/legacy installs)
    MedeiaCLI = None
    if "medeia_entry" in sys.modules:
        mod = sys.modules.get("medeia_entry")
        if hasattr(mod, "MedeiaCLI"):
            MedeiaCLI = getattr(mod, "MedeiaCLI")
        else:
            # Preserve the existing error message used by tests that inject a
            # dummy 'medeia_entry' without a `MedeiaCLI` attribute.
            raise ImportError(
                "Imported module 'medeia_entry' does not define 'MedeiaCLI' and direct import of top-level 'CLI' failed.\n"
                "Remedy: ensure the top-level 'medeia_entry' module exports 'MedeiaCLI' or run from the project root/debug the checkout."
            )

    # 2) If no in-memory module provided the class, try importing the repo-root CLI
    if MedeiaCLI is None:
        try:
            repo_root = _ensure_repo_root_on_sys_path()
            from CLI import CLI as _M  # type: ignore
            MedeiaCLI = _M
        except Exception as exc:
            # Provide diagnostic information
            import traceback
            error_msg = (
                "Could not import 'MedeiaCLI'. This often means the project is not available on sys.path.\n"
                "Diagnostic info:\n"
                f"  - sys.executable: {sys.executable}\n"
                f"  - sys.path (first 5): {sys.path[:5]}\n"
                f"  - current working directory: {Path.cwd()}\n"
                f"  - this file: {Path(__file__).resolve()}\n"
            )
            try:
                repo = _ensure_repo_root_on_sys_path()
                if repo:
                    error_msg += f"  - detected repo root: {repo}\n"
                    cli_path = repo / "CLI.py"
                    error_msg += f"  - CLI.py exists at {cli_path}: {cli_path.exists()}\n"
            except Exception:
                pass
            error_msg += (
                "\nRemedy: Run 'pip install -e .' from the project root or re-run the bootstrap script.\n"
                "Set MM_DEBUG=1 to enable detailed diagnostics."
            )
            if os.environ.get("MM_DEBUG"):
                error_msg += f"\n\nTraceback:\n{traceback.format_exc()}"
            raise ImportError(error_msg) from exc

    try:
        app = MedeiaCLI()
        app.run()
        return 0
    except SystemExit as exc:
        return int(getattr(exc, "code", 0) or 0)



def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for console_scripts.

    Accepts an optional argv list (useful for testing). Mode flags are parsed
    and removed before dispatching to the selected runner.
    """
    args = list(argv) if argv is not None else list(sys.argv[1:])

    clean_args = _strip_mode_args(args)

    # Early environment sanity check to detect urllib3/urllib3-future conflicts.
    # When a broken urllib3 is detected we print an actionable message and
    # exit early to avoid confusing import-time errors later during startup.
    try:
        from SYS.env_check import ensure_urllib3_ok

        try:
            ensure_urllib3_ok(exit_on_error=True)
        except SystemExit as exc:
            # Bubble out the exit code as the CLI return value for clearer
            # behavior in shell sessions and scripts.
            return int(getattr(exc, "code", 2) or 2)
    except Exception:
        # If the sanity check itself cannot be imported or run, don't block
        # startup; we'll continue and let normal import errors surface.
        pass

    # Support quoting a pipeline (or even a single full command) on the command line.
    #
    # - If the user provides a single argument that contains a pipe character,
    #   treat it as a pipeline and rewrite the args to call the internal `pipeline`
    #   subcommand so existing CLI pipeline handling is used.
    #
    # - If the user provides a single argument that contains whitespace but no pipe,
    #   expand it into argv tokens (PowerShell commonly encourages quoting strings).
    #
    # Examples:
        #    mm "download-file <url> | add-tag 'x' | add-file -instance local"
        #    mm "download-file '<url>' -query 'format:720' -path 'C:\\out'"
    if len(clean_args) == 1:
        single = clean_args[0]
        if "|" in single and not single.startswith("-"):
            clean_args = ["pipeline", "--pipeline", single]
        elif (not single.startswith("-")) and any(ch.isspace() for ch in single):
            try:
                expanded = shlex.split(single, posix=True)
                if expanded:
                    clean_args = list(expanded)
            except Exception:
                pass

    # Default to CLI if --cli is requested or no explicit mode provided.
    return _run_cli(clean_args)


if __name__ == "__main__":
    raise SystemExit(main())
