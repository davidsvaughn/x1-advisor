"""LLM telemetry: token-usage accounting + cost computation.

Every LLM call in X1 Advisor must request **full token usage** and route it through
`Tracker.log(...)`, so we always know what a turn (and a day) costs. This covers not just
the chat answer but *all* model calls: query planning, the deep-tier critic/sub-agents,
ingestion-time calls (VLM captions, table summaries, record/profile summaries), and
embeddings.

Design notes
------------
- `estimate()` is a **pure** function over canonical token counts. It never reads provider
  responses directly, so it's trivially testable and provider-agnostic.
- Provider usage payloads are *not* uniform, so `Usage.from_haystack_meta()` normalizes them
  into canonical fields (uncached input / cache-read / cache-write / output). See the per-
  provider notes there — Anthropic's `input_tokens` already EXCLUDES cached tokens, whereas
  OpenAI's `prompt_tokens` INCLUDES them. Getting this wrong silently mis-prices every call.
- Anthropic prompt caching has two prices: **cache read ≈ 0.1× input**, **cache write ≈ 1.25×
  input**. Both are tracked. Our stable-prompt-prefix strategy makes these material.
- Pricing lives inline in `PRICING` (per 1M tokens, USD). Unknown model ⇒ raise, so a missing
  entry is loud, not a silent $0.
- Persistence is via a pluggable `CostSink`; the pricing/accounting core has no DB dependency.
  `JsonlSink` is provided as a zero-infra default that preserves the full record (no truncation).

Usage
-----
    from x1_advisor.cost import Tracker, Usage, JsonlSink

    tracker = Tracker(run_id="turn-abc", sink=JsonlSink("cost_ledger.jsonl"))

    result = chat_generator.run(messages)            # Haystack generator
    usage = Usage.from_haystack_meta("anthropic", result["replies"][0].meta)
    tracker.log(provider="anthropic", model="claude-opus-4-8",
                stage="agent.respond", purpose="answer user", usage=usage)

    print(tracker.run_total)        # USD spent this turn
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal, Protocol

ProviderName = Literal["anthropic", "openai", "gemini", "grok", "deepseek", "jina"]


# ---------------------------------------------------------------------------
# Pricing — per 1,000,000 tokens, USD.
#   input        : uncached input tokens
#   cache_read   : tokens served from prompt cache (Anthropic ~0.1x input)
#   cache_write  : tokens written to prompt cache (Anthropic ~1.25x input, 5-min ephemeral)
#   output       : generated tokens
#   _embed       : embedding models are priced per input token (output/cache = 0)
#   _tool_<name> : per-call surcharge for a provider-billed tool (e.g. web search)
# Unknown model => KeyError (loud). Values marked "verify" are estimates pending confirmation.
# ---------------------------------------------------------------------------
PRICING: dict[str, dict[str, dict[str, float]]] = {
    "anthropic": {
        # cache_write = 1.25 * input (5-min ephemeral); cache_read = 0.1 * input.
        "claude-opus-4-8":   {"input": 5.00, "cache_read": 0.50, "cache_write": 6.25, "output": 25.00},
        "claude-opus-4-7":   {"input": 5.00, "cache_read": 0.50, "cache_write": 6.25, "output": 25.00},
        "claude-opus-4-6":   {"input": 5.00, "cache_read": 0.50, "cache_write": 6.25, "output": 25.00},
        "claude-opus-4-5":   {"input": 5.00, "cache_read": 0.50, "cache_write": 6.25, "output": 25.00},
        "claude-sonnet-4-6": {"input": 3.00, "cache_read": 0.30, "cache_write": 3.75, "output": 15.00},
        "claude-haiku-4-5":  {"input": 1.00, "cache_read": 0.10, "cache_write": 1.25, "output":  5.00},
        # embeddings: Anthropic has no first-party embedding model as of this writing.
        "_tool_web_search":  {"per_call": 0.010},
    },
    "openai": {
        # OpenAI auto-caches; cache reads are discounted, cache writes are not separately billed.
        # gpt-5.6 family, verified 2026-07-31 against developers.openai.com/api/docs/pricing
        # (the 2026-07-30 price cut is included). Tiers are sol > terra > luna on
        # capability AND on price; luna is the budget tier, not a stronger model.
        "gpt-5.6-sol":       {"input": 5.00, "cache_read": 0.500, "output": 30.00},
        "gpt-5.6-terra":     {"input": 2.00, "cache_read": 0.200, "output": 12.00},
        "gpt-5.6-luna":      {"input": 0.20, "cache_read": 0.020, "output":  1.20},
        "gpt-5.5":           {"input": 5.00, "cache_read": 0.500, "output": 30.00},  # <=272K ctx
        "gpt-5.1":           {"input": 1.25, "cache_read": 0.125, "output": 10.00},
        "gpt-5.2":           {"input": 1.75, "cache_read": 0.175, "output": 14.00},
        "gpt-5.4":           {"input": 2.50, "cache_read": 0.250, "output": 15.00},  # verify
        "gpt-5.4-mini":      {"input": 0.75, "cache_read": 0.075, "output":  4.50},  # verify
        "gpt-5.4-nano":      {"input": 0.20, "cache_read": 0.020, "output":  1.25},  # verify
        "gpt-5-mini":        {"input": 0.25, "cache_read": 0.025, "output":  2.00},
        "text-embedding-3-small": {"_embed": 0.02},
        "text-embedding-3-large": {"_embed": 0.13},
        "_tool_web_search":  {"per_call": 0.010},
    },
    "gemini": {
        # Gemini has tiered input pricing (<=200K vs >200K); assumes the low tier for our prompts.
        "gemini-3.1-pro-preview":  {"input": 2.00, "cache_read": 0.20, "output": 12.00},
        "gemini-3-flash-preview":  {"input": 0.50, "cache_read": 0.05, "output":  3.00},
        "gemini-3.1-flash-lite":   {"input": 0.25, "cache_read": 0.025, "output": 1.50},  # verify
        "_tool_web_search":        {"per_call": 0.010},
    },
    "grok": {
        "grok-4.3":          {"input": 1.25, "cache_read": 0.20, "output": 2.50},
        "_tool_web_search":  {"per_call": 0.005},
        "_tool_x_search":    {"per_call": 0.005},
    },
    "jina": {
        # Reranker API bills tokens from the same pack as embeddings; v2 was $0.02/1M and
        # the v3 page advertises the shared token-pack pricing (exact v3 rate: verify).
        "jina-reranker-v3": {"_embed": 0.02},  # verify
    },
    "deepseek": {
        "deepseek-v4-pro":   {"input": 0.435, "cache_read": 0.003625, "output": 0.87},
        "deepseek-v4-flash": {"input": 0.14,  "cache_read": 0.0028,   "output": 0.28},
        # Server-side web search (Anthropic-compatible endpoint only) bills TOKENS ONLY:
        # verified 2026-07-07 via live call (usage reported server_tool_use.web_search_requests=1,
        # search results injected as ~5.5k input tokens, no fee field) and the official pricing
        # page (token prices only, no per-search line item). Explicit $0 row so the tool-call
        # accounting path stays visible rather than silently unpriced.
        "_tool_web_search":  {"per_call": 0.0},
    },
}


def _per_token(rate_per_million: float) -> float:
    return rate_per_million / 1_000_000.0


# ---------------------------------------------------------------------------
# Canonical usage + normalization from provider meta
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Usage:
    """Canonical, provider-agnostic token usage for a single LLM call.

    All four token fields are non-overlapping: total input = input + cache_read + cache_write.
    """
    input_tokens: int = 0        # uncached input
    output_tokens: int = 0
    cache_read_tokens: int = 0   # served from prompt cache
    cache_write_tokens: int = 0  # written to prompt cache
    embed_tokens: int = 0        # for embedding calls

    @classmethod
    def from_haystack_meta(cls, provider: ProviderName, meta: dict[str, Any]) -> "Usage":
        """Normalize a Haystack ``reply.meta`` (or its ``usage`` sub-dict) into canonical fields.

        Field names differ by provider — handle each explicitly rather than guessing:

        - **anthropic**: ``input_tokens`` already EXCLUDES cached tokens; cache fields are
          ``cache_read_input_tokens`` and ``cache_creation_input_tokens``.
        - **openai/azure**: ``prompt_tokens`` INCLUDES cached; cached count is at
          ``prompt_tokens_details.cached_tokens``; no separate cache-write charge.
        - **gemini/grok/deepseek**: OpenAI-style ``prompt_tokens``/``completion_tokens``.
        """
        usage = meta.get("usage", meta) if isinstance(meta, dict) else {}
        usage = usage or {}

        if provider == "anthropic":
            return cls(
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
                # some integrations surface the write count as cache_write_input_tokens
                cache_write_tokens=int(
                    usage.get("cache_creation_input_tokens",
                              usage.get("cache_write_input_tokens", 0))
                ),
            )

        # Some providers speak the Anthropic wire shape even though they're priced as
        # themselves — DeepSeek's Anthropic-compatible endpoint (the only one with its
        # server-side web_search; verified 2026-07-07) returns input_tokens +
        # cache_read/cache_creation_input_tokens. Without this, cache tokens would be
        # silently dropped from the OpenAI-shape branch below.
        if "prompt_tokens" not in usage and (
            "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage
        ):
            return cls.from_haystack_meta("anthropic", usage)

        # OpenAI-compatible shape (openai, azure, gemini, grok, deepseek, ...)
        prompt = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
        completion = int(usage.get("completion_tokens", usage.get("output_tokens", 0)))
        details = usage.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens", 0))
        return cls(
            input_tokens=max(0, prompt - cached),
            output_tokens=completion,
            cache_read_tokens=cached,
            cache_write_tokens=0,
        )


# ---------------------------------------------------------------------------
# Pure pricing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Breakdown:
    input_cost: float = 0.0
    cache_read_cost: float = 0.0
    cache_write_cost: float = 0.0
    output_cost: float = 0.0
    embed_cost: float = 0.0
    tool_cost: float = 0.0

    @property
    def total(self) -> float:
        return (self.input_cost + self.cache_read_cost + self.cache_write_cost
                + self.output_cost + self.embed_cost + self.tool_cost)


def estimate(
    *,
    provider: ProviderName,
    model: str,
    usage: Usage | None = None,
    tool_calls: Iterable[str] | None = None,
) -> Breakdown:
    """Pure pricing estimate from canonical `usage`. Raises on unknown provider/model."""
    usage = usage or Usage()
    if provider not in PRICING:
        raise KeyError(f"Unknown provider: {provider!r}")
    table = PRICING[provider]
    if model not in table:
        raise KeyError(f"Unknown {provider} model: {model!r} (add it to PRICING in cost.py)")
    rates = table[model]

    input_rate = rates.get("input", 0.0)
    cache_read_rate = rates.get("cache_read", input_rate)        # default: full input price
    cache_write_rate = rates.get("cache_write", input_rate)      # default: full input price
    output_rate = rates.get("output", 0.0)
    embed_rate = rates.get("_embed", 0.0)

    bd = Breakdown(
        input_cost=usage.input_tokens * _per_token(input_rate),
        cache_read_cost=usage.cache_read_tokens * _per_token(cache_read_rate),
        cache_write_cost=usage.cache_write_tokens * _per_token(cache_write_rate),
        output_cost=usage.output_tokens * _per_token(output_rate),
        embed_cost=usage.embed_tokens * _per_token(embed_rate),
        tool_cost=sum(
            table[f"_tool_{t}"]["per_call"]
            for t in (tool_calls or [])
            if f"_tool_{t}" in table and "per_call" in table[f"_tool_{t}"]
        ),
    )
    return bd


# ---------------------------------------------------------------------------
# Persistence sink (pluggable)
# ---------------------------------------------------------------------------
@dataclass
class CostRecord:
    run_id: str | None
    provider: str
    model: str
    stage: str
    purpose: str | None
    usage: Usage
    breakdown: Breakdown
    tool_calls: list[str]

    @property
    def cost_usd(self) -> float:
        return self.breakdown.total

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cost_usd"] = self.cost_usd
        return d


class CostSink(Protocol):
    def record(self, rec: CostRecord) -> None: ...


class NullSink:
    """Discards records (in-memory accounting only)."""
    def record(self, rec: CostRecord) -> None:  # noqa: D401
        return None


class JsonlSink:
    """Appends one full JSON record per call. Preserves all fields — never truncates."""
    def __init__(self, path: str) -> None:
        self.path = path

    def record(self, rec: CostRecord) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec.to_dict()) + "\n")


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------
@dataclass
class _Caps:
    per_run_soft_cap_usd: float = 3.0
    daily_cap_usd: float | None = None  # enforced by the sink/ledger if it can sum a day


class Tracker:
    """Per-run cost accumulator. Logs every LLM call and forwards to a `CostSink`."""

    def __init__(
        self,
        *,
        run_id: str | None = None,
        sink: CostSink | None = None,
        per_run_soft_cap_usd: float = 3.0,
    ) -> None:
        self.run_id = run_id
        self.sink: CostSink = sink or NullSink()
        self.caps = _Caps(per_run_soft_cap_usd=per_run_soft_cap_usd)
        self._run_total: float = 0.0
        self.records: list[CostRecord] = []

    def log(
        self,
        *,
        provider: ProviderName,
        model: str,
        stage: str,
        usage: Usage,
        purpose: str | None = None,
        tool_calls: Iterable[str] | None = None,
    ) -> CostRecord:
        tools = list(tool_calls or [])
        bd = estimate(provider=provider, model=model, usage=usage, tool_calls=tools)
        rec = CostRecord(
            run_id=self.run_id, provider=provider, model=model, stage=stage,
            purpose=purpose, usage=usage, breakdown=bd, tool_calls=tools,
        )
        self._run_total += bd.total
        self.records.append(rec)
        self.sink.record(rec)
        return rec

    def log_haystack(
        self,
        *,
        provider: ProviderName,
        model: str,
        stage: str,
        meta: dict[str, Any],
        purpose: str | None = None,
        tool_calls: Iterable[str] | None = None,
    ) -> CostRecord:
        """Convenience: extract usage straight from a Haystack ``reply.meta`` and log it."""
        return self.log(
            provider=provider, model=model, stage=stage, purpose=purpose,
            usage=Usage.from_haystack_meta(provider, meta), tool_calls=tool_calls,
        )

    @property
    def run_total(self) -> float:
        return self._run_total

    def over_per_run_soft_cap(self) -> bool:
        return self._run_total >= self.caps.per_run_soft_cap_usd


if __name__ == "__main__":
    # Smoke test: an Anthropic call that read a cached prefix and wrote a new one.
    u = Usage(input_tokens=1200, output_tokens=800, cache_read_tokens=9000, cache_write_tokens=300)
    bd = estimate(provider="anthropic", model="claude-opus-4-8", usage=u, tool_calls=["web_search"])
    print(
        f"opus-4-8: in=${bd.input_cost:.5f} read=${bd.cache_read_cost:.5f} "
        f"write=${bd.cache_write_cost:.5f} out=${bd.output_cost:.5f} "
        f"tool=${bd.tool_cost:.5f} total=${bd.total:.5f}"
    )
