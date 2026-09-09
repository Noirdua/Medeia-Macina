"""Search engine result parsing — Bing, DuckDuckGo, Yahoo, and site crawling.

Extracted from search.py to keep the cmdlet class focused on orchestration.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence
from collections import deque
from pathlib import Path
import re
import time
import html as _html
from urllib.parse import urlparse, parse_qs, unquote, urljoin

from SYS.logger import debug
from SYS.payload_builders import normalize_file_extension

_WHITESPACE_RE = re.compile(r"\s+")
_SITE_TOKEN_RE = re.compile(r"(?:^|\s)site:([^\s,]+)", flags=re.IGNORECASE)
_FILETYPE_TOKEN_RE = re.compile(
    r"(?:^|\s)(?:ext|filetype):\.?([a-z0-9]{1,12})\b",
    flags=re.IGNORECASE,
)
_TYPE_TOKEN_RE = re.compile(
    r"(?:^|\s)type:\.?([a-z0-9]{1,12})\b",
    flags=re.IGNORECASE,
)
_SITE_REMOVE_RE = re.compile(r"(?:^|\s)site:[^\s,]+", flags=re.IGNORECASE)
_FILETYPE_REMOVE_RE = re.compile(
    r"(?:^|\s)(?:ext|filetype):\.?[a-z0-9]{1,12}\b",
    flags=re.IGNORECASE,
)
_TYPE_REMOVE_RE = re.compile(
    r"(?:^|\s)type:\.?[a-z0-9]{1,12}\b",
    flags=re.IGNORECASE,
)
_SCHEME_PREFIX_RE = re.compile(r"^[a-z]+:")
_PAGE_URL_RE = re.compile(r"^https?://", flags=re.IGNORECASE)
_SRCSET_URL_RE = re.compile(r"([^\s,]+)\s*(?:\d+(?:\.\d+)?[wx])?", flags=re.IGNORECASE)
_YAHOO_RU_RE = re.compile(r"/RU=([^/]+)/RK=", flags=re.IGNORECASE)
_SCRAPE_SKIP_EXT = {
    "html", "htm", "php", "asp", "aspx", "jsp", "shtml", "xhtml",
    "js", "mjs", "css", "map",
    "woff", "woff2", "ttf", "otf", "eot",
}
_SCRAPE_TYPE_GROUPS: Dict[str, set[str]] = {
    "image": {"jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "ico", "avif", "tif", "tiff", "jxl", "heic"},
    "pdf": {"pdf"},
    "document": {"doc", "docx", "odt", "rtf", "txt", "epub", "mobi", "azw3", "djvu", "ppt", "pptx", "xls", "xlsx", "odp", "ods"},
    "audio": {"mp3", "flac", "wav", "ogg", "m4a", "aac", "opus", "wma", "aiff"},
    "video": {"mp4", "webm", "mkv", "avi", "mov", "m4v", "ogv"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "tgz"},
    "data": {"json", "xml", "csv", "tsv", "yaml", "yml"},
}
_SCRAPE_TYPE_ALIASES = {
    "image": "image",
    "images": "image",
    "img": "image",
    "picture": "image",
    "pictures": "image",
    "pic": "image",
    "pics": "image",
    "pdf": "pdf",
    "pdfs": "pdf",
    "document": "document",
    "documents": "document",
    "doc": "document",
    "docs": "document",
    "audio": "audio",
    "music": "audio",
    "video": "video",
    "videos": "video",
    "archive": "archive",
    "archives": "archive",
    "data": "data",
    "other": "other",
}
_SCRAPE_TYPE_LABELS = {
    "image": "Images",
    "pdf": "PDFs",
    "document": "Documents",
    "audio": "Audio",
    "video": "Video",
    "archive": "Archives",
    "data": "Data",
    "other": "Other",
}
_SCRAPE_TYPE_ORDER = ("image", "pdf", "document", "audio", "video", "archive", "data", "other")
_EXT_TO_SCRAPE_TYPE: Dict[str, str] = {
    ext: group
    for group, extensions in _SCRAPE_TYPE_GROUPS.items()
    for ext in extensions
}
_SCRAPE_MAX_ASSETS = 500
_SCRAPE_MAX_BYTES = 2_500_000
_SCRAPE_CACHE_TTL = 30.0
_SCRAPE_CACHE: Dict[str, tuple[float, List[Dict[str, str]]]] = {}
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_DDG_RESULT_ANCHOR_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    flags=re.IGNORECASE | re.DOTALL,
)
_GENERIC_ANCHOR_RE = re.compile(
    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    flags=re.IGNORECASE | re.DOTALL,
)
_BING_RESULT_ANCHOR_RE = re.compile(
    r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    flags=re.IGNORECASE | re.DOTALL,
)


def _normalize_extension(ext_value: Any) -> str:
    """Sanitize extension strings to alphanumerics and cap at 5 chars."""
    return normalize_file_extension(ext_value)


def _normalize_host(value: Any) -> str:
    """Normalize host names for matching/filtering."""
    host = str(value or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    if ":" in host:
        host = host.split(":", 1)[0]
    return host


def _host_is_blocked(host: Any) -> bool:
    """Reject loopback, private, link-local, reserved, and unspecified hosts."""
    import ipaddress

    host_text = str(host or "").strip().lower().strip("[]")
    if not host_text:
        return True
    try:
        ip = ipaddress.ip_address(host_text)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _normalize_space(text: Any) -> str:
    return _WHITESPACE_RE.sub(" ", str(text or "")).strip()


def _url_matches_site(cls, url: str, site_host: str) -> bool:
    """Return True when URL host is the requested site/subdomain."""
    try:
        parsed = urlparse(str(url or ""))
        host = _normalize_host(getattr(parsed, "hostname", "") or "")
    except Exception:
        return False

    target = _normalize_host(site_host)
    if not host or not target:
        return False
    return host == target or host.endswith(f".{target}")


def _itertext_join(node: Any) -> str:
    try:
        return " ".join([str(text).strip() for text in node.itertext() if str(text).strip()])
    except Exception:
        return ""


def _html_fragment_to_text(fragment: Any) -> str:
    text = _HTML_TAG_RE.sub(" ", str(fragment or ""))
    return _html.unescape(text)


def _append_web_result(
    cls,
    items: List[Dict[str, str]],
    seen_urls: set[str],
    *,
    site_host: str,
    url_text: str,
    title_text: str,
    snippet_text: str,
) -> None:
    url_clean = str(url_text or "").strip()
    if not url_clean or not url_clean.startswith(("http://", "https://")):
        return
    if not _url_matches_site(url_clean, site_host):
        return
    if url_clean in seen_urls:
        return

    seen_urls.add(url_clean)
    items.append(
        {
            "url": url_clean,
            "title": _normalize_space(title_text) or url_clean,
            "snippet": _normalize_space(snippet_text),
        }
    )


def _parse_web_results_with_fallback(
    cls,
    *,
    html_text: str,
    limit: int,
    lxml_parser: Any,
    regex_parser: Any,
    fallback_when_empty: bool = False,
) -> List[Dict[str, str]]:
    """Run an lxml-based parser with an optional regex fallback."""
    items: List[Dict[str, str]] = []
    seen_urls: set[str] = set()
    should_run_regex = False

    try:
        from lxml import html as lxml_html

        doc = lxml_html.fromstring(html_text or "")
        lxml_parser(doc, items, seen_urls)
        should_run_regex = fallback_when_empty and not items
    except Exception:
        should_run_regex = True

    if should_run_regex:
        regex_parser(html_text or "", items, seen_urls)

    return items[:limit]


def _extract_duckduckgo_target_url(href: Any) -> str:
    """Extract direct target URL from DuckDuckGo result links."""
    raw_href = str(href or "").strip()
    if not raw_href:
        return ""

    if raw_href.startswith("//"):
        raw_href = f"https:{raw_href}"

    if raw_href.startswith("/"):
        raw_href = f"https://duckduckgo.com{raw_href}"

    parsed = None
    try:
        parsed = urlparse(raw_href)
    except Exception:
        parsed = None

    try:
        host = str(getattr(parsed, "hostname", "") or "").strip().lower()
    except Exception:
        host = ""

    if host.endswith("duckduckgo.com"):
        try:
            query = parse_qs(str(getattr(parsed, "query", "") or ""))
            candidate = (query.get("uddg") or [""])[0]
            if candidate:
                return str(unquote(candidate)).strip()
        except Exception:
            pass

    return raw_href


def _extract_yahoo_target_url(href: Any) -> str:
    """Extract direct target URL from Yahoo redirect links."""
    raw_href = str(href or "").strip()
    if not raw_href:
        return ""

    ru_match = _YAHOO_RU_RE.search(raw_href)
    if ru_match:
        try:
            return str(unquote(ru_match.group(1))).strip()
        except Exception:
            pass

    try:
        parsed = urlparse(raw_href)
        query = parse_qs(str(getattr(parsed, "query", "") or ""))
        candidate = (query.get("RU") or query.get("ru") or [""])[0]
        if candidate:
            return str(unquote(candidate)).strip()
    except Exception:
        pass

    return raw_href


def parse_duckduckgo_results(
    cls,
    *,
    html_text: str,
    site_host: str,
    limit: int,
) -> List[Dict[str, str]]:
    """Parse DuckDuckGo HTML results into normalized rows."""
    def _parse_lxml(doc: Any, items: List[Dict[str, str]], seen_urls: set[str]) -> None:
        result_nodes = doc.xpath("//div[contains(@class, 'result')]")

        for node in result_nodes:
            links = node.xpath(".//a[contains(@class, 'result__a')]")
            if not links:
                continue

            link = links[0]
            href = _extract_duckduckgo_target_url(link.get("href"))
            title = _itertext_join(link)

            snippet_nodes = node.xpath(".//*[contains(@class, 'result__snippet')]")
            snippet = ""
            if snippet_nodes:
                snippet = _itertext_join(snippet_nodes[0])

            _append_web_result(
                cls,
                items,
                seen_urls,
                site_host=site_host,
                url_text=href,
                title_text=title,
                snippet_text=snippet,
            )
            if len(items) >= limit:
                break

    def _parse_regex(raw_html: str, items: List[Dict[str, str]], seen_urls: set[str]) -> None:
        for match in _DDG_RESULT_ANCHOR_RE.finditer(raw_html):
            href = _extract_duckduckgo_target_url(match.group(1))
            title_html = match.group(2)
            title = _html_fragment_to_text(title_html)
            _append_web_result(
                cls,
                items,
                seen_urls,
                site_host=site_host,
                url_text=href,
                title_text=title,
                snippet_text="",
            )
            if len(items) >= limit:
                break

    return _parse_web_results_with_fallback(
        cls,
        html_text=html_text,
        limit=limit,
        lxml_parser=_parse_lxml,
        regex_parser=_parse_regex,
        fallback_when_empty=True,
    )


def parse_yahoo_results(
    cls,
    *,
    html_text: str,
    site_host: str,
    limit: int,
) -> List[Dict[str, str]]:
    """Parse Yahoo HTML search results into normalized rows."""
    def _parse_lxml(doc: Any, items: List[Dict[str, str]], seen_urls: set[str]) -> None:
        for node in doc.xpath("//a[@href]"):
            href = _extract_yahoo_target_url(node.get("href"))
            title = _itertext_join(node)
            _append_web_result(
                cls,
                items,
                seen_urls,
                site_host=site_host,
                url_text=href,
                title_text=title,
                snippet_text="",
            )
            if len(items) >= limit:
                break

    def _parse_regex(raw_html: str, items: List[Dict[str, str]], seen_urls: set[str]) -> None:
        for match in _GENERIC_ANCHOR_RE.finditer(raw_html):
            href = _extract_yahoo_target_url(match.group(1))
            title_html = match.group(2)
            title = _html_fragment_to_text(title_html)
            _append_web_result(
                cls,
                items,
                seen_urls,
                site_host=site_host,
                url_text=href,
                title_text=title,
                snippet_text="",
            )
            if len(items) >= limit:
                break

    return _parse_web_results_with_fallback(
        cls,
        html_text=html_text,
        limit=limit,
        lxml_parser=_parse_lxml,
        regex_parser=_parse_regex,
    )


def query_yahoo(
    cls,
    *,
    search_query: str,
    site_host: str,
    limit: int,
    session: Any,
    deadline: Optional[float] = None,
) -> List[Dict[str, str]]:
    """Fetch results from Yahoo search (robust fallback in bot-protected envs)."""
    all_rows: List[Dict[str, str]] = []
    seen_urls: set[str] = set()

    max_pages = max(1, min((max(1, int(limit or 1)) + 9) // 10, 3))
    for page_idx in range(max_pages):
        if deadline is not None and time.monotonic() >= deadline:
            break

        params = {
            "p": search_query,
            "n": "10",
            "b": str((page_idx * 10) + 1),
        }
        try:
            read_timeout = 10.0
            if deadline is not None:
                remaining = max(0.0, float(deadline - time.monotonic()))
                if remaining <= 0.0:
                    break
                read_timeout = max(3.0, min(10.0, remaining))

            response = session.get(
                "https://search.yahoo.com/search",
                params=params,
                timeout=(3, read_timeout),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            response.raise_for_status()
        except Exception:
            break

        page_rows = parse_yahoo_results(
            cls,
            html_text=response.text,
            site_host=site_host,
            limit=max(1, limit - len(all_rows)),
        )
        new_rows = 0
        for row in page_rows:
            url_value = str(row.get("url") or "").strip()
            if not url_value or url_value in seen_urls:
                continue
            seen_urls.add(url_value)
            all_rows.append(row)
            new_rows += 1
            if len(all_rows) >= limit:
                break

        if len(all_rows) >= limit or new_rows == 0:
            break

    return all_rows[:limit]


def parse_bing_results(
    cls,
    *,
    html_text: str,
    site_host: str,
    limit: int,
) -> List[Dict[str, str]]:
    """Parse Bing HTML search results into normalized rows."""
    def _parse_lxml(doc: Any, items: List[Dict[str, str]], seen_urls: set[str]) -> None:
        result_nodes = doc.xpath("//li[contains(@class, 'b_algo')]")

        for node in result_nodes:
            links = node.xpath(".//h2/a")
            if not links:
                continue
            link = links[0]
            href = str(link.get("href") or "").strip()
            title = _itertext_join(link)

            snippet = ""
            for sel in (
                ".//*[contains(@class,'b_caption')]//p",
                ".//*[contains(@class,'b_snippet')]",
                ".//p",
            ):
                snip_nodes = node.xpath(sel)
                if snip_nodes:
                    snippet = _itertext_join(snip_nodes[0])
                    break

            _append_web_result(
                cls,
                items,
                seen_urls,
                site_host=site_host,
                url_text=href,
                title_text=title,
                snippet_text=snippet,
            )
            if len(items) >= limit:
                break

    def _parse_regex(raw_html: str, items: List[Dict[str, str]], seen_urls: set[str]) -> None:
        for match in _BING_RESULT_ANCHOR_RE.finditer(raw_html):
            href = match.group(1)
            title = _html_fragment_to_text(match.group(2))
            _append_web_result(
                cls,
                items,
                seen_urls,
                site_host=site_host,
                url_text=href,
                title_text=title,
                snippet_text="",
            )
            if len(items) >= limit:
                break

    return _parse_web_results_with_fallback(
        cls,
        html_text=html_text,
        limit=limit,
        lxml_parser=_parse_lxml,
        regex_parser=_parse_regex,
    )


def query_bing(
    cls,
    *,
    search_query: str,
    site_host: str,
    limit: int,
    session: Any,
    deadline: Optional[float] = None,
) -> List[Dict[str, str]]:
    """Fetch results from Bing (supports filetype: and site: natively)."""
    all_rows: List[Dict[str, str]] = []
    seen_urls: set[str] = set()

    page_start = 1
    pages_checked = 0
    max_pages = max(1, min((max(1, int(limit or 1)) + 49) // 50, 3))
    while len(all_rows) < limit and pages_checked < max_pages:
        if deadline is not None and time.monotonic() >= deadline:
            break

        params = {"q": search_query, "first": str(page_start), "count": "50"}
        try:
            read_timeout = 10.0
            if deadline is not None:
                remaining = max(0.0, float(deadline - time.monotonic()))
                if remaining <= 0.0:
                    break
                read_timeout = max(3.0, min(10.0, remaining))

            response = session.get(
                "https://www.bing.com/search",
                params=params,
                timeout=(3, read_timeout),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            response.raise_for_status()
        except Exception:
            break

        page_rows = parse_bing_results(
            cls,
            html_text=response.text,
            site_host=site_host,
            limit=max(1, limit - len(all_rows)),
        )
        new_rows = 0
        for row in page_rows:
            url_value = str(row.get("url") or "").strip()
            if not url_value or url_value in seen_urls:
                continue
            seen_urls.add(url_value)
            all_rows.append(row)
            new_rows += 1
            if len(all_rows) >= limit:
                break

        if new_rows == 0 or len(all_rows) >= limit:
            break
        page_start += 50
        pages_checked += 1

    return all_rows


def query_web_search(
    cls,
    *,
    search_query: str,
    site_host: str,
    limit: int,
) -> List[Dict[str, str]]:
    """Execute web search and return parsed result rows.

    Uses Yahoo first (works in environments where Bing/DDG HTML endpoints
    are challenge-gated), then Bing, then DuckDuckGo.
    """
    from API.requests_client import get_requests_session

    session = get_requests_session()
    normalized_limit = max(1, min(int(limit or 1), 100))
    engine_deadline = time.monotonic() + 12.0

    all_rows = query_yahoo(
        cls,
        search_query=search_query,
        site_host=site_host,
        limit=normalized_limit,
        session=session,
        deadline=engine_deadline,
    )
    if all_rows:
        return all_rows[:normalized_limit]

    all_rows = query_bing(
        cls,
        search_query=search_query,
        site_host=site_host,
        limit=normalized_limit,
        session=session,
        deadline=engine_deadline,
    )
    if all_rows:
        return all_rows[:normalized_limit]

    all_rows_ddg: List[Dict[str, str]] = []
    seen_urls: set[str] = set()
    endpoints = [
        "https://html.duckduckgo.com/html/",
        "https://duckduckgo.com/html/",
    ]
    for endpoint in endpoints:
        if time.monotonic() >= engine_deadline:
            break
        max_offsets = min(3, max(1, (normalized_limit + 29) // 30))
        for page_idx in range(max_offsets):
            if time.monotonic() >= engine_deadline:
                break
            offset = page_idx * 30
            params = {"q": search_query, "s": str(offset)}
            remaining = max(0.0, float(engine_deadline - time.monotonic()))
            if remaining <= 0.0:
                break
            read_timeout = max(3.0, min(10.0, remaining))
            response = session.get(
                endpoint,
                params=params,
                timeout=(3, read_timeout),
                headers={"Referer": "https://duckduckgo.com/"},
            )
            response.raise_for_status()
            page_rows = parse_duckduckgo_results(
                cls,
                html_text=response.text,
                site_host=site_host,
                limit=max(1, normalized_limit - len(all_rows_ddg)),
            )
            new_rows = 0
            for row in page_rows:
                url_value = str(row.get("url") or "").strip()
                if not url_value or url_value in seen_urls:
                    continue
                seen_urls.add(url_value)
                all_rows_ddg.append(row)
                new_rows += 1
                if len(all_rows_ddg) >= normalized_limit:
                    break
            if len(all_rows_ddg) >= normalized_limit or new_rows == 0:
                break
        if all_rows_ddg:
            break

    return all_rows_ddg[:normalized_limit]


def _is_probable_html_path(path_value: str) -> bool:
    """Return True when URL path likely points to an HTML page."""
    path = str(path_value or "").strip()
    if not path:
        return True
    suffix = Path(path).suffix.lower()
    if not suffix:
        return True
    return suffix in {".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".shtml", ".xhtml"}


def _extract_html_links(cls, *, html_text: str, base_url: str) -> List[str]:
    """Extract absolute links from an HTML document."""
    links: List[str] = []
    seen: set[str] = set()

    def _add_link(raw_href: Any) -> None:
        href = str(raw_href or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            return
        try:
            absolute = urljoin(base_url, href)
            parsed = urlparse(absolute)
        except Exception:
            return
        if str(getattr(parsed, "scheme", "") or "").lower() not in {"http", "https"}:
            return
        clean = parsed._replace(fragment="").geturl()
        if clean in seen:
            return
        seen.add(clean)
        links.append(clean)

    try:
        from lxml import html as lxml_html

        doc = lxml_html.fromstring(html_text or "")
        for node in doc.xpath("//a[@href]"):
            _add_link(node.get("href"))
    except Exception:
        href_pattern = re.compile(r'<a[^>]+href=["\']([^"\']+)["\']', flags=re.IGNORECASE)
        for match in href_pattern.finditer(html_text or ""):
            _add_link(match.group(1))

    return links


def _page_url_from_text(value: Any) -> str:
    raw = str(value or "").strip().strip("'\"")
    if not raw or not _PAGE_URL_RE.match(raw):
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    if str(getattr(parsed, "scheme", "") or "").lower() not in {"http", "https"}:
        return ""
    if not str(getattr(parsed, "hostname", "") or "").strip():
        return ""
    return parsed._replace(fragment="").geturl()


def _normalize_scrape_type_group(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return str(_SCRAPE_TYPE_ALIASES.get(text) or "")


def scrape_type_group_for_ext(ext_value: Any, *, fallback: str = "other") -> str:
    ext = _normalize_extension(ext_value)
    if not ext:
        return str(fallback or "")
    return str(_EXT_TO_SCRAPE_TYPE.get(ext) or fallback or "")


def url_looks_like_direct_file(url: str) -> bool:
    text = str(url or "").strip()
    if not text:
        return False
    try:
        parsed = urlparse(text)
    except Exception:
        return False
    ext = _normalize_extension(Path(str(getattr(parsed, "path", "") or "")).suffix)
    if not ext or ext in _SCRAPE_SKIP_EXT:
        return False
    return ext in _EXT_TO_SCRAPE_TYPE or len(ext) <= 5


def parse_scrape_filters(query: Any) -> tuple[str, str]:
    """Parse scrape filters into (type_group, filetype).

    ``type:image`` selects a type group; ``ext:pdf`` / ``filetype:pdf`` select a
    concrete extension. When a ``type:`` value is not a known group name it falls
    back to an extension filter.
    """
    text = _normalize_space(query)
    if not text:
        return "", ""
    type_match = _TYPE_TOKEN_RE.search(text)
    if type_match:
        raw = str(type_match.group(1) or "").strip().lower()
        type_group = _normalize_scrape_type_group(raw)
        if type_group:
            return type_group, ""
        return "", _normalize_extension(raw)
    filetype_match = _FILETYPE_TOKEN_RE.search(text)
    if filetype_match:
        raw = str(filetype_match.group(1) or "").strip().lower()
        return "", _normalize_extension(raw)
    return "", ""


def _srcset_urls(raw_srcset: Any) -> List[str]:
    urls: List[str] = []
    text = str(raw_srcset or "").strip()
    if not text:
        return urls
    for chunk in text.split(","):
        match = _SRCSET_URL_RE.search(chunk.strip())
        if not match:
            continue
        candidate = str(match.group(1) or "").strip()
        if candidate:
            urls.append(candidate)
    return urls


def _asset_title_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        name = Path(unquote(str(getattr(parsed, "path", "") or ""))).name
    except Exception:
        name = ""
    return name or url


def _add_scrape_asset(
    assets: List[Dict[str, str]],
    seen: set[str],
    *,
    raw_url: Any,
    base_url: str,
    kind_hint: str = "",
) -> None:
    href = str(raw_url or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "data:")):
        return
    if len(assets) >= _SCRAPE_MAX_ASSETS:
        return
    try:
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
    except Exception:
        return
    if str(getattr(parsed, "scheme", "") or "").lower() not in {"http", "https"}:
        return
    clean = parsed._replace(fragment="").geturl()
    if not clean or clean in seen:
        return
    path_text = str(getattr(parsed, "path", "") or "")
    ext = _normalize_extension(Path(path_text).suffix)
    if ext in _SCRAPE_SKIP_EXT:
        return
    type_group = scrape_type_group_for_ext(ext, fallback="")
    if not type_group:
        if kind_hint:
            type_group = kind_hint
            if not ext:
                ext = kind_hint if kind_hint != "image" else ""
        elif ext:
            type_group = "other"
        else:
            return
    seen.add(clean)
    assets.append(
        {
            "url": clean,
            "title": _asset_title_from_url(clean),
            "ext": ext,
            "type": type_group,
            "snippet": "",
        }
    )


def _extract_html_assets(html_text: str, base_url: str) -> List[Dict[str, str]]:
    assets: List[Dict[str, str]] = []
    seen: set[str] = set()

    def _add(raw_url: Any, *, kind_hint: str = "") -> None:
        _add_scrape_asset(assets, seen, raw_url=raw_url, base_url=base_url, kind_hint=kind_hint)

    try:
        from lxml import html as lxml_html

        doc = lxml_html.fromstring(html_text or "")
        for node in doc.xpath("//img"):
            _add(node.get("src"), kind_hint="image")
            _add(node.get("data-src"), kind_hint="image")
            _add(node.get("data-original"), kind_hint="image")
            for srcset_url in _srcset_urls(node.get("srcset")):
                _add(srcset_url, kind_hint="image")
        for node in doc.xpath("//source|//video|//audio"):
            kind = "video"
            tag = str(getattr(node, "tag", "") or "").lower()
            if tag == "audio":
                kind = "audio"
            _add(node.get("src"), kind_hint=kind)
            for srcset_url in _srcset_urls(node.get("srcset")):
                _add(srcset_url, kind_hint=kind)
        for node in doc.xpath("//embed|//object"):
            _add(node.get("src"))
            _add(node.get("data"))
        for node in doc.xpath("//a[@href]"):
            _add(node.get("href"))
        for node in doc.xpath("//meta[@property or @name]"):
            key = str(node.get("property") or node.get("name") or "").strip().lower()
            if key in {"og:image", "twitter:image", "og:image:url"}:
                _add(node.get("content"), kind_hint="image")
    except Exception:
        img_pattern = re.compile(
            r'<(?:img|source|video|audio|embed)\b[^>]+(?:src|data-src)=["\']([^"\']+)["\']',
            flags=re.IGNORECASE,
        )
        href_pattern = re.compile(r'<a[^>]+href=["\']([^"\']+)["\']', flags=re.IGNORECASE)
        for match in img_pattern.finditer(html_text or ""):
            _add(match.group(1), kind_hint="image")
        for match in href_pattern.finditer(html_text or ""):
            _add(match.group(1))

    return assets


def _scrape_page(page_url: str, *, timeout: float = 12.0) -> tuple[str, List[Dict[str, str]]]:
    """Fetch a URL and classify it.

    Returns ``(kind, assets)`` where kind is one of:
      - ``"html"``: an HTML page (assets may be empty)
      - ``"file"``: a direct downloadable file
      - ``"error"``: fetch failed, unsafe host, or oversized body
    """
    target = _page_url_from_text(page_url)
    if not target:
        return "error", []

    try:
        parsed = urlparse(target)
        host = str(getattr(parsed, "hostname", "") or "").strip()
    except Exception:
        return "error", []
    if _host_is_blocked(host):
        debug("Page scrape blocked host", {"url": target, "host": host})
        return "error", []

    cached = _SCRAPE_CACHE.get(target)
    if cached is not None:
        cached_at, assets = cached
        if time.monotonic() - cached_at <= _SCRAPE_CACHE_TTL:
            return "html", assets

    from API.requests_client import get_requests_session

    session = get_requests_session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        response = session.get(
            target,
            timeout=(4, max(4.0, float(timeout or 12.0))),
            headers=headers,
            stream=True,
        )
    except Exception as exc:
        debug("Page scrape failed", {"url": target, "error": str(exc)})
        return "error", []

    try:
        with response:
            response.raise_for_status()
            final_url = str(getattr(response, "url", "") or target)
            try:
                final_host = str(urlparse(final_url).hostname or "").strip()
            except Exception:
                final_host = ""
            if _host_is_blocked(final_host):
                debug("Page scrape blocked redirect host", {"url": final_url, "host": final_host})
                return "error", []

            content_type = str((response.headers or {}).get("content-type", "") or "").lower()
            if "html" not in content_type and "xhtml" not in content_type:
                if url_looks_like_direct_file(final_url):
                    ext = _normalize_extension(Path(urlparse(final_url).path).suffix)
                    return "file", [
                        {
                            "url": urlparse(final_url)._replace(fragment="").geturl(),
                            "title": _asset_title_from_url(final_url),
                            "ext": ext,
                            "type": scrape_type_group_for_ext(ext),
                            "snippet": "Direct file URL",
                        }
                    ]
                return "error", []

            chunks: List[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > _SCRAPE_MAX_BYTES:
                    debug("Page scrape exceeded size cap", {"url": final_url, "bytes": total})
                    return "error", []
                chunks.append(chunk)

            html_text = b"".join(chunks).decode("utf-8", errors="replace")
    except Exception as exc:
        debug("Page scrape failed", {"url": target, "error": str(exc)})
        return "error", []

    assets = _extract_html_assets(html_text, final_url)
    _SCRAPE_CACHE[target] = (time.monotonic(), assets)
    return "html", assets


def scrape_page_assets(page_url: str, *, timeout: float = 12.0) -> List[Dict[str, str]]:
    return _scrape_page(page_url, timeout=timeout)[1]


def filter_scrape_assets(
    assets: Sequence[Dict[str, str]],
    *,
    type_group: str = "",
    filetype: str = "",
) -> List[Dict[str, str]]:
    wanted_type = _normalize_scrape_type_group(type_group)
    wanted_ext = _normalize_extension(filetype)
    rows: List[Dict[str, str]] = []
    for asset in assets or []:
        ext = _normalize_extension(asset.get("ext"))
        group = _normalize_scrape_type_group(asset.get("type")) or scrape_type_group_for_ext(ext)
        if wanted_type and group != wanted_type:
            continue
        if wanted_ext and ext != wanted_ext:
            continue
        rows.append(
            {
                "url": str(asset.get("url") or "").strip(),
                "title": str(asset.get("title") or "").strip(),
                "ext": ext,
                "type": group or "other",
                "snippet": str(asset.get("snippet") or "").strip(),
            }
        )
    return [row for row in rows if row.get("url")]


def group_scrape_assets(assets: Sequence[Dict[str, str]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, str]]] = {key: [] for key in _SCRAPE_TYPE_ORDER}
    for asset in filter_scrape_assets(assets):
        grouped.setdefault(str(asset.get("type") or "other"), []).append(asset)

    rows: List[Dict[str, Any]] = []
    for type_key in _SCRAPE_TYPE_ORDER:
        files = grouped.get(type_key) or []
        if not files:
            continue
        extensions: List[str] = []
        seen_ext: set[str] = set()
        for asset in files:
            ext = _normalize_extension(asset.get("ext"))
            if not ext or ext in seen_ext:
                continue
            seen_ext.add(ext)
            extensions.append(ext)
        rows.append(
            {
                "type": type_key,
                "label": _SCRAPE_TYPE_LABELS.get(type_key, type_key.title()),
                "count": len(files),
                "extensions": extensions,
            }
        )
    return rows


def build_scrape_type_rows(assets: Sequence[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Build normalized type-group rows shared by search-file and download-file."""
    rows: List[Dict[str, Any]] = []
    for type_row in group_scrape_assets(assets):
        type_key = str(type_row.get("type") or "").strip()
        label = str(type_row.get("label") or type_key).strip()
        count = int(type_row.get("count") or 0)
        extensions = [
            str(ext).strip()
            for ext in (type_row.get("extensions") or [])
            if str(ext).strip()
        ]
        extensions_text = ", ".join(extensions)
        rows.append(
            {
                "type": type_key,
                "label": label,
                "count": count,
                "extensions": extensions,
                "extensions_text": extensions_text,
                "columns": [
                    ("Type", label),
                    ("Files", count),
                    ("Extensions", extensions_text),
                ],
                "selection_query": f"type:{type_key}",
            }
        )
    return rows


