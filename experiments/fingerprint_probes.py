"""Fingerprint probes — does the corpus watermark actually move? (Gate 1A)

A fingerprint that misses a change is worse than no fingerprint: it labels two
different behaviors with the same identity. The failure the review named is the
**in-place** one — a re-embed of existing chunk_ids, or an ACL/metadata
correction — which changes retrieval while every count and max-id stays put.

Each probe mutates inside a transaction and rolls back, so the corpus is
untouched. Run: uv run python -m experiments.fingerprint_probes
"""

from __future__ import annotations

import sys

from x1_advisor import fingerprint as fp
from x1_advisor.db import connect
from x1_advisor.index import active_config


def main() -> None:
    failures: list[str] = []
    with connect() as conn:
        cfg = active_config(conn).id
        base = fp.corpus_watermark(conn, cfg)

        # cache hit: an unchanged corpus must return the identical watermark
        if fp.corpus_watermark(conn, cfg) != base:
            failures.append("watermark is not stable across calls on an unchanged corpus")

        cases = [
            ("chunk metadata correction",
             "UPDATE advisor.doc_chunks SET metadata = metadata || '{\"probe\": 1}'::jsonb "
             "WHERE id = (SELECT min(id) FROM advisor.doc_chunks)",
             "chunk_digest"),
            ("chunk text edit",
             "UPDATE advisor.doc_chunks SET text = text || ' probe' "
             "WHERE id = (SELECT min(id) FROM advisor.doc_chunks)",
             "chunk_digest"),
            ("in-place re-embed",
             f"UPDATE advisor.emb_{cfg} SET embedding = "
             f"(SELECT embedding FROM advisor.emb_{cfg} ORDER BY chunk_id DESC LIMIT 1) "
             f"WHERE chunk_id = (SELECT min(chunk_id) FROM advisor.emb_{cfg})",
             "embedding_digest"),
        ]
        for label, sql, expected_field in cases:
            conn.execute(sql)
            after = fp.corpus_watermark(conn, cfg)
            conn.rollback()
            if after == base:
                failures.append(f"{label}: watermark did NOT move")
            elif after[expected_field] == base[expected_field]:
                failures.append(
                    f"{label}: watermark moved but {expected_field} did not "
                    f"(moved: {[k for k in base if after[k] != base[k]]})")
            # rollback must restore the previous identity exactly
            if fp.corpus_watermark(conn, cfg) != base:
                failures.append(f"{label}: watermark did not return to baseline after rollback")

    print("failures:", failures or "NONE")
    print("\nFINGERPRINT PROBES:", "PASS" if not failures else "FAIL")
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
