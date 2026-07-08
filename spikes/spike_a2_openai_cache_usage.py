"""Spike A′ (cost, OpenAI variant): cached-token usage fields must survive the
Haystack integration for the provider we're actually developing on.

Same concern as spike A (Anthropic variant, key-blocked): `cost.py` prices OpenAI
cache reads from `prompt_tokens_details.cached_tokens` inside `reply.meta["usage"]`.
If Haystack's OpenAIChatGenerator drops that sub-dict, every cached call is silently
over-priced (cached tokens billed at full input rate in our ledger — 10x the real
price) and we can't see caching working at all.

OpenAI caches automatically for prompts >= 1024 tokens (no cache_control needed).
Cache hits on a fresh prefix can lag by a few seconds, so call 1 primes and calls
2..4 retry until cached_tokens > 0.

Usage: `python -m spikes.spike_a2_openai_cache_usage [openai|deepseek]`
DeepSeek speaks the same OpenAI wire shape (incl. prompt_tokens_details) through
its /chat/completions endpoint and also caches automatically — the deepseek run
covers the gate while the OpenAI key is invalid (401 as of 2026-07-08).

Models: gpt-5-mini ($0.25/1M in) or deepseek-v4-flash. Spend: well under $0.01.
"""

from __future__ import annotations

import sys
import time

from spikes._common import require_env, result

PROVIDERS = {
    "openai": {"model": "gpt-5-mini", "extra": {}},
    "deepseek": {"model": "deepseek-v4-flash", "extra": {"api_base_url": "https://api.deepseek.com"}},
}
PROVIDER = sys.argv[1] if len(sys.argv) > 1 else "openai"
if PROVIDER not in PROVIDERS:
    print(f"usage: python -m spikes.spike_a2_openai_cache_usage [{'|'.join(PROVIDERS)}]")
    sys.exit(2)

require_env("OPENAI_API_KEY" if PROVIDER == "openai" else "DEEPSEEK_API_KEY")

from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.dataclasses import ChatMessage
from haystack.utils import Secret

from x1_advisor.cost import Usage, estimate

MODEL = PROVIDERS[PROVIDER]["model"]

# Deterministic ~2k-token prefix (>= OpenAI's 1024-token auto-cache minimum).
FILLER = "\n".join(
    f"Reference item {i:04d}: the X1 advisor indexes startup profiles, investor "
    f"profiles, pitch decks, CVs, and evaluation reports as markdown documents "
    f"with block-level citations and ACL-class metadata stamped on every chunk."
    for i in range(80)
)


def run_once(generator: OpenAIChatGenerator) -> dict:
    reply = generator.run(
        [
            ChatMessage.from_system(f"You are a test assistant. Context:\n{FILLER}"),
            ChatMessage.from_user("Reply with the single word: ok"),
        ]
    )["replies"][0]
    print(f"  meta['usage'] = {reply.meta.get('usage')}")
    return reply.meta


def main() -> None:
    extra = dict(PROVIDERS[PROVIDER]["extra"])
    if PROVIDER == "deepseek":
        extra["api_key"] = Secret.from_env_var("DEEPSEEK_API_KEY")
    generator = OpenAIChatGenerator(model=MODEL, **extra)

    print("call 1 (primes the cache):")
    meta = run_once(generator)
    u_first = Usage.from_haystack_meta("openai", meta)

    u_cached = None
    for attempt in range(2, 5):
        time.sleep(2)
        print(f"call {attempt} (expect cached_tokens > 0):")
        meta = run_once(generator)
        u = Usage.from_haystack_meta("openai", meta)
        if u.cache_read_tokens > 0:
            u_cached = u
            break

    print(f"\ncanonical first call:  {u_first}")
    print(f"canonical cached call: {u_cached}")
    if u_cached:
        bd = estimate(provider="openai", model=MODEL, usage=u_cached)
        print(f"cached call priced: ${bd.total:.6f} (read=${bd.cache_read_cost:.6f})")

    result(
        u_first.input_tokens > 0 and u_cached is not None,
        "OpenAI prompt_tokens_details.cached_tokens arrives in reply.meta['usage'] "
        "and normalizes through Usage.from_haystack_meta",
    )


if __name__ == "__main__":
    main()
