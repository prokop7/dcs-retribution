import threading
from typing import Any, Optional

from game.claudecommander.events import OtherEvent, StreamEvent, TextEvent
from game.claudecommander.planning import PlanningJob, PlanningStatus, SideRun
from game.claudecommander.process import ClaudeNotFoundError, ClaudeRunResult
from game.claudecommander.prompts import RunBrief, RunKind, run_message, system_prompt
from game.theater.player import Player


class FakeRunner:
    def __init__(self, result: Optional[ClaudeRunResult], error: Any = None) -> None:
        self.result = result
        self.error = error
        self.messages: list[str] = []
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def run(
        self, saved_session_id: Optional[str], turn: int, message: str, on_event: Any
    ) -> ClaudeRunResult:
        self.messages.append(message)
        on_event(TextEvent("thinking about SEAD"))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class RecordingListener:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list[tuple[Player, StreamEvent | OtherEvent]] = []
        self.finished: dict[Player, PlanningStatus] = {}

    def on_event(self, player: Player, event: StreamEvent | OtherEvent) -> None:
        with self.lock:
            self.events.append((player, event))

    def on_finished(
        self, player: Player, status: PlanningStatus, result: ClaudeRunResult
    ) -> None:
        with self.lock:
            self.finished[player] = status


def side(player: Player, runner: FakeRunner) -> SideRun:
    return SideRun(
        player,
        runner,  # type: ignore[arg-type]
        None,
        2,
        RunBrief(RunKind.TURN, 2, directive="Hold Kutaisi."),
    )


def test_runs_sides_concurrently_and_reports_status() -> None:
    red = FakeRunner(ClaudeRunResult(session_id="r", success=True))
    blue = FakeRunner(None, ClaudeNotFoundError("no claude"))
    listener = RecordingListener()
    job = PlanningJob([side(Player.RED, red), side(Player.BLUE, blue)], listener)
    job.start()
    job.join(timeout=5)

    assert not job.is_running
    assert listener.finished == {
        Player.RED: PlanningStatus.SUCCEEDED,
        Player.BLUE: PlanningStatus.FAILED,
    }
    assert job.results[Player.BLUE].error == "no claude"
    assert {p for p, _ in listener.events} == {Player.RED, Player.BLUE}
    assert "Hold Kutaisi." in red.messages[0]


def test_cancelled_status() -> None:
    runner = FakeRunner(ClaudeRunResult(cancelled=True, error="cancelled"))
    listener = RecordingListener()
    job = PlanningJob([side(Player.RED, runner)], listener)
    job.start()
    job.join(timeout=5)
    assert listener.finished[Player.RED] is PlanningStatus.CANCELLED


def test_run_messages() -> None:
    first = run_message(RunBrief(RunKind.TURN, 0, is_first_turn=True))
    assert "campaign begins" in first
    takeover = run_message(RunBrief(RunKind.TURN, 7, is_first_turn=True))
    assert "take command of this side in an ongoing campaign" in takeover
    assert "None. Use your own judgement." in first

    turn = run_message(
        RunBrief(
            RunKind.TURN,
            3,
            directive="Conserve MiG-31s.",
            debrief={"own_losses": [{"type": "MiG-29A", "count": 2}]},
        )
    )
    assert "<debrief>" in turn and "MiG-29A" in turn
    assert "Conserve MiG-31s." in turn

    incremental = run_message(
        RunBrief(
            RunKind.INCREMENTAL,
            3,
            changes=["Weather changed: overcast at 3000 ft."],
            locked_packages=["Strike Senaki (player)"],
        )
    )
    assert "situation changed" in incremental
    assert "- Weather changed: overcast at 3000 ft." in incremental
    assert "Strike Senaki (player)" in incremental
    assert "<debrief>" not in incremental


def test_system_prompt_names_side() -> None:
    assert "RED coalition" in system_prompt("Red")
