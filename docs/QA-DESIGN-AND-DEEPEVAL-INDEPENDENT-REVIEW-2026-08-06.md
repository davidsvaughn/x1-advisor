# X1 Advisor QA Design and DeepEval — Independent Review

> **Date:** 2026-08-06
> **Status:** Point-in-time independent review
> **Repository snapshot:** clean `main` at `87ac00e`, synchronized with
> `origin/main` when reviewed
> **Scope:** the X1 Advisor architecture, the QA-loop design and its evolution,
> the implementation work from the preceding week, the escalation-gate design,
> the current accepted golden-v2 baseline, and potentially useful parts of the
> DeepEval ecosystem
> **Not a decision record:** findings and recommendations in this document do
> not change the accepted design or authorize implementation. Decisions belong
> in [`DECISIONS.md`](DECISIONS.md), and current readiness belongs in
> [`PLAN.md`](PLAN.md) §R.

## Executive summary

The X1 Advisor's overall architecture remains a coherent fit for the product:
an interactive research agent with hybrid retrieval, narrow structured-query
tools, evidence-bound answers, validated citations, and a QA system built
around immutable run artifacts. The recent QA work is also directionally
strong. In particular, using deterministic checks as high-recall detectors and
escalating their disputed failures to narrowly scoped, replay-calibrated judges
is a better design than continuing to add brittle parsing exceptions.

The present `s6` baseline is nevertheless **not yet a trustworthy regression
bar for the complete suite**. The problem is not one isolated bad rubric. It is
the combination of three control-plane weaknesses:

1. The accepted baseline pairs a replicate-median core result of **39/49** with
   a single script run of **0/4**. That records the script suite's low tail as
   the bar and therefore provides little meaningful script-regression
   protection.
2. Accepted manifests report the claim judge as `human-calibrated`, even though
   all 32 calibration records marked `human` also say `assist_shown: true`.
   The separate escalation-gate replay is valuable, but it is a policy
   regression test assembled largely from motivating examples and synthetics;
   it is not an independent human calibration set.
3. The intended fail-safe boundary does not contain judge transport failures.
   A missing CLI, timeout, non-zero subprocess exit, or malformed outer JSON can
   abort a parallel run and leave a partial no-summary manifest. The baseline
   promoter can then accept that incomplete file because it does not validate
   completeness, summary presence, expected case IDs, or manifest identity
   against the pointer it writes.

Several narrower issues reinforce those concerns: scripts do not apply the new
behavior escalation used by single cases; ties can resolve leniently on
negative-polarity overclaim gates; evaluator identity is not independently
fingerprinted; and `schema_version` means two different things in core rows.

My assessment is therefore:

- **Architecture:** sound, with the readiness caveats already recorded in
  `PLAN.md`; this remains a validated TEST/admin prototype rather than a
  production-ready service.
- **Escalation-gate concept:** endorse and retain.
- **Current implementation:** promising but not yet internally complete across
  run, script, rejudge, calibration, promotion, and comparison paths.
- **Current baseline:** useful diagnostic evidence, but not a defensible full
  regression baseline until evaluator provenance, run completeness, and script
  treatment are corrected.
- **DeepEval:** do not replace or wrap the main harness with it. The Apache-2.0
  local package may be useful later for *candidate* question generation and
  exploratory multi-turn simulation. The hosted Confident AI platform does
  not fit the no-subscription requirement. Several evaluation patterns are
  worth borrowing without taking a dependency.

## 1. Review questions and method

This review answers five questions:

1. Does the high-level X1 Advisor architecture still fit the intended product?
2. Is the revised QA loop—especially escalation gates—conceptually sound?
3. Does the current implementation enforce the design across all execution
   paths and artifacts?
4. Is the accepted `s6` baseline a trustworthy bar for future comparisons?
5. Does DeepEval contain free/local components or ideas that materially improve
   this system?

The review was performed independently from the other agent's conclusions. I
first inspected the repository's canonical routing and design documents, then
traced current claims into code, manifests, calibration records, and the
accepted baseline pointer. Only after forming the independent findings did I
compare them with the supplied response.

Primary repository evidence included:

- [`AGENTS.md`](../AGENTS.md), [`PLAN.md`](PLAN.md) §R,
  [`ARCHITECTURE.md`](ARCHITECTURE.md), and
  [`ARCHITECTURE-REVIEW.md`](ARCHITECTURE-REVIEW.md);
- [`QA-RUNBOOK.md`](QA-RUNBOOK.md),
  [`QA-LOOP-DESIGN-2026-07-30.md`](QA-LOOP-DESIGN-2026-07-30.md),
  [`GOLDEN-V2-DESIGN-2026-07-31.md`](GOLDEN-V2-DESIGN-2026-07-31.md), and
  [`QUESTION-BANK.md`](QUESTION-BANK.md);
- the recent commit history and dated entries in
  [`DECISIONS.md`](DECISIONS.md);
- current run, script, rejudge, adjudication, comparison, promotion, bundle,
  fingerprint, truth-set, and judge implementations;
