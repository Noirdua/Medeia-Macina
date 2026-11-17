from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional, Set


def normalize_urls(value: Any) -> List[str]:
    """Normalize a URL field into a stable, deduplicated list.

    Accepts None, a single URL string, or a list/tuple/set of URL strings.
    """

    def _iter_raw_urls(raw: Any) -> Iterable[str]:
        if raw is None:
            return

        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return

            matches = re.findall(r"https?://[^\s,]+", text, flags=re.IGNORECASE)
            if matches:
                for match in matches:
                    yield match
                return

            for token in text.replace("\n", " ").replace("\r", " ").replace(",", " ").split():
                if not token:
                    continue
                lowered = token.lower()
                is_url = lowered.startswith((
                    "http://",
                    "https://",
                    "magnet:",
                    "torrent:",
                    "ytdl://",
                    "tidal:",
                    "data:",
                    "ftp:",
                    "sftp:",
                    "alldebrid:",
                    "alldebrid🧲",
                ))
                is_struct_url = (
                    "." in token
                    and "/" in token
                    and not token.startswith((".", "/"))
                )
                if is_url or is_struct_url:
                    yield token
            return

        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                if item is None:
                    continue
                if isinstance(item, str):
                    if item.strip():
                        yield item
                else:
                    text = str(item).strip()
                    if text:
                        yield text
            return

        text = str(raw).strip()
        if text:
            yield text

    def _canonicalize(url_text: str) -> Optional[str]:
        url = str(url_text or "").strip()
        if not url:
            return None
        url = url.strip("<>\"' ")
        url = url.rstrip(')].,;"')
        if not url:
            return None
        lowered = url.lower()
        has_scheme = lowered.startswith((
            "http://", "https://", "magnet:", "torrent:", "tidal:",
            "hydrus:", "ytdl:", "soulseek:", "matrix:", "file:",
            "alldebrid:", "alldebrid🧲",
        ))
        if not (has_scheme or "://" in lowered):
            return None
        return url

    seen: Set[str] = set()
    out: List[str] = []
    for raw_url in _iter_raw_urls(value):
        canonical = _canonicalize(raw_url)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out
