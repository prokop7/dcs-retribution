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


def test_losses_are_attributed_to_what_killed_them(test_game: Game) -> None:
    from types import SimpleNamespace

    from game.claudecommander.debrief import debrief_markdown
    from game.debriefing import KillRecord

    debriefing, _target, _airfield = _make_debriefing(test_game)
    blue_flight = _first_flight(test_game, Player.BLUE)
    red_flight = _first_flight(test_game, Player.RED)
    blue_cp, red_cp = _front_cps(test_game)
    units = {
        "blue-1": FlyingUnit(blue_flight, None),
        "red-1": FlyingUnit(red_flight, None),
    }
    debriefing.unit_map = SimpleNamespace(flight=units.get)  # type: ignore[assignment]
    debriefing.state_data = SimpleNamespace(  # type: ignore[assignment]
        kill_records=[
            KillRecord(
                target="blue-1",
                killer="red-1",
                killer_type="whatever",
                weapon="R-77",
                x=red_cp.position.x,
                z=red_cp.position.y,
                alt=6000,
                time=1380,
            ),
            KillRecord(target="red-1", killer="blue-1", target_type="MiG-29A"),
        ]
    )
    summary = summarize_debriefing(debriefing, Player.BLUE)
    attribution = summary["loss_attribution"]
    assert attribution["losses_by_cause"] == {"enemy aircraft": 1}
    [loss] = attribution["aircraft_lost"]
    assert loss["killed_by"] == f"{red_flight.unit_type.variant_id} (aircraft)"
    assert loss["weapon"] == "R-77"
    assert red_cp.name in loss["where"]
    assert loss["minutes_after_start"] == 23
    assert attribution["kills_by_own_aircraft"] == [
        {
            "by": blue_flight.unit_type.variant_id,
            "killed": red_flight.unit_type.variant_id,
            "count": 1,
        }
    ]

    text = debrief_markdown(summary)
    assert "{" not in text
    assert "killed by" in text and "R-77" in text


def test_missions_without_attribution_say_so(test_game: Game) -> None:
    debriefing, _target, _airfield = _make_debriefing(test_game)
    summary = summarize_debriefing(debriefing, Player.BLUE)
    attribution = summary["loss_attribution"]
    assert not attribution["attribution_available"]
    assert attribution["losses_by_cause"] == {
        "not attributed (crash, fuel or simulated combat)": 1
    }


def test_kill_records_parse_from_state() -> None:
    from game.debriefing import KillRecord

    record = KillRecord.from_json(
        {"target": "a", "killer_type": "MiG-29A", "x": 1, "z": 2, "time": 60.0}
    )
    assert record is not None and record.killer_type == "MiG-29A" and record.z == 2
    assert KillRecord.from_json("just a name") is None
    assert KillRecord.from_json({"killer": "x"}) is None


def _unit_label(mapping: TheaterUnitMapping) -> str:
    unit_type = mapping.theater_unit.unit_type
    assert unit_type is not None
    return unit_type.variant_id


