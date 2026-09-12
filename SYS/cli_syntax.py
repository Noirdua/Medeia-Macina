from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import re

from SYS.command_parsing import extract_arg_value


@dataclass(frozen=True)
class SyntaxErrorDetail:
    message: str
    expected: Optional[str] = None


def _split_pipeline_stages(text: str) -> list[str]:
    """Split a pipeline command into stage strings on unquoted '|' characters."""
    raw = str(text or "")
    if not raw:
        return []

    stages: list[str] = []
    buf: list[str] = []
    quote: Optional[str] = None
    escaped = False

    for ch in raw:
        if escaped:
            buf.append(ch)
            escaped = False
            continue

        if ch == "\\" and quote is not None:
            buf.append(ch)
            escaped = True
            continue

        if ch in ('"', "'"):
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None
            buf.append(ch)
            continue

        if ch == "|" and quote is None:
            stage = "".join(buf).strip()
            if stage:
                stages.append(stage)
            buf = []
            continue

        buf.append(ch)

    tail = "".join(buf).strip()
    if tail:
        stages.append(tail)
    return stages


def _tokenize_stage(stage_text: str) -> list[str]:
    """Tokenize a stage string (best-effort)."""
    import shlex

    text = str(stage_text or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text)
    except Exception:
        return text.split()


def _parse_pipeline_tokens(raw: str) -> list[tuple[str, list[str]]]:
    parsed: list[tuple[str, list[str]]] = []
    for stage in _split_pipeline_stages(raw):
        tokens = _tokenize_stage(stage)
        if not tokens:
            continue
        cmd = str(tokens[0]).replace("_", "-").strip().lower()
        parsed.append((cmd, tokens))
    return parsed


def _has_flag(tokens: list[str], *flags: str) -> bool:
    want = {str(f).strip().lower() for f in flags if str(f).strip()}
    if not want:
        return False
    for tok in tokens:
        low = str(tok).strip().lower()
        if low in want:
            return True
        # Support -arg=value
        if "=" in low:
            head = low.split("=", 1)[0].strip()
            if head in want:
                return True
    return False


def _get_flag_value(tokens: list[str], *flags: str) -> Optional[str]:
    return extract_arg_value(tokens, flags=flags)


def _validate_add_note_requires_add_file_order(raw: str) -> Optional[SyntaxErrorDetail]:
    """Enforce: add-note in piped mode must occur after add-file.

    Rationale: add-note requires a known (store, hash) target; piping before add-file
    means the item likely has no hash yet.
    """
    parsed = _parse_pipeline_tokens(raw)
    if len(parsed) <= 1:
        return None

    add_file_positions = [i for i, (cmd, _toks) in enumerate(parsed) if cmd == "add-file"]
    if not add_file_positions:
        return None

    for i, (cmd, tokens) in enumerate(parsed):
        if cmd != "add-note":
            continue

        # If add-note occurs before any add-file stage, it must be explicitly targeted.
        if any(pos > i for pos in add_file_positions):
            has_hash = _has_flag(tokens, "-hash", "--hash")
            has_store = _has_flag(tokens, "-instance", "--instance")

            query_val = _get_flag_value(tokens, "-query", "--query")
            has_store_hash_in_query = False
            if query_val:
                try:
                    parsed_q = parse_query(str(query_val))
                    q_hash = get_query_field(parsed_q, "hash") or get_query_field(parsed_q, "sha256")
                    q_store = get_query_field(parsed_q, "instance") or get_query_field(parsed_q, "store")
                    has_store_hash_in_query = bool(
                        str(q_hash or "").strip() and str(q_store or "").strip()
                    )
                except Exception:
                    has_store_hash_in_query = False

            if (has_hash and has_store) or has_store_hash_in_query:
                continue
            return SyntaxErrorDetail(
                "Pipeline error: 'add-note' must come after 'add-file' when used with piped input. "
                "Move 'add-note' after 'add-file', or call it with explicit targeting: "
                'add-note -query "instance:<instance> hash:<sha256> title:<title>,text:<text>".'
            )

    return None


