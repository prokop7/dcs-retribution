"""Creating and changing packages and flights."""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

from game.ato.flight import Flight
from game.ato.flightmember import apply_default_player_laser_code
from game.ato.flighttype import FlightType
from game.ato.package import Package
from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.domains import Domain, require_domain
from game.claudecommander.mcp.tools import ToolError
from game.claudecommander.tools.airplan import (
    ESCORT_TASKS,
    TotArgs,
    apply_tot,
    editable_flight,
    editable_package,
    find_divert_field,
    flight_deleted,
    flight_label,
    flight_summary,
    flight_warnings,
    list_text,
    package_header,
    package_updated,
    parse_start_type,
    parse_tot,
    range_facts,
    refresh_asap,
    timing_warnings,
)
from game.claudecommander.tools.common import parse_flight_type
from game.claudecommander.tools.loadouts import apply_named_loadout
from game.claudecommander.tools.lookup import _find
from game.claudecommander.view import CommanderView, Json, PACKAGE_IDS
from game.dcs.aircrafttype import AircraftType

if TYPE_CHECKING:
    from game.sim import GameUpdateEvents
    from game.squadrons import Squadron
    from game.theater import MissionTarget


class FlightSpec(BaseModel):
    task: str = Field(
        description='Mission type, e.g. "Strike", "SEAD", "DEAD", "BARCAP", '
        '"TARCAP", "Escort", "SEAD Escort", "CAS", "BAI", "OCA/Runway", '
        '"Anti-ship", "AEW&C", "Refueling". It must be valid for the target '
        "(see get_mission_targets)."
    )
    count: int = Field(ge=1, le=4, description="Aircraft in the flight.")
    aircraft_type: Optional[str] = Field(
        default=None,
        description="Aircraft type (id as in get_own_forces squadrons). Used to "
        "pick a squadron when squadron_id is not given.",
    )
    squadron_id: Optional[str] = Field(
        default=None, description="The squadron to take the aircraft from."
    )
    start_type: Optional[str] = Field(
        default=None,
        description="cold, warm, runway or in_flight. Defaults to the campaign "
        "setting (for player flights, the player start type setting).",
    )
    loadout: Optional[str] = Field(
        default=None,
        description="Named loadout (see lookup_aircraft). Defaults to the default "
        "loadout for the task.",
    )


class CreatePackageArgs(TotArgs):
    target_id: str = Field(
        description="Id of the target: a base, ground object or front line, or one "
        "of your bases for BARCAP/AEW&C/refueling."
    )
    flights: list[FlightSpec] = Field(min_length=1, max_length=8)


class AddFlightArgs(FlightSpec):
    package_id: str


class FlightIdArgs(BaseModel):
    flight_id: str


class PackageIdArgs(BaseModel):
    package_id: str


class SetTotArgs(TotArgs):
    package_id: str


class StartTypeArgs(BaseModel):
    flight_id: str
    start_type: str = Field(description="cold, warm, runway or in_flight.")


def _valid_tasks(view: CommanderView, target: MissionTarget) -> list[FlightType]:
    tasks = view.mission_types_for(target)
    if not view.game.settings.plugin_option("ctld"):
        tasks = [t for t in tasks if t is not FlightType.AIR_ASSAULT]
    return tasks


def _check_task(view: CommanderView, target: MissionTarget, task: FlightType) -> None:
    valid = _valid_tasks(view, target)
    if task not in valid:
        extra = ""
        if task is FlightType.AIR_ASSAULT and task in view.mission_types_for(target):
            extra = " Air assault needs the CTLD plugin, which is off."
        raise ToolError(
            f"{task.value} is not a valid mission against {target.name} for your "
            f"side.{extra} Valid: {', '.join(t.value for t in valid)}."
        )


def _squadron_problems(
    view: CommanderView, squadron: Squadron, task: FlightType, count: int
) -> list[str]:
    problems = []
    name = (
        f"{squadron.name} ({squadron.aircraft.variant_id} at {squadron.location.name})"
    )
    if not squadron.capable_of(task):
        capable = sorted(t.value for t in squadron.aircraft.iter_task_capabilities())
        problems.append(
            f"{name} cannot fly {task.value} (it can: {', '.join(capable)})"
        )
    if squadron.location.captured is not view.player:
        problems.append(f"{name} is at a base you no longer hold")
    elif not squadron.location.runway_is_operational():
        problems.append(
            f"{name}: the runway at {squadron.location.name} is not operational"
        )
    if squadron.untasked_aircraft < count:
        problems.append(
            f"{name} has {squadron.untasked_aircraft} untasked aircraft, {count} needed"
        )
    if not squadron.can_provide_pilots(count):
        problems.append(
            f"{name} has {squadron.number_of_available_pilots} available pilots, "
            f"{count} needed"
        )
    if count > squadron.aircraft.max_group_size:
        problems.append(
            f"{squadron.aircraft.variant_id} flies in groups of at most "
            f"{squadron.aircraft.max_group_size}"
        )
    return problems


