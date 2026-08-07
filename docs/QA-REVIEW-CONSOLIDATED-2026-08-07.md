# QA Design Review — Verification Pass and Consolidated Verdict

> **Date:** 2026-08-07
> **Status:** point-in-time review (third pass)
> **Repository snapshot:** `main` at `0462cec` (s6 baseline accepted at `87ac00e`)
> **Prior passes:** (1) a three-agent review by this reviewer conducted at
> `5b416d4`, before the s6 baseline was accepted (delivered conversationally,
> not as a doc); (2) [`QA-DESIGN-AND-DEEPEVAL-INDEPENDENT-REVIEW-2026-08-06.md`](QA-DESIGN-AND-DEEPEVAL-INDEPENDENT-REVIEW-2026-08-06.md)
> ("the independent review"), conducted at `87ac00e` with visibility into pass 1.
> **Method:** every load-bearing claim unique to either prior pass was
> re-verified directly against code, manifests, calibration JSONL, the baseline
> pointer, and DECISIONS at this snapshot; findings the two passes already
> agreed on were spot-verified once. Verification commands ran against the
> working tree, not commit messages or doc prose.
> **Not a decision record:** adopted choices belong in
> [`DECISIONS.md`](DECISIONS.md); readiness belongs in [`PLAN.md`](PLAN.md) §R.

## 1. Consolidated verdict

Both prior reviews agree on the essentials, and this pass confirms them: the
escalation-gate architecture (deterministic formulas detect, calibrated judges
dispose, formula verdicts preserved, must-fail labels ratchet against
leniency) is sound and should remain the default grading methodology. The
disagreements between the two passes were resolved by verification, and the
independent review wins the two arguments that matter most:

1. **The claim-judge's `human-calibrated` state overstates its provenance** —
   every one of the 32 "human" calibration labels was assisted, zero
   unassisted labels exist, and the state machine never checks. Pass 1's claim
   that the calibration machinery is "ahead of DeepEval" conflated the
   (strong) gate-policy replay with (pending) independent claim-judge
   calibration. Those are different properties and only the first exists today.
2. **Baseline promotion validates filenames, not artifacts** — pass 1 missed
   the promotion path entirely. Combined with the uncontained judge-transport
   failure mode, there is a real path from judge outage to a promotable
   partial manifest.

This pass also tempers the independent review in one place: the 0/4 script
baseline was a **documented, deliberate acceptance** (DECISIONS 2026-08-07
records the low-tail caveat, the 0/4→3/4 history, and the stable-failure work
list), not silent governance drift. The criticism that survives is the policy
asymmetry — replicate-median for core, single low-tail run for scripts — and
the absence of an explicit slice-acceptance policy.

Bottom line, unchanged from the independent review but now verified: **the QA
architecture is right; the control plane (promotion validation, calibration
provenance, failure containment, evaluator identity) has not yet caught up
with the grading semantics.** Finish the control plane before adding QA
surface area, DeepEval included.

## 2. Verification matrix

Every row below was verified in this pass at `0462cec`. "P1" = pass 1
(this reviewer, at `5b416d4`); "IR" = the independent review.

