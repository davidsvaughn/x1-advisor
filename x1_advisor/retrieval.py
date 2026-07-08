"""Hybrid retrieval: dense (pgvector cosine) + lexical (native FTS) → RRF →
group-by-entity diversification. Plain SQL/psycopg — framework-independent by
construction (review §6.4 guardrail 1); this function IS the bake-off entry point:
`retrieve(query, filters, config_id)` (PLAN Phase 2).

ACL: the `acl` argument is the MANDATORY retriever-level filter (never prompt-level).
`acl="admin"` sees everything; a dict applies class predicates (PLAN §0.2 — gates
content classes, never other users' existence):
    {"user_id": 7,
     "owned_entity_ids": {"startup_company": [3]},   # drafts/private carve-outs
     "purchased_evaluation_ids": [11, 12]}           # premium gating
Phase 4 wires the SQL resolver that builds this dict from the requesting user.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from x1_advisor.cost import Tracker, Usage
from x1_advisor.index import CONFIGS, IndexConfig, active_config

RRF_K = 60
LEG_DEPTH = 50          # candidates fetched per leg before fusion
PER_DOC_CAP = 3         # diversification: max chunks per document in final top-k

RERANK_MODEL = "jina-reranker-v3"
RERANK_DEPTH = 40       # fused candidates sent to the reranker
RERANK_BLEND = 0.7      # final = 0.3·rrf_norm + 0.7·rerank_norm (pipeshub starting point)


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    block_index: int
    page_number: int | None
    text: str
    metadata: dict[str, Any]
    title: str
    source_type: str
    dense_rank: int | None = None
    lex_rank: int | None = None
    rrf_score: float = 0.0


def _acl_sql(acl: Any) -> tuple[str, list]:
    """ACL-class predicates over chunk metadata / document columns. Default-open
    cross-user research; gates classes only (visibility, drafts, premium)."""
    if acl == "admin":
        return "", []
    if not isinstance(acl, dict):
        raise ValueError("acl must be 'admin' or a class-filter dict")
    params: list = []
    owned = acl.get("owned_entity_ids") or {}
    owned_pairs = [(t, i) for t, ids in owned.items() for i in ids]
    owned_clause = ""
    if owned_pairs:
        owned_clause = (
            " OR (d.entity_type, d.entity_id) IN ("
            + ",".join(["(%s,%s)"] * len(owned_pairs)) + ")"
        )
    # private docs: excluded in cross-user answers (PLAN §5.1 recommended default)
    sql = f" AND (d.visibility <> 'private'{owned_clause})"
    for t, i in owned_pairs:
        params.extend([t, i])
    # drafts: owner-only
    sql += f" AND (d.is_published{owned_clause})"
    for t, i in owned_pairs:
        params.extend([t, i])
    # premium full text: purchase-gated per requester
    purchased = acl.get("purchased_evaluation_ids") or []
    if purchased:
        sql += (" AND (COALESCE((c.metadata->>'acl_premium_gated')::boolean, false) = false"
                " OR (c.metadata->>'evaluation_id')::bigint = ANY(%s))")
        params.append(purchased)
    else:
        sql += " AND COALESCE((c.metadata->>'acl_premium_gated')::boolean, false) = false"
    # hidden evals stay hidden for everyone but admins
    sql += " AND COALESCE((c.metadata->>'acl_eval_is_visible')::boolean, true)"
    return sql, params


def _filter_sql(filters: dict[str, Any] | None) -> tuple[str, list]:
    """Structured-field filters over chunk metadata (equality or any-of on arrays)."""
    if not filters:
        return "", []
    sql, params = "", []
    for key, value in filters.items():
        if isinstance(value, list):
            sql += f" AND (c.metadata->'{key}' ?| %s OR c.metadata->>'{key}' = ANY(%s))"
            params.extend([value, value])
        else:
            sql += f" AND c.metadata->>'{key}' = %s"
            params.append(str(value))
    return sql, params


_BASE_FROM = """
    FROM advisor.doc_chunks c
    JOIN advisor.documents d ON d.id = c.document_id
    WHERE d.superseded_by IS NULL