def _choose_squadron(
    view: CommanderView, target: MissionTarget, spec: FlightSpec, task: FlightType
) -> tuple[Squadron, Json]:
    """The squadron for a flight and the facts that qualified it."""
    if spec.squadron_id is not None:
        squadron = view.own_squadron(spec.squadron_id)
        if spec.aircraft_type is not None:
            wanted = _find("aircraft", spec.aircraft_type, AircraftType.iter_all())
            if wanted != squadron.aircraft:
                raise ToolError(
                    f"Squadron {squadron.name} flies {squadron.aircraft.variant_id}, "
                    f"not {wanted.variant_id}. Drop aircraft_type or pick another "
                    "squadron."
                )
        problems = _squadron_problems(view, squadron, task, spec.count)
        if problems:
            raise ToolError("; ".join(problems) + ".")
        in_range, distance, limit = range_facts(squadron, target)
        return squadron, {
            "chosen": "as requested",
            "distance_nm": distance,
            "planner_range_nm": limit,
        }

    squadrons = list(view.coalition.air_wing.iter_squadrons())
    if spec.aircraft_type is not None:
        aircraft = _find(
            "aircraft",
            spec.aircraft_type,
            list({s.aircraft for s in squadrons}) or list(AircraftType.iter_all()),
        )
        squadrons = [s for s in squadrons if s.aircraft == aircraft]
        if not squadrons:
            raise ToolError(f"You have no {aircraft.variant_id} squadron.")
    else:
        squadrons = [s for s in squadrons if s.capable_of(task)]
        if not squadrons:
            raise ToolError(f"None of your squadrons can fly {task.value}.")

    qualified: list[tuple[float, Squadron, float]] = []
    rejected: list[str] = []
    for squadron in squadrons:
        problems = _squadron_problems(view, squadron, task, spec.count)
        in_range, distance, limit = range_facts(squadron, target)
        if not in_range:
            problems.append(
                f"{squadron.name}: target {distance} nm away, planner range {limit} nm"
            )
        if problems:
            rejected.extend(problems)
        else:
            qualified.append((distance, squadron, limit))
    if not qualified:
        raise ToolError(
            f"No squadron qualifies for {spec.count}x {task.value}: "
            f"{list_text(rejected, 12)}. Give squadron_id to use an out-of-range "
            "squadron anyway."
        )
    qualified.sort(key=lambda q: q[0])
    distance, squadron, limit = qualified[0]
    info: Json = {
        "chosen": "closest qualifying squadron: capable of the task, enough "
        "untasked aircraft and pilots, runway operational, within planner range",
        "distance_nm": distance,
        "planner_range_nm": limit,
    }
    others = [f"{s.name} ({s.id}, {d} nm)" for d, s, _ in qualified[1:6]]
    if others:
        info["other_qualifying"] = others
    return squadron, info


def _new_flight(
    view: CommanderView, package: Package, spec: FlightSpec
) -> tuple[Flight, Json]:
    task = parse_flight_type(spec.task)
    _check_task(view, package.target, task)
    squadron, info = _choose_squadron(view, package.target, spec, task)
    settings = view.game.settings
    required = squadron.location.required_aircraft_start_type
    requested = parse_start_type(spec.start_type) if spec.start_type else None
    if required is not None:
        if requested is not None and requested is not required:
            info["start_type_note"] = (
                f"{squadron.location.name} requires {required.value} starts"
            )
        start_type = required
    else:
        start_type = requested or settings.default_start_type
    flight = Flight(
        package,
        squadron,
        spec.count,
        task,
        start_type,
        divert=find_divert_field(view, package.target, squadron),
    )
    for member in flight.iter_members():
        apply_default_player_laser_code(
            member, settings, squadron.coalition.laser_code_registry
        )
    if required is None and requested is None and flight.client_count > 0:
        # Like PackageBuilder: player flights use the player start type setting.
        flight.start_type = settings.default_start_type_client
    package.add_flight(flight)
    return flight, info


