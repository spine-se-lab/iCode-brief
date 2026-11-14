"""Frozen Compass v1.9/mini loop with EvalHub gateway and sandbox transport adapters."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import shlex
import socket
import sys
import tarfile
import time
from pathlib import Path

# Keep mini's dotenv/config lookup inside this trial, before importing the package.
os.environ.setdefault("MSWEA_GLOBAL_CONFIG_DIR", str(Path(__file__).resolve().parents[2] / ".mini-config"))
os.environ["MSWEA_SILENT_STARTUP"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

import requests
import yaml
from compass_brief.mini.exploration import ExploringCompassMiniAgent
from minisweagent import package_dir
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.openrouter_model import OpenRouterAuthenticationError, OpenRouterModel
from minisweagent.models.utils.actions_toolcall import BASH_TOOL

ARCHIVE_SHA = "032bd720722e99ea3c783a8844aa15fac31e5c612203e18f1060c423e3923ac7"


def verify_source(root: Path):
    manifest = json.loads((root / "SOURCE-MANIFEST.json").read_text())
    if (
        manifest.get("compass_version") != "1.9"
        or manifest["runtime_package_sha256"] != ARCHIVE_SHA
        or len(manifest["files_sha256"]) != 72
    ):
        raise RuntimeError("Unexpected R5 source manifest")
    for name, expected in manifest["files_sha256"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Frozen R5 source mismatch: " + name)
    import compass_brief.mini.exploration as exploration

    if not Path(exploration.__file__).resolve().is_relative_to(root.resolve()):
        raise RuntimeError("Wrong R5 import location")
    for package, version in {
        "mini-swe-agent": "2.4.6",
        "litellm": "1.104.2",
        "requests": "2.34.2",
        "PyYAML": "6.0.3",
    }.items():
        if importlib.metadata.version(package) != version:
            raise RuntimeError(package + " " + version + " is required")
    return manifest


def normalized_usage(usage):
    result = dict(usage or {})
    if result.get("prompt_cache_hit_tokens") is None:
        result["prompt_cache_hit_tokens"] = (result.get("prompt_tokens_details") or {}).get("cached_tokens")
    return result


class Audit:
    def __init__(self, directory):
        self.path = directory / "adapter-events.jsonl"
        self.responses = {"main": 0, "maintenance": 0}

    def write(self, event, **values):
        with self.path.open("a") as stream:
            stream.write(json.dumps({"event": event, "at": time.time(), **values}) + "\n")

    def response(self, kind, data):
        self.responses[kind] += 1
        message = (data.get("choices") or [{}])[0].get("message") or {}
        self.write(
            kind + "_response",
            model=data.get("model"),
            usage=normalized_usage(data.get("usage")),
            reasoning_content_present=bool(message.get("reasoning_content")),
        )


class GatewayModel(OpenRouterModel):
    """Reuse mini's native history, tool parsing, retries and observation formatter."""

    def __init__(self, *, base_url, key, audit, **kwargs):
        super().__init__(**kwargs)
        self._api_url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = key
        self.audit = audit

    def _calculate_cost(self, response):
        # The gateway can omit provider billing. This is mini's internal cost
        # accumulator only; retain the provider's original usage in the audit.
        cost = (response.get("usage") or {}).get("cost")
        return {"cost": float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else 0.0}

    def _query(self, messages, **kwargs):
        body = {
            "model": self.config.model_name,
            "messages": messages,
            "tools": [BASH_TOOL],
            **(self.config.model_kwargs | kwargs),
        }
        self.audit.write(
            "main_request",
            model=body["model"],
            reasoning_effort=body["reasoning_effort"],
            thinking=body["thinking"],
            max_tokens=body["max_tokens"],
            message_count=len(messages),
        )
        try:
            response = requests.post(
                self._api_url,
                headers={"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"},
                json=body,
                timeout=900,
            )
            if response.status_code == 401:
                raise OpenRouterAuthenticationError("Gateway authentication failed")
            if response.status_code != 200:
                raise RuntimeError("Gateway HTTP " + str(response.status_code))
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError("Gateway response is not an object")
            self.audit.response("main", data)
            if not data.get("choices"):
                raise RuntimeError("Gateway response has no choices")
        except requests.RequestException as error:
            self.audit.write("main_error", error_type=type(error).__name__)
            raise RuntimeError("Gateway transport " + type(error).__name__) from None
        return data


