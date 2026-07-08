"""Embedding index management (PLAN §1 `index_configs` registry).

One row per (embedding_model, dim, distance, chunker_version); each config owns
`advisor.emb_{config_id}` (chunk_id → vector). Chunks/documents are shared across
configs, so standing up a parallel config for a bake-off is just re-embedding —
pennies at this corpus size. Exactly one config is `active` (serves the agent).

Run:  uv run python -m x1_advisor.index --config te3s_1536_ck1 [--activate]
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from openai import OpenAI

from x1_advisor.cost import Tracker, Usage
from x1_advisor.db import connect
from x1_advisor.ingest.chunker import CHUNKER_VERSION

EMBED_BATCH = 256


@dataclass(frozen=True)
class IndexConfig:
    id: str
    embedding_model: str
    dim: int
    distance: str = "cosine"
    chunker_version: str = CHUNKER_VERSION


CONFIGS = {
    "te3s_1536_ck1": IndexConfig("te3s_1536_ck1", "text-embedding-3-small", 1536),
    # E1 bake-off candidates register here (voyage-4 etc. when keys land)
}


def ensure_config(conn, cfg: IndexConfig, activate: bool = False) -> None:
    conn.execute(
        """INSERT INTO advisor.index_configs (id, embedding_model, dim, distance, chunker_version)
           VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
        (cfg.id, cfg.embedding_model, cfg.dim, cfg.distance, cfg.chunker_version),
    )
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS advisor.emb_{cfg.id} (
              chunk_id bigint PRIMARY KEY REFERENCES advisor.doc_chunks(id) ON DELETE CASCADE,
              embedding vector({cfg.dim}) NOT NULL)""",
    )
    conn.execute(
        f"""CREATE INDEX IF NOT EXISTS emb_{cfg.id}_hnsw
            ON advisor.emb_{cfg.id} USING hnsw (embedding vector_cosine_ops)""",
    )
    if activate:
        conn.execute("UPDATE advisor.index_configs SET status='experimental' WHERE status='active'")
        conn.execute("UPDATE advisor.index_configs SET status='active' WHERE id=%s", (cfg.id,))
    conn.commit()


def active_config(conn) -> IndexConfig:
    row = conn.execute(
        "SELECT id FROM advisor.index_configs WHERE status='active'"
    ).fetchone()
    if not row:
        raise RuntimeError("no active index_config — run x1_advisor.index --activate first")
    return CONFIGS[row["id"]]


def embed_texts(client: OpenAI, cfg: IndexConfig, texts: list[str],
                tracker: Tracker) -> list[list[float]]:
    resp = client.embeddings.create(model=cfg.embedding_model, input=texts)
    tracker.log(provider="openai", model=cfg.embedding_model, stage="index.embed",
                usage=Usage(embed_tokens=resp.usage.prompt_tokens))
    return [d.embedding for d in resp.data]


def embed_missing(conn, cfg: IndexConfig, tracker: Tracker) -> int:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    total = 0
    while True:
        rows = conn.execute(
            f"""SELECT c.id, c.text FROM advisor.doc_chunks c
                LEFT JOIN advisor.emb_{cfg.id} e ON e.chunk_id = c.id
                WHERE e.chunk_id IS NULL ORDER BY c.id LIMIT %s""",
            (EMBED_BATCH,),
        ).fetchall()
        if not rows:
            break
        # embeddings API rejects empty strings; chunker never emits them, but guard
        vectors = embed_texts(client, cfg, [r["text"][:32000] for r in rows], tracker)
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO advisor.emb_{cfg.id} (chunk_id, embedding) VALUES (%s, %s::vector)",
                [(r["id"], str(v)) for r, v in zip(rows, vectors)],
            )
        conn.commit()
        total += len(rows)
        print(f"  embedded {total} chunks (${tracker.run_total:.4f})")
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="te3s_1536_ck1", choices=sorted(CONFIGS))
    ap.add_argument("--activate", action="store_true",
                    help="mark this config active (deactivates others)")
    args = ap.parse_args()

    cfg = CONFIGS[args.config]
    tracker = Tracker(run_id=f"index:{cfg.id}")
    with connect() as conn:
        ensure_config(conn, cfg, activate=args.activate)
        n = embed_missing(conn, cfg, tracker)
        count = conn.execute(f"SELECT count(*) c FROM advisor.emb_{cfg.id}").fetchone()["c"]
    print(f"\nconfig {cfg.id}: +{n} embedded, {count} total vectors, "
          f"cost ${tracker.run_total:.4f}")
    sys.exit(0)


if __name__ == "__main__":
    main()
