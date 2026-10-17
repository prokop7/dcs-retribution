"""The commander's Markdown journal.

The journal is the durable memory of a commander: if its Claude session is lost,
or the player loads an older save, a new session is seeded from it. Entries are
grouped by turn so the journal can be cut back to any turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

_ENTRY_HEADER = re.compile(r"^## Turn (\d+) — (.*)$")


@dataclass(frozen=True)
class JournalEntry:
    turn: int
    title: str
    body: str

    def render(self) -> str:
        return f"## Turn {self.turn} — {self.title}\n\n{self.body.strip()}\n"


def parse_entries(text: str) -> list[JournalEntry]:
    entries: list[JournalEntry] = []
    turn: Optional[int] = None
    title = ""
    body: list[str] = []

    def flush() -> None:
        if turn is not None:
            entries.append(JournalEntry(turn, title, "\n".join(body).strip()))

    for line in text.splitlines():
        match = _ENTRY_HEADER.match(line)
        if match:
            flush()
            turn = int(match.group(1))
            title = match.group(2).strip()
            body = []
        elif turn is not None:
            body.append(line)
    flush()
    return entries


class CommanderJournal:
    def __init__(self, path: Path, heading: str) -> None:
        self.path = path
        self.heading = heading

    def entries(self, up_to_turn: Optional[int] = None) -> list[JournalEntry]:
        if not self.path.exists():
            return []
        entries = parse_entries(self.path.read_text(encoding="utf-8"))
        if up_to_turn is None:
            return entries
        return [e for e in entries if e.turn <= up_to_turn]

    def append(self, turn: int, title: str, body: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists()
        with self.path.open("a", encoding="utf-8", newline="\n") as journal:
            if new_file:
                journal.write(f"# {self.heading}\n\n")
            journal.write(JournalEntry(turn, title, body).render() + "\n")

    def render(self, up_to_turn: Optional[int] = None) -> str:
        return "\n".join(e.render() for e in self.entries(up_to_turn))

    def rewind_to(self, turn: int) -> Optional[Path]:
        """Drops entries after ``turn``, archiving the full journal first.

        Returns the archive path, or None if nothing had to be dropped.
        """
        entries = self.entries()
        kept = [e for e in entries if e.turn <= turn]
        if len(kept) == len(entries):
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive = self.path.with_name(f"{self.path.stem}.abandoned-{stamp}.md")
        self.path.replace(archive)
        with self.path.open("w", encoding="utf-8", newline="\n") as journal:
            journal.write(f"# {self.heading}\n\n")
            for entry in kept:
                journal.write(entry.render() + "\n")
        return archive
