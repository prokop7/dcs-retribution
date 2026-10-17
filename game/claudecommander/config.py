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

    def mcp_url(self, side: str) -> str:
        return f"http://127.0.0.1:{self.server_port}/mcp/{side.lower()}"
