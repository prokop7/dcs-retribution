"""After-action summary of a mission from one commander's point of view.

The game's ``Debriefing`` lists losses as "player" (BLUE) and "enemy" (RED). This
module turns it into what one side may learn from the mission:

* its own losses, in full (aircraft by type/base/task, ground units, sites,
  runways, bases);
* enemy losses it can confirm: aircraft destroyed (type and origin base) and
  ground units, sites, buildings, runways and convoys destroyed;
* for each of its own packages, the flights' losses and the confirmed damage at
  the package's target;
* the enemy aircraft types it encountered, which also feed the enemy order of
  battle hook (``EnemySighting``);
* the weapon feed, when the mission recorded it: per own package, what each
  aircraft type fired and what became of each weapon (hit, intercepted and by
  which enemy defense, no hit), what it hit; enemy launches aimed at its own
  aircraft (launcher type, weapon, hits), as RWR/MAWS would show them; and what
  its own air defenses fired at enemy aircraft and weapons, with hits and
  intercepts.

The game reports every enemy loss of the mission, so every enemy loss counts as
confirmed. The summary never reveals the enemy's plans, remaining forces or
budget. From the weapon feed it never reveals enemy shots at anything but its
own aircraft (beyond the enemy defenses that engaged its own weapons), nor
enemy unit names: enemy launchers and defenses appear by type only.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable, Iterator, Optional, Sequence, TYPE_CHECKING, TypeVar

from dcs.mapping import Point

from game.claudecommander.view import EnemySighting, PACKAGE_IDS
from game.theater.controlpoint import ControlPoint
from game.theater.frontline import FrontLine
from game.theater.player import Player
from game.theater.theatergroundobject import TheaterGroundObject

if TYPE_CHECKING:
    from game.ato.flight import Flight
    from game.ato.package import Package
    from game.debriefing import (
        Debriefing,
        HitRecord,
        KillRecord,
        ShotRecord,
        WeaponFate,
    )
    from game.unitmap import FlyingUnit, TheaterUnitMapping, SceneryObjectMapping

T = TypeVar("T")


def _pick(player: Player, blue: T, red: T) -> T:
    return blue if player.is_blue else red


def _counts(values: Iterable[str]) -> dict[str, int]:
    return dict(Counter(values).most_common())


def _aircraft_losses(losses: Sequence[FlyingUnit]) -> dict[str, Any]:
    return {
        "total": len(losses),
        "by_type": _counts(l.flight.unit_type.variant_id for l in losses),
        "by_base": _counts(l.flight.departure.name for l in losses),
        "by_task": _counts(l.flight.flight_type.value for l in losses),
    }


def _site_losses(
    units: Sequence[TheaterUnitMapping], scenery: Sequence[SceneryObjectMapping]
) -> list[dict[str, Any]]:
    by_site: dict[TheaterGroundObject, Counter[str]] = {}
    for unit in units:
        theater_unit = unit.theater_unit
        label = (
            theater_unit.unit_type.variant_id
            if theater_unit.unit_type is not None
            else theater_unit.type.id
        )
        by_site.setdefault(theater_unit.ground_object, Counter())[label] += 1
    for building in scenery:
        by_site.setdefault(building.ground_unit.ground_object, Counter())[
            building.trigger_zone.name
        ] += 1
    sites = []
    for tgo, counts in by_site.items():
        sites.append(
            {
                "id": str(tgo.id),
                "name": tgo.name,
                "category": tgo.category,
                "base": tgo.control_point.name,
                "destroyed": dict(counts),
                "site_destroyed": tgo.is_dead,
            }
        )
    return sites


def _type_counts(counts: dict[Any, int]) -> dict[str, int]:
    return {
        getattr(unit_type, "variant_id", str(unit_type)): count
        for unit_type, count in counts.items()
        if count
    }


def _damage_at_target(
    package: Package, debriefing: Debriefing, enemy_player: Player
) -> int:
    """Confirmed enemy losses at a package's target, from any source."""
    target = package.target
    ground = debriefing.ground_losses
    if isinstance(target, TheaterGroundObject):
        units = _pick(
            enemy_player, ground.player_ground_objects, ground.enemy_ground_objects
        )
        scenery = _pick(enemy_player, ground.player_scenery, ground.enemy_scenery)
        return sum(1 for u in units if u.theater_unit.ground_object is target) + sum(
            1 for s in scenery if s.ground_unit.ground_object is target
        )
    if isinstance(target, FrontLine):
        enemy_cp = target.control_point_friendly_to(enemy_player)
        front = _pick(enemy_player, ground.player_front_line, ground.enemy_front_line)
        return sum(1 for unit in front if unit.origin is enemy_cp)
    if isinstance(target, ControlPoint):
        airfields = _pick(enemy_player, ground.player_airfields, ground.enemy_airfields)
        return sum(1 for airfield in airfields if airfield is target)
    return 0


