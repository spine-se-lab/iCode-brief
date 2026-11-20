"""Project a Brief into Chrys Feature additions without editing history."""

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import Message

from compass_brief.briefing.delivery import DeliveryStrategy
from compass_brief.briefing.policy import DeliveryFacts, DeliveryMark, delivery_reason
from compass_brief.briefing.render import CONFLICT_REVIEW_GUIDANCE

_SOURCE = "compass_brief"
_PREFIX = "feature:compass_brief:"
_MARK_KEY = "_chrys_feature_metadata"
_HEADER = (
    "[Compass Brief — current state]\n"
    "This is the latest Compass summary. Where it differs from older Compass summaries, "
    "use this update; retain the original task, constraints and tool evidence.\n\n"
)


def conflict_notice(lines: list[str]) -> str:
    """A new requirement conflict, put to the agent once where it is working."""
    return (
        "[Compass — new requirement conflict]\n"
        + "\n".join(lines)
        + "\nBefore continuing, re-read the quoted requirement in the task text and state in one sentence whether "
        "your behavior follows it. If it does not, change the code. "
        + CONFLICT_REVIEW_GUIDANCE
    )


def delegated_prompt(brief: str, prompt: str, messages: list[Message]) -> str:
    """Carry actual branch user text alongside the delegating agent's interpretation.

    Read host history at dispatch time: a second copy/cursor would become stale
    after a fork, restore or compaction. Never relabel summaries as user text.
    """
    user_text = "\n\n".join(
        f"[User message {index + 1}]\n{message.text}"
        for index, message in enumerate(messages)
        if message.role == "user"
        and message.text.strip()
        and not message.additional_properties.get(HistoryMarkerKind.KEY)
        and not message.additional_properties.get("_chrys_feature_append")
    )
    # Bound extra child context; preserve both initial wording and recent
    # corrections, with exact ranges rather than a silently rewritten contract.
    if len(user_text) > 16000:
        user_text = (
            f"[Excerpt: first 8000 of {len(user_text)} characters]\n{user_text[:8000]}\n"
            f"[Middle omitted; final 8000 characters follow]\n{user_text[-8000:]}"
        )
    return (
        brief + "\n\n[Parent user context — available active-branch history]\n"
        "The quoted user messages are chronological; later user corrections take precedence. "
        "History may be incomplete after compaction. Missing text does not waive requirements. "
        "Use the delegated request below to scope your work, but its interpretations and the Brief "
        "are not replacements for user requirements. Flag any narrower assumption when reviewing.\n\n"
        + (user_text or "[No original user text available; do not treat the delegated wording as a user quote.]")
        + "\n\n[Delegated request — agent-authored]\n"
        + prompt
    )


def brief_additions(
    text: str,
    messages: list[Message],
    strategy: DeliveryStrategy,
    *,
    facts: DeliveryFacts | None = None,
    task_key: str = "",
) -> list[Message]:
    if not text:
        return []
    if strategy == "request_only":
        return [Message("user", [text])]
    payload = _HEADER + text
    latest = next(
        (
            message
            for message in reversed(messages)
            if str(message.additional_properties.get(HistoryMarkerKind.INJECTION_ID_KEY, "")).startswith(_PREFIX)
        ),
        None,
    )
    # The active history is authoritative: after compaction/removal, re-deliver
    # even an unchanged Brief. No separate cursor can get ahead of persistence.
    mark = DeliveryMark()
    if facts is not None and latest is not None:
        saved = latest.additional_properties.get(_MARK_KEY)
        if isinstance(saved, dict) and saved.get("task_key") == task_key:
            try:
                revision, degraded, escalations = saved["revision"], saved["degraded"], saved["escalations"]
                valid = (
                    type(revision) is int
                    and 0 <= revision <= facts.revision
                    and type(degraded) is bool
                    and isinstance(escalations, list)
                    and all(
                        isinstance(pair, list) and len(pair) == 2 and all(isinstance(value, str) for value in pair)
                        for pair in escalations
                    )
                )
                if valid:
                    mark = DeliveryMark(revision, frozenset(map(tuple, escalations)), degraded)
                    if not delivery_reason(facts, mark):
                        return []
            except KeyError:
                pass  # Legacy/malformed history receives one fresh marked Brief.
    elif facts is None and latest is not None and latest.text == payload:
        return []
    result = Message("user", [payload])
    result.additional_properties["_chrys_feature_append"] = _SOURCE
    if facts is not None:
        mark = mark.after(facts)
        # Persist the trigger with the host's injection transaction, never ahead
        # of it. Restores/forks read their own history; compaction re-delivers.
        result.additional_properties[_MARK_KEY] = {
            "task_key": task_key,
            "revision": mark.revision,
            "degraded": mark.degraded,
            "escalations": [list(pair) for pair in sorted(mark.escalations)],
        }
    return [result]
