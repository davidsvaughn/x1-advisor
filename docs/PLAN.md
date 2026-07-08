# X1 Advisor — Implementation Plan

> Status: **working plan** (2026-07-07). Operationalizes [`ARCHITECTURE.md`](ARCHITECTURE.md)
> (the design) as amended by [`ARCHITECTURE-REVIEW.md`](ARCHITECTURE-REVIEW.md) (the review).
> Where the two disagree, this plan follows the review. Supersedes the "Suggested next step"
> section of [`HANDOFF.md`](HANDOFF.md).
>
> **Prime directive of this plan:** model choices (LLM, embeddings, reranker, web search) are
> **experiments, not commitments**. Every seam where a model/provider plugs in is built
> swappable, every candidate is measured on the same golden set with cost attached, and the
> "decision" for each is a dated entry in `DECISIONS.md` after the bake-off — not a line in
> this plan.

---

## 0. Decisions adopted up front (from the review)

These are settled unless new evidence overturns them; everything model-shaped is *not* in this
list (see Phase 3):

1. **Citations are page-level in v1.** `(document_id, block_index, page_number)` is the
   citation primitive; no `bounding_box` column. Deck citation UX = slide thumbnail.
2. **Access model: open cross-user research with class-based guardrails (David, 2026-07-07).**
   Founders and investors researching *other* startups/investors/companies **is the whole
   point of the feature** — the agent must have cross-user reach over entity data, evals, and
   documents. **Never build identity-based walls** ("you can only see your own data") into
   retrieval; guardrails gate *classes of sensitive content*, not other users' existence:
   - **Default-open:** published entity profiles, eval scores + visible summaries,
     `public`/`x1` documents, website content, web evidence — researchable by any
     authenticated user, about anyone.
   - **Class guardrails (the only gates):** unpublished/draft profiles (owner-only — they're
     drafts); `private`-visibility documents (uploader chose privacy — treatment below);
     premium eval report full text (a paid product — purchase-gated via `report_purchases`);
     and a never-index list (contact emails, invite/claim/share tokens, anything
     credential-shaped).
   - Mechanism unchanged from the review: ACL-class metadata on every chunk + a query-time
     filter — so all of the above are **policy dials, not re-indexes**.
   ⚠️ *Open sub-decisions (David), §5:* treatment of `private` decks in cross-user answers,
   and premium purchase-gating strictness.
3. **Eval harness before tuning.** No bake-off runs before the golden set exists (Phase 2).
4. **Haystack for the Tier-1 slice, used shallow** (pipelines-as-tools + one `Agent`), with
   the three §6.4 integration spikes as a go/no-go gate in Phase 0, and the thin direct-SDK
   stack as the named exit ramp. Tool schemas, prompts, citation layer, ACL resolver, and the
   eval harness are written as plain Python — framework-independent.
5. **Deferred until evidence demands them:** Tier-2 deep mode (and when built: converged
   shape — single loop + plan artifact + parallel read-only subagents, *not* plan→critic→
   execute), auto-router, sentence-level dual granularity, block-level diff-indexing
   (v1 = version-and-append), bounding-box extraction, sparse-vector lexical upgrade.
6. **Ingestion reuses what exists:** eval bundles are the backfill source; the Gemini
   extraction pipeline is inherited, not rebuilt; entity profiles start from the
   `ReportChatService` field lists; TipTap HTML → markdown in the profile renderer.
7. **Freshness = periodic content-hash sweep** (cron), not event plumbing.
8. **Every LLM/embedding/tool call routes through `cost.py`**; unknown model = loud failure.

---

## 1. Pluggability seams (what "experiment-ready" means concretely)

Three registries, all stored in the `advisor` schema so runs are reproducible:

- **`index_configs`** — one row per (embedding_model, dim, distance, chunker_version,
  preprocessing flags). Each config owns its own embeddings table
  (`advisor.emb_{config_id}`); chunks and documents are shared across configs. The retrieval
  pipeline takes a `config_id`. Exactly one config has `status='active'` (serves the agent);
  others are `experimental`. Corpus is small enough that building N parallel indexes is
  pennies and minutes — exploit that.
- **Generator registry** — a thin factory: `get_chat_model(role) -> ChatGenerator`, keyed by
  role (`agent`, `profile_summary`, `record_summary`, `condense`, `judge`) with the model id
  per role in config, not code. Haystack generators are provider-agnostic
  (Anthropic/OpenAI/Google; DeepSeek via its OpenAI-compatible endpoint), and `cost.py` is
  already multi-provider.
