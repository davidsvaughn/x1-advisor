"""Paired re-judge of a golden v2 run under the current judge backend.

Same bundles, same answers, fresh judgment — the only honest way to attribute
a score movement to the JUDGE (the same rule rejudge.py earned for v1 after
the 1E review retraction). Agent outputs, deterministic assertions, truth
grades and behavior verdicts are carried over untouched; only the declared
`judged:*` units (and, for scripts, the per-turn `t{n}:judged:*` units) are
recomputed from the fresh verdicts, and `pass` is re-composed.

Originals are never touched. Output is a NEW manifest whose rows keep the
original run identity (bindings, truth digests, agent git_sha) plus a
`rejudge` provenance block; the fresh judge verdicts land in the manifest as
the body-free projection only, with the full verdicts (claim text, reasons)
exported owner-only next to the source bundles' new run directory. The new
judge_model in the projections changes the effective scoring contract, so the
comparator will refuse to gate rejudged rows against live-judged ones — the
paired read is THIS tool's console report.

Run: uv run python -m experiments.rejudge_v2 2026-08-04_v2_core_d8b1799_r2
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from experiments import checkers
from experiments.manifest import git_sha, open_new_manifest
from x1_advisor.agent.bundle import QA_ARTIFACTS_DIR, judge_manifest_projection
from x1_advisor.agent.judge import calibration_state, judge_bundle
from x1_advisor.cost import JsonlSink, Tracker

RUNS_DIR = Path(__file__).parent / "runs"
_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_(?P<exp>.+)_[0-9a-f]{7}"
                      r"(?:\+dirty)?_r\d+$")


def _recompute_judged(units: dict, prefix: str, verdict: dict | None) -> dict:
    """Overwrite the `<prefix>judged:*` keys in-place from a fresh verdict."""
    labels = set((verdict or {}).get("labels") or [])
    for key in units:
        if key.startswith(f"{prefix}judged:"):
            dim = key.rsplit("judged:", 1)[1]
            units[key] = (not (labels & checkers.DIMENSION_FAIL_LABELS[dim])
                          if verdict else None)
    return units


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run", help="source manifest name (without .jsonl)")
    args = ap.parse_args()
    src_name = args.run.removesuffix(".jsonl")

    src_manifest = RUNS_DIR / f"{src_name}.jsonl"
    bundle_dir = QA_ARTIFACTS_DIR / src_name
    if not src_manifest.exists():
        sys.exit(f"no such manifest: {src_manifest}")
    if not bundle_dir.is_dir():
        sys.exit(f"no bundle directory for run: {bundle_dir}")
    m = _NAME_RE.match(src_name)
    if not m:
        sys.exit(f"unrecognized manifest name shape: {src_name}")

    rows = [json.loads(line) for line in
            src_manifest.read_text().splitlines() if line.strip()]
    calibration = calibration_state()
    # `_cc` keeps the slice substring (e.g. `_v2_core`) intact for nightly's
    # per-slice comparator while naming what changed
    run_id, manifest_path, manifest_file = open_new_manifest(
        f"{dt.date.today()}_{m.group('exp')}_cc")
    tracker = Tracker(run_id=f"{run_id}:rejudge",
                      sink=JsonlSink(os.environ.get("ADVISOR_COST_LEDGER",
                                                    "cost_ledger.jsonl")))
    verdict_dir = QA_ARTIFACTS_DIR / run_id
    verdict_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(verdict_dir, 0o700)

    def _judge_one(bundle_name: str) -> tuple[str, dict | None]:
        path = bundle_dir / f"{bundle_name}.json"
        bundle = json.loads(path.read_text())
        # snapshot-judged bundles need no DB; a legacy bundle would need conn
        # and is refused rather than silently reconstructed
        if (bundle.get("schema_version") or 0) < 3:
            sys.exit(f"{bundle_name}: pre-snapshot bundle — rejudge would not "
                     "grade what the model saw")
        try:
            verdict = judge_bundle(None, bundle, tracker=tracker,
                                   calibration=calibration)
        except Exception as exc:                       # noqa: BLE001
            # one dead judge call must not kill a 47-bundle pass; the case
            # comes back UNGRADED (None) and is counted loudly below
            print(f"  ! judge failed on {bundle_name}: {exc}", flush=True)
            return bundle_name, None
        out = verdict_dir / f"{bundle_name}.judge.json"
        out.write_text(json.dumps(verdict, indent=1))
        os.chmod(out, 0o600)
        return bundle_name, verdict

    # every bundle that needs a fresh verdict, judged up front in parallel
    # (each is an independent subprocess); rows are then rebuilt in order
    wanted: list[str] = []
    for row in rows:
        units = row.get("graded_units") or {}
        if row.get("case_id") and any(k.startswith("judged:") for k in units):
            wanted.append(row["case_id"])
        elif row.get("script_id"):
            for t in row.get("turns") or []:
                if t.get("judge") is not None and any(
                        k.startswith(f"t{t.get('turn')}:judged:")
                        for k in units):
                    wanted.append(f"{row['script_id']}t{t.get('turn')}")
    print(f"judging {len(wanted)} bundle(s) under {calibration['judge_model']}…")
    with ThreadPoolExecutor(max_workers=4) as pool:
        verdicts = dict(pool.map(_judge_one, wanted))

    def fresh_verdict(bundle_name: str) -> dict | None:
        return verdicts[bundle_name]

    provenance = {"source_manifest": src_name, "rejudge_sha": git_sha(),
                  "judge": calibration["judge_model"]}
    flips, before_pass, after_pass, ungraded = [], 0, 0, 0
    with manifest_file as manifest:
        for row in rows:
            if row.get("record") == "summary":
                row = {**row, "record": "summary", "run_id": run_id,
                       "rejudge": provenance}
                manifest.write(json.dumps(row) + "\n")
                continue
            old_pass = row.get("pass")
            units = dict(row.get("graded_units") or {})

            if row.get("case_id") and any(k.startswith("judged:")
                                          for k in units):
                verdict = fresh_verdict(row["case_id"])
                _recompute_judged(units, "", verdict)
                row = {**row, "judge": judge_manifest_projection(verdict)}
            elif row.get("script_id"):
                turns = [dict(t) for t in row.get("turns") or []]
                for t in turns:
                    tn = t.get("turn")
                    if t.get("judge") is not None and any(
                            k.startswith(f"t{tn}:judged:") for k in units):
                        verdict = fresh_verdict(f"{row['script_id']}t{tn}")
                        _recompute_judged(units, f"t{tn}:", verdict)
                        t["judge"] = judge_manifest_projection(verdict)
                row = {**row, "turns": turns}
            else:
                manifest.write(json.dumps(row) + "\n")
                continue

            row["graded_units"] = units
            row["pass"] = checkers.compose_pass(units)
            row["rejudge"] = provenance
            manifest.write(json.dumps(row) + "\n")

            rid = row.get("case_id") or row.get("script_id")
            before_pass += old_pass is True
            after_pass += row["pass"] is True
            ungraded += row["pass"] is None
            if row["pass"] != old_pass:
                flips.append((rid, old_pass, row["pass"]))
            mark = {True: "PASS", False: "FAIL", None: "----"}
            print(f"  {mark[old_pass]} -> {mark[row['pass']]}  {rid}"
                  + ("  *" if row["pass"] != old_pass else ""))

    print(f"\npaired rejudge of {src_name} under {provenance['judge']}:")
    print(f"  passing {before_pass} -> {after_pass}"
          f"  ({len(flips)} flip(s), {ungraded} ungraded)")
    for rid, old, new in flips:
        print(f"    {rid}: {old} -> {new}")
    print(f"manifest: {manifest_path}")
    print(f"judge cost (API-equivalent): ${tracker.run_total:.2f}")


if __name__ == "__main__":
    main()
