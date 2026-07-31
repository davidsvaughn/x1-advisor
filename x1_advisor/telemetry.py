"""Langfuse observability for agent turns (Phase 4; project `x1-backend-agentic`).

Env-gated: USE_LANGFUSE=true + LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL (mapped to
LANGFUSE_HOST for the SDK). One trace per turn: root span carries question/answer,
child generation observations carry per-step ACTUAL usage (input/cached/output
tokens) and cost from cost.py, tool spans carry call arguments; trace scores:
citation_resolvability and cost_usd — the hooks the Langfuse eval workflows
(github.com/langfuse/skills) build on for continual improvement.

Telemetry must never break a turn: emission is wrapped, failures print a warning.
"""

from __future__ import annotations

import os
from typing import Any


def _client():
    if os.environ.get("USE_LANGFUSE", "").lower() not in ("1", "true", "yes"):
        return None
    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        return None
    os.environ.setdefault("LANGFUSE_HOST", os.environ.get("LANGFUSE_BASE_URL", ""))
    from langfuse import get_client

    return get_client()


def emit_turn_trace(result: dict[str, Any], *, model: str) -> str | None:
    """Emit one Langfuse trace for a completed turn. Returns trace_id or None."""
    try:
        langfuse = _client()
        if langfuse is None:
            return None
        from langfuse import propagate_attributes

        cs = result["citation_stats"]
        with propagate_attributes(
            trace_name="advisor.turn", tags=["advisor", "tier1"],
            metadata={"latency_ms": str(result["latency_ms"]),
                      "over_soft_cap": str(result["over_soft_cap"]),
                      "citations_resolved": f"{cs['resolved']}/{cs['emitted']}"},
        ):
            with langfuse.start_as_current_observation(
                as_type="span", name="advisor.turn",
                input={"question": result["question"]},
            ) as root:
                for s in result["steps"]:
                    with langfuse.start_as_current_observation(
                        as_type="generation", name=f"agent.step{s['step']}", model=model,
                    ) as gen:
                        gen.update(
                            usage_details={"input": s["input_tokens"],
                                           "cache_read_input_tokens": s["cached_tokens"],
                                           "output": s["output_tokens"]},
                            cost_details={"total": s["cost_usd"]},
                            output={"tool_calls": s["tool_calls"]},
                        )
                for tc in result["tool_calls"]:
                    with langfuse.start_as_current_observation(
                        as_type="span", name=f"tool.{tc['tool']}", input=tc["arguments"],
                    ):
                        pass
                root.update(output={"answer": result["answer"],
                                    "citations": result["citations"]})
                trace_id = root.trace_id
        cs = result["citation_stats"]
        if cs["emitted"]:
            langfuse.create_score(trace_id=trace_id, name="citation_resolvability",
                                  value=cs["resolved"] / cs["emitted"])
        langfuse.create_score(trace_id=trace_id, name="cost_usd",
                              value=result["cost_usd"])
        langfuse.flush()
        return trace_id
    except Exception as exc:  # noqa: BLE001 — telemetry must never break a turn
        print(f"  [telemetry] Langfuse emission failed: {type(exc).__name__}: {exc}")
        return None


def emit_judge_scores(trace_id: str | None, judgement: dict[str, Any]) -> None:
    """Attach claim/citation judge scores to an existing turn trace.

    The judge runs offline (Gate 1B-3), so these land after the trace exists.
    `comment` carries the calibration state with every value — a faithfulness
    number read without knowing how trustworthy the judge is would be exactly
    the unverified proxy the review warned against.
    """
    try:
        langfuse = _client()
        if langfuse is None or not trace_id:
            return
        state = judgement.get("calibration", {}).get("state", "uncalibrated")
        comment = f"judge={judgement.get('judge_model')} calibration={state}"
        for name, value in (judgement.get("scores") or {}).items():
            if value is not None:
                langfuse.create_score(trace_id=trace_id, name=name,
                                      value=value, comment=comment)
        langfuse.flush()
    except Exception as exc:  # noqa: BLE001
        print(f"  [telemetry] judge score emission failed: {type(exc).__name__}: {exc}")
