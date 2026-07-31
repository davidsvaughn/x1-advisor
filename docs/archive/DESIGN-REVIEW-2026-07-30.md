# Design review 2026-07-30 — fresh-eyes pass (for second-agent review)

> **Audience: a reviewing agent.** This is a self-audit of the x1-advisor design and
> implementation as of commit `0c06b13`, written by the agent that built most of it.
> Your job: **verify or refute each claim against the code**, challenge the priority
> ordering, and surface anything this review missed. Every claim carries file:line
> evidence and a confidence level. Do not take the claims on trust — several are
> exactly the kind of thing the author of the code is likely to be wrong about.
>
> Context to load first: [`PLAN.md`](../PLAN.md) (the working plan; phases + §9 context
> discipline), [`DECISIONS.md`](../DECISIONS.md) (dated evidence log), then the modules
> cited below. The project: a conversational research agent over the X1
> startup/investor CRM (Postgres entities → markdown profile docs, eval-report
> bundles, pitch decks, web) returning source-grounded answers with validated
> citations. Stack: Haystack Agent (used shallow) + gpt-5.1, plain-SQL hybrid
> retrieval on pgvector + FTS, retriever-level ACL, cost accounting on every call,
> Langfuse tracing. Status: Phases 0–4 complete with measured exit criteria;
> Phase 5 (service) half-built; Phase 3 bake-offs partially run; Phase 6 (prod
> cutover) not started.

## 1. State snapshot (verified 2026-07-30, all claims re-checked against disk)

- Corpus on x1-db-test, `advisor` schema: **412 live docs / 7,693 chunks** (incl. one
  `record_summary` chunk per doc), all embedded under config `te3s_1536_ck1`
  (text-embedding-3-small, 1536d).
- Retrieval quality, golden v1 (45 Qs, 36 gradable): **recall@10 0.833, MRR 0.746**,
  28/36 full recall, 4/36 zero recall. Record summaries were the big lever
  (+0.055); the E2 reranker bake-off was a wash → **v1 ships RRF-only** (DECISIONS
  2026-07-08/09).
- Agent exit run: **63/63 citations resolvable (100%)**, mean **$0.011/turn**, seeded
  ACL probes pass in both directions (no leak for non-purchasers; purchasers see
  purchased premium).
- Working providers: OpenAI company-paid default (chat/embed/web search); DeepSeek
  wired but opt-in (personal key); Anthropic/Voyage keys still absent.
- Tests: 5 unit tests passing, incl. a SHA-256 pin on `SYSTEM_PROMPT` (prompt-cache
  stability guard).
- Open David decisions: drop `advisor_obs` (10 GB) / `advisor_evidence` (133 MB);
  prod `CREATE EXTENSION vector`; keys; budget caps; private-doc treatment
  (PLAN §5.1).

## 2. Findings — implementation defects

Ordered by severity. For each: claim, evidence, proposed fix, confidence.

### F1. SQL injection through metadata filter *keys* (security — fix before any non-admin exposure)

- **Claim:** `_filter_sql` interpolates the filter **key** into SQL unparameterized:
  `x1_advisor/retrieval.py:94` (`c.metadata->'{key}'`) and `:97`
  (`c.metadata->>'{key}'`). Values are parameterized; keys are not. Filter dicts
  originate from the LLM (`search_corpus` in `x1_advisor/agent/tools.py:46-53`,
  which validates only `entity_type`), and the LLM reads corpus + web content — so
  the injection chain is: *malicious deck/website text → prompt-injected agent
  passes a crafted filter key → arbitrary SQL inside the ACL-bearing query*.
  Exploitation requires steering the model, but the query carries the ACL
  predicates, so a successful injection is an ACL bypass, not just a crash.
- **Proposed fix:** whitelist filter keys to a fixed enum (entity_type, source_type,
  company_name, section_key, and whatever else ingest actually stamps). This also
  creates the natural seat for value validation (see F7).
- **Confidence: high** that the interpolation exists and is LLM-reachable;
  **medium** on real-world exploitability (gpt-5.1 must be induced to emit a
  malicious key — plausible via corpus-content injection, unproven).
- **Reviewer question:** is there any *other* unparameterized identifier reachable
  from model output? (I checked `queries.py` — all five queries parameterize; the
  `emb_{cfg.id}` table-name interpolation at `retrieval.py:184-186` comes from the
  server-side CONFIGS registry, not the model — please verify.)

