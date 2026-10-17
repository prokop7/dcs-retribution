"""Per-side commander state that is saved with the game."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class EnemySighting:
    """An enemy aircraft the side has seen, for example in a debriefing.

    Hook for the enemy order of battle: by default (design doc section 4) the enemy
    air order of battle is hidden, and only what a side has actually encountered is
    revealed through these sightings.
    """

    aircraft_type: str
    turn: int
    #: How it was seen, e.g. "shot_down" or "encountered".
    how: str
    #: Name of the base it came from, if known.
    origin: Optional[str] = None


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

    #: Changes that ride along with the next run without causing one, such as the
    #: opposing commander rebuilding a site while both are planning. Replanning on
    #: those would let two commanders trigger each other back and forth.
    deferred_changes: list[str] = field(default_factory=list)

    #: Fog-filtered summary of the last mission, delivered with the next turn run.
    pending_debrief: Optional[dict[str, Any]] = None

    #: Fog-filtered summary of the last mission, kept for get_debrief.
    last_debrief: Optional[dict[str, Any]] = None

    #: Enemy aircraft this side has encountered, revealed to its commander.
    sightings: list[EnemySighting] = field(default_factory=list)

    #: Digest of the system prompt the commander's session last received. Claude
    #: Code keeps a conversation's system prompt, so changed rules are sent in a
    #: run message instead.
    rules_digest: Optional[str] = None

    #: The turn the commander called finish_turn_planning for.
    finished_turn: int = -1

    #: Error message of the last failed run, for the UI.
    last_error: Optional[str] = None

    def __setstate__(self, state: dict[str, Any]) -> None:
        # Fields added after a save was made take their defaults.
        new_state = CommanderState().__dict__
        new_state.update(state)
        self.__dict__.update(new_state)

    def record_debriefing(
        self, summary: dict[str, Any], sightings: list[EnemySighting]
    ) -> None:
        self.pending_debrief = summary
        self.last_debrief = summary
        known = set(self.sightings)
        self.sightings.extend(s for s in sightings if s not in known)
        self.needs_planning = True

    def record_change(self, change: str, replan: bool = True) -> None:
        if not replan:
            if change not in self.deferred_changes:
                self.deferred_changes.append(change)
            return
        if change not in self.pending_changes:
            self.pending_changes.append(change)
        self.needs_planning = True
