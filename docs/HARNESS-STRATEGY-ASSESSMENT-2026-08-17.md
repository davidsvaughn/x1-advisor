# Harness Strategy Assessment — multi-turn substrate, evolve vs. replatform

**Date:** 2026-08-17
**Status:** point-in-time assessment + proposed direction. Every build/spike
listed in §7 is **David-gated** — nothing here is authorized yet.
**Trigger:** the thread-037 amnesia finding
([EVIDENCE-RECALL-DESIGN-2026-08-15](EVIDENCE-RECALL-DESIGN-2026-08-15.md)),
David's dissatisfaction with multi-turn deep-research ergonomics and token
costs, and the external-agent consultation in
[chats/chat1.md](chats/chat1.md) (DeerFlow / Deep Agents / DeepSeek Harness
evaluation), which David flagged as possibly too conservative about
replatforming.
**Inputs:** the four context-engineering docs
([May 2026 plan](refs/2026-05-08-ADVISOR-CONTEXT-ENGINEERING-PLAN.md),
[Manus webinar notes](refs/manus-context-engineering-transcript.md),
[LangChain Deep Agents](refs/langchain-deepagents-context-engineering.md),
[evidence-recall design](EVIDENCE-RECALL-DESIGN-2026-08-15.md)); triage
threads 021/022/027 (local); the thread-36/37 bundles; a full-codebase
coupling audit (§3); the live-thread cost ledger (§2); DECISIONS
2026-08-14 (×4); PLAN §R gate state.

---

## 1. The two reported problems, re-scoped against evidence

### 1.1 Multi-turn amnesia — real, precisely located, design-level

Verified directly in the bundles:

| Turn | Question | Tool calls | Cost | Latency |
|---|---|---|---|---|
| 86 (thread 37) | "How many evaluations flag possible weaknesses… in brand or market positioning?" | `analyze_scope` | $0.0895 | 37.2s |
| 88 (thread 37) | "can you show me a few of the most notable results?" | `analyze_scope` **again, same scope** | $0.0860 | 47.5s |

(Bundles: `.qa-artifacts/runs/turn_00000086_thread_37.json`,
`turn_00000088_thread_37.json`.)

Turn 88 re-ran the full census 30 seconds after turn 86 produced it,
because follow-ups can see prior *prose* but not prior *evidence*. The
mechanism (audited in code):

- History is client-supplied text only. `AskRequest` carries a
  `history` array from the caller (`service.py:60-63`); the dev console
  maintains it as a JS array. The server never reads `advisor.turns` to
  reconstruct history.
- `_history_messages` (`advisor.py:200-221`): last 5 exchanges verbatim,
  older turns luna-summarized to ≤150 words. Prose only.
- Tool results, the evidence registry, refs, and coverage never survive a
  turn boundary — stated explicitly at `advisor.py:233-236`.
- Meanwhile the full forensic record (messages, evidence snapshots,
  findings, coverage, ACL snapshot) **is** persisted every turn in
  `advisor.turns.research_record` (`schema.sql:74-83`,
  `bundle.py:59-100`) — but nothing in the live answer path reads it.
  Readers today: the dev console's citation renderer, replay, and the
  judge. QA-only.

So the system has rich structured memory and a live agent that cannot
touch it. The [evidence-recall design](EVIDENCE-RECALL-DESIGN-2026-08-15.md)
diagnoses this correctly as two individually-right decisions (text-only
history for context discipline; per-turn evidence for citation validity)
intersecting badly.

**Key historical fact the replatform debate must absorb:** the
[May 2026 plan](refs/2026-05-08-ADVISOR-CONTEXT-ENGINEERING-PLAN.md) was
written for the *previous* advisor — built on **LangChain DeepAgents** —
and records the *same* failure modes thread-037 just exhibited
("follow-up turns reconstruct the active startup set from prose memory",
"verbose tool payload replay"). This problem has now followed the project
across two harnesses. That is strong evidence it is a **design problem,
not a framework problem**: no off-the-shelf harness ships the missing
piece, which is X1-aware recall with ACL revalidation (chat1's own
analysis of DeepSeek Harness concedes this — its session recall is
literal/FTS over logged text; it does not know a finding from a coverage
count, and it would happily replay ACL-revoked evidence).

