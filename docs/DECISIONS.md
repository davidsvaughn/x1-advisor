# Decisions

> Dated, evidence-backed decisions per [`PLAN.md`](PLAN.md) §1 — bake-off outcomes and
> engineering choices land here, newest first. Each entry names its evidence (spike
> output, manifest path, or doc reference) and the revisit trigger if one exists.

## 2026-08-13 — test-env re-convergence: mastra-v3 experiment archived, prod evaluations restored to test

David's call ("good ideas, wrong time"): the May-2026 mastra-eval v3 bundle
experiment is SET ASIDE and the test env re-converged with prod. Executed
today (prod strictly read-only throughout — every prod connection opened
with `default_transaction_read_only=on`):

1. **Archived**, self-describing, at
   `gs://x1-app-www-test/archive/mastra-eval-v3-2026-05/` (104 objects:
   all 99 v3 bundle objects, the three design docs incl.
   `2026-05-01-EVAL-BUNDLE-SCHEMA.md`, a README with the revival
   checklist, and a full JSONL dump of the 75 deleted rows; local mirror
   in `.qa-artifacts/archive/mastra-eval-v3/`). Code stays on branch
   `harness` (x1-backend; x1-app `9ccdf5c0`).
2. **Schema restored**: the 7 columns the phantom (never-merged) May-1
   migration dropped are back on `startup_company_evaluations` (nullable —
   prod has the scores NOT NULL, but existing test-local rows have no
   values; do not fabricate); the phantom row is deleted from `migrations`,
   which now honestly ends at 2026-04-18.
3. **Data restored**: 75 mastra rows deleted (dump exists); the 4
   test-local original-shape rows re-idd to 100205–100208 (nothing
   references eval ids); **107 prod evaluation rows copied verbatim**
   (bare-path pointers are bucket-agnostic) for the 45 id+name-matched
   companies, ids preserved for cross-env debugging; sequence bumped.
   Final: **111 rows, 0 experimental pointers, 45 companies**, eval
   history now spans 2025-08 → 2026-07.
4. **Blobs**: 232/233 referenced objects copied server-side prod→test.
   The 233rd (`reports/X1 Pipeline_6dfa9121….json`, eval id 8) is missing
   ON PROD — test now mirrors that dangling pointer faithfully; the
   advisor backfill must skip-loudly on missing blobs (check before
   re-ingest). Companies test-only (`Ask Norby`, `FT Pro`) keep their
   local state.
5. Test-company id+name identity vs prod verified 48/50 before any copy;
   3 ai_versions FK targets pre-verified present.

Still open: x1-app migration catch-up (repo pulled to origin/dev; test DB
pending everything after 2026-04-18 — apply only in step with the test app
deployment, David to confirm its version); advisor re-ingest + truth
rebuild + fresh baseline (separate step; prod_fixtures docs retire then).

## 2026-08-11 — harness seatbelts: judge-transport containment, promoter completeness, `nightly.py` → `qa.py`

Three small fixes, David-approved, drawn from the consolidated review's P0/P1
(the rest of that list is deferred to the next contract sever). No grading
semantics change — the scoring contract does not sever. David also
explicitly closed two review threads: DeepEval adoption is dropped entirely,
and the assisted-vs-unassisted calibration-provenance concern is dropped
("too nitpicky").

