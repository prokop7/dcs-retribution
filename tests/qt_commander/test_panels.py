"""The top panel and ATO panel with a real game while a commander plans."""

from typing import Iterator

import pytest
from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QApplication

from game import Game
from game.theater.player import Player
from qt_ui.models import GameModel
from qt_ui.simcontroller import SimController
from qt_ui.uiflags import UiFlags
from qt_ui.widgets.QTopPanel import FLYING_BLOCKED_TOOLTIP, QTopPanel
from qt_ui.widgets.ato import PLANNING_LOCK_TOOLTIP, QAirTaskingOrderPanel
from qt_ui.windows.commander.bridge import CommanderBridge
from tests.qt_commander.conftest import FakeCoordinator


@pytest.fixture
def commanded_game(real_game: Game) -> Iterator[Game]:
    settings = real_game.settings
    previous = settings.claude_commander_blue
    settings.claude_commander_blue = True
    try:
        yield real_game
    finally:
        settings.claude_commander_blue = previous


@pytest.fixture
def real_bridge(
    qapp: QApplication, coordinator: FakeCoordinator, commanded_game: Game
) -> Iterator[CommanderBridge]:
    bridge = CommanderBridge(coordinator)
    bridge.attach(commanded_game)
    yield bridge
    bridge.detach()


def test_top_panel_blocks_flying_while_planning(
    commanded_game: Game, real_bridge: CommanderBridge, coordinator: FakeCoordinator
) -> None:
    sim_controller = SimController(commanded_game)
    game_model = GameModel(commanded_game, sim_controller)
    panel = QTopPanel(game_model, sim_controller, UiFlags(False, False), real_bridge)
    panel.setGame(commanded_game)
    assert commanded_game.turn > 0
    assert panel.passTurnButton.isEnabled()
    assert panel.proceedButton.isEnabled()
    assert panel.commander_status is not None
    assert not panel.commander_status.isHidden()
    assert not panel.commander_status.boxes[Player.BLUE].isHidden()
    assert panel.commander_status.boxes[Player.RED].isHidden()

    coordinator.planning.add(Player.BLUE)
    real_bridge.state_changed.emit()
    assert not panel.passTurnButton.isEnabled()
    assert not panel.proceedButton.isEnabled()
    assert panel.proceedButton.toolTip() == FLYING_BLOCKED_TOOLTIP

    # A game update (e.g. from the settings window) keeps the lock.
    panel.setGame(commanded_game)
    assert not panel.proceedButton.isEnabled()

    # Passing the turn is refused while planning.
    turn = commanded_game.turn
    panel.passTurn()
    assert commanded_game.turn == turn

    coordinator.planning.clear()
    real_bridge.state_changed.emit()
    assert panel.passTurnButton.isEnabled()
    assert panel.proceedButton.isEnabled()
    assert panel.proceedButton.toolTip() == ""


def test_ato_panel_is_locked_while_its_side_plans(
    commanded_game: Game, real_bridge: CommanderBridge
) -> None:
    assert commanded_game.blue.ato.packages, "the test game should have packages"
    sim_controller = SimController(commanded_game)
    game_model = GameModel(commanded_game, sim_controller)
    panel = QAirTaskingOrderPanel(game_model)
    package_panel = panel.package_panel
    package_panel.package_list.selectionModel().setCurrentIndex(
        game_model.ato_model.index(0, 0), QItemSelectionModel.SelectionFlag.Select
    )
    assert package_panel.edit_button.isEnabled()

    panel.set_locked_sides({Player.BLUE})
    assert panel.editing_locked
    assert not package_panel.edit_button.isEnabled()
    assert not package_panel.delete_button.isEnabled()
    assert not package_panel.clone_button.isEnabled()
    assert package_panel.edit_button.toolTip() == PLANNING_LOCK_TOOLTIP
    assert not panel.flight_panel.edit_button.isEnabled()
    assert panel.flight_panel.flight_list.locked

    # Selecting another package doesn't unlock editing.
    package_panel.package_list.selectionModel().setCurrentIndex(
        game_model.ato_model.index(0, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect,
    )
    panel.on_package_change()
    assert not package_panel.edit_button.isEnabled()

    # OPFOR's ATO isn't locked by OWNFOR planning.
    panel.red_ato_checkbox.setChecked(True)
    assert panel.displayed_side is Player.RED
    assert [package_panel.locked, panel.flight_panel.locked] == [False, False]
    panel.red_ato_checkbox.setChecked(False)
    assert [package_panel.locked, panel.flight_panel.locked] == [True, True]

    panel.set_locked_sides(set())
    package_panel.package_list.selectionModel().setCurrentIndex(
        game_model.ato_model.index(0, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect,
    )
    panel.on_package_change()
    assert package_panel.edit_button.isEnabled()
    assert package_panel.edit_button.toolTip() == ""
