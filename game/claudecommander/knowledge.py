"""General lessons shared by all commanders across campaigns.

Two files make up the knowledge: the one shipped with Retribution
(resources/commander/knowledge.md) and the player's own copy in the commander
folder, which commanders extend with record_lesson and the player may edit.
Lessons must be general game and tool facts, never intelligence about a
campaign: both sides of every campaign read them.
"""

from __future__ import annotations

from pathlib import Path

BUNDLED_KNOWLEDGE = Path("resources/commander/knowledge.md")
USER_KNOWLEDGE_NAME = "knowledge.md"
USER_HEADER = (
    "# Commander knowledge (yours)\n\n"
    "Lessons commanders recorded with record_lesson. Edit freely; every "
    "commander reads this file at the start of each turn.\n\n"
)


def _lessons(path: Path) -> list[str]:
    if not path.exists():
        return []
    lessons: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            lessons.append(stripped[2:].strip())
        elif lessons and stripped and not stripped.startswith("#"):
            # Continuation line of the previous lesson.
            lessons[-1] = f"{lessons[-1]} {stripped}"
    return lessons


class CommanderKnowledge:
    def __init__(self, user_file: Path, bundled_file: Path = BUNDLED_KNOWLEDGE):
        self.user_file = user_file
        self.bundled_file = bundled_file

    def lessons(self) -> list[str]:
        seen: dict[str, None] = {}
        for lesson in _lessons(self.bundled_file) + _lessons(self.user_file):
            seen.setdefault(lesson, None)
        return list(seen)

    def add(self, lesson: str) -> bool:
        """Adds a lesson; returns False if it is already known."""
        lesson = " ".join(lesson.split())
        if not lesson or lesson in self.lessons():
            return False
        self.user_file.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.user_file.exists()
        with self.user_file.open("a", encoding="utf-8", newline="\n") as file:
            if new_file:
                file.write(USER_HEADER)
            file.write(f"- {lesson}\n")
        return True

    def render(self) -> str:
        return "\n".join(f"- {lesson}" for lesson in self.lessons())
