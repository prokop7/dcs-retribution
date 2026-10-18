from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

from game.debriefing import (
    AirLosses,
    BaseCaptureEvent,
    Debriefing,
    GroundLosses,
    SideLossCounts,
)
from game.theater import ControlPoint, Player


def _items(n: int) -> list[Any]:
    """n placeholder loss objects. loss_counts only needs list length."""
    return [MagicMock() for _ in range(n)]


def _airlift(cargo_size: int) -> Any:
    """An airlift loss whose .cargo has the given length."""
    return SimpleNamespace(cargo=list(range(cargo_size)))


def _capture(captured_by: Player) -> BaseCaptureEvent:
    return BaseCaptureEvent(cast(ControlPoint, MagicMock()), captured_by)


def _sample_debriefing() -> Debriefing:
    """A Debriefing with known per-side losses, built without the heavy
    __init__ (which needs a Game and UnitMap). loss_counts and the loss
    properties only read air_losses, ground_losses, and base_captures."""
    air = AirLosses(player=_items(1), enemy=_items(2))
    ground = GroundLosses(
        player_front_line=_items(3),
        enemy_front_line=_items(1),
        player_motorpool=_items(2),
        enemy_motorpool=_items(3),
        player_convoy=_items(2),
        enemy_convoy=_items(0),
        player_cargo_ships=_items(1),
        enemy_cargo_ships=_items(4),
        player_airlifts=[_airlift(2)],
        enemy_airlifts=[_airlift(1), _airlift(3)],
        player_ground_objects=_items(5),
        enemy_ground_objects=_items(2),
        player_scenery=_items(0),
        enemy_scenery=_items(1),
        player_airfields=_items(1),
        enemy_airfields=_items(2),
    )
    captures = [
        _capture(Player.BLUE),
        _capture(Player.BLUE),
        _capture(Player.RED),
    ]
    debriefing = Debriefing.__new__(Debriefing)
    debriefing.air_losses = air
    debriefing.ground_losses = ground
    debriefing.base_captures = captures
    return debriefing


def test_loss_counts_blue_side() -> None:
    blue = _sample_debriefing().loss_counts(Player.BLUE)
    assert blue == SideLossCounts(
        aircraft=1,
        front_line=3,
        motorpool=2,
        convoy=2,
        cargo_ships=1,
        airlift_cargo=2,
        ground_objects=5,
        scenery=0,
        bases_lost=1,  # one base captured by RED == one base Blue lost
        runways_destroyed=1,
    )


def test_loss_counts_red_side() -> None:
    red = _sample_debriefing().loss_counts(Player.RED)
    assert red == SideLossCounts(
        aircraft=2,
        front_line=1,
        motorpool=3,
        convoy=0,
        cargo_ships=4,
        airlift_cargo=4,  # 1 + 3
        ground_objects=2,
        scenery=1,
        bases_lost=2,  # two bases captured by BLUE == two bases Red lost
        runways_destroyed=2,
    )


def test_loss_counts_partition_matches_combined_totals() -> None:
    """Blue + Red for each category must equal the combined total the UI
    shows today (the existing Debriefing properties). Pins that loss_counts
    reads the same lists and never alters totals."""
    debriefing = _sample_debriefing()
    blue = debriefing.loss_counts(Player.BLUE)
    red = debriefing.loss_counts(Player.RED)

    assert blue.aircraft + red.aircraft == len(list(debriefing.air_losses.losses))
    assert blue.front_line + red.front_line == len(list(debriefing.front_line_losses))
    assert blue.motorpool + red.motorpool == len(list(debriefing.motorpool_losses))
    assert blue.convoy + red.convoy == len(list(debriefing.convoy_losses))
    assert blue.cargo_ships + red.cargo_ships == len(list(debriefing.cargo_ship_losses))
    assert blue.airlift_cargo + red.airlift_cargo == sum(
        len(loss.cargo) for loss in debriefing.airlift_losses
    )
    assert blue.ground_objects + red.ground_objects == len(
        list(debriefing.ground_object_losses)
    )
    assert blue.scenery + red.scenery == len(list(debriefing.scenery_object_losses))
    assert blue.bases_lost + red.bases_lost == len(debriefing.base_captures)
    assert blue.runways_destroyed + red.runways_destroyed == len(
        list(debriefing.damaged_runways)
    )


def test_motorpool_losses_by_type_counts_per_side() -> None:
    """motorpool_losses_by_type buckets a side's motorpool kills by unit type,
    mirroring front_line_losses_by_type. Feeds the debrief casualty screen."""
    t90 = MagicMock(name="T-90A")
    bmp = MagicMock(name="BMP-3")
    debriefing = _sample_debriefing()
    debriefing.ground_losses = GroundLosses(
        player_motorpool=[
            MagicMock(unit_type=t90),
            MagicMock(unit_type=t90),
            MagicMock(unit_type=bmp),
        ],
        enemy_motorpool=[MagicMock(unit_type=bmp)],
    )

    assert debriefing.motorpool_losses_by_type(Player.BLUE) == {t90: 2, bmp: 1}
    assert debriefing.motorpool_losses_by_type(Player.RED) == {bmp: 1}


def test_state_without_weapon_feed_parses() -> None:
    from game.debriefing import StateData
    from game.unitmap import UnitMap

    state = StateData.from_json({"mission_ended": True, "kill_events": []}, UnitMap())
    assert state.shot_records == []
    assert state.hit_records == []
    assert state.weapon_fates == []


def test_weapon_feed_parses_tolerantly() -> None:
    from game.debriefing import StateData, WeaponEngagement
    from game.unitmap import UnitMap

    data = {
        "shot_events": [
            {
                "id": 1,
                "time": 10.5,
                "shooter": "Hornet-1",
                "shooter_coalition": 2,
                "weapon": "AGM_88",
                "display_name": "AGM-88C",
                "target": "SA15-1",
                "range_m": 50000,
            },
            {"id": 2.0, "target_is_weapon": True, "target_shot": 1},
            {"weapon": "no id"},
            "junk",
        ],
        "hit_events": [{"shot": 1, "target": "SA15-1"}, {}, 7],
        "weapon_fates": [
            {
                "shot": 1,
                "fate": "intercepted",
                "intercepted_by": {"shot": 2, "shooter_type": "Tor 9A331"},
                "engaged_by": [{"shot": 2}, "junk"],
                "x": 1,
            },
            {"shot": 3, "fate": "exploded"},
            {"fate": "hit"},
        ],
    }
    state = StateData.from_json(data, UnitMap())

    first, second = state.shot_records
    assert first.weapon_name == "AGM-88C" and first.range_m == 50000.0
    assert first.shooter_coalition == 2 and not first.target_is_weapon
    assert second.id == 2 and second.target_is_weapon and second.target_shot == 1
    assert second.weapon_name == "unknown weapon"

    assert [hit.shot for hit in state.hit_records] == [1, None]

    [fate] = state.weapon_fates
    assert fate.fate == "intercepted" and fate.x == 1.0
    assert fate.intercepted_by == WeaponEngagement(shot=2, shooter_type="Tor 9A331")
    assert fate.engaged_by == (WeaponEngagement(shot=2),)


def test_weapon_feed_tolerates_empty_lua_tables() -> None:
    from game.debriefing import StateData
    from game.unitmap import UnitMap

    # An empty Lua table may be encoded as an object rather than a list.
    state = StateData.from_json(
        {"shot_events": {}, "hit_events": {}, "weapon_fates": {}}, UnitMap()
    )
    assert state.shot_records == [] and state.weapon_fates == []
