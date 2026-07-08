"""Bake-off harness (PLAN Phase 2): golden set → retrieval metrics → JSONL manifest.

Run:  uv run python -m experiments.run --config te3s_1536_ck1 --golden v1 [--k 10]

Writes one JSONL line per question to experiments/runs/{date}_{config}_{golden}.jsonl
(model outputs never truncated) plus a summary line, and prints aggregate
recall@k / MRR. Web-required questions are skipped here (E3 grades those).
Answer-quality / judge columns land with Phase 3 (E4); this harness grades
retrieval only, which is what E1/E2 decide on.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml

from x1_advisor.cost import Tracker
from x1_advisor.db import connect
from x1_advisor.retrieval import Hit, retrieve

GOLDEN_DIR = Path(__file__).parent / "golden"
RUNS_DIR = Path(__file__).parent / "runs"


def hit_matches(hit: Hit, matcher: dict) -> bool:
    m = hit.metadata or {}
    checks = {
        "source_type": lambda v: hit.source_type == v,
        "title_contains": lambda v: v.lower() in (hit.title or "").lower(),
        "company_name": lambda v: m.get("company_name") == v,
        "section_key": lambda v: m.get("section_key") == v,
        "entity_type": lambda v: m.get("entity_type") == v,
    }
    return all(checks[key](value) for key, value in matcher.items())


def grade(hits: list[Hit], expected: list[dict]) -> dict:
    matched, first_rank = 0, None
    for matcher in expected:
        rank = next((i for i, h in enumerate(hits, 1) if hit_matches(h, matcher)), None)
        if rank:
            matched += 1
            first_rank = min(first_rank or rank, rank)
    return {
        "expected": len(expected),
        "matched": matched,
        "recall": matched / len(expected) if expected else None,
        "mrr": (1.0 / first_rank) if first_rank else 0.0,
    }


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5,
                              cwd=Path(__file__).parent).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def run_agent_mode(questions: list[dict], limit: int) -> None:
    """Phase-4 exit measurement: agent end-to-end over golden questions.

    Grades the CITATION contract mechanically (resolved/emitted refs — the >=95%
    bar) plus cost/latency distribution. Includes a couple of web questions;
    answer-content judging is E4's job, not tonight's.
    """
    from x1_advisor.agent.advisor import run_turn

    non_web = [q for q in questions if not q.get("web_required")]
    web = [q for q in questions if q.get("web_required")]
    subset = (non_web + web[:2])[:limit]

    run_id = f"{dt.date.today()}_agent_v1"
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = RUNS_DIR / f"{run_id}.jsonl"
    emitted = resolved = 0
    costs, latencies, no_citation = [], [], []
    with connect() as conn, open(manifest_path, "w") as manifest:
        for q in subset:
            r = run_turn(conn, q["question"], acl="admin")
            cs = r["citation_stats"]
            emitted += cs["emitted"]
            resolved += cs["resolved"]
            costs.append(r["cost_usd"])
            latencies.append(r["latency_ms"])
            if cs["resolved"] == 0:
                no_citation.append(q["id"])
            manifest.write(json.dumps({
                "run_id": run_id, "experiment": "phase4-agent",
                "git_sha": git_sha(), "question_id": q["id"],
                "category": q["category"], **r,
            }, default=str) + "\n")
            print(f"  {q['id']} {q['category']:13s} cite {cs['resolved']}/{cs['emitted']}"
                  f"{' DROPPED:' + ','.join(cs['dropped']) if cs['dropped'] else ''}"
                  f" ${r['cost_usd']:.4f} {r['latency_ms']}ms"
                  f" steps={len(r['steps'])}"
                  f"{' ⚠CAP' if r['over_soft_cap'] else ''}")

    n = len(subset)
    costs.sort(); latencies.sort()
    print(f"\n== {run_id} ==")
    print(f"questions: {n} | citation resolvability: {resolved}/{emitted} "
          f"({100 * resolved / emitted if emitted else 0:.1f}%) — bar is >=95%")
    print(f"zero-citation answers: {no_citation or 'none'}")
    print(f"cost/turn: mean ${sum(costs)/n:.4f}, p50 ${costs[n//2]:.4f}, "
          f"max ${costs[-1]:.4f}, total ${sum(costs):.4f}")
    print(f"latency: p50 {latencies[n//2]}ms, max {latencies[-1]}ms")
    print(f"manifest: {manifest_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None, help="index config id (default: active)")
    ap.add_argument("--golden", default="v1")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--agent", action="store_true",
                    help="run the full agent per question (Phase-4 exit measurement)")
    ap.add_argument("--limit", type=int, default=20, help="agent mode: max questions")
    ap.add_argument("--rerank", action="store_true",
                    help="E2: Jina rerank blend over the fused candidate pool")
    args = ap.parse_args()

    golden = yaml.safe_load((GOLDEN_DIR / f"{args.golden}.yaml").read_text())
    questions = golden["questions"]
    if args.agent:
        run_agent_mode(questions, args.limit)
        return
    run_id = (f"{dt.date.today()}_{args.config or 'active'}_{args.golden}"
              + ("_rerank" if args.rerank else ""))
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = RUNS_DIR / f"{run_id}.jsonl"
    tracker = Tracker(run_id=run_id)

    recalls, mrrs, skipped = [], [], 0
    with connect() as conn, open(manifest_path, "w") as manifest:
        for q in questions:
            if q.get("web_required"):
                skipped += 1
                continue
            t0 = time.monotonic()
            hits = retrieve(conn, q["question"], acl="admin",
                            filters=q.get("filters"), config_id=args.config,
                            k=args.k, rerank=args.rerank, tracker=tracker)
            latency_ms = int((time.monotonic() - t0) * 1000)
            g = grade(hits, q["expected"])
            recalls.append(g["recall"])
            mrrs.append(g["mrr"])
            manifest.write(json.dumps({
                "run_id": run_id, "experiment": "phase2-baseline",
                "config_id": args.config or "active", "git_sha": git_sha(),
                "question_id": q["id"], "category": q["category"],
                "question": q["question"], **g,
                "retrieved": [
                    {"document_id": h.document_id, "block_index": h.block_index,
                     "page_number": h.page_number, "source_type": h.source_type,
                     "title": h.title, "rrf": round(h.rrf_score, 5),
                     "dense_rank": h.dense_rank, "lex_rank": h.lex_rank}
                    for h in hits
                ],
                "latency_ms": latency_ms,
                "cost_usd": tracker.run_total,
            }) + "\n")
            flag = "✓" if g["recall"] == 1.0 else ("~" if g["matched"] else "✗")
            print(f"  {flag} {q['id']} {q['category']:13s} recall={g['recall']:.2f} "
                  f"mrr={g['mrr']:.2f} {latency_ms}ms")

    n = len(recalls)
    print(f"\n== {run_id} ==")
    print(f"questions graded: {n} (web skipped: {skipped})")
    print(f"mean recall@{args.k}: {sum(recalls)/n:.3f}")
    print(f"mean MRR:        {sum(mrrs)/n:.3f}")
    print(f"full recall:     {sum(1 for r in recalls if r == 1.0)}/{n}")
    print(f"zero recall:     {sum(1 for r in recalls if r == 0.0)}/{n}")
    print(f"total cost:      ${tracker.run_total:.4f}")
    print(f"manifest:        {manifest_path}")
    sys.exit(0)


if __name__ == "__main__":
    main()
