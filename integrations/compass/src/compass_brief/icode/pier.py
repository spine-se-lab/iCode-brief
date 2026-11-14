"""Pier adapter for iCode's native Code Agent, distinct from D/M/S orchestration."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from pathlib import Path

from pier.agents.installed.base import BaseInstalledAgent
from pier.models.agent.install import AgentInstallSpec, InstallStep
from pier.models.agent.network import NetworkAllowlist

from compass_brief.icode.clarification import ARMS, write_contract_reference, write_reference_input


def harness_instruction(instruction: str, run_identity: str) -> str:
    """Shared evaluation hygiene; never inspect or rewrite the submitted tests."""
    marker = "AgentCheck_" + hashlib.sha256(run_identity.encode()).hexdigest()[:12]
    return instruction + (
        "\n\n[Evaluation workspace]\n"
        f"For regression tests you add, include the unique marker {marker} in new test file and "
        "top-level test/helper names, preserving the language's test-discovery prefix. "
        "Do not rename existing repository tests. This avoids collisions with independently added checks; "
        "it is not a requirement about the product implementation. Keep full stdout and stderr from your checks."
    )


class ICodePilotAgent(BaseInstalledAgent):
    def __init__(self, *args, pilot_bundle: str, arm: str, **kwargs):
        super().__init__(*args, **kwargs)
        if arm not in ARMS:
            raise ValueError("Unknown iCode experiment arm")
        self.bundle, self.arm = Path(pilot_bundle), arm
        self.manifest = json.loads((self.bundle / "manifest.json").read_text())
        for name, expected in self.manifest["sha256"].items():
            if hashlib.sha256((self.bundle / name).read_bytes()).hexdigest() != expected:
                raise ValueError(f"pilot bundle digest mismatch: {name}")

    @staticmethod
    def name():
        return "icode-native-pilot"

    def version(self):
        return self.manifest["icode_commit"]

    def network_allowlist(self):
        return NetworkAllowlist(domains=["openrouter.ai"])

    def install_spec(self):
        requirements = base64.b64encode((self.bundle / "requirements.txt").read_bytes()).decode()
        return AgentInstallSpec(
            agent_name=self.name(),
            version=self.version(),
            steps=[
                InstallStep(
                    run=(
                        "curl -LsSf https://astral.sh/uv/install.sh | sh\n"
                        'export PATH="$HOME/.local/bin:$PATH"\n'
                        'uv venv --python 3.14 "$HOME/.local/share/icode-pilot"\n'
                        f"printf %s {requirements} | base64 -d > /tmp/icode-pilot-requirements.txt\n"
                        'uv pip install --python "$HOME/.local/share/icode-pilot/bin/python" -r /tmp/icode-pilot-requirements.txt\n'
                    )
                )
            ],
        )

    async def setup(self, environment):
        await super().setup(environment)
        staged = self.logs_dir / "pilot"
        shutil.copytree(self.bundle, staged, ignore=shutil.ignore_patterns("clarifications"))
        # Stage runtime code only; the external archive stays on the harness host.
        # run() sends only the one matching task supplement to the coding agent.
        source = staged / "compass/src"
        if not ARMS[self.arm][0]:
            for metadata in source.glob("*.dist-info"):
                shutil.rmtree(metadata)  # no entry point = no Compass feature in baseline
        await self.exec_as_agent(
            environment,
            command=(
                'export PATH="$HOME/.local/bin:$PATH"\n'
                'uv pip install --python "$HOME/.local/share/icode-pilot/bin/python" --no-deps /logs/agent/pilot/chrys-0.28.0-py3-none-any.whl\n'
                '"$HOME/.local/share/icode-pilot/bin/python" -c "import sysconfig; from pathlib import Path; '
                "Path(sysconfig.get_path('purelib'), 'codehelix_compass_brief.pth').write_text('/logs/agent/pilot/compass/src\\n')"
                 '"\n'
                '"$HOME/.local/share/icode-pilot/bin/python" -c "from chrys.app.cli.run import main; import compass_brief.icode.run"\n'
            ),
        )

    async def run(self, instruction, environment, context):
        key = self._get_env("OPENROUTER_API_KEY") or self._get_env("APIKEY")
        if not key:
            raise ValueError("OPENROUTER_API_KEY is not set")
        (self.logs_dir / "instruction-original.txt").write_text(instruction)
        contract = harness_instruction(instruction, str(self.logs_dir))
        if self.arm == "arm2_grk_reference":
            rendered = write_contract_reference(self.logs_dir, self.bundle, self.manifest, instruction, contract)
        else:
            rendered = (write_reference_input(self.logs_dir, self.bundle, self.manifest, instruction, contract)
                        if ARMS[self.arm][1] else contract)
        (self.logs_dir / "instruction.txt").write_text(rendered)
        # Do not use BaseInstalledAgent._exec: its debug record includes env values.
        try:
            result = await environment.exec(
                command=f'"$HOME/.local/share/icode-pilot/bin/python" -m compass_brief.icode.run {self.arm} > /logs/agent/icode-result.json 2> /logs/agent/icode-stderr.log',
                env=environment.agent_process_env({"OPENROUTER_API_KEY": key}),
            )
        finally:
            if self.arm == "arm2_grk_reference":
                (self.logs_dir / ".contract-reference.pending.json").unlink(missing_ok=True)
        self.populate_context_post_run(context)
        if result.return_code:
            raise RuntimeError(f"iCode exited {result.return_code}; see icode-stderr.log")

    def populate_context_post_run(self, context):
        path = self.logs_dir / "model-calls.jsonl"
        if not path.exists():
            return
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        replies = [row for row in rows if row["event"] == "response"]
        context.n_input_tokens = sum(row["usage"].get("input_token_count", 0) for row in replies)
        context.n_output_tokens = sum(row["usage"].get("output_token_count", 0) for row in replies)
        # Actual per-generation charges are fetched after the trial, not guessed
        # from a price table. Unknown or missing charges must not become zero.
        context.cost_usd = None
