from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence


_ACRONYM_LABELS = {
    "id": "ID",
    "ids": "IDs",
    "url": "URL",
    "urls": "URLs",
    "api": "API",
    "http": "HTTP",
    "https": "HTTPS",
    "ftp": "FTP",
    "ftps": "FTPS",
    "scp": "SCP",
    "ssh": "SSH",
    "ip": "IP",
    "mpv": "MPV",
}


def _labelize_key(key: str) -> str:
    parts = [part for part in str(key or "").replace("_", " ").split() if part]
    normalized: list[str] = []
    for part in parts:
        lowered = part.lower()
        normalized.append(_ACRONYM_LABELS.get(lowered, part.title()))
    return " ".join(normalized)


def _has_display_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        text = value.strip()
        return bool(text and text.lower() not in {"<null>", "null", "none"})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_has_display_value(item) for item in value)
    return True


def _normalize_tags_value(tags: Any) -> Optional[str]:
    if tags is None:
        return None
    if isinstance(tags, str):
        text = tags.strip()
        return text or None
    if isinstance(tags, Sequence):
        seen: list[str] = []
        for tag in tags:
            text = str(tag or "").strip()
            if text and text not in seen:
                seen.append(text)
        return ", ".join(seen) if seen else None
    text = str(tags).strip()
    return text or None


def prepare_detail_metadata(
    subject: Any,
    *,
    include_subject_fields: bool = False,
    title: Optional[str] = None,
    hash_value: Optional[str] = None,
    store: Optional[str] = None,
    path: Optional[str] = None,
    tags: Any = None,
    prefer_existing_tags: bool = True,
    extra_fields: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    from SYS.result_table import extract_item_metadata

    metadata = extract_item_metadata(subject) or {}
    if "Store" in metadata and "Instance" not in metadata:
        metadata["Instance"] = metadata.pop("Store")

    if include_subject_fields and isinstance(subject, dict):
        for key, value in subject.items():
            if str(key).startswith("_") or key in {"selection_action", "selection_args"}:
                continue
            label = _labelize_key(str(key))
            if label == "Store":
                label = "Instance"
            if label not in metadata and _has_display_value(value):
                metadata[label] = value

    if title:
        metadata["Title"] = title
    if hash_value:
        metadata["Hash"] = hash_value
    if store:
        metadata["Instance"] = store
    if path:
        metadata["Path"] = path

    tags_text = _normalize_tags_value(tags)
    if tags_text and (not prefer_existing_tags or not metadata.get("Tags")):
        metadata["Tags"] = tags_text

    for key, value in (extra_fields or {}).items():
        if _has_display_value(value):
            metadata[str(key)] = value

    return metadata


def create_detail_view(
    title: str,
    metadata: dict[str, Any],
    *,
    table_name: Optional[str] = None,
    source_command: Optional[tuple[str, Sequence[str]]] = None,
    init_command: Optional[tuple[str, Sequence[str]]] = None,
    max_columns: Optional[int] = None,
    exclude_tags: bool = False,
    detail_order: Optional[Sequence[str]] = None,
    value_case: Optional[str] = "preserve",
    perseverance: bool = True,
) -> Any:
    from SYS.result_table import ItemDetailView

    kwargs: dict[str, Any] = {"item_metadata": metadata}
    if max_columns is not None:
        kwargs["max_columns"] = max_columns
    if exclude_tags:
        kwargs["exclude_tags"] = True
    if detail_order is not None:
        normalized_order: list[str] = []
        for key in detail_order:
            text = str(key)
            if text.lower() == "store":
                normalized_order.append("Instance")
            else:
                normalized_order.append(text)
        kwargs["detail_order"] = normalized_order

    table = ItemDetailView(title, **kwargs)
    if table_name:
        table = table.set_table(table_name)
    if value_case:
        table = table.set_value_case(value_case)
    if perseverance:
        table = table._perseverance(True)
    if source_command:
        name, args = source_command
        table.set_source_command(name, list(args))
    if init_command:
        name, args = init_command
        table = table.init_command(name, list(args))
    return table


def render_selection_detail_view(
    *,
    ctx: Any,
    item: Any,
    title: str,
    metadata: dict[str, Any],
    table_name: Optional[str] = None,
    detail_order: Optional[Sequence[str]] = None,
    value_case: Optional[str] = "preserve",
    exclude_tags: bool = False,
) -> bool:
    from SYS.rich_display import stdout_console

    detail_view = create_detail_view(
        title,
        metadata,
        table_name=table_name,
        detail_order=detail_order,
        value_case=value_case,
        exclude_tags=exclude_tags,
    )

    payload = item.to_dict() if hasattr(item, "to_dict") else item
    try:
        if hasattr(ctx, "set_last_result_items_only") and isinstance(payload, dict):
            ctx.set_last_result_items_only([payload])
    except Exception:
        pass

    try:
        try:
            detail_view.title = ""
            detail_view.header_lines = []
        except Exception:
            pass
        stdout_console().print()
        stdout_console().print(detail_view.to_rich())
    except Exception:
        return False
    return True