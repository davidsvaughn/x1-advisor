# Question Bank — master test-question corpus for X1 Advisor

> Date: 2026-07-30, **revised same day** per
> [`QA-BANK-CONTEXT-REVIEW-2026-07-30.md`](QA-BANK-CONTEXT-REVIEW-2026-07-30.md)
> §7 (orthogonal readiness dimensions; SCAN split into SCAN-T/SCAN-A;
> tool-not-ready reclassifications; curation weighting; tiered golden-v2 plan).
> Synthesized from **13+ places** where David wrote example/test
> questions across x1-link (three checkouts + git history), deepagents-poc, and
> x1-backend, recovered 2026-07-30 (none of it had carried into this repo's docs
> or golden set). This document is the **designated seed for golden v2** (Gate 4
> of `ARCHITECTURE-PLAN-REVIEW-2026-07-30.md`) and the question-side companion to
> `QA-LOOP-DESIGN-2026-07-30.md` (whose `expected_route` + funnel labels these
> questions are annotated for).
>
> Wording is preserved verbatim wherever possible — the phrasing *is* the test.

## 0. Provenance

| Key | Source | Count | Where |
|---|---|---|---|
| P4 | Phase-4 smoke-test suite (2026-05-08) | ~90 | `~/code/x1/deepagents-poc/x1-link/docs/deepagents-poc/PHASE-4-SMOKE-TEST-QUESTIONS.md` (also x1-link history `55ce2b28:docs/deepagents-poc/…`) |
| KS | Knowledge-snapshot plan, "Example User Questions to Design For" | ~46 | `~/code/x1/link/x1-link/old_docs/advisor/design/KNOWLEDGE-SNAPSHOT-IMPLEMENTATION-PLAN.md` §557-638 (also history `37ecfc06`) |
| SCR | David's live-test script (scratch notes) | 5-question script ×2 variants | `~/code/x1/x1-link/scratch.txt:1-30` |
| XRM | XRM-board use cases | 14 | `~/code/x1/x1-link/docs/advisor/pages/XRM.md:328-353` |
| CAP | Advisor-page-capability-layer docs | ~12 | x1-link history `c0cec6e0`/`5e943423`/`245b0cfa` (`SEARCH-PAGE-INTEGRATION-MENTAL-MODEL.md` etc.) |
| EDS | Evaluation-data-strategy routing map | 6 | history `c9a5c854:docs/advisor/EVALUATION-DATA-STRATEGY.md` (+ quarantine copy in `link/x1-link`) |
| BP | Advisor-agent blueprint route mappings | ~5 | `~/code/x1/link/x1-link/old_docs/advisor/design_legacy/ADVISOR-AGENT-BLUEPRINT.md` |
| SKL | search-postgres skill examples | 8+4 | `~/code/x1/x1-link/services/x1-advisor/skills/search-postgres/{SKILL,SNAPSHOT-RETRIEVAL}.md` |
| BRN | Capability brainstorm | 4 | history `8dc2e288:docs/advisor/X1-ADVISOR-CAPABILITY-BRAINSTORM.md` |
| RSA | Research-subagent plan | 3 | `~/code/x1/x1-link/docs/plans/2026-04-28-RESEARCH-SUBAGENT.md` |
| RAG | RAG cheat sheet (report chat) | 3 | `~/code/x1/x1-link/docs/interview/RAG-CHEAT-SHEET.md:22-24` |
| LFT | **28 real captured advisor threads** (actual user turns) | ~100+ turns | `~/code/x1/x1-link/.x1/langfuse-threads/*/TIMELINE.md` |
| GV1 | Golden v1 (agent-authored, 2026-07-08) | 45 | `experiments/golden/v1.yaml` |
| RUB | Eval-pipeline rubric banks (different purpose — see §4) | ~70 | `~/code/x1/x1-backend/sdk/docs/tmp/questions.json` + `investor_test_template*.json` |

