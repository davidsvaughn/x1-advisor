"""Record-summary blocks (PLAN Phase 1 leftover): one LLM-written summary chunk per
live document, granularity='record_summary', block_index=10000 (sentinel above any
real block; chunker ck1 tops out well below). Summaries carry the same ACL metadata
as the document's block chunks and get embedded by index.embed_missing like any
chunk — they give dense retrieval a whole-document handle (E4b: this is where the
cheap-model experiment lives; gpt-5-mini first).

Run:  uv run python -m x1_advisor.ingest.summaries [--limit N]
Idempotent: docs with an existing record_summary chunk are skipped.
Cost: ~412 docs x ~2.5k tokens on gpt-5-mini ≈ $0.25 one-time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from openai import OpenAI

from x1_advisor.cost import Tracker, Usage
from x1_advisor.db import connect

MODEL = "gpt-5-mini"
SUMMARY_BLOCK_INDEX = 10_000
DOC_HEAD_CHARS = 6_000

PROMPT = (
    "Write a 2-4 sentence summary of this document for a search index. Name the "
    "company/person and the document kind, then the load-bearing facts (what they "
    "do, key numbers, conclusions). No preamble.\n\nTITLE: {title}\nKIND: {kind}\n"
    "---\n{body}"
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    tracker = Tracker(run_id="record-summaries")
    done = failed = 0
    with connect() as conn:
        rows = conn.execute(
            """SELECT d.id, d.title, d.source_type, d.markdown,
                      (SELECT c.metadata FROM advisor.doc_chunks c
                       WHERE c.document_id = d.id AND c.granularity = 'block'
                       LIMIT 1) AS meta
               FROM advisor.documents d
               WHERE d.superseded_by IS NULL
                 AND NOT EXISTS (SELECT 1 FROM advisor.doc_chunks c
                                 WHERE c.document_id = d.id
                                   AND c.granularity = 'record_summary')
               ORDER BY d.id""",
        ).fetchall()
        if args.limit:
            rows = rows[: args.limit]
        print(f"{len(rows)} documents need record summaries")

        for row in rows:
            try:
                resp = client.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "user", "content": PROMPT.format(
                        title=row["title"], kind=row["source_type"],
                        body=row["markdown"][:DOC_HEAD_CHARS])}],
                )
                tracker.log(provider="openai", model=MODEL, stage="ingest.record_summary",
                            usage=Usage.from_haystack_meta("openai", resp.usage.model_dump()))
                summary = (resp.choices[0].message.content or "").strip()
                if not summary:
                    raise ValueError("empty summary")
                conn.execute(
                    """INSERT INTO advisor.doc_chunks
                         (document_id, block_index, granularity, text, metadata)
                       VALUES (%s, %s, 'record_summary', %s, %s)""",
                    (row["id"], SUMMARY_BLOCK_INDEX, summary,
                     json.dumps(row["meta"] or {}, default=str)),
                )
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
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
