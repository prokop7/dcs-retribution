"""Your side's air tasking order."""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.tools.common import Paging
from game.claudecommander.view import CommanderView, Json, page


class AtoArgs(Paging):
    package_id: Optional[str] = Field(
        default=None, description="Only this package (waypoints included)."
    )
    include_waypoints: Optional[bool] = Field(
        default=None,
        description="Include every waypoint of each flight. Defaults to true when "
        "package_id is given, else false.",
    )


def ato(view: CommanderView, args: AtoArgs) -> Json:
    include_waypoints = args.include_waypoints
    if args.package_id is not None:
        package = view.own_package(args.package_id)
        if include_waypoints is None:
            include_waypoints = True
        return {"packages": [view.package_json(package, include_waypoints)]}
    packages = view.coalition.ato.packages
    sliced, meta = page(packages, args.offset, args.limit)
    result: Json = {
        "mission_start": view.game.conditions.start_time.strftime("%H:%M:%S"),
        "packages": [view.package_json(p, bool(include_waypoints)) for p in sliced],
    }
    if meta["total"] != len(sliced) or args.offset:
        result["packages_page"] = meta
    return result


@commander_tools.register(
    "get_ato",
    "Your side's air tasking order for this turn: packages (id, target, primary "
    "task, time over target) and their flights (id, task, aircraft, count, "
    "squadron, departure base, start type, loadout, takeoff and TOT times). Times "
    "are mission-local HH:MM:SS. Waypoints (type, x/y meters, altitude ft, TOT) "
    "are included for a single package_id or with include_waypoints. The enemy "
    "ATO is never visible.",
    AtoArgs,
    read_only=True,
)
def get_ato(context: CommanderContext, args: AtoArgs) -> Json:
    return ato(context.view(), args)
