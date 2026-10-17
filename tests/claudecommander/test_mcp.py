from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

from game.claudecommander.mcp.auth import CommanderTokens
from game.claudecommander.mcp.protocol import McpDispatcher
from game.server.mcp.routes import _authorize
from game.claudecommander.mcp.tools import ToolError, ToolRegistry
from game.theater.player import Player


class EchoArgs(BaseModel):
    text: str
    times: int = 1


def make_dispatcher() -> McpDispatcher[list[str]]:
    registry: ToolRegistry[list[str]] = ToolRegistry()

    @registry.register("echo", "Echoes text.", EchoArgs, read_only=True)
    def echo(context: list[str], args: EchoArgs) -> dict[str, Any]:
        context.append(args.text)
        return {"echo": args.text * args.times}

    @registry.register("fail", "Always fails.")
    def fail(context: list[str], args: BaseModel) -> None:
        raise ToolError("nope")

    return McpDispatcher(registry, "test", "1.0", "instructions")


def call(
    dispatcher: McpDispatcher[list[str]], message: dict[str, Any], log: list[str]
) -> Any:
    return dispatcher.handle(message, lambda: log)


def test_initialize_echoes_supported_version() -> None:
    response = call(
        make_dispatcher(),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        [],
    )
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert response["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert response["result"]["instructions"] == "instructions"


def test_initialize_unknown_version_falls_back_to_latest() -> None:
    response = call(
        make_dispatcher(),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"},
        },
        [],
    )
    assert response["result"]["protocolVersion"] == "2025-11-25"


def test_notifications_get_no_response() -> None:
    assert (
        call(
            make_dispatcher(),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            [],
        )
        is None
    )


def test_tools_list_has_object_schemas() -> None:
    response = call(
        make_dispatcher(), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, []
    )
    tools = {t["name"]: t for t in response["result"]["tools"]}
    assert tools["echo"]["inputSchema"]["required"] == ["text"]
    assert tools["echo"]["annotations"] == {"readOnlyHint": True}
    assert tools["fail"]["inputSchema"] == {"type": "object", "properties": {}}


def test_tools_call_validates_and_runs() -> None:
    log: list[str] = []
    response = call(
        make_dispatcher(),
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "ab", "times": 2}},
        },
        log,
    )
    assert response["result"] == {
        "content": [{"type": "text", "text": '{"echo": "abab"}'}],
        "isError": False,
    }
    assert log == ["ab"]


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"name": "echo", "arguments": {"times": 2}}, "Invalid arguments for echo"),
        ({"name": "fail"}, "nope"),
        ({"name": "missing"}, "Unknown tool: missing"),
    ],
)
def test_tool_errors_are_results(params: dict[str, Any], expected: str) -> None:
    response = call(
        make_dispatcher(),
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": params},
        [],
    )
    assert response["result"]["isError"] is True
    assert expected in response["result"]["content"][0]["text"]


def test_unknown_method() -> None:
    response = call(
        make_dispatcher(), {"jsonrpc": "2.0", "id": 5, "method": "resources/list"}, []
    )
    assert response["error"]["code"] == -32601


def test_batch() -> None:
    response = make_dispatcher().handle(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ],
        lambda: [],
    )
    assert response == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_tokens_bind_to_one_side() -> None:
    token = CommanderTokens.issue(Player.RED)
    try:
        assert _authorize("red", f"Bearer {token}") is Player.RED
        with pytest.raises(HTTPException) as forbidden:
            _authorize("blue", f"Bearer {token}")
        assert forbidden.value.status_code == 403
        with pytest.raises(HTTPException) as unauthorized:
            _authorize("red", None)
        assert unauthorized.value.status_code == 401
    finally:
        CommanderTokens.revoke(token)
    with pytest.raises(HTTPException):
        _authorize("red", f"Bearer {token}")
