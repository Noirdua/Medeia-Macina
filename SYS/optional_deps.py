from __future__ import annotations

import importlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from SYS.logger import log
from SYS.rich_display import stdout_console


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def _is_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _try_import(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except Exception:
        return False


_FLORENCEVISION_DEPENDENCIES: List[Tuple[str, str]] = [
    ("transformers", "transformers>=4.45.0"),
    ("torch", "torch>=2.4.0"),
    ("PIL", "Pillow>=10.0.0"),
    ("einops", "einops>=0.8.0"),
    ("timm", "timm>=1.0.0"),
]

_PROVIDER_DEPENDENCIES: Dict[str, List[Tuple[str, str]]] = {
    "telegram": [
        ("telethon", "telethon>=1.36.0"),
        ("cryptg", "cryptg"),
    ],
    "soulseek": [("aioslsk", "aioslsk>=1.6.0")],
}

def florencevision_missing_modules() -> List[str]:
    return [
        requirement
        for import_name, requirement in _FLORENCEVISION_DEPENDENCIES
        if not _try_import(import_name)
    ]


def _provider_missing_modules(config: Dict[str, Any]) -> Dict[str, List[str]]:
    missing: Dict[str, List[str]] = {}
    plugin_cfg = (config or {}).get("plugin")
    if not isinstance(plugin_cfg, dict):
        return missing

    for provider_name, requirements in _PROVIDER_DEPENDENCIES.items():
        block = plugin_cfg.get(provider_name)
        if not isinstance(block, dict) or not block:
            continue
        missing_for_provider = [
            requirement
            for import_name, requirement in requirements
            if not _try_import(import_name)
        ]
        if missing_for_provider:
            missing[provider_name] = missing_for_provider
    return missing


def _pip_install(requirements: List[str]) -> Tuple[bool, str]:
    if not requirements:
        return True, "No requirements"

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", *requirements]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            importlib.invalidate_caches()
            return True, proc.stdout.strip() or "Installed"
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return False, out.strip() or f"pip exited with code {proc.returncode}"
    except Exception as exc:
        return False, str(exc)


def _install_requirements(label: str, requirements: List[str]) -> None:
    if not requirements:
        return

    names = ", ".join(requirements)
    status_text = f"Installing {label} dependencies: {names}"
    try:
        with stdout_console().status(status_text, spinner="dots"):
            ok, detail = _pip_install(requirements)
    except Exception:
        log(f"[startup] {label} dependencies missing ({names}). Attempting auto-install...")
        ok, detail = _pip_install(requirements)

    if ok:
        log(f"[startup] {label} dependency install OK")
    else:
        log(f"[startup] {label} dependency auto-install failed. {detail}")


def deno_available() -> bool:
    if shutil.which("deno"):
        return True
    home = Path.home()
    candidates = [
        home / ".deno" / "bin" / ("deno.exe" if os.name == "nt" else "deno"),
        home / ".deno" / "bin" / "deno",
    ]
    return any(path.is_file() for path in candidates)


def ensure_deno(version: Optional[str] = None) -> Tuple[bool, str]:
    if deno_available():
        return True, "deno already installed"
    system = platform.system().lower()
    try:
        if system == "windows":
            if version:
                ver = version if str(version).startswith("v") else f"v{version}"
                ps_cmd = f"iwr https://deno.land/x/install/install.ps1 -useb | iex; Install-Deno -Version {ver}"
            else:
                ps_cmd = "iwr https://deno.land/x/install/install.ps1 -useb | iex"
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    ps_cmd,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        else:
            if version:
                ver = version if str(version).startswith("v") else f"v{version}"
                cmd = f"curl -fsSL https://deno.land/install.sh | sh -s {ver}"
            else:
                cmd = "curl -fsSL https://deno.land/install.sh | sh"
            proc = subprocess.run(
                ["sh", "-c", cmd],
                check=False,
                capture_output=True,
                text=True,
            )
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or "deno install failed").strip()
        if deno_available():
            return True, "deno installed"
        return False, "deno installed but not on PATH (add ~/.deno/bin)"
    except Exception as exc:
        return False, str(exc)


def maybe_auto_install_configured_tools(config: Dict[str, Any]) -> None:
    """Best-effort dependency auto-installer for configured tools and providers.

    This is intentionally conservative:
    - Only acts when a configuration block is present/enabled.
    - Skips under pytest.

    Current supported features: FlorenceVision tool, Telegram provider, Soulseek provider
    """
    if _is_pytest():
        return

    plugin_cfg = (config or {}).get("plugin")
    legacy_tool_cfg = (config or {}).get("tool")
    fv = None
    if isinstance(plugin_cfg, dict):
        fv = plugin_cfg.get("florencevision")
    if fv is None and isinstance(legacy_tool_cfg, dict):
        fv = legacy_tool_cfg.get("florencevision")
    if isinstance(fv, dict) and _as_bool(fv.get("enabled"), False):
        auto_install = _as_bool(fv.get("auto_install"), True)
        if auto_install:
            missing = florencevision_missing_modules()
            if missing:
                _install_requirements("FlorenceVision", missing)

    provider_missing = _provider_missing_modules(config)
    for provider_name, requirements in provider_missing.items():
        label = f"{provider_name.title()} provider"
        _install_requirements(label, requirements)


__all__ = ["maybe_auto_install_configured_tools", "florencevision_missing_modules"]
