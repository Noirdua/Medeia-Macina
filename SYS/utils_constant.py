from typing import Any, Dict

mime_maps: Dict[str, Dict[str, Dict[str, Any]]] = {
    "image": {
        "jpg": {
            "ext": ".jpg",
            "mimes": ["image/jpeg",
                      "image/jpg"]
        },
        "png": {
            "ext": ".png",
            "mimes": ["image/png"]
        },
        "gif": {
            "ext": ".gif",
            "mimes": ["image/gif"]
        },
        "webp": {
            "ext": ".webp",
            "mimes": ["image/webp"]
        },
        "avif": {
            "ext": ".avif",
            "mimes": ["image/avif"]
        },
        "jxl": {
            "ext": ".jxl",
            "mimes": ["image/jxl"]
        },
        "bmp": {
            "ext": ".bmp",
            "mimes": ["image/bmp"]
        },
        "heic": {
            "ext": ".heic",
            "mimes": ["image/heic"]
        },
        "heif": {
            "ext": ".heif",
            "mimes": ["image/heif"]
        },
        "ico": {
            "ext": ".ico",
            "mimes": ["image/x-icon",
                      "image/vnd.microsoft.icon"]
        },
        "qoi": {
            "ext": ".qoi",
            "mimes": ["image/qoi"]
        },
        "tiff": {
            "ext": ".tiff",
            "mimes": ["image/tiff",
                      "image/x-tiff"]
        },
        "svg": {
            "ext": ".svg",
            "mimes": ["image/svg+xml"]
        },
    },
    "image_sequence": {
        "apng": {
            "ext": ".apng",
            "mimes": ["image/apng"],
            "sequence": True
        },
        "avifs": {
            "ext": ".avifs",
            "mimes": ["image/avif-sequence"],
            "sequence": True
        },
        "heics": {
            "ext": ".heics",
            "mimes": ["image/heic-sequence"],
            "sequence": True
        },
        "heifs": {
            "ext": ".heifs",
            "mimes": ["image/heif-sequence"],
            "sequence": True
        },
    },
    "video": {
        "mp4": {
            "ext": ".mp4",
            "mimes": ["video/mp4",
                      "audio/mp4"]
        },
        "webm": {
            "ext": ".webm",
            "mimes": ["video/webm",
                      "audio/webm"]
        },
        "mov": {
            "ext": ".mov",
            "mimes": ["video/quicktime"]
        },
        "ogv": {
            "ext": ".ogv",
            "mimes": ["video/ogg"]
        },
        "mpeg": {
            "ext": ".mpeg",
            "mimes": ["video/mpeg"]
        },
        "avi": {
            "ext": ".avi",
            "mimes": ["video/x-msvideo",
                      "video/avi"]
        },
        "flv": {
            "ext": ".flv",
            "mimes": ["video/x-flv"]
        },
        "mkv": {
            "ext": ".mkv",
            "mimes": ["video/x-matroska",
                      "application/x-matroska"],
            "audio_only_ext": ".mka",
        },
        "wmv": {
            "ext": ".wmv",
            "mimes": ["video/x-ms-wmv"]
        },
        "rv": {
            "ext": ".rv",
            "mimes": ["video/vnd.rn-realvideo"]
        },
    },
    "audio": {
        "mp3": {
            "ext": ".mp3",
            "mimes": ["audio/mpeg",
                      "audio/mp3"]
        },
        "m4a": {
            "ext": ".m4a",
            "mimes": ["audio/mp4",
                      "audio/x-m4a"]
        },
        "ogg": {
            "ext": ".ogg",
            "mimes": ["audio/ogg"]
        },
        "opus": {
            "ext": ".opus",
            "mimes": ["audio/opus"]
        },
        "flac": {
            "ext": ".flac",
            "mimes": ["audio/flac"]
        },
        "wav": {
            "ext": ".wav",
            "mimes": ["audio/wav",
                      "audio/x-wav",
                      "audio/vnd.wave"]
        },
        "wma": {
            "ext": ".wma",
            "mimes": ["audio/x-ms-wma"]
        },
        "tta": {
            "ext": ".tta",
            "mimes": ["audio/x-tta"]
        },
        "wv": {
            "ext": ".wv",
            "mimes": ["audio/x-wavpack",
                      "audio/wavpack"]
        },
        "mka": {
            "ext": ".mka",
            "mimes": ["audio/x-matroska",
                      "video/x-matroska"]
        },
    },
    "document": {
        "pdf": {
            "ext": ".pdf",
            "mimes": ["application/pdf"]
        },
        "epub": {
            "ext": ".epub",
            "mimes": ["application/epub+zip"]
        },
        "djvu": {
            "ext": ".djvu",
            "mimes": ["application/vnd.djvu"]
        },
        "rtf": {
            "ext": ".rtf",
            "mimes": ["application/rtf"]
        },
        "docx": {
            "ext":
            ".docx",
            "mimes":
            ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
        },
        "xlsx": {
            "ext": ".xlsx",
            "mimes":
            ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
        },
        "pptx": {
            "ext":
            ".pptx",
            "mimes": [
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ],
        },
        "doc": {
            "ext": ".doc",
            "mimes": ["application/msword"]
        },
        "xls": {
            "ext": ".xls",
            "mimes": ["application/vnd.ms-excel"]
        },
        "ppt": {
            "ext": ".ppt",
            "mimes": ["application/vnd.ms-powerpoint"]
        },
    },
    "archive": {
        "zip": {
            "ext": ".zip",
            "mimes": ["application/zip"]
        },
        "7z": {
            "ext": ".7z",
            "mimes": ["application/x-7z-compressed"]
        },
        "rar": {
            "ext": ".rar",
            "mimes": ["application/x-rar-compressed",
                      "application/vnd.rar"]
        },
        "gz": {
            "ext": ".gz",
            "mimes": ["application/gzip",
                      "application/x-gzip"]
        },
        "tar": {
            "ext": ".tar",
            "mimes": ["application/x-tar"]
        },
        "cbz": {
            "ext": ".cbz",
            "mimes": ["application/zip"],
            "note":
            "zip archive of images; prefer extension-based detection for comics",
        },
    },
    "project": {
        "clip": {
            "ext": ".clip",
            "mimes": ["application/clip"]
        },
        "kra": {
            "ext": ".kra",
            "mimes": ["application/x-krita"]
        },
        "procreate": {
            "ext": ".procreate",
            "mimes": ["application/x-procreate"]
        },
        "psd": {
            "ext": ".psd",
            "mimes": ["image/vnd.adobe.photoshop"]
        },
        "swf": {
            "ext": ".swf",
            "mimes": ["application/x-shockwave-flash"]
        },
    },
    "other": {
        "octet-stream": {
            "ext": "",
            "mimes": ["application/octet-stream"]
        },
        "json": {
            "ext": ".json",
            "mimes": ["application/json"]
        },
        "xml": {
            "ext": ".xml",
            "mimes": ["application/xml",
                      "text/xml"]
        },
        "csv": {
            "ext": ".csv",
            "mimes": ["text/csv"]
        },
    },
}


def get_type_from_ext(ext: str) -> str:
    """Determine the type (e.g., 'image', 'video', 'audio') from file extension.

    Args:
        ext: File extension (with or without leading dot, e.g., 'jpg' or '.jpg')

    Returns:
        Type string (e.g., 'image', 'video', 'audio') or 'other' if unknown
    """
    if not ext:
        return "other"

    ext_clean = ext.lstrip(".").lower()

    for type_name, extensions_dict in mime_maps.items():
        if ext_clean in extensions_dict:
            return type_name

    return "other"


# Canonical supported extension set for all stores/cmdlets.
# Derived from mime_maps so there is a single source of truth.
ALL_SUPPORTED_EXTENSIONS: set[str] = {
    spec["ext"].lower()
    for group in mime_maps.values()
    for spec in group.values()
    if isinstance(spec, dict) and isinstance(spec.get("ext"), str) and spec.get("ext")
}