- the accepted baseline pointer and the manifests it names;
- the claim-judge and escalation-gate calibration JSONL files.

The external DeepEval assessment used its official repository, documentation,
and product materials. Hosted-platform capabilities were kept separate from
what the open-source Python package can run locally.

### Verification performed

At the reviewed snapshot:

| Check | Result |
|---|---:|
| `uv run pytest -q` | **213 passed** |
| `uv run python -m experiments.truth --golden v2 --check` | **14/14 truth sets current** |
| `git diff --check` | clean |
| `git status --short --branch` | clean `main`, synchronized with `origin/main` |
| adjudication calibration labels | 46 labels; 46 local bodies; no duplicate IDs |
| claim-judge calibration records | 42 total: 32 `human`, 10 `synthetic` |
| independent unassisted human claim-judge labels | **0** |

A green unit-test run is evidence that the tested mechanics work. It does not,
by itself, prove baseline completeness, evaluator provenance, calibration
independence, or equivalence among the case, script, and rejudge paths. Those
properties were reviewed separately.

## 2. High-level architecture assessment

### 2.1 What the system is designed to be

The current architecture consistently describes an interactive “research
buddy,” not a report generator and not an app-controlling copilot. Its core
shape is:

1. Receive a conversational research question, optionally with explicit page
   context.
2. Resolve it through hybrid retrieval over rendered X1 profiles, private
   documents, narrow structured queries, and the web.
3. Preserve retrieved evidence in a citation registry rather than letting
   generated summaries masquerade as sources.
4. Produce an answer whose citations are validated in code.
5. Persist owner-only rich turn bundles for diagnosis while projecting
   body-free manifests for committed regression evidence.

This division is appropriate. Semantic retrieval remains the spine for
open-ended research; parameterized structured tools handle questions where
precision matters; source-grounding is an enforced system property rather than
a prompt aspiration; and QA artifacts separate sensitive diagnostic bodies
from shareable run metadata.

### 2.2 What is especially strong

- **Product scope is clear.** The advisor researches; it does not mutate the
  app or revive the aborted copilot direction.
- **Retrieval and evidence are distinct.** Record summaries may help retrieval
  without automatically becoming citable evidence.
- **Structured access is narrow.** SQL-like precision is exposed through typed
  tools rather than turning the product into a SQL-first agent.
- **Citation validation is in code.** This materially reduces dependence on
  model compliance.
- **QA provenance is designed in.** Immutable manifests, turn fingerprints,
  truth digests, contract versions, and body-free projections are the right
  ingredients for reproducible evaluation.
- **Model/provider seams remain experimental.** The architecture does not make
  a bake-off winner permanent by accident.

### 2.3 Readiness boundary

The architecture should not be confused with production readiness. The current
`PLAN.md` §R still has open work in request authorization and thread ownership,
runtime limits/backpressure/SSE/budgets, context snapshots, and production data
coverage/freshness. The sanctioned environment remains TEST. The accurate
description is therefore: **a validated admin prototype with a strong research
and QA architecture, not a production-ready multi-user service**.

That distinction is not an architectural failure. It is the correct readiness
boundary to preserve while QA quality is being established.

## 3. How the QA loop evolved

The recent history matters because many score changes are contract changes or
grader corrections, not straightforward improvements in agent answer quality.

| Date | Milestone | Significance |
|---|---|---|
| 2026-07-30 | Gate 1 observability foundation | Turn bundles, fingerprints, retrieval explanations, immutable manifests, replay/compare plumbing, and the evidence-boundary correction made failures diagnosable. |
| 2026-07-31 | Golden v2 and headless-agent designs adopted | A suite with smoke, core, and multi-turn script slices replaced weak phase-exit shorthand; early strict results exposed several harness defects. |
| 2026-08-01 | Strict `s2` contract after harness corrections | Earlier numbers were invalidated; the low score became an honest capability measurement rather than a reason to relax the contract. |
| 2026-08-04 | Prompt Path A, headless Opus claim judge, and `scan_text` | Core capability reached 18/49 under a new modes contract; scan-based questions began receiving full-recall and overclaim checks. |
| 2026-08-05 | First accepted baseline and overclaim discipline | Baseline acceptance became explicit and committed; truth/census work continued. |
| 2026-08-06 | Truth tiers, registry/census work, and `s3`/`s4` escalation | Deterministic formula failures began escalating to narrowly scoped judges; calibration replay and leniency-ratchet cases were added. |
| 2026-08-06 | `s5` script parity for several gates | Quote, coverage, entity-intrusion, and related grading defects were addressed; a reported run reached core 41/49 and scripts 3/4. |
| 2026-08-06 | `s6` behavior gate and parallel harness | Behavior obligations received escalation in the case path; core was run three times (35/39/39) to measure variance. The accepted core became the median/consensus-nearest 39/49 run. The accepted script manifest, however, is 0/4 from one replicate. |

Two cautions apply to this timeline:

1. The newest `DECISIONS.md` entries are dated 2026-08-07 even though the
   reviewed repository state and commits were observed on 2026-08-06. Some
   adjudication records likewise have a future `ratified` date. This should be
   corrected or explicitly explained so chronology remains trustworthy.
