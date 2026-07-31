# X1 Advisor — Architecture Review

> Review of [`ARCHITECTURE.md`](ARCHITECTURE.md) (design draft 2026-06-12), performed 2026-07-07.
>
> **Method.** Read the design doc, its reference-mining inputs (`docs/refs/pipeshub-ai.md`,
> `docs/refs/chroma.md`), the framing chats, and `x1_advisor/cost.py`. Explored the two real
> codebases (`x1-app` Laravel monolith, `dev/x1-backend` Mastra pipelines) with dedicated agents.
> Inspected the live GCP surfaces directly: test **and** prod Cloud SQL databases (read-only),
> GCS buckets, sample extraction blobs and eval bundles. Ran two web-research sweeps (verified
> against official docs/pricing as of July 2026) on the framework choice and on every open
> component decision. Evidence for every claim is cited inline.

---

## 1. Executive summary

**The design is fundamentally sound and unusually well-grounded.** The core principle — hybrid
semantic retrieval over a unified index, with structured fields as metadata filters rather than
LLM-authored SQL — is the right call for this product, and the inspection of the real data
*strengthens* the case for it (see §4.1). The citation mechanism, context discipline, two-tier
loop, and cost telemetry are all proven patterns adopted with clear provenance. The pgvector
decision is correct and verified against the actual Cloud SQL instances.

Four material findings, however, need to change or extend the design before implementation:

| # | Finding | Severity | Section |
|---|---------|----------|---------|
| 1 | **Citation schema assumes layout metadata the pipeline doesn't produce.** The extraction path (Gemini vision → markdown) deliberately strips page numbers and produces no bounding boxes. `bounding_box` in the schema is currently un-populatable; `page_number` is recoverable only by splitting on `# Page N` delimiters. | High — breaks §4.3/§8 as written | §5.1 |
| 2 | **Access control is entirely absent from the design.** The app has real permission gates — `is_published` on profiles, `visibility` (`private`/`x1`/`public`) on documents (**~87% of prod documents are `private`**), `is_visible` + purchase gating on evaluations. A unified index queried without per-user filtering leaks private material. This is the biggest unaddressed product/design question. | High — must be designed in from day one | §5.2 |
| 3 | **"The DB is canonical for all markdown" is a migration project, not a current fact.** Today markdown lives canonically in GCS JSON blobs (`doc-extract/…`, eval bundles under `reports/…`); the DB holds only pointers. In prod the extraction cloud-cache is **empty** — most documents have no standalone extraction at all. The design reads as if the ingestion pipeline only ever touches the DB; in reality a backfill from GCS plus an extraction-triggering path must be built first. | Medium — plan, don't just declare | §5.3 |
| 4 | **No evaluation/quality harness anywhere in the design.** Nothing measures whether retrieval retrieves or answers ground. At the actual corpus size (hundreds of documents) a golden-question set is cheap and high-leverage. | Medium — add a section | §5.4 |
| 5 | **The Tier-2 deep-loop shape (plan→critic→execute→evaluate) is out of step with mid-2026 practice**, which converged on a single gather-act-verify loop + durable plan artifact + parallel *read-only* search subagents. Tier 2 is deferred anyway — rewrite its shape before building it. | Low now, Medium when Tier 2 is built | §6.3 |

And one cross-cutting calibration:

**The corpus is two orders of magnitude smaller than the design's mental model.** Prod today:
293 startups, 363 team members, 267 CVs, 33 investors, 219 uploaded documents, 189 evaluations
(~700 GCS report objects). That's a few thousand chunks — not millions. Nearly every
infrastructure decision survives this observation (pgvector, hybrid retrieval, Haystack), but
several *complexity* decisions (dual-granularity indexing, diff-indexing, a Tier-1/Tier-2 auto
router) are optimizations for a scale that doesn't exist yet and should be sequenced accordingly
(§7). The right instinct is already in the doc's own "Next" step — build the smallest end-to-end
slice — this review just pushes it harder.

Everything else is affirmations with adjustments: the stack choice is defensible (with a real
alternative worth naming — §6), the open decisions now have concrete recommendations backed by
verified July-2026 pricing and capability data (§7), and a suggested build order closes the review
(§8).

---

## 2. Ground truth: what the data actually looks like

This section records what was found by direct inspection, since several design assumptions turn
on it. (Test DB = `x1-db-test`; prod DB = `x1-db`; both PG 16.13 on Cloud SQL, project
`vertical-album-400917`.)

### 2.1 Scale (prod, 2026-07-07)

| Table | Rows | Notes |
|---|---|---|
| `startup_companies` | 293 | 46/50 in test have `full_description` (avg ~1.5k chars) |
| `startup_company_team_members` | 363 | `personal_summary` present on ~1/3 |
| `cvs` / `cv_experiences` | 267 / 510 | plus `cv_education`, `portfolio_items` (100) |
| `investors` | 33 | `investment_thesis` sparse (3/14 filled in test) |
| `startup_company_documents` | 219 | **190 private / 15 public / 14 x1** |
| `startup_company_evaluations` | 189 | `raw_json` = varchar **GCS path**, not JSON |
| `users` / `organizations` | 362 / 13 | plus `investment_companies`, `investment_funds` (≈0 rows yet) |

GCS (prod bucket `x1-app-www-prod`): `reports/` 688 objects (eval bundles + PDFs),
`startup_company_documents/` 302, `portfolio_documents/` 206, `doc-extract/` **0** (extraction
cloud-cache not enabled in prod). Prod bucket has **object versioning suspended**.

A full re-index of the entire corpus is therefore a trivial batch job today (order of
$1–$10 in embedding cost at current prices, minutes of wall-clock). This matters for how much
incremental-indexing machinery is worth building up front.

### 2.2 Where text actually lives

1. **Entity fields (Postgres).** Rich text on `startup_companies` (`full_description`,
   `one_sentence_pitch`, `description`), team members (`personal_summary`, `achievements`,
   `skillsets`, `focus_statement`, `core_responsibilities`), `cv_experiences` (same family),
   `investors` (`investment_thesis`, `focus_statement`, `value_beyond_capital`,
   `pitch_instructions`), `investment_funds` (`investment_thesis`, `executive_summary`, …).
   **Caveat: these are TipTap editor output — a mix of HTML fragments and plain text** (17/46
   test `full_description`s start with an HTML tag). Profile rendering needs an HTML→markdown
   step the design doesn't mention.