def build_scrape_file_rows(
    assets: Sequence[Dict[str, str]],
    *,
    type_group: str = "",
    filetype: str = "",
) -> List[Dict[str, Any]]:
    """Build normalized file rows shared by search-file and download-file."""
    rows: List[Dict[str, Any]] = []
    for asset in filter_scrape_assets(assets, type_group=type_group, filetype=filetype):
        url = str(asset.get("url") or "").strip()
        if not url:
            continue
        ext = _normalize_extension(asset.get("ext"))
        group = str(asset.get("type") or "").strip()
        title = str(asset.get("title") or "").strip() or url
        group_label = _SCRAPE_TYPE_LABELS.get(group, group or "File")
        rows.append(
            {
                "url": url,
                "ext": ext,
                "group": group,
                "group_label": group_label,
                "title": title,
                "columns": [
                    ("Title", title),
                    ("Type", group_label),
                    ("Ext", ext),
                    ("URL", url),
                ],
            }
        )
    return rows


def assemble_scrape_result_rows(
    *,
    page_url: str,
    assets: Sequence[Dict[str, str]],
    type_group: str = "",
    filetype: str = "",
    command: str = "search-file",
    extra_args: Optional[Sequence[str]] = None,
    site_host: str = "",
    limit: int = 100,
) -> tuple[bool, str, List[Dict[str, Any]], str]:
    """Build scrape table rows for search-file and download-file.

    Returns (show_types, table_name, rows, empty_message).
    """
    extra = [str(a) for a in (extra_args or []) if str(a).strip()]
    show_types = not type_group and not filetype
    if show_types:
        type_rows = build_scrape_type_rows(assets)
        if not type_rows:
            return True, "web.scrape.types", [], f"No downloadable files found on {page_url}"
        rows: List[Dict[str, Any]] = []
        for type_row in type_rows:
            type_key = str(type_row.get("type") or "").strip()
            label = str(type_row.get("label") or type_key).strip()
            selection_query = str(type_row.get("selection_query") or f"type:{type_key}")
            if command == "download-file":
                selection_args = ["-url", page_url, "-query", selection_query] + extra
                selection_action = ["download-file", "-url", page_url, "-query", selection_query] + extra
            else:
                selection_args = ["-query", selection_query, page_url]
                selection_action = ["search-file", page_url, "-query", selection_query]
            rows.append(
                {
                    "table": "web.scrape.types",
                    "title": label,
                    "path": page_url,
                    "url": page_url,
                    "columns": type_row.get("columns"),
                    "tag": [f"site:{site_host}", f"type:{type_key}"] if site_host else [f"type:{type_key}"],
                    "detail": f"{int(type_row.get('count') or 0)} file(s)",
                    "_selection_args": selection_args,
                    "_selection_action": selection_action,
                }
            )
        return True, "web.scrape.types", rows, ""

    file_rows = build_scrape_file_rows(assets, type_group=type_group, filetype=filetype)
    if not file_rows:
        label = type_group or filetype or "files"
        return False, "web.scrape", [], f"No {label} found on {page_url}"
    rows = []
    for asset in file_rows[: max(1, int(limit or 100))]:
        file_url = str(asset.get("url") or "").strip()
        if not file_url:
            continue
        selection_args = ["-url", file_url] + extra
        rows.append(
            {
                "table": "web.scrape",
                "title": asset.get("title") or file_url,
                "path": file_url,
                "url": file_url,
                "ext": asset.get("ext"),
                "columns": asset.get("columns"),
                "detail": asset.get("group_label") or "",
                "tag": (
                    ([f"site:{site_host}"] if site_host else [])
                    + ([f"type:{asset.get('group')}"] if asset.get("group") else [])
                    + ([f"ext:{asset.get('ext')}"] if asset.get("ext") else [])
                ),
                "_selection_args": selection_args,
                "_selection_action": ["download-file"] + selection_args,
            }
        )
    return False, "web.scrape", rows, ""


