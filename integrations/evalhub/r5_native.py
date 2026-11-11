"""iCode 0.34 adapter for the frozen Compass v1.4.2 core.

One process owns one trial. This is not an installer for shared iCode profiles.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import posixpath
import shlex
import time
from pathlib import Path
from typing import Annotated

from compass_brief.briefing.policy import DeliveryMark, delivery_reason
from compass_brief.briefing.render import CONFLICT_REVIEW_GUIDANCE, requirement_conflicts
from compass_brief.evidence.observation import ToolObservation
from compass_brief.ports import ModelReply
from compass_brief.session import CompassSession

from chrys.extensions import Extension, ToolContext
from chrys.foundation.platform.files import atomic_write_owner_only_text, secure_open_owner_verified_binary
from chrys.foundation.tool_kinds import get_tool_kind
from chrys.foundation.tool_result_metadata import (
    TOOL_RESULT_METADATA_KEY,
    tool_result_metadata_failure_state,
    tool_result_metadata_is_rejected,
)
from chrys.kernel.middleware import ChatMiddleware, FunctionMiddleware

UPSTREAM = "ffdc67ed74c1c2d78d16ab4b8544fed8d9ee3c69"
ARCHIVE = "2866727c047a5bd4746f6d25b0ad64ebc717332b07f9842e3be607c98c95291e"
BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-flash"
MODEL_PROFILE = "dee05ee03400"
MAX_CALLS, MAX_SECONDS = 250, 11400
CLOSING = (
    "More than 200 model steps have been used. Finish the current work: "
    "check the affected behavior, known failures and final diff; do not start "
    "new speculative work. Submit when ready and state any unresolved issues. "
    "This reminder does not certify correctness."
)
extension = Extension()
controller = None


class R5HandoffError(RuntimeError):
    """A terminal workflow failure with a credential-free diagnostic code."""


def exploration_read_only(command):
    """Allow only simple source inspection before the R5 handoff.

    General shell execution resumes after Brief delivery. Shell substitutions,
    redirects, interpreters and executable hooks require a completed handoff.
    """
    if any(char in command for char in "$`<>();\\\n\r"):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    segments = [[]]
    for token in tokens:
        if token in ("&&", "|"):
            segments.append([])
        elif token and set(token) <= {"&", "|"}:
            return False
        else:
            segments[-1].append(token)
    for words in segments:
        if not words:
            return False
        name, *args = words
        if name == "cd":
            if len(args) != 1 or args[0].startswith("-"):
                return False
        elif name in {"cat", "head", "tail", "nl", "ls", "pwd", "wc", "grep"}:
            pass
        elif name == "rg":
            if any(arg.startswith(("--pre", "--hostname-bin")) for arg in args):
                return False
        elif name == "git":
            if args != ["rev-parse", "HEAD"]:
                return False
        else:
            return False
    return bool(tokens)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=lambda x: x.to_dict() if hasattr(x, "to_dict") else str(x))


class Artifacts:
    write_text = staticmethod(atomic_write_owner_only_text)
    open_verified_binary = staticmethod(secure_open_owner_verified_binary)


def frozen_constants(root):
    manifest = json.loads((root / "SOURCE-MANIFEST.json").read_text())
    if (
        manifest["original_archive_sha256"] != ARCHIVE
        or manifest.get("version") != "1.4.2"
        or len(manifest["files_sha256"]) != 72
    ):
        raise RuntimeError("Unexpected Compass v1.4.2 manifest")
    for name, digest in manifest["files_sha256"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("Frozen Compass v1.4.2 mismatch: " + name)
    import compass_brief.session as core

    if Path(core.__file__).resolve() != root / "src/compass_brief/session.py":
        raise RuntimeError("PYTHONPATH must load the frozen R5 core")
    tree = ast.parse((root / "src/compass_brief/mini/agent.py").read_text())
    return {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"SUBMISSION_RULES", "SUBMISSION_CHECK"}
    }


class OfficialMaintenance:
    def __init__(self, key, audit):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=key, base_url=BASE_URL, max_retries=0)
        self.audit = audit

    async def complete(self, *, system_prompt, user_prompt, response_schema, max_output_tokens, timeout_seconds):
        started = time.monotonic()
        self.audit("maintenance_request", timeout_seconds=timeout_seconds, max_output_tokens=max_output_tokens)
        try:
            response = await self.client.chat.completions.create(
                model=MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                        + "\nReturn a JSON object matching this schema:\n"
                        + dump(response_schema),
                    },
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                max_tokens=max_output_tokens,
                temperature=0,
                extra_body={"thinking": {"type": "disabled"}},
                timeout=timeout_seconds,
            )
        except asyncio.CancelledError:
            self.audit("maintenance_cancelled", seconds=time.monotonic() - started)
            raise
        except Exception as exc:
            self.audit("maintenance_error", error_type=type(exc).__name__)
            # SDK messages may contain request material; do not persist them.
            raise RuntimeError("Official maintenance request failed: " + type(exc).__name__) from None
        usage = response.usage.model_dump(mode="json") if response.usage else None
        self.audit(
            "maintenance_response",
            model=response.model,
            response_id=response.id,
            usage=usage,
            seconds=time.monotonic() - started,
        )
        if not response.choices or response.choices[0].finish_reason != "stop":
            raise RuntimeError("Maintenance response incomplete")
        return ModelReply(response.choices[0].message.content or "", usage)


class Controller:
    def __init__(self, task, cwd, directory, constants, model=None):
        self.task, self.cwd, self.directory, self.constants = task, cwd, directory, constants
        self.calls = 0
        self.container = None
        self.started_at = time.monotonic()
        self.started = self.handoff_seen = self.rules_seen = self.closing_seen = self.check_done = False
        self.handoff_failure = None
        self.handoff_lock = asyncio.Lock()
        self.mark = DeliveryMark()
        self.seen, self.conflicts = set(), set()
        self.tool_kinds, self.tool_calls = {}, {}
        self.batch_size = 0
        self.model = model
        self.core = CompassSession(
            artifacts=Artifacts(),
            events=self,
            model=lambda: self.model,
            directory=directory / "compass",
            repo_path=lambda: str(cwd),
            detail_entry="inspect_compass_brief",
        )

    def audit(self, event, **fields):
        path = self.directory / "adapter-events.jsonl"
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600), "a") as f:
            f.write(dump({"event": event, "time": time.time(), "step": self.calls, **fields}) + "\n")

    async def publish(self, event):
        self.audit("core_event", type=type(event).__name__)

    def remaining(self):
        return MAX_SECONDS - (time.monotonic() - self.started_at)

    def check_budget(self):
        if self.handoff_failure:
            raise R5HandoffError(self.handoff_failure)
        if self.calls >= MAX_CALLS or self.remaining() <= 0:
            self.audit(
                "budget_exhausted", calls=self.calls, cause="main_calls" if self.calls >= MAX_CALLS else "wall_time"
            )
            raise RuntimeError("R5 trial budget exhausted")

    def save(self):
        Artifacts.write_text(self.directory / "compass-state.json", dump(self.core.contribute_session_state()))
        Artifacts.write_text(
            self.directory / "handoff-status.json",
            dump(
                {
                    "started": self.started,
                    "brief_delivered": self.handoff_seen,
                    "failure_code": self.handoff_failure,
                }
            ),
        )

    async def capture(self, messages, *, maintain):
        for message in messages:
            identity = message.message_id or sha(dump(message.to_dict()))
            if message.role == "assistant":
                for index, item in enumerate(message.contents):
                    if item.type in {"text", "text_reasoning"} and item.text:
                        await self.core.record_agent_statement(
                            f"{identity}:{index}", item.text, phase="icode", step=self.calls
                        )
                    if item.type == "function_call":
                        self.tool_calls[item.call_id] = (item.name, item.arguments)
            for item in message.contents:
                if item.type != "function_result" or item.call_id in self.seen:
                    continue
                self.seen.add(item.call_id)
                name, args = self.tool_calls.get(item.call_id, ("", {}))
                kind = self.tool_kinds.get(name)
                meta = (item.additional_properties or {}).get(TOOL_RESULT_METADATA_KEY, {})
                if kind not in {"filesystem.read", "filesystem.write", "search", "shell"}:
                    continue
                if meta.get("compass_control") or tool_result_metadata_is_rejected(meta):
                    continue
                if isinstance(args, str):
                    args = json.loads(args)
                code = meta.get("shell_exit_code", meta.get("process_exit_code"))
                code = code if type(code) is int else None
                await self.core.observe(
                    ToolObservation(
                        tool_name=name,
                        tool_kind=kind,
                        arguments=args or {},
                        result=item.result if isinstance(item.result, str) else dump(item.result),
                        failed=tool_result_metadata_failure_state(meta) is True or bool(item.exception),
                        exit_code=code,
                        working_dir="" if self.container else str(self.cwd),
                        phase="icode",
                        tool_call_id=item.call_id,
                        step=self.calls,
                    ),
                    schedule_maintenance=maintain and self.started and self.calls < MAX_CALLS,
                )

    async def handoff(self, summary):
        self.check_budget()
        if self.batch_size != 1:
            self.audit("handoff_rejected", reason="parallel_tool_batch")
            return "Not started. Call start_compass_brief alone, then wait for the Brief."
        async with self.handoff_lock:
            return await self._handoff(summary)

    def fail_handoff(self, code):
        self.handoff_failure = code
        self.audit("handoff_failed", code=code, started=self.started, brief_delivered=self.handoff_seen)
        Artifacts.write_text(
            self.directory / "failure.json",
            dump(
                {
                    "code": code,
                    "stage": "r5_handoff",
                    "started": self.started,
                    "brief_delivered": self.handoff_seen,
                }
            ),
        )
        raise R5HandoffError(code)

    async def _handoff(self, summary):
        self.check_budget()
        if not self.core.evidence.snapshot():
            self.audit("handoff_rejected", reason="no_repository_evidence")
            return "Not started. First inspect the relevant repository source and tests."
        if not self.started:
            self.audit("handoff_started")
            await self.core.record_agent_statement(
                "exploration:" + sha(summary),
                "Exploration handoff (agent interpretation):\n" + summary,
                phase="exploration",
                step=self.calls,
            )
            try:
                await self.core.start(self.task, repository_context=await self.core.exploration_context())
            except Exception:
                self.fail_handoff("r5_contract_start_failed")
            fallback = [c for c in self.core.goals.snapshot().conclusions if c.tag == "extraction"]
            if fallback:
                code = (
                    "r5_contract_extraction_timeout"
                    if any("(timeout)" in c.content for c in fallback)
                    else "r5_contract_extraction_failed"
                )
                self.fail_handoff(code)
            try:
                await self.core.maintain(phase="exploration-complete", drain_conversation=True)
            except Exception:
                self.fail_handoff("r5_initial_maintenance_failed")
            self.started = True
            self.audit("handoff_ready")
            self.save()
        return "Compass started. Read the Brief on the next turn before editing."


@extension.tool(requires_approval=False)
async def start_compass_brief(
    ctx: ToolContext, exploration_summary: Annotated[str, "Repository findings, paths and unresolved assumptions"]
) -> str:
    """Finish repository exploration and initialize R5. Call alone before editing."""
    return await controller.handoff(exploration_summary)


@extension.tool(requires_approval=False)
async def inspect_compass_brief(
    ctx: ToolContext, refs: Annotated[list[str], "Requirement, claim or evidence IDs; empty for overview"]
) -> str:
    """Read full Compass records. This is context, not new repository evidence."""
    return await controller.core.inspect(refs)


class ExplorationGuard(FunctionMiddleware):
    def __init__(self, owner):
        self.owner = owner

    async def process(self, context, call_next):
        if self.owner.handoff_failure:
            raise R5HandoffError(self.owner.handoff_failure)
        kind = get_tool_kind(context.function)
        args = context.arguments
        args = args.model_dump() if hasattr(args, "model_dump") else dict(args)
        command = args.get("command", args.get("cmd", args.get("script", "")))
        changes = kind == "filesystem.write" or (kind == "shell" and not exploration_read_only(command))
        if changes and not self.owner.handoff_seen:
            context.metadata[TOOL_RESULT_METADATA_KEY] = {"compass_control": True, "failed": True}
            context.result = (
                "Not executed. Inspect repository source with simple cat, rg, ls or pwd commands, "
                "then call start_compass_brief alone and read the Brief before editing."
            )
            self.owner.audit("write_held", tool=context.function.name)
            if self.owner.core.evidence.snapshot():
                async with self.owner.handoff_lock:
                    await self.owner._handoff(
                        "Source inspection is recorded in the evidence ledger. "
                        "Initialize R5 before executing the pending repository operation."
                    )
                context.result = (
                    "Not executed. R5 is initialized. Read the Brief in the next model request, "
                    "then retry this repository operation."
                )
            return
        await call_next()


class ContainerShell(FunctionMiddleware):
    """Run repository commands in the launcher's isolated container, never on the host."""

    def __init__(self, owner):
        self.owner = owner

    async def process(self, context, call_next):
        if context.function.name in {
            "start_compass_brief",
            "inspect_compass_brief",
            "compress_context",
            "list_compressed_contexts",
            "recall_context",
        }:
            await call_next()
            return
        if context.function.name != "execute":
            raise RuntimeError("Tool not allowed in isolated benchmark")
        args = context.arguments
        args = args.model_dump() if hasattr(args, "model_dump") else dict(args)
        seconds = min(300, max(1, int(args.get("timeout", 30))))
        workdir = posixpath.normpath(posixpath.join("/app", args.get("workdir") or "."))
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-w",
            workdir,
            self.owner.container,
            "timeout",
            "-k",
            "5",
            str(seconds),
            "bash",
            "-lc",
            args["command"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=seconds + 20)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        text = output.decode(errors="replace")
        if len(text) > 24000:
            text = text[:12000] + "\n[output middle truncated]\n" + text[-12000:]
        context.result = text + f"\n[exit_code: {process.returncode}]"
        context.metadata[TOOL_RESULT_METADATA_KEY] = {
            "shell_exit_code": process.returncode,
            "failed": process.returncode != 0,
            "compass_container_execution": True,
        }
        self.owner.audit(
            "container_tool", tool="execute", exit_code=process.returncode, command_sha256=sha(args["command"])
        )