def _package_results(
    debriefing: Debriefing, player: Player, own_losses: Sequence[FlyingUnit]
) -> list[dict[str, Any]]:
    lost_by_flight: Counter[Any] = Counter(loss.flight for loss in own_losses)
    results = []
    for package in debriefing.game.coalition_for(player).ato.packages:
        primary = package.primary_task
        damage = _damage_at_target(package, debriefing, player.opponent)
        results.append(
            {
                "package_id": PACKAGE_IDS.id_for(package),
                "target": package.target.name,
                "task": primary.value if primary is not None else None,
                "confirmed_damage_at_target": damage,
                "flights": [
                    {
                        "task": flight.flight_type.value,
                        "aircraft": flight.unit_type.variant_id,
                        "count": flight.count,
                        "lost": lost_by_flight.get(flight, 0),
                    }
                    for flight in package.flights
                ],
            }
        )
    return results


_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _describe_killer(dcs_type: Optional[str]) -> tuple[str, str]:
    """A readable name and category ("aircraft", "air defense", "ship") for a
    DCS unit type id."""
    if not dcs_type:
        return "unknown", "unknown"
    from dcs.helicopters import helicopter_map
    from dcs.planes import plane_map
    from dcs.ships import ship_map
    from dcs.vehicles import vehicle_map

    from game.dcs.aircrafttype import AircraftType
    from game.dcs.groundunittype import GroundUnitType
    from game.dcs.shipunittype import ShipUnitType

    try:
        if dcs_type in plane_map or dcs_type in helicopter_map:
            dcs_unit = plane_map.get(dcs_type) or helicopter_map[dcs_type]
            name = next(AircraftType.for_dcs_type(dcs_unit)).variant_id
            return name, "aircraft"
        if dcs_type in vehicle_map:
            name = next(GroundUnitType.for_dcs_type(vehicle_map[dcs_type])).variant_id
            return name, "air defense"
        if dcs_type in ship_map:
            name = next(ShipUnitType.for_dcs_type(ship_map[dcs_type])).variant_id
            return name, "ship"
    except (StopIteration, KeyError):
        pass
    if dcs_type in plane_map or dcs_type in helicopter_map:
        return dcs_type, "aircraft"
    if dcs_type in ship_map:
        return dcs_type, "ship"
    if dcs_type in vehicle_map:
        return dcs_type, "air defense"
    return dcs_type, "unknown"


def _where(debriefing: Debriefing, record: KillRecord) -> dict[str, Any]:
    if record.x is None or record.z is None:
        return {}
    theater = debriefing.game.theater
    point = Point(record.x, record.z, theater.terrain)
    nearest = min(
        theater.controlpoints, key=lambda cp: cp.position.distance_to_point(point)
    )
    distance_nm = nearest.position.distance_to_point(point) / 1852
    bearing = math.degrees(
        math.atan2(point.y - nearest.position.y, point.x - nearest.position.x)
    )
    direction = _COMPASS[round((bearing % 360) / 45) % 8]
    result: dict[str, Any] = {
        "where": f"{distance_nm:.0f} nm {direction} of {nearest.name}",
        "over_water": theater.is_in_sea(point),
    }
    if record.alt is not None:
        result["altitude_ft"] = round(record.alt / 0.3048 / 100) * 100
    return result


def _known_threats_at(
    debriefing: Debriefing, player: Player, record: KillRecord
) -> list[str]:
    """Known enemy air defense sites whose nominal range covers the point."""
    from game.theater.theatergroundobject import (
        EwrGroundObject,
        NavalGroundObject,
        SamGroundObject,
    )

    if record.x is None or record.z is None:
        return []
    theater = debriefing.game.theater
    point = Point(record.x, record.z, theater.terrain)
    covering = []
    for tgo in theater.ground_objects:
        if (
            tgo.control_point.captured is player
            or tgo.control_point.captured.is_neutral
        ):
            continue
        if not isinstance(tgo, (SamGroundObject, NavalGroundObject)) or isinstance(
            tgo, EwrGroundObject
        ):
            continue
        reach = tgo.max_threat_range().meters
        if reach and tgo.position.distance_to_point(point) <= reach:
            covering.append(tgo.name)
    return sorted(covering)


