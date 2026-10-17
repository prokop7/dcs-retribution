from __future__ import annotations

from typing import Any

from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.mcp.tools import NoArguments


@commander_tools.register(
    "get_situation",
    "Overview of the campaign for your side: turn, date and start time, time of "
    "day, weather (clouds, fog, wind at surface/6500 ft/26000 ft, QNH, "
    "temperature), budget and income per turn, victory state, your faction and "
    "doctrine, the enemy faction, base/front line counts, and your_domains: the "
    "decisions you own (a BLUE commander shares the side with the player, who "
    "keeps the domains not listed). Units: nm, ft, kt, true degrees; wind "
    "direction is where it blows from.",
    read_only=True,
)
def get_situation(context: CommanderContext, _args: NoArguments) -> dict[str, Any]:
    return context.view().situation()


@commander_tools.register(
    "get_debrief",
    "After-action summary of the last mission flown, as your side knows it: your "
    "losses (aircraft by type, base and task; ground units; sites; runways), enemy "
    "losses you can confirm, bases that changed hands, the results of each of your "
    "packages, and enemy aircraft types encountered.",
    read_only=True,
)
def get_debrief(context: CommanderContext, _args: NoArguments) -> dict[str, Any]:
    debrief = context.game.coalition_for(context.player).commander.last_debrief
    if debrief is None:
        return {"debrief": None, "note": "No mission has been flown yet."}
    return {"debrief": debrief}