"""


def _rerank_jina(query: str, candidates: list["Hit"],
                 tracker: Tracker | None) -> list["Hit"]:
    """E2 slot: Jina rerank over the fused candidate pool, blended with RRF
    (0.3·rrf + 0.7·rerank, both min-max normalized within the pool)."""
    if not candidates:
        return candidates
    import time

    import httpx

    for attempt in range(5):
        resp = httpx.post(
            "https://api.jina.ai/v1/rerank",
            headers={"Authorization": f"Bearer {os.environ['JINA_API_KEY']}",
                     "Content-Type": "application/json"},
            json={"model": RERANK_MODEL, "query": query,
                  "documents": [h.text[:4096] for h in candidates],
                  "top_n": len(candidates)},
            timeout=60,
        )
        if resp.status_code != 429:
            break
        # free-tier RPM limit: honor Retry-After, else exponential backoff
        wait = float(resp.headers.get("retry-after") or 15 * (attempt + 1))
        time.sleep(min(wait, 90))
    resp.raise_for_status()
    data = resp.json()
    if tracker:
        tracker.log(provider="jina", model=RERANK_MODEL, stage="retrieve.rerank",
                    usage=Usage(embed_tokens=(data.get("usage") or {}).get("total_tokens", 0)))
    scores = {r["index"]: r["relevance_score"] for r in data["results"]}
    lo, hi = min(scores.values()), max(scores.values())
    rrfs = [h.rrf_score for h in candidates]
    rlo, rhi = min(rrfs), max(rrfs)

    def blended(i: int, h: "Hit") -> float:
        rr = (scores.get(i, lo) - lo) / (hi - lo) if hi > lo else 0.0
        rf = (h.rrf_score - rlo) / (rhi - rlo) if rhi > rlo else 0.0
        return (1 - RERANK_BLEND) * rf + RERANK_BLEND * rr

    order = sorted(range(len(candidates)), key=lambda i: -blended(i, candidates[i]))
    return [candidates[i] for i in order]


def retrieve(
    conn,
    query: str,
    *,
    acl: Any,
    filters: dict[str, Any] | None = None,
    config_id: str | None = None,
    k: int = 10,
    rerank: bool = False,
    tracker: Tracker | None = None,
) -> list[Hit]:
    cfg: IndexConfig = CONFIGS[config_id] if config_id else active_config(conn)
    acl_sql, acl_params = _acl_sql(acl)
    f_sql, f_params = _filter_sql(filters)
    where_tail = acl_sql + f_sql
    tail_params = acl_params + f_params

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.embeddings.create(model=cfg.embedding_model, input=[query])
    if tracker:
        tracker.log(provider="openai", model=cfg.embedding_model,
                    stage="retrieve.embed_query",
                    usage=Usage(embed_tokens=resp.usage.prompt_tokens))
    qvec = str(resp.data[0].embedding)

    hits: dict[int, Hit] = {}

    dense_rows = conn.execute(
        f"""SELECT c.id, c.document_id, c.block_index, c.page_number, c.text,
                   c.metadata, d.title, d.source_type
            {_BASE_FROM} {where_tail}
              AND EXISTS (SELECT 1 FROM advisor.emb_{cfg.id} e WHERE e.chunk_id = c.id)
            ORDER BY (SELECT e.embedding <=> %s::vector
                      FROM advisor.emb_{cfg.id} e WHERE e.chunk_id = c.id)
            LIMIT %s""",
        (*tail_params, qvec, LEG_DEPTH),
    ).fetchall()
    for rank, r in enumerate(dense_rows, 1):
        hits[r["id"]] = Hit(r["id"], r["document_id"], r["block_index"],
                            r["page_number"], r["text"], r["metadata"],
                            r["title"], r["source_type"], dense_rank=rank)

    lex_rows = conn.execute(
        f"""SELECT c.id, c.document_id, c.block_index, c.page_number, c.text,
                   c.metadata, d.title, d.source_type,
                   ts_rank_cd(to_tsvector('english', c.text), q) AS score
            {_BASE_FROM.replace('WHERE', ', websearch_to_tsquery(%s) q WHERE')} {where_tail}
              AND to_tsvector('english', c.text) @@ q
            ORDER BY score DESC LIMIT %s""",
        (query, *tail_params, LEG_DEPTH),
    ).fetchall()
    for rank, r in enumerate(lex_rows, 1):
        h = hits.get(r["id"])
        if h:
            h.lex_rank = rank
        else:
            hits[r["id"]] = Hit(r["id"], r["document_id"], r["block_index"],
                                r["page_number"], r["text"], r["metadata"],
                                r["title"], r["source_type"], lex_rank=rank)

    for h in hits.values():
        h.rrf_score = sum(1.0 / (RRF_K + rank)
                          for rank in (h.dense_rank, h.lex_rank) if rank)

    ranked = sorted(hits.values(), key=lambda h: -h.rrf_score)
    if rerank:
        ranked = _rerank_jina(query, ranked[:RERANK_DEPTH], tracker) + ranked[RERANK_DEPTH:]
    out: list[Hit] = []
    per_doc: dict[int, int] = {}
    seen_text: set[int] = set()
    for h in ranked:
        # cross-document near-duplicate guard: sibling eval bundles for the same
        # company yield near-identical deck/section chunks as distinct documents
        text_key = hash(h.text.strip().lower())
        if text_key in seen_text:
            continue
        if per_doc.get(h.document_id, 0) >= PER_DOC_CAP:
            continue
        seen_text.add(text_key)
        out.append(h)
        per_doc[h.document_id] = per_doc.get(h.document_id, 0) + 1
        if len(out) == k:
            break
    return out
