"""mini shell entry syntax; shared Brief rendering remains host independent."""

import re

from compass_brief.briefing.render import CANONICAL_DETAIL_ENTRY


def shell_detail_entry(text: str, entry: str) -> str:
    """Rewrite the Brief's own detail-entry placeholder for a different host.

    Call forms become argument lists, because a host reached through a shell has
    no Python call syntax. The renderer budgets the resulting host text too.
    """
    if not entry or entry == CANONICAL_DETAIL_ENTRY:
        return text

    def as_arguments(match: re.Match[str]) -> str:
        refs = [item.strip().strip('"\'') for item in match.group(1).split(",")]
        # `[...]` is the canonical placeholder for "any IDs"; with no argument the
        # entry returns every item, so passing a literal ellipsis would be wrong.
        return " ".join([entry, *[ref for ref in refs if ref and ref != "..."]])

    text = re.sub(rf"{CANONICAL_DETAIL_ENTRY}\(\[(.*?)\]\)", as_arguments, text)
    return text.replace(CANONICAL_DETAIL_ENTRY, entry)

