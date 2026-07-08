"""Seeded ACL probe suite (PLAN Phase-4 exit: zero ACL violations).

Probes retrieval directly (the enforcement point — the agent can only cite what
retrieval returns) with adversarial queries under non-privileged ACLs, then asserts
no gated class leaks. Run: uv run python -m experiments.acl_probes
"""

from __future__ import annotations

import sys

from x1_advisor.db import connect
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

    print(f"nobody persona violations:    {v_nobody or 'NONE'}")
    print(f"purchaser persona violations: {v_purchaser or 'NONE'}")
    print(f"positive control (purchaser sees purchased premium): {purchased_visible}")
    ok = not v_nobody and not v_purchaser and (purchased_visible or not granted)
    print("\nACL PROBES:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
