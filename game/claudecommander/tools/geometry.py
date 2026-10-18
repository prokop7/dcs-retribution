"""Geometry helpers: distances, bearings and route threat checks."""

from __future__ import annotations

import math
from typing import Optional

from dcs.mapping import Point
from pydantic import BaseModel, Field

from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.tools.common import PointRef
from game.claudecommander.view import (
    CommanderView,
    Json,
    METERS_PER_FOOT,
    bearing_deg,
    segment_circle_crossing,
    to_nm,
)

_MAX_POINTS = 40


class MeasureArgs(BaseModel):
    points: list[PointRef] = Field(
        min_length=2,
        max_length=_MAX_POINTS,
        description="Two or more points, measured as a path in order.",
    )


class RouteThreatArgs(BaseModel):
    points: list[PointRef] = Field(
        min_length=2,
        max_length=_MAX_POINTS,
        description="The route as two or more points, in flying order.",
    )
    altitude_ft: Optional[float] = Field(
        default=None,
        ge=0,
        description="Flight altitude. When given, each threat range is treated "
        "as a slant range and the horizontal radius at this altitude is used.",
    )
    include_detection: bool = Field(
        default=True, description="Also report crossings of radar detection ranges."
    )


def _resolve(view: CommanderView, points: list[PointRef]) -> list[tuple[Point, str]]:
    return [p.resolve(view) for p in points]


def measure(view: CommanderView, args: MeasureArgs) -> Json:
    resolved = _resolve(view, args.points)
    legs = []
    total = 0.0
    for (start, start_name), (end, end_name) in zip(resolved, resolved[1:]):
        distance = start.distance_to_point(end)
        total += distance
        legs.append(
            {
                "from": start_name,
                "to": end_name,
                "distance_nm": to_nm(distance),
                "bearing_deg": bearing_deg(start, end),
            }
        )
    return {"legs": legs, "total_nm": to_nm(total)}


def _horizontal_radius(slant_range: float, altitude_m: Optional[float]) -> float:
    if altitude_m is None:
        return slant_range
    if altitude_m >= slant_range:
        return 0.0
    return math.sqrt(slant_range * slant_range - altitude_m * altitude_m)


def route_threats(view: CommanderView, args: RouteThreatArgs) -> Json:
    resolved = _resolve(view, args.points)
    altitude_m = (
        args.altitude_ft * METERS_PER_FOOT if args.altitude_ft is not None else None
    )
    threats = view.known_threats(include_detection_only=args.include_detection)
    legs = []
    total = 0.0
    for index, ((start, start_name), (end, end_name)) in enumerate(
        zip(resolved, resolved[1:]), start=1
    ):
        length = start.distance_to_point(end)
        leg: Json = {
            "leg": index,
            "from": start_name,
            "to": end_name,
            "start_nm": to_nm(total),
            "length_nm": to_nm(length),
            "bearing_deg": bearing_deg(start, end),
        }
        total += length
        engagement = []
        detection = []
        for threat in threats:
            if threat.threat_range_m:
                radius = _horizontal_radius(threat.threat_range_m, altitude_m)
                crossing = segment_circle_crossing(start, end, threat.position, radius)
                if radius and crossing is not None:
                    entry, exit_, closest = crossing
                    item: Json = {
                        "id": threat.id,
                        "name": threat.name,
                        "category": threat.category,
                        "base": threat.control_point,
                        "threat_nm": to_nm(threat.threat_range_m),
                        "entry_nm": to_nm(entry),
                        "exit_nm": to_nm(exit_),
                        "closest_nm": to_nm(closest),
                    }
                    if altitude_m is not None:
                        item["radius_at_alt_nm"] = to_nm(radius)
                    engagement.append(item)
            if args.include_detection and threat.detection_range_m:
                crossing = segment_circle_crossing(
                    start, end, threat.position, threat.detection_range_m
                )
                if crossing is not None:
                    entry, exit_, closest = crossing
                    detection.append(
                        {
                            "id": threat.id,
                            "name": threat.name,
                            "detection_nm": to_nm(threat.detection_range_m),
                            "entry_nm": to_nm(entry),
                            "exit_nm": to_nm(exit_),
                        }
                    )
        leg["threats"] = sorted(engagement, key=lambda t: t["entry_nm"])
        if args.include_detection:
            leg["detection"] = sorted(detection, key=lambda t: t["entry_nm"])
        legs.append(leg)
    return {
        "note": "Known enemy ground and naval air defenses only, at the ranges "
        "they can engage and see with now, as circles without terrain masking. entry/exit/closest are nm "
        "from the start of the leg. Enemy fighters are not included.",
        "legs": legs,
        "total_nm": to_nm(total),
    }


@commander_tools.register(
    "measure",
    "Distance (nm) and true bearing between points, as a path. Points are ids "
    "(bases, ground objects, front lines, your squadrons or packages), game x/y "
    "meters, or lat/lon.",
    MeasureArgs,
    read_only=True,
)
def measure_tool(context: CommanderContext, args: MeasureArgs) -> Json:
    return measure(context.view(), args)


@commander_tools.register(
    "check_route_threats",
    "For a route (points as in measure), lists for each leg which known enemy air "
    "defense engagement ranges it enters, with entry/exit distance along the leg "
    "and closest approach, and optionally which radar detection ranges it "
    "crosses. Uses only enemy sites your side knows, at the ranges they can "
    "engage with now; destroyed sites and SAMs with no working radar are "
    "excluded.",
    RouteThreatArgs,
    read_only=True,
)
def check_route_threats(context: CommanderContext, args: RouteThreatArgs) -> Json:
    return route_threats(context.view(), args)
