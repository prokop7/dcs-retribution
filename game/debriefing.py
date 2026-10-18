from __future__ import annotations

import itertools
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    TYPE_CHECKING,
    TypeVar,
    Union,
)
from uuid import UUID

from game.dcs.aircrafttype import AircraftType
from game.dcs.groundunittype import GroundUnitType
from game.theater import Airfield, ControlPoint, Player

if TYPE_CHECKING:
    from game import Game
    from game.ato.flight import Flight
    from game.sim.simulationresults import SimulationResults
    from game.transfers import CargoShip
    from game.unitmap import (
        AirliftUnits,
        ConvoyUnit,
        FlyingUnit,
        FrontLineUnit,
        TheaterUnitMapping,
        UnitMap,
        SceneryObjectMapping,
    )

DEBRIEFING_LOG_EXTENSION = "log"

RecordT = TypeVar("RecordT")


@dataclass(frozen=True)
class AirLosses:
    player: list[FlyingUnit]
    enemy: list[FlyingUnit]

    @property
    def losses(self) -> Iterator[FlyingUnit]:
        return itertools.chain(self.player, self.enemy)

    def by_type(self, player: Player) -> Dict[AircraftType, int]:
        losses_by_type: Dict[AircraftType, int] = defaultdict(int)
        losses = self.player if player.is_blue else self.enemy
        for loss in losses:
            losses_by_type[loss.flight.unit_type] += 1
        return losses_by_type

    def surviving_flight_members(self, flight: Flight) -> int:
        losses = 0
        for loss in self.losses:
            if loss.flight == flight:
                losses += 1
        return flight.count - losses


@dataclass
class GroundLosses:
    player_front_line: List[FrontLineUnit] = field(default_factory=list)
    enemy_front_line: List[FrontLineUnit] = field(default_factory=list)

    player_motorpool: List[FrontLineUnit] = field(default_factory=list)
    enemy_motorpool: List[FrontLineUnit] = field(default_factory=list)

    player_convoy: List[ConvoyUnit] = field(default_factory=list)
    enemy_convoy: List[ConvoyUnit] = field(default_factory=list)

    player_cargo_ships: List[CargoShip] = field(default_factory=list)
    enemy_cargo_ships: List[CargoShip] = field(default_factory=list)

    player_airlifts: List[AirliftUnits] = field(default_factory=list)
    enemy_airlifts: List[AirliftUnits] = field(default_factory=list)

    player_ground_objects: List[TheaterUnitMapping] = field(default_factory=list)
    enemy_ground_objects: List[TheaterUnitMapping] = field(default_factory=list)

    player_scenery: List[SceneryObjectMapping] = field(default_factory=list)
    enemy_scenery: List[SceneryObjectMapping] = field(default_factory=list)

    player_airfields: List[Airfield] = field(default_factory=list)
    enemy_airfields: List[Airfield] = field(default_factory=list)


@dataclass(frozen=True)
class BaseCaptureEvent:
    control_point: ControlPoint
    captured_by_player: Player


@dataclass(frozen=True)
class SideLossCounts:
    aircraft: int
    front_line: int
    motorpool: int
    convoy: int
    cargo_ships: int
    airlift_cargo: int
    ground_objects: int
    scenery: int
    bases_lost: int
    runways_destroyed: int


@dataclass(frozen=True)
class KillRecord:
    """Who killed what in the mission, as reported by DCS (S_EVENT_KILL).

    Only present in results from missions generated with kill attribution;
    every field but the target may be missing.
    """

    target: str
    target_type: Optional[str] = None
    killer: Optional[str] = None
    killer_type: Optional[str] = None
    #: DCS coalition id of the killer: 1 red, 2 blue.
    killer_coalition: Optional[int] = None
    weapon: Optional[str] = None
    #: DCS map coordinates of the target when it died (x north, z east), meters.
    x: Optional[float] = None
    z: Optional[float] = None
    alt: Optional[float] = None
    #: Mission time in seconds.
    time: Optional[float] = None

    @classmethod
    def from_json(cls, data: Any) -> Optional[KillRecord]:
        if not isinstance(data, dict) or not data.get("target"):
            return None

        def number(key: str) -> Optional[float]:
            value = data.get(key)
            return float(value) if isinstance(value, (int, float)) else None

        def text(key: str) -> Optional[str]:
            value = data.get(key)
            return str(value) if value not in (None, "") else None

        coalition = data.get("killer_coalition")
        return cls(
            target=str(data["target"]),
            target_type=text("target_type"),
            killer=text("killer"),
            killer_type=text("killer_type"),
            killer_coalition=coalition if isinstance(coalition, int) else None,
            weapon=text("weapon"),
            x=number("x"),
            z=number("z"),
            alt=number("alt"),
            time=number("time"),
        )


