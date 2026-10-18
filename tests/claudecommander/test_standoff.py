"""Weapon reach facts and opt-in standoff attacks."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from dcs.mapping import Point
from dcs.mission import Mission
from dcs.terrain import Caucasus

from game import Game
from game.ato.flight import Flight
from game.ato.flighttype import FlightType
from game.ato.flightwaypointtype import FlightWaypointType
from game.ato.standoff import (
    JOIN_CLEARANCE,
    find_launch_point,
    loadout_reach,
)
from game.claudecommander.tools.loadouts import named_loadouts
from game.claudecommander.tools.reach import covering_threats_at
from game.claudecommander.view import CommanderView
from game.theater import TheaterGroundObject
from game.theater.player import Player
from game.utils import nautical_miles
from tests.claudecommander.test_air_tools import (
    Caller,
    clear_ato,
    own_airbase,
    package_by_id,
)

F16 = "F-16CM Fighting Falcon (Block 50)"
#: The F-16's named loadout with JSOWs (glide weapons, ~60 nm).
JSOW_LOADOUT = "Retribution Anti-ship"
NM = 1852.0


def strike_factories(game: Game) -> list[TheaterGroundObject]:
    view = CommanderView(game, Player.BLUE)
    return [
        tgo
        for tgo in sorted(view.enemy_ground_objects(), key=lambda t: t.name)
        if tgo.category == "factory"
        and not tgo.is_dead
        and FlightType.STRIKE in view.mission_types_for(tgo)
    ]


def covered_factory(game: Game) -> TheaterGroundObject:
    """The enemy factory inside the most known threat rings."""
    view = CommanderView(game, Player.BLUE)
    best: tuple[int, TheaterGroundObject] | None = None
    for tgo in strike_factories(game):
        _, threats = covering_threats_at(view, tgo.position, tgo.control_point.position)
        if best is None or len(threats) > best[0]:
            best = (len(threats), tgo)
    assert best is not None and best[0] > 0, "no factory inside threat rings"
    return best[1]


def ring_depth(view: CommanderView, tgo: TheaterGroundObject) -> float:
    """How far (m) known threat rings extend from a target in every direction."""
    _, threats = covering_threats_at(view, tgo.position, tgo.control_point.position)
    return max(
        (
            t.threat_range_m - t.position.distance_to_point(tgo.position)
            for t in threats
        ),
        default=0.0,
    )


def f16_strike(caller: Caller, target: TheaterGroundObject) -> dict[str, Any]:
    return caller.ok(
        "create_package",
        {
            "target_id": str(target.id),
            "flights": [{"task": "Strike", "count": 2, "aircraft_type": F16}],
            "minutes_after_start": 60,
        },
    )


def only_flight(game: Game, data: dict[str, Any]) -> Flight:
    (flight,) = package_by_id(game, Player.BLUE, data["package"]["id"]).flights
    return flight


def ingress_of(flight: Flight) -> Any:
    return flight.flight_plan.layout.ingress


def nm_to_target(flight: Flight, point: Point) -> float:
    return point.distance_to_point(flight.package.target.position) / NM


# Weapon reach in the lookups ---------------------------------------------------------


def test_lookup_aircraft_lists_loadout_reach(game: Game) -> None:
    caller = Caller(game, Player.BLUE)
    data = caller.ok("lookup_aircraft", {"name": F16})
    assert "approximate" in data["loadout_reach_note"]
    loadouts = {entry["name"]: entry for entry in data["loadouts"]}
    jsow = loadouts[JSOW_LOADOUT]
    assert jsow["reach_nm"] == 60
    assert "JSOW" in jsow["reach_weapon"]
    assert "glide_weapon" in jsow["weapon_kinds"]
    strike = loadouts["Retribution Strike"]
    assert strike["reach_nm"] == 13
    assert strike["weapon_kinds"] == ["guided_bomb"]
    # Air-to-air loadouts have no air-to-ground reach.
    assert all(
        "reach_nm" not in entry
        for name, entry in loadouts.items()
        if name.startswith("AIM-120C*6")
    )


def test_lookup_weapon_returns_kind_and_range(game: Game) -> None:
    caller = Caller(game, Player.BLUE)
    data = caller.ok("lookup_weapon", {"name": "AGM-154C JSOW"})
    assert data["kind"] == "glide_weapon"
    assert data["range_nm"] == 60
    assert "Approximate" in data["range_note"]
    data = caller.ok("lookup_weapon", {"name": "Mk 82"})
    assert data["kind"] == "unguided_bomb"
    assert data["range_nm"] == 2
    assert "default for the kind" in data["range_note"]
    data = caller.ok("lookup_weapon", {"name": "AIM-120C"})
    assert data["kind"] is None and data["range_nm"] is None


def test_task_decides_which_weapons_count() -> None:
    from game.dcs.aircrafttype import AircraftType

    tu22 = next(a for a in AircraftType.iter_all() if a.dcs_id == "Tu-22M3")
    kh22 = named_loadouts(tu22)["Kh-22N"]
    # Anti-ship missiles count for anti-ship, not for strikes.
    anti_ship = loadout_reach(kh22, FlightType.ANTISHIP)
    assert anti_ship is not None and anti_ship.reach_nm >= 100
    assert loadout_reach(kh22, FlightType.STRIKE) is None
    overall = loadout_reach(kh22)
    assert overall is not None and overall.kinds == ("anti_ship_missile",)


# Reach warnings ------------------------------------------------------------------------


def test_reach_warning_names_a_loadout_that_reaches(game: Game) -> None:
    caller = Caller(game, Player.BLUE)
    view = CommanderView(game, Player.BLUE)
    # The warning needs rings reaching past the GBUs' 13 nm along the flight's
    # approach, which depends on the generated campaign. Try the factories
    # covered most deeply first.
    warnings: list[str] = []
    for target in sorted(strike_factories(game), key=lambda t: -ring_depth(view, t))[
        :5
    ]:
        clear_ato(game, Player.BLUE)
        data = f16_strike(caller, target)
        warnings = [w for w in data.get("warnings", []) if "threat rings" in w]
        if warnings:
            break
    assert len(warnings) == 1, data.get("warnings")
    flight_id = data["flights"][0]["id"]
    assert "reach ~13 nm" in warnings[0]
    assert repr(JSOW_LOADOUT) in warnings[0]
    # Facts only: the loadout is not changed.
    assert data["flights"][0]["loadout"] == "Retribution Strike"

    issues = caller.ok("validate_air_plan", {})["issues"]
    assert [i for i in issues if i["kind"] == "standoff_reach"]

    result = caller.ok(
        "set_flight_loadout", {"flight_id": flight_id, "loadout_name": JSOW_LOADOUT}
    )
    assert result["loadout"]["reach_nm"] == 60
    assert not any("threat rings" in w for w in result.get("warnings", []))
    issues = caller.ok("validate_air_plan", {})["issues"]
    assert not [i for i in issues if i["kind"] == "standoff_reach"]


# Standoff routes -------------------------------------------------------------------


def test_standoff_attack_moves_the_attack_point_out(game: Game) -> None:
    clear_ato(game, Player.BLUE)
    caller = Caller(game, Player.BLUE)
    target = covered_factory(game)
    data = f16_strike(caller, target)
    flight = only_flight(game, data)
    flight_id = str(flight.id)
    default_types = [w.waypoint_type for w in flight.flight_plan.waypoints]
    default_ingress = nm_to_target(flight, ingress_of(flight).position)
    targets = list(flight.flight_plan.layout.targets)
    assert ingress_of(flight).name == "INGRESS"

    caller.ok(
        "set_flight_loadout", {"flight_id": flight_id, "loadout_name": JSOW_LOADOUT}
    )
    result = caller.ok(
        "set_standoff_attack", {"flight_id": flight_id, "launch_distance_nm": 60}
    )
    assert flight.standoff_attack
    assert flight.standoff_launch_distance_nm == 60
    launch = ingress_of(flight)
    assert launch.name == "LAUNCH"
    assert launch.waypoint_type is FlightWaypointType.INGRESS_STRIKE
    assert nm_to_target(flight, launch.position) == pytest.approx(60, abs=0.5)
    assert nm_to_target(flight, launch.position) > default_ingress
    # The lineup point (10 nm before the default ingress) is passed and dropped;
    # the target waypoints stay for players.
    types = [w.waypoint_type for w in flight.flight_plan.waypoints]
    assert len(types) == len(default_types) - 1
    new_targets = flight.flight_plan.layout.targets
    assert [t.name for t in new_targets] == [t.name for t in targets]
    assert all(t.only_for_player for t in new_targets)
    standoff = result["flight"]["standoff_attack"]
    assert standoff["launch_point_nm_from_target"] == pytest.approx(60, abs=0.5)
    assert standoff["loadout_reach_nm"] == 60
    route_names = [w.get("name") for w in result["flight"]["route"]]
    assert "LAUNCH on " + target.name in route_names
    # The flight passes the launch point before the target time.
    plan = flight.flight_plan
    launch_time = plan.tot_for_waypoint(launch)
    assert launch_time is not None and launch_time < plan.tot

    ato = caller.ok("get_ato", {"package_id": data["package"]["id"]})
    assert ato["packages"][0]["flights"][0]["standoff_attack"]["launch_distance"] == (
        "60 nm"
    )

    caller.ok("set_standoff_attack", {"flight_id": flight_id, "enabled": False})
    assert (flight.standoff_attack, flight.standoff_launch_distance_nm) == (
        False,
        None,
    )
    assert ingress_of(flight).name == "INGRESS"
    assert [w.waypoint_type for w in flight.flight_plan.waypoints] == default_types


def test_derived_launch_distance_follows_the_loadout(game: Game) -> None:
    clear_ato(game, Player.BLUE)
    caller = Caller(game, Player.BLUE)
    data = f16_strike(caller, covered_factory(game))
    flight = only_flight(game, data)
    flight_id = str(flight.id)
    default_ingress = nm_to_target(flight, ingress_of(flight).position)
    # The default loadout's JDAMs (~13 nm) don't reach as far as the ingress
    # point: the attack still starts there, never closer.
    result = caller.ok("set_standoff_attack", {"flight_id": flight_id})
    launch = ingress_of(flight)
    assert launch.name == "LAUNCH"
    assert nm_to_target(flight, launch.position) == pytest.approx(
        default_ingress, abs=0.1
    )
    assert any("already" in w for w in result.get("warnings", []))
    # A loadout with longer reach moves the launch point out (80% of 60 nm),
    # unless the ingress point is already further out.
    result = caller.ok(
        "set_flight_loadout", {"flight_id": flight_id, "loadout_name": JSOW_LOADOUT}
    )
    assert "standoff_attack" in result
    assert nm_to_target(flight, ingress_of(flight).position) == pytest.approx(
        max(48, default_ingress), abs=0.5
    )


def test_standoff_attack_validation(game: Game) -> None:
    clear_ato(game, Player.BLUE)
    caller = Caller(game, Player.BLUE)
    data = f16_strike(caller, covered_factory(game))
    flight_id = data["flights"][0]["id"]
    caller.ok(
        "set_flight_loadout",
        {"flight_id": flight_id, "loadout_name": "AIM-120C*6, FUEL*2"},
    )
    text = caller.error("set_standoff_attack", {"flight_id": flight_id})
    assert "no weapon with known reach" in text
    # An explicit distance needs no reach data.
    caller.ok("set_standoff_attack", {"flight_id": flight_id, "launch_distance_nm": 50})

    base = own_airbase(game, Player.BLUE)
    barcap = caller.ok(
        "create_package",
        {
            "target_id": str(base.id),
            "flights": [{"task": "BARCAP", "count": 2, "aircraft_type": "F-15C Eagle"}],
        },
    )
    text = caller.error(
        "set_standoff_attack", {"flight_id": barcap["flights"][0]["id"]}
    )
    assert "cannot attack from standoff" in text


def test_find_launch_point_geometry() -> None:
    terrain = Caucasus()
    target = Point(0, 0, terrain)
    join = Point(80 * NM, 0, terrain)
    lineup = Point(55 * NM, 0, terrain)
    ingress = Point(45 * NM, 0, terrain)
    path = [join, lineup, ingress]

    launch = find_launch_point(path, target, nautical_miles(50))
    assert launch.distance.nautical_miles == pytest.approx(50)
    assert launch.leg == 1  # between the lineup and the ingress point
    launch = find_launch_point(path, target, nautical_miles(60))
    assert launch.position.x == pytest.approx(60 * NM)
    assert launch.leg == 0
    # Never closer than the ingress point...
    launch = find_launch_point(path, target, nautical_miles(10))
    assert launch.position == ingress
    assert launch.leg == 2
    # ... and not before the join point.
    launch = find_launch_point(path, target, nautical_miles(500))
    assert launch.distance.nautical_miles == pytest.approx(
        80 - JOIN_CLEARANCE.nautical_miles
    )
    assert launch.requested.nautical_miles == pytest.approx(500)

    # A dog-leg approach: the launch point is on the leg that comes within range.
    bent = [Point(40 * NM, 40 * NM, terrain), Point(30 * NM, 0, terrain)]
    launch = find_launch_point(bent, target, nautical_miles(40))
    assert launch.distance.nautical_miles == pytest.approx(40)
    assert 30 * NM < launch.position.x < 40 * NM


# Default behaviour stays as it was ---------------------------------------------------


def test_flights_default_to_no_standoff(test_game: Game) -> None:
    flights = [
        f
        for coalition in (test_game.blue, test_game.red)
        for p in coalition.ato.packages
        for f in p.flights
    ]
    assert flights
    for flight in flights:
        assert flight.standoff_attack is False
        assert flight.standoff_launch_distance_nm is None
        assert "standoff_attack" not in flight.__dict__
        for waypoint in flight.flight_plan.waypoints:
            assert waypoint.name != "LAUNCH"


def test_old_flights_load_without_standoff(game: Game) -> None:
    flight = next(f for p in game.blue.ato.packages for f in p.flights)
    state = flight.__getstate__()
    state.pop("standoff_attack", None)
    state.pop("standoff_launch_distance_nm", None)
    loaded = Flight.__new__(Flight)
    loaded.__setstate__(state)
    assert loaded.standoff_attack is False
    assert loaded.standoff_launch_distance_nm is None


def _miz_groups(path: Path) -> dict[int, Any]:
    mission = Mission()
    mission.load_file(str(path))
    groups = {}
    for coalition in mission.coalition.values():
        for country in coalition.countries.values():
            for group in list(country.plane_group) + list(country.helicopter_group):
                groups[group.id] = group
    return groups


def _attack_tasks(point: Any) -> list[dict[str, Any]]:
    return [
        t.dict()
        for t in point.tasks
        if t.dict().get("id") in ("Bombing", "AttackGroup", "CarpetBombing")
    ]


def test_standoff_route_in_generated_mission(game: Game, tmp_path: Path) -> None:
    from game.sim.gameloop import GameLoop
    from game.sim.gameupdatecallbacks import GameUpdateCallbacks

    htn_flights = [f for p in game.red.ato.packages for f in p.flights]
    clear_ato(game, Player.BLUE)
    caller = Caller(game, Player.BLUE)
    target = covered_factory(game)
    default = only_flight(game, f16_strike(caller, target))
    standoff = only_flight(game, f16_strike(caller, target))
    for flight in (default, standoff):
        caller.ok(
            "set_flight_loadout",
            {"flight_id": str(flight.id), "loadout_name": JSOW_LOADOUT},
        )
    caller.ok(
        "set_standoff_attack", {"flight_id": str(standoff.id), "launch_distance_nm": 60}
    )

    out = tmp_path / "standoff.miz"
    loop = GameLoop(game, GameUpdateCallbacks(lambda: None, lambda e: None))
    loop.pause_and_generate_miz(out)
    groups = _miz_groups(out)
    tpos = target.position

    def distance(point: Any) -> float:
        return math.hypot(point.position.x - tpos.x, point.position.y - tpos.y) / NM

    default_points = groups[default.group_id].points
    standoff_points = groups[standoff.group_id].points
    default_names = [str(p.name) for p in default_points]
    standoff_names = [str(p.name) for p in standoff_points]
    assert "INGRESS" in default_names and "LAUNCH" not in default_names
    assert "LAUNCH" in standoff_names and "INGRESS" not in standoff_names

    ingress = default_points[default_names.index("INGRESS")]
    launch = standoff_points[standoff_names.index("LAUNCH")]
    # The mission matches the plan. The plan launches at 60 nm unless that would
    # put the launch point before the package's join point (see find_launch_point).
    planned = nm_to_target(standoff, ingress_of(standoff).position)
    assert distance(launch) == pytest.approx(planned, abs=0.5)
    assert planned <= 60.5
    assert distance(ingress) < distance(launch)
    # The same attack tasks, on the launch point instead of the ingress point.
    assert len(_attack_tasks(launch)) == len(_attack_tasks(ingress)) > 0
    assert ingress.ETA_locked and launch.ETA_locked
    # AI routes skip the target waypoints: after the attack they turn for the
    # split point. The standoff flight never comes closer than its launch point.
    assert standoff_names[standoff_names.index("LAUNCH") + 1] == "SPLIT"
    assert min(distance(p) for p in standoff_points) == pytest.approx(
        distance(launch), abs=0.5
    )
    # Flights the built-in planner made keep their ingress attack points.
    for flight in htn_flights:
        group = groups.get(flight.group_id)
        if group is None:
            continue
        names = [str(p.name) for p in group.points]
        assert "LAUNCH" not in names
