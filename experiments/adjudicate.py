"""Escalation gates (David's methodology call, 2026-08-06): the deterministic
formula is a DETECTOR; the calibrated judge is the GATE.

The pattern this replaces: reduce a nuanced obligation to a formula, watch the
formula misfire on phrasing the formula's author never imagined, patch the
formula, repeat (census buckets → list inheritance → hedge regexes → …; the
citation bracket-counter failing bold headlines whose elaboration is cited one
sentence away). Every misfire was already visible as a misfire to the Opus
judge in the same pipeline — its reason lines said so — and then the
aggregation formula discarded that judgment. These gates stop discarding it.

Design contract:

- **Escalation only.** Adjudication runs solely on formula-flagged failures.
  Clean formula passes stay deterministic, cheap and reproducible; judge
  spend is bounded by the number of disputes, not the size of the suite.
- **Telemetry is never displaced.** The formula's own verdict rides in the
  manifest unchanged; adjudication is recorded BESIDE it with samples, votes
  and (bundle-only) reasons. A drift between the two is a visible signal —
  the leniency-ratchet tripwire — never a silent overwrite.
- **Intent, not mechanics, in the rubric.** The rubrics below say what the
  product wants from a reader's point of view and let the judge generalize
  across phrasing. When a rubric is edited, the scoring contract severs
  (cases.SCHEMA_VERSION), same as any grading-semantics change.
- **k-sample majority.** Single-sample judge variance flipped a verdict on
  near-identical answers (the v2c036 flip-flop); gates get ADJ_SAMPLES
  independent reads and per-item majority.
- **Fail-safe on judge failure.** If adjudication produces nothing usable,
  the formula's failure verdict STANDS. Escalation can only ever be earned,
  never defaulted into.

Both adjudicators run on the cc judge backend (headless Opus — the
calibrated judge; David-seat, dev/QA only, same billing boundary as
judge_cc.py).
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

ADJ_SAMPLES = int(os.environ.get("ADVISOR_ADJ_SAMPLES", "3"))


class _CitationVerdict(BaseModel):
    id: int
    adequate: bool
    reason: str


class _CitationVerdicts(BaseModel):
    verdicts: list[_CitationVerdict]


class _NameVerdict(BaseModel):
    id: int
    verdict: bool
    reason: str


class _NameVerdicts(BaseModel):
    overclaim_flagged: list[_NameVerdict] = []
    miss_flagged: list[_NameVerdict] = []


CITATION_RUBRIC = """\
A mechanical citation check flagged assertions in this ANSWER because no
citation bracket sits inside their sentence. Adjudicate each flag against the
product's citation INTENT, which is about the reader, not bracket placement:

INTENT: a reader must be able to trace every load-bearing factual assertion
to supporting evidence without guesswork. An assertion is ADEQUATELY
attributed when the answer's own structure hands the reader its evidence —
for example, a summary or headline assertion whose elaborating sentences
immediately beside it (same bullet, same paragraph) carry citations that
genuinely support the full assertion, including any quantity or comparison it
makes. An assertion is INADEQUATELY attributed when the reader has no marked
path to evidence for some factual element of it: a standalone assertion with
no cited elaboration nearby, a thesis whose support is nowhere marked, or an
assertion whose neighboring citations do not actually cover what it asserts.

You are judging traceability, not truth — but do not reward citation theater:
a nearby bracket whose evidence does not support the assertion's content is
not a path to evidence. Judge each flagged assertion in the context of the
FULL answer and the evidence set.

Do not use any tools. Respond with ONLY a JSON object — no prose, no fences:
{"verdicts": [{"id": 1, "adequate": true, "reason": "one sentence"}, ...]}
One entry per flagged assertion id.

QUESTION:
{question}

ANSWER:
---
{answer}
---

FLAGGED ASSERTIONS:
{flagged}

EVIDENCE (everything the agent was shown):
{evidence}
"""


NAMES_RUBRIC = """\
A deterministic parser graded how this ANSWER treated certain entity names
against a corpus-scan oracle, and flagged the names below. Adjudicate the
parser's reading against the product's census INTENT:

