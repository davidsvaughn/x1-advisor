"""Unit tests: analyze_scope helpers (ANALYZE-SCOPE-DESIGN, built 2026-08-14).

The map reply parser and the injectable-ask map/reduce are the testable
core; scope resolution rides retrieval's ACL predicate (covered by its own
tests) and the OpenAI path is exercised live.

Run: uv run pytest -q tests/test_analyze_scope.py
"""

from __future__ import annotations

import json

from x1_advisor.agent.analyze import _parse_map_reply, analyze


def test_parse_validates_supports_against_shown_blocks():
    out = _parse_map_reply(
        'noise {"relevant": true, "findings": "weak positioning", '
        '"supports": [2, 7, "9", -1]} trailing', {2, 9})
    assert out["relevant"] is True
    assert out["supports"] == [2, 9]          # 7 and -1 were never shown


def test_parse_never_raises_and_unparseable_is_not_a_finding():
    assert _parse_map_reply("total garbage", {1}) == {
        "relevant": False, "findings": "", "supports": []}
    # "relevant" without findings text is not a finding either
    out = _parse_map_reply('{"relevant": true, "findings": ""}', {1})
    assert out["relevant"] is False


class StubConn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        class R:
            def __init__(self, rows): self._r = rows
            def fetchall(self): return self._r
        return R(self.rows)


def test_map_reduce_with_injected_model():
    rows = [
        {"document_id": 1, "entity_id": 10, "title": "A — Eval",
         "source_type": "eval_basic", "evaluation_id": "1",
         "block_index": 0, "text": "brand is weak"},
        {"document_id": 2, "entity_id": 11, "title": "B — Eval",
         "source_type": "eval_basic", "evaluation_id": "2",
         "block_index": 3, "text": "all good"},
    ]

    def fake_ask(prompt):
        if "Synthesize" in prompt:
            return "one company flags brand weakness"
        if "A — Eval" in prompt:
            return json.dumps({"relevant": True,
                               "findings": "brand weakness", "supports": [0]})
        return json.dumps({"relevant": False})

    out = analyze(StubConn(rows), question="who flags brand weakness?",
                  entity_type="startup_company", source_types=["eval_basic"],
                  acl="admin", tracker=None, ask=fake_ask)
    assert out["coverage"]["docs_read"] == 2
    assert out["coverage"]["relevant_documents"] == 1
    assert out["findings"][0]["supports"] == [0]
    assert "brand weakness" in out["reduction"]


def test_empty_scope_and_cap_are_visible_errors():
    out = analyze(StubConn([]), question="q", entity_type="startup_company",
                  source_types=["eval_basic"], acl="admin", tracker=None,
                  ask=lambda p: "")
    assert out["error"].startswith("no readable documents")


def test_frontier_mode_reads_in_rank_order_and_stops_when_dry():
    # 130 docs (> FULL_READ_CAP): only the first 3 in embedding-rank order
    # are relevant; the frontier must stop after STOP_AFTER_IRRELEVANT
    # consecutive misses instead of reading all 130
    rows = []
    for i in range(130):
        rows.append({"document_id": i, "entity_id": i, "title": f"D{i}",
                     "source_type": "eval_basic", "evaluation_id": str(i),
                     "block_index": 0, "text": "text"})

    def fake_ranker(conn, doc_ids, question, tracker):
        return sorted(doc_ids)                 # 0,1,2 first

    def fake_ask(prompt):
        if "Synthesize" in prompt:
            return "three docs relevant"
        import re
        n = int(re.search(r"DOCUMENT: D(\d+)", prompt).group(1))
        if n < 3:
            return '{"relevant": true, "findings": "hit", "supports": [0]}'
        return '{"relevant": false}'

    out = analyze(StubConn(rows), question="q", entity_type="startup_company",
                  source_types=["eval_basic"], acl="admin", tracker=None,
                  ask=fake_ask, ranker=fake_ranker)
    cov = out["coverage"]
    assert cov["mode"] == "embedding_ranked_frontier"
    assert cov["relevant_documents"] == 3
    assert cov["docs_read"] < 40               # stopped early, not 130
    assert cov["docs_unread"] == 130 - cov["docs_read"]
    assert "consecutive irrelevant" in cov["stopping_rule"]


def test_tool_wrapper_body_imports_and_runs(monkeypatch):
    # regression: the wrapper's imports live in the function BODY, so a
    # stale name passes every build-time test and kills every live call
    # (2026-08-14: MAX_DOCS rename broke turn 80). Invoke the real wrapper.
    import x1_advisor.agent.analyze as analyze_mod
    from x1_advisor.agent.evidence import EvidenceRegistry
    from x1_advisor.agent.tools import build_tools

    monkeypatch.setattr(analyze_mod, "analyze", lambda *a, **k: {
        "coverage": {"docs_read": 1, "docs_in_scope": 1, "entities_read": 1,
                     "evaluations_read": 1, "relevant_evaluations": 1,
                     "redundant_renderings_skipped": 0,
                     "relevant_documents": 1, "eval_recency": "current",
                     "mode": "full_read"},
        "findings": [{"document_id": 1, "entity_id": 1, "evaluation_id": "5",
                      "title": "T", "findings": "f", "supports": [0]}],
        "reduction": "r", "model": "m"})
    reg = EvidenceRegistry()
    tools = build_tools(None, acl="admin", registry=reg, tracker=None)
    fn = next(t for t in tools if t.name == "analyze_scope").function
    import json as _json
    out = _json.loads(fn(question="q", source_types=["eval_premium"]))
    assert out["coverage"]["eval_recency_defaulted"] is True
    assert out["findings"][0]["refs"]          # support became a citable ref
    assert len(reg) == 2                       # coverage ref + 1 chunk ref


def test_canonical_read_skips_sections_and_basic_when_premium_present():
    rows = [
        {"document_id": 1, "entity_id": 10, "title": "A — Premium",
         "source_type": "eval_premium", "evaluation_id": "5",
         "block_index": 0, "text": "full report"},
        {"document_id": 2, "entity_id": 10, "title": "A — Basic",
         "source_type": "eval_basic", "evaluation_id": "5",
         "block_index": 0, "text": "excerpt"},
        {"document_id": 3, "entity_id": 10, "title": "A — Team section",
         "source_type": "eval_section", "evaluation_id": "5",
         "block_index": 0, "text": "verbatim subset"},
        # a different eval WITHOUT premium: its section survives
        {"document_id": 4, "entity_id": 10, "title": "A — Old section",
         "source_type": "eval_section", "evaluation_id": "6",
         "block_index": 0, "text": "no premium twin"},
    ]
    from x1_advisor.agent.analyze import resolve_scope
    docs = resolve_scope(StubConn(rows), entity_type="startup_company",
                         source_types=["eval_premium", "eval_basic",
                                       "eval_section"], acl="admin")
    assert [d["document_id"] for d in docs] == [1, 4]
    assert docs[0]["redundant_renderings_skipped"] == 2