- **`SearchProvider` tool interface** — `web_search(query) -> [SearchFinding]` and
  `fetch_url(url) -> markdown`, where `SearchFinding = {title, url, snippet_or_content,
  published_at?}`. Each candidate (§Phase 3, E3) implements this interface so the agent's tool
  schema never changes when the provider swaps. **Contract requirement: every finding must
  carry a real, fetchable URL** — the citation validator depends on it.

Run manifests: every bake-off run writes a JSONL manifest (config ids, model ids, git SHA,
per-question results, cost from `cost.py`) under `experiments/runs/`. No silent truncation of
model outputs in manifests.

---

## 2. Phases

### Phase 0 — Foundations & go/no-go spikes *(≈1–2 days)*

Schema + credentials + the checks that decide whether the Haystack path proceeds.

- [ ] Decide fate of leftovers on test: `advisor_obs` (8.77M rows — truncate or drop),
      `advisor_evidence` (drop after noting the 1536-dim precedent). ⚠️ *David confirms drop.*
- [ ] `CREATE SCHEMA advisor;` on test; enable `CREATE EXTENSION vector` on **prod** (one-time;
      decide who runs it against prod). DDL per Appendix A.
- [ ] Secrets: add `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`, `DEEPSEEK_API_KEY` (present) to
      `.env`; confirm Langfuse keys work.
- [ ] **Spike A (cost):** one cached AnthropicChatGenerator call → assert
      `cache_creation_input_tokens`/`cache_read_input_tokens` arrive in `reply.meta["usage"]`
      and land correctly through `Usage.from_haystack_meta`. If missing: wrap usage extraction
      around the raw client response (thin adapter), file upstream issue.
- [ ] **Spike B (server tools):** pass Anthropic server-side `web_search`/`web_fetch` tool
      blocks through the integration in an Agent loop. If blocked: E3 still proceeds — the
      Anthropic candidate just runs through a thin direct-SDK tool instead.
- [ ] **Spike C (models):** current + newest Claude model ids and thinking kwargs don't 400
      through the integration.
- [x] **Spike D (DeepSeek search): RESOLVED** — a working implementation exists in
      `/home/david/code/davidsvaughn/cedar/alpha-claw/alpha_claw/strategy/engine/deepseek_agentic_provider.py`
      (empirically verified there 2026-06-22). The contract: DeepSeek's server-side web search
      works **only via its Anthropic-compatible endpoint** (`https://api.deepseek.com/anthropic`)
      using the Anthropic Messages server tool `{"type": "web_search_20250305", "name":
      "web_search", "max_uses": N}` (`server_tool_use`/`web_search_tool_result` blocks); the
      OpenAI-compatible `/chat/completions` endpoint **rejects** web-search tools. Citations
      come back as `{url, title}` from both text-block `citations` and tool-result blocks —
      satisfies our citation contract. alpha-claw's `research()` mode (search → grounded
      findings + citations, no decision) is exactly the E3 "delegated searcher" shape; port it
      (httpx-only, ~150 lines). Note flash defaults to single-round search and truncates at
      low `max_tokens` — raise `search_max_tokens` for research use. Remaining sub-task:
      confirm what the search tool itself bills (tokens only vs per-search fee) from a live
      call's usage block, then add the `deepseek` `_tool_web_search` row to `cost.py`.
      *(V4-Flash token pricing verified: $0.14/1M in, $0.28/1M out, $0.0028 cache-hit.)*
- [ ] PgBouncer decision or per-worker store instances (review §6.1.3) noted in deploy config.
- **Gate:** Spikes A–C pass (or have working adapters) → continue on Haystack. Two or more
  hard-fail → switch Phase 4 to the thin-stack (direct SDK tool runner); Phases 1–3 are
  framework-independent and unaffected either way.

### Phase 1 — Ingestion slice, breadth-first *(≈2–4 days)*

Target: every evaluated startup + all entity profiles indexed, with ACL metadata.

