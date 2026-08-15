<!-- doc-id: 2026-05-08-advisor-context-engineering-plan -->

# Advisor Context Engineering Plan

**Date:** 2026-05-08  
**Scope:** `services/x1-deep-advisor-python/` Search-page parent agent and document-evidence workflow  
**Primary references:**
- [DeepAgents Context Engineering](../reference/deepagents/DEEPAGENTS-DOCS/CONTEXT-ENGINEERING.md)
- [DeepAgents Customization](../reference/deepagents/DEEPAGENTS-DOCS/CUSTOMIZATION.md)
- [Manus transcript notes](../reference/manus/TRANSCRIPT.md)
- [Document Evidence Workflow Design](./DOCUMENT-EVIDENCE-WORKFLOW-DESIGN.md)

---

## Why this plan exists

The current Python advisor already has better internal document retrieval than it had in earlier phases, but the parent agent still carries too much unnecessary context between turns.

The most recent Langfuse thread, `x1-user-36-advisor-1778265705461-9c6bc62d9a5f88`, is a useful concrete example. It shows both the strengths of the current document workflow and the remaining context-engineering problems this plan is meant to fix: verbose tool payload replay, entity/name drift risk across follow-ups, and unnecessary fan-out on multi-startup evidence questions.

Observed failure modes:

- Parent turn history accumulates medium-sized tool payloads that are too small to trigger DeepAgents' built-in offloading, but large enough to bloat follow-up turns.
- The system prompt still carries generic DeepAgents/coding-agent baggage that is irrelevant for the Search-page advisor.
- Follow-up turns reconstruct the active startup set from prose memory instead of a compact, authoritative set representation.
- Multi-startup evidence questions can fan out into many per-entity tool calls instead of one set-oriented call.
- The document tool returns both the user-facing answer and too much retrieval/debug payload (`evidencePreview`, `searchPlan`, `searchArtifactPaths`) into parent history.

This plan applies context-engineering principles directly to those issues:

- **Offload** heavy or reconstructible context out of message history.
- **Reduce** the context that stays inline.
- **Retrieve** full detail on demand through stable handles.
- **Isolate** heavy evidence work inside the document workflow.
- **Cache** by keeping the parent prompt and recurring context more stable.

---

## Core principles for this advisor

### 1. Keep the parent agent thin

The parent should route, track the current working set, decide between page action / taxonomy / SQL / document evidence, and produce the final user response. It should not carry raw evidence blobs, retrieval plans, or generic coding-agent instructions.

### 2. Return compact, reversible tool outputs

If a tool result can be reconstructed from artifacts, state, or a stable handle, the parent should receive the compact form only.

### 3. Represent entity sets explicitly

The active result set should be represented in structured frontend context, not re-inferred from prior natural-language turns.

### 4. Batch set-oriented evidence work

Questions about multiple startups should be answered through one set-oriented document workflow call by default. Per-entity fan-out should be an internal implementation detail only when needed.

### 5. Preserve user-facing grounding while hiding internal IDs

IDs remain internal for routing, retrieval, and entity stability. User responses should refer to names, not numeric IDs.

---

## Current-state diagnosis

### Parent prompt assembly is still too noisy

`agent.py` currently uses `create_deep_agent(... system_prompt=BASE_SYSTEM_PROMPT ...)`, which means the custom prompt is layered on top of DeepAgents base prompt behavior rather than replacing it outright (`services/x1-deep-advisor-python/src/x1_deep_advisor/agent.py`). That leaves generic planning/filesystem baggage in the parent runtime even after some tool exclusions.

### Frontend context is too weak for stable set reuse

`format_page_capability()` currently exposes `idsSample`, `filtersSummary`, `sort`, and a `readableDataManifest`, but not a compact authoritative `id -> name` map or a compact reusable working-set descriptor (`services/x1-deep-advisor-python/src/x1_deep_advisor/context.py`).

### Document tool output is too verbose for parent history

`answer_document_question_tool` currently returns JSON containing:
- `answer`
- `citations`
- `evidencePreview`
- `searchPlan`
- `searchArtifactPaths`
- `caveats`

That contract is defined in `tools/agent_tools.py`, and the actual payload is assembled in `document_workflow/graph.py`.

### Isolation is only partial

