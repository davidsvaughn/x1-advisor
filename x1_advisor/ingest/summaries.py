"""Record-summary blocks: one LLM-written summary chunk per live document,
granularity='record_summary', block_index=10000 (sentinel above any real block;
chunker ck1 tops out well below). Summaries carry the same ACL metadata as the
document's block chunks and get embedded by index.embed_missing like any chunk —
they give dense retrieval a whole-document handle. Model: E4b bake-off ran
2026-08-13 (gpt-5-mini vs 5.4-nano vs 5.6-luna — blind judge round + full
corpus-state retrieval arms); gpt-5.6-luna won on recall and is the default
(ADVISOR_SUMMARY_MODEL overrides; changes go through DECISIONS).

**Whole-document coverage (Gate 1B).** The first version summarized
`markdown[:6000]` while the prompt asked for a summary of "this document" —
and 243 of 412 documents are longer than that, so the majority of summaries
described a fraction of their source while presenting as document-level. They
are also retrieval bait: a summary is what makes a long document findable at
all. So this version reads the whole document through an explicit map/reduce —
windows summarized independently, then combined — and stamps the coverage it
achieved into chunk metadata. A partial summary is never silent again.

Record summaries are **retrieval-only**: `search_corpus` substitutes the
document's best source block before evidence reaches the model
(`retrieval._expand_summaries`). They route; they are never cited.

Run:  uv run python -m x1_advisor.ingest.summaries [--limit N] [--refresh]
      uv run python -m x1_advisor.index          # embed the new/changed chunks
`--refresh` regenerates existing summaries (and drops their embeddings so the
indexer redoes them); without it, documents that already have one are skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from openai import OpenAI

from x1_advisor.cost import Tracker, Usage
from x1_advisor.db import connect

MODEL = os.environ.get("ADVISOR_SUMMARY_MODEL", "gpt-5.6-luna")
SUMMARY_BLOCK_INDEX = 10_000
MAP_WINDOW_CHARS = 12_000      # per map-step window; a 60k document → 5 windows

SINGLE_PROMPT = (
    "Write a 2-4 sentence summary of this document for a search index. Name the "
    "company/person and the document kind, then the load-bearing facts (what they "
    "do, key numbers, conclusions). No preamble.\n\nTITLE: {title}\nKIND: {kind}\n"
    "---\n{body}"
)
MAP_PROMPT = (
    "This is PART {i} of {n} of one document. Write 2-3 sentences capturing only "
    "what THIS part contains: the load-bearing facts, numbers and conclusions. "
    "Do not speculate about the other parts. No preamble.\n\n"
    "TITLE: {title}\nKIND: {kind}\n--- PART {i}/{n} ---\n{body}"
)
REDUCE_PROMPT = (
    "Below are summaries of every part of one document, in order. Write a single "
    "2-4 sentence summary of the WHOLE document for a search index. Name the "
    "company/person and the document kind, then the load-bearing facts across all "
    "parts. No preamble.\n\nTITLE: {title}\nKIND: {kind}\n---\n{parts}"
)


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 — surfaced by the caller
        return exc


def split_windows(markdown: str, limit: int = MAP_WINDOW_CHARS) -> list[str]:
    """Whole-document coverage: every character lands in exactly one window.

    Splits on paragraph boundaries so a window is readable; a single paragraph
    longer than the limit is hard-split rather than dropped.
    """
    windows: list[str] = []
    current = ""
    for para in markdown.split("\n\n"):
        while len(para) > limit:
            if current:
                windows.append(current)
                current = ""
            windows.append(para[:limit])
            para = para[limit:]
        if not current:
            current = para
        elif len(current) + 2 + len(para) <= limit:
            current += "\n\n" + para
        else:
            windows.append(current)
            current = para
    if current:
        windows.append(current)
    return windows or [""]


def main() -> None:
    global MODEL
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--refresh", action="store_true",
                    help="regenerate summaries that already exist")
    ap.add_argument("--model", default=MODEL,
                    help="summary model override (E4b bake-off arm; the "
                         "default only changes via DECISIONS)")
    args = ap.parse_args()
    MODEL = args.model

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    tracker = Tracker(run_id="record-summaries")
    done = failed = 0
    with connect() as conn:
        having = "" if args.refresh else """
                 AND NOT EXISTS (SELECT 1 FROM advisor.doc_chunks c
                                 WHERE c.document_id = d.id
                                   AND c.granularity = 'record_summary')"""
        rows = conn.execute(
            f"""SELECT d.id, d.title, d.source_type, d.markdown,
                       (SELECT c.metadata FROM advisor.doc_chunks c
                        WHERE c.document_id = d.id AND c.granularity = 'block'
                        LIMIT 1) AS meta
                FROM advisor.documents d
                WHERE d.superseded_by IS NULL {having}
                ORDER BY d.id""",
        ).fetchall()
        if args.limit:
            rows = rows[: args.limit]
        multi = sum(1 for r in rows if len(r["markdown"]) > MAP_WINDOW_CHARS)
        print(f"{len(rows)} documents to summarize ({multi} need map/reduce)")

        from concurrent.futures import ThreadPoolExecutor

        # summaries need no deliberation — run at the model's lowest effort
        # tier. gpt-5-mini's generation calls it "minimal"; 5.4/5.6 renamed
        # it "none". Resolved on first 400 and cached.
        effort = {"value": "minimal"}

        def ask(prompt: str) -> str:
            from openai import BadRequestError
            for tier in (effort["value"], "none"):
                try:
                    resp = client.chat.completions.create(
                        model=MODEL,
                        reasoning_effort=tier,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    effort["value"] = tier
                    break
                except BadRequestError as exc:
                    if "reasoning_effort" not in str(exc):
                        raise
            tracker.log(provider="openai", model=MODEL,
                        stage="ingest.record_summary",
                        usage=Usage.from_haystack_meta(
                            "openai", resp.usage.model_dump()))
            return (resp.choices[0].message.content or "").strip()

        def summarize(row: dict) -> tuple[dict, str, dict]:
            body = row["markdown"] or ""
            windows = split_windows(body)
            fmt = {"title": row["title"], "kind": row["source_type"]}
            if len(windows) == 1:
                summary = ask(SINGLE_PROMPT.format(body=windows[0], **fmt))
            else:
                parts = [ask(MAP_PROMPT.format(body=w, i=i, n=len(windows), **fmt))
                         for i, w in enumerate(windows, 1)]
                summary = ask(REDUCE_PROMPT.format(
                    parts="\n".join(f"PART {i}: {p}" for i, p in enumerate(parts, 1)),
                    **fmt))
            # provenance of the summary itself: how much of the document it saw,
            # so a partial summary can never masquerade as a whole one
            prov = {"summary_windows": len(windows),
                    "summary_source_chars": sum(len(w) for w in windows),
                    "summary_document_chars": len(body),
                    "summary_model": MODEL}
            return row, summary, prov

        with ThreadPoolExecutor(max_workers=8) as pool:
            for future_result in pool.map(lambda r: _safe(summarize, r), rows):
                if isinstance(future_result, Exception):
                    failed += 1
                    print(f"  FAIL: {future_result}", file=sys.stderr)
                    continue
                row, summary, prov = future_result
                try:
                    if not summary:
                        raise ValueError("empty summary")
                    meta = {**(row["meta"] or {}), **prov}
                    existing = conn.execute(
                        """SELECT id FROM advisor.doc_chunks
                           WHERE document_id = %s AND granularity = 'record_summary'""",
                        (row["id"],)).fetchone()
                    if existing:
                        conn.execute(
                            """UPDATE advisor.doc_chunks SET text = %s, metadata = %s
                               WHERE id = %s""",
                            (summary, json.dumps(meta, default=str), existing["id"]))
                        # the old vector describes text that no longer exists;
                        # drop it so index.embed_missing regenerates it
                        for tbl in conn.execute(
                            """SELECT table_name FROM information_schema.tables
                               WHERE table_schema = 'advisor'
                                 AND table_name LIKE 'emb\\_%'""").fetchall():
                            conn.execute(
                                f"DELETE FROM advisor.{tbl['table_name']} "
                                "WHERE chunk_id = %s", (existing["id"],))
                    else:
                        conn.execute(
                            """INSERT INTO advisor.doc_chunks
                                 (document_id, block_index, granularity, text, metadata)
                               VALUES (%s, %s, 'record_summary', %s, %s)""",
                            (row["id"], SUMMARY_BLOCK_INDEX, summary,
                             json.dumps(meta, default=str)))
                    conn.commit()
                    done += 1
                    if done % 50 == 0:
                        print(f"  {done} done (${tracker.run_total:.4f})")
                except Exception as exc:  # noqa: BLE001 — record and continue
                    failed += 1
                    print(f"  FAIL doc {row['id']}: {exc}", file=sys.stderr)
                    conn.rollback()

    print(f"\nrecord summaries: {done} written, {failed} failed, "
          f"${tracker.run_total:.4f}")
    print("next: uv run python -m x1_advisor.index   # embed the changed chunks")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
