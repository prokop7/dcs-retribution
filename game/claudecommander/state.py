"""Per-side commander state that is saved with the game."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class CommanderState:
    """What a coalition's commander needs to survive save and load.

    The Claude session itself lives in Claude Code's own storage; this only records
    which session belongs to this side of this campaign and what the commander still
    has to be told.
    """

    #: The Claude Code session of this side, or None before the first run.
    session_id: Optional[str] = None

    #: The last turn a full planning run completed for.
    planned_turn: int = -1

    #: True while a planning run is needed. Saved, so a game saved or closed during
    #: planning resumes planning when loaded.
    needs_planning: bool = False

    #: Changes the commander hasn't been told about yet.
    pending_changes: list[str] = field(default_factory=list)

    #: Fog-filtered summary of the last mission, delivered with the next turn run.
    pending_debrief: Optional[dict[str, Any]] = None

    #: The turn the commander called finish_turn_planning for.
    finished_turn: int = -1

    #: Error message of the last failed run, for the UI.
    last_error: Optional[str] = None

    def __setstate__(self, state: dict[str, Any]) -> None:
        # Fields added after a save was made take their defaults.
        new_state = CommanderState().__dict__
        new_state.update(state)
        self.__dict__.update(new_state)

    def record_change(self, change: str) -> None:
        if change not in self.pending_changes:
            self.pending_changes.append(change)
        self.needs_planning = True