class BriefChat(ChatMiddleware):
    def __init__(self, owner, injection):
        self.owner, self.injection = owner, injection
        self.pending = None

    async def process(self, context, call_next):
        c = self.owner
        c.check_budget()
        if context.stream:
            raise RuntimeError("R5 experiment profile must use stream=false")
        await c.capture(context.messages, maintain=True)
        facts = c.core.delivery_facts()
        snapshot = c.core.claims.snapshot()
        active = {x.id: x for x in snapshot.claims if x.id in snapshot.state.active_ids}
        conflicts = set(requirement_conflicts(active, snapshot.conflict_notes)) - c.conflicts
        reason = delivery_reason(facts, c.mark)
        text = ""
        if c.calls > 200 and c.handoff_seen:
            if not c.closing_seen:
                text, reason = CLOSING, "closing-budget"
        elif c.started and (not c.handoff_seen or reason or conflicts):
            text = c.core.brief_for_request(recipient="model", phase="icode")
            reason = reason or "exploration-or-conflict"
            if conflicts:
                text += (
                    "\n\n[Compass — new requirement conflict]\n"
                    + "\n".join(sorted(conflicts))
                    + "\nBefore continuing, re-read the quoted requirement in the task text and state in one sentence whether "
                    "your behavior follows it. If it does not, change the code. " + CONFLICT_REVIEW_GUIDANCE
                )
        if text and self.pending is None:
            if not c.rules_seen:
                text += c.constants["SUBMISSION_RULES"]
            self.pending = (text, facts, conflicts, reason)
            self.injection.queue(text, injection_id="compass:" + sha(text))

        def sent(messages):
            if self.pending and any(self.pending[0] in m.text for m in messages):
                payload, delivered_facts, new_conflicts, why = self.pending
                c.mark = c.mark.after(delivered_facts)
                c.conflicts.update(new_conflicts)
                c.rules_seen = True
                c.handoff_seen = c.started
                c.closing_seen |= why == "closing-budget"
                c.audit("brief_delivered", reason=why, text=payload, sha256=sha(payload))
                self.pending = None

        context.request_message_observers.append(sent)
        # iCode observes messages both before and after provider preparation.
        # Count the native model invocation once, not those two notifications.
        c.check_budget()
        c.calls += 1
        c.audit("main_request")
        await call_next()
        response = context.result
        c.batch_size = sum(item.type == "function_call" for m in response.messages for item in m.contents)
        await c.capture(response.messages, maintain=False)
        c.audit("main_response", model=response.model, response_id=response.response_id, usage=response.usage_details)
        c.save()
        if self.pending is not None:
            c.fail_handoff("r5_brief_delivery_unconfirmed")


