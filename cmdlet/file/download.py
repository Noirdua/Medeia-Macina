"""download-file cmdlet — re-export module.

This module re-exports the public API from the split sub-modules to maintain
backward compatibility with existing imports.
"""

from .download_core import Download_File, CMDLET

# Re-export sub-module symbols for direct import if needed
from .download_fetch import (
    _emit_plugin_items,
    _consume_plugin_download_result,
    _process_explicit_urls,
    _expand_provider_items,
    _process_provider_items,
    _download_provider_items,
    _emit_local_file,
    _resolve_provider_preflight_items,
    _build_provider_playlist_item_selector,
    _format_timecode,
    _rebase_subtitle_timestamp_text,
    _format_clip_range,
    _apply_clip_decorations,
    _parse_clip_spec_to_ranges,
)

from .download_storage import (
    _iter_storage_export_refs,
    _export_store_file,
    _process_storage_items,
    _process_explicit_local_sources,
    _extract_hash_from_search_hit,
    _iter_duplicate_tag_values,
    _extract_duplicate_namespace_tags,
    _extract_duplicate_title_tag,
    _extract_duplicate_title,
    _has_duplicate_title,
    _build_duplicate_display_row,
    _fetch_duplicate_metadata_for_hash,
    _enrich_duplicate_metadata,
    _fetch_duplicate_metadata_for_hashes,
    _collect_existing_url_match_refs_for_url,
    _find_existing_url_matches_for_url,
    _find_existing_hash_for_url,
    _find_existing_hashes_for_url,
    _init_storage,
    _supports_storage_duplicate_lookup,
    _filter_supported_urls,
    _canonicalize_url_for_storage,
    _preflight_url_duplicate,
    _preflight_explicit_url_duplicates,
    _download_supported_urls,
    _maybe_show_playlist_table,
    _maybe_show_format_table_for_single_url,
    _run_streaming_urls,
)
