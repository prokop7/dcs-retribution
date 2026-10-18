"""Weapon reach facts for commanders: loadout reach, threat rings around a
target, and standoff launch points.

Nothing here is registered as a tool. All figures are approximate (see
WeaponGroup.range_nm); the tools state them as facts and never pick a loadout.
"""

from __future__ import annotations

import math
from typing import Optional, TYPE_CHECKING

from game.ato.loadouts import Loadout
from game.ato.standoff import (
    LoadoutReach,
    STANDOFF_TASKS,
    effective_loadout,
    flight_reach,
    launch_distance,
    loadout_reach,
    standoff_applies,
)
from game.claudecommander.view import CommanderView, Json, KnownThreat, to_nm

if TYPE_CHECKING:
    from dcs import Point

    from game.ato.flight import Flight
    from game.ato.flighttype import FlightType
    from game.dcs.aircrafttype import AircraftType

REACH_NOTE = (
    "reach_nm: approximate maximum release range of the loadout's air-to-ground "
    "weapons (rough public figures, conservative; real release range depends on "
    "altitude and speed, and the DCS AI decides where it releases). It counts "
    "every kind. Reach for a mission (warnings, standoff launch distance) only "
    "counts the kinds that mission's attack is for: no anti-ship or anti-radiation "
    "missiles for Strike, BAI and OCA/Runway, no land-attack cruise missiles for "
    "Anti-ship."
)

#: Loadouts named in one warning.
MAX_ALTERNATIVES = 3


def reach_json(reach: Optional[LoadoutReach]) -> Json:
    if reach is None:
        return {}
    return {
        "reach_nm": round(reach.reach_nm),
        "reach_weapon": reach.weapon,
        "weapon_kinds": list(reach.kinds),
    }


def _ray_exit_distance(
    origin: Point, direction: tuple[float, float], center: Point, radius: float
) -> Optional[float]:
    """Distance along a ray from origin to where it leaves a circle, if it does."""
    cx, cy = center.x - origin.x, center.y - origin.y
    projection = cx * direction[0] + cy * direction[1]
    perpendicular_sq = cx * cx + cy * cy - projection * projection
    remaining = radius * radius - perpendicular_sq
    if remaining < 0:
        return None
    exit_distance = projection + math.sqrt(remaining)
    if exit_distance <= 0:
        return None
    return exit_distance


def approach_origin(flight: Flight) -> Point:
    """The point the flight approaches its target from."""
    waypoints = flight.package.waypoints
    if waypoints is not None:
        return waypoints.ingress
    return flight.departure.position


def covering_threats(
    view: CommanderView, flight: Flight
) -> tuple[float, list[KnownThreat]]:
    """Known threat rings that cover the flight's target, and how far (nm) they
    extend from the target along the flight's approach."""
    return covering_threats_at(
        view, flight.package.target.position, approach_origin(flight)
    )


def covering_threats_at(
    view: CommanderView, target: Point, origin: Point
) -> tuple[float, list[KnownThreat]]:
    """Known threat rings that cover a target, and how far (nm) they extend from
    it towards ``origin``. Ground-level engagement ranges as they are now."""
    dx, dy = origin.x - target.x, origin.y - target.y
    length = math.hypot(dx, dy)
    direction = (dx / length, dy / length) if length else (1.0, 0.0)
    extent = 0.0
    threats = []
    for threat in view.known_threats(include_detection_only=False):
        if threat.position.distance_to_point(target) >= threat.threat_range_m:
            continue
        exit_distance = _ray_exit_distance(
            target, direction, threat.position, threat.threat_range_m
        )
        if exit_distance is None:
            continue
        threats.append(threat)
        extent = max(extent, exit_distance)
    return to_nm(extent), threats


def named_loadout_reaches(
    view: CommanderView, aircraft: AircraftType, task: FlightType
) -> list[tuple[str, LoadoutReach]]:
    """(name, reach for the task) of the aircraft's named loadouts, as equipped
    on the campaign date, longest reach first."""
    game = view.game
    restrict = game.settings.restrict_weapons_by_date
    faction = view.coalition.faction
    result = []
    seen: set[str] = set()
    for loadout in Loadout.iter_for_aircraft(aircraft):
        if loadout.name in seen:
            continue
        seen.add(loadout.name)
        if any(w is not None and not w.known_to_pydcs for w in loadout.pylons.values()):
            continue
        if restrict:
            loadout = loadout.degrade_for_date(aircraft, game.date, faction)
        reach = loadout_reach(loadout, task)
        if reach is not None:
            result.append((loadout.name, reach))
    result.sort(key=lambda r: -r[1].reach_nm)
    return result


def _threat_names(threats: list[KnownThreat]) -> str:
    names = [f"{t.name} ({to_nm(t.threat_range_m)} nm)" for t in threats[:4]]
    if len(threats) > 4:
        names.append(f"{len(threats) - 4} more")
    return ", ".join(names)


def _loadout_name(flight: Flight) -> str:
    members = list(flight.iter_members())
    return members[0].loadout.name if members else "none"


