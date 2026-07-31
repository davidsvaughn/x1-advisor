# Decisions

> Dated, evidence-backed decisions per [`PLAN.md`](PLAN.md) §1 — bake-off outcomes and
> engineering choices land here, newest first. Each entry names its evidence (spike
> output, manifest path, or doc reference) and the revisit trigger if one exists.

## 2026-07-31 — Gate 4 build (golden v2.0): steps 1–4 landed

David authorized the build (Golden v2.0 §9 steps 1–4 + Track H1) after the
2026-07-31 handoff. Four of the five build deliverables are in; the H1 runner
and the v2.0 baseline run are not yet done. Evidence: commits `32b33c4`
(schema+compiler), `6f6d049` (truth builder + checkers), `8f6b569` (authoring),
`9fffdda` (script runner); suite digest via
`uv run python -m experiments.cases --golden v2`.

- **Suite:** 56 cases + 4 scripts (60 gate units, 13 script turns); 7 smoke
  from the P4 compact set, 53 core. Grading modes split 31 capability / 29
  honesty. Every unit carries provenance; a test asserts it.
- **Behavior obligations are NOT deterministic** (deviation from the §4 sketch,
  adopted here): `correct_premise`, `state_absence`, `surface_ambiguity`,
  `surface_conflict`, `decline_action`, `ask_clarifying`,
  `disclose_capabilities` moved to their own `grade.behavior` block, judged
  against a targeted rubric. Filing "did it correct the false premise?" under a
  block named `deterministic` would be the same overclaim the suite exists to
  catch. Evidence fidelity stays mechanical — a quoted span is or is not in the
  evidence.
- **`blocked_on` added to the readiness model:** the route cannot always imply
  the blocker. The inventory class routes through `structured_query`, which
  exists, while the coverage query inside it does not (bank §3.3), so
  `tool_ready: false` must name what it waits on or it is a mood, not a field.
- **Corpus facts that constrain Gate 4** (measured, not assumed): only 25 of 64
  company names have evaluation documents — 39 test startups have none — so a
  corpus-wide enumeration's honest denominator is 25, not 50; 9 company names
  exist as BOTH a prod fixture and a test entity, so every truth set records
  two denominators; the investor/organization profiles are near-empty of
  thematic language, making those three cases honesty tests with verified-empty
  oracles rather than recall tests.
- **Two authored cases are traps the corpus handed us:** bank#28 ("clinical
  validation risk") has a verified-empty oracle while five companies discuss
  clinical validation — reporting the lexical no-match as a semantic negative
  is precisely the bank §3.2 failure; bank#86 ("Which founders have PhDs?")
  exists because `PhD` matches nothing in the CVs while `Ph.D` matches three.
- **Deferred, needs David:** the §6 prompt-injection canary requires planting
  an instruction-shaped document in the test corpus — a corpus write and a
  Gate 2 fixture. Not done unilaterally. A test asserts it is the only missing
  class.
- **First live signal (v2s004, 2 turns, $0.023):** the cross-turn machinery
  reads correctly (5 documents searched, 5/5 entities carried, no intruders)
  and the script fails on a declared assertion — the answer listed the
  startups but never stated the scope it searched. Coverage disclosure is
  measurable now, and currently absent.

## 2026-07-31 — Headless Claude Code agents: adopt at the edges, never the Tier-1 loop

David reviewed the helm pattern (alpha-claw: `claude -p` runner, settings-scoped
sandbox, proposal-only power boundary, subscription-billed) and asked whether it
fits x1-advisor. Assessment + adoption design:
[`CC-AGENTS-DESIGN-2026-07-31.md`](CC-AGENTS-DESIGN-2026-07-31.md); PLAN §R
Track H. **All three adoption paths approved (David, 2026-07-31).**

- **Not Tier-1**: latency (minutes-shaped harness vs 8.7s mean turn), per-user
  ACL inverts the single-principal sandbox model, the Gate 1D snapshot/replay
  machinery requires owning the loop, and subscription usage defeats `cost.py`
  metering. The Haystack loop stays the spine.
- **H1 — QA-side agents (now):** nightly golden runs + funnel triage,
  truth-set rebuilds, calibration prep, held-out batch execution (Gate 4's
  "separately authorized evaluation service" in near-term form),
  second-agent reviews. David-seat subscription — legitimate (dev tooling).
