"""Helpers shared by the air planning tools.

Nothing here is registered as a tool. The helpers resolve and validate ids,
time packages, describe routes and look for known threats along them.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

from game.ato.flighttype import FlightType
from game.ato.flightwaypointtype import FlightWaypointType
from game.ato.starttype import StartType
from game.ato.standoff import standoff_applies
from game.claudecommander.mcp.tools import ToolError
from game.claudecommander.tools.reach import standoff_facts, standoff_json
from game.claudecommander.view import (
    CommanderView,
    Json,
    METERS_PER_NM,
    PACKAGE_IDS,
    segment_circle_crossing,
    to_ft,
    to_nm,
)
from game.utils import meters, nautical_miles

if TYPE_CHECKING:
    from game import Game
    from game.ato.flight import Flight
    from game.ato.flightwaypoint import FlightWaypoint
    from game.ato.package import Package
    from game.sim import GameUpdateEvents
    from game.squadrons import Squadron
    from game.theater import ControlPoint, MissionTarget

LB_PER_KG = 2.20462

#: Escorts are planned after the flights they protect, like PackageFulfiller does,
#: because their routes follow the package waypoints the main flights define.
ESCORT_TASKS = {FlightType.ESCORT, FlightType.SEAD_ESCORT}

#: Tasks that suppress or destroy air defenses.
SEAD_TASKS = {
    FlightType.SEAD,
    FlightType.SEAD_ESCORT,
    FlightType.SEAD_SWEEP,
    FlightType.DEAD,
}

#: Waypoints a commander may not delete or move.
CRITICAL_WAYPOINTS = {
    FlightWaypointType.TAKEOFF,
    FlightWaypointType.LANDING_POINT,
    FlightWaypointType.TARGET_POINT,
    FlightWaypointType.TARGET_GROUP_LOC,
    FlightWaypointType.TARGET_SHIP,
}

#: Waypoints whose position or altitude belongs to an airfield or fixed reference.
AIRFIELD_WAYPOINTS = {
    FlightWaypointType.TAKEOFF,
    FlightWaypointType.LANDING_POINT,
    FlightWaypointType.DIVERT,
    FlightWaypointType.BULLSEYE,
}

_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


# Ids and locks ---------------------------------------------------------------------


def package_label(package: Package) -> str:
    return (
        f"{PACKAGE_IDS.id_for(package)} ({package.package_description} against "
        f"{package.target.name})"
    )


def check_unlocked(package: Package) -> None:
    if package.player_locked:
        raise ToolError(
            f"Package {package_label(package)} was made or edited by the player and "
            "is locked: you cannot change or delete it or its flights. Plan around "
            "it, or create a new package."
        )


def editable_package(view: CommanderView, package_id: str) -> Package:
    package = view.own_package(package_id)
    check_unlocked(package)
    return package


def editable_flight(view: CommanderView, flight_id: str) -> Flight:
    flight = view.own_flight(flight_id)
    check_unlocked(flight.package)
    return flight


def flight_deleted(events: GameUpdateEvents, flight: Flight) -> None:
    """Records a deleted flight, dropping any earlier event for it."""
    events.new_flights.discard(flight)
    events.updated_flights.discard(flight)
    events.delete_flight(flight)


def package_updated(events: GameUpdateEvents, package: Package) -> None:
    """Flights in a package depend on each other's timing; refresh them all."""
    for flight in package.flights:
        if flight not in events.new_flights:
            events.update_flight(flight)


def parse_start_type(value: str) -> StartType:
    wanted = value.strip().lower().replace("_", " ").replace("-", " ")
    for start_type in StartType:
        if wanted in (
            start_type.value.lower(),
            start_type.name.lower().replace("_", " "),
        ):
            return start_type
    raise ToolError(
        f"Unknown start type {value!r}. Valid: cold, warm, runway, in_flight."
    )


# Time over target -----------------------------------------------------------------


