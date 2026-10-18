"""AI SEAD tasking in the generated mission."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dcs.task import WeaponType as DcsWeaponType

from game import Game
from game.data.weapons import WeaponType
from game.theater import TheaterGroundObject
from game.theater.player import Player
from game.theater.theatergroundobject import SamGroundObject
from tests.claudecommander.test_air_tools import Caller, clear_ato, package_by_id
from tests.claudecommander.test_standoff import F16, _miz_groups


def enemy_sam(game: Game) -> TheaterGroundObject:
    for cp in game.theater.control_points_for(Player.RED):
        for tgo in sorted(cp.ground_objects, key=lambda t: t.name):
            if isinstance(tgo, SamGroundObject) and not tgo.is_dead:
                return tgo
    raise AssertionError("No enemy SAM")


def test_harm_shooters_wait_for_the_radar(game: Game, tmp_path: Path) -> None:
    from game.sim.gameloop import GameLoop
    from game.sim.gameupdatecallbacks import GameUpdateCallbacks

    clear_ato(game, Player.BLUE)
    sam = enemy_sam(game)
    data = Caller(game, Player.BLUE).ok(
        "create_package",
        {
            "target_id": str(sam.id),
            "flights": [{"task": "SEAD", "count": 2, "aircraft_type": F16}],
            "minutes_after_start": 60,
        },
    )
    (flight,) = package_by_id(game, Player.BLUE, data["package"]["id"]).flights
    assert flight.any_member_has_weapon_of_type(WeaponType.ARM)

    out = tmp_path / "sead.miz"
    GameLoop(
        game, GameUpdateCallbacks(lambda: None, lambda e: None)
    ).pause_and_generate_miz(out)
    points = _miz_groups(out)[flight.group_id].points
    ingress = next(p for p in points if str(p.name) == "INGRESS")
    tasks: list[dict[str, Any]] = [t.dict() for t in ingress.tasks]

    engage = [t for t in tasks if t["id"] == "EngageGroup"]
    assert engage, tasks
    assert all(t["params"]["weaponType"] == DcsWeaponType.ARM.value for t in engage)
    # A one-shot attack on a dark radar is abandoned, and bombs would take the
    # flight into the live SAM: only decoys are used as an attack.
    attacks = [t for t in tasks if t["id"] == "AttackGroup"]
    assert all(t["params"]["weaponType"] == DcsWeaponType.Decoy.value for t in attacks)
    assert len(engage) == len(attacks) == len({t["params"]["groupId"] for t in engage})
