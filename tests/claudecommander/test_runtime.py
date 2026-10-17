import json
from datetime import timedelta
from pathlib import Path

from game.claudecommander.config import ClaudeRunConfig, connect_host
from game.claudecommander.events import (
    InitEvent,
    OtherEvent,
    ResultEvent,
    TextEvent,
    ToolResultEvent,
    ToolUseEvent,
    parse_line,
    user_message,
)
from game.claudecommander.journal import CommanderJournal
from game.claudecommander.process import (
    ClaudeRunResult,
    SessionArgs,
    build_command,
    write_mcp_config,
)
from game.claudecommander.runner import seed_message
from game.claudecommander.session import (
    NewSessionReason,
    SessionAction,
    SessionRegistry,
    decide_session,
    is_missing_session_error,
)


def test_parse_init_and_assistant_events() -> None:
    assert parse_line(
        '{"type":"system","subtype":"init","session_id":"abc","model":"m"}'
    ) == [InitEvent("abc", "m")]
    events = parse_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Planning SEAD."},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "mcp__retribution__get_situation",
                            "input": {},
                        },
                    ]
                },
            }
        )
    )
    assert events == [
        TextEvent("Planning SEAD."),
        ToolUseEvent("t1", "mcp__retribution__get_situation", {}),
    ]


def test_parse_tool_result_and_result() -> None:
    assert parse_line(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [{"type": "text", "text": "ok"}],
                            "is_error": False,
                        }
                    ]
                },
            }
        )
    ) == [ToolResultEvent("t1", "ok", False)]
    [result] = parse_line(
        '{"type":"result","subtype":"success","is_error":false,'
        '"session_id":"abc","result":"done","total_cost_usd":0.5}'
    )
    assert isinstance(result, ResultEvent)
    assert result.session_id == "abc" and result.result == "done"
    assert not result.is_error


def test_parse_unknown_and_garbage() -> None:
    [other] = parse_line('{"type":"rate_limit_event"}')
    assert isinstance(other, OtherEvent) and other.type == "rate_limit_event"
    [garbage] = parse_line("not json")
    assert isinstance(garbage, OtherEvent) and garbage.type == "unparsable"
    assert parse_line("   ") == []


def test_user_message_format() -> None:
    assert json.loads(user_message("hi")) == {
        "type": "user",
        "message": {"role": "user", "content": "hi"},
    }


def test_build_command_locks_down_tools(tmp_path: Path) -> None:
    config = ClaudeRunConfig("claude", "opus", "high", timedelta(minutes=20), 16880)
    command = build_command(
        "claude",
        config,
        SessionArgs("sid", resume=True),
        tmp_path / "mcp.json",
        tmp_path / "prompt.md",
    )
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in command
    assert "--disable-slash-commands" in command
    assert command[command.index("--allowedTools") + 1] == "mcp__retribution"
    assert command[-2:] == ["--resume", "sid"]
    assert SessionArgs("sid", resume=False).to_args() == ["--session-id", "sid"]
    # The game server binds to ::1 by default.
    assert config.mcp_url("Red") == "http://[::1]:16880/mcp/red"
    assert connect_host("127.0.0.1") == "127.0.0.1"
    assert connect_host("0.0.0.0") == "127.0.0.1"
    assert connect_host("::") == "[::1]"
    assert connect_host("localhost") == "localhost"


def test_write_mcp_config(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    write_mcp_config(path, "http://127.0.0.1:1/mcp/red", "tok")
    server = json.loads(path.read_text())["mcpServers"]["retribution"]
    assert server == {
        "type": "http",
        "url": "http://127.0.0.1:1/mcp/red",
        "headers": {"Authorization": "Bearer tok"},
    }


def test_journal_append_render_and_rewind(tmp_path: Path) -> None:
    journal = CommanderJournal(tmp_path / "journal.md", "RED commander")
    journal.append(1, "Intent", "Defend the north.")
    journal.append(2, "Debrief", "Lost two MiG-29s.")
    journal.append(3, "Intent", "Strike Senaki.")
    assert [e.turn for e in journal.entries()] == [1, 2, 3]
    assert "Strike Senaki" not in journal.render(up_to_turn=2)

    archive = journal.rewind_to(1)
    assert archive is not None and archive.exists()
    assert [e.turn for e in journal.entries()] == [1]
    assert journal.entries()[0].body == "Defend the north."
    assert (tmp_path / "journal.md").read_text().startswith("# RED commander")
    assert journal.rewind_to(5) is None


def test_session_decisions(tmp_path: Path) -> None:
    registry = SessionRegistry(tmp_path / "sessions.json")
    first = decide_session(None, registry, 0)
    assert first.action is SessionAction.NEW
    assert first.reason is NewSessionReason.FIRST_RUN

    lost = decide_session("unknown", registry, 3)
    assert lost.action is SessionAction.NEW and lost.reason is NewSessionReason.LOST

    registry.mark_synced("s1", 8)
    assert decide_session("s1", registry, 8).action is SessionAction.RESUME
    assert decide_session("s1", registry, 9).action is SessionAction.RESUME
    rewind = decide_session("s1", registry, 5)
    assert rewind.action is SessionAction.NEW
    assert rewind.reason is NewSessionReason.REWIND
    assert rewind.session_id != "s1"


def test_missing_session_detection() -> None:
    assert is_missing_session_error(
        ClaudeRunResult(error="No conversation found with session ID: x")
    )
    assert not is_missing_session_error(ClaudeRunResult(error="timeout"))


def test_seed_message() -> None:
    assert seed_message(NewSessionReason.LOST, "", 3, "plan") == "plan"
    seeded = seed_message(NewSessionReason.REWIND, "## Turn 1 — x\n\ny", 3, "plan")
    assert 'reason="an older save was loaded"' in seeded
    assert seeded.endswith("plan")
