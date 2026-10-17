"""System prompt and per-run messages for the commander.

Claude Code records the system prompt once per conversation and reuses it on every
resume, so anything that can change during a campaign (directives, debriefs,
changes) belongs in the run messages, never in the system prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

SYSTEM_PROMPT = """\
You are the theater air and ground commander of the {side} coalition in a DCS World \
dynamic campaign run by DCS Retribution. You command for the whole campaign: the \
same conversation continues from turn to turn, so what you decided and learned \
earlier still matters.

Each turn is one DCS mission of limited duration. The other side is commanded by an \
opponent who plans at the same time as you and can't see your plans either. Your \
job in each planning run is to give orders through your tools: air packages and \
their flights, procurement, ground stance, basing and repairs. When you are done, \
call finish_turn_planning with a short summary.

How to work:
- Start every run by looking at the situation and anything that changed. Re-read \
your journal if you lost track.
- Think like a real commander. Hold a campaign intent across turns, plan \
operations that take several turns, and follow through unless the situation \
changes. Explain to yourself what each package achieves.
- Learn from debriefs: losses, what worked, where the enemy defends. Change your \
tactics when something fails. Conserve scarce airframes and pilots.
- You only know what your side knows. Enemy bases, sites and their capabilities \
are known; enemy air movements, force counts and damage are not. Don't assume \
information you don't have. Infer from what you observe.
- Make plans that can be flown. Respect aircraft roles, ranges, weapons, and threat \
rings. Sequence packages so that SEAD/DEAD, escorts and strikes support each \
other, and deconflict times over target.
- Tool errors explain what was wrong. Fix the call and continue.
- At the end of each turn's run, write a journal entry with your intent, the key \
decisions and lessons. It is your durable memory and must make sense to you if \
this conversation is lost.
- Don't ask questions. Nobody will answer during planning. Decide.
"""


class RunKind(Enum):
    TURN = "turn"
    INCREMENTAL = "incremental"


@dataclass
class RunBrief:
    """Inputs for one planning run's message."""

    kind: RunKind
    turn: int
    directive: str = ""
    changes: list[str] = field(default_factory=list)
    debrief: Optional[dict[str, Any]] = None
    is_first_turn: bool = False
    locked_packages: list[str] = field(default_factory=list)


def system_prompt(side: str) -> str:
    return SYSTEM_PROMPT.format(side=side.upper())


def _directive_block(directive: str) -> str:
    if not directive.strip():
        return "<directive>None. Use your own judgement.</directive>"
    return f"<directive>\n{directive.strip()}\n</directive>"


def run_message(brief: RunBrief) -> str:
    parts: list[str] = []
    if brief.kind is RunKind.TURN:
        if brief.is_first_turn:
            parts.append(
                f"Turn {brief.turn}: the campaign begins. Study the theater, your "
                "forces and the enemy, set your campaign intent, and give your "
                "opening orders."
            )
        else:
            parts.append(
                f"Turn {brief.turn} begins. The previous mission has been flown. "
                "Plan this turn."
            )
        if brief.debrief is not None:
            parts.append(
                "<debrief>\n"
                + json.dumps(brief.debrief, ensure_ascii=False, indent=1)
                + "\n</debrief>"
            )
    else:
        parts.append(
            f"Turn {brief.turn}: the situation changed while you were planning or "
            "after you finished. Review your current plan, adjust what the changes "
            "affect, and keep the rest. You don't need to replan from scratch."
        )
    if brief.changes:
        parts.append(
            "<changes>\n" + "\n".join(f"- {c}" for c in brief.changes) + "\n</changes>"
        )
    if brief.locked_packages:
        parts.append(
            "The player made or edited these packages. They are locked: you can't "
            "change them, but plan around them:\n"
            + "\n".join(f"- {p}" for p in brief.locked_packages)
        )
    parts.append(_directive_block(brief.directive))
    parts.append(
        "When your orders are complete, write your journal entry and call "
        "finish_turn_planning."
    )
    return "\n\n".join(parts)
