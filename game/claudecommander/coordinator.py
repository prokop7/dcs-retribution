"""Decides when commanders plan and applies the results to the game.

The coordinator is process-wide and not saved. The UI attaches it to the loaded
game; the game model asks it for planning through :meth:`request` whenever a turn
starts or a mid-turn change requires re-planning. Requests made while no UI is
attached (for example during campaign generation) are kept in the saved
``CommanderState`` and start once a UI attaches.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, Protocol, TYPE_CHECKING

from game.claudecommander.config import ClaudeRunConfig
from game.claudecommander.context import GAME_LOCK
from game.claudecommander.events import OtherEvent, StreamEvent
from game.claudecommander.journal import CommanderJournal
from game.claudecommander.knowledge import CommanderKnowledge, USER_KNOWLEDGE_NAME
from game.claudecommander.planning import PlanningJob, PlanningStatus, SideRun
from game.claudecommander.process import ClaudeRunResult
from game.claudecommander.prompts import (
    RunBrief,
    RunKind,
    rules_digest,
    system_prompt,
)
from game.claudecommander.runner import CommanderPaths, CommanderRunner
from game.theater.player import Player

if TYPE_CHECKING:
    from game import Game
    from game.coalition import Coalition


class CommanderListener(Protocol):
    """UI hooks. Called from worker threads; implementations must marshal to the UI
    thread themselves."""

    def on_planning_started(self, player: Player, kind: RunKind) -> None: ...

    def on_event(self, player: Player, event: StreamEvent | OtherEvent) -> None: ...

    def on_planning_finished(
        self, player: Player, status: PlanningStatus, result: ClaudeRunResult
    ) -> None: ...


def commander_enabled(game: Game, player: Player) -> bool:
    if player.is_red:
        return game.settings.claude_commander_red
    if player.is_blue:
        return game.settings.claude_commander_blue
    return False


def directive_for(game: Game, player: Player) -> str:
    if player.is_red:
        return game.settings.claude_commander_red_directive
    return game.settings.claude_commander_blue_directive


def run_config(game: Game) -> ClaudeRunConfig:
    from game.persistency import server_port
    from game.server.settings import ServerSettings

    settings = game.settings
    return ClaudeRunConfig(
        executable=settings.claude_commander_executable,
        model=settings.claude_commander_model,
        effort=settings.claude_commander_effort,
        timeout=timedelta(minutes=settings.claude_commander_timeout_minutes),
        server_port=server_port(),
        server_host=ServerSettings.get().server_bind_address,
    )


def commander_paths(game: Game, player: Player) -> CommanderPaths:
    """The work folder of a side's commander in this campaign."""
    from game.persistency import commander_dir

    return CommanderPaths.for_side(commander_dir(), game.campaign_id, player)


def journal_heading(game: Game, player: Player) -> str:
    return (
        f"{player.value} commander journal — "
        f"{game.campaign_name or 'campaign'} ({game.campaign_id})"
    )


def commander_knowledge() -> CommanderKnowledge:
    """General lessons shared by every commander in every campaign."""
    from game.persistency import commander_dir

    return CommanderKnowledge(commander_dir() / USER_KNOWLEDGE_NAME)


def commander_journal(game: Game, player: Player) -> CommanderJournal:
    return CommanderJournal(
        commander_paths(game, player).journal, journal_heading(game, player)
    )


def assign_missing_tots(coalition: Coalition, now: datetime) -> None:
    """Gives packages the commander didn't time an ASAP time over target.

    Packages without a TOT keep ``datetime.min``, which breaks flight plan timing.
    """
    from game.ato.traveltime import TotEstimator

    for package in coalition.ato.packages:
        if package.time_over_target == datetime.min and package.flights:
            package.time_over_target = TotEstimator(package).earliest_tot(now)


def _debrief_markdown(debrief: dict[str, object]) -> str:
    return "```json\n" + json.dumps(debrief, ensure_ascii=False, indent=1) + "\n```"


