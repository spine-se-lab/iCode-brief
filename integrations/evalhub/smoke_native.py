"""Real iCode CLI + frozen R5 + Harbor transport, with fully mocked model HTTP."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from offline_source import prepare_source
from test_agent import compact_agent as agent

ROOT = Path(__file__).resolve().parent


class SyntheticEnvironment:
    def __init__(self, root):
        self.root = root
        self.environment_dir = root / "task/environment"
        self.calls = []

    async def exec(self, command, *, cwd=None, timeout_sec=30):
        assert cwd is None or Path(cwd) == self.root
        self.calls.append(command)
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-lc",
            command,
            cwd=self.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_sec)
        return SimpleNamespace(stdout=stdout.decode(), stderr=stderr.decode(), return_code=process.returncode)


async def main():
    mode = os.environ.get("R5_TEST_HANDOFF_MODE", "normal")
    with tempfile.TemporaryDirectory(prefix="r5-harbor-native-") as directory:
        root = Path(directory).resolve()
        agent.REPO_URL = prepare_source(root / "source")
        sandbox = root / "sandbox"
        sandbox.mkdir()
        (sandbox / "fixture.py").write_text("def parse(data):\n    return None\n")
        environment = SyntheticEnvironment(sandbox)
        solver = agent.R5ICodeAgent(
            logs_dir=root / "logs",
            model_name="deepseek-flash",
            execution_mode="runner",
            runtime_python=None if os.environ.get("R5_TEST_FRESH_RUNTIME") else sys.executable,
            extra_env={"DEEPSEEK_API_KEY": "offline-placeholder"},
        )
        try:
            await solver.setup(environment)
        except BaseException:
            for name in ("runtime-setup.log", "source-verification.log"):
                verification = root / "logs" / name
                if verification.exists():
                    print(verification.read_text()[-3000:])
            raise
        runtime = solver.runtime / "integrations/evalhub"
        original = runtime / "runtime_bridge.py"
        original.rename(runtime / "runtime_bridge_real.py")
        shutil.copyfile(ROOT / "offline_http_fixture.py", runtime / "offline_http_fixture.py")
        original.write_text(
            "import sys,json\nfrom pathlib import Path\nfrom unittest.mock import patch\n"
            "import httpx\nimport offline_http_fixture as f\nimport runtime_bridge_real as b\n"
            f"f.mode={mode!r}\n"
            "with patch.object(httpx.AsyncClient,'send',f.send):\n rc=b.main(sys.argv[1])\n"
            "Path(__file__).with_name('smoke-counts.json').write_text(json.dumps("
            "{'main':len(f.main_bodies),'maintenance':len(f.side_bodies)}))\nraise SystemExit(rc)\n"
        )
        context = SimpleNamespace(metadata=None)
        try:
            await solver.run(
                "In fixture.py, make parse(data) return an empty list for empty input. Use the Compass workflow.",
                environment,
                context,
            )
        except RuntimeError:
            if mode == "timeout":
                events = [
                    json.loads(line) for line in (root / "logs/agent/adapter-events.jsonl").read_text().splitlines()
                ]
                assert sum(e["event"] == "main_response" for e in events) == 2
                assert sum(e["event"] == "maintenance_request" for e in events) == 2
                assert sum(e["event"] == "maintenance_cancelled" for e in events) == 2
                assert not any(e["event"] == "brief_delivered" for e in events)
                assert context.metadata["r5_icode"]["failure"]["code"] == "r5_contract_extraction_timeout"
                assert (sandbox / "fixture.py").read_text() == "def parse(data):\n    return None\n"
                result = {
                    "passed": True,
                    "mode": mode,
                    "main_responses": 2,
                    "cancelled_maintenance_attempts": 2,
                    "external_model_calls": 0,
                }
                (ROOT / "native-smoke-timeout-v19.json").write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps(result))
                return
            # All credentials and model responses in this test are synthetic.
            print((root / "logs/generation.log").read_text()[-7000:])
            raise
        assert mode != "timeout", "Failed handoff was accepted as success"
        assert not (sandbox / "unauthorized.txt").exists()
        if "return []" not in (sandbox / "fixture.py").read_text():
            print((root / "logs/generation.log").read_text()[-7000:])
            raise AssertionError("Synthetic sandbox file was not edited")
        assert not (root / "logs/runner-workspace/fixture.py").exists()
        counts = json.loads((runtime / "smoke-counts.json").read_text())
        events = [json.loads(line) for line in (root / "logs/agent/adapter-events.jsonl").read_text().splitlines()]
        assert counts["main"] == 5
        assert sum(e["event"] == "main_request" for e in events) == 5
        assert sum(e["event"] == "submission_check" for e in events) == 1
        assert any(e["event"] == "brief_delivered" for e in events)
        if mode == "automatic":
            assert sum(e["event"] == "write_held" for e in events) == 1
            assert sum(e["event"] == "handoff_ready" for e in events) == 1
        if mode == "recover":
            assert sum(e["event"] == "maintenance_cancelled" for e in events) == 1
            assert not any(e["event"] == "handoff_failed" for e in events)
        result = {
            "passed": True,
            "real_icode_cli": True,
            "compass_v19": True,
            "dependencies_installed_during_setup": solver.runtime_python is None,
            "synthetic_main_responses": counts["main"],
            "synthetic_maintenance_attempts": counts["maintenance"],
            "synthetic_maintenance_responses": sum(e["event"] == "maintenance_response" for e in events),
            "sandbox_file_changed": True,
            "runner_workspace_file_unchanged": True,
            "brief_delivered": True,
            "submission_check": True,
            "external_model_calls": 0,
            "context": vars(context),
        }
        print(json.dumps(result))
        (ROOT / ("native-smoke-" + mode + "-v19.json")).write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