**Readiness model** (revised per `QA-BANK-CONTEXT-REVIEW-2026-07-30.md` §4.2 —
the earlier single ✅ conflated "the data exists" with "the current agent can
answer correctly"). Promotion into golden v2 requires orthogonal fields
(`source_available`, `tool_ready`, `scope`, `operation`, `context_required`,
`golden_priority`); this source bank uses compound display tags to expose the
known dependencies without pretending it is already the machine-readable
golden specification:

- **✅** — source *and* tools ready today (corpus content + an existing
  tool/query path).
- **🔧** — source exists but the tool/query path doesn't (missing registry
  query, coverage query, or relationship/aggregate support).
- **WS** — needs working-set/page context
  (`CONTEXT-SNAPSHOT-DESIGN-2026-07-30.md`).
- **SEL** — needs an explicit selected-entity page context.
- **PA** — needs the entity set established by a prior answer/turn.
- **SCAN-T** — needs the exhaustive **text scan** tool (`scan_text`, §3.2A:
  deterministic phrase/FTS over a bounded scope with per-entity coverage).
- **SCAN-A** — needs **bounded semantic analysis** (`analyze_scope`, §3.2B:
  budgeted per-entity judgment + synthesis; heavier machinery, sequenced
  later).
- **NOTES** — needs deferred notes/XRM coverage ingestion (historical PLAN
  Phase 6; not revised Gate 6).
- **CONTRACT** — needs a durable answer-behavior contract and grading rule
  (quotes, scope/coverage disclosure, or abstention).
- **ACL** — needs an explicit persona/disclosure-policy case.
- Tags combine (e.g. WS+SCAN-T).

The compact status is deliberately compound: context alone does not make an
aggregate, coverage query, exhaustive scan, or semantic comparison ready. When
a case is promoted into golden v2, all six orthogonal fields above are required;
the compact tag is not a substitute for that machine-readable contract.

Curation weighting (review §4.1): real captured user turns (LFT) outrank
speculative design examples; repeated copies of one historical list count as
one source, not independent demand; copilot-era questions were re-curated for
research-agent scope.

> Scope note (David, 2026-07-30): the advisor is a **research agent, not an
> actor**. Action-style commands from the earlier copilot-era lists (move cards,
> assign, tag, apply filters to the UI) are deliberately **excluded** from this
> bank.

---

## 1. The master bank

Deduplicated; each entry keeps its best verbatim phrasing. Route abbreviations:
`search` = search_corpus, `sql` = structured_query, `source` = get_source,
`web` = web_research, `hybrid` = sql→search chain.

### 1.1 Single-entity evidence (route: search → source)

| # | Question | Src | Status |
|---|---|---|---|
| 1 | What are the biggest risks mentioned for this startup? | P4 | SEL+✅ |
| 2 | What strengths are highlighted for this startup? | P4 | SEL+✅ |
| 3 | What concerns are raised about the founding team? | P4 | SEL+✅ |
| 4 | What does the evaluation say about market timing? | P4 | SEL+✅ |
| 5 | What traction signals are mentioned in the documents? | P4 | SEL+✅ |
| 6 | What evidence is there that this startup has enterprise demand? | P4 | SEL+✅ |
| 7 | What objections or caveats appear in the evaluation narrative? | P4 | SEL+✅ |
| 8 | What does the evaluator say about defensibility or moat? | P4 | SEL+✅ |
| 9 | What narrative evidence supports this startup being venture-scale? | P4 | SEL+✅ |
| 10 | What rationale is given for the overall evaluation conclusion? | P4 | SEL+✅ |
| 11 | Why did the evaluator give this startup a weak market score? | P4 (wrong-tool) | SEL+✅ |
| 12 | What exact concerns are mentioned about the founder? | P4 (wrong-tool) | SEL+✅ |
| 13 | What language in the pitch deck supports the claim of strong traction? | P4 (wrong-tool) | SEL+✅ |
| 14 | Is there any mention of customer concentration for this startup? | P4 (edge) | SEL+✅ |
| 15 | Are there contradictions between the evaluation and the pitch deck for this startup? | P4 (edge) | SEL+✅ |
| 16 | Summarize the evidence for why this startup might fail. | P4 (edge) | SEL+✅ |
| 17 | What's the team score? / What are the founder's biggest risks? | RAG | SEL+✅ |
| 18 | Compare the traction analysis to the market analysis. | RAG | SEL+✅ |
| 19 | What did the evaluation say about their moat? | CAP | SEL+✅ |
| 20 | What risks did the research identify for VeraAI? | CAP | ✅ |
| 21 | How has this startup improved since last evaluation? | EDS | SEL+🔧 (cross-version comparison; latest/prior-eval semantics §3.5) |
| 22 | Were there any red flags in the traction analysis? | EDS | SEL+✅ |

