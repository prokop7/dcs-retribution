"""Runs one commander planning pass for one side."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from game.claudecommander.config import ClaudeRunConfig
from game.claudecommander.journal import CommanderJournal
from game.claudecommander.process import (
    ClaudeRun,
    ClaudeRunResult,
    EventCallback,
    SessionArgs,
    build_command,
    resolve_executable,
    write_mcp_config,
)
from game.claudecommander.session import (
    NewSessionReason,
    SessionAction,
    SessionDecision,
    SessionRegistry,
    decide_session,
    is_missing_session_error,
)
from game.claudecommander.mcp.auth import CommanderTokens
from game.theater.player import Player


@dataclass(frozen=True)
class CommanderPaths:
    """Per campaign and side work folder.

    Claude Code stores sessions per working directory, so a side must always run
    from the same folder for ``--resume`` to find its session.
    """

    root: Path

    @staticmethod
    def for_side(base: Path, campaign_id: str, player: Player) -> CommanderPaths:
        return CommanderPaths(base / campaign_id / player.value.lower())

    @property
    def journal(self) -> Path:
        return self.root / "journal.md"

    @property
    def sessions(self) -> Path:
        return self.root / "sessions.json"

    @property
    def mcp_config(self) -> Path:
        return self.root / "mcp.json"

    @property
    def system_prompt(self) -> Path:
        return self.root / "system-prompt.md"


def seed_message(
    reason: NewSessionReason, journal_text: str, game_turn: int, message: str
) -> str:
    """Wraps a turn message with the journal when a session starts fresh."""
    if not journal_text.strip():
        return message
    return (
        f'<session_restore reason="{reason.value}">\n'
        "You are continuing a campaign you have commanded before, but your earlier "
        "conversation is unavailable. Below is your own journal up to turn "
        f"{game_turn}. Treat it as your memory; nothing after turn {game_turn} has "
        "happened.\n\n"
        f"{journal_text}\n"
        "</session_restore>\n\n"
        f"{message}"
    )


class CommanderRunner:
    def __init__(
        self,
        player: Player,
        paths: CommanderPaths,
        config: ClaudeRunConfig,
        system_prompt: str,
        journal_heading: str,
    ) -> None:
        self.player = player
        self.paths = paths
        self.config = config
        self.system_prompt = system_prompt
        self.journal = CommanderJournal(paths.journal, journal_heading)
        self.registry = SessionRegistry(paths.sessions)
        self._active: Optional[ClaudeRun] = None
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        if self._active is not None:
            self._active.cancel()

    def run(
        self,
        saved_session_id: Optional[str],
        game_turn: int,
        message: str,
        on_event: Optional[EventCallback] = None,
    ) -> ClaudeRunResult:
        """Runs a planning pass and returns its result.

        The caller stores ``result.session_id`` in the save once this returns.
        """
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.paths.system_prompt.write_text(self.system_prompt, encoding="utf-8")
        executable = resolve_executable(self.config.executable)

        decision = decide_session(saved_session_id, self.registry, game_turn)
        result = self._run_once(executable, decision, game_turn, message, on_event)
        if (
            decision.action is SessionAction.RESUME
            and not self._cancelled
            and is_missing_session_error(result)
        ):
            logging.warning(
                "%s commander session %s is gone, starting a new one",
                self.player.value,
                decision.session_id,
            )
            decision = decide_session(None, self.registry, game_turn)
            decision = SessionDecision(
                SessionAction.NEW, decision.session_id, NewSessionReason.LOST
            )
            result = self._run_once(executable, decision, game_turn, message, on_event)

        if result.success:
            result.session_id = result.session_id or decision.session_id
            self.registry.mark_synced(result.session_id, game_turn)
        return result

    def _run_once(
        self,
        executable: str,
        decision: SessionDecision,
        game_turn: int,
        message: str,
        on_event: Optional[EventCallback],
    ) -> ClaudeRunResult:
        if decision.action is SessionAction.NEW:
            assert decision.reason is not None
            if decision.reason is NewSessionReason.REWIND:
                archive = self.journal.rewind_to(game_turn)
                if archive is not None:
                    logging.info("Archived abandoned journal timeline to %s", archive)
            message = seed_message(
                decision.reason, self.journal.render(game_turn), game_turn, message
            )

        token = CommanderTokens.issue(self.player)
        try:
            write_mcp_config(
                self.paths.mcp_config, self.config.mcp_url(self.player.value), token
            )
            command = build_command(
                executable,
                self.config,
                SessionArgs(
                    decision.session_id, decision.action is SessionAction.RESUME
                ),
                self.paths.mcp_config,
                self.paths.system_prompt,
            )
            self._active = ClaudeRun(
                command,
                self.paths.root,
                message,
                self.config.timeout.total_seconds(),
                on_event,
            )
            if self._cancelled:
                return ClaudeRunResult(cancelled=True, error="Planning was cancelled.")
            return self._active.run()
        finally:
            self._active = None
            CommanderTokens.revoke(token)
            self.paths.mcp_config.unlink(missing_ok=True)
