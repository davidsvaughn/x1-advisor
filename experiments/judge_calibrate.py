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


def sample_pairs(limit: int) -> int:
    """Append unlabeled real (claim, evidence) pairs from stored turns."""
    from x1_advisor.agent.judge import evidence_texts
    from x1_advisor.db import connect

    existing = {i["id"] for i in load()}
    added = 0
    with connect() as conn, SET_PATH.open("a") as fh:
        rows = conn.execute(
            """SELECT id, research_record FROM advisor.turns
               WHERE role = 'assistant'
                 AND research_record ? 'validation'
               ORDER BY id DESC LIMIT 200""").fetchall()
        for row in rows:
            bundle = row["research_record"]
            sources = evidence_texts(conn, bundle)
            for c in bundle.get("validation", {}).get("citations", []):
                item_id = f"turn{row['id']}_c{c.get('n')}"
                src = sources.get(c.get("n"))
                if item_id in existing or not src or not src["text"]:
                    continue
                fh.write(json.dumps({
                    "id": item_id, "provenance": "unlabeled", "label": None,
                    "claim": "<<paste the sentence from the answer that carries "
                             f"citation [{c.get('n')}]>>",
                    "evidence": src["text"][:2000], "note": src["locator"],
                }) + "\n")
                added += 1
                if added >= limit:
                    return added
    return added


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=0,
                    help="append N unlabeled real pairs for a human pass")
    args = ap.parse_args()

    if args.sample:
        n = sample_pairs(args.sample)
        print(f"appended {n} unlabeled pairs to {SET_PATH}")
        print("fill in \"label\" (supported|partial|unsupported) and set "
              "\"provenance\": \"human\"")
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
