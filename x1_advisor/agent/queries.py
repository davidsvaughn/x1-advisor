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


def _owner_published_acl(acl: Any, alias: str, entity_type: str) -> tuple[str, list]:
    """Draft (unpublished) profiles are owner-only — one class predicate for
    every published/owned profile table (startups, investors, CVs), not a
    per-table re-derivation."""
    _check_acl(acl)
    if acl == "admin":
        return "", []
    owned = [int(i) for i in
             ((acl.get("owned_entity_ids") or {}).get(entity_type) or [])]
    if owned:
        return f" AND ({alias}.is_published OR {alias}.id = ANY(%s))", [owned]
    return f" AND {alias}.is_published", []


def _company_acl(acl: Any, alias: str = "s") -> tuple[str, list]:
    """Draft (unpublished) startup profiles are owner-only."""
    return _owner_published_acl(acl, alias, "startup_company")


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


# --- label resolver (bank §1.4 classification questions) --------------------
# One reusable resolver across the platform's label vocabularies (standing
# registry-resolver rule: never a one-off industry mapper). Each source yields
# (startup_company_id, label, detail) rows; matching is a substring ILIKE at
# any hierarchy level. JSON columns are guarded with jsonb_typeof so a null or
# non-array value contributes nothing instead of erroring the query.
LABEL_SOURCES: dict[str, str] = {
    "industry": """SELECT s.id AS startup_company_id,
                          ind->>'category' AS label, ind->>'subcategory' AS detail
                   FROM startup_companies s
                   CROSS JOIN LATERAL jsonb_array_elements(
                       CASE WHEN jsonb_typeof(s.industries::jsonb) = 'array'
                            THEN s.industries::jsonb ELSE '[]'::jsonb END) ind""",
    "sector": """SELECT s.id AS startup_company_id, sec.value AS label,
                        NULL::text AS detail
                 FROM startup_companies s
                 CROSS JOIN LATERAL jsonb_array_elements_text(
                     CASE WHEN jsonb_typeof(s.target_sectors::jsonb) = 'array'
                          THEN s.target_sectors::jsonb ELSE '[]'::jsonb END
                  || CASE WHEN jsonb_typeof(s.custom_target_sectors::jsonb) = 'array'
                          THEN s.custom_target_sectors::jsonb ELSE '[]'::jsonb END
                     ) sec(value)""",
    "region": """SELECT scr.startup_company_id, r.name AS label,
                        r.description AS detail
                 FROM startup_company_regions scr
                 JOIN regions r ON r.id = scr.region_id""",
}


def _label_source(params: dict) -> str:
    lt = str(params.get("label_type") or "").strip().lower()
    if lt not in LABEL_SOURCES:
        raise ValueError(
            f"label_type must be one of {sorted(LABEL_SOURCES)}, got {lt!r}")
    return LABEL_SOURCES[lt]


def _startups_by_label(conn, params: dict, acl: Any) -> list[dict]:
    src = _label_source(params)
    value = str(params.get("value") or "").strip()
    if not value:
        raise ValueError("value is required (a substring of the label)")
    c_sql, c_args = _company_acl(acl)
    return conn.execute(
        f"""WITH labels AS ({src})
            SELECT s.name, s.slug, s.is_published,
                   array_agg(DISTINCT l.label) AS matched_labels
            FROM labels l
            JOIN startup_companies s ON s.id = l.startup_company_id
            WHERE (l.label ILIKE '%%' || %s || '%%'
                   OR l.detail ILIKE '%%' || %s || '%%'){c_sql}
            GROUP BY s.id, s.name, s.slug, s.is_published
            ORDER BY s.name LIMIT %s""",
        (value, value, *c_args, _limit(params, MAX_ROWS)),
    ).fetchall()


def _list_labels(conn, params: dict, acl: Any) -> list[dict]:
    src = _label_source(params)
    c_sql, c_args = _company_acl(acl)
    return conn.execute(
        f"""WITH labels AS ({src})
            SELECT l.label, count(DISTINCT l.startup_company_id) AS startups
            FROM labels l
            JOIN startup_companies s ON s.id = l.startup_company_id
            WHERE l.label IS NOT NULL{c_sql}
            GROUP BY l.label
            ORDER BY startups DESC, l.label LIMIT %s""",
        (*c_args, _limit(params, MAX_ROWS)),
    ).fetchall()


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


