"""The fog-of-war filtered read model of one commander side.

Every commander tool reads the game through a ``CommanderView``. The view is the
only place that decides what a side may know, so tool handlers never touch
``Game`` directly and never put raw game objects into their output.

The rule is "static known, dynamic hidden" (docs/dev/design/claude-commander.md,
section 2.3):

* Own side: everything.
* Enemy, known: control points (type, position, owner, runway damage), ground
  object sites with their category, layout (unit types at full strength) and
  nominal detection/threat ranges, the strategic value of buildings, front line
  positions, and the enemy faction's catalogue. A site with no unit left alive is
  reported as destroyed (battle damage assessment), but partial damage, unit
  health and repairs are not.
* Enemy, hidden: ATO and flight plans, squadrons and aircraft per base, ground
  units at bases and on the front, the contents of motorpools, purchase orders,
  transfers and convoys, and the budget.

Output conventions shared by all tools:

* Positions are game coordinates in meters (``x`` is north, ``y`` is east) plus
  ``lat``/``lon`` in decimal degrees.
* Distances and ranges are nautical miles (``*_nm``), altitudes feet
  (``*_ft``), speeds knots (``*_kt``), bearings true degrees (``*_deg``).
* Ids are stable strings: control points, ground objects, front lines,
  squadrons and flights use their UUID. Packages have no persistent id, so they
  get an id that is stable for as long as the game stays loaded.
"""

from __future__ import annotations

import math
import uuid
import weakref
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional, Sequence, TYPE_CHECKING

from dcs.mapping import LatLng, Point
from dcs.unittype import VehicleType

from game.ato.flighttype import FlightType
from game.claudecommander.domains import owned_domains
from game.claudecommander.state import EnemySighting
from game.claudecommander.mcp.tools import ToolError
from game.config import REWARDS
from game.data.radar_db import LAUNCHER_TRACKER_PAIRS, TELARS, TRACK_RADARS
from game.theater.controlpoint import ControlPoint, ParkingType
from game.theater.frontline import FrontLine
from game.theater.missiontarget import MissionTarget
from game.theater.player import Player
from game.theater.theatergroundobject import (
    BuildingGroundObject,
    MotorpoolGroundObject,
    TheaterGroundObject,
    VehicleGroupGroundObject,
)

if TYPE_CHECKING:
    from game import Game
    from game.ato.flight import Flight
    from game.ato.package import Package
    from game.coalition import Coalition
    from game.factions.faction import Faction
    from game.squadrons import Squadron
    from game.theater.theatergroup import TheaterGroup, TheaterUnit

Json = dict[str, Any]

METERS_PER_NM = 1852.0
METERS_PER_FOOT = 0.3048


def to_nm(meters: float) -> float:
    return round(meters / METERS_PER_NM, 1)


def to_ft(meters: float) -> int:
    return int(round(meters / METERS_PER_FOOT))


def bearing_deg(origin: Point, target: Point) -> int:
    """True bearing from origin to target, 0-359."""
    return int(round(origin.heading_between_point(target))) % 360


def position(point: Point) -> Json:
    """The shared position format: game x/y in meters plus lat/lon."""
    latlng = point.latlng()
    return {
        "x": int(round(point.x)),
        "y": int(round(point.y)),
        "lat": round(latlng.lat, 4),
        "lon": round(latlng.lng, 4),
    }


def page(items: Sequence[Any], offset: int, limit: int) -> tuple[list[Any], Json]:
    """Slices a list and describes the slice so the caller can ask for more."""
    sliced = list(items[offset : offset + limit])
    meta: Json = {"total": len(items)}
    if offset:
        meta["offset"] = offset
    if offset + limit < len(items):
        meta["next_offset"] = offset + limit
    return sliced, meta


class UnknownIdError(ToolError):
    pass


@dataclass(frozen=True)
class KnownThreat:
    """A known enemy air defense, with ranges at full strength."""

    id: str
    name: str
    category: str
    control_point: str
    position: Point
    threat_range_m: float
    detection_range_m: float


class _PackageIds:
    """Ids for packages, which have no persistent id of their own.

    Ids stay valid while the package object exists, i.e. as long as the game is
    loaded. They are not saved.
    """

    def __init__(self) -> None:
        self._ids: weakref.WeakKeyDictionary[Package, str] = weakref.WeakKeyDictionary()

    def id_for(self, package: Package) -> str:
        package_id = self._ids.get(package)
        if package_id is None:
            package_id = f"pkg-{uuid.uuid4().hex[:8]}"
            self._ids[package] = package_id
        return package_id


PACKAGE_IDS = _PackageIds()


def _unit_label(unit: TheaterUnit) -> str:
    unit_type = unit.unit_type
    if unit_type is not None:
        return unit_type.variant_id
    return unit.type.id or unit.name


