"""Pi persisted messages -> distinct user contract / assistant statement inputs.

Only finalized branch entries are read. Streaming updates are never separately
counted. Custom Briefs/checkpoints and tool results are not assistant replies.
"""

import json
from hashlib import sha256


def text(content):
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content or [] if p.get("type") == "text")


def identity(entry):
    # Real Pi entries always have IDs. Legacy fixture/import fallback remains
    # deterministic; adapters with no stable message identity need their own ID.
    return entry.get("id") or "legacy:" + sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()


async def accept_message(session, entry, order, users):
    msg = entry.get("message", {})
    role = msg.get("role")
    if role not in {"assistant", "user"}:
        return
    body = text(msg.get("content"))
    if not body.strip():
        return
    key = identity(entry)
    if role == "assistant":
        session.record_assistant_reply(
            message_id=key,
            text=body,
            order=order,
            tool_call_ids=[
                part["id"]
                for part in msg.get("content", [])
                if isinstance(part, dict) and part.get("type") == "toolCall"
            ],
            status="partial" if msg.get("stopReason") in {"aborted", "error", "length"} else "complete",
        )
    elif key not in users:
        users[key] = {"text": body, "order": order}
        session.record_user_input(message_id=key, text=body, order=order)
        if not session.goals.snapshot().description:
            await session.start(body)
