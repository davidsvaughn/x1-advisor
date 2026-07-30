# X1 Advisor — review of QA loop, question bank, context snapshot, and revised plan

> Date: 2026-07-30
>
> Audience: the agent revising the design and implementation plan.
>
> Scope: independent review of:
>
> - `QA-LOOP-DESIGN-2026-07-30.md`
> - `QUESTION-BANK.md`
> - `CONTEXT-SNAPSHOT-DESIGN-2026-07-30.md`
> - `PLAN.md` §R
>
> Framing question answered: do the three proposals form one coherent bet — QA
> loop as instrument, question bank as test material, context snapshot as scope
> semantics — and do they contradict the agreed Step-0/Gate-1 priorities?
>
> Short answer: **yes, they form a coherent and high-value design; no, they do
> not fundamentally contradict Step 0 or Gate 1.** Before adopting them
> unchanged, correct the snapshot’s replay semantics, split exhaustive text scan
> from semantic corpus analysis, and strengthen the QA funnel/replay model.

## 1. Overall verdict

The plan is substantially stronger after §R.

The readiness matrix is honest, the gate sequence reflects the real
dependencies, and the three new proposals compose naturally:

- The **QA loop** supplies the diagnostic instrument.
- The **question bank** supplies realistic test material and product demand.
- The **context snapshot** supplies the missing working-set/scope semantics.
- The funnel connects them through `expected_route`, `context_fixture`, captured
  tool results, retrieval explain, replay, and comparison.

There is no fatal architectural problem. The known major risks remain:

1. generated record summaries currently cross the citable-evidence boundary;
2. answer quality is not yet measured adequately;
3. authorization is not end-to-end;
4. the service runtime is not concurrency-safe; and
5. production coverage/freshness is not established.

Those risks are now sequenced rather than ignored. The proposals introduce
three additional design issues that should be corrected before implementation:

1. an intensional filter that is re-materialized later is not a replayable
   snapshot;
2. `scan_corpus` conflates exact exhaustive search with semantic corpus
   analysis; and
3. the QA replay/funnel assumes stable evidence, a reusable stored ACL, and one
   mandatory expected-evidence set.

## 2. Composition: do the three proposals fit together?

Yes.

The intended flow is coherent:

```text
question bank case
  + expected route
  + optional context fixture
        |
        v
context resolver
  -> opaque selected/visible/working-set scope
        |
        v
agent turn
  -> complete turn bundle
  -> retrieval explain
        |
        v
funnel classifier
  -> routing / scope / retrieval / ranking / citation / answer label
        |
        v
replay + compare
  -> stage-level fix
  -> full-suite regression check
```

This is the right conceptual architecture for agent-driven QA. It makes
localization a structured lookup rather than forcing every reviewing agent to
reconstruct the execution path from raw logs.

The proposal set also exposes real product requirements that golden v1 missed:

- “this startup” and “these results” need page/working-set context;
- top-k retrieval cannot claim exhaustive enumeration;
- exact-quote requests need a distinct answer contract;
- “current page” versus “broader database” must be explicit;
- multi-turn sequences need sequence-level tests; and
- action requests must receive a graceful research-only decline.

## 3. Context snapshot review

### 3.1 Major correction: the proposed intensional snapshot is not replayable

The document says the snapshot is immutable for its turn and makes working-set
questions replayable. It also says a `filter_spec` is re-materialized against
the database at resolution time.

Those claims conflict. Replaying the same bundle after records or scores change
can produce a different entity set.

#### Recommendation: extensional v1 for both visible and full working sets

Persist the resolved IDs that were in scope for the turn:

```jsonc
{
  "schema": 1,
  "page": {
    "type": "search",
    "route": "/startups",
    "selected": null
  },
  "working_set": {
    "entity_type": "startup_company",
    "visible_ids": [3, 17, 42],
    "matching_ids": [3, 17, 42, 88, 91],
    "filter_spec": {"score_gte": 77, "industry": "healthcare"},
    "filter_contract_version": 1,
    "sort": "score desc",
    "total": 5
  }
}
```

The model still sees only a compact context line and opaque scope handles. The
IDs cost no prompt tokens. At the current X1 scale, carrying dozens or hundreds
of references in the request and turn bundle is trivial.

Keep `filter_spec` for:

- display and debugging;
- provenance;
- validating that app/advisor filter semantics agree; and
- future re-materialization comparisons.

Use `matching_ids` as the immutable turn scope and replay primitive.