def _doc_acl(acl: Any) -> tuple[str, list]:
    """Doc-level mirror of retrieval._acl_sql over advisor.documents: private
    docs and drafts are owner-only; platform-hidden evaluations are admin-only.
    Premium gating is chunk-level and handled by the caller."""
    _check_acl(acl)
    if acl == "admin":
        return "", []
    params: list = []
    owned = acl.get("owned_entity_ids") or {}
    owned_pairs = [(t, int(i)) for t, ids in owned.items() for i in ids]
    owned_clause = ""
    if owned_pairs:
        owned_clause = (" OR (d.entity_type, d.entity_id) IN ("
                        + ",".join(["(%s,%s)"] * len(owned_pairs)) + ")")
    sql = f" AND (d.visibility <> 'private'{owned_clause})"
    for t, i in owned_pairs:
        params.extend([t, i])
    sql += f" AND (d.is_published{owned_clause})"
    for t, i in owned_pairs:
        params.extend([t, i])
    sql += " AND COALESCE(d.eval_is_visible, true)"
    return sql, params


def _required_name(params: dict) -> str:
    name = str(params.get("company_name") or "").strip()
    if not name:
        raise ValueError("company_name is required (a substring of the company name)")
    return name


def _documents_for_company(conn, params: dict, acl: Any) -> list[dict]:
    """Coverage/inventory (bank §3.3): what exists for this company, and what
    of it is searchable text vs a file on record vs premium-gated."""
    name = _required_name(params)
    doc_sql, doc_args = _doc_acl(acl)
    # a doc is searchable for THIS requester if any of its source blocks
    # survives the same premium predicate the retriever applies
    if acl == "admin":
        open_filter = "c.granularity = 'block'"
        open_args: list = []
    else:
        prem = "COALESCE((c.metadata->>'acl_premium_gated')::boolean, false) = false"
        purchased = [int(i) for i in (acl.get("purchased_evaluation_ids") or [])]
        if purchased:
            prem = (f"({prem} OR (c.metadata->>'evaluation_id')::bigint = ANY(%s))")
            open_args = [purchased]
        else:
            open_args = []
        open_filter = f"c.granularity = 'block' AND {prem}"
    # the entity's name is the title's first ' — ' segment (scan.py convention)
    # — it spans fixture envs the app-table id join cannot see
    indexed = conn.execute(
        f"""SELECT d.title, d.source_type, d.version, d.visibility, d.is_published,
                   coalesce(max(c.metadata->>'entity_ref_env'), 'test') AS env,
                   count(c.id) FILTER (WHERE c.granularity = 'block') AS blocks,
                   count(c.id) FILTER (WHERE {open_filter}) AS open_blocks
            FROM advisor.documents d
            JOIN advisor.doc_chunks c ON c.document_id = d.id
            WHERE d.entity_type = 'startup_company'
              AND d.superseded_by IS NULL
              AND split_part(d.title, ' — ', 1) ILIKE '%%' || %s || '%%'{doc_sql}
            GROUP BY d.id
            ORDER BY d.source_type, d.title, d.version""",
        (*open_args, name, *doc_args),
    ).fetchall()

    c_sql, c_args = _company_acl(acl)
    up_sql, up_args = "", []
    if acl != "admin":
        owned = [int(i) for i in
                 ((acl.get("owned_entity_ids") or {}).get("startup_company") or [])]
        if owned:
            up_sql, up_args = " AND (sd.visibility <> 'private' OR s.id = ANY(%s))", [owned]
        else:
            up_sql = " AND sd.visibility <> 'private'"
    uploads = conn.execute(
        f"""SELECT sd.document_type, sd.file_name, sd.visibility,
                   sd.created_at::date AS uploaded
            FROM startup_company_documents sd
            JOIN startup_companies s ON s.id = sd.startup_company_id
            WHERE s.name ILIKE '%%' || %s || '%%'{c_sql}{up_sql}
            ORDER BY sd.document_type, sd.created_at""",
        (name, *c_args, *up_args),
    ).fetchall()

    out: list[dict] = []
    for r in indexed:
        row = {"kind": "indexed", "title": r["title"],
               "source_type": r["source_type"], "version": r["version"],
               "env": r["env"], "visibility": r["visibility"],
               "is_published": r["is_published"], "blocks": r["blocks"],
               "searchable": r["open_blocks"] > 0}
        if r["blocks"] and not r["open_blocks"]:
            row["gated"] = True     # exists; its text is premium-gated for you
        out.append(row)
    for r in uploads:
        out.append({"kind": "upload", "document_type": r["document_type"],
                    "file_name": r["file_name"], "visibility": r["visibility"],
                    "uploaded": r["uploaded"]})
    return out


