"""add-file cmdlet — re-export module.

This module re-exports the public API from the split sub-modules to maintain
backward compatibility with existing imports.
"""

from .add_core import (
    Add_File,
    CMDLET,
    _CommandDependencies,
    SUPPORTED_MEDIA_EXTENSIONS,
    _REMOTE_URL_PREFIXES,
    _SCREENSHOT_TIME_SUFFIX_RE,
)
from .add_tagging import _maybe_apply_florencevision_tags

# Re-export sub-module functions for direct import if needed
from .add_validation import (
    _resolve_source,
    _validate_source,
    _scan_directory_for_files,
    _is_probable_url,
    _resolve_backend_by_name,
    _download_piped_source,
    _maybe_download_plugin_result,
    _build_plugin_filename,
    _maybe_download_backend_file,
    _download_remote_backend_url,
)

from .add_tagging import (
    _prepare_metadata,
    _resolve_file_hash,
    _load_sidecar_bundle,
    _get_url,
    _get_relationships,
    _get_duration,
    _get_note_text,
    _parse_relationship_tag_king_alts,
    _parse_relationships_king_alts,
    _normalize_hash_candidate,
    _resolve_media_kind,
)

from .add_storage import (
    _handle_storage_backend,
    _handle_plugin_upload,
    _emit_plugin_upload_payload,
    _emit_storage_result,
    _emit_pipe_object,
    _update_pipe_object_destination,
    _find_existing_hash_by_urls,
    _try_emit_search_file_by_hash,
    _try_emit_search_file_by_hashes,
    _apply_pending_url_associations,
    _apply_pending_tag_associations,
    _apply_pending_relationships,
    _cleanup_after_success,
    _cleanup_sidecar_files,
    _copy_sidecars,
    _persist_local_metadata,
    _stop_live_progress_for_terminal_render,
)