### 1.2 "Model reads too much text" — narrower than it feels, mostly already fixed

Tabulated across all 38 live-thread bundles in `.qa-artifacts/runs/`
(turns 14–88): total $1.26, mean $0.033/turn, max $0.219.

- **Ordinary turns are healthy.** $0.002–0.02, 3–8s. terra's per-step
  uncached input is ~6–15 tokens riding a 4–8k cached prefix — the
  context discipline and prompt-cache strategy are demonstrably working.
- **The top five costs are all `analyze_scope` turns** (78, 82, 84, 86,
  88: $0.086–$0.219). The token eating is luna map-reads inside the
  census tool, not terra context bloat — and the last week's commits
  attacked exactly that:
  - sections-as-units + per-eval read cap (`f6a5189`; DECISIONS
    2026-08-14): census $0.197 → $0.059 (K=3) → **$0.039 (K=2, kept,
    `cd15e6f`)**, 26s, all 45 evals covered;
  - map-call cache opt-out (`03d79e6`, `analyze.py:222-228`): unique
    per-doc prompts were paying the 1.25× cache-write premium with zero
    reads — $0.026 of the $0.197 turn;
  - dense-leg rewrite + ivfflat (`81ad500`; DECISIONS 2026-08-14): the
    old correlated-subquery shape could never use any vector index —
    every search was an exact full scan (39.4s cold). Now index-served:
    warm **0.44s** end-to-end including the query-embed call.
- **What remains of the cost problem is essentially one class:
  follow-ups re-earning evidence** (turn 88). That is the recall problem
  again, wearing a cost hat.

Conclusion: single-turn efficiency does not motivate a replatform; it has
been (and continues to be) fixed in place with measurements. The
multi-turn substrate is the genuine gap.

---

## 2. The strategic finding: X1 is barely on Haystack at all

A full-code audit measured the framework coupling surface:

**Haystack-coupled: ~35 lines.** The `Agent` construction
(`advisor.py:249-256`), the `OpenAIResponsesChatGenerator` wrapper
(`advisor.py:60-77`), `ChatMessage` construction in `_history_messages`
and the wrap-up path, the `Tool` objects (`tools.py:477-677`),
`Usage.from_haystack_meta`, `m.to_dict()` in `serialize_messages`, and
`ChatMessage` use in `replay.py`. Haystack (`haystack-ai` 2.30.2, pinned
`>=2.30,<3`) contributes the step loop, tool dispatch, and message
serialization. Nothing else.

**Framework-independent (the actual asset):**

| Asset | Where | Why it matters |
|---|---|---|
| Evidence registry + per-ref snapshots | `evidence.py:100-210` | judge is a pure function of the bundle |
| Citation validator (drop/renumber) | `evidence.py:213-247` | invalid citations cannot survive |
| Single ACL predicate, shared by 3 evidence paths | `retrieval.py:65-99`, imported by `scan.py:39`, `analyze.py:32` | one ACL implementation, never two |
| Typed filter layer | `filters.py` | closed an ACL-bypass class |
| Fingerprints + prompt/tool-schema hash pinning | `fingerprint.py:75-88`, `tests/test_agent_units.py:52,94` | cache stability + QA comparability |
| research_record / bundle schema v3 | `bundle.py` | the forensic memory recall will mine |
| Cost ledger, raise-on-unknown-model | `cost.py` | never a silent $0 |
| Hand-rolled hybrid retrieval (SQL, not Haystack retrievers) | `retrieval.py` | the "RAG stack" owes Haystack nothing |
| QA harness | `experiments/` (7,986 LOC — bigger than the 3,569-LOC product) | the measurement loop |