def standoff_facts(view: CommanderView, flight: Flight) -> list[tuple[str, str]]:
    """(kind, text) facts about a strike-like flight's weapon reach.

    - ``standoff_reach``: the loadout's reach is shorter than the threat rings
      covering the target extend along the approach, and a named loadout of the
      aircraft reaches further.
    - ``standoff_launch``: for a standoff attack, the launch point is inside a
      known threat ring, beyond the loadout's reach, or closer than asked.
    """
    if flight.flight_type not in STANDOFF_TASKS:
        return []
    if flight.package.target.is_friendly(view.player):
        return []
    facts: list[tuple[str, str]] = []
    reach = flight_reach(flight)
    extent, threats = covering_threats(view, flight)
    label = f"Flight {flight.id} ({flight.flight_type.value}, loadout {_loadout_name(flight)!r}"
    reach_text = (
        f"reach ~{reach.reach_nm:.0f} nm with {reach.weapon})"
        if reach is not None
        else "no weapon with known reach)"
    )
    if threats and (reach is None or reach.reach_nm < extent):
        better = [
            (name, r)
            for name, r in named_loadout_reaches(
                view, flight.unit_type, flight.flight_type
            )
            if r.reach_nm >= extent
        ]
        if better:
            options = ", ".join(
                f"{name!r} (~{r.reach_nm:.0f} nm, {r.weapon})"
                for name, r in better[:MAX_ALTERNATIVES]
            )
            facts.append(
                (
                    "standoff_reach",
                    f"{label}, {reach_text}: the target is inside known threat "
                    f"rings ({_threat_names(threats)}) that extend ~{extent:.0f} nm "
                    f"from it along the approach. {flight.unit_type.variant_id} "
                    f"loadouts that reach that far for {flight.flight_type.value}: "
                    f"{options}. Reach figures are approximate; to release from "
                    "outside the rings the flight also needs set_standoff_attack.",
                )
            )
    if standoff_applies(flight):
        facts.extend(_launch_facts(view, flight, label, reach_text, reach))
    return facts


def _launch_point(flight: Flight) -> Optional[Point]:
    layout = flight.flight_plan.layout
    ingress = getattr(layout, "ingress", None)
    if ingress is None:
        return None
    return ingress.position


def _launch_facts(
    view: CommanderView,
    flight: Flight,
    label: str,
    reach_text: str,
    reach: Optional[LoadoutReach],
) -> list[tuple[str, str]]:
    position = _launch_point(flight)
    if position is None:
        return []
    target = flight.package.target.position
    distance = to_nm(position.distance_to_point(target))
    facts = []
    if reach is not None and reach.reach_nm < distance:
        facts.append(
            (
                "standoff_launch",
                f"{label}, {reach_text}: its launch point is {distance:.0f} nm from "
                "the target, beyond the loadout's reach; the AI will fly closer "
                "before it releases, if at all.",
            )
        )
    inside = [
        t
        for t in view.known_threats(include_detection_only=False)
        if t.position.distance_to_point(position) < t.threat_range_m
    ]
    if inside:
        facts.append(
            (
                "standoff_launch",
                f"{label}: its launch point ({distance:.0f} nm from the target) is "
                f"inside known threat rings: {_threat_names(inside)}.",
            )
        )
    wanted = launch_distance(flight)
    if wanted is not None and wanted.nautical_miles - distance > 1:
        facts.append(
            (
                "standoff_launch",
                f"{label}: launch distance {wanted.nautical_miles:.0f} nm asked, "
                f"{distance:.0f} nm used: the launch point stays on the approach "
                "after the package join point.",
            )
        )
    elif wanted is not None and distance - wanted.nautical_miles > 1:
        facts.append(
            (
                "standoff_launch",
                f"{label}: the package's ingress point is already {distance:.0f} nm "
                f"from the target, further out than the {wanted.nautical_miles:.0f} "
                "nm launch distance, so the attack starts there as it would "
                "without standoff (a closer launch point would only start the "
                "attack later).",
            )
        )
    return facts


def standoff_json(flight: Flight) -> Optional[Json]:
    """The flight's standoff attack settings and launch point, if it has one."""
    if not flight.standoff_attack:
        return None
    result: Json = {
        "launch_distance": (
            "derived from loadout reach"
            if flight.standoff_launch_distance_nm is None
            else f"{flight.standoff_launch_distance_nm:g} nm"
        )
    }
    if not standoff_applies(flight):
        result["note"] = (
            f"Not applied: {flight.flight_type.value} flights have no standoff "
            "launch point."
        )
        return result
    position = _launch_point(flight)
    if position is not None:
        target = flight.package.target.position
        result["launch_point_nm_from_target"] = to_nm(
            position.distance_to_point(target)
        )
        result["launch_point"] = {
            "x": int(round(position.x)),
            "y": int(round(position.y)),
        }
    reach = flight_reach(flight)
    if reach is not None:
        result["loadout_reach_nm"] = round(reach.reach_nm)
    return result


def loadout_reach_json(view: CommanderView, flight: Flight) -> Json:
    """Reach of the flight's loadout for its task, as equipped."""
    loadout = effective_loadout(flight)
    if loadout is None:
        return {}
    return reach_json(loadout_reach(loadout, flight.flight_type))
