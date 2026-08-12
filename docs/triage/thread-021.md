# Thread 21 — triage

> **living triage doc** (opened 2026-08-12, covering the 2026-08-11/12
> session). Thread 21, turns 41–52 (6 exchanges); bundles:
> `.qa-artifacts/runs/turn_000000{42,44,46,48,50,52}_thread_21.json`.
> Prompt state: enumeration-structure rule live (`832458c`).

**Session shape:** list startups → count evaluations → filter to biotech →
rank 4 by promise/risk → recurring themes → red flags. Overall quality:
strong. Context held across all six turns; honesty discipline visible
(turn 48 grounded its ranking in recorded scores explicitly; turn 50
refused to claim "recurring" themes for the two startups whose evaluation
text is not indexed). **Queued: this thread is a candidate multi-turn
script for golden v2 (QUESTION-BANK §1.12 shape).**

## Issues

### 1. Evaluation-coverage semantics unexplainable by the agent
- **Source:** flag, turn 44 — *"this might warrant giving the advisor
  additional background knowledge (like what evaluations cover). Maybe a
  'background info' doc..."*
- **Diagnosis:** asked "how many have pitch deck evaluations", the agent
  gave the honest count (one `structured_query`) plus the caveat that it
  cannot tell deck-based evaluations apart. Platform semantics — that X1
  evaluations are generated from decks/websites/sections — exist nowhere
  the agent can retrieve or cite. Related registry gap: `deck_extract`
  documents are joinable to evaluations, so the deck question is answerable
  with an inventory query.
- **Fix (planned):** (a) small ingested platform-reference document
  (searchable, citable) describing what evaluations are and cover — content
  needs David; (b) consider inventory query extension. Matches
  QUESTION-BANK §3.3 (coverage surface).
- **Status:** David-gated (doc content/scoping).

### 2. Industry/sector questions run as text census — 34 s
- **Source:** flag, turn 46 — *"why this took so long (34s)... is this type
  of industry/sector info not exposed in DB?"*
- **Diagnosis (verified in bundle + registry):** one `scan_text` call, then
  a heavy reasoning step (500 output tokens = the bulk of the latency; the
  scan itself is fast). Root cause: industry labels exist in the app DB and
  on chunk metadata, but `x1_advisor/agent/queries.py` has **no label-based
  registry query**, so a classification question falls back to lexical
  scanning + prose reasoning over excerpts.
- **Fix (planned):** registry-based reusable label resolver —
  `startups_by_label(label_type, value)` across industries/regions/etc.
  (per the standing registry-resolver rule; never a one-off industry
  mapper). Turns the class into a ~4 s cited platform-data answer.
- **Status:** queued (top of list).

### 3. Turn-48 trio: parallelism, cite-through, italics
- **Source:** flag, turn 48 (three parts).
- **(a) Subagents?** No — Tier-1 is a single agent loop by design
  (citations/ACL enforceable in code); deep mode is Track H3, deferred.
  The model DID batch 8 tool calls in one step (bundle, step 1); whether
  Haystack's tool invoker executes a batch concurrently is unmeasured —
  worth checking before optimizing. Note: the 12-tool turn took 17 s, the
  1-tool turn took 34 s — reasoning, not tools, dominates latency. Status:
  open (measure, low priority).
- **(b) Surface the evaluation report's own outbound links?** Possible;
  it is a cite-through policy call: those links are the *report's*
  citations, unverified by the advisor. Clean form = surface them labeled
  as the report's own sources; they must not enter the validated citation
  set. Status: David-gated (policy).
- **(c) Italics not rendering in console.** Confirmed console bug (only
  `**bold**` was handled). Status: **fixed `e9aab3f`**.

### 4. Formatting polish: orphan "1." lists; tables for theme sections
- **Source:** flag, turn 50 — *"repeated lists of '1.' but no '2.' '3.'
  seems awkward... lists of one should be re-imagined... perhaps that whole
  section should be a table...?"*
- **Diagnosis:** model style tic (singleton numbered lists) + a taste call
  (tables for parallel per-entity sections). Both are prompt-level style
  guidance → require David's approval; each prompt change costs a
  run-comparison cycle, so batch them.
- **Fix (planned):** one formatting addendum proposal (singleton numbered
  lists → prose; parallel per-entity analysis → tables), wording to David.
- **Status:** David-gated (batched proposal).

### 5. "Score exists but report text doesn't" reads as contradiction
- **Source:** flag, turn 52 — *"the Kadence bio 'red flag'... refers to a
  chunk of the indexed evaluation... which it seems to think doesn't exist
  (?) First of all, does it? ... some sort of 'scope' confusion?"*
- **Diagnosis (bundle-verified; DB confirmation pending ADC reauth):** not
  agent scope confusion — a corpus-coverage reality stated without enough
  context. Turn 48 cited Kadence's score (68) from the app DB via
  structured query; turns 50/52 said no **indexed evaluation report** —
  and turn 52's Kadence row is explicitly cited to the
  `documents_for_company` coverage query [4] + the profile [5], i.e. the
  agent checked. Both are consistent: the evaluation ROW exists (score
  queryable); its report TEXT was never ingested (test-env bundle drift:
  75/79 test bundles are the experimental shape and are skipped —
  DECISIONS 2026-07-08). The reader-facing failure is that no answer ever
  says the reconciling sentence: "an evaluation exists (score 68), but its
  report text is not in my indexed corpus."
- **Fix (planned):** same family as issue 1 — coverage-disclosure
  semantics: when structured rows show an evaluation but
  `documents_for_company` shows no eval-text documents, the answer should
  state that split explicitly. Candidate behavior obligation for a golden
  case; possibly one prompt sentence (David-gated) once issue 1's
  reference doc lands.
- **Status:** open — pair with issue 1; DB-side confirmation of Kadence's
  eval/doc rows pending reauth.

## Cross-references
- Flags queue: `.qa-artifacts/repl/flagged.jsonl` (latest record per turn wins)
- Related commits this session: `832458c` (enumeration prompt rule),
  `d8c0985` (console tables+collapse), `f48e44a` (flags modal, history,
  source viewer), `e9aab3f` (italics)
- DECISIONS 2026-08-11 (prompt rule, s7 list-inheritance queue)
- Langfuse: project `alpha-claw` (dsv-org), traces tagged release=git sha
