"""Multi-turn script runner (Gate 4 build step 4; GOLDEN-V2-DESIGN §8).

`experiments/run.py` grades single turns. The scripts are ordered sequences —
SCR Script A is *show all → filter score>77 → which mention regulatory risk →
exact quotes → best evidence* — and what they test only exists across turns:
coreference ("which of these"), working-set carryover, and evidence fidelity
applied to a set someone established two turns ago.

So a script runs as ONE conversation through the real history/condense path
(`advisor.run_turn(history=…)`), and is graded twice over:

* **per turn** — the same bundles, funnel labels, deterministic checkers and
  judge as a single case. Recorded for the funnel; not the gate.
* **across turns** — the assertions a single turn cannot express. The gate unit
  is the script: it passes only as a sequence (§8).

The coverage-challenge assertion is the one that matters most and the one a
naive implementation gets wrong: "did you search all 20?" is graded against
**what the prior turn's bundle actually searched**, never against what the
answer claims it searched. Grading a coverage claim by reading the coverage
claim measures nothing.

Run:
    uv run python -m experiments.script_runner --golden v2 --script v2s003
    uv run python -m experiments.script_runner --golden v2 --judge
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from experiments import checkers
from experiments.behavior import judge_behaviors
from experiments.cases import (
    CaseValidationError,
    Script,
    load_suite,
    render_question,
    resolve_bindings,
)
from experiments.funnel import classify
from experiments.manifest import code_fingerprint, git_sha, open_new_manifest
from x1_advisor.agent.bundle import (
    QA_ARTIFACTS_DIR,
    export_bundle,
    judge_manifest_projection,
)
from x1_advisor.agent.judge import calibration_state, evidence_texts, judge_bundle
from x1_advisor.cost import JsonlSink, Tracker
from x1_advisor.db import connect


@dataclass
class TurnRecord:
    """Everything the cross-turn assertions are allowed to look at.

    Deliberately a plain data object with no bundle inside: the assertions are
    pure functions of these fields, which is what makes them testable without a
    database or a model.
    """
    n: int
    question: str
    answer: str
    evidence: list[str] = field(default_factory=list)
    # what the turn actually put in front of the model — the denominator a
    # coverage claim is checked against
    searched_documents: int = 0
    searched_rows: int = 0          # structured_query rows
    searched_chunks: int = 0

    @property
    def entities(self) -> list[str]:
        return checkers.entity_mentions(checkers.strip_citations(self.answer))

    def named(self, vocabulary: set[str] | None) -> set[str]:
        """Entity mentions restricted to a KNOWN entity vocabulary.

        Without this the carryover assertions read every capitalized token as
        an entity: the first live run flagged `Australia`, `Q3` and `Slovenian`
        as startups the model had invented. A cross-turn set assertion is about
        the entities of the corpus, so it is answered against the corpus's own
        names; the unrestricted form stays available for the entity-grounding
        diagnostic, where over-collection is cheap.
        """
        mentions = {e.lower() for e in self.entities}
        return {m for m in mentions if m in vocabulary} if vocabulary else mentions


def _diag(check: str, passed: bool, **detail: Any) -> checkers.Diagnostic:
    return checkers.Diagnostic(check=check, passed=passed, detail=detail)


def assert_set_carryover(records: dict[int, TurnRecord], from_turn: int,
                         to_turn: int, vocabulary: set[str] | None = None
                         ) -> checkers.Diagnostic:
    """The later turn must operate on the set the earlier turn established."""
    earlier = records[from_turn].named(vocabulary)
    shared = earlier & records[to_turn].named(vocabulary)
    return _diag("set_carryover", bool(shared), from_turn=from_turn,
                 to_turn=to_turn, established=len(earlier), carried=len(shared))


def assert_no_new_entities(records: dict[int, TurnRecord], from_turn: int,
                           to_turn: int, vocabulary: set[str] | None = None
                           ) -> checkers.Diagnostic:
    """The later turn introduces nothing that was not in the established set.

    Entities named in the later turn's own question are allowed — the user is
    permitted to widen the subject; the model is not.
    """
    allowed = records[from_turn].named(vocabulary)
    asked = {e.lower() for e in checkers.entity_mentions(records[to_turn].question)}
    allowed |= ({a for a in asked if a in vocabulary} if vocabulary else asked)
    intruders = sorted(records[to_turn].named(vocabulary) - allowed)
    return _diag("no_new_entities", not intruders, from_turn=from_turn,
                 to_turn=to_turn, intruders=intruders)


def assert_quotes_from_turn(records: dict[int, TurnRecord], from_turn: int,
                            to_turn: int, vocabulary: set[str] | None = None
                            ) -> checkers.Diagnostic:
    """Quotes in the later turn must be verbatim from evidence seen since the
    set was established.

    Simplification worth naming: the evidence union spans turns from..to rather
    than the establishing turn alone, because a legitimate follow-up fetches
    full sources (`get_source`) for entities it already had. The assertion that
    the quotes belong to the right ENTITIES is carried by no_new_entities.
    """
    evidence = [text for n in range(from_turn, to_turn + 1)
                for text in records[n].evidence]
    # declaring this assertion means the turn was ASKED for quotes — an answer
    # containing none has not met the obligation (vacuous-pass fix, second
    # review finding 2)
    diagnostic = checkers.check_quotes_verbatim(records[to_turn].answer,
                                                evidence, require_quotes=True)
    return _diag("quotes_from_turn", diagnostic.passed, from_turn=from_turn,
                 to_turn=to_turn, **diagnostic.detail)


def assert_coverage_claim_grounded(records: dict[int, TurnRecord], turn: int,
                                   vocabulary: set[str] | None = None
                                   ) -> checkers.Diagnostic:
    """A coverage claim is graded against the BUNDLE of the turn that did the
    searching, not against the claim itself (§8).

    "Did you search all 20?" answered "yes, all 20" when the prior turn put 8
    documents in front of the model is the failure this exists to catch. Any
    number the answer offers as a coverage count is compared with what was
    actually searched across the conversation so far; a claim larger than the
    evidence is not grounded.
    """
    # documents shown OR structured rows returned — a listing answered from the
    # registry searched rows, not documents, and both are honest denominators
    searched = max((max(records[n].searched_documents, records[n].searched_rows)
                    for n in records if n <= turn), default=0)
    answer = checkers.strip_citations(records[turn].answer)
    claimed = [int(float(n)) for n in checkers.numerals(answer)
               if n.replace(".", "").isdigit()]
    overclaims = sorted({n for n in claimed if n > searched})
    return _diag("coverage_claim_grounded", not overclaims, turn=turn,
                 searched_documents=searched, overclaimed=overclaims)


CROSS_TURN_IMPL = {
    "set_carryover": assert_set_carryover,
    "no_new_entities": assert_no_new_entities,
    "quotes_from_turn": assert_quotes_from_turn,
    "coverage_claim_grounded": assert_coverage_claim_grounded,
}


def evaluate_cross_turn(script: Script, records: dict[int, TurnRecord],
                        vocabulary: set[str] | None = None
                        ) -> list[checkers.Diagnostic]:
    """Pure function over turn records — no bundle, no database, no model."""
    out = []
    for assertion in script.cross_turn:
        kind = dict(assertion)
        impl = CROSS_TURN_IMPL[kind.pop("type")]
        out.append(impl(records, vocabulary=vocabulary, **kind))
    return out


def entity_vocabulary(conn) -> set[str]:
    """Every entity name the corpus knows, lowercased.

    The set assertions are about corpus entities, so they are answered against
    the corpus's own names rather than against whatever the answer capitalized.
    """
    rows = conn.execute(
        """SELECT DISTINCT lower(name) AS name FROM (
               SELECT ch.metadata->>'company_name' AS name
               FROM advisor.doc_chunks ch
               WHERE ch.metadata->>'company_name' IS NOT NULL
               UNION
               SELECT split_part(d.title, ' — ', 1) FROM advisor.documents d
               WHERE d.superseded_by IS NULL AND d.entity_type IN
                     ('cv', 'investor', 'organization')
               UNION
               SELECT s.name FROM startup_companies s
           ) t WHERE name IS NOT NULL AND length(name) > 1""").fetchall()
    return {r["name"] for r in rows}


def turn_record(conn, n: int, question: str, result: dict) -> TurnRecord:
    """Project a live turn into the plain record the assertions read.

    The searched counts come from the bundle's own retrieval explain — the
    chunks actually RETURNED to the model, mapped to their documents through
    the fused candidate list — plus the row count of any structured query.
    Anything derived from the answer text would defeat the purpose: these are
    the numbers a coverage claim is checked against.
    """
    from experiments.funnel import tool_results

    bundle = result["bundle"]
    refs = evidence_texts(conn, bundle)
    explains = bundle.get("retrieval_explain") or []
    chunk_to_doc = {f["chunk_id"]: f.get("document_id")
                    for e in explains for f in e.get("fused") or []}
    returned = [cid for e in explains for cid in e.get("returned") or []]
    documents = {chunk_to_doc.get(cid) for cid in returned} - {None}
    rows = sum((call["payload"] or {}).get("row_count") or 0
               for call in tool_results(bundle)
               if call["tool"] == "structured_query")
    return TurnRecord(
        n=n, question=question,
        answer=bundle.get("validation", {}).get("answer") or "",
        evidence=[str(ref.get("snapshot") or ref.get("text") or "")
                  for ref in refs.values() if isinstance(ref, dict)],
        searched_documents=len(documents),
        searched_rows=rows,
        searched_chunks=len(returned))


def run_script(conn, script: Script, *, fixtures: dict, seed: int | str,
               judge: bool = False, tracker: Tracker | None = None,
               run_id: str | None = None,
               vocabulary: set[str] | None = None) -> dict[str, Any]:
    """Execute one script as a single conversation and grade it as a unit."""
    from x1_advisor.agent.advisor import run_turn

    bound = resolve_bindings(script, seed=seed, fixtures=fixtures) if script.bindings else {}
    history: list[dict] = []
    records: dict[int, TurnRecord] = {}
    turns_out: list[dict[str, Any]] = []
    turn_units: dict[int, dict[str, bool | None]] = {}

    for turn in script.turns:
        question = render_question(turn.question, bound)
        result = run_turn(conn, question, acl="admin", history=history)
        answer = result["bundle"].get("validation", {}).get("answer") or ""
        history += [{"role": "user", "content": question},
                    {"role": "assistant", "content": answer}]

        record = turn_record(conn, turn.n, question, result)
        records[turn.n] = record

        verdict, behavior_verdicts = None, []
        if judge:
            if turn.grade.judged:
                verdict = judge_bundle(conn, result["bundle"], tracker=tracker,
                                       calibration=calibration_state())
                result["bundle"]["scores"].update(verdict["scores"])
                result["bundle"]["judge"] = verdict
            if turn.grade.behavior:
                behavior_verdicts = judge_behaviors(question, answer,
                                                    turn.grade.behavior,
                                                    tracker=tracker)

        # two different things, kept apart on purpose: the assertions this turn
        # DECLARED are its grading contract, while the §5.2 globals are
        # diagnostics that gate nothing until a false-positive audit promotes
        # them (review criterion 4)
        assertions = checkers.run_case_checks(
            answer, evidence=record.evidence,
            deterministic=turn.grade.deterministic,
            citation_stats=result["citation_stats"])
        diagnostics = checkers.run_global_checkers(
            answer, evidence=record.evidence, question=question)
        # the funnel wants a question-shaped dict; a script turn has no
        # `expected` matchers, so the corpus-funnel stages simply stay quiet
        labels = classify(conn, result["bundle"],
                          {"id": f"{script.id}t{turn.n}", "category": script.cls})
        # names stay with the bundle (owner-only), counts go to the manifest.
        # The committed manifest gets NO question text and NO raw judge verdict
        # — the first version wrote both, and full claim text with verdict
        # reasoning sat in git for a day (second review, finding 5). The turn
        # is identified by its number; the template is in the committed suite
        # and the bindings are recorded on the script row.
        result["bundle"]["behavior_verdicts"] = behavior_verdicts
        result["bundle"]["checks"] = {
            "assertions": [a.to_dict() for a in assertions],
            "diagnostics": [d.to_dict() for d in diagnostics]}
        bundle_path = export_bundle(result["bundle"],
                                    name=f"{script.id}t{turn.n}", subdir=run_id)
        turn_units[turn.n] = checkers.unit_verdicts(
            assertions, turn.grade.judged, turn.grade.behavior, verdict,
            behavior_verdicts)
        turns_out.append({
            "turn": turn.n,
            "cost_usd": result["cost_usd"], "latency_ms": result["latency_ms"],
            "citation_stats": result["citation_stats"],
            "searched_documents": record.searched_documents,
            "assertions": [checkers.countable(d) for d in assertions],
            "diagnostics": [checkers.countable(d) for d in diagnostics],
            "labels": labels["labels"], "notes": labels["notes"],
            "behavior": [{"obligation": v["obligation"], "met": v["met"]}
                         for v in behavior_verdicts],
            "graded_units": turn_units[turn.n],
            "judge": judge_manifest_projection(verdict),
            "bundle": bundle_path.name if bundle_path else None,
        })

    cross = evaluate_cross_turn(script, records, vocabulary)
    # §8: the gate unit is the script. A declared per-turn unit failing —
    # deterministic assertion, judged dimension, or behavior obligation — or
    # any cross-turn assertion failing, fails the whole sequence; passing four
    # turns and dropping the set on the fifth is a failed script, not 80% of
    # one. A declared unit the run could not grade (judge off) makes the
    # script UNGRADED rather than passing. The §5.2 diagnostics are reported
    # but deliberately excluded from the verdict until the false-positive
    # audit (criterion 4).
    units: dict[str, bool | None] = {
        f"t{n}:{name}": ok
        for n, turn_result in sorted(turn_units.items())
        for name, ok in turn_result.items()}
    for i, diagnostic in enumerate(cross, 1):
        units[f"cross:{i}:{diagnostic.check}"] = diagnostic.passed
    return {
        "script_id": script.id, "class": script.cls, "tier": script.tier,
        "grading_mode": script.grading_mode,
        "bindings": {k: v.get("name") for k, v in bound.items()},
        "turns": turns_out,
        "cross_turn": [checkers.countable(d) for d in cross],
        "cross_turn_pass": all(d.passed for d in cross),
        "graded_units": units,
        "pass": checkers.compose_pass(units),
        "cost_usd": sum(t["cost_usd"] for t in turns_out),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", default="v2")
    ap.add_argument("--script", default=None, help="run one script by id")
    ap.add_argument("--seed", default="v2-baseline",
                    help="binding seed; paired runs MUST use the same one (§4)")
    ap.add_argument("--judge", action="store_true",
                    help="run the claim/citation judge on turns that ask for it")
    args = ap.parse_args()

    try:
        suite = load_suite(args.golden)
    except CaseValidationError as exc:
        print(f"suite does not compile ({len(exc.errors)} error(s)):", file=sys.stderr)
        for err in exc.errors:
            print(f"  {err}", file=sys.stderr)
        sys.exit(2)

    scripts = [s for s in suite.scripts
               if args.script is None or s.id == args.script]
    if not scripts:
        sys.exit(f"no such script: {args.script}")

    run_id, manifest_path, manifest_file = open_new_manifest(
        f"{dt.date.today()}_scripts_{suite.version}")
    tracker = Tracker(run_id=f"{run_id}:judge",
                      sink=JsonlSink(os.environ.get("ADVISOR_COST_LEDGER",
                                                    "cost_ledger.jsonl")))
    # identity captured once, before the first turn runs (second review,
    # finding 4: the first baseline's rows spanned three commits)
    started_sha, started_fp = git_sha(), code_fingerprint()

    results = []
    with connect() as conn, manifest_file as manifest:
        vocabulary = entity_vocabulary(conn)
        for script in scripts:
            print(f"\n== {script.id} ({script.cls}, {len(script.turns)} turns) ==")
            result = run_script(conn, script, fixtures=suite.fixtures,
                                seed=args.seed, judge=args.judge,
                                tracker=tracker, run_id=run_id,
                                vocabulary=vocabulary)
            for turn in result["turns"]:
                failed = [name for name, ok in turn["graded_units"].items()
                          if ok is False]
                ungraded = [name for name, ok in turn["graded_units"].items()
                            if ok is None]
                print(f"  turn {turn['turn']}  ${turn['cost_usd']:.4f} "
                      f"{turn['latency_ms']}ms  docs={turn['searched_documents']}  "
                      f"cite {turn['citation_stats']['resolved']}/"
                      f"{turn['citation_stats']['emitted']}"
                      + (f"  failed: {','.join(failed)}" if failed else "")
                      + (f"  ungraded: {','.join(ungraded)}" if ungraded
                         else "" if failed else "  checks ok"))
            for diagnostic in result["cross_turn"]:
                mark = "ok  " if diagnostic["passed"] else "FAIL"
                print(f"  {mark} {diagnostic['check']:<24} {diagnostic['detail']}")
            verdict_mark = {True: "PASS", False: "FAIL",
                            None: "UNGRADED"}[result["pass"]]
            print(f"  => {verdict_mark} (${result['cost_usd']:.4f})")
            results.append(result)
            manifest.write(json.dumps({
                "run_id": run_id, "experiment": "golden-v2-scripts",
                "git_sha": started_sha, "code_fingerprint": started_fp,
                **suite.identity(), **result,
            }, default=str) + "\n")

        ended_sha = git_sha()
        drift = ended_sha != started_sha
        manifest.write(json.dumps({
            "record": "summary", "run_id": run_id,
            "git_sha": started_sha, "git_sha_at_end": ended_sha,
            "identity_drift": drift, **suite.identity(), "seed": args.seed,
        }) + "\n")

    passed = sum(1 for r in results if r["pass"] is True)
    ungraded = sum(1 for r in results if r["pass"] is None)
    print(f"\n== {run_id} ==")
    if drift:
        print(f"!! IDENTITY DRIFT: HEAD moved {started_sha} -> {ended_sha} "
              "during the run — do not commit while a run is live; this "
              "manifest is tainted for baseline use")
    print(f"scripts: {passed}/{len(results)} passed as sequences"
          + (f"  ({ungraded} ungraded: declared judged/behavior units need "
             "--judge)" if ungraded else ""))
    print(f"cost: ${sum(r['cost_usd'] for r in results):.4f}"
          + (f" + judge ${tracker.run_total:.4f}" if args.judge else ""))
    print(f"manifest: {manifest_path}")
    print(f"bundles:  {QA_ARTIFACTS_DIR / run_id}")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
