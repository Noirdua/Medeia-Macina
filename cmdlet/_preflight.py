"""Store backend resolution and preflight validation helpers.

Resolves store backends for cmdlets, preferring targeted instances before
falling back to the backend registry. Used by store-related cmdlets to avoid
repeated registry setup boilerplate.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

__all__ = [
    "get_store_backend",
    "get_preferred_store_backend",
]


def get_store_backend(
    config: Optional[Dict[str, Any]],
    store_name: Optional[str],
    *,
    store_registry: Any = None,
    suppress_debug: bool = False,
) -> Tuple[Optional[Any], Any, Optional[Exception]]:
    """Resolve a store backend, optionally reusing an existing registry.

    Returns a tuple of ``(backend, store_registry, exc)`` so callers can keep
    their command-specific error messages while avoiding repeated registry setup
    and ``store[name]`` boilerplate.
    """
    registry = store_registry
    if registry is None:
        try:
            from PluginCore.backend_registry import BackendRegistry

            registry = BackendRegistry(config or {}, suppress_debug=suppress_debug)
        except Exception as exc:
            return None, None, exc

    backend_name = str(store_name or "").strip()
    if not backend_name:
        return None, registry, KeyError("Missing store name")

    try:
        return registry[backend_name], registry, None
    except Exception as exc:
        return None, registry, exc


def get_preferred_store_backend(
    config: Optional[Dict[str, Any]],
    store_name: Optional[str],
    *,
    store_registry: Any = None,
    suppress_debug: bool = True,
) -> Tuple[Optional[Any], Any, Optional[Exception]]:
    """Prefer a targeted backend instance before falling back to registry lookup."""
    direct_exc: Optional[Exception] = None
    try:
        from PluginCore.backend_registry import get_backend_instance

        backend = get_backend_instance(
            config or {},
            str(store_name or ""),
            suppress_debug=suppress_debug,
        )
        if backend is not None:
            return backend, store_registry, None
    except Exception as exc:
        direct_exc = exc

    backend, registry, lookup_exc = get_store_backend(
        config,
        store_name,
        store_registry=store_registry,
        suppress_debug=suppress_debug,
    )
    if backend is not None:
        return backend, registry, None
    return None, registry, direct_exc or lookup_exc
