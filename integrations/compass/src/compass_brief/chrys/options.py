from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

_SIDE_CALL_STRIP_KEYS: Final[frozenset[str]] = frozenset(
    {
        "background",
        "continuation_token",
        "conversation",
        "conversation_id",
        "output_format",
        "previous_response_id",
        "response_format",
        "text_format",
        "tool_choice",
        "tools",
    }
)
_SIDE_CALL_EXTRA_BODY_STRIP_KEYS: Final[frozenset[str]] = frozenset(
    {
        *_SIDE_CALL_STRIP_KEYS,
        "input",
        "instructions",
        "messages",
        "output_config",
        "prompt",
        "store",
        "stream",
        "system",
        "text",
    }
)


def stateless_side_call_options(chat_options: Mapping[str, Any]) -> dict[str, Any]:
    """Isolate a tool-free side call from inherited conversation and output shaping."""
    options = {key: value for key, value in chat_options.items() if key not in _SIDE_CALL_STRIP_KEYS}
    for key in ("text", "output_config"):
        value = options.get(key)
        if isinstance(value, Mapping):
            preserved = {name: item for name, item in value.items() if name != "format"}
            if preserved:
                options[key] = preserved
                continue
        options.pop(key, None)
    extra_body = options.get("extra_body")
    if isinstance(extra_body, Mapping):
        options["extra_body"] = {
            key: value for key, value in extra_body.items() if key not in _SIDE_CALL_EXTRA_BODY_STRIP_KEYS
        }
    options["store"] = False
    return options
