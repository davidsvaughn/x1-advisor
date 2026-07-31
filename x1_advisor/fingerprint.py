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


def corpus_watermark(conn) -> dict[str, Any]:
    """Identity of the corpus that answered this turn.

    `content_digest` covers every live document's content hash, so a re-ingest
    that changes text moves the watermark even when the document count doesn't.
    """
    docs = conn.execute(
        """SELECT count(*) AS documents,
                  coalesce(max(id), 0) AS max_document_id,
                  md5(coalesce(string_agg(content_hash, ',' ORDER BY id), ''))
                      AS content_digest
           FROM advisor.documents WHERE superseded_by IS NULL"""
    ).fetchone()
    chunks = conn.execute(
        """SELECT count(*) AS chunks FROM advisor.doc_chunks c
           JOIN advisor.documents d ON d.id = c.document_id
           WHERE d.superseded_by IS NULL"""
    ).fetchone()
    return {**dict(docs), **dict(chunks)}


def turn_fingerprint(conn, *, prompt: str, tools: Sequence[Any],
                     agent_model: str, config_id: str,
                     feature_flags: dict[str, Any] | None = None) -> dict[str, Any]:
    dirty = git_dirty()
    return {
        "git_sha": git_sha(),
        "worktree_dirty": dirty,
        # only meaningful when the SHA is not the whole story
        "source_tree_sha256": source_tree_sha256() if dirty else None,
        "prompt_sha256": sha256_text(prompt),
        "tool_schema_sha256": tool_schema_digest(tools),
        "config_id": config_id,
        "corpus_watermark": corpus_watermark(conn),
        "agent_model": agent_model,
        "filter_contract_version": FILTER_CONTRACT_VERSION,
        "acl_policy_version": ACL_POLICY_VERSION,
        "feature_flags": feature_flags or {},
    }