INTENT: a census answer reports the whole census — every entity the scan
matched appears by name, grouped or annotated however the writer judges
useful. NAMING an entity is not ASSERTING it matched. An entity is positively
asserted as matching only when a reader would come away believing the answer
claims that entity matched what the QUESTION asked. It is NOT so asserted
when it appears only: in a labeled exclusion/irrelevance group; in a
scan-scope enumeration ("the scan covered …"); under negation or an explicit
non-match statement; or credited explicitly and only to a broader or adjacent
term than the asked concept ("matched on the broad word 'procurement'" is an
assertion about that word, not about the asked concept).

For each id under OVERCLAIM-FLAGGED: verdict=true if the answer positively
asserts that entity as matching the asked concept, false if it does not.
For each id under MISS-FLAGGED: verdict=true if the answer credits that
entity as a match in ANY phrasing — grouped, annotated, hedged or
variant-attributed all count as crediting — false if the entity is absent or
only mentioned without match credit.

Do not use any tools. Respond with ONLY a JSON object — no prose, no fences:
{"overclaim_flagged": [{"id": 1, "verdict": false, "reason": "..."}, ...],
 "miss_flagged": [{"id": 1, "verdict": true, "reason": "..."}, ...]}
One entry per flagged id on each list (a list may be empty).

QUESTION:
{question}

ANSWER:
---
{answer}
---

OVERCLAIM-FLAGGED (parser says: positively asserted, oracle says: no match):
{overclaimed}

MISS-FLAGGED (oracle says: matched, parser says: not credited):
{missed}
"""


def _samples(prompt: str, schema: type[BaseModel], *, tracker: Any,
             stage: str, transport: Callable | None) -> tuple[list[BaseModel], str]:
    """ADJ_SAMPLES independent judge reads; unparseable samples are dropped,
    never coerced. Returns (parsed samples, judge model string)."""
    from x1_advisor.agent.judge_cc import (_judged_model, _parse_json_result,
                                           _run_claude)
    run = transport or _run_claude
    parsed: list[BaseModel] = []
    model = ""
    for _ in range(ADJ_SAMPLES):
        out = run(prompt, tracker=tracker, stage=stage)
        model = model or _judged_model(out)
        try:
            parsed.append(schema.model_validate(
                _parse_json_result((out or {}).get("result", ""))))
        except (ValueError, ValidationError):
            continue
    return parsed, model


def _majority(votes: list[bool]) -> bool | None:
    """Per-item majority over cast votes; no votes → None (no earned verdict)."""
    if not votes:
        return None
    return sum(votes) > len(votes) / 2


def adjudicate_citation_coverage(bundle: dict, verdict: dict, *,
                                 tracker: Any = None,
                                 _transport: Callable | None = None,
                                 ) -> dict[str, Any] | None:
    """Escalate the formula's uncited-claim flags to the judge.

    Returns None when there is nothing flagged (formula verdict stands
    untouched). Otherwise returns the adjudication record; `passed` is True
    only when EVERY flagged claim earned an adequate-majority — a claim with
    no usable votes keeps its formula failure (fail-safe)."""
    flagged = verdict.get("uncited_claims") or []
    if not flagged:
        return None
    from x1_advisor.agent.judge_cc import _evidence_block

    evidence, _ = _evidence_block(bundle)
    answer = bundle.get("validation", {}).get("answer", "")
    question = ((bundle.get("request") or {}).get("question")
                or (bundle.get("request") or {}).get("prompt") or "")
    listed = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(flagged))
    prompt = (CITATION_RUBRIC
              .replace("{question}", question)
              .replace("{answer}", answer)
              .replace("{flagged}", listed)
              .replace("{evidence}", evidence))
    samples, model = _samples(prompt, _CitationVerdicts,
                              tracker=tracker, stage="adjudicate.citation",
                              transport=_transport)

    per_claim: list[dict[str, Any]] = []
    for i, claim in enumerate(flagged):
        votes = [v.adequate for s in samples for v in s.verdicts if v.id == i + 1]
        reasons = [v.reason for s in samples for v in s.verdicts if v.id == i + 1]
        adequate = _majority(votes)
        per_claim.append({"claim": claim,
                          "adequate": bool(adequate),   # None → False, fail-safe
                          "votes": votes, "reasons": reasons})
    return {
        "gate": "citation_coverage",
        "flagged": len(flagged),
        "inadequate": sum(1 for c in per_claim if not c["adequate"]),
        "passed": (all(c["adequate"] for c in per_claim)
                   if samples else False),
        "samples": ADJ_SAMPLES, "samples_used": len(samples),
        "judge_model": model,
        # bundle-only detail (quotes the answer) — projected to counts in the
        # manifest by judge_manifest_projection
        "per_claim": per_claim,
    }


FAITHFULNESS_RUBRIC = """\
The entailment grader marked assertions in this ANSWER "partial" — part
supported by their cited evidence, part not. Adjudicate each flag against the
product's faithfulness INTENT (David's standing direction: lean less
nitpicky, never absolve fabrication):