class TotArgs(BaseModel):
    tot: Optional[str] = Field(
        default=None,
        description='Time over target, mission-local "HH:MM" or "HH:MM:SS" on the '
        'mission date, or an ISO datetime ("2011-06-01T08:30:00").',
    )
    minutes_after_start: Optional[float] = Field(
        default=None,
        ge=0,
        description="Time over target as minutes after mission start. Flights "
        "that would take off before mission start are spawned in the air where "
        "their plan puts them, so early TOTs are fine for distant targets.",
    )
    asap: Optional[bool] = Field(
        default=None,
        description="Earliest TOT at which every flight of the package can arrive "
        "if it starts at mission start. Re-computed whenever the package changes "
        "until an explicit TOT is set.",
    )

    def given(self) -> int:
        return sum(
            [
                self.tot is not None,
                self.minutes_after_start is not None,
                bool(self.asap),
            ]
        )


def mission_start(game: Game) -> datetime:
    return game.conditions.start_time


def hms(value: Optional[datetime]) -> Optional[str]:
    if value is None or value == datetime.min:
        return None
    try:
        return value.strftime("%H:%M:%S")
    except (ValueError, OverflowError):
        return None


def parse_tot(game: Game, args: TotArgs) -> Optional[datetime]:
    """The explicit TOT asked for, or None for ASAP."""
    if args.given() > 1:
        raise ToolError("Give only one of tot, minutes_after_start or asap.")
    start = mission_start(game)
    if args.minutes_after_start is not None:
        return start + timedelta(minutes=args.minutes_after_start)
    if args.tot is None:
        return None
    text = args.tot.strip()
    match = _TIME.match(text)
    if match:
        hour, minute, second = (int(g) if g else 0 for g in match.groups())
        if hour > 23 or minute > 59 or second > 59:
            raise ToolError(f"Invalid time {text!r}.")
        tot = start.replace(hour=hour, minute=minute, second=second, microsecond=0)
    else:
        try:
            tot = datetime.fromisoformat(text)
        except ValueError:
            raise ToolError(
                f'Cannot read tot {text!r}. Use "HH:MM", "HH:MM:SS" or an ISO '
                'datetime such as "2011-06-01T08:30:00".'
            )
        if tot.tzinfo is not None:
            tot = tot.replace(tzinfo=None)
    if tot < start:
        raise ToolError(
            f"TOT {tot:%Y-%m-%d %H:%M:%S} is before mission start "
            f"{start:%Y-%m-%d %H:%M:%S}."
        )
    if tot > start + timedelta(hours=24):
        raise ToolError("TOT must be within 24 hours of mission start.")
    return tot


def apply_tot(game: Game, package: Package, tot: Optional[datetime]) -> list[str]:
    """Sets the package TOT (None = ASAP) and returns timing warnings."""
    start = mission_start(game)
    if tot is None:
        package.auto_asap = True
        package.set_tot_asap(start)
        return []
    package.auto_asap = False
    package.time_over_target = tot
    return timing_warnings(game, package)


def refresh_asap(game: Game, package: Package) -> None:
    if package.auto_asap and package.flights:
        package.set_tot_asap(mission_start(game))


def state_at_mission_start(game: Game, flight: Flight) -> str | None:
    """Where a flight will be when the mission starts, if that is notable.

    Uses the same catch-up as the simulation: a flight that should already be under
    way is spawned where its plan puts it. Returns None for flights that simply
    start after mission start.
    """
    from game.ato.flightstate import Completed, InFlight, Uninitialized
    from game.sim import GameUpdateEvents
    from game.sim.aircraftsimulation import settle_initial_state

    start = mission_start(game)
    original = flight.state
    try:
        flight.set_state(Uninitialized(flight, game.settings))
        settle_initial_state(flight, start, GameUpdateEvents())
        state = flight.state
        if isinstance(state, Completed):
            return (
                "would already have landed before mission start and is dropped "
                "from the mission; choose a later TOT"
            )
        if isinstance(state, InFlight):
            position = state.estimate_position()
            from_base = meters(position.distance_to_point(flight.departure.position))
            players = (
                "; it has player pilots, who will also spawn in the air"
                if flight.client_count
                else ""
            )
            return (
                f"airborne at mission start, about {from_base.nautical_miles:.0f} nm "
                f"from {flight.departure.name}, flying to "
                f"{state.next_waypoint.pretty_name or state.next_waypoint.name}"
                f"{players}"
            )
        if state.is_waiting_for_start:
            return None
        return f"already starting up or taxiing at {flight.departure.name} at mission start"
    except Exception:
        return None
    finally:
        flight.set_state(original)


