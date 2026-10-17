import threading
from typing import Any

from PySide6.QtCore import QCoreApplication
from PySide6.QtTest import QTest

from game.claudecommander.events import ToolResultEvent, ToolUseEvent
from game.claudecommander.planning import PlanningStatus
from game.claudecommander.process import ClaudeRunResult
from game.claudecommander.prompts import RunKind
from game.theater.player import Player
from qt_ui.windows.commander.bridge import (
    CommanderBridge,
    REPLAN_CHANGE,
    TAKE_COMMAND_CHANGE,
)
from qt_ui.windows.commander.logformat import LogKind
from tests.qt_commander.conftest import FakeCoordinator, FakeGame, as_game


def run_in_worker(target: Any) -> None:
    thread = threading.Thread(target=target)
    thread.start()
    thread.join()


def test_attach_and_detach(
    bridge: CommanderBridge, coordinator: FakeCoordinator, fake_game: FakeGame
) -> None:
    assert coordinator.calls == [("attach", fake_game)]
    # The same game again is a no-op: re-attaching would restart failed planning.
    bridge.set_game(as_game(fake_game))
    assert coordinator.names() == ["attach"]
    bridge.set_game(None)
    assert coordinator.names() == ["attach", "detach"]
    assert bridge.game is None


def test_worker_callbacks_are_delivered_on_the_ui_thread(
    bridge: CommanderBridge, coordinator: FakeCoordinator
) -> None:
    received: list[tuple[str, threading.Thread]] = []
    bridge.planning_started.connect(
        lambda player, kind: received.append(("started", threading.current_thread()))
    )
    bridge.line_added.connect(
        lambda player, line: received.append(
            (line.kind.value, threading.current_thread())
        )
    )

    def worker() -> None:
        coordinator.planning.add(Player.BLUE)
        bridge.on_planning_started(Player.BLUE, RunKind.TURN)
        bridge.on_event(
            Player.BLUE,
            ToolUseEvent("t1", "mcp__retribution__create_package", {"target": "X"}),
        )

    run_in_worker(worker)
    # Nothing runs on the worker thread; the signals are queued.
    assert received == []
    QCoreApplication.processEvents()

    assert [r[0] for r in received] == ["marker", "started", "tool_call"]
    assert all(r[1] is threading.main_thread() for r in received)
    log = bridge.log(Player.BLUE)
    assert log.planning and log.kind is RunKind.TURN
    assert log.tool_calls == 1
    assert log.lines[-1].text == "create_package(target='X')"
    assert bridge.elapsed_seconds(Player.BLUE) is not None


def test_callbacks_of_a_previous_game_are_dropped(
    bridge: CommanderBridge, fake_game: FakeGame
) -> None:
    started: list[Player] = []
    bridge.planning_started.connect(lambda player, kind: started.append(player))
    run_in_worker(lambda: bridge.on_planning_started(Player.BLUE, RunKind.TURN))
    bridge.attach(as_game(FakeGame()))
    QCoreApplication.processEvents()
    assert started == []
    assert bridge.log(Player.BLUE).lines == []


def test_live_refresh_is_throttled(bridge: CommanderBridge) -> None:
    refreshed: list[Player] = []
    bridge.live_refresh_requested.connect(refreshed.append)

    def results() -> None:
        for i in range(5):
            bridge.on_event(Player.BLUE, ToolResultEvent(f"t{i}", "ok", False))

    # The first refresh happens right away, and a burst refreshes once.
    run_in_worker(results)
    QTest.qWait(20)
    assert refreshed == [Player.BLUE]

    # Results inside the interval wait for the interval to pass.
    run_in_worker(results)
    QCoreApplication.processEvents()
    assert refreshed == [Player.BLUE]
    QTest.qWait(250)
    assert refreshed == [Player.BLUE, Player.BLUE]


def test_finish_requests_refresh_and_reports(
    bridge: CommanderBridge, coordinator: FakeCoordinator
) -> None:
    refreshes: list[bool] = []
    finished: list[tuple[Player, PlanningStatus]] = []
    bridge.refresh_requested.connect(lambda: refreshes.append(True))
    bridge.planning_finished.connect(
        lambda player, status, result: finished.append((player, status))
    )
    result = ClaudeRunResult(error="boom", total_cost_usd=0.5)
    run_in_worker(
        lambda: bridge.on_planning_finished(Player.RED, PlanningStatus.FAILED, result)
    )
    QCoreApplication.processEvents()
    assert refreshes == [True]
    assert finished == [(Player.RED, PlanningStatus.FAILED)]
    log = bridge.log(Player.RED)
    assert not log.planning and log.last_result is result
    assert log.lines[-1].kind is LogKind.ERROR
    assert "boom" in log.lines[-1].text and "$0.50" in log.lines[-1].text


def test_commands_go_to_the_coordinator(
    bridge: CommanderBridge, coordinator: FakeCoordinator
) -> None:
    refreshes: list[bool] = []
    bridge.refresh_requested.connect(lambda: refreshes.append(True))
    bridge.replan(Player.BLUE)
    bridge.cancel(Player.BLUE)
    bridge.retry(Player.RED)
    bridge.use_builtin_planner(Player.RED)
    assert coordinator.calls[1:] == [
        ("request", Player.BLUE, REPLAN_CHANGE),
        ("cancel", Player.BLUE),
        ("retry", Player.RED),
        ("builtin", Player.RED),
    ]
    assert refreshes == [True]


def test_enabling_a_commander_in_settings_takes_command(
    bridge: CommanderBridge, coordinator: FakeCoordinator, fake_game: FakeGame
) -> None:
    before = bridge.enabled_sides()
    assert before == {Player.BLUE: True, Player.RED: False}
    fake_game.settings.claude_commander_red = True
    bridge.apply_settings_change(before)
    assert coordinator.calls[-1] == ("request", Player.RED, TAKE_COMMAND_CHANGE)
    # The built-in plan of this turn is handed over before the commander runs.
    assert fake_game.coalitions[Player.RED].handed_over == 1

    # Unchanged settings do nothing.
    count = len(coordinator.calls)
    bridge.apply_settings_change(bridge.enabled_sides())
    assert len(coordinator.calls) == count


def test_disabling_a_planning_commander_cancels_it_silently(
    bridge: CommanderBridge, coordinator: FakeCoordinator, fake_game: FakeGame
) -> None:
    finished: list[Player] = []
    bridge.planning_finished.connect(
        lambda player, status, result: finished.append(player)
    )
    coordinator.planning.add(Player.BLUE)
    before = bridge.enabled_sides()
    fake_game.settings.claude_commander_blue = False
    bridge.apply_settings_change(before)
    assert coordinator.calls[-1] == ("cancel", Player.BLUE)

    coordinator.planning.clear()
    cancelled = ClaudeRunResult(cancelled=True, error="Planning was cancelled.")
    bridge.on_planning_finished(Player.BLUE, PlanningStatus.CANCELLED, cancelled)
    QCoreApplication.processEvents()
    assert finished == []
    # A later cancellation by the player is reported again.
    bridge.on_planning_finished(Player.BLUE, PlanningStatus.CANCELLED, cancelled)
    QCoreApplication.processEvents()
    assert finished == [Player.BLUE]
