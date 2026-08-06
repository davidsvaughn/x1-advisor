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


QUOTES_RUBRIC = """\
A verbatim-quote check flagged quoted spans in this ANSWER that it could not
find byte-exact in the evidence. Adjudicate each flag against the product's
quotation INTENT:

INTENT: words inside quotation marks are the source's words — the reader
trusts that. A flagged span is a FAITHFUL quotation when its wording appears
in the evidence with meaning intact, allowing the standard editorial
conventions: marked elision ("…") joining verbatim pieces, bracketed splices
("[its]") for grammar, typographic variants (quote marks, hyphens, spacing),
added emphasis, and dropped inline links or citation markers. It is NOT
faithful when the span paraphrases the source while presenting itself as
quotation, when an elision or splice changes what the source meant, when the
quoted words put something in the source's mouth it does not say, or when the
span appears nowhere in the evidence.

Do not use any tools. Respond with ONLY a JSON object — no prose, no fences:
{"verdicts": [{"id": 1, "faithful": true, "reason": "one sentence"}, ...]}
One entry per flagged span id.

QUESTION:
{question}

ANSWER:
---
{answer}
---

FLAGGED QUOTED SPANS:
{flagged}

EVIDENCE (everything shown across the turns in scope):
{evidence}
"""


COVERAGE_STATEMENT_RUBRIC = """\
A pattern check found no coverage disclosure in this ANSWER, which was
required to state its search scope (must_disclose_coverage). Adjudicate
against the product's INTENT:

INTENT: when the agent answers from a search or scan, the reader must learn
the scope — what was actually searched or covered, and what was not
(unindexed, missing, out of reach) — in any phrasing whatsoever. The check
is about substance, not wording: "the remaining 12 listed startups had no
indexed evaluation text in this scan" is a disclosure; a bare list of
findings with no scope statement is not.

Do not use any tools. Respond with ONLY a JSON object — no prose, no fences:
{"disclosed": true, "reason": "one sentence"}

QUESTION:
{question}

ANSWER:
---
{answer}
---
"""


COVERAGE_CLAIMS_RUBRIC = """\
A numeric check flagged numbers in this ANSWER as possible coverage
overclaims: each flagged number exceeds the checker's own count of what this
conversation actually searched. Adjudicate each flag against the product's
INTENT:

INTENT: the agent must never claim broader search coverage than actually
happened — "yes, I searched all 20" when eight documents were shown is the
failure this exists to catch. But a number is not automatically a coverage
claim: corpus inventory statistics the agent's tools reported (how many
entities exist, how many hold evaluations, how many were not indexed) and
quantities quoted from the evidence are legitimate reporting. For each
flagged number: overclaim=true only when the answer uses it to assert the
agent SEARCHED, read or checked that many — beyond what the SEARCH TELEMETRY
supports; overclaim=false when the number describes inventory, tool-reported
coverage, or evidence content.

SEARCH TELEMETRY (deterministic, trusted — what the conversation actually
put in front of the model):
{telemetry}

Do not use any tools. Respond with ONLY a JSON object — no prose, no fences:
{"verdicts": [{"id": 1, "overclaim": false, "reason": "one sentence"}, ...]}
One entry per flagged number id.

QUESTION:
{question}

ANSWER:
---
{answer}
---

FLAGGED NUMBERS:
{flagged}

EVIDENCE (everything shown this turn):
{evidence}
"""


ENTITY_INTRUSION_RUBRIC = """\
A set-continuity check flagged entity names that a later turn introduced
without their being in the set an earlier turn established. Adjudicate each
flag against the product's INTENT:

INTENT: the model must not invent entities or drift to entities outside the
conversation's evidence. But an entity is LEGITIMATE when the conversation's
own evidence introduced it — a record surfaced by a tool or scan in any turn
shown below — and the answer presents it for what the evidence shows it to
be. For each flagged name: grounded=true when the evidence shown introduced
the entity; grounded=false when the name has no basis in the evidence.

Do not use any tools. Respond with ONLY a JSON object — no prose, no fences:
{"verdicts": [{"id": 1, "grounded": true, "reason": "one sentence"}, ...]}
One entry per flagged name id.

QUESTION:
{question}

ANSWER:
---
{answer}
---

FLAGGED NAMES:
{flagged}

EVIDENCE (everything shown across the turns in scope):
{evidence}
"""


