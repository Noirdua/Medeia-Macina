from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple
import sys

from SYS import pipeline as ctx
from .. import _shared as sh
from SYS.logger import log
from PluginCore.backend_registry import BackendRegistry


class Add_Url(sh.Cmdlet):
    """Add URL associations to files via hash+store."""

    def __init__(self) -> None:
        super().__init__(
            name="add-url",
            summary="Associate a URL with a file",
            usage="@1 | add-url <url>",
            arg=[
                sh.SharedArgs.QUERY,
                sh.SharedArgs.INSTANCE,
                sh.CmdletArg("url",
                             required=True,
                             description="URL to associate"),
            ],
            detail=[
                "- Associates URL with file identified by hash+store",
                "- Multiple url can be comma-separated",
            ],
            exec=self.run,
        )
        self.register()

    def run(self, result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
        """Add URL to file via hash+store backend."""
        parsed = sh.parse_cmdlet_args(args, self)

        # Compatibility/piping fix:
        # `SharedArgs.QUERY` is positional in the shared parser, so `add-url <url>`
        # (and `@N | add-url <url>`) can mistakenly parse the URL into `query`.
        # If `url` is missing and `query` looks like an http(s) URL, treat it as `url`.
        try:
            if (not parsed.get("url")) and isinstance(parsed.get("query"), str):
                q = str(parsed.get("query") or "").strip()
                if q.startswith(("http://", "https://")):
                    parsed["url"] = q
                    parsed.pop("query", None)
        except Exception:
            pass

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
            sh.get_field(result,
                         "hash") if result is not None else None
        )
        store_name = parsed.get("instance") or (
            sh.get_field(result,
                         "store") if result is not None else None
        )
        url_arg = parsed.get("url")
        if not url_arg:
            try:
                inferred = sh.extract_url_from_result(result)
                if inferred:
                    candidate = inferred[0]
                    if isinstance(candidate, str) and candidate.strip():
                        url_arg = candidate.strip()
                    parsed["url"] = url_arg
            except Exception:
                pass

        # If we have multiple piped items, we will resolve hash/store per item below.
        if not results:
            if not file_hash:
                log(
                    'Error: No file hash provided (pipe an item or use -query "hash:<sha256>")'
                )
                return 1
            if not store_name:
                log("Error: No store name provided")
                return 1

        if not url_arg:
            log("Error: No URL provided")
            return 1

        # Normalize hash (single-item mode)
        if not results and file_hash:
            file_hash = sh.normalize_hash(file_hash)
            if not file_hash:
                log("Error: Invalid hash format")
                return 1

        # Parse url (comma-separated)
        urls = [u.strip() for u in str(url_arg).split(",") if u.strip()]
        if not urls:
            log("Error: No valid url provided")
            return 1

        # Get backend and add url
        try:
            storage = BackendRegistry(config)

            # Build batches per store.
            store_override = parsed.get("instance")

            if results:
                def _warn(message: str) -> None:
                    ctx.print_if_visible(f"[add-url] Warning: {message}", file=sys.stderr)

                batch, pass_through = sh.collect_store_hash_value_batch(
                    results,
                    store_registry=storage,
                    value_resolver=lambda _item: list(urls),
                    override_hash=query_hash,
                    override_store=store_override,
                    on_warning=_warn,
                )

                supported_batch: Dict[str, List[Tuple[str, Sequence[str]]]] = {}
                for store_text, store_pairs in batch.items():
                    backend, storage, _exc = sh.get_store_backend(
                        config,
                        store_text,
                        store_registry=storage,
                    )
                    if backend is None:
                        _warn(f"Store '{store_text}' not configured; skipping")
                        continue
                    if not bool(getattr(backend, "supports_url_association", False)):
                        _warn(f"Store '{store_text}' does not support URLs; skipping")
                        continue
                    supported_batch[store_text] = store_pairs

                # Execute per-instance batches.
                storage, batch_stats = sh.run_store_hash_value_batches(
                    config,
                    supported_batch,
                    bulk_method_name="add_url_bulk",
                    single_method_name="add_url",
                    store_registry=storage,
                )
                for store_text, item_count, _value_count in batch_stats:
                    ctx.print_if_visible(
                        f"✓ add-url: {len(urls)} url(s) for {item_count} item(s) in '{store_text}'",
                        file=sys.stderr,
                    )

                # Pass items through unchanged (but update url field for convenience).
                for item in pass_through:
                    existing = sh.get_field(item, "url")
                    merged = sh.merge_urls(existing, list(urls))
                    sh.set_item_urls(item, merged)
                    ctx.emit(item)
                return 0

            # Single-item mode
            backend, storage, exc = sh.get_store_backend(
                config,
                str(store_name),
                store_registry=storage,
            )
            if backend is None:
                log(f"Error: Storage backend '{store_name}' not configured")
                return 1
            if not bool(getattr(backend, "supports_url_association", False)):
                log(f"Error: Store '{store_name}' does not support URL associations")
                return 1
            backend.add_url(str(file_hash), urls, config=config)
            ctx.print_if_visible(
                f"✓ add-url: {len(urls)} url(s) added",
                file=sys.stderr
            )
            if result is not None:
                existing = sh.get_field(result, "url")
                merged = sh.merge_urls(existing, list(urls))
                sh.set_item_urls(result, merged)
                ctx.emit(result)
            return 0

        except Exception as exc:
            log(f"Error adding URL: {exc}", file=sys.stderr)
            return 1


CMDLET = Add_Url()