def _loss_attribution(debriefing: Debriefing, player: Player) -> dict[str, Any]:
    """What killed this side's aircraft, and what this side's aircraft killed.

    Comes from the kills DCS reported. Losses DCS reported no killer for (crashes,
    fuel, or combat resolved by the game's fast forward) count as unattributed.
    """
    state = getattr(debriefing, "state_data", None)
    records = list(getattr(state, "kill_records", []))
    unit_map = getattr(debriefing, "unit_map", None)
    if unit_map is None:
        records = []
    details = []
    causes: Counter[str] = Counter()
    own_kills: Counter[tuple[str, str]] = Counter()
    attributed: set[str] = set()
    for record in records:
        assert unit_map is not None
        victim_unit = unit_map.flight(record.target)
        victim = victim_unit.flight if victim_unit is not None else None
        killer_unit = unit_map.flight(record.killer) if record.killer else None
        killer_flight = killer_unit.flight if killer_unit is not None else None
        if victim is not None and victim.squadron.player is player:
            attributed.add(record.target)
            if killer_flight is not None:
                if killer_flight.squadron.player is player:
                    name, category = killer_flight.unit_type.variant_id, "own side"
                else:
                    name, category = killer_flight.unit_type.variant_id, "aircraft"
            else:
                name, category = _describe_killer(record.killer_type)
            causes[{"aircraft": "enemy aircraft"}.get(category, category)] += 1
            detail: dict[str, Any] = {
                "aircraft": victim.unit_type.variant_id,
                "task": victim.flight_type.value,
                "package_target": victim.package.target.name,
                "killed_by": f"{name} ({category})",
            }
            if record.weapon:
                detail["weapon"] = record.weapon
            detail.update(_where(debriefing, record))
            threats = _known_threats_at(debriefing, player, record)
            if threats:
                detail["inside_known_air_defense"] = threats
            if record.time is not None:
                detail["minutes_after_start"] = round(record.time / 60)
            details.append(detail)
        elif (
            killer_flight is not None
            and killer_flight.squadron.player is player
            and record.target_type
        ):
            victim_name = (
                victim.unit_type.variant_id
                if victim is not None
                else _describe_killer(record.target_type)[0]
            )
            own_kills[(killer_flight.unit_type.variant_id, victim_name)] += 1
    own_air = _pick(player, debriefing.air_losses.player, debriefing.air_losses.enemy)
    unattributed = max(0, len(own_air) - len(attributed))
    if unattributed:
        causes["not attributed (crash, fuel or simulated combat)"] += unattributed
    return {
        "losses_by_cause": dict(causes.most_common()),
        "aircraft_lost": details,
        "kills_by_own_aircraft": [
            {"by": by, "killed": killed, "count": count}
            for (by, killed), count in own_kills.most_common()
        ],
        "attribution_available": bool(records),
    }


#: DCS coalition ids.
_DCS_COALITIONS = {1: Player.RED, 2: Player.BLUE}
_MAX_WEAPON_LINES_PER_PACKAGE = 10
_MAX_WEAPON_LINES = 15
_MAX_BREAKDOWN = 5

WEAPON_FEED_NOTE = (
    "Intercepts are inferred: a defending missile fired at the weapon ended with "
    "it. 'no hit' means DCS reported no hit (a miss, a decoyed weapon, or blast "
    "damage DCS did not report as a hit). 'unresolved' weapons were still flying "
    "at mission end or were not tracked."
)


def _weapon_label(name: Optional[str]) -> str:
    if not name:
        return "unknown weapon"
    if name.startswith("weapons."):
        return name.rsplit(".", 1)[-1]
    return name


def _top(counts: Counter[str]) -> dict[str, int]:
    return dict(counts.most_common(_MAX_BREAKDOWN))


@dataclass
class _Tally:
    fired: int = 0
    outcomes: Counter[str] = dataclass_field(default_factory=Counter)
    intercepted_by: Counter[str] = dataclass_field(default_factory=Counter)
    targets_hit: Counter[str] = dataclass_field(default_factory=Counter)
    targets: Counter[str] = dataclass_field(default_factory=Counter)
    weapons_targeted: Counter[str] = dataclass_field(default_factory=Counter)
    engaged: int = 0
    intercepts: int = 0
    ranges_m: list[float] = dataclass_field(default_factory=list)

    def outcome_counts(self) -> dict[str, int]:
        return {
            "fired": self.fired,
            "hit": self.outcomes["hit"],
            "intercepted": self.outcomes["intercepted"],
            "no_hit": self.outcomes["no_hit"],
            "unresolved": self.outcomes["unresolved"],
        }