| # | Claim | Source | Verified against | Result |
|---|---|---|---|---|
| V1 | Judge transport failures (nonzero exit, timeout, malformed outer JSON, missing CLI) raise past the fail-safe; worker exceptions can abort a parallel run and strand partial manifests | P1 §1 / IR F4 | `judge_cc.py::_run_claude`, `adjudicate.py::_samples` (catches only `ValueError`/`ValidationError` post-transport), `run_v2.py` / `script_runner.py` future loops | **Confirmed** |
| V2 | Tie votes resolve leniently on negative-polarity gates (coverage overclaims, asserted names); no odd-`k` guard on `ADVISOR_ADJ_SAMPLES` | P1 §2 / IR F7 | `adjudicate.py::_majority` (1–1 → `False` → flag dismissed on negative framings) | **Confirmed** |
| V3 | Script turns never receive behavior escalation | P1 §3 / IR F5 | `escalate_behaviors` called in `run_v2.py` only; absent from `script_runner.py` imports and turn loop | **Confirmed** |
| V4 | P1's suggestion that V3 might explain scripts 0/4 | P1 | Accepted script manifest failure units; DECISIONS s5 entry | **Refuted** — all four scripts carry faithfulness failures; V3 explains one unit of one script (IR was right) |
| V5 | Rejudge wraps claim judging and adjudication in one `try`, discarding a successful judge verdict on gate error | P1 §4 / IR F8 | `rejudge_v2.py` | **Confirmed** |
| V6 | Calibration replay warns-and-excludes labels with missing owner-only bodies and can exit 0; all 46 bodies present at this snapshot | P1 §5 / IR F9 | `adjudicate_calibrate.py`, `.qa-artifacts/calibration/` | **Confirmed** (enforcement gap, not current data loss) |
| V7 | The accepted baseline pairs core r2 (39/49, median of 35/39/39 replicates) with the single 0/4 script run | IR F1 | `experiments/golden/baseline.json`, three `5b416d4` core manifests on disk, DECISIONS 2026-08-07 | **Confirmed**; severity tempered — acceptance was documented and deliberate, with caveat and work list recorded |
| V8 | `accept_baseline` checks existence/slice-uniqueness/drift only; the drift check fires only on summary rows that exist, so a summary-less partial passes; pointer metadata is taken from the *current* suite and truth manifest, not derived from the accepted run | IR F2 | `nightly.py::accept_baseline` (lines 95–128) | **Confirmed** — high severity in combination with V1 |
| V9 | All 32 `provenance:"human"` claim-judge calibration records have `assist_shown:true`; zero unassisted labels; `calibration_state` emits `human-calibrated` without inspecting `assist_shown` | IR F3 | `experiments/judge_calibration.jsonl` (counted: 32 human/assisted, 10 synthetic), `judge.py::calibration_state` | **Confirmed** — note `judge.py` comments record the assist honestly; the defect is the state naming plus the pending unassisted batch |
| V10 | No evaluator fingerprint: the scoring contract is a hand-bumped `SCHEMA_VERSION` plus a grading-modes hash; rubric text, `k`, tie policy, and calibration digest are not part of run identity | IR F6 | `cases.py::Suite.contract` | **Confirmed** |
| V11 | `schema_version` collision: core rows spread `suite.identity()` then `**row`, and the row carries `manifest_record()`'s bundle schema (3), overwriting the suite value (6); script rows report 6 | IR F10 | `run_v2.py:417`, `run_v2.py:328`, `bundle.py:70`, `cases.py:474` | **Confirmed** (mechanism exactly as described; comparator keys on `scoring_contract`, so provenance debt, not a scoring break) |
| V12 | `openai` and `posthog` already appear in the lockfile, weakening P1's dependency-hygiene warning about DeepEval | IR §8.2 | `uv.lock` | **Half-confirmed** — both present; `sentry-sdk` is **not**, so DeepEval would still add it. The dev-only-group recommendation stands either way |
| V13 | DECISIONS entries are future-dated relative to commits | P1 (design pass) / IR | `DECISIONS.md`, commit dates, today's date | **Now benign** — the 2026-08-07 entries describe overnight 08-06→07 work and today is 08-07; a one-line convention note (entry date = date written) would close it |

## 3. What the independent review changed in this reviewer's verdict

Concessions, stated plainly:

1. **Calibration provenance (V9) is the most important new finding of the
   week's reviews.** The correct statement is: the *gate-policy replay* is
   strong and worth keeping exactly as is; *independent claim-judge
   calibration* does not yet exist, and manifests currently claim otherwise.
   Pass 1 never opened `judge_calibration.jsonl`.
2. **The promotion path (V8) was a blind spot.** Pass 1 ranked the transport
   crash as the top defect; the sharper formulation is IR's — the crash plus
   the permissive promoter forms a control-plane path from judge outage to a
   promotable partial artifact.
3. **V4: pass 1 speculated where it could have checked.** The behavior-gap
   explanation for scripts 0/4 was wrong; the manifest settles it.
4. **Evaluator identity (V10) and the schema collision (V11)** are real
   findings pass 1 missed. Pass 1 flagged the seven-gate copy-paste as the
   place where tie policy diverged; IR generalized correctly — anything not
   fingerprinted (rubric text, `k`, polarity policy) can drift without
   severing comparability.
5. **The "~30-line adapter" framing for a DeepEval custom judge was
   understated.** A pilot-grade `DeepEvalBaseLLM` needs schema-aware output,
   sync+async paths, timeouts, JSON recovery, and cost routing through
   `cost.py`. Small project, not a snippet.

## 4. Where this pass tempers the independent review

1. **F1's framing.** "Not a trustworthy regression bar" is correct for the
   script slice, but the acceptance was transparent, reasoned, and recorded —
   the failure identities are enumerated in DECISIONS as the work list. The
   actionable residue is exactly IR's own recommendation: an explicit,
   documented slice-acceptance policy (replicated minimum, or non-gating
   diagnostic status until one exists). Recommend the latter as the immediate
   move, since a 3/4 run already exists in sequential history.
