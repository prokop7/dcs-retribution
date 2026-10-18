"""query_combat_log: drill into the shots, intercepts and kills of past missions."""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal, Optional

from dcs.mapping import Point
from pydantic import Field

from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.mcp.tools import ToolError
from game.claudecommander.tools.common import AreaFilter, Paging
from game.claudecommander.view import Json

GroupField = Literal[
    "event",
    "outcome",
    "weapon",
    "shooter",
    "shooter_side",
    "target",
    "package",
    "intercepted_by",
    "victim",
    "killer",
]


class CombatLogArgs(AreaFilter, Paging):
    turn: Optional[int] = Field(
        default=None,
        description="Turn whose mission to query (default: the latest kept).",
    )
    event: Optional[Literal["shot", "kill"]] = Field(
        default=None, description="Only shots or only kills."
    )
    side: Optional[Literal["own", "enemy"]] = Field(
        default=None,
        description="Shots fired by this side (shooter_side), or kills scored by it "
        "(killer_side).",
    )
    outcome: Optional[Literal["hit", "intercepted", "no_hit", "unresolved"]] = None
    package: Optional[str] = Field(
        default=None, description="Text the package label must contain."
    )
    weapon: Optional[str] = Field(
        default=None, description="Text the weapon name must contain."
    )
    shooter: Optional[str] = Field(
        default=None, description="Text the shooter or killer must contain."
    )
    target: Optional[str] = Field(
        default=None, description="Text the target or victim must contain."
    )
    from_min: Optional[float] = Field(default=None, description="Mission minute from.")
    to_min: Optional[float] = Field(default=None, description="Mission minute to.")
    group_by: list[GroupField] = Field(
        default_factory=list,
        description="Return counts per combination of these fields instead of "
        'records, e.g. ["weapon", "outcome"].',
    )


def _contains(value: Any, needle: Optional[str]) -> bool:
    return needle is None or needle.lower() in str(value or "").lower()


def _matches(record: Json, args: CombatLogArgs) -> bool:
    if args.event is not None and record.get("event") != args.event:
        return False
    if args.side is not None:
        side = record.get("shooter_side", record.get("killer_side"))
        if side != args.side:
            return False
    if args.outcome is not None and record.get("outcome") != args.outcome:
        return False
    if not _contains(record.get("package"), args.package):
        return False
    if not _contains(record.get("weapon"), args.weapon):
        return False
    if not _contains(record.get("shooter", record.get("killer")), args.shooter):
        return False
    if not _contains(record.get("target", record.get("victim")), args.target):
        return False
    minute = record.get("t_min")
    if args.from_min is not None and (minute is None or minute < args.from_min):
        return False
    if args.to_min is not None and (minute is None or minute > args.to_min):
        return False
    return True


@commander_tools.register(
    "query_combat_log",
    "Drills into the combat log of your recent missions (the last three are kept): "
    "every shot your aircraft and air defenses fired with its outcome (hit, "
    "intercepted and by what, no hit, unresolved), enemy launches at your "
    "aircraft or weapons (by launcher type), and kills you suffered or scored, "
    "with mission minute, launch range and where. Filter by event, side, outcome, "
    "package, weapon, shooter, target, time window and area (near + radius_nm), "
    "page with limit/offset, or set group_by for counts: group_by "
    '["weapon", "outcome"] with package "BELUGA" shows how each weapon fared '
    "against that target. Intercepts are inferred and approximate.",
    CombatLogArgs,
    read_only=True,
)
def query_combat_log(context: CommanderContext, args: CombatLogArgs) -> Json:
    view = context.view()
    logs = context.game.coalition_for(context.player).commander.combat_logs
    if not logs:
        return {
            "available_turns": [],
            "note": "No combat log yet: no mission with recorded combat has been "
            "flown since combat logging was added.",
        }
    turn = args.turn if args.turn is not None else max(logs)
    if turn not in logs:
        raise ToolError(f"No combat log for turn {turn}. Available: {sorted(logs)}.")
    records = [r for r in logs[turn] if _matches(r, args)]
    center, radius = args.area(view)
    if center is not None and radius is not None:
        terrain = context.game.theater.terrain
        records = [
            r
            for r in records
            if "x" in r
            and center.distance_to_point(Point(r["x"], r["y"], terrain))
            <= radius * 1852
        ]
    result: Json = {
        "turn": turn,
        "available_turns": sorted(logs),
        "matched": len(records),
    }
    end = args.offset + args.limit
    if args.group_by:
        counts: Counter[tuple[str, ...]] = Counter(
            tuple(str(r.get(f, "-")) for f in args.group_by) for r in records
        )
        groups = [
            {**dict(zip(args.group_by, key)), "count": count}
            for key, count in counts.most_common()
        ]
        result["groups"] = groups[args.offset : end]
        if len(groups) > end:
            result["groups_omitted"] = len(groups) - end
        return result
    result["records"] = records[args.offset : end]
    if len(records) > end:
        result["records_omitted"] = len(records) - end
    return result
