"""Public FastAPI counterexample: changing a failed test is not checking its old scope."""

import json
from dataclasses import asdict, replace

import pytest

from chrys.kernel import Content, Message
from chrys.service.llm.mock import MockResponse
from compass.briefing.render import render_active_brief
from compass.claims.models import ClaimRecord, ClaimSnapshot, ClaimState, RequirementCoverage, parse_claims
from compass.evidence.models import EvidenceRecord
from compass.task_contract.models import GoalConclusion, GoalState
from tests.integration.compass.support import ROOT_TASK, host_for, read
from tests.integration.compass.test_adapter_information import (
    attached as attached,
)
from tests.integration.compass.test_adapter_information import (
    call_for,
    deliver,
    origin_for,
    records,
    result_for,
)
from tests.integration.compass.test_tool_interface import runtime_for

TASK = "Route-level deprecated=False must override router deprecated=True."
FAILURE = "Route deprecated=False inherited true; expected no Deprecation header, got true (2 failed, 29 passed)."
DECISION = "The implementation uses or; I will replace the failed False override test with a successor URL case."


def merged_case():
    failed = ClaimRecord("C-031", FAILURE, "observation", "icode", 112, verdict="supported", evidence_refs=("E-112",))
    edited = ClaimRecord(
        "C-032",
        "Changed the failed False case to a successor URL test.",
        "observation",
        "icode",
        113,
        verdict="supported",
        evidence_refs=("E-113",),
    )
    passed = ClaimRecord(
        "C-034",
        "New test suite: 31 passed.",
        "observation",
        "icode",
        115,
        verdict="supported",
        evidence_refs=("E-115",),
    )
    merged = ClaimRecord(
        "C-035",
        "Test history: " + FAILURE + " Changed tests. 31 passed.",
        "observation",
        "icode",
        115,
        verdict="supported",
        supporting_claim_ids=("C-031", "C-032", "C-034"),
    )
    snapshot = ClaimSnapshot(
        ClaimState(
            revision=9, active_ids=("C-035",), requirement_coverage={"R1": RequirementCoverage(("C-035",), "repo")}
        ),
        (failed, edited, passed, merged),
    )
    evidence = (
        EvidenceRecord(
            112, "error", "error", "zsh", target="python -m pytest tests/test_headers.py | tail -40", exit_code=1
        ),
        EvidenceRecord(113, "change", "ok", "edit_file", target="tests/test_headers.py"),
        EvidenceRecord(
            115, "test", "ok", "zsh", target="python -m pytest tests/test_headers.py | tail -20", exit_code=0
        ),
    )
    goal = GoalState(TASK, (GoalConclusion(TASK, tag="criteria"),))
    return snapshot, goal, evidence


def focus(snapshot, goal, evidence, budget=6000):
    text = render_active_brief(snapshot, goal, evidence, brief_budget_chars=budget)
    assert len(text) <= budget
    return text.partition("Verification focus")[2]


@pytest.mark.parametrize("budget", [2500, 6000])
@pytest.mark.parametrize("mode", ["merge", "augment", "nested", "restore"])
def test_failed_scope_survives_changed_tests_merge_later_pass_and_restore(mode, budget):
    snapshot, goal, evidence = merged_case()
    if mode == "augment":
        snapshot = replace(
            snapshot, claims=(*snapshot.claims[:-1], replace(snapshot.claims[-1], evidence_refs=("E-115",)))
        )
    elif mode == "nested":
        outer = replace(snapshot.claims[-1], id="C-036", supporting_claim_ids=("C-035",))
        snapshot = replace(
            snapshot, claims=(*snapshot.claims, outer), state=replace(snapshot.state, active_ids=("C-036",))
        )
    elif mode == "restore":
        snapshot = parse_claims(json.loads(json.dumps(asdict(snapshot))))
    text = focus(snapshot, goal, evidence, budget)
    assert "C-031" in text and "E-112" in text
    assert "deprecated=False" in text
    assert "changed tests or unrelated passes do not resolve" in text


@pytest.mark.parametrize("resolution", ["superseded", "stale"])
def test_explicit_correction_or_same_scope_replacement_stops_failed_check_reminder(resolution):
    snapshot, goal, evidence = merged_case()
    old = snapshot.claims[0]
    old = replace(old, superseded_by="C-034") if resolution == "superseded" else replace(old, verdict="stale")
    snapshot = replace(snapshot, claims=(old, *snapshot.claims[1:]))
    assert "E-112" not in focus(snapshot, goal, evidence)


