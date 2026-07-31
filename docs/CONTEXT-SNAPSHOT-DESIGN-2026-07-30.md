# Context Snapshot — carrying "what's on screen" into the advisor

> Date: 2026-07-30. Status: **proposal, revised same day** per
> [`QA-BANK-CONTEXT-REVIEW-2026-07-30.md`](archive/QA-BANK-CONTEXT-REVIEW-2026-07-30.md)
> §7 (all requested changes adopted — headline: v1 scopes are **extensional**;
> the original intensional re-materialization contradicted replayability).
> §9 records the resolutions and the subsequent typed-reference refinement
> needed for mixed-type search pages and XRM boards.
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

**P1 — Reference-based, never content-based.** The client sends typed entity
refs and filter provenance, never row data or rendered content. The server
validates and canonicalizes the refs, resolves their current records, and
intersects them with the requester's ACL. It does **not** re-run `filter_spec`
to decide historical membership: the extensional refs are the scope truth.
Reasons: (a) a client cannot spoof source content; (b) payloads stay small; and
(c) source content is fresh when live tools resolve a ref, while the set of
refs remains pinned for replay. The snapshot is a *pointer structure*.

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
expands handles to typed-ref sets. A 200-entity working set costs the prompt ~30
tokens, not ~2,000.

**P5 — Versioned, per-page-type schemas with honest degradation.** `schema: 1`;
page types are an enum with standardized payloads. Unknown page types produce
`context_status: unsupported`, not silent "no context." A non-deictic question
may continue without page scope; a deictic question must report that the scope
was unavailable (§3.1).

## 3. Wire shape — extensional v1

**Both scopes are extensional (explicit typed-reference lists).** The app owns
the filter semantics and must export the complete resolved membership through
the same query builder used for the page. The current app responses expose page
items and counts, not always the full member list, so Gate 3B explicitly adds an
ID-only context-snapshot projection rather than pretending pagination already
provides it. The advisor never re-implements the app's filter semantics — if
app and advisor each interpreted `score_gte`/industry/publication, "passing the
current filters" would eventually mean different things on the page and in the
answer.

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
      "selected": null                          // or {"entity_type":"startup_company","entity_id":17}
    },
    "working_set": {
      "visible_refs": [                          // the page actually rendered
        {"entity_type": "startup_company", "entity_id": 3},
        {"entity_type": "startup_company", "entity_id": 17},
        {"entity_type": "startup_company", "entity_id": 42}
      ],
      "matching_refs": [                         // FULL filtered set — immutable turn scope
        {"entity_type": "startup_company", "entity_id": 3},
        {"entity_type": "startup_company", "entity_id": 17},
        {"entity_type": "startup_company", "entity_id": 42},
        {"entity_type": "startup_company", "entity_id": 88},
        {"entity_type": "startup_company", "entity_id": 91}
      ],
      "filter_spec": {"score_gte": 77, "industry": "healthcare"},
      "filter_contract_version": 1,
      "total": 5,
      "sort": "score desc"
    }
  }
}
```

- **"currently on screen"** → `visible_refs`.
- **"passing the current filters"** → `matching_refs` — resolved by the app at
  snapshot time, canonicalized/deduplicated by the advisor, persisted as the
  immutable scope in the turn bundle, and used as the replay primitive. Never
  re-materialized later: a spec re-run against changed data is a different set,
  which would break replay (review §3.1). The originally requested refs and
  current ACL result may also be retained as forensic fields; neither replaces
  the canonical scope.
- A ref is always the pair `(entity_type, entity_id)`. Numeric ids alone are
  ambiguous across tables. Mixed search results and polymorphic XRM boards are
  therefore representable without inventing one page-wide `entity_type`.
- **`filter_spec` is provenance only** — display, debugging, and app/advisor
  filter-semantics drift checks. It is validated against a server-defined
  whitelist (the same typed-filter registry the F1 fix introduces — one filter
  contract, not two SQL paths) but is never the source of scope truth.
- Refs cost no prompt tokens (P4). The app-side projection has a configured
  `max_inline_scope_refs` and never truncates silently. If the complete set
  exceeds that limit, the authenticated app backend materializes the typed refs
  and returns a server-minted, user-bound `scope_snapshot_id`; the advisor
  resolves that immutable set, rechecks current ACL, and persists the resolved
  typed refs in the turn bundle so replay never depends on the transport token
  remaining live. A live query definition never stands in for a historical
  snapshot.

An `entity_detail` page needs only `page.selected`; a board page's working set
is the typed card-entity list (read-only and potentially mixed-type).
Board/notes content itself remains deferred to the coverage/ingestion work
(historical PLAN Phase 6, not revised Gate 6) — context can scope a search to
board entities today even though note text isn't searchable yet.

### 3.1 Context resolution status

Context resolution is explicit, never silent:

```text
context_status = resolved | absent | unsupported | invalid | stale
```

An unknown page type or failed validation degrades to `unsupported`/`invalid`
— **visibly**. If the question is deictic ("these", "on screen") and context
is anything but `resolved`, the agent must say the page scope was unavailable
rather than silently answering corpus-wide (that would be a self-inflicted
scope error). `absent` + non-deictic question = normal corpus-wide chat.

## 4. How the agent consumes it

1. **Context block — server-authored, delimited, spoof-proof.** `run_turn`
   renders the snapshot to one compact line placed after the stable
   system/tool prefix (so the cache prefix is untouched). A separate
   server-controlled context message is the clean form; if the Haystack message
   path makes that awkward, an explicitly delimited server-authored tail on the
   user message is acceptable **provided**: the client cannot write the
   authoritative context block, the model is told which block is
   server-resolved, and user text mimicking `[Context: …]` cannot override it
   (the server strips/escapes look-alikes from user input). Example:
   `[Context (server-resolved): search page · 3 of 5 matching startups visible
   (filters: score≥77, industry=healthcare) · none selected]`.
2. **Scope handles on tools** — `search_corpus`, `scan_text` (when built),
   and `structured_query` gain an optional `scope` parameter:
   `"selected" | "visible" | "working_set" | "all"` (default `all`). The server
   resolves the handle to an entity-set predicate. The model never enumerates
   ids. `selected` is an explicit scope, not a hidden filter default inside
   tools — "this startup" maps to `scope: "selected"`.
3. **Coverage reporting** — a scoped answer distinguishes page scope from
   eligible/scanned coverage ("5 startups were in the page scope; 3 were
   eligible and scanned"). It may mention restricted counts only when the
   class-disclosure policy permits; otherwise use the policy-approved
   unavailable wording without exposing private existence. This is exactly the
   honesty users demanded in the captured threads ("did you search all 20?",
   "why did you only search their summaries?"). The scan-tool coverage contract
   (QUESTION-BANK §3.2) and this compose naturally.

## 5. Multi-turn semantics (deixis rules)

- Each request MAY carry a fresh snapshot; the newest snapshot supersedes.
- A turn with no snapshot inherits the thread's most recent snapshot (persisted
  server-side with the thread once server-owned history lands — F6). Stored
  thread context is **user-owned thread state** — it belongs to the requesting
  user's thread and is never shared or readable across users.
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
fixtures. The context dimension of every WS-status question becomes runnable
headlessly; compound cases such as `WS+SCAN-A` remain gated by their separately
declared tool capability:

```yaml
- id: g2-041
  question: "Across the startups currently on screen, what risks are mentioned most often?"
  context_fixture:
    page: {type: search}
    working_set:
      visible_refs: [<10 typed fixture refs>]
      matching_refs: [<10 typed fixture refs>]
      total: 10
  expected_scope: {required: visible}