class _WeaponFeed:
    """A mission's weapon feed (shots, hits, weapon fates), indexed for one side."""

    def __init__(self, debriefing: Debriefing, player: Player) -> None:
        state = getattr(debriefing, "state_data", None)
        self.shots: list[ShotRecord] = list(getattr(state, "shot_records", []))
        self.hits: list[HitRecord] = list(getattr(state, "hit_records", []))
        fates: list[WeaponFate] = list(getattr(state, "weapon_fates", []))
        self.unit_map = getattr(debriefing, "unit_map", None)
        self.player = player
        self.shot_by_id = {shot.id: shot for shot in self.shots}
        self.fate_by_shot = {fate.shot: fate for fate in fates}
        self.hits_by_shot: dict[int, list[HitRecord]] = {}
        for hit in self.hits:
            if hit.shot is not None:
                self.hits_by_shot.setdefault(hit.shot, []).append(hit)
        self.intercepts_by_defender: Counter[int] = Counter(
            fate.intercepted_by.shot
            for fate in fates
            if fate.fate == "intercepted"
            and fate.intercepted_by is not None
            and fate.intercepted_by.shot is not None
        )

    def flight(self, unit_name: Optional[str]) -> Optional[Flight]:
        lookup = getattr(self.unit_map, "flight", None)
        if not unit_name or lookup is None:
            return None
        unit = lookup(unit_name)
        return unit.flight if unit is not None else None

    def site_unit(self, unit_name: Optional[str]) -> Optional[TheaterUnitMapping]:
        lookup = getattr(self.unit_map, "theater_units", None)
        if not unit_name or lookup is None:
            return None
        return lookup(unit_name)

    def ground_target(self, name: Optional[str]) -> Optional[tuple[Player, str]]:
        """Owner and label of a ground target: a site or armor group unit, a
        building, a front line or reserve unit, a convoy vehicle or a map object.

        Site names are known to both sides (static intelligence), so they are part
        of the label; DCS unit names never are.
        """
        if not name or self.unit_map is None:
            return None
        lookup = getattr(self.unit_map, "theater_units", None)
        site = lookup(name) if lookup is not None else None
        if site is not None:
            unit = site.theater_unit
            tgo = unit.ground_object
            kind = "building" if unit.is_static else tgo.category
            what = unit.unit_type.variant_id if unit.unit_type else unit.name
            return tgo.control_point.captured, f"{tgo.name} ({kind}: {what})"
        for method, kind in (
            ("front_line_unit", "front line"),
            ("motorpool_unit", "reserve"),
        ):
            lookup = getattr(self.unit_map, method, None)
            found = lookup(name) if lookup is not None else None
            if found is not None:
                return (
                    found.origin.captured,
                    f"{found.unit_type.variant_id} ({kind}, {found.origin.name})",
                )
        lookup = getattr(self.unit_map, "convoy_unit", None)
        convoy = lookup(name) if lookup is not None else None
        if convoy is not None:
            return (
                convoy.convoy.player,
                f"{convoy.unit_type.variant_id} (convoy to "
                f"{convoy.convoy.destination.name})",
            )
        lookup = getattr(self.unit_map, "scenery_object", None)
        scenery = lookup(name) if lookup is not None else None
        if scenery is not None:
            tgo = scenery.ground_unit.ground_object
            return tgo.control_point.captured, f"{tgo.name} (building)"
        return None

    def shooter_side(self, shot: ShotRecord) -> Optional[Player]:
        if shot.shooter_coalition in _DCS_COALITIONS:
            return _DCS_COALITIONS[shot.shooter_coalition]
        flight = self.flight(shot.shooter)
        if flight is not None:
            return flight.squadron.player
        site = self.site_unit(shot.shooter)
        if site is not None:
            return site.theater_unit.ground_object.control_point.captured
        return None

    def outcome(self, shot: ShotRecord) -> str:
        fate = self.fate_by_shot.get(shot.id)
        if shot.id in self.hits_by_shot or (fate is not None and fate.fate == "hit"):
            return "hit"
        return fate.fate if fate is not None else "unresolved"

    def describe_unit(self, name: Optional[str], dcs_type: Optional[str]) -> str:
        flight = self.flight(name)
        if flight is not None:
            return flight.unit_type.variant_id
        ground = self.ground_target(name)
        if ground is not None:
            return ground[1]
        site = self.site_unit(name)
        if site is not None and site.theater_unit.unit_type is not None:
            return site.theater_unit.unit_type.variant_id
        return _describe_killer(dcs_type)[0]

    def describe_launcher(self, name: Optional[str], dcs_type: Optional[str]) -> str:
        flight = self.flight(name)
        if flight is not None:
            return f"{flight.unit_type.variant_id} (aircraft)"
        site = self.site_unit(name)
        if site is not None and site.theater_unit.unit_type is not None:
            category = "ship" if site.theater_unit.is_ship else "air defense"
            ground = self.ground_target(name)
            label = (
                ground[1]
                if ground is not None
                else (site.theater_unit.unit_type.variant_id)
            )
            return f"{label} ({category})"
        label, category = _describe_killer(dcs_type)
        return label if category == "unknown" else f"{label} ({category})"

    def is_own_aircraft(
        self, name: Optional[str], dcs_type: Optional[str], coalition: Optional[int]
    ) -> bool:
        flight = self.flight(name)
        if flight is not None:
            return flight.squadron.player is self.player
        return (
            _DCS_COALITIONS.get(coalition or 0) is self.player
            and _describe_killer(dcs_type)[1] == "aircraft"
        )

    def own_aircraft_shots(self) -> Iterator[tuple[ShotRecord, Flight]]:
        for shot in self.shots:
            flight = self.flight(shot.shooter)
            if flight is not None and flight.squadron.player is self.player:
                yield shot, flight

    def weapon_of_shot(self, shot_id: Optional[int], fallback: Optional[str]) -> str:
        shot = self.shot_by_id.get(shot_id) if shot_id is not None else None
        if shot is not None:
            return _weapon_label(shot.weapon_name)
        return _weapon_label(fallback)