def _validate_file_cmdlet_stage_actions(raw: str) -> Optional[SyntaxErrorDetail]:
    parsed = _parse_pipeline_tokens(raw)
    if not parsed:
        return None

    try:
        from cmdlet.file_cmdlet import File as FileCmdlet
    except Exception:
        return None

    for cmd, tokens in parsed:
        if cmd != "file":
            continue

        action, _passthrough, seen = FileCmdlet._extract_action(tokens[1:])
        if action is not None:
            continue

        if seen:
            rendered = ", ".join(f"-{name}" for name in seen)
            return SyntaxErrorDetail(
                f"Pipeline error: 'file' has conflicting actions ({rendered}); choose exactly one."
            )

        if _has_flag(tokens[1:], "-plugin", "--plugin", "-instance", "--instance", "-path", "--path"):
            return SyntaxErrorDetail(
                "Pipeline error: 'file' requires an explicit action here. "
                "Use 'file -add -plugin local -instance <name|path>' for local export, or 'file -search ...' for search."
            )

        return SyntaxErrorDetail(
            "Pipeline error: 'file' requires -search/-query for search or exactly one action flag "
            "(-search, -add, -delete, -merge, -download, -convert, -trim, -archive)."
        )

    return None


def _validate_metadata_cmdlet_stage_actions(raw: str) -> Optional[SyntaxErrorDetail]:
    parsed = _parse_pipeline_tokens(raw)
    if not parsed:
        return None

    try:
        from cmdlet.metadata_cmdlet import Metadata
    except Exception:
        return None

    for cmd, tokens in parsed:
        if cmd not in {"metadata", "meta"}:
            continue

        action, _domain, _passthrough, seen_actions, seen_domains = Metadata._extract_parts(
            tokens[1:]
        )
        if Metadata._auto_flag_present(tokens[1:]) and "auto" not in Metadata._plugin_metadata_actions():
            return SyntaxErrorDetail(
                "metadata -auto requires the metadata+ plugin. "
                "Install with .plugin -add metadata_plus. Pipeline was not started."
            )
        if action is None:
            if seen_actions:
                rendered = ", ".join(f"-{name}" for name in seen_actions)
                return SyntaxErrorDetail(
                    f"Pipeline error: 'metadata' has conflicting actions ({rendered}); choose exactly one."
                )
            available = ", ".join(f"-{name}" for name in Metadata._all_action_flags())
            return SyntaxErrorDetail(
                f"Pipeline error: 'metadata' requires exactly one action flag "
                f"({available}). Pipeline was not started."
            )
        if len(seen_domains) > 1:
            rendered = ", ".join(f"-{name}" for name in seen_domains)
            return SyntaxErrorDetail(
                f"Pipeline error: 'metadata' has conflicting domains ({rendered}); choose one. "
                "Pipeline was not started."
            )

    return None


def _validate_add_file_stage_preflight(
    raw: str,
    config: Optional[Dict[str, Any]],
) -> Optional[SyntaxErrorDetail]:
    if not isinstance(config, dict):
        return None

    parsed = _parse_pipeline_tokens(raw)
    if not parsed:
        return None

    try:
        from cmdlet.file.add import Add_File
        from cmdlet.file_cmdlet import File as FileCmdlet
    except Exception:
        return None

    for cmd, tokens in parsed:
        stage_args: Optional[list[str]] = None

        if cmd == "add-file":
            stage_args = tokens[1:]
        elif cmd == "file":
            action, passthrough, _seen = FileCmdlet._extract_action(tokens[1:])
            if action == "add":
                stage_args = passthrough

        if stage_args is None:
            continue

        message = Add_File.validate_preflight_args(stage_args, config)
        if message:
            return SyntaxErrorDetail(message)

    return None


