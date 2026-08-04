# Handoff — build Golden v2.0 + Track H1

> Date: 2026-07-31. Status: **point-in-time handoff** for the coding agent
> starting the approved build. Supersedes
> [`archive/HANDOFF.md`](archive/HANDOFF.md) (2026-07-07 snapshot).
> Authorized scope: **Golden v2.0 (Gate 4 wave 1, build-order steps 1–4) and
> Track H1** — David commissioned this handoff 2026-07-31, which lifts the
> build pause *for this scope only*. **H2/H3 are designed but NOT authorized
> to build.**

## 0. Read first (in this order, nothing else up front)

1. [`../AGENTS.md`](../AGENTS.md) — standing rules; they are non-negotiable.
2. [`PLAN.md`](PLAN.md) §R — readiness matrix + gate sequence (current truth).
3. [`GOLDEN-V2-DESIGN-2026-07-31.md`](GOLDEN-V2-DESIGN-2026-07-31.md) — your
   primary spec (Gate 4). §9 is your build order; §4 the case schema; §5 the
   grading design.
4. [`CC-AGENTS-DESIGN-2026-07-31.md`](CC-AGENTS-DESIGN-2026-07-31.md) §4 + §9
   — the H1 spec and the five acceptance criteria.
5. [`QUESTION-BANK.md`](QUESTION-BANK.md) — the case source. Curate, don't
   copy; wording is preserved verbatim — *the phrasing is the test*.
6. [`QA-RUNBOOK.md`](QA-RUNBOOK.md) — how the existing QA machinery operates
   (funnel, comparator, judge rules §8, calibration §7).

## 1. Where the project stands

- **Gate 1 closed 2026-07-31** after four correctness passes (1A–1E). Live
  machinery you build on, not around: turn bundles + fingerprints, retrieval
  explain, snapshot-judging (judge grades per-ref snapshots of what the model
  saw), route-aware funnel, scoring-contract-aware comparator (four gates:
  pass-flips, label worsening, mean-drop `--score-drop 0.10`, completeness),
  three replay modes, blind calibration flow + labeling UI.
- **Why v2 exists:** golden v1 is agent-authored (2026-07-08); zero questions
  derive from QUESTION-BANK or the 28 real threads. Working numbers
  (faithfulness ≈ 0.5, coverage ≈ 0.83–0.88, ±0.05–0.07 at n=20,
  synthetic-only calibrated) are therefore validity-limited — Gate 4 exists
  to fix that, not to add precision. **v1 stays frozen** as the
  retrieval-regression suite; never edit it.
- **Models:** agent `gpt-5.6-terra`, OpenAI **Responses API**,
  `reasoning effort=medium` (`ADVISOR_AGENT_MODEL`/`ADVISOR_AGENT_REASONING`
  env overrides). Judge `gpt-5.6-terra` (QA-RUNBOOK §8 rules; judge model is
  part of the scoring contract). From gpt-5.6 on, Chat Completions rejects
  function tools + reasoning — everything goes through Responses.
- **Corpus (test env):** 412 docs / 6,728 chunks; 24 prod fixture bundles;
  75/79 test bundles are an experimental shape and are skipped loudly.

## 2. Deliverables, in order

Per GOLDEN-V2-DESIGN §9 and CC-AGENTS §4. Commit + push at each milestone.

1. **Case schema + compiler** (~1 day) — YAML → validated case objects: six
   orthogonal readiness fields, class taxonomy, deterministic+judged grade
   blocks, `fallback_contract`, seeded entity-binding resolver, compiled
   **suite digest**. Unit tests alongside (`tests/test_agent_units.py` has 20
   passing tests — match its style; run `uv run pytest -q`).
2. **Truth-set builder + global checkers** (~1 day) — deterministic offline
   FTS/phrase scan over `advisor.doc_chunks` → `truth/*.json` with builder
   version + corpus content-hash + scope definition. Checkers (numeric
   grounding, entity grounding, coverage-statement) recorded as
   **diagnostics only** — nothing gates until a false-positive audit
   promotes it (acceptance criterion 4).
3. **Author v2.0 core** (~52 cases + 4 scripts) per the §3 composition table:
   bank-sourced verbatim, LFT-weighted, SEL cases bound to explicit fixtures,
   truth-robustness cases with offline-verified premises/absences. Every case
   carries provenance (bank row / LFT thread).
4. **Script runner** (~1 day) — executes a script as one conversation;
   per-turn + cross-turn assertions; the script is the gate unit; bundles per
   turn feed the existing judge/funnel unchanged.