The document workflow already isolates retrieval, ranking, and synthesis internally, which is good. But it still returns too much of its internal retrieval state back to the parent, weakening the isolation benefit.

### DeepAgents built-ins are not firing at the right granularity

DeepAgents offloads large tool inputs/results once they cross its threshold and summarizes later when context approaches the model window. Our problem is cumulative growth from many medium-sized payloads, so we need to compact earlier and more intentionally instead of expecting automatic rescue.

---

## Target architecture

### Parent layer

The parent agent should operate on:

- one compact, advisor-specific base prompt
- one compact page-context block
- a small stable tool surface
- compact tool result summaries

The parent should keep enough information to:

- understand the current page and working set
- know when to call `apply_search_intent`
- know when to call `list_allowed_values`
- know when to call SQL vs document evidence
- know the current entity set by ID internally and by name for user-facing language

It should **not** keep:

- long evidence previews
- search planning internals
- artifact-path lists unless explicitly needed later
- generic file-editing / todo-planning prompt instructions

### Document-evidence layer

The document workflow should continue to own:

- document discovery/materialization
- zap search and range expansion
- evidence ranking
- internal synthesis
- detailed retrieval artifacts

The workflow should return a compact parent-facing object and offload any richer payload to artifacts/logging.

---

## How the retrieval handle works

This section is the worked example that Sections C and D both depend on. The goal is to make the retrieval-handle mechanism concrete enough that "compact output with handle" is unambiguous when implementers reach those sections.

### Core idea

A "handle" is a stable string identifier that points to content stored *outside* the model's message history, in the virtual filesystem (DeepAgents `StateBackend`, scoped to the thread). The parent's context only carries the small string. The heavy payload lives at the path the string points to. On follow-up, a tool dereferences the handle to read the content back.

This is "free" in Manus's sense: the identifier already exists naturally — file path for file ops, URL for browser ops, byte range for evidence spans. We are not inventing a new ID system; we are keeping the natural one and dropping the inline payload.

### Walkthrough

**Turn 1** — user asks: *"What risks are mentioned in the evaluations of these 5 healthcare startups?"*

Internally the document workflow:

1. Runs zap searches → writes JSONL to `/artifacts/zap-search-abc123.jsonl`
2. Ranks/expands → writes aggregate evidence to `/artifacts/document-evidence-abc123.json`
3. Returns a compact response to the parent:

```json
{
  "answer": "Three startups flagged reimbursement risk; two flagged regulatory uncertainty…",
  "citations": [
    {"entityName": "Acme Health", "range": "/artifacts/sys-doc-startup-42-uuid.json:1234:1678"},
    {"entityName": "BetaCare",   "range": "/artifacts/sys-doc-startup-43-uuid.json:980:1422"}
  ],
  "evidenceHandle": "/artifacts/document-evidence-abc123.json",
  "caveats": []
}
```

That blob is on the order of 400 tokens. The actual evidence might be 30K tokens, sitting safely in the artifact file.

**Turn 2** — user asks: *"Show me the actual quote behind the BetaCare reimbursement risk."*

The parent's history carries:

- the compact JSON above (with citation `range` strings and the top-level `evidenceHandle`)
- its prior `answer` text

The parent calls a resolver — either `answer_document_question_tool` again with `prior_handle="/artifacts/document-evidence-abc123.json"`, or a dedicated `expand_evidence(range)` tool. Either way the resolver:

1. Reads `/artifacts/sys-doc-startup-43-uuid.json` from the virtual filesystem
2. Slices bytes 980–1422, or runs `zap --range path:980:1422 --window 500` to widen the window
3. Returns the expanded excerpt

The parent never re-runs the original search. It never re-ingests the 30K-token evidence blob. It just dereferences a 60-character string.

### Two flavors of handle

Both are first-class:

1. **Aggregate evidence handle** — one per document-tool call. Points to the full ranked-evidence JSON. Useful when the user asks something the parent could not anticipate ("what else did you find?", "summarize for the other 3 startups").
   - Format: `/artifacts/document-evidence-<id>.json`
   - Field: top-level `evidenceHandle` in the tool output

2. **Byte-range citation handles** — one per cited span. Points to a specific byte window in a specific source document. Useful for "show me the actual quote" or "expand around citation 2."
   - Format: `/artifacts/sys-doc-startup-43-uuid.json:980:1422`
   - Field: `range` on each citation
   - These are reversible in Manus's sense: zap's byte offsets are stable against the original file, so `--range path:980:1422 --window 1000` always returns the same expanded window. The handle is not a one-time token; it is a coordinate.

