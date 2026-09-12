from __future__ import annotations

from typing import Any, Dict, List, Sequence, Optional
from pathlib import Path
import sys
import re

from SYS.logger import log, debug
from SYS.item_accessors import extract_item_tags, get_string_list, set_field
from SYS.payload_builders import extract_title_tag_value
from SYS.result_publication import publish_result_table

from SYS import models
from SYS import pipeline as ctx
from .. import _shared as sh

normalize_result_input = sh.normalize_result_input
filter_results_by_temp = sh.filter_results_by_temp
Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
SharedArgs = sh.SharedArgs
normalize_hash = sh.normalize_hash
parse_tag_arguments = sh.parse_tag_arguments
expand_tag_groups = sh.expand_tag_groups
merge_sequences = sh.merge_sequences
render_tag_value_templates = sh.render_tag_value_templates
parse_cmdlet_args = sh.parse_cmdlet_args
collapse_namespace_tags = sh.collapse_namespace_tags
should_show_help = sh.should_show_help
get_field = sh.get_field

_FIELD_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_FIELD_SPEC_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9_]*)(?::([A-Za-z][A-Za-z0-9_]*))?$"
)
_EXTRACT_FIELD_RE = re.compile(
    r"\(([A-Za-z][A-Za-z0-9_]*(?::[A-Za-z][A-Za-z0-9_]*)?)\)"
)
_DETAIL_PANEL_LIMIT = 9
_EXTRACT_NUMERIC_FIELDS = frozenset({
    "disk",
    "disc",
    "cd",
    "track",
    "trk",
    "episode",
    "ep",
    "season",
    "year",
    "date",
    "airdate",
    "pubdate",
    "month",
    "day",
    "mm",
    "dd",
    "yy",
    "yyyy",
})
_EXTRACT_SLASH_JOIN_FIELDS = frozenset({
    "date",
    "airdate",
    "pubdate",
    "published",
    "issued",
    "recorded",
    "broadcast",
})
_EXTRACT_DATE_PARTS = {
    "year": "year",
    "yyyy": "year",
    "yy": "year",
    "month": "month",
    "mm": "month",
    "day": "day",
    "dd": "day",
}
_EXTRACT_DATE_TOKEN = (
    r"(?:\d{8}|\d{1,4}[-./]\d{1,2}[-./]\d{1,4}|\d{1,4}\s+\d{1,2}\s+\d{1,4})"
)