5. **H1 deterministic runner + cron** — plain Python, exit-coded: nightly
   golden run → funnel → comparator vs last accepted baseline; truth-set
   rebuild on corpus-hash change; calibration prep; held-out execution from
   `.qa-artifacts/heldout/` emitting **aggregates only**. Then the CC triage
   agent: settings profile (helm sandbox recipe, CC-AGENTS §4) + prompt; it
   reads artifacts and writes `.qa-artifacts/reports/` — it executes nothing.
6. **v2.0 baseline run** — smoke + core, full funnel + judge; report the
   result but draw no strategic conclusions yourself (that comparison against
   the v1-era claims is David's + the teacher session's call).

**Acceptance criteria (all five, from the 2026-07-31 review — treat as the
definition of done):** execution/triage split; evidence adapter before any H2
ingest (out of scope here, but don't half-build it); run identity on every
result (contract + resolved bindings + suite digest + truth-set digest,
paired runs pin identical bindings); checkers diagnostics-first; truth
generation deterministic/versioned, never agent-authored.

## 3. Code map

- `x1_advisor/agent/` — `advisor.py` (loop, `agent_generator()`,
  feature_flags), `judge.py`, `bundle.py`, `replay.py`, `tools.py`,
  `evidence.py` (citation layer), `queries.py` (structured registry).
- `x1_advisor/` — `retrieval.py` (hybrid + `_acl_sql`), `cost.py` (EVERY
  model call routes here; unknown model raises), `db.py`, `fingerprint.py`.
- `experiments/` — `run.py` (harness), `compare.py` (comparator),
  `funnel.py`, `judge_calibrate.py`, `judge_bakeoff.py`, `rejudge.py`,
  `label_ui.py`, `golden/v1.yaml`.
- `.qa-artifacts/` — untracked, owner-only (0600): `runs/`, `calibration/`,
  and (yours to add) `heldout/`, `reports/`, `truth/` if you keep truth sets
  untracked — decide tracked-vs-untracked by whether case bodies leak
  (truth sets contain corpus-derived matches: **untracked**, digests in git).

## 4. Environment + operational gotchas (will cost you hours if skipped)

- **DB:** cloud-sql-proxy binary `/home/david/Downloads/BeeKeeper/cloud-sql-proxy`,
  socket dir `~/cloudsql`, instance `vertical-album-400917:us-east1:x1-sql-test`.
  If connections fail with `invalid_grant`/reauth errors: ADC is expired —
  ask David to run `gcloud auth application-default login` (suggest the `!`
  prefix), then **restart the proxy** (a running proxy keeps stale creds).
- **Test env ONLY** (`x1-db-test`); app tables read-only; writes only to the
  `advisor` schema. Never commit `.env`.
- **Billing:** OpenAI key is company-paid (default). DeepSeek key is David's
  personal money — opt-in only. Claude Max subscription backs David-seat
  dev/QA work only. A full judged 20-question suite run costs ~$1–2; ask
  before anything that looks like a sweep (many replicates / bake-offs).
- **Langfuse** mirrors to David's personal org (`dsv-org`/`alpha-claw`) —
  it is a mirror, never a dependency; local bundles + manifests are the QA
  source of truth.
- **Port 8100 is taken** on this machine (alpha-claw frontend); the advisor
  service smoke-tests on another port (8123 previously).
- **Machine load:** run broad repo scans sequentially and `nice`d;
  `~/code/x1` contains huge node_modules trees; check for leaked
  `playwright-mcp-server` processes if load spikes.
- **Commit messages:** backticks in `git commit -m` get shell-substituted —
  use `git commit -F <file>`.

## 5. Hard don'ts

- Don't edit `experiments/golden/v1.yaml` or any committed manifest
  (manifests are immutable; the comparator depends on it).
- Don't compare or gate across differing scoring contracts — the comparator
  refuses (exit 2) by design; never "fix" that.
- Don't hand-author or hand-edit truth sets; don't let any agent do so.
- Don't put case bodies, evidence bodies, or held-out content in git or in
  triage reports — labels/digests/aggregates only.
- Don't fix a failing case with case-specific prompt wording or one-off
  resolvers (no-test-case-hacking rule; prompt changes need David's explicit
  approval).
- Don't build H2/H3, don't touch prod, don't rewrite pushed history.

## 6. Waiting on David (don't block; note and continue)

- Held-out batch (~10–15 questions) once your runner exists — his task.
- H2 cite-through/ACL policy sign-off — not your scope.
- Anthropic company API key (PLAN Phase 0) — not needed for this build.

Hand back: PLAN §R Gate 4 progress notes + a dated DECISIONS entry per
milestone, same conventions as the existing entries.
