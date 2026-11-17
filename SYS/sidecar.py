from __future__ import annotations

from pathlib import Path
from typing import List, Optional


def derive_sidecar_path(media_path: Path) -> Path:
    try:
        return media_path.parent / (media_path.name + ".tag")
    except ValueError:
        return media_path.with_name(media_path.name + ".tag")


def is_sidecar_filename(name: str) -> bool:
    lower = str(name or "").strip().lower()
    return lower.endswith(".tag") or lower.endswith(".metadata")


def parse_sidecar_text(raw: str) -> tuple[Optional[str], List[str], List[str]]:
    hash_value: Optional[str] = None
    tags: List[str] = []
    urls: List[str] = []
    in_tags = False
    saw_tags_section = False
    for raw_line in str(raw or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        lower = line.lower()
        if lower in {"[tags]", "[tag]"}:
            in_tags = True
            saw_tags_section = True
            continue
        if lower.startswith("[") and lower.endswith("]"):
            in_tags = False
            continue
        if in_tags:
            tags.append(line.lower())
            continue
        if lower.startswith("hash:"):
            hash_value = line.split(":", 1)[1].strip() if ":" in line else ""
        elif lower.startswith("url:"):
            url_part = line.split(":", 1)[1].strip() if ":" in line else ""
            if url_part:
                for url_segment in url_part.split(","):
                    for url_token in url_segment.split():
                        url_clean = url_token.strip()
                        if url_clean and url_clean not in urls:
                            urls.append(url_clean)
        elif saw_tags_section:
            continue
        else:
            tags.append(line.lower())
    return hash_value, tags, urls


def read_sidecar_metadata(sidecar_path: Path) -> tuple[Optional[str], List[str], List[str]]:
    if not sidecar_path.exists():
        return None, [], []
    try:
        raw = sidecar_path.read_text(encoding="utf-8")
    except OSError:
        return None, [], []
    return parse_sidecar_text(raw)