For CLI testing before selected-page context lands, bind `SEL` cases to an
explicit company fixture/name; do not silently broaden them to corpus scope.

### 1.2 Corpus-wide enumeration — split into exact scan vs semantic analysis (§3.2)

David's signature test — appears in **five** independent sources (KS, SCR, RSA,
SKL, LFT). Top-k retrieval alone cannot answer these honestly. Per review
§4.3 the class splits: "mentions X" is a deterministic **text scan** (SCAN-T);
"has strong/weak Y" is bounded **semantic analysis** (SCAN-A). Never report a
lexical no-match as a semantic negative.

| # | Question | Src | Status |
|---|---|---|---|
| 23 | Which startup evaluations in the database mention regulatory risk? | KS+SCR+SKL+LFT | SCAN-T |
| 24 | Which startup evaluations mention FDA, CE mark, or reimbursement complexity? | KS | SCAN-T |
| 25 | Which startup evaluations explicitly mention weak go-to-market execution? | KS | SCAN-T |
| 26 | Which startups have repeated concerns about capital intensity across multiple evaluations? | KS | SCAN-A |
| 27 | Which startups have strong technical differentiation but weak commercialization plans, according to their evaluations? | KS | SCAN-A |
| 28 | Which startup evaluations mention clinical validation risk in the raw evaluation artifacts? | KS | SCAN-T |
| 29 | Which startups are described as platform businesses rather than single-product companies? | KS | SCAN-A |
| 30 | Which startup evaluations have evidence of patent defensibility? | KS | SCAN-A |
| 31 | Which startup evaluations mention payer adoption or hospital procurement friction? | KS | SCAN-T |
| 32 | Which visible startups have artifact language suggesting execution risk even when the DB summary is vague? | KS | WS+SCAN-A |
| 33 | Which of these startups has at least one founder with a PhD? | SCR | WS+SCAN-T/hybrid |
| 34 | Compare the strongest GTM concerns across these 12 startups. | RSA | WS+SCAN-A |
| 35 | Find evidence of FDA/compliance mentions, then summarize by startup. | RSA | SCAN-T |
| 36 | Do any of these CVs mention FDA, HIPAA, or reimbursement experience? | SKL | WS+SCAN-T |

### 1.3 Working-set scoped (route: hybrid; context plus operation shown separately)