class _CoverageVerdict(BaseModel):
    disclosed: bool
    reason: str


class _NumberVerdict(BaseModel):
    id: int
    overclaim: bool
    reason: str


class _NumberVerdicts(BaseModel):
    verdicts: list[_NumberVerdict]


class _GroundedVerdict(BaseModel):
    id: int
    grounded: bool
    reason: str


class _GroundedVerdicts(BaseModel):
    verdicts: list[_GroundedVerdict]


def _clip_evidence(evidence: list[str]) -> str:
    from x1_advisor.agent.judge import EVIDENCE_CHARS
    return "\n\n".join(f"[{i + 1}] {t[:EVIDENCE_CHARS]}"
                       for i, t in enumerate(evidence) if t)


def adjudicate_quotes(question: str, answer: str, unfound: list[str],
                      evidence: list[str], *, tracker: Any = None,
                      _transport: Callable | None = None,
                      ) -> dict[str, Any] | None:
    """Escalate quote spans the byte-exact matcher could not find.

    The matcher (with its deterministic canonicalization) stays the detector;
    what escalates is whether a still-unmatched span is a faithful quotation
    under standard editorial conventions or a fabricated/meaning-altered one.
    `passed` is True only when EVERY flagged span earns a faithful-majority."""
    if not unfound:
        return None
    listed = "\n".join(f"{i + 1}. \"{q}\"" for i, q in enumerate(unfound))
    prompt = (QUOTES_RUBRIC
              .replace("{question}", question)
              .replace("{answer}", answer)
              .replace("{flagged}", listed)
              .replace("{evidence}", _clip_evidence(evidence)))
    samples, model = _samples(prompt, _FaithVerdicts, tracker=tracker,
                              stage="adjudicate.quotes", transport=_transport)
    per_item: list[dict[str, Any]] = []
    for i, q in enumerate(unfound):
        votes = [v.faithful for s in samples for v in s.verdicts if v.id == i + 1]
        reasons = [v.reason for s in samples for v in s.verdicts if v.id == i + 1]
        faithful = _majority(votes)
        per_item.append({"quote": q, "faithful": bool(faithful),  # None → False
                         "votes": votes, "reasons": reasons})
    return {
        "gate": "quotes",
        "flagged": len(unfound),
        "inadequate": sum(1 for c in per_item if not c["faithful"]),
        "passed": (all(c["faithful"] for c in per_item) if samples else False),
        "samples": ADJ_SAMPLES, "samples_used": len(samples),
        "judge_model": model,
        # bundle-only detail (quotes the answer)
        "per_item": per_item,
    }


def adjudicate_coverage_statement(question: str, answer: str, *,
                                  tracker: Any = None,
                                  _transport: Callable | None = None,
                                  ) -> dict[str, Any]:
    """Escalate a zero-pattern-match coverage disclosure to the judge.

    Single whole-answer verdict: did the answer state its search scope in
    substance? Fail-safe as ever — no usable votes leaves the formula
    failure standing."""
    prompt = (COVERAGE_STATEMENT_RUBRIC
              .replace("{question}", question)
              .replace("{answer}", answer))
    samples, model = _samples(prompt, _CoverageVerdict, tracker=tracker,
                              stage="adjudicate.coverage_statement",
                              transport=_transport)
    votes = [s.disclosed for s in samples]
    disclosed = _majority(votes)
    return {
        "gate": "coverage_statement",
        "flagged": 1,
        "inadequate": 0 if disclosed else 1,
        "passed": bool(disclosed),               # None → False, fail-safe
        "samples": ADJ_SAMPLES, "samples_used": len(samples),
        "judge_model": model,
        "per_item": [{"disclosed": bool(disclosed), "votes": votes,
                      "reasons": [s.reason for s in samples]}],
    }


