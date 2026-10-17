"""Procurement, logistics, repair and memory tools, and BLUE domain gating."""

from __future__ import annotations

import pytest

from game import Game
from game.config import RUNWAY_REPAIR_COST
from game.ground_forces.combat_stance import CombatStance
from game.squadrons import Squadron
from game.theater import ControlPoint, ParkingType
from game.theater.player import Player
from game.theater.theatergroundobject import (
    BuildingGroundObject,
    SamGroundObject,
    TheaterGroundObject,
)
from tests.claudecommander.test_air_tools import Caller, clear_ato, own_airbase


def buyable_squadron(game: Game, player: Player) -> Squadron:
    for squadron in game.coalition_for(player).air_wing.iter_squadrons():
        parking = ParkingType().from_squadron(squadron)
        if squadron.location.unclaimed_parking(
            parking
        ) >= 2 and squadron.has_aircraft_capacity_for(2):
            return squadron
    raise AssertionError("No squadron with room for more aircraft")


def own_site(game: Game, player: Player, kind: type) -> TheaterGroundObject:
    for cp in game.theater.control_points_for(player):
        for tgo in cp.ground_objects:
            if isinstance(tgo, kind) and tgo.units and not tgo.is_dead:
                if not isinstance(tgo, BuildingGroundObject) or tgo.repair_cost() > 0:
                    return tgo
    raise AssertionError(f"No own {kind.__name__}")


def own_building(game: Game, player: Player) -> BuildingGroundObject:
    building = own_site(game, player, BuildingGroundObject)
    assert isinstance(building, BuildingGroundObject)
    return building


def recruiting_base(game: Game, player: Player) -> ControlPoint:
    for cp in game.theater.control_points_for(player):
        if cp.has_ground_unit_source(game):
            return cp
    raise AssertionError("No base can receive ground units")


def test_buy_and_cancel_aircraft(game: Game) -> None:
    caller = Caller(game, Player.BLUE)
    coalition = game.blue
    coalition.budget = 1000
    squadron = buyable_squadron(game, Player.BLUE)
    price = squadron.aircraft.price
    pending = squadron.pending_deliveries

    data = caller.ok("buy_aircraft", {"squadron_id": str(squadron.id), "count": 2})
    assert data["bought"] == 2
    assert squadron.pending_deliveries == pending + 2
    assert coalition.budget == pytest.approx(1000 - 2 * price)
    assert data["budget_remaining"] == round(coalition.budget, 1)
    assert squadron.location in caller.events.updated_control_points

    data = caller.ok(
        "cancel_aircraft_order", {"squadron_id": str(squadron.id), "count": 1}
    )
    assert squadron.pending_deliveries == pending + 1
    assert coalition.budget == pytest.approx(1000 - price)
    text = caller.error(
        "cancel_aircraft_order", {"squadron_id": str(squadron.id), "count": 5}
    )
    assert "on order" in text

    coalition.budget = price * 1.5
    text = caller.error("buy_aircraft", {"squadron_id": str(squadron.id), "count": 2})
    assert "not enough budget" in text and "Nothing was bought" in text
    assert squadron.pending_deliveries == pending + 1
    assert coalition.budget == pytest.approx(price * 1.5)


def test_buy_ground_units(game: Game) -> None:
    caller = Caller(game, Player.RED)
    coalition = game.red
    coalition.budget = 500
    cp = recruiting_base(game, Player.RED)
    unit = next(iter(coalition.faction.frontline_units))
    data = caller.ok(
        "buy_ground_units",
        {"control_point_id": str(cp.id), "unit_type": unit.variant_id, "count": 3},
    )
    assert data["on_order"] == cp.ground_unit_orders.pending_orders(unit) >= 3
    assert coalition.budget == pytest.approx(500 - 3 * unit.price)
    caller.ok(
        "cancel_ground_unit_order",
        {"control_point_id": str(cp.id), "unit_type": unit.variant_id, "count": 3},
    )
    assert coalition.budget == pytest.approx(500)

    coalition.budget = 0
    text = caller.error(
        "buy_ground_units",
        {"control_point_id": str(cp.id), "unit_type": unit.variant_id, "count": 1},
    )
    assert "you have 0" in text
    text = caller.error(
        "buy_ground_units",
        {"control_point_id": str(cp.id), "unit_type": "F-16C", "count": 1},
    )
    assert "ground unit of your faction" in text


