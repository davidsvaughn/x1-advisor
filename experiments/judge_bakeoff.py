"""Pick the judge model by measured agreement, not by capability marketing.

A judge's job is not to be the strongest model available — it is to apply the
entailment rubric the way a careful person does. Those are different targets,
and only one of them is measurable here: run every candidate over the SAME
(claim, evidence) pairs and score each against the human labels.

    uv run python -m experiments.judge_bakeoff                 # all candidates
    uv run python -m experiments.judge_bakeoff --models gpt-5.1,gpt-5.6-terra

References, in descending authority:

* **human** — labels in `experiments/judge_calibration.jsonl` (provenance
  human). The only ground truth on real, ambiguous text. Everything below is a
  sanity check.
* **synthetic** — known-answer cases built by mechanical mutation (a number
  changed, an entity swapped). Ground truth, but of the "is this judge broken"
  kind: passing says nothing about agreement with a person on real text.
* **assist** — an assistant's blind second read, if
  `.qa-artifacts/calibration/assist.jsonl` exists. NOT ground truth: it is one
  more model, and picking the judge that best matches another model selects for
  shared bias. Reported to show spread, never to decide.

Pairwise model agreement is printed too, because it answers the question that
comes before "which is best": do the candidates even disagree? If they agree
99% of the time, the choice is a cost decision and nothing more.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import OpenAI

from experiments.judge_calibrate import (ITEMS_PATH, _load_jsonl,
                                         cohens_kappa, load)
from x1_advisor.agent.judge import (ENTAILMENT_PROMPT, Entailment, LABELS,
                                    _ask)
from x1_advisor.cost import Tracker

CANDIDATES = ["gpt-5.1", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
ASSIST_PATH = ITEMS_PATH.parent / "assist.jsonl"
OUT_PATH = ITEMS_PATH.parent / "bakeoff.jsonl"


def run_model(client: OpenAI, model: str, items: list[dict]) -> tuple[dict, float, float]:
    """Every candidate sees byte-identical prompts — the only variable is the model."""
    tracker = Tracker(run_id=f"judge-bakeoff-{model}")
    t0 = time.time()

    def one(item: dict) -> tuple[str, str]:
        v = _ask(client, tracker,
                 ENTAILMENT_PROMPT.format(claim=item["claim"],
                                          source=item["evidence"]),
                 Entailment, "judge.bakeoff", model=model)
        return item["id"], (v.verdict if v else "unverifiable")

    with ThreadPoolExecutor(max_workers=6) as pool:
        verdicts = dict(pool.map(one, items))
    return verdicts, tracker.run_total, time.time() - t0


def agreement(a: dict[str, str], b: dict[str, str]) -> tuple[int, int, float | None]:
    pairs = [(a[k], b[k]) for k in a if k in b]
    hits = sum(1 for x, y in pairs if x == y)
    return hits, len(pairs), cohens_kappa(pairs)


def col(a: dict[str, str], ref: dict[str, str], width: int = 12) -> str:
    """Agreement cell, or an explicit dash when the two sets never overlap —
    a blank would read as 'no agreement' rather than 'nothing measured'."""
    hits, n, _ = agreement(a, ref)
    return ("—".rjust(width) if not n
            else f"{hits}/{n} ({hits / n:.2f})".rjust(width))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", default=",".join(CANDIDATES))
    ap.add_argument("--limit", type=int, default=0, help="first N items (smoke test)")
    ap.add_argument("--labeled-only", action="store_true",
                    help="score only items that already carry a label (unlabeled "
                         "items cost money and add nothing to the decision)")
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    items = _load_jsonl(ITEMS_PATH)
    if args.labeled_only:
        have = {i["id"] for i in load() if i.get("label") in LABELS}
        items = [i for i in items if i["id"] in have]
    if args.limit:
        items = items[:args.limit]
    if not items:
        raise SystemExit(f"no calibration bodies in {ITEMS_PATH} — run --sample first")

    human = {i["id"]: i["label"] for i in load()
             if i.get("provenance") == "human" and i.get("label") in LABELS}
    synth = {i["id"]: i["label"] for i in load()
             if i.get("provenance") == "synthetic" and i.get("label") in LABELS}
    assist = {r["id"]: r["label"]
              for r in _load_jsonl(ASSIST_PATH) if r.get("label") in LABELS}

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    results, costs, secs = {}, {}, {}
    for m in models:
        results[m], costs[m], secs[m] = run_model(client, m, items)
        print(f"  ran {m:<15} {len(results[m])} items  ${costs[m]:.4f}  {secs[m]:.0f}s")

    print(f"\n== judge bake-off: {len(items)} pairs, identical prompts ==")
    human_note = (f"{len(human)} human-labeled" if human else
                  "NO human labels yet — the column that decides is missing")
    print(f"references: {human_note}"
          + (f"; {len(assist)} assistant labels (indicative only)" if assist else ""))

    header = f"\n{'model':<16}{'cost':>9}{'sec':>6}"
    if human:
        header += f"{'vs human':>12}{'kappa':>8}{'false clean':>13}"
    if synth:
        header += f"{'vs synth':>12}"
    if assist:
        header += f"{'vs assist':>12}"
    print(header)
    for m in models:
        row = f"{m:<16}{costs[m]:>9.4f}{secs[m]:>6.0f}"
        if human:
            hits, n, k = agreement(results[m], human)
            # the asymmetric error that matters: an unsupported claim waved through
            miss = sum(1 for i, lab in human.items()
                       if lab == "unsupported" and results[m].get(i) == "supported")
            row += (col(results[m], human)
                    + ("n/a" if k is None else f"{k:.2f}").rjust(8)
                    + str(miss).rjust(13))
        if synth:
            row += col(results[m], synth)
        if assist:
            row += col(results[m], assist)
        print(row)

    print("\nverdict mix (a judge that never says 'unsupported' cannot fail an answer):")
    for m in models:
        print(f"  {m:<16}{dict(Counter(results[m].values()))}")

    if len(models) > 1:
        print("\npairwise agreement:")
        for i, a in enumerate(models):
            for b in models[i + 1:]:
                hits, n, k = agreement(results[a], results[b])
                print(f"  {a:<15} vs {b:<15} {hits}/{n} ({hits/n:.2f})"
                      f"  kappa {'n/a' if k is None else format(k, '.2f')}")

    rows = [{"id": it["id"], "stratum": it.get("stratum"),
             "human": human.get(it["id"]), "synthetic": synth.get(it["id"]),
             "assist": assist.get(it["id"]),
             **{m: results[m].get(it["id"]) for m in models}} for it in items]
    OUT_PATH.write_text("".join(json.dumps(r) + "\n" for r in rows))
    os.chmod(OUT_PATH, 0o600)
    print(f"\nper-item verdicts: {OUT_PATH}")
    print(f"total cost ${sum(costs.values()):.4f}")
    if not human:
        print("\n⚠ No human labels — this run shows SPREAD, not correctness. "
              "Nothing here says which judge is right.")


if __name__ == "__main__":
    main()
