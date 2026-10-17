"""Specification lookups for aircraft, ground units and ships."""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Iterable, Optional, TypeVar

from pydantic import BaseModel, Field

from game.ato.loadouts import Loadout
from game.claudecommander.context import CommanderContext, commander_tools
from game.ato.standoff import loadout_reach
from game.claudecommander.mcp.tools import ToolError
from game.claudecommander.tools.reach import REACH_NOTE, reach_json
from game.claudecommander.view import CommanderView, Json, to_nm
from game.dcs.aircrafttype import AircraftType
from game.dcs.groundunittype import GroundUnitType
from game.dcs.shipunittype import ShipUnitType
from game.dcs.unittype import UnitType

_DESCRIPTION_LIMIT = 600
_MAX_CANDIDATES = 25

T = TypeVar("T", bound=UnitType[Any])


def _plain(text: str, limit: int = _DESCRIPTION_LIMIT) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _find(kind: str, name: str, candidates: Iterable[T]) -> T:
    """Exact, then case-insensitive, then substring match on the unit names."""
    units = list(candidates)
    wanted = name.strip().lower()

    def names(unit: T) -> list[str]:
        return [unit.variant_id, unit.display_name, unit.dcs_id]

    exact = [u for u in units if name in names(u)]
    if exact:
        return exact[0]
    folded = [u for u in units if wanted in (n.lower() for n in names(u))]
    if len(folded) == 1:
        return folded[0]
    partial = folded or [u for u in units if any(wanted in n.lower() for n in names(u))]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ToolError(f"No {kind} matches {name!r}.")
    options = sorted({u.variant_id for u in partial})
    shown = ", ".join(options[:_MAX_CANDIDATES])
    more = (
        ""
        if len(options) <= _MAX_CANDIDATES
        else f" (+{len(options) - _MAX_CANDIDATES} more)"
    )
    raise ToolError(
        f"{name!r} matches several {kind}s: {shown}{more}. Be more specific."
    )


def _weapons(loadout: Loadout) -> list[str]:
    counts: Counter[str] = Counter()
    for weapon in loadout.pylons.values():
        if weapon is None or weapon.clsid == "<CLEAN>":
            continue
        counts[weapon.name] += 1
    return [f"{count}x {name}" for name, count in counts.items()]


def _loadouts(view: CommanderView, aircraft: AircraftType) -> list[Json]:
    game = view.game
    restrict = game.settings.restrict_weapons_by_date
    faction = view.coalition.faction
    result = []
    seen: set[str] = set()
    for loadout in Loadout.iter_for_aircraft(aircraft):
        if loadout.name in seen:
            continue
        seen.add(loadout.name)
        if restrict:
            loadout = loadout.degrade_for_date(aircraft, game.date, faction)
        entry: Json = {"name": loadout.name, "weapons": _weapons(loadout)}
        entry.update(reach_json(loadout_reach(loadout)))
        if not entry["weapons"]:
            entry["note"] = "This loadout carries no weapons."
        unknown = unknown_weapons(loadout)
        if unknown:
            entry["unknown_weapons"] = unknown
            entry["note"] = (
                "Some weapons come from a different version of the aircraft's "
                "mod; they may be missing from the pylons in DCS. Prefer another "
                "loadout."
            )
        result.append(entry)
    return result


def unknown_weapons(loadout: Loadout) -> list[str]:
    """CLSIDs of weapons in the loadout that pydcs doesn't define."""
    return sorted(
        {
            w.clsid
            for w in loadout.pylons.values()
            if w is not None and not w.known_to_pydcs
        }
    )


def _default_loadouts(aircraft: AircraftType) -> dict[str, str]:
    defaults = {}
    for task in aircraft.iter_task_capabilities():
        try:
            loadout = Loadout.default_for_task_and_aircraft(
                task, aircraft.dcs_unit_type
            )
        except Exception:
            logging.exception("Default loadout lookup failed for %s", aircraft)
            continue
        if loadout.name != "Empty":
            defaults[task.value] = loadout.name
    return defaults


def aircraft_json(view: CommanderView, aircraft: AircraftType, loadouts: bool) -> Json:
    dcs_type = aircraft.dcs_unit_type
    result: Json = {
        "id": aircraft.variant_id,
        "dcs_id": aircraft.dcs_id,
        "display_name": aircraft.display_name,
        "role": aircraft.role,
        "introduced": aircraft.year_introduced,
        "origin": aircraft.country_of_origin,
        "manufacturer": aircraft.manufacturer,
        "price": aircraft.price,
        "helicopter": aircraft.helicopter,
        "carrier_capable": aircraft.carrier_capable,
        "lha_capable": aircraft.lha_capable,
        "player_flyable": aircraft.flyable,
        "max_group_size": aircraft.max_group_size,
        "max_speed_kt": round(aircraft.max_speed.knots),
        "planner_max_mission_range_nm": round(
            aircraft.max_mission_range.nautical_miles
        ),
        "internal_fuel_kg": round(aircraft.max_fuel),
        "patrol_altitude_ft": round(aircraft.preferred_patrol_altitude.feet),
        "patrol_speed_kt": round(
            aircraft.preferred_patrol_speed(aircraft.preferred_patrol_altitude).knots
        ),
        "cruise_altitude_ft": round(aircraft.preferred_cruise_altitude.feet),
        "combat_altitude_ft": round(aircraft.preferred_combat_altitude.feet),
        "built_in_targeting_pod": aircraft.has_built_in_target_pod,
        "built_in_ecm": aircraft.has_built_in_ecm,
        "built_in_jammer": aircraft.has_built_in_jamming,
        "datalink": bool(getattr(dcs_type, "networked_datalink", False)),
        "eplrs": aircraft.eplrs_capable,
        "chaff_flare": [getattr(dcs_type, "chaff", 0), getattr(dcs_type, "flare", 0)],
        "task_weights": {
            task.value: weight for task, weight in aircraft.task_priorities.items()
        },
        "in_own_faction": aircraft in view.coalition.faction.aircraft,
        "in_enemy_faction": aircraft in view.enemy_coalition.faction.aircraft,
        "description": _plain(aircraft.description),
    }
    if aircraft.fuel_consumption is not None:
        fuel = aircraft.fuel_consumption
        result["fuel_lb"] = {
            "taxi": fuel.taxi,
            "climb_per_nm": fuel.climb,
            "cruise_per_nm": fuel.cruise,
            "combat_per_nm": fuel.combat,
            "min_safe": fuel.min_safe,
        }
    if aircraft.cabin_size:
        result["cabin_size"] = aircraft.cabin_size
    if aircraft.can_carry_crates:
        result["can_carry_crates"] = True
    if loadouts:
        result["default_loadout_by_task"] = _default_loadouts(aircraft)
        result["loadouts"] = _loadouts(view, aircraft)
        result["loadout_reach_note"] = REACH_NOTE
        result["loadouts_date_restricted"] = view.game.settings.restrict_weapons_by_date
        if view.game.settings.restrict_weapons_by_date:
            result["loadouts_as_of"] = view.game.date.isoformat()
    return result