This dissolves the "replatform" framing. There is no platform to escape.
What adopting an external harness would actually mean is **building the
conversation substrate X1 never built** (server-owned sessions, resume,
compaction-with-recall) on someone else's runtime instead of on our
Postgres. The real question is not *replatform vs. evolve*; it is
**"who owns the session log — our Postgres or their process?"**

---

## 3. Review of the external-agent recommendation (chat1)

Its layering insight is correct and worth adopting as doctrine:

> The harness owns conversation mechanics; X1 owns what a handle *means*
> and whether its contents remain valid.

Three corrections — note that not all cut in the conservative direction:

1. **"Replatforming" is a misnomer** (§2 above). The 35-line coupling
   makes "swap the loop runner" a contained change, not a rewrite. The
   agent's effort model implicitly priced a rewrite.
2. **Its "blocking" production concerns are not differential.** It scored
   DeepSeek Harness 4/10 for production because it lacks auth, tenancy,
   and distributed persistence. All three are *also missing from X1
   today*: Gate 2 and Gate 3A are open (PLAN §R), `_acl_for` is a dev
   stub (`service.py:66-74`), history is client-supplied. That bill is
   owed on **every** path, so it cannot count against any particular one.
   If anything this cuts the adventurous way: replatforming is cheaper
   *now* than it will ever be again, precisely because the hardening has
   not yet been spent on the current shape.
3. **It missed the strongest conservative evidence available** — the
   same amnesia failure previously occurred on LangChain DeepAgents
   (§1.1). Harness swaps do not fix this class by default.

