"""H1 deterministic QA harness (Track H; CC-AGENTS-DESIGN §4).

Hand-invoked, always — scheduled/unattended runs are a production-mode
concept and are deliberately not a dev thing (David, DECISIONS 2026-08-05;
this file was born as `nightly.py` for that imagined cron and renamed
2026-08-11 because the name kept implying a scheduler that does not exist).

**Execution and triage are split** (review acceptance criterion 1). This file
is the execution half: plain Python, exit-coded, no agent in the loop,
identical whoever triggers it. The Claude Code triage agent reads the
artifacts this produces and writes prose; it runs nothing and it never
authors an oracle (criterion 5).

Jobs, in the order a full invocation runs them:

1. **Truth-set staleness → rebuild.** Corpus text moved ⇒ every oracle keyed to
   it is stale. Rebuilding is deterministic and versioned; the digest diff is
   recorded for review rather than applied silently.
2. **Golden run** — smoke by default, `--full` adds core and the scripts.
3. **Comparator** against the last accepted baseline, which is a committed
   pointer, not "the newest file lying around". Differing scoring contracts
   still yield NOT COMPARABLE by design.
4. **Held-out batch** from `.qa-artifacts/heldout/` — executed here and reported
   as **aggregates only**, so case bodies never reach the teacher's context
   (GOLDEN-V2-DESIGN §7.2). This is Gate 4's "separately authorized evaluation
   service" in its near-term form.
5. **Calibration pressure** — reports when human labels have fallen below the
   ≥30 the runbook requires before a faithfulness number may be quoted.

Exit codes carry the verdict, because scripts and CI read exit codes, not
prose:

    0  everything ran and nothing regressed
    1  a regression, a failing gate, or a failed job
    2  NOT COMPARABLE (contract/digest/binding mismatch) — needs a human
    3  stale artifacts that could not be rebuilt

Run:
    uv run python -m experiments.qa                  # smoke
    uv run python -m experiments.qa --full --judge   # every slice + compare
    uv run python -m experiments.qa --accept <run>   # promote a baseline
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from experiments.cases import GOLDEN_DIR, load_suite
from experiments.truth import DIGEST_MANIFEST, read_manifest
from x1_advisor.agent.bundle import QA_ARTIFACTS_DIR

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "experiments" / "runs"
HELDOUT_DIR = QA_ARTIFACTS_DIR.parent / "heldout"
REPORTS_DIR = QA_ARTIFACTS_DIR.parent / "reports"
# the accepted baseline is a committed POINTER: "newest manifest in the
# directory" would silently re-baseline on every run, which is how a slow
# regression becomes the new normal
BASELINE_POINTER = GOLDEN_DIR / "baseline.json"

# A full run produces THREE manifests — smoke, core, scripts — and each is
# its own comparison. The first version compared only the single newest
# manifest, so two of the three slices (including every script failure) were
# never gated at all (second review, finding 1).
SLICES = {"smoke": "_v2_smoke", "core": "_v2_core", "scripts": "_scripts_v2"}


def slice_of(manifest_name: str) -> str:
    matches = [s for s, marker in SLICES.items() if marker in manifest_name]
    if len(matches) != 1:
        raise SystemExit(f"{manifest_name!r} does not name exactly one slice "
                         f"(markers: {SLICES})")
    return matches[0]


def _run(argv: list[str]) -> tuple[int, str]:
    """Run a job as its own process so its exit code is the real thing."""
    proc = subprocess.run([sys.executable, "-m", *argv], cwd=REPO_ROOT,
                          capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def latest_manifest(prefix: str) -> Path | None:
    matches = sorted(RUNS_DIR.glob(f"*{prefix}*.jsonl"),
                     key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None


def read_baseline() -> dict[str, Any]:
    if BASELINE_POINTER.exists():
        return json.loads(BASELINE_POINTER.read_text())
    return {}


def accept_baseline(manifest_names: list[str]) -> None:
    """Promote a run — one manifest per slice — to 'the bar'. Deliberately
    explicit and deliberately committed: accepting a baseline is a judgment
    about whether the numbers are good, which is a human's call (QA-RUNBOOK).

    Each manifest must exist, be COMPLETE, contain no identity drift, carry
    the current scoring contract, and name a distinct slice; the pointer
    records all of them so job_compare gates every slice, not whichever file
    is newest.

    Completeness is checked against the suite itself (2026-08-11): a runner
    that died mid-run leaves a partial manifest whose filename is
    indistinguishable from a complete one (it happened, 2026-08-04), and a
    partial accepted as the bar makes every future comparison silently blind
    to the missing cases. A pointer must be proof of a complete run.
    """
    suite = load_suite("v2")
    expected = {"smoke": sum(1 for c in suite.cases if c.tier == "smoke"),
                "core": sum(1 for c in suite.cases if c.tier == "core"),
                "scripts": len(suite.scripts)}
    manifests: dict[str, str] = {}
    for name in manifest_names:
        path = RUNS_DIR / name
        if not path.exists():
            raise SystemExit(f"no such manifest: {path}")
        rows = [json.loads(line) for line in path.read_text().splitlines()
                if line.strip()]
        summaries = [r for r in rows if r.get("record") == "summary"]
        result_rows = [r for r in rows if r.get("record") != "summary"]
        if any(rec.get("identity_drift") for rec in summaries):
            raise SystemExit(
                f"{name}: identity drift recorded (HEAD moved during the "
                "run) — a tainted run cannot become the bar")
        if not summaries:
            raise SystemExit(
                f"{name}: no summary row — the runner never finished this "
                "manifest; a partial run cannot become the bar (delete the "
                "partial and re-run)")
        s = slice_of(name)
        if s in manifests:
            raise SystemExit(f"two manifests name the {s!r} slice")
        if len(result_rows) != expected[s]:
            raise SystemExit(
                f"{name}: {len(result_rows)}/{expected[s]} {s} rows — an "
                "incomplete run cannot become the bar")
        contracts = {r.get("scoring_contract") for r in rows}
        if contracts != {suite.contract}:
            raise SystemExit(
                f"{name}: graded under {sorted(map(str, contracts))}, but the "
                f"current suite contract is {suite.contract!r} — re-run (or "
                "rejudge) on the current contract before accepting")
        manifests[s] = name
    BASELINE_POINTER.write_text(json.dumps({
        "manifests": dict(sorted(manifests.items())),
        "suite": suite.version,
        "scoring_contract": suite.contract,
        "suite_digest": suite.digest,
        "truth_digests": {k: v["digest"] for k, v in
                          read_manifest().get("truth_sets", {}).items()},
        "accepted": dt.date.today().isoformat(),
    }, indent=2) + "\n")


def job_truth(steps: list[dict]) -> int:
    code, out = _run(["experiments.truth", "--golden", "v2", "--check"])
    if code == 0:
        steps.append({"job": "truth", "status": "current", "detail": ""})
        return 0
    before = {k: v.get("digest") for k, v in
              read_manifest().get("truth_sets", {}).items()}
    rebuild_code, rebuild_out = _run(["experiments.truth", "--golden", "v2"])
    after = {k: v.get("digest") for k, v in
             read_manifest().get("truth_sets", {}).items()}
    # the diff is surfaced, never applied silently (§5.1): an oracle that moved
    # because the corpus moved is normal; an oracle that moved a lot is a
    # question for triage
    moved = sorted(k for k in after if before.get(k) != after[k])
    steps.append({"job": "truth", "status": "rebuilt" if rebuild_code == 0 else "failed",
                  "oracles_moved": moved, "detail": rebuild_out.strip()[-2000:]})
    return 0 if rebuild_code == 0 else 3


def job_golden(steps: list[dict], *, full: bool, judge: bool, seed: str) -> int:
    worst = 0
    tiers = ["smoke", "core"] if full else ["smoke"]
    for tier in tiers:
        argv = ["experiments.run_v2", "--tier", tier, "--seed", seed]
        if judge:
            argv.append("--judge")
        code, out = _run(argv)
        steps.append({"job": f"golden:{tier}", "status": "ok" if code == 0 else "failed",
                      "detail": out.strip()[-4000:]})
        worst = max(worst, min(code, 1))
    if full:
        argv = ["experiments.script_runner", "--golden", "v2", "--seed", seed]
        if judge:
            argv.append("--judge")
        code, out = _run(argv)
        # exit 1 here means a script failed as a sequence — a RESULT, and it
        # rolls into the harness exit code like any other failing tier. The
        # first version dropped it on the floor, so 0/4 scripts passing still
        # exited 0 (second review, finding 1).
        steps.append({"job": "scripts", "status": "ok" if code == 0 else "failing",
                      "detail": out.strip()[-4000:]})
        worst = max(worst, min(code, 1))
    return worst


def job_compare(steps: list[dict]) -> int:
    baseline = read_baseline()
    if not baseline:
        steps.append({"job": "compare", "status": "no-baseline",
                      "detail": "no accepted baseline yet — run with --accept "
                                "once a run is judged good"})
        return 0
    worst = 0
    for slice_name, baseline_name in sorted(
            (baseline.get("manifests") or {}).items()):
        before = RUNS_DIR / baseline_name
        if not before.exists():
            steps.append({"job": f"compare:{slice_name}",
                          "status": "baseline-missing", "detail": baseline_name})
            worst = 2
            continue
        newest = latest_manifest(SLICES[slice_name])
        if newest is None or newest.name == baseline_name:
            # nothing newer than the bar itself: either the golden job was
            # skipped this invocation or it failed to write — say so, don't self-compare
            steps.append({"job": f"compare:{slice_name}", "status": "no-new-run",
                          "detail": ""})
            continue
        code, out = _run(["experiments.compare", str(before), str(newest)])
        # exit 2 is the comparator refusing to gate across differing contracts
        # — that is the machinery working, and it needs a human, not a retry
        steps.append({"job": f"compare:{slice_name}",
                      "status": {0: "clean", 1: "regression",
                                 2: "not-comparable"}.get(code, "error"),
                      "detail": out.strip()[-4000:]})
        worst = 2 if 2 in (worst, code) else max(worst, code)
    return worst


def job_heldout(steps: list[dict], *, judge: bool, seed: str) -> int:
    """Execute David's held-out cases and report AGGREGATES ONLY (§7.2).

    The bodies live in owner-only storage and are never echoed into the step
    log, because this log is what the triage agent — and therefore the teacher
    session — reads.
    """
    suites = sorted(HELDOUT_DIR.glob("*.yaml")) if HELDOUT_DIR.exists() else []
    if not suites:
        steps.append({"job": "heldout", "status": "absent",
                      "detail": f"no held-out suite in {HELDOUT_DIR}"})
        return 0
    worst = 0
    for suite_path in suites:
        argv = ["experiments.run_v2", "--suite-path", str(suite_path),
                "--tier", "all", "--seed", seed]
        if judge:
            argv.append("--judge")
        code, out = _run(argv)
        summary = [line for line in out.splitlines()
                   if line.startswith(("cases:", "diagnostics", "truth-graded",
                                       "cost:", "scoring contract"))]
        steps.append({"job": f"heldout:{suite_path.stem}",
                      "status": "ok" if code == 0 else "failed",
                      # aggregates only: never the per-case lines, which name
                      # the questions
                      "detail": "\n".join(summary)})
        worst = max(worst, min(code, 1))
    return worst


def job_calibration(steps: list[dict]) -> int:
    """Report the judge's calibration from its CANONICAL set — the same
    `calibration_state()` every judged score carries. The first version
    counted a different file (`.qa-artifacts/calibration/pending.jsonl`), so
    the harness said 0/30 while judged rows correctly said human-calibrated
    (second review, H1 section): one source of truth, or two answers.
    """
    from x1_advisor.agent.judge import calibration_state

    state = calibration_state()
    short = max(0, state["min_human_labels"] - state["human_labels"])
    steps.append({"job": "calibration",
                  "status": "ok" if not short else "short",
                  "detail": f"{state['state']}: {state['human_labels']}/"
                            f"{state['min_human_labels']} human labels, "
                            f"{state['synthetic_labels']} synthetic "
                            f"(judge {state['judge_model']})"
                            + (f"; {short} more needed before any faithfulness "
                               "number is quotable (RUNBOOK §7)" if short else "")})
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true",
                    help="core tier + scripts as well as smoke")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--seed", default="v2-baseline",
                    help="binding seed; the baseline's seed is pinned so paired "
                         "runs resolve identical entities (§4)")
    ap.add_argument("--accept", default=None, metavar="MANIFESTS",
                    help="promote a run to the accepted baseline and exit — "
                         "comma-separated manifest names, one per slice "
                         "(smoke, core, scripts)")
    ap.add_argument("--skip", default="", help="comma-separated job names to skip")
    args = ap.parse_args()

    if args.accept:
        names = [n.strip() for n in args.accept.split(",") if n.strip()]
        accept_baseline(names)
        print(f"accepted baseline ({len(names)} slice(s)): "
              f"{', '.join(names)}\n  -> {BASELINE_POINTER}")
        return

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    steps: list[dict] = []
    worst = 0
    started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    for name, job in (("truth", lambda: job_truth(steps)),
                      ("golden", lambda: job_golden(steps, full=args.full,
                                                    judge=args.judge, seed=args.seed)),
                      ("compare", lambda: job_compare(steps)),
                      ("heldout", lambda: job_heldout(steps, judge=args.judge,
                                                      seed=args.seed)),
                      ("calibration", lambda: job_calibration(steps))):
        if name in skip:
            continue
        code = job()
        # 2 (not comparable) outranks 1 (regression): it means nobody can say
        # whether anything regressed, which is worse than knowing something did
        worst = 2 if 2 in (worst, code) else max(worst, code)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORTS_DIR / f"{dt.date.today()}_qa.json"
    report.write_text(json.dumps({
        "started": started, "exit_code": worst, "full": args.full,
        "judge": args.judge, "seed": args.seed, "steps": steps}, indent=2))

    print(f"== qa {started} ==")
    for step in steps:
        print(f"  {step['status']:<15} {step['job']}")
        if step.get("oracles_moved"):
            print(f"                  oracles moved: {step['oracles_moved']}")
    print(f"\nreport: {report}")
    print(f"exit {worst}")
    sys.exit(worst)


if __name__ == "__main__":
    main()
