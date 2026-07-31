"""X1 Advisor service (Phase-5 skeleton).

Run locally:  uv run uvicorn x1_advisor.service:app --port 8100 --workers 1

Deploy notes (DECISIONS 2026-07-07, amended Step 0.3): a bounded in-process
connection pool per worker — one checkout and one transaction boundary per
request (db.pool(); the F2 rationale lives there). Still no PgBouncer at this
scale. Cloud Run + Cloud SQL Python connector at cutover, with Cloud Run
concurrency set to match ADVISOR_DB_POOL_MAX (Gate 3A).

Auth: `X-User-Id` header is a DEV STUB — production replaces it with the signed
user token from the Laravel session (PLAN Phase 5), resolved to an ACL
class-dict; until then non-admin requests get the least-privileged ACL. SSE
token streaming lands with the UI phase; /ask returns the full validated result
JSON (answer, citations, per-step usage, cost, trace_id).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import anyio
from fastapi import FastAPI, Header, HTTPException
from psycopg_pool import PoolTimeout
from pydantic import BaseModel

from x1_advisor.agent.advisor import run_turn, save_turn
from x1_advisor.db import POOL_MAX, close_pool, pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = pool()
    try:
        yield
    finally:
        close_pool()


app = FastAPI(title="x1-advisor", lifespan=lifespan)


class AskRequest(BaseModel):
    question: str
    thread_id: int | None = None
    history: list[dict[str, str]] | None = None


def _acl_for(user_id: str | None) -> Any:
    """DEV STUB: admin for the magic dev header, least-privilege otherwise.
    Production: verify the signed Laravel token, resolve purchases/ownership."""
    if user_id == "admin":
        return "admin"
    try:
        return {"user_id": int(user_id)} if user_id else {"user_id": 0}
    except ValueError:
        raise HTTPException(400, "invalid X-User-Id") from None


@app.get("/health")
def health() -> dict:
    with app.state.pool.connection() as conn:
        row = conn.execute("SELECT count(*) AS docs FROM advisor.documents "
                           "WHERE superseded_by IS NULL").fetchone()
    return {"ok": True, "live_documents": row["docs"]}


@app.post("/ask")
async def ask(req: AskRequest, x_user_id: str | None = Header(default=None)) -> dict:
    acl = _acl_for(x_user_id)

    def _run() -> dict:
        # one checkout per request: the turn and its persisted rows share a
        # transaction that belongs to this request and nobody else
        with app.state.pool.connection() as conn:
            result = run_turn(conn, req.question, acl=acl, history=req.history)
            result["thread_id"] = save_turn(
                conn, result,
                user_id=0 if acl == "admin" else acl["user_id"],
                thread_id=req.thread_id)
            # the bundle is persisted and exported, not returned: it is a
            # separate access surface (bundle.py P5) and far larger than an
            # answer. A bundle-read endpoint is Gate 2 work.
            result.pop("bundle", None)
            result.pop("bundle_path", None)
            return result

    # run_turn is synchronous (psycopg + tool loop): keep the event loop free
    try:
        return await anyio.to_thread.run_sync(_run)
    except PoolTimeout:
        # a turn holds its connection for its whole duration, so the pool is
        # the concurrency bound. Say "too busy, retry" rather than 500.
        raise HTTPException(
            503, f"advisor is at capacity ({POOL_MAX} concurrent turns); retry shortly",
            headers={"Retry-After": "10"}) from None
