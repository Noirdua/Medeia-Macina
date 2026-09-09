"""Convenience mixins and helpers for table-based plugins.

Provides a small `TablePluginMixin` that handles HTTP fetch + table extraction
(using `SYS.html_table.extract_records`) and converts records into
`PluginCore.base.SearchResult` rows with sane default column ordering.

Plugins can subclass this mixin to implement search quickly:

class MyPlugin(TablePluginMixin, Plugin):
    URL = ("https://example.org/search",)

    def search(self, query, limit=50, **kwargs):
        url = f"{self.URL[0]}?q={quote_plus(query)}"
        return self.search_table_from_url(url, limit=limit, xpaths=self.DEFAULT_XPATHS)

The mixin deliberately avoids adding heavy dependencies (uses our lxml helper)
so authors don't have to install pandas/bs4 unless they want to.
"""
from __future__ import annotations

from typing import List, Optional

from API.HTTP import HTTPClient
from PluginCore.base import SearchResult
from SYS.html_table import extract_records
import lxml.html as lxml_html

import logging
logger = logging.getLogger(__name__)


class TablePluginMixin:
    """Mixin to simplify plugins that scrape table/list results from HTML.

    Methods:
      - search_table_from_url(url, limit, xpaths): fetches HTML, extracts records, returns SearchResults
      - DEFAULT_XPATHS: default xpath list used when none is provided
    """

    # Reuse the same defaults as the html_table helper
    DEFAULT_XPATHS: List[str] = [
        "//table//tbody/tr",
        "//table//tr[td]",
        "//div[contains(@class,'list-item')]",
        "//div[contains(@class,'result')]",
        "//li[contains(@class,'item')]",
    ]

    def search_table_from_url(self, url: str, limit: int = 50, xpaths: Optional[List[str]] = None, timeout: float = 15.0) -> List[SearchResult]:
        """Fetch `url`, extract table/list records, and return SearchResult list.

        `xpaths` is passed to `extract_records` (falls back to DEFAULT_XPATHS).
        """
        if not url:
            return []

        try:
            with HTTPClient(timeout=timeout) as client:
                resp = client.get(url)
                content = resp.content
        except Exception:
            logger.exception("Failed to fetch URL %s for plugin %s", url, getattr(self, 'name', '<plugin>'))
            return []

        # Ensure we pass an lxml document or string (httpx returns bytes)
        try:
            doc = lxml_html.fromstring(content)
        except Exception:
            logger.debug("Failed to parse content with lxml; attempting to decode as utf-8", exc_info=True)
            try:
                doc = content.decode("utf-8")
            except Exception:
                logger.debug("Failed to decode content as utf-8; falling back to str()", exc_info=True)
                doc = str(content)

        records, chosen = extract_records(doc, base_url=url, xpaths=xpaths or self.DEFAULT_XPATHS)

        results: List[SearchResult] = []
        for rec in (records or [])[: int(limit)]:
            title = rec.get("title") or ""
            path = rec.get("path") or ""
            platform = rec.get("system") or rec.get("platform") or ""
            size = rec.get("size") or ""
            region = rec.get("region") or ""
            version = rec.get("version") or ""
            languages = rec.get("languages") or ""

            cols = [("Title", title)]
            if platform:
                cols.append(("Platform", platform))
            if size:
                cols.append(("Size", size))
            if region:
                cols.append(("Region", region))
            if version:
                cols.append(("Version", version))
            if languages:
                cols.append(("Languages", languages))

            results.append(
                SearchResult(
                    table=(getattr(self, "name", "plugin") or "plugin"),
                    title=title,
                    path=path,
                    detail="",
                    annotations=[],
                    media_kind="file",
                    size_bytes=None,
                    tag={getattr(self, "name", "plugin") or "plugin"},
                    columns=cols,
                    full_metadata={"raw_record": rec},
                )
            )

        return results