```

Golden v2 gains `context_fixture` and `expected_scope` fields. **`scope_error`
is graded only against explicit golden expectations** — `required: <scope>` or
`allowed: [<scopes>]` for questions that legitimately admit more than one
reading — never inferred mechanically from arbitrary question wording (review
§3.6). Multi-turn scripts (bank §1.12) test the deixis rules directly: UI scope
on turn 1, a narrowed answer set on turn 2, "pull exact quotes for each" on
turn 3, and a fresh snapshot superseding prior scope on turn 4.

## 7. Implementation slices (all small; ordered)

1. **Schema + plumbing** (~½ day): `context` on AskRequest, typed-ref
   validation/canonicalization +
   `context_status`, filter_spec whitelist (shared typed-filter contract from
   the F1 fix — one filter layer, not a second SQL path), server-authored
   context block, persistence of the resolved extensional scope into
   `research_record`.
2. **Scope handles** (~½–1 day): entity-set resolver (`selected`/`visible`/
   `working_set`), `scope` param on search_corpus + structured_query,
   `_filter_sql` entity-set predicate.
3. **Harness fixtures** (~½ day): `context_fixture` + `expected_scope` in
   golden, `scope_error` grading, and activation of the context dimension for
   WS questions without erasing their other readiness dependencies.
4. **App snapshot projection + UI wiring** (Gate 3B, ~½–1 day): reuse the
   app's exact filtered query to return a complete ID-only typed-ref projection
   alongside page results, or a user-bound materialized `scope_snapshot_id`
   above `max_inline_scope_refs`; then wire the chat request to that snapshot.
   By then the contract is proven headlessly. This is a real backend task—the
   current paginated/grouped search responses expose page items and totals, not
   a complete scope.

## 8. What this deliberately does NOT do

- No agent→page channel: no filter pushing, no navigation, no mutations, no
  "then filter to just those". Research agent only.
- No client-supplied content: markdown, row data, or rendered text in a
  snapshot is rejected, not stored.
- No live binding: the snapshot never auto-refreshes mid-turn.
- No cross-user context: a snapshot is scoped to the requesting user's session;
  it is never shared thread state.

## 9. Review resolutions (QA-BANK-CONTEXT-REVIEW-2026-07-30 §3/§7)

1. ~~Extensional vs intensional~~ → **Resolved: extensional v1** for both
   visible and full working sets (`matching_refs`); the original intensional
   re-materialization contradicted replay immutability and would have forked
   filter semantics between Laravel and Python. `filter_spec` demoted to
   provenance. §3 rewritten accordingly.
2. ~~Re-materialization injection/perf~~ → moot (no re-materialization);
   `filter_spec` validation still rides the shared typed-filter whitelist.
3. Answer-set deixis → **conversational resolution stands for v1**; promote to
   tracked state only if the multi-turn scripts show a recurring failure class.
4. Context placement → after the cached prefix; server-controlled message
   preferred, delimited server-authored tail acceptable with the three
   anti-spoof conditions in §4 item 1.
5. `scope_error` → graded only against explicit `expected_scope`
   (required/allowed) golden declarations; never inferred from arbitrary
   wording.
6. Actor channel → reviewer confirmed none exists; the hard rule stands: no
   tool result may become a page command, navigation request, filter mutation,
   or app write.
7. Homogeneous id lists → **Refined after implementation-path verification:**
   scopes carry typed refs so mixed search pages and polymorphic boards are
   representable; Gate 3B owns the app-side complete-membership projection.
