"""Read-only checks of flights and of the whole air plan."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from game.ato.starttype import StartType
from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.mcp.tools import NoArguments
from game.claudecommander.tools.airplan import (
    SEAD_TASKS,
    ESCORT_TASKS,
    flight_label,
    flight_summary,
    fuel_estimate,
    hms,
    mission_start,
    package_label,
    range_facts,
    route_distance_nm,
    route_threats,
)
from game.claudecommander.view import CommanderView, Json, PACKAGE_IDS

#: Keeps validate_air_plan compact on very large ATOs.
MAX_ISSUES = 80


class EstimateFlightArgs(BaseModel):
    flight_id: str


def estimate(view: CommanderView, flight_id: str) -> Json:
    flight = view.own_flight(flight_id)
    plan = flight.flight_plan
    result = flight_summary(flight)
    result["package_id"] = PACKAGE_IDS.id_for(flight.package)
    result["route_distance_nm"] = route_distance_nm(flight)
    try:
        duration = plan.landing_time - plan.takeoff_time()
        result["flight_time_min"] = round(duration.total_seconds() / 60)
    except Exception:
        pass
    in_range, distance, limit = range_facts(flight.squadron, flight.package.target)
    result["target_distance_nm"] = distance
    result["planner_range_nm"] = limit
    fuel = fuel_estimate(flight)
    result["fuel"] = fuel if fuel is not None else "no fuel data for this aircraft"
    result["known_threats_on_route"] = route_threats(view, flight)
    return result


@commander_tools.register(
    "estimate_flight",
    "Estimates for one of your flights: route distance, startup/takeoff/TOT/"
    "landing times and flight time, distance to the target against the planner's "
    "mission range, a fuel estimate (internal fuel against route burn and landing "
    "reserve), and the known enemy air defenses whose engagement range each leg "
    "enters (at the leg's altitude).",
    EstimateFlightArgs,
    read_only=True,
)
def estimate_flight(context: CommanderContext, args: EstimateFlightArgs) -> Json:
    return estimate(context.view(), args.flight_id)


def validate(view: CommanderView) -> Json:
    """Facts about the whole air plan that may need attention."""
    game = view.game
    start = mission_start(game)
    packages = view.coalition.ato.packages
    issues: list[Json] = []

    def issue(kind: str, text: str, **extra: Any) -> None:
        issues.append({"kind": kind, "text": text, **extra})

    by_target: dict[str, list[tuple[str, datetime]]] = defaultdict(list)
    pilots: Counter[int] = Counter()
    pilot_names: dict[int, str] = {}
    for package in packages:
        label = package_label(package)
        package_id = PACKAGE_IDS.id_for(package)
        if not package.flights:
            issue(
                "empty_package", f"Package {label} has no flights.", package=package_id
            )
            continue
        if package.time_over_target == datetime.min:
            issue(
                "no_tot",
                f"Package {label} has no TOT; it will be timed ASAP when planning "
                "ends.",
                package=package_id,
            )
        elif not package.target.is_friendly(view.player):
            by_target[package.target.name].append(
                (package_id, package.time_over_target)
            )
        if all(f.flight_type in ESCORT_TASKS for f in package.flights):
            issue(
                "escorts_only",
                f"Package {label} has only escort flights.",
                package=package_id,
            )
        has_sead = any(f.flight_type in SEAD_TASKS for f in package.flights)
        for flight in package.flights:
            name = flight_label(flight)
            plan = flight.flight_plan
            try:
                startup = plan.startup_time()
                if startup < start and flight.start_type is not StartType.IN_FLIGHT:
                    issue(
                        "starts_before_mission",
                        f"Flight {name} starts up at {hms(startup)}, before mission "
                        f"start {start:%H:%M:%S}; it will spawn en route.",
                        package=package_id,
                    )
            except Exception as ex:
                issue(
                    "timing_error",
                    f"Flight {name}: times cannot be computed ({ex}).",
                    package=package_id,
                )
            if flight.missing_pilots:
                issue(
                    "missing_pilots",
                    f"Flight {name} has {flight.missing_pilots} seats without a pilot.",
                    package=package_id,
                )
            if not flight.departure.runway_is_operational():
                issue(
                    "runway_closed",
                    f"Flight {name} departs from {flight.departure.name}, whose runway "
                    "is not operational.",
                    package=package_id,
                )
            if not flight.departure.is_friendly(view.player):
                issue(
                    "lost_base",
                    f"Flight {name} departs from {flight.departure.name}, which you no "
                    "longer hold.",
                    package=package_id,
                )
            threats = route_threats(view, flight)
            if threats and not has_sead:
                issue(
                    "threats_without_sead",
                    f"Flight {name} enters the engagement range of "
                    f"{', '.join(t['name'] for t in threats[:5])}"
                    f"{' and more' if len(threats) > 5 else ''} and its package "
                    "has no SEAD/DEAD flight.",
                    package=package_id,
                )
            fuel = fuel_estimate(flight)
            if fuel is not None and fuel["margin_lb"] < 0:
                issue(
                    "fuel",
                    f"Flight {name}: estimated burn plus reserve exceeds internal "
                    f"fuel by {-fuel['margin_lb']} lb (external tanks: "
                    f"{fuel['external_fuel_tanks']}).",
                    package=package_id,
                )
            in_range, distance, limit = range_facts(flight.squadron, package.target)
            if not in_range:
                issue(
                    "range",
                    f"Flight {name}: target {distance} nm from base, planner range "
                    f"{limit} nm.",
                    package=package_id,
                )
            for pilot in flight.roster.iter_pilots():
                if pilot is not None:
                    pilots[id(pilot)] += 1
                    pilot_names[id(pilot)] = pilot.name
    for squadron in view.coalition.air_wing.iter_squadrons():
        if squadron.untasked_aircraft < 0:
            issue(
                "overassigned_aircraft",
                f"Squadron {squadron.name} has {-squadron.untasked_aircraft} more "
                "aircraft tasked than it owns.",
            )
    for pilot_id, count in pilots.items():
        if count > 1:
            issue(
                "double_assigned_pilot",
                f"Pilot {pilot_names[pilot_id]} is assigned to {count} flights.",
            )
    for target, entries in by_target.items():
        if len(entries) > 1:
            entries.sort(key=lambda e: e[1])
            issue(
                "same_target",
                f"{len(entries)} packages against {target}: "
                + ", ".join(f"{pid} TOT {hms(tot)}" for pid, tot in entries),
            )
    locked = [PACKAGE_IDS.id_for(p) for p in packages if p.player_locked]
    result: Json = {
        "packages": len(packages),
        "flights": sum(len(p.flights) for p in packages),
        "issues": issues[:MAX_ISSUES],
    }
    if len(issues) > MAX_ISSUES:
        result["issues_omitted"] = len(issues) - MAX_ISSUES
    if locked:
        result["player_locked_packages"] = locked
    return result


@commander_tools.register(
    "validate_air_plan",
    "Checks your whole air tasking order and lists facts that may need attention: "
    "empty or escort-only packages, packages without TOT, several packages against "
    "the same target (with TOTs), flights starting before mission start, missing "
    "pilots, closed runways or lost bases, routes entering known air defense "
    "ranges in packages without SEAD/DEAD, fuel and range concerns, over-assigned "
    "aircraft or pilots, and packages locked by the player. No scoring.",
    NoArguments,
    read_only=True,
)
def validate_air_plan(context: CommanderContext, _args: NoArguments) -> Json:
    return validate(context.view())
