"""MCP server definition for the commander endpoint."""

from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.mcp.protocol import McpDispatcher
from game.version import VERSION

# Importing the tool modules registers their tools.
from game.claudecommander import tools as _tools  # noqa: F401

SERVER_NAME = "retribution"

INSTRUCTIONS = (
    "Tools for commanding one side of a DCS Retribution campaign. Everything you "
    "can see is limited to what your side knows. Changes take effect immediately "
    "and are shown to the player as you make them."
)

commander_dispatcher: McpDispatcher[CommanderContext] = McpDispatcher(
    commander_tools, SERVER_NAME, VERSION, INSTRUCTIONS
)
