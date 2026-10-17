import pytest
from dcs.mapping import Point
from dcs.terrain import Caucasus

from game.claudecommander.view import page, segment_circle_crossing

TERRAIN = Caucasus()


def p(x: float, y: float) -> Point:
    return Point(x, y, TERRAIN)


def test_leg_through_center() -> None:
    crossing = segment_circle_crossing(p(0, 0), p(100, 0), p(50, 0), 10)
    assert crossing == pytest.approx((40, 60, 0))


def test_leg_misses_circle() -> None:
    assert segment_circle_crossing(p(0, 0), p(100, 0), p(50, 20), 10) is None


def test_leg_starting_inside() -> None:
    crossing = segment_circle_crossing(p(0, 0), p(100, 0), p(0, 0), 10)
    assert crossing == pytest.approx((0, 10, 0))


def test_leg_ending_before_circle() -> None:
    assert segment_circle_crossing(p(0, 0), p(30, 0), p(50, 0), 10) is None


def test_leg_ending_inside() -> None:
    crossing = segment_circle_crossing(p(0, 0), p(45, 0), p(50, 0), 10)
    assert crossing == pytest.approx((40, 45, 5))


def test_tangent_offset_leg() -> None:
    crossing = segment_circle_crossing(p(0, 0), p(100, 0), p(50, 6), 10)
    assert crossing == pytest.approx((42, 58, 6))


def test_zero_length_leg() -> None:
    assert segment_circle_crossing(p(1, 1), p(1, 1), p(0, 0), 10) == pytest.approx(
        (0, 0, 2**0.5)
    )


def test_page() -> None:
    items, meta = page(list(range(10)), 0, 4)
    assert items == [0, 1, 2, 3]
    assert meta == {"total": 10, "next_offset": 4}
    items, meta = page(list(range(10)), 8, 4)
    assert items == [8, 9]
    assert meta == {"total": 10, "offset": 8}
