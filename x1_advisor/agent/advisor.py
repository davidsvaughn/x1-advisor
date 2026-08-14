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
from haystack.components.generators.chat import (OpenAIChatGenerator,
                                                 OpenAIResponsesChatGenerator)
from haystack.dataclasses import ChatMessage

from x1_advisor.agent.bundle import build_bundle, export_bundle
from x1_advisor.agent.evidence import EvidenceRegistry, validate_citations
from x1_advisor.agent.tools import build_tools
from x1_advisor.cost import JsonlSink, Tracker, Usage
from x1_advisor.index import active_config

# Overridable so a model change can be A/B'd against the committed baseline
# (`ADVISOR_AGENT_MODEL=... experiments.run --agent`) instead of edited in and
# hoped about. The resolved value is stamped into every trace and bundle, so a
# run always says which model produced it.
# gpt-5.6-terra since 2026-07-31 (E8). Both arms ran on the Responses transport
# so the model was the only variable, and both were re-graded by gpt-5.6-luna —
# a judge that wrote neither arm, because terra grading its own answers is
# exactly the confound this comparison could not afford.
#
# Under that neutral judge: faithfulness 0.7226 -> 0.7258 (**+0.003, a dead
# tie**), citation coverage 0.8271 -> 0.9326 (**+0.106**), 0 broken / 1 fixed,
# labels net -7, compare PASS. Coverage is the one quality claim that survives:
# it clears the ~0.04 judge-only swing measured for that metric by ~2.5x.
# Faithfulness does not move, and is not claimed.
#
# Self-preference was real but small: judged by terra, terra's faithfulness
# edge read +0.014; judged neutrally it is +0.003. The inflation is inside
# noise, and it points the way the theory predicted.
#
# Also real, and judge-independent: mean latency 20.3s -> 8.7s and cost/turn
# $0.0196 -> $0.0163 — cheaper despite higher per-token prices, because terra
# reaches an answer in fewer, shorter steps.
AGENT_MODEL = os.environ.get("ADVISOR_AGENT_MODEL", "gpt-5.6-terra")
# Reasoning effort is a behavioural knob, so it rides the turn fingerprint
# rather than sitting as an unrecorded default.
AGENT_REASONING = os.environ.get("ADVISOR_AGENT_REASONING", "medium")


def agent_generator(model: str = AGENT_MODEL) -> OpenAIResponsesChatGenerator:
    """The agent's LLM transport: OpenAI's **Responses** API, for every model.

    Not a preference — a constraint. From gpt-5.6 on, function tools and
    reasoning are mutually exclusive on Chat Completions: the API rejects the
    pair outright and offers `reasoning_effort: "none"` as the only way to keep
    tools there. Grading a model with its reasoning switched off would have
    measured a crippled version of it and called that the model.

    Responses serves tools+reasoning for every model we might run (verified
    2026-07-31 across gpt-5.1 and the 5.6 tiers), so this stays one code path
    instead of a per-model fork that would confound transport with model in
    every future comparison.
    """
    return OpenAIResponsesChatGenerator(
        model=model,
        generation_kwargs={"reasoning": {"effort": AGENT_REASONING}},
    )


MAX_STEPS = 8
PER_TURN_SOFT_CAP_USD = 0.50

