"""Moving carriers, LHAs and naval groups."""

from __future__ import annotations

from typing import Optional, Union

from dcs.mapping import Point
from pydantic import BaseModel, Field

from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.domains import Domain, require_domain
from game.claudecommander.mcp.tools import NoArguments, ToolError
from game.claudecommander.tools.common import PointRef
from game.claudecommander.view import (
    CommanderView,
    Json,
    UnknownIdError,
    bearing_deg,
    position,
    to_nm,
)
from game.theater.controlpoint import Carrier, ControlPoint
from game.theater.theatergroundobject import ShipGroundObject

Mover = Union[ControlPoint, ShipGroundObject]


def _own_mover(view: CommanderView, unit_id: str) -> Mover:
    for cp in view.own_control_points():
        if str(cp.id) == unit_id:
            if not cp.moveable:
                raise ToolError(f"{cp.name} is not a ship and cannot move.")
            return cp
    for tgo in view.own_ground_objects():
        if str(tgo.id) == unit_id:
            if not isinstance(tgo, ShipGroundObject):
                raise ToolError(f"{tgo.name} is not a naval group and cannot move.")
            return tgo
    raise UnknownIdError(f"No carrier, LHA or naval group of yours with id {unit_id}")


def own_movers(view: CommanderView) -> list[Mover]:
    movers: list[Mover] = [cp for cp in view.own_control_points() if cp.moveable]
    movers.extend(
        tgo for tgo in view.own_ground_objects() if isinstance(tgo, ShipGroundObject)
    )
    return movers


def mover_json(view: CommanderView, mover: Mover) -> Json:
    result: Json = {"id": str(mover.id), "name": mover.name}
    if isinstance(mover, ControlPoint):
        result["kind"] = "carrier" if isinstance(mover, Carrier) else "lha"
        try:
            result["operational"] = mover.runway_is_operational()
        except RuntimeError:  # its carrier group is gone
            result["operational"] = False
        result["aircraft"] = sum(s.owned_aircraft for s in mover.squadrons)
    else:
        result["kind"] = "naval_group"
        result["base"] = mover.control_point.name
        result["alive"] = mover.alive_unit_count
        threat = mover.max_threat_range().meters
        if threat:
            result["threat_nm"] = to_nm(threat)
    result.update(position(mover.position))
    result["max_move_nm"] = round(mover.max_move_distance.nautical_miles)
    if mover.target_position is not None:
        result["moving_to"] = position(mover.target_position)
    return result


def _check_destination(view: CommanderView, mover: Mover, point: Point) -> None:
    distance = mover.position.distance_to_point(point)
    if not mover.destination_in_range(point):
        raise ToolError(
            f"{mover.name} can move at most "
            f"{round(mover.max_move_distance.nautical_miles)} nm per turn; that "
            f"destination is {to_nm(distance)} nm away."
        )
    theater = view.game.theater
    if theater.landmap is None:
        return
    if not theater.is_in_sea(point):
        raise ToolError("That destination is not open sea.")
    if theater.landmap.land_inbetween(mover.position, point):
        raise ToolError(
            f"Land lies between {mover.name} and that destination; ships move in a "
            "straight line, so pick an intermediate point this turn."
        )


class MoveFleetArgs(BaseModel):
    unit_id: str = Field(
        description="Id of one of your carriers or LHAs (a base id) or naval groups "
        "(a ground object id), see get_fleet."
    )
    destination: Optional[PointRef] = Field(
        default=None,
        description="Where to sail. Omit to cancel a planned move.",
    )


@commander_tools.register(
    "get_fleet",
    "Your carriers, LHAs and naval groups: position, how far each can move per "
    "turn, a move already ordered (moving_to), aircraft aboard (carriers and LHAs) "
    "and threat range (naval groups).",
    NoArguments,
    read_only=True,
)
def get_fleet(context: CommanderContext, _args: NoArguments) -> Json:
    view = context.view()
    return {"fleet": [mover_json(view, m) for m in own_movers(view)]}


@commander_tools.register(
    "move_fleet",
    "Orders one of your carriers, LHAs or naval groups to sail to a point in open "
    "sea, up to its max_move_nm away in a straight line over water. This turn's "
    "mission is flown from the current position: naval groups get under way "
    "toward the destination during the mission, carriers and LHAs stay on "
    "station. Every ship is at its destination from next turn. Omit destination "
    "to cancel a planned move.",
    MoveFleetArgs,
)
def move_fleet(context: CommanderContext, args: MoveFleetArgs) -> Json:
    require_domain(context.game, context.player, Domain.FLEET_MOVEMENT)
    view = context.view()
    mover = _own_mover(view, args.unit_id)
    if args.destination is None:
        mover.target_position = None
    else:
        point, _ = args.destination.resolve(view)
        _check_destination(view, mover, point)
        mover.target_position = point
    if isinstance(mover, ControlPoint):
        context.events.update_control_point(mover)
    else:
        context.events.update_tgo(mover)
    result = mover_json(view, mover)
    if mover.target_position is not None:
        result["distance_nm"] = to_nm(
            mover.position.distance_to_point(mover.target_position)
        )
        result["bearing_deg"] = bearing_deg(mover.position, mover.target_position)
    return result
