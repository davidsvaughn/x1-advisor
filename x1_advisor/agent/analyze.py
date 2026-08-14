"""analyze_scope — budgeted semantic map/reduce over a document scope
(ANALYZE-SCOPE-DESIGN-2026-08-13; built 2026-08-14 on the demand trigger:
thread-032, "this question deserves a deeper semantic search").

scan_text censuses exact WORDS; this censuses MEANING: a cheap model reads
every in-scope document IN FULL (its ACL-readable blocks) and extracts
question-relevant findings with the block indexes that back them, then one
reduce call synthesizes across documents. The Tier-1 loop, citations, ACL
and cost enforcement are untouched — this is the web_research delegation
pattern aimed at the corpus.

Boundaries (the spec's contract):
  - ACL: blocks are fetched through retrieval's own class predicate — a
    block the requester cannot retrieve is never read;
  - evidence: per-finding supports are (document_id, block_index) pairs the
    caller registers as citable chunk refs; the reduction is generated text
    and is NOT citable (same boundary as record summaries);
  - honesty: coverage counts (docs read / in scope, entities) ride the
    result and the visible caps are part of the tool contract;
  - non-goals: no web, no recursive reads, no persistence of reductions.
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from x1_advisor.cost import Tracker, Usage
from x1_advisor.retrieval import _acl_sql

ANALYZE_MODEL = os.environ.get("ADVISOR_ANALYZE_MODEL", "gpt-5.6-luna")
FULL_READ_CAP = 100     # scopes up to this size are read in full (strict census)
MAX_READS = 120         # frontier mode never reads more than this many docs
STOP_AFTER_IRRELEVANT = 15   # frontier stopping rule: consecutive irrelevant
MAP_WORKERS = 8
PER_DOC_FINDING_CHARS = 1200    # map output budget per document
REDUCTION_CHARS = 1800

_MAP_PROMPT = """You are extracting findings for one analytical question from ONE document.

QUESTION: {question}

DOCUMENT: {title}
Content, as numbered blocks:
{blocks}

Reply with ONLY a JSON object:
{{"relevant": true/false, "findings": "...", "supports": [block numbers]}}

- "relevant": does this document actually bear on the question?
- "findings": what THIS document says that bears on the question — grounded
  in its text, no speculation, max {budget} characters. Empty if irrelevant.
- "supports": the block numbers that back the findings (must be from the
  numbers shown above)."""

_REDUCE_PROMPT = """Below are per-document findings extracted for one analytical question.
Synthesize them: recurring patterns, notable outliers, and an overall
answer to the question. Max {budget} characters. No preamble.

QUESTION: {question}

