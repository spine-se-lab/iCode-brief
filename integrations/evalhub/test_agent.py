"""Offline lifecycle/transport checks. No external model calls or task verifier."""

import ast
import asyncio
import hashlib
import importlib.util
import json
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent


class StubBaseAgent:
    def __init__(self, logs_dir, model_name=None, extra_env=None, **kwargs):
        self.logs_dir = logs_dir
        self.model_name = model_name
        self.extra_env = extra_env or {}


base = types.ModuleType("harbor.agents.base")
base.BaseAgent = StubBaseAgent
with patch.dict(sys.modules, {"harbor.agents.base": base}):
    spec = importlib.util.spec_from_file_location("upload_agent", ROOT / "r5_icode_evalhub.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    spec = importlib.util.spec_from_file_location("compact_agent", ROOT / "r5_icode_evalhub.py")
    compact_agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compact_agent)


CHILD = """
import asyncio,json,os,sys
from pathlib import Path
s=json.loads(Path(sys.argv[1]).read_text())
assert os.environ['R5_EVALHUB_API_KEY']=='synthetic-offline-key'
async def work():
 reader,writer=await asyncio.open_unix_connection(s['socket'])
 writer.write((json.dumps({'command':'write-answer','cwd':s['workdir'],'timeout_sec':30})+'\\n').encode())
 await writer.drain()
 result=json.loads(await reader.readline())
 writer.close()
 await writer.wait_closed()
 assert result['return_code']==0 and result['output']=='done'
asyncio.run(work())
d=Path(s['agent_dir']);d.mkdir()
events=[{'event':'main_response','usage':{'input_token_count':7,'output_token_count':3,'cache_read_input_token_count':4}},
        {'event':'maintenance_response','usage':{'prompt_tokens':5,'completion_tokens':2,'prompt_cache_hit_tokens':1}}]
(d/'adapter-events.jsonl').write_text(''.join(json.dumps(e)+'\\n' for e in events))
"""


