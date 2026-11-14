"""Configure an isolated trial, then invoke iCode's native headless Code agent.

The paired arms use the same builtin profile, model and request observer.
Only the treatment discovers Compass and appends its tool-use instructions.
No task code, grading logic, reference patch or historical case report is read.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import sysconfig
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

from compass_brief.icode.clarification import ARMS, consume_contract_reference, digest, input_contract

TOOL_INSTRUCTIONS = """

## Compass Brief tools
Before implementing, explore the task's relevant public entry points, implementation,
callers and existing tests. Target the affected behavior; do not scan the entire repo.
Then call start_compass_brief(exploration_summary=...) as a separate tool step. Summarize
what you found with file/symbol or Evidence references, boundaries to preserve, and any
unresolved assumptions. Do not replace the original issue with your interpretation.
This is the explicit exploration-complete handoff: Compass derives Goal, requirements
(R) and constraints (K) from the original task, supplied task guidance and recorded
repository observations, and returns the first Brief. Wait for it, review G/R/K, and
only then edit files or delegate implementation. Never batch this handoff with edits.
Read-only exploration may be delegated before the handoff. Your summary is an agent
statement, not proof of repository behavior; state unknowns rather than inventing facts.
Compass records tool evidence (E). After the handoff it appends a bounded Brief after
each maintenance: R/K with their
evidence status, claims (C) drawn from tool evidence and from your own statements and
reasoning, and tensions where a claim disagrees with a requirement or with evidence you
already saw. Use inspect_compass_brief with R/K, C or E IDs to read an item in full.
A supported claim proves only its stated scope; an agent-only claim is an unverified
assumption. Settle each tension with a concrete check through a public entry point,
not by dismissing either side. The Brief is context, not a substitute for inspecting
code and running tests. Before finishing, map every task clause to the affected public
entry points and the scope your checks actually exercised; passing your own tests only
proves that scope. When a decision is unverified, name the assumption and run a check
that could disprove it. If you change a test's expected result, input or entry point
after a failure, explain the contract basis and recheck the original failing condition;
a passing replacement check alone does not resolve it. Report remaining uncertainty
instead of silently narrowing the task.
"""


def configure(logs: Path, *, treatment: bool, arm: str | None = None) -> dict:
    private_reference = consume_contract_reference(logs) if arm == "arm2_grk_reference" else {}
    import chrys

    config = Path.home() / ".chrys"
    config.mkdir(exist_ok=True)
    models = config / "models"
    models.mkdir(exist_ok=True)
    profile = {
        "id": "c0216pilotmodel",
        "name": "Compass Pilot Model",
        "provider": "openai",
        "model_id": "deepseek/deepseek-v4.1-flash",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "{{OPENROUTER_API_KEY}}",
        "stream": False,
        "max_context_tokens": 1000000,
        "max_output_tokens": 64000,
        "chat_options": json.dumps(
            {
                "extra_body": {
                    "provider": {"only": ["deepseek"], "allow_fallbacks": False},
                    "reasoning": {"effort": "max"},
                }
            }
        ),
    }
    (models / "c0216pilotmodel.yaml").write_text(yaml.safe_dump(profile))
    builtin = Path(chrys.__file__).parent / "service/profiles/agents/builtins/Code.yaml"
    original = yaml.safe_load(builtin.read_text())
    identity = {
        "arm": arm or ("arm2" if treatment else "baseline"),
        "builtin_prompt_sha256": hashlib.sha256(original["instructions"].encode()).hexdigest(),
        "max_output_tokens": profile["max_output_tokens"],
    }
    if treatment:
        agents = config / "agents"
        agents.mkdir(exist_ok=True)
        enhanced = {**original, "name": "CodeBrief", "id": "c0216pilotbrief", "display_name": "Code + Compass"}
        enhanced["instructions"] = original["instructions"] + TOOL_INSTRUCTIONS
        enhanced["tools"] = {**original["tools"], "builtins": [*original["tools"]["builtins"], "compass_brief"]}
        (agents / "CodeBrief.yaml").write_text(yaml.safe_dump(enhanced, sort_keys=False))
        configuration = {
            "data_location": "host_session",
            "brief_delivery_strategy": "append",
            "contract_after_exploration": True,
            "maintenance_response_format": "json_object",
            "maintenance_reasoning_options": '{"reasoning":{"enabled":false}}',
        }
        contract = input_contract(logs)
        if contract is not None:
            configuration["task_input_contract"] = contract
        binding = logs / "compass-configuration.json"
        if arm == "arm2_grk_reference":
            reference_metadata = json.loads((logs / "reference-input.json").read_text())
            configuration["contract_reference"] = {k: reference_metadata[k] for k in
                                                 ("has_guidance", "guidance_sha256", "authoritative_text_sha256")}
            from compass_brief.chrys.task_input import register_contract_reference

            register_contract_reference(binding, private_reference)
        binding.write_text(json.dumps({"configuration": configuration}))
        locator = Path(sysconfig.get_path("purelib")) / "codehelix_compass_brief.json"
        locator.write_text(json.dumps({"config_ref": str(binding), "package_store": str(logs / "package-store")}))
        identity["tool_instructions_sha256"] = hashlib.sha256(TOOL_INSTRUCTIONS.encode()).hexdigest()
    reference = logs / "reference-input.json"
    if reference.exists():
        input_contract(logs)  # validate the separately saved task input in either treatment
        identity["reference_input"] = json.loads(reference.read_text())
    (logs / "profile-identity.json").write_text(json.dumps(identity, indent=2))
    return private_reference


def observe_calls(logs: Path, *, contract_reference: str = "") -> None:
    """Extend the host's observer without replacing its callbacks or transport."""
    from chrys.foundation.models.history_markers import HistoryMarkerKind
    from chrys.kernel import ResponseStream
    from chrys.service.llm import observer

    sink = logs / "model-calls.jsonl"
    reference_path = logs / "reference-input.json"
    reference = json.loads(reference_path.read_text()) if reference_path.exists() else None
    rendered = (logs / "instruction.txt").read_text() if reference else None
    if reference:
        input_contract(logs)
    original = observer.WireCallObserver

    def write(row):
        with sink.open("a") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    class ObservedCall:
        def __init__(self, delegate, request):
            self.delegate, self.request = delegate, request
            self.started = time.monotonic()

        def failed(self, exc):
            write(
                {
                    **self.request,
                    "event": "failed",
                    "error_type": type(exc).__name__,
                    "seconds": time.monotonic() - self.started,
                }
            )
            self.delegate.failed(exc)

        def landed(self, response):
            write(
                {
                    **self.request,
                    "event": "response",
                    "response_id": response.response_id,
                    "finished_at": datetime.now(UTC).isoformat(),
                    "finish_reason": getattr(response, "finish_reason", None),
                    "usage": dict(response.usage_details or {}),
                    "seconds": time.monotonic() - self.started,
                }
            )
            return response

        def observe(self, result):
            observed = self.delegate.observe(result)
            if isinstance(observed, ResponseStream):
                observed.with_result_hook(self.landed)
                return observed

            async def wait():
                try:
                    response = await observed
                except BaseException as exc:
                    write(
                        {
                            **self.request,
                            "event": "failed",
                            "error_type": type(exc).__name__,
                            "seconds": time.monotonic() - self.started,
                        }
                    )
                    raise
                return self.landed(response)

            return wait()

    class PilotObserver(original):
        def begin(self, messages, options, **kwargs):
            extra = options.get("extra_body") or {}
            side = any(
                m.role == "system"
                and any(
                    title in m.text
                    for title in ("ClaimMaintenanceDelta", "IssueRequirementExtraction", "RequirementCheck")
                )
                for m in messages
            )
            request = {
                "call_id": os.urandom(8).hex(),
                "started_at": datetime.now(UTC).isoformat(),
                "kind": "compass" if side else "agent_or_host",
                "provider_options": extra.get("provider"),
                "reasoning_options": extra.get("reasoning"),
                "max_output_tokens": options.get("max_tokens"),
                "brief_hashes": [
                    hashlib.sha256(m.text.encode()).hexdigest()
                    for m in messages
                    if m.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.FEATURE_CONTEXT
                ],
            }
            if reference and reference.get("mode") == "contract_only":
                extraction = any(m.role == "system" and "IssueRequirementExtraction" in m.text for m in messages)
                visible = bool(contract_reference and any(contract_reference in m.text for m in messages))
                if visible and not extraction:
                    raise ValueError("Private contract reference reached a non-extraction request")
                request["reference_input"] = {
                    "mode": "contract_only", "guidance_sha256": reference["guidance_sha256"],
                    "has_guidance": reference["has_guidance"], "guidance_visible": visible,
                    "contract_extraction": extraction, "contract_reference_seen": extraction and visible,
                    "initial_message_seen": any(digest(m.text) == reference["rendered_text_sha256"] for m in messages),
                }
            elif reference:
                request["reference_input"] = {
                    "guidance_sha256": reference["guidance_sha256"],
                    "has_guidance": reference["has_guidance"],
                    "initial_message_seen": any(digest(m.text) == reference["rendered_text_sha256"] for m in messages),
                    "guidance_visible": bool(reference["has_guidance"] and any(rendered in m.text for m in messages)),
                }
            write({**request, "event": "request"})
            return ObservedCall(super().begin(messages, options, **kwargs), request)

    observer.WireCallObserver = PilotObserver


def main():
    logs = Path("/logs/agent")
    arm = sys.argv[1]
    if arm not in ARMS:
        raise ValueError("Unknown iCode experiment arm")
    treatment = ARMS[arm][0]
    private_reference = configure(logs, treatment=treatment, arm=arm)
    observe_calls(logs, contract_reference=private_reference.get("guidance", ""))
    os.environ["CHRYS_SESSION_ROOT_DIR"] = str(logs / "sessions")
    from chrys.app.cli.run import main as run

    return run(
        [
            "--agent",
            "CodeBrief" if treatment else "Code",
            "--model",
            "c0216pilotmodel",
            "--task",
            str(logs / "instruction.txt"),
            "--json",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
