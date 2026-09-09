from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from PluginCore.registry import REGISTRY
from PluginCore.validate import iter_plugin_entries


def _plugin_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    block = config.get("plugin")
    return block if isinstance(block, dict) else {}


def installed_plugin_keys() -> Set[str]:
    keys: Set[str] = set()
    try:
        for info in REGISTRY.iter_plugins():
            name = str(getattr(info, "canonical_name", "") or "").strip().lower()
            if name:
                keys.add(name)
            for alias in getattr(info, "alias_names", ()) or ():
                text = str(alias or "").strip().lower()
                if text:
                    keys.add(text)
    except Exception:
        pass
    for name, _path in iter_plugin_entries():
        text = str(name or "").strip().lower()
        if text:
            keys.add(text)
    return keys


def orphan_config_keys(config: Dict[str, Any]) -> List[str]:
    installed = installed_plugin_keys()
    orphans: List[str] = []
    for key in _plugin_cfg(config):
        name = str(key or "").strip()
        if name and name.strip().lower() not in installed:
            orphans.append(name)
    return orphans


def _keys_for_name(name: str, config: Dict[str, Any]) -> List[str]:
    target = str(name or "").strip().lower()
    if not target:
        return []
    wanted = {target}
    try:
        info = REGISTRY.get(target)
    except Exception:
        info = None
    if info is not None:
        wanted.add(str(info.canonical_name or "").strip().lower())
        wanted.update(
            str(alias or "").strip().lower()
            for alias in (info.alias_names or ())
        )
    wanted.discard("")
    found: List[str] = []
    seen: Set[str] = set()
    for key in _plugin_cfg(config):
        text = str(key or "").strip()
        lowered = text.lower()
        if lowered in wanted and lowered not in seen:
            seen.add(lowered)
            found.append(text)
    if target not in seen:
        # Still record the requested name so callers can report it even if absent.
        pass
    return found


def _plugin_dir_for_name(name: str) -> Optional[Path]:
    target = str(name or "").strip().lower()
    if not target:
        return None
    for entry_name, path in iter_plugin_entries():
        if str(entry_name or "").strip().lower() == target:
            return path
    return None


def uninstall_plugins(
    config: Dict[str, Any],
    names: Sequence[str] | None = None,
    *,
    orphans: bool = False,
    delete_files: bool = False,
) -> List[Dict[str, Any]]:
    """Remove plugin config (and optionally plugin files).

    Returns one result dict per removed name: {name, config, files, detail}.
    """
    from SYS.config import save_config_and_verify

    plugin_cfg = _plugin_cfg(config)
    if "plugin" not in config or not isinstance(config.get("plugin"), dict):
        config["plugin"] = plugin_cfg

    targets: List[str] = []
    seen: Set[str] = set()

    def _add(raw: str) -> None:
        text = str(raw or "").strip()
        key = text.lower()
        if not text or key in seen:
            return
        seen.add(key)
        targets.append(text)

    for raw in names or ():
        _add(raw)
    if orphans:
        for raw in orphan_config_keys(config):
            _add(raw)

    results: List[Dict[str, Any]] = []
    removed_any = False
    for name in targets:
        keys = _keys_for_name(name, config)
        config_removed = False
        for key in keys:
            if key in plugin_cfg:
                plugin_cfg.pop(key, None)
                config_removed = True
                removed_any = True
        files_removed = False
        file_detail = ""
        if delete_files:
            path = _plugin_dir_for_name(name)
            if path is not None and path.exists():
                try:
                    if path.is_file():
                        path.unlink()
                    else:
                        import shutil

                        shutil.rmtree(path)
                    files_removed = True
                except Exception as exc:
                    file_detail = f"could not delete {path}: {exc}"
        if not keys and not files_removed:
            results.append(
                {
                    "name": name,
                    "config": False,
                    "files": False,
                    "detail": "not in config",
                }
            )
            continue
        detail_parts = []
        if config_removed:
            detail_parts.append("removed config")
        if files_removed:
            detail_parts.append("removed files")
        if file_detail:
            detail_parts.append(file_detail)
        if not detail_parts:
            detail_parts.append("no config entry")
        results.append(
            {
                "name": name,
                "config": config_removed,
                "files": files_removed,
                "detail": ", ".join(detail_parts),
            }
        )

    if removed_any:
        save_config_and_verify(config)
    return results
