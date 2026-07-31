# X1 Advisor — Implementation Plan

> Status: **working plan** (2026-07-07, **revised 2026-07-30** — see §R below).
> Operationalizes [`ARCHITECTURE.md`](ARCHITECTURE.md)
> (the design) as amended by [`ARCHITECTURE-REVIEW.md`](ARCHITECTURE-REVIEW.md) (the review).
> Where the two disagree, this plan follows the review. Supersedes the "Suggested next step"
> section of [`HANDOFF.md`](HANDOFF.md).

---

## §R. Revision — 2026-07-30 (supersedes the phase-exit shorthand below)

Two independent design reviews
([`DESIGN-REVIEW-2026-07-30.md`](DESIGN-REVIEW-2026-07-30.md) self-audit,
[`ARCHITECTURE-PLAN-REVIEW-2026-07-30.md`](ARCHITECTURE-PLAN-REVIEW-2026-07-30.md)
second-agent) converged on: the architecture is sound, but the project is a
**validated admin prototype**, not "Phases 0–4 complete" — the phase exit
criteria were met as written but were too weak to support the conclusions drawn
from them. The §2 phase checklists below stand as **historical record**;
current truth is this readiness matrix and the gate sequence that follows.

### Readiness matrix (2026-07-30)

| Area | State |
|---|---|
| Test-corpus ingestion | Prototype validated |
| Retrieval | Prototype validated; **evidence-boundary correction required** (record summaries are citable — must become retrieval-only) |
| Answer quality | **Not yet adequately measured** (citation resolvability ≠ faithfulness; no judge) |
| ACL | Retrieval-level filter validated; **end-to-end boundary incomplete** (structured_query unfiltered, filter-key SQL injection, thread ownership) |
| Service runtime | Skeleton only; **not concurrency-safe** (shared connection/transaction) |
| Production data coverage & freshness | Not established (historical ingestion Phase 6 not started; distinct from revised Gate 6) |
| Admin pilot / non-admin exposure | Not ready (gates 3 / 6) |

### Scope decision (David, 2026-07-30)

The advisor is a **research agent, not an actor**. No app-mutating or
UI-driving capabilities; out-of-scope action requests get a graceful decline.
Page context flows **into** the advisor only (see context-snapshot design).

### Active proposals (docs adopted into this plan)

- [`QA-LOOP-DESIGN-2026-07-30.md`](QA-LOOP-DESIGN-2026-07-30.md) — teacher-QA
  observability: turn bundles, retrieval explain, funnel classification,
  replay/compare. Reviewed and revised same-day.
- [`QUESTION-BANK.md`](QUESTION-BANK.md) — master test-question corpus
  (13 recovered sources); seed for golden v2; 7 architecture implications.
  Reviewed and revised same-day.
- [`CONTEXT-SNAPSHOT-DESIGN-2026-07-30.md`](CONTEXT-SNAPSHOT-DESIGN-2026-07-30.md)
  — page-context/working-set architecture (app → advisor only).

All three were reviewed same-day
([`QA-BANK-CONTEXT-REVIEW-2026-07-30.md`](QA-BANK-CONTEXT-REVIEW-2026-07-30.md):
coherent bet, no fatal problems) and **revised per its §7** — headline
corrections: extensional snapshots (replayability), `scan_text` vs
`analyze_scope` split (two future capabilities, not one), three replay modes,
acceptable-evidence groups, replay never trusts stored ACLs.

### Revised execution sequence (merges the independent review's Gates 1–6)

- **Step 0 — immediate fixes — ✅ DONE 2026-07-30** (`711de6f`, `382b687`,
  `e72ef89`, `4d5e1da`, `37684a0`; see DECISIONS 2026-07-30 for evidence and
  three carry-forward findings): structured-query ACL (the
  `evaluations_for_company` leak was live on test, and the same predicate was
  missing from three sibling queries — fixed as a class); filter-key whitelist
  → typed filter layer (F1/F7); in-process psycopg pool (F2); manifest
  no-clobber; persist `raw_answer` (F5). Golden v1 unchanged at recall@10
  0.833 / MRR 0.746, so none of it moved retrieval.
