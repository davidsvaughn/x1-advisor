"""X1 Advisor service (Phase-5 skeleton).

Run locally (dev default, --reload picks up code/prompt changes on save —
without it a running service answers with the OLD system prompt after a
prompt edit, which cost a confused half hour on 2026-08-11):

    uv run uvicorn x1_advisor.service:app --port 8123 --workers 1 --reload

(8100 is taken by another app on the dev box.) Then open
http://localhost:8123/ — the built-in DEV CONSOLE: a self-contained browser
chat page (markdown rendering, clickable citations, per-turn cost/ids, and a
flag button feeding the same regression queue as the REPL's /flag). The
console is dev tooling on the product's own API — the real UI
(@assistant-ui/react + SSE) replaces it in the app; set
ADVISOR_DEV_CONSOLE=0 in any deployed config (Gate 3A checklist).

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

import os
from contextlib import asynccontextmanager
from typing import Any

import anyio
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from psycopg_pool import PoolTimeout
from pydantic import BaseModel

from x1_advisor.agent.advisor import run_turn, save_turn
from x1_advisor.agent.flags import flag_exchange
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


class FlagRequest(BaseModel):
    thread_id: int | None = None
    turn_id: int | None = None
    question: str | None = None
    note: str | None = None
    cost_usd: float | None = None


@app.post("/flag")
def flag(req: FlagRequest) -> dict:
    """Queue an exchange as a regression-suite candidate (same file the REPL's
    /flag writes; curation into golden v2 stays a human step)."""
    path = flag_exchange(thread_id=req.thread_id, turn_id=req.turn_id,
                         question=req.question, note=req.note,
                         cost_usd=req.cost_usd)
    return {"queued": str(path)}


@app.get("/", response_class=HTMLResponse)
def console() -> str:
    """Self-contained dev chat console. No external assets, no build step —
    everything the page needs is in this one response."""
    if os.environ.get("ADVISOR_DEV_CONSOLE", "1") == "0":
        raise HTTPException(404, "dev console disabled")
    return _CONSOLE_HTML


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


# --- dev console page ------------------------------------------------------
# Vanilla HTML/JS, deliberately dependency-free: the point is a five-second
# feedback loop on the product's own API, not a second frontend. Rendering is
# markdown-lite (headings, bold, code, lists, TABLES, [n] citation chips);
# citations render per kind — internal doc#block, platform query provenance,
# live web links. Long enumerations auto-collapse: first 8 items visible,
# the rest behind a native <details> expander — the ANSWER stays complete
# (census honesty is the model's contract), collapsing is presentation only.
# All model/corpus text is HTML-escaped before any markup pass.
_CONSOLE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>x1-advisor dev console</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root { color-scheme: light dark; }
 body { font: 15px/1.5 system-ui, sans-serif; max-width: 860px;
        margin: 0 auto; padding: 1rem 1rem 8rem; }
 h1 { font-size: 1.1rem; } h1 small { color: #888; font-weight: normal; }
 .ex { border-left: 3px solid #8884; padding: .2rem 0 .2rem .8rem;
       margin: 1rem 0; }
 .q { font-weight: 600; white-space: pre-wrap; }
 .a { margin-top: .4rem; }
 .a h4 { margin: .7rem 0 .2rem; } .a ul, .a ol { margin: .2rem 0 .2rem 1.4rem; }
 .a code { background: #8882; padding: 0 .25em; border-radius: 3px; }
 .a table { border-collapse: collapse; margin: .3rem 0; font-size: .92em; }
 .a th, .a td { border: 1px solid #8884; padding: .15rem .55rem;
                text-align: left; }
 .a details { margin: .1rem 0 .4rem; }
 .a summary { cursor: pointer; color: #79c; font-size: .85em;
              user-select: none; }
 .cites { margin-top: .5rem; font-size: .85rem; color: #777; }
 .cites div { margin: .1rem 0; }
 .meta { margin-top: .3rem; font-size: .78rem; color: #999; }
 .meta button { font-size: .78rem; margin-left: .6rem; }
 sup a { text-decoration: none; }
 #bar { position: fixed; bottom: 0; left: 0; right: 0; background: canvas;
        border-top: 1px solid #8884; padding: .7rem 1rem; }
 #bar .inner { max-width: 860px; margin: 0 auto; display: flex; gap: .5rem; }
 #q { flex: 1; font: inherit; padding: .45rem; min-height: 2.4rem;
      resize: vertical; }
 #status { font-size: .8rem; color: #999; margin-top: .3rem; }
 .err { color: #c33; white-space: pre-wrap; }
</style></head><body>
<h1>x1-advisor <small>dev console — thread <span id="tid">new</span> ·
ACL <select id="acl"><option value="admin">admin</option>
<option value="anon">anonymous</option></select></small></h1>
<div id="log"></div>
<div id="bar"><div class="inner">
 <textarea id="q" placeholder="Ask the advisor…  (Enter sends, Shift+Enter = newline)"></textarea>
 <button id="send">Send</button></div>
 <div id="status"></div></div>
<script>
"use strict";
let history = [], threadId = null, exN = 0;
const COLLAPSE_OVER = 12, COLLAPSE_HEAD = 8;
const $ = id => document.getElementById(id);
const esc = s => s.replace(/&/g,"&amp;").replace(/</g,"&lt;")
                  .replace(/>/g,"&gt;").replace(/"/g,"&quot;");

function inline(s, ex) {
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>")
       .replace(/\\*\\*([^*]+)\\*\\*/g, "<b>$1</b>")
       .replace(/(https?:\\/\\/[^\\s<)]+)/g,
                '<a href="$1" target="_blank" rel="noreferrer">$1</a>');
  return s.replace(/\\[(\\d+(?:\\s*,\\s*\\d+)*)\\]/g, (m, nums) =>
    "<sup>" + nums.split(/\\s*,\\s*/).map(n =>
      `<a href="#c${ex}_${n}">[${n}]</a>`).join("") + "</sup>");
}

function md(text, ex) {
  const out = []; let list = null, para = [], table = null;
  const flushP = () => { if (para.length)
    { out.push("<p>" + inline(para.join(" "), ex) + "</p>"); para = []; } };
  const flushL = () => { if (!list) return;
    const [tag, items] = list; list = null;
    if (items.length > COLLAPSE_OVER) {
      const rest = tag === "ol" ? `<ol start="${COLLAPSE_HEAD + 1}">`
                                : `<${tag}>`;
      out.push(`<${tag}>` + items.slice(0, COLLAPSE_HEAD).join("") +
        `</${tag}><details><summary>show ${items.length - COLLAPSE_HEAD} ` +
        `more…</summary>` + rest + items.slice(COLLAPSE_HEAD).join("") +
        `</${tag}></details>`);
    } else out.push(`<${tag}>` + items.join("") + `</${tag}>`); };
  const flushT = () => { if (!table) return;
    const { header, rows } = table; table = null;
    const tr = (cells, t) => "<tr>" + cells.map(c =>
      `<${t}>` + inline(c, ex) + `</${t}>`).join("") + "</tr>";
    const head = header ? "<thead>" + tr(header, "th") + "</thead>" : "";
    const body = rows.map(r => tr(r, "td"));
    if (rows.length > COLLAPSE_OVER) {
      out.push("<table>" + head + "<tbody>" +
        body.slice(0, COLLAPSE_HEAD).join("") + "</tbody></table>" +
        `<details><summary>show ${rows.length - COLLAPSE_HEAD} more ` +
        `rows…</summary><table>` + head + "<tbody>" +
        body.slice(COLLAPSE_HEAD).join("") + "</tbody></table></details>");
    } else out.push("<table>" + head + "<tbody>" + body.join("") +
                    "</tbody></table>"); };
  for (const raw of esc(text).split("\\n")) {
    const line = raw.trimEnd();
    let m;
    if (!line.trim()) { flushP(); flushL(); flushT(); continue; }
    if (/^\\|.*\\|\\s*$/.test(line)) {
      flushP(); flushL();
      const cells = line.replace(/^\\s*\\||\\|\\s*$/g, "")
                        .split("|").map(s => s.trim());
      if (table && !table.header && table.rows.length === 1 &&
          cells.every(c => /^:?-+:?$/.test(c)))
        table.header = table.rows.pop();
      else if (!table) table = { header: null, rows: [cells] };
      else table.rows.push(cells);
      continue;
    }
    if ((m = line.match(/^#{1,4}\\s+(.*)/)))
      { flushP(); flushL(); flushT();
        out.push("<h4>" + inline(m[1], ex) + "</h4>"); }
    else if ((m = line.match(/^\\s*[-*]\\s+(.*)/))) {
      flushP(); flushT(); if (list && list[0] !== "ul") flushL();
      list = list || ["ul", []]; list[1].push("<li>" + inline(m[1], ex) + "</li>");
    } else if ((m = line.match(/^\\s*\\d+[.)]\\s+(.*)/))) {
      flushP(); flushT(); if (list && list[0] !== "ol") flushL();
      list = list || ["ol", []]; list[1].push("<li>" + inline(m[1], ex) + "</li>");
    } else { flushT(); para.push(line); }
  }
  flushP(); flushL(); flushT();
  return out.join("");
}

function citeLine(c, ex) {
  const t = c.title ? esc(c.title) + " — " : "";
  let loc;
  if (c.type === "internal")
    loc = `doc ${c.document_id}#${c.block_index}` +
          (c.page_number != null ? ` p${c.page_number}` : "");
  else if (c.type === "platform_data")
    loc = `platform query <code>${esc(c.query || "")}</code>` +
          ` (${c.row_count} rows, as of ${esc(String(c.as_of || "?"))})`;
  else
    loc = c.url ? `<a href="${esc(c.url)}" target="_blank" rel="noreferrer">` +
                  `${esc(c.url)}</a>` : "(no locator)";
  return `<div id="c${ex}_${c.n}">[${c.n}] ${t}${loc}</div>`;
}

async function send() {
  const q = $("q").value.trim();
  if (!q) return;
  $("q").value = ""; $("send").disabled = true;
  const ex = ++exN;
  const box = document.createElement("div");
  box.className = "ex";
  box.innerHTML = `<div class="q">${esc(q)}</div><div class="a">…</div>`;
  $("log").appendChild(box); box.scrollIntoView(false);
  $("status").textContent = "thinking…";
  const t0 = Date.now();
  try {
    const rsp = await fetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json",
                 ...($("acl").value === "admin" ? { "X-User-Id": "admin" } : {}) },
      body: JSON.stringify({ question: q, thread_id: threadId, history }),
    });
    if (!rsp.ok) throw new Error(rsp.status + " " + await rsp.text());
    const r = await rsp.json();
    threadId = r.thread_id; $("tid").textContent = threadId;
    history.push({ role: "user", content: q },
                 { role: "assistant", content: r.answer });
    box.querySelector(".a").innerHTML = md(r.answer, ex);
    if (r.citations && r.citations.length)
      box.insertAdjacentHTML("beforeend", '<div class="cites">' +
        r.citations.map(c => citeLine(c, ex)).join("") + "</div>");
    box.insertAdjacentHTML("beforeend",
      `<div class="meta">turn ${r.turn_id} · $${(r.cost_usd || 0).toFixed(4)}` +
      ` · ${((Date.now() - t0) / 1000).toFixed(1)}s` +
      ` <button onclick="flagTurn(${r.turn_id}, this)">flag for QA</button></div>`);
    window._turns = window._turns || {};
    window._turns[r.turn_id] = { question: q, cost_usd: r.cost_usd };
    box.scrollIntoView(false);
  } catch (e) {
    box.querySelector(".a").innerHTML = `<div class="err">${esc(String(e))}</div>`;
  } finally {
    $("send").disabled = false; $("status").textContent = ""; $("q").focus();
  }
}

async function flagTurn(turnId, btn) {
  const note = prompt("Optional note for the QA queue:", "") ;
  if (note === null) return;
  const info = (window._turns || {})[turnId] || {};
  const rsp = await fetch("/flag", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId, turn_id: turnId,
                           question: info.question, note: note || null,
                           cost_usd: info.cost_usd }) });
  btn.textContent = rsp.ok ? "flagged ✓" : "flag failed";
  btn.disabled = rsp.ok;
}

$("send").onclick = send;
$("q").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
$("q").focus();
</script></body></html>"""
