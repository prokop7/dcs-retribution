"""How far a flight may get before the mission starts.

Flights that should take off before mission start are fast forwarded to where their
plan puts them at mission start and spawned in the air there. That catch-up is not
simulated with combat, so it may only cover airspace nobody contests. A flight never
gets further than:

* its package join point, or for a route without one, its first ingress, drop-off or
  target waypoint;
* the point where its route comes closer to its target than any join point the
  planner would create (the doctrine's join distance plus its minimum ingress
  distance), so moving the join point forward gains nothing;
* the point where its route enters a known enemy air defense ring it started
  outside of, so it can't be carried past a SAM it would have to fly through.

Flights without such waypoints (patrols, AEW&C, tankers, ferries) operate over
friendly airspace and have no limit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, TYPE_CHECKING

from shapely.geometry import Point as ShapelyPoint
from shapely.prepared import PreparedGeometry, prep

from game.ato.flightwaypointtype import FlightWaypointType
from game.utils import nautical_miles

if TYPE_CHECKING:
    from game.ato import Flight

#: Spacing of the points checked along each leg.
_SAMPLE_STEP_M = nautical_miles(1).meters

_CONTESTED_WAYPOINTS = {
    FlightWaypointType.DROPOFF_ZONE,
    FlightWaypointType.TARGET_POINT,
    FlightWaypointType.TARGET_GROUP_LOC,
}


@dataclass(frozen=True)
class PrestartLimit:
    """The furthest point of a route a flight may reach before mission start: on
    the leg from waypoint ``index`` to the next one, ``fraction`` of the way."""

    index: int
    fraction: float = 0.0
    reason: str = "its package join point"


def contested_waypoint(flight: Flight) -> Optional[int]:
    """Index of the join point, or the first ingress, drop-off or target waypoint
    of a route without one."""
    waypoints = flight.flight_plan.waypoints
    for index, waypoint in enumerate(waypoints):
        if waypoint.waypoint_type is FlightWaypointType.JOIN:
            return index
    for index, waypoint in enumerate(waypoints):
        if (
            waypoint.waypoint_type.name.startswith("INGRESS")
            or waypoint.waypoint_type in _CONTESTED_WAYPOINTS
        ):
            return index
    return None


def min_target_distance_m(flight: Flight) -> float:
    doctrine = flight.coalition.doctrine
    return (doctrine.join_distance + doctrine.min_ingress_distance).meters


def _air_defense_zone(flight: Flight) -> Optional[PreparedGeometry]:
    try:
        zones = flight.coalition.opponent.threat_zone
    except AssertionError:  # Not computed yet.
        return None
    if zones.air_defenses.is_empty:
        return None
    return prep(zones.air_defenses)


def prestart_limit(flight: Flight) -> Optional[PrestartLimit]:
    boundary = contested_waypoint(flight)
    if boundary is None:
        return None
    waypoints = flight.flight_plan.waypoints
    target = flight.package.target.position
    floor = min_target_distance_m(flight)
    zone = _air_defense_zone(flight)
    was_outside: Optional[bool] = None
    for index in range(boundary):
        start = waypoints[index].position
        end = waypoints[index + 1].position
        steps = max(1, math.ceil(start.distance_to_point(end) / _SAMPLE_STEP_M))
        for step in range(steps):
            fraction = step / steps
            point = start.lerp(end, fraction)
            if point.distance_to_point(target) < floor:
                return PrestartLimit(
                    index, fraction, f"{round(floor / 1852)} nm from its target"
                )
            inside = zone is not None and zone.contains(ShapelyPoint(point.x, point.y))
            if inside and was_outside:
                return PrestartLimit(index, fraction, "an enemy air defense ring")
            was_outside = not inside
    return PrestartLimit(boundary)


def time_at_limit(flight: Flight, limit: PrestartLimit) -> Optional[datetime]:
    """When the flight's plan has it at ``limit``."""
    plan = flight.flight_plan
    waypoints = plan.waypoints
    if limit.index + 1 >= len(waypoints):
        return plan.tot_for_waypoint(waypoints[limit.index])
    current, following = waypoints[limit.index], waypoints[limit.index + 1]
    leg = plan.travel_time_between_waypoints(current, following)
    arrival = plan.tot_for_waypoint(following)
    if arrival is not None:
        # Counting back from the next waypoint skips any hold at this one.
        return arrival - leg * (1 - limit.fraction)
    departure = plan.depart_time_for_waypoint(current) or plan.tot_for_waypoint(current)
    if departure is None:
        return None
    return departure + leg * limit.fraction


def elapsed_on_leg(flight: Flight, limit: PrestartLimit) -> timedelta:
    waypoints = flight.flight_plan.waypoints
    leg = flight.flight_plan.travel_time_between_waypoints(
        waypoints[limit.index], waypoints[limit.index + 1]
    )
    return leg * limit.fraction
