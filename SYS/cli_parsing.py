"""CLI parsing helpers moved out of `CLI.py`.

Contains selection parsing and the REPL lexer so `CLI.py` can be smaller and
these pure helpers are easier to test.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from SYS.logger import debug

# Prompt-toolkit lexer types are optional and expensive (~300ms). Use find_spec
# to detect availability without importing, then lazy-load on first use.
import importlib.util as _importlib_util
_PTK_AVAILABLE: bool = _importlib_util.find_spec("prompt_toolkit") is not None

_ptk_Document: Any = None
_ptk_Lexer: Any = None


def _get_ptk_Document() -> Any:
    global _ptk_Document
    if _ptk_Document is None:
        if _PTK_AVAILABLE:
            from prompt_toolkit.document import Document as _Doc
            _ptk_Document = _Doc
        else:
            _ptk_Document = object
    return _ptk_Document


def _get_ptk_Lexer() -> Any:
    global _ptk_Lexer
    if _ptk_Lexer is None:
        if _PTK_AVAILABLE:
            from prompt_toolkit.lexers import Lexer as _Lex
            _ptk_Lexer = _Lex
        else:
            _ptk_Lexer = object
    return _ptk_Lexer


# Stable aliases: these resolve lazily the first time they are accessed.
# Code that does `isinstance(x, Document)` or `class Foo(Lexer)` at class-body
# time needs the real object, so we keep module-level names that proxy to the
# lazy getters via __getattr__ on the module.  Callers that reference
# Document/Lexer INSIDE functions will always get the real class.
# Populate the module-level names now so that class bodies below can inherit.
Document: Any = _get_ptk_Document()
Lexer: Any = _get_ptk_Lexer()

# Pre-compiled regexes for the lexer (avoid recompiling on every call)
_TEMPLATE_FUNC_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


def lex_template_inner(text: str, *, default_class: str = "class:string") -> List[Tuple[str, str]]:
    """Color extract/transform syntax: [a|b], trim(), $(ns), (field)."""
    tokens: List[Tuple[str, str]] = []
    i = 0
    n = len(text or "")
    while i < n:
        if text.startswith("$(", i) or text.startswith("#(", i):
            start = i
            i += 2
            depth = 1
            while i < n and depth:
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                i += 1
            tokens.append(("class:template_placeholder", text[start:i]))
            continue
        ch = text[i]
        if ch in "[]":
            tokens.append(("class:template_bracket", ch))
            i += 1
            continue
        if ch == "|":
            tokens.append(("class:template_or", ch))
            i += 1
            continue
        match = _TEMPLATE_FUNC_RE.match(text, i)
        if match is not None and match.end() < n and text[match.end()] == "(":
            tokens.append(("class:template_func", match.group(0)))
            tokens.append(("class:template_func", "("))
            i = match.end() + 1
            continue
        if ch in "()":
            tokens.append(("class:template_field", ch))
            i += 1
            continue
        j = i + 1
        while j < n:
            nxt = text[j]
            if nxt in "[]|()$" or text.startswith("$(", j) or text.startswith("#(", j):
                break
            word = _TEMPLATE_FUNC_RE.match(text, j)
            if word is not None and word.end() < n and text[word.end()] == "(":
                break
            j += 1
        tokens.append((default_class, text[i:j]))
        i = j
    return tokens


TOKEN_PATTERN = re.compile(
    r"""
    (\s+) |                                      # 1. Whitespace
    (\|) |                                       # 2. Pipe
    ("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*') |      # 3. Quoted string
    ([^\s\|]+)                                   # 4. Word
    """,
    re.VERBOSE,
)
KEY_PREFIX_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*:)(.*)$")
SELECTION_RANGE_RE = re.compile(r"^[0-9\-\*,]+$")
DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class SelectionSyntax:
    """Parses @ selection syntax into ordered 1-based indices."""

    _RANGE_RE = re.compile(r"^[0-9\-]+$")

    @staticmethod
    def parse(token: str) -> Optional[List[int]]:
        """Return ordered 1-based indices or None when not a concrete selection.

        Concrete selections:
        - @2
        - @2-5
        - @{1,3,5}
        - @2,5,7-9

        Special (non-concrete) selectors return None:
        - @* (select all)
        - @.. (history prev)
        - @,, (history next)
        """

        if not token or not token.startswith("@"):
            return None

        selector = token[1:].strip()
        if selector in (".", ",", "*"):
            return None

        if selector.startswith("{") and selector.endswith("}"):
            selector = selector[1:-1].strip()

        indices: List[int] = []
        seen: set[int] = set()
        for part in selector.split(","):
            part = part.strip()
            if not part:
                continue

            if "-" in part:
                pieces = part.split("-", 1)
                if len(pieces) != 2:
                    return None
                start_str = pieces[0].strip()
                end_str = pieces[1].strip()
                if not start_str or not end_str:
                    return None
                try:
                    start = int(start_str)
                    end = int(end_str)
                except ValueError:
                    return None
                if start <= 0 or end <= 0 or start > end:
                    return None
                for value in range(start, end + 1):
                    if value not in seen:
                        seen.add(value)
                        indices.append(value)
                continue

            try:
                value = int(part)
            except ValueError:
                return None
            if value <= 0:
                return None
            if value not in seen:
                seen.add(value)
                indices.append(value)

        return indices if indices else None


class SelectionFilterSyntax:
    """Parses and applies @"COL:filter" selection filters.

    Notes:
    - CLI tokenization (shlex) strips quotes, so a user input of `@"TITLE:foo"`
      arrives as `@TITLE:foo`. We support both forms.
    - Filters apply to the *current selectable table items* (in-memory), not to
      provider searches.
    """

    _OP_RE = re.compile(r"^(>=|<=|!=|==|>|<|=)\s*(.+)$")
    _DUR_TOKEN_RE = re.compile(r"(?i)(\d+)\s*([hms])")

    @staticmethod
    def parse(token: str) -> Optional[List[Tuple[str, str]]]:
        """Return list of (column, raw_expression) or None when not a filter token."""

        if not token or not str(token).startswith("@"):
            return None

        if token.strip() == "@*":
            return None

        # If this is a concrete numeric selection (@2, @1-3, @{1,3}), do not treat it as a filter.
        try:
            if SelectionSyntax.parse(str(token)) is not None:
                return None
        except Exception as exc:
            debug("SelectionSyntax.parse failed during filter detection: %s", exc, exc_info=True)

        raw = str(token)[1:].strip()
        if not raw:
            return None

        # If quotes survived tokenization, strip a single symmetric wrapper.
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
            raw = raw[1:-1].strip()

        # Shorthand: @"foo" means Title contains "foo".
        if ":" not in raw:
            if raw:
                return [("Title", raw)]
            return None

        parts = [p.strip() for p in raw.split(",") if p.strip()]
        conditions: List[Tuple[str, str]] = []
        for part in parts:
            if ":" not in part:
                return None
            col, expr = part.split(":", 1)
            col = str(col or "").strip()
            expr = str(expr or "").strip()
            if not col:
                return None
            conditions.append((col, expr))

        return conditions if conditions else None

    @staticmethod
    def _norm_key(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip().lower())

    @staticmethod
    def _item_column_map(item: Any) -> Dict[str, str]:
        out: Dict[str, str] = {}

        def _set(k: Any, v: Any) -> None:
            key = SelectionFilterSyntax._norm_key(str(k or ""))
            if not key:
                return
            if v is None:
                return
            try:
                if isinstance(v, (list, tuple, set)):
                    text = ", ".join(str(x) for x in v if x is not None)
                else:
                    text = str(v)
            except Exception:
                return
            out[key] = text

        if isinstance(item, dict):
            # Display columns (primary UX surface)
            cols = item.get("columns")
            if isinstance(cols, list):
                for pair in cols:
                    try:
                        if isinstance(pair, (list, tuple)) and len(pair) == 2:
                            _set(pair[0], pair[1])
                    except Exception:
                        continue
            # Direct keys as fallback
            for k, v in item.items():
                if k == "columns":
                    continue
                _set(k, v)
        else:
            cols = getattr(item, "columns", None)
            if isinstance(cols, list):
                for pair in cols:
                    try:
                        if isinstance(pair, (list, tuple)) and len(pair) == 2:
                            _set(pair[0], pair[1])
                    except Exception:
                        continue
            for k in ("title", "path", "detail", "provider", "store", "table"):
                try:
                    _set(k, getattr(item, k, None))
                except Exception as exc:
                    debug("SelectionFilterSyntax: failed to _set attribute %s on item: %s", k, exc, exc_info=True)

        return out

    @staticmethod
    def _parse_duration_seconds(text: str) -> Optional[int]:
        s = str(text or "").strip()
        if not s:
            return None

        if s.isdigit():
            try:
                return max(0, int(s))
            except Exception:
                return None

        # clock format: M:SS or H:MM:SS
        if ":" in s:
            parts = [p.strip() for p in s.split(":")]
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                m_str, sec_str = parts
                return max(0, int(m_str) * 60 + int(sec_str))
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                h_str, m_str, sec_str = parts
                return max(0, int(h_str) * 3600 + int(m_str) * 60 + int(sec_str))

        # token format: 1h2m3s (tokens can appear in any combination)
        total = 0
        found = False
        for match in SelectionFilterSyntax._DUR_TOKEN_RE.finditer(s):
            found = True
            n = int(match.group(1))
            unit = match.group(2).lower()
            if unit == "h":
                total += n * 3600
            elif unit == "m":
                total += n * 60
            elif unit == "s":
                total += n
        if found:
            return max(0, int(total))

        return None

    @staticmethod
    def _parse_float(text: str) -> Optional[float]:
        s = str(text or "").strip()
        if not s:
            return None
        s = s.replace(",", "")
        try:
            return float(s)
        except Exception:
            return None

    @staticmethod
    def _parse_op(expr: str) -> Tuple[Optional[str], str]:
        text = str(expr or "").strip()
        if not text:
            return None, ""
        m = SelectionFilterSyntax._OP_RE.match(text)
        if not m:
            return None, text
        return m.group(1), str(m.group(2) or "").strip()

    @staticmethod
    def matches(item: Any, conditions: List[Tuple[str, str]]) -> bool:
        colmap = SelectionFilterSyntax._item_column_map(item)

        for col, expr in conditions:
            key = SelectionFilterSyntax._norm_key(col)
            actual = colmap.get(key)

            # Convenience aliases for common UX names.
            if actual is None:
                if key == "duration":
                    actual = colmap.get("duration")
                elif key == "title":
                    actual = colmap.get("title")

            if actual is None:
                return False

            op, rhs = SelectionFilterSyntax._parse_op(expr)
            left_text = str(actual or "").strip()
            right_text = str(rhs or "").strip()

            if op is None:
                if not right_text:
                    return False
                if right_text.lower() not in left_text.lower():
                    return False
                continue

            # Comparator: try duration parsing first when it looks time-like.
            prefer_duration = (
                key == "duration"
                or any(ch in right_text for ch in (":", "h", "m", "s"))
                or any(ch in left_text for ch in (":", "h", "m", "s"))
            )

            left_num: Optional[float] = None
            right_num: Optional[float] = None

            if prefer_duration:
                ldur = SelectionFilterSyntax._parse_duration_seconds(left_text)
                rdur = SelectionFilterSyntax._parse_duration_seconds(right_text)
                if ldur is not None and rdur is not None:
                    left_num = float(ldur)
                    right_num = float(rdur)

            if left_num is None or right_num is None:
                left_num = SelectionFilterSyntax._parse_float(left_text)
                right_num = SelectionFilterSyntax._parse_float(right_text)

            if left_num is not None and right_num is not None:
                if op in ("=", "=="):
                    if not (left_num == right_num):
                        return False
                elif op == "!=":
                    if not (left_num != right_num):
                        return False
                elif op == ">":
                    if not (left_num > right_num):
                        return False
                elif op == ">=":
                    if not (left_num >= right_num):
                        return False
                elif op == "<":
                    if not (left_num < right_num):
                        return False
                elif op == "<=":
                    if not (left_num <= right_num):
                        return False
                else:
                    return False
                continue

            # Fallback to string equality for =/!= when numeric parsing fails.
            if op in ("=", "=="):
                if left_text.lower() != right_text.lower():
                    return False
            elif op == "!=":
                if left_text.lower() == right_text.lower():
                    return False
            else:
                return False

        return True


class MedeiaLexer(Lexer):
    def lex_document(self, document: "Document") -> Callable[[int], List[Tuple[str, str]]]:  # type: ignore[override]

        def get_line(lineno: int) -> List[Tuple[str, str]]:
            """Return token list for a single input line (used by prompt-toolkit)."""
            line = document.lines[lineno]
            tokens: List[Tuple[str, str]] = []

            # Using TOKEN_PATTERN precompiled at module scope.

            is_cmdlet = True

            def _emit_keyed_value(word: str) -> bool:
                """Emit `key:` prefixes (comma-separated) as argument tokens.

                Designed for values like:
                  clip:3m4s-3m14s,1h22m-1h33m,item:2-3

                Avoids special-casing URLs (://) and Windows drive paths (C:\\...).
                Returns True if it handled the token.
                """
                if not word or ":" not in word:
                    return False
                # Avoid URLs and common scheme patterns.
                if "://" in word:
                    return False
                # Avoid Windows drive paths (e.g., C:\\foo or D:/bar)
                if DRIVE_RE.match(word):
                    return False

                parts = word.split(",")
                handled_any = False
                for i, part in enumerate(parts):
                    if i > 0:
                        tokens.append(("class:value", ","))
                    if part == "":
                        continue
                    m = KEY_PREFIX_RE.match(part)
                    if m:
                        tokens.append(("class:argument", m.group(1)))
                        rest = m.group(2) or ""
                        if rest:
                            tokens.extend(
                                lex_template_inner(rest, default_class="class:value")
                            )
                        handled_any = True
                    else:
                        tokens.extend(
                            lex_template_inner(part, default_class="class:value")
                        )
                        handled_any = True

                return handled_any

            for match in TOKEN_PATTERN.finditer(line):
                ws, pipe, quote, word = match.groups()
                if ws:
                    tokens.append(("", ws))
                    continue
                if pipe:
                    tokens.append(("class:pipe", pipe))
                    is_cmdlet = True
                    continue
                if quote:
                    # If the quoted token contains a keyed spec (clip:/item:/hash:),
                    # highlight the `key:` portion in argument-blue even inside quotes.
                    if len(quote) >= 2 and quote[0] == quote[-1] and quote[0] in ('"', "'"):
                        q = quote[0]
                        inner = quote[1:-1]
                        start_index = len(tokens)
                        if _emit_keyed_value(inner):
                            tokens.insert(start_index, ("class:string", q))
                            tokens.append(("class:string", q))
                            is_cmdlet = False
                            continue
                        tokens.append(("class:string", q))
                        tokens.extend(lex_template_inner(inner, default_class="class:string"))
                        tokens.append(("class:string", q))
                        is_cmdlet = False
                        continue

                    tokens.append(("class:string", quote))
                    is_cmdlet = False
                    continue
                if not word:
                    continue

                if word.startswith("@"):  # selection tokens
                    rest = word[1:]
                    if rest and SELECTION_RANGE_RE.fullmatch(rest):
                        tokens.append(("class:selection_at", "@"))
                        tokens.append(("class:selection_range", rest))
                        is_cmdlet = False
                        continue
                    if rest and ":" in rest:
                        tokens.append(("class:selection_at", "@"))
                        tokens.append(("class:selection_filter", rest))
                        is_cmdlet = False
                        continue
                    if rest == "":
                        tokens.append(("class:selection_at", "@"))
                        is_cmdlet = False
                        continue

                if is_cmdlet:
                    tokens.append(("class:cmdlet", word))
                    is_cmdlet = False
                elif word.startswith("-"):
                    tokens.append(("class:argument", word))
                else:
                    if not _emit_keyed_value(word):
                        if any(ch in word for ch in "[]$()|") or "(" in word:
                            tokens.extend(lex_template_inner(word, default_class="class:value"))
                        else:
                            tokens.append(("class:value", word))

            return tokens

        return get_line

