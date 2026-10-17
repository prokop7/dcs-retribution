"""Flight loadouts and weapon lookups."""

from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

from pydantic import BaseModel, Field, model_validator

from game.ato.loadouts import Loadout
from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.domains import Domain, require_domain
from game.claudecommander.mcp.tools import ToolError
from game.claudecommander.tools.airplan import editable_flight, list_text
from game.claudecommander.view import CommanderView, Json
from game.data.weapons import Pylon, Weapon, WeaponGroup
from game.dcs.aircrafttype import AircraftType

if TYPE_CHECKING:
    from game.ato.flight import Flight


def named_loadouts(aircraft: AircraftType) -> dict[str, Loadout]:
    loadouts: dict[str, Loadout] = {}
    for loadout in Loadout.iter_for_aircraft(aircraft):
        loadouts.setdefault(loadout.name, loadout)
    return loadouts


def find_named_loadout(aircraft: AircraftType, name: str) -> Loadout:
    loadouts = named_loadouts(aircraft)
    if name in loadouts:
        return loadouts[name]
    folded = [n for n in loadouts if n.lower() == name.strip().lower()]
    if len(folded) == 1:
        return loadouts[folded[0]]
    raise ToolError(
        f"{aircraft.variant_id} has no loadout named {name!r}. Named loadouts: "
        f"{list_text(sorted(loadouts), 60)}. lookup_aircraft lists their weapons."
    )


def _weapon_label(weapon: Weapon) -> str:
    return f"{weapon.name} [{weapon.clsid}]"


def _weapon_year(weapon: Weapon, view: CommanderView) -> Optional[int]:
    year = weapon.weapon_group.introduction_year
    overrides = getattr(
        view.coalition.faction, "weapons_introduction_year_overrides", {}
    )
    return overrides.get(weapon.weapon_group.name, year)


def _resolve_weapon(pylon: Pylon, wanted: str) -> Weapon:
    weapon = Weapon.with_clsid(wanted)
    if weapon is not None:
        if not pylon.can_equip(weapon):
            raise ToolError(
                f"Pylon {pylon.number} cannot carry {weapon.name} ({wanted}). "
                f"It can carry: {list_text(sorted(w.name for w in pylon.allowed), 40)}."
            )
        return weapon
    folded = wanted.strip().lower()
    exact = [w for w in pylon.allowed if w.name.lower() == folded]
    if len(exact) >= 1:
        return exact[0]
    partial = [w for w in pylon.allowed if folded in w.name.lower()]
    names = sorted({w.name for w in partial})
    if len(names) == 1:
        return partial[0]
    if not partial:
        raise ToolError(
            f"Pylon {pylon.number} has no weapon matching {wanted!r}. It can carry: "
            f"{list_text(sorted(w.name for w in pylon.allowed), 40)}."
        )
    raise ToolError(
        f"{wanted!r} matches several weapons for pylon {pylon.number}: "
        f"{list_text(names, 20)}. Give the exact name or CLSID."
    )


def loadout_json(view: CommanderView, flight: Flight) -> Json:
    """The flight lead's loadout, as generated in the mission."""
    members = list(flight.iter_members())
    if not members:
        return {}
    loadout = members[0].loadout
    game = view.game
    result: Json = {
        "name": loadout.name,
        "custom": loadout.is_custom,
        "pylons": {
            str(n): w.name for n, w in sorted(loadout.pylons.items()) if w is not None
        },
    }
    if game.settings.restrict_weapons_by_date:
        degraded = loadout.degrade_for_date(
            flight.unit_type, game.date, view.coalition.faction, flight.package.target
        )
        changes = {}
        for number, weapon in loadout.pylons.items():
            if weapon is None:
                continue
            replacement = degraded.pylons.get(number)
            if replacement != weapon:
                changes[str(number)] = (
                    replacement.name if replacement is not None else "(removed)"
                )
        if changes:
            result["replaced_for_campaign_date"] = changes
    return result


