from __future__ import annotations

from typing import Any, Dict, Sequence, Optional, List
import json
import sys

from SYS.item_accessors import get_extension_field, get_int_field
from SYS.logger import log
from SYS.payload_builders import build_file_result_payload

from . import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
SharedArgs = sh.SharedArgs
parse_cmdlet_args = sh.parse_cmdlet_args
get_field = sh.get_field
from SYS import pipeline as ctx
from SYS.result_table import Table
from SYS.result_table_helpers import add_row_columns


class Get_Metadata(Cmdlet):
    """Class-based get-metadata cmdlet with self-registration."""

    def __init__(self) -> None:
        """Initialize get-metadata cmdlet."""
        super().__init__(
            name="get-metadata",
            summary="Print metadata for files by hash and storage backend.",
            usage='get-metadata [-query "hash:<sha256>"] [-instance <backend>]',
            alias=[],
            arg=[
                SharedArgs.QUERY,
                SharedArgs.INSTANCE,
            ],
            detail=[
                "- Retrieves metadata from storage backend using file hash as identifier.",
                "- Shows hash, MIME type, size, duration/pages, known url, and import timestamp.",
                "- Hash and store are taken from piped result or can be overridden with -query/-instance flags.",
                "- All metadata is retrieved from the storage backend's database (single source of truth).",
            ],
            exec=self.run,
        )
        self.register()

    @staticmethod
    def _extract_imported_ts(meta: Dict[str, Any]) -> Optional[int]:
        """Extract an imported timestamp from metadata if available.
        
        Attempts to parse imported timestamp from metadata dict in multiple formats:
        - Numeric Unix timestamp (int/float)
        - ISO format string (e.g., "2024-01-15T10:30:00")
        
        Args:
            meta: Metadata dictionary from backend (e.g., from get_metadata())
            
        Returns:
            Unix timestamp as integer if found, None otherwise
        """
        if not isinstance(meta, dict):
            return None

        # Prefer explicit time_imported if present
        explicit = meta.get("time_imported")
        if isinstance(explicit, (int, float)):
            return int(explicit)

        # Try parsing string timestamps
        if isinstance(explicit, str):
            try:
                import datetime as _dt

                return int(_dt.datetime.fromisoformat(explicit).timestamp())
            except Exception:
                pass

        return None

    @staticmethod
    def _format_imported(ts: Optional[int]) -> str:
        """Format Unix timestamp as human-readable date string (UTC).
        
        Converts Unix timestamp to YYYY-MM-DD HH:MM:SS format.
        Used for displaying file import dates to users.
        
        Args:
            ts: Unix timestamp (integer) or None
            
        Returns:
            Formatted date string (e.g., "2024-01-15 10:30:00") or empty string if invalid
        """
        if not ts:
            return ""
        try:
            import datetime as _dt

            return _dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""

    @staticmethod
    def _build_table_row(
        title: str,
        store: str,
        path: str,
        mime: str,
        size_bytes: Optional[int],
        dur_seconds: Optional[int],
        imported_ts: Optional[int],
        url: list[str],
        hash_value: Optional[str],
        pages: Optional[int] = None,
        tag: Optional[List[str]] = None,
        ext: Optional[str] = None,
    ) -> Dict[str,
              Any]:
        """Build a normalized metadata row dict for display and piping.
        
        Converts raw metadata fields into a standardized row format suitable for:
        - Display in result tables
        - Piping to downstream cmdlets
        - JSON serialization
        
        Args:
            title: File or resource title
            instance: Backend store name (e.g., "hydrus", "local")
            path: File path or resource identifier
            mime: MIME type (e.g., "image/jpeg", "video/mp4")
            size_bytes: File size in bytes
            dur_seconds: Duration in seconds (for video/audio)
            imported_ts: Unix timestamp when item was imported
            url: List of known URLs associated with file
            hash_value: File hash (SHA256 or other)
            pages: Number of pages (for PDFs)
            tag: List of tags applied to file
            ext: File extension (e.g., "jpg", "mp4")
            
        Returns:
            Dictionary with normalized metadata fields and display columns
        """
        size_mb = None
        size_int: Optional[int] = None
        if size_bytes is not None:
            try:
                size_int = int(size_bytes)
            except Exception:
                size_int = None
        if isinstance(size_int, int):
            try:
                size_mb = int(size_int / (1024 * 1024))
            except Exception:
                size_mb = None

        dur_int = int(dur_seconds) if isinstance(dur_seconds, (int, float)) else None
        pages_int = int(pages) if isinstance(pages, (int, float)) else None
        imported_label = Get_Metadata._format_imported(imported_ts)

        duration_label = "Duration(s)"
        duration_value = str(dur_int) if dur_int is not None else ""
        if mime and mime.lower().startswith("application/pdf"):
            duration_label = "Pages"
            duration_value = str(pages_int) if pages_int is not None else ""

        columns = [
            ("Title",
             title or ""),
            ("Hash",
             hash_value or ""),
            ("MIME",
             mime or ""),
            ("Size(MB)",
             str(size_mb) if size_mb is not None else ""),
            (duration_label,
             duration_value),
            ("Imported",
             imported_label),
            ("Instance",
             store or ""),
        ]

        payload = build_file_result_payload(
            title=title,
            fallback_title=path,
            path=path,
            url=url,
            hash_value=hash_value,
            store=store,
            tag=tag or [],
            ext=ext,
            size_bytes=size_int,
            columns=columns,
        )
        payload.update(
            {
                "mime": mime,
                "duration_seconds": dur_int,
                "pages": pages_int,
                "imported_ts": imported_ts,
                "imported": imported_label,
                "instance": store,
            }
        )
        return payload

    @staticmethod
    def _add_table_body_row(table: Table, row: Dict[str, Any]) -> None:
        """Add a single metadata row to the result table.
        
        Extracts column values from row dict and adds to result table using
        standard column ordering (Hash, MIME, Size, Duration/Pages).
        
        Args:
            table: Result table to add row to
            row: Metadata row dict (from _build_table_row)
        """
        columns = row.get("columns") if isinstance(row, dict) else None
        lookup: Dict[str,
                     Any] = {}
        if isinstance(columns, list):
            for col in columns:
                if isinstance(col, tuple) and len(col) == 2:
                    label, value = col
                    lookup[str(label)] = value

        columns_to_add = [
            ("Hash", lookup.get("Hash", "")),
            ("MIME", lookup.get("MIME", "")),
            ("Size(MB)", lookup.get("Size(MB)", "")),
        ]
        if "Duration(s)" in lookup:
            columns_to_add.append(("Duration(s)", lookup.get("Duration(s)", "")))
        elif "Pages" in lookup:
            columns_to_add.append(("Pages", lookup.get("Pages", "")))
        else:
            columns_to_add.append(("Duration(s)", ""))
        add_row_columns(table, columns_to_add)

    @staticmethod
    def _extract_metadata_tags(metadata: Dict[str, Any]) -> List[str]:
        tags: List[str] = []

        def _append(tag_value: Any) -> None:
            text = str(tag_value or "").strip()
            if text and text not in tags:
                tags.append(text)

        def _walk_tag_values(value: Any) -> None:
            if isinstance(value, str):
                _append(value)
                return
            if isinstance(value, dict):
                for nested_value in value.values():
                    _walk_tag_values(nested_value)
                return
            if isinstance(value, (list, tuple, set)):
                for nested_value in value:
                    _walk_tag_values(nested_value)

        raw_tags = metadata.get("tags")
        if isinstance(raw_tags, dict):
            for service_data in raw_tags.values():
                if not isinstance(service_data, dict):
                    continue
                matched_tag_key = False
                for key, tag_mapping in service_data.items():
                    if "tag" not in str(key).strip().lower():
                        continue
                    matched_tag_key = True
                    _walk_tag_values(tag_mapping)
                if not matched_tag_key:
                    _walk_tag_values(service_data)
        elif isinstance(raw_tags, list):
            for tag_value in raw_tags:
                _append(tag_value)

        for key in ("tags_flat", "tag"):
            raw_value = metadata.get(key)
            _walk_tag_values(raw_value)

        return tags

    def run(self, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        """Execute get-metadata cmdlet - retrieve and display file metadata.
        
        Queries a storage backend (Hydrus, local, etc.) for file metadata using hash.
        Extracts tags embedded in metadata response (avoiding duplicate API calls).
        Displays metadata in rich detail panel and result table.
        Allows piping (@N) to other cmdlets for chaining operations.
        
        Optimizations:
        - Extracts tags from metadata response (no separate get_tag() call)
        - Single HTTP request to backends per file
        
        Args:
            result: Piped input (dict with optional hash/store/title/tag fields)
            args: Command line arguments ([-query "hash:..."] [-instance backend])
            config: Application configuration dict
            
        Returns:
            0 on success, 1 on error (no metadata found, backend unavailable, etc.)
        """
        # Parse arguments
        parsed = parse_cmdlet_args(args, self)

        query_hash, query_valid = sh.require_single_hash_query(
            parsed.get("query"),
            'No hash available - use -query "hash:<sha256>"',
            log_file=sys.stderr,
        )
        if not query_valid:
            return 1

        # Get hash and store from parsed args or result
        file_hash = query_hash or get_field(result, "hash")
        storage_source = parsed.get("instance") or get_field(result, "store")

        if not file_hash:
            log('No hash available - use -query "hash:<sha256>"', file=sys.stderr)
            return 1

        if not storage_source:
            log("No storage backend specified - use -instance to specify", file=sys.stderr)
            return 1

        # Use storage backend to get metadata
        try:
            backend, _store_registry, _exc = sh.get_preferred_store_backend(
                config,
                storage_source,
                suppress_debug=True,
            )
            if backend is None:
                log(f"Storage backend '{storage_source}' not found", file=sys.stderr)
                return 1

            # Get metadata from backend
            metadata = backend.get_metadata(file_hash)

            if not metadata:
                log(
                    f"No metadata found for hash {file_hash[:8]}... in {storage_source}",
                    file=sys.stderr,
                )
                return 1

            # Extract title from tags if available
            title = get_field(result, "title") or file_hash[:16]

            # Get tags from input result
            item_tags = get_field(result, "tag") or get_field(result, "tags") or []
            if not isinstance(item_tags, list):
                item_tags = [str(item_tags)]
            else:
                item_tags = [str(t) for t in item_tags]

            metadata_tags = self._extract_metadata_tags(metadata)
            if not metadata_tags:
                get_tag = getattr(backend, "get_tag", None)
                if callable(get_tag):
                    try:
                        backend_tags, _source = get_tag(file_hash, config=config)
                        metadata_tags = [
                            str(tag) for tag in (backend_tags or [])
                            if str(tag or "").strip()
                        ]
                    except Exception:
                        metadata_tags = []

            for tag_value in metadata_tags:
                tag_text = str(tag_value or "").strip()
                if not tag_text:
                    continue
                if tag_text not in item_tags:
                    item_tags.append(tag_text)
                if not get_field(result, "title") and tag_text.lower().startswith("title:"):
                    parts = tag_text.split(":", 1)
                    if len(parts) > 1:
                        title = parts[1].strip()


            # Extract metadata fields
            mime_type = metadata.get("mime") or metadata.get("ext", "")
            file_ext = get_extension_field(metadata, "ext", "extension")
            file_size = get_int_field(metadata, "size", "size_bytes")
            duration_seconds = metadata.get("duration")
            if duration_seconds is None:
                duration_seconds = metadata.get("duration_seconds")
            if duration_seconds is None:
                duration_seconds = metadata.get("length")
            if duration_seconds is None and isinstance(metadata.get("duration_ms"),
                                                       (int,
                                                        float)):
                try:
                    duration_seconds = float(metadata["duration_ms"]) / 1000.0
                except Exception:
                    duration_seconds = None

            if isinstance(duration_seconds, str):
                s = duration_seconds.strip()
                if s:
                    try:
                        duration_seconds = float(s)
                    except ValueError:
                        if ":" in s:
                            parts = [p.strip() for p in s.split(":") if p.strip()]
                            if len(parts) in {2,
                                              3} and all(p.isdigit() for p in parts):
                                nums = [int(p) for p in parts]
                                if len(nums) == 2:
                                    duration_seconds = float(nums[0] * 60 + nums[1])
                                else:
                                    duration_seconds = float(
                                        nums[0] * 3600 + nums[1] * 60 + nums[2]
                                    )
                        else:
                            duration_seconds = None
            pages = metadata.get("pages")
            url = metadata.get("url") or []
            imported_ts = self._extract_imported_ts(metadata)

            # Normalize url
            if isinstance(url, str):
                try:
                    url = json.loads(url)
                except (json.JSONDecodeError, TypeError):
                    url = []
            if not isinstance(url, list):
                url = []

            # Build display row
            row = self._build_table_row(
                title=title,
                store=storage_source,
                path=metadata.get("path",
                                  ""),
                mime=mime_type,
                size_bytes=file_size,
                dur_seconds=duration_seconds,
                imported_ts=imported_ts,
                url=url,
                hash_value=file_hash,
                pages=pages,
                tag=item_tags,
                ext=file_ext,
            )
            plugin_name = str(
                getattr(backend, "STORE_TYPE", None)
                or getattr(backend, "PLUGIN_NAME", None)
                or ""
            ).strip()
            if plugin_name:
                row["plugin"] = plugin_name

            table_title = f"get-metadata: {title}" if title else "get-metadata"
            table = Table(table_title
                                ).init_command(table_title,
                                               "get-metadata",
                                               list(args))
            self._add_table_body_row(table, row)
            # Use helper to display item and make it @-selectable
            from ._shared import display_and_persist_items
            display_and_persist_items([row], title=table_title, subject=row)
            ctx.emit(row)
            return 0

        except KeyError:
            log(f"Storage backend '{storage_source}' not found", file=sys.stderr)
            return 1
        except Exception as exc:
            log(f"Failed to get metadata: {exc}", file=sys.stderr)
            return 1


CMDLET = Get_Metadata()