class Environment:
    def __init__(self, root):
        self.root = root
        self.environment_dir = root / "task/environment"
        self.calls = []

    async def exec(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command == "pwd -P":
            return SimpleNamespace(return_code=0, stdout="/app\n", stderr="")
        if command == "write-answer":
            (self.root / "answer.txt").write_text("synthetic answer")
            return SimpleNamespace(return_code=0, stdout="done", stderr="")
        raise AssertionError(command)


class Checks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = Environment(self.root)
        self.context = SimpleNamespace(metadata=None)
        self.solver = agent.R5ICodeAgent(
            logs_dir=self.root / "logs",
            model_name="deepseek/deepseek-flash",
            execution_mode="runner",
            extra_env={"DEEPSEEK_API_KEY": "synthetic-offline-key"},
        )
        (self.solver.runtime / "integrations/evalhub").mkdir(parents=True)
        (self.root / "logs/source-identity.json").write_text("{}")
        self.solver.workdir = "/app"
        self.solver.python = sys.executable

    def tearDown(self):
        self.temporary.cleanup()

    def child(self, source):
        (self.solver.runtime / "integrations/evalhub/runtime_bridge.py").write_text(source)

    async def test_lifecycle_transport_usage_and_no_sandbox_secret(self):
        self.child(CHILD)
        await self.solver.run("Write the answer", self.environment, self.context)
        self.assertEqual((self.root / "answer.txt").read_text(), "synthetic answer")
        self.assertEqual(self.context.n_input_tokens, 12)
        self.assertEqual(self.context.n_output_tokens, 5)
        self.assertEqual(self.context.n_cache_tokens, 5)
        self.assertNotIn("env", self.environment.calls[0][1])
        self.assertEqual(self.context.metadata["r5_icode"]["generation_exit_code"], 0)
        self.assertNotIn("synthetic-offline-key", (self.root / "logs/runtime-settings.json").read_text())
        with self.assertRaisesRegex(RuntimeError, "fresh trials"):
            await self.solver.run("again", self.environment, self.context)

    async def test_unexpected_failure_propagates(self):
        self.child("raise SystemExit(7)")
        with self.assertRaisesRegex(RuntimeError, "code 7"):
            await self.solver.run("test", self.environment, self.context)
        self.assertIsNone(self.context.n_input_tokens)
        self.assertEqual(self.context.metadata["r5_icode"]["generation_exit_code"], 7)

    async def test_custom_platform_model_is_forwarded_unchanged(self):
        self.solver.model_name = "deepseek-v4.1-flash-1017-yisen-481c7b0059dd"
        self.child(CHILD)
        await self.solver.run("test", self.environment, self.context)
        settings = json.loads((self.root / "logs/runtime-settings.json").read_text())
        self.assertEqual(settings["model"], self.solver.model_name)
        self.assertEqual(self.context.metadata["r5_icode"]["platform_model_name"], self.solver.model_name)

    async def test_api_model_overrides_platform_alias_without_stripping(self):
        self.solver.api_model = "company/deepseek-flash-custom"
        self.child(CHILD)
        await self.solver.run("test", self.environment, self.context)
        settings = json.loads((self.root / "logs/runtime-settings.json").read_text())
        self.assertEqual(settings["model"], self.solver.api_model)
        self.assertEqual(self.context.metadata["r5_icode"]["api_model"], self.solver.api_model)

    async def test_empty_model_rejected_before_subprocess(self):
        self.solver.api_model = " "
        with self.assertRaisesRegex(ValueError, "provider model ID"):
            await self.solver.run("test", self.environment, self.context)
        self.assertFalse((self.root / "logs/runtime-settings.json").exists())

    async def test_budget_exit_returns_candidate_to_harbor(self):
        self.child(
            "import json,sys; from pathlib import Path; s=json.loads(Path(sys.argv[1]).read_text()); "
            "d=Path(s['agent_dir']); d.mkdir(); "
            "(d/'adapter-events.jsonl').write_text(json.dumps({'event':'budget_exhausted'})+'\\n'); "
            "raise SystemExit(1)"
        )
        await self.solver.run("test", self.environment, self.context)
        self.assertTrue(self.context.metadata["r5_icode"]["budget_exhausted"])
        self.assertEqual(self.context.metadata["r5_icode"]["generation_exit_code"], 1)

    async def test_failed_handoff_cannot_be_accepted_as_budget_exit(self):
        self.child(
            "import json,sys; from pathlib import Path; s=json.loads(Path(sys.argv[1]).read_text()); "
            "d=Path(s['agent_dir']); d.mkdir(); "
            "(d/'adapter-events.jsonl').write_text(json.dumps({'event':'budget_exhausted'})+'\\n'); "
            "(d/'failure.json').write_text(json.dumps({'code':'r5_contract_extraction_timeout'})); "
            "raise SystemExit(1)"
        )
        with self.assertRaisesRegex(RuntimeError, "r5_contract_extraction_timeout"):
            await self.solver.run("test", self.environment, self.context)
        self.assertEqual(
            self.context.metadata["r5_icode"]["failure"],
            {"code": "r5_contract_extraction_timeout", "stage": "r5_handoff"},
        )

    async def test_collection_downloads_only_existing_allowlisted_logs(self):
        Path(self.solver.logs_dir).mkdir(exist_ok=True)
        environment = SimpleNamespace(download_file=AsyncMock())
        with patch.object(
            self.solver, "_sandbox_command", new=AsyncMock(return_value=json.dumps(["prepared.json", "../secret.txt"]))
        ):
            await self.solver._download_sandbox_logs(environment)
        self.assertEqual(environment.download_file.call_count, 1)
        self.assertTrue(environment.download_file.call_args.kwargs["source_path"].endswith("/prepared.json"))

    async def test_platform_exception_contains_safe_handoff_code(self):
        solver = compact_agent.R5ICodeAgent(logs_dir=self.root / "failure-host", model_name="custom-model")
        Path(solver.logs_dir).mkdir()
        solver.python = "sandbox-prepared"

        async def collect(environment):
            (Path(solver.logs_dir) / "outcome.json").write_text(
                json.dumps(
                    {
                        "error": "RuntimeError",
                        "context": {
                            "metadata": {
                                "r5_icode": {
                                    "failure": {"code": "r5_contract_extraction_timeout", "stage": "r5_handoff"},
                                    "generation_exit_code": 1,
                                }
                            }
                        },
                    }
                )
            )

        environment = SimpleNamespace(upload_file=AsyncMock())
        with (
            patch.object(solver, "_sandbox_command", new=AsyncMock(return_value="DONE")),
            patch.object(solver, "_download_sandbox_logs", side_effect=collect),
            self.assertRaisesRegex(RuntimeError, "r5_contract_extraction_timeout; stage=r5_handoff"),
        ):
            await solver.run("task", environment, SimpleNamespace(metadata=None))

    def test_exploration_rejects_interpreters_hooks_and_shell_side_effects(self):
        tree = ast.parse((ROOT / "r5_native.py").read_text())
        function = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "exploration_read_only"
        )
        namespace = {"shlex": shlex}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "<read-gate>", "exec"), namespace)
        check = namespace["exploration_read_only"]
        for command in [
            "cat fixture.py",
            "cd /app && rg -n parse src",
            "rg --files | head -20",
            "git rev-parse HEAD",
            "ls -la",
            "grep -n pattern file.py",
        ]:
            with self.subTest(command=command):
                self.assertTrue(check(command))
        for command in [
            "python -c \"open('a','w').write('x')\"",
            'node -e "write()"',
            "perl -pi -e s/a/b/ f",
            "sed -i s/a/b/ f",
            "git commit -am x",
            'git -c alias.x="!touch x" x',
            "rg --pre=sh anything",
            "rg --hostname-bin=script anything",
            "find . -exec touch x \\;",
            "cat a > b",
            "cat $(touch a)",
            "cat `touch a`",
            "cat a; touch b",
            "cat a\ntouch b",
            "ls & touch b",
            "ls || touch b",
            "ls |& python -c pass",
            "git status --short",
            "exec sh",
            "",
        ]:
            with self.subTest(command=command):
                self.assertFalse(check(command))

    async def test_prompt_template_is_rendered_once(self):
        template = self.root / "prompt.txt"
        template.write_text("{{instruction}}")
        self.solver.prompt_template_path = template
        templating = types.ModuleType("harbor.utils.templating")
        calls = []

        def render(path, instruction, task_dir):
            calls.append((path, task_dir))
            return "rendered " + instruction

        templating.render_prompt_template = render
        self.child(CHILD)
        with patch.dict(sys.modules, {"harbor.utils.templating": templating}):
            await self.solver.run("task", self.environment, self.context)
        self.assertEqual(len(calls), 1)
        self.assertEqual((self.root / "logs/instruction.txt").read_text(), "rendered task")

    async def test_cancellation_waits_for_child(self):
        self.child("import time; time.sleep(60)")
        task = asyncio.create_task(self.solver.run("test", self.environment, self.context))
        await asyncio.sleep(0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNotNone(self.context.metadata["r5_icode"]["generation_exit_code"])

    async def test_cancellation_waits_for_sandbox_command(self):
        started = asyncio.Event()
        settled = asyncio.Event()
        original = self.environment.exec

        async def delayed(command, **kwargs):
            started.set()
            await asyncio.sleep(0.1)
            result = await original(command, **kwargs)
            settled.set()
            return result

        self.environment.exec = delayed
        self.child(CHILD)
        task = asyncio.create_task(self.solver.run("test", self.environment, self.context))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(settled.is_set())

    async def test_sandbox_cancellation_settles_launch_before_stop(self):
        solver = compact_agent.R5ICodeAgent(logs_dir=self.root / "sandbox-host-logs", model_name="custom-model")
        Path(solver.logs_dir).mkdir()
        solver.python = "sandbox-prepared"
        launched = asyncio.Event()
        actions = []

        async def command(environment, action, timeout):
            actions.append(action)
            if action == "start":
                launched.set()
                await asyncio.sleep(0.1)
                actions.append("launch_settled")

        async def collect(environment):
            actions.append("collected")

        async def upload(**kwargs):
            pass

        environment = SimpleNamespace(upload_file=upload)
        with (
            patch.object(solver, "_sandbox_command", side_effect=command),
            patch.object(solver, "_download_sandbox_logs", side_effect=collect),
        ):
            task = asyncio.create_task(solver.run("task", environment, SimpleNamespace(metadata=None)))
            await launched.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(actions, ["start", "launch_settled", "stop", "collected"])

    async def test_transient_poll_failures_do_not_cancel_or_relaunch_worker(self):
        solver = compact_agent.R5ICodeAgent(logs_dir=self.root / "poll-logs", model_name="custom-model")
        Path(solver.logs_dir).mkdir()
        solver.python = "sandbox-prepared"
        actions = []
        states = iter([-1, TimeoutError("synthetic transport failure"), "RUNNING", -1, "DONE"])

        async def execute(command, **kwargs):
            action = command.split()[-1]
            actions.append(action)
            if action == "wait":
                value = next(states)
                if isinstance(value, Exception):
                    raise value
                return SimpleNamespace(
                    return_code=value if isinstance(value, int) else 0, stdout="" if isinstance(value, int) else value
                )
            return SimpleNamespace(return_code=0, stdout="")

        async def collect(environment):
            (Path(solver.logs_dir) / "outcome.json").write_text(
                json.dumps({"error": None, "context": {"metadata": {}}})
            )

        environment = SimpleNamespace(exec=execute, upload_file=AsyncMock())
        with (
            patch.object(solver, "_download_sandbox_logs", side_effect=collect),
            patch.object(compact_agent.asyncio, "sleep", new=AsyncMock()),
        ):
            await solver.run("task", environment, SimpleNamespace(metadata=None))
        self.assertEqual(actions, ["start", "wait", "wait", "wait", "wait", "wait", "stop"])
        self.assertEqual(len((Path(solver.logs_dir) / "poll-errors.jsonl").read_text().splitlines()), 3)

    async def test_persistent_poll_failure_is_bounded_and_not_silently_successful(self):
        solver = compact_agent.R5ICodeAgent(logs_dir=self.root / "failed-poll-logs", model_name="custom-model")
        Path(solver.logs_dir).mkdir()
        solver.python = "sandbox-prepared"
        actions = []

        async def execute(command, **kwargs):
            action = command.split()[-1]
            actions.append(action)
            return SimpleNamespace(return_code=-1 if action == "wait" else 0, stdout="")

        environment = SimpleNamespace(exec=execute, upload_file=AsyncMock())
        with (
            patch.object(solver, "_download_sandbox_logs", new=AsyncMock()),
            patch.object(compact_agent.asyncio, "sleep", new=AsyncMock()),
            self.assertRaisesRegex(RuntimeError, "observation unavailable for 6 consecutive polls"),
        ):
            await solver.run("task", environment, SimpleNamespace(metadata=None))
        self.assertEqual(actions, ["start"] + ["wait"] * 6 + ["stop"])

    async def test_sandbox_shell_clears_gateway_key_and_times_out(self):
        spec = importlib.util.spec_from_file_location("sandbox_helper", ROOT / "sandbox_agent.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with patch.dict(helper.os.environ, {"OPENAI_API_KEY": "synthetic-offline-key"}):
            result = await helper.Environment().exec('test -z "${OPENAI_API_KEY:-}"')
            self.assertEqual(result.return_code, 0)
        result = await helper.Environment().exec("sleep 60", timeout_sec=0.05)
        self.assertEqual(result.return_code, 124)

    def test_missing_gateway_fails_without_reading_runner_credentials(self):
        spec = importlib.util.spec_from_file_location("sandbox_helper", ROOT / "sandbox_agent.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with (
            patch.dict(helper.os.environ, {"DEEPSEEK_API_KEY": "synthetic-offline-key"}, clear=True),
            self.assertRaisesRegex(RuntimeError, "EvalHub must inject"),
        ):
            helper.require_gateway()

    def test_v142_source_and_upload_identity(self):
        root = ROOT.parent / "compass"
        manifest = json.loads((root / "SOURCE-MANIFEST.json").read_text())
        self.assertEqual(manifest["version"], "1.4.2")
        self.assertEqual(manifest["original_archive_sha256"], agent.R5_ARCHIVE)
        self.assertEqual(len(manifest["files_sha256"]), 72)
        for name, expected in manifest["files_sha256"].items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), expected)
        source = (ROOT / "r5_icode_evalhub.py").read_text()
        self.assertLess(len(source.encode()), 1_000_000)
        self.assertNotIn("RUNTIME_B64", source)
        self.assertNotIn("pip install iCode", source)
        self.assertEqual(agent.REPO_REF, "codex/compass-v1.4.2-evalhub")
        self.assertIn(
            "--frozen",
            {
                node.value
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            },
        )
        self.assertIn("brief_budget_chars=6000", (root / "src/compass_brief/session.py").read_text())

    async def test_branch_commit_mismatch_stops_before_runtime_install(self):
        source = self.root / "source"

        def seed():
            subprocess.run(["git", "init", "-q", "-b", "fixture-branch", str(source)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@localhost",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "--allow-empty",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )

        await asyncio.to_thread(seed)
        solver = agent.R5ICodeAgent(
            logs_dir=self.root / "clone-logs",
            execution_mode="runner",
            repo_ref="fixture-branch",
            repo_commit="0" * 40,
            runtime_python=sys.executable,
        )
        with (
            patch.object(agent, "REPO_URL", source.as_uri()),
            self.assertRaisesRegex(RuntimeError, "Branch HEAD does not match repo_commit"),
        ):
            await solver.setup(self.environment)
        self.assertTrue((solver.runtime / ".git").exists())
        self.assertIsNone(solver.python)
        self.assertFalse((self.root / "clone-logs/agent/adapter-events.jsonl").exists())

    def test_reject_invalid_git_selectors(self):
        for value in ("", "--upload-pack=bad"):
            with self.assertRaises(ValueError):
                agent.R5ICodeAgent(logs_dir=self.root, repo_ref=value)
        with self.assertRaises(ValueError):
            agent.R5ICodeAgent(logs_dir=self.root, repo_commit="main")


if __name__ == "__main__":
    unittest.main()
