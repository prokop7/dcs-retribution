"""Procurement, front line stance, ground unit transfers and squadron basing."""

from __future__ import annotations

from pydantic import BaseModel, Field

from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.domains import Domain, require_domain
from game.claudecommander.mcp.tools import ToolError
from game.claudecommander.tools.airplan import flight_deleted
from game.claudecommander.tools.lookup import _find
from game.claudecommander.view import CommanderView, Json, PACKAGE_IDS
from game.dcs.groundunittype import GroundUnitType
from game.ground_forces.combat_stance import CombatStance
from game.purchaseadapter import (
    AircraftPurchaseAdapter,
    GroundUnitPurchaseAdapter,
    TransactionError,
)
from game.theater import ParkingType
from game.theater.controlpoint import ControlPoint
from game.transfers import TransferOrder


def _budget(view: CommanderView) -> float:
    return round(view.coalition.budget, 1)


# Front line stance --------------------------------------------------------------------


class StanceArgs(BaseModel):
    front_line_id: str
    stance: str = Field(
        description="DEFENSIVE, AGGRESSIVE, RETREAT, BREAKTHROUGH, ELIMINATION or "
        "AMBUSH."
    )


@commander_tools.register(
    "set_front_line_stance",
    "Sets the stance of your ground forces on a front line. DEFENSIVE: hold with "
    "medium groups. AGGRESSIVE: advance with medium groups. RETREAT: fall back. "
    "BREAKTHROUGH: rush forward with large armored groups. ELIMINATION: advance to "
    "destroy enemy forces. AMBUSH: defensive with ATGMs and infantry forward in "
    "smaller groups.",
    StanceArgs,
)
def set_front_line_stance(context: CommanderContext, args: StanceArgs) -> Json:
    require_domain(context.game, context.player, Domain.FRONT_LINE_STANCE)
    view = context.view()
    front = view.front_line(args.front_line_id)
    try:
        stance = CombatStance[args.stance.strip().upper()]
    except KeyError:
        raise ToolError(
            f"Unknown stance {args.stance!r}. Valid: "
            f"{', '.join(s.name for s in CombatStance)}."
        )
    own_cp = front.control_point_friendly_to(view.player)
    enemy_cp = front.control_point_hostile_to(view.player)
    previous = own_cp.stances.get(enemy_cp.id)
    own_cp.stances[enemy_cp.id] = stance
    context.events.update_front_line(front)
    return {
        "front_line_id": str(front.id),
        "front_line": front.name,
        "previous_stance": previous.name if previous is not None else None,
        "stance": stance.name,
        "deployable_units": own_cp.deployable_front_line_units,
    }


# Aircraft --------------------------------------------------------------------------------


class AircraftOrderArgs(BaseModel):
    squadron_id: str
    count: int = Field(ge=1, le=50)


def _aircraft_order_json(
    view: CommanderView, adapter: AircraftPurchaseAdapter, squadron_id: str
) -> Json:
    squadron = view.own_squadron(squadron_id)
    parking = ParkingType().from_squadron(squadron)
    return {
        "squadron_id": squadron_id,
        "squadron": squadron.name,
        "aircraft": squadron.aircraft.variant_id,
        "unit_price": squadron.aircraft.price,
        "owned": squadron.owned_aircraft,
        "on_order": squadron.pending_deliveries,
        "max_size": squadron.max_size,
        "free_parking_at_base": squadron.location.unclaimed_parking(parking),
        "budget_remaining": _budget(view),
    }


def _aircraft_purchase_problem(
    view: CommanderView, adapter: AircraftPurchaseAdapter, squadron_id: str
) -> str:
    squadron = view.own_squadron(squadron_id)
    if view.coalition.budget < squadron.aircraft.price:
        return (
            f"not enough budget ({_budget(view)}M left, {squadron.aircraft.price}M "
            "each)"
        )
    parking = ParkingType().from_squadron(squadron)
    if squadron.location.unclaimed_parking(parking) <= 0:
        return f"no free parking left at {squadron.location.name}"
    if not squadron.has_aircraft_capacity_for(1):
        return (
            f"the squadron is full ({squadron.owned_aircraft} owned + "
            f"{squadron.pending_deliveries} on order, max {squadron.max_size})"
        )
    return "the purchase is not allowed"


