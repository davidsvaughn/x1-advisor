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
from x1_advisor.filters import CompiledFilters, compile_filters
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
    # 'block' | 'record_summary' — record summaries are generated text ABOUT a
    # document, so the evidence boundary (Gate 1B) turns on this field
    granularity: str = "block"
    dense_rank: int | None = None
    lex_rank: int | None = None
    rrf_score: float = 0.0
    # set when this block took the place of a record-summary hit: the generated
    # summary that routed us to this document. Context for the model, NEVER
    # citable — see expand_summaries below.
    routed_by_summary: str | None = None


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


SUMMARY_EXPANSION_PER_DOC = 1   # 1:1 substitution — result size and context stay flat

_BASE_FROM = """
    FROM advisor.doc_chunks c
    JOIN advisor.documents d ON d.id = c.document_id
    WHERE d.superseded_by IS NULL
"""


def _row_to_hit(r: dict) -> "Hit":
    return Hit(r["id"], r["document_id"], r["block_index"], r["page_number"],
               r["text"], r["metadata"], r["title"], r["source_type"],
               r["granularity"])


def _expand_summaries(conn, out: list["Hit"], *, cfg: IndexConfig, qvec: str,
                      where_tail: str, tail_params: list,
                      ) -> tuple[list["Hit"], list[dict]]:
    """Evidence boundary (Gate 1B): a record summary identifies a document, it
    is never the terminal citation.

    Record summaries are LLM-written text *about* a document — the review found
    the agent citing them as if they were source blocks, and the first Gate-1A
    measurement put 38% of "resolvable" citations on them. But they are also the
    single best recall lever we have (+0.055), precisely because they give dense
    retrieval a whole-document handle.

    So: keep the routing, drop the citability. Each summary hit is replaced,
    in its own rank position, by the best-matching **source block** of the same
    document, and that block carries the summary text as labelled context the
    model can read but cannot cite. A document with no eligible source block
    loses the hit entirely — a summary must never survive as evidence.
    """
    summary_hits = [h for h in out if h.granularity == "record_summary"]
    if not summary_hits:
        return out, []

    doc_ids = sorted({h.document_id for h in summary_hits})
    have = [h.chunk_id for h in out]
    rows = conn.execute(
        f"""SELECT * FROM (
              SELECT c.id, c.document_id, c.block_index, c.page_number, c.text,
                     c.metadata, c.granularity, d.title, d.source_type,
                     row_number() OVER (
                         PARTITION BY c.document_id
                         ORDER BY (SELECT e.embedding <=> %s::vector
                                   FROM advisor.emb_{cfg.id} e
                                   WHERE e.chunk_id = c.id)) AS rn
              {_BASE_FROM} {where_tail}
                AND c.granularity = 'block'
                AND c.document_id = ANY(%s)
                AND NOT (c.id = ANY(%s))
                AND EXISTS (SELECT 1 FROM advisor.emb_{cfg.id} e
                            WHERE e.chunk_id = c.id)
            ) t WHERE rn <= %s""",
        (qvec, *tail_params, doc_ids, have, SUMMARY_EXPANSION_PER_DOC),
    ).fetchall()

    by_doc: dict[int, list[Hit]] = {}
    for r in rows:
        by_doc.setdefault(r["document_id"], []).append(_row_to_hit(r))

    expanded: list[Hit] = []
    substitutions: list[dict] = []
    for h in out:
        if h.granularity != "record_summary":
            expanded.append(h)
            continue
        replacements = by_doc.get(h.document_id, [])
        for b in replacements:
            # inherit the summary's rank position: the document earned it
            b.dense_rank, b.lex_rank = h.dense_rank, h.lex_rank
            b.rrf_score = h.rrf_score
            b.routed_by_summary = h.text
            expanded.append(b)
        outcome = "expanded"
        if not replacements:
            # two very different reasons for no replacement, and the funnel
            # classifier needs to tell them apart: the document is already in
            # the result via a real block (nothing lost), or it has no eligible
            # source block at all (the hit is dropped — a summary is never
            # downgraded into evidence)
            outcome = ("already_represented"
                       if any(o.document_id == h.document_id
                              and o.granularity == "block" for o in out)
                       else "dropped_no_source_block")
        substitutions.append({
            "summary_chunk_id": h.chunk_id, "document_id": h.document_id,
            "replaced_by": [b.chunk_id for b in replacements],
            "outcome": outcome,
        })
    return expanded, substitutions


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
    filters: dict[str, Any] | CompiledFilters | None = None,
    config_id: str | None = None,
    k: int = 10,
    rerank: bool = False,
    tracker: Tracker | None = None,
    explain_out: list[dict[str, Any]] | None = None,
    expand_summaries: bool = False,
) -> list[Hit]:
    """Hybrid retrieve.

    `explain_out` collects a per-call retrieval explain (QA-LOOP-DESIGN §4.2) —
    it goes to the turn bundle, never into the model's context, so it costs
    nothing in tokens.

    `expand_summaries` enforces the Gate-1B evidence boundary (see
    `_expand_summaries`). It is **off by default** so that this function stays
    the raw retrieval primitive the bake-offs measure, and on for the agent
    path, where the boundary belongs: nothing that reaches the model may be a
    generated summary. `experiments.run --expand-summaries` measures the
    corrected path against the same golden set.
    """
    cfg: IndexConfig = CONFIGS[config_id] if config_id else active_config(conn)
    acl_sql, acl_params = _acl_sql(acl)
    # filters are compiled — never interpolated. Callers that need the
    # resolution notes (the model-facing tool) compile first and pass the
    # CompiledFilters through; harness callers may pass a plain dict.
    compiled = compile_filters(conn, filters)
    where_tail = acl_sql + compiled.sql
    tail_params = acl_params + list(compiled.params)

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
                   c.metadata, c.granularity, d.title, d.source_type
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
                            r["title"], r["source_type"], r["granularity"],
                            dense_rank=rank)

    lex_rows = conn.execute(
        f"""SELECT c.id, c.document_id, c.block_index, c.page_number, c.text,
                   c.metadata, c.granularity, d.title, d.source_type,
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
                                r["title"], r["source_type"], r["granularity"],
                                lex_rank=rank)

    for h in hits.values():
        h.rrf_score = sum(1.0 / (RRF_K + rank)
                          for rank in (h.dense_rank, h.lex_rank) if rank)

    ranked = sorted(hits.values(), key=lambda h: -h.rrf_score)
    if rerank:
        ranked = _rerank_jina(query, ranked[:RERANK_DEPTH], tracker) + ranked[RERANK_DEPTH:]
    out: list[Hit] = []
    per_doc: dict[int, int] = {}
    seen_text: set[int] = set()
    # why a fused candidate did not make it into the results. Everything else
    # that fell short is derivable from `fused` minus `returned` (rank order).
    dropped: dict[str, list[int]] = {"dedup_text": [], "per_doc_cap": []}
    for h in ranked:
        if len(out) == k:
            break
        # cross-document near-duplicate guard: sibling eval bundles for the same
        # company yield near-identical deck/section chunks as distinct documents.
        # NB: hash() is per-process salted — fine per call, never persist it.
        text_key = hash(h.text.strip().lower())
        if text_key in seen_text:
            dropped["dedup_text"].append(h.chunk_id)
            continue
        if per_doc.get(h.document_id, 0) >= PER_DOC_CAP:
            dropped["per_doc_cap"].append(h.chunk_id)
            continue
        seen_text.add(text_key)
        out.append(h)
        per_doc[h.document_id] = per_doc.get(h.document_id, 0) + 1

    substitutions: list[dict] = []
    if expand_summaries:
        out, substitutions = _expand_summaries(
            conn, out, cfg=cfg, qvec=qvec, where_tail=where_tail,
            tail_params=tail_params)

    if explain_out is not None:
        explain_out.append({
            "call": len(explain_out) + 1,
            "query": query,
            "filters": dict(compiled.applied),
            "filter_notes": list(compiled.notes),
            # forensic snapshot of the ACL this search ran under; replay must
            # re-resolve authorization rather than feed this back (QA-LOOP §4.4)
            "acl": acl if acl == "admin" else dict(acl),
            "config_id": cfg.id,
            "k": k,
            "rerank": rerank,
            "legs": {
                "dense": [{"chunk_id": r["id"], "document_id": r["document_id"],
                           "rank": i} for i, r in enumerate(dense_rows, 1)],
                "lexical": [{"chunk_id": r["id"], "document_id": r["document_id"],
                             "rank": i} for i, r in enumerate(lex_rows, 1)],
            },
            "fused": [{"chunk_id": h.chunk_id, "document_id": h.document_id,
                       "rrf": round(h.rrf_score, 6), "dense_rank": h.dense_rank,
                       "lex_rank": h.lex_rank, "granularity": h.granularity}
                      for h in ranked],
            "dropped": {reason: ids for reason, ids in dropped.items() if ids},
            "summary_expansion": substitutions,
            "returned": [h.chunk_id for h in out],
        })
    return out