- **H2 — research-note flywheel (after v2.0 baseline):** scheduled deep
  research ingested as `research_note` documents. Policy set here:
  **cite-through or no ingest** (notes carry their own validated citation
  trail — generated text never launders into bare "evidence", per the Gate 1B
  lesson) and **max-restrictive ACL inheritance** from source evidence.
- **H3 — Tier-2 deep mode (on demand):** headless agent over MCP tools
  wrapping `build_tools` under the requesting principal; Agent SDK + API
  billing in production; turn-bundle adapter is the named QA work item.

**Billing rule (standing): subscription backs David-seat dev work only;
multi-user/production paths run API billing through `cost.py`.** Distinct from
E4a (Claude as *model* via API in the existing loop), which stays a bake-off.
Revisit trigger: if the QA machinery ever decouples from loop ownership, or a
vendor ships metered multi-seat subscription APIs.

## 2026-07-31 — Gate 1B: the evidence boundary holds, and answer quality is finally measured

All five 1B items landed (`12e9bee`, `593ff23`, `4561bb2`, `362703b`, `40240cb`).
Full agent suite, 20 golden questions, corrected corpus
(`experiments/runs/2026-07-31_agent_v1_40240cb_r1.jsonl`):

| | before (`bef0bd0`) | after (`40240cb`) |
|---|---|---|
| citation resolvability | 73/73 (100%) | 72/72 (100%) |
| **citations on record summaries** | **28 of 73 (38%)** | **0** |
| zero-citation answers | g014, g020 | **none** |
| faithfulness | *not measurable* | **0.584** |
| citation coverage | *not measurable* | **0.813** |
| cost/turn | $0.0083 | $0.0084 |
| retrieval recall@10 / MRR (golden v1) | 0.833 / 0.746 | 0.847 / 0.759 |

The headline is the row that used to be invisible. Resolvability was 100% before
and after and told us nothing; underneath it, 38% of citations pointed at
generated summaries and two whole answers cited nothing at all. Both are now zero,
at unchanged cost per turn.

**What the judge says about the answers themselves** (294 cited claims):
186 supported, 78 partial, 30 unsupported, and 66 factual claims carrying no
citation. 19 of 20 questions carry at least one judge label. So the honest
statement is: the *evidence plumbing* is now correct, and the *answers* have a
real quality gap that was previously unmeasurable. That gap is the work, and it
is now visible.

**Read 0.584 as a lower bound with a shape, not a number.** Faithfulness counts
only full entailment; `partial` — supported in weaker terms than claimed — is the
largest failure mode at 78 of 294, and the judge errs strict (calibration: 8/10,
kappa 0.64, both errors partial→unsupported, zero false clean bills). The judge
is also stochastic: the same turn scored 0.83 and 0.62 on two runs because claim
decomposition varies, so per-question scores are noise and the suite aggregate is
the unit.

**Calibration is `synthetic-only` and that is a real limit.** 10 known-answer
mutation cases prove the judge is not broken; they do not prove it agrees with a
person on ambiguous text. No faithfulness number should be quoted as established
until ≥30 human labels exist (`judge_calibrate --sample N` emits real pairs).

**Judging costs more than answering:** $0.0321/question against a $0.0084 turn.
Budget the QA loop accordingly — this is not a rounding error, and it argues for
judging tiers (smoke every run, full suite on gates) rather than always-on.

**Golden v1 barely exercises the new platform-data path** — exactly 1 of 72
citations. The aggregate/list question class (g023/g024) is thin, so Gate 4 must
add real coverage or 1B-4 stays effectively untested.

## 2026-07-30 — Gate 1A complete: the QA loop can now see, and it found three things

All four Gate-1A items are in (`e19d8e5`, `bef0bd0`, `f90e37f`, plus manifest
immutability from Step 0.4 `4d5e1da`):

