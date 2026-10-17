from PySide6.QtCore import QCoreApplication

from game.claudecommander.events import TextEvent, ThinkingEvent, ToolUseEvent
from game.claudecommander.planning import PlanningStatus
from game.claudecommander.process import ClaudeRunResult
from game.claudecommander.prompts import RunKind
from game.theater.player import Player
from qt_ui.widgets.commander.statuswidget import (
    CommanderStatusBox,
    CommanderStatusPanel,
    CommanderUiState,
    commander_status,
)
from qt_ui.windows.commander.bridge import CommanderBridge
from qt_ui.windows.commander.failuredialog import (
    CommanderFailureHandler,
    FailureChoice,
)
from qt_ui.windows.commander.logwindow import CommanderLogWindow, EMPTY_JOURNAL
from tests.qt_commander.conftest import FakeCoordinator, FakeGame


def start_planning(
    bridge: CommanderBridge, coordinator: FakeCoordinator, player: Player
) -> None:
    coordinator.planning.add(player)
    bridge.on_planning_started(player, RunKind.TURN)
    bridge.on_event(player, ToolUseEvent("t1", "mcp__retribution__get_ato", {}))
    QCoreApplication.processEvents()


def finish_planning(
    bridge: CommanderBridge,
    coordinator: FakeCoordinator,
    player: Player,
    status: PlanningStatus,
    result: ClaudeRunResult,
) -> None:
    coordinator.planning.discard(player)
    bridge.on_planning_finished(player, status, result)
    QCoreApplication.processEvents()


def test_status_transitions(
    bridge: CommanderBridge, coordinator: FakeCoordinator, fake_game: FakeGame
) -> None:
    state = fake_game.coalition_for(Player.BLUE).commander
    fake_game.coalition_for(Player.BLUE).ato.packages.extend([object(), object()])

    state.needs_planning = True
    assert commander_status(bridge, Player.BLUE).state is CommanderUiState.IDLE

    start_planning(bridge, coordinator, Player.BLUE)
    status = commander_status(bridge, Player.BLUE)
    assert status.state is CommanderUiState.PLANNING
    assert status.text == "Planning 00:00 · 1 call · 2 pkgs"
    assert "1 tool call so far, 2 packages" in status.tooltip

    coordinator.planning.clear()
    state.needs_planning = False
    state.planned_turn = fake_game.turn
    status = commander_status(bridge, Player.BLUE)
    assert status.state is CommanderUiState.READY
    assert status.text == "Ready · 2 pkgs"

    state.needs_planning = True
    state.last_error = "Claude Code exited with code 1"
    status = commander_status(bridge, Player.BLUE)
    assert status.state is CommanderUiState.ERROR
    assert "exited with code 1" in status.tooltip


def test_status_box_buttons(
    bridge: CommanderBridge, coordinator: FakeCoordinator, fake_game: FakeGame
) -> None:
    box = CommanderStatusBox(bridge, Player.BLUE)
    assert not box.isHidden()
    assert not box.log_button.isHidden()
    assert not box.replan_button.isHidden()
    assert box.cancel_button.isHidden()

    start_planning(bridge, coordinator, Player.BLUE)
    box.refresh()
    assert box.status_label.text().startswith("Planning")
    assert not box.cancel_button.isHidden()
    assert box.replan_button.isHidden()

    box.cancel_button.click()
    assert coordinator.calls[-1] == ("cancel", Player.BLUE)
    coordinator.planning.clear()
    box.refresh()
    box.replan_button.click()
    assert coordinator.calls[-1][:2] == ("request", Player.BLUE)

    # Hidden for sides Claude doesn't command.
    red_box = CommanderStatusBox(bridge, Player.RED)
    assert red_box.isHidden()


def test_opfor_without_reveal_hides_details(
    bridge: CommanderBridge, coordinator: FakeCoordinator, fake_game: FakeGame
) -> None:
    fake_game.settings.claude_commander_red = True
    fake_game.coalition_for(Player.RED).ato.packages.append(object())
    box = CommanderStatusBox(bridge, Player.RED)
    start_planning(bridge, coordinator, Player.RED)
    box.refresh()
    assert box.status_label.text() == "Planning…"
    assert box.status_label.toolTip() == "OPFOR commander is planning…"
    assert box.log_button.isHidden()
    assert not box.cancel_button.isHidden()
    box.open_log()
    assert box.log_window is None

    coordinator.planning.clear()
    box.refresh()
    assert box.status_label.text() == "Ready"
    assert "package" not in box.status_label.toolTip()

    fake_game.settings.claude_commander_reveal_red = True
    box.refresh()
    assert not box.log_button.isHidden()
    assert box.status_label.text() == "Ready (built-in) · 1 pkg"