This also avoids duplicating Laravel search behavior in Python. If the app and
advisor separately interpret `score_gte`, industry, publication, or other
filters, “passing the current filters” will eventually mean different things
on the page and in the answer.

If scopes later become too large to transmit, introduce a server-minted,
materialized `scope_snapshot_id`. Do not substitute a live query definition for
a historical snapshot.

### 3.2 Add an explicit selected scope

The proposed tool scope enum is:

```text
visible | working_set | all
```

Add:

```text
selected
```

“This startup” should map to an explicit scope handle. Do not make selected
behavior a hidden filter default inside tools.

### 3.3 Unknown context must degrade honestly

The proposal says an unknown page type becomes “no context” plus a log line.
That can silently widen a deictic question from the intended working set to the
entire corpus.

Instead, preserve a visible context status:

```text
context_status = resolved | absent | unsupported | invalid | stale
```

If the question depends on unresolved context, the agent should say that the
page scope was unavailable rather than answer corpus-wide.

### 3.4 Context message placement

The variable context should remain after the stable system/tool prefix, so it
does not disrupt caching of that prefix.

A separate server-controlled context/developer message is cleaner than
concatenating trusted context into user-authored text. If the current Haystack
message path makes that awkward, an explicitly delimited server-authored tail
in the user message is acceptable, provided:

- the client cannot write the authoritative context line;
- the model is told which block is server-resolved; and
- user text that mimics `[Context: ...]` cannot override it.

### 3.5 Multi-turn answer-set deixis

Conversational resolution of answer sets is acceptable for v1. Do not build
tracked answer-set state preemptively.

The existing multi-turn scripts should test:

- UI scope on turn 1;
- a narrowed answer set on turn 2;
- “pull exact quotes for each” on turn 3; and
- a fresh UI snapshot superseding prior UI scope.

Promote answer sets into explicit state only if those tests reveal a recurring
failure class.

### 3.6 Scope-error classification

`scope_error` is mechanically detectable for golden cases that declare an
explicit expected scope. It should not be inferred mechanically for arbitrary
production questions whose wording may be ambiguous.

Allow golden cases to declare:

```yaml
expected_scope:
  required: visible
```

or:

```yaml
expected_scope:
  allowed: [working_set, all]
```

This avoids false failures for questions that legitimately admit more than one
scope interpretation.

### 3.7 Actor-channel check

Nothing in the proposal recreates an actor channel. References and filters flow
app → advisor, and the advisor performs read-only research. Preserve the hard
rule that no tool result can become a page command, navigation request, filter
mutation, or app write.

## 4. Question-bank review

### 4.1 The bank is highly valuable, but it is not yet a golden set

The bank is a much better representation of the intended product than golden
v1. It includes real captured threads, repeated user phrasing, wrong-tool
temptations, coverage challenges, working-set questions, exact-quote
directives, and multi-turn sequences.

Its provenance is useful, but source frequency is not automatically product
priority:

- real captured turns should carry more weight than speculative examples;
- repeated copies of the same historical list should not count as independent
  user demand; and
- questions from abandoned copilot-era designs still need current-scope
  curation even after action commands are removed.

### 4.2 Status tags currently overclaim readiness

The legend says ✅ means answerable with the current corpus and tools. Several
questions marked ✅ require capabilities that are not implemented:

- historical comparison across evaluations;
- investor/startup relationship queries;
- average or comparison queries absent from the five-query registry;
- fund/CV enumeration;
- document inventory and coverage queries;
- corpus-wide thematic comparisons; and
- “recommendations with biggest impact,” whose underlying data contract is
  unclear.

Replace the single status with orthogonal fields:

```yaml
source_available: yes | partial | no | unknown
tool_ready: yes | no
scope: entity | selected | visible | working_set | corpus
operation: lookup | aggregate | exact_scan | semantic_analysis | comparison
context_required: none | page | prior_answer
golden_priority: smoke | core | extended
```

This separates “the data probably exists” from “the current agent can answer
correctly.”

### 4.3 Split `scan_corpus` into two capabilities

The question bank correctly establishes that top-k retrieval cannot answer
bounded enumeration honestly. But its SCAN category contains two different
tasks.

#### A. Exhaustive text scan

Examples:

- mentions “FDA,” “CE mark,” or “reimbursement”;
- contains “McKinsey,” “BCG,” or “Bain”;
- explicitly mentions regulatory risk.

Contract:

```text
scan_text(scope, query_or_phrases)
  -> per-entity matched | no_match | not_indexed | restricted
  -> exact matching passages and citations
  -> eligible/scanned/matched coverage counts
```

This can be deterministic FTS/phrase scanning.

