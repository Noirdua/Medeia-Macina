from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, List

HELP_INSTANCE = "help"

_FILES: List[Dict[str, object]] = [
    {
        "name": "sample-note.txt",
        "bytes": b"Sample note for help examples.\n",
        "tags": ["title:Sample Note", "artist:Sample", "series:Help", "type:text"],
    },
    {
        "name": "sample-photo.jpg",
        "bytes": bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9"),
        "tags": ["title:Sample Photo", "artist:Sample", "series:Help", "type:image"],
    },
    {
        "name": "sample-icon.png",
        "bytes": bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
        ),
        "tags": ["title:Sample Icon", "artist:Sample", "series:Help", "type:image"],
    },
    {
        "name": "sample-loop.gif",
        "bytes": b"GIF89a\x01\x00\x01\x00\x00\x00\x00;\x00",
        "tags": ["title:Sample Loop", "artist:Sample", "series:Help", "type:image"],
    },
    {
        "name": "sample-song.mp3",
        "bytes": b"ID3\x03\x00\x00\x00\x00\x00\x00",
        "tags": [
            "title:Sample Artist - Sample Song",
            "artist:Sample Artist",
            "album:Help",
            "track:1",
            "series:Help",
            "type:audio",
        ],
    },
    {
        "name": "sample-show.mp4",
        "bytes": bytes.fromhex("00000018667479706d703432000000006d703432"),
        "tags": [
            "title:Help Show - Pilot",
            "series:Help Show",
            "track:1",
            "artist:Sample",
            "channel:kabbalah nightly",
            "type:video",
        ],
    },
    {
        "name": "sample-page.pdf",
        "bytes": b"%PDF-1.1\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n",
        "tags": ["title:Sample Page", "artist:Sample", "series:Help", "type:book"],
    },
    {
        "name": "sample-pack.zip",
        "bytes": bytes.fromhex("504b0506000000000000000000000000000000000000"),
        "tags": ["title:Sample Pack", "artist:Sample", "series:Help", "type:archive"],
    },
    {
        "name": "sample-data.json",
        "bytes": b'{"title":"Sample Data","series":"Help"}\n',
        "tags": ["title:Sample Data", "artist:Sample", "series:Help", "type:data"],
    },
]


def sample_root() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "help-sample"


def _sidecar_text(tags: List[str]) -> str:
    lines = ["[tags]"]
    lines.extend(tags)
    return "\n".join(lines) + "\n"


def _drop_local_instance() -> None:
    from SYS.config import load_config, save_config

    config = load_config()
    plugin = config.get("plugin")
    if not isinstance(plugin, dict):
        return
    local = plugin.get("local")
    if not isinstance(local, dict) or HELP_INSTANCE not in local:
        return
    local.pop(HELP_INSTANCE, None)
    save_config(config)


def write_sample_tags(path: Path, tags: List[str]) -> bool:
    root = sample_root().resolve()
    try:
        target = Path(path).expanduser().resolve()
    except Exception:
        return False
    if target.parent != root:
        return False
    sidecar = target.parent / (target.name + ".metadata")
    sidecar.write_text(_sidecar_text([str(tag) for tag in tags if str(tag).strip()]), encoding="utf-8")
    return True


def ensure_sample(*, reset: bool = False) -> Path:
    root = sample_root()
    if reset and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    if reset:
        for child in list(root.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    for spec in _FILES:
        path = root / str(spec["name"])
        if reset or not path.is_file():
            path.write_bytes(spec["bytes"])  # type: ignore[arg-type]
        sidecar = path.parent / (path.name + ".metadata")
        if reset or not sidecar.is_file():
            sidecar.write_text(_sidecar_text(list(spec["tags"])), encoding="utf-8")  # type: ignore[arg-type]
    _drop_local_instance()
    return root
