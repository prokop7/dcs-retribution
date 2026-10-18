"""Flights that should already be under way at mission start spawn in the air."""

from __future__ import annotations

from datetime import timedelta

import pytest

from game import Game
from game.ato.flight import Flight
from game.ato.flightstate import (
    Completed,
    InFlight,
    Navigating,
    Uninitialized,
    WaitingForStart,
)
from game.ato.starttype import StartType
from game.claudecommander.tools.airplan import state_at_mission_start
from game.sim import GameUpdateEvents
from game.sim.aircraftsimulation import settle_initial_state
from game.sim.prestart import (
    contested_waypoint,
    elapsed_on_leg,
    min_target_distance_m,
    prestart_limit,
)
from game.utils import nautical_miles


def long_flight(game: Game) -> Flight:
    """A planned flight whose takeoff is well before its TOT."""
    start = game.conditions.start_time
    best = None
    for coalition in (game.blue, game.red):
        for package in coalition.ato.packages:
            for flight in package.flights:
                plan = flight.flight_plan
                transit = plan.tot - plan.takeoff_time()
                if best is None or transit > best[0]:
                    best = (transit, flight)
    assert best is not None and best[0] > timedelta(minutes=8), best
    flight = best[1]
    assert flight.package.time_over_target >= start
    return flight


def settle(game: Game, flight: Flight) -> None:
    flight.set_state(Uninitialized(flight, game.settings))
    settle_initial_state(flight, game.conditions.start_time, GameUpdateEvents())


def test_flight_that_took_off_before_start_is_airborne(game: Game) -> None:
    flight = long_flight(game)
    start = game.conditions.start_time
    plan = flight.flight_plan
    # Move the TOT so that takeoff is five minutes before mission start.
    flight.package.time_over_target = (
        start + (plan.tot - plan.takeoff_time()) - (timedelta(minutes=5))
    )
    for start_type in (StartType.COLD, StartType.WARM, StartType.RUNWAY):
        flight.start_type = start_type
        settle(game, flight)
        assert isinstance(flight.state, InFlight), (start_type, flight.state)
        assert flight.state.waypoint_index >= 1

    where = state_at_mission_start(game, flight)
    assert where is not None and "airborne at mission start" in where


def test_air_start_catches_up_on_time_already_flown(game: Game) -> None:
    flight = long_flight(game)
    start = game.conditions.start_time
    flight.start_type = StartType.IN_FLIGHT
    plan = flight.flight_plan
    flight.package.time_over_target = (
        start + (plan.tot - plan.takeoff_time()) - (timedelta(minutes=5))
    )
    settle(game, flight)
    assert isinstance(flight.state, InFlight)
    # Without the catch-up an air start sits at its first waypoint with nothing
    # flown; five minutes of flight must show.
    assert flight.state.waypoint_index > 0 or flight.state.elapsed_time >= timedelta(
        minutes=5
    )


def test_future_flights_wait_and_no_flight_skips_the_contested_part(
    game: Game,
) -> None:
    flight = long_flight(game)
    start = game.conditions.start_time
    flight.package.time_over_target = start + timedelta(hours=2)
    settle(game, flight)
    assert isinstance(flight.state, WaitingForStart)
    assert state_at_mission_start(game, flight) is None

    # A TOT long before mission start would have the flight finish its whole
    # mission unopposed. It is held at its pre-start limit instead.
    flight.package.time_over_target = start - timedelta(hours=6)
    settle(game, flight)
    limit = prestart_limit(flight)
    assert limit is not None
    assert isinstance(flight.state, Navigating)
    assert flight.state.waypoint_index == limit.index
    assert flight.state.elapsed_time == elapsed_on_leg(flight, limit)
    join = contested_waypoint(flight)
    assert join is not None and limit.index <= join
    assert flight.flight_plan.waypoints[join].waypoint_type.name == "JOIN"
    assert distance_to_target_nm(flight) >= min_target_nm(flight) - 1.1


def test_moving_the_join_point_forward_gains_nothing(game: Game) -> None:
    flight = long_flight(game)
    plan = flight.flight_plan
    join_index = contested_waypoint(flight)
    assert join_index is not None
    join = plan.waypoints[join_index]
    target = flight.package.target.position
    # Drag the join point to 5 nm from the target.
    join.position = target.point_from_heading(
        target.heading_between_point(join.position), nautical_miles(5).meters
    )
    flight.package.time_over_target = game.conditions.start_time - timedelta(hours=6)
    settle(game, flight)
    limit = prestart_limit(flight)
    assert limit is not None and limit.reason != "its package join point"
    assert distance_to_target_nm(flight) >= min_target_nm(flight) - 1.1


def test_pre_start_time_does_not_carry_a_flight_into_a_sam_ring(
    game: Game, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shapely.geometry import Point as ShapelyPoint
    from shapely.prepared import prep

    import game.sim.prestart as prestart

    flight = long_flight(game)
    waypoints = flight.flight_plan.waypoints
    join_index = contested_waypoint(flight)
    assert join_index is not None and join_index >= 2
    # A 5 nm SAM ring in the middle of the leg before the join point.
    a = waypoints[join_index - 1].position
    b = waypoints[join_index].position
    middle = a.lerp(b, 0.5)
    radius = min(nautical_miles(5).meters, a.distance_to_point(b) / 4)
    ring = ShapelyPoint(middle.x, middle.y).buffer(radius)
    monkeypatch.setattr(prestart, "_air_defense_zone", lambda flight: prep(ring))

    limit = prestart_limit(flight)
    assert limit is not None and limit.reason == "an enemy air defense ring"
    assert limit.index == join_index - 1 and 0 < limit.fraction < 0.5
    flight.package.time_over_target = game.conditions.start_time - timedelta(hours=6)
    settle(game, flight)
    assert isinstance(flight.state, Navigating)
    position = flight.state.estimate_position()
    assert position.distance_to_point(middle) >= radius - nautical_miles(1).meters


def test_tools_refuse_a_tot_that_skips_the_contested_part(game: Game) -> None:
    from tests.claudecommander.test_air_tools import Caller

    flight = long_flight(game)
    package = flight.package
    player = flight.squadron.player
    package.player_locked = False
    caller = Caller(game, player)
    package_id = caller.ok("get_ato", {})["packages"][0]["id"]
    for p in caller.ok("get_ato", {})["packages"]:
        if p["target"]["name"] == package.target.name:
            package_id = p["id"]
    before = package.time_over_target

    text = caller.error(
        "set_package_tot", {"package_id": package_id, "minutes_after_start": 1}
    )
    assert "Earliest allowed TOT" in text
    assert package.time_over_target == before

    validation = caller.ok("validate_air_plan", {})
    assert not any(i["kind"] == "tot_too_early" for i in validation["issues"])

    # The reported earliest TOT is accepted as given.
    earliest = text.rsplit("Earliest allowed TOT: ", 1)[1].rstrip(".")
    caller.ok("set_package_tot", {"package_id": package_id, "tot": earliest})


def distance_to_target_nm(flight: Flight) -> float:
    state = flight.state
    assert isinstance(state, InFlight)
    position = state.estimate_position()
    return position.distance_to_point(flight.package.target.position) / 1852


def min_target_nm(flight: Flight) -> float:
    return min_target_distance_m(flight) / 1852
