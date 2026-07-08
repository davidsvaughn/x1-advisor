"""Spike A (cost): cache-token usage fields must survive the Haystack integration.

`x1_advisor.cost.Usage.from_haystack_meta` prices Anthropic calls from
`cache_creation_input_tokens` / `cache_read_input_tokens` in `reply.meta["usage"]`.
The review (§6.1.5) could not confirm anthropic-haystack passes them through — if it
doesn't, every cached call is silently under-priced. This spike makes two identical
calls with a cacheable system prefix and asserts:

  1. call 1 reports cache_creation_input_tokens > 0 (cache write), and
  2. call 2 reports cache_read_input_tokens > 0 (cache read), and
  3. both land in canonical fields via Usage.from_haystack_meta.

Model: claude-haiku-4-5 (cheapest; min cacheable prefix 4096 tokens, so the filler
prefix below is ~6k tokens). Total spend: well under $0.05.
"""

from __future__ import annotations

from spikes._common import require_env, result

require_env("ANTHROPIC_API_KEY")

from haystack.dataclasses import ChatMessage
from haystack_integrations.components.generators.anthropic import AnthropicChatGenerator

from x1_advisor.cost import Usage, estimate

MODEL = "claude-haiku-4-5"

# Deterministic ~6k-token prefix (> the 4096-token minimum cacheable prefix for
# this model). Content is irrelevant; byte-stability across the two calls is what
# matters for the prefix cache.
FILLER = "\n".join(
    f"Reference item {i:04d}: the X1 advisor indexes startup profiles, investor "
    f"profiles, pitch decks, CVs, and evaluation reports as markdown documents "
    f"with block-level citations and ACL-class metadata stamped on every chunk."
    for i in range(220)
)


def run_once(generator: AnthropicChatGenerator) -> dict:
    system = ChatMessage.from_system(f"You are a test assistant. Context:\n{FILLER}")
    system._meta["cache_control"] = {"type": "ephemeral"}
    reply = generator.run(
        [system, ChatMessage.from_user("Reply with the single word: ok")]
    )["replies"][0]
    print(f"  meta['usage'] = {reply.meta.get('usage')}")
    return reply.meta


def main() -> None:
    generator = AnthropicChatGenerator(
        model=MODEL, generation_kwargs={"max_tokens": 16}
    )

    print("call 1 (expect cache WRITE):")
    meta1 = run_once(generator)
    print("call 2 (expect cache READ):")
    meta2 = run_once(generator)

    u1 = Usage.from_haystack_meta("anthropic", meta1)
    u2 = Usage.from_haystack_meta("anthropic", meta2)
    print(f"\ncanonical call 1: {u1}")
    print(f"canonical call 2: {u2}")
    for label, u in (("call 1", u1), ("call 2", u2)):
        bd = estimate(provider="anthropic", model=MODEL, usage=u)
        print(f"{label} priced: ${bd.total:.6f} "
              f"(write=${bd.cache_write_cost:.6f} read=${bd.cache_read_cost:.6f})")

    result(
        u1.cache_write_tokens > 0 and u2.cache_read_tokens > 0,
        "cache_creation/cache_read tokens arrive in reply.meta['usage'] and "
        "normalize through Usage.from_haystack_meta",
    )


if __name__ == "__main__":
    main()
