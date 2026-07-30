# Teacher-QA loop — observability design for agent-driven improvement

> Date: 2026-07-30. Status: **proposal, for review** — written for second-agent
> review, same contract as [`DESIGN-REVIEW-2026-07-30.md`](DESIGN-REVIEW-2026-07-30.md):
> every claim about current state carries file:line evidence; verify, don't trust.
> Design questions for the reviewer are collected in §8.
>
> Context: [`ARCHITECTURE-PLAN-REVIEW-2026-07-30.md`](ARCHITECTURE-PLAN-REVIEW-2026-07-30.md)
> (the independent review; its Gates 1–6 are taken as the working sequence) and
> [`DESIGN-REVIEW-2026-07-30.md`](DESIGN-REVIEW-2026-07-30.md) (fresh-eyes review;
> its step-0 fixes are assumed to land first).

## 1. The product requirement (David, 2026-07-30)

> Design the advisor so that a **"teacher" AI agent can do QA on it, inspect the
> traces, hone in quickly on the source of any problems, and fix them.** The
> observability/telemetry apparatus must be optimized for that quick QA loop —
> so other AI agents can rapidly improve the workflow through repeated rounds of
> testing and fixing.

This makes *agent-legible observability* a first-class design axis, not a
nicety. The loop being optimized:

```text
run QA suite → observe traces → localize failure to a stage → fix the stage
     ↑                                                            |
     └────────── verify: replay case + compare full runs ─────────┘
```

The teacher agent is a Claude-Code-class agent operating in this repo via CLI.
Its costs are context and wall-clock: every trace it must read end-to-end and
every failure it must re-derive from raw data is loop latency. The design goal
is that **localization is a lookup, not an investigation.**

## 2. Honest scoring of the current design

| Capability | Score | Evidence |
|---|---|---|
| **Run** — agent can execute QA | strong | Everything CLI-runnable: `ask.py`, `chat.py`, `experiments.run` (retrieval + agent modes), `acl_probes.py`; golden set machine-gradable; JSONL manifests |
| **Observe** — what happened is recorded | half-built | Per-step usage tables (`advisor.py:127-140`), default-on cost ledger, Langfuse spans, `research_record` JSONB — but see O1: tool **results** are not persisted |
| **Localize** — failure → stage | weak | No retrieval explain (ranks computed then discarded, `retrieval.py:44-47`); no failure-stage classification; localization = open-ended trace reading |
| **Verify fix** — replay + regression diff | mostly missing | Full request not persisted (no replay possible); no run comparator; turns not stamped with code/prompt fingerprints; manifests overwrite by name |

Net: ~6/10 for "an agent can run QA", ~3/10 for "an agent can localize without
re-deriving everything." The foundations (instrumentation-first culture, compact
tool results, manifests) are genuinely better than typical — the gaps are
specific and cheap.

### The four load-bearing gaps

**O1 — Tool results are not persisted.** `research_record` stores tool calls
(name + arguments) but not what the tools returned (`advisor.py:193-195` keeps
only `citations, citation_stats, steps, tool_calls, latency_ms`). The single
most important debugging question — *what did the model actually see?* — is
unanswerable after the fact, so "retrieval returned garbage" and "model ignored
good evidence" (opposite fixes) are indistinguishable. Tool results are compact
by construction (§9 discipline: 600-char snippets, k=8, bounded web findings),
so full persistence costs ~20–50 KB/turn.

**O2 — Retrieval is a black box in the trace.** `Hit` carries
`dense_rank`/`lex_rank`/`rrf_score` internally and they are thrown away. When a
search misses, five different causes need five different fixes — ACL predicate
excluded it, dense leg ranked it #40, FTS never matched, text-hash dedup ate
it, per-doc cap dropped it — and none is distinguishable post-hoc.

**O3 — No automatic failure-stage classification.** Most localization is set
arithmetic that needs no LLM: expected docs vs retrieved vs shown-to-model vs
cited. Today nobody computes it; every failure is an open-ended read.