def _weapon_feed_debriefing(
    game: Game,
) -> tuple[Debriefing, TheaterUnitMapping, TheaterUnitMapping]:
    """A debriefing whose mission recorded a weapon feed.

    BLUE fires 4 HARMs at a RED SAM: 1 hits, 2 are intercepted by the SAM, 1
    misses. RED's SAM fires 3 missiles at the BLUE jet (1 hits) and RED's
    fighter 2 R-77s (1 hits); RED's fighter also fires a Kh-29L at a BLUE
    tank. BLUE's SAM fires at the RED fighter twice (1 hits) and once at an
    R-77, which it intercepts.
    """
    from types import SimpleNamespace

    from game.debriefing import (
        HitRecord,
        ShotRecord,
        WeaponEngagement,
        WeaponFate,
    )

    debriefing, _target, _airfield = _make_debriefing(game)
    blue_flight = _first_flight(game, Player.BLUE)
    red_flight = _first_flight(game, Player.RED)
    red_sam = TheaterUnitMapping(
        next(u for u in _site_owned_by(game, Player.RED).units if u.unit_type),
        MagicMock(),
    )
    blue_sam = TheaterUnitMapping(
        next(u for u in _site_owned_by(game, Player.BLUE).units if u.unit_type),
        MagicMock(),
    )
    aircraft = {
        "blue-1": FlyingUnit(blue_flight, None),
        "red-1": FlyingUnit(red_flight, None),
    }
    sites = {"red-sam-1": red_sam, "blue-sam-1": blue_sam}
    debriefing.unit_map = SimpleNamespace(  # type: ignore[assignment]
        flight=aircraft.get, theater_units=sites.get
    )

    def harm(shot_id: int) -> ShotRecord:
        return ShotRecord(
            id=shot_id,
            shooter="blue-1",
            shooter_type="FA-18C_hornet",
            shooter_coalition=2,
            weapon="AGM_88",
            display_name="AGM-88C",
            target="red-sam-1",
            target_type="Tor 9A331",
            target_coalition=1,
            range_m=46300,
        )

    def red_shot(
        shot_id: int, shooter: str, weapon: str, target: str = "blue-1"
    ) -> ShotRecord:
        return ShotRecord(
            id=shot_id,
            shooter=shooter,
            shooter_type="red type",
            shooter_coalition=1,
            weapon=weapon,
            target=target,
            target_type="T-72B" if target == "blue-tank" else "FA-18C_hornet",
            target_coalition=2,
        )

    tor = WeaponEngagement(
        shot=5, shooter="red-sam-1", shooter_type="Tor 9A331", weapon="9M338"
    )
    shots = [harm(1), harm(2), harm(3), harm(4)]
    shots += [
        ShotRecord(
            id=5 + n,
            shooter="red-sam-1",
            shooter_coalition=1,
            weapon="9M338",
            target_is_weapon=True,
            target_shot=2 + n,
        )
        for n in range(2)
    ]
    shots += [red_shot(7 + n, "red-sam-1", "9M338") for n in range(3)]
    shots += [red_shot(10 + n, "red-1", "R-77") for n in range(2)]
    shots += [red_shot(12, "red-1", "Kh-29L", target="blue-tank")]
    shots += [
        ShotRecord(id=13 + n, shooter="blue-sam-1", weapon="5V55", target="red-1")
        for n in range(2)
    ]
    shots += [
        ShotRecord(
            id=15,
            shooter="blue-sam-1",
            weapon="5V55",
            target_is_weapon=True,
            target_shot=11,
        )
    ]
    hits = [
        HitRecord(shot=1, target="red-sam-1", target_type="Tor 9A331"),
        HitRecord(shot=10, target="blue-1"),
        HitRecord(shot=13, target="red-1"),
    ]
    fates = [
        WeaponFate(shot=1, fate="hit"),
        WeaponFate(shot=2, fate="intercepted", intercepted_by=tor, engaged_by=(tor,)),
        WeaponFate(
            shot=3,
            fate="intercepted",
            intercepted_by=WeaponEngagement(shot=6, shooter="red-sam-1"),
        ),
        WeaponFate(shot=4, fate="no_hit"),
        WeaponFate(shot=7, fate="hit"),
        WeaponFate(shot=8, fate="no_hit"),
        WeaponFate(
            shot=11,
            fate="intercepted",
            intercepted_by=WeaponEngagement(shot=15, shooter="blue-sam-1"),
        ),
    ]
    debriefing.state_data = SimpleNamespace(  # type: ignore[assignment]
        kill_records=[], shot_records=shots, hit_records=hits, weapon_fates=fates
    )
    return debriefing, red_sam, blue_sam


