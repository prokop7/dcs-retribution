"""Standoff attacks: how far a loadout reaches, and where to launch from.

A standoff attack is opt-in per flight (``Flight.standoff_attack``). Its flight
plan moves the flight's ingress point, where the AI is given its attack tasks,
out along the approach to a launch point at the launch distance from the target
(never closer than the planned ingress point). After the attack the AI
continues to the split point as in any strike-like plan; the target waypoints
are only generated for player flights.

Weapon reach comes from the approximate ``range_nm``/``kind`` weapon data (see
WeaponGroup). It is a planning figure only: where the DCS AI actually releases
depends on its own weapon logic, altitude and speed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, TYPE_CHECKING

from dcs import Point

from game.ato.flighttype import FlightType
from game.data.weapons import WeaponKind
from game.utils import Distance, nautical_miles

if TYPE_CHECKING:
    from game.ato.flight import Flight
    from game.ato.loadouts import Loadout

#: Flight types whose flight plan supports a standoff launch point.
STANDOFF_TASKS = frozenset(
    {
        FlightType.STRIKE,
        FlightType.DEAD,
        FlightType.BAI,
        FlightType.ANTISHIP,
        FlightType.OCA_RUNWAY,
    }
)

_SURFACE_ATTACK = frozenset(
    {
        WeaponKind.CRUISE_MISSILE,
        WeaponKind.STANDOFF_MISSILE,
        WeaponKind.GLIDE_WEAPON,
        WeaponKind.GUIDED_BOMB,
        WeaponKind.UNGUIDED_BOMB,
        WeaponKind.ROCKET,
    }
)

#: Weapon kinds the AI's attack tasks for each mission type use. The tasks are
#: generated in game/missiongenerator/aircraft/waypoints (*ingress.py): strikes
#: bomb a point with ASM/guided/unguided bombs, DEAD adds anti-radiation
#: missiles, anti-ship attacks the ships with anti-ship and guided weapons.
TASK_WEAPON_KINDS: dict[FlightType, frozenset[WeaponKind]] = {
    FlightType.STRIKE: _SURFACE_ATTACK,
    FlightType.OCA_RUNWAY: _SURFACE_ATTACK,
    FlightType.BAI: _SURFACE_ATTACK | {WeaponKind.ATGM},
    FlightType.DEAD: _SURFACE_ATTACK
    | {WeaponKind.ATGM, WeaponKind.ANTI_RADIATION_MISSILE},
    FlightType.ANTISHIP: frozenset(
        {
            WeaponKind.ANTI_SHIP_MISSILE,
            WeaponKind.STANDOFF_MISSILE,
            WeaponKind.GLIDE_WEAPON,
            WeaponKind.GUIDED_BOMB,
            WeaponKind.UNGUIDED_BOMB,
            WeaponKind.ROCKET,
        }
    ),
}

#: Every kind that attacks a surface target (decoys don't).
AIR_TO_GROUND_KINDS: frozenset[WeaponKind] = frozenset(
    k for k in WeaponKind if k is not WeaponKind.DECOY
)

#: The derived launch distance is this fraction of the loadout's reach, so that
#: the weapons are released inside their (approximate) range.
REACH_MARGIN = 0.8

#: The shortest launch distance a commander may ask for.
MIN_LAUNCH_DISTANCE = nautical_miles(3)

#: The launch point stays at least this far after the package join point.
JOIN_CLEARANCE = nautical_miles(2)


@dataclass(frozen=True)
class LoadoutReach:
    #: The approximate release range of the longest-reaching usable weapon.
    reach_nm: float
    #: The weapon group that gives the reach.
    weapon: str
    #: Kinds of all air-to-ground weapons in the loadout.
    kinds: tuple[str, ...]


def loadout_reach(
    loadout: Loadout, task: Optional[FlightType] = None
) -> Optional[LoadoutReach]:
    """Reach of a loadout's air-to-ground weapons, for a task if given.

    With a task, only weapon kinds that task's attack uses count (for example
    anti-ship missiles don't count for a strike). None if no weapon qualifies
    or none has reach data.
    """
    allowed = AIR_TO_GROUND_KINDS
    if task is not None:
        allowed = TASK_WEAPON_KINDS.get(task, AIR_TO_GROUND_KINDS)
    best: Optional[tuple[float, str]] = None
    kinds: set[str] = set()
    for weapon in loadout.pylons.values():
        if weapon is None:
            continue
        group = weapon.weapon_group
        if group.kind is None or group.kind not in AIR_TO_GROUND_KINDS:
            continue
        kinds.add(group.kind.value)
        reach = group.reach_nm
        if group.kind not in allowed or reach is None:
            continue
        if best is None or reach > best[0]:
            best = (reach, group.name)
    if best is None:
        return None
    return LoadoutReach(best[0], best[1], tuple(sorted(kinds)))


def effective_loadout(flight: Flight) -> Optional[Loadout]:
    """The flight lead's loadout as the mission generator will equip it."""
    members = list(flight.iter_members())
    if not members:
        return None
    loadout = members[0].loadout
    game = flight.coalition.game
    if game.settings.restrict_weapons_by_date:
        loadout = loadout.degrade_for_date(
            flight.unit_type, game.date, flight.coalition.faction, flight.package.target
        )
    return loadout


