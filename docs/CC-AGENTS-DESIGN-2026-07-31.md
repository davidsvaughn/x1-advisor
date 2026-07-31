# Headless Claude Code agents in x1-advisor — assessment + adoption design

> Date: 2026-07-31. Status: **adopted** (David, 2026-07-31: all three adoption
> paths approved — "I like all 3 ideas and want to implement all").
> Origin: the helm pattern in alpha-claw
> (`~/code/davidsvaughn/cedar/alpha-claw/docs/meta/helm/README.md`, §"the debate
> roles become full headless Claude Code agents") — `claude -p` stream-json
> runner, settings-scoped sandbox, read-only DB role, env allowlist,
> proposal-only power boundary, subscription-billed on David's Claude Max seat.
> David asked whether the same pattern fits x1-advisor.

---

## 1. Two questions untangled

The inquiry bundles two decisions that must be separated:

1. **Claude as the model** — running the existing Haystack agent loop on a
   Claude model via API instead of `gpt-5.6-terra`. Already anticipated
   (PLAN E4a; generator registry); a config change once `ANTHROPIC_API_KEY`
   lands. Citations, ACL, QA machinery, `cost.py` all unchanged. This doc does
   **not** decide E4a — it stays a bake-off.
2. **Claude Code as the harness** — replacing the agent loop with headless CC
   sessions, helm-style. Decision (this doc): **wrong shape for the Tier-1
   chat product; right shape for three job-shaped workloads at the edges.**

## 2. The billing boundary

A consumer subscription (Claude Max) is one person's seat. Helm legitimately
runs on it because there is one user (David) and ~one session a day. The
advisor's production runtime serves *other users* — running that on a personal
subscription is a terms violation and an operational trap (shared rate limits,
no SLA, N concurrent users throttling one seat). Raw API calls cannot bill to
a subscription at all; only the vendor's own harness can, and that harness is
single-seat by design. (Same wall the earlier ChatGPT-Team inquiry hit.)

**Rule: subscription billing backs David-seat dev work only; anything
multi-user or production-path runs on API billing through `cost.py`.**

| Workload | Harness | Billing | Status |
|---|---|---|---|
| Tier-1 chat (prod, multi-user) | Haystack loop (unchanged) | API, company key | unchanged — never subscription |
| H1 QA/teacher jobs | headless CC | Claude Max (David seat) | **adopt now** |
| H2 research-note cron | headless CC | subscription during pilot → API if promoted | design → pilot after v2.0 baseline |
| H3 Tier-2 deep mode | CC / Agent SDK over MCP tools | subscription (admin pilot) → API (prod) | named shape; build on demand |
| E4a Claude-model datapoint | existing loop, via API | API | when `ANTHROPIC_API_KEY` lands |

Note on metering: subscription usage is not dollar-metered per call, so H1/H2
pilot jobs log turn counts + persisted transcripts instead of `cost.py` rows.
Anything promoted to API billing rejoins `cost.py` with no exceptions.

## 3. Why the CC harness stays out of Tier-1

1. **Latency.** Helm's session budget is 1800s; the advisor's mean turn is
   8.7s. Process spawn + harness overhead + an autonomous tool loop is a
   minutes-shaped tool; chat is a seconds-shaped job.
2. **The ACL boundary inverts.** Helm has one principal — a read-only DB role
   that sees what David may see. The advisor's Gate 2 design is
   *per-requesting-user* class predicates applied inside retrieval. A CC agent
   with psql sees whatever the role sees; per-user gating would need per-user
   roles or MCP-only tools carrying auth context — and stripping Bash/psql
   discards most of what the harness buys.
3. **It un-builds the Gate 1D QA machinery.** The judge grades per-ref
   snapshots of exactly what the model saw — a pure function of the turn
   bundle. Claude Code owns its own context (compaction, tool-result
   management), destroying that property; frozen-tool replay becomes
   practically impossible.
4. **Cost discipline fuzzes.** Per-turn caps and cost attribution depend on
   `cost.py` metering every call; subscription usage can't be metered that way.

The Haystack loop owning the turn is what makes citations, ACLs, and the QA
loop *enforceable in code*. That is the product's spine; it stays.

## 4. H1 — QA-side headless agents (adopt now)

Formalizes what already happens ad hoc (the teacher loop *is* Claude Code) into
scheduled, sandboxed, journaled jobs on David's subscription seat.

**Jobs (initial set):**
- **Nightly golden run + triage:** smoke (+ core, once v2.0 lands) → funnel →
  comparator vs the last accepted baseline → a morning triage report (md, in
  `.qa-artifacts/reports/`). Failures arrive pre-classified, not discovered
  mid-session.
- **Truth-set rebuild** on corpus content-hash change
  (GOLDEN-V2-DESIGN §5.1) — a stale truth set fails loudly.
- **Calibration batch prep:** fresh blind pairs staged for the labeling UI
  when label count drops below target.
- **Held-out batch execution.** Gate 4 requires held-out case bodies never
  mounted in the teacher's context. A *separate* headless CC session with its
  own context executes `.qa-artifacts/heldout/`, reporting only aggregate
  metrics + funnel labels back. **H1 is the concrete near-term form of Gate
  4's "separately authorized CI/evaluation service."**
- **Second-agent review passes** on milestone commits (the review loop David
  currently runs by hand).

**Sandbox recipe (ported from helm, live-probed there):** settings-scoped
`claude -p` with stream-json; full transcript persisted (0600, untracked,
no-silent-truncation applies); read-only DB principal for any direct SQL;
`.env`/credential paths denied via BOTH Read and Bash; subprocess env built
from an allowlist; no curl/wget/interpreters — web egress only through
transcripted WebSearch/WebFetch; bounded max-turns and timeout per job.

**Guardrails:** QA agents never write tracked files, never commit/push, never
touch prod. Artifacts land in `.qa-artifacts/`; reports are for David + the
teacher session.

Effort: ~1 day (runner script + settings profiles + first cron).

## 5. H2 — the research-note flywheel (first product-facing use)

ARCHITECTURE §4.1 promises "the agent's own output becomes first-class
retrievable evidence — the knowledge base compounds," but nothing produces
that today. H2 builds it: a scheduled headless CC job does deep, slow research
no chat turn can afford, and the results ingest as `source_type=
'research_note'` documents — entity-linked, content-hashed, version-and-append.

**Initial job candidates:**
1. **New-evaluation sweep** — on ingest of a new eval bundle: cross-doc
   synthesis (deck vs eval vs website tensions) + web context for the entity.
2. **Stale-profile web refresh** — fresh web evidence on corpus entities whose
   material is aging.
3. **Thematic corpus scans** — pre-computed enumeration reports (regulatory
   risk, GTM concerns) using the truth-set/scan logic for coverage honesty.
   Partially superseded when `scan_text` goes live; their durable value is the
   synthesis layer + web enrichment on top.

**Design constraints (the load-bearing part):**

- **Cite-through, or no ingest.** Research notes are generated text — the
  same class Gate 1B spent itself making non-citable when record summaries
  were being cited. The difference in kind: a research note is a first-class
  *document* carrying its **own resolvable citation trail** to underlying
  evidence (corpus refs + web URLs, validated by the same citation
  post-validator before ingest). When the advisor later cites a research
  note, the UI must be able to follow through to the note's sources. A note
  whose citations don't validate is rejected, not ingested. No bare
  assertions laundered into "evidence."
- **ACL non-laundering.** Notes inherit **max-restrictive** ACL from their
  source evidence (the deck-inheritance pattern): a note derived from
  premium/purchase-gated text is itself gated; web + open-source-only notes
  may be x1-visible. The job runs under an explicit principal; admin-shadow
  content never lands open.
- **Freshness.** Notes are dated, superseded via version-and-append when
  their inputs change (content-hash sweep), and clearly self-identify as
  agent-authored with a generation date in the rendered text.
- **Quality is measurable by the existing machinery.** A research note is an
  answer with citations — the faithfulness/coverage judge grades it exactly
  like a turn. H2 therefore sequences **after the golden v2.0 baseline**, so
  note quality has a calibrated yardstick from day one.

Pilot: test corpus only, subscription-billed, low cadence (nightly or
on-ingest). Promotion to prod requires: Gate 3A infra, API billing, and judged
note quality on record.

Effort: design note refinement + ingestion path + first job ≈ 2–3 days.

## 6. H3 — Tier-2 deep mode, named shape (build on demonstrated demand)

Helm is a proven Tier-2 harness: opt-in "go deep," minutes-scale background
job, cited report out, proposal-only power boundary. The adaptation for a
multi-user product:

- **MCP tools carry the boundary.** The deep agent gets **only** MCP tools
  wrapping the same `build_tools` closures under the requesting principal's
  auth context (`search_corpus`, `get_source`, `structured_query`, web). No
  Bash, no filesystem beyond scratch. ACL enforcement stays where it lives
  today — inside the tools — so the harness is swappable, not load-bearing.
- **Same output contract.** Draft report with tiny refs → the same citation
  post-validator → the same evidence store. A turn-bundle adapter captures
  the MCP tool calls so deep turns stay judgeable/replayable — this adapter
  is the main QA-machinery work item when H3 builds.
- **Billing:** admin-only pilot may run on the David-seat subscription
  (single user by definition); production runs Agent SDK + API key through
  `cost.py`.
- **Trigger unchanged** from ARCHITECTURE §7 / PLAN "Later": demonstrated
  demand — the likely tripwire is SCAN-A-class questions overflowing Tier-1
  budgets once golden v2 starts measuring them honestly.

Effort now: zero. This section exists so the shape is named before demand
arrives, not designed under it.

## 7. Sequencing

Golden v2.0 (GOLDEN-V2-DESIGN steps 1–4) remains the priority — H1 lands
alongside it because it *accelerates* Gate 4 (nightly runs, truth-set
rebuilds, held-out execution). H2 follows the v2.0 baseline. H3 waits for
demand. Nothing here blocks Gates 2/3A.

## 8. Open items (David)

1. Confirm the claude-cli auth on this machine is the seat H1/H2 should bill
   to (it is the same Max plan the teacher session runs on).
2. Approve the H2 cite-through rule and max-restrictive ACL inheritance as
   stated (they become product policy the first time a note ingests).
3. Confirm the H2 job list / cadence (nightly vs on-ingest).
4. When H2 promotes beyond pilot: company API key decision (Anthropic key
   still missing per PLAN Phase 0).
