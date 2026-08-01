"""Targeted behavior judge (GOLDEN-V2-DESIGN §6; second-review fix).

`grade.behavior` obligations — corrected the false premise, surfaced the
conflict, declined the action — are judgments, which is why cases.py refused to
file them under `deterministic`. But declaring them and never grading them is
the same silent no-op the compiler exists to prevent: the second review found
v2c045 confidently describing a "second evaluation" that the fixture guarantees
does not exist, and still passing its false-premise contract, because nothing
ever asked a judge about the obligation.

This module asks. One structured call per case grades every declared
obligation against its rubric — the rubric text is the single source in
`cases.BEHAVIOR_OBLIGATIONS`, so the vocabulary the compiler whitelists is
exactly the vocabulary the judge grades. The verdicts are judgments by the
same judge model as the claim judge and carry its identity; the manifest gets
{obligation, met} only — the reasons quote the answer, so they stay in the
owner-only bundle.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel

from experiments.cases import BEHAVIOR_OBLIGATIONS

BEHAVIOR_PROMPT = """\
You are grading whether an assistant's ANSWER met specific behavioral
obligations. The user asked the QUESTION; the obligations describe what a
correct response was required to DO — independent of whether its facts are
right (a separate judge checks facts).

For each obligation listed, decide met=true only if the answer actually did
it. Judge the answer's behavior, not its politeness. Give a one-sentence
reason naming the sentence or omission that decided it.

OBLIGATIONS:
{obligations}

QUESTION:
{question}

ANSWER:
---
{answer}
"""


class BehaviorVerdict(BaseModel):
    obligation: str
    met: bool
    reason: str


class BehaviorVerdicts(BaseModel):
    verdicts: list[BehaviorVerdict]


def judge_behaviors(question: str, answer: str, obligations: tuple[str, ...],
                    *, tracker: Any | None = None) -> list[dict[str, Any]]:
    """Grade each declared obligation. Returns one verdict per obligation, in
    declaration order; an obligation the judge failed to address comes back
    met=False rather than silently absent — ungraded must never read as met."""
    if not obligations:
        return []
    from openai import OpenAI

    from x1_advisor.agent.judge import JUDGE_MODEL, _ask

    unknown = [o for o in obligations if o not in BEHAVIOR_OBLIGATIONS]
    if unknown:
        raise KeyError(f"unknown behavior obligation(s) {unknown} — the "
                       "compiler whitelist and this judge must agree")

    listed = "\n".join(f"- {o}: {BEHAVIOR_OBLIGATIONS[o]}" for o in obligations)
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    parsed = _ask(client, tracker,
                  BEHAVIOR_PROMPT.format(obligations=listed, question=question,
                                         answer=answer),
                  BehaviorVerdicts, "judge.behavior")
    by_name = {v.obligation: v for v in (parsed.verdicts if parsed else [])}
    return [{
        "obligation": o,
        "met": bool(by_name[o].met) if o in by_name else False,
        "reason": (by_name[o].reason if o in by_name
                   else "judge returned no verdict for this obligation"),
        "judge_model": JUDGE_MODEL,
    } for o in obligations]