### Why this beats keeping the data inline

- **Turn 1**: parent receives ~400 tokens instead of ~30K.
- **Turn 5**: parent's history accumulates ~400 × 5 = 2K tokens of compact tool output, instead of 150K of replayed payloads.
- **Cache friendly**: the parent's prefix (system prompt + tool defs + early turns) does not shift just because evidence got large. The KV cache stays warm across turns.
- **Reversible** in Manus's sense: no information is *lost*, only externalized. The model can still get to it via a tool dereference.

### What this implies for implementation

You already have most of the mechanism — the workflow already writes `/artifacts/zap-search-*.jsonl` and citations already carry `range` strings. What is missing today:

1. **Aggregate `evidenceHandle`** does not exist yet. The implementation note in [DOCUMENT-EVIDENCE-WORKFLOW-DESIGN.md](./DOCUMENT-EVIDENCE-WORKFLOW-DESIGN.md) records `evidenceArtifactPath` as currently `null` because no aggregate evidence artifact is written. The implementation should write it and surface it as `evidenceHandle`.
2. **An expansion-side tool** to dereference handles. Either polymorphic (`answer_document_question_tool` with a `prior_handle` parameter) or dedicated (`expand_evidence`). Polymorphic is preferable for tool-surface minimalism.
3. **Drop the noise** (`evidencePreview`, `searchPlan`, `searchArtifactPaths`) since the single `evidenceHandle` subsumes their replay value, and the per-citation `range` strings preserve drill-down.

The mental model: **inline content is wrong by default; handles are right by default; tools are how you trade a handle for content when you actually need it.**

---

## Required modifications

## A. Trim parent prompt surface (do not replace BASE outright on day one)

### Goal

Remove generic coding-agent instructions from what the parent model actually sees, while preserving DeepAgents scaffolding the parent legitimately uses.

### Background — why not full BASE replacement first

DeepAgents documentation explicitly recommends *tweaking* the base prompt via `system_prompt=` and `HarnessProfile` overrides rather than replacing the BASE prompt outright. The BASE prompt teaches the model how to use harness primitives — `read_file`, `grep`, `ls`, `task` (subagent), and others. The parent already excludes `write_todos`, `write_file`, `edit_file`, and `glob` via `_ToolExclusionMiddleware`, so the most-irrelevant baggage is already stripped from the *tool* surface. The remaining BASE content mostly explains tools the parent might still need (for example, `read_file` to inspect an artifact during expansion).

Replacing BASE with `HarnessProfile.base_system_prompt` is a heavier hammer. It is justified only after we know the parent does not need the scaffolding for the remaining built-ins.

### Conservative trim (not replacement)

1. Keep `system_prompt=BASE_SYSTEM_PROMPT` layering in place.
2. Tighten `BASE_SYSTEM_PROMPT` itself: remove anything that overlaps with what the DeepAgents BASE prompt already covers (filesystem mechanics, task-tool mechanics) and keep only Search-advisor-specific routing language.
3. Expand `PARENT_AGENT_EXCLUDED_TOOLS` if any built-in tool is confirmed unused after the trim (candidates: `task` if subagents are not used, `ls` if no artifact inspection happens at the parent layer).
4. Use `HarnessProfile.tool_description_overrides` to compress descriptions of remaining built-ins the parent uses but does not need verbose docs for.
5. Disable the general-purpose subagent if it is not intentionally used.
6. Confirm no per-turn mutation of tool descriptions or system prompt prefixes (cache-stability check — see Section F).

### Conditional follow-up — full BASE replacement

If post-implementation trace evidence shows the remaining BASE content is still meaningful turn-history bloat *and* the parent does not exercise any built-in that BASE explains, replace BASE with `HarnessProfile.base_system_prompt`. Do this as an instrumented decision, not a default.

### Expected result

- No `write_todos` / filesystem-write guidance in the parent system prompt.
- Smaller, more stable prompt prefix for KV-cache reuse.
- Less irrelevant tool-selection pressure on the parent model.
- BASE-replacement decision is informed by traces, not assumed.

---

## B. Strengthen frontend context into a compact working-set contract

### Goal