2. A movement from 22 to 37 or 41 is largely the grader ceasing to reject good
   answers for formula limitations. It is an important QA improvement, but it
   should not be described as an equivalent jump in underlying product answer
   quality.

## 4. Current QA design

### 4.1 Suite and artifacts

Golden v2 contains three slices:

- 7 smoke cases;
- 49 core cases;
- 4 multi-turn scripts.

Runs write JSONL manifests under `experiments/runs/`. Rich answer and evidence
bodies live outside git in owner-only artifacts. Committed manifests retain the
case identity, fingerprints, scores, grader outputs, contract metadata, and
body-free adjudication summaries needed for comparison.

Machine truth sets are generated from the TEST corpus for enumerative and
state-sensitive questions. Their digests are recorded so a corpus movement is
not silently mistaken for an agent regression. The suite contract
`golden-v2.0/s<N>/modes-<hash>` deliberately severs comparability when grading
semantics are manually advanced.

### 4.2 Three grading layers

The loop combines:

1. **Deterministic checks** for properties that can be mechanically verified:
   expected entities, names, state absence, coverage counts, quote matching,
   and similar assertions.
2. **Claim judging** for answer faithfulness against titled evidence. The
   current backend is headless Claude/Opus in dev/QA, isolated from repository
   context and denied tool access.
3. **Behavior obligations** for answer qualities such as explicitly stating a
   meaningful absence rather than merely avoiding fabrication.

### 4.3 Escalation gates

The escalation design treats deterministic formulas as detectors, not as
infallible readers. A clean deterministic pass spends nothing. A formula-flagged
failure may be escalated to a specialized judge that considers the answer in
context and returns structured votes. The formula result is preserved beside
the adjudication. “Must fail” labeled cases protect against rubric changes that
would make the judge more lenient than the established policy.

This is a sound pattern for the error types observed:

- honest coverage language can defeat a narrow regex;
- typography and editorial conventions can make valid quotations look
  different byte-for-byte;
- an evidence-surfaced entity can look “invented” to a registry-only formula;
- a reader can understand a state absence even if the exact parenthetical in a
  machine rubric is not repeated.

The crucial safety property is polarity: a judge may rescue a detector false
positive, but should not absolve a truly unsupported or unverifiable claim.
The current faithfulness gate preserves that structural boundary by refusing to
adjudicate fabrication-like claim states.

### 4.4 What the calibration replay does—and does not do

The 46-label adjudication replay is valuable. It runs recorded examples through
the production gate functions, measures agreement, and fails on must-fail
leniency breaches. That turns rubric edits into observable, reviewable changes.

It is best described as a **policy regression suite**. It is not an independent
estimate of judge generalization because many labels originate in the failures
that motivated each gate, some answers contribute several correlated labels,
and synthetic must-fails are intentionally obvious. A perfect replay therefore
means “the current implementation reproduces the ratified policy on this
fixture set,” not “the judge is independently calibrated on future answers.”

That distinction is central to the findings below.

## 5. Detailed findings

### F1 — High: the accepted script baseline is not a meaningful regression bar

**Evidence**

`experiments/golden/baseline.json` accepts:

- core: `2026-08-06_v2_core_5b416d4_r2.jsonl` — **39/49**;
- smoke: `2026-08-06_v2_smoke_64d063d_r1.jsonl` — **7/7**;
- scripts: `2026-08-06_scripts_v2.0_5b416d4_r1.jsonl` — **0/4**.

The core choice was informed by three same-seed replicates scoring 35, 39, and
39. The script choice came from one run. `DECISIONS.md` itself calls that script
result the “low tail” and records sequential history from 0/4 to 3/4 on fresh
answers.

The four accepted script failures are not one uniform behavior-gate failure:

- `v2s004`: turn 2 faithfulness;
- `v2s002`: turn 2 faithfulness;
- `v2s003`: turn 1 faithfulness and turn 2 `state_absence` behavior;
- `v2s001`: turns 3 and 4 faithfulness.

**Why it matters**

A zero baseline protects only against results below zero, which is impossible.
It can still detect metadata drift or changed failure identities, but it does
not provide a useful “do not make scripts worse” quality floor. It also mixes a
variance-aware core policy with a single-replicate script policy.

The CLI semantics are incoherent with acceptance: `script_runner` exits nonzero
unless every script passes, while `run_v2` reserves nonzero exit mainly for
stale truth. Accepting 0/4 does not make a full nightly green; it records a bar
that the script command itself defines as failure.

**Recommendation**

Do not treat the current script pointer as an accepted quality floor. Decide and
document one of two policies:

1. baseline slices are promotable only after meeting an explicit minimum and a
   replicate policy; or
2. a failing slice is retained as diagnostic history but marked non-gating
   until an acceptable run exists.

Use equivalent variance treatment for core and scripts, with stable run IDs and
an explicit consensus-selection rule.

### F2 — High: baseline promotion validates filenames, not a complete run

**Evidence**

