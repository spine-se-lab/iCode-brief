"""Pi worker: real Compass core, host-owned model calls.

One request per process. Credentials are never sent over this pipe. Every result
owns a fresh artifact directory, so an abandoned operation cannot overwrite the
accepted state or another branch's raw Evidence.
"""

import asyncio
import json
import os
import stat
import sys
import tempfile
import traceback
from dataclasses import asdict
from pathlib import Path

from compass_brief.evidence.observation import ToolObservation, record_observation
from compass_brief.model_calls.policy import ModelCallError
from compass_brief.ports import ModelReply
from compass_brief.session import CompassSession
from compass_brief.task_contract.extraction import requirement_entries

from .brief_view import brief_view
from .conversation_inputs import accept_message, identity, text


def send(packet):
    """One bounded JSON packet on the private parent/worker pipe, not a log."""
    sys.stdout.write(json.dumps(packet) + "\n")
    sys.stdout.flush()


class Artifacts:
    def __init__(self, root):
        self.root = root.resolve(strict=True)

    def checked(self, path):
        path = Path(path)
        if not path.is_absolute() or not path.is_relative_to(self.root):
            raise ValueError("artifact outside configured data root")
        for parent in (path, *path.parents):
            if parent == self.root:
                break
            if parent.is_symlink():
                raise ValueError("symlink in artifact path")
        return path

    def write_text(self, path, payload):
        path = self.checked(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def open_verified_binary(self, path):
        fd = os.open(self.checked(path), os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(fd)
            raise ValueError("unsafe artifact")
        return os.fdopen(fd, "rb")


class Events:
    def __init__(self):
        self.names = []
        self.brief = {}

    async def publish(self, event):
        self.names.append(type(event).__name__)
        if type(event).__name__ == "BriefUpdated":
            self.brief = asdict(event)


class HostModel:
    def __init__(self, reader):
        self.reader = reader
        self.sequence = 0

    async def complete(self, **request):
        self.sequence += 1
        request_id = self.sequence
        send({"type": "model", "id": request_id, "request": request})
        try:
            while True:
                reply = json.loads(await self.reader.readline())
                reply_id = reply.get("id")
                if not isinstance(reply_id, int) or isinstance(reply_id, bool) or reply_id > request_id:
                    raise ValueError("model response mismatch")
                if reply_id == request_id:
                    break
                # A timeout can cancel readline before Node's corresponding
                # response reaches the pipe. It belongs to the abandoned call,
                # never to this retry. No concurrent complete() calls here.
        except asyncio.CancelledError:
            send({"type": "cancel_model", "id": request_id})
            raise
        if reply.get("error"):
            raise ModelCallError(reply["error"])
        return ModelReply(reply["text"], reply.get("usage"))


async def run(reader, request):
    # macOS /var is an OS alias of /private/var. Canonicalize the trusted root
    # before creating paths; retain no-symlink checks below that root.
    root = Path(request["data_root"]).resolve(strict=True)
    directory = Path(tempfile.mkdtemp(prefix="snapshot-", dir=root))
    artifacts, events = Artifacts(root), Events()
    model = HostModel(reader) if request.get("live") else None
    session = CompassSession(
        artifacts=artifacts,
        events=events,
        model=lambda: model,
        directory=directory,
        session_id=request["session_id"],
        repo_path=lambda: request["cwd"],
    )
    await session.restore_session_state({"compass_brief": request.get("state")})
    adapter = (request.get("state") or {}).get("pi_inputs", {})
    users = dict(adapter.get("users", {}))
    tool_messages = dict(adapter.get("tool_messages", {}))
    if request.get("task") and not session.goals.snapshot().description:
        await session.start(request["task"])
    calls = {}
    for entry in request.get("entries", []):
        msg = entry.get("message", {})
        if msg.get("role") == "assistant":
            for part in msg.get("content", []):
                if part.get("type") == "toolCall":
                    calls[part["id"]] = part
    seen = {item.invocation_id for item in session.evidence.snapshot() if item.invocation_id}
    ordered = {entry.get("id"): index for index, entry in enumerate(request.get("entries", [])) if entry.get("id")}
    for index, entry in enumerate(request.get("tail", [])):
        msg = entry.get("message", {})
        await accept_message(session, entry, ordered.get(entry.get("id"), index), users)
        if msg.get("role") != "toolResult" or msg.get("toolCallId") in seen:
            continue
        receipt = (msg.get("details") or {}).get("codehelixCompassExecution")
        if not receipt:
            # No proof the tool ran: rejected calls and legacy unmarked tails
            # stay in Pi history, not Compass Evidence. Existing state is kept.
            continue
        if (
            receipt.get("version") != 1
            or receipt.get("toolCallId") != msg["toolCallId"]
            or receipt.get("toolName") != msg.get("toolName")
            or not isinstance(receipt.get("input"), dict)
        ):
            raise ValueError("invalid execution receipt")
        call = calls.get(msg["toolCallId"])
        if not call:
            raise ValueError("tool input missing")
        if call["name"] != receipt["toolName"]:
            raise ValueError("execution receipt tool mismatch")
        name, args = receipt["toolName"], dict(receipt["input"])
        if name == "inspect_compass_brief":
            # Inspecting our own projection is not independent external Evidence.
            continue
        result = text(msg.get("content"))
        kind = {
            "read": "filesystem.read",
            "edit": "filesystem.write",
            "write": "filesystem.write",
            "bash": "shell",
            "grep": "search",
            "find": "search",
        }.get(name, "")
        if name == "read":
            start = args.get("offset", 1)
            count = max(1, len(result.splitlines()))
            count = min(count, args.get("limit", count))
            args["line_range"] = [start, start + count - 1]
            if (msg.get("details") or {}).get("truncation", {}).get("truncated"):
                result += "\n[output truncated]"
        observation = ToolObservation(
            tool_name="read_file" if name == "read" else name,
            tool_kind=kind,
            arguments=args,
            result=result,
            failed=bool(msg.get("isError")),
            invocation_id=msg["toolCallId"],
            working_dir=request["cwd"],
            step=len(session.evidence.snapshot()) + 1,
        )
        if request.get("live"):
            await session.observe(observation)
        else:
            await record_observation(session.evidence, observation)
        seen.add(msg["toolCallId"])
        tool_messages[identity(entry)] = {
            "order": ordered.get(entry.get("id"), index),
            "tool_call_id": msg["toolCallId"],
            "evidence_sequence": session.evidence.snapshot()[-1].sequence,
        }
    if not request.get("live"):
        # Rebuild pending policy after replay, without making historical model calls.
        await session.restore_session_state(session.contribute_session_state())
    if request.get("maintain") and (
        session.maintenance_status().pending_evidence or session.statements.pending or session.user_inputs.pending
    ):
        await session.request_maintenance()
    state = session.contribute_session_state()["compass_brief"]
    brief = session.brief_for_request()
    snapshot = session.claims.snapshot()
    outbox = session.delivery_intent()
    if state is not None:
        state["pi_inputs"] = {"users": users, "tool_messages": tool_messages}
    artifacts.write_text(directory / "state.json", json.dumps(state))
    artifacts.write_text(directory / "model-brief.txt", brief)
    snapshot = session.claims.snapshot()
    await session.publish_brief()
    return {
        "state": state,
        "brief": brief,
        "delivery": outbox,
        "view": await brief_view(session, events.brief),
        "requirements": [
            {"id": key, "text": content} for key, content in requirement_entries(session.goals.snapshot())
        ],
        "inspection": await session.inspect(request["inspect"]) if "inspect" in request else "",
        "summary": {
            "evidence": len(session.evidence.snapshot()),
            "claims": len(snapshot.state.active_ids),
            "revision": snapshot.state.revision,
            "processed": snapshot.state.processed_evidence_seq,
            "assistant_captured": len(session.statements.seen),
            "assistant_pending": len(session.statements.pending),
            "user_pending": len(session.user_inputs.pending),
            "links": {key: list(row.claim_ids) for key, row in snapshot.state.requirement_coverage.items()},
            "status": asdict(session.maintenance_status()),
            "fallback": any(
                item.tag == "extraction" and "fallback" in item.content for item in session.goals.snapshot().conclusions
            ),
        },
        "events": events.names,
    }


async def main():
    # Snapshots and actual tool output can exceed asyncio's default 64 KiB line
    # limit. This is a bounded local protocol, not an unbounded network reader.
    reader = asyncio.StreamReader(limit=8 * 1024 * 1024)
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    request = json.loads(await reader.readline())
    result = await run(reader, request)
    send({"type": "result", "result": result})


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
        frames = [
            f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
            for frame in traceback.extract_tb(exc.__traceback__)[-4:]
        ]
        send({"type": "error", "code": type(exc).__name__, "frames": frames})
        sys.exit(1)
