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
    # every trace carries the code identity that produced it (1D-6, David's
    # ask): `release` is the SDK's first-class field for exactly this, read
    # at client init — `71b13c0` or `71b13c0+dirty`
    from x1_advisor.fingerprint import code_fingerprint

    os.environ.setdefault("LANGFUSE_RELEASE", code_fingerprint())
    from langfuse import get_client

    return get_client()


def emit_turn_trace(result: dict[str, Any], *, model: str,
                    fingerprint: dict[str, Any] | None = None) -> str | None:
    """Emit one Langfuse trace for a completed turn. Returns trace_id or None.

    `fingerprint` is the turn fingerprint (fingerprint.py): the trace metadata
    carries enough of it to answer "what version of everything produced this
    trace" from the Langfuse UI alone — git sha rides separately as the
    trace's `release`. Bundles remain the complete record.
    """
    try:
        langfuse = _client()
        if langfuse is None:
            return None
        from langfuse import propagate_attributes

        fp = fingerprint or {}
        corpus = fp.get("corpus_watermark") or {}
        fp_meta = {k: str(v) for k, v in {
            "git_sha": fp.get("git_sha"),
            "worktree_dirty": fp.get("worktree_dirty"),
            "prompt_sha": (fp.get("prompt_sha256") or "")[:12] or None,
            "tool_schema_sha": (fp.get("tool_schema_sha256") or "")[:12] or None,
            "agent_model_resolved": fp.get("agent_model_resolved"),
            "config_id": fp.get("config_id"),
            "corpus_chunk_digest": (corpus.get("chunk_digest") or "")[:12] or None,
            "corpus_embedding_digest": (corpus.get("embedding_digest") or "")[:12] or None,
        }.items() if v is not None}

        cs = result["citation_stats"]
        with propagate_attributes(
            trace_name="advisor.turn", tags=["advisor", "tier1"],
            metadata={"latency_ms": str(result["latency_ms"]),
                      "over_soft_cap": str(result["over_soft_cap"]),
                      "citations_resolved": f"{cs['resolved']}/{cs['emitted']}",
                      **fp_meta},
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
        # no flush() here: it forced a synchronous network round-trip inside
        # the turn (~1.5s measured, thread-022). The SDK's background worker
        # delivers batches on its own interval and at exit; emit_judge_scores
        # (offline, short-lived processes) keeps its explicit flush.
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
