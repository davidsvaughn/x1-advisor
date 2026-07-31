# `experiments/runs/` — body-free comparison manifests

Two artifact classes, deliberately separate (QA-LOOP-DESIGN §4.1 "Storage"):

| Where | What | Committed? |
|---|---|---|
| **Postgres `advisor.turns.research_record`** | canonical turn bundle (v2) — transactional, queryable | n/a |
| **`.qa-artifacts/runs/`** | complete bundle exports: every message, every tool result, evidence text | **never** — gitignored, `0700`/`0600`, opt-in retention |
| **`experiments/runs/`** (here) | body-free manifests: fingerprints, case ids, metrics, costs, opaque evidence identifiers | yes, after review |

A manifest here may be committed only when it carries neither entitled text nor
restricted-existence metadata. `bundle.manifest_record()` is the projection that
enforces this: it keeps fingerprints, per-step usage, citation *identifiers*
(`document_id` / `block_index` / `page_number` / web `url`), retrieval leg sizes
and returned chunk ids — and drops answer text, evidence text and source titles.

One nuance: `retrieval[].filter_notes` can quote stored company names (the
"did you mean" suggestions). That is safe by construction — `filters.known_values()`
computes suggestions over the **default-open** corpus only, so a note can never
reveal a draft, private, hidden or purchase-gated document's existence.

Naming is `{date}_{config}_{golden}_{git-sha}[+dirty]_r{n}.jsonl`. Files are
created with `O_EXCL`, so a rerun allocates the next sequence number and can
never overwrite an earlier run (`experiments/manifest.py`). `+dirty` means the
worktree differed from HEAD, so the SHA does not describe the code that ran —
the bundle's `source_tree_sha256` does.

## Legacy artifacts (pre-Gate-1A)

`2026-07-08_active_v1.jsonl`, `2026-07-08_active_v1_rerank.jsonl` and
`2026-07-08_agent_v1.jsonl` predate this split. The agent manifest contains
**generated answer text and source titles**; the retrieval manifests contain
document titles. They are kept as historical provenance for the numbers quoted
in `docs/DECISIONS.md` — they are **not** a template for the v2 writer, and new
runs must not be modelled on them. All three cover the test corpus under an
admin ACL, so nothing in them is entitled to a narrower audience than the repo.
