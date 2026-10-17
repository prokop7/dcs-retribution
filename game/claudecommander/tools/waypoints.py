"""Editing flight routes.

Follows what the player can do in the flight's waypoint tab and on the map:
altitudes are set per waypoint, navigation points are inserted where the flight
plan layout supports them, waypoints are moved (moving the package's join, split
or ingress point on its primary flight moves it for the whole package, like the
map does) and deleted. Deleting anything but a navigation, divert or refuel point
converts the flight to a custom flight plan, as the waypoint tab does after
asking; that needs ``allow_custom_flight_plan``.
"""

from __future__ import annotations

import json
from typing import Literal, Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

from game.ato.flightplans.custom import CustomFlightPlan
from game.ato.flightplans.formationattack import FormationAttackLayout
from game.ato.flightplans.standard import StandardLayout
from game.ato.flightplans.waypointbuilder import WaypointBuilder
from game.ato.flightwaypointtype import FlightWaypointType
from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.domains import Domain, require_domain
from game.claudecommander.mcp.tools import ToolError
from game.claudecommander.tools.airplan import (
    AIRFIELD_WAYPOINTS,
    CRITICAL_WAYPOINTS,
    editable_flight,
    flight_summary,
    flight_warnings,
    package_updated,
    refresh_asap,
    route_json,
)
from game.claudecommander.tools.common import PointRef
from game.claudecommander.view import CommanderView, Json
from game.server.waypoints.routes import update_package_waypoints_if_primary_flight
from game.utils import feet

if TYPE_CHECKING:
    from game.ato.flight import Flight
    from game.ato.flightwaypoint import FlightWaypoint
    from game.sim import GameUpdateEvents

#: Waypoint types the standard layouts can insert a navigation point after.
_INSERT_AFTER = {
    FlightWaypointType.TAKEOFF,
    FlightWaypointType.LOITER,
    FlightWaypointType.SPLIT,
    FlightWaypointType.REFUEL,
    FlightWaypointType.PATROL,
    FlightWaypointType.EGRESS,
    FlightWaypointType.NAV,
}


class WaypointOperation(BaseModel):
    op: Literal["set_altitude", "insert", "move", "delete"] = Field(
        description="set_altitude: altitude (and reference) of waypoint `index`. "
        "insert: new navigation point after waypoint `index`, at `point` (default: "
        "halfway to the next waypoint). move: waypoint `index` to `point`. delete: "
        "waypoint `index`."
    )
    index: int = Field(ge=0, description="Waypoint index in the route (0 = takeoff).")
    altitude_ft: Optional[float] = Field(
        default=None, ge=0, le=60000, description="For set_altitude and insert."
    )
    altitude_reference: Optional[Literal["MSL", "AGL"]] = Field(
        default=None,
        description="For set_altitude and insert. Default: unchanged / MSL.",
    )
    point: Optional[PointRef] = Field(default=None, description="For insert and move.")


class EditWaypointsArgs(BaseModel):
    flight_id: str
    operations: list[WaypointOperation] = Field(min_length=1, max_length=30)
    allow_custom_flight_plan: bool = Field(
        default=False,
        description="Allow deletions that convert the flight to a custom flight "
        "plan. Custom plans keep the waypoints but are no longer re-timed with the "
        "package's join/split schedule.",
    )


def _waypoint(flight: Flight, index: int) -> FlightWaypoint:
    waypoints = flight.flight_plan.waypoints
    if index >= len(waypoints):
        raise ToolError(
            f"Waypoint index {index} is out of range: the route has waypoints 0-"
            f"{len(waypoints) - 1}."
        )
    return waypoints[index]


def _insertable_after(flight: Flight) -> list[int]:
    plan = flight.flight_plan
    waypoints = plan.waypoints
    if isinstance(plan, CustomFlightPlan):
        return list(range(len(waypoints)))
    layout = plan.layout
    if not isinstance(layout, StandardLayout):
        return []
    result = []
    for index, waypoint in enumerate(waypoints):
        following = waypoints[index + 1] if index + 1 < len(waypoints) else None
        if following is not None and following.waypoint_type in (
            FlightWaypointType.LOITER,
            FlightWaypointType.REFUEL,
        ):
            # The layout puts the new point after the hold / refuel point.
            continue
        if waypoint.waypoint_type is FlightWaypointType.NAV:
            if waypoint in layout.nav_to or waypoint in layout.nav_from:
                result.append(index)
        elif waypoint.waypoint_type in _INSERT_AFTER:
            result.append(index)
    return result


def _set_altitude(waypoint: FlightWaypoint, op: WaypointOperation) -> None:
    if op.altitude_ft is None:
        raise ToolError("set_altitude needs altitude_ft.")
    if waypoint.waypoint_type in AIRFIELD_WAYPOINTS:
        raise ToolError(
            f"The altitude of a {waypoint.waypoint_type.name} waypoint belongs to its "
            "airfield and cannot be set."
        )
    waypoint.alt = feet(op.altitude_ft)
    if op.altitude_reference is not None:
        waypoint.alt_type = "RADIO" if op.altitude_reference == "AGL" else "BARO"


