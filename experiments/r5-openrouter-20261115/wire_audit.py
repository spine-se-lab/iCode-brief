"""Validate final serialized model requests and retain only safe parameter evidence."""
import json

import httpx

MODEL = "deepseek/deepseek-v4.1-flash-1017"
RESPONSE_MODELS = {MODEL, "deepseek/deepseek-v4.1-flash-20261017"}
PROVIDER = {"only": ["deepseek"], "allow_fallbacks": False, "require_parameters": True}


def validate_request(request):
    if str(request.url) != "https://openrouter.ai/api/v1/chat/completions":
        raise RuntimeError("Unexpected model endpoint")
    body = json.loads(request.content)
    if body.get("model") != MODEL or body.get("provider") != PROVIDER:
        raise RuntimeError("Unexpected model or provider routing")
    reasoning = body.get("reasoning")
    if reasoning == {"enabled": False}:
        role = "maintenance"
        if body.get("tools") or body.get("temperature") != 0:
            raise RuntimeError("Unexpected maintenance configuration")
    elif reasoning == {"effort": "max"}:
        role = "main" if body.get("tools") else "icode_auxiliary"
        if role == "main" and body.get("max_tokens") != 64000:
            raise RuntimeError("Main output limit must be 64000")
    else:
        raise RuntimeError("Unexpected reasoning policy")
    if not isinstance(body.get("max_tokens"), int) or not 0 < body["max_tokens"] <= 64000:
        raise RuntimeError("Unexpected output limit")
    if body.get("stream", False):
        raise RuntimeError("Unexpected streaming request")
    for forbidden in ("reasoning_effort", "thinking", "models", "route"):
        if forbidden in body:
            raise RuntimeError("Conflicting model request option")
    return role, {key: body[key] for key in (
        "model", "provider", "reasoning", "max_tokens", "temperature", "top_p", "top_k", "stream"
    ) if key in body}


def install_wire_audit(audit):
    original_send = httpx.AsyncClient.send

    async def send(client, request, **kwargs):
        try:
            role, parameters = validate_request(request)
        except Exception:
            audit("configuration_violation", stage="request")
            raise
        audit("wire_request", role=role, parameters=parameters)
        response = await original_send(client, request, **kwargs)
        if response.status_code == 200:
            await response.aread()
            data = response.json()
            model, provider = data.get("model"), data.get("provider")
            audit("wire_response", role=role, model=model, provider=provider,
                  response_id=data.get("id"), usage=data.get("usage"))
            if model not in RESPONSE_MODELS or provider != "DeepSeek":
                audit("configuration_violation", stage="response", model=model, provider=provider)
                raise RuntimeError("Unexpected response model or provider")
        else:
            audit("wire_http_error", role=role, status=response.status_code)
        return response

    httpx.AsyncClient.send = send
    return original_send