- **Gate 1 — evidence correctness + QA loop, in internal order** (review
  §6.2): **1A observability foundation — ✅ DONE 2026-07-30** (`e19d8e5`,
  `bef0bd0`, `f90e37f`, + `4d5e1da`; DECISIONS 2026-07-30): turn bundles +
  fingerprints, retrieval explain, manifest immutability, non-git owner-only
  storage. It measured 1B's case on first run — **38% of "100% resolvable"
  citations point at record summaries** — and found that the lexical leg is
  silent on 21 of 35 golden questions (Gate 5) → **1B evidence correction —
  ✅ DONE 2026-07-31** (`12e9bee`, `593ff23`, `4561bb2`, `362703b`, `40240cb`;
  DECISIONS 2026-07-31): record summaries retrieval-only with source-block
  expansion, whole-document summaries, calibrated claim/citation judge,
  structured rows as citable platform data, full suite rerun. **Citations on
  generated summaries 38% → 0; zero-citation answers 2 → 0; cost/turn flat.**
  Answer quality measured for the first time: faithfulness 0.584, citation
  coverage 0.813. ⚠️ Two carry-overs, neither blocking 1C: the judge is
  **`synthetic-only` calibrated** — no faithfulness number may be quoted as
  established until ≥30 human labels exist — and golden v1 exercises the new
  platform-data path exactly once, so Gate 4 must add aggregate/list coverage
  or 1B-4 stays effectively untested → **1C loop completion** (funnel
  classifier, three replay modes, run comparator, teacher runbook). The small
  evidence-boundary fix is never delayed by the full QA package.
- **Gate 2 — security boundary end-to-end:** one request-auth context consumed
  by every data-bearing tool/endpoint; ACL-aware structured queries; thread
  ownership; **bundle-read + replay authorization and stale-ACL handling**;
  persisted citation/source endpoint; context-scope∩ACL tests; admin-shadow QA
  artifact isolation; injection tests; per-probe admin-scope positive controls.
- **Gate 3A — production-safe admin pilot:** server-owned history; request
  limits/timeouts/backpressure; bounded DB/model concurrency; enforceable cost
  budgets; SSE protocol (with citation post-validation semantics); minimal prod
  backfill rehearsal + coverage registry.
- **Gate 3B — context-snapshot support** (before golden v2; not blocking the
  plain-chat pilot): context schema + `context_status`, selected/visible/
  working-set scope handles, polymorphic typed refs, extensional resolved-scope
  persistence, app-side complete-membership projection using the page's exact
  query builder (inline refs or a user-bound materialized snapshot above the
  configured limit), context fixtures + scope grading.
- **Gate 4 — golden v2:** curate (not copy) QUESTION-BANK — smoke ~12 /
  core ~40–60 + scripts / extended ~80–100 tiers; acceptable evidence groups;
  `expected_route` + `expected_scope` + `context_fixture`; genuinely-blind
  held-out cases executed by a separately authorized CI/evaluation service
  (case bodies are not mounted in the teacher workspace); real-thread
  weighting; exact-scan vs semantic-analysis cases separated. Future
  capabilities named by the bank: **`scan_text`**
  (deterministic bounded text scan — build first) and **`analyze_scope`**
  (budgeted semantic map/reduce — on demonstrated demand).
- **Gate 5 — provider/model experiments:** generator/embedding/search seams
  (D1/D2 + SearchProvider), then E1/E3/E4 via immutable paired manifests.
  RRF-only and current models stay provisional until then. Two experiments were
  promoted out of "deferred" by Gate 1A measurements (§2 Phase 3):
  **E5 lexical-leg query preprocessing** — the leg returns nothing for 21 of 35
  golden questions, so the hybrid is dense-only in the majority case — and
  **E6 chunk contextualization vs. document summaries**, which tests whether
  per-chunk context prefixes make the record-summary class unnecessary
  altogether.
