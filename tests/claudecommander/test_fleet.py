"""Moving carriers, LHAs and naval groups."""

from __future__ import annotations

from dcs.mapping import LatLng, Point

from game import Game
from game.theater import ControlPoint
from game.theater.controlpoint import Carrier
from game.theater.player import Player
from game.utils import Heading, nautical_miles
from tests.claudecommander.test_air_tools import Caller


def own_carrier(game: Game, player: Player) -> ControlPoint:
    for cp in game.theater.control_points_for(player):
        if cp.is_fleet:
            return cp
    # The test campaign's factions have no carriers; put one off Haifa.
    at = Point.from_latlng(LatLng(32.95, 34.55), game.theater.terrain)
    carrier = Carrier("Test Carrier", at, game.theater, player)
    carrier.finish_init(game)
    game.theater.add_controlpoint(carrier)
    return carrier


def sea_point(game: Game, origin: Point, distance_nm: float) -> Point:
    landmap = game.theater.landmap
    assert landmap is not None
    for bearing in range(0, 360, 15):
        point = origin.point_from_heading(
            Heading.from_degrees(bearing).degrees, nautical_miles(distance_nm).meters
        )
        if game.theater.is_in_sea(point) and not landmap.land_inbetween(origin, point):
            return point
    raise AssertionError("No open sea around the carrier")


def test_get_fleet_lists_carriers(game: Game) -> None:
    carrier = own_carrier(game, Player.BLUE)
    fleet = Caller(game, Player.BLUE).ok("get_fleet", {})["fleet"]
    entry = next(f for f in fleet if f["id"] == str(carrier.id))
    assert entry["kind"] in ("carrier", "lha")
    assert entry["max_move_nm"] == 80
    assert "moving_to" not in entry


def test_move_fleet_checks_range_and_water(game: Game) -> None:
    caller = Caller(game, Player.BLUE)
    carrier = own_carrier(game, Player.BLUE)
    unit_id = str(carrier.id)

    far = sea_point(game, carrier.position, 120)
    text = caller.error(
        "move_fleet",
        {"unit_id": unit_id, "destination": {"x": far.x, "y": far.y}},
    )
    assert "at most 80 nm" in text

    inland = min(
        (cp for cp in game.theater.controlpoints if not cp.is_fleet),
        key=lambda cp: cp.position.distance_to_point(carrier.position),
    )
    assert carrier.destination_in_range(inland.position)
    text = caller.error(
        "move_fleet", {"unit_id": unit_id, "destination": {"id": str(inland.id)}}
    )
    assert "open sea" in text or "Land lies between" in text
    assert not any("moving_to" in u for u in caller.ok("get_fleet", {})["fleet"])

    near = sea_point(game, carrier.position, 30)
    result = caller.ok(
        "move_fleet",
        {"unit_id": unit_id, "destination": {"x": near.x, "y": near.y}},
    )
    assert carrier.target_position is not None
    assert 29 <= result["distance_nm"] <= 31
    assert "moving_to" in result

    caller.ok("move_fleet", {"unit_id": unit_id})
    assert carrier.target_position is None


def test_enemy_ships_cannot_be_moved(game: Game) -> None:
    carrier = own_carrier(game, Player.BLUE)
    near = sea_point(game, carrier.position, 30)
    text = Caller(game, Player.RED).error(
        "move_fleet",
        {"unit_id": str(carrier.id), "destination": {"x": near.x, "y": near.y}},
    )
    assert "No carrier, LHA or naval group of yours" in text
    assert carrier.target_position is None