def _insert(view: CommanderView, flight: Flight, op: WaypointOperation) -> None:
    plan = flight.flight_plan
    waypoints = plan.waypoints
    after = _waypoint(flight, op.index)
    following = waypoints[op.index + 1] if op.index + 1 < len(waypoints) else None
    point = op.point.resolve(view)[0] if op.point is not None else None
    if isinstance(plan, CustomFlightPlan):
        new = StandardLayout.get_midpoint(after, following)
        plan.layout.custom_waypoints.insert(op.index, new)
    else:
        layout = plan.layout
        valid = _insertable_after(flight)
        if not isinstance(layout, StandardLayout) or op.index not in valid:
            raise ToolError(
                f"Cannot insert a navigation point after waypoint {op.index} "
                f"({after.waypoint_type.name}) in this flight plan. Valid indices: "
                f"{valid or 'none'}."
            )
        before = set(map(id, waypoints))
        if not layout.add_waypoint(after, following):
            raise ToolError(
                f"Cannot insert a navigation point after waypoint {op.index}. Valid "
                f"indices: {valid}."
            )
        added = [w for w in plan.waypoints if id(w) not in before]
        if len(added) != 1:
            raise ToolError("The new waypoint could not be located.")
        new = added[0]
    if point is not None:
        new.position = point
    if op.altitude_ft is not None:
        # Same altitude reference rule as the game's own navigation points (AGL at
        # low altitude) unless one is given.
        default = WaypointBuilder.nav(new.position, feet(op.altitude_ft))
        new.alt = default.alt
        new.alt_type = default.alt_type
    if op.altitude_reference is not None:
        new.alt_type = "RADIO" if op.altitude_reference == "AGL" else "BARO"


def _move(
    view: CommanderView,
    flight: Flight,
    op: WaypointOperation,
    events: GameUpdateEvents,
) -> None:
    waypoint = _waypoint(flight, op.index)
    if op.point is None:
        raise ToolError("move needs point.")
    if waypoint.waypoint_type in AIRFIELD_WAYPOINTS | CRITICAL_WAYPOINTS:
        raise ToolError(
            f"A {waypoint.waypoint_type.name} waypoint cannot be moved: it is tied to "
            "an airfield or the target."
        )
    waypoint.position = op.point.resolve(view)[0]
    update_package_waypoints_if_primary_flight(waypoint, flight, events)


def _delete(flight: Flight, op: WaypointOperation, allow_custom: bool) -> None:
    waypoint = _waypoint(flight, op.index)
    plan = flight.flight_plan
    layout = plan.layout
    kind = waypoint.waypoint_type
    if kind in CRITICAL_WAYPOINTS:
        if (
            isinstance(layout, FormationAttackLayout)
            and waypoint in layout.targets
            and len(layout.targets) > 1
        ):
            layout.targets.remove(waypoint)
            return
        raise ToolError(
            f"A {kind.name} waypoint cannot be deleted: the flight needs it."
        )
    if waypoint is plan.tot_waypoint:
        raise ToolError(
            f"Waypoint {op.index} ({kind.name}) is the flight's time-over-target "
            "point and cannot be deleted."
        )
    if layout.delete_waypoint(waypoint):
        return
    if not allow_custom:
        raise ToolError(
            f"Deleting waypoint {op.index} ({kind.name}) converts the flight to a "
            "custom flight plan, which is no longer re-timed with the package. Set "
            "allow_custom_flight_plan=true to do it."
        )
    if not isinstance(plan, CustomFlightPlan):
        flight.degrade_to_custom_flight_plan()
    custom = flight.flight_plan
    assert isinstance(custom, CustomFlightPlan)
    custom.layout.custom_waypoints.remove(waypoint)


@commander_tools.register(
    "edit_flight_waypoints",
    "Edits a flight's route with a list of operations run in order (indices refer "
    "to the route as left by the previous operation; get_ato with package_id shows "
    "the route). set_altitude (ft, MSL or AGL), insert a navigation point after a "
    "waypoint, move a waypoint (moving the package's join/split/ingress on its "
    "primary flight moves it for the package's other flights too), delete a "
    "waypoint. Takeoff, landing and target waypoints cannot be deleted or moved; "
    "airfield waypoints keep their altitude. Ground speeds are not editable: the "
    "game derives them from the package timing. Returns the new route, times and "
    "threat/fuel warnings.",
    EditWaypointsArgs,
)
def edit_flight_waypoints(context: CommanderContext, args: EditWaypointsArgs) -> Json:
    require_domain(context.game, context.player, Domain.AIR_TASKING)
    view = context.view()
    flight = editable_flight(view, args.flight_id)
    applied = 0
    try:
        for op in args.operations:
            waypoint = _waypoint(flight, op.index)
            if op.op == "set_altitude":
                _set_altitude(waypoint, op)
            elif op.op == "insert":
                _insert(view, flight, op)
            elif op.op == "move":
                _move(view, flight, op, context.events)
            else:
                _delete(flight, op, args.allow_custom_flight_plan)
            applied += 1
    except ToolError as ex:
        if applied:
            refresh_asap(context.game, flight.package)
            context.events.update_flight(flight)
            package_updated(context.events, flight.package)
            route = json.dumps(route_json(flight), separators=(",", ":"))
            raise ToolError(
                f"Operation {applied + 1} failed: {ex} Operations 1-{applied} were "
                f"applied. Current route: {route}"
            )
        raise ToolError(f"Operation 1 failed: {ex}")
    refresh_asap(context.game, flight.package)
    context.events.update_flight(flight)
    package_updated(context.events, flight.package)
    result: Json = {"flight": flight_summary(flight)}
    warnings = flight_warnings(view, flight)
    if warnings:
        result["warnings"] = warnings
    return result
