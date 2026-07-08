"""Database access for the advisor service.

App tables are read-only, always; writes go only to the `advisor` schema.
Connection is via the cloud-sql-proxy Unix socket (dev) or the Cloud SQL
connector socket path (deploy) — both are just a socket-dir `host` to psycopg.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_SOCKET = os.path.expanduser(
    "~/cloudsql/vertical-album-400917:us-east1:x1-sql-test"
)


def connect(autocommit: bool = False) -> psycopg.Connection:
    """Connection from env: ADVISOR_PGHOST (socket dir) / DB_USER / DB_PASS / DB_NAME."""
    return psycopg.connect(
        host=os.environ.get("ADVISOR_PGHOST", DEFAULT_SOCKET),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ["DB_PASS"],
        dbname=os.environ.get("DB_NAME", "x1-db-test"),
        autocommit=autocommit,
        row_factory=dict_row,
    )


def apply_schema(conn: psycopg.Connection) -> None:
    conn.execute((PROJECT_ROOT / "x1_advisor" / "schema.sql").read_text())
    conn.commit()
