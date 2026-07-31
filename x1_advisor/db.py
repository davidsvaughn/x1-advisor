"""Database access for the advisor service.

App tables are read-only, always; writes go only to the `advisor` schema.
Connection is via the cloud-sql-proxy Unix socket (dev) or the Cloud SQL
connector socket path (deploy) — both are just a socket-dir `host` to psycopg.

Two access patterns, deliberately separate:

* `connect()` — one dedicated connection. For CLI tools, ingest sweeps and the
  eval harness: single-threaded, long-running, owns its transaction.
* `pool()` — a bounded in-process `ConnectionPool` for the **service**, one
  checkout (and one transaction boundary) per request.

The service must not share a connection across requests (DESIGN-REVIEW F2):
psycopg serializes operations on a connection, so nothing corrupts, but all
in-flight requests would share one transaction — `save_turn`'s commit would
commit other requests' pending work, one request's error would poison another's
statements, and every DB call across all requests would queue behind one socket
while agent turns run 10–40 s. The pool also fixes the idle-drop failure: after
Cloud Run idles, Cloud SQL drops the connection and the old single-connection
service returned 500 forever with no reconnect path.

This is not a reversal of the 2026-07-07 no-PgBouncer decision — that was about
a network sidecar; this is a library pool inside the worker.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_SOCKET = os.path.expanduser(
    "~/cloudsql/vertical-album-400917:us-east1:x1-sql-test"
)


def conn_kwargs(autocommit: bool = False) -> dict:
    """Connection args from env: ADVISOR_PGHOST (socket dir) / DB_USER / DB_PASS / DB_NAME."""
    return {
        "host": os.environ.get("ADVISOR_PGHOST", DEFAULT_SOCKET),
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ["DB_PASS"],
        "dbname": os.environ.get("DB_NAME", "x1-db-test"),
        "autocommit": autocommit,
        "row_factory": dict_row,
    }


def connect(autocommit: bool = False) -> psycopg.Connection:
    """One dedicated connection (CLI, ingest, eval harness)."""
    return psycopg.connect(**conn_kwargs(autocommit))


# --- service connection pool ---------------------------------------------
# max_size is the real concurrency bound on agent turns: a turn holds its
# connection for its whole 10–40 s. Cloud Run concurrency must be set to match
# it (Gate 3A) — until then the pool timeout is what stops requests piling up.
POOL_MIN = int(os.environ.get("ADVISOR_DB_POOL_MIN", "1"))
POOL_MAX = int(os.environ.get("ADVISOR_DB_POOL_MAX", "4"))
POOL_TIMEOUT_S = float(os.environ.get("ADVISOR_DB_POOL_TIMEOUT", "30"))

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """Process-wide bounded pool; created on first use."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            kwargs=conn_kwargs(),
            min_size=POOL_MIN,
            max_size=POOL_MAX,
            timeout=POOL_TIMEOUT_S,
            # hand out only connections that still work: Cloud SQL drops idle
            # ones, and a dead connection must be replaced, not returned
            check=ConnectionPool.check_connection,
            open=False,
        )
        # wait=False: the service starts even if the database is briefly
        # unreachable and reconnects on its own; /health reports the truth
        _pool.open(wait=False)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def apply_schema(conn: psycopg.Connection) -> None:
    conn.execute((PROJECT_ROOT / "x1_advisor" / "schema.sql").read_text())
    conn.commit()
