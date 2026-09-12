from __future__ import annotations

from typing import Any, Iterable, Optional

from SYS.utils import normalize_sha256_hex


def get_field(obj: Any, field: str, default: Optional[Any] = None) -> Any:
    if isinstance(obj, list):
        if not obj:
            return default
        obj = obj[0]

    if isinstance(obj, dict):
        return obj.get(field, default)

    value = getattr(obj, field, None)
    if value is not None:
        return value

    extra_value = getattr(obj, "extra", None)
    if isinstance(extra_value, dict):
        return extra_value.get(field, default)

    return default


def first_field(obj: Any, fields: Iterable[str], default: Optional[Any] = None) -> Any:
    for field in fields:
        value = get_field(obj, str(field), None)
        if value is not None:
            return value
    return default


def get_text_field(obj: Any, *fields: str, default: str = "") -> str:
    value = first_field(obj, fields, default=None)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def get_column_text(obj: Any, *labels: str) -> Optional[str]:
    columns = get_field(obj, "columns")
    if not isinstance(columns, list):
        return None
    wanted = {str(label or "").strip().lower() for label in labels if str(label or "").strip()}
    if not wanted:
        return None
    for pair in columns:
        try:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            key, value = pair
            if str(key or "").strip().lower() not in wanted:
                continue
            text = str(value or "").strip()
            if text:
                return text
        except Exception:
            continue
    return None


def get_int_field(obj: Any, *fields: str) -> Optional[int]:
    value = first_field(obj, fields, default=None)
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(value)
    except Exception:
        return None


def get_extension_field(obj: Any, *fields: str) -> str:
    text = get_text_field(obj, *(fields or ("ext", "extension")), default="")
    return text.lstrip(".") if text else ""


def get_result_title(obj: Any, *fields: str) -> Optional[str]:
    text = get_text_field(obj, *(fields or ("title", "name", "filename")), default="")
    if text:
        return text
    return get_column_text(obj, "title", "name")


def extract_item_tags(obj: Any) -> list[str]:
    return get_string_list(obj, "tag")


def get_string_list(obj: Any, field: str) -> list[str]:
    value = get_field(obj, field)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item).strip() for item in value if item is not None and str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    return []


def set_field(obj: Any, field: str, value: Any) -> bool:
    if isinstance(obj, dict):
        obj[field] = value
        return True
    try:
        setattr(obj, field, value)
        return True
    except Exception:
        return False


def get_sha256_hex(obj: Any, *fields: str) -> Optional[str]:
    return normalize_sha256_hex(get_text_field(obj, *(fields or ("hash",))))


def get_store_name(obj: Any, *fields: str) -> Optional[str]:
    value = get_text_field(obj, *(fields or ("store",)))
    return value or None


def get_http_url(obj: Any, *fields: str) -> Optional[str]:
    value = get_text_field(obj, *(fields or ("url", "target")))
    if value.lower().startswith(("http://", "https://")):
        return value
    return None