def _nominal_group_ranges(group: TheaterGroup) -> tuple[float, float]:
    """(threat, detection) range in meters of a group at full strength.

    Mirrors TheaterGroup.max_threat_range/max_detection_range but ignores whether
    units are alive, because the enemy's current unit health is hidden.
    """
    detection = 0.0
    max_non_radar = 0.0
    max_telar = 0.0
    max_tel = 0.0
    trackers = set()
    launchers: dict[type[VehicleType], float] = {}
    for unit in group.units:
        if not unit.is_anti_air:
            continue
        threat = float(getattr(unit.type, "threat_range", None) or 0)
        detection = max(
            detection, float(getattr(unit.type, "detection_range", None) or 0)
        )
        if unit.type in TRACK_RADARS:
            trackers.add(unit.type)
        elif unit.type in TELARS:
            max_telar = max(max_telar, threat)
        elif issubclass(unit.type, VehicleType) and unit.type in LAUNCHER_TRACKER_PAIRS:
            launchers[unit.type] = max(launchers.get(unit.type, 0.0), threat)
        else:
            max_non_radar = max(max_non_radar, threat)
    for launcher, threat in launchers.items():
        if any(tr in trackers for tr in LAUNCHER_TRACKER_PAIRS[launcher]):
            max_tel = max(max_tel, threat)
    return max(max_tel, max_telar, max_non_radar), detection


def nominal_ranges(tgo: TheaterGroundObject) -> tuple[float, float]:
    """(threat, detection) range in meters of a ground object at full strength."""
    threat = 0.0
    detection = 0.0
    for group in tgo.groups:
        group_threat, group_detection = _nominal_group_ranges(group)
        threat = max(threat, group_threat)
        detection = max(detection, group_detection)
    return threat, detection


