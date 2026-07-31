"""Run comparator (Gate 1C; contract-aware since Gate 1D-3) — what changed
between two runs, and was it a fix?

Two manifests in, one verdict out. The order matters: **fingerprint diff first**,
because "recall went up" is not a finding until you know whether the code, the
prompt, the models or the corpus moved underneath it. We watched recall move
0.778 → 0.833 with zero code change when record summaries landed; a comparator
that only diffed metrics would have credited that to whatever commit was
nearest.

**Runs are compared under a named scoring contract.** "Pass" means a different
thing on a retrieval run (recall), an unjudged agent run (citations resolve),
and a judged run (no judge labels). The first version of this tool silently
picked a definition per record, so comparing a pre-judge manifest against a
post-judge one reported 17 phantom regressions — the questions didn't get
worse, the bar moved. Differing contracts now yield NOT COMPARABLE (exit 2)
and only shared metrics are shown; a gate that cannot say "pass" must say
"cannot compare", never guess.

The gate is deliberately **suite-aware** rather than uniformly zero-regression
(QA-LOOP-DESIGN §4.5). Retrieval runs are deterministic — same query, same
corpus, same result — so ANY per-question recall/MRR decrease blocks, not just
a full-recall flip (0.8 → 0.4 used to slip through as "still failing"). Agent
runs are stochastic: the model picks different search queries on identical
input, so a single question flipping is noise and only a budget overrun is
signal.

**Stochastic runs gate on three things, because pass-flips alone are vacuous
when every question already fails** (1E review, finding 1: with 0/20 passing,
faithfulness could collapse to zero and flip nothing):

  1. net pass-flips            ≤ --budget
  2. net label worsening       ≤ --budget   (questions gaining failure labels
                                             minus questions only losing them)
  3. mean faithfulness / citation_coverage drop over shared questions
                               ≤ --score-drop (default 0.10)

The 0.10 default is MEASURED, not guessed (1E-4, 2026-07-31): re-judging the
same 20 answers moved mean faithfulness by 0.070 (per-question swings ±0.2–0.4
— the judge re-inventories claims each pass), and a full replicate run moved
it 0.067. A threshold below the judge's own noise floor flags noise as
regression; one at 0.10 catches collapse while riding out the wobble. The
real shrink comes from a bigger suite (Gate 4) — n=20 puts ~±0.05 on any mean.

Run: uv run python -m experiments.compare <before.jsonl> <after.jsonl>
Exit: 0 pass · 1 fail · 2 not comparable
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

RUNS_DIR = Path(__file__).parent / "runs"
# a stochastic suite needs a budget, not a zero: below this many net regressions
# the run is noise, above it something moved
DEFAULT_REGRESSION_BUDGET = 2


def load(path: Path) -> tuple[list[dict], dict | None]:
    records, summary = [], None
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("question_id"):
            records.append(rec)
        elif rec.get("record") == "summary":
            summary = rec
    return records, summary


def fingerprint_of(records: list[dict], summary: dict | None) -> dict[str, Any]:
    # retrieval runs put it on the summary record (once), agent runs per question
    for r in ([summary] if summary else []) + records:
        fp = (r or {}).get("fingerprint")
        if fp:
            return fp
    # pre-Gate-1A manifests carry only a git sha
    return {k: records[0].get(k) for k in ("git_sha", "code_fingerprint", "config_id")
            if records and records[0].get(k)}


def flatten(fp: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (fp or {}).items():
        if isinstance(v, dict):
            out.update(flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def contract_of(records: list[dict]) -> str:
    """The scoring contract a RUN was graded under — decided per run, never per
    record, so a mixed manifest cannot flip definitions mid-comparison.

    Evidence provenance is PART of the judged contract: a legacy-judged run
    (judge read the live DB) and a snapshot-judged run (judge read what the
    model saw) measure different things, and runbook rule 5 forbids comparing
    them. Records with no provenance field predate snapshots — legacy.

    So is the judge MODEL. A faithfulness number is that model's reading of
    the rubric: swap the judge and the scale moves under you, exactly as it
    does when provenance changes. Without this, changing judges would look
    like the agent got better or worse overnight — the same phantom-regression
    trap 1D-3 closed for the appearance of the judge column. Manifests written
    before the judge model was recorded fall back to `unknown-judge`, which is
    still a distinct contract: it is not comparable to a named one, because
    nothing says it was the same model."""
    if all(r.get("experiment") == "phase2-baseline" for r in records):
        return "retrieval"
    judged = [r for r in records
              if r.get("judge")
              or (r.get("scores") or {}).get("faithfulness") is not None]
    if judged:
        prov = {(r.get("judge") or {}).get("evidence_provenance")
                or "reconstructed-legacy" for r in judged}
        models = {(r.get("judge") or {}).get("judge_model") or "unknown-judge"
                  for r in judged}
        return (f"judged/{prov.pop() if len(prov) == 1 else 'mixed-provenance'}"
                f"/{models.pop() if len(models) == 1 else 'mixed-judge'}")
    return "citation-liveness"


def passed(rec: dict, contract: str) -> bool | None:
    """Did this question pass UNDER THE GIVEN CONTRACT? None when the record
    cannot say — an ungraded record is reported, never guessed at."""
    if contract == "retrieval":
        return rec.get("recall") == 1.0
    if contract.startswith("judged"):
        j = rec.get("judge") or {}
        if j.get("labels") is not None:
            return not j["labels"]
        scores = rec.get("scores") or {}
        if scores.get("faithfulness") is not None:
            # pre-1D manifests: no label projection, scores only
            return (scores["faithfulness"] == 1.0
                    and scores.get("citation_coverage") == 1.0)
        return None
    stats = rec.get("citation_stats") or {}
    if stats.get("emitted"):
        return stats["resolved"] == stats["emitted"]
    return None


def metric(rec: dict, name: str) -> float | None:
    for source in (rec, rec.get("scores") or {}, rec.get("summary") or {}):
        if isinstance(source, dict) and source.get(name) is not None:
            return source[name]
    return None


METRICS = ("recall", "mrr", "faithfulness", "citation_coverage",
           "cost_usd", "latency_ms")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--budget", type=int, default=DEFAULT_REGRESSION_BUDGET,
                    help="net regressions tolerated on a stochastic suite")
    ap.add_argument("--score-drop", type=float, default=0.10,
                    help="max tolerated drop in mean faithfulness/coverage on "
                         "a stochastic suite (default measured from the "
                         "2026-07-31 rejudge/replicate noise floor)")
    args = ap.parse_args()

    paths = []
    for arg in (args.before, args.after):
        p = Path(arg)
        paths.append(p if p.exists() else RUNS_DIR / arg)
    (before, before_sum), (after, after_sum) = load(paths[0]), load(paths[1])
    a_by, b_by = {r["question_id"]: r for r in before}, {r["question_id"]: r for r in after}
    ca, cb = contract_of(before), contract_of(after)
    comparable = ca == cb

    # --- what moved underneath the numbers --------------------------------
    fa = flatten(fingerprint_of(before, before_sum))
    fb = flatten(fingerprint_of(after, after_sum))
    changed = {k: (fa.get(k), fb.get(k)) for k in sorted(set(fa) | set(fb))
               if fa.get(k) != fb.get(k)}
    print(f"{paths[0].name}\n  ->  {paths[1].name}\n")
    print("== fingerprint ==")
    if not changed:
        print("  identical — any metric change is noise or data outside the fingerprint")
    for k, (x, y) in changed.items():
        print(f"  {k:<34} {str(x)[:28]:<30} -> {str(y)[:28]}")

    # --- scoring contract ---------------------------------------------------
    print(f"\n== scoring contract ==\n  before: {ca}   after: {cb}")
    if not comparable:
        print("  DIFFERENT CONTRACTS — 'pass' means different things in these "
              "two runs, so\n  per-question transitions would be phantom "
              "regressions (finding 4: 17 of them).\n  Showing shared metrics "
              "only; the verdict is NOT COMPARABLE, not pass/fail.")

    # --- per-question transitions (same contract only) ----------------------
    buckets: dict[str, list[str]] = {"fixed": [], "broken": [], "still_failing": [],
                                     "still_passing": [], "ungraded": [],
                                     "added": [], "removed": []}
    if comparable:
        print("\n== questions ==")
        for qid in sorted(set(a_by) | set(b_by)):
            if qid not in a_by:
                buckets["added"].append(qid)
                continue
            if qid not in b_by:
                buckets["removed"].append(qid)
                continue
            pa, pb = passed(a_by[qid], ca), passed(b_by[qid], cb)
            key = ("ungraded" if pa is None or pb is None
                   else "still_passing" if pa and pb else "fixed" if pb and not pa
                   else "broken" if pa and not pb else "still_failing")
            buckets[key].append(qid)
        for key in ("broken", "fixed", "still_failing", "ungraded",
                    "added", "removed"):
            if buckets[key]:
                print(f"  {key:<14} {len(buckets[key]):>3}  {', '.join(buckets[key])}")
        print(f"  {'still_passing':<14} {len(buckets['still_passing']):>3}")

    # --- deterministic per-question metric regressions ----------------------
    # recall 0.8 -> 0.4 is a real regression even though both "fail"; MRR can
    # sink while recall holds 1.0. On a deterministic suite every decrease is
    # signal, so every decrease is listed and any decrease blocks.
    metric_regressions: list[str] = []
    if comparable and ca == "retrieval":
        for qid in sorted(set(a_by) & set(b_by)):
            for name in ("recall", "mrr"):
                x, y = metric(a_by[qid], name), metric(b_by[qid], name)
                if x is not None and y is not None and y < x - 1e-9:
                    metric_regressions.append(f"{qid} {name} {x:.3f} -> {y:.3f}")
        if metric_regressions:
            print("\n== per-question metric regressions ==")
            for line in metric_regressions:
                print(f"  {line}")

    # --- label shifts (funnel + judge labels ride in the manifest, 1D-3) ----
    def labels_of(rec):
        if rec.get("labels") is not None:            # run.py agent manifests
            return set(rec["labels"])
        j = rec.get("judge") or {}
        return set(j.get("labels") or [])

    if comparable:
        shifts = {qid: (labels_of(a_by[qid]), labels_of(b_by[qid]))
                  for qid in set(a_by) & set(b_by)
                  if labels_of(a_by[qid]) != labels_of(b_by[qid])}
        if shifts:
            print("\n== label shifts ==")
            for qid, (x, y) in sorted(shifts.items()):
                print(f"  {qid:<7} -{sorted(x - y) or '[]'}  +{sorted(y - x) or '[]'}")

    # --- aggregate metrics --------------------------------------------------
    print("\n== metrics ==")
    for name in METRICS:
        xs = [metric(a_by[q], name) for q in a_by if metric(a_by[q], name) is not None]
        ys = [metric(b_by[q], name) for q in b_by if metric(b_by[q], name) is not None]
        if not xs and not ys:
            continue
        ax = sum(xs) / len(xs) if xs else None
        ay = sum(ys) / len(ys) if ys else None
        if ax is None or ay is None:
            print(f"  {name:<20} {'n/a' if ax is None else f'{ax:.4f}':>10}"
                  f" -> {'n/a' if ay is None else f'{ay:.4f}':>10}   "
                  "(not comparable — absent from one run)")
            continue
        print(f"  {name:<20} {ax:>10.4f} -> {ay:>10.4f}   {ay - ax:+.4f}")

    # --- suite-aware verdict ------------------------------------------------
    print("\n== verdict ==")
    if not comparable:
        print(f"  scoring contracts differ ({ca} vs {cb}) — re-run one side "
              "under the other's contract to gate")
        print("\nCOMPARE: NOT COMPARABLE")
        sys.exit(2)
    if ca == "retrieval":
        # deterministic: any per-question metric decrease is real and blocks,
        # not just pass-flips
        ok = not buckets["broken"] and not metric_regressions
        print(f"  deterministic suite: {len(buckets['broken'])} pass-flip(s), "
              f"{len(metric_regressions)} per-question metric regression(s); "
              "the bar is zero")
    else:
        shared = set(a_by) & set(b_by)
        # gate 1: pass-flips (vacuous when everything already fails — which is
        # exactly why it cannot be the only gate)
        net = len(buckets["broken"]) - len(buckets["fixed"])
        # gate 2: label worsening — a question gaining any failure label
        # counts against; one only shedding labels counts for
        gained = [q for q in sorted(shared)
                  if labels_of(b_by[q]) - labels_of(a_by[q])]
        lost_only = [q for q in sorted(shared)
                     if (labels_of(a_by[q]) - labels_of(b_by[q]))
                     and not (labels_of(b_by[q]) - labels_of(a_by[q]))]
        net_labels = len(gained) - len(lost_only)
        # gate 3: mean score drops over the SHARED questions (paired sets —
        # added/removed questions must not masquerade as movement)
        score_fails = []
        for name in ("faithfulness", "citation_coverage"):
            pairs = [(metric(a_by[q], name), metric(b_by[q], name))
                     for q in shared]
            pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
            if not pairs:
                continue
            ma = sum(x for x, _ in pairs) / len(pairs)
            mb = sum(y for _, y in pairs) / len(pairs)
            if ma - mb > args.score_drop:
                score_fails.append(f"{name} {ma:.3f} -> {mb:.3f} "
                                   f"(drop {ma - mb:.3f} > {args.score_drop})")
        ok = (net <= args.budget and net_labels <= args.budget
              and not score_fails)
        print(f"  stochastic suite: {len(buckets['broken'])} broken, "
              f"{len(buckets['fixed'])} fixed, net {net:+d}; budget {args.budget}")
        print(f"  labels: {len(gained)} question(s) gained labels "
              f"({', '.join(gained) or '-'}), {len(lost_only)} only lost, "
              f"net {net_labels:+d}; budget {args.budget}")
        for f in score_fails:
            print(f"  SCORE REGRESSION: {f}")
        print("  (single flips are noise on a stochastic suite — the gates are "
              "net flips, net label worsening, and mean score drops)")
        if buckets["ungraded"]:
            print(f"  ungraded (cannot say pass/fail): {buckets['ungraded']}")
    print(f"\nCOMPARE: {'PASS' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
