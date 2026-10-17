from __future__ import annotations

import json
from unittest.mock import MagicMock

from game import Game
from game.ato.flight import Flight
from game.claudecommander.debrief import (
    sightings_from_debriefing,
    summarize_debriefing,
)
from game.claudecommander.view import EnemySighting, PACKAGE_IDS
from game.debriefing import AirLosses, Debriefing, GroundLosses
from game.theater.controlpoint import Airfield, ControlPoint
from game.theater.player import Player
from game.theater.theatergroundobject import TheaterGroundObject
from game.unitmap import FlyingUnit, FrontLineUnit, TheaterUnitMapping


def _first_flight(game: Game, player: Player) -> Flight:
    return game.coalition_for(player).ato.packages[0].flights[0]


def _site_owned_by(game: Game, player: Player) -> TheaterGroundObject:
    for cp in game.theater.control_points_for(player):
        for tgo in cp.ground_objects:
            if tgo.category == "aa" and tgo.units:
                return tgo
    raise AssertionError(f"No {player} SAM site")


def _blue_strike_target(game: Game) -> TheaterGroundObject:
    for package in game.blue.ato.packages:
        if isinstance(package.target, TheaterGroundObject) and any(
            True for _ in package.target.units
        ):
            return package.target
    return _site_owned_by(game, Player.RED)


def _front_cps(game: Game) -> tuple[ControlPoint, ControlPoint]:
    front = next(game.theater.conflicts())
    return front.blue_cp, front.red_cp


def _make_debriefing(game: Game) -> tuple[Debriefing, TheaterGroundObject, Airfield]:
    blue_flight = _first_flight(game, Player.BLUE)
    red_flight = _first_flight(game, Player.RED)
    blue_cp, red_cp = _front_cps(game)
    blue_unit_type = next(iter(game.blue.faction.frontline_units))
    red_unit_type = next(iter(game.red.faction.frontline_units))
    red_target = _blue_strike_target(game)
    blue_site = _site_owned_by(game, Player.BLUE)
    red_airfield = next(
        cp
        for cp in game.theater.control_points_for(Player.RED)
        if isinstance(cp, Airfield)
    )

    debriefing = Debriefing.__new__(Debriefing)
    debriefing.game = game
    debriefing.air_losses = AirLosses(
        player=[FlyingUnit(blue_flight, None)],
        enemy=[FlyingUnit(red_flight, None), FlyingUnit(red_flight, None)],
    )
    debriefing.ground_losses = GroundLosses(
        player_front_line=[FrontLineUnit(blue_unit_type, blue_cp)],
        enemy_front_line=[FrontLineUnit(red_unit_type, red_cp)] * 3,
        player_ground_objects=[
            TheaterUnitMapping(next(iter(blue_site.units)), MagicMock())
        ],
        enemy_ground_objects=[
            TheaterUnitMapping(next(iter(red_target.units)), MagicMock())
        ],
        enemy_airfields=[red_airfield],
    )
    debriefing.base_captures = []
    return debriefing, red_target, red_airfield


def test_blue_summary_splits_own_and_enemy(test_game: Game) -> None:
    debriefing, red_target, red_airfield = _make_debriefing(test_game)
    blue_flight = _first_flight(test_game, Player.BLUE)
    red_flight = _first_flight(test_game, Player.RED)

    summary = summarize_debriefing(debriefing, Player.BLUE)

    own = summary["own_losses"]
    assert own["aircraft"]["total"] == 1
    assert own["aircraft"]["by_type"] == {blue_flight.unit_type.variant_id: 1}
    assert own["aircraft"]["by_task"] == {blue_flight.flight_type.value: 1}
    assert sum(own["front_line_units"].values()) == 1
    assert len(own["sites"]) == 1
    assert own["runways_hit"] == []

    enemy = summary["confirmed_enemy_losses"]
    assert enemy["aircraft"] == [
        {
            "aircraft": red_flight.unit_type.variant_id,
            "origin": red_flight.departure.name,
            "count": 2,
        }
    ]
    assert sum(enemy["front_line_units"].values()) == 3
    assert [s["id"] for s in enemy["sites"]] == [str(red_target.id)]
    assert enemy["runways_hit"] == [red_airfield.name]
    assert summary["enemy_aircraft_types_encountered"] == [
        red_flight.unit_type.variant_id
    ]

    packages = summary["packages"]
    assert len(packages) == len(test_game.blue.ato.packages)
    blue_ids = {PACKAGE_IDS.id_for(p) for p in test_game.blue.ato.packages}
    assert {p["package_id"] for p in packages} == blue_ids
    lost = sum(f["lost"] for p in packages for f in p["flights"])
    assert lost == 1
    for package, result in zip(test_game.blue.ato.packages, packages):
        if package.target is red_target:
            assert result["confirmed_damage_at_target"] >= 1
    json.dumps(summary)


def test_red_summary_is_the_mirror(test_game: Game) -> None:
    debriefing, _, red_airfield = _make_debriefing(test_game)
    blue_flight = _first_flight(test_game, Player.BLUE)

    summary = summarize_debriefing(debriefing, Player.RED)

    assert summary["own_losses"]["aircraft"]["total"] == 2
    assert summary["own_losses"]["runways_hit"] == [red_airfield.name]
    assert sum(summary["own_losses"]["front_line_units"].values()) == 3
    assert summary["confirmed_enemy_losses"]["aircraft"] == [
        {
            "aircraft": blue_flight.unit_type.variant_id,
            "origin": blue_flight.departure.name,
            "count": 1,
        }
    ]
    red_ids = {PACKAGE_IDS.id_for(p) for p in test_game.red.ato.packages}
    assert {p["package_id"] for p in summary["packages"]} == red_ids
    blue_ids = {PACKAGE_IDS.id_for(p) for p in test_game.blue.ato.packages}
    text = json.dumps(summary)
    for package_id in blue_ids:
        assert package_id not in text


def test_sightings_from_debriefing(test_game: Game) -> None:
    debriefing, _, _ = _make_debriefing(test_game)
    red_flight = _first_flight(test_game, Player.RED)
    assert sightings_from_debriefing(debriefing, Player.BLUE, 3) == [
        EnemySighting(
            red_flight.unit_type.variant_id,
            3,
            "shot_down",
            red_flight.departure.name,
        )
    ]
