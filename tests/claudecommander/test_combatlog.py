"""Combat logs and query_combat_log."""

from __future__ import annotations

import json
from typing import Any

from game import Game
from game.claudecommander.combatlog import build_combat_log
from game.claudecommander.state import CommanderState
from game.theater.player import Player
from tests.claudecommander.test_air_tools import Caller
from tests.claudecommander.test_debrief import _weapon_feed_debriefing


def test_blue_log_holds_own_fire_and_fire_at_own_aircraft(test_game: Game) -> None:
    debriefing, _red_sam, _blue_sam = _weapon_feed_debriefing(test_game)
    log = build_combat_log(debriefing, Player.BLUE, 4)
    shots = [r for r in log if r["event"] == "shot"]
    harms = [r for r in shots if r["weapon"].startswith("AGM-88")]
    assert len(harms) == 4
    assert sorted(r["outcome"] for r in harms) == [
        "hit",
        "intercepted",
        "intercepted",
        "no_hit",
    ]
    assert all(r["package"] for r in harms)
    assert harms[0]["range_nm"] == 25.0
    # Tor missiles aimed at our HARMs are visible as attempts on our weapons.
    assert any(r.get("target", "").startswith("own AGM-88") for r in shots)
    # Enemy fire at our ground forces is visible too.
    assert any("Kh-29" in r["weapon"] for r in shots)
    # Enemy units appear by type only.
    assert "red-sam-1" not in json.dumps(log) and "red-1" not in json.dumps(log)


def test_logs_are_kept_for_the_last_missions() -> None:
    state = CommanderState()
    for turn in range(1, 6):
        state.record_debriefing({}, [], [{"event": "shot"}], turn, missions_kept=3)
    assert sorted(state.combat_logs) == [3, 4, 5]


def query(game: Game, args: dict[str, Any]) -> dict[str, Any]:
    return Caller(game, Player.BLUE).ok("query_combat_log", args)


def test_query_filters_and_groups(game: Game) -> None:
    debriefing, _red_sam, _blue_sam = _weapon_feed_debriefing(game)
    game.blue.commander.combat_logs = {4: build_combat_log(debriefing, Player.BLUE, 4)}

    grouped = query(game, {"weapon": "AGM-88", "group_by": ["outcome"]})
    counts = {g["outcome"]: g["count"] for g in grouped["groups"]}
    assert counts == {"intercepted": 2, "hit": 1, "no_hit": 1}

    intercepted = query(game, {"outcome": "intercepted", "side": "own"})
    assert intercepted["matched"] >= 2
    assert all(r["outcome"] == "intercepted" for r in intercepted["records"])

    enemy = query(game, {"side": "enemy", "event": "shot"})
    assert enemy["matched"] > 0
    assert all(r["shooter_side"] == "enemy" for r in enemy["records"])

    paged = query(game, {"limit": 2})
    assert len(paged["records"]) == 2 and paged["records_omitted"] > 0


def test_query_without_logs_and_unknown_turn(game: Game) -> None:
    game.blue.commander.combat_logs = {}
    assert query(game, {})["available_turns"] == []
    game.blue.commander.combat_logs = {4: []}
    text = Caller(game, Player.BLUE).error("query_combat_log", {"turn": 9})
    assert "Available: [4]" in text


def test_ground_targets_count_in_debrief_and_log(test_game: Game) -> None:
    from types import SimpleNamespace

    from game.claudecommander.debrief import debrief_markdown, summarize_debriefing
    from game.unitmap import FrontLineUnit

    debriefing, _red_sam, _blue_sam = _weapon_feed_debriefing(test_game)
    blue_cp = next(test_game.theater.conflicts()).blue_cp
    tank_type = next(iter(test_game.blue.faction.frontline_units))
    tank = FrontLineUnit(tank_type, blue_cp)
    unit_map = debriefing.unit_map
    debriefing.unit_map = SimpleNamespace(  # type: ignore[assignment]
        flight=unit_map.flight,
        theater_units=unit_map.theater_units,
        front_line_unit={"blue-tank": tank}.get,
    )

    weapons = summarize_debriefing(debriefing, Player.BLUE)["weapons"]
    [line] = weapons["enemy_fire_at_own_ground"]
    assert line["weapon"].startswith("Kh-29") and line["fired"] == 1
    assert any(tank_type.variant_id in target for target in line["at"])
    markdown = debrief_markdown(summarize_debriefing(debriefing, Player.BLUE))
    assert "Enemy fired at your ground forces" in markdown

    log = build_combat_log(debriefing, Player.BLUE, 4)
    kh29 = [r for r in log if "Kh-29" in r.get("weapon", "")]
    assert len(kh29) == 1 and kh29[0]["target_side"] == "own"
    assert "front line" in kh29[0]["target"]
    # RED never learns about BLUE's own internal fire beyond what hit it.
    red_log = build_combat_log(debriefing, Player.RED, 4)
    assert all(
        r.get("shooter_side") == "own" or r.get("target_side") == "own"
        for r in red_log
        if r["event"] == "shot"
    )
