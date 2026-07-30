# Context Snapshot — carrying "what's on screen" into the advisor

> Date: 2026-07-30. Status: **proposal, for review** (same contract as the other
> 2026-07-30 design docs: verify claims, challenge decisions; reviewer questions
> in §9).
>
> Motivation: the recovered question bank (`QUESTION-BANK.md`) shows the single
> most common phrasing across every era of David's test questions is
> working-set-relative — "this startup", "the startups currently on screen",
> "passing the current filters". This is the canonical in-page copilot scenario,
> and the current x1-advisor has **no representation for it**. This doc proposes
> the canonical architecture. Scope guard: context flows **app → advisor only**.
> The advisor is a research agent, not an actor — it never pushes filters,
> navigation, or mutations back to the page (that was the abandoned copilot-era
> direction).

## 1. Current state — do we have anything for this?

**In x1-advisor: no.** `AskRequest` is `{question, thread_id, history}`
(`service.py:37-40`); no context field, nothing in ARCHITECTURE/PLAN. This was
a deliberate v1 simplification that the recovered questions now show cuts out
the product's most natural usage mode.

**But the seams are ready.** Three existing mechanisms make this a clean
addition rather than a redesign:

1. **Per-request tool closures** — `build_tools(conn, acl=…, registry=…,
   tracker=…)` already injects per-request state into every tool; a `context`
   resolver rides the same path.
2. **Retrieval metadata filters** — chunks carry entity refs
   (`entity_type`/`entity_id`) in metadata; an entity-set predicate is one more
   filter clause in `_filter_sql` (behind the same whitelist F1 introduces).
3. **The ACL pattern** — a per-request dict resolved server-side, consumed at
   the retrieval layer, never trusted from the model. Context follows the same
   shape and the same trust rules.

**Prior art in x1-link** (the abandoned copilot): the page-capability layer,
`applySearchIntent`/`updateWorkingMemory` tools, `PAGE-INTEGRATION-PATTERN.md`,
and the open design question "what belongs in advisor-owned working memory
versus page-owned live state?". The captured threads (`.x1/langfuse-threads/`)
show it working live. What we keep from that era is the *read half*: page state
flows in as context. What we drop is the write half (agent driving the UI).

## 2. Design principles

**P1 — Reference-based, never content-based.** The client sends entity refs and
filter specs, never row data or rendered content. The server re-resolves
everything against the DB under the requester's ACL. Reasons: (a) a client
could otherwise spoof context content; (b) payloads stay tiny; (c) data is
fresh at resolution time. The snapshot is a *pointer structure*.

**P2 — Context is an intersection, never an expansion.** The working set can
only *narrow* what retrieval returns. Membership in the snapshot grants zero
access: every context-scoped query still passes the full ACL predicate set. A
snapshot naming gated entity ids yields the same gated-vs-absent behavior as
any other query.

**P3 — Snapshot semantics, pinned per turn.** The context is immutable for the
turn it arrives with and is persisted in the turn bundle
(`research_record.request.context` — QA-loop schema §4.1). "These" in a
follow-up resolves against recorded state, not against whatever the page shows
now. This is what makes working-set questions **replayable** and testable.

