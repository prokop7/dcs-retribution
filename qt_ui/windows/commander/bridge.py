"""Connects the commander coordinator to the Qt UI.

The coordinator calls its listener from worker threads. ``CommanderBridge`` is that
listener: every callback is re-emitted through a queued Qt signal, so all UI work
happens on the UI thread. The bridge also keeps an in-memory log of each side's
runs for the log window, and throttles the live refresh of Qt views while a
commander adds packages.
"""

from __future__ import annotations

import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, Protocol, TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot

from game.server import EventStream
from game.sim import GameUpdateEvents
from game.claudecommander.coordinator import CommanderCoordinator, commander_enabled
from game.claudecommander.events import (
    OtherEvent,
    StreamEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from game.claudecommander.planning import PlanningStatus
from game.claudecommander.process import ClaudeRunResult
from game.claudecommander.prompts import RunKind
from game.theater.player import Player
from qt_ui.windows.commander.logformat import LogKind, LogLine, format_event

if TYPE_CHECKING:
    from game import Game

COMMANDED_SIDES = (Player.BLUE, Player.RED)

TAKE_COMMAND_CHANGE = "You have taken command of this side."
REPLAN_CHANGE = "The player asked you to review and re-plan the current turn."

#: Most recent log lines kept per side.
_MAX_LOG_LINES = 5000


class Coordinator(Protocol):
    """The part of ``CommanderCoordinator`` the UI uses."""

    def attach(self, game: Game, listener: CommanderBridge) -> None: ...

    def detach(self) -> None: ...

    def request(
        self, game: Game, player: Player, change: Optional[str] = None
    ) -> None: ...

    def retry(self, player: Player) -> None: ...

    def cancel(self, player: Player) -> None: ...

    def use_builtin_planner(self, player: Player) -> None: ...

    def is_planning(self, player: Player) -> bool: ...

    @property
    def any_planning(self) -> bool: ...


def side_label(player: Player) -> str:
    return "OWNFOR" if player.is_blue else "OPFOR"


@dataclass
class SideLog:
    """What the UI knows about one side's commander in this app session."""

    lines: list[LogLine] = field(default_factory=list)
    tool_names: dict[str, str] = field(default_factory=dict)
    planning: bool = False
    kind: Optional[RunKind] = None
    started_at: Optional[float] = None
    tool_calls: int = 0
    last_status: Optional[PlanningStatus] = None
    last_result: Optional[ClaudeRunResult] = None

    def add(self, line: LogLine) -> None:
        self.lines.append(line)
        if len(self.lines) > _MAX_LOG_LINES:
            del self.lines[: len(self.lines) - _MAX_LOG_LINES]


class CommanderBridge(QObject):
    """Qt facade over the commander coordinator. Lives on the UI thread."""

    #: (Player, RunKind)
    planning_started = Signal(object, object)
    #: (Player, LogLine) for every line added to a side's log.
    line_added = Signal(object, object)
    #: (Player, PlanningStatus, ClaudeRunResult). Not emitted for runs the UI
    #: cancelled itself, e.g. because the commander was switched off.
    planning_finished = Signal(object, object, object)
    #: Something shown by the status widgets changed.
    state_changed = Signal()
    #: (Player) A planning side changed the game; throttled.
    live_refresh_requested = Signal(object)
    #: The game changed in ways that need a full refresh of the Qt views.
    refresh_requested = Signal()
    #: The bridge was attached to another game or detached.
    game_changed = Signal()

    # Internal: re-emitted on the UI thread. The first argument is the attachment
    # generation, so callbacks of a previous game are dropped.
    _started = Signal(int, object, object, float)
    _event = Signal(int, object, object)
    _finished = Signal(int, object, object, object)

    def __init__(
        self,
        coordinator: Optional[Coordinator] = None,
        parent: Optional[QObject] = None,
        refresh_interval_ms: int = 1000,
    ) -> None:
        super().__init__(parent)
        self.coordinator: Coordinator = (
            coordinator if coordinator is not None else CommanderCoordinator.get()
        )
        self._game: Optional[Game] = None
        self._generation = 0
        self._logs: dict[Player, SideLog] = {p: SideLog() for p in COMMANDED_SIDES}
        self._silent_cancels: set[Player] = set()

        self.refresh_interval_ms = refresh_interval_ms
        self._last_live_refresh = 0.0
        self._dirty: set[Player] = set()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._flush_live_refresh)

        queued = Qt.ConnectionType.QueuedConnection
        self._started.connect(self._on_started, queued)
        self._event.connect(self._on_event, queued)
        self._finished.connect(self._on_finished, queued)

    # Attachment.

    @property
    def game(self) -> Optional[Game]:
        return self._game

    def set_game(self, game: Optional[Game]) -> None:
        """Follows the game shown by the UI. Cheap to call for the same game."""
        if game is self._game:
            return
        if game is None:
            self.detach()
        else:
            self.attach(game)

    def attach(self, game: Game) -> None:
        self._generation += 1
        self._game = game
        self._reset()
        self.game_changed.emit()
        self.coordinator.attach(game, self)
        self.state_changed.emit()

    def detach(self) -> None:
        """Cancels running commanders and forgets the game."""
        self._generation += 1
        self._game = None
        self._reset()
        self.coordinator.detach()
        self.game_changed.emit()
        self.state_changed.emit()

    def _reset(self) -> None:
        self._logs = {p: SideLog() for p in COMMANDED_SIDES}
        self._silent_cancels.clear()
        self._dirty.clear()
        self._refresh_timer.stop()

    # Queries.

    def log(self, player: Player) -> SideLog:
        return self._logs[player]

    def is_planning(self, player: Player) -> bool:
        return self.coordinator.is_planning(player)

    @property
    def any_planning(self) -> bool:
        return self.coordinator.any_planning

    def is_enabled(self, player: Player) -> bool:
        return self._game is not None and commander_enabled(self._game, player)

    def elapsed_seconds(self, player: Player) -> Optional[float]:
        started = self._logs[player].started_at
        if started is None or not self._logs[player].planning:
            return None
        return max(0.0, time.monotonic() - started)

    def reveals(self, player: Player) -> bool:
        """True if the player may see this side's log, journal and plan details."""
        if player.is_blue:
            return True
        return self._game is not None and bool(
            self._game.settings.claude_commander_reveal_red
        )

    # Commands. All must be called on the UI thread.

    def replan(self, player: Player) -> None:
        if self._game is not None:
            self.coordinator.request(self._game, player, REPLAN_CHANGE)
            self.state_changed.emit()

    def cancel(self, player: Player) -> None:
        self.coordinator.cancel(player)

    def retry(self, player: Player) -> None:
        self.coordinator.retry(player)
        self.state_changed.emit()

    def use_builtin_planner(self, player: Player) -> None:
        self.coordinator.use_builtin_planner(player)
        self._add_marker(player, "Planned this turn with the built-in planner.")
        self.refresh_requested.emit()
        self.state_changed.emit()

    def enabled_sides(self) -> dict[Player, bool]:
        return {p: self.is_enabled(p) for p in COMMANDED_SIDES}

    def apply_settings_change(self, before: dict[Player, bool]) -> None:
        """Reacts to commanders being switched on or off in the settings.

        ``before`` is :meth:`enabled_sides` from before the settings were changed.
        """
        if self._game is None:
            return
        for player in COMMANDED_SIDES:
            enabled = self.is_enabled(player)
            if enabled and not before.get(player, False):
                # The built-in planner already planned this turn; undo it first.
                events = GameUpdateEvents()
                self._game.coalition_for(player).hand_over_to_claude(events)
                EventStream.put_nowait(events)
                self.coordinator.request(self._game, player, TAKE_COMMAND_CHANGE)
            elif not enabled and self.coordinator.is_planning(player):
                self._silent_cancels.add(player)
                self.coordinator.cancel(player)
        self.state_changed.emit()

    # CommanderListener. Called from worker threads (and sometimes the UI thread);
    # only emits signals.

    def on_planning_started(self, player: Player, kind: RunKind) -> None:
        self._started.emit(self._generation, player, kind, time.monotonic())

    def on_event(self, player: Player, event: StreamEvent | OtherEvent) -> None:
        self._event.emit(self._generation, player, event)

    def on_planning_finished(
        self, player: Player, status: PlanningStatus, result: ClaudeRunResult
    ) -> None:
        self._finished.emit(self._generation, player, status, result)

    # UI thread slots.

    @Slot(int, object, object, float)
    def _on_started(
        self, generation: int, player: Player, kind: RunKind, started_at: float
    ) -> None:
        if generation != self._generation:
            return
        log = self._logs[player]
        log.planning = True
        log.kind = kind
        log.started_at = started_at
        log.tool_calls = 0
        log.tool_names.clear()
        turn = self._game.turn if self._game is not None else "?"
        what = "Turn" if kind is RunKind.TURN else "Incremental"
        self._add_marker(player, f"{what} planning started (turn {turn}).")
        self.planning_started.emit(player, kind)
        self.state_changed.emit()

    @Slot(int, object, object)
    def _on_event(
        self, generation: int, player: Player, event: StreamEvent | OtherEvent
    ) -> None:
        if generation != self._generation:
            return
        log = self._logs[player]
        if isinstance(event, ToolUseEvent):
            log.tool_calls += 1
        line = format_event(event, log.tool_names)
        if line is not None:
            log.add(line)
            self.line_added.emit(player, line)
        if isinstance(event, ToolUseEvent):
            self.state_changed.emit()
        if isinstance(event, ToolResultEvent):
            self._request_live_refresh(player)

    @Slot(int, object, object, object)
    def _on_finished(
        self,
        generation: int,
        player: Player,
        status: PlanningStatus,
        result: ClaudeRunResult,
    ) -> None:
        if generation != self._generation:
            return
        log = self._logs[player]
        log.planning = False
        log.last_status = status
        log.last_result = result
        message = f"Planning {status.value}."
        if result.error and status is not PlanningStatus.SUCCEEDED:
            message += f" {result.error}"
        if result.total_cost_usd is not None:
            message += f" (cost ${result.total_cost_usd:.2f})"
        self._add_marker(
            player,
            message,
            LogKind.MARKER if status is PlanningStatus.SUCCEEDED else LogKind.ERROR,
        )
        self._dirty.discard(player)
        self.refresh_requested.emit()
        self.state_changed.emit()

        silent = player in self._silent_cancels
        self._silent_cancels.discard(player)
        if silent and status is PlanningStatus.CANCELLED:
            return
        self.planning_finished.emit(player, status, result)

    def _add_marker(
        self, player: Player, text: str, kind: LogKind = LogKind.MARKER
    ) -> None:
        line = LogLine(kind, text, datetime.now())
        self._logs[player].add(line)
        self.line_added.emit(player, line)

    # Live refresh throttling.

    def _request_live_refresh(self, player: Player) -> None:
        self._dirty.add(player)
        if self._refresh_timer.isActive():
            return
        since = (time.monotonic() - self._last_live_refresh) * 1000
        delay = max(0, int(self.refresh_interval_ms - since))
        self._refresh_timer.start(delay)

    @Slot()
    def _flush_live_refresh(self) -> None:
        self._last_live_refresh = time.monotonic()
        dirty = [p for p in COMMANDED_SIDES if p in self._dirty]
        self._dirty.clear()
        for player in dirty:
            self.live_refresh_requested.emit(player)
        if dirty:
            self.state_changed.emit()
