"""Input models and helpers shared by the commander tools."""

from __future__ import annotations

from typing import Optional

from dcs.mapping import Point
from pydantic import BaseModel, Field, model_validator

from game.ato.flighttype import FlightType
from game.claudecommander.mcp.tools import ToolError
from game.claudecommander.view import CommanderView, METERS_PER_NM, position

DEFAULT_LIMIT = 40
MAX_LIMIT = 100


class PointRef(BaseModel):
    """A point on the map, given in exactly one of three ways."""

    id: Optional[str] = Field(
        default=None,
        description="Id of a base, ground object, front line, squadron or package.",
    )
    x: Optional[float] = Field(default=None, description="Game x in meters (north).")
    y: Optional[float] = Field(default=None, description="Game y in meters (east).")
    lat: Optional[float] = None
    lon: Optional[float] = None

    @model_validator(mode="after")
    def _exactly_one_form(self) -> PointRef:
        forms = [
            self.id is not None,
            self.x is not None or self.y is not None,
            self.lat is not None or self.lon is not None,
        ]
        if sum(forms) != 1:
            raise ValueError("give exactly one of: id, x and y, or lat and lon")
        if forms[1] and (self.x is None or self.y is None):
            raise ValueError("x and y must be given together")
        if forms[2] and (self.lat is None or self.lon is None):
            raise ValueError("lat and lon must be given together")
        return self

    def resolve(self, view: CommanderView) -> tuple[Point, str]:
        """The point and a short label for it."""
        point, name = view.point_from(self.x, self.y, self.lat, self.lon, self.id)
        if name is None:
            coords = position(point)
            name = f"({coords['x']}, {coords['y']})"
        return point, name


class AreaFilter(BaseModel):
    near: Optional[PointRef] = Field(
        default=None, description="Only return objects within radius_nm of this."
    )
    radius_nm: Optional[float] = Field(default=None, gt=0)

    def area(self, view: CommanderView) -> tuple[Optional[Point], Optional[float]]:
        if self.near is None:
            if self.radius_nm is not None:
                raise ToolError("radius_nm needs near")
            return None, None
        if self.radius_nm is None:
            raise ToolError("near needs radius_nm")
        point, _ = self.near.resolve(view)
        return point, self.radius_nm * METERS_PER_NM


class Paging(BaseModel):
    limit: int = Field(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="Maximum items per list.",
    )
    offset: int = Field(default=0, ge=0, description="Items to skip in each list.")


def parse_flight_type(name: str) -> FlightType:
    """Accepts a mission type by its value ("Anti-ship") or name ("ANTISHIP")."""
    wanted = name.strip().lower()
    for flight_type in FlightType:
        if wanted in (flight_type.value.lower(), flight_type.name.lower()):
            return flight_type
    valid = ", ".join(t.value for t in FlightType)
    raise ToolError(f"Unknown mission type {name!r}. Valid mission types: {valid}")
