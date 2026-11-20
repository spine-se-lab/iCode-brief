"""Core-owned delivery intent; adapters only append/deduplicate in host history."""

from copy import deepcopy
from hashlib import sha256

from compass.core.briefing.policy import weak_requirements


class BriefNotifications:
    def __init__(self, state=None):
        state = state or {}
        self.intent = deepcopy(state.get("intent"))
        self.reminders = list(state.get("reminders", []))
        if self.intent is not None and (
            not isinstance(self.intent, dict)
            or not isinstance(self.intent.get("id"), str)
            or not isinstance(self.intent.get("text"), str)
            or self.intent.get("reason") not in {"maintenance", "reminder"}
        ):
            raise ValueError("invalid Brief delivery intent")

    def update(self, snapshot, requirements, *, step, text, maintained=False):
        reminders = [
            [key, severity] for _, key, severity, _ in weak_requirements(snapshot, requirements, current_step=step)
        ]
        reason = (
            "maintenance"
            if maintained
            else "reminder"
            if any(item not in self.reminders for item in reminders)
            else None
        )
        if not reason or not text:
            return
        revision = snapshot.state.revision
        self.intent = {
            "id": sha256(f"{revision}:{text}".encode()).hexdigest(),
            "text": text,
            "reason": reason,
            "revision": revision,
        }
        self.reminders = reminders

    def serialize(self):
        return {"intent": deepcopy(self.intent), "reminders": deepcopy(self.reminders)}