**Judge-transport containment + tripwire** (`judge_cc.py`, `adjudicate.py`).
A dead `claude -p` subprocess (timeout, nonzero exit, garbage stdout) used to
raise past the escalation gates' fail-safe — which only caught parse errors —
and kill a ~15-minute graded run at whatever case it was on. Now: an isolated
dead call is a discarded judge SAMPLE (counted, printed, recorded on the
summary row as `judge_transport`), and the fail-safe keeps the formula
verdict standing, which can only preserve a failure, never absolve one.
Systemic failure crashes loudly by design (David: "I might rather fail
loud"): ≥3 failures above a 10% failure rate, or all k samples for one item
dead in the same window, raise `JudgeTransportDown` — a run whose judge is
down must not complete as a plausible-looking formula-only run. Knobs:
`ADVISOR_JUDGE_MAX_TRANSPORT_FAILURES` / `ADVISOR_JUDGE_MAX_TRANSPORT_RATE`.
A missing CLI binary stays a hard, immediate error (config, not flake).

**Promoter completeness** (`qa.py::accept_baseline`). `--accept` verified
filenames, drift and slice-uniqueness but never that a manifest was
complete — and a runner death mid-run leaves a partial whose filename is
indistinguishable from a complete run's (it happened 2026-08-04). Accepting
one would silently blind every future comparison to the missing cases. Now
each manifest must carry exactly the suite's row count for its slice
(currently smoke 7 / core 49 / scripts 4 — read from the compiled suite, not
hard-coded), a summary row (proof the runner finished), and the current
scoring contract on every row. Verified read-only against the accepted s6
trio: all three pass (49/49, 7/7, 4/4), so nothing is retroactively
invalidated.

**Rename `experiments/nightly.py` → `experiments/qa.py`.** The name predated
the 2026-08-05 decision that scheduled runs are a production concept; it kept
implying a scheduler that does not exist and confused its own operator. Same
CLI, same jobs, same exit codes; reports now write `<date>_qa.json`. Living
docs and the triage prompt updated; dated/historical docs keep the old name
as record.

Evidence: `tests/test_transport_and_promoter.py` (containment, tripwire
boundaries, promoter refusals); full suite 226/226.

**Addendum 3, same day — enumeration-structure prompt rule (David: "go").**
The flagged semicolon-wall (thread 18: 49 startup names packed into one
sentence) traced to a prompt collision: rule 5 demands the complete census
while Style demands ~400 words, and with no structural-formatting guidance
the model resolves the squeeze by compression. Approved wording added to
Style: enumerations >~6 items render as a markdown list or table (table when
items share comparable attributes), never a semicolon-run sentence; the word
budget yields to enumerations. Durable rationale: resolves the
tight-vs-complete tension for every census/list answer, no query or entity
named. SYSTEM_PROMPT_SHA256 updated in-commit; judged core run + baseline
comparison follows this commit. Sample-and-ask was considered and rejected
(collides with census honesty + full-recall grading); collapsing long lists
is handled render-side in the dev console instead.
*Outcome (same day, run `2026-08-11_v2_core_832458c_r1`):* 40/49 (baseline
39/49, band 35–39), faithfulness +0.01, overclaims down, latency −4s — but
compare FAILs on net label worsening: structure redistributes citation
brackets, the judge inventories each bullet as its own claim, and
`citation_coverage_error` labels rise (~0.956→0.909 mechanical coverage)
on answers the calibrated citation-intent GATE blesses (that is why passes
rose while labels worsened — adjudication never rewrites telemetry, by
design). Verified in bundles (v2c034: 12-bullet inventory, lead-in carries
the citation, 15 "uncited" claims). David: prompt change stays; **queued
for the s7 sever: list-inheritance in the cc judge claim inventory** (a
list item inherits its lead-in's citation), batched with the previously
deferred s7 items. Interim: smoke+scripts rerun under the new prompt for a
clean trio; acceptance remains David's call. Also: `--reload` is now the
documented dev default for the service (a running process otherwise keeps
the old prompt in memory after an edit).

**Addendum 2, same day — first live REPL session: three bugs, live-loop
tooling, browser dev console.** David's first interactive session crashed on
its first turn and exposed: (1) the REPL's citation printer knew only
internal/web kinds — a `platform_data` citation (structured-query evidence)
KeyError'd the session (`chat.py::_locator` now renders all three kinds);
(2) the REPL never appended to `history` — run_turn only READS it, so every
"multi-turn" session was stateless (now appended per §9); (3) the model's
first tool call wrote `parameters` (the JSON-Schema word) for
`structured_query`'s `params` and burned an agent step on the TypeError
round-trip — the closure now accepts the synonym, `params` stays canonical
(durable tool-contract tolerance, not trace-patching). Visibility answer to
David's ask: every live turn already persisted twice (advisor.threads/turns
+ complete bundle export) — the REPL now SAYS so (thread/turn ids + bundle
filename per turn). Regression capture: `/flag [note]` (REPL) and a flag
button (console) append pointers to `.qa-artifacts/repl/flagged.jsonl`
(`agent/flags.py`) — candidates for QUESTION-BANK/golden-v2 curation, never
auto-added. New: the service serves a self-contained browser dev console at
`GET /` (markdown-lite, per-kind citations, clickable web links, per-turn
cost/ids, flag button; `ADVISOR_DEV_CONSOLE=0` disables — deploy checklist).
Verified in a real browser (playwright): renderer output correct, full
ask→render→flag flow live, zero console errors. Port note: run on 8123
(8100 occupied on the dev box).

**Addendum, same day — dev proxy self-healing** (`db.py::connect`,
David: "wire the proxy restart into the command itself"). The recurring dev
blocker (ADC lapses ~weekly; a long-running cloud-sql-proxy keeps cached
credentials and drops every connection until RESTARTED — refreshing auth
alone never heals it; three occurrences: 07-30, 08-06, 08-11) is now
handled inside `connect()`: a proxy-shaped failure on a proxy-socket host
triggers one ADC check + proxy restart + retry. Lapsed ADC fails with the
actual fix (`gcloud auth application-default login`) instead of the cryptic
psycopg error. Narrow by construction: only instance-shaped socket hosts,
only proxy-death signatures, only when a proxy binary exists on the box —
deploy paths and real auth errors raise exactly as before; kill switch
`ADVISOR_PROXY_AUTOSTART=0`. Live-verified by killing the proxy and
watching `connect()` revive it. Tests: `tests/test_db_proxy_revival.py`;
suite 234/234.

## 2026-08-07 — s6 accepted from a replicate median; parallel harness; variance measured

Three threads landed together:

**Behavior gate (s6) — and the harness catching its first bad rubric.** The
targeted behavior judge failed v2c026 twice for not reciting the
state_absence parenthetical while the answer stated the absence and where
market scores live; terra is now the DETECTOR and unmet flags escalate to
the cc judge (`adjudicate_behavior`). Rubric v1 failed calibration exactly
as the replay harness exists to detect: 0.40 agreement with TWO
leniency-ratchet breaches (the judge second-guessed case-verified
presuppositions from the answer's confidence) plus an anchoring inversion
(it verified the detector's wording complaint instead of judging
reader-substance). v3 (presupposition = declared ground truth; the
detector's complaint quoted as "usually literally true — not the
question"; statistic-paired-with-absence named as the honest form):
behavior 1.00/1.00, full set 45/46, 0 breaches, the one stable miss the
recorded cc03 strict-side tension. Also: `single_evaluation_startup` pool
gained a normalized-name collision guard (2ndCourt.com/2ndCourtcom,
AcceliumPartnersAG — one evaluation per record, two under the user-typed
name; regenerated, 3 entities out).

**Parallel harness (5b416d4, David: "I don't want to wait").** Pooled k=3
gate samples, 4-wide case workers (own DB connection each), 4-wide script
workers, one global judge-subprocess semaphore
(ADVISOR_JUDGE_CONCURRENCY), a Tracker lock. Orchestration only — no
grading semantics; judged core wall time ~3x better; drift-free manifests
verified. The killed sequential run's partial core manifest was deleted,
not completed: rows spanning two shas are the exact mixed identity the
harness refuses.

**Replicate-median baseline (David: "okay do 2").** Three core replicates
at 5b416d4, same seed: **35 / 39 / 39** (mean entity recall .849/.889/.866)
— the first measured variance band, and proof single-run baselines were
±4-6 noise (yesterday's 41 was the lucky tail; rerun-until-green stays
forbidden, the median replaces it). Accepted: core r2 (39/49, median,
nearest the consensus failure set), smoke 64d063d_r1 (7/7), scripts
5b416d4_r1 (0/4 — low tail; sequential history 0/4→3/4 on fresh answers).
**Stable failures, all three runs — the real work list:** census
under-enumeration/overclaims (v2c010/011/012/040/047, gate-examined),
v2c033 (a scoping instruction answered with an acknowledgment and no
search), v2c051 (Accelium AG/GmbH ambiguity never surfaced). Flagged, not
decided: the cc judge refuted list-scoped claims ("which of these…") with
scan hits from outside the list — an error class landing in `unsupported`,
which never escalates by design; relief would mean touching the calibrated
CC judge prompt (David's call). Revisit: baseline accepts use a replicate
median from here on; any rubric edit replays the calibration set first.

## 2026-08-07 — s5: the gates reach everything (scripts 0/4 → 3/4); baseline re-accepted

The scripts diagnosis (David: "proceed with the scripts failures") found every
failing unit in the d3afbc7 0/4 traced to a formula misreading good behavior —
no fabrication, no coverage lies, no invented entities. The concrete shapes:
quotes failed for marked elision ("…"), bracketed splices ("[its]"),
nested-quote-mark conversion, added **emphasis**, a U+2011 hyphen and a
dropped inline link — all traced to evidence at 0.82–1.00 similarity;
v2s003's model answer ("No—not all 20 … 25 had searchable text, 39 were not
indexed") was failed for containing honest numbers because every numeral was
compared against a searched-denominator of 1; v2s002's "remaining 12 listed
startups had no indexed evaluation text" matched 0 coverage regexes (one
adjective defeats `\d+ startups`); the scan-discovered no-space record
`acceliumpartnersag` was flagged as an invented entity; and script turns had
NO gates at all — v2s004's three cited-elaboration headlines failed
unadjudicated sub-shape A. David approved the four-part plan ("sounds
great!"); contract severs s4 → s5 (`410a95c`):

1. **Script parity** — the citation/faithfulness gates now run on script
   turns exactly as on cases.
2. **Detector hygiene** — quote matching canonicalized (`_canon_quote`):
   typographic hyphens/quote-mark folding, markdown emphasis, inline links,
   NBSP. Deterministic, both sides; 12 → 8 misses on the stored run.
3. **Four new gates** (`experiments/adjudicate.py`): quotes (editorial
   conventions faithful, paraphrase/meaning-altering elision not),
   coverage_statement (scope disclosure in substance, any phrasing),
   coverage_claims (searched denominator stays deterministic — and now
   counts scan_text's own `counts.scanned` — while the reading of which
   numbers CLAIM coverage escalates; telemetry rides in the prompt),
   entity_intrusion (evidence-surfaced records are not inventions). Gated
   deterministic assertions and cross-turn checks carry
   `formula_passed` + a body-free adjudication summary in the manifest;
   per-item detail stays owner-only (NAMED_DETAIL).
4. **Calibration 26 → 41 labels** (6 new must-fail synthetics: paraphrase-
   as-quote, negation-dropping elision, invented quote, missing scope
   statement, searched-all-N overclaim, invented entity); replay harness
   extended to all seven gates. Live replay: **0.98 agreement (40/41), 0
   leniency breaches**, $8.98. The one miss — cc03, "the scan covered 64
   startup entities" where 64 is the eligible count and 25 were scanned —
   split 2/1 toward overclaim: the judge errs STRICT on that phrasing; the
   label stands as the intent anchor and the tension is recorded here, not
   relabeled away.

**Verification trio at `410a95c`** (judged, seed `v2-baseline`, no drift),
accepted as baseline: smoke **7/7**; core **41/49** (s4 bar 37/49 — v2c015,
v2c032 and the state_absence stragglers flip; 14 judged-dim gate escalations,
all unanimous); scripts **0/4 → 3/4**, the remaining failure a genuine
defect: v2s004 stated "£0.1m net income" from a table whose 2025 row reads
£1.0m — graded unsupported, and the faithfulness gate structurally refuses
to adjudicate when unsupported exists (never absolve B). Remaining core
failures are all examined signal: 4 census truth_set fails with judge-upheld
overclaims, state_absence (v2c026), premise correction (v2c045), ambiguity
surfacing (v2c051), and v2c033 citing nothing. Noted for the tool backlog:
one agent turn called `structured_query` with a `parameters` kwarg and
recovered on retry. Revisit triggers: any rubric edit re-runs
`experiments.adjudicate_calibrate` before the next accept; rejudge_v2
escalates judged dims only (fresh runs are the canonical s5 path).

## 2026-08-06 — Escalation gates (s3): the formula flags, the judge disposes — David's methodology call

Prompted by the citation_coverage post-mortem: the bracket-counting formula
failed answers whose every claim the Opus judge had verified TRUE, because
bold bullet headlines carry no bracket while their elaborating sentences one
line down are fully cited (v2c019: 21/21 supported, cc 0.67, case fail; the
judge's own reason lines showed it saw this). Third such episode in a week
(census buckets/list-inheritance; the state_absence obligation phrasing) —
each one a formula misfire that an intelligent reader of the pipeline's own
output overrode by hand. David's call, generalizing beyond the instance:
**stop distilling judge intelligence into formulas that gate. The formula is
a detector; the calibrated judge adjudicates its flags against an intent
rubric. Expected to become the default methodology.** Field practice agrees
(hybrid norm; escalation-with-ensemble on flagged cases; "citation theater"
is a named Goodhart mode — sources in the 2026-08-06 session log).

**Shipped** (`experiments/adjudicate.py`, 7179497 + 35bb106; scoring
contract severs s2 → s3): two gates —

- **citation_coverage**: uncited-claim flags adjudicated for reader
  traceability (a headline whose adjacent cited sentences genuinely support
  it is adequately attributed; citation theater is not). Wrong-ref claims
  (the v2c036 swap) are structurally untouched: they fail *faithfulness*,
  which no adjudication reaches.
- **asserted_names**: overclaim/miss flags adjudicated against rule-5 census
  intent. The ORACLE stays deterministic forever — only the parser's reading
  of prose escalates. Note: the rubric's variant-attribution clause realizes
  the semantics of the parked truth-v4 "adjacent tier" (Option A) through
  judgment instead of oracle machinery.

Safety properties, test-pinned: escalation only (clean formula passes never
spend judge tokens); fail-safe (unusable judge → the formula failure stands);
k=3 per-item majority; formula output rides in every manifest beside the
adjudication (leniency-ratchet tripwire); body-free projections.

**Verification on the stored baseline answers** (the artifacts that motivated
the build; all votes unanimous 3/3): v2c019's 7 flagged headlines → adequate,
gate passes. v2c021's thesis + closing recap → adequate. v2c047's 8 formula
overclaims → 7 disclosed-not-asserted (broader-term census inside a
disclaimed group), **1 upheld** (an entity the answer singled out as a
most-risk-like reference) — case still fails, correctly. Relief where the
formula misread, no relief where the answer overstated.

**Same-day follow-up (s4)** — David resolved both open items with one
directive: "I'd always lean towards being LESS nitpicky rather than more,"
delegated. So: (1) the seed calibration labels are ratified as proposed
(thesis/closing-recap = adequate when every element maps to a cited bullet);
(2) faithfulness joined the escalation (c274559, contract s3 → s4 — no
baseline was ever accepted on s3). Scope is deliberately asymmetric: ONLY
"partial" entailment flags escalate, adjudicated for
would-the-reader-be-misled against the WHOLE turn evidence set (hedged,
labeled inference over cited inputs = faithful; inference dressed as sourced
fact, or quantities beyond the evidence = unfaithful). "unsupported" and
"unverifiable" NEVER escalate — with any present the gate cannot flip and
spends nothing; fabrication stays out of reach by construction, per David's
standing "never absolve B." Verified on the stored v2c021 partials: 4/4
faithful, unanimous 3/3, units flip to pass. Calibration set now 26 labels
(+4 real hedged-synthesis, +2 synthetic must-fail).

**Replay harness shipped and green (d31d67f)** — the recorded trust
condition before nightly scale. `experiments/adjudicate_calibrate.py`
replays every labeled item through the production gate functions — same
rubrics, same k=3 majority — so a rubric edit becomes a measured change
instead of a vibed one. The five synthetic must-fail bodies (uncited
headline, citation theater, uncited quantitative thesis, fabricated
causality dressed as a sourced finding, quantity beyond the evidence) were
authored owner-only beside the real items' bundle pointers; a must-fail
item judged lenient exits nonzero, so nothing automated can shrug the
ratchet moving. First live replay (2026-08-06, cc Opus): **26/26
agreement, kappa 1.00 on all three gates, 0 leniency breaches**, $3.60 —
the never-before-judged synthetics included. Run it after any rubric edit
and before trusting a re-baseline.

**Baseline re-accepted under s4 (`d3afbc7` trio, seed `v2-baseline`,
judged, no identity drift)** — the first accepted reference where the gates
are live. Smoke **7/7** (= bar). Core **37/49** vs the s2 bar's 20/49 — the
step change is the session's three workstreams landing together: registry
queries unblocking the state_absence cluster, the population-statistics
prompt rule, and the escalation gates ending the formula-artifact failures.
Scripts **0/4**, identical to the s2 bar (quotes_verbatim carryover,
coverage_claim_grounded, multi-turn citation coverage — real multi-turn
defects, next in line as capability work). In-run gate telemetry: 11
adjudications across 9 cases — 10 unanimous 3/3 overturns of formula
artifacts (incl. v2c036's once-flip-flopping faithfulness partial, now
stable under k=3), and **1 upheld failure** (v2c014 faithfulness, 1/1
inadequate) — relief and anti-leniency demonstrated in the same manifest.
Judge spend for the full judged trio: ~$20. Remaining core failures are
real signal, not formula noise: state_absence behavior (v2c026/031),
faithfulness (v2c014/024/046), truth-set (v2c009/047), quotes_verbatim
(v2c032/033), correct_premise (v2c045/046), coverage_statement (v2c015),
surface_ambiguity (v2c051). Revisit trigger: any rubric edit re-runs
`experiments.adjudicate_calibrate` before the next accept.

## 2026-08-06 — Coverage + aggregate registry queries: the state_absence cluster unblocked

First capability build of the post-baseline queue (David: "start on real
queue"). Four registry queries shipped in `queries.QUERIES` (dc3044f) — the
surface the `registry_query`/`coverage_query` cases had waited on since the
bank review:

- **documents_for_company** — the §3.3 coverage surface: indexed corpus
  documents matched by the title-name convention (spans both fixture envs,
  which the app-id join cannot), each with per-requester searchable/gated
  status derived from the retriever's own premium chunk predicate, plus app
  uploads on record — files, never presented as searchable text.
- **evaluation_score_stats** — avg/min/max/count of the **overall** X1 score.
  Only the overall score is structured data (schema recon: evaluations'
  `raw_json` column holds a report file path, not JSON); dimension scores
  live in evaluation document text, and the query description says so.
- **investors_for_company** — the platform match registry; empty is an honest
  "none recorded" (the registry has no rows on the test corpus).
- **count_cvs** — total / open-to-work / published under the CV ACL.

ACL: `_owner_published_acl` generalizes drafts-are-owner-only across the
startup/investor/CV profile tables; `_doc_acl` mirrors `retrieval._acl_sql`
at document level; premium gating is existence-visible / text-gated, the same
posture as scan's `restricted` surfacing. Live-verified in both directions
(non-admin sees BMI OrganBank's premium report `gated: true` and loses the
private deck uploads; stats population shifts 79→71 evals under ACL).

**Case flips** — v2c025–027 + v2c034–037 to `tool_ready: true`; contract
`modes-bd3235eb → modes-046b5064` (§4: never silent). The first capability
run (manifests `2026-08-06_v2_core_dc3044f_r1–r7`, clean tree) exposed a
spec artifact: the four presence-answering cases failed **only**
`behavior:state_absence` — an obligation ordering the answer to "say the
corpus does not contain it" against answers correctly reporting presence
(v2c034 returned the full 12-document inventory; the judge could only rule
the obligation unmet). state_absence was the honesty-era fallback bar; it
stays where absence genuinely is the answer (v2c025 empty registry, v2c026
missing market dimension, v2c035 deck-searchability nuance) and came off the
four presence cases — `cases.py` itself requires it only for class
`known_absence`. A dirty-tree verification regrade confirmed all four pass
under the corrected blocks (manifests deleted per run-identity hygiene; the
clean-sha rerun follows this commit).

**Cluster after: 6 of 7 pass.** The residual fail is product signal, not
measurement: **v2c026** ("average market score") — the agent demonstrably
read the new catalog (its answer scopes overall-vs-dimension exactly as the
description states) and correctly declined to estimate, but never *called*
evaluation_score_stats, and asserted "no extractable numeric market scores"
off two corpus searches (judge: unsupported claim; faithfulness 0.33). The
durable gap is behavior — run the registry aggregate and pair the overall
statistic with the dimension-absence statement — which is prompt/routing
work needing its own approval, not a case edit.

Revisit triggers: the match registry gains rows (v2c025 flips to presence —
recheck its obligation); dimension scores land as structured columns
(v2c026's premise changes); working-set inventory (bank #68–70) waits on
Gate 3B context.

## 2026-08-06 — Census framing in the truth grader: exclusion groups, scope lists, trailing hedges

The first fully-judged trio under truth v3 (`23eb169` manifests, core 22/49,
zero judge coercions) exposed a pincer the same day the census-completeness
prompt rule landed: rule 5 orders the agent to *name every lexical hit and
file the irrelevant ones in a labeled group*, while `asserted_names` counted
every named entity as an assertion. Three of the run's best answers graded
worst:

- **v2c012** — 11 names under "the following appear to be unrelated to payer
  adoption or hospital procurement" → 11 overclaims; its one truth entity,
  framed "not hospital procurement friction *per se*, but…", was
  negation-stripped → recall 0.00 on a complete, correctly-annotated census.
- **v2c041** — "The scan covered: …" enumerating 13 scanned investor profiles
  on a correctly-reported empty oracle → 13 overclaims.
- **v2c038** — the mirror jaw: the agent *didn't* name 7 weak matches
  ("I've excluded them…" without naming) → recall 0.27. Compliance punished
  one way, the old curation mode punished the other.

**Decision: four polarity buckets in `asserted_names`** (was two). `positive`
(recall credit + overclaim liability), `negated` (neither), `excluded` —
named in a labeled exclusion group: recall credit, **no** liability, because
the census reached the reader and nothing was asserted as matching — and
`scope` — named in a scan-scope enumeration: neither credit nor liability.
Precedence positive > excluded > negated > scope, so a disclaimed repeat can
never hide a real claim. "not X per se" joined the hedge list (trailing form,
gap capped at clause punctuation). Markers are lexical and conservative;
blind spots stated in the module, same design contract as negation.

**Evidence** — offline re-grade of the `23eb169` core answers, new grader vs
old: v2c012 recall 0.00→1.00, overclaims 13→3; v2c041 overclaims 13→0; every
other truth case byte-identical. The three residual v2c012 overclaims and
v2c047's five are **adjacent-term assertions** ("closest substantively
relevant mentions" beyond the lexical oracle) — the still-open Neusner-class
policy question, deliberately untouched pending David's call. v2c038's 0.27
also stands: that run really did drop names, and the grader must keep saying
so.

**Comparability:** grader semantics changed again, so the `23eb169` trio is a
diagnostic artifact, not a baseline candidate; the re-accept candidate is the
next judged trio on this commit. Revisit trigger unchanged from the v3 entry:
any truth case passing `judged:faithfulness` while failing `truth_set` in ≥2
runs gets a grader/oracle audit before any agent-side change.

**Same-day follow-up (judged trio on `23a782b`).** The rerun validated the
buckets live (v2c012 recall 0.00→1.00, v2c041 clean pass, v2c009 at 1.00
again) and exposed two more layers:

1. *List formatting severs framing.* The agent formats census groups as
   markdown lists — "returned five incidental mentions:" then one name per
   bullet — and newline sentence-splitting cut the bullets loose from the
   framing line, so v2c049 graded 5 disclosed non-matches as 5 overclaims.
   Fixed structurally, not with more markers: list items inherit the census
   bucket of their introducing line until prose resumes; a plain intro ("The
   9 are:") inherits nothing, and an item's own negation beats inheritance.
   Offline re-grade: v2c049 5→0 oc; both judged runs otherwise
   byte-identical, run-1 grades reproduced exactly.

2. *Variant attribution is the dominant residual, and it is a policy, not a
   marker gap.* Of the 32 core overclaims left under the final grader, the
   verified bulk (v2c012's 15, v2c047's 8, v2c038's adjacent 4) are the agent
   doing exactly what the base-token rule orders — scan broad terms, report
   hits attributed to the fired term ("fired on the broad words *payer*,
   *procurement*, or *hospital*") — while the truth set only recognizes the
   narrow oracle terms. Every run invents fresh phrasing for this
   disclosure, so lexical exclusion markers structurally cannot keep up.
   This is the parked variant-attribution overclaim policy, now measured as
   the main cost. Candidate durable design, David's call: truth builder v4
   adds an *adjacent tier* per case (base tokens of each oracle term), so
   the oracle itself knows which entities are variant-explainable and the
   grader classifies them entity-by-entity — no prose parsing — as
   disclosed-variant (visible count, no liability) instead of overclaim.

## 2026-08-06 — Truth tier v3: word-start phrase matching + hedge-aware negation

Triggered by the v2c039/v2c009 bimodality diagnosis (David: "start with the
v2c diagnosis"). Both bimodalities were **harness defects, not agent
variance** — the agent ran identical, correct scans in every run.

**Defect 1 — oracle corruption (engine).** `phrase` matching was bare
substring (`ILIKE '%term%'`): "CE mark" fired inside "performance
marketing", "FDA" inside a GCS URL token, "clinical" inside "preclinical".
Corpus audit of all 14 truth sets: 9 of v2c009's 14 matched entities were
artifacts (only BMI OrganBank genuinely mentions CE marking); v2c038 carried
one (Paul Jaminet via "preclinical"); v2c008's flagged hits were legitimate
plural inflections. The run that *detected* the artifacts from excerpts and
declined to repeat them scored recall 0.29; the run that repeated them with
a caveat scored 0.86 — the grader rewarded credulity. Fix: `scan.py
_match_columns` phrase terms are now word-start-anchored regexes (`~*`,
`\m` + escaped term) — left edge closed, right edge open, so "regulatory
risks" and "CE marked" still match. `BUILDER_VERSION` 2→3, all 14 truth
sets rebuilt (`truth_digests.json`): v2c009 14→5 matched, v2c038 12→11,
all other counts unchanged. Post-rebuild audit: zero artifact-only entities.
Known open-right cost, accepted: "Bain" matches "Bainbridge" (no instance
in the current corpus). The shared engine means the fix lands in the live
tool and the oracle at once — users stop seeing phantom matches too.

**Defect 2 — negation scope (checker).** `asserted_names` grades polarity
per sentence; an honest hedged group ("consulting-related wording — not
necessarily a consulting background: A, B, C…") read as a denial of every
name in it. Measured: v2c039's identical 21-name census graded 0.94 when
bulleted, 0.44 when hedged in prose (12 names negated). Fix: hedge patterns
("not necessarily/always/only/exclusively/solely/merely/just") are masked
before the negation test. Real denials still negate; remaining blind spots
(double negation, mixed-polarity sentences) stay documented in checkers.py.

**Effect, same stored answers re-graded** (three 1bb0fe1 runs, zero new
cost): v2c039 0.94/0.94/0.44 → **0.94 all three** (bimodality was entirely
the checker); v2c009 0.29/0.36/0.86 → **0.80/1.00/0.80 with the polarity
inverted** — the artifact-skeptic answer now top-scores and the credulous
answer's repeats count as 8 overclaims, which is the incentive pointing the
right way. Mean truth recall 0.77/0.87/0.80 → 0.87/0.97/0.88.

Comparability: truth-tier numbers before this change are not comparable to
numbers after it (per-case `truth_digest` moved on every truth case; the
scoring-contract modes string is unchanged). The accepted baseline trio
(`fdba68a`, `experiments/golden/baseline.json`) predates v3 — re-accept at
the next judged run (David's call). Principle recorded: **when the agent
outsmarts the oracle, the oracle is wrong** — audit it, fix it, and never
tune the agent toward a defective answer key. Revisit trigger: any truth
case whose `judged:faithfulness` passes while `truth_set` fails in ≥2 runs
gets an oracle audit before any agent-side change.

**Same-day follow-up (the two remaining census defects).** (1) v2c039's
predicate now carries the concept's lexical forms — "consultant",
"consultancy" join "consulting" (the v2c040 PhD/Ph.D pattern; a CV titled
"Operations Consultant" mentions a consulting background). Truth 16→21
matched; the three stored 1bb0fe1 answers re-grade **1.00/0, 1.00/0,
1.00/0** — the case is fully solved by the harness corrections alone.
(2) `_SENTENCE_SPLIT_RE` no longer splits after a single-capital initial:
"Randolph W. Hubbell" was cut mid-name, so no answer could ever be credited
for him — a structural recall cap of 0.94 on every v2c039 run and one lost
name per v2c038 run. Remaining, deliberately not "fixed": v2c038's
adjacent-term reporting (an entity matched only via "medical device"
asserted under a regulatory/clinical question — ~1 entity residual cost,
watch under the fixed tool before widening any predicate) and the census
curation habit (one run dropped 4 matched names as "incidental" without
naming them — agent-side, prompt rule proposed to David). Most re-graded
overclaims elsewhere are stale-tool residue: those bundles ran against the
substring tool, whose phantom matches the agent faithfully relayed; the
fixed engine cannot produce them.

## 2026-08-05 — Nightly cron: no — unattended runs are a production concept

David, clarifying a standing misread: his earlier interest in "nightly
processes" referred to the advisor **in production** (live users, corpus and
provider models drifting on their own — where an unattended regression check
earns its keep). During development, QA/eval/fix cycles are **live and
supervised** — a change is made, the suite is run, the result is read, the
next change follows. An unattended 2am run in dev re-measures a system
nobody touched, spends ~$8.5 of judge seat-quota, and reports to an empty
room. Decision: the cron is off the table until the production transition
(his call, then). The nightly runner/comparator (`qa/nightly.sh`,
`experiments/nightly.py`) remains in daily use as the hand-invoked harness —
nothing built is discarded; only the scheduling concept was wrong. Do not
re-propose the cron during dev.

## 2026-08-04 — scan_text shipped; tool_ready flipped; first capability baseline trio

Path B, David-authorized start and David-approved flip ("yes fix judge, then
do the flip!"). Three landings, one arc:

**The tool** (`2934f7a`). The bounded exhaustive scan moved out of
`experiments/truth.py` into `x1_advisor/scan.py` and is now shared by the
truth-set builder and the new `scan_text` agent tool — grader and tool are
one code path by design (§5.1's compounding payoff realized). Refactor
proven digest-identical two ways: `truth --check` 14/14 current, and the
live tool reproduces v2c008's committed oracle byte-for-byte. Tool
contract: per-entity `matched | no_match | not_indexed` (+ `restricted` for
non-admin), complete coverage counts, citable excerpts (snapshot = exactly
what the model saw), the scan itself query-kind citable evidence for
coverage claims. Blocks only, structurally (record summaries are not
evidence); ACL via retrieval's own `_acl_sql` (one implementation);
premium-gated existence → `restricted` per the bank §3.2A disclosure
policy; private/unpublished excluded from every status and count. Engine
latency ~0.2–0.6s full-corpus over the wire — cheaper than one agent
reasoning step. SYSTEM_PROMPT rule 5 now routes census questions to the
tool and pins lexical-no-match semantics; both prefix hashes updated
in-commit.

**Adoption, then the flip** (`fdba68a`). First post-ship core run
(`2026-08-04_v2_core_2934f7a_r1`, honesty contracts still in force):
**adoption 100%** — all 14 blocked_on=scan_text cases used the tool
unprompted, truth-graded mean recall 0.41 → 0.73 vs the pre-tool answers.
On those numbers David approved the flip: all 14 cases now
`tool_ready: true`, fallback contracts dropped, `must_not_claim_exhaustive`
dropped (a census answer legitimately claims completeness for its scanned
scope — design §4 "unless route == scan_text"). Core tier 38/49
capability-graded (was 24). **Scoring contract moved
`modes-ff08feef` → `modes-bd3235eb`** — comparability across the flip is
severed; the comparator refuses to gate across it.

**Judge completeness fix** (same commit; found by the 2934f7a run, v2c028).
A live-cited factual claim the judge model inventoried but left
verdict-null was coerced to `unverifiable`, label-failing a case whose
every judged claim was supported. Now: one completeness retry naming the
unverdicted claims, then UNGRADED (None) — never a synthetic verdict.
Dead-citation claims keep their honest `unverifiable` without a retry.
Verified in the baseline run: zero coercions, zero ungraded across 49.

**First capability baseline** (trio at `fdba68a`, committed `285902e`):
smoke **7/7** · core **18/49** · scripts **0/4** — the same core headline
as the honesty run on a much harder bar. Flipped cases 5/14; v2c008 is the
exemplar (recall 1.0, zero overclaims, clean pass — unanswerable two days
prior). Failure profile is now the product's, not the harness's:
**overclaims are the top measured defect** (15 entities; 5 of the 9 flipped
failures; sharpest: v2c039 at recall 0.94 with 5 overclaimed) — the agent
scans a phrase variant and reports variant hits as the asked concept. Also
real: recall variance run-to-run (0.73 → 0.59 on identical questions; the
agent's scan-phrase choices are nondeterministic), state_absence 8,
judge-strictness residue ~2. Next lever: an overclaim-discipline prompt
rule — proposal goes to David separately (prompt changes need his explicit
approval). His open calls: accept this trio as baseline
(`nightly --accept`), nightly cron, unassisted calibration labels,
held-out batch.

Revisit triggers: three runs of flipped-tier numbers to size recall
variance; false-positive audit of the cc judge once flags accumulate under
the capability contract.

## 2026-08-04 — Judge audit: ~92% false positives; judge switched to headless Opus 5

A four-auditor false-positive audit of all 28 `judged:faithfulness` failures
in the post-Path-A run (74 flagged claims, each checked against the exact
evidence snapshots; full rulings owner-only at
`.qa-artifacts/reports/2026-08-04_judge_audit.md`) found **3 judge-correct /
68 false-positive / 3 ambiguous**. Causes were structural, in the judging
harness, not the judge model: `evidence_texts()` stripped document titles
(the only carrier of company names and report types), `judge_one()` showed
each claim only its own citations (comparative claims unjudgeable by
construction), the inventory step mutated claims before grading them, and
the entailment prompt punished faithful paraphrase. Path-A disclosure
sentences were separately being counted as uncited factual claims — honesty
penalized in the citation-coverage dimension (the −0.058 dip explained).

Decision (David, 2026-08-04): while golden v2 is in heavy development, **the
judge is the auditor** — `ADVISOR_JUDGE_BACKEND=cc` (default) judges via one
headless `claude -p` call per answer with the full titled evidence set;
model **Opus 5** (David: "the auditor should be opus-5"). Labels and scores
stay Python-computed, so label semantics are unchanged; the judge_model in
every projection severs the scoring contract between backends. Dev/QA only
per the Track H billing boundary — production judging must be API-billed.
The OpenAI pipeline stays selectable (`ADVISOR_JUDGE_BACKEND=openai`) with
its defects documented in place, un-fixed. Landed in `09c2261`.

Evidence, calibration, and the paired rejudge (`experiments.rejudge_v2`,
same bundles, fresh judgment, originals untouched):

- cc:opus vs the 32 assisted human labels: kappa 0.56 (terra 0.63), every
  miss a partial-boundary call, **zero false clean bills**. The replay uses
  stored title-less pairs, so the informational fixes cannot score here;
  the pending unassisted label batch (runbook §8) will give the real read.
- Core, same agent outputs (`2026-08-04_v2_core_cc_801628e_r1`): **13/49 →
  22/49**. `judged:faithfulness` failures 28 → 6; faithfulness mean 0.830 →
  0.955, citation coverage 0.776 → 0.915. Eleven up-flips; two down-flips,
  both defensible — v2c026 a genuine catch (platform-capability claim cited
  to two narrative passages that say nothing about it), v2c020 a mild
  strictness call (one over-extended claim + uncited analytic conclusions).
- Scripts (`2026-08-04_scripts_v2.0_cc_801628e_r1`): 0/4 unchanged — those
  failures are deterministic quote/coverage units, not judge labels.
- Judge cost, API-equivalent (subscription-billed in practice): ~$0.17/case,
  $7.98/core run, $1.67/scripts. Headless calls share the Max seat quota.

Remaining core failure profile is now dominated by real agent work:
truth_set 6, state_absence 6, judged:faithfulness 6, coverage_statement 5,
judged:citation_coverage 5, quotes_verbatim 2, must_cite 2,
correct_premise 2, surface_ambiguity 1. Next authorized step: Path B
(`scan_text`). Revisit triggers: unassisted calibration batch before
trusting cc-judge means as gates; audit a sample of cc-judge flags once
enough accumulate (the auditor-judge deserves the same skepticism the old
judge got).

## 2026-08-04 — Path A: coverage/honesty prompt rules (David-approved)

David approved the full Path A draft (2026-08-04); applied in `d8b1799`:
coverage/honesty rules 5–9 in `SYSTEM_PROMPT` (sampler-not-census disclosure,
absence-of-evidence wording, supported-matches-only, premise correction,
ambiguity surfacing, decline-and-offer-research), a sampler note on
`search_corpus`'s description, and a fix to the step-budget line that
literally instructed the agent to "conclude the material is not in the
corpus" on an empty search — the exact epistemic error the honesty tier
grades. Prompt-cache hash pin updated in the same commit.

Rerun at `d8b1799` (seed `v2-baseline`, judged, truth sets current 14/14;
manifests `2026-08-04_v2_smoke_d8b1799_r2`, `2026-08-04_v2_core_d8b1799_r2`,
`2026-08-04_scripts_v2.0_d8b1799_r2` — `_r1` files were empty artifacts of a
DB-auth crash, deleted):

- **Smoke 7/7** (was 7/7). **Core 13/49** (was 8/49; comparator: 6 fixed —
  v2c017, v2c020, v2c040, v2c044, v2c048, v2c054 — 1 broken — v2c024, lost
  on judged:faithfulness — net −5). **Scripts 0/4** (unchanged; multi-turn
  quote drift and turn-2 coverage overclaim were not targets of this change).
- Failing-unit shifts on core: coverage_statement 18→5, overclaimed entities
  16→10, truth_set 9→6, behavior:state_absence 8→6, correct_premise 3→2,
  surface_ambiguity 2→1, decline_action 1→0; must_cite 2→2, quotes_verbatim
  2→2, judged:citation_coverage 6→6, **judged:faithfulness 27→28** — flat,
  and now present on 28 of 36 failing cases: the faithfulness judge is the
  dominant remaining blocker.
- Metric means: precision 0.30→0.42, overclaims/case 1.14→0.71, recall
  0.450→0.407 (hedging trades a little recall), faithfulness 0.830→0.837,
  citation_coverage 0.835→0.776 (−0.058, at the measured ±0.05–0.07 noise
  floor). Comparator verdict PASS on all three slices.
- Baseline acceptance (`--accept`) remains David's call; next authorized step
  is Path B (`scan_text`). Revisit trigger: if the faithfulness-label wall
  persists after scan_text lands, audit the judge's synthesis_error label
  against the §5.2 numeric/entity-grounding diagnostics before touching the
  agent again.

## 2026-08-01 — Second review: five harness defects fixed; baseline re-run clean

A second-agent review rejected the 2026-07-31 baseline and named five
integration defects. All five verified real, fixed in `0fb175f`; truth sets
rebuilt under builder v2; H1 launcher in `aee9de2`; baseline re-run from the
clean commit `aee9de2` (manifests `2026-08-01_v2_smoke_aee9de2_r1`,
`2026-08-01_v2_core_aee9de2_r1`, `2026-08-01_scripts_v2.0_aee9de2_r1`).
Evidence: `tests/test_second_review_fixes.py` (23 regression pins; 122 total
passing).

- **The comparator gated nothing on v2** — it loaded only `question_id` rows,
  so two v2 manifests compared as zero records and printed PASS. Now: loads
  case/script rows, refuses empty/disjoint/unknown manifests (exit 2), reads
  the recorded contract + judge model, and treats moved bindings or oracles
  as incompleteness. The nightly compares all three slices against a
  per-slice baseline pointer and script failures reach the exit code.
- **`pass` covered only mechanical assertions.** `must_cite` was skipped
  entirely ("graded elsewhere" was nowhere), judge labels and behavior
  obligations never gated, zero quotes passed `must_quote_verbatim`. 21 of
  the first baseline's 49 core rows said `pass: true` while carrying judge
  failure labels, and the "clean class split" was an artifact — classes whose
  only check was the unimplemented `must_cite` passed automatically. `pass`
  now composes every declared graded unit (deterministic, truth, judged
  dimensions via labels, behavior obligations via a targeted behavior judge —
  `experiments/behavior.py`); an ungradeable unit makes the case UNGRADED,
  never passing. `synthesis_quality` is retired from the judged vocabulary
  until a grader exists (four cases declared a bar nothing measured).
  SCHEMA_VERSION=2 rides in the contract string
  (`golden-v2.0/s2/modes-…`), so pre-fix manifests can never gate against
  post-fix ones. **All 2026-07-31 baseline numbers are void** — 30/49, the
  class split, recall 0.318, 11 overclaims measured a weaker bar.
- **Truth keys were unmatchable for people/CV/investor scopes** (document
  titles like "Paul Jaminet — CV") and overclaim counted negated mentions.
  Builder v2 keys by real entity name; grading searches the answer for known
  names (finds `2ndCourt.com`, lowercase brands) and splits polarity first —
  "X does not mention it" is disclosure, not overclaim. Rebuild moved every
  digest; match/chunk counts identical, confirming only the keying changed.
- **The scripts manifest carried bodies** (rendered questions + full judge
  verdicts with claim text) in git for ~a day. All manifest writers now share
  `judge_manifest_projection`; the leaked file is untracked (content
  preserved owner-only in `.qa-artifacts/quarantine/`); a repo-level test
  scans every committed v2 manifest for body-carrying keys.
- **The first core manifest spanned three commits** (44+3+2 rows) because
  identity was stamped per row while commits landed under a live background
  run. Identity is now captured once at start, drift is detected and recorded
  on the summary row, and `--accept` refuses tainted manifests. Working rule:
  never commit while a run is live.
- **Nightly calibration read the wrong file** (`pending.jsonl`, reporting
  0/30) while judged rows correctly said human-calibrated from the canonical
  `experiments/judge_calibration.jsonl` (32 human labels). One source now.

**Re-run under the strict contract** (`golden-v2.0/s2/modes-ff08feef`, seed
`v2-baseline`, ~$2.9): smoke **7/7**; core **8/49** (0 ungraded); scripts
**0/4**. Not comparable to the 07-31 numbers by construction — the bar
changed, and the comparator refuses the pair. Core failing units:
judged:faithfulness 27, coverage_statement 18, truth_set 9,
behavior:state_absence 8, judged:citation_coverage 6, correct_premise 3,
quotes_verbatim 2, must_cite 2, surface_ambiguity 2, decline_action 1.
Truth-graded 14: mean entity recall 0.450 (people/CV now measurable),
overclaimed entities 16, verified-empty oracles respected 2/8. Behavior:
surface_conflict 2/2, ask_clarifying 1/1, disclose_capabilities 1/1 met;
state_absence 3/11, correct_premise 0/3, surface_ambiguity 0/2,
decline_action 0/1. Judge (n=45, human-calibrated): faithfulness 0.830,
citation coverage 0.853. No baseline accepted — `nightly.py --accept` is
David's call, as is the strategic read of these numbers.

Revisit triggers: baseline acceptance; re-adding `synthesis_quality` with a
grader; honesty→capability flips when `scan_text` ships (contract change).

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
