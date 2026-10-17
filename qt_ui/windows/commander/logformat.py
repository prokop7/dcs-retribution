"""Turns commander stream events into short, human-readable log lines."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from game.claudecommander.events import (
    InitEvent,
    OtherEvent,
    ResultEvent,
    StreamEvent,
    TextEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
)

_MAX_VALUE_LENGTH = 40
_MAX_SUMMARY_LENGTH = 200
_MAX_RESULT_LENGTH = 300


class LogKind(Enum):
    MARKER = "marker"
    TEXT = "text"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    RESULT = "result"
    ERROR = "error"


@dataclass(frozen=True)
class LogLine:
    kind: LogKind
    text: str
    time: datetime

    @property
    def is_error(self) -> bool:
        return self.kind is LogKind.ERROR


def short_tool_name(name: str) -> str:
    """``mcp__retribution__create_package`` -> ``create_package``."""
    return name.rsplit("__", 1)[-1] if "__" in name else name


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _summarize_value(value: Any) -> str:
    if isinstance(value, str):
        return repr(_shorten(value, _MAX_VALUE_LENGTH))
    if isinstance(value, (bool, int, float)) or value is None:
        return repr(value)
    if isinstance(value, list):
        if all(isinstance(v, (str, int, float, bool)) for v in value) and value:
            return _shorten(
                "[" + ", ".join(_summarize_value(v) for v in value) + "]",
                _MAX_VALUE_LENGTH,
            )
        return f"[{len(value)} item{'s' if len(value) != 1 else ''}]"
    if isinstance(value, dict):
        return "{…}" if value else "{}"
    return _shorten(str(value), _MAX_VALUE_LENGTH)


def summarize_tool_call(name: str, arguments: dict[str, Any]) -> str:
    """One-line summary of a tool call, e.g. ``create_package(target='X', ...)``."""
    args = ", ".join(f"{k}={_summarize_value(v)}" for k, v in arguments.items())
    return _shorten(f"{short_tool_name(name)}({args})", _MAX_SUMMARY_LENGTH)


def format_event(
    event: StreamEvent | OtherEvent,
    tool_names: dict[str, str],
    now: Optional[datetime] = None,
) -> Optional[LogLine]:
    """Formats ``event`` for the live log, or returns None to leave it out.

    ``tool_names`` maps tool use ids to tool names. It is updated for tool calls so
    that results can name the tool they belong to.
    """
    time = now or datetime.now()
    if isinstance(event, InitEvent):
        return LogLine(LogKind.MARKER, f"Session started ({event.model}).", time)
    if isinstance(event, TextEvent):
        return LogLine(LogKind.TEXT, event.text.strip(), time)
    if isinstance(event, ThinkingEvent):
        return LogLine(LogKind.THINKING, event.text.strip(), time)
    if isinstance(event, ToolUseEvent):
        tool_names[event.tool_use_id] = short_tool_name(event.name)
        return LogLine(
            LogKind.TOOL_CALL, summarize_tool_call(event.name, event.input), time
        )
    if isinstance(event, ToolResultEvent):
        name = tool_names.get(event.tool_use_id, "tool")
        text = _shorten(event.text, _MAX_RESULT_LENGTH) or "(no output)"
        if event.is_error:
            return LogLine(LogKind.ERROR, f"{name} failed: {text}", time)
        return LogLine(LogKind.TOOL_RESULT, f"{name} → {text}", time)
    if isinstance(event, ResultEvent):
        parts: list[str] = []
        if event.is_error:
            detail = "; ".join(event.errors) or event.subtype
            parts.append(f"Run ended with an error: {detail}")
        elif event.result.strip():
            parts.append(event.result.strip())
        else:
            parts.append("Run finished.")
        stats: list[str] = []
        if event.total_cost_usd is not None:
            stats.append(f"cost ${event.total_cost_usd:.2f}")
        if event.num_turns is not None:
            stats.append(f"{event.num_turns} turns")
        if stats:
            parts.append("(" + ", ".join(stats) + ")")
        kind = LogKind.ERROR if event.is_error else LogKind.RESULT
        return LogLine(kind, "\n".join(parts), time)
    return None


_COLORS = {
    LogKind.MARKER: "#8a8a8a",
    LogKind.THINKING: "#8a8a8a",
    LogKind.TOOL_CALL: "#4f9de0",
    LogKind.TOOL_RESULT: "#5fa860",
    LogKind.ERROR: "#e05050",
}


def line_html(line: LogLine) -> str:
    """Renders a log line as an HTML fragment for a QTextBrowser."""
    stamp = f'<span style="color:#8a8a8a">{line.time:%H:%M:%S}</span>'
    body = html.escape(line.text).replace("\n", "<br/>")
    if line.kind is LogKind.TOOL_CALL:
        body = f"<code>{body}</code>"
    elif line.kind is LogKind.THINKING:
        body = f"<i>{body}</i>"
    elif line.kind in (LogKind.RESULT, LogKind.MARKER):
        body = f"<b>{body}</b>"
    color = _COLORS.get(line.kind)
    if color is not None:
        body = f'<span style="color:{color}">{body}</span>'
    return f"<p>{stamp}&nbsp; {body}</p>"
