from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple
import sys

from SYS import pipeline as ctx
from . import _shared as sh
from ._shared import (
    Cmdlet,
    CmdletArg,
    SharedArgs,
    parse_cmdlet_args,
    get_field,
    normalize_hash,
)
from SYS.logger import log
from PluginCore.backend_registry import BackendRegistry


class Delete_Url(Cmdlet):
    """Delete URL associations from files via hash+store."""

    def __init__(self) -> None:
        super().__init__(
            name="delete-url",
            summary="Remove a URL association from a file",
            usage="@1 | delete-url <url>",
            arg=[
                SharedArgs.QUERY,
                SharedArgs.INSTANCE,
                CmdletArg(
                    "url",
                    required=False,
                    description="URL to remove (optional when piping url rows)",
                ),
            ],
            detail=[
                "- Removes URL association from file identified by hash+instance",
                "- Multiple url can be comma-separated",
            ],
            exec=self.run,
        )
        self.register()

    def run(self, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        """Delete URL from file via hash+store backend."""
        parsed = parse_cmdlet_args(args, self)

        query_hash, query_valid = sh.require_single_hash_query(
            parsed.get("query"),
            "Error: -query must be of the form hash:<sha256>",
        )
        if not query_valid:
            return 1

        # Bulk input is common in pipelines; treat a list of PipeObjects as a batch.
        results: List[Any] = (
            result if isinstance(result,
                                 list) else ([result] if result is not None else [])
        )

        if query_hash and len(results) > 1:
            log("Error: -query hash:<sha256> cannot be used with multiple piped items")
            return 1

        # Extract hash and store from result or args
        file_hash = query_hash or (
            get_field(result,
                      "hash") if result is not None else None
        )
        store_name = parsed.get("instance") or (
            get_field(result,
                      "store") if result is not None else None
        )
        url_arg = parsed.get("url")

        # If we have multiple piped items, we will resolve hash/store per item below.
        if not results:
            if not file_hash:
                log(
                    'Error: No file hash provided (pipe an item or use -query "hash:<sha256>")'
                )
                return 1
            if not store_name:
                log("Error: No instance name provided")
                return 1

        # Normalize hash (single-item mode)
        if not results and file_hash:
            file_hash = normalize_hash(file_hash)
            if not file_hash:
                log("Error: Invalid hash format")
                return 1

        from SYS.metadata import normalize_urls

        def _urls_from_arg(raw: Any) -> List[str]:
            if raw is None:
                return []
            # Support comma-separated input for backwards compatibility
            if isinstance(raw, str) and "," in raw:
                return [u.strip() for u in raw.split(",") if u.strip()]
            return [u.strip() for u in normalize_urls(raw) if str(u).strip()]

        urls_from_cli = _urls_from_arg(url_arg)

        # Get backend and delete url
        try:
            storage = BackendRegistry(config)

            store_override = parsed.get("instance")

            if results:
                def _warn(message: str) -> None:
                    ctx.print_if_visible(f"[delete-url] Warning: {message}", file=sys.stderr)

                def _resolve_item_urls(item: Any) -> List[str]:
                    item_urls = list(urls_from_cli)
                    if not item_urls:
                        item_urls = [
                            u.strip() for u in normalize_urls(
                                get_field(item, "url") or get_field(item, "source_url")
                            ) if str(u).strip()
                        ]
                    if not item_urls:
                        _warn("Item has no url field; skipping")
                    return item_urls

                batch, pass_through = sh.collect_store_hash_value_batch(
                    results,
                    store_registry=storage,
                    value_resolver=_resolve_item_urls,
                    override_hash=query_hash,
                    override_store=store_override,
                    on_warning=_warn,
                )

                storage, batch_stats = sh.run_store_hash_value_batches(
                    config,
                    batch,
                    bulk_method_name="delete_url_bulk",
                    single_method_name="delete_url",
                    store_registry=storage,
                )
                for store_text, item_count, deleted_count in batch_stats:
                    ctx.print_if_visible(
                        f"✓ delete-url: {deleted_count} url(s) for {item_count} item(s) in '{store_text}'",
                        file=sys.stderr,
                    )

                for item in pass_through:
                    existing = get_field(item, "url")
                    # In batch mode we removed the union of requested urls for the file.
                    # Using urls_from_cli (if present) matches the user's explicit intent; otherwise
                    # remove the piped url row(s).
                    remove_set = urls_from_cli
                    if not remove_set:
                        remove_set = [
                            u.strip() for u in normalize_urls(
                                get_field(item, "url") or get_field(item, "source_url")
                            ) if str(u).strip()
                        ]
                    sh.set_item_urls(item, sh.remove_urls(existing, list(remove_set)))
                    ctx.emit(item)
                return 0

            # Single-item mode
            if not urls_from_cli:
                urls_from_cli = [
                    u.strip() for u in normalize_urls(
                        get_field(result, "url") or get_field(result, "source_url")
                    ) if str(u).strip()
                ]
            if not urls_from_cli:
                log("Error: No URL provided")
                return 1

            backend, storage, exc = sh.get_store_backend(
                config,
                str(store_name),
                store_registry=storage,
            )
            if backend is None:
                log(f"Error: Storage backend '{store_name}' not configured")
                return 1
            backend.delete_url(str(file_hash), list(urls_from_cli), config=config)
            ctx.print_if_visible(
                f"✓ delete-url: {len(urls_from_cli)} url(s) removed",
                file=sys.stderr
            )
            if result is not None:
                existing = get_field(result, "url")
                sh.set_item_urls(result, sh.remove_urls(existing, list(urls_from_cli)))
                ctx.emit(result)
            return 0

        except Exception as exc:
            log(f"Error deleting URL: {exc}", file=sys.stderr)
            return 1


CMDLET = Delete_Url()
