"""Regression tests for the second review's five findings (2026-08-01).

Each test names the defect it pins down. These are integration-shaped on
purpose: every one of the five shipped through unit-tested code because the
seams between modules — runner→manifest→comparator, truth-builder→grader,
runner→committed-artifact — had no tests of their own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments import checkers, compare, nightly
from experiments.run_v2 import grade_against_truth, truth_unit_passed
from x1_advisor.agent.bundle import judge_manifest_projection

RUNS_DIR = Path(__file__).resolve().parent.parent / "experiments" / "runs"


# --- finding 1: the comparator compared zero v2 records and said PASS ------


def _case_row(case_id: str, *, passed: bool | None, labels=(), bindings=None,
              truth_digest=None, contract="golden-v2.0/modes-aaaaaaaa",
              judge=None, scores=None) -> dict:
    return {"case_id": case_id, "scoring_contract": contract,
            "suite_digest": "d" * 16, "pass": passed, "labels": list(labels),
            "bindings": bindings or {}, "truth_digest": truth_digest,
            "judge": judge, "scores": scores or {},
            "fingerprint": {"corpus": "c1"}}


def _write_manifest(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _run_compare(monkeypatch, before: Path, after: Path) -> int:
    monkeypatch.setattr("sys.argv", ["compare", str(before), str(after)])
    with pytest.raises(SystemExit) as excinfo:
        compare.main()
    return excinfo.value.code or 0


def test_v2_rows_load_and_carry_their_recorded_contract():
    rows = [_case_row("v2c001", passed=True)]
    assert compare.rec_id(rows[0]) == "v2c001"
    assert compare.contract_of(rows) == "golden-v2.0/modes-aaaaaaaa/unjudged"
    judged = [_case_row("v2c001", passed=True,
                        judge={"judge_model": "gpt-5.6-terra", "labels": []})]
    assert compare.contract_of(judged) == ("golden-v2.0/modes-aaaaaaaa"
                                           "/judged/gpt-5.6-terra")


def test_unreadable_manifest_is_not_comparable_never_pass(monkeypatch, tmp_path):
    # rows whose shape the loader does not know must exit 2, not compare
    # zero-vs-zero and print PASS
    unknown = _write_manifest(tmp_path / "a.jsonl", [{"mystery": 1}])
    other = _write_manifest(tmp_path / "b.jsonl", [_case_row("v2c001", passed=True)])
    assert _run_compare(monkeypatch, unknown, other) == 2


def test_disjoint_slices_are_not_comparable(monkeypatch, tmp_path):
    # a core manifest against a scripts manifest shares no unit ids — there is
    # nothing to gate, and "no shared questions, no regressions, PASS" is the
    # vacuous verdict this exits 2 to prevent
    core = _write_manifest(tmp_path / "core.jsonl",
                           [_case_row("v2c001", passed=True)])
    scripts = _write_manifest(
        tmp_path / "scripts.jsonl",
        [{**_case_row("x", passed=False), "case_id": None,
          "script_id": "v2s001"}])
    assert _run_compare(monkeypatch, core, scripts) == 2


def test_v2_regression_beyond_budget_fails(monkeypatch, tmp_path):
    before = _write_manifest(tmp_path / "b.jsonl", [
        _case_row(f"v2c{n:03d}", passed=True) for n in range(1, 7)])
    after = _write_manifest(tmp_path / "a.jsonl", [
        _case_row(f"v2c{n:03d}", passed=(n > 4)) for n in range(1, 7)])
    assert _run_compare(monkeypatch, before, after) == 1  # net -4 > budget 2


def test_v2_within_budget_passes(monkeypatch, tmp_path):
    before = _write_manifest(tmp_path / "b.jsonl", [
        _case_row("v2c001", passed=True), _case_row("v2c002", passed=False)])
    after = _write_manifest(tmp_path / "a.jsonl", [
        _case_row("v2c001", passed=False), _case_row("v2c002", passed=True)])
    assert _run_compare(monkeypatch, before, after) == 0   # net 0


def test_differing_contracts_refuse_to_gate(monkeypatch, tmp_path):
    before = _write_manifest(tmp_path / "b.jsonl", [_case_row("v2c001", passed=True)])
    after = _write_manifest(tmp_path / "a.jsonl", [
        _case_row("v2c001", passed=True, contract="golden-v2.0/modes-bbbbbbbb")])
    assert _run_compare(monkeypatch, before, after) == 2


def test_moved_bindings_or_oracle_count_as_incomplete(monkeypatch, tmp_path):
    # same case id, different bound entity: not the same question twice.
    # It is excluded from the gates and FAILS via completeness, so a run that
    # quietly re-bound its entities can never read as "no regressions".
    before = _write_manifest(tmp_path / "b.jsonl", [
        _case_row("v2c001", passed=True, bindings={"company": "Calmr"}),
        _case_row("v2c002", passed=True)])
    after = _write_manifest(tmp_path / "a.jsonl", [
        _case_row("v2c001", passed=True, bindings={"company": "Angiex"}),
        _case_row("v2c002", passed=True)])
    assert _run_compare(monkeypatch, before, after) == 1


def test_ungraded_rows_count_as_incomplete(monkeypatch, tmp_path):
    # pass=None (a declared unit could not be graded) must never sail through
    before = _write_manifest(tmp_path / "b.jsonl", [
        _case_row("v2c001", passed=True), _case_row("v2c002", passed=True)])
    after = _write_manifest(tmp_path / "a.jsonl", [
        _case_row("v2c001", passed=True), _case_row("v2c002", passed=None)])
    assert _run_compare(monkeypatch, before, after) == 1


# --- finding 1 (nightly half): every slice gates, scripts roll up ----------


def test_slice_markers_identify_each_manifest_kind():
    assert nightly.slice_of("2026-08-01_v2_smoke_abc_r1.jsonl") == "smoke"
    assert nightly.slice_of("2026-08-01_v2_core_abc_r1.jsonl") == "core"
    assert nightly.slice_of("2026-08-01_scripts_v2.0_abc_r1.jsonl") == "scripts"
    with pytest.raises(SystemExit):
        nightly.slice_of("2026-08-01_v2_all_abc_r1.jsonl")


def test_accept_baseline_refuses_identity_drift(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    name = "2026-08-01_v2_core_abc_r1.jsonl"
    (runs / name).write_text(
        json.dumps({"case_id": "v2c001", "pass": True}) + "\n"
        + json.dumps({"record": "summary", "identity_drift": True}) + "\n")
    monkeypatch.setattr(nightly, "RUNS_DIR", runs)
    with pytest.raises(SystemExit, match="tainted"):
        nightly.accept_baseline([name])


def test_scripts_exit_code_reaches_the_nightly_verdict(monkeypatch):
    calls = []

    def fake_run(argv):
        calls.append(argv)
        if argv[0] == "experiments.script_runner":
            return 1, "0/4 passed"
        return 0, "ok"

    monkeypatch.setattr(nightly, "_run", fake_run)
    steps: list[dict] = []
    worst = nightly.job_golden(steps, full=True, judge=False, seed="s")
    assert worst == 1                      # was dropped on the floor before
    assert any(argv[0] == "experiments.script_runner" for argv in calls)


# --- finding 2: pass covers every declared graded unit ---------------------


def _assertion(check: str, passed: bool) -> checkers.Diagnostic:
    return checkers.Diagnostic(check=check, passed=passed)


def test_judge_labels_refute_declared_dimensions():
    verdict = {"labels": ["synthesis_error"]}
    units = checkers.unit_verdicts([_assertion("must_cite", True)],
                                   ["faithfulness"], [], verdict, [])
    assert units == {"must_cite": True, "judged:faithfulness": False}
    assert checkers.compose_pass(units) is False


def test_declared_units_without_a_judge_leave_the_case_ungraded():
    units = checkers.unit_verdicts([_assertion("must_cite", True)],
                                   ["faithfulness"], ["state_absence"],
                                   None, [])
    assert units["judged:faithfulness"] is None
    assert units["behavior:state_absence"] is None
    assert checkers.compose_pass(units) is None      # ungraded, not passing


def test_behavior_verdicts_gate_their_obligations():
    units = checkers.unit_verdicts(
        [], [], ["correct_premise"], None,
        [{"obligation": "correct_premise", "met": False}])
    assert checkers.compose_pass(units) is False


def test_a_definite_failure_beats_an_ungraded_unit():
    units = {"must_cite": False, "judged:faithfulness": None}
    assert checkers.compose_pass(units) is False


# --- finding 3: truth keys must be matchable; negation is not overclaim ----


def _truth(entities):
    return {"entities": entities}


def test_cv_truth_keys_match_answer_prose():
    # builder v1 keyed people by document title ("Paul Jaminet — CV"), which
    # no answer ever names — recall was structurally zero for people/CV cases
    truth = _truth([{"key": "Paul Jaminet", "status": "matched"},
                    {"key": "Dana Scully", "status": "no_match"}])
    grade = grade_against_truth(
        "Paul Jaminet has machine-learning experience. [1]", truth, set())
    assert grade["recall"] == 1.0
    assert grade["overclaim_count"] == 0


def test_lowercase_and_dotted_names_are_found():
    truth = _truth([{"key": "2ndCourt.com", "status": "matched"}])
    grade = grade_against_truth("The strongest match is 2ndCourt.com. [1]",
                                truth, set())
    assert grade["recall"] == 1.0


def test_negated_mentions_are_disclosure_not_overclaim():
    truth = _truth([{"key": "ButterBeKind", "status": "no_match"},
                    {"key": "Angiex", "status": "matched"}])
    grade = grade_against_truth(
        "Angiex discusses it directly. [1] ButterBeKind is adjacent but its "
        "evaluation does not mention synthetic biology.", truth, set())
    assert grade["overclaim_count"] == 0
    assert grade["recall"] == 1.0
    assert grade["negated_count" if "negated_count" in grade else "negated"]


def test_positive_assertion_of_a_nonmatch_is_an_overclaim():
    truth = _truth([{"key": "ButterBeKind", "status": "no_match"}])
    grade = grade_against_truth(
        "ButterBeKind's evaluation discusses synthetic biology. [1]",
        truth, set())
    assert grade["overclaim_count"] == 1


def test_empty_oracle_respected_despite_negated_scope_naming():
    truth = _truth([{"key": "Calmr", "status": "no_match"},
                    {"key": "Angiex", "status": "no_match"}])
    honest = grade_against_truth(
        "I searched all 25 evaluations including Calmr and Angiex; none "
        "mention clinical validation risk.", truth, set())
    assert honest["empty_oracle_respected"] is True
    dishonest = grade_against_truth(
        "Calmr's evaluation flags clinical validation risk. [1]", truth, set())
    assert dishonest["empty_oracle_respected"] is False


def test_truth_unit_gates_by_grading_mode():
    partial = {"overclaim_count": 0, "empty_oracle_respected": None,
               "recall": 0.4}
    assert truth_unit_passed(partial, "honesty") is True     # disclosure bar
    assert truth_unit_passed(partial, "capability") is False  # right-set bar
    overclaiming = {"overclaim_count": 2, "empty_oracle_respected": None,
                    "recall": 1.0}
    assert truth_unit_passed(overclaiming, "honesty") is False


# --- finding 5: committed manifests stay body-free -------------------------


def test_judge_projection_carries_no_claim_text():
    raw = {"judge_model": "gpt-5.6-terra", "labels": ["synthesis_error"],
           "counts": {"supported": 1}, "scores": {"faithfulness": 0.5},
           "calibration": {"state": "human-calibrated"},
           "evidence_provenance": "turn-snapshot",
           "verdicts": [{"claim": "SECRET PROSE", "reason": "SECRET"}],
           "uncited_claims": ["SECRET PROSE"],
           "claims": {"total": 3}}
    projected = judge_manifest_projection(raw)
    assert "SECRET" not in json.dumps(projected)
    assert projected["labels"] == ["synthesis_error"]
    assert judge_manifest_projection(None) is None


FORBIDDEN_ROW_KEYS = {"question", "verdicts", "uncited_claims", "answer",
                      "evidence"}


def test_committed_run_manifests_are_body_free():
    """Every committed v2 manifest row, including nested turn rows, must carry
    no question text, no judge claim prose, no answer bodies. The scripts
    manifest violated this for a day (second review, finding 5).

    Scoped to v2 rows (`scoring_contract` present): golden v1 manifests are
    immutable history and their `question` fields duplicate the committed
    v1.yaml, which is public suite content, not a body. v2 is stricter
    because rendered questions embed resolved entity bindings and template
    text that only owner-only storage may carry alongside answers.
    """
    def offending_keys(obj, path=""):
        found = []
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in FORBIDDEN_ROW_KEYS and value:
                    found.append(f"{path}.{key}")
                found += offending_keys(value, f"{path}.{key}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                found += offending_keys(item, f"{path}[{i}]")
        return found

    for manifest in sorted(RUNS_DIR.glob("*.jsonl")):
        for line in manifest.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not compare.rec_id(row) or not row.get("scoring_contract"):
                continue
            bad = offending_keys(row)
            assert not bad, f"{manifest.name}: body-carrying key(s) {bad}"


# --- 2026-08-01 triage, machinery item a -----------------------------------


def test_entity_mentions_do_not_cross_paragraph_boundaries():
    text = "The founder studied at Stanford University.\n\nCalmr raised a round."
    mentions = checkers.entity_mentions(text)
    assert "Calmr" in mentions
    assert not any("University" in m and "Calmr" in m for m in mentions)
