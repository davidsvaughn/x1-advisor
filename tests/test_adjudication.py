"""Unit tests: the escalation gates (experiments/adjudicate.py, s3).

The properties that make the gates safe, each pinned:

  - escalation only: nothing flagged → no adjudication, formula verdict
    untouched;
  - fail-safe: an unusable judge (nothing parseable, missing verdicts) can
    never convert a formula failure into a pass;
  - per-item majority over k samples; a single dissent does not flip;
  - the formula's own output survives beside the adjudication (telemetry —
    the leniency-ratchet tripwire);
  - sub-shape B is structurally out of reach: wrong-ref claims fail
    faithfulness, and no citation_coverage adjudication touches faithfulness;
  - manifest projections stay body-free (no claim text, no entity names).

Run: uv run pytest -q tests/test_adjudication.py
"""

from __future__ import annotations

import json

from experiments import adjudicate, checkers
from experiments.adjudicate import (adjudicate_asserted_names,
                                    adjudicate_citation_coverage)
from experiments.run_v2 import _countable_truth, truth_unit_passed
from x1_advisor.agent.bundle import judge_manifest_projection

BUNDLE = {
    "request": {"question": "What objections appear in the evaluation?"},
    "validation": {"answer": "**Fit is narrow.** Inks are resistive. [ref1]",
                   "citations": [{"ref": "r1", "n": 1, "type": "internal"}]},
    "evidence": [{"ref": "r1", "kind": "chunk", "title": "Calmr — Evaluation",
                  "document_id": 1, "block_index": 0, "snapshot": "inks are resistive"}],
    "schema_version": 3,
}


def _transport_returning(*payloads):
    """A fake CLI runner yielding each payload in turn (cycling the last)."""
    seq = list(payloads)

    def run(prompt, *, tracker=None, stage=""):
        out = seq.pop(0) if len(seq) > 1 else seq[0]
        return {"result": json.dumps(out),
                "modelUsage": {"claude-opus-5": {"inputTokens": 1,
                                                 "outputTokens": 1}}}
    return run


def _cc_verdict(uncited=("Fit is narrow.",)):
    return {"labels": ["citation_coverage_error"] if uncited else [],
            "counts": {"supported": 1, "partial": 0, "unsupported": 0,
                       "unverifiable": 0},
            "scores": {"faithfulness": 1.0, "citation_coverage": 0.5},
            "uncited_claims": list(uncited), "judge_model": "cc:claude-opus-5"}


# --- citation gate ---------------------------------------------------------


def test_nothing_flagged_means_no_adjudication():
    assert adjudicate_citation_coverage(BUNDLE, _cc_verdict(uncited=()),
                                        _transport=_transport_returning({})) is None


def test_adequate_majority_flips_the_gate_and_keeps_formula_telemetry():
    ok = {"verdicts": [{"id": 1, "adequate": True, "reason": "body cited"}]}
    verdict = _cc_verdict()
    adj = adjudicate_citation_coverage(BUNDLE, verdict,
                                       _transport=_transport_returning(ok))
    assert adj["passed"] is True and adj["inadequate"] == 0
    # the formula's own verdict is untouched telemetry
    assert verdict["labels"] == ["citation_coverage_error"]
    assert verdict["scores"]["citation_coverage"] == 0.5

    verdict["adjudications"] = {"citation_coverage": adj}
    units = checkers.unit_verdicts([], ["citation_coverage"], [], verdict, [])
    assert units["judged:citation_coverage"] is True


def test_single_dissent_does_not_flip_majority():
    ok = {"verdicts": [{"id": 1, "adequate": True, "reason": "ok"}]}
    no = {"verdicts": [{"id": 1, "adequate": False, "reason": "no path"}]}
    adj = adjudicate_citation_coverage(BUNDLE, _cc_verdict(),
                                       _transport=_transport_returning(no, ok, ok))
    assert adj["passed"] is True and adj["per_claim"][0]["votes"].count(True) == 2


def test_inadequate_majority_keeps_the_failure():
    no = {"verdicts": [{"id": 1, "adequate": False, "reason": "no path"}]}
    verdict = _cc_verdict()
    adj = adjudicate_citation_coverage(BUNDLE, verdict,
                                       _transport=_transport_returning(no))
    assert adj["passed"] is False
    verdict["adjudications"] = {"citation_coverage": adj}
    units = checkers.unit_verdicts([], ["citation_coverage"], [], verdict, [])
    assert units["judged:citation_coverage"] is False


