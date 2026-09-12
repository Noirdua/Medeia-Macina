"""Shared utilities for cmdlets — re-export module.

This module re-exports all utilities from the focused sub-modules to maintain
backward compatibility with existing imports. New code should import directly
from the appropriate sub-module, but existing ``from cmdlet._shared import ...``
imports continue to work unchanged.

Sub-modules:
- ``_tag_utils`` — tag parsing, normalization, template rendering, extraction
- ``_pipeobject_utils`` — PipeObject creation, coercion, metadata propagation
- ``_url_utils`` — URL preflight checks, merging, removal
- ``_template_utils`` — pipeline preview and template helpers
- ``_preflight`` — store backend resolution and validation
- ``_display`` — Rich output, saved-panel rendering, result persistence
- ``_store_utils`` — hash resolution, store batch dispatching
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from contextlib import AbstractContextManager, nullcontext

from SYS.logger import log, debug, debug_panel
from SYS import models
from SYS import pipeline as pipeline_context
from SYS.payload_builders import build_file_result_payload, build_table_result_payload
from SYS.result_publication import publish_result_table
from SYS.result_table import Table
from SYS.rich_display import stderr_console as get_stderr_console
from SYS.cmdlet_spec import Cmdlet, CmdletArg, QueryArg, SharedArgs, parse_cmdlet_args
from rich.prompt import Confirm

from ._tag_utils import (
    set_tag_groups_path,
    _normalize_hash_cached,
    normalize_hash,
    looks_like_hash,
    parse_tag_arguments,
    render_tag_value_templates,
    build_tag_value_lookup,
    _add_tag_values_to_lookup,
    expand_tag_groups,
    first_title_tag,
    apply_preferred_title,
    collapse_namespace_tags,
    collect_relationship_labels,
    extract_tag_from_result,
    extract_title_from_result,
    extract_url_from_result,
    TAG_GROUPS_PATH,
)

from ._pipeobject_utils import (
    get_field,
    merge_sequences,
    normalize_result_input,
    normalize_result_items,
    value_has_content,
    filter_results_by_temp,
    create_pipe_object_result,
    mark_as_temp,
    set_parent_hash,
    get_pipe_object_path,
    get_pipe_object_hash,
    coerce_to_pipe_object,
    resolve_item_store_hash,
    propagate_metadata,
    extract_relationships,
    extract_duration,
    pipeline_item_local_path,
)

from ._url_utils import (
    check_url_exists_in_storage,
    merge_urls,
    remove_urls,
    set_item_urls,
)

from ._template_utils import (
    build_pipeline_preview,
)

from ._preflight import (
    get_store_backend,
    get_preferred_store_backend,
)

from ._display import (
    _print_live_safe_stderr,
    _print_saved_output_panel,
    _apply_saved_path_update,
    _unique_destination_path,
    _extract_flag_value,
    apply_output_path_from_pipeobjects,
    display_and_persist_items,
)

from ._store_utils import (
    resolve_hash_for_cmdlet,
    parse_hash_query,
    parse_single_hash_query,
    require_hash_query,
    require_single_hash_query,
    get_hash_for_operation,
    coalesce_hash_value_pairs,
    run_store_hash_value_batches,
    run_store_note_batches,
    collect_store_hash_value_batch,
)


def resolve_target_dir(
    parsed: Dict[str, Any],
    config: Dict[str, Any],
    *,
    handle_creations: bool = True,
) -> Optional[Path]:
    """Resolve a target directory from -path, -storage, or config fallback.

    Args:
        parsed: Parsed cmdlet arguments dict.
        config: System configuration dict.
        handle_creations: Whether to create the directory if it doesn't exist.

    Returns:
        Path to the resolved directory, or None if invalid.
    """
    from SYS.utils import expand_path

    target = parsed.get("path")
    if target:
        try:
            from SYS.config import resolve_path_alias

            raw_target = str(target or "").strip()
            aliased = resolve_path_alias(config, raw_target)
            if raw_target.startswith("$") and aliased is None:
                from SYS.logger import log
                log(
                    f"Unknown path alias {raw_target}. Set it with .config path_aliases.<name> <path>",
                    file=sys.stderr,
                )
                return None

            p = aliased if aliased is not None else expand_path(raw_target)
            if handle_creations:
                p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception as e:
            from SYS.logger import log
            log(f"Cannot use target path {target}: {e}", file=sys.stderr)
            return None

    storage_val = parsed.get("storage")
    if storage_val:
        try:
            return SharedArgs.resolve_storage(storage_val)
        except Exception as e:
            from SYS.logger import log
            log(f"Invalid storage location: {e}", file=sys.stderr)
            return None

    try:
        from SYS.config import resolve_output_dir
        out_dir = resolve_output_dir(config)
        if handle_creations:
            out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir
    except Exception:
        from SYS.utils import default_staging_dir
        p = default_staging_dir()
        if handle_creations:
            p.mkdir(parents=True, exist_ok=True)
        return p


def coerce_to_path(value: Any) -> Path:
    """Extract a Path from common provider result shapes (Path, str, dict, object)."""
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        return Path(value)

    p = getattr(value, "path", None)
    if p:
        return Path(str(p))

    if isinstance(value, dict):
        p = value.get("path")
        if p:
            return Path(str(p))

    raise ValueError(f"Cannot coerce {type(value).__name__} to Path (missing 'path' field)")


def resolve_media_kind_by_extension(path: Path) -> str:
    """Resolve media kind (audio, video, image, document, other) from file extension."""
    if not isinstance(path, Path):
        try:
            path = Path(str(path))
        except Exception:
            return "other"

    suffix = path.suffix.lower()
    if suffix in {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".mka"}:
        return "audio"
    if suffix in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".mpg", ".mpeg", ".ts", ".m4v", ".wmv"}:
        return "video"
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}:
        return "image"
    if suffix in {".pdf", ".epub", ".txt", ".mobi", ".azw3", ".cbz", ".cbr", ".doc", ".docx"}:
        return "document"
    return "other"


def should_show_help(args: Sequence[str]) -> bool:
    """Check if help flag was passed in arguments.

    Consolidates repeated pattern of checking for help flags across cmdlet.

    Args:
            args: Command arguments to check

    Returns:
            True if any help flag is present (-?, /?, --help, -h, help, --cmdlet)

    Examples:
            if should_show_help(args):
                    log(json.dumps(CMDLET, ensure_ascii=False, indent=2))
                    return 0
    """
    try:
        return any(
            str(a).lower() in {"-?", "/?", "--help", "-h", "help", "--cmdlet"} for a in args
        )
    except Exception:
        return False