def test_blue_domains_follow_hq_automation(game: Game) -> None:
    caller = Caller(game, Player.BLUE)
    game.blue.budget = 1000
    game.red.budget = 1000
    game.settings.automate_aircraft_reinforcements = False
    game.settings.automate_front_line_stance = False
    squadron = buyable_squadron(game, Player.BLUE)
    text = caller.error("buy_aircraft", {"squadron_id": str(squadron.id), "count": 1})
    assert "The player controls aircraft purchases" in text
    assert "automate_aircraft_reinforcements" in text
    front = next(iter(game.theater.conflicts()))
    text = caller.error(
        "set_front_line_stance",
        {"front_line_id": str(front.id), "stance": "AGGRESSIVE"},
    )
    assert "front line stances" in text
    domains = caller.ok("get_situation", {})["your_domains"]
    assert "air_tasking" in domains and "transfers" in domains
    assert "aircraft_purchase" not in domains

    red = Caller(game, Player.RED)
    red_squadron = buyable_squadron(game, Player.RED)
    red.ok("buy_aircraft", {"squadron_id": str(red_squadron.id), "count": 1})
    assert "aircraft_purchase" in red.ok("get_situation", {})["your_domains"]

    game.settings.automate_aircraft_reinforcements = True
    caller.ok("buy_aircraft", {"squadron_id": str(squadron.id), "count": 1})


def test_front_line_stance(game: Game) -> None:
    caller = Caller(game, Player.RED)
    front = next(iter(game.theater.conflicts()))
    data = caller.ok(
        "set_front_line_stance",
        {"front_line_id": str(front.id), "stance": "breakthrough"},
    )
    own = front.control_point_friendly_to(Player.RED)
    enemy = front.control_point_hostile_to(Player.RED)
    assert own.stances[enemy.id] is CombatStance.BREAKTHROUGH
    assert data["stance"] == "BREAKTHROUGH"
    assert front in caller.events.updated_front_lines
    text = caller.error(
        "set_front_line_stance", {"front_line_id": str(front.id), "stance": "charge"}
    )
    assert "AMBUSH" in text


def test_repair_runway(game: Game) -> None:
    caller = Caller(game, Player.BLUE)
    game.settings.automate_runway_repair = True
    base = own_airbase(game, Player.BLUE)
    game.blue.budget = 500
    text = caller.error("repair_runway", {"control_point_id": str(base.id)})
    assert "not damaged" in text
    base.runway_status.damage()
    caller.ok("repair_runway", {"control_point_id": str(base.id)})
    assert base.runway_status.repair_turns_remaining is not None
    assert game.blue.budget == pytest.approx(500 - RUNWAY_REPAIR_COST)
    assert "already being repaired" in caller.error(
        "repair_runway", {"control_point_id": str(base.id)}
    )


