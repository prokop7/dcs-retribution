"""Shared state for commander tool calls."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Sequence, TYPE_CHECKING

from game.claudecommander.mcp.tools import ToolRegistry

if TYPE_CHECKING:
    from game import Game
    from game.claudecommander.view import CommanderView, EnemySighting
    from game.sim import GameUpdateEvents
    from game.theater.player import Player

#: Serializes every commander tool call. RED and BLUE plan concurrently, and the
#: MCP endpoint runs handlers on the server's thread pool, so game mutations must
#: never interleave.
GAME_LOCK = threading.RLock()


@dataclass
class CommanderContext:
    """What a tool handler gets: the game, the side it acts for, and an event sink.

    Handlers must only expose information that side is allowed to know. Read the
    game through ``view()``, never directly. See the fog of war section of
    docs/dev/design/claude-commander.md.
    """

    game: Game
    player: Player
    events: GameUpdateEvents
    #: Enemy aircraft this side has encountered (e.g. from debriefings).
    sightings: Sequence[EnemySighting] = field(default_factory=tuple)

    def view(self) -> CommanderView:
        from game.claudecommander.view import CommanderView

        return CommanderView(self.game, self.player, self.sightings)


commander_tools: ToolRegistry[CommanderContext] = ToolRegistry()
