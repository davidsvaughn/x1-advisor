"""X1 Advisor service (Phase-5 skeleton).

Run locally:  uv run uvicorn x1_advisor.service:app --port 8100 --workers 1

Deploy notes (DECISIONS 2026-07-07): one DB connection per worker process
(per-worker store instances, no PgBouncer at this scale); Cloud Run + Cloud SQL
Python connector at cutover. Auth: `X-User-Id` header is a DEV STUB — production
replaces it with the signed user token from the Laravel session (PLAN Phase 5),
resolved to an ACL class-dict; until then non-admin requests get the least-
privileged ACL. SSE token streaming lands with the UI phase; /ask returns the
full validated result JSON (answer, citations, per-step usage, cost, trace_id).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import anyio
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from x1_advisor.agent.advisor import run_turn, save_turn
from x1_advisor.db import connect


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.conn = connect()          # one connection per worker process
    yield
    app.state.conn.close()


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
    row = app.state.conn.execute("SELECT count(*) AS docs FROM advisor.documents "
                                 "WHERE superseded_by IS NULL").fetchone()
    return {"ok": True, "live_documents": row["docs"]}


@app.post("/ask")
async def ask(req: AskRequest, x_user_id: str | None = Header(default=None)) -> dict:
    acl = _acl_for(x_user_id)
    conn = app.state.conn

    def _run() -> dict:
        result = run_turn(conn, req.question, acl=acl, history=req.history)
        result["thread_id"] = save_turn(
            conn, result,
            user_id=0 if acl == "admin" else acl["user_id"],
            thread_id=req.thread_id)
        return result

    # run_turn is synchronous (psycopg + tool loop): keep the event loop free
    return await anyio.to_thread.run_sync(_run)