- **Fingerprints** (`x1_advisor/fingerprint.py`) — git sha, worktree_dirty,
  source-tree hash when dirty, prompt sha, tool-schema sha, index config, corpus
  watermark (hashes every live document's `content_hash`), agent model, filter and
  ACL policy versions. Also closes **F4**: CI now pins the tool schemas as well as
  the prompt, since the cached prefix is both.
- **Retrieval explain** (`retrieve(..., explain_out=…)`) — per call: query,
  compiled filters + notes, forensic ACL, both legs with ranks, the full fused list
  with `rrf`/`dense_rank`/`lex_rank`/`granularity`, drop reasons, returned ids. Side
  channel only; zero tokens. `Hit` gained `granularity`.
- **Turn bundles v2** (`research_record`, `x1_advisor/agent/bundle.py`) —
  request/principal/forensic-ACL, fingerprint, summary + verdict, steps, the full
  verbatim message list, retrieval explains, `raw_answer`, validation, scores with
  `faithfulness: null` so a bundle never implies a judge ran.
- **Owner-only storage** — `.qa-artifacts/runs/` (already gitignored) created
  `0700`/`0600` with `O_EXCL` and an opt-in retention window defaulting to *keep
  everything*. `experiments/runs/` now holds only body-free projections; the split
  and the legacy-artifact caveat are in `experiments/runs/README.md`.

**Agent suite rerun on the current corpus** (20 golden questions, admin):
73/73 citations resolvable (100%), mean **$0.0083**/turn, total $0.1666, p50 7.8 s /
max 13.7 s, zero-citation answers `g014` and `g020`. Manifest
`experiments/runs/2026-07-30_agent_v1_bef0bd0+dirty_r1.jsonl` (47 KB, body-free);
bundles under `.qa-artifacts/runs/2026-07-30_agent_v1_bef0bd0+dirty_r1/` (852 KB).

### What the instrument found on its first run

1. **38% of "resolvable" citations point at generated text.** 28 of the 73
   citations, across 16 of the 20 questions, resolve to `record_summary` chunks
   (`block_index` 10000) — LLM-written summaries *about* a document, not source
   evidence. The 100% headline was counting them as citable sources. This is the
   Gate-1B evidence-boundary correction, now measured rather than asserted, and it
   sets the bar the fix has to clear: record summaries retrieval-only, with
   expansion to the source blocks they summarize.
2. **Hybrid retrieval is dense-only for most questions.** Across golden v1 the
   lexical leg returns **zero** rows for 21 of 35 questions and ≤1 for 23. Cause
   confirmed in SQL: `websearch_to_tsquery` ANDs every stemmed term, so a
   natural-language question matches almost nothing — *"What does the X1 Pipeline
   premium report identify as key uncertainties?"* → `'x1' & 'pipelin' & 'premium'
   & 'report' & 'identifi' & 'key' & 'uncertainti'` → 4 chunks, while *"X1 Pipeline
   key uncertainties"* → 213. It is not dead weight when it does fire: 25 returned
   hits across 11 questions came from the lexical leg alone. **Deliberately not
   fixed** — tuning retrieval before the judge exists is the trap the plan warns
   about. Gate 5 (query preprocessing for the lexical leg: OR semantics or keyword
   extraction) with the golden set as the referee.
3. **Record summaries dominate the candidate pool too**, not just the citations —
   34 of 77 fused candidates on a typical broad query. Whatever Gate 1B does about
   citability, the ranking effect is a separate question.

### Review pass on Gate 1A (second agent, same day) — four gaps closed (`5feb699`)

The reviewer was right on all four, and one was mine to be embarrassed about:

1. **The storage split only covered agent mode.** The retrieval writer still put
   source titles in commit-eligible manifests — 350 of them, 30 naming premium
   reports, in a manifest committed *after* the contract was written. Writer
   migrated; that manifest withdrawn and reproduced body-free. (Disagreed on one
   sub-point: internal `document_id`/`chunk_id` need no pseudonymizing — they name
   nothing and resolve only against a database whose holder can read it all
   anyway. Titles do name their source, which is the actual defect.)
2. **The fingerprint missed in-place change** — a metadata correction or a re-embed
   of the same `chunk_id`s moved retrieval without moving the watermark. Now digests
   chunk text+metadata and embedding vectors, memoized behind a `max(xmin)` sentinel
   (~100 ms typical, ~1.6 s only when something actually moved).
   `experiments/fingerprint_probes.py` proves it: three in-place mutation classes,
   each rolled back, each moving the expected digest. Also captured
   `agent_model_resolved` — we ask for `gpt-5.1`, the provider serves
   `gpt-5.1-2025-11-13`, and only the alias was reaching the manifest.
3. **ACL positive controls covered one class** while the docstring claimed all.
   Each class is now checked for existence and admin reachability; premium, private
   and unpublished pass, **`hidden_eval` is SKIPPED — no such chunk exists on test,
   so that class remains unverified anywhere.** Gate 2 needs a seeded fixture.
4. **`g020` distorted the retrieval score.** Both numbers now reported and stored:
   ALL CASES **0.833 / 0.746** (comparable with earlier manifests), ROUTE-VALID ONLY
   **0.857 / 0.767**.

Left open deliberately: the gated-note existence-disclosure conflict — see
PLAN §5.1(d). It is a product decision, not a defect, and harmless while the pilot
is admin-only.

## 2026-07-30 — Step 0 complete: all five immediate fixes landed and verified on test

PLAN §R Step 0 done, one commit per fix, each verified live against x1-db-test before
moving on. Retrieval behavior is unchanged end-to-end: golden v1 after all five fixes is
**recall@10 0.833 / MRR 0.746, 28/36 full recall, 4/36 zero recall** — identical to the
2026-07-08 baseline (`experiments/runs/2026-07-30_active_v1_e72ef89+dirty_r2.jsonl`).

| Fix | Commit | Evidence |
|---|---|---|
| Structured-query ACL | `711de6f` | live leak reproduced then closed (below); `acl_probes` PASS |
| Typed filter layer (F1/F7) | `382b687` | hostile keys rejected; agent turn 4/4 citations, cache intact |
| Connection pool (F2) | `e72ef89` | 4/4 `runtime_probes`; 3 concurrent `/ask` → distinct threads |
| Immutable manifests | `4d5e1da` | `O_EXCL` + sha + sequence; `test_manifests_never_overwrite` |
| Persist `raw_answer` (F5) | `37684a0` | `research_record->>'raw_answer'` populated |

**The structured-query leak was live, not theoretical.** `queries.py` read app tables with
no ACL at all. On test: 5 unpublished (draft) startup companies have 8 evaluations, and
`evaluations_for_company` / `top_startups_by_score` returned their names and scores to any
caller. Fixed as a class, not per-query — `run_query` now takes a required `acl` and every
query applies the retriever's own predicates (drafts owner-only; hidden evaluations
admin-only with no owner carve-out). Before: `evaluations_for_company('Animafelix')` → 2
rows for anyone. After: nobody → 0, admin → 2, owner → 2.

**Three findings worth carrying forward:**

1. **Golden g020 was never a retrieval miss.** It filters on `fundraising_round`, a field
   the corpus does not stamp on chunks, so pre-fix it returned 0 hits and scored 0 recall
   — one of the four "zero-recall questions" in the 0.833 headline was a routing bug.
   `filter_error` is now its own class in the manifest. Gate 4 decides: stamp entity
   attributes onto profile chunks (a re-index) and add the field to the filter registry,
   or move the case to `structured_query`, which already answers it.
2. **Zero-citation answers score as perfect citation resolvability.** Two of three
   consecutive live turns retrieved 8 evidence blocks and cited none (0/0 = 100%). The
   same question that cited 4/4 still does, so this is the `evidence_unused` class, not a
   regression — and it is precisely the blind spot S1's faithfulness judge closes.
3. **Fixture-derived documents are ACL-stamped open regardless of their prod entity's
   state.** `run_db_mode` correctly propagates `s.is_published` / `e.is_visible`, but
   `run_fixtures_mode` hard-codes `is_published=True, eval_is_visible=True,
   entity_id=None` (the prod entity does not exist on test). 227 corpus documents have a
   NULL entity link, so the draft/owner carve-out cannot apply to them. Not a live leak —
   but any ACL measurement over fixture documents is optimistic, and golden v2 grades
   against that corpus. Gate 2 should either stamp fixtures with their prod ACL state or
   exclude them from ACL scoring.

Also: `structured_query` answers carry zero citations by construction (rows are not
registered as evidence), so that whole question class trivially satisfies the citation
bar. Gate 1B's judge work should decide whether structured rows become citable evidence.

## 2026-07-09 — Record-summary blocks land: recall@10 0.778 → 0.833 (best lever so far)

Phase-1 leftover closed (`ingest/summaries.py`): one gpt-5-mini record-summary chunk per
live document (`granularity='record_summary'`, block_index 10000, ACL inherited), 412
embedded. One-time cost **$0.20** (reasoning_effort=minimal + 8-way pool; serial
default-effort was ~20s/doc — 10× slower). Golden v1 after: **recall@10 0.833 (+0.055),
MRR 0.746, full recall 28/36, zero-recall 4/36** — a bigger lift than the E2 reranker
(which was a wash), at one-tenth the E2 evaluation cost. E4b note: gpt-5-mini at minimal
effort is the working record-summary model.

Also tonight: multi-turn history discipline (last-5 verbatim + gpt-5-mini condense;
verified follow-up coreference), default-on cost ledger, gated-vs-absent messaging
(class+count only; hidden evals never revealed), FastAPI service skeleton (+Dockerfile/
cloudbuild, trigger NOT wired), first unit tests incl. the §9 prompt-prefix-stability CI
assertion (SYSTEM_PROMPT hash pinned; 5/5 passing).

## 2026-07-08 — E2 reranker bake-off: RRF-only WINS (reranker adds nothing at this scale)

First Phase-3 bake-off decided. Candidate `jina-reranker-v3` (blend 0.3·rrf + 0.7·rerank
over the fused top-40) vs the RRF-only baseline, golden v1, config `te3s_1536_ck1`:

| metric | RRF-only | + jina-reranker-v3 |
|---|---|---|
| recall@10 | 0.778 | 0.792 |
| MRR | 0.727 | 0.718 |
| full recall | 25/36 | 26/36 |
| zero recall | 5/36 | **same 5** |

Manifests: `experiments/runs/2026-07-08_active_v1{,_rerank}.jsonl`. A wash — exactly the
outcome the plan flagged as "genuinely plausible at this corpus size". The five failures
are unchanged (they're tool-selection/data problems, not ranking problems), and the
reranker adds a dependency, latency, and free-tier 429 rate limits.

**Decision: v1 ships RRF-only.** Runner-up/fallback: `jina-reranker-v3` blend — the slot
is implemented (`retrieve(rerank=True)`, harness `--rerank`), so re-evaluating after the
prod cutover (~2× corpus) or after golden v2 grows harder is one flag. Voyage rerank
candidates join when `VOYAGE_API_KEY` lands.

## 2026-07-08 — Phase-4 exit measurements: citations 100%, ACL probes PASS

**Agent-mode golden run** (`experiments.run --agent`, 20 questions, manifest
`experiments/runs/2026-07-08_agent_v1.jsonl`): **63/63 citations resolvable (100% —
bar was ≥95%)**, zero dropped refs; cost mean **$0.011/turn**, p50 $0.0096, max $0.05
(total $0.23 for the run); latency p50 10s, max 42s.

**Seeded ACL probes** (`experiments/acl_probes.py`): adversarial queries per gated
class under a no-rights persona and a purchaser persona → **zero violations**, and the
positive control (purchaser sees their purchased premium doc) passes.

**Instrumentation catch #5:** all four "filtered" list questions returned false
"nothing found" answers — the model passed `entity_type: "startup"` but the metadata
enum is `startup_company`, so every search matched nothing. Fixed in the tool contract
(values enumerated + common aliases normalized); re-test returns a cited 8-source list
at $0.009. Lesson reinforced: enum-valued tool params must enumerate their values in
the description.

Remaining for Phase-4 close-out: Langfuse tracing, multi-turn history discipline,
record-summary blocks (gpt-5-mini), "gated vs absent" messaging distinction.

## 2026-07-08 — Phase 4 first slice: agent runs end-to-end; context discipline verified live

**Shipped** (`x1_advisor/agent/`): citation layer (`evidence.py` — tiny refs, post-
validator resolves/dedupes/renumbers/drops; stats now distinct-emitted/resolved/dropped),
structured-query registry (`queries.py`, 5 read-only queries — the only SQL surface),
compact-by-construction tools (`tools.py` — snippets flagged `_truncated`, `get_source`
escalation, bounded web searcher), agent assembly (`advisor.py` — gpt-5.1 Haystack Agent,
byte-stable system prompt, max 8 steps, $0.50 soft cap, threads/turns persistence), CLI
(`ask.py` — prints a per-step usage table on every run).

**Context discipline (David's priority) verified on live runs:** prompt prefix caches
(cached tokens climb per step while uncached input stays flat ≈ tool-result tail only);
per-turn costs: corpus cross-doc $0.024, aggregate $0.004, corpus+web $0.049 — inside
the plan's §3 envelope. Per-step usage tables are printed on every ask.py run.

**Issues found by instrumented sample runs, all fixed:**
1. Sibling eval bundles yield near-identical chunks as distinct documents → retrieval
   now dedupes by text hash post-RRF.
2. `web_research` returned bare ref ids → model (correctly) omitted citations entirely;
   now returns attributable (ref, url, title) triples → 6/6 resolvable web citations.
3. Unbounded web searcher: 250s/$0.079 turn → `max_output_tokens=1200` + concise
   instructions → 36s/$0.049.
4. gpt-5.1 skipped web search on a "right now" question (known conservative triggering)
   → trigger conditions written into the tool description (stable product semantics).

**ACL probe:** non-purchaser asking for premium report content gets zero premium
evidence and an honest miss (retriever-level filter, not prompt-level). Phase-5 nicety:
distinguish "gated" from "absent" in the message. Still open for Phase-4 exit: 20-golden-
question end-to-end run (≥95% resolvable citations), seeded probe suite, Langfuse
tracing, record-summary blocks, multi-turn history discipline (last-5-verbatim+summary).

## 2026-07-08 — Phase 2 landed: hybrid retrieval + golden set + baseline (recall@10 0.778)

**Shipped:** embedding index registry (`x1_advisor/index.py`, config `te3s_1536_ck1` =
text-embedding-3-small/1536d/ck1, ACTIVE; 7,281 vectors embedded for **$0.066** total);
hybrid retrieval (`x1_advisor/retrieval.py` — pgvector cosine + `websearch_to_tsquery`
FTS → RRF(k=60) → per-document cap 3; plain SQL/psycopg, framework-independent; ACL is a
mandatory retriever-level argument with class predicates — private-doc exclusion, draft
owner-only, premium purchase-gating — verified live: non-admin loses gated premium
chunks); golden set v1 (`experiments/golden/v1.yaml`, 45 questions incl. hard negatives
+ 9 web-required); harness (`experiments/run.py` → JSONL manifests under
`experiments/runs/`).

**Baseline (2026-07-08_active_v1, 36 graded):** mean recall@10 **0.778**, MRR **0.727**,
25/36 full recall, 5 zero-recall, median latency ~420ms/query. Known failure modes, all
expected: aggregate questions (g023/g024 — they need Phase-4 `structured_query`, not
retrieval), one metadata-filter value mismatch (g020 fundraising_round), tiny-doc miss
(g012 website 140 chars), person-semantics miss (g031). These are the E2 reranker's and
Phase-4 tools' baselines to beat. Fixture company names fixed en route (gen-1 name lives
at `inputs.company.startup_companies_row`; gen-0a has none → filename-slug fallback).

## 2026-07-08 — Phase 1 ingestion slice landed on test; test-env drift documented

**Shipped** (`x1_advisor/ingest/`): eval-bundle backfill (`backfill_evals.py`), entity
profile renderer (`profiles.py` + `render_profiles.py`, field lists from
ReportChatService recon), chunker v1 (`chunker.py`, `ck1`: `# Page N`/heading/paragraph
blocks, stable block_index, char_span verified), ACL-stamped chunks (`store.py`,
version-and-append with per-section identity). **On test: 412 live documents / 6,728
chunks** — 270 eval-derived (25 premium [gated], 28 basic, 196 sections, 16 decks
[paged, page=slide], 5 website) + 142 profiles (50 startups incl. team, 75 CVs,
14 investors, 3 orgs). Idempotent re-runs verified (all `unchanged`). Never-index
list applied at ingest (emails, tokens, invite fields, lat/long). Still open from
Phase 1: LLM record-summary blocks (`granularity='record_summary'`, gpt-5-mini via
generator registry).