def _time(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(value.strftime("%H:%M:%S"))
    except (AttributeError, ValueError, OverflowError):
        return None


class CommanderView:
    """What one side knows about the campaign.

    ``sightings`` is the hook for enemy aircraft the side has encountered (from
    debriefings). It defaults to none, which reveals no enemy order of battle.
    """

    def __init__(
        self,
        game: Game,
        player: Player,
        sightings: Iterable[EnemySighting] = (),
    ) -> None:
        if player.is_neutral:
            raise ValueError("A commander view needs a side, not neutral")
        self.game = game
        self.player = player
        self.enemy = player.opponent
        self.sightings: list[EnemySighting] = list(sightings)

    # Basic accessors ------------------------------------------------------------

    @property
    def coalition(self) -> Coalition:
        return self.game.coalition_for(self.player)

    @property
    def enemy_coalition(self) -> Coalition:
        return self.game.coalition_for(self.enemy)

    def own_control_points(self) -> list[ControlPoint]:
        return list(self.game.theater.control_points_for(self.player))

    def other_control_points(self) -> list[ControlPoint]:
        """Enemy and neutral control points."""
        return [
            cp
            for cp in self.game.theater.controlpoints
            if cp.captured is not self.player
        ]

    def own_ground_objects(self) -> list[TheaterGroundObject]:
        return [tgo for cp in self.own_control_points() for tgo in cp.ground_objects]

    def enemy_ground_objects(self) -> list[TheaterGroundObject]:
        return [
            tgo
            for cp in self.game.theater.control_points_for(self.enemy)
            for tgo in cp.ground_objects
        ]

    def front_line_objects(self) -> list[FrontLine]:
        return list(self.game.theater.conflicts())

    def owner_label(self, owner: Player) -> str:
        if owner is self.player:
            return "own"
        if owner.is_neutral:
            return "neutral"
        return "enemy"

    # Resolvers, for tools that need the underlying objects ------------------------
    #
    # These return game objects for internal use (e.g. by write tools). Never put
    # what they return into tool output directly.

    def control_point(self, cp_id: str) -> ControlPoint:
        for cp in self.game.theater.controlpoints:
            if str(cp.id) == cp_id:
                return cp
        raise UnknownIdError(f"No control point with id {cp_id}")

    def own_control_point(self, cp_id: str) -> ControlPoint:
        cp = self.control_point(cp_id)
        if cp.captured is not self.player:
            raise UnknownIdError(f"{cp.name} is not one of your bases")
        return cp

    def ground_object(self, tgo_id: str) -> TheaterGroundObject:
        for tgo in self.game.theater.ground_objects:
            if str(tgo.id) == tgo_id:
                return tgo
        raise UnknownIdError(f"No ground object with id {tgo_id}")

    def own_ground_object(self, tgo_id: str) -> TheaterGroundObject:
        tgo = self.ground_object(tgo_id)
        if tgo.control_point.captured is not self.player:
            raise UnknownIdError(f"{tgo.name} is not one of your ground objects")
        return tgo

    def front_line(self, front_id: str) -> FrontLine:
        for front in self.front_line_objects():
            if str(front.id) == front_id:
                return front
        raise UnknownIdError(f"No front line with id {front_id}")

    def own_squadron(self, squadron_id: str) -> Squadron:
        for squadron in self.coalition.air_wing.iter_squadrons():
            if str(squadron.id) == squadron_id:
                return squadron
        raise UnknownIdError(f"No squadron of yours with id {squadron_id}")

    def own_package(self, package_id: str) -> Package:
        for package in self.coalition.ato.packages:
            if PACKAGE_IDS.id_for(package) == package_id:
                return package
        raise UnknownIdError(f"No package of yours with id {package_id}")

    def own_flight(self, flight_id: str) -> Flight:
        for package in self.coalition.ato.packages:
            for flight in package.flights:
                if str(flight.id) == flight_id:
                    return flight
        raise UnknownIdError(f"No flight of yours with id {flight_id}")

    def mission_target(self, target_id: str) -> MissionTarget:
        """A control point, ground object or front line by id."""
        for resolver in (self.control_point, self.ground_object, self.front_line):
            try:
                return resolver(target_id)
            except UnknownIdError:
                continue
        raise UnknownIdError(
            f"No control point, ground object or front line with id {target_id}"
        )

    def position_of(self, object_id: str) -> tuple[Point, str]:
        """The position and name of anything with an id this side may locate."""
        try:
            target = self.mission_target(object_id)
            return target.position, target.name
        except UnknownIdError:
            pass
        try:
            squadron = self.own_squadron(object_id)
            return squadron.location.position, squadron.location.name
        except UnknownIdError:
            pass
        try:
            package = self.own_package(object_id)
            return package.target.position, package.target.name
        except UnknownIdError:
            pass
        raise UnknownIdError(f"Unknown id {object_id}")

    def point_from(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        object_id: Optional[str] = None,
    ) -> tuple[Point, Optional[str]]:
        """Resolves one of the accepted point forms to a game point."""
        terrain = self.game.theater.terrain
        if object_id is not None:
            point, name = self.position_of(object_id)
            return point, name
        if x is not None and y is not None:
            return Point(x, y, terrain), None
        if lat is not None and lon is not None:
            return Point.from_latlng(LatLng(lat, lon), terrain), None
        raise ToolError("A point needs either id, x and y, or lat and lon")

    # Situation ------------------------------------------------------------------

    def victory_state(self) -> str:
        own = list(self.game.theater.control_points_for(self.player, state_check=True))
        enemy = list(self.game.theater.control_points_for(self.enemy, state_check=True))
        if not own:
            return "lost"
        if not enemy:
            return "won"
        return "ongoing"

    def weather(self) -> Json:
        weather = self.game.conditions.weather
        result: Json = {"type": type(weather).__name__}
        clouds = weather.clouds
        if clouds is not None:
            cloud_info: Json = {"base_ft": to_ft(clouds.base)}
            if clouds.preset is not None:
                cloud_info["preset"] = clouds.preset.ui_name
                cloud_info["preset_max_base_ft"] = to_ft(clouds.preset.max_base)
            else:
                cloud_info["density"] = clouds.density
                cloud_info["thickness_ft"] = to_ft(clouds.thickness)
            cloud_info["precipitation"] = clouds.precipitation.name.rstrip("_")
            result["clouds"] = cloud_info
        else:
            result["clouds"] = None
        if weather.fog is not None:
            result["fog"] = {
                "visibility_nm": to_nm(weather.fog.visibility.meters),
                "thickness_ft": to_ft(weather.fog.thickness),
            }
        wind = weather.wind

        def wind_json(value: Any) -> Json:
            return {
                "from_deg": int(round(value.direction)) % 360,
                "kt": round(value.speed * 1.94384),
            }

        result["wind"] = {
            "surface": wind_json(wind.at_0m),
            "6500ft": wind_json(wind.at_2000m),
            "26000ft": wind_json(wind.at_8000m),
        }
        atmospheric = weather.atmospheric
        result["qnh_inhg"] = round(atmospheric.qnh.inches_hg, 2)
        result["temperature_c"] = round(atmospheric.temperature_celsius)
        result["turbulence"] = round(atmospheric.turbulence_per_10cm, 1)
        return result

    def income(self) -> Json:
        from game.income import Income

        income = Income(self.game, self.player)
        return {
            "total": round(income.total, 1),
            "from_bases": round(income.from_bases, 1),
            "from_buildings": round(income.total_buildings, 1),
            "multiplier": income.multiplier,
        }

    def situation(self) -> Json:
        game = self.game
        coalition = self.coalition
        conditions = game.conditions
        return {
            "side": self.player.value,
            "turn": game.turn,
            "theater": game.theater.terrain.name,
            "date": game.date.isoformat(),
            "start_time": conditions.start_time.strftime("%H:%M"),
            "time_of_day": conditions.time_of_day.value,
            "weather": self.weather(),
            "budget": round(coalition.budget, 1),
            "income_per_turn": self.income(),
            "victory_state": self.victory_state(),
            "faction": coalition.faction.name,
            "doctrine": coalition.doctrine.name,
            "enemy_faction": self.enemy_coalition.faction.name,
            "own_bases": len(self.own_control_points()),
            "enemy_bases": sum(1 for _ in game.theater.control_points_for(self.enemy)),
            "front_lines": len(self.front_line_objects()),
            "restrict_weapons_by_date": game.settings.restrict_weapons_by_date,
            "your_domains": owned_domains(game, self.player),
            "mission_window": self.mission_window(),
            "force": self.force_utilization(idle_limit=0),
        }

    def mission_window(self) -> Json:
        """When this turn's mission runs. Both sides fly in the same window."""
        start = self.game.conditions.start_time
        duration = self.game.settings.desired_player_mission_duration
        return {
            "start": start.strftime("%H:%M"),
            "planned_duration_min": round(duration.total_seconds() / 60),
            "end": (start + duration).strftime("%H:%M"),
        }

    def force_utilization(self, idle_limit: int = 25) -> Json:
        """How much of the air wing this turn's plan uses. Facts only.

        Ready aircraft are untasked aircraft that also have an untasked pilot.
        """
        owned = 0
        ready = 0
        idle: list[Json] = []
        for squadron in self.coalition.air_wing.iter_squadrons():
            owned += squadron.owned_aircraft
            squadron_ready = min(
                squadron.untasked_aircraft, squadron.number_of_available_pilots
            )
            ready += squadron_ready
            if squadron_ready > 0:
                idle.append(
                    {
                        "squadron_id": str(squadron.id),
                        "squadron": squadron.name,
                        "aircraft": squadron.aircraft.variant_id,
                        "base": squadron.location.name,
                        "primary_task": squadron.primary_task.value,
                        "ready": squadron_ready,
                    }
                )
        tasked = sum(f.count for p in self.coalition.ato.packages for f in p.flights)
        result: Json = {
            "aircraft_owned": owned,
            "aircraft_tasked": tasked,
            "aircraft_ready_untasked": ready,
        }
        if idle_limit:
            idle.sort(key=lambda s: -s["ready"])
            result["idle_squadrons"] = idle[:idle_limit]
            if len(idle) > idle_limit:
                result["idle_squadrons_omitted"] = len(idle) - idle_limit
        return result

    # Control points ---------------------------------------------------------------

    def _cp_header(self, cp: ControlPoint) -> Json:
        return {
            "id": str(cp.id),
            "name": cp.name,
            "type": cp.cptype.name,
            "owner": self.owner_label(cp.captured),
            **position(cp.position),
        }

    def _parking(self, cp: ControlPoint) -> Json:
        fixed = ParkingType(fixed_wing=True, fixed_wing_stol=True, rotary_wing=False)
        rotary = ParkingType(fixed_wing=False, fixed_wing_stol=False, rotary_wing=True)
        try:
            return {
                "fixed_wing": cp.total_aircraft_parking(fixed),
                "rotary_wing": cp.total_aircraft_parking(rotary),
            }
        except (AttributeError, NotImplementedError):
            return {}

    def _runway(self, cp: ControlPoint, own: bool) -> Json:
        if cp.is_fleet:
            return {"operational": cp.runway_is_operational()}
        if not cp.runway_is_destroyable:
            return {}
        status = cp.runway_status
        result: Json = {"damaged": status.damaged}
        if own:
            result["operational"] = cp.runway_is_operational()
            if status.repair_turns_remaining is not None:
                result["repair_turns_remaining"] = status.repair_turns_remaining
        return result

    def own_base(self, cp: ControlPoint) -> Json:
        result = self._cp_header(cp)
        result["status"] = cp.status.name
        result["runway"] = self._runway(cp, own=True)
        result["parking"] = self._parking(cp)
        aircraft: Counter[str] = Counter()
        for squadron in cp.squadrons:
            aircraft[squadron.aircraft.variant_id] += squadron.owned_aircraft
        result["aircraft"] = {k: v for k, v in aircraft.items() if v}
        result["ground_units"] = {
            unit.variant_id: count for unit, count in cp.base.armor.items()
        }
        orders = {
            getattr(unit, "variant_id", str(unit)): count
            for unit, count in cp.ground_unit_orders.units.items()
            if count
        }
        if orders:
            result["ground_unit_orders"] = orders
        result["income"] = cp.income_per_turn
        result["can_recruit_ground_units"] = cp.can_recruit_ground_units(self.game)
        result["ammo_depots_alive"] = cp.active_ammo_depots_count
        result["ammo_depots_total"] = cp.total_ammo_depots_count
        result["front_line_unit_capacity"] = cp.frontline_unit_count_limit
        result["economy"] = self.base_economy(cp, own=True)
        result["connected_to"] = [c.name for c in cp.connected_points]
        enemy_fronts = [c.name for c in cp.front_lines]
        if enemy_fronts:
            result["front_lines_with"] = enemy_fronts
        if cp.moveable and cp.target_position is not None:
            result["moving_to"] = position(cp.target_position)
        return result

    def other_base(self, cp: ControlPoint) -> Json:
        result = self._cp_header(cp)
        result["runway"] = self._runway(cp, own=False)
        result["parking"] = self._parking(cp)
        result["connected_to"] = [c.name for c in cp.connected_points]
        result["capture"] = self.capture_facts(cp)
        result["economy"] = self.base_economy(cp, own=False)
        return result

    def capture_facts(self, cp: ControlPoint) -> Json:
        """What taking a base involves, from what this side knows.

        Air assault troops can take a base unless armor groups defend it within
        the capture radius; those must be destroyed first. Their partial damage is
        hidden like for any enemy site, so a group counts until it is destroyed.
        """
        blocking = [
            tgo.name
            for tgo in cp.ground_objects
            if isinstance(tgo, VehicleGroupGroundObject)
            and not tgo.is_dead
            and tgo.distance_to(cp) < ControlPoint.CAPTURE_DISTANCE.meters
        ]
        facts: Json = {
            "air_assault_possible": FlightType.AIR_ASSAULT in self.mission_types_for(cp)
            and not cp.is_fleet,
            "blocked_by_armor": blocking,
            "capture_radius_nm": to_nm(ControlPoint.CAPTURE_DISTANCE.meters),
            "front_line_with_you": any(
                fl.control_point_friendly_to(self.player).captured is self.player
                for fl in cp.front_lines.values()
            ),
        }
        if not self.game.settings.plugin_option("ctld"):
            facts["air_assault_possible"] = False
            facts["air_assault_note"] = (
                "Air assault needs the CTLD plugin, which is off."
            )
        if cp.captured.is_neutral:
            facts["note"] = "Neutral and undefended: an air assault takes it."
        return facts

    def base_economy(self, cp: ControlPoint, own: bool) -> Json:
        """Income and facilities a base is worth to whoever holds it.

        Buildings change hands with the base in the state they are in; destroyed
        ones pay nothing until their new owner repairs them. Enemy buildings are
        known at full strength unless destroyed (partial damage is hidden).
        """
        intact = 0.0
        destroyed = 0.0
        facilities: Counter[str] = Counter()
        for tgo in cp.ground_objects:
            if not isinstance(tgo, BuildingGroundObject):
                continue
            statics = list(tgo.statics)
            per_building = REWARDS.get(tgo.category, 0)
            alive = sum(1 for b in statics if b.alive)
            if not own and alive:
                # Known at full strength until every building is destroyed.
                alive = len(statics)
            intact += alive * per_building
            destroyed += (len(statics) - alive) * per_building
            if alive:
                facilities[tgo.category] += 1
        result: Json = {
            "base_income": cp.income_per_turn,
            "building_income": round(intact, 1),
            "value_per_turn": round(cp.income_per_turn + intact, 1),
            "facilities": dict(facilities),
        }
        if destroyed:
            result["destroyed_building_income"] = round(destroyed, 1)
        if facilities.get("factory"):
            result["note_factory"] = "Ground units can be recruited at this base."
        if facilities.get("ammo"):
            result["note_ammo"] = "Ammo depots raise this base's front line capacity."
        return result

    def own_bases(self) -> list[Json]:
        return [self.own_base(cp) for cp in self.own_control_points()]

    def enemy_bases(self) -> list[Json]:
        return [self.other_base(cp) for cp in self.other_control_points()]

    # Squadrons --------------------------------------------------------------------

    def own_squadron_json(self, squadron: Squadron) -> Json:
        result: Json = {
            "id": str(squadron.id),
            "name": squadron.name,
            "aircraft": squadron.aircraft.variant_id,
            "base": squadron.location.name,
            "primary_task": squadron.primary_task.value,
            "auto_tasks": sorted(
                t.value for t in squadron.auto_assignable_mission_types
            ),
            "aircraft_owned": squadron.owned_aircraft,
            "aircraft_untasked": squadron.untasked_aircraft,
            "max_size": squadron.max_size,
            "pilots_active": len(squadron.active_pilots),
            "pilots_available": squadron.number_of_available_pilots,
        }
        if squadron.pending_deliveries:
            result["on_order"] = squadron.pending_deliveries
        if squadron.destination is not None:
            result["relocating_to"] = squadron.destination.name
        if squadron.pilot_limits_enabled:
            result["pilot_limit"] = squadron.pilot_limit
        return result

    def own_squadrons(self) -> list[Json]:
        return [
            self.own_squadron_json(s) for s in self.coalition.air_wing.iter_squadrons()
        ]

    def aircraft_task_weights(self) -> Json:
        """Auto-planner task weights of the aircraft types in use, from unit data."""
        weights: Json = {}
        for squadron in self.coalition.air_wing.iter_squadrons():
            aircraft = squadron.aircraft
            if aircraft.variant_id not in weights:
                weights[aircraft.variant_id] = {
                    task.value: weight
                    for task, weight in aircraft.task_priorities.items()
                }
        return weights

    # Ground objects -----------------------------------------------------------------

    def _tgo_header(self, tgo: TheaterGroundObject) -> Json:
        return {
            "id": str(tgo.id),
            "name": tgo.name,
            "category": tgo.category,
            "base": tgo.control_point.name,
            **position(tgo.position),
        }

    @staticmethod
    def _dead_unit_json(unit: TheaterUnit) -> Json:
        """A dead unit of an own site, named so repair tools can address it."""
        result: Json = {"unit": unit.name, "type": _unit_label(unit)}
        if unit.repair_turns_remaining is not None:
            result["repair_turns_remaining"] = unit.repair_turns_remaining
        elif not unit.repairable and not unit.is_static:
            result["repairable"] = False
        return result

    def own_ground_object_json(self, tgo: TheaterGroundObject) -> Json:
        result = self._tgo_header(tgo)
        units: dict[str, list[int]] = {}
        for unit in tgo.units:
            counts = units.setdefault(_unit_label(unit), [0, 0])
            counts[0] += int(unit.alive)
            counts[1] += 1
        result["units_alive_total"] = units
        dead = [self._dead_unit_json(unit) for unit in tgo.units if not unit.alive]
        if dead:
            result["dead_units"] = dead
        result["alive"] = tgo.alive_unit_count
        result["total"] = tgo.unit_count
        threat = tgo.max_threat_range().meters
        detection = tgo.max_detection_range().meters
        if threat:
            result["threat_nm"] = to_nm(threat)
        if detection:
            result["detection_nm"] = to_nm(detection)
        if tgo.category in REWARDS:
            alive_buildings = sum(1 for b in tgo.statics if b.alive)
            result["income"] = round(alive_buildings * REWARDS[tgo.category], 2)
        if tgo.is_iads:
            result["iads"] = True
        return result

    def enemy_ground_object_json(self, tgo: TheaterGroundObject) -> Json:
        result = self._tgo_header(tgo)
        if tgo.is_dead and not isinstance(tgo, MotorpoolGroundObject):
            result["status"] = "destroyed"
            return result
        if isinstance(tgo, MotorpoolGroundObject):
            # Its units are the enemy's reserve armor, which is hidden.
            result["status"] = "unknown"
            result["units"] = "unknown (reserve armor)"
            return result
        result["status"] = "active"
        result["units"] = dict(Counter(_unit_label(u) for u in tgo.units))
        threat, detection = nominal_ranges(tgo)
        if threat:
            result["threat_nm"] = to_nm(threat)
        if detection:
            result["detection_nm"] = to_nm(detection)
        if tgo.category in REWARDS:
            buildings = sum(1 for _ in tgo.statics)
            result["income_value"] = round(buildings * REWARDS[tgo.category], 2)
        if tgo.is_iads:
            result["iads"] = True
        return result

    def own_ground_object_summary(
        self, tgos: Iterable[TheaterGroundObject]
    ) -> dict[str, dict[str, list[int]]]:
        """Per base and category: [sites with survivors, sites]."""
        summary: dict[str, dict[str, list[int]]] = {}
        for tgo in tgos:
            counts = summary.setdefault(tgo.control_point.name, {}).setdefault(
                tgo.category, [0, 0]
            )
            counts[0] += int(not tgo.is_dead)
            counts[1] += 1
        return summary

    def enemy_ground_object_summary(
        self, tgos: Iterable[TheaterGroundObject]
    ) -> dict[str, dict[str, list[int]]]:
        """Per base and category: [sites not known destroyed, sites]."""
        summary: dict[str, dict[str, list[int]]] = {}
        for tgo in tgos:
            counts = summary.setdefault(tgo.control_point.name, {}).setdefault(
                tgo.category, [0, 0]
            )
            destroyed = tgo.is_dead and not isinstance(tgo, MotorpoolGroundObject)
            counts[0] += int(not destroyed)
            counts[1] += 1
        return summary

    def known_threats(self, include_detection_only: bool = True) -> list[KnownThreat]:
        """Known enemy air defenses (ground and naval), at full-strength ranges."""
        threats = []
        for tgo in self.enemy_ground_objects():
            if tgo.is_dead or isinstance(tgo, MotorpoolGroundObject):
                continue
            threat, detection = nominal_ranges(tgo)
            if not threat and not (include_detection_only and detection):
                continue
            threats.append(
                KnownThreat(
                    str(tgo.id),
                    tgo.name,
                    tgo.category,
                    tgo.control_point.name,
                    tgo.position,
                    threat,
                    detection,
                )
            )
        return threats

    # Front lines ------------------------------------------------------------------

    def front_line_json(self, front: FrontLine) -> Json:
        own_cp = front.control_point_friendly_to(self.player)
        enemy_cp = front.control_point_hostile_to(self.player)
        heading = front.blue_forward_heading
        if not self.player.is_blue:
            heading = heading.opposite
        result: Json = {
            "id": str(front.id),
            "name": front.name,
            "own_base": own_cp.name,
            "own_base_id": str(own_cp.id),
            "enemy_base": enemy_cp.name,
            "enemy_base_id": str(enemy_cp.id),
            **position(front.position),
            "own_forward_heading_deg": heading.degrees,
            "route_length_nm": to_nm(front.route_length),
            "distance_from_own_base_nm": to_nm(
                own_cp.position.distance_to_point(front.position)
            ),
            "distance_from_enemy_base_nm": to_nm(
                enemy_cp.position.distance_to_point(front.position)
            ),
        }
        stance = own_cp.stances.get(enemy_cp.id)
        result["own_stance"] = stance.name if stance is not None else None
        result["own_units_at_base"] = own_cp.base.total_armor
        result["own_deployable_units"] = own_cp.deployable_front_line_units
        result["own_base_strength"] = round(own_cp.base.strength, 2)
        return result

    def front_lines(self) -> list[Json]:
        return [self.front_line_json(f) for f in self.front_line_objects()]

    # Logistics ----------------------------------------------------------------------

    def own_transfers(self) -> list[Json]:
        transfers = []
        for transfer in self.coalition.transfers:
            transfers.append(
                {
                    "from": transfer.origin.name,
                    "to": transfer.destination.name,
                    "at": transfer.position.name,
                    "units": {u.variant_id: n for u, n in transfer.units.items()},
                    "transport": (
                        type(transfer.transport).__name__
                        if transfer.transport is not None
                        else None
                    ),
                }
            )
        return transfers

    # Air tasking order --------------------------------------------------------------

    def _target_json(self, target: MissionTarget) -> Json:
        kind = "other"
        target_id: Optional[str] = None
        if isinstance(target, ControlPoint):
            kind, target_id = "base", str(target.id)
        elif isinstance(target, TheaterGroundObject):
            kind, target_id = "ground_object", str(target.id)
        elif isinstance(target, FrontLine):
            kind, target_id = "front_line", str(target.id)
        result: Json = {"name": target.name, "kind": kind}
        if target_id is not None:
            result["id"] = target_id
        return result

    def _waypoints(self, flight: Flight) -> list[Json]:
        waypoints = []
        plan = flight.flight_plan
        for waypoint in plan.waypoints:
            entry: Json = {
                "name": waypoint.name,
                "type": waypoint.waypoint_type.name,
                "x": int(round(waypoint.position.x)),
                "y": int(round(waypoint.position.y)),
                "alt_ft": to_ft(waypoint.alt.meters),
            }
            if waypoint.alt_type != "BARO":
                entry["alt_type"] = waypoint.alt_type
            try:
                tot = _time(plan.tot_for_waypoint(waypoint))
            except Exception:
                tot = None
            if tot is not None:
                entry["tot"] = tot
            waypoints.append(entry)
        return waypoints

    def flight_json(self, flight: Flight, include_waypoints: bool) -> Json:
        result: Json = {
            "id": str(flight.id),
            "task": flight.flight_type.value,
            "aircraft": flight.unit_type.variant_id,
            "count": flight.count,
            "squadron": flight.squadron.name,
            "squadron_id": str(flight.squadron.id),
            "departure": flight.departure.name,
            "start_type": flight.start_type.value,
        }
        if flight.arrival != flight.departure:
            result["arrival"] = flight.arrival.name
        if flight.client_count:
            result["player_slots"] = flight.client_count
        members = list(flight.iter_members())
        if members:
            result["loadout"] = members[0].loadout.name
        plan = flight.flight_plan
        try:
            result["takeoff"] = _time(plan.takeoff_time())
            result["tot"] = _time(plan.tot)
        except Exception:
            pass
        if include_waypoints:
            result["waypoints"] = self._waypoints(flight)
        else:
            result["waypoint_count"] = len(plan.waypoints)
        return result

    def package_json(self, package: Package, include_waypoints: bool) -> Json:
        primary = package.primary_task
        result: Json = {
            "id": PACKAGE_IDS.id_for(package),
            "target": self._target_json(package.target),
            "primary_task": primary.value if primary is not None else None,
            "tot": _time(package.time_over_target),
        }
        if package.auto_asap:
            result["asap"] = True
        if package.player_locked:
            # Made or edited by the player: commanders must not change it.
            result["locked"] = True
        if package.commander_purpose:
            result["purpose"] = package.commander_purpose
        result["flights"] = [
            self.flight_json(f, include_waypoints) for f in package.flights
        ]
        return result

    def ato(self, include_waypoints: bool = False) -> list[Json]:
        return [
            self.package_json(p, include_waypoints) for p in self.coalition.ato.packages
        ]

    # Mission targets -----------------------------------------------------------------

    def mission_target_objects(self) -> Iterator[MissionTarget]:
        """Everything this side could plan a package against."""
        # Neutral bases too: they can be taken by air assault.
        yield from self.game.theater.controlpoints
        for front in self.front_line_objects():
            yield front
        for cp in self.game.theater.controlpoints:
            if cp.captured.is_neutral:
                continue
            for tgo in cp.ground_objects:
                if tgo.is_dead and not isinstance(tgo, MotorpoolGroundObject):
                    continue
                yield tgo

    def mission_types_for(self, target: MissionTarget) -> list[FlightType]:
        seen: dict[FlightType, None] = {}
        for task in target.mission_types(self.player):
            seen[task] = None
        if isinstance(target, ControlPoint) and target.captured.is_neutral:
            # Nobody defends or uses a neutral base; the only point in going there
            # is taking it, with whatever protects the helicopters on the way.
            allowed = {
                FlightType.AIR_ASSAULT,
                FlightType.ESCORT,
                FlightType.SEAD_ESCORT,
                FlightType.TARCAP,
                FlightType.SWEEP,
            }
            return [t for t in seen if t in allowed]
        return list(seen)

    # Catalogue ------------------------------------------------------------------------

    def faction_catalogue(self, faction: Faction) -> Json:
        def names(units: Iterable[Any]) -> list[str]:
            return sorted(u.variant_id for u in units)

        return {
            "faction": faction.name,
            "aircraft_types": names(faction.aircraft),
            "awacs": names(faction.awacs),
            "tankers": names(faction.tankers),
            "frontline_units": names(faction.frontline_units),
            "artillery_units": names(faction.artillery_units),
            "air_defense_units": names(faction.air_defense_units),
            "missiles": names(faction.missiles),
            "naval_units": names(faction.naval_units),
            "carriers": sorted(c.variant_id for c in faction.carriers),
            "preset_groups": sorted(g.name for g in faction.preset_groups),
        }

    def enemy_sightings(self) -> list[Json]:
        return [
            {
                "aircraft": s.aircraft_type,
                "turn": s.turn,
                "how": s.how,
                **({"origin": s.origin} if s.origin else {}),
            }
            for s in self.sightings
        ]


def within(point: Point, center: Optional[Point], radius_m: Optional[float]) -> bool:
    if center is None or radius_m is None:
        return True
    return point.distance_to_point(center) <= radius_m


def segment_circle_crossing(
    start: Point, end: Point, center: Point, radius: float
) -> Optional[tuple[float, float, float]]:
    """Where a straight leg crosses a circle.

    Returns (entry, exit, closest) distances in meters measured from the start of
    the leg (entry/exit are clamped to the leg), or None if the leg stays outside.
    ``closest`` is the closest approach of the leg to the center.
    """
    dx = end.x - start.x
    dy = end.y - start.y
    length = math.hypot(dx, dy)
    fx = start.x - center.x
    fy = start.y - center.y
    if length == 0:
        distance = math.hypot(fx, fy)
        if distance <= radius:
            return 0.0, 0.0, distance
        return None
    ux, uy = dx / length, dy / length
    projection = -(fx * ux + fy * uy)
    clamped = min(max(projection, 0.0), length)
    closest = math.hypot(fx + ux * clamped, fy + uy * clamped)
    if closest > radius:
        return None
    perpendicular = math.hypot(fx + ux * projection, fy + uy * projection)
    half_chord = math.sqrt(max(radius * radius - perpendicular * perpendicular, 0.0))
    entry = max(projection - half_chord, 0.0)
    exit_ = min(projection + half_chord, length)
    if entry > length or exit_ < 0:
        return None
    return entry, exit_, closest
