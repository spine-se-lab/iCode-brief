"""Run the existing R5 wrapper inside the assigned EvalHub sandbox."""

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parent


def save(name, value):
    path = ROOT / name
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def load_agent():
    # The sandbox needs the solver, while Harbor itself remains on the runner.
    base = ModuleType("harbor.agents.base")

    class BaseAgent:
        def __init__(self, logs_dir, model_name=None, extra_env=None, **kwargs):
            self.logs_dir = logs_dir
            self.model_name = model_name
            self.extra_env = extra_env or {}

    base.BaseAgent = BaseAgent
    sys.modules["harbor.agents.base"] = base
    spec = importlib.util.spec_from_file_location("r5_upload", ROOT / "upload.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Environment:
    async def exec(self, command, *, cwd=None, timeout_sec=30):
        # Repository tools do not inherit gateway credentials or installation settings.
        env = {
            name: os.environ[name]
            for name in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM")
            if name in os.environ
        }
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_sec)
            return SimpleNamespace(
                return_code=process.returncode,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
            )
        except TimeoutError:
            return SimpleNamespace(return_code=124, stdout="", stderr="Command timed out")
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()


def solver(module):
    options = json.loads((ROOT / "options.json").read_text())
    return module.R5ICodeAgent(
        logs_dir=ROOT,
        model_name=options["model_name"],
        api_model=options["api_model"],
        workdir=options["workdir"],
        runtime_python=options["runtime_python"],
        setup_timeout=options["setup_timeout"],
        execution_mode="runner",
        repo_ref=options["repo_ref"],
        repo_commit=options["repo_commit"],
        api_key_env="OPENAI_API_KEY",
        base_url=os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE"),
    )


def require_gateway():
    if not os.environ.get("OPENAI_API_KEY") or not (
        os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE")
    ):
        raise RuntimeError("EvalHub must inject OPENAI_API_KEY and OPENAI_BASE_URL into the task sandbox")


async def setup():
    require_gateway()
    module = load_agent()
    agent = solver(module)
    if not agent.runtime_python and shutil.which("uv") is None:
        # Install only into this trial's directory. Use the configured pip index/cache.
        bootstrap = ROOT / "bootstrap"
        with (ROOT / "bootstrap.log").open("w") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pip",
                "install",
                "--target",
                str(bootstrap),
                "uv",
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            try:
                code = await asyncio.wait_for(process.wait(), agent.setup_timeout)
            finally:
                await module.stop_process(process)
            if code:
                raise RuntimeError("Provide uv or Python pip in the sandbox; see bootstrap.log")
        os.environ["PATH"] = str(bootstrap / "bin") + os.pathsep + os.environ.get("PATH", "")
    await agent.setup(Environment())
    save("prepared.json", {"python": agent.python, "workdir": agent.workdir})


async def bounded_setup():
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    options = json.loads((ROOT / "options.json").read_text())
    await asyncio.wait_for(setup(), options["setup_timeout"])


def finalize_workspace(workdir, initial_head, artifacts=Path("/logs/artifacts")):
    """Expose the complete candidate to EvalHub's working-tree patch collector."""

    def git(*args, data=None):
        return subprocess.run(
            ["git", *args], cwd=workdir, input=data, capture_output=True, check=True, timeout=120
        ).stdout

    final_head = git("rev-parse", "--verify", "HEAD").decode().strip()
    # Retain commits and the entire final tracked/non-ignored candidate before
    # moving HEAD/index. Never reset --hard or change candidate file contents.
    git("update-ref", "refs/r5-evalhub/model-head", final_head)
    git("add", "-A")
    tree = git("write-tree").decode().strip()
    git("update-ref", "refs/r5-evalhub/submission-tree", tree)
    flags = ("--binary", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames")
    candidate = git("diff", *flags, initial_head, tree, "--")
    (ROOT / "submission.patch").write_bytes(candidate)
    added = git("diff", "--name-only", "--diff-filter=A", "--no-renames", "-z", initial_head, tree, "--")
    git("reset", "--mixed", initial_head)
    if added:
        # Intent-to-add makes even new files visible to plain git diff. Force
        # includes files the model committed despite a matching ignore rule.
        git("add", "-N", "-f", "--pathspec-from-file=-", "--pathspec-file-nul", data=added)
    exported = git("diff", *flags, "HEAD", "--")
    if exported != candidate or git("diff", *flags, "--") != candidate:
        raise RuntimeError("Workspace patch differs from the saved candidate")
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "patch.diff").write_bytes(candidate)
    receipt = {
        "initial_head": initial_head,
        "model_head": final_head,
        "candidate_tree": tree,
        "patch_bytes": len(candidate),
        "patch_sha256": hashlib.sha256(candidate).hexdigest(),
        "working_tree_diff_verified": True,
        "commit_backup_ref": "refs/r5-evalhub/model-head",
        "tree_backup_ref": "refs/r5-evalhub/submission-tree",
    }
    save("submission.json", receipt)
    return receipt