def crawl_site_for_extension(
    cls,
    *,
    seed_url: str,
    site_host: str,
    extension: str,
    limit: int,
    max_duration_seconds: float = 15.0,
) -> List[Dict[str, str]]:
    """Fallback crawler that discovers in-site file links by extension."""
    from API.requests_client import get_requests_session

    normalized_ext = _normalize_extension(extension)
    if not normalized_ext:
        return []

    start_url = _normalize_seed_url(cls, seed_url, site_host)
    if not start_url:
        return []

    session = get_requests_session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    queue: deque[str] = deque([start_url])
    queued: set[str] = {start_url}
    visited_pages: set[str] = set()
    seen_files: set[str] = set()
    rows: List[Dict[str, str]] = []
    normalized_limit = max(1, min(int(limit or 1), 100))
    max_pages = max(8, min(normalized_limit * 4, 64))
    crawl_deadline = time.monotonic() + max(5.0, float(max_duration_seconds or 0.0))

    while (
        queue
        and len(visited_pages) < max_pages
        and len(rows) < normalized_limit
        and time.monotonic() < crawl_deadline
    ):
        page_url = queue.popleft()
        queued.discard(page_url)
        if page_url in visited_pages:
            continue
        visited_pages.add(page_url)

        if time.monotonic() >= crawl_deadline:
            break

        try:
            response = session.get(page_url, timeout=(4, 8), headers=headers)
            response.raise_for_status()
        except Exception:
            continue

        final_url = str(getattr(response, "url", "") or page_url)
        try:
            parsed_final = urlparse(final_url)
        except Exception:
            continue

        final_host = _normalize_host(getattr(parsed_final, "hostname", "") or "")
        if not _url_matches_site(final_url, site_host):
            continue

        final_path = str(getattr(parsed_final, "path", "") or "")
        direct_ext = _normalize_extension(Path(final_path).suffix)
        if direct_ext == normalized_ext:
            file_url = parsed_final._replace(fragment="").geturl()
            if file_url not in seen_files:
                seen_files.add(file_url)
                title = Path(unquote(final_path)).name or file_url
                rows.append(
                    {
                        "url": file_url,
                        "title": title,
                        "snippet": "Discovered via in-site crawl",
                    }
                )
            continue

        content_type = str((response.headers or {}).get("content-type", "") or "").lower()
        if "html" not in content_type and "xhtml" not in content_type:
            continue

        html_text = str(getattr(response, "text", "") or "")
        if not html_text:
            continue
        if len(html_text) > 2_500_000:
            continue

        discovered_links = _extract_html_links(cls, html_text=html_text, base_url=final_url)
        for idx, target in enumerate(discovered_links):
            if len(rows) >= normalized_limit:
                break
            if idx >= 300:
                break
            if time.monotonic() >= crawl_deadline:
                break
            try:
                parsed_target = urlparse(target)
            except Exception:
                continue
            target_host = _normalize_host(getattr(parsed_target, "hostname", "") or "")
            if not target_host or not (target_host == final_host or target_host.endswith(f".{site_host}")):
                if not _url_matches_site(target, site_host):
                    continue

            target_clean = parsed_target._replace(fragment="").geturl()
            target_path = str(getattr(parsed_target, "path", "") or "")
            target_ext = _normalize_extension(Path(target_path).suffix)

            if target_ext == normalized_ext:
                if target_clean in seen_files:
                    continue
                seen_files.add(target_clean)
                title = Path(unquote(target_path)).name or target_clean
                rows.append(
                    {
                        "url": target_clean,
                        "title": title,
                        "snippet": f"Discovered via crawl from {final_path or '/'}",
                    }
                )
                continue

            if _is_probable_html_path(target_path):
                if target_clean not in visited_pages and target_clean not in queued:
                    queue.append(target_clean)
                    queued.add(target_clean)

    if time.monotonic() >= crawl_deadline:
        debug(
            "Web crawl fallback reached time budget",
            {
                "site": site_host,
                "visited_pages": len(visited_pages),
                "queued_pages": len(queue),
                "results": len(rows),
                "time_budget_seconds": max_duration_seconds,
            },
        )

    return rows[:normalized_limit]