- **Gate 6 — non-admin exposure:** policy finalization (private docs, premium,
  existence disclosure), persona suite, audience opens only after every path
  consumes the same verified auth context.
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

Run manifests: every bake-off writes a body-free JSONL comparison manifest
(config ids, model ids, git SHA, per-question labels/metrics, policy-safe opaque
evidence ids, cost from `cost.py`) under `experiments/runs/`; restricted source
identities are omitted or pseudonymized. Complete model messages and tool-result
bodies belong only in gitignored `.qa-artifacts/runs/` or the canonical
authorized JSONB bundle; they are never committed. No silent truncation is
allowed inside the complete bundle. The committed v1 manifests predate this
split and contain answers/source metadata; Gate 1A reviews them as historical
test-corpus artifacts and migrates the writer rather than using them as the v2
security template.

---

## 2. Phases

### Phase 0 — Foundations & go/no-go spikes *(≈1–2 days)*

Schema + credentials + the checks that decide whether the Haystack path proceeds.

- [ ] Decide fate of leftovers on test: `advisor_obs` (measured 2026-07-07: **10 GB**, events
      8.77M rows — truncate or drop), `advisor_evidence` (**133 MB**, 8,268 rows of
      `vector(1536)` HNSW cosine — drop after noting the dim precedent). ⚠️ *David confirms drop.*
- [x] `CREATE SCHEMA advisor;` on test **(done 2026-07-07; pgvector 0.8.1 already ENABLED on
      test)**; ⚠️ still open: enable `CREATE EXTENSION vector` on **prod** (one-time;
      decide who runs it against prod). DDL per Appendix A.
