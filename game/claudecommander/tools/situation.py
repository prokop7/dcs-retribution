from __future__ import annotations

from typing import Any

from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.mcp.tools import NoArguments


@commander_tools.register(
    "get_situation",
    "Overview of the campaign for your side: turn, date and start time, time of "
    "day, weather (clouds, fog, wind at surface/6500 ft/26000 ft, QNH, "
    "temperature), budget and income per turn, victory state, your faction and "
    "doctrine, the enemy faction, and base/front line counts. Units: nm, ft, kt, "
    "true degrees; wind direction is where it blows from.",
    read_only=True,
)
def get_situation(context: CommanderContext, _args: NoArguments) -> dict[str, Any]:
    return context.view().situation()
