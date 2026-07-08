"""Spike D (remaining sub-task): confirm how DeepSeek bills its server-side
web_search from a live call's usage block, then price it in cost.py.

The API contract itself is already resolved (alpha-claw, verified 2026-06-22):
Anthropic-compatible endpoint only, `web_search_20250305` server tool, citations
as {url, title}. What is still open is billing: does the usage block report a
per-search count/fee, or is search folded into token usage only?

This spike makes one live web-search call with deepseek-v4-flash and dumps the
FULL usage block (plus content block types + citations, re-verifying the
contract from this repo). Decision rule for cost.py:
  - usage reports a search-request count (`server_tool_use.web_search_requests`
    or similar) → keep a `_tool_web_search` row keyed to whatever DeepSeek's
    published per-search price is (0 if searches are free beyond tokens).
  - usage reports tokens only → search results are billed as input tokens;
    no per-call surcharge row is needed (record that as the finding).

Cost: one flash call, fractions of a cent.
"""

from __future__ import annotations

import json

import httpx

from spikes._common import require_env, result

API_KEY = require_env("DEEPSEEK_API_KEY")

BASE = "https://api.deepseek.com/anthropic"
MODEL = "deepseek-v4-flash"
QUESTION = (
    "What was the closing price of the S&P 500 index at the most recent close? "
    "Answer in one sentence with the date."
)


def main() -> None:
    body = {
        "model": MODEL,
        "max_tokens": 4096,  # flash truncates at low max_tokens in search mode
        "system": (
            "Use the web_search tool to answer with current information. "
            "Answer concisely."
        ),
        "messages": [{"role": "user", "content": QUESTION}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
    }
    resp = httpx.post(
        f"{BASE}/v1/messages",
        headers={
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body,
        timeout=120,
    )
    print(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()

    print("\n-- content block types --")
    citations: list[dict] = []
    for block in data.get("content", []) or []:
        btype = block.get("type")
        print(f"  {btype}")
        if btype == "text":
            print(f"    text: {block.get('text', '')[:300]}")
            for cit in block.get("citations") or []:
                if isinstance(cit, dict) and cit.get("url"):
                    citations.append({"url": cit["url"], "title": cit.get("title")})
        elif btype == "web_search_tool_result":
            content = block.get("content")
            for res in content if isinstance(content, list) else []:
                if isinstance(res, dict) and res.get("url"):
                    citations.append({"url": res["url"], "title": res.get("title")})

    print(f"\n-- citations ({len(citations)}) --")
    for c in citations[:8]:
        print(f"  {c['url']}  ({c.get('title')})")

    usage = data.get("usage", {})
    print("\n-- FULL usage block (verbatim — the billing evidence) --")
    print(json.dumps(usage, indent=2, sort_keys=True))

    server_tool_use = usage.get("server_tool_use") or {}
    search_count = server_tool_use.get("web_search_requests")
    print(f"\nweb_search_requests reported: {search_count!r}")
    print(f"stop_reason: {data.get('stop_reason')!r}  model: {data.get('model')!r}")

    searched = search_count is not None and search_count > 0 or bool(citations)
    result(
        searched and bool(usage.get("output_tokens")),
        "live web_search call succeeded; usage block captured above — set the "
        "deepseek _tool_web_search decision in cost.py from this evidence",
    )


if __name__ == "__main__":
    main()