def _own_weapon_use(feed: _WeaponFeed) -> list[dict[str, Any]]:
    """Per own package: what each aircraft type fired and what became of it."""
    by_package: dict[Package, dict[tuple[str, str, str], _Tally]] = {}
    for shot, flight in feed.own_aircraft_shots():
        key = (
            flight.unit_type.variant_id,
            flight.flight_type.value,
            _weapon_label(shot.weapon_name),
        )
        tally = by_package.setdefault(flight.package, {}).setdefault(key, _Tally())
        tally.fired += 1
        outcome = feed.outcome(shot)
        tally.outcomes[outcome] += 1
        fate = feed.fate_by_shot.get(shot.id)
        if fate is not None and fate.engaged_by:
            tally.engaged += 1
        if outcome == "intercepted" and fate is not None:
            defender = fate.intercepted_by
            if defender is not None:
                label = feed.describe_unit(defender.shooter, defender.shooter_type)
                weapon = feed.weapon_of_shot(defender.shot, defender.weapon)
                if weapon != _weapon_label(None):
                    label += f" ({weapon})"
                tally.intercepted_by[label] += 1
        for hit in feed.hits_by_shot.get(shot.id, []):
            if not hit.target_is_weapon:
                tally.targets_hit[feed.describe_unit(hit.target, hit.target_type)] += 1
        if shot.range_m is not None:
            tally.ranges_m.append(shot.range_m)

    results = []
    for package, tallies in by_package.items():
        primary = package.primary_task
        weapons = []
        for (aircraft, task, weapon), tally in sorted(
            tallies.items(), key=lambda item: -item[1].fired
        )[:_MAX_WEAPON_LINES_PER_PACKAGE]:
            line: dict[str, Any] = {
                "aircraft": aircraft,
                "task": task,
                "weapon": weapon,
                **tally.outcome_counts(),
            }
            if tally.engaged:
                line["engaged_by_defenses"] = tally.engaged
            if tally.intercepted_by:
                line["intercepted_by"] = _top(tally.intercepted_by)
            if tally.targets_hit:
                line["targets_hit"] = _top(tally.targets_hit)
            if tally.ranges_m:
                average = sum(tally.ranges_m) / len(tally.ranges_m)
                line["avg_launch_range_nm"] = round(average / 1852, 1)
            weapons.append(line)
        results.append(
            {
                "package_id": PACKAGE_IDS.id_for(package),
                "task": primary.value if primary is not None else None,
                "target": package.target.name,
                "weapons": weapons,
            }
        )
    return results


def _enemy_fire_at_own_aircraft(feed: _WeaponFeed) -> list[dict[str, Any]]:
    """Enemy launches aimed at this side's aircraft (what RWR/MAWS would show)."""
    tallies: dict[tuple[str, str], _Tally] = {}
    for shot in feed.shots:
        if feed.shooter_side(shot) is not feed.player.opponent:
            continue
        if shot.target_is_weapon or not feed.is_own_aircraft(
            shot.target, shot.target_type, shot.target_coalition
        ):
            continue
        key = (
            feed.describe_launcher(shot.shooter, shot.shooter_type),
            _weapon_label(shot.weapon_name),
        )
        tally = tallies.setdefault(key, _Tally())
        tally.fired += 1
        tally.outcomes[feed.outcome(shot)] += 1
        tally.targets[feed.describe_unit(shot.target, shot.target_type)] += 1
        if shot.range_m is not None:
            tally.ranges_m.append(shot.range_m)
    results = []
    for (launcher, weapon), tally in sorted(
        tallies.items(), key=lambda item: -item[1].fired
    )[:_MAX_WEAPON_LINES]:
        line: dict[str, Any] = {
            "launcher": launcher,
            "weapon": weapon,
            "fired": tally.fired,
            "hit": tally.outcomes["hit"],
            "at": _top(tally.targets),
        }
        if tally.ranges_m:
            average = sum(tally.ranges_m) / len(tally.ranges_m)
            line["avg_launch_range_nm"] = round(average / 1852, 1)
        results.append(line)
    return results


def _enemy_fire_at_own_ground(feed: _WeaponFeed) -> list[dict[str, Any]]:
    """Enemy weapons aimed at this side's ground forces, sites and buildings.

    The units under attack see it; launchers appear by type only.
    """
    tallies: dict[tuple[str, str], _Tally] = {}
    for shot in feed.shots:
        if feed.shooter_side(shot) is not feed.player.opponent:
            continue
        if shot.target_is_weapon:
            continue
        ground = feed.ground_target(shot.target)
        if ground is None or ground[0] is not feed.player:
            continue
        key = (
            feed.describe_launcher(shot.shooter, shot.shooter_type),
            _weapon_label(shot.weapon_name),
        )
        tally = tallies.setdefault(key, _Tally())
        tally.fired += 1
        tally.outcomes[feed.outcome(shot)] += 1
        tally.targets[ground[1]] += 1
    return [
        {
            "launcher": launcher,
            "weapon": weapon,
            "fired": tally.fired,
            "hit": tally.outcomes["hit"],
            "at": _top(tally.targets),
        }
        for (launcher, weapon), tally in sorted(
            tallies.items(), key=lambda item: -item[1].fired
        )[:_MAX_WEAPON_LINES]
    ]


