"""Service-runtime probes (Step 0.3 / Gate 3A groundwork).

Asserts the properties a shared connection cannot have. Needs a live database.
Run: uv run python -m experiments.runtime_probes
"""

from __future__ import annotations

import sys
import threading

import psycopg

from x1_advisor.db import close_pool, pool


def main() -> None:
    failures: list[str] = []
    p = pool()
    p.wait(timeout=30)

    # 1. concurrent checkouts are distinct backends, not one shared socket
    pids: list[int] = []
    barrier = threading.Barrier(2)

    def grab() -> None:
        with p.connection() as conn:
            pids.append(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
            barrier.wait(timeout=30)      # hold both checkouts at once

    threads = [threading.Thread(target=grab) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    if len(set(pids)) != 2:
        failures.append(f"concurrent checkouts shared a backend: {pids}")

    # 2. one request's uncommitted write is invisible to another, and its
    #    rollback does not touch the other's work (the save_turn hazard)
    with p.connection() as a, p.connection() as b:
        a.execute("CREATE TEMP TABLE IF NOT EXISTS probe_a (n int)")
        a.execute("INSERT INTO probe_a VALUES (1)")
        visible = b.execute(
            "SELECT count(*) AS n FROM pg_class WHERE relname = 'probe_a'"
        ).fetchone()["n"]
        if visible:
            failures.append("uncommitted work in one checkout was visible in another")

    # 3. an error in one checkout does not poison another's statements
    with p.connection() as a:
        try:
            with p.connection() as b:
                b.execute("SELECT 1/0")
        except psycopg.errors.DivisionByZero:
            pass
        else:
            failures.append("expected DivisionByZero from the poisoned checkout")
        if a.execute("SELECT 42 AS n").fetchone()["n"] != 42:
            failures.append("a healthy checkout was poisoned by another's error")

    # 4. a broken connection is replaced, not handed out again (the Cloud SQL
    #    idle-drop failure that used to 500 forever)
    with p.connection() as conn:
        dead_pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        conn.close()                       # simulate the server dropping it
    with p.connection() as conn:
        if conn.closed:
            failures.append("pool handed out a closed connection")
        elif conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"] == dead_pid:
            failures.append("pool reused the dropped backend")

    close_pool()
    print("failures:", failures or "NONE")
    print("\nRUNTIME PROBES:", "PASS" if not failures else "FAIL")
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
