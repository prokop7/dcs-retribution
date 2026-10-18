"""One side's combat log of a mission: shots, hits and kills it may know about.

The debrief aggregates; this keeps the individual records so a commander can
drill into them with query_combat_log. The fog rules are those of the debrief:

* everything the side's own aircraft and air defenses fired, and what became of
  it (hit, intercepted and by what launcher type, no hit);
* enemy launches at the side's own aircraft, ground forces, sites and buildings,
  and at its weapons (intercept attempts), by launcher type only;
* kills where the side lost the unit (aircraft, armor, site or building) or
  scored the kill, ground targets included.

Enemy unit names never appear, and nothing the enemy fired at anything else.
"""

from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

from dcs.mapping import Point

from game.claudecommander.debrief import (
    _DCS_COALITIONS,
    _WeaponFeed,
    _weapon_label,
)
from game.theater.player import Player

if TYPE_CHECKING:
    from game.ato.flight import Flight
    from game.debriefing import Debriefing, KillRecord, ShotRecord

#: Records kept per mission; the mission feed itself is capped at 4000 shots.
MAX_RECORDS = 3000
#: Missions kept in a commander's saved state.
MISSIONS_KEPT = 3

Record = dict[str, Any]


def _flight_label(flight: Flight) -> str:
    package = flight.package
    target = package.target.name
    primary = package.primary_task
    task = primary.value if primary is not None else flight.flight_type.value
    return f"{task} vs {target}"


def _position(game_x: Optional[float], game_y: Optional[float]) -> Record:
    if game_x is None or game_y is None:
        return {}
    return {"x": round(game_x), "y": round(game_y)}


def _where(debriefing: Debriefing, x: Optional[float], y: Optional[float]) -> Record:
    if x is None or y is None:
        return {}
    theater = debriefing.game.theater
    point = Point(x, y, theater.terrain)
    nearest = min(
        theater.controlpoints, key=lambda cp: cp.position.distance_to_point(point)
    )
    distance_nm = nearest.position.distance_to_point(point) / 1852
    return {
        "where": f"{distance_nm:.0f} nm from {nearest.name}",
        "over_water": theater.is_in_sea(point),
    }


