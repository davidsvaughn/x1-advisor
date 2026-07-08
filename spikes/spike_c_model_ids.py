"""Spike C (models): current + newest Claude model ids and thinking kwargs must not
400 through the integration.

The integration historically lags new API surface (review §6.1.4): adaptive
thinking landed months late, and newest-model handling was unverified. This spike
runs one minimal call per (model, thinking-kwargs) combination that the plan's
bake-offs will need:

  - claude-opus-4-8   + thinking={"type": "adaptive"}   (E4a candidate, current Opus)
  - claude-sonnet-5   + thinking={"type": "adaptive"}   (E4a candidate, current Sonnet)
  - claude-sonnet-4-6 + thinking={"type": "adaptive"}   (E4a candidate)
  - claude-haiku-4-5  + no thinking                     (E4b candidate; pre-4.6 rules)

Note: adaptive-thinking models reject temperature/top_p/budget_tokens — those are
deliberately absent. Cost: four tiny calls, < $0.05 total.

If claude-sonnet-5 passes, add its pricing row to cost.py before using it in
bake-offs (cost.py raises on unknown models by design).
"""

from __future__ import annotations

from spikes._common import require_env, result

require_env("ANTHROPIC_API_KEY")

from haystack.dataclasses import ChatMessage
from haystack_integrations.components.generators.anthropic import AnthropicChatGenerator

# (model, generation_kwargs)
CASES = [
    ("claude-opus-4-8", {"max_tokens": 2048, "thinking": {"type": "adaptive"}}),
    ("claude-sonnet-5", {"max_tokens": 2048, "thinking": {"type": "adaptive"}}),
    ("claude-sonnet-4-6", {"max_tokens": 2048, "thinking": {"type": "adaptive"}}),
    ("claude-haiku-4-5", {"max_tokens": 64}),
]


def main() -> None:
    failures: list[str] = []
    for model, kwargs in CASES:
        label = f"{model} kwargs={kwargs}"
        print(f"-- {label}")
        try:
            generator = AnthropicChatGenerator(model=model, generation_kwargs=kwargs)
            reply = generator.run(
                [ChatMessage.from_user("Reply with the single word: ok")]
            )["replies"][0]
            print(f"   ok: model={reply.meta.get('model')} "
                  f"finish={reply.meta.get('finish_reason')} "
                  f"usage={reply.meta.get('usage')}")
        except Exception as exc:  # noqa: BLE001 — the spike's job is to report this
            print(f"   FAILED: {type(exc).__name__}: {exc}")
            failures.append(label)

    if failures:
        print("\nfailed cases:")
        for f in failures:
            print(f"  - {f}")
    result(not failures, "current/newest model ids + thinking kwargs accepted by the integration")


if __name__ == "__main__":
    main()