**O4 — No replay, no comparator, no fingerprints.** A stored failing turn
cannot be re-executed (request ACL/history not persisted); two runs cannot be
mechanically diffed; turns don't record `git_sha`/prompt-SHA/tool-schema-SHA,
so behavior changes can't be correlated with code changes. The manifest
overwrite defect (found 2026-07-30: `2026-07-08_active_v1.jsonl` committed with
different contents at `94dcbb4` and `0c06b13`) actively destroys the
before/after evidence a teacher needs.

## 3. Design principles

**P1 — Local artifacts are the QA source of truth; Langfuse is a human mirror.**
Proven by the July quota incident: Langfuse ingestion was silently suspended
for ~6 days (org-level free-tier cap blown by an unrelated project) and those
traces are unrecoverable — `telemetry.py` is correctly fire-and-forget, which
means cloud traces can vanish without breaking anything *including the QA
loop's memory*. Therefore: Postgres `research_record` + JSONL manifests must be
complete enough that the teacher never *needs* Langfuse. Langfuse keeps
dashboards, score timelines, datasets — a mirror, never a dependency.

**P2 — Traces are layered for an agent reader.** An agent reading traces spends
context — the same economics as §9, pointed at ourselves. Every artifact has a
summary layer (one JSON object: verdict, funnel label, steps, cost) above
drill-down layers (per-step records → full tool results → retrieval explain),
each opened only on demand. Same `get_source` escalation pattern.

**P3 — No silent truncation in QA artifacts.** Bundles store everything the
model saw and said. Any future size cap is opt-in config, default unlimited
(project rule). Bounded-by-construction tool results make this affordable.

**P4 — The loop must be structurally hostile to test-case hacking.** A teacher
agent iterating on failing traces is *incentivized* toward narrow fixes (the
global no-prompt-hacking rule exists precisely because of this pressure).
Mechanical counter-pressures in §6.

**P5 — Bundles are an access surface.** A turn bundle contains evidence text
the requesting user was entitled to see. Reading a bundle later is a *second*
access path and must be treated like the persisted-citation endpoint from the
architecture review: admin-only in v1, ACL re-evaluated at read time if ever
exposed wider.

## 4. The artifacts

### 4.1 Turn bundle (`research_record` v2)

One complete, replayable record per turn, stored in `advisor.turns.research_record`
(JSONB; schema-versioned). Sketch:

```jsonc
{
  "schema_version": 2,
  "request":     {"question": "...", "history": [...], "acl": {...} | "admin",
                  "thread_id": 42},
  "fingerprint": {"git_sha": "71b13c0", "prompt_sha256": "dc236bb7…",
                  "tool_schema_sha256": "…", "config_id": "te3s_1536_ck1",
                  "agent_model": "gpt-5.1"},
  "summary":     {"verdict": "answered|wrapped_up|error", "steps": 7,
                  "cost_usd": 0.011, "latency_ms": 10400,
                  "citations": {"emitted": 5, "resolved": 5, "dropped": []}},
  "steps":       [ /* existing per-step usage rows, unchanged */ ],
  "messages":    [ /* FULL message list incl. every tool result verbatim —
                      the exact JSON strings the model saw */ ],
  "retrieval_explain": [ /* one entry per search_corpus call, §4.2 */ ],
  "raw_answer":  "…pre-validation model output (step-0 fix F5)…",
  "validation":  { /* validate_citations output incl. resolved citations */ },
  "scores":      {"citation_resolvability": 1.0, "faithfulness": null}
}
```

Notes: `summary` is the P2 top layer — a teacher lists summaries cheaply
(SQL over JSONB) and opens one bundle only when needed. `fingerprint` makes
"what changed between these two behaviors" a field comparison. The tool-schema
SHA is the same canonical serialization proposed for the extended CI cache pin
(DESIGN-REVIEW F4) — one implementation, two uses.

### 4.2 Retrieval explain

Emitted by `retrieve()` (always-on; a few KB per call — cheap enough that a
debug flag would only create the "wasn't enabled when it mattered" failure):

```jsonc
{"call": 1, "query": "…", "filters": {...}, "acl": "admin|class-dict",
 "config_id": "te3s_1536_ck1",
 "legs": {"dense":   [{"chunk_id": 9911, "document_id": 210, "rank": 1}, …],
          "lexical": [{"chunk_id": 8712, "document_id": 198, "rank": 1}, …]},
 "fused":   [{"chunk_id": 9911, "rrf": 0.0323, "dense_rank": 1, "lex_rank": 4,
              "granularity": "block|record_summary"}, …],
 "dropped": {"dedup_text_hash": [8811], "per_doc_cap": [8812, 8813]},
 "returned": [9911, 8712, …]}
```

