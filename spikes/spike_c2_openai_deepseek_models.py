"""Spike C′ (models, available-provider variant): the model ids and endpoints the
build actually runs on must not 400 through Haystack.

Covers the working stack while ANTHROPIC/VOYAGE keys are absent:
  - gpt-5.1 and gpt-5-mini through OpenAIChatGenerator (E4 candidates with solid
    pricing rows in cost.py),
  - deepseek-v4-flash through OpenAIChatGenerator pointed at DeepSeek's
    OpenAI-compatible endpoint (E4b candidate; the generator-registry seam),
  - text-embedding-3-small through OpenAITextEmbedder (E1 candidate and the working
    default embedding for Phase 1/2; expect 1536 dims — same dim as the
    advisor_evidence precedent) with usage priced through cost.py.

Cost: four tiny calls, ~a cent.
"""

from __future__ import annotations

from spikes._common import require_env, result

require_env("OPENAI_API_KEY")
require_env("DEEPSEEK_API_KEY")

from haystack.components.embedders import OpenAITextEmbedder
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.dataclasses import ChatMessage
from haystack.utils import Secret

from x1_advisor.cost import Usage, estimate

EMBED_MODEL = "text-embedding-3-small"

CHAT_CASES = [
    ("openai", "gpt-5.1", {}),
    ("openai", "gpt-5-mini", {}),
    ("deepseek", "deepseek-v4-flash", {"api_base_url": "https://api.deepseek.com",
                                       "api_key": Secret.from_env_var("DEEPSEEK_API_KEY")}),
]


def check_chat_models() -> list[str]:
    failures: list[str] = []
    for provider, model, extra in CHAT_CASES:
        print(f"-- chat: {provider}:{model}")
        try:
            generator = OpenAIChatGenerator(model=model, **extra)
            reply = generator.run(
                [ChatMessage.from_user("Reply with the single word: ok")]
            )["replies"][0]
            usage = Usage.from_haystack_meta(provider, reply.meta)
            bd = estimate(provider=provider, model=model, usage=usage)
            print(f"   ok: model={reply.meta.get('model')} usage={reply.meta.get('usage')}")
            print(f"   priced: ${bd.total:.6f}")
        except Exception as exc:  # noqa: BLE001 — the spike's job is to report this
            print(f"   FAILED: {type(exc).__name__}: {exc}")
            failures.append(f"{provider}:{model}")
    return failures


def check_embedder() -> list[str]:
    print(f"-- embed: openai:{EMBED_MODEL}")
    try:
        embedder = OpenAITextEmbedder(model=EMBED_MODEL)
        out = embedder.run(text="Seed-stage fintech startup in Berlin building payment rails.")
        dim = len(out["embedding"])
        meta = out.get("meta", {})
        print(f"   ok: dim={dim} meta={meta}")
        tokens = (meta.get("usage") or {}).get("prompt_tokens", 0)
        bd = estimate(provider="openai", model=EMBED_MODEL, usage=Usage(embed_tokens=tokens))
        print(f"   priced: ${bd.total:.8f} ({tokens} embed tokens)")
        if dim != 1536:
            return [f"openai:{EMBED_MODEL} unexpected dim {dim}"]
        return []
    except Exception as exc:  # noqa: BLE001
        print(f"   FAILED: {type(exc).__name__}: {exc}")
        return [f"openai:{EMBED_MODEL}"]


def main() -> None:
    failures = check_chat_models() + check_embedder()
    if failures:
        print("\nfailed cases:")
        for f in failures:
            print(f"  - {f}")
    result(not failures, "available-provider model ids + embedder accepted through Haystack")


if __name__ == "__main__":
    main()
