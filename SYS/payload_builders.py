from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Sequence
from urllib.parse import unquote, urlparse


def normalize_file_extension(ext_value: Any) -> str:
    ext = str(ext_value or "").strip().lstrip(".")
    for sep in (" ", "|", "(", "[", "{", ",", ";"):
        if sep in ext:
            ext = ext.split(sep, 1)[0]
            break
    if "." in ext:
        ext = ext.split(".")[-1]
    ext = "".join(ch for ch in ext if ch.isalnum())
    return ext[:5]


def extract_title_tag_value(tags: Sequence[str]) -> Optional[str]:
    values: list[str] = []
    for tag in tags:
        text = str(tag or "").strip()
        if text.lower().startswith("title:"):
            value = text.split(":", 1)[1].strip()
            if value:
                values.append(value)
    if not values:
        return None
    return max(values, key=len)


def _derive_title(
    title: Optional[str],
    fallback_title: Optional[str],
    path: Optional[str],
    url: Any,
    hash_value: Optional[str],
) -> str:
    for candidate in (title, fallback_title):
        text = str(candidate or "").strip()
        if text:
            return text

    path_text = str(path or "").strip()
    if path_text:
        try:
            return Path(path_text).stem or path_text
        except Exception:
            return path_text

    if isinstance(url, str):
        try:
            parsed = urlparse(url)
            name = Path(unquote(parsed.path)).stem
            if name:
                return name
        except Exception:
            pass
        text = url.strip()
        if text:
            return text

    if isinstance(url, list):
        for candidate in url:
            text = str(candidate or "").strip()
            if text:
                return text

    return str(hash_value or "").strip()


def build_file_result_payload(
    *,
    title: Optional[str] = None,
    fallback_title: Optional[str] = None,
    path: Optional[str] = None,
    url: Any = None,
    hash_value: Optional[str] = None,
    store: Optional[str] = None,
    tag: Optional[Sequence[str]] = None,
    ext: Any = None,
    size_bytes: Optional[int] = None,
    columns: Optional[Sequence[tuple[str, Any]]] = None,
    source: Optional[str] = None,
    table: Optional[str] = None,
    detail: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    resolved_title = _derive_title(title, fallback_title, path, url, hash_value)
    resolved_path = str(path).strip() if path is not None and str(path).strip() else None
    resolved_store = str(store).strip() if store is not None and str(store).strip() else None
    resolved_ext = normalize_file_extension(ext)
    if not resolved_ext:
        for candidate in (resolved_path, resolved_title):
            text = str(candidate or "").strip()
            if not text:
                continue
            try:
                resolved_ext = normalize_file_extension(Path(text).suffix)
            except Exception:
                resolved_ext = ""
            if resolved_ext:
                break

    payload: Dict[str, Any] = {"title": resolved_title}

    if resolved_path is not None:
        payload["path"] = resolved_path
    if hash_value:
        payload["hash"] = str(hash_value)
    if url not in (None, "", []):
        payload["url"] = url
    if resolved_store is not None:
        payload["store"] = resolved_store
    if tag is not None:
        payload["tag"] = list(tag)
    if resolved_ext:
        payload["ext"] = resolved_ext
    if size_bytes is not None:
        payload["size_bytes"] = size_bytes
    if columns is not None:
        payload["columns"] = list(columns)
    if source:
        payload["source"] = source
    if table:
        payload["table"] = table
    if detail is not None:
        payload["detail"] = str(detail)

    payload.update(extra)
    return payload


def build_table_result_payload(
    *,
    columns: Sequence[tuple[str, Any]],
    title: Optional[str] = None,
    table: Optional[str] = None,
    detail: Optional[str] = None,
    selection_args: Optional[Sequence[Any]] = None,
    selection_action: Optional[Sequence[Any]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "columns": [(str(label), value) for label, value in columns],
    }

    if title is not None:
        payload["title"] = str(title)
    if table:
        payload["table"] = table
    if detail is not None:
        payload["detail"] = str(detail)
    if selection_args:
        payload["_selection_args"] = [str(arg) for arg in selection_args if arg is not None]
    if selection_action:
        payload["_selection_action"] = [str(arg) for arg in selection_action if arg is not None]

    payload.update(extra)
    return payload