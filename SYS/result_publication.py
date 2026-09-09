from __future__ import annotations

from typing import Any, Iterable, Optional


def resolve_publication_subject(
    items: Iterable[Any] | None,
    subject: Any = None,
) -> Any:
    if subject is not None:
        return subject
    resolved_items = list(items or [])
    if not resolved_items:
        return None
    if len(resolved_items) == 1:
        return resolved_items[0]
    return resolved_items


def publish_result_table(
    pipeline_context: Any,
    result_table: Any,
    items: Iterable[Any] | None = None,
    *,
    subject: Any = None,
    overlay: bool = False,
) -> None:
    resolved_items = list(items or [])
    resolved_subject = resolve_publication_subject(resolved_items, subject)
    if overlay:
        pipeline_context.set_last_result_table_overlay(
            result_table,
            resolved_items,
            subject=resolved_subject,
        )
        return
    pipeline_context.set_last_result_table(
        result_table,
        resolved_items,
        subject=resolved_subject,
    )


def overlay_existing_result_table(
    pipeline_context: Any,
    *,
    subject: Any = None,
) -> bool:
    table = pipeline_context.get_last_result_table()
    items = list(pipeline_context.get_last_result_items() or [])
    if table is None or not items:
        return False
    publish_result_table(
        pipeline_context,
        table,
        items,
        subject=subject,
        overlay=True,
    )
    return True