from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, TYPE_CHECKING

from game.armedforces.armedforces import ArmedForces
from game.ato.airtaaskingorder import AirTaskingOrder
from game.campaignloader.defaultsquadronassigner import DefaultSquadronAssigner
from game.claudecommander.state import CommanderState
from game.commander import TheaterCommander
from game.commander.missionscheduler import MissionScheduler
from game.income import Income
from game.navmesh import NavMesh
from game.orderedset import OrderedSet
from game.procurement import AircraftProcurementRequest, ProcurementAi
from game.profiling import MultiEventTracer, logged_duration
from game.squadrons import AirWing
from game.theater.bullseye import Bullseye
from game.theater.player import Player
from game.theater.transitnetwork import TransitNetwork, TransitNetworkBuilder
from game.threatzones import ThreatZones
from game.transfers import PendingTransfers

if TYPE_CHECKING:
    from .campaignloader import CampaignAirWingConfig
    from .data.doctrine import Doctrine
    from .factions.faction import Faction
    from .game import Game
    from .lasercodes import LaserCodeRegistry
    from .sim import GameUpdateEvents


class Coalition:
    def __init__(
        self, game: Game, faction: Faction, budget: float, player: Player
    ) -> None:
        self.game = game
        self.player = player
        self.faction = faction
        self.budget = budget
        self.ato = AirTaskingOrder()
        self.transit_network = TransitNetwork()
        self.procurement_requests: OrderedSet[AircraftProcurementRequest] = OrderedSet()
        self.bullseye = Bullseye(self.game.point_in_world(0, 0))
        self.air_wing = AirWing(player, game, self.faction)
        self.armed_forces = ArmedForces(self.faction)
        self.transfers = PendingTransfers(game, player)
        self.commander = CommanderState()

        # Late initialized because the two coalitions in the game are mutually
        # dependent, so must be both constructed before this property can be set.
        self._opponent: Optional[Coalition] = None

        # Volatile properties that are not persisted to the save file since they can be
        # recomputed on load. Keeping this data out of the save file makes save compat
        # breaks less frequent. Each of these properties has a non-underscore-prefixed
        # @property that should be used for non-Optional access.
        #
        # All of these are late-initialized, but will be non-None after the game has
        # finished loading.
        self._threat_zone: Optional[ThreatZones] = None
        self._navmesh: Optional[NavMesh] = None

    @property
    def doctrine(self) -> Doctrine:
        return self.faction.doctrine

    @property
    def coalition_id(self) -> int:
        if self.player.is_blue:
            return 2
        elif self.player.is_red:
            return 1
        return 0

    @property
    def opponent(self) -> Coalition:
        assert self._opponent is not None
        return self._opponent

    @property
    def threat_zone(self) -> ThreatZones:
        assert self._threat_zone is not None
        return self._threat_zone

    @property
    def nav_mesh(self) -> NavMesh:
        assert self._navmesh is not None
        return self._navmesh

    @property
    def laser_code_registry(self) -> LaserCodeRegistry:
        return self.game.laser_code_registry

    def __getstate__(self) -> dict[str, Any]:
        state = self.__dict__.copy()
        # Avoid persisting any volatile types that can be deterministically
        # recomputed on load for the sake of save compatibility.
        # TODO: Figure out why this is needed after adding neutral point support
        if state["player"] != Player.NEUTRAL:
            del state["_threat_zone"]
            del state["_navmesh"]
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        # Migration: Convert old boolean player values to Player enum
        if "player" in state and isinstance(state["player"], bool):
            from game.theater.player import Player

            if state["player"]:
                state["player"] = Player.BLUE
            else:
                state["player"] = Player.RED

        if "commander" not in state:
            state["commander"] = CommanderState()

        self.__dict__.update(state)

    def set_opponent(self, opponent: Coalition) -> None:
        if self._opponent is not None:
            raise RuntimeError("Double-initialization of Coalition.opponent")
        self._opponent = opponent

    def configure_default_air_wing(
        self, air_wing_config: CampaignAirWingConfig
    ) -> None:
        DefaultSquadronAssigner(air_wing_config, self.game, self).assign()

    def adjust_budget(self, amount: float) -> None:
        self.budget += amount

    def compute_threat_zones(self, events: GameUpdateEvents) -> None:
        self._threat_zone = ThreatZones.for_faction(self.game, self.player)
        events.update_threat_zones(self.player, self._threat_zone)

    def compute_nav_meshes(self, events: GameUpdateEvents) -> None:
        self._navmesh = NavMesh.from_threat_zones(
            self.opponent.threat_zone, self.game.theater
        )
        events.update_navmesh(self.player, self._navmesh)

    def update_transit_network(self) -> None:
        self.transit_network = TransitNetworkBuilder(
            self.game.theater, self.player
        ).build()

    def set_bullseye(self, bullseye: Bullseye) -> None:
        self.bullseye = bullseye

    def end_turn(self) -> None:
        """Processes coalition-specific turn finalization.

        For more information on turn finalization in general, see the documentation for
        `Game.finish_turn`.
        """
        self.air_wing.end_turn()
        self.budget += Income(self.game, self.player).total

        # Need to recompute before transfers and deliveries to account for captures.
        # This happens in in initialize_turn as well, because cheating doesn't advance a
        # turn but can capture bases so we need to recompute there as well.
        self.update_transit_network()

        # Must happen *before* unit deliveries are handled, or else new units will spawn
        # one hop ahead. ControlPoint.process_turn handles unit deliveries. The
        # coalition-specific turn-end happens before the theater-wide turn-end, so this
        # is handled correctly.
        self.transfers.perform_transfers()

    def preinit_turn_0(self, squadrons_start_full: bool) -> None:
        """Runs final Coalition initialization.

        Final initialization occurs before Game.initialize_turn runs for turn 0.
        """
        self.air_wing.populate_for_turn_0(squadrons_start_full)

    def initialize_turn(self, is_turn_0: bool, events: GameUpdateEvents) -> None:
        """Processes coalition-specific turn initialization.

        For more information on turn initialization in general, see the documentation
        for `Game.initialize_turn`.
        """
        if self.claude_commanded:
            self._initialize_turn_for_claude(events)
            return

        # Needs to happen *before* planning transfers so we don't cancel them.
        self.ato.clear()
        self.air_wing.reset()
        self.refund_outstanding_orders()
        self.procurement_requests.clear()

        with logged_duration("Transit network identification"):
            self.update_transit_network()
        with logged_duration("Procurement of airlift assets"):
            self.transfers.order_airlift_assets()
        with logged_duration("Transport planning"):
            self.transfers.plan_transports(self.game.conditions.start_time)

        if not is_turn_0:
            self.plan_missions(self.game.conditions.start_time)
        self.plan_procurement(events)

    @property
    def claude_commanded(self) -> bool:
        from game.claudecommander.coordinator import commander_enabled

        return commander_enabled(self.game, self.player)

    def _initialize_turn_for_claude(self, events: GameUpdateEvents) -> None:
        """Turn initialization for a side planned by Claude.

        At the start of a turn the ATO is cleared like for the built-in planner. When
        the turn is re-initialized after a change (a base capture, a bought SAM, new
        conditions, ...) the commander's plan is kept and only packages the change
        made impossible are dropped; the commander is then asked to adjust its plan.
        Orders are never refunded: procurement is the commander's decision.
        """
        from game.claudecommander.coordinator import CommanderCoordinator

        coordinator = CommanderCoordinator.get()
        new_turn = self.commander.planned_turn != self.game.turn
        if new_turn and not coordinator.is_planning(self.player):
            self.ato.clear()
            self.air_wing.reset()
        else:
            self.drop_invalid_packages(events)
        self.procurement_requests.clear()

        with logged_duration("Transit network identification"):
            self.update_transit_network()
        with logged_duration("Procurement of airlift assets"):
            self.transfers.order_airlift_assets()
        with logged_duration("Transport planning"):
            self.transfers.plan_transports(self.game.conditions.start_time)

        coordinator.request(self.game, self.player)

    def drop_invalid_packages(self, events: GameUpdateEvents) -> list[str]:
        """Removes packages whose target or departure no longer fits this side.

        Returns descriptions of the removed packages.
        """
        removed: list[str] = []
        for package in list(self.ato.packages):
            target_valid = package.primary_task in set(
                package.target.mission_types(self.player)
            )
            departures_valid = all(
                f.departure.is_friendly(self.player) for f in package.flights
            )
            if target_valid and departures_valid:
                continue
            events.delete_flights_in_package(package)
            self.ato.remove_package(package)
            removed.append(f"{package.primary_task} package against {package.target}")
        for description in removed:
            self.commander.record_change(
                f"Removed your {description}: its target or base changed hands."
            )
        return removed

    def hand_over_to_claude(self, events: GameUpdateEvents) -> None:
        """Undoes this turn's built-in planning when Claude takes command mid-turn.

        The built-in planner and procurement AI already ran for this turn, so
        without this the commander would start with their packages in its ATO and
        their purchases already paid for. Packages the player made are kept, and
        for OWNFOR only purchases in domains the commander owns (the automated
        ones) are refunded, so the player's own orders stay.
        """
        for package in list(self.ato.packages):
            if package.player_locked:
                continue
            events.delete_flights_in_package(package)
            self.ato.remove_package(package)
        self.procurement_requests.clear()
        settings = self.game.settings
        if self.player.is_red or settings.automate_front_line_reinforcements:
            for cp in self.game.theater.control_points_for(self.player):
                cp.ground_unit_orders.refund_all(self)
        if self.player.is_red or settings.automate_aircraft_reinforcements:
            for squadron in self.air_wing.iter_squadrons():
                squadron.refund_orders()
        # A full turn run, even if the commander planned this turn before.
        self.commander.planned_turn = min(
            self.commander.planned_turn, self.game.turn - 1
        )

    def replan_ground_war(self) -> None:
        """Re-plans this side's front line groups for its current stances.

        Turn initialization plans the ground war before the commander runs, so
        stances the commander sets would otherwise only size the groups next turn.
        """
        from game.ground_forces.ai_ground_planner import GroundPlanner

        for cp in self.game.theater.control_points_for(self.player):
            if cp.has_frontline:
                planner = GroundPlanner(cp, self.game)
                planner.plan_groundwar()
                self.game.ground_planners[cp.id] = planner

    def run_builtin_planning(self, events: GameUpdateEvents) -> None:
        """Plans this turn with the built-in planner, for when Claude can't."""
        self.ato.clear()
        self.air_wing.reset()
        self.procurement_requests.clear()
        if self.game.turn > 0:
            self.plan_missions(self.game.conditions.start_time)
        self.plan_procurement(events)

    def refund_outstanding_orders(self) -> None:
        # TODO: Split orders between air and ground units.
        # This isn't quite right. If the player has ground purchases automated we should
        # be refunding the ground units, and if they have air automated but not ground
        # we should be refunding air units.
        if self.player and not self.game.settings.automate_aircraft_reinforcements:
            return

        for cp in self.game.theater.control_points_for(self.player):
            cp.ground_unit_orders.refund_all(self)
        for squadron in self.air_wing.iter_squadrons():
            squadron.refund_orders()

    def plan_missions(self, now: datetime) -> None:
        color = "Blue" if self.player.is_blue else "Red"
        with MultiEventTracer() as tracer:
            with tracer.trace(f"{color} mission planning"):
                with tracer.trace(f"{color} mission identification"):
                    TheaterCommander(self.game, self.player).plan_missions(now, tracer)
                with tracer.trace(f"{color} mission scheduling"):
                    MissionScheduler(
                        self, self.game.settings.desired_player_mission_duration
                    ).schedule_missions(now)

    def plan_procurement(self, events: GameUpdateEvents) -> None:
        # The first turn needs to buy a *lot* of aircraft to fill CAPs, so it gets much
        # more of the budget that turn. Otherwise budget (after repairs) is split evenly
        # between air and ground. For the default starting budget of 2000 this gives 600
        # to ground forces and 1400 to aircraft. After that the budget will be spent
        # proportionally based on how much is already invested.

        if self.player.is_blue:
            manage_runways = self.game.settings.automate_runway_repair
            manage_front_line = self.game.settings.automate_front_line_reinforcements
            manage_aircraft = self.game.settings.automate_aircraft_reinforcements
            manage_ground_objects = self.game.settings.automate_ground_object_repairs
            manage_buildings = self.game.settings.automate_building_repairs
        else:
            manage_runways = True
            manage_front_line = True
            manage_aircraft = True
            manage_ground_objects = self.game.settings.automate_ground_object_repairs
            manage_buildings = self.game.settings.automate_building_repairs

        self.budget = ProcurementAi(
            self.game,
            self.player,
            self.faction,
            manage_runways=manage_runways,
            manage_front_line=manage_front_line,
            manage_aircraft=manage_aircraft,
            manage_ground_objects=manage_ground_objects,
            manage_buildings=manage_buildings,
        ).spend_budget(self.budget, events)

    def add_procurement_request(self, request: AircraftProcurementRequest) -> None:
        self.procurement_requests.add(request)
