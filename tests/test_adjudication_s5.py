"""Unit tests: the s5 escalation surface — gated assertions, cross-turn
checks, quote canonicalization (experiments/adjudicate.py + checkers +
script_runner).

Pinned properties, mirroring test_adjudication.py's for the s3/s4 gates:

  - escalation only: nothing flagged → None, formula verdict untouched; a
    quotes obligation with NO quotes at all never escalates (real absence);
  - fail-safe everywhere: unusable judge leaves the formula failure standing;
    an unadjudicated coverage-claim flag STAYS an overclaim, an unadjudicated
    intruder STAYS an intruder;
  - the formula verdict survives as telemetry (detail["formula_passed"]) and
    manifests stay body-free (adjudication_items projects to a count);
  - quote canonicalization: typographic variants, markdown emphasis, inline
    links and non-breaking spaces are formatting; marked elision and bracket
    splices are NOT canonicalized away — they escalate to the gate;
  - the coverage denominator counts entities a scan actually scanned.

Run: uv run pytest -q tests/test_adjudication_s5.py
"""

from __future__ import annotations

import json

from experiments import checkers
from experiments.adjudicate import (adjudicate_coverage_claims,
                                    adjudicate_coverage_statement,
                                    adjudicate_entity_intrusion,
                                    adjudicate_quotes, escalate_assertions)
from experiments.checkers import _canon_quote, check_quotes_verbatim, countable
from experiments.script_runner import (TurnRecord,
                                       assert_coverage_claim_grounded,
                                       escalate_cross_turn)


def _transport_returning(*payloads):
    seq = list(payloads)

    def run(prompt, *, tracker=None, stage=""):
        out = seq.pop(0) if len(seq) > 1 else seq[0]
        return {"result": json.dumps(out),
                "modelUsage": {"claude-opus-5": {"inputTokens": 1,
                                                 "outputTokens": 1}}}
    return run


# --- quote canonicalization (detector hygiene) -----------------------------


def test_canon_folds_typography_emphasis_and_links():
    ev = ["usually position themselves as non‑clinical companions",
          "vulnerable users ([EU AI Act overview](https://x.eu/a)). Even as "
          "a “wellness” product",
          "Regulatory enforcement risk: If an app makes claims, scrutiny follows"]
    good = ('The answer quotes "usually position themselves as non-clinical '
            'companions" and "vulnerable users. Even as a \'wellness\' product" '
            'and "**Regulatory enforcement risk:** If an app makes claims, '
            'scrutiny follows".')
    assert check_quotes_verbatim(good, ev).passed


def test_canon_does_not_absorb_elision_or_paraphrase():
    ev = ["The act restricts therapist chatbots and signals direct regulatory activity."]
    elided = 'It says "The act restricts therapist chatbots … regulatory activity."'
    assert not check_quotes_verbatim(elided, ev).passed  # gate's job, not canon's
    paraphrase = 'It says "the law bans therapy bots outright."'
    assert not check_quotes_verbatim(paraphrase, ev).passed


def test_canon_handles_nbsp():
    assert _canon_quote("a b") == _canon_quote("a b")


# --- quotes gate -----------------------------------------------------------

_EV = ["The act restricts therapist chatbots and signals direct regulatory activity."]


def test_quotes_gate_escalation_only():
    assert adjudicate_quotes("q", "a", [], _EV,
                             _transport=_transport_returning({})) is None


def test_quotes_gate_flips_on_faithful_majority():
    ok = {"verdicts": [{"id": 1, "faithful": True, "reason": "marked elision"}]}
    adj = adjudicate_quotes("q", "a", ["The act … regulatory activity."], _EV,
                            _transport=_transport_returning(ok))
    assert adj["passed"] is True and adj["inadequate"] == 0


def test_quotes_gate_fail_safe_on_garbage():
    adj = adjudicate_quotes("q", "a", ["whatever"], _EV,
                            _transport=lambda p, *, tracker=None, stage="":
                            {"result": "not json", "modelUsage": {}})
    assert adj["passed"] is False


def test_quotes_gate_single_dissent_does_not_flip():
    yes = {"verdicts": [{"id": 1, "faithful": True, "reason": "ok"}]}
    no = {"verdicts": [{"id": 1, "faithful": False, "reason": "paraphrase"}]}
    adj = adjudicate_quotes("q", "a", ["x y z"], _EV,
                            _transport=_transport_returning(no, yes, yes))
    assert adj["passed"] is True and adj["per_item"][0]["votes"].count(False) == 1


# --- coverage-statement gate -----------------------------------------------


def test_coverage_statement_gate_flips_and_fails_safe():
    ok = {"disclosed": True, "reason": "states the 12 unindexed startups"}
    adj = adjudicate_coverage_statement("q", "a", _transport=_transport_returning(ok))
    assert adj["passed"] is True
    bad = adjudicate_coverage_statement(
        "q", "a", _transport=lambda p, *, tracker=None, stage="":
        {"result": "nope", "modelUsage": {}})
    assert bad["passed"] is False


# --- coverage-claims gate --------------------------------------------------


def test_coverage_claims_inventory_numbers_are_not_overclaims():
    ok = {"verdicts": [{"id": 1, "overclaim": False, "reason": "tool-reported"},
                       {"id": 2, "overclaim": False, "reason": "inventory"}]}
    adj = adjudicate_coverage_claims("q", "a", [47, 39], {"turn1": {}}, [],
                                     _transport=_transport_returning(ok))
    assert adj["passed"] is True and adj["inadequate"] == 0