- [ ] `advisor.documents` / `doc_chunks` / `entity_profiles` tables (Appendix A).
- [ ] **Eval-bundle backfill**: walk `startup_company_evaluations.raw_json` GCS pointers
      (handle both path generations: `reports/{slug}_{uuid}.json` and
      `evaluations/{id}_{time}.json`); ingest `premium_markdown`, `basic_markdown`,
      `pitchDeckContent`, `websiteContent`, `section_results[].analysis+rawFindings` as
      documents with provenance + `eval_is_visible` + purchase-gating metadata. Zero LLM cost.
- [ ] **Entity profile renderer**: startups (+team) from the `ReportChatService` field lists;
      investors, CVs (+experiences/education, polymorphic `companyable` names), funds, orgs.
      TipTap HTML→markdown (`markdownify` or equivalent); label arrays resolved through
      lookup tables into normalized filter metadata; latest eval score on the startup card.
      `content_hash` per profile for the freshness sweep.
- [ ] **Chunker v1**: structure-aware, block-level only. Split on `# Page N` (decks: page =
      slide = chunk), headings, and paragraph groups; each doc also gets one **record-summary
      block** (LLM-written; role=`record_summary` via the generator registry — this is where
      a cheap-model experiment lands later). Stable `block_index`; re-ingest =
      version-and-append (old chunks marked superseded, still citation-resolvable).
- [ ] ACL stamping on every chunk (visibility, is_published, entity refs, eval gates,
      derived-doc provenance rule: max-restrictive inheritance).
- **Exit criteria:** ≥ 180 eval-derived docs + ~700 entity profiles ingested on test; spot
  check 10 random chunks for correct ACL metadata and page numbers.

### Phase 2 — Retrieval + golden set *(≈2–3 days, before any bake-off)*

- [ ] Hybrid pipeline: `PgvectorEmbeddingRetriever` + `PgvectorKeywordRetriever`
      (`websearch_to_tsquery`-style tuning where possible, `unaccent`) → RRF join →
      (optional reranker slot) → **group-by-entity diversification** (window top-k per
      document/entity in SQL).
