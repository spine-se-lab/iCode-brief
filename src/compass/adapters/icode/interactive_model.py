"""Maintenance through the selected iCode model profile, without another credential."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from chrys.foundation.trajectory.context import side_call_scope
from chrys.kernel import Message
from chrys.service.llm.clients import create_client
from chrys.service.llm.one_shot import get_final_response
from chrys.service.profiles.models.options import effective_chat_options
from compass.ports import ModelReply
from compass.provenance import dump

if TYPE_CHECKING:
    from chrys.service.profiles.models.schema import ModelProfile


class SelectedModel:
    def __init__(self, profile: ModelProfile, report_usage: Callable[[Mapping[str, Any]], None] | None = None) -> None:
        self.profile = deepcopy(profile)
        self.report_usage = report_usage
        self.client = None
        self.closed = False
        self.lock = asyncio.Lock()

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ModelReply:
        async with self.lock:
            if self.closed:
                raise RuntimeError("Compass model is closed")
            if self.client is None:
                self.client = await create_client(self.profile)
        options = deepcopy(effective_chat_options(self.profile) or {})
        # Keep the selected provider's routing, headers and credentials. Only
        # maintenance-specific output controls differ from the main request.
        for key in ("tools", "tool_choice", "max_completion_tokens", "max_output_tokens", "reasoning_effort"):
            options.pop(key, None)
        options.update(max_tokens=max_output_tokens, temperature=0, response_format={"type": "json_object"})
        extra = options["extra_body"] = dict(options.get("extra_body") or {})
        if "openrouter.ai" in self.profile.base_url:
            extra["reasoning"] = {"enabled": False}
        elif self.profile.provider == "deepseek-openai":
            extra["thinking"] = {"type": "disabled"}
        messages = [
            Message(
                "system", [system_prompt + "\nReturn a JSON object matching this schema:\n" + dump(response_schema)]
            ),
            Message("user", [user_prompt]),
        ]
        with side_call_scope("compass_maintenance"):
            async with asyncio.timeout(timeout_seconds):
                response = await get_final_response(self.client, messages, stream=False, options=options)
        if self.report_usage and response.usage_details:
            self.report_usage(response.usage_details)
        if response.finish_reason != "stop" or not response.text:
            raise RuntimeError("Compass maintenance response incomplete")
        return ModelReply(response.text, response.usage_details)

    async def aclose(self) -> None:
        async with self.lock:
            self.closed = True
            if self.client is not None:
                await self.client.aclose()
                self.client = None
