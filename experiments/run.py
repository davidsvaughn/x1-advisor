"""Bake-off harness (PLAN Phase 2): golden set → retrieval metrics → JSONL manifest.

Run:  uv run python -m experiments.run --config te3s_1536_ck1 --golden v1 [--k 10]

Writes one JSONL line per question to
experiments/runs/{date}_{config}_{golden}_{sha}_r{n}.jsonl (model outputs never
truncated) plus a summary line, and prints aggregate recall@k / MRR. Manifests
are immutable — a rerun allocates the next sequence number rather than
overwriting (experiments/manifest.py). Web-required questions are skipped here
(E3 grades those). Answer-quality / judge columns land with Phase 3 (E4); this
harness grades retrieval only, which is what E1/E2 decide on.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import yaml

from experiments.manifest import code_fingerprint, git_sha, open_new_manifest
from x1_advisor.agent.bundle import QA_ARTIFACTS_DIR, export_bundle, manifest_record
from x1_advisor.agent.judge import calibration_state, judge_bundle
from x1_advisor.telemetry import emit_judge_scores
from x1_advisor.cost import JsonlSink, Tracker
from x1_advisor.db import connect
from x1_advisor.filters import FilterError
from x1_advisor.fingerprint import run_fingerprint
from x1_advisor.index import active_config
from x1_advisor.retrieval import Hit, retrieve

GOLDEN_DIR = Path(__file__).parent / "golden"


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


def run_agent_mode(questions: list[dict], limit: int, judge: bool = False) -> None:
    """Phase-4 exit measurement: agent end-to-end over golden questions.

    Grades the CITATION contract mechanically (resolved/emitted refs — the >=95%
    bar) plus cost/latency distribution. Includes a couple of web questions;
    answer-content judging is E4's job, not tonight's.
    """
    from x1_advisor.agent.advisor import run_turn

    non_web = [q for q in questions if not q.get("web_required")]
    web = [q for q in questions if q.get("web_required")]
    subset = (non_web + web[:2])[:limit]

    run_id, manifest_path, manifest_file = open_new_manifest(
        f"{dt.date.today()}_agent_v1")
    emitted = resolved = 0
    costs, latencies, no_citation, judged = [], [], [], []
    judge_tracker = Tracker(run_id=f"{run_id}:judge",
                            sink=JsonlSink(os.environ.get("ADVISOR_COST_LEDGER",
                                                          "cost_ledger.jsonl")))
    with connect() as conn, manifest_file as manifest:
        for q in subset:
            r = run_turn(conn, q["question"], acl="admin")
            cs = r["citation_stats"]
            emitted += cs["emitted"]
            resolved += cs["resolved"]
            costs.append(r["cost_usd"])
            latencies.append(r["latency_ms"])
            if cs["resolved"] == 0:
                no_citation.append(q["id"])
            if judge:
                # resolvability says a citation points somewhere real; this says
                # whether the somewhere supports the claim (Gate 1B-3).
                # Judge spend is real spend: it goes through cost.py like
                # everything else, and is reported separately from turn cost.
                verdict = judge_bundle(conn, r["bundle"], tracker=judge_tracker,
                                       calibration=calibration_state())
                r["bundle"]["scores"].update(verdict["scores"])
                r["bundle"]["judge"] = verdict
                emit_judge_scores(r.get("trace_id"), verdict)
                judged.append(verdict)
            # storage split (QA-LOOP §4.1): the complete bundle — answer text,
            # every tool result, evidence text — goes to owner-only local
            # storage; the committed manifest gets the body-free projection
            bundle_path = export_bundle(r["bundle"], name=q["id"], subdir=run_id)
            manifest.write(json.dumps({
                "run_id": run_id, "experiment": "phase4-agent",
                "git_sha": git_sha(), "code_fingerprint": code_fingerprint(),
                "question_id": q["id"], "category": q["category"],
                "bundle": bundle_path.name if bundle_path else None,
                **manifest_record(r["bundle"]),
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
    if judged:
        def mean_of(key):
            vals = [j["scores"][key] for j in judged if j["scores"][key] is not None]
            return sum(vals) / len(vals) if vals else None

        tot = {k: sum(j["counts"][k] for j in judged)
               for k in ("supported", "partial", "unsupported", "unverifiable")}
        state = judged[0]["calibration"]
        print(f"\n-- claim/citation judge ({judged[0]['judge_model']}, "
              f"calibration: {state['state']}) --")
        print(f"faithfulness      {mean_of('faithfulness'):.3f}   "
              f"(claims: {tot['supported']} supported, {tot['partial']} partial, "
              f"{tot['unsupported']} unsupported, {tot['unverifiable']} unverifiable)")
        print(f"citation coverage {mean_of('citation_coverage'):.3f}   "
              f"uncited factual claims: "
              f"{sum(j['claims']['uncited'] for j in judged)}")
        flagged = [j for j in judged if j["labels"]]
        print(f"questions with judge labels: {len(flagged)}/{len(judged)}")
        print(f"judge cost: ${judge_tracker.run_total:.4f} "
              f"(${judge_tracker.run_total / len(judged):.4f}/question, "
              "separate from turn cost above)")
    print(f"manifest (body-free): {manifest_path}")
    print(f"bundles (owner-only): {QA_ARTIFACTS_DIR / run_id}")


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
    ap.add_argument("--judge", action="store_true",
                    help="agent mode: run the claim/citation judge per question "
                         "(Gate 1B-3; adds ~$0.01/question)")
    args = ap.parse_args()

    golden = yaml.safe_load((GOLDEN_DIR / f"{args.golden}.yaml").read_text())
    questions = golden["questions"]
    if args.agent:
        run_agent_mode(questions, args.limit, judge=args.judge)
        return
    run_id, manifest_path, manifest_file = open_new_manifest(
        f"{dt.date.today()}_{args.config or 'active'}_{args.golden}"
        + ("_rerank" if args.rerank else ""))
    tracker = Tracker(run_id=run_id)

    recalls, mrrs, skipped, filter_errors = [], [], 0, []
    per_q_filter_error: list[str | None] = []
    run_fp: dict = {}
    with connect() as conn, manifest_file as manifest:
        run_fp = run_fingerprint(conn, config_id=args.config or active_config(conn).id)
        for q in questions:
            if q.get("web_required"):
                skipped += 1
                continue
            t0 = time.monotonic()
            filter_error = None
            try:
                hits = retrieve(conn, q["question"], acl="admin",
                                filters=q.get("filters"), config_id=args.config,
                                k=args.k, rerank=args.rerank, tracker=tracker)
            except FilterError as exc:
                # a golden case whose filter names a field the corpus does not
                # stamp is a ROUTING failure, not a retrieval failure. Before
                # the typed filter layer this scored a silent zero-recall and
                # was counted as a retrieval miss (g020 in the 2026-07-08
                # baseline: 0 hits, 0 recall, indistinguishable from a real
                # miss). Record it as its own class; never crash the suite.
                filter_error, hits = str(exc), []
                filter_errors.append(q["id"])
            latency_ms = int((time.monotonic() - t0) * 1000)
            g = grade(hits, q["expected"])
            recalls.append(g["recall"])
            mrrs.append(g["mrr"])
            per_q_filter_error.append(filter_error)
            manifest.write(json.dumps({
                "run_id": run_id, "experiment": "phase2-baseline",
                "config_id": args.config or "active", "git_sha": git_sha(),
                "code_fingerprint": code_fingerprint(),
                "question_id": q["id"], "category": q["category"],
                "question": q["question"], **g,
                "filter_error": filter_error,
                # body-free (QA-LOOP §4.1): identifiers and ranks, never source
                # TITLES — a title names a document, and 30 of the entries in
                # the pre-fix version of this manifest named premium reports.
                # The question text is the golden case itself, already committed
                # in experiments/golden/, so it discloses nothing new.
                "retrieved": [
                    {"document_id": h.document_id, "block_index": h.block_index,
                     "chunk_id": h.chunk_id, "page_number": h.page_number,
                     "source_type": h.source_type, "granularity": h.granularity,
                     "rrf": round(h.rrf_score, 5),
                     "dense_rank": h.dense_rank, "lex_rank": h.lex_rank}
                    for h in hits
                ],
                "latency_ms": latency_ms,
                "cost_usd": tracker.run_total,
            }) + "\n")
            flag = ("!" if filter_error else
                    "✓" if g["recall"] == 1.0 else "~" if g["matched"] else "✗")
            print(f"  {flag} {q['id']} {q['category']:13s} recall={g['recall']:.2f} "
                  f"mrr={g['mrr']:.2f} {latency_ms}ms"
                  + (f"  FILTER: {filter_error}" if filter_error else ""))

    n = len(recalls)
    # two populations, both reported. A case whose filter names a field the
    # corpus does not stamp never reached retrieval, so scoring it as a
    # retrieval miss overstates the miss rate — but dropping it silently would
    # break comparison with every manifest written before the split existed.
    valid = [(r, m) for r, m, e in zip(recalls, mrrs, per_q_filter_error) if not e]
    summary = {
        "run_id": run_id, "experiment": "phase2-baseline", "record": "summary",
        "config_id": args.config or "active", "golden": args.golden, "k": args.k,
        "code_fingerprint": code_fingerprint(),
        # the full fingerprint rides on the summary record, once: it is
        # identical across questions, and without the corpus watermark a
        # retrieval manifest cannot explain its own numbers
        "fingerprint": run_fp,
        "graded": n, "web_skipped": skipped,
        "all_cases": {"n": n, "recall": sum(recalls) / n, "mrr": sum(mrrs) / n,
                      "full_recall": sum(1 for r in recalls if r == 1.0),
                      "zero_recall": sum(1 for r in recalls if r == 0.0)},
        "route_valid_only": {
            "n": len(valid),
            "recall": (sum(r for r, _ in valid) / len(valid)) if valid else None,
            "mrr": (sum(m for _, m in valid) / len(valid)) if valid else None},
        "filter_errors": filter_errors,
        "cost_usd": round(tracker.run_total, 6),
    }
    with manifest_path.open("a") as fh:
        fh.write(json.dumps(summary) + "\n")

    print(f"\n== {run_id} ==")
    print(f"questions graded: {n} (web skipped: {skipped})")
    print(f"ALL CASES        recall@{args.k} {sum(recalls)/n:.3f}  MRR {sum(mrrs)/n:.3f}"
          "   ← comparable with pre-2026-07-30 manifests")
    if valid and len(valid) != n:
        print(f"ROUTE-VALID ONLY recall@{args.k} {sum(r for r, _ in valid)/len(valid):.3f}"
              f"  MRR {sum(m for _, m in valid)/len(valid):.3f}"
              f"   ← excludes {len(filter_errors)} filter/routing failure(s)")
    print(f"full recall:     {sum(1 for r in recalls if r == 1.0)}/{n}")
    print(f"zero recall:     {sum(1 for r in recalls if r == 0.0)}/{n}")
    print(f"filter errors:   {filter_errors or 'none'}"
          "  (routing failures, not retrieval misses; counted in ALL CASES only)")
    print(f"total cost:      ${tracker.run_total:.4f}")
    print(f"manifest:        {manifest_path}")
    sys.exit(0)


if __name__ == "__main__":
    main()