#### B. Bounded semantic analysis

Examples:

- strong technical differentiation but weak commercialization;
- unusually strong operator experience;
- described as a platform rather than a single-product business;
- most common failure modes across evaluations.

Contract:

```text
analyze_scope(scope, question_or_rubric, limits)
  -> per-entity evidence gathered
  -> per-entity verdict with citations
  -> aggregate synthesis
  -> eligible/analyzed/insufficient-evidence counts
```

This is a bounded map/reduce research operation, not lexical scan. It needs
explicit entity, document, token, latency, and cost budgets.

Never translate “no lexical match” into a semantic negative. The honest result
is “no matching phrase was found in the indexed eligible text.”

### 4.4 Exact-quote behavior is a valid product contract

“Pull the exact quotes” and “show excerpts, not a summary” are stable semantics.
When invoked:

- use original source blocks, never record-summary chunks;
- return verbatim spans;
- identify searched scope and coverage;
- cite every quote; and
- distinguish unavailable, restricted, and no-match sources.

This is durable product guidance rather than a trace-specific prompt hack.

### 4.5 Golden-v2 sizing

Keep the proposed three-tier structure:

1. compact deterministic smoke suite: about 12 cases;
2. decision-grade core: about 40–60 cases plus scripts;
3. extended suite: about 80–100 cases plus real-thread replay corpus.

Do not jump directly from the bank to a 100-case expensive agent run. First make
the core cases precise enough to have:

- required answer facts or behaviors;
- acceptable evidence groups;
- allowed/required tool routes;
- scope/context fixtures;
- ACL persona where relevant; and
- deterministic versus stochastic grading rules.

## 5. QA-loop review

### 5.1 What is right

The following should be adopted:

- Local artifacts as QA source of truth; Langfuse as a mirror.
- Complete, schema-versioned turn bundles.
- Layered summaries above drill-down details.
- Persisting the exact tool outputs shown to the model.
- Prompt/tool/config/code fingerprints.
- Always-on bounded retrieval explanations.
- Immutable manifests that refuse overwrite.
- Structured replay and run comparison.
- Stage-level diagnosis rather than question-specific fixes.

These directly reduce the cost of future debugging.

### 5.2 Replay needs three modes

The proposed replay reruns the entire turn against current code. That cannot
distinguish model behavior from changes in:

- database contents;
- index state;
- ACL resolution;
- web results;
- model/provider behavior; or
- tool implementation.

Support three modes:

```text
replay --frozen-tools
  Reuse recorded tool outputs; rerun synthesis/validation only.

replay --live-tools
  Rerun retrieval/tools against current data; retain the recorded request and
  compare evidence sets.

replay --full
  Rerun the current end-to-end workflow.
```

`--frozen-tools` is essential for isolating “the model mishandled good evidence.”
`--live-tools` isolates retrieval/data changes. `--full` measures current user
behavior.

### 5.3 Never replay by trusting a serialized ACL

The bundle should persist:

- principal/user identifier;
- policy version;
- resolved ACL snapshot for forensic comparison; and
- test persona identifier, where applicable.

A live replay must re-resolve current authorization. Feeding the stored ACL dict
directly back into production tools can resurrect revoked access or replay an
admin entitlement outside its intended context.

If an admin-only forensic replay needs the recorded ACL, make that an explicit
read-only mode with loud labeling.

Gate 2 must cover bundle reads and replay execution. “Bundles are admin-only”
is a useful v1 policy but does not replace authorization in the replay path.

### 5.4 Funnel taxonomy needs richer answer and runtime labels

The E → R → S → C funnel is an excellent localization structure:

- expected/acceptable evidence;
- retrieved evidence;
- evidence shown to the model;
- cited evidence.

However, E should be modeled as **acceptable evidence groups**, not one
mandatory set. A correct answer may use a different source block supporting the
same fact.

Add:

| Label | Meaning |
|---|---|
| `tool_error` | Tool/provider/DB call failed |
| `runtime_error` | Timeout, cancellation, serialization, or unexpected exception |
| `context_error` | Context missing, invalid, stale, or unresolved |
| `scope_error` | Explicit golden scope contract violated |
| `citation_coverage_error` | Factual claim lacks an adequate citation |
| `answer_contract_error` | Incomplete answer, wrong scope, failure to abstain, or quote/directive violation |
| `synthesis_error` | Cited evidence does not support the generated claim |

Do not add `dense_miss` and `lexical_miss` as top-level labels. Keep those as
retrieval-explain detail under `retrieval_miss`.