2. **Extraction blobs (GCS).** `doc-extract/{content_hash}/{config_hash}.json` with shape
   `{markdown, model, mode, timestamp}` — produced by `x1-extract` (Gemini
   `gemini-3-flash-preview`, vision mode). Markdown contains `# Page N` H1 delimiters per page
   and inline VLM captions (`[Illustration: …]`) for visuals. **No bounding boxes; page count not
   even persisted.** The DB table `doc_extraction_cache` is only a pointer index
   (`content_hash`, `config_hash`, `gcs_path`, `model`, `extract_mode`; the token/page columns
   are written as NULL). Exists in test only; **prod has neither the table rows nor the blobs**.
3. **Eval bundles (GCS) — the richest indexable source.** `reports/{slug}_{uuid}.json`
   (schema_version 2) contains: `outputs.premium_markdown` (~52k chars in the sample),
   `basic_markdown` (~4k), `section_results[]` (`sectionName`, `topic`, `analysis`, `score`,
   `dimensionScores`, `rawFindings`), `company_data.pitchDeckContent` (the full extracted deck
   markdown, ~23k), **and `company_data.websiteContent`** (website→markdown already happens
   inside the eval pipeline), plus a full snapshot of the startup row and team members at eval
   time. The DB row (`startup_company_evaluations`) carries scores + three GCS pointers.
   Two path generations exist (`evaluations/{id}_{time}.json` app-era vs `reports/{slug}_{uuid}.json`
   backend-era) — the ingester must handle both.
4. **Uploaded binaries (GCS).** `startup_company_documents/{ts}_{companyId}_{userId}_{uuid}_{name}`;
   mostly visual pitch-deck PDFs (avg ~5 MB). CVs under `portfolio_documents/…`.

### 2.3 Pipelines that produce/consume this data

`dev/x1-backend` (TypeScript, **Mastra** + Vercel AI SDK; Cloud Run): `eval` (pitch-deck
evaluation; writes bundle + 2 PDFs + DB row; models are **OpenAI** — gpt-5.1 etc. — with
OpenAI's built-in web-search tool for section research), `import` (CV/startup import; writes
structured JSON to `jobs.results`, which **x1-app consumes and writes into entity tables**),
`extract` (shared library: PDF/PPTX/DOCX → markdown via Gemini vision; LibreOffice for Office
formats). There is **no existing embedding/vector/RAG code anywhere** in the backend. The app
(`x1-app`, Laravel 12) has ILIKE-based search only, plus two OpenAI chat features
(`ReportChatService` — stuffs a whole eval bundle into context; `ProfileChatService`).

Two directly reusable assets: `ReportChatService::buildCompanySection`
(`x1-app/app/Services/ReportChatService.php:127`) already assembles exactly the denormalized
startup+team "profile card" the design wants for `entity_profiles`; and the extract library's
`(content_hash, config_hash)` caching discipline is the provenance pattern §4.3 wants.

### 2.4 Permission surfaces (detail for §5.2)

- Profiles (`startup_companies`, `cvs`, `investors`, funds, orgs): `is_published` boolean —
  unpublished = owner/admin only. The app's own `SearchService` hard-filters on it.
- Documents: `visibility` enum — `public` (anyone), `x1` (any authenticated user), `private`
  (startup admins/founders only), plus `share_token` deep links.
- Evaluations: `is_visible` + report **purchase/entitlement** gating (premium content is paid).
- Platform admins (`users.is_admin`) see everything.

### 2.5 Environment/infra facts

- pgvector **0.8.1 installed on test; available but not yet enabled on prod** (`CREATE EXTENSION
  vector` needed). `pg_trgm` installed. Cloud SQL's extension allowlist is closed — no ParadeDB
  `pg_search`, no VectorChord, no `rum` (details §7.4).
- Leftovers from the aborted copilot on the **test** instance: `advisor_obs` schema
  (**8.77M rows** in `events` — worth truncating for cost/hygiene) and `advisor_evidence.evidence`
  (8,268 rows, `vector(1536)` embeddings of entity profile text — evidence that a prior indexing
  experiment used 1536-dim/OpenAI). Decide: drop, or archive then drop, before creating the new
  `advisor` schema.
- `.env` already carries `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `JINA_API_KEY`, Langfuse keys — but
  **no `ANTHROPIC_API_KEY`**, despite the design's LLM choice. Practical to-do.

---

## 3. What the design gets right (affirmations)

Quickly, so the rest of the review can focus on deltas:

- **Retrieval spine, not SQL-first (§3 of the design).** Affirmed, and the data inspection adds
  evidence the doc didn't have: the schema's filterable fields are *dirtier than they look*.
  Enum constraints on `fundraising_status`/`fundraising_round` were dropped in later migrations
  (values are now free-form strings); `industries`/`sector_focus`/etc. are `json` arrays of
  display labels, not FKs; two generations of company tables exist. Generated SQL over this
  would be brittle in exactly the ways the doc predicts. Indexing denormalized profiles with
  *normalized* metadata (resolved at index time, when you can afford to be careful) is the
  right response to this schema.
- **pgvector in the shared instance, dedicated schema (§4.2).** Affirmed by direct verification:
  0.8.1 available, corpus is tiny, and the JOIN-ability argument from `docs/refs/chroma.md`
  gets a new, concrete use: **permission filtering** (§5.2) wants SQL joins against
  memberships/ownership at query time — trivially expressible in pgvector-in-Postgres,
  painful in an external vector store.
- **Citations: block primitive, tiny refs, omit-rather-than-guess, server-side validator (§8).**
  Affirmed as mechanism — this is the strongest borrowed pattern. Amendments in §5.1 concern
  only the *locator payload* (page/bbox), not the mechanism.
- **Two-tier loop, fast tier first (§7); context discipline (§9).** Affirmed; matches both the
  pipeshub evidence and current (2026) agent-building practice. See §7.6 for a router
  recommendation.
- **Cost telemetry (§10).** Affirmed; `cost.py` is implemented, tested, and handles the
  Anthropic-vs-OpenAI token-accounting subtlety correctly. Housekeeping: several `# verify`
  rates in `PRICING`, no Fable-5 row yet, embedding row pending the model decision (§7.1),
  and note the backend uses OpenAI + Gemini models, so multi-provider coverage stays relevant.
- **"Agent output becomes retrievable evidence" (§4.1, property 2).** Genuinely valuable — with
  one wrinkle the design must add: derived documents (research notes, eval findings) must
  **inherit the most restrictive visibility of their sources** (§5.2), or the research agent
  becomes a laundering channel for private material.

---

## 4. Decision-by-decision assessment

### 4.1 Core principle (§3) — AFFIRM, with one honest limitation to document

