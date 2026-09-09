#!/usr/bin/env python3
"""Install and manage the Hydrus web GUI companion app.

This helper keeps the Medios bootstrap as the single user-facing installer entry
point while isolating the Node/npm-specific logic required by
`api-HydrusNetwork`.
"""

from __future__ import annotations

import argparse
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_REPO_URL = str(os.environ.get("MM_HYDRUS_WEB_GUI_REPO") or "").strip()
DEFAULT_DEST_NAME = "api-HydrusNetwork"
DEFAULT_SERVICE_NAME = "hydrus-web-gui"
DEFAULT_PORT = 4173


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture_output: bool = False,
    debug: bool = False,
) -> subprocess.CompletedProcess:
    if debug:
        location = f" (cwd={cwd})" if cwd else ""
        print(f"> {' '.join(cmd)}{location}")
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=capture_output,
        capture_output=capture_output,
    )


def find_executable(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def detect_npm() -> str | None:
    if os.name == "nt":
        return find_executable("npm.cmd", "npm")
    return find_executable("npm")


def parse_node_major_version(node_exe: str, debug: bool = False) -> int | None:
    try:
        result = run([node_exe, "--version"], capture_output=True, debug=debug)
        version_text = (result.stdout or "").strip()
        if version_text.startswith("v"):
            version_text = version_text[1:]
        major = version_text.split(".", 1)[0]
        return int(major)
    except Exception:
        return None


def with_privilege(cmd: list[str]) -> list[str]:
    if os.name == "nt":
        return cmd
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return cmd
    sudo = shutil.which("sudo")
    if sudo:
        return [sudo, *cmd]
    return cmd


def ensure_node_runtime(debug: bool = False) -> tuple[str, str]:
    node_exe = find_executable("node")
    npm_exe = detect_npm()
    major = parse_node_major_version(node_exe, debug=debug) if node_exe else None

    if node_exe and npm_exe and major is not None and major >= 20:
        return node_exe, npm_exe

    system = platform.system().lower()
    print("Node.js 20+ and npm are required for the Hydrus web GUI. Attempting installation...")

    if system == "windows":
        winget = find_executable("winget")
        if not winget:
            raise RuntimeError(
                "Node.js 20+ is required. Install it manually or ensure winget is available."
            )
        run(
            [
                winget,
                "install",
                "--id",
                "OpenJS.NodeJS.LTS",
                "-e",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
            debug=debug,
        )
    elif system == "linux":
        if find_executable("apt"):
            run(with_privilege(["apt", "update"]), debug=debug)
            run(with_privilege(["apt", "install", "-y", "nodejs", "npm"]), debug=debug)
        elif find_executable("dnf"):
            run(with_privilege(["dnf", "install", "-y", "nodejs", "npm"]), debug=debug)
        elif find_executable("pacman"):
            run(with_privilege(["pacman", "-Sy", "--noconfirm", "nodejs", "npm"]), debug=debug)
        else:
            raise RuntimeError(
                "Node.js 20+ is required. Install nodejs/npm manually for this distribution."
            )
    else:
        raise RuntimeError("Automatic Hydrus web GUI setup is currently supported on Windows and Linux.")

    node_exe = find_executable("node")
    npm_exe = detect_npm()
    major = parse_node_major_version(node_exe, debug=debug) if node_exe else None
    if not node_exe or not npm_exe or major is None or major < 20:
        raise RuntimeError(
            "Node.js installation completed, but Node.js 20+ with npm is still not available on PATH."
        )
    return node_exe, npm_exe


def clone_or_update_repo(repo_url: str, dest: Path, force: bool = False, debug: bool = False) -> None:
    git = find_executable("git")
    if not git:
        raise RuntimeError("git is required to install the Hydrus web GUI.")

    if dest.exists() and force:
        shutil.rmtree(dest)

    if dest.exists():
        if (dest / ".git").exists():
            run([git, "-C", str(dest), "pull", "--ff-only"], debug=debug)
            return
        if any(dest.iterdir()):
            raise RuntimeError(
                f"Destination already exists and is not a git repository: {dest}. Use --force to replace it."
            )

    dest.parent.mkdir(parents=True, exist_ok=True)
    run([git, "clone", "--depth", "1", repo_url, str(dest)], debug=debug)


def install_web_gui_dependencies(app_root: Path, npm_exe: str, debug: bool = False) -> None:
    run([npm_exe, "install"], cwd=app_root, debug=debug)
    run([npm_exe, "run", "build"], cwd=app_root, debug=debug)


def write_service_launcher(app_root: Path, npm_exe: str, port: int) -> Path:
    if os.name == "nt":
        launcher = app_root / "run-hydrus-web-gui.cmd"
        launcher.write_text(
            "@echo off\n"
            "setlocal\n"
            "cd /d \"%~dp0\"\n"
            f"call \"{npm_exe}\" run build\n"
            "if errorlevel 1 exit /b %errorlevel%\n"
            f"call \"{npm_exe}\" run preview -- --host 0.0.0.0 --port {port}\n",
            encoding="utf-8",
        )
        return launcher

    launcher = app_root / "run-hydrus-web-gui.sh"
    launcher.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "cd \"$(dirname \"$0\")\"\n"
        f"\"{npm_exe}\" run build\n"
        f"exec \"{npm_exe}\" run preview -- --host 0.0.0.0 --port {port}\n",
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | 0o111)
    return launcher


def install_service_windows(service_name: str, launcher_path: Path, debug: bool = False) -> None:
    schtasks = find_executable("schtasks")
    if not schtasks:
        raise RuntimeError("schtasks is required to register the Hydrus web GUI on Windows.")

    tr_command = f'cmd.exe /c ""{launcher_path}""'
    run(
        [
            schtasks,
            "/Create",
            "/SC",
            "ONLOGON",
            "/TN",
            service_name,
            "/TR",
            tr_command,
            "/RL",
            "LIMITED",
            "/F",
        ],
        debug=debug,
    )


def install_service_linux(service_name: str, app_root: Path, launcher_path: Path, debug: bool = False) -> None:
    systemctl = find_executable("systemctl")
    if not systemctl:
        raise RuntimeError("systemctl is required to register the Hydrus web GUI on Linux.")

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        unit_dir = Path("/etc/systemd/system")
        unit_name = f"{service_name}.service"
        wanted_by = "multi-user.target"
        systemctl_cmd = [systemctl]
        service_user = os.environ.get("SUDO_USER") or os.environ.get("USER")
    else:
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_name = f"{service_name}.service"
        wanted_by = "default.target"
        systemctl_cmd = [systemctl, "--user"]
        service_user = None

    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_file = unit_dir / unit_name
    exec_start = f"/bin/bash -lc {shlex.quote(str(launcher_path))}"

    unit_lines = [
        "[Unit]",
        "Description=Hydrus Web GUI",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={app_root}",
        f"ExecStart={exec_start}",
        "Restart=on-failure",
        "Environment=NODE_ENV=production",
    ]
    if service_user and hasattr(os, "geteuid") and os.geteuid() == 0:
        unit_lines.append(f"User={service_user}")
        unit_lines.append(f"Group={service_user}")
    unit_lines.extend([
        "",
        "[Install]",
        f"WantedBy={wanted_by}",
        "",
    ])
    unit_file.write_text("\n".join(unit_lines), encoding="utf-8")

    run([*systemctl_cmd, "daemon-reload"], debug=debug)
    run([*systemctl_cmd, "enable", "--now", unit_name], debug=debug)


def install_service(service_name: str, app_root: Path, launcher_path: Path, debug: bool = False) -> None:
    system = platform.system().lower()
    if system == "windows":
        install_service_windows(service_name, launcher_path, debug=debug)
        return
    if system == "linux":
        install_service_linux(service_name, app_root, launcher_path, debug=debug)
        return
    raise RuntimeError("Service installation for the Hydrus web GUI is only supported on Windows and Linux.")


def setup_mpv_handler(app_root: Path, npm_exe: str, debug: bool = False) -> None:
    system = platform.system().lower()
    if system not in {"windows", "linux"}:
        print("Skipping mpv-handler setup: this helper is only automated on Windows and Linux.")
        return
    run([npm_exe, "run", "setup:mpv-handler"], cwd=app_root, debug=debug)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install the Hydrus web GUI companion app")
    parser.add_argument("--root", default=".", help="Root directory that will contain the checkout")
    parser.add_argument("--dest-name", default=DEFAULT_DEST_NAME, help="Folder name for the checkout")
    parser.add_argument("--repo", default=DEFAULT_REPO_URL, help="Repository URL for the Hydrus web GUI")
    parser.add_argument("--force", action="store_true", help="Replace an existing non-empty destination")
    parser.add_argument(
        "--install-service",
        action="store_true",
        help="Register the web GUI to start automatically using Task Scheduler or systemd",
    )
    parser.add_argument(
        "--service-name",
        default=DEFAULT_SERVICE_NAME,
        help=f"Service name to register (default: {DEFAULT_SERVICE_NAME})",
    )
    parser.add_argument(
        "--setup-mpv-handler",
        action="store_true",
        help="Run the web GUI's mpv-handler setup helper after installation",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Preferred preview port for the generated service launcher (default: {DEFAULT_PORT})",
    )
    parser.add_argument("--debug", action="store_true", help="Show installer debug output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(os.path.expandvars(os.path.expanduser(args.root))).resolve()
    dest = root / os.path.expandvars(args.dest_name)

    try:
        _, npm_exe = ensure_node_runtime(debug=args.debug)
        clone_or_update_repo(args.repo, dest, force=args.force, debug=args.debug)
        install_web_gui_dependencies(dest, npm_exe, debug=args.debug)
        launcher_path = write_service_launcher(dest, npm_exe, args.port)

        if args.setup_mpv_handler:
            setup_mpv_handler(dest, npm_exe, debug=args.debug)

        if args.install_service:
            install_service(args.service_name, dest, launcher_path, debug=args.debug)

        print(f"Hydrus web GUI ready at: {dest}")
        print(f"Launcher: {launcher_path}")
        print(f"URL: http://localhost:{args.port}")
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"Hydrus web GUI install failed: {exc}", file=sys.stderr)
        return int(exc.returncode or 1)
    except Exception as exc:
        print(f"Hydrus web GUI install error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())