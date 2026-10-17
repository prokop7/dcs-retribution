"""The air planning write tools on a private copy of the generated campaign."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Optional

import pytest

from game import Game
from game.ato.flightplans.custom import CustomFlightPlan
from game.ato.flighttype import FlightType
from game.ato.flightwaypointtype import FlightWaypointType
from game.ato.package import Package
from game.claudecommander.context import CommanderContext
from game.claudecommander.mcpserver import commander_dispatcher
from game.claudecommander.tools.ato import locked_package_descriptions
from game.claudecommander.tools.loadouts import named_loadouts
from game.claudecommander.view import CommanderView, PACKAGE_IDS
from game.sim import GameUpdateEvents
from game.squadrons import Squadron
from game.theater import ControlPoint, TheaterGroundObject
from game.theater.player import Player

MAX_RESPONSE_CHARS = 40_000

WRITE_TOOLS = {
    "create_package",
    "add_flight",
    "delete_flight",
    "delete_package",
    "set_package_tot",
    "edit_flight_waypoints",
    "set_flight_loadout",
    "set_flight_start_type",
    "set_front_line_stance",
    "buy_aircraft",
    "cancel_aircraft_order",
    "buy_ground_units",
    "cancel_ground_unit_order",
    "buy_or_rebuild_site",
    "repair_site",
    "repair_building",
    "repair_runway",
    "transfer_ground_units",
    "relocate_squadron",
    "write_journal",
    "finish_turn_planning",
}
READ_TOOLS = {
    "estimate_flight",
    "lookup_weapon",
    "validate_air_plan",
    "read_journal",
    "get_site_options",
}


class Caller:
    """Calls commander tools through the MCP dispatcher and keeps the events."""

    def __init__(self, game: Game, player: Player) -> None:
        self.game = game
        self.player = player
        self.events = GameUpdateEvents()

    def raw(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        response = commander_dispatcher.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            lambda: CommanderContext(self.game, self.player, self.events),
        )
        assert isinstance(response, dict)
        result = response["result"]
        text = result["content"][0]["text"]
        assert len(text) < MAX_RESPONSE_CHARS, f"{name} returned {len(text)} chars"
        return text, result["isError"]

    def ok(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        text, is_error = self.raw(name, arguments)
        assert not is_error, text
        data = json.loads(text)
        assert isinstance(data, dict)
        return data

    def error(self, name: str, arguments: dict[str, Any]) -> str:
        text, is_error = self.raw(name, arguments)
        assert is_error, text
        return text


def clear_ato(game: Game, player: Player) -> None:
    """Frees every aircraft and pilot of a side."""
    game.coalition_for(player).ato.clear()


def squadron(game: Game, player: Player, variant: str) -> Squadron:
    for s in game.coalition_for(player).air_wing.iter_squadrons():
        if s.aircraft.variant_id == variant:
            return s
    raise AssertionError(f"No {variant} squadron")


def enemy_target(
    game: Game, player: Player, task: FlightType, category: Optional[str] = None
) -> TheaterGroundObject:
    view = CommanderView(game, player)
    for tgo in sorted(view.enemy_ground_objects(), key=lambda t: t.name):
        if category is not None and tgo.category != category:
            continue
        if tgo.is_dead:
            continue
        if task in view.mission_types_for(tgo):
            return tgo
    raise AssertionError(f"No enemy {category} target for {task}")


def own_airbase(game: Game, player: Player) -> ControlPoint:
    for cp in game.theater.control_points_for(player):
        if cp.cptype.name == "AIRBASE":
            return cp
    raise AssertionError("No own airbase")


def package_by_id(game: Game, player: Player, package_id: str) -> Package:
    for package in game.coalition_for(player).ato.packages:
        if PACKAGE_IDS.id_for(package) == package_id:
            return package
    raise AssertionError(f"No package {package_id}")


def strike(caller: Caller, **extra: Any) -> dict[str, Any]:
    target = enemy_target(caller.game, caller.player, FlightType.STRIKE, "factory")
    return caller.ok(
        "create_package",
        {
            "target_id": str(target.id),
            "flights": [
                {
                    "task": "Strike",
                    "count": 2,
                    "aircraft_type": "F-16CM Fighting Falcon (Block 50)",
                }
            ],
            **extra,
        },
    )


def test_tools_are_listed_with_valid_schemas() -> None:
    response = commander_dispatcher.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, lambda: None  # type: ignore[arg-type, return-value]
    )
    assert isinstance(response, dict)
    tools = {t["name"]: t for t in response["result"]["tools"]}
    assert (WRITE_TOOLS | READ_TOOLS) <= set(tools)
    for name in WRITE_TOOLS | READ_TOOLS:
        tool = tools[name]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert tool["description"]
        json.dumps(schema)
        assert tool["annotations"] == {"readOnlyHint": name in READ_TOOLS}


def test_create_strike_package(game: Game) -> None:
    clear_ato(game, Player.BLUE)
    caller = Caller(game, Player.BLUE)
    data = strike(caller)
    package = package_by_id(game, Player.BLUE, data["package"]["id"])
    assert package in game.blue.ato.packages
    assert package.auto_asap
    assert data["package"]["tot"] is not None
    (flight_data,) = data["flights"]
    (flight,) = package.flights
    assert flight_data["id"] == str(flight.id)
    assert flight.flight_type is FlightType.STRIKE
    assert flight.count == 2
    assert flight_data["squadron_choice"]["chosen"].startswith("closest")
    types = [w["type"] for w in flight_data["route"]]
    assert types[0] == "TAKEOFF"
    assert "JOIN" in types and "INGRESS_STRIKE" in types
    assert "TARGET_POINT" in types or "TARGET_GROUP_LOC" in types
    assert len(flight_data["route"]) == len(flight.flight_plan.waypoints)
    assert flight_data["takeoff"] is not None
    assert caller.events.new_flights == {flight}


def test_create_dead_package_with_sead_escort(game: Game) -> None:
    clear_ato(game, Player.BLUE)
    caller = Caller(game, Player.BLUE)
    target = enemy_target(game, Player.BLUE, FlightType.DEAD, "aa")
    f16 = squadron(game, Player.BLUE, "F-16CM Fighting Falcon (Block 50)")
    data = caller.ok(
        "create_package",
        {
            "target_id": str(target.id),
            "flights": [
                {"task": "SEAD Escort", "count": 2, "squadron_id": str(f16.id)},
                {"task": "DEAD", "count": 2, "aircraft_type": "F-4E Phantom II"},
            ],
            "minutes_after_start": 60,
        },
    )
    package = package_by_id(game, Player.BLUE, data["package"]["id"])
    assert package.primary_task is FlightType.DEAD
    assert not package.auto_asap
    start = game.conditions.start_time
    assert package.time_over_target == start + timedelta(minutes=60)
    assert {f.flight_type for f in package.flights} == {
        FlightType.DEAD,
        FlightType.SEAD_ESCORT,
    }
    for flight in package.flights:
        assert len(flight.flight_plan.waypoints) > 4
    assert data["flights"][0]["squadron_choice"]["chosen"] == "as requested"
    assert len(caller.events.new_flights) == 2


def test_create_barcap_over_own_base(game: Game) -> None:
    clear_ato(game, Player.BLUE)
    caller = Caller(game, Player.BLUE)
    base = own_airbase(game, Player.BLUE)
    data = caller.ok(
        "create_package",
        {
            "target_id": str(base.id),
            "flights": [
                {
                    "task": "BARCAP",
                    "count": 2,
                    "aircraft_type": "F-15C Eagle",
                    "start_type": "runway",
                }
            ],
            "tot": (game.conditions.start_time + timedelta(minutes=45)).strftime(
                "%H:%M"
            ),
        },
    )
    (flight,) = package_by_id(game, Player.BLUE, data["package"]["id"]).flights
    assert flight.flight_type is FlightType.BARCAP
    assert flight.start_type.value == "Runway"
    assert any(
        w.waypoint_type is FlightWaypointType.PATROL_TRACK
        for w in flight.flight_plan.waypoints
    )


def test_create_package_validation(game: Game) -> None:
    clear_ato(game, Player.BLUE)
    caller = Caller(game, Player.BLUE)
    factory = enemy_target(game, Player.BLUE, FlightType.STRIKE, "factory")
    base = own_airbase(game, Player.BLUE)
    packages_before = len(game.blue.ato.packages)

    text = caller.error(
        "create_package",
        {"target_id": str(factory.id), "flights": [{"task": "BARCAP", "count": 2}]},
    )
    assert "not a valid mission" in text and "Strike" in text

    text = caller.error(
        "create_package",
        {"target_id": "nope", "flights": [{"task": "Strike", "count": 2}]},
    )
    assert "No control point, ground object or front line" in text

    eagle = squadron(game, Player.BLUE, "F-15C Eagle")
    text = caller.error(
        "create_package",
        {
            "target_id": str(factory.id),
            "flights": [{"task": "Strike", "count": 2, "squadron_id": str(eagle.id)}],
        },
    )
    assert "cannot fly Strike" in text

    hawkeye = squadron(game, Player.BLUE, "E-2C Hawkeye")
    text = caller.error(
        "create_package",
        {
            "target_id": str(base.id),
            "flights": [{"task": "AEW&C", "count": 3, "squadron_id": str(hawkeye.id)}],
        },
    )
    assert "untasked aircraft" in text

    text = caller.error(
        "create_package",
        {
            "target_id": str(factory.id),
            "flights": [{"task": "Escort", "count": 2, "aircraft_type": "F-15C Eagle"}],
        },
    )
    assert "escort flights only" in text

    before_start = game.conditions.start_time - timedelta(minutes=30)
    text = caller.error(
        "create_package",
        {
            "target_id": str(factory.id),
            "flights": [{"task": "Strike", "count": 2}],
            "tot": before_start.strftime("%H:%M"),
        },
    )
    assert "before mission start" in text

    # Nothing was created and every aircraft went back to its squadron.
    assert len(game.blue.ato.packages) == packages_before
    assert eagle.untasked_aircraft == eagle.owned_aircraft
    assert not caller.events.new_flights


def test_add_and_delete_flights(game: Game) -> None:
    clear_ato(game, Player.BLUE)
    caller = Caller(game, Player.BLUE)
    data = strike(caller)
    package_id = data["package"]["id"]
    package = package_by_id(game, Player.BLUE, package_id)
    eagle = squadron(game, Player.BLUE, "F-15C Eagle")
    untasked = eagle.untasked_aircraft

    added = caller.ok(
        "add_flight",
        {
            "package_id": package_id,
            "task": "Escort",
            "count": 2,
            "squadron_id": str(eagle.id),
        },
    )
    escort_id = added["flights"][0]["id"]
    assert eagle.untasked_aircraft == untasked - 2
    assert len(package.flights) == 2
    assert added["other_flights"][0]["id"] == data["flights"][0]["id"]

    caller.events = GameUpdateEvents()
    caller.ok("delete_flight", {"flight_id": escort_id})
    assert eagle.untasked_aircraft == untasked
    assert len(package.flights) == 1
    assert escort_id in {str(i) for i in caller.events.deleted_flights}

    result = caller.ok("delete_flight", {"flight_id": data["flights"][0]["id"]})
    assert result["package_deleted"] is True
    assert package not in game.blue.ato.packages

    data = strike(caller)
    caller.ok("delete_package", {"package_id": data["package"]["id"]})
    assert not game.blue.ato.packages
    caller.error("delete_package", {"package_id": data["package"]["id"]})


def test_set_package_tot_and_start_type(game: Game) -> None:
    clear_ato(game, Player.BLUE)
    caller = Caller(game, Player.BLUE)
    data = strike(caller)
    package_id = data["package"]["id"]
    package = package_by_id(game, Player.BLUE, package_id)
    start = game.conditions.start_time

    tot = start + timedelta(hours=1, minutes=30)
    result = caller.ok(
        "set_package_tot", {"package_id": package_id, "tot": tot.strftime("%H:%M:%S")}
    )
    assert package.time_over_target == tot
    assert not package.auto_asap
    assert result["package"]["tot"] == tot.strftime("%H:%M:%S")

    caller.ok("set_package_tot", {"package_id": package_id, "asap": True})
    package = package_by_id(game, Player.BLUE, package_id)
    assert package.auto_asap
    assert package.time_over_target < tot

    caller.error("set_package_tot", {"package_id": package_id})
    caller.error("set_package_tot", {"package_id": package_id, "tot": "25:00"})
    text = caller.error(
        "set_package_tot",
        {"package_id": package_id, "tot": "10:00", "minutes_after_start": 5},
    )
    assert "only one" in text

    flight_id = data["flights"][0]["id"]
    caller.ok("set_flight_start_type", {"flight_id": flight_id, "start_type": "warm"})
    assert package.flights[0].start_type.value == "Warm"
    text = caller.error(
        "set_flight_start_type", {"flight_id": flight_id, "start_type": "hot"}
    )
    assert "cold, warm, runway, in_flight" in text


def test_waypoint_edit_round_trip(game: Game) -> None:
    clear_ato(game, Player.BLUE)
    caller = Caller(game, Player.BLUE)
    data = strike(caller)
    flight_id = data["flights"][0]["id"]
    flight = package_by_id(game, Player.BLUE, data["package"]["id"]).flights[0]
    route = data["flights"][0]["route"]
    count = len(route)
    join = next(w["i"] for w in route if w["type"] == "JOIN")
    takeoff = route[0]
    point = {"x": takeoff["x"] + 20000, "y": takeoff["y"] + 20000}
    # A navigation point goes after the hold point if the flight has one.
    after = next((w["i"] for w in route if w["type"] == "LOITER"), 0)
    if after:
        text = caller.error(
            "edit_flight_waypoints",
            {
                "flight_id": flight_id,
                "operations": [{"op": "insert", "index": 0, "point": point}],
            },
        )
        assert f"Valid indices: [{after}" in text
    new = after + 1

    result = caller.ok(
        "edit_flight_waypoints",
        {
            "flight_id": flight_id,
            "operations": [
                {"op": "insert", "index": after, "point": point, "altitude_ft": 12000},
                {"op": "set_altitude", "index": join + 1, "altitude_ft": 18000},
            ],
        },
    )
    new_route = result["flight"]["route"]
    assert len(new_route) == count + 1
    assert new_route[new]["type"] == "NAV"
    assert (new_route[new]["x"], new_route[new]["y"]) == (point["x"], point["y"])
    assert new_route[new]["alt_ft"] == 12000
    assert new_route[join + 1]["type"] == "JOIN"
    assert new_route[join + 1]["alt_ft"] == 18000
    waypoints = flight.flight_plan.waypoints
    assert round(waypoints[join + 1].alt.feet) == 18000
    assert flight in caller.events.updated_flights

    moved = {"x": point["x"] + 5000, "y": point["y"]}
    result = caller.ok(
        "edit_flight_waypoints",
        {
            "flight_id": flight_id,
            "operations": [{"op": "move", "index": new, "point": moved}],
        },
    )
    assert result["flight"]["route"][new]["x"] == moved["x"]

    result = caller.ok(
        "edit_flight_waypoints",
        {"flight_id": flight_id, "operations": [{"op": "delete", "index": new}]},
    )
    assert len(result["flight"]["route"]) == count

    text = caller.error(
        "edit_flight_waypoints",
        {"flight_id": flight_id, "operations": [{"op": "delete", "index": 0}]},
    )
    assert "TAKEOFF waypoint cannot be deleted" in text
    target_index = next(
        w["i"] for w in route if w["type"] in ("TARGET_POINT", "TARGET_GROUP_LOC")
    )
    text = caller.error(
        "edit_flight_waypoints",
        {
            "flight_id": flight_id,
            "operations": [{"op": "move", "index": target_index, "point": point}],
        },
    )
    assert "cannot be moved" in text
    text = caller.error(
        "edit_flight_waypoints",
        {"flight_id": flight_id, "operations": [{"op": "delete", "index": join}]},
    )
    assert "allow_custom_flight_plan" in text
    text = caller.error(
        "edit_flight_waypoints",
        {"flight_id": flight_id, "operations": [{"op": "delete", "index": 99}]},
    )
    assert "out of range" in text

    result = caller.ok(
        "edit_flight_waypoints",
        {
            "flight_id": flight_id,
            "operations": [{"op": "delete", "index": join}],
            "allow_custom_flight_plan": True,
        },
    )
    assert isinstance(flight.flight_plan, CustomFlightPlan)
    assert result["flight"]["custom_flight_plan"] is True
    assert len(result["flight"]["route"]) == count - 1


def test_loadouts(game: Game) -> None:
    clear_ato(game, Player.BLUE)
    caller = Caller(game, Player.BLUE)
    data = strike(caller)
    flight_id = data["flights"][0]["id"]
    flight = package_by_id(game, Player.BLUE, data["package"]["id"]).flights[0]
    names = sorted(named_loadouts(flight.unit_type))
    assert names

    result = caller.ok(
        "set_flight_loadout", {"flight_id": flight_id, "loadout_name": names[0]}
    )
    assert result["loadout"]["name"] == names[0]
    assert all(m.loadout.name == names[0] for m in flight.iter_members())

    text = caller.error(
        "set_flight_loadout", {"flight_id": flight_id, "loadout_name": "No such"}
    )
    assert "has no loadout named" in text and names[0] in text

    result = caller.ok(
        "set_flight_loadout",
        {"flight_id": flight_id, "pylons": {"1": "AIM-9X", "2": None}},
    )
    pylons = result["loadout"]["pylons"]
    assert "AIM-9X" in pylons["1"]
    assert "2" not in pylons
    assert result["loadout"]["custom"] is True
    for member in flight.iter_members():
        assert member.loadout.is_custom
        weapon = member.loadout.pylons[1]
        assert weapon is not None and "AIM-9X" in weapon.name
        assert 2 not in member.loadout.pylons

    text = caller.error(
        "set_flight_loadout", {"flight_id": flight_id, "pylons": {"99": "AIM-9X"}}
    )
    assert "has no pylon 99" in text
    text = caller.error(
        "set_flight_loadout", {"flight_id": flight_id, "pylons": {"1": "GBU-31"}}
    )
    assert "Pylon 1 has no weapon matching" in text

    game.settings.restrict_weapons_by_date = True
    game.date = game.date.replace(year=1975)
    text = caller.error(
        "set_flight_loadout", {"flight_id": flight_id, "pylons": {"1": "AIM-9X"}}
    )
    assert "not available on the campaign date 1975" in text
    assert "can carry on this date" in text


def test_lookup_weapon(game: Game) -> None:
    caller = Caller(game, Player.BLUE)
    data = caller.ok("lookup_weapon", {"name": "AIM-120C"})
    assert data["introduced"]
    assert data["weapons"]
    assert any(
        c["aircraft"] == "F-16CM Fighting Falcon (Block 50)"
        for c in data["carried_by_your_aircraft"]
    )
    assert "No weapon matches" in caller.error("lookup_weapon", {"name": "zzzz"})


def test_estimate_and_validate(game: Game) -> None:
    caller = Caller(game, Player.BLUE)
    for package in game.blue.ato.packages:
        for flight in package.flights:
            data = caller.ok("estimate_flight", {"flight_id": str(flight.id)})
            assert data["route_distance_nm"] > 0
            assert "known_threats_on_route" in data
    plan = caller.ok("validate_air_plan", {})
    assert plan["packages"] == len(game.blue.ato.packages)
    assert isinstance(plan["issues"], list)


def test_locked_packages_are_read_only(game: Game) -> None:
    clear_ato(game, Player.BLUE)
    caller = Caller(game, Player.BLUE)
    data = strike(caller)
    package_id = data["package"]["id"]
    flight_id = data["flights"][0]["id"]
    package = package_by_id(game, Player.BLUE, package_id)
    package.player_locked = True

    for name, arguments in [
        ("delete_package", {"package_id": package_id}),
        ("delete_flight", {"flight_id": flight_id}),
        ("set_package_tot", {"package_id": package_id, "asap": True}),
        ("add_flight", {"package_id": package_id, "task": "Escort", "count": 2}),
        (
            "edit_flight_waypoints",
            {
                "flight_id": flight_id,
                "operations": [{"op": "set_altitude", "index": 1, "altitude_ft": 1}],
            },
        ),
        ("set_flight_loadout", {"flight_id": flight_id, "pylons": {"1": None}}),
        ("set_flight_start_type", {"flight_id": flight_id, "start_type": "cold"}),
    ]:
        assert "locked" in caller.error(name, arguments), name
    assert package in game.blue.ato.packages

    ato = caller.ok("get_ato", {"package_id": package_id})
    assert ato["packages"][0]["locked"] is True
    (line,) = locked_package_descriptions(game.blue)
    assert line.startswith(package_id) and "Strike" in line


def test_old_packages_load_unlocked() -> None:
    package = Package.__new__(Package)
    assert package.player_locked is False


def test_red_can_plan_too(game: Game) -> None:
    clear_ato(game, Player.RED)
    caller = Caller(game, Player.RED)
    base = own_airbase(game, Player.RED)
    data = caller.ok(
        "create_package",
        {"target_id": str(base.id), "flights": [{"task": "BARCAP", "count": 2}]},
    )
    assert data["flights"][0]["aircraft"]
    blue_squadron = next(game.blue.air_wing.iter_squadrons())
    text = caller.error(
        "create_package",
        {
            "target_id": str(base.id),
            "flights": [
                {"task": "BARCAP", "count": 2, "squadron_id": str(blue_squadron.id)}
            ],
        },
    )
    assert "No squadron of yours" in text


def test_purpose_and_force_utilization(game: Game) -> None:
    clear_ato(game, Player.BLUE)
    caller = Caller(game, Player.BLUE)
    before = caller.ok("validate_air_plan", {})["force"]
    assert before["aircraft_tasked"] == 0
    assert before["aircraft_ready_untasked"] > 0
    assert before["idle_squadrons"]

    base = own_airbase(game, Player.BLUE)
    created = caller.ok(
        "create_package",
        {
            "target_id": str(base.id),
            "flights": [{"task": "BARCAP", "count": 2}],
            "purpose": "Cover the airfield against the morning raid.",
        },
    )
    package_id = created["package"]["id"] if "package" in created else created["id"]
    ato = caller.ok("get_ato", {"package_id": package_id})
    assert "Cover the airfield" in json.dumps(ato)

    after = caller.ok("validate_air_plan", {})["force"]
    assert after["aircraft_tasked"] == 2
    assert after["aircraft_ready_untasked"] == before["aircraft_ready_untasked"] - 2

    situation = caller.ok("get_situation", {})
    assert situation["force"]["aircraft_tasked"] == 2
    assert "idle_squadrons" not in situation["force"]
    assert situation["mission_window"]["start"]
