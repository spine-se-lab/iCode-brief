"""Host-side mini runner: model credentials stay outside the task container."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import subprocess
import time
from hashlib import sha256
from pathlib import Path

import yaml

# Official DeepSeek API. `deepseek-flash` is a floating alias (DeepSeek-V4.1-Flash-1017 on 2026-11-04, per
# api-docs.deepseek.com pricing); every response's `model` and `system_fingerprint` is logged to pin what ran.
DEEPSEEK_API_BASE = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-flash"
_KEY_ENV = ("DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE")


def read_key(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Key file must be a regular file owned by this user, with permissions 600 or 400")
        value = handle.read(16385).strip()
    if not value or len(value) > 16384 or any(c.isspace() for c in value):
        raise ValueError("Key file must contain one nonempty token, without an export or variable assignment")
    if "=" in value or value.startswith(("'", '"')):
        raise ValueError("Write only the API key, without shell syntax")
    return value


def isolated_environment_options(image: str, cwd: str, network: str = "none", proxy: str = "") -> dict:
    if (network == "none") != (not proxy):
        raise ValueError("A connected task requires the controlled egress proxy")
    proxy_env = dict.fromkeys(("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"), proxy) if proxy else {}
    return {
        "image": image, "cwd": cwd, "env": proxy_env, "forward_env": [],
        "run_args": ["--rm", "--platform=linux/amd64", f"--network={network}", "--cap-drop=NET_RAW", "--cpus=2", "--memory=5g"],
        "timeout": 300, "container_timeout": "4h",
    }


def verify_isolation(env, key: str, network: str = "none") -> dict:
    details = json.loads(subprocess.check_output(  # noqa: S603 — executable and container ID come from the host DockerEnvironment
        [env.config.executable, "inspect", env.container_id], text=True,
    ))[0]
    if details["HostConfig"]["NetworkMode"] != network or details.get("Mounts"):
        raise RuntimeError("Task container has an unexpected network or host mount")
    if network != "none":
        network_info = json.loads(subprocess.check_output(  # noqa: S603 — host deployment network name, never model input
            [env.config.executable, "network", "inspect", network], text=True,
        ))[0]
        if not network_info.get("Internal"):
            raise RuntimeError("Connected tasks must use an internal network with controlled proxy egress")
    variables = details.get("Config", {}).get("Env") or []
    if any(key in value or any(word in value.partition("=")[0].upper()
                              for word in ("API_KEY", "APIKEY", "ACCESS_TOKEN", "SECRET")) for value in variables):
        raise RuntimeError("Task image contains credentials; prepare a clean image before solving")
    return {"network": network, "host_mounts": 0, "credential_env_checked": True, "image_id": details["Image"]}


def collect_patch(env, base_commit: str) -> str:
    """Retain committed changes as well as staged/unstaged/untracked changes."""
    patch = env.execute({"command": "git add -N . && git diff --binary " + shlex.quote(base_commit)})
    if patch["returncode"]:
        raise RuntimeError("Could not collect candidate patch")
    return patch["output"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Prepared task image with source and dependencies, no hidden tests")
    parser.add_argument("--cwd", default="/app")
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--reference-file", type=Path, help="Optional vetted guidance, read only by contract extraction")
    parser.add_argument("--output", type=Path, required=True, help="New host-only run directory")
    parser.add_argument("--arm", choices=("baseline", "arm2", "arm2_grk_reference", "arm2_grk_no_reference"), default="arm2")
    parser.add_argument("--task-network", default="none")
    parser.add_argument("--proxy-url", default="")
    parser.add_argument("--step-limit", type=int, default=250)
    parser.add_argument("--cost-limit", type=float, default=0)
    args = parser.parse_args(argv)
    if args.arm == "baseline" and args.reference_file:
        parser.error("baseline does not consume Compass reference material")
    if args.arm == "arm2_grk_reference" and not args.reference_file:
        parser.error("arm2_grk_reference requires nonempty reference material")
    if args.arm == "arm2_grk_no_reference" and args.reference_file:
        parser.error("arm2_grk_no_reference must not receive reference material")
    task = args.task_file.read_text()
    if not task.strip():
        parser.error("task must not be empty")
    reference = args.reference_file.read_text() if args.reference_file else ""
    if args.arm == "arm2_grk_reference" and not reference.strip():
        parser.error("reference material is empty")
    key = read_key(args.key_file)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    previous_env = {name: os.environ.get(name) for name in _KEY_ENV}
    env = agent = None
    result = {}
    started = time.monotonic()
    try:
        # mini can read a user dotenv at import time. The explicit file remains authoritative.
        import minisweagent
        import requests
        from minisweagent.environments.docker import DockerEnvironment
        from minisweagent.models.openrouter_model import OpenRouterAuthenticationError, OpenRouterModel
        from minisweagent.models.utils.actions_toolcall import BASH_TOOL

        class AuditedModel(OpenRouterModel):
            def _query(self, messages, **kwargs):
                # Log the actual prepared request identity, never credentials or raw prompts.
                # Assistant messages go back as returned, so `reasoning_content` is passed back as the
                # official API requires for thinking mode with tools.
                payload = {"model": self.config.model_name, "messages": messages,
                           "tools": [BASH_TOOL], **(self.config.model_kwargs | kwargs)}
                record = {"at": time.time(), "messages": len(messages),
                          "message_hashes": [sha256(json.dumps(m, sort_keys=True).encode()).hexdigest() for m in messages],
                          "provider": "deepseek-official", "reasoning": payload["reasoning_effort"],
                          "brief_messages": sum(
                              (m.get("role"), m.get("content")) in {
                                  (b.get("role"), b.get("content")) for b in agent.messages
                                  if b.get("extra", {}).get("compass") == "brief" and isinstance(b.get("content"), str)
                              } for m in messages if isinstance(m.get("content"), str)
                          ) if agent is not None else 0}
                with (args.output / "requests.jsonl").open("a") as log:
                    log.write(json.dumps(record) + "\n")
                response = requests.post(DEEPSEEK_API_BASE + "/chat/completions",
                    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                    json=payload, timeout=900)
                if response.status_code == 401:
                    raise OpenRouterAuthenticationError("Authentication failed")
                response.raise_for_status()
                data = response.json()
                with (args.output / "responses.jsonl").open("a") as log:
                    log.write(json.dumps({"at": time.time(), "id": data.get("id"), "model": data.get("model"),
                                          "system_fingerprint": data.get("system_fingerprint"),
                                          "usage": data.get("usage"), "error": data.get("error")}) + "\n")
                if not data.get("choices"):
                    raise RuntimeError("Provider returned no choices; see host-only response audit")
                if record["brief_messages"] and not (args.output / "first-brief-request.json").exists():
                    (args.output / "first-brief-request.json").write_text(json.dumps(record, indent=2))
                return data

        from compass_brief.mini.exploration import ExploringCompassMiniAgent
        from compass_brief.mini.submission import SubmissionSafeMiniAgent

        # Set after mini's dotenv import so the explicit key file and endpoint win; LiteLLM (Maintenance) reads
        # both, and would otherwise default to the /beta base.
        os.environ.update(DEEPSEEK_API_KEY=key, DEEPSEEK_API_BASE=DEEPSEEK_API_BASE)
        native = yaml.safe_load((Path(minisweagent.__file__).parent / "config/mini.yaml").read_text())
        model = AuditedModel(
            **(native.get("model", {}) | {
                "model_name": DEEPSEEK_MODEL,
                "model_kwargs": {"reasoning_effort": "max", "max_tokens": 64000},
            })
        )
        env = DockerEnvironment(**isolated_environment_options(args.image, args.cwd, args.task_network, args.proxy_url))
        isolation = verify_isolation(env, key, args.task_network)
        base = env.execute({"command": "git rev-parse HEAD"})
        if base["returncode"]:
            raise RuntimeError("Task image must contain a Git checkout")
        base_commit = base["output"].strip()
        metadata = {
            "container_id": env.container_id, "base_commit": base_commit, "adapter": "mini-host-v0240", "mini_version": minisweagent.__version__, "arm": args.arm,
            "task_sha256": sha256(task.encode()).hexdigest(),
            "reference_sha256": sha256(reference.encode()).hexdigest() if reference else None,
            "reference_supplied": bool(reference), "isolation": isolation,
            "provider": "deepseek-official", "api_base": DEEPSEEK_API_BASE, "model": DEEPSEEK_MODEL, "reasoning": "max",
        }
        (args.output / "run-identity.json").write_text(json.dumps(metadata, indent=2))
        options = native["agent"] | {
            "mode": "yolo", "confirm_exit": False, "step_limit": args.step_limit,
            "cost_limit": args.cost_limit, "output_path": args.output / "trajectory.json",
        }
        if args.arm != "baseline":
            agent = ExploringCompassMiniAgent(
                model, env, **options, compass_directory=args.output / "compass",
                compass_maintenance_model="deepseek/" + DEEPSEEK_MODEL,
                compass_maintenance_response_format="json_object", reference_context=reference,
            )
        else:
            agent = SubmissionSafeMiniAgent(model, env, **options)
        platform = env.execute({"command": "uname -s; uname -r; uname -v; uname -m"})
        if platform["returncode"] or len(platform["output"].splitlines()) != 4:
            raise RuntimeError("Could not determine the actual task-container platform")
        result = agent.run(task, **dict(zip(("system", "release", "version", "machine"), platform["output"].splitlines(), strict=True)))
        # Include commits and untracked additions relative to the frozen starting revision.
        (args.output / "model.patch").write_text(collect_patch(env, base_commit))
        return 0 if result.get("exit_status") == "Submitted" else 1
    finally:
        if agent is not None:
            trajectory = agent.serialize()
            usage = [m.get("extra", {}).get("response", {}).get("usage", {}) for m in agent.messages]
            main_usage = {name: sum(u.get(name, 0) or 0 for u in usage)
                          for name in ("prompt_tokens", "completion_tokens")}
            main_usage["cost_usd"] = agent.cost
            summary = {"exit_status": result.get("exit_status", "runtime_error"),
                       "seconds": time.monotonic() - started, "main": main_usage,
                       "compass": trajectory["info"].get("compass", {}).get("side_calls"), "graded": False}
            (args.output / "usage.json").write_text(json.dumps(summary, indent=2))
        if env is not None:
            env.cleanup()
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == "__main__":
    raise SystemExit(main())
