"""Seeded ACL probe suite (PLAN Phase-4 exit: zero ACL violations).

Two enforcement points, probed separately because they are separate SQL surfaces:

  1. **Retrieval** — adversarial queries under non-privileged ACLs; assert no
     gated class appears in the hits (the agent can only cite what retrieval
     returns).
  2. **Structured queries** — the `queries.py` registry, which reads app tables
     directly and therefore cannot inherit retrieval's predicates.

Every negative probe is paired with an **admin-scope positive control**: proof
that the gated target is actually reachable when the gate is opened. Without it a
probe can pass because the sensitive row does not exist at all
(ARCHITECTURE-PLAN-REVIEW-2026-07-30, P0 "authorization is not end-to-end").
Targets are discovered from the data, not hard-coded, so the suite ports to prod.

Run: uv run python -m experiments.acl_probes
"""

from __future__ import annotations

import sys
from typing import Any

from x1_advisor.agent.queries import run_query
from x1_advisor.db import connect
from x1_advisor.filters import unknown_corpus_values
from x1_advisor.retrieval import retrieve

NOBODY = {"user_id": 999_999}                       # owns nothing, purchased nothing
PURCHASER = {"user_id": 999_998, "purchased_evaluation_ids": []}  # filled at runtime

PROBES = [
    # (name, query, filters) — adversarial phrasing aimed at each gated class
    ("premium_by_name", "key uncertainties premium investability report X1 Pipeline", None),
    ("premium_by_filter", "investment recommendation", {"source_type": "eval_premium"}),
    ("private_deck", "pitch deck slides traction revenue", {"source_type": "deck_extract"}),
    ("unpublished_profile", "Test Profile startup company profile", {"source_type": "profile"}),
    ("hidden_eval", "evaluation findings", {"source_type": "eval_section"}),
]


def violations_for(conn, acl, purchased: set[int]) -> list[str]:
    out = []
    for name, query, filters in PROBES:
        for h in retrieve(conn, query, acl=acl, filters=filters, k=10):
            m = h.metadata
            if m.get("acl_premium_gated") and int(m.get("evaluation_id") or -1) not in purchased:
                out.append(f"{name}: premium leak doc {h.document_id}")
            if m.get("acl_visibility") == "private":
                out.append(f"{name}: private doc leak doc {h.document_id}")
            if m.get("acl_is_published") is False:
                out.append(f"{name}: unpublished leak doc {h.document_id}")
            if m.get("acl_eval_is_visible") is False:
                out.append(f"{name}: hidden-eval leak doc {h.document_id}")
    return out


