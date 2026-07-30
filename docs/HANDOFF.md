# X1 Advisor — Handoff

> ⚠️ **STALE SNAPSHOT (2026-07-07).** The state table below predates the entire
> build-out (ingestion, retrieval, agent, service all now exist on test) and the
> 2026-07-30 reviews. **Current truth: [`PLAN.md`](PLAN.md) §R** (readiness
> matrix + gate sequence + active proposal docs). Read order for a new agent:
> `PLAN.md` §R → the three active proposals it lists → `DECISIONS.md` →
> [`ARCHITECTURE.md`](ARCHITECTURE.md) / [`ARCHITECTURE-REVIEW.md`](ARCHITECTURE-REVIEW.md)
> for base design rationale. The rest of this file is kept as historical record.

## What this project is

An **interactive, conversational research agent** ("research buddy") for the X1
startup/investor CRM. Multi-hop research over three evidence sources — the X1 Postgres DB
(entities rendered as profile docs), a private document store (pitch decks/CVs/reports →
markdown), and the web — returning **source-grounded answers with citations**. Chat session,
not a report job; **not** an app-controlling copilot (prior aborted direction — don't revive).

## Current state

| Artifact | Status |
|---|---|
| `docs/ARCHITECTURE.md` | Design (2026-06-12), amended by the review |
| `docs/ARCHITECTURE-REVIEW.md` | Full review vs. ground truth + July-2026 research (committed `8fa6f1c`) |
| `docs/PLAN.md` | **The working plan.** Phased build; model choices are bake-offs, not commitments |
| `docs/DECISIONS.md` | Dated decisions log (PgBouncer call, DeepSeek billing, Phase-0 gate status) |
| `x1_advisor/cost.py` | Implemented + tested (multi-provider telemetry; DeepSeek search billing verified) |
| `pyproject.toml` + `uv.lock` | Python 3.13; haystack-ai 2.30.2, anthropic-haystack 5.13.0, pgvector-haystack 6.3.1 pinned |
| `spikes/` | Phase-0 spike scripts. D passed live; A–C written, **blocked on `ANTHROPIC_API_KEY`** |
| `advisor` schema | Created on test (2026-07-07); pgvector 0.8.1 enabled there. No tables yet |
| Everything else | **Not built.** No service, no ingestion, no agent |

**Provider policy (David, 2026-07-08 — see DECISIONS.md):** don't block on Anthropic keys.
**OpenAI is the dev default for chat, embeddings, and web search** (company-paid API key);
**DeepSeek stays a wired-up option but is opt-in only** (David's personal key today; a
company key may come later); Anthropic/Voyage candidates rejoin when their keys land.

**Next action: finish Phase 4 exit criteria** (20-golden-question end-to-end run with
≥95% resolvable citations; seeded ACL probe suite; Langfuse tracing; multi-turn history
discipline), then Phase 3 bake-offs (E2/E3 runnable now; E1 when VOYAGE key lands) and
Phase 5 serving. Phases 0–2 complete + **Phase-4 first slice WORKING** (all 2026-07-08,
see DECISIONS.md): `uv run python -m x1_advisor.agent.ask "question"` answers with
validated citations and a per-step usage table; corpus turn ≈ $0.02, aggregate ≈ $0.004,
corpus+web ≈ $0.05. Corpus: 412 live docs / 7,281 ACL-stamped+embedded chunks on
x1-db-test; retrieval baseline recall@10 0.778 / MRR 0.727
(`experiments/runs/2026-07-08_active_v1.jsonl`). **Context discipline is the testing
acceptance bar (David)** — keep the per-step usage table green: flat uncached input,
growing cached tokens, compact tool results.
Working defaults: gpt-5.1 (agent), text-embedding-3-small (1536d), OpenAI server-side web
search (`include=["web_search_call.action.sources"]` for citations). Know before touching
eval data: **test-env drift** — 75/79 test bundles are an experimental shape (skipped
loudly at ingest) and test dropped redundant eval score columns; 24 original-shape prod
fixture bundles live at `gs://x1-app-www-test/reports/prod_fixtures/` (DECISIONS.md).
Phase-1 leftover: LLM record-summary blocks. Still pending David:
`advisor_obs`/`advisor_evidence` drop confirm, `ANTHROPIC_API_KEY`/`VOYAGE_API_KEY`
(Anthropic spikes shelved, non-blocking), who runs `CREATE EXTENSION vector` on prod.

## Decisions locked (don't relitigate without new evidence)

- **Retrieval spine, not SQL-first** (user feels strongly); structured fields = metadata
  filters; profiles = denormalized markdown docs. Haystack (used shallow/swappable, Phase-0
  spikes gate it; thin direct-SDK stack is the named exit ramp). Python service. pgvector in
  the shared Cloud SQL instance, `advisor` schema.
- **Page-level citations v1** (extraction has no bboxes — only `# Page N` delimiters);
  block-index + tiny-ref + server-side validator mechanism per design §8.
- **Access model: OPEN cross-user research with class-based guardrails (David, 2026-07-07).**
  Founders/investors researching *other* startups & investors is **the whole point of the
  feature** — never build identity-based walls into retrieval ("only your own data" would
  kill the product). Guardrails gate content *classes* only: draft/unpublished profiles
  (owner-only), `private`-visibility docs (treatment = open sub-decision), premium report
  full text (purchase-gated), and a never-index PII/token list. ACL-class metadata on every
  chunk + mandatory retriever-level filter (never prompt-level) makes all of this a policy
  dial. See PLAN.md §0.2.
- **Eval harness before tuning.** Golden set (Phase 2) gates every bake-off (Phase 3).
- **Deferred:** Tier-2 deep mode (converged single-loop shape when built, not plan→critic),
  auto-router, sentence granularity, block-diff indexing, bboxes, sparse vectors.

## Model choices = EXPERIMENTS (user's explicit instruction)

Embeddings, reranker, web search, and per-role generation models are all bake-offs (PLAN.md
Phase 3, E1–E4) behind pluggable seams (index_configs registry, generator registry,
SearchProvider interface). Leading candidates: voyage-4 family (embeddings/rerank), Anthropic
server-side web_search vs **DeepSeek-v4-flash delegated searcher** (user reports it's very
good and it's extremely cheap) vs Serper baseline. Decisions land as dated entries in
`DECISIONS.md` with manifests.

## Key ground-truth facts (verified 2026-07-07; details in review §2)

- **Scale (prod):** 293 startups, 363 team members, 267 CVs, 33 investors, 219 docs,
  189 evals. Corpus = low hundreds of docs → full re-index costs pennies; exploit that.
- **Markdown lives in GCS today**, DB holds pointers. Prod `doc-extract/` is **empty** —
  backfill from **eval bundles** (`reports/{slug}_{uuid}.json`: `premium_markdown`,
  `pitchDeckContent`, `websiteContent`, `section_results`; two path generations exist).
- **Entity rich-text is TipTap HTML** — convert before embedding. Profile field lists:
  `x1-app/app/Services/ReportChatService.php:127`.
- **pgvector 0.8.1 installed on test; NOT yet enabled on prod.** PG 16.13. No BM25 extension
  possible on Cloud SQL (closed allowlist) — lexical leg = tuned native FTS.
- **Leftover schemas on test** from the aborted attempt: `advisor_obs` (8.77M rows),
  `advisor_evidence` (1536-dim vectors) — drop/truncate pending David's confirm.
- `.env` has OPENAI/GOOGLE/DEEPSEEK/JINA/Langfuse keys; **missing `ANTHROPIC_API_KEY` and
  `VOYAGE_API_KEY`** (David must add).

## Database access

Recipe: `/home/david/code/x1/x1-link/.claude/skills/database-connection/SKILL.md`.
test = `x1-sql-test`/`x1-db-test` (auto-suspends; wake via proxy script), prod = `x1-sql`/
`x1-db` (**read-only on app tables, always**; advisor writes only to the `advisor` schema).
Proxy: `bash scripts/cloud-sql-proxy.sh` from `x1-link`; connect via the Unix socket path.
GCS buckets: `x1-app-www-{test,prod}` (prod object versioning is suspended — be careful).

## Open decisions David must confirm (PLAN.md §5)

1. Guardrail-class treatments (audience is decided: founders + investors, cross-user,
   default-open): private-doc handling in cross-user answers, premium purchase-gating
   strictness, never-index list. PLAN.md §5.1.
2. Drop/truncate the leftover `advisor_obs`/`advisor_evidence` schemas.
3. Who runs `CREATE EXTENSION vector` on prod, and when.
4. Budget caps (proposal: $0.50/turn soft, $20/day during dev).
5. Whether the main-agent bake-off (E4a) includes non-Claude candidates.

## Norms (unchanged)

- Stay on `main`, no feature branches. Commit + push when asked. **Never commit `.env`.**
- Use Context7 MCP to verify library APIs (Haystack, Anthropic, pgvector) before writing code.
- No prompt/test-case hacking; no silent truncation (see global + project CLAUDE.md).
- Route **every** LLM/embedding/tool call through `x1_advisor/cost.py`; unknown model must
  raise, never price as $0.
- Reference codebases for ideas (not dependencies): `x1/link/pipeshub-ai`, `x1/dev/chroma`,
  alpha-claw (DeepSeek search provider), old Mastra copilot at `x1/x1-link` (lessons only).
