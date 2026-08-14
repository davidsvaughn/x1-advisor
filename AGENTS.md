# x1-advisor — agent instructions

Canonical entry point ([`CLAUDE.md`](CLAUDE.md) points here). Read this file,
then `docs/PLAN.md` §R, then route by task — do not read the whole docs
folder.

## What this project is

An interactive, conversational **research agent** ("research buddy") for the
X1 startup/investor platform: multi-hop research over the X1 Postgres DB
(entities rendered as profile documents), a private document store (pitch
decks / CVs / reports → markdown), and the web — returning source-grounded
answers whose citations are validated in code. A chat session, not a report
job. It is **not** an app-controlling copilot (prior aborted direction — do
not revive) and **not** SQL-first (hybrid retrieval is the spine; SQL is a
narrow precision tool).

Stack: Python 3.13 + Haystack; pgvector in the shared Cloud SQL Postgres
under a dedicated `advisor` schema; agent model `gpt-5.6-terra` on the OpenAI
Responses API (provisional — model choices are bake-offs, see DECISIONS).
QA claim judge: headless Claude Opus 5 (`ADVISOR_JUDGE_BACKEND=cc`, dev/QA
only — RUNBOOK §8.0, DECISIONS 2026-08-04).

## Read order and task routing

Always: **this file → [`docs/PLAN.md`](docs/PLAN.md) §R** (readiness matrix +
gate sequence = current truth). Then:

| Working on | Read |
|---|---|
| **Picking up the current work** (next: overclaim-discipline proposal) | [`docs/HANDOFF.md`](docs/HANDOFF.md) |
| Gate 4 / golden v2 | [`docs/GOLDEN-V2-DESIGN-2026-07-31.md`](docs/GOLDEN-V2-DESIGN-2026-07-31.md) + [`docs/QUESTION-BANK.md`](docs/QUESTION-BANK.md) |
| Gate 3B / page context | [`docs/CONTEXT-SNAPSHOT-DESIGN-2026-07-30.md`](docs/CONTEXT-SNAPSHOT-DESIGN-2026-07-30.md) |
| Track H / headless agents | [`docs/CC-AGENTS-DESIGN-2026-07-31.md`](docs/CC-AGENTS-DESIGN-2026-07-31.md) |
| Running / interpreting QA | [`docs/QA-RUNBOOK.md`](docs/QA-RUNBOOK.md) |
| Why a decision was made | [`docs/DECISIONS.md`](docs/DECISIONS.md), then the dated review docs |
| Base design rationale | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) **as amended by** [`docs/ARCHITECTURE-REVIEW.md`](docs/ARCHITECTURE-REVIEW.md) |

## Document status taxonomy

Status comes from each document's **header banner**, never from its filename —
date-suffixed docs may keep changing while active (Golden-v2 and CC-Agents
did, same-day, as review criteria were folded in).

| Document | Status |
|---|---|
| `docs/PLAN.md` | **living** — §R is current truth; §2 phase checklists are historical record |
| `docs/DECISIONS.md` | **living** — dated log, newest first |
| `docs/QA-RUNBOOK.md` | **living** — QA operating manual |
| `docs/QUESTION-BANK.md` | **living** — master question corpus; seed for golden v2 |
| `docs/HANDOFF.md` | **point-in-time handoff** (2026-08-04 evening) — scan_text live + flipped; next: overclaim-discipline proposal |
| `docs/GOLDEN-V2-DESIGN-2026-07-31.md` | **adopted design** (Gate 4 spec; build authorized by David 2026-07-31 for steps 1–4 + H1) |
| `docs/CC-AGENTS-DESIGN-2026-07-31.md` | **adopted design** (Track H) |
| `docs/CONTEXT-SNAPSHOT-DESIGN-2026-07-30.md` | **adopted design** (Gate 3B; not built) |
| `docs/QA-LOOP-DESIGN-2026-07-30.md` | **adopted design — largely implemented** (Gate 1); kept as rationale |
| `docs/ARCHITECTURE.md` | **historical design draft** (2026-06-12) — several `[DECIDED]` items since superseded; see its banner |
| `docs/ARCHITECTURE-REVIEW.md` | **historical review** (2026-07-07) — PLAN follows it where it disagrees with ARCHITECTURE |
| `docs/QA-DESIGN-AND-DEEPEVAL-INDEPENDENT-REVIEW-2026-08-06.md` | **point-in-time review** (2026-08-06, at `87ac00e`) — QA design, s6 baseline, DeepEval; findings F1–F10 |
| `docs/QA-REVIEW-CONSOLIDATED-2026-08-07.md` | **point-in-time review** (2026-08-07) — verification pass over both prior QA reviews; consolidated priorities |
| `docs/ANALYZE-SCOPE-DESIGN-2026-08-13.md` | **proposed design** — semantic map/reduce tool; build gated on live-testing demand (SCAN-A class) |
| `docs/triage/` | **living, GITIGNORED (local-only)** — one triage doc per live advisor thread (flags + diagnoses + fix status). NO content restrictions: quotes answers/corpus freely (David 2026-08-12) — which is exactly why it never enters git. See its README |
| `docs/refs/` | reference-mining notes (pipeshub, chroma) cited by ARCHITECTURE |
| `docs/archive/` | **absorbed point-in-time docs** — the 2026-07-30 reviews, the stale HANDOFF snapshot, kickoff transcripts. Evidence trail only; see its README |

**Archive convention:** when a dated doc's conclusions are fully absorbed and
it is no longer consulted for current decisions, it moves to `docs/archive/`
(git mv, links fixed) and this table is updated. Living docs and
still-consulted rationale stay at the top level.

## Standing rules (non-negotiable)

- **Environment:** the sanctioned environment is TEST only (`x1-db-test` /
  `x1-app-www-test`). App tables are read-only; the advisor writes **only** to
  the `advisor` schema. Never commit `.env`.
- **Cost:** every LLM/embedding call routes through `x1_advisor/cost.py`; an
  unknown model must raise — never a silent $0. Consumer-subscription billing
  (Claude Max) backs David-seat dev/QA work only, never a multi-user
  production path (DECISIONS 2026-07-31).
- **Access model:** open cross-user research **is the product**. Guardrails
  gate *classes* of sensitive content (private docs, drafts, premium text);
  never build identity walls ("you can only see your own data").
- **No test-case hacking:** never fix a failing trace with query-specific
  prompt wording, schema hints, or one-off resolvers that mirror the trace;
  fix the underlying concern. Prompt/guidance changes need David's explicit
  approval with a durable product rationale.
- **No silent truncation:** never drop, summarize, or cap user data without
  explicit approval; length caps are opt-in and default to unlimited.
- **Models are experiments, not commitments:** every provider/model seam is
  swappable; changes land via bake-off + a dated DECISIONS entry.
- **Workflow:** work on `main`; commit and push at every milestone, not in
  end-of-session batches.
