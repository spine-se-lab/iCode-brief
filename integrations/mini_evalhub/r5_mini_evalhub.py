"""EvalHub entrypoint for Original R5 + mini-swe-agent 2.4.6, fetched from a named Git branch."""

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
REPO_REF = "codex/r5-mini-original-evalhub"
MINI_VERSION = "2.4.6"
R5_ARCHIVE = "62c304ae6315ffb1c977def04b77fb9c552ac75ceb533fd249c2b0180818d257"


async def stop_process(process, grace_seconds=10):
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), grace_seconds)
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
        "main_response": ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens"),
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


class R5MiniAgent(BaseAgent):
    """Original R5 + mini-swe-agent 2.4.6, with tools executed by the assigned Harbor sandbox."""

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
        return "r5-mini-original"

    def version(self):
        return "r5-original-mini-2.4.6-git-v2"

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
            "agent/run-status.json",
            "agent/trajectory.json",
            "agent/compass-artifacts.tar.gz",
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
            deadline = asyncio.get_running_loop().time() + 11800
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
            raise RuntimeError("Sandbox R5/mini-swe-agent failed (" + outcome["error"] + "); see generation.log")

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
                        "mini_swe_agent": MINI_VERSION,
                        "compass_version": "0.2.40-r5-original",
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
                        str(self.runtime / "integrations/mini_evalhub"),
                    ],
                    log,
                )
                self.python = str(self.runtime / ".venv/bin/python")
            # Validate the exact core and its import path before any model call.
            setup_env["PYTHONPATH"] = os.pathsep.join(
                [
                    str(self.runtime / "integrations/mini_evalhub"),
                    str(self.runtime / "integrations/compass/src"),
                ]
            )
            await command(
                [
                    self.python,
                    "-c",
                    (
                        "import sys; from pathlib import Path; import runtime_bridge; "
                        "assert sys.version_info >= (3, 14); "
                        "runtime_bridge.verify_source(Path(sys.argv[1]).resolve())"
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
                    response = {
                        "return_code": result.return_code,
                        "output": output,
                        "exception_info": getattr(result, "exception_info", ""),
                    }
                except TimeoutError:
                    response = {"return_code": -1, "output": "", "exception_info": "Command timed out"}
                except Exception as error:
                    response = {"return_code": -1, "output": "", "exception_info": type(error).__name__}
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
                    str(self.runtime / "integrations/mini_evalhub"),
                    str(self.runtime / "integrations/compass/src"),
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
                            str(self.runtime / "integrations/mini_evalhub/runtime_bridge.py"),
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
                    await stop_process(process, grace_seconds=30)
                    return_code = process.returncode
                server.close()
                await server.wait_closed()
                # Let the bounded sandbox command settle before Harbor starts verification.
                await asyncio.gather(*list(pending), return_exceptions=True)
                counts, responses, exhausted = observed_usage(events_path)
                for name, value in counts.items():
                    setattr(context, name, value)
                context.metadata = {
                    **(context.metadata or {}),
                    "r5_mini": {
                        "mini_swe_agent": MINI_VERSION,
                        "r5_archive": R5_ARCHIVE,
                        "compass_version": "0.2.40-r5-original",
                        "source": json.loads((logs / "source-identity.json").read_text()),
                        "platform_model_name": self.model_name,
                        "api_model": model,
                        "generation_exit_code": 124 if wall_timeout else return_code,
                        "generation_process_exit_code": return_code,
                        "budget_exhausted": exhausted,
                        "wall_timeout": wall_timeout,
                        "responses": responses,
                        "transport": "harbor.environment.exec",
                    },
                }
            status = {}
            status_path = logs / "agent/run-status.json"
            if status_path.exists():
                status = json.loads(status_path.read_text())
                context.metadata["r5_mini"]["result"] = status
            # Historical generation and grading are separate. Preserve nonzero
            # terminal codes while allowing the retained candidate to be graded.
            candidate_terminal = return_code == 1 and status.get("exit_status") in {
                "LimitsExceeded",
                "RepeatedFormatError",
            }
            if return_code and not (candidate_terminal or wall_timeout):
                raise RuntimeError(f"R5/mini-swe-agent exited with code {return_code}; see generation.log")


# The build appends only the sandbox bootstrap, without a runtime archive.

SANDBOX_BOOTSTRAP = '"""Run the existing R5 wrapper inside the assigned EvalHub sandbox."""\n\nimport asyncio\nimport contextlib\nimport hashlib\nimport importlib.util\nimport json\nimport os\nimport shutil\nimport signal\nimport subprocess\nimport sys\nimport time\nfrom pathlib import Path\nfrom types import ModuleType, SimpleNamespace\n\nROOT = Path(__file__).resolve().parent\n\n\ndef save(name, value):\n    path = ROOT / name\n    temporary = path.with_suffix(".tmp")\n    temporary.write_text(json.dumps(value))\n    temporary.replace(path)\n\n\ndef load_agent():\n    # The sandbox needs the solver, while Harbor itself remains on the runner.\n    base = ModuleType("harbor.agents.base")\n\n    class BaseAgent:\n        def __init__(self, logs_dir, model_name=None, extra_env=None, **kwargs):\n            self.logs_dir = logs_dir\n            self.model_name = model_name\n            self.extra_env = extra_env or {}\n\n    base.BaseAgent = BaseAgent\n    sys.modules["harbor.agents.base"] = base\n    spec = importlib.util.spec_from_file_location("r5_upload", ROOT / "upload.py")\n    module = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(module)\n    return module\n\n\nclass Environment:\n    async def exec(self, command, *, cwd=None, timeout_sec=30):\n        # Repository tools do not inherit gateway credentials or installation settings.\n        env = {\n            name: os.environ[name]\n            for name in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM")\n            if name in os.environ\n        }\n        process = await asyncio.create_subprocess_exec(\n            "bash",\n            "-c",\n            command,\n            cwd=cwd,\n            env=env,\n            stdout=asyncio.subprocess.PIPE,\n            stderr=asyncio.subprocess.STDOUT,\n            start_new_session=True,\n        )\n        communication = asyncio.create_task(process.communicate())\n        timed_out = False\n        try:\n            await asyncio.wait_for(asyncio.shield(communication), timeout_sec)\n        except TimeoutError:\n            timed_out = True\n        finally:\n            if not communication.done():\n                with contextlib.suppress(ProcessLookupError):\n                    os.killpg(process.pid, signal.SIGKILL)\n            await process.wait()\n            stdout, _ = await communication\n        return SimpleNamespace(\n            return_code=-1 if timed_out else process.returncode,\n            stdout=stdout.decode(errors="replace"),\n            stderr="",\n            exception_info="Command timed out" if timed_out else "",\n        )\n\n\ndef solver(module):\n    options = json.loads((ROOT / "options.json").read_text())\n    return module.R5MiniAgent(\n        logs_dir=ROOT,\n        model_name=options["model_name"],\n        api_model=options["api_model"],\n        workdir=options["workdir"],\n        runtime_python=options["runtime_python"],\n        setup_timeout=options["setup_timeout"],\n        execution_mode="runner",\n        repo_ref=options["repo_ref"],\n        repo_commit=options["repo_commit"],\n        api_key_env="OPENAI_API_KEY",\n        base_url=os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE"),\n    )\n\n\ndef require_gateway():\n    if not os.environ.get("OPENAI_API_KEY") or not (\n        os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE")\n    ):\n        raise RuntimeError("EvalHub must inject OPENAI_API_KEY and OPENAI_BASE_URL into the task sandbox")\n\n\nasync def setup():\n    require_gateway()\n    module = load_agent()\n    agent = solver(module)\n    if not agent.runtime_python and shutil.which("uv") is None:\n        # Install only into this trial\'s directory. Use the configured pip index/cache.\n        bootstrap = ROOT / "bootstrap"\n        with (ROOT / "bootstrap.log").open("w") as log:\n            process = await asyncio.create_subprocess_exec(\n                sys.executable,\n                "-m",\n                "pip",\n                "install",\n                "--target",\n                str(bootstrap),\n                "uv",\n                stdout=log,\n                stderr=log,\n                start_new_session=True,\n            )\n            try:\n                code = await asyncio.wait_for(process.wait(), agent.setup_timeout)\n            finally:\n                await module.stop_process(process)\n            if code:\n                raise RuntimeError("Provide uv or Python pip in the sandbox; see bootstrap.log")\n        os.environ["PATH"] = str(bootstrap / "bin") + os.pathsep + os.environ.get("PATH", "")\n    await agent.setup(Environment())\n    save("prepared.json", {"python": agent.python, "workdir": agent.workdir})\n\n\nasync def bounded_setup():\n    task = asyncio.current_task()\n    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)\n    options = json.loads((ROOT / "options.json").read_text())\n    await asyncio.wait_for(setup(), options["setup_timeout"])\n\n\ndef finalize_workspace(workdir, initial_head, artifacts=Path("/logs/artifacts")):\n    """Expose the complete candidate to EvalHub\'s working-tree patch collector."""\n\n    def git(*args, data=None):\n        return subprocess.run(\n            ["git", *args], cwd=workdir, input=data, capture_output=True, check=True, timeout=120\n        ).stdout\n\n    final_head = git("rev-parse", "--verify", "HEAD").decode().strip()\n    # Retain commits and the entire final tracked/non-ignored candidate before\n    # moving HEAD/index. Never reset --hard or change candidate file contents.\n    git("update-ref", "refs/r5-evalhub/model-head", final_head)\n    git("add", "-A")\n    tree = git("write-tree").decode().strip()\n    git("update-ref", "refs/r5-evalhub/submission-tree", tree)\n    flags = ("--binary", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames")\n    candidate = git("diff", *flags, initial_head, tree, "--")\n    (ROOT / "submission.patch").write_bytes(candidate)\n    added = git("diff", "--name-only", "--diff-filter=A", "--no-renames", "-z", initial_head, tree, "--")\n    git("reset", "--mixed", initial_head)\n    if added:\n        # Intent-to-add makes even new files visible to plain git diff. Force\n        # includes files the model committed despite a matching ignore rule.\n        git("add", "-N", "-f", "--pathspec-from-file=-", "--pathspec-file-nul", data=added)\n    exported = git("diff", *flags, "HEAD", "--")\n    if exported != candidate or git("diff", *flags, "--") != candidate:\n        raise RuntimeError("Workspace patch differs from the saved candidate")\n    artifacts.mkdir(parents=True, exist_ok=True)\n    (artifacts / "patch.diff").write_bytes(candidate)\n    receipt = {\n        "initial_head": initial_head,\n        "model_head": final_head,\n        "candidate_tree": tree,\n        "patch_bytes": len(candidate),\n        "patch_sha256": hashlib.sha256(candidate).hexdigest(),\n        "working_tree_diff_verified": True,\n        "commit_backup_ref": "refs/r5-evalhub/model-head",\n        "tree_backup_ref": "refs/r5-evalhub/submission-tree",\n    }\n    save("submission.json", receipt)\n    return receipt\n\n\nasync def run():\n    require_gateway()\n    # Match the reference agent\'s bypass_proxy behavior for the internal gateway.\n    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):\n        os.environ.pop(name, None)\n    module = load_agent()\n    agent = solver(module)\n    prepared = json.loads((ROOT / "prepared.json").read_text())\n    agent.python = prepared["python"]\n    agent.workdir = prepared["workdir"]\n    context = SimpleNamespace(metadata=None)\n    environment = Environment()\n    head = await environment.exec("git rev-parse --verify HEAD", cwd=agent.workdir)\n    initial_head = head.stdout.strip() if head.return_code == 0 else None\n    task = asyncio.current_task()\n    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)\n    error = None\n    try:\n        await agent.run((ROOT / "task.txt").read_text(), environment, context)\n    except BaseException as exception:\n        error = type(exception).__name__\n    finally:\n        if initial_head:\n            try:\n                receipt = await asyncio.to_thread(finalize_workspace, agent.workdir, initial_head)\n                context.metadata = {\n                    **(context.metadata or {}),\n                    "workspace_finalize_exit_code": 0,\n                    "submission": receipt,\n                }\n            except Exception as exception:\n                error = error or "WorkspaceSubmissionError"\n                context.metadata = {**(context.metadata or {}), "workspace_finalize_error": type(exception).__name__}\n        else:\n            error = error or "MissingInitialGitHead"\n        save("outcome.json", {"error": error, "context": vars(context)})\n\n\ndef live(pid):\n    try:\n        os.kill(pid, 0)\n    except ProcessLookupError:\n        return False\n    # Zombie processes have already completed.\n    stat = Path(f"/proc/{pid}/stat")\n    return not (stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z")\n\n\ndef stop():\n    path = ROOT / "started.json"\n    if not path.exists():\n        return\n    pid = json.loads(path.read_text())["pid"]\n    if not live(pid):\n        return\n    cmdline = Path(f"/proc/{pid}/cmdline")\n    if not cmdline.exists() or str(Path(__file__).resolve()).encode() not in cmdline.read_bytes().split(b"\\0"):\n        raise RuntimeError("Cannot verify ownership of the sandbox process")\n    os.kill(pid, signal.SIGTERM)\n    deadline = time.monotonic() + 480\n    while live(pid) and time.monotonic() < deadline:\n        time.sleep(0.5)\n    if live(pid):\n        raise RuntimeError("Sandbox process has not settled after cancellation")\n\n\ndef main():\n    action = sys.argv[1]\n    if action == "logs":\n        names = (\n            "outcome.json",\n            "prepared.json",\n            "worker.log",\n            "bootstrap.log",\n            "runtime-setup.log",\n            "source-verification.log",\n            "generation.log",\n            "agent/adapter-events.jsonl",\n            "agent/run-status.json",\n            "agent/trajectory.json",\n            "agent/compass-artifacts.tar.gz",\n            "submission.json",\n            "submission.patch",\n            "source-identity.json",\n        )\n        print(json.dumps([name for name in names if (ROOT / name).is_file()]))\n    elif action == "setup":\n        asyncio.run(bounded_setup())\n    elif action == "start":\n        # An exclusive marker prevents duplicate generation after uncertain exec results.\n        with (ROOT / "launch-once").open("x"):\n            pass\n        with (ROOT / "worker.log").open("w") as log:\n            process = subprocess.Popen(\n                [sys.executable, str(Path(__file__).resolve()), "run"],\n                stdin=subprocess.DEVNULL,\n                stdout=log,\n                stderr=log,\n                start_new_session=True,\n            )\n        save("started.json", {"pid": process.pid})\n    elif action == "run":\n        asyncio.run(run())\n    elif action == "wait":\n        if (ROOT / "outcome.json").exists():\n            print("DONE")\n        else:\n            started = json.loads((ROOT / "started.json").read_text())\n            if not live(started["pid"]):\n                raise RuntimeError("Sandbox solver exited without an outcome; see worker.log")\n            print("RUNNING")\n    elif action == "stop":\n        stop()\n    else:\n        raise ValueError("Unknown sandbox action")\n\n\nif __name__ == "__main__":\n    try:\n        main()\n    except BaseException as error:\n        # Never echo API error bodies, credential values, or request headers.\n        print("Sandbox agent action failed (" + type(error).__name__ + ")", file=sys.stderr)\n        raise SystemExit(1) from None\n'
