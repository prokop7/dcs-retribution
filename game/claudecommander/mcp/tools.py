"""Tool registry for the commander MCP endpoint.

Tools are plain functions taking a context object and a validated pydantic model.
The registry turns the models into JSON schemas for ``tools/list`` and validates
arguments for ``tools/call``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel, ValidationError

ContextT = TypeVar("ContextT")


class ToolError(Exception):
    """Raised by a tool handler to report a recoverable error back to Claude.

    The message is returned as an ``isError`` tool result rather than a JSON-RPC
    error, so the model can read it and correct its next call.
    """


class NoArguments(BaseModel):
    """Input model for tools that take no arguments."""


@dataclass(frozen=True)
class ToolResult:
    text: str
    is_error: bool = False

    def to_mcp(self) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": self.text}],
            "isError": self.is_error,
        }


@dataclass(frozen=True)
class Tool(Generic[ContextT]):
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[ContextT, Any], Any]
    read_only: bool

    def schema(self) -> dict[str, Any]:
        input_schema = self.input_model.model_json_schema()
        # MCP requires an object schema, even for tools without arguments.
        input_schema.setdefault("type", "object")
        input_schema.setdefault("properties", {})
        # The tool description already explains the input; docstrings of the input
        # model are for developers.
        input_schema.pop("title", None)
        input_schema.pop("description", None)
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": input_schema,
            "annotations": {"readOnlyHint": self.read_only},
        }


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json(exclude_none=True)
    return json.dumps(value, ensure_ascii=False, default=str)


class ToolRegistry(Generic[ContextT]):
    def __init__(self) -> None:
        self._tools: dict[str, Tool[ContextT]] = {}

    def register(
        self,
        name: str,
        description: str,
        input_model: type[BaseModel] = NoArguments,
        read_only: bool = False,
    ) -> Callable[[Callable[[ContextT, Any], Any]], Callable[[ContextT, Any], Any]]:
        def decorator(
            handler: Callable[[ContextT, Any], Any],
        ) -> Callable[[ContextT, Any], Any]:
            if name in self._tools:
                raise ValueError(f"Tool {name} is already registered")
            self._tools[name] = Tool(name, description, input_model, handler, read_only)
            return handler

        return decorator

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def list_schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def call(self, context: ContextT, name: str, arguments: Any) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool: {name}", is_error=True)
        try:
            args = tool.input_model.model_validate(arguments or {})
        except ValidationError as ex:
            return ToolResult(f"Invalid arguments for {name}: {ex}", is_error=True)
        try:
            return ToolResult(_render(tool.handler(context, args)))
        except ToolError as ex:
            return ToolResult(str(ex), is_error=True)
