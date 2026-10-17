"""Air assault on neutral bases, capture facts and base economy."""

from __future__ import annotations

import json

from game import Game
from game.ato.flighttype import FlightType
from game.claudecommander.view import CommanderView
from game.sim import GameUpdateEvents
from game.config import REWARDS
from game.theater.controlpoint import ControlPoint
from game.theater.player import Player
from game.theater.theatergroundobject import BuildingGroundObject
from tests.claudecommander.test_air_tools import Caller


def neutralize_an_enemy_base(game: Game) -> ControlPoint:
    cp = next(
        cp
        for cp in game.theater.control_points_for(Player.RED)
        if not cp.is_fleet and not cp.has_frontline
    )
    cp.capture(game, GameUpdateEvents(), for_player=Player.NEUTRAL)
    return cp


def test_neutral_bases_are_air_assault_targets(game: Game) -> None:
    game.settings.set_plugin_option("ctld", True)
    cp = neutralize_an_enemy_base(game)
    view = CommanderView(game, Player.BLUE)
    types = view.mission_types_for(cp)
    assert FlightType.AIR_ASSAULT in types and FlightType.ESCORT in types
    assert FlightType.OCA_RUNWAY not in types and FlightType.STRIKE not in types

    caller = Caller(game, Player.BLUE)
    targets = caller.ok("get_mission_targets", {"mission_type": "Air Assault"})
    assert str(cp.id) in json.dumps(targets)

    intel = caller.ok("get_enemy_intel", {"kind": "bases"})
    entry = next(b for b in intel["bases"] if b["id"] == str(cp.id))
    assert entry["capture"]["air_assault_possible"]
    assert entry["capture"]["blocked_by_armor"] == []
    assert "Neutral" in entry["capture"]["note"]


def test_air_assault_needs_ctld(game: Game) -> None:
    game.settings.set_plugin_option("ctld", False)
    cp = neutralize_an_enemy_base(game)
    facts = CommanderView(game, Player.BLUE).capture_facts(cp)
    assert facts["air_assault_possible"] is False
    assert "CTLD" in facts["air_assault_note"]


def test_base_economy_counts_buildings_that_change_hands(game: Game) -> None:
    view = CommanderView(game, Player.BLUE)
    enemy = next(
        cp
        for cp in game.theater.control_points_for(Player.RED)
        if any(
            isinstance(t, BuildingGroundObject) and t.category in REWARDS
            for t in cp.ground_objects
        )
    )
    economy = view.base_economy(enemy, own=False)
    assert (
        economy["value_per_turn"] == economy["base_income"] + economy["building_income"]
    )
    assert economy["facilities"]

    # Partial damage to enemy buildings stays hidden until a building site is
    # destroyed outright.
    building = next(
        t
        for t in enemy.ground_objects
        if isinstance(t, BuildingGroundObject) and t.category in REWARDS
    )
    statics = list(building.statics)
    statics[0].alive = False
    if len(statics) > 1:
        assert view.base_economy(enemy, own=False) == economy
    for static in statics:
        static.alive = False
    after = view.base_economy(enemy, own=False)
    assert after["building_income"] < economy["building_income"]
    assert after["destroyed_building_income"] > 0