INTENT: the agent is a research advisor — drawing reasoned, clearly-signaled
inferences from evidence is its job, not a violation. A partial flag is a
REAL failure when the reader would be misled about what the evidence shows:
a factual element stated as established that no evidence in the turn
establishes, a quantity/scope stronger than the evidence, or an inference
dressed as a sourced fact. It is NOT a failure when the assertion is an
explicitly hedged or evidently-inferential synthesis ("may", "could",
"raises the risk that", a labeled conclusion) whose factual inputs are
established by the evidence shown — judge those inputs against the WHOLE
evidence set below, not only the refs the sentence cited: support living in
a ref the sentence did not cite is an attribution nit, not unfaithfulness.

For each flagged assertion: verdict "faithful" (the flag was grader
strictness) or "unfaithful" (the reader would be misled).

Do not use any tools. Respond with ONLY a JSON object — no prose, no fences:
{"verdicts": [{"id": 1, "faithful": true, "reason": "one sentence"}, ...]}
One entry per flagged assertion id.

QUESTION:
{question}

ANSWER:
---
{answer}
---

FLAGGED ASSERTIONS (with the grader's partial reasons):
{flagged}

EVIDENCE (everything the agent was shown):
{evidence}
"""


class _FaithVerdict(BaseModel):
    id: int
    faithful: bool
    reason: str


class _FaithVerdicts(BaseModel):
    verdicts: list[_FaithVerdict]


def adjudicate_faithfulness(bundle: dict, verdict: dict, *,
                            tracker: Any = None,
                            _transport: Callable | None = None,
                            ) -> dict[str, Any] | None:
    """Escalate "partial" entailment flags — and ONLY those.

    "unsupported" (contradiction/fabrication) and "unverifiable" (dead refs)
    are never adjudicated: they must keep failing, so when any exist the
    gate cannot flip and we do not spend judge tokens (returns None; formula
    verdict stands). The adjudicator sees the grader's own partial reasons —
    it is answering a DIFFERENT question (would the reader be misled?), not
    re-running entailment."""
    counts = verdict.get("counts") or {}
    if counts.get("unsupported") or counts.get("unverifiable"):
        return None
    partials = [v for v in verdict.get("verdicts") or []
                if v.get("verdict") == "partial"]
    if not partials:
        return None
    from x1_advisor.agent.judge_cc import _evidence_block

    evidence, _ = _evidence_block(bundle)
    answer = bundle.get("validation", {}).get("answer", "")
    question = ((bundle.get("request") or {}).get("question")
                or (bundle.get("request") or {}).get("prompt") or "")
    listed = "\n".join(
        f"{i + 1}. \"{v['claim']}\" (cited {v.get('citations')}; grader: "
        f"{v.get('reason', '')})" for i, v in enumerate(partials))
    prompt = (FAITHFULNESS_RUBRIC
              .replace("{question}", question)
              .replace("{answer}", answer)
              .replace("{flagged}", listed)
              .replace("{evidence}", evidence))
    samples, model = _samples(prompt, _FaithVerdicts,
                              tracker=tracker, stage="adjudicate.faithfulness",
                              transport=_transport)

    per_claim: list[dict[str, Any]] = []
    for i, v in enumerate(partials):
        votes = [x.faithful for s in samples for x in s.verdicts if x.id == i + 1]
        reasons = [x.reason for s in samples for x in s.verdicts if x.id == i + 1]
        faithful = _majority(votes)
        per_claim.append({"claim": v["claim"],
                          "faithful": bool(faithful),  # None → False, fail-safe
                          "votes": votes, "reasons": reasons})
    return {
        "gate": "faithfulness",
        "flagged": len(partials),
        "inadequate": sum(1 for c in per_claim if not c["faithful"]),
        "passed": (all(c["faithful"] for c in per_claim)
                   if samples else False),
        "samples": ADJ_SAMPLES, "samples_used": len(samples),
        "judge_model": model,
        # bundle-only detail (quotes the answer)
        "per_claim": per_claim,
    }


def adjudicate_asserted_names(question: str, answer: str, grade: dict, *,
                              tracker: Any = None,
                              _transport: Callable | None = None,
                              ) -> dict[str, Any] | None:
    """Escalate the formula's overclaim/miss flags to the judge.

    `grade` is the formula truth grade (grade_against_truth output, with
    `overclaimed`, `missed`, `hit_count`, `truth_matched`). The oracle itself
    is never adjudicated — corpus membership stays deterministic; only the
    PARSER'S READING of the prose is."""
    overclaimed = grade.get("overclaimed") or []
    missed = grade.get("missed") or []
    if not overclaimed and not missed:
        return None

    fmt = lambda names: ("\n".join(f"{i + 1}. {n}" for i, n in enumerate(names))
                         or "(none)")
    prompt = (NAMES_RUBRIC
              .replace("{question}", question)
              .replace("{answer}", answer)
              .replace("{overclaimed}", fmt(overclaimed))
              .replace("{missed}", fmt(missed)))
    samples, model = _samples(prompt, _NameVerdicts,
                              tracker=tracker, stage="adjudicate.names",
                              transport=_transport)

    def tally(names: list[str], side: str, fail_default: bool) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for i, name in enumerate(names):
            votes = [v.verdict for s in samples
                     for v in getattr(s, side) if v.id == i + 1]
            reasons = [v.reason for s in samples
                       for v in getattr(s, side) if v.id == i + 1]
            verdict = _majority(votes)
            out[name] = {"verdict": (fail_default if verdict is None
                                     else verdict),
                         "votes": votes, "reasons": reasons}
        return out

    # no earned verdict → the formula's reading stands: an unadjudicated
    # overclaim stays an overclaim (True); an unadjudicated miss stays
    # uncredited (False)
    oc = tally(overclaimed, "overclaim_flagged", fail_default=True)
    ms = tally(missed, "miss_flagged", fail_default=False)

    asserted = sorted(n for n, v in oc.items() if v["verdict"])
    credited = sorted(n for n, v in ms.items() if v["verdict"])
    matched, hits = grade.get("truth_matched") or 0, grade.get("hit_count") or 0
    adj_hits = hits + len(credited)
    return {
        "gate": "asserted_names",
        "asserted": asserted,                 # upheld overclaims
        "disclosed": sorted(set(overclaimed) - set(asserted)),
        "credited": credited,                 # rescued recall
        "uncredited": sorted(set(missed) - set(credited)),
        "overclaim_count": len(asserted),
        "recall": (adj_hits / matched) if matched else None,
        "empty_oracle_respected": (not asserted) if not matched else None,
        "samples": ADJ_SAMPLES, "samples_used": len(samples),
        "judge_model": model,
        # bundle-only detail (quotes the answer)
        "per_name": {"overclaim_flagged": oc, "miss_flagged": ms},
    }
