"""Structured-query registry (PLAN Phase 4): named, parameterized, READ-ONLY SQL
over app tables — the tool for aggregate/list questions retrieval can't answer
(golden g023/g024 class). The registry is the only SQL surface exposed to the
model; there is no free-form SQL tool.
"""

from __future__ import annotations

from typing import Any

MAX_ROWS = 50


def _count_startups(conn, params: dict) -> list[dict]:
    return conn.execute(
        """SELECT count(*) AS startups,
                  count(*) FILTER (WHERE is_published) AS published
           FROM startup_companies"""
    ).fetchall()


def _count_evaluated_startups(conn, params: dict) -> list[dict]:
    return conn.execute(
        """SELECT count(DISTINCT startup_company_id) AS evaluated_startups,
                  count(*) AS evaluations
           FROM startup_company_evaluations WHERE is_visible"""
    ).fetchall()


def _list_startups(conn, params: dict) -> list[dict]:
    round_f = params.get("fundraising_round")
    sql = """SELECT name, slug, fundraising_round, fundraising_status,
                    headquarters_location
             FROM startup_companies WHERE is_published"""
    args: list = []
    if round_f:
        sql += " AND lower(fundraising_round) = lower(%s)"
        args.append(round_f)
    sql += " ORDER BY name LIMIT %s"
    args.append(min(int(params.get("limit", MAX_ROWS)), MAX_ROWS))
    return conn.execute(sql, args).fetchall()


def _top_startups_by_score(conn, params: dict) -> list[dict]:
    return conn.execute(
        """SELECT s.name, s.slug, e.overall_score, e.evaluation_date::date AS date
           FROM startup_company_evaluations e
           JOIN startup_companies s ON s.id = e.startup_company_id
           WHERE e.is_visible
           ORDER BY e.overall_score DESC, e.evaluation_date DESC
           LIMIT %s""",
        (min(int(params.get("limit", 10)), MAX_ROWS),),
    ).fetchall()


def _evaluations_for_company(conn, params: dict) -> list[dict]:
    return conn.execute(
        """SELECT s.name, e.overall_score, e.evaluation_date::date AS date, e.is_visible
           FROM startup_company_evaluations e
           JOIN startup_companies s ON s.id = e.startup_company_id
           WHERE s.name ILIKE '%%' || %s || '%%'
           ORDER BY e.evaluation_date DESC LIMIT %s""",
        (params.get("company_name", ""), MAX_ROWS),
    ).fetchall()


QUERIES: dict[str, dict[str, Any]] = {
    "count_startups": {
        "fn": _count_startups, "params": {},
        "description": "Total and published startup counts on the platform.",
    },
    "count_evaluated_startups": {
        "fn": _count_evaluated_startups, "params": {},
        "description": "How many startups have visible X1 evaluations (and total evaluations).",
    },
    "list_startups": {
        "fn": _list_startups,
        "params": {"fundraising_round": "optional, e.g. Seed",
                   "limit": f"optional, max {MAX_ROWS}"},
        "description": "List published startups (name, round, status, HQ), optionally filtered by fundraising round.",
    },
    "top_startups_by_score": {
        "fn": _top_startups_by_score, "params": {"limit": "optional, default 10"},
        "description": "Highest-scoring visible X1 evaluations with company names and dates.",
    },
    "evaluations_for_company": {
        "fn": _evaluations_for_company,
        "params": {"company_name": "required substring match"},
        "description": "Evaluation history (scores + dates) for a named company.",
    },
}


def catalog() -> str:
    return "; ".join(f"{name}({', '.join(q['params']) or 'no params'}) — {q['description']}"
                     for name, q in QUERIES.items())


def run_query(conn, name: str, params: dict | None) -> list[dict]:
    if name not in QUERIES:
        raise KeyError(f"unknown structured query {name!r}; available: {sorted(QUERIES)}")
    rows = QUERIES[name]["fn"](conn, params or {})
    return [dict(r) for r in rows[:MAX_ROWS]]
