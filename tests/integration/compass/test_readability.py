"""New checks for named delivery/state records, without replaying the old suite."""

from dataclasses import asdict, fields

import pytest

from chrys.kernel import Message
from chrys.service.agent_middleware.injection import InjectionMiddleware
from compass.adapters.icode.middleware import BriefChat, PendingBrief
from compass.adapters.icode.storage import Artifacts
from compass.briefing.policy import DeliveryFacts
from compass.evaluation.records import BudgetExhausted, InvalidCompassRun
from compass.evaluation.run_state import RunState, WorkflowFlags
from compass.evaluation.workflow import prepare_submission_check, require_handoff
from compass.provenance import SOURCE_ROOT, source_constants

FLAG_NAMES = (
    "started",
    "handoff_seen",
    "rules_seen",
    "closing_seen",
    "check_done",
    "handoff_requested",
    "write_requested",
)


def state(tmp_path):
    return RunState("task", tmp_path, tmp_path, source_constants(SOURCE_ROOT), artifacts=Artifacts())


def test_flags_are_independent_and_have_exact_existing_wire_names():
    assert tuple(field.name for field in fields(WorkflowFlags)) == FLAG_NAMES
    first, second = WorkflowFlags(), WorkflowFlags()
    first.write_requested = True
    assert asdict(second) == dict.fromkeys(FLAG_NAMES, False)
    assert first.write_requested and not first.started and not first.handoff_seen


def test_named_pending_brief_keeps_headless_delivery_behavior(tmp_path):
    run = state(tmp_path)
    run.flags.started = True
    chat = BriefChat(run, InjectionMiddleware())
    facts = DeliveryFacts(revision=7, escalations=(("R1", "REVIEW"),))
    chat.pending = PendingBrief("brief text", facts, {"R1 conflict"}, "maintenance")
    chat.observe_request([Message("user", ["other text"])])
    assert not run.flags.handoff_seen and chat.pending is not None
    chat.observe_request([Message("user", ["brief text"])])
    assert chat.pending is None and run.flags.handoff_seen and run.flags.rules_seen
    assert run.mark.revision == 7 and run.conflicts == {"R1 conflict"}


def test_validity_and_submission_budget_still_use_the_independent_flags(tmp_path):
    run = state(tmp_path)
    for flag in ("started", "handoff_seen", "rules_seen"):
        with pytest.raises(InvalidCompassRun):
            require_handoff(run)
        setattr(run.flags, flag, True)
    require_handoff(run)
    run.calls = 236
    held, _prompt = prepare_submission_check(run)
    assert not held and run.flags.check_done
    assert prepare_submission_check(run) is None
    run.calls = 250
    with pytest.raises(BudgetExhausted):
        run.check_budget()
