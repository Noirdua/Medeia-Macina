from __future__ import annotations

import ast
import hashlib
import os
import sys
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

DEFAULT_PLUGIN_SOURCE = ""
DEFAULT_PLUGIN_SOURCE_BRANCH = "main"


def plugin_source_url(config: Optional[Dict[str, Any]] = None) -> str:
    import os

    for env_name in ("MM_PLUGIN_SOURCE", "MEDEIA_PLUGIN_SOURCE"):
        raw = str(os.environ.get(env_name, "") or "").strip()
        if raw:
            return raw
    if isinstance(config, dict):
        raw = str(config.get("plugin_source") or "").strip()
        if raw:
            return raw
    return DEFAULT_PLUGIN_SOURCE


def plugin_source_branch(config: Optional[Dict[str, Any]] = None) -> str:
    import os

    for env_name in ("MM_PLUGIN_SOURCE_BRANCH", "MEDEIA_PLUGIN_SOURCE_BRANCH"):
        raw = str(os.environ.get(env_name, "") or "").strip()
        if raw:
            return raw
    if isinstance(config, dict):
        raw = str(config.get("plugin_source_branch") or "").strip()
        if raw:
            return raw
    return DEFAULT_PLUGIN_SOURCE_BRANCH


def plugin_source_cache_dir() -> Path:
    from PluginCore.registry import _repo_root

    return _repo_root() / ".plugin-source"


def install_plugins_dir() -> Path:
    from PluginCore.registry import _repo_root

    path = _repo_root() / "plugins"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _git() -> str:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git is required to install plugins from a source repository")
    return git


def _run_git(args: Sequence[str], *, cwd: Optional[Path] = None) -> str:
    completed = subprocess.run(
        [_git(), *[str(part) for part in args]],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = str(completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or f"git {' '.join(str(a) for a in args)} failed")
    return str(completed.stdout or "")


def _remote_url(cache: Path) -> str:
    try:
        return _run_git(["remote", "get-url", "origin"], cwd=cache).strip()
    except Exception:
        return ""


def _urls_match(left: str, right: str) -> bool:
    def _norm(value: str) -> str:
        text = str(value or "").strip().rstrip("/")
        if text.endswith(".git"):
            text = text[:-4]
        parsed = urlparse(text)
        host = str(parsed.netloc or "").lower()
        path = str(parsed.path or "").lower()
        return f"{host}{path}"

    return _norm(left) == _norm(right) and bool(_norm(left))


def _as_local_source(raw: str) -> Optional[Path]:
    text = str(raw or "").strip()
    if not text or "://" in text:
        return None
    try:
        path = Path(text).expanduser()
        if path.exists() and path.is_dir():
            return path.resolve()
    except Exception:
        return None
    return None


def _cache_has_commit(cache: Path) -> bool:
    try:
        _run_git(["rev-parse", "--verify", "HEAD"], cwd=cache)
        return True
    except Exception:
        return False


def _reset_cache_to_origin(cache: Path, branch: str) -> None:
    _run_git(["fetch", "--depth", "1", "origin", branch], cwd=cache)
    _run_git(["checkout", "-B", branch, "FETCH_HEAD"], cwd=cache)


def _rmtree(path: Path) -> None:
    def _onerror(func: Any, name: str, _exc: Any) -> None:
        try:
            os.chmod(name, stat.S_IWRITE)
            func(name)
        except Exception:
            pass

    if path.exists():
        shutil.rmtree(path, onerror=_onerror)


def _clone_source(url: str, dest: Path, branch: str) -> None:
    _rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run_git(["clone", "--depth", "1", "--branch", branch, url, str(dest)])
    except Exception:
        _rmtree(dest)
        _run_git(["clone", "--depth", "1", url, str(dest)])


def sync_plugin_source(config: Optional[Dict[str, Any]] = None) -> Path:
    url = plugin_source_url(config)
    local = _as_local_source(url)
    if local is not None:
        if (local / ".git").exists():
            try:
                _run_git(["pull", "--ff-only"], cwd=local)
            except Exception:
                pass
        return local
    branch = plugin_source_branch(config)
    cache = plugin_source_cache_dir()
    cache.parent.mkdir(parents=True, exist_ok=True)
    git_dir = cache / ".git"
    if git_dir.exists() and _urls_match(_remote_url(cache), url) and _cache_has_commit(cache):
        try:
            _reset_cache_to_origin(cache, branch)
            return cache
        except Exception:
            pass
    _clone_source(url, cache, branch)
    return cache


def catalog_root(source_root: Path) -> Path:
    nested = source_root / "plugins"
    try:
        if nested.is_dir() and any(nested.iterdir()):
            return nested
    except Exception:
        pass
    return source_root


def _literal_text(node: ast.AST) -> str:
    try:
        value = ast.literal_eval(node)
    except Exception:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(part).strip() for part in value if str(part).strip())
    return str(value or "").strip()