def adjudicate_coverage_claims(question: str, answer: str,
                               flagged: list[int], telemetry: dict[str, Any],
                               evidence: list[str], *, tracker: Any = None,
                               _transport: Callable | None = None,
                               ) -> dict[str, Any] | None:
    """Escalate numerals the coverage-grounding formula flagged.

    The denominator — what WAS searched — stays deterministic and rides in
    the prompt as trusted telemetry; only the reading of which numbers are
    coverage CLAIMS escalates. fail_default is overclaim=True: a flag with no
    earned verdict keeps its formula failure."""
    if not flagged:
        return None
    listed = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(flagged))
    prompt = (COVERAGE_CLAIMS_RUBRIC
              .replace("{telemetry}", json.dumps(telemetry, sort_keys=True))
              .replace("{question}", question)
              .replace("{answer}", answer)
              .replace("{flagged}", listed)
              .replace("{evidence}", _clip_evidence(evidence)))
    samples, model = _samples(prompt, _NumberVerdicts, tracker=tracker,
                              stage="adjudicate.coverage_claims",
                              transport=_transport)
    per_item: list[dict[str, Any]] = []
    for i, n in enumerate(flagged):
        votes = [v.overclaim for s in samples for v in s.verdicts if v.id == i + 1]
        reasons = [v.reason for s in samples for v in s.verdicts if v.id == i + 1]
        verdict = _majority(votes)
        per_item.append({"number": n,
                         "overclaim": True if verdict is None else verdict,
                         "votes": votes, "reasons": reasons})
    return {
        "gate": "coverage_claims",
        "flagged": len(flagged),
        "inadequate": sum(1 for c in per_item if c["overclaim"]),
        "passed": (not any(c["overclaim"] for c in per_item)
                   if samples else False),
        "samples": ADJ_SAMPLES, "samples_used": len(samples),
        "judge_model": model,
        "per_item": per_item,
    }


def adjudicate_entity_intrusion(question: str, answer: str,
                                intruders: list[str], evidence: list[str], *,
                                tracker: Any = None,
                                _transport: Callable | None = None,
                                ) -> dict[str, Any] | None:
    """Escalate entity names the set-continuity formula called intruders.

    An entity the conversation's own evidence surfaced is not an invention;
    one with no evidentiary basis keeps its formula failure (fail_default:
    ungrounded)."""
    if not intruders:
        return None
    listed = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(intruders))
    prompt = (ENTITY_INTRUSION_RUBRIC
              .replace("{question}", question)
              .replace("{answer}", answer)
              .replace("{flagged}", listed)
              .replace("{evidence}", _clip_evidence(evidence)))
    samples, model = _samples(prompt, _GroundedVerdicts, tracker=tracker,
                              stage="adjudicate.entity_intrusion",
                              transport=_transport)
    per_item: list[dict[str, Any]] = []
    for i, name in enumerate(intruders):
        votes = [v.grounded for s in samples for v in s.verdicts if v.id == i + 1]
        reasons = [v.reason for s in samples for v in s.verdicts if v.id == i + 1]
        grounded = _majority(votes)
        per_item.append({"name": name, "grounded": bool(grounded),  # None → False
                         "votes": votes, "reasons": reasons})
    return {
        "gate": "entity_intrusion",
        "flagged": len(intruders),
        "inadequate": sum(1 for c in per_item if not c["grounded"]),
        "passed": (all(c["grounded"] for c in per_item) if samples else False),
        "samples": ADJ_SAMPLES, "samples_used": len(samples),
        "judge_model": model,
        "per_item": per_item,
    }


BEHAVIOR_RUBRIC = """\
A targeted behavior judge marked an OBLIGATION unmet on this ANSWER (its
reason is below). Adjudicate against the obligation's INTENT, not its
wording:

INTENT: behavior obligations describe what the reader must come away with —
product substance, never phrasings to recite. An obligation is MET when the
answer delivers that substance in any wording at all: a scope disclosure is
met when the answer's own text and citations make plain what was consulted
and where the absence lies, whether or not it repeats the obligation's
vocabulary. It is UNMET when the substance is genuinely missing — the reader
would not learn what the obligation exists to guarantee: an absence never
stated, a false premise played along with, an ambiguity silently resolved,
a caveat the reader needed and never got.

OBLIGATION ({obligation}): {obligation_text}

FIRST JUDGE'S REASON FOR UNMET: {reason}

Do not use any tools. Respond with ONLY a JSON object — no prose, no fences:
{"met": true, "reason": "one sentence"}

QUESTION:
{question}

ANSWER:
---
{answer}
---
"""


class _MetVerdict(BaseModel):
    met: bool
    reason: str


