"""Golden v2 case runner (Gate 4; GOLDEN-V2-DESIGN §4, §5, §10).

`experiments/run.py` grades golden v1: retrieval matchers over hits. v2 grades
answers, so it needs its own execution path — same agent, same bundles, same
funnel, same judge, but with the three things v1 cannot express:

* **run identity on every row** (review criterion 3): the scoring contract, the
  compiled suite digest, the resolved entity bindings, and the digest of the
  truth set each case was graded against. A comparison whose bindings or
  oracles moved is not a comparison, and the recorded row is what lets the
  comparator say so.
* **truth-set grading** (§5.1): entity-level recall, precision and — the one
  that matters for the measured failure mode — an **overclaim** count, every
  entity asserted as matching that the corpus says does not.
* **honesty vs capability** (§4): a case whose tool does not exist yet is
  graded on what it discloses, not on what it returns.

Selected-entity cases keep their wording verbatim and receive the bound entity
as an explicit preamble line — the v2.0 stand-in for page context (bank §1.1
note). The preamble is recorded in the manifest, because it is part of the
input the model saw.

Run:
    uv run python -m experiments.run_v2 --tier smoke
    uv run python -m experiments.run_v2 --tier core --judge --limit 5
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any

from experiments import checkers
from experiments.cases import (
    Case,
    CaseValidationError,
    Suite,
    load_suite,
    render_question,
    resolve_bindings,
)
from experiments.funnel import classify
from experiments.manifest import code_fingerprint, git_sha, open_new_manifest
from experiments.script_runner import entity_vocabulary, turn_record
from experiments.truth import TruthSetStale, load_truth_set
from x1_advisor.agent.bundle import QA_ARTIFACTS_DIR, export_bundle, manifest_record
from x1_advisor.agent.judge import calibration_state, judge_bundle
from x1_advisor.cost import JsonlSink, Tracker
from x1_advisor.db import connect

# The v2.0 stand-in for selected-page context (Gate 3B replaces it with a real
# context snapshot). It is a preamble, not a rewrite: the user's sentence is
# preserved exactly, which is the whole point of keeping bank wording verbatim.
SELECTED_PREAMBLE = "[Selected startup: {name}]\n{question}"


def prepare(case: Case, *, seed: str, fixtures: dict) -> dict[str, Any]:
    """Resolve bindings and produce the exact input the model will see."""
    bound = resolve_bindings(case, seed=seed, fixtures=fixtures) if case.bindings else {}
    question = render_question(case.question, bound)
    prompt = question
    if case.binding_mode == "selected_entity" and bound:
        name = next(iter(bound.values()))["name"]
        prompt = SELECTED_PREAMBLE.format(name=name, question=question)
    return {"question": question, "prompt": prompt,
            "bindings": {slot: entity.get("name") for slot, entity in bound.items()}}


def _countable_truth(grade: dict | None) -> dict | None:
    return {k: v for k, v in grade.items() if k != "overclaimed"} if grade else None


def grade_against_truth(answer: str, truth: dict, vocabulary: set[str]
                        ) -> dict[str, Any]:
    """Entity-level recall / precision / overclaim against the computed oracle.

    Only names the corpus actually knows are considered, so ordinary prose
    capitalization cannot inflate either number. An overclaim — an entity
    asserted as matching that the truth set says does not — is reported
    separately because it is the measured dominant failure (the model
    overstates its evidence), and averaging it into precision hides it.
    """
    matched = {e["key"].lower() for e in truth["entities"]
               if e["status"] == "matched"}
    scanned = {e["key"].lower() for e in truth["entities"]
               if e["status"] != "not_indexed"}
    named = {m.lower() for m in checkers.entity_mentions(
        checkers.strip_citations(answer))} & vocabulary

    hits = named & matched
    # asserted, known to the scan, and NOT in the true set
    overclaimed = sorted((named & scanned) - matched)
    return {
        "truth_matched": len(matched),
        "named": len(named),
        "recall": (len(hits) / len(matched)) if matched else None,
        "precision": (len(hits) / len(named)) if named else None,
        "overclaimed": overclaimed,
        "overclaim_count": len(overclaimed),
        # an empty oracle is a real answer: the honest response names nobody
        "empty_oracle_respected": (not named) if not matched else None,
    }


def run_case(conn, case: Case, *, suite: Suite, seed: str, vocabulary: set[str],
             judge: bool = False, tracker: Tracker | None = None,
             run_id: str | None = None) -> dict[str, Any]:
    from x1_advisor.agent.advisor import run_turn

    prepared = prepare(case, seed=seed, fixtures=suite.fixtures)
    result = run_turn(conn, prepared["prompt"], acl="admin")
    bundle = result["bundle"]
    answer = bundle.get("validation", {}).get("answer") or ""
    record = turn_record(conn, 1, prepared["question"], result)

    assertions = checkers.run_case_checks(answer, evidence=record.evidence,
                                          deterministic=case.grade.deterministic)
    diagnostics = checkers.run_global_checkers(answer, evidence=record.evidence,
                                               question=prepared["question"])

    truth_grade, truth_digest = None, None
    if case.truth_set:
        # a stale oracle raises rather than grading quietly (§5.1); the case is
        # recorded as ungraded so the run does not silently shrink
        truth = load_truth_set(conn, case)
        truth_digest = truth["digest"]
        truth_grade = grade_against_truth(answer, truth, vocabulary)

    verdict = None
    if judge and case.grade.judged:
        verdict = judge_bundle(conn, bundle, tracker=tracker,
                               calibration=calibration_state())
        bundle["scores"].update(verdict["scores"])
        bundle["judge"] = verdict

    labels = classify(conn, bundle, {"id": case.id, "category": case.cls,
                                     "acceptable_routes": list(case.acceptable_routes)})
    # Full grading detail — including WHICH entities were overclaimed and which
    # answer mentions were ungrounded — is corpus-derived content and belongs in
    # owner-only storage with the bundle. The committed manifest gets counts
    # (QA-LOOP §4.1 body-free rule; the same reason truth sets are untracked).
    bundle["truth_grade"] = truth_grade
    bundle["checks"] = {"assertions": [a.to_dict() for a in assertions],
                        "diagnostics": [d.to_dict() for d in diagnostics]}
    bundle_path = export_bundle(bundle, name=case.id, subdir=run_id)

    return {
        "case_id": case.id, "class": case.cls, "tier": case.tier,
        "grading_mode": case.grading_mode, "blocked_on": case.blocked_on,
        "fallback_contract": case.fallback_contract,
        # run identity (criterion 3) — bindings and oracle digest, per row
        "bindings": prepared["bindings"], "truth_digest": truth_digest,
        "binding_mode": case.binding_mode,
        "preamble": prepared["prompt"] != prepared["question"],
        "assertions": [checkers.countable(a) for a in assertions],
        "diagnostics": [checkers.countable(d) for d in diagnostics],
        "truth_grade": _countable_truth(truth_grade),
        "searched_documents": record.searched_documents,
        "searched_rows": record.searched_rows,
        "labels": labels["labels"], "notes": labels["notes"],
        "routes": labels["routes"],
        "judge": verdict,
        "bundle": bundle_path.name if bundle_path else None,
        "cost_usd": result["cost_usd"], "latency_ms": result["latency_ms"],
        "citation_stats": result["citation_stats"],
        # the declared contract decides the verdict; the §5.2 globals are
        # diagnostics until a false-positive audit promotes them (criterion 4)
        "pass": all(a.passed for a in assertions),
        **manifest_record(bundle),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", default="v2")
    ap.add_argument("--tier", default="smoke",
                    choices=["smoke", "core", "extended", "all"])
    ap.add_argument("--case", default=None, help="run one case by id")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--seed", default="v2-baseline",
                    help="binding seed; paired runs MUST use the same one (§4)")
    ap.add_argument("--judge", action="store_true")
    args = ap.parse_args()

    try:
        suite = load_suite(args.golden)
    except CaseValidationError as exc:
        print(f"suite does not compile ({len(exc.errors)} error(s)):", file=sys.stderr)
        for err in exc.errors:
            print(f"  {err}", file=sys.stderr)
        sys.exit(2)

    cases = [c for c in suite.cases
             if (args.case is None or c.id == args.case)
             and (args.tier == "all" or c.tier == args.tier)]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        sys.exit("no cases selected")

    run_id, manifest_path, manifest_file = open_new_manifest(
        f"{dt.date.today()}_v2_{args.tier}")
    tracker = Tracker(run_id=f"{run_id}:judge",
                      sink=JsonlSink(os.environ.get("ADVISOR_COST_LEDGER",
                                                    "cost_ledger.jsonl")))
    rows, stale = [], []
    with connect() as conn, manifest_file as manifest:
        vocabulary = entity_vocabulary(conn)
        for case in cases:
            try:
                row = run_case(conn, case, suite=suite, seed=args.seed,
                               vocabulary=vocabulary, judge=args.judge,
                               tracker=tracker, run_id=run_id)
            except TruthSetStale as exc:
                # loud, and the case is not counted: grading against an oracle
                # built from a different corpus would produce confident numbers
                # about a corpus that no longer exists
                stale.append(str(exc))
                print(f"  STALE  {case.id}: {exc}", file=sys.stderr)
                continue
            rows.append(row)
            manifest.write(json.dumps({
                "run_id": run_id, "experiment": "golden-v2",
                "git_sha": git_sha(), "code_fingerprint": code_fingerprint(),
                **suite.identity(), **row}, default=str) + "\n")
            failed = [a["check"] for a in row["assertions"] if not a["passed"]]
            truth = row["truth_grade"]
            print(f"  {'PASS' if row['pass'] else 'FAIL'} {case.id} "
                  f"{case.cls:<22} {row['grading_mode']:<11} "
                  f"cite {row['citation_stats']['resolved']}/"
                  f"{row['citation_stats']['emitted']} "
                  f"${row['cost_usd']:.4f}"
                  + (f"  recall={truth['recall']:.2f}"
                     if truth and truth["recall"] is not None else "")
                  + (f" overclaim={truth['overclaim_count']}" if truth else "")
                  + (f"  checks: {','.join(failed)}" if failed else ""))

    passed = sum(1 for r in rows if r["pass"])
    print(f"\n== {run_id} ==")
    print(f"cases: {passed}/{len(rows)} passing their declared contract"
          + (f"  ({len(stale)} skipped: stale truth set)" if stale else ""))
    print(f"scoring contract: {suite.contract}")
    print(f"suite digest:     {suite.digest[:16]}…   seed: {args.seed}")
    diag_fail: dict[str, int] = {}
    for row in rows:
        for diagnostic in row["diagnostics"]:
            if not diagnostic["passed"]:
                diag_fail[diagnostic["check"]] = diag_fail.get(diagnostic["check"], 0) + 1
    print("diagnostics (non-gating, criterion 4): "
          + (", ".join(f"{k} {v}" for k, v in sorted(diag_fail.items())) or "all clean"))
    overclaims = sum((r["truth_grade"] or {}).get("overclaim_count", 0) for r in rows)
    graded = [r for r in rows if r["truth_grade"]]
    if graded:
        recalls = [r["truth_grade"]["recall"] for r in graded
                   if r["truth_grade"]["recall"] is not None]
        print(f"truth-graded cases: {len(graded)}"
              + (f", mean entity recall {sum(recalls)/len(recalls):.3f}" if recalls else "")
              + f", overclaimed entities {overclaims}")
    print(f"cost: ${sum(r['cost_usd'] for r in rows):.4f}"
          + (f" + judge ${tracker.run_total:.4f}" if args.judge else ""))
    print(f"manifest: {manifest_path}")
    print(f"bundles:  {QA_ARTIFACTS_DIR / run_id}")
    sys.exit(1 if stale else 0)


if __name__ == "__main__":
    main()
