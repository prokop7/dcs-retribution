from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from game.commander.tasks.theatercommandertask import TheaterCommanderTask
from game.utils import nautical_miles

if TYPE_CHECKING:
    from game.commander.theaterstate import TheaterState


class TaskPostProcessor(ABC):
    @abstractmethod
    def apply(
        self,
        tasks: list[TheaterCommanderTask],
        state: TheaterState,
        initial_state: TheaterState,
    ) -> list[TheaterCommanderTask]: ...


@dataclass
class CombineCloseBaiTasks(TaskPostProcessor):
    def apply(
        self,
        tasks: list[TheaterCommanderTask],
        state: TheaterState,
        initial_state: TheaterState,
    ) -> list[TheaterCommanderTask]:
        from game.commander.tasks.primitive.armedrecon import PlanArmedReconFromBai
        from game.commander.tasks.primitive.bai import PlanBai

        bai_tasks: list[tuple[int, PlanBai]] = [
            (index, task)
            for index, task in enumerate(tasks)
            if isinstance(task, PlanBai)
        ]
        if len(bai_tasks) < 2:
            return tasks

        max_distance = nautical_miles(
            state.context.settings.armed_recon_engagement_range_distance
        ).meters

        unassigned = list(bai_tasks)
        clusters: list[list[tuple[int, PlanBai]]] = []
        while unassigned:
            index, bai_task = unassigned.pop(0)
            cluster = [(index, bai_task)]
            expanded = True
            while expanded:
                expanded = False
                remaining: list[tuple[int, PlanBai]] = []
                for other_index, other_bai_task in unassigned:
                    if (
                        bai_task.target.control_point
                        != other_bai_task.target.control_point
                    ):
                        remaining.append((other_index, other_bai_task))
                        continue
                    if any(
                        other_bai_task.target.distance_to(near.target) <= max_distance
                        for _, near in cluster
                    ):
                        cluster.append((other_index, other_bai_task))
                        expanded = True
                    else:
                        remaining.append((other_index, other_bai_task))
                unassigned = remaining
            clusters.append(sorted(cluster, key=lambda item: item[0]))

        if all(len(cluster) == 1 for cluster in clusters):
            return tasks

        replacements: dict[int, TheaterCommanderTask] = {}
        removals: set[int] = set()
        for cluster in clusters:
            if len(cluster) == 1:
                continue
            indices = [index for index, _ in cluster]
            battle_positions = [task.target for _, task in cluster]
            primary_index = indices[0]
            control_point = battle_positions[0].control_point
            target = battle_positions[0]

            original_tasks = [task for _, task in cluster]
            for task in original_tasks:
                task.release_package()

            replacement = PlanArmedReconFromBai(
                target,
                battle_positions,
                allow_missing_battle_positions=True,
            )
            planning_state = state.clone()
            initial_positions = initial_state.enemy_battle_positions.get(control_point)
            planning_positions = planning_state.enemy_battle_positions.get(
                control_point
            )
            if initial_positions and planning_positions:
                for battle_position in battle_positions:
                    if (
                        battle_position in initial_positions.blocking_capture
                        and battle_position not in planning_positions.blocking_capture
                    ):
                        planning_positions.blocking_capture.append(battle_position)
                    if (
                        battle_position in initial_positions.defending_front_line
                        and battle_position
                        not in planning_positions.defending_front_line
                    ):
                        planning_positions.defending_front_line.append(battle_position)

            if replacement.preconditions_met(planning_state):
                replacements[primary_index] = replacement
                removals.update(indices[1:])
            else:
                restore_state = state.clone()
                restore_positions = restore_state.enemy_battle_positions.get(
                    control_point
                )
                if initial_positions and restore_positions:
                    for battle_position in battle_positions:
                        if (
                            battle_position in initial_positions.blocking_capture
                            and battle_position
                            not in restore_positions.blocking_capture
                        ):
                            restore_positions.blocking_capture.append(battle_position)
                        if (
                            battle_position in initial_positions.defending_front_line
                            and battle_position
                            not in restore_positions.defending_front_line
                        ):
                            restore_positions.defending_front_line.append(
                                battle_position
                            )
                for task in original_tasks:
                    task.preconditions_met(restore_state)

        combined: list[TheaterCommanderTask] = []
        for index, planned_task in enumerate(tasks):
            if index in removals:
                continue
            if index in replacements:
                combined.append(replacements[index])
            else:
                combined.append(planned_task)
        return combined
