# X1 Advisor — Research Agent Architecture

> Status: **design draft** (2026-06-12). This is the agreed target design, not a record of
> existing code. Decisions marked **[DECIDED]** are settled; **[OPEN]** still needs a call.
>
> Several sections below are informed by reference mining of two external codebases —
> see [`docs/refs/pipeshub-ai.md`](refs/pipeshub-ai.md) (a close sibling system: hybrid
> retrieval, block-level citations, deep-research agent) and [`docs/refs/chroma.md`](refs/chroma.md)
> (vector-store internals + a pgvector-vs-Chroma evaluation). Borrowed patterns are cited inline as
> _(ref: pipeshub …)_.

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

advisor.doc_chunks            -- the "block" in citation terms
  id, document_id -> documents.id,
  block_index,                       -- STABLE position within the doc; the citation primitive (see §8)
  granularity ('block'|'sentence'|'record_summary'),   -- dual-granularity + the profile-summary block
  text, char_span, page_number, bounding_box (jsonb),  -- page/bbox enable precise span citations for PDFs
  metadata (jsonb)                   -- entity ids, stage, industry, region, etc.

advisor.doc_embeddings        -- or folded into doc_chunks
  chunk_id -> doc_chunks.id, embedding vector, model

advisor.entity_profiles       -- denormalized, embeddable entity cards (regenerated on change)
  entity_type, entity_id, markdown, metadata (jsonb), content_hash, updated_at
```

`content_hash` drives cache-invalidation and re-indexing, reusing the pattern already in
`doc_extraction_cache`. `block_index` is **stable across re-ingest** (see diff-indexing in §5.1) so
that citations emitted in earlier turns keep resolving _(ref: pipeshub `models/blocks.py`,
`utils/citations.py`)_.

## 5. Pipelines

### 5.1 Ingestion / indexing (DB-only)

```text
new/changed markdown in advisor.documents
  → chunk (structure-aware; see below)
  → embed (document embedder)
  → write chunks + embeddings (PgvectorDocumentStore)

new/changed app entity row
  → render entity_profile markdown (denormalize joins: startup + team + fundraising + eval summary)
  → chunk + embed + write