def test_unusable_judge_is_fail_safe_never_a_free_pass():
    # nothing parseable across all samples → formula failure stands
    adj = adjudicate_citation_coverage(
        BUNDLE, _cc_verdict(),
        _transport=lambda prompt, tracker=None, stage="": {"result": "garbage"})
    assert adj["passed"] is False and adj["samples_used"] == 0
    # a sample that omits the flagged id earns no verdict → same fail-safe
    empty = {"verdicts": []}
    adj = adjudicate_citation_coverage(BUNDLE, _cc_verdict(),
                                       _transport=_transport_returning(empty))
    assert adj["passed"] is False


def test_wrong_ref_claims_are_out_of_reach_of_this_gate():
    """Sub-shape B fails faithfulness (partial/unsupported on a CITED claim);
    citation adjudication only ever touches the citation_coverage unit."""
    verdict = {"labels": ["synthesis_error"],
               "scores": {"faithfulness": 0.5, "citation_coverage": 1.0},
               "uncited_claims": [], "judge_model": "cc:claude-opus-5"}
    assert adjudicate_citation_coverage(BUNDLE, verdict,
                                        _transport=_transport_returning({})) is None
    units = checkers.unit_verdicts([], ["faithfulness", "citation_coverage"],
                                   [], verdict, [])
    assert units["judged:faithfulness"] is False       # B stays failed
    assert units["judged:citation_coverage"] is True


# --- faithfulness gate (s4) ------------------------------------------------


def _faith_verdict(partials=1, unsupported=0, unverifiable=0):
    verdicts = ([{"claim": f"Hedged synthesis {i}.", "citations": [1],
                  "verdict": "partial", "reason": "inference"}
                 for i in range(partials)]
                + [{"claim": "Fabricated.", "citations": [1],
                    "verdict": "unsupported", "reason": "contradicted"}
                   for _ in range(unsupported)])
    return {"labels": ["synthesis_error"],
            "counts": {"supported": 5, "partial": partials,
                       "unsupported": unsupported,
                       "unverifiable": unverifiable},
            "scores": {"faithfulness": 5 / (5 + partials + unsupported),
                       "citation_coverage": 1.0},
            "verdicts": verdicts, "uncited_claims": [],
            "judge_model": "cc:claude-opus-5"}


def test_faithful_partials_flip_the_gate():
    from experiments.adjudicate import adjudicate_faithfulness
    ok = {"verdicts": [{"id": 1, "faithful": True, "reason": "hedged, inputs cited"}]}
    verdict = _faith_verdict(partials=1)
    adj = adjudicate_faithfulness(BUNDLE, verdict,
                                  _transport=_transport_returning(ok))
    assert adj["passed"] is True
    verdict["adjudications"] = {"faithfulness": adj}
    units = checkers.unit_verdicts([], ["faithfulness"], [], verdict, [])
    assert units["judged:faithfulness"] is True
    # formula score untouched — telemetry
    assert verdict["scores"]["faithfulness"] < 1.0


def test_unsupported_claims_block_escalation_entirely():
    """Fabrication is never adjudicated: with any unsupported (or
    unverifiable) verdict present the gate cannot flip, so no judge tokens
    are spent and the formula failure stands."""
    from experiments.adjudicate import adjudicate_faithfulness
    verdict = _faith_verdict(partials=2, unsupported=1)
    assert adjudicate_faithfulness(
        BUNDLE, verdict,
        _transport=_transport_returning({})) is None
    units = checkers.unit_verdicts([], ["faithfulness"], [], verdict, [])
    assert units["judged:faithfulness"] is False
    verdict = _faith_verdict(partials=1, unverifiable=1)
    assert adjudicate_faithfulness(
        BUNDLE, verdict, _transport=_transport_returning({})) is None


def test_misleading_partial_keeps_the_failure_fail_safe():
    from experiments.adjudicate import adjudicate_faithfulness
    no = {"verdicts": [{"id": 1, "faithful": False, "reason": "stated as fact"}]}
    verdict = _faith_verdict(partials=1)
    adj = adjudicate_faithfulness(BUNDLE, verdict,
                                  _transport=_transport_returning(no))
    assert adj["passed"] is False
    # unusable judge → formula failure stands
    adj = adjudicate_faithfulness(
        BUNDLE, _faith_verdict(partials=1),
        _transport=lambda prompt, tracker=None, stage="": {"result": "?"})
    assert adj["passed"] is False and adj["samples_used"] == 0


# --- asserted_names gate ---------------------------------------------------

