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
from pathlib import Path

from experiments.cases import (
    Case,
    compile_suite,
    CaseValidationError,
    Suite,
    load_suite,
    render_question,
    resolve_bindings,
)
from experiments.adjudicate import (adjudicate_asserted_names,
                                    adjudicate_citation_coverage,
                                    adjudicate_faithfulness,
                                    escalate_assertions, escalate_behaviors)
from experiments.behavior import judge_behaviors
from experiments.funnel import classify
from experiments.manifest import code_fingerprint, git_sha, open_new_manifest
from experiments.script_runner import entity_vocabulary, turn_record
from experiments.truth import TruthSetStale, load_truth_set
from x1_advisor.agent.bundle import (
    QA_ARTIFACTS_DIR,
    export_bundle,
    judge_manifest_projection,
    manifest_record,
)
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


_TRUTH_NAME_KEYS = ("overclaimed", "negated", "excluded", "scope_listed",
                    "missed")
_ADJ_NAME_KEYS = ("asserted", "disclosed", "credited", "uncredited")


def _countable_truth(grade: dict | None) -> dict | None:
    """Manifest projection of a truth grade: counts, never entity names."""
    if not grade:
        return None
    out = {k: v for k, v in grade.items()
           if k not in _TRUTH_NAME_KEYS and k != "adjudication"}
    for key in _TRUTH_NAME_KEYS:
        out[f"{key}_count"] = len(grade.get(key) or [])
    adj = grade.get("adjudication")
    if adj:
        proj = {k: v for k, v in adj.items()
                if k not in _ADJ_NAME_KEYS and k != "per_name"}
        for key in _ADJ_NAME_KEYS:
            proj[f"{key}_count"] = len(adj.get(key) or [])
        out["adjudication"] = proj
    return out


def grade_against_truth(answer: str, truth: dict, vocabulary: set[str]
                        ) -> dict[str, Any]:
    """Entity-level recall / precision / overclaim against the computed oracle.

    Membership is decided by SEARCHING the answer for the oracle's own keys
    (plus the corpus vocabulary), never by parsing the answer into candidate
    entities — parsing misses every name that does not look like a capitalized
    run, which zeroed people/CV recall in the first implementation (second
    review, finding 3). Mentions are split by polarity first: "X does not
    mention it" is the honest disclosure, not an overclaim.

    An overclaim — an entity positively asserted that the truth set says does
    not match — is reported separately because it is the measured dominant
    failure (the model overstates its evidence), and averaging it into
    precision hides it.

    Recall counts the CENSUS delivered to the reader: positive assertions plus
    names filed in a labeled exclusion group (rule 5 compliance). Overclaim
    liability attaches only to positive assertions — an entity the agent named
    and explicitly filed as irrelevant was disclosed, not overstated. Names in
    scope enumerations ("the scan covered: …") earn neither.
    """
    matched = {e["key"].lower() for e in truth["entities"]
               if e["status"] == "matched"}
    scanned = {e["key"].lower() for e in truth["entities"]
               if e["status"] != "not_indexed"}
    known = vocabulary | {e["key"] for e in truth["entities"]}
    buckets = checkers.asserted_names(
        checkers.strip_citations(answer), known)

    census = buckets.positive | buckets.excluded
    hits = census & matched
    # positively asserted, known to the scan, and NOT in the true set
    overclaimed = sorted((buckets.positive & scanned) - matched)
    return {
        "truth_matched": len(matched),
        "named": len(census),
        "hit_count": len(hits),
        "missed": sorted(matched - hits),
        "recall": (len(hits) / len(matched)) if matched else None,
        # precision stays assertion-only: of what the agent CLAIMED matched,
        # how much did — excluded names claim nothing
        "precision": (len(buckets.positive & matched) / len(buckets.positive))
                     if buckets.positive else None,
        "overclaimed": overclaimed,
        "overclaim_count": len(overclaimed),
        "negated": sorted(buckets.negated),
        "excluded": sorted(buckets.excluded),
        "scope_listed": sorted(buckets.scope),
        # an empty oracle is a real answer: the honest response positively
        # names nobody in scope (naming entities to REPORT their absence is
        # the contract being met, not broken)
        "empty_oracle_respected": (not (buckets.positive & scanned))
                                  if not matched else None,
    }


def truth_unit_passed(grade: dict[str, Any], grading_mode: str) -> bool:
    """Did the truth-graded unit meet its declared contract?

    Honesty mode (tool not ready — §4): the bar is not overstating evidence.
    No positively-asserted entity the scan refutes, and a verified-empty
    oracle answered by naming nobody. Recall does NOT gate here — demanding
    exhaustive enumeration from top-k retrieval is the capability bar, and
    grading it early is how every enumeration case reads as failing forever.

    Capability mode (scan_text shipped): return the right set — full recall,
    still zero overclaims. The grading-mode flip changes the suite contract
    string, so the comparator refuses to gate across it (§4).

    Escalation gate (s3): when the formula flagged failures and the judge
    adjudicated them (grade["adjudication"], experiments/adjudicate.py), the
    ADJUDICATED overclaim/recall/empty-oracle values gate; the formula's own
    values stay recorded beside them as telemetry. The oracle is never
    adjudicated — only the parser's reading of the prose.
    """
    adj = grade.get("adjudication") or {}
    oc = adj.get("overclaim_count", grade["overclaim_count"])
    eor = adj.get("empty_oracle_respected", grade["empty_oracle_respected"])
    recall = adj.get("recall", grade["recall"])
    honest = oc == 0 and eor is not False
    if grading_mode == "honesty":
        return honest
    return honest and (recall is None or recall == 1.0)


