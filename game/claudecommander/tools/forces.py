"""Own forces, enemy intelligence and front lines."""

from __future__ import annotations

from typing import Any, Literal, Optional, Sequence

from dcs.mapping import Point
from pydantic import Field

from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.mcp.tools import NoArguments
from game.claudecommander.tools.common import AreaFilter, Paging
from game.claudecommander.view import CommanderView, Json, page, position, within
from game.theater.theatergroundobject import TheaterGroundObject

OwnKind = Literal[
    "overview",
    "bases",
    "squadrons",
    "ground_objects",
    "front_lines",
    "transfers",
    "catalogue",
]
EnemyKind = Literal[
    "overview", "bases", "ground_objects", "front_lines", "catalogue", "sightings"
]

_CATEGORIES_HELP = (
    "Only ground objects of these categories: aa (air defense), ewr, armor, "
    "missile, coastal, ship, CARRIER, LHA, factory, ammo, fuel, power, comms, "
    "commandcenter, oil, derrick, ware, village, allycamp, farp, fob, motorpool, "
    "ww2bunker."
)


class OwnForcesArgs(AreaFilter, Paging):
    kind: OwnKind = Field(
        default="overview",
        description="overview = bases, squadrons, front lines, transfers and a "
        "count of ground objects per base and category. Ask for ground_objects for "
        "the sites themselves.",
    )
    categories: Optional[list[str]] = Field(default=None, description=_CATEGORIES_HELP)


class EnemyIntelArgs(AreaFilter, Paging):
    kind: EnemyKind = Field(
        default="overview",
        description="overview = bases, front lines, sightings and a count of "
        "ground objects per base and category. Ask for ground_objects for the "
        "sites themselves.",
    )
    categories: Optional[list[str]] = Field(default=None, description=_CATEGORIES_HELP)


def _paged_section(result: Json, name: str, items: Sequence[Any], args: Paging) -> None:
    sliced, meta = page(items, args.offset, args.limit)
    result[name] = sliced
    if meta["total"] != len(sliced) or args.offset:
        result[f"{name}_page"] = meta


def _filter_tgos(
    tgos: list[TheaterGroundObject],
    categories: Optional[list[str]],
    center: Optional[Point],
    radius: Optional[float],
) -> list[TheaterGroundObject]:
    wanted = {c.lower() for c in categories} if categories else None
    selected = [
        tgo
        for tgo in tgos
        if (wanted is None or tgo.category.lower() in wanted)
        and within(tgo.position, center, radius)
    ]
    return sorted(selected, key=lambda t: (t.control_point.name, t.category, t.name))


def own_forces(view: CommanderView, args: OwnForcesArgs) -> Json:
    center, radius = args.area(view)
    kinds = (
        {"bases", "squadrons", "front_lines", "transfers", "summary"}
        if args.kind == "overview"
        else {args.kind}
    )
    result: Json = {"side": view.player.value}
    if "bases" in kinds:
        bases = [
            view.own_base(cp)
            for cp in view.own_control_points()
            if within(cp.position, center, radius)
        ]
        _paged_section(result, "bases", bases, args)
    if "squadrons" in kinds:
        squadrons = [
            view.own_squadron_json(s)
            for s in view.coalition.air_wing.iter_squadrons()
            if within(s.location.position, center, radius)
        ]
        _paged_section(result, "squadrons", squadrons, args)
        used = {s["aircraft"] for s in result["squadrons"]}
        result["aircraft_task_weights"] = {
            k: v for k, v in view.aircraft_task_weights().items() if k in used
        }
    tgos = _filter_tgos(view.own_ground_objects(), args.categories, center, radius)
    if "summary" in kinds:
        result["ground_objects_by_base"] = view.own_ground_object_summary(tgos)
        result["ground_objects_by_base_format"] = "{base: {category: [alive, sites]}}"
    if "ground_objects" in kinds:
        _paged_section(
            result,
            "ground_objects",
            [view.own_ground_object_json(t) for t in tgos],
            args,
        )
    if "front_lines" in kinds:
        result["front_lines"] = [
            view.front_line_json(f)
            for f in view.front_line_objects()
            if within(f.position, center, radius)
        ]
    if "transfers" in kinds:
        result["transfers"] = view.own_transfers()
    if "catalogue" in kinds:
        result["catalogue"] = view.faction_catalogue(view.coalition.faction)
    return result


