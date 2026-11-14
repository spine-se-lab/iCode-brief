"""Synthetic contract checks and a complete upload-to-patch smoke test."""

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "compass/src"))
sys.path.insert(0, str(ROOT))
os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(ROOT / ".venv/mini-config")
import build_agent
import runtime_bridge

base = ModuleType("harbor.agents.base")


class BaseAgent:
    def __init__(self, *, logs_dir, model_name=None, extra_env=None):
        self.logs_dir, self.model_name, self.extra_env = logs_dir, model_name, extra_env
        logs_dir.mkdir(parents=True, exist_ok=True)


base.BaseAgent = BaseAgent
sys.modules.setdefault("harbor.agents.base", base)


def load_upload(path):
    spec = importlib.util.spec_from_file_location("upload", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/v1/chat/completions"
            assert self.headers["Authorization"] == "Bearer synthetic-test-key"
            assert body["model"] == "platform-deepseek-flash-alias"
            assert "provider" not in body
            if body.get("tools"):
                self.server.main.append(body)
                step = len(self.server.main)
                assert body["reasoning_effort"] == "max"
                assert body["thinking"] == {"type": "enabled"}
                assert body["max_tokens"] == 64000
                commands = [
                    "cat fixture.py",
                    "compass start 'Read fixture.py; parse returns None. Empty input must return a list.'",
                    "printf 'def parse(data):\\n    return []\\n' > fixture.py",
                    "python3 -c 'from fixture import parse; assert parse(\"\") == []'",
                    "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
                    "git diff --stat",
                    "git add fixture.py && git -c user.name=Test -c user.email=test@localhost commit -qm candidate",
                    "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
                ]
                assert step <= len(commands), f"Unexpected extra main request {step}"
                if step >= 3:
                    assert any("R1" in str(m.get("content")) for m in body["messages"])
                    assert any(m.get("reasoning_content") for m in body["messages"] if m["role"] == "assistant")
                message = {
                    "role": "assistant",
                    "content": "Inspect, implement and verify the requested empty-input result.",
                    "reasoning_content": "Synthetic reasoning for this offline test.",
                    "tool_calls": [
                        {
                            "id": f"call-{step}",
                            "type": "function",
                            "function": {"name": "bash", "arguments": json.dumps({"command": commands[step - 1]})},
                        }
                    ],
                }
                finish = "tool_calls"
            else:
                if self.server.side_failures:
                    self.server.side_failures -= 1
                    encoded = json.dumps(
                        {"error": {"message": "synthetic transient failure", "type": "server_error"}}
                    ).encode()
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                    return
                self.server.side.append(body)
                assert body["thinking"] == {"type": "disabled"}
                assert body["temperature"] == 0
                assert body["response_format"] == {"type": "json_object"}
                system = body["messages"][0]["content"]
                if "IssueRequirementExtraction" in system or '"signature_changes"' in system:
                    content = {
                        "goal": "Return an empty list for empty input.",
                        "requirements": [
                            {"id": "R1", "text": "Return an empty list for empty input.", "reference_derived": False}
                        ],
                        "constraints": [],
                        "signature_changes": [],
                    }
                else:
                    content = {
                        name: []
                        for name in ("new_claims", "augment", "merge", "conflicts", "contradiction_scan", "deactivate")
                    }
                message, finish = {"role": "assistant", "content": json.dumps(content)}, "stop"
            data = {
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "total_tokens": 110,
                    "prompt_tokens_details": {"cached_tokens": 50},
                },
            }
            encoded = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except BaseException as error:
            self.server.errors.append(repr(error))
            self.send_error(500, "Synthetic assertion failed")


class Sandbox:
    def __init__(self, repo, url):
        self.repo, self.url = repo, url

    async def upload_file(self, *, source_path, target_path):
        shutil.copyfile(source_path, target_path)

    async def download_file(self, *, source_path, target_path):
        shutil.copyfile(source_path, target_path)

    async def exec(self, command, *, timeout_sec=30):
        env = os.environ | {
            "OPENAI_API_KEY": "synthetic-test-key",
            "OPENAI_BASE_URL": self.url,
            "MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT": "1",
            "NO_PROXY": "*",
        }
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=self.repo,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_sec)
        return SimpleNamespace(
            return_code=process.returncode, stdout=stdout.decode().replace("\n", ""), stderr=stderr.decode()
        )