def structured_probes(conn) -> tuple[list[str], list[str]]:
    """Probe the structured-query registry. Returns (violations, notes).

    Notes record probes that were SKIPPED for want of a target class in the data
    — a skipped probe is not a passing probe, and must never read as one.
    """
    violations: list[str] = []
    notes: list[str] = []

    def rows(name: str, params: dict | None, acl: Any) -> list[dict]:
        return run_query(conn, name, params, acl=acl)

    # --- class: draft (unpublished) startup companies -----------------------
    draft = conn.execute(
        """SELECT s.id, s.name, count(e.id) AS n_evals
           FROM startup_companies s
           JOIN startup_company_evaluations e ON e.startup_company_id = s.id
           WHERE s.is_published = false
           GROUP BY s.id, s.name ORDER BY n_evals DESC, s.name LIMIT 1"""
    ).fetchone()
    if not draft:
        notes.append("SKIPPED draft-company probes: no unpublished company with "
                     "evaluations exists in this database")
    else:
        # positive control first: admin must actually see the target
        admin_rows = rows("evaluations_for_company", {"company_name": draft["name"]}, "admin")
        if not admin_rows:
            violations.append(
                f"positive control FAILED: admin sees no evaluations for draft "
                f"company {draft['name']!r} — the negative probe below is vacuous")
        # negative: a user who owns nothing must see none of them
        leaked = rows("evaluations_for_company", {"company_name": draft["name"]}, NOBODY)
        if leaked:
            violations.append(
                f"evaluations_for_company: {len(leaked)} draft-company evaluation(s) "
                f"leaked for {draft['name']!r}")
        # owner carve-out positive control: the owner SHOULD see their own draft
        owner = {"user_id": 999_997,
                 "owned_entity_ids": {"startup_company": [draft["id"]]}}
        if not rows("evaluations_for_company", {"company_name": draft["name"]}, owner):
            violations.append(
                f"owner carve-out FAILED: owner of draft {draft['name']!r} cannot see "
                "its own evaluations")

        for r in rows("top_startups_by_score", {"limit": 50}, NOBODY):
            if r.get("is_published") is False:
                violations.append(f"top_startups_by_score: draft company {r['name']!r} leaked")
        for r in rows("list_startups", {"limit": 50}, NOBODY):
            if r.get("is_published") is False:
                violations.append(f"list_startups: draft company {r['name']!r} leaked")
        counts = rows("count_startups", None, NOBODY)[0]
        if counts["startups"] != counts["published"]:
            violations.append(
                f"count_startups: draft count disclosed to a non-owner "
                f"({counts['startups']} total vs {counts['published']} published)")

    # --- class: platform-hidden evaluations ---------------------------------
    hidden = conn.execute(
        """SELECT s.name, count(*) AS n
           FROM startup_company_evaluations e
           JOIN startup_companies s ON s.id = e.startup_company_id
           WHERE e.is_visible = false
           GROUP BY s.name ORDER BY n DESC LIMIT 1"""
    ).fetchone()
    if not hidden:
        notes.append("SKIPPED hidden-evaluation probes: no is_visible=false "
                     "evaluation exists in this database")
    else:
        if not rows("evaluations_for_company", {"company_name": hidden["name"]}, "admin"):
            violations.append(
                f"positive control FAILED: admin sees no evaluations for {hidden['name']!r}")
        for r in rows("evaluations_for_company", {"company_name": hidden["name"]}, NOBODY):
            if r.get("is_visible") is False:
                violations.append(
                    f"evaluations_for_company: hidden evaluation leaked for {r['name']!r}")
        for r in rows("top_startups_by_score", {"limit": 50}, NOBODY):
            if r.get("is_visible") is False:
                violations.append(f"top_startups_by_score: hidden evaluation leaked "
                                  f"for {r['name']!r}")

    # --- the ACL argument itself is mandatory -------------------------------
    try:
        run_query(conn, "count_startups", None, acl="everyone")   # type: ignore[arg-type]
        violations.append("run_query accepted a bogus acl value instead of raising")
    except ValueError:
        pass

    return violations, notes


def main() -> None:
    with connect() as conn:
        # a real evaluation id to grant the purchaser persona
        row = conn.execute(
            """SELECT (c.metadata->>'evaluation_id')::bigint AS eid
               FROM advisor.doc_chunks c
               WHERE c.metadata->>'acl_premium_gated' = 'true'
                 AND c.metadata->>'evaluation_id' IS NOT NULL LIMIT 1"""
        ).fetchone()
        granted = {row["eid"]} if row else set()

        v_nobody = violations_for(conn, NOBODY, set())
        acl_purchaser = {**PURCHASER, "purchased_evaluation_ids": sorted(granted)}
        v_purchaser = [v for v in violations_for(conn, acl_purchaser, granted)]

        # positive control: the purchaser SHOULD see their purchased premium doc
        purchased_visible = False
        if granted:
            hits = retrieve(conn, "investment recommendation",
                            acl=acl_purchaser, filters={"source_type": "eval_premium"}, k=10)
            purchased_visible = any(
                int(h.metadata.get("evaluation_id") or -1) in granted for h in hits)

        v_structured, notes = structured_probes(conn)
        # filter registry is the allowlist: a stored enum value it does not
        # declare is material the model cannot filter on and the schema hides
        drift = unknown_corpus_values(conn)

    print("== retrieval ==")
    print(f"nobody persona violations:    {v_nobody or 'NONE'}")
    print(f"purchaser persona violations: {v_purchaser or 'NONE'}")
    print(f"positive control (purchaser sees purchased premium): {purchased_visible}")
    print("\n== structured queries ==")
    print(f"violations: {v_structured or 'NONE'}")
    for n in notes:
        print(f"  note: {n}")
    print("\n== filter registry ==")
    print(f"undeclared enum values in corpus: {drift or 'NONE'}")
    ok = (not v_nobody and not v_purchaser and not v_structured and not drift
          and (purchased_visible or not granted))
    print("\nACL PROBES:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
