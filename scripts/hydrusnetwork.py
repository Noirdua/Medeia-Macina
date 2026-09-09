#!/usr/bin/env python3
"""Create a 'hydrusnetwork' directory and clone the Hydrus repository into it.

Works on Linux and Windows. Behavior:
- By default creates ./hydrusnetwork and clones https://github.com/hydrusnetwork/hydrus there.
- If the target directory already exists:
  - When run non-interactively: Use --update to run `git pull` (if it's a git repo) or --force to re-clone.
  - When run interactively without flags, the script presents a numeric menu to choose actions:
    1) Update definitions (attempt to update a 'definitions' subdir if present)
    2) Update hydrus (git pull)
    3) Re-clone (remove and re-clone)
- If `git` is not available, the script will fall back to downloading the repository ZIP and extracting it.
- By default the script will create a repository-local virtual environment `./<dest>/.venv` after cloning/extraction; use `--no-venv` to skip this. By default the script will install dependencies from `scripts/requirements.txt` into that venv (use `--no-install-deps` to skip). After setup the script will print instructions for running the client; use `--run-client` to *launch* `hydrus_client.py` using the created repo venv's Python (use `--run-client-detached` to run it in the background).

Examples:
  python scripts/hydrusnetwork.py
  python scripts/hydrusnetwork.py --root /opt --dest-name hydrusnetwork --force
  python scripts/hydrusnetwork.py --update

"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
import re
from pathlib import Path
from typing import Optional, Sequence, Tuple

import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")

try:
    from run_client import (
        build_hydrus_install_targets,
        can_pip_install_project,
        detach_kwargs_for_platform,
        find_requirements,
        get_python_in_venv,
        install_service_auto,
        is_hydrus_repo,
        parse_requirements_file,
        uninstall_service_auto,
    )
except ImportError:
    from scripts.run_client import (
        build_hydrus_install_targets,
        can_pip_install_project,
        detach_kwargs_for_platform,
        find_requirements,
        get_python_in_venv,
        install_service_auto,
        is_hydrus_repo,
        parse_requirements_file,
        uninstall_service_auto,
    )


def _determine_service_user(args) -> Optional[str]:
    user = getattr(args, "service_user", None)
    if user:
        user = user.strip()
        if not user:
            user = None
    if (
        not user
        and os.name != "nt"
        and hasattr(os, "geteuid")
        and os.geteuid() == 0
    ):
        user = "hydrusnetwork"
    return user


def find_git_executable() -> Optional[str]:
    """Return the git executable path or None if not found."""
    import shutil as _shutil

    git = _shutil.which("git")
    if not git:
        return None
    # Quick sanity check
    try:
        subprocess.run(
            [git,
             "--version"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return git
    except Exception:
        return None


def is_git_repo(path: Path) -> bool:
    """Determine whether the given path is a git working tree."""
    if not path.exists() or not path.is_dir():
        return False
    if (path / ".git").exists():
        return True
    git = find_git_executable()
    if not git:
        return False
    try:
        subprocess.run(
            [git,
             "-C",
             str(path),
             "rev-parse",
             "--is-inside-work-tree"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def run_git_clone(
    git: str,
    repo: str,
    dest: Path,
    branch: Optional[str] = None,
    depth: Optional[int] = None
) -> None:
    # Build git clone with options before the repository argument. Support shallow clones
    # via --depth when requested.
    cmd = [git, "clone"]
    if depth is not None and depth > 0:
        cmd += ["--depth", str(int(depth))]
        # For performance/clarity, when doing a shallow clone of a specific branch,
        # prefer --single-branch to avoid fetching other refs.
        if branch:
            cmd += ["--single-branch"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [repo, str(dest)]
    logging.info(
        "Cloning: %s -> %s (depth=%s)",
        repo,
        dest,
        str(depth) if depth else "full"
    )
    subprocess.run(cmd, check=True)


def run_git_pull(git: str, dest: Path) -> None:
    logging.info("Updating git repository in %s", dest)
    subprocess.run([git, "-C", str(dest), "pull", "--ff-only"], check=True)


def _sanitize_store_name(name: str) -> str:
    clean = "".join(ch for ch in name if ch.isalnum() or ch in {"-", "_"})
    return clean or "hydrus"


def update_medios_config(hydrus_path: Path) -> bool:
    """Attempt to update Medios-Macina root configuration with the hydrus path.
    
    We look for an existing hydrusnetwork backend entry and attach the gitclone
    path to that backend. This avoids hard-coded store names such as "hydrus" or
    "hn-local" so users can pick their own alias.
    """
    script_dir = Path(__file__).resolve().parent
    root = script_dir.parent
    db_path = root / "medios.db"
    hydrus_abs_path = str(hydrus_path.resolve())

    if not db_path.exists():
        return False

    try:
        import sqlite3

        with sqlite3.connect(str(db_path), timeout=30.0) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            cur.execute(
                "SELECT DISTINCT item_name FROM config WHERE category='plugin' AND subtype='hydrusnetwork'"
            )
            rows = [row[0] for row in cur.fetchall() if row[0]]

            if not rows:
                store_name = _sanitize_store_name(hydrus_path.name)
                cur.execute(
                    "INSERT OR REPLACE INTO config (category, subtype, item_name, key, value) VALUES (?, ?, ?, ?, ?)",
                    ('plugin', 'hydrusnetwork', store_name, 'name', store_name)
                )
                rows = [store_name]

            for name in rows:
                cur.execute(
                    "INSERT OR REPLACE INTO config (category, subtype, item_name, key, value) VALUES (?, ?, ?, ?, ?)",
                    ('plugin', 'hydrusnetwork', name, 'gitclone', hydrus_abs_path)
                )

            conn.commit()
            logging.info(
                "✅ Linked Hydrus installation in medios.db for [%s] (gitclone=\"%s\")",
                rows,
                hydrus_abs_path,
            )
            return True
    except Exception as e:
        logging.error("Failed to update medios.db: %s", e)
        return False


def download_and_extract_zip(
    repo_url: str,
    dest: Path,
    branch_candidates: Tuple[str,
                             ...] = ("main",
                                     "master")
) -> None:
    """Download the GitHub repo zip and extract it into dest.

    This avoids requiring git to be installed.
    """

    # By default, if a project virtualenv is detected (".venv" or "venv" under
    # the chosen --root, or $VIRTUAL_ENV), the script will re-exec itself under
    # that venv's python interpreter so subsequent operations use the project
    # environment. Use --no-project-venv to opt out of this behavior.
    # Parse owner/repo from URL like https://github.com/owner/repo
    try:
        from urllib.parse import urlparse

        p = urlparse(repo_url)
        parts = [p for p in p.path.split("/") if p]
        if len(parts) < 2:
            raise ValueError("Cannot parse owner/repo from URL")
        owner, repo = parts[0], parts[1]
    except Exception:
        raise RuntimeError(f"Invalid repo URL: {repo_url}")

    errors = []
    for branch in branch_candidates:
        zip_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"
        logging.info("Attempting ZIP download: %s", zip_url)
        try:
            with urllib.request.urlopen(zip_url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} while fetching {zip_url}")
                with tempfile.TemporaryDirectory() as td:
                    tmpzip = Path(td) / "repo.zip"
                    with open(tmpzip, "wb") as fh:
                        fh.write(resp.read())
                    with zipfile.ZipFile(tmpzip, "r") as z:
                        z.extractall(td)
                    # Extracted content usually at repo-<branch>/
                    extracted_root = None
                    td_path = Path(td)
                    for child in td_path.iterdir():
                        if child.is_dir():
                            extracted_root = child
                            break
                    if not extracted_root:
                        raise RuntimeError("Broken ZIP: no extracted directory found")
                    # Move contents of extracted_root into dest
                    dest.mkdir(parents=True, exist_ok=True)
                    for entry in extracted_root.iterdir():
                        target = dest / entry.name
                        if target.exists():
                            # Try to remove before moving
                            if target.is_dir():
                                shutil.rmtree(target)
                            else:
                                target.unlink()
                        shutil.move(str(entry), str(dest))
                    logging.info(
                        "Downloaded and extracted %s (branch: %s) into %s",
                        repo_url,
                        branch,
                        dest
                    )
                    return
        except Exception as exc:
            errors.append(str(exc))
            logging.debug("ZIP download failed for branch %s: %s", branch, exc)
            continue

    # If we failed for all branches
    raise RuntimeError(f"Failed to download zip for {repo_url}; errors: {errors}")


# --- Project venv helpers -------------------------------------------------


def find_project_venv(root: Path) -> Optional[Path]:
    """Find a project venv directory under the given root (or VIRTUAL_ENV).

    Checks, in order: $VIRTUAL_ENV, <root>/.venv, <root>/venv
    Returns the Path to the venv dir if it looks valid, else None.
    """
    candidates = []
    try:
        venv_env = os.environ.get("VIRTUAL_ENV")
        if venv_env:
            candidates.append(Path(venv_env))
    except Exception:
        pass
    candidates.extend([root / ".venv", root / "venv"])  # order matters: prefer .venv
    for c in candidates:
        try:
            if c and c.exists():
                py = get_python_in_venv(c)
                if py is not None:
                    return c
        except Exception:
            continue
    return None


def maybe_reexec_under_project_venv(
    root: Path,
    disable: bool = False,
    extra_argv: Sequence[str] | None = None,
) -> None:
    """If a project venv exists and we are not already running under it, re-exec
    the current script using that venv's python interpreter.

    This makes the script "use the project venv by default" when present.
    """
    if disable:
        return
    # Avoid infinite re-exec loops
    if os.environ.get("HYDRUSNETWORK_REEXEC") == "1":
        return

    try:
        venv_dir = find_project_venv(root)
        if not venv_dir:
            return
        py = get_python_in_venv(venv_dir)
        if not py:
            return

        current = Path(sys.executable)
        try:
            # If current interpreter is the same as venv's python, skip.
            if current.resolve() == py.resolve():
                return
        except Exception:
            pass

        logging.info("Re-executing under project venv: %s", py)
        env = os.environ.copy()
        env["HYDRUSNETWORK_REEXEC"] = "1"
        # Use absolute script path to avoid any relative path quirks.
        try:
            script_path = Path(sys.argv[0]).resolve()
        except Exception:
            script_path = None
        args = [
            str(py),
            str(script_path) if script_path is not None else sys.argv[0]
        ] + sys.argv[1:]
        if extra_argv:
            args += list(extra_argv)
        logging.debug("Exec args: %s", args)
        os.execvpe(str(py), args, env)
    except Exception as exc:
        logging.debug("Failed to re-exec under project venv: %s", exc)
        return


# --- Permissions helpers -------------------------------------------------


def is_elevated() -> bool:
    """Return True if the current process is elevated (Windows) or running as root (Unix)."""
    try:
        if os.name == "nt":
            import ctypes

            try:
                return bool(ctypes.windll.shell32.IsUserAnAdmin())
            except Exception:
                return False
        else:
            try:
                # Use getattr for platform-specific os methods to satisfy Mypy
                geteuid = getattr(os, "geteuid", None)
                if geteuid:
                    return bool(geteuid() == 0)
                return False
            except Exception:
                return False
    except Exception:
        return False


def fix_permissions_windows(path: Path, user: Optional[str] = None) -> bool:
    """Attempt to set owner and grant FullControl via icacls/takeown.

    Returns True if commands report success; otherwise False. May require elevation.
    """
    import getpass
    import subprocess

    try:
        if not user:
            try:
                who = subprocess.check_output(["whoami"], text=True).strip()
                user = who or getpass.getuser()
            except Exception:
                user = getpass.getuser()

        logging.info(
            "Attempting Windows ownership/ACL fix for %s (owner=%s)",
            path,
            user
        )

        # Try to take ownership (best-effort)
        try:
            subprocess.run(
                ["takeown",
                 "/F",
                 str(path),
                 "/R",
                 "/D",
                 "Y"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

        rc_setowner = 1
        rc_grant = 1
        try:
            out = subprocess.run(
                ["icacls",
                 str(path),
                 "/setowner",
                 user,
                 "/T",
                 "/C"],
                check=False
            )
            rc_setowner = int(out.returncode)
        except Exception:
            rc_setowner = 1

        try:
            out = subprocess.run(
                ["icacls",
                 str(path),
                 "/grant",
                 f"{user}:(OI)(CI)F",
                 "/T",
                 "/C"],
                check=False
            )
            rc_grant = int(out.returncode)
        except Exception:
            rc_grant = 1

        success = rc_setowner == 0 or rc_grant == 0
        if success:
            logging.info("Windows permission fix succeeded (owner/grant applied).")
        else:
            logging.warning(
                "Windows permission fix did not fully succeed (setowner/grant may require elevation)."
            )
        return success
    except Exception as exc:
        logging.debug("Windows fix-permissions error: %s", exc)
        return False


def fix_permissions_unix(
    path: Path,
    user: Optional[str] = None,
    group: Optional[str] = None
) -> bool:
    """Attempt to chown/chmod recursively for a Unix-like system.

    Returns True if operations were attempted; may still fail for some files if not root.
    """
    import getpass
    import pwd
    import grp
    import subprocess

    try:
        if not user:
            user = getpass.getuser()

        try:
            pw = pwd.getpwnam(user)  # type: ignore[attr-defined]
            uid = pw.pw_uid
            gid = pw.pw_gid if not group else grp.getgrnam(group).gr_gid  # type: ignore[attr-defined]
        except Exception:
            logging.warning("Could not resolve user/group to uid/gid; skipping chown.")
            return False

        logging.info(
            "Attempting to chown recursively to %s:%s (may require root)...",
            user,
            group or pw.pw_gid,
        )

        try:
            subprocess.run(
                ["chown",
                 "-R",
                 f"{user}:{group or pw.pw_gid}",
                 str(path)],
                check=True
            )
        except Exception:
            # Best-effort fallback: chown/chmod individual entries
            for root_dir, dirs, files in os.walk(path):
                if hasattr(os, "chown"):
                    try:
                        os.chown(root_dir, uid, gid)
                    except Exception:
                        pass
                for fn in files:
                    fpath = os.path.join(root_dir, fn)
                    if hasattr(os, "chown"):
                        try:
                            os.chown(fpath, uid, gid)
                        except Exception:
                            pass

        # Fix modes: directories 0o755, files 0o644 (best-effort)
        for root_dir, dirs, files in os.walk(path):
            for d in dirs:
                try:
                    os.chmod(os.path.join(root_dir, d), 0o755)
                except Exception:
                    pass
            for f in files:
                try:
                    os.chmod(os.path.join(root_dir, f), 0o644)
                except Exception:
                    pass

        logging.info(
            "Unix permission fix attempted (some changes may require root privilege)."
        )
        return True
    except Exception as exc:
        logging.debug("Unix fix-permissions error: %s", exc)
        return False


def fix_permissions(
    path: Path,
    user: Optional[str] = None,
    group: Optional[str] = None
) -> bool:
    try:
        if os.name == "nt":
            return fix_permissions_windows(path, user=user)
        else:
            return fix_permissions_unix(path, user=user, group=group)
    except Exception as exc:
        logging.debug("General fix-permissions error: %s", exc)
        return False


IMPORT_NAME_OVERRIDES = {
    "pyyaml": "yaml",
    "pillow": "PIL",
    "python-dateutil": "dateutil",
    "beautifulsoup4": "bs4",
    "pillow-heif": "pillow_heif",
    "pillow-jxl-plugin": "pillow_jxl",
    "pyopenssl": "OpenSSL",
    "pysocks": "socks",
    "service-identity": "service_identity",
    "show-in-file-manager": "showinfm",
    "opencv-python-headless": "cv2",
    "mpv": "mpv",
    "pyside6": "PySide6",
    "pyside6-essentials": "PySide6",
    "pyside6-addons": "PySide6",
}


def normalize_python_command(python_cmd: Optional[Sequence[str] | str]) -> list[str]:
    if python_cmd is None:
        return [sys.executable]
    if isinstance(python_cmd, (str, Path)):
        return [str(python_cmd)]
    return [str(part) for part in python_cmd]


def get_python_version_info(python_cmd: Optional[Sequence[str] | str] = None) -> Optional[tuple[int, int, int]]:
    cmd = normalize_python_command(python_cmd)
    try:
        result = subprocess.run(
            cmd + [
                "-c",
                "import sys; print('.'.join(str(part) for part in sys.version_info[:3]))",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True,
        )
        version_text = (result.stdout or "").strip()
        major, minor, micro = version_text.split(".", 2)
        return (int(major), int(minor), int(micro))
    except Exception:
        return None


def select_hydrus_python_command() -> tuple[list[str], tuple[int, int, int]]:
    candidates: list[list[str]] = []

    if os.name == "nt" and shutil.which("py"):
        for version in ("3.12", "3.11", "3.10", "3.13", "3.14"):
            candidates.append(["py", f"-{version}"])

    candidates.append([sys.executable])

    for name in ("python3.12", "python3.11", "python3.10", "python3", "python"):
        resolved = shutil.which(name)
        if resolved:
            candidates.append([resolved])

    seen: set[tuple[str, ...]] = set()
    best_fallback: Optional[tuple[list[str], tuple[int, int, int]]] = None

    for candidate in candidates:
        normalized = tuple(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)

        version = get_python_version_info(candidate)
        if version is None or version < (3, 10):
            continue

        if version < (3, 13):
            return candidate, version

        if best_fallback is None or version < best_fallback[1]:
            best_fallback = (candidate, version)

    if best_fallback is not None:
        return best_fallback

    version = get_python_version_info([sys.executable])
    if version is None:
        raise RuntimeError("Could not determine a usable Python interpreter for Hydrus")
    return [sys.executable], version


def ensure_repo_venv(
    repo_root: Path,
    venv_name: str = ".venv",
    recreate: bool = False,
    purpose: Optional[str] = None,
) -> Path:
    python_cmd: list[str]
    python_version: Optional[tuple[int, int, int]]
    if is_hydrus_repo(repo_root):
        python_cmd, python_version = select_hydrus_python_command()
        if python_version is not None:
            logging.info(
                "Using Python %s.%s.%s for Hydrus venv creation via %s",
                python_version[0],
                python_version[1],
                python_version[2],
                " ".join(python_cmd),
            )
            if python_version >= (3, 13):
                logging.info(
                    "Hydrus is running on a newer Python; installer will apply compatibility package overrides."
                )
    else:
        python_cmd = [sys.executable]

    venv_dir = repo_root / str(venv_name)
    if venv_dir.exists():
        if recreate:
            logging.info("Removing existing venv: %s", venv_dir)
            shutil.rmtree(venv_dir)
        else:
            logging.info("Using existing venv at %s", venv_dir)

    if not venv_dir.exists():
        if purpose:
            logging.info("Creating venv at %s for %s", venv_dir, purpose)
        else:
            logging.info("Creating venv at %s", venv_dir)
        subprocess.run(python_cmd + ["-m", "venv", str(venv_dir)], check=True)

    venv_py = get_python_in_venv(venv_dir)
    if not venv_py:
        raise RuntimeError(f"Could not locate python in venv {venv_dir}")

    logging.info("Venv ready: %s", venv_py)
    return venv_py


def install_requirements_into_venv(
    venv_py: Path,
    repo_root: Path,
    req_path: Optional[Path],
    reinstall: bool = False,
) -> None:
    python_version = get_python_version_info([str(venv_py)])
    if python_version is None:
        raise RuntimeError(f"Could not determine python version for {venv_py}")

    if req_path and req_path.exists():
        logging.info(
            "Installing dependencies from %s into venv (reinstall=%s)",
            req_path,
            bool(reinstall),
        )
    elif can_pip_install_project(repo_root):
        logging.info(
            "No requirements.txt found; installing hydrus project from %s using 'pip install .' (reinstall=%s)",
            repo_root,
            bool(reinstall),
        )
    else:
        logging.info(
            "No requirements.txt or pyproject.toml/setup.py found in %s; skipping dependency installation.",
            repo_root,
        )
        return

    subprocess.run(
        [
            str(venv_py),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--upgrade",
            "pip",
        ],
        check=True,
    )

    cmd = [
        str(venv_py),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--prefer-binary",
    ]
    if reinstall:
        cmd.extend(["--upgrade", "--force-reinstall"])
    cmd.extend(build_hydrus_install_targets(req_path, repo_root, python_version))
    subprocess.run(cmd, cwd=str(repo_root), check=True)
    logging.info("Dependencies installed successfully")


def verify_requirements_in_venv(venv_py: Path, req_path: Optional[Path]) -> bool:
    if req_path is None or not req_path.exists():
        logging.info("No requirements.txt found; skipping import verification, running pip check only.")
        pip_check = subprocess.run(
            [str(venv_py), "-m", "pip", "check"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if pip_check.returncode != 0:
            output = (pip_check.stdout or "").strip() or "Unknown dependency issue"
            logging.warning("pip check reported issues:\n%s", output)
            return False
        return True

    packages = parse_requirements_file(req_path)
    if not packages:
        logging.debug(
            "No parseable packages found in %s for verification; skipping further checks",
            req_path,
        )
        return True

    logging.info("Running pip consistency check inside the venv...")
    pip_check = subprocess.run(
        [str(venv_py), "-m", "pip", "check"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if pip_check.returncode != 0:
        output = (pip_check.stdout or "").strip() or "Unknown dependency issue"
        logging.warning("pip check reported issues:\n%s", output)

    seen_modules: set[str] = set()
    targets: list[tuple[str, str]] = []
    for package in packages:
        module_name = IMPORT_NAME_OVERRIDES.get(package, package)
        if module_name in seen_modules:
            continue
        seen_modules.add(module_name)
        targets.append((package, module_name))

    verify_script = (
        "import importlib, json\n"
        f"targets = {targets!r}\n"
        "failures = []\n"
        "for package, module_name in targets:\n"
        "    try:\n"
        "        importlib.import_module(module_name)\n"
        "    except Exception as exc:\n"
        "        failures.append((package, module_name, f'{type(exc).__name__}: {exc}'))\n"
        "print(json.dumps(failures))\n"
        "raise SystemExit(1 if failures else 0)\n"
    )
    result = subprocess.run(
        [str(venv_py), "-c", verify_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    failures: list[tuple[str, str, str]] = []
    raw_output = (result.stdout or "").strip()
    if raw_output:
        try:
            decoded = json.loads(raw_output)
            failures = [tuple(item) for item in decoded]
        except json.JSONDecodeError:
            logging.warning(
                "Dependency import verification returned unexpected output: %s",
                raw_output,
            )

    any_missing = False
    for package, module_name, error in failures:
        if module_name == "mpv":
            logging.info(
                "Package '%s' is installed, but import '%s' failed (likely missing system libmpv). This is usually non-critical.",
                package,
                module_name,
            )
            continue

        logging.warning(
            "Package '%s' appears installed but import '%s' failed inside venv: %s",
            package,
            module_name,
            error,
        )
        any_missing = True

    if result.returncode != 0 and not failures:
        stderr_text = (result.stderr or "").strip()
        if stderr_text:
            logging.warning("Dependency import verification failed: %s", stderr_text)
        any_missing = True

    if pip_check.returncode == 0 and not any_missing:
        logging.info("Dependency verification completed successfully")
        return True

    logging.warning(
        "Some dependencies may not be importable in the venv; consider running with --reinstall-deps"
    )
    return False


def open_in_editor(path: Path) -> bool:
    """Open the file using the OS default opener.

    Uses:
    - Windows: os.startfile
    - macOS: open
    - Linux: xdg-open (only if DISPLAY or WAYLAND_DISPLAY is present)

    Returns True if an opener was invoked (success is best-effort).
    """
    import shutil
    import subprocess
    import os
    import sys

    try:
        # Windows: use os.startfile when available
        if os.name == "nt":
            try:
                os.startfile(str(path))
                logging.info("Opened %s with default application", path)
                return True
            except Exception:
                pass

        # macOS: use open
        if sys.platform == "darwin":
            try:
                subprocess.run(["open", str(path)], check=False)
                logging.info("Opened %s with default application", path)
                return True
            except Exception:
                pass

        # Linux: use xdg-open only if a display is available and xdg-open exists
        if shutil.which("xdg-open") and (os.environ.get("DISPLAY")
                                         or os.environ.get("WAYLAND_DISPLAY")):
            try:
                subprocess.run(["xdg-open", str(path)], check=False)
                logging.info("Opened %s with default application", path)
                return True
            except Exception:
                pass

        logging.debug(
            "No available method to open %s automatically (headless or no opener installed)",
            path
        )
        return False
    except Exception as exc:
        logging.debug("open_in_editor failed for %s: %s", path, exc)
        return False


def _update_git_dest(git: Optional[str], dest: Path) -> int:
    if not git:
        logging.error("Git not found; cannot --update without git")
        return 2
    try:
        run_git_pull(git, dest)
        logging.info("Updated repository in %s", dest)
        return 0
    except subprocess.CalledProcessError as e:
        logging.error("git pull failed: %s", e)
        return 3


def _prompt_existing_install_action(dest: Path, *, can_update: bool) -> str:
    logging.info("Destination %s already exists.", dest)
    print("")
    print("Select an action:")
    print("  1) Continue setup (venv and dependencies)")
    if can_update:
        print("  2) Update hydrus (git pull) then continue setup")
    print("  3) Continue setup and install service")
    print("  4) Re-clone (remove and re-clone the repository)")
    print("  0) Do nothing / exit")
    try:
        choice = (input("Enter choice [0-4]: ") or "").strip()
    except Exception:
        logging.info("No interactive input available; exiting.")
        return "abort"
    if choice == "1":
        return "reuse"
    if choice == "2":
        return "update" if can_update else "abort"
    if choice == "3":
        return "service"
    if choice == "4":
        return "reclone"
    logging.info("No valid choice selected; exiting.")
    return "abort"


def _dir_nonempty(path: Path) -> bool:
    try:
        return any(path.iterdir())
    except OSError:
        return True


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clone Hydrus into a 'hydrusnetwork' directory."
    )
    parser.add_argument(
        "--root",
        "-r",
        default=".",
        help=
        "Root folder to create the hydrusnetwork directory in (default: current working directory)",
    )
    parser.add_argument(
        "--dest-name",
        "-d",
        default="hydrusnetwork",
        help="Name of the destination folder (default: hydrusnetwork)",
    )
    parser.add_argument(
        "--repo",
        default="https://github.com/hydrusnetwork/hydrus",
        help="Repository URL to clone"
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="If dest exists and is a git repo, run git pull instead of cloning",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Remove existing destination directory before cloning",
    )
    parser.add_argument(
        "--branch",
        "-b",
        default=None,
        help="Branch to clone (passed to git clone --branch)."
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=1,
        help=
        "If set, pass --depth to git clone (default: 1 for a shallow clone). Use --full to perform a full clone instead.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Perform a full clone (no --depth passed to git clone)"
    )
    group_obtain = parser.add_mutually_exclusive_group()
    group_obtain.add_argument(
        "--git",
        dest="git",
        action="store_true",
        help="Use git clone (shallow by default) to allow updates. This is the default.",
    )
    group_obtain.add_argument(
        "--no-git",
        dest="git",
        action="store_false",
        help="Use ZIP download instead of git clone (no git pull support).",
    )
    parser.set_defaults(git=True)
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help=
        "If set, do not attempt to download ZIP when git is missing (only relevant with --git)",
    )
    parser.add_argument(
        "--fix-permissions",
        action="store_true",
        help=
        "Fix ownership/permissions on the obtained repo (OS-aware). Requires elevated privileges for some actions.",
    )
    parser.add_argument(
        "--fix-permissions-user",
        default=None,
        help="User to set as owner when fixing permissions (defaults to current user).",
    )
    parser.add_argument(
        "--fix-permissions-group",
        default=None,
        help="Group to set when fixing permissions (Unix only).",
    )
    parser.add_argument(
        "--no-venv",
        action="store_true",
        help=
        "Do not create a venv inside the cloned repo (default: create a .venv folder)",
    )
    parser.add_argument(
        "--venv-name",
        default=".venv",
        help="Name of the venv directory to create inside the repo (default: .venv)",
    )
    parser.add_argument(
        "--recreate-venv",
        action="store_true",
        help="Remove existing venv and create a fresh one"
    )
    # By default install dependencies into the created venv; use --no-install-deps to opt out
    group_install = parser.add_mutually_exclusive_group()
    group_install.add_argument(
        "--install-deps",
        dest="install_deps",
        action="store_true",
        help=
        "Install dependencies from requirements.txt into the created venv (default).",
    )
    group_install.add_argument(
        "--no-install-deps",
        dest="install_deps",
        action="store_false",
        help="Do not install dependencies from requirements.txt into the created venv.",
    )
    parser.set_defaults(install_deps=True)
    parser.add_argument(
        "--reinstall-deps",
        action="store_true",
        help=
        "If present, force re-install dependencies into the created venv using pip --force-reinstall.",
    )
    parser.add_argument(
        "--no-open-client",
        action="store_true",
        help=
        "(ignored) installer no longer opens hydrus_client.py automatically; use the run_client helper to launch the client when ready.",
    )
    parser.add_argument(
        "--run-client",
        action="store_true",
        help=
        "Run hydrus_client.py using the repo-local venv's Python (if present). This runs the client in the foreground unless --run-client-detached is specified.",
    )
    parser.add_argument(
        "--run-client-detached",
        action="store_true",
        help="Start hydrus_client.py and do not wait for it to exit (detached).",
    )
    parser.add_argument(
        "--run-client-headless",
        action="store_true",
        help=
        "If used with --run-client, attempt to run hydrus_client.py without showing the Qt GUI (best-effort)",
    )
    parser.add_argument(
        "--install-service",
        action="store_true",
        help="Register the hydrus client to start on boot (user-level).",
    )
    parser.add_argument(
        "--uninstall-service",
        action="store_true",
        help="Remove a registered start-on-boot service for the hydrus client.",
    )
    parser.add_argument(
        "--service-name",
        default="hydrus-client",
        help="Name for the installed service/scheduled task (default: hydrus-client)",
    )
    parser.add_argument(
        "--service-user",
        default=None,
        help="When installing a systemd system service as root, run it under this user",
    )
    parser.add_argument(
        "--no-project-venv",
        action="store_true",
        help="Do not attempt to re-exec the script under a project venv (if present)",
    )
    parser.add_argument(
        "--use-project-venv",
        action="store_true",
        help="Force using the project venv even when running interactively",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Force interactive setup even if root/name are provided or no TTY is detected",
    )

    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Interactive setup for root and name if not provided and in a TTY
    # We check sys.argv directly to see if the flags were explicitly passed.
    interactive_setup = False
    wants_interactive = args.interactive or (
        sys.stdin.isatty() and not any(arg in sys.argv for arg in ["--root", "-r", "--dest-name", "-d"])
    )
    if wants_interactive:
        print("\nHydrusNetwork Setup")
        print("--------------------")
        
        # Ask for root path
        default_root = Path.cwd()
        try:
            print(f"Current directory: {default_root}")
            root_input = input(f"Enter root directory for Hydrus installation [default: {default_root}]: ").strip()
            if root_input:
                # If they typed "C:" or similar, assume they want the root "C:\"
                if len(root_input) == 2 and root_input[1] == ":" and root_input[0].isalpha():
                    root_input += "\\"
                args.root = root_input
            else:
                args.root = str(default_root)
                
            # Ask for destination folder name
            dest_input = input("Enter folder name for Hydrus [default: hydrusnetwork]: ").strip()
            if dest_input:
                args.dest_name = dest_input
        except (EOFError, KeyboardInterrupt):
            print("\nSetup cancelled.")
            return 0
        interactive_setup = True

    # Expand variables like $HOME or %USERPROFILE% and ~
    args.root = os.path.expandvars(args.root)
    root = Path(args.root).expanduser().resolve()
    # Python executable inside the repo venv (set when we create/find the venv)
    venv_py = None
    # Re-exec under project venv by default when present (opt-out with --no-project-venv)
    try:
        # If we are already running in a venv-like environment, we might skip re-exec.
        # However, we only re-exec if the target root is the same as the project root.
        disable_reexec = bool(args.no_project_venv)
        
        # Don't re-exec when running interactively unless explicitly requested.
        if interactive_setup and not args.use_project_venv:
            disable_reexec = True
            
        current_repo_root = Path(__file__).resolve().parent.parent
        # Only re-exec if the target root folder matches the folder where THIS script lives.
        # This prevents picking up Medios-Macina's .venv when installing Hydrus to a separate drive/folder.
        if root != current_repo_root and not args.use_project_venv:
            disable_reexec = True

        maybe_reexec_under_project_venv(
            root,
            disable=disable_reexec,
            extra_argv=["--root", args.root, "--dest-name", args.dest_name],
        )
    except Exception:
        pass

    args.dest_name = os.path.expandvars(args.dest_name)
    dest = root / args.dest_name

    try:
        git = find_git_executable()
        obtained = False
        obtained_by: Optional[str] = None

        if dest.exists() and args.force:
            logging.info("Removing existing directory: %s", dest)
            shutil.rmtree(dest)

        if dest.exists() and is_hydrus_repo(dest):
            action = "reuse"
            can_update = bool(git and is_git_repo(dest))
            if args.update:
                action = "update"
            elif interactive_setup:
                action = _prompt_existing_install_action(dest, can_update=can_update)

            if action == "abort":
                return 0
            if action == "reclone":
                logging.info("Removing existing directory: %s", dest)
                shutil.rmtree(dest)
            else:
                if action == "update":
                    rc = _update_git_dest(git, dest)
                    if rc:
                        return rc
                if action == "service":
                    args.install_service = True
                logging.info("Using existing Hydrus install at %s", dest)
                obtained = True
                obtained_by = "existing"
        elif dest.exists() and _dir_nonempty(dest):
            overwrite = False
            if sys.stdin and sys.stdin.isatty():
                print(f"\nDestination {dest} already exists and is not a Hydrus repository.")
                try:
                    overwrite = input("Overwrite it? [y/N]: ").strip().lower() in {
                        "y",
                        "yes",
                    }
                except Exception:
                    overwrite = False
                if not overwrite:
                    print("Aborted.")
                    return 0
            if overwrite:
                logging.info("Removing existing directory: %s", dest)
                shutil.rmtree(dest)
            else:
                logging.error(
                    "Destination %s already exists and is not empty. Use --force to overwrite.",
                    dest,
                )
                return 4

        dest.parent.mkdir(parents=True, exist_ok=True)

        if not obtained and args.git:
            if git:
                try:
                    depth_to_use = None if getattr(args, "full", False) else args.depth
                    run_git_clone(
                        git,
                        args.repo,
                        dest,
                        branch=args.branch,
                        depth=depth_to_use
                    )
                    logging.info("Repository cloned into %s", dest)
                    obtained = True
                    obtained_by = "git"
                except subprocess.CalledProcessError as e:
                    logging.error("git clone failed: %s", e)
                    if args.no_fallback:
                        return 5
                    logging.info("Git clone failed; falling back to ZIP download...")
            else:
                logging.info("Git not found; falling back to ZIP download...")

        if not obtained:
            try:
                download_and_extract_zip(args.repo, dest)
                logging.info("Repository downloaded and extracted into %s", dest)
                obtained = True
                obtained_by = "zip"
            except Exception as exc:
                logging.error("Failed to obtain repository (ZIP): %s", exc)
                return 7

        # Auto-link to Medios-Macina if possible
        if obtained:
            update_medios_config(dest)

        # Post-obtain setup: create repository-local venv (unless disabled)
        if not getattr(args, "no_venv", False):
            try:
                venv_py = ensure_repo_venv(
                    dest,
                    getattr(args, "venv_name", ".venv"),
                    recreate=getattr(args, "recreate_venv", False),
                )

                # Optionally install or reinstall requirements.txt
                if getattr(args,
                           "install_deps",
                           False) or getattr(args,
                                             "reinstall_deps",
                                             False):
                    req = find_requirements(dest)
                    if req and req.exists():
                        try:
                            install_requirements_into_venv(
                                venv_py,
                                dest,
                                req,
                                reinstall=getattr(args, "reinstall_deps", False),
                            )
                        except subprocess.CalledProcessError as e:
                            logging.error("Failed to install dependencies: %s", e)
                            return 10

                        if not verify_requirements_in_venv(venv_py, req):
                            logging.warning(
                                "To re-install and verify, run:\n  %s -m pip install -r %s\nThen run the client with:\n  %s %s",
                                venv_py,
                                req,
                                venv_py,
                                dest / "hydrus_client.py",
                            )

                    elif can_pip_install_project(dest):
                        logging.info(
                            "No requirements.txt found; installing hydrus project from %s using 'pip install .'",
                            dest,
                        )
                        try:
                            install_requirements_into_venv(
                                venv_py,
                                dest,
                                None,
                                reinstall=getattr(args, "reinstall_deps", False),
                            )
                            verify_requirements_in_venv(venv_py, None)
                        except subprocess.CalledProcessError as e:
                            logging.error("Failed to install dependencies: %s", e)
                            return 10

                    else:
                        logging.info(
                            "No requirements.txt or pyproject.toml/setup.py found in %s; skipping dependency installation",
                            dest,
                        )

            except Exception as exc:
                logging.exception("Unexpected error during venv setup: %s", exc)
                return 99

        # Optionally fix permissions
        if getattr(args, "fix_permissions", False):
            logging.info("Fixing ownership/permissions for %s", dest)
            fp_user = getattr(args, "fix_permissions_user", None)
            fp_group = getattr(args, "fix_permissions_group", None)
            try:
                ok_perm = fix_permissions(dest, user=fp_user, group=fp_group)
                if not ok_perm:
                    logging.warning(
                        "Permission fix reported issues or lacked privileges; some files may remain inaccessible."
                    )
            except Exception as exc:
                logging.exception("Failed to fix permissions: %s", exc)

        logging.info("Setup complete.")

        # Optionally open/run hydrus_client.py in the repo for convenience (open by default if present).
        client_candidates = [
            dest / "hydrus_client.py",
            dest / "client" / "hydrus_client.py"
        ]
        client_found = None
        for p in client_candidates:
            if p.exists():
                client_found = p
                break

        script_dir = Path(__file__).resolve().parent
        installed_helper = ensure_run_client_helper(dest, script_dir)

        run_client_script = None
        if client_found:
            # Prefer the helper installed directly into the Hydrus repository.
            helper_candidates = [installed_helper, dest / "run_client.py", script_dir / "run_client.py"]
            for cand in helper_candidates:
                if cand and cand.exists():
                    run_client_script = cand
                    break
            if getattr(args,
                       "install_service",
                       False) or getattr(args,
                                         "uninstall_service",
                                         False):
                if not venv_py:
                    venv_dir = dest / str(getattr(args, "venv_name", ".venv"))
                    venv_py = get_python_in_venv(venv_dir)
                if not venv_py:
                    logging.error(
                        "Could not locate python in repo venv; cannot manage service."
                    )
                else:
                    if getattr(args, "install_service", False):
                        service_user = _determine_service_user(args)
                        if run_client_script and run_client_script.exists():
                            cmd = [
                                str(venv_py),
                                str(run_client_script),
                                "--install-service",
                                "--service-name",
                                args.service_name,
                                "--detached",
                                "--headless",
                            ]
                            if service_user:
                                cmd.extend(["--service-user", service_user])
                            logging.info("Installing service via helper: %s", cmd)
                            try:
                                subprocess.run(cmd, cwd=str(dest), check=True)
                                logging.info("Service installed (user-level).")
                            except subprocess.CalledProcessError as e:
                                logging.error("Service install failed: %s", e)
                        else:
                            if install_service_auto:
                                ok = install_service_auto(
                                    args.service_name,
                                    dest,
                                    venv_py,
                                    headless=True,
                                    detached=True,
                                    service_user=service_user
                                )
                                if ok:
                                    logging.info("Service installed (user-level).")
                                else:
                                    logging.error("Service install failed.")
                            else:
                                logging.error(
                                    "Service installer functions are not available in this environment. Please run '%s %s --install-service' inside the repository, or use the helper script when available.",
                                    venv_py,
                                    dest / "run_client.py",
                                )
                    if getattr(args, "uninstall_service", False):
                        if run_client_script and run_client_script.exists():
                            cmd = [
                                str(venv_py),
                                str(run_client_script),
                                "--uninstall-service",
                                "--service-name",
                                args.service_name,
                            ]
                            logging.info("Uninstalling service via helper: %s", cmd)
                            try:
                                subprocess.run(cmd, cwd=str(dest), check=True)
                                logging.info("Service removed.")
                            except subprocess.CalledProcessError as e:
                                logging.error("Service uninstall failed: %s", e)
                        else:
                            if uninstall_service_auto:
                                ok = uninstall_service_auto(
                                    args.service_name,
                                    dest,
                                    venv_py
                                )
                                if ok:
                                    logging.info("Service removed.")
                                else:
                                    logging.error("Service uninstall failed.")
                            else:
                                logging.error(
                                    "Service uninstaller functions are not available in this environment. Please run '%s %s --uninstall-service' inside the repository, or use the helper script when available.",
                                    venv_py,
                                    dest / "run_client.py",
                                )

            # If user requested to run the client, prefer running it with the repo venv python.
            if getattr(args, "run_client", False):
                if getattr(args, "no_venv", False):
                    logging.error(
                        "--run-client requested but venv creation was skipped (use --venv-name or omit --no-venv)."
                    )
                else:
                    try:
                        if not venv_py:
                            venv_dir = dest / str(getattr(args, "venv_name", ".venv"))
                            venv_py = get_python_in_venv(venv_dir)
                        if not venv_py:
                            logging.error(
                                "Could not locate python in repo venv; cannot run client."
                            )
                        else:
                            # Prefer to use the repository helper script if present; it knows how to
                            # install/verify and support headless/detached options.
                            if run_client_script and run_client_script.exists():
                                cmd = [str(venv_py), str(run_client_script)]
                                if getattr(args, "reinstall_deps", False):
                                    cmd.append("--reinstall")
                                elif getattr(args, "install_deps", False):
                                    cmd.append("--install-deps")
                                if getattr(args, "run_client_headless", False):
                                    cmd.append("--headless")
                                if getattr(args, "run_client_detached", False):
                                    cmd.append("--detached")

                                logging.info(
                                    "Running hydrus client via helper: %s",
                                    cmd
                                )
                                try:
                                    if getattr(args, "run_client_detached", False):
                                        kwargs = detach_kwargs_for_platform()
                                        kwargs.update({
                                            "cwd": str(dest)
                                        })
                                        subprocess.Popen(cmd, **kwargs)
                                        logging.info(
                                            "Hydrus client launched (detached)."
                                        )
                                    else:
                                        subprocess.run(cmd, cwd=str(dest))
                                except subprocess.CalledProcessError as e:
                                    logging.error(
                                        "run_client.py exited non-zero: %s",
                                        e
                                    )
                            else:
                                # Fallback: call the client directly; support headless by setting
                                # QT_QPA_PLATFORM or using xvfb-run on Linux.
                                cmd = [str(venv_py), str(client_found)]
                                env = os.environ.copy()
                                if getattr(args, "run_client_headless", False):
                                    if os.name == "posix" and shutil.which("xvfb-run"):
                                        cmd = [
                                            "xvfb-run",
                                            "--auto-servernum",
                                            "--server-args=-screen 0 1024x768x24",
                                        ] + cmd
                                        logging.info(
                                            "Headless: using xvfb-run to provide a virtual X server"
                                        )
                                    else:
                                        env["QT_QPA_PLATFORM"] = "offscreen"
                                        logging.info(
                                            "Headless: setting QT_QPA_PLATFORM=offscreen (best-effort)"
                                        )

                                logging.info(
                                    "Running hydrus client with %s: %s",
                                    venv_py,
                                    client_found
                                )
                                if getattr(args, "run_client_detached", False):
                                    try:
                                        kwargs = detach_kwargs_for_platform()
                                        kwargs.update({
                                            "cwd": str(dest),
                                            "env": env
                                        })
                                        subprocess.Popen(cmd, **kwargs)
                                        logging.info(
                                            "Hydrus client launched (detached)."
                                        )
                                    except Exception as exc:
                                        logging.exception(
                                            "Failed to launch client detached: %s",
                                            exc
                                        )
                                else:
                                    try:
                                        subprocess.run(cmd, cwd=str(dest), env=env)
                                    except subprocess.CalledProcessError as e:
                                        logging.error(
                                            "hydrus client exited non-zero: %s",
                                            e
                                        )
                    except Exception as exc:
                        logging.exception("Failed to run hydrus client: %s", exc)

            # We no longer attempt to open or auto-launch the Hydrus client at the end
            # because this can behave unpredictably in headless environments. Instead,
            # print a short instruction for the user to run it manually.
            try:
                helper = dest / "run_client.py"
                if helper.exists():
                    logging.info("To start Hydrus (headless):\n  python3 run_client.py --headless")
                else:
                    logging.info(
                        "To start Hydrus (headless):\n  QT_QPA_PLATFORM=offscreen %s %s",
                        venv_py or "python3",
                        dest / "hydrus_client.py",
                    )
            except Exception:
                pass
        else:
            logging.debug(
                "No hydrus_client.py found to open or run (looked in %s).",
                client_candidates
            )

        return 0

    except Exception as exc:  # pragma: no cover - defensive
        logging.exception("Unexpected error: %s", exc)
        return 99


def ensure_run_client_helper(dest: Path, script_dir: Path) -> Optional[Path]:
    """Ensure the run_client helper is installed inside the target repository."""
    helper_src = script_dir / "run_client.py"
    if not helper_src.exists():
        logging.debug(
            "run_client helper not found in %s; skipping copy.",
            helper_src,
        )
        return None

    helper_dest = dest / "run_client.py"
    try:
        shutil.copy2(helper_src, helper_dest)
        if os.name != "nt":
            helper_dest.chmod(helper_dest.stat().st_mode | 0o111)
        logging.debug("Installed run_client helper to %s", helper_dest)
        return helper_dest
    except Exception as exc:
        logging.debug("Failed to copy run_client helper: %s", exc)
        return None


if __name__ == "__main__":
    raise SystemExit(main())
