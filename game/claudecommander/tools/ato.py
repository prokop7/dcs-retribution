"""Your side's air tasking order."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from pydantic import Field

from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.tools.common import Paging
from game.claudecommander.view import CommanderView, Json, PACKAGE_IDS, page

if TYPE_CHECKING:
    from game.coalition import Coalition


def locked_package_descriptions(coalition: Coalition) -> list[str]:
    """One line per package the player made or edited, for the run brief.

    For example: "pkg-1a2b3c4d: Strike against Damascus Factory, TOT 08:30:00,
    flights: 2x F-16CM (Strike), 2x F-15C (Escort)".
    """
    lines = []
    for package in coalition.ato.packages:
        if not package.player_locked:
            continue
        flights: Counter[str] = Counter()
        for flight in package.flights:
            flights[
                f"{flight.unit_type.variant_id} ({flight.flight_type.value})"
            ] += flight.count
        tot = package.time_over_target
        tot_text = (
            "no TOT" if tot == datetime.min else f"TOT {tot.strftime('%H:%M:%S')}"
        )
        flight_text = ", ".join(f"{n}x {f}" for f, n in flights.items()) or "none"
        lines.append(
            f"{PACKAGE_IDS.id_for(package)}: {package.package_description} against "
            f"{package.target.name}, {tot_text}, flights: {flight_text}"
        )
    return lines


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
    "are included for a single package_id or with include_waypoints. Packages "
    "marked locked were made or edited by the player and cannot be changed. The "
    "enemy ATO is never visible.",
    AtoArgs,
    read_only=True,
)
def get_ato(context: CommanderContext, args: AtoArgs) -> Json:
    return ato(context.view(), args)