def _own_air_defense(feed: _WeaponFeed) -> list[dict[str, Any]]:
    """Shots by this side's ground units and ships at aircraft and weapons."""
    tallies: dict[tuple[str, str, str], _Tally] = {}
    for shot in feed.shots:
        if feed.shooter_side(shot) is not feed.player:
            continue
        if feed.flight(shot.shooter) is not None:
            continue
        if shot.target_is_weapon:
            target_kind = "weapon"
        elif shot.target and (
            feed.flight(shot.target) is not None
            or _describe_killer(shot.target_type)[1] == "aircraft"
        ):
            target_kind = "aircraft"
        elif shot.missile_category == "SAM":
            target_kind = "other"
        else:
            continue
        site = feed.site_unit(shot.shooter)
        site_name = site.theater_unit.ground_object.name if site else "unlisted unit"
        key = (
            site_name,
            feed.describe_unit(shot.shooter, shot.shooter_type),
            _weapon_label(shot.weapon_name),
        )
        tally = tallies.setdefault(key, _Tally())
        tally.fired += 1
        tally.outcomes[feed.outcome(shot)] += 1
        tally.intercepts += feed.intercepts_by_defender[shot.id]
        if target_kind == "weapon":
            tally.weapons_targeted[
                feed.weapon_of_shot(shot.target_shot, shot.target_type)
            ] += 1
        elif target_kind == "aircraft":
            tally.targets[feed.describe_unit(shot.target, shot.target_type)] += 1
    results = []
    for (site_name, unit, weapon), tally in sorted(
        tallies.items(), key=lambda item: -item[1].fired
    )[:_MAX_WEAPON_LINES]:
        line: dict[str, Any] = {
            "site": site_name,
            "unit": unit,
            "weapon": weapon,
            "fired": tally.fired,
            "hit": tally.outcomes["hit"],
            "intercepts": tally.intercepts,
        }
        if tally.targets:
            line["at_aircraft"] = _top(tally.targets)
        if tally.weapons_targeted:
            line["at_weapons"] = _top(tally.weapons_targeted)
        results.append(line)
    return results


def _weapons_summary(debriefing: Debriefing, player: Player) -> dict[str, Any]:
    """The weapon feed from one side's point of view.

    The side sees everything its own aircraft and air defenses fired and what
    became of it (including which enemy defenses intercepted its weapons), and
    enemy launches aimed at its own aircraft and at its ground forces, sites and
    buildings. Enemy shots at anything else are left out.
    """
    feed = _WeaponFeed(debriefing, player)
    if not feed.shots:
        return {"available": False}
    totals = _Tally()
    for shot, _flight in feed.own_aircraft_shots():
        totals.fired += 1
        totals.outcomes[feed.outcome(shot)] += 1
    return {
        "available": True,
        "own_aircraft_totals": totals.outcome_counts(),
        "own_packages": _own_weapon_use(feed),
        "enemy_fire_at_own_aircraft": _enemy_fire_at_own_aircraft(feed),
        "enemy_fire_at_own_ground": _enemy_fire_at_own_ground(feed),
        "own_air_defense": _own_air_defense(feed),
        "note": WEAPON_FEED_NOTE,
    }


