# X1 Advisor — Research Agent Architecture

> Status: **design draft** (2026-06-12). This is the agreed target design, not a record of
> existing code. Decisions marked **[DECIDED]** are settled; **[OPEN]** still needs a call.

## 1. What we are building

An **interactive, conversational research agent** ("research buddy") for the X1 platform.

The user asks an open-ended question; the agent performs **multi-hop research** across three
evidence sources — the X1 database, the private document store, and the web — and returns a
**source-grounded answer with citations**, then waits for the next turn. It is a chat session,
not a job.

The domain is startups, their team members, investors, funds, and organizations, plus the
documents they upload (pitch decks, business plans, CVs) and the documents the system itself
produces (evaluation findings, reports, research notes).

### Non-goals (explicitly)

- **Not** a one-shot deep-research report generator. Answers are conversational and bounded.
- **Not** an app-controlling "agentic copilot" with page actions / capability contracts. That
  was the prior, aborted direction; it is out of scope here.
- **Not SQL-first.** See §3 — generated SQL over a wide schema is brittle and cannot do
  semantic or document-content retrieval. SQL is a precision *tool*, not the spine.

## 2. Stack [DECIDED]

| Layer | Choice | Rationale |
|---|---|---|
| Orchestration + retrieval | **Haystack** (Prototype A: agent owns the loop) | Native `retrieve → filter → rank → compress → generate` discipline; production-grade hybrid retrieval, rerankers, pgvector integration, and a tool-calling `Agent`. |
| Language / runtime | **Python** (standalone service) | Haystack is Python. Accepted tradeoff vs. the TS backend; the research agent is its own service. |
| LLM | **Claude** (latest: Opus 4.8 / Sonnet 4.6 by task) via `AnthropicChatGenerator` | Haystack's chat generator is provider-agnostic. |
| Doc store + vectors | **pgvector** in the shared Cloud SQL Postgres, dedicated `advisor` schema | See §4. |

LangGraph is **deferred**, not rejected. We add it only if we later hit real control-flow needs
(approval flows, multi-agent roles, long-running background jobs, hard human checkpoints). For
"research over private docs + DB + web," Haystack-alone gets a serious evaluation first.

## 3. The core principle: retrieval is the spine, structured data is indexed evidence

The system is **not** "an LLM that writes SQL." The spine is **hybrid semantic retrieval over a
unified index**. Structured database content is folded *into* that index rather than queried
beside it:

- **Every entity** (startup, person, investor, fund, organization) is rendered into a
  denormalized, embeddable **profile document** — regenerated when the underlying rows change —
  and indexed alongside document chunks.
- **Structured fields** (stage, industry, region, fundraising amount, etc.) ride along as
  **metadata** on those vectors and are used as **retrieval filters**, never as LLM-authored SQL.

This is what makes the system both **semantic** (it can match "startups solving water scarcity")
and **relationally precise** (it can filter "raised > $2M AND industry = fintech") — without the
brittleness of generated joins, and while **scaling with the corpus** as the DB and document
store grow.

SQL survives only as a narrow, optional tool for genuine aggregates/counts where retrieval is the
wrong instrument. It does not drive the agent.

## 4. Storage model [DECIDED]

### 4.1 The DB is canonical for all markdown; GCS holds binaries

Policy: **all source material is converted to markdown** (PDF→md, website→md) and the markdown
is **stored in the database**. The research/index pipeline therefore reads **only the DB** — it
never reaches into GCS on the hot path.

The DB/GCS seam is drawn cleanly:

| Lives in Postgres (`advisor` schema) | Lives in GCS |
|---|---|
| Canonical markdown text | Original binaries (source PDFs, docx, pptx) — download / UI / archive |
| Chunks | Markdown mirror (optional, for backward-compat) |
| Embeddings (pgvector) | |
| Provenance / metadata | |

This unifies three kinds of content under one store:
1. **Uploaded** documents (pitch decks, business plans, CVs) after markdown extraction.
2. **System-produced** documents (eval findings, reports, research notes) — markdown-native.

Property worth calling out: because (2) lands in the same store, **the agent's own output becomes
first-class retrievable evidence** for future research. The knowledge base compounds.

### 4.2 Where it physically lives [DECIDED]

**pgvector, in the existing shared Cloud SQL instance, under a dedicated `advisor` schema.**

