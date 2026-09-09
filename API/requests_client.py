"""Shared `requests` session helper.

Many providers still use `requests` directly. Reusing a Session provides:
- Connection pooling (fewer TCP/TLS handshakes)
- Lower CPU overhead per request

This module intentionally avoids importing the heavy httpx-based stack.
"""

from __future__ import annotations

import atexit
from collections import OrderedDict
import threading
from typing import Any, Dict, Optional, Tuple
from weakref import WeakSet

import requests
from requests.adapters import HTTPAdapter

from API.ssl_certs import resolve_verify_value

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)

_local = threading.local()
_MAX_SESSIONS_PER_THREAD = 4
_session_registry_lock = threading.Lock()
_all_sessions: "WeakSet[requests.Session]" = WeakSet()


def _session_key(
    *,
    user_agent: str,
    verify_ssl: bool,
    pool_connections: int,
    pool_maxsize: int,
) -> Tuple[str, Any, int, int]:
    return (
        str(user_agent or _DEFAULT_USER_AGENT),
        resolve_verify_value(verify_ssl),
        int(pool_connections),
        int(pool_maxsize),
    )


def _get_thread_session_cache() -> "OrderedDict[Tuple[str, Any, int, int], requests.Session]":
    cache = getattr(_local, "session_cache", None)
    if cache is None:
        cache = OrderedDict()
        _local.session_cache = cache
    return cache


def _register_session(session: requests.Session) -> None:
    try:
        with _session_registry_lock:
            _all_sessions.add(session)
    except Exception:
        pass


def get_requests_session(
    *,
    user_agent: str = _DEFAULT_USER_AGENT,
    verify_ssl: bool = True,
    pool_connections: int = 16,
    pool_maxsize: int = 16,
) -> requests.Session:
    """Return a thread-local pooled Session keyed by config values."""

    key = _session_key(
        user_agent=user_agent,
        verify_ssl=verify_ssl,
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
    )
    cache = _get_thread_session_cache()

    existing = cache.get(key)
    if existing is not None:
        cache.move_to_end(key)
        return existing

    session = requests.Session()
    session.headers.update({"User-Agent": key[0]})

    # Expand connection pool; keep max_retries=0 to avoid semantic changes.
    adapter = HTTPAdapter(
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
        max_retries=0,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # Configure verify once.
    session.verify = key[1]
    _register_session(session)

    cache[key] = session
    while len(cache) > _MAX_SESSIONS_PER_THREAD:
        _, old_session = cache.popitem(last=False)
        try:
            old_session.close()
        except Exception:
            pass
    return session


def request(
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = None,
    **kwargs: Any,
) -> requests.Response:
    """Convenience wrapper around the shared Session."""

    sess = get_requests_session()
    return sess.request(method, url, params=params, headers=headers, timeout=timeout, **kwargs)


def close_requests_sessions() -> None:
    """Close cached requests sessions for the current thread and global registry."""

    cache = getattr(_local, "session_cache", None)
    if cache:
        try:
            sessions = list(cache.values())
            cache.clear()
        except Exception:
            sessions = []
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

    try:
        with _session_registry_lock:
            all_sessions = list(_all_sessions)
            _all_sessions.clear()
    except Exception:
        all_sessions = []

    for session in all_sessions:
        try:
            session.close()
        except Exception:
            pass


atexit.register(close_requests_sessions)
