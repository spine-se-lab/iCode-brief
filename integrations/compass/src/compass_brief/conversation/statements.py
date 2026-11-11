"""Incremental conversation statements, deliberately separate from tool Evidence.

Adapters submit finalized host messages, not streaming deltas. Identity is the
host's branch-stable message ID. Acknowledgement only follows successful core
maintenance; prompt-budget overflow remains pending across checkpoints.
"""

from copy import deepcopy
from hashlib import sha256

# Per maintenance window. Replies carry their whole reasoning; a long block is
# read in order across consecutive windows instead of being cut to its tail.
STATEMENT_BUDGET = 40000
STATEMENT_ITEM_CHARS = 40000


class StatementBuffer:
    def __init__(self, state=None):
        state = state or {"seen": {}, "pending": []}
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("seen"), dict)
            or not isinstance(state.get("pending"), list)
        ):
            raise ValueError("invalid assistant statement buffer")
        self.seen = dict(state["seen"])
        self.pending = deepcopy(state["pending"])
        for item in self.pending:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("text"), str)
                or item.get("message_id") not in self.seen
                or type(item.get("offset")) is not int
                or not 0 <= item["offset"] < len(item["text"])
            ):
                raise ValueError("invalid pending assistant statement")

    def record(
        self, *, message_id, text, order, evidence_anchor, tool_call_ids=(), status="complete", source="assistant"
    ):
        if source not in {"user", "assistant"}:
            raise ValueError("unsupported conversation source")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("assistant message requires stable identity")
        if not text.strip():
            return False
        digest = sha256(text.encode()).hexdigest()
        if message_id in self.seen:
            if self.seen[message_id] != digest:
                raise ValueError("finalized assistant message changed under the same identity")
            return False
        self.seen[message_id] = digest
        self.pending.append(
            {
                "message_id": message_id,
                "source": source,
                "order": order,
                "step": evidence_anchor,
                "tool_call_ids": list(tool_call_ids),
                "status": status,
                "text": text,
                "offset": 0,
            }
        )
        return True

    def batch(self):
        # Match the existing maintenance statement budget without silently
        # acknowledging text that was omitted from the model request.
        remaining, result = STATEMENT_BUDGET, []
        for item in self.pending:
            if not remaining:
                break
            start = item["offset"]
            excerpt = item["text"][start : start + min(STATEMENT_ITEM_CHARS, remaining)]
            result.append(
                {**item, "text": excerpt, "end_offset": start + len(excerpt), "total_chars": len(item["text"])}
            )
            remaining -= len(excerpt)
        return tuple(result)

    def acknowledge(self, batch):
        consumed = {item["message_id"]: item["end_offset"] for item in batch}
        for item in self.pending:
            item["offset"] = consumed.get(item["message_id"], item["offset"])
        self.pending = [item for item in self.pending if item["offset"] < len(item["text"])]

    def serialize(self):
        return {"seen": dict(self.seen), "pending": deepcopy(self.pending)}