| # | Question | Src | Status |
|---|---|---|---|
| 37 | Across the startups currently on screen, what risks are mentioned most often? | P4 | WS+SCAN-A |
| 38 | Among the startups currently on screen with a score above 80, what risks are mentioned most often? | P4 (compact #7) | WS+SCAN-A |
| 39 | Which startups passing the current filters have pitch decks, and what do those decks say about traction? | P4 (compact #8) | WS+🔧+SCAN-A (coverage then analysis) |
| 40 | For the top 5 highest-scoring startups currently on screen, summarize the main risks in their evaluations. | P4 | WS+SCAN-A |
| 41 | Which currently filtered AI startups mention regulatory risk in their documents? | P4 | WS+SCAN-T |
| 42 | Among the startups in the current results founded in 2024, what concerns are raised about traction? | P4 | WS+SCAN-A |
| 43 | For the startups currently on screen, what evidence conflicts with their current scores? | P4 (edge) | WS+SCAN-A |
| 44 | Which startups currently on screen have no evaluation yet? | P4 (compact #11) | WS+🔧 (coverage query) |
| 45 | Which ones are based in Europe? (follow-up narrowing) | CAP | PA |
| 46 | Which of these has the best traction? / How many of these have been evaluated? | CAP+SKL | (PA or WS)+SCAN-A / (PA or WS)+🔧 |
| 47 | What is the average market score among these? | CAP | (PA or WS)+🔧 (aggregate query) |
| 48 | Which of these seem the most promising? | CAP | (PA or WS)+SCAN-A |
| 49 | Which would a deep-tech investor care about? | CAP | (PA or WS)+SCAN-A |
| 50 | Stay strictly within the current working set and explain your evidence. | KS (directive) | WS |

### 1.4 Canonical/structured data (route: sql)

| # | Question | Src | Status |
|---|---|---|---|
| 51 | What industry is this startup in? | P4 | SEL+✅ |
| 52 | What is the current X1 score for this startup? | P4 | SEL+✅ |
| 53 | What's the market score for StartupX? | EDS | ✅ |
| 54 | Which startups scored above 70 overall? | EDS+SCR ("score over 77") | ✅ |
| 55 | How many startups in the current results have uploaded pitch decks? | P4 | WS+🔧 (doc-inventory query) |
| 56 | What investors are associated with this startup? | P4 | SEL+🔧 (relationship query not in registry) |
| 57 | How many startups are in the database? | e2e smoke | ✅ |
| 58 | Which startups currently on screen were created in the last 12 months? | P4 | WS |
| 59 | Which currently filtered startups are based in Switzerland? | P4 | WS |
| 60 | What's the average market score? | BP | 🔧 (no avg query in registry) |
| 61 | How does VeraAI compare to the average market score? | CAP | 🔧 (depends on #60) |
| 62 | Which fund has the largest committed capital? / Compare fund durations | SKL | 🔧 (no fund queries in registry) |
| 63 | How many CVs are open to work? / Who has the most skills? | SKL | 🔧 (no CV queries in registry) |

### 1.5 Inventory / coverage (route: sql/coverage registry — see §3.3)

| # | Question | Src | Status |
|---|---|---|---|
| 64 | What documents are available for this startup? | P4 (compact #3) | SEL+🔧 (coverage query — §3.3) |
| 65 | Do we have a pitch deck for this startup? | P4 (compact #4) | SEL+🔧 (coverage query) |
| 66 | What evaluation bundles exist for this startup? | P4 | SEL+🔧 (coverage query) |
| 67 | Which document types are searchable for this startup? | P4 | SEL+🔧 (coverage query) |
| 68 | Across the startups currently on screen, which ones have pitch decks? | P4 | WS+🔧 |
| 69 | Which startups passing the current filters have any searchable documents at all? | P4 | WS+🔧 |
| 70 | For the current working set, which startups have both evaluation bundles and uploaded documents? | P4 | WS+🔧 |

### 1.6 Investor / fund / organization (route: search + sql)

| # | Question | Src | Status |
|---|---|---|---|
| 71 | Which investors mention biotech, medtech, or diagnostics focus in their descriptions? | KS | SCAN-T |
| 72 | Which funds seem most aligned with capital-intensive deep-tech deals? | KS | SCAN-A |
| 73 | Which organizations in the database mention acceleration programs for climate startups? | KS | SCAN-T |
| 74 | Which investor profiles mention board involvement or operating support? | KS | SCAN-T |
| 75 | Which investment companies describe late-stage, growth, or crossover behavior? | KS | SCAN-A |
| 76 | Which organizations mention university spinout support? | KS | SCAN-T |
| 77 | Which of these investors emphasize climate or hard-tech themes in their notes? | SKL | (PA or WS)+NOTES+SCAN-A |
| 78 | Which investors in our network focus on climate seed and move fast? | BRN | NOTES+SCAN-A ("move fast" needs notes) |
| 79 | Which of these are angel investors? / Who has the fastest response time? | SKL | (PA or WS)+🔧+NOTES |

### 1.7 People / CV / team (route: search + sql)

| # | Question | Src | Status |
|---|---|---|---|
| 80 | Which visible startups have founders with prior exits? | KS | WS+SCAN-T/hybrid |
| 81 | Which team members mention regulatory or clinical backgrounds? | KS | SCAN-T |
| 82 | Which CVs mention McKinsey, BCG, Bain, or consulting backgrounds? | KS | SCAN-T |
| 83 | Which founders mention synthetic biology, drug discovery, or semiconductor expertise? | KS | SCAN-T |
| 84 | Which startups appear to have teams with unusually strong operator experience? | KS | SCAN-A |
| 85 | Which candidate profiles mention fund formation, LP relations, or investment committee work? | KS | SCAN-T |
| 86 | Which founders have PhDs? | BP+SCR | SCAN-T/hybrid |

### 1.8 Cross-entity / comparative / discovery (route: multi-hop)

| # | Question | Src | Status |
|---|---|---|---|
| 87 | Compare how investors and startup evaluations talk about regulatory risk in medtech. | KS | SCAN-A |
| 88 | Which startups look strong in evaluations but weak in internal notes? | KS | NOTES+SCAN-A |
| 89 | Which companies appear in both positive investor-fit notes and negative execution-risk evaluations? | KS | NOTES+SCAN-A |
| 90 | What patterns show up repeatedly in successful vs unsuccessful climate startups? | KS | SCAN-A |
| 91 | Which fund descriptions overlap most with the themes in our highest-rated startup evaluations? | KS | SCAN-A |
| 92 | What are the most common failure modes across our recent startup evaluations? | KS | SCAN-A |
| 93 | What themes recur in promising AI infrastructure companies? | KS | SCAN-A |
| 94 | What differentiates the top-scoring biotech evaluations from the rest? | KS | SCAN-A |
| 95 | Across all available evidence, what concerns come up most often for first-time founders? | KS | SCAN-A |
| 96 | What evidence suggests strong distribution advantage across portfolio companies? | KS | SCAN-A |
| 97 | What patterns do you see across our portfolio? | EDS | SCAN-A (top-k search gives a *sampled* answer today — must say so) |
| 98 | Which recommendations had the biggest impact for similar startups? | EDS | 🔧 (underlying data contract unclear) |

### 1.9 Internal notes / XRM (compound ingestion, scope, and operation requirements)

| # | Question | Src | Status |
|---|---|---|---|
| 99 | What concerns have teammates repeatedly raised about this startup? | KS | SEL+NOTES+SCAN-A |
| 100 | Which startups have internal notes mentioning poor responsiveness or missed deadlines? | KS | NOTES+SCAN-T |
| 101 | What objections have been logged most often for companies in this pipeline stage? | KS | WS+NOTES+SCAN-A |
| 102 | Which investors has our team described as slow-moving or low-conviction? | KS | NOTES+SCAN-A |
| 103 | Which XRM notes mention a pricing model concern? | KS | NOTES+SCAN-T |
| 104 | Which accounts have multiple notes about procurement friction? | KS | NOTES+SCAN-A |
| 105 | Summarize this pipeline — how many in each stage? | XRM | WS+🔧 (XRM aggregate) |
| 106 | Which entities on this board don't have an evaluation yet? | XRM | WS+🔧 (coverage query) |
| 107 | Compare the entities in Due Diligence — who's strongest? | XRM | WS+SCAN-A |
| 108 | Which boards have applications waiting for review? | BRN | 🔧 (XRM aggregate) |


### 1.10 Evidence-fidelity and scope directives (answer-contract tests)

| # | Directive | Src | Status |
|---|---|---|---|
| 109 | Pull the exact quotes for each startup. | SCR | PA+CONTRACT+✅ |
| 110 | Show me the best evidence (best quotes) for each startup. | SCR | PA+CONTRACT+✅ |
| 111 | Find exact passages supporting the claim that these startups face regulatory hurdles. | KS | (PA or WS)+CONTRACT+✅ |
| 112 | Show me the most relevant excerpts, not just a summary. | KS | CONTRACT+✅ |
| 113 | Search all available evidence, not just the current page. | KS | WS+CONTRACT |
| 114 | Search only full artifact content, not DB summaries. | KS | CONTRACT+✅ |
| 115 | Use the latest evaluation per startup only. | KS | 🔧 (latest-eval filter semantic, §3.5) |
| 116 | Compare current page results with the broader database. | KS | WS+SCAN-A |
| 117 | Summarize the strongest evidence for and against the startups currently visible on screen. | KS | WS+SCAN-A |

### 1.11 Coverage-challenge follow-ups (from real threads — gold for the QA loop)

| # | Follow-up | Src | Status |
|---|---|---|---|
| 118 | Did you search all 20 startups? | LFT | PA+CONTRACT |
| 119 | Why did you only search their summaries? | LFT | PA+CONTRACT |
| 120 | Can you see my CV contents? | LFT | SEL+🔧+ACL (coverage query) |
| 121 | (after a list) …pull the exact quotes for each | SCR/LFT | PA+CONTRACT+✅ |

### 1.12 Multi-turn scripts (test as ordered sequences, not single turns)

**Script A — David's canonical loop (SCR, verbatim):**
1. show all the startups in the database
2. just show the ones with score over 77
3. which of these startups mention regulatory risk in their evaluation?
4. pull the exact quotes for each startup
5. show me the best evidence (best quotes) for each startup

**Script B — 3-question variant (SCR):** score>77 → regulatory-risk mention →
best quotes.

**Script C — P4 compact smoke set (12 questions, P4 §7)** — the fast-CI suite.

**Script D — harvest from LFT threads**: 28 real conversations with turn-by-turn
user messages + expected tool paths recorded in each `TIMELINE.md`.

---

## 2. Testing methodology distilled from David's artifacts

These patterns recur across P4 §8-9, SCR, and the LFT capture apparatus — they
are David's own methodology, and they slot directly into `QA-LOOP-DESIGN`:

1. **Observations-to-record per question (P4 §8)** — first tool called; routing
   stayed on expected path; evidence-vs-inventory confusion; SQL-first ordering
   on hybrids; grounded-vs-vague answer; latency; redundant SQL hops. This is
   the funnel-label set of `QA-LOOP-DESIGN §4.3`, specified fourteen months of
   iterations earlier. `expected_route` per question is not optional in golden
   v2 — every source list annotates routes.
2. **Wrong-tool temptation as a first-class category (P4 §6)** — questions that
   *sound* metadata-answerable but need narrative evidence. Golden v2 needs this
   category; it is the direct test of the `routing_error` funnel label.
3. **Reusable phrasing patterns over named companies (P4 §9)** — "avoid
   overusing a single company name unless you are intentionally debugging one
   specific trace." The anti-test-case-hacking rule, in David's words. Golden v2
   should parameterize entity slots and rotate bindings.
4. **Three-tier suite structure** — (a) compact smoke set (~12, every CI run),
   (b) full graded golden set, (c) real-thread replay corpus (LFT + accumulated
   `advisor.turns`). All three tiers already exist in embryo.
5. **Scripted multi-turn tests** — the canonical loop is filter → narrow →
   mention-search → exact quotes. Tests coreference, working-set carryover, and
   evidence fidelity in one script. Golden v2 needs script-type cases; the
   harness needs to grade a *sequence*.
6. **Coverage honesty is user-visible** — real users immediately asked "did you
   search all 20?" and "why did you only search their summaries?" (LFT). The
   answer contract must state scope searched; the coverage-aware synthesis
   requirement in KS (§"Pattern B") anticipated exactly this.
7. **Iterate-until-clean loop (SCR)** — "test again, 4 questions, analyze and
   evaluate, fix problems, repeat… until everything looks good" — the teacher-QA
   loop, hand-run. The QA-loop tooling industrializes this exact cadence.

## 3. Design/architecture implications

Surfaced by reading the bank against the current implementation:

### 3.1 Working-set context is a missing first-class concept
The single most common phrasing across every era of David's lists ("currently on
screen", "passing the current filters", "the current working set", "this
startup") has **no representation** in the current advisor. Gate-3B integration
must carry immutable typed refs for the visible and full matching sets (or a
materialized `scope_snapshot_id`) into `/ask`, exposed to tools as an optional
entity-set filter. Typed refs are required because search pages and XRM boards
may contain multiple entity types. Without this, roughly a third of the bank is
untestable and the product misses its most natural usage mode. (Also needed for
directive #113/116 "current page vs broader database".)

### 3.2 Top-k retrieval cannot answer bounded enumeration — and the fix is TWO capabilities, not one

"Which of these 50 mention X?" requires examining *all 50* with a per-entity
verdict, not the top-8 chunks. KS already specified the coverage requirement:
report which documents matched *and which did not*, "in the evidence contract,
not as prompt fluff." Per review §4.3 the original single `scan_corpus` idea
conflated two different operations:

**A. `scan_text(scope, query_or_phrases)`** — deterministic exhaustive text
scan (FTS/phrase) over a bounded scope →
per-entity `matched | no_match | not_indexed | restricted`, exact matching
passages with citations, and eligible/scanned/matched coverage counts. Cheap
(mostly SQL); answers the SCAN-T class and makes follow-ups #118-119 honestly
answerable. **Disclosure note:** the per-entity `restricted` status reveals
that gated material *exists* for that entity — it must follow the same
class-disclosure policy as the gated-vs-absent note (recommendation:
premium-class existence only; never private-doc existence). `restricted` may
exist in the internal/admin result, but the user-facing renderer maps it to the
policy-approved gated/unavailable/omitted form. Eligible/scanned counts exclude
undisclosable private sources, so totals cannot reveal them indirectly.

**B. `analyze_scope(scope, question_or_rubric, limits)`** — bounded semantic
map/reduce: per-entity evidence gathering, per-entity verdict with citations,
aggregate synthesis, eligible/analyzed/insufficient-evidence counts, under
explicit entity/document/token/latency/cost budgets. Answers the SCAN-A class.
Real machinery — sequence it after `scan_text`, on demonstrated demand.

**Honesty rule (both):** never translate a lexical no-match into a semantic
negative. "No matching phrase was found in the indexed eligible text" is the
correct claim; "they have no regulatory risk" is not.

### 3.3 The coverage model gets a product surface
P4's inventory questions (#64-70) are user-facing queries over exactly the
coverage registry proposed in review point S3 (indexed / gated / stale /
not-indexed / doesn't-exist). That registry is not just internal bookkeeping —
it needs a query path (structured_query or dedicated tool).

### 3.4 Evidence-fidelity mode (answer contract, per review §4.4)
"Pull the exact quotes" / "excerpts, not a summary" (#109-112) is a stable
answer-contract semantic. When invoked: use **original source blocks only —
never record-summary chunks**; return verbatim spans via get_source; cite
every quote; state the searched scope and coverage; and distinguish
unavailable vs restricted vs no-match sources. Belongs in the system prompt
contract as a durable product rule, guarded by a golden case and the
`answer_contract_error` funnel label.

### 3.5 Latest-eval-only semantics (#115)
Version-and-append handles document supersession, but "latest evaluation per
startup" is a *query-time* semantic across sibling eval documents. Needs an
explicit filter (e.g. `latest_eval_only: true`) rather than hoping dedup
approximates it.

### 3.6 Out-of-scope requests get a graceful decline
The advisor is a research agent, not an actor (scope decision, 2026-07-30).
Users habituated to copilot UIs will still occasionally ask it to change things
in the app; the durable behavior is a graceful decline plus the analysis that
would inform the action — never a hallucinated "done." One golden behavior case
guards this; no action capabilities are in scope.

### 3.7 The rubric banks (RUB) are judge vocabulary, not test questions
`questions.json` / `investor_test_template*.json` (~10 investigative questions ×
7 sections) define what a *good evaluation* covers. Their use here: dimension
vocabulary for the faithfulness/completeness judge when grading synthesis-class
answers (#90-98), not test inputs.

## 4. Relationship to golden v1 → v2

Golden v1 (GV1) covers entity-lookup, cross-doc, filtered, and person classes
well, with real hard negatives — keep all 45. Its gaps are exactly this bank's
strengths: exact-scan + semantic enumeration (§1.2), working-set (§1.3),
inventory (§1.5), wrong-tool temptation, directives (§1.10), coverage
challenges (§1.11), and multi-turn scripts (§1.12).

Golden v2 **curates** the bank — it does not copy it (review §4.5/§6.5). Three
tiers:

1. **Smoke** (~12, deterministic, every CI run) — seeded from the P4 compact
   set.
2. **Core** (~40–60 + 3–5 scripts, decision-grade) — each case precise before
   any expensive agent run: required facts/behaviors, acceptable evidence
   groups, allowed/required routes (`expected_route`) and scopes
   (`expected_scope`), context fixtures, ACL persona where relevant, and a
   deterministic-vs-stochastic grading rule.
3. **Extended** (~80–100 + real-thread replay corpus) — breadth, paraphrase
   variants (entity-slot parameterization), exact-scan vs semantic-analysis
   cases kept separate.

Real captured threads (LFT) weight above speculative design examples; a
question repeated across historical copies counts once. Do not jump from the
bank straight to a 100-case agent run — precision in the core tier first.