This turns every retrieval miss into a lookup: expected chunk absent from both
legs → embedding/FTS problem; present in `fused` but in `dropped` → cap/dedup
problem; absent only under the user ACL → `acl_block`. It also directly
supports the architecture review's P0 evidence-boundary work (granularity is
visible at every stage) and future E1/E2 debugging.

Implementation note: `retrieve()` grows an optional `explain` output (returned
alongside hits and attached to the bundle by `search_corpus`); the harness
stores it in manifests too, replacing today's bare `retrieved` list.

### 4.3 Funnel classification (the localization layer)

For each golden question, the harness computes four sets from the bundle:

- **E** — expected evidence (golden matchers; golden v2 adds `expected_route`)
- **R** — everything any retrieval leg surfaced (from `retrieval_explain`)
- **S** — evidence actually shown to the model (tool results in `messages`)
- **C** — evidence cited in the validated answer

and assigns the **first failing stage** in funnel order:

| Label | Mechanical rule (no LLM) |
|---|---|
| `routing_error` | expected tool route not taken (aggregate → no `structured_query`; web-required → no `web_research`), or filters supplied that match zero known values |
| `acl_block` | E-items retrieved under admin scope but excluded under the test persona (this also fixes the vacuous-pass defect in `acl_probes.py` — every probe gains an admin-scope positive control for free) |
| `retrieval_miss` | some E-item ∉ R |
| `ranking_drop` | E-item ∈ R but ∉ S (lost to fusion rank, dedup, or per-doc cap — `dropped` says which) |
| `evidence_unused` | E-item ∈ S but ∉ C |
| `validation_drop` | model cited it but the validator dropped the ref |
| `step_cap` | wrap-up synthesis path invoked (`steps[].tool_calls == ["(wrapup)"]`) |
| `synthesis_error` | E ⊆ C but the faithfulness judge fails the claim — **the only label requiring an LLM** (Gate-1 judge) |

Multi-part questions can carry one label per expected item; the question-level
label is the earliest stage. The output layer is one line per question:
`{question_id, pass|fail, label, evidence}` — a teacher reads 36 lines and
knows where to dig.

### 4.4 Replay

```
uv run python -m x1_advisor.agent.replay <turn_id> [--times N] [--json]
```

Loads the bundle's `request`, re-runs `run_turn` against current code, and
prints a structured diff: fingerprint delta (what changed since the original —
git SHA, prompt SHA, schema SHA, config), funnel-label transition, citation
set diff, steps/cost delta. Text-level diffs are explicitly *not* the contract
— LLM sampling makes them noise; the funnel/citation level is where
determinism lives. `--times N` reruns for flakiness classification (report
label distribution) — worth having from day one since single-run "fixed!" is
the classic false positive of trace-driven fixing.

### 4.5 Run comparator + manifest immutability

```
uv run python -m experiments.compare runs/<A>.jsonl runs/<B>.jsonl
```

Per-question status transitions (`fixed` / `broken` / `still_failing` /
`still_passing`), label shifts, recall/MRR/cost/latency/step deltas; nonzero
exit on any `broken` → usable as a CI gate. Prerequisite fix: manifest
filenames gain git-short-SHA + sequence (`2026-07-30_te3s_1536_ck1_71b13c0_r1.jsonl`)
and the harness **refuses to overwrite** — the current `date_config` naming
already destroyed the 0.778 baseline in place (recoverable only from git).

## 5. Langfuse's role (mirror, per P1)

Keep: per-turn traces with usage/cost observations, `citation_resolvability` /
`cost_usd` / future `faithfulness` scores, and (Gate-4) golden-set-as-dataset
experiments for human-facing timelines. Add bundle linkage: trace metadata
carries `turn_id` so a human clicking a Langfuse trace can find the full local
bundle, and vice versa (`trace_id` is already in the turn result). The teacher
loop itself reads only local artifacts. (Operational note 2026-07-30: keys now
point at the personal `dsv-org`/`alpha-claw` project after the org-level quota
suspension; a dedicated project for advisor traces is recommended but the loop
must keep working through any future suspension — which P1 guarantees.)

