#!/usr/bin/env python3
"""scripts/bootstrap.py

Unified project bootstrap helper (Python-only).

This script installs Python dependencies from `scripts/requirements.txt` and then
downloads Playwright browser binaries by running `python -m playwright install`.
By default this script installs **Chromium** only to conserve space; pass
`--browsers all` to install all supported engines (chromium, firefox, webkit).

FFmpeg: The project includes ffmpeg binaries for Windows (in MPV/ffmpeg). On Linux/macOS,
install ffmpeg using your system package manager (apt install ffmpeg, brew install ffmpeg, etc.).
ffmpeg-python is installed as a dependency, but requires ffmpeg itself to be on your PATH.

Note: This Python script is the canonical installer for the project — prefer
running `python ./scripts/bootstrap.py` locally. The platform scripts
(`scripts/bootstrap.ps1` and `scripts/bootstrap.sh`) are thin wrappers
that delegate to this script.

The install flow is owned here so `bootstrap.py` remains the single global
entry point, while platform wrappers only provide shell-specific convenience.

This file replaces the old `scripts/setup.py` to ensure the repository only has
one `setup.py` (at the repository root) for packaging.

Usage:
    python ./scripts/bootstrap.py           # install deps and playwright browsers
  python ./scripts/bootstrap.py --skip-deps
  python ./scripts/bootstrap.py --playwright-only

Optional flags:
    --skip-deps        Skip `pip install -r scripts/requirements.txt` step
  --no-playwright    Skip running `python -m playwright install` (still installs deps)
  --playwright-only  Install only Playwright browsers (installs playwright package if missing)
  --browsers         Comma-separated list of Playwright browsers to install (default: chromium)
    --install-editable Install the project in editable mode (pip install -e .) for running tests
  --install-mpv      Install MPV player if not already installed (default)
  --no-mpv           Skip installing MPV player
  --install-deno     Install the Deno runtime using the official installer (default)
  --no-deno          Skip installing the Deno runtime
  --deno-version     Pin a specific Deno version to install (e.g., v1.34.3)
  --upgrade-pip      Upgrade pip, setuptools, and wheel before installing deps
  --check-install    Verify that the 'mm' command was installed correctly
  --debug            Show detailed diagnostic information during installation
  --quiet            Suppress output (used internally by platform scripts)
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import tempfile
import urllib.request
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Optional


def _ensure_interactive_stdin() -> None:
    """If stdin is piped (e.g. via curl | python), re-open it to the terminal."""
    if not sys.stdin.isatty():
        try:
            if platform.system().lower() == "windows":
                # Ensure the handle is actually opened for reading correctly
                new_stdin = open("CONIN$", "r")
                sys.stdin = new_stdin
            else:
                sys.stdin = open("/dev/tty", "r")
            
            # Flush existing buffers to ensure clean state
            if hasattr(sys.stdin, 'flush'):
                sys.stdin.flush()
        except Exception as e:
            if "--debug" in sys.argv:
                print(f"[bootstrap] Failed to re-open stdin: {e}")


def run(cmd: list[str], quiet: bool = False, debug: bool = False, cwd: Optional[Path] = None, env: Optional[dict[str, str]] = None, check: bool = True) -> subprocess.CompletedProcess:
    if debug:
        print(f"\n> {' '.join(cmd)}")
    
    # Create a copy of the environment to potentially modify it for pip
    run_env = env.copy() if env is not None else os.environ.copy()
    
    # If we are running a python command, ensure we don't leak user site-packages
    # which can cause "ModuleNotFoundError: No module named 'attr'" errors in recent pip/rich,
    # and ensures we only use packages installed in our venv.
    if len(cmd) >= 1 and "python" in str(cmd[0]).lower():
        run_env["PYTHONNOUSERSITE"] = "1"
        # Also clear any other potentially conflicting variables
        run_env.pop("PYTHONPATH", None)

    # Ensure subprocess uses the re-opened interactive stdin if we have one
    stdin_handle = sys.stdin if not sys.stdin.isatty() or platform.system().lower() == "windows" else None

    if quiet and not debug:
        return subprocess.run(
            cmd, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL,
            cwd=str(cwd) if cwd else None,
            env=run_env,
            stdin=stdin_handle,
            check=check
        )
    else:
        if not debug:
            print(f"> {' '.join(cmd)}")
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=run_env, stdin=stdin_handle, check=check)


REPO_URL = str(os.environ.get("MM_REPO_URL") or "").strip()
HYDRUS_REPO_URL = "https://github.com/hydrusnetwork/hydrus.git"
HYDRUS_WEB_GUI_REPO_URL = str(os.environ.get("MM_HYDRUS_WEB_GUI_REPO") or "").strip()
HYDRUS_INSTALLER_SCRIPT_URLS = ()
RUN_CLIENT_SCRIPT_URLS = ()


class ProgressBar:
    def __init__(self, total: int, quiet: bool = False):
        self.total = total
        self.current = 0
        self.quiet = quiet
        self.bar_width = 40

    def update(self, step_name: str):
        if self.current < self.total:
            self.current += 1
        if self.quiet:
            return

        percent = int(100 * (self.current / self.total))
        filled = int(self.bar_width * self.current // self.total)
        bar = "█" * filled + "░" * (self.bar_width - filled)

        # Keep progress output append-only for terminals that do not reliably
        # handle cursor-up ANSI control sequences.
        print(f"[{bar}] {percent:>3}% | {step_name}")


LOGO = r"""
 ███╗   ███╗███████╗██████╗ ███████╗██╗ █████╗     ███╗   ███╗ █████╗  ██████╗██╗███╗   ██╗ █████╗ 
 ████╗ ████║██╔════╝██╔══██╗██╔════╝██║██╔══██╗    ████╗ ████║██╔══██╗██╔════╝██║████╗  ██║██╔══██╗
 ██╔████╔██║█████╗  ██║  ██║█████╗  ██║███████║    ██╔████╔██║███████║██║     ██║██╔██╗ ██║███████║
 ██║╚██╔╝██║██╔══╝  ██║  ██║██╔══╝  ██║██╔══██║    ██║╚██╔╝██║██╔══██║██║     ██║██║╚██╗██║██╔══██║
 ██║ ╚═╝ ██║███████╗██████╔╝███████╗██║██║  ██║    ██║ ╚═╝ ██║██║  ██║╚██████╗██║██║ ╚████║██║  ██║
 ╚═╝     ╚═╝╚══════╝╚═════╝ ╚══════╝╚═╝╚═╝  ╚═╝    ╚═╝     ╚═╝╚═╝  ╚═╝ ╚═════╝╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝
                        < ΓΝΩΘΙ ΣΕΑΥΤΟΝ | TEMET NOSCE | KNOW THYSELF >
                        0123456789123456789123456789123456789123456789
                        0246813579246813579246813579246813579246813579
                        0369369369369369369369369369369369369369369369
                        0483726159483726159483726159483726159483726159
                        0516273849516273849516273849516273849516273849
                        0639639639639639639639639639639639639639639639
                        0753816429753816429753816429753816429753816429
                        0876543219876543219876543219876543219876543219
                        0999999999999999999999999999999999999999999999
                        < ALL WITHIN ARE KNOW | ABLE ALL ARE WITHOUT >
