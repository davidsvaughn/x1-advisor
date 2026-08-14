# analyze_scope — budgeted semantic map/reduce over a document scope

> Status: **proposed design** (2026-08-13). Build is gated on demonstrated
> demand per PLAN §R — David's live-testing flags showing questions where
> sampled retrieval was the limiting factor. Named as a future capability by
> QUESTION-BANK §3.2 (the SCAN-A class); this doc makes it buildable on a
> green light. Companion precedent: `scan_text` (exact lexical census,
> shipped 2934f7a) — analyze_scope is its semantic sibling.

## 1. What it is

A TOOL (not an architecture change): the agent calls
`analyze_scope(question, scope, budget)` and receives one bounded result
containing per-document findings extracted by a cheap model that READ each
document in scope IN FULL. The Tier-1 loop, citations, ACL, and cost
enforcement are untouched — this is the `web_research` delegation pattern
plus the summaries map/reduce, aimed at the corpus.

Answers the class the current tools cannot: "read all N evaluations and
tell me the recurring risks" — today answered from top-k samples with
coverage disclosure; with analyze_scope answered from full reads with
per-document citations.

## 2. Tool contract (sketch)

```
analyze_scope(
  question: str,          # the analytical question, verbatim
  scope: {                # same scope grammar as scan_text
    entity_type, entity_ids | all,
    source_types: [eval_premium, eval_basic, eval_section, deck_extract,
                   website, profile],
  },
  per_doc_budget: int = 2000,   # max tokens of findings per document
) -> {
  ref,                    # registered evidence ref (citable as platform data)
  coverage: {docs_read, docs_in_scope, not_indexed_entities},  # honest counts
  findings: [ {document_id, entity_id, title,
               findings: str,          # grounded extraction, ≤ per_doc_budget
               supports: [block_index] # blocks backing each finding
             } ],
  reduction: str,         # cross-document synthesis by the map/reduce reducer
}
```

- **ACL:** scope resolution goes through the same class-predicate filter as
  retrieval — a document the requester cannot retrieve is not read.
- **Evidence:** each per-doc finding registers block-level supports so the
  main agent can cite `(document_id, block_index)` exactly as with
  search_corpus; the reduction itself is generated text and is NOT citable
  (same boundary as record summaries).
- **Coverage honesty:** `docs_read/docs_in_scope` ride the result and the
  answer must disclose them (rule 5/6 machinery applies unchanged).

## 3. Execution

Map: one call per document (whole markdown, windowed at 12k chars exactly
like `ingest/summaries.split_windows`) to the cheap-tier model asking for
question-relevant findings with block indexes; 8-wide pool. Reduce: one
call over all findings. Model: `ADVISOR_ANALYZE_MODEL`, default the E4b
winner (gpt-5.6-luna) at lowest reasoning effort. Budget controls: hard cap
on docs (start: 100), per-doc token budget, per-call soft cost cap rolled
into the turn's $0.50 (a full 50-doc scope ≈ $0.10–0.25 at luna prices;
latency ≈ 20–40 s pooled — acceptable for the explicit "go deep" ask,
disclosed in the answer).

## 4. Grading plan (the expensive half)

- New tool ⇒ TOOL_SCHEMA_SHA256 + prompt guidance (one rule-5-adjacent
  sentence; David-approved wording) ⇒ scoring-contract sever (s7 — batch
  with the queued list-inheritance judge fix and the other deferred s7
  items so comparability is cut once).
- SCAN-A cases in QUESTION-BANK get authored into golden v2 with
  `expected_route: [analyze_scope]`; graded on coverage disclosure +
  faithfulness of findings (judged) + entity recall against a scan_text
  prefilter oracle where the question permits one (truth-robustness note:
  semantic questions get acceptable-evidence groups, not exact oracles).
- Scripts: one multi-turn script where a census turn escalates to an
  analyze_scope turn (bank §1.12 shape).

## 5. Non-goals

No app-controlling actions, no web scope (web_research owns that), no
recursive sub-agents (a finding never triggers more reads), no persistence
of reductions as corpus documents (that is Track H2's research-note
flywheel, with its own cite-through rules).

## 6. Trigger and effort

Build trigger: flagged live threads where the sampled answer was the
limiting factor (the demand signal PLAN requires). Estimated effort: ~1 day
build (tool + scope resolver reuse + pool) + the s7 grading batch it joins.
