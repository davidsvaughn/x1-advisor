"""Spike B (server tools): Anthropic server-side web_search must pass through the
integration — first on a bare generator call, then inside a Haystack Agent loop.

Server tools (web_search/web_fetch) execute on Anthropic's side: the client only
declares the tool block; results come back as `server_tool_use` /
`web_search_tool_result` content blocks in the same response. The question this
spike answers (review §6.4 guardrail 2b): does anthropic-haystack accept a raw
server-tool dict (there is no Haystack `Tool` for it) and surface the grounded
answer without crashing?

Checks:
  1. Bare generator: raw tool dict via generation_kwargs["tools"] → non-empty text
     answer, no exception.
  2. Agent loop: same generator inside `Agent(exit_conditions=["text"])` → the loop
     terminates with a text answer (server tools never hit the Agent's tool invoker,
     so the first reply should already be terminal text).

If check 1 fails: E3's Anthropic candidate runs through a thin direct-SDK tool
instead (named fallback in the plan) — E3 itself proceeds.

Model: claude-sonnet-4-6 (supports web_search_20260209). ~1 search + small tokens
per check ≈ $0.02–0.04 total.
"""

from __future__ import annotations

from spikes._common import require_env, result

require_env("ANTHROPIC_API_KEY")

from haystack.components.agents import Agent
from haystack.dataclasses import ChatMessage
from haystack_integrations.components.generators.anthropic import AnthropicChatGenerator

MODEL = "claude-sonnet-4-6"
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 1}
QUESTION = "In one sentence: what is Anthropic's most recently released Claude model?"


def check_bare_generator() -> bool:
    print("check 1: raw server-tool dict via generation_kwargs on a bare generator")
    generator = AnthropicChatGenerator(
        model=MODEL,
        generation_kwargs={"max_tokens": 1024, "tools": [WEB_SEARCH_TOOL]},
    )
    try:
        reply = generator.run([ChatMessage.from_user(QUESTION)])["replies"][0]
    except Exception as exc:  # noqa: BLE001 — the spike's job is to report this
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        return False
    print(f"  finish_reason={reply.meta.get('finish_reason')} "
          f"usage={reply.meta.get('usage')}")
    text = reply.text or ""
    print(f"  text ({len(text)} chars): {text[:300]}")
    return bool(text.strip())


def check_agent_loop() -> bool:
    print("\ncheck 2: same server tool inside a Haystack Agent loop")
    agent = Agent(
        chat_generator=AnthropicChatGenerator(
            model=MODEL,
            generation_kwargs={"max_tokens": 1024, "tools": [WEB_SEARCH_TOOL]},
        ),
        tools=[],
        exit_conditions=["text"],
        max_agent_steps=4,
    )
    agent.warm_up()
    try:
        messages = agent.run(messages=[ChatMessage.from_user(QUESTION)])["messages"]
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        return False
    final = messages[-1]
    text = final.text or ""
    print(f"  {len(messages)} messages; final text ({len(text)} chars): {text[:300]}")
    return bool(text.strip())


def main() -> None:
    ok_bare = check_bare_generator()
    ok_agent = check_agent_loop()
    result(
        ok_bare and ok_agent,
        "Anthropic server-side web_search passes through the integration "
        "(bare generator + Agent loop)",
    )


if __name__ == "__main__":
    main()