`experiments/nightly.py::accept_baseline` currently verifies that each named
file exists, that filenames map to distinct slices, and that any summary row it
encounters does not report identity drift. It does **not** require:

- a summary row;
- the expected case or script IDs;
- the expected row count;
- complete grading;
- agreement between manifest suite/contract/digest/seed and the pointer;
- a clean run identity assembled from the manifest;
- the expected slice recorded inside the artifact.

It then loads the *current* suite and truth manifest and writes those values to
the baseline pointer, rather than deriving and validating them from the run
being accepted.

**Why it matters**

A partial, stale, or no-summary manifest can be promoted under current suite
metadata it did not actually use. The pointer can therefore look internally
current while naming an artifact that is incomplete or was evaluated under
different identity.

This is not hypothetical in design terms: the transport issue in F4 can leave
partial manifests without a summary.

**Recommendation**

Make promotion a strict artifact validator. For every slice, require a unique
summary, exact expected IDs and count, complete grading status, no duplicate
rows, no identity drift, consistent seed/commit/suite/contract/digests, and a
slice-specific pass policy. Build the pointer from validated manifest identity,
then cross-check it against the current suite. Refuse promotion on any missing
or ambiguous field.

### F3 — High: `human-calibrated` overstates claim-judge provenance

**Evidence**

`experiments/judge_calibration.jsonl` contains 42 records:

- 32 with `provenance: "human"`;
- 10 with `provenance: "synthetic"`.

Every one of the 32 `human` records also has `assist_shown: true`. The count of
unassisted human labels is zero. Code comments in
`x1_advisor/agent/judge.py` and `QA-RUNBOOK.md` explicitly say a fresh
unassisted draw remains pending.

Nevertheless, `judge.py::calibration_state` counts any record with
`provenance == "human"` and a label. At 30 records it emits
`state: "human-calibrated"`; it does not inspect `assist_shown`. Accepted
manifests inherit that stronger claim.

**Why it matters**

Human ratification after seeing an assistant proposal is useful, but it does
not measure independent human/judge agreement. Assistance can anchor the
reviewer and makes the two labels statistically dependent. The current state
name tells later readers that a stronger validation occurred than the data
supports.

The 46-item escalation replay cannot substitute for this missing provenance.
It validates specialized gate policy on a correlated fixture set; the claim
judge evaluates answer faithfulness more broadly.

**Recommendation**

Change the state machine to distinguish at least:

- `uncalibrated`;
- `synthetic-only`;
- `human-assisted`;
- `human-calibrated` only after the minimum unassisted batch.

Record counts for assisted and unassisted labels separately in every manifest.
Draw and label the pending unassisted set before making calibrated performance
claims. Keep the policy-replay result as a separate field rather than folding
it into claim-judge provenance.

### F4 — High: judge transport failures escape the intended fail-safe

**Evidence**

`x1_advisor/agent/judge_cc.py::_run_claude` raises on:

- a missing `claude` executable;
- subprocess timeout;
- nonzero CLI exit;
- malformed outer CLI JSON.

`experiments/adjudicate.py::_samples` drops parse or Pydantic-validation
failures only *after* a transport call returns. Exceptions from the transport
escape the sample pool. The parallel loops in `experiments/run_v2.py` and
`experiments/script_runner.py` do not contain all worker exceptions at the case
or turn boundary. `script_runner` calls `future.result()` without a general
failure conversion; `run_v2` specifically catches stale truth, not arbitrary
judge failure.

**Why it matters**

The documented intent is “unusable judge output means the deterministic
failure stands.” That intent holds for some parse failures but not for
transport failures. One quota error, timeout, CLI failure, or malformed wrapper
can abort the run while other workers continue consuming cost during pool
shutdown. The manifest may contain only the rows written before the exception
and no summary.

Together with F2, this creates a control-plane path from judge outage to a
promotable partial artifact.

**Recommendation**

Contain failures at two levels:

1. In `_samples`, convert each transport/parse/validation failure into an
   unusable sample with structured error telemetry; allow the gate's declared
   fail-default to decide.
2. At each case/script future boundary, catch unexpected worker exceptions,
   record a terminal `ERROR`/`UNGRADED` row, and continue or cancel according to
   an explicit run policy. Never silently record the case as a pass.

Require the final summary to report error counts and make any error
non-promotable. Avoid broad catches that hide programming defects: preserve the
exception class and bounded message in local diagnostics.

### F5 — Medium-high: behavior escalation is absent from the script path

**Evidence**

Single cases call `escalate_behaviors` in `experiments/run_v2.py`. Script turns
evaluate behavior obligations in `experiments/script_runner.py`, then call
`escalate_assertions`; they never import or invoke `escalate_behaviors`.

The `s6` script manifest projection can carry adjudication keys, but the script
execution path cannot produce behavior adjudications. The concrete current
impact is `v2s003` turn 2, whose `state_absence` obligation fails without the
same reader-substance escalation available to a single case.

**Why it matters**

One scoring contract should mean the same grading semantics across slices. An
equivalent answer can currently receive behavior escalation as a core case and
not as a script turn.

