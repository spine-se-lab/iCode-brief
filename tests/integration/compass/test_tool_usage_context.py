"""The native model sees concise tool guidance before making its first tool call."""

import pytest

from chrys.service.llm.mock import MockResponse
from tests.integration.compass.support import host_for, read


@pytest.mark.asyncio
async def test_guidance_precedes_tools_without_repeated_context_or_side_calls(tmp_path, monkeypatch):
    host, client, probe, reads, _ = await host_for(
        tmp_path,
        monkeypatch,
        [read(), MockResponse(text="done"), read(), MockResponse(text="continued")],
    )
    try:
        assert (await host.run_until_final("Explain this code", timeout=10)).text == "done"
        first = "\n".join(message.text for message in client.call_history[0][0])
        marker = '<extension_context extension="compass" name="compass_tools">'
        assert first.count(marker) == 1
        guidance = first.split(marker, 1)[1].split("</extension_context>", 1)[0]
        assert guidance.count(".") == 3
        assert all(
            name in guidance
            for name in (
                "inspect_compass_brief(refs)",
                "update_compass_requirements()",
                "update_compass_brief()",
            )
        )
        assert "after repository exploration" in guidance
        assert "trigger maintenance when due" in guidance
        assert (await host.run_until_final("Explain further", timeout=10)).text == "continued"
        latest = "\n".join(message.text for message in client.call_history[-1][0])
        assert latest.count(marker) == 1  # native context catalog reuses unchanged guidance
        assert reads == ["a.py", "a.py"]
        assert not probe.prompts  # guidance/capture alone makes no maintenance call
    finally:
        await host.shutdown()