class RuntimeChecks(unittest.TestCase):
    def test_frozen_identity(self):
        manifest = runtime_bridge.verify_source(ROOT.parent / "compass")
        self.assertEqual(len(manifest["files_sha256"]), 72)

    def test_usage_unknown_is_preserved(self):
        source = build_agent.build()
        self.assertLess(len(source.encode()), 1_000_000)
        agent = load_upload(ROOT / "r5_mini_evalhub.py")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events"
            path.write_text(
                json.dumps({"event": "main_response", "usage": {"prompt_tokens": 50, "completion_tokens": 10}})
            )
            counts, responses, _ = agent.observed_usage(path)
            self.assertEqual(counts, {"n_input_tokens": 50, "n_output_tokens": 10, "n_cache_tokens": None})
            self.assertEqual(responses["main_response"], 1)

    def test_main_response_without_choices_still_records_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = runtime_bridge.Audit(Path(directory))
            model = runtime_bridge.GatewayModel(
                base_url="https://gateway.invalid/v1",
                key="synthetic-key",
                audit=audit,
                model_name="platform-alias",
                cost_tracking="ignore_errors",
                model_kwargs={"thinking": {"type": "enabled"}, "reasoning_effort": "max", "max_tokens": 64000},
            )
            response = SimpleNamespace(
                status_code=200, json=lambda: {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10}}
            )
            with (
                patch.object(runtime_bridge.requests, "post", return_value=response),
                self.assertRaisesRegex(RuntimeError, "no choices"),
            ):
                model._query([])
            events = [json.loads(line) for line in audit.path.read_text().splitlines()]
            self.assertEqual(events[-1]["event"], "main_response")
            self.assertEqual(events[-1]["usage"]["prompt_tokens"], 100)
            self.assertNotIn("synthetic-key", audit.path.read_text())

    def test_source_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "compass"
            shutil.copytree(ROOT.parent / "compass", source)
            (source / "src/compass_brief/mini/exploration.py").write_text("# altered")
            with self.assertRaisesRegex(RuntimeError, "source mismatch"):
                runtime_bridge.verify_source(source)

    def test_full_sandbox_upload_to_patch(self):
        with tempfile.TemporaryDirectory(prefix="r5-mini-test-") as directory:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
            server.main, server.side, server.errors = [], [], []
            server.side_failures = 1
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                asyncio.run(self._smoke(Path(directory), server))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            self.assertFalse(server.errors, server.errors)

    async def _smoke(self, root, server):
        source = root / "source"
        source.mkdir()
        shutil.copytree(
            ROOT.parent / "compass", source / "integrations/compass", ignore=shutil.ignore_patterns("__pycache__")
        )
        shutil.copytree(
            ROOT, source / "integrations/mini_evalhub", ignore=shutil.ignore_patterns(".venv", "__pycache__")
        )
        git(source, "init", "-qb", "codex/r5-mini-original-evalhub")
        git(source, "add", "integrations")
        git(source, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "test source")
        upload = root / "upload.py"
        upload.write_text(
            build_agent.build().replace("https://github.com/spine-se-lab/iCode-brief.git", source.as_uri())
        )
        agent = load_upload(upload)
        repo = root / "repo"
        repo.mkdir()
        (repo / "fixture.py").write_text("def parse(data):\n    return None\n")
        git(repo, "init", "-q")
        git(repo, "add", "fixture.py")
        git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "base")
        initial = git(repo, "rev-parse", "HEAD")
        solver = agent.R5MiniAgent(
            logs_dir=root / "host",
            model_name="platform-deepseek-flash-alias",
            runtime_python=sys.executable,
            repo_commit=git(source, "rev-parse", "HEAD").decode().strip(),
        )
        solver.sandbox_root = str(root / "sandbox")
        sandbox = Sandbox(repo, f"http://127.0.0.1:{server.server_port}/v1")
        wrong = agent.R5MiniAgent(
            logs_dir=root / "wrong-host",
            model_name="platform-deepseek-flash-alias",
            runtime_python=sys.executable,
            repo_commit="0" * 40,
        )
        wrong.sandbox_root = str(root / "wrong-sandbox")
        with self.assertRaises(RuntimeError):
            await wrong.setup(sandbox)
        assert not server.main and not server.side
        await solver.setup(sandbox)
        bootstrap = Path(solver.sandbox_root) / "sandbox_agent.py"
        bootstrap.write_text(
            bootstrap.read_text().replace(
                'Path("/logs/artifacts")', repr(root / "artifacts").replace("PosixPath", "Path")
            )
        )
        context = SimpleNamespace(metadata=None)
        try:
            await solver.run("Make parse(data) in fixture.py return an empty list for empty input.", sandbox, context)
        except BaseException:
            for filename in ("generation.log", "worker.log", "agent/run-status.json"):
                path = Path(solver.sandbox_root) / filename
                if path.exists():
                    print(path.read_text()[-6000:])
            print(server.errors)
            raise
        assert len(server.main) == 8
        assert len(server.side) >= 2
        assert server.side_failures == 0
        events = [json.loads(line) for line in (root / "host/agent/adapter-events.jsonl").read_text().splitlines()]
        assert not any(event["event"] == "maintenance_error" for event in events)
        assert sum(event["event"] == "maintenance_request" for event in events) == len(server.side)
        assert context.n_input_tokens == (len(server.main) + len(server.side)) * 100
        assert context.n_output_tokens == (len(server.main) + len(server.side)) * 10
        assert context.n_cache_tokens == (len(server.main) + len(server.side)) * 50
        status = context.metadata["r5_mini"]["result"]
        assert status["exit_status"] == "Submitted"
        assert status["compass"]["contract"]["requirements"] == 1
        assert status["brief_followed_by_main_response"] > 0
        assert status["compass"]["reviews_before_submit"] == 0
        assert git(repo, "rev-parse", "HEAD") == initial
        exported = (root / "host/submission.patch").read_bytes()
        assert b"+    return []" in exported
        assert exported == git(repo, "diff", "--binary")
        verifier = root / "verifier"
        subprocess.run(["git", "clone", "-q", str(repo), str(verifier)], check=True)
        subprocess.run(["git", "apply", "--index", "-"], cwd=verifier, input=exported, check=True)
        assert git(verifier, "write-tree").decode().strip() == context.metadata["submission"]["candidate_tree"]
        for name in (
            "source-identity.json",
            "agent/adapter-events.jsonl",
            "agent/trajectory.json",
            "agent/run-status.json",
            "outcome.json",
        ):
            assert "synthetic-test-key" not in (root / "host" / name).read_text()
        assert (root / "host/agent/compass-artifacts.tar.gz").stat().st_size > 0
        assert (await solver._sandbox_command(sandbox, "wait", 30)) == "DONE"
        self.assertNotEqual((await sandbox.exec(f"python3 {bootstrap} start")).return_code, 0)


if __name__ == "__main__":
    unittest.main()