This gap does **not** explain the complete 0/4 script result: every current
script also has at least one faithfulness failure. It explains one failing unit,
not the whole slice.

**Recommendation**

Route script behavior results through the same escalation function and tests as
core cases. Add a parity test that feeds equivalent case and script-turn inputs
through all applicable dimensions. Because this changes grading semantics,
replay calibration and sever the scoring contract.

### F6 — Medium: evaluator identity is incomplete

**Evidence**

`experiments/cases.py::Suite.contract` includes a manually advanced schema
version and a modes hash. `x1_advisor/agent/bundle.py::judge_manifest_projection`
records judge model, calibration state, provenance, and adjudication summaries.
`x1_advisor/fingerprint.py::turn_fingerprint` identifies agent behavior.

There is no independently derived evaluator fingerprint covering, for example:

- judge and gate rubric text;
- structured output schemas;
- calibration-set digest;
- `ADVISOR_ADJ_SAMPLES`;
- tie and fail-default policy;
- judge evidence-window settings;
- relevant evaluator code/version.

**Why it matters**

Changing `k`, a rubric, or evaluator logic without remembering to bump
`SCHEMA_VERSION` can leave runs apparently comparable. The agent fingerprint
cannot solve this because agent behavior and evaluator behavior are distinct
identities.

**Recommendation**

Add an evaluator fingerprint derived from normalized rubrics, schemas,
calibration digest, sample count, polarity/fail-default policy, evidence
settings, backend/model identity, and evaluator code version. Store it on every
graded row and summary; enforce it in compare and promotion. Keep the semantic
contract as a human-readable release marker, but derive or validate it against
the fingerprint rather than trusting a manual bump alone.

### F7 — Medium: tie handling is lenient on negative-polarity gates

**Evidence**

`experiments/adjudicate.py::_majority` returns `False` when true and false votes
tie. For positive questions such as “is this adequate?”, false is conservative.
For negative questions such as “is this an overclaim?” or “was this asserted?”,
false dismisses the detector's flag.

With `k=3`, one unusable sample plus a 1–1 split therefore resolves leniently on
coverage-claim and asserted-name paths. The sample-count environment variable
is not constrained to a positive odd value.

**Why it matters**

The same shared vote primitive has different safety behavior depending on
question polarity. On the overclaim gates—the place the leniency ratchet is
most important—a degraded sample set can waive a flagged failure.

**Recommendation**

Return `None` for ties and route it through the gate's explicit fail-default.
Require a positive odd sample count, or define a quorum and abstention policy
that remains conservative under missing samples. Prefer positively phrased
schemas where practical, and encode polarity in the shared gate abstraction so
it is reviewable rather than implicit.

### F8 — Medium: rejudge can discard a successful claim verdict

**Evidence**

`experiments/rejudge_v2.py` places claim judging and subsequent adjudication in
one broad `try` block. If adjudication raises after the claim judge succeeded,
the row becomes `UNGRADED`; the successful claim verdict is discarded.

**Why it matters**

Rejudge is not failure-equivalent to the live path and loses good evidence. It
also makes it harder to distinguish a claim-judge failure from a gate failure.

**Recommendation**

Separate the stages and persist their outcomes independently. A gate outage
should preserve the successful claim verdict and retain the deterministic
failure according to the gate fail-default.

### F9 — Medium: calibration replay can succeed with incomplete usable input

**Evidence**

`experiments/adjudicate_calibrate.py` reports labeled records whose owner-only
bodies are missing and excludes them. Its mandatory nonzero condition centers
on must-fail leniency breaches; general disagreement and some missing-body
conditions can still result in exit zero.

The reviewed checkout happened to have all 46 bodies, so the present replay was
complete. The issue is enforcement, not current data loss.

**Why it matters**

If local calibration artifacts are moved or lost, the ratchet can become weaker
while the command still appears successful. Completeness is part of calibration
identity.

**Recommendation**

Record the expected label IDs and calibration digest in a committed index.
Require every required body, especially every must-fail body, before reporting a
passing replay. Distinguish `PASS`, `FAIL`, and `NOT COMPARABLE/INCOMPLETE` in
both exit status and machine-readable output.

### F10 — Low-medium: `schema_version` is overloaded in core manifests

**Evidence**

Accepted core case rows carry `schema_version: 3`, while their summary uses
schema 6 and the scoring contract is `s6`. `bundle.py::manifest_record` emits
the turn-bundle schema under `schema_version`. In `run_v2`, the row projection
is spread after suite identity, so the bundle field overwrites the suite field.
Script rows follow a different projection and report 6.

**Why it matters**

The comparator currently keys primarily on `scoring_contract`, so this is not
an immediate scoring break. It is a provenance ambiguity: one field means
bundle schema on core rows and suite/evaluator schema elsewhere.

**Recommendation**

Rename and retain both meanings explicitly, for example
`bundle_schema_version` and `suite_schema_version`. Add a manifest-schema test
for every row type and slice.

## 6. Cross-cutting implementation assessment

### 6.1 What is working well

- The gates preserve formula results rather than rewriting history.
- Faithfulness escalation does not adjudicate unsupported/unverifiable claims
  into supported ones.