def _evaluation_score_stats(conn, params: dict, acl: Any) -> list[dict]:
    e_sql, e_args = _eval_acl(acl)
    c_sql, c_args = _company_acl(acl)
    return conn.execute(
        f"""SELECT count(*) AS evaluations,
                   count(DISTINCT e.startup_company_id) AS evaluated_startups,
                   round(avg(e.overall_score), 1) AS avg_overall_score,
                   min(e.overall_score) AS min_overall_score,
                   max(e.overall_score) AS max_overall_score
            FROM startup_company_evaluations e
            JOIN startup_companies s ON s.id = e.startup_company_id
            WHERE true{e_sql}{c_sql}""",
        e_args + c_args,
    ).fetchall()


def _investors_for_company(conn, params: dict, acl: Any) -> list[dict]:
    name = _required_name(params)
    c_sql, c_args = _company_acl(acl)
    i_sql, i_args = _owner_published_acl(acl, "i", "investor")
    return conn.execute(
        f"""SELECT coalesce(i.display_name, i.slug) AS investor, i.investor_type,
                   m.match_score, m.is_active, i.is_published
            FROM investor_startup_matches m
            JOIN investors i ON i.id = m.investor_id
            JOIN startup_companies s ON s.id = m.startup_company_id
            WHERE s.name ILIKE '%%' || %s || '%%'{c_sql}{i_sql}
            ORDER BY m.match_score DESC NULLS LAST, investor
            LIMIT %s""",
        (name, *c_args, *i_args, MAX_ROWS),
    ).fetchall()


def _count_cvs(conn, params: dict, acl: Any) -> list[dict]:
    v_sql, v_args = _owner_published_acl(acl, "v", "cv")
    return conn.execute(
        f"""SELECT count(*) AS cvs,
                   count(*) FILTER (WHERE v.open_to_work) AS open_to_work,
                   count(*) FILTER (WHERE v.is_published) AS published
            FROM cvs v
            WHERE true{v_sql}""",
        v_args,
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
    "startups_by_label": {
        "fn": _startups_by_label,
        "params": {"label_type": "required: industry | sector | region",
                   "value": "required substring match",
                   "limit": f"optional, max {MAX_ROWS}"},
        "description": ("Startups visible to you carrying a platform label "
                        "matching value. label_type: 'industry' (LinkedIn-style "
                        "category, matched at any hierarchy level), 'sector' "
                        "(target sectors incl. custom), 'region'. Faster and "
                        "more authoritative than text-scanning for "
                        "classification questions; empty means no label "
                        "matches — the classification may still exist only "
                        "in document text."),
    },
    "list_labels": {
        "fn": _list_labels,
        "params": {"label_type": "required: industry | sector | region",
                   "limit": f"optional, max {MAX_ROWS}"},
        "description": ("The platform's label vocabulary of one type with "
                        "visible-startup counts — discover exact label values "
                        "before filtering with startups_by_label."),
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
    "documents_for_company": {
        "fn": _documents_for_company,
        "params": {"company_name": "required substring match"},
        "description": ("Document inventory visible to you for a named company: "
                        "indexed corpus documents (searchable text — eval "
                        "sections/basic/premium, profile, deck extract, website) "
                        "with per-document searchable/gated status, plus uploaded "
                        "files on record (pitch decks etc.), which are not "
                        "searchable text."),
    },
    "evaluation_score_stats": {
        "fn": _evaluation_score_stats, "params": {},
        "description": ("Avg/min/max overall X1 score and counts across "
                        "evaluations visible to you. Only the overall score is "
                        "structured data; dimension scores (market, team, …) "
                        "live in evaluation document text."),
    },
    "investors_for_company": {
        "fn": _investors_for_company,
        "params": {"company_name": "required substring match"},
        "description": ("Investor associations recorded for a named company in "
                        "the platform match registry (investor, type, match "
                        "score, active). Empty means none recorded."),
    },
    "count_cvs": {
        "fn": _count_cvs, "params": {},
        "description": "CV counts visible to you (total, open to work, published).",
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
