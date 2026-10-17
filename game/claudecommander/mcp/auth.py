import secrets
import threading
from typing import Optional

from game.theater.player import Player


class CommanderTokens:
    """Bearer tokens that bind an MCP connection to one side.

    A token is issued for each planning run and revoked when the run ends, so a
    commander process can only ever see its own side's endpoint.
    """

    _lock = threading.Lock()
    _tokens: dict[str, Player] = {}

    @classmethod
    def issue(cls, player: Player) -> str:
        token = secrets.token_urlsafe(32)
        with cls._lock:
            cls._tokens[token] = player
        return token

    @classmethod
    def revoke(cls, token: str) -> None:
        with cls._lock:
            cls._tokens.pop(token, None)

    @classmethod
    def player_for(cls, token: str) -> Optional[Player]:
        with cls._lock:
            return cls._tokens.get(token)
