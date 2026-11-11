# Copyright (c) 2026 Chrys. All rights reserved.

"""One mini-SWE-agent subclass that maintains a Compass session alongside the run.

mini owns the model conversation and the bash loop; Compass only reads what
actually executed and hands back a bounded Brief. The three host capabilities
Compass asks for (:mod:`compass_brief.ports`) are implemented here for a local
trial directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
import time
from dataclasses import asdict, dataclass
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Literal

from minisweagent.agents.interactive import InteractiveAgentConfig
from minisweagent.exceptions import Submitted

from compass_brief.briefing.policy import DeliveryFacts, DeliveryMark, delivery_reason
from compass_brief.events import MaintenanceFailed
from compass_brief.evidence.observation import ToolObservation
from compass_brief.mini.inspect_cli import DETAIL_SHIM, STATE_FILE
from compass_brief.mini.presentation import shell_detail_entry
from compass_brief.mini.submission import SubmissionSafeMiniAgent
from compass_brief.model_calls.policy import CallSettings, ModelCallError
from compass_brief.persistence.locations import deployment_binding
from compass_brief.ports import ModelReply
from compass_brief.session import CompassSession
from compass_brief.task_contract.extraction import requirement_entries

#: ``compass_maintenance_model`` value that runs the trial with no Maintenance
#: model at all, leaving the core's public model-free fallback in charge.
NO_MAINTENANCE_MODEL = "none"


SUBMISSION_RULES = (
    "\n\n[Compass — implementation and submission rules]\n"
    "Naming: use a distinctive prefix for new self-tests and their helper functions, types and "
    "package-level variables. A separate file does not guarantee a separate namespace. Before "
    "submission, check for collisions with visible repository declarations and run the relevant "
    "test collection/build. Rename your new self-tests when needed; do not weaken existing tests.\n"
    "Signatures: function, method and type signatures explicitly specified by the issue are "
    "contracts. Preserve their parameter count, order and types, return types and exported names; "
    "do not change them to fit your implementation. Before submission, compare each explicit "
    "signature with the final code and check existing repository callers. Use only the issue and "
    "current repository. Follow repository conventions where the issue is silent; do not invent "
    "requirements, access hidden tests, or silently resolve an unresolved issue/repository conflict."
)
SUBMISSION_CHECK = (
    "[Compass — pre-submission check]\n"
    "Recheck the naming and signature rules from the first Brief against the final diff: "
    "new self-test/helper names versus visible repository declarations; issue-explicit signatures "
    "versus implementation and existing callers; relevant collection/build after the latest edit. "
    "Use only the issue and repository. State anything unresolved; this reminder does not certify correctness."
)

# DETAIL_SHIM is imported: the Brief names one short path instead of a full
# interpreter invocation, because it is mentioned about twenty times in one Brief.

NO_DETAIL_ENTRY = "(detail query unavailable: this run configured no Compass directory)"


class TrialArtifacts:
    """`SessionArtifactStore` over one local trial directory.

    Claims, Briefs and Evidence are audit records, so writes replace atomically
    and stay owner-only, and reads refuse to follow a symlink.
    """

    def write_text(self, path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(f".{path.name}.tmp")
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with open(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(staged, path)

    def open_verified_binary(self, path: Path):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        if os.fstat(descriptor).st_uid != os.getuid():
            os.close(descriptor)
            raise PermissionError(f"{path} is not owned by this user")
        return open(descriptor, "rb")


class MaintenanceFailures:
    """`EventSink` for a run with no live UI: keeps failed maintenance windows.

    Committed Claims record what maintenance achieved, but a failed window commits
    nothing, so dropping its event left a trial whose ledger stopped mid-run looking
    like a healthy one. Every other event is still dropped.
    """

    def __init__(self) -> None:
        self.windows: list[dict] = []

    async def publish(self, event: object) -> None:
        if isinstance(event, MaintenanceFailed):
            self.windows.append({"step": event.step, "error": event.error, "detail": event.detail})


@dataclass
class SideCallUsage:
    """Token, cost and wall-clock totals for one kind of Compass side call."""

    calls: int = 0
    failures: int = 0
    """Attempts with no usable reply: transport errors, timeouts, cut-off or empty
    replies. A reply the core then fails to parse or commit is not one of these;
    those are per window, in ``maintenance_failures``."""
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    cost_usd: float | None = 0.0
    """None once any call's cost could not be determined: reporting 0 there would
    understate a comparison against the baseline."""

    def record(self, *, seconds: float, usage, cost: float | None, failed: bool = False) -> None:
        self.calls += 1
        self.seconds += seconds
        if failed:
            self.failures += 1
        self.input_tokens += int((usage or {}).get("prompt_tokens") or 0)
        self.output_tokens += int((usage or {}).get("completion_tokens") or 0)
        if cost is None:
            self.cost_usd = None
        elif self.cost_usd is not None:
            self.cost_usd += cost


class SideCallMeter:
    """Separate totals per kind of Compass call, plus the sum a trial is charged.

    A failed attempt is still billed and the core retries by calling again, so
    every attempt is counted where it happens rather than inferred afterwards.
    """

    #: Task extraction and Claim maintenance are different budgets and must stay
    #: separable; the schema title is what distinguishes them at the call site.
    EXTRACTION_SCHEMA = "IssueRequirementExtraction"

    def __init__(self):
        self.extraction = SideCallUsage()
        self.maintenance = SideCallUsage()

    def kind(self, response_schema) -> SideCallUsage:
        title = (response_schema or {}).get("title")
        return self.extraction if title == self.EXTRACTION_SCHEMA else self.maintenance

    def totals(self) -> dict:
        both = (self.extraction, self.maintenance)
        costs = [item.cost_usd for item in both]
        return {
            "calls": sum(item.calls for item in both),
            "failures": sum(item.failures for item in both),
            "input_tokens": sum(item.input_tokens for item in both),
            "output_tokens": sum(item.output_tokens for item in both),
            "seconds": round(sum(item.seconds for item in both), 3),
            "cost_usd": None if None in costs else round(sum(costs), 6),
        }

    def report(self) -> dict:
        return {
            "extraction": asdict(self.extraction),
            "maintenance": asdict(self.maintenance),
            "total": self.totals(),
        }


def _response_cost(litellm, reply) -> float | None:
    """The provider's cost for one reply, or None when it cannot be determined.

    None is not zero: an unpriced model must show as unknown, never as free. A
    returned zero is undeterminable too, because litellm answers 0 for a model it
    has no prices for instead of raising -- measured on a real run, where two
    billed calls reported a total of $0.00.
    """
    try:
        cost = float(litellm.completion_cost(completion_response=reply))
    except Exception:
        return None
    return cost if cost > 0 else None


#: Provider-native reasoning controls a side call must not inherit from the main
#: conversation. Maintenance is mechanical structured extraction: on a reasoning
#: model the inherited effort spends the whole output cap and the wall clock on
#: thinking the schema does not need.
REASONING_KEYS = ("reasoning", "reasoning_effort", "thinking", "output_config")


def maintenance_reasoning(settings: CallSettings) -> dict:
    """Reasoning controls for one Maintenance call, from the deployment's choice.

    ``inherit`` sends nothing and accepts whatever the model does by default.
    Explicit ``reasoning_options`` win, so a provider that names its control
    differently stays reachable without changing this adapter.
    """
    try:
        overrides = json.loads(settings.reasoning_options or "{}")
    except ValueError:
        overrides = {}
    if overrides:
        return {key: value for key, value in overrides.items() if key in REASONING_KEYS}
    if settings.reasoning == "inherit":
        return {}
    if settings.reasoning == "low":
        return {"reasoning": {"effort": "low"}}
    # auto and off both mean "do not think about this": the schema is the answer.
    return {"reasoning": {"enabled": False}}


def deepseek_reasoning(reasoning: dict) -> dict:
    """The official DeepSeek API ignores OpenRouter's `reasoning` object and thinks by default (effort high)."""
    effort = reasoning.get("reasoning")
    if not isinstance(effort, dict):
        return reasoning
    rest = {key: value for key, value in reasoning.items() if key != "reasoning"}
    if effort.get("enabled") is False:
        return rest | {"thinking": {"type": "disabled"}}
    return rest | ({"reasoning_effort": effort["effort"]} if effort.get("effort") else {})