- Calibration replay executes production functions rather than a parallel toy
  implementation.
- Must-fail fixtures explicitly guard the leniency direction.
- Parallel workers use local state and a shared judge semaphore; tracker access
  is locked.
- Run files use exclusive creation and downstream comparison keys by case ID,
  avoiding reliance on worker completion order.
- Same-seed core replicates exposed substantial judge/agent variance rather
  than hiding it with rerun-until-green behavior.
- Body-free manifest projections and owner-only detailed bodies remain the
  right privacy/provenance split.

### 6.2 The main design debt

The gate pattern is repeated across specialized functions. Rubric text must be
specialized, but transport, sample validation, quorum/tie policy, fail-default,
summary projection, and telemetry should share one abstraction. The current
duplication makes it easy for polarity and error handling to diverge across
gates and execution paths.

A useful abstraction would make these dimensions explicit:

```text
detector result
  -> escalation eligibility
  -> rubric + structured vote schema
  -> sample transport and validation
  -> quorum / tie / abstention policy
  -> polarity-aware fail-default
  -> formula result + adjudication result + provenance
```

The abstraction must not homogenize domain rubrics or introduce a generic judge
that can waive any failure. Its purpose is consistent control-plane semantics.

## 7. Documentation assessment

The fastest-moving implementation is ahead of the living documentation.

### 7.1 Current drift

- `PLAN.md` §R still reports core 18/49, scripts 0/4, and no accepted baseline.
- `HANDOFF.md` still describes the 2026-08-04 state and says the next task is an
  overclaim-discipline proposal.
- `QA-RUNBOOK.md` describes the claim judge but does not yet explain `s3`–`s6`
  escalation gates, replay calibration, the leniency ratchet, replicate-median
  policy, or the difference between claim-judge calibration and gate-policy
  replay.
- The newest `DECISIONS.md` entries cover the `s5`/`s6` work, but their
  2026-08-07 dates are ahead of the reviewed 2026-08-06 repository state.

### 7.2 Recommended documentation update

After the baseline semantics are decided:

1. Update `QA-RUNBOOK.md` with a normative end-to-end grading flow, failure
   containment policy, calibration taxonomy, promotion preconditions, and
   replicate policy.
2. Refresh `PLAN.md` §R with the current measured state while clearly labeling
   the accepted 0/4 script issue.
3. Replace `HANDOFF.md` with a new point-in-time handoff.
4. Correct or explain future-dated decision and ratification records.
5. Keep this review as evidence and put the actual adopted choices in
   `DECISIONS.md`.

## 8. Assessment of the other agent's response

The supplied response was useful and found several real issues. Its central
recommendation—to retain the escalation-gate architecture—was right. Its
overall verdict was, however, too positive because it treated calibration and
baseline acceptance more strongly than the artifacts justify.

### 8.1 Where I agree

| Other response | Independent assessment |
|---|---|
| Escalation is preferable to continuing to expand brittle deterministic parsers. | Agree. This is the right default pattern for reader-substance disagreements. |
| Judge transport failure can abort the parallel run. | Agree; verified, and the interaction with permissive baseline promotion raises its severity. |
| Tie handling is lenient on overclaim-polarity gates. | Agree; verified. |
| Script turns omit behavior escalation. | Agree; verified. |
| Unsupported/unverifiable faithfulness claims are structurally protected from rescue. | Agree; this is an important strength. |
| Living documentation lags the implementation. | Agree. |
| DeepEval OSS and the Confident AI platform must be evaluated separately. | Agree. |
| Synthesizer and Conversation Simulator are the most plausible local components to explore. | Agree, with stricter limits on how their output enters the golden set. |

### 8.2 Where I differ

#### The claim that calibration is “ahead” is not supportable as written

The response praised calibration replay, kappa, and the leniency ratchet. Those
are good policy-regression mechanisms. It did not inspect—or at least did not
report—the `assist_shown` provenance of the separate claim-judge calibration
set. All 32 records counted as human calibration were assisted. Consequently,
the manifest claim `human-calibrated` is not supported by an independent human
batch.

My distinction is:

- **gate-policy replay:** strong and worth keeping;
- **independent claim-judge calibration:** still pending;
- **generalization evidence:** not established by replaying motivating and
  synthetic fixtures.

#### The baseline problem is larger than the script behavior gap

The response correctly noticed 0/4 scripts and suggested the missing behavior
call might plausibly explain it. In the current accepted manifest, only one
failing unit is a behavior obligation; all four scripts have faithfulness
failures. The missing call is real but is not a complete diagnosis.

More importantly, the later repository state accepted the 0/4 script file as
the baseline. That turns the concern from “an unresolved untracked run” into a
baseline-governance issue. The weak promoter makes this more serious than the
response recognized.

#### “The parallel harness reviewed clean” is too broad

The concurrency mechanics themselves are mostly sound: locks, local worker
state, output ordering, and the judge semaphore are reasonable. The
orchestration boundary is not clean because worker exceptions can escape,
continue consuming cost during shutdown, and leave partial manifests. A fairer
statement is: **the parallelization primitives appear sound; terminal failure
handling and artifact completion do not**.

