"""
Unified HTTP client for downlow using httpx.

Provides synchronous HTTP operations with:
- Automatic retries on transient failures
- Configurable timeouts and headers
- Built-in progress tracking for downloads
- Request/response logging support
"""

import httpx
import sys
import time
import traceback
import re
import os
import json
from typing import Optional, Dict, Any, Callable, List, Union
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs
import logging

from SYS.logger import debug, debug_panel, is_debug_enabled, log
from SYS.models import DebugLogger, DownloadError, DownloadMediaResult, ProgressBar
from SYS.utils import ensure_directory, sha256_file, sanitize_filename as _sanitize_filename_base, unique_path as _unique_path

try:  # Optional; used for metadata extraction when available
    from SYS.yt_metadata import extract_ytdlp_tags
except Exception:  # pragma: no cover - optional dependency
    extract_ytdlp_tags = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

from API.ssl_certs import resolve_verify_value as _resolve_verify_value
from API.httpx_shared import get_shared_httpx_client

_URL_CREDENTIAL_RE = re.compile(r"://[^@/:]+(:[^@/]+)?@")


def _redact_url(url: str) -> str:
    return _URL_CREDENTIAL_RE.sub(r"://***:***@", str(url or ""))


# Default configuration
DEFAULT_TIMEOUT = 30.0
_CONTENT_DISPOSITION_FILENAME_RE = re.compile(
    r'filename\*?=(?:"([^"]*)"|([^;\s]*))'
)
DEFAULT_RETRIES = 3
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