The unified-index design handles semantic + filtered retrieval well. What it does *not* give you
is **relational traversal**: "which investors have funded startups in sector X," "who on team A
previously worked at a company evaluated below 50" — multi-hop joins across entities. PipesHub
(the design's main reference) never solved this either; its graph is an ACL/metadata filter, not
a relationship walker (the reference doc says this explicitly). The design should say where such
questions go: either (a) the agent chains multiple `search_corpus`/`get_source` hops — works,
costs latency and tokens, and is the honest v1 answer; or (b) a small set of *parameterized
relationship queries* inside `structured_query` (e.g. `investments_by_investor(investor_id)`),
which the registry-resolver approach in §7.5 accommodates cleanly. Recommend (a) now + named
additions to (b) as real user questions accumulate. What should **not** happen is quietly
reintroducing free-form SQL to fill the gap.

### 4.2 Storage model (§4) — AFFIRM the policy; reframe as a migration (see §5.3)

The DB/GCS seam (markdown + chunks + vectors + provenance in Postgres; binaries in GCS) is
right, and at this corpus size the "markdown in the DB" decision is comfortably cheap (the
entire prod markdown corpus is a few tens of MB). Two schema-sketch adjustments:

- `advisor.doc_chunks.bounding_box` — remove or explicitly mark "future; not populatable by
  current extraction" (§5.1). Keep `page_number` (recoverable from `# Page N`).
- Add **access-control metadata** to `advisor.documents` and denormalize it onto chunk metadata:
  `visibility`, `is_published` (of the owning entity), `owner_user_ids`/`admin_user_ids` (or an
  entity pointer that resolves to them), `evaluation_is_visible`. §5.2.
- Add `embedding_model`/`dim`/`distance` as a **stored, versioned config row** in the `advisor`
  schema (the "persisted embedding-function config" idea from the Chroma reference) so ingest
  and query can never diverge — the pipeshub reference flagged model-consistency as a real
  production bug source.
- Naming nit: prior schemas `advisor_obs`/`advisor_evidence` exist on test. Either adopt the
  same prefix family deliberately or clean them up first (§2.5).

### 4.3 Ingestion & chunking (§5.1) — AFFIRM structure-aware; DEFER dual-granularity; diff-indexing is a v2 optimization

- **Structure-aware chunking**: affirm — the markdown is heading/page-delimited and chunks
  naturally. For decks, `# Page N` sections ≈ one chunk per slide, which is also the natural
  citation unit.
- **Dual granularity (block + sentence)**: defer. It multiplies index size and adds a result
  dedup problem, for a corpus where whole-block retrieval over a few thousand blocks with a
  reranker will already be strong. Add sentence-level later **if** eval (§5.4) shows precision
  failures on long blocks. (pipeshub needed it at enterprise scale; you are not at enterprise
  scale.)
- **Record-summary block / entity profiles**: affirm — this is the highest-value indexing idea
  in the design, and `ReportChatService` already contains the field list for startups. Note the
  LLM-written summary adds an ingestion-time LLM cost per document/entity — fine at this scale,
  and `cost.py` already plans to track it.
- **Multimodal VLM captioning**: already exists in the extract pipeline's vision mode (captions
  inline in the markdown). Don't rebuild it; inherit it. What's genuinely missing is bbox — see §5.1.