#### Evaluator identity was missed

The response discussed contract severing but did not identify that evaluator
semantics are still manually versioned and incompletely fingerprinted. This is
central to whether two green comparator outputs are actually comparable.

#### Manifest schema collision was missed

The same `schema_version` field currently represents bundle schema in core rows
and suite/evaluator schema in summaries and scripts. This is lower severity but
important provenance debt.

#### Its documentation finding became partly stale

The response said `s6` and the parallel harness had no `DECISIONS.md` entry.
That was fair for the earlier snapshot it saw. At `87ac00e`, those entries exist;
the remaining problem is that they are future-dated and living readiness docs
still lag. This is a snapshot difference, not a reasoning error.

#### The proposed DeepEval adapter was understated

A reliable `DeepEvalBaseLLM` integration is more than a small shell wrapper if
it is expected to work across Synthesizer and Simulator flows. It needs
schema-aware Pydantic output, synchronous and asynchronous generation,
timeouts, JSON recovery, concurrency limits, cost tracking, and deterministic
error handling. That is feasible, but it should be scoped as a real dev-tool
adapter rather than assumed to be a throwaway 30-line seam.

The response also warned that DeepEval's transitive `openai` and `posthog`
dependencies should not enter production. The architectural conclusion—keep a
pilot dev-only—is correct, but those packages already appear transitively in
the current lockfile, so the warning is less dispositive than presented.

### 8.3 Overall assessment of the response

The other review was strongest as a code-level scan for immediate defects and
as an initial DeepEval survey. It was weaker as an audit of the current
baseline's evidentiary meaning. It saw several bugs but did not fully inspect
the promotion contract, calibration provenance, evaluator identity, or the
accepted manifest after the later commit.

My final verdict is therefore more conservative: **the QA architecture is
strong, but the baseline control plane is not yet strong enough to certify the
current `s6` suite as a trustworthy regression bar**.

## 9. DeepEval research

### 9.1 Free/local boundary

DeepEval's Python package is open source under Apache-2.0 and supports local
execution with user-supplied models. The hosted Confident AI product is a
separate service. Its free tier is limited, and product self-hosting is an
Enterprise offering. Under the explicit no-monthly-subscription requirement:

- **DeepEval OSS package:** eligible for a contained dev-only pilot;
- **Confident AI hosted platform:** out of scope;
- **Confident AI self-hosted platform:** out of scope unless its commercial
  constraint changes.

If the OSS package is tried, set `DEEPEVAL_TELEMETRY_OPT_OUT=1` because the
official environment-variable documentation says telemetry is enabled by
default. Do not require a Confident AI login or remote dataset/test-run upload.

Official references:

