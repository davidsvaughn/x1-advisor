"""Unit tests: the coverage/aggregate registry queries (bank §1.4/§1.5/§3.3).

These queries unblocked the `registry_query`/`coverage_query` cases, so what
they must pin is the CONTRACT the cases grade against:

  - documents_for_company is the coverage surface: indexed corpus docs with
    per-requester searchable/gated status, plus uploads that are files on
    record, never presented as searchable text;
  - premium gating mirrors the retriever's chunk predicate — existence is
    visible, text access is what gates;
  - relationship/count queries return honest empties and honest scopes under
    the same class ACL the retriever applies (never identity walls);
  - only the overall X1 score is aggregatable — the stats query must not
    invent per-dimension aggregates.

Run: uv run pytest -q tests/test_registry_queries.py
"""

from __future__ import annotations

import pytest

from x1_advisor.agent import queries


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class StubConn:
    """Routes documents_for_company's two statements by table name and
    records every (sql, params) pair for predicate assertions."""

    def __init__(self, indexed_rows=(), upload_rows=(), rows=()):
        self.indexed_rows = list(indexed_rows)
        self.upload_rows = list(upload_rows)
        self.rows = list(rows)
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "advisor.documents" in sql:
            return _Result(self.indexed_rows)
        if "startup_company_documents" in sql:
            return _Result(self.upload_rows)
        return _Result(self.rows)


INDEXED_ROW = {"title": "Calmr — Evaluation: Market", "source_type": "eval_section",
               "version": 1, "visibility": "x1", "is_published": True,
               "env": "prod", "blocks": 12, "open_blocks": 12}
UPLOAD_ROW = {"document_type": "Pitch Deck", "file_name": "calmr_deck.pdf",
              "visibility": "private", "uploaded": "2026-05-01"}


# --- the shared class predicate -------------------------------------------


def test_owner_published_acl_is_one_predicate_across_profile_tables():
    # the company helper is the same class predicate, not a divergent copy
    assert queries._company_acl({"user_id": 1}) == (" AND s.is_published", [])
    assert queries._owner_published_acl({"user_id": 1}, "i", "investor") == (
        " AND i.is_published", [])
    sql, args = queries._owner_published_acl(
        {"owned_entity_ids": {"cv": [7]}}, "v", "cv")
    assert sql == " AND (v.is_published OR v.id = ANY(%s))" and args == [[7]]
    assert queries._owner_published_acl("admin", "v", "cv") == ("", [])


# --- documents_for_company (the §3.3 coverage surface) ---------------------


def test_documents_for_company_requires_company_name():
    with pytest.raises(ValueError):
        queries.run_query(StubConn(), "documents_for_company", {}, acl="admin")


def test_documents_for_company_admin_combines_indexed_and_uploads():
    conn = StubConn(indexed_rows=[INDEXED_ROW], upload_rows=[UPLOAD_ROW])
    rows = queries.run_query(conn, "documents_for_company",
                             {"company_name": "Calmr"}, acl="admin")

    indexed, upload = rows
    assert indexed["kind"] == "indexed" and indexed["searchable"] is True
    assert "gated" not in indexed                 # nothing is gated for admin
    assert indexed["source_type"] == "eval_section" and indexed["blocks"] == 12
    assert upload == {"kind": "upload", "document_type": "Pitch Deck",
                      "file_name": "calmr_deck.pdf", "visibility": "private",
                      "uploaded": "2026-05-01"}

    doc_sql, _ = conn.calls[0]
    # corpus membership is the title-name convention (spans fixture envs),
    # never an app-table id join; superseded versions are excluded like scan.py
    assert "split_part(d.title, ' — ', 1) ILIKE" in doc_sql
    assert "superseded_by IS NULL" in doc_sql
    # admin path carries no ACL fragments (d.visibility still rides as a
    # SELECTed gating column — the row shape shows what the gate decided)
    assert "d.visibility <>" not in doc_sql and "acl_premium_gated" not in doc_sql


def test_documents_for_company_non_admin_gates_premium_but_shows_existence():
    gated_row = dict(INDEXED_ROW, source_type="eval_premium", open_blocks=0)
    private_upload = dict(UPLOAD_ROW)
    conn = StubConn(indexed_rows=[gated_row], upload_rows=[private_upload])
    rows = queries.run_query(conn, "documents_for_company",
                             {"company_name": "Calmr"}, acl={"user_id": 9})

    (indexed, upload) = rows
    # the doc RIDES BACK — coverage never hides existence — but it is marked
    assert indexed["gated"] is True and indexed["searchable"] is False

    doc_sql, _ = conn.calls[0]
    up_sql, _ = conn.calls[1]
    # doc-level classes mirror retrieval._acl_sql; premium is the chunk predicate
    assert "d.visibility <> 'private'" in doc_sql
    assert "d.is_published" in doc_sql
    assert "COALESCE(d.eval_is_visible, true)" in doc_sql
    assert "acl_premium_gated" in doc_sql
    # private uploads are excluded for non-owners
    assert "sd.visibility <> 'private'" in up_sql