def apply_named_loadout(view: CommanderView, flight: Flight, name: str) -> Json:
    loadout = find_named_loadout(flight.unit_type, name)
    for member in flight.iter_members():
        member.loadout = loadout.clone()
        member.use_custom_loadout = False
    flight.use_same_loadout_for_all_members = True
    return loadout_json(view, flight)


def apply_pylons(
    view: CommanderView, flight: Flight, pylons: dict[str, Optional[str]]
) -> Json:
    game = view.game
    aircraft = flight.unit_type
    valid_pylons = sorted(aircraft.dcs_unit_type.pylons)
    members = list(flight.iter_members())
    base = members[0].loadout if members else Loadout.empty_loadout()
    new_pylons = dict(base.pylons)
    settings = dict(base.pylon_settings)
    restrict = game.settings.restrict_weapons_by_date
    faction = view.coalition.faction
    for key, wanted in pylons.items():
        try:
            number = int(key)
        except ValueError:
            raise ToolError(f"Pylon {key!r} is not a pylon number.")
        if number not in valid_pylons:
            raise ToolError(
                f"{aircraft.variant_id} has no pylon {number}. Pylons: "
                f"{', '.join(str(p) for p in valid_pylons)}."
            )
        if wanted is None or not wanted.strip():
            new_pylons.pop(number, None)
            settings.pop(number, None)
            continue
        pylon = Pylon.for_aircraft(aircraft, number)
        weapon = _resolve_weapon(pylon, wanted)
        if restrict and not weapon.available_on(game.date, faction):
            available = sorted(w.name for w in pylon.available_on(game.date, faction))
            raise ToolError(
                f"{weapon.name} (introduced {_weapon_year(weapon, view)}) is not "
                f"available on the campaign date {game.date.isoformat()} (weapon "
                f"date restriction is on). Pylon {number} can carry on this date: "
                f"{list_text(available, 40)}."
            )
        new_pylons[number] = weapon
        settings.pop(number, None)
    loadout = Loadout(
        "Custom", new_pylons, base.date, is_custom=True, pylon_settings=settings
    )
    for member in members:
        member.loadout = loadout.clone()
        member.use_custom_loadout = True
    flight.use_same_loadout_for_all_members = True
    return loadout_json(view, flight)


class SetLoadoutArgs(BaseModel):
    flight_id: str
    loadout_name: Optional[str] = Field(
        default=None,
        description="A named loadout of the aircraft, as listed by lookup_aircraft.",
    )
    pylons: Optional[dict[str, Optional[str]]] = Field(
        default=None,
        description='Pylon-by-pylon changes to the current loadout: {"<pylon '
        'number>": weapon CLSID or name, or null to empty the pylon}. Pylons not '
        "listed keep their weapon. Applied after loadout_name if both are given.",
    )

    @model_validator(mode="after")
    def _something(self) -> SetLoadoutArgs:
        if self.loadout_name is None and not self.pylons:
            raise ValueError("give loadout_name, pylons or both")
        return self


@commander_tools.register(
    "set_flight_loadout",
    "Sets the loadout of every aircraft in a flight: a named loadout of the "
    "aircraft (see lookup_aircraft) and/or pylon-by-pylon changes (weapon CLSID or "
    "name, null to empty). Weapons are checked against the pylon and, when weapon "
    "date restriction is on, the campaign date. Returns the resulting pylons and "
    "any weapons the mission generator will replace for the campaign date.",
    SetLoadoutArgs,
)
def set_flight_loadout(context: CommanderContext, args: SetLoadoutArgs) -> Json:
    require_domain(context.game, context.player, Domain.AIR_TASKING)
    view = context.view()
    flight = editable_flight(view, args.flight_id)
    if not list(flight.iter_members()):
        raise ToolError(f"Flight {flight.id} has no aircraft.")
    result: Json = {}
    if args.loadout_name is not None:
        result = apply_named_loadout(view, flight, args.loadout_name)
    if args.pylons:
        result = apply_pylons(view, flight, args.pylons)
    context.events.update_flight(flight)
    return {"flight_id": str(flight.id), "loadout": result}


