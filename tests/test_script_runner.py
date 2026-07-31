"""Unit tests: cross-turn assertions (GOLDEN-V2-DESIGN §8).

The assertions are pure functions of TurnRecord, which is the point: the thing
that decides whether a script passed can be tested with hand-written turns, no
database and no model spend.

Run: uv run pytest -q
"""

from __future__ import annotations

from experiments.cases import Readiness, Script, Turn, Grade
from experiments.script_runner import (
    TurnRecord,
    assert_coverage_claim_grounded,
    assert_no_new_entities,
    assert_quotes_from_turn,
    assert_set_carryover,
    evaluate_cross_turn,
)

READY = Readiness(source_available=True, tool_ready=True, scope="corpus",
                  operation="exact_scan", context_required="prior_answer",
                  golden_priority="p0")


def records(*turns: TurnRecord) -> dict[int, TurnRecord]:
    return {t.n: t for t in turns}


def test_set_carryover_passes_when_the_later_turn_uses_the_established_set():
    r = records(
        TurnRecord(1, "show all the startups", "Calmr, ZeroPact and Angiex."),
        TurnRecord(2, "which of these mention regulatory risk?",
                   "Calmr and Angiex mention it."))
    assert assert_set_carryover(r, 1, 2).passed


def test_set_carryover_fails_when_the_later_turn_drops_the_set():
    """The failure §8 exists to catch: turn 2 answers a corpus-wide question
    instead of the one about "these"."""
    r = records(
        TurnRecord(1, "show all the startups", "Calmr, ZeroPact and Angiex."),
        TurnRecord(2, "which of these mention regulatory risk?",
                   "BMI OrganBank and Bits Lifestyle mention it."))
    diagnostic = assert_set_carryover(r, 1, 2)
    assert not diagnostic.passed and diagnostic.detail["carried"] == 0


def test_no_new_entities_flags_an_intruder_but_allows_the_user_to_widen():
    r = records(
        TurnRecord(1, "show the startups", "Calmr and ZeroPact."),
        TurnRecord(2, "and their quotes?", "Calmr, ZeroPact and Fabricorp."))
    assert not assert_no_new_entities(r, 1, 2).passed

    widened = records(
        TurnRecord(1, "show the startups", "Calmr and ZeroPact."),
        TurnRecord(2, "what about Angiex?", "Angiex is similar to Calmr."))
    assert assert_no_new_entities(widened, 1, 2).passed


def test_quotes_must_come_from_evidence_seen_since_the_set_was_established():
    r = records(
        TurnRecord(1, "which mention regulatory risk?", "Calmr does.",
                   evidence=["Calmr faces significant regulatory risk in the EU."]),
        TurnRecord(2, "pull the exact quotes", 'Calmr: "significant regulatory '
                                               'risk in the EU".'))
    assert assert_quotes_from_turn(r, 1, 2).passed

    invented = records(
        r[1],
        TurnRecord(2, "pull the exact quotes",
                   'Calmr: "insurmountable regulatory barriers in the EU".'))
    diagnostic = assert_quotes_from_turn(invented, 1, 2)
    assert not diagnostic.passed and diagnostic.detail["unfound"]


def test_coverage_claim_is_graded_against_the_bundle_not_the_claim():
    """The real LFT turn: "Did you search all 20 startups?" answered "yes, all
    20" when the searching turn put 8 documents in front of the model."""
    r = records(
        TurnRecord(1, "which mention regulatory risk?", "Three of them do.",
                   searched_documents=8),
        TurnRecord(2, "Did you search all 20 startups?",
                   "Yes — I searched all 20 startups.", searched_documents=0))
    diagnostic = assert_coverage_claim_grounded(r, 2)
    assert not diagnostic.passed
    assert diagnostic.detail["searched_documents"] == 8
    assert 20 in diagnostic.detail["overclaimed"]


def test_an_honest_coverage_answer_passes():
    r = records(
        TurnRecord(1, "which mention regulatory risk?", "Three of them do.",
                   searched_documents=25),
        TurnRecord(2, "Did you search all 20 startups?",
                   "No — I searched 8 documents across 3 startups; that is the "
                   "scope I can account for.", searched_documents=8))
    assert assert_coverage_claim_grounded(r, 2).passed


def test_evaluate_cross_turn_runs_every_declared_assertion():
    script = Script(
        id="v2s001", cls="coverage_challenge", tier="core", provenance="LFT",
        bindings={}, binding_mode="substitute", readiness=READY,
        turns=(Turn(1, "q1", Grade({}, ())), Turn(2, "q2", Grade({}, ()))),
        cross_turn=({"type": "set_carryover", "from_turn": 1, "to_turn": 2},
                    {"type": "coverage_claim_grounded", "turn": 2}))
    r = records(
        TurnRecord(1, "q1", "Calmr and Angiex.", searched_documents=9),
        TurnRecord(2, "q2", "Calmr mentions it; I searched 9 documents.",
                   searched_documents=9))
    results = evaluate_cross_turn(script, r)
    assert [d.check for d in results] == ["set_carryover",
                                          "coverage_claim_grounded"]
    assert all(d.passed for d in results)