def graded_units(case: Case, assertions: list, truth_grade: dict | None,
                 verdict: dict | None, behavior_verdicts: list[dict]
                 ) -> dict[str, bool | None]:
    """Every unit the case's grade block declares, each True/False/None.

    "Passing" a case while a declared dimension went unmeasured is the
    vacuous-pass bug the second review caught (21 rows `pass: true` carrying
    judge failure labels) — so a unit the run could not grade is None, and
    `checkers.compose_pass` makes the whole case ungraded rather than passing.
    """
    units = checkers.unit_verdicts(assertions, case.grade.judged,
                                   case.grade.behavior, verdict,
                                   behavior_verdicts)
    if case.truth_set:
        units["truth_set"] = (truth_unit_passed(truth_grade, case.grading_mode)
                              if truth_grade else None)
    return units


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
                                          deterministic=case.grade.deterministic,
                                          citation_stats=result["citation_stats"])
    diagnostics = checkers.run_global_checkers(answer, evidence=record.evidence,
                                               question=prepared["question"])

    truth_grade, truth_digest = None, None
    if case.truth_set:
        # a stale oracle raises rather than grading quietly (§5.1); the case is
        # recorded as ungraded so the run does not silently shrink
        truth = load_truth_set(conn, case)
        truth_digest = truth["digest"]
        truth_grade = grade_against_truth(answer, truth, vocabulary)

    verdict, behavior_verdicts = None, []
    if judge:
        if case.grade.judged:
            verdict = judge_bundle(conn, bundle, tracker=tracker,
                                   calibration=calibration_state())
            bundle["scores"].update(verdict["scores"])
            bundle["judge"] = verdict
        if case.grade.behavior:
            behavior_verdicts = judge_behaviors(prepared["question"], answer,
                                                case.grade.behavior,
                                                tracker=tracker)
        # escalation gates (s3/s4): formula-flagged failures go to the judge;
        # clean passes never escalate, and formula output stays as telemetry
        if verdict and "citation_coverage" in case.grade.judged:
            adj = adjudicate_citation_coverage(bundle, verdict, tracker=tracker)
            if adj:
                verdict.setdefault("adjudications", {})["citation_coverage"] = adj
        if verdict and "faithfulness" in case.grade.judged:
            adj = adjudicate_faithfulness(bundle, verdict, tracker=tracker)
            if adj:
                verdict.setdefault("adjudications", {})["faithfulness"] = adj
        if truth_grade and (truth_grade["overclaim_count"] > 0
                            or (case.grading_mode == "capability"
                                and truth_grade["missed"])):
            adj = adjudicate_asserted_names(prepared["question"], answer,
                                            truth_grade, tracker=tracker)
            if adj:
                truth_grade["adjudication"] = adj
        # s5: formula-failed gated assertions (quotes_verbatim,
        # coverage_statement) escalate too — mutated in place, formula
        # verdict preserved in detail["formula_passed"]
        escalate_assertions(assertions, question=prepared["question"],
                            answer=answer, evidence=record.evidence,
                            tracker=tracker)
        # s6: unmet behavior obligations escalate — the targeted behavior
        # judge is the detector, its verdict survives as formula_met
        escalate_behaviors(behavior_verdicts, question=prepared["question"],
                           answer=answer, tracker=tracker)

    labels = classify(conn, bundle, {"id": case.id, "category": case.cls,
                                     "acceptable_routes": list(case.acceptable_routes)})
    # Full grading detail — including WHICH entities were overclaimed, which
    # mentions were ungrounded, and the behavior judge's reasons (they quote
    # the answer) — is corpus-derived content and belongs in owner-only storage
    # with the bundle. The committed manifest gets counts and verdict booleans
    # (QA-LOOP §4.1 body-free rule; the same reason truth sets are untracked).
    bundle["truth_grade"] = truth_grade
    bundle["behavior_verdicts"] = behavior_verdicts
    bundle["checks"] = {"assertions": [a.to_dict() for a in assertions],
                        "diagnostics": [d.to_dict() for d in diagnostics]}
    bundle_path = export_bundle(bundle, name=case.id, subdir=run_id)

    units = graded_units(case, assertions, truth_grade, verdict,
                         behavior_verdicts)
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
        "behavior": [{"obligation": v["obligation"], "met": v["met"],
                      **({"formula_met": v["formula_met"],
                          "adjudication": v["adjudication"]}
                         if "adjudication" in v else {})}
                     for v in behavior_verdicts],
        "searched_documents": record.searched_documents,
        "searched_rows": record.searched_rows,
        "labels": labels["labels"], "notes": labels["notes"],
        "routes": labels["routes"],
        # explicitly the body-free projection — never the raw verdict, which
        # carries claim text and reasoning (second review, finding 5)
        "judge": judge_manifest_projection(verdict),
        "bundle": bundle_path.name if bundle_path else None,
        "cost_usd": result["cost_usd"], "latency_ms": result["latency_ms"],
        "citation_stats": result["citation_stats"],
        # the verdict covers EVERY declared graded unit (second review,
        # finding 2): deterministic assertions, the truth-set unit under the
        # case's grading mode, declared judged dimensions via judge labels,
        # and behavior obligations via the targeted behavior judge. None =
        # a declared unit could not be graded on this run (judge off) — the
        # case is ungraded, not passing. The §5.2 globals remain diagnostics
        # until a false-positive audit promotes them (criterion 4).
        "graded_units": units,
        "pass": checkers.compose_pass(units),
        **manifest_record(bundle),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", default="v2")
    ap.add_argument("--suite-path", default=None,
                    help="compile a suite from an explicit path (held-out "
                         "batches live outside experiments/golden/)")
    ap.add_argument("--tier", default="smoke",
                    choices=["smoke", "core", "extended", "all"])
    ap.add_argument("--case", default=None, help="run one case by id")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--seed", default="v2-baseline",
                    help="binding seed; paired runs MUST use the same one (§4)")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--workers", type=int,
                    default=int(os.environ.get("ADVISOR_RUN_WORKERS", "4")),
                    help="concurrent cases (each on its own DB connection); "
                         "judge subprocess pressure is bounded globally by "
                         "ADVISOR_JUDGE_CONCURRENCY, so this parallelizes "
                         "wall-clock, not seat load")
    args = ap.parse_args()

    try:
        suite = (compile_suite(Path(args.suite_path)) if args.suite_path
                 else load_suite(args.golden))
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
    # Run identity is captured ONCE, before the first case. The first baseline
    # run recorded git_sha() per row while commits landed underneath it — 49
    # rows spanning three commits is not one run identity (second review,
    # finding 4). The start sha is what the loaded code actually was; a HEAD
    # that moved by the end taints the run and is stamped on the summary.
    started_sha, started_fp = git_sha(), code_fingerprint()

    rows, stale = [], []
    with connect() as conn:
        vocabulary = entity_vocabulary(conn)

    # Cases run CONCURRENTLY (2026-08-07): each worker gets its own DB
    # connection; the shared judge-subprocess bound lives in judge_cc.
    # Manifest rows are written from this thread as cases COMPLETE — row
    # order is completion order, which carries no meaning (rows are keyed
    # by case_id and identity is stamped per row).
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def one(case):
        with connect() as case_conn:
            return run_case(case_conn, case, suite=suite, seed=args.seed,
                            vocabulary=vocabulary, judge=args.judge,
                            tracker=tracker, run_id=run_id)

    with manifest_file as manifest, \
            ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(one, case): case for case in cases}
        for fut in as_completed(futures):
            case = futures[fut]
            try:
                row = fut.result()
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
                "git_sha": started_sha, "code_fingerprint": started_fp,
                **suite.identity(), **row}, default=str) + "\n")
            failed = [name for name, ok in row["graded_units"].items()
                      if ok is False]
            ungraded = [name for name, ok in row["graded_units"].items()
                        if ok is None]
            truth = row["truth_grade"]
            mark = {True: "PASS", False: "FAIL", None: "----"}[row["pass"]]
            print(f"  {mark} {case.id} "
                  f"{case.cls:<22} {row['grading_mode']:<11} "
                  f"cite {row['citation_stats']['resolved']}/"
                  f"{row['citation_stats']['emitted']} "
                  f"${row['cost_usd']:.4f}"
                  + (f"  recall={truth['recall']:.2f}"
                     if truth and truth["recall"] is not None else "")
                  + (f" overclaim={truth['overclaim_count']}" if truth else "")
                  + (f"  failed: {','.join(failed)}" if failed else "")
                  + (f"  ungraded: {','.join(ungraded)}" if ungraded else ""))

        ended_sha = git_sha()
        drift = ended_sha != started_sha
        manifest.write(json.dumps({
            "record": "summary", "run_id": run_id,
            "git_sha": started_sha, "git_sha_at_end": ended_sha,
            "identity_drift": drift, **suite.identity(), "seed": args.seed,
        }) + "\n")

    passed = sum(1 for r in rows if r["pass"] is True)
    ungraded_rows = sum(1 for r in rows if r["pass"] is None)
    print(f"\n== {run_id} ==")
    if drift:
        print(f"!! IDENTITY DRIFT: HEAD moved {started_sha} -> {ended_sha} "
              "during the run — do not commit while a run is live; this "
              "manifest is tainted for baseline use")
    print(f"cases: {passed}/{len(rows)} passing their declared contract"
          + (f"  ({ungraded_rows} ungraded: declared judged/behavior units "
             "need --judge)" if ungraded_rows else "")
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
