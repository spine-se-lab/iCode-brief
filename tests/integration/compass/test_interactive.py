"""New interactive Compass cases only; native hosts and widgets, offline models."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chrys.app.tui.theme import TuiVariableDefaultsMixin
from chrys.kernel import ChatResponse, Message
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.agents.serializer import save_profile
from chrys.service.profiles.models.schema import ModelProfile
from compass.adapters.icode.interactive_model import SelectedModel
from compass.ports import ModelReply
from tests.support.tui_helpers import WidgetApp


class CompassWidgetApp(TuiVariableDefaultsMixin, WidgetApp):
    pass


async def maintenance(self, *, system_prompt, user_prompt, response_schema, max_output_tokens, timeout_seconds):
    if response_schema["title"] == "IssueRequirementExtraction":
        data = {
            "requirements": [{"id": "R1", "text": "Return an empty list for empty input."}],
            "constraints": [],
            "signature_changes": [],
        }
    else:
        data = {k: [] for k in ("new_claims", "augment", "merge", "conflicts", "contradiction_scan", "deactivate")}
    return ModelReply(json.dumps(data))


@pytest.mark.asyncio
async def test_compass_extension_roundtrip_and_off_preserves_other_metadata(tmp_path):
    from chrys.app.tui.screens.agents.panels.tools import ToolsConfigPanel
    from tests.app.tui.screens.test_agent_config_extensions import _switches
    from tests.support.waiting import wait_for

    profile = AgentProfile(name="Mine", metadata={"other": "keep"})
    app = CompassWidgetApp(lambda: ToolsConfigPanel(profile.tools, extensions=profile.extensions))
    async with app.run_test(size=(100, 60)) as pilot:
        await wait_for(lambda: "compass" in _switches(app), pilot=pilot, timeout=10, description="Compass extension")
        toggle = _switches(app)["compass"]
        assert toggle.value is False
        toggle.value = True
        profile.extensions = app.query_one(ToolsConfigPanel).get_config().extensions
        path = save_profile(profile, target_dir=tmp_path)
        restored = load_profile_from_yaml(path)
        assert restored.metadata == {"other": "keep"}
        assert restored.extensions == ["compass"]
        toggle.value = False
        assert app.query_one(ToolsConfigPanel).get_config().extensions == []


@pytest.mark.asyncio
async def test_maintenance_uses_selected_connection_and_preserves_profile(monkeypatch):
    from compass.adapters.icode import interactive_model

    profile = ModelProfile(
        id="selected",
        name="UI choice",
        provider="deepseek-openai",
        model_id="private-alias",
        base_url="https://openrouter.ai/api/v1",
        api_key="{{TEST_MODEL_KEY}}",
        chat_options=json.dumps({"extra_body": {"provider": {"only": ["deepseek"]}, "reasoning": {"effort": "max"}}}),
    )
    original = deepcopy(profile)
    client = SimpleNamespace(
        get_response=AsyncMock(
            return_value=ChatResponse(
                messages=[Message("assistant", ['{"ok":true}'])],
                finish_reason="stop",
                usage_details={"input_token_count": 4},
            )
        ),
        aclose=AsyncMock(),
    )
    create = AsyncMock(return_value=client)
    monkeypatch.setattr(interactive_model, "create_client", create)
    usage = []
    model = SelectedModel(profile, usage.append)
    try:
        response = await model.complete(
            system_prompt="s",
            user_prompt="u",
            response_schema={"type": "object"},
            max_output_tokens=200,
            timeout_seconds=5,
        )
        assert response.text == '{"ok":true}'
        assert create.await_args.args[0] == original
        options = client.get_response.await_args.kwargs["options"]
        assert options["extra_body"]["provider"] == {"only": ["deepseek"]}
        assert options["extra_body"]["reasoning"] == {"enabled": False}
        assert options["max_tokens"] == 200 and options["temperature"] == 0
        assert profile == original
        assert usage == [{"input_token_count": 4}]
    finally:
        await model.aclose()
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_full_agent_settings_save_button_persists_compass(monkeypatch):
    from textual.widgets import Button

    from chrys.foundation.platform import get_platform
    from tests.app.tui.screens import _agent_config_support as support
    from tests.app.tui.screens._agent_config_support import (
        _activate_agent_config_tab,
        _wait_for_hydrated,
        make_profile,
        open_agent_config,
        registry_with,
    )
    from tests.app.tui.screens.test_agent_config_extensions import _switches
    from tests.support.tui_helpers import PlaceholderHostApp
    from tests.support.waiting import wait_for

    class ConfigHost(TuiVariableDefaultsMixin, PlaceholderHostApp):
        pass

    monkeypatch.setattr(support, "PlaceholderHostApp", ConfigHost)
    registry = registry_with(make_profile("Mine", display_name="Mine", description="Coding"))
    async with open_agent_config(registry, current_profile="Mine") as (screen, pilot):
        await _activate_agent_config_tab(screen, pilot, "tools")
        await wait_for(lambda: "compass" in _switches(screen), pilot=pilot, timeout=10, description="Compass extension")
        _switches(screen)["compass"].value = True
        save = screen.query_one("#ac-save", Button)
        await wait_for(lambda: not save.disabled, pilot=pilot, timeout=10, description="Compass enables Save")
        save.press()
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)
    assert registry.get("Mine").extensions == ["compass"]
    saved = load_profile_from_yaml(get_platform().config_dir / "agents" / "Mine.yaml")
    assert saved.extensions == ["compass"]
    assert "compass_enabled" not in saved.metadata
