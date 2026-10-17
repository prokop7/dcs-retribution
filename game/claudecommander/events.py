"""Parsing of Claude Code ``--output-format stream-json`` events.

Only the events the planner cares about are modeled. Anything else becomes an
``OtherEvent`` so that new CLI event types never break planning.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Union


@dataclass(frozen=True)
class InitEvent:
    session_id: str
    model: str


@dataclass(frozen=True)
class TextEvent:
    text: str


@dataclass(frozen=True)
class ThinkingEvent:
    text: str


@dataclass(frozen=True)
class ToolUseEvent:
    tool_use_id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResultEvent:
    tool_use_id: str
    text: str
    is_error: bool


@dataclass(frozen=True)
class ResultEvent:
    session_id: Optional[str]
    is_error: bool
    subtype: str
    result: str
    total_cost_usd: Optional[float] = None
    num_turns: Optional[int] = None
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OtherEvent:
    type: str
    raw: dict[str, Any]


StreamEvent = Union[
    InitEvent, TextEvent, ThinkingEvent, ToolUseEvent, ToolResultEvent, ResultEvent
]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def parse_line(line: str) -> list[StreamEvent | OtherEvent]:
    """Parses one line of stream-json output into zero or more events."""
    line = line.strip()
    if not line:
        return []
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return [OtherEvent("unparsable", {"line": line})]
    if not isinstance(data, dict):
        return [OtherEvent("unparsable", {"line": line})]

    event_type = data.get("type", "")
    if event_type == "system" and data.get("subtype") == "init":
        return [InitEvent(data.get("session_id", ""), data.get("model", ""))]

    if event_type == "assistant":
        events: list[StreamEvent | OtherEvent] = []
        for block in data.get("message", {}).get("content", []):
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                events.append(TextEvent(block["text"]))
            elif kind == "thinking" and block.get("thinking"):
                events.append(ThinkingEvent(block["thinking"]))
            elif kind == "tool_use":
                events.append(
                    ToolUseEvent(
                        block.get("id", ""),
                        block.get("name", ""),
                        block.get("input") or {},
                    )
                )
        return events

    if event_type == "user":
        content = data.get("message", {}).get("content", [])
        if not isinstance(content, list):
            return []
        return [
            ToolResultEvent(
                block.get("tool_use_id", ""),
                _content_text(block.get("content")),
                bool(block.get("is_error", False)),
            )
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]

    if event_type == "result":
        errors = data.get("errors") or []
        return [
            ResultEvent(
                session_id=data.get("session_id"),
                is_error=bool(data.get("is_error", False)),
                subtype=data.get("subtype", ""),
                result=data.get("result", "") or "",
                total_cost_usd=data.get("total_cost_usd"),
                num_turns=data.get("num_turns"),
                errors=[str(e) for e in errors],
            )
        ]

    return [OtherEvent(event_type, data)]


def user_message(text: str) -> str:
    """Formats a user turn for ``--input-format stream-json``."""
    return json.dumps(
        {"type": "user", "message": {"role": "user", "content": text}},
        ensure_ascii=False,
    )
