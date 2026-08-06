"""Unit tests: the adjudication replay harness (experiments/adjudicate_calibrate.py).

Pinned properties:

  - labels join to bodies by id; a label with no local body is EXCLUDED and
    reported, never silently graded against nothing;
  - the label↔gate vocabulary maps both ways on every gate (adequate/
    inadequate, faithful/unfaithful, asserted/not_asserted, credited/
    uncredited);
  - a must-fail item the gate judges lenient is a leniency-ratchet breach;
    a must-fail item the gate upholds is not;
  - a faithfulness label whose claim no longer exists in the pointed bundle
    dies loudly (broken pointer ≠ re-grade different text);
  - synthetic items replay from their inline mini-bundle, no run bundle
    needed;
  - the TRACKED labels file stays body-free: no answers, snapshots, claims,
    entities or inline bundles ever land in git.

Run: uv run pytest -q tests/test_adjudicate_calibrate.py
"""

from __future__ import annotations

import json

import pytest

from experiments import adjudicate_calibrate as ac
from experiments.adjudicate_calibrate import (Result, replay,
                                              replay_faithfulness,
                                              replay_names)


def _transport_returning(*payloads):
    seq = list(payloads)

    def run(prompt, *, tracker=None, stage=""):
        out = seq.pop(0) if len(seq) > 1 else seq[0]
        return {"result": json.dumps(out),
                "modelUsage": {"claude-opus-5": {"inputTokens": 1,
                                                 "outputTokens": 1}}}
    return run


_INLINE = {"request": {"question": "Q?"},
           "validation": {"answer": "**H.** Body. [1]",
                          "citations": [{"ref": "s1", "n": 1,
                                         "type": "internal"}]},
           "evidence": [{"ref": "s1", "kind": "chunk", "title": "T",
                         "document_id": 1, "block_index": 0,
                         "snapshot": "body facts"}]}


def _cit_item(id_, claim, label, stratum=""):
    return ({"id": id_, "gate": "citation_coverage", "synthetic": True,
             "claim": claim, "inline": _INLINE},
            {"id": id_, "gate": "citation_coverage", "label": label,
             "stratum": stratum, "note": id_})


# --- join and exclusion ----------------------------------------------------


def test_label_without_body_is_excluded_and_reported():
    item, label = _cit_item("c1", "H.", "adequate")
    orphan = {"id": "ghost", "gate": "citation_coverage",
              "label": "adequate", "stratum": "", "note": ""}
    ok = {"verdicts": [{"id": 1, "adequate": True, "reason": "cited body"}]}
    results, missing = replay([label, orphan], [item],
                              _transport=_transport_returning(ok))
    assert missing == ["ghost"]
    assert [r.id for r in results] == ["c1"]


# --- vocabulary mapping ----------------------------------------------------


def test_citation_vocabulary_maps_both_directions():
    i1, l1 = _cit_item("c1", "H one.", "adequate")
    i2, l2 = _cit_item("c2", "H two.", "inadequate")
    one = {"verdicts": [{"id": 1, "adequate": True, "reason": "ok"}]}
    bad = {"verdicts": [{"id": 1, "adequate": False, "reason": "theater"}]}
    r1, _ = replay([l1], [i1], _transport=_transport_returning(one))
    r2, _ = replay([l2], [i2], _transport=_transport_returning(bad))
    assert (r1[0].expected, r1[0].got, r1[0].agree) == ("adequate", "adequate", True)
    assert (r2[0].expected, r2[0].got, r2[0].agree) == ("inadequate", "inadequate", True)


def test_names_vocabulary_maps_sides():
    bundle = dict(_INLINE)
    items = [{"id": "n1", "gate": "asserted_names", "synthetic": True,
              "entity": "acme", "side": "overclaim_flagged", "inline": bundle,
              "_label": {"label": "not_asserted", "stratum": "", "note": ""}},
             {"id": "n2", "gate": "asserted_names", "synthetic": True,
              "entity": "zorg", "side": "miss_flagged", "inline": bundle,
              "_label": {"label": "credited", "stratum": "", "note": ""}}]
    ok = {"overclaim_flagged": [{"id": 1, "verdict": False, "reason": "excluded group"}],
          "miss_flagged": [{"id": 1, "verdict": True, "reason": "grouped credit"}]}
    out = replay_names(items, _transport=_transport_returning(ok))
    got = {it["id"]: g for it, g, _ in out}
    assert got == {"n1": "not_asserted", "n2": "credited"}


# --- the ratchet -----------------------------------------------------------


def test_must_fail_judged_lenient_is_a_breach():
    r = Result(id="syn1", gate="citation_coverage", expected="inadequate",
               got="adequate", votes=[True, True, True],
               stratum="must-fail", note="")
    assert r.leniency_breach


def test_must_fail_upheld_is_not_a_breach():
    r = Result(id="syn1", gate="citation_coverage", expected="inadequate",
               got="inadequate", votes=[False, False, False],
               stratum="must-fail", note="")
    assert not r.leniency_breach


def test_strict_disagreement_on_a_lenient_label_is_not_a_breach():
    r = Result(id="cit1", gate="citation_coverage", expected="adequate",
               got="inadequate", votes=[False, False, True],
               stratum="headline", note="")
    assert not r.agree and not r.leniency_breach


# --- faithfulness pointer discipline ---------------------------------------


def test_faithfulness_broken_pointer_dies_loudly(tmp_path, monkeypatch):
    bundle = {**_INLINE, "judge": {"verdicts": [
        {"claim": "different text", "verdict": "partial",
         "citations": [1], "reason": "r"}]}}
    p = tmp_path / "b.json"
    p.write_text(json.dumps(bundle))
    monkeypatch.setattr(ac, "REPO_ROOT", tmp_path)
    item = {"id": "f1", "gate": "faithfulness", "bundle": "b.json",
            "claim": "labeled claim not in bundle",
            "_label": {"label": "faithful", "stratum": "", "note": ""}}
    with pytest.raises(SystemExit):
        replay_faithfulness([item], _transport=_transport_returning({}))


def test_synthetic_faithfulness_replays_from_inline_bundle():
    item = {"id": "sf1", "gate": "faithfulness", "synthetic": True,
            "claim": "Most churned.", "cited": [1],
            "grader_reason": "one anecdote", "inline": _INLINE,
            "_label": {"label": "unfaithful", "stratum": "must-fail",
                       "note": ""}}
    bad = {"verdicts": [{"id": 1, "faithful": False,
                         "reason": "quantity beyond evidence"}]}
    out = replay_faithfulness([item], _transport=_transport_returning(bad))
    assert out[0][1] == "unfaithful"


# --- the tracked file stays body-free --------------------------------------


def test_committed_labels_file_carries_no_bodies():
    allowed = {"id", "gate", "provenance", "ratified", "label", "stratum",
               "note"}
    for line in ac.LABELS_PATH.read_text().splitlines():
        rec = json.loads(line)
        assert set(rec) <= allowed, f"{rec['id']}: unexpected keys {set(rec) - allowed}"
        assert rec["label"] in {"adequate", "inadequate", "faithful",
                                "unfaithful", "asserted", "not_asserted",
                                "credited", "uncredited",
                                # s5 gates
                                "disclosed", "undisclosed",
                                "overclaim", "not_overclaim",
                                "grounded", "ungrounded"}
