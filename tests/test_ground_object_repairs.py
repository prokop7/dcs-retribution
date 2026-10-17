"""Tests for the SAM and building repair features."""

from __future__ import annotations

import itertools
from typing import Any, Optional, Type, Union
from unittest.mock import MagicMock

import pytest
from dcs.mapping import Point
from dcs.statics import Fortification
from dcs.terrain import Terrain
from dcs.unittype import UnitType as DcsUnitType
from dcs.vehicles import AirDefence

from game.game import Game
from game.point_with_heading import PointWithHeading
from game.procurement import ProcurementAi, RepairCandidate
from game.settings import Settings
from game.sim import GameUpdateEvents
from game.theater.controlpoint import ControlPoint
from game.theater.player import Player
from game.theater.presetlocation import PresetLocation
from game.theater.theatergroundobject import (
    BuildingGroundObject,
    SamGroundObject,
    TheaterGroundObject,
)
from game.theater.theatergroup import (
    REPAIR_CARCASS_RADIUS,
    TheaterGroup,
    TheaterUnit,
)
from game.utils import Heading, meters

_ids = itertools.count(1)


def _point(x: float, y: float) -> Point:
    return Point(x, y, MagicMock(spec=Terrain))


def _game(settings: Optional[Settings] = None) -> MagicMock:
    game = MagicMock()
    game.settings = settings if settings is not None else Settings()
    return game


def _control_point(game: MagicMock, x: float = 0.0, y: float = 0.0) -> MagicMock:
    cp = MagicMock()
    cp.position = _point(x, y)
    cp.captured.is_neutral = False
    cp.captured.is_blue = True
    cp.is_friendly.return_value = True
    cp.coalition.game = game
    cp.front_lines = {}
    return cp


def _sam(cp: MagicMock, x: float, y: float) -> SamGroundObject:
    return SamGroundObject(
        f"SAM {next(_ids)}", PresetLocation("sam", _point(x, y)), cp, None
    )


def _building(cp: MagicMock, category: str) -> BuildingGroundObject:
    return BuildingGroundObject(
        f"{category} {next(_ids)}",
        category,
        PresetLocation(category, cp.position),
        cp,
        None,
    )


def _add_unit(
    ground_object: TheaterGroundObject,
    unit_type: Type[DcsUnitType],
    alive: bool = True,
) -> TheaterUnit:
    position = PointWithHeading.from_point(ground_object.position, Heading(0))
    unit = TheaterUnit(
        next(_ids), unit_type.id, unit_type, position, ground_object, alive=alive
    )
    if not ground_object.groups:
        ground_object.groups.append(
            TheaterGroup(next(_ids), "group", position, [], ground_object)
        )
    ground_object.groups[0].units.append(unit)
    return unit


def _state(unit: TheaterUnit) -> tuple[bool, Optional[int]]:
    """(alive, repair_turns_remaining), read in one go to keep mypy from narrowing."""
    return unit.alive, unit.repair_turns_remaining


def _procurement(game: MagicMock) -> ProcurementAi:
    return ProcurementAi(
        game,
        Player.BLUE,
        MagicMock(),
        manage_runways=False,
        manage_front_line=False,
        manage_aircraft=False,
        manage_ground_objects=True,
        manage_buildings=True,
    )


# Repair countdown and revive


@pytest.fixture
def dead_sam_unit() -> TheaterUnit:
    cp = _control_point(_game())
    sam = _sam(cp, 0, 0)
    return _add_unit(sam, AirDefence.SA_11_Buk_SR_9S18M1, alive=False)


def test_begin_repair_starts_countdown(dead_sam_unit: TheaterUnit) -> None:
    events = GameUpdateEvents()
    dead_sam_unit.begin_repair(2, events)
    assert dead_sam_unit.repair_turns_remaining == 2
    assert not dead_sam_unit.alive
    assert dead_sam_unit.ground_object not in events.updated_tgos