def flight_reach(flight: Flight) -> Optional[LoadoutReach]:
    loadout = effective_loadout(flight)
    if loadout is None:
        return None
    return loadout_reach(loadout, flight.flight_type)


def standoff_applies(flight: Flight) -> bool:
    return flight.standoff_attack and flight.flight_type in STANDOFF_TASKS


def launch_distance(flight: Flight) -> Optional[Distance]:
    """The launch distance asked for: explicit, or derived from the loadout."""
    if flight.standoff_launch_distance_nm is not None:
        return nautical_miles(flight.standoff_launch_distance_nm)
    reach = flight_reach(flight)
    if reach is None:
        return None
    return nautical_miles(reach.reach_nm * REACH_MARGIN)


@dataclass(frozen=True)
class LaunchPoint:
    position: Point
    #: Straight-line distance from the target.
    distance: Distance
    #: The distance asked for, before clamping.
    requested: Distance
    #: Index of the path point the launch point's leg starts at.
    leg: int


def _point_at_distance(a: Point, b: Point, target: Point, distance: float) -> Point:
    """The point on a-b that is ``distance`` from target.

    a must be at least ``distance`` from target and b closer than that, so the
    leg crosses that circle exactly once, going in: |a + t(b - a) - target| =
    distance has its smaller root in [0, 1].
    """
    vx, vy = b.x - a.x, b.y - a.y
    fx, fy = a.x - target.x, a.y - target.y
    vv = vx * vx + vy * vy
    if vv == 0:
        return a
    fv = fx * vx + fy * vy
    ff = fx * fx + fy * fy
    discriminant = max(fv * fv - vv * (ff - distance * distance), 0.0)
    t = (-fv - math.sqrt(discriminant)) / vv
    t = min(max(t, 0.0), 1.0)
    return a.new_in_same_map(a.x + vx * t, a.y + vy * t)


def find_launch_point(
    path: Iterable[Point], target: Point, distance: Distance
) -> LaunchPoint:
    """Where on the approach to launch from, ``distance`` from the target.

    ``path`` runs from the package join point to the ingress point (the target
    ends the approach). The launch point is never closer to the target than the
    ingress point: the AI begins its attack where it is given its attack tasks,
    so moving them closer would only delay the attack, not move the release. It
    stays at least JOIN_CLEARANCE after the join point: before the join the
    flight is still forming up with its package. Within those limits it is on
    the last leg that reaches out to the distance, so the flight comes no closer
    than that to the target before launching.
    """
    points = list(path)
    ingress = points[-1]
    ingress_distance = ingress.distance_to_point(target)
    limit = points[0].distance_to_point(target) - JOIN_CLEARANCE.meters
    used = min(distance.meters, limit)
    if used <= ingress_distance:
        return LaunchPoint(
            ingress, Distance.from_meters(ingress_distance), distance, len(points) - 1
        )
    for index in range(len(points) - 1, 0, -1):
        a, b = points[index - 1], points[index]
        if a.distance_to_point(target) >= used:
            position = _point_at_distance(a, b, target, used)
            return LaunchPoint(
                position,
                Distance.from_meters(position.distance_to_point(target)),
                distance,
                index - 1,
            )
    # Not reached: the join point is further out than the clamped distance.
    start = points[0]
    return LaunchPoint(
        start, Distance.from_meters(start.distance_to_point(target)), distance, 0
    )


def standoff_summary(flight: Flight) -> Optional[str]:
    """One line on the flight's standoff attack for the UI, if it has one."""
    if not flight.standoff_attack:
        return None
    if not standoff_applies(flight):
        return f"Standoff attack: not applied to {flight.flight_type.value} flights"
    ingress = getattr(flight.flight_plan.layout, "ingress", None)
    if ingress is None:
        return "Standoff attack"
    distance = Distance.from_meters(
        ingress.position.distance_to_point(flight.package.target.position)
    )
    return (
        f"Standoff attack: weapons released from the LAUNCH waypoint, "
        f"{distance.nautical_miles:.0f} nm from the target"
    )
