# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only typography for full and legacy compact Briefs; never rewrite archives."""

import re

from rich.text import Text

from chrys.app.tui.util.source_text import sanitize_source_text

_SECTIONS = (
    "Goal",
    "Requirements",
    "Constraints",
    "Claims",
    "Already read",
    "Reported symptoms",
    "Requirement conflicts",
    "Tensions",
    "Verification focus",
    "OPEN LOOPS",
    "Priority contract clarifications",
    "Evidence recorded",
    "Evidence step",
)
_COMPACT_ROW = re.compile(r"(?<=;)\s+(?=[RK]\d+[!?+]/)")
_ID = re.compile(r"^(?:[-!?+]\s*)?([RK]\d+|C-\d{3,}|E-\d{3,})(?=[!?+/: ,]|$)")


def brief_text(body: str, *, heading_style="bold") -> Text:
    """Format known Brief labels and row boundaries, leaving source prose literal."""
    result = Text()
    for line in sanitize_source_text(body).splitlines():
        clean = line.strip()
        if clean.startswith("[Goal & Claims") and clean.endswith("]"):
            result.append(clean, style=heading_style).append("\n\n")
            continue
        label, separator, content = clean.partition(":")
        section = next(
            (name for name in _SECTIONS if label == name or label.startswith((name + " (", name + " —"))), None
        )
        if separator and section:
            if result.plain and not result.plain.endswith("\n\n"):
                result.append("\n")
            result.append(label + ":", style=heading_style).append("\n")
            if content.strip():
                rows = (
                    _COMPACT_ROW.split(content.strip())
                    if section in {"Requirements", "Constraints"}
                    else [content.strip()]
                )
                for row in rows:
                    _append_row(result, row, heading_style)
            continue
        if not clean:
            if not result.plain.endswith("\n\n"):
                result.append("\n")
            continue
        _append_row(result, clean, heading_style)
    return result


def _append_row(result: Text, row: str, heading_style) -> None:
    match = _ID.match(row)
    if match and result.plain and not result.plain.endswith("\n\n"):
        result.append("\n")
    offset = len(result)
    result.append("  " + row + "\n")
    if match:
        result.stylize(heading_style, offset + 2 + match.start(1), offset + 2 + match.end(1))