# BYTE-STABLE prefix — edit deliberately; any change invalidates the prompt cache.
SYSTEM_PROMPT = """\
You are X1 Advisor, a research agent for the X1 startup/investor platform. You answer
research questions about startups, investors, people, funds, and markets using tools:

- search_corpus: the X1 corpus (profiles, evaluation reports/sections, pitch decks,
  website content). Your primary evidence source — search it first. Returns only
  the top-ranked matches for a query — a sample, never an exhaustive scan.
- scan_text: exhaustive phrase/keyword scan of EVERY indexed document in a bounded
  scope, with a per-entity verdict (matched / no_match / not_indexed), complete
  coverage counts, and citable excerpts. The census tool: use it for "which/all/
  every/how many … mention X" questions instead of repeated searches.
- get_source: full text of one evidence block when a snippet was truncated and the
  detail matters.
- structured_query: exact counts/lists/rankings from the platform database. Use it for
  "how many", "list all", "top by score" questions — never estimate these from search.
- analyze_scope: semantic census — a cheap model reads EVERY document in a bounded
  scope in full and returns per-document findings with citable refs plus a synthesis.
  Slower and costs real money: reserve it for meaning-level questions across a scope.
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

Coverage and honesty rules:
5. search_corpus is a sampler, not a census — scan_text is the census. For
   "which/all/every X…" questions, prefer scan_text and report its coverage counts.
   scan_text censuses exact WORDS; analyze_scope censuses MEANING — when the
   question asks which documents discuss, flag, or imply something in any wording
   (weaknesses, risks, themes), phrase-guessing undercounts: use analyze_scope
   and report its coverage counts instead.
   Search results are what the search surfaced, never a complete list; only
   structured_query and scan_text results may be stated as exact or complete, and
   a scan is complete only for the scope its counts describe. A scan_text no_match
   is lexical — the phrases did not appear in that entity's indexed text — never
   proof that the topic or risk itself is absent. A census answer reports the
   whole census: every matched entity appears by name. Group or annotate matches
   by strength if that helps the reader, but never silently drop a match as
   incidental — if you judge some hits irrelevant, name them in their own group
   and say why, so the reader keeps the complete result.
6. Finding nothing is not evidence of absence. If searches come up empty, say the
   searches found nothing and name what you searched — do not conclude the thing
   does not exist, and do not substitute adjacent material or an estimate for the
   missing fact. For population statistics (averages, counts, extremes): run
   structured_query and report what the registry computes, named for exactly what
   it measures. If the requested statistic is not available as structured data,
   say so plainly — a figure synthesized from retrieved passages is a sample
   artifact, not a statistic.
7. Assert a match only when the evidence supports it. A snippet that mentions a
   term in passing or in an unrelated context is not a match — if unsure, present
   it as uncertain rather than counting it. When reporting scan results, credit
   each entity with the phrase that actually fired: a hit on a broader or adjacent
   variant is reported as that variant, never upgraded to the exact concept asked.
   If the asked phrase itself matched nothing, say so before offering variant
   matches.
8. Check the question's premise against evidence before building on it. If evidence
   contradicts what the question assumes, lead with the correction rather than
   explaining something that didn't happen. If a referent is ambiguous, say so and
   cover the plausible readings or ask.
9. If asked to take an action you cannot perform (send, schedule, modify data),
   decline plainly and offer what you can do: research and evidence.
10. Platform evaluation reports carry their own outbound source links — the
   report's research references. Surface them when relevant, always attributed to
   the report ("the evaluation cites …", with the link), never as your own
   citations: you have not verified them, so they never appear in square brackets.
11. Questions about how the X1 platform itself works — what evaluations are and
   cover, how scores and investor matches are computed, what data the platform
   holds — are answered from the platform_reference tool, not corpus search. Its
   content is your background knowledge, not evidence: state it freely without
   citation, and never attach a ref to it or present it as a quoted document.
12. Evaluations are dated snapshots and companies accumulate several (repeat
   evaluations of one deck; evaluations of older decks). Evaluation chunks carry
   eval_recency: 'current' is each company's standing assessment — the latest
   evaluation of its newest evaluated deck. For questions about companies'
   CURRENT state, weaknesses, or standing scores, filter to eval_recency
   'current' and disclose the narrowing ("counting each company's current
   evaluation; N older evaluations not counted"). For history, trend, or
   consistency questions, scan all vintages and say so. Never mix vintages in
   one count without labeling them.

Style: lead with the answer, keep it tight, use the reader's vocabulary — they have
not seen your tool calls. Do not pad; do not repeat the evidence verbatim when a
summary sentence and a citation will do. Keep answers under roughly 400 words unless
the user asks for a full report. Enumerations longer than about six items are
rendered as structure, never packed into a semicolon-run sentence: a markdown list,
or a markdown table when the items share attributes worth comparing (name, stage,
score). The word-count guideline yields to enumerations — structure, not
compression, is how a complete census stays readable. A list of one is prose,
never a numbered list. When several entities are analyzed along the same
dimensions (per-entity themes, risks, verdicts), prefer a single table — one row
per entity, one column per dimension — over repeated parallel sections. Never
pack more than about six items into one delimiter-separated run (semicolons,
commas); longer runs become a bulleted list, grouped under headings when the
data provides a grouping. For frequency distributions with a long tail, a table
fits only the head where counts differ; render the tail as grouped bullet
lists, never as packed table cells. Web evidence from
web_research lists sources as (ref, url, title) — cite those refs exactly like
corpus refs.

You have a hard budget of 8 tool steps per turn — plan multi-part questions before
acting and spend steps where they buy the most. If a search comes back empty, do not
re-search with variations more than once: state that your searches did not surface
it (not that it doesn't exist), and move on to the next part.\
"""


