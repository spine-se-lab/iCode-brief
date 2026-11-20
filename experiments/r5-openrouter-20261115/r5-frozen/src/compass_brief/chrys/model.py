# Copyright (c) 2026 Chrys. All rights reserved.

"""Adapt one Chrys profile to the stateless structured model port."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from inspect import isawaitable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from chrys.kernel import Message
from chrys.kernel.client import OUTPUT_CAP_OPTION_ALIASES
from chrys.service.llm.clients import create_client

try:
    from chrys.service.llm.one_shot import get_final_response
except ModuleNotFoundError as exc:
    if exc.name != "chrys.service.llm.one_shot":
        raise
    from chrys.service.llm.responses import get_final_response
from chrys.service.llm.route_sessions import derive_llm_route_session_id
from chrys.service.profiles.models.options import effective_chat_options

from compass_brief.chrys.options import stateless_side_call_options
from compass_brief.model_calls.policy import CallSettings, ModelCallError
from compass_brief.ports import ModelReply

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from chrys.service.profiles.models.schema import ModelProfile

__all__ = ["ChrysMaintenanceModel"]

_ROUTE_KIND = "claim-maintenance"
# Explicitly verified routes only; model-family names do not prove capability.
_OPENROUTER_OPTIONAL_REASONING = frozenset({"z-ai/glm-5.2", "deepseek/deepseek-v4-flash-0731"})


class ChrysMaintenanceModel:
    """One Chrys ModelProfile, reachable as a single structured side call."""

    def __init__(
        self,
        profile: ModelProfile,
        *,
        parent_session_id: str,
        session_dir: Path | None,
        report_usage: Callable[[Mapping[str, Any]], None] | None = None,
        client_factory: Callable[..., Any] = create_client,
        settings: CallSettings | None = None,
        response_format: str = "json_schema",
    ) -> None:
        self._profile = profile
        self._parent_session_id = parent_session_id
        self._session_dir = session_dir
        self._report_usage = report_usage
        self._client_factory = client_factory
        self._settings = settings or CallSettings()
        if response_format not in {"json_schema", "json_object"}:
            raise ValueError("unsupported maintenance response format")
        self._response_format = response_format

    @property
    def model_id(self) -> str:
        """Identify the active model profile in maintenance metrics."""
        return self._profile.id

    @property
    def provider(self) -> str:
        """Identify the active provider in maintenance metrics."""
        return self._profile.provider

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ModelReply:
        """Run one structured, tool-free request against the active profile."""
        options = deepcopy(_side_call_chat_options(self._profile))
        if response_schema.get("title") == "IssueRequirementExtraction":
            options.pop("output_config", None)
        # The profile's own output cap is expressed under several aliases; any
        # one left behind would silently override the ceiling set below.
        for alias in OUTPUT_CAP_OPTION_ALIASES:
            options.pop(alias, None)
            if isinstance(options.get("extra_body"), dict):
                options["extra_body"].pop(alias, None)
        options["max_tokens"] = max_output_tokens
        options.setdefault("temperature", 0)
        options["response_format"] = (
            {"type": "json_object"} if self._response_format == "json_object" else dict(response_schema)
        )
        if self._response_format == "json_object":
            # The transport's JSON mode does not enforce a schema. Keep the
            # contract in the request; the shared parser still validates it.
            system_prompt += "\nReturn JSON matching this schema:\n" + json.dumps(response_schema)
        # This verified model supports disabling reasoning. In bounded structured
        # side calls its inherited high/xhigh reasoning consumed the entire cap.
        # Do not send unsupported controls to other providers or mandatory-reasoning models.
        known_optional = (
            urlsplit(self._profile.base_url).hostname == "openrouter.ai"
            and self._profile.model_id in _OPENROUTER_OPTIONAL_REASONING
        )
        overrides = json.loads(self._settings.reasoning_options)
        mode = self._settings.reasoning
        if not overrides and mode in {"off", "low"} and not known_optional:
            raise ModelCallError("unsupported_reasoning_override")
        if not overrides and known_optional and mode != "inherit":
            for key in ("reasoning", "reasoning_effort", "thinking", "output_config"):
                options.pop(key, None)
            extra = dict(options.get("extra_body") or {})
            for key in ("reasoning", "reasoning_effort", "thinking", "output_config"):
                extra.pop(key, None)
            extra["reasoning"] = {"effort": "low"} if mode == "low" else {"enabled": False}
            options["extra_body"] = extra
        if overrides:
            # Explicit provider-native controls, scoped to this side call only.
            for key in ("reasoning", "reasoning_effort", "thinking", "output_config"):
                options.pop(key, None)
            extra = dict(options.get("extra_body") or {})
            for key in ("reasoning", "reasoning_effort", "thinking", "output_config"):
                extra.pop(key, None)
            extra.update(overrides)
            options["extra_body"] = extra
        if "store" in options:
            # Statelessness: each call carries its whole prompt, so a stored
            # conversation would only leak this session into the next one.
            options["store"] = False
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._client_for_call() as client:
                    response = await get_final_response(
                        client,
                        [Message("system", [system_prompt]), Message("user", [user_prompt])],
                        stream=self._profile.stream,
                        options=options,
                        timeout=min(self._profile.http_read_timeout, timeout_seconds),
                    )
        except httpx.TimeoutException:
            raise ModelCallError("timeout") from None
        except httpx.TransportError:
            raise ModelCallError("provider_transient") from None
        if response.usage_details and self._report_usage is not None:
            self._report_usage(response.usage_details)
        text = response.text or ""
        if getattr(response, "finish_reason", None) == "length" or not text.strip():
            usage = response.usage_details or {}
            # Counts only: never log prompts, response text, reasoning or credentials.
            counts = {
                key: value
                for key in ("output_token_count", "reasoning_output_token_count")
                if isinstance((value := usage.get(key)), int)
            }
            raise ModelCallError(
                "truncated" if getattr(response, "finish_reason", None) == "length" else "empty",
                output_tokens=counts.get("output_token_count", 0),
                reasoning_tokens=counts.get("reasoning_output_token_count", 0),
            )
        return ModelReply(text=text, usage_details=response.usage_details)

    @asynccontextmanager
    async def _client_for_call(self):
        route_session_id = derive_llm_route_session_id(
            self._parent_session_id,
            route_kind=_ROUTE_KIND,
            model_profile=self._profile,
        )
        client = self._client_factory(
            # Our policy owns retries and deadlines; do not multiply them
            # by the host SDK's independent transport retry count.
            profile=replace(self._profile, http_max_retries=0),
            session_id=route_session_id,
            parent_session_id=self._parent_session_id,
            session_dir=self._session_dir,
        )
        # iCode 0.28 uses an async factory; legacy Chrys factories are synchronous.
        if isawaitable(client):
            client = await client
        try:
            yield client
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                result = close()
                if isawaitable(result):
                    await result


def _side_call_chat_options(profile: ModelProfile) -> dict[str, Any]:
    """Preserve profile settings while isolating the maintenance conversation."""
    options = stateless_side_call_options(effective_chat_options(profile) or {})
    return options
