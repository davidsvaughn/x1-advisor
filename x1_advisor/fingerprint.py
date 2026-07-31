"""Behavior fingerprints for turn bundles and run manifests (Gate 1A).

A git SHA does not identify behavior. The worktree is dirty most of the time
during development, and behavior moves without any code change at all — recall
went 0.778 → 0.833 when record summaries landed in the corpus. So "what changed
between these two runs" has to be a field comparison over everything that can
move an answer: code, prompt, tool schemas, index config, corpus content, model,
and policy versions (QA-LOOP-DESIGN §4.1).

Cheap by construction: the only query is a rollup over ~400 document rows, and
the source-tree hash is computed only when the worktree is actually dirty.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

# Bump when the meaning of a stored filter contract changes (registry fields,
# operators, or resolution semantics) — old bundles then compare honestly.
FILTER_CONTRACT_VERSION = 1
# Bump when the ACL class predicates change (retrieval._acl_sql / queries.py).
ACL_POLICY_VERSION = 2      # 2 = structured queries gated (Step 0.1)


def _git(*args: str) -> str | None:
    try:
        p = subprocess.run(["git", *args], capture_output=True, text=True,
                           timeout=5, cwd=REPO_ROOT)
    except Exception:  # noqa: BLE001 — git absent or wedged; caller degrades
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def git_sha() -> str:
    return _git("rev-parse", "--short", "HEAD") or "unknown"


def git_dirty() -> bool:
    """True when tracked files differ from HEAD (untracked files don't count)."""
    out = _git("status", "--porcelain", "--untracked-files=no")
    return bool(out) if out is not None else True   # unknown → assume dirty


def code_fingerprint() -> str:
    """Short human-facing form: `71b13c0` or `71b13c0+dirty`."""
    return git_sha() + ("+dirty" if git_dirty() else "")


def source_tree_sha256() -> str | None:
    """Content hash of tracked source files — what the SHA can't tell you when
    the worktree is dirty. None if git isn't usable."""
    listing = _git("ls-files", "-z", "*.py", "*.sql", "*.yaml", "*.yml", "*.toml")
    if listing is None:
        return None
    h = hashlib.sha256()
    for rel in sorted(p for p in listing.split("\0") if p):
        h.update(rel.encode())
        try:
            h.update(hashlib.sha256((REPO_ROOT / rel).read_bytes()).digest())
        except OSError:
            h.update(b"<missing>")     # deleted-but-tracked is itself a state
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def tool_schema_digest(tools: Iterable[Any]) -> str:
    """Canonical hash of the tool definitions.

    The cached prompt prefix is system prompt **plus tool schemas**, so a tool
    description edit silently invalidates the cache exactly like a prompt edit
    (DESIGN-REVIEW F4 — it happened four times in Phase 4 with no test failure).
    One implementation, two uses: this is both the CI cache pin and a bundle
    fingerprint field. `structured_query`'s description embeds `catalog()`, so
    the digest correctly covers the query registry too.
    """
    canonical = [{"name": t.name, "description": t.description,
                  "parameters": t.parameters}
                 for t in sorted(tools, key=lambda t: t.name)]
    return sha256_text(json.dumps(canonical, sort_keys=True, separators=(",", ":")))


_watermark_cache: tuple[tuple, dict[str, Any]] | None = None


def _corpus_sentinel(conn, config_id: str) -> tuple:
    """Cheap (~100 ms) memoization KEY for the watermark below — the digests
    are the truth; this only decides whether they must be recomputed.

    Counts and max-ids miss an **in-place** update — a re-embed of the same
    chunk_ids, or an ACL/metadata correction — so the sentinel also reads
    xmin, the writing transaction id in the tuple header (never detoasts the
    vectors). Both max(xmin) and sum(xmin) per table:

    * max alone has a real window (Gate 1D review, finding 9): a long-running
      transaction with a LOWER xid committing after a higher xid already
      exists raises nothing. But its in-place update rewrites the tuple's
      xmin, which moves the SUM even when the max stands still.
    * `VACUUM FREEZE` / wraparound can move either backwards — safe direction,
      a mismatch only forces recomputing identical digests.

    Residual window, stated honestly: concurrent updates between two probes
    whose xmin deltas exactly cancel in the sum. With single-writer,
    admin-run ingest that requires an engineered coincidence; if writers ever
    multiply, the upgrade path is an explicit corpus revision bumped by every
    writer, not a cleverer sentinel.
    """
    q = ("SELECT count(*) AS n, coalesce(max(xmin::text::bigint),0) AS mx, "
         "coalesce(sum(xmin::text::bigint),0) AS sx FROM {}")
    rows = [conn.execute(q.format("advisor.documents")).fetchone(),
            conn.execute(q.format("advisor.doc_chunks")).fetchone(),
            conn.execute(q.format(f"advisor.emb_{config_id}")).fetchone()]
    return (config_id, *[(r["n"], r["mx"], r["sx"]) for r in rows])


def corpus_watermark(conn, config_id: str) -> dict[str, Any]:
    """Identity of the corpus that answered this turn.

    Covers everything retrieval reads: document content hashes, chunk **text and
    metadata** (so an ACL or metadata correction moves it), and the embedding
    vectors themselves (so an in-place re-embed moves it). Transaction ids are
    the cache key only — they are local to a database instance and would make
    two identical corpora look different.

    ~1.6 s to compute in full, so it is memoized behind the sentinel above: a
    turn pays ~100 ms while the corpus is unchanged, and the full price only on
    the first turn after it moves.
    """
    global _watermark_cache
    sentinel = _corpus_sentinel(conn, config_id)
    if _watermark_cache and _watermark_cache[0] == sentinel:
        return _watermark_cache[1]

    docs = conn.execute(
        """SELECT count(*) AS documents,
                  coalesce(max(id), 0) AS max_document_id,
                  md5(coalesce(string_agg(content_hash, ',' ORDER BY id), ''))
                      AS document_digest
           FROM advisor.documents WHERE superseded_by IS NULL"""
    ).fetchone()
    chunks = conn.execute(
        """SELECT count(*) AS chunks,
                  md5(coalesce(string_agg(
                      md5(c.text || coalesce(c.metadata::text, '')), ',' ORDER BY c.id
                  ), '')) AS chunk_digest
           FROM advisor.doc_chunks c
           JOIN advisor.documents d ON d.id = c.document_id
           WHERE d.superseded_by IS NULL"""
    ).fetchone()
    emb = conn.execute(
        f"""SELECT count(*) AS embeddings,
                   md5(coalesce(string_agg(md5(e.embedding::text), ',' ORDER BY e.chunk_id),
                                '')) AS embedding_digest
            FROM advisor.emb_{config_id} e"""
    ).fetchone()
    watermark = {**dict(docs), **dict(chunks), **dict(emb)}
    _watermark_cache = (sentinel, watermark)
    return watermark


def corpus_text_watermark(conn) -> dict[str, Any]:
    """Identity of the corpus TEXT — what an offline scan actually reads.

    Deliberately narrower than `corpus_watermark`: a truth set (GOLDEN-V2-DESIGN
    §5.1) is a deterministic scan over document text, so a re-embed under a new
    index config must not mark it stale, while an edit to a single chunk must.
    Embeddings and config id are therefore excluded, and the result is comparable
    across index configs.
    """
    docs = conn.execute(
        """SELECT count(*) AS documents,
                  md5(coalesce(string_agg(content_hash, ',' ORDER BY id), ''))
                      AS document_digest
           FROM advisor.documents WHERE superseded_by IS NULL"""
    ).fetchone()
    chunks = conn.execute(
        """SELECT count(*) AS chunks,
                  md5(coalesce(string_agg(
                      md5(c.text || coalesce(c.metadata::text, '')), ',' ORDER BY c.id
                  ), '')) AS chunk_digest
           FROM advisor.doc_chunks c
           JOIN advisor.documents d ON d.id = c.document_id
           WHERE d.superseded_by IS NULL"""
    ).fetchone()
    return {**dict(docs), **dict(chunks)}


def run_fingerprint(conn, *, config_id: str) -> dict[str, Any]:
    """The part of a fingerprint that does not involve a model or a prompt.

    Retrieval-only runs need this: recall moved 0.778 → 0.833 with no code
    change at all when record summaries landed, so a manifest carrying just a
    git sha cannot explain its own numbers.
    """
    dirty = git_dirty()
    return {
        "git_sha": git_sha(),
        "worktree_dirty": dirty,
        # only meaningful when the SHA is not the whole story
        "source_tree_sha256": source_tree_sha256() if dirty else None,
        "config_id": config_id,
        "corpus_watermark": corpus_watermark(conn, config_id),
        "filter_contract_version": FILTER_CONTRACT_VERSION,
        "acl_policy_version": ACL_POLICY_VERSION,
    }


def turn_fingerprint(conn, *, prompt: str, tools: Sequence[Any],
                     agent_model: str, config_id: str,
                     agent_model_resolved: str | None = None,
                     feature_flags: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        **run_fingerprint(conn, config_id=config_id),
        "prompt_sha256": sha256_text(prompt),
        "tool_schema_sha256": tool_schema_digest(tools),
        # `agent_model` is what we asked for ("gpt-5.1"); `agent_model_resolved`
        # is what the provider actually served ("gpt-5.1-2025-11-13"). A silent
        # snapshot bump moves behavior and only the resolved id shows it.
        "agent_model": agent_model,
        "agent_model_resolved": agent_model_resolved,
        "feature_flags": feature_flags or {},
    }