- [DeepEval GitHub repository](https://github.com/confident-ai/deepeval)
- [DeepEval introduction](https://deepeval.com/docs/introduction)
- [DeepEval environment variables](https://deepeval.com/docs/environment-variables)
- [Confident AI product and self-hosting FAQ](https://www.confident-ai.com/)

### 9.2 Component assessment

| Component | Verdict | Rationale |
|---|---|---|
| Synthesizer | Later, contained pilot | Could generate candidate questions and adversarial variants from corpus contexts. Its output must be manually reviewed and independently grounded before entering the question bank. It must never promote generated “goldens” directly. |
| Conversation Simulator | Later, contained pilot | Could expand exploration beyond four hand-authored scripts and exercise multi-turn state. Generated traces should be exploratory inputs graded by the existing harness, not accepted baselines. |
| Simulation Graph | Borrow or pilot with Simulator | Branching scenarios and conditional user behavior are useful for research conversations, especially follow-up and ambiguity handling. Keep the graph as scenario generation, not truth. |
| G-Eval-style evaluation steps | Borrow the pattern | Explicit, frozen evaluation steps make rubrics easier to review and calibrate. The repository already has the more important detector-plus-gate safety boundary. |
| DAG metric | Borrow routing ideas only | Routing failure classes to specialized rubrics is useful, but DeepEval DAG judgment nodes are still LLM calls; they do not replace deterministic detectors. |
| Conversation completeness | Borrow decomposition | Extract intentions, judge each intention's satisfaction, and report the ratio/failed intentions. This can strengthen script diagnostics without adopting the metric class. |
| Generic RAG metrics | Skip for now | They duplicate a bespoke harness that has corpus-derived truth, citation validation, contract severing, and product-specific obligations. |
| DeepEval pytest runner/dataset management | Skip for now | The current immutable manifests, comparator, and explicit baseline pointer carry stronger project-specific semantics. |
| Hosted dashboards/annotation workflows | Skip | Subscription and platform coupling violate the stated constraint. |
| `deepteam` red teaming | Park | Local/open-source adversarial probing may become useful for corpus-borne prompt injection, but it is not the current measured bottleneck. |

Relevant feature references:

- [Synthesizer](https://deepeval.com/docs/golden-synthesizer)
- [Conversation Simulator](https://deepeval.com/docs/conversation-simulator)
- [Simulation Graph](https://deepeval.com/docs/conversation-simulator-simulation-graph)
- [DAG metric](https://deepeval.com/docs/metrics-dag)
- [Conversation Completeness](https://deepeval.com/docs/metrics-conversation-completeness)
- [Custom model integration](https://deepeval.com/guides/guides-using-custom-llms)
- [LLM-as-a-judge guidance that inspired the gate discussion](https://deepeval.com/blog/llm-as-a-judge)

### 9.3 What to borrow now without adding a dependency

Three patterns have immediate value:

1. **Frozen evaluation steps.** State the ordered questions a judge must answer,
   not only a prose rubric and final boolean.
2. **Failure-class routing.** Keep specialized rubrics for quotation,
   enumerative coverage, entity intrusion, and behavior rather than allowing one
   general judge to waive unrelated detectors.
3. **Intention-level multi-turn diagnosis.** Track the user's outstanding
   intentions across a script and judge satisfaction individually, with a
   bounded turn window where appropriate.

These ideas can be implemented in the existing harness and included in its
evaluator fingerprint and calibration replay.

### 9.4 Conditions for a future pilot

A DeepEval pilot should begin only after the current baseline control-plane
findings are resolved. It should:

- live in a dev-only dependency group;
- run locally without a Confident AI account;
- opt out of telemetry;
- route every model call through the repository's cost/provenance layer;
- use a schema-aware sync/async custom model adapter;
- save outputs as candidate artifacts outside the accepted golden corpus;
- require human review and independently generated truth before promotion;
- compare the value of discovered cases against the maintenance and dependency
  cost.

The first experiment should be small: generate candidate variants for a narrow
subset of existing question-bank contexts, then measure how many survive human
review and expose a genuinely new failure class. Do not begin by integrating
DeepEval into nightly scoring.

## 10. Recommended remediation sequence

### Priority 0 — restore baseline trust before adding QA surface area

1. Harden baseline promotion (F2).
2. Reclassify the 0/4 script artifact as diagnostic/non-gating or establish a
   valid replicated script baseline (F1).
3. Correct calibration-state provenance and run the independent human batch
   (F3).

**Exit criteria**

- Every accepted slice has a complete, summary-bearing, identity-consistent
  manifest with exact expected IDs.
- Baseline metadata is derived from and agrees with the manifest.
- The script slice has an explicit, meaningful acceptance policy.
- Manifests distinguish assisted from unassisted calibration and do not claim
  `human-calibrated` without the required unassisted batch.

### Priority 1 — make failure containment match the design

1. Contain judge transport errors per sample and per case/script future (F4).
2. Separate claim judge and adjudication errors in rejudge (F8).
3. Make calibration incompleteness non-passing (F9).

**Exit criteria**

- Injected timeout, nonzero exit, missing CLI, and malformed JSON tests do not
  abort the whole pool or produce promotable partial artifacts.
- Error rows and summaries are explicit and machine-readable.
- No successful claim verdict is discarded because a later gate failed.
- Missing required calibration bodies yield `INCOMPLETE`/nonzero.

### Priority 2 — establish semantic parity and evaluator identity

1. Add behavior escalation to scripts and parity tests (F5).
2. Make ties abstentions and validate sample count/quorum (F7).
3. Add evaluator fingerprinting and enforce it in compare/promotion (F6).
4. Separate bundle and suite schema fields (F10).
5. Replay the full calibration set and sever the contract.

**Exit criteria**

- Equivalent core and script-turn inputs receive the same applicable grading
  semantics.
- Missing samples and ties cannot resolve leniently by polarity accident.
- Any rubric, schema, `k`, fail-default, calibration-set, or evaluator-code
  change makes runs non-comparable unless explicitly accepted.

### Priority 3 — absorb the design into living documentation

Update `QA-RUNBOOK.md`, `PLAN.md` §R, `HANDOFF.md`, and `DECISIONS.md` as
described in §7. Correct chronology before using dates as provenance.

### Priority 4 — consider a DeepEval discovery pilot

Only after Priorities 0–3, run a narrow Synthesizer or Conversation Simulator
experiment under the conditions in §9.4. Success is not “the library runs.”
Success is a measurable increase in independently reviewed, product-relevant
failure coverage that the existing process would not have found cheaply.

## 11. Final conclusion

The recent work has moved X1 Advisor from an opaque pass/fail loop toward a
serious evidence-bearing evaluation system. The escalation-gate idea is a real
advance: formulas detect, specialized judges interpret, formula results remain
visible, and labeled must-fails constrain leniency. The same week also exposed
the next maturity boundary. A grading system is trustworthy only when its
evaluator provenance, run completeness, execution-path parity, and baseline
promotion are enforced as strictly as its individual rubrics.

The right next move is not a larger framework integration. It is to finish that
control plane: make incomplete runs unpromotable, make calibration claims match
their provenance, establish a meaningful script bar, contain judge failures,
and fingerprint the evaluator. Once those properties hold, DeepEval can be
used selectively as a local source of candidate questions and exploratory
conversations—never as a shortcut around independent truth or baseline
governance.