def summarize_debriefing(debriefing: Debriefing, player: Player) -> dict[str, Any]:
    """A fog-filtered after-action summary of a mission for one side."""
    if player.is_neutral:
        raise ValueError("A debriefing summary needs a side, not neutral")
    enemy = player.opponent
    air = debriefing.air_losses
    ground = debriefing.ground_losses
    own_air = _pick(player, air.player, air.enemy)
    enemy_air = _pick(player, air.enemy, air.player)

    own_sites = _site_losses(
        _pick(player, ground.player_ground_objects, ground.enemy_ground_objects),
        _pick(player, ground.player_scenery, ground.enemy_scenery),
    )
    enemy_sites = _site_losses(
        _pick(player, ground.enemy_ground_objects, ground.player_ground_objects),
        _pick(player, ground.enemy_scenery, ground.player_scenery),
    )
    own_runways = _pick(player, ground.player_airfields, ground.enemy_airfields)
    enemy_runways = _pick(player, ground.enemy_airfields, ground.player_airfields)

    enemy_aircraft = Counter(
        (loss.flight.unit_type.variant_id, loss.flight.departure.name)
        for loss in enemy_air
    )

    return {
        "side": player.value,
        "own_losses": {
            "aircraft": _aircraft_losses(own_air),
            "front_line_units": _type_counts(
                debriefing.front_line_losses_by_type(player)
            ),
            "reserve_units": _type_counts(debriefing.motorpool_losses_by_type(player)),
            "convoy_units": _type_counts(debriefing.convoy_losses_by_type(player)),
            "cargo_ship_units": _type_counts(
                debriefing.cargo_ship_losses_by_type(player)
            ),
            "airlift_units": _type_counts(debriefing.airlift_losses_by_type(player)),
            "sites": own_sites,
            "runways_hit": [airfield.name for airfield in own_runways],
        },
        "confirmed_enemy_losses": {
            "aircraft": [
                {"aircraft": aircraft, "origin": origin, "count": count}
                for (aircraft, origin), count in enemy_aircraft.most_common()
            ],
            "front_line_units": _type_counts(
                debriefing.front_line_losses_by_type(enemy)
            ),
            "reserve_units": _type_counts(debriefing.motorpool_losses_by_type(enemy)),
            "convoy_units": _type_counts(debriefing.convoy_losses_by_type(enemy)),
            "cargo_ship_units": _type_counts(
                debriefing.cargo_ship_losses_by_type(enemy)
            ),
            "airlift_units": _type_counts(debriefing.airlift_losses_by_type(enemy)),
            "sites": enemy_sites,
            "runways_hit": [airfield.name for airfield in enemy_runways],
        },
        "base_captures": [
            {
                "base": capture.control_point.name,
                "captured_by": (
                    "own" if capture.captured_by_player is player else "enemy"
                ),
            }
            for capture in debriefing.base_captures
        ],
        "packages": _package_results(debriefing, player, own_air),
        "loss_attribution": _loss_attribution(debriefing, player),
        "weapons": _weapons_summary(debriefing, player),
        "enemy_aircraft_types_encountered": sorted({a for a, _ in enemy_aircraft}),
    }


def sightings_from_debriefing(
    debriefing: Debriefing, player: Player, turn: int
) -> list[EnemySighting]:
    """Enemy aircraft a side encountered in a mission, for ``CommanderView``."""
    enemy_air = _pick(player, debriefing.air_losses.enemy, debriefing.air_losses.player)
    seen = {
        (loss.flight.unit_type.variant_id, loss.flight.departure.name)
        for loss in enemy_air
    }
    return [
        EnemySighting(aircraft, turn, "shot_down", origin)
        for aircraft, origin in sorted(seen)
    ]


def _count_text(counts: Any) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{n}x {name}" for name, n in counts.items())


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _outcome_text(line: dict[str, Any]) -> str:
    parts = []
    if line.get("hit"):
        parts.append(f"{line['hit']} hit")
    if line.get("intercepted"):
        text = f"{line['intercepted']} intercepted"
        if line.get("intercepted_by"):
            text += " by " + _count_text(line["intercepted_by"])
        parts.append(text)
    if line.get("no_hit"):
        parts.append(f"{line['no_hit']} no hit")
    if line.get("unresolved"):
        parts.append(f"{line['unresolved']} unresolved")
    return ", ".join(parts) if parts else "no result"


def _weapons_markdown(weapons: dict[str, Any]) -> list[str]:
    if not weapons.get("available"):
        return []
    lines = ["**Weapons** (intercepts are inferred, approximate):"]
    totals = _as_dict(weapons.get("own_aircraft_totals"))
    if totals.get("fired"):
        lines.append(
            f"- Own aircraft fired {totals['fired']} weapons: " + _outcome_text(totals)
        )
    for package in weapons.get("own_packages", []):
        for line in package.get("weapons", []):
            text = (
                f"- Package {package.get('task')} vs {package.get('target')}: "
                f"{line['aircraft']} ({line['task']}) {line['fired']}x "
                f"{line['weapon']} fired: {_outcome_text(line)}"
            )
            if line.get("engaged_by_defenses"):
                text += f"; {line['engaged_by_defenses']} engaged by enemy defenses"
            if line.get("avg_launch_range_nm") is not None:
                text += f"; launched at {line['avg_launch_range_nm']} nm on average"
            if line.get("targets_hit"):
                text += "; hit " + _count_text(line["targets_hit"])
            lines.append(text)
    enemy_fire = weapons.get("enemy_fire_at_own_aircraft", [])
    if enemy_fire:
        lines.append(
            "**Enemy fired at your aircraft:** "
            + "; ".join(
                f"{e['fired']}x {e['weapon']} from {e['launcher']}: {e['hit']} hit"
                + (f" (at {_count_text(e['at'])})" if e.get("at") else "")
                for e in enemy_fire
            )
        )
    ground_fire = weapons.get("enemy_fire_at_own_ground", [])
    if ground_fire:
        lines.append(
            "**Enemy fired at your ground forces:** "
            + "; ".join(
                f"{e['fired']}x {e['weapon']} from {e['launcher']}: {e['hit']} hit"
                + (f" (at {_count_text(e['at'])})" if e.get("at") else "")
                for e in ground_fire
            )
        )
    air_defense = weapons.get("own_air_defense", [])
    if air_defense:
        lines.append("**Own air defense:**")
    for defense in air_defense:
        targets = [
            _count_text(defense[kind])
            for kind in ("at_aircraft", "at_weapons")
            if defense.get(kind)
        ]
        lines.append(
            f"- {defense['site']} ({defense['unit']}): "
            f"{defense['fired']}x {defense['weapon']}"
            + (f" at {', '.join(targets)}" if targets else "")
            + f": {defense['hit']} hit, {defense['intercepts']} enemy weapons"
            " intercepted"
        )
    return lines


