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

from collections import Counter
from typing import Any, Iterable, Sequence, TYPE_CHECKING, TypeVar

from game.claudecommander.view import EnemySighting, PACKAGE_IDS
from game.theater.controlpoint import ControlPoint
from game.theater.frontline import FrontLine
from game.theater.player import Player
from game.theater.theatergroundobject import TheaterGroundObject

if TYPE_CHECKING:
    from game.ato.package import Package
    from game.debriefing import Debriefing
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
