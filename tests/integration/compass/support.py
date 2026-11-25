"""Shared offline host helpers for Compass integration tests."""

import asyncio
import json
from dataclasses import replace

from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.tool_kinds import set_tool_kind
from chrys.kernel import ChatResponseUpdate, Content, ResponseStream, tool
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.session_host import ChrysSessionHost
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from compass.adapters.icode.interactive_model import SelectedModel
from compass.ports import ModelReply
from tests.orchestration._session_host_helpers import (
    _make_agent_registry,
    _make_profile,
    _make_settings_and_models,
    _patch_runtime,
)


class RetryClient(MockChatClient):
    def __init__(self, responses, failures=(), *, failure_kind="disconnect"):
        super().__init__(responses=responses)
        self.failures = set(failures)
        self.failure_kind = failure_kind
        self.requests = []
        self.inspect = lambda: None
        self.ledgers = []

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.requests.append(([m.text for m in messages], stream))
        self.ledgers.append(self.inspect())
        if len(self.requests) not in self.failures:
            return super()._inner_get_response(messages=messages, stream=stream, options=options, **kwargs)

        async def fail():
            if self.failure_kind == "stall":
                await asyncio.Event().wait()
            raise ConnectionError("synthetic network disconnect")

        async def updates():
            if self.failure_kind == "shown":
                yield ChatResponseUpdate(contents=[Content.from_text("visible answer")], role="assistant")
            else:
                yield ChatResponseUpdate(
                    contents=[Content.from_text_reasoning(text="unfinished thought")], role="assistant"
                )
            await fail()

        return ResponseStream(updates()) if stream else fail()


ROOT_TASK = "Return an empty list for empty input."
FOLLOWUP = "Also support empty strings."


def read():
    return MockResponse(tool_calls=[("read_file", "r1", {"path": "a.py"})])


def handoff():
    return MockResponse(tool_calls=[("start_compass_brief", "h1", {"exploration_summary": "a.py inspected"})])


def write():
    return MockResponse(tool_calls=[("write_file", "w1", {"path": "a.py", "content": "fixed"})])


def update():
    return MockResponse(tool_calls=[("update_compass_requirements", "u1", {})])


def initial_responses():
    return [read(), handoff(), MockResponse(text="implemented"), MockResponse(text="checked")]


class MaintenanceProbe:
    def __init__(self):
        self.extractions = []
        self.prompts = []

    async def complete(self, *, system_prompt, user_prompt, response_schema, max_output_tokens, timeout_seconds):
        self.prompts.append(user_prompt)
        if response_schema["title"] == "IssueRequirementExtraction":
            self.extractions.append(user_prompt)
            requirements = [{"id": "R1", "text": ROOT_TASK}]
            if FOLLOWUP in user_prompt:
                requirements.append({"id": "R2", "text": FOLLOWUP})
            payload = {"requirements": requirements, "constraints": [], "signature_changes": []}
        else:
            payload = {key: [] for key in response_schema["required"]}
            if response_schema["title"] == "ClaimMaintenanceDelta":
                data, _ = json.JSONDecoder().raw_decode(user_prompt.split("INPUT:\n", 1)[1])
                payload["requirement_coverage"] = [
                    {"requirement_id": row["id"], "covered_by": []} for row in data["goal"]["requirements"]
                ]
        return ModelReply(json.dumps(payload))


async def host_for(tmp_path, monkeypatch, responses, *, stream=False, failures=(), extra_clients=(), stored=False):
    reads, writes = [], []

    @tool
    async def read_file(path: str) -> str:
        """Read synthetic source."""
        reads.append(path)
        return "def f(items): return list(items)"

    @tool
    async def write_file(path: str, content: str) -> str:
        """Write synthetic code."""
        writes.append(path)
        return "written"

    set_tool_kind(read_file, "filesystem.read")
    set_tool_kind(write_file, "filesystem.write")
    client = RetryClient(responses, failures)
    _patch_runtime(monkeypatch, [client, *extra_clients], [read_file, write_file])
    probe = MaintenanceProbe()

    async def maintain(self, **kwargs):
        return await probe.complete(**kwargs)

    monkeypatch.setattr(SelectedModel, "complete", maintain)
    monkeypatch.setattr(TurnBindings, "_BACKOFF_SCHEDULE", (0,))
    settings, models = _make_settings_and_models()
    settings = replace(settings, max_transient_retries=1)
    models.get("mock-profile").stream = stream
    models.get("mock-profile").chat_options = json.dumps({"store": stored})
    models.register(ModelProfile(id="second", name="Second", provider="mock", model_id="second", stream=stream))
    profile = _make_profile()
    profile.extensions.append("compass")
    host = ChrysSessionHost(
        profile_name=profile.name,
        settings=settings,
        agent_registry=_make_agent_registry(profile),
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
        cwd=str(tmp_path),
        surface=SessionSurface.TUI,
    )
    return host, client, probe, reads, writes