This is the mainstream choice for a team already on Postgres — logical isolation (advisor
migrations/tables don't tangle into the Laravel app schema) without standing up separate infra.
`PgvectorDocumentStore` supports embedding retrieval, keyword retrieval, and metadata filtering
in the same database.

**Low-regret discipline:** the doc-store tables reference app entities by id only
(`startup_company_id`, `user_id`, …) for filtering — they do **not** deeply entangle into the
app's foreign-key graph. That keeps the research substrate portable, so peeling it onto its own
instance (or swapping pgvector for Qdrant/Weaviate) later is a contained change, not a rewrite.

### 4.3 Schema sketch (advisor schema)

```text
advisor.documents
  id, source_type ('upload'|'website'|'eval'|'report'|'research_note'),
  entity_type, entity_id,            -- loose FK to app entity (e.g. startup_company_id)
  title, markdown,                   -- canonical text
  source_ref,                        -- GCS path of original binary / mirror, if any
  extraction_model, extraction_version, content_hash,   -- provenance (cf. doc_extraction_cache)
  created_at, updated_at

advisor.doc_chunks
  id, document_id -> documents.id,
  ordinal, text, char_span, metadata (jsonb)   -- entity ids, stage, industry, region, etc.

advisor.doc_embeddings        -- or folded into doc_chunks
  chunk_id -> doc_chunks.id, embedding vector, model

advisor.entity_profiles       -- denormalized, embeddable entity cards (regenerated on change)
  entity_type, entity_id, markdown, metadata (jsonb), content_hash, updated_at
```

`content_hash` drives cache-invalidation and re-indexing, reusing the pattern already in
`doc_extraction_cache`.

## 5. Pipelines

### 5.1 Ingestion / indexing (DB-only)

```text
new/changed markdown in advisor.documents
  → chunk
  → embed (document embedder)
  → write chunks + embeddings (PgvectorDocumentStore)

new/changed app entity row
  → render entity_profile markdown (denormalize joins: startup + team + fundraising + eval summary)
  → chunk + embed + write
```

Re-indexing is a DB job; no GCS access. Triggered by content-hash mismatch.

### 5.2 Retrieval (hybrid + rerank)

```text
query
  ├─ PgvectorKeywordRetriever   (BM25-style full-text)
  └─ PgvectorEmbeddingRetriever (dense; cosine, HNSW)
        ↓
   DocumentJoiner(join_mode="reciprocal_rank_fusion")
        ↓
   Reranker (cross-encoder / late-interaction)
        ↓
   top-k evidence (chunks + entity profiles), carrying source metadata + filters applied
```

Metadata filters (entity, stage, industry, region, date) are applied at the retriever level.

## 6. Tools exposed to the agent

The Haystack `Agent` owns the tool-call loop. Tools (each a `ComponentTool` or `@tool`):

| Tool | Purpose |
|---|---|
| `search_corpus(query, filters)` | The hybrid retrieval pipeline above. Primary research tool. Returns ranked evidence with source ids. |
| `get_source(source_id, span?)` | Fetch fuller context / an excerpt of a specific chunk or document on demand (rehydrate). |
| `structured_query(...)` | Narrow, safe aggregates/counts where retrieval is wrong (e.g. "how many seed-stage fintechs"). Parameterized / read-only — **not** free-form LLM SQL. |
| `web_search(query)` | External evidence (e.g. `SerperDevWebSearch`). |
| `fetch_url(url)` | Read a specific web page; converted to markdown for grounding. |

## 7. The agent loop [DECIDED shape, OPEN tuning]

Each user turn runs a **bounded multi-hop loop** and then waits:

- `max_agent_steps` / per-turn tool-call budget (start ~6–12).
- `exit_conditions = ["text"]` — agent returns when it answers without a tool call.
- Multi-hop: retrieve → read → if a new entity/claim surfaces, search again → stop when the
  question is covered or budget is hit.
- Answer **only from collected evidence**; persist a compact research record per turn.

## 8. Citations & grounding

Answers cite the `source_id`s of the evidence actually used. Because retrieval results carry
source metadata (document/chunk id, entity, url), citations resolve to real artifacts and can be
re-opened via `get_source`. The final answer is constrained to the collected evidence set, not
arbitrary prior chat/tool text — this is the main defense against hallucinated citations.

## 9. Context discipline

Carried over from prior lessons (the "Hermes" notes), expressed in Haystack terms:

1. The latest user request stays an active, unsummarized message.
2. Large tool results do **not** live in the transcript. The LLM sees compact references
   (counts, previews, source ids); full payloads stay retrievable and are rehydrated on demand
   via `get_source`.
3. Memory (`MemoryStore`) is injected as **background**, never as a new user instruction.
4. Switching entities/tasks must not let stale results dominate the next answer.
5. Prompt prefix stays stable for provider caching.

## 10. Open decisions / next steps

- **[OPEN]** Embedding model + dimension (affects pgvector column + recall/cost).
- **[OPEN]** Chunking strategy for extracted pitch decks (visual, slide-structured) vs. CVs vs.
  long reports.
- **[OPEN]** Reranker choice (hosted cross-encoder vs. local late-interaction).
- **[OPEN]** Web search provider.
- **[OPEN]** Entity-profile rendering: which joins/fields compose each entity card, and the
  re-index trigger (event vs. periodic content-hash sweep).
- **[OPEN]** `structured_query` surface: which parameterized queries to expose, read-only guard.
- **Next:** stand up the smallest end-to-end slice — `advisor` schema + pgvector, index a handful
  of real entity profiles + extracted decks, wire `search_corpus` + `web_search` into a Haystack
  `Agent`, and ask real questions to find where retrieval actually strains.
```
