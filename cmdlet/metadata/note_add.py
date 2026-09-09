from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
import sys
import re

from SYS.logger import log

from SYS import pipeline as ctx
from .. import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
QueryArg = sh.QueryArg
SharedArgs = sh.SharedArgs
normalize_hash = sh.normalize_hash
parse_cmdlet_args = sh.parse_cmdlet_args
normalize_result_input = sh.normalize_result_input
should_show_help = sh.should_show_help


class Add_Note(Cmdlet):
    DEFAULT_QUERY_HINTS = (
        "title:",
        "text:",
        "hash:",
        "caption:",
        "sub:",
        "subtitle:",
    )

    def __init__(self) -> None:
        super().__init__(
            name="add-note",
            summary="Add file store note",
            usage=
            'add-note (-query "title:<title>,text:<text>[,instance:<instance>][,hash:<sha256>]") [ -instance <store> | <piped> ]',
            alias=[""],
            arg=[
                SharedArgs.INSTANCE,
                QueryArg(
                    "hash",
                    key="hash",
                    aliases=["sha256"],
                    type="string",
                    required=False,
                    handler=normalize_hash,
                    description=
                    "(Optional) Specific file hash target, provided via -query as hash:<sha256>. When omitted, uses piped item hash.",
                    query_only=True,
                ),
                SharedArgs.QUERY,
            ],
            detail=["""
                dde
                """],
            exec=self.run,
        )
        # Populate dynamic store choices for autocomplete
        try:
            SharedArgs.INSTANCE.choices = SharedArgs.get_store_choices(None)
        except Exception:
            pass
        self.register()

    @staticmethod
    def _commas_to_spaces_outside_quotes(text: str) -> str:
        buf: List[str] = []
        quote: Optional[str] = None
        escaped = False
        for ch in str(text or ""):
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
            if ch == "," and quote is None:
                buf.append(" ")
                continue
            buf.append(ch)
        return "".join(buf)

    @staticmethod
    def _parse_note_query(query: str) -> Tuple[Optional[str], Optional[str]]:
        """Parse note payload from -query.

        Expected:
            title:<title>,text:<text>
        Commas are treated as separators when not inside quotes.
        """
        raw = str(query or "").strip()
        if not raw:
            return None, None

        try:
            from SYS.cli_syntax import parse_query, get_field
        except Exception:
            parse_query = None  # type: ignore
            get_field = None  # type: ignore

        normalized = Add_Note._commas_to_spaces_outside_quotes(raw)

        if callable(parse_query) and callable(get_field):
            parsed = parse_query(normalized)
            name = get_field(parsed, "title")
            text = get_field(parsed, "text")
            name_s = str(name or "").strip() if name is not None else ""
            text_s = str(text or "").strip() if text is not None else ""
            return (name_s or None, text_s or None)

        # Fallback: best-effort regex.
        name_match = re.search(
            r"\btitle\s*:\s*([^,\s]+)",
            normalized,
            flags=re.IGNORECASE
        )
        text_match = re.search(r"\btext\s*:\s*(.+)$", normalized, flags=re.IGNORECASE)
        note_name = name_match.group(1).strip() if name_match else ""
        note_text = text_match.group(1).strip() if text_match else ""
        return (note_name or None, note_text or None)

    @classmethod
    def _looks_like_note_query_token(cls, token: Any) -> bool:
        text = str(token or "").strip().lower()
        if not text:
            return False
        return any(hint in text for hint in cls.DEFAULT_QUERY_HINTS)

    @classmethod
    def _default_query_args(cls, args: Sequence[str]) -> List[str]:
        tokens: List[str] = list(args or [])
        lower_tokens = {str(tok).lower() for tok in tokens if tok is not None}
        if "-query" in lower_tokens or "--query" in lower_tokens:
            return tokens

        for idx, tok in enumerate(tokens):
            token_text = str(tok or "")
            if not token_text or token_text.startswith("-"):
                continue
            if not cls._looks_like_note_query_token(token_text):
                continue

            combined_parts = [token_text]
            end = idx + 1
            while end < len(tokens):
                next_text = str(tokens[end] or "")
                if not next_text or next_text.startswith("-"):
                    break
                if not cls._looks_like_note_query_token(next_text):
                    break
                combined_parts.append(next_text)
                end += 1

            combined_query = " ".join(combined_parts)
            tokens[idx:end] = [combined_query]
            tokens.insert(idx, "-query")
            return tokens

        return tokens

    def run(self, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        if should_show_help(args):
            log(f"Cmdlet: {self.name}\nSummary: {self.summary}\nUsage: {self.usage}")
            return 0

        parsed_args = self._default_query_args(args)
        parsed = parse_cmdlet_args(parsed_args, self)

        store_override = parsed.get("instance")
        hash_override = normalize_hash(parsed.get("hash"))
        note_name, note_text = self._parse_note_query(str(parsed.get("query") or ""))
        note_name = str(note_name or "").strip()
        note_text = str(note_text or "").strip()
        if not note_name or not note_text:
            pass # We now support implicit pipeline notes if -query is missing
            # But if explicit targeting (store+hash) is used, we still demand args below.

        if hash_override and not store_override:
            log(
                "[add_note] Error: hash:<sha256> requires instance:<instance> in -query or -instance <store>",
                file=sys.stderr,
            )
            return 1

        explicit_target = bool(hash_override and store_override)
        results = normalize_result_input(result)

        if explicit_target and (not note_name or not note_text):
            log(
                "[add_note] Error: Explicit target (store+hash) requires -query with title/text",
                file=sys.stderr,
            )
            return 1

        if results and explicit_target:
            # Direct targeting mode: apply note once to the explicit target and
            # pass through any piped items unchanged.
            try:
                backend, _store_registry, exc = sh.get_store_backend(
                    config,
                    str(store_override),
                )
                if backend is None:
                    raise exc or KeyError(store_override)
                if not bool(getattr(backend, "supports_note_association", False)):
                    log(
                        f"[add_note] Error: Store '{store_override}' does not support notes",
                        file=sys.stderr,
                    )
                    return 1
                ok = bool(
                    backend.set_note(
                        str(hash_override),
                        note_name,
                        note_text,
                        config=config
                    )
                )
                if ok:
                    ctx.print_if_visible(
                        f"✓ add-note: 1 item in '{store_override}'",
                        file=sys.stderr
                    )
                    log(
                        "[add_note] Updated 1/1 item(s)",
                        file=sys.stderr
                    )
                    for res in results:
                        ctx.emit(res)
                    return 0
                log(
                    "[add_note] Warning: Note write reported failure",
                    file=sys.stderr
                )
                return 1
            except Exception as exc:
                log(f"[add_note] Error: Failed to set note: {exc}", file=sys.stderr)
                return 1

        if not results:
            if explicit_target:
                # Allow standalone use (no piped input) and enable piping the target forward.
                results = [{
                    "store": str(store_override),
                    "hash": hash_override
                }]
            else:
                log(
                    '[add_note] Error: Requires piped item(s) from add-file, or explicit targeting via store/hash (e.g., -query "instance:<instance> hash:<sha256> ...")',
                    file=sys.stderr,
                )
                return 1

            store_registry = None
        planned_ops = 0

        # Batch write plan: store -> [(hash, name, text), ...]
        note_ops: Dict[str,
                       List[Tuple[str,
                                  str,
                                  str]]] = {}

        for res in results:
            if not isinstance(res, dict):
                ctx.emit(res)
                continue

            # Determine notes to write for this item
            notes_to_write: List[Tuple[str, str]] = []
            
            # 1. Explicit arguments always take precedence
            if note_name and note_text:
                notes_to_write.append((note_name, note_text))
            
            # 2. Pipeline notes auto-ingestion
            # Look for 'notes' dictionary in the item (propagated by pipeline/download-file)
            # Structure: {'notes': {'lyric': '...', 'sub': '...'}}
            # Check both root and nested 'extra'
            
            # Check root 'notes' (dict or extra.notes)
            pipeline_notes = res.get("notes")
            if not isinstance(pipeline_notes, dict):
                extra = res.get("extra")
                if isinstance(extra, dict):
                    pipeline_notes = extra.get("notes")
                    
            if isinstance(pipeline_notes, dict):
                for k, v in pipeline_notes.items():
                    # If arg-provided note conflicts effectively with pipeline note? 
                    # We just append both.
                    if v and str(v).strip():
                        notes_to_write.append((str(k), str(v)))

            if not notes_to_write:
                # Pass through items that have nothing to add
                ctx.emit(res)
                continue

            store_name, resolved_hash = sh.resolve_item_store_hash(
                res,
                override_store=str(store_override) if store_override else None,
                override_hash=str(hash_override) if hash_override else None,
                path_fields=("path",),
            )

            if not store_name:
                log(
                    "[add_note] Error: Missing -instance and item has no store field",
                    file=sys.stderr
                )
                continue

            if not resolved_hash:
                log(
                    "[add_note] Warning: Item missing usable hash; skipping",
                    file=sys.stderr
                )
                ctx.emit(res)
                continue
            
            # Queue operations
            if store_name not in note_ops:
                note_ops[store_name] = []
            
            for (n_name, n_text) in notes_to_write:
                note_ops[store_name].append((resolved_hash, n_name, n_text))
                planned_ops += 1
                
            ctx.emit(res)


        # Execute batch operations
        def _on_store_error(store_name: str, exc: Exception) -> None:
            log(f"[add_note] Store access failed '{store_name}': {exc}", file=sys.stderr)

        def _on_unsupported_store(store_name: str) -> None:
            log(f"[add_note] Store '{store_name}' does not support notes", file=sys.stderr)

        def _on_item_error(store_name: str, hash_value: str, note_name_value: str, exc: Exception) -> None:
            log(f"[add_note] Write failed {store_name}:{hash_value} ({note_name_value}): {exc}", file=sys.stderr)

        store_registry, success_count = sh.run_store_note_batches(
            config,
            note_ops,
            store_registry=store_registry,
            on_store_error=_on_store_error,
            on_unsupported_store=_on_unsupported_store,
            on_item_error=_on_item_error,
        )

        if planned_ops > 0:
            msg = f"✓ add-note: Updated {success_count}/{planned_ops} notes across {len(note_ops)} stores"
            ctx.print_if_visible(msg, file=sys.stderr)
            
        return 0


CMDLET = Add_Note()