def install_runtime(owner):
    from chrys.orchestration.engine.build import builder
    from chrys.orchestration.session_host import ChrysSessionHost

    original_create = builder.create_runtime
    original_run = ChrysSessionHost.run_until_final

    def create(*args, **kwargs):
        runtime = original_create(*args, **kwargs)
        if owner.container:
            shell = next(
                tool
                for tool in runtime.agent.default_options["tools"]
                if get_tool_kind(tool) == "shell" and tool.name != "list_workspace_dirs"
            )
            shell.name = "execute"
            shell.description = (
                "Execute a bash command inside the isolated Linux task container. "
                "Use this for reading, searching, writing, builds and tests. Default workdir /app; "
                "no host files or credentials; network follows the task sandbox policy. Timeout 1-300 seconds, no asynchronous handle. "
                "Output is bounded and includes the real process exit code."
            )
            runtime.agent.default_options["tools"] = [
                tool
                for tool in runtime.agent.default_options["tools"]
                if tool.name in {"execute", "start_compass_brief", "inspect_compass_brief"}
            ]
            runtime.agent.context_providers = [
                provider
                for provider in runtime.agent.context_providers
                if type(provider).__name__ != "ChrysSkillsProvider"
            ]
        owner.tool_kinds.update({tool.name: get_tool_kind(tool) for tool in runtime.agent.default_options["tools"]})
        runtime.agent.middleware = [
            ExplorationGuard(owner),
            *([ContainerShell(owner)] if owner.container else []),
            BriefChat(owner, runtime.injection),
            *runtime.agent.middleware,
        ]
        return runtime

    async def run(host, message, **kwargs):
        try:
            async with asyncio.timeout(max(0, owner.remaining())):
                result = await original_run(host, message, **kwargs)
                owner.check_budget()
                if not owner.started or not owner.handoff_seen:
                    owner.fail_handoff("r5_handoff_incomplete_at_submission")
                if owner.started and not owner.check_done:
                    owner.check_done = True
                    held = MAX_CALLS - owner.calls >= 15 and owner.remaining() > 0
                    prompt = owner.constants["SUBMISSION_CHECK"] + (
                        "\nWithin the remaining budget, check/fix or explain unresolved items, then submit again."
                        if held
                        else "\nInsufficient remaining budget for a held check: accepting this submission immediately."
                    )
                    owner.audit("submission_check", held=held, text=prompt)
                    if held:
                        result = await original_run(host, prompt, **kwargs)
                if owner.started and owner.calls < MAX_CALLS:
                    await owner.core.maintain(phase="icode", stage_end=True)
                return result
        finally:
            await owner.core.before_fork()
            owner.save()
            if owner.model:
                await owner.model.client.close()

    builder.create_runtime, ChrysSessionHost.run_until_final = create, run
    return original_create, original_run


