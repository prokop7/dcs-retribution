"""Which decisions a commander owns.

RED commanders own everything. A BLUE commander shares the side with the player,
so it only owns the domains the player handed to HQ automation in the settings
(design doc, open point 2). Air tasking, ground unit transfers and squadron
relocation always belong to the commander; the player can still edit or add
packages, which then become locked (``Package.player_locked``).
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, TYPE_CHECKING

from game.claudecommander.mcp.tools import ToolError

if TYPE_CHECKING:
    from game import Game
    from game.theater.player import Player


class Domain(Enum):
    AIR_TASKING = "air_tasking"
    AIRCRAFT_PURCHASE = "aircraft_purchase"
    GROUND_UNIT_PURCHASE = "ground_unit_purchase"
    FRONT_LINE_STANCE = "front_line_stance"
    RUNWAY_REPAIR = "runway_repair"
    SITE_REPAIR = "site_repair"
    BUILDING_REPAIR = "building_repair"
    TRANSFERS = "transfers"


#: The HQ automation setting that hands a domain to a BLUE commander. Domains
#: without a setting are always owned.
BLUE_DOMAIN_SETTINGS: dict[Domain, str] = {
    Domain.AIRCRAFT_PURCHASE: "automate_aircraft_reinforcements",
    Domain.GROUND_UNIT_PURCHASE: "automate_front_line_reinforcements",
    Domain.FRONT_LINE_STANCE: "automate_front_line_stance",
    Domain.RUNWAY_REPAIR: "automate_runway_repair",
    Domain.SITE_REPAIR: "automate_ground_object_repairs",
    Domain.BUILDING_REPAIR: "automate_building_repairs",
}

_DESCRIPTIONS: dict[Domain, str] = {
    Domain.AIR_TASKING: "air packages and flights",
    Domain.AIRCRAFT_PURCHASE: "aircraft purchases",
    Domain.GROUND_UNIT_PURCHASE: "ground unit purchases",
    Domain.FRONT_LINE_STANCE: "front line stances",
    Domain.RUNWAY_REPAIR: "runway repairs",
    Domain.SITE_REPAIR: "repairing, buying and rebuilding air defense and other "
    "unit sites",
    Domain.BUILDING_REPAIR: "building repairs",
    Domain.TRANSFERS: "ground unit transfers and squadron relocation",
}


def _setting_for(domain: Domain) -> Optional[str]:
    return BLUE_DOMAIN_SETTINGS.get(domain)


def owns_domain(game: Game, player: Player, domain: Domain) -> bool:
    if not player.is_blue:
        return True
    setting = _setting_for(domain)
    if setting is None:
        return True
    return bool(getattr(game.settings, setting))


def owned_domains(game: Game, player: Player) -> list[str]:
    return [d.value for d in Domain if owns_domain(game, player, d)]


def require_domain(game: Game, player: Player, domain: Domain) -> None:
    """Raises a ToolError if the player, not the commander, controls ``domain``."""
    if owns_domain(game, player, domain):
        return
    setting = _setting_for(domain)
    raise ToolError(
        f"The player controls {_DESCRIPTIONS[domain]} for your side (HQ automation "
        f"setting '{setting}' is off), so you cannot do this. Your domains: "
        f"{', '.join(owned_domains(game, player))}."
    )