def test_coverage_claims_unadjudicated_flag_stays_an_overclaim():
    partial = {"verdicts": [{"id": 1, "overclaim": False, "reason": "inventory"}]}
    adj = adjudicate_coverage_claims("q", "a", [47, 20], {"t": {}}, [],
                                     _transport=_transport_returning(partial))
    assert adj["passed"] is False            # id 2 earned no verdict → stands
    assert adj["per_item"][1]["overclaim"] is True


def test_coverage_claims_upheld_overclaim_fails():
    bad = {"verdicts": [{"id": 1, "overclaim": True,
                         "reason": "claims to have searched all 20"}]}
    adj = adjudicate_coverage_claims("q", "a", [20], {"t": {}}, [],
                                     _transport=_transport_returning(bad))
    assert adj["passed"] is False and adj["inadequate"] == 1


def test_coverage_claims_telemetry_rides_in_the_prompt():
    seen = {}

    def spy(prompt, *, tracker=None, stage=""):
        seen["prompt"] = prompt
        return {"result": json.dumps(
            {"verdicts": [{"id": 1, "overclaim": False, "reason": "r"}]}),
            "modelUsage": {}}
    adjudicate_coverage_claims("q", "a", [25],
                               {"turn1": {"scan_entities_scanned": 25}}, [],
                               _transport=spy)
    assert "scan_entities_scanned" in seen["prompt"]


# --- entity-intrusion gate -------------------------------------------------


def test_intrusion_grounded_entity_flips():
    ok = {"verdicts": [{"id": 1, "grounded": True,
                        "reason": "surfaced by the scan in turn 2"}]}
    adj = adjudicate_entity_intrusion("q", "a", ["acceliumpartnersag"],
                                      ["scan matched AcceliumPartnersAG"],
                                      _transport=_transport_returning(ok))
    assert adj["passed"] is True


def test_intrusion_ungrounded_entity_stays_an_intruder():
    bad = {"verdicts": [{"id": 1, "grounded": False, "reason": "no basis"}]}
    adj = adjudicate_entity_intrusion("q", "a", ["zorgcorp"], ["nothing"],
                                      _transport=_transport_returning(bad))
    assert adj["passed"] is False


# --- assertion escalation wiring -------------------------------------------


def test_no_quotes_at_all_never_escalates():
    assertions = [check_quotes_verbatim("no excerpts here", _EV,
                                        require_quotes=True)]
    calls = []

    def spy(prompt, *, tracker=None, stage=""):
        calls.append(stage)
        return {"result": "{}", "modelUsage": {}}
    escalate_assertions(assertions, question="q", answer="no excerpts here",
                        evidence=_EV, _transport=spy)
    assert not calls and assertions[0].passed is False


def test_escalated_assertion_keeps_formula_telemetry_and_stays_body_free():
    answer = 'It says "The act … regulatory activity."'
    assertions = [check_quotes_verbatim(answer, _EV, require_quotes=True)]
    ok = {"verdicts": [{"id": 1, "faithful": True, "reason": "marked elision"}]}
    escalate_assertions(assertions, question="q", answer=answer, evidence=_EV,
                        _transport=_transport_returning(ok))
    a = assertions[0]
    assert a.passed is True
    assert a.detail["formula_passed"] is False
    projected = json.dumps(countable(a))
    assert "regulatory activity" not in projected     # no quote bodies
    assert "adjudication_items_count" in projected


def test_passing_assertion_is_left_alone():
    answer = 'It says "The act restricts therapist chatbots and signals direct regulatory activity."'
    assertions = [check_quotes_verbatim(answer, _EV, require_quotes=True)]
    escalate_assertions(assertions, question="q", answer=answer, evidence=_EV,
                        _transport=lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("gate must not run")))
    assert assertions[0].passed is True


# --- cross-turn escalation wiring ------------------------------------------


def _records():
    return {1: TurnRecord(n=1, question="scan?", answer="scanned.",
                          evidence=["scan matched AcceliumPartnersAG"],
                          searched_scan_entities=25),
            2: TurnRecord(n=2, question="all 20?",
                          answer="No — 25 searchable, 39 not indexed.",
                          evidence=["counts: eligible 64, scanned 25"])}


def test_scan_entities_count_in_denominator():
    diag = assert_coverage_claim_grounded(_records(), 2)
    assert diag.detail["searched"] == 25
    assert 39 in diag.detail["overclaimed"] and 25 not in diag.detail["overclaimed"]


def test_cross_turn_coverage_claims_escalation_flips():
    cross = [assert_coverage_claim_grounded(_records(), 2)]
    assert cross[0].passed is False
    ok = {"verdicts": [{"id": 1, "overclaim": False,
                        "reason": "tool-reported unindexed count"}]}
    escalate_cross_turn(cross, _records(), _transport=_transport_returning(ok))
    assert cross[0].passed is True
    assert cross[0].detail["formula_passed"] is False
    assert "39" not in json.dumps(checkers.countable(cross[0]).get("detail")
                                  .get("adjudication_items_count", ""))


def test_cross_turn_fail_safe_keeps_formula_failure():
    cross = [assert_coverage_claim_grounded(_records(), 2)]
    escalate_cross_turn(cross, _records(),
                        _transport=lambda p, *, tracker=None, stage="":
                        {"result": "garbage", "modelUsage": {}})
    assert cross[0].passed is False