def _json_number(data: dict[str, Any], key: str) -> Optional[float]:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _json_int(data: dict[str, Any], key: str) -> Optional[int]:
    value = data.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _json_text(data: dict[str, Any], key: str) -> Optional[str]:
    value = data.get(key)
    return str(value) if value not in (None, "") else None


@dataclass(frozen=True)
class ShotRecord:
    """A weapon fired in the mission (S_EVENT_SHOT); gun rounds and shells are
    not recorded. Every field but the id may be missing."""

    #: Sequential id; hits and fates refer to the shot by it.
    id: int
    time: Optional[float] = None
    shooter: Optional[str] = None
    shooter_type: Optional[str] = None
    #: DCS coalition id: 1 red, 2 blue.
    shooter_coalition: Optional[int] = None
    #: DCS weapon type name, e.g. "AGM_88".
    weapon: Optional[str] = None
    #: The weapon's display name, e.g. "AGM-88C HARM", when DCS gives one.
    display_name: Optional[str] = None
    #: "missile", "rocket", "bomb" or "torpedo".
    category: Optional[str] = None
    #: For missiles: "AAM", "SAM", "BM", "ANTI_SHIP", "CRUISE" or "OTHER".
    missile_category: Optional[str] = None
    guidance: Optional[str] = None
    #: The weapon's target at launch (weapon:getTarget()), when it had one.
    target: Optional[str] = None
    target_type: Optional[str] = None
    target_coalition: Optional[int] = None
    #: True when the target was itself a weapon (a SAM fired at a missile).
    target_is_weapon: bool = False
    #: The shot id of the targeted weapon, when it was tracked.
    target_shot: Optional[int] = None
    #: Distance from the shooter to the target at launch, meters.
    range_m: Optional[float] = None

    @property
    def weapon_name(self) -> str:
        return self.display_name or self.weapon or "unknown weapon"

    @classmethod
    def from_json(cls, data: Any) -> Optional[ShotRecord]:
        if not isinstance(data, dict):
            return None
        shot_id = _json_int(data, "id")
        if shot_id is None:
            return None
        return cls(
            id=shot_id,
            time=_json_number(data, "time"),
            shooter=_json_text(data, "shooter"),
            shooter_type=_json_text(data, "shooter_type"),
            shooter_coalition=_json_int(data, "shooter_coalition"),
            weapon=_json_text(data, "weapon"),
            display_name=_json_text(data, "display_name"),
            category=_json_text(data, "category"),
            missile_category=_json_text(data, "missile_category"),
            guidance=_json_text(data, "guidance"),
            target=_json_text(data, "target"),
            target_type=_json_text(data, "target_type"),
            target_coalition=_json_int(data, "target_coalition"),
            target_is_weapon=data.get("target_is_weapon") is True,
            target_shot=_json_int(data, "target_shot"),
            range_m=_json_number(data, "range_m"),
        )


@dataclass(frozen=True)
class HitRecord:
    """A weapon hit (S_EVENT_HIT); gun rounds and shells are not recorded."""

    time: Optional[float] = None
    weapon: Optional[str] = None
    #: The shot id of the weapon, when it was tracked.
    shot: Optional[int] = None
    shooter: Optional[str] = None
    shooter_type: Optional[str] = None
    shooter_coalition: Optional[int] = None
    target: Optional[str] = None
    target_type: Optional[str] = None
    target_coalition: Optional[int] = None
    target_is_weapon: bool = False
    target_shot: Optional[int] = None

    @classmethod
    def from_json(cls, data: Any) -> Optional[HitRecord]:
        if not isinstance(data, dict):
            return None
        return cls(
            time=_json_number(data, "time"),
            weapon=_json_text(data, "weapon"),
            shot=_json_int(data, "shot"),
            shooter=_json_text(data, "shooter"),
            shooter_type=_json_text(data, "shooter_type"),
            shooter_coalition=_json_int(data, "shooter_coalition"),
            target=_json_text(data, "target"),
            target_type=_json_text(data, "target_type"),
            target_coalition=_json_int(data, "target_coalition"),
            target_is_weapon=data.get("target_is_weapon") is True,
            target_shot=_json_int(data, "target_shot"),
        )