def test_repair_site_and_building(game: Game) -> None:
    caller = Caller(game, Player.RED)
    game.red.budget = 5000
    sam = own_site(game, Player.RED, SamGroundObject)
    dead = [u for u in sam.units if u.repairable][:2]
    for unit in dead:
        unit.alive = False
    sites = caller.ok("get_own_forces", {"kind": "ground_objects", "limit": 100})
    entry = next(s for s in sites["ground_objects"] if s["id"] == str(sam.id))
    assert {d["unit"] for d in entry["dead_units"]} == {u.name for u in dead}

    text = caller.error(
        "repair_site", {"tgo_id": str(sam.id), "unit_ids": ["no such unit"]}
    )
    assert "Repairable dead units" in text
    data = caller.ok("repair_site", {"tgo_id": str(sam.id), "unit_ids": [dead[0].name]})
    price = dead[0].unit_type.price if dead[0].unit_type else 0
    assert data["cost"] == price
    assert game.red.budget == pytest.approx(5000 - price)
    turns = game.settings.ground_object_repair_turns
    if turns:
        assert dead[0].repair_turns_remaining == turns
    else:
        assert dead[0].alive
    assert sam in caller.events.updated_tgos
    caller.ok("repair_site", {"tgo_id": str(sam.id)})
    assert "no dead units" in caller.error("repair_site", {"tgo_id": str(sam.id)})

    building = own_building(game, Player.RED)
    static = next(iter(building.statics))
    static.alive = False
    budget = game.red.budget
    data = caller.ok("repair_building", {"tgo_id": str(building.id)})
    assert data["buildings"] == 1
    assert game.red.budget == pytest.approx(budget - building.repair_cost())
    assert "is not a building site" in caller.error(
        "repair_building", {"tgo_id": str(sam.id)}
    )


def test_buy_or_rebuild_site(game: Game) -> None:
    caller = Caller(game, Player.RED)
    game.red.budget = 5000
    sam = own_site(game, Player.RED, SamGroundObject)
    options = caller.ok("get_site_options", {"tgo_id": str(sam.id)})
    assert options["force_groups"]
    force_group = options["force_groups"][0]["force_group"]
    detail = caller.ok(
        "get_site_options", {"tgo_id": str(sam.id), "force_group": force_group}
    )
    layout = detail["force_groups"][0]["layouts"][0]
    refund = options["refund_for_current_units"]

    data = caller.ok(
        "buy_or_rebuild_site",
        {"tgo_id": str(sam.id), "force_group": force_group, "layout": layout["layout"]},
    )
    assert data["refund"] == refund
    assert game.red.budget == pytest.approx(5000 - data["price"] + refund)
    assert sam.units
    activation = game.settings.purchased_ground_object_activation_turns
    if activation:
        assert data["in_service_in_turns"] == activation
        assert all(
            u.repair_turns_remaining == activation for u in sam.units if u.repairable
        )
    assert sam in caller.events.updated_tgos

    text = caller.error(
        "buy_or_rebuild_site",
        {"tgo_id": str(sam.id), "force_group": "no such group"},
    )
    assert "Available:" in text
    # Rebuilding the same group again is refunded in full (the new units are still
    # in their activation countdown), so it costs nothing net; any debt blocks it.
    game.red.budget = -1
    text = caller.error(
        "buy_or_rebuild_site", {"tgo_id": str(sam.id), "force_group": force_group}
    )
    assert "you have -1" in text


def test_transfer_ground_units(game: Game) -> None:
    caller = Caller(game, Player.BLUE)
    network = game.transit_network_for(Player.BLUE)
    bases = [cp for cp in game.theater.control_points_for(Player.BLUE)]
    pairs = [
        (a, b)
        for a in bases
        for b in bases
        if a != b
        and b.can_deploy_ground_units
        and a.base.total_armor
        and network.has_path_between(a, b)
    ]
    assert pairs, "The test campaign has no ground supply route"
    origin, destination = pairs[0]
    unit, available = next((u, n) for u, n in origin.base.armor.items() if n)
    count = min(2, available)
    data = caller.ok(
        "transfer_ground_units",
        {
            "origin_id": str(origin.id),
            "destination_id": str(destination.id),
            "units": {unit.variant_id: count},
        },
    )
    assert data["units"] == {unit.variant_id: count}
    assert origin.base.total_units_of_type(unit) == available - count
    assert game.blue.transfers.pending_transfer_count >= 1
    text = caller.error(
        "transfer_ground_units",
        {
            "origin_id": str(origin.id),
            "destination_id": str(destination.id),
            "units": {unit.variant_id: 10_000},
        },
    )
    # The generated campaign sometimes leaves the origin empty after the first
    # transfer; both refusals are correct.
    assert "cannot transfer" in text or "has no ground units" in text


