"""
Pipeline execution engine for cmdlet.
"""
from __future__ import annotations

from collections.abc import Iterable
import sys
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Callable

from SYS.cmdlet_catalog import import_cmd_module
from SYS.logger import debug, log
from SYS.pipeline_state import (
    HELP_EXAMPLE_SOURCE_COMMANDS,
    _worker,
    _cli_parsing,
    _result_table,
    _split_pipeline_tokens,
    _emit_selection_debug_panel,
)

import logging

logger = logging.getLogger(__name__)


# SYS.rich_display deferred imports are handled inline where needed.
# SYS.background_notifier deferred imports are handled inline where needed.


class PipelineExecutor:
    def __init__(self, *, config_loader: Optional[Any] = None) -> None:
        self._config_loader = config_loader
        self._toolbar_output: Optional[Callable[[str], None]] = None

    def _load_config(self) -> Dict[str, Any]:
        try:
            if self._config_loader is not None:
                return self._config_loader.load()
        except Exception:
            logger.exception(
                "Failed to use config_loader.load(); falling back to SYS.config.load_config"
            )
        try:
            from SYS.config import load_config

            return load_config()
        except Exception:
            return {}

    def set_toolbar_output(self, output: Optional[Callable[[str], None]]) -> None:
        self._toolbar_output = output

    # -------------------------------------------------------------------
    # Static helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _split_stages(tokens: Sequence[str]) -> List[List[str]]:
        stages: List[List[str]] = []
        current: List[str] = []
        for token in tokens:
            if token == "|":
                if current:
                    stages.append(current)
                    current = []
            else:
                current.append(token)
        if current:
            stages.append(current)
        return stages

    @staticmethod
    def _stage_file_action(stage_tokens: Sequence[Any]) -> Optional[str]:
        if not stage_tokens:
            return None

        head = str(stage_tokens[0] or "").replace("_", "-").strip().lower()
        if head in {"add-file", "download-file", "delete-file"}:
            return head
        if head != "file":
            return None
        args = {str(t).replace("_", "-").strip().lower() for t in stage_tokens[1:]}
        if "-add" in args or "--add" in args:
            return "add-file"
        if "-download" in args or "--download" in args or "-dl" in args or "--dl" in args:
            return "download-file"
        if "-delete" in args or "--delete" in args or "-del" in args or "--del" in args:
            return "delete-file"
        return None

    @staticmethod
    def _stage_progress_label(stage_tokens: Sequence[Any]) -> str:
        if not stage_tokens:
            return "stage"
        head = str(stage_tokens[0] or "").replace("_", "-").strip().lower()
        action = PipelineExecutor._stage_file_action(stage_tokens)
        if action == "download-file":
            return "download"
        if action == "add-file":
            if PipelineExecutor._stage_dest_path(stage_tokens):
                return "save"
            toks = [str(t) for t in stage_tokens[1:]]
            for idx, tok in enumerate(toks):
                low = tok.replace("_", "-").strip().lower()
                if low in {"-plugin", "--plugin"} and idx + 1 < len(toks):
                    nxt = str(toks[idx + 1] or "").strip()
                    if nxt and not nxt.startswith("-"):
                        return "upload"
                if low.startswith("-plugin=") or low.startswith("--plugin="):
                    return "upload"
            return "add"
        if action == "delete-file":
            return "delete"
        if head == "file":
            args = {
                str(t).replace("_", "-").strip().lower() for t in stage_tokens[1:]
            }
            for name in ("search", "convert", "trim", "archive", "merge"):
                if f"-{name}" in args or f"--{name}" in args:
                    return name
        return head or "stage"

    @staticmethod
    def _stage_dest_path(stage_tokens: Sequence[Any]) -> Optional[str]:
        tokens = [str(t) for t in (stage_tokens or [])]
        for idx, tok in enumerate(tokens):
            low = tok.replace("_", "-").strip().lower()
            raw = ""
            if low in {"-path", "--path"} and idx + 1 < len(tokens):
                nxt = str(tokens[idx + 1] or "").strip()
                if nxt and not nxt.startswith("-"):
                    raw = nxt
            elif low.startswith("-path=") or low.startswith("--path="):
                raw = tok.split("=", 1)[1].strip()
            if not raw:
                continue
            try:
                dest = Path(raw).expanduser()
            except Exception:
                return raw
            if dest.is_file():
                return None
            return raw
        return None

    @staticmethod
    def _item_existing_local_path(item: Any) -> Optional[Path]:
        candidates: List[Any] = []
        try:
            from SYS.item_accessors import get_field

            candidates.extend(
                [
                    get_field(item, "path"),
                    get_field(item, "file"),
                    get_field(item, "local_path"),
                ]
            )
        except Exception:
            pass
        if isinstance(item, dict):
            candidates.extend([item.get("path"), item.get("file"), item.get("local_path")])
        seen: set[str] = set()
        for raw in candidates:
            text = str(raw or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            low = text.lower()
            if low.startswith("http://") or low.startswith("https://"):
                continue
            try:
                path = Path(text).expanduser()
            except Exception:
                continue
            try:
                if path.is_file():
                    return path
            except Exception:
                continue
        return None

    @staticmethod
    def _selection_has_local_files(
        indices: Sequence[int],
        items: Sequence[Any],
    ) -> bool:
        if not indices:
            return False
        for idx in indices:
            item = None
            try:
                if 0 <= int(idx) < len(items):
                    item = items[int(idx)]
            except Exception:
                item = None
            if PipelineExecutor._item_existing_local_path(item) is None:
                return False
        return True

    @staticmethod
    def _preflight_pipeline_stages(stages: List[List[str]]) -> Optional[str]:
        """Return an error string if any stage is invalid, before running work."""
        for stage in stages or []:
            if not stage:
                continue
            cmd = str(stage[0] or "").replace("_", "-").strip().lower()
            args = list(stage[1:])
            if cmd in {"file"}:
                try:
                    from cmdlet.file_cmdlet import File

                    action, _passthrough, seen = File._extract_action(args)
                except Exception:
                    continue
                if action is None:
                    if not seen:
                        return (
                            "file: missing action flag. Fix the pipeline before download starts "
                            "(use -search, -add, -download, …)."
                        )
                    return (
                        "file: conflicting actions ("
                        + ", ".join(f"-{name}" for name in seen)
                        + "). Fix the pipeline before it runs."
                    )
            if cmd in {"metadata", "meta"}:
                try:
                    from cmdlet.metadata_cmdlet import Metadata

                    action, _domain, _passthrough, seen_actions, seen_domains = Metadata._extract_parts(args)
                except Exception:
                    continue
                if Metadata._auto_flag_present(args) and "auto" not in Metadata._plugin_metadata_actions():
                    return (
                        "metadata -auto requires the metadata+ plugin. "
                        "Install with .plugin -add metadata_plus. Pipeline was not started."
                    )
                if action is None:
                    if not seen_actions:
                        available = ", ".join(f"-{name}" for name in Metadata._all_action_flags())
                        return (
                            f"metadata: missing action flag; choose exactly one of "
                            f"{available}. Pipeline was not started."
                        )
                    return (
                        "metadata: conflicting actions ("
                        + ", ".join(f"-{name}" for name in seen_actions)
                        + "). Pipeline was not started."
                    )
                if len(seen_domains) > 1:
                    return (
                        "metadata: conflicting domains ("
                        + ", ".join(f"-{name}" for name in seen_domains)
                        + "). Pipeline was not started."
                    )
        return None

    @staticmethod
    def _validate_download_file_relationship_order(stages: List[List[str]]) -> bool:
        def _norm(name: str) -> str:
            return str(name or "").replace("_", "-").strip().lower()

        def _file_action(stage_tokens: List[str]) -> Optional[str]:
            if not stage_tokens:
                return None
            head = _norm(stage_tokens[0])
            if head in {"download-file", "add-file"}:
                return head
            if head != "file":
                return None
            args = {_norm(t) for t in stage_tokens[1:]}
            if "-download" in args or "--download" in args or "-dl" in args or "--dl" in args:
                return "download-file"
            if "-add" in args or "--add" in args:
                return "add-file"
            return None

        names: List[str] = []
        for stage in stages or []:
            if not stage:
                continue
            names.append(_norm(stage[0]))

        dl_idxs = [
            i for i, stage in enumerate(stages or []) if _file_action(stage or []) == "download-file"
        ]
        rel_idxs = [i for i, n in enumerate(names) if n == "add-relationship"]
        add_file_idxs = [
            i for i, stage in enumerate(stages or []) if _file_action(stage or []) == "add-file"
        ]

        if not dl_idxs or not rel_idxs:
            return True

        for rel_i in rel_idxs:
            dl_before = [d for d in dl_idxs if d < rel_i]
            if not dl_before:
                continue
            dl_i = max(dl_before)
            if not any(dl_i < a < rel_i for a in add_file_idxs):
                log(
                    "Pipeline order error: when using download-file with add-relationship, "
                    "add-relationship must come after add-file (so items are stored and have hash).\n"
                    "Example: download-file <...> | add-file -instance <name> | add-relationship",
                    file=sys.stderr,
                )
                return False

        return True

    @staticmethod
    def _try_clear_pipeline_stop(ctx: Any) -> None:
        try:
            if hasattr(ctx, "clear_pipeline_stop"):
                ctx.clear_pipeline_stop()
        except Exception:
            logger.exception("Failed to clear pipeline stop via ctx.clear_pipeline_stop")

    @staticmethod
    def _maybe_seed_current_stage_table(ctx: Any) -> None:
        try:
            if hasattr(ctx, "get_current_stage_table") and not ctx.get_current_stage_table():
                display_table = (
                    ctx.get_display_table() if hasattr(ctx, "get_display_table") else None
                )
                if display_table:
                    ctx.set_current_stage_table(display_table)
                else:
                    last_table = (
                        ctx.get_last_result_table()
                        if hasattr(ctx, "get_last_result_table")
                        else None
                    )
                    if last_table:
                        ctx.set_current_stage_table(last_table)
        except Exception:
            logger.exception("Failed to seed current_stage_table from display or last table")

    @staticmethod
    def _maybe_apply_pending_pipeline_tail(
        ctx: Any, stages: List[List[str]]
    ) -> List[List[str]]:
        try:
            pending_tail = (
                ctx.get_pending_pipeline_tail()
                if hasattr(ctx, "get_pending_pipeline_tail")
                else []
            )
            pending_source = (
                ctx.get_pending_pipeline_source()
                if hasattr(ctx, "get_pending_pipeline_source")
                else None
            )
        except Exception:
            pending_tail = []
            pending_source = None

        try:
            current_source = (
                ctx.get_current_stage_table_source_command()
                if hasattr(ctx, "get_current_stage_table_source_command")
                else None
            )
        except Exception:
            current_source = None

        try:
            effective_source = current_source or (
                ctx.get_last_result_table_source_command()
                if hasattr(ctx, "get_last_result_table_source_command")
                else None
            )
        except Exception:
            effective_source = current_source

        selection_start = bool(stages and stages[0] and stages[0][0].startswith("@"))

        def _tail_is_suffix(existing: List[List[str]], tail: List[List[str]]) -> bool:
            if not tail or not existing:
                return False
            if len(tail) > len(existing):
                return False
            return existing[-len(tail) :] == tail

        if pending_tail and selection_start:
            if (pending_source is None) or (
                effective_source and pending_source == effective_source
            ):
                if not _tail_is_suffix(stages, pending_tail):
                    stages = list(stages) + list(pending_tail)
                try:
                    if hasattr(ctx, "clear_pending_pipeline_tail"):
                        ctx.clear_pending_pipeline_tail()
                except Exception:
                    logger.exception(
                        "Failed to clear pending pipeline tail after appending pending tail"
                    )
            else:
                try:
                    if hasattr(ctx, "clear_pending_pipeline_tail"):
                        ctx.clear_pending_pipeline_tail()
                except Exception:
                    logger.exception(
                        "Failed to clear pending pipeline tail (source mismatch branch)"
                    )
        elif pending_tail:
            try:
                if hasattr(ctx, "clear_pending_pipeline_tail"):
                    ctx.clear_pending_pipeline_tail()
            except Exception:
                logger.exception("Failed to clear stale pending pipeline tail")
        return stages

    def _apply_quiet_background_flag(self, config: Any) -> Any:
        if isinstance(config, dict):
            try:
                from SYS.rich_display import terminal_supports_live_progress

                is_tty = terminal_supports_live_progress()
            except Exception:
                is_tty = False
            config["_quiet_background_output"] = not is_tty
        return config

    @staticmethod
    def _extract_first_stage_selection_tokens(
        stages: List[List[str]],
    ) -> tuple[List[List[str]], List[int], bool, bool]:
        first_stage_tokens = stages[0] if stages else []
        first_stage_selection_indices: List[int] = []
        first_stage_had_extra_args = False
        first_stage_select_all = False

        if first_stage_tokens:
            new_first_stage: List[str] = []
            for token in first_stage_tokens:
                if token.startswith("@"):
                    selection = _cli_parsing().SelectionSyntax.parse(token)
                    if selection is not None:
                        first_stage_selection_indices = [i - 1 for i in selection]
                        continue
                    if token == "@*":
                        first_stage_select_all = True
                        continue
                new_first_stage.append(token)

            if new_first_stage:
                stages = list(stages)
                stages[0] = new_first_stage
                if first_stage_selection_indices or first_stage_select_all:
                    first_stage_had_extra_args = True
            elif first_stage_selection_indices or first_stage_select_all:
                stages = list(stages)
                stages.pop(0)

        return (
            stages,
            first_stage_selection_indices,
            first_stage_had_extra_args,
            first_stage_select_all,
        )

    @staticmethod
    def _apply_select_all_if_requested(
        ctx: Any, indices: List[int], select_all: bool
    ) -> List[int]:
        if not select_all:
            return indices
        try:
            last_items = ctx.get_last_result_items()
        except Exception:
            last_items = None
        if last_items:
            return list(range(len(last_items)))
        return indices

    @staticmethod
    def _maybe_run_class_selector(
        ctx: Any,
        config: Any,
        selected_items: list,
        *,
        stage_is_last: bool,
        source_command: Any = None,
        prefer_detail_fallback: bool = False,
    ) -> bool:
        if not stage_is_last:
            return False
        table_kind = ""
        try:
            current = ctx.get_current_stage_table() or ctx.get_last_result_table()
            table_kind = str(getattr(current, "table", "") or "").strip().lower()
        except Exception:
            table_kind = ""
        if table_kind.startswith("metadata."):
            return False

        candidates: list[str] = []
        seen: set[str] = set()
        current_table = None
        table_meta = None
        table_type = ""

        def _add(value: Any) -> None:
            try:
                text = str(value or "").strip().lower()
            except Exception as exc:
                logger.debug(
                    "Failed to normalize candidate value: %s", exc, exc_info=True
                )
                return
            if not text or text in seen:
                return
            seen.add(text)
            candidates.append(text)

        try:
            current_table = ctx.get_current_stage_table() or ctx.get_last_result_table()
            _add(
                current_table.table
                if current_table and hasattr(current_table, "table")
                else None
            )
            if current_table and hasattr(current_table, "table"):
                table_type = str(getattr(current_table, "table", "") or "").strip()

            try:
                meta = (
                    current_table.get_table_metadata()
                    if current_table is not None
                    and hasattr(current_table, "get_table_metadata")
                    else getattr(current_table, "table_metadata", None)
                )
            except Exception:
                meta = None
            table_meta = meta if isinstance(meta, dict) else None
            if isinstance(meta, dict):
                _add(meta.get("plugin"))
        except Exception:
            logger.exception(
                "Failed to inspect current_table/table metadata in _maybe_run_class_selector"
            )

        for item in selected_items or []:
            if isinstance(item, dict):
                _add(item.get("plugin"))
                _add(item.get("store"))
                _add(item.get("table"))
            else:
                _add(getattr(item, "plugin", None))
                _add(getattr(item, "store", None))
                _add(getattr(item, "table", None))

        try:
            from PluginCore.registry import get_plugin, is_known_plugin_name
        except Exception:
            get_plugin = None  # type: ignore
            is_known_plugin_name = None  # type: ignore

        if is_known_plugin_name is not None:
            try:
                for key in list(candidates):
                    if not isinstance(key, str):
                        continue
                    if "." not in key:
                        continue
                    if is_known_plugin_name(key):
                        continue
                    prefix = str(key).split(".", 1)[0].strip().lower()
                    if prefix and is_known_plugin_name(prefix):
                        _add(prefix)
            except Exception:
                logger.exception(
                    "Failed while computing plugin prefix heuristics in _maybe_run_class_selector"
                )

        if get_plugin is not None:
            for key in candidates:
                try:
                    if is_known_plugin_name is not None and (
                        not is_known_plugin_name(key)
                    ):
                        continue
                except Exception:
                    logger.exception(
                        "is_known_plugin_name predicate failed for key %s; falling back",
                        key,
                    )
                try:
                    provider = get_plugin(key, config)
                except Exception as exc:
                    logger.exception(
                        "Failed to load plugin '%s' during selector resolution: %s",
                        key,
                        exc,
                    )
                    continue
                selector = getattr(provider, "selector", None)
                if selector is None:
                    continue
                try:
                    handled = bool(
                        selector(selected_items, ctx=ctx, stage_is_last=True)
                    )
                except Exception as exc:
                    logger.exception(
                        "%s selector failed during selection: %s", key, exc
                    )
                    return True
                if handled:
                    return True

                if prefer_detail_fallback:
                    detail_renderer = getattr(provider, "show_selection_details", None)
                    if callable(detail_renderer):
                        try:
                            detail_handled = bool(
                                detail_renderer(
                                    selected_items,
                                    ctx=ctx,
                                    stage_is_last=True,
                                    source_command=str(source_command or ""),
                                    table_type=table_type,
                                    table_metadata=table_meta,
                                )
                            )
                        except Exception as exc:
                            logger.exception(
                                "%s detail fallback failed during selection: %s",
                                key,
                                exc,
                            )
                            return True
                        if detail_handled:
                            return True

        store_keys: list[str] = []
        for item in selected_items or []:
            if isinstance(item, dict):
                v = item.get("store")
            else:
                v = getattr(item, "store", None)
            name = str(v or "").strip()
            if name:
                store_keys.append(name)

        if store_keys:
            try:
                from PluginCore.backend_registry import BackendRegistry

                backend_registry = BackendRegistry(config, suppress_debug=True)
                _backend_names = list(backend_registry.list_backends() or [])
                _backend_by_lower = {
                    str(n).lower(): str(n) for n in _backend_names if str(n).strip()
                }
                for name in store_keys:
                    resolved_name = name
                    if not backend_registry.is_available(resolved_name):
                        resolved_name = _backend_by_lower.get(
                            str(name).lower(), name
                        )
                    if not backend_registry.is_available(resolved_name):
                        continue
                    backend = backend_registry[resolved_name]
                    selector = getattr(backend, "selector", None)
                    if selector is None:
                        continue
                    handled = bool(
                        selector(selected_items, ctx=ctx, stage_is_last=True)
                    )
                    if handled:
                        return True
            except Exception:
                logger.exception(
                    "Failed while running store-based selector logic in _maybe_run_class_selector"
                )

        return False

    @staticmethod
    def _maybe_expand_plugin_selection(
        selected_items: List[Any],
        *,
        ctx: Any,
        config: Dict[str, Any],
        stage_table: Any,
    ) -> Optional[List[Any]]:
        candidates: list[str] = []

        def _add(value: Any) -> None:
            text = str(value or "").strip().lower()
            if text and text not in candidates:
                candidates.append(text)

        table_type = None
        try:
            table_type = (
                stage_table.table
                if stage_table is not None and hasattr(stage_table, "table")
                else None
            )
        except Exception:
            table_type = None
        _add(table_type)

        try:
            meta = (
                stage_table.get_table_metadata()
                if stage_table is not None and hasattr(stage_table, "get_table_metadata")
                else getattr(stage_table, "table_metadata", None)
            )
        except Exception:
            meta = None
        if isinstance(meta, dict):
            _add(meta.get("plugin"))

        for item in selected_items or []:
            if isinstance(item, dict):
                _add(item.get("plugin"))
                _add(item.get("table"))
                _add(item.get("source"))
            else:
                _add(getattr(item, "plugin", None))
                _add(getattr(item, "table", None))
                _add(getattr(item, "source", None))

        try:
            from PluginCore.registry import get_plugin, is_known_plugin_name
        except Exception:
            return None

        for key in list(candidates):
            if "." in key:
                prefix = str(key).split(".", 1)[0].strip().lower()
                if prefix and prefix not in candidates:
                    candidates.append(prefix)

        for key in candidates:
            try:
                if not is_known_plugin_name(key):
                    continue
            except Exception:
                continue
            try:
                plugin = get_plugin(key, config)
            except Exception:
                continue
            if plugin is None:
                continue
            expand = getattr(plugin, "expand_selection", None)
            if not callable(expand):
                continue
            try:
                expanded = expand(
                    selected_items,
                    ctx=ctx,
                    stage_is_last=False,
                    table_type=str(table_type or ""),
                )
            except Exception:
                logger.exception("%s expand_selection failed", key)
                return None
            if expanded is None:
                continue
            if not isinstance(expanded, Iterable):
                logger.error(
                    "%s expand_selection returned a non-iterable result",
                    key,
                )
                continue
            try:
                expanded_items = list(expanded)
            except TypeError:
                logger.error(
                    "%s expand_selection returned a non-iterable result",
                    key,
                )
                continue
            if expanded_items:
                return expanded_items
        return None

    @staticmethod
    def _summarize_stage_text(stage_tokens: Sequence[str], limit: int = 140) -> str:
        combined = " ".join(str(tok) for tok in stage_tokens if tok is not None).strip()
        if not combined:
            return ""
        normalized = re.sub(r"\s+", " ", combined)
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3].rstrip() + "..."

    @staticmethod
    def _log_pipeline_event(
        worker_manager: Any,
        worker_id: Optional[str],
        message: str,
    ) -> None:
        if not worker_manager or not worker_id or not message:
            return
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            timestamp = ""
        if timestamp:
            text = f"{timestamp} - PIPELINE - {message}"
        else:
            text = f"PIPELINE - {message}"
        try:
            worker_manager.append_stdout(worker_id, text + "\n", channel="log")
        except Exception:
            logger.exception(
                "Failed to append pipeline event to worker stdout for %s", worker_id
            )

    @staticmethod
    def _maybe_open_url_selection(
        current_table: Any,
        selected_items: list,
        *,
        stage_is_last: bool,
    ) -> bool:
        if not stage_is_last:
            return False
        if not selected_items or len(selected_items) != 1:
            return False

        table_type = ""
        source_cmd = ""
        try:
            table_type = (
                str(getattr(current_table, "table", "") or "").strip().lower()
            )
        except Exception:
            table_type = ""
        try:
            source_cmd = (
                str(getattr(current_table, "source_command", "") or "")
                .strip()
                .replace("_", "-")
                .lower()
            )
        except Exception:
            source_cmd = ""

        if table_type != "url" and source_cmd != "get-url":
            return False

        item = selected_items[0]
        url = None
        try:
            from SYS.field_access import get_field

            url = get_field(item, "url")
        except Exception:
            try:
                url = (
                    item.get("url")
                    if isinstance(item, dict)
                    else getattr(item, "url", None)
                )
            except Exception:
                url = None

        url_text = str(url or "").strip()
        if not url_text:
            return False

        try:
            import webbrowser

            webbrowser.open(url_text, new=2)
            return True
        except Exception:
            return False

    # -------------------------------------------------------------------
    # Instance helpers
    # -------------------------------------------------------------------

    def _maybe_enable_background_notifier(
        self,
        worker_manager: Any,
        config: Any,
        pipeline_session: Any,
    ) -> None:
        if not (pipeline_session and worker_manager and isinstance(config, dict)):
            return

        session_worker_ids = config.get("_session_worker_ids")
        if not session_worker_ids:
            return

        try:
            output_fn = self._toolbar_output
            quiet_mode = bool(config.get("_quiet_background_output"))
            terminal_only = quiet_mode and not output_fn
            kwargs: Dict[str, Any] = {
                "session_worker_ids": session_worker_ids,
                "only_terminal_updates": terminal_only,
                "overlay_mode": bool(output_fn),
            }
            if output_fn:
                kwargs["output"] = output_fn
            from SYS.background_notifier import ensure_background_notifier

            ensure_background_notifier(worker_manager, **kwargs)
        except Exception:
            logger.exception(
                "Failed to enable background notifier for session_worker_ids=%r",
                session_worker_ids,
            )

    # -------------------------------------------------------------------
    # Selection expansion
    # -------------------------------------------------------------------

    def _maybe_apply_initial_selection(
        self,
        ctx: Any,
        config: Any,
        stages: List[List[str]],
        *,
        selection_indices: List[int],
        first_stage_had_extra_args: bool,
        worker_manager: Any,
        pipeline_session: Any,
    ) -> tuple[bool, Any]:
        if not selection_indices:
            return True, None

        # ------------------------------------------------------------------
        # PHASE 1: Synchronize current stage table with display table
        # ------------------------------------------------------------------
        display_table = None
        try:
            display_table = (
                ctx.get_display_table() if hasattr(ctx, "get_display_table") else None
            )
        except Exception:
            display_table = None

        current_stage_table = None
        try:
            current_stage_table = (
                ctx.get_current_stage_table()
                if hasattr(ctx, "get_current_stage_table")
                else None
            )
        except Exception:
            current_stage_table = None

        try:
            if display_table is not None and hasattr(ctx, "set_current_stage_table"):
                ctx.set_current_stage_table(display_table)
            elif current_stage_table is None and hasattr(ctx, "set_current_stage_table"):
                last_table = (
                    ctx.get_last_result_table()
                    if hasattr(ctx, "get_last_result_table")
                    else None
                )
                if last_table is not None:
                    ctx.set_current_stage_table(last_table)
        except Exception:
            logger.exception(
                "Failed to sync current_stage_table from display/last table in _maybe_apply_initial_selection"
            )

        # ------------------------------------------------------------------
        # Helper functions for row action/args discovery
        # ------------------------------------------------------------------
        def _get_row_action(
            idx: int, items_cache: Optional[List[Any]] = None
        ) -> Optional[List[str]]:
            try:
                action = ctx.get_current_stage_table_row_selection_action(idx)
                if action:
                    return [str(x) for x in action if x is not None]
            except Exception:
                pass

            resolved_items: List[Any] = items_cache if items_cache is not None else []
            if items_cache is None:
                try:
                    resolved_items = list(ctx.get_last_result_items() or [])
                except Exception:
                    resolved_items = []

            if 0 <= idx < len(resolved_items):
                item = resolved_items[idx]
                if isinstance(item, dict):
                    candidate = item.get("_selection_action")
                    if isinstance(candidate, (list, tuple)):
                        return [str(x) for x in candidate if x is not None]
            return None

        def _get_row_args(
            idx: int, items_cache: Optional[List[Any]] = None
        ) -> Optional[List[str]]:
            try:
                args = ctx.get_current_stage_table_row_selection_args(idx)
                if args:
                    return [str(x) for x in args if x is not None]
            except Exception:
                pass

            resolved_items: List[Any] = items_cache if items_cache is not None else []
            if items_cache is None:
                try:
                    resolved_items = list(ctx.get_last_result_items() or [])
                except Exception:
                    resolved_items = []

            if 0 <= idx < len(resolved_items):
                item = resolved_items[idx]
                if isinstance(item, dict):
                    candidate = item.get("_selection_args")
                    if isinstance(candidate, (list, tuple)):
                        return [str(x) for x in candidate if x is not None]
            return None

        def _norm_cmd_name(value: Any) -> str:
            return str(value or "").replace("_", "-").strip().lower()

        # ------------------------------------------------------------------
        # PHASE 2: Parse source command and table metadata
        # ------------------------------------------------------------------
        source_cmd = None
        source_args_raw = None
        try:
            source_cmd = ctx.get_current_stage_table_source_command()
            source_args_raw = ctx.get_current_stage_table_source_args()
        except Exception:
            source_cmd = None
            source_args_raw = None

        if isinstance(source_args_raw, str):
            source_args: List[str] = [source_args_raw]
        elif isinstance(source_args_raw, list):
            source_args = [str(x) for x in source_args_raw if x is not None]
        else:
            source_args = []

        current_table = None
        try:
            current_table = ctx.get_current_stage_table()
        except Exception:
            current_table = None
        table_type = (
            current_table.table
            if current_table and hasattr(current_table, "table")
            else None
        )

        # ------------------------------------------------------------------
        # PHASE 3: Handle command expansion for @N syntax
        # ------------------------------------------------------------------
        example_selector_triggered = False
        normalized_source_cmd = (
            str(source_cmd or "").replace("_", "-").strip().lower()
        )
        prefer_row_action = False
        preferred_row_action = None

        if normalized_source_cmd in HELP_EXAMPLE_SOURCE_COMMANDS and selection_indices:
            try:
                idx = selection_indices[0]
                row_args = ctx.get_current_stage_table_row_selection_args(idx)
            except Exception:
                row_args = None
            tokens: List[str] = []
            if isinstance(row_args, list) and row_args:
                tokens = [str(x) for x in row_args if x is not None]
            if tokens:
                stage_groups = _split_pipeline_tokens(tokens)
                if stage_groups:
                    for stage in reversed(stage_groups):
                        stages.insert(0, stage)
                    selection_indices = []
                    example_selector_triggered = True

        if not example_selector_triggered:
            if not (
                table_type in {"youtube", "soulseek"}
                or (source_cmd == "search-file" and source_args and "youtube" in source_args)
            ):
                selected_row_args: List[str] = []
                skip_pipe_expansion = source_cmd in {".pipe", ".mpv"} and len(stages) > 0
                if len(selection_indices) == 1 and not stages:
                    try:
                        row_action = _get_row_action(selection_indices[0])
                    except Exception:
                        row_action = None
                    if row_action:
                        replay = _split_pipeline_tokens(row_action)
                        if replay:
                            stages[:] = replay
                        else:
                            stages.insert(0, list(row_action))
                        return True, None

                if source_cmd and not skip_pipe_expansion and not prefer_row_action:
                    src = str(source_cmd).replace("_", "-").strip().lower()

                    if src == "add-file" and selection_indices:
                        row_args_list: List[List[str]] = []
                        for idx in selection_indices:
                            try:
                                row_args = ctx.get_current_stage_table_row_selection_args(
                                    idx
                                )
                            except Exception:
                                row_args = None
                            if isinstance(row_args, list) and row_args:
                                row_args_list.append(
                                    [str(x) for x in row_args if x is not None]
                                )

                        paths: List[str] = []
                        can_merge = bool(row_args_list) and (
                            len(row_args_list) == len(selection_indices)
                        )
                        if can_merge:
                            for ra in row_args_list:
                                if len(ra) == 1:
                                    p = str(ra[0]).strip()
                                    if p:
                                        paths.append(p)
                                else:
                                    can_merge = False
                                    break

                        if can_merge and paths:
                            selected_row_args.append(",".join(paths))
                        elif len(selection_indices) == 1 and row_args_list:
                            selected_row_args.extend(row_args_list[0])
                    else:
                        if len(selection_indices) == 1:
                            idx = selection_indices[0]
                            row_args = ctx.get_current_stage_table_row_selection_args(idx)
                            if row_args:
                                selected_row_args.extend(row_args)

                if selected_row_args and not stages:
                    if isinstance(source_cmd, list):
                        cmd_list: List[str] = [
                            str(x) for x in source_cmd if x is not None
                        ]
                    elif isinstance(source_cmd, str):
                        cmd_list = [source_cmd]
                    else:
                        cmd_list = []

                    expanded_stage: List[str] = (
                        cmd_list + selected_row_args + source_args
                    )
                    stages.insert(0, expanded_stage)

                    if pipeline_session and worker_manager:
                        try:
                            worker_manager.log_step(
                                pipeline_session.worker_id,
                                f"@N expansion: {source_cmd} + selected_args={selected_row_args} + source_args={source_args}",
                            )
                        except Exception:
                            logger.exception(
                                "Failed to record pipeline log step for @N expansion (pipeline_session=%r)",
                                getattr(pipeline_session, "worker_id", None),
                            )
                elif selected_row_args and stages:
                    pass

            stage_table = None
            try:
                stage_table = ctx.get_current_stage_table()
            except Exception:
                stage_table = None

            display_table = None
            try:
                display_table = (
                    ctx.get_display_table()
                    if hasattr(ctx, "get_display_table")
                    else None
                )
            except Exception:
                display_table = None

            if not stage_table and display_table is not None:
                stage_table = display_table
            if not stage_table:
                try:
                    stage_table = ctx.get_last_result_table()
                except Exception:
                    stage_table = None

            # ----------------------------------------------------------------
            # PHASE 4: Retrieve and filter items from current result set
            # ----------------------------------------------------------------
            try:
                items_list = ctx.get_last_result_items() or []
            except Exception as exc:
                debug(f"@N: Exception getting items_list: {exc}")
                items_list = []
            if not items_list and stage_table is not None:
                recovered: List[Any] = []
                try:
                    for row in getattr(stage_table, "rows", []) or []:
                        payload = getattr(row, "payload", None)
                        if payload is not None:
                            recovered.append(payload)
                except Exception:
                    recovered = []
                if recovered:
                    items_list = recovered
            resolved_items = items_list if items_list else []
            if items_list:
                filtered = [
                    resolved_items[i]
                    for i in selection_indices
                    if 0 <= i < len(resolved_items)
                ]
                if selection_indices:
                    if len(selection_indices) == 1:
                        selection_label = f"@{selection_indices[0] + 1}"
                    else:
                        selection_label = (
                            "@{"
                            + ",".join(str(idx + 1) for idx in selection_indices)
                            + "}"
                        )
                else:
                    selection_label = "@selection"
                _emit_selection_debug_panel(
                    selection_token=selection_label,
                    selection_indices=selection_indices,
                    item_count=len(items_list),
                    filtered_count=len(filtered),
                    stage_table_present=(stage_table is not None),
                    display_table_present=(display_table is not None),
                    stage_is_last=(not stages),
                    row_action=preferred_row_action,
                    downstream_stages=stages,
                    mode=("row_action" if preferred_row_action else "selection"),
                    source_table=stage_table or display_table,
                )
                if not filtered:
                    total = len(items_list)
                    wanted = ",".join(str(idx + 1) for idx in selection_indices) or "?"
                    log(
                        f"@{wanted} matched 0 of {total} row(s). "
                        f"{'Use @1.' if total == 1 else f'Use @1..@{total}.' if total else 'No result table is loaded.'}",
                        file=sys.stderr,
                    )
                    return False, None

                if stages:
                    expanded = PipelineExecutor._maybe_expand_plugin_selection(
                        filtered,
                        ctx=ctx,
                        config=config,
                        stage_table=stage_table,
                    )
                    if expanded:
                        filtered = expanded

                if PipelineExecutor._maybe_run_class_selector(
                    ctx,
                    config,
                    filtered,
                    stage_is_last=(not stages),
                    source_command=source_cmd,
                    prefer_detail_fallback=bool(
                        prefer_row_action
                        and not stages
                        and len(selection_indices) == 1
                    ),
                ):
                    return False, None

                from SYS.pipe_object import coerce_to_pipe_object

                filtered_pipe_objs = [
                    coerce_to_pipe_object(item) for item in filtered
                ]
                piped_result = (
                    filtered_pipe_objs
                    if len(filtered_pipe_objs) > 1
                    else filtered_pipe_objs[0]
                )

                if pipeline_session and worker_manager:
                    try:
                        selection_parts = [f"@{i+1}" for i in selection_indices]
                        worker_manager.log_step(
                            pipeline_session.worker_id,
                            f"Applied @N selection {' | '.join(selection_parts)}",
                        )
                    except Exception:
                        logger.exception(
                            "Failed to record Applied @N selection log step (pipeline_session=%r)",
                            getattr(pipeline_session, "worker_id", None),
                        )

                try:
                    current_table = ctx.get_current_stage_table()
                    if current_table is None and hasattr(ctx, "get_display_table"):
                        current_table = ctx.get_display_table()
                    if current_table is None:
                        current_table = ctx.get_last_result_table()
                except Exception:
                    logger.exception(
                        "Failed to determine current_table for selection auto-insert; defaulting to None"
                    )
                    current_table = None
                table_type_hint = None
                try:
                    raw_table_type = (
                        stage_table.table
                        if stage_table is not None and hasattr(stage_table, "table")
                        else None
                    )
                    if isinstance(raw_table_type, str) and raw_table_type.strip():
                        table_type_hint = raw_table_type
                except Exception:
                    table_type_hint = None
                table_type = None
                try:
                    if isinstance(table_type_hint, str) and table_type_hint.strip():
                        table_type = table_type_hint
                    else:
                        table_type = (
                            current_table.table
                            if current_table and hasattr(current_table, "table")
                            else None
                        )
                except Exception:
                    logger.exception(
                        "Failed to compute table_type from current_table; using fallback attribute access"
                    )
                    table_type = (
                        current_table.table
                        if current_table and hasattr(current_table, "table")
                        else None
                    )

                auto_stage = None
                if isinstance(table_type, str) and table_type:
                    try:
                        from PluginCore.registry import (
                            selection_auto_stage_for_table,
                        )

                        auto_stage = selection_auto_stage_for_table(table_type)
                    except Exception:
                        auto_stage = None

                source_cmd_for_selection = None
                source_args_for_selection: List[str] = []
                try:
                    source_cmd_for_selection = (
                        ctx.get_current_stage_table_source_command()
                        or ctx.get_last_result_table_source_command()
                    )
                    source_args_for_selection = (
                        ctx.get_current_stage_table_source_args()
                        or ctx.get_last_result_table_source_args()
                        or []
                    )
                except Exception:
                    source_cmd_for_selection = None
                    source_args_for_selection = []

                if not stages and selection_indices and source_cmd_for_selection:
                    src_norm = _norm_cmd_name(source_cmd_for_selection)
                    if src_norm in {".worker", "worker", "workers"}:
                        if len(selection_indices) == 1:
                            idx = selection_indices[0]
                            row_args = _get_row_args(idx, items_list)
                            if row_args:
                                stages.append(
                                    [str(source_cmd_for_selection)]
                                    + row_args
                                    + [
                                        str(x)
                                        for x in source_args_for_selection
                                        if x is not None
                                    ]
                                )

                def _apply_row_action_to_stage(stage_idx: int) -> bool:
                    if not selection_indices or len(selection_indices) != 1:
                        return False
                    row_action = _get_row_action(selection_indices[0], items_list)
                    if not row_action:
                        return False
                    if 0 <= stage_idx < len(stages):
                        stages[stage_idx] = row_action
                        return True
                    return False

                # ----------------------------------------------------------------
                # PHASE 5: Auto-insert stages based on table type and user selection
                # ----------------------------------------------------------------
                if not stages:
                    if str(table_type or "") == "metadata.isbn":
                        row_action = None
                        if selection_indices and len(selection_indices) == 1:
                            row_action = _get_row_action(
                                selection_indices[0], items_list
                            )
                        if row_action:
                            stages.append(list(row_action))
                        else:
                            stages.append(["metadata", "-add"])
                        debug("Applying ISBN match via metadata -add")
                    elif isinstance(table_type, str) and table_type.startswith(
                        "metadata."
                    ):
                        debug("Auto-applying metadata selection via metadata -get")
                        stages.append(["metadata", "-get"])
                    elif auto_stage:
                        try:
                            debug(f"Auto-running selection via {auto_stage[0]}")
                        except Exception:
                            logger.exception(
                                "Failed to print auto-run selection message for %s",
                                auto_stage[0],
                            )
                        stages.append(list(auto_stage))
                        debug(
                            f"Inserted auto stage before row action: {stages[-1]}"
                        )

                        if selection_indices:
                            try:
                                if not _apply_row_action_to_stage(len(stages) - 1):
                                    if len(selection_indices) == 1:
                                        idx = selection_indices[0]
                                        row_args = _get_row_args(idx, items_list)
                                        if row_args:
                                            inserted = stages[-1]
                                            if inserted:
                                                cmd = inserted[0]
                                                tail = [
                                                    str(x) for x in inserted[1:]
                                                ]
                                                stages[-1] = [cmd] + row_args + tail
                            except Exception:
                                logger.exception(
                                    "Failed to attach selection args to auto-inserted stage"
                                )

                    if (
                        not stages
                        and selection_indices
                        and len(selection_indices) == 1
                    ):
                        row_action = _get_row_action(selection_indices[0], items_list)
                        if row_action:
                            stages.append(row_action)
                            if pipeline_session and worker_manager:
                                try:
                                    worker_manager.log_step(
                                        pipeline_session.worker_id,
                                        f"@N applied row action -> {' '.join(row_action)}",
                                    )
                                except Exception:
                                    logger.exception(
                                        "Failed to record pipeline log step for applied row action (pipeline_session=%r)",
                                        getattr(
                                            pipeline_session, "worker_id", None
                                        ),
                                    )
                else:
                    first_cmd = stages[0][0] if stages and stages[0] else None
                    first_cmd_norm = _norm_cmd_name(first_cmd)

                    inserted_provider_download = False
                    if (
                        PipelineExecutor._stage_file_action(stages[0])
                        == "add-file"
                    ):
                        dest_path = PipelineExecutor._stage_dest_path(stages[0])
                        local_ready = PipelineExecutor._selection_has_local_files(
                            selection_indices, items_list
                        )
                        if dest_path and len(selection_indices) == 1 and not local_ready:
                            row_args = _get_row_args(
                                selection_indices[0], items_list
                            )
                            if row_args:
                                stage = list(stages[0])
                                existing = {
                                    str(tok).replace("_", "-").strip().lower()
                                    for tok in stage
                                }
                                if "-url" not in existing and "--url" not in existing:
                                    stages[0] = [stage[0]] + [
                                        str(x) for x in row_args if x is not None
                                    ] + stage[1:]
                        elif (not local_ready) and (not dest_path) and len(selection_indices) == 1:
                            row_action = _get_row_action(
                                selection_indices[0], items_list
                            )
                            if (
                                row_action
                                and PipelineExecutor._stage_file_action(row_action)
                                == "download-file"
                            ):
                                stages.insert(
                                    0,
                                    [str(x) for x in row_action if x is not None],
                                )
                                inserted_provider_download = True
                                debug(
                                    "Auto-inserting row download-file action before add-file"
                                )

                        if (
                            (not inserted_provider_download)
                            and (not dest_path)
                            and (not local_ready)
                            and len(selection_indices) > 1
                        ):
                            try:
                                has_download_row_action = False
                                for idx in selection_indices:
                                    row_action = _get_row_action(
                                        idx, items_list
                                    )
                                    if (
                                        row_action
                                        and PipelineExecutor._stage_file_action(
                                            row_action
                                        )
                                        == "download-file"
                                    ):
                                        has_download_row_action = True
                                        break
                                if has_download_row_action:
                                    stages.insert(0, ["file", "-download"])
                                    inserted_provider_download = True
                                    debug(
                                        "Auto-inserting download-file before add-file for provider selection"
                                    )
                            except Exception:
                                pass

                    if (
                        isinstance(table_type, str)
                        and table_type.startswith("metadata.")
                        and table_type != "metadata.isbn"
                        and first_cmd
                        not in (
                            "metadata",
                            "tag",
                            ".pipe",
                            ".mpv",
                        )
                    ):
                        debug(
                            "Auto-inserting metadata -get after metadata selection"
                        )
                        stages.insert(0, ["metadata", "-get"])
                    elif auto_stage:
                        first_cmd_norm = _norm_cmd_name(
                            stages[0][0] if stages and stages[0] else None
                        )
                        auto_cmd_norm = _norm_cmd_name(auto_stage[0])
                        dest_path = PipelineExecutor._stage_dest_path(stages[0])
                        already_has_download = bool(
                            inserted_provider_download
                            or any(
                                PipelineExecutor._stage_file_action(stage) == "download-file"
                                for stage in stages
                            )
                        )
                        skip_download_auto = bool(
                            auto_cmd_norm in {"download-file", "file"}
                            and (
                                already_has_download
                                or dest_path
                                or PipelineExecutor._selection_has_local_files(
                                    selection_indices, items_list
                                )
                            )
                        )
                        if (not skip_download_auto) and first_cmd_norm not in (
                            auto_cmd_norm,
                            ".pipe",
                            ".mpv",
                        ):
                            debug(
                                f"Auto-inserting {auto_cmd_norm} after selection"
                            )
                            stages.insert(0, list(auto_stage))
                            debug(
                                f"Inserted auto stage before existing pipeline: {stages[0]}"
                            )

                            if selection_indices:
                                try:
                                    if not _apply_row_action_to_stage(0):
                                        if len(selection_indices) == 1:
                                            idx = selection_indices[0]
                                            row_args = _get_row_args(
                                                idx, items_list
                                            )
                                            if row_args:
                                                inserted = stages[0]
                                                if inserted:
                                                    cmd = inserted[0]
                                                    tail = [
                                                        str(x)
                                                        for x in inserted[1:]
                                                    ]
                                                    stages[0] = (
                                                        [cmd] + row_args + tail
                                                    )
                                except Exception:
                                    logger.exception(
                                        "Failed to attach selection args to inserted auto stage (alternate branch)"
                                    )

                if (not stages) and selection_indices and len(selection_indices) == 1:
                    try:
                        selected_item = filtered[0] if filtered else None
                        if selected_item is not None and not isinstance(
                            selected_item, dict
                        ):
                            to_dict = getattr(selected_item, "to_dict", None)
                            if callable(to_dict):
                                selected_item = to_dict()
                        if isinstance(selected_item, dict):
                            from SYS.rich_display import (
                                render_item_details_panel,
                            )

                            render_item_details_panel(selected_item)
                            try:
                                ctx.set_last_result_items_only([selected_item])
                            except Exception:
                                pass
                    except Exception:
                        logger.exception(
                            "Failed to render selection-only item details"
                        )
                return True, piped_result
            else:
                debug("@N: No items to select from (items_list empty)")
                log("No previous results to select from", file=sys.stderr)
                return False, None

        return True, None

    # -------------------------------------------------------------------
    # Live progress
    # -------------------------------------------------------------------

    @staticmethod
    def _maybe_start_live_progress(
        config: Any, stages: List[List[str]]
    ) -> tuple[Any, Dict[int, int]]:
        progress_ui = None
        pipe_index_by_stage: Dict[int, int] = {}

        try:
            quiet_mode = (
                bool(config.get("_quiet_background_output"))
                if isinstance(config, dict)
                else False
            )
        except Exception:
            quiet_mode = False

        try:
            from SYS.rich_display import terminal_supports_live_progress

            if (not quiet_mode) and terminal_supports_live_progress():
                from SYS.models import PipelineLiveProgress

                pipe_stage_indices: List[int] = []
                pipe_labels: List[str] = []
                for idx, stage_tokens in enumerate(stages):
                    if not stage_tokens:
                        continue
                    name = str(stage_tokens[0]).replace("_", "-").lower()
                    if name == "@" or name.startswith("@"):
                        continue

                    if (
                        PipelineExecutor._stage_file_action(stage_tokens)
                        == "add-file"
                        or name in {"add_file"}
                    ):
                        try:
                            from pathlib import Path as _Path

                            toks = list(stage_tokens[1:])
                            i = 0
                            while i < len(toks):
                                t = str(toks[i])
                                low = t.lower().strip()
                                if low in {"-path", "--path", "-p"} and i + 1 < len(
                                    toks
                                ):
                                    nxt = str(toks[i + 1])
                                    if nxt and ("," not in nxt):
                                        p = _Path(nxt)
                                        if p.exists() and p.is_dir():
                                            name = ""
                                            break
                                    i += 2
                                    continue
                                i += 1
                        except Exception:
                            logger.exception(
                                "Failed to inspect add-file stage tokens for potential directory; skipping Live progress"
                            )
                        if not name:
                            continue
                    if name in {
                        "get-relationship",
                        "get-rel",
                        "get-metadata",
                        ".pipe",
                        ".mpv",
                        ".matrix",
                        ".help",
                        "help",
                        "?",
                        ".config",
                        ".status",
                        ".table",
                        ".worker",
                        ".adjective",
                    }:
                        continue
                    if (
                        PipelineExecutor._stage_file_action(stage_tokens)
                        == "delete-file"
                        or name in {"del-file"}
                    ):
                        continue
                    pipe_stage_indices.append(idx)
                    pipe_labels.append(
                        PipelineExecutor._stage_progress_label(stage_tokens) or name
                    )

                if pipe_labels:
                    progress_ui = PipelineLiveProgress(pipe_labels, enabled=True)
                    progress_ui.start()
                    try:
                        from SYS import pipeline as _pipeline_ctx

                        if hasattr(_pipeline_ctx, "set_live_progress"):
                            _pipeline_ctx.set_live_progress(progress_ui)
                        if hasattr(_pipeline_ctx, "get_progress_event_callback"):
                            progress_cb = _pipeline_ctx.get_progress_event_callback()
                            if callable(progress_cb) and hasattr(
                                progress_ui, "set_event_callback"
                            ):
                                progress_ui.set_event_callback(progress_cb)
                    except Exception:
                        logger.exception(
                            "Failed to register PipelineLiveProgress with pipeline context"
                        )
                    pipe_index_by_stage = {
                        stage_idx: pipe_idx
                        for pipe_idx, stage_idx in enumerate(pipe_stage_indices)
                    }
        except Exception:
            progress_ui = None
            pipe_index_by_stage = {}

        return progress_ui, pipe_index_by_stage

    # -------------------------------------------------------------------
    # Main execution entry point
    # -------------------------------------------------------------------

    def execute_tokens(self, tokens: List[str]) -> None:
        from cmdlet import REGISTRY

        from SYS import pipeline_state as ctx

        worker_manager = None
        pipeline_text = " ".join(str(token) for token in tokens)
        pipeline_session = None
        pipeline_status = "completed"
        pipeline_error = ""
        progress_ui = None
        pipe_index_by_stage: Dict[int, int] = {}

        try:
            try:
                from SYS.logger import debug_panel

                debug_panel(
                    "Pipeline execution",
                    [
                        ("command", " ".join(str(tok) for tok in tokens)),
                        ("token_count", len(tokens)),
                    ],
                )
            except Exception:
                debug(f"execute_tokens: tokens={tokens}")
            self._try_clear_pipeline_stop(ctx)

            try:
                if hasattr(ctx, "set_current_stage_table"):
                    ctx.set_current_stage_table(None)
            except Exception:
                logger.exception(
                    "Failed to clear current_stage_table in execute_tokens"
                )

            try:
                ctx.store_value("preflight", {})
            except Exception:
                logger.exception(
                    "Failed to set preflight cache in execute_tokens"
                )

            stages = self._split_stages(tokens)
            if not stages:
                log("Invalid pipeline syntax", file=sys.stderr)
                return
            self._maybe_seed_current_stage_table(ctx)
            stages = self._maybe_apply_pending_pipeline_tail(ctx, stages)
            config = self._load_config()
            config = self._apply_quiet_background_flag(config)

            (
                stages,
                first_stage_selection_indices,
                first_stage_had_extra_args,
                first_stage_select_all,
            ) = self._extract_first_stage_selection_tokens(stages)
            first_stage_selection_indices = self._apply_select_all_if_requested(
                ctx, first_stage_selection_indices, first_stage_select_all
            )

            preflight_error = self._preflight_pipeline_stages(stages)
            if preflight_error:
                log(preflight_error, file=sys.stderr)
                pipeline_status = "failed"
                pipeline_error = preflight_error
                return

            if not self._validate_download_file_relationship_order(stages):
                pipeline_status = "failed"
                pipeline_error = "Invalid pipeline order"
                return

            piped_result: Any = None
            worker_manager = _worker().WorkerManagerRegistry.ensure(config)
            pipeline_text = " | ".join(" ".join(stage) for stage in stages)
            pipeline_session = _worker().WorkerStages.begin_pipeline(
                worker_manager,
                pipeline_text=pipeline_text,
                config=config,
            )
            if pipeline_session and worker_manager:
                self._log_pipeline_event(
                    worker_manager,
                    pipeline_session.worker_id,
                    f"Pipeline start: {pipeline_text or '(empty pipeline)'}",
                )
            self._maybe_enable_background_notifier(
                worker_manager, config, pipeline_session
            )

            ok, initial_piped = self._maybe_apply_initial_selection(
                ctx,
                config,
                stages,
                selection_indices=first_stage_selection_indices,
                first_stage_had_extra_args=first_stage_had_extra_args,
                worker_manager=worker_manager,
                pipeline_session=pipeline_session,
            )
            if not ok:
                return
            if initial_piped is not None:
                piped_result = initial_piped

            try:
                from SYS.instance_chooser import (
                    maybe_publish_pipeline_instance_chooser,
                    store_pipeline_target_instances,
                )

                if maybe_publish_pipeline_instance_chooser(stages, config):
                    pipeline_status = "paused_selection"
                    return
                store_pipeline_target_instances(stages)
            except Exception:
                logger.exception("Failed pipeline instance chooser before stages")

            progress_ui, pipe_index_by_stage = self._maybe_start_live_progress(
                config, stages
            )

            for stage_index, stage_tokens in enumerate(stages):
                if not stage_tokens:
                    continue

                raw_stage_name = str(stage_tokens[0])
                cmd_name = raw_stage_name.replace("_", "-").lower()
                stage_args = stage_tokens[1:]

                if cmd_name == "@":
                    try:
                        next_cmd = None
                        if stage_index + 1 < len(stages) and stages[
                            stage_index + 1
                        ]:
                            next_cmd = (
                                str(stages[stage_index + 1][0])
                                .replace("_", "-")
                                .strip()
                                .lower()
                            )

                        current_table = None
                        try:
                            current_table = (
                                ctx.get_current_stage_table()
                                or ctx.get_last_result_table()
                            )
                        except Exception:
                            current_table = None

                        source_cmd = (
                            str(
                                getattr(current_table, "source_command", "") or ""
                            )
                            .replace("_", "-")
                            .strip()
                            .lower()
                        )
                        is_get_tag_table = source_cmd == "metadata"

                        if is_get_tag_table and next_cmd == "metadata":
                            subject = ctx.get_last_result_subject()
                            if subject is not None:
                                next_args = [
                                    str(x).replace("_", "-").strip().lower()
                                    for x in (
                                        stages[stage_index + 1][1:]
                                        if stage_index + 1 < len(stages)
                                        else []
                                    )
                                ]
                                if (
                                    "-add" not in next_args
                                    and "--add" not in next_args
                                ):
                                    pass
                                else:
                                    piped_result = subject
                                    try:
                                        subject_items = (
                                            subject
                                            if isinstance(subject, list)
                                            else [subject]
                                        )
                                        ctx.set_last_items(subject_items)
                                    except Exception:
                                        logger.exception(
                                            "Failed to set last_items from tag subject during @ handling"
                                        )
                                    if pipeline_session and worker_manager:
                                        try:
                                            worker_manager.log_step(
                                                pipeline_session.worker_id,
                                                "@ used metadata table subject for metadata -add",
                                            )
                                        except Exception:
                                            logger.exception(
                                                "Failed to record pipeline log step for '@ used metadata table subject for metadata -add' (pipeline_session=%r)",
                                                getattr(
                                                    pipeline_session,
                                                    "worker_id",
                                                    None,
                                                ),
                                            )
                                    continue
                    except Exception:
                        logger.exception(
                            "Failed to evaluate tag @ subject special-case"
                        )

                    last_items = None
                    try:
                        last_items = ctx.get_last_result_items()
                    except Exception:
                        last_items = None

                    if last_items:
                        from SYS.pipe_object import coerce_to_pipe_object

                        try:
                            pipe_items = [
                                coerce_to_pipe_object(x)
                                for x in list(last_items)
                            ]
                        except Exception:
                            pipe_items = list(last_items)
                        piped_result = (
                            pipe_items
                            if len(pipe_items) > 1
                            else pipe_items[0]
                        )
                        try:
                            ctx.set_last_items(pipe_items)
                        except Exception:
                            logger.exception(
                                "Failed to set last items after @ selection"
                            )
                        if pipeline_session and worker_manager:
                            try:
                                worker_manager.log_step(
                                    pipeline_session.worker_id,
                                    "@ used last result items",
                                )
                            except Exception:
                                logger.exception(
                                    "Failed to record pipeline log step for '@ used last result items' (pipeline_session=%r)",
                                    getattr(
                                        pipeline_session, "worker_id", None
                                    ),
                                )
                        continue

                    subject = ctx.get_last_result_subject()
                    if subject is None:
                        log("No current result table. Run a search first, then @N.", file=sys.stderr)
                        pipeline_status = "failed"
                        pipeline_error = "No result items/subject for @"
                        return
                    piped_result = subject
                    try:
                        subject_items = (
                            subject
                            if isinstance(subject, list)
                            else [subject]
                        )
                        ctx.set_last_items(subject_items)
                    except Exception:
                        logger.exception(
                            "Failed to set last_items from subject during @ handling"
                        )
                    if pipeline_session and worker_manager:
                        try:
                            worker_manager.log_step(
                                pipeline_session.worker_id,
                                "@ used current table subject",
                            )
                        except Exception:
                            logger.exception(
                                "Failed to record pipeline log step for '@ used current table subject' (pipeline_session=%r)",
                                getattr(
                                    pipeline_session, "worker_id", None
                                ),
                            )
                    continue

                if cmd_name.startswith("@"):
                    selection_token = raw_stage_name
                    selection = _cli_parsing().SelectionSyntax.parse(
                        selection_token
                    )
                    filter_spec = _cli_parsing().SelectionFilterSyntax.parse(
                        selection_token
                    )
                    is_select_all = selection_token.strip() == "@*"
                    if (
                        selection is None
                        and filter_spec is None
                        and not is_select_all
                    ):
                        log(f"Invalid selection: {selection_token}", file=sys.stderr)
                        pipeline_status = "failed"
                        pipeline_error = f"Invalid selection {selection_token}"
                        return

                    selected_indices: List[int] = []
                    display_table = None
                    try:
                        display_table = (
                            ctx.get_display_table()
                            if hasattr(ctx, "get_display_table")
                            else None
                        )
                    except Exception:
                        display_table = None

                    stage_table = ctx.get_current_stage_table()
                    try:
                        if display_table is not None and hasattr(
                            ctx, "set_current_stage_table"
                        ):
                            ctx.set_current_stage_table(display_table)
                            stage_table = display_table
                    except Exception:
                        logger.exception(
                            "Failed to set current_stage_table from display table during selection processing"
                        )

                    if not stage_table and display_table is not None:
                        stage_table = display_table
                    if not stage_table:
                        stage_table = ctx.get_last_result_table()

                    try:
                        if hasattr(ctx, "debug_table_state"):
                            ctx.debug_table_state(
                                f"selection {selection_token}"
                            )
                    except Exception:
                        logger.exception(
                            "Failed to debug_table_state during selection %s",
                            selection_token,
                        )

                    if display_table is not None and stage_table is display_table:
                        items_list = ctx.get_last_result_items() or []
                    else:
                        if hasattr(ctx, "get_last_selectable_result_items"):
                            items_list = (
                                ctx.get_last_selectable_result_items() or []
                            )
                        else:
                            items_list = ctx.get_last_result_items() or []

                    if is_select_all:
                        selected_indices = list(range(len(items_list)))
                    elif filter_spec is not None:
                        selected_indices = [
                            i
                            for i, item in enumerate(items_list)
                            if _cli_parsing().SelectionFilterSyntax.matches(
                                item, filter_spec
                            )
                        ]
                    else:
                        selected_indices = [
                            i - 1 for i in (selection or [])
                        ]

                    resolved_items = items_list if items_list else []
                    filtered = [
                        resolved_items[i]
                        for i in selected_indices
                        if 0 <= i < len(resolved_items)
                    ]
                    _emit_selection_debug_panel(
                        selection_token=selection_token,
                        selection_indices=selected_indices,
                        item_count=len(items_list or []),
                        filtered_count=len(filtered),
                        stage_table_present=(stage_table is not None),
                        display_table_present=(display_table is not None),
                        stage_is_last=(stage_index + 1 >= len(stages)),
                        downstream_stages=stages[stage_index + 1 :],
                        mode="selection",
                        source_table=stage_table or display_table,
                    )
                    try:
                        debug(
                            f"Selection {selection_token} -> resolved_indices={selected_indices} filtered_count={len(filtered)}"
                        )
                        if filtered:
                            sample = filtered[0]
                            if isinstance(sample, dict):
                                debug(
                                    f"Selection sample: hash={sample.get('hash')} store={sample.get('store')} _selection_args={sample.get('_selection_args')} _selection_action={sample.get('_selection_action')}"
                                )
                            else:
                                try:
                                    debug(
                                        f"Selection sample object: plugin={getattr(sample, 'plugin', None)} store={getattr(sample, 'store', None)}"
                                    )
                                except Exception:
                                    logger.exception(
                                        "Failed to debug selection sample object"
                                    )
                    except Exception:
                        logger.exception(
                            "Failed to produce selection debug sample for token %s",
                            selection_token,
                        )

                    if not filtered:
                        total = len(items_list) if isinstance(items_list, list) else 0
                        log(
                            f"{selection_token} matched 0 of {total} row(s). "
                            f"{'No result table is loaded.' if total == 0 else f'Use @1..@{total}.'}",
                            file=sys.stderr,
                        )
                        pipeline_status = "failed"
                        pipeline_error = "Empty selection"
                        return

                    stage_is_last = stage_index + 1 >= len(stages)
                    if filter_spec is not None and stage_is_last:
                        try:
                            base_table = stage_table
                            if base_table is None:
                                base_table = ctx.get_last_result_table()

                            if base_table is not None and hasattr(
                                base_table, "copy_with_title"
                            ):
                                new_table = base_table.copy_with_title(
                                    getattr(base_table, "title", "")
                                    or "Results"
                                )
                            else:
                                new_table = _result_table()(
                                    getattr(base_table, "title", "")
                                    if base_table is not None
                                    else "Results"
                                )

                            try:
                                if base_table is not None and getattr(
                                    base_table, "table", None
                                ):
                                    new_table.set_table(
                                        str(getattr(base_table, "table"))
                                    )
                            except Exception:
                                logger.exception(
                                    "Failed to set table on new_table for filter overlay"
                                )

                            try:
                                safe = str(selection_token)[1:].strip()
                                new_table.set_header_line(
                                    f'filter: "{safe}"'
                                )
                            except Exception:
                                logger.exception(
                                    "Failed to set header line for filter overlay for token %s",
                                    selection_token,
                                )

                            for item in filtered:
                                new_table.add_result(item)

                            try:
                                ctx.set_last_result_table_overlay(
                                    new_table,
                                    items=list(filtered),
                                    subject=ctx.get_last_result_subject(),
                                )
                            except Exception:
                                logger.exception(
                                    "Failed to set last_result_table_overlay for filter selection"
                                )

                            try:
                                from SYS.rich_display import (
                                    stdout_console,
                                )

                                stdout_console().print()
                                stdout_console().print(new_table)
                            except Exception:
                                logger.exception(
                                    "Failed to render filter overlay to stdout_console"
                                )
                        except Exception:
                            logger.exception(
                                "Failed while rendering filter overlay for selection %s",
                                selection_token,
                            )
                        continue

                    current_table = (
                        ctx.get_current_stage_table()
                        or ctx.get_last_result_table()
                    )
                    if (not is_select_all) and (len(filtered) == 1):
                        try:
                            PipelineExecutor._maybe_open_url_selection(
                                current_table,
                                filtered,
                                stage_is_last=(
                                    stage_index + 1 >= len(stages)
                                ),
                            )
                        except Exception:
                            logger.exception(
                                "Failed to open URL selection for table %s",
                                getattr(current_table, "table", None),
                            )

                    if PipelineExecutor._maybe_run_class_selector(
                        ctx,
                        config,
                        filtered,
                        stage_is_last=(stage_index + 1 >= len(stages)),
                    ):
                        return

                    next_cmd: Optional[str] = None
                    next_args: List[str] = []
                    try:
                        if stage_index + 1 < len(stages) and stages[
                            stage_index + 1
                        ]:
                            next_cmd = (
                                str(stages[stage_index + 1][0])
                                .replace("_", "-")
                                .lower()
                            )
                            next_args = [
                                str(x).replace("_", "-").strip().lower()
                                for x in stages[stage_index + 1][1:]
                            ]
                    except Exception:
                        logger.exception(
                            "Failed to determine next_cmd during selection expansion for stage_index %s",
                            stage_index,
                        )
                        next_cmd = None
                        next_args = []

                    def _is_tag_row(obj: Any) -> bool:
                        try:
                            if (
                                hasattr(obj, "__class__")
                                and obj.__class__.__name__ == "TagItem"
                                and hasattr(obj, "tag_name")
                            ):
                                return True
                        except Exception:
                            logger.exception(
                                "Failed to inspect TagItem object while checking _is_tag_row"
                            )
                        try:
                            if isinstance(obj, dict) and obj.get("tag_name"):
                                return True
                        except Exception:
                            logger.exception(
                                "Failed to inspect dict tag_name while checking _is_tag_row"
                            )
                        return False

                    if (
                        next_cmd == "tag"
                        and ("-delete" in next_args or "--delete" in next_args)
                        and len(filtered) > 1
                        and all(_is_tag_row(x) for x in filtered)
                    ):
                        from SYS.field_access import get_field

                        tags: List[str] = []
                        first_hash = None
                        first_store = None
                        first_path = None
                        for item in filtered:
                            tag_name = get_field(item, "tag_name")
                            if tag_name:
                                tags.append(str(tag_name))
                            if first_hash is None:
                                first_hash = get_field(item, "hash")
                            if first_store is None:
                                first_store = get_field(item, "store")
                            if first_path is None:
                                first_path = get_field(
                                    item, "path"
                                ) or get_field(item, "target")

                        if tags:
                            grouped = {
                                "table": "tag.selection",
                                "media_kind": "tag",
                                "hash": first_hash,
                                "store": first_store,
                                "path": first_path,
                                "tag": tags,
                            }
                            piped_result = grouped
                            continue

                    from SYS.pipe_object import coerce_to_pipe_object

                    filtered_pipe_objs = [
                        coerce_to_pipe_object(item) for item in filtered
                    ]
                    piped_result = (
                        filtered_pipe_objs
                        if len(filtered_pipe_objs) > 1
                        else filtered_pipe_objs[0]
                    )

                    current_table = (
                        ctx.get_current_stage_table()
                        or ctx.get_last_result_table()
                    )
                    table_type = (
                        current_table.table
                        if current_table
                        and hasattr(current_table, "table")
                        else None
                    )

                    def _norm_stage_cmd(name: Any) -> str:
                        return (
                            str(name or "").replace("_", "-").strip().lower()
                        )

                    next_cmd = None
                    if stage_index + 1 < len(stages) and stages[
                        stage_index + 1
                    ]:
                        next_cmd = _norm_stage_cmd(
                            stages[stage_index + 1][0]
                        )

                    auto_stage = None
                    if isinstance(table_type, str) and table_type:
                        try:
                            from PluginCore.registry import (
                                selection_auto_stage_for_table,
                            )

                            at_end = bool(stage_index + 1 >= len(stages))
                            auto_stage = selection_auto_stage_for_table(
                                table_type,
                                stage_args if at_end else None,
                            )
                        except Exception:
                            auto_stage = None

                    if filter_spec is None:
                        if stage_index + 1 >= len(stages):
                            if auto_stage:
                                try:
                                    debug(
                                        f"Auto-running selection via {auto_stage[0]}"
                                    )
                                except Exception:
                                    logger.exception(
                                        "Failed to print auto-run selection message for %s",
                                        auto_stage[0],
                                    )
                                stages.append(list(auto_stage))
                        else:
                            if auto_stage:
                                auto_cmd = _norm_stage_cmd(auto_stage[0])
                                if next_cmd not in (
                                    auto_cmd,
                                    ".pipe",
                                    ".mpv",
                                ):
                                    debug(
                                        f"Auto-inserting {auto_cmd} after selection"
                                    )
                                    stages.insert(
                                        stage_index + 1, list(auto_stage)
                                    )
                    continue

                cmd_fn = REGISTRY.get(cmd_name)
                try:
                    mod = import_cmd_module(cmd_name, reload_loaded=True)
                    data = getattr(mod, "CMDLET", None) if mod else None
                    if (
                        data
                        and hasattr(data, "exec")
                        and callable(getattr(data, "exec"))
                    ):
                        from SYS.cmdlet_spec import (
                            collect_registered_cmdlet_names,
                        )

                        run_fn = getattr(data, "exec")
                        for registered_name in collect_registered_cmdlet_names(
                            data, fallback_name=cmd_name
                        ):
                            REGISTRY[registered_name] = run_fn
                        cmd_fn = run_fn
                except Exception:
                    pass

                if not cmd_fn:
                    try:
                        mod = import_cmd_module(cmd_name)
                        data = getattr(mod, "CMDLET", None) if mod else None
                        if (
                            data
                            and hasattr(data, "exec")
                            and callable(getattr(data, "exec"))
                        ):
                            run_fn = getattr(data, "exec")
                            REGISTRY[cmd_name] = run_fn
                            cmd_fn = run_fn
                    except Exception:
                        cmd_fn = None

                if not cmd_fn:
                    log(f"Unknown command: {cmd_name}", file=sys.stderr)
                    pipeline_status = "failed"
                    pipeline_error = f"Unknown command: {cmd_name}"
                    return

                try:
                    from SYS.models import PipelineStageContext

                    pipe_idx = pipe_index_by_stage.get(stage_index)

                    output_table: Optional[Any] = None
                    pre_stage_table: Optional[Any] = None
                    pre_last_result_table: Optional[Any] = None
                    session = _worker().WorkerStages.begin_stage(
                        worker_manager,
                        cmd_name=cmd_name,
                        stage_tokens=stage_tokens,
                        config=config,
                        command_text=pipeline_text
                        if pipeline_text
                        else " ".join(stage_tokens),
                    )
                    try:
                        stage_ctx = PipelineStageContext(
                            stage_index=stage_index,
                            total_stages=len(stages),
                            pipe_index=pipe_idx,
                            worker_id=session.worker_id
                            if session
                            else None,
                            on_emit=(
                                lambda x: progress_ui.on_emit(pipe_idx, x)
                            )
                            if progress_ui is not None
                            and pipe_idx is not None
                            else None,
                        )

                        ctx.set_stage_context(stage_ctx)
                        ctx.set_current_cmdlet_name(cmd_name)
                        ctx.set_current_stage_text(" ".join(stage_tokens))
                        ctx.clear_emits()

                        if progress_ui is not None and pipe_idx is not None:
                            progress_ui.begin_pipe(pipe_idx, total_items=1)

                        try:
                            pre_stage_table = (
                                ctx.get_current_stage_table()
                                if hasattr(ctx, "get_current_stage_table")
                                else None
                            )
                        except Exception:
                            pre_stage_table = None
                        try:
                            pre_last_result_table = (
                                ctx.get_last_result_table()
                                if hasattr(ctx, "get_last_result_table")
                                else None
                            )
                        except Exception:
                            pre_last_result_table = None

                        ret_code = cmd_fn(
                            piped_result, stage_args, config
                        )
                        if ret_code is not None:
                            try:
                                normalized_ret = int(ret_code)
                            except Exception:
                                normalized_ret = 0
                            if normalized_ret != 0:
                                pipeline_status = "failed"
                                pipeline_error = f"Stage '{cmd_name}' failed with exit code {normalized_ret}"
                                return

                        if stage_index + 1 < len(stages):
                            try:
                                post_stage_table = (
                                    ctx.get_current_stage_table()
                                    if hasattr(ctx, "get_current_stage_table")
                                    else None
                                )
                            except Exception:
                                post_stage_table = None
                            if (
                                post_stage_table is not None
                                and post_stage_table is not pre_stage_table
                            ):
                                has_emits = bool(stage_ctx.emits)
                                if not has_emits:
                                    tail = stages[stage_index + 1 :]
                                    source = None
                                    try:
                                        raw_source = getattr(
                                            post_stage_table,
                                            "source_command",
                                            None,
                                        )
                                        if raw_source:
                                            source = (
                                                str(raw_source)
                                                .replace("_", "-")
                                                .strip()
                                                .lower()
                                            )
                                    except Exception:
                                        source = None
                                    try:
                                        ctx.set_pending_pipeline_tail(
                                            tail, source
                                        )
                                    except Exception:
                                        pass
                                    if post_stage_table is not None:
                                        try:
                                            from SYS.rich_display import (
                                                stdout_console,
                                            )

                                            stdout_console().print()
                                            stdout_console().print(
                                                post_stage_table
                                            )
                                        except Exception:
                                            pass
                                    logger.info(
                                        "Pipeline paused for selection — use @N to continue"
                                    )
                                    pipeline_status = "paused_selection"
                                    return

                        output_table = None
                        if stage_index + 1 >= len(stages):
                            try:
                                output_table = (
                                    ctx.get_display_table()
                                    if hasattr(ctx, "get_display_table")
                                    else None
                                )
                            except Exception:
                                output_table = None

                            if output_table is None:
                                current_stage_table = None
                                last_result_table = None
                                try:
                                    current_stage_table = (
                                        ctx.get_current_stage_table()
                                        if hasattr(
                                            ctx, "get_current_stage_table"
                                        )
                                        else None
                                    )
                                except Exception:
                                    current_stage_table = None
                                try:
                                    last_result_table = (
                                        ctx.get_last_result_table()
                                        if hasattr(
                                            ctx, "get_last_result_table"
                                        )
                                        else None
                                    )
                                except Exception:
                                    last_result_table = None

                                if (
                                    current_stage_table is not None
                                    and current_stage_table
                                    is not pre_stage_table
                                ):
                                    output_table = current_stage_table
                                elif (
                                    last_result_table is not None
                                    and last_result_table
                                    is not pre_last_result_table
                                ):
                                    output_table = last_result_table

                        stage_emits = list(stage_ctx.emits)
                        if stage_emits:
                            piped_result = (
                                stage_emits
                                if len(stage_emits) > 1
                                else stage_emits[0]
                            )
                        else:
                            piped_result = None
                    finally:
                        if progress_ui is not None and pipe_idx is not None:
                            try:
                                progress_ui.finish_pipe(pipe_idx)
                            except Exception:
                                logger.exception(
                                    "Failed to finish pipe in progress_ui"
                                )
                        if output_table is not None and not getattr(
                            output_table, "_rendered_by_cmdlet", False
                        ):
                            try:
                                from SYS.rich_display import (
                                    stdout_console,
                                )

                                stdout_console().print()
                                stdout_console().print(output_table)
                            except Exception:
                                logger.exception(
                                    "Failed to render output_table to stdout_console"
                                )
                        if session:
                            try:
                                session.close()
                            except Exception:
                                logger.exception(
                                    "Failed to close pipeline stage session"
                                )

                except Exception as exc:
                    pipeline_status = "failed"
                    pipeline_error = f"{cmd_name}: {exc}"
                    debug(
                        f"Error in stage {stage_index} ({cmd_name}): {exc}"
                    )
                    return
        except Exception as exc:
            pipeline_status = "failed"
            pipeline_error = f"{type(exc).__name__}: {exc}"
            log(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            if progress_ui is not None:
                try:
                    progress_ui.complete_all_pipes()
                except Exception:
                    logger.exception(
                        "Failed to complete all pipe UI tasks in progress_ui.complete_all_pipes"
                    )
                try:
                    progress_ui.stop()
                except Exception:
                    logger.exception("Failed to stop progress_ui")
            try:
                from SYS import pipeline as _pipeline_ctx

                if hasattr(_pipeline_ctx, "set_live_progress"):
                    _pipeline_ctx.set_live_progress(None)
            except Exception:
                logger.exception(
                    "Failed to clear live_progress on pipeline context"
                )
            try:
                if pipeline_session and worker_manager:
                    pipeline_session.close(
                        status=pipeline_status, error_msg=pipeline_error
                    )
            except Exception:
                logger.exception(
                    "Failed to close pipeline session during finalization"
                )
            try:
                if pipeline_session and worker_manager:
                    self._log_pipeline_event(
                        worker_manager,
                        pipeline_session.worker_id,
                        f"Pipeline {pipeline_status}: {pipeline_error or ''}",
                    )
            except Exception:
                logger.exception(
                    "Failed to log final pipeline status (pipeline_session=%r)",
                    getattr(pipeline_session, "worker_id", None),
                )
            try:
                from SYS.pipeline_state import set_last_execution_result

                set_last_execution_result(
                    status=pipeline_status,
                    error=pipeline_error,
                    command_text=pipeline_text,
                )
            except Exception:
                logger.exception(
                    "Failed to record last execution result for pipeline"
                )


__all__ = ["PipelineExecutor"]
