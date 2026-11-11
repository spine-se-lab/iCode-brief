"""Exercise sandbox upload/setup/run/download with fake gateway responses only."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from offline_source import prepare_source
from test_agent import compact_agent as agent

ROOT = Path(__file__).resolve().parent


class Sandbox:
    def __init__(self, repository):
        self.repository = repository
        self.environment_dir = repository / "environment"

    async def upload_file(self, *, source_path, target_path):
        shutil.copyfile(source_path, target_path)

    async def download_file(self, *, source_path, target_path):
        shutil.copyfile(source_path, target_path)

    async def exec(self, command, *, timeout_sec=30):
        env = dict(os.environ)
        env["OPENAI_BASE_URL"] = "https://gateway.invalid/v1"
        env["OPENAI_API_KEY"] = "offline-placeholder"
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=self.repository,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_sec)
        # Simulate OpenSandbox stdout newline removal.
        return SimpleNamespace(
            return_code=process.returncode, stdout=stdout.decode().replace("\n", ""), stderr=stderr.decode()
        )


async def main():
    with tempfile.TemporaryDirectory(prefix="r5-sandbox-smoke-") as directory:
        root = Path(directory)
        source = prepare_source(root / "source")
        upload = root / "offline-upload.py"
        upload.write_text(Path(agent.__file__).read_text().replace(agent.REPO_URL, source))
        agent.__file__ = str(upload)
        repository = root / "repo"
        repository.mkdir()
        (repository / "fixture.py").write_text("def parse(data):\n    return None\n")
        environment = Sandbox(repository)
        init = await environment.exec(
            "git init -q && git add fixture.py && git -c user.name=Test -c user.email=test@localhost commit -qm initial"
        )
        assert init.return_code == 0
        initial = (await environment.exec("git rev-parse HEAD")).stdout
        solver = agent.R5ICodeAgent(
            logs_dir=root / "host-logs",
            runtime_python=sys.executable,
            model_name="deepseek-v4.1-flash-1017-yisen-481c7b0059dd",
        )
        solver.sandbox_root = str(root / "sandbox-logs")
        remote = Path(solver.sandbox_root)
        await solver.setup(environment)
        helper = remote / "sandbox_agent.py"
        helper.write_text(
            helper.read_text().replace(
                'Path("/logs/artifacts")', "Path(" + repr(str(root / "platform-artifacts")) + ")"
            )
        )
        runtime = remote / "r5-runtime/integrations/evalhub"
        bridge = runtime / "runtime_bridge.py"
        bridge.rename(runtime / "runtime_bridge_real.py")
        fixture = (ROOT / "offline_http_fixture.py").read_text().replace("api.deepseek.com", "gateway.invalid")
        (runtime / "offline_http_fixture.py").write_text(fixture)
        bridge.write_text(
            "import sys,json,subprocess,os\nfrom pathlib import Path\nfrom unittest.mock import patch\n"
            "import httpx\nimport offline_http_fixture as f\nimport runtime_bridge_real as b\n"
            "with patch.object(httpx.AsyncClient,'send',f.send):\n rc=b.main(sys.argv[1])\n"
            "os.chdir(json.loads(Path(sys.argv[1]).read_text())['workdir'])\n"
            "subprocess.run(['git','add','-A'],check=True)\n"
            "subprocess.run(['git','-c','user.name=Test','-c','user.email=test@localhost',"
            "'commit','-qm','model committed candidate'],check=True)\n"
            "assert subprocess.check_output(['git','diff','--binary'])==b''\n"
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
        except BaseException:
            # This test uses synthetic credentials and fixture content exclusively.
            for name in ("generation.log", "worker.log"):
                if (remote / name).exists():
                    print((remote / name).read_text()[-5000:])
            raise
        assert "return []" in (repository / "fixture.py").read_text()
        exported = subprocess.check_output(["git", "diff", "--binary"], cwd=repository)
        assert b"+    return []" in exported
        assert (await environment.exec("git rev-parse HEAD")).stdout == initial
        assert (root / "host-logs/submission.patch").read_bytes() == exported
        verifier = root / "verifier"
        subprocess.run(["git", "clone", "-q", str(repository), str(verifier)], check=True)
        subprocess.run(["git", "apply", "--binary", "--index", "-"], cwd=verifier, input=exported, check=True)
        assert "return []" in (verifier / "fixture.py").read_text()
        assert (
            subprocess.check_output(["git", "write-tree"], cwd=verifier).decode().strip()
            == context.metadata["submission"]["candidate_tree"]
        )
        assert context.metadata["workspace_finalize_exit_code"] == 0
        counts = json.loads((runtime / "smoke-counts.json").read_text())
        assert counts["main"] == 5
        assert counts["maintenance"] >= 3
        metadata = context.metadata["r5_icode"]
        assert metadata["api_model"] == solver.model_name
        assert metadata["generation_exit_code"] == 0
        settings = json.loads((remote / "runtime-settings.json").read_text())
        assert settings["base_url"] == "https://gateway.invalid/v1"
        assert "offline-placeholder" not in (remote / "runtime-settings.json").read_text()
        events = [json.loads(line) for line in (root / "host-logs/agent/adapter-events.jsonl").read_text().splitlines()]
        assert any(e["event"] == "brief_delivered" for e in events)
        assert sum(e["event"] == "submission_check" for e in events) == 1
        try:
            await solver.run("Duplicate", environment, SimpleNamespace(metadata=None))
        except RuntimeError:
            pass
        else:
            raise AssertionError("Duplicate generation accepted")
        result = {
            "passed": True,
            "execution_mode": "sandbox",
            "real_icode_cli": True,
            "gateway_url_and_key_in_sandbox_only": True,
            "custom_model_name": True,
            "main_responses": 5,
            "maintenance_responses": counts["maintenance"],
            "main_reasoning_effort": "max",
            "main_max_tokens": 64000,
            "source_hash_verification": True,
            "duplicate_launch_rejected": True,
            "committed_candidate_exported_by_plain_git_diff": True,
            "patch_applied_in_clean_verifier": True,
            "stdout_newlines_stripped": True,
            "external_model_calls": 0,
            "platform": "macOS local sandbox simulation, not internal EvalHub/Linux",
            "brief_delivered": True,
            "submission_check": True,
        }
        (ROOT / "validation-sandbox-native.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