def test_countdown_revives_unit_on_last_turn(dead_sam_unit: TheaterUnit) -> None:
    game: Any = dead_sam_unit.ground_object.control_point.coalition.game
    dead_sam_unit.begin_repair(2, GameUpdateEvents())

    events = GameUpdateEvents()
    dead_sam_unit.process_repair_turn(events)
    assert _state(dead_sam_unit) == (False, 1)
    game.remove_destroyed_units_near.assert_not_called()

    dead_sam_unit.process_repair_turn(events)
    assert _state(dead_sam_unit) == (True, None)
    assert dead_sam_unit.ground_object in events.updated_tgos
    game.remove_destroyed_units_near.assert_called_once_with(
        dead_sam_unit.position, REPAIR_CARCASS_RADIUS
    )


def test_zero_turn_repair_revives_through_given_events(
    dead_sam_unit: TheaterUnit,
) -> None:
    events = GameUpdateEvents()
    dead_sam_unit.begin_repair(0, events)
    assert dead_sam_unit.alive
    assert dead_sam_unit.repair_turns_remaining is None
    # The map must be told about the revived unit.
    assert dead_sam_unit.ground_object in events.updated_tgos


def test_countdown_is_cleared_for_unit_revived_otherwise(
    dead_sam_unit: TheaterUnit,
) -> None:
    dead_sam_unit.begin_repair(3, GameUpdateEvents())
    dead_sam_unit.alive = True
    dead_sam_unit.process_repair_turn(GameUpdateEvents())
    assert dead_sam_unit.repair_turns_remaining is None


def test_unit_without_repair_is_untouched(dead_sam_unit: TheaterUnit) -> None:
    events = GameUpdateEvents()
    dead_sam_unit.process_repair_turn(events)
    assert not dead_sam_unit.alive
    assert dead_sam_unit.repair_turns_remaining is None
    assert events.empty


def test_control_point_ticks_all_ground_objects() -> None:
    cp = _control_point(_game())
    sam = _sam(cp, 0, 0)
    first = _add_unit(sam, AirDefence.SA_11_Buk_SR_9S18M1, alive=False)
    second = _add_unit(sam, AirDefence.SA_11_Buk_LN_9A310M1, alive=False)
    first.repair_turns_remaining = 1
    second.repair_turns_remaining = 3
    cp.ground_objects = [sam]

    ControlPoint.process_ground_object_repairs(cp, GameUpdateEvents())

    assert _state(first) == (True, None)
    assert _state(second) == (False, 2)


def test_site_under_repair_is_reported_damaged(dead_sam_unit: TheaterUnit) -> None:
    sam = dead_sam_unit.ground_object
    assert sam.is_dead and not sam.has_pending_repairs
    dead_sam_unit.begin_repair(2, GameUpdateEvents())
    assert sam.has_pending_repairs


def _carcass(x: float, z: float) -> dict[str, Union[float, str]]:
    return {"x": x, "y": 0.0, "z": z, "type": "Wreck", "orientation": 0.0}


def test_remove_destroyed_units_near() -> None:
    game = Game.__new__(Game)
    near = _carcass(105.0, 200.0)
    far = _carcass(100.0, 260.0)
    garbled: dict[str, Union[float, str]] = {"type": "Wreck"}
    nan = _carcass(float("nan"), 200.0)
    game._Game__destroyed_units = [near, far, garbled, nan]  # type: ignore[attr-defined]

    # pydcs Point.y is the DCS z axis.
    game.remove_destroyed_units_near(_point(100.0, 200.0), meters(15))

    assert game.get_destroyed_units() == [far, garbled, nan]


# Repair selection


def test_select_repairs_prefers_priority_then_price() -> None:
    units = [MagicMock(name=f"unit{i}") for i in range(4)]
    candidates: list[RepairCandidate] = [
        (1.0, 10, units[0]),
        (3.0, 50, units[1]),
        (3.0, 20, units[2]),
        (2.0, 40, units[3]),
    ]
    # 20 + 50 fit; 40 doesn't fit in the remaining 30, but 10 still does.
    selected = ProcurementAi.select_repairs(candidates, 100)
    assert selected == [(20, units[2]), (50, units[1]), (10, units[0])]


def test_select_repairs_with_no_budget() -> None:
    assert ProcurementAi.select_repairs([(1.0, 10, MagicMock())], 5) == []


def test_procurement_flags_are_keyword_only() -> None:
    with pytest.raises(TypeError):
        ProcurementAi(  # type: ignore[misc]
            _game(), Player.BLUE, MagicMock(), True, True, True, True, True
        )


# SAM repair prioritisation


