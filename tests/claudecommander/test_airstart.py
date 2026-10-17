"""Flights that should already be under way at mission start spawn in the air."""

from __future__ import annotations

from datetime import timedelta

from game import Game
from game.ato.flight import Flight
from game.ato.flightstate import Completed, InFlight, Uninitialized, WaitingForStart
from game.ato.starttype import StartType
from game.claudecommander.tools.airplan import state_at_mission_start
from game.sim import GameUpdateEvents
from game.sim.aircraftsimulation import settle_initial_state


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


def test_future_flights_wait_and_finished_flights_are_reported(game: Game) -> None:
    flight = long_flight(game)
    start = game.conditions.start_time
    plan = flight.flight_plan
    flight.package.time_over_target = start + timedelta(hours=2)
    settle(game, flight)
    assert isinstance(flight.state, WaitingForStart)
    assert state_at_mission_start(game, flight) is None

    flight.package.time_over_target = start - timedelta(hours=6)
    settle(game, flight)
    assert isinstance(flight.state, Completed)
    where = state_at_mission_start(game, flight)
    assert where is not None and "dropped" in where
    # The probe restores whatever state the flight had.
    assert isinstance(flight.state, Completed)
