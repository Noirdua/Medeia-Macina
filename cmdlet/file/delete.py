"""Delete-file cmdlet: Delete files from local storage and/or Hydrus."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence
import posixpath
import sys
from pathlib import Path

from SYS.logger import debug, log
from PluginCore.registry import get_plugin, plugin_for_storage

from .. import _shared as sh
from SYS import pipeline as ctx
from SYS.result_table_helpers import add_row_columns
from SYS.result_table import Table, _format_size
from SYS.rich_display import stdout_console


class Delete_File(sh.Cmdlet):
    """Class-based delete-file cmdlet with self-registration."""

    def __init__(self) -> None:
        super().__init__(
            name="delete-file",
            summary=
            "Delete a file locally and/or from Hydrus, including database entries.",
            usage=
            'delete-file [-query "hash:<sha256>"] [-conserve <local|hydrus>] [-lib-root <path>] [reason]',
            alias=["del-file"],
            arg=[
                sh.SharedArgs.QUERY,
                sh.CmdletArg(
                    "conserve",
                    description="Choose which copy to keep: 'local' or 'hydrus'."
                ),
                sh.CmdletArg(
                    "lib-root",
                    description="Path to local library root for database cleanup."
                ),
                sh.CmdletArg(
                    "reason",
                    description="Optional reason for deletion (free text)."
                ),
            ],
            detail=[
                "Default removes both the local file and Hydrus file.",
                "Use -conserve local to keep the local file, or -conserve hydrus to keep it in Hydrus.",
                "Database entries are automatically cleaned up for local files.",
                "Any remaining arguments are treated as the Hydrus reason text.",
            ],
            exec=self.run,
        )
        self.register()

    def _process_single_item(
        self,
        item: Any,
        override_hash: str | None,
        conserve: str | None,
        lib_root: str | None,
        reason: str,
        config: Dict[str,
                     Any],
        skip_hydrus_hashes: Optional[set[str]] = None,
        registry: Any = None,
    ) -> List[Dict[str,
                   Any]]:
        """Process deletion for a single item.

        Returns display rows (for the final Rich table). Returning an empty list
        indicates no delete occurred.
        """
        # Handle item as either dict or object
        if isinstance(item, dict):
            hash_hex_raw = item.get("hash_hex") or item.get("hash")
            target = item.get("target") or item.get("file_path") or item.get("path")
            title_val = item.get("title") or item.get("name")
        else:
            hash_hex_raw = sh.get_field(item, "hash_hex") or sh.get_field(item, "hash")
            target = (
                sh.get_field(item,
                             "target") or sh.get_field(item,
                                                       "file_path")
                or sh.get_field(item,
                                "path")
            )
            title_val = sh.get_field(item, "title") or sh.get_field(item, "name")

        def _get_ext_from_item() -> str:
            try:
                if isinstance(item, dict):
                    ext_val = item.get("ext")
                    if ext_val:
                        return str(ext_val)
                    extra = item.get("extra")
                    if isinstance(extra, dict) and extra.get("ext"):
                        return str(extra.get("ext"))
                else:
                    ext_val = sh.get_field(item, "ext")
                    if ext_val:
                        return str(ext_val)
                    extra = sh.get_field(item, "extra")
                    if isinstance(extra, dict) and extra.get("ext"):
                        return str(extra.get("ext"))
            except Exception:
                pass

            # Fallback: infer from target path or title if it looks like a filename
            try:
                if isinstance(target, str) and target:
                    suffix = Path(target).suffix
                    if suffix:
                        return suffix.lstrip(".")
            except Exception:
                pass

            try:
                if title_val:
                    suffix = Path(str(title_val)).suffix
                    if suffix:
                        return suffix.lstrip(".")
            except Exception:
                pass

            return ""

        store = None
        if isinstance(item, dict):
            store = item.get("store")
        else:
            store = sh.get_field(item, "store")

        # Extract plugin/provider identity and full metadata for plugin-level dispatch
        provider_name = None
        full_metadata: Dict[str, Any] = {}
        if isinstance(item, dict):
            provider_name = item.get("plugin") or item.get("table")
            raw_meta = item.get("full_metadata") or item.get("metadata")
            if isinstance(raw_meta, dict):
                full_metadata = raw_meta
        else:
            try:
                provider_name = sh.get_field(item, "plugin") or sh.get_field(item, "table")
            except Exception:
                pass
            try:
                raw_meta = sh.get_field(item, "full_metadata") or sh.get_field(item, "metadata")
                if isinstance(raw_meta, dict):
                    full_metadata = raw_meta
            except Exception:
                pass
        provider_name = str(provider_name or "").strip().lower() or None

        store_lower = str(store).lower() if store else ""

        backend = None
        try:
            if store:
                if registry is None:
                    from PluginCore.backend_registry import get_or_create_registry

                    registry = get_or_create_registry(config, suppress_debug=True)
                if registry.is_available(str(store)):
                    backend = registry[str(store)]
        except Exception:
            backend = None

        hydrus_provider = plugin_for_storage(
            config, name=provider_name, item=item, backend=backend
        )
        is_hydrus_store = False
        try:
            if hydrus_provider is not None and backend is not None:
                is_hydrus_store = bool(hydrus_provider.is_backend(backend, str(store or "")))
        except Exception:
            is_hydrus_store = False

        # Backwards-compatible fallback heuristic (older items might only carry a name).
        if (not is_hydrus_store) and hydrus_provider is not None and bool(store_lower):
            try:
                is_hydrus_store = bool(hydrus_provider.is_store_name(store_lower))
            except Exception:
                is_hydrus_store = False
        store_label = str(store) if store else "default"
        hydrus_prefix = f"[hydrusnetwork:{store_label}]"

        # For Hydrus files, the target IS the hash
        if is_hydrus_store and not hash_hex_raw:
            hash_hex_raw = target

        hash_hex = (
            sh.normalize_hash(override_hash)
            if override_hash else sh.normalize_hash(hash_hex_raw)
        )

        local_deleted = False
        _target_str = str(target).strip().lower() if isinstance(target, str) else ""
        local_target = (
            isinstance(target, str) and target.strip()
            and not _target_str.startswith(("http://", "https://", "ftp://", "ftps://"))
        )
        deleted_rows: List[Dict[str, Any]] = []

        # --- Plugin-level delete dispatch ---
        # When the item originates from a plugin (e.g. FTP), and that plugin exposes
        # a delete_file() method, delegate to it instead of attempting a local unlink.
        if conserve != "local" and provider_name and not is_hydrus_store:
            try:
                candidate_plugin = get_plugin(provider_name, config)
                plugin_deleter = getattr(candidate_plugin, "delete_file", None) if candidate_plugin else None
                if callable(plugin_deleter):
                    # Prefer ftp_path from full_metadata; fall back to the path/url field
                    remote = (
                        full_metadata.get("ftp_path")
                        or full_metadata.get("selection_url")
                        or full_metadata.get("ftp_url")
                        or (str(target).strip() if isinstance(target, str) else "")
                    )
                    instance_hint = full_metadata.get("instance") or None
                    if remote:
                        plugin_ok = bool(plugin_deleter(remote, instance=instance_hint))
                        if plugin_ok:
                            local_deleted = True
                            size_hint = (
                                full_metadata.get("size")
                                or (item.get("size_bytes") if isinstance(item, dict) else None)
                                or sh.get_field(item, "size_bytes")
                            )
                            deleted_rows.append(
                                {
                                    "title": str(title_val).strip() if title_val else posixpath.basename(str(remote).rstrip("/")),
                                    "store": instance_hint or provider_name,
                                    "hash": hash_hex or "",
                                    "size_bytes": size_hint,
                                    "ext": _get_ext_from_item(),
                                }
                            )
                            return deleted_rows
            except Exception:
                pass

        # If this item references a configured non-Hydrus store backend, prefer deleting
        # via the backend API. This supports store items where `path`/`target` is the hash.
        if conserve != "local" and store and (not is_hydrus_store):
            try:
                # Re-use an already resolved backend when available.
                if backend is None:
                    if registry is None:
                        from PluginCore.backend_registry import get_or_create_registry

                        registry = get_or_create_registry(config, suppress_debug=True)
                    if registry.is_available(str(store)):
                        backend = registry[str(store)]

                if backend is not None:

                    # Prefer hash when available.
                    hash_candidate = sh.normalize_hash(
                        hash_hex_raw
                    ) if hash_hex_raw else None
                    if not hash_candidate and isinstance(target, str):
                        hash_candidate = sh.normalize_hash(target)

                    resolved_path = None
                    try:
                        if hash_candidate and hasattr(backend, "get_file"):
                            candidate_path = backend.get_file(hash_candidate)
                            resolved_path = (
                                candidate_path if isinstance(candidate_path,
                                                             Path) else None
                            )
                    except Exception:
                        resolved_path = None

                    identifier = hash_candidate or (
                        str(target).strip() if isinstance(target,
                                                          str) else ""
                    )
                    if identifier:
                        deleter = getattr(backend, "delete_file", None)
                        if callable(deleter) and bool(deleter(identifier)):
                            local_deleted = True

                            size_bytes: int | None = None
                            try:
                                if (resolved_path is not None
                                        and isinstance(resolved_path,
                                                       Path)
                                        and resolved_path.exists()):
                                    size_bytes = int(resolved_path.stat().st_size)
                            except Exception:
                                size_bytes = None

                            deleted_rows.append(
                                {
                                    "title": (
                                        str(title_val).strip() if title_val else (
                                            resolved_path.name
                                            if resolved_path else identifier
                                        )
                                    ),
                                    "store":
                                    store_label,
                                    "hash":
                                    hash_candidate or (hash_hex or ""),
                                    "size_bytes":
                                    size_bytes,
                                    "ext":
                                    _get_ext_from_item() or (
                                        resolved_path.suffix.lstrip(".")
                                        if resolved_path else ""
                                    ),
                                }
                            )

                            # Best-effort remove sidecars if we know the resolved path.
                            try:
                                if resolved_path is not None and isinstance(
                                        resolved_path,
                                        Path):
                                    for sidecar in (
                                            resolved_path.with_suffix(".tag"),
                                            resolved_path.with_suffix(".metadata"),
                                            resolved_path.with_suffix(".notes"),
                                    ):
                                        try:
                                            if sidecar.exists() and sidecar.is_file():
                                                sidecar.unlink()
                                        except Exception:
                                            pass
                            except Exception:
                                pass

                            # Skip legacy local-path deletion below.
                            local_target = False
            except Exception:
                pass

        if conserve != "local" and local_target:
            path = Path(str(target))
            size_bytes: int | None = None
            try:
                if path.exists() and path.is_file():
                    size_bytes = int(path.stat().st_size)
            except Exception:
                size_bytes = None

            # Delete the local file directly
            try:
                if path.exists() and path.is_file():
                    path.unlink()
                    local_deleted = True
                    deleted_rows.append(
                        {
                            "title":
                            str(title_val).strip() if title_val else path.name,
                            "store": store_label,
                            "hash": hash_hex or sh.normalize_hash(path.stem) or "",
                            "size_bytes": size_bytes,
                            "ext": _get_ext_from_item() or path.suffix.lstrip("."),
                        }
                    )
            except Exception as exc:
                log(f"Local delete failed: {exc}", file=sys.stderr)

            # Remove common sidecars regardless of file removal success
            for sidecar in (
                    path.with_suffix(".tag"),
                    path.with_suffix(".metadata"),
                    path.with_suffix(".notes"),
            ):
                try:
                    if sidecar.exists() and sidecar.is_file():
                        sidecar.unlink()
                except Exception:
                    pass

        hydrus_deleted = False
        should_try_hydrus = is_hydrus_store

        # If conserve is set to hydrus, definitely don't delete
        if conserve == "hydrus":
            should_try_hydrus = False

        if should_try_hydrus and hash_hex:
            did_hydrus_delete = False
            if skip_hydrus_hashes and hash_hex in skip_hydrus_hashes:
                did_hydrus_delete = True
            else:
                try:
                    if hydrus_provider is not None:
                        did_hydrus_delete = bool(
                            hydrus_provider.delete_hash(
                                hash_hex,
                                store_name=str(store) if store else None,
                                reason=reason or None,
                            )
                        )
                except Exception:
                    did_hydrus_delete = False

            if did_hydrus_delete:
                hydrus_deleted = True
                title_str = str(title_val).strip() if title_val else ""
                if title_str:
                    debug(
                        f"{hydrus_prefix} Deleted title:{title_str} hash:{hash_hex}",
                        file=sys.stderr,
                    )
                else:
                    debug(f"{hydrus_prefix} Deleted hash:{hash_hex}", file=sys.stderr)
            else:
                if not local_deleted:
                    if store:
                        log(f"Hydrus store unavailable for '{store}'", file=sys.stderr)
                    else:
                        log("Hydrus delete failed", file=sys.stderr)
                    return []

        if hydrus_deleted and hash_hex:
            size_hint = None
            try:
                if isinstance(item, dict):
                    size_hint = item.get("size_bytes") or item.get("size")
                else:
                    size_hint = sh.get_field(item,
                                             "size_bytes"
                                             ) or sh.get_field(item,
                                                               "size")
            except Exception:
                size_hint = None
            deleted_rows.append(
                {
                    "title": str(title_val).strip() if title_val else "",
                    "store": store_label,
                    "hash": hash_hex,
                    "size_bytes": size_hint,
                    "ext": _get_ext_from_item(),
                }
            )

        if hydrus_deleted or local_deleted:
            return deleted_rows

        log("Selected result has neither Hydrus hash nor local file target")
        return []

    def run(self, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        """Execute delete-file command."""
        if sh.should_show_help(args):
            log(f"Cmdlet: {self.name}\nSummary: {self.summary}\nUsage: {self.usage}")
            return 0

        # Parse arguments
        override_query: str | None = None
        override_hash: str | None = None
        conserve: str | None = None
        lib_root: str | None = None
        reason_tokens: list[str] = []
        i = 0

        while i < len(args):
            token = args[i]
            low = str(token).lower()
            if low in {"-query",
                       "--query",
                       "query"} and i + 1 < len(args):
                override_query = str(args[i + 1]).strip()
                i += 2
                continue
            if low in {"-conserve",
                       "--conserve"} and i + 1 < len(args):
                value = str(args[i + 1]).strip().lower()
                if value in {"local",
                             "hydrus"}:
                    conserve = value
                    i += 2
                    continue
            if low in {"-lib-root",
                       "--lib-root",
                       "lib-root"} and i + 1 < len(args):
                lib_root = str(args[i + 1]).strip()
                i += 2
                continue
            reason_tokens.append(token)
            i += 1

        override_hash, query_valid = sh.require_single_hash_query(
            override_query,
            "Invalid -query value (expected hash:<sha256>)",
            log_file=sys.stderr,
        )
        if not query_valid:
            return 1

        reason = " ".join(token for token in reason_tokens
                          if str(token).strip()).strip()

        items = sh.normalize_result_items(result)

        if not items:
            log("No items to delete", file=sys.stderr)
            return 1

        success_count = 0
        deleted_rows: List[Dict[str, Any]] = []
        skip_hydrus_hashes: set[str] = set()
        if conserve != "hydrus":
            hydrus_provider = plugin_for_storage(config) or get_plugin("hydrusnetwork", config)
            by_store: Dict[str, List[str]] = {}
            if hydrus_provider is not None:
                for item in items:
                    if isinstance(item, dict):
                        store = item.get("store")
                        hash_raw = item.get("hash_hex") or item.get("hash")
                    else:
                        store = sh.get_field(item, "store")
                        hash_raw = sh.get_field(item, "hash_hex") or sh.get_field(item, "hash")
                    hash_hex = (
                        sh.normalize_hash(override_hash)
                        if override_hash else sh.normalize_hash(hash_raw)
                    )
                    if not hash_hex:
                        continue
                    store_key = str(store or "")
                    is_hydrus = False
                    try:
                        is_hydrus = bool(hydrus_provider.is_store_name(store_key.lower())) if store_key else False
                    except Exception:
                        is_hydrus = False
                    if is_hydrus:
                        by_store.setdefault(store_key, []).append(hash_hex)
                for store_key, hashes in by_store.items():
                    try:
                        if hydrus_provider.delete_hashes(
                            hashes,
                            store_name=store_key or None,
                            reason=reason or None,
                        ):
                            skip_hydrus_hashes.update(hashes)
                    except Exception:
                        pass
        try:
            from PluginCore.backend_registry import get_or_create_registry

            shared_registry = get_or_create_registry(config, suppress_debug=True)
        except Exception:
            shared_registry = None
        for item in items:
            rows = self._process_single_item(
                item,
                override_hash,
                conserve,
                lib_root,
                reason,
                config,
                skip_hydrus_hashes=skip_hydrus_hashes,
                registry=shared_registry,
            )
            if rows:
                success_count += 1
                deleted_rows.extend(rows)

        if deleted_rows:
            table = Table("Deleted")
            table._interactive(True)._perseverance(True)
            for row in deleted_rows:
                add_row_columns(
                    table,
                    [
                        ("Title", row.get("title", "")),
                        ("Instance", row.get("store", "")),
                        ("Hash", row.get("hash", "")),
                        ("Size", _format_size(row.get("size_bytes"), integer_only=False)),
                        ("Ext", row.get("ext", "")),
                    ],
                )

            # Display-only: print directly and do not affect selection/history.
            try:
                stdout_console().print()
                stdout_console().print(table)
                setattr(table, "_rendered_by_cmdlet", True)
            except Exception:
                pass

            # Ensure no stale overlay/selection carries forward.
            try:
                ctx.set_last_result_items_only([])
                ctx.set_current_stage_table(None)
            except Exception:
                pass

        return 0 if success_count > 0 else 1


# Instantiate and register the cmdlet
CMDLET = Delete_File()