Make the active startup set explicit, compact, and reusable across turns — without giving the model a partial sample it will mistake for the full set.

### Changes

1. Extend the Search page capability payload to include a compact `idToName` map for the current working set sample/window.
2. Tag the `idToName` map with an explicit `samplePolicy` field so the model treats it as bounded ground truth, not the whole working set. Suggested shape:
   ```json
   {
     "idToName": {"42": "Acme Health", "43": "BetaCare"},
     "samplePolicy": {
       "policy": "first_n_visible",
       "sampleSize": 20,
       "totalCount": 137
     }
   }
   ```
   When `sampleSize < totalCount`, the parent must not answer name-of-id-X questions for IDs outside the sample by guessing. It must route to a tool that resolves names (SQL or document workflow).
3. Add a compact structured representation of the current working set that survives follow-up questions better than prose.
4. Keep `filtersSummary`, but heavily compress or drop long industry-path expansions in follow-up context.
5. Preserve internal IDs in context for tool routing only.
6. Preserve the rule that user-visible answers should use names, not numeric IDs.

### Expected result

- The model stops guessing which names map to which IDs.
- The model treats the `idToName` map as bounded — it does not invent names for IDs outside the sample.
- “these startups”, “the current results”, and similar follow-ups resolve against structured state.
- Fewer wrong pairings and less need to re-derive the active set from history.

---

## C. Redesign the document tool output contract around compaction

### Goal

Return only what the parent actually needs, while keeping rich evidence reconstructible through stable handles.

