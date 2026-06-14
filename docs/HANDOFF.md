# X1 Advisor — Handoff

> For an agent picking up this project. Last updated 2026-06-14.
> Read this, then [`ARCHITECTURE.md`](ARCHITECTURE.md) (the design source of truth).

## What this project is

An **interactive, conversational research agent** ("research buddy") for the X1 startup/investor
CRM platform. The user asks open-ended questions; the agent does **multi-hop research** over three
evidence sources — the X1 Postgres DB, a private document store (pitch decks, CVs, reports →
markdown), and the web — and returns **source-grounded answers with citations**, then waits for the
next turn. It is a chat session, not a one-shot report job, and **not** an app-controlling copilot
(that was a prior, aborted direction — do not revive it).

This repo (`/home/david/code/x1/dev/x1-advisor`) is **greenfield**. So far it's design docs + one
Python module. No service is running yet.

## Current state (what exists)

| Path | What it is |
|---|---|
| `docs/ARCHITECTURE.md` | **The design source of truth.** Decisions marked [DECIDED] vs [OPEN]. Read fully. |
| `docs/refs/pipeshub-ai.md` | Deep-dive of a close sibling system. Highest-value external reference (citations, chunking, deep-research loop). |
| `docs/refs/chroma.md` | Vector-store internals + pgvector-vs-Chroma evaluation (conclusion: stay on pgvector). |
| `docs/chats/chat1.md`, `chat2.md` | Original framing conversations (Haystack vs LangGraph, the "research buddy" goal). |
| `x1_advisor/cost.py` | LLM telemetry + cache-aware cost tracking (implemented, tested). |
| `.env` | DB + GCP credentials (gitignored — never commit). |

Nothing else is built yet: no Haystack service, no `advisor` schema, no ingestion, no agent.

## Key decisions already locked (see ARCHITECTURE.md for rationale)

- **Framework:** Haystack (agent owns the tool-call loop). Python service. LLM = Claude via
  `AnthropicChatGenerator`. LangGraph deferred.
- **The spine is hybrid semantic retrieval over a unified index — NOT SQL-first.** Structured DB
  entities are rendered into denormalized "profile documents" and indexed alongside doc chunks;
  structured fields become **metadata filters**, never LLM-authored SQL. The user feels strongly
  about this; don't drift back toward an SQL-first agent.
- **Storage:** markdown is canonical in Postgres + **pgvector**, dedicated `advisor` schema, in the
  existing shared Cloud SQL instance; binaries stay in GCS. The pipeline reads only the DB.
- **Chunking:** structure-aware + dual-granularity + a record-summary block per doc; multimodal
  (VLM-caption slides) for pitch decks. (Borrowed from pipeshub.)
- **Citations:** block-index primitive → tiny opaque refs → "omit rather than guess" →
  server-side validator that repairs/drops/renumbers. Grounding enforced in code.
- **Agent loop:** two tiers — fast single-turn ReAct (build first) vs. opt-in deep
  plan→critic→execute→evaluate.
- **Telemetry:** every LLM call routes full token usage through `x1_advisor/cost.py`.

## Open decisions (ARCHITECTURE.md §11)

- **[OPEN] Embedding model + dimension** — *the recommended next task.* Pins both the pgvector
  column dimension and the embed pricing row in `cost.py`. Research cost/quality/dim tradeoffs for
  our doc types (pitch decks, CVs, reports) and recommend one.
- [OPEN] Reranker choice (start blend ~`0.3·dense + 0.7·rerank`).
- [OPEN] Web search provider (Serper/Brave/Tavily).
- [OPEN] Entity-profile rendering: which joins/fields per entity card; refresh trigger.
- [OPEN] `structured_query` surface (parameterized, read-only).
- [OPEN] Tier-1 vs Tier-2 router.

## Suggested next step

Either (a) settle the **embedding model** (unblocks pgvector dimension + pricing), or (b) stand up
the **smallest end-to-end Tier-1 slice**: create the `advisor` schema + pgvector, index a few real
entity profiles + extracted decks, wire `search_corpus` + `web_search` into a Haystack `Agent`
(fast path only), and ask real questions to find where retrieval strains. (a) logically precedes (b).

## How to work here (norms the user has set)

- **Stay on `main`. No feature branches** (user's explicit current preference). Commit + push to
  main when asked.
- **Never commit `.env`** (gitignored; verify before staging). End commit messages with the
  `Co-Authored-By: Claude Opus 4.8` trailer.
- **Don't get derailed by prior attempts.** Old code in `/home/david/code/x1/x1-link/services/x1-advisor`
  (Mastra/TypeScript) is the aborted copilot — useful for lessons (`docs/advisor/HERMES-LESSONS.md`),
  but we are NOT porting it. Look for ideas, don't copy patterns.
- **Use Context7 MCP** to verify library/framework APIs (Haystack, Anthropic, pgvector) before
  writing code — don't rely on training data.
- **No prompt/test-case hacking, no silent truncation** — see the project + global CLAUDE.md rules.

## Database access

The schema and data live in Cloud SQL Postgres (shared with the live x1-app/x1-backend). Connection
recipe: `/home/david/code/x1/x1-link/.claude/skills/database-connection/SKILL.md`. Quick facts:
- Two envs: **test** (`x1-db-test`, auto-suspends to save cost — wake via the proxy script) and
  **prod** (`x1-db`, live users — read-only only).
- Start the proxy: `bash scripts/cloud-sql-proxy.sh` from `x1-link` (handles instance wake-up +
  socket). Connect via the Unix socket path, not localhost.
- All advisor DB access must be read-only `SELECT`/`WITH` (the prior code had an
  `assertReadOnlySelect()` guard — replicate that pattern).
- Real schema highlights (test counts; prod is larger and growing): `startup_companies` (rich text
  columns: `full_description`, `one_sentence_pitch`, fundraising_*), `startup_company_team_members`
  (`personal_summary`, `achievements`), `cvs` + `cv_experiences`, `investors` (`investment_thesis`),
  `startup_company_documents` (GCS `file_path` to source PDFs; mostly pitch decks),
  `startup_company_evaluations` (score + small `raw_json`; full report is a PDF in GCS),
  `doc_extraction_cache` (markdown extraction cache, content/config hashed).

## Linked reference codebases (in the workspace, for ideas — not dependencies)

- `/home/david/code/x1/link/pipeshub-ai` — sibling AI workplace-search platform. See
  `docs/refs/pipeshub-ai.md`.
- `/home/david/code/x1/dev/chroma` — the Chroma vector DB. See `docs/refs/chroma.md`.
- `/home/david/code/davidsvaughn/signal-hunter/signal_hunter/cost.py` — the cost-tracking module
  `x1_advisor/cost.py` was adapted from; canonical-ish pricing source.

## Existing x1 backend (real, for integration context)

`/home/david/code/x1/dev/x1-backend/mastra/` has working Mastra/TypeScript pipelines: `eval`
(pitch-deck evaluation → the reports we'll index), `import` (CV/startup doc import), `extract`
(PDF → markdown extraction — the feeder that should write markdown into our `advisor` doc store).