**P4 — Token-cheap by construction (§9 discipline).** The model never sees raw
id lists. It sees a 1–2 line summary ("Context: search page, 20 startup results
of 47 matching filters score≥77 + industry=healthcare, none selected") and
passes an opaque **scope handle** to tools (`scope: "working_set"`). The server
expands handles to id sets. A 200-entity working set costs the prompt ~30
tokens, not ~2,000.

**P5 — Versioned, per-page-type schemas.** `schema: 1`; page types are an enum
with standardized payloads. Unknown page types degrade to "no context" plus a
log line — never an error.

## 3. Wire shape

```jsonc
POST /ask
{
  "question": "which of these mention regulatory risk in their evaluations?",
  "thread_id": 42,
  "context": {                                  // optional; absent = plain chat
    "schema": 1,
    "page": {
      "type": "search",                         // search | entity_detail | board | portfolio | report
      "route": "/startups",
      "selected": null                          // or {"entity_type":"startup_company","id":17}
    },
    "working_set": {
      "entity_type": "startup_company",
      "filter_spec": {"score_gte": 77, "industry": "healthcare"},  // intensional definition
      "total": 47,                              // full filtered-set size
      "visible_ids": [3, 17, 42, 88, …],        // the page actually rendered (≤ page size)
      "sort": "score desc"
    }
  }
}
```

Two deliberate representations, because David's own phrasings distinguish them
(P4 smoke suite §9 lists both):

- **"currently on screen"** → `visible_ids` (extensional, small, exact).
- **"passing the current filters"** → `filter_spec` + `total` (intensional).
  The server re-materializes the full id set from the spec at resolution time —
  so "all 47", not just the visible 20. `filter_spec` vocabulary is a
  server-defined whitelist (same registry the F1 filter-key fix introduces);
  unknown keys are rejected loudly at request validation, not silently dropped.

An `entity_detail` page needs only `page.selected`; a board page's working set
is the card entity list (read-only). Board/notes content itself remains
NOTES-status until Phase-6 ingestion — context can scope a search to board
entities today even though note text isn't searchable yet.

## 4. How the agent consumes it

1. **Context line** — `run_turn` renders the snapshot to one compact line
   appended to the turn's user message (volatile tail, never the cached prefix):
   `[Context: search page · 20 of 47 startups visible (filters: score≥77,
   industry=healthcare) · none selected]`. That plus tool descriptions is all
   the model needs for deixis ("these" = the working set; "this startup" =
   selected).
2. **Scope handles on tools** — `search_corpus`, `scan_corpus` (when built),
   and `structured_query` gain an optional `scope` parameter:
   `"visible" | "working_set" | "all"` (default `all`). The server resolves the
   handle to an entity-set predicate. The model never enumerates ids.
3. **Selected-entity default** — on an `entity_detail` page, "this startup"
   questions resolve `selected` into an entity filter automatically when the
   model scopes to it; bare searches stay corpus-wide.
4. **Coverage reporting** — a `working_set`-scoped answer states its scope
   ("searched all 47 matching startups" / "the 20 on screen"), which is exactly
   the honesty users demanded in the captured threads ("did you search all
   20?", "why did you only search their summaries?"). The scan-tool coverage
   contract (QUESTION-BANK §3.2) and this compose naturally.

## 5. Multi-turn semantics (deixis rules)

- Each request MAY carry a fresh snapshot; the newest snapshot supersedes.
- A turn with no snapshot inherits the thread's most recent snapshot (persisted
  server-side with the thread once server-owned history lands — F6).
- **Two kinds of "these"**: the UI working set, and the *answer set* (the list
  the assistant just presented — e.g. after "which of these mention regulatory
  risk?" returned 6 companies, "pull the exact quotes for each" means those 6).
  v1 rule: the model resolves answer-set deixis conversationally (the names are
  in history — verified working in the multi-turn tests); scope handles cover
  UI deixis only. If funnel data later shows answer-set misresolution, promote
  answer sets to tracked state (the evidence registry already knows which
  entities each answer cited). Don't build that until the failure is observed.

## 6. Testing — the snapshot unblocks WS questions *before the UI exists*

This is the immediate payoff: the harness injects **synthetic snapshots** as
fixtures. Every WS-status question in the question bank becomes runnable
headlessly today:

```yaml
- id: g2-041
  question: "Across the startups currently on screen, what risks are mentioned most often?"
  context_fixture: {page: {type: search}, working_set: {entity_type: startup_company,
                    visible_ids: [<10 fixture ids>], filter_spec: null, total: 10}}
  expected_route: search(scope=visible)
```

Golden v2 gains a `context_fixture` field; the funnel classifier checks scope
resolution (a new mechanical failure label: `scope_error` — answered corpus-wide
when the question was working-set-scoped, or vice versa). Multi-turn scripts
(bank §1.12) carry snapshots on turn 1 and test carryover on later turns.

## 7. Implementation slices (all small; ordered)

1. **Schema + plumbing** (~½ day): `context` on AskRequest, validation +
   filter_spec whitelist, context line rendering, persistence into
   `research_record`. No tool changes yet — "this startup" via selected-entity
   already improves entity_detail asks.
2. **Scope handles** (~½–1 day): entity-set resolver, `scope` param on
   search_corpus + structured_query, `_filter_sql` entity-set predicate (rides
   the F1 whitelist work).
3. **Harness fixtures** (~½ day): `context_fixture` in golden, `scope_error`
   funnel label, WS questions activated in golden v2.
4. **UI wiring** (Phase 5, with the chat page): the app builds real snapshots.
   By then the contract is proven headlessly.

## 8. What this deliberately does NOT do

- No agent→page channel: no filter pushing, no navigation, no mutations, no
  "then filter to just those". Research agent only.
- No client-supplied content: markdown, row data, or rendered text in a
  snapshot is rejected, not stored.
- No live binding: the snapshot never auto-refreshes mid-turn.
- No cross-user context: a snapshot is scoped to the requesting user's session;
  it is never shared thread state.

## 9. Questions for the reviewing agent

1. Is the extensional/intensional split (visible_ids vs filter_spec+total) the
   right cut, or should v1 ship extensional-only (ids, capped) for simplicity
   and add specs later? (My view: specs are needed for "all 47 passing
   filters", which the question corpus demands — but challenge it.)
2. `filter_spec` re-materialization runs app-table queries per request — same
   read-only role as structured_query. Any injection/perf concern beyond the
   whitelist treatment?
3. Deixis v1 rule (§5): is conversational answer-set resolution good enough, or
   does the multi-turn script corpus already argue for tracked answer sets?
4. Should the context line live in the user message (proposed) or a separate
   system-adjacent message? Cache implications favor the user-message tail —
   verify.
5. `scope_error` funnel-label mechanics: detectable purely from
   retrieval_explain + tool args? Any question class where scope is genuinely
   ambiguous and the label would misfire?
6. Does anything here quietly recreate an actor channel? (It must not.)
