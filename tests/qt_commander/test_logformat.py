from datetime import datetime

from game.claudecommander.events import (
    InitEvent,
    OtherEvent,
    ResultEvent,
    TextEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from qt_ui.windows.commander.logformat import (
    LogKind,
    format_event,
    line_html,
    short_tool_name,
    summarize_tool_call,
)

NOW = datetime(2026, 1, 1, 12, 0, 0)


def test_short_tool_name() -> None:
    assert short_tool_name("mcp__retribution__create_package") == "create_package"
    assert short_tool_name("get_ato") == "get_ato"


def test_tool_call_summary_is_one_short_line() -> None:
    summary = summarize_tool_call(
        "mcp__retribution__create_package",
        {
            "target": "Damascus SAM site",
            "flights": [{"task": "SEAD", "count": 2}, {"task": "Strike"}],
            "tot": None,
            "options": {"start_type": "Cold"},
        },
    )
    assert summary == (
        "create_package(target='Damascus SAM site', flights=[2 items], tot=None, "
        "options={…})"
    )
    long = summarize_tool_call("write_journal", {"text": "x" * 500})
    assert "\n" not in long
    assert len(long) <= 200


def test_tool_results_name_their_call() -> None:
    names: dict[str, str] = {}
    call = format_event(
        ToolUseEvent("t1", "mcp__retribution__delete_package", {"package_id": 3}),
        names,
        NOW,
    )
    assert call is not None and call.kind is LogKind.TOOL_CALL
    ok = format_event(ToolResultEvent("t1", "Deleted.", False), names, NOW)
    assert ok is not None and ok.kind is LogKind.TOOL_RESULT
    assert ok.text == "delete_package → Deleted."
    error = format_event(ToolResultEvent("t1", "No such package", True), names, NOW)
    assert error is not None and error.is_error
    assert error.text == "delete_package failed: No such package"


def test_result_shows_summary_and_cost() -> None:
    line = format_event(
        ResultEvent("s", False, "success", "Planned 4 packages.", 1.234, 17),
        {},
        NOW,
    )
    assert line is not None and line.kind is LogKind.RESULT
    assert "Planned 4 packages." in line.text
    assert "cost $1.23" in line.text and "17 turns" in line.text


def test_other_kinds() -> None:
    assert format_event(OtherEvent("x", {}), {}, NOW) is None
    init = format_event(InitEvent("s", "opus"), {}, NOW)
    assert init is not None and init.kind is LogKind.MARKER
    text = format_event(TextEvent(" Hello "), {}, NOW)
    assert text is not None and text.text == "Hello"
    thinking = format_event(ThinkingEvent("hmm"), {}, NOW)
    assert thinking is not None and thinking.kind is LogKind.THINKING


def test_html_escapes_text() -> None:
    line = format_event(TextEvent("<b>bold</b> & co"), {}, NOW)
    assert line is not None
    rendered = line_html(line)
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; co" in rendered
    assert "12:00:00" in rendered
