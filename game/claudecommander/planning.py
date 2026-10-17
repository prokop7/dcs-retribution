"""Runs the commanders of one or more sides concurrently."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol

from game.claudecommander.events import OtherEvent, StreamEvent
from game.claudecommander.process import ClaudeNotFoundError, ClaudeRunResult
from game.claudecommander.prompts import RunBrief, run_message
from game.claudecommander.runner import CommanderRunner
from game.theater.player import Player


class PlanningStatus(Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanningListener(Protocol):
    """Receives progress from worker threads. Implementations must be thread safe."""

    def on_event(self, player: Player, event: StreamEvent | OtherEvent) -> None: ...

    def on_finished(
        self, player: Player, status: PlanningStatus, result: ClaudeRunResult
    ) -> None: ...


@dataclass
class SideRun:
    player: Player
    runner: CommanderRunner
    saved_session_id: Optional[str]
    turn: int
    brief: RunBrief


class PlanningJob:
    def __init__(self, runs: list[SideRun], listener: PlanningListener) -> None:
        self.runs = runs
        self.listener = listener
        self._threads: list[threading.Thread] = []
        self.results: dict[Player, ClaudeRunResult] = {}

    def start(self) -> None:
        for run in self.runs:
            thread = threading.Thread(
                target=self._run_side,
                args=(run,),
                name=f"{run.player.value} commander",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def cancel(self) -> None:
        for run in self.runs:
            run.runner.cancel()

    def join(self, timeout: Optional[float] = None) -> None:
        for thread in self._threads:
            thread.join(timeout)

    @property
    def is_running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def _run_side(self, run: SideRun) -> None:
        try:
            result = run.runner.run(
                run.saved_session_id,
                run.turn,
                run_message(run.brief),
                lambda event: self.listener.on_event(run.player, event),
            )
        except ClaudeNotFoundError as ex:
            result = ClaudeRunResult(error=str(ex))
        except Exception as ex:
            logging.exception("%s commander run crashed", run.player.value)
            result = ClaudeRunResult(error=f"{type(ex).__name__}: {ex}")

        self.results[run.player] = result
        if result.cancelled:
            status = PlanningStatus.CANCELLED
        elif result.success:
            status = PlanningStatus.SUCCEEDED
        else:
            status = PlanningStatus.FAILED
        self.listener.on_finished(run.player, status, result)
