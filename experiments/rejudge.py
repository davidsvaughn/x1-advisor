"""Re-judge an existing run's bundles under the current judge (Gate 1E-4).

Judge semantics change — evidence snapshots, clip removal, model swaps — and
the only honest way to attribute a score movement to the *judge* is a PAIRED
pass: same bundles, same answers, fresh judgment. "0.584 → 0.569 because
snapshot judging is stricter" was once claimed across two different stochastic
agent runs; that claim was unattributable and got retracted (1E review,
finding 3). This tool makes such claims earnable.

Originals are never touched: exported bundles and the source manifest are
immutable records of what actually ran. Output is a NEW manifest whose records
carry fresh judge verdicts and recomputed funnel labels, and the console
report is the paired before/after.

Run: uv run python -m experiments.rejudge <run-directory-name> [--golden v1]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import yaml

from experiments.funnel import classify
from experiments.manifest import code_fingerprint, git_sha, open_new_manifest
from x1_advisor.agent.bundle import QA_ARTIFACTS_DIR, manifest_record
from x1_advisor.agent.judge import calibration_state, judge_bundle
from x1_advisor.cost import JsonlSink, Tracker
from x1_advisor.db import connect

GOLDEN_DIR = Path(__file__).parent / "golden"
RUNS_DIR = Path(__file__).parent / "runs"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run", help="run directory under .qa-artifacts/runs/")
    ap.add_argument("--golden", default="v1")
    args = ap.parse_args()

    golden = yaml.safe_load((GOLDEN_DIR / f"{args.golden}.yaml").read_text())
    by_id = {q["id"]: q for q in golden["questions"]}
    run_dir = QA_ARTIFACTS_DIR / args.run
    if not run_dir.is_dir():
        sys.exit(f"no such run directory: {run_dir}")

    # paired baseline: the source manifest's per-question scores, if present
    before: dict[str, dict] = {}
    src_manifest = RUNS_DIR / f"{args.run}.jsonl"
    if src_manifest.exists():
        for line in src_manifest.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                if rec.get("question_id"):
                    before[rec["question_id"]] = rec.get("scores") or {}

    tracker = Tracker(run_id=f"rejudge:{args.run}",
                      sink=JsonlSink(os.environ.get("ADVISOR_COST_LEDGER",
                                                    "cost_ledger.jsonl")))
    calibration = calibration_state()
    run_id, manifest_path, manifest_file = open_new_manifest(
        f"{dt.date.today()}_rejudge_{args.run}")
    rows = []
    with connect() as conn, manifest_file as manifest:
        for path in sorted(run_dir.glob("*.json")):
            q = by_id.get(path.stem)
            if not q:
                continue
            bundle = json.loads(path.read_text())
            verdict = judge_bundle(conn, bundle, tracker=tracker,
                                   calibration=calibration)
            # in-memory only — the exported bundle stays exactly as recorded
            b2 = {**bundle, "judge": verdict,
                  "scores": {**(bundle.get("scores") or {}), **verdict["scores"]}}
            fun = classify(conn, b2, q)
            manifest.write(json.dumps({
                "run_id": run_id, "experiment": "phase4-agent",
                "rejudge_of": args.run,
                "git_sha": git_sha(), "code_fingerprint": code_fingerprint(),
                "question_id": q["id"], "category": q["category"],
                "labels": fun["labels"], "notes": fun["notes"],
                "routes": fun["routes"],
                **manifest_record(b2),
            }, default=str) + "\n")
            s = verdict["scores"]
            rows.append((q["id"], before.get(q["id"], {}), s))
            print(f"  {q['id']}  faithfulness "
                  f"{(before.get(q['id'], {}).get('faithfulness'))} -> "
                  f"{s['faithfulness']}   coverage "
                  f"{(before.get(q['id'], {}).get('citation_coverage'))} -> "
                  f"{s['citation_coverage']}")

    def mean(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    fb = mean([b.get("faithfulness") for _, b, _ in rows])
    fa = mean([a.get("faithfulness") for _, _, a in rows])
    cb = mean([b.get("citation_coverage") for _, b, _ in rows])
    ca = mean([a.get("citation_coverage") for _, _, a in rows])
    print(f"\n== rejudge {args.run} ({len(rows)} questions, PAIRED) ==")
    fmt = lambda v: "n/a" if v is None else f"{v:.3f}"  # noqa: E731
    print(f"faithfulness      {fmt(fb)} -> {fmt(fa)}")
    print(f"citation coverage {fmt(cb)} -> {fmt(ca)}")
    print(f"manifest: {manifest_path}")
    print(f"judge cost: ${tracker.run_total:.4f}")


if __name__ == "__main__":
    main()
