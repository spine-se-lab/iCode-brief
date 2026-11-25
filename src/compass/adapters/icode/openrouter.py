"""Explicit OpenRouter policy, shared by main profile and maintenance transport."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from compass.ports import ModelReply
from compass.provenance import dump


@dataclass(frozen=True)
class ModelConfig:
    model: str = "deepseek/deepseek-v4.1-flash-1017"
    base_url: str = "https://openrouter.ai/api/v1"
    max_output_tokens: int = 64000
    http_retries: int = 2

    def extra(self, *, maintenance: bool = False) -> dict[str, Any]:
        return {
            "reasoning": {"enabled": False} if maintenance else {"effort": "max"},
            "provider": {"only": ["deepseek"], "allow_fallbacks": False, "require_parameters": True},
        }

    def profile(self, *, api_key: str = "") -> dict[str, Any]:
        return {
            "id": "dee05ee03600",
            "name": "Compass OpenRouter DeepSeek",
            "provider": "deepseek-openai",
            "api_style": "chat_completions",
            "model_id": self.model,
            "base_url": self.base_url,
            "api_key": api_key,
            "stream": False,
            "max_context_tokens": 1000000,
            "max_output_tokens": self.max_output_tokens,
            "http_max_retries": self.http_retries,
            "chat_options": dump({"extra_body": self.extra()}),
        }


class MaintenanceModel:
    def __init__(self, key: str, audit: Callable[..., None], config: ModelConfig | None = None) -> None:
        from openai import AsyncOpenAI

        config = config or ModelConfig()
        self.config = config
        self.audit = audit
        self.client = AsyncOpenAI(api_key=key, base_url=config.base_url, max_retries=config.http_retries)

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ModelReply:
        started = time.monotonic()
        try:
            response = await self.client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                        + "\nReturn a JSON object matching this schema:\n"
                        + dump(response_schema),
                    },
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                max_tokens=max_output_tokens,
                temperature=0,
                extra_body=self.config.extra(maintenance=True),
                timeout=timeout_seconds,
            )
        except Exception as exc:
            self.audit("maintenance_error", error_type=type(exc).__name__)
            raise
        usage = response.usage.model_dump(mode="json") if response.usage else None
        self.audit(
            "maintenance_response",
            model=response.model,
            response_id=response.id,
            usage=usage,
            seconds=time.monotonic() - started,
        )
        if not response.choices or response.choices[0].finish_reason != "stop":
            raise RuntimeError("Maintenance response incomplete")
        return ModelReply(response.choices[0].message.content or "", usage)

    async def aclose(self) -> None:
        await self.client.close()