### F2. One shared psycopg connection across concurrent requests (correctness + throughput)

- **Claim:** the service opens one connection per worker (`x1_advisor/service.py:29`)
  and `/ask` runs in anyio's default thread pool (`service.py:75`, capacity ~40
  threads). psycopg3 connections are thread-safe (internally locked, operations
  serialized) so nothing corrupts, but all concurrent requests share **one
  transaction context**: `save_turn`'s `conn.commit()`
  (`x1_advisor/agent/advisor.py:205`) commits whatever *other* in-flight requests
  have pending; an error in one request's transaction poisons statements from
  another; and every DB call across all requests serializes on one socket while
  agent turns run 10–40 s. Separately: after Cloud Run idle, Cloud SQL drops the
  connection and `/health`+`/ask` 500 forever — there is no reconnect path.
- **Proposed fix:** in-process `psycopg_pool.ConnectionPool(min_size=1, max_size=4)`
  per worker, checkout per request. This preserves the 2026-07-07 no-PgBouncer
  decision (that was about a network sidecar; this is a library pool).
- **Confidence: high** on the shared-transaction hazard and the idle-drop failure;
  **medium** on psycopg3's exact locking semantics — reviewer should confirm
  against psycopg3 docs (Connection is documented thread-safe; cursors are not).

### F3. Gated-vs-absent note only fires on *fully empty* results (product / revenue surface)

- **Claim:** `x1_advisor/agent/tools.py:59` gates the access-note logic on
  `if not hits and acl != "admin"`. If a search returns 1 open hit and 7
  purchase-gated ones, the user gets a thin answer with no signal that paid
  material exists. Premium gating is a revenue surface — the partial case is
  arguably *more* valuable than the empty case (the user is already engaged with
  the entity).
- **Proposed fix:** whenever `acl != "admin"`, compare open-result count against an
  admin-scope count (class + count only, as today — never titles/content) and
  attach the note when the delta is nonzero. Cost: one extra retrieve on
  non-admin searches (embedding re-use, see F8, makes this nearly free).
- **Confidence: high** on the behavior; **medium** on the product call — David may
  prefer notes only on total misses to avoid upsell noise. Flag for him.

### F4. Prompt-cache CI guard covers only half the cached prefix

- **Claim:** the OpenAI prompt-cache prefix is system prompt **+ tool schemas**, but
  `tests/test_agent_units.py:15-23` pins only `SYSTEM_PROMPT`. Tool descriptions
  changed four times during Phase 4 alone (enum enumeration, web-search trigger
  conditions, …) — each silently invalidated the cache with no test failure. §9
  calls context discipline the top priority; the guard should match it.
- **Proposed fix:** extend the pin to a canonical serialization of the four tool
  schemas (name + description + parameters, sorted keys). Note `structured_query`'s
  description embeds `catalog()` (`tools.py:188`) — the pin then also covers the
  query registry, which is correct: registry edits invalidate the cache too.
- **Confidence: high.** (Verify the assumption that OpenAI's cache prefix includes
  tool definitions — Anthropic's documented behavior; OpenAI's cache is automatic
  and the boundary is undocumented, but tools precede messages in the request.)

### F5. Raw pre-validation answer is discarded (data-loss policy)

- **Claim:** `validate_citations` rewrites the answer (drops unresolvable refs,
  renumbers) and only the rewritten text is persisted
  (`advisor.py:163-177`, `save_turn` at `:185-206`). Debugging "why was ref3
  dropped" is impossible after the fact. This brushes the project's
  no-silent-data-loss rule.
- **Proposed fix:** store `raw_answer` inside `research_record` JSONB. One line.
- **Confidence: high.**

### F6. History is client-supplied, unbounded, and ignores persisted turns

- **Claim:** `/ask` accepts an arbitrary `history` array (`service.py:37-40`) and
  never reads the turns already saved under `thread_id`. Consequences: (a) cost
  amplification — a client can post megabytes of history straight into token
  spend; (b) fabrication — the client can supply assistant turns the assistant
  never said; (c) the condense step (`advisor.py:78-93`) re-summarizes older
  history on *every* turn instead of caching the summary per thread.
- **Proposed fix (Phase 5, with real auth):** server-side history — reconstruct
  from `advisor.turns` by `thread_id`, cap length, persist the rolling condensed
  summary on the thread row. Client sends only `question` + `thread_id`.