@commander_tools.register(
    "buy_aircraft",
    "Orders aircraft for one of your squadrons. They are delivered at the end of "
    "the turn. Checks budget, free parking at the squadron's base and the "
    "squadron's size limit; all or nothing. Returns the order and the remaining "
    "budget.",
    AircraftOrderArgs,
)
def buy_aircraft(context: CommanderContext, args: AircraftOrderArgs) -> Json:
    require_domain(context.game, context.player, Domain.AIRCRAFT_PURCHASE)
    view = context.view()
    squadron = view.own_squadron(args.squadron_id)
    adapter = AircraftPurchaseAdapter(squadron.location)
    bought = 0
    for _ in range(args.count):
        if not adapter.has_pending_sales(squadron) and not adapter.can_buy(squadron):
            problem = _aircraft_purchase_problem(view, adapter, args.squadron_id)
            if bought:
                adapter.sell(squadron, bought)
            raise ToolError(
                f"Cannot buy {args.count} {squadron.aircraft.variant_id} for "
                f"{squadron.name}: after {bought}, {problem}. Nothing was bought."
            )
        try:
            adapter.buy(squadron, 1)
        except TransactionError as ex:
            if bought:
                adapter.sell(squadron, bought)
            raise ToolError(f"{ex}. Nothing was bought.")
        bought += 1
    context.events.update_control_point(squadron.location)
    result = _aircraft_order_json(view, adapter, args.squadron_id)
    result["bought"] = bought
    return result


@commander_tools.register(
    "cancel_aircraft_order",
    "Cancels aircraft still on order for one of your squadrons and refunds them. "
    "Delivered aircraft cannot be sold.",
    AircraftOrderArgs,
)
def cancel_aircraft_order(context: CommanderContext, args: AircraftOrderArgs) -> Json:
    require_domain(context.game, context.player, Domain.AIRCRAFT_PURCHASE)
    view = context.view()
    squadron = view.own_squadron(args.squadron_id)
    if squadron.pending_deliveries < args.count:
        raise ToolError(
            f"{squadron.name} has {max(squadron.pending_deliveries, 0)} aircraft on "
            f"order; cannot cancel {args.count}."
        )
    adapter = AircraftPurchaseAdapter(squadron.location)
    adapter.sell(squadron, args.count)
    context.events.update_control_point(squadron.location)
    result = _aircraft_order_json(view, adapter, args.squadron_id)
    result["cancelled"] = args.count
    return result


# Ground units ----------------------------------------------------------------------------


class GroundOrderArgs(BaseModel):
    control_point_id: str
    unit_type: str = Field(
        description="Ground unit type from your faction (see get_own_forces kind "
        "catalogue: frontline_units, artillery_units; also logistics units)."
    )
    count: int = Field(ge=1, le=100)


def _ground_unit(view: CommanderView, name: str) -> GroundUnitType:
    units = list(dict.fromkeys(view.coalition.faction.ground_units))
    return _find("ground unit of your faction", name, units)


def _ground_json(view: CommanderView, cp: ControlPoint, unit: GroundUnitType) -> Json:
    return {
        "control_point": cp.name,
        "unit_type": unit.variant_id,
        "unit_price": unit.price,
        "at_base": cp.base.total_units_of_type(unit),
        "on_order": cp.ground_unit_orders.pending_orders(unit),
        "base_total_armor": cp.base.total_armor,
        "front_line_unit_capacity": cp.frontline_unit_count_limit,
        "budget_remaining": _budget(view),
    }