2. **Assisted labels are weak evidence, not zero evidence.** 32/32 assisted
   agreement still says something; anchoring makes it unquantifiable. The fix
   is cheap (honest state names) plus the one thing only David can supply: the
   pending unassisted batch.
3. **"Too positive" is half snapshot artifact.** At `5b416d4` no s6 baseline
   existed, and pass 1 explicitly flagged the untracked 35/49 + 0/4 manifests
   as needing resolution before anything built on s6. On promoter and
   provenance, however, the criticism is earned (see §3).
4. **On V10 enforcement:** deriving/validating the contract from an evaluator
   fingerprint is right. Note the policy already implies it — any rubric edit
   is decision-grade and replay-gated — so the fingerprint mostly automates a
   rule that exists; whether a fingerprint mismatch hard-fails comparison or
   warns pending acceptance is David's call.

## 5. One finding neither review ranked: the `unsupported` collision

DECISIONS 2026-08-07 flags (but defers) a cc-judge error class: list-scoped
claims ("which of these …") refuted using scan hits from *outside* the list,
landing in `unsupported` — which **by deliberate design never escalates**
(the never-absolve-fabrication hard line). This is the first observed case of
the hard line colliding with a demonstrated judge false-positive class, and it
plausibly contributes to the script faithfulness failures. It deserves a place
in the priority list rather than a "flagged, not decided" footnote, because
its resolution (adjusting the calibrated CC judge prompt) requires the same
unassisted calibration batch as V9 — the two should be done together so the
batch can measure the error class before and after.

## 6. Consolidated remediation sequence

This endorses the independent review's §10 sequence with three annotations.

- **P0 — restore baseline trust** (IR F2, F1, F3): harden `accept_baseline`
  into a strict artifact validator; declare the script slice non-gating
  diagnostic (or set a replicated minimum); rename calibration states
  honestly (`human-assisted` ≠ `human-calibrated`).
  *Annotation:* the unassisted human batch is David-gated — schedule it with
  the §5 judge-error-class decision so one batch serves both.
- **P1 — failure containment** (IR F4, F8, F9): per-sample transport
  containment in `_samples`; per-future containment with explicit
  `ERROR`/`UNGRADED` rows; stage separation in rejudge; missing calibration
  bodies → `INCOMPLETE`/nonzero.
- **P2 — parity and identity, one contract sever** (IR F5, F7, F6, F10):
  script behavior escalation, tie-as-abstention with odd-`k` guard, evaluator
  fingerprint, `bundle_schema_version`/`suite_schema_version` split; full
  calibration replay; sever s6 → s7 once.
  *Annotation:* do the gate-abstraction consolidation here, not later — seven
  copies of transport/quorum/fail-default logic is where V2 came from, and
  P2 touches most of them anyway. Both prior reviews independently converged
  on this refactor.
- **P3 — documentation**: RUNBOOK gains the normative grading flow
  (gates, replay, replicate policy, promotion preconditions); PLAN §R and
  HANDOFF refreshed; the date-convention note from V13.
- **P4 — DeepEval pilot, only after P0–P3** (see §7).

## 7. DeepEval — final position (both reviews concur)

- **Constraint verdict:** the `deepeval` pip package is Apache-2.0 and runs
  fully local (telemetry off via `DEEPEVAL_TELEMETRY_OPT_OUT=1`); the
  Confident AI platform — including its "self-hosted" Enterprise offering —
  is out of scope under the no-subscription constraint. No component of the
  hosted platform is load-bearing for anything recommended here.
- **Do not adopt as a harness.** Metrics, pytest runner, and dataset
  management duplicate a bespoke loop that has stronger project-specific
  semantics (corpus-derived truth, contract severing, baseline governance).
  Nothing in their OSS package resembles the calibration replay; on
  *gate-policy* tooling this repo is ahead of the framework that inspired it.
- **Borrow now, dependency-free:** frozen ordered evaluation steps in judge
  rubrics; failure-class routing to specialized sub-rubrics; banded
  score rubrics (range → named expected outcome); intention-ledger
  decomposition and bounded turn windows for script grading.
- **Pilot later (P4):** the Synthesizer (`generate_goldens_from_contexts` /
  `from_goldens`) as a *candidate generator* for the question bank, and
  optionally the Conversation Simulator for exploratory multi-turn traces —
  dev-only dependency group, local models via a properly scoped
  `DeepEvalBaseLLM` adapter routed through `cost.py`, outputs quarantined as
  candidates until human review and independent truth generation. Success
  metric: independently reviewed new failure classes found, not "the library
  runs".
