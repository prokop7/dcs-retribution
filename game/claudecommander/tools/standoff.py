"""Standoff attacks: release from a launch point outside the target area."""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

from game.ato.flightplans.custom import CustomFlightPlan
from game.ato.standoff import (
    MIN_LAUNCH_DISTANCE,
    REACH_MARGIN,
    STANDOFF_TASKS,
    flight_reach,
)
from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.domains import Domain, require_domain
from game.claudecommander.mcp.tools import ToolError
from game.claudecommander.tools.airplan import (
    editable_flight,
    flight_summary,
    flight_warnings,
    package_updated,
    refresh_asap,
    timing_warnings,
)
from game.claudecommander.view import Json

if TYPE_CHECKING:
    from game.ato.flight import Flight


class StandoffArgs(BaseModel):
    flight_id: str
    enabled: bool = Field(
        default=True, description="false turns the standoff attack off again."
    )
    launch_distance_nm: Optional[float] = Field(
        default=None,
        ge=MIN_LAUNCH_DISTANCE.nautical_miles,
        le=1500,
        description="Distance from the target to release at. Default: "
        f"{REACH_MARGIN:.0%} of the loadout's weapon reach for the task (see "
        "lookup_aircraft reach_nm), re-derived when the loadout changes.",
    )


def rebuild_plan(flight: Flight) -> None:
    try:
        flight.recreate_flight_plan()
    except Exception as ex:
        logging.exception("Commander standoff flight planning failed")
        raise ToolError(f"Could not rebuild the flight plan: {ex}")


@commander_tools.register(
    "set_standoff_attack",
    "Makes a Strike, DEAD, BAI, Anti-ship or OCA/Runway flight attack from "
    "standoff: its ingress point, where the AI is given its attack tasks, moves "
    "out along the approach to a launch point at launch_distance_nm from the "
    "target (default: derived from the loadout's weapon reach), and after the "
    "attack the AI turns for its split point instead of continuing to the target. "
    "The package TOT stays the time over target: the flight passes the launch "
    "point earlier by its own flying time from there to the target, so weapons "
    "flying about as fast as the aircraft arrive around TOT. The launch point "
    "stays after the package join point. Rebuilds the flight's route (earlier "
    "waypoint edits to this flight are lost). Off by default; the game's own "
    "planner never uses it. Where the AI actually releases is up to DCS: with a "
    "weapon that doesn't reach from the launch point it flies closer. "
    "enabled=false restores the normal attack.",
    StandoffArgs,
)
def set_standoff_attack(context: CommanderContext, args: StandoffArgs) -> Json:
    require_domain(context.game, context.player, Domain.AIR_TASKING)
    view = context.view()
    flight = editable_flight(view, args.flight_id)
    previous = (flight.standoff_attack, flight.standoff_launch_distance_nm)
    if args.enabled:
        if flight.flight_type not in STANDOFF_TASKS:
            raise ToolError(
                f"{flight.flight_type.value} flights cannot attack from standoff. "
                "Standoff attacks are for "
                f"{', '.join(sorted(t.value for t in STANDOFF_TASKS))}."
            )
        if isinstance(flight.flight_plan, CustomFlightPlan):
            raise ToolError(
                f"Flight {flight.id} has a custom flight plan, which is not rebuilt "
                "with a launch point. Delete and re-create the flight first."
            )
        if args.launch_distance_nm is None and flight_reach(flight) is None:
            raise ToolError(
                f"The loadout of flight {flight.id} has no weapon with known reach "
                f"for {flight.flight_type.value}. Set a loadout with standoff "
                "weapons first (lookup_aircraft lists each loadout's reach_nm) or "
                "give launch_distance_nm."
            )
        flight.standoff_attack = True
        flight.standoff_launch_distance_nm = args.launch_distance_nm
    else:
        flight.standoff_attack = False
        flight.standoff_launch_distance_nm = None
    try:
        rebuild_plan(flight)
    except ToolError:
        flight.standoff_attack, flight.standoff_launch_distance_nm = previous
        rebuild_plan(flight)
        raise
    refresh_asap(context.game, flight.package)
    context.events.update_flight(flight)
    package_updated(context.events, flight.package)
    result: Json = {"flight": flight_summary(flight)}
    warnings = flight_warnings(view, flight)
    if not flight.package.auto_asap:
        warnings.extend(timing_warnings(context.game, flight.package))
    if warnings:
        result["warnings"] = warnings
    return result