_ITEM_LABEL = re.compile(r"^- (?:\[[A-Z]+\] )?([^\s:]+(?: ↔ [^\s:]+)?)")


def _item_label(line: str) -> str:
    """The subject of one review line: `R9` for weak coverage, `C-1 ↔ C-2` for a tension."""
    match = _ITEM_LABEL.match(line)
    return match.group(1) if match else line.removeprefix("- ")[:40]


class LitellmMaintenanceModel:
    """`MaintenanceModel` as a separate, tool-free structured call.

    mini's own ``model.query()`` always attaches the bash tool and demands an
    action, so Maintenance reuses only the provider and credentials, never that
    request shape -- and not its reasoning effort either.
    """

    def __init__(
        self, model_id: str, meter: SideCallMeter, reasoning=None, provider=None,
        response_format: Literal["json_schema", "json_object"] = "json_schema", **model_kwargs,
    ):
        self.model_id = model_id
        self._meter = meter
        self._reasoning = reasoning or {}
        self._provider = provider
        self._response_format = response_format
        self._model_kwargs = model_kwargs

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema,
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ModelReply:
        import litellm

        started = time.monotonic()
        usage = self._meter.kind(response_schema)
        schema = dict(response_schema or {})
        schema_title = schema.pop("title", "response")
        if self._response_format == "json_object":
            # Some endpoints (including official DeepSeek) reject json_schema.
            # The full per-call schema, including narrowed evidence references,
            # remains in the prompt; the core still parses/validates every reply.
            system_prompt += (
                "\nReturn one JSON instance, not a schema or a wrapper. Its top-level keys are exactly: "
                + ", ".join(schema.get("properties", {}))
                + ". Do not add title, type, properties, or commentary fields. "
                "Do not use Markdown fences. Schema for the instance:\n" + json.dumps(schema)
            )
            response_format = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": schema_title, "schema": schema, "strict": False},
            }
        try:
            reply = await litellm.acompletion(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_output_tokens,
                timeout=timeout_seconds,
                # The schema itself, as the Chrys adapter sends it. With json_object
                # alone the model copied the schema's own title/type keys into its
                # answer or left fields out: replayed on live trials, 10 of 13 replies
                # were rejected though their claims were sound; with it, 11 of 11 parsed.
                response_format=response_format,
                temperature=0,
                drop_params=True,
                # extra_body: these are provider-native controls, not OpenAI ones,
                # and drop_params would otherwise strip them.
                extra_body=(self._reasoning | ({"provider": self._provider} if self._provider is not None else {})) or None,
                **self._model_kwargs,
            )
        except BaseException:
            # A refused or timed-out attempt still costs time, and the core retries
            # by calling again; dropping it would flatter this arm.
            usage.record(seconds=time.monotonic() - started, usage=None, cost=None, failed=True)
            raise
        reported = getattr(reply, "usage", None)
        reported = dict(reported) if reported else None
        choice = reply.choices[0]
        text = choice.message.content or ""
        # As the Chrys adapter does: a cut-off or empty reply is not malformed JSON.
        # Naming it lets the core retry a cut-off one with a larger cap and keeps it
        # out of invalid_json, which otherwise hid why maintenance kept failing.
        failure = "truncated" if choice.finish_reason == "length" else "" if text.strip() else "empty"
        usage.record(
            seconds=time.monotonic() - started,
            usage=reported,
            cost=_response_cost(litellm, reply),
            failed=bool(failure),
        )
        if failure:
            details = (reported or {}).get("completion_tokens_details")
            reasoning = (
                details.get("reasoning_tokens")
                if isinstance(details, dict)
                else getattr(details, "reasoning_tokens", 0)
            )
            raise ModelCallError(
                failure,
                output_tokens=int((reported or {}).get("completion_tokens") or 0),
                reasoning_tokens=int(reasoning or 0),
            )
        return ModelReply(text=text, usage_details=reported)