```

**Chunking strategy [DECIDED direction]** _(ref: pipeshub `modules/transformers/vectorstore.py`)_ —
**structure-aware, not fixed-window.** No `chunk_size`/`overlap` constant; chunk boundaries follow
document structure (headings, paragraphs, table rows, slides). Two refinements worth importing:

- **Dual granularity:** index each block *and* its individual sentences, so retrieval can match a
  precise sentence or a whole section. (`granularity` column in §4.3.)
- **Record-summary block:** every document also gets one denormalized, LLM-written summary block
  indexed as its own unit — this is the same construct as our **entity profile document**, so a
  single semantic hit can surface a whole doc/entity's gist. Decks/CVs/reports all get one.
- **Multimodal → searchable text** for visual pitch decks: VLM-caption each slide/image and
  convert table rows to natural language, carrying `page_number` + `bounding_box` for citations.

**Re-indexing = diff-indexing [DECIDED]** _(ref: pipeshub `pipeline.py` reconciliation)_. On
re-ingest of a changed document, diff blocks against the prior version and **re-embed only changed
blocks, preserving `block_index` for unchanged ones**. This keeps the index fresh cheaply *and*
keeps previously-emitted citations valid. Trigger is content-hash mismatch; no GCS access.

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

Notes from reference mining:
- **Fusion is ours to assemble.** PipesHub gets dense+sparse RRF "for free" from Qdrant's named
  vectors; on pgvector we fuse the two retrievers ourselves via `DocumentJoiner`. Haystack ships
  this off-the-shelf, but it is our composition, not a single server-side call _(ref: chroma.md §5)_.
- **Reranker blend, starting point:** combine dense and rerank scores rather than replacing, e.g.
  `final = 0.3·dense + 0.7·rerank` _(ref: pipeshub `retrieval_service.py`)_. Tune later.
- **Scoring as algebra (later):** Chroma models ranking as a serializable arithmetic expression
  (`RankExpr`: sum/mul/exp/log/RRF) — a clean, loggable way to express weighted fusion if our
  blend grows beyond a constant _(ref: chroma.md §3e)_.

## 6. Tools exposed to the agent

The Haystack `Agent` owns the tool-call loop. Tools (each a `ComponentTool` or `@tool`):

| Tool | Purpose |
|---|---|
| `search_corpus(query, filters)` | The hybrid retrieval pipeline above. Primary research tool. Returns ranked evidence with source ids. |
| `get_source(source_id, span?)` | Fetch fuller context / an excerpt of a specific chunk or document on demand (rehydrate). Mirrors pipeshub's `fetch_full_record`: search returns excerpts; the agent pulls the full doc only when it asks. |
| `structured_query(...)` | Narrow, safe aggregates/counts where retrieval is wrong (e.g. "how many seed-stage fintechs"). Parameterized / read-only — **not** free-form LLM SQL. |
| `web_search(query)` | External evidence (e.g. `SerperDevWebSearch`). |
| `fetch_url(url)` | Read a specific web page; converted to markdown for grounding. |

## 7. The agent loop [DECIDED shape, OPEN tuning]

**Two tiers, not one** _(ref: pipeshub `qna/` vs `deep/`)_. Most questions should not pay
deep-research cost. We run two paths and route by question complexity:

**Tier 1 — fast single-turn (default).** A bounded ReAct loop, one-to-few tool calls, answers in
seconds. This is what every ordinary question hits.

- `max_agent_steps` / per-turn tool-call budget (start ~6–12).
- `exit_conditions = ["text"]` — agent returns when it answers without a tool call.
- Multi-hop within the turn: retrieve → read → if a new entity/claim surfaces, search again →
  stop when the question is covered or budget is hit.
- Answer **only from collected evidence**; persist a compact research record per turn.

**Tier 2 — deep multi-hop (opt-in / on hard questions).** A plan→critic→execute→evaluate loop for
genuinely multi-part research. Borrowed shape from pipeshub's deep agent:

- **Plan** into a typed task DAG with explicit `depends_on` and per-task *scoped instructions*
  (each sub-task sees only its slice, not the global prompt).
- **Critic gate** the plan once, **biased toward approve** (partial execution beats stalling);
  allow a single re-plan.
- **Execute** sub-tasks in parallel, each gated on its own dependencies; isolated context per
  sub-task.
- **Evaluate / aggregate** → answer or one bounded retry.
- **Hard budgets:** max iterations (~3) and per-agent tool-call caps. No unbounded loops.

The fast tier is the priority to build first; the deep tier is a later addition for the "deep
answer" product mode. Both share the same `respond` / citation stage so output is identical.

## 8. Citations & grounding [DECIDED — adopt pipeshub's block-citation mechanism]

Grounding is **enforced in code, not trusted from the LLM**. We adopt PipesHub's end-to-end
block-citation design _(ref: pipeshub `models/blocks.py`, `utils/chat_helpers.py`,
`utils/citations.py`)_, which is the strongest part of that codebase. The chain:

1. **The chunk/block is the citation primitive.** Each retrieved unit resolves to a stable
   `(document_id, block_index)` → text + `page_number`/`bounding_box` (for PDFs) or entity row +
   field (for profile documents). `block_index` is stable across re-ingest (§5.1), so citations
   stay valid over time.
2. **Tiny opaque refs in the model's output, not URLs.** Each block shown to the agent is labelled
   with a short id like `ref1`. The model cites by emitting `[source](ref1)` inline right after the
   claim. (LLMs reproduce short refs reliably and mangle long URLs.)
3. **"Omit rather than guess."** The system prompt instructs the model to use the exact ref shown,
   never invent numbers, and **omit a citation if unsure** which block a fact came from.
4. **Server-side post-validator.** After generation, a resolver walks every emitted ref, maps it
   back to the actual retrieved block row, **repairs malformed refs, de-dupes, drops refs that
   don't resolve, and renumbers sequentially** to `[1], [2], …`. The model never picks the final
   numbers; invalid citations cannot survive.

The final answer is constrained to the collected evidence set, not arbitrary prior chat/tool text.
Web citations carry a `web|url` type; internal ones carry `document`/`entity` + the resolvable
`(document_id, block_index)` so the UI can deep-link to the exact span. `get_source` re-opens any
cited block on demand.

## 9. Context discipline

Carried over from prior lessons (the "Hermes" notes) and sharpened with concrete mechanisms from
pipeshub's `deep/context_manager.py` / `qna/memory_optimizer.py`:

1. The latest user request stays an active, unsummarized message.
2. Large tool results do **not** live in the transcript. The LLM sees compact references
   (counts, previews, source ids); full payloads stay retrievable and are rehydrated on demand
   via `get_source`. Concretely _(ref: pipeshub `compact_tool_results`)_: keep priority keys
   (id/url/name/status) intact, truncate large string bodies, flag `_truncated` — but **never
   silently drop items** (preserve-all is the rule; truncation is opt-in and visible).
3. **Conversation compaction:** keep the last ~5 turns verbatim, LLM-summarize older turns.
4. Memory (`MemoryStore`) is injected as **background**, never as a new user instruction.
5. Switching entities/tasks must not let stale results dominate the next answer.
6. Deep-tier sub-tasks get **isolated context** (their task + dependency results only), not the
   whole conversation.
7. Explicit token + iteration budgets (start: context budget ~16k tokens, deep max iterations ~3).
8. Prompt prefix stays stable for provider caching.

## 10. Telemetry & cost tracking [DECIDED]

Every LLM call **requests full token usage and routes it through a cost tracker** — not just the
chat answer, but *all* model calls: query planning, the deep-tier critic/sub-agents, ingestion-time
calls (VLM captions, table summaries, record/profile summaries), embeddings. Implemented in
[`x1_advisor/cost.py`](../x1_advisor/cost.py).

- **Full usage, normalized.** Haystack exposes usage on `reply.meta["usage"]`, but field names
  differ by provider. `Usage.from_haystack_meta(provider, meta)` normalizes into canonical
  non-overlapping fields: uncached input / cache-read / cache-write / output. (Critical subtlety:
  Anthropic's `input_tokens` already *excludes* cached tokens; OpenAI's `prompt_tokens` *includes*
  them — mishandling either silently mis-prices every call.)
- **Cache-aware pricing.** Anthropic prompt caching bills **reads ≈ 0.1× input** and **writes ≈
  1.25× input**; both are tracked, since our stable-prefix strategy (§9) makes them material.
- **Loud on unknown models.** `estimate()` raises if a model isn't in the `PRICING` table — a
  missing entry is never a silent $0.
- **Pluggable persistence.** A `CostSink` interface decouples accounting from storage; `JsonlSink`
  is the zero-infra default and writes the **full** record per call (honors no-silent-truncation).
- **Budgets.** `Tracker` accumulates per-run cost and exposes a soft per-run cap; the deep-tier
  budget gate (§7) consults it. A daily cap is enforced by a ledger sink once one exists.
- Pricing table is the canonical source, kept current (mirrors
  `signal-hunter/signal_hunter/cost.py`); `[OPEN]` — pick the embedding model so its per-token
  rate and the pgvector dimension are both pinned.

## 11. Open decisions / next steps

Resolved by reference mining (now [DECIDED direction], detailed above):
- **Chunking** → structure-aware + dual-granularity + record-summary block + multimodal (§5.1).
- **Citations** → block-index + tiny-ref + omit-rather-than-guess + server-side validator (§8).
- **Re-index trigger** → diff-indexing on content-hash mismatch, stable `block_index` (§5.1).
- **Deep-research loop shape** → plan→critic→execute→evaluate, two-tier (§7).

Still open:
- **[OPEN]** Embedding model + dimension (affects pgvector column + recall/cost). Pipeshub reads
  dimension at runtime from the embedder and defaults to cosine; we should pick a model first.
- **[OPEN]** Reranker choice (hosted cross-encoder vs. local late-interaction) — blend ~`0.3·dense
  + 0.7·rerank` to start.
- **[OPEN]** Web search provider (e.g. Serper/Brave/Tavily).
- **[OPEN]** Entity-profile rendering: which joins/fields compose each entity card; event-driven vs.
  periodic content-hash sweep for refresh.
- **[OPEN]** `structured_query` surface: which parameterized queries to expose, read-only guard.
- **[OPEN]** Question router for Tier-1 vs Tier-2 (fast vs deep) — when to escalate.

- **Next:** stand up the smallest end-to-end slice — `advisor` schema + pgvector, index a handful
  of real entity profiles + extracted decks, wire `search_corpus` + `web_search` into a Haystack
  `Agent` (Tier-1 fast path only), and ask real questions to find where retrieval actually strains.
```
