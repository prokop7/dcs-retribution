import pickle
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

import pytest

from game.claudecommander.coordinator import CommanderCoordinator
from game.claudecommander.events import OtherEvent, StreamEvent
from game.claudecommander.journal import CommanderJournal
from game.claudecommander.planning import PlanningStatus
from game.claudecommander.process import ClaudeRunResult
from game.claudecommander.prompts import RunKind
from game.claudecommander.state import CommanderState
from game.theater.player import Player


class ScriptedRunner:
    """Stands in for CommanderRunner. Each run pops the next scripted result."""

    def __init__(self, journal: CommanderJournal, results: list[ClaudeRunResult]):
        self.journal = journal
        self.results = results
        self.messages: list[str] = []
        self.release = threading.Event()
        self.release.set()

    def cancel(self) -> None:
        pass

    def run(
        self, saved_session_id: Optional[str], turn: int, message: str, on_event: Any
    ) -> ClaudeRunResult:
        self.messages.append(message)
        self.release.wait(5)
        return self.results.pop(0)


class Listener:
    def __init__(self) -> None:
        self.started: list[tuple[Player, RunKind]] = []
        self.finished: list[tuple[Player, PlanningStatus]] = []
        self.done = threading.Event()

    def on_planning_started(self, player: Player, kind: RunKind) -> None:
        self.started.append((player, kind))

    def on_event(self, player: Player, event: StreamEvent | OtherEvent) -> None:
        pass

    def on_planning_finished(
        self, player: Player, status: PlanningStatus, result: ClaudeRunResult
    ) -> None:
        self.finished.append((player, status))
        self.done.set()


def make_game() -> Any:
    red = SimpleNamespace(commander=CommanderState(), ato=SimpleNamespace(packages=[]))
    blue = SimpleNamespace(commander=CommanderState(), ato=SimpleNamespace(packages=[]))
    settings = SimpleNamespace(
        claude_commander_red=True,
        claude_commander_blue=False,
        claude_commander_red_directive="Hold Krymsk.",
        claude_commander_blue_directive="",
    )
    return SimpleNamespace(
        settings=settings,
        turn=2,
        campaign_id="c1",
        campaign_name="Test",
        conditions=SimpleNamespace(start_time=datetime(2026, 5, 1, 8)),
        coalition_for=lambda p: red if p is Player.RED else blue,
    )


@pytest.fixture
def coordinator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[[list[ClaudeRunResult]], tuple[CommanderCoordinator, ScriptedRunner]]:
    def build(
        results: list[ClaudeRunResult],
    ) -> tuple[CommanderCoordinator, ScriptedRunner]:
        runner = ScriptedRunner(
            CommanderJournal(tmp_path / "journal.md", "RED"), results
        )
        coordinator = CommanderCoordinator()
        monkeypatch.setattr(coordinator, "_runner", lambda game, player: runner)
        return coordinator, runner

    return build


def wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_request_without_ui_is_kept_until_attach(coordinator: Any) -> None:
    coord, runner = coordinator(
        [ClaudeRunResult(session_id="s1", success=True, final_text="Orders given.")]
    )
    game = make_game()
    state = game.coalition_for(Player.RED).commander
    game.coalition_for(Player.RED).commander.pending_debrief = {"own_losses": []}

    coord.request(game, Player.RED)
    assert game.coalition_for(Player.RED).commander.needs_planning
    assert not runner.messages

    listener = Listener()
    coord.attach(game, listener)
    assert listener.done.wait(5)
    wait_until(lambda: not coord.is_planning(Player.RED))

    assert listener.started == [(Player.RED, RunKind.TURN)]
    assert listener.finished == [(Player.RED, PlanningStatus.SUCCEEDED)]
    assert state.session_id == "s1"
    assert state.planned_turn == 2
    assert state.pending_debrief is None
    assert not state.needs_planning
    assert "Hold Krymsk." in runner.messages[0]
    titles = [e.title for e in runner.journal.entries()]
    assert titles == ["Debrief of the last mission", "Planning summary (turn)"]


def test_blue_not_commanded_is_ignored(coordinator: Any) -> None:
    coord, runner = coordinator([])
    game = make_game()
    coord.attach(game, Listener())
    coord.request(game, Player.BLUE)
    assert not coord.is_planning(Player.BLUE)
    assert not runner.messages


def test_failure_keeps_changes_for_retry(coordinator: Any) -> None:
    coord, runner = coordinator(
        [
            ClaudeRunResult(error="rate limited"),
            ClaudeRunResult(session_id="s1", success=True),
        ]
    )
    game = make_game()
    listener = Listener()
    coord.attach(game, listener)
    coord.request(game, Player.RED, "Senaki was captured.")
    assert listener.done.wait(5)
    wait_until(lambda: not coord.is_planning(Player.RED))

    state = game.coalition_for(Player.RED).commander
    assert listener.finished == [(Player.RED, PlanningStatus.FAILED)]
    assert state.last_error == "rate limited"
    assert state.needs_planning
    assert state.pending_changes == ["Senaki was captured."]

    listener.done.clear()
    coord.retry(Player.RED)
    assert listener.done.wait(5)
    wait_until(lambda: not coord.is_planning(Player.RED))
    assert "Senaki was captured." in runner.messages[1]
    assert state.pending_changes == [] and state.last_error is None


def test_change_during_run_triggers_incremental_run(coordinator: Any) -> None:
    coord, runner = coordinator(
        [
            ClaudeRunResult(session_id="s1", success=True),
            ClaudeRunResult(session_id="s1", success=True),
        ]
    )
    runner.release.clear()
    game = make_game()
    listener = Listener()
    coord.attach(game, listener)
    coord.request(game, Player.RED)
    wait_until(lambda: len(runner.messages) == 1)

    coord.request(game, Player.RED, "Weather changed.")
    assert len(runner.messages) == 1  # Still planning; queued.
    # A save made now must still say that planning is needed.
    saved = pickle.loads(pickle.dumps(game.coalition_for(Player.RED).commander))
    assert saved.needs_planning
    assert saved.pending_changes == ["Weather changed."]
    runner.release.set()

    wait_until(lambda: len(listener.finished) == 2)
    wait_until(lambda: not coord.is_planning(Player.RED))
    assert [k for _, k in listener.started] == [RunKind.TURN, RunKind.INCREMENTAL]
    assert "Weather changed." in runner.messages[1]


def test_commander_state_save_compat() -> None:
    state = CommanderState(session_id="s1", planned_turn=3)
    old = pickle.loads(pickle.dumps(state))
    assert old.session_id == "s1" and old.planned_turn == 3

    legacy = CommanderState.__new__(CommanderState)
    legacy.__setstate__({"session_id": "s0"})
    assert legacy.session_id == "s0"
    assert legacy.pending_changes == [] and legacy.finished_turn == -1


def test_success_clears_request(coordinator: Any) -> None:
    coord, runner = coordinator([ClaudeRunResult(session_id="s1", success=True)])
    game = make_game()
    listener = Listener()
    coord.attach(game, listener)
    coord.request(game, Player.RED, "Senaki was captured.")
    assert listener.done.wait(5)
    wait_until(lambda: not coord.is_planning(Player.RED))
    state = game.coalition_for(Player.RED).commander
    assert state.pending_changes == []
    assert len(runner.messages) == 1
    assert not game.coalition_for(Player.RED).commander.needs_planning