@pytest.fixture
def sam_theater() -> dict[str, Any]:
    """A SAM site defending a front line base and an identical remote one."""
    game = _game()
    base = _control_point(game, 0, 0)
    base.front_lines = {"front": MagicMock(position=_point(5_000, 0))}
    oil = _building(base, "oil")
    frontline_sam = _sam(base, 10_000, 0)
    remote_sam = _sam(base, 500_000, 0)
    for sam in (frontline_sam, remote_sam):
        _add_unit(sam, AirDefence.SA_11_Buk_SR_9S18M1, alive=False)
        _add_unit(sam, AirDefence.SA_11_Buk_CC_9S470M1, alive=False)
        _add_unit(sam, AirDefence.SA_11_Buk_LN_9A310M1)
    base.ground_objects = [oil, frontline_sam, remote_sam]
    game.theater.player_points.return_value = [base]
    game.theater.ground_objects = base.ground_objects
    return {"game": game, "frontline": frontline_sam, "remote": remote_sam}


def test_sam_covering_front_line_base_has_higher_priority(
    sam_theater: dict[str, Any],
) -> None:
    ai = _procurement(sam_theater["game"])
    frontline = ai.ground_object_repair_priority(sam_theater["frontline"])
    remote = ai.ground_object_repair_priority(sam_theater["remote"])
    assert frontline > remote


def test_sam_priority_follows_weights(sam_theater: dict[str, Any]) -> None:
    settings: Settings = sam_theater["game"].settings
    ai = _procurement(sam_theater["game"])
    before = ai.ground_object_repair_priority(sam_theater["frontline"])
    settings.sam_repair_weight_frontline += 1.0
    after = ai.ground_object_repair_priority(sam_theater["frontline"])
    assert after > before


def test_sam_repairs_critical_units_of_sites_above_threshold(
    sam_theater: dict[str, Any],
) -> None:
    game = sam_theater["game"]
    ai = _procurement(game)
    frontline: SamGroundObject = sam_theater["frontline"]
    remote: SamGroundObject = sam_theater["remote"]
    game.settings.sam_repair_priority_threshold = (
        ai.ground_object_repair_priority(frontline)
        + ai.ground_object_repair_priority(remote)
    ) / 2

    candidates = ai.sam_repair_candidates()

    # Only the search radar: the command post isn't needed for the site to work
    # and the remote site is below the threshold.
    assert [unit.type for _, _, unit in candidates] == [AirDefence.SA_11_Buk_SR_9S18M1]
    radar = candidates[0][2]
    assert radar.ground_object is frontline

    # While the radar is being repaired nothing else is queued for the site.
    radar.repair_turns_remaining = 2
    assert ai.sam_repair_candidates() == []

    # Once it's back in service the rest of the site gets repaired.
    radar.alive = True
    radar.repair_turns_remaining = None
    assert [unit.type for _, _, unit in ai.sam_repair_candidates()] == [
        AirDefence.SA_11_Buk_CC_9S470M1
    ]


def test_repair_ground_objects_spends_budget_fraction(
    sam_theater: dict[str, Any],
) -> None:
    game = sam_theater["game"]
    game.settings.sam_repair_priority_threshold = 0.0
    game.settings.sam_repair_budget_fraction = 0.5
    ai = _procurement(game)

    # 30 of 60 is available: one 28M search radar fits, the second doesn't.
    remaining = ai.repair_ground_objects(60, GameUpdateEvents())

    assert remaining == 32
    repairing = [
        u
        for sam in (sam_theater["frontline"], sam_theater["remote"])
        for u in sam.units
        if u.repair_turns_remaining is not None
    ]
    assert len(repairing) == 1
    assert repairing[0].ground_object is sam_theater["frontline"]
    assert (
        repairing[0].repair_turns_remaining == game.settings.ground_object_repair_turns
    )


# Building repairs


@pytest.fixture
def buildings() -> dict[str, Any]:
    game = _game()
    base = _control_point(game, 0, 0)
    enemy = _control_point(game, 200_000, 0)
    game.theater.player_points.return_value = [base]
    game.theater.control_points_for.return_value = [enemy]
    result: dict[str, Any] = {"game": game}
    base.ground_objects = []
    for category in ("oil", "ammo", "factory"):
        building = _building(base, category)
        result[category] = _add_unit(building, Fortification.Workshop_A, alive=False)
        base.ground_objects.append(building)
    return result