@dataclass(frozen=True)
class WeaponEngagement:
    """A defending weapon fired at a tracked weapon."""

    shot: Optional[int] = None
    shooter: Optional[str] = None
    shooter_type: Optional[str] = None
    weapon: Optional[str] = None

    @classmethod
    def from_json(cls, data: Any) -> Optional[WeaponEngagement]:
        if not isinstance(data, dict):
            return None
        return cls(
            shot=_json_int(data, "shot"),
            shooter=_json_text(data, "shooter"),
            shooter_type=_json_text(data, "shooter_type"),
            weapon=_json_text(data, "weapon"),
        )


WEAPON_FATES = ("hit", "intercepted", "no_hit")


@dataclass(frozen=True)
class WeaponFate:
    """What became of a tracked weapon once it no longer existed.

    "hit": DCS reported it hitting (or killing) something. "intercepted": a
    defending weapon fired at it hit it, or ended within a couple of seconds of
    it without it hitting anything; this is inferred and approximate. "no_hit":
    it vanished without a reported hit. Weapons still flying at mission end
    have no fate.
    """

    shot: int
    fate: str
    weapon: Optional[str] = None
    #: Mission time it was found gone, seconds.
    time: Optional[float] = None
    #: Last known position (DCS x north, z east, alt), meters.
    x: Optional[float] = None
    z: Optional[float] = None
    alt: Optional[float] = None
    intercepted_by: Optional[WeaponEngagement] = None
    engaged_by: tuple[WeaponEngagement, ...] = ()

    @classmethod
    def from_json(cls, data: Any) -> Optional[WeaponFate]:
        if not isinstance(data, dict):
            return None
        shot = _json_int(data, "shot")
        fate = data.get("fate")
        if shot is None or fate not in WEAPON_FATES:
            return None
        engaged = data.get("engaged_by")
        return cls(
            shot=shot,
            fate=str(fate),
            weapon=_json_text(data, "weapon"),
            time=_json_number(data, "time"),
            x=_json_number(data, "x"),
            z=_json_number(data, "z"),
            alt=_json_number(data, "alt"),
            intercepted_by=WeaponEngagement.from_json(data.get("intercepted_by")),
            engaged_by=tuple(
                engagement
                for engagement in map(
                    WeaponEngagement.from_json,
                    engaged if isinstance(engaged, list) else [],
                )
                if engagement is not None
            ),
        )


def _parse_records(
    data: Dict[str, Any], key: str, parse: Callable[[Any], Optional[RecordT]]
) -> list[RecordT]:
    values = data.get(key)
    if not isinstance(values, list):
        return []
    return [record for record in map(parse, values) if record is not None]


@dataclass(frozen=True)
class StateData:
    #: True if the mission ended. If False, the mission exited abnormally.
    mission_ended: bool

    #: Names of aircraft units that were killed during the mission.
    killed_aircraft: List[str]

    #: Names of vehicles, ships or buildings that were killed during the mission.
    killed_ground_units: List[str]

    #: List of descriptions of destroyed statics. Format of each element is a mapping of
    #: the coordinate type ("x", "y", "z", "type", "orientation") to the value.
    destroyed_statics: List[dict[str, Union[float, str]]]

    #: Mangled names of bases that were captured during the mission.
    base_capture_events: List[str]

    #: Kill attribution, when the mission recorded it.
    kill_records: List[KillRecord] = field(default_factory=list)

    #: The weapon feed (shots, hits and weapon fates), when the mission recorded
    #: it. Missions generated before it have none.
    shot_records: List[ShotRecord] = field(default_factory=list)
    hit_records: List[HitRecord] = field(default_factory=list)
    weapon_fates: List[WeaponFate] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: Dict[str, Any], unit_map: UnitMap) -> StateData:
        def clean_unit_list(unit_list: List[Any]) -> List[str]:
            # Cleans list of units in state.json by
            # - Removing duplicates. Airfields emit a new "dead" event every time a bomb
            #   is dropped on them when they've already dead.
            # - Normalise dead map objects (which are ints) to strings. The unit map
            #   only stores strings
            units = set()
            for unit in unit_list:
                units.add(str(unit))
            return list(units)

        killed_aircraft = []
        killed_ground_units = []

        # Process killed units from S_EVENT_UNIT_LOST, S_EVENT_CRASH, S_EVENT_DEAD & S_EVENT_KILL
        # Try to process every event that could indicate a unit was killed, even if it is
        # inefficient and results in duplication as the logic DCS uses to trigger the various
        # event types is not clear and may change over time.
        killed_units = clean_unit_list(
            data.get("unit_lost_events", [])
            + data.get("kill_events", [])
            + data.get("crash_events", [])
            + data.get("dead_events", [])
        )
        for unit in killed_units:  # organize killed units into aircraft vs ground
            if unit_map.flight(unit) is not None:
                killed_aircraft.append(unit)
            else:
                killed_ground_units.append(unit)

        return cls(
            mission_ended=data.get("mission_ended", False),
            killed_aircraft=killed_aircraft,
            killed_ground_units=killed_ground_units,
            destroyed_statics=data.get("destroyed_objects_positions", []),
            base_capture_events=data.get("base_capture_events", []),
            kill_records=[
                record
                for record in map(KillRecord.from_json, data.get("kill_details", []))
                if record is not None
            ],
            shot_records=_parse_records(data, "shot_events", ShotRecord.from_json),
            hit_records=_parse_records(data, "hit_events", HitRecord.from_json),
            weapon_fates=_parse_records(data, "weapon_fates", WeaponFate.from_json),
        )


