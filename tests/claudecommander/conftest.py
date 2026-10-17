"""Fixtures that build a real campaign for the commander tool tests.

Generating a game takes several seconds, so the game is built once per test
session. Tests must leave it as they found it: undo any mutation in a
``try``/``finally`` (see the damage tests in test_intel_tools.py).
"""

from __future__ import annotations

import logging
import pickle
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pytest
from dcs.payloads import PayloadDirectories

from game import Game, naming, persistency
from game.ato.closestairfields import ObjectiveDistanceCache
from game.campaignloader.campaign import Campaign, DEFAULT_BUDGET
from game.server.eventstream import EventStream
from game.factions import FACTIONS
from game.settings import Settings
from game.theater.start_generator import GameGenerator, GeneratorSettings, ModSettings

#: Small Syria campaign with SAMs, EWRs, buildings, a front line and a carrier.
TEST_CAMPAIGN = Path("resources/campaigns/golan_heights_lite.yaml")


def generate_test_game(campaign_path: Path, user_folder: Path) -> Game:
    """Builds a game the way the ``new-game`` CLI subcommand does, without Qt.

    Unlike the CLI, aircraft and ground procurement is automated for BLUE too, so
    that after passing a turn both sides own squadrons and have a planned ATO.
    """
    persistency.setup(str(user_folder), prefer_liberation_payloads=False, port=0)
    PayloadDirectories.set_fallback(Path("resources/customized_payloads").resolve())

    campaign = Campaign.from_file(campaign_path)
    theater = campaign.load_theater(campaign.advanced_iads)
    start_date = datetime(2011, 6, 1)
    if campaign.recommended_start_date is not None:
        start_date = datetime.combine(
            campaign.recommended_start_date, datetime.min.time()
        )
    settings = Settings(
        automate_runway_repair=True,
        automate_front_line_reinforcements=True,
        automate_aircraft_reinforcements=True,
    )
    generator = GameGenerator(
        FACTIONS[campaign.recommended_player_faction],
        FACTIONS[campaign.recommended_enemy_faction],
        theater,
        campaign.load_air_wing_config(theater),
        settings,
        GeneratorSettings(
            start_date=start_date,
            start_time=campaign.recommended_start_time,
            player_budget=DEFAULT_BUDGET,
            enemy_budget=DEFAULT_BUDGET,
            inverted=False,
            advanced_iads=theater.iads_network.advanced_iads,
            no_carrier=False,
            no_lha=False,
            no_player_navy=False,
            no_enemy_navy=False,
            tgo_config=campaign.load_ground_forces_config(),
            carrier_config=campaign.load_carrier_config(),
            squadrons_start_full=False,
        ),
        ModSettings(),
    )
    game = generator.generate()
    game.begin_turn_0(squadrons_start_full=False)
    # Turn 0 only procures. Passing the turn plans missions for both sides.
    game.pass_turn()
    return game


@pytest.fixture(scope="session")
def test_game(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Game]:
    previous_level = logging.getLogger().level
    logging.getLogger().setLevel(logging.ERROR)
    try:
        game = generate_test_game(TEST_CAMPAIGN, tmp_path_factory.mktemp("dcs"))
    finally:
        logging.getLogger().setLevel(previous_level)
    yield game


@pytest.fixture
def game(test_game: Game) -> Iterator[Game]:
    """A private copy of the test game for tests that change it.

    The copy goes through pickle like a save and load (about half a second), so
    tests can mutate it freely. Process-wide caches that point at a game (the
    airfield distance cache, the name generator) are switched to the copy and
    back.
    """
    copy = pickle.loads(pickle.dumps(test_game))
    original_namegen = naming.namegen
    copy.on_load(game_still_initializing=True)
    try:
        yield copy
    finally:
        ObjectiveDistanceCache.set_theater(test_game.theater)
        naming.namegen = original_namegen
        EventStream.drain()