HISTORY_VERBATIM_TURNS = 5   # §9: last-5 user/assistant exchanges verbatim
CONDENSE_MODEL = os.environ.get("ADVISOR_CONDENSE_MODEL", "gpt-5.6-luna")


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
             tracker: Tracker | None = None,
             streaming_callback: Any = None) -> dict[str, Any]:
    """One user question → grounded, citation-validated answer + usage table.

    `history` = prior turns [{"role": "user"|"assistant", "content": str}, ...];
    the last 5 exchanges ride verbatim, anything older is condensed once per call
    (cheap model) — tool results from prior turns are never replayed.

    `streaming_callback` (haystack StreamingCallbackT) receives live chunks —
    text deltas + tool-call deltas — for the service's streaming endpoint. The
    streamed text is the RAW answer; the returned/persisted answer remains the
    citation-validated one (a streaming client re-renders on the final result)."""
    # cost ledger is default-ON (no-silent-spend); ADVISOR_COST_LEDGER overrides path
    ledger = os.environ.get("ADVISOR_COST_LEDGER", "cost_ledger.jsonl")
    tracker = tracker or Tracker(run_id=f"turn:{int(time.time())}",
                                 sink=JsonlSink(ledger),
                                 per_run_soft_cap_usd=PER_TURN_SOFT_CAP_USD)
    registry = EvidenceRegistry()
    # side channel for retrieval explains: goes to the turn bundle, never into
    # the model's context (QA-LOOP §4.2)
    retrieval_explain: list[dict] = []
    tools = build_tools(conn, acl=acl, registry=registry, tracker=tracker,
                        explain_out=retrieval_explain)
    agent = Agent(
        chat_generator=agent_generator(),
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        exit_conditions=["text"],
        max_agent_steps=MAX_STEPS,
    )
    agent.warm_up()

    t0 = time.monotonic()
    messages = agent.run(
        messages=[*_history_messages(history, tracker),
                  ChatMessage.from_user(question)],
        streaming_callback=streaming_callback)["messages"]
    latency_ms = int((time.monotonic() - t0) * 1000)

    steps = []
    tool_calls: list[dict] = []
    # what the provider actually served, which is not what we asked for: a
    # snapshot bump behind the "gpt-5.1" alias changes behavior invisibly
    model_resolved: str | None = None
    for m in messages:
        if m.is_from("assistant") and m.meta.get("model"):
            model_resolved = m.meta["model"]
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
    verdict = "answered"
    if not raw_answer.strip():
        verdict = "wrapped_up"
        # step cap hit mid-research: synthesize from gathered evidence rather than
        # returning nothing for the money spent (honest degradation, §9)
        wrap = ChatMessage.from_user(
            "You have reached the tool-step limit. Write your final answer NOW from "
            "the evidence already gathered: cite refs you have, state plainly which "
            "parts you could not verify or complete, and do not call any more tools.")
        reply = agent_generator().run(
            [ChatMessage.from_system(SYSTEM_PROMPT), *messages, wrap])["replies"][0]
        u = Usage.from_haystack_meta("openai", reply.meta)
        rec = tracker.log(provider="openai", model=AGENT_MODEL,
                          stage="agent.wrapup", usage=u)
        steps.append({"step": len(steps) + 1, "input_tokens": u.input_tokens,
                      "cached_tokens": u.cache_read_tokens,
                      "output_tokens": u.output_tokens,
                      "cost_usd": round(rec.cost_usd, 6), "tool_calls": ["(wrapup)"]})
        raw_answer = reply.text or ""
        model_resolved = reply.meta.get("model") or model_resolved
        if not raw_answer.strip():
            verdict = "error"       # spent the budget and produced nothing
        # the bundle must hold everything the model saw and said (P3), and the
        # wrap-up exchange is part of that
        messages = [*messages, wrap, reply]
    validated = validate_citations(raw_answer, registry)

    from x1_advisor.agent.tools import SUMMARY_CONTEXT_ENABLED
    from x1_advisor.fingerprint import turn_fingerprint

    # phase timings (thread-022: console wall time ran ~3x latency_ms with no
    # way to see where) — latency_ms stays the agent loop; the rest of the
    # turn's real cost gets named. The service adds pool_wait/save/total.
    timings_ms: dict[str, int] = {"agent": latency_ms}
    _t = time.monotonic()
    config_id = active_config(conn).id
    # computed once, used twice: the Langfuse trace metadata (so a bad trace
    # names the code/prompt/corpus that produced it) and the turn bundle
    fingerprint = turn_fingerprint(
        conn, prompt=SYSTEM_PROMPT, tools=tools, agent_model=AGENT_MODEL,
        agent_model_resolved=model_resolved, config_id=config_id,
        # flags that change what the model is shown must ride the fingerprint,
        # or an E7 A/B run would be indistinguishable from noise
        feature_flags={"summary_context": SUMMARY_CONTEXT_ENABLED,
                       "agent_reasoning": AGENT_REASONING},
        # live turns never block on a corpus-watermark recompute (thread-022:
        # it was 20.5s of a 30s turn): a just-changed corpus yields the prior
        # watermark flagged stale + a background refresh. QA paths stay exact.
        block_watermark=False)
    timings_ms["fingerprint"] = int((time.monotonic() - _t) * 1000)

    result = {
        "question": question,
        "answer": validated["answer"],
        # the model's output BEFORE validate_citations dropped unresolvable refs
        # and renumbered the rest. Kept and persisted so "why was ref3 dropped"
        # stays answerable after the fact (no-silent-data-loss rule; F5)
        "raw_answer": raw_answer,
        "citations": validated["citations"],
        "citation_stats": {"emitted": validated["emitted"],
                           "resolved": validated["resolved"],
                           "dropped": validated["dropped"],
                           "evidence_registered": len(registry)},
        "steps": steps,
        "tool_calls": tool_calls,
        "latency_ms": latency_ms,
        "timings_ms": timings_ms,
        "cost_usd": round(tracker.run_total, 6),
        "over_soft_cap": tracker.over_per_run_soft_cap(),
    }
    from x1_advisor.telemetry import emit_turn_trace

    _t = time.monotonic()
    result["trace_id"] = emit_turn_trace(result, model=AGENT_MODEL,
                                         fingerprint=fingerprint)
    timings_ms["telemetry"] = int((time.monotonic() - _t) * 1000)

    # the full replayable record. Kept OUT of the caller's user-facing fields:
    # it is an access surface (bundle.py P5) and far too large for a response
    # body, so the service pops it and save_turn persists it.
    _t = time.monotonic()
    result["bundle"] = build_bundle(
        conn, question=question, history=history, thread_id=None, acl=acl,
        prompt=SYSTEM_PROMPT, tools=tools, agent_model=AGENT_MODEL,
        agent_model_resolved=model_resolved, fingerprint=fingerprint,
        config_id=config_id, messages=messages,
        retrieval_explain=retrieval_explain, raw_answer=raw_answer,
        validated=validated, steps=steps, evidence=registry.to_list(),
        # timings_ms rides by REFERENCE: the bundle-build and service phases
        # (save/pool/total) land in it after this call and are present when
        # the bundle is exported at save time
        summary={"verdict": verdict, "steps": len(steps),
                 "cost_usd": result["cost_usd"], "latency_ms": latency_ms,
                 "timings_ms": timings_ms,
                 "over_soft_cap": result["over_soft_cap"],
                 "trace_id": result["trace_id"],
                 "citations": result["citation_stats"]})
    timings_ms["bundle"] = int((time.monotonic() - _t) * 1000)
    return result