def test_status_panel_visibility(
    bridge: CommanderBridge, coordinator: FakeCoordinator, fake_game: FakeGame
) -> None:
    panel = CommanderStatusPanel(bridge)
    assert not panel.isHidden()
    fake_game.settings.claude_commander_blue = False
    bridge.state_changed.emit()
    assert panel.isHidden()

    fake_game.settings.claude_commander_red = True
    start_planning(bridge, coordinator, Player.RED)
    assert not panel.isHidden()
    assert panel.timer.isActive()
    coordinator.planning.clear()
    bridge.state_changed.emit()
    assert not panel.timer.isActive()


def test_failure_dialog_retry(
    bridge: CommanderBridge, coordinator: FakeCoordinator
) -> None:
    handler = CommanderFailureHandler(bridge, None)
    result = ClaudeRunResult(
        error="Planning timed out after 20 minutes.", stderr_tail=["a", "b"]
    )
    finish_planning(bridge, coordinator, Player.BLUE, PlanningStatus.FAILED, result)
    dialog = handler.dialogs[Player.BLUE]
    assert dialog.isVisible() and dialog.isModal()
    assert "timed out" in dialog.error_label.text()
    assert dialog.details is not None
    assert dialog.details.toPlainText() == "a\nb"
    assert not dialog.details.isVisible()
    dialog.details_button.click()
    assert dialog.details.isVisible()

    dialog.retry_button.click()
    assert dialog.choice is FailureChoice.RETRY
    assert not dialog.isVisible()
    assert coordinator.calls[-1] == ("retry", Player.BLUE)
    assert Player.BLUE not in handler.dialogs


def test_failure_dialog_builtin_and_cancel(
    bridge: CommanderBridge, coordinator: FakeCoordinator, fake_game: FakeGame
) -> None:
    fake_game.settings.claude_commander_red = True
    handler = CommanderFailureHandler(bridge, None)
    refreshes: list[bool] = []
    bridge.refresh_requested.connect(lambda: refreshes.append(True))

    # Both sides fail: one dialog each.
    failed = ClaudeRunResult(error="boom")
    finish_planning(bridge, coordinator, Player.BLUE, PlanningStatus.FAILED, failed)
    cancelled = ClaudeRunResult(error="Planning was cancelled.", cancelled=True)
    finish_planning(
        bridge, coordinator, Player.RED, PlanningStatus.CANCELLED, cancelled
    )
    assert set(handler.dialogs) == {Player.BLUE, Player.RED}
    assert handler.dialogs[Player.BLUE].details is None
    assert "cancelled" in handler.dialogs[Player.RED].windowTitle()

    refreshes.clear()
    handler.dialogs[Player.BLUE].builtin_button.click()
    assert coordinator.calls[-1] == ("builtin", Player.BLUE)
    assert refreshes == [True]

    calls = len(coordinator.calls)
    handler.dialogs[Player.RED].cancel_button.click()
    assert len(coordinator.calls) == calls
    assert handler.dialogs == {}


def test_success_opens_no_dialog(
    bridge: CommanderBridge, coordinator: FakeCoordinator
) -> None:
    handler = CommanderFailureHandler(bridge, None)
    finish_planning(
        bridge,
        coordinator,
        Player.BLUE,
        PlanningStatus.SUCCEEDED,
        ClaudeRunResult(success=True),
    )
    assert handler.dialogs == {}


def test_new_run_closes_the_failure_dialog(
    bridge: CommanderBridge, coordinator: FakeCoordinator
) -> None:
    handler = CommanderFailureHandler(bridge, None)
    finish_planning(
        bridge,
        coordinator,
        Player.BLUE,
        PlanningStatus.FAILED,
        ClaudeRunResult(error="x"),
    )
    dialog = handler.dialogs[Player.BLUE]
    start_planning(bridge, coordinator, Player.BLUE)
    assert not dialog.isVisible()
    assert handler.dialogs == {}
    assert ("retry", Player.BLUE) not in coordinator.calls


def test_log_window(bridge: CommanderBridge, coordinator: FakeCoordinator) -> None:
    window = CommanderLogWindow(
        bridge,
        Player.BLUE,
        journal_source=lambda player: "## Turn 1 — Intent\n\nHold the **line**.\n",
    )
    assert "Nothing yet" in window.live_text()
    assert "Hold the line." in window.journal_text()

    start_planning(bridge, coordinator, Player.BLUE)
    bridge.on_event(Player.BLUE, TextEvent("Looking at the front."))
    bridge.on_event(Player.BLUE, ThinkingEvent("secret musings"))
    QCoreApplication.processEvents()
    text = window.live_text()
    assert "planning started" in text
    assert "get_ato()" in text
    assert "Looking at the front." in text
    assert "secret musings" not in text

    window.show_thinking.setChecked(True)
    assert "secret musings" in window.live_text()

    # Another side's events stay out.
    bridge.on_event(Player.RED, TextEvent("red stuff"))
    QCoreApplication.processEvents()
    assert "red stuff" not in window.live_text()

    empty = CommanderLogWindow(bridge, Player.BLUE, journal_source=lambda p: "")
    assert empty.journal_text() == EMPTY_JOURNAL.strip("*")
    window.close()
    empty.close()