**Bundle contract (live-verified):** FOUR original generations in prod
(`gen-0b`, `gen-0a`, `gen-1`, `gen-2` — see `bundles.py` docstring) all parse; the
parser targets prod's shapes because the agent ships against prod.

**Test-env drift (David asked; confirmed 2026-07-08):**
1. **75 of 79 test eval bundles are an EXPERIMENTAL shape** (camelCase
   entityType/entityId/report; new pointer scheme
   `gs://…/evaluations/startup/{id}/{uuid}/bundle.json`, rows dated May 2–6). They are
   skipped loudly at ingest (`skipped:experimental_shape`), not parsed. The 4 most
   recent evals (May 31–Jun 2) are back on the original `reports/` scheme.
2. **Accompanying DB alteration — minor, confirmed:** migration
   `2026_05_01_000000_drop_redundant_columns_from_startup_company_evaluations_table`
   is applied on test but exists in no local repo; it dropped `market_score`,
   `product_score`, `traction_score`, `team_score`, `finance_score`, `summary`,
   `notes` (present in the repo's create migration). **Zero advisor impact** — the
   backfill reads only id/startup_company_id/raw_json/is_visible (+ startup
   is_published); category scores come from inside bundles. Also noted: test is ~30
   app migrations behind the repo (nothing after 2026-04-16 applied); 4 old
   entitlement migrations on test aren't in the repo. Prod-facing code unaffected.
3. **Remedy:** 24 original-shape prod bundles (12 gen-1, 8 gen-0a, 3 gen-0b, 1 gen-2;
   distinct companies) copied server-side to `gs://x1-app-www-test/reports/prod_fixtures/`
   (prod untouched; test experiments untouched). They ingest via
   `backfill_evals.py --fixtures` as entity_id-NULL docs with
   `{entity_ref_env: 'prod', prod_startup_company_id}` metadata.

## 2026-07-08 — Phase-0 gate PASSED on the OpenAI/DeepSeek stack → continue on Haystack

All three provider-swapped gate spikes green after David refreshed `OPENAI_API_KEY`:

- **A′ cache usage** (`spike_a2_openai_cache_usage.py`): OpenAI auto-cache hit on call 2
  — `prompt_tokens_details.cached_tokens: 3328` of 3,385 arrived in `reply.meta["usage"]`
  and normalized to `cache_read_tokens` via `Usage.from_haystack_meta`; cached call priced
  $0.000117. The cost-tracking failure mode §10 guards against is closed for this stack.
- **B′ agent loop + web search** (`spike_b2_agent_web_search.py`): Haystack
  `Agent(exit_conditions=["text"])` + gpt-5.1 main agent + `web_research` Tool wrapping
  OpenAI server-side web search → tool invoked, correct grounded answer, **37 resolvable
  citation URLs**. ~**$0.025 per search-call** (10.4k injected input tokens + $0.01 fee) —
  vs $0.0008 for the DeepSeek equivalent; E3 will quantify that trade properly.
  **Implementation finding for E3:** on gpt-5.1 the inline `url_citation` annotations are
  NOT reliably emitted (0 on two live runs); pass
  `include=["web_search_call.action.sources"]` and read `action.sources` (filter
  `type=="url"`; internal feeds like `oai-finance` come back `type=="api"`, `url=null`).
- **C′ models + embedder** (`spike_c2_openai_deepseek_models.py`): `gpt-5.1`,
  `gpt-5-mini`, `deepseek-v4-flash` (OpenAI-compatible endpoint, `prompt_tokens_details`
  intact), and `text-embedding-3-small` (**1536 dims** confirmed) all pass through
  Haystack with `cost.py` pricing.

**Consequence:** Phase 4 stays on Haystack (thin-stack exit ramp unused). Phase 1/2 can
proceed on the working defaults (gpt-5.1 / text-embedding-3-small / OpenAI web search).
Anthropic spikes A–C remain shelved, non-blocking.

## 2026-07-08 — Dev providers: OpenAI default everywhere (company-paid); DeepSeek opt-in; Anthropic deferred (David)

**Decision (David, verbatim intent):** don't block on missing Anthropic keys — develop on
what we hold keys for. **OpenAI is the working default for chat, embeddings, AND web
search** because those calls bill to the company API key. **DeepSeek stays fully wired as
an option** (spike-D-verified searcher, generator-registry candidate) but currently runs on
David's **personal** key, so it is **opt-in only** — a company DeepSeek key may come later
and would lift that restriction. Anthropic/Voyage candidates re-enter the moment their keys
land; this changes *dev defaults*, not the bake-off design (all E1–E4 candidates stand).

Working defaults until bake-offs say otherwise:
- **Main agent (dev):** `gpt-5.1` via `OpenAIChatGenerator`.
- **Embeddings (Phase 1/2):** `text-embedding-3-small` (1536d — same dim as the
  `advisor_evidence` precedent).
- **Web search:** OpenAI server-side `web_search` (Responses API) wrapped as the
  delegated-searcher `Tool` — `spikes/spike_b2_agent_web_search.py` is the reference shape;
  `--searcher deepseek` is the opt-in variant.

**Blocker found while executing this (2026-07-08):** the `OPENAI_API_KEY` in `.env` is
**invalid — OpenAI returns 401** (key suffix `…baYA`; no shell override; `.env` dates to
Jun 12). Credential-file reads outside this repo are policy-gated for the agent, so David
must refresh it (re-sync from x1-backend or paste a current company key). Until then every
OpenAI-default spike is key-blocked, same exit-2 discipline as the Anthropic ones.

**Gate evidence already in hand (provider-swapped spikes, 2026-07-08):**
- `deepseek-v4-flash` through Haystack's `OpenAIChatGenerator` (OpenAI-compatible endpoint):
  works; usage arrives with `prompt_tokens_details.cached_tokens` **present in
  `reply.meta["usage"]`** — the load-bearing cache-field-passthrough question is answered
  for the OpenAI wire shape; priced through `cost.py` at $0.000008.
- Haystack `Agent` + `Tool` constructs and warms up clean on this stack (import-level).
- Full A′/B′/C′ runs pend only on the refreshed OpenAI key.

## 2026-07-07 — Connection pooling: per-worker store instances, no PgBouncer (Phase 0)

**Decision:** run one `PgvectorDocumentStore` (i.e. one cached psycopg connection —
review §6.1.3: the store holds a single connection, sync *and* async) **per service
worker**, not a PgBouncer sidecar.

**Why:** at this scale (corpus in the low hundreds of documents, single Cloud Run
service, admin-gated v1 audience) a PgBouncer deployment is pure overhead. A Cloud Run
deploy with 1–2 uvicorn workers holds 1–2 advisor connections against Cloud SQL —
nowhere near connection limits. The service will connect via the Cloud SQL Python
connector (no proxy sidecar), per review §5.

**Deploy-config implication (Phase 5):** instantiate the document store per worker
process (not module-level shared across forks); set uvicorn `--workers` explicitly so
the connection count is a deliberate number.

**Revisit trigger:** worker count × per-worker store instances approaching ~20
connections, or opening the audience beyond admins pushes concurrency up. Then:
PgBouncer (transaction pooling) in front of Cloud SQL.

## 2026-07-07 — DeepSeek server-side web_search bills tokens only (Spike D closed)

**Decision:** `cost.py` prices DeepSeek `web_search` at **$0 per call** (explicit
`_tool_web_search` row); the real cost is the search results injected as **input
tokens** on the Anthropic-compatible endpoint.

**Evidence (live call, 2026-07-07, `spikes/spike_d_deepseek_billing.py`):**
- `POST https://api.deepseek.com/anthropic/v1/messages`, model `deepseek-v4-flash`,
  tool `{"type": "web_search_20250305", "name": "web_search"}` → HTTP 200,
  content blocks `thinking → server_tool_use → web_search_tool_result → thinking →
  text`, 10 citations all carrying real URLs (citation contract satisfied).
- Usage block verbatim: `{"input_tokens": 5555, "output_tokens": 169,
  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
  "server_tool_use": {"web_search_requests": 1}, "service_tier": "standard"}` —
  a search count, **no fee field**; the one-line question cost 5.5k input tokens
  because results are billed as input.
- DeepSeek's official pricing page (api-docs.deepseek.com/quick_start/pricing) lists
  token prices only; no per-search line item.
- Whole grounded-search call priced through `cost.py`: **$0.000825**.

**Side finding baked into `cost.py`:** the Anthropic-compatible endpoint returns
*Anthropic-shaped* usage (`input_tokens` + `cache_read/creation_input_tokens`).
`Usage.from_haystack_meta` now detects that shape for non-Anthropic providers so
DeepSeek cache-read tokens aren't silently dropped.

## 2026-07-07 — Phase 0 gate status (spikes A–C blocked on `ANTHROPIC_API_KEY`)

Spikes A–C are written and runnable (`spikes/spike_{a,b,c}_*.py`, exit 2 = blocked,
0 = pass, 1 = fail) but **cannot run until `ANTHROPIC_API_KEY` lands in `.env`**
(checked: not in `.env`, not in the shell env, no `ant` CLI profile on this machine).
The Haystack go/no-go gate is therefore **open, not failed**. Everything
framework-independent proceeded:

- `advisor` schema created on test; pgvector 0.8.1 **enabled** (not just available).
- Versions pinned via `uv.lock`: haystack-ai 2.30.2, anthropic-haystack 5.13.0,
  pgvector-haystack 6.3.1, anthropic SDK 0.116.0, Python 3.13.
- Langfuse keys verified (HTTP 200, project `x1-backend-agentic`).
- Leftover schemas measured for David's drop/confirm: `advisor_obs` = **10 GB**
  (events 8.77M rows, runtime_traces 410, service_runs 1,091), `advisor_evidence` =
  **133 MB** (8,268 rows, `vector(1536)` HNSW cosine — the dim precedent noted in
  the plan). Nothing dropped.
- GCS access verified read-only (`x1-app-www-test`, incl. `doc-extract/` content).
