"""Small helper utilities for extracting structured records from HTML tables
using lxml.

Goal: make it trivial for provider authors to extract table rows and common
fields (title, link, standardized column keys) without re-implementing the
same heuristics in every provider.

Key functions:
- find_candidate_nodes(doc_or_html, xpaths=...)
- extract_records(doc_or_html, base_url=None, xpaths=...)
- normalize_header(name, synonyms=...)

This module intentionally avoids heavyweight deps (no pandas) and works with
`lxml.html` elements (the project already uses lxml).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from lxml import html as lxml_html
from urllib.parse import urljoin
import re
from SYS.logger import debug

# Default xpaths for candidate result containers
_DEFAULT_XPATHS = [
    "//table//tbody/tr",
    "//table//tr[td]",
    "//div[contains(@class,'list-item')]",
    "//div[contains(@class,'result')]",
    "//li[contains(@class,'item')]",
]

# Simple header synonyms (you can extend as needed)
_DEFAULT_SYNONYMS = {
    "platform": "system",
    "system": "system",
    "name": "title",
    "title": "title",
}


def _ensure_doc(doc_or_html: Any) -> lxml_html.HtmlElement:
    if isinstance(doc_or_html, str):
        return lxml_html.fromstring(doc_or_html)
    return doc_or_html


def _text_or_img_title(el) -> str:
    # Prefer img/@title if present (useful for flag icons)
    try:
        imgs = el.xpath('.//img/@title')
        if imgs:
            return str(imgs[0]).strip()
    except Exception as exc:
        debug("Failed to retrieve img title from element: %s", exc, exc_info=True)
    return (el.text_content() or "").strip()


def find_candidate_nodes(doc_or_html: Any, xpaths: Optional[List[str]] = None) -> Tuple[List[Any], Optional[str]]:
    """Find candidate nodes for results using a prioritized xpath list.

    Returns (nodes, chosen_xpath).
    """
    doc = _ensure_doc(doc_or_html)
    for xp in (xpaths or _DEFAULT_XPATHS):
        try:
            found = doc.xpath(xp)
            if found:
                return list(found), xp
        except Exception as exc:
            debug("Failed to execute xpath %s: %s", xp, exc, exc_info=True)
            continue
    return [], None


def _parse_tr_nodes(tr_nodes: List[Any], base: Optional[str] = None) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []

    for tr in tr_nodes:
        try:
            tds = tr.xpath("./td")
            if not tds or len(tds) < 1:
                continue

            # canonical fields
            rec: Dict[str, str] = {}

            # Heuristic: if the first cell contains an anchor, treat it as the title/path
            # (detail pages often put the file link in the first column and size in the second).
            a0 = tds[0].xpath('.//a[contains(@href,"/vault/")]') or tds[0].xpath('.//a')
            if a0:
                rec["title"] = (a0[0].text_content() or "").strip()
                href = a0[0].get("href")
                rec["path"] = urljoin(base, href) if href and base else (href or "")

                # Try to find a size cell in the remaining tds (class 'size' is common)
                size_val = None
                for td in tds[1:]:
                    s = td.xpath('.//span[contains(@class,"size")]/text()')
                    if s:
                        size_val = str(s[0]).strip()
                        break
                if not size_val and len(tds) > 1:
                    txt = (tds[1].text_content() or "").strip()
                    # crude size heuristic: contains digits and a unit letter
                    if txt and re.search(r"\d", txt):
                        size_val = txt

                if size_val:
                    rec["size"] = size_val

            else:
                # First cell often "system"/"platform"
                rec["platform"] = _text_or_img_title(tds[0])

                # Title + optional link from second column
                if len(tds) > 1:
                    a = tds[1].xpath('.//a[contains(@href,"/vault/")]') or tds[1].xpath('.//a')
                    if a:
                        rec["title"] = (a[0].text_content() or "").strip()
                        href = a[0].get("href")
                        rec["path"] = urljoin(base, href) if href and base else (href or "")
                    else:
                        rec["title"] = (tds[1].text_content() or "").strip()

                # Additional columns in common Vimm layout
                if len(tds) > 2:
                    rec["region"] = _text_or_img_title(tds[2]).strip()
                if len(tds) > 3:
                    rec["version"] = (tds[3].text_content() or "").strip()
                if len(tds) > 4:
                    rec["languages"] = (tds[4].text_content() or "").strip()

            out.append(rec)
        except Exception:
            continue

    return out


def _parse_list_item_nodes(nodes: List[Any], base: Optional[str] = None) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for node in nodes:
        try:
            rec: Dict[str, str] = {}
            # title heuristics
            a = node.xpath('.//h2/a') or node.xpath('.//a')
            if a:
                rec["title"] = (a[0].text_content() or "").strip()
                href = a[0].get("href")
                rec["path"] = urljoin(base, href) if href and base else (href or "")
            else:
                rec["title"] = (node.text_content() or "").strip()

            # platform, size
            p = node.xpath('.//span[contains(@class,"platform")]/text()')
            if p:
                rec["platform"] = str(p[0]).strip()

            s = node.xpath('.//span[contains(@class,"size")]/text()')
            if s:
                rec["size"] = str(s[0]).strip()

            out.append(rec)
        except Exception:
            continue
    return out


def normalize_header(name: str, synonyms: Optional[Dict[str, str]] = None) -> str:
    """Normalize header names to a canonical form.

    Defaults map 'platform' -> 'system' and 'name' -> 'title', but callers
    can pass a custom synonyms dict.
    """
    if not name:
        return ""
    s = str(name or "").strip().lower()
    s = re.sub(r"\s+", "_", s)
    syn = (synonyms or _DEFAULT_SYNONYMS).get(s)
    return syn or s


def extract_records(doc_or_html: Any, base_url: Optional[str] = None, xpaths: Optional[List[str]] = None, use_pandas_if_available: bool = True) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Find result candidate nodes and return a list of normalized records plus chosen xpath.

    If pandas is available and `use_pandas_if_available` is True, attempt to parse
    HTML tables using `pandas.read_html` and return those records. Falls back to
    node-based parsing when pandas is not available or fails. Returns (records, chosen)
    where `chosen` is the xpath that matched or the string 'pandas' when the
    pandas path was used.
    """
    # Prepare an HTML string for pandas if needed
    html_text: Optional[str] = None
    if isinstance(doc_or_html, (bytes, bytearray)):
        try:
            html_text = doc_or_html.decode("utf-8")
        except Exception:
            html_text = doc_or_html.decode("latin-1", errors="ignore")
    elif isinstance(doc_or_html, str):
        html_text = doc_or_html
    else:
        try:
            html_text = lxml_html.tostring(doc_or_html, encoding="unicode")
        except Exception:
            html_text = str(doc_or_html)

    # Try pandas first when available and requested
    if use_pandas_if_available and html_text is not None:
        try:
            import pandas as _pd  # type: ignore

            dfs = _pd.read_html(html_text)
            if dfs:
                # pick the largest dataframe by row count for heuristics
                df = max(dfs, key=lambda d: getattr(d, "shape", (len(getattr(d, 'index', [])), 0))[0])
                try:
                    rows = df.to_dict("records")
                except Exception as exc:
                    debug("pandas HTML table parse: df.to_dict('records') failed: %s", exc, exc_info=True)
                    # Some DataFrame-like objects may have slightly different APIs
                    rows = [dict(r) for r in df]

                records: List[Dict[str, str]] = []
                for row in rows:
                    row_norm: Dict[str, str] = {}
                    for k, v in (row or {}).items():
                        nk = normalize_header(str(k or ""))
                        row_norm[nk] = (str(v).strip() if v is not None else "")
                    records.append(row_norm)

                # Attempt to recover hrefs by matching anchor text -> href
                try:
                    doc = lxml_html.fromstring(html_text)
                    anchors = {}
                    for a in doc.xpath('//a'):
                        txt = (a.text_content() or "").strip()
                        href = a.get("href")
                        if txt and href and txt not in anchors:
                            anchors[txt] = href
                    for rec in records:
                        if not rec.get("path") and rec.get("title"):
                            href = anchors.get(rec["title"])
                            if href:
                                rec["path"] = urljoin(base_url, href) if base_url else href
                except Exception as exc:
                    debug("pandas: failed to recover anchor hrefs for table rows: %s", exc, exc_info=True)

                return records, "pandas"
        except Exception as exc:
            # Pandas not present or parsing failed; fall back to node parsing
            debug("pandas: not available or parsing failed: %s", exc, exc_info=True)

    # Fallback to node-based parsing
    nodes, chosen = find_candidate_nodes(doc_or_html, xpaths=xpaths)
    if not nodes:
        return [], chosen

    # Determine node type and parse accordingly
    first = nodes[0]
    tag = getattr(first, "tag", "").lower()
    if tag == "tr":
        records = _parse_tr_nodes(nodes, base=base_url)
    else:
        # list-item style
        records = _parse_list_item_nodes(nodes, base=base_url)

    # Normalize keys (map platform->system etc)
    normed: List[Dict[str, str]] = []
    for r in records:
        norm_row: Dict[str, str] = {}
        for k, v in (r or {}).items():
            nk = normalize_header(k)
            norm_row[nk] = v
        normed.append(norm_row)

    return normed, chosen


# Small convenience: convert records to SearchResult. Plugins can call this or
# use their own mapping when they need full SearchResult objects.
from PluginCore.base import SearchResult  # local import to avoid circular issues


def records_to_search_results(records: List[Dict[str, str]], table: str = "plugin") -> List[SearchResult]:
    out: List[SearchResult] = []
    for rec in records:
        title = rec.get("title") or rec.get("name") or ""
        path = rec.get("path") or ""
        meta = dict(rec)
        out.append(
            SearchResult(
                table=table,
                title=str(title),
                path=str(path),
                detail="",
                annotations=[],
                media_kind="file",
                size_bytes=None,
                tag={table},
                columns=[(k.title(), v) for k, v in rec.items() if k and v],
                full_metadata={"raw_record": rec, "raw": rec},
            )
        )
    return out
