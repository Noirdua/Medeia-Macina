from __future__ import annotations

import re
from typing import Any, Dict, List

DEFAULT_PROMPT_TEXT = "<🜂🜄|🜁🜃>"

SYNTAX_COLOR_FIELDS = (
    ("syntax_cmdlet", "cmdlet", "Cmdlet", "#ffffff"),
    ("syntax_action", "action", "Verb", "#ffd75f"),
    ("syntax_argument", "argument", "Argument", "#3b8eea"),
    ("syntax_value", "value", "Value", "#9a3209"),
    ("syntax_string", "string", "String", "#6d0d93"),
    ("syntax_pipe", "pipe", "Pipe", "#4caf50"),
    ("syntax_selection_at", "selection_at", "Selection @", "#f1c40f"),
    ("syntax_selection_range", "selection_range", "Selection Range", "#4caf50"),
    ("syntax_system_hash", "system_hash", "System #", "#e5c07b"),
    ("syntax_system_field", "system_field", "System Field", "#e06c75"),
    ("syntax_system_op", "system_op", "System Operator", "#56b6c2"),
    ("syntax_system_number", "system_number", "System Number", "#98c379"),
    ("syntax_template_func", "template_func", "Template Function", "#56b6c2"),
    ("syntax_template_bracket", "template_bracket", "Template Bracket", "#e5c07b"),
    ("syntax_template_or", "template_or", "Template Or", "#d19a66"),
    ("syntax_template_placeholder", "template_placeholder", "Template Placeholder", "#98c379"),
    ("syntax_template_field", "template_field", "Template Field", "#c678dd"),
)

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_NAMED_COLOR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def syntax_config_fields() -> List[Dict[str, Any]]:
    fields: List[Dict[str, Any]] = [
        {
            "key": "prompt_text",
            "label": "Prompt",
            "group": "Syntax",
            "type": "string",
            "default": DEFAULT_PROMPT_TEXT,
        }
    ]
    for key, _style, label, default in SYNTAX_COLOR_FIELDS:
        fields.append({
            "key": key,
            "label": label,
            "group": "Syntax",
            "type": "string",
            "default": default,
        })
    return fields


def _stored(config: Any, key: str) -> str:
    if not isinstance(config, dict):
        return ""
    return str(config.get(key) or "").strip()


def resolve_prompt_text(config: Any = None) -> str:
    text = _stored(config, "prompt_text").replace("\r", "").replace("\n", "")
    if not text or text.lower() in {"default", "reset"}:
        return DEFAULT_PROMPT_TEXT
    return text[:80]


def syntax_color_style(value: Any) -> str:
    text = str(value or "").strip()
    if _HEX_COLOR_RE.fullmatch(text) or _NAMED_COLOR_RE.fullmatch(text):
        if text.lower() not in {"default", "reset"}:
            return text
    return ""


def _color_value(raw: str, default: str) -> str:
    text = str(raw or "").strip()
    if not text or text.lower() in {"default", "reset"}:
        return default
    if _HEX_COLOR_RE.fullmatch(text) or _NAMED_COLOR_RE.fullmatch(text):
        return text
    return default


def resolve_syntax_style(config: Any = None) -> Dict[str, str]:
    style = {
        "bottom-toolbar": "noreverse",
        "completion-menu": "bg:#1c1c1c #f2f2f2",
        "completion-menu.completion": "bg:#1c1c1c #f2f2f2",
        "completion-menu.completion.current": "bg:#264f78 #ffffff",
        "completion-menu.meta.completion": "bg:#1c1c1c #d0d0d0",
        "completion-menu.meta.completion.current": "bg:#264f78 #ffffff",
        "completion-menu.multi-column-meta": "bg:#1c1c1c #d0d0d0",
    }
    for key, style_name, _label, default in SYNTAX_COLOR_FIELDS:
        style[style_name] = _color_value(_stored(config, key), default)
    return style
