"""Unit tests: the scan_text agent tool + the shared scan engine's ACL path.

The engine's admin-path aggregation (statuses, coverage denominators, digest
identity) is pinned by test_truth_and_checkers.py — those tests now run
through x1_advisor.scan via the truth builder's wrapper, which is itself the
refactor proof. This file pins what the TOOL adds on top:

  - record summaries are excluded structurally (granularity is not a knob);
  - every matched entity's NAME always rides back — display caps bound only
    excerpt bodies, are flagged, and never touch the counts;
  - matching excerpts are registered evidence whose snapshot is exactly the
    text shown to the model;
  - the scan itself is citable query-kind evidence (coverage claims need a ref);
  - a non-admin scan applies the retriever's ACL predicates and surfaces the
    premium-gated class as `restricted`, never as a false `no_match`;
  - validation failures are actionable errors naming the valid vocabulary.

Run: uv run pytest -q tests/test_scan_tool.py
"""

from __future__ import annotations

import json

from x1_advisor.agent.evidence import EvidenceRegistry
from x1_advisor.agent.tools import (
    SCAN_EXCERPT_ENTITY_CAP,
    SCAN_EXCERPTS_PER_ENTITY,
    _scan_excerpt,
    build_tools,
)

LONG_TEXT = ("The company operates in a complex environment. " * 10
             + "Auditors flagged material regulatory risk in the EU filing. "
             + "Mitigation is planned for next year. " * 10)

CHUNK_ROWS = [
    {"chunk_id": 1, "document_id": 10, "block_index": 0, "name": "Calmr",
     "env": "prod", "entity_id": None, "text": LONG_TEXT, "page_number": 3,
     "doc_title": "Calmr — Evaluation", "t0": True},
    {"chunk_id": 2, "document_id": 10, "block_index": 1, "name": "Calmr",
     "env": "prod", "entity_id": None, "text": "nothing here", "page_number": 4,
     "doc_title": "Calmr — Evaluation", "t0": False},
    {"chunk_id": 3, "document_id": 12, "block_index": 0, "name": "ZeroPact",
     "env": "test", "entity_id": "8", "text": "clean text", "page_number": None,
     "doc_title": "ZeroPact — Evaluation", "t0": False},
]
COMPANY_ROWS = [{"id": 8, "name": "ZeroPact"}, {"id": 9, "name": "Unindexed Co"}]


class StubConn:
    """Routes the engine's three query shapes and records every call.

    The main scan is the only query selecting `chunk_id`; the not-indexed
    backfill is the only one touching `startup_companies`; anything else over
    doc_chunks is the restricted-existence pass.
    """

    def __init__(self, chunk_rows=CHUNK_ROWS, company_rows=COMPANY_ROWS,
                 gated_rows=()):
        self.chunk_rows, self.company_rows = list(chunk_rows), list(company_rows)
        self.gated_rows = list(gated_rows)
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "chunk_id" in sql:
            rows = self.chunk_rows
        elif "startup_companies" in sql:
            rows = self.company_rows
        else:
            rows = self.gated_rows
        return _Result(rows)


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


def _scan_tool(conn, acl="admin", registry=None):
    registry = registry if registry is not None else EvidenceRegistry()
    tools = {t.name: t for t in build_tools(conn, acl=acl, registry=registry,
                                            tracker=None)}
    return tools["scan_text"].function, registry


# --- the tool payload ------------------------------------------------------


def test_scan_text_payload_matched_entities_and_citable_excerpts():
    conn = StubConn()
    scan_text, registry = _scan_tool(conn)
    out = json.loads(scan_text(phrases=["regulatory risk"]))

    assert out["counts"]["matched"] == 1
    assert out["counts"]["eligible"] == 3
    assert out["no_match"] == ["ZeroPact"]
    assert out["not_indexed"] == ["Unindexed Co"]

    (calmr,) = out["matched"]
    assert calmr["entity"] == "Calmr" and calmr["matched_chunks"] == 1
    (excerpt,) = calmr["excerpts"]
    assert "regulatory risk" in excerpt["excerpt"].lower()
    assert excerpt["title"] == "Calmr — Evaluation" and excerpt["page"] == 3
    # the ref resolves, and its snapshot is EXACTLY the text the model saw —
    # the judge must judge against what was shown, not the full block
    ev = registry.get(excerpt["ref"])
    assert ev.kind == "chunk" and (ev.document_id, ev.block_index) == (10, 0)
    assert ev.snapshot == excerpt["excerpt"]


def test_scan_text_registers_the_scan_as_citable_query_evidence():
    conn = StubConn()
    scan_text, registry = _scan_tool(conn)
    out = json.loads(scan_text(phrases=["regulatory risk"]))

    ev = registry.get(out["ref"])
    assert ev.kind == "query" and ev.query_name == "scan_text"
    assert ev.row_count == out["counts"]["eligible"]
    assert ev.query_params["phrases"] == ["regulatory risk"]
    assert ev.to_citation()["type"] == "platform_data"
    # the snapshot is the verbatim payload the model read
    assert json.loads(ev.snapshot) == out


