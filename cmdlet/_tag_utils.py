"""Tag-related utilities: parsing, normalization, template rendering, and extraction.

Covers the full tag lifecycle:
- Parsing raw tag arguments from command-line tokens
- Normalizing hash strings for tag identity
- Expanding tag group references from JSON config files
- Rendering tag value templates with ``#(placeholder)`` and ``<transform(...)>`` syntax
- Extracting tags, titles, and URLs from result objects (PipeObject/dict)
- Manipulating tag lists (namespace collapse, preferred titles, relationships)
"""

from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from SYS import models
from SYS.logger import log
from ._pipeobject_utils import merge_sequences

__all__ = [
    "set_tag_groups_path",
    "normalize_hash",
    "looks_like_hash",
    "parse_tag_arguments",
    "render_tag_value_templates",
    "build_tag_value_lookup",
    "_add_tag_values_to_lookup",
    "expand_tag_groups",
    "first_title_tag",
    "apply_preferred_title",
    "collapse_namespace_tags",
    "collect_relationship_labels",
    "extract_tag_from_result",
    "extract_title_from_result",
    "extract_url_from_result",
]

TAG_GROUPS_PATH: Optional[Path] = None

_TAG_GROUPS_CACHE: Optional[Dict[str, List[str]]] = None
_TAG_GROUPS_MTIME: Optional[float] = None

_TAG_VALUE_FUNCTION_RE = re.compile(r"<([a-zA-Z_][a-zA-Z0-9_-]*)\((.*?)\)>")
_BARE_FIELD_RE = re.compile(r"\(([A-Za-z][A-Za-z0-9_]*)\)")

# Transform catalog: single source of truth for completion help and the set of
# names the bare ``name(...)`` scanner treats as functions. The evaluator
# (``_apply_tag_value_function``) is the source of truth for behavior.
# ``args`` lists the argument kind at each position: value, number, format, cond.
# ``variadic`` means trailing args repeat the last kind (e.g. if2).
TAG_FUNCTIONS: Dict[str, Dict[str, Any]] = {
    "padding": {"aliases": ["pad", "zfill"], "args": ["number", "value"], "signature": "padding(width, $value)", "help": "Zero-pad value to width"},
    "default": {"aliases": [], "args": ["value", "value"], "signature": "default($value, fallback)", "help": "Use fallback when value is missing"},
    "replace": {"aliases": [], "args": ["value", "value", "value"], "signature": "replace($value, old, new)", "help": "Replace old with new"},
    "regex": {"aliases": ["sub"], "args": ["value", "value", "value"], "signature": "regex($value, pattern, replacement)", "help": "Regex substitution"},
    "increment": {"aliases": ["inc", "add"], "args": ["value", "number"], "min_args": 1, "signature": "increment($value, n)", "help": "Add n (default 1)"},
    "date": {"aliases": ["formatdate", "datefmt"], "args": ["value", "format"], "min_args": 1, "signature": "date($value, format)", "help": "Format a date"},
    "trim": {"aliases": ["strip"], "args": ["value"], "signature": "trim($value)", "help": "Strip whitespace"},
    "left": {"aliases": [], "args": ["value", "number"], "signature": "left($value, n)", "help": "First n characters"},
    "right": {"aliases": [], "args": ["value", "number"], "signature": "right($value, n)", "help": "Last n characters"},
    "cutleft": {"aliases": [], "args": ["value", "number"], "signature": "cutleft($value, n)", "help": "Drop first n characters"},
    "cutright": {"aliases": [], "args": ["value", "number"], "signature": "cutright($value, n)", "help": "Drop last n characters"},
    "if": {"aliases": ["iff", "when"], "args": ["cond", "value", "value"], "min_args": 2, "signature": "if($cond, then, else)", "help": "Pick then/else by condition"},
    "if2": {"aliases": ["coalesce"], "args": ["value"], "variadic": True, "min_args": 1, "signature": "if2($a, $b, ...)", "help": "First non-empty value"},
    "slug": {"aliases": ["sanitize"], "args": ["value"], "signature": "slug($value)", "help": "Filesystem-safe slug"},
    "lower": {"aliases": [], "args": ["value"], "signature": "lower($value)", "help": "Lowercase"},
    "upper": {"aliases": [], "args": ["value"], "signature": "upper($value)", "help": "Uppercase"},
    "caps": {"aliases": ["titlecase"], "args": ["value"], "signature": "caps($value)", "help": "Title case"},
}


def tag_function_aliases() -> Dict[str, str]:
    """Map every function name and alias to its canonical name."""
    mapping: Dict[str, str] = {}
    for canonical, spec in TAG_FUNCTIONS.items():
        mapping[canonical] = canonical
        for alias in spec.get("aliases") or []:
            mapping[alias] = canonical
    return mapping


@lru_cache(maxsize=1)
def _known_tag_function_names() -> frozenset[str]:
    return frozenset(tag_function_aliases().keys())


@lru_cache(maxsize=1)
def _bare_tag_function_re() -> re.Pattern[str]:
    names = sorted(re.escape(name) for name in _known_tag_function_names())
    if not names:
        return re.compile(r"(?!x)x")
    return re.compile(rf"(?<![\w])({'|'.join(names)})\(", re.IGNORECASE)


def tag_function_completions() -> List[Tuple[str, str, str]]:
    """Return ``(name, signature, help)`` for every canonical transform."""
    out: List[Tuple[str, str, str]] = []
    for canonical, spec in TAG_FUNCTIONS.items():
        out.append(
            (
                canonical,
                str(spec.get("signature") or canonical),
                str(spec.get("help") or ""),
            )
        )
    return out


