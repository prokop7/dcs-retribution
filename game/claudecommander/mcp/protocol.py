"""Minimal MCP (Model Context Protocol) JSON-RPC dispatcher.

Only the parts of the protocol needed for a tools-only server are implemented:
``initialize``, ``ping``, ``tools/list``, ``tools/call`` and notifications. We avoid
the official SDK because it requires a newer anyio than the one pinned by the rest
of the server.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Generic, Optional, TypeVar

from game.claudecommander.mcp.tools import ToolRegistry

ContextT = TypeVar("ContextT")

SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class McpDispatcher(Generic[ContextT]):
    def __init__(
        self,
        registry: ToolRegistry[ContextT],
        server_name: str,
        server_version: str,
        instructions: Optional[str] = None,
    ) -> None:
        self.registry = registry
        self.server_name = server_name
        self.server_version = server_version
        self.instructions = instructions

    def handle(
        self, payload: Any, context_factory: Callable[[], ContextT]
    ) -> Optional[dict[str, Any] | list[dict[str, Any]]]:
        """Handles a JSON-RPC message or batch.

        Returns None when there is nothing to send back (notifications only).
        """
        if isinstance(payload, list):
            responses = [self._handle_one(m, context_factory) for m in payload]
            filtered = [r for r in responses if r is not None]
            return filtered or None
        return self._handle_one(payload, context_factory)

    def _handle_one(
        self, message: Any, context_factory: Callable[[], ContextT]
    ) -> Optional[dict[str, Any]]:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "Invalid JSON-RPC message")

        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message
        if method is None:
            # A response from the client to a server request. We never send any.
            return None
        if is_notification:
            return None

        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object")

        try:
            if method == "initialize":
                return _result(request_id, self._initialize(params))
            if method == "ping":
                return _result(request_id, {})
            if method == "tools/list":
                return _result(request_id, {"tools": self.registry.list_schemas()})
            if method == "tools/call":
                name = params.get("name")
                if not isinstance(name, str):
                    return _error(request_id, INVALID_PARAMS, "Missing tool name")
                result = self.registry.call(
                    context_factory(), name, params.get("arguments")
                )
                return _result(request_id, result.to_mcp())
        except Exception as ex:  # pragma: no cover - defensive
            logging.exception("MCP request %s failed", method)
            return _error(request_id, INTERNAL_ERROR, f"{type(ex).__name__}: {ex}")
        return _error(request_id, METHOD_NOT_FOUND, f"Unknown method: {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if requested in SUPPORTED_PROTOCOL_VERSIONS:
            version = requested
        else:
            version = SUPPORTED_PROTOCOL_VERSIONS[0]
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.server_name, "version": self.server_version},
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result
