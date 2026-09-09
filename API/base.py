from __future__ import annotations

from typing import Any, Dict, Optional
import atexit
import threading

from .HTTP import HTTPClient


class ApiError(Exception):
    """Base exception for API errors."""
    pass


class API:
    """Base class for API clients using the internal HTTPClient."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = float(timeout)
        self._http_client: Optional[HTTPClient] = None
        self._http_client_lock = threading.Lock()
        atexit.register(self.close)

    def _get_http_client(self) -> HTTPClient:
        """Return a reusable opened HTTP client for this API instance."""
        client = self._http_client
        if client is not None and getattr(client, "_client", None) is not None:
            return client

        with self._http_client_lock:
            client = self._http_client
            if client is None:
                client = HTTPClient(timeout=self.timeout)
                self._http_client = client
            if getattr(client, "_client", None) is None:
                client.__enter__()
            return client

    def close(self) -> None:
        client = self._http_client
        if client is None:
            return
        with self._http_client_lock:
            current = self._http_client
            if current is None:
                return
            try:
                current.__exit__(None, None, None)
            except Exception:
                pass
            self._http_client = None

    def _get_json(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/{str(path or '').lstrip('/')}"
        try:
            client = self._get_http_client()
            response = client.get(url, params=params, headers=headers, allow_redirects=True)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise ApiError(f"API request failed for {url}: {exc}") from exc

    def _post_json(
        self,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/{str(path or '').lstrip('/')}"
        try:
            client = self._get_http_client()
            response = client.request(
                "POST",
                url,
                json=json_data,
                params=params,
                headers=headers,
                follow_redirects=True,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise ApiError(f"API request failed for {url}: {exc}") from exc