class HTTPClient:
    """Unified HTTP client with sync support."""

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        user_agent: str = DEFAULT_USER_AGENT,
        verify_ssl: bool = True,
        headers: Optional[Dict[str,
                               str]] = None,
        trust_env: bool = True,
    ):
        """
        Initialize HTTP client.

        Args:
            timeout: Request timeout in seconds
            retries: Number of retries on transient failures
            user_agent: User-Agent header value
            verify_ssl: Whether to verify SSL certificates
            headers: Additional headers to include in all requests
            trust_env: Honor HTTP_PROXY/HTTPS_PROXY/ALL_PROXY from the environment
        """
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self.verify_ssl = verify_ssl
        self.base_headers = headers or {}
        self.trust_env = bool(trust_env)
        self._client: Optional[httpx.Client] = None

        self._httpx_verify = _resolve_verify_value(verify_ssl)

    # Debug helpers
    def _debug_panel(self, title: str, rows: List[tuple[str, Any]]) -> None:
        if not is_debug_enabled():
            return
        debug_panel(title, rows, border_style="bright_blue")

    def __enter__(self):
        """Context manager entry."""
        self._client = get_shared_httpx_client(
            timeout=self.timeout if isinstance(self.timeout, (int, float)) else 30.0,
            verify_ssl=self.verify_ssl,
            headers=self._get_headers(),
            trust_env=self.trust_env,
            http2=False,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit. Shared clients stay pooled."""
        self._client = None

    def _get_headers(self) -> Dict[str, str]:
        """Get request headers with user-agent."""
        headers = {
            "User-Agent": self.user_agent
        }
        headers.update(self.base_headers)
        return headers

    def get(
        self,
        url: str,
        params: Optional[Dict[str,
                              Any]] = None,
        headers: Optional[Dict[str,
                               str]] = None,
        allow_redirects: bool = True,
    ) -> httpx.Response:
        """
        Make a GET request.

        Args:
            url: Request URL
            params: Query parameters
            headers: Additional headers
            allow_redirects: Follow redirects

        Returns:
            httpx.Response object
        """
        return self._request(
            "GET",
            url,
            params=params,
            headers=headers,
            follow_redirects=allow_redirects,
        )

    def post(
        self,
        url: str,
        data: Optional[Any] = None,
        json: Optional[Dict] = None,
        files: Optional[Dict] = None,
        headers: Optional[Dict[str,
                               str]] = None,
    ) -> httpx.Response:
        """
        Make a POST request.

        Args:
            url: Request URL
            data: Form data
            json: JSON data
            files: Files to upload
            headers: Additional headers

        Returns:
            httpx.Response object
        """
        return self._request(
            "POST",
            url,
            data=data,
            json=json,
            files=files,
            headers=headers,
        )

    def put(
        self,
        url: str,
        data: Optional[Any] = None,
        json: Optional[Dict] = None,
        content: Optional[Any] = None,
        files: Optional[Dict] = None,
        headers: Optional[Dict[str,
                               str]] = None,
    ) -> httpx.Response:
        """
        Make a PUT request.

        Args:
            url: Request URL
            data: Form data
            json: JSON data
            content: Raw content
            files: Files to upload
            headers: Additional headers

        Returns:
            httpx.Response object
        """
        return self._request(
            "PUT",
            url,
            data=data,
            json=json,
            content=content,
            files=files,
            headers=headers,
        )

    def delete(
        self,
        url: str,
        headers: Optional[Dict[str,
                               str]] = None,
    ) -> httpx.Response:
        """
        Make a DELETE request.

        Args:
            url: Request URL
            headers: Additional headers

        Returns:
            httpx.Response object
        """
        return self._request(
            "DELETE",
            url,
            headers=headers,
        )

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """
        Make a generic HTTP request.

        Args:
            method: HTTP method
            url: Request URL
            **kwargs: Additional arguments

        Returns:
            httpx.Response object
        """
        return self._request(method, url, **kwargs)

    def download(
        self,
        url: str,
        file_path: str,
        chunk_size: int = 262144,
        progress_callback: Optional[Callable[[int,
                                              int],
                                             None]] = None,
        headers: Optional[Dict[str,
                               str]] = None,
    ) -> Path:
        """
        Download a file from URL with optional progress tracking.

        Args:
            url: File URL
            file_path: Local file path to save to
            chunk_size: Download chunk size
            progress_callback: Callback(bytes_downloaded, total_bytes)
            headers: Additional headers

        Returns:
            Path object of downloaded file
        """
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        extra = dict(headers or {})
        attempts = max(1, int(self.retries or 1))
        last_exception: Exception | None = None
        for attempt in range(attempts):
            existing = path.stat().st_size if path.exists() else 0
            req_headers = dict(extra)
            if existing > 0:
                req_headers["Range"] = f"bytes={existing}-"
            try:
                with self._request_stream(
                    "GET",
                    url,
                    headers=req_headers or None,
                    follow_redirects=True,
                ) as response:
                    if response.status_code == 416:
                        return path
                    response.raise_for_status()
                    resumed = response.status_code == 206 and existing > 0
                    if response.status_code == 200 and existing:
                        existing = 0
                    mode = "ab" if resumed else "wb"
                    content_len = int(response.headers.get("content-length", 0) or 0)
                    total_bytes = (existing + content_len) if resumed else content_len
                    bytes_downloaded = existing
                    if progress_callback:
                        try:
                            progress_callback(bytes_downloaded, total_bytes)
                        except Exception:
                            logger.exception("Error in progress_callback initial call")
                    with open(path, mode) as handle:
                        for chunk in response.iter_bytes(max(65536, int(chunk_size or 262144))):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            bytes_downloaded += len(chunk)
                            if progress_callback:
                                progress_callback(bytes_downloaded, total_bytes)
                    if progress_callback:
                        try:
                            progress_callback(bytes_downloaded, total_bytes)
                        except Exception:
                            logger.exception("Error in progress_callback final call")
                    return path
            except Exception as exc:
                last_exception = exc
                if attempt >= attempts - 1:
                    break
                time.sleep(min(2 ** attempt, 8))
        if last_exception:
            raise last_exception
        raise RuntimeError("Download failed after retries")

    def _request(
        self,
        method: str,
        url: str,
        raise_for_status: bool = True,
        log_http_errors: bool = True,
        **kwargs,
    ) -> httpx.Response:
        """
        Make an HTTP request with automatic retries.

        Args:
            method: HTTP method
            url: Request URL
            **kwargs: Additional arguments for httpx.Client.request()

        Returns:
            httpx.Response object
        """
        if not self._client:
            raise RuntimeError(
                "HTTPClient must be used with context manager (with statement)"
            )

        # Merge headers once per call (do not rebuild for every retry attempt).
        merged_headers = self._get_headers()
        extra_headers = kwargs.get("headers")
        if extra_headers:
            try:
                merged_headers.update(extra_headers)
            except Exception:
                # If headers is not a mapping, keep it as-is and let httpx raise.
                merged_headers = extra_headers
        kwargs["headers"] = merged_headers

        last_exception: Exception | None = None

        def _raw_debug_enabled() -> bool:
            try:
                val = str(os.environ.get("MM_HTTP_RAW", "")).strip().lower()
                if val in {"0", "false", "no", "off"}:
                    return False
                return is_debug_enabled()
            except Exception:
                return is_debug_enabled()

        def _redact_mapping(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            secret_keys = {
                "authorization",
                "apikey",
                "api_key",
                "access_key",
                "token",
                "password",
                "secret",
                "cookie",
            }
            out = {}
            for key, item in value.items():
                name = str(key or "").strip().lower()
                if name in secret_keys or "key" in name or "token" in name or "secret" in name:
                    out[key] = "***"
                else:
                    out[key] = item
            return out

        def _preview(value: Any, *, limit: int = 2000) -> str:
            if value is None:
                return "<not provided>"
            try:
                # File-like objects (uploads/streams) - show compact summary instead of raw contents
                if hasattr(value, "read") or hasattr(value, "fileno"):
                    name = getattr(value, "name", None)
                    total = getattr(value, "_total", None)
                    label = getattr(value, "_label", None)
                    try:
                        pos = value.tell() if hasattr(value, "tell") else None
                    except Exception:
                        pos = None
                    parts = []
                    if name is not None:
                        parts.append(f"name={name!r}")
                    if total is not None:
                        parts.append(f"total={total}")
                    if label is not None:
                        parts.append(f"label={label!r}")
                    if pos is not None:
                        parts.append(f"pos={pos}")
                    summary = " ".join(parts) if parts else None
                    return f"<file-like {summary}>" if summary else "<file-like>"

                if isinstance(value, (dict, list, tuple)):
                    # Use default=str to avoid failing on non-serializable objects (e.g., file-like)
                    text = json.dumps(value, ensure_ascii=False, default=str)
                elif isinstance(value, (bytes, bytearray)):
                    text = value.decode("utf-8", errors="replace")
                else:
                    text = str(value)
            except Exception:
                try:
                    text = repr(value)
                except Exception:
                    text = "<unprintable>"
            if len(text) > limit:
                return text[:limit] + "..."
            return text

        for attempt in range(self.retries):
            self._debug_panel(
                "HTTP request",
                [
                    ("method", method),
                    ("url", _redact_url(url)),
                    ("attempt", f"{attempt + 1}/{self.retries}"),
                    ("params", _redact_mapping(kwargs.get("params"))),
                    ("headers", _redact_mapping(kwargs.get("headers"))),
                    ("verify", self._httpx_verify),
                    ("follow_redirects", kwargs.get("follow_redirects", False)),
                ],
            )
            if _raw_debug_enabled():
                self._debug_panel(
                    "HTTP request raw",
                    [
                        ("params", _preview(_redact_mapping(kwargs.get("params")))),
                        ("data", _preview(_redact_mapping(kwargs.get("data")))),
                        ("json", _preview(_redact_mapping(kwargs.get("json")))),
                        ("content", _preview(kwargs.get("content"))),
                        ("files", _preview(kwargs.get("files"))),
                    ],
                )
            try:
                response = self._client.request(method, url, **kwargs)
                self._debug_panel(
                    "HTTP response",
                    [
                        ("method", method),
                        ("url", _redact_url(url)),
                        ("status", getattr(response, "status_code", "")),
                        ("elapsed", getattr(response, "elapsed", "")),
                        (
                            "content_length",
                            response.headers.get("content-length") if hasattr(response, "headers") else "",
                        ),
                    ],
                )
                if _raw_debug_enabled():
                    content_type = ""
                    try:
                        content_type = response.headers.get("content-type", "")
                    except Exception:
                        content_type = ""
                    body_preview = ""
                    try:
                        if isinstance(content_type, str) and (
                            content_type.startswith("application/json")
                            or content_type.startswith("text/")
                        ):
                            body_preview = _preview(response.text, limit=4000)
                        else:
                            raw = response.content
                            if raw is None:
                                body_preview = "<no content>"
                            else:
                                body_preview = raw[:400].hex()
                    except Exception:
                        body_preview = "<unavailable>"
                    self._debug_panel(
                        "HTTP response raw",
                        [
                            ("content_type", content_type),
                            ("body_preview", body_preview),
                            ("body_length", len(response.content) if response is not None else ""),
                        ],
                    )
                if raise_for_status:
                    response.raise_for_status()
                return response
            except httpx.TimeoutException as e:
                last_exception = e
                if attempt < self.retries - 1:
                    continue
            except httpx.HTTPStatusError as e:
                # Don't retry on 4xx errors
                if 400 <= e.response.status_code < 500:
                    try:
                        response_text = e.response.text[:500]
                    except Exception:
                        response_text = "<unable to read response>"
                    if log_http_errors:
                        logger.error(
                            f"HTTP {e.response.status_code} from {_redact_url(url)}: {response_text}"
                        )
                    raise
                last_exception = e
                try:
                    response_text = e.response.text[:200]
                except Exception:
                    response_text = "<unable to read response>"
                logger.warning(
                    f"HTTP {e.response.status_code} on attempt {attempt + 1}/{self.retries}: {_redact_url(url)} - {response_text}"
                )
                if attempt < self.retries - 1:
                    continue
            except (httpx.RequestError, httpx.ConnectError) as e:
                last_exception = e
                logger.warning(
                    f"Connection error on attempt {attempt + 1}/{self.retries}: {_redact_url(url)} - {e}"
                )

                # Detect certificate verification failures in the underlying error
                msg = str(e or "").lower()
                if ("certificate verify failed" in msg or "unable to get local issuer certificate" in msg):
                    logger.info("Certificate verification failed; attempting to retry with a system-aware CA bundle")
                    try:
                        temp_client = get_shared_httpx_client(
                            timeout=self.timeout,
                            verify_ssl=self._httpx_verify,
                            headers=self._get_headers(),
                        )
                        try:
                            response = temp_client.request(method, url, **kwargs)
                            if raise_for_status:
                                response.raise_for_status()
                            return response
                        except Exception as e2:
                            last_exception = e2
                    except Exception:
                        # certifi/pip-system-certs/httpx not available; fall back to existing retry behavior
                        pass

                if attempt < self.retries - 1:
                    continue
            except Exception as e:
                # Catch-all to handle non-httpx exceptions that may represent
                # certificate verification failures from underlying transports.
                last_exception = e
                logger.warning(f"Request exception on attempt {attempt + 1}/{self.retries}: {_redact_url(url)} - {e}")
                msg = str(e or "").lower()
                if ("certificate verify failed" in msg or "unable to get local issuer certificate" in msg):
                    logger.info("Certificate verification failed; attempting to retry with a system-aware CA bundle")
                    try:
                        temp_client = get_shared_httpx_client(
                            timeout=self.timeout,
                            verify_ssl=self._httpx_verify,
                            headers=self._get_headers(),
                        )
                        try:
                            response = temp_client.request(method, url, **kwargs)
                            if raise_for_status:
                                response.raise_for_status()
                            return response
                        except Exception as e2:
                            last_exception = e2
                    except Exception:
                        # certifi/pip-system-certs/httpx not available; fall back to existing retry behavior
                        pass

                if attempt < self.retries - 1:
                    continue

        if last_exception:
            raise last_exception

        raise RuntimeError("Request failed after retries")

    def _request_stream(self, method: str, url: str, **kwargs):
        """Make a streaming request."""
        if not self._client:
            raise RuntimeError(
                "HTTPClient must be used with context manager (with statement)"
            )

        # Merge headers
        if "headers" in kwargs and kwargs["headers"]:
            headers = self._get_headers()
            headers.update(kwargs["headers"])
            kwargs["headers"] = headers
        else:
            kwargs["headers"] = self._get_headers()

        self._debug_panel(
            "HTTP stream",
            [
                ("method", method),
                ("url", _redact_url(url)),
                ("headers", kwargs.get("headers")),
                ("follow_redirects", kwargs.get("follow_redirects", False)),
            ],
        )

        return self._client.stream(method, url, **kwargs)


def _rewrite_known_hoster_url(url: str) -> str:
    """Rewrite viewer-page URLs of known file hosters to their direct-download form."""
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").strip().lower()
        path = str(parsed.path or "")
        if host in {"pixeldrain.com", "www.pixeldrain.com"}:
            match = re.match(r"^/(u|l)/([A-Za-z0-9]+)$", path)
            if match:
                kind, file_id = match.group(1), match.group(2)
                if kind == "u":
                    return f"https://pixeldrain.com/api/file/{file_id}"
                return f"https://pixeldrain.com/api/list/{file_id}/zip"
        if host in {"catbox.moe", "www.catbox.moe"} and path.startswith("/c/"):
            file_id = path[len("/c/"):]
            if file_id:
                return f"https://files.catbox.moe/{file_id}"
    except Exception:
        pass
    return str(url or "")


def download_direct_file(
    url: str,
    output_dir: Path,
    debug_logger: Optional[DebugLogger] = None,
    quiet: bool = False,
    suggested_filename: Optional[str] = None,
    pipeline_progress: Optional[Any] = None,
) -> DownloadMediaResult:
    """Download a direct file (PDF, image, document, etc.) with guardrails and metadata hooks."""

    ensure_directory(output_dir)
    url = _rewrite_known_hoster_url(url)

    def _sanitize_filename(name: str) -> str:
        text = str(name or "").strip()
        if not text:
            return ""
        text = text.replace("\\", "/").split("/")[-1]
        return _sanitize_filename_base(text, fallback="")

    parsed_url = urlparse(url)
    url_path = parsed_url.path

    filename: Optional[str] = None
    if parsed_url.query:
        query_params = parse_qs(parsed_url.query)
        for param_name in ("filename", "download", "file", "name"):
            if param_name in query_params and query_params[param_name]:
                filename = query_params[param_name][0]
                filename = unquote(filename)
                break

    if not filename or not filename.strip():
        filename = url_path.split("/")[-1] if url_path else ""
        filename = unquote(filename)

    if "?" in filename:
        filename = filename.split("?")[0]

    content_type = ""
    try:
        with HTTPClient(timeout=10.0) as client:
            response = client._request("HEAD", url, follow_redirects=True)
            content_disposition = response.headers.get("content-disposition", "")
            try:
                content_type = str(response.headers.get("content-type", "") or "").strip().lower()
            except Exception:
                content_type = ""

            if content_disposition:
                match = _CONTENT_DISPOSITION_FILENAME_RE.search(content_disposition)
                if match:
                    extracted_name = match.group(1) or match.group(2)
                    if extracted_name:
                        filename = unquote(extracted_name)
    except Exception as exc:
        if not quiet:
            log(f"Could not get filename from headers: {exc}", file=sys.stderr)

    try:
        page_like_exts = {".php", ".asp", ".aspx", ".jsp", ".cgi"}
        ext = ""
        try:
            ext = Path(str(filename or "")).suffix.lower()
        except Exception:
            ext = ""

        ct0 = (content_type or "").split(";", 1)[0].strip().lower()
        # Probe when the content-type is unknown too, so HTML viewer pages are
        # refused instead of being saved as files.
        must_probe = bool(ct0.startswith("text/html") or ext in page_like_exts or not ct0)

        if must_probe:
            with HTTPClient(timeout=10.0) as client:
                with client._request_stream("GET", url, follow_redirects=True) as resp:
                    resp.raise_for_status()
                    ct = (
                        str(resp.headers.get("content-type", "") or "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if ct.startswith("text/html"):
                        raise DownloadError("URL appears to be an HTML page, not a direct file")
    except DownloadError:
        raise
    except Exception:
        logger.exception("Unexpected error while probing URL content")

    suggested = _sanitize_filename(suggested_filename) if suggested_filename else ""
    if suggested:
        suggested_path = Path(suggested)
        if suggested_path.suffix:
            filename = suggested
        else:
            detected_ext = ""
            try:
                detected_ext = Path(str(filename)).suffix
            except Exception:
                logger.exception("Failed to detect file extension from filename")
                detected_ext = ""
            filename = suggested + detected_ext if detected_ext else suggested

    try:
        has_ext = bool(filename and Path(str(filename)).suffix)
    except Exception:
        logger.exception("Failed to determine if filename has extension")
        has_ext = False

    if filename and (not has_ext):
        ct = (content_type or "").split(";", 1)[0].strip().lower()
        ext_by_ct = {
            "application/pdf": ".pdf",
            "application/epub+zip": ".epub",
            "application/x-mobipocket-ebook": ".mobi",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "text/plain": ".txt",
            "application/zip": ".zip",
        }

        if ct in ext_by_ct:
            filename = f"{filename}{ext_by_ct[ct]}"
        elif ct.startswith("text/html"):
            raise DownloadError("URL appears to be an HTML page, not a direct file")

    if not filename or not str(filename).strip():
        raise DownloadError(
            "Could not determine filename for URL (no Content-Disposition and no path filename)"
        )

    file_path = _unique_path(output_dir / str(filename))

    use_pipeline_transfer = False
    try:
        if pipeline_progress is not None and hasattr(pipeline_progress, "update_transfer"):
            ui = None
            if hasattr(pipeline_progress, "ui_and_pipe_index"):
                ui, _ = pipeline_progress.ui_and_pipe_index()  # type: ignore[attr-defined]
            use_pipeline_transfer = ui is not None
    except Exception:
        use_pipeline_transfer = False

    progress_bar: Optional[ProgressBar] = None
    if (not quiet) and (not use_pipeline_transfer):
        progress_bar = ProgressBar()

    transfer_started = [False]

    try:
        downloaded_bytes = [0]
        transfer_started[0] = False

        def _maybe_begin_transfer(content_length: int) -> None:
            if pipeline_progress is None or transfer_started[0]:
                return
            try:
                total_val: Optional[int] = (
                    int(content_length)
                    if isinstance(content_length, int) and content_length > 0
                    else None
                )
            except Exception:
                total_val = None
            try:
                if hasattr(pipeline_progress, "begin_transfer"):
                    pipeline_progress.begin_transfer(
                        label=str(filename or "download"),
                        total=total_val,
                    )
                    transfer_started[0] = True
            except Exception:
                return

        def progress_callback(bytes_downloaded: int, content_length: int) -> None:
            downloaded_bytes[0] = int(bytes_downloaded or 0)

            try:
                if pipeline_progress is not None and hasattr(pipeline_progress, "update_transfer"):
                    _maybe_begin_transfer(content_length)
                    total_val: Optional[int] = (
                        int(content_length)
                        if isinstance(content_length, int) and content_length > 0
                        else None
                    )
                    pipeline_progress.update_transfer(
                        label=str(filename or "download"),
                        completed=int(bytes_downloaded or 0),
                        total=total_val,
                    )
            except Exception:
                logger.exception("Error updating pipeline progress transfer")

            if progress_bar is not None:
                progress_bar.update(
                    downloaded=int(bytes_downloaded or 0),
                    total=int(content_length) if content_length and content_length > 0 else None,
                    label=str(filename or "download"),
                    file=sys.stderr,
                )

        with HTTPClient(timeout=30.0) as client:
            client.download(url, str(file_path), progress_callback=progress_callback)

        try:
            if progress_bar is not None:
                progress_bar.finish()
        except Exception:
            logger.exception("Failed to finish progress bar")

        try:
            if pipeline_progress is not None and transfer_started[0] and hasattr(
                pipeline_progress, "finish_transfer"
            ):
                pipeline_progress.finish_transfer(label=str(filename or "download"))
        except Exception:
            logger.exception("Failed to finish pipeline transfer")

        ext_out = ""
        try:
            ext_out = Path(str(filename)).suffix.lstrip(".")
        except Exception:
            ext_out = ""

        info: Dict[str, Any] = {
            "id": str(filename).rsplit(".", 1)[0] if "." in str(filename) else str(filename),
            "ext": ext_out,
            "webpage_url": url,
        }

        hash_value = None
        try:
            hash_value = sha256_file(file_path)
        except Exception:
            logger.exception("Failed to compute SHA256 of downloaded file")

        tags: List[str] = []
        if extract_ytdlp_tags is not None:
            try:
                tags = extract_ytdlp_tags(info)
            except Exception as exc:
                log(f"Error extracting tags: {exc}", file=sys.stderr)

        if not any(str(t).startswith("title:") for t in tags):
            info["title"] = str(filename)
            tags = []
            if extract_ytdlp_tags is not None:
                try:
                    tags = extract_ytdlp_tags(info)
                except Exception as exc:
                    log(f"Error extracting tags with filename: {exc}", file=sys.stderr)

        if debug_logger is not None:
            debug_logger.write_record(
                "direct-file-downloaded",
                {"url": url, "path": str(file_path), "hash": hash_value},
            )

        return DownloadMediaResult(
            path=file_path,
            info=info,
            tag=tags,
            source_url=url,
            hash_value=hash_value,
        )

    except (httpx.HTTPError, httpx.RequestError) as exc:
        try:
            if progress_bar is not None:
                progress_bar.finish()
        except Exception:
            logger.exception("Failed to finish progress bar during HTTP error handling")
        try:
            if pipeline_progress is not None and transfer_started[0] and hasattr(
                pipeline_progress, "finish_transfer"
            ):
                pipeline_progress.finish_transfer(label=str(filename or "download"))
        except Exception:
            logger.exception("Failed to finish pipeline transfer during HTTP error handling")

        log(f"Download error: {exc}", file=sys.stderr)
        if debug_logger is not None:
            debug_logger.write_record(
                "exception",
                {"phase": "direct-file", "url": url, "error": str(exc)},
            )
        raise DownloadError(f"Failed to download {url}: {exc}") from exc

    except Exception as exc:
        try:
            if progress_bar is not None:
                progress_bar.finish()
        except Exception:
            logger.exception("Failed to finish progress bar during error handling")
        try:
            if pipeline_progress is not None and transfer_started[0] and hasattr(
                pipeline_progress, "finish_transfer"
            ):
                pipeline_progress.finish_transfer(label=str(filename or "download"))
        except Exception:
            logger.exception("Failed to finish pipeline transfer during error handling")

        log(f"Error downloading file: {exc}", file=sys.stderr)
        if debug_logger is not None:
            debug_logger.write_record(
                "exception",
                {
                    "phase": "direct-file",
                    "url": url,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
        raise DownloadError(f"Error downloading file: {exc}") from exc