def _range_nm(dcs_type: Any, attribute: str) -> Optional[float]:
    value = getattr(dcs_type, attribute, None)
    if not value:
        return None
    return to_nm(float(value))


def unit_json(view: CommanderView, unit: GroundUnitType | ShipUnitType) -> Json:
    dcs_type = unit.dcs_unit_type
    result: Json = {
        "id": unit.variant_id,
        "dcs_id": unit.dcs_id,
        "display_name": unit.display_name,
        "kind": "ship" if isinstance(unit, ShipUnitType) else "ground_unit",
        "class": unit.unit_class.value,
        "role": unit.role,
        "introduced": unit.year_introduced,
        "origin": unit.country_of_origin,
        "price": unit.price,
    }
    for key, attribute in (
        ("threat_range_nm", "threat_range"),
        ("detection_range_nm", "detection_range"),
        ("air_weapon_range_nm", "air_weapon_dist"),
    ):
        value = _range_nm(dcs_type, attribute)
        if value is not None:
            result[key] = value
    if isinstance(unit, GroundUnitType):
        skynet = unit.skynet_properties.to_dict()
        if skynet:
            result["skynet"] = skynet
    own = view.coalition.faction
    enemy = view.enemy_coalition.faction

    def in_faction(faction: Any) -> bool:
        if isinstance(unit, ShipUnitType):
            return unit in faction.naval_units or unit in faction.carriers
        return any(
            unit in group
            for group in (
                faction.frontline_units,
                faction.artillery_units,
                faction.infantry_units,
                faction.logistics_units,
                faction.air_defense_units,
                faction.missiles,
            )
        )

    result["in_own_faction"] = in_faction(own)
    result["in_enemy_faction"] = in_faction(enemy)
    result["description"] = _plain(unit.description)
    return result


class LookupAircraftArgs(BaseModel):
    name: str = Field(
        description="Aircraft id (as used elsewhere), display name or DCS type id. "
        "Partial names work if unambiguous."
    )
    include_loadouts: bool = Field(
        default=True, description="Include named loadouts and their weapons."
    )


class LookupUnitArgs(BaseModel):
    name: str = Field(
        description="Ground unit or ship id, display name or DCS type id. Partial "
        "names work if unambiguous."
    )


def _all_ground_units() -> list[GroundUnitType]:
    units: list[GroundUnitType] = []
    for dcs_type in GroundUnitType.each_dcs_type():
        units.extend(GroundUnitType.for_dcs_type(dcs_type))
    return units


def _all_ships() -> list[ShipUnitType]:
    units: list[ShipUnitType] = []
    for dcs_type in ShipUnitType.each_dcs_type():
        units.extend(ShipUnitType.for_dcs_type(dcs_type))
    return units


@commander_tools.register(
    "lookup_aircraft",
    "Specifications of an aircraft type: role, introduction year, helicopter / "
    "carrier capability, max speed, the auto-planner's max mission range, "
    "patrol/cruise/combat altitude and speed, fuel and fuel burn (lb/nm), "
    "built-in pods/jammers, the task weights the game's auto-planner uses, the "
    "default loadout per task and every named loadout with its weapons (degraded "
    "to the campaign date when weapon date restriction is on), its approximate "
    "air-to-ground weapon reach (reach_nm, the weapon giving it) and weapon kinds. "
    "Works for any aircraft, including the enemy's.",
    LookupAircraftArgs,
    read_only=True,
)
def lookup_aircraft(context: CommanderContext, args: LookupAircraftArgs) -> Json:
    aircraft = _find("aircraft", args.name, AircraftType.iter_all())
    return aircraft_json(context.view(), aircraft, args.include_loadouts)


@commander_tools.register(
    "lookup_unit",
    "Specifications of a ground unit or ship type: class (e.g. SearchRadar, "
    "TrackRadar, Launcher, TELAR, AAA, Tank), role, introduction year, price, "
    "threat range, detection range and air weapon range in nm, and Skynet IADS "
    "properties if any.",
    LookupUnitArgs,
    read_only=True,
)
def lookup_unit(context: CommanderContext, args: LookupUnitArgs) -> Json:
    units: list[GroundUnitType | ShipUnitType] = [*_all_ground_units(), *_all_ships()]
    unit = _find("unit", args.name, units)
    return unit_json(context.view(), unit)
