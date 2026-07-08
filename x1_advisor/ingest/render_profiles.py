"""Render entity profiles → advisor.documents (+ entity_profiles registry).

Run:  uv run python -m x1_advisor.ingest.render_profiles [--entity-type T] [--limit N]

Idempotent: unchanged content_hash → skip; changed → version-and-append (store.py).
The advisor.entity_profiles registry row always points at the live document.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from x1_advisor.db import connect
from x1_advisor.ingest.profiles import ENTITY_RENDERERS, make_profile_doc
from x1_advisor.ingest.store import upsert_document


def render_entity_type(conn, entity_type: str, limit: int | None,
                       stats: Counter) -> None:
    table, renderer = ENTITY_RENDERERS[entity_type]
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608 — table from fixed map
        rows = cur.fetchall()
    if limit:
        rows = rows[:limit]

    for row in rows:
        try:
            markdown, meta = renderer(conn, row)
            doc = make_profile_doc(entity_type, row, markdown, meta)
            is_published = bool(row.get("is_published", True))
            doc_id, action = upsert_document(
                conn, doc, entity_type=entity_type, entity_id=row["id"],
                source_ref=f"db://{entity_type}/{row['id']}",
                is_published=is_published,
            )
            conn.execute(
                """INSERT INTO advisor.entity_profiles
                     (entity_type, entity_id, document_id, content_hash, rendered_at)
                   VALUES (%s, %s, %s, %s, now())
                   ON CONFLICT (entity_type, entity_id) DO UPDATE
                     SET document_id = EXCLUDED.document_id,
                         content_hash = EXCLUDED.content_hash,
                         rendered_at = now()""",
                (entity_type, row["id"], doc_id, doc.content_hash),
            )
            conn.commit()
            stats[f"{entity_type}:{action}"] += 1
        except Exception as exc:  # noqa: BLE001 — record and continue; don't wedge the sweep
            stats[f"error:{entity_type}"] += 1
            print(f"  ERROR {entity_type} {row.get('id')}: {exc}", file=sys.stderr)
            conn.rollback()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--entity-type", choices=sorted(ENTITY_RENDERERS), default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    stats: Counter[str] = Counter()
    with connect() as conn:
        types = [args.entity_type] if args.entity_type else sorted(ENTITY_RENDERERS)
        for entity_type in types:
            render_entity_type(conn, entity_type, args.limit, stats)

    print("\nprofile render summary:")
    for key, n in sorted(stats.items()):
        print(f"  {key:40s} {n}")
    sys.exit(1 if any(k.startswith("error:") for k in stats) else 0)


if __name__ == "__main__":
    main()
