# X1 Advisor — independent architecture and plan review

> Date: 2026-07-30
>
> Scope: independent evaluation of the overall X1 Advisor architecture, working
> plan, decisions, current implementation, experiment design, security boundary,
> service/runtime design, and rollout order. This is not a review of
> `DESIGN-REVIEW-2026-07-30.md`, although that document was fact-checked and is
> assessed below.
>
> Evidence reviewed: `ARCHITECTURE.md`, `ARCHITECTURE-REVIEW.md`, `PLAN.md`,
> `DECISIONS.md`, `HANDOFF.md`, the ingestion/retrieval/agent/service code, the
> golden set and experiment harnesses, current checked-in run artifacts, and the
> unit tests. The implementation reviewed is commit `71b13c0`; its only change
> from implementation commit `0c06b13` is the other review document.

## 1. Verdict

The core architecture is sound, but the project is best described as a
**validated admin prototype**, not “Phases 0–4 complete.”

Continue with the architecture, but insert explicit correctness and
production-safety gates before model bake-offs, UI work, or non-admin exposure.
The largest risks now are not embedding quality or framework choice. They are:

1. the boundary between retrieval aids and citable evidence;
2. an evaluation harness that is not strong enough to support the decisions
   being made from it;
3. incomplete end-to-end authorization;
4. an unsafe service state/concurrency model; and
5. production data coverage and freshness being scheduled too late.

The strongest choices should remain:

- Retrieval-first rather than generated SQL.
- Denormalized entity profiles alongside document evidence.
- Postgres/pgvector in a dedicated schema.
- Page/block citations and version-and-append.
- A bounded Tier-1 loop, with deep research deferred.
- Class-based ACL filtering at retrieval time.

## 2. Findings

### P0. Generated summaries cross the citable-evidence boundary

This is the most important issue missed by the existing fresh-eyes review.

`x1_advisor/ingest/summaries.py` generates a record summary from only the first
6,000 characters of each document:

- `DOC_HEAD_CHARS = 6_000`
- `body=row["markdown"][:DOC_HEAD_CHARS]`

The prompt nevertheless asks for a summary of “this document.” For a long deck,
evaluation report, or CV, the generated text may omit most of the source while
presenting itself as a document-level summary.

That text is inserted into `advisor.doc_chunks` as
`granularity='record_summary'`, but `retrieval.Hit` does not carry granularity.
`search_corpus` therefore registers and returns record summaries exactly like
primary source blocks. The agent can cite a generated, partial summary as if it
were source evidence.

This is not theoretical. In the current retrieval artifact:

- record-summary chunks appear in the top ten for 30 of 36 questions;
- there are 122 record-summary hits across those questions; and
- a record summary ranks first for 12 questions.

The reported 63/63 end-to-end citation run predates record summaries
(`git_sha=6033a17` in `experiments/runs/2026-07-08_agent_v1.jsonl`), so it does
not test the current evidence path.

#### Recommendation

- Mark LLM summaries `retrieval_only`.
- Use a summary hit to identify a document, then expand to original source
  blocks before evidence is returned to the agent.
- Never allow a generated summary to be the terminal citation.
- If summaries remain, summarize the whole document through an explicit
  hierarchical strategy rather than a silent head slice.
- Keep deterministic entity profiles citable; they are denormalized source
  records rather than free-form LLM summaries.

The original architecture’s idea that ordinary agent output should become
future retrievable evidence should also be reversed by default. It creates
circular sourcing and self-reinforcing hallucination. Store turns for history
and evaluation, but admit only deliberately curated research notes carrying:

- their original source citations;
- a verification state;
- derivation/provenance links; and
- max-restrictive inherited ACL.

### P0. The evaluation harness cannot support current quality decisions

The golden set and retrieval harness are useful smoke tests, but
`recall@10=0.833` is not a trustworthy product-quality metric.

Several expected-source matchers are materially weaker than their questions:

- A comparison question may pass after retrieving only one side.
- “Which companies have both a pitch deck and website?” does not require the
  two hits to belong to the same company.
- Plural enumeration questions often require only one matching profile.
- Aggregate questions are graded as retrieval questions even though the design
  assigns them to `structured_query`.
- Broad advice questions may pass after retrieving a single investor profile.

The grader only asks whether at least one top-k hit matches each loose metadata
matcher. It does not assess whether the retrieved blocks contain the facts
needed to answer.

