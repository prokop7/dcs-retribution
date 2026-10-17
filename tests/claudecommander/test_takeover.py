"""Claude taking over a side, and the campaign's opening turns."""

from __future__ import annotations

from game import Game
from game.claudecommander.prompts import RunBrief, RunKind, run_message
from game.claudecommander.tools.ato import locked_package_descriptions
from game.sim import GameUpdateEvents
from game.theater.player import Player
from tests.claudecommander.test_air_tools import Caller, own_airbase


def test_hand_over_clears_built_in_plan_and_refunds_its_orders(game: Game) -> None:
    red = game.red
    packages = list(red.ato.packages)
    assert packages, "The test game should have a built-in RED plan"
    packages[0].player_locked = True
    squadron = next(red.air_wing.iter_squadrons())
    budget = red.budget
    squadron.pending_deliveries += 2
    red.budget -= 2 * squadron.aircraft.price
    red.commander.planned_turn = game.turn

    red.hand_over_to_claude(GameUpdateEvents())

    assert red.ato.packages == [packages[0]]
    assert locked_package_descriptions(red)
    # Every order the built-in procurement placed this turn is refunded, including
    # the two aircraft added above.
    assert all(s.pending_deliveries == 0 for s in red.air_wing.iter_squadrons())
    assert all(
        not any(cp.ground_unit_orders.units.values())
        for cp in game.theater.control_points_for(Player.RED)
    )
    assert red.budget >= budget
    assert red.commander.planned_turn == game.turn - 1


def test_hand_over_keeps_player_orders_in_player_domains(game: Game) -> None:
    blue = game.blue
    game.settings.automate_aircraft_reinforcements = False
    squadron = next(blue.air_wing.iter_squadrons())
    squadron.pending_deliveries += 1
    blue.hand_over_to_claude(GameUpdateEvents())
    assert squadron.pending_deliveries >= 1


def test_ground_war_is_replanned_for_current_stances(game: Game) -> None:
    before = dict(game.ground_planners)
    game.red.replan_ground_war()
    replanned = [
        cp_id
        for cp_id, planner in game.ground_planners.items()
        if before.get(cp_id) is not planner
    ]
    assert replanned
    assert all(
        game.theater.find_control_point_by_id(cp_id).captured is Player.RED
        for cp_id in replanned
    )


def test_no_packages_on_turn_zero(game: Game) -> None:
    game.turn = 0
    caller = Caller(game, Player.BLUE)
    text = caller.error(
        "create_package",
        {
            "target_id": str(own_airbase(game, Player.BLUE).id),
            "flights": [{"task": "BARCAP", "count": 2}],
        },
    )
    assert "Turn 0 has no mission" in text


def test_opening_turn_messages() -> None:
    first_mission = run_message(RunBrief(RunKind.TURN, 1))
    assert "No mission has been flown yet" in first_mission
    skipped = run_message(RunBrief(RunKind.TURN, 4))
    assert "skipped" in skipped
    flown = run_message(RunBrief(RunKind.TURN, 4, debrief={"own_losses": {}}))
    assert "The previous mission has been flown" in flown
