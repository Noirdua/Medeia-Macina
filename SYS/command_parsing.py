from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence

VALUE_ARG_FLAGS = frozenset({"-value", "--value", "-set-value", "--set-value"})


def extract_piped_value(result: Any) -> Optional[str]:
    if isinstance(result, str):
        return result.strip() if result.strip() else None
    if isinstance(result, (int, float)):
        return str(result)
    if isinstance(result, dict):
        value = result.get("value")
        if value is not None:
            return str(value).strip()
    return None


def extract_arg_value(
    args: Sequence[str],
    *,
    flags: Iterable[str],
    allow_positional: bool = False,
) -> Optional[str]:
    if not args:
        return None

    tokens = [str(tok) for tok in args if tok is not None]
    normalized_flags = {
        str(flag).strip().lower() for flag in flags if str(flag).strip()
    }
    if not normalized_flags:
        return None

    for idx, tok in enumerate(tokens):
        text = tok.strip()
        if not text:
            continue
        low = text.lower()
        if low in normalized_flags and idx + 1 < len(tokens):
            candidate = str(tokens[idx + 1]).strip()
            if candidate:
                return candidate
        if "=" in low:
            head, value = low.split("=", 1)
            if head in normalized_flags and value:
                return value.strip()

    if not allow_positional:
        return None

    for tok in tokens:
        text = str(tok).strip()
        if text and not text.startswith("-"):
            return text
    return None


def extract_value_arg(args: Sequence[str]) -> Optional[str]:
    return extract_arg_value(args, flags=VALUE_ARG_FLAGS, allow_positional=True)


def has_flag(args: Sequence[str], flag: str) -> bool:
    try:
        want = str(flag or "").strip().lower()
        if not want:
            return False
        return any(str(arg).strip().lower() == want for arg in (args or []))
    except Exception:
        return False


def normalize_to_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