Likewise, “100% citation resolvability” proves only that emitted `refN` tokens
belong to the per-turn registry. It does not measure:

- whether the cited block supports the adjacent claim;
- whether important factual claims are uncited;
- whether the answer covers the question;
- whether the system abstains correctly;
- whether source quality is appropriate; or
- whether a generated record summary distorted its source.

The E3 web subset is not yet decision-grade either: nine time-varying questions
have no grading implementation, reference facts, source-quality rubric, or
frozen evaluation date.

The ACL suite also has weak negative-test construction. Apart from the
purchased-premium case, it does not first prove that the targeted gated source
is retrieved under admin scope. A probe can therefore pass because the
sensitive source did not retrieve at all.

#### Recommendation

Build a golden v2 with separate layers:

1. **Retrieval sufficiency**
   - required documents and/or required facts;
   - same-entity constraints for cross-source questions;
   - enumeration coverage where applicable;
   - typed expected tool route (`search_corpus`, `structured_query`, web).
2. **Claim-level citation evaluation**
   - citation entailment/faithfulness;
   - citation coverage over factual claims;
   - invalid or misleading attribution;
   - source-quality classification.
3. **Answer behavior**
   - completeness;
   - correct abstention;
   - restricted-versus-absent handling;
   - current-versus-snapshot handling.
4. **Operational metrics**
   - latency;
   - cost;
   - tool steps;
   - context growth;
   - provider failures.

An LLM judge is appropriate for scale, but calibrate it against a small
human-labeled claim/citation set. Do not turn the judge’s own score into another
unverified proxy.

### P0. Authorization is not end-to-end

The filter-key SQL injection in `retrieval._filter_sql` is real and
model-reachable. Model-supplied filter keys are interpolated into quoted JSON
expressions while values are parameterized. A malicious key can break out of
the JSON-key literal and alter the ACL-bearing query.

The correct fix is broader than a key whitelist plus a cache of
`SELECT DISTINCT` values. The search tool needs a typed filter DSL:

- explicit properties in the tool schema;
- an allowlisted set of operators per field;
- type and range validation;
- canonical-value/entity resolvers; and
- a compiler that emits only fixed SQL fragments.

Other authorization gaps:

- A client may submit any `thread_id`; `save_turn()` does not verify ownership
  before appending turns.
- `structured_query` does not receive the requesting ACL.
- The future persisted-citation endpoint is unspecified. The current
  request-local `get_source(ref)` closure is safe because it only accepts refs
  registered during that turn, but it cannot serve a citation opened later
  from a stored thread.
- A persistent source endpoint must re-evaluate current ACL because ownership,
  visibility, publication, and purchases can change after the citation was
  created.
- Private-document and premium policy decisions remain open, so the plan cannot
  truthfully say the ACL surface is ready.
- The ACL probes do not cover filter injection, structured queries, source
  rehydration, thread access, owned-private positive controls, or permission
  changes.

#### Recommendation

Define one request authorization context and require every data-bearing tool or
endpoint to consume it:

```text
verified service identity + verified X1 user identity
  -> request ACL context
     -> search_corpus
     -> structured_query
     -> source/citation fetch
     -> thread/history access
```

Add end-to-end positive and negative tests at every boundary. A retrieval-only
ACL test is necessary but insufficient.

### P0. The service/runtime design is not production-safe

The shared psycopg connection diagnosis in the existing review is correct.
Psycopg connections are thread-safe in the sense that operations are
serialized, but cursors on one connection share a transaction and error state.
The FastAPI service currently sends concurrent requests from the anyio thread
pool through one connection.

Required correction: a bounded in-process connection pool with one checkout
and transaction boundary per request.

Related gaps:

- Client-supplied history is unbounded, untrusted, and disconnected from
  persisted thread history.
- Question/history request sizes have no limits.
- Malformed structured-query values can raise uncaught `ValueError` or
  `TypeError`.
- Cloud Run concurrency is not explicitly matched to the database pool and
  model-call capacity.
- There is no application-level backpressure or concurrency semaphore.
- Request timeout, cancellation, and provider retry behavior are undefined.
- The `$0.50` cost cap is advisory: `Tracker.over_per_run_soft_cap()` is checked
  after spending and cannot stop a subsequent call.
- A daily cap is designed but not implemented.
- SSE streaming conflicts with citation post-validation. Streaming provisional
  text and later deleting or renumbering refs needs an explicit client protocol.