class _ActiveRun:
    def __init__(self, job: PlanningJob, kind: RunKind, changes: list[str]) -> None:
        self.job = job
        self.kind = kind
        self.changes = changes


class CommanderCoordinator:
    _instance: Optional[CommanderCoordinator] = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.game: Optional[Game] = None
        self.listener: Optional[CommanderListener] = None
        self._runs: dict[Player, _ActiveRun] = {}
        #: Sides that got a new request while planning; they run again afterwards.
        self._rerun: set[Player] = set()

    @classmethod
    def get(cls) -> CommanderCoordinator:
        if cls._instance is None:
            cls._instance = CommanderCoordinator()
        return cls._instance

    # Attachment.

    def attach(self, game: Game, listener: CommanderListener) -> None:
        """Binds the coordinator to the game shown in the UI and starts pending
        planning."""
        with self._lock:
            if self.game is not game:
                self.cancel_all()
            self.game = game
            self.listener = listener
            for player in (Player.RED, Player.BLUE):
                coalition = game.coalition_for(player)
                if (
                    commander_enabled(game, player)
                    and coalition.commander.needs_planning
                ):
                    self._start(player)

    def detach(self) -> None:
        with self._lock:
            self.cancel_all()
            self.game = None
            self.listener = None

    # Queries.

    def is_planning(self, player: Player) -> bool:
        with self._lock:
            return player in self._runs

    @property
    def any_planning(self) -> bool:
        return self.is_planning(Player.RED) or self.is_planning(Player.BLUE)

    # Requests.

    def request(self, game: Game, player: Player, change: Optional[str] = None) -> None:
        """Asks for a planning run for ``player``.

        Starts a full turn run if the commander hasn't planned this turn yet, or an
        incremental run otherwise. If a run is already in progress the request is
        picked up when it finishes.
        """
        state = game.coalition_for(player).commander
        if change is not None:
            state.record_change(change)
        state.needs_planning = True
        with self._lock:
            if game is self.game and self.listener is not None:
                if player in self._runs:
                    self._rerun.add(player)
                else:
                    self._start(player)

    def retry(self, player: Player) -> None:
        with self._lock:
            if self.game is not None:
                self.game.coalition_for(player).commander.needs_planning = True
                self._start(player)

    def cancel(self, player: Player) -> None:
        with self._lock:
            run = self._runs.get(player)
        if run is not None:
            run.job.cancel()

    def cancel_all(self) -> None:
        for player in list(self._runs):
            self.cancel(player)

    def use_builtin_planner(self, player: Player) -> None:
        """Plans this turn for ``player`` with the built-in planner instead.

        Must be called from the thread that owns the game (the UI thread).
        """
        from game.sim import GameUpdateEvents
        from game.server import EventStream

        with self._lock:
            game = self.game
        if game is None:
            return
        events = GameUpdateEvents()
        with GAME_LOCK:
            coalition = game.coalition_for(player)
            coalition.run_builtin_planning(events)
            state = coalition.commander
            state.needs_planning = False
            state.last_error = None
        EventStream.put_nowait(events)

    # Running.

    def _runner(self, game: Game, player: Player) -> CommanderRunner:
        return CommanderRunner(
            player,
            commander_paths(game, player),
            run_config(game),
            system_prompt(player.value),
            journal_heading(game, player),
        )

    def _start(self, player: Player) -> None:
        from game.claudecommander.tools.ato import locked_package_descriptions

        game = self.game
        assert game is not None
        if self.is_planning(player) or not commander_enabled(game, player):
            return

        with GAME_LOCK:
            state = game.coalition_for(player).commander
            if not state.needs_planning:
                return
            kind = (
                RunKind.TURN if state.planned_turn != game.turn else RunKind.INCREMENTAL
            )
            # Changes and needs_planning stay in the saved state until the run
            # succeeds, so a game saved or closed mid-run plans again on load.
            changes = list(state.pending_changes) + [
                c for c in state.deferred_changes if c not in state.pending_changes
            ]
            brief = RunBrief(
                kind=kind,
                turn=game.turn,
                directive=directive_for(game, player),
                changes=changes,
                debrief=state.pending_debrief if kind is RunKind.TURN else None,
                is_first_turn=kind is RunKind.TURN and state.planned_turn < 0,
                locked_packages=locked_package_descriptions(game.coalition_for(player)),
                knowledge=(
                    commander_knowledge().render() if kind is RunKind.TURN else ""
                ),
                rules_update=(
                    system_prompt(player.value)
                    if state.session_id is not None
                    and state.rules_digest != rules_digest(player.value)
                    else ""
                ),
            )
            session_id = state.session_id

        runner = self._runner(game, player)
        if brief.debrief is not None:
            runner.journal.append(
                game.turn,
                "Debrief of the last mission",
                _debrief_markdown(brief.debrief),
            )
        job = PlanningJob(
            [SideRun(player, runner, session_id, game.turn, brief)],
            _JobListener(self, game, kind, changes),
        )
        self._runs[player] = _ActiveRun(job, kind, changes)
        if self.listener is not None:
            self.listener.on_planning_started(player, kind)
        job.start()

    def _finished(
        self,
        game: Game,
        player: Player,
        kind: RunKind,
        changes: list[str],
        status: PlanningStatus,
        result: ClaudeRunResult,
    ) -> None:
        with self._lock:
            self._runs.pop(player, None)
            rerun = player in self._rerun
            self._rerun.discard(player)
        with GAME_LOCK:
            coalition = game.coalition_for(player)
            state = coalition.commander
            if result.session_id is not None and result.success:
                state.session_id = result.session_id
                state.rules_digest = rules_digest(player.value)
            if status is PlanningStatus.SUCCEEDED:
                if kind is RunKind.TURN:
                    state.planned_turn = game.turn
                    state.pending_debrief = None
                state.last_error = None
                coalition.replan_ground_war()
                state.pending_changes = [
                    c for c in state.pending_changes if c not in changes
                ]
                state.deferred_changes = [
                    c for c in state.deferred_changes if c not in changes
                ]
                state.needs_planning = rerun or bool(state.pending_changes)
                assign_missing_tots(coalition, game.conditions.start_time)
                if result.final_text.strip():
                    self._runner(game, player).journal.append(
                        game.turn,
                        f"Planning summary ({kind.value})",
                        result.final_text,
                    )
            else:
                # The changes are still pending, so a retry includes them.
                state.needs_planning = True
                state.last_error = result.error
        logging.info(
            "%s commander %s run finished: %s %s",
            player.value,
            kind.value,
            status.value,
            result.error or "",
        )

        with self._lock:
            listener = self.listener if game is self.game else None
        if listener is not None:
            listener.on_planning_finished(player, status, result)

        if status is PlanningStatus.SUCCEEDED and state.needs_planning:
            # Changes arrived while planning; tell the commander about them.
            with self._lock:
                if game is self.game and self.listener is not None:
                    self._start(player)


class _JobListener:
    def __init__(
        self,
        coordinator: CommanderCoordinator,
        game: Game,
        kind: RunKind,
        changes: list[str],
    ) -> None:
        self.coordinator = coordinator
        self.game = game
        self.kind = kind
        self.changes = changes

    def on_event(self, player: Player, event: StreamEvent | OtherEvent) -> None:
        listener = self.coordinator.listener
        if listener is not None and self.coordinator.game is self.game:
            listener.on_event(player, event)

    def on_finished(
        self, player: Player, status: PlanningStatus, result: ClaudeRunResult
    ) -> None:
        self.coordinator._finished(
            self.game, player, self.kind, self.changes, status, result
        )
