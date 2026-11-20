"""0.2.40 exploration handoff for mini's shell-only model interface."""

from __future__ import annotations

import shlex
from hashlib import sha256

from compass_brief.briefing.render import CONFLICT_REVIEW_GUIDANCE, requirement_conflicts
from compass_brief.evidence.actions import classify_shell
from compass_brief.mini.agent import CompassMiniAgent

INSTRUCTIONS = """

Compass workflow: first explore the relevant repository entry points, implementation,
callers and tests. Before editing, execute one standalone bash action:
  compass start 'Exploration findings, source paths and unresolved assumptions'
This is an adapter command, not a repository executable. Do not use command -v or search the filesystem for it. Wait for its G/R/K and
Brief, review them, then implement and check the task. The original issue remains
authoritative; repository findings and external guidance are reference material.
Use `compass inspect R1 C-001` to read full records. Briefs are updated context,
not new tool evidence. A passing self-written check proves only the exercised
conditions; it does not prove the interpretation used to write that check.
"""


class _Controls:
    def __init__(self, environment, owner):
        self._environment, self._owner = environment, owner

    def __getattr__(self, name):
        return getattr(self._environment, name)

    def execute(self, action, *args, **kwargs):
        response = self._owner.control(action.get("command", ""))
        if response is not None:
            return {"output": response, "returncode": 0, "exception_info": "", "compass_control": True}
        return self._environment.execute(action, *args, **kwargs)


class ExploringCompassMiniAgent(CompassMiniAgent):
    """Keep the shared Core unchanged; adapt exploration, statements and delivery."""

    def __init__(self, model, env, *, reference_context: str = "", **kwargs):
        self._task_text = ""
        self._handoff_pending = False
        self._force_brief = False
        self._batch_size = 0
        self._notified_conflicts: set[str] = set()
        kwargs["instance_template"] += INSTRUCTIONS
        super().__init__(
            model, env,
            reference_context=lambda task: reference_context if task == self._task_text else "",
            **kwargs,
        )
        self.env = _Controls(self.env, self)

    def _install_detail_query(self) -> str:
        # Host paths must not be advertised as executables in the task container.
        return "compass inspect"

    def _save_detail_state(self) -> None:
        if self.config.compass_directory is not None:
            super()._save_detail_state()

    def _start_task(self, task: str) -> None:
        self._task_text = task

    def query(self):
        self._handoff_pending = False  # only a new model request can read the handoff
        return super().query()

    def execute_actions(self, message):
        self._batch_size = len(message.get("extra", {}).get("actions", []))
        return super().execute_actions(message)

    def control(self, command: str) -> str | None:
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            parts = list(lexer)
        except ValueError:
            parts = []
        # mini often prefixes every command with its working directory. Accept only
        # a no-op cd into that same directory, never run any surrounding shell code.
        if len(parts) >= 4 and parts[0] == "cd" and parts[2] == "&&" and parts[3] == "compass":
            if parts[1].rstrip("/") not in {".", self.env.config.cwd.rstrip("/")}:
                return "Use the adapter command in the task working directory: compass start 'summary'"
            parts = parts[3:]
        if parts[:2] == ["compass", "inspect"]:
            return self._compass_runner.run(self.compass.inspect(parts[2:]))
        if parts[:2] == ["compass", "start"]:
            if self._out_of_budget():
                return "Budget exhausted: exploration extraction/maintenance not run; recorded observations retained."
            if len(parts) != 3 or not parts[2].strip() or self._batch_size != 1:
                return "Use one standalone action: compass start 'findings, source paths and open assumptions'"
            if self.compass.goals.snapshot().description:
                self._force_brief = True
                return "Contract already exists; review the current Brief."
            if not any(e.kind == "observation" and e.outcome == "ok" for e in self.compass.evidence.snapshot()):
                return "Read the relevant repository code before the exploration handoff."
            self._compass_runner.run(self._complete_exploration(parts[2]))
            self._handoff_pending = True
            self._force_brief = True
            return "Exploration handoff complete. Review the following G/R/K and Brief before editing."
        if parts[:1] == ["compass"]:
            return "Supported adapter commands: compass start 'summary'; compass inspect [R/K/C IDs]"
        if (not self.compass.goals.snapshot().description or self._handoff_pending) and (
            classify_shell(command) == "change" or "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in command
        ):
            # Same semantic guard as iCode; this classifier is not a security sandbox.
            return "Not executed: finish exploration with a standalone compass start command; read its Brief first."
        return None

    async def _complete_exploration(self, summary):
        await self.compass.record_agent_statement(
            "mini:exploration:" + sha256(summary.encode()).hexdigest(),
            "Exploration handoff (agent interpretation):\n" + summary,
            phase="mini", step=self.n_calls,
        )
        await self.compass.start(self._task_text, repository_context=await self.compass.exploration_context())
        if any(item.tag == "extraction" for item in self.compass.goals.snapshot().conclusions):
            raise RuntimeError("Compass contract extraction failed after bounded retries; coding not started")
        await self.compass.maintain(phase="exploration-complete", drain_conversation=True)

    def _record_observation(self, action, output, observation, *, maintain=True):
        if output.get("compass_control"):
            return  # own Brief/details and denied actions are never repository evidence
        super()._record_observation(
            action, output, observation,
            maintain=maintain and bool(self.compass.goals.snapshot().description),
        )

    def _reminder(self):
        if self.n_calls > 200:
            return super()._reminder()
        if not self.compass.goals.snapshot().description:
            return None
        message = super()._reminder()
        snapshot = self.compass.claims.snapshot()
        active = {c.id: c for c in snapshot.claims if c.id in snapshot.state.active_ids}
        conflicts = [line for line in requirement_conflicts(active, snapshot.conflict_notes)
                     if line not in self._notified_conflicts]
        if message is None and (self._force_brief or conflicts):
            message = self.model.format_message(
                role="user", content=self.compass.brief_for_request(recipient="model", phase="mini"),
                extra={"compass": "brief", "compass_reason": "exploration-or-conflict"},
            )
            self._staged = (message, self.compass.delivery_facts())
        if conflicts:
            message["content"] += (
                "\n\n[Compass — new requirement conflict]\n" + "\n".join(conflicts)
                + "\nBefore continuing, re-read the quoted requirement in the task text and state in one sentence whether "
                "your behavior follows it. If it does not, change the code. " + CONFLICT_REVIEW_GUIDANCE
            )
            self._notified_conflicts.update(conflicts)
        self._force_brief = False
        return message

    def add_messages(self, *messages):
        result = super().add_messages(*messages)
        for index, message in enumerate(messages, start=len(self.messages) - len(messages)):
            if message.get("role") != "assistant":
                continue
            thought = message.get("reasoning_content") or message.get("reasoning")
            if not thought:
                details = message.get("reasoning_details") or []
                thought = "".join(d.get("text", "") for d in details if isinstance(d, dict))
            if isinstance(thought, str) and thought.strip():
                self._compass_runner.run(self.compass.record_agent_statement(
                    f"mini:reasoning:{index}", "[reasoning]\n" + thought, phase="mini", step=self.n_calls,
                ))
        return result
