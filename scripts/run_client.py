#!/usr/bin/env python3
"""Run the Hydrus client (top-level helper)

This standalone helper is intended to live in the project's top-level `scripts/`
folder so it remains available even if the Hydrus repository subfolder is not
present or its copy of this helper gets removed.

Features (subset of the repo helper):
- Locate repository venv (default: <workspace>/hydrusnetwork/.venv)
- Install or reinstall scripts/requirements.txt into the venv
- Verify key imports
- Launch hydrus_client.py (foreground or detached)
- Install/uninstall simple user-level start-on-boot services (schtasks/systemd/crontab)

Usage examples:
  python scripts/run_client.py --verify
  python scripts/run_client.py --detached --headless
  python scripts/run_client.py --install-deps --verify

"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

try:
    import pwd
except ImportError:
    pwd = None


def get_python_in_venv(venv_dir: Path) -> Optional[Path]:
    try:
        v = Path(venv_dir)
        # Windows
        win_python = v / "Scripts" / "python.exe"
        if win_python.exists():
            return win_python
        # Unix
        unix_python = v / "bin" / "python"
        if unix_python.exists():
            return unix_python
        unix_py3 = v / "bin" / "python3"
        if unix_py3.exists():
            return unix_py3
    except Exception:
        pass
    return None


def find_requirements(root: Path) -> Optional[Path]:
    candidates = [root / "scripts" / "requirements.txt", root / "requirements.txt", root / "client" / "requirements.txt"]
    for c in candidates:
        if c.exists():
            return c
    # shallow two-level search
    try:
        for p in root.iterdir():
            if not p.is_dir():
                continue
            for child in (p,
                          ):
                candidate = child / "requirements.txt"
                if candidate.exists():
                    return candidate
    except Exception:
        pass
    return None


def can_pip_install_project(root: Path) -> bool:
    return (
        (root / "pyproject.toml").exists()
        or (root / "setup.py").exists()
        or (root / "setup.cfg").exists()
    )


def install_requirements(
    venv_py: Path,
    repo_root: Path,
    req_path: Optional[Path],
    reinstall: bool = False,
    upgrade: bool = False
) -> bool:
    try:
        # Suppression flag for Windows
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000

        if req_path and req_path.exists():
            print(f"Installing/Updating {req_path} into venv ({venv_py})...")
        elif can_pip_install_project(repo_root):
            print(f"No requirements.txt found; installing hydrus project from {repo_root} using 'pip install .'...")
        else:
            print(f"No requirements.txt or pyproject.toml/setup.py found in {repo_root}; skipping dependency installation.")
            return True

        subprocess.run(
            [str(venv_py),
             "-m",
             "pip",
             "install",
             "--upgrade",
             "pip"],
            check=True,
            **kwargs
        )
        python_version = get_python_version_info(venv_py)
        install_cmd = [str(venv_py), "-m", "pip", "install", "--prefer-binary"]
        if upgrade:
            install_cmd.append("--upgrade")
        if reinstall:
            install_cmd.extend(["--upgrade", "--force-reinstall"])
        install_cmd.extend(build_hydrus_install_targets(req_path, repo_root, python_version))
        subprocess.run(install_cmd, check=True, **kwargs)
        return True
    except subprocess.CalledProcessError as e:
        print("Failed to install requirements:", e)
        return False


def parse_requirements_file(req_path: Path) -> List[str]:
    names: List[str] = []
    try:
        with req_path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("-e") or line.startswith("--"):
                    continue
                if "://" in line or line.startswith("file:"):
                    continue
                line = line.split(";")[0].strip()
                line = line.split("[")[0].strip()
                for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", "==="):
                    if sep in line:
                        line = line.split(sep)[0].strip()
                if " @ " in line:
                    line = line.split(" @ ")[0].strip()
                if line:
                    names.append(line.split()[0].strip().lower())
    except Exception:
        pass
    return names


def get_python_version_info(python_exe: Path) -> Optional[tuple[int, int, int]]:
    try:
        result = subprocess.run(
            [
                str(python_exe),
                "-c",
                "import sys; print('.'.join(str(part) for part in sys.version_info[:3]))",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True,
            timeout=10,
        )
        version_text = (result.stdout or "").strip()
        major, minor, micro = version_text.split(".", 2)
        return int(major), int(minor), int(micro)
    except Exception:
        return None


def is_hydrus_repo(repo_root: Path) -> bool:
    return (repo_root / "setup_venv.py").exists() and (repo_root / "hydrus_client.py").exists()


def build_hydrus_install_targets(
    req_path: Optional[Path],
    repo_root: Path,
    python_version: Optional[tuple[int, int, int]],
) -> List[str]:
    if req_path is None or not req_path.exists():
        if can_pip_install_project(repo_root):
            return ["."]
        return []

    if python_version is None or python_version < (3, 13) or not is_hydrus_repo(repo_root):
        return ["-r", str(req_path)]

    overrides = {
        "pyside6": "PySide6==6.10.1",
        "qtpy": "QtPy==2.4.3",
        "opencv-python-headless": "opencv-python-headless==4.13.0.90",
        "numpy": "numpy==2.4.1",
    }
    targets: List[str] = []
    seen_packages = set()

    for raw_line in req_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        package_name = re.split(r"[<>=!~\[]", line, maxsplit=1)[0].strip().lower()
        if package_name in overrides:
            line = overrides[package_name]

        targets.append(line)
        seen_packages.add(package_name)

    if os.name == "nt" and "pywin32" not in seen_packages:
        targets.append("pywin32")

    print(
        f"Using Hydrus compatibility dependency set for Python {python_version[0]}.{python_version[1]}.{python_version[2]}"
    )
    return targets


def verify_imports(venv_py: Path, packages: List[str]) -> bool:
    # Skip mpv check as it is problematic to install and causes slow startups
    packages = [p for p in packages if p.lower() != "mpv"]

    # Map some package names to import names (handle common cases where package name differs from import name)
    import_map = {
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
        "pyside6": "PySide6",
    }
    
    # Helper for silent subprocess execution on Windows
    def _run_silent(cmd, **kwargs):
        if os.name == "nt":
            # 0x08000000 = CREATE_NO_WINDOW
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x08000000
        return subprocess.run(cmd, **kwargs)

    missing = []
    for pkg in packages:
        try:
            out = _run_silent(
                [str(venv_py),
                 "-m",
                 "pip",
                 "show",
                 pkg],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            missing.append(pkg)
            continue
        except Exception:
            missing.append(pkg)
            continue

        if out.returncode != 0 or not out.stdout.strip():
            missing.append(pkg)
            continue

        import_name = import_map.get(pkg, pkg)
        try:
            _run_silent(
                [str(venv_py),
                 "-c",
                 f"import {import_name}"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            missing.append(pkg)
    if missing:
        print(
            "The following packages were not importable in the venv:",
            ", ".join(missing)
        )
        return False
    return True


def is_first_run(repo_root: Path) -> bool:
    try:
        db_dir = repo_root / "db"
        if db_dir.exists() and any(db_dir.iterdir()):
            return False
        for f in repo_root.glob("*.db"):
            if f.exists():
                return False
    except Exception:
        return False
    return True


def _user_exists(username: str) -> bool:
    if pwd is None:
        return False
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def ensure_service_user(username: str) -> bool:
    if not username:
        return False
    if _user_exists(username):
        return True
    useradd = shutil.which("useradd")
    if not useradd:
        print("useradd not found; cannot create service user.")
        return False
    shell = "/usr/sbin/nologin" if Path("/usr/sbin/nologin").exists() else "/bin/false"
    cmd = [
        useradd,
        "--system",
        "--no-create-home",
        "--shell",
        shell,
        username,
    ]
    try:
        subprocess.run(cmd, check=True)
        print(f"Created service user '{username}'.")
        return True
    except FileNotFoundError:
        print("useradd executable missing; cannot create service user.")
        return False
    except subprocess.CalledProcessError as exc:
        print(f"Failed to create service user '{username}': {exc}")
        return False


def grant_service_user_repo_access(repo_root: Path, username: str) -> bool:
    try:
        for dirpath, dirnames, filenames in os.walk(repo_root):
            shutil.chown(dirpath, user=username, group=username)
            for name in dirnames + filenames:
                path = Path(dirpath) / name
                shutil.chown(path, user=username, group=username)
        return True
    except PermissionError as exc:
        print(
            f"Failed to grant ownership of '{repo_root}' to '{username}': {exc}"
        )
        return False
    except Exception as exc:
        print(
            f"Error while adjusting permissions for '{username}' in '{repo_root}': {exc}"
        )
        return False


# --- Service install/uninstall helpers -----------------------------------


def install_service_windows(
    service_name: str,
    repo_root: Path,
    venv_py: Path,
    headless: bool = True,
    detached: bool = True,
    start_on: str = "logon",
    pull: bool = False,
    workspace_root: Optional[Path] = None,
) -> bool:
    try:
        schtasks = shutil.which("schtasks")
        if not schtasks:
            print(
                "schtasks not available on this system; cannot install Windows scheduled task."
            )
            return False

        # Use the repository root for the service wrapper script
        bat = repo_root / "run-client.bat"
        
        # If there's a local copy of run_client.py in the target repo, use that instead
        # of the one from Medios-Macina to keep the service independent.
        local_helper = repo_root / "run_client.py"
        if not local_helper.exists():
            local_helper = repo_root / "scripts" / "run_client.py"
            
        target_script = local_helper if local_helper.exists() else Path(__file__).resolve()
        
        python_exe = venv_py
        # Use pythonw.exe for windowless execution on Windows
        pythonw_exe = python_exe.parent / "pythonw.exe"
        if not pythonw_exe.exists():
            pythonw_exe = python_exe

        if not bat.exists():
            # The .bat remains using python.exe for manual/interactive runs
            content = f'@echo off\n"{python_exe}" "{target_script}" %*\n'
            bat.write_text(content, encoding="utf-8")

        sc = "ONLOGON" if start_on == "logon" else "ONSTART"
        
        # When running as a service, we DO NOT use --detached. 
        # This keeps the run_client.py process alive as a monitor for the task scheduler,
        # preventing it from thinking the task finished/crashed and trying to restart it.
        task_args = ""
        if headless: task_args += "--headless "
        if pull: task_args += "--pull "
        # Force the correct repo root for the service
        task_args += f'--repo-root "{repo_root}" '

        # Use pythonw for the task to avoid console window
        tr_command = f'"{pythonw_exe}" "{target_script}" {task_args.strip()}'
        
        cmd = [
            schtasks,
            "/Create",
            "/SC",
            sc,
            "/TN",
            service_name,
            "/TR",
            tr_command,
            "/RL",
            "LIMITED",
            "/F",
        ]
        subprocess.run(cmd, check=True)
        print(f"Scheduled task '{service_name}' created ({sc}).")
        return True
    except subprocess.CalledProcessError as e:
        print("Failed to create scheduled task:", e)
        return False
    except Exception as exc:
        print("Windows install-service error:", exc)
        return False


def uninstall_service_windows(service_name: str) -> bool:
    try:
        schtasks = shutil.which("schtasks")
        if not schtasks:
            print(
                "schtasks not available on this system; cannot remove scheduled task."
            )
            return False
        cmd = [schtasks, "/Delete", "/TN", service_name, "/F"]
        subprocess.run(cmd, check=True)
        print(f"Scheduled task '{service_name}' removed.")
        return True
    except subprocess.CalledProcessError as e:
        print("Failed to delete scheduled task:", e)
        return False
    except Exception as exc:
        print("Windows uninstall-service error:", exc)
        return False


def install_service_systemd(
    service_name: str,
    repo_root: Path,
    venv_py: Path,
    headless: bool = True,
    detached: bool = True,
    pull: bool = False,
    workspace_root: Optional[Path] = None,
    service_user: Optional[str] = None,
) -> bool:
    try:
        helper_path = Path(__file__).resolve()
        print(f"Installing systemd user service via {helper_path}...")
        print("systemctl env:", {
            "DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
            "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR"),
            "HOME": os.environ.get("HOME"),
        })
        systemctl = shutil.which("systemctl")
        if not systemctl:
            print(
                "systemctl not available; falling back to crontab @reboot (if present)."
            )
            return install_service_cron(
                service_name,
                repo_root,
                venv_py,
                headless,
                detached,
                pull=pull,
                workspace_root=workspace_root
            )

        if (
            not os.environ.get("DBUS_SESSION_BUS_ADDRESS")
            or not os.environ.get("XDG_RUNTIME_DIR")
        ):
            print(
                "DBUS_SESSION_BUS_ADDRESS/XDG_RUNTIME_DIR not set; skipping systemd user install"
            )
            return install_service_cron(
                service_name,
                repo_root,
                venv_py,
                headless=headless,
                detached=detached,
                pull=pull,
                workspace_root=workspace_root,
            )

        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            print(
                "Running as root; installing system-wide systemd service instead."
            )
            return install_service_systemd_system(
                service_name,
                repo_root,
                venv_py,
                headless=headless,
                detached=detached,
                pull=pull,
                workspace_root=workspace_root,
                service_user=service_user,
            )

        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_file = unit_dir / f"{service_name}.service"
        
        # Prefer local helper if it exists
        local_helper = repo_root / "run_client.py"
        if not local_helper.exists():
            local_helper = repo_root / "scripts" / "run_client.py"
        target_script = local_helper if local_helper.exists() else Path(__file__).resolve()
        
        exec_args = f'"{venv_py}" "{target_script}" --detached '
        if headless: exec_args += "--headless "
        if pull: exec_args += "--pull "
        exec_args += f'--repo-root "{repo_root}" '
        
        content = f"[Unit]\nDescription=Medios-Macina Client\nAfter=network.target\n\n[Service]\nType=simple\nExecStart={exec_args}\nWorkingDirectory={str(repo_root)}\nRestart=on-failure\nEnvironment=PYTHONUNBUFFERED=1\n\n[Install]\nWantedBy=default.target\n"
        unit_file.write_text(content, encoding="utf-8")
        def _run_systemctl(cmd: List[str]) -> subprocess.CompletedProcess:
            try:
                return subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except Exception as exc:
                raise

        failed = False
        for cmd in [
            [systemctl, "--user", "daemon-reload"],
            [systemctl,
             "--user",
             "enable",
             "--now",
             f"{service_name}.service"],
        ]:
            result = _run_systemctl(cmd)
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                stdout = (result.stdout or "").strip()
                err_lines = "\n".join([l for l in [stderr, stdout] if l])
                print("Failed to run systemctl", cmd, "exit", result.returncode)
                if err_lines:
                    print(err_lines)
                if "user scope bus" in err_lines.lower() or "xdg_runtime_dir" in err_lines.lower():
                    print("systemd user bus unavailable; falling back to cron @reboot install.")
                    return install_service_cron(
                        service_name,
                        repo_root,
                        venv_py,
                        headless=headless,
                        detached=detached,
                        pull=pull,
                        workspace_root=workspace_root,
                    )
                failed = True
                break

        if failed:
            return False

        print(f"systemd user service '{service_name}' installed and started.")
        return True
    except Exception as exc:
        print("systemd install error:", exc)
        return False


def uninstall_service_systemd(service_name: str) -> bool:
    try:
        systemctl = shutil.which("systemctl")
        if not systemctl:
            print("systemctl not available; cannot uninstall systemd service.")
            return False
        subprocess.run(
            [systemctl,
             "--user",
             "disable",
             "--now",
             f"{service_name}.service"],
            check=False
        )
        unit_file = Path.home(
        ) / ".config" / "systemd" / "user" / f"{service_name}.service"
        if unit_file.exists():
            unit_file.unlink()
        subprocess.run([systemctl, "--user", "daemon-reload"], check=True)
        print(f"systemd user service '{service_name}' removed.")
        return True
    except Exception as exc:
        print("systemd uninstall error:", exc)
        return False


def install_service_systemd_system(
    service_name: str,
    repo_root: Path,
    venv_py: Path,
    headless: bool = True,
    detached: bool = True,
    pull: bool = False,
    workspace_root: Optional[Path] = None,
    service_user: Optional[str] = None,
) -> bool:
    try:
        systemctl = shutil.which("systemctl")
        if not systemctl:
            print(
                "systemctl not available; falling back to crontab @reboot (if present)."
            )
            return install_service_cron(
                service_name,
                repo_root,
                venv_py,
                headless,
                detached,
                pull=pull,
                workspace_root=workspace_root,
            )

        if service_user and not ensure_service_user(service_user):
            print(f"Unable to prepare service user '{service_user}' for system service.")
            return False
        if service_user and not grant_service_user_repo_access(repo_root, service_user):
            print(
                f"Failed to assign '{service_user}' as the owner of '{repo_root}'."
            )
            return False

        unit_dir = Path("/etc/systemd/system")
        service_file = unit_dir / f"{service_name}.service"
        unit_dir.mkdir(parents=True, exist_ok=True)

        local_helper = repo_root / "run_client.py"
        if not local_helper.exists():
            local_helper = repo_root / "scripts" / "run_client.py"
        target_script = local_helper if local_helper.exists() else Path(__file__).resolve()

        exec_args = f'"{venv_py}" "{target_script}" --detached '
        if headless:
            exec_args += "--headless "
        if pull:
            exec_args += "--pull "
        exec_args += f'--repo-root "{repo_root}" '

        service_lines = [
            "[Unit]",
            "Description=Medios-Macina Client (system service)",
            "After=network.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={exec_args}",
            f"WorkingDirectory={repo_root}",
            "Restart=on-failure",
            "Environment=PYTHONUNBUFFERED=1",
        ]
        if service_user:
            service_lines.append(f"User={service_user}")
            service_lines.append(f"Group={service_user}")
        service_lines.extend([
            "",
            "[Install]",
            "WantedBy=multi-user.target",
        ])
        content = "\n".join(service_lines) + "\n"
        service_file.write_text(content, encoding="utf-8")

        for cmd in [
            [systemctl, "daemon-reload"],
            [systemctl, "enable", "--now", f"{service_name}.service"],
        ]:
            subprocess.run(cmd, check=True)

        print(f"system-wide systemd service '{service_name}' enabled and started.")
        return True
    except subprocess.CalledProcessError as exc:
        print("Failed to install system-wide service:", exc)
        return False
    except PermissionError as exc:
        print("Permission denied while writing systemd unit:", exc)
        return False
    except Exception as exc:
        print("system-wide install error:", exc)
        return False


def install_service_cron(
    service_name: str,
    repo_root: Path,
    venv_py: Path,
    headless: bool = True,
    detached: bool = True,
    pull: bool = False,
    workspace_root: Optional[Path] = None,
) -> bool:
    try:
        crontab = shutil.which("crontab")
        if not crontab:
            print("crontab not available; cannot install reboot cron job.")
            return False
            
        # Prefer local helper if it exists
        local_helper = repo_root / "run_client.py"
        if not local_helper.exists():
            local_helper = repo_root / "scripts" / "run_client.py"
        target_script = local_helper if local_helper.exists() else Path(__file__).resolve()
        
        exec_args = f'"{venv_py}" "{target_script}" --detached '
        if headless: exec_args += "--headless "
        if pull: exec_args += "--pull "
        exec_args += f'--repo-root "{repo_root}" '
            
        entry = f"@reboot {exec_args} # {service_name}\n"
        proc = subprocess.run(
            [crontab,
             "-l"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        existing = proc.stdout if proc.returncode == 0 else ""
        if entry.strip() in existing:
            print("Crontab entry already present; skipping.")
            return True
        new = existing + "\n" + entry
        subprocess.run([crontab, "-"], input=new, text=True, check=True)
        print(f"Crontab @reboot entry added for '{service_name}'.")
        return True
    except subprocess.CalledProcessError as e:
        print("Failed to install crontab entry:", e)
        return False
    except Exception as exc:
        print("crontab install error:", exc)
        return False


def uninstall_service_cron(service_name: str, repo_root: Path, venv_py: Path) -> bool:
    try:
        crontab = shutil.which("crontab")
        if not crontab:
            print("crontab not available; cannot remove reboot cron job.")
            return False
        proc = subprocess.run(
            [crontab,
             "-l"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        if proc.returncode != 0:
            print("No crontab found for user; nothing to remove.")
            return True
        lines = [l for l in proc.stdout.splitlines() if f"# {service_name}" not in l]
        new = "\n".join(lines) + "\n"
        subprocess.run([crontab, "-"], input=new, text=True, check=True)
        print(f"Crontab entry for '{service_name}' removed.")
        return True
    except subprocess.CalledProcessError as e:
        print("Failed to modify crontab:", e)
        return False
    except Exception as exc:
        print("crontab uninstall error:", exc)
        return False


def install_service_auto(
    service_name: str,
    repo_root: Path,
    venv_py: Path,
    headless: bool = True,
    detached: bool = True,
    pull: bool = False,
    workspace_root: Optional[Path] = None,
    service_user: Optional[str] = None,
) -> bool:
    try:
        if os.name == "nt":
            return install_service_windows(
                service_name,
                repo_root,
                venv_py,
                headless=headless,
                detached=detached,
                pull=pull,
                workspace_root=workspace_root
            )
        else:
            if shutil.which("systemctl"):
                return install_service_systemd(
                    service_name,
                    repo_root,
                    venv_py,
                    headless=headless,
                    detached=detached,
                    pull=pull,
                    workspace_root=workspace_root,
                    service_user=service_user,
                )
            else:
                return install_service_cron(
                    service_name,
                    repo_root,
                    venv_py,
                    headless=headless,
                    detached=detached,
                    pull=pull,
                    workspace_root=workspace_root
                )
    except Exception as exc:
        print("install_service_auto error:", exc)
        return False
    except Exception as exc:
        print("install_service_auto error:", exc)
        return False


def uninstall_service_auto(service_name: str, repo_root: Path, venv_py: Path) -> bool:
    try:
        if os.name == "nt":
            return uninstall_service_windows(service_name)
        else:
            if shutil.which("systemctl"):
                return uninstall_service_systemd(service_name)
            else:
                return uninstall_service_cron(service_name, repo_root, venv_py)
    except Exception as exc:
        print("uninstall_service_auto error:", exc)
        return False


def print_activation_instructions(
    repo_root: Path,
    venv_dir: Path,
    venv_py: Path
) -> None:
    print("\nActivation and run examples:")
    # PowerShell
    print(f"  PowerShell:\n    . {shlex.quote(str(venv_dir))}\\Scripts\\Activate.ps1")
    # CMD
    print(f"  CMD:\n    {str(venv_dir)}\\Scripts\\activate.bat")
    # Bash
    print(f"  Bash (Linux/macOS/WSL):\n    source {str(venv_dir)}/bin/activate")
    print(
        f"\nDirect run without activating:\n  {str(venv_py)} {str(repo_root/ 'hydrus_client.py')}"
    )


def detach_kwargs_for_platform():
    kwargs = {}
    if os.name == "nt":
        # Flags to ensure the process is detached and has NO console window
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return kwargs


def find_venv_python(repo_root: Path,
                     venv_arg: Optional[str],
                     venv_name: str) -> Optional[Path]:
    # venv_arg may be a python executable or a directory
    if venv_arg:
        p = Path(venv_arg)
        if p.exists():
            if p.is_file():
                return p
            else:
                found = get_python_in_venv(p)
                if found:
                    return found
    # Hydrus installs may use either .venv (this helper's default) or venv.
    names: List[str] = []
    for name in (venv_name, ".venv", "venv"):
        if name and name not in names:
            names.append(name)
    for name in names:
        found = get_python_in_venv(repo_root / name)
        if found:
            return found
    # Fallback: if current interpreter is inside repo venv
    try:
        cur = Path(sys.executable).resolve()
        if repo_root in cur.parents:
            return cur
    except Exception:
        pass
    return None


def _python_can_import(python_exe: Path, modules: List[str]) -> bool:
    """Return True if the given python executable can import all modules in the list.

    Uses a subprocess to avoid side-effects in the current interpreter.
    """
    if not python_exe:
        return False
    try:
        # Build a short import test string. Use semicolons to ensure any import error results in non-zero exit.
        imports = ";".join([f"import {m}" for m in modules])
        out = subprocess.run(
            [str(python_exe),
             "-c",
             imports],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return out.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description=
        "Run hydrus_client.py using the repo-local venv Python (top-level helper)"
    )
    p.add_argument(
        "--venv",
        help="Path to venv dir or python executable (overrides default .venv)"
    )
    p.add_argument(
        "--venv-name",
        default=".venv",
        help="Name of the venv folder to look for (default: .venv)"
    )
    p.add_argument(
        "--client",
        default="hydrus_client.py",
        help="Path to hydrus_client.py relative to repo root",
    )
    p.add_argument(
        "--repo-root",
        default=None,
        help="Path to the hydrus repository root (overrides auto-detection)",
    )
    p.add_argument(
        "--install-deps",
        action="store_true",
        help="Install requirements.txt into the venv before running",
    )
    p.add_argument(
        "--reinstall",
        action="store_true",
        help=
        "Force re-install dependencies from requirements.txt into the venv (uses --force-reinstall)",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help=
        "Verify that packages from requirements.txt are importable in the venv (after install)",
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help=
        "Attempt to launch the client without showing the Qt GUI (best-effort). Default for subsequent runs; first run will show GUI unless --headless is supplied",
    )
    p.add_argument(
        "--pull",
        action="store_true",
        help="Force a repository update before starting the client",
    )
    p.add_argument(
        "--no-update",
        action="store_true",
        help="Skip the default repository git pull before startup",
    )
    p.add_argument(
        "--update-deps",
        action="store_true",
        help="Update python dependencies to latest compatible versions on startup",
    )
    p.add_argument(
        "--gui",
        action="store_true",
        help="Start the client with the GUI visible (overrides headless/default) ",
    )
    p.add_argument(
        "--detached",
        action="store_true",
        help="Start the client and do not wait (detached)"
    )
    p.add_argument(
        "--install-service",
        action="store_true",
        help=
        "Install a user-level start-on-boot service/scheduled task for the hydrus client",
    )
    p.add_argument(
        "--uninstall-service",
        action="store_true",
        help="Remove an installed start-on-boot service/scheduled task",
    )
    p.add_argument(
        "--service-name",
        default="hydrus-client",
        help="Name of the service / scheduled task to install (default: hydrus-client)",
    )
    p.add_argument(
        "--service-user",
        default=None,
        help="When installing a system-wide unit as root, optionally run it under this user (default: hydrusnetwork)",
    )
    p.add_argument(
        "--cwd",
        default=None,
        help="Working directory to start the client in (default: repo root)"
    )
    p.add_argument("--quiet", action="store_true", help="Reduce output")
    p.add_argument(
        "client_args",
        nargs=argparse.REMAINDER,
        help="Arguments to pass to hydrus_client.py (prefix with --)",
    )

    args = p.parse_args(argv)

    script_dir = Path(__file__).resolve().parent
    if (script_dir / "hydrus_client.py").exists():
        workspace_root = script_dir
    else:
        workspace_root = script_dir.parent

    if args.repo_root:
        repo_root = Path(args.repo_root).expanduser().resolve()
    else:
        if (workspace_root / "hydrus_client.py").exists():
            repo_root = workspace_root
        else:
            candidate = workspace_root / "hydrusnetwork"
            if candidate.exists():
                repo_root = candidate
            else:
                repo_root = workspace_root

    repo_updated = False
    should_pull_repo = not args.no_update and not (args.install_service or args.uninstall_service)
    if should_pull_repo:
        if shutil.which("git"):
            if (repo_root / ".git").exists():
                if not args.quiet:
                    print(f"Updating repository via 'git pull --ff-only' in {repo_root}...")
                try:
                    k = {}
                    if os.name == "nt":
                        k["creationflags"] = 0x08000000
                    result = subprocess.run(
                        ["git", "pull", "--ff-only"],
                        cwd=str(repo_root),
                        check=False,
                        capture_output=True,
                        text=True,
                        **k,
                    )
                    pull_output = "\n".join(
                        part.strip()
                        for part in [result.stdout or "", result.stderr or ""]
                        if part.strip()
                    )
                    if result.returncode != 0:
                        print(f"Warning: git pull failed: {pull_output or result.returncode}")
                    else:
                        repo_updated = "already up to date" not in pull_output.lower()
                        if pull_output and not args.quiet:
                            print(pull_output)
                except Exception as e:
                    print(f"Warning: git pull failed: {e}")
            else:
                if not args.quiet:
                    print("Skipping 'git pull': directory is not a git repository.")
        else:
            if not args.quiet:
                print("Skipping 'git pull': 'git' not found on PATH.")

    venv_py = find_venv_python(repo_root, args.venv, args.venv_name)

    def _is_running_in_virtualenv() -> bool:
        try:
            return hasattr(sys,
                           "real_prefix") or getattr(sys,
                                                     "base_prefix",
                                                     None
                                                     ) != getattr(sys,
                                                                  "prefix",
                                                                  None)
        except Exception:
            return False

    # Skip heavy verification if we are just installing/uninstalling a service
    do_verify = args.verify and not (args.install_service or args.uninstall_service)

    # Prefer the current interpreter if the helper was invoked from a virtualenv
    # and the user did not explicitly pass --venv. This matches the user's likely
    # intent when they called: <venv_python> scripts/run_client.py ...
    cur_py = Path(sys.executable)

    # However, if we've already found a repo-local venv and the current Python
    # is external to the repository, we do NOT prefer it yet - we'll verify the
    # repo-local one first. This prevents tools like Medios-Macina from
    # accidentally installing their own venv into the repo's services.
    cur_is_external = True
    try:
        if repo_root in cur_py.resolve().parents:
            cur_is_external = False
    except Exception:
        pass

    if args.venv is None and _is_running_in_virtualenv() and cur_py and (not venv_py or not cur_is_external):
        # If current interpreter looks like a venv and can import required modules,
        # prefer it immediately rather than forcing the repo venv.
        req = find_requirements(repo_root)
        pkgs = parse_requirements_file(req) if req and do_verify else []
        check_pkgs = pkgs if pkgs else ["pyyaml"]
        
        ok_cur = False
        if do_verify:
            try:
                ok_cur = verify_imports(cur_py, check_pkgs)
            except Exception:
                ok_cur = _python_can_import(cur_py, ["yaml"])
        else:
            # If skipping verification, assume current is OK if it's the right version
            ok_cur = True
            
        if ok_cur:
            venv_py = cur_py
            if not args.quiet:
                print(f"Using current Python interpreter as venv: {cur_py}")

    # If we found a repo-local venv, verify it has at least the core imports (or the
    # packages listed in requirements.txt). If not, prefer the current Python
    # interpreter when that interpreter looks more suitable (e.g. has deps installed).
    if venv_py and venv_py != cur_py and do_verify:
        if not args.quiet:
             print(f"Found venv python: {venv_py}")
        req = find_requirements(repo_root)
        pkgs = parse_requirements_file(req) if req else []
        check_pkgs = pkgs if pkgs else ["pyyaml"]
        try:
            ok_venv = verify_imports(venv_py, check_pkgs)
        except Exception:
            ok_venv = _python_can_import(venv_py, ["yaml"])
        # ... logic continues below

        if not ok_venv:
            try:
                ok_cur = verify_imports(cur_py, check_pkgs)
            except Exception:
                ok_cur = _python_can_import(cur_py, ["yaml"])

            if ok_cur:
                if not args.quiet:
                    print(
                        f"Repository venv ({venv_py}) is missing required packages; using current Python at {cur_py} instead."
                    )
                venv_py = cur_py
            else:
                print(
                    "Warning: repository venv appears to be missing required packages. If the client fails to start, run this helper with --install-deps to install requirements into the repo venv, or use --venv to point to a Python that has the deps."
                )

    if not venv_py:
        print("Could not locate a repository venv.")
        print(
            "Create one with: python -m venv venv (inside your hydrus repo) and then re-run this helper, or use the installer to create it for you."
        )
        venv_dir = repo_root / "venv" if (repo_root / "venv").exists() else repo_root / args.venv_name
        print_activation_instructions(
            repo_root,
            venv_dir,
            get_python_in_venv(venv_dir) or (venv_dir / "bin" / "python"),
        )
        return 2

    client_path = (repo_root / args.client).resolve()
    if not client_path.exists():
        print(f"Client file not found: {client_path}")
        return 3

    cwd = Path(args.cwd).resolve() if args.cwd else repo_root

    # Optionally install dependencies
    # Automatically update dependencies if we pulled new code or if forced via env/flag
    should_update = args.update_deps or os.environ.get("MM_UPDATE_DEPS") == "1"
    
    # Check config.conf for auto_update_deps
    config_path = repo_root / "config.conf"
    if not should_update and config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
                if "auto_update_deps=true" in content.lower().replace(" ", ""):
                    should_update = True
        except Exception:
            pass

    if not should_update and (args.pull or repo_updated) and not (args.install_service or args.uninstall_service):
        should_update = True

    if args.install_deps or args.reinstall or should_update:
        req = find_requirements(repo_root)
        ok = install_requirements(venv_py, repo_root, req, reinstall=args.reinstall, upgrade=should_update)
        if not ok:
            print("Dependency installation failed; aborting")
            return 4
        if args.verify:
            if req:
                pkgs = parse_requirements_file(req)
                if pkgs and not verify_imports(venv_py, pkgs):
                    print(
                        "Verification failed; see instructions above to re-run installation."
                    )
            else:
                subprocess.run([str(venv_py), "-m", "pip", "check"], check=False)

    # If not installing but user asked to verify, do verification only
    if args.verify and not (args.install_deps or args.reinstall or should_update):
        req = find_requirements(repo_root)
        if req:
            pkgs = parse_requirements_file(req)
            if pkgs and not verify_imports(venv_py, pkgs):
                print(
                    "Verification found missing packages. Use --install-deps to install into the venv."
                )

    # If the venv appears to be missing required packages, offer to install them interactively
    if do_verify:
        req = find_requirements(repo_root)
        pkgs = parse_requirements_file(req) if req else []
        check_pkgs = pkgs if pkgs else ["pyyaml"]
        try:
            venv_ok = verify_imports(venv_py, check_pkgs)
        except Exception:
            venv_ok = _python_can_import(venv_py, ["yaml"])  # fallback

        if not venv_ok:
            # If user explicitly requested install, we've already attempted it above; otherwise, do not block.
            if args.install_deps or args.reinstall:
                # if we already did an install attempt and it still fails, bail
                print("Dependency verification failed after install; aborting.")
                return 4

            # Default: print a clear warning and proceed to launch with the repository venv
            print(
                "Warning: repository venv appears to be missing required packages. Proceeding to launch with repository venv; the client may fail to start. Use --install-deps to install requirements into the repo venv."
            )

    # Service install/uninstall requests
    if args.install_service or args.uninstall_service:
        first_run = is_first_run(repo_root)
        if args.gui:
            use_headless = False
        elif args.headless:
            use_headless = True
        else:
            use_headless = not first_run

        service_user = args.service_user.strip() if args.service_user else None
        if (
            args.install_service
            and not service_user
            and os.name != "nt"
            and hasattr(os, "geteuid")
            and os.geteuid() == 0
        ):
            service_user = "hydrusnetwork"

        if args.install_service:
            ok = install_service_auto(
                args.service_name,
                repo_root,
                venv_py,
                headless=use_headless,
                detached=True,
                pull=args.pull,
                workspace_root=workspace_root,
                service_user=service_user
            )
            return 0 if ok else 6
        if args.uninstall_service:
            ok = uninstall_service_auto(args.service_name, repo_root, venv_py)
            return 0 if ok else 7

    # Determine headless vs GUI
    if args.gui:
        headless = False
    elif args.headless:
        headless = True
    else:
        # Default to GUI for the client launcher
        headless = False

    # On Windows, if we are headless, use pythonw.exe for the client too to avoid a console.
    if os.name == "nt" and headless:
        pw = venv_py.parent / "pythonw.exe"
        if pw.exists():
            venv_py = pw

    # Prepare the command
    client_args = args.client_args or []
    cmd = [str(venv_py), str(client_path)] + client_args

    if not args.quiet and is_first_run(repo_root):
        print("First run detected: defaulting to GUI unless --headless is specified.")

    env = os.environ.copy()
    if headless:
        env["QT_QPA_PLATFORM"] = "offscreen"
        if not args.quiet:
            print("Headless: QT_QPA_PLATFORM=offscreen")

    # Inform which Python will be used
    if not args.quiet:
        try:
            print(f"Launching Hydrus client with Python: {venv_py}")
            print(f"Command: {' '.join(shlex.quote(str(c)) for c in cmd)}")
        except Exception:
            pass

    # Launch
    if args.detached:
        try:
            kwargs = detach_kwargs_for_platform()
            kwargs.update({
                "cwd": str(cwd),
                "env": env
            })
            subprocess.Popen(cmd, **kwargs)
            print("Hydrus client launched (detached).")
            return 0
        except Exception as exc:
            print("Failed to launch client detached:", exc)
            return 5
    else:
        p = None
        try:
            # On Windows, if we are already using pythonw.exe, we don't need CREATE_NO_WINDOW
            # as the process already has no console. Avoiding extra flags helps with 
            # service termination mapping.
            kwargs = {}
            if os.name == "nt" and headless and "pythonw.exe" not in str(venv_py).lower():
                kwargs["creationflags"] = 0x08000000
            
            p = subprocess.Popen(cmd, cwd=str(cwd), env=env, **kwargs)
            p.wait()
            return p.returncode
        except Exception as e:
            if not args.quiet:
                print(f"Hydrus client error: {e}")
            return 5
        finally:
            # Ensure the child process is terminated if the monitor process is killed
            if p and p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