- **Diff-indexing with stable `block_index`**: keep the *stability contract* (citations must
  keep resolving), but implement it the cheap way first: re-ingest = new document version, keep
  old chunk rows (mark superseded) so old citations resolve, re-embed the whole doc (~$0.01).
  Block-level diffing (pipeshub's reconciliation) is real engineering — matching blocks across
  re-extraction where boundaries shift — and buys you almost nothing until re-ingest volume is
  meaningful. Version-and-append gives the same citation stability with a fraction of the code.

### 4.4 Retrieval pipeline (§5.2 of the design) — AFFIRM shape; two adjustments

Hybrid (dense + lexical) → RRF → rerank is the right spine. Adjustments:

1. **Know what the lexical leg actually is.** `PgvectorKeywordRetriever` is Postgres full-text
   (`ts_rank`-family), which has **no IDF/BM25 scoring** — PG's own docs concede fair global
   normalization is impossible with local information only. On Cloud SQL you *cannot* install
   any BM25 extension (closed allowlist — no ParadeDB, no VectorChord-BM25, no pg_textsearch,
   no rum; verified). This is acceptable here — RRF consumes rank order, the dense leg and
   reranker carry ranking quality, and the corpus is small — but it should be a *documented,
   revisitable* decision, with the upgrade ladder recorded: tuned FTS (`websearch_to_tsquery`,
   `unaccent`, `ts_rank_cd` normalization flags) → BGE-M3/SPLADE sparse vectors in pgvector
   `sparsevec` (works on Cloud SQL 0.8.1 today) → app-layer BM25 over GIN candidates (trivial at
   10³–10⁴ docs) → AlloyDB (native BM25 "coming soon" per Google, announced Apr 2026, unshipped).
2. **Add group-by-entity diversification** (the Chroma `MinK`-per-group idea; `DISTINCT ON` /
   window top-k in SQL): without it, one 60-page premium report will dominate top-k for any
   query about its startup. Cheap to implement, high perceived-quality impact.

Also worth noting: pgvector 0.8.x **iterative index scans** exist precisely to fix
filtered-HNSW overfiltering — relevant once permission filters (§5.2) make every query a
filtered query. Cloud SQL runs 0.8.1; upstream 0.8.3/0.8.4 fix an HNSW-corruption-under-vacuum
bug, so watch the Cloud SQL maintenance channel. (At current scale, exact scan — no index at
all — is also a perfectly fine interim answer.)

### 4.5 Tools (§6) — AFFIRM set; one addition, one tightening

The five-tool set is right. Add a sixth cheap tool: **`list_or_aggregate` presets** are already
planned inside `structured_query`; make sure it also covers *entity enumeration* ("list all
fintech startups raising now") — retrieval is a bad enumerator, and enumeration questions are
common in a CRM. Tighten `get_source` to accept the citation tuple `(document_id, block_index)`
directly so the UI's "open citation" and the agent's rehydration share one code path.
`fetch_url` gets a strong recommendation in §7.3 (Anthropic `web_fetch` is token-cost-only).

### 4.6 Agent loop (§7) & context discipline (§9) — AFFIRM Tier 1; Tier 2's internal shape needs a rewrite before it's built (§6.3)

One addition to §9's list: the **stable prompt prefix** item should be made testable — assert in
CI that the system prompt + tool definitions serialize identically across turns (tool-def
ordering is a classic silent cache-buster; at Anthropic's cache-write 1.25× / cache-read 0.1×
pricing, an accidental cache miss on every turn roughly doubles input cost).

### 4.7 Telemetry (§10) — AFFIRM (already implemented); housekeeping list

Add when known: chosen embedding model row; Fable-5/newer Anthropic rows; resolve the four
`# verify` rates; decide the `JsonlSink` file's rotation story before the service runs
long-lived. The deep-tier budget gate consuming `Tracker` is designed but not yet wired — fine,
it's greenfield. **One hard dependency to verify on day 1:** `Usage.from_haystack_meta` assumes
the Anthropic integration surfaces `cache_creation_input_tokens`/`cache_read_input_tokens` in
`reply.meta["usage"]` — this could not be confirmed from the integration's changelog/source
(§6.1.5). If absent, every cached call is silently under-priced, which is the exact failure
mode §10 exists to prevent.

---

## 5. Material gaps (things the design must add)

### 5.1 Citations vs. extraction reality — resolve the mismatch explicitly

The design (§4.3, §5.1, §8) carries `page_number` + `bounding_box` through schema, ingestion,
and citation payloads, citing pipeshub. But pipeshub's bboxes come from **Docling** parsing;
X1's extraction is **Gemini-vision → markdown that deliberately discards layout** (the prompt
says "Ignore headers, footers, page numbers"; output is `# Page N`-delimited markdown with
inline VLM captions; verified against a live blob). Nothing in the current pipeline can fill a
`bounding_box` column, and `page_count` isn't even persisted.

**Recommendation.** Design citations as **page-level for v1** and make that a stated product
decision, not an accident:

- Chunker records `page_number` parsed from `# Page N` delimiters (works today for every
  extracted doc). For decks — the dominant doc type — a page *is* a slide, so "cite → show
  slide N thumbnail" is already a good UX; render thumbnails from the GCS binary on demand.
- Keep `bounding_box` out of the schema (or nullable + documented as unfilled) until there's a
  concrete UI that needs sub-page highlighting for dense documents (CVs, business plans).
- When that need arrives, the verified July-2026 options are: **Docling** (MIT, active,
  per-item page+bbox+charspan provenance, VLM picture-description — the natural fit, and free),
  **Mistral OCR 4** ($4/1k pages, new blocks-with-bbox output — two weeks old, pilot before
  trusting), or LlamaParse/Reducto (managed, $1–30/1k pages). Claude's native PDF citations are
  page-level only (no bbox) — confirming page-level is a respectable industry ceiling right now.
- This also means §5.1's "multimodal → searchable text … carrying `page_number` +
  `bounding_box`" should be rewritten: the VLM captioning **already exists** in x1-extract; what
  would be new is a layout-aware parse, and it's deferred.

### 5.2 Access control — the biggest unmade decision

The design never says **who can use the advisor or what it may reveal**. The platform's actual
permission model (§2.4) makes the unified index a leak vector if ignored: 87% of prod documents
are `private` (founder/admin-only), premium eval content is *paid*, unpublished profiles are
draft-private. A few concrete failure cases the current design would permit:

- An investor asks about a startup and gets content quoted from that startup's `private` pitch
  deck, or from a premium report they haven't purchased.
- A founder's unpublished draft profile surfaces in another user's research answer.
- A research note generated for user A (derived from A's private docs) is later retrieved into
  an answer for user B — the "knowledge base compounds" property (§4.1) *amplifies* leaks.

**Recommendation.** Adopt the pipeshub *filter-then-search* shape (which the team's own
reference doc describes in detail, for exactly this purpose — it just wasn't carried into the
design):

1. Index **everything**, but stamp every document/chunk with ACL metadata at ingest:
   `visibility`, owning `entity_type/entity_id`, `is_published`, `eval_is_visible`,
   plus derived-doc provenance (max-restrictive inheritance from sources).
2. Resolve the **requesting user → accessible-set** at query time with plain SQL joins
   (memberships, ownership, purchases, `is_admin`) — this is precisely where pgvector-in-Postgres
   pays off over an external vector store.
3. Apply it as a **mandatory retriever-level filter** (not a prompt instruction, not
   post-filtering of the answer), in the same place §5.2's metadata filters already sit.
4. Decide the **v1 audience** to keep scope sane. Simplest sound v1: internal/admin users only
   (filter = everything), with the ACL metadata already indexed so opening it to founders
   ("your own startup + public + x1 docs") and investors is a filter change, not a re-index.
   Note `users.advisor_about_user`/`advisor_response_style` columns already exist in the app —
   the prior attempt clearly intended end-user exposure, so this decision will come fast.

This costs almost nothing now (a few metadata columns + one resolver query) and is very
expensive to retrofit after citations/research-notes accumulate.

### 5.3 "Markdown-canonical in the DB" — write the migration plan

Because prod has **no extraction blobs** and the extraction cache table doesn't even exist
there, the ingestion story must include:

1. **Backfill from eval bundles** (688 prod objects): `pitchDeckContent`, `premium_markdown`,
   `basic_markdown`, `websiteContent`, `section_results` → `advisor.documents` rows with
   provenance pointing at the bundle. This alone covers most startups that matter (every
   evaluated one) and costs no LLM calls.
2. **Extraction path for never-evaluated documents** (private decks, CVs under
   `portfolio_documents/`, non-deck docs): reuse the `x1-extract` library (or port its exact
   config-hash discipline) — but the advisor service is Python and x1-extract is TypeScript, so
   decide: (a) call a small extraction endpoint on the existing backend, (b) shell out to a
   worker, or (c) reimplement in Python against the same `(content_hash, config_hash)` contract.
   (a) keeps one extraction implementation and is recommended.
3. **HTML→markdown for entity fields** (TipTap output) inside the profile renderer.
4. **Forward path**: new uploads/evals must land in `advisor.documents` — either the backend
   writes markdown to the DB going forward (touches x1-backend), or the advisor sweeps for
   new/changed rows and pulls (no backend changes; see freshness, §5.5). Sweeping is the
   low-coupling v1 answer.

### 5.4 Evaluation harness — add a section to the design

Nothing in ARCHITECTURE.md measures quality. At this corpus size this is cheap and should exist
*before* tuning decisions (embedding model, reranker blend, chunking granularity) are made,
or those decisions will be vibes-based. Minimum viable harness:

- **Retrieval set:** ~30–50 golden questions (entity lookups, cross-doc, filtered, aggregate)
  with expected source docs; measure recall@k / MRR on every retrieval-affecting change.
- **Groundedness check:** for generated answers, verify every citation resolves (the §8
  validator already does this mechanically) *and* sample-audit that cited text supports the
  claim (LLM-as-judge is fine here); track uncited-claim rate.
- Log per-question cost/latency from `cost.py` so quality changes carry their price tag.
- Langfuse keys already exist in `.env`, and the user's stated debugging loop is
  Langfuse-trace-driven — wire traces in from day one.

### 5.5 Smaller gaps (each needs a sentence in the design, not a system)

- **Freshness trigger** (§11 open): recommend a **periodic content-hash sweep** (cron; compare
  `updated_at`/hash per entity and doc row) over event-driven triggers. Rationale: the writers
  are Laravel + backend jobs (would need triggers/outbox in someone else's codebase); at this
  scale a 5-minute sweep is indistinguishable from real-time and is one cron job.
- **Conversation/session persistence**: where do chat threads, per-turn research records, and
  the promised "compact research record per turn" (§7) live? Recommend: same `advisor` schema
  (`advisor.threads`, `advisor.turns`, with the research record as a first-class row —
  which also feeds §5.4's eval set and, later, indexed research notes).
- **Serving/API surface**: unstated. The app already ships `@assistant-ui/react`; the natural
  contract is an SSE/streaming chat endpoint on the Python service, consumed by an
  Inertia/React page. Decide auth between app and service (the service must know the requesting
  user for §5.2 — a signed user token from the Laravel session is the simplest sound answer).
- **Deployment**: everything else here is Cloud Run + Cloud Build; assume the same for the
  advisor. Connect to Cloud SQL via the Cloud SQL Python connector (no proxy sidecar needed);
  pgvector must be enabled on prod (`CREATE EXTENSION vector` — one-time, needs a decision on
  who runs it).
- **Failure modes**: extraction failures (record `extraction_error` on the doc row and skip;
  don't wedge the sweep), embedding-API outage (queue and retry; the corpus is small enough to
  re-run), model-pricing drift (cost.py already raises on unknown models — good).
- **`.env`/secrets**: add `ANTHROPIC_API_KEY`; pick the web-search provider key (§7.3).

---

## 6. Stack assessment (Haystack, and the alternative worth naming)

*(All framework claims below were verified against release notes, changelogs, and source in a
July-2026 research sweep; items that could not be verified are flagged.)*

### 6.1 Haystack, verified state (July 2026)

The good news first — the choice is mechanically sound:

- **Actively maintained, monthly cadence** (2.24 → 2.31-rc between Feb and Jul 2026). The
  `Agent` component has been **core since 2.12 (~Mar 2025)**: tool-call loop, `exit_conditions`
  (default `["text"]`, exactly as §7 of the design assumes), `max_agent_steps`, shared state,
  streaming with tool events, breakpoints/snapshots, async. `ComponentTool`/`PipelineTool`
  exist as designed.
- **`pgvector-haystack` supports everything §4 needs**: `schema_name` (the dedicated `advisor`
  schema works), HNSW with tunable params, `halfvec`, JSONB metadata filtering with
  injection-hardening, full async, `create_extension` auto-setup. Actively maintained (v6.3.1,
  June 2026).
- **`anthropic-haystack` is current-ish**: v5.12.0 (July 2, 2026); prompt caching (since 2024),
  extended/adaptive thinking (May 2026), streaming + async + tools, multimodal. Model is a
  plain string, so current Claude IDs pass through.
- Since 2.29 there's also a core `MultiRetriever` (parallel retrievers + RRF) — a
  one-component alternative to the two-retrievers-plus-`DocumentJoiner` composition in §5.2 of
  the design.

Now the caveats that must be engineered around (each verified):

1. **No Anthropic-native Citations support.** This is the most consequential finding. The
   Claude API has a GA **Citations API** (`search_result` content blocks +
   `citations: {enabled: true}`): a retrieval tool returns search results, and the model emits
   **server-validated citations** (`cited_text` — not billed as output tokens, streamed as
   `citations_delta`). That is a first-party, API-enforced version of a large part of what §8
   of the design plans to hand-build. LangChain, LlamaIndex, and the Vercel AI SDK all expose
   it; **`anthropic-haystack` shows no trace of it** in changelog or docs. Consequence: on
   Haystack, the design's pipeshub-style citation machinery (tiny refs + post-validator) isn't
   just a nice pattern — it's *mandatory*, because the native alternative is inaccessible
   without bypassing the framework. That machinery is proven and the design specifies it well,
   so this is acceptable — but it should be a conscious trade, and it's the single strongest
   argument for the thin-stack alternative in §6.3.
2. **The keyword retriever is Postgres FTS, not BM25** — `ts_rank_cd` over `plainto_tsquery`,
   AND-semantics, no fuzzy matching (deepset's own docs say so). Already covered in §4.4; fine
   at this scale, just don't call it BM25 in the design.
3. **No connection pooling in the pgvector store**: one cached psycopg connection per store
   instance (sync *and* async). A concurrent chat service must run PgBouncer, or one store
   instance per worker, or accept serialization. One sentence of deployment design — but a
   production incident if discovered later.
4. **Integration lag as a pattern**: adaptive thinking landed months after the API shipped it;
   public docs still show 2024-era models and an obsolete caching beta header; newest-model
   handling (Fable-family constraints) is unverified. Expect to be weeks-to-months behind new
   Claude API surface while on the integration.
5. **Cache-token usage surfacing is unverified.** `cost.py`'s `Usage.from_haystack_meta`
   depends on `cache_creation_input_tokens`/`cache_read_input_tokens` appearing in
   `reply.meta["usage"]`; the research could not confirm the integration passes them through.
   **Day-1 spike: make one cached call and assert both fields arrive** — if they don't, cost
   tracking silently under-prices every cached call (the exact failure mode §10 was built to
   prevent).
6. **HITL and memory stores are still in `haystack-experimental`**, and a core agent-loop bug
   (multi-tool-call turns vs exit conditions) was fixed as recently as 2.30 (June 2026). The
   loop is solid but still hardening; pin versions and read release notes.
7. **Ecosystem position**: Haystack is a distant #3 (≈1M PyPI downloads/month vs LangGraph's
   ≈62M; 25.8K stars vs LlamaIndex 50.7K/LangGraph 36.7K), effectively single-vendor (top
   contributors are all deepset staff), and deepset hasn't raised since Aug 2023 (~$45M total
   vs LangChain's $1.25B valuation). Counter-signals: cadence is healthy, and the Dec 2025
   "Haystack Enterprise Platform" rebrand makes the OSS framework deepset's commercial funnel —
   an argument *against* abandonment. Realistic risk is concentrated-vendor slowdown, not
   disappearance.

### 6.2 Alternatives, one paragraph each (verified July 2026)

- **LangGraph/LangChain 1.x** — the field leader (GA Oct 2025, stability pledge holding);
  most mature agent loop (middleware, Postgres checkpointers, interrupts); `langchain-postgres`
  has native hybrid (vector + tsvector, weighted/RRF); Anthropic-native citations wired
  through. Cost: heavier abstraction stack, the churn tax of its 1.0 transition, and it's
  still a Python service — it solves nothing Haystack doesn't while adding the LangChain
  dependency web. The design's "deferred, not rejected" stance remains correct; the *reasons*
  to ever adopt it (checkpointing, HITL, durable state) are also the reasons listed in §2 of
  the design.
- **LlamaIndex** — Python side alive but the company has pivoted to document-AI (LlamaCloud);
  pgvector hybrid exists but with explicitly unsupported fusion weighting; **LlamaIndex.TS is
  archived/deprecated** (Mar 2026). Not a contender for the spine.
- **Pydantic-AI** — v2 (June 2026), excellent and the **deepest Claude support of any
  third-party framework** (adaptive thinking + effort, caching helpers, server tools, current
  models incl. Fable family) — but **citations are a confirmed gap** (open issues, an unmerged
  PR, a literal `# TODO` in `models/anthropic.py`), and retrieval is bring-your-own. It's the
  best "thin agent shell" if hand-rolling retrieval anyway.
- **Mastra** (the TS incumbent — this deserves honesty because the team already runs it):
  1.0 stable (Jan 2026, APIs locked), $22M Series A (Apr 2026), markdown-aware chunkers that
  fit this corpus, `PgVector` + Postgres-backed memory, evals built in, and one language for
  the whole platform. Two verified gaps for *this* use case: **dense+lexical hybrid over
  pgvector is unshipped** (PR closed unmerged; tracking issue #13226 open — metadata-filtered
  vector search only), and **no citation engine** (though the underlying `@ai-sdk/anthropic`
  has carried Anthropic-native citations incl. `search_result` blocks since early 2026, so the
  primitive is reachable). Choosing Mastra means hand-building the hybrid-retrieval spine and
  the citation layer inside a framework that doesn't supply either — the two things Haystack
  actually brings. Also noted for risk files: the `@mastra` npm scope suffered a same-day-
  remediated supply-chain attack June 16–17, 2026 (≥1.45.0 clean).
- **OpenAI Agents SDK** — Claude is second-class on every path (LiteLLM "best-effort beta",
  caching deliberately not first-class, hosted tools OpenAI-only). Not a contender.
- **Direct Anthropic SDK ("thin stack")** — the serious alternative. The beta **tool runner**
  (`client.beta.messages.tool_runner`) drives the loop with per-iteration hooks,
  `max_iterations`, streaming; retrieval = ~200 lines of psycopg against pgvector (the hybrid
  SQL is DIY *on Haystack too* — RRF fusion is our composition either way); and it gets the
  **Citations API natively**, plus zero adapter lag on every new Claude feature (server-side
  web_search/web_fetch tools, dynamic filtering, adaptive thinking) the day they ship. What
  you give up: the component/pipeline vocabulary, off-the-shelf embedders/rerankers/joiners,
  tracing integrations, and a maintained document store layer. (The Claude *Agent* SDK — the
  Claude Code harness as a library — is the wrong shape here: coding-agent tools, no citations
  surface.)

### 6.3 The two-tier loop vs. mid-2026 practice — revise Tier 2's shape

The design's Tier-2 (`plan → critic → execute → evaluate`, borrowed from pipeshub) reflects
2025 practice. The field has since converged elsewhere, with unusual unanimity across
Anthropic's engineering posts (Jun 2025 → Nov 2025), OpenAI's Deep Research, and LangChain's
open_deep_research retrospective:

- **One strong-model gather-act-verify loop** is the core; planning lives in the model's
  interleaved (adaptive) thinking plus a **durable plan artifact** (a todo/brief the model
  writes and re-reads), not in a separate planner→critic model pipeline.
- **Parallelize reading, never writing**: sub-agents are read-only searchers whose distilled
  findings return to a single synthesizing context (Anthropic's multi-agent research system:
  +90% on research evals at ~15× tokens; a 2026 controlled study found most fixed multi-agent
  pipelines *underperform* a matched single-agent baseline).
- A separate critic-gate step survives mainly as a cost-tiering trick, not a quality
  architecture.

Concretely for the design: **Tier 1 is unaffected** (it *is* the converged loop — Haystack's
Agent maps to it fine). **Tier 2 §7 should be rewritten** when its time comes: same loop +
research-brief artifact + parallel read-only `search_corpus`/`web_search` sub-calls with
isolated contexts + single-context synthesis, with the §7 budgets kept. The typed task-DAG,
per-task critic, and event-gated sub-agent orchestration from pipeshub should *not* be built.
This also softens one argument for heavyweight orchestration frameworks: the converged shape
is simple enough that any of the §6.2 options (including the thin stack) can express it.

### 6.4 Verdict

**Keep Haystack for the Tier-1 slice — the decision survives review — but with four
guardrails, and with the thin-stack alternative named as the explicit fallback.**

Rationale: the things Haystack genuinely supplies this design (pgvector store + retrievers,
embedder/reranker components, DocumentJoiner/MultiRetriever fusion, a serviceable agent loop,
tracing hooks) are exactly the parts the team would otherwise hand-write, and the corpus-scale
reality (§2.1) means none of Haystack's scale limitations bind. The citations gap (§6.1.1) is
neutralized by the fact that the design *already* specifies the pipeshub-style mechanism —
which produces UI-resolvable `(document_id, block_index)` citations that the native Citations
API wouldn't give you for internal docs anyway. The ecosystem risk is real but priced in by
guardrail #4.

Guardrails:
1. **Keep Haystack usage shallow and swappable**: pipelines-as-tools + one Agent; no deep
   investment in Haystack-specific state/memory/HITL (those are experimental anyway). The §4.2
   "low-regret discipline" already promises portability for storage; extend the same discipline
   to the framework layer. Concretely: own the tool schemas, the citation layer, the prompts,
   and the eval harness as plain Python — framework-independent by construction.
2. **Day-1 verification spikes** (hours, not days): (a) cache-token fields arrive in
   `reply.meta["usage"]` (else patch/PR or read usage via a thin client wrapper);
   (b) Anthropic **server-side web_search/web_fetch tool blocks** pass through the integration
   (unverified — if not, §7.3's fallback is Serper + fetch); (c) Fable-5/newest-model kwargs
   don't 400 through the integration's thinking handling.
3. **Deployment realism**: PgBouncer or per-worker store instances (§6.1.3); pin
   `haystack-ai`/integration versions; subscribe to release notes (the 2.30 loop fix shows
   why).
4. **Named exit ramp**: if two or more of the spikes fail, or if the integration lag starts
   costing product features (Citations API being the likely first casualty), fall back to the
   thin stack — direct Anthropic SDK tool runner + the same pgvector SQL + à-la-carte reuse of
   whatever components still help. The retrieval schema, tools, prompts, citations, and eval
   harness all survive that move unchanged if guardrail #1 is honored.

One more consequence worth stating: **LangGraph stays deferred** (nothing found changes that),
and **Mastra — despite the language alignment — would hand the team *more* build work exactly
where the design is most demanding** (hybrid retrieval, citations). The Python-service tradeoff
the design accepted remains the price of the strongest retrieval toolkit; the thin stack is the
hedge that keeps that price bounded.

---

## 7. Recommendations on the open decisions (§11)

*(Component data below is from a verified July-2026 research sweep; all prices from official
pricing pages unless noted. Final recommendations that interact with the framework choice are
cross-referenced to §6.)*

### 7.1 Embedding model [OPEN → recommendation]

**Recommend: `voyage-4` (1024-dim default), with `voyage-4-lite` as the cost floor and
`voyage-context-4` as the upgrade to trial.** Rationale:

- **Anthropic has no first-party embeddings and officially recommends Voyage** — so this is the
  path of least dissonance for a Claude-centric stack (verified July 2026 on the Claude docs).
- **1024-dim fits pgvector's HNSW limit (≤2000 for `vector`) natively**; no halfvec/truncation
  contortions. MRL options (256/512/2048) leave room to shrink later. 32k-token input handles
  whole entity profiles and big report chunks (OpenAI's is 8k; gemini-embedding-001 is a
  2,048-token trap).
- **Pricing is a non-issue at this scale**: $0.06/1M tokens (voyage-4), and Voyage gives 200M
  free tokens per model — the entire prod corpus is a rounding error; even 100× growth is.
- `voyage-context-4` ($0.12/1M) embeds all chunks of a doc in one contextualized pass — chunk
  vectors carry document context. That is *exactly* the deck-slide/CV-section shape of this
  corpus. Trial it against `voyage-4` in the §5.4 harness once it exists; don't block on it.
- Vendor-risk note: Voyage is MongoDB-owned (2025) — stable ownership; also available via open
  weights at the low end (`voyage-4-nano`, Apache 2.0) as an exit hatch.
- Runner-up: OpenAI `text-embedding-3-small` (1536-dim, $0.02) — battle-tested, key already in
  `.env`, and the prior x1 experiment used 1536-dim; choose it if minimizing new vendors beats
  a quality/context edge. Avoid 3072-dim models (index friction) and Gemini-001 (input limit).
- **Pin the choice in the schema** as a config row (§4.2) and add the pricing row to
  `cost.py` (`_embed` rate) in the same commit.

### 7.2 Reranker [OPEN → recommendation]

**Recommend: hosted Voyage `rerank-2.5-lite` ($0.02/1M tokens) to start, upgrade to
`rerank-2.5` ($0.05/1M) if the eval harness shows headroom.** Rationale: token-based pricing is
dramatically cheaper than Cohere's per-search unit for long business-document chunks (Cohere
search units silently multiply when docs exceed 500 tokens); same vendor as embeddings (one
account, 200M free tokens); 32k context; self-hosting a cross-encoder is ~$750/mo of GPU vs
single-digit dollars hosted at this volume — not close. Keep pipeshub's `0.3·dense + 0.7·rerank`
blend as the starting point, and (from the Chroma reference) log the fusion as a serializable
expression so blends are reproducible. Alternatives if vendor diversity is wanted: ZeroEntropy
`zerank-2` ($0.025/1M; small-vendor risk, Apache-2.0 fallback model exists) or Jina v3 (the
`JINA_API_KEY` already in `.env` makes this the zero-signup option — and Haystack ships a
`JinaRanker` component, whereas a Voyage reranker needs a ~30-line custom component; both are
fine). At a few thousand chunks, also *test skipping the reranker entirely* in the harness —
hybrid+RRF alone may be within noise at this corpus size, and it removes a per-query
dependency.

### 7.3 Web search provider [OPEN → recommendation]

**Recommend: Anthropic's server-side `web_search` tool + `web_fetch` for v1.** Rationale:

- `web_search` is $10/1k searches + tokens — 2× the market anchor (Brave/Perplexity $5/1k,
  Serper $0.30–1/1k) but the market options return SERP snippets, so you'd build fetching,
  extraction, and citation plumbing on top; the built-in tool returns citable, content-bearing
  results inside the same API call, **with enforced citations that plug directly into the §8
  citation validator** (web citations carry `url` + `cited_text`). At expected advisor volumes
  (≤ a few k searches/mo) the absolute difference is tens of dollars.
- Newer tool versions add **dynamic filtering** (model-written filters run *before* results
  enter context — a direct context-discipline win, §9) and `response_inclusion: "excluded"`.
- **`web_fetch` has no per-request charge** (tokens only), handles PDFs, and has
  anti-exfiltration constraints — it *is* the design's `fetch_url` tool, for free.
- Caveats to record: results are opaque (`encrypted_content` — no raw SERP to persist), and
  there's no neural/semantic search. If "find startups similar to X" web-discovery becomes a
  real pattern, add **Exa** ($7/1k) as a second, client-side tool. **Framework dependency:**
  whether `anthropic-haystack` passes server-side tool blocks through cleanly is one of the
  §6.4 day-1 spikes — if it doesn't, fall back to Serper ($0.30–1/1k) + fetch/extract as
  client-side tools, which works in any framework. (Bing Search API is retired — Aug 2025 —
  for anyone still holding that mental model.)
- `cost.py` already has `_tool_web_search` per-call rows — wire them in.

### 7.4 pgvector / lexical leg [was implicit → now explicit]

Covered in §4.4: stay on pgvector (re-affirmed with July-2026 data — consensus and benchmarks
put pgvector comfortable to single-digit millions of vectors; you are at ~10⁴); Cloud SQL
allowlist excludes every BM25 extension, so the lexical leg is tuned native FTS with the
documented upgrade ladder (sparse vectors via `sparsevec` being the interesting middle rung —
it works on Cloud SQL today). Enable the `vector` extension on prod as an explicit migration
step. AlloyDB is the escape hatch if search-perf ever binds (ScaNN + announced-but-unshipped
native BM25), not a present need.

### 7.5 Entity-profile rendering & `structured_query` surface [OPEN → recommendation]

- **Field lists**: start from `ReportChatService::buildCompanySection`/`buildTeamSection`
  (startups + teams — already curated), extend the same pattern to investors
  (`investment_thesis`, `focus_statement`, `value_beyond_capital`, stages/sectors/check size),
  CVs (headline, experiences w/ polymorphic `companyable` names, education, skills), funds
  (`investment_thesis`, `executive_summary`, fund_size_*), organizations. Convert TipTap HTML →
  markdown; resolve label arrays (`industries`, `sector_focus`) through the lookup tables so
  profile metadata is normalized even when source labels are messy. Include latest eval score +
  one-line eval summary on the startup card (it's the single most-asked-about attribute in an
  investor CRM).
- **Refresh**: periodic sweep + content-hash comparison (§5.5), regenerating only changed
  profiles; at 300 entities a full nightly regeneration is also acceptable — don't build
  event plumbing.
- **`structured_query`**: a small registry of named, parameterized, read-only queries
  (`count_startups(filters)`, `list_startups(filters, fields)`, `investments_by_investor(id)`,
  `top_by_score(n, filters)`…), each validated against a **shared filter-resolver registry**
  (one resolver per lookup domain — industries, regions, stages — reused across queries, not
  per-field one-offs). Enforce read-only both by construction (no free SQL) and by connecting
  as a `SELECT`-only DB role (stronger than the old `assertReadOnlySelect()` string guard —
  though replicate that too, defense in depth).

### 7.6 Tier-1 vs Tier-2 router [OPEN → recommendation]

**Don't build a router for v1 — make deep research an explicit user action** (a "go deep"
button/command), defaulting everything to Tier 1. Rationale: routers are a classification
problem you can't tune without traffic; explicit opt-in matches the product framing ("research
buddy… waits for the next turn"), keeps cost predictable, and produces labeled training data
(which questions users escalate) for a future auto-router. Revisit after there's a Tier-2 to
route *to* — which per §8 is deliberately late in the build order.

---

## 8. Right-sized build order

The design's own "Next" step is correct; this sharpens it against everything above:

1. **Foundations** (day-scale): create `advisor` schema (+ decide fate of `advisor_obs`/
   `advisor_evidence` leftovers); enable pgvector on prod; add `ANTHROPIC_API_KEY`; pin
   embedding model (7.1) + add its `cost.py` row; stored embedding-config row; run the three
   §6.4 integration spikes (cache-token usage fields, server-tool passthrough, newest-model
   kwargs) before committing to the Haystack path.
2. **Ingest a real slice, breadth-first**: eval-bundle backfill (§5.3.1 — no LLM cost, covers
   evaluated startups) + entity profiles for startups/team/investors (§7.5) with ACL metadata
   stamped (§5.2.1). Page-aware structure chunking; block-level only; record-summary blocks.
3. **Retrieval + eval harness together**: hybrid RRF pipeline + entity diversification (§4.4);
   golden-question set (§5.4) *before* tuning anything; then decide reranker on/off (§7.2).
4. **Tier-1 agent**: `search_corpus`, `get_source`, `web_search`/`web_fetch` (§7.3), citation
   validator (§8 of the design), `cost.py` wired to every call, Langfuse tracing, per-user ACL
   filter (even if v1 audience = admins only), thread/turn persistence (§5.5).
5. **Then and only then**: extraction path for never-evaluated docs (§5.3.2), `structured_query`
   registry (§7.5), freshness sweep (§5.5), UI integration.
6. **Later, evidence-driven**: sentence granularity, block-level diff-indexing, Tier-2 deep
   loop, auto-router, layout-aware extraction/bboxes (§5.1), sparse-vector lexical upgrade.

---

## 9. Evidence index

- Live DB inspection (2026-07-07): test `x1-db-test` + prod `x1-db` via cloud-sql-proxy,
  read-only. Row counts, column shapes, extension lists as quoted in §2.
- GCS: `gs://x1-app-www-test` blob samples (`doc-extract/f56b…/9403….json`,
  `reports/x1_pipeline_df84848c….json`); prefix counts on test + prod buckets.
- `x1-backend` (branch `mastra-endpoints`): `mastra/extract/src/lib/document-loader.ts` (Gemini
  prompt, page delimiters), `mastra/extract/src/lib/cloud-cache.ts` + `sql/001-create-cache-table.sql`
  (cache contract), `mastra/eval/src/runtime/job-runner.ts` (bundle shape/paths),
  `mastra/eval/src/runtime/evaluation-insert.ts` (DB row), `mastra/import/src/service/run-job.ts`.
- `x1-app` (branch `link`): `app/Services/ReportChatService.php:127` (profile-card precedent),
  `app/Services/SearchService.php:90` (`is_published` filtering),
  `app/Http/Controllers/SharedDocumentController.php:37` (visibility semantics),
  `app/Models/StartupCompanyEvaluation.php:61` (raw_json = GCS path), migrations for enum
  drops and `visibility`.
- References: `docs/refs/pipeshub-ai.md`, `docs/refs/chroma.md`, `docs/archive/chats/chat1/2.md`,
  `x1-link/docs/advisor/HERMES-LESSONS.md`, `x1_advisor/cost.py`.
- July-2026 component research (embeddings/rerankers/web-search/pgvector/extraction/BM25):
  verified against official docs & pricing pages; key sources include
  docs.voyageai.com, platform.claude.com tool docs, github.com/pgvector/pgvector,
  cloud.google.com/sql/docs/postgres/extensions & release-notes, docs.paradedb.com,
  github.com/timescale/pg_textsearch, github.com/docling-project/docling, mistral.ai (OCR 4),
  tavily.com/brave.com/serper.dev/exa.ai pricing pages.
- July-2026 framework research (§6): haystack.deepset.ai/release-notes,
  github.com/deepset-ai/haystack-core-integrations (anthropic + pgvector changelogs and
  source), docs.haystack.deepset.ai (agents, pgvectorkeywordretriever, documentjoiner),
  platform.claude.com (citations.md, search-results.md, tool docs),
  anthropic.com/engineering (building-effective-agents, multi-agent-research-system,
  effective-harnesses-for-long-running-agents), langchain.com/blog (series-b,
  open_deep_research), github.com/mastra-ai/mastra/issues/13226, mastra.ai/blog
  (announcing-mastra-1, series-a), pydantic.dev/docs/ai, pypistats.org, and the
  LlamaIndexTS archive notice. Unverified items are flagged inline (§6.1.5, §6.4 spikes).