def test_building_repair_cost_follows_income_and_bonuses(
    buildings: dict[str, Any],
) -> None:
    assert buildings["oil"].ground_object.repair_cost() == 40  # 10 * 4
    assert buildings["ammo"].ground_object.repair_cost() == 18  # 2 * 4 + 10
    assert buildings["factory"].ground_object.repair_cost() == 22  # 2.5 * 4 + 12


def test_building_repairs_take_priority_order_within_budget(
    buildings: dict[str, Any],
) -> None:
    game = buildings["game"]
    ai = _procurement(game)
    # Default priorities: factory (4.65) > ammo depot (4.51) > oil (4.4).
    priorities = {
        category: ai.building_repair_priority(buildings[category].ground_object)
        for category in ("oil", "ammo", "factory")
    }
    assert priorities["factory"] > priorities["ammo"] > priorities["oil"]

    # 15% of 200 is 30: the 22M factory fits, the 18M ammo depot no longer does.
    remaining = ai.repair_buildings(200, GameUpdateEvents())

    assert remaining == 178
    assert buildings["factory"].repair_turns_remaining == 4
    assert buildings["ammo"].repair_turns_remaining is None
    assert buildings["oil"].repair_turns_remaining is None
    game.message.assert_called_once()


def test_building_repairs_skip_buildings_already_repairing(
    buildings: dict[str, Any],
) -> None:
    buildings["factory"].repair_turns_remaining = 1
    remaining = _procurement(buildings["game"]).repair_buildings(
        200, GameUpdateEvents()
    )
    assert remaining == 182
    assert buildings["ammo"].repair_turns_remaining == 4


def test_instant_building_repairs(buildings: dict[str, Any]) -> None:
    game = buildings["game"]
    game.settings.building_repair_turns = 0
    events = GameUpdateEvents()

    remaining = _procurement(game).repair_buildings(1000, events)

    assert remaining == 1000 - (40 + 18 + 22)
    for category in ("oil", "ammo", "factory"):
        unit = buildings[category]
        assert unit.alive
        assert unit.ground_object in events.updated_tgos


def test_spend_budget_repairs_buildings_when_enabled(
    buildings: dict[str, Any],
) -> None:
    ai = _procurement(buildings["game"])
    ai.manage_ground_objects = False
    assert ai.spend_budget(200, GameUpdateEvents()) == 178


# Save compatibility


def test_theater_unit_from_old_save_has_no_repair() -> None:
    cp = _control_point(_game())
    unit = _add_unit(_sam(cp, 0, 0), AirDefence.SA_11_Buk_SR_9S18M1)
    state = dict(unit.__dict__)
    del state["repair_turns_remaining"]

    # Unpickling restores __dict__ without running __init__.
    loaded = TheaterUnit.__new__(TheaterUnit)
    loaded.__dict__.update(state)

    assert loaded.repair_turns_remaining is None


def test_ground_object_from_old_save_has_required_unit_classes() -> None:
    sam = _sam(_control_point(_game()), 0, 0)
    state = sam.__getstate__()
    del state["required_unit_classes"]

    loaded = SamGroundObject.__new__(SamGroundObject)
    loaded.__setstate__(state)

    assert loaded.required_unit_classes == set()
    assert not loaded.has_pending_repairs


def test_settings_from_old_save_get_repair_defaults() -> None:
    new_fields = {
        "automate_ground_object_repairs": False,
        "ground_object_repair_turns": 2,
        "purchased_ground_object_activation_turns": 2,
        "sam_repair_budget_fraction": 0.4,
        "sam_repair_priority_threshold": 1.5,
        "automate_building_repairs": False,
        "building_repair_turns": 4,
        "building_repair_budget_percent": 15,
        "building_repair_income_multiplier": 4.0,
        "building_repair_ammo_bonus": 10.0,
        "building_repair_factory_bonus": 12.0,
    }
    state = Settings().__dict__.copy()
    for name in new_fields:
        del state[name]

    loaded = Settings.__new__(Settings)
    loaded.__setstate__(state)

    for name, default in new_fields.items():
        assert getattr(loaded, name) == default, name
