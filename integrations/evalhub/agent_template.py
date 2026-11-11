"""EvalHub entrypoint for Compass v1.4.2 + iCode, fetched from a named Git branch."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import signal
import tempfile
import uuid
from pathlib import Path

from harbor.agents.base import BaseAgent

SANDBOX_BOOTSTRAP = ""  # Filled by build_agent.py in the standalone upload.

REPO_URL = "https://github.com/spine-se-lab/iCode-brief.git"
REPO_REF = "codex/compass-v1.4.2-evalhub"
ICODE_COMMIT = "ffdc67ed74c1c2d78d16ab4b8544fed8d9ee3c69"
R5_ARCHIVE = "2866727c047a5bd4746f6d25b0ad64ebc717332b07f9842e3be607c98c95291e"


async def stop_process(process):
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), 10)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


def observed_usage(events_path):
    counts = {"n_input_tokens": 0, "n_output_tokens": 0, "n_cache_tokens": 0}
    missing = set()
    responses = {"main_response": 0, "maintenance_response": 0}
    budget_exhausted = False
    fields = {
        "main_response": ("input_token_count", "output_token_count", "cache_read_input_token_count"),
        "maintenance_response": ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens"),
    }
    if events_path.exists():
        for line in events_path.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("event")
            budget_exhausted |= kind == "budget_exhausted"
            if kind not in fields:
                continue
            responses[kind] += 1
            usage = event.get("usage") or {}
            for target, source in zip(counts, fields[kind], strict=True):
                value = usage.get(source)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    counts[target] += int(value)
                else:
                    missing.add(target)
    if not sum(responses.values()):
        missing.update(counts)
    return {key: None if key in missing else value for key, value in counts.items()}, responses, budget_exhausted


class R5ICodeAgent(BaseAgent):
    """Compass v1.4.2 + iCode, with tools executed by the assigned Harbor sandbox."""

    def __init__(
        self,
        *args,
        workdir=None,
        base_url=None,
        api_model=None,
        api_key_env="DEEPSEEK_API_KEY",
        runtime_python=None,
        repo_ref=REPO_REF,
        repo_commit=None,
        setup_timeout=1200,
        prompt_template_path=None,
        extra_env=None,
        execution_mode="sandbox",
        **kwargs,
    ):
        super().__init__(*args, extra_env=extra_env, **kwargs)
        self.workdir = workdir
        self.base_url = base_url
        self.api_model = api_model
        self.api_key_env = api_key_env
        self.runtime_python = runtime_python
        if not isinstance(repo_ref, str) or not repo_ref or repo_ref.startswith("-"):
            raise ValueError("repo_ref must name a Git branch")
        if repo_commit is not None and (
            len(repo_commit) != 40 or any(c not in "0123456789abcdef" for c in repo_commit)
        ):
            raise ValueError("repo_commit must be a full lowercase Git commit SHA")
        self.repo_ref = repo_ref
        self.repo_commit = repo_commit
        self.setup_timeout = int(setup_timeout)
        self.prompt_template_path = Path(prompt_template_path) if prompt_template_path else None
        self.model_env = dict(getattr(self, "extra_env", {}) or {})
        self.model_env.update(extra_env or {})
        self.runtime = Path(self.logs_dir).resolve() / "r5-runtime"
        self.python = None
        if execution_mode not in ("sandbox", "runner"):
            raise ValueError("execution_mode must be sandbox or runner")
        self.execution_mode = execution_mode
        self.sandbox_root = "/logs/agent/r5-" + uuid.uuid4().hex

    @staticmethod
    def name():
        return "r5-icode"

    def version(self):
        return "compass-v1.4.2-icode-0.34.0-git"

    async def setup(self, environment):
        if self.execution_mode == "sandbox":
            return await self._setup_sandbox(environment)
        return await self._setup_runner(environment)

    async def _sandbox_command(self, environment, action, timeout):
        command = "python3 " + shlex.quote(self.sandbox_root + "/sandbox_agent.py") + " " + action
        try:
            result = await environment.exec(command=command, timeout_sec=timeout)
        except Exception as error:
            if action != "wait":
                raise
            self._record_poll_failure(type(error).__name__)
            return "UNKNOWN"
        if action == "wait" and (result.return_code is None or result.return_code < 0):
            self._record_poll_failure("exec_return_code", result.return_code)
            return "UNKNOWN"
        if result.return_code:
            raise RuntimeError(f"Sandbox {action} failed (exit {result.return_code}); see {self.sandbox_root} logs")
        state = (result.stdout or "").strip()
        if action == "wait" and state not in ("RUNNING", "DONE"):
            self._record_poll_failure("invalid_status")
            return "UNKNOWN"
        return state

    def _record_poll_failure(self, reason, return_code=None):
        # Keep only transport metadata, never response bodies or credentials.
        with (Path(self.logs_dir) / "poll-errors.jsonl").open("a") as log:
            log.write(json.dumps({"reason": reason, "return_code": return_code}) + "\n")

    async def _download_sandbox_logs(self, environment):
        try:
            available = json.loads(await self._sandbox_command(environment, "logs", 30))
            if not isinstance(available, list):
                raise ValueError("Invalid log manifest")
        except Exception as error:
            (Path(self.logs_dir) / "log-collection-error.json").write_text(
                json.dumps({"error_type": type(error).__name__})
            )
            return
        for name in (
            "outcome.json",
            "prepared.json",
            "worker.log",
            "bootstrap.log",
            "runtime-setup.log",
            "source-verification.log",
            "generation.log",
            "agent/adapter-events.jsonl",
            "agent/failure.json",
            "agent/handoff-status.json",
            "submission.json",
            "submission.patch",
            "source-identity.json",
        ):
            if name not in available:
                continue
            target = Path(self.logs_dir) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                await environment.download_file(source_path=self.sandbox_root + "/" + name, target_path=str(target))
            except Exception as error:
                # Full originals remain under /logs/agent for platform artifact collection.
                with (Path(self.logs_dir) / "log-download-errors.jsonl").open("a") as log:
                    log.write(json.dumps({"file": name, "error_type": type(error).__name__}) + "\n")

    async def _setup_sandbox(self, environment):
        logs = Path(self.logs_dir)
        logs.mkdir(parents=True, exist_ok=True)
        result = await environment.exec(command="mkdir -p " + shlex.quote(self.sandbox_root), timeout_sec=30)
        if result.return_code:
            raise RuntimeError("Cannot create the sandbox agent directory")
        options = {
            "model_name": self.model_name,
            "api_model": self.api_model,
            "workdir": self.workdir,
            "runtime_python": self.runtime_python,
            "setup_timeout": self.setup_timeout,
            "repo_ref": self.repo_ref,
            "repo_commit": self.repo_commit,
        }
        (logs / "sandbox-options.json").write_text(json.dumps(options))
        (logs / "sandbox_agent.py").write_text(SANDBOX_BOOTSTRAP)
        for source, name in (
            (Path(__file__), "upload.py"),
            (logs / "sandbox-options.json", "options.json"),
            (logs / "sandbox_agent.py", "sandbox_agent.py"),
        ):
            await environment.upload_file(source_path=str(source), target_path=self.sandbox_root + "/" + name)
        try:
            await self._sandbox_command(environment, "setup", self.setup_timeout + 120)
            self.python = "sandbox-prepared"
        finally:
            await self._download_sandbox_logs(environment)

    async def _run_sandbox(self, instruction, environment, context):
        if self.python != "sandbox-prepared":
            raise RuntimeError("Call setup before run")
        if self.prompt_template_path is not None:
            from harbor.utils.templating import render_prompt_template

            instruction = render_prompt_template(
                self.prompt_template_path, instruction, task_dir=environment.environment_dir.parent
            )
        task_path = Path(self.logs_dir) / "instruction.txt"
        task_path.write_text(instruction)
        await environment.upload_file(source_path=str(task_path), target_path=self.sandbox_root + "/task.txt")
        launch = asyncio.create_task(self._sandbox_command(environment, "start", 30))
        try:
            await asyncio.shield(launch)
            deadline = asyncio.get_running_loop().time() + 11460
            failed_polls = 0
            while asyncio.get_running_loop().time() < deadline:
                state = await self._sandbox_command(environment, "wait", 30)
                if state == "DONE":
                    break
                failed_polls = failed_polls + 1 if state == "UNKNOWN" else 0
                if failed_polls >= 6:
                    raise RuntimeError(
                        "Sandbox observation unavailable for 6 consecutive polls; "
                        "stopping the owned worker; see poll-errors.jsonl"
                    )
                # Wait on the runner, not inside a remote exec connection.
                await asyncio.sleep(min(20, max(0, deadline - asyncio.get_running_loop().time())))
            else:
                raise TimeoutError("Sandbox solver exceeded the generation deadline")
        finally:
            # Settle a submitted launch before stopping its owned worker.
            try:
                await launch
            finally:
                try:
                    await self._sandbox_command(environment, "stop", 510)
                finally:
                    await self._download_sandbox_logs(environment)
        outcome_path = Path(self.logs_dir) / "outcome.json"
        if not outcome_path.exists():
            raise RuntimeError("Sandbox outcome download failed; see platform /logs/agent artifacts")
        outcome = json.loads(outcome_path.read_text())
        for name, value in outcome["context"].items():
            if name == "metadata":
                context.metadata = {**(context.metadata or {}), **(value or {})}
            else:
                setattr(context, name, value)
        context.metadata = {
            **(context.metadata or {}),
            "evalhub_gateway": {
                "execution": "task_sandbox",
                "credentials": "injected_OPENAI_environment",
                "artifacts": self.sandbox_root,
            },
        }
        if outcome["error"]:
            failure = (context.metadata or {}).get("r5_icode", {}).get("failure")
            if failure:
                raise RuntimeError(
                    "Sandbox R5/iCode failed: "
                    + failure["code"]
                    + "; stage="
                    + failure["stage"]
                    + "; see agent/failure.json"
                )
            raise RuntimeError("Sandbox R5/iCode failed (" + outcome["error"] + "); see generation.log")

    async def _setup_runner(self, environment):
        Path(self.logs_dir).mkdir(parents=True, exist_ok=True)
        result = await environment.exec("pwd -P", cwd=self.workdir, timeout_sec=30)
        if result.return_code:
            raise RuntimeError("Cannot resolve the task working directory")
        self.workdir = (result.stdout or "").strip()
        if not self.workdir.startswith("/") or "\n" in self.workdir:
            raise RuntimeError("The task must supply an absolute Linux working directory")
        if self.runtime.exists():
            raise RuntimeError("Runtime directory already exists; use a fresh trial")
        setup_env = dict(os.environ)
        setup_env["GIT_TERMINAL_PROMPT"] = "0"
        setup_env["UV_PROJECT_ENVIRONMENT"] = str(self.runtime / ".venv")
        deadline = asyncio.get_running_loop().time() + self.setup_timeout

        async def command(argv, log):
            process = await asyncio.create_subprocess_exec(
                *argv,
                env=setup_env,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            try:
                code = await asyncio.wait_for(process.wait(), max(0, deadline - asyncio.get_running_loop().time()))
            finally:
                await stop_process(process)
            if code:
                raise RuntimeError("Runtime setup failed; see runtime-setup.log")

        with (Path(self.logs_dir) / "runtime-setup.log").open("w") as log:
            await command(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--single-branch",
                    "--branch",
                    self.repo_ref,
                    "--",
                    REPO_URL,
                    str(self.runtime),
                ],
                log,
            )
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(self.runtime),
                "rev-parse",
                "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=log,
                start_new_session=True,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(), max(0, deadline - asyncio.get_running_loop().time())
                )
            finally:
                await stop_process(process)
            if process.returncode:
                raise RuntimeError("Cannot identify the cloned source")
            commit = stdout.decode().strip()
            if self.repo_commit is not None and commit != self.repo_commit:
                raise RuntimeError("Branch HEAD does not match repo_commit")
            (Path(self.logs_dir) / "source-identity.json").write_text(
                json.dumps(
                    {
                        "repository": REPO_URL,
                        "branch": self.repo_ref,
                        "commit": commit,
                        "icode_upstream": ICODE_COMMIT,
                        "compass_version": "1.4.2",
                        "compass_archive_sha256": R5_ARCHIVE,
                    },
                    indent=2,
                )
            )
            if self.runtime_python:
                self.python = self.runtime_python
            else:
                if shutil.which("uv") is None:
                    raise RuntimeError("Provide uv or runtime_python with the pinned dependencies")
                await command(
                    [
                        "uv",
                        "sync",
                        "--frozen",
                        "--no-dev",
                        "--no-default-groups",
                        "--python",
                        "3.14.0",
                        "--project",
                        str(self.runtime),
                    ],
                    log,
                )
                self.python = str(self.runtime / ".venv/bin/python")
            # Validate the exact core and its import path before any model call.
            setup_env["PYTHONPATH"] = os.pathsep.join(
                [
                    str(self.runtime / "integrations/evalhub"),
                    str(self.runtime / "integrations/compass/src"),
                    str(self.runtime / "src"),
                ]
            )
            await command(
                [
                    self.python,
                    "-c",
                    (
                        "import sys; from pathlib import Path; import r5_native; "
                        "assert sys.version_info >= (3, 14); "
                        "r5_native.frozen_constants(Path(sys.argv[1]).resolve())"
                    ),
                    str(self.runtime / "integrations/compass"),
                ],
                log,
            )

    async def run(self, instruction, environment, context):
        if self.execution_mode == "sandbox":
            return await self._run_sandbox(instruction, environment, context)
        return await self._run_runner(instruction, environment, context)

    async def _run_runner(self, instruction, environment, context):
        if self.python is None:
            raise RuntimeError("Call setup before run")
        if self.prompt_template_path is not None:
            from harbor.utils.templating import render_prompt_template

            instruction = render_prompt_template(
                self.prompt_template_path, instruction, task_dir=environment.environment_dir.parent
            )
        model = self.api_model if self.api_model is not None else self.model_name
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Select a job model or set api_model to the provider model ID")
        if self.api_model is None and model == "deepseek/deepseek-flash":
            model = "deepseek-flash"
        key = self.model_env.get(self.api_key_env) or os.environ.get(self.api_key_env)
        if not key:
            raise RuntimeError(f"Supply {self.api_key_env} through the job environment variables")
        base_url = (
            self.base_url
            or self.model_env.get("DEEPSEEK_BASE_URL")
            or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        )
        logs = Path(self.logs_dir).resolve()
        task_path = logs / "instruction.txt"
        task_path.write_text(instruction, encoding="utf-8")
        task_path.chmod(0o600)
        events_path = logs / "agent/adapter-events.jsonl"
        if events_path.exists():
            raise RuntimeError("This agent supports fresh trials only; use a new trial logs directory")
        pending = set()

        async def execute(reader, writer):
            current = asyncio.current_task()
            pending.add(current)
            try:
                try:
                    request = json.loads(await asyncio.wait_for(reader.readline(), 10))
                    result = await environment.exec(
                        request["command"],
                        cwd=request["cwd"],
                        timeout_sec=min(300, max(1, int(request["timeout_sec"]))),
                    )
                    output = (result.stdout or "") + (result.stderr or "")
                    if len(output) > 24000:
                        output = output[:12000] + "\n[output middle truncated]\n" + output[-12000:]
                    response = {"return_code": result.return_code, "output": output}
                except TimeoutError:
                    response = {"return_code": 124, "output": "Command timed out"}
                except Exception as error:
                    response = {"error": type(error).__name__}
                try:
                    writer.write((json.dumps(response) + "\n").encode())
                    await writer.drain()
                except ConnectionError:
                    pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
                finally:
                    pending.discard(current)

        with tempfile.TemporaryDirectory(prefix="r5-harbor-") as temporary:
            socket_path = str(Path(temporary) / "exec.sock")
            server = await asyncio.start_unix_server(execute, path=socket_path, limit=1048576)
            settings = {
                "socket": socket_path,
                "workdir": self.workdir,
                "model": model,
                "base_url": base_url,
                "agent_dir": str(logs / "agent"),
                "workspace": str(logs / "runner-workspace"),
                "task_path": str(task_path),
            }
            settings_path = logs / "runtime-settings.json"
            settings_path.write_text(json.dumps(settings), encoding="utf-8")
            env = dict(os.environ)
            for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
                env.pop(name, None)
            env["R5_EVALHUB_API_KEY"] = key
            env["PYTHONPATH"] = os.pathsep.join(
                [
                    str(self.runtime / "integrations/evalhub"),
                    str(self.runtime / "integrations/compass/src"),
                    str(self.runtime / "src"),
                ]
            )
            process = None
            return_code = None
            wall_timeout = False
            try:
                async with server:
                    with (logs / "generation.log").open("w") as log:
                        process = await asyncio.create_subprocess_exec(
                            self.python,
                            str(self.runtime / "integrations/evalhub/runtime_bridge.py"),
                            str(settings_path),
                            cwd=self.runtime,
                            env=env,
                            stdout=log,
                            stderr=log,
                            start_new_session=True,
                        )
                        return_code = await asyncio.wait_for(process.wait(), 11400)
            except TimeoutError:
                wall_timeout = True
            finally:
                if process is not None:
                    await stop_process(process)
                    return_code = process.returncode
                server.close()
                await server.wait_closed()
                # Let the bounded sandbox command settle before Harbor starts verification.
                await asyncio.gather(*list(pending), return_exceptions=True)
                counts, responses, exhausted = observed_usage(events_path)
                exhausted |= wall_timeout
                for name, value in counts.items():
                    setattr(context, name, value)
                context.metadata = {
                    **(context.metadata or {}),
                    "r5_icode": {
                        "upstream": ICODE_COMMIT,
                        "r5_archive": R5_ARCHIVE,
                        "compass_version": "1.4.2",
                        "source": json.loads((logs / "source-identity.json").read_text()),
                        "platform_model_name": self.model_name,
                        "api_model": model,
                        "generation_exit_code": return_code,
                        "budget_exhausted": exhausted,
                        "wall_timeout": wall_timeout,
                        "responses": responses,
                        "transport": "harbor.environment.exec",
                    },
                }
            # Only fixed diagnostic codes are surfaced. Never expose SDK messages.
            failure = None
            failure_path = logs / "agent/failure.json"
            if failure_path.exists():
                candidate = json.loads(failure_path.read_text())
                if candidate.get("code") in {
                    "r5_contract_start_failed",
                    "r5_contract_extraction_failed",
                    "r5_contract_extraction_timeout",
                    "r5_initial_maintenance_failed",
                    "r5_brief_delivery_unconfirmed",
                    "r5_handoff_incomplete_at_submission",
                }:
                    failure = {"code": candidate["code"], "stage": "r5_handoff"}
            status_path = logs / "agent/handoff-status.json"
            if status_path.exists() and (return_code == 0 or exhausted):
                status = json.loads(status_path.read_text())
                if not status.get("started") or not status.get("brief_delivered"):
                    failure = failure or {"code": "r5_handoff_incomplete_at_submission", "stage": "r5_handoff"}
            if failure:
                context.metadata["r5_icode"]["failure"] = failure
                raise RuntimeError(failure["code"])
            if return_code and not exhausted:
                raise RuntimeError(f"R5/iCode exited with code {return_code}; see generation.log")


# The build appends only the sandbox bootstrap, without a runtime archive.