def _extract_site_host(cls, candidate: Any) -> Optional[str]:
    """Extract a host/domain from URL-like input."""
    raw = str(candidate or "").strip().strip('"').strip("'")
    if not raw:
        return None

    if raw.lower().startswith("site:"):
        raw = raw.split(":", 1)[1].strip()

    parsed = None
    try:
        parsed = urlparse(raw)
    except Exception:
        parsed = None

    if parsed is None or not getattr(parsed, "hostname", None):
        try:
            parsed = urlparse(f"https://{raw}")
        except Exception:
            parsed = None

    host = ""
    try:
        host = str(getattr(parsed, "hostname", "") or "").strip().lower()
    except Exception:
        host = ""

    host = _normalize_host(host)
    if not host or "." not in host:
        return None
    return host


def _normalize_seed_url(cls, seed_value: Any, site_host: str) -> str:
    """Build a safe crawl starting URL from user input and resolved host."""
    raw = str(seed_value or "").strip().strip("'\"")
    if not raw:
        raw = str(site_host or "").strip()

    if raw and not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"

    try:
        parsed = urlparse(raw)
    except Exception:
        parsed = urlparse("")

    target = _normalize_host(site_host)
    host = _normalize_host(getattr(parsed, "hostname", "") or "")
    if target and host and not (host == target or host.endswith(f".{target}")):
        return f"https://{target}/"

    scheme = str(getattr(parsed, "scheme", "") or "https").lower()
    if scheme not in {"http", "https"}:
        scheme = "https"

    netloc = str(getattr(parsed, "netloc", "") or "").strip()
    if not netloc:
        netloc = target
    path = str(getattr(parsed, "path", "") or "").strip()
    if not path:
        path = "/"

    return f"{scheme}://{netloc}{path}"


