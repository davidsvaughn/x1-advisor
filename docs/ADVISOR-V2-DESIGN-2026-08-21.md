# X1 Advisor v2 — ground-up redesign (session-first research agent)

**Date:** 2026-08-21
**Status:** **proposed design + build plan.** Nothing here is authorized; every
build item in §8 is David-gated. Written with fresh eyes over the whole
project (code, DECISIONS, triage threads, bundles, cost ledger), the three
prior generations of the advisor, the context-engineering references in
`refs/`, the external consultations in `chats/`, and the LangChain Deep
Agents workshop.
**One-line verdict:** the problem was never the harness; it is that every
generation designed *the turn* and never designed *the session*. v2 designs
the session first — an event-sourced thread log in Postgres, typed artifacts
with handles, a thread-scoped evidence store with revalidation at citation
time, and a schema-driven thread brief instead of replayed history — and runs
that design on Deep Agents/LangGraph with the X1 domain layer kept
framework-free behind a thin adapter.

---

## 0. TL;DR

1. **Three advisors, one failure.** Mastra/TS (Mar–Apr), Deep Agents POC
   (May), Haystack (Jun–Aug). Each re-solved retrieval and the single turn;
   each then hit multi-turn amnesia / payload replay / working-set drift. The
   May plan diagnosed it and prescribed handles; the prescription was never
   built on any generation. §1.
2. **What is genuinely good and comes along:** the ACL predicate in SQL, the
   typed filter layer, the evidence registry + citation validator, the
   compact-by-construction tool discipline, `scan_text` / `analyze_scope` /
   the structured-query registry and the coverage-honesty semantics that grew
   around them, `eval_recency`, the cost ledger, the flag→triage loop, and
   the turn bundle as a forensic record. ~3.5k LOC of plain Python, none of
   it framework-bound. §2.
3. **What does not come along:** client-owned text-only history, per-turn
   evidence, 68 KB tool payloads in context, a 12-rule prompt that is a patch
   log, the byte-stable-prompt *ceremony* (pin for cache economics, not for
   comparison contracts), and most of the 8k-LOC QA harness. The ledger
   says it plainly: **$28.67 of agent spend vs ~$300 of judge/adjudication
   spend** across the project's life. §1.3, §7.
4. **v2 architecture (§3–§5):** session log → artifacts + handles →
   thread-scoped evidence with shown-this-turn citability and live-ACL
   revalidation → thread brief (structured, incremental) + last-K verbatim
   exchanges → a ~8-tool atomic surface where every heavy result returns a
   *card + handle* → agent-as-tool isolation for the two expensive
   operations → streaming answer validated before persistence.
5. **Harness (§6):** Deep Agents on LangGraph, used for what it is good at
   (thread checkpointing, streaming, interrupts, subagent isolation,
   middleware hooks, Studio, tracing) with the base prompt replaced via a
   harness profile and three custom middlewares carrying the X1 semantics.
   Gate it with a 2–3 day spike; the domain layer is written so the loop can
   be swapped in a day if the spike fails.
6. **QA (§7):** keep the bundle/ledger/flag/triage loop; replace the graded
   golden suite + calibration + adjudication with a 12-case smoke set, the
   canonical multi-turn scripts graded on *mechanics* (deterministic), and
   spot-check judging of flagged turns only. Observability: decide the trace
   schema and the single thread id now; decide the product (Opik vs
   Langfuse) in week 1 on the harness spike's own trace stream — the
   day-to-day loop exists in both, and the Opik-only half (shipped agent
   metrics, thread-level rules, user simulation, optimizer) is precisely the
   machinery we should stop building by hand, adopted under the
   mechanics-first discipline.
7. **Retrieval (§4.6):** untouched by the session design — locators cross
   turns, vectors do not. One cheap independent experiment queued: a
   `halfvec(1024)` index config that cuts the vector footprint ~3× on the
   0.6 GB db-f1-micro and may re-open HNSW.
8. **Plan (§8):** ~3 weeks to parity-plus on the six-step multi-turn protocol,
   with the harness spike at day 3, the observability go/no-go in parallel,
   and the QA-lite loop from day 1.

---

## 1. Retrospective — what three generations taught us

### 1.1 The same failure followed the project across harnesses

| Generation | Stack | What it solved | Where it died |
|---|---|---|---|
| x1-link advisor (Mar–Apr 2026) | TypeScript, Mastra, CopilotKit, page-capability layer | Page context, SQL routing, copilot actions | Copilot direction abandoned; "what belongs in advisor-owned working memory vs page state" never answered |
| Deep Agents POC (**May 5–9, 2026 — five days, ~60 commits**, branch `spike/python-deepagents-copilotkit-search`) | Python, `deepagents 0.5.7`, LangGraph subgraphs (SQL, document-evidence), `zap` byte-range search (Rust), `.artifacts/` files | Schema-card SQL routing, workflow-behind-one-tool, coverage as a first-class output, `idToName`+`samplePolicy` working-set context | **`create_deep_agent` was called with no checkpointer** — `configurable.thread_id` was set and inert; the client replayed full history every turn; `StateBackend` was abandoned for a local-disk shim; the base prompt was string-truncated by a middleware; no citation validation, no ACL, no cost ledger, streaming was one `text-delta` after a synchronous turn. "Medium-sized tool payloads too small to trigger offloading", "follow-up turns reconstruct the active startup set from prose memory" ([May plan](refs/2026-05-08-ADVISOR-CONTEXT-ENGINEERING-PLAN.md) §"Observed failure modes"). The spike's own must-prove list (`ALTERNATIVE-STACK-EVALUATION.md` §11) had thread reload, streaming, and filesystem-fits-retrieval fail — 3 of 7. **No decision doc records the drop**; the branch simply stops on May 9 with the plan's steps 4–11 unbuilt |
| Haystack advisor (Jun 12–Aug 2026, this repo) | Python, Haystack `Agent`, pgvector hybrid retrieval, evidence registry, QA harness | Citation validity, ACL in SQL, census tools, cost discipline | Thread-037 amnesia: turn 88 re-ran a $0.086/47 s census 30 s after turn 86 produced it, because history is client-supplied prose and evidence is per-turn ([HARNESS-STRATEGY-ASSESSMENT](HARNESS-STRATEGY-ASSESSMENT-2026-08-17.md) §1.1) |

The May plan's remedy — *inline content is wrong by default; handles are
right by default; tools trade a handle for content on demand* — was written
for generation 2, not implemented there, and not carried into generation 3.
Generation 3 instead made the opposite trade on purpose: text-only history
for context discipline, per-turn registry for citation validity
([EVIDENCE-RECALL-DESIGN](EVIDENCE-RECALL-DESIGN-2026-08-15.md) §"The gap").
Both choices were individually defensible; together they guarantee that a
follow-up must re-earn all evidence.

**The lesson is structural, not a bug:** nobody ever owned the *session*.
`AskRequest` carries a browser-maintained `history` array
(`x1_advisor/service.py:60-63`); `run_turn` builds a fresh
`EvidenceRegistry()` per call (`x1_advisor/agent/advisor.py:243`); the rich
`research_record` is persisted every turn (`advisor.py:406-422`) and read
only by QA. The system has excellent memory that the live agent cannot
touch.

### 1.2 What the bundles and ledger actually show

Measured on the live-thread bundles and `cost_ledger.jsonl`:

- **Ordinary turns are cheap and fast**: $0.002–0.02, 3–8 s, ~4k-token
  cached prefix, uncached input in single digits. The prompt-cache strategy
  works. Single-turn efficiency does not motivate a rewrite.
- **The expensive class is evidence re-earning** (turn 88 class), and the
  mechanism is worse than it looks: the 68,907-char `analyze_scope` result
  in turn 88 was billed as **13,522 cache-write tokens at 1.25×** — $0.033 of
  the $0.041 final step — and then never read again (turn ended next step).
  The per-step usage table (`advisor.py:277-284`, `ask.py:60-64`) shows
  input/cached/output and **omits `cache_write_tokens`**, so the
  "context-bloat instrument" is blind to exactly this case. (Ledger rows:
  `turn:1786751481`, `turn:1786751603`.)
- **Context per turn is small except for one payload.** Turn 88's context:
  7.6k-char system prompt, 96+651+50 chars of prior prose, one 68.9k-char
  tool result, 2k-char answer. The model needed ~2k tokens of findings and a
  handle; it got 17k tokens of JSON.
