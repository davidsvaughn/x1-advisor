"""Tier-1 research agent assembly (PLAN Phase 4).

Context discipline (§9, David's priority):
  - SYSTEM_PROMPT is byte-stable — no timestamps, no user-specifics — so the prompt
    prefix caches across steps and turns (verify via cached tokens in the usage table).
  - Volatile context (the question) arrives as the last user message only.
  - Tool results are compact by construction (tools.py).
  - Every generation step's usage is logged to cost.py and returned as a per-step
    table: watch input-token growth across steps — superlinear growth means a fat
    tool result or broken cache prefix.
  - Per-turn soft cost cap ($0.50 proposal, PLAN §5.4): exceeded → flagged in result.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from haystack.components.agents import Agent
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.dataclasses import ChatMessage

from x1_advisor.agent.evidence import EvidenceRegistry, validate_citations
from x1_advisor.agent.tools import build_tools
from x1_advisor.cost import JsonlSink, Tracker, Usage

AGENT_MODEL = "gpt-5.1"
MAX_STEPS = 8
PER_TURN_SOFT_CAP_USD = 0.50

# BYTE-STABLE prefix — edit deliberately; any change invalidates the prompt cache.
SYSTEM_PROMPT = """\
You are X1 Advisor, a research agent for the X1 startup/investor platform. You answer
research questions about startups, investors, people, funds, and markets using tools:

- search_corpus: the X1 corpus (profiles, evaluation reports/sections, pitch decks,
  website content). Your primary evidence source — search it first.
- get_source: full text of one evidence block when a snippet was truncated and the
  detail matters.
- structured_query: exact counts/lists/rankings from the platform database. Use it for
  "how many", "list all", "top by score" questions — never estimate these from search.
- web_research: live web evidence for current events, market data, competitors.

Evidence and citation rules:
1. Ground every factual claim in evidence you retrieved this turn; cite it with the
   evidence ref in square brackets, e.g. [ref2] or [ref1, ref4], immediately after the
   claim.
2. If you cannot find supporting evidence, say so plainly — OMIT a citation rather
   than guessing one. Never invent refs.
3. Prefer multiple sources for load-bearing claims; note disagreements between sources.
4. Internal evidence (profiles, evaluations, decks) is the authority on platform data;
   the web is for current/external context.

Style: lead with the answer, keep it tight, use the reader's vocabulary — they have
not seen your tool calls. Do not pad; do not repeat the evidence verbatim when a
summary sentence and a citation will do. Keep answers under roughly 400 words unless
the user asks for a full report. Web evidence from web_research lists sources as
(ref, url, title) — cite those refs exactly like corpus refs.

