"""Run the pinned solver in its own process, with Harbor-owned repository execution."""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
import sys
from pathlib import Path

import r5_native as native


class HarborShell(native.FunctionMiddleware):
    def __init__(self, owner):
        self.owner = owner

    async def process(self, context, call_next):
        if context.function.name in {
            "start_compass_brief",
            "inspect_compass_brief",
            "compress_context",
            "list_compressed_contexts",
            "recall_context",
        }:
            await call_next()
            return
        if context.function.name != "execute":
            raise RuntimeError("Repository tool is outside the Harbor execution contract")
        arguments = context.arguments
        arguments = arguments.model_dump() if hasattr(arguments, "model_dump") else dict(arguments)
        seconds = min(300, max(1, int(arguments.get("timeout", 30))))
        workdir = posixpath.normpath(posixpath.join(SETTINGS["workdir"], arguments.get("workdir") or "."))
        reader, writer = await asyncio.open_unix_connection(SETTINGS["socket"], limit=1048576)
        try:
            request = {"command": arguments["command"], "cwd": workdir, "timeout_sec": seconds}
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()
            response = json.loads(await asyncio.wait_for(reader.readline(), seconds + 30))
        finally:
            writer.close()
            await writer.wait_closed()
        if response.get("error"):
            raise RuntimeError("Harbor execution failed: " + response["error"])
        text = response["output"]
        context.result = text + f"\n[exit_code: {response['return_code']}]"
        context.metadata[native.TOOL_RESULT_METADATA_KEY] = {
            "shell_exit_code": response["return_code"],
            "failed": response["return_code"] != 0,
            "compass_container_execution": True,
        }
        self.owner.audit(
            "container_tool",
            tool="execute",
            exit_code=response["return_code"],
            command_sha256=native.sha(arguments["command"]),
        )


def main(settings_path):
    global SETTINGS
    SETTINGS = json.loads(Path(settings_path).read_text())
    root = Path(__file__).resolve().parent
    directory = Path(SETTINGS["agent_dir"])
    workspace = Path(SETTINGS["workspace"])
    task_path = Path(SETTINGS["task_path"])
    directory.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    native.MODEL = SETTINGS["model"]
    native.BASE_URL = SETTINGS["base_url"]
    constants = native.frozen_constants(root.parent / "compass")
    native.ContainerShell = HarborShell
    config = native.configure(directory, root.parents[1], container=True)
    native.bind_config(config)
    key = os.environ["R5_EVALHUB_API_KEY"]
    os.environ["DEEPSEEK_API_KEY"] = key
    os.environ["CHRYS_MAX_TRANSIENT_RETRIES"] = "0"
    owner = native.Controller(task_path.read_text(), workspace, directory, constants)
    owner.model = native.OfficialMaintenance(key, owner.audit)
    owner.container = "harbor-environment"
    native.controller = owner
    owner.audit("harbor_transport", workdir=SETTINGS["workdir"], upstream=native.UPSTREAM, r5_archive=native.ARCHIVE)
    native.install_runtime(owner)
    from chrys.app.cli.run import main as cli

    return cli(
        [
            "--task",
            str(task_path),
            "--workdir",
            str(workspace),
            "--agent",
            "R5Code",
            "--model",
            native.MODEL_PROFILE,
            "--json",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
