# Decisions

> Dated, evidence-backed decisions per [`PLAN.md`](PLAN.md) §1 — bake-off outcomes and
> engineering choices land here, newest first. Each entry names its evidence (spike
> output, manifest path, or doc reference) and the revisit trigger if one exists.

## 2026-07-09 — Record-summary blocks land: recall@10 0.778 → 0.833 (best lever so far)

Phase-1 leftover closed (`ingest/summaries.py`): one gpt-5-mini record-summary chunk per
live document (`granularity='record_summary'`, block_index 10000, ACL inherited), 412
embedded. One-time cost **$0.20** (reasoning_effort=minimal + 8-way pool; serial
default-effort was ~20s/doc — 10× slower). Golden v1 after: **recall@10 0.833 (+0.055),
MRR 0.746, full recall 28/36, zero-recall 4/36** — a bigger lift than the E2 reranker
(which was a wash), at one-tenth the E2 evaluation cost. E4b note: gpt-5-mini at minimal
effort is the working record-summary model.

Also tonight: multi-turn history discipline (last-5 verbatim + gpt-5-mini condense;
verified follow-up coreference), default-on cost ledger, gated-vs-absent messaging
(class+count only; hidden evals never revealed), FastAPI service skeleton (+Dockerfile/
cloudbuild, trigger NOT wired), first unit tests incl. the §9 prompt-prefix-stability CI
assertion (SYSTEM_PROMPT hash pinned; 5/5 passing).

## 2026-07-08 — E2 reranker bake-off: RRF-only WINS (reranker adds nothing at this scale)

First Phase-3 bake-off decided. Candidate `jina-reranker-v3` (blend 0.3·rrf + 0.7·rerank
over the fused top-40) vs the RRF-only baseline, golden v1, config `te3s_1536_ck1`:

| metric | RRF-only | + jina-reranker-v3 |
|---|---|---|
| recall@10 | 0.778 | 0.792 |
| MRR | 0.727 | 0.718 |
| full recall | 25/36 | 26/36 |
| zero recall | 5/36 | **same 5** |