You have a hard budget of 8 tool steps per turn — plan multi-part questions before
acting and spend steps where they buy the most. If a search comes back empty, do not
re-search with variations more than once: conclude the material is not in the corpus,
say so in the answer, and move on to the next part.\
"""


HISTORY_VERBATIM_TURNS = 5   # §9: last-5 user/assistant exchanges verbatim
CONDENSE_MODEL = "gpt-5-mini"


def _history_messages(history: list[dict] | None,
                      tracker: Tracker) -> list[ChatMessage]:
    """§9 history discipline: older turns condensed (cheap model), recent verbatim."""
    if not history:
        return []
    verbatim = history[-2 * HISTORY_VERBATIM_TURNS:]
    older = history[: len(history) - len(verbatim)]
    out: list[ChatMessage] = []
    if older:
        transcript = "\n".join(f"{t['role']}: {t['content'][:800]}" for t in older)
        reply = OpenAIChatGenerator(model=CONDENSE_MODEL).run([ChatMessage.from_user(
            "Condense this earlier conversation into <=150 words of context a "
            "research agent needs to continue it (entities discussed, conclusions "
            "reached, open questions):\n\n" + transcript)])["replies"][0]
        tracker.log(provider="openai", model=CONDENSE_MODEL, stage="agent.condense",
                    usage=Usage.from_haystack_meta("openai", reply.meta))
        out.append(ChatMessage.from_user(
            f"[Summary of earlier conversation]\n{reply.text}"))
    for t in verbatim:
        maker = ChatMessage.from_user if t["role"] == "user" else ChatMessage.from_assistant
        out.append(maker(t["content"]))
    return out


def run_turn(conn, question: str, *, acl: Any = "admin",
             history: list[dict] | None = None,
             tracker: Tracker | None = None) -> dict[str, Any]:
    """One user question → grounded, citation-validated answer + usage table.

    `history` = prior turns [{"role": "user"|"assistant", "content": str}, ...];
    the last 5 exchanges ride verbatim, anything older is condensed once per call
    (cheap model) — tool results from prior turns are never replayed."""
    # cost ledger is default-ON (no-silent-spend); ADVISOR_COST_LEDGER overrides path
    ledger = os.environ.get("ADVISOR_COST_LEDGER", "cost_ledger.jsonl")
    tracker = tracker or Tracker(run_id=f"turn:{int(time.time())}",
                                 sink=JsonlSink(ledger),
                                 per_run_soft_cap_usd=PER_TURN_SOFT_CAP_USD)
    registry = EvidenceRegistry()
    agent = Agent(
        chat_generator=OpenAIChatGenerator(model=AGENT_MODEL),
        tools=build_tools(conn, acl=acl, registry=registry, tracker=tracker),
        system_prompt=SYSTEM_PROMPT,
        exit_conditions=["text"],
        max_agent_steps=MAX_STEPS,
    )
    agent.warm_up()

    t0 = time.monotonic()
    messages = agent.run(
        messages=[*_history_messages(history, tracker),
                  ChatMessage.from_user(question)])["messages"]
    latency_ms = int((time.monotonic() - t0) * 1000)

    steps = []
    tool_calls: list[dict] = []
    for m in messages:
        if m.is_from("assistant") and m.meta.get("usage"):
            u = Usage.from_haystack_meta("openai", m.meta)
            rec = tracker.log(provider="openai", model=AGENT_MODEL,
                              stage="agent.step", usage=u)
            steps.append({
                "step": len(steps) + 1,
                "input_tokens": u.input_tokens,
                "cached_tokens": u.cache_read_tokens,
                "output_tokens": u.output_tokens,
                "cost_usd": round(rec.cost_usd, 6),
                "tool_calls": [tc.tool_name for tc in (m.tool_calls or [])],
            })
        if m.tool_calls:
            for tc in m.tool_calls:
                tool_calls.append({"tool": tc.tool_name, "arguments": tc.arguments})

    raw_answer = messages[-1].text or ""
    if not raw_answer.strip():
        # step cap hit mid-research: synthesize from gathered evidence rather than
        # returning nothing for the money spent (honest degradation, §9)
        wrap = ChatMessage.from_user(
            "You have reached the tool-step limit. Write your final answer NOW from "
            "the evidence already gathered: cite refs you have, state plainly which "
            "parts you could not verify or complete, and do not call any more tools.")
        reply = OpenAIChatGenerator(model=AGENT_MODEL).run(
            [ChatMessage.from_system(SYSTEM_PROMPT), *messages, wrap])["replies"][0]
        u = Usage.from_haystack_meta("openai", reply.meta)
        rec = tracker.log(provider="openai", model=AGENT_MODEL,
                          stage="agent.wrapup", usage=u)
        steps.append({"step": len(steps) + 1, "input_tokens": u.input_tokens,
                      "cached_tokens": u.cache_read_tokens,
                      "output_tokens": u.output_tokens,
                      "cost_usd": round(rec.cost_usd, 6), "tool_calls": ["(wrapup)"]})
        raw_answer = reply.text or ""
    validated = validate_citations(raw_answer, registry)

    result = {
        "question": question,
        "answer": validated["answer"],
        "citations": validated["citations"],
        "citation_stats": {"emitted": validated["emitted"],
                           "resolved": validated["resolved"],
                           "dropped": validated["dropped"],
                           "evidence_registered": len(registry)},
        "steps": steps,
        "tool_calls": tool_calls,
        "latency_ms": latency_ms,
        "cost_usd": round(tracker.run_total, 6),
        "over_soft_cap": tracker.over_per_run_soft_cap(),
    }
    from x1_advisor.telemetry import emit_turn_trace

    result["trace_id"] = emit_turn_trace(result, model=AGENT_MODEL)
    return result


def save_turn(conn, result: dict, *, user_id: int = 0,
              thread_id: int | None = None) -> int:
    """Persist to advisor.threads/turns (research_record feeds the eval set)."""
    if thread_id is None:
        thread_id = conn.execute(
            "INSERT INTO advisor.threads (user_id, title) VALUES (%s, %s) RETURNING id",
            (user_id, result["question"][:120]),
        ).fetchone()["id"]
    record = {k: result[k] for k in
              ("citations", "citation_stats", "steps", "tool_calls", "latency_ms")}
    conn.execute(
        """INSERT INTO advisor.turns (thread_id, role, content) VALUES (%s,'user',%s)""",
        (thread_id, result["question"]),
    )
    conn.execute(
        """INSERT INTO advisor.turns (thread_id, role, content, research_record, cost_usd)
           VALUES (%s,'assistant',%s,%s,%s)""",
        (thread_id, result["answer"], json.dumps(record, default=str),
         result["cost_usd"]),
    )
    conn.commit()
    return thread_id