**Missing candidate:** the **Claude Agent SDK** — which
[PLAN §R Track H3](PLAN.md) *already names* as the Tier-2 harness
("headless agent over MCP tools wrapping `build_tools` under the
requesting principal; Agent SDK + API billing in production"). The
project committed to this harness for deep mode months ago; the demand
trigger ("on demonstrated demand") has arguably now fired. One real
caveat: the SDK is Claude-only, which cuts against the prime directive
("models are experiments, not commitments") and against terra, the
measured E8 winner (`advisor.py:32-53`). DeepSeek Harness is
model-pluggable (adapter registry on `ctx.llm`, per its architecture
doc). This trade-off belongs in the bake-off scoring.

Where chat1 is right and stays right: DeepSeek Harness's event-sourced
session model (append-only log as source of truth; compaction that
shadows rather than deletes; recall tools over shadowed events; spill
files with locators) is the best articulation so far of the architecture
X1 needs. And its "webserver is not production" warnings are accurate.

---

## 4. The reframe: three convergent moves, then a measured divergence

Both futures — evolve-in-place and harness-adoption — need the **same
first three things**, and all three are harness-independent:

### 4.1 Ship `recall_evidence` (already designed, PROPOSED)

Per [EVIDENCE-RECALL-DESIGN-2026-08-15](EVIDENCE-RECALL-DESIGN-2026-08-15.md):
~100 lines against the existing `research_record`, no schema changes.
Every recalled support re-fetched through the live ACL query and
re-registered as a fresh ref — only the *selection* is remembered.
Kills the observed $0.086/47s follow-up class → ~$0.01/5–8s.

This is not a stopgap that a later harness would obsolete. It **is** the
X1-aware artifact-recall layer that any harness would call through
anyway (chat1 concedes exactly this: "your proposed `recall_evidence` or
generalized `recall_artifact` layer is still needed"). Costs: one more
tool → `TOOL_SCHEMA_SHA256` churn + QA-trio grading, batched into the
owed s7 batch.

### 4.2 Generalize to a thread-scoped artifact registry

The May plan's handle architecture, finally landed in the current stack:
heavy tool results (censuses, scan result sets, structured-query result
sets, web research) register an **artifact handle**;
`list_artifacts()` / `recall_artifact(handle, selection)` trade a handle
for revalidated content. Postgres already persists everything needed
(the artifacts live inside `research_record` today); this is mostly
read-path code plus a naming discipline. Mental model from the May plan:
*inline content is wrong by default; handles are right by default; tools
trade a handle for content on demand.*

### 4.3 Server-owned history (the Gate 3A slice)

`/ask` assembles history from `advisor.turns` by `thread_id`; the client
sends only `thread_id` + the new question. Small, owed regardless
(PLAN §R Gate 3A "server-owned history"), and a prerequisite for any
session substrate. Also the precondition for cross-turn handles being
trustworthy (the server, not the browser, knows what turn N's artifacts
were).

Do these three and we will have built, in our own stack, the exact thing
DeepSeek Harness's architecture teaches — **the session log as source of
truth, with handles instead of replay**. Steal the architecture, not the
codebase.

---

## 5. The bake-off — where "move fast and break things" earns its keep

Run the adventure the way this project already runs experiments: as a
harness bake-off on branches, graded by the machinery we built. The QA
harness is precisely the instrument that makes replatform bets cheap and
measurable — most teams cannot move fast on this decision because they
cannot tell whether the new thing is worse. We can.

### Spikes (parallel branches, timeboxed ~2–3 days each)

- **Spike A — Claude Agent SDK** (= the Track H3 prototype, pulled
  forward): mount `build_tools` via in-process MCP under a fixed test
  principal, X1 system prompt, 3–5 tools only, session persistence on.
  No new language boundary; already sanctioned by PLAN §R.
- **Spike B — DeepSeek Harness x1 profile**, per chat1's §"What I would
  prototype": coding tools disabled, X1 tools via MCP, session-query
  tools + durable FTS index enabled, fixed local test principal.

### Shared protocol (from chat1, adopted verbatim)

1. Run the semantic census.
2. "Show me the most notable results."
3. "Show the exact supporting passages for the second one."
4. "Now restrict that to healthcare companies."
5. Restart the harness; ask another follow-up.
6. Force compaction; repeat the passage request.

**Success criteria:** `analyze_scope` runs once; follow-ups ≈ $0.01–0.02;
every citation passes the existing validator; restart and compaction do
not break recall; no cross-principal artifact access. Answers graded by
the CC judge; outcome lands as a dated DECISIONS entry. The prime
directive extends by one word: **harnesses** are experiments, not
commitments.

### Three invariants any winning harness must preserve

This — not git risk — is the real cost of adoption. A harness that
cannot honor these fails the spike regardless of ergonomics:

1. **Byte-stable prompt/tool-schema surface.** Anything that injects its
   own preamble or mutates schemas per-turn breaks the cached prefix and
   every committed QA comparison (`fingerprint.py:80-83` records four
   silent cache invalidations from tool-description edits alone).
2. **The per-ref snapshot contract** (`evidence.py:63-71`). The judge is
   a pure function of the bundle; lose the snapshots and historical
   scores drift with the corpus.
3. **`acl_resolved` stays forensic-only.** Recalled evidence is always
   re-fetched under the live principal (`replay.py:20-25` is the
   precedent; the recall design's revalidation rule is the same
   boundary).

### Adoption path if a spike wins

Beachhead = **deep-research mode** behind the existing service API:
harness-backed sessions serve the explicit "go deep" experience while
Tier-1 quick answers stay on the proven loop. Promote the harness to own
the whole loop only after it has carried deep mode in supervised use.
(This is Track H3's shape; the only change is treating it as the main
event rather than a later addition.)

---

## 6. Side findings from the audit (independent of the strategy call)

1. **The jina reranker is implemented but OFF in the live path**
   (`retrieval.py:196-237`; `search_corpus` never passes `rerank=True`,
   `tools.py:104-106`; only `experiments/run.py --rerank` uses it). An
   unexploited quality lever; cheap bake-off (E-series shape, paired
   manifests).
2. **Read-efficiency telemetry.** The bundles already carry per-step
   usage and `timings_ms`; a derived per-turn metric (tokens read by
   tools vs. evidence actually cited/used) would make "the model read
   too much" a detected condition rather than a felt one — the
   escalation-gate pattern: formulas detect, judge/triage disposes. No
   prompt wording involved.
3. **Haystack 3** (structured State, large-result offloading) is a
   lesser option for this problem: its State helps within a run, not
   across turns, and the artifact registry (§4.2) supersedes what we
   would use it for. Not recommended as the multi-turn fix; the `<3` pin
   can be revisited on its own merits later.

---

## 7. Recommended sequence (all items David-gated)

| # | Item | Effort | Gate |
|---|---|---|---|
| 1 | Build `recall_evidence` per the 08-15 design | ~1 day + owed-s7 QA batch | David: "build it" |
| 2 | Server-owned history slice (`/ask` assembles from `advisor.turns`) | ~0.5–1 day | David |
| 3 | Artifact-registry generalization (handles + `list_artifacts`/`recall_artifact`) | ~2–3 days, after 1–2 prove the pattern | David (scope) |
| 4 | Spike A: Agent SDK deep-mode prototype (H3) | ~2–3 days, branch | David |
| 5 | Spike B: DeepSeek Harness x1 profile | ~2–3 days, branch | David |
| 6 | Bake-off run + DECISIONS entry ("who owns the loop") | after 4–5 | David decides |
| 7 | Reranker bake-off (§6.1) | independent, anytime | David |

Items 1–3 are on every path. Items 4–6 are the adventure, run as
experiments with the existing judge as referee. Item 7 is free-standing.

My recommendation on ordering within the spikes: **Spike A first** — it
is already sanctioned as H3, needs no Python↔TS boundary, and its main
risk (Claude-only models) is exactly the kind of thing the bake-off
scoring should expose rather than assume.

---

## References

- [EVIDENCE-RECALL-DESIGN-2026-08-15.md](EVIDENCE-RECALL-DESIGN-2026-08-15.md) — the proposed recall tool (PROPOSED, unbuilt)
- [refs/2026-05-08-ADVISOR-CONTEXT-ENGINEERING-PLAN.md](refs/2026-05-08-ADVISOR-CONTEXT-ENGINEERING-PLAN.md) — the ancestor plan (DeepAgents era); handle architecture; same failure modes
- [chats/chat1.md](chats/chat1.md) — external-agent consultation: OSS harness survey + DeepSeek Harness evaluation
- [refs/manus-context-engineering-transcript.md](refs/manus-context-engineering-transcript.md), [refs/langchain-deepagents-context-engineering.md](refs/langchain-deepagents-context-engineering.md) — background principles (offload / compact / retrieve / isolate / cache)
- [ANALYZE-SCOPE-DESIGN-2026-08-13.md](ANALYZE-SCOPE-DESIGN-2026-08-13.md) + DECISIONS 2026-08-14 (×4) — the census tool and its cost optimization arc
- [PLAN.md](PLAN.md) §R — gate state (Gate 2 / 3A open; Track H3 names the Agent SDK shape)
- Bundles: `.qa-artifacts/runs/turn_00000086_thread_37.json`, `turn_00000088_thread_37.json` (local, gitignored)
- Commits this arc: `81ad500` (ivfflat + index-served dense leg), `03d79e6` (map-call cache opt-out), `f6a5189` (sections-as-units + K cap), `cd15e6f` (counts from coverage; K=2), `7d81fea` (refs + recall design)
- Code anchors: `advisor.py:200-236` (history), `service.py:60-74` (client history + ACL stub), `evidence.py` (registry/validator), `bundle.py` (research_record), `retrieval.py:196-237` (dormant reranker), `analyze.py:41-44,222-228` (K cap, cache opt-out)