Manifests: `experiments/runs/2026-07-08_active_v1{,_rerank}.jsonl`. A wash — exactly the
outcome the plan flagged as "genuinely plausible at this corpus size". The five failures
are unchanged (they're tool-selection/data problems, not ranking problems), and the
reranker adds a dependency, latency, and free-tier 429 rate limits.

**Decision: v1 ships RRF-only.** Runner-up/fallback: `jina-reranker-v3` blend — the slot
is implemented (`retrieve(rerank=True)`, harness `--rerank`), so re-evaluating after the
prod cutover (~2× corpus) or after golden v2 grows harder is one flag. Voyage rerank
candidates join when `VOYAGE_API_KEY` lands.

## 2026-07-08 — Phase-4 exit measurements: citations 100%, ACL probes PASS

**Agent-mode golden run** (`experiments.run --agent`, 20 questions, manifest
`experiments/runs/2026-07-08_agent_v1.jsonl`): **63/63 citations resolvable (100% —
bar was ≥95%)**, zero dropped refs; cost mean **$0.011/turn**, p50 $0.0096, max $0.05
(total $0.23 for the run); latency p50 10s, max 42s.

**Seeded ACL probes** (`experiments/acl_probes.py`): adversarial queries per gated
class under a no-rights persona and a purchaser persona → **zero violations**, and the
positive control (purchaser sees their purchased premium doc) passes.

**Instrumentation catch #5:** all four "filtered" list questions returned false
"nothing found" answers — the model passed `entity_type: "startup"` but the metadata
enum is `startup_company`, so every search matched nothing. Fixed in the tool contract
(values enumerated + common aliases normalized); re-test returns a cited 8-source list
at $0.009. Lesson reinforced: enum-valued tool params must enumerate their values in
the description.

Remaining for Phase-4 close-out: Langfuse tracing, multi-turn history discipline,
record-summary blocks (gpt-5-mini), "gated vs absent" messaging distinction.

## 2026-07-08 — Phase 4 first slice: agent runs end-to-end; context discipline verified live

**Shipped** (`x1_advisor/agent/`): citation layer (`evidence.py` — tiny refs, post-
validator resolves/dedupes/renumbers/drops; stats now distinct-emitted/resolved/dropped),
structured-query registry (`queries.py`, 5 read-only queries — the only SQL surface),
compact-by-construction tools (`tools.py` — snippets flagged `_truncated`, `get_source`
escalation, bounded web searcher), agent assembly (`advisor.py` — gpt-5.1 Haystack Agent,
byte-stable system prompt, max 8 steps, $0.50 soft cap, threads/turns persistence), CLI
(`ask.py` — prints a per-step usage table on every run).

**Context discipline (David's priority) verified on live runs:** prompt prefix caches
(cached tokens climb per step while uncached input stays flat ≈ tool-result tail only);
per-turn costs: corpus cross-doc $0.024, aggregate $0.004, corpus+web $0.049 — inside
the plan's §3 envelope. Per-step usage tables are printed on every ask.py run.

**Issues found by instrumented sample runs, all fixed:**
1. Sibling eval bundles yield near-identical chunks as distinct documents → retrieval
   now dedupes by text hash post-RRF.
2. `web_research` returned bare ref ids → model (correctly) omitted citations entirely;
   now returns attributable (ref, url, title) triples → 6/6 resolvable web citations.
3. Unbounded web searcher: 250s/$0.079 turn → `max_output_tokens=1200` + concise
   instructions → 36s/$0.049.
4. gpt-5.1 skipped web search on a "right now" question (known conservative triggering)
   → trigger conditions written into the tool description (stable product semantics).

**ACL probe:** non-purchaser asking for premium report content gets zero premium
evidence and an honest miss (retriever-level filter, not prompt-level). Phase-5 nicety:
distinguish "gated" from "absent" in the message. Still open for Phase-4 exit: 20-golden-
question end-to-end run (≥95% resolvable citations), seeded probe suite, Langfuse
tracing, record-summary blocks, multi-turn history discipline (last-5-verbatim+summary).

## 2026-07-08 — Phase 2 landed: hybrid retrieval + golden set + baseline (recall@10 0.778)

**Shipped:** embedding index registry (`x1_advisor/index.py`, config `te3s_1536_ck1` =
text-embedding-3-small/1536d/ck1, ACTIVE; 7,281 vectors embedded for **$0.066** total);
hybrid retrieval (`x1_advisor/retrieval.py` — pgvector cosine + `websearch_to_tsquery`
FTS → RRF(k=60) → per-document cap 3; plain SQL/psycopg, framework-independent; ACL is a
mandatory retriever-level argument with class predicates — private-doc exclusion, draft
owner-only, premium purchase-gating — verified live: non-admin loses gated premium
chunks); golden set v1 (`experiments/golden/v1.yaml`, 45 questions incl. hard negatives
+ 9 web-required); harness (`experiments/run.py` → JSONL manifests under
`experiments/runs/`).

**Baseline (2026-07-08_active_v1, 36 graded):** mean recall@10 **0.778**, MRR **0.727**,
25/36 full recall, 5 zero-recall, median latency ~420ms/query. Known failure modes, all
expected: aggregate questions (g023/g024 — they need Phase-4 `structured_query`, not
retrieval), one metadata-filter value mismatch (g020 fundraising_round), tiny-doc miss
(g012 website 140 chars), person-semantics miss (g031). These are the E2 reranker's and
Phase-4 tools' baselines to beat. Fixture company names fixed en route (gen-1 name lives
at `inputs.company.startup_companies_row`; gen-0a has none → filename-slug fallback).

## 2026-07-08 — Phase 1 ingestion slice landed on test; test-env drift documented

**Shipped** (`x1_advisor/ingest/`): eval-bundle backfill (`backfill_evals.py`), entity
profile renderer (`profiles.py` + `render_profiles.py`, field lists from
ReportChatService recon), chunker v1 (`chunker.py`, `ck1`: `# Page N`/heading/paragraph
blocks, stable block_index, char_span verified), ACL-stamped chunks (`store.py`,
version-and-append with per-section identity). **On test: 412 live documents / 6,728
chunks** — 270 eval-derived (25 premium [gated], 28 basic, 196 sections, 16 decks
[paged, page=slide], 5 website) + 142 profiles (50 startups incl. team, 75 CVs,
14 investors, 3 orgs). Idempotent re-runs verified (all `unchanged`). Never-index
list applied at ingest (emails, tokens, invite fields, lat/long). Still open from
Phase 1: LLM record-summary blocks (`granularity='record_summary'`, gpt-5-mini via
generator registry).

**Bundle contract (live-verified):** FOUR original generations in prod
(`gen-0b`, `gen-0a`, `gen-1`, `gen-2` — see `bundles.py` docstring) all parse; the
parser targets prod's shapes because the agent ships against prod.

**Test-env drift (David asked; confirmed 2026-07-08):**
1. **75 of 79 test eval bundles are an EXPERIMENTAL shape** (camelCase
   entityType/entityId/report; new pointer scheme
   `gs://…/evaluations/startup/{id}/{uuid}/bundle.json`, rows dated May 2–6). They are
   skipped loudly at ingest (`skipped:experimental_shape`), not parsed. The 4 most
   recent evals (May 31–Jun 2) are back on the original `reports/` scheme.
2. **Accompanying DB alteration — minor, confirmed:** migration
   `2026_05_01_000000_drop_redundant_columns_from_startup_company_evaluations_table`
   is applied on test but exists in no local repo; it dropped `market_score`,
   `product_score`, `traction_score`, `team_score`, `finance_score`, `summary`,
   `notes` (present in the repo's create migration). **Zero advisor impact** — the
   backfill reads only id/startup_company_id/raw_json/is_visible (+ startup
   is_published); category scores come from inside bundles. Also noted: test is ~30
   app migrations behind the repo (nothing after 2026-04-16 applied); 4 old
   entitlement migrations on test aren't in the repo. Prod-facing code unaffected.
3. **Remedy:** 24 original-shape prod bundles (12 gen-1, 8 gen-0a, 3 gen-0b, 1 gen-2;
   distinct companies) copied server-side to `gs://x1-app-www-test/reports/prod_fixtures/`
   (prod untouched; test experiments untouched). They ingest via
   `backfill_evals.py --fixtures` as entity_id-NULL docs with
   `{entity_ref_env: 'prod', prod_startup_company_id}` metadata.

## 2026-07-08 — Phase-0 gate PASSED on the OpenAI/DeepSeek stack → continue on Haystack

All three provider-swapped gate spikes green after David refreshed `OPENAI_API_KEY`:

- **A′ cache usage** (`spike_a2_openai_cache_usage.py`): OpenAI auto-cache hit on call 2
  — `prompt_tokens_details.cached_tokens: 3328` of 3,385 arrived in `reply.meta["usage"]`
  and normalized to `cache_read_tokens` via `Usage.from_haystack_meta`; cached call priced
  $0.000117. The cost-tracking failure mode §10 guards against is closed for this stack.
- **B′ agent loop + web search** (`spike_b2_agent_web_search.py`): Haystack
  `Agent(exit_conditions=["text"])` + gpt-5.1 main agent + `web_research` Tool wrapping
  OpenAI server-side web search → tool invoked, correct grounded answer, **37 resolvable
  citation URLs**. ~**$0.025 per search-call** (10.4k injected input tokens + $0.01 fee) —
  vs $0.0008 for the DeepSeek equivalent; E3 will quantify that trade properly.
  **Implementation finding for E3:** on gpt-5.1 the inline `url_citation` annotations are
  NOT reliably emitted (0 on two live runs); pass
  `include=["web_search_call.action.sources"]` and read `action.sources` (filter
  `type=="url"`; internal feeds like `oai-finance` come back `type=="api"`, `url=null`).
- **C′ models + embedder** (`spike_c2_openai_deepseek_models.py`): `gpt-5.1`,
  `gpt-5-mini`, `deepseek-v4-flash` (OpenAI-compatible endpoint, `prompt_tokens_details`
  intact), and `text-embedding-3-small` (**1536 dims** confirmed) all pass through
  Haystack with `cost.py` pricing.

**Consequence:** Phase 4 stays on Haystack (thin-stack exit ramp unused). Phase 1/2 can
proceed on the working defaults (gpt-5.1 / text-embedding-3-small / OpenAI web search).
Anthropic spikes A–C remain shelved, non-blocking.

## 2026-07-08 — Dev providers: OpenAI default everywhere (company-paid); DeepSeek opt-in; Anthropic deferred (David)

**Decision (David, verbatim intent):** don't block on missing Anthropic keys — develop on
what we hold keys for. **OpenAI is the working default for chat, embeddings, AND web
search** because those calls bill to the company API key. **DeepSeek stays fully wired as
an option** (spike-D-verified searcher, generator-registry candidate) but currently runs on
David's **personal** key, so it is **opt-in only** — a company DeepSeek key may come later
and would lift that restriction. Anthropic/Voyage candidates re-enter the moment their keys
land; this changes *dev defaults*, not the bake-off design (all E1–E4 candidates stand).

Working defaults until bake-offs say otherwise:
- **Main agent (dev):** `gpt-5.1` via `OpenAIChatGenerator`.
- **Embeddings (Phase 1/2):** `text-embedding-3-small` (1536d — same dim as the
  `advisor_evidence` precedent).
- **Web search:** OpenAI server-side `web_search` (Responses API) wrapped as the
  delegated-searcher `Tool` — `spikes/spike_b2_agent_web_search.py` is the reference shape;
  `--searcher deepseek` is the opt-in variant.

**Blocker found while executing this (2026-07-08):** the `OPENAI_API_KEY` in `.env` is
**invalid — OpenAI returns 401** (key suffix `…baYA`; no shell override; `.env` dates to
Jun 12). Credential-file reads outside this repo are policy-gated for the agent, so David
must refresh it (re-sync from x1-backend or paste a current company key). Until then every
OpenAI-default spike is key-blocked, same exit-2 discipline as the Anthropic ones.

**Gate evidence already in hand (provider-swapped spikes, 2026-07-08):**
- `deepseek-v4-flash` through Haystack's `OpenAIChatGenerator` (OpenAI-compatible endpoint):
  works; usage arrives with `prompt_tokens_details.cached_tokens` **present in
  `reply.meta["usage"]`** — the load-bearing cache-field-passthrough question is answered
  for the OpenAI wire shape; priced through `cost.py` at $0.000008.
- Haystack `Agent` + `Tool` constructs and warms up clean on this stack (import-level).
- Full A′/B′/C′ runs pend only on the refreshed OpenAI key.

## 2026-07-07 — Connection pooling: per-worker store instances, no PgBouncer (Phase 0)

**Decision:** run one `PgvectorDocumentStore` (i.e. one cached psycopg connection —
review §6.1.3: the store holds a single connection, sync *and* async) **per service
worker**, not a PgBouncer sidecar.

**Why:** at this scale (corpus in the low hundreds of documents, single Cloud Run
service, admin-gated v1 audience) a PgBouncer deployment is pure overhead. A Cloud Run
deploy with 1–2 uvicorn workers holds 1–2 advisor connections against Cloud SQL —
nowhere near connection limits. The service will connect via the Cloud SQL Python
connector (no proxy sidecar), per review §5.

**Deploy-config implication (Phase 5):** instantiate the document store per worker
process (not module-level shared across forks); set uvicorn `--workers` explicitly so
the connection count is a deliberate number.

**Revisit trigger:** worker count × per-worker store instances approaching ~20
connections, or opening the audience beyond admins pushes concurrency up. Then:
PgBouncer (transaction pooling) in front of Cloud SQL.

## 2026-07-07 — DeepSeek server-side web_search bills tokens only (Spike D closed)

**Decision:** `cost.py` prices DeepSeek `web_search` at **$0 per call** (explicit
`_tool_web_search` row); the real cost is the search results injected as **input
tokens** on the Anthropic-compatible endpoint.

**Evidence (live call, 2026-07-07, `spikes/spike_d_deepseek_billing.py`):**
- `POST https://api.deepseek.com/anthropic/v1/messages`, model `deepseek-v4-flash`,
  tool `{"type": "web_search_20250305", "name": "web_search"}` → HTTP 200,
  content blocks `thinking → server_tool_use → web_search_tool_result → thinking →
  text`, 10 citations all carrying real URLs (citation contract satisfied).
- Usage block verbatim: `{"input_tokens": 5555, "output_tokens": 169,
  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
  "server_tool_use": {"web_search_requests": 1}, "service_tier": "standard"}` —
  a search count, **no fee field**; the one-line question cost 5.5k input tokens
  because results are billed as input.
- DeepSeek's official pricing page (api-docs.deepseek.com/quick_start/pricing) lists
  token prices only; no per-search line item.
- Whole grounded-search call priced through `cost.py`: **$0.000825**.

**Side finding baked into `cost.py`:** the Anthropic-compatible endpoint returns
*Anthropic-shaped* usage (`input_tokens` + `cache_read/creation_input_tokens`).
`Usage.from_haystack_meta` now detects that shape for non-Anthropic providers so
DeepSeek cache-read tokens aren't silently dropped.

## 2026-07-07 — Phase 0 gate status (spikes A–C blocked on `ANTHROPIC_API_KEY`)

Spikes A–C are written and runnable (`spikes/spike_{a,b,c}_*.py`, exit 2 = blocked,
0 = pass, 1 = fail) but **cannot run until `ANTHROPIC_API_KEY` lands in `.env`**
(checked: not in `.env`, not in the shell env, no `ant` CLI profile on this machine).
The Haystack go/no-go gate is therefore **open, not failed**. Everything
framework-independent proceeded:

- `advisor` schema created on test; pgvector 0.8.1 **enabled** (not just available).
- Versions pinned via `uv.lock`: haystack-ai 2.30.2, anthropic-haystack 5.13.0,
  pgvector-haystack 6.3.1, anthropic SDK 0.116.0, Python 3.13.
- Langfuse keys verified (HTTP 200, project `x1-backend-agentic`).
- Leftover schemas measured for David's drop/confirm: `advisor_obs` = **10 GB**
  (events 8.77M rows, runtime_traces 410, service_runs 1,091), `advisor_evidence` =
  **133 MB** (8,268 rows, `vector(1536)` HNSW cosine — the dim precedent noted in
  the plan). Nothing dropped.
- GCS access verified read-only (`x1-app-www-test`, incl. `doc-extract/` content).