def tag_function_signature(name: str) -> Optional[str]:
    """Return the display signature for a canonical name or alias, else None."""
    canonical = tag_function_aliases().get(str(name or "").strip().lower())
    if canonical is None:
        return None
    spec = TAG_FUNCTIONS.get(canonical)
    return str(spec.get("signature") or canonical) if spec else canonical


def tag_function_arg_kinds(name: str) -> List[str]:
    """Return the argument kinds (value/number/format/cond) for a function."""
    canonical = tag_function_aliases().get(str(name or "").strip().lower())
    spec = TAG_FUNCTIONS.get(canonical) if canonical else None
    if not spec:
        return []
    return [str(kind) for kind in (spec.get("args") or [])]


def tag_function_variadic(name: str) -> bool:
    canonical = tag_function_aliases().get(str(name or "").strip().lower())
    spec = TAG_FUNCTIONS.get(canonical) if canonical else None
    return bool(spec and spec.get("variadic"))


def tag_function_min_args(name: str) -> int:
    canonical = tag_function_aliases().get(str(name or "").strip().lower())
    spec = TAG_FUNCTIONS.get(canonical) if canonical else None
    if not spec:
        return 0
    kinds = spec.get("args") or []
    if spec.get("min_args") is not None:
        try:
            return int(spec.get("min_args"))
        except Exception:
            pass
    return 1 if spec.get("variadic") else len(kinds)


class TagFunctionError(ValueError):
    """Raised when a tag transform is present but not a valid typed call."""


def _tag_identifier_before(text: str, index: int) -> Optional[str]:
    j = index - 1
    while j >= 0 and (text[j].isalnum() or text[j] in {"_", "-"}):
        j -= 1
    candidate = text[j + 1 : index]
    if candidate and (candidate[0].isalpha() or candidate[0] == "_"):
        return candidate
    return None


def _last_arg_partial(body: str) -> str:
    depth = 0
    quote: Optional[str] = None
    escape = False
    last_comma = -1
    for idx, ch in enumerate(body):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in {"'", '"'}:
            quote = ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            last_comma = idx
    return body[last_comma + 1 :].strip() if last_comma >= 0 else body.strip()


def _count_top_level_commas(body: str) -> int:
    depth = 0
    quote: Optional[str] = None
    escape = False
    count = 0
    for ch in body:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in {"'", '"'}:
            quote = ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            count += 1
    return count