- [ ] Secrets: add `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY` ⚠️ *(both still missing — David)*;
      ⚠️ **`OPENAI_API_KEY` in `.env` is INVALID (401, found 2026-07-08) — David must refresh
      it (it's the company-paid default provider now, see DECISIONS 2026-07-08)**;
      `DEEPSEEK_API_KEY` present (David's personal key — opt-in use only); Langfuse keys
      **verified working 2026-07-07** (project `x1-backend-agentic`).
- [ ] **Spike A (cost):** one cached AnthropicChatGenerator call → assert
      `cache_creation_input_tokens`/`cache_read_input_tokens` arrive in `reply.meta["usage"]`
      and land correctly through `Usage.from_haystack_meta`. If missing: wrap usage extraction
      around the raw client response (thin adapter), file upstream issue.
      *Script ready: `spikes/spike_a_cache_usage.py` — blocked on `ANTHROPIC_API_KEY`.*
- [ ] **Spike B (server tools):** pass Anthropic server-side `web_search`/`web_fetch` tool
      blocks through the integration in an Agent loop. If blocked: E3 still proceeds — the
      Anthropic candidate just runs through a thin direct-SDK tool instead.
      *Script ready: `spikes/spike_b_server_tools.py` — blocked on `ANTHROPIC_API_KEY`.*
- [ ] **Spike C (models):** current + newest Claude model ids and thinking kwargs don't 400
      through the integration.
      *Script ready: `spikes/spike_c_model_ids.py` — blocked on `ANTHROPIC_API_KEY`.*
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
      low `max_tokens` — raise `search_max_tokens` for research use.
      **Billing sub-task CLOSED 2026-07-07** (`spikes/spike_d_deepseek_billing.py`, live call):
      search bills **tokens only** — usage reports `server_tool_use.web_search_requests` but
      no fee field, results are injected as input tokens (~5.5k for a one-line question), and
      the official pricing page has no per-search line item. `cost.py` now carries an explicit
      $0 `_tool_web_search` row for deepseek + Anthropic-shaped-usage detection (the endpoint
      returns `input_tokens`/`cache_read_input_tokens`, not `prompt_tokens`). Full grounded
      call: **$0.000825**. See DECISIONS.md.
      *(V4-Flash token pricing verified: $0.14/1M in, $0.28/1M out, $0.0028 cache-hit.)*
- [x] PgBouncer decision or per-worker store instances (review §6.1.3): **per-worker store
      instances, no PgBouncer** (2026-07-07) — see DECISIONS.md for rationale + revisit trigger.
- **Gate (re-scoped 2026-07-08, DECISIONS.md):** evaluated on the providers we hold keys
  for — OpenAI (company-paid default for chat/embeddings/web search) with DeepSeek as the
  opt-in alternate. Provider-swapped spikes: `spike_a2_openai_cache_usage.py`,
  `spike_b2_agent_web_search.py` (Agent loop + delegated-searcher Tool — the Phase-4 shape),
  `spike_c2_openai_deepseek_models.py` (chat models + embedder). These pass → continue on
  Haystack. Two or more hard-fail → switch Phase 4 to the thin-stack; Phases 1–3 are
  framework-independent either way. The original Anthropic spikes A–C stay on the shelf,
  **non-blocking**, to run whenever `ANTHROPIC_API_KEY` lands.
  **GATE PASSED 2026-07-08 — all three provider-swapped spikes green → continue on
  Haystack.** A′: OpenAI cached_tokens arrive in `reply.meta` and price correctly
  (3,328/3,385 tokens cache-read on call 2). B′: Agent loop + OpenAI web-search
  delegated-searcher tool end-to-end, 37 resolvable citations (~$0.025/search-call —
  note: use `include=["web_search_call.action.sources"]`; inline url_citation
  annotations are unreliable on gpt-5.1). C′: gpt-5.1, gpt-5-mini,
  deepseek-v4-flash, text-embedding-3-small (1536d) all work + price. Details in
  DECISIONS.md.

### Phase 1 — Ingestion slice, breadth-first *(≈2–4 days)*

Target: every evaluated startup + all entity profiles indexed, with ACL metadata.

- [x] `advisor.documents` / `doc_chunks` / `entity_profiles` tables (Appendix A) —
      **applied on test 2026-07-08** (`x1_advisor/schema.sql`, idempotent).
- [x] **Eval-bundle backfill** (`x1_advisor/ingest/backfill_evals.py`) — walks both pointer
      forms; parser handles all FOUR original prod bundle generations (gen-0a/0b/1/2, see
      `bundles.py`); premium/basic/sections/deck/website docs with provenance +
      `eval_is_visible` + purchase-gating metadata. Zero LLM cost. **Note (2026-07-08):
      75/79 test bundles are an experimental shape — skipped loudly; 24 original-shape
      prod fixtures copied to `reports/prod_fixtures/` ingest via `--fixtures`. See
      DECISIONS.md (test-env drift).**
- [x] **Entity profile renderer** (`ingest/profiles.py` + `render_profiles.py`) — startups
      (+team) per ReportChatService field lists; investors, CVs (+experiences/education,
      polymorphic `companyable`), investment companies, funds, orgs. TipTap→markdown
      (markdownify); labels resolved to filter metadata; latest eval score on startup card;
      content_hash per profile; never-index list applied (emails/tokens/lat-long).
- [x] **Chunker v1** (`ingest/chunker.py`, `ck1`) — `# Page N` (deck page=slide=chunk),
      headings, paragraph groups; stable `block_index`; char_span verified; re-ingest =
      version-and-append. ⚠️ *Still open: the per-doc LLM record-summary block
      (role=`record_summary` via generator registry — gpt-5-mini candidate).*
- [x] ACL stamping on every chunk (`ingest/store.py`) — visibility, is_published, entity
      refs, eval gates, deck max-restrictive inheritance (private unless the source doc row
      upgrades it).
- **Exit criteria: MET on test 2026-07-08** — 412 live docs / 6,728 chunks: 270 eval-derived
  (≥180 ✓) + 142 profiles (= every entity on test; ~700 is the PROD entity count, reachable
  at cutover). 10-random-chunk spot check: ACL fields + gated premium + unpublished-profile
  flags correct; deck chunks paged.

### Phase 2 — Retrieval + golden set *(≈2–3 days, before any bake-off)*

- [x] Hybrid pipeline (`x1_advisor/retrieval.py`, 2026-07-08) — implemented as plain
      SQL/psycopg rather than the pgvector-haystack retrievers (Appendix A's shared-chunks +
      per-config emb tables don't fit the store's single-table model; review §6.2 noted the
      hybrid SQL is DIY either way): pgvector cosine + `websearch_to_tsquery` FTS → RRF →
      (reranker slot pending E2) → per-document diversification cap. ACL = mandatory
      retriever-level arg with class predicates (verified: non-admin loses premium chunks).
- [x] Retrieval is `retrieve(query, filters, config_id, acl, k)` — the bake-off entry point.
- [x] **Golden set v1** (`experiments/golden/v1.yaml`) — 45 questions (36 gradable + 9
      web-required for E3) across all planned categories, grounded in real corpus entities,
      hard negatives included (Accelium AG vs GmbH, duplicate people). Answer-side judge
      grading lands with Phase 3/E4.
- [x] Harness CLI: `python -m experiments.run --config <id> --golden v1` → JSONL manifest +
      metrics (recall@k, MRR, latency, cost).
- **Exit criteria: MET 2026-07-08** — baseline on `te3s_1536_ck1` (RRF only, no reranker):
  **recall@10 0.778, MRR 0.727**, 25/36 full recall; failures characterized in DECISIONS.md
  (aggregates await Phase-4 `structured_query`; that's by design).

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
*Working default while the bake-off pends (2026-07-08, DECISIONS.md): OpenAI server-side
`web_search` — company-paid. DeepSeek is candidate #2 and fully wired, but runs on David's
personal key today → opt-in only until a company DeepSeek key exists.*
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

**E5 — Lexical leg** *(no longer speculative — the trigger fired, 2026-07-30)*
Ladder per review §4.4: tuned FTS → BGE-M3 sparse in `sparsevec` → app-layer BM25.
Gate 1A's retrieval explain measured the leg **returning zero rows for 21 of 35 golden
questions and ≤1 for 23**: `websearch_to_tsquery` ANDs every stemmed term, so a
natural-language question matches almost nothing (*"What does the X1 Pipeline premium
report identify as key uncertainties?"* → 4 chunks; *"X1 Pipeline key uncertainties"* →
213). It is not dead weight when it fires — 25 returned hits across 11 questions came from
the lexical leg alone — so the first rung is **query preprocessing** (OR semantics,
stopword/interrogative stripping, or a keyword-extraction step) before any new index
technology. Cheap, and it may be most of the win.

**E6 — Chunk contextualization vs. document summaries** *(after E1; the two are entangled)*
The reason record summaries exist is **entity identity**: an eval-section chunk discusses
"the team" without ever naming the company, so it cannot be found by company name. Summaries
route around that with a separate node per document. *Contextual retrieval* attacks the
cause instead — prepend a short, chunk-specific context line ("This section is from
ArtCentrica's team evaluation…") to each chunk **before embedding**, so every chunk carries
its own identity. Candidates:
  1. **Current**: record summaries as retrieval-only routers (Gate 1B baseline).
  2. **Contextual chunks**: generated context prefix on all ~7,281 blocks, no summary node.
  3. **Both** — they are not mutually exclusive, and the interaction is the real question.
  4. **`voyage-context-4`** (already an E1 candidate): the same idea moved into the embedding
     model rather than the text. Fold the comparison in if E1 runs it.
Cost: a generation pass over 7,281 blocks rather than 412 documents — bigger, but each call
is small and the source document caches across its own chunks. Metrics: recall@10/MRR on
golden v2, reported **separately for the entity-identity question class**, which is the class
this is meant to fix; plus $/re-index and re-index wall-clock, since freshness sweeps pay it
repeatedly.
*Secondary payoff if (2) wins outright:* the retrieval-only evidence class disappears
entirely — no summary nodes means no summary/expansion machinery and no
generated-text-as-citation hazard to police (Gate 1B-1 exists only because that class
exists). Weigh that simplification alongside the recall numbers, not after them.

- **Exit criteria for Phase 3:** four dated entries in `DECISIONS.md` (embedding, reranker,
  web search, per-role models), each with the manifest path and the runner-up named as
  fallback. E5/E6 land their own entries when run.

### Phase 4 — Tier-1 agent assembly *(≈3–5 days)*

- [ ] Haystack `Agent` (or thin-stack equivalent if the Phase-0 gate flipped): tools =
      `search_corpus(query, filters)`, `get_source(document_id, block_index | source_id,
      span?)`, `structured_query(name, params)` (registry starts with 3–5 queries:
      `count_startups`, `list_startups`, `top_by_score`, `investments_by_investor`),
      `web_search`/`fetch_url` (E3 winner). `exit_conditions=["text"]`,
      `max_agent_steps` ~8, per-turn tool budget.
- [x] **Citation layer** (`agent/evidence.py`, 2026-07-08): tiny refs, omit-rather-than-
      guess, post-validator (resolve/dedupe/renumber/drop), internal → (document_id,
      block_index, page_number), web → {url}.
- [x] **Access filter** (`retrieval._acl_sql` + `agent/tools.py`, 2026-07-08): class-based
      retriever-level filter (private, drafts, premium purchase-gating, hidden evals);
      gated-vs-absent messaging (class+count only, hidden evals never revealed).
      *Remaining: the requesting-user → class-dict SQL resolver (service auth, Phase 5).*
- [x] Context discipline per §9 (2026-07-08): compact flagged tool results + get_source
      escalation; last-5-turns verbatim + condensed older history (`agent.condense`,
      gpt-5-mini); byte-stable prompt prefix (verified via cached-token growth);
      per-step usage table on every run; step-budget guidance + graceful wrap-up
      synthesis at the cap. *Remaining: CI assertion of prefix stability.*
- [x] Persistence (`advisor.threads`/`turns` + `save_turn`, wired into service + chat REPL).
- [x] `cost.py` `Tracker` on every call; $0.50 per-turn soft cap; default-on JSONL ledger;
      **Langfuse tracing live** (telemetry.py, SDK v4; per-step usage/cost observations +
      citation_resolvability & cost_usd scores; verified via langfuse-cli).
- **Exit criteria: MET 2026-07-08** — 20-question agent run **100% resolvable citations**
  (63/63; bar ≥95%), mean $0.011/turn with full per-step visibility, seeded ACL probes
  (`experiments/acl_probes.py`) zero violations + positive purchase control.

### Phase 5 — Serving & UI *(≈2–4 days, can start during Phase 4)*

- [ ] FastAPI service, SSE streaming chat endpoint; Cloud Run deploy (Cloud Build, same
      pattern as the rest of the platform); Cloud SQL Python connector (no proxy sidecar);
      PgBouncer/per-worker-store per Phase 0 decision.
      *Started 2026-07-08: `x1_advisor/service.py` (/ask JSON + /health + thread
      persistence, per-worker connection, dev auth stub) smoke-tested locally;
      Dockerfile + cloudbuild.yaml written (trigger NOT wired; deploys are David's call).
      SSE token streaming still open.*
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
   d. **Existence disclosure — the docs currently hold two policies, and the code
      follows the older one.** `tools.py`'s gated-vs-absent note tells a non-admin
      how many restricted blocks exist *and their class*, including
      `"private document"` and `"unpublished draft"`
      ([`QA-LOOP-DESIGN`](QA-LOOP-DESIGN-2026-07-30.md) §4.3 also confines
      admin-shadow classification to persona QA, yet this runs on every empty
      non-admin search). [`QUESTION-BANK`](QUESTION-BANK.md) §7A recommends the
      narrower rule: **premium-class existence only; never private-doc existence**.
      Note this is *separate from* (a): (a) is whether private content is
      researchable, (d) is whether a stranger learns the document exists at all.
      *Recommended:* adopt the QUESTION-BANK rule — say "purchasable material
      exists" for premium, and treat private/draft exactly like absent — and make
      the admin-shadow retrieval conditional on that, since today it also doubles
      the retrieval cost of every empty non-admin search. Must be settled before
      Gate 6; harmless while the pilot is admin-only.
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
