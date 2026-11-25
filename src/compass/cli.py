"""Dedicated opt-in headless entry point; no modifications to installed profiles."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from importlib.resources import files
from pathlib import Path

from .adapters.icode.credentials import key_file_template, read_key
from .adapters.icode.openrouter import MaintenanceModel, ModelConfig
from .adapters.icode.storage import Artifacts
from .evaluation.budget import MAX_CALLS, MAX_SECONDS
from .evaluation.prompts import WORKFLOW
from .evaluation.records import RunRequest, RunResult
from .provenance import ARCHIVE, SOURCE_ROOT, UPSTREAM, dump, sha, source_constants


def prepare(directory, config=None, *, key_file=None):
    config = config or ModelConfig()
    import yaml

    root = directory / "config"
    agent = yaml.safe_load(files("chrys").joinpath("service/profiles/agents/builtins/Code.yaml").read_text())
    agent.update(name="CompassCode", id="c0de05003400", extensions=[])
    agent["sub_agents"] = {"agents": []}
    agent["skills"] = {
        "paths": [],
        "inline": [],
        "auto_load_user_agents_skills": False,
        "auto_load_cwd_agents_skills": False,
    }
    agent["tools"]["builtins"] = ["filesystem.read", "filesystem.write", "shell", "search"]
    # Only explicitly inspected repository material goes into Compass exploration.
    agent["memory"] = {"files": [], "folders": []}
    agent["instructions"] += WORKFLOW
    values = {
        "agents/CompassCode.yaml": agent,
        "models/dee05ee03600.yaml": config.profile(api_key=key_file_template(key_file) if key_file is not None else ""),
        "settings.yaml": {
            "model": {"profile": {"active": "dee05ee03600"}},
            "session": {"title": {"auto": False}},
            "llm": {"retry": {"max_transient": 0}},
            "log": {"raw_http_capture": False},
        },
    }
    for name, value in values.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        Artifacts.write_text(path, yaml.safe_dump(value, allow_unicode=True, sort_keys=False))
    return root


async def execute(request: RunRequest, constants, key):
    task, cwd, directory = request.task, request.workspace, request.directory
    # Imported after CHRYS_CONFIG_DIR is set, before bootstrap freezes settings.
    from chrys.foundation.config.settings_store import load_settings
    from chrys.orchestration.session_host import ChrysSessionHost
    from chrys.orchestration.startup import bootstrap_runtime
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.models.registry import ModelProfileRegistry

    from .adapters.icode.headless import HeadlessBackend
    from .evaluation.run_state import RunState
    from .evaluation.workflow import CompassRun

    bootstrap_runtime(setup_telemetry=False)
    loaded = load_settings(
        model_profile="dee05ee03600", max_transient_retries=0, session_title_auto=False, raw_http_capture=False
    )
    agents, models = AgentProfileRegistry(), ModelProfileRegistry()
    agents.load_user_profiles(directory / "config/agents")
    models.load_profiles(directory / "config/models")
    owner = RunState(task, cwd, directory, constants, artifacts=Artifacts())
    owner.model = MaintenanceModel(key, owner.audit)
    # The callback is per host, not a process-wide replacement; it reattaches
    # after a native rebuild at the start of either Compass turn.
    backend = None

    def on_turn_started():
        if backend is not None:
            backend.attach()

    host = None
    try:
        host = ChrysSessionHost(
            profile_name="CompassCode",
            surface=None,
            loaded_settings=loaded,
            agent_registry=agents,
            model_registry=models,
            cwd=str(cwd),
            on_turn_started=on_turn_started,
        )
        host.engine.pin_model_profile()
        backend = HeadlessBackend(owner, host, agents.get("CompassCode"))
        run = CompassRun(owner, backend)
        return await run.run()
    finally:
        try:
            if host is not None:
                await host.shutdown()
        finally:
            await owner.model.aclose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="New private run artifact directory")
    parser.add_argument("--task", type=Path)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--key-file", type=Path, help="Owner-only OpenRouter key file; never copied into profiles")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    directory = args.directory.resolve()
    if directory.exists():
        parser.error("Use a new directory; existing run artifacts are never overwritten")
    constants = source_constants(SOURCE_ROOT)
    if not args.prepare_only and not all((args.task, args.cwd, args.key_file)):
        parser.error("running requires --task, --cwd and --key-file")
    # Resolve/read inputs before recording dispatch. No credential is saved.
    task = args.task.read_text() if args.task else ""
    key = read_key(args.key_file) if not args.prepare_only else None
    cwd = args.cwd.resolve() if args.cwd else None
    if cwd is not None and not cwd.is_dir():
        parser.error("--cwd must be an existing repository directory")
    directory.mkdir(parents=True, mode=0o700)
    config = prepare(directory, key_file=args.key_file)
    if args.prepare_only:
        sys.stdout.write(dump({"prepared": True, "model_calls": 0, "config": str(config), "upstream": UPSTREAM}) + "\n")
        return 0
    os.environ["CHRYS_CONFIG_DIR"] = str(config)
    os.environ["CHRYS_MAX_TRANSIENT_RETRIES"] = "0"
    os.environ["CHRYS_SESSION_TITLE_AUTO"] = "0"
    os.environ["CHRYS_DEBUG_LLM_RAW_HTTP_LOG"] = "0"
    Artifacts.write_text(
        directory / "run-intent.json",
        dump(
            {
                "task_sha256": sha(task),
                "upstream_base": UPSTREAM,
                "compass_archive": ARCHIVE,
                "model": ModelConfig().model,
                "main_budget": MAX_CALLS,
                "wall_seconds": MAX_SECONDS,
            }
        ),
    )
    try:
        result = asyncio.run(execute(RunRequest(task, cwd, directory), constants, key))
    except Exception as exc:
        # SDK exception strings can contain request material.
        outcome = RunResult(
            status="timeout" if isinstance(exc, TimeoutError) else "failed", error_type=type(exc).__name__
        ).to_dict()
        Artifacts.write_text(directory / "outcome.json", dump(outcome))
        sys.stdout.write(dump(outcome) + "\n")
        return 1
    outcome = RunResult(status="completed", session_id=result.session_id, text=result.text).to_dict()
    Artifacts.write_text(directory / "outcome.json", dump(outcome))
    sys.stdout.write(dump(outcome) + "\n")
    return 0
