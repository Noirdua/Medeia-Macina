"""Shared `httpx.Client` helper.

Creating short-lived httpx clients disables connection pooling and costs extra CPU.
This module provides a small keyed client cache for callers that just need basic
GETs without the full HTTPClient wrapper.
"""

from __future__ import annotations

import atexit
from collections import OrderedDict
import threading
from typing import Any, Dict, Optional, Tuple

import httpx

from API.ssl_certs import resolve_verify_value

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)

_lock = threading.Lock()
_MAX_SHARED_CLIENTS = 8
_shared_clients: "OrderedDict[Tuple[float, Tuple[str, str], Tuple[Tuple[str, str], ...]], httpx.Client]" = OrderedDict()


def _normalize_headers(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    normalized: Dict[str, str] = {"User-Agent": _DEFAULT_USER_AGENT}
    if headers:
        normalized.update({str(k): str(v) for k, v in headers.items()})
    return normalized


def _verify_key(verify_value: Any) -> Tuple[str, str]:
    if isinstance(verify_value, bool):
        return ("bool", "1" if verify_value else "0")
    if isinstance(verify_value, str):
        return ("str", verify_value)
    return ("obj", str(id(verify_value)))


def _client_key(
    *,
    timeout: float,
    verify_value: Any,
    merged_headers: Dict[str, str],
    trust_env: bool,
    http2: bool,
) -> Tuple[Any, ...]:
    header_items = tuple(
        sorted((str(k).lower(), str(v)) for k, v in merged_headers.items())
    )
    return (
        float(timeout),
        _verify_key(verify_value),
        header_items,
        bool(trust_env),
        bool(http2),
    )


def get_shared_httpx_client(
    *,
    timeout: float = 30.0,
    verify_ssl: bool | str = True,
    headers: Optional[Dict[str, str]] = None,
    trust_env: bool = True,
    http2: bool = False,
) -> httpx.Client:
    """Return a shared synchronous httpx.Client for a specific config key."""

    verify_value = resolve_verify_value(verify_ssl)
    merged_headers = _normalize_headers(headers)
    key = _client_key(
        timeout=timeout,
        verify_value=verify_value,
        merged_headers=merged_headers,
        trust_env=trust_env,
        http2=http2,
    )

    with _lock:
        existing = _shared_clients.get(key)
        if existing is not None:
            _shared_clients.move_to_end(key)
            return existing

        client = httpx.Client(
            timeout=timeout,
            verify=verify_value,
            headers=merged_headers,
            trust_env=bool(trust_env),
            http2=bool(http2),
        )
        _shared_clients[key] = client

        while len(_shared_clients) > _MAX_SHARED_CLIENTS:
            _, old_client = _shared_clients.popitem(last=False)
            try:
                old_client.close()
            except Exception:
                pass

        return client


def close_shared_httpx_client() -> None:
    with _lock:
        clients = list(_shared_clients.values())
        _shared_clients.clear()
    for client in clients:
        try:
            client.close()
        except Exception:
            pass


atexit.register(close_shared_httpx_client)