def build_web_search_plan(
    cls,
    *,
    query: str,
    positional_args: List[str],
    storage_backend: Optional[str],
    store_filter: Optional[str],
    hash_query: List[str],
) -> Optional[Dict[str, Any]]:
    """Build web-search plan for URL + ext/filetype query syntax.

    Example input:
        search-file "example.com/foo" -query "ext:pdf"
    Produces:
        site:example.com filetype:pdf
    """
    if storage_backend or store_filter or hash_query:
        return None

    text = _normalize_space(query)
    if not text:
        return None

    local_markers = ("url:", "hash:", "tag:", "instance:", "system:")
    if any(marker in text.lower() for marker in local_markers):
        return None

    site_host: Optional[str] = None
    site_from_positional = False
    site_token_to_strip = ""
    seed_url = ""

    site_match = _SITE_TOKEN_RE.search(text)
    if site_match:
        site_host = _extract_site_host(cls, site_match.group(1))
        seed_url = str(site_match.group(1) or "").strip()

    if not site_host and positional_args:
        site_host = _extract_site_host(cls, positional_args[0])
        site_from_positional = bool(site_host)
        if site_from_positional:
            site_token_to_strip = str(positional_args[0] or "").strip()
            seed_url = site_token_to_strip

    if not site_host:
        for token in text.split():
            candidate = str(token or "").strip().strip(",")
            if not candidate:
                continue
            lower_candidate = candidate.lower()
            if lower_candidate.startswith(("ext:", "filetype:", "type:", "site:")):
                continue
            if _SCHEME_PREFIX_RE.match(lower_candidate) and not lower_candidate.startswith(
                ("http://", "https://")
            ):
                continue
            guessed = _extract_site_host(cls, candidate)
            if guessed:
                site_host = guessed
                site_token_to_strip = candidate
                break

    scrape_url = ""
    for candidate in (seed_url, *(positional_args or []), *text.split()):
        scrape_url = _page_url_from_text(candidate)
        if scrape_url:
            break

    if not site_host and scrape_url:
        site_host = _extract_site_host(cls, scrape_url)

    if not site_host:
        return None

    filetype_match = _FILETYPE_TOKEN_RE.search(text)
    filetype = _normalize_extension(filetype_match.group(1)) if filetype_match else ""

    type_match = _TYPE_TOKEN_RE.search(text)
    type_group = _normalize_scrape_type_group(type_match.group(1)) if type_match else ""

    has_explicit_site = bool(site_match)
    if scrape_url:
        return {
            "mode": "scrape",
            "site_host": site_host,
            "filetype": filetype,
            "type_group": type_group,
            "search_query": scrape_url,
            "seed_url": scrape_url,
            "scrape_url": scrape_url,
        }

    if not filetype and not has_explicit_site:
        return None

    residual = text
    residual = _SITE_REMOVE_RE.sub(" ", residual)
    residual = _FILETYPE_REMOVE_RE.sub(" ", residual)
    residual = _TYPE_REMOVE_RE.sub(" ", residual)

    if site_from_positional and positional_args:
        first = str(positional_args[0] or "").strip()
        if first:
            residual = re.sub(rf"(?:^|\s){re.escape(first)}(?:\s|$)", " ", residual, count=1)
    elif site_token_to_strip:
        residual = re.sub(
            rf"(?:^|\s){re.escape(site_token_to_strip)}(?:\s|$)",
            " ",
            residual,
            count=1,
        )

    residual = _normalize_space(residual)

    search_terms: List[str] = [f"site:{site_host}"]
    if filetype:
        search_terms.append(f"filetype:{filetype}")
    if residual:
        search_terms.append(residual)

    search_query = " ".join(search_terms).strip()
    if not search_query:
        return None

    normalized_seed_url = _normalize_seed_url(cls, seed_url, site_host)

    return {
        "site_host": site_host,
        "filetype": filetype,
        "search_query": search_query,
        "residual": residual,
        "seed_url": normalized_seed_url,
    }