class ObservingModel:
    """Delegate to the real model, recording what each executed command produced.

    Wrapping the formatter rather than the environment is what keeps the record
    honest: this is the one place holding both the untruncated output and the text
    the model is actually shown, so a Claim can cite only what was visible without
    this adapter re-deriving mini's elision rules.
    """

    def __init__(self, model, record):
        self._model = model
        self._record = record

    def __getattr__(self, name):
        return getattr(self._model, name)

    def format_observation_messages(self, message, outputs, template_vars=None):
        messages = self._model.format_observation_messages(message, outputs, template_vars)
        return self._record(message, outputs, messages)


class CompassMiniAgentConfig(InteractiveAgentConfig):
    compass_directory: Path | None = None
    """Trial directory for Compass artifacts. Unset keeps the session in memory."""
    compass_maintenance_interval: int = 24
    """Evidence records between automatic Maintenance calls, not model turns."""
    compass_brief_budget_chars: int = 6000
    compass_maintenance_model: str = ""
    """Model id for the Maintenance call. Unset reuses mini's model; ``none`` disables it."""
    compass_maintenance_response_format: Literal["json_schema", "json_object"] = "json_schema"
    """Explicit endpoint compatibility choice; both modes retain strict core validation."""
    compass_review_before_submit: bool = False
    """Hold a submission once when the ledger still lists something worth a last
    look. Off by default: it adds main-model turns and is an experiment variable
    of its own, so enabling it silently would fold two changes into one result."""
    compass_review_limit: int = 1
    """How many times one trial may hold a submission. A bounded count is what
    stops the same unresolved item from blocking a submission forever."""
    compass_review_items: int = 5
    """How many items one reminder may list. Measured on a real task: an unbounded
    reminder listed 15 URGENT entries, four of them about the same function, which
    a model is unlikely to act on past the first few. The count of omitted items is
    still reported, so a short list never pretends to be the whole ledger."""
    compass_raw_audit_bytes: int = 1 << 20
    """Per-command cap for the untruncated output kept for audit. Compass's own
    Evidence excerpt is bounded and is not a record of the full output."""


