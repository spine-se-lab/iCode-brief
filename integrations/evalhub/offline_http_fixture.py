"""Synthetic HTTP replies reused from the existing R5 offline smoke test."""

import asyncio
import json
from dataclasses import replace

import httpx
import r5_native as r

main_bodies, side_bodies = [], []
mode = "normal"


async def send(client, request, **kwargs):
    assert request.url.host == "api.deepseek.com", "Unexpected endpoint in offline test"
    body = json.loads(request.content)
    assert body["model"] == r.MODEL
    assert "provider" not in body
    if body.get("tools"):
        main_bodies.append(body)
        step = len(main_bodies)
        if mode in ("timeout", "recover"):
            r.controller.core._call_settings = replace(
                r.controller.core._call_settings, timeout_seconds=0.05, total_timeout_seconds=0.1
            )
        if mode == "timeout":
            assert step <= 2, "Main model continued after a terminal handoff failure"
        assert r.controller.core._brief_budget_chars == 15000
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "max"
        assert body.get("max_tokens") == 64000
        assert body.get("stream", False) is False
        names = {t["function"]["name"] for t in body["tools"]}
        print(
            json.dumps(
                {
                    "offline_debug_tools": sorted(names),
                    "auth_matches_synthetic": request.headers.get("authorization") == "Bearer offline-placeholder",
                }
            ),
            flush=True,
        )
        assert {"execute", "start_compass_brief", "inspect_compass_brief"} <= names
        assert names <= {
            "execute",
            "start_compass_brief",
            "inspect_compass_brief",
            "compress_context",
            "list_compressed_contexts",
            "recall_context",
        }
        assert request.headers["authorization"] == "Bearer offline-placeholder"
        calls = {
            1: ("execute", {"command": "cat fixture.py", "reason": "Inspect synthetic fixture"}),
            2: (
                "start_compass_brief",
                {"exploration_summary": "Read fixture.py: parse returns None; issue asks empty list."},
            ),
            3: (
                "execute",
                {
                    "command": "printf 'def parse(data):\\n    return []\\n' > fixture.py",
                    "reason": "Edit synthetic fixture",
                },
            ),
        }
        if mode == "automatic":
            calls[2] = (
                "execute",
                {
                    "command": "python -c \"open('unauthorized.txt','w').write('bad')\"",
                    "reason": "Try an interpreter write before handoff",
                },
            )
        if step in calls:
            name, args = calls[step]
            message = {
                "role": "assistant",
                "content": "",
                "reasoning_content": "Synthetic offline reasoning.",
                "tool_calls": [
                    {"id": f"call{step}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                ],
            }
            finish = "tool_calls"
        else:
            assert step in (4, 5), f"Unexpected repeated main call: {step}"
            message = {
                "role": "assistant",
                "content": "Finished synthetic offline task.",
                "reasoning_content": "Synthetic final reasoning.",
            }
            finish = "stop"
        if step == 3:
            assert any(r.controller.constants["SUBMISSION_RULES"] in str(m.get("content")) for m in body["messages"])
            assert any(m.get("reasoning_content") for m in body["messages"] if m["role"] == "assistant")
        if step == 5:
            assert any(r.controller.constants["SUBMISSION_CHECK"] in str(m.get("content")) for m in body["messages"])
    else:
        side_bodies.append(body)
        if mode == "timeout" or (mode == "recover" and len(side_bodies) == 1):
            await asyncio.sleep(10)
            raise AssertionError("The maintenance deadline did not cancel the request")
        assert body["thinking"] == {"type": "disabled"}
        system = body["messages"][0]["content"]
        if '"title": "IssueRequirementExtraction"' in system:
            content = {
                "requirements": [{"id": "R1", "text": "Return an empty list for empty input."}],
                "constraints": [],
                "signature_changes": [],
            }
        else:
            content = {
                k: [] for k in ("new_claims", "augment", "merge", "conflicts", "contradiction_scan", "deactivate")
            }
        message, finish = {"role": "assistant", "content": json.dumps(content)}, "stop"
    return httpx.Response(
        200,
        request=request,
        json={
            "id": f"synthetic-{len(main_bodies)}-{len(side_bodies)}",
            "object": "chat.completion",
            "created": 0,
            "model": r.MODEL,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "prompt_cache_hit_tokens": 50,
            },
        },
    )