- **Where the money went:** $336.57 lifetime ledger spend; `agent.step`
  $28.67; `judge.cc` $160.43 (API-equivalent on the Max seat); the
  `adjudicate.*` family $135; everything else noise. **The evaluation
  machinery outspent the product ~10:1.** (Seat-billed judge calls are not
  real dollars, but the ratio is the signal.)
- **Where the time went:** 210 commits; 183 of them in weeks 31–33
  (Jul 27–Aug 16), the Gate-1/Gate-4 QA-machinery arc — five "QA-machinery
  correctness passes" in one day (PLAN §R 1D/1E), baseline never accepted,
  the QA trio "owed" for weeks while prompt changes queued behind
  "comparison cycles" (DECISIONS 2026-08-14 ×4 all end with "batched into
  the pending comparison cycle").

### 1.3 The mistakes, named (so v2 does not repeat them)

1. **Designing the turn, not the session.** Client-owned history; text-only
   carry; per-turn registry; bundles as QA-only. (All three generations.)
2. **Payloads inline.** Tool results stay in context for the rest of the
   turn at full size; the discipline was caps-on-snippets, not
   cards-with-handles. 68 KB census results paid at the cache-write premium.
3. **Prompt as patch log.** `SYSTEM_PROMPT` (`advisor.py:84-193`) carries 12
   numbered rules accreted from triage — rules 5/6/7 alone are ~400 words
   of census/honesty doctrine. Several encode *tool* semantics that belong
   in tool contracts and result schemas (the design already moved counts to
   `coverage.*` fields; the prose about counts stayed).
4. **The comparability contract as a tax on iteration.** `SYSTEM_PROMPT_SHA256`
   / `TOOL_SCHEMA_SHA256` pins, "scoring-contract severs", judge
   calibration (32 human labels, "synthetic-only" states), four adjudication
   dimensions, replicate medians, baseline acceptance ceremony. Each of
   these is defensible; the sum made a one-sentence tool-description edit a
   multi-hour event. Cache stability needs a *test that the prefix is
   byte-stable across calls*, not a test that it never changes.
5. **QA leaking into the product path.** The exact corpus watermark
   recomputed inside a user turn (20.5 s of a 30 s turn; DECISIONS
   2026-08-14 (4)); a synchronous Langfuse flush (1.5 s). Both fixed, both
   symptoms of measurement concerns owning runtime code.
6. **A single 8-step loop for everything.** No planning, no delegation, no
   budget negotiation; "deep" work was pushed into tools
   (`analyze_scope`, `web_research`) — which is actually the right pattern
   (Manus: most sub-agents are agent-as-tool) but was never named as such,
   so it could not be generalized.
7. **Working sets reconstructed from prose** ("just show the ones with score
   over 77" → "which of these…"). The CONTEXT-SNAPSHOT design was adopted
   and not built; the May plan's `idToName` working-set contract was written
   and not built. The most common phrasing in the question bank (§3.1) still
   has no representation.
8. **Harness-swap as reflex.** Each pivot changed the loop runner and kept
   the turn-shaped design. The harness assessment's coupling audit is the
   proof: Haystack is ~35 lines. Swapping it again, *without* changing the
   session design, would produce a fourth amnesiac advisor.

### 1.4 What genuinely worked (and why)

- **ACL is one SQL predicate** (`retrieval.py:65-99`), imported by scan
  (`scan.py:38`) and analyze (`analyze.py:32`); structured queries mirror it
  (`queries.py:37-61,234-255`). Default-open cross-user research, class
  gates only, never identity walls. Right model, right layer.
- **Evidence registry + validator** (`evidence.py`): three evidence kinds
  (chunk / web / query-as-platform-data), per-ref snapshots of what the
  model saw, drop-and-renumber at answer time. Unresolvable citations cannot
  survive; exact database answers carry provenance.
- **Typed filter layer** (`filters.py`): registry of fields, allowlisted
  operators, canonical-value resolution with "did you mean" notes, static
  enum contract. Closed an injection→ACL-bypass class.
- **Tools compact by construction, caps visible, truncation flagged**
  (`tools.py` header). `search_corpus` snippets + `get_source` escalation;
  `scan_text` exact counts/names always, excerpt bodies capped and the cap
  disclosed; `list_labels` `label_total`/`match_total` after the thread-022
  silent-truncation bug.
- **The census pair.** `scan_text` (deterministic, shared engine with the
  truth builder) and `analyze_scope` (embedding-ranked frontier, per-eval
  read cap, canonical-read policy, counts from coverage never from
  synthesis). Semantics that took weeks of live triage to get right:
  lexical no-match ≠ semantic negative; `restricted` ≠ `no_match` ≠
  `not_indexed`; `eval_recency='current'` as the structural default.
- **Structured-query registry + label resolver** — the only SQL surface;
  reusable across vocabularies; rows self-describing.
- **Cost ledger** — every call, every provider, raise on unknown model,
  cache read/write priced correctly per transport.
- **The flag → triage-doc loop** (`flags.py`, `docs/triage/`): David flags a
  live turn, the triage doc records diagnosis + fix + status. This found
  real platform-data bugs (LinkedIn-id industry mis-mapping, 629 "Unknown
  company" titles) that no golden suite would have. It is the QA loop that
  earned its keep.
- **The turn bundle** (`bundle.py`) — everything the model saw, verbatim,
  plus evidence identities and retrieval explain. In v2 it stops being an
  export and becomes the session log itself.
- **Platform reference as uncitable background** (`platform_reference` tool)
  — structural uncitability beats instructed uncitability.

### 1.5 From the previous generation, worth carrying (ideas, not code)

The May POC (`~/code/x1/deepagents-poc/x1-link/services/x1-deep-advisor-python`)
was largely agent-written in five days and never wired its own persistence,
but four of its ideas are better than what gen 3 has:

- **Citations as coordinates.** `zap` returns byte-window spans whose
  search-output fields equal its expand-input fields (`zap/SPEC.md`): a
  citation is `path:start:end`, re-cut identically forever. Gen 3's
  `(document_id, block_index)` is the same idea at block granularity; v2
  keeps it and makes every artifact locator the same shape.
- **Workflow behind one tool.** `answer_document_question_tool` hid a whole
  LangGraph behind one stable parent tool with a compact typed return —
  the pattern that actually contained context growth. v2 names it
  (agent-as-tool) and applies it to `census` and `web`.
- **Coverage as output, not prose.** `entityCoverage[] ∈ {found,
  no_evidence, no_searchable_documents}` + `coverageComplete`
  (`document_workflow/graph.py:597-640`) — independently rediscovered by gen
  3 as "counts from coverage, never from synthesis". v2 makes it a card
  field on every scope-taking tool.
- **`idToName` + `samplePolicy` + `readableDataManifest`**
  (`context.py:56-105`): send shapes not values; send a bounded id→name
  map labelled as a sample so the model cannot extrapolate. This is the
  working-set card of §4.2.
- **The must-prove checklist before porting tools**
  (`ALTERNATIVE-STACK-EVALUATION.md` §11): thread reload, context
  propagation, tool-result ballooning, streaming, observability,
  filesystem-fits-retrieval. Last time it was written and not executed;
  §6.4 executes it first.

---

## 2. What comes along, what is rewritten, what is dropped

| Component | Disposition | Notes |
|---|---|---|
| `retrieval.py` (hybrid + RRF + summary expansion, ACL predicate) | **keep as-is** | Plain SQL; index-served dense leg |
| `scan.py`, `agent/analyze.py`, `agent/queries.py`, `filters.py` | **keep; re-wrap** | Tool *implementations* unchanged; tool *contracts* change to card+handle (§5) |
| `agent/evidence.py` | **keep; extend** | Thread-scoped persistence, stable refs across turns, revalidation query (§4.3) |
| `cost.py` | **keep** | Add a LangChain callback adapter; per-step table shows cache-write |
| `ingest/*`, `index.py`, `schema.sql` corpus tables | **keep** | Add session/artifact/evidence tables (§4.1) |
| `platform_reference.md` + tool | **keep** | Candidate for a Deep Agents *skill* (on-demand) |
| `fingerprint.py` | **keep, slim** | Code sha + prompt/tool digest + corpus watermark as *metadata*; no pins in tests; watermark never inline in a turn |
| `agent/bundle.py` | **replace** | The session log (§4.1) is the bundle; a projection produces the old shape for replay if wanted |
| `agent/advisor.py` (loop, history, prompt) | **rewrite** | §5, §6 |
| `service.py` | **rewrite, keep API shape** | `/ask`, `/ask/stream` SSE contract stays; `history` field removed; `thread_id` + optional `context` |
| Dev console | **keep for now** | Point it at the new API; LangGraph Studio for debugging |
| `agent/replay.py` | **keep `full`; drop `frozen`/`live`** | Replay = re-run a thread's user turns against today's code |
| `experiments/` (7,986 LOC) | **mostly drop** | §7 keeps ~4 pieces |
| `SYSTEM_PROMPT` 12 rules | **distill** | Product semantics kept (coverage honesty, vintage disclosure, cite-through, decline actions, style); tool mechanics move into tool contracts; target ≤ ~1,200 tokens (from ~1,900) |

---

## 3. Design principles for v2

Derived from §1, in priority order. Each names the failure it prevents.

1. **The session is the unit of design.** The server owns the thread; the
   thread is an append-only event log in Postgres; the model's context is a
   *view* assembled from the log per call, never the log itself.
   *(Prevents 1.3-1.)*
2. **Handles, not payloads.** Every tool result is a **card** (small,
   schema-bounded, what the model needs to reason and to cite) plus a
   **handle** to the full artifact. Cards are what enter context; artifacts
   live in Postgres; `recall(handle, selection)` trades a handle for content.
   *(Prevents 1.3-2.)*
3. **Evidence is thread-scoped; citability is shown-this-turn; validity is
   checked live.** A chunk keeps the same ref for the life of a thread (so
   the model's memory and the validator agree); a ref is citable only if it
   was shown in the current turn (fresh tool call or recall card); every
   cited chunk is re-resolved under the live principal and corpus at
   validation time. *(Keeps 1.4's citation guarantee while killing
   re-earning.)*
4. **Working sets are first-class.** An entity set produced by a tool or by
   page context is an artifact (`ws:n`) with a name, a derivation, a count,
   and members; every scope-taking tool accepts a working-set handle.
   *(Prevents 1.3-7.)*
5. **Compaction is schema-driven and incremental.** A **thread brief** —
   working sets, artifacts, established facts, open items, preferences — is
   maintained by code + a cheap model from each turn's events, not by
   summarizing the transcript. Older verbatim turns drop out behind it.
   Generic "summarize at 85%" is a safety net, not the design.
   *(Manus §5: schemas as contracts; Deep Agents summarization only as
   backstop.)*
6. **ACL in SQL, once.** `_acl_sql` stays the single predicate; every
   evidence path and every recall goes through it; the principal comes from
   the request context and propagates to subagents; nothing the model says
   can widen it.
7. **Compact by construction; caps visible; never silent.** Unchanged from
   gen 3 — it is the part that worked.
8. **Coverage and counts are data.** Counts come from `coverage.*` /
   `*_total` fields computed in code; synthesis never produces a number the
   tool did not. (Already true for `analyze_scope`; make it true everywhere
   and stop re-stating it in prose.)
9. **Isolate the expensive work as agent-as-tool with a fixed output
   schema.** `analyze_scope` and `web_research` already are; name the
   pattern and use it for any future "go deep" mode. Parent context stays
   clean; the child returns cards + handles.
10. **Model-agnostic; cache-stable prefix as an economic property.** Stable
    system prompt and tool schemas within a deployment because cache reads
    are 10× cheaper, verified by a runtime assertion (same prefix bytes
    across consecutive calls) — not by committed hashes that block edits.
11. **Measure what the user feels.** Per-turn cost, latency, first-token
    time, follow-up-to-first-turn cost ratio, citations resolved/dropped,
    flagged turns. Judge quality by spot checks on flagged turns.
    *(Prevents 1.3-4/5.)*
12. **Build less, understand more.** Every layer must name the failure it
    prevents; if it cannot, it does not ship. (Manus §19.)

---

## 4. The session substrate (framework-independent, Postgres)

### 4.1 Tables (all in the `advisor` schema; sketch)

```sql
-- one row per conversation; the server mints ids, the client never sends history
advisor.threads(id, user_id, title, created_at, brief jsonb, brief_version int)

-- append-only event log: the canonical record of everything that happened
advisor.events(
  id bigserial, thread_id, turn_id, seq int,
  kind text,          -- user_message | assistant_message | tool_call | tool_result
                      -- | artifact | compaction | context_snapshot | error
  payload jsonb,      -- full, never truncated (tool_result payload = the CARD the model saw)
  artifact_id bigint, -- when a tool result produced one
  usage jsonb,        -- per model call: input/cache_read/cache_write/output, model, cost
  created_at)

-- typed heavy results, addressable by handle
advisor.artifacts(
  id bigserial, thread_id, turn_id,
  handle text,        -- 'art:12', 'ws:3', 'ans:7' — unique per thread, model-facing
  kind text,          -- census | scan | query_result | search_set | web_findings
                      -- | working_set | answer | note
  card jsonb,         -- the compact model-facing summary (what entered context)
  payload jsonb,      -- the full result (findings, rows, members…), no chunk bodies
  locators jsonb,     -- evidence locators: [(document_id, block_index)], urls, (query, params)
  principal_hash text, corpus_watermark jsonb, created_at)

-- thread-scoped evidence registry (replaces the per-turn in-memory one)
advisor.evidence(
  thread_id, ref text,            -- 'r14' stable for the thread
  kind text, document_id, block_index, page_number, url, query_name, query_params jsonb,
  snapshot text,                  -- longest view the model has had of it
  first_turn int, last_shown_turn int,
  PRIMARY KEY (thread_id, ref))
```

`advisor.turns` survives as a view (or thin table) for the console and API:
`(thread_id, turn_id, question, answer, citations, cost_usd, created_at)`.

Everything QA ever read from `research_record` is derivable from `events` +
`artifacts` + `evidence` for a `(thread_id, turn_id)`; the bundle export
becomes a projection function, not a second copy.

### 4.2 Artifacts and cards

A tool implementation returns a Python result; the **artifact layer** (one
module, ~200 lines) decides what the model sees:

- **Card** — always. Schema per kind, bounded by construction (not by
  character clipping). Examples:
  - `census` card: `{handle, question, scope, coverage{…}, top_findings:[≤8 × {entity, finding ≤240ch, refs}], more: n, reduction_not_citable ≤600ch}`
  - `scan` card: `{handle, scope, counts, matched:[names…] (complete), excerpts:[≤12], more_entities_with_excerpts: n}`
  - `query_result` card: `{handle, query, params, row_count, total, rows:[≤20]}`
  - `working_set` card: `{handle, name, derivation, count, members:[names ≤30], more: n}`
  - `answer` card (the assistant's own prior answers, for the brief): `{handle, turn, question, lede ≤300ch, citations: n, artifacts_used: [handles]}`
- **Payload** — the full structured result, persisted. Never chunk bodies:
  locators only. (This is what keeps artifacts ACL-safe to store and cheap
  to revalidate.)
- **Handle** — `kind:n` per thread. Tools accept handles wherever they
  accept scope (`scope: {working_set: "ws:3"}`, `recall("art:12", …)`).

`recall(handle, select)` is one generic tool (replaces the narrow
`recall_evidence`): `select` is a small grammar — `"all"`, `"top k"`,
`{"entities": [...]}`, `{"query": "free text"}` (ranks findings by embedding
or FTS within the artifact). It returns a fresh card whose evidence refs were
**re-fetched under the live ACL** (one `IN` query over locators), registers
them as shown-this-turn, and reports `dropped_stale`.

### 4.3 Evidence, refs, citability

- Registration is idempotent per thread: `(chunk, doc, block)` → the same
  `rNN` for the thread's life (`EvidenceRegistry.register_chunk` already
  dedupes by key; it just needs the thread as its scope and a table behind it).
- **Shown-this-turn** is tracked per turn (`last_shown_turn`). The validator
  accepts `[rNN]` only if `last_shown_turn == current_turn`; otherwise the
  ref is dropped *and the drop is reported in the card the model sees on the
  next step* (so the model learns to `recall` rather than cite from memory).
- **Revalidation** at validation time: all cited chunk refs → one SQL
  `SELECT … WHERE (document_id, block_index) IN (…) AND superseded_by IS NULL
  <acl_sql>`. Missing → dropped with reason `stale|revoked`. Web refs are
  not revalidated (not reproducible — same as today). Query refs carry
  `as_of`.
- `acl_resolved` in bundles stays forensic-only; replay re-resolves the
  replaying principal (keep `replay.py`'s rule).

### 4.4 The thread brief (compaction as a schema)

Maintained per thread, regenerated at the end of each turn by code where
possible and by a cheap model (`luna`) for the prose fields, from **that
turn's events only** (incremental, never from the whole transcript):

```json
{
  "working_sets": [{"handle":"ws:3","name":"startups with score>77","count":14,"turn":2}],
  "artifacts":    [{"handle":"art:7","kind":"census","one_line":"brand/market weaknesses — 45 evals, 31 relevant","turn":3}],
  "established":  [{"text":"31/45 current evaluations flag brand or market-positioning weaknesses","refs":["art:7"]}],
  "open":         ["user asked for healthcare-only restriction next"],
  "preferences":  ["tables for per-entity comparisons"],
  "entities_in_play": ["Angiex","BMI OrganBank","Orphagen"]
}
```

Size target ≤ 800 tokens; rendered as one user-role message after the
system prompt. Bounded by schema (list caps with `more: n`), so it cannot
grow into the thing it replaces. `established` items carry artifact/answer
handles, so "why do you think that?" is a `recall`, not a re-run.

### 4.5 Context assembly per model call (the view)

```
[system]  stable prompt (~1.2k tok) — identical bytes across calls & turns
[user]    thread brief (≤800 tok; omitted on turn 1)
[user/assistant] last K=2 exchanges verbatim — user text + assistant final text only
          (no tool calls, no tool results, no reasoning from prior turns)
[user]    page-context snapshot if present (typed refs → registered as ws:n)
[user]    the current question
…         this turn's tool calls + CARDS (never raw payloads)
```

Consequences: prefix (system + tools) is cached; the brief changes once per
turn (cheap); the turn's own cards are bounded; nothing from a prior turn is
replayed. Deep Agents' offloading (>20k tokens) and summarization (85%)
remain enabled as backstops and should essentially never fire — firing is a
metric to alarm on, not a feature to rely on.

### 4.6 Where retrieval touches the session (and where it does not)

The RAG stack is the part of gen 3 with numbers behind it (golden-v1
recall@10 0.757 / MRR 0.581 post-restore; warm dense leg 0.44 s end-to-end
including the embed call; the largest quality lever ever measured was record
summaries, +0.055 — not the index type). v2 keeps `retrieval.py` as-is and
the session design is deliberately orthogonal to it. The touchpoints,
explicitly:

- **Locators, not vectors, cross turns.** Artifacts store
  `(document_id, block_index)`; `recall` revalidation is one SQL `IN` query
  under `_acl_sql`. No embedding call on the follow-up path — that is the
  whole mechanism behind "$0.086 → ~$0.01".
- **Corpus versioning.** Version-and-append (`documents.superseded_by`)
  means a locator can go stale between turns. `dropped_stale` on the recall
  card plus the `corpus_watermark` stamped on every artifact is how the
  agent says "the corpus moved since turn 3" instead of silently re-citing.
- **`census`'s embedding prefilter** (`analyze.rank_by_embedding`) is an
  exact aggregate over the scope's documents — scope-bounded, never
  corpus-bounded. Unchanged.
- **`recall(select={"query": …})`** ranks findings *inside* one artifact:
  FTS/substring over the findings text first; embed on the fly only if that
  proves inadequate (tens of items, pennies).
- **Record summaries** stay retrieval-only and non-citable; the thread brief
  and working sets never touch vectors.

**The footprint question is real, the ANN-kind question is not.** One index
config exists (`te3s_1536_ck1`: `text-embedding-3-small`, 1536 dims, stored
`vector(1536)` float32, `index.py:70,84`). ~50k vectors × 1536 × 4 B ≈
**307 MB of vectors plus an index of similar size on a db-f1-micro with
0.6 GB RAM** — the reason HNSW could not build (`index.py:34-38`: needs
~400 MB `maintenance_work_mem` at this scale), the reason cold scans were
39 s before the dense-leg rewrite, and the reason ivfflat was the right
pragmatic call. At 50k vectors ivfflat-vs-HNSW is a marginal recall/latency
trade; the memory footprint is not marginal.

**E1b — a retrieval-only experiment, independent of v2 (~1 h, ~$0.50):** a
second `index_configs` row — the registry exists for exactly this, one
`emb_{config}` table per config, graded on golden-v1 retrieval metrics with
no agent and no judge — using `halfvec(1024)`: `text-embedding-3-small`
called with the API's `dimensions=1024` (Matryoshka truncation) and
pgvector's half-precision type (0.8.1 on test; `halfvec` since 0.7). ≈100 MB
instead of ≈307 MB: a 3× cut that may make HNSW buildable in memory on the
same instance and, separately, measures whether 1024 dims costs recall. If
recall holds, flip it `active` and record E1b in DECISIONS; if not, an hour
was spent. E1 (embedding *model* swap) stays deferred as pinned.

**E2b — re-run the reranker on the right metric (~1 h, retrieval-only).**
Search is two-stage by design: stage 1 is recall-oriented and content-blind
(dense top-50 + FTS top-50 fused by RRF — a rank-only formula; each chunk's
vector was computed without knowing the question, so it encodes *topic*,
not *answer-fit*); stage 2, built and **off**, is `jina-reranker-v3` — a
0.6B listwise model that reads the query together with the fused top-40
(each clipped to 4,096 chars) and scores them jointly, blended
0.3·rrf + 0.7·rerank, then re-sorted before dedup / per-doc cap / top-8
(`retrieval.py:196-237, 334`; `search_corpus` never passes `rerank=True`,
`tools.py:104-106`; only `experiments/run.py --rerank` exercises it). Cost
≈ $0.001 per search (token-billed, `cost.py:117` still "verify"); latency
one extra round trip, ~300–500 ms on a 0.44 s leg. E2 (DECISIONS
2026-07-08) called it a wash — recall@10 0.778 → 0.792, MRR 0.727 → 0.718,
the same 5 zero-recall questions either way — but two things make that
less conclusive than it reads: (1) **recall@10 is the wrong k** — a
reranker reorders *within* the set, and what the agent sees is the top
**8 snippets** that enter context, i.e. precision at small k, the direct
input to the `synthesis_error` / `citation_coverage_error` classes the
funnel found dominant; (2) **the corpus has tripled since** (~29k → ~50k
vectors), and E4b measured recall falling 0.833 → 0.757 from distractor
pressure alone — the regime where reranking starts to pay. One placement
quirk to test alongside: rerank currently runs *before* summary expansion
(`retrieval.py:334` vs `:359`), so it scores generated record summaries
that are then swapped for source blocks; an after-expansion arm would let
it pick the best block per routed document. Protocol: `run.py --rerank` on
today's corpus, paired manifests, scored on **precision@8 + MRR** (not
recall@10), two arms (before/after expansion). If it moves, enable for
`search` only — `scan` and `census` read everything in scope and never
rank; the session substrate is untouched. (`jina-reranker-v3.5` exists,
same API, if the v3 row is retired.)

E5 (lexical-leg query preprocessing — the leg is silent on most golden
questions) stays the third open retrieval-quality item. All three are
bake-offs the v2 run record supports; nothing in v2 depends on them.

---

## 5. Tool surface (≈8 tools, atomic, stable schemas)

Every tool returns `{card…, handle}`; every scope-taking tool accepts
`working_set` handles; errors are actionable (valid vocabularies echoed),
as today.

| Tool | Implementation | Card | Notes |
|---|---|---|---|
| `search` | `retrieval.retrieve` + summary expansion | ≤8 snippets + refs, `filter_notes`, `access_note` | unchanged semantics; result set becomes `art:n` (search_set) so "show me more from that search" is a recall |
| `scan` | `scan.scan` | counts + complete name lists + capped excerpts | `scan_text` renamed; accepts `working_set` |
| `census` | `analyze.analyze` | coverage + top findings + handle | `analyze_scope` renamed; per-finding refs; reduction uncitable; eval_recency default `current` |
| `query` | `queries.run_query` | rows ≤20 + totals | registry unchanged; result → `art:n` |
| `source` | `get_source` | one block, upgraded snapshot | unchanged |
| `recall` | artifact layer | kind-specific card, revalidated refs, `dropped_stale` | the multi-turn primitive |
| `web` | OpenAI Responses web_search (or provider seam) | findings ≤1.6k ch + sources | unchanged; agent-as-tool |
| `platform_reference` | static doc | the doc | candidate for a skill; uncitable by construction |
| *(later)* `resolve_set` | SQL over app tables | `ws:n` card | "the ones with score>77", "these 12 healthcare startups" → explicit working set; page context arrives the same way |

What is deliberately **not** on the surface: free-form SQL, file-system
tools for the main agent (see §6.3), to-do lists (Manus measured a third of
actions as to-do updates; add a planner only on measured need), any action
tools (research agent, not actor — scope decision stands).

**Prompt distillation.** The system prompt keeps: role; the one-paragraph
tool doctrine (sampler vs census vs meaning vs exact counts); citation rules
1–4 and 8–9 as they are; coverage honesty as *three* sentences (only
tool-computed counts may be stated as exact; a lexical no-match is not a
semantic negative; disclose scope, vintage, and caps); cite-through (rule
10) and platform-reference (rule 11) as one sentence each; style as a short
list. Tool-specific doctrine (what `terms_fired` means, how to credit
variants, what `eval_recency` narrows) lives in tool descriptions and card
field names. Target: system prompt ≤ ~1,200 tokens (today: 7,581 chars ≈
1,900 tokens); total cached prefix ≈ 4–5k tokens including tool schemas —
the same order as today's ~4k, with the tokens spent on contracts instead
of prose.

---

## 6. Harness: Deep Agents / LangGraph, used deliberately

Verified against the only `deepagents` install on this machine — **0.5.7**
in the May POC's venv (`langchain 1.2.17`, `langgraph 1.1.10`); the
workshop repo's lock pins 0.5.3 and was never synced. Anchors below are
into that tree (`SP = …/x1-deep-advisor-python/.venv/lib/python3.14/site-packages`).

### 6.1 Why a harness at all this time, and why this one

The harness assessment was right that Haystack was 35 lines and that
"replatform" was a misnomer. The question for v2 is different: §4 *is* a
session substrate, and someone has to own thread checkpointing, resume,
streaming, interrupts, subagent isolation, and the per-call hook points
where X1 semantics plug in. Options:

| Option | Owns the loop + session mechanics | X1 must add | Verdict |
|---|---|---|---|
| **Deep Agents on LangGraph** | thread checkpointer (Postgres), streaming, HITL interrupts, `task` subagents, middleware hooks, offload/summarize backstops, skills/AGENTS.md, harness profiles, Studio, LangSmith/Opik callbacks | artifact middleware, brief middleware, cost callback, evidence/ACL revalidation (domain layer) | **recommended** — the mechanics are exactly the ones §4 needs; model-agnostic; the May-plan pain points (base-prompt baggage, sub-threshold payloads) are now addressable by profile + our own card discipline |
| Hand-rolled loop on the Responses API | nothing — we write checkpoint/resume, streaming, tool dispatch (~600–900 LOC) | everything above, plus the loop | viable fallback; total control; no churn risk; loses subagent/interrupt/Studio ergonomics |
| Claude Agent SDK | full harness, sessions, compaction | MCP wrapping of tools; Claude-only | no — violates the model-agnostic directive; terra is the measured winner; fine as a later *deep-mode* experiment |
| DeepSeek Harness | event-sourced sessions, recall over shadowed events | TS boundary, auth, Postgres persistence, pre-release churn | no (chat1's analysis stands); **steal the architecture** — §4 already does |
| Haystack 3 State | structured state within a run | everything cross-turn | no — helps within a run, not across turns |

### 6.2 The shape

```
FastAPI  /ask  /ask/stream  /threads…           ← same contract; server-owned threads
   │ principal (ACL dict) + thread_id + question + optional page context
   ▼
create_deep_agent(model=terra via Responses, tools=X1 tools, profile=x1,
                  middleware=[ArtifactMiddleware, ThreadBriefMiddleware, CostMiddleware],
                  checkpointer=PostgresSaver, backend=StateBackend (offload backstop),
                  subagents=[] initially; context_schema=Principal)
   │
   ├── LangGraph owns: loop, checkpoint per thread_id, streaming, interrupts, task
   └── X1 domain layer (plain Python, framework-free):
         tools/*.py (retrieval, scan, analyze, queries, web)   ← unchanged implementations
         artifacts.py (cards, handles, recall, revalidation)
         evidence.py (thread-scoped registry, validator)
         brief.py (schema, incremental update)
         session.py (events/artifacts/evidence tables, projections)
         cost.py, fingerprint.py (slim), acl (retrieval._acl_sql)
```

- **`ArtifactMiddleware`** (`wrap_tool_call`, `types.py:649`): runs the
  handler, persists the full result as an artifact row + event, and returns
  a `ToolMessage` whose **content is the card** and whose
  **`.artifact` sidecar carries the handle + locators** (model-invisible;
  survives eviction — `filesystem.py:1830`). This is exactly what
  `FilesystemMiddleware.wrap_tool_call` itself does (`filesystem.py:2117-2136`).
  Ordering works in our favour: custom middleware sits at position 8,
  *inside* `FilesystemMiddleware` (position 3), so our card is what the
  80k-char eviction check sees — it never fires. Tools stay pure functions.
- **`ThreadBriefMiddleware`** (`wrap_model_call`): `request.override(messages=
  [brief] + last_K_exchanges + this_turn)` — a view only; `state["messages"]`
  is untouched. This is the framework's central design property and
  deepagents relies on it three times itself (shadow compaction at
  `summarization.py:906-1008`, human-message eviction at
  `filesystem.py:1939-1978`, `_truncate_args`). After the turn the brief is
  updated from the turn's events (service layer, or an `after_agent` hook).
- **`CostMiddleware`** (`wrap_model_call`): usage incl. cache-write →
  `cost.py`; per-step table; soft cap. (A LangChain callback works too;
  the middleware keeps it in one place.)
- **Finalization** happens in the service layer, not a middleware: stream raw
  text; on completion run `validate_citations` (shown-this-turn +
  revalidation); persist `assistant_message` event + `answer` artifact; emit
  the final SSE event with validated answer + citations. Same semantics as
  today's `/ask/stream`.
- **Principal propagation:** `context_schema=Principal`; tools read
  `runtime.context`; it propagates to subagents automatically; nothing
  model-controlled touches it.
- **Model:** pass a **pre-built** model object. The `openai:*` string path
  registers `use_responses_api=True` with server-side retention on
  (`profiles/provider/_openai.py:19-23`); for a private corpus build it
  explicitly — `init_chat_model("openai:gpt-5.6-terra", use_responses_api=True,
  store=False, include=["reasoning.encrypted_content"])`
  (documented workaround at `graph.py:247-257`).

### 6.3 What we turn off, replace, or watch (0.5.7 mechanics)

The default stack (`graph.py:628-681`, first = outermost): Todo → Skills →
Filesystem → SubAgent → Summarization → PatchToolCalls → *ours* → profile
extras → ToolExclusion → AnthropicPromptCaching → Memory → HITL. What that
means for a research agent:

- **Base system prompt** (`BASE_AGENT_PROMPT`, 566 tokens, coding-flavoured,
  `graph.py:56-97`): `system_prompt=` only *prepends*; replace it via
  `register_harness_profile("openai:gpt-5.6-terra", HarnessProfile(
  base_system_prompt=X1_PROMPT, excluded_middleware={"TodoListMiddleware"},
  excluded_tools={"write_file","edit_file","glob","ls"},
  general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))`
  (`harness_profiles.py:256-309`). The POC did this with a string-truncating
  middleware; the profile is the sanctioned path.
- **Prompt budget.** Without the profile, middleware appends ~8–10k tokens
  of coding-agent prompt: `write_todos` prompt 268 + tool description 913;
  `task` prompt 535 + tool description **1,643**; filesystem 295; memory
  1,116; skills 427. With todos excluded, **no `subagents=`** (then
  `SubAgentMiddleware` and the `task` tool are not added at all,
  `graph.py:640`), no `memory=`, and the base prompt replaced, the residual
  is the filesystem prompt (~295 tokens; `FilesystemMiddleware` cannot be
  excluded, `graph.py:173-188`) plus whatever skills we declare. Target
  prefix: ~1.2k (ours) + ~0.3k + ~3k tool schemas ≈ 4.5k tokens — the same
  order as today's 4k cached prefix.
- **Filesystem tools**: keep `read_file` and `grep` (read-only); exclude the
  rest. Rationale: the summarization backstop writes the offloaded
  transcript to `/conversation_history/{thread_id}.md` and tells the model
  to `read_file` it back — the backstop is useless without a reader. Our
  artifacts never go through the file system (cards are small; payloads
  hold locators, not chunk bodies), so no ACL-blind read path exists.
- **Backend**: `StateBackend` default is enough at launch (files live in the
  checkpoint). If the eviction/offload paths ever matter, route them to
  Postgres rather than disk: `CompositeBackend(default=StateBackend(),
  routes={"/large_tool_results/": PostgresBackend, "/conversation_history/":
  PostgresBackend})` — eviction targets are *paths* resolved through the
  backend (`filesystem.py:1811`, `summarization.py:317`). A backend is six
  sync methods (`protocol.py:318-624`; async comes free via `to_thread`);
  the third-party `deepagents-backends` package already ships a
  `PostgresBackend` aligned to 0.5.2 (`~/code/x1/deepagents-backends`, MIT).
- **Eviction** fires at 20k tokens ≈ **80k chars** on a tool result
  (`filesystem.py:195,701,1806`), is hardcoded at the `create_deep_agent`
  call site, and **is destructive to the checkpoint** (the replaced
  `ToolMessage` is what gets persisted). Irrelevant once cards exist; alarm
  if it ever fires.
- **Summarization** (`_DeepAgentsSummarizationMiddleware`): trigger 85% of
  `max_input_tokens`, keep 10%, **non-destructive** — it records a
  `_summarization_event{cutoff_index, summary_message, file_path}` in
  private state and reconstructs `[summary] + messages[cutoff:]` per call
  (`summarization.py:109-120, 521-558`); the raw log stays in the
  checkpoint. Keep as the backstop. Because it is *outer* to our
  middleware, if we ever want the brief to own compaction completely:
  exclude it via the profile (its `name` is the public alias for exactly
  this, `summarization.py:218-230`) and re-add
  `create_summarization_middleware(model, backend)` **after**
  `ThreadBriefMiddleware` in our list so ours is outermost.
- **`PatchToolCallsMiddleware`** (`patch_tool_calls.py:14-43`) repairs a
  turn interrupted mid-tool-call on the next invoke — the only place the
  message list is rewritten, and it is a repair. Keep; it is what makes
  "user sends another message while research runs" safe.
- **Memory / skills** load once per thread (`memory.py:271-272`), not per
  turn — fine for us; note it for `platform_reference` as a skill.
- **Subagents**: none at launch (`analyze_scope`/`web_research` are
  agent-as-tool). When a deep-mode `research` subagent is added: it starts
  from a single `HumanMessage` (true isolation, `subagents.py:428-444`),
  shares the backend, runs concurrently with sibling `task` calls
  (`factory.py:1748`), callbacks/traces propagate (nested spans tagged
  `ls_agent_type=subagent`), and **only its last message returns** — so it
  must return a `response_format` schema (findings + artifact handles), the
  Manus `submit_result` pattern.
- **Prompt caching middleware** is added unconditionally but no-ops on
  non-Anthropic models; the system prefix and tool block are byte-stable
  across turns (only `MemoryMiddleware` varies, and we do not use it).
- **Checkpointer**: `pip install langgraph-checkpoint-postgres` →
  `PostgresSaver`/`AsyncPostgresSaver` in the `advisor` schema (psycopg 3 is
  already a dependency). The checkpoint is the *mechanical* thread state
  (messages, private middleware state, interrupts); our
  `events/artifacts/evidence` tables are the *semantic* record. Both keyed by
  `thread_id` — **always pass one**, or offloaded history lands in an
  orphaned `session_<random>.md` (`summarization.py:424-427`). The
  duplication is deliberate: the checkpoint can be deleted and rebuilt from
  events; events cannot be rebuilt from the checkpoint.
- **`interrupt_on`**: not needed (research agent, no actions). Available if
  a budget-approval step ("this census will cost ~$0.10, proceed?") is ever
  wanted — it is one config key.

### 6.4 Spike gate (days 2–3; pass/fail, not a bake-off)

Run the six-step protocol from the harness assessment with three tools
(`census`, `recall`, `source`) on the real corpus:

1. census → 2. "most notable results" → 3. "exact supporting passages for the
second" → 4. "restrict to healthcare" → 5. restart process, follow-up →
6. force compaction, repeat the passage request.

Pass criteria: `census` runs once; follow-ups ≤ $0.02 and ≤ 8 s; every
citation passes validation; restart and compaction preserve recall;
**cached prefix tokens identical across consecutive calls** (LangChain's
message rendering on the Responses API must not perturb the prefix);
middleware can substitute the model's view without rewriting the
checkpoint; usage including cache-write reaches the ledger; subagent spans
nest in the tracer. Any hard fail → the domain layer moves to the
hand-rolled loop (est. 1–2 days) and the plan continues.

### 6.5 Verified against the source (0.5.7) — and what to re-verify on upgrade

| Question | Answer | Anchor |
|---|---|---|
| Intercept a tool result before it reaches the model/state? | Yes — `wrap_tool_call` returns the `ToolMessage` that gets appended | `types.py:649`, precedent `filesystem.py:2117` |
| Rewrite the model's view without rewriting persisted state? | Yes — `wrap_model_call` + `request.override(messages=…)`; state untouched unless an `ExtendedModelResponse` carries a `Command` | `types.py:478, 290-311`; precedent `summarization.py:906-1008` |
| Replace (not append to) the base prompt? | Only via `HarnessProfile.base_system_prompt` | `harness_profiles.py:256, 775-793` |
| Drop todos / task / memory prompt weight? | `excluded_middleware={"TodoListMiddleware"}`; omit `subagents=`; omit `memory=` | `graph.py:628-681` |
| Is compaction destructive? | No (shadow event); eviction **is** | `summarization.py:1148-1153`; `filesystem.py:1806-1814` |
| Tunable thresholds? | No (20k/50k tokens hardcoded at the call site; `FilesystemMiddleware` not excludable) | `graph.py:633-639, 173-188` |
| Postgres checkpointer? | `langgraph-checkpoint-postgres` (not in any local venv yet) | `graph.py:715` |
| Responses API + private data? | Pass a pre-built model with `store=False` | `_openai.py:19-23`, `graph.py:247-257` |
| Subagent traces nest? | Yes — ambient callbacks propagate via `ensure_config` | `subagents.py:442`, `runnables/config.py:263-284` |
| Prefix byte-stable? | Yes — system/tool blocks built once; only memory varies | `_utils.py:6-23`, `_tools.py:29-65` |

Churn risk is real (0.5.x → deprecations stamped for 0.6/0.7/1.0 in-tree;
the docs already reference a 1.6 summarization tool). Pin the version, keep
the adapter to the three middlewares + the profile (~300 lines), and re-run
§6.4 on every bump.

---

## 7. QA and observability — the lean loop

### 7.1 Keep (and why)

- **Flag → triage doc** — the loop that found real bugs. Unchanged; the
  flag now records `thread_id`/`turn_id` into the session log.
- **Session log = bundle.** Every turn is replayable and inspectable by
  construction; no export step; the dev console's thread view reads it.
- **Cost ledger** with cache-write visible in the per-step table.
- **Deterministic checks that are cheap and catch real classes:** citation
  resolvability, revalidation drops, coverage-count consistency (stated
  numbers ⊆ tool-computed numbers), truncation-signal probes, ACL probes
  with positive controls (`acl_probes.py`, 248 lines — "a skipped check is
  not a passing check"), prefix-stability assertion (runtime).
- **Computed truth sets** (`truth.py`, 476 lines): nobody authors an oracle;
  a phrase predicate + the corpus supply membership, watermark-checked. This
  is what grades the scan turns of the multi-turn scripts at n=1 without a
  judge. Keep, unchanged.
- **The funnel** (`funnel.py`, 231 lines): E/R/S/C stage sets → 11
  non-collapsing labels; turns "quality is low" into a stage name. Its only
  coupling is the Haystack `ChatMessage` shape in `tool_results`
  (`funnel.py:76`) — a 20-line adapter over the event log.
- **Immutable manifests + fingerprint per run** (`manifest.py`, 46 lines;
  `O_EXCL`): zero churn since written; the one piece of the comparator that
  earns its keep.
- **The CC judge as a pattern** (whole answer + full evidence *with titles*
  in one call; the LLM returns an inventory, **Python computes the
  scores**) — kept for spot checks only (§7.2).
- **Replay `full`** for "does today's code still answer this thread well".

### 7.2 Replace

- The graded golden v2 suite (56 cases, 14 truth sets, 4 scripts) +
  calibration + adjudication + comparator + baseline acceptance → **a
  12-case smoke set** (curated from QUESTION-BANK §1.1/1.2/1.4: one per
  route) and **the canonical multi-turn scripts A/B** (QUESTION-BANK §1.12)
  plus the six-step census protocol, graded on **mechanics** —
  deterministic, no judge: expected route hit; census/scan ran once per
  thread; follow-up cost ≤ threshold; working set carried (the members of
  turn 2's set are exactly turn 3's scope); citations resolve; no dropped
  refs; first-token time. Run on demand before a push; ~$1 per run.
- **LLM judge → spot checks only.** A faithfulness/overclaim judge runs on
  flagged turns and on a weekly sample of ~10 live turns, producing a triage
  note, not a score series. No calibration program. When David flags
  "this is wrong", the triage doc is the ground truth.
- **Prompt/tool-schema hashes in tests → a runtime prefix-stability
  assertion** (same bytes across two consecutive calls in one process) and a
  *logged* digest in the fingerprint for forensics. Edits to prompts and
  tool descriptions are normal commits.

### 7.3 Observability and the post-launch improvement loop

Two decisions here, with different clocks.

**Decide at the outset (substrate, product-independent):**

- **One thread id everywhere.** `advisor.threads.id` = LangGraph
  `configurable.thread_id` = the tracer's thread/session id. Both Opik
  (`opik_tracer.py:335`) and Langfuse's LangChain handler key conversations
  off that config value. This is the decision the May POC got wrong
  (thread_id set, inert).
- **The emitter schema** (`telemetry.py`, ~100 lines): thread → turn →
  model step / tool span / subagent span, each carrying `turn_id`, git sha,
  prompt+tool digest, model, **cost from our ledger** (both products accept a
  supplied cost; in Opik a supplied `total_cost` wins over its own
  calculation, `SpanDAO.java:1888`), cache read/write, artifact handles,
  citations resolved/dropped.
- **The run record** `{question, answer, citations, evidence, trace,
  fingerprint}` — what either product's dataset/experiment features ingest,
  and what ~60% of the existing QA code already consumes (§7.4).

Neither product sits in the critical path; neither owns the semantics of a
citation.

**Decide in week 1 (product): Opik vs Langfuse, with the post-launch loop
as the criterion.** The honest comparison, Opik surveyed at
`~/code/x1/dev/opik` (v2.2.36, 2026-08-20), Langfuse from its current docs:

| Loop capability | Langfuse | Opik |
|---|---|---|
| Thread/session view; cost incl. cache tokens; LangGraph subgraph nesting | ✅ sessions | ✅ threads (LangGraph `thread_id` auto-maps; interrupts/resume modeled) |
| Flag a turn → dataset item → experiment → compare runs | ✅ datasets, experiments (item- and run-level evaluators) | ✅ same, + dataset versioning, + `TestSuite` |
| Online judge on production traces (sampling, filters, backfill) | ✅ LLM-as-a-judge **and code evaluators**; managed templates | ✅ rules (hallucination/moderation/relevance templates) |
| **Thread-level automatic rules** (coherence, frustration, custom Python over the whole conversation) | ➖ session scores via SDK/API; you assemble the context and run the judge yourself | ✅ built in, 15-min inactivity trigger |
| **Built-in agent metrics** (trajectory accuracy, tool-correctness, task completion) + ~20 heuristic metrics | ➖ bring your own judge prompt | ✅ shipped (`metrics/llm_judges/trajectory_accuracy`, …) |
| **Multi-turn user simulation** against an app that owns its history by `thread_id` | ✗ | ✅ `run_simulation(app, SimulatedUser(persona, fixed_responses))` — exactly our `/ask`; `fixed_responses` = deterministic scripts |
| **Prompt/agent optimization** (GEPA, HRPO, evolutionary, few-shot Bayesian, meta-prompt, parameter) over a dataset + metric, incl. tools via `OptimizableAgent` | ✗ | ✅ `opik-optimizer` |
| Annotation queues (human review) | ✅ traces/observations/sessions | ✅ traces + threads |
| Prompt versioning linked to traces | ✅ | ✅ content-hashed commits |
| OTel ingestion | ✅ | ✅ HTTP/protobuf only |
| Self-host | lighter; OSS has users/auth | **9 containers**, **no OSS auth**, undocumented footprint (~6–8 GB), retention off by default; Comet's hosted tier exists for the spike (verify limits) |
| Maturity / churn | mature | fast-moving (daily releases); SDK hard-pins `litellm` — resolver dry-run against `deepagents`/`langchain` first |
| Traps | — | SDK **truncates any span field >20 MB by wholesale replacement** (`payload_truncation.py:41-61`); set `max_payload_size_mb=0` on day one |

Read honestly: **the day-to-day loop — see the thread, flag the turn, make
it a case, re-run, compare, sample-judge production — exists in both.** The
Opik-only column is the *machinery* half of the improvement loop: shipped
agent metrics, thread-level rules, user simulation, optimization. That is
exactly the half this project spent ~8k LOC and ~$300 of judge spend
building by hand, and exactly the half §7.2 proposes to shrink. So the
argument for Opik is not "more features"; it is **"someone else maintains
the generic evaluation machinery, so we never re-grow our own."** The
argument against is ops (nine containers, no auth — it must sit behind a
network boundary wherever production traces originate) and churn.

Two disciplines travel with whichever tool wins, because the Opik-only
features are also the ones nearest the doom loop:

1. **Deterministic mechanics first, judge second** (the escalation-gate
   methodology, kept): scripts A/B and the six-step protocol are graded on
   route, single-census, follow-up cost, working-set carry, citation
   resolution — code, not judges. Shipped judge metrics run as *sampled
   online rules* and *spot checks*, never as a gate.
2. **The optimizer only ever touches bounded, non-product prompts** — the
   census map prompt, the brief updater, the reduce prompt — on held-out
   data, with the result landing as a normal reviewed commit. Never the
   system prompt's product semantics (the no-test-case-hacking rule, now
   with a tool that makes hacking easy).

**Recommendation:** run the product spike in **week 1 on the harness
spike's own trace stream** (days 3–5, parallel), not at day 10.
Go/no-go for Opik: (a) the stack runs on the dev box (or Comet's hosted
tier) within budget; (b) dependency resolution with the v2 env is clean;
(c) `track_langgraph` shows the six-step protocol as one thread with nested
tool/subagent spans and ledger cost; (d) trace → dataset → `evaluate` on
script A works; (e) `run_simulation` with `fixed_responses` drives script A
against `/ask`; (f) truncation disabled. Pass → Opik is the QA-lite
substrate and §7.2's home-grown runner shrinks to adapters (the script
runner becomes `run_simulation` + `evaluate_threads`; the comparator
becomes experiment compare). Fail on (a)/(b) → Langfuse (already wired,
zero ops) carries the build, and the home-grown ~150-line runner stands;
revisit Opik when there is production traffic to justify the ops. Either
way, production traces need an owned project (not the personal dev org)
before launch — an ops/ownership item on every path.

### 7.4 What the harness survey found (the inventory behind §7.2)

The current machinery is ≈13,200 LOC (`experiments/` 7,986 + judges 921 +
tests 4,058 + `qa/` 245), 92 committed manifests. Roughly **60% is already
black-box** over `(question, answer, citations, evidence)`; the coupling is
in four places — `funnel.tool_results` (Haystack message shape),
`retrieval_explain`, the evidence-registry↔citation `ref` join, and
`replay.py`. A ~15-field run record
`{question, answer, citations[{ref,n,type,locator}], evidence[{ref,snapshot,
title,locator}], retrieval_trace, fingerprint}` — which the v2 event log
emits natively — carries everything else over.

What v2 does **not** rebuild, with the evidence:

- `cases.py` — 1,230 lines of compiler for 56 cases: 17 classes × 6
  readiness fields × 7 `blocked_on` reasons × 4 fallback contracts, several
  classes with exactly one case. The readiness taxonomy encodes a roadmap
  into the test suite; cases flip grading mode when a tool ships, which
  severs the contract and voids every baseline.
- `adjudicate.py` — 901 lines, **eight** copy-pasted escalation gates, each
  its own rubric + schema + k=3 majority loop; the consolidated review's
  own words: "seven copies of transport/quorum/fail-default logic". The tie
  policy dismisses flags on negative-polarity gates (finding V2).
- The prose-polarity regex machine in `checkers.py:130-280` — 11 commits,
  the highest churn in the harness, a patch chain (census buckets → list
  inheritance → hedge regexes → trailing hedges → word-start matching); the
  fix was to *add* a judge on top, not delete the formula.
- Calibration: 32 "human" labels, **all 32 `assist_shown: true`**, reported
  as `human-calibrated`; two rounds of blinding fixes and a 337-line
  label UI guarding them (~25 LOC per label).
- Scoring-contract churn s2→s6 in eight days; two judge backends kept
  alive, one documented as ~92% false-positive and "deliberately un-fixed".
- The whole gate stack can grade one case on up to 14 units, each
  escalatable to a 3-sample Opus judge — and the accepted baseline's entire
  failure surface reduces to three labels (`citation_coverage_error` 20,
  `routing_error` 12, `synthesis_error` 11).

What it **learned** that v2 encodes as tests or tool semantics rather than
as grading machinery:

- Overclaiming, not missing, is the dominant failure; report it separately.
- "X does not mention Y" is the honest answer, never an overclaim;
  process/disclosure statements ("I searched X and found nothing") are not
  citable claims.
- A coverage claim is graded against what the prior turn's tools actually
  searched, never against what the answer says it searched — in v2 that is
  a card field (`coverage`) compared to a card field, no prose parsing.
- Evidence titles are where company names live; strip them and the judge
  false-positives explode.
- Judges err strict, never lenient (zero false clean bills across four
  candidates); re-judging identical answers swings a question ±0.2–0.4 and a
  20-question mean ±0.07 — quote one decimal, never three; never pick the
  judge that agrees with another model.
- Corpus facts: 25 of 64 companies have evaluation documents (honest
  enumeration denominator is 25); `PhD` has zero hits, `Ph.D` three; 9
  company names exist as both prod fixture and test entity; record
  summaries are generated text and never evidence.
- A skipped check is not a passing check; an incomplete run is not a
  passing run; never edit the golden case to make the harness pass.

---

## 8. Build plan (all items David-gated)

| Day | Deliverable | Proof |
|---|---|---|
| 1 | Schema: `threads.brief`, `events`, `artifacts`, `evidence`; `session.py` projections; `artifacts.py` cards for census/scan/query/search/working_set | unit tests over cards; bundle projection reproduces today's bundle fields |
| 2–3 | Deep Agents skeleton: pinned `deepagents` + `langgraph-checkpoint-postgres`; x1 harness profile (base prompt replaced, todos excluded, write/edit/glob/ls excluded, GP subagent off); pre-built Responses model with `store=False`; `ArtifactMiddleware`, `CostMiddleware`, `PostgresSaver`, 3 tools; **spike gate §6.4** | six-step protocol passes; prefix-stability assertion green; prefix ≤ ~5k tokens; decision recorded in DECISIONS |
| 4–5 | `ThreadBriefMiddleware` + brief schema; thread-scoped evidence + shown-this-turn + revalidation; `recall` select grammar | script A/B run end-to-end; follow-up cost ≤ $0.02 |
| 6–7 | Full tool port (`search`, `scan`, `query`, `source`, `web`, `platform_reference`); prompt distillation; server-owned `/ask` + SSE; console on the new API | smoke-12 passes on mechanics; live session with David |
| 8–9 | Working sets: `resolve_set`, page-context snapshot → `ws:n`, scope handles on scan/census/search | "just the ones over 77 → which of these mention X → exact quotes" without prose reconstruction |
| 3–5 (parallel track) | Telemetry emitter + single thread id (day 3, with the harness spike); **Opik go/no-go per §7.3** on that trace stream (Comet hosted tier or docker on the dev box; dependency dry-run; truncation off) | six-step protocol visible as one thread with nested spans + ledger cost in the chosen tool; decision in DECISIONS |
| 10 | Cache-write in the step table; prefix-stability alarm; offload/summarize alarms; online sampled judge rule (hallucination/faithfulness) on live turns | alarms fire in a forced test; one sampled judge score lands on a live trace |
| 11–12 | QA-lite: smoke-12 + scripts A/B on mechanics — Opik-native (`run_simulation` + `evaluate_threads` + experiments) if the go/no-go passed, else the ~150-line home-grown runner; ACL probes; spot-check judge on flagged turns; replay `full` | runner green; one triage doc written against v2 |
| 13–15 | Supervised live use; triage; first `research` subagent **only if** a flagged thread demands it; DECISIONS entry: v2 replaces v1 behind `/ask` | David's call |

Parallel, any time, independent of v2: **E1b `halfvec(1024)` index config**
(§4.6 — ~1 h, retrieval-only, may re-open HNSW on the f1-micro); **E2b
reranker re-run** (§4.6 — ~1 h, precision@8 + MRR on the tripled corpus,
before/after-expansion arms; enable for `search` only if it moves); E5
lexical-leg query preprocessing; skills for `platform_reference`/style.

### What this plan deliberately does not do

- Rebuild retrieval, ingestion, or the corpus model. They are fine.
- Start with subagents, planners, or memory across threads. Add on evidence.
- Re-create the graded golden suite, the judge calibration program, or the
  comparability contract. If a future David wants a score series, the
  session log holds everything needed to compute one retroactively.
- Pick an observability product before the trace stream exists.
- Promise a harness. Deep Agents is the recommendation and the first thing
  built; the domain layer is written so that the recommendation can be wrong
  cheaply.

---

## 9. Decisions this supersedes (if adopted)

| Prior decision | Where | v2 |
|---|---|---|
| Tier-1 loop stays Haystack-owned; CC/Agent SDK at the edges only | CC-AGENTS-DESIGN §3, PLAN Track H | Loop moves to Deep Agents/LangGraph; edges unchanged |
| History is client-supplied, text-only; last 5 verbatim, older condensed | `advisor.py:196-221`, `service.py:60-63` | Server-owned thread; brief + last 2 verbatim; tool results never replayed |
| Evidence registry per turn; "cite only what you retrieved this turn" | `advisor.py:243`, prompt rule 1 | Thread-scoped registry; citability = shown-this-turn (fresh or recalled); revalidated live |
| `recall_evidence` as a single narrow tool (PROPOSED) | EVIDENCE-RECALL-DESIGN | Generic `recall(handle, select)` over typed artifacts |
| `SYSTEM_PROMPT_SHA256` / `TOOL_SCHEMA_SHA256` pinned in tests; scoring-contract severs | `tests/test_agent_units.py`, QA-RUNBOOK | Runtime prefix-stability assertion; digests logged, not pinned |
| Golden v2 graded suite + calibration + adjudication as the quality gate | GOLDEN-V2-DESIGN, DECISIONS 2026-08-04/06/07 | Smoke-12 + scripts on mechanics + flagged-turn spot checks |
| No subagents in Tier-1 | thread-021 triage 3a | Agent-as-tool named and kept; `task` subagents available for deep mode on demand |
| Haystack `<3` pin; Haystack 3 State considered | HARNESS-STRATEGY §6.3 | Moot |

Decisions that **stand**: research agent not actor; open cross-user research
with class guardrails only; advisor access = premium purchasers; models are
experiments (terra stays until a bake-off says otherwise; luna for brief and
census maps); TEST env only; advisor writes only its schema; no test-case
hacking; no silent truncation; commit at milestones.

---

## References

- Code anchors: `x1_advisor/agent/advisor.py` (loop, history, prompt),
  `service.py` (API, console), `agent/tools.py` (contracts),
  `agent/evidence.py`, `agent/bundle.py`, `retrieval.py`, `scan.py`,
  `agent/analyze.py`, `agent/queries.py`, `filters.py`, `cost.py`,
  `fingerprint.py`, `telemetry.py`, `schema.sql`
- Bundles: `.qa-artifacts/runs/turn_00000086_thread_37.json`,
  `turn_00000088_thread_37.json`; ledger `cost_ledger.jsonl`
- [HARNESS-STRATEGY-ASSESSMENT-2026-08-17](HARNESS-STRATEGY-ASSESSMENT-2026-08-17.md),
  [EVIDENCE-RECALL-DESIGN-2026-08-15](EVIDENCE-RECALL-DESIGN-2026-08-15.md),
  [ANALYZE-SCOPE-DESIGN-2026-08-13](ANALYZE-SCOPE-DESIGN-2026-08-13.md),
  [CONTEXT-SNAPSHOT-DESIGN-2026-07-30](CONTEXT-SNAPSHOT-DESIGN-2026-07-30.md),
  [QUESTION-BANK](QUESTION-BANK.md) §1.12, §3
- [refs/2026-05-08-ADVISOR-CONTEXT-ENGINEERING-PLAN](refs/2026-05-08-ADVISOR-CONTEXT-ENGINEERING-PLAN.md)
  (the gen-2 diagnosis and the handle architecture),
  [refs/manus-context-engineering-transcript](refs/manus-context-engineering-transcript.md)
  (compaction vs summarization, schemas as contracts, agent-as-tool, avoid
  over-engineering),
  [refs/langchain-deepagents-context-engineering](refs/langchain-deepagents-context-engineering.md),
  [refs/langchain-deep-agents-workshop](refs/langchain-deep-agents-workshop.md)
- `chats/chat1.md` (OSS harness survey; DeepSeek Harness), `chats/chat3.md`
  (Opik vs Langfuse; Deep Agents + Opik boundary)
- Previous generation: `~/code/x1/deepagents-poc/x1-link/services/x1-deep-advisor-python`
