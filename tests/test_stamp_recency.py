"""Unit tests: eval_recency classification (thread-029 design, David-approved).

The taxonomy is the contract: exactly one `current` per company; deck recency
dominates eval recency; unknowables land in `undetermined`, never guessed.

Run: uv run pytest -q tests/test_stamp_recency.py
"""

from __future__ import annotations

from x1_advisor.ingest.stamp_recency import classify

D = {"deckA": 1, "deckB": 2}          # deckB uploaded after deckA


def _e(eid, date, deck=None):
    return {"eval_id": eid, "date": date, "deck_key": deck}


def test_single_eval_is_current():
    assert classify([_e("1", 10, "deckA")], D) == {"1": "current"}
    assert classify([_e("1", 10)], D) == {"1": "current"}   # unlinked too


def test_repeat_and_prior_deck():
    out = classify([_e("1", 10, "deckA"), _e("2", 20, "deckB"),
                    _e("3", 30, "deckB")], D)
    assert out == {"1": "prior_deck", "2": "repeat_current_deck",
                   "3": "current"}


def test_deck_recency_dominates_eval_recency():
    # the OLD deck was re-evaluated after the new deck's eval: the new
    # deck's latest eval is still current (David's rule)
    out = classify([_e("1", 10, "deckB"), _e("2", 20, "deckA")], D)
    assert out == {"1": "current", "2": "prior_deck"}


def test_no_linkage_at_all_uses_date_order():
    out = classify([_e("1", 10), _e("2", 20), _e("3", 30)], D)
    assert out == {"1": "undetermined", "2": "undetermined", "3": "current"}


def test_newest_unlinked_eval_is_current_and_others_undetermined():
    # its deck is unknown, so no other eval's deck can be compared to it
    out = classify([_e("1", 10, "deckA"), _e("2", 30)], D)
    assert out == {"1": "undetermined", "2": "current"}


def test_older_unlinked_eval_is_undetermined_not_guessed():
    out = classify([_e("1", 10), _e("2", 20, "deckA")], D)
    assert out == {"1": "undetermined", "2": "current"}


def test_deck_not_on_record_ranks_by_its_newest_eval():
    # deckX has no upload date: it ranks by its newest evaluation, which is
    # newer than deckA's — so deckX is the current deck
    out = classify([_e("1", 10, "deckA"), _e("2", 30, "deckX")], D)
    assert out == {"1": "prior_deck", "2": "current"}


def test_exactly_one_current_per_company():
    evals = [_e(str(i), i, "deckB" if i % 2 else "deckA") for i in range(8)]
    out = classify(evals, D)
    assert sum(1 for v in out.values() if v == "current") == 1
