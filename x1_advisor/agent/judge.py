"""Claim/citation judge (Gate 1B-3) — does the cited evidence actually support
the claim in front of it?

`citation_resolvability` was the Phase-4 exit metric and it proved the wrong
thing: that every `[n]` points at a block that exists. A model citing plausibly
but wrongly scores a perfect 100%, and so does a model that cites nothing at all
(0/0). Gate 1A then measured 38% of those "resolvable" citations landing on
generated summaries. Resolvability is a liveness check, not a quality one.

Two questions, judged separately because they fail separately:

* **faithfulness** — of the factual claims that carry a citation, how many are
  actually entailed by the cited text? A failure here is `synthesis_error`.
* **citation coverage** — of the factual claims in the answer, how many carry a
  citation at all? A failure here is `citation_coverage_error`.

Evidence comes from what the model was actually shown — the per-ref snapshots
the evidence registry captured at tool time (Gate 1D-1). Judging against the
live database judged a *different document* than the one the model read: a
600-char snippet could be scored "supported" using text the model never saw,
and a corpus change could silently rewrite historical scores. With snapshots
the judge is a pure function of the bundle. Bundles that predate snapshots are
still judgeable via DB reconstruction, but every such score carries
`evidence_provenance: "reconstructed-legacy"` so it can never be quoted as if
it measured synthesis.

Known limitation — web evidence is CALL-level, not URL-level: the web tool
returns one findings text per call plus URL annotations, and no per-URL page
text exists to snapshot. Two URLs from the same call therefore share a
snapshot, and a claim citing one may be judged supported by text the other
contributed. Bounded by what the API returns; fixing it means fetching pages.

**The judge is itself unverified until calibrated.** The second review was
explicit: do not turn the judge's score into another unverified proxy. So every
score carries the calibration state it was produced under, and
`experiments/judge_calibrate.py` is how that state is earned.

Run: uv run python -m x1_advisor.agent.judge <turn_id> [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

from openai import OpenAI
from pydantic import BaseModel

from x1_advisor.agent.evidence import canonical_params
from x1_advisor.cost import Tracker, Usage

# gpt-5.6-terra since 2026-07-31, picked by `judge_bakeoff --labeled-only`
# against 32 human labels — the first judge choice resting on human ground
# truth rather than on inference about the models.
#
#   terra 25/32 (kappa 0.63) · sol 24/32 (0.58) · luna 21/32 (0.45) · 5.1 20/32 (0.44)
#   false clean bills: 0 for all four.
#   cost per judged suite: luna ~$0.10 · terra ~$0.72 · sol ~$1.50
#
# terra is both the best-agreeing and the better value; sol is one item behind
# at double the price. luna and gpt-5.1 are a genuine step down, which retired
# two earlier guesses: that luna's synthetic 10/10 predicted real-text agreement
# (it did not), and that gpt-5.1's strictness sat closer to a careful reader (it
# is the furthest from one).
#
# **This is also the agent's model, so the judge grades its own answers.** That
# is a real effect — E8 measured terra's self-preference at +0.011 faithfulness,
# inside noise but pointing the way theory predicts. It is handled by policy
# rather than by buying a pricier judge, because the bias is CONSTANT and
# therefore cancels in the comparison that matters most: same agent model on
# both sides (prompt, retrieval, tool changes). It does NOT cancel when the arms
# run different agent models — so for a model A/B, regrade both arms with a
# judge that wrote neither (`ADVISOR_JUDGE_MODEL=gpt-5.6-luna experiments.rejudge`,
# ~$0.10 a side). QA-RUNBOOK §8 states the rule.
#
# Caveat on the labels, recorded so it is not lost: all 32 were produced with a
# second opinion visible and matched it 32/32, so they carry `assist_shown` in
# the set. A fresh unassisted draw is pending and can revise this.
JUDGE_MODEL = os.environ.get("ADVISOR_JUDGE_MODEL", "gpt-5.6-terra")
# v2 (Gate 1D-1): claims are judged against per-ref snapshots of what the model
# saw, not against the current database; scores from v1 are not comparable
SCHEMA_VERSION = 2
# Opt-in cap on judge evidence input (a cost knob, NEVER a default): unset or
# 0 means UNLIMITED. The 8,000-char default this replaces silently cut an
# 11,106-char structured snapshot mid-payload, so claims about entities past
# the cutoff were judged against evidence that omitted them (1E review). Chunk
# and web snapshots are bounded by tool output contracts; structured payloads
# are bounded only by MAX_ROWS × row width, which can exceed any fixed guess.
EVIDENCE_CHARS = int(os.environ.get("ADVISOR_JUDGE_EVIDENCE_CHARS", "0")) or None

# The judge owns the answer to "how far should you trust me". The labelled set
# is data and the agreement measurement is a harness
# (experiments/judge_calibrate.py), but the *state* travels with every score.
CALIBRATION_SET = (Path(__file__).resolve().parents[2] / "experiments"
                   / "judge_calibration.jsonl")
LABELS = ("supported", "partial", "unsupported")
MIN_HUMAN_LABELS = 30      # below this, agreement is not a meaningful estimate


def load_calibration_set() -> list[dict]:
    if not CALIBRATION_SET.exists():
        return []
    return [json.loads(line) for line in
            CALIBRATION_SET.read_text().splitlines() if line.strip()]


def calibration_state(items: list[dict] | None = None) -> dict:
    """What a judged score is allowed to claim about itself.

    `synthetic-only` means the judge has been shown to catch objective errors
    (mutated numbers, swapped entities) but has never been checked against a
    human on real, ambiguous text. That is a real distinction and the score
    must carry it.
    """
    items = load_calibration_set() if items is None else items
    human = [i for i in items if i.get("provenance") == "human" and i.get("label")]
    synthetic = [i for i in items
                 if i.get("provenance") == "synthetic" and i.get("label")]
    state = ("human-calibrated" if len(human) >= MIN_HUMAN_LABELS
             else "synthetic-only" if synthetic else "uncalibrated")
    return {"state": state, "human_labels": len(human),
            "synthetic_labels": len(synthetic),
            "min_human_labels": MIN_HUMAN_LABELS, "judge_model": JUDGE_MODEL}


# --------------------------------------------------------------------------
# structured outputs

class Claim(BaseModel):
    text: str
    is_factual: bool
    citation_numbers: list[int]


class ClaimInventory(BaseModel):
    claims: list[Claim]


class Entailment(BaseModel):
    verdict: Literal["supported", "partial", "unsupported"]
    reason: str


INVENTORY_PROMPT = """\
List every distinct assertion in this ANSWER.

