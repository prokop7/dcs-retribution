from __future__ import annotations

from typing import Any

from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.mcp.tools import NoArguments


@commander_tools.register(
    "get_situation",
    "Current turn, date and time, weather and your budget.",
    read_only=True,
)
def get_situation(context: CommanderContext, _args: NoArguments) -> dict[str, Any]:
    game = context.game
    coalition = game.coalition_for(context.player)
    conditions = game.conditions
    return {
        "side": context.player.value,
        "turn": game.turn,
        "start_time": conditions.start_time.isoformat(),
        "time_of_day": conditions.time_of_day.value,
        "budget": round(coalition.budget, 1),
        "faction": coalition.faction.name,
    }
