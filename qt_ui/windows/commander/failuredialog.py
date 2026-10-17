"""The dialog shown when a commander's planning run fails or is cancelled."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from game.claudecommander.planning import PlanningStatus
from game.claudecommander.process import ClaudeRunResult
from game.theater.player import Player
from qt_ui.windows.commander.bridge import CommanderBridge, side_label


class FailureChoice(Enum):
    RETRY = "retry"
    BUILTIN = "builtin"
    CANCEL = "cancel"


class CommanderFailureDialog(QDialog):
    """Asks how to continue after a failed run: retry, built-in planner or cancel.

    "Cancel" leaves the plan as it is. The side keeps needing planning, so its status
    shows the error and offers a re-plan.
    """

    #: (Player, FailureChoice)
    chosen = Signal(object, object)

    def __init__(
        self,
        player: Player,
        status: PlanningStatus,
        result: ClaudeRunResult,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.player = player
        self.choice: Optional[FailureChoice] = None
        self.setModal(True)

        side = side_label(player)
        if status is PlanningStatus.CANCELLED:
            title = f"{side} commander planning was cancelled"
        else:
            title = f"{side} commander planning failed"
        self.setWindowTitle(title)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        heading = QLabel(f"<b>{title}</b>")
        layout.addWidget(heading)

        error = result.error or "The commander stopped without a result."
        self.error_label = QLabel(error)
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        explanation = QLabel(
            "Retry the run, plan this turn with the built-in planner instead, or "
            "cancel to keep the current plan. Flying is blocked while a commander "
            "is planning."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.details: Optional[QPlainTextEdit] = None
        if result.stderr_tail:
            self.details_button = QPushButton("Show details")
            self.details_button.setCheckable(True)
            layout.addWidget(self.details_button)
            self.details = QPlainTextEdit("\n".join(result.stderr_tail))
            self.details.setReadOnly(True)
            self.details.setVisible(False)
            self.details.setMinimumHeight(160)
            layout.addWidget(self.details)
            self.details_button.toggled.connect(self.details.setVisible)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.retry_button = QPushButton("Retry")
        self.retry_button.setProperty("style", "btn-primary")
        self.retry_button.clicked.connect(lambda: self.choose(FailureChoice.RETRY))
        buttons.addWidget(self.retry_button)
        self.builtin_button = QPushButton("Use built-in planner this turn")
        self.builtin_button.clicked.connect(lambda: self.choose(FailureChoice.BUILTIN))
        buttons.addWidget(self.builtin_button)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(lambda: self.choose(FailureChoice.CANCEL))
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self.retry_button.setDefault(True)

    def choose(self, choice: FailureChoice) -> None:
        if self.choice is not None:
            return
        self.choice = choice
        self.chosen.emit(self.player, choice)
        if choice is FailureChoice.CANCEL:
            self.reject()
        else:
            self.accept()

    def reject(self) -> None:
        # Closing the window or pressing Escape counts as "Cancel".
        if self.choice is None:
            self.choice = FailureChoice.CANCEL
            self.chosen.emit(self.player, FailureChoice.CANCEL)
        super().reject()


class CommanderFailureHandler:
    """Opens one failure dialog per side and carries out the choice."""

    def __init__(self, bridge: CommanderBridge, parent: Optional[QWidget]) -> None:
        self.bridge = bridge
        self.parent = parent
        self.dialogs: dict[Player, CommanderFailureDialog] = {}
        bridge.planning_finished.connect(self.on_planning_finished)
        bridge.planning_started.connect(self.on_planning_started)
        bridge.game_changed.connect(self.close_all)

    def on_planning_started(self, player: Player, _kind: object) -> None:
        # A new run (for example a re-plan from the status bar) makes an open
        # dialog for that side moot.
        dialog = self.dialogs.pop(player, None)
        if dialog is not None and dialog.choice is None:
            dialog.choice = FailureChoice.CANCEL
            dialog.close()

    def on_planning_finished(
        self, player: Player, status: PlanningStatus, result: ClaudeRunResult
    ) -> None:
        if status not in (PlanningStatus.FAILED, PlanningStatus.CANCELLED):
            return
        if self.bridge.game is None:
            return
        old = self.dialogs.pop(player, None)
        if old is not None and old.choice is None:
            old.choice = FailureChoice.CANCEL
            old.close()
        dialog = CommanderFailureDialog(player, status, result, self.parent)
        dialog.chosen.connect(self.on_chosen)
        self.dialogs[player] = dialog
        dialog.show()

    def on_chosen(self, player: Player, choice: FailureChoice) -> None:
        self.dialogs.pop(player, None)
        if choice is FailureChoice.RETRY:
            self.bridge.retry(player)
        elif choice is FailureChoice.BUILTIN:
            self.bridge.use_builtin_planner(player)
        else:
            self.bridge.state_changed.emit()

    def close_all(self) -> None:
        for dialog in list(self.dialogs.values()):
            dialog.choice = FailureChoice.CANCEL
            dialog.close()
        self.dialogs.clear()
