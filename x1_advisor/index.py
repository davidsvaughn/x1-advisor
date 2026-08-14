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
    """Embed every chunk missing from the config's vector table.

    Batches run CONCURRENTLY (2026-08-13, David: the serial loop was the one
    bulk API job left un-parallelized — a corpus-scale rebuild took ~25 min
    that a pooled loop does in ~5). Each worker owns its own DB connection
    (psycopg connections are not shared across threads); the INSERT is
    ON CONFLICT DO NOTHING so a racing writer — another worker or another
    process — degrades to wasted work, never a crash or a corrupt row.
    ADVISOR_EMBED_WORKERS tunes the pool (default 8 — held up clean
    against the OpenAI tier on the 2026-08-14 research-split re-embed)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from x1_advisor.db import connect

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    ids = [r["id"] for r in conn.execute(
        f"""SELECT c.id FROM advisor.doc_chunks c
            LEFT JOIN advisor.emb_{cfg.id} e ON e.chunk_id = c.id
            WHERE e.chunk_id IS NULL ORDER BY c.id""").fetchall()]
    if not ids:
        return 0
    batches = [ids[i:i + EMBED_BATCH] for i in range(0, len(ids), EMBED_BATCH)]
    workers = max(1, int(os.environ.get("ADVISOR_EMBED_WORKERS", "8")))
    done = 0
    lock = threading.Lock()

    def one(batch_ids: list[int]) -> int:
        nonlocal done
        with connect() as wconn:
            rows = wconn.execute(
                "SELECT id, text FROM advisor.doc_chunks WHERE id = ANY(%s) "
                "ORDER BY id", (batch_ids,)).fetchall()
            # embeddings API rejects empty strings; chunker never emits them
            vectors = embed_texts(client, cfg,
                                  [r["text"][:32000] for r in rows], tracker)
            with wconn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO advisor.emb_{cfg.id} (chunk_id, embedding) "
                    "VALUES (%s, %s::vector) ON CONFLICT (chunk_id) DO NOTHING",
                    [(r["id"], str(v)) for r, v in zip(rows, vectors)],
                )
            wconn.commit()
        with lock:
            done += len(rows)
            print(f"  embedded {done}/{len(ids)} chunks (${tracker.run_total:.4f})",
                  flush=True)
        return len(rows)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return sum(pool.map(one, batches))


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