def test_documents_for_company_purchase_carve_out_reaches_the_sql():
    conn = StubConn(indexed_rows=[], upload_rows=[])
    queries.run_query(conn, "documents_for_company", {"company_name": "Calmr"},
                      acl={"user_id": 9, "purchased_evaluation_ids": [11]})
    doc_sql, doc_params = conn.calls[0]
    assert "evaluation_id" in doc_sql
    assert [11] in list(doc_params)


# --- aggregates / relationships -------------------------------------------


def test_evaluation_score_stats_aggregates_overall_score_only():
    row = {"evaluations": 79, "evaluated_startups": 75,
           "avg_overall_score": 67.9, "min_overall_score": 41,
           "max_overall_score": 85}
    conn = StubConn(rows=[row])
    out = queries.run_query(conn, "evaluation_score_stats", None, acl="admin")
    assert out == [row]
    sql, _ = conn.calls[0]
    assert "avg(e.overall_score)" in sql
    # the honest scope is IN the description: only the overall score is
    # structured data — no per-dimension aggregate is offered or implied
    desc = queries.QUERIES["evaluation_score_stats"]["description"]
    assert "overall" in desc and "dimension" in desc


def test_investors_for_company_empty_is_an_honest_empty_not_an_error():
    conn = StubConn(rows=[])
    out = queries.run_query(conn, "investors_for_company",
                            {"company_name": "Calmr"}, acl={"user_id": 9})
    assert out == []
    sql, _ = conn.calls[0]
    assert "investor_startup_matches" in sql
    assert "i.is_published" in sql          # investor drafts are owner-only


def test_count_cvs_counts_under_cv_acl():
    conn = StubConn(rows=[{"cvs": 45, "open_to_work": 13, "published": 45}])
    out = queries.run_query(conn, "count_cvs", None, acl={"user_id": 9})
    assert out[0]["open_to_work"] == 13
    sql, _ = conn.calls[0]
    assert "v.is_published" in sql          # unpublished CVs are owner-only


# --- label resolver (bank §1.4 classification questions) --------------------


def test_label_resolver_rejects_unknown_label_type():
    with pytest.raises(ValueError):
        queries.run_query(StubConn(), "startups_by_label",
                          {"label_type": "mood", "value": "x"}, acl="admin")
    with pytest.raises(ValueError):
        queries.run_query(StubConn(), "list_labels", {}, acl="admin")


def test_startups_by_label_requires_value():
    with pytest.raises(ValueError):
        queries.run_query(StubConn(), "startups_by_label",
                          {"label_type": "sector"}, acl="admin")


def test_startups_by_label_matches_all_levels_under_company_acl():
    row = {"name": "BMI OrganBank", "slug": "bmi", "is_published": True,
           "matched_labels": ["Biotechnology"]}
    conn = StubConn(rows=[row])
    out = queries.run_query(conn, "startups_by_label",
                            {"label_type": "sector", "value": "biotech"},
                            acl={"user_id": 9})
    assert out == [row]
    sql, params = conn.calls[0]
    # substring match at every hierarchy level, not just the display label
    assert "l.label ILIKE" in sql and "l.detail ILIKE" in sql
    assert "s.is_published" in sql            # drafts stay owner-only
    # a null or non-array json column contributes nothing instead of erroring
    assert "jsonb_typeof" in sql
    assert params[0] == "biotech"


def test_list_labels_counts_only_visible_companies():
    conn = StubConn(rows=[{"label": "FinTech", "startups": 7}])
    out = queries.run_query(conn, "list_labels", {"label_type": "industry"},
                            acl={"user_id": 9})
    assert out[0]["startups"] == 7
    sql, _ = conn.calls[0]
    assert "count(DISTINCT l.startup_company_id)" in sql
    assert "s.is_published" in sql


def test_region_labels_come_from_the_pivot_registry():
    conn = StubConn(rows=[])
    queries.run_query(conn, "startups_by_label",
                      {"label_type": "region", "value": "europe"}, acl="admin")
    sql, _ = conn.calls[0]
    assert "startup_company_regions" in sql and "JOIN regions" in sql


def test_new_queries_are_in_the_catalog():
    cat = queries.catalog()
    for name in ("documents_for_company", "evaluation_score_stats",
                 "investors_for_company", "count_cvs",
                 "startups_by_label", "list_labels"):
        assert name in cat