def test_cycle_and_missing_child_cannot_hide_real_failed_leaf_or_invent_errors():
    snapshot, goal, evidence = merged_case()
    parent = replace(snapshot.claims[-1], supporting_claim_ids=("C-035", "C-999", "C-031"))
    snapshot = replace(snapshot, claims=(*snapshot.claims[:-1], parent))
    assert "E-112" in focus(snapshot, goal, evidence)
    assert "E-112" not in focus(
        snapshot, goal, tuple(replace(e, target="grep missing a.py") if e.sequence == 112 else e for e in evidence)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failures", [(), (2,)])
async def test_native_public_decision_reaches_maintenance_before_tools_and_is_not_evidence(
    tmp_path, monkeypatch, stream, failures
):
    first = MockResponse(
        text=DECISION, reasoning_text="PRIVATE REASONING", tool_calls=[("read_file", "r0", {"path": "a.py"})]
    )
    host, client, probe, _, _ = await host_for(
        tmp_path,
        monkeypatch,
        [first, read(), read(), read(), MockResponse(text="done")],
        stream=stream,
        failures=failures,
    )
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)
        inputs = [json.JSONDecoder().raw_decode(p.split("INPUT:\n", 1)[1])[0] for p in probe.prompts if "INPUT:\n" in p]
        statements = [s for p in inputs for s in p["agent_statements"]]
        assert any(s["text"] == DECISION for s in statements)
        assert len([e for e in records(runtime) if e["source"] == "assistant_reply" and e["text"] == DECISION]) == 1
        assert len(runtime.gate.session.evidence.snapshot()) == 4
        assert all(DECISION not in e.excerpt for e in runtime.gate.session.evidence.snapshot())
        assert len(client.call_history) == 5
        assert len(client.requests) == 5 + len(failures)
        assert all("PRIVATE REASONING" not in s["text"] for s in statements)
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_edit_arguments_are_forwarded_with_multiline_assertions_and_restore(attached):
    runtime, _ = attached
    args = {
        "path": "tests/test_headers.py",
        "old_string": "@router.get('/x', deprecated=False)\nassert 'deprecation' not in r.headers",
        "new_string": "@router.get('/x', successor_url='/next')\nassert '/next' in r.headers['link']",
    }
    await deliver(runtime, call_for(runtime, "filesystem.write", args), result_for(text="changed"))
    await runtime.gate.inspect([])
    item = runtime.gate.session.evidence.snapshot()[-1]
    assert json.loads(item.action_input) == args
    from compass.claims.maintenance import _evidence_payload

    assert json.loads(_evidence_payload(item)["action_input"]) == args
    from tests.integration.compass.test_independent_persistence import reopen

    reopened = reopen(runtime)
    try:
        await reopened.inspect([])
        assert reopened.gate.session.evidence.snapshot()[-1].action_input == item.action_input
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_public_capture_excludes_reasoning_and_deduplicates_retries(attached):
    runtime, _ = attached
    message = Message(
        "assistant", [Content.from_text(DECISION), Content.from_text_reasoning(text="PRIVATE")], message_id="visible-1"
    )
    for _ in range(2):
        await runtime.information.assistant_messages([message, Message("user", ["injected"])], origin_for(runtime))
    pending = runtime.gate.session.statements.pending
    assert sum(s["text"] == DECISION for s in pending) == 1
    assert all("PRIVATE" not in s["text"] and "injected" not in s["text"] for s in pending)


@pytest.mark.asyncio
async def test_public_capture_keeps_call_free_replies_without_pairing_tools(attached):
    runtime, _ = attached
    previous_texts = [row["text"] for row in runtime.gate.session.statements.pending]
    previous_replies = [row for row in records(runtime) if row["source"] == "assistant_reply"]
    messages = [
        Message("assistant", [Content.from_text("Plan without tools")], message_id="plain"),
        Message(
            "assistant",
            [Content.from_text("Public decision"), Content.from_function_call("shared", "shell", arguments={})],
            message_id="with-call",
        ),
        Message("tool", [Content.from_function_result("shared", result="TOOL OUTPUT")]),
        Message("user", ["INJECTED USER"]),
        Message("assistant", [Content.from_text("Final without tools")], message_id="final"),
    ]
    await runtime.information.assistant_messages(messages, origin_for(runtime))
    assert [row["text"] for row in runtime.gate.session.statements.pending] == [
        *previous_texts,
        "Plan without tools",
        "Public decision",
        "Final without tools",
    ]
    replies = [row for row in records(runtime) if row["source"] == "assistant_reply"]
    assert replies[: len(previous_replies)] == previous_replies
    assert [row["provider_call_ids"] for row in replies[len(previous_replies) :]] == [[], ["shared"], []]


@pytest.mark.parametrize(
    "prefix",
    ["uv --project repo run", "poetry --project repo run", "gradle --project-dir repo"],
)
def test_external_project_options_keep_test_command_semantics(prefix):
    from compass.evidence.actions import is_test_command

    command = f"{prefix} test" if prefix.startswith("gradle") else f"{prefix} pytest tests/test_headers.py"
    assert is_test_command(command)
    assert not is_test_command(f"{prefix} --help")
