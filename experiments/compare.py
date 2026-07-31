"""Run comparator (Gate 1C) — what changed between two runs, and was it a fix?

Two manifests in, one verdict out. The order matters: **fingerprint diff first**,
because "recall went up" is not a finding until you know whether the code, the
prompt, the models or the corpus moved underneath it. We watched recall move
0.778 → 0.833 with zero code change when record summaries landed; a comparator
that only diffed metrics would have credited that to whatever commit was
nearest.

The gate is deliberately **suite-aware** rather than uniformly zero-regression
(QA-LOOP-DESIGN §4.5). Retrieval runs are deterministic — same query, same
corpus, same result — so any regression there is real and blocks. Agent runs are
stochastic: the model picks different search queries on identical input, so a
single question flipping is noise and only a budget overrun is signal.

Run: uv run python -m experiments.compare <before.jsonl> <after.jsonl>
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


def passed(rec: dict) -> bool | None:
    """Did this question pass? None when the manifest cannot say."""
    if rec.get("experiment") == "phase2-baseline":
        return rec.get("recall") == 1.0
    scores = rec.get("scores") or {}
    if scores.get("faithfulness") is not None:
        # judged runs: full entailment of every cited claim, nothing uncited
        return scores["faithfulness"] == 1.0 and scores.get("citation_coverage") == 1.0
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
    args = ap.parse_args()

    paths = []
    for arg in (args.before, args.after):
        p = Path(arg)
        paths.append(p if p.exists() else RUNS_DIR / arg)
    (before, before_sum), (after, after_sum) = load(paths[0]), load(paths[1])
    a_by, b_by = {r["question_id"]: r for r in before}, {r["question_id"]: r for r in after}
    deterministic = all(r.get("experiment") == "phase2-baseline" for r in before + after)

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

    # --- per-question transitions -----------------------------------------
    print("\n== questions ==")
    buckets: dict[str, list[str]] = {"fixed": [], "broken": [], "still_failing": [],
                                     "still_passing": [], "added": [], "removed": []}
    for qid in sorted(set(a_by) | set(b_by)):
        if qid not in a_by:
            buckets["added"].append(qid)
            continue
        if qid not in b_by:
            buckets["removed"].append(qid)
            continue
        pa, pb = passed(a_by[qid]), passed(b_by[qid])
        key = ("still_passing" if pa and pb else "fixed" if pb and not pa
               else "broken" if pa and not pb else "still_failing")
        buckets[key].append(qid)
    for key in ("broken", "fixed", "still_failing", "added", "removed"):
        if buckets[key]:
            print(f"  {key:<14} {len(buckets[key]):>3}  {', '.join(buckets[key])}")
    print(f"  {'still_passing':<14} {len(buckets['still_passing']):>3}")

    # --- label shifts (funnel labels ride in the bundle, judge labels here) -
    def labels_of(rec):
        return set((rec.get("judge") or {}).get("labels")
                   or (rec.get("scores") or {}).get("labels") or [])

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
    net = len(buckets["broken"]) - len(buckets["fixed"])
    print("\n== verdict ==")
    if deterministic:
        ok = not buckets["broken"]
        print(f"  deterministic suite: {len(buckets['broken'])} regression(s); "
              "the bar is zero")
    else:
        ok = net <= args.budget
        print(f"  stochastic suite: {len(buckets['broken'])} broken, "
              f"{len(buckets['fixed'])} fixed, net {net:+d}; budget {args.budget}")
        print("  (the model chooses its own search queries, so single flips are "
              "noise — judge the net and the label shifts, not the individual)")
    print(f"\nCOMPARE: {'PASS' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