async def run():
    require_gateway()
    # Match the reference agent's bypass_proxy behavior for the internal gateway.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(name, None)
    module = load_agent()
    agent = solver(module)
    prepared = json.loads((ROOT / "prepared.json").read_text())
    agent.python = prepared["python"]
    agent.workdir = prepared["workdir"]
    context = SimpleNamespace(metadata=None)
    environment = Environment()
    head = await environment.exec("git rev-parse --verify HEAD", cwd=agent.workdir)
    initial_head = head.stdout.strip() if head.return_code == 0 else None
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    error = None
    try:
        await agent.run((ROOT / "task.txt").read_text(), environment, context)
    except BaseException as exception:
        error = type(exception).__name__
    finally:
        if initial_head:
            try:
                receipt = await asyncio.to_thread(finalize_workspace, agent.workdir, initial_head)
                context.metadata = {
                    **(context.metadata or {}),
                    "workspace_finalize_exit_code": 0,
                    "submission": receipt,
                }
            except Exception as exception:
                error = error or "WorkspaceSubmissionError"
                context.metadata = {**(context.metadata or {}), "workspace_finalize_error": type(exception).__name__}
        else:
            error = error or "MissingInitialGitHead"
        save("outcome.json", {"error": error, "context": vars(context)})


def live(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Zombie processes have already completed.
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z")


def stop():
    path = ROOT / "started.json"
    if not path.exists():
        return
    pid = json.loads(path.read_text())["pid"]
    if not live(pid):
        return
    cmdline = Path(f"/proc/{pid}/cmdline")
    if not cmdline.exists() or str(Path(__file__).resolve()).encode() not in cmdline.read_bytes().split(b"\0"):
        raise RuntimeError("Cannot verify ownership of the sandbox process")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 480
    while live(pid) and time.monotonic() < deadline:
        time.sleep(0.5)
    if live(pid):
        raise RuntimeError("Sandbox process has not settled after cancellation")


def main():
    action = sys.argv[1]
    if action == "logs":
        names = (
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
        )
        print(json.dumps([name for name in names if (ROOT / name).is_file()]))
    elif action == "setup":
        asyncio.run(bounded_setup())
    elif action == "start":
        # An exclusive marker prevents duplicate generation after uncertain exec results.
        with (ROOT / "launch-once").open("x"):
            pass
        with (ROOT / "worker.log").open("w") as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "run"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        save("started.json", {"pid": process.pid})
    elif action == "run":
        asyncio.run(run())
    elif action == "wait":
        if (ROOT / "outcome.json").exists():
            print("DONE")
        else:
            started = json.loads((ROOT / "started.json").read_text())
            if not live(started["pid"]):
                raise RuntimeError("Sandbox solver exited without an outcome; see worker.log")
            print("RUNNING")
    elif action == "stop":
        stop()
    else:
        raise ValueError("Unknown sandbox action")


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:
        # Never echo API error bodies, credential values, or request headers.
        print("Sandbox agent action failed (" + type(error).__name__ + ")", file=sys.stderr)
        raise SystemExit(1) from None