def adjudicate_behavior(question: str, answer: str, obligation: str,
                        obligation_text: str, reason: str, *,
                        tracker: Any = None,
                        _transport: Callable | None = None) -> dict[str, Any]:
    """Escalate one unmet behavior obligation to the judge.

    The targeted behavior judge stays the DETECTOR — cheap, per-obligation,
    literal. Its unmet flags escalate here with its own reason attached, and
    the Opus judge answers a different question: did the answer deliver the
    obligation's substance? (v2c026: "the platform does not provide a
    structured market average; market scores live in evaluation documents"
    plus a cited registry aggregate was failed for not reciting 'corpus
    search, registry query, or both'.) Fail-safe: no usable votes leaves the
    detector's failure standing."""
    prompt = (BEHAVIOR_RUBRIC
              .replace("{obligation}", obligation)
              .replace("{obligation_text}", obligation_text)
              .replace("{reason}", reason or "(none given)")
              .replace("{question}", question)
              .replace("{answer}", answer))
    samples, model = _samples(prompt, _MetVerdict, tracker=tracker,
                              stage="adjudicate.behavior",
                              transport=_transport)
    votes = [s.met for s in samples]
    met = _majority(votes)
    return {
        "gate": "behavior",
        "flagged": 1,
        "inadequate": 0 if met else 1,
        "passed": bool(met),                     # None → False, fail-safe
        "samples": ADJ_SAMPLES, "samples_used": len(samples),
        "judge_model": model,
        "per_item": [{"obligation": obligation, "met": bool(met),
                      "votes": votes, "reasons": [s.reason for s in samples]}],
    }


def escalate_behaviors(behavior_verdicts: list[dict], *, question: str,
                       answer: str, tracker: Any = None,
                       _transport: Callable | None = None) -> None:
    """Escalate every unmet behavior verdict in place (s6).

    The detector's verdict survives as `formula_met`; `met` becomes the
    adjudicated verdict; the body-free summary rides in the dict (projected
    into manifests) while per-sample reasons stay bundle-only."""
    from experiments.cases import BEHAVIOR_OBLIGATIONS

    for v in behavior_verdicts:
        if v.get("met") is not False:
            continue
        adj = adjudicate_behavior(question, answer, v["obligation"],
                                  BEHAVIOR_OBLIGATIONS[v["obligation"]],
                                  v.get("reason", ""), tracker=tracker,
                                  _transport=_transport)
        v["formula_met"] = False
        v["adjudication"] = {k: adj.get(k) for k in _SUMMARY_KEYS}
        v["adjudication_reasons"] = adj["per_item"][0]["reasons"]
        v["met"] = adj["passed"]


# body-free summary keys copied into diagnostic detail (and thence into
# manifests via checkers.countable); per_item stays bundle-only under the
# NAMED_DETAIL projection
_SUMMARY_KEYS = ("gate", "flagged", "inadequate", "passed",
                 "samples", "samples_used", "judge_model")


def attach_adjudication(diagnostic: Any, adj: dict[str, Any]) -> Any:
    """A new Diagnostic carrying the adjudicated verdict: the formula verdict
    survives in detail["formula_passed"], the body-free summary rides beside
    it (manifest-safe), per-item detail stays bundle-only (NAMED_DETAIL).
    Diagnostics are frozen, so the caller swaps this into its list."""
    import dataclasses
    return dataclasses.replace(
        diagnostic, passed=adj["passed"],
        detail={**diagnostic.detail,
                "formula_passed": diagnostic.passed,
                "adjudication": {k: adj.get(k) for k in _SUMMARY_KEYS},
                "adjudication_items": adj["per_item"]})


def escalate_assertions(assertions: list, *, question: str, answer: str,
                        evidence: list[str], tracker: Any = None,
                        _transport: Callable | None = None) -> None:
    """Escalate formula-failed gated ASSERTIONS, swapping adjudicated
    replacements into the list (s5).

    quotes_verbatim and coverage_statement join the judged dimensions in the
    escalation pattern. A quotes failure with NO quotes at all
    (require_quotes unmet) is a real absence, not a formula misread — it
    never escalates."""
    for i, a in enumerate(assertions):
        if a.passed:
            continue
        if a.check == "quotes_verbatim":
            unfound = a.detail.get("unfound") or []
            adj = adjudicate_quotes(question, answer, unfound, evidence,
                                    tracker=tracker, _transport=_transport)
        elif a.check == "coverage_statement":
            adj = adjudicate_coverage_statement(question, answer,
                                                tracker=tracker,
                                                _transport=_transport)
        else:
            continue
        if adj is not None:
            assertions[i] = attach_adjudication(a, adj)


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
