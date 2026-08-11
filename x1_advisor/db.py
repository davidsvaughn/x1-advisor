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
import shutil
import subprocess
import sys
import time
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


# --- dev proxy self-healing ------------------------------------------------
# The recurring dev failure (2026-07-30 / 08-06 / 08-11): ADC lapses ~weekly,
# and a long-running cloud-sql-proxy keeps its cached credentials — every
# connect then dies with "server closed the connection unexpectedly" until the
# proxy is RESTARTED (re-authing alone never heals it). connect() now does the
# restart itself: one revival attempt, then one retry. Deploy paths are
# untouched — no proxy binary on the box means no revival, original error
# re-raised. Disable with ADVISOR_PROXY_AUTOSTART=0.

_PROXY_ERROR_SIGNS = ("server closed the connection unexpectedly",
                      "Connection refused", "No such file or directory")


def _proxy_binary() -> str | None:
    return (os.environ.get("ADVISOR_SQL_PROXY_BIN")
            or shutil.which("cloud-sql-proxy")
            or (lambda p: p if os.path.exists(p) else None)(
                os.path.expanduser("~/Downloads/BeeKeeper/cloud-sql-proxy")))


def _proxy_recoverable(host: str, exc: Exception) -> bool:
    """Only a proxy-socket host, only proxy-death signatures, only in dev."""
    if os.environ.get("ADVISOR_PROXY_AUTOSTART", "1") == "0":
        return False
    # instance-connection-name socket dirs look like project:region:instance
    if ":" not in os.path.basename(host):
        return False
    return (any(sign in str(exc) for sign in _PROXY_ERROR_SIGNS)
            and _proxy_binary() is not None)


def _check_adc() -> None:
    """A proxy restart is useless on lapsed ADC — fail with the actual fix."""
    try:
        probe = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return  # can't verify here; let the restarted proxy be the judge
    if probe.returncode != 0:
        raise RuntimeError(
            "Application Default Credentials are expired (this recurs about "
            "weekly). Run:\n\n    gcloud auth application-default login\n\n"
            "then retry — the proxy restart is automatic from there. "
            f"[{(probe.stderr or '').strip()[:200]}]")


def _revive_proxy(host: str) -> None:
    binary = _proxy_binary()
    instance = os.path.basename(host)          # project:region:instance
    socket_root = os.path.dirname(host)        # e.g. ~/cloudsql
    _check_adc()
    print(f"[db] connection through {instance} failed — restarting "
          "cloud-sql-proxy with fresh credentials", file=sys.stderr)
    subprocess.run(["pkill", "-f", f"cloud-sql-proxy.*{instance}"],
                   capture_output=True)
    time.sleep(1.0)
    log = open(os.path.join(socket_root, "proxy.log"), "a")
    subprocess.Popen([binary, "--unix-socket", socket_root, instance],
                     stdout=log, stderr=subprocess.STDOUT,
                     start_new_session=True)   # outlives this process
    socket_file = os.path.join(host, ".s.PGSQL.5432")
    for _ in range(20):                        # up to ~10 s for socket ready
        if os.path.exists(socket_file):
            print("[db] proxy is up", file=sys.stderr)
            return
        time.sleep(0.5)
    print("[db] proxy did not come up in 10 s — see "
          f"{socket_root}/proxy.log", file=sys.stderr)


def connect(autocommit: bool = False) -> psycopg.Connection:
    """One dedicated connection (CLI, ingest, eval harness).

    Self-heals the dev cloud-sql-proxy: a proxy-shaped connection failure
    triggers one ADC check + proxy restart + retry. Anything else — and any
    failure that survives the retry — raises as before."""
    kwargs = conn_kwargs(autocommit)
    try:
        return psycopg.connect(**kwargs)
    except psycopg.OperationalError as exc:
        if not _proxy_recoverable(kwargs["host"], exc):
            raise
        _revive_proxy(kwargs["host"])
        return psycopg.connect(**kwargs)


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