class _LogBuilder:
    def __init__(self, debriefing: Debriefing, player: Player, turn: int) -> None:
        self.debriefing = debriefing
        self.player = player
        self.turn = turn
        self.feed = _WeaponFeed(debriefing, player)

    def side_of(self, name: Optional[str], coalition: Optional[int]) -> Optional[str]:
        flight = self.feed.flight(name)
        ground = self.feed.ground_target(name)
        if flight is not None:
            owner: Optional[Player] = flight.squadron.player
        elif ground is not None:
            owner = ground[0]
        else:
            owner = _DCS_COALITIONS.get(coalition or 0)
        if owner is None:
            return None
        return "own" if owner is self.player else "enemy"

    def label(
        self, name: Optional[str], dcs_type: Optional[str], side: Optional[str]
    ) -> str:
        if side == "own":
            flight = self.feed.flight(name)
            if flight is not None:
                return f"{flight.unit_type.variant_id} ({flight.flight_type.value})"
        ground = self.feed.ground_target(name)
        if ground is not None:
            # Site and building names are static intelligence; units by type.
            return ground[1]
        # Enemy aircraft and anything unresolved by type only.
        return self.feed.describe_launcher(name, dcs_type)

    def shot_record(self, shot: ShotRecord) -> Optional[Record]:
        shooter_side = self.side_of(shot.shooter, shot.shooter_coalition)
        target_side = (
            None
            if shot.target_is_weapon
            else self.side_of(shot.target, shot.target_coalition)
        )
        targeted_shot = (
            self.feed.shot_by_id.get(shot.target_shot)
            if shot.target_shot is not None
            else None
        )
        at_own_weapon = targeted_shot is not None and (
            self.side_of(targeted_shot.shooter, targeted_shot.shooter_coalition)
            == "own"
        )
        at_own = not shot.target_is_weapon and (
            self.feed.is_own_aircraft(
                shot.target, shot.target_type, shot.target_coalition
            )
            or target_side == "own"
        )
        if shooter_side != "own" and not at_own and not at_own_weapon:
            # Enemy fire at anything but our forces and weapons stays unknown.
            return None
        record: Record = {
            "turn": self.turn,
            "event": "shot",
            "shot": shot.id,
            "shooter_side": shooter_side or "unknown",
            "shooter": self.label(shot.shooter, shot.shooter_type, shooter_side),
            "weapon": _weapon_label(shot.weapon_name),
            "outcome": self.feed.outcome(shot),
        }
        if shot.time is not None:
            record["t_min"] = round(shot.time / 60, 1)
        flight = self.feed.flight(shot.shooter)
        if flight is not None and shooter_side == "own":
            record["package"] = _flight_label(flight)
        if at_own_weapon and targeted_shot is not None:
            record["target"] = (
                f"own {_weapon_label(targeted_shot.weapon_name)} (shot {targeted_shot.id})"
            )
            record["target_side"] = "own"
        elif shot.target or shot.target_type:
            record["target"] = self.label(shot.target, shot.target_type, target_side)
            record["target_side"] = target_side or "unknown"
        if shot.range_m is not None:
            record["range_nm"] = round(shot.range_m / 1852, 1)
        fate = self.feed.fate_by_shot.get(shot.id)
        if fate is not None:
            if fate.intercepted_by is not None:
                record["intercepted_by"] = self.feed.describe_launcher(
                    None, fate.intercepted_by.shooter_type
                )
            # DCS x is north and z is east, as game x and y.
            record.update(_position(fate.x, fate.z))
            record.update(_where(self.debriefing, fate.x, fate.z))
        return record

    def kill_record(self, kill: KillRecord) -> Optional[Record]:
        victim_side = self.side_of(kill.target, None)
        killer_side = self.side_of(kill.killer, kill.killer_coalition)
        if victim_side != "own" and killer_side != "own":
            return None
        record: Record = {
            "turn": self.turn,
            "event": "kill",
            "victim_side": victim_side or "unknown",
            "victim": self.label(kill.target, kill.target_type, victim_side),
            "killer_side": killer_side or "unknown",
            "killer": (
                self.label(kill.killer, kill.killer_type, killer_side)
                if kill.killer or kill.killer_type
                else "unknown"
            ),
        }
        if kill.time is not None:
            record["t_min"] = round(kill.time / 60, 1)
        if kill.weapon:
            record["weapon"] = _weapon_label(kill.weapon)
        own_flight = self.feed.flight(
            kill.target if victim_side == "own" else kill.killer
        )
        if own_flight is not None:
            record["package"] = _flight_label(own_flight)
        if kill.alt is not None:
            record["altitude_ft"] = round(kill.alt / 0.3048 / 100) * 100
        record.update(_position(kill.x, kill.z))
        record.update(_where(self.debriefing, kill.x, kill.z))
        return record

    def build(self) -> list[Record]:
        records: list[Record] = []
        for shot in self.feed.shots:
            record = self.shot_record(shot)
            if record is not None:
                records.append(record)
        state = getattr(self.debriefing, "state_data", None)
        for kill in getattr(state, "kill_records", []):
            record = self.kill_record(kill)
            if record is not None:
                records.append(record)
        records.sort(key=lambda r: r.get("t_min", 0.0))
        return records[:MAX_RECORDS]


def build_combat_log(debriefing: Debriefing, player: Player, turn: int) -> list[Record]:
    """The side's combat log of a mission, oldest first."""
    if player.is_neutral:
        raise ValueError("A combat log needs a side, not neutral")
    return _LogBuilder(debriefing, player, turn).build()
