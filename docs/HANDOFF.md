# Handoff — scan_text live, capability contract live; next: overclaim discipline

> Date: 2026-08-04 (evening). Status: **point-in-time handoff**, written after
> the scan_text build + tool_ready flip session. Supersedes the morning
> handoff (that brief's scope — build `scan_text` — is DONE, flipped, and
> baselined). Authorized next scope: **an overclaim-discipline prompt rule**,
> proposal owed to David — do NOT apply prompt wording without his explicit
> approval (standing rule).

## 0. Read first (in this order)

1. [`../AGENTS.md`](../AGENTS.md) — standing rules; non-negotiable.
2. [`PLAN.md`](PLAN.md) §R — readiness matrix + gate sequence (current truth).
3. [`DECISIONS.md`](DECISIONS.md) — the top entry ("scan_text shipped;
   tool_ready flipped") is the complete record of 2026-08-04's second half.
4. [`QA-RUNBOOK.md`](QA-RUNBOOK.md) — §8.0 is the current judge; §1 runner
   commands.

## 1. Where things stand (one paragraph)

`scan_text` is live (`2934f7a`): the bounded exhaustive scan engine lives in
`x1_advisor/scan.py`, shared by the agent tool and the truth-set builder —
one code path, digest-proven. Adoption on first contact was 100%, so David
approved the `tool_ready` flip (`fdba68a`): the 14 scan cases are
capability-graded (full recall + zero overclaims), the scoring contract
moved to `modes-bd3235eb`, and nothing before the flip is comparable to
anything after. The judge got a completeness fix in the same commit (missing
per-claim verdicts now retry once, then leave the case UNGRADED — never a
coerced label). The first capability trio is committed (`285902e`): smoke
**7/7** · core **18/49** · scripts **0/4**, seed `v2-baseline`, all at
`fdba68a`. It is the candidate baseline; acceptance is David's call.

## 2. The next build: overclaim discipline (proposal stage)

The top measured defect (15 overclaimed entities; 5 of the 9 flipped-case
failures): the agent scans its own phrase variants, then reports variant
hits as matches of the asked concept — sharpest at v2c039, recall 0.94 with
5 overclaims. The fix direction discussed with David (he has NOT yet seen or
approved wording): a rule-7-adjacent prompt rule — attribute scan matches at
the phrase level the scan actually fired, never silently upgraded to the
asked concept — possibly paired with `scan_text` echoing fired terms more
prominently per entity. This is durable tool semantics (the `terms` field
already exists per excerpt), not test-case patching; still, prompt wording
goes to David first, with the durable rationale stated. After approval:
apply, update `SYSTEM_PROMPT_SHA256` in-commit, fresh core run, compare
flipped-tier movement.

Second lever, cheaper: **recall variance** — scan-phrase choices differ run
to run (mean recall 0.73 → 0.59 across two runs on identical questions).
Watch it across the next few runs before proposing anything; it may resolve
under the overclaim rule (both are phrase-choice discipline).

## 3. David's open decisions (his, not yours)

1. **Accept the baseline trio**:
   `uv run python -m experiments.nightly --accept 2026-08-04_v2_smoke_fdba68a_r1,2026-08-04_v2_core_fdba68a_r1,2026-08-04_scripts_v2.0_fdba68a_r1`
   — unlocks the comparator (every run since ever reports `no-baseline`).
2. ~~Nightly cron~~ — **decided 2026-08-05: no.** Unattended nightly runs
   are a production-mode concept; in dev, all QA runs are live and
   supervised. The runner/comparator stays the hand-invoked harness. Do not
   re-propose during dev (DECISIONS 2026-08-05).
3. Unassisted calibration labels (runbook §7) — gates trust in judged means;
   also the moment to re-score cc:opus vs terra fairly.
4. Held-out batch (~10–15 questions, `.qa-artifacts/heldout/`,
   `--suite-path`).
5. Triage's parked case-design questions: v2c033, v2c038 oracle strictness,
   `step_cap` funnel label (funnel.py keys on a "(wrapup)" marker bundles
   never record).

## 4. Standing constraints (unchanged; enforced by AGENTS.md + tests)

TEST env only; advisor writes only the `advisor` schema; never commit
`.env`. Manifests immutable; truth sets machine-built, never hand-edited; no
case/evidence bodies in git or triage reports. No test-case hacking — prompt
changes need David's explicit approval with durable product rationale.
Claude Max = David-seat dev/QA only, never production; unknown models raise
in `cost.py`. Commit + push at every milestone; never commit during a live
golden run. `CLAUDE_CONFIG_DIR=/home/david/.claude-max` for all headless
`claude -p` (judge included). Prompt-prefix hashes
(`SYSTEM_PROMPT_SHA256`, `TOOL_SCHEMA_SHA256`) update in the same commit as
any prompt/tool edit.

## 5. Gotchas learned today (cheap to inherit)

- The scan engine's admin path must stay byte-identical to the truth
  builder's or every committed truth digest moves — `uv run python -m
  experiments.truth --check` before and after touching `x1_advisor/scan.py`;
  a semantic change means bumping `BUILDER_VERSION` and rebuilding oracles.
- The suite's flipped cases dropped `must_not_claim_exhaustive` — do not
  re-add it to capability cases; a census answer legitimately claims
  completeness for its scanned scope (design §4).
- `script_runner` died mid-run once (transient, un-reproduced); its partial
  manifest was deleted before commit. If a runner dies, delete the partial
  manifest and re-run — a half-manifest committed as if complete poisons
  comparisons silently.
- Judge cost is real: ~$8.5 seat-equivalent per judged core run. Batch your
  reruns deliberately.
