from __future__ import annotations

from typing import Any, Iterable


def add_row_columns(table: Any, columns: Iterable[tuple[str, Any]]) -> Any:
    row = table.add_row()
    for label, value in columns:
        row.add_column(str(label), "" if value is None else str(value))
    return row