def configure(directory, upstream, *, container=False):
    import yaml

    config = directory / "config"
    agent = yaml.safe_load((upstream / "src/chrys/service/profiles/agents/builtins/Code.yaml").read_text())
    agent.update(name="R5Code", id="c0de05003400", extensions=["compass_r5"])
    agent["sub_agents"] = {"agents": []}
    agent["skills"] = {
        "paths": [],
        "inline": [],
        "auto_load_user_agents_skills": False,
        "auto_load_cwd_agents_skills": False,
    }
    agent["tools"]["builtins"] = ["filesystem.read", "filesystem.write", "shell", "search"]
    agent["instructions"] += (
        "\nCompass workflow: first explore the relevant repository entry points, implementation, callers and tests. "
        "Before editing call start_compass_brief alone with your exploration findings, source paths and unresolved assumptions. "
        "Wait for its G/R/K and Brief, review them, then implement and check the task. "
        "The original issue remains authoritative; repository findings are reference material. "
        "Use inspect_compass_brief to read full records. Briefs are updated context, not new tool evidence. "
        "A passing self-written check proves only the exercised conditions.\n"
    )
    if container:
        agent["tools"]["builtins"] = ["shell"]
        agent["instructions"] += (
            "\nBenchmark execution: the only repository tool is execute. It runs bash inside an isolated Linux container, "
            "default directory /app. Use execute for reading, searching, editing and testing; dedicated file tools are not available. "
            "Tool workdir refers to a path INSIDE the container. Host paths in interface metadata are not repository paths. "
            "Host files are unavailable. Network access follows the task sandbox policy. Do not use the mini-agent submission marker; finish with your normal final response.\n"
        )
    model = {
        "id": MODEL_PROFILE,
        "name": "DeepSeek official R5",
        "provider": "deepseek-openai",
        "api_style": "chat_completions",
        "model_id": MODEL,
        "base_url": BASE_URL,
        "api_key": "{{DEEPSEEK_API_KEY}}",
        "stream": False,
        "max_context_tokens": 1000000,
        "max_output_tokens": 64000,
        "http_max_retries": 0,
        "chat_options": dump({"reasoning_effort": "max", "extra_body": {"thinking": {"type": "enabled"}}}),
    }
    for path, value in [(config / "agents/c0de05003400.yaml", agent), (config / f"models/{MODEL_PROFILE}.yaml", model)]:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        Artifacts.write_text(path, yaml.safe_dump(value, allow_unicode=True, sort_keys=False))
    path = config / "extensions/compass_r5.py"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    Artifacts.write_text(path, "from r5_native import extension\n")
    return config


def bind_config(config):
    import chrys.foundation.platform as platform

    platform.conventional_config_dir = lambda: config
    platform.get_platform.cache_clear()
    if platform.get_platform().config_dir != config.resolve():
        raise RuntimeError("Isolated config binding failed")