def timing_warnings(game: Game, package: Package) -> list[str]:
    """Notes on flights that are already under way or finished at mission start."""
    notes = []
    for flight in package.flights:
        where = state_at_mission_start(game, flight)
        if where is not None:
            notes.append(f"{flight_label(flight)}: {where}.")
    if not notes:
        return []
    return [
        "Flights that should already be under way at mission start spawn where "
        "their plan puts them: " + " ".join(notes)
    ]


# Descriptions -----------------------------------------------------------------------


def flight_label(flight: Flight) -> str:
    return f"{flight.id} ({flight.count}x {flight.unit_type.variant_id} {flight.flight_type.value})"


def _waypoint_time(
    flight: Flight,
    waypoint: FlightWaypoint,
    previous: Optional[tuple[FlightWaypoint, datetime]],
) -> tuple[Optional[str], Optional[datetime]]:
    plan = flight.flight_plan
    if waypoint.waypoint_type is FlightWaypointType.TAKEOFF:
        takeoff = plan.takeoff_time()
        return "takeoff", takeoff
    tot = plan.tot_for_waypoint(waypoint)
    if tot is not None:
        return "tot", tot
    depart = plan.depart_time_for_waypoint(waypoint)
    if depart is not None:
        return "depart", depart
    if previous is not None:
        last_waypoint, last_time = previous
        return "eta", last_time + plan.travel_time_between_waypoints(
            last_waypoint, waypoint
        )
    return None, None


def route_json(flight: Flight) -> list[Json]:
    """The flight's waypoints with index, type, position, altitude and timing."""
    result = []
    previous: Optional[tuple[FlightWaypoint, datetime]] = None
    for index, waypoint in enumerate(flight.flight_plan.waypoints):
        entry: Json = {
            "i": index,
            "type": waypoint.waypoint_type.name,
            "x": int(round(waypoint.position.x)),
            "y": int(round(waypoint.position.y)),
            "alt_ft": to_ft(waypoint.alt.meters),
        }
        if waypoint.alt_type == "RADIO":
            entry["agl"] = True
        name = waypoint.display_name or waypoint.name
        if name and name.strip().upper() != waypoint.waypoint_type.name:
            entry["name"] = name.strip()
        try:
            kind, time = _waypoint_time(flight, waypoint, previous)
        except Exception:
            kind, time = None, None
        if kind is not None and time is not None:
            text = hms(time)
            if text is not None:
                entry[kind] = text
                previous = (waypoint, time)
        result.append(entry)
    return result


def _tasked_loadout(flight: Flight) -> Optional[str]:
    members = list(flight.iter_members())
    return members[0].loadout.name if members else None


def flight_summary(flight: Flight, include_route: bool = True) -> Json:
    plan = flight.flight_plan
    result: Json = {
        "id": str(flight.id),
        "task": flight.flight_type.value,
        "aircraft": flight.unit_type.variant_id,
        "count": flight.count,
        "squadron": flight.squadron.name,
        "squadron_id": str(flight.squadron.id),
        "base": flight.departure.name,
        "start_type": flight.start_type.value,
        "loadout": _tasked_loadout(flight),
    }
    if flight.missing_pilots:
        result["missing_pilots"] = flight.missing_pilots
    if flight.client_count:
        result["player_slots"] = flight.client_count
    try:
        result["startup"] = hms(plan.startup_time())
        result["takeoff"] = hms(plan.takeoff_time())
        result["tot"] = hms(plan.tot)
        result["landing"] = hms(plan.landing_time)
    except Exception:
        pass
    if plan.is_custom:
        result["custom_flight_plan"] = True
    standoff = standoff_json(flight)
    if standoff is not None:
        result["standoff_attack"] = standoff
    if include_route:
        result["route"] = route_json(flight)
    return result


