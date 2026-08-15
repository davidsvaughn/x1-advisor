# Evidence Recall Across Turns — design (PROPOSED)

**Date:** 2026-08-15
**Status:** PROPOSED — not built. Awaiting David's go.
**Trigger:** thread-037 turn 88 — "show me a few of the most notable results"
re-ran the entire analyze_scope census ($0.086, 47s) to display findings the
prior turn had produced 30 seconds earlier, because follow-up turns can see
prior *prose* but not prior *evidence*.
**Lineage:** this is the "stable handles" prescription of the
[2026-05-08 Advisor Context Engineering Plan](refs/2026-05-08-ADVISOR-CONTEXT-ENGINEERING-PLAN.md)
(written for the previous Python deep-advisor; its observed failure modes —
"follow-up turns reconstruct the active startup set from prose memory",
"verbose tool payload replay" — are exactly what thread-037 exhibited),
applied to the current advisor's citation-validated world. Background:
[Manus webinar notes](refs/manus-context-engineering-transcript.md),
[LangChain Deep Agents context engineering](refs/langchain-deepagents-context-engineering.md).

## The gap, precisely

Two deliberate decisions intersect badly:

1. **History is text-only** (context discipline): `advisor.turns` stores
   user/assistant prose; tool payloads never replay into later turns. Right
   call — the stock carry-everything chat pattern makes every follow-up pay
   for every prior tool result forever.
2. **Evidence is per-turn** (citation validity): refs are validated against
   an evidence registry built fresh under the current turn's ACL. Right
   call — replayed stale results could cite superseded or ACL-revoked
   content.

Consequence: a follow-up must re-earn all evidence. Cheap when evidence was
one search; expensive now that analyze_scope censuses cost real money.
Neither stock pattern fixes this: Haystack/OpenAI message-replay is
profligate and validation-blind; our current design is safe but amnesiac.
The middle path is **selective recall on demand, with revalidation**.

## Design

One new tool, no schema changes elsewhere, no new storage (the research
record already persists everything needed).

### Tool: `recall_evidence`

- **Args:** `turn_id` (optional; default = the most recent assistant turn in
  this thread that registered chunk evidence).
- **Source:** that turn's `research_record` — findings (title,
  entity/evaluation ids, finding text, `(document_id, block_index)`
  supports), coverage block, and the non-citable reduction.
- **Revalidation (the citation-boundary rule):** every recalled support is
  re-fetched through the SAME ACL-bearing chunk query the live tools use
  (current principal, current corpus). A support whose document is
  superseded, deleted, or no longer ACL-visible is dropped; survivors are
  re-registered in THIS turn's evidence registry as fresh refs. The tool
  reports `recalled`, `dropped_stale`, and the source turn id. Recalled
  evidence is therefore exactly as citable as freshly-fetched evidence —
  because it IS freshly fetched; only the *selection* is remembered.
- **What terra sees:** compact findings list + original coverage (labeled
  with the source turn) + staleness note. Zero LLM calls inside the tool;
  one DB round trip. Expected follow-up cost: ~$0.01–0.02, ~5–8s
  (terra steps only).
- **Scope guards:** same thread only; principal must match the recorded
  `acl_resolved` or the ACL re-resolves under the current principal (the
  revalidation query enforces the live one regardless — the guard just
  avoids confusing cross-user recalls if threads are ever shared).

### Prompt/tool-description guidance (needs the usual schema-hash update)

analyze_scope/search results in a prior turn + a follow-up that refers to
them ("those results", "show me examples", "which of them...") → call
`recall_evidence` FIRST; re-run the census only if recall reports the scope
has changed (`dropped_stale` high) or the question genuinely needs new
reads.

### Non-goals (v1)

- No cross-thread recall, no multi-turn merging (recall one turn's record).
- The recalled reduction stays non-citable (unchanged boundary).
- No proactive injection into history — recall is a tool the agent chooses,
  so quiet follow-ups ("thanks") pay nothing. This is the plan's "retrieve
  full detail on demand through stable handles", not carry-forward.

### Coverage honesty

A recalled census answers with the ORIGINAL coverage disclosure plus the
recall provenance ("findings recalled from turn N's census of 45
evaluations; 0 dropped as stale"). The count still comes from
`coverage.relevant_evaluations` — mechanical, never synthesized.

## Costs & risks

- New tool → TOOL_SCHEMA_SHA256 churn + one more tool in the cached prefix
  (~150 tokens/turn). QA-trio must grade recall-flavored follow-ups (add to
  the owed heavily-loaded batch).
- Staleness UX: if the corpus rebuilt between turns, recall may drop many
  supports; the disclosure covers it, and the agent falls back to a re-run.
- Risk of over-recall (agent recalling when a fresh search is right): the
  description's "re-run if scope changed" line plus QA watch-item.
