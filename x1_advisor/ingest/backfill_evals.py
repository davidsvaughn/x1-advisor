"""Eval-bundle backfill: GCS bundles → advisor.documents/doc_chunks. Zero LLM cost.

Two sources, both ending in the same store call:

  DB mode (default)   — walk `startup_company_evaluations.raw_json` pointers on the
                        connected DB (app tables read-only). Pointers come in two
                        forms: relative object paths (`reports/x.json`) and full
                        `gs://` URIs. Experimental-shape blobs (test-only, see
                        bundles.py) are counted and skipped loudly.
  --fixtures mode     — ingest original-shape prod bundles copied (2026-07-08) to
                        `gs://{bucket}/reports/prod_fixtures/`. These have no eval
                        row and reference prod startup ids, so they're stored with
                        entity_id NULL + metadata {entity_ref_env: 'prod',
                        prod_startup_company_id: N} — realistic content for
                        retrieval dev without polluting app-table joins.

Run:  uv run python -m x1_advisor.ingest.backfill_evals [--fixtures] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

from google.cloud import storage

from x1_advisor.db import connect
from x1_advisor.ingest.bundles import (
    UnsupportedBundleShape,
    parse_bundle,
    startup_company_id,
)
from x1_advisor.ingest.store import upsert_document

BUCKET = os.environ.get("GCS_BUCKET", "x1-app-www-test")
FIXTURES_PREFIX = "reports/prod_fixtures/"


def _blob_path(pointer: str) -> tuple[str, str]:
    """raw_json pointer → (bucket, object_path). Handles both pointer forms."""
    p = pointer.strip().strip('"')
    if p.startswith("gs://"):
        bucket, _, path = p[5:].partition("/")
        return bucket, path
    return BUCKET, p


def _resolve_deck_visibility(conn, doc) -> None:
    """Max-restrictive inheritance for deck extracts: default 'private', upgraded
    only when the source startup_company_documents row is found and says so."""
    if doc.source_type != "deck_extract":
        return
    doc_id = doc.metadata.get("deck_document_id")
    gcs_path = doc.metadata.get("deck_gcs_path")
    row = None
    with conn.cursor() as cur:
        if doc_id:
            cur.execute(
                "SELECT visibility FROM startup_company_documents WHERE id = %s",
                (int(doc_id),),
            )
            row = cur.fetchone()
        if row is None and gcs_path:
            cur.execute(
                "SELECT visibility FROM startup_company_documents WHERE file_path = %s",
                (str(gcs_path).removeprefix(f"gs://{BUCKET}/"),),
            )
            row = cur.fetchone()
    if row:
        doc.visibility = row["visibility"]
        doc.metadata["deck_visibility_resolved"] = True


def ingest_bundle(conn, client, bucket_name, path, *, entity_id, is_published,
                  eval_is_visible, extra_meta, stats,
                  fallback_company_name=None) -> None:
    raw = client.bucket(bucket_name).blob(path).download_as_bytes()
    bundle = json.loads(raw)
    source_ref = f"gs://{bucket_name}/{path}"
    docs = parse_bundle(bundle, source_ref,
                        fallback_company_name=fallback_company_name)
    for doc in docs:
        doc.metadata.update(extra_meta)
        if entity_id is not None:
            _resolve_deck_visibility(conn, doc)
        _, action = upsert_document(
            conn, doc,
            # entity_type is the KIND of thing the document is about; entity_id
            # is the link to a local row. Conflating them left every bundle we
            # could not link locally with entity_type NULL, which silently
            # excluded it from any search filtering on entity_type — 75% of the
            # corpus (Gate 1B).
            entity_type="startup_company",
            entity_id=entity_id, source_ref=source_ref,
            is_published=is_published, eval_is_visible=eval_is_visible,
        )
        stats[f"{doc.source_type}:{action}"] += 1
    conn.commit()


def run_db_mode(conn, client, limit, stats) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.id, e.startup_company_id, e.raw_json, e.is_visible,
                      s.is_published, s.name
               FROM startup_company_evaluations e
               JOIN startup_companies s ON s.id = e.startup_company_id
               WHERE e.raw_json IS NOT NULL
               ORDER BY e.id""",
        )
        rows = cur.fetchall()
    if limit:
        rows = rows[:limit]
    for row in rows:
        bucket_name, path = _blob_path(row["raw_json"])
        try:
            ingest_bundle(
                conn, client, bucket_name, path,
                entity_id=row["startup_company_id"],
                is_published=row["is_published"],
                eval_is_visible=row["is_visible"],
                extra_meta={"evaluation_id": row["id"]},
                stats=stats,
                # legacy prod bundles carry no internal company name — the
                # joined app-table name is the fallback (629 docs shipped as
                # "Unknown company" without it; triage thread-021)
                fallback_company_name=row["name"],
            )
        except UnsupportedBundleShape:
            stats["skipped:experimental_shape"] += 1
            conn.rollback()
        except Exception as exc:  # noqa: BLE001 — record and continue; don't wedge the sweep
            stats[f"error:{type(exc).__name__}"] += 1
            print(f"  ERROR eval {row['id']} ({path}): {exc}", file=sys.stderr)
            conn.rollback()


def run_fixtures_mode(conn, client, limit, stats) -> None:
    blobs = [b for b in client.list_blobs(BUCKET, prefix=FIXTURES_PREFIX)
             if b.name.endswith(".json")]
    if limit:
        blobs = blobs[:limit]
    for blob in blobs:
        try:
            raw = blob.download_as_bytes()
            bundle = json.loads(raw)
            prod_id = startup_company_id(bundle)
            source_ref = f"gs://{BUCKET}/{blob.name}"
            # gen-0a bundles carry no company name — fall back to the filename slug
            slug = re.split(r"_[0-9a-f]{8}-", blob.name.rsplit("/", 1)[-1])[0]
            for doc in parse_bundle(bundle, source_ref, fallback_company_name=slug):
                doc.metadata.update(
                    {"entity_ref_env": "prod", "prod_startup_company_id": prod_id}
                )
                _, action = upsert_document(
                    conn, doc,
                    # a prod fixture is a startup evaluation; only its local
                    # entity id is unknown (the prod row has no test twin)
                    entity_type="startup_company", entity_id=None,
                    source_ref=source_ref, is_published=True, eval_is_visible=True,
                )
                stats[f"{doc.source_type}:{action}"] += 1
            conn.commit()
        except UnsupportedBundleShape:
            stats["skipped:experimental_shape"] += 1
            conn.rollback()
        except Exception as exc:  # noqa: BLE001
            stats[f"error:{type(exc).__name__}"] += 1
            print(f"  ERROR fixture {blob.name}: {exc}", file=sys.stderr)
            conn.rollback()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixtures", action="store_true",
                    help=f"ingest gs://{BUCKET}/{FIXTURES_PREFIX} instead of DB pointers")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    stats: Counter[str] = Counter()
    client = storage.Client()
    with connect() as conn:
        if args.fixtures:
            run_fixtures_mode(conn, client, args.limit, stats)
        else:
            run_db_mode(conn, client, args.limit, stats)

    print("\nbackfill summary:")
    for key, n in sorted(stats.items()):
        print(f"  {key:40s} {n}")
    errors = sum(n for k, n in stats.items() if k.startswith("error:"))
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
