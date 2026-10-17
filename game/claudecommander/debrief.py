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
  battle hook (``EnemySighting``).

The game reports every enemy loss of the mission, so every enemy loss counts as
confirmed. The summary never reveals the enemy's plans, remaining forces or
budget.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable, Optional, Sequence, TYPE_CHECKING, TypeVar

from dcs.mapping import Point

from game.claudecommander.view import EnemySighting, PACKAGE_IDS
from game.theater.controlpoint import ControlPoint
from game.theater.frontline import FrontLine
from game.theater.player import Player
from game.theater.theatergroundobject import TheaterGroundObject

if TYPE_CHECKING:
    from game.ato.package import Package
    from game.debriefing import Debriefing, KillRecord
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
    if not attribution.get("attribution_available", True):
        lines.append("_This mission's results don't say what killed each aircraft._")
    return "\n".join(lines)
