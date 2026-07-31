# `experiments/runs/` — body-free comparison manifests

Two artifact classes, deliberately separate (QA-LOOP-DESIGN §4.1 "Storage"):

| Where | What | Committed? |
|---|---|---|
| **Postgres `advisor.turns.research_record`** | canonical turn bundle (v2) — transactional, queryable | n/a |
| **`.qa-artifacts/runs/`** | complete bundle exports: every message, every tool result, evidence text | **never** — gitignored, `0700`/`0600`, opt-in retention |
| **`experiments/runs/`** (here) | body-free manifests: fingerprints, case ids, metrics, costs, opaque evidence identifiers | yes, after review |

A manifest here may be committed only when it carries neither entitled text nor
restricted-existence metadata. Both writers enforce it:

- **agent mode** — `bundle.manifest_record()` keeps fingerprints, per-step
  usage, citation *identifiers* (`document_id` / `block_index` / `page_number` /
  web `url`), retrieval leg sizes and returned chunk ids; it drops answer text,
  evidence text and source titles.
- **retrieval mode** — per-hit `document_id` / `chunk_id` / `block_index` /
  `page_number` / `source_type` / `granularity` / ranks. **No titles.** The
  golden question text is kept because the case is already committed in
  `experiments/golden/`, so it discloses nothing the repo does not already hold.

Internal surrogate ids (`document_id`, `chunk_id`) are the opaque identifier
here: they name nothing on their own and resolve only against the database,
which anyone able to resolve them can already read directly. Titles are
different — a title names its source in plain text, which is why they are out.

One nuance: `retrieval[].filter_notes` can quote stored company names (the
"did you mean" suggestions). That is safe by construction — `filters.known_values()`
computes suggestions over the **default-open** corpus only, so a note can never
reveal a draft, private, hidden or purchase-gated document's existence.

Naming is `{date}_{config}_{golden}_{git-sha}[+dirty]_r{n}.jsonl`. Files are
created with `O_EXCL`, so a rerun allocates the next sequence number and can
never overwrite an earlier run (`experiments/manifest.py`). `+dirty` means the
worktree differed from HEAD, so the SHA does not describe the code that ran —
the bundle's `source_tree_sha256` does.

## Withdrawn

`2026-07-30_active_v1_e72ef89+dirty_r2.jsonl` was written by the retrieval-mode
writer **after** this contract was documented but **before** the writer was
migrated, so it carried 350 source titles including 30 premium-report
identities. It was removed rather than grandfathered; its numbers are unchanged
and reproduced in `2026-07-30_active_v1_1995bfe+dirty_r1.jsonl`. It remains in
git history — all of it is test-corpus material retrieved under an admin ACL, so
this is a contract violation to correct, not an incident.

## Legacy artifacts (pre-Gate-1A)

`2026-07-08_active_v1.jsonl`, `2026-07-08_active_v1_rerank.jsonl` and
`2026-07-08_agent_v1.jsonl` predate this split. The agent manifest contains
**generated answer text and source titles**; the retrieval manifests contain
document titles. They are kept as historical provenance for the numbers quoted
in `docs/DECISIONS.md` — they are **not** a template for the v2 writer, and new
runs must not be modelled on them. All three cover the test corpus under an
admin ACL, so nothing in them is entitled to a narrower audience than the repo.