- [ ] Retrieval is a function of `(query, filters, config_id)` — the bake-off entry point.
- [ ] **Golden set v1**: 40–60 questions with expected source docs/entities, spanning:
      entity lookup ("what does X do"), cross-doc ("compare X's traction claims vs eval
      findings"), filtered ("seed-stage fintechs in Europe"), aggregate ("how many…"),
      team/person, investor-thesis, and **10–15 web-required questions** (for E3). Store as
      YAML in `experiments/golden/`; grade retrieval by recall@k/MRR, answers by
      LLM-as-judge groundedness + citation-resolvability (mechanical).
- [ ] Harness CLI: `python -m experiments.run --config <id> --golden v1` → manifest JSONL +
      cost summary.
- **Exit criteria:** harness runs end-to-end on the active config; baseline numbers recorded.

### Phase 3 — Bake-offs *(≈3–5 days, parallelizable; each ends with a DECISIONS.md entry)*

**E1 — Embeddings** *(run first; everything downstream depends on it)*
Candidates: `voyage-4` (1024d), `voyage-4-lite` (1024d), `text-embedding-3-small` (1536d),
`voyage-context-4` (1024d, contextualized chunks — the interesting one for deck slides / CV
sections). Optional stretch: `gemini-embedding-2` (needs MRL truncation or halfvec).
One `index_config` + embeddings table each; full-corpus embed per candidate costs cents.
Metrics: recall@10/MRR on golden set; ties broken by cost and by 200M-free-tier headroom.
→ Winner becomes `status='active'`; pin row in `cost.py`; record dim in config registry.

**E2 — Reranker** *(after E1 winner fixed)*
Candidates: **none** (RRF only — genuinely plausible at this corpus size), Voyage
`rerank-2.5-lite`, Voyage `rerank-2.5`, Jina reranker v3 (key already in `.env`; Haystack has
`JinaRanker`). Blend per pipeshub `0.3·dense + 0.7·rerank` as starting point; log fusion as a
serializable expression. Metrics: nDCG@5/answer groundedness delta vs "none", latency, $/query.

**E3 — Web search** *(independent of E1/E2; uses the web-question golden subset)*
Candidates, all behind the `SearchProvider` interface:
  1. **Anthropic server-side `web_search` + `web_fetch`** ($10/1k + tokens; enforced
     citations; dynamic filtering on newer tool versions).
  2. **DeepSeek delegated searcher** — `deepseek-v4-flash` with its web search enabled,
     wrapped as a *sub-agent tool*: the main agent calls `web_research(question)`, the
     DeepSeek call runs its native search and returns findings **with URLs**, which the main
     agent treats like any other evidence. Contingent on Spike D confirming the API contract.
     If token pricing dominates, this is plausibly the cheapest content-bearing option.
  3. **Serper + fetch** ($0.30–1/1k, snippets + our own `web_fetch`/extraction) — the cheap
     client-side baseline.
  4. *(Optional)* Exa ($7/1k) only if "find startups similar to X" web-discovery questions
     make it into the golden set.
Metrics: answer quality (judge), **citation resolvability** (every web citation must carry a
real URL that fetches), freshness, latency, $/question end-to-end (tool + tokens, from
`cost.py`). Note per review: mixed setups are fine — e.g. Anthropic `web_fetch` (free per
call) can serve as the fetcher for candidates 2–3.

**E4 — Generation models** *(cheap to run continuously once the harness exists)*
Two separate questions:
  a. **Main agent model**: Sonnet vs Opus tiers on the full golden set (quality vs $/turn —
     expect Sonnet-class to win on value; decide with data). The plan keeps Claude as the
     default main-agent family (AnthropicChatGenerator, prompt caching, §9 context
     discipline), but the generator registry makes a cross-provider check (e.g.
     `deepseek-v4-pro`, `gemini-3.1-pro`) a config change if we want the datapoint.
  b. **Ingestion/sub-task roles** (record summaries, profile summaries, condense, judge):
     this is where volume lives, and where cheap models earn their keep — candidates:
     `claude-haiku-4-5`, `gemini-3-flash-preview` (already the extraction model),
     `deepseek-v4-flash`, `gpt-5-mini`. Judge quality per role on ~20 samples, pick per-role.

**E5 — Lexical leg** *(deferred; only if E1/E2 show recall misses on keyword-ish queries)*
Ladder per review §4.4: tuned FTS → BGE-M3 sparse in `sparsevec` → app-layer BM25.

- **Exit criteria for Phase 3:** four dated entries in `DECISIONS.md` (embedding, reranker,
  web search, per-role models), each with the manifest path and the runner-up named as
  fallback.

### Phase 4 — Tier-1 agent assembly *(≈3–5 days)*

- [ ] Haystack `Agent` (or thin-stack equivalent if the Phase-0 gate flipped): tools =
      `search_corpus(query, filters)`, `get_source(document_id, block_index | source_id,
      span?)`, `structured_query(name, params)` (registry starts with 3–5 queries:
      `count_startups`, `list_startups`, `top_by_score`, `investments_by_investor`),
      `web_search`/`fetch_url` (E3 winner). `exit_conditions=["text"]`,
      `max_agent_steps` ~8, per-turn tool budget.
- [ ] **Citation layer** (plain Python, framework-independent): tiny refs (`ref1…`) on every
      evidence block shown to the model; "omit rather than guess" prompt rule; post-validator
      that repairs/dedupes/resolves/renumbers and **drops non-resolving refs**; internal
      citations resolve to `(document_id, block_index, page_number)`, web to `{url}`.
- [ ] **Access filter**: requesting-user → *guardrail-class* resolver (SQL over
      `is_published`, doc `visibility`, `report_purchases`, ownership for draft/private
      carve-outs, `is_admin`), applied as mandatory retriever filter. Default is OPEN
      cross-user research (§0.2) — the filter removes gated *classes*, it never scopes
      results to the requester's own entities. Smoke-test with admin auth first, then open
      to founders/investors with the same policy (config, not code).
- [ ] Context discipline per §9: compact tool results (keep ids/URLs, truncate bodies, flag
      `_truncated`, never drop items), last-5-turns verbatim + summary, stable prompt prefix
      (+ CI assertion of prefix stability), token budget.
- [ ] Persistence: `advisor.threads` / `advisor.turns` with the per-turn research record
      (evidence set, tool calls, citations, cost) — feeds the eval set and future indexing of
      research notes (with max-restrictive ACL inheritance).
- [ ] `cost.py` `Tracker` wired to every call; per-turn soft cap; Langfuse tracing on.
- **Exit criteria:** 20 golden questions answered end-to-end with ≥95% resolvable citations,
  full cost visibility per turn, zero ACL violations on a seeded private-doc probe set.

### Phase 5 — Serving & UI *(≈2–4 days, can start during Phase 4)*

- [ ] FastAPI service, SSE streaming chat endpoint; Cloud Run deploy (Cloud Build, same
      pattern as the rest of the platform); Cloud SQL Python connector (no proxy sidecar);
      PgBouncer/per-worker-store per Phase 0 decision.
- [ ] Auth: signed user token from the Laravel session → service (the service must know the
      requesting user for ACL); admin-gated in v1.
- [ ] UI: the app already ships `@assistant-ui/react` — wire a chat page to the SSE endpoint;
      citations deep-link via `get_source` (slide thumbnail for deck pages).
- [ ] Ops: JSONL cost-ledger rotation; error surfaces (extraction failures, provider outages)
      degrade gracefully with honest messages.

### Phase 6 — Coverage & freshness *(≈2–3 days)*

- [ ] **Extraction path for never-evaluated docs** (prod has no standalone extraction blobs):
      preferred = small HTTP endpoint on x1-backend wrapping `x1-extract` (one extraction
      implementation, same `(content_hash, config_hash)` contract); fallback = Python port.
      Covers private decks, CVs in `portfolio_documents/`, non-deck docs.
- [ ] **Freshness sweep**: cron (Cloud Scheduler → service endpoint) comparing `updated_at` +
      content hashes across entities/docs/evals; re-render changed profiles, ingest new
      bundles/uploads; version-and-append on change.
- [ ] Prod cutover: enable extension, run backfill against prod (read-only on app tables;
      writes only to `advisor` schema), admin smoke test.

### Later (evidence-driven, explicitly not scheduled)

Tier-2 deep mode (converged shape; explicit "go deep" user action; budgets from §7 of the
design), auto-router (train on observed escalations), sentence-granularity indexing,
block-level diff-indexing, layout-aware extraction for sub-page citations (Docling first
candidate), sparse-vector lexical leg, founder/investor audience rollout (ACL is ready).

---

## 3. Cost envelope (order-of-magnitude, from verified July-2026 prices)

- **One-time indexing** (full corpus, per embedding config): ~1–3M tokens ≈ **$0.06–0.36**
  (voyage-4) — N parallel configs for the bake-off round to well under $5.
- **Record/profile summaries** (LLM, one-time + on change): ~900 docs/profiles × ~2k tokens
  ≈ **$5–30** depending on E4b winner.
- **Per Tier-1 turn** (Sonnet-class, cached prefix, 3–6 tool calls): ~**$0.03–0.15**;
  web-heavy turns + Anthropic search: +$0.01–0.05. Bake-off rounds (60 questions × ~8
  configs): **single-digit dollars per full sweep**.
- Budgets enforced by `Tracker` per-run soft cap; daily ledger cap once the ledger sink lands.

## 4. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Haystack integration gaps bite late | Phase-0 spikes as gate; framework-independent core (§1); named thin-stack exit ramp |
| DeepSeek web-search API contract ≠ chat product | Spike D before E3; candidate degrades to "DeepSeek + Serper results in-context" or drops out |
| Guardrail mistakes leak gated classes (private docs, unpurchased premium text, PII) | ACL-class metadata from day 1; mandatory filter (not prompt-level); seeded probe set in Phase-4 exit criteria; never-index list applied at ingest |
| Over-restriction kills the product (walls around other users' data) | §0.2 is explicit: default-open cross-user research; guardrails are class-based only; golden set includes cross-user research questions that MUST pass |
| Golden set too easy → bake-offs can't discriminate | Include hard negatives (similar startups), filtered + web questions; grow set from real usage (threads/turns) |
| Model/pricing drift during experiments | `cost.py` raises on unknown models; manifests pin model ids + git SHA |
| Prod app-table load from sweeps | Read-only role, off-peak cron, row-count-bounded queries |

## 5. Open items needing David

1. Guardrail-class treatments (§0.2 — audience itself is DECIDED: founders + investors,
   cross-user, default-open):
   a. **`private`-visibility documents** in cross-user answers: (i) fully excluded,
      (ii) *recommended:* verbatim deck content excluded but platform-authored eval-derived
      findings about that startup remain researchable, or (iii) fully researchable.
   b. **Premium eval report full text**: keep purchase-gated per requester (recommended —
      it's revenue; scores + basic summaries open to all), or open it.
   c. Confirm the never-index list (contact emails, invite/claim/share tokens, lat/long?).
2. Confirm drop/truncate of `advisor_obs` + `advisor_evidence` leftovers (Phase 0).
3. Who runs `CREATE EXTENSION vector` on prod, and when (Phase 0 / Phase 6 cutover).
4. Budget comfort: per-turn soft cap default (proposal: $0.50) and daily cap (proposal: $20
   during development).
5. Whether E4a should include non-Claude main-agent candidates for the datapoint, or stay
   Claude-tier-only.

---

## Appendix A — `advisor` schema DDL sketch (v1)

```sql
CREATE SCHEMA IF NOT EXISTS advisor;

CREATE TABLE advisor.index_configs (
  id            text PRIMARY KEY,              -- e.g. 'voyage4_1024_ck1'
  embedding_model text NOT NULL,
  dim           int  NOT NULL,
  distance      text NOT NULL DEFAULT 'cosine',
  chunker_version text NOT NULL,
  status        text NOT NULL DEFAULT 'experimental',  -- 'active' | 'experimental' | 'retired'
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE advisor.documents (
  id            bigserial PRIMARY KEY,
  source_type   text NOT NULL,   -- 'upload'|'website'|'eval_premium'|'eval_basic'|'eval_section'|'deck_extract'|'research_note'|'profile'
  entity_type   text, entity_id bigint,        -- loose FK to app entity
  title         text, markdown text NOT NULL,
  source_ref    text,                          -- GCS path of origin (bundle/binary), if any
  version       int  NOT NULL DEFAULT 1,
  superseded_by bigint REFERENCES advisor.documents(id),
  content_hash  text NOT NULL,
  extraction_model text, extraction_config text,
  -- ACL (denormalized onto chunk metadata at index time)
  visibility    text NOT NULL DEFAULT 'x1',    -- 'private'|'x1'|'public'
  is_published  boolean NOT NULL DEFAULT true,
  eval_is_visible boolean,
  acl_source    jsonb,                         -- provenance for derived docs (max-restrictive)
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE advisor.doc_chunks (
  id            bigserial PRIMARY KEY,
  document_id   bigint NOT NULL REFERENCES advisor.documents(id),
  block_index   int NOT NULL,                  -- stable; the citation primitive
  granularity   text NOT NULL DEFAULT 'block', -- 'block'|'record_summary'  (no 'sentence' in v1)
  text          text NOT NULL,
  page_number   int,                           -- parsed from '# Page N'; null for non-paged
  char_span     int4range,
  metadata      jsonb NOT NULL DEFAULT '{}',   -- entity refs, stage, industry, region, ACL fields
  UNIQUE (document_id, block_index)
);

-- one per index_config, created by the harness:
--   CREATE TABLE advisor.emb_{config_id} (
--     chunk_id bigint PRIMARY KEY REFERENCES advisor.doc_chunks(id),
--     embedding vector({dim}) NOT NULL );
--   + HNSW index (or exact scan at current scale)

CREATE TABLE advisor.entity_profiles (
  entity_type   text NOT NULL, entity_id bigint NOT NULL,
  document_id   bigint NOT NULL REFERENCES advisor.documents(id),
  content_hash  text NOT NULL,
  rendered_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (entity_type, entity_id)
);

CREATE TABLE advisor.threads (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL,
  title text, created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE advisor.turns (
  id bigserial PRIMARY KEY,
  thread_id bigint NOT NULL REFERENCES advisor.threads(id),
  role text NOT NULL,                          -- 'user'|'assistant'
  content text NOT NULL,
  research_record jsonb,                       -- evidence ids, tool calls, citations, cost
  cost_usd numeric(10,6),
  created_at timestamptz NOT NULL DEFAULT now()
);
```

*(Full-text: GIN index on `to_tsvector('english', doc_chunks.text)`; `unaccent` in the query
path. All app-table access via a read-only role.)*

## Appendix B — experiment manifest shape

```jsonc
// experiments/runs/2026-07-08_e1_voyage4.jsonl — one line per golden question
{"run_id": "...", "experiment": "E1", "config_id": "voyage4_1024_ck1",
 "git_sha": "...", "question_id": "g014", "retrieved": [...], "recall_at_10": 1.0,
 "mrr": 0.5, "answer": "...", "judge": {"grounded": true, "score": 4},
 "citations": {"emitted": 5, "resolved": 5}, "cost_usd": 0.0042,
 "latency_ms": 3100, "model_ids": {"agent": "...", "embed": "voyage-4"}}
```