class LookupWeaponArgs(BaseModel):
    name: str = Field(
        description="Weapon CLSID, weapon name (as in loadouts) or weapon group "
        "name. Partial names work if unambiguous."
    )


def _find_group(name: str) -> tuple[WeaponGroup, Optional[Weapon]]:
    weapon = Weapon.with_clsid(name)
    if weapon is not None:
        return weapon.weapon_group, weapon
    WeaponGroup.load_all()
    groups = list(WeaponGroup._by_name.values())
    folded = name.strip().lower()
    for group in groups:
        if group.name.lower() == folded:
            return group, None
    by_weapon: dict[str, Weapon] = {}
    for group in groups:
        if group.name in ("Unknown",):
            continue
        for candidate in group.weapons:
            try:
                weapon_name = candidate.name
            except KeyError:
                continue
            if weapon_name.lower() == folded:
                return group, candidate
            if folded in weapon_name.lower() or folded in group.name.lower():
                by_weapon.setdefault(group.name, candidate)
    if len(by_weapon) == 1:
        candidate = next(iter(by_weapon.values()))
        return candidate.weapon_group, candidate
    if not by_weapon:
        raise ToolError(f"No weapon matches {name!r}.")
    raise ToolError(
        f"{name!r} matches several weapon groups: {list_text(sorted(by_weapon), 25)}. "
        "Be more specific."
    )


def _carriers(view: CommanderView, group: WeaponGroup) -> list[Json]:
    weapons = set(group.weapons)
    carriers = []
    aircraft_types = {s.aircraft for s in view.coalition.air_wing.iter_squadrons()}
    for aircraft in sorted(aircraft_types, key=lambda a: a.variant_id):
        pylons = [
            pylon.number
            for pylon in Pylon.iter_pylons(aircraft)
            if pylon.allowed & weapons
        ]
        if pylons:
            carriers.append({"aircraft": aircraft.variant_id, "pylons": pylons})
    return carriers


@commander_tools.register(
    "lookup_weapon",
    "Data on a weapon from the game's weapon database: its group, type (ARM, LGB, "
    "TGP, ...), introduction year (with your faction's overrides), the fallback "
    "chain used when weapon date restriction replaces it, its CLSIDs, whether you "
    "may use it on the campaign date, and which of your squadrons' aircraft can "
    "carry it on which pylons.",
    LookupWeaponArgs,
    read_only=True,
)
def lookup_weapon(context: CommanderContext, args: LookupWeaponArgs) -> Json:
    view = context.view()
    group, weapon = _find_group(args.name)
    faction = view.coalition.faction
    overrides: dict[str, Any] = getattr(
        faction, "weapons_introduction_year_overrides", {}
    )
    chain = []
    fallback = group.fallback
    seen = {group.name}
    while fallback is not None and fallback.name not in seen:
        seen.add(fallback.name)
        chain.append(
            {
                "group": fallback.name,
                "year": overrides.get(fallback.name, fallback.introduction_year),
            }
        )
        fallback = fallback.fallback

    def weapon_name(w: Weapon) -> str:
        try:
            return w.name
        except KeyError:
            return w.clsid

    result: Json = {
        "group": group.name,
        "type": group.type.value,
        "introduced": overrides.get(group.name, group.introduction_year),
        "fallbacks": chain,
        "weapons": [
            {"name": weapon_name(w), "clsid": w.clsid} for w in group.weapons[:12]
        ],
    }
    if len(group.weapons) > 12:
        result["weapons_total"] = len(group.weapons)
    if weapon is not None:
        result["matched"] = {"name": weapon_name(weapon), "clsid": weapon.clsid}
        available = weapon.available_on(view.game.date, faction)
    else:
        available = bool(group.weapons) and group.weapons[0].available_on(
            view.game.date, faction
        )
    result["available_on_campaign_date"] = available
    result["restrict_weapons_by_date"] = view.game.settings.restrict_weapons_by_date
    result["carried_by_your_aircraft"] = _carriers(view, group)
    return result
