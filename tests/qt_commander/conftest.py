"""Fixtures for the Claude Commander Qt UI tests.

The tests run without a display: the offscreen Qt platform must be selected before
PySide6 is imported anywhere.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterator, Optional, cast

import pytest
from PySide6.QtWidgets import QApplication

from game import Game
from game.claudecommander.state import CommanderState
from game.theater.player import Player
from qt_ui import liberation_theme, uiconstants
from qt_ui.windows.GameUpdateSignal import GameUpdateSignal
from qt_ui.windows.commander.bridge import CommanderBridge


@pytest.fixture(scope="session")
def qapp() -> Iterator[QApplication]:
    app = QApplication.instance() or QApplication([])
    liberation_theme.set_theme_index(0)
    uiconstants.load_icons()
    uiconstants.load_event_icons()
    if GameUpdateSignal.get_instance() is None:
        GameUpdateSignal()
    yield cast(QApplication, app)


class FakeCoordinator:
    """Records the UI's calls instead of running Claude."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.planning: set[Player] = set()
        self.game: Optional[Game] = None
        self.listener: Optional[CommanderBridge] = None

    def attach(self, game: Game, listener: CommanderBridge) -> None:
        self.calls.append(("attach", game))
        self.game = game
        self.listener = listener

    def detach(self) -> None:
        self.calls.append(("detach",))
        self.planning.clear()
        self.game = None
        self.listener = None

    def request(self, game: Game, player: Player, change: Optional[str] = None) -> None:
        self.calls.append(("request", player, change))

    def retry(self, player: Player) -> None:
        self.calls.append(("retry", player))

    def cancel(self, player: Player) -> None:
        self.calls.append(("cancel", player))

    def use_builtin_planner(self, player: Player) -> None:
        self.calls.append(("builtin", player))

    def is_planning(self, player: Player) -> bool:
        return player in self.planning

    @property
    def any_planning(self) -> bool:
        return bool(self.planning)

    def names(self) -> list[Any]:
        return [c[0] for c in self.calls]


@dataclass
class FakeCoalition:
    commander: CommanderState = field(default_factory=CommanderState)
    ato: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(packages=[]))


class FakeGame:
    """The bits of ``Game`` the commander UI reads."""

    def __init__(self, blue: bool = True, red: bool = False) -> None:
        self.settings = SimpleNamespace(
            claude_commander_blue=blue,
            claude_commander_red=red,
            claude_commander_reveal_red=False,
        )
        self.turn = 1
        self.coalitions = {Player.BLUE: FakeCoalition(), Player.RED: FakeCoalition()}

    def coalition_for(self, player: Player) -> FakeCoalition:
        return self.coalitions[player]


def as_game(fake: FakeGame) -> Game:
    return cast(Game, fake)


@pytest.fixture
def coordinator() -> FakeCoordinator:
    return FakeCoordinator()


@pytest.fixture
def fake_game() -> FakeGame:
    return FakeGame()


@pytest.fixture
def bridge(
    qapp: QApplication, coordinator: FakeCoordinator, fake_game: FakeGame
) -> Iterator[CommanderBridge]:
    bridge = CommanderBridge(coordinator, refresh_interval_ms=100)
    bridge.attach(as_game(fake_game))
    yield bridge
    bridge.deleteLater()


@pytest.fixture(scope="session")
def real_game(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Game]:
    from tests.claudecommander.conftest import TEST_CAMPAIGN, generate_test_game

    previous_level = logging.getLogger().level
    logging.getLogger().setLevel(logging.ERROR)
    try:
        game = generate_test_game(TEST_CAMPAIGN, tmp_path_factory.mktemp("dcs"))
    finally:
        logging.getLogger().setLevel(previous_level)
    yield game
