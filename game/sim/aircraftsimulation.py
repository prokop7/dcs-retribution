from __future__ import annotations

import itertools
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Optional

from typing_extensions import TYPE_CHECKING

from game.ato.flightstate import Uninitialized, Completed, InCombat
from game.settings.settings import FastForwardStopCondition, CombatResolutionMethod
from .combat import CombatInitiator, FrozenCombat
from .simulationresults import SimulationResults

if TYPE_CHECKING:
    from game import Game
    from game.ato import Flight
    from .gameupdateevents import GameUpdateEvents


#: Enough for startup -> taxi -> takeoff -> navigating, with margin.
_MAX_SETTLE_STEPS = 8


def uncontested_limit(flight: Flight) -> Optional[int]:
    """Index of the furthest waypoint a flight may reach before mission start.

    Time before mission start is not simulated with combat, so it may only cover
    the part of a route nobody contests: up to the package's join point, or, for a
    route without one, its first ingress, drop-off or target waypoint. Flights
    without such waypoints (patrols, AEW&C, tankers, ferries) operate over
    friendly airspace and have no limit.
    """
    from game.ato.flightwaypointtype import FlightWaypointType

    waypoints = flight.flight_plan.waypoints
    for index, waypoint in enumerate(waypoints):
        if waypoint.waypoint_type is FlightWaypointType.JOIN:
            return index
    contested = {
        FlightWaypointType.DROPOFF_ZONE,
        FlightWaypointType.TARGET_POINT,
        FlightWaypointType.TARGET_GROUP_LOC,
    }
    for index, waypoint in enumerate(waypoints):
        if (
            waypoint.waypoint_type.name.startswith("INGRESS")
            or waypoint.waypoint_type in contested
        ):
            return index
    return None


def settle_initial_state(
    flight: Flight, now: datetime, events: GameUpdateEvents
) -> None:
    """Puts a flight where its plan says it is at ``now``.

    A flight whose startup time is before ``now`` starts in StartUp, Taxi or Takeoff
    as of its own startup time. Ticking it with zero duration lets each phase end at
    its own completion time and roll the overrun into the next, so a flight that
    should have taken off before mission start ends up navigating at its planned
    position (and is spawned in the air there) whatever the fast forward setting.

    Combat is not evaluated during this catch-up, so a flight is never advanced past
    its uncontested limit (see ``uncontested_limit``): a flight that would be
    further along spawns at that point and flies the contested part during the
    mission, arriving late, instead of appearing next to its target.
    """
    flight.state.reinitialize(now)
    for _ in range(_MAX_SETTLE_STEPS):
        state = flight.state
        if state.is_waiting_for_start:
            return
        state.on_game_tick(events, now, timedelta())
        if flight.state is state:
            break
    _hold_at_uncontested_limit(flight)


def _hold_at_uncontested_limit(flight: Flight) -> None:
    from game.ato.flightstate import Navigating
    from game.ato.flightstate.inflight import InFlight

    limit = uncontested_limit(flight)
    if limit is None:
        return
    state = flight.state
    if isinstance(state, InFlight):
        if state.waypoint_index < limit:
            return
        if state.waypoint_index == limit and state.elapsed_time <= timedelta():
            return
    elif not isinstance(state, Completed):
        return
    flight.set_state(Navigating(flight, state.settings, waypoint_index=limit))


class AircraftSimulation:
    def __init__(self, game: Game) -> None:
        self.game = game
        self.combats: list[FrozenCombat] = []
        self.results = SimulationResults()

    def begin_simulation(self) -> None:
        self.reset()
        self.set_initial_flight_states()

    def on_game_tick(
        self,
        events: GameUpdateEvents,
        time: datetime,
        duration: timedelta,
        combat_resolution_method: CombatResolutionMethod,
        force_continue: bool,
    ) -> None:
        if (
            not self._auto_resolve_combat(combat_resolution_method, force_continue)
            and self.combats
        ):
            logging.error(
                "Cannot resume simulation because aircraft are in combat and "
                "auto-resolve is disabled"
            )
            events.complete_simulation()
            return

        still_active = []
        for combat in self.combats:
            if combat.on_game_tick(
                time,
                duration,
                self.results,
                events,
                combat_resolution_method,
            ):
                events.end_combat(combat)
            else:
                still_active.append(combat)
        self.combats = still_active

        for flight in self.iter_flights():
            flight.on_game_tick(events, time, duration)

        # Finish updating all flights before checking for combat so that the new
        # positions are used.
        CombatInitiator(self.game, self.combats, events).update_active_combats()

        # After updating all combat states, check for halts.
        for flight in self.iter_flights():
            if flight.should_halt_sim() and not force_continue:
                events.complete_simulation()
                return

        # Find completed flights, removing them from the ATO and returning aircraft
        # and pilots back to the squadron.
        for flight in self.iter_flights():
            if type(flight.state) == Completed:
                flight.package.remove_flight(flight)
                if len(flight.package.flights) == 0:
                    flight.squadron.coalition.ato.remove_package(flight.package)

        if (
            not self._auto_resolve_combat(combat_resolution_method, force_continue)
            and self.combats
        ):
            events.complete_simulation()

    def set_initial_flight_states(self) -> None:
        from .gameupdateevents import GameUpdateEvents

        now = self.game.conditions.start_time
        events = GameUpdateEvents()
        for flight in self.iter_flights():
            settle_initial_state(flight, now, events)

    def reset(self) -> None:
        for flight in self.iter_flights():
            flight.set_state(Uninitialized(flight, self.game.settings))

    def iter_flights(self) -> Iterator[Flight]:
        packages = itertools.chain(
            self.game.blue.ato.packages, self.game.red.ato.packages
        )
        for package in packages:
            yield from package.flights

    def _auto_resolve_combat(
        self, combat_resolution_method: CombatResolutionMethod, force_continue: bool
    ) -> bool:
        if force_continue:
            return True
        return combat_resolution_method != CombatResolutionMethod.PAUSE
