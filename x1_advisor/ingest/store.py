"""Write path for advisor.documents / advisor.doc_chunks.

Idempotency contract (PLAN Phase 1, "version-and-append"):
  - A document's identity is (source_type, entity_type, entity_id, source_ref,
    section_key) — the section key (from acl_source) discriminates the N
    eval_section documents that share one bundle's source_ref.
  - Unchanged content_hash → skip (no write).
  - Changed content_hash → insert a new row with version+1 and point the old row's
    superseded_by at it. Old chunks stay citation-resolvable; retrieval filters on
    superseded_by IS NULL.
  - Chunks are written at document-insert time with ACL-class metadata denormalized
    onto every chunk (the mandatory retriever filter reads chunk metadata only).
"""

from __future__ import annotations

import json
from typing import Any

import psycopg

from x1_advisor.ingest.bundles import SourceDoc
from x1_advisor.ingest.chunker import chunk_markdown


def _chunk_metadata(doc_meta: dict[str, Any], *, source_type: str, entity_type: str | None,
                    entity_id: int | None, visibility: str, is_published: bool,
                    eval_is_visible: bool | None) -> dict[str, Any]:
    """ACL-class + entity-ref fields every chunk carries (retriever filter contract)."""
    keep = {k: doc_meta[k] for k in (
        "company_name", "eval_overall_score", "section_key", "section_score",
        "website_url", "premium_gated", "evaluation_id", "entity_ref_env",
        "prod_startup_company_id", "bundle_generation",
    ) if doc_meta.get(k) is not None}
    return {
        **keep,
        "source_type": source_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "acl_visibility": visibility,
        "acl_is_published": is_published,
        "acl_eval_is_visible": eval_is_visible,
        "acl_premium_gated": bool(doc_meta.get("premium_gated", False)),
    }


def upsert_document(
    conn: psycopg.Connection,
    doc: SourceDoc,
    *,
    entity_type: str | None,
    entity_id: int | None,
    source_ref: str,
    is_published: bool = True,
    eval_is_visible: bool | None = None,
    extraction_model: str | None = None,
) -> tuple[int | None, str]:
    """Insert (or version-and-append) one document + its chunks.

    Returns (document_id, action) where action ∈ {'inserted', 'versioned', 'unchanged'}.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, version, content_hash FROM advisor.documents
               WHERE source_type = %s AND entity_type IS NOT DISTINCT FROM %s
                 AND entity_id IS NOT DISTINCT FROM %s AND source_ref = %s
                 AND (acl_source->>'section_key') IS NOT DISTINCT FROM %s
                 AND superseded_by IS NULL
               ORDER BY version DESC LIMIT 1""",
            (doc.source_type, entity_type, entity_id, source_ref,
             doc.metadata.get("section_key")),
        )
        existing = cur.fetchone()
        if existing and existing["content_hash"] == doc.content_hash:
            return existing["id"], "unchanged"

        version = (existing["version"] + 1) if existing else 1
        cur.execute(
            """INSERT INTO advisor.documents
                 (source_type, entity_type, entity_id, title, markdown, source_ref,
                  version, content_hash, extraction_model, visibility, is_published,
                  eval_is_visible, acl_source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (doc.source_type, entity_type, entity_id, doc.title, doc.markdown,
             source_ref, version, doc.content_hash, extraction_model,
             doc.visibility, is_published, eval_is_visible,
             json.dumps(doc.metadata, default=str)),
        )
        new_id = cur.fetchone()["id"]
        if existing:
            cur.execute(
                "UPDATE advisor.documents SET superseded_by = %s, updated_at = now() WHERE id = %s",
                (new_id, existing["id"]),
            )

        meta = _chunk_metadata(
            doc.metadata, source_type=doc.source_type, entity_type=entity_type,
            entity_id=entity_id, visibility=doc.visibility,
            is_published=is_published, eval_is_visible=eval_is_visible,
        )
        rows = [
            (new_id, b.block_index, "block", b.text, b.page_number,
             f"[{b.char_start},{b.char_end})", json.dumps(meta, default=str))
            for b in chunk_markdown(doc.markdown)
        ]
        cur.executemany(
            """INSERT INTO advisor.doc_chunks
                 (document_id, block_index, granularity, text, page_number, char_span, metadata)
               VALUES (%s,%s,%s,%s,%s,%s::int4range,%s)""",
            rows,
        )
    return new_id, ("versioned" if existing else "inserted")
