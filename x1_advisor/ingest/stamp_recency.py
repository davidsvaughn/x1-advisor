"""Stamp eval_recency onto every evaluation chunk (thread-029 follow-up,
David-approved design 2026-08-14).

Companies accumulate evaluations: repeat evaluations of the same deck and
evaluations of older decks. Questions about a company's CURRENT state should
be answerable over its standing assessment without silently discarding
history, so every evaluation's chunks carry one of:

  current             the most recent evaluation of the most recently
                      EVALUATED deck — exactly one per company
  repeat_current_deck an earlier evaluation of that same deck
  prior_deck          an evaluation linked to an older deck
  undetermined        an older evaluation whose deck cannot be resolved
                      (legacy bundles without deck linkage; never guessed)

Rules (David 2026-08-14):
  - deck recency dominates eval recency: if an older deck was re-evaluated
    after the newest deck's evaluation, the newest deck's latest eval is
    still `current`;
  - companies with no deck linkage at all: date order decides — latest is
    `current`, the rest `undetermined` (repeat-vs-prior is unknowable);
  - if the newest evaluation itself has no deck linkage it is `current`
    (freshest standing assessment) and every other evaluation becomes
    `undetermined` — their decks can't be compared to an unknown one;
  - a newer deck uploaded but not yet evaluated changes nothing here (that
    fact is coverage-disclosure material, not a recency class).

The sweep is a pure re-derivation from the company's full evaluation set —
a new evaluation demotes its siblings, so stamps are never patched
incrementally. Deck identity = the bundle's deck_gcs_path (present on all
deck-carrying bundles; numeric deck_document_id is spottier). Metadata-only
updates: embeddings untouched; the corpus watermark moves (by design).

Run:  uv run python -m x1_advisor.ingest.stamp_recency
Also invoked automatically at the end of backfill_evals DB mode.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from typing import Any

from x1_advisor.db import connect

VALUES = ("current", "repeat_current_deck", "prior_deck", "undetermined")


def classify(evals: list[dict[str, Any]],
             deck_dates: dict[str, Any]) -> dict[str, str]:
    """One company's evaluations -> {eval_id: recency class}. Pure function.

    evals: [{"eval_id": str, "date": comparable, "deck_key": str | None}]
    deck_dates: deck_key -> upload date (missing keys rank by newest eval).
    """
    if not evals:
        return {}
    linked = [e for e in evals if e["deck_key"]]

    def deck_rank(key: str):
        # upload date when the deck is on record; otherwise its newest
        # evaluation date as a lower-bound proxy (an eval cannot precede its
        # deck). One axis — an unrecorded newer deck still outranks a
        # recorded ancient one.
        up = deck_dates.get(key)
        if up is None:
            up = max(e["date"] for e in linked if e["deck_key"] == key)
        return (up, key)

    top_deck = (max({e["deck_key"] for e in linked}, key=deck_rank)
                if linked else None)
    newest = max(evals, key=lambda e: (e["date"], str(e["eval_id"])))
    if newest["deck_key"] in (None, top_deck):
        current = newest
    else:
        # deck recency dominates: newest eval belongs to an older deck
        cand = [e for e in linked if e["deck_key"] == top_deck]
        current = max(cand, key=lambda e: (e["date"], str(e["eval_id"])))

    out: dict[str, str] = {}
    for e in evals:
        if e is current:
            out[e["eval_id"]] = "current"
        elif current["deck_key"] is None or e["deck_key"] is None:
            out[e["eval_id"]] = "undetermined"
        elif e["deck_key"] == current["deck_key"]:
            out[e["eval_id"]] = "repeat_current_deck"
        else:
            out[e["eval_id"]] = "prior_deck"
    return out


def sweep(conn) -> Counter:
    """Recompute and apply stamps for every evaluation in the corpus."""
    rows = conn.execute(
        """SELECT e.id::text AS eval_id, e.startup_company_id,
                  coalesce(e.evaluation_date::date, e.created_at::date) AS date
           FROM startup_company_evaluations e"""
    ).fetchall()
    linkage = {
        r["eval_id"]: r["deck_key"]
        for r in conn.execute(
            """SELECT DISTINCT acl_source->>'evaluation_id' AS eval_id,
                      acl_source->>'deck_gcs_path' AS deck_key
               FROM advisor.documents
               WHERE source_type = 'deck_extract' AND superseded_by IS NULL
                 AND acl_source->>'evaluation_id' IS NOT NULL""").fetchall()
    }
    deck_dates = {
        r["file_path"]: r["uploaded"]
        for r in conn.execute(
            "SELECT file_path, created_at::date AS uploaded "
            "FROM startup_company_documents"
        ).fetchall()
    }

    by_company: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_company[r["startup_company_id"]].append(
            {"eval_id": r["eval_id"], "date": r["date"],
             "deck_key": linkage.get(r["eval_id"])})

    stats: Counter = Counter()
    for evals in by_company.values():
        for eval_id, recency in classify(evals, deck_dates).items():
            stats[recency] += 1
            patch = json.dumps({"eval_recency": recency})
            n = conn.execute(
                """UPDATE advisor.doc_chunks c
                   SET metadata = c.metadata || %s::jsonb
                   FROM advisor.documents d
                   WHERE d.id = c.document_id AND d.superseded_by IS NULL
                     AND c.metadata->>'evaluation_id' = %s
                     AND c.metadata->>'eval_recency' IS DISTINCT FROM %s""",
                (patch, eval_id, recency)).rowcount
            conn.execute(
                """UPDATE advisor.documents
                   SET acl_source = acl_source || %s::jsonb
                   WHERE superseded_by IS NULL
                     AND acl_source->>'evaluation_id' = %s
                     AND acl_source->>'eval_recency' IS DISTINCT FROM %s""",
                (patch, eval_id, recency))
            stats["chunks_updated"] += n
    conn.commit()
    return stats


def main() -> None:
    with connect() as conn:
        stats = sweep(conn)
    print("eval_recency sweep:")
    for k in (*VALUES, "chunks_updated"):
        print(f"  {k:22s} {stats.get(k, 0)}")
    sys.exit(0)


if __name__ == "__main__":
    main()
