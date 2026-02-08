from __future__ import annotations

from dataclasses import dataclass, field

from game.ato.flighttype import FlightType
from game.commander.missionproposals import EscortType
from game.commander.tasks.packageplanningtask import PackagePlanningTask
from game.commander.theaterstate import TheaterState
from game.data.groups import GroupTask
from game.theater import ControlPoint
from game.theater.theatergroundobject import VehicleGroupGroundObject
from game.utils import nautical_miles


@dataclass
class PlanArmedRecon(PackagePlanningTask[ControlPoint]):
    def preconditions_met(self, state: TheaterState) -> bool:
        if self.target not in state.control_point_priority_queue:
            return False
        if not self.target_area_preconditions_met(state):
            return False
        return super().preconditions_met(state)

    def apply_effects(self, state: TheaterState) -> None:
        state.control_point_priority_queue.remove(self.target)
        super().apply_effects(state)

    def propose_flights(self) -> None:
        self.propose_flight(FlightType.ARMED_RECON, self.get_flight_size())
        self.propose_common_escorts()


@dataclass
class PlanArmedReconFromBai(PackagePlanningTask[VehicleGroupGroundObject]):
    battle_positions: list[VehicleGroupGroundObject]
    # BAI target are already removed from state
    allow_missing_battle_positions: bool = False
    _planning_state: TheaterState | None = field(init=False, default=None, repr=False)

    def preconditions_met(self, state: TheaterState) -> bool:
        if not self.battle_positions:
            return False
        selected_target = self._select_best_target(state)
        if selected_target is None:
            return False
        self.target = selected_target
        self._planning_state = state
        if not self.allow_missing_battle_positions:
            if not any(
                state.has_battle_position(battle_position)
                for battle_position in self.battle_positions
            ):
                return False
        if not self.target_area_preconditions_met(state):
            return False
        return super().preconditions_met(state)

    def apply_effects(self, state: TheaterState) -> None:
        for battle_position in self.battle_positions:
            if state.has_battle_position(battle_position):
                state.eliminate_battle_position(battle_position)
        super().apply_effects(state)

    def propose_flights(self) -> None:
        cas_aircraft = self._cas_aircraft_count()
        self.propose_flight(FlightType.ARMED_RECON, cas_aircraft)

        escort_aircraft = self._escort_aircraft_count(cas_aircraft)
        if escort_aircraft > 0:
            self.propose_flight(FlightType.ESCORT, escort_aircraft, EscortType.AirToAir)

        if self._needs_sead():
            self.propose_flight(FlightType.SEAD_ESCORT, 2, EscortType.Sead)
            self.propose_flight(FlightType.SEAD_SWEEP, 2, EscortType.Sead)

    def _cas_aircraft_count(self) -> int:
        unit_count = self._covered_armored_unit_count()
        return (unit_count + 3) // 4 + 1

    @staticmethod
    def _escort_aircraft_count(cas_aircraft: int) -> int:
        return (cas_aircraft + 1) // 2

    def _select_best_target(
        self, state: TheaterState
    ) -> VehicleGroupGroundObject | None:
        all_enemy_groups = self._all_enemy_armored_groups(state)
        if not all_enemy_groups:
            return None

        engagement_range_meters = nautical_miles(
            state.context.settings.armed_recon_engagement_range_distance
        ).meters

        best_target = None
        best_score = (-1, -1, -1)
        for candidate in self.battle_positions:
            covered_groups = [
                group
                for group in all_enemy_groups
                if candidate.distance_to(group) <= engagement_range_meters
            ]
            covered_units = sum(group.alive_unit_count for group in covered_groups)
            score = (
                len(covered_groups),
                covered_units,
                candidate.alive_unit_count,
            )
            if score > best_score:
                best_target = candidate
                best_score = score
        return best_target

    def _covered_armored_unit_count(self) -> int:
        if not self.target:
            return 0
        covered_groups = self._covered_armored_groups()
        return sum(group.alive_unit_count for group in covered_groups)

    def _covered_armored_groups(self) -> list[VehicleGroupGroundObject]:
        if self._planning_state is None or not self.target:
            return []
        all_enemy_groups = self._all_enemy_armored_groups(self._planning_state)
        if not all_enemy_groups:
            return []
        engagement_range_meters = nautical_miles(
            self._planning_state.context.settings.armed_recon_engagement_range_distance
        ).meters
        return [
            group
            for group in all_enemy_groups
            if self.target.distance_to(group) <= engagement_range_meters
        ]

    def _all_enemy_armored_groups(
        self, state: TheaterState
    ) -> list[VehicleGroupGroundObject]:
        enemy_cps = state.context.theater.control_points_for(
            state.context.coalition.player.opponent
        )
        armored_groups = []
        for cp in enemy_cps:
            for tgo in cp.ground_objects:
                if isinstance(tgo, VehicleGroupGroundObject) and not tgo.is_dead:
                    armored_groups.append(tgo)
        return armored_groups

    def _needs_sead(self) -> bool:
        if self._planning_state is None or not self.target:
            return False
        armored_groups = self._covered_armored_groups()
        if not armored_groups:
            return False
        shorad_tasks = {GroupTask.SHORAD, GroupTask.MERAD}
        for air_defense in self._planning_state.enemy_air_defenses:
            if air_defense.task not in shorad_tasks:
                continue
            threat_range = air_defense.max_threat_range().meters
            for group in armored_groups:
                if air_defense.distance_to(group) <= threat_range:
                    return True
        return False