For each one give:
- text: the assertion, quoted or closely paraphrased from the answer
- is_factual: true if it states a checkable fact about the world (a number, a
  name, an event, a conclusion attributed to a source). False for hedges,
  questions, offers to help, restatements of the user's question, and pure
  connective prose.
- citation_numbers: the bracketed citation numbers attached to it, e.g. [1,3]
  becomes [1, 3]. Empty list if it carries none.

Split compound sentences into separate assertions when they make separate
factual claims. Do not invent assertions that are not in the text.

ANSWER:
---
{answer}
"""

ENTAILMENT_PROMPT = """\
Decide whether the SOURCE text supports the CLAIM.

- supported: every factual element of the claim follows from the source.
- partial: the source supports part of the claim but not all of it, or supports
  it in weaker terms than the claim states.
- unsupported: the source does not establish the claim, contradicts it, or is
  about something else.

Judge only against the source given. Do not use outside knowledge. A claim that
is true in the world but absent from the source is unsupported. Numbers, names
and dates must match: "68" is not support for "78".

Give a one-sentence reason naming the specific element that decided it.

CLAIM:
{claim}

SOURCE:
---
{source}
"""


# --------------------------------------------------------------------------

def _ask(client: OpenAI, tracker: Tracker | None, prompt: str,
         schema: type[BaseModel], stage: str,
         model: str | None = None) -> BaseModel | None:
    """`model` overrides JUDGE_MODEL for this call — used by the bake-off to
    put several candidate judges on byte-identical prompts. Cost is attributed
    to whichever model actually ran, never to the configured default."""
    model = model or JUDGE_MODEL
    resp = client.chat.completions.parse(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format=schema,
    )
    if tracker:
        tracker.log(provider="openai", model=model, stage=stage,
                    usage=Usage.from_haystack_meta("openai", resp.usage.model_dump()))
    return resp.choices[0].message.parsed


def evidence_provenance(bundle: dict) -> str:
    """Can this bundle be judged against what the model actually saw?

    `turn-snapshot` — the evidence registry carries per-ref snapshots (bundle
    schema v3+): the judge is a pure function of the bundle.
    `reconstructed-legacy` — pre-snapshot bundle: evidence is re-derived from
    the current database and pooled web findings, which measures citation
    audit-worthiness *today*, not synthesis fidelity *then*. Scores under this
    provenance must never be compared against snapshot-judged scores.
    """
    by_ref = {e.get("ref"): e for e in bundle.get("evidence") or []}
    cits = bundle.get("validation", {}).get("citations", [])
    if not cits:   # nothing to resolve — go by the bundle's own contract
        return ("turn-snapshot" if (bundle.get("schema_version") or 0) >= 3
                else "reconstructed-legacy")
    if all(c.get("ref") in by_ref
           and by_ref[c["ref"]].get("snapshot") for c in cits):
        return "turn-snapshot"
    return "reconstructed-legacy"


def evidence_texts(conn, bundle: dict) -> dict[int, dict[str, Any]]:
    """Citation number → the text the model was shown for it."""
    if evidence_provenance(bundle) == "turn-snapshot":
        by_ref = {e["ref"]: e for e in bundle.get("evidence") or []}
        out: dict[int, dict[str, Any]] = {}
        for c in bundle.get("validation", {}).get("citations", []):
            ev = by_ref[c["ref"]]
            locator = (f"doc {ev.get('document_id')} block {ev.get('block_index')}"
                       if ev.get("kind") == "chunk" else
                       f"{ev.get('query_name')}({canonical_params(ev.get('query_params'))})"
                       if ev.get("kind") == "query" else ev.get("url", ""))
            out[c["n"]] = {"kind": c.get("type"), "text": ev["snapshot"],
                           "locator": locator}
        return out
    return _legacy_evidence_texts(conn, bundle)


def _legacy_evidence_texts(conn, bundle: dict) -> dict[int, dict[str, Any]]:
    """Pre-snapshot reconstruction — kept ONLY so old bundles stay judgeable.

    Known-wrong in two documented ways (the Gate 1D review): internal refs
    resolve to the full CURRENT block (the model may have seen a snippet of an
    earlier version), and web refs each receive every web finding in the turn.
    Callers surface this via `evidence_provenance`.
    """
    out: dict[int, dict[str, Any]] = {}
    web_findings: list[str] = []
    query_results: dict[tuple[str, str], str] = {}
    for m in bundle.get("messages", []):
        for content in m.get("content", []) or []:
            result = (content.get("tool_call_result") or {}).get("result")
            if not result:
                continue
            try:
                payload = json.loads(result)
            except (TypeError, ValueError):
                continue
            if payload.get("findings"):
                web_findings.append(payload["findings"])
            if payload.get("query") and "rows" in payload:
                query_results[(payload["query"],
                               canonical_params(payload.get("params")))] = (
                    f"X1 platform data — {payload['query']}"
                    f"({canonical_params(payload.get('params'))})\n"
                    f"What this query returns: {payload.get('description', '')}\n"
                    f"Access scope: {payload.get('acl_scope', 'unknown')}\n"
                    f"Rows:\n{json.dumps(payload['rows'], default=str, indent=1)}")

    for c in bundle.get("validation", {}).get("citations", []):
        n = c.get("n")
        if c.get("type") == "internal" and conn is not None:
            row = conn.execute(
                """SELECT c.text FROM advisor.doc_chunks c
                   WHERE c.document_id = %s AND c.block_index = %s""",
                (c["document_id"], c["block_index"]),
            ).fetchone()
            out[n] = {"kind": "internal", "text": (row or {}).get("text", ""),
                      "locator": f"doc {c['document_id']} block {c['block_index']}"}
        elif c.get("type") == "web":
            out[n] = {"kind": "web", "text": "\n\n".join(web_findings),
                      "locator": c.get("url", "")}
        elif c.get("type") == "platform_data":
            key = (c.get("query"), canonical_params(c.get("params")))
            out[n] = {"kind": "platform_data",
                      "text": query_results.get(key, ""),
                      "locator": f"{c.get('query')}({canonical_params(c.get('params'))})"}
    return out


def judge_bundle(conn, bundle: dict, *, tracker: Tracker | None = None,
                 calibration: dict | None = None) -> dict[str, Any]:
    """Judge one turn bundle. Returns scores + per-claim verdicts."""
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    answer = bundle.get("validation", {}).get("answer", "")
    sources = evidence_texts(conn, bundle)

    inventory = _ask(client, tracker, INVENTORY_PROMPT.format(answer=answer),
                     ClaimInventory, "judge.inventory")
    claims = list(inventory.claims) if inventory else []
    factual = [c for c in claims if c.is_factual]
    cited = [c for c in factual if c.citation_numbers]
    uncited = [c for c in factual if not c.citation_numbers]

    def judge_one(claim: Claim) -> dict:
        texts, locators, missing = [], [], []
        for n in claim.citation_numbers:
            src = sources.get(n)
            if src and src["text"]:
                texts.append(f"[{n}] {src['text'][:EVIDENCE_CHARS]}")
                locators.append(src["locator"])
            else:
                missing.append(n)
        if not texts:
            # cited a number with no retrievable text behind it: not the model
            # inventing a claim, but not verifiable either — never silently
            # counted as support
            return {"claim": claim.text, "citations": claim.citation_numbers,
                    "verdict": "unverifiable", "reason": f"no evidence text for {missing}",
                    "locators": []}
        verdict = _ask(client, tracker,
                       ENTAILMENT_PROMPT.format(claim=claim.text,
                                                source="\n\n".join(texts)),
                       Entailment, "judge.entailment")
        return {"claim": claim.text, "citations": claim.citation_numbers,
                "verdict": verdict.verdict if verdict else "unverifiable",
                "reason": verdict.reason if verdict else "judge returned nothing",
                "locators": locators}

    with ThreadPoolExecutor(max_workers=6) as pool:
        verdicts = list(pool.map(judge_one, cited))

    counts = {v: sum(1 for x in verdicts if x["verdict"] == v)
              for v in ("supported", "partial", "unsupported", "unverifiable")}
    judged = counts["supported"] + counts["partial"] + counts["unsupported"]
    return {
        "schema_version": SCHEMA_VERSION,
        "judge_model": JUDGE_MODEL,
        # a score is only as trustworthy as the judge behind it; carry that state
        # with the number so it can never be read as established fact
        "calibration": calibration or {"state": "uncalibrated"},
        # snapshot-judged or legacy-reconstructed — the two are NOT comparable
        "evidence_provenance": evidence_provenance(bundle),
        "claims": {"total": len(claims), "factual": len(factual),
                   "cited": len(cited), "uncited": len(uncited)},
        "verdicts": verdicts,
        "uncited_claims": [c.text for c in uncited],
        "counts": counts,
        "scores": {
            # strict: only full entailment counts. `partial` is reported, never
            # quietly folded into the numerator. `unverifiable` stays out of the
            # denominator (there is nothing to entail against) but is NEVER
            # silent: it gets its own label below, so a turn full of dead
            # citations cannot score clean by having nothing judgeable.
            "faithfulness": (counts["supported"] / judged) if judged else None,
            "citation_coverage": (len(cited) / len(factual)) if factual else None,
        },
        "labels": sorted(
            ({"synthesis_error"} if counts["unsupported"] or counts["partial"] else set())
            | ({"citation_coverage_error"} if uncited else set())
            | ({"unverifiable_citation"} if counts["unverifiable"] else set())),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("turn_id", type=int)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from x1_advisor.db import connect

    tracker = Tracker(run_id=f"judge:{args.turn_id}")
    with connect() as conn:
        row = conn.execute(
            "SELECT research_record FROM advisor.turns WHERE id = %s",
            (args.turn_id,)).fetchone()
        if not row or not row["research_record"]:
            sys.exit(f"turn {args.turn_id} has no research_record")
        result = judge_bundle(conn, row["research_record"], tracker=tracker,
                              calibration=calibration_state())

    if args.json:
        print(json.dumps(result, indent=2))
        return
    s, c = result["scores"], result["claims"]
    print(f"turn {args.turn_id}  judge={result['judge_model']}  "
          f"calibration={result['calibration']['state']}  "
          f"evidence={result['evidence_provenance']}")
    if result["evidence_provenance"] == "reconstructed-legacy":
        print("  ⚠ pre-snapshot bundle: judged against the CURRENT database, "
              "not what the model saw — do not compare with snapshot-judged scores")
    print(f"claims: {c['factual']} factual ({c['cited']} cited, {c['uncited']} uncited)")
    def pct(v: float | None) -> str:
        return "n/a" if v is None else format(v, ".2f")

    n = result["counts"]
    print(f"faithfulness      {pct(s['faithfulness'])}   "
          f"({n['supported']} supported, {n['partial']} partial, "
          f"{n['unsupported']} unsupported, {n['unverifiable']} unverifiable)")
    print(f"citation coverage {pct(s['citation_coverage'])}")
    for v in result["verdicts"]:
        if v["verdict"] != "supported":
            print(f"  ! {v['verdict']:<12} {v['claim'][:80]}")
            print(f"    {v['reason'][:110]}")
    for u in result["uncited_claims"]:
        print(f"  ? uncited      {u[:80]}")
    print(f"\nlabels: {result['labels'] or 'none'}   cost ${tracker.run_total:.4f}")


if __name__ == "__main__":
    main()
