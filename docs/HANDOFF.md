# X1 Advisor — Handoff

> For an agent picking up this project. Last updated **2026-07-07** (supersedes the 06-14
> version; the review + implementation plan now exist).
> Read order: this file → [`PLAN.md`](PLAN.md) (the working plan — **start here for what to
> do**) → [`ARCHITECTURE.md`](ARCHITECTURE.md) (design rationale) →
> [`ARCHITECTURE-REVIEW.md`](ARCHITECTURE-REVIEW.md) (ground-truth findings + amendments;
> §1, §5, §6.4 are the load-bearing parts).

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
| `x1_advisor/cost.py` | Implemented + tested (multi-provider telemetry) |
| Everything else | **Not built.** No service, no `advisor` schema, no ingestion, no agent |

**Next action: PLAN.md Phase 0** (schema, keys, go/no-go spikes). Spike D (DeepSeek web
search) is already resolved — see the plan; port the reference implementation from
`/home/david/code/davidsvaughn/cedar/alpha-claw/alpha_claw/strategy/engine/deepseek_agentic_provider.py`.

## Decisions locked (don't relitigate without new evidence)

- **Retrieval spine, not SQL-first** (user feels strongly); structured fields = metadata
  filters; profiles = denormalized markdown docs. Haystack (used shallow/swappable, Phase-0
  spikes gate it; thin direct-SDK stack is the named exit ramp). Python service. pgvector in
  the shared Cloud SQL instance, `advisor` schema.
- **Page-level citations v1** (extraction has no bboxes — only `# Page N` delimiters);
  block-index + tiny-ref + server-side validator mechanism per design §8.
- **ACL metadata indexed from day one; v1 audience = admins.** 87% of prod docs are
  `private`; `is_published`/`visibility`/`is_visible`+purchases are real gates. Mandatory
  retriever-level filter, never prompt-level.
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

1. v1 audience (plan assumes admins-only) + later founder/investor rollout shape.
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
