from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional, Sequence, Tuple


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def looks_like_url(value: Any, *, extra_prefixes: Iterable[str] = ()) -> bool:
    try:
        text = str(value or "").strip().lower()
    except Exception:
        return False
    if not text:
        return False
    prefixes = ("http://", "https://", "magnet:", "torrent:") + tuple(
        str(prefix).strip().lower() for prefix in extra_prefixes if str(prefix).strip()
    )
    return text.startswith(prefixes)


def normalize_selection_args(selection_args: Any) -> Optional[List[str]]:
    if isinstance(selection_args, (list, tuple)):
        return [str(arg) for arg in selection_args if arg is not None]
    if selection_args is not None:
        return [str(selection_args)]
    return None


def normalize_hash_for_selection(value: Any) -> str:
    text = str(value or "").strip()
    if _SHA256_RE.fullmatch(text):
        return text.lower()
    return text


def build_hash_store_selection(
    hash_value: Any,
    store_value: Any,
    *,
    action_name: str = "get-metadata",
) -> Tuple[Optional[List[str]], Optional[List[str]]]:
    hash_text = normalize_hash_for_selection(hash_value)
    store_text = str(store_value or "").strip()
    if not hash_text or not store_text:
        return None, None
    args = ["-query", f"hash:{hash_text}", "-instance", store_text]
    return args, [action_name] + list(args)


def build_default_selection(
    *,
    path_value: Any,
    hash_value: Any = None,
    store_value: Any = None,
) -> Tuple[Optional[List[str]], Optional[List[str]]]:
    path_text = str(path_value or "").strip()
    hash_args, hash_action = build_hash_store_selection(hash_value, store_value)

    if path_text:
        if looks_like_url(path_text):
            if hash_args and "/view_file" in path_text:
                return hash_args, hash_action
            args = ["-url", path_text]
            return args, ["download-file", "-url", path_text]

        if hash_args:
            return hash_args, hash_action

        try:
            from SYS.utils import expand_path

            resolved_path = str(expand_path(path_text))
        except Exception:
            resolved_path = path_text

        args = [resolved_path]
        return args, ["download-file", resolved_path]

    return hash_args, hash_action


def extract_selection_fields(
    item: Any,
    *,
    extra_url_prefixes: Iterable[str] = (),
) -> Tuple[Optional[List[str]], Optional[List[str]], Optional[str]]:
    selection_args: Any = None
    selection_action: Any = None
    item_url: Any = None

    if isinstance(item, dict):
        selection_args = item.get("_selection_args") or item.get("selection_args")
        selection_action = item.get("_selection_action") or item.get("selection_action")
        item_url = item.get("url") or item.get("path") or item.get("target")
        nested_values = [item.get("metadata"), item.get("full_metadata"), item.get("extra")]
    else:
        item_url = getattr(item, "url", None) or getattr(item, "path", None) or getattr(item, "target", None)
        nested_values = [
            getattr(item, "metadata", None),
            getattr(item, "full_metadata", None),
            getattr(item, "extra", None),
        ]

    for nested in nested_values:
        if not isinstance(nested, dict):
            continue
        selection_args = selection_args or nested.get("_selection_args") or nested.get("selection_args")
        selection_action = selection_action or nested.get("_selection_action") or nested.get("selection_action")
        item_url = item_url or nested.get("url") or nested.get("source_url") or nested.get("target")

    normalized_args = normalize_selection_args(selection_args)
    normalized_action = normalize_selection_args(selection_action)

    if item_url and not looks_like_url(item_url, extra_prefixes=extra_url_prefixes):
        item_url = None

    return normalized_args, normalized_action, str(item_url) if item_url else None


def selection_args_have_url(
    args_list: Sequence[str],
    *,
    extra_url_prefixes: Iterable[str] = (),
) -> bool:
    for idx, arg in enumerate(args_list):
        low = str(arg or "").strip().lower()
        if low in {"-url", "--url"} and idx + 1 < len(args_list):
            return True
        if looks_like_url(arg, extra_prefixes=extra_url_prefixes):
            return True
    return False


def extract_urls_from_selection_args(
    args_list: Sequence[str],
    *,
    extra_url_prefixes: Iterable[str] = (),
) -> List[str]:
    urls: List[str] = []
    idx = 0
    while idx < len(args_list):
        token = str(args_list[idx] or "")
        low = token.strip().lower()
        if low in {"-url", "--url"} and idx + 1 < len(args_list):
            candidate = str(args_list[idx + 1] or "").strip()
            if looks_like_url(candidate, extra_prefixes=extra_url_prefixes) and candidate not in urls:
                urls.append(candidate)
            idx += 2
            continue
        if looks_like_url(token, extra_prefixes=extra_url_prefixes):
            candidate = token.strip()
            if candidate not in urls:
                urls.append(candidate)
        idx += 1
    return urls