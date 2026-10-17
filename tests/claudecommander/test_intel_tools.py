"""The read-only intel tools on a real generated campaign, for both sides."""

from __future__ import annotations

import json
from typing import Any, Iterator

import pytest

from game import Game
from game.claudecommander.context import CommanderContext
from game.claudecommander.mcpserver import commander_dispatcher
from game.claudecommander.view import (
    CommanderView,
    EnemySighting,
    PACKAGE_IDS,
    nominal_ranges,
)
from game.sim import GameUpdateEvents
from game.theater.player import Player
from game.theater.theatergroundobject import (
    MotorpoolGroundObject,
    TheaterGroundObject,
)

SIDES = [Player.BLUE, Player.RED]

INTEL_TOOLS = {
    "get_situation",
    "get_own_forces",
    "get_enemy_intel",
    "get_front_lines",
    "get_ato",
    "get_mission_targets",
    "lookup_aircraft",
    "lookup_unit",
    "check_route_threats",
    "measure",
}

#: Well under what Claude Code accepts from one MCP tool result (~25k tokens).
MAX_RESPONSE_CHARS = 40_000

#: Keys that only ever describe your own side's dynamic state.
OWN_ONLY_KEYS = {
    "aircraft",
    "ground_units",
    "ground_unit_orders",
    "squadrons",
    "aircraft_owned",
    "aircraft_untasked",
    "units_alive_total",
    "alive",
    "packages",
    "flights",
    "own_stance",
    "own_units_at_base",
    "own_deployable_units",
    "repair_turns_remaining",
    "budget",
    "transfers",
}


def call(
    game: Game,
    player: Player,
    name: str,
    arguments: dict[str, Any],
    sightings: tuple[EnemySighting, ...] = (),
) -> tuple[str, bool]:
    response = commander_dispatcher.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        lambda: CommanderContext(game, player, GameUpdateEvents(), sightings),
    )
    assert isinstance(response, dict)
    result = response["result"]
    return result["content"][0]["text"], result["isError"]


def call_json(
    game: Game, player: Player, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    text, is_error = call(game, player, name, arguments)
    assert not is_error, text
    assert len(text) < MAX_RESPONSE_CHARS, f"{name} returned {len(text)} chars"
    data = json.loads(text)
    assert isinstance(data, dict)
    return data


def keys_in(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from keys_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from keys_in(item)


def own_and_enemy_ids(game: Game, player: Player) -> tuple[set[str], set[str]]:
    def ids(coalition_player: Player) -> set[str]:
        coalition = game.coalition_for(coalition_player)
        found = {str(s.id) for s in coalition.air_wing.iter_squadrons()}
        for package in coalition.ato.packages:
            found.add(PACKAGE_IDS.id_for(package))
            found.update(str(f.id) for f in package.flights)
        return found

    return ids(player), ids(player.opponent)


def enemy_site_with(game: Game, player: Player, min_units: int) -> TheaterGroundObject:
    view = CommanderView(game, player)
    for tgo in view.enemy_ground_objects():
        if (
            tgo.category == "aa"
            and tgo.alive_unit_count == tgo.unit_count >= min_units
            and nominal_ranges(tgo)[0] > 0
        ):
            return tgo
    raise AssertionError("The test campaign has no suitable enemy SAM site")


def representative_calls(
    game: Game, player: Player
) -> list[tuple[str, dict[str, Any]]]:
    view = CommanderView(game, player)
    own_base = view.own_control_points()[0]
    enemy_base = next(game.theater.control_points_for(player.opponent))
    route = [{"id": str(own_base.id)}, {"id": str(enemy_base.id)}]
    calls: list[tuple[str, dict[str, Any]]] = [
        ("get_situation", {}),
        ("get_own_forces", {}),
        ("get_own_forces", {"kind": "ground_objects", "limit": 100}),
        ("get_own_forces", {"kind": "catalogue"}),
        ("get_own_forces", {"kind": "squadrons", "limit": 100}),
        ("get_enemy_intel", {}),
        ("get_enemy_intel", {"kind": "ground_objects", "limit": 100}),
        ("get_enemy_intel", {"kind": "catalogue"}),
        (
            "get_enemy_intel",
            {
                "kind": "ground_objects",
                "near": {"id": str(enemy_base.id)},
                "radius_nm": 30,
                "categories": ["aa", "ewr"],
            },
        ),
        ("get_front_lines", {}),
        ("get_ato", {}),
        ("get_ato", {"include_waypoints": True, "limit": 100}),
        ("get_mission_targets", {}),
        ("get_mission_targets", {"limit": 100, "owner": "any"}),
        ("get_mission_targets", {"mission_type": "DEAD"}),
        ("lookup_aircraft", {"name": "F-16CM Fighting Falcon (Block 50)"}),
        ("lookup_unit", {"name": "SA-11 Buk LN 9A310M1"}),
        ("measure", {"points": route}),
        ("check_route_threats", {"points": route, "altitude_ft": 15000}),
    ]
    packages = view.coalition.ato.packages
    if packages:
        calls.append(("get_ato", {"package_id": PACKAGE_IDS.id_for(packages[0])}))
    return calls


def test_mcp_lists_intel_tools_as_read_only() -> None:
    response = commander_dispatcher.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, lambda: None  # type: ignore[arg-type, return-value]
    )
    assert isinstance(response, dict)
    tools = {t["name"]: t for t in response["result"]["tools"]}
    assert INTEL_TOOLS <= set(tools)
    for name in INTEL_TOOLS:
        assert tools[name]["annotations"] == {"readOnlyHint": True}
        assert tools[name]["inputSchema"]["type"] == "object"


def test_fixture_has_both_atos(test_game: Game) -> None:
    assert test_game.turn == 1
    for player in SIDES:
        coalition = test_game.coalition_for(player)
        assert coalition.ato.packages, f"{player} planned no packages"
        assert list(coalition.air_wing.iter_squadrons())


@pytest.mark.parametrize("player", SIDES, ids=lambda p: p.value)
def test_every_tool_runs_within_size_bounds(test_game: Game, player: Player) -> None:
    for name, arguments in representative_calls(test_game, player):
        call_json(test_game, player, name, arguments)


@pytest.mark.parametrize("player", SIDES, ids=lambda p: p.value)
def test_no_enemy_squadron_package_or_flight_ids_leak(
    test_game: Game, player: Player
) -> None:
    own_ids, enemy_ids = own_and_enemy_ids(test_game, player)
    assert enemy_ids
    seen_own = False
    for name, arguments in representative_calls(test_game, player):
        text, is_error = call(test_game, player, name, arguments)
        assert not is_error, text
        for enemy_id in enemy_ids:
            assert enemy_id not in text, f"{name} leaked enemy id {enemy_id}"
        seen_own = seen_own or any(own_id in text for own_id in own_ids)
    assert seen_own


@pytest.mark.parametrize("player", SIDES, ids=lambda p: p.value)
def test_enemy_intel_has_no_dynamic_fields(test_game: Game, player: Player) -> None:
    for kind in ("overview", "bases", "ground_objects", "front_lines", "catalogue"):
        data = call_json(
            test_game, player, "get_enemy_intel", {"kind": kind, "limit": 100}
        )
        leaked = OWN_ONLY_KEYS & set(keys_in(data))
        assert not leaked, f"get_enemy_intel kind={kind} has {leaked}"


@pytest.mark.parametrize("player", SIDES, ids=lambda p: p.value)
def test_ato_is_own_side_only(test_game: Game, player: Player) -> None:
    data = call_json(test_game, player, "get_ato", {"limit": 100})
    own_squadrons = {
        str(s.id) for s in test_game.coalition_for(player).air_wing.iter_squadrons()
    }
    flights = [f for p in data["packages"] for f in p["flights"]]
    assert flights
    assert {f["squadron_id"] for f in flights} <= own_squadrons
    assert len(data["packages"]) == len(test_game.coalition_for(player).ato.packages)


def test_partial_damage_of_enemy_site_is_hidden(test_game: Game) -> None:
    tgo = enemy_site_with(test_game, Player.BLUE, min_units=2)
    view = CommanderView(test_game, Player.BLUE)
    before = view.enemy_ground_object_json(tgo)
    victim = next(u for u in tgo.units if u.is_anti_air)
    victim.alive = False
    tgo.invalidate_threat_poly()
    try:
        after = view.enemy_ground_object_json(tgo)
        listed = call_json(
            test_game,
            Player.BLUE,
            "get_enemy_intel",
            {"kind": "ground_objects", "near": {"id": str(tgo.id)}, "radius_nm": 0.1},
        )
        own = CommanderView(test_game, Player.RED).own_ground_object_json(tgo)
    finally:
        victim.alive = True
        tgo.invalidate_threat_poly()
    assert after == before
    assert after["status"] == "active"
    assert [g for g in listed["ground_objects"] if g["id"] == str(tgo.id)] == [before]
    # The owner sees the damage.
    assert own["alive"] == tgo.unit_count - 1


def test_destroyed_enemy_site_is_reported_destroyed(test_game: Game) -> None:
    tgo = enemy_site_with(test_game, Player.BLUE, min_units=1)
    view = CommanderView(test_game, Player.BLUE)
    for unit in tgo.units:
        unit.alive = False
    tgo.invalidate_threat_poly()
    try:
        entry = view.enemy_ground_object_json(tgo)
        threat_ids = {t.id for t in view.known_threats()}
        targets = call_json(
            test_game, Player.BLUE, "get_mission_targets", {"limit": 100}
        )
    finally:
        for unit in tgo.units:
            unit.alive = True
        tgo.invalidate_threat_poly()
    assert entry["status"] == "destroyed"
    assert "units" not in entry and "threat_nm" not in entry
    assert str(tgo.id) not in threat_ids
    assert str(tgo.id) not in {t["id"] for t in targets["targets"]}


def test_enemy_motorpool_contents_are_hidden(test_game: Game) -> None:
    view = CommanderView(test_game, Player.BLUE)
    for tgo in view.enemy_ground_objects():
        if isinstance(tgo, MotorpoolGroundObject):
            entry = view.enemy_ground_object_json(tgo)
            assert entry["status"] == "unknown"
            assert not isinstance(entry["units"], dict)


@pytest.mark.parametrize("player", SIDES, ids=lambda p: p.value)
def test_enemy_bases_show_no_forces(test_game: Game, player: Player) -> None:
    data = call_json(test_game, player, "get_enemy_intel", {"kind": "bases"})
    assert data["bases"]
    for base in data["bases"]:
        assert base["owner"] in ("enemy", "neutral")
        assert set(base) <= {
            "id",
            "name",
            "type",
            "owner",
            "x",
            "y",
            "lat",
            "lon",
            "runway",
            "parking",
            "connected_to",
        }
        assert set(base["runway"]) <= {"damaged", "operational"}


@pytest.mark.parametrize("player", SIDES, ids=lambda p: p.value)
def test_own_forces_show_detail(test_game: Game, player: Player) -> None:
    data = call_json(test_game, player, "get_own_forces", {"limit": 100})
    assert data["bases"] and data["squadrons"]
    assert all(b["owner"] == "own" for b in data["bases"])
    assert data["ground_objects_by_base"]
    squadron = data["squadrons"][0]
    assert {"aircraft_owned", "aircraft_untasked", "pilots_available"} <= set(squadron)
    assert squadron["aircraft"] in data["aircraft_task_weights"]
    sites = call_json(
        test_game, player, "get_own_forces", {"kind": "ground_objects", "limit": 5}
    )
    assert len(sites["ground_objects"]) == 5
    assert sites["ground_objects_page"]["next_offset"] == 5
    assert "units_alive_total" in sites["ground_objects"][0]


def test_situation(test_game: Game) -> None:
    data = call_json(test_game, Player.RED, "get_situation", {})
    assert data["side"] == "Red"
    assert data["turn"] == 1
    assert data["victory_state"] == "ongoing"
    assert data["faction"] == test_game.red.faction.name
    assert data["enemy_faction"] == test_game.blue.faction.name
    assert {"surface", "6500ft", "26000ft"} == set(data["weather"]["wind"])
    assert data["budget"] == round(test_game.red.budget, 1)


@pytest.mark.parametrize("player", SIDES, ids=lambda p: p.value)
def test_mission_targets(test_game: Game, player: Player) -> None:
    data = call_json(test_game, player, "get_mission_targets", {"limit": 100})
    targets = data["targets"]
    assert targets
    assert all(t["owner"] in ("enemy", "contested") for t in targets)
    kinds = {t["kind"] for t in targets}
    assert {"base", "ground_object", "front_line"} <= kinds
    dead = call_json(
        test_game, player, "get_mission_targets", {"mission_type": "dead", "limit": 100}
    )
    assert dead["targets"]
    assert "DEAD" in dead["missions_valid_for_all_listed"]
    view = CommanderView(test_game, player)
    for target in dead["targets"]:
        assert view.mission_target(target["id"]).is_friendly(player) is False


def test_mission_targets_rejects_unknown_mission_type(test_game: Game) -> None:
    text, is_error = call(
        test_game, Player.BLUE, "get_mission_targets", {"mission_type": "Nuke"}
    )
    assert is_error
    assert "Valid mission types" in text


def test_lookups(test_game: Game) -> None:
    aircraft = call_json(
        test_game, Player.BLUE, "lookup_aircraft", {"name": "f-15c eagle"}
    )
    assert aircraft["id"] == "F-15C Eagle"
    assert aircraft["loadouts"]
    assert "BARCAP" in aircraft["task_weights"]
    unit = call_json(
        test_game, Player.RED, "lookup_unit", {"name": "SA-11 Buk LN 9A310M1"}
    )
    assert unit["threat_range_nm"] > 10
    assert unit["class"] == "TELAR"
    text, is_error = call(test_game, Player.RED, "lookup_unit", {"name": "SA-10"})
    assert is_error and "several" in text
    text, is_error = call(test_game, Player.RED, "lookup_aircraft", {"name": "zzz"})
    assert is_error


def test_route_threats_find_a_known_site(test_game: Game) -> None:
    tgo = enemy_site_with(test_game, Player.BLUE, min_units=1)
    threat_m, _ = nominal_ranges(tgo)
    start = {"x": tgo.position.x - 3 * threat_m, "y": tgo.position.y}
    end = {"x": tgo.position.x + 3 * threat_m, "y": tgo.position.y}
    data = call_json(
        test_game, Player.BLUE, "check_route_threats", {"points": [start, end]}
    )
    hits = {t["id"]: t for t in data["legs"][0]["threats"]}
    assert str(tgo.id) in hits
    hit = hits[str(tgo.id)]
    assert hit["closest_nm"] == 0.0
    assert hit["entry_nm"] == pytest.approx(2 * threat_m / 1852, abs=0.2)
    # Flying above the threat's slant range clears it.
    high = call_json(
        test_game,
        Player.BLUE,
        "check_route_threats",
        {"points": [start, end], "altitude_ft": threat_m / 0.3048 + 1000},
    )
    assert str(tgo.id) not in {t["id"] for t in high["legs"][0]["threats"]}
    # The owner of the site never sees its own sites as threats.
    own = call_json(
        test_game, Player.RED, "check_route_threats", {"points": [start, end]}
    )
    assert str(tgo.id) not in {t["id"] for t in own["legs"][0]["threats"]}


def test_measure_and_points(test_game: Game) -> None:
    cps = test_game.theater.controlpoints
    a, b = cps[0], cps[1]
    data = call_json(
        test_game,
        Player.BLUE,
        "measure",
        {"points": [{"id": str(a.id)}, {"x": b.position.x, "y": b.position.y}]},
    )
    expected = a.position.distance_to_point(b.position) / 1852
    assert data["total_nm"] == pytest.approx(expected, abs=0.1)
    latlng = b.position.latlng()
    via_ll = call_json(
        test_game,
        Player.BLUE,
        "measure",
        {"points": [{"id": str(a.id)}, {"lat": latlng.lat, "lon": latlng.lng}]},
    )
    assert via_ll["total_nm"] == pytest.approx(expected, abs=0.1)
    text, is_error = call(
        test_game, Player.BLUE, "measure", {"points": [{"id": str(a.id)}, {"x": 1}]}
    )
    assert is_error
    text, is_error = call(
        test_game,
        Player.BLUE,
        "measure",
        {"points": [{"id": str(a.id)}, {"id": "nope"}]},
    )
    assert is_error and "Unknown id" in text


def test_sightings_hook(test_game: Game) -> None:
    sighting = EnemySighting("MiG-29A Fulcrum-A", 1, "shot_down", "Damascus")
    text, is_error = call(
        test_game, Player.BLUE, "get_enemy_intel", {"kind": "sightings"}, (sighting,)
    )
    assert not is_error
    assert json.loads(text)["sightings"] == [
        {
            "aircraft": "MiG-29A Fulcrum-A",
            "turn": 1,
            "how": "shot_down",
            "origin": "Damascus",
        }
    ]
    plain = call_json(test_game, Player.BLUE, "get_enemy_intel", {"kind": "sightings"})
    assert plain["sightings"] == []