def package_header(package: Package) -> Json:
    primary = package.primary_task
    result: Json = {
        "id": PACKAGE_IDS.id_for(package),
        "target": package.target.name,
        "primary_task": primary.value if primary is not None else None,
        "tot": hms(package.time_over_target),
    }
    if package.auto_asap:
        result["asap"] = True
    return result


# Threats, range and fuel -----------------------------------------------------------


def _horizontal_radius(slant_range: float, altitude_m: Optional[float]) -> float:
    if altitude_m is None:
        return slant_range
    if altitude_m >= slant_range:
        return 0.0
    return math.sqrt(slant_range * slant_range - altitude_m * altitude_m)


#: Reference points in a flight plan that are not flown to.
_NOT_FLOWN = {FlightWaypointType.BULLSEYE, FlightWaypointType.DIVERT}


def _skipped_waypoints(flight: Flight) -> set[int]:
    """ids of waypoints an AI standoff attack doesn't fly to.

    Its target waypoints are only generated for player flights: the AI releases
    at the launch point and turns for the split point.
    """
    if not standoff_applies(flight) or flight.client_count:
        return set()
    targets = getattr(flight.flight_plan.layout, "targets", [])
    return {id(w) for w in targets if w.only_for_player}


def flown_legs(flight: Flight) -> list[tuple[int, FlightWaypoint, FlightWaypoint]]:
    """(index of the leg's end waypoint, start, end) for the legs actually flown.

    Legs to or from the bullseye and the divert field are left out. Waypoints an
    AI standoff attack skips (its target waypoints) are passed over: the leg
    runs from the launch point to the next flown waypoint.
    """
    skipped = _skipped_waypoints(flight)
    flown = [
        (index, w)
        for index, w in enumerate(flight.flight_plan.waypoints)
        if id(w) not in skipped
    ]
    legs = []
    for (_, a), (b_index, b) in zip(flown, flown[1:]):
        if a.waypoint_type in _NOT_FLOWN or b.waypoint_type in _NOT_FLOWN:
            continue
        legs.append((b_index, a, b))
    return legs


def route_threats(view: CommanderView, flight: Flight) -> list[Json]:
    """Known enemy air defenses whose engagement range the route enters.

    Each leg is checked at the lower altitude of its two waypoints (slant range);
    legs flown AGL use the AGL altitude, which is conservative over high terrain.
    """
    threats = view.known_threats(include_detection_only=False)
    found: dict[str, Json] = {}
    for index, a, b in flown_legs(flight):
        altitude = min(a.alt.meters, b.alt.meters)
        for threat in threats:
            radius = _horizontal_radius(threat.threat_range_m, altitude)
            if not radius:
                continue
            crossing = segment_circle_crossing(
                a.position, b.position, threat.position, radius
            )
            if crossing is None:
                continue
            entry = found.setdefault(
                threat.id,
                {
                    "id": threat.id,
                    "name": threat.name,
                    "category": threat.category,
                    "threat_nm": to_nm(threat.threat_range_m),
                    "legs": [],
                    "closest_nm": None,
                },
            )
            entry["legs"].append(f"{index - 1}-{index}")
            closest = to_nm(crossing[2])
            if entry["closest_nm"] is None or closest < entry["closest_nm"]:
                entry["closest_nm"] = closest
    return sorted(found.values(), key=lambda t: t["closest_nm"])


def range_facts(squadron: Squadron, target: MissionTarget) -> tuple[bool, float, float]:
    """(within planner range, distance nm, planner range limit nm).

    Mirrors Squadron.can_auto_assign_mission: the larger of the aircraft's
    max_mission_range and the campaign's max mission range setting.
    """
    settings = squadron.coalition.game.settings
    distance = meters(target.distance_to(squadron.location))
    if squadron.aircraft.helicopter:
        limit = max(
            squadron.aircraft.max_mission_range,
            nautical_miles(settings.max_mission_range_helicopters),
        )
    else:
        limit = max(
            squadron.aircraft.max_mission_range,
            nautical_miles(settings.max_mission_range_planes),
        )
    return (
        distance <= limit,
        round(distance.nautical_miles, 1),
        round(limit.nautical_miles, 1),
    )