def debrief_markdown(summary: dict[str, Any]) -> str:
    """A readable version of a debrief summary, for the commander's journal."""
    lines: list[str] = []
    own = _as_dict(summary.get("own_losses"))
    aircraft = _as_dict(own.get("aircraft"))
    lines.append(
        f"**Own aircraft lost:** {aircraft.get('total', 0)}"
        + (
            f" ({_count_text(aircraft.get('by_type'))})"
            if aircraft.get("total")
            else ""
        )
    )
    attribution = _as_dict(summary.get("loss_attribution"))
    causes = attribution.get("losses_by_cause")
    if causes:
        lines.append(
            "**Causes:** " + ", ".join(f"{cause} {n}" for cause, n in causes.items())
        )
    for loss in attribution.get("aircraft_lost", []):
        parts = [f"killed by {loss['killed_by']}"]
        if loss.get("weapon"):
            parts.append(f"with {loss['weapon']}")
        if loss.get("where"):
            parts.append(
                loss["where"] + (" over water" if loss.get("over_water") else "")
            )
        if loss.get("inside_known_air_defense"):
            parts.append("inside " + ", ".join(loss["inside_known_air_defense"]))
        if loss.get("minutes_after_start") is not None:
            parts.append(f"+{loss['minutes_after_start']} min")
        lines.append(
            f"- {loss['aircraft']} ({loss['task']}, package vs "
            f"{loss['package_target']}): " + ", ".join(parts)
        )
    for kind, label in (
        ("front_line_units", "front line units"),
        ("reserve_units", "reserve units"),
        ("convoy_units", "convoy units"),
    ):
        if own.get(kind):
            lines.append(f"**Own {label} lost:** {_count_text(own[kind])}")
    for site in own.get("sites", []):
        state = "destroyed" if site.get("site_destroyed") else "damaged"
        lines.append(
            f"- Own site {site['name']} ({site['category']}, {site['base']}) {state}: "
            f"{_count_text(site.get('destroyed'))}"
        )
    if own.get("runways_hit"):
        lines.append("**Own runways hit:** " + ", ".join(own["runways_hit"]))

    enemy = _as_dict(summary.get("confirmed_enemy_losses"))
    enemy_air = enemy.get("aircraft", [])
    if enemy_air:
        lines.append(
            "**Enemy aircraft destroyed:** "
            + ", ".join(
                f"{e['count']}x {e['aircraft']} (from {e['origin']})" for e in enemy_air
            )
        )
    kills = attribution.get("kills_by_own_aircraft", [])
    if kills:
        lines.append(
            "**Kills by own aircraft:** "
            + ", ".join(f"{k['by']} killed {k['count']}x {k['killed']}" for k in kills)
        )
    if enemy.get("front_line_units"):
        lines.append(
            f"**Enemy front line units destroyed:** {_count_text(enemy['front_line_units'])}"
        )
    for site in enemy.get("sites", []):
        state = "destroyed" if site.get("site_destroyed") else "damaged"
        lines.append(
            f"- Enemy site {site['name']} ({site['category']}, {site['base']}) {state}"
        )
    if enemy.get("runways_hit"):
        lines.append("**Enemy runways hit:** " + ", ".join(enemy["runways_hit"]))

    for capture in summary.get("base_captures", []):
        lines.append(
            f"**Base captured:** {capture['base']} by {capture['captured_by']}"
        )

    packages = summary.get("packages", [])
    if packages:
        lines.append("**Packages:**")
        for package in packages:
            lost = sum(f.get("lost", 0) for f in package.get("flights", []))
            damage = package.get("confirmed_damage_at_target")
            lines.append(
                f"- {package.get('task')} vs {package.get('target')}: "
                f"{lost} aircraft lost; damage at target: "
                + (
                    _count_text(damage)
                    if isinstance(damage, dict)
                    else str(damage or "none")
                )
            )
    lines.extend(_weapons_markdown(_as_dict(summary.get("weapons"))))
    if not attribution.get("attribution_available", True):
        lines.append("_This mission's results don't say what killed each aircraft._")
    return "\n".join(lines)