“First failing stage” remains useful, but multi-part questions should retain
one label per required fact/behavior rather than collapsing away later-stage
failures.

### 5.5 ACL-block classification is not free

To classify `acl_block`, the QA harness must compare persona-scoped results with
an admin/control retrieval. A normal runtime explain record only knows what the
current ACL allowed.

The admin shadow evidence:

- belongs only in restricted QA artifacts;
- must never appear in the user-visible turn bundle; and
- should run only for ACL/persona cases, not every production request.

### 5.6 Bundle storage

Recommended:

- Postgres JSONB is canonical for turns because it is transactional and
  queryable.
- Harness runs export immutable JSONL artifacts for grep, comparison, and
  preservation outside a mutable test database.
- For long-lived production evidence, archive exports to an immutable object
  store rather than creating two independently writable canonical copies.

Persisted bundles contain entitled evidence text and untrusted corpus/web
content. The teacher runbook must treat all bundle text as data, never
instructions.

### 5.7 Always-on retrieval explain

Agree for v1. With fixed leg depths, IDs/ranks/drop reasons are small and highly
valuable. Store metadata and IDs, not duplicated full chunk bodies—the tool
messages already contain the text shown to the model.

Introduce sampling only if measured storage or latency demonstrates a problem.
Do not start with a debug flag that will be off during the important failure.

### 5.8 Replay flakiness

Keep `--times N`, but do not hardcode three replays for every fix.

- One replay is the default.
- Use repeated replay for known stochastic cases, model/provider changes, or a
  previously flaky label.
- Full-suite stochastic comparisons should use label distributions or
  thresholds rather than a single binary transition.

### 5.9 Anti-test-case-hacking mechanics need two adjustments

#### Promote failure classes, not every failure verbatim

Replace:

> Every fixed failure is promoted into the golden set.

With:

> Every novel failure class is normalized or parameterized into the golden set.

Otherwise the suite grows indefinitely with duplicate entity-specific cases and
creates the same narrow-test pressure the mechanism is trying to prevent.

#### “Zero broken” depends on suite type

Zero broken transitions is appropriate for deterministic smoke tests. It is too
strict for model-graded, web, or stochastic cases.

Use:

- zero deterministic regressions;
- bounded quality/cost/latency regression budgets;
- repeated samples for stochastic labels; and
- explicit quarantining of known flaky cases.

### 5.10 Held-out set

A held-out set is only meaningful if the teacher cannot read it during the
iteration loop. A `held_out: true` field in the same file is a convention, not a
blind test.

Keep blind cases outside the normal teacher context and reveal only aggregate
round-end results. Rotate or refresh them after they are exposed.

### 5.11 Fingerprints

In addition to the proposed fields, capture:

- dirty-worktree state or source-tree hash;
- corpus/index version or watermark;
- golden schema/version;
- context/filter contract version;
- ACL policy version;
- provider-returned model fingerprint/version where available; and
- relevant feature flags.

Git SHA alone does not identify behavior when the working tree is dirty or the
database/index changed.

### 5.12 Effort estimate

The proposed 3–4 days is optimistic if “complete” includes migrations, tests,
replay isolation, ACL-safe bundle access, and a reliable classifier.

A realistic focused estimate is approximately 5–7 engineering days:

- turn-bundle schema/persistence and capture hooks;
- retrieval explain across all drop stages;
- golden-schema changes and acceptable evidence groups;
- classifier and summary output;
- frozen/live/full replay modes;
- comparator and immutable manifest handling;
- unit/integration tests;
- bundle/replay authorization;
- runbook and documentation.

A thinner v1 can still land in 3–4 days if it is explicitly limited to:

- bundle capture;
- retrieval explain;
- immutable manifests;
- basic classifier; and
- one live replay mode.

## 6. Revised-plan and sequencing review

`PLAN.md` §R correctly absorbs the independent review’s Gates 1–6. Nothing in
the proposal set requires undoing Step 0.

### 6.1 Step 0

Keep as written:

- visibility leak fix;
- typed filter/security fix;
- in-process psycopg pool;
- immutable/no-clobber manifests; and
- raw-answer persistence.

The typed filter contract should become the shared foundation for later scope
and context semantics. Do not implement context filters as a separate SQL path.

### 6.2 Gate 1

The QA work belongs alongside Gate 1, but sequence its internals:

#### Gate 1A — observability foundation

- bundle capture;
- prompt/tool/config/code fingerprints;
- retrieval explain;
- manifest immutability.

#### Gate 1B — evidence correctness

