"""Store and storage operation utilities.

Contains hash resolution, store batch dispatching, and hash query parsing
used by store-related cmdlets.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from pathlib import Path

from SYS.logger import log, error_panel
from ._preflight import get_store_backend

__all__ = [
    "resolve_hash_for_cmdlet",
    "parse_hash_query",
    "parse_single_hash_query",
    "require_hash_query",
    "require_single_hash_query",
    "get_hash_for_operation",
    "coalesce_hash_value_pairs",
    "run_store_hash_value_batches",
    "run_store_note_batches",
    "collect_store_hash_value_batch",
]


def resolve_hash_for_cmdlet(
    raw_hash: Optional[str],
    raw_path: Optional[str],
    override_hash: Optional[str],
) -> Optional[str]:
    """Resolve a file hash for note/tag/file cmdlets.

    Shared implementation used by add-note, delete-note, get-note, and similar
    cmdlets that need to identify a file by its SHA-256 hash.

    Resolution order:
    1. ``override_hash`` — explicit hash provided via *-query* (highest priority)
    2. ``raw_hash`` — positional hash argument
    3. ``raw_path`` stem — if the filename stem is a 64-char hex string it is
       treated directly as the hash (Hydrus-style naming convention)
    4. SHA-256 computed from the file at ``raw_path``

    Args:
        raw_hash: Hash string from positional argument.
        raw_path: Filesystem path to the file (may be None).
        override_hash: Hash extracted from *-query* (takes precedence).

    Returns:
        Normalised 64-char lowercase hex hash, or ``None`` if unresolvable.
    """
    from ._tag_utils import normalize_hash

    resolved = normalize_hash(override_hash) if override_hash else normalize_hash(raw_hash)
    if resolved:
        return resolved
    if raw_path:
        try:
            p = Path(str(raw_path))
            stem = p.stem
            if len(stem) == 64 and all(c in "0123456789abcdef" for c in stem.lower()):
                return stem.lower()
            if p.exists() and p.is_file():
                from SYS.utils import sha256_file as _sha256_file
                return _sha256_file(p)
        except Exception:
            return None
    return None


def parse_hash_query(query: Optional[str]) -> List[str]:
    """Parse a unified query string for `hash:` into normalized SHA256 hashes.

    Supported examples:
    - hash:<h1>
    - hash:<h1>,<h2>,<h3>
    - Hash: <h1> <h2> <h3>
    - hash:{<h1>, <h2>}

    Returns:
            List of unique normalized 64-hex SHA256 hashes.
    """
    import re
    from ._tag_utils import normalize_hash

    q = str(query or "").strip()
    if not q:
        return []

    m = re.match(r"^hash(?:es)?\s*:\s*(.+)$", q, flags=re.IGNORECASE)
    if not m:
        return []

    rest = (m.group(1) or "").strip()
    if rest.startswith("{") and rest.endswith("}"):
        rest = rest[1:-1].strip()
    if rest.startswith("[") and rest.endswith("]"):
        rest = rest[1:-1].strip()

    raw_parts = [p.strip() for p in re.split(r"[\s,]+", rest) if p.strip()]
    out: List[str] = []
    for part in raw_parts:
        h = normalize_hash(part)
        if not h:
            continue
        if h not in out:
            out.append(h)
    return out


def parse_single_hash_query(query: Optional[str]) -> Optional[str]:
    """Parse `hash:` query and require exactly one hash."""
    hashes = parse_hash_query(query)
    if len(hashes) != 1:
        return None
    return hashes[0]


def require_hash_query(
    query: Optional[str],
    error_message: str,
    *,
    log_file: Any = None,
) -> Tuple[List[str], bool]:
    """Parse a multi-hash query and log a caller-provided error on invalid input."""
    hashes = parse_hash_query(query)
    if query and not hashes:
        error_panel("Error", [("message", error_message)])
        return [], False
    return hashes, True


def require_single_hash_query(
    query: Optional[str],
    error_message: str,
    *,
    log_file: Any = None,
) -> Tuple[Optional[str], bool]:
    """Parse a single-hash query and log a caller-provided error on invalid input."""
    query_hash = parse_single_hash_query(query)
    if query and not query_hash:
        error_panel("Error", [("message", error_message)])
        return None, False
    return query_hash, True


def get_hash_for_operation(
    override_hash: Optional[str],
    result: Any,
    field_name: str = "hash",
) -> Optional[str]:
    """Get normalized hash from override or result object, consolidating common pattern.

    Eliminates repeated pattern: normalize_hash(override) if override else normalize_hash(get_field(result, ...))

    Args:
            override_hash: Hash passed as command argument (takes precedence)
            result: Object containing hash field (fallback)
            field_name: Name of hash field in result object (default: "hash")

    Returns:
            Normalized hash string, or None if neither override nor result provides valid hash
    """
    from ._tag_utils import normalize_hash
    from ._pipeobject_utils import get_field

    if override_hash:
        return normalize_hash(override_hash)
    hash_value = (
        get_field(result,
                  field_name) or getattr(result,
                                         field_name,
                                         None) or getattr(result,
                                                          "hash",
                                                          None)
    )
    return normalize_hash(hash_value)


def coalesce_hash_value_pairs(
    pairs: Sequence[Tuple[str, Sequence[str]]],
) -> List[Tuple[str, List[str]]]:
    """Merge duplicate hash/value pairs while preserving first-seen value order."""
    merged: Dict[str, List[str]] = {}
    for hash_value, values in pairs:
        normalized_hash = str(hash_value or "").strip()
        if not normalized_hash:
            continue
        bucket = merged.setdefault(normalized_hash, [])
        seen = set(bucket)
        for value in values or []:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            bucket.append(text)
    return [(hash_value, items) for hash_value, items in merged.items() if items]


def run_store_hash_value_batches(
    config: Optional[Dict[str, Any]],
    batch: Dict[str, List[Tuple[str, Sequence[str]]]],
    *,
    bulk_method_name: str,
    single_method_name: str,
    store_registry: Any = None,
    suppress_debug: bool = False,
    pass_config_to_bulk: bool = True,
    pass_config_to_single: bool = True,
) -> Tuple[Any, List[Tuple[str, int, int]]]:
    """Dispatch grouped hash/value batches across stores.

    Returns ``(store_registry, stats)`` where ``stats`` contains
    ``(store_name, item_count, value_count)`` for each dispatched store.
    Missing stores are skipped so callers can preserve existing warning behavior.
    """
    registry = store_registry
    stats: List[Tuple[str, int, int]] = []
    for store_name, pairs in batch.items():
        backend, registry, _exc = get_store_backend(
            config,
            store_name,
            store_registry=registry,
            suppress_debug=suppress_debug,
        )
        if backend is None:
            continue

        bulk_pairs = coalesce_hash_value_pairs(pairs)
        if not bulk_pairs:
            continue

        bulk_fn = getattr(backend, bulk_method_name, None)
        if callable(bulk_fn):
            if pass_config_to_bulk:
                bulk_fn(bulk_pairs, config=config)
            else:
                bulk_fn(bulk_pairs)
        else:
            single_fn = getattr(backend, single_method_name)
            for hash_value, values in bulk_pairs:
                if pass_config_to_single:
                    single_fn(hash_value, values, config=config)
                else:
                    single_fn(hash_value, values)

        stats.append(
            (
                store_name,
                len(bulk_pairs),
                sum(len(values or []) for _hash_value, values in bulk_pairs),
            )
        )

    return registry, stats


def run_store_note_batches(
    config: Optional[Dict[str, Any]],
    batch: Dict[str, List[Tuple[str, str, str]]],
    *,
    store_registry: Any = None,
    suppress_debug: bool = False,
    on_store_error: Optional[Callable[[str, Exception], None]] = None,
    on_unsupported_store: Optional[Callable[[str], None]] = None,
    on_item_error: Optional[Callable[[str, str, str, Exception], None]] = None,
) -> Tuple[Any, int]:
    """Dispatch grouped note writes across stores while preserving item-level errors."""
    registry = store_registry
    success_count = 0
    for store_name, items in batch.items():
        backend, registry, exc = get_store_backend(
            config,
            store_name,
            store_registry=registry,
            suppress_debug=suppress_debug,
        )
        if backend is None:
            if on_store_error is not None and exc is not None:
                on_store_error(store_name, exc)
            continue
        supports_note_capability = getattr(backend, "supports_note_association", None)
        if supports_note_capability is None:
            supports_note_capability = hasattr(backend, "set_note")
        if not bool(supports_note_capability):
            if on_unsupported_store is not None:
                on_unsupported_store(store_name)
            continue
        if not hasattr(backend, "set_note"):
            if on_unsupported_store is not None:
                on_unsupported_store(store_name)
            continue

        for hash_value, note_name, note_text in items:
            try:
                if backend.set_note(hash_value, note_name, note_text, config=config):
                    success_count += 1
            except Exception as item_exc:
                if on_item_error is not None:
                    on_item_error(store_name, hash_value, note_name, item_exc)

    return registry, success_count


def collect_store_hash_value_batch(
    items: Sequence[Any],
    *,
    store_registry: Any,
    value_resolver: Callable[[Any], Optional[Sequence[str]]],
    override_hash: Optional[str] = None,
    override_store: Optional[str] = None,
    on_warning: Optional[Callable[[str], None]] = None,
) -> Tuple[Dict[str, List[Tuple[str, List[str]]]], List[Any]]:
    """Collect validated store/hash/value batches while preserving passthrough items."""
    from ._tag_utils import normalize_hash
    from ._pipeobject_utils import get_field as _get_field

    batch: Dict[str, List[Tuple[str, List[str]]]] = {}
    pass_through: List[Any] = []

    for item in items:
        pass_through.append(item)

        raw_hash = override_hash or _get_field(item, "hash")
        raw_store = override_store or _get_field(item, "store")
        if not raw_hash or not raw_store:
            if on_warning is not None:
                on_warning("Item missing hash/store; skipping")
            continue

        normalized = normalize_hash(raw_hash)
        if not normalized:
            if on_warning is not None:
                on_warning("Item has invalid hash; skipping")
            continue

        store_text = str(raw_store).strip()
        if not store_text:
            if on_warning is not None:
                on_warning("Item has empty store; skipping")
            continue

        try:
            is_available = bool(store_registry.is_available(store_text))
        except Exception:
            is_available = False
        if not is_available:
            if on_warning is not None:
                on_warning(f"Store '{store_text}' not configured; skipping")
            continue

        values = [str(value).strip() for value in (value_resolver(item) or []) if str(value).strip()]
        if not values:
            continue

        batch.setdefault(store_text, []).append((normalized, values))

    return batch, pass_through