- **Confidence: high.** Deliberately deferred (the REPL/dev clients are trusted),
  but it must land inside the non-admin exposure gate (§5).

### F7. Filter values are unvalidated → silent empty results (recurrence of a known failure class)

- **Claim:** the `entity_type` enum bug (DECISIONS 2026-07-08, "instrumentation
  catch #5": model passed `startup`, metadata says `startup_company`, every search
  matched nothing) is fixed for `entity_type` only (`tools.py:46-51`). The same
  failure mode is live for every other filter key: `company_name` is exact-match
  (`retrieval.py:97-98`), so `filters={"company_name": "Accelium"}` vs the stored
  "Accelium GmbH" returns zero rows with no signal about *why*.
- **Proposed fix:** with the F1 whitelist in place, validate filter values against
  known values (cheap `SELECT DISTINCT` cache) and return "filter X matched 0
  known values; nearest: […]" in the tool result. Class-based, not query-specific
  — same registry-resolver shape the project rules already prescribe.
- **Confidence: high** on the mechanism; **medium** on frequency (golden v1 mostly
  avoids it because questions use canonical names).

### F8. Minor inefficiencies (note-and-move-on tier)

- Query embedded twice on the gated-note path (`tools.py:53` then `:61` → two
  `retrieve()` calls → two embedding API calls). Pass the vector through.
- A new `OpenAI()` client per `retrieve()` call (`retrieval.py:170`). Harmless;
  tidy up when the embed seam moves (D2).
- `web_research` silently caps sources at 8 (`tools.py:157`) — should be flagged
  like every other cap per the tool-contract convention.
- `hash()` for dedup (`retrieval.py:226`) is per-process salted — fine because
  `seen_text` is per-call; note so nobody persists it.

## 3. Findings — plan-vs-implementation drift

### D1. The generator registry (PLAN §1) was never built

Model choices live as scattered constants: `AGENT_MODEL`/`CONDENSE_MODEL`
(`advisor.py:29,70`), `WEB_MODEL` (`tools.py:31`), the summary model in
`ingest/summaries.py`. The plan promised `get_chat_model(role)` with per-role
model ids in config. ~30 lines closes it and makes E4 bake-offs config changes.

### D2. Query embedding is hard-coded to OpenAI inside `retrieve()`

`retrieval.py:170-176` constructs an OpenAI client directly. Consequence: **the E1
embeddings bake-off is blocked in code, not just on Voyage keys** — a `voyage-4`
index config cannot be *queried* without editing the retrieval function, despite
the config registry advertising pluggability. Fix: `embed_query(cfg, text)`
dispatching on a provider field in `IndexConfig`.

### D3. The HNSW index is dead weight (and the code looks like it scales when it doesn't)

The dense leg (`retrieval.py:180-189`) orders by a correlated subquery
(`ORDER BY (SELECT e.embedding <=> %s … WHERE e.chunk_id = c.id)`), which the
planner cannot serve from the HNSW index → every query is an exact scan over the
ACL-filtered candidate set. At 7.7k chunks this is *good* (exact = perfect recall,
~ms latency; DECISIONS baseline measured ~420 ms/query total). But we build and
maintain an index that is never read, and the query shape silently stops scaling
somewhere around ~100k chunks. Either drop the index build or leave a loud marker
that the SQL must be restructured (index-served ANN + post-filter) at scale.
**Reviewer: please confirm the planner claim** — an `EXPLAIN ANALYZE` on test
settles it in one query.

### D4. Unused dependencies ship in the service image

`anthropic-haystack`, `pgvector-haystack`, `anthropic` (pyproject.toml:8-10) have
no import on the serving path (retrieval went plain-SQL; Anthropic awaits keys).
Prune from the deploy image (keep available for spikes), shrinking image size and
supply-chain surface. **Reviewer: grep for imports before agreeing** — I checked
`x1_advisor/` but not `spikes/`/`experiments/` (those can keep dev-group deps).

## 4. Strategic reconsiderations (the "step back" part)

### S1. Biggest eval gap: citation *resolvability* ≠ citation *faithfulness*

