"""Compact per-side commander status for the top panel."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from game.theater.player import Player
from qt_ui.windows.commander.bridge import (
    COMMANDED_SIDES,
    CommanderBridge,
    side_label,
)
from qt_ui.windows.commander.logwindow import CommanderLogWindow, JournalSource


class CommanderUiState(Enum):
    IDLE = "Idle"
    PLANNING = "Planning"
    READY = "Ready"
    ERROR = "Error"


@dataclass(frozen=True)
class CommanderStatus:
    state: CommanderUiState
    text: str
    tooltip: str = ""


_WIDEST_STATUS = "Planning 00:00 · 999 calls · 99 pkgs"


def _mm_ss(seconds: Optional[float]) -> str:
    total = int(seconds or 0)
    return f"{total // 60:02d}:{total % 60:02d}"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def commander_status(bridge: CommanderBridge, player: Player) -> CommanderStatus:
    """What the status widget shows for ``player``.

    OPFOR's details (time, tool calls, packages) are hidden unless the OPFOR
    commander is revealed in the settings.
    """
    game = bridge.game
    if game is None:
        return CommanderStatus(CommanderUiState.IDLE, "No game")
    state = game.coalition_for(player).commander
    reveal = bridge.reveals(player)
    packages = len(game.coalition_for(player).ato.packages)
    side = side_label(player)

    # The top panel is crowded, so labels are short and tooltips spell them out.
    if bridge.is_planning(player):
        if not reveal:
            return CommanderStatus(
                CommanderUiState.PLANNING,
                "Planning…",
                f"{side} commander is planning…",
            )
        log = bridge.log(player)
        elapsed = _mm_ss(bridge.elapsed_seconds(player))
        text = (
            f"Planning {elapsed} · {_plural(log.tool_calls, 'call')} · "
            f"{_plural(packages, 'pkg')}"
        )
        tooltip = (
            f"Planning for {elapsed}: {_plural(log.tool_calls, 'tool call')} so "
            f"far, {_plural(packages, 'package')} in the ATO."
        )
        return CommanderStatus(CommanderUiState.PLANNING, text, tooltip)

    if state.needs_planning and state.last_error:
        return CommanderStatus(
            CommanderUiState.ERROR,
            "Error",
            f"{state.last_error}\n\nUse Re-plan to try again.",
        )

    if not state.needs_planning:
        if not reveal:
            return CommanderStatus(
                CommanderUiState.READY, "Ready", f"{side} commander is ready."
            )
        text = "Ready"
        tooltip = f"{side} commander is ready"
        if state.planned_turn != game.turn:
            text = "Ready (built-in)"
            tooltip = "This turn was planned by the built-in planner"
        return CommanderStatus(
            CommanderUiState.READY,
            f"{text} · {_plural(packages, 'pkg')}",
            f"{tooltip}: {_plural(packages, 'package')} in the ATO.",
        )

    return CommanderStatus(
        CommanderUiState.IDLE, "Idle", "Planning starts when the game is ready."
    )


class CommanderStatusBox(QWidget):
    """Status and controls of one side's commander, as one compact row.

    Shown in the main window's status bar: the top panel has no room left, and
    widgets there set the window's minimum width.
    """

    def __init__(
        self,
        bridge: CommanderBridge,
        player: Player,
        journal_source: Optional[JournalSource] = None,
    ) -> None:
        super().__init__()
        self.bridge = bridge
        self.player = player
        self.journal_source = journal_source
        self.log_window: Optional[CommanderLogWindow] = None
        self.status = CommanderStatus(CommanderUiState.IDLE, "")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)
        self.side_label = QLabel(f"<b>{side_label(player)}</b>")
        layout.addWidget(self.side_label)
        self.status_label = QLabel()
        layout.addWidget(self.status_label)

        self.log_button = QPushButton("Log")
        self.log_button.setToolTip("Show the commander's live log and journal.")
        self.log_button.clicked.connect(self.open_log)
        layout.addWidget(self.log_button)

        self.replan_button = QPushButton("Re-plan")
        self.replan_button.setToolTip(
            "Ask the commander to review and re-plan the current turn."
        )
        self.replan_button.clicked.connect(lambda: self.bridge.replan(self.player))
        layout.addWidget(self.replan_button)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setProperty("style", "btn-danger")
        self.cancel_button.setToolTip("Stop the commander's planning run.")
        self.cancel_button.clicked.connect(lambda: self.bridge.cancel(self.player))
        layout.addWidget(self.cancel_button)

        self.refresh()

    def refresh(self) -> None:
        enabled = self.bridge.is_enabled(self.player)
        self.setVisible(enabled)
        if not enabled:
            self._close_log()
            return
        self.status = commander_status(self.bridge, self.player)
        # Wide enough for the longest status so the row doesn't jump while the
        # timer ticks. Measured after polishing, since the stylesheet sets the font.
        self.status_label.ensurePolished()
        self.status_label.setMinimumWidth(
            self.status_label.fontMetrics().horizontalAdvance(_WIDEST_STATUS) + 8
        )
        self.status_label.setText(self.status.text)
        self.status_label.setToolTip(self.status.tooltip)
        planning = self.status.state is CommanderUiState.PLANNING
        reveal = self.bridge.reveals(self.player)
        self.log_button.setVisible(reveal)
        self.cancel_button.setVisible(planning)
        self.replan_button.setVisible(not planning)
        if not reveal:
            self._close_log()

    def open_log(self) -> None:
        if not self.bridge.reveals(self.player):
            return
        if self.log_window is not None and self.log_window.isVisible():
            self.log_window.raise_()
            self.log_window.activateWindow()
            return
        self.log_window = CommanderLogWindow(
            self.bridge, self.player, self.journal_source, self.window()
        )
        self.log_window.show()

    def _close_log(self) -> None:
        if self.log_window is not None:
            self.log_window.close()
            self.log_window = None


class CommanderStatusPanel(QWidget):
    """The status boxes of both commanders; hidden while neither side has one."""

    def __init__(
        self,
        bridge: CommanderBridge,
        journal_source: Optional[JournalSource] = None,
    ) -> None:
        super().__init__()
        self.bridge = bridge
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.boxes = {
            player: CommanderStatusBox(bridge, player, journal_source)
            for player in COMMANDED_SIDES
        }
        for index, box in enumerate(self.boxes.values()):
            if index:
                separator = QFrame()
                separator.setFrameShape(QFrame.Shape.VLine)
                separator.setFrameShadow(QFrame.Shadow.Sunken)
                layout.addWidget(separator)
            layout.addWidget(box)

        # Ticks the elapsed time while a commander plans.
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)

        bridge.state_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        for box in self.boxes.values():
            box.refresh()
        self.setVisible(any(self.bridge.is_enabled(p) for p in self.boxes))
        if self.bridge.any_planning:
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.timer.stop()
