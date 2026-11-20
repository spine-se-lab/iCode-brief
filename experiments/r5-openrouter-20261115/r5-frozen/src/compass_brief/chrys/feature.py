# Copyright (c) 2026 Chrys. All rights reserved.

"""Declare the independent Compass Brief feature."""

from pathlib import Path

from chrys.service.features import FeatureDefinition

from compass_brief.chrys.tools import (
    inspect_compass_brief,
    maintain_compass_brief,
    revise_compass_brief,
    start_compass_brief,
)


def _tools(session_dir: Path) -> list:
    return [inspect_compass_brief, start_compass_brief, maintain_compass_brief, revise_compass_brief]


def feature_definition() -> FeatureDefinition:
    return FeatureDefinition(
        name="compass_brief",
        tool_category="compass_brief",
        tool_factory=_tools,
        engine_adapter="compass_brief.chrys.engine:CompassBriefFeature",
        tui_adapter="compass_brief.chrys.tui.feature:ClaimLayerTuiFeature",
        session_state_keys=("compass_brief",),
    )
