"""Possible mission targets for your side."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field

from game.ato.flighttype import FlightType
from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.tools.common import AreaFilter, Paging, parse_flight_type
from game.claudecommander.view import CommanderView, Json, page, position, within
from game.theater.controlpoint import ControlPoint
from game.theater.frontline import FrontLine
from game.theater.missiontarget import MissionTarget
from game.theater.theatergroundobject import TheaterGroundObject


class MissionTargetsArgs(AreaFilter, Paging):
    mission_type: Optional[str] = Field(
        default=None,
        description='Only targets that accept this mission type, e.g. "DEAD", '
        '"Strike", "BAI", "OCA/Runway", "Anti-ship", "CAS", "BARCAP".',
    )
    owner: Literal["enemy", "own", "any"] = Field(
        default="enemy", description="Whose targets to list."
    )


def _target_entry(view: CommanderView, target: MissionTarget) -> Json:
    if isinstance(target, ControlPoint):
        return {
            "id": str(target.id),
            "kind": "base",
            "name": target.name,
            "type": target.cptype.name,
            "owner": view.owner_label(target.captured),
            **position(target.position),
        }
    if isinstance(target, TheaterGroundObject):
        return {
            "id": str(target.id),
            "kind": "ground_object",
            "name": target.name,
            "category": target.category,
            "base": target.control_point.name,
            "owner": view.owner_label(target.control_point.captured),
            **position(target.position),
        }
    if isinstance(target, FrontLine):
        return {
            "id": str(target.id),
            "kind": "front_line",
            "name": target.name,
            "owner": "contested",
            **position(target.position),
        }
    raise TypeError(f"Unexpected mission target {target}")


def _owner_matches(view: CommanderView, target: MissionTarget, owner: str) -> bool:
    if owner == "any" or isinstance(target, FrontLine):
        return True
    if isinstance(target, ControlPoint):
        captured = target.captured
    elif isinstance(target, TheaterGroundObject):
        captured = target.control_point.captured
    else:
        return False
    return (captured is view.player) == (owner == "own")


def mission_targets(view: CommanderView, args: MissionTargetsArgs) -> Json:
    wanted: Optional[FlightType] = None
    if args.mission_type is not None:
        wanted = parse_flight_type(args.mission_type)
    center, radius = args.area(view)

    selected: list[tuple[MissionTarget, list[FlightType]]] = []
    for target in view.mission_target_objects():
        if not _owner_matches(view, target, args.owner):
            continue
        if not within(target.position, center, radius):
            continue
        missions = view.mission_types_for(target)
        if wanted is not None and wanted not in missions:
            continue
        selected.append((target, missions))

    common: list[FlightType] = []
    if selected:
        shared = set(selected[0][1])
        for _, missions in selected[1:]:
            shared &= set(missions)
        common = [m for m in selected[0][1] if m in shared]

    entries = []
    for target, missions in selected:
        entry = _target_entry(view, target)
        entry["missions"] = [m.value for m in missions if m not in common]
        entries.append(entry)
    sliced, meta = page(entries, args.offset, args.limit)
    result: Json = {
        "missions_valid_for_all_listed": [m.value for m in common],
        "targets": sliced,
    }
    if meta["total"] != len(sliced) or args.offset:
        result["targets_page"] = meta
    return result


@commander_tools.register(
    "get_mission_targets",
    "What your side can plan packages against, and which mission types each target "
    "accepts under the game's rules: enemy bases (OCA, air assault), neutral "
    "bases (air assault, plus escorts for it: they are undefended), enemy ground "
    "objects (Strike, DEAD/SEAD for air defenses, BAI for armor and motorpools, "
    "Anti-ship for ships), front lines (CAS), and your own bases/objects "
    "(BARCAP, AEW&C, refueling). Mission types valid for every listed target are "
    "given once in missions_valid_for_all_listed. Destroyed sites are omitted. Use "
    "the ids with planning tools. No priorities are given; details on each target "
    "are in get_enemy_intel / get_own_forces.",
    MissionTargetsArgs,
    read_only=True,
)
def get_mission_targets(context: CommanderContext, args: MissionTargetsArgs) -> Json:
    return mission_targets(context.view(), args)