## 6. Anti-test-case-hacking mechanics (P4)

The teacher's runbook (§7) is not advisory — these are enforced by the loop's
tooling:

1. **A fix counts only if the full suite improves.** `compare` gates on zero
   `broken` transitions; the failing case alone passing is not success.
2. **Every fixed failure is promoted** into the golden set (with matcher +
   expected route) in the same change — the set grows monotonically from real
   failures, per the plan's own risk table.
3. **Funnel labels direct fixes at stages, not questions.** A `routing_error`
   fix touches the tool contract; adding question-specific prompt wording to
   dodge a label is the exact anti-pattern the global rule names.
4. **Held-out subset:** a slice of golden v2 (rotating) is excluded from the
   teacher's iteration loop and checked only at round end — judge and prompt
   overfitting shows up as a train/held-out gap.
5. **Judge calibration is fixed during a round** (Gate-1 calibration set;
   langfuse skill's judge-calibration reference) — the teacher may not tune
   the judge to make a fix pass.
6. **Prompt/guidance changes still require explicit human approval** (global
   rule) — the loop automates diagnosis and verification, not that decision.

## 7. Teacher runbook (lands in AGENTS.md)

The onboarding contract for any QA agent, one read:

1. `uv run python -m experiments.run --agent --golden v2` → manifest + funnel
   summary (one line per question).
2. Read funnel lines; pick the dominant failing *stage*, not the loudest
   question.
3. Open one exemplar bundle, layer by layer (summary → steps → messages →
   retrieval_explain). Stop descending once the cause is identified.
4. Hypothesize a stage-level fix; check DECISIONS.md for prior art first.
5. Implement; `replay <turn_id> --times 3` on the exemplar(s).
6. Full-suite rerun + `compare` vs the pre-fix manifest — zero `broken`
   transitions required.
7. Promote fixed failures into golden; write the DECISIONS.md entry (evidence:
   both manifest paths); commit.

## 8. Questions for the reviewing agent

1. **Funnel taxonomy:** is the label set complete and the first-failing-stage
   ordering right? Where does it misclassify — e.g. multi-intent questions,
   answers correct-but-uncited, web-evidence questions where E is fuzzy?
2. **`ranking_drop` vs `retrieval_miss`:** worth splitting further (leg-level
   labels: `dense_miss`/`lexical_miss`)? Cheap given the explain record, but
   label proliferation has a reading cost.
3. **Bundle storage:** Postgres JSONB only, or JSONB + JSONL sidecar files per
   run? (JSONB is queryable and transactional; files are greppable and survive
   DB resets. Proposal: JSONB canonical, harness exports JSONL per run.)
4. **Always-on retrieval explain** — agree, or is there a corpus-scale point
   where it must become sampled?
5. **Replay flakiness:** is `--times 3` label-distribution reporting the right
   v1, or is that over-engineering before evidence of flaky labels exists?
6. **P5 bundle ACL:** admin-only bundles in v1 — sufficient, or does the Gate-2
   authorization work need to cover bundle reads from day one?
7. **Sequencing:** proposal is step-0 fixes → this package *alongside* Gate 1
   (the judge is shared between them; O1–O4 don't depend on it). Agree, or
   does anything here belong behind Gate 2?
8. **Effort sanity-check:** bundles ~1 day; explain ~½ day; classifier ~½ day;
   replay ~½ day; compare + manifest fix ~½ day; runbook ~¼ day. ≈3–4 days
   total. What's underestimated?

## 9. What this buys

Every later gate gets cheaper: Gate-1's agent-suite rerun lands as funnel
lines instead of raw manifests; Gate-2's ACL work inherits per-probe positive
controls; Gate-4's golden v2 grows automatically from promoted failures;
Gate-5's bake-offs get paired, immutable, mechanically-diffable manifests. The
loop this industrializes already happened once by hand — the `entity_type`
enum bug (DECISIONS 2026-07-08, catch #5) was found by instrumented sample
runs, localized by reading per-step tables, fixed at the tool contract, and
verified by rerun. That cycle took an evening; the target is minutes.
