"""Buying, rebuilding and repairing sites, buildings and runways.

These mirror the ground object menu (buy/rebuild a site from a force group
layout, repair dead units), the building info panel (repair buildings) and the
base menu (repair the runway), including their prices and repair times.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Type

from dcs.unittype import UnitType as DcsUnitType
from pydantic import BaseModel, Field

from game.armedforces.forcegroup import ForceGroup
from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.domains import Domain, require_domain
from game.claudecommander.mcp.tools import ToolError
from game.claudecommander.tools.airplan import list_text
from game.claudecommander.view import CommanderView, Json
from game.config import RUNWAY_REPAIR_COST
from game.data.groups import GroupRole, GroupTask
from game.layout.layout import LayoutException, TgoLayout, TgoLayoutUnitGroup
from game.theater.theatergroundobject import (
    BuildingGroundObject,
    CoastalSiteGroundObject,
    EwrGroundObject,
    MissileSiteGroundObject,
    SamGroundObject,
    ShipGroundObject,
    TheaterGroundObject,
    VehicleGroupGroundObject,
)


def _budget(view: CommanderView) -> float:
    return round(view.coalition.budget, 1)


def _site_changed(
    context: CommanderContext, tgo: TheaterGroundObject, what: str
) -> None:
    events = context.events
    events.update_tgo(tgo)
    context.game.theater.iads_network.update_tgo(tgo, events)
    # Static intelligence: the other side learns that the site changed. Deferred
    # to its next run so the two commanders can't keep re-triggering each other.
    context.game.notify_commander(
        context.player.opponent, f"Enemy site {tgo.name} changed: {what}.", defer=True
    )


# Buying and rebuilding --------------------------------------------------------------


def _tasks_for(tgo: TheaterGroundObject) -> list[GroupTask]:
    """The force group tasks the ground object buy menu offers for a site."""
    if isinstance(tgo, SamGroundObject):
        return list(GroupRole.AIR_DEFENSE.tasks)
    if isinstance(tgo, VehicleGroupGroundObject):
        return list(GroupRole.GROUND_FORCE.tasks)
    if isinstance(tgo, EwrGroundObject):
        return [GroupTask.EARLY_WARNING_RADAR]
    if isinstance(tgo, ShipGroundObject):
        return [GroupTask.NAVY]
    if isinstance(tgo, MissileSiteGroundObject):
        return [GroupTask.MISSILE]
    if isinstance(tgo, CoastalSiteGroundObject):
        return [GroupTask.COASTAL]
    raise ToolError(f"{tgo.name} ({tgo.category}) cannot be bought or rebuilt.")


def _purchasable_site(view: CommanderView, tgo_id: str) -> TheaterGroundObject:
    tgo = view.own_ground_object(tgo_id)
    if not tgo.purchasable:
        if tgo.category in ("ship", "missile", "coastal"):
            raise ToolError(
                f"{tgo.name} ({tgo.category}) can only be bought on turn 0."
            )
        raise ToolError(f"{tgo.name} ({tgo.category}) cannot be bought or rebuilt.")
    _tasks_for(tgo)
    return tgo


def _force_groups(view: CommanderView, tgo: TheaterGroundObject) -> list[ForceGroup]:
    return view.coalition.armed_forces.groups_for_tasks(_tasks_for(tgo))


@dataclass
class _UnitOption:
    dcs_type: Type[DcsUnitType]
    label: str
    price: int


def _unit_options(
    force_group: ForceGroup, group: TgoLayoutUnitGroup
) -> list[_UnitOption]:
    options = [
        _UnitOption(u.dcs_unit_type, u.variant_id, u.price)
        for u in force_group.unit_types_for_group(group)
    ]
    options.extend(
        _UnitOption(s, f"{s.id} (static)", 0)
        for s in force_group.statics_for_group(group)
    )
    return options


def _default_count(group: TgoLayoutUnitGroup) -> int:
    if group.unit_count:
        return max(group.unit_count)
    return group.max_size


def _pending_repair_value(tgo: TheaterGroundObject) -> int:
    total = 0
    for unit in tgo.units:
        if unit.alive or unit.repair_turns_remaining is None:
            continue
        if unit.unit_type is not None:
            total += unit.unit_type.price
    return total


def _refund_value(tgo: TheaterGroundObject) -> int:
    return tgo.value + _pending_repair_value(tgo)


def _layout_json(force_group: ForceGroup, layout: TgoLayout) -> Json:
    groups = []
    for layout_group in layout.groups:
        unit_groups = []
        for unit_group in layout_group.unit_groups:
            options = _unit_options(force_group, unit_group)
            if not options:
                continue
            entry: Json = {
                "name": unit_group.name,
                "units": [f"{o.label} ${o.price}M" for o in options],
                "default_count": _default_count(unit_group),
                "max_count": unit_group.max_size,
            }
            if unit_group.optional:
                entry["optional"] = True
            unit_groups.append(entry)
        groups.append({"group": layout_group.group_name, "unit_groups": unit_groups})
    return {"layout": layout.name, "groups": groups}


class SiteOptionsArgs(BaseModel):
    tgo_id: str
    force_group: Optional[str] = Field(
        default=None, description="Only this force group, with full layout details."
    )


@commander_tools.register(
    "get_site_options",
    "What you can buy at one of your own sites (SAM, EWR or armor site; ships, "
    "missile and coastal sites only on turn 0): the force groups of your faction "
    "and their layouts, each layout's groups and unit groups with the unit types "
    "(price each), default and maximum count, and the refund you get for what is "
    "there now (current units plus repairs in progress).",
    SiteOptionsArgs,
    read_only=True,
)
def get_site_options(context: CommanderContext, args: SiteOptionsArgs) -> Json:
    view = context.view()
    tgo = _purchasable_site(view, args.tgo_id)
    groups = _force_groups(view, tgo)
    if args.force_group is not None:
        groups = [_force_group(groups, args.force_group)]
    detailed = args.force_group is not None or len(groups) <= 3
    result: Json = {
        "site": tgo.name,
        "category": tgo.category,
        "refund_for_current_units": _refund_value(tgo),
        "free_on_turn_0": context.game.turn == 0,
        "activation_turns": context.game.settings.purchased_ground_object_activation_turns,
        "budget": _budget(view),
    }
    if detailed:
        result["force_groups"] = [
            {
                "force_group": g.name,
                "layouts": [_layout_json(g, layout) for layout in g.layouts],
            }
            for g in groups
        ]
    else:
        result["force_groups"] = [
            {"force_group": g.name, "layouts": [layout.name for layout in g.layouts]}
            for g in groups
        ]
        result["note"] = "Give force_group for the layouts' units and prices."
    return result


def _force_group(groups: list[ForceGroup], name: str) -> ForceGroup:
    for group in groups:
        if group.name.lower() == name.strip().lower():
            return group
    raise ToolError(
        f"No force group {name!r} for this site. Available: "
        f"{list_text(g.name for g in groups)}."
    )


class UnitGroupChoice(BaseModel):
    name: str = Field(description="Unit group name from get_site_options.")
    unit_type: Optional[str] = Field(
        default=None, description="One of the unit group's unit types (default: first)."
    )
    count: Optional[int] = Field(default=None, ge=1)
    enabled: bool = Field(default=True, description="false skips an optional group.")


class BuySiteArgs(BaseModel):
    tgo_id: str
    force_group: str
    layout: Optional[str] = Field(
        default=None, description="Layout name (default: the force group's first)."
    )
    unit_groups: list[UnitGroupChoice] = Field(
        default_factory=list,
        description="Choices for unit groups; unit groups not listed use their "
        "first unit type and default count.",
    )


@dataclass
class _Purchase:
    unit_group: TgoLayoutUnitGroup
    group_name: str
    option: _UnitOption
    count: int

    @property
    def price(self) -> int:
        return self.option.price * self.count


def _plan_purchase(
    force_group: ForceGroup, layout: TgoLayout, choices: list[UnitGroupChoice]
) -> list[_Purchase]:
    by_name = {c.name.lower(): c for c in choices}
    used: set[str] = set()
    purchases = []
    for layout_group in layout.groups:
        for unit_group in layout_group.unit_groups:
            try:
                options = _unit_options(force_group, unit_group)
            except LayoutException:
                options = []
            if not options:
                continue
            choice = by_name.get(unit_group.name.lower())
            if choice is not None:
                used.add(unit_group.name.lower())
                if not choice.enabled:
                    if not unit_group.optional:
                        raise ToolError(
                            f"Unit group {unit_group.name} is required and cannot "
                            "be disabled."
                        )
                    continue
            option = options[0]
            if choice is not None and choice.unit_type is not None:
                wanted = choice.unit_type.strip().lower()
                matches = [
                    o
                    for o in options
                    if o.label.lower() == wanted or o.dcs_type.id.lower() == wanted
                ]
                if not matches:
                    raise ToolError(
                        f"Unit group {unit_group.name} cannot use "
                        f"{choice.unit_type!r}. Options: "
                        f"{list_text(o.label for o in options)}."
                    )
                option = matches[0]
            count = _default_count(unit_group)
            if choice is not None and choice.count is not None:
                count = choice.count
            if count > unit_group.max_size:
                raise ToolError(
                    f"Unit group {unit_group.name} has room for at most "
                    f"{unit_group.max_size} units."
                )
            purchases.append(
                _Purchase(unit_group, layout_group.group_name, option, count)
            )
    unknown = [c.name for c in choices if c.name.lower() not in used]
    if unknown:
        raise ToolError(
            f"Unknown unit groups for layout {layout.name}: {', '.join(unknown)}."
        )
    if not purchases:
        raise ToolError(f"Layout {layout.name} has no units your faction can field.")
    return purchases


@commander_tools.register(
    "buy_or_rebuild_site",
    "Replaces the units of one of your sites with a new group bought from a force "
    "group layout, exactly like the site's buy menu: the units there now (and "
    "repairs in progress) are refunded, the new units are paid for (free on turn "
    "0), and units bought after turn 0 only come into service after the "
    "configured activation delay. Use get_site_options first. Returns the cost "
    "and the remaining budget.",
    BuySiteArgs,
)
def buy_or_rebuild_site(context: CommanderContext, args: BuySiteArgs) -> Json:
    require_domain(context.game, context.player, Domain.SITE_REPAIR)
    view = context.view()
    game = context.game
    tgo = _purchasable_site(view, args.tgo_id)
    force_group = _force_group(_force_groups(view, tgo), args.force_group)
    layouts = list(force_group.layouts)
    if args.layout is None:
        layout = layouts[0]
    else:
        matching = [l for l in layouts if l.name.lower() == args.layout.strip().lower()]
        if not matching:
            raise ToolError(
                f"Force group {force_group.name} has no layout {args.layout!r}. "
                f"Layouts: {list_text(l.name for l in layouts)}."
            )
        layout = matching[0]
    purchases = _plan_purchase(force_group, layout, args.unit_groups)
    price = sum(p.price for p in purchases)
    refund = _refund_value(tgo)
    cost = price - refund
    if game.turn and cost > view.coalition.budget:
        raise ToolError(
            f"The new group costs {price}M minus a refund of {refund}M = {cost}M; you "
            f"have {_budget(view)}M."
        )

    tgo.heading = game.theater.heading_to_conflict_from(tgo.position) or tgo.heading
    if game.turn:
        view.coalition.budget -= cost
    tgo.groups = []
    for purchase in purchases:
        force_group.create_theater_group_for_tgo(
            tgo,
            purchase.unit_group,
            f"{tgo.name} ({purchase.group_name})",
            game,
            purchase.option.dcs_type,
            purchase.count,
        )
    tgo.required_unit_classes = ForceGroup.required_unit_classes_for_layout(layout)
    activation_turns = game.settings.purchased_ground_object_activation_turns
    delayed = bool(game.turn and activation_turns > 0)
    if delayed:
        for unit in tgo.units:
            if not unit.repairable:
                continue
            unit.alive = False
            unit.repair_turns_remaining = activation_turns
        tgo.invalidate_threat_poly()
    _site_changed(context, tgo, "units were replaced")
    result: Json = {
        "site": tgo.name,
        "force_group": force_group.name,
        "layout": layout.name,
        "units": [
            {"group": p.unit_group.name, "unit_type": p.option.label, "count": p.count}
            for p in purchases
        ],
        "price": price,
        "refund": refund,
        "cost": cost if game.turn else 0,
        "budget_remaining": _budget(view),
    }
    if delayed:
        result["in_service_in_turns"] = activation_turns
    return result


# Repairs -----------------------------------------------------------------------------


class RepairSiteArgs(BaseModel):
    tgo_id: str
    unit_ids: Optional[list[str]] = Field(
        default=None,
        description="Names of dead units to repair (see dead_units in "
        "get_own_forces ground_objects). Default: every dead unit not already being "
        "repaired.",
    )


@commander_tools.register(
    "repair_site",
    "Pays to repair dead units of one of your sites (SAMs, EWRs, armor, ships); "
    "each costs its unit price and returns to service after the configured repair "
    "time. All or nothing. Returns the cost, the repair time and the remaining "
    "budget.",
    RepairSiteArgs,
)
def repair_site(context: CommanderContext, args: RepairSiteArgs) -> Json:
    require_domain(context.game, context.player, Domain.SITE_REPAIR)
    view = context.view()
    tgo = view.own_ground_object(args.tgo_id)
    if isinstance(tgo, BuildingGroundObject):
        raise ToolError(f"{tgo.name} is a building site; use repair_building.")
    candidates = [
        u
        for u in tgo.units
        if not u.alive and u.repairable and u.repair_turns_remaining is None
    ]
    if args.unit_ids is not None:
        by_name = {u.name: u for u in candidates}
        all_names = {u.name: u for u in tgo.units}
        selected = []
        for name in args.unit_ids:
            if name in by_name:
                selected.append(by_name[name])
            elif name in all_names:
                unit = all_names[name]
                state = (
                    "alive"
                    if unit.alive
                    else (
                        "already being repaired"
                        if unit.repair_turns_remaining is not None
                        else "not repairable"
                    )
                )
                raise ToolError(f"Unit {name} of {tgo.name} is {state}.")
            else:
                raise ToolError(
                    f"{tgo.name} has no unit {name!r}. Repairable dead units: "
                    f"{list_text(by_name) or 'none'}."
                )
        candidates = selected
    if not candidates:
        raise ToolError(f"{tgo.name} has no dead units waiting for repair.")
    cost = sum(u.unit_type.price for u in candidates if u.unit_type is not None)
    if cost > view.coalition.budget:
        raise ToolError(
            f"Repairing {len(candidates)} units costs {cost}M; you have "
            f"{_budget(view)}M."
        )
    turns = context.game.settings.ground_object_repair_turns
    view.coalition.budget -= cost
    for unit in candidates:
        unit.begin_repair(turns, context.events)
    _site_changed(context, tgo, "units are being repaired")
    return {
        "site": tgo.name,
        "repaired_units": [u.name for u in candidates],
        "cost": cost,
        "in_service_in_turns": turns,
        "budget_remaining": _budget(view),
    }


class TgoArgs(BaseModel):
    tgo_id: str


@commander_tools.register(
    "repair_building",
    "Pays to repair the destroyed buildings of one of your building sites "
    "(factories, ammo depots, power, comms, fuel, ...). The price per building "
    "depends on the site's income and type; buildings return after the configured "
    "repair time. Returns the cost and the remaining budget.",
    TgoArgs,
)
def repair_building(context: CommanderContext, args: TgoArgs) -> Json:
    require_domain(context.game, context.player, Domain.BUILDING_REPAIR)
    view = context.view()
    tgo = view.own_ground_object(args.tgo_id)
    if not isinstance(tgo, BuildingGroundObject):
        raise ToolError(f"{tgo.name} ({tgo.category}) is not a building site.")
    dead = [u for u in tgo.statics if not u.alive and u.repair_turns_remaining is None]
    if not dead:
        raise ToolError(f"{tgo.name} has no destroyed buildings waiting for repair.")
    price = tgo.repair_cost()
    if price <= 0:
        raise ToolError(f"Buildings of category {tgo.category} cannot be repaired.")
    cost = price * len(dead)
    if cost > view.coalition.budget:
        raise ToolError(
            f"Repairing {len(dead)} buildings costs {cost:g}M ({price:g}M each); you "
            f"have {_budget(view)}M."
        )
    turns = context.game.settings.building_repair_turns
    view.coalition.budget -= cost
    for unit in dead:
        unit.begin_repair(turns, context.events)
    _site_changed(context, tgo, "buildings are being repaired")
    return {
        "site": tgo.name,
        "buildings": len(dead),
        "cost": round(cost, 1),
        "in_service_in_turns": turns,
        "budget_remaining": _budget(view),
    }


class ControlPointArgs(BaseModel):
    control_point_id: str


@commander_tools.register(
    "repair_runway",
    f"Pays {RUNWAY_REPAIR_COST}M to start repairing the damaged runway of one of "
    "your airfields; it reopens after 4 turns.",
    ControlPointArgs,
)
def repair_runway(context: CommanderContext, args: ControlPointArgs) -> Json:
    require_domain(context.game, context.player, Domain.RUNWAY_REPAIR)
    view = context.view()
    cp = view.own_control_point(args.control_point_id)
    if not cp.runway_can_be_repaired:
        if cp.runway_is_destroyable and cp.runway_status.repair_turns_remaining:
            raise ToolError(
                f"The runway at {cp.name} is already being repaired "
                f"({cp.runway_status.repair_turns_remaining} turns left)."
            )
        raise ToolError(f"The runway at {cp.name} is not damaged.")
    if view.coalition.budget < RUNWAY_REPAIR_COST:
        raise ToolError(
            f"Runway repair costs {RUNWAY_REPAIR_COST}M; you have {_budget(view)}M."
        )
    cp.begin_runway_repair()
    view.coalition.budget -= RUNWAY_REPAIR_COST
    context.events.update_control_point(cp)
    return {
        "base": cp.name,
        "cost": RUNWAY_REPAIR_COST,
        "repair_turns_remaining": cp.runway_status.repair_turns_remaining,
        "budget_remaining": _budget(view),
    }