class CompassMiniAgent(SubmissionSafeMiniAgent):
    def __init__(self, model, env, *, config_class: type = CompassMiniAgentConfig, reference_context=None, **kwargs):
        super().__init__(model, env, config_class=config_class, **kwargs)
        # mini's loop is synchronous and the core is async. One Runner holds one
        # event loop for the whole trial, so the core's locks and cursors stay on
        # a single loop; a fresh loop per shell command would split them.
        self._compass_runner = asyncio.Runner()
        self.side_calls = SideCallMeter()
        # A CodeHelix install leaves its deployment configuration beside the
        # activation, so the Maintenance budgets and reasoning choice made at
        # install time are the ones actually used here. A development checkout has
        # no locator and falls back to the core defaults. Read before building the
        # Maintenance model, which needs the reasoning choice.
        binding = deployment_binding()
        self._call_settings = CallSettings.from_configuration(binding[1]) if binding else CallSettings()
        maintenance = self._maintenance_model()
        self._artifacts = TrialArtifacts()
        self._raw_index = 0
        self._detail_entry = self._install_detail_query()
        self._maintenance_failures = MaintenanceFailures()
        self.compass = CompassSession(
            task_reference_context=reference_context or (lambda task: ""),
            artifacts=self._artifacts,
            events=self._maintenance_failures,
            model=lambda: maintenance,
            directory=self.config.compass_directory,
            repo_path=self._repo_path,
            brief_budget_chars=self.config.compass_brief_budget_chars,
            maintenance_interval=self.config.compass_maintenance_interval,
            call_settings=self._call_settings,
            detail_entry=self._detail_entry,
            detail_renderer=partial(shell_detail_entry, entry=self._detail_entry),
        )
        self.model = ObservingModel(model, self._observe_batch)
        self._mark = DeliveryMark()
        self._staged: tuple[dict, DeliveryFacts] | None = None
        self._submitting = False
        self._reviews: list[frozenset[str]] = []
        self._commands_run = 0
        #: Commands run when the last review was delivered, until that review is
        #: answered; None when no review is waiting for one.
        self._review_awaits: int | None = None
        self._review_repeats = 0
        self._closing_reminder_sent = False
        self._submission_rules_step: int | None = None
        self._submission_check: dict | None = None

    def _install_detail_query(self) -> str:
        """Give the Brief a detail entry this host can actually execute.

        mini's only tool is a shell, so the canonical in-process tool name would
        send the model after something that does not exist. Without a trial
        directory nothing can be saved for a second process to read, and saying
        so is better than naming an entry that would fail.
        """
        directory = self.config.compass_directory
        if directory is None:
            return NO_DETAIL_ENTRY
        shim = Path(directory) / DETAIL_SHIM
        state = Path(directory) / STATE_FILE
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} -m compass_brief.mini.inspect_cli "
            f'--state {shlex.quote(str(state))} "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o700)
        return str(shim)

    def _save_detail_state(self) -> None:
        """Keep the snapshot the detail entry reads current with the ledger.

        Saved after every recorded observation, not only per request: the model
        can query details in the same turn it ran a command, and "the latest
        saved state" has to mean that.
        """
        if self._detail_entry is NO_DETAIL_ENTRY:
            return
        state = Path(self.config.compass_directory) / STATE_FILE
        self._artifacts.write_text(state, json.dumps(self.compass.contribute_session_state(), indent=2))

    def _maintenance_model(self):
        configured = self.config.compass_maintenance_model
        if configured == NO_MAINTENANCE_MODEL:
            return None
        model_id = configured or getattr(self.model.config, "model_name", "")
        if not model_id:
            return None
        # Keep extraction and maintenance on the main OpenRouter route, without
        # inheriting its tools, sampling options or expensive reasoning effort.
        provider = None
        if model_id.startswith("openrouter/"):
            provider = getattr(self.model.config, "model_kwargs", {}).get("provider")
        reasoning = maintenance_reasoning(self._call_settings)
        if model_id.startswith("deepseek/"):
            reasoning = deepseek_reasoning(reasoning)
        return LitellmMaintenanceModel(
            model_id, self.side_calls, reasoning=reasoning, provider=provider,
            response_format=self.config.compass_maintenance_response_format,
        )

    def _repo_path(self) -> str:
        return getattr(self.env.config, "cwd", "") or os.getcwd()

    def _observe_batch(self, message: dict, outputs: list[dict], messages: list[dict]) -> list[dict]:
        """Record one turn's executed commands, then decide whether to remind once.

        Pairing follows the real ``outputs``. The toolcall formatter pads its
        result list with "action was not executed" placeholders up to the number
        of actions, and counting those would invent Evidence for commands that
        never ran.
        """
        actions = message.get("extra", {}).get("actions", [])
        executed = len(outputs)
        # strict: a formatter that cannot show one observation per executed command
        # breaks the promise that Claims cite what the model saw, and recording
        # fewer in silence would be worse than stopping here.
        paired = zip(actions[:executed], outputs, messages[:executed], strict=True)
        for action, output, observation in paired:
            # A turn that already signalled submit records its last batch without
            # paying for maintenance nothing would read. Pending stays pending, so
            # if a review holds the submission the ordinary policy resumes.
            self._record_observation(action, output, observation, maintain=not self._submitting and not self._out_of_budget())
            self._commands_run += 1
        if executed:
            self._save_detail_state()
        if self._submitting or executed < len(actions) or self._out_of_budget():
            # The turn is ending: a submit, or an action that raised through. A
            # reminder appended now would reach no model request.
            return messages
        brief = self._reminder()
        return [*messages, brief] if brief else messages

    def _record_observation(self, action: dict, output: dict, observation: dict, *, maintain: bool = True) -> None:
        visible = observation.get("content", "")
        if not isinstance(visible, str):
            # A multimodal observation carries parts; Claims reason over the text.
            visible = "".join(part.get("text", "") for part in visible or () if isinstance(part, dict))
        self._archive_raw(action, output.get("output", ""))
        self._compass_runner.run(
            self.compass.observe(
                ToolObservation(
                    tool_name="bash",
                    tool_kind="shell",
                    arguments={"command": action.get("command", "")},
                    # What the model was shown, never the elided middle of a long
                    # output: Compass must not claim to have read what was hidden.
                    result=visible,
                    failed=bool(output.get("exception_info")),
                    exit_code=output.get("returncode"),
                    working_dir=self._repo_path(),
                    phase="mini",
                    step=self.n_calls,
                ),
                schedule_maintenance=maintain,
            )
        )

    def _archive_raw(self, action: dict, raw: str) -> None:
        """Keep the untruncated output for audit, beside Compass's bounded Evidence."""
        directory = self.config.compass_directory
        if directory is None or not raw:
            return
        cap = self.config.compass_raw_audit_bytes
        path = Path(directory) / "raw" / f"step-{self.n_calls:04d}-{self._raw_index:02d}.txt"
        self._raw_index += 1
        self._artifacts.write_text(path, f"$ {action.get('command', '')}\n{raw if cap <= 0 else raw[:cap]}")

    def _reminder(self) -> dict | None:
        """Stage one merged reminder when a trigger fired, else nothing."""
        facts = self.compass.delivery_facts()
        if self.n_calls > 200:
            if self._closing_reminder_sent:
                return None
            message = self.model.format_message(
                role="user",
                content=(
                    "More than 200 model steps have been used. Finish the current work: "
                    "check the affected behavior, known failures and final diff; do not start "
                    "new speculative work. Submit when ready and state any unresolved issues. "
                    "This reminder does not certify correctness."
                ),
                extra={"compass": "brief", "compass_reason": "closing-budget", "compass_finish_only": True},
            )
            self._staged = (message, facts)
            return message
        reason = delivery_reason(facts, self._mark)
        if not reason:
            return None
        brief = self.compass.brief_for_request(recipient="model", phase="mini")
        if not brief:
            return None
        message = self.model.format_message(
            role="user", content=brief, extra={"compass": "brief", "compass_reason": reason}
        )
        # Marked only once the history really holds it: a formatter return value
        # that never reaches `add_messages` must not count as delivered.
        self._staged = (message, facts)
        return message

    def _check_for_new_task_or_submit(self, exception):
        # Runs before the formatter, so the last batch of this turn is not recorded
        # yet. Only note the intent here and keep the original exception; the
        # decision happens once those observations exist.
        self._submitting = True
        return super()._check_for_new_task_or_submit(exception)

    def execute_actions(self, message: dict) -> list[dict]:
        self._submitting = False
        try:
            return super().execute_actions(message)
        except Submitted:
            # The supermethod's `finally` has already appended this turn's
            # observations, so the ledger is current. Only a submission is caught:
            # a budget exit, a cancellation or an error must stay what it is.
            hold = self._check_submission_contract()
            if hold is not None:
                if hold:
                    return []  # Observations and the check are already in history.
                raise  # Low budget: preserve this submission without another model call.
            repeat = self._unanswered_review()
            if repeat is not None:
                # Not a new review: waiting on this one again would hold every later
                # identical resubmission too, until the budget ran out.
                self._review_repeats += 1
                self._interrupt(repeat, itype="CompassReviewRepeat")
            review = self._final_review()
            if review is None:
                raise
            self._review_awaits = self._commands_run
            self._interrupt(review, itype="CompassReview")
        finally:
            self._submitting = False

    def _check_submission_contract(self) -> bool | None:
        """Check once; fewer than 15 remaining calls never holds a submission."""
        if self._submission_check is not None:
            return None
        remaining = self.config.step_limit - self.n_calls if self.config.step_limit > 0 else None
        hold = (remaining is None or remaining >= 15) and not self._out_of_budget()
        content = SUBMISSION_CHECK + (
            "\nWithin the remaining budget, check/fix or explain unresolved items, then submit again."
            if hold else "\nInsufficient remaining budget for a held check: accepting this submission immediately."
        )
        message = self.model.format_message(
            role="user", content=content,
            extra={"compass": "brief", "compass_reason": "submission-check", "compass_check_held": hold},
        )
        self.add_messages(message)
        self._submission_check = {
            "step": self.n_calls, "remaining_steps": remaining, "held": hold,
            "message_sha256": sha256(message["content"].encode()).hexdigest(),
        }
        return hold

    def _unanswered_review(self) -> str | None:
        """Hold once more a resubmission that ran nothing after the review.

        The submit command itself never produces an observation, so an unchanged
        count means no command ran and none of the listed items was looked at. Held
        at most once per review: a model that checks and still resubmits is
        answered, and one that ignores this too is let through.
        """
        awaited, self._review_awaits = self._review_awaits, None
        if awaited is None or self._commands_run != awaited or self._out_of_budget():
            return None
        return (
            "You resubmitted without running anything after the review above, so none of its items "
            "was checked. Take them in turn: check each, fix it, or say why it is already resolved; "
            "then submit again."
        )

    def _out_of_budget(self) -> bool:
        # Continuing would only raise LimitsExceeded on the next request. A budget
        # exit must not be dressed up as a task that carried on.
        return (
            0 < self.config.step_limit <= self.n_calls
            or 0 < self.config.cost_limit <= self.cost
            or 0 < self.config.wall_time_limit_seconds <= int(time.time() - self._start_time)
        )

    def _final_review(self) -> str | None:
        """Ask for one last look when the ledger still lists something, else None."""
        if not self.config.compass_review_before_submit:
            return None
        if len(self._reviews) >= max(0, self.config.compass_review_limit):
            return None
        if self._out_of_budget():
            return None
        items = self.compass.final_review()
        if not items:
            return None
        # Deduplicate on everything the ledger listed, not on the shortened text:
        # otherwise trimming the list would let the same items hold a second time.
        fingerprint = frozenset(items)
        if fingerprint in self._reviews:
            return None
        self._reviews.append(fingerprint)
        limit = max(1, self.config.compass_review_items)
        omitted = max(0, len(items) - limit)
        shown = items[:limit]
        facts = self.compass.delivery_facts()
        pending = (
            f"\n{facts.pending_evidence} observation(s) are not in the maintained Claims yet, "
            "so an item below may already be resolved; re-check rather than assume."
            if facts.pending_evidence
            else ""
        )
        return (
            "Before this is final, the Compass ledger still lists:\n"
            + "\n".join(shown)
            # Name every item left out. When all requirements are weak the ledger
            # cannot tell which one is really missing, and a bare count hid the ones
            # that failed behind the first few shown.
            + (f"\n… and {omitted} more: {', '.join(_item_label(line) for line in items[limit:])}." if omitted else "")
            + pending
            + "\nWeak coverage means the evidence is thin, not that the code is wrong. "
            "Within the remaining budget, prioritize a concrete failure or an unverified public behavior. "
            "Derive the expected result from the issue and existing callers, independently of your implementation; "
            "check the relevant boundary or failure path and any affected repository tests after the latest edit. "
            "A passing self-written test proves only the exercised case. Do not invent requirements. "
            "Check, fix, or state plainly what remains unresolved, then submit again."
        )

    def add_messages(self, *messages: dict) -> list[dict]:
        for message in messages:
            if self._submission_rules_step is None and message.get("extra", {}).get("compass") == "brief":
                message["content"] += SUBMISSION_RULES
                message["extra"]["compass_submission_rules"] = True
                self._submission_rules_step = self.n_calls
        result = super().add_messages(*messages)
        for index, message in enumerate(messages, start=len(self.messages) - len(messages)):
            if message.get("role") != "assistant":
                continue
            text = message.get("content", "")
            if not isinstance(text, str):
                text = "".join(part.get("text", "") for part in text or () if isinstance(part, dict))
            self._compass_runner.run(
                self.compass.record_agent_statement(f"mini:{index}", text, phase="mini", step=self.n_calls)
            )
        if self._staged is not None and any(message is self._staged[0] for message in messages):
            self._mark = self._mark.after(self._staged[1])
            if self._staged[0].get("extra", {}).get("compass_finish_only"):
                self._closing_reminder_sent = True
            self._staged = None
        return result

    def _start_task(self, task: str) -> None:
        self._compass_runner.run(self.compass.start(task))

    def run(self, task: str = "", **kwargs) -> dict:
        try:
            if task.strip():
                self._start_task(task)
            result = super().run(task, **kwargs)
            if result.get("exit_status") == "Submitted" and not self._out_of_budget():
                self._compass_runner.run(self.compass.maintain(phase="mini", stage_end=True))
            return result
        finally:
            try:
                self._save_detail_state()
                self.save(self.config.output_path)
            finally:
                self._compass_runner.close()

    def _contract_sizes(self) -> dict:
        entries = requirement_entries(self.compass.goals.snapshot())
        return {
            "requirements": sum(1 for identifier, _text in entries if identifier.startswith("R")),
            "constraints": sum(1 for identifier, _text in entries if identifier.startswith("K")),
        }

    def serialize(self, *extra_dicts) -> dict:
        status = self.compass.maintenance_status()
        return super().serialize(
            {
                "info": {
                    "compass": {
                        # Whether this trial is really the treatment arm. A failed
                        # extraction degrades Compass to bookkeeping without failing
                        # the trial, so a run with no contract must be visible here
                        # rather than averaged in as though it had one.
                        "contract": self._contract_sizes(),
                        # Compass's own calls are invisible to mini's accounting, so a
                        # comparison that omitted them would understate this arm.
                        "side_calls": self.side_calls.report(),
                        "reviews_before_submit": len(self._reviews),
                        "review_repeats": self._review_repeats,
                        "budget_exhausted": self._out_of_budget(),
                        "closing_reminder_delivered": self._closing_reminder_sent,
                        "submission_rules_step": self._submission_rules_step,
                        "submission_check": self._submission_check,
                        "delivered_revision": self._mark.revision,
                        "delivered_escalations": sorted(self._mark.escalations),
                        "n_evidence": len(self.compass.evidence.snapshot()),
                        "n_claims": len(self.compass.claims.snapshot().claims),
                        "maintenance": {
                            "phase": status.phase,
                            "pending_evidence": status.pending_evidence,
                            "pending_statement_chars": status.pending_statement_chars,
                            "error": status.error,
                        },
                        # One entry per failed window, with why. The final status
                        # above shows only the last one, and the side-call meter
                        # never sees a reply the core could not parse.
                        "maintenance_failures": self._maintenance_failures.windows,
                    }
                }
            },
            *extra_dicts,
        )