"""


# Helpers to find shell executables and to run the platform-specific
# bootstrap script (scripts/bootstrap.sh or scripts/bootstrap.ps1).
def _find_powershell() -> str | None:
    for name in ("pwsh", "powershell"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _find_shell() -> str | None:
    for name in ("bash", "sh"):
        p = shutil.which(name)
        if p:
            return p
    return None


def run_platform_bootstrap(repo_root: Path) -> int:
    """Run the platform bootstrap script in quiet/non-interactive mode if present.

    Returns the script exit code (0 on success). If no script is present this is a
    no-op and returns 0.
    """

    ps1 = repo_root / "scripts" / "bootstrap.ps1"
    sh_script = repo_root / "scripts" / "bootstrap.sh"

    system = platform.system().lower()

    if system == "windows" and ps1.exists():
        exe = _find_powershell()
        if not exe:
            print("PowerShell not found; cannot run bootstrap.ps1", file=sys.stderr)
            return 1
        cmd = [
            exe,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ps1),
            "-Quiet",
        ]
    elif sh_script.exists():
        shell = _find_shell()
        if not shell:
            print("Shell not found; cannot run bootstrap.sh", file=sys.stderr)
            return 1
        # Use -q (quiet) to skip interactive prompts when supported.
        cmd = [shell, str(sh_script), "-q"]
    else:
        # Nothing to run
        return 0

    print("Running platform bootstrap script:", " ".join(cmd))
    rc = subprocess.run(cmd, cwd=str(repo_root))
    if rc.returncode != 0:
        print(
            f"Bootstrap script failed with exit code {rc.returncode}",
            file=sys.stderr
        )
    return int(rc.returncode or 0)


def playwright_package_installed(python_path: Optional[Path] = None) -> bool:
    interpreter = str(python_path or sys.executable)
    try:
        result = subprocess.run(
            [interpreter, "-c", "import playwright"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _build_playwright_install_cmd(browsers: str | None) -> list[str]:
    """Return the command to install Playwright browsers.

    - If browsers is None or empty: default to install Chromium only (headless).
    - If browsers contains 'all': install all engines by running 'playwright install' with no extra args.
    - Otherwise, validate entries and return a command that installs the named engines.
    
    The --with-deps flag is NOT used because:
      1. The project already includes ffmpeg (in MPV/ffmpeg)
      2. Most system dependencies should already be available
    """

    # Use --skip-browsers to just install deps without browsers, then install specific browsers
    base = [sys.executable, "-m", "playwright", "install"]
    if not browsers:
        return base + ["chromium"]

    items = [b.strip().lower() for b in browsers.split(",") if b.strip()]
    if not items:
        return base + ["chromium"]
    if "all" in items:
        return base

    allowed = {"chromium",
               "firefox",
               "webkit"}
    invalid = [b for b in items if b not in allowed]
    if invalid:
        raise ValueError(
            f"invalid browsers specified: {invalid}. Valid choices: chromium, firefox, webkit, or 'all'"
        )
    return base + items


def _check_deno_installed() -> bool:
    """Check if Deno is already installed and accessible in PATH."""
    return shutil.which("deno") is not None


def _check_mpv_installed() -> bool:
    """Check if MPV is already installed and accessible in PATH."""
    return shutil.which("mpv") is not None


def _install_mpv() -> int:
    """Install MPV player for the current platform.
    
    Returns exit code 0 on success, non-zero otherwise.
    """
    system = platform.system().lower()
    
    try:
        if system == "windows":
            # Windows: use winget (built-in package manager)
            if shutil.which("winget"):
                print("Installing MPV via winget...")
                run(
                    [
                        "winget",
                        "install",
                        "--id=mpv.net",
                        "-e",
                        "--source",
                        "winget",
                        "--accept-source-agreements",
                        "--accept-package-agreements",
                        "--silent",
                    ]
                )
            else:
                print(
                    "MPV not found and winget not available.\n"
                    "Please install MPV manually from https://mpv.io/installation/",
                    file=sys.stderr
                )
                return 1
        elif system == "darwin":
            # macOS: use Homebrew
            if shutil.which("brew"):
                print("Installing MPV via Homebrew...")
                run(["brew", "install", "mpv"])
            else:
                print(
                    "MPV not found and Homebrew not available.\n"
                    "Install Homebrew from https://brew.sh then run: brew install mpv",
                    file=sys.stderr
                )
                return 1
        else:
            # Linux: use apt, dnf, or pacman
            if shutil.which("apt"):
                print("Installing MPV via apt...")
                run(["sudo", "apt", "install", "-y", "mpv"])
            elif shutil.which("dnf"):
                print("Installing MPV via dnf...")
                run(["sudo", "dnf", "install", "-y", "mpv"])
            elif shutil.which("pacman"):
                print("Installing MPV via pacman...")
                run(["sudo", "pacman", "-S", "mpv"])
            else:
                print(
                    "MPV not found and no recognized package manager available.\n"
                    "Please install MPV manually for your distribution.",
                    file=sys.stderr
                )
                return 1

        # Verify installation
        if shutil.which("mpv"):
            print(f"MPV installed at: {shutil.which('mpv')}")
            return 0
        
        print("MPV installation completed but 'mpv' not found in PATH.", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"MPV install failed: {exc}", file=sys.stderr)
        return int(exc.returncode or 1)
    except Exception as exc:
        print(f"MPV install error: {exc}", file=sys.stderr)
        return 1


def _install_deno(version: str | None = None) -> int:
    """Install Deno runtime for the current platform.

    Uses the official Deno install scripts:
      - Unix/macOS: curl -fsSL https://deno.land/x/install/install.sh | sh [-s <version>]
      - Windows: powershell iwr https://deno.land/x/install/install.ps1 -useb | iex; Install-Deno [-Version <version>]

    Returns exit code 0 on success, non-zero otherwise.
    """

    system = platform.system().lower()

    try:
        if system == "windows":
            # Use official PowerShell installer
            if version:
                ver = version if version.startswith("v") else f"v{version}"
                ps_cmd = f"iwr https://deno.land/x/install/install.ps1 -useb | iex; Install-Deno -Version {ver}"
            else:
                ps_cmd = "iwr https://deno.land/x/install/install.ps1 -useb | iex"
            run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    ps_cmd
                ]
            )
        else:
            # POSIX: use curl + sh installer
            if version:
                ver = version if version.startswith("v") else f"v{version}"
                cmd = f"curl -fsSL https://deno.land/x/install/install.sh | sh -s {ver}"
            else:
                cmd = "curl -fsSL https://deno.land/x/install/install.sh | sh"
            run(["sh", "-c", cmd])

        # Check that 'deno' is now available in PATH
        if shutil.which("deno"):
            print(f"Deno installed at: {shutil.which('deno')}")
            return 0

        print(
            "Deno installation completed but 'deno' not found in PATH. You may need to add Deno's bin directory to your PATH manually.",
            file=sys.stderr,
        )
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Deno install failed: {exc}", file=sys.stderr)
        return int(exc.returncode or 1)


def main() -> int:
    # Ensure interactive stdin if piped
    _ensure_interactive_stdin()

    parser = argparse.ArgumentParser(
        description="Bootstrap Medios-Macina: install deps and Playwright browsers"
    )
    parser.add_argument(
        "--skip-deps",
        action="store_true",
        help="Skip installing Python dependencies from scripts/requirements.txt",
    )
    parser.add_argument(
        "--playwright",
        action="store_true",
        help="Install Playwright browsers (normally installed with .plugin -add playwright)",
    )
    parser.add_argument(
        "--no-playwright",
        action="store_true",
        help="Skip Playwright browsers (default)",
    )
    parser.add_argument(
        "--playwright-only",
        action="store_true",
        help="Only run 'playwright install' (skips dependency installation)",
    )
    parser.add_argument(
        "--no-delegate",
        action="store_true",
        help="Legacy no-op retained for wrapper compatibility; Python bootstrap is always the canonical entry point.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Quiet mode: minimize informational output (useful when called from platform wrappers)",
    )
    parser.add_argument(
        "--browsers",
        type=str,
        default="chromium",
        help=
        "Comma-separated list of browsers to install: chromium,firefox,webkit or 'all' (default: chromium)",
    )
    parser.add_argument(
        "--install-editable",
        action="store_true",
        help="Install the project in editable mode (pip install -e .) for running tests",
    )
    mpv_group = parser.add_mutually_exclusive_group()
    mpv_group.add_argument(
        "--install-mpv",
        action="store_true",
        help="Install MPV player if not already installed (default behavior)",
    )
    mpv_group.add_argument(
        "--no-mpv",
        action="store_true",
        help="Skip installing MPV player (opt out)"
    )
    deno_group = parser.add_mutually_exclusive_group()
    deno_group.add_argument(
        "--install-deno",
        action="store_true",
        help="Install the Deno runtime (normally installed with .plugin -add ytdlp)",
    )
    deno_group.add_argument(
        "--no-deno",
        action="store_true",
        help="Skip installing Deno runtime (opt out)"
    )
    parser.add_argument(
        "--deno-version",
        type=str,
        default=None,
        help="Specific Deno version to install (e.g., v1.34.3)",
    )
    parser.add_argument(
        "--upgrade-pip",
        action="store_true",
        help="Upgrade pip/setuptools/wheel before installing requirements",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show detailed diagnostic information during installation",
    )
    parser.add_argument(
        "--check-install",
        action="store_true",
        help="Verify that the 'mm' command was installed correctly",
    )
    parser.add_argument(
        "--install-hydrus-web-gui",
        action="store_true",
        help="Clone/update api-HydrusNetwork, install npm dependencies, and prepare the Hydrus web GUI",
    )
    parser.add_argument(
        "--hydrus-web-gui-root",
        type=str,
        default=None,
        help="Root directory where the Hydrus web GUI should be installed (default: the Medios repo root)",
    )
    parser.add_argument(
        "--hydrus-web-gui-dest-name",
        type=str,
        default="api-HydrusNetwork",
        help="Folder name for the Hydrus web GUI checkout (default: api-HydrusNetwork)",
    )
    parser.add_argument(
        "--install-hydrus-web-gui-service",
        action="store_true",
        help="Register the Hydrus web GUI to start automatically using systemd on Linux or Task Scheduler on Windows",
    )
    parser.add_argument(
        "--setup-hydrus-web-gui-mpv-handler",
        action="store_true",
        help="Run the Hydrus web GUI's mpv-handler setup helper after installation (Windows/Linux)",
    )
    parser.add_argument(
        "--hydrus-web-gui-service-name",
        type=str,
        default="hydrus-web-gui",
        help="Service name used for the Hydrus web GUI auto-start registration",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Uninstall local .venv and user shims (non-interactive)",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Assume yes for confirmation prompts during uninstall",
    )
    args = parser.parse_args()

    if args.install_hydrus_web_gui_service or args.setup_hydrus_web_gui_mpv_handler:
        args.install_hydrus_web_gui = True

    # Ensure repo_root is always the project root, not the current working directory
    # This prevents issues when bootstrap.py is run from different directories
    try:
        script_path = Path(__file__).resolve()
        script_dir = script_path.parent
        repo_root = script_dir.parent
    except NameError:
        # Running via pipe/eval, __file__ is not defined
        script_path = None
        script_dir = Path.cwd()
        # In Web Installer mode, we don't assume CWD is the repo root.
        repo_root = None

    # DETECT REPOSITORY
    # Check if we are already inside a valid Medios-Macina repo
    def _is_valid_mm_repo(p: Path | None) -> bool:
        if p is None: return False
        return (p / "CLI.py").exists() and (p / "scripts").exists()

    # Detect if we are already inside a valid Medios-Macina repo
    is_in_repo = _is_valid_mm_repo(repo_root)
    if not is_in_repo and _is_valid_mm_repo(Path.cwd()):
        repo_root = Path.cwd()
        script_dir = repo_root / "scripts"
        is_in_repo = True

    # If running from a pipe/standalone (Web Installer Mode), we force is_in_repo = False
    # so that Option 1 will always provide the path prompt rather than auto-detecting.
    if script_path is None:
        is_in_repo = False

    if not args.quiet and args.debug:
        print(f"Bootstrap script location: {script_dir}")
        print(f"Project root: {repo_root}")
        print(f"Current working directory: {Path.cwd()}")
        print(f"Is in repo: {is_in_repo}")

    # Helpers for interactive menu and uninstall detection
    def _venv_python_path(p: Path) -> Path | None:
        """Return the path to a python executable inside a venv directory if present."""
        if (p / "Scripts" / "python.exe").exists():
            return p / "Scripts" / "python.exe"
        if (p / "bin" / "python").exists():
            return p / "bin" / "python"
        return None

    def _is_installed() -> bool:
        """Return True if the project appears installed into the local .venv."""
        if repo_root is None:
            return False
            
        vdir = repo_root / ".venv"
        py = _venv_python_path(vdir)
        if py is None:
            return False
        try:
            # We use the global run() with check=False to avoid raising CalledProcessError
            # when the package is not found (which returns exit code 1).
            res = run([str(py), "-m", "pip", "show", "medeia-macina"], quiet=True, check=False)
            return res.returncode == 0
        except Exception:
            return False

    def _do_uninstall() -> int:
        """Attempt to remove the local venv and any shims written to the user's bin.

        If this script is running using the Python inside the local `.venv`, we
        attempt to re-run the uninstall using a Python interpreter outside the
        venv (so files can be removed on Windows). If no suitable external
        interpreter can be found, the user is asked to deactivate the venv and
        re-run the uninstall.
        """
        vdir = repo_root / ".venv"
        if not vdir.exists():
            if not args.quiet:
                print("No local .venv found; nothing to uninstall.")
            return 0

        # If the current interpreter is the one inside the local venv, try to
        # run the uninstall via a Python outside the venv so files (including
        # the interpreter binary) can be removed on Windows.
        current_exe = Path(sys.executable).resolve()
        try:
            in_venv = str(current_exe).lower().startswith(str(vdir.resolve()).lower())
        except Exception:
            in_venv = False

        if in_venv:
            if not args.quiet:
                print(f"Detected local venv Python in use: {current_exe}")

            if not args.yes:
                try:
                    resp = input("Uninstall will be attempted using a system Python outside the .venv. Continue? [Y/n]: ")
                except EOFError:
                    print("Non-interactive environment; pass --uninstall --yes to uninstall without prompts.", file=sys.stderr)
                    return 2
                if resp.strip().lower() in ("n", "no"):
                    print("Uninstall aborted.")
                    return 1

            def _find_external_python() -> list[str] | None:
                """Return a command (list) for a Python interpreter outside the venv, or None."""
                try:
                    base = Path(sys.base_prefix)
                    candidates: list[Path | str] = []
                    if platform.system().lower() == "windows":
                        candidates.append(base / "python.exe")
                    else:
                        candidates.extend([base / "bin" / "python3", base / "bin" / "python"])

                    for name in ("python3", "python"):
                        p = shutil.which(name)
                        if p:
                            candidates.append(Path(p))

                    # Special-case the Windows py launcher: ensure it resolves
                    # to a Python outside the venv before returning ['py','-3']
                    if platform.system().lower() == "windows":
                        py_launcher = shutil.which("py")
                        if py_launcher:
                            try:
                                out = subprocess.check_output(["py", "-3", "-c", "import sys; print(sys.executable)"], text=True).strip()
                                if out and not str(Path(out).resolve()).lower().startswith(str(vdir.resolve()).lower()):
                                    return ["py", "-3"]
                            except Exception:
                                pass

                    for c in candidates:
                        try:
                            if isinstance(c, Path) and c.exists():
                                c_resolved = Path(c).resolve()
                                if not str(c_resolved).lower().startswith(str(vdir.resolve()).lower()) and c_resolved != current_exe:
                                    return [str(c_resolved)]
                        except Exception:
                            continue
                except Exception:
                    pass
                return None

            ext = _find_external_python()
            if ext:
                cmd = ext + [str(repo_root / "scripts" / "bootstrap.py"), "--uninstall", "--yes"]
                if not args.quiet:
                    print("Attempting uninstall using external Python:", " ".join(cmd))
                rc = subprocess.run(cmd)
                if rc.returncode != 0:
                    print(
                        f"External uninstall exited with {rc.returncode}; ensure no processes are using files in {vdir} and try again.",
                        file=sys.stderr,
                    )
                return int(rc.returncode or 0)

            print(
                "Could not find a Python interpreter outside the local .venv. Please deactivate your venv (run 'deactivate') or run the uninstall from a system Python:\n  python ./scripts/bootstrap.py --uninstall --yes",
                file=sys.stderr,
            )
            return 2

        # Normal (non-venv) uninstall flow: confirm and remove launchers, shims, and venv
        if not args.yes:
            try:
                prompt = input(f"Remove local virtualenv at {vdir} and installed user shims? [y/N]: ")
            except EOFError:
                print("Non-interactive environment; pass --uninstall --yes to uninstall without prompts.", file=sys.stderr)
                return 2
            if prompt.strip().lower() not in ("y", "yes"):
                print("Uninstall aborted.")
                return 1

        # Remove repo-local launchers
        def _remove_launcher(path: Path) -> None:
            if path.exists():
                try:
                    path.unlink()
                    if not args.quiet:
                        print(f"Removed local launcher: {path}")
                except Exception as exc:
                    print(f"Warning: failed to remove {path}: {exc}", file=sys.stderr)

        scripts_launcher = repo_root / "scripts" / "mm.ps1"
        _remove_launcher(scripts_launcher)
        for legacy in ("mm", "mm.ps1", "mm.bat"):
            _remove_launcher(repo_root / legacy)

        # Remove user shims that the installer may have written
        try:
            system = platform.system().lower()
            if system == "windows":
                user_bin = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "bin"
                if user_bin.exists():
                    for name in ("mm.ps1",):
                        p = user_bin / name
                        if p.exists():
                            try:
                                p.unlink()
                                if not args.quiet:
                                    print(f"Removed user shim: {p}")
                            except Exception as exc:
                                print(f"Warning: failed to remove {p}: {exc}", file=sys.stderr)
            else:
                user_bin = Path(os.environ.get("XDG_BIN_HOME", str(Path.home() / ".local/bin")))
                if user_bin.exists():
                    p = user_bin / "mm"
                    if p.exists():
                        p.unlink()
                        if not args.quiet:
                            print(f"Removed user shim: {p}")
        except Exception as exc:
            print(f"Warning: failed to remove user shims: {exc}", file=sys.stderr)

        # Remove .venv directory
        try:
            shutil.rmtree(vdir)
            if not args.quiet:
                print(f"Removed local virtualenv: {vdir}")
        except Exception as exc:
            print(f"Failed to remove venv: {exc}", file=sys.stderr)
            return 1
        return 0

    def _update_config_value(root: Path, key: str, value: str) -> bool:
        db_path = root / "medios.db"
        config_path = root / "config.conf"
        
        # Try database first
        if db_path.exists():
            try:
                import sqlite3
                with sqlite3.connect(str(db_path), timeout=30.0) as conn:
                    conn.row_factory = sqlite3.Row
                    cur = conn.cursor()
                    
                    # Find all existing hydrusnetwork instance names
                    cur.execute(
                        "SELECT DISTINCT item_name FROM config WHERE category='plugin' AND subtype='hydrusnetwork'"
                    )
                    rows = cur.fetchall()
                    item_names = [r[0] for r in rows if r[0]]
                    
                    if not item_names:
                        # Only create if none exist. Use a sensible name from the path if possible.
                        # We don't have the hydrus_path here easily, but we can try to find it.
                        # For now, if we are in bootstrap, we might just be setting a global.
                        # But this function is specifically for instance settings.
                        # Let's use 'home' instead of 'hydrus' as it's the standard default.
                        item_name = "home"
                        cur.execute(
                            "INSERT OR REPLACE INTO config (category, subtype, item_name, key, value) VALUES (?, ?, ?, ?, ?)",
                            ('plugin', 'hydrusnetwork', item_name, 'NAME', item_name)
                        )
                        item_names = [item_name]

                    # Update all existing instances with this key/value
                    for name in item_names:
                        cur.execute(
                            "INSERT OR REPLACE INTO config (category, subtype, item_name, key, value) VALUES (?, ?, ?, ?, ?)",
                            ('plugin', 'hydrusnetwork', name, key, value)
                        )
                    
                    conn.commit()
                    return True
            except Exception as e:
                print(f"Error updating database config: {e}")

        # Fallback to config.conf
        if not config_path.exists():
            fallback = root / "config.conf.remove"
            if fallback.exists():
                shutil.copy(fallback, config_path)
            else:
                return False
        try:
            content = config_path.read_text(encoding="utf-8")
            pattern = rf'^(\s*{re.escape(key)}\s*=\s*)(.*)$'
            if re.search(pattern, content, flags=re.MULTILINE):
                new_content = re.sub(pattern, rf'\1"{value}"', content, flags=re.MULTILINE)
            else:
                section_pattern = r'\[store=hydrusnetwork\]'
                if re.search(section_pattern, content):
                    new_content = re.sub(section_pattern, f'[store=hydrusnetwork]\n{key}="{value}"', content, count=1)
                else:
                    new_content = content + f'\n\n[store=hydrusnetwork]\nname="hydrus"\n{key}="{value}"'
            config_path.write_text(new_content, encoding="utf-8")
            return True
        except Exception as e:
            print(f"Error updating legacy config: {e}")
            return False

    def _interactive_menu() -> str | int:
        """Show a simple interactive menu to choose install/uninstall tasks."""
        try:
            installed = _is_installed()
            while True:
                os.system("cls" if os.name == "nt" else "clear")
                term_width = shutil.get_terminal_size((80, 20)).columns

                # Use the same centering logic as the main installation screen
                logo_lines = LOGO.strip('\n').splitlines()
                max_logo_width = 0
                for line in logo_lines:
                    max_logo_width = max(max_logo_width, len(line.rstrip()))
                
                logo_padding = ' ' * max((term_width - max_logo_width) // 2, 0)
                
                print("\n" * 2)
                for line in logo_lines:
                    print(f"{logo_padding}{line.rstrip()}")
                
                print("\n")
                menu_title = "   MEDEIA MACINA BOOTSTRAP MENU   "
                border = "=" * len(menu_title)
                print(border.center(term_width))
                print(menu_title.center(term_width))
                print(border.center(term_width))
                print("\n")
                
                # Define menu options
                service_option = (
                    "5) Install Hydrus System Service (Auto-update + Headless)"
                    if installed
                    else "4) Install Hydrus System Service (Auto-update + Headless)"
                )
                options = [
                    "1) Reinstall" if installed else "1) Install",
                    "2) Extras > HydrusNetwork",
                    "3) Extras > Hydrus Web GUI",
                ]
                if installed:
                    options.append("4) Uninstall")
                options.append(service_option)
                options.append("q) Quit")

                # Center the block of options by finding the longest one
                max_opt_width = max(len(opt) for opt in options)
                opt_padding = ' ' * max((term_width - max_opt_width) // 2, 0)
                
                for opt in options:
                    print(f"{opt_padding}{opt}")
                
                prompt = "\nChoose an option: "
                # Try to center the prompt roughly
                indent = " " * max((term_width // 2) - (len(prompt) // 2), 0)
                sys.stdout.write(f"{indent}{prompt}")
                sys.stdout.flush()
                choice = sys.stdin.readline().strip().lower()
                
                if choice in ("1", "install", "reinstall"):
                    return "install"
                
                if choice in ("2", "extras", "hydrus"):
                    return "extras_hydrus"

                if choice in ("3", "web", "webgui", "gui"):
                    return "extras_hydrus_web_gui"
                
                if installed and choice in ("4", "uninstall"):
                    return "uninstall"
                
                if (installed and choice == "5") or (not installed and choice == "4"):
                    return "install_service"
                
                if choice in ("q", "quit", "exit"):
                    return 0
        except EOFError:
            return "install"

    def _prompt_hydrus_install_location() -> tuple[Path, str, bool] | None:
        """Ask the user for the Hydrus installation root and folder name."""
        default_root = Path.home()
        default_dest_name = "hydrusnetwork"
        print("\n[Standalone Hydrus Installation]")
        print("Choose where to install HydrusNetwork (the installer will create the repo there).")
        try:
            root_input = input(f"Root directory [{default_root}]: ").strip()
            if root_input:
                if len(root_input) == 2 and root_input[1] == ":" and root_input[0].isalpha():
                    root_input += "\\"
                expanded = os.path.expandvars(os.path.expanduser(root_input))
                root_path = Path(expanded).resolve()
            else:
                root_path = default_root

            dest_input = input(f"Folder name [{default_dest_name}]: ").strip()
            dest_name = dest_input or default_dest_name
            dest_path = root_path / dest_name
            force = False
            if dest_path.exists() and (dest_path / "hydrus_client.py").exists():
                print(f"Found existing Hydrus install at {dest_path}; it will be reused.")
            elif dest_path.exists():
                try:
                    nonempty = any(dest_path.iterdir())
                except OSError:
                    nonempty = True
                if nonempty:
                    overwrite = input(
                        f"{dest_path} exists and is not empty. Overwrite? [y/N]: "
                    ).strip().lower()
                    if overwrite not in {"y", "yes"}:
                        print("Hydrus installation cancelled.")
                        return None
                    force = True
            return root_path, dest_name, force
        except (EOFError, KeyboardInterrupt):
            print("\nHydrus installation cancelled.")
            return None

    def _run_standalone_hydrus_install(
        install_root: Path,
        install_dest: str,
        *,
        force: bool = False,
    ) -> bool:
        hydrus_script = None
        temp_installer_dir: Path | None = None
        temp_hydrus_repo: Path | None = None
        if is_in_repo and repo_root:
            candidate = repo_root / "scripts" / "hydrusnetwork.py"
            if candidate.exists():
                hydrus_script = candidate

        if hydrus_script is None:
            print("Downloading the Hydrus installation helper...")
            try:
                helper_dir = Path(tempfile.mkdtemp(prefix="mm_hydrus_"))
                helper_path = helper_dir / "hydrusnetwork.py"
                if _download_hydrus_installer(helper_path):
                    if not _ensure_run_client_in_target(helper_dir):
                        print("Warning: could not obtain the run_client helper; Hydrus setup may fail.")
                    hydrus_script = helper_path
                    temp_installer_dir = helper_dir
                else:
                    shutil.rmtree(helper_dir, ignore_errors=True)
            except Exception as e:
                print(f"Error setting up temporary installer: {e}")

        if hydrus_script is None:
            print("Falling back to clone the Medios-Macina repository to obtain the helper script...")
            try:
                temp_mm_repo_dir = Path(tempfile.mkdtemp(prefix="mm_repo_"))
                if _clone_repo(REPO_URL, temp_mm_repo_dir, depth=1):
                    hydrus_script = temp_mm_repo_dir / "scripts" / "hydrusnetwork.py"
                    temp_hydrus_repo = temp_mm_repo_dir
                else:
                    shutil.rmtree(temp_mm_repo_dir, ignore_errors=True)
            except Exception as e:
                print(f"Error cloning Medios-Macina repo: {e}")

        if not hydrus_script or not hydrus_script.exists():
            print("\nError: Hydrus installer script not found.")
            return False

        cmd = [
            sys.executable,
            str(hydrus_script),
            "--no-project-venv",
            "--root",
            str(install_root),
            "--dest-name",
            install_dest,
        ]
        if force:
            cmd.append("--force")

        try:
            env = os.environ.copy()
            env.pop("VIRTUAL_ENV", None)
            env.pop("PYTHONHOME", None)
            env.pop("PYTHONPATH", None)
            subprocess.check_call(cmd, env=env, stdin=sys.stdin)
            _ensure_run_client_in_target(Path(install_root) / install_dest)
            if is_in_repo and repo_root:
                _update_config_value(
                    repo_root,
                    "gitclone",
                    str(Path(install_root) / install_dest),
                )
            return True
        except subprocess.CalledProcessError:
            print("\nHydrusNetwork setup exited with an error.")
            return False
        except Exception as e:
            print(f"\nFailed to run HydrusNetwork setup: {e}")
            return False
        finally:
            if temp_installer_dir is not None:
                shutil.rmtree(temp_installer_dir, ignore_errors=True)
            if temp_hydrus_repo is not None:
                shutil.rmtree(temp_hydrus_repo, ignore_errors=True)

    def _prompt_hydrus_web_gui_location() -> tuple[Path, str, bool, bool] | None:
        """Ask the user for the Hydrus web GUI installation settings."""
        default_root = repo_root if repo_root else Path.home()
        default_dest_name = "api-HydrusNetwork"
        print("\n[Hydrus Web GUI Installation]")
        print("Install the api-HydrusNetwork web UI and optionally register it to auto-start.")
        try:
            root_input = input(f"Root directory [{default_root}]: ").strip()
            if root_input:
                if len(root_input) == 2 and root_input[1] == ":" and root_input[0].isalpha():
                    root_input += "\\"
                root_path = Path(os.path.expandvars(os.path.expanduser(root_input))).resolve()
            else:
                root_path = default_root

            dest_input = input(f"Folder name [{default_dest_name}]: ").strip()
            dest_name = dest_input or default_dest_name

            service_default = "Y"
            service_input = input(
                "Install auto-start service with systemd/Task Scheduler? [Y/n]: "
            ).strip().lower()
            install_service = service_input not in {"n", "no"}

            setup_mpv_input = input(
                "Run mpv-handler setup after install? [y/N]: "
            ).strip().lower()
            setup_mpv_handler = setup_mpv_input in {"y", "yes"}

            return root_path, dest_name, install_service, setup_mpv_handler
        except (EOFError, KeyboardInterrupt):
            print("\nHydrus web GUI installation cancelled.")
            return None

    def _clone_repo(url: str, dest: Path, depth: int = 1) -> bool:
        """Helper to clone a repository."""
        try:
            cmd = ["git", "clone"]
            if depth:
                cmd.extend(["--depth", str(depth)])
            cmd.extend([url, str(dest)])
            subprocess.check_call(cmd)
            return True
        except Exception as e:
            print(f"Error: Failed to clone repository: {e}", file=sys.stderr)
            return False

    def _download_hydrus_installer(dest: Path) -> bool:
        """Download the hydrusnetwork.py helper script into the provided path."""
        last_exc: Exception | None = None
        for url in HYDRUS_INSTALLER_SCRIPT_URLS:
            try:
                # Add a user-agent to avoid being blocked by some servers
                req = urllib.request.Request(url, headers={"User-Agent": "Medeia-Macina-Installer"})
                with urllib.request.urlopen(req) as response:
                    dest.write_bytes(response.read())
                return True
            except Exception as exc:
                last_exc = exc
        if last_exc:
            print(f"Error: Failed to download Hydrus installer script: {last_exc}", file=sys.stderr)
        else:
            print("Error: Failed to download Hydrus installer script", file=sys.stderr)
        return False

    def _run_hydrus_web_gui_installer(
        install_root: Path,
        install_dest: str,
        *,
        install_service: bool = False,
        setup_mpv_handler: bool = False,
    ) -> bool:
        web_gui_script = script_dir / "hydrus_web_gui.py"
        if not web_gui_script.exists():
            print(f"Error: {web_gui_script} not found.", file=sys.stderr)
            return False

        cmd = [
            sys.executable,
            str(web_gui_script),
            "--root",
            str(install_root),
            "--dest-name",
            install_dest,
            "--repo",
            HYDRUS_WEB_GUI_REPO_URL,
        ]
        if install_service:
            cmd.extend([
                "--install-service",
                "--service-name",
                args.hydrus_web_gui_service_name,
            ])
        if setup_mpv_handler:
            cmd.append("--setup-mpv-handler")
        if args.debug:
            cmd.append("--debug")

        try:
            subprocess.check_call(cmd, stdin=sys.stdin)
            return True
        except subprocess.CalledProcessError:
            return False

    def _ensure_repo_available() -> bool:
        """Prompt for a clone location when running outside the repository."""
        nonlocal repo_root, script_dir, is_in_repo
        repo_dir_name = "Medios-Macina"

        # If we have already settled on a repository path in this session, skip.
        if is_in_repo and repo_root is not None:
            return True

        if not shutil.which("git"):
            print("\nError: 'git' was not found on your PATH.", file=sys.stderr)
            print("Please install Git (https://git-scm.com/) and try again.", file=sys.stderr)
            return False

        try:
            # When piped, script_path is None. We don't want to use the detected repo_root
            # because that's just CWD.
            if script_path is not None and repo_root is not None:
                default_install = repo_root
            else:
                # When piped, default to home folder on POSIX, CWD on Windows
                if platform.system().lower() != "windows":
                    default_install = Path.home() / "medios"
                else:
                    default_install = Path.cwd() / "Medios-Macina"

            print("\n[WEB INSTALLER MODE]")
            print(f"Current working directory: {Path.cwd()}")
            print("Where would you like to install Medios-Macina?")
            
            # Use sys.stdin.readline() to be more robust than input() in some terminal environments
            sys.stdout.write(f"Installation directory [{default_install}]: ")
            sys.stdout.flush()
            install_dir_raw = sys.stdin.readline().strip()
            
            if not install_dir_raw:
                install_path = default_install
            else:
                # Resolve while expanding user paths (~) and environment variables ($HOME)
                expanded = os.path.expandvars(os.path.expanduser(install_dir_raw))
                install_path = Path(expanded).resolve()

            if install_path is None:
                print("Error: Could not determine installation path.", file=sys.stderr)
                return False

            if install_path.exists() and install_path.is_dir() and not _is_valid_mm_repo(install_path):
                try:
                    has_contents = any(install_path.iterdir())
                except Exception:
                    has_contents = False
                if has_contents:
                    install_path = install_path / repo_dir_name

        except (EOFError, KeyboardInterrupt):
            return False

        if not install_path.exists():
            print(f"Creating directory: {install_path}")
            install_path.mkdir(parents=True, exist_ok=True)

        if _is_valid_mm_repo(install_path):
            if not args.quiet:
                print(f"Using existing repository in {install_path}.")
            repo_root = install_path
        else:
            print(f"Cloning Medios-Macina into {install_path} (depth 1)...")
            print(f"Source: {REPO_URL}")
            if _clone_repo(REPO_URL, install_path, depth=1):
                repo_root = install_path
            else:
                return False

        os.chdir(str(repo_root))
        is_in_repo = True
        if not args.quiet:
            print(f"\nSuccessfully set up repository at {repo_root}")
            print("Resuming bootstrap...\n")

        script_dir = repo_root / "scripts"
        is_in_repo = True
        return True

    # If the user passed --uninstall explicitly, perform non-interactive uninstall and exit
    if args.uninstall:
        return _do_uninstall()

    if args.check_install:
        # Verify mm command is properly installed
        home = Path.home()
        system = platform.system().lower()
        
        print("Checking 'mm' command installation...")
        print()
        
        if system == "windows":
            user_bin = Path(os.environ.get("USERPROFILE", str(home))) / "bin"
            mm_bat = user_bin / "mm.bat"
            
            print("Checking for shim files:")
            print(f"  mm.bat: {'✓' if mm_bat.exists() else '✗'} ({mm_bat})")
            print()
            
            if mm_bat.exists():
                bat_content = mm_bat.read_text(encoding="utf-8")
                if "REPO=" in bat_content or "ENTRY=" in bat_content:
                    print(f"  mm.bat content looks valid ({len(bat_content)} bytes)")
                else:
                    print("  ⚠️ mm.bat content may be corrupted")
            print()
            
            # Check PATH
            path = os.environ.get("PATH", "")
            user_bin_str = str(user_bin)
            in_path = user_bin_str in path
            print("Checking PATH environment variable:")
            print(f"  {user_bin_str} in current session PATH: {'✓' if in_path else '✗'}")
            
            # Check registry
            try:
                import winreg
                reg = winreg.ConnectRegistry(None, winreg.HKEY_CURRENT_USER)
                key = winreg.OpenKey(reg, "Environment", 0, winreg.KEY_READ)
                current_path = winreg.QueryValueEx(key, "Path")[0]
                winreg.CloseKey(key)
                in_reg = user_bin_str in current_path
                print(f"  {user_bin_str} in registry PATH: {'✓' if in_reg else '✗'}")
                if not in_reg:
                    print()
                    print("📝 Note: Path is not in registry. It may work in this session but won't persist.")
                    print(f"   To fix, run: [Environment]::SetEnvironmentVariable('PATH', '{user_bin_str};' + [Environment]::GetEnvironmentVariable('PATH','User'), 'User')")
            except Exception as e:
                print(f"  Could not check registry: {e}")
            print()
            
            # Test if mm command works
            print("Testing 'mm' command...")
            try:
                result = subprocess.run(["mm", "--help"], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    print("  ✓ 'mm --help' works!")
                    print(f"     Output (first line): {result.stdout.split(chr(10))[0]}")
                else:
                    print(f"  ✗ 'mm --help' failed with exit code {result.returncode}")
                    if result.stderr:
                        print(f"     Error: {result.stderr.strip()}")
            except FileNotFoundError:
                # mm not found via PATH, try calling the .ps1 directly
                print("  ✗ 'mm' command not found in PATH")
                print("     Shims exist but command is not accessible via PATH")
                print()
                print("Attempting to call shim directly...")
                try:
                    result = subprocess.run(
                        [str(mm_bat), "--help"],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0:
                        print("  ✓ Direct shim call works!")
                        print("     The shim files are valid and functional.")
                        print()
                        print("⚠️  'mm' is not in PATH, but the shims are working correctly.")
                        print()
                        print("Possible causes and fixes:")
                        print("  1. Terminal needs restart: Close and reopen your terminal/PowerShell")
                        print("  2. PATH reload: Run: $env:Path = [Environment]::GetEnvironmentVariable('PATH', 'User') + ';' + [Environment]::GetEnvironmentVariable('PATH', 'Machine')")
                        print(f"  3. Manual PATH: Add {user_bin} to your system PATH manually")
                    else:
                        print("  ✗ Direct shim call failed")
                        if result.stderr:
                            print(f"     Error: {result.stderr.strip()}")
                except Exception as e:
                    print(f"  ✗ Could not test direct shim: {e}")
            except subprocess.TimeoutExpired:
                print("  ✗ 'mm' command timed out")
            except Exception as e:
                print(f"  ✗ Error testing 'mm': {e}")
        else:
            # POSIX (Linux/macOS)
            # Check likely installation locations
            locations = [home / ".local" / "bin" / "mm", Path("/usr/local/bin/mm"), Path("/usr/bin/mm")]
            found_shims = [p for p in locations if p.exists()]
            
            print("Checking for shim files:")
            for p in locations:
                if p.exists():
                    print(f"  mm: ✓ ({p})")
                else:
                    if args.debug:
                        print(f"  mm: ✗ ({p})")
            
            if not found_shims:
                print("  mm: ✗ (No shim found in standard locations)")
            print()
            
            path = os.environ.get("PATH", "")
            
            # Find which 'mm' is actually being run
            actual_mm = shutil.which("mm")
            print("Checking PATH environment variable:")
            if actual_mm:
                print(f"  'mm' resolved to: {actual_mm}")
                # Check if it's in a directory on the PATH
                if any(str(Path(actual_mm).parent) in p for p in path.split(os.pathsep)):
                    print("  Command is accessible via current session PATH: ✓")
                else:
                    print("  Command is found but directory may not be in current PATH: ⚠️")
            else:
                print("  'mm' not found in current session PATH: ✗")
            print()
            
            # Test if mm command works
            print("Testing 'mm' command...")
            try:
                result = subprocess.run(["mm", "--help"], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    print("  ✓ 'mm --help' works!")
                    print(f"     Output (first line): {result.stdout.split(chr(10))[0]}")
                else:
                    print(f"  ✗ 'mm --help' failed with exit code {result.returncode}")
                    if result.stderr:
                        print(f"     Error: {result.stderr.strip()}")
            except FileNotFoundError:
                print("  ✗ 'mm' command not found in PATH")
            except Exception as e:
                print(f"  ✗ Error testing 'mm': {e}")
        
        print()
        print("✅ Installation check complete!")
        return 0

    def _user_exists(username: str) -> bool:
        """Return True when a local system user with the given name exists."""
        try:
            import pwd

            pwd.getpwnam(username)
            return True
        except Exception:
            return False

    def _venv_has_qt_bindings(py: Path) -> bool:
        """Return True when the given Python has a Qt binding (PySide6/PyQt) importable."""
        probe = (
            "import importlib.util as u;"
            "raise SystemExit(0 if any(u.find_spec(m) for m in "
            "('PySide6','PyQt6','PyQt5','PySide2')) else 1)"
        )
        try:
            result = subprocess.run(
                [str(py), "-c", probe],
                capture_output=True,
                text=True,
                timeout=60,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _find_target_venv_python(target_repo: Path) -> Path:
        """Return the Python executable inside the target repo's venv, or the current interpreter."""
        for venv_name in (".venv", "venv"):
            vdir = target_repo / venv_name
            py = _venv_python_path(vdir)
            if py is None and (vdir / "bin" / "python3").exists():
                py = vdir / "bin" / "python3"
            if py is not None and py.exists():
                return py
        fallback = Path(sys.executable)
        print(f"Warning: no repo-local venv found; using {fallback}")
        return fallback

    def _ensure_qt_bindings(venv_py: Path) -> None:
        """Install PySide6 into the venv when no Qt binding is importable (headless still needs Qt)."""
        if _venv_has_qt_bindings(venv_py):
            return
        print(
            "Qt bindings (PySide6/PyQt) not found in the repo venv; "
            "installing PySide6 so the client can boot headless..."
        )
        try:
            subprocess.run(
                [str(venv_py), "-m", "pip", "install", "--no-cache-dir", "PySide6"],
                check=True,
            )
            print("PySide6 installed.")
        except Exception as exc:
            print(f"Warning: failed to install PySide6: {exc}")
            print("The client will not start without a Qt binding. Install it manually with:")
            print(f"  {venv_py} -m pip install PySide6")

    def _ensure_run_client_in_target(target_repo: Path) -> Optional[Path]:
        """Copy a run_client.py helper into the target repo if it is missing.

        The Hydrus installer normally copies this helper, but when it runs from a
        downloaded temp copy it has no run_client.py beside it, so the copy is
        silently skipped. This ensures the target repo always ends up with one.
        """
        target = target_repo / "run_client.py"
        if target.exists():
            return target

        sources = []
        if repo_root:
            sources.append(repo_root / "run_client.py")
            sources.append(repo_root / "scripts" / "run_client.py")
        sources.append(Path(__file__).resolve().parent / "run_client.py")

        for src in sources:
            try:
                if src.exists():
                    shutil.copy2(src, target)
                    if os.name != "nt":
                        target.chmod(target.stat().st_mode | 0o111)
                    print(f"Installed run_client helper to {target}")
                    return target
            except Exception as exc:
                print(f"Warning: could not copy run_client helper from {src}: {exc}")

        # Fallback: download the tracked helper from the repository. This covers
        # standalone/pipe installs where bootstrap.py has no scripts/ beside it.
        print("No local run_client.py found; downloading it...")
        for url in RUN_CLIENT_SCRIPT_URLS:
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Medeia-Macina-Installer"}
                )
                with urllib.request.urlopen(req, timeout=60) as response:
                    target.write_bytes(response.read())
                if os.name != "nt":
                    target.chmod(target.stat().st_mode | 0o111)
                print(f"Downloaded run_client helper to {target}")
                return target
            except Exception as exc:
                print(f"Warning: could not download run_client helper from {url}: {exc}")

        print("Warning: no run_client.py source found to copy into the target repo.")
        return None

    def _install_qt_system_deps() -> None:
        """Install the system packages Qt needs to run headless on Linux (offscreen)."""
        if os.name == "nt":
            return
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            print("Not running as root; skipping Qt system dependency installation.")
            return
        apt_get = shutil.which("apt-get")
        if not apt_get:
            print("apt-get not found; skipping Qt system dependency installation.")
            return
        packages = [
            "ffmpeg",
            "libglib2.0-0",
            "libfontconfig1",
            "libdbus-1-3",
            "libxkbcommon0",
        ]
        print(f"Installing Qt system dependencies: {' '.join(packages)} ...")
        try:
            subprocess.run([apt_get, "install", "-y"] + packages, check=False)
        except Exception as exc:
            print(f"Warning: could not install Qt system dependencies: {exc}")

    def _install_systemd_service_direct(
        target_repo: Path,
        service_name: str,
        service_user: str,
    ) -> bool:
        """Write and enable a systemd system service that launches the Hydrus client.

        Runs hydrus_client.py directly with the offscreen Qt platform (no display
        needed), installs any missing Qt system/binding dependencies, and prepares
        a writable HOME for the service user.
        """
        systemctl = shutil.which("systemctl")
        if not systemctl:
            print("systemctl not found; cannot install a systemd system service.")
            return False

        venv_py = _find_target_venv_python(target_repo)
        client_script = target_repo / "hydrus_client.py"

        # Keep a run_client.py helper in the repo for manual use.
        _ensure_run_client_in_target(target_repo)

        # Install Qt system dependencies (offscreen platform needs GLib etc. on Linux).
        _install_qt_system_deps()

        # Hydrus needs a Qt binding even when running headless (offscreen).
        _ensure_qt_bindings(venv_py)

        # Best-effort: create the service user so the unit can run unprivileged.
        service_home: Optional[Path] = None
        if service_user and not _user_exists(service_user):
            useradd = shutil.which("useradd")
            if useradd:
                shell = (
                    "/usr/sbin/nologin"
                    if Path("/usr/sbin/nologin").exists()
                    else "/bin/false"
                )
                service_home = Path("/var/lib") / service_user
                try:
                    subprocess.run(
                        [
                            useradd,
                            "--system",
                            "--no-create-home",
                            "--shell",
                            shell,
                            "--home-dir",
                            str(service_home),
                            service_user,
                        ],
                        check=True,
                    )
                    print(f"Created service user '{service_user}'.")
                except Exception as exc:
                    print(f"Warning: could not create service user '{service_user}': {exc}")
                    service_home = None
        elif service_user:
            service_home = Path("/var/lib") / service_user

        # Provide a writable HOME so Hydrus can write its database and crash logs.
        if service_user and service_home is not None:
            try:
                service_home.mkdir(parents=True, exist_ok=True)
                if shutil.which("chown"):
                    subprocess.run(
                        ["chown", "-R", f"{service_user}:{service_user}", str(service_home)],
                        check=False,
                    )
                print(f"Service data directory: {service_home}")
            except Exception as exc:
                print(f"Warning: could not prepare service home {service_home}: {exc}")

        # Launch hydrus_client.py directly with the offscreen Qt platform (no display).
        exec_line = f'"{venv_py}" "{client_script}"'
        print("Headless: running hydrus_client.py with QT_QPA_PLATFORM=offscreen")

        unit_lines = [
            "[Unit]",
            f"Description=Medios-Macina Hydrus Client ({service_name})",
            "After=network.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={exec_line}",
            f"WorkingDirectory={target_repo}",
            "Restart=on-failure",
            "Environment=PYTHONUNBUFFERED=1",
            "Environment=QT_QPA_PLATFORM=offscreen",
        ]
        if service_home is not None:
            unit_lines.append(f"Environment=HOME={service_home}")
        if service_user:
            unit_lines.append(f"User={service_user}")
            unit_lines.append(f"Group={service_user}")
        unit_lines.extend(
            [
                "",
                "[Install]",
                "WantedBy=multi-user.target",
            ]
        )

        try:
            unit_dir = Path("/etc/systemd/system")
            unit_dir.mkdir(parents=True, exist_ok=True)
            unit_file = unit_dir / f"{service_name}.service"
            unit_file.write_text("\n".join(unit_lines) + "\n", encoding="utf-8")
            print(f"Wrote systemd unit: {unit_file}")
        except PermissionError as exc:
            print(f"Permission denied while writing systemd unit: {exc}")
            return False
        except Exception as exc:
            print(f"Failed to write systemd unit: {exc}")
            return False

        for cmd in [
            [systemctl, "daemon-reload"],
            [systemctl, "enable", "--now", f"{service_name}.service"],
        ]:
            try:
                subprocess.run(cmd, check=True)
            except Exception as exc:
                print(f"Failed to run {' '.join(cmd)}: {exc}")
                return False

        print(f"systemd system service '{service_name}' installed and started.")
        return True

    # If no specific action flag is passed and we're in a terminal (or we're being piped), show the menu
    if (sys.stdin.isatty() or sys.stdout.isatty() or script_path is None) and not args.quiet:
        while True:
            sel = _interactive_menu()
            if sel == "install":
                if not _ensure_repo_available():
                    return 1
                args.skip_deps = False
                args.install_editable = True
                args.no_playwright = True
                # Break the loop to proceed with the main installation steps below
                break
            elif sel == "extras_hydrus":
                install_location = _prompt_hydrus_install_location()
                if install_location is None:
                    continue
                install_root, install_dest, force = install_location
                _run_standalone_hydrus_install(install_root, install_dest, force=force)
                print("\nHydrus installation task finished.")
                sys.stdout.write("Press Enter to return to menu...")
                sys.stdout.flush()
                sys.stdin.readline()
                continue
            elif sel == "extras_hydrus_web_gui":
                if not _ensure_repo_available():
                    return 1
                web_gui_location = _prompt_hydrus_web_gui_location()
                if web_gui_location is None:
                    continue
                install_root, install_dest, install_service, setup_mpv_handler = web_gui_location
                ok = _run_hydrus_web_gui_installer(
                    install_root,
                    install_dest,
                    install_service=install_service,
                    setup_mpv_handler=setup_mpv_handler,
                )
                if ok:
                    print("\nHydrus web GUI installation finished.")
                else:
                    print("\nHydrus web GUI installation failed.")
                sys.stdout.write("Press Enter to return to menu...")
                sys.stdout.flush()
                sys.stdin.readline()
                continue
            elif sel == "install_service":
                # Direct path input for the target repository
                print("\n[ SYSTEM SERVICE INSTALLATION ]")
                print("Enter the root directory of the Hydrus repository you want to run as a service.")
                print("This is the folder containing 'hydrus_client.py'.")
                
                # Default to repo_root/hydrusnetwork if available, otherwise CWD
                default_path = repo_root / "hydrusnetwork" if repo_root else Path.cwd()
                sys.stdout.write(f"Repository Root [{default_path}]: ")
                sys.stdout.flush()
                
                path_raw = sys.stdin.readline().strip()
                target_repo = Path(path_raw).resolve() if path_raw else default_path

                if not (target_repo / "hydrus_client.py").exists():
                    print(f"\n[!] Error: 'hydrus_client.py' not found in: {target_repo}")
                    print("    Please ensure you've entered the correct repository root.")
                    sys.stdout.write("\nPress Enter to return to menu...")
                    sys.stdout.flush()
                    sys.stdin.readline()
                    continue

                # Headless hydrus still needs a Qt binding, so make sure one is
                # installed before we create the service.
                try:
                    _ensure_qt_bindings(_find_target_venv_python(target_repo))
                except Exception:
                    pass

                run_client_script = Path(__file__).parent / "run_client.py"
                if not run_client_script.exists():
                    # Fallback to target repo's copy if our local one is missing
                    candidates = [
                        target_repo / "run_client.py",
                        target_repo / "scripts" / "run_client.py",
                    ]
                    for candidate in candidates:
                        if candidate.exists():
                            run_client_script = candidate
                            break

                if os.name == "nt":
                    # Windows: delegate to run_client.py (schtasks-based service).
                    if run_client_script and run_client_script.exists():
                        try:
                            subprocess.check_call(
                                [
                                    sys.executable,
                                    str(run_client_script),
                                    "--install-service",
                                    "--service-name", "hydrus-client",
                                    "--service-user", "hydrusnetwork",
                                    "--repo-root", str(target_repo),
                                    "--headless",
                                    "--pull",
                                ],
                                stdin=sys.stdin,
                            )
                            print("\nHydrus System service installed successfully.")
                        except subprocess.CalledProcessError:
                            print("\nService installation failed.")
                        except Exception as e:
                            print(f"\nError installing service: {e}")
                    else:
                        print("\nrun_client helper not found.")
                else:
                    # Linux: use the direct installer, which ensures Qt bindings,
                    # prepares a writable HOME, and prefers repo-local run_client.py.
                    if _install_systemd_service_direct(target_repo, "hydrus-client", "hydrusnetwork"):
                        print("\nHydrus System service installed successfully.")
                    else:
                        print("\nService installation failed.")
                
                sys.stdout.write("\nPress Enter to return to menu...")
                sys.stdout.flush()
                sys.stdin.readline()
                continue
            elif sel == "uninstall":
                return _do_uninstall()
            elif sel == 0:
                return 0
            elif sel == "menu":
                continue

    if not is_in_repo:
        if not _ensure_repo_available():
            return 1

    if sys.version_info < (3, 8):
        print("Warning: Python 3.8+ is recommended.", file=sys.stderr)

    # UI setup: Logo and Progress Bar
    if not args.quiet and not args.debug:
        # Clear the terminal before showing logo
        os.system('cls' if os.name == 'nt' else 'clear')
        term_width = shutil.get_terminal_size((80, 20)).columns
        logo_lines = LOGO.strip('\n').splitlines()
        max_line_width = 0
        for line in logo_lines:
            max_line_width = max(max_line_width, len(line.rstrip()))
        padding = ' ' * max((term_width - max_line_width) // 2, 0)
        print("\n" * 2)
        for line in logo_lines:
            print(f"{padding}{line.rstrip()}")
        print("\n")

    if repo_root is None:
        print("Error: No project repository found. Please ensure you are running this script inside the project folder or follow the interactive install prompts.", file=sys.stderr)
        return 1

    should_install_deps = not args.skip_deps and not args.playwright_only
    should_install_playwright = bool(getattr(args, "playwright", False)) and not args.no_playwright
    should_install_project = not args.playwright_only

    total_steps = 2
    if args.playwright_only:
        total_steps += 1
    else:
        if args.upgrade_pip:
            total_steps += 1
        if should_install_deps:
            total_steps += 1
        if should_install_playwright:
            total_steps += 1
        if should_install_project:
            total_steps += 1
        total_steps += 3
        if not getattr(args, "no_mpv", False):
            total_steps += 1
        if getattr(args, "install_deno", False):
            total_steps += 1
        if args.install_hydrus_web_gui:
            total_steps += 1
    
    pb = ProgressBar(total_steps, quiet=args.quiet or args.debug)

    def _run_cmd(cmd: list[str], cwd: Optional[Path] = None, context: str = ""):
        """Helper to run commands with shared settings."""
        if not args.quiet and context:
            print(f"[working] {context}")
        # In normal mode, stream subprocess output so users can see activity.
        run(cmd, quiet=(args.quiet and not args.debug), debug=args.debug, cwd=cwd)

    # Opinionated: always create or use a local venv at the project root (.venv)
    venv_dir = repo_root / ".venv"
    
    # Validate that venv_dir is where we expect it to be
    if not args.quiet:
        print(f"Planned venv location: {venv_dir}")
        if venv_dir.parent != repo_root:
            print(f"WARNING: venv parent is {venv_dir.parent}, expected {repo_root}", file=sys.stderr)
        if "scripts" in str(venv_dir).lower():
            print(f"WARNING: venv path contains 'scripts': {venv_dir}", file=sys.stderr)

    def _venv_python_bin(p: Path) -> Path:
        if platform.system().lower() == "windows":
            return p / "Scripts" / "python.exe"
        return p / "bin" / "python"

    def _ensure_local_venv() -> Path:
        """Create (if missing) and return the path to the venv's python executable."""

        try:
            if not venv_dir.exists():
                _run_cmd([sys.executable, "-m", "venv", str(venv_dir)])
            
            py = _venv_python_bin(venv_dir)
            if not py.exists():
                _run_cmd([sys.executable, "-m", "venv", str(venv_dir)])

            py = _venv_python_bin(venv_dir)
            if not py.exists():
                raise RuntimeError(f"Unable to locate venv python at {py}")
            return py
        except subprocess.CalledProcessError as exc:
            print(f"Failed to create or prepare local venv: {exc}", file=sys.stderr)
            raise
    
    def _ensure_pip_available(python_path: Path) -> None:
        """Ensure pip is available inside the venv; fall back to ensurepip if needed."""

        try:
            # Use run() to ensure clean environment (fixes ModuleNotFoundError: No module named 'attr' in pipe mode)
            run([str(python_path), "-m", "pip", "--version"], quiet=True)
            return
        except Exception:
            pass

        try:
            # ensurepip is a stdlib module, but it can still benefit from a clean environment
            # although usually it doesn't use site-packages.
            run([str(python_path), "-m", "ensurepip", "--upgrade"], quiet=True)
        except Exception:
            print(
                "Failed to install pip inside the local virtualenv via ensurepip; ensure your Python build includes ensurepip and retry.",
                file=sys.stderr,
            )
            raise

    # 1. Virtual Environment Setup
    pb.update("Preparing virtual environment...")
    venv_python = _ensure_local_venv()
    
    # 2. Pip Availability
    pb.update("Checking for pip...")
    _ensure_pip_available(venv_python)

    try:
        if args.playwright_only:
            # Playwright browser install (short-circuit)
            pb.update("Setting up Playwright and browsers...")
            if not playwright_package_installed(venv_python):
                _run_cmd(
                    [str(venv_python), "-m", "pip", "install", "--no-cache-dir", "playwright"],
                    context="Installing Playwright package",
                )

            try:
                cmd = _build_playwright_install_cmd(args.browsers)
                cmd[0] = str(venv_python)
                _run_cmd(cmd, context="Installing Playwright browsers")
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
            return 0

        # Progress tracking continues for full install
        if args.upgrade_pip:
            pb.update("Upgrading pip/setuptools/wheel...")
            _run_cmd(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    "--no-cache-dir",
                    "pip",
                    "setuptools",
                    "wheel",
                ],
                context="Upgrading pip, setuptools, and wheel",
            )

        # 4. Core Dependencies
        req_file = repo_root / "scripts" / "requirements.txt"
        if should_install_deps:
            pb.update("Installing core dependencies...")
            if req_file.exists():
                _run_cmd(
                    [str(venv_python), "-m", "pip", "install", "--no-cache-dir", "-r", str(req_file)],
                    context=f"Installing dependency set from {req_file}",
                )

        # 5. Playwright Setup
        if should_install_playwright:
            pb.update("Setting up Playwright and browsers...")
            if not playwright_package_installed(venv_python):
                _run_cmd(
                    [str(venv_python), "-m", "pip", "install", "--no-cache-dir", "playwright"],
                    context="Installing Playwright package",
                )

            try:
                cmd = _build_playwright_install_cmd(args.browsers)
                cmd[0] = str(venv_python)
                _run_cmd(cmd, context="Installing Playwright browsers")
            except Exception:
                pass

        # 6. Internal Components
        if should_install_project:
            pb.update("Installing internal components...")
            if platform.system() != "Windows":
                old_mm = venv_dir / "bin" / "mm"
                if old_mm.exists():
                    try:
                        old_mm.unlink()
                    except Exception:
                        pass

            project_cmd = [str(venv_python), "-m", "pip", "install", "--no-cache-dir"]
            if args.install_editable:
                project_cmd.extend(["-e", str(repo_root)])
            else:
                project_cmd.append(str(repo_root))
            _run_cmd(project_cmd, context="Installing Medios-Macina package")

        # 7. CLI Verification
        pb.update("Verifying CLI configuration...")
        # Check core imports (skip optional python-mpv here because it depends on
        # the system libmpv shared library and is not required for the repo-local
        # MPV IPC integration; MPV executable availability is handled below.)
        try:
            missing = []
            for mod in ["importlib", "shutil", "subprocess"]:
                try:
                    # Use run() for verification to ensure clean environment
                    run([str(venv_python), "-c", f"import {mod}"], quiet=True)
                except Exception:
                    missing.append(mod)

            # Note: If the MPV executable is missing it will be handled by the
            # MPV installation step later in this script; do not attempt to
            # install the third-party `python-mpv` package here.
            if missing:
                print(f"\nWarning: The following packages were not importable in the venv: {', '.join(missing)}")
        except Exception:
            pass

        try:
            # Use run() for CLI verification to ensure clean environment (fixes global interference)
            run([str(venv_python), "-c", "import importlib; importlib.import_module('CLI')"], quiet=True)
        except Exception:
            try:
                # If direct import fails, try to diagnose why or add a .pth link
                cmd = [
                    str(venv_python),
                    "-c",
                    (
                        "import site, sysconfig\n"
                        "out=[]\n"
                        "try:\n    out.extend(site.getsitepackages())\nexcept Exception:\n    pass\n"
                        "try:\n    p = sysconfig.get_paths().get('purelib')\n    if p:\n        out.append(p)\nexcept Exception:\n    pass\n"
                        "seen=[]; res=[]\n"
                        "for x in out:\n    if x and x not in seen:\n        seen.append(x); res.append(x)\n"
                        "for s in res:\n    print(s)\n"
                    ),
                ]
                out = subprocess.check_output(cmd, text=True, env={**os.environ, "PYTHONNOUSERSITE": "1"}).strip().splitlines()
                site_dir: Path | None = None
                for sp in out:
                    if sp and Path(sp).exists():
                        site_dir = Path(sp)
                        break
                if site_dir:
                    pth_file = site_dir / "medeia_repo.pth"
                    content = str(repo_root) + "\n"
                    if pth_file.exists():
                        txt = pth_file.read_text(encoding="utf-8")
                        if str(repo_root) not in txt:
                            with pth_file.open("a", encoding="utf-8") as fh:
                                fh.write(content)
                    else:
                        with pth_file.open("w", encoding="utf-8") as fh:
                            fh.write(content)
            except Exception:
                pass
        except Exception:
            pass

        # 8. MPV
        install_mpv_requested = not getattr(args, "no_mpv", False)
        if install_mpv_requested:
            pb.update("Setting up MPV media player...")
            if not _check_mpv_installed():
                _install_mpv()

        # 9. Deno
        install_deno_requested = bool(getattr(args, "install_deno", False))
        if install_deno_requested:
            pb.update("Setting up Deno runtime...")
            if not _check_deno_installed():
                _install_deno(args.deno_version)

        hydrus_web_gui_root = (
            Path(os.path.expandvars(os.path.expanduser(args.hydrus_web_gui_root))).resolve()
            if args.hydrus_web_gui_root
            else repo_root
        )
        if args.install_hydrus_web_gui:
            pb.update("Installing Hydrus web GUI...")
            ok = _run_hydrus_web_gui_installer(
                hydrus_web_gui_root,
                args.hydrus_web_gui_dest_name,
                install_service=args.install_hydrus_web_gui_service,
                setup_mpv_handler=args.setup_hydrus_web_gui_mpv_handler,
            )
            if not ok:
                return 1

        # 10. Finalizing setup
        pb.update("Writing launcher scripts...")
        def _write_launchers() -> None:
            launcher_dir = repo_root / "scripts"
            launcher_dir.mkdir(parents=True, exist_ok=True)
            ps1 = launcher_dir / "mm.ps1"

            ps1_text = r"""Param([Parameter(ValueFromRemainingArguments=$true)] $args)
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $scriptDir "..")).Path
$venv = Join-Path $repo '.venv'
$py = Join-Path $venv 'Scripts\python.exe'
$requirements = Join-Path $repo 'scripts\requirements.txt'

# Automatically check for updates if this is a git repository
if (Test-Path (Join-Path $repo ".git")) {
    try {
        if (-not $env:MM_NO_UPDATE) {
            $conf = Join-Path $repo "config.conf"
            $skip = $false
            if (Test-Path $conf) {
                if ($null -ne (Get-Content $conf | Select-String "auto_update\s*=\s*(false|no|off|0)")) {
                    $skip = $true
                }
            }
            if (-not $skip) {
                $mmPrefix = "[mm]"
                $mmDesign = Join-Path $repo "design\\mm.md"
                if (Test-Path -LiteralPath $mmDesign) {
                    $mmLine = Get-Content -LiteralPath $mmDesign -ErrorAction SilentlyContinue |
                        Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") } |
                        Select-Object -First 1
                    if ($mmLine) { $mmPrefix = ([string]$mmLine).Trim() }
                }
                Write-Host "Checking for updates..." -ForegroundColor Gray
                Write-Host "$mmPrefix Checking repository updates..." -ForegroundColor DarkGray
                $update = git -C "$repo" pull --ff-only 2>&1
                $gitExit = $LASTEXITCODE
                $updateText = ($update | Out-String)
                $repoUpdated = ($updateText -match "Updating|Fast-forward")
                $repoUpToDate = ($updateText -match "Already up[\s-]+to[\s-]+date")
                $skipDepUpdate = $false

                if ($gitExit -ne 0) {
                    $skipDepUpdate = $true
                    Write-Host "$mmPrefix Warning: git update check failed; continuing startup." -ForegroundColor Yellow
                    $gitTail = @($update | Select-Object -Last 3)
                    foreach ($line in $gitTail) {
                        if ($line) { Write-Host "[git] $line" -ForegroundColor DarkYellow }
                    }
                } elseif ($repoUpdated) {
                    Write-Host "$mmPrefix Repository update found (fast-forward/applied)." -ForegroundColor Green
                } elseif ($repoUpToDate) {
                    Write-Host "$mmPrefix Repository already up to date." -ForegroundColor DarkGray
                } else {
                    Write-Host "$mmPrefix Repository update check completed." -ForegroundColor DarkGray
                }

                if ($skipDepUpdate) {
                    Write-Host "$mmPrefix Python module update skipped (network/update check unavailable)." -ForegroundColor DarkGray
                } elseif (-not $env:MM_NO_DEP_UPDATE -and (Test-Path $py) -and (Test-Path $requirements)) {
                    Write-Host "$mmPrefix Checking Python module updates..." -ForegroundColor DarkGray
                    $depOutput = & $py -m pip install --disable-pip-version-check --upgrade -r $requirements 2>&1
                    $depExit = $LASTEXITCODE
                    if ($depExit -ne 0) {
                        Write-Host "$mmPrefix Warning: dependency update failed; continuing startup." -ForegroundColor Yellow
                        $depTail = @($depOutput | Select-Object -Last 5)
                        foreach ($line in $depTail) {
                            if ($line) { Write-Host "[pip] $line" -ForegroundColor DarkYellow }
                        }
                    } else {
                        $depSummary = @($depOutput | Select-String -Pattern "Successfully installed" | Select-Object -Last 1)
                        if ($depSummary.Count -gt 0) {
                            Write-Host "$mmPrefix $($depSummary[0].Line)" -ForegroundColor Green
                        } else {
                            Write-Host "$mmPrefix Python modules are already up to date." -ForegroundColor DarkGray
                        }
                    }
                } elseif ($env:MM_NO_DEP_UPDATE) {
                    Write-Host "$mmPrefix Python module update skipped (MM_NO_DEP_UPDATE=1)." -ForegroundColor DarkGray
                } else {
                    Write-Host "$mmPrefix Python module update skipped (venv python or requirements file missing)." -ForegroundColor DarkGray
                }

                if ($repoUpdated) {
                    Write-Host "Medeia-Macina has been updated. Please restart the application to apply changes." -ForegroundColor Cyan
                    exit 0
                }
            }
        }
    } catch {}
}

if (Test-Path $py) { 
    & $py -m scripts.cli_entry @args; exit $LASTEXITCODE 
}
# Ensure venv Scripts dir is on PATH for provider discovery
$venvScripts = Join-Path $venv 'Scripts'
if (Test-Path $venvScripts) { $env:PATH = $venvScripts + ';' + $env:PATH }
# Fallback to system python if venv doesn't exist
if (Test-Path (Join-Path $repo 'CLI.py')) { 
    python -m scripts.cli_entry @args 
} else {
    python -m scripts.cli_entry @args
}
"""
            try:
                ps1.write_text(ps1_text, encoding="utf-8")
            except Exception:
                pass

        _write_launchers()

        # 11. Global Environment
        pb.update("Configuring global environment...")
        def _install_user_shims(repo: Path) -> None:
            try:
                home = Path.home()
                system = platform.system().lower()

                if system == "windows":
                    user_bin = Path(os.environ.get("USERPROFILE", str(home))) / "bin"
                    user_bin.mkdir(parents=True, exist_ok=True)

                    # Validate repo path
                    if not (repo / ".venv").exists():
                        print(f"WARNING: venv not found at {repo}/.venv - mm command may not work", file=sys.stderr)
                    if not (repo / "scripts").exists():
                        print(f"WARNING: scripts folder not found at {repo}/scripts - mm command may not work", file=sys.stderr)

                    # Keep the batch shim deliberately small: cmd.exe parses every
                    # parenthesized block before executing it, which makes richer
                    # update logic brittle when paths or messages contain special
                    # batch characters.
                    mm_bat = user_bin / "mm.bat"
                    repo_bat_str = str(repo)
                    bat_text = (
                        "@echo off\n"
                        "setlocal\n"
                        f'set "REPO={repo_bat_str}"\n'
                        "set \"PY=%REPO%\\.venv\\Scripts\\python.exe\"\n"
                        "set \"PYTHONPATH=%REPO%\"\n"
                        "set \"PYTHONNOUSERSITE=1\"\n"
                        "\n"
                        "cd /d \"%REPO%\"\n"
                        "if exist \"%PY%\" (\n"
                        "  \"%PY%\" -m scripts.cli_entry %*\n"
                        "  exit /b %ERRORLEVEL%\n"
                        ")\n"
                        "python -m scripts.cli_entry %*\n"
                        "exit /b %ERRORLEVEL%\n"
                    )
                    if mm_bat.exists():
                        bak = mm_bat.with_suffix(f".bak{int(time.time())}")
                        mm_bat.replace(bak)
                    mm_bat.write_text(bat_text, encoding="utf-8")

                    # Validate that the batch shim was created correctly
                    bat_ok = mm_bat.exists() and len(mm_bat.read_text(encoding="utf-8")) > 0
                    if not bat_ok:
                        raise RuntimeError("Failed to create mm.bat shim")

                    if args.debug:
                        print(f"[bootstrap] Created mm.bat ({len(bat_text)} bytes)")
                        print(f"[bootstrap] Repo path embedded in shim: {repo}")
                        print(f"[bootstrap] Venv location: {repo}/.venv")
                        print(f"[bootstrap] Shim directory: {user_bin}")

                    # Add user_bin to PATH for current and future sessions
                    str_bin = str(user_bin)
                    cur_path = os.environ.get("PATH", "")
                    
                    # Update current session PATH if not already present
                    if str_bin not in cur_path:
                        os.environ["PATH"] = str_bin + ";" + cur_path
                    
                    # Persist to user's Windows registry PATH for future sessions
                    try:
                        ps_cmd = (
                            "$bin = '{bin}';"
                            "$cur = [Environment]::GetEnvironmentVariable('PATH','User');"
                            "if ($cur -notlike \"*$bin*\") {{"
                            "  $val = if ($cur) {{ $bin + ';' + $cur }} else {{ $bin }};"
                            "  [Environment]::SetEnvironmentVariable('PATH', $val, 'User');"
                            "}}"
                        ).format(bin=str_bin.replace("\\", "\\\\"))
                        result = subprocess.run(
                            ["powershell",
                             "-NoProfile",
                             "-Command",
                             ps_cmd],
                            check=False,
                            capture_output=True,
                            text=True
                        )
                        if args.debug and result.stderr:
                            print(f"[bootstrap] PowerShell output: {result.stderr}")
                        
                        # Also reload PATH in current session for immediate availability
                        reload_cmd = (
                            "$env:PATH = [Environment]::GetEnvironmentVariable('PATH', 'User') + ';' + [Environment]::GetEnvironmentVariable('PATH', 'Machine')"
                        )
                        subprocess.run(
                            ["powershell",
                             "-NoProfile",
                             "-Command",
                             reload_cmd],
                            check=False,
                            capture_output=True,
                            text=True
                        )
                    except Exception as e:
                        if args.debug:
                            print(f"[bootstrap] Could not persist PATH to registry: {e}", file=sys.stderr)

                    if not args.quiet:
                        print(f"Installed global launcher to: {user_bin}")
                        print("✓ mm.bat (Command Prompt and PowerShell)")
                        print()
                        print("You can now run 'mm' from any terminal window.")
                        print("If 'mm' is not found, restart your terminal or reload PATH:")
                        print("  PowerShell: $env:PATH = [Environment]::GetEnvironmentVariable('PATH','User') + ';' + [Environment]::GetEnvironmentVariable('PATH','Machine')")
                        print("  CMD: path %PATH%")

                else:
                    # POSIX
                    # If running as root (id 0), prefer /usr/bin or /usr/local/bin which are standard on PATH
                    is_root = False
                    try:
                        if platform.system().lower() != "windows" and os.getuid() == 0:
                            is_root = True
                    except (AttributeError, Exception):
                        pass

                    if is_root:
                        user_bin = Path("/usr/local/bin")
                        if not os.access(user_bin, os.W_OK):
                            user_bin = Path("/usr/bin")
                    else:
                        user_bin = Path(os.environ.get("XDG_BIN_HOME", str(home / ".local/bin")))
                    
                    user_bin.mkdir(parents=True, exist_ok=True)

                    mm_sh = user_bin / "mm"
                    
                    # Search PATH/standard locations for existing 'mm' shims to avoid conflicts
                    common_paths = [user_bin, Path("/usr/local/bin"), Path("/usr/bin"), home / ".local" / "bin"]
                    for p_dir in common_paths:
                        p_mm = p_dir / "mm"
                        if p_mm.exists() and p_mm.resolve() != mm_sh.resolve():
                            try:
                                # Only remove if it looks like one of our shims
                                content = p_mm.read_text(encoding="utf-8", errors="ignore")
                                if "Medeia" in content or "Medios" in content or "cli_entry" in content:
                                    p_mm.unlink()
                                    if not args.quiet:
                                        print(f"Removed conflicting old shim: {p_mm}")
                            except Exception:
                                pass

                    # Remove old launcher to overwrite with new one
                    if mm_sh.exists():
                        try:
                            mm_sh.unlink()
                        except Exception:
                            pass
                    sh_text = (
                        "#!/usr/bin/env bash\n"
                        "set -e\n"
                        f'REPO="{repo}"\n'
                        "# Prefer git top-level when available to avoid embedding a parent path.\n"
                        "if command -v git >/dev/null 2>&1; then\n"
                        '  gitroot=$(git -C "$REPO" rev-parse --show-toplevel 2>/dev/null || true)\n'
                        '  if [ -n "$gitroot" ]; then\n'
                        '    REPO="$gitroot"\n'
                        "  fi\n"
                        "fi\n"
                        "# If git not available or didn't resolve, walk up from CWD to find a project root.\n"
                        'if [ ! -f "$REPO/CLI.py" ] && [ ! -f "$REPO/pyproject.toml" ] && [ ! -f "$REPO/scripts/pyproject.toml" ]; then\n'
                        '  CUR="$(pwd -P)"\n'
                        '  while [ "$CUR" != "/" ] && [ "$CUR" != "" ]; do\n'
                        '    if [ -f "$CUR/CLI.py" ] || [ -f "$CUR/pyproject.toml" ] || [ -f "$CUR/scripts/pyproject.toml" ]; then\n'
                        '      REPO="$CUR"\n'
                        "      break\n"
                        "    fi\n"
                        '    CUR="$(dirname "$CUR")"\n'
                        "  done\n"
                        "fi\n"
                        'VENV="$REPO/.venv"\n'
                        "# Debug mode: set MM_DEBUG=1 to print repository, venv, and import diagnostics\n"
                        'if [ -n "${MM_DEBUG:-}" ]; then\n'
                        '  echo "[mm-debug] diagnostics" >&2\n'
                        '  echo "Resolved REPO: $REPO" >&2\n'
                        '  echo "Resolved VENV: $VENV" >&2\n'
                        '  echo "VENV exists: $( [ -d "$VENV" ] && echo yes || echo no )" >&2\n'
                        '  echo "Candidates:" >&2\n'
                        '  echo "  VENV/bin/mm: $( [ -x "$VENV/bin/mm" ] && echo yes || echo no )" >&2\n'
                        '  echo "  VENV/bin/python3: $( [ -x "$VENV/bin/python3" ] && echo yes || echo no )" >&2\n'
                        '  echo "  VENV/bin/python: $( [ -x "$VENV/bin/python" ] && echo yes || echo no )" >&2\n'
                        '  echo "  system python3: $(command -v python3 || echo none)" >&2\n'
                        '  echo "  system python: $(command -v python || echo none)" >&2\n'
                        '  for pycmd in "$VENV/bin/python3" "$VENV/bin/python" "$(command -v python3 2>/dev/null)" "$(command -v python 2>/dev/null)"; do\n'
                        '    if [ -n "$pycmd" ] && [ -x "$pycmd" ]; then\n'
                        '      echo "---- Testing with: $pycmd ----" >&2\n'
                        "      $pycmd - <<'PY'\nimport sys, importlib, traceback, importlib.util\nprint('sys.executable:', sys.executable)\nprint('sys.path (first 8):', sys.path[:8])\nfor mod in ('CLI','medeia_macina','scripts.cli_entry'):\n    try:\n        spec = importlib.util.find_spec(mod)\n        print(mod, 'spec:', spec)\n        if spec:\n            m = importlib.import_module(mod)\n            print(mod, 'loaded at', getattr(m, '__file__', None))\n    except Exception:\n        print(mod, 'import failed')\n        traceback.print_exc()\nPY\n"
                        "    fi\n"
                        "  done\n"
                        '  echo "[mm-debug] end diagnostics" >&2\n'
                        "fi\n"
                        "\n"
                        "# Automatically check for updates if this is a git repository\n"
                        'if [ -z "${MM_NO_UPDATE:-}" ] && [ -d "$REPO/.git" ] && command -v git >/dev/null 2>&1; then\n'
                        '  AUTO_UPDATE="true"\n'
                        '  if [ -f "$REPO/config.conf" ]; then\n'
                        "    if grep -qiE 'auto_update[[:space:]]*=[[:space:]]*(false|no|off|0)' \"$REPO/config.conf\"; then\n"
                        '      AUTO_UPDATE="false"\n'
                        '    fi\n'
                        '  fi\n'
                        '  if [ "$AUTO_UPDATE" = "true" ]; then\n'
                         '    echo "Checking for updates..."\n'
                         '    MM_PREFIX="[mm]"\n'
                         '    if [ -f "$REPO/design/mm.md" ]; then\n'
                         '      MM_LINE=$(grep -v "^[[:space:]]*#" "$REPO/design/mm.md" | grep -v "^[[:space:]]*$" | head -n 1 | tr -d "\\r")\n'
                         '      if [ -n "$MM_LINE" ]; then MM_PREFIX="$MM_LINE"; fi\n'
                         '    fi\n'
                         '    echo "$MM_PREFIX Checking repository updates..."\n'
                        '    UPDATE_OUT=$(git -C "$REPO" pull --ff-only 2>&1)\n'
                        '    UPDATE_EXIT=$?\n'
                        '    REPO_UPDATED="false"\n'
                        '    SKIP_DEP_UPDATE="false"\n'
                        '    if [ $UPDATE_EXIT -ne 0 ]; then\n'
                        '      SKIP_DEP_UPDATE="true"\n'
                        '      echo "$MM_PREFIX Warning: git update check failed; continuing startup."\n'
                        '      printf "%s\n" "$UPDATE_OUT" | tail -n 3\n'
                        '    elif echo "$UPDATE_OUT" | grep -qiE \'Updating|Fast-forward\'; then\n'
                        '      REPO_UPDATED="true"\n'
                        '      echo "$MM_PREFIX Repository update found (fast-forward/applied)."\n'
                        '    elif echo "$UPDATE_OUT" | grep -qiE \'Already up[ -]to[ -]date\'; then\n'
                        '      echo "$MM_PREFIX Repository already up to date."\n'
                        '    else\n'
                        '      echo "$MM_PREFIX Repository update check completed."\n'
                        '    fi\n'
                        '    MM_PY=""\n'
                        '    if [ -x "$VENV/bin/python3" ]; then\n'
                        '      MM_PY="$VENV/bin/python3"\n'
                        '    elif [ -x "$VENV/bin/python" ]; then\n'
                        '      MM_PY="$VENV/bin/python"\n'
                        '    fi\n'
                        '    if [ "$SKIP_DEP_UPDATE" = "true" ]; then\n'
                        '      echo "$MM_PREFIX Python module update skipped (network/update check unavailable)."\n'
                        '    elif [ -z "${MM_NO_DEP_UPDATE:-}" ] && [ -n "$MM_PY" ] && [ -f "$REPO/scripts/requirements.txt" ]; then\n'
                        '      echo "$MM_PREFIX Checking Python module updates..."\n'
                        '      DEP_OUT=$("$MM_PY" -m pip install --disable-pip-version-check --upgrade -r "$REPO/scripts/requirements.txt" 2>&1)\n'
                        '      DEP_EXIT=$?\n'
                        '      if [ $DEP_EXIT -ne 0 ]; then\n'
                        '        echo "$MM_PREFIX Warning: dependency update failed; continuing startup."\n'
                        '        printf "%s\n" "$DEP_OUT" | tail -n 5\n'
                        '      else\n'
                        '        DEP_SUM=$(printf "%s\n" "$DEP_OUT" | grep -i "Successfully installed" | tail -n 1 || true)\n'
                        '        if [ -n "$DEP_SUM" ]; then\n'
                        '          echo "$MM_PREFIX $DEP_SUM"\n'
                        '        else\n'
                        '          echo "$MM_PREFIX Python modules are already up to date."\n'
                        '        fi\n'
                        '      fi\n'
                        '    elif [ -n "${MM_NO_DEP_UPDATE:-}" ]; then\n'
                        '      echo "$MM_PREFIX Python module update skipped (MM_NO_DEP_UPDATE=1)."\n'
                        '    else\n'
                        '      echo "$MM_PREFIX Python module update skipped (venv python or requirements file missing)."\n'
                        '    fi\n'
                        '    if [ "$REPO_UPDATED" = "true" ]; then\n'
                        '      echo "Medeia-Macina has been updated. Please restart the application to apply changes."\n'
                        '      exit 0\n'
                        '    fi\n'
                        '  fi\n'
                        "fi\n"
                        "\n"
                        "# Use -m scripts.cli_entry directly instead of pip-generated wrapper to avoid entry point issues\n"
                        "# Prefer venv's python3, then venv's python\n"
                        'if [ -x "$VENV/bin/python3" ]; then\n'
                        '  exec "$VENV/bin/python3" -m scripts.cli_entry "$@"\n'
                        "fi\n"
                        'if [ -x "$VENV/bin/python" ]; then\n'
                        '  exec "$VENV/bin/python" -m scripts.cli_entry "$@"\n'
                        "fi\n"
                        "# Fallback to system python3, then system python (only if it's Python 3)\n"
                        "if command -v python3 >/dev/null 2>&1; then\n"
                        '  exec python3 -m scripts.cli_entry "$@"\n'
                        "fi\n"
                        "if command -v python >/dev/null 2>&1; then\n"
                        "  if python -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)'; then\n"
                        '    exec python -m scripts.cli_entry "$@"\n'
                        "  fi\n"
                        "fi\n"
                        "echo 'Error: no suitable Python 3 interpreter found. Please install Python 3 or use the venv.' >&2\n"
                        "exit 127\n"
                    )
                    if mm_sh.exists():
                        bak = mm_sh.with_suffix(f".bak{int(time.time())}")
                        mm_sh.replace(bak)
                    mm_sh.write_text(sh_text, encoding="utf-8")
                    mm_sh.chmod(mm_sh.stat().st_mode | 0o111)

                    # Ensure the user's bin is on PATH for future sessions by adding to ~/.profile
                    cur_path = os.environ.get("PATH", "")
                    if str(user_bin) not in cur_path:
                        profile = home / ".profile"
                        snippet = (
                            "# Added by Medeia-Macina setup: ensure user local bin is on PATH\n"
                            'if [ -d "$HOME/.local/bin" ] && [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then\n'
                            '  PATH="$HOME/.local/bin:$PATH"\n'
                            "fi\n"
                        )
                        try:
                            txt = profile.read_text() if profile.exists() else ""
                            if snippet.strip() not in txt:
                                with profile.open("a", encoding="utf-8") as fh:
                                    fh.write("\n" + snippet)
                        except Exception:
                            pass

                    if not args.quiet:
                        print(f"Installed global launcher to: {mm_sh}")

            except Exception as exc:  # pragma: no cover - best effort
                print(f"Failed to install global shims: {exc}", file=sys.stderr)

        _install_user_shims(repo_root)

        if not args.quiet:
            print()
            print("Installation completed successfully.")
            print(f"Environment path: {venv_dir}")
            print("Launch command from terminal: mm")
            print("Inside the app: .config")
            print()
        return 0

    except subprocess.CalledProcessError as exc:
        print(
            f"Error: command failed with exit {exc.returncode}: {exc}",
            file=sys.stderr
        )
        return int(exc.returncode or 1)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