def save_turn(conn, result: dict, *, user_id: int = 0,
              thread_id: int | None = None) -> int:
    """Persist to advisor.threads/turns.

    `research_record` holds the v2 turn bundle (bundle.py) — the canonical,
    transactional copy. A complete export also lands in owner-only local
    storage, which is what the teacher loop reads.
    """
    if thread_id is None:
        thread_id = conn.execute(
            "INSERT INTO advisor.threads (user_id, title) VALUES (%s, %s) RETURNING id",
            (user_id, result["question"][:120]),
        ).fetchone()["id"]
    # `content` holds the validated answer; the bundle preserves what the model
    # actually wrote and everything it saw (F5 + Gate 1A)
    record = dict(result.get("bundle") or {})
    if record:
        record["request"] = {**record["request"], "thread_id": thread_id}
    else:   # a caller that built no bundle still gets the v1 fields, not silence
        record = {k: result[k] for k in
                  ("raw_answer", "citations", "citation_stats", "steps",
                   "tool_calls", "latency_ms") if k in result}
    conn.execute(
        """INSERT INTO advisor.turns (thread_id, role, content) VALUES (%s,'user',%s)""",
        (thread_id, result["question"]),
    )
    turn_id = conn.execute(
        """INSERT INTO advisor.turns (thread_id, role, content, research_record, cost_usd)
           VALUES (%s,'assistant',%s,%s,%s) RETURNING id""",
        (thread_id, result["answer"], json.dumps(record, default=str),
         result["cost_usd"]),
    ).fetchone()["id"]
    conn.commit()
    result["turn_id"] = turn_id
    path = export_bundle(record, name=f"turn_{turn_id:08d}_thread_{thread_id}")
    if path:
        result["bundle_path"] = str(path)
    return thread_id