def _plan_order(flights: list[Flight]) -> list[Flight]:
    main = [f for f in flights if f.flight_type not in ESCORT_TASKS]
    escorts = [f for f in flights if f.flight_type in ESCORT_TASKS]
    return main + escorts


def _build_plans(package: Package, flights: list[Flight]) -> None:
    for flight in _plan_order(flights):
        flight.recreate_flight_plan()


def _rollback(package: Package, flights: list[Flight]) -> None:
    for flight in flights:
        if flight in package.flights:
            package.remove_flight(flight)


def _add_flights(
    view: CommanderView, package: Package, specs: list[FlightSpec]
) -> tuple[list[Flight], list[Json]]:
    """Adds flights with flight plans to a package, all or nothing."""
    created: list[Flight] = []
    infos: list[Json] = []
    try:
        for spec in specs:
            flight, info = _new_flight(view, package, spec)
            created.append(flight)
            infos.append(info)
        try:
            _build_plans(package, created)
        except Exception as ex:
            logging.exception("Commander flight planning failed")
            raise ToolError(f"Could not build the flight plans: {ex}")
        for flight, spec, info in zip(created, specs, infos):
            if spec.loadout is not None:
                apply_named_loadout(view, flight, spec.loadout)
    except Exception:
        _rollback(package, created)
        raise
    return created, infos


def _only_escorts(package: Package) -> bool:
    return bool(package.flights) and all(
        f.flight_type in ESCORT_TASKS for f in package.flights
    )


def _plan_result(
    view: CommanderView,
    package: Package,
    flights: list[Flight],
    infos: list[Json],
    warnings: list[str],
) -> Json:
    entries = []
    for flight, info in zip(flights, infos):
        entry = flight_summary(flight)
        entry["squadron_choice"] = info
        entries.append(entry)
        warnings.extend(flight_warnings(view, flight))
    result: Json = {"package": package_header(package), "flights": entries}
    other = [f for f in package.flights if f not in flights]
    if other:
        result["other_flights"] = [
            flight_summary(f, include_route=False) for f in other
        ]
    if warnings:
        result["warnings"] = warnings
    return result


@commander_tools.register(
    "create_package",
    "Creates a package against a target with one or more flights and builds "
    "flyable flight plans for them the way the game's planner does (escorts follow "
    "the main flights' package route). For each flight give the task and count, "
    "and either squadron_id or aircraft_type (or neither: then the closest "
    "squadron that can fly the task, has the aircraft and pilots, an operational "
    "runway and the target in planner range is used; the result says which and "
    "why). Timing: tot, minutes_after_start or asap; with none of them the package "
    "is ASAP. Returns the package and flight ids, squadrons, times and each "
    "route (waypoint index, type, x/y, altitude, times), plus warnings: known "
    "threats along the routes, fuel and range. All or nothing: on error nothing "
    "is created.",
    CreatePackageArgs,
)
def create_package(context: CommanderContext, args: CreatePackageArgs) -> Json:
    require_domain(context.game, context.player, Domain.AIR_TASKING)
    view = context.view()
    game = context.game
    target = view.mission_target(args.target_id)
    tot = parse_tot(game, args)
    package = Package(target, game.db.flights)
    flights, infos = _add_flights(view, package, args.flights)
    if _only_escorts(package):
        _rollback(package, flights)
        raise ToolError(
            "A package cannot consist of escort flights only (Escort, SEAD Escort). "
            "Add the flight they protect."
        )
    warnings = apply_tot(game, package, tot)
    view.coalition.ato.add_package(package)
    for flight in package.flights:
        context.events.new_flight(flight)
    game.compute_unculled_zones(context.events)
    return _plan_result(view, package, flights, infos, warnings)


@commander_tools.register(
    "add_flight",
    "Adds a flight to one of your packages and builds its flight plan (same "
    "flight fields as create_package). ASAP packages get a new TOT. Returns the "
    "flight with its route and warnings.",
    AddFlightArgs,
)
def add_flight(context: CommanderContext, args: AddFlightArgs) -> Json:
    require_domain(context.game, context.player, Domain.AIR_TASKING)
    view = context.view()
    package = editable_package(view, args.package_id)
    spec = FlightSpec.model_validate(args.model_dump(exclude={"package_id"}))
    flights, infos = _add_flights(view, package, [spec])
    refresh_asap(context.game, package)
    context.events.new_flight(flights[0])
    package_updated(context.events, package)
    warnings = [] if package.auto_asap else timing_warnings(context.game, package)
    return _plan_result(view, package, flights, infos, warnings)