def test_relocate_squadron(game: Game) -> None:
    clear_ato(game, Player.BLUE)
    caller = Caller(game, Player.BLUE)
    helicopters = [
        s
        for s in game.blue.air_wing.iter_squadrons()
        if s.aircraft.helicopter and s.owned_aircraft
    ]
    squadron = helicopters[0]
    origin = squadron.location
    destination = next(
        cp
        for cp in game.theater.control_points_for(Player.BLUE)
        if cp != origin and squadron.operates_from(cp)
    )
    data = caller.ok(
        "relocate_squadron",
        {"squadron_id": str(squadron.id), "destination_id": str(destination.id)},
    )
    assert data["relocating_to"] == destination.name
    assert squadron.destination == destination
    assert data["ferry_flights"]
    assert caller.events.new_flights

    data = caller.ok(
        "relocate_squadron",
        {"squadron_id": str(squadron.id), "destination_id": str(origin.id)},
    )
    assert data["relocating_to"] is None
    assert caller.events.deleted_flights
    text = caller.error(
        "relocate_squadron",
        {"squadron_id": str(squadron.id), "destination_id": str(origin.id)},
    )
    assert "already based" in text


def test_journal_and_finish(game: Game) -> None:
    caller = Caller(game, Player.RED)
    caller.ok("write_journal", {"title": "Intent", "text": "Take Golan South."})
    caller.ok("write_journal", {"title": "Note", "text": "Watch the F-15Cs."})
    data = caller.ok("read_journal", {})
    assert "Take Golan South." in data["journal"]
    assert data["entries"] >= 2
    data = caller.ok("read_journal", {"from_turn": game.turn + 1})
    assert data["entries"] == 0
    blue = Caller(game, Player.BLUE).ok("read_journal", {})
    assert "Take Golan South." not in blue["journal"]

    result = caller.ok("finish_turn_planning", {"summary": "Done."})
    assert result["ok"] is True
    assert game.red.commander.finished_turn == game.turn
    assert "issues" in result["air_plan"]


def test_commanders_pay_for_site_rebuilds_on_turn_zero(game: Game) -> None:
    """Turn 0 rebuilds are free for the player's campaign setup, never for Claude."""
    game.turn = 0
    caller = Caller(game, Player.RED)
    sam = own_site(game, Player.RED, SamGroundObject)
    # Nothing left to refund, so any rebuild costs its full price.
    for unit in sam.units:
        unit.alive = False
        unit.repair_turns_remaining = None
    options = caller.ok("get_site_options", {"tgo_id": str(sam.id)})
    assert "free_on_turn_0" not in options
    assert options["refund_for_current_units"] == 0
    force_group = options["force_groups"][0]["force_group"]
    refund = 0

    game.red.budget = 0
    text = caller.error(
        "buy_or_rebuild_site", {"tgo_id": str(sam.id), "force_group": force_group}
    )
    assert "you have 0" in text

    game.red.budget = 5000
    data = caller.ok(
        "buy_or_rebuild_site", {"tgo_id": str(sam.id), "force_group": force_group}
    )
    assert data["cost"] == data["price"] - refund
    assert game.red.budget == pytest.approx(5000 - data["price"] + refund)


def test_commanders_cannot_reshape_setup_only_sites(game: Game) -> None:
    game.turn = 0
    for player in (Player.RED, Player.BLUE):
        caller = Caller(game, player)
        for tgo in game.theater.ground_objects:
            if tgo.control_point.captured is player and tgo.category in (
                "ship",
                "missile",
                "coastal",
            ):
                text = caller.error("get_site_options", {"tgo_id": str(tgo.id)})
                assert "cannot be rebuilt by a commander" in text
                return
    pytest.skip("The test campaign has no ship, missile or coastal site")