GRADE = {"truth_matched": 3, "hit_count": 2, "missed": ["calmr"],
         "overclaimed": ["beespenser", "zeropact"], "overclaim_count": 2,
         "recall": 2 / 3, "empty_oracle_respected": None,
         "negated": [], "excluded": [], "scope_listed": [], "named": 2,
         "precision": 0.5}


def test_names_adjudication_flips_disclosed_names_and_rescues_credit():
    votes = {"overclaim_flagged": [
                 {"id": 1, "verdict": False, "reason": "labeled exclusion group"},
                 {"id": 2, "verdict": True, "reason": "asserted as matching"}],
             "miss_flagged": [
                 {"id": 1, "verdict": True, "reason": "credited via variant, grouped"}]}
    adj = adjudicate_asserted_names("q", "answer text", GRADE,
                                    _transport=_transport_returning(votes))
    assert adj["asserted"] == ["zeropact"]         # upheld overclaim
    assert adj["disclosed"] == ["beespenser"]      # rule-5 compliance, no liability
    assert adj["credited"] == ["calmr"]            # rescued recall
    assert adj["overclaim_count"] == 1 and adj["recall"] == 1.0

    graded = {**GRADE, "adjudication": adj}
    # zeropact stands: still an overclaim → still failing, both modes
    assert truth_unit_passed(graded, "honesty") is False
    assert truth_unit_passed(graded, "capability") is False


def test_names_adjudication_full_disclosure_passes_capability():
    votes = {"overclaim_flagged": [
                 {"id": 1, "verdict": False, "reason": "exclusion group"},
                 {"id": 2, "verdict": False, "reason": "scope list"}],
             "miss_flagged": [{"id": 1, "verdict": True, "reason": "grouped credit"}]}
    adj = adjudicate_asserted_names("q", "answer", GRADE,
                                    _transport=_transport_returning(votes))
    graded = {**GRADE, "adjudication": adj}
    assert truth_unit_passed(graded, "capability") is True
    # formula values still present and unchanged — telemetry
    assert graded["overclaim_count"] == 2 and graded["recall"] == 2 / 3


def test_names_fail_safe_keeps_the_formula_reading():
    # judge silent on both lists → overclaims stand, misses stay uncredited
    adj = adjudicate_asserted_names(
        "q", "answer", GRADE,
        _transport=_transport_returning({"overclaim_flagged": [],
                                         "miss_flagged": []}))
    assert adj["overclaim_count"] == 2 and adj["recall"] == 2 / 3
    assert truth_unit_passed({**GRADE, "adjudication": adj}, "honesty") is False


def test_empty_oracle_adjudication():
    grade = {**GRADE, "truth_matched": 0, "hit_count": 0, "missed": [],
             "recall": None, "empty_oracle_respected": False}
    votes = {"overclaim_flagged": [
                 {"id": 1, "verdict": False, "reason": "scope enumeration"},
                 {"id": 2, "verdict": False, "reason": "scope enumeration"}],
             "miss_flagged": []}
    adj = adjudicate_asserted_names("q", "answer", grade,
                                    _transport=_transport_returning(votes))
    assert adj["empty_oracle_respected"] is True
    assert truth_unit_passed({**grade, "adjudication": adj}, "honesty") is True


# --- projections stay body-free -------------------------------------------


def test_manifest_projections_carry_counts_never_text():
    ok = {"verdicts": [{"id": 1, "adequate": True, "reason": "body cited"}]}
    verdict = _cc_verdict()
    verdict["adjudications"] = {"citation_coverage": adjudicate_citation_coverage(
        BUNDLE, verdict, _transport=_transport_returning(ok))}
    proj = judge_manifest_projection(verdict)
    flat = json.dumps(proj)
    assert proj["adjudications"]["citation_coverage"]["passed"] is True
    assert "per_claim" not in flat and "Fit is narrow" not in flat

    votes = {"overclaim_flagged": [
                 {"id": 1, "verdict": False, "reason": "x"},
                 {"id": 2, "verdict": True, "reason": "y"}],
             "miss_flagged": [{"id": 1, "verdict": True, "reason": "z"}]}
    graded = {**GRADE, "adjudication": adjudicate_asserted_names(
        "q", "a", GRADE, _transport=_transport_returning(votes))}
    tproj = json.dumps(_countable_truth(graded))
    assert "beespenser" not in tproj and "calmr" not in tproj
    assert json.loads(tproj)["adjudication"]["asserted_count"] == 1
    assert json.loads(tproj)["missed_count"] == 1