def _external_tanks(flight: Flight) -> int:
    members = list(flight.iter_members())
    if not members:
        return 0
    return sum(
        1
        for weapon in members[0].loadout.pylons.values()
        if weapon is not None and "fuel tank" in weapon.name.lower()
    )


def fuel_estimate(flight: Flight) -> Optional[Json]:
    consumption = flight.unit_type.fuel_consumption
    if consumption is None:
        return None
    plan = flight.flight_plan
    burn = 0.0
    if flight.start_type in (StartType.COLD, StartType.WARM):
        burn += consumption.taxi
    for _, a, b in flown_legs(flight):
        leg = plan.fuel_consumption_between_points(a, b)
        if leg is not None:
            burn += leg
    internal = flight.fuel * LB_PER_KG
    result: Json = {
        "internal_fuel_lb": round(internal),
        "estimated_burn_lb": round(burn),
        "landing_reserve_lb": consumption.min_safe,
        "margin_lb": round(internal - burn - consumption.min_safe),
        "external_fuel_tanks": _external_tanks(flight),
        "note": "Internal fuel only (external tanks add to it). Route legs at "
        "planner burn rates; time on station or in combat beyond the route is not "
        "included.",
    }
    return result


def route_distance_nm(flight: Flight) -> float:
    total = sum(
        a.position.distance_to_point(b.position) for _, a, b in flown_legs(flight)
    )
    return round(total / METERS_PER_NM, 1)


def flight_warnings(view: CommanderView, flight: Flight) -> list[str]:
    """Facts worth knowing about a freshly planned flight."""
    warnings = []
    threats = route_threats(view, flight)
    if threats:
        names = ", ".join(
            f"{t['name']} ({t['category']}, {t['threat_nm']} nm, legs "
            f"{','.join(t['legs'])})"
            for t in threats[:6]
        )
        more = f" and {len(threats) - 6} more" if len(threats) > 6 else ""
        warnings.append(
            f"Flight {flight.id} route enters known engagement ranges: {names}{more}."
        )
    fuel = fuel_estimate(flight)
    if fuel is not None and fuel["margin_lb"] < 0:
        warnings.append(
            f"Flight {flight.id}: estimated burn {fuel['estimated_burn_lb']} lb plus "
            f"reserve {fuel['landing_reserve_lb']} lb exceeds internal fuel "
            f"{fuel['internal_fuel_lb']} lb (external tanks: "
            f"{fuel['external_fuel_tanks']})."
        )
    in_range, distance, limit = range_facts(flight.squadron, flight.package.target)
    if not in_range:
        warnings.append(
            f"Flight {flight.id}: target is {distance} nm from {flight.departure.name}, "
            f"beyond the planner's mission range of {limit} nm for "
            f"{flight.unit_type.variant_id}."
        )
    if flight.missing_pilots:
        warnings.append(
            f"Flight {flight.id} has {flight.missing_pilots} seats without a pilot; "
            "those aircraft will not fly."
        )
    warnings.extend(text for _, text in standoff_facts(view, flight))
    return warnings


def find_divert_field(
    view: CommanderView, target: MissionTarget, squadron: Squadron
) -> Optional[ControlPoint]:
    """The divert field PackageBuilder would pick."""
    from game.ato.closestairfields import ObjectiveDistanceCache
    from game.theater import OffMapSpawn

    airfields = ObjectiveDistanceCache.get_closest_airfields(target)
    for airfield in airfields.operational_airfields_within(nautical_miles(150)):
        if airfield.captured is not view.player:
            continue
        if airfield == squadron.location:
            continue
        if not airfield.can_operate(squadron.aircraft):
            continue
        if isinstance(airfield, OffMapSpawn):
            continue
        return airfield
    return None


def list_text(items: Iterable[Any], limit: int = 30) -> str:
    values = [str(i) for i in items]
    text = ", ".join(values[:limit])
    if len(values) > limit:
        text += f" (+{len(values) - limit} more)"
    return text
