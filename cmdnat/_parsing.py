"""Re-export command parsing from SYS.command_parsing (single implementation)."""

from __future__ import annotations

from SYS.command_parsing import (
    VALUE_ARG_FLAGS,
    extract_arg_value,
    extract_piped_value,
    extract_value_arg,
    has_flag,
    normalize_to_list,
)

__all__ = [
    "VALUE_ARG_FLAGS",
    "extract_arg_value",
    "extract_piped_value",
    "extract_value_arg",
    "has_flag",
    "normalize_to_list",
]
