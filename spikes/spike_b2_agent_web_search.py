"""Spike B′ (agent loop, available-provider variant): the Phase-4 shape — a Haystack
Agent whose web evidence comes from a delegated-searcher tool — must work end-to-end.

Provider policy (David, 2026-07-08): OpenAI is the default for chat, embeddings, AND
web search — those calls are company-paid. DeepSeek remains a wired-up option (very
cheap, verified in spike D) but runs on David's personal key today, so it is opt-in
only until a company DeepSeek key exists.

Usage: `python -m spikes.spike_b2_agent_web_search [--searcher deepseek]`

Default: gpt-5.1 main agent + gpt-5.1 delegated searcher via OpenAI's server-side
web_search (Responses API; per-call fee already priced in cost.py). The searcher is
wrapped as an ordinary Haystack `Tool` — the E3 "delegated searcher" shape, so the
agent's tool schema is identical whichever provider backs it.

Checks:
  1. Agent(exit_conditions=["text"], max_agent_steps=4) terminates with a text answer,
  2. the web_research tool was actually invoked (a tool-call message in the trace),
  3. the tool's findings carried >= 1 real citation URL (citation contract).

Cost: a few cents (OpenAI path), company-paid.
"""

from __future__ import annotations

import json
import sys

import httpx

from spikes._common import require_env, result

SEARCHER = "deepseek" if "--searcher" in sys.argv and "deepseek" in sys.argv else "openai"

require_env("OPENAI_API_KEY")  # main agent is always OpenAI
if SEARCHER == "deepseek":
    DEEPSEEK_API_KEY = require_env("DEEPSEEK_API_KEY")
    print("NOTE: --searcher deepseek runs on the PERSONAL DeepSeek key (opt-in).")

from haystack.components.agents import Agent
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.dataclasses import ChatMessage
from haystack.tools import Tool

from x1_advisor.cost import Usage, estimate

QUESTION = "At what level did the S&P 500 index most recently close? One sentence."
SEARCH_MODEL = "gpt-5.1"

_citations_seen: list[dict] = []  # spike-level evidence capture


def web_research_openai(question: str) -> str:
    """Delegated search via OpenAI's server-side web_search (Responses API).

    Citations come from two channels, both needed (verified live 2026-07-08 on
    gpt-5.1): inline `url_citation` annotations are NOT reliably emitted, while
    `include=["web_search_call.action.sources"]` dependably returns the consulted
    source URLs (filter to type=="url" — internal feeds like `oai-finance` come
    back as type=="api" with url=null).
    """
    from openai import OpenAI

    resp = OpenAI().responses.create(
        model=SEARCH_MODEL,
        tools=[{"type": "web_search"}],
        include=["web_search_call.action.sources"],
        input=question,
    )
    citations: list[dict] = []
    for item in resp.output:
        itype = getattr(item, "type", "")
        if itype == "message":
            for content in getattr(item, "content", []) or []:
                for ann in getattr(content, "annotations", []) or []:
                    if getattr(ann, "type", "") == "url_citation" and getattr(ann, "url", None):
                        citations.append({"url": ann.url, "title": getattr(ann, "title", None)})
        elif itype == "web_search_call":
            sources = getattr(getattr(item, "action", None), "sources", None) or []
            for src in sources:
                if getattr(src, "type", "") == "url" and getattr(src, "url", None):
                    citations.append({"url": src.url, "title": None})
    seen: set[str] = set()
    citations = [c for c in citations if not (c["url"] in seen or seen.add(c["url"]))]
    _citations_seen.extend(citations)
    usage = Usage(
        input_tokens=resp.usage.input_tokens if resp.usage else 0,
        output_tokens=resp.usage.output_tokens if resp.usage else 0,
    )
    bd = estimate(provider="openai", model=SEARCH_MODEL, usage=usage, tool_calls=["web_search"])
    print(f"  [web_research/openai] {len(citations)} citations; "
          f"usage in={usage.input_tokens} out={usage.output_tokens}; ${bd.total:.6f}")
    return json.dumps({"findings": resp.output_text, "citations": citations[:8]})


def web_research_deepseek(question: str) -> str:
    """Delegated search via DeepSeek (spike D contract). OPT-IN: personal key."""
    resp = httpx.post(
        "https://api.deepseek.com/anthropic/v1/messages",
        headers={
            "x-api-key": DEEPSEEK_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "deepseek-v4-flash",
            "max_tokens": 4096,
            "system": "Use the web_search tool; answer concisely with key facts.",
            "messages": [{"role": "user", "content": question}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    text_parts, citations = [], []
    for block in data.get("content", []) or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
            for cit in block.get("citations") or []:
                if isinstance(cit, dict) and cit.get("url"):
                    citations.append({"url": cit["url"], "title": cit.get("title")})
        elif block.get("type") == "web_search_tool_result":
            content = block.get("content")
            for res in content if isinstance(content, list) else []:
                if isinstance(res, dict) and res.get("url"):
                    citations.append({"url": res["url"], "title": res.get("title")})
    _citations_seen.extend(citations)
    print(f"  [web_research/deepseek] {len(citations)} citations; "
          f"usage={data.get('usage', {})}")
    return json.dumps(
        {"findings": "\n".join(text_parts).strip(), "citations": citations[:8]}
    )


TOOL = Tool(
    name="web_research",
    description=(
        "Research a question on the live web. Returns grounded findings plus "
        "citations with real URLs. Use for anything needing current information."
    ),
    parameters={
        "type": "object",
        "properties": {"question": {"type": "string", "description": "The research question."}},
        "required": ["question"],
    },
    function=web_research_openai if SEARCHER == "openai" else web_research_deepseek,
)


def main() -> None:
    print(f"searcher = {SEARCHER}")
    agent = Agent(
        chat_generator=OpenAIChatGenerator(model="gpt-5.1"),
        tools=[TOOL],
        system_prompt=(
            "Answer using the web_research tool for anything time-sensitive. "
            "Be concise."
        ),
        exit_conditions=["text"],
        max_agent_steps=4,
    )
    agent.warm_up()
    messages = agent.run(messages=[ChatMessage.from_user(QUESTION)])["messages"]

    tool_called = any(m.tool_calls for m in messages)
    final_text = messages[-1].text or ""
    print(f"\ntrace: {len(messages)} messages; tool_called={tool_called}; "
          f"citations_total={len(_citations_seen)}")
    print(f"final answer: {final_text[:300]}")

    result(
        tool_called and bool(final_text.strip()) and len(_citations_seen) >= 1,
        f"Haystack Agent loop + {SEARCHER} delegated-search tool + OpenAI main agent "
        "work end-to-end with resolvable citations",
    )


if __name__ == "__main__":
    main()
