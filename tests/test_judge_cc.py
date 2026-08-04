"""The cc judge backend is graded on the same contract as the OpenAI one:
identical verdict schema, identical label semantics computed in Python, never
a fabricated verdict, and nothing body-carrying reaches a manifest."""

import json

from x1_advisor.agent import judge as judge_mod
from x1_advisor.agent.bundle import judge_manifest_projection
from x1_advisor.agent.judge_cc import entail_cc, judge_bundle_cc


def _bundle():
    return {
        "schema_version": 3,
        "request": {"question": "What does the CV say about Angiex?"},
        "evidence": [
            {"ref": "ref1", "kind": "chunk", "title": "Brian Clark — CV",
             "document_id": "10", "block_index": "2",
             "snapshot": "Head of Manufacturing at Angiex. CMC leader for two "
                         "FDA-approved drugs."},
            {"ref": "ref2", "kind": "chunk", "title": "Angiex — Startup Profile",
             "document_id": "11", "block_index": "0",
             "snapshot": "Angiex is a biotech company developing tumor "
                         "endothelial marker therapies."},
        ],
        "validation": {
            "answer": "Brian Clark led CMC for two FDA-approved drugs [ref1]. "
                      "Angiex develops therapies [ref2]. The corpus holds no "
                      "churn data. I searched profiles and CVs.",
            "citations": [
                {"n": 1, "ref": "ref1", "type": "internal"},
                {"n": 2, "ref": "ref2", "type": "internal"},
            ],
        },
    }


def _cli(result_obj):
    return {"result": json.dumps(result_obj),
            "modelUsage": {"claude-opus-5": {
                "inputTokens": 10, "outputTokens": 5,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                "canonicalModel": "claude-opus-5", "costUSD": 0.001}}}


def _inventory():
    return {"claims": [
        {"text": "Brian Clark led CMC for two FDA-approved drugs",
         "is_factual": True, "citation_numbers": [1],
         "verdict": "supported", "reason": "stated in the CV"},
        {"text": "Angiex develops therapies", "is_factual": True,
         "citation_numbers": [2], "verdict": "partial",
         "reason": "therapy area is narrower than claimed"},
        {"text": "The corpus holds no churn data", "is_factual": True,
         "citation_numbers": [], "verdict": None, "reason": None},
        {"text": "I searched profiles and CVs", "is_factual": False,
         "citation_numbers": [], "verdict": None, "reason": None},
        {"text": "A claim citing a dead ref", "is_factual": True,
         "citation_numbers": [9], "verdict": "supported",
         "reason": "should be overridden to unverifiable"},
    ]}


def test_cc_verdict_schema_and_labels():
    verdict = judge_bundle_cc(None, _bundle(),
                              _transport=lambda p, **kw: _cli(_inventory()))
    assert verdict["judge_model"] == "cc:claude-opus-5"
    assert verdict["judge_backend"] == "cc"
    assert verdict["evidence_provenance"] == "turn-snapshot"
    assert verdict["claims"] == {"total": 5, "factual": 4, "cited": 3,
                                 "uncited": 1}
    assert verdict["counts"] == {"supported": 1, "partial": 1,
                                 "unsupported": 0, "unverifiable": 1}
    # partial -> synthesis_error; uncited factual -> citation_coverage_error;
    # dead citation -> unverifiable_citation. Same composition as the OpenAI
    # path, computed in Python, never by the model.
    assert verdict["labels"] == ["citation_coverage_error", "synthesis_error",
                                 "unverifiable_citation"]
    assert verdict["scores"]["faithfulness"] == 0.5
    assert verdict["scores"]["citation_coverage"] == 0.75
    # the process disclosure was classified non-factual: it appears nowhere
    assert all("searched" not in v["claim"] for v in verdict["verdicts"])
    assert "I searched profiles and CVs" not in verdict["uncited_claims"]


def test_cc_dead_citation_is_unverifiable_not_trusted():
    verdict = judge_bundle_cc(None, _bundle(),
                              _transport=lambda p, **kw: _cli(_inventory()))
    dead = [v for v in verdict["verdicts"] if v["citations"] == [9]]
    assert dead and dead[0]["verdict"] == "unverifiable"


def test_cc_parse_retry_then_success():
    calls = []

    def transport(prompt, **kw):
        calls.append(prompt)
        if len(calls) == 1:
            return {"result": "sorry, here is my analysis..."}
        return _cli(_inventory())

    verdict = judge_bundle_cc(None, _bundle(), _transport=transport)
    assert verdict is not None and len(calls) == 2
    assert "failed to parse" in calls[1]


