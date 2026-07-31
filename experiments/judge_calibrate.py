"""Calibrate the claim/citation judge (Gate 1B-3).

"An LLM judge is appropriate for scale, but calibrate it against a small
human-labeled claim/citation set. Do not turn the judge's own score into another
unverified proxy." — ARCHITECTURE-PLAN-REVIEW-2026-07-30

Two label provenances, and the distinction is the whole point:

* **synthetic** — known-answer cases built by objective mutation (a number
  changed, an entity swapped, a hedge hardened into a claim). Ground truth is
  mechanical, so these catch a judge that is broken: one that rubber-stamps
  everything, or that leaks outside knowledge. They are *not* evidence that the
  judge agrees with a human on real, ambiguous text.
* **human** — real (claim, evidence) pairs sampled from actual turns and labeled
  by a person. Only these support a claim of agreement on the real distribution.

So the reported state is deliberately conservative: `synthetic-only` until enough
human labels exist, and every judged turn carries that state next to its score.

Add human labels:
  uv run python -m experiments.judge_calibrate --sample 20   # emit unlabeled pairs
  # fill in "label" on each line, set provenance to "human"
  uv run python -m experiments.judge_calibrate                # score agreement
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import OpenAI

from x1_advisor.agent.judge import (CALIBRATION_SET, ENTAILMENT_PROMPT,
                                    JUDGE_MODEL, LABELS, MIN_HUMAN_LABELS,
                                    Entailment, _ask, calibration_state,
                                    load_calibration_set)
from x1_advisor.agent.bundle import QA_ARTIFACTS_DIR
from x1_advisor.cost import Tracker

# the judge owns the set path and the trust rules; this module only measures
SET_PATH = CALIBRATION_SET
load = load_calibration_set


def cohens_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Agreement corrected for chance — raw accuracy flatters a skewed set."""
    n = len(pairs)
    if not n:
        return None
    observed = sum(1 for a, b in pairs if a == b) / n
    ga, gb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum((ga[k] / n) * (gb[k] / n) for k in set(ga) | set(gb))
    return None if expected == 1 else (observed - expected) / (1 - expected)


def sample_pairs(limit: int, run: str | None = None) -> int:
    """Append unlabeled real (claim, evidence) pairs for a human labelling pass.

    Claims come from the judge's own inventory in exported bundles, so a labeller
    reads a real claim against the real evidence and only has to supply a
    verdict — no hand-extraction of sentences.

    **Stratified across the judge's verdicts, and blind.** Random sampling would
    be almost all `supported`, which makes kappa unstable and teaches nothing
    about the boundary that actually matters. Each item therefore records the
    `stratum` it was drawn from so agreement can be reweighted to the population
    later — and the judge's verdict is deliberately NOT written to the file, so
    labelling is not anchored by it.
    """
    from x1_advisor.agent.judge import evidence_texts
    from x1_advisor.db import connect

    existing = {i["id"] for i in load()}
    runs_dir = QA_ARTIFACTS_DIR / run if run else QA_ARTIFACTS_DIR
    buckets: dict[str, list[dict]] = {"supported": [], "partial": [],
                                      "unsupported": [], "unverifiable": []}
    with connect() as conn:
        for path in sorted(runs_dir.rglob("*.json")):
            bundle = json.loads(path.read_text())
            verdicts = (bundle.get("judge") or {}).get("verdicts") or []
            if not verdicts:
                continue
            sources = evidence_texts(conn, bundle)
            for k, v in enumerate(verdicts):
                text = "\n\n".join(
                    sources[n]["text"] for n in v["citations"]
                    if sources.get(n) and sources[n]["text"])
                item_id = f"{path.stem}_v{k}"
                if not text or item_id in existing:
                    continue
                buckets.setdefault(v["verdict"], []).append({
                    "id": item_id, "provenance": "unlabeled", "label": None,
                    "stratum": v["verdict"],       # NOT the answer — the bucket
                    "claim": v["claim"], "evidence": text[:3000],
                    "note": ", ".join(v["locators"]) or path.stem,
                })

    # round-robin across strata so the set is balanced rather than
    # supported-dominated; take whatever each bucket can give
    picked: list[dict] = []
    order = ["unsupported", "partial", "supported", "unverifiable"]
    while len(picked) < limit and any(buckets[b] for b in order):
        for b in order:
            if buckets[b] and len(picked) < limit:
                picked.append(buckets[b].pop(0))
    with SET_PATH.open("a") as fh:
        for item in picked:
            fh.write(json.dumps(item) + "\n")
    return len(picked)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=0,
                    help="append N unlabeled real pairs for a human pass")
    ap.add_argument("--run", default=None,
                    help="sample from one .qa-artifacts run directory")
    args = ap.parse_args()

    if args.sample:
        n = sample_pairs(args.sample, run=args.run)
        counts = Counter(i["stratum"] for i in load() if i.get("provenance") == "unlabeled")
        print(f"appended {n} unlabeled pairs to {SET_PATH}")
        print(f"strata: {dict(counts)}  (balanced on purpose — reweight before "
              "reading accuracy as a population estimate)")
        print("For each: read `claim` against `evidence`, set `label` to "
              "supported | partial | unsupported, and change `provenance` to "
              '"human". The judge\'s own verdict is deliberately absent so the '
              "labelling is not anchored by it.")
        return

    items = [i for i in load() if i.get("label") in LABELS]
    if not items:
        sys.exit(f"no labeled items in {SET_PATH}")
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    tracker = Tracker(run_id="judge-calibration")

    def run(item: dict) -> tuple[dict, str]:
        v = _ask(client, tracker,
                 ENTAILMENT_PROMPT.format(claim=item["claim"], source=item["evidence"]),
                 Entailment, "judge.calibrate")
        return item, (v.verdict if v else "unverifiable")

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, items))

    pairs = [(i["label"], got) for i, got in results]
    accuracy = sum(1 for a, b in pairs if a == b) / len(pairs)
    kappa = cohens_kappa(pairs)

    print(f"judge: {JUDGE_MODEL}   items: {len(items)}")
    for prov in ("synthetic", "human"):
        subset = [(i, g) for i, g in results if i.get("provenance") == prov]
        if subset:
            acc = sum(1 for i, g in subset if i["label"] == g) / len(subset)
            print(f"  {prov:<10} n={len(subset):<4} accuracy {acc:.2f}")
    print(f"overall accuracy {accuracy:.2f}   Cohen's kappa "
          f"{'n/a' if kappa is None else format(kappa, '.2f')}")

    # the safety-critical direction: a judge that calls unsupported claims
    # supported is worse than one that is merely strict
    missed = [(i, g) for i, g in results
              if i["label"] == "unsupported" and g == "supported"]
    print(f"unsupported judged supported (false clean bill): {len(missed)}")
    for i, g in results:
        if i["label"] != g:
            print(f"  ! {i['id']:<14} expected {i['label']:<12} got {g:<12} {i['note']}")

    state = calibration_state()
    print(f"\ncalibration state: {state['state']} "
          f"({state['human_labels']} human / {state['synthetic_labels']} synthetic; "
          f"{MIN_HUMAN_LABELS} human needed for 'human-calibrated')")
    print(f"cost ${tracker.run_total:.4f}")


if __name__ == "__main__":
    main()