#### Recommendation

Before deploying even an admin pilot:

- use server-owned history reconstructed from an authorized thread;
- validate `role` and `content` rather than accepting arbitrary dictionaries;
- set request and output limits;
- use a bounded database pool and application concurrency limit;
- set Cloud Run concurrency deliberately;
- implement per-stage timeouts and honest degradation;
- preflight estimated spend before starting another model/tool step;
- define the SSE event protocol for provisional text, evidence refs, final
  validation, and errors.

### P1. “Model choices are experiments” is not implemented

The plan’s prime directive says models/providers are experiments behind stable
seams, but three of those seams do not exist:

- Agent, condense, web, and record-summary models are constants rather than a
  generator registry.
- Query embedding constructs an OpenAI client directly.
- Web research constructs an OpenAI Responses request directly; the planned
  `SearchProvider` interface was never built.

E1, E3, and E4 therefore require implementation edits, not config changes.

The existing review catches the generator registry and embedding seam but
misses the SearchProvider drift.

#### Recommendation

Build the seams only after the evaluation corrections above, then run paired
experiments from immutable manifests:

- generator role config;
- embedding provider + model + dimension config;
- search provider returning a normalized evidence contract;
- explicit provider/model versions and prompt/schema hashes.

Do not prioritize a second OpenAI embedding model yet. Of the four current
zero-recall questions, at least three are primarily aggregate, enumeration, or
filter-contract problems. A different embedding model will not resolve those
architecture mismatches.

### P1. Production data realism and freshness are scheduled too late

Phase 6 currently holds extraction, freshness, and production backfill. Those
capabilities determine whether the product has honest coverage and should
precede meaningful product-quality conclusions.

The test environment already demonstrates producer-contract drift: 75 of 79
test bundles use an unsupported shape and are deliberately skipped. Building
against the current production shape was reasonable for the prototype, but is
not a durable integration contract.

The ingestion path also lacks a first-class operational control plane:

- no ingestion-run or source-sync state;
- no persisted failure/retry state;
- no source watermark;
- no database-enforced uniqueness for one live source identity;
- no explicit source-dependency graph for derived ACL inheritance; and
- no contract fixtures shared with the bundle producer.

#### Recommendation

Move a minimal production-data admin rehearsal ahead of UI integration:

1. schema-versioned bundle adapters and producer-owned fixtures;
2. source sync/run state with errors and retries;
3. database constraints for live-source identity;
4. explicit provenance/dependency records;
5. controlled prod backfill and read-only admin pilot;
6. periodic freshness sweep with coverage reporting.

The useful state “entity exists on X1 but has no indexed evidence” should come
from an explicit coverage registry, not an ad hoc fallback query.

### P1. Retrieval is broadly right, with two qualifications

The dense query structurally bypasses HNSW: it filters `doc_chunks`, performs
embedding lookups by `chunk_id` through a correlated subquery, and then sorts.
At roughly 7,700 chunks, exact scanning is acceptable. This is future-scale
debt, not a current priority.

The more immediate gap is diversification. The review and plan discuss
group-by-entity diversification, but the implementation caps chunks per
**document**. A company can own many evaluation-section documents and still
dominate top-k.

#### Recommendation

- Diversify by canonical entity or evaluation bundle for broad questions.
- Allow entity-scoped searches to spend more of top-k on that entity.
- Preserve source-type diversity for comparison questions.
- Keep RRF-only as a provisional default, but rerun its decision after golden v2.

### P2. Framework choice is not the current problem

Keeping Haystack is acceptable tactically. The original rationale has weakened
because retrieval is plain SQL, citations are custom, web search uses the
OpenAI SDK directly, and the framework now provides mainly the agent loop and
tool wrappers.

That does not justify a migration today. Changing frameworks would not fix the
evidence, evaluation, authorization, runtime, or ingestion issues above.

Keep Haystack shallow and avoid new framework-specific state. Revisit only if:

- streaming/citation mechanics become materially harder;
- provider integration lag blocks a required product feature; or
- the corrected eval harness demonstrates loop behavior that is difficult to
  control.

## 3. Assessment of `DESIGN-REVIEW-2026-07-30.md`

The existing fresh-eyes review is strong on:

- F1 filter-key injection;
- F2 shared-connection transaction hazards;
- F5 raw-answer loss;
- F6 client-supplied history;
- D1–D4 implementation drift;
- S1 citation faithfulness;
- S5 production-corpus rehearsal; and
- S6 a named non-admin exposure gate.

Points of disagreement or correction:

1. **It misses the generated-summary evidence boundary.** This is more
   consequential than another model bake-off.
2. **It assigns too much confidence to the evaluation results.** Current recall
   and citation metrics are smoke tests, not exit criteria.
3. **It over-prioritizes E1.** Most current zero-recall cases are not embedding
   failures.
4. **F7’s proposed `SELECT DISTINCT` cache is too narrow.** The correct design is
   a typed filter and entity-resolution layer.
5. **F3 is a product-policy question, not clearly a defect.** Partial-result
   upsell messaging may be valuable, but revealing that restricted relevant
   material exists is itself a policy decision and potential side channel.
6. **F4 is valid but minor.** OpenAI prompt caching does include tool
   definitions in the stable prefix. A schema hash makes changes deliberate; it
   does not make legitimate tool changes wrong.
7. **S3 should be generalized.** “Exists but not indexed” belongs to a coverage
   model with restricted, stale, missing, and indexed states.
8. **Keeping Haystack is reasonable only as a tactical non-change.** It is not
   currently a strategic advantage worth optimizing around.

## 4. Revised execution sequence

### Gate 1 — Evidence correctness

- Make generated summaries retrieval-only.
- Preserve raw model answers alongside validated answers.
- Add calibrated claim/citation faithfulness and coverage grading.
- Strengthen golden questions and expected tool routes.
- Rerun the full agent suite against the current record-summary corpus.

### Gate 2 — Complete the security boundary

- Typed filter compiler and entity resolver.
- Request-scoped database transactions.
- Authorized thread ownership.
- ACL-aware structured queries.
- Persistent citation/source endpoint with current ACL revalidation.
- End-to-end ACL and injection tests.

### Gate 3 — Production-safe admin pilot

- Server-owned history.
- Request limits, timeouts, retries, backpressure, and bounded concurrency.
- Enforceable cost/tool budgets.
- Explicit SSE validation protocol.
- Minimal current-production backfill and coverage reporting.

### Gate 4 — Golden v2 from real usage

- Capture admin pilot questions and failure traces.
- Add answer utility, abstention, freshness, source quality, and coverage cases.
- Separate product failures from retrieval/model failures.

### Gate 5 — Provider/model experiments

- Build the generator, embedding, and search-provider seams.
- Run E1/E3/E4 through immutable paired manifests.
- Treat RRF-only and current model selections as provisional until then.

### Gate 6 — Non-admin expansion

- Finalize private-document, premium, and existence-disclosure policies.
- Exercise founder, investor, purchaser, owner, and revoked-access personas.
- Open the audience only after all data-bearing paths consume the same verified
  authorization context.

## 5. Documentation/state correction

The documentation set currently reports incompatible project states:

- `DESIGN-REVIEW-2026-07-30.md` says Phases 0–4 are complete.
- `PLAN.md` still contains open Phase-0 prerequisites and only one completed
  Phase-3 bake-off.
- `PLAN.md` leaves the main Phase-4 Agent item unchecked while declaring the
  phase exit met.
- `HANDOFF.md` contains materially stale “everything else not built” and “next
  action” language.

Replace phase-completion shorthand with a current readiness matrix:

| Readiness area | Current state |
|---|---|
| Test corpus ingestion | Prototype validated |
| Retrieval | Prototype validated; evidence-boundary correction required |
| Answer quality | Not yet measured adequately |
| ACL retrieval filter | Prototype validated; end-to-end boundary incomplete |
| Service | Skeleton only; not concurrency-safe |
| Production data coverage | Not established |
| Admin pilot | Not ready |
| Non-admin exposure | Not ready |

## 6. Verification notes

- `uv run pytest -q`: **5 passed**.
- Psycopg connection/thread/transaction semantics were checked against current
  Psycopg 3 documentation; the F2 diagnosis is correct.
- OpenAI prompt-cache documentation confirms that tools must remain identical
  for a stable cached prefix; the premise behind F4 is correct.
- The Cloud SQL proxy/socket was not running during this review. Current corpus
  counts and `EXPLAIN ANALYZE` could not be independently rechecked live.
  The HNSW conclusion is based on static analysis of the SQL query shape.