The 100% exit metric proves every ref points at a real block — not that the block
supports the claim in front of it. A model citing plausibly-but-wrongly scores
perfect today. Everything needed for a faithfulness judge already exists (golden
set, `research_record` per turn, Langfuse scores wired, judge role reserved in
E4b): an LLM-judge entailment pass over (claim, cited block) pairs, emitted as a
Langfuse score next to `citation_resolvability`. **This is the highest-leverage
next investment** — it also unblocks E3 and E4a, which need answer-level grading
to be decidable. Recommendation: build before any further retrieval tuning.

### S2. Don't let E1 wait on Voyage keys

`text-embedding-3-large` is available now; full-corpus embed ≈ well under $1;
after D2 the harness is one config-id away. Even a two-candidate E1 answers
whether the 4 zero-recall questions are embedding-limited or structurally beyond
retrieval. Voyage joins later without redesign.

### S3. Add a third epistemic state: "exists on platform, not indexed"

Today the agent distinguishes found / gated / absent. Until Phase 6's extraction
path lands, never-evaluated startups have only a profile doc — users will ask
about them and get flat misses. Class-based fix: on an entity-shaped miss, check
platform existence via `structured_query` and answer "X exists on the platform
but has no indexed documents yet." Honest-coverage messaging; value grows the
longer Phase 6 waits.

### S4. The web searcher is probably over-modeled

`web_research` delegates to gpt-5.1 (`tools.py:31`); its job is distillation of
injected search results, not reasoning. gpt-5-mini as searcher is a zero-new-keys
experiment that plausibly cuts web-turn cost several-fold — the natural first E3
datapoint.

### S5. Golden-set portability at prod cutover

Matchers key on company_name/title/section_key (portable), but the corpus behind
them is test fixtures. At cutover: re-run golden v1 against prod-ingested data
before trusting it; expect some re-grounding. And golden v2 should be grown from
real `research_record` threads — the plan's own risk table says so and the data
is already accumulating.

### S6. Bundle a "before non-admin exposure" gate

F1 + F2 + F6 + the real auth→ACL resolver + a request-size guard are individually
small but collectively the difference between "admin demo" and "safe for a
founder." Treat as one named gate, not five scattered TODOs — the audience
decision could arrive suddenly.

### S7. Explicit non-changes (reviewer: challenge these hardest)

- **Haystack stays** — used shallow (one Agent + Tool wrappers), the thin-stack
  exit ramp remains cheap, and it has caused zero integration pain since the gate.
- **RRF-only stays** until golden v2 or the ~2× prod corpus reopens E2 (the slot
  is one flag).
- **No PgBouncer, no Tier-2 deep mode, no sentence granularity, no auto-router** —
  the plan's deferral discipline is correct; F2's in-process pool is not a
  reversal of the PgBouncer decision.

## 5. Proposed priority order (for review)

1. **F1** filter-key whitelist (+F7 value validation while in there) — security,
   small.
2. **F2** connection pool — correctness under any concurrency, prerequisite for
   letting anyone else touch the service.
3. **S1** faithfulness judge + Langfuse score — the eval gap everything else
   depends on.
4. **D1+D2** registry/embed-seam consolidation, then **S2** (cheap E1) and **S4**
   (mini searcher E3 datapoint).
5. **F6 + SSE + auth resolver** as one Phase-5 push behind the **S6** gate.
6. F3/F4/F5/F8/D3/D4 folded in opportunistically alongside the above.

## 6. Questions for the reviewing agent

1. Does any injection path beyond F1 exist from model-controlled input to SQL?
   (Check `_filter_sql` callers, `queries.py` params, table-name interpolations.)
2. Is the F2 diagnosis of psycopg3 semantics right (thread-safe connection, shared
   transaction context, no auto-reconnect)?
3. `EXPLAIN` check for D3: does the dense-leg query really bypass HNSW?
4. Is S1 (faithfulness judge) actually the top-leverage item, or would you rank
   prod cutover realism (S5/Phase 6) higher given the corpus is fixtures-based?
5. What did this review *miss*? Specifically look at: `ingest/store.py`
   version-and-append identity logic, `evidence.py` ref-parsing edge cases
   (e.g. refs inside code spans or URLs), `telemetry.py` failure modes,
   ACL-class completeness vs PLAN §0.2 (is any gated class un-predicated?),
   and the Dockerfile/cloudbuild for secret handling.
6. Are any of the §7 non-changes (S7) wrong — in particular, is keeping Haystack
   still justified now that the Agent loop is the only thing it provides?