def find_open_tag_function(text: str) -> Optional[tuple[str, int, str]]:
    """Return ``(canonical_name, arg_index, current_arg_partial)`` when *text*
    ends inside an open known function call, else ``None``.

    ``arg_index`` is the number of top-level commas already typed (0 = first
    argument), and ``current_arg_partial`` is the text after the last top-level
    comma (or the opening parenthesis).
    """
    names = _known_tag_function_names()
    aliases = tag_function_aliases()
    stack: List[tuple[int, Optional[str]]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in {"'", '"'}:
            quote = ch
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "(":
            name = _tag_identifier_before(text, i)
            known = name if (name is not None and name.lower() in names) else None
            stack.append((i, known))
        elif ch == ")":
            if stack:
                stack.pop()
        i += 1
    if not stack:
        return None
    open_idx, name = stack[-1]
    if name is None:
        return None
    canonical = aliases.get(name.lower(), name)
    body = text[open_idx + 1 :]
    return canonical, _count_top_level_commas(body), _last_arg_partial(body)


def tag_value_has_function(text: str) -> bool:
    if _TAG_VALUE_FUNCTION_RE.search(text):
        return True
    return _bare_tag_function_re().search(text) is not None


@lru_cache(maxsize=16)
def _compile_tag_placeholder_re(prefixes: tuple[str, ...]) -> re.Pattern[str]:
    alts = [re.escape(p) for p in prefixes if p]
    if not alts:
        alts = [re.escape("$")]
    return re.compile(rf"(?:{'|'.join(alts)})\(([^)]+)\)")


def tag_placeholder_pattern() -> re.Pattern[str]:
    try:
        from SYS.config import get_tag_placeholder_prefixes

        prefixes = get_tag_placeholder_prefixes()
    except Exception:
        prefixes = ("$", "#")
    return _compile_tag_placeholder_re(tuple(prefixes))


def wrap_tag_placeholder(inner: str) -> str:
    try:
        from SYS.config import wrap_tag_placeholder as _wrap

        return _wrap(inner)
    except Exception:
        return f"$({inner})"


def _expand_bare_tag_placeholders(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return raw
    try:
        from SYS.config import get_tag_placeholder_prefixes

        prefixes = get_tag_placeholder_prefixes()
    except Exception:
        prefixes = ("$", "#")
    primary = prefixes[0] if prefixes else "$"

    def _repl(match: re.Match[str]) -> str:
        start = match.start()
        for prefix in prefixes:
            if prefix and start >= len(prefix) and raw[start - len(prefix):start] == prefix:
                return match.group(0)
        return f"{primary}({match.group(1)})"

    return _BARE_FIELD_RE.sub(_repl, raw)


def set_tag_groups_path(path: Path) -> None:
    """Set the path to the tag groups JSON file."""
    global TAG_GROUPS_PATH
    TAG_GROUPS_PATH = path


@lru_cache(maxsize=4096)
def _normalize_hash_cached(hash_hex: str) -> Optional[str]:
    text = hash_hex.strip().lower()
    if not text:
        return None
    if len(text) != 64:
        return None
    if not all(ch in "0123456789abcdef" for ch in text):
        return None
    return text


def normalize_hash(hash_hex: Optional[str]) -> Optional[str]:
    """Normalize a hash string to lowercase, or return None if invalid.

    Args:
            hash_hex: String that should be a hex hash

    Returns:
            Lowercase hash string, or None if input is not a string or is empty
    """
    if not isinstance(hash_hex, str):
        return None
    return _normalize_hash_cached(hash_hex)


def looks_like_hash(candidate: Optional[str]) -> bool:
    """Check if a string looks like a SHA256 hash (64 hex chars).

    Args:
            candidate: String to test

    Returns:
            True if the string is 64 lowercase hex characters
    """
    if not isinstance(candidate, str):
        return False
    text = candidate.strip().lower()
    return len(text) == 64 and all(ch in "0123456789abcdef" for ch in text)


def _normalize_tag_value_template_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    try:
        text = re.sub(r"\s+", " ", text).strip()
    except Exception:
        text = " ".join(text.split())
    return text


def _tag_value_template_keys(value: Any) -> list[str]:
    normalized = _normalize_tag_value_template_name(value)
    if not normalized:
        return []

    keys = [normalized]

    trimmed_hash = re.sub(r"\s*#+\s*$", "", normalized).strip()
    if trimmed_hash and trimmed_hash not in keys:
        keys.append(trimmed_hash)

    return keys


def _add_tag_values_to_lookup(lookup: Dict[str, List[str]], tag_text: Any) -> None:
    text = str(tag_text or "").strip()
    if not text or ":" not in text:
        return
    if tag_placeholder_pattern().search(text) or tag_value_has_function(text):
        return

    namespace, value = text.split(":", 1)
    value_text = str(value or "").strip()
    if not value_text:
        return

    for key in _tag_value_template_keys(namespace):
        values = lookup.setdefault(key, [])
        if value_text not in values:
            values.append(value_text)


def build_tag_value_lookup(
    tags: Optional[Iterable[Any]],
    *,
    result: Any = None,
) -> Dict[str, List[str]]:
    """Build a placeholder lookup from existing tags and lightweight result fields.

    Placeholder lookups use ``#(namespace)`` syntax. Namespace matching is
    case-insensitive and trims repeated whitespace. A trailing ``#`` in the
    placeholder is ignored so inputs like ``#(track #)`` can resolve ``track:9``.
    """

    lookup: Dict[str, List[str]] = {}
    for tag in tags or []:
        _add_tag_values_to_lookup(lookup, tag)

    title_text = extract_title_from_result(result)
    if title_text:
        _add_tag_values_to_lookup(lookup, f"title:{title_text}")

    return lookup


def _split_tag_value_function_args(value: Any) -> list[str]:
    text = str(value or "")
    args: list[str] = []
    current: list[str] = []
    depth = 0
    quote: Optional[str] = None
    escape = False

    for ch in text:
        if escape:
            current.append(ch)
            escape = False
            continue
        if ch == "\\":
            current.append(ch)
            escape = True
            continue
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in {"'", '"'}:
            current.append(ch)
            quote = ch
            continue
        if ch in {"(", "[", "{"}:
            depth += 1
            current.append(ch)
            continue
        if ch in {")", "]", "}"}:
            depth = max(0, depth - 1)
            current.append(ch)
            continue
        if ch == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
            continue
        current.append(ch)

    tail = "".join(current).strip()
    if tail or args:
        args.append(tail)
    return args


def _strip_tag_value_function_arg(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _padding_width_from_spec(value: Any) -> Optional[int]:
    spec = _strip_tag_value_function_arg(value)
    if not spec:
        return None
    if re.fullmatch(r"0+", spec):
        return len(spec)
    if spec.isdigit():
        try:
            width = int(spec)
        except Exception:
            return None
        return width if width > 0 else None
    return None


def _replace_tag_value_placeholders(
    value: Any,
    lookup: Dict[str, List[str]],
    *,
    preserve_unresolved: bool,
) -> tuple[str, bool]:
    text = str(value or "")
    unresolved = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal unresolved
        inner = match.group(1) or ""
        parts = _split_tag_value_function_args(inner)
        name = parts[0] if parts else inner
        fmt = _strip_tag_value_function_arg(parts[1]) if len(parts) > 1 else ""
        keys = _tag_value_template_keys(name)
        values: List[str] = []
        for key in keys:
            for candidate in lookup.get(key, []):
                if candidate not in values:
                    values.append(candidate)
        if not values:
            unresolved = True
            return match.group(0) if preserve_unresolved else ""
        if fmt and re.fullmatch(r"[+-]?\d+", fmt.strip()):
            try:
                index = int(fmt.strip())
            except Exception:
                index = 0
            if index < 0:
                index = len(values) + index + 1
            if not (1 <= index <= len(values)):
                unresolved = True
                return match.group(0) if preserve_unresolved else ""
            return values[index - 1]
        text = ", ".join(values)
        if fmt:
            parsed = _parse_tag_date(text)
            if parsed is not None:
                text = _format_tag_date(parsed, fmt)
        return text

    return tag_placeholder_pattern().sub(_replace, text), unresolved


def _coerce_tag_value_integer(value: Any) -> Optional[int]:
    text = _strip_tag_value_function_arg(value)
    if not text:
        return None
    if not re.fullmatch(r"[+-]?\d+", text):
        return None
    try:
        return int(text)
    except Exception:
        return None


def _parse_tag_date(text: str) -> Optional[tuple[str, str, str]]:
    compact = re.sub(r"\D", "", str(text or ""))
    if len(compact) == 8 and compact.isdigit():
        digits = [compact[:4], compact[4:6], compact[6:8]]
    else:
        digits = re.findall(r"\d+", str(text or ""))
    if len(digits) < 3:
        return None
    a, b, c = digits[0], digits[1], digits[2]
    try:
        if len(a) == 4:
            year, month, day = a, b, c
        elif len(c) == 4:
            year, month, day = c, a, b
        else:
            year_n = int(c)
            year = str(2000 + year_n if year_n < 100 else year_n)
            month, day = a, b
        month_i = int(month)
        day_i = int(day)
        if not (1 <= month_i <= 12 and 1 <= day_i <= 31):
            return None
        return str(int(year)), f"{month_i:02d}", f"{day_i:02d}"
    except Exception:
        return None


def _format_tag_date(parts: tuple[str, str, str], fmt: str) -> str:
    year, month, day = parts
    yy = year[-2:] if year else ""
    months_full = (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    )
    months_abbr = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )
    try:
        month_i = int(month)
        month_full = months_full[month_i - 1] if 1 <= month_i <= 12 else month
        month_abbr = months_abbr[month_i - 1] if 1 <= month_i <= 12 else month
    except Exception:
        month_full = month
        month_abbr = month
    out = str(fmt or "YYYY-MM-DD")
    out = out.replace("YYYY", year).replace("yyyy", year)
    out = out.replace("YY", yy).replace("yy", yy)
    out = out.replace("MMMM", month_full).replace("MMM", month_abbr)
    out = out.replace("MM", month).replace("mm", month)
    out = out.replace("DD", day).replace("dd", day)
    return out


def _apply_tag_value_function(
    name: str,
    args: Sequence[str],
    *,
    lookup: Dict[str, List[str]],
) -> Optional[str]:
    func = str(name or "").strip().lower()

    resolved_values: list[str] = []
    unresolved_flags: list[bool] = []
    for arg in args:
        rendered, unresolved = _replace_tag_value_placeholders(
            arg,
            lookup,
            preserve_unresolved=True,
        )
        resolved_values.append(_strip_tag_value_function_arg(rendered))
        unresolved_flags.append(unresolved)

    aliases = tag_function_aliases()
    canonical = aliases.get(func)
    if canonical:
        kinds = tag_function_arg_kinds(canonical)
        min_args = tag_function_min_args(canonical)
        variadic = tag_function_variadic(canonical)
        count = len(resolved_values)
        signature = tag_function_signature(canonical) or canonical
        max_args = None if variadic else len(kinds)
        if count < min_args or (max_args is not None and count > max_args):
            raise TagFunctionError(
                f"{canonical}() expected {signature}, got {count} argument(s)"
            )
        check_kinds = list(kinds)
        if variadic and count > len(check_kinds):
            last = check_kinds[-1] if check_kinds else "value"
            check_kinds = check_kinds + [last] * (count - len(check_kinds))
        for index, kind in enumerate(check_kinds):
            if index >= count or unresolved_flags[index]:
                continue
            if kind != "number":
                continue
            if _coerce_tag_value_integer(resolved_values[index]) is None:
                raise TagFunctionError(
                    f"{canonical}() argument {index + 1} must be a number, got {resolved_values[index]!r} (expected {signature})"
                )

    if func in {"padding", "pad", "zfill"}:
        if len(resolved_values) != 2 or any(unresolved_flags):
            return None
        width = _padding_width_from_spec(resolved_values[0])
        if width is None:
            return None
        return str(resolved_values[1]).zfill(width)

    if func == "default":
        if len(resolved_values) != 2:
            return None
        primary = resolved_values[0]
        fallback = resolved_values[1]
        if not unresolved_flags[0] and str(primary).strip():
            return str(primary)
        if unresolved_flags[1]:
            return None
        return str(fallback)

    if func == "replace":
        if len(resolved_values) != 3 or any(unresolved_flags):
            return None
        return str(resolved_values[0]).replace(
            str(resolved_values[1]),
            str(resolved_values[2]),
        )

    if func in {"regex", "sub"}:
        if len(resolved_values) != 3 or any(unresolved_flags):
            return None
        try:
            pattern = str(resolved_values[1])
            replacement = str(resolved_values[2])
            return re.sub(pattern, replacement, str(resolved_values[0]))
        except re.error:
            return None

    if func in {"increment", "inc", "add"}:
        if len(resolved_values) not in {1, 2}:
            return None
        if unresolved_flags[0]:
            return None
        base_value = _coerce_tag_value_integer(resolved_values[0])
        if base_value is None:
            return None
        step_value = 1
        if len(resolved_values) == 2:
            if unresolved_flags[1]:
                return None
            parsed_step = _coerce_tag_value_integer(resolved_values[1])
            if parsed_step is None:
                return None
            step_value = parsed_step
        return str(base_value + step_value)

    if func in {"date", "formatdate", "datefmt"}:
        if not resolved_values or unresolved_flags[0]:
            return None
        parsed = _parse_tag_date(resolved_values[0])
        if parsed is None:
            return None
        fmt = ""
        if len(resolved_values) >= 2 and not unresolved_flags[1]:
            fmt = str(resolved_values[1] or "").strip()
        if not fmt:
            try:
                from SYS.config import get_date_format

                fmt = str(get_date_format() or "YYYY-MM-DD")
            except Exception:
                fmt = "YYYY-MM-DD"
        return _format_tag_date(parsed, fmt)

    if func in {"trim", "strip"}:
        if len(resolved_values) != 1 or unresolved_flags[0]:
            return None
        return str(resolved_values[0]).strip()

    if func in {"left", "right", "cutleft", "cutright"}:
        if len(resolved_values) != 2 or any(unresolved_flags):
            return None
        count = _coerce_tag_value_integer(resolved_values[1])
        if count is None or count < 0:
            return None
        text = str(resolved_values[0])
        if func == "left":
            return text[:count]
        if func == "right":
            return text[-count:] if count else ""
        if func == "cutleft":
            return text[count:]
        return text[:-count] if count else text

    if func in {"if", "iff", "when"}:
        if len(resolved_values) not in {2, 3}:
            return None
        cond = "" if unresolved_flags[0] else str(resolved_values[0]).strip()
        then_value = resolved_values[1] if len(resolved_values) > 1 else ""
        else_value = resolved_values[2] if len(resolved_values) > 2 else ""
        chosen = then_value if cond else else_value
        chosen_unresolved = unresolved_flags[1] if cond else (
            unresolved_flags[2] if len(unresolved_flags) > 2 else False
        )
        if chosen_unresolved:
            return None
        return str(chosen)

    if func in {"if2", "coalesce"}:
        if not resolved_values:
            return None
        for text, missing in zip(resolved_values, unresolved_flags):
            if missing:
                continue
            value = str(text or "").strip()
            if value:
                return str(text)
        return None

    if func in {"slug", "sanitize"}:
        if len(resolved_values) != 1 or unresolved_flags[0]:
            return None
        text = str(resolved_values[0]).strip().lower()
        text = re.sub(r"[^a-z0-9]+", "_", text)
        return text.strip("_")

    if func == "lower":
        if len(resolved_values) != 1 or unresolved_flags[0]:
            return None
        return str(resolved_values[0]).lower()

    if func == "upper":
        if len(resolved_values) != 1 or unresolved_flags[0]:
            return None
        return str(resolved_values[0]).upper()

    if func in {"caps", "titlecase"}:
        if len(resolved_values) != 1 or unresolved_flags[0]:
            return None
        return str(resolved_values[0]).title()

    return None


_FUNCTION_NAME_HEAD_RE = re.compile(r"<([a-zA-Z_][a-zA-Z0-9_-]*)\(")


def _match_call_parens(text: str, open_paren: int) -> Optional[tuple[int, str]]:
    """Match balanced parentheses starting at ``open_paren``.

    Returns ``(index_after_close, args_body)`` where ``args_body`` excludes the
    surrounding parentheses. Quotes and backslash escapes are respected.
    """
    depth = 1
    quote: Optional[str] = None
    escape = False
    j = open_paren
    while j < len(text):
        ch = text[j]
        if escape:
            escape = False
            j += 1
            continue
        if ch == "\\":
            escape = True
            j += 1
            continue
        if quote:
            if ch == quote:
                quote = None
            j += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            j += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return j + 1, text[open_paren:j]
        j += 1
    return None


def _scan_tag_value_function(
    text: str,
    start: int = 0,
) -> Optional[tuple[int, int, str, str]]:
    """Locate the next transform with balanced brackets.

    Supports both the angle-bracket form ``<name(args)>`` and the bare
    Python-style form ``name(args)`` (where ``name`` is a known function).
    Returns ``(start, end, name, args_body)`` for the leftmost match.
    """

    angle: Optional[tuple[int, int, str, str]] = None
    pos = start
    while True:
        lt = text.find("<", pos)
        if lt == -1:
            break
        head = _FUNCTION_NAME_HEAD_RE.match(text, lt)
        if head is None:
            pos = lt + 1
            continue
        name = head.group(1)
        open_paren = head.end()
        matched = _match_call_parens(text, open_paren)
        if matched is None:
            pos = lt + 1
            continue
        end, args_body = matched
        k = end
        while k < len(text) and text[k].isspace():
            k += 1
        if k < len(text) and text[k] == ">":
            k += 1
        angle = (lt, k, name, args_body)
        break

    bare: Optional[tuple[int, int, str, str]] = None
    for match in _bare_tag_function_re().finditer(text, start):
        name = match.group(1)
        open_paren = match.end()
        matched = _match_call_parens(text, open_paren)
        if matched is None:
            continue
        end, args_body = matched
        bare = (match.start(), end, name, args_body)
        break

    if angle is None:
        return bare
    if bare is None:
        return angle
    return angle if angle[0] <= bare[0] else bare


def _render_tag_value_function_templates(
    value: Any,
    *,
    lookup: Dict[str, List[str]],
) -> tuple[str, bool]:
    text = str(value or "")
    unresolved = False

    previous = None
    rendered = text
    while previous != rendered:
        previous = rendered
        found = _scan_tag_value_function(rendered)
        if found is None:
            break

        out: list[str] = []
        cursor = 0
        while found is not None:
            start, end, func_name, args_body = found
            out.append(rendered[cursor:start])
            func_args = _split_tag_value_function_args(args_body)
            try:
                result = _apply_tag_value_function(
                    func_name,
                    func_args,
                    lookup=lookup,
                )
            except TagFunctionError:
                unresolved = True
                out.append(rendered[start:end])
                cursor = end
                break
            if result is None:
                unresolved = True
                out.append(rendered[start:end])
            else:
                out.append(result)
            cursor = end
            found = _scan_tag_value_function(rendered, end)
        if unresolved:
            break
        out.append(rendered[cursor:])
        rendered = "".join(out)

    if tag_value_has_function(rendered):
        unresolved = True

    return rendered, unresolved


def render_tag_value_templates(
    tags: Sequence[Any],
    *,
    existing_tags: Optional[Iterable[Any]] = None,
    result: Any = None,
) -> tuple[list[str], list[str]]:
    """Resolve ``#(namespace)`` placeholders and ``<transform(...)>`` functions.

    Returns ``(resolved_tags, unresolved_templates)``. Tags whose placeholders
    cannot be fully resolved are omitted from ``resolved_tags`` and returned in
    ``unresolved_templates`` so callers can warn or summarize skipped items.

    Currently supported transforms (angle form ``<trim($x)>`` and bare form
    ``trim($x)`` are both accepted):
    - ``padding($episode, 2)`` / ``pad`` / ``zfill``
    - ``default($season, 0)``
    - ``replace($title, old, new)``
    - ``regex($title, pattern, replacement)`` (``sub`` alias)
    - ``increment($episode, 1)`` (``inc`` / ``add``)
    - ``date($date, MM/DD/YYYY)``
    - ``if($season, s$season, )`` / ``if2(a, b, c)``
    - ``trim`` / ``left`` / ``right`` / ``cutleft`` / ``cutright``
    - ``slug`` / ``lower`` / ``upper`` / ``caps``
    """

    entries: list[dict[str, Any]] = []
    lookup = build_tag_value_lookup(existing_tags, result=result)

    for raw_tag in tags or []:
        text = str(raw_tag or "").strip()
        if not text:
            continue
        if ":" in text:
            namespace, value = text.split(":", 1)
            if namespace.strip() and value:
                value = _expand_bare_tag_placeholders(value)
                text = f"{namespace}:{value}"
        has_template = bool(
            tag_placeholder_pattern().search(text)
            or tag_value_has_function(text)
        )
        entry = {
            "raw": text,
            "resolved": None,
            "has_template": has_template,
        }
        if not has_template:
            entry["resolved"] = text
            _add_tag_values_to_lookup(lookup, text)
        entries.append(entry)

    progress = True
    while progress:
        progress = False
        for entry in entries:
            if entry["resolved"] is not None or not entry["has_template"]:
                continue

            rendered, unresolved = _replace_tag_value_placeholders(
                entry["raw"],
                lookup,
                preserve_unresolved=bool(tag_value_has_function(str(entry["raw"]))),
            )

            rendered, function_unresolved = _render_tag_value_function_templates(
                rendered,
                lookup=lookup,
            )
            if function_unresolved:
                continue

            if unresolved and tag_placeholder_pattern().search(rendered):
                continue

            rendered = rendered.strip()
            if not rendered:
                entry["resolved"] = ""
                progress = True
                continue

            entry["resolved"] = rendered
            _add_tag_values_to_lookup(lookup, rendered)
            progress = True

    resolved_tags = merge_sequences(
        [entry["resolved"] for entry in entries if isinstance(entry.get("resolved"), str) and entry.get("resolved")],
        case_sensitive=True,
    )
    unresolved_templates = [
        str(entry["raw"])
        for entry in entries
        if entry["has_template"] and not entry.get("resolved")
    ]
    return resolved_tags, unresolved_templates


def _normalize_tag_group_entry(value: Any) -> Optional[str]:
    """Internal: Normalize a single tag group entry."""
    if not isinstance(value, str):
        value = str(value)
    text = value.strip()
    return text or None


def _load_tag_groups() -> Dict[str, List[str]]:
    """Load tag group definitions from JSON file with caching."""
    global _TAG_GROUPS_CACHE, _TAG_GROUPS_MTIME, TAG_GROUPS_PATH

    if TAG_GROUPS_PATH is None:
        try:
            script_dir = Path(__file__).parent.parent

            candidate = script_dir / "adjective.json"
            if candidate.exists():
                TAG_GROUPS_PATH = candidate
            else:
                candidate = script_dir / "helper" / "adjective.json"
                if candidate.exists():
                    TAG_GROUPS_PATH = candidate
        except Exception:
            pass

    if TAG_GROUPS_PATH is None:
        return {}

    path = TAG_GROUPS_PATH
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        _TAG_GROUPS_CACHE = {}
        _TAG_GROUPS_MTIME = None
        return {}
    except OSError as exc:
        log(f"Failed to read tag groups: {exc}", file=sys.stderr)
        _TAG_GROUPS_CACHE = {}
        _TAG_GROUPS_MTIME = None
        return {}

    mtime = stat_result.st_mtime
    if _TAG_GROUPS_CACHE is not None and _TAG_GROUPS_MTIME == mtime:
        return _TAG_GROUPS_CACHE

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"Invalid tag group JSON ({path}): {exc}", file=sys.stderr)
        _TAG_GROUPS_CACHE = {}
        _TAG_GROUPS_MTIME = mtime
        return {}

    groups: Dict[str, List[str]] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if not isinstance(key, str):
                continue
            name = key.strip().lower()
            if not name:
                continue
            members: List[str] = []
            if isinstance(value, list):
                for entry in value:
                    normalized = _normalize_tag_group_entry(entry)
                    if normalized:
                        members.append(normalized)
            elif isinstance(value, str):
                normalized = _normalize_tag_group_entry(value)
                if normalized:
                    members.extend(
                        token.strip() for token in normalized.split(",")
                        if token.strip()
                    )
            if members:
                groups[name] = members

    _TAG_GROUPS_CACHE = groups
    _TAG_GROUPS_MTIME = mtime
    return groups


def expand_tag_groups(raw_tags: Iterable[str]) -> List[str]:
    """Expand tag group references (e.g., {my_group}) into member tags.

    Tag groups are defined in JSON and can be nested. Groups are referenced
    with curly braces: {group_name}.

    Args:
            raw_tags: Sequence of tag strings, some may reference groups like "{group_name}"

    Returns:
            List of expanded tags with group references replaced
    """
    groups = _load_tag_groups()
    if not groups:
        return [tag for tag in raw_tags if isinstance(tag, str) and tag.strip()]

    def _expand(tokens: Iterable[str], seen: Set[str]) -> List[str]:
        result: List[str] = []
        for token in tokens:
            if not isinstance(token, str):
                continue
            candidate = token.strip()
            if not candidate:
                continue
            if candidate.startswith("{") and candidate.endswith("}") and len(candidate) > 2:
                name = candidate[1:-1].strip().lower()
                if not name:
                    continue
                if name in seen:
                    log(
                        f"Tag group recursion detected for {{{name}}}; skipping",
                        file=sys.stderr,
                    )
                    continue
                members = groups.get(name)
                if not members:
                    log(f"Unknown tag group {{{name}}}", file=sys.stderr)
                    result.append(candidate)
                    continue
                result.extend(_expand(members, seen | {name}))
            else:
                result.append(candidate)
        return result

    return _expand(raw_tags, set())


def parse_tag_arguments(arguments: Sequence[str]) -> List[str]:
    """Parse tag arguments from command line tokens.

    - Supports comma-separated tags.
    - Supports pipe namespace shorthand: "artist:A|B|C" -> artist:A, artist:B, artist:C.

    Args:
            arguments: Sequence of argument strings

    Returns:
            List of normalized tag strings (empty strings filtered out)
    """

    def _split_top_level_commas(text: str) -> List[str]:
        segments: List[str] = []
        current: List[str] = []
        paren_depth = 0
        angle_depth = 0
        quote: Optional[str] = None
        escape = False

        for ch in text:
            if escape:
                current.append(ch)
                escape = False
                continue
            if ch == "\\":
                current.append(ch)
                escape = True
                continue
            if quote:
                current.append(ch)
                if ch == quote:
                    quote = None
                continue
            if ch in {"'", '"'}:
                current.append(ch)
                quote = ch
                continue
            if ch == "(":
                paren_depth += 1
                current.append(ch)
                continue
            if ch == ")":
                paren_depth = max(0, paren_depth - 1)
                current.append(ch)
                continue
            if ch == "<":
                angle_depth += 1
                current.append(ch)
                continue
            if ch == ">":
                angle_depth = max(0, angle_depth - 1)
                current.append(ch)
                continue
            if ch == "," and paren_depth == 0 and angle_depth == 0:
                segments.append("".join(current).strip())
                current = []
                continue
            current.append(ch)

        tail = "".join(current).strip()
        if tail or segments:
            segments.append(tail)
        return segments

    def _expand_pipe_namespace(text: str) -> List[str]:
        parts = text.split("|")
        expanded: List[str] = []
        last_ns: Optional[str] = None
        for part in parts:
            segment = part.strip()
            if not segment:
                continue
            if ":" in segment:
                ns, val = segment.split(":", 1)
                ns = ns.strip()
                val = val.strip()
                last_ns = ns or last_ns
                if last_ns and val:
                    expanded.append(f"{last_ns}:{val}")
                elif ns or val:
                    expanded.append(f"{ns}:{val}".strip(":"))
            else:
                if last_ns:
                    expanded.append(f"{last_ns}:{segment}")
                else:
                    expanded.append(segment)
        return expanded

    tags: List[str] = []
    for argument in arguments:
        for token in _split_top_level_commas(str(argument)):
            text = token.strip()
            if not text:
                continue
            pipe_expanded = _expand_pipe_namespace(text)
            for entry in pipe_expanded:
                candidate = entry.strip()
                if not candidate:
                    continue
                if ":" in candidate:
                    ns, val = candidate.split(":", 1)
                    ns = ns.strip()
                    val = val.strip()
                    candidate = f"{ns}:{val}" if ns or val else ""
                if candidate:
                    tags.append(candidate)
    return tags


def first_title_tag(source: Optional[Iterable[str]]) -> Optional[str]:
    """Find the first tag starting with "title:" in a collection.

    Args:
            source: Iterable of tag strings

    Returns:
            First title: tag found, or None
    """
    if not source:
        return None
    for item in source:
        if not isinstance(item, str):
            continue
        candidate = item.strip()
        if candidate and candidate.lower().startswith("title:"):
            return candidate
    return None


def apply_preferred_title(tags: List[str], preferred: Optional[str]) -> List[str]:
    """Replace any title: tags with a preferred title tag.

    Args:
            tags: List of tags (may contain multiple "title:" entries)
            preferred: Preferred title tag to use (full "title: ..." format)

    Returns:
            List with old title tags removed and preferred title added (at most once)
    """
    if not preferred:
        return tags
    preferred_clean = preferred.strip()
    if not preferred_clean:
        return tags
    preferred_lower = preferred_clean.lower()
    filtered: List[str] = []
    has_preferred = False
    for tag in tags:
        candidate = tag.strip()
        if not candidate:
            continue
        if candidate.lower().startswith("title:"):
            if candidate.lower() == preferred_lower:
                if not has_preferred:
                    filtered.append(candidate)
                    has_preferred = True
            continue
        filtered.append(candidate)
    if not has_preferred:
        filtered.append(preferred_clean)
    return filtered


def collapse_namespace_tags(
    tags: Optional[Iterable[Any]],
    namespace: str,
    prefer: str = "last",
) -> list[str]:
    """Reduce tags so only one entry for a given namespace remains.

    Keeps either the first or last occurrence (default last) while preserving overall order
    for non-matching tags. Useful for ensuring a single title: tag.
    """
    if not tags:
        return []
    ns = str(namespace or "").strip().lower()
    if not ns:
        return list(tags) if isinstance(tags, list) else list(tags)

    prefer_last = str(prefer or "last").lower() != "first"
    ns_prefix = ns + ":"

    items = list(tags)
    if prefer_last:
        kept: list[str] = []
        seen_ns = False
        for tag in reversed(items):
            text = str(tag)
            if text.lower().startswith(ns_prefix):
                if seen_ns:
                    continue
                seen_ns = True
            kept.append(text)
        kept.reverse()
        return kept
    else:
        kept_ns = False
        result: list[str] = []
        for tag in items:
            text = str(tag)
            if text.lower().startswith(ns_prefix):
                if kept_ns:
                    continue
                kept_ns = True
            result.append(text)
        return result


def collect_relationship_labels(
    payload: Any,
    label_stack: List[str] | None = None,
    mapping: Dict[str, str] | None = None,
) -> Dict[str, str]:
    """Recursively extract hash-to-label mappings from nested relationship data.

    Walks through nested dicts/lists looking for sha256-like strings (64 hex chars)
    and builds a mapping from hash to its path in the structure.

    Example:
            data = {
                    "duplicates": [
                            "abc123...",  # Will be mapped to "duplicates"
                            {"type": "related", "items": ["def456..."]}  # Will be mapped to "duplicates / type / items"
                    ]
            }
            result = collect_relationship_labels(data)
            # result = {"abc123...": "duplicates", "def456...": "duplicates / type / items"}

    Args:
            payload: Nested data structure (dict, list, string, etc.)
            label_stack: Internal use - tracks path during recursion
            mapping: Internal use - accumulates hash->label mappings

    Returns:
            Dict mapping hash strings to their path labels
    """
    if label_stack is None:
        label_stack = []
    if mapping is None:
        mapping = {}

    if isinstance(payload, dict):
        for key, value in payload.items():
            next_stack = label_stack
            if isinstance(key, str) and key:
                formatted = key.replace("_", " ").strip()
                next_stack = label_stack + [formatted]
            collect_relationship_labels(value, next_stack, mapping)
    elif isinstance(payload, (list, tuple, set)):
        for value in payload:
            collect_relationship_labels(value, label_stack, mapping)
    elif isinstance(payload, str) and looks_like_hash(payload):
        hash_value = payload.lower()
        if label_stack:
            label = " / ".join(item for item in label_stack if item)
        else:
            label = "related"
        mapping.setdefault(hash_value, label)

    return mapping


def extract_tag_from_result(result: Any) -> list[str]:
    """Extract all tags from a result dict or PipeObject.

    Handles mixed types (lists, sets, strings) and various field names.
    """
    tag: list[str] = []

    def _extend(candidate: Any) -> None:
        if not candidate:
            return
        if isinstance(candidate, (list, set, tuple)):
            tag.extend(str(t) for t in candidate if t is not None)
        elif isinstance(candidate, str):
            tag.append(candidate)

    if isinstance(result, models.PipeObject):
        tag.extend(result.tag or [])
        if isinstance(result.extra, dict):
            _extend(result.extra.get("tag"))
        if isinstance(result.metadata, dict):
            _extend(result.metadata.get("tag"))
            _extend(result.metadata.get("tags"))
    elif hasattr(result, "tag"):
        _extend(getattr(result, "tag"))

    if isinstance(result, dict):
        _extend(result.get("tag"))
        _extend(result.get("tags"))

        extra = result.get("extra")
        if isinstance(extra, dict):
            _extend(extra.get("tag"))
            _extend(extra.get("tags"))

        fm = result.get("full_metadata") or result.get("metadata")
        if isinstance(fm, dict):
            _extend(fm.get("tag"))
            _extend(fm.get("tags"))

    return merge_sequences(tag, case_sensitive=True)


def extract_title_from_result(result: Any) -> Optional[str]:
    """Extract the title from a result dict or PipeObject."""
    if isinstance(result, models.PipeObject):
        return result.title
    elif hasattr(result, "title"):
        return getattr(result, "title")
    elif isinstance(result, dict):
        return result.get("title")
    return None


def extract_url_from_result(result: Any) -> list[str]:
    """Extract all unique URLs from a result dict or PipeObject.

    Handles mixed types (lists, strings) and various field names (url, source_url, webpage_url).
    Centralizes extraction logic for cmdlets like download-file, add-file, get-url.
    """
    url: list[str] = []

    def _extend(candidate: Any) -> None:
        if not candidate:
            return
        if isinstance(candidate, list):
            url.extend(candidate)
        elif isinstance(candidate, str):
            url.append(candidate)

    if isinstance(result, models.PipeObject):
        _extend(result.url)
        _extend(result.source_url)
        if isinstance(result.extra, dict):
            _extend(result.extra.get("url"))
            _extend(result.extra.get("source_url"))
        if isinstance(result.metadata, dict):
            _extend(result.metadata.get("url"))
            _extend(result.metadata.get("source_url"))
            _extend(result.metadata.get("webpage_url"))
        if isinstance(getattr(result, "full_metadata", None), dict):
            fm = getattr(result, "full_metadata", None)
            if isinstance(fm, dict):
                _extend(fm.get("url"))
                _extend(fm.get("source_url"))
                _extend(fm.get("webpage_url"))

    elif hasattr(result, "url") or hasattr(result, "source_url"):
        _extend(getattr(result, "url", None))
        _extend(getattr(result, "source_url", None))

    if isinstance(result, dict):
        _extend(result.get("url"))
        _extend(result.get("source_url"))
        _extend(result.get("webpage_url"))

        extra = result.get("extra")
        if isinstance(extra, dict):
            _extend(extra.get("url"))

        fm = result.get("full_metadata") or result.get("metadata")
        if isinstance(fm, dict):
            _extend(fm.get("url"))
            _extend(fm.get("source_url"))
            _extend(fm.get("webpage_url"))

    from SYS.metadata import normalize_urls
    return normalize_urls(url)
