import json
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from game.claudecommander.context import CommanderContext, GAME_LOCK
from game.server.dependencies import GameContext
from game.server.eventstream import EventStream
from game.claudecommander.mcp.auth import CommanderTokens
from game.theater.player import Player

router: APIRouter = APIRouter(prefix="/mcp")


def _authorize(side: str, authorization: Optional[str]) -> Player:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    player = CommanderTokens.player_for(authorization.removeprefix("Bearer "))
    if player is None or player.value.lower() != side.lower():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return player


def _dispatch(
    player: Player, payload: Any
) -> Optional[dict[str, Any] | list[dict[str, Any]]]:
    # Imported here: the tools import game.server modules, which import this one.
    from game.claudecommander.mcpserver import commander_dispatcher

    # Tool calls touch the game model, so keep them off the event loop and
    # serialized with every other commander call.
    with GAME_LOCK, EventStream.event_context() as events:
        game = GameContext.require()

        def context() -> CommanderContext:
            # Only built for tool calls; protocol messages never touch the game.
            sightings = tuple(game.coalition_for(player).commander.sightings)
            return CommanderContext(game, player, events, sightings)

        return commander_dispatcher.handle(payload, context)


@router.post("/{side}", operation_id="commander_mcp", include_in_schema=False)
async def commander_mcp(
    side: str, request: Request, authorization: Optional[str] = Header(default=None)
) -> Response:
    player = _authorize(side, authorization)
    try:
        payload: Any = json.loads(await request.body())
    except json.JSONDecodeError:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    response = await run_in_threadpool(_dispatch, player, payload)
    if response is None:
        return Response(status_code=status.HTTP_202_ACCEPTED)
    return JSONResponse(response)


@router.get("/{side}", include_in_schema=False)
def commander_mcp_stream(side: str) -> Response:
    # We never push server-initiated messages, so no SSE stream is offered.
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED)


@router.delete("/{side}", include_in_schema=False)
def commander_mcp_close(side: str) -> Response:
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED)