def test_cc_unparseable_twice_is_ungraded_never_guessed():
    verdict = judge_bundle_cc(None, _bundle(),
                              _transport=lambda p, **kw: {"result": "nope"})
    assert verdict is None


def _inventory_unjudged():
    """A live-cited factual claim the judge inventoried but never judged —
    the v2c028 shape (2934f7a run): coercing it to 'unverifiable' label-failed
    a case whose every judged claim was supported."""
    inv = _inventory()
    inv["claims"][0] = {**inv["claims"][0], "verdict": None, "reason": None}
    return inv


def test_cc_unjudged_live_claim_retries_then_uses_complete_verdicts():
    calls = []

    def transport(prompt, **kw):
        calls.append(prompt)
        return _cli(_inventory_unjudged() if len(calls) == 1 else _inventory())

    verdict = judge_bundle_cc(None, _bundle(), _transport=transport)
    assert verdict is not None and len(calls) == 2
    assert "without a verdict" in calls[1]
    assert "Brian Clark led CMC" in calls[1]         # names the gap
    by_claim = {v["claim"]: v["verdict"] for v in verdict["verdicts"]}
    assert by_claim["Brian Clark led CMC for two FDA-approved drugs"] == "supported"
    # the only unverifiable left is the genuinely dead ref
    assert verdict["counts"]["unverifiable"] == 1


def test_cc_unjudged_twice_is_ungraded_never_coerced():
    verdict = judge_bundle_cc(
        None, _bundle(),
        _transport=lambda p, **kw: _cli(_inventory_unjudged()))
    assert verdict is None


def test_cc_dead_ref_null_verdict_is_unverifiable_without_retry():
    """A claim whose every citation points at a dead ref was shown NO evidence
    — a null verdict there is the honest output, not an incomplete judgment."""
    inv = _inventory()
    inv["claims"][4] = {**inv["claims"][4], "verdict": None, "reason": None}
    calls = []

    def transport(prompt, **kw):
        calls.append(prompt)
        return _cli(inv)

    verdict = judge_bundle_cc(None, _bundle(), _transport=transport)
    assert verdict is not None and len(calls) == 1   # no retry
    assert verdict["counts"]["unverifiable"] == 1
    assert "unverifiable_citation" in verdict["labels"]


def test_cc_prompt_carries_titles_and_uncited_evidence():
    """The two informational fixes from the 2026-08-04 audit: titles present,
    and evidence the agent saw but did not cite still shown to the judge."""
    seen = {}

    def transport(prompt, **kw):
        seen["prompt"] = prompt
        return _cli({"claims": []})

    bundle = _bundle()
    bundle["evidence"].append({"ref": "ref3", "kind": "chunk",
                               "title": "Angiex — Market Opportunity",
                               "document_id": "12", "block_index": "1",
                               "snapshot": "At $9,500 per procedure."})
    judge_bundle_cc(None, bundle, _transport=transport)
    assert "Brian Clark — CV" in seen["prompt"]
    assert "shown, uncited" in seen["prompt"]
    assert "$9,500" in seen["prompt"]


def test_cc_projection_is_body_free():
    verdict = judge_bundle_cc(None, _bundle(),
                              _transport=lambda p, **kw: _cli(_inventory()))
    projection = json.dumps(judge_manifest_projection(verdict))
    assert "Brian Clark" not in projection      # no claim text
    assert "stated in the CV" not in projection  # no reasons
    assert "cc:claude-opus-5" in projection


def test_cc_backend_dispatch(monkeypatch):
    sentinel = {"labels": []}
    monkeypatch.setattr(judge_mod, "JUDGE_BACKEND", "cc")
    monkeypatch.setattr("x1_advisor.agent.judge_cc.judge_bundle_cc",
                        lambda conn, bundle, **kw: sentinel)
    assert judge_mod.judge_bundle(None, _bundle()) is sentinel


def test_entail_cc_verdict_and_fallback():
    ok = entail_cc("claim", "source", _transport=lambda p, **kw: {
        "result": '{"verdict": "partial", "reason": "half supported"}'})
    assert ok == ("partial", "half supported")
    bad = entail_cc("claim", "source",
                    _transport=lambda p, **kw: {"result": "not json"})
    assert bad[0] == "unverifiable"