See [How the retrieval handle works](#how-the-retrieval-handle-works) for the worked example this section assumes.

### Changes

1. Define a **compact parent-facing output** for `answer_document_question_tool`, limited to:
   - `answer`
   - compact `citations` (each with a byte-range handle)
   - short `caveats`
   - **required** `evidenceHandle` — a stable path to the aggregate evidence artifact (e.g. `/artifacts/document-evidence-<id>.json`)
2. Make `evidenceHandle` **required**, not optional. It is the single re-entry point for follow-up turns. Without it, drill-down questions either re-run zap from scratch or the model fabricates.
3. Remove from the parent-visible payload:
   - `evidencePreview`
   - `searchPlan`
   - `searchArtifactPaths`
   - any other retrieval-debug material not required for immediate user response
4. Persist richer debug/retrieval payloads to artifacts or observability records (Langfuse) instead.
5. Keep citations durable and reversible via stable artifact/range handles. Each citation's `range` (e.g. `/artifacts/sys-doc-startup-43-uuid.json:980:1422`) is itself a handle — usable directly by `zap --range path:start:end --window N` for span expansion.
6. **Update `ANSWER_DOCUMENT_QUESTION_DESCRIPTION` in the same change.** The current description enumerates the returned fields verbatim (`Returns JSON with answer, citations, evidencePreview, searchPlan, searchArtifactPaths, and caveats`). If the contract changes but the description does not, the model will read fields that no longer exist and may cite or surface them. Description-and-contract drift is a class of bug worth blocking with a test (see Section G).
7. Add a small expansion-side surface so the parent can drill into prior evidence without re-running the workflow. Two viable shapes:
   - **Polymorphic**: extend `answer_document_question_tool` with an optional `prior_handle` parameter; when set, the workflow reads the prior aggregate artifact instead of re-resolving from scratch.
   - **Dedicated**: add an `expand_evidence(handle_or_range)` tool that resolves a handle and returns an expanded excerpt.
   The polymorphic shape keeps the parent's tool surface minimal, which is preferable per the Manus rule of thumb (~30 tools max, fewer is better).

### Expected result

- Smaller parent turn history.
- Better alignment with Manus-style reversible compaction: nothing is lost, only externalized.
- Follow-up "show me the quote" or "expand citation 2" questions resolve through a tool dereference, not a re-search.
- No loss of debuggability — full evidence remains recoverable off-path via observability and artifact files.

### Important constraint

Do **not** solve this by making the parent re-open raw artifacts during normal answering. The normal path should remain: internal workflow retrieves/synthesizes, parent receives a compact grounded result. The handle is for follow-up drill-down, not for the parent to digest evidence inline as a substitute for synthesis.

---

## D. Make multi-entity document questions set-native

### Goal

Handle “these startups” or any explicit multi-startup evidence request in one workflow call by default — and ensure the *output* of that call is also set-shaped and bounded.

### Why a set-shaped output schema matters

One parent call for N startups solves the call-count problem at the parent layer, but it can quietly re-create the bloat problem inside the synthesized response. A 30-startup synthesis can produce a 25K-token answer that becomes the next turn's bloat source — and DeepAgents' built-in offloading only fires above the 20K-token tool-result threshold, so this kind of payload sits just under the safety net.

The fix is a bounded set-shaped output schema. Manus's "schemas as contracts" principle: if the workflow knows it is producing a set, it should produce a set with a hard budget per element, with overflow going to artifact.

### Changes

1. Treat multi-entity evidence questions as set-oriented at the parent routing layer.
2. Pass the full target entity set to one `answer_document_question_tool` call.
3. Keep any per-entity subdivision internal to the document workflow when necessary.
4. Define a bounded set-shaped output schema for multi-entity evidence answers:
   - `answer`: short set-level summary (e.g. ≤ 250 chars)
   - `entityFindings`: array of `{entityName, bullets[≤3], citations[≤2]}` keyed by name (not ID)
   - `evidenceHandle`: required, points to the aggregate evidence artifact for follow-up drill-down
   - hard caps: max ≤ 200 chars per bullet, max ≤ N entities in `entityFindings`; spillover entities go to artifact only and are referenced by count in `caveats`
5. Add explicit tests that:
   - a multi-startup question produces one parent document-tool call, not N sibling calls
   - the output respects the per-entity char/bullet caps
   - spillover entities are surfaced in `caveats` (not silently dropped)
6. Ensure the parent can summarize set-level results without leaking internal IDs.

### Expected result

- Lower tool-call count.
- Lower repeated context per turn.
- Multi-entity answers are bounded by schema, not by model whim.
- More predictable follow-up behavior for “all/current/these startups” evidence questions.

---

## E. Tighten hybrid SQL → document routing around structured set handoff

### Goal

When structured selection is required before evidence retrieval, pass the selected set cleanly between workflows — and minimize how much SQL output leaks into parent history.

### Approach: internal hybrid resolution

The document evidence workflow design ([DOCUMENT-EVIDENCE-WORKFLOW-DESIGN.md](./DOCUMENT-EVIDENCE-WORKFLOW-DESIGN.md)) target is for `resolve_documents` to invoke `answer_database_question_tool` *internally* — Manus's "share memory by communicating" pattern via shared workflow state. SQL output never reaches the parent. That is what we are implementing. We do not ship a parent-orchestrated transitional state.

### Changes

1. Implement hybrid resolution as an internal node inside `resolve_documents`. When the question requires entity selection (e.g. "top 5 healthcare startups"), `resolve_documents` invokes `answer_database_question_tool` directly and consumes its output as workflow state, not as a parent-visible tool result.
2. Standardize the SQL tool output with an explicit `selectedEntityIds` field for downstream nodes to consume; the parent never sees this output during hybrid flows.
3. Do not pass `previewRows` forward into document resolution — only IDs and names are needed.
4. Keep parent-orchestrated SQL→document flow available for non-hybrid cases (a structured question that *might* be followed by an evidence question), but do not use it as the default for inherently hybrid questions.

### Expected result

- Hybrid flows become deterministic and contained inside the workflow.
- SQL pre-query output never bloats parent history.
- Better routing for prompts like “for the top 5 healthcare startups, what risks are mentioned in their evaluations?”

---

## F. Use DeepAgents context-engineering features deliberately, not passively

### Goal

Make the harness work for this advisor instead of hoping defaults happen to fit.

### Changes

1. Keep DeepAgents offloading/summarization enabled.
2. Verify and enforce **prefix stability** for KV-cache reuse:
   - System prompt content does not change per turn (no per-request mutation, no dynamic injection that varies between turns of the same thread).
   - All tool descriptions are static (no dynamic schema generation per request).
   - `format_page_capability()` output is stable when the page state is stable; transient fields like `lastActionResult` either stay short or are gated behind a stable condition.
   - Add a regression test that asserts the assembled system prompt + tool schema JSON for a fixed input is byte-identical across two consecutive runs.
3. Use stable artifact handles and compact output schemas so DeepAgents retrieval/offload has better primitives to work with.
4. Defer the summarization tool middleware (`create_summarization_tool_middleware`) until the contract changes have landed and post-implementation traces are captured. If compaction is right, summarization should be vestigial. Add it only if traces show long threads still hitting the 85% summarization trigger.
5. Do not rely on summarization as the first line of defense; compact tool outputs earlier.

### Expected result

- Better use of built-in context engineering.
- Stable KV-cache prefix → measurable cache-hit rate on Anthropic input caching.
- Fewer runaway long-turn failures.
- Less need for emergency context reduction after the fact.

---

## G. Update tests to enforce context-discipline behavior

### Goal

Make context engineering part of the contract, not an informal preference.

### Add or update tests for:

1. **Prompt surface tests**
   - parent prompt no longer contains generic coding-agent/todo/filesystem baggage
   - parent tool surface remains intentionally small
   - assembled system prompt + tool schema is byte-identical across two consecutive runs for the same input (cache prefix stability)

2. **Context-format tests**
   - page context includes compact `idToName` with an explicit `samplePolicy` field
   - when `sampleSize < totalCount`, model behavior does not invent names for IDs outside the sample
   - long filter/taxonomy expansions are compressed
   - no user-facing response template includes numeric IDs by default

3. **Tool-contract tests**
   - document tool compact output excludes `evidencePreview`, `searchPlan`, and `searchArtifactPaths`
   - document tool compact output includes a non-null `evidenceHandle` (required, not optional)
   - citations remain present and well-formed, each with a byte-range handle
   - richer retrieval payload is still persisted somewhere recoverable
   - **`ANSWER_DOCUMENT_QUESTION_DESCRIPTION` matches the actual returned schema** (regression test for description-vs-contract drift)
   - follow-up "expand citation" / "show the quote" requests dereference an existing handle rather than re-running zap

4. **Batching tests**
   - multi-startup evidence requests produce one parent tool call
   - multi-entity output respects per-entity char/bullet caps
   - spillover entities are surfaced in `caveats`, not silently dropped
   - hybrid SQL→document flows hand off a structured entity set without replaying SQL `previewRows` into the document call

5. **Trace-oriented verification (concrete budgets)**
   - Define a fixed multi-turn evidence script (e.g. "show 5 healthcare startups, ask about risks, drill into one citation, ask about a different subset"). Capture the baseline turn-by-turn input-token count today as the regression boundary.
   - Set a concrete budget the suite enforces — for example, turn-3 input tokens ≤ 50% of the current baseline, total thread input tokens for the 5-turn script ≤ 60% of baseline. Tune from the actual baseline.
   - No entity/name drift in multi-turn evidence conversations (assert that the same entity is referred to by the same name across turns).
   - With prompt-caching enabled, the prefix should hit cache from turn 2 onward.

---

## Implementation

All work below ships together — there are no intermediate releases. Order is dependency-driven: contract changes first (steps 1–8), measurement next (step 9), conditional follow-ups last (steps 10–11).

### Steps

1. **Trim the parent system prompt** (Section A): tighten `BASE_SYSTEM_PROMPT` to Search-advisor-specific content only; expand `PARENT_AGENT_EXCLUDED_TOOLS` for any built-in confirmed unused; add `tool_description_overrides` for remaining built-ins; disable the general-purpose subagent if unused.
2. **Working-set contract** (Section B): extend Search page capability with compact `idToName` and explicit `samplePolicy`; compress filter/taxonomy expansions; preserve internal IDs in context, names in answers.
3. **Compact document tool output** (Section C): restrict `answer_document_question_tool` output to `answer`, compact `citations` (with byte-range handles), short `caveats`, and a **required** `evidenceHandle`. Drop `evidencePreview`, `searchPlan`, `searchArtifactPaths` from the parent-visible payload.
4. **Aggregate evidence artifact** (Section C, "How the retrieval handle works"): write the aggregate evidence JSON at `/artifacts/document-evidence-<id>.json` and return its path as `evidenceHandle`.
5. **Expansion-side tool surface** (Section C): add a polymorphic `prior_handle` parameter to `answer_document_question_tool` (preferred) or a dedicated `expand_evidence(handle_or_range)` tool. Make follow-up "show me the quote" / "expand citation N" requests dereference an existing handle.
6. **Tool description in lockstep** (Section C): update `ANSWER_DOCUMENT_QUESTION_DESCRIPTION` so its enumerated return fields match the new schema. Add a regression test for description-vs-contract drift.
7. **Bounded set-shaped output** (Section D): for multi-entity questions, return `entityFindings` keyed by name with per-entity char/bullet caps; spillover entities surface in `caveats`. Treat multi-entity routing as set-native at the parent layer (one tool call, not N).
8. **Internal hybrid resolution** (Section E): move SQL-then-document hybrid resolution into `resolve_documents` as an internal node. SQL pre-query never touches parent context. Standardize an explicit `selectedEntityIds` field on the SQL output for internal consumption; do not pass `previewRows` forward.
9. **Prefix stability and tests** (Sections F + G): assert byte-identical assembled prompt + tool schema across runs; add the contract-vs-description test; add per-entity cap tests; capture today's turn-by-turn baseline token counts and set concrete budgets (turn-3 ≤ 50% of baseline, total thread ≤ 60%).
10. **Conditional — full BASE replacement** (Section A): with the new traces in hand, decide whether remaining BASE-prompt bloat justifies replacing it via `HarnessProfile.base_system_prompt`. Default to "no" unless trace evidence shows otherwise.
11. **Conditional — summarization tool middleware** (Section F): only enable `create_summarization_tool_middleware` if post-implementation traces show summarization still firing at the 85% trigger. If compaction is right, summarization should be vestigial.

### Done criteria

- Parent system prompt is visibly smaller, advisor-specific, and stable across turns (cache-friendly prefix).
- Trace shows stable `id → name` grounding across follow-ups; no hallucinated names for IDs outside the sample.
- Parent no longer receives `evidencePreview` / `searchPlan` / `searchArtifactPaths`.
- Follow-up "show me the quote" questions dereference the handle, not re-run zap.
- Multi-entity evidence questions produce one parent tool call with bounded per-entity output.
- SQL pre-query for hybrid evidence questions never reaches the parent.
- Concrete token budgets met against the captured baseline.
- BASE-replacement and summarization-middleware decisions are documented yes-or-no with trace evidence, not defaults.

---

## What not to do

- Do not replace DeepAgents first.
- Do not fix one trace by adding query-specific prompt wording.
- Do not move more retrieval work back into the parent model.
- Do not expose numeric IDs in user-facing answers.
- Do not depend on summarization alone to clean up avoidable parent-history bloat.
- Do not keep retrieval-debug payloads inline just because they are useful during development.
- Do not replace the DeepAgents BASE prompt before establishing what the parent actually uses from it. Trim first, replace only with evidence.
- Do not return the retrieval handle as optional or transient. Without it, follow-up drill-down has nowhere to land.
- Do not let the model treat a sampled `idToName` map as the full set. Always tag it with sample policy.
- Do not change the document-tool output contract without updating the tool's description in the same change.

---

## Verification plan

### Code-level verification

- unit tests for prompt assembly and context formatting
- unit tests for compact document-tool outputs
- unit tests for set-native multi-entity routing
- targeted tests for hybrid SQL→document set handoff

### Trace-level verification

Use representative Search-page sessions and confirm:

1. Parent prompt is compact and advisor-specific.
2. Multi-turn follow-ups keep a stable working-set representation.
3. Multi-startup evidence questions batch into one parent call.
4. Parent history no longer accumulates long retrieval/debug payloads.
5. Token counts for later turns are materially lower than the current bloated traces.

### Product-level verification

Confirm the user-visible behavior still feels stronger, not weaker:

- answers remain grounded and concise
- citations remain durable
- follow-up evidence questions stay coherent
- current-result-set references work reliably

---

## Highest-leverage subset

If priorities have to shift mid-implementation, these three items deliver the most value per unit of churn:

1. **Trim the parent prompt** — tighten `BASE_SYSTEM_PROMPT`, expand `PARENT_AGENT_EXCLUDED_TOOLS` where safe, add `tool_description_overrides`.
2. **Compact `idToName` working-set context** with an explicit `samplePolicy` field so the model treats it as bounded ground truth.
3. **Compact document-tool output** with a **required** `evidenceHandle`. Drop `evidencePreview`, `searchPlan`, and `searchArtifactPaths`. **Update `ANSWER_DOCUMENT_QUESTION_DESCRIPTION` in the same change.**

These three together attack both context bloat and entity/name drift, which are the dominant failure modes in the current trace.