@commander_tools.register(
    "buy_ground_units",
    "Orders ground units at one of your bases. They arrive at the end of the turn "
    "(from a base with a factory, by road if needed). The base must be able to "
    "deploy ground units and be connected to a base that can recruit them. Checks "
    "budget; all or nothing. Units beyond the base's front line capacity stay in "
    "reserve.",
    GroundOrderArgs,
)
def buy_ground_units(context: CommanderContext, args: GroundOrderArgs) -> Json:
    require_domain(context.game, context.player, Domain.GROUND_UNIT_PURCHASE)
    view = context.view()
    cp = view.own_control_point(args.control_point_id)
    unit = _ground_unit(view, args.unit_type)
    if not cp.has_ground_unit_source(context.game):
        if not cp.can_deploy_ground_units:
            reason = f"{cp.name} cannot hold ground units"
        else:
            reason = (
                f"no base of yours that can recruit ground units (one with a "
                f"factory) supplies {cp.name}"
            )
        raise ToolError(f"Cannot order ground units at {cp.name}: {reason}.")
    cost = unit.price * args.count
    if cost > view.coalition.budget:
        raise ToolError(
            f"{args.count}x {unit.variant_id} cost {cost}M; you have {_budget(view)}M."
        )
    adapter = GroundUnitPurchaseAdapter(cp, view.coalition, context.game)
    adapter.buy(unit, args.count)
    context.events.update_control_point(cp)
    result = _ground_json(view, cp, unit)
    result["bought"] = args.count
    return result


@commander_tools.register(
    "cancel_ground_unit_order",
    "Cancels ground units still on order at one of your bases and refunds them.",
    GroundOrderArgs,
)
def cancel_ground_unit_order(context: CommanderContext, args: GroundOrderArgs) -> Json:
    require_domain(context.game, context.player, Domain.GROUND_UNIT_PURCHASE)
    view = context.view()
    cp = view.own_control_point(args.control_point_id)
    unit = _ground_unit(view, args.unit_type)
    pending = cp.ground_unit_orders.pending_orders(unit)
    if pending < args.count:
        raise ToolError(
            f"{cp.name} has {pending} {unit.variant_id} on order; cannot cancel "
            f"{args.count}."
        )
    GroundUnitPurchaseAdapter(cp, view.coalition, context.game).sell(unit, args.count)
    context.events.update_control_point(cp)
    result = _ground_json(view, cp, unit)
    result["cancelled"] = args.count
    return result


# Transfers -------------------------------------------------------------------------------


class TransferArgs(BaseModel):
    origin_id: str = Field(description="Your base the units leave from.")
    destination_id: str = Field(description="Your base the units go to.")
    units: dict[str, int] = Field(
        min_length=1, description="{unit type: count} from the origin's ground units."
    )
    airlift: bool = Field(
        default=False,
        description="Move the units by air (needs transport helicopters or "
        "aircraft able to fly the airlift) instead of by road or ship.",
    )


