"""The commander's journal and the end of a planning run."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from game.claudecommander.context import CommanderContext, commander_tools
from game.claudecommander.coordinator import commander_journal
from game.claudecommander.mcp.tools import ToolError
from game.claudecommander.tools.airchecks import validate
from game.claudecommander.view import Json

#: Keeps read_journal well inside what one tool result may hold.
MAX_JOURNAL_CHARS = 30_000


class WriteJournalArgs(BaseModel):
    title: str = Field(min_length=1, max_length=120, description="Short heading.")
    text: str = Field(
        min_length=1,
        max_length=20_000,
        description="Markdown: intent, objectives, observations, lessons.",
    )


@commander_tools.register(
    "write_journal",
    "Adds an entry to your campaign journal under the current turn. The journal "
    "is your long-term memory: if your session is lost or the player loads an "
    "older save, you continue from it. Record intent, objectives, what you "
    "expect, and lessons learned.",
    WriteJournalArgs,
)
def write_journal(context: CommanderContext, args: WriteJournalArgs) -> Json:
    title = " ".join(args.title.split())
    journal = commander_journal(context.game, context.player)
    journal.append(context.game.turn, title, args.text)
    return {"ok": True, "turn": context.game.turn, "title": title}


class ReadJournalArgs(BaseModel):
    from_turn: Optional[int] = Field(default=None, ge=0)
    to_turn: Optional[int] = Field(default=None, ge=0)


@commander_tools.register(
    "read_journal",
    "Reads your campaign journal (all of it, or the entries of a turn range). "
    "Long journals return the most recent entries that fit.",
    ReadJournalArgs,
    read_only=True,
)
def read_journal(context: CommanderContext, args: ReadJournalArgs) -> Json:
    if (
        args.from_turn is not None
        and args.to_turn is not None
        and args.from_turn > args.to_turn
    ):
        raise ToolError("from_turn must not be after to_turn.")
    journal = commander_journal(context.game, context.player)
    entries = [
        e
        for e in journal.entries()
        if (args.from_turn is None or e.turn >= args.from_turn)
        and (args.to_turn is None or e.turn <= args.to_turn)
    ]
    rendered: list[str] = []
    size = 0
    for entry in reversed(entries):
        text = entry.render()
        if rendered and size + len(text) > MAX_JOURNAL_CHARS:
            break
        rendered.append(text)
        size += len(text)
    rendered.reverse()
    result: Json = {"entries": len(entries), "journal": "\n".join(rendered)}
    if len(rendered) < len(entries):
        result["omitted_older_entries"] = len(entries) - len(rendered)
    return result


class FinishArgs(BaseModel):
    summary: str = Field(
        min_length=1, description="One or two sentences on what this run planned."
    )


@commander_tools.register(
    "finish_turn_planning",
    "Marks your planning for this turn as done and returns a final check of your "
    "air plan, so you can fix anything left before ending. Packages without a TOT "
    "are timed ASAP afterwards. End the run with a final message summarizing your "
    "plan and intent; that message is written to your journal.",
    FinishArgs,
)
def finish_turn_planning(context: CommanderContext, args: FinishArgs) -> Json:
    coalition = context.game.coalition_for(context.player)
    coalition.commander.finished_turn = context.game.turn
    check = validate(context.view())
    return {
        "ok": True,
        "turn": context.game.turn,
        "air_plan": check,
    }


class RecordLessonArgs(BaseModel):
    lesson: str = Field(
        min_length=10,
        max_length=400,
        description="One general lesson about the game or the planning tools, "
        "true in any campaign.",
    )


@commander_tools.register(
    "record_lesson",
    "Adds a general lesson to the knowledge every commander reads at the start of "
    "each turn, in every campaign and on both sides: facts about aircraft, "
    "loadouts, weapons or how the tools behave (for example a loadout's range "
    "limit). Never record intelligence about the current campaign or the enemy; "
    "that belongs in your journal.",
    RecordLessonArgs,
)
def record_lesson(context: CommanderContext, args: RecordLessonArgs) -> Json:
    from game.claudecommander.coordinator import commander_knowledge

    added = commander_knowledge().add(args.lesson)
    return {"recorded": added, "note": None if added else "Already known."}