def _assign_meta(node: ast.AST, mapping: Dict[str, str], dest: Dict[str, str]) -> None:
    name = ""
    value_node: Optional[ast.AST] = None
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        name = node.targets[0].id
        value_node = node.value
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
        name = node.target.id
        value_node = node.value
    key = mapping.get(name)
    if not key or value_node is None:
        return
    text = _literal_text(value_node)
    if text:
        dest[key] = text


def _docstring_summary(node: ast.AST) -> str:
    raw = ast.get_docstring(node) or ""
    for line in raw.splitlines():
        text = line.strip()
        if text:
            return text
    return ""


def read_plugin_metadata(path: Path) -> Dict[str, str]:
    """Read PLUGIN_VERSION / DESCRIPTION / AUTHOR without importing the plugin."""
    meta = {"version": "", "description": "", "author": ""}
    target = path / "__init__.py" if path.is_dir() else path
    try:
        tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
    except Exception:
        return meta
    mapping = {
        "PLUGIN_VERSION": "version",
        "PLUGIN_DESCRIPTION": "description",
        "PLUGIN_AUTHOR": "author",
    }
    for node in tree.body:
        _assign_meta(node, mapping, meta)
    class_docs: List[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        class_meta = {"version": "", "description": "", "author": ""}
        for child in node.body:
            _assign_meta(child, mapping, class_meta)
        for key, value in class_meta.items():
            if value and not meta[key]:
                meta[key] = value
        summary = _docstring_summary(node)
        if summary:
            class_docs.append(summary)
    if not meta["description"]:
        meta["description"] = next(iter(class_docs), "") or _docstring_summary(tree)
    return meta


def read_plugin_requires(path: Path) -> List[str]:
    reqs: List[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        if isinstance(raw, (list, tuple)):
            for item in raw:
                _add(item)
            return
        text = str(raw or "").strip()
        if not text or text.startswith("#") or text in seen:
            return
        seen.add(text)
        reqs.append(text)

    target = path / "__init__.py" if path.is_dir() else path
    try:
        tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            name_node = node.targets[0]
            if not isinstance(name_node, ast.Name) or name_node.id != "PLUGIN_REQUIRES":
                continue
            try:
                _add(ast.literal_eval(node.value))
            except Exception:
                continue
    except Exception:
        pass
    req_file = path / "requirements.txt" if path.is_dir() else path.with_name("requirements.txt")
    try:
        if req_file.is_file():
            for line in req_file.read_text(encoding="utf-8").splitlines():
                _add(line)
    except Exception:
        pass
    return reqs


def read_plugin_system_requires(path: Path) -> List[str]:
    reqs: List[str] = []
    target = path / "__init__.py" if path.is_dir() else path
    try:
        tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            name_node = node.targets[0]
            if not isinstance(name_node, ast.Name) or name_node.id != "PLUGIN_SYSTEM_REQUIRES":
                continue
            try:
                value = ast.literal_eval(node.value)
            except Exception:
                continue
            items = value if isinstance(value, (list, tuple)) else [value]
            for item in items:
                text = str(item or "").strip().lower()
                if text and text not in reqs:
                    reqs.append(text)
    except Exception:
        pass
    pip_reqs = " ".join(read_plugin_requires(path)).lower()
    if "yt-dlp-ejs" in pip_reqs and "deno" not in reqs:
        reqs.append("deno")
    return reqs


def read_plugin_depends(path: Path) -> List[str]:
    deps: List[str] = []
    target = path / "__init__.py" if path.is_dir() else path
    try:
        tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            name_node = node.targets[0]
            if not isinstance(name_node, ast.Name) or name_node.id != "PLUGIN_DEPENDS":
                continue
            try:
                value = ast.literal_eval(node.value)
            except Exception:
                continue
            items = value if isinstance(value, (list, tuple)) else [value]
            for item in items:
                text = str(item or "").strip().lower()
                if text and text not in deps:
                    deps.append(text)
    except Exception:
        pass
    return deps


def missing_plugin_depends(
    names: Sequence[str],
    config: Optional[Dict[str, Any]] = None,
    *,
    source_root: Optional[Path] = None,
) -> List[str]:
    from PluginCore.lifecycle import installed_plugin_keys

    root = source_root if source_root is not None else sync_plugin_source(config)
    catalog = {name.lower(): path for name, path in list_catalog_plugins(root)}
    installed = {key.lower() for key in installed_plugin_keys()}
    wanted = [str(n or "").strip().lower() for n in names if str(n or "").strip()]
    missing: List[str] = []
    seen = set(wanted)
    for name in list(wanted):
        src = catalog.get(name)
        if src is None:
            continue
        for dep in read_plugin_depends(src):
            if dep in seen or dep in installed:
                continue
            seen.add(dep)
            missing.append(dep)
    return missing


def install_plugin_dependencies(plugin_path: Path) -> Dict[str, Any]:
    requirements = read_plugin_requires(plugin_path)
    system_requires = read_plugin_system_requires(plugin_path)
    if not requirements and not system_requires:
        return {"ok": True, "detail": ""}
    from SYS.optional_deps import _pip_install

    ok, detail = True, ""
    if requirements:
        ok, detail = _pip_install(requirements)
    extra = ""
    joined = " ".join(requirements).lower()
    for tool in system_requires:
        if tool == "deno":
            from SYS.optional_deps import ensure_deno

            deno_ok, deno_detail = ensure_deno()
            extra += f"; {deno_detail}"
            if not deno_ok:
                ok = False
                detail = (detail + "\n" + deno_detail).strip()
    if ok and "playwright" in joined:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=False,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                extra = " (playwright browsers failed)"
                ok = False
                detail = (detail + "\n" + (proc.stderr or proc.stdout or "")).strip()
        except Exception as exc:
            extra = f" (playwright install error: {exc})"
            ok = False
    return {
        "ok": ok,
        "detail": ("deps: " + ", ".join(requirements) + extra) if ok else (detail or "pip failed"),
        "requirements": requirements,
    }


def list_catalog_plugins(source_root: Path) -> List[Tuple[str, Path]]:
    root = catalog_root(source_root)
    entries: List[Tuple[str, Path]] = []
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name.lower())
    except Exception:
        return entries
    for child in children:
        name = str(child.name or "").strip()
        if not name or name.startswith(".") or name.startswith("_"):
            continue
        if name.lower() in {"readme.md", "readme.txt", "license", "license.md"}:
            continue
        if child.is_dir() and (child / "__init__.py").exists():
            entries.append((child.name, child))
        elif child.is_file() and child.suffix.lower() == ".py" and child.stem != "__init__":
            entries.append((child.stem, child))
    return entries


def _plugin_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        files = [path]
        root = path.parent
    elif path.is_dir():
        skip_parts = {"__pycache__", ".git", "watch_later", "shadercache"}
        skip_suffix = {".pyc", ".pyo", ".pyd", ".dll", ".so", ".exe"}
        skip_names = {
            "medeia-store-cache.json",
            "medeia-selected-store.json",
            "cookies.txt",
            "splash.png",
        }
        files = sorted(
            child
            for child in path.rglob("*")
            if child.is_file()
            and child.name not in skip_names
            and child.suffix.lower() not in skip_suffix
            and not any(part in skip_parts for part in child.parts)
        )
        root = path
    else:
        return ""
    for file_path in files:
        try:
            rel = file_path.relative_to(root).as_posix().encode("utf-8")
            digest.update(rel)
            digest.update(file_path.read_bytes())
        except Exception:
            continue
    return digest.hexdigest()[:12]


def _copy_plugin(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    keep_names = {
        "medeia-store-cache.json",
        "medeia-selected-store.json",
        "cookies.txt",
        "splash.png",
    }
    kept: List[Tuple[Path, bytes]] = []
    if dest.is_dir():
        for child in dest.rglob("*"):
            if child.is_file() and (
                child.name in keep_names
                or ("cookie" in child.name.lower() and child.suffix.lower() in {".txt", ".cookies"})
            ):
                try:
                    kept.append((child.relative_to(dest), child.read_bytes()))
                except Exception:
                    pass
    if dest.exists():
        if dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    if src.is_file():
        shutil.copy2(src, dest)
        return
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    for rel, data in kept:
        target = dest / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(data)
        except Exception:
            pass


def _local_dlc_plugin_dir(plugin_name: str) -> Optional[Path]:
    try:
        from PluginCore.registry import _repo_root

        sibling = _repo_root().parent / "Medeia-Macina-Plugin" / str(plugin_name or "").strip()
    except Exception:
        return None
    if sibling.exists() and (sibling.is_dir() or sibling.is_file()):
        return sibling
    return None


def _local_source_differs_hint(plugin_name: str, dest_fp: str) -> str:
    try:
        from PluginCore.registry import _repo_root

        sibling = _repo_root().parent / "Medeia-Macina-Plugin"
    except Exception:
        return ""
    try:
        local_plugin = sibling / plugin_name
        if not sibling.is_dir() or not local_plugin.exists():
            return ""
        if _plugin_fingerprint(local_plugin) == dest_fp:
            return ""
        return f"local {sibling} differs — .plugin -source {sibling}"
    except Exception:
        return ""


def install_from_catalog(
    names: Sequence[str],
    config: Optional[Dict[str, Any]] = None,
    *,
    source_root: Optional[Path] = None,
    force: bool = False,
) -> List[Dict[str, Any]]:
    from PluginCore.registry import refresh_local_plugin

    root = source_root if source_root is not None else sync_plugin_source(config)
    source_label = str(root)
    catalog = {name.lower(): path for name, path in list_catalog_plugins(root)}
    dest_root = install_plugins_dir()
    results: List[Dict[str, Any]] = []
    for raw in names:
        name = str(raw or "").strip()
        if not name:
            continue
        src = catalog.get(name.lower())
        from_label = source_label
        if src is not None:
            sibling = _local_dlc_plugin_dir(src.name if src.is_dir() else Path(src).stem)
            if sibling is not None and _plugin_fingerprint(sibling) != _plugin_fingerprint(src):
                src = sibling
                from_label = str(sibling.parent)
        if src is None:
            results.append(
                {
                    "name": name,
                    "ok": False,
                    "status": "missing",
                    "from": "",
                    "to": "",
                    "detail": "not in plugin source",
                }
            )
            continue
        dest = dest_root / (src.name if src.is_dir() else src.name)
        src_meta = read_plugin_metadata(src)
        dest_meta = read_plugin_metadata(dest) if dest.exists() else {}
        src_ver = str(src_meta.get("version") or "").strip()
        dest_ver = str(dest_meta.get("version") or "").strip()
        existed = dest.exists()
        src_fp = _plugin_fingerprint(src)
        dest_fp = _plugin_fingerprint(dest) if existed else ""
        same = existed and not force and src_fp == dest_fp
        try:
            deps: Dict[str, Any] = {"ok": True, "detail": ""}
            if not same:
                _copy_plugin(src, dest)
                refresh_local_plugin(dest.stem if dest.is_file() else dest.name)
                deps = install_plugin_dependencies(dest)
            if not existed:
                status = "installed"
                detail = f"installed {src_ver or 'ok'}"
            elif same:
                status = "current"
                detail = f"already up to date fp={src_fp}"
                local_hint = _local_source_differs_hint(src.name, dest_fp)
                if local_hint:
                    detail = f"{detail}; {local_hint}"
            else:
                status = "updated"
                if dest_ver and src_ver and dest_ver != src_ver:
                    detail = f"{dest_ver} → {src_ver}"
                elif src_ver:
                    detail = f"updated to {src_ver}"
                else:
                    detail = f"files changed fp={dest_fp}->{src_fp}"
            if deps.get("detail"):
                detail = f"{detail}; {deps['detail']}"
            results.append(
                {
                    "name": name,
                    "ok": bool(deps.get("ok", True)),
                    "status": status if deps.get("ok", True) else "deps-fail",
                    "from": dest_ver,
                    "to": src_ver or dest_ver,
                    "detail": f"{detail} [{from_label}]",
                    "source": from_label,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "name": name,
                    "ok": False,
                    "status": "fail",
                    "from": dest_ver,
                    "to": src_ver,
                    "detail": str(exc),
                }
            )
    return results


def available_plugins(config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    from PluginCore.lifecycle import installed_plugin_keys

    root = sync_plugin_source(config)
    installed = installed_plugin_keys()
    rows: List[Dict[str, Any]] = []
    for name, path in list_catalog_plugins(root):
        kind = "package" if path.is_dir() else "module"
        meta = read_plugin_metadata(path)
        rows.append(
            {
                "name": name,
                "kind": kind,
                "installed": name.lower() in installed,
                "path": str(path),
                "version": meta.get("version") or "",
                "author": meta.get("author") or "",
                "description": meta.get("description") or "",
            }
        )
    return rows