class HarborEnvironment(LocalEnvironment):
    """Use mini's completion sentinel semantics over the existing Harbor bridge."""

    def __init__(self, settings, env):
        super().__init__(cwd=settings["workdir"], timeout=300, env=env)
        self.socket_path = settings["socket"]

    def get_template_vars(self, **kwargs):
        # LocalEnvironment's default includes os.environ, which contains credentials.
        return self.config.model_dump() | platform.uname()._asdict() | kwargs

    def execute(self, action, cwd="", *, timeout=None):
        seconds = min(300, max(1, int(timeout or self.config.timeout)))
        command = "bash -lc " + shlex.quote(action.get("command", ""))
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(seconds + 30)
            connection.connect(self.socket_path)
            request = {"command": command, "cwd": cwd or self.config.cwd, "timeout_sec": seconds}
            connection.sendall((json.dumps(request) + "\n").encode())
            with connection.makefile("rb") as stream:
                result = json.loads(stream.readline())
        output = {
            "output": result["output"],
            "returncode": result["return_code"],
            "exception_info": result.get("exception_info", ""),
        }
        self._check_finished(output)
        return output


def main(settings_path):
    settings = json.loads(Path(settings_path).read_text())
    verify_source(Path(__file__).resolve().parent.parent / "compass")
    directory = Path(settings["agent_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    audit = Audit(directory)
    key = os.environ.pop("R5_EVALHUB_API_KEY")
    native = yaml.safe_load((package_dir / "config/mini.yaml").read_text())
    model = GatewayModel(
        **(
            native["model"]
            | {
                "model_name": settings["model"],
                "model_kwargs": {"thinking": {"type": "enabled"}, "reasoning_effort": "max", "max_tokens": 64000},
                "cost_tracking": "ignore_errors",
            }
        ),
        base_url=settings["base_url"],
        key=key,
        audit=audit,
    )
    env = HarborEnvironment(settings, {})
    # Keep the frozen side-call prompts, validation, budgets and meter. Only map
    # the OpenRouter transport controls onto the platform's OpenAI-compatible API.
    import litellm

    completion = litellm.acompletion

    async def gateway_completion(**kwargs):
        kwargs["api_base"] = settings["base_url"]
        kwargs["api_key"] = key
        reasoning = (kwargs.pop("extra_body", None) or {}).get("reasoning", {})
        if reasoning != {"enabled": False}:
            raise RuntimeError("Unexpected frozen maintenance reasoning policy")
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        audit.write(
            "maintenance_request",
            model=settings["model"],
            thinking=kwargs["extra_body"]["thinking"],
            max_tokens=kwargs["max_tokens"],
            temperature=kwargs["temperature"],
        )
        try:
            reply = await completion(**kwargs)
        except Exception as error:
            audit.write("maintenance_error", error_type=type(error).__name__)
            # Preserve status_code so the frozen core can retry transient provider errors.
            raise
        audit.response("maintenance", reply.model_dump())
        return reply

    litellm.acompletion = gateway_completion
    agent = None
    started = time.monotonic()
    result = {"exit_status": "runtime_error"}
    try:
        agent = ExploringCompassMiniAgent(
            model,
            env,
            **(
                native["agent"]
                | {
                    "mode": "yolo",
                    "confirm_exit": False,
                    "step_limit": 250,
                    "cost_limit": 0,
                    "output_path": directory / "trajectory.json",
                }
            ),
            compass_directory=directory / "compass",
            compass_maintenance_model="openai/" + settings["model"],
            compass_maintenance_response_format="json_object",
            compass_review_before_submit=False,
            reference_context="",
        )
        system = env.execute({"command": "uname -s; uname -r; uname -v; uname -m"})
        lines = system["output"].splitlines()
        if system["returncode"] or len(lines) != 4:
            raise RuntimeError("Cannot identify the actual task sandbox")
        result = agent.run(
            Path(settings["task_path"]).read_text(),
            **dict(zip(("system", "release", "version", "machine"), lines, strict=True)),
        )
        return 0 if result.get("exit_status") == "Submitted" else 1
    finally:
        litellm.acompletion = completion
        status = {
            "exit_status": result.get("exit_status"),
            "seconds": time.monotonic() - started,
            "responses": audit.responses,
            "graded": False,
        }
        if agent is not None:
            trajectory = agent.serialize()
            status["compass"] = trajectory["info"]["compass"]
            status["main_steps"] = agent.n_calls
            status["brief_messages"] = sum(m.get("extra", {}).get("compass") == "brief" for m in agent.messages)
            status["brief_followed_by_main_response"] = sum(
                m.get("extra", {}).get("compass") == "brief"
                and any(later.get("role") == "assistant" for later in agent.messages[i + 1 :])
                for i, m in enumerate(agent.messages)
            )
        if result.get("exit_status") == "LimitsExceeded":
            audit.write("budget_exhausted")
        (directory / "run-status.json").write_text(json.dumps(status, indent=2))
        if (directory / "compass").exists():
            with tarfile.open(directory / "compass-artifacts.tar.gz", "w:gz") as archive:
                archive.add(directory / "compass", arcname="compass")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1]))
    except Exception as error:
        # Avoid surfacing SDK response bodies, request headers or credentials.
        print(json.dumps({"error": type(error).__name__}))
        raise SystemExit(1) from None
