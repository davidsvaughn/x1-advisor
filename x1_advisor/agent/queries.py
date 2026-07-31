"""Structured-query registry (PLAN Phase 4): named, parameterized, READ-ONLY SQL
over app tables — the tool for aggregate/list questions retrieval can't answer
(golden g023/g024 class). The registry is the only SQL surface exposed to the
model; there is no free-form SQL tool.

ACL (PLAN §0.2 — MANDATORY, same contract as `retrieval.retrieve`): every query
takes the requester's ACL and applies the same class predicates the retriever
applies, so the two evidence paths cannot disagree about what is visible:

  - **draft/unpublished startup profiles are owner-only** — carve-out via
    `acl["owned_entity_ids"]["startup_company"]`;
  - **platform-hidden evaluations** (`is_visible = false`) are admin-only:
    hidden, not user-gated, so there is deliberately NO owner carve-out
    (mirrors `retrieval._acl_sql` and the gated-note suppression in `tools.py`).

Eval scores and dates for published companies are default-open cross-user
research material — never identity-walled. Nothing here is purchase-gated:
that class is premium report *full text*, which lives in the corpus, not in
this registry.

Every row shape carries its gating columns (`is_published`, `is_visible`) so a
replayed or archived result shows what the gate decided, not just what survived.
"""

from __future__ import annotations

from typing import Any

MAX_ROWS = 50


def _check_acl(acl: Any) -> None:
    if acl != "admin" and not isinstance(acl, dict):
        raise ValueError("acl must be 'admin' or a class-filter dict")


def _company_acl(acl: Any, alias: str = "s") -> tuple[str, list]:
    """Draft (unpublished) startup profiles are owner-only."""
    _check_acl(acl)
    if acl == "admin":
        return "", []
    owned = [int(i) for i in
             ((acl.get("owned_entity_ids") or {}).get("startup_company") or [])]
    if owned:
        return f" AND ({alias}.is_published OR {alias}.id = ANY(%s))", [owned]
    return f" AND {alias}.is_published", []


def _eval_acl(acl: Any, alias: str = "e") -> tuple[str, list]:
    """Platform-hidden evaluations stay hidden for everyone but admins."""
    _check_acl(acl)
    if acl == "admin":
        return "", []
    return f" AND {alias}.is_visible", []


def _limit(params: dict, default: int) -> int:
    """Bounded row cap. Bad input raises ValueError (surfaced as a tool error,
    never an uncaught 500) rather than silently falling back to a default."""
    raw = params.get("limit", default)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"limit must be an integer, got {raw!r}") from None
    if n < 1:
        raise ValueError(f"limit must be >= 1, got {n}")
    return min(n, MAX_ROWS)


def _count_startups(conn, params: dict, acl: Any) -> list[dict]:
    c_sql, c_args = _company_acl(acl)
    return conn.execute(
        f"""SELECT count(*) AS startups,
                   count(*) FILTER (WHERE s.is_published) AS published
            FROM startup_companies s
            WHERE true{c_sql}""",
        c_args,
    ).fetchall()


def _count_evaluated_startups(conn, params: dict, acl: Any) -> list[dict]:
    e_sql, e_args = _eval_acl(acl)
    c_sql, c_args = _company_acl(acl)
    return conn.execute(
        f"""SELECT count(DISTINCT e.startup_company_id) AS evaluated_startups,
                   count(*) AS evaluations
            FROM startup_company_evaluations e
            JOIN startup_companies s ON s.id = e.startup_company_id
            WHERE true{e_sql}{c_sql}""",
        e_args + c_args,
    ).fetchall()


def _list_startups(conn, params: dict, acl: Any) -> list[dict]:
    c_sql, c_args = _company_acl(acl)
    sql = f"""SELECT s.name, s.slug, s.fundraising_round, s.fundraising_status,
                     s.headquarters_location, s.is_published
              FROM startup_companies s
              WHERE true{c_sql}"""
    args: list = list(c_args)
    round_f = params.get("fundraising_round")
    if round_f:
        sql += " AND lower(s.fundraising_round) = lower(%s)"
        args.append(str(round_f))
    sql += " ORDER BY s.name LIMIT %s"
    args.append(_limit(params, MAX_ROWS))
    return conn.execute(sql, args).fetchall()


def _top_startups_by_score(conn, params: dict, acl: Any) -> list[dict]:
    e_sql, e_args = _eval_acl(acl)
    c_sql, c_args = _company_acl(acl)
    return conn.execute(
        f"""SELECT s.name, s.slug, e.overall_score, e.evaluation_date::date AS date,
                   e.is_visible, s.is_published
            FROM startup_company_evaluations e
            JOIN startup_companies s ON s.id = e.startup_company_id
            WHERE true{e_sql}{c_sql}
            ORDER BY e.overall_score DESC, e.evaluation_date DESC
            LIMIT %s""",
        (*e_args, *c_args, _limit(params, 10)),
    ).fetchall()


def _evaluations_for_company(conn, params: dict, acl: Any) -> list[dict]:
    name = str(params.get("company_name") or "").strip()
    if not name:
        raise ValueError("company_name is required (a substring of the company name)")
    e_sql, e_args = _eval_acl(acl)
    c_sql, c_args = _company_acl(acl)
    return conn.execute(
        f"""SELECT s.name, e.overall_score, e.evaluation_date::date AS date,
                   e.is_visible, s.is_published
            FROM startup_company_evaluations e
            JOIN startup_companies s ON s.id = e.startup_company_id
            WHERE s.name ILIKE '%%' || %s || '%%'{e_sql}{c_sql}
            ORDER BY e.evaluation_date DESC LIMIT %s""",
        (name, *e_args, *c_args, MAX_ROWS),
    ).fetchall()


QUERIES: dict[str, dict[str, Any]] = {
    "count_startups": {
        "fn": _count_startups, "params": {},
        "description": "Startup counts visible to you (total, published).",
    },
    "count_evaluated_startups": {
        "fn": _count_evaluated_startups, "params": {},
        "description": "How many startups have X1 evaluations visible to you (and total evaluations).",
    },
    "list_startups": {
        "fn": _list_startups,
        "params": {"fundraising_round": "optional, e.g. Seed",
                   "limit": f"optional, max {MAX_ROWS}"},
        "description": "List startups visible to you (name, round, status, HQ), optionally filtered by fundraising round.",
    },
    "top_startups_by_score": {
        "fn": _top_startups_by_score, "params": {"limit": "optional, default 10"},
        "description": "Highest-scoring X1 evaluations visible to you, with company names and dates.",
    },
    "evaluations_for_company": {
        "fn": _evaluations_for_company,
        "params": {"company_name": "required substring match"},
        "description": "Evaluation history (scores + dates) visible to you for a named company.",
    },
}


def catalog() -> str:
    return "; ".join(f"{name}({', '.join(q['params']) or 'no params'}) — {q['description']}"
                     for name, q in QUERIES.items())


def run_query(conn, name: str, params: dict | None, *, acl: Any) -> list[dict]:
    """Run a registry query under the requester's ACL.

    `acl` is required and has no default: a caller that forgets it must fail
    loudly rather than silently inherit admin reach.
    """
    if name not in QUERIES:
        raise KeyError(f"unknown structured query {name!r}; available: {sorted(QUERIES)}")
    _check_acl(acl)
    rows = QUERIES[name]["fn"](conn, params or {}, acl)
    return [dict(r) for r in rows[:MAX_ROWS]]
