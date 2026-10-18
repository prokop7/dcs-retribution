from typing import Any
from unittest.mock import MagicMock

from game.missiongenerator.aircraft.waypoints.waypointgenerator import (
    WaypointGenerator,
)


def _wp(eta_locked: bool, speed_locked: bool, name: str = "") -> Any:
    wp = MagicMock()
    wp.ETA_locked = eta_locked
    wp.speed_locked = speed_locked
    wp.name = name
    return wp


def _generator(points: list[Any]) -> WaypointGenerator:
    # Bypass __init__ (it needs a full flight/mission); only group.points and
    # flight are consulted by the method under test.
    gen: Any = object.__new__(WaypointGenerator)
    gen.group = MagicMock()
    gen.group.points = points
    gen.flight = MagicMock()
    return gen


def test_unlocks_speed_locked_waypoint_trapped_between_tots() -> None:
    """A carrier escort's JOIN waypoint gets a locked speed (its TOT clamps to
    the mission start, ETA == 0) while trapped between TOT-locked waypoints,
    which DCS rejects. The speed lock must be dropped while the times stay."""
    # spawn, JOIN (trapped), ESCORT HOLD, SPLIT, REFUEL, LANDING
    points = [
        _wp(eta_locked=True, speed_locked=True, name="spawn"),
        _wp(eta_locked=True, speed_locked=True, name="JOIN"),
        _wp(eta_locked=True, speed_locked=False, name="ESCORT HOLD"),
        _wp(eta_locked=False, speed_locked=True, name="SPLIT"),
        _wp(eta_locked=False, speed_locked=True, name="REFUEL"),
        _wp(eta_locked=False, speed_locked=True, name="LANDING"),
    ]

    _generator(points)._resolve_locked_speed_time_conflicts()

    # Only JOIN is unlocked; the spawn (nothing before it) and the SPLIT/RTB legs
    # (no TOT-locked waypoint after them) keep their locked speed.
    assert [p.speed_locked for p in points] == [True, False, False, True, True, True]
    # Times are untouched.
    assert all(p.ETA_locked for p in points[:3])


def test_leaves_untrapped_speed_locks_alone() -> None:
    """A normal escort (JOIN has a real TOT, so no speed lock there) is unchanged:
    the split/RTB legs are not trapped (no TOT-locked waypoint follows them)."""
    points = [
        _wp(eta_locked=True, speed_locked=True, name="spawn"),
        _wp(eta_locked=True, speed_locked=False, name="JOIN"),
        _wp(eta_locked=True, speed_locked=False, name="ESCORT HOLD"),
        _wp(eta_locked=False, speed_locked=True, name="SPLIT"),
        _wp(eta_locked=False, speed_locked=True, name="REFUEL"),
        _wp(eta_locked=False, speed_locked=True, name="LANDING"),
    ]

    _generator(points)._resolve_locked_speed_time_conflicts()

    assert [p.speed_locked for p in points] == [True, False, False, True, True, True]


def test_unlocks_a_run_of_trapped_speed_locked_waypoints() -> None:
    """Every locked-speed waypoint in a run bounded by TOT-locked waypoints on
    both sides is unlocked."""
    points = [
        _wp(eta_locked=True, speed_locked=False, name="A"),
        _wp(eta_locked=False, speed_locked=True, name="B"),
        _wp(eta_locked=False, speed_locked=True, name="C"),
        _wp(eta_locked=True, speed_locked=False, name="D"),
    ]

    _generator(points)._resolve_locked_speed_time_conflicts()

    assert [p.speed_locked for p in points] == [False, False, False, False]


def _timed(eta: int, eta_locked: bool, speed_locked: bool, name: str) -> Any:
    wp = _wp(eta_locked, speed_locked, name)
    wp.ETA = eta
    return wp


def test_unlocks_times_already_past_at_mission_start() -> None:
    """A patrol already on station at mission start: its race track start time
    has passed, so its ETA is clamped to 0 and its speed locked. Time-locked as
    well, it sits at 0 next to the spawn point, which DCS rejects."""
    points = [
        _timed(0, eta_locked=True, speed_locked=True, name="spawn"),
        _timed(0, eta_locked=True, speed_locked=True, name="RACETRACK START"),
        _timed(0, eta_locked=False, speed_locked=True, name="RACETRACK END"),
        _timed(0, eta_locked=False, speed_locked=True, name="LANDING"),
    ]

    generator = _generator(points)
    generator._unlock_past_times()
    generator._resolve_locked_speed_time_conflicts()

    # The spawn point keeps its time (it sets when the group appears); the race
    # track start is flown at its planned speed.
    assert [p.ETA_locked for p in points] == [True, False, False, False]
    assert [p.speed_locked for p in points] == [True, True, True, True]


def test_keeps_times_still_ahead() -> None:
    points = [
        _timed(0, eta_locked=True, speed_locked=True, name="spawn"),
        _timed(600, eta_locked=True, speed_locked=False, name="RACETRACK START"),
        _timed(0, eta_locked=False, speed_locked=True, name="RACETRACK END"),
    ]

    _generator(points)._unlock_past_times()

    assert [p.ETA_locked for p in points] == [True, True, False]
    assert [p.speed_locked for p in points] == [True, False, True]
