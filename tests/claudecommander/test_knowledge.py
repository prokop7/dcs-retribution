from pathlib import Path

from game.claudecommander.knowledge import BUNDLED_KNOWLEDGE, CommanderKnowledge
from game.claudecommander.prompts import RunBrief, RunKind, run_message, system_prompt


def test_bundled_lessons_are_loaded(tmp_path: Path) -> None:
    knowledge = CommanderKnowledge(tmp_path / "knowledge.md")
    lessons = knowledge.lessons()
    assert any("4x HARM" in lesson and "70 nm" in lesson for lesson in lessons)
    assert any("Navigation points" in lesson for lesson in lessons)
    assert BUNDLED_KNOWLEDGE.exists()


def test_commanders_add_lessons_once(tmp_path: Path) -> None:
    user_file = tmp_path / "knowledge.md"
    knowledge = CommanderKnowledge(user_file, bundled_file=tmp_path / "none.md")
    assert knowledge.add("Mirage F1 tanks limit it to two AIM-9s.")
    assert not knowledge.add("Mirage  F1 tanks limit it to two AIM-9s.")
    assert knowledge.lessons() == ["Mirage F1 tanks limit it to two AIM-9s."]
    assert user_file.read_text(encoding="utf-8").startswith("# Commander knowledge")


def test_turn_messages_carry_knowledge() -> None:
    message = run_message(RunBrief(RunKind.TURN, 3, knowledge="- A lesson."))
    assert "<knowledge>" in message and "- A lesson." in message
    assert "<knowledge>" not in run_message(RunBrief(RunKind.TURN, 3))
    prompt = system_prompt("Blue")
    assert "record_lesson" in prompt
    assert "Protect your economy" in prompt