class Debriefing:
    def __init__(
        self, state_data: Dict[str, Any], game: Game, unit_map: UnitMap
    ) -> None:
        self.state_data = StateData.from_json(state_data, unit_map)
        self.game = game
        self.unit_map = unit_map

        self.player_country = game.blue.faction.country.name
        self.enemy_country = game.red.faction.country.name

        self.air_losses = self.dead_aircraft()
        self.ground_losses = self.dead_ground_units()
        self.base_captures = self.base_capture_events()

    def merge_simulation_results(self, results: SimulationResults) -> None:
        for air_loss in results.air_losses:
            if air_loss.flight.squadron.player.is_blue:
                self.air_losses.player.append(air_loss)
            else:
                self.air_losses.enemy.append(air_loss)

    @property
    def front_line_losses(self) -> Iterator[FrontLineUnit]:
        yield from self.ground_losses.player_front_line
        yield from self.ground_losses.enemy_front_line

    @property
    def motorpool_losses(self) -> Iterator[FrontLineUnit]:
        yield from self.ground_losses.player_motorpool
        yield from self.ground_losses.enemy_motorpool

    @property
    def convoy_losses(self) -> Iterator[ConvoyUnit]:
        yield from self.ground_losses.player_convoy
        yield from self.ground_losses.enemy_convoy

    @property
    def cargo_ship_losses(self) -> Iterator[CargoShip]:
        yield from self.ground_losses.player_cargo_ships
        yield from self.ground_losses.enemy_cargo_ships

    @property
    def airlift_losses(self) -> Iterator[AirliftUnits]:
        yield from self.ground_losses.player_airlifts
        yield from self.ground_losses.enemy_airlifts

    @property
    def ground_object_losses(self) -> Iterator[TheaterUnitMapping]:
        yield from self.ground_losses.player_ground_objects
        yield from self.ground_losses.enemy_ground_objects

    @property
    def scenery_object_losses(self) -> Iterator[SceneryObjectMapping]:
        yield from self.ground_losses.player_scenery
        yield from self.ground_losses.enemy_scenery

    @property
    def damaged_runways(self) -> Iterator[Airfield]:
        yield from self.ground_losses.player_airfields
        yield from self.ground_losses.enemy_airfields

    @cached_property
    def _casualties_by_origin(self) -> Dict[ControlPoint, int]:
        # commit_front_line_battle_impact() calls casualty_count() twice per
        # front-line pair; computing this once avoids re-scanning every
        # front-line loss on each call (O(pairs x losses), thousands per mission).
        counts: Dict[ControlPoint, int] = defaultdict(int)
        for loss in self.front_line_losses:
            counts[loss.origin] += 1
        return counts

    def casualty_count(self, control_point: ControlPoint) -> int:
        return self._casualties_by_origin.get(control_point, 0)

    def front_line_losses_by_type(self, player: Player) -> dict[GroundUnitType, int]:
        losses_by_type: dict[GroundUnitType, int] = defaultdict(int)
        if player.is_blue:
            losses = self.ground_losses.player_front_line
        else:
            losses = self.ground_losses.enemy_front_line
        for loss in losses:
            losses_by_type[loss.unit_type] += 1
        return losses_by_type

    def motorpool_losses_by_type(self, player: Player) -> dict[GroundUnitType, int]:
        losses_by_type: dict[GroundUnitType, int] = defaultdict(int)
        if player.is_blue:
            losses = self.ground_losses.player_motorpool
        else:
            losses = self.ground_losses.enemy_motorpool
        for loss in losses:
            losses_by_type[loss.unit_type] += 1
        return losses_by_type

    def convoy_losses_by_type(self, player: Player) -> dict[GroundUnitType, int]:
        losses_by_type: dict[GroundUnitType, int] = defaultdict(int)
        if player.is_blue:
            losses = self.ground_losses.player_convoy
        else:
            losses = self.ground_losses.enemy_convoy
        for loss in losses:
            losses_by_type[loss.unit_type] += 1
        return losses_by_type

    def cargo_ship_losses_by_type(self, player: Player) -> dict[GroundUnitType, int]:
        losses_by_type: dict[GroundUnitType, int] = defaultdict(int)
        if player.is_blue:
            ships = self.ground_losses.player_cargo_ships
        else:
            ships = self.ground_losses.enemy_cargo_ships
        for ship in ships:
            for unit_type, count in ship.units.items():
                losses_by_type[unit_type] += count
        return losses_by_type

    def airlift_losses_by_type(self, player: Player) -> dict[GroundUnitType, int]:
        losses_by_type: dict[GroundUnitType, int] = defaultdict(int)
        if player.is_blue:
            losses = self.ground_losses.player_airlifts
        else:
            losses = self.ground_losses.enemy_airlifts
        for loss in losses:
            for unit_type in loss.cargo:
                losses_by_type[unit_type] += 1
        return losses_by_type

    def ground_object_losses_by_type(self, player: Player) -> Dict[str, int]:
        losses_by_type: Dict[str, int] = defaultdict(int)
        if player.is_blue:
            losses = self.ground_losses.player_ground_objects
        else:
            losses = self.ground_losses.enemy_ground_objects
        for loss in losses:
            losses_by_type[loss.theater_unit.type.id] += 1
        return losses_by_type

    def scenery_losses_by_type(self, player: Player) -> Dict[str, int]:
        losses_by_type: Dict[str, int] = defaultdict(int)
        if player.is_blue:
            losses = self.ground_losses.player_scenery
        else:
            losses = self.ground_losses.enemy_scenery
        for loss in losses:
            losses_by_type[loss.trigger_zone.name] += 1
        return losses_by_type

    def loss_counts(self, player: Player) -> SideLossCounts:
        gl = self.ground_losses
        if player.is_blue:
            air = self.air_losses.player
            front_line = gl.player_front_line
            motorpool = gl.player_motorpool
            convoy = gl.player_convoy
            cargo_ships = gl.player_cargo_ships
            airlifts = gl.player_airlifts
            ground_objects = gl.player_ground_objects
            scenery = gl.player_scenery
            airfields = gl.player_airfields
        else:
            air = self.air_losses.enemy
            front_line = gl.enemy_front_line
            motorpool = gl.enemy_motorpool
            convoy = gl.enemy_convoy
            cargo_ships = gl.enemy_cargo_ships
            airlifts = gl.enemy_airlifts
            ground_objects = gl.enemy_ground_objects
            scenery = gl.enemy_scenery
            airfields = gl.enemy_airfields
        return SideLossCounts(
            aircraft=len(air),
            front_line=len(front_line),
            motorpool=len(motorpool),
            convoy=len(convoy),
            cargo_ships=len(cargo_ships),
            airlift_cargo=sum(len(loss.cargo) for loss in airlifts),
            ground_objects=len(ground_objects),
            scenery=len(scenery),
            bases_lost=sum(
                1
                for capture in self.base_captures
                if capture.captured_by_player == player.opponent
            ),
            runways_destroyed=len(airfields),
        )

    def dead_aircraft(self) -> AirLosses:
        player_losses = []
        enemy_losses = []
        for unit_name in self.state_data.killed_aircraft:
            aircraft = self.unit_map.flight(unit_name)
            if aircraft is None:
                logging.error(f"Could not find Flight matching {unit_name}")
                continue
            if aircraft.flight.departure.captured.is_blue:
                player_losses.append(aircraft)
            else:
                enemy_losses.append(aircraft)
        return AirLosses(player_losses, enemy_losses)

    def dead_ground_units(self) -> GroundLosses:
        losses = GroundLosses()
        untracked: List[str] = []
        for unit_name in self.state_data.killed_ground_units:
            front_line_unit = self.unit_map.front_line_unit(unit_name)
            if front_line_unit is not None:
                if front_line_unit.origin.captured.is_blue:
                    losses.player_front_line.append(front_line_unit)
                else:
                    losses.enemy_front_line.append(front_line_unit)
                continue

            motorpool_unit = self.unit_map.motorpool_unit(unit_name)
            if motorpool_unit is not None:
                if motorpool_unit.origin.captured.is_blue:
                    losses.player_motorpool.append(motorpool_unit)
                else:
                    losses.enemy_motorpool.append(motorpool_unit)
                continue

            convoy_unit = self.unit_map.convoy_unit(unit_name)
            if convoy_unit is not None:
                if convoy_unit.convoy.player_owned.is_blue:
                    losses.player_convoy.append(convoy_unit)
                else:
                    losses.enemy_convoy.append(convoy_unit)
                continue

            cargo_ship = self.unit_map.cargo_ship(unit_name)
            if cargo_ship is not None:
                if cargo_ship.player_owned.is_blue:
                    losses.player_cargo_ships.append(cargo_ship)
                else:
                    losses.enemy_cargo_ships.append(cargo_ship)
                continue

            ground_object = self.unit_map.theater_units(unit_name)
            if ground_object is not None:
                if ground_object.theater_unit.ground_object.is_friendly(
                    to_player=Player.BLUE
                ):
                    losses.player_ground_objects.append(ground_object)
                else:
                    losses.enemy_ground_objects.append(ground_object)
                continue

            scenery_object = self.unit_map.scenery_object(unit_name)
            # Try appending object to the name, because we do this for building statics.
            if scenery_object is not None:
                if scenery_object.ground_unit.ground_object.is_friendly(
                    to_player=Player.BLUE
                ):
                    losses.player_scenery.append(scenery_object)
                else:
                    losses.enemy_scenery.append(scenery_object)
                continue

            airfield = self.unit_map.airfield(unit_name)
            if airfield is not None:
                if airfield.captured.is_blue:
                    losses.player_airfields.append(airfield)
                elif airfield.captured.is_red:
                    losses.enemy_airfields.append(airfield)
                continue

            # We don't track infantry or map/scenery objects, so a mission can
            # end with thousands of these unclaimed deaths. Collect them and log
            # one summary instead of a line each: per-unit logging here floods
            # the handlers (a file stat + flush per line, plus the log-window UI
            # hook) and froze the debrief for ~20s on busy missions.
            untracked.append(unit_name)

        if untracked:
            logging.debug(
                "%d untracked ground unit deaths had no effect (untracked "
                "infantry or map/scenery objects). First few: %s",
                len(untracked),
                ", ".join(untracked[:10]),
            )

        for unit_name in self.state_data.killed_aircraft:
            airlift_unit = self.unit_map.airlift_unit(unit_name)
            if airlift_unit is not None:
                if airlift_unit.transfer.player.is_blue:
                    losses.player_airlifts.append(airlift_unit)
                else:
                    losses.enemy_airlifts.append(airlift_unit)
                continue

        return losses

    def base_capture_events(self) -> List[BaseCaptureEvent]:
        """Keeps only the last instance of a base capture event for each base ID."""
        blue_coalition_id = 2
        seen = set()
        captures = []
        for capture in reversed(self.state_data.base_capture_events):
            # The ID string in the JSON file will be the UUID generated from retribution
            cp_id, new_owner_id_str, _name = capture.split("||")

            # Only the most recent capture event matters.
            if cp_id in seen:
                continue
            seen.add(cp_id)

            try:
                control_point = self.game.theater.find_control_point_by_id(UUID(cp_id))
            except (KeyError, ValueError):
                # Captured base is not a part of the campaign. This happens when neutral
                # bases are near the conflict. Nothing to do.
                continue
            if int(new_owner_id_str) == blue_coalition_id:
                captured_by_player = Player.BLUE
            else:
                captured_by_player = Player.RED
            if control_point.is_friendly(to_player=captured_by_player):
                # Base is currently friendly to the new owner. Was captured and
                # recaptured in the same mission. Nothing to do.
                continue

            captures.append(BaseCaptureEvent(control_point, captured_by_player))
        return captures