{findings}"""


def resolve_scope(conn, *, entity_type: str, source_types: list[str],
                  acl: Any, entity_ids: list[int] | None = None,
                  eval_recency: str | None = None) -> list[dict[str, Any]]:
    """Scope -> documents with their ACL-readable blocks, capped visibly.

    Returns [{document_id, entity_id, title, blocks: [(block_index, text)]}]
    ordered by document. Blocks pass retrieval's exact class predicate.
    """
    acl_sql, acl_params = _acl_sql(acl)
    sql = f"""SELECT d.id AS document_id, d.entity_id, d.title,
                     d.source_type, d.acl_source->>'evaluation_id' AS evaluation_id,
                     c.block_index, c.text
              FROM advisor.doc_chunks c
              JOIN advisor.documents d ON d.id = c.document_id
              WHERE d.superseded_by IS NULL
                AND c.granularity = 'block'
                AND d.source_type = ANY(%s)
                AND c.metadata->>'entity_type' = %s"""
    params: list[Any] = [source_types, entity_type]
    if eval_recency:
        sql += " AND c.metadata->>'eval_recency' = %s"
        params.append(eval_recency)
    if entity_ids:
        sql += " AND d.entity_id = ANY(%s)"
        params.append(entity_ids)
    sql += acl_sql + " ORDER BY d.id, c.block_index"
    docs: dict[int, dict[str, Any]] = {}
    for r in conn.execute(sql, (*params, *acl_params)).fetchall():
        d = docs.setdefault(r["document_id"],
                            {"document_id": r["document_id"],
                             "entity_id": r["entity_id"],
                             "title": r["title"],
                             "source_type": r["source_type"],
                             "evaluation_id": r["evaluation_id"],
                             "blocks": []})
        d["blocks"].append((r["block_index"], r["text"]))
    out = list(docs.values())
    # basic reports are EXCERPTS of the same run's premium report (David
    # 2026-08-14): when an evaluation's premium doc is readable for this
    # requester, its basic twin is duplicate text — skip it. When premium is
    # gated (non-purchaser), basic is what they may read: it stays. The skip
    # count rides the coverage disclosure via the caller.
    premium_evals = {d["evaluation_id"] for d in out
                     if d["source_type"] == "eval_premium" and d["evaluation_id"]}
    kept = [d for d in out
            if not (d["source_type"] == "eval_basic"
                    and d["evaluation_id"] in premium_evals)]
    skipped = len(out) - len(kept)
    for d in kept:
        d["basic_twins_skipped"] = skipped     # same value on all; read once
    return kept


def rank_by_embedding(conn, doc_ids: list[int], question: str,
                      tracker: Tracker | None) -> list[int]:
    """Embedding prefilter (David 2026-08-14): rank the scope's documents by
    their best-chunk similarity to the question — the vectors PRIORITIZE
    (what similarity is good at), the reader JUDGES (what it cannot do).
    Docs without vectors sort last rather than dropping."""
    from openai import OpenAI

    from x1_advisor.index import active_config

    cfg = active_config(conn)
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.embeddings.create(model=cfg.embedding_model, input=[question])
    if tracker is not None:
        tracker.log(provider="openai", model=cfg.embedding_model,
                    stage="tool.analyze_scope.rank",
                    usage=Usage(embed_tokens=resp.usage.prompt_tokens))
    qvec = str(resp.data[0].embedding)
    rows = conn.execute(
        f"""SELECT c.document_id, min(e.embedding <=> %s::vector) AS dist
            FROM advisor.emb_{cfg.id} e
            JOIN advisor.doc_chunks c ON c.id = e.chunk_id
            WHERE c.document_id = ANY(%s)
            GROUP BY c.document_id
            ORDER BY dist""",
        (qvec, doc_ids)).fetchall()
    ranked = [r["document_id"] for r in rows]
    return ranked + [d for d in doc_ids if d not in set(ranked)]


def _parse_map_reply(text: str, valid_blocks: set[int]) -> dict[str, Any]:
    """Model reply -> {relevant, findings, supports}; never raises.
    Unparseable replies come back as an honest non-finding, and supports are
    validated against the blocks the model was actually shown."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        d = {}
    supports = [int(b) for b in (d.get("supports") or [])
                if isinstance(b, (int, str)) and str(b).lstrip("-").isdigit()
                and int(b) in valid_blocks]
    findings = str(d.get("findings") or "")[:PER_DOC_FINDING_CHARS]
    return {"relevant": bool(d.get("relevant")) and bool(findings.strip()),
            "findings": findings, "supports": supports}


