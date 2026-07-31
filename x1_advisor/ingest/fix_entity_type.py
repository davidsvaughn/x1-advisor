"""Backfill `entity_type` on bundle-derived documents (Gate 1B).

`backfill_evals` set `entity_type = "startup_company" if entity_id is not None
else None`, conflating the KIND of thing a document is about with the LINK to a
local row. Every prod-fixture bundle — the prod entity has no test twin, so
`entity_id` is NULL by design — therefore landed with `entity_type` NULL.

Consequence: **75% of corpus chunks (5,731 of 7,693) carry no entity_type**, so
any `search_corpus(filters={"entity_type": "startup_company"})` silently
excluded almost every evaluation, deck and website document. Gate 1A's retrieval
explain caught it: the agent asked for ArtCentrica's team section under that
filter and got back only X1 Pipeline, because ArtCentrica's evaluation documents
were invisible to the filter.

This only rewrites metadata — no re-chunking, no re-embedding — and only for
documents whose source is an evaluation bundle. It is idempotent, and it aligns
existing rows with what the fixed ingester now writes, so a later re-ingest
matches on identity instead of version-and-appending duplicates.

ACL is unaffected: the owner carve-out matches on the pair
`(entity_type, entity_id)`, and `entity_id` stays NULL, so no row that was
gated becomes visible.

Run:  uv run python -m x1_advisor.ingest.fix_entity_type [--apply]
Without --apply it reports what would change and writes nothing.
"""

from __future__ import annotations

import argparse

from x1_advisor.db import connect

BUNDLE_SOURCE_TYPES = ("eval_section", "eval_premium", "eval_basic",
                       "deck_extract", "website")

SELECT_TARGETS = f"""
    SELECT id FROM advisor.documents
     WHERE entity_type IS NULL
       AND source_ref LIKE 'gs://%%/reports/%%'
       AND source_type IN {BUNDLE_SOURCE_TYPES}
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the change (default: report only)")
    args = ap.parse_args()

    with connect() as conn:
        targets = [r["id"] for r in conn.execute(SELECT_TARGETS).fetchall()]
        chunks = conn.execute(
            "SELECT count(*) AS n FROM advisor.doc_chunks WHERE document_id = ANY(%s)",
            (targets,)).fetchone()["n"]
        # anything NULL that this migration does NOT claim must be reported, not
        # silently left behind
        skipped = conn.execute(
            "SELECT source_type, count(*) AS n FROM advisor.documents "
            "WHERE entity_type IS NULL AND NOT (id = ANY(%s)) GROUP BY 1",
            (targets,)).fetchall()

        print(f"documents to update: {len(targets)}  (chunks: {chunks})")
        if skipped:
            print("NULL entity_type left untouched (not bundle-derived): "
                  + ", ".join(f"{r['source_type']}={r['n']}" for r in skipped))
        if not args.apply:
            print("\ndry run — pass --apply to write")
            return

        conn.execute(
            "UPDATE advisor.documents SET entity_type = 'startup_company', "
            "updated_at = now() WHERE id = ANY(%s)", (targets,))
        conn.execute(
            "UPDATE advisor.doc_chunks "
            "SET metadata = metadata || '{\"entity_type\": \"startup_company\"}'::jsonb "
            "WHERE document_id = ANY(%s)", (targets,))
        conn.commit()

        remaining = conn.execute(
            "SELECT count(*) AS n FROM advisor.doc_chunks c "
            "JOIN advisor.documents d ON d.id = c.document_id "
            "WHERE d.superseded_by IS NULL AND c.metadata->>'entity_type' IS NULL"
        ).fetchone()["n"]
        print(f"applied. chunks still missing entity_type: {remaining}")


if __name__ == "__main__":
    main()
