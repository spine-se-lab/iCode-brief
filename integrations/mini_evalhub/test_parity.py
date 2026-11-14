"""Regression checks for historical retry and terminal-state semantics."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "compass/src"))
sys.path.insert(0, str(ROOT))
os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(ROOT / ".venv/mini-config")
os.environ["MSWEA_SILENT_STARTUP"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

import litellm
import minisweagent.models.utils.retry as native_retry
import runtime_bridge
from compass_brief.mini.agent import LitellmMaintenanceModel, SideCallMeter
from compass_brief.model_calls.policy import CallSettings, ModelCallError, structured_call


class ParityChecks(unittest.TestCase):
    def run_terminal(self, directory, exit_status, during_run=None):
        root = Path(directory)
        settings = {
            "agent_dir": str(root / "agent"),
            "workdir": str(root),
            "socket": str(root / "socket"),
            "model": "platform-alias",
            "base_url": "https://gateway.invalid/v1",
            "task_path": str(root / "task.txt"),
        }
        (root / "task.txt").write_text("synthetic task")
        (root / "settings.json").write_text(json.dumps(settings))
        instance = Mock(n_calls=250, messages=[])
        instance.serialize.return_value = {"info": {"compass": {}}}

        def run(*args, **kwargs):
            if during_run:
                during_run()
            return {"exit_status": exit_status}

        instance.run.side_effect = run
        with (
            patch.dict(runtime_bridge.os.environ, {"R5_EVALHUB_API_KEY": "synthetic-key"}),
            patch.object(runtime_bridge, "ExploringCompassMiniAgent", return_value=instance) as constructor,
            patch.object(
                runtime_bridge.HarborEnvironment,
                "execute",
                return_value={
                    "output": "Linux\nrelease\nversion\nx86_64\n",
                    "returncode": 0,
                },
            ),
        ):
            code = runtime_bridge.main(root / "settings.json")
        self.assertEqual(constructor.call_args.kwargs["step_limit"], 250)
        self.assertEqual(constructor.call_args.kwargs["cost_limit"], 0)
        self.assertEqual(constructor.call_args.args[1].config.env, {})
        return code, json.loads((root / "agent/run-status.json").read_text())

    def test_native_terminal_codes_and_fixed_budget(self):
        for status, expected in (("Submitted", 0), ("LimitsExceeded", 1), ("RepeatedFormatError", 1)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                code, result = self.run_terminal(directory, status)
                self.assertEqual(code, expected)
                self.assertEqual(result["exit_status"], status)
                self.assertFalse(result["graded"])

    def test_maintenance_transient_errors_reach_frozen_retry_policy(self):
        class ProviderError(Exception):
            def __init__(self, status):
                self.status_code = status

        for status, attempts in ((429, 2), (503, 2), (401, 1)):
            completion = AsyncMock(side_effect=ProviderError(status))

            def side_call(attempts=attempts):
                model = LitellmMaintenanceModel(
                    "openai/platform-alias",
                    SideCallMeter(),
                    reasoning={"reasoning": {"enabled": False}},
                    response_format="json_object",
                )
                with self.assertRaises(ModelCallError) as error:
                    asyncio.run(
                        structured_call(
                            model,
                            system_prompt="synthetic",
                            user_prompt="synthetic",
                            response_schema={"type": "object", "properties": {}},
                            parse=lambda reply: json.loads(reply.text),
                            settings=CallSettings(),
                        )
                    )
                self.assertEqual(error.exception.code, "provider_transient" if attempts == 2 else "provider_error")

            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as directory,
                patch.object(litellm, "acompletion", completion),
            ):
                self.run_terminal(directory, "Submitted", side_call)
            self.assertEqual(completion.await_count, attempts)
            for call in completion.call_args_list:
                self.assertNotIn("num_retries", call.kwargs)
                self.assertNotIn("max_retries", call.kwargs)
                self.assertEqual(call.kwargs["extra_body"], {"thinking": {"type": "disabled"}})
                self.assertEqual(call.kwargs["max_tokens"], 16384)
                self.assertEqual(call.kwargs["temperature"], 0)

    def test_main_retry_limit_backoff_and_authentication_abort(self):
        original_retrying = native_retry.Retrying
        sleeps = []

        def retrying(**kwargs):
            return original_retrying(**kwargs, sleep=sleeps.append)

        for status, attempts in ((503, 10), (401, 1)):
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(runtime_bridge.os.environ, {"MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT": "10"}),
                patch.object(native_retry, "Retrying", side_effect=retrying),
                patch.object(runtime_bridge.requests, "post", return_value=SimpleNamespace(status_code=status)) as post,
            ):
                sleeps.clear()
                model = runtime_bridge.GatewayModel(
                    base_url="https://gateway.invalid/v1",
                    key="synthetic-key",
                    audit=runtime_bridge.Audit(Path(directory)),
                    model_name="platform-alias",
                    model_kwargs={"thinking": {"type": "enabled"}, "reasoning_effort": "max", "max_tokens": 64000},
                    cost_tracking="ignore_errors",
                )
                exception = RuntimeError if status == 503 else runtime_bridge.OpenRouterAuthenticationError
                with self.assertRaises(exception):
                    model.query([])
                self.assertEqual(post.call_count, attempts)
                self.assertEqual(sleeps, [4, 4, 4, 8, 16, 32, 60, 60, 60] if attempts == 10 else [])
                self.assertEqual(post.call_args.kwargs["timeout"], 900)