def _looks_like_extract_template(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw or not _EXTRACT_FIELD_RE.search(raw):
        return False
    if re.match(r"^[A-Za-z][A-Za-z0-9_]*:", raw):
        return False
    return True


def _normalize_title_for_extract(text: str) -> str:
    """Normalize common separators in titles for matching.

    Helps when sources use unicode dashes or odd whitespace.
    """

    s = str(text or "").strip()
    if not s:
        return s
    # Common unicode dash variants -> '-'
    s = s.replace("\u2013", "-")  # en dash
    s = s.replace("\u2014", "-")  # em dash
    s = s.replace("\u2212", "-")  # minus sign
    s = s.replace("\u2010", "-")  # hyphen
    s = s.replace("\u2011", "-")  # non-breaking hyphen
    s = s.replace("\u2012", "-")  # figure dash
    s = s.replace("\u2015", "-")  # horizontal bar

    # Collapse any whitespace runs (including newlines/tabs) to a single space.
    # Some sources wrap the artist name or title across lines.
    try:
        s = re.sub(r"\s+", " ", s).strip()
    except Exception:
        s = " ".join(s.split())
    return s


def _strip_title_prefix(text: str) -> str:
    s = str(text or "").strip()
    if s.lower().startswith("title:"):
        s = s.split(":", 1)[1].strip()
    return s


def _literal_to_title_pattern_regex(literal: str) -> str:
    """Convert a literal chunk of a template into a regex fragment.

    Keeps punctuation literal, but treats any whitespace run as \\s*.
    ``#`` matches a number (not stored as a tag); ``#?`` is an optional number.
    """

    out: List[str] = []
    i = 0
    while i < len(literal):
        ch = literal[i]
        if ch.isspace():
            while i < len(literal) and literal[i].isspace():
                i += 1
            out.append(r"\s*")
            continue
        if ch == "#":
            if i + 1 < len(literal) and literal[i + 1] == "?":
                out.append(r"(?:\d+)?")
                i += 2
                continue
            out.append(r"\d+")
            i += 1
            continue
        if ch == "[":
            close = literal.find("]", i + 1)
            if close > i:
                inner = literal[i + 1:close]
                alts = [alt.strip() for alt in inner.split("|") if alt.strip()]
                if alts:
                    fragments = [
                        _literal_to_title_pattern_regex(alt) if alt else ""
                        for alt in alts
                    ]
                    fragments = [frag for frag in fragments if frag]
                    if fragments:
                        out.append(r"\s*(?:" + "|".join(fragments) + r")\s*")
                        i = close + 1
                        continue
        out.append(re.escape(ch))
        i += 1
    return "".join(out)


def _split_extract_or_branches(template: str) -> List[str]:
    text = str(template or "")
    branches: List[str] = []
    current: List[str] = []
    paren = 0
    bracket = 0
    for ch in text:
        if ch == "(":
            paren += 1
            current.append(ch)
            continue
        if ch == ")":
            paren = max(0, paren - 1)
            current.append(ch)
            continue
        if ch == "[":
            bracket += 1
            current.append(ch)
            continue
        if ch == "]":
            bracket = max(0, bracket - 1)
            current.append(ch)
            continue
        if ch == "|" and paren == 0 and bracket == 0:
            branch = "".join(current).strip()
            if branch:
                branches.append(branch)
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        branches.append(tail)
    return branches or [text.strip()]


def _tail_has_number_token(tail: str) -> bool:
    text = str(tail or "")
    i = 0
    while i < len(text):
        if text[i] == "#":
            return True
        i += 1
    return False


def _compile_extract_branch(
    tpl: str,
    *,
    name_counts: Dict[str, int],
) -> tuple[str, List[tuple[str, str, Optional[str]]]]:
    matches = list(re.finditer(r"\(([^)]+)\)", tpl))
    if not matches:
        raise ValueError("extract template must contain at least one (field)")

    specs: List[tuple[str, str, Optional[str]]] = []
    parts: List[str] = []
    last_end = 0

    for idx, m in enumerate(matches):
        literal = tpl[last_end:m.start()]
        if literal:
            parts.append(_literal_to_title_pattern_regex(literal))

        raw_spec = (m.group(1) or "").strip()
        parsed = _FIELD_SPEC_RE.fullmatch(raw_spec)
        if not parsed:
            raise ValueError(
                f"invalid field '{raw_spec}' (use (name) or (name:part), e.g. (date:day))"
            )
        raw_name = parsed.group(1)
        role = (parsed.group(2) or "").strip() or None
        if role:
            role = role.lower()
            if role not in _EXTRACT_DATE_PARTS:
                raise ValueError(
                    f"unknown part '{role}' in ({raw_name}:{role}); use year, month, day (or yy, mm, dd)"
                )
        seen = name_counts.get(raw_name, 0) + 1
        name_counts[raw_name] = seen
        group_name = raw_name if seen == 1 else f"{raw_name}__{seen}"
        specs.append((raw_name, group_name, role))

        name_lower = raw_name.lower()
        is_last = idx == (len(matches) - 1)
        numeric = name_lower in _EXTRACT_NUMERIC_FIELDS or (role or "") in _EXTRACT_DATE_PARTS
        rest_after = tpl[m.end():]
        if (role or "") in _EXTRACT_DATE_PARTS:
            parts.append(rf"(?P<{group_name}>\d+)")
        elif name_lower in _EXTRACT_SLASH_JOIN_FIELDS and not role:
            parts.append(rf"(?P<{group_name}>{_EXTRACT_DATE_TOKEN})")
        elif numeric:
            parts.append(rf"(?P<{group_name}>\d+)")
        elif is_last and _tail_has_number_token(rest_after):
            parts.append(rf"(?P<{group_name}>.+?)")
        elif is_last:
            parts.append(rf"(?P<{group_name}>.+)")
        else:
            parts.append(rf"(?P<{group_name}>.+?)")

        last_end = m.end()

    tail = tpl[last_end:]
    if tail:
        if tail.strip() == "#":
            parts.append(r"\s*(?:\d+)?")
        else:
            parts.append(_literal_to_title_pattern_regex(tail))
    parts.append(r"\s*.*")
    return "".join(parts), specs


def _compile_extract_template(
    template: str,
) -> tuple[re.Pattern[str], List[tuple[str, str, Optional[str]]]]:
    """Compile a (field) template. `|` ORs whole patterns; `[a|b]` ORs literals."""

    tpl = str(template or "").strip()
    if not tpl:
        raise ValueError("empty extract template")

    branches = _split_extract_or_branches(tpl)
    name_counts: Dict[str, int] = {}
    bodies: List[str] = []
    specs: List[tuple[str, str, Optional[str]]] = []
    for branch in branches:
        body, branch_specs = _compile_extract_branch(branch, name_counts=name_counts)
        bodies.append(body)
        specs.extend(branch_specs)
    if len(bodies) == 1:
        rx = r"^.*?" + bodies[0] + r"$"
    else:
        rx = r"^(?:.*?(?:" + "|".join(bodies) + r"))$"
    return re.compile(rx, flags=re.IGNORECASE), specs


def _normalize_date_part(canon: str, value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return str(value or "").strip()
    if canon == "year":
        if len(digits) <= 2:
            year = int(digits)
            return str(2000 + year if year < 100 else year)
        return digits.zfill(4) if len(digits) < 4 else digits
    if canon in {"month", "day"}:
        return digits.zfill(2)
    return digits


def _assemble_extract_field(
    logical: str,
    pieces: List[tuple[Optional[str], str]],
) -> str:
    labeled: Dict[str, str] = {}
    unlabeled: List[str] = []
    for role, value in pieces:
        canon = _EXTRACT_DATE_PARTS.get(str(role or "").lower())
        if canon:
            labeled[canon] = _normalize_date_part(canon, value)
        else:
            unlabeled.append(value)
    if labeled:
        from SYS.config import get_date_format

        fmt = str(get_date_format() or "YYYY-MM-DD")
        year = labeled.get("year", "")
        month = labeled.get("month", "")
        day = labeled.get("day", "")
        yy = year[-2:] if year else ""
        assembled = (
            fmt.replace("YYYY", year)
            .replace("yyyy", year)
            .replace("YY", yy)
            .replace("yy", yy)
            .replace("MM", month)
            .replace("mm", month)
            .replace("DD", day)
            .replace("dd", day)
        )
        for sep in ("-", "/", ".", " "):
            while f"{sep}{sep}" in assembled:
                assembled = assembled.replace(f"{sep}{sep}", sep)
            assembled = assembled.strip(sep)
        if unlabeled:
            extra = " ".join(unlabeled)
            assembled = f"{assembled} {extra}".strip() if assembled else extra
        return assembled
    if len(unlabeled) == 1:
        return unlabeled[0]
    joiner = "/" if logical.lower() in _EXTRACT_SLASH_JOIN_FIELDS else " "
    return joiner.join(unlabeled)


def _extract_tags_from_title(title_text: str, template: str) -> List[str]:
    """Extract (field)->value from title_text and return ['field:value', ...]."""

    title_clean = _normalize_title_for_extract(_strip_title_prefix(title_text))
    if not title_clean:
        return []

    pattern, specs = _compile_extract_template(template)
    m = pattern.match(title_clean)
    if not m:
        return []

    grouped: Dict[str, List[tuple[Optional[str], str]]] = {}
    order: List[str] = []
    for logical, group, role in specs:
        try:
            value = (m.group(group) or "").strip()
        except IndexError:
            continue
        if not value:
            continue
        if logical not in grouped:
            order.append(logical)
            grouped[logical] = []
        grouped[logical].append((role, value))

    out: List[str] = []
    for logical in order:
        pieces = grouped.get(logical) or []
        if not pieces:
            continue
        assembled = _assemble_extract_field(logical, pieces)
        if assembled and logical.lower() in _EXTRACT_SLASH_JOIN_FIELDS:
            try:
                from cmdlet._tag_utils import _format_tag_date, _parse_tag_date
                from SYS.config import get_date_format

                parsed = _parse_tag_date(assembled)
                if parsed is not None:
                    assembled = _format_tag_date(parsed, str(get_date_format() or "YYYY-MM-DD"))
            except Exception:
                pass
        if assembled:
            out.append(f"{logical}:{assembled}")
    return out


def _get_title_candidates_for_extraction(
    res: Any,
    existing_tags: Optional[List[str]] = None
) -> List[str]:
    """Return a list of possible title strings in priority order."""

    candidates: List[str] = []

    def add_candidate(val: Any) -> None:
        if val is None:
            return
        s = _normalize_title_for_extract(_strip_title_prefix(str(val)))
        if not s:
            return
        if s not in candidates:
            candidates.append(s)

    # 1) Item's title field (may be a display title, not the title: tag)
    try:
        add_candidate(get_field(res, "title"))
    except Exception:
        pass
    if isinstance(res, dict):
        add_candidate(res.get("title"))

    # 2) title: tag from either store tags or piped tags
    tags = existing_tags if isinstance(existing_tags, list) else _extract_item_tags(res)
    add_candidate(_extract_title_tag(tags) or "")

    # 3) Filename stem
    try:
        path_val = get_field(res, "path")
        if path_val:
            p = Path(str(path_val))
            add_candidate((p.stem or "").strip())
    except Exception:
        pass

    return candidates


def _extract_tags_from_title_candidates(candidates: List[str],
                                        template: str) -> tuple[List[str],
                                                                Optional[str]]:
    """Try candidates in order; return (tags, matched_candidate)."""

    for c in candidates:
        extracted = _extract_tags_from_title(c, template)
        if extracted:
            return extracted, c
    return [], None


def _try_compile_extract_template(
    template: Optional[str],
) -> tuple[Optional[re.Pattern[str]],
           Optional[str]]:
    """Compile template for debug; return (pattern, error_message)."""
    if template is None:
        return None, None
    try:
        pattern, _specs = _compile_extract_template(str(template))
        return pattern, None
    except Exception as exc:
        return None, str(exc)


def _extract_title_tag(tags: List[str]) -> Optional[str]:
    """Return the value of the first title: tag if present."""
    return extract_title_tag_value(tags)


def _extract_item_tags(res: Any) -> List[str]:
    return extract_item_tags(res)


def _set_item_tags(res: Any, tags: List[str]) -> None:
    set_field(res, "tag", tags)


def _display_title_from_result(res: Any) -> str:
    for key in ("title", "name"):
        try:
            value = get_field(res, key)
        except Exception:
            value = None
        text = str(value or "").strip()
        if text:
            return text
    blob: Any = None
    if isinstance(res, dict):
        blob = res.get("tag") or res.get("tags")
    else:
        blob = getattr(res, "tag", None)
    title = extract_title_tag_value(blob if isinstance(blob, (list, tuple, set)) else [])
    return str(title or "").strip()


def _column_pair(col: Any) -> Optional[tuple[str, str]]:
    if isinstance(col, (tuple, list)) and len(col) >= 2:
        return (str(col[0]), str(col[1]))
    name = getattr(col, "name", None)
    value = getattr(col, "value", None)
    if name is not None:
        return (str(name), str(value or ""))
    return None


def _set_item_tag_display(res: Any, tags: Sequence[str]) -> None:
    shown = [str(tag).strip() for tag in tags if str(tag or "").strip()]
    text = ", ".join(shown)
    title = _display_title_from_result(res)

    def _patch_columns(cols: Any) -> List[Any]:
        updated: List[Any] = []
        found_tag = False
        found_title = False
        if isinstance(cols, list):
            for col in cols:
                pair = _column_pair(col)
                if pair is None:
                    updated.append(col)
                    continue
                label, value = pair
                key = label.strip().lower()
                if key == "tag":
                    updated.append((label, text))
                    found_tag = True
                elif key == "title":
                    updated.append((label, title or value))
                    found_title = True
                else:
                    updated.append((label, value))
        if title and not found_title:
            updated.insert(0, ("Title", title))
            found_title = True
        if not found_tag and text:
            insert_at = 1 if found_title else 0
            updated.insert(insert_at, ("Tag", text))
        return updated

    if isinstance(res, dict):
        res["columns"] = _patch_columns(res.get("columns"))
        return
    cols = getattr(res, "columns", None)
    if cols is None and not text:
        return
    try:
        res.columns = _patch_columns(cols)
    except Exception:
        pass


def _apply_title_to_result(res: Any, title_value: Optional[str]) -> None:
    """Update result object/dict title fields and columns in-place."""
    if not title_value:
        return
    if isinstance(res, models.PipeObject):
        res.title = title_value
        # Update columns if present (Title column assumed index 0)
        columns = getattr(res, "columns", None)
        if isinstance(columns, list) and columns:
            label, *_ = columns[0]
            if str(label).lower() == "title":
                columns[0] = (label, title_value)
    elif isinstance(res, dict):
        res["title"] = title_value
        cols = res.get("columns")
        if isinstance(cols, list):
            updated = []
            changed = False
            for col in cols:
                if isinstance(col, tuple) and len(col) == 2:
                    label, _val = col
                    if str(label).lower() == "title":
                        updated.append((label, title_value))
                        changed = True
                    else:
                        updated.append(col)
                else:
                    updated.append(col)
            if changed:
                res["columns"] = updated


def _matches_target(
    item: Any,
    target_hash: Optional[str],
    target_path: Optional[str],
    target_instance: Optional[str] = None,
) -> bool:
    """Determine whether a result item refers to the given target.

    Important: hashes can collide across backends in this app's UX (same media in
    multiple stores). When target_store is provided, it must match too.
    """

    def norm(val: Any) -> Optional[str]:
        return str(val).lower() if val is not None else None

    target_hash_l = target_hash.lower() if target_hash else None
    target_path_l = target_path.lower() if target_path else None
    target_store_l = target_instance.lower() if target_instance else None

    if isinstance(item, dict):
        hashes = [norm(item.get("hash"))]
        paths = [norm(item.get("path"))]
        stores = [norm(item.get("store"))]
    else:
        hashes = [norm(get_field(item, "hash"))]
        paths = [norm(get_field(item, "path"))]
        stores = [norm(get_field(item, "store"))]

    if target_store_l:
        if target_store_l not in stores:
            return False

    if target_hash_l and target_hash_l in hashes:
        return True
    if target_path_l and target_path_l in paths:
        return True
    return False


def _update_item_title_fields(item: Any, new_title: str) -> None:
    """Mutate an item to reflect a new title in plain fields and columns."""
    if isinstance(item, models.PipeObject):
        item.title = new_title
        columns = getattr(item, "columns", None)
        if isinstance(columns, list) and columns:
            label, *_ = columns[0]
            if str(label).lower() == "title":
                columns[0] = (label, new_title)
    elif isinstance(item, dict):
        item["title"] = new_title
        cols = item.get("columns")
        if isinstance(cols, list):
            updated_cols = []
            changed = False
            for col in cols:
                if isinstance(col, tuple) and len(col) == 2:
                    label, _val = col
                    if str(label).lower() == "title":
                        updated_cols.append((label, new_title))
                        changed = True
                    else:
                        updated_cols.append(col)
                else:
                    updated_cols.append(col)
            if changed:
                item["columns"] = updated_cols


def _refresh_result_table_title(
    new_title: str,
    target_hash: Optional[str],
    target_instance: Optional[str],
    target_path: Optional[str],
) -> None:
    """Refresh the cached result table with an updated title and redisplay it."""
    try:
        last_table = ctx.get_last_result_table()
        items = ctx.get_last_result_items()
        if not last_table or not items:
            return

        updated_items = []
        match_found = False
        for item in items:
            try:
                if _matches_target(item, target_hash, target_path, target_instance):
                    _update_item_title_fields(item, new_title)
                    match_found = True
            except Exception:
                pass
            updated_items.append(item)
        if not match_found:
            return

        new_table = last_table.copy_with_title(getattr(last_table, "title", ""))

        for item in updated_items:
            new_table.add_result(item)

        # Keep the underlying history intact; update only the overlay so @.. can
        # clear the overlay then continue back to prior tables (e.g., the search list).
        publish_result_table(ctx, new_table, updated_items, overlay=True)
    except Exception:
        pass


def _refresh_tag_view(
    res: Any,
    target_hash: Optional[str],
    store_name: Optional[str],
    target_path: Optional[str],
    config: Dict[str,
                 Any],
) -> None:
    """Refresh tag display via get-tag. Prefer current subject; fall back to direct hash refresh."""
    try:
        from cmdlet import get as get_cmdlet  # type: ignore
    except Exception:
        return

    if not target_hash:
        return

    get_tag = None
    try:
        get_tag = get_cmdlet("metadata")
    except Exception:
        get_tag = None
    if not callable(get_tag):
        return

    try:
        subject = ctx.get_last_result_subject()
        if not subject or not _matches_target(subject, target_hash, target_path, store_name):
            return

        refresh_args: List[str] = ["-get", "-query", f"hash:{target_hash}"]
        
        # Build a lean subject so get-tag fetches fresh tags instead of reusing cached payloads.
        def _build_refresh_subject() -> Dict[str, Any]:
            payload: Dict[str, Any] = {}
            payload["hash"] = target_hash
            if sh.value_has_content(store_name):
                payload["store"] = store_name

            path_value = target_path or get_field(subject, "path")
            if not sh.value_has_content(path_value):
                path_value = get_field(subject, "target")
            if sh.value_has_content(path_value):
                payload["path"] = path_value

            for key in ("title", "name", "url", "relations", "service_name"):
                val = get_field(subject, key)
                if sh.value_has_content(val):
                    payload[key] = val

            extra_value = get_field(subject, "extra")
            if isinstance(extra_value, dict):
                cleaned = {
                    k: v for k, v in extra_value.items()
                    if str(k).lower() not in {"tag", "tags"}
                }
                if cleaned:
                    payload["extra"] = cleaned
            elif sh.value_has_content(extra_value):
                payload["extra"] = extra_value

            return payload

        refresh_subject = _build_refresh_subject()
        with ctx.suspend_live_progress():
            get_tag(refresh_subject, refresh_args, config)
    except Exception:
        pass


class Add_Tag(Cmdlet):
    """Class-based metadata -add tag handler with Cmdlet metadata inheritance."""

    def __init__(self, *, register_cmdlet: bool = True) -> None:
        super().__init__(
            name="tag",
            summary="Add tag to a file in an instance.",
            usage=
            'metadata -add [-query "hash:<sha256> instance:<store>"] [-extract "(field) ..."] [-duplicate <format>] [-list <list>[,<list>...]] [--all] <tag>[,<tag>...]',
            arg=[
                CmdletArg(
                    "tag",
                    type="string",
                    required=False,
                    description=
                    "One or more tag to add. Comma- or space-separated. Can also use {list_name} syntax. If omitted, uses tag from pipeline payload.",
                    variadic=True,
                ),
                SharedArgs.QUERY,
                SharedArgs.INSTANCE,
                CmdletArg(
                    "-extract",
                    type="string",
                    description=
                    'Extract tags from the item\'s title using a simple template with (field) placeholders. Example: -extract "(artist) - (album) - (disk)-(track) (title)" will add artist:, album:, disk:, track:, title: tags.',
                ),
                CmdletArg(
                    "--extract-debug",
                    type="flag",
                    description=
                    'Debug flag only. Template still goes on -extract, e.g. -extract "(magazine) vol (issue)" --extract-debug',
                ),
                CmdletArg(
                    "-duplicate",
                    type="string",
                    description=
                    "Copy existing tag values to new namespaces. Formats: title:album,artist (explicit) or title,album,artist (inferred)",
                ),
                CmdletArg(
                    "-list",
                    type="string",
                    description=
                    "Load predefined tag lists from adjective.json. Comma-separated list names (e.g., -list philosophy,occult).",
                ),
                CmdletArg(
                    "--all",
                    type="flag",
                    description=
                    "Include temporary files in tagging (by default, only tag non-temporary files).",
                ),
            ],
            detail=[
                "- By default, only tag non-temporary files (from pipelines). Use --all to tag everything.",
                "- Requires a store backend: use -instance or pipe items that include store.",
                "- If -query is not provided, uses the piped item's hash (or derives from its path when possible).",
                "- Multiple tag can be comma-separated or space-separated.",
                "- Use -list to include predefined tag lists from adjective.json: -list philosophy,occult",
                '- tag can also reference lists with curly braces: metadata -add {philosophy} "other:tag"',
                "- Use -duplicate to copy EXISTING tag values to new namespaces:",
                "  Explicit format: -duplicate title:album,artist (copies title: to album: and artist:)",
                "  Inferred format: -duplicate title,album,artist (first is source, rest are targets)",
                "- The source namespace must already exist in the file being tagged.",
                "- Target namespaces that already have a value are skipped (not overwritten).",
                "- Use -extract to derive namespaced tags from the current title (title field or title: tag) using a simple template.",
                '- Extract OR: [ - | : ] in literals, or | between patterns. Example: -extract "part (episode)[-|:](name)".',
                "- Use $(namespace) inside a tag value to insert existing values, e.g. metadata -add \"title:$(track) - $(series)\".",
                "- Format dates inline: metadata -add \"title:$(date, MM/DD/YY) $(series)\".",
                "- Transforms: cutleft($(title), 27), left($(title), 10), padding(2, $(track)). Tab-complete shows signature and types.",
                "- A transform with the wrong arity or type is rejected (not stored as a literal).",
                "- Or <date($(date), MM/DD/YYYY)>. Tokens: YYYY YY MM DD MMM MMMM.",
                "- See docs/tag_template_syntax.md for recipe-style examples and the current shared template syntax.",
            ],
            exec=self.run,
        )
        if register_cmdlet:
            self.register()

    def run(self, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        """Add tag to a file with smart filtering for pipeline results."""
        if should_show_help(args):
            log(f"Cmdlet: {self.name}\nSummary: {self.summary}\nUsage: {self.usage}")
            return 0

        # Parse arguments
        parsed = parse_cmdlet_args(args, self)

        extract_template = parsed.get("extract")
        if extract_template is not None:
            extract_template = str(extract_template).strip() or None

        extract_debug = bool(parsed.get("extract-debug", False))

        raw_tag = parsed.get("tag", [])
        if isinstance(raw_tag, str):
            raw_tag = [raw_tag]

        if not extract_template:
            kept_tags: List[Any] = []
            for item in raw_tag or []:
                text = str(item or "").strip()
                if not extract_template and _looks_like_extract_template(text):
                    extract_template = text
                    continue
                kept_tags.append(item)
            raw_tag = kept_tags

        extract_debug_rx, extract_debug_err = _try_compile_extract_template(extract_template)

        if extract_debug and not extract_template:
            log(
                'metadata -add: --extract-debug needs -extract "(field) ...", e.g. -extract "(magazine) vol (issue) number (number)" --extract-debug',
                file=sys.stderr,
            )
            return 1

        # Normalize input early so a non-hash -query can be treated as the tag payload
        # when the target item is already coming from the pipeline.
        results = normalize_result_input(result)
        if not results:
            try:
                cached = list(ctx.get_last_result_items() or [])
            except Exception:
                cached = []
            if len(cached) == 1:
                results = normalize_result_input(cached)

        query_value = parsed.get("query")
        query_hash = sh.parse_single_hash_query(query_value)
        if query_value and not query_hash:
            if results:
                raw_tag = [str(query_value).strip()] + list(raw_tag or [])
                query_hash = None
            else:
                from SYS.logger import error_panel

                error_panel(
                    "Error",
                    [
                        ("command", "metadata -add"),
                        ("query", str(query_value)),
                        ("expected", "hash:<sha256>, or a tag value when items are piped"),
                    ],
                )
                return 1

        hash_override = query_hash

        # If add-tag is in the middle of a pipeline (has downstream stages), default to
        # including temp files. This enables common flows like:
        #   @N | download-file | add-tag ... | add-file ...
        store_override = parsed.get("instance")
        stage_ctx = ctx.get_stage_context()
        is_last_stage = (stage_ctx is None) or bool(
            getattr(stage_ctx, "is_last_stage", True)
        )
        has_downstream = bool(
            stage_ctx is not None and not getattr(stage_ctx,
                                                  "is_last_stage",
                                                  False)
        )

        include_temp = True

        # When no pipeline payload is present but -query/-instance pinpoints a hash, tag it directly.
        if not results and hash_override and store_override:
            results = [{"hash": hash_override, "store": store_override}]

        if not results:
            log(
                "No valid files to tag (all results were temporary; use --all to include temporary files)",
                file=sys.stderr,
            )
            return 1

        # Fallback: if no tag provided explicitly, try to pull from first result payload.
        # IMPORTANT: when -extract is used, users typically want *only* extracted tags,
        # not "re-add whatever tags are already in the payload".
        if not raw_tag and results and not extract_template:
            first = results[0]
            payload_tag = None

            # Try multiple tag lookup strategies in order
            tag_lookups = [
                lambda x: getattr(x, "tag", None),
                lambda x: x.get("tag") if isinstance(x, dict) else None,
            ]

            for lookup in tag_lookups:
                try:
                    payload_tag = lookup(first)
                    if payload_tag:
                        break
                except (AttributeError, TypeError, KeyError):
                    continue

            if payload_tag:
                if isinstance(payload_tag, str):
                    raw_tag = [payload_tag]
                elif isinstance(payload_tag, list):
                    raw_tag = payload_tag

        # Handle -list argument (convert to {list} syntax)
        list_arg = parsed.get("list")
        if list_arg:
            for l in list_arg.split(","):
                l = l.strip()
                if l:
                    raw_tag.append(f"{{{l}}}")

        # Parse and expand tag
        tag_to_add = parse_tag_arguments(raw_tag)
        tag_to_add = expand_tag_groups(tag_to_add)

        if not tag_to_add and not extract_template:
            log(
                "No tag provided to add (and no -extract template provided)",
                file=sys.stderr
            )
            return 1

        if extract_template and extract_debug and extract_debug_err:
            log(
                f"[add_tag] extract template error: {extract_debug_err}",
                file=sys.stderr
            )
            return 1

        # Get other flags
        duplicate_arg = parsed.get("duplicate")

        # tag ARE provided - apply them to each store-backed result
        total_added = 0
        total_modified = 0
        unresolved_template_count = 0
        store_registry: Any = None

        def _resolve_backend(name: Optional[str]) -> tuple[Any | None, Any, Exception | None]:
            nonlocal store_registry
            backend_name = str(name or "").strip()
            if not backend_name:
                return None, store_registry, KeyError("Missing store name")
            if backend_name in _backend_instance_cache:
                return _backend_instance_cache[backend_name], store_registry, None
            try:
                backend, registry, exc = sh.get_preferred_store_backend(
                    config,
                    backend_name,
                    store_registry=store_registry,
                    suppress_debug=True,
                )
            except TypeError as exc2:
                # Tests may monkeypatch get_store_backend with a reduced signature.
                if "store_registry" in str(exc2):
                    backend, registry, exc = sh.get_store_backend(
                        config,
                        backend_name,
                        suppress_debug=True,
                    )
                else:
                    raise
            if registry is not None:
                store_registry = registry
            if backend is not None:
                _backend_instance_cache[backend_name] = backend
            return backend, store_registry, exc

        pending_bulk_add: Dict[tuple[int, tuple[str, ...], tuple[str, ...]], Dict[str, Any]] = {}
        _backend_instance_cache: Dict[str, Any] = {}

        extract_matched_items = 0
        extract_no_match_items = 0
        display_items: List[Any] = []

        for res in results:
            store_name: Optional[str]
            raw_hash: Optional[str]
            raw_path: Optional[str]

            if isinstance(res, models.PipeObject):
                store_name = store_override or res.store
                raw_hash = res.hash
                raw_path = res.path
            elif isinstance(res, dict):
                store_name = store_override or res.get("store")
                raw_hash = res.get("hash")
                raw_path = res.get("path")
            else:
                ctx.emit(res)
                continue

            if not store_name:
                store_name = None

            # If the item isn't in a configured store backend yet (e.g., store=PATH) but has a local file,
            # treat add-tag as a pipeline mutation (carry tags forward for add-file) instead of a store write.
            if not store_override:
                store_name_str = str(store_name) if store_name is not None else ""
                
                is_known_backend = False
                try:
                    backend_probe, store_registry, _probe_exc = _resolve_backend(store_name_str)
                    is_known_backend = backend_probe is not None
                except Exception:
                    pass

                # If the item isn't in a configured store backend yet (e.g., store=PATH),
                # treat add-tag as a pipeline mutation (carry tags forward for add-file) 
                # instead of a store write.
                if not is_known_backend:
                    try:
                        # We allow metadata updates even if file doesn't exist locally, 
                        # but check path existence if valid path provided.
                        proceed_local = True
                        if raw_path:
                            try:
                                if not Path(str(raw_path)).expanduser().exists():
                                    # If path is provided but missing, we might prefer skipping?
                                    # But for pipeline metadata, purely missing file shouldn't block tagging.
                                    # So we allow it.
                                    pass
                            except Exception:
                                pass
                        
                        if proceed_local:
                            existing_tag_list = _extract_item_tags(res)
                            existing_lower = {
                                t.lower()
                                for t in existing_tag_list if isinstance(t, str)
                            }

                            item_tag_to_add = list(tag_to_add)

                            if extract_template:
                                candidates = _get_title_candidates_for_extraction(
                                    res,
                                    existing_tag_list
                                )
                                extracted, matched = _extract_tags_from_title_candidates(
                                    candidates, extract_template
                                )
                                if extracted:
                                    extract_matched_items += 1
                                    if extract_debug:
                                        log(
                                            f"[add_tag] extract matched: {matched!r} -> {extracted}",
                                            file=sys.stderr,
                                        )
                                    for new_tag in extracted:
                                        if new_tag.lower() not in existing_lower:
                                            item_tag_to_add.append(new_tag)
                                else:
                                    extract_no_match_items += 1
                                    if extract_debug:
                                        rx_preview = (
                                            extract_debug_rx.pattern
                                            if extract_debug_rx else "<uncompiled>"
                                        )
                                        cand_preview = "; ".join(
                                            [repr(c) for c in candidates[:3]]
                                        )
                                        log(
                                            f"[add_tag] extract no match for template {extract_template!r}. regex: {rx_preview!r}. candidates: {cand_preview}",
                                            file=sys.stderr,
                                        )

                            item_tag_to_add = collapse_namespace_tags(
                                item_tag_to_add,
                                "title",
                                prefer="last"
                            )

                            if duplicate_arg:
                                parts = str(duplicate_arg).split(":")
                                source_ns = ""
                                targets: list[str] = []

                                if len(parts) > 1:
                                    source_ns = parts[0]
                                    targets = [
                                        t.strip() for t in parts[1].split(",")
                                        if t.strip()
                                    ]
                                else:
                                    parts2 = str(duplicate_arg).split(",")
                                    if len(parts2) > 1:
                                        source_ns = parts2[0]
                                        targets = [
                                            t.strip() for t in parts2[1:] if t.strip()
                                        ]

                                if source_ns and targets:
                                    source_prefix = source_ns.lower() + ":"
                                    for t in existing_tag_list:
                                        if not t.lower().startswith(source_prefix):
                                            continue
                                        value = t.split(":", 1)[1]
                                        for target_ns in targets:
                                            new_tag = f"{target_ns}:{value}"
                                            if new_tag.lower() not in existing_lower:
                                                item_tag_to_add.append(new_tag)

                            item_tag_to_add, unresolved_templates = render_tag_value_templates(
                                item_tag_to_add,
                                existing_tags=merge_sequences(existing_tag_list, item_tag_to_add, case_sensitive=True),
                                result=res,
                            )
                            unresolved_template_count += len(unresolved_templates)
                            item_tag_to_add = [
                                tag for tag in item_tag_to_add
                                if ":" not in str(tag) or str(tag).split(":", 1)[1].strip()
                            ]

                            adding_title = any(
                                isinstance(t, str) and t.strip().lower().startswith("title:")
                                for t in item_tag_to_add
                            )
                            if adding_title:
                                item_tag_to_add = collapse_namespace_tags(
                                    item_tag_to_add,
                                    "title",
                                    prefer="last"
                                )

                            removed_namespace_tag: list[str] = []
                            for new_tag in item_tag_to_add:
                                if not isinstance(new_tag, str) or ":" not in new_tag:
                                    continue
                                ns = new_tag.split(":", 1)[0].strip()
                                if not ns:
                                    continue
                                if ns.lower() == "title" and not adding_title:
                                    continue
                                ns_prefix = ns.lower() + ":"
                                for t in existing_tag_list:
                                    if (t.lower().startswith(ns_prefix)
                                            and t.lower() != new_tag.lower()):
                                        removed_namespace_tag.append(t)
                            removed_namespace_tag = sorted(
                                {t
                                 for t in removed_namespace_tag}
                            )

                            actual_tag_to_add = [
                                t for t in item_tag_to_add if isinstance(t, str)
                                and t.lower() not in existing_lower
                            ]

                            updated_tag_list = [
                                t for t in existing_tag_list
                                if t not in removed_namespace_tag
                            ]
                            updated_tag_list.extend(actual_tag_to_add)

                            if adding_title:
                                updated_tag_list = collapse_namespace_tags(
                                    updated_tag_list,
                                    "title",
                                    prefer="last"
                                )

                            _set_item_tags(res, updated_tag_list)
                            if actual_tag_to_add:
                                _set_item_tag_display(res, actual_tag_to_add)
                            if adding_title:
                                final_title = _extract_title_tag(updated_tag_list)
                                _apply_title_to_result(res, final_title)

                            total_added += len(actual_tag_to_add)
                            total_modified += (
                                1 if (removed_namespace_tag or actual_tag_to_add) else 0
                            )

                            ctx.emit(res)
                            continue
                    except Exception:
                        pass

                if store_name_str and not is_known_backend:
                    # If it's not a known backend and we didn't handle it above as a local/pipeline 
                    # metadata edit, then it's an error.
                    log(
                        f"[add_tag] Error: Unknown store '{store_name_str}'",
                        file=sys.stderr,
                    )
                    return 1

            resolved_hash = sh.resolve_hash_for_cmdlet(
                str(raw_hash) if raw_hash else None,
                str(raw_path) if raw_path else None,
                str(hash_override) if hash_override else None,
            )

            if not resolved_hash:
                log(
                    "[add_tag] Warning: Item missing usable hash (and could not derive from path); skipping",
                    file=sys.stderr,
                )
                ctx.emit(res)
                continue

            backend, store_registry, exc = _resolve_backend(str(store_name))
            if backend is None:
                log(
                    f"[add_tag] Error: Unknown store '{store_name}': {exc}",
                    file=sys.stderr
                )
                return 1

            inline_tags = _extract_item_tags(res)
            use_inline_tags = bool(inline_tags)

            if use_inline_tags:
                existing_tag_list = [t for t in inline_tags if isinstance(t, str)]
            else:
                try:
                    existing_tag, _src = backend.get_tag(resolved_hash, config=config)
                except Exception:
                    existing_tag = []
                existing_tag_list = [t for t in (existing_tag or []) if isinstance(t, str)]
            existing_lower = {t.lower()
                              for t in existing_tag_list}
            original_title = _extract_title_tag(existing_tag_list)

            # Per-item tag list (do not mutate shared list)
            item_tag_to_add = list(tag_to_add)

            if extract_template:
                candidates2 = _get_title_candidates_for_extraction(
                    res,
                    existing_tag_list
                )
                extracted2, matched2 = _extract_tags_from_title_candidates(
                    candidates2, extract_template
                )
                if extracted2:
                    extract_matched_items += 1
                    if extract_debug:
                        log(
                            f"[add_tag] extract matched: {matched2!r} -> {extracted2}",
                            file=sys.stderr,
                        )
                    for new_tag in extracted2:
                        if new_tag.lower() not in existing_lower:
                            item_tag_to_add.append(new_tag)
                else:
                    extract_no_match_items += 1
                    if extract_debug:
                        rx_preview2 = (
                            extract_debug_rx.pattern
                            if extract_debug_rx else "<uncompiled>"
                        )
                        cand_preview2 = "; ".join([repr(c) for c in candidates2[:3]])
                        log(
                            f"[add_tag] extract no match for template {extract_template!r}. regex: {rx_preview2!r}. candidates: {cand_preview2}",
                            file=sys.stderr,
                        )

            item_tag_to_add = collapse_namespace_tags(
                item_tag_to_add,
                "title",
                prefer="last"
            )

            # Handle -duplicate logic (copy existing tag to new namespaces)
            if duplicate_arg:
                parts = str(duplicate_arg).split(":")
                source_ns = ""
                targets: list[str] = []

                if len(parts) > 1:
                    source_ns = parts[0]
                    targets = [t.strip() for t in parts[1].split(",") if t.strip()]
                else:
                    parts2 = str(duplicate_arg).split(",")
                    if len(parts2) > 1:
                        source_ns = parts2[0]
                        targets = [t.strip() for t in parts2[1:] if t.strip()]

                if source_ns and targets:
                    source_prefix = source_ns.lower() + ":"
                    for t in existing_tag_list:
                        if not t.lower().startswith(source_prefix):
                            continue
                        value = t.split(":", 1)[1]
                        for target_ns in targets:
                            new_tag = f"{target_ns}:{value}"
                            if new_tag.lower() not in existing_lower:
                                item_tag_to_add.append(new_tag)

            item_tag_to_add, unresolved_templates = render_tag_value_templates(
                item_tag_to_add,
                existing_tags=merge_sequences(existing_tag_list, item_tag_to_add, case_sensitive=True),
                result=res,
            )
            unresolved_template_count += len(unresolved_templates)
            item_tag_to_add = [
                tag for tag in item_tag_to_add
                if ":" not in str(tag) or str(tag).split(":", 1)[1].strip()
            ]

            adding_title = any(
                isinstance(t, str) and t.strip().lower().startswith("title:")
                for t in item_tag_to_add
            )
            if adding_title:
                item_tag_to_add = collapse_namespace_tags(
                    item_tag_to_add,
                    "title",
                    prefer="last"
                )

            changed = False
            refreshed_list = list(existing_tag_list)
            try:
                from SYS.metadata import compute_namespaced_tag_overwrite
            except Exception:
                compute_namespaced_tag_overwrite = None  # type: ignore

            tags_to_remove: List[str] = []
            tags_to_add: List[str] = []
            merged_tags: List[str] = list(existing_tag_list)
            if compute_namespaced_tag_overwrite:
                try:
                    tags_to_remove, tags_to_add, merged_tags = compute_namespaced_tag_overwrite(
                        existing_tag_list,
                        item_tag_to_add,
                    )
                except Exception:
                    tags_to_remove = []
                    tags_to_add = []
                    merged_tags = list(existing_tag_list)

            queued_bulk = False
            ok_add = False
            add_tags_bulk_fn = getattr(backend, "add_tags_bulk", None)
            if tags_to_add and callable(add_tags_bulk_fn):
                add_key = tuple(sorted({str(t).strip().lower() for t in tags_to_add if str(t).strip()}))
                remove_key = tuple(sorted({str(t).strip().lower() for t in tags_to_remove if str(t).strip()}))
                if add_key:
                    batch_key = (id(backend), add_key, remove_key)
                    bucket = pending_bulk_add.get(batch_key)
                    if bucket is None:
                        bucket = {
                            "backend": backend,
                            "add_tags": list(add_key),
                            "remove_tags": list(remove_key),
                            "hashes": [],
                        }
                        pending_bulk_add[batch_key] = bucket
                    bucket["hashes"].append(resolved_hash)
                    queued_bulk = True
                    ok_add = True

            if not queued_bulk:
                ok_add = False
                ok_remove = True
                try:
                    ok_add = backend.add_tag(
                        resolved_hash,
                        item_tag_to_add,
                        config=config,
                        existing_tags=existing_tag_list,
                    )
                except Exception as exc:
                    log(f"[add_tag] Warning: Failed adding tag: {exc}", file=sys.stderr)
                    ok_add = False
                if tags_to_remove:
                    try:
                        delete_fn = getattr(backend, "delete_tag", None)
                        if callable(delete_fn):
                            ok_remove = delete_fn(resolved_hash, list(tags_to_remove), config=config)
                    except Exception as exc:
                        log(f"[add_tag] Warning: Failed removing tag: {exc}", file=sys.stderr)
                        ok_remove = False
                if not ok_add and not ok_remove:
                    log("[add_tag] Warning: Store rejected tag update", file=sys.stderr)

            if ok_add and merged_tags:
                refreshed_list = list(merged_tags)
            else:
                refreshed_list = list(existing_tag_list)

            if tags_to_add or tags_to_remove:
                changed = True
                total_added += len(tags_to_add)
                total_modified += 1

            # Update the result's tag using canonical field
            if isinstance(res, models.PipeObject):
                res.tag = refreshed_list
            elif isinstance(res, dict):
                res["tag"] = refreshed_list
            if tags_to_add:
                _set_item_tag_display(res, tags_to_add)

            if adding_title:
                final_title = _extract_title_tag(refreshed_list)
                _apply_title_to_result(res, final_title)
            if tags_to_add or tags_to_remove or adding_title:
                try:
                    ctx.patch_cached_result_items(
                        file_hash=resolved_hash,
                        instance=str(store_name or ""),
                        path=raw_path,
                        title=_extract_title_tag(refreshed_list) if adding_title else None,
                        tags=refreshed_list,
                    )
                except Exception:
                    pass

            if changed and not use_inline_tags:
                _refresh_tag_view(res, resolved_hash, str(store_name), raw_path, config)

            if is_last_stage:
                display_items.append(res)

            ctx.emit(res)

        for bucket in pending_bulk_add.values():
            backend = bucket.get("backend")
            add_tags_for_batch = list(bucket.get("add_tags") or [])
            remove_tags_for_batch = list(bucket.get("remove_tags") or [])
            hashes_for_batch = [str(h).strip().lower() for h in (bucket.get("hashes") or []) if str(h).strip()]
            if backend is None or not hashes_for_batch:
                continue

            batch_items = [(h, list(add_tags_for_batch), list(remove_tags_for_batch)) for h in hashes_for_batch]
            add_tags_bulk_fn = getattr(backend, "add_tags_bulk", None)
            applied = False
            if callable(add_tags_bulk_fn):
                try:
                    applied = bool(add_tags_bulk_fn(batch_items))
                except Exception:
                    applied = False

            if applied:
                continue

            # Fallback path: retain correctness if backend bulk call fails.
            for h in hashes_for_batch:
                try:
                    backend.add_tag(h, list(add_tags_for_batch), config=config)
                except Exception as exc:
                    log(f"[add_tag] Warning: Failed fallback add_tag for {h}: {exc}", file=sys.stderr)

        from SYS.logger import status_panel

        status_panel(
            "metadata -add",
            [
                ("added", total_added),
                ("items", len(results)),
                ("modified", total_modified),
            ],
        )

        if (not has_downstream) and (display_items or results):
            display_items = display_items or list(results)
            try:
                live_progress = ctx.get_live_progress()
            except Exception:
                live_progress = None

            if live_progress is not None:
                try:
                    pipe_idx = getattr(stage_ctx, "pipe_index", None)
                    if isinstance(pipe_idx, int):
                        live_progress.finish_pipe(int(pipe_idx), force_complete=True)
                except Exception:
                    pass
                try:
                    live_progress.stop()
                except Exception:
                    pass
                try:
                    if hasattr(ctx, "set_live_progress"):
                        ctx.set_live_progress(None)
                except Exception:
                    pass

            try:
                subject = display_items[0] if len(display_items) == 1 else list(display_items)
                # Use helper to display items and make them @-selectable
                display_type = "item" if len(display_items) <= _DETAIL_PANEL_LIMIT else "custom"
                sh.display_and_persist_items(
                    list(display_items),
                    title="Result",
                    subject=subject,
                    display_type=display_type,
                )
            except Exception:
                pass

            try:
                if stage_ctx is not None:
                    stage_ctx.emits = []
            except Exception:
                pass

        if extract_template and extract_matched_items == 0:
            log(
                f"[add_tag] extract: no matches for template '{extract_template}' across {len(results)} item(s)",
                file=sys.stderr,
            )
        elif extract_template and extract_no_match_items > 0 and extract_debug:
            log(
                f"[add_tag] extract: matched {extract_matched_items}, no-match {extract_no_match_items}",
                file=sys.stderr,
            )

        if unresolved_template_count > 0:
            log(
                f"[add_tag] skipped {unresolved_template_count} tag template(s) with unresolved placeholders or invalid function calls",
                file=sys.stderr,
            )

        return 0


CMDLET = Add_Tag(register_cmdlet=False)

