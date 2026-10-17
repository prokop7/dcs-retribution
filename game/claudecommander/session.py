"""Decides whether a commander run resumes its Claude session or starts anew.

Each side of a campaign keeps one long-lived Claude Code session. The save file
records which session it belongs to, but not how far that session has progressed:
an older save of the same campaign still points at the same session even though
the session has since played later turns. The work folder therefore keeps a
registry of each session's last synced turn, and that is what rewinds are
detected against.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from game.claudecommander.process import ClaudeRunResult


class SessionAction(Enum):
    RESUME = "resume"
    #: Start a new session seeded with the journal up to the current turn.
    NEW = "new"


class NewSessionReason(Enum):
    FIRST_RUN = "first run for this campaign"
    REWIND = "an older save was loaded"
    LOST = "the previous session could not be found"


@dataclass(frozen=True)
class SessionDecision:
    action: SessionAction
    session_id: str
    reason: Optional[NewSessionReason] = None


class SessionRegistry:
    """Last synced turn of every session, stored in the commander work folder."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logging.exception("Could not read %s", self.path)
            return {}
        return {str(k): int(v) for k, v in data.items()}

    def last_synced_turn(self, session_id: str) -> Optional[int]:
        return self._load().get(session_id)

    def mark_synced(self, session_id: str, turn: int) -> None:
        data = self._load()
        data[session_id] = turn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def decide_session(
    saved_session_id: Optional[str], registry: SessionRegistry, game_turn: int
) -> SessionDecision:
    if saved_session_id is None:
        return SessionDecision(
            SessionAction.NEW, str(uuid.uuid4()), NewSessionReason.FIRST_RUN
        )
    synced = registry.last_synced_turn(saved_session_id)
    if synced is None:
        return SessionDecision(
            SessionAction.NEW, str(uuid.uuid4()), NewSessionReason.LOST
        )
    if synced > game_turn:
        return SessionDecision(
            SessionAction.NEW, str(uuid.uuid4()), NewSessionReason.REWIND
        )
    return SessionDecision(SessionAction.RESUME, saved_session_id)


def is_missing_session_error(result: ClaudeRunResult) -> bool:
    text = " ".join([result.error or "", *result.stderr_tail]).lower()
    return "no conversation found" in text
