from __future__ import annotations

"""Metadata domain command handlers.

This package centralizes routing for metadata sub-domains (tag, url,
relationship, note, inspect) behind the top-level `metadata` cmdlet.
"""

from .tag import run_tag_action
from .url import run_url_action
from .relationship import run_relationship_action
from .note import run_note_action
from .inspect import run_inspect_action

__all__ = [
    "run_tag_action",
    "run_url_action",
    "run_relationship_action",
    "run_note_action",
    "run_inspect_action",
]
