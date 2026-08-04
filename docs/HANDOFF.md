# Handoff — golden v2 QA loop live; next: `scan_text` (Path B)

> Date: 2026-08-04. Status: **point-in-time handoff**, written after the
> judge-audit/judge-switch session. Supersedes
> [`archive/HANDOFF-2026-07-31.md`](archive/HANDOFF-2026-07-31.md) (the
> Golden v2.0 + H1 build brief — that build is DONE, reviewed, and repaired).
> Authorized next scope: **`scan_text` (Path B)** — David authorized the
> build 2026-08-04 and asked for a pause after this documentation pass, so
> the build is **not started**. Wait for his go before writing tool code.

## 0. Read first (in this order)

1. [`../AGENTS.md`](../AGENTS.md) — standing rules; non-negotiable.
2. [`PLAN.md`](PLAN.md) §R — readiness matrix + gate sequence (current truth).
3. [`DECISIONS.md`](DECISIONS.md) — the three 2026-08-01/04 entries at the top
   are the complete record of what happened since the last handoff.
4. [`QA-RUNBOOK.md`](QA-RUNBOOK.md) — how to run everything; **§8.0 is the
   current judge**; §1 has the v2 runner commands.
5. [`GOLDEN-V2-DESIGN-2026-07-31.md`](GOLDEN-V2-DESIGN-2026-07-31.md) — §5.1
   is the `scan_text` design seed (the truth-set builder is ~80% of the
   tool); §4 explains the honesty→capability contract flip the tool enables.

## 1. What happened since the 2026-07-31 handoff (short version)

Full detail lives in DECISIONS — this is the orientation map, newest first:

1. **Judge switched to headless Opus 5** (`09c2261`). A second-agent-style
   audit (4 parallel auditors, all 28 faithfulness-failing cases, 74 flagged
   claims vs the exact evidence snapshots) found ~92% false positives with
   structural causes in the OpenAI judge pipeline: titles stripped from the
   payload, per-claim citation tunnel vision, extractor-mutated claims,
   hyper-literal entailment. New backend: one `claude -p` call per answer,
   full titled evidence, labels computed in Python
   (`x1_advisor/agent/judge_cc.py`). `ADVISOR_JUDGE_BACKEND=openai` restores
   the old pipeline (defects documented in place, un-fixed). Paired rejudge
   of identical answers: **core 13/49 → 22/49**
   (`experiments.rejudge_v2`, manifest `2026-08-04_v2_core_cc_801628e_r1`).
2. **Coverage/honesty prompt rules** (`d8b1799`, David-approved "Path A"):
   the agent now discloses search scope, never equates empty search with
   absence (the old prompt literally instructed that error), asserts only
   supported matches, corrects false premises, surfaces ambiguity, declines
   out-of-capability actions. Core 8/49 → 13/49; disclosure-cluster failures
   collapsed (coverage_statement 18→5, overclaims 16→10).
3. **Five harness defects fixed after a second review** (`0fb175f`,
   2026-08-01): v2-aware comparator with identity checks, full
   declared-contract pass composition, real-name truth keys with negation
   handling, single-commit run identity, body-free script manifests. All
   2026-07-31 numbers are void; `tests/test_second_review_fixes.py` pins
   everything.
4. **H1 is live**: deterministic nightly runner + launcher
   (`qa/nightly.sh` — timeout, env-scrubbed triage, 0600 transcripts, exit
   75 preflight skip). The cron line is documented there but **deliberately
   not installed** (David stood down all crons 2026-07-08).

## 2. Current state of the numbers (2026-08-04, judge = cc:opus)

- smoke **7/7** · core **22/49** · scripts **0/4** — same binding seed
  `v2-baseline` throughout; truth sets current (14/14).
- Remaining core failures are believed to be REAL agent work: truth_set 6,
  behavior:state_absence 6, judged:faithfulness 6, coverage_statement 5,
  judged:citation_coverage 5, quotes_verbatim 2, must_cite 2,
  correct_premise 2, surface_ambiguity 1.
- Scripts fail on verbatim-quote drift across turns and one coverage
  overclaim — deterministic units, judge-independent.
- **No baseline has ever been accepted** (`experiments/golden/baseline.json`
  does not exist), so every nightly comparator run reports `no-baseline`.
  Acceptance is David's call, likely the `_cc` manifests once he trusts them.

## 3. The next build: `scan_text` — sketch, not yet designed in anger

Design §5.1 + the Gate 4 headline name it "build first". The seed: the
truth-set builder (`experiments/truth.py`) already implements the bounded
deterministic scan — same scope logic, per-entity
`matched | no_match | not_indexed` statuses, coverage counts. The tool is
that engine exposed to the agent with: ACL applied like every data-bearing
tool, `record_summary` chunks excluded (generated text is not evidence),
matching chunks registered in the EvidenceRegistry so results are citable,
and a schema whose description teaches when scanning beats top-k search.
Known decision points for the build (flag to David, don't decide silently):

- **Refactor discipline**: sharing the scan engine between tool and oracle is
  the design intent (capability grading checks the agent *reports* the scan
  faithfully), but the refactor must leave truth digests byte-identical —
  prove it with `uv run python -m experiments.truth --check` (14/14 current)
  — or bump `BUILDER_VERSION` and rebuild, which moves oracles.
- **`tool_ready` flip**: 24 cases carry `tool_ready: false` + a fallback
  honesty contract. Flipping them to capability grading changes the suite
  digest and pass semantics for half the core tier — land the tool, measure
  adoption first, and put the flip in front of David with numbers.
- **Tool-schema hash**: adding a tool changes `TOOL_SCHEMA_SHA256`
  (`tests/test_agent_units.py`) — update it in the same commit, per its rule.

## 4. Open decisions (David's, not yours)

1. Accept a baseline (`nightly --accept <smoke>,<core>,<scripts>` — probably
   the 2026-08-04 `_cc` core/scripts + the `aee9de2` smoke, or rerun fresh).
2. Install the nightly cron (line documented in `qa/nightly.sh`, ~$3/night
   equivalent + seat quota now that the judge is subscription-billed).
3. Author the held-out batch (~10–15 questions, `.qa-artifacts/heldout/`,
   runner supports `--suite-path`).
4. Unassisted calibration labels (runbook §7) — gates how much the judged
   means may be trusted; also the right moment to re-score cc:opus vs terra.
5. Triage's flagged case-design questions: v2c033 (constraint-setting prompt
   graded as same-turn work), v2c038 (matched-mode people oracles counting
   differently-sourced-but-true attributions as overclaims), `step_cap`
   funnel label never applied (`funnel.py` keys on a `"(wrapup)"` marker
   bundles never record).

## 5. Standing constraints (unchanged, enforced by AGENTS.md + tests)

TEST env only; advisor writes only the `advisor` schema; never commit `.env`.
Manifests are immutable; truth sets are machine-built, never hand-edited; no
case bodies/evidence in git or triage reports. No test-case hacking — prompt
changes need David's explicit approval with a durable product rationale.
Claude Max = David-seat dev/QA only, never production; unknown models raise
in `cost.py`, never a silent $0. Commit + push at every milestone. Never
commit while a golden run is live (run-identity taint); never install a cron
without David's explicit go.