def _delete_flight(events: GameUpdateEvents, package: Package, flight: Flight) -> None:
    if not flight.state.cancelable:
        raise ToolError(f"Flight {flight_label(flight)} is already under way.")
    flight_deleted(events, flight)
    package.remove_flight(flight)


@commander_tools.register(
    "delete_flight",
    "Deletes one of your flights; its aircraft and pilots become available again. "
    "A package left without flights is deleted too.",
    FlightIdArgs,
)
def delete_flight(context: CommanderContext, args: FlightIdArgs) -> Json:
    require_domain(context.game, context.player, Domain.AIR_TASKING)
    view = context.view()
    flight = editable_flight(view, args.flight_id)
    package = flight.package
    package_id = PACKAGE_IDS.id_for(package)
    _delete_flight(context.events, package, flight)
    result: Json = {"deleted_flight": args.flight_id, "package_id": package_id}
    if not package.flights:
        view.coalition.ato.remove_package(package)
        context.game.compute_unculled_zones(context.events)
        result["package_deleted"] = True
        return result
    refresh_asap(context.game, package)
    package_updated(context.events, package)
    result["package"] = package_header(package)
    if _only_escorts(package):
        result["warnings"] = [
            "The package now has only escort flights, which have nothing to escort."
        ]
    return result


@commander_tools.register(
    "delete_package",
    "Deletes one of your packages and all its flights.",
    PackageIdArgs,
)
def delete_package(context: CommanderContext, args: PackageIdArgs) -> Json:
    require_domain(context.game, context.player, Domain.AIR_TASKING)
    view = context.view()
    package = editable_package(view, args.package_id)
    for flight in package.flights:
        if not flight.state.cancelable:
            raise ToolError(f"Flight {flight_label(flight)} is already under way.")
    for flight in list(package.flights):
        flight_deleted(context.events, flight)
    flights = len(package.flights)
    view.coalition.ato.remove_package(package)
    context.game.compute_unculled_zones(context.events)
    return {"deleted_package": args.package_id, "flights_deleted": flights}


@commander_tools.register(
    "set_package_tot",
    "Sets the time over target of one of your packages: tot (mission-local "
    "HH:MM[:SS] or ISO datetime), minutes_after_start, or asap. Every flight's "
    "times follow the package TOT. Returns the new times and a warning if flights "
    "would need to start before mission start.",
    SetTotArgs,
)
def set_package_tot(context: CommanderContext, args: SetTotArgs) -> Json:
    require_domain(context.game, context.player, Domain.AIR_TASKING)
    view = context.view()
    package = editable_package(view, args.package_id)
    if args.given() == 0:
        raise ToolError(
            "Give one of tot, minutes_after_start or asap=true (to turn ASAP off, "
            "give an explicit tot or minutes_after_start)."
        )
    tot = parse_tot(context.game, args)
    warnings = apply_tot(context.game, package, tot)
    package_updated(context.events, package)
    result: Json = {
        "package": package_header(package),
        "flights": [flight_summary(f, include_route=False) for f in package.flights],
    }
    if warnings:
        result["warnings"] = warnings
    return result


@commander_tools.register(
    "set_flight_start_type",
    "Sets how a flight starts: cold (engines off at parking), warm (engines "
    "running at parking), runway, or in_flight (spawns airborne). Anything but "
    "cold makes the flight untargetable by enemy OCA/Aircraft strikes. Off-map "
    "bases always start in flight.",
    StartTypeArgs,
)
def set_flight_start_type(context: CommanderContext, args: StartTypeArgs) -> Json:
    require_domain(context.game, context.player, Domain.AIR_TASKING)
    view = context.view()
    flight = editable_flight(view, args.flight_id)
    start_type = parse_start_type(args.start_type)
    required = flight.departure.required_aircraft_start_type
    if required is not None and start_type is not required:
        raise ToolError(
            f"Flights from {flight.departure.name} must start {required.value}."
        )
    flight.start_type = start_type
    refresh_asap(context.game, flight.package)
    package_updated(context.events, flight.package)
    result: Json = {"flight": flight_summary(flight, include_route=False)}
    warnings = timing_warnings(context.game, flight.package)
    if warnings:
        result["warnings"] = warnings
    return result