- record summaries retrieval-only;
- source-block expansion;
- whole-document summary correction;
- calibrated claim/citation judge;
- full agent rerun on the current summary corpus.

#### Gate 1C — QA loop completion

- funnel classifier;
- replay modes;
- run comparator;
- teacher runbook.

Do not let the full QA package delay the small, known evidence-boundary
correction. Instrument first, correct it, then industrialize replay/compare.

### 6.3 Gate 2

Keep end-to-end authorization here, but include:

- bundle read authorization;
- replay authorization and stale-ACL handling;
- persisted citation/source endpoint;
- structured query ACL;
- thread ownership;
- context-scope intersection with ACL; and
- admin-shadow QA artifact isolation.

### 6.4 Gate 3

Split Gate 3 conceptually:

#### Gate 3A — production-safe admin pilot

- server-owned history;
- request limits;
- timeouts/retries/backpressure;
- bounded database/model concurrency;
- enforceable budgets;
- SSE/citation-finalization protocol;
- minimal production backfill rehearsal;
- coverage registry.

#### Gate 3B — context-snapshot support

- context schema;
- selected/visible/working-set scope handles;
- extensional resolved scope persistence;
- context fixtures and scope grading.

Context snapshots should land before golden v2 because many real cases depend
on them. They should not block the first safe plain-chat admin pilot.

### 6.5 Gate 4

Golden v2 should curate—not copy—the question bank:

- smoke/core/extended tiers;
- acceptable evidence groups;
- expected capabilities/routes rather than brittle exact tool traces;
- context fixtures;
- sequence grading;
- held-out cases;
- real-thread weighting; and
- separate exact-scan versus semantic-analysis cases.

### 6.6 Gates 5 and 6

Keep as written:

- build provider/model seams only after the harness is trustworthy;
- treat current RRF/model choices as provisional; and
- open non-admin access only after one verified authorization context reaches
  every data-bearing path.

## 7. Concrete changes requested from the authoring agent

### `CONTEXT-SNAPSHOT-DESIGN-2026-07-30.md`

1. Make v1 scopes extensional and persist full resolved `matching_ids`.
2. Retain `filter_spec` as provenance, not the replay primitive.
3. Add `selected` to the scope enum.
4. Add context resolution status and honest unsupported-context behavior.
5. Clarify server-controlled message placement.
6. Make `scope_error` depend on explicit golden expectations.
7. Clarify that stored thread context is user-owned thread state, never
   cross-user state.

### `QUESTION-BANK.md`

1. Replace the overloaded status tag with orthogonal readiness dimensions.
2. Split `scan_text` cases from bounded semantic-analysis cases.
3. Reclassify questions whose tools/coverage paths do not exist.
4. Weight real captured threads above speculative examples.
5. Define smoke/core/extended candidate sets.
6. Preserve exact-quote, latest-eval, graceful-decline, and coverage-honesty
   implications.

### `QA-LOOP-DESIGN-2026-07-30.md`

1. Add frozen-tools, live-tools, and full replay modes.
2. Never replay from a trusted serialized ACL by default.
3. Model expected evidence as acceptable evidence groups.
4. Add runtime, context/scope, citation-coverage, and answer-contract labels.
5. Keep leg-level misses in explain detail, not top-level taxonomy.
6. State that ACL-block classification requires a restricted admin control run.
7. Make JSONB canonical with immutable JSONL export.
8. Make repeated replay conditional rather than mandatory.
9. Promote novel failure classes, not every failure verbatim.
10. Use deterministic zero-regression plus stochastic regression budgets.
11. Keep held-out cases genuinely blind.
12. Expand fingerprints beyond git SHA.
13. Revise the effort estimate or explicitly narrow v1.

### `PLAN.md` §R

1. Split Gate 1 into observability foundation, evidence correction, and loop
   completion.
2. Split Gate 3 into safe admin pilot and context support.
3. Add bundle/replay authorization to Gate 2.
4. Name exact scan and semantic scope analysis as separate future capabilities.

## 8. Final assessment

The design plan is now credible and worth executing.

The proposals do not distract from the previously agreed priorities; they make
those priorities easier to verify. The important discipline is to avoid turning
the QA system or question corpus into a new layer of premature machinery:

- capture enough to diagnose the current evidence correction;
- keep scopes exact and replayable;
- distinguish lexical coverage from semantic judgment;
- grade capabilities and answer contracts rather than exact incidental traces;
- preserve authorization at every secondary data/replay path; and
- use real questions to grow generalized tests, not entity-specific patches.

With the corrections above, the three documents become one strong, coherent
bet rather than three adjacent features.

