from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

MCP_SERVER_NAME = "retribution"

#: Effort levels accepted by ``claude --effort``.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ClaudeRunConfig:
    """Everything needed to launch one commander run of the Claude Code CLI."""

    executable: str
    model: str
    effort: str
    timeout: timedelta
    server_port: int
    #: The game server's bind address (ServerSettings.server_bind_address).
    server_host: str = "::1"

    def mcp_url(self, side: str) -> str:
        return f"http://{connect_host(self.server_host)}:{self.server_port}/mcp/{side.lower()}"


def connect_host(bind_address: str) -> str:
    """The host a local client uses to reach a server bound to ``bind_address``.

    The game server binds to ``::1`` by default, so connecting to 127.0.0.1 fails.
    Wildcard binds are reached through the matching loopback address, and IPv6
    literals are bracketed for use in a URL.
    """
    host = bind_address.strip() or "::1"
    if host == "0.0.0.0":
        return "127.0.0.1"
    if host == "::":
        host = "::1"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host