def validate_pipeline_text(
    text: str,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[SyntaxErrorDetail]:
    """Validate raw CLI input before tokenization/execution.

    This is intentionally lightweight and focuses on user-facing syntax issues:
    - Unbalanced single/double quotes
    - Dangling or empty pipeline stages (|)

    Returns:
        None if valid, otherwise a SyntaxErrorDetail describing the issue.
    """
    if text is None:
        return SyntaxErrorDetail("Empty command")

    raw = text.strip()
    if not raw:
        return SyntaxErrorDetail("Empty command")

    in_single = False
    in_double = False
    escaped = False
    last_pipe_outside_quotes: Optional[int] = None

    for idx, ch in enumerate(raw):
        if escaped:
            escaped = False
            continue

        if ch == "\\" and (in_single or in_double):
            escaped = True
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            continue

        if ch == "'" and not in_double:
            in_single = not in_single
            continue

        if ch == "|" and not in_single and not in_double:
            # Record pipe locations to catch empty stages/dangling pipe.
            if last_pipe_outside_quotes is not None and last_pipe_outside_quotes == idx - 1:
                return SyntaxErrorDetail("Syntax error: empty pipeline stage (found '||').")
            last_pipe_outside_quotes = idx

    if in_double:
        return SyntaxErrorDetail("Syntax error: missing closing " + '"' + ".", expected='"')
    if in_single:
        return SyntaxErrorDetail("Syntax error: missing closing '.", expected="'")

    # Dangling pipe at end / pipe as first non-space character
    if raw.startswith("|"):
        return SyntaxErrorDetail("Syntax error: pipeline cannot start with '|'.")
    if raw.endswith("|"):
        return SyntaxErrorDetail("Syntax error: pipeline cannot end with '|'.")

    # Empty stage like "cmd1 | | cmd2" (spaces between pipes)
    if "|" in raw:
        # Simple pass: look for pipes that have only whitespace between them.
        # We only check outside quotes by re-scanning and counting non-space chars between pipes.
        in_single = False
        in_double = False
        escaped = False
        seen_nonspace_since_pipe = True  # start true to allow leading command
        for ch in raw:
            if escaped:
                escaped = False
                continue
            if ch == "\\" and (in_single or in_double):
                escaped = True
                continue
            if ch == '"' and not in_single:
                in_double = not in_double
                continue
            if ch == "'" and not in_double:
                in_single = not in_single
                continue
            if ch == "|" and not in_single and not in_double:
                if not seen_nonspace_since_pipe:
                    return SyntaxErrorDetail(
                        "Syntax error: empty pipeline stage (use a command between '|')."
                    )
                seen_nonspace_since_pipe = False
                continue
            if not in_single and not in_double and not ch.isspace():
                seen_nonspace_since_pipe = True

        # Pipeline-only semantic rules.
        semantic_error = _validate_add_note_requires_add_file_order(raw)
        if semantic_error is not None:
            return semantic_error

    semantic_error = _validate_file_cmdlet_stage_actions(raw)
    if semantic_error is not None:
        return semantic_error

    semantic_error = _validate_metadata_cmdlet_stage_actions(raw)
    if semantic_error is not None:
        return semantic_error

    semantic_error = _validate_add_file_stage_preflight(raw, config)
    if semantic_error is not None:
        return semantic_error

    return None


def parse_query(query: str) -> Dict[str, Any]:
    """Parse a query string into field:value pairs and free text.

    Supports syntax like:
      - isbn:0557677203
      - author:"Albert Pike"
      - title:"Morals and Dogma" year:2010
      - Mixed with free text: Morals isbn:0557677203

    Returns:
        Dict with keys:
          - fields: Dict[str, str]
          - text: str
          - raw: str
    """

    result: Dict[str, Any] = {
        "fields": {},
        "text": "",
        "raw": query,
    }

    if not query or not query.strip():
        return result

    raw = query.strip()
    remaining_parts: list[str] = []

    # Match field:value where value is either a quoted string or a non-space token.
    pattern = r'(\w+):(?:"([^"]*)"|(\S+))'

    pos = 0
    for match in re.finditer(pattern, raw):
        if match.start() > pos:
            before_text = raw[pos : match.start()].strip()
            if before_text:
                remaining_parts.append(before_text)

        field_name = (match.group(1) or "").lower()
        field_value = match.group(2) if match.group(2) is not None else match.group(3)
        if field_name:
            result["fields"][field_name] = field_value

        pos = match.end()

    if pos < len(raw):
        remaining_text = raw[pos:].strip()
        if remaining_text:
            remaining_parts.append(remaining_text)

    result["text"] = " ".join(remaining_parts)
    return result


def get_query_field(
    parsed_query: Dict[str, Any], field_name: str, default: Optional[str] = None
) -> Optional[str]:
    return parsed_query.get("fields", {}).get((field_name or "").lower(), default)


get_field = get_query_field


def get_free_text(parsed_query: Dict[str, Any]) -> str:
    """Get the free-text portion of a parsed query."""

    return str(parsed_query.get("text", "") or "")