def test_scan_text_scans_source_blocks_only_never_record_summaries():
    """Gate 1B structurally: granularity is not a model-facing knob."""
    conn = StubConn()
    scan_text, _ = _scan_tool(conn)
    scan_text(phrases=["x"])
    main_sql, main_params = conn.calls[0]
    assert "granularity" in main_sql
    assert ["block"] in list(main_params)


def test_scan_text_caps_are_flagged_and_counts_stay_exact():
    rows = [dict(CHUNK_ROWS[0], chunk_id=100 + i, block_index=i,
                 text=f"regulatory risk instance {i}") for i in range(4)]
    conn = StubConn(chunk_rows=rows, company_rows=[])
    scan_text, _ = _scan_tool(conn)
    out = json.loads(scan_text(phrases=["regulatory risk"]))

    (calmr,) = out["matched"]
    assert len(calmr["excerpts"]) == SCAN_EXCERPTS_PER_ENTITY
    assert calmr["more_matches"] == 4 - SCAN_EXCERPTS_PER_ENTITY
    assert calmr["matched_chunks"] == 4
    assert out["counts"]["matching_chunks"] == 4


def test_scan_text_entity_cap_never_drops_a_matched_name():
    n = SCAN_EXCERPT_ENTITY_CAP + 6
    rows = [{"chunk_id": i, "document_id": i, "block_index": 0,
             "name": f"Co{i:03d}", "env": "test", "entity_id": str(i),
             "text": "regulatory risk", "page_number": None,
             "doc_title": f"Co{i:03d} — Evaluation", "t0": True}
            for i in range(n)]
    conn = StubConn(chunk_rows=rows, company_rows=[])
    scan_text, _ = _scan_tool(conn)
    out = json.loads(scan_text(phrases=["regulatory risk"]))

    assert len(out["matched"]) == n                      # every name, always
    with_bodies = [m for m in out["matched"] if m["excerpts"]]
    assert len(with_bodies) == SCAN_EXCERPT_ENTITY_CAP
    assert "_excerpts_capped" in out
    assert out["counts"]["matched"] == n


# --- validation ------------------------------------------------------------


def test_scan_text_validation_is_actionable():
    conn = StubConn()
    scan_text, _ = _scan_tool(conn)

    assert "phrases" in json.loads(scan_text(phrases=[]))["error"]
    err = json.loads(scan_text(phrases=["x"], entity_type="bogus"))["error"]
    assert "startup_company" in err                      # names the valid set
    err = json.loads(scan_text(phrases=["x"], source_types=["bogus"]))["error"]
    assert "eval_section" in err
    assert "match" in json.loads(scan_text(phrases=["x"], match="regex"))["error"]
    # aliases resolve instead of erroring, like the search filter registry
    out = json.loads(scan_text(phrases=["x"], entity_type="startup"))
    assert out["scope"]["entity_type"] == "startup_company"
    # a bare string is treated as one phrase, not rejected
    out = json.loads(scan_text(phrases="regulatory risk"))
    assert out["scope"]["phrases"] == ["regulatory risk"]


# --- ACL -------------------------------------------------------------------


def test_non_admin_scan_applies_acl_and_surfaces_restricted():
    gated = [{"name": "GatedCo", "env": "prod", "entity_id": None},
             {"name": "Calmr", "env": "prod", "entity_id": None}]
    conn = StubConn(company_rows=[], gated_rows=gated)
    scan_text, _ = _scan_tool(conn, acl={})      # least-privileged requester
    out = json.loads(scan_text(phrases=["regulatory risk"]))

    # the visible scan carries the retriever's own ACL predicates
    main_sql, _ = conn.calls[0]
    assert "visibility" in main_sql and "is_published" in main_sql
    assert "acl_premium_gated" in main_sql

    # gated-only entity: restricted, never a false no_match/not_indexed
    assert out["restricted"] == ["GatedCo"]
    assert out["counts"]["restricted"] == 1
    assert "access_note" in out
    # entity that matched on visible text but also has unscanned gated text
    (calmr,) = out["matched"]
    assert calmr["entity"] == "Calmr" and calmr["gated_unscanned"] is True


def test_admin_scan_has_no_acl_predicates_and_no_restricted_key():
    """The truth-builder path must stay byte-identical (HANDOFF §3): no ACL
    fragment in the SQL, no `restricted` key in the counts."""
    conn = StubConn()
    scan_text, _ = _scan_tool(conn, acl="admin")
    out = json.loads(scan_text(phrases=["regulatory risk"]))
    main_sql, _ = conn.calls[0]
    assert "visibility" not in main_sql
    assert "restricted" not in out["counts"] and "restricted" not in out
    assert len(conn.calls) == 2                  # scan + not-indexed backfill


# --- excerpts --------------------------------------------------------------


def test_scan_excerpt_windows_the_first_hit_verbatim():
    excerpt = _scan_excerpt(LONG_TEXT, ["regulatory risk"])
    assert "regulatory risk" in excerpt
    assert excerpt.startswith("… ") and excerpt.endswith(" …")
    # verbatim source text: the windowed body must appear in the block as-is,
    # or quoted spans could not survive a verbatim check against the source
    assert excerpt.strip("… ") in LONG_TEXT

    head = _scan_excerpt("regulatory risk opens this short block.", ["regulatory risk"])
    assert head == "regulatory risk opens this short block."