def test_weapons_summary_for_own_packages(test_game: Game) -> None:
    debriefing, red_sam, blue_sam = _weapon_feed_debriefing(test_game)
    blue_flight = _first_flight(test_game, Player.BLUE)
    red_flight = _first_flight(test_game, Player.RED)
    red_sam_label = _unit_label(red_sam)

    weapons = summarize_debriefing(debriefing, Player.BLUE)["weapons"]

    assert weapons["available"]
    assert weapons["own_aircraft_totals"] == {
        "fired": 4,
        "hit": 1,
        "intercepted": 2,
        "no_hit": 1,
        "unresolved": 0,
    }
    [package] = weapons["own_packages"]
    assert package["package_id"] == PACKAGE_IDS.id_for(blue_flight.package)
    assert package["target"] == blue_flight.package.target.name
    [line] = package["weapons"]
    assert line == {
        "aircraft": blue_flight.unit_type.variant_id,
        "task": blue_flight.flight_type.value,
        "weapon": "AGM-88C",
        "fired": 4,
        "hit": 1,
        "intercepted": 2,
        "no_hit": 1,
        "unresolved": 0,
        "engaged_by_defenses": 1,
        "intercepted_by": {f"{red_sam_label} (9M338)": 2},
        "targets_hit": {red_sam_label: 1},
        "avg_launch_range_nm": 25.0,
    }

    enemy_fire = {e["weapon"]: e for e in weapons["enemy_fire_at_own_aircraft"]}
    assert set(enemy_fire) == {"9M338", "R-77"}
    assert enemy_fire["9M338"]["fired"] == 3 and enemy_fire["9M338"]["hit"] == 1
    assert enemy_fire["R-77"]["fired"] == 2 and enemy_fire["R-77"]["hit"] == 1
    assert enemy_fire["R-77"]["launcher"] == (
        f"{red_flight.unit_type.variant_id} (aircraft)"
    )
    assert enemy_fire["R-77"]["at"] == {blue_flight.unit_type.variant_id: 2}
    assert enemy_fire["9M338"]["launcher"] == f"{red_sam_label} (air defense)"

    [defense] = weapons["own_air_defense"]
    assert defense == {
        "site": blue_sam.theater_unit.ground_object.name,
        "unit": _unit_label(blue_sam),
        "weapon": "5V55",
        "fired": 3,
        "hit": 1,
        "intercepts": 1,
        "at_aircraft": {red_flight.unit_type.variant_id: 2},
        "at_weapons": {"R-77": 1},
    }


def test_weapons_summary_hides_enemy_fire_at_others(test_game: Game) -> None:
    debriefing, red_sam, _blue_sam = _weapon_feed_debriefing(test_game)

    blue = summarize_debriefing(debriefing, Player.BLUE)["weapons"]
    blue_text = json.dumps(blue)
    assert "Kh-29L" not in blue_text  # aimed at a tank, not at an aircraft
    assert "red-sam-1" not in blue_text and "red-1" not in blue_text

    red = summarize_debriefing(debriefing, Player.RED)["weapons"]
    red_text = json.dumps(red)
    assert red["own_aircraft_totals"]["fired"] == 3  # 2 R-77s and the Kh-29L
    assert "5V55" in red_text  # BLUE's SAM fired at RED's fighter
    [defense_9m338] = [d for d in red["own_air_defense"] if d["weapon"] == "9M338"]
    assert defense_9m338["site"] == red_sam.theater_unit.ground_object.name
    assert defense_9m338["fired"] == 5
    assert defense_9m338["intercepts"] == 2
    assert defense_9m338["at_weapons"] == {"AGM-88C": 2}
    # BLUE's HARMs were aimed at RED's SAM, not at RED's aircraft.
    assert all(e["weapon"] != "AGM-88C" for e in red["enemy_fire_at_own_aircraft"])


def test_weapons_markdown_is_readable(test_game: Game) -> None:
    from game.claudecommander.debrief import debrief_markdown

    debriefing, red_sam, _blue_sam = _weapon_feed_debriefing(test_game)
    text = debrief_markdown(summarize_debriefing(debriefing, Player.BLUE))

    assert "{" not in text and "}" not in text
    assert "**Weapons**" in text
    assert (
        f"4x AGM-88C fired: 1 hit, 2 intercepted by 2x {_unit_label(red_sam)} "
        "(9M338), 1 no hit"
    ) in text
    assert "**Enemy fired at your aircraft:** 3x 9M338" in text
    assert "**Own air defense:**" in text
    assert "1 hit, 1 enemy weapons intercepted" in text


def test_missions_without_weapon_feed(test_game: Game) -> None:
    from game.claudecommander.debrief import debrief_markdown

    debriefing, _target, _airfield = _make_debriefing(test_game)
    summary = summarize_debriefing(debriefing, Player.BLUE)
    assert summary["weapons"] == {"available": False}
    assert "Weapons" not in debrief_markdown(summary)