def analyze(conn, *, question: str, entity_type: str,
            source_types: list[str], acl: Any, tracker: Tracker | None,
            entity_ids: list[int] | None = None,
            eval_recency: str | None = None,
            ask: Callable[[str], str] | None = None,
            ranker: Callable[..., list[int]] | None = None) -> dict[str, Any]:
    """Run the map/reduce. `ask` is injectable for tests; default = one
    ANALYZE_MODEL chat call at the model's lowest reasoning effort."""
    docs = resolve_scope(conn, entity_type=entity_type,
                         source_types=source_types, acl=acl,
                         entity_ids=entity_ids, eval_recency=eval_recency)
    if not docs:
        return {"error": "no readable documents in that scope", "docs_in_scope": 0}

    if ask is None:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        effort = {"value": "minimal"}    # minimal->none fallback (summaries.py)

        def ask(prompt: str) -> str:
            from openai import BadRequestError
            for tier in (effort["value"], "none"):
                try:
                    resp = client.chat.completions.create(
                        model=ANALYZE_MODEL, reasoning_effort=tier,
                        messages=[{"role": "user", "content": prompt}])
                    effort["value"] = tier
                    break
                except BadRequestError as exc:
                    if "reasoning_effort" not in str(exc):
                        raise
            if tracker is not None:
                tracker.log(provider="openai", model=ANALYZE_MODEL,
                            stage="tool.analyze_scope",
                            usage=Usage.from_haystack_meta(
                                "openai", resp.usage.model_dump()))
            return (resp.choices[0].message.content or "").strip()

    def map_one(doc: dict[str, Any]) -> dict[str, Any]:
        blocks_txt = "\n".join(f"[b{i}] {t}" for i, t in doc["blocks"])
        reply = ask(_MAP_PROMPT.format(question=question, title=doc["title"],
                                       blocks=blocks_txt,
                                       budget=PER_DOC_FINDING_CHARS))
        parsed = _parse_map_reply(reply, {i for i, _ in doc["blocks"]})
        return {**doc, **parsed}

    mode = "full_read"
    stopped_early = False
    if len(docs) <= FULL_READ_CAP:
        with ThreadPoolExecutor(max_workers=MAP_WORKERS) as pool:
            mapped = list(pool.map(map_one, docs))
    else:
        # embedding-ranked frontier: read in relevance order, batch by batch,
        # stop once the frontier goes dry (STOP_AFTER_IRRELEVANT consecutive
        # irrelevant docs) or at MAX_READS. Not a strict census — the unread
        # tail and the stopping rule ride the coverage disclosure.
        mode = "embedding_ranked_frontier"
        if ranker is None:
            ranker = rank_by_embedding
        order = ranker(conn, [d["document_id"] for d in docs], question, tracker)
        by_id = {d["document_id"]: d for d in docs}
        queue = [by_id[i] for i in order if i in by_id][:MAX_READS]
        mapped = []
        with ThreadPoolExecutor(max_workers=MAP_WORKERS) as pool:
            for start in range(0, len(queue), MAP_WORKERS):
                batch = queue[start:start + MAP_WORKERS]
                mapped.extend(pool.map(map_one, batch))
                tail = [m["relevant"] for m in mapped[-STOP_AFTER_IRRELEVANT:]]
                if (len(mapped) >= STOP_AFTER_IRRELEVANT
                        and not any(tail)):
                    stopped_early = True
                    break

    relevant = [m for m in mapped if m["relevant"]]
    reduction = ""
    if relevant:
        listing = "\n\n".join(
            f"--- {m['title']} ---\n{m['findings']}" for m in relevant)
        reduction = ask(_REDUCE_PROMPT.format(
            question=question, findings=listing,
            budget=REDUCTION_CHARS))[:REDUCTION_CHARS]

    return {
        "coverage": {"docs_read": len(mapped), "docs_in_scope": len(docs),
                     "entities_read": len({m["entity_id"] for m in mapped}),
                     "evaluations_read": len({m["evaluation_id"] for m in mapped
                                              if m.get("evaluation_id")}),
                     "relevant_evaluations": len({m["evaluation_id"] for m in relevant
                                                  if m.get("evaluation_id")}),
                     "basic_reports_skipped_as_premium_excerpts":
                         docs[0].get("basic_twins_skipped", 0) if docs else 0,
                     "relevant_documents": len(relevant),
                     "eval_recency": eval_recency,
                     "mode": mode,
                     **({"stopping_rule":
                         f"stopped after {STOP_AFTER_IRRELEVANT} consecutive "
                         "irrelevant documents in embedding-relevance order"
                         if stopped_early else
                         f"read cap ({MAX_READS}) or queue exhausted",
                         "docs_unread": len(docs) - len(mapped)}
                        if mode != "full_read" else {})},
        "findings": [{"document_id": m["document_id"],
                      "entity_id": m["entity_id"],
                      "evaluation_id": m.get("evaluation_id"),
                      "title": m["title"],
                      "findings": m["findings"], "supports": m["supports"]}
                     for m in relevant],
        "reduction": reduction,
        "model": ANALYZE_MODEL,
    }