def enemy_intel(view: CommanderView, args: EnemyIntelArgs) -> Json:
    center, radius = args.area(view)
    kinds = (
        {"bases", "front_lines", "sightings", "summary"}
        if args.kind == "overview"
        else {args.kind}
    )
    result: Json = {
        "side": view.enemy.value,
        "note": "Site layouts are at full strength. Threat and detection ranges are "
        "what each site can engage and see with now. Enemy aircraft, ground unit "
        "counts, orders, transfers and the enemy ATO are unknown.",
    }
    if "bases" in kinds:
        bases = [
            view.other_base(cp)
            for cp in view.other_control_points()
            if within(cp.position, center, radius)
        ]
        _paged_section(result, "bases", bases, args)
    tgos = _filter_tgos(view.enemy_ground_objects(), args.categories, center, radius)
    if "summary" in kinds:
        result["ground_objects_by_base"] = view.enemy_ground_object_summary(tgos)
        result["ground_objects_by_base_format"] = (
            "{base: {category: [not known destroyed, sites]}}"
        )
    if "ground_objects" in kinds:
        _paged_section(
            result,
            "ground_objects",
            [view.enemy_ground_object_json(t) for t in tgos],
            args,
        )
    if "front_lines" in kinds:
        result["front_lines"] = [
            {
                "id": str(front.id),
                "name": front.name,
                "enemy_base": front.control_point_hostile_to(view.player).name,
                **position(front.position),
            }
            for front in view.front_line_objects()
            if within(front.position, center, radius)
        ]
    if "sightings" in kinds:
        result["sightings"] = view.enemy_sightings()
    if "catalogue" in kinds:
        result["catalogue"] = view.faction_catalogue(view.enemy_coalition.faction)
    return result


@commander_tools.register(
    "get_own_forces",
    "Your forces in full detail. bases: type, position, runway, parking, aircraft "
    "and ground units present, ground unit orders, income, front line capacity. "
    "squadrons: aircraft type, base, aircraft owned/untasked, pilots, auto-assign "
    "tasks, plus the auto-planner task weights of each aircraft type. "
    "ground_objects: SAMs, EWRs, buildings, armor, ships with [alive, total] units "
    "per type, current threat/detection range and income. front_lines: position, "
    "stance and your units. transfers: ground unit transfers in progress. "
    "catalogue: what your faction can buy. Filter with categories or near + "
    "radius_nm; lists are paged with limit/offset (see *_page.next_offset).",
    OwnForcesArgs,
    read_only=True,
)
def get_own_forces(context: CommanderContext, args: OwnForcesArgs) -> Json:
    return own_forces(context.view(), args)


@commander_tools.register(
    "get_enemy_intel",
    "What your side knows about the enemy (and neutral bases). bases: type, "
    "position, owner, runway damage, parking capacity. ground_objects: every enemy "
    "site with category, unit types at full strength, current threat/detection "
    "range (air_defense says when radars or launchers are down, with the "
    "full-strength range) and income value of buildings; sites with no survivors "
    "show as destroyed, other partial damage is unknown. front_lines: positions. catalogue: the "
    "enemy faction's aircraft and units. sightings: enemy aircraft you have "
    "encountered. Enemy aircraft per base, ground unit counts, orders and the "
    "enemy ATO are unknown. Same filters and paging as get_own_forces.",
    EnemyIntelArgs,
    read_only=True,
)
def get_enemy_intel(context: CommanderContext, args: EnemyIntelArgs) -> Json:
    return enemy_intel(context.view(), args)


@commander_tools.register(
    "get_front_lines",
    "Every front line: id, position, the bases on each side, distance from each, "
    "the heading your forces advance on, your stance, your units at the base and "
    "how many can deploy. Enemy strength on the front is unknown.",
    read_only=True,
)
def get_front_lines(context: CommanderContext, _args: NoArguments) -> Json:
    return {"front_lines": context.view().front_lines()}