@commander_tools.register(
    "transfer_ground_units",
    "Moves ground units between two of your bases: by road convoy or cargo ship "
    "along your supply routes, or by airlift. Convoys and ships take turns and can "
    "be attacked on the way. Returns the transfer and the units left at the origin.",
    TransferArgs,
)
def transfer_ground_units(context: CommanderContext, args: TransferArgs) -> Json:
    require_domain(context.game, context.player, Domain.TRANSFERS)
    view = context.view()
    game = context.game
    origin = view.own_control_point(args.origin_id)
    destination = view.own_control_point(args.destination_id)
    if origin == destination:
        raise ToolError("Origin and destination are the same base.")
    if not destination.can_deploy_ground_units:
        raise ToolError(f"{destination.name} cannot hold ground units.")
    at_origin = {u.variant_id: n for u, n in origin.base.armor.items() if n}
    units: dict[GroundUnitType, int] = {}
    for name, count in args.units.items():
        if count <= 0:
            raise ToolError(f"Count for {name} must be positive.")
        candidates = [u for u, n in origin.base.armor.items() if n]
        if not candidates:
            raise ToolError(f"{origin.name} has no ground units.")
        unit = _find(f"ground unit at {origin.name}", name, candidates)
        available = origin.base.total_units_of_type(unit)
        if count > available:
            raise ToolError(
                f"{origin.name} has {available} {unit.variant_id}; cannot transfer "
                f"{count}. Units there: {at_origin}."
            )
        units[unit] = units.get(unit, 0) + count
    network = game.transit_network_for(view.player)
    if not network.has_path_between(origin, destination):
        raise ToolError(
            f"Your transit network (roads, shipping lanes and airlift links between "
            f"airfields) does not connect {origin.name} to {destination.name}."
        )
    transfer = TransferOrder(
        origin=origin,
        destination=destination,
        units=units,
        request_airflift=args.airlift,
    )
    view.coalition.transfers.new_transfer(transfer, game.conditions.start_time)
    context.events.update_control_point(origin)
    context.events.update_control_point(destination)
    result: Json = {
        "from": origin.name,
        "to": destination.name,
        "units": {u.variant_id: n for u, n in units.items()},
        "transport": transfer.description,
        "left_at_origin": {u.variant_id: n for u, n in origin.base.armor.items() if n},
    }
    if args.airlift and transfer.transport is None:
        result["warning"] = (
            "No airlift could be arranged yet (no available transport aircraft); "
            "the units wait at the origin."
        )
    return result


# Squadron relocation -------------------------------------------------------------------


class RelocateArgs(BaseModel):
    squadron_id: str
    destination_id: str = Field(
        description="Your base to move to. The squadron's current base cancels a "
        "planned relocation."
    )


@commander_tools.register(
    "relocate_squadron",
    "Moves one of your squadrons to another of your bases. Its untasked aircraft "
    "ferry there this turn (ferry flights are added to your ATO) and it operates "
    "from the new base from next turn. Checks that the base can operate the "
    "aircraft and has parking for the whole squadron. Giving the current base "
    "cancels a planned move.",
    RelocateArgs,
)
def relocate_squadron(context: CommanderContext, args: RelocateArgs) -> Json:
    require_domain(context.game, context.player, Domain.TRANSFERS)
    view = context.view()
    squadron = view.own_squadron(args.squadron_id)
    destination = view.own_control_point(args.destination_id)
    before = [
        f
        for p in view.coalition.ato.packages
        for f in p.flights
        if f.squadron == squadron
    ]
    try:
        if destination == squadron.location:
            if squadron.destination is None:
                raise ToolError(
                    f"{squadron.name} is already based at {destination.name}."
                )
            squadron.cancel_relocation()
        else:
            if not squadron.operates_from(destination):
                raise ToolError(
                    f"{squadron.aircraft.variant_id} squadron {squadron.name} cannot "
                    f"operate from {destination.name} ({destination.cptype.name})."
                )
            if destination == squadron.destination:
                raise ToolError(
                    f"{squadron.name} is already moving to {destination.name}."
                )
            squadron.plan_relocation(destination, context.game.conditions.start_time)
    except RuntimeError as ex:
        raise ToolError(str(ex))
    after = [
        f
        for p in view.coalition.ato.packages
        for f in p.flights
        if f.squadron == squadron
    ]
    for flight in before:
        if flight not in after:
            flight_deleted(context.events, flight)
    ferry: list[Json] = []
    ferrying = 0
    for flight in after:
        if flight not in before:
            context.events.new_flight(flight)
            ferrying += flight.count
            ferry.append(
                {
                    "flight_id": str(flight.id),
                    "package_id": PACKAGE_IDS.id_for(flight.package),
                    "count": flight.count,
                }
            )
    return {
        "squadron": squadron.name,
        "base": squadron.location.name,
        "relocating_to": (
            squadron.destination.name if squadron.destination is not None else None
        ),
        "ferry_flights": ferry,
        "aircraft_on_other_missions": squadron.owned_aircraft
        - squadron.untasked_aircraft
        - ferrying,
    }
