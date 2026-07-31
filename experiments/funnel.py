"""Funnel classifier (Gate 1C, route-aware since Gate 1D-2) — *where* did this
question fail?

A pass/fail per question tells a teacher nothing actionable. This localizes the
failure to a stage by computing four sets and finding the first one that lost
the evidence:

    E — evidence the golden case says would answer it
    R — everything any retrieval leg surfaced      (retrieval_explain)
    S — evidence actually shown to the model       (tool results)
    C — evidence cited in the validated answer     (validation)

E ⊄ R is a retrieval problem. E ⊆ R but E ⊄ S is a ranking/cap problem. E ⊆ S
but E ⊄ C means the model was handed the answer and did not use it. Each of
those is a different fix, and before this they all looked like "recall is low".

**Labels are route-aware.** The agent has three evidence routes — corpus
search, structured platform queries, live web — and golden v1's matchers only
describe the corpus one. The first version of this classifier judged every
question by the corpus funnel alone, so a question correctly answered by a
structured query with a valid platform citation was labelled `retrieval_miss`
(g020: 22 rows, proper citation, called a miss — a classifier inventing a
failure mode, again). Now: when expected corpus evidence never surfaced but a
structured/web route produced *cited* evidence, that is recorded as the note
`route_substituted:<route>`, not a failure — golden v1 cannot express
route-equivalent evidence; Gate 4's golden v2 encodes it properly as
acceptable-evidence groups.

Labels are assigned in funnel order and are **not collapsed** — an earlier-stage
label does not hide a later-stage one, because a question can fail twice and
fixing only the first failure would look like progress while the answer stays
wrong (QA-LOOP-DESIGN §4.3).

Run: uv run python -m experiments.funnel <run-directory-name>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from x1_advisor.agent.bundle import QA_ARTIFACTS_DIR
from x1_advisor.db import connect

GOLDEN_DIR = Path(__file__).parent / "golden"


def chunk_facts(conn, chunk_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Metadata for chunk ids so golden matchers can be evaluated against them."""
    if not chunk_ids:
        return {}
    rows = conn.execute(
        """SELECT c.id, c.document_id, c.block_index, c.metadata, c.granularity,
                  d.title, d.source_type
           FROM advisor.doc_chunks c JOIN advisor.documents d ON d.id = c.document_id
           WHERE c.id = ANY(%s)""", (list(chunk_ids),)).fetchall()
    return {r["id"]: dict(r) for r in rows}


def matches(fact: dict, matcher: dict) -> bool:
    m = fact.get("metadata") or {}
    checks = {
        "source_type": lambda v: fact.get("source_type") == v,
        "title_contains": lambda v: v.lower() in (fact.get("title") or "").lower(),
        "company_name": lambda v: m.get("company_name") == v,
        "section_key": lambda v: m.get("section_key") == v,
        "entity_type": lambda v: m.get("entity_type") == v,
    }
    return all(key in checks and checks[key](value) for key, value in matcher.items())


def tool_results(bundle: dict) -> list[dict[str, Any]]:
    """(tool, arguments, parsed payload) for every tool result in the turn.
    The serialized message's `origin` says which tool produced a result — shape
    detection on the payload alone cannot attribute error results."""
    out = []
    for m in bundle.get("messages") or []:
        for c in m.get("content") or []:
            tcr = c.get("tool_call_result")
            if not tcr:
                continue
            origin = tcr.get("origin") or {}
            try:
                payload = json.loads(tcr.get("result") or "")
            except (TypeError, ValueError):
                payload = None
            out.append({"tool": origin.get("tool_name"),
                        "arguments": origin.get("arguments") or {},
                        "payload": payload if isinstance(payload, dict) else None})
    return out


def classify(conn, bundle: dict, question: dict) -> dict[str, Any]:
    """One question → failure labels + informational notes + the sets behind them."""
    explains = bundle.get("retrieval_explain") or []
    expected = question.get("expected") or []
    calls = tool_results(bundle)
    structured = [c for c in calls if c["tool"] == "structured_query"]
    web_calls = [c for c in calls if c["tool"] == "web_research"]

    citations = bundle.get("validation", {}).get("citations", [])
    # C: cited evidence. Citations carry (document_id, block_index), not chunk
    # ids, so join on that pair — the chunk's own columns, never its metadata,
    # which does not contain document_id at all.
    cited_pairs = {(c.get("document_id"), c.get("block_index"))
                   for c in citations if c.get("type") == "internal"}
    platform_cited = any(c.get("type") == "platform_data" for c in citations)
    web_cited = any(c.get("type") == "web" for c in citations)

    # R: everything any leg surfaced, across every corpus search this turn
    r_ids = {f["chunk_id"] for e in explains for f in e["fused"]}
    # S: what actually came back to the model
    s_ids = {cid for e in explains for cid in e["returned"]}
    facts = chunk_facts(conn, sorted(r_ids | s_ids))
    cited_chunks = {cid for cid, f in facts.items()
                    if (f["document_id"], f["block_index"]) in cited_pairs}

    per_matcher = []
    for matcher in expected:
        in_r = {cid for cid in r_ids if matches(facts.get(cid, {}), matcher)}
        in_s = {cid for cid in s_ids if matches(facts.get(cid, {}), matcher)}
        in_c = in_s & cited_chunks
        per_matcher.append({"matcher": matcher, "in_R": len(in_r),
                            "in_S": len(in_s), "in_C": len(in_c)})

    # the structured route succeeded only if it returned rows AND those rows
    # were actually cited — rows the answer never used substitute for nothing
    structured_rows = sum((c["payload"] or {}).get("row_count") or 0
                          for c in structured)
    structured_errors = sum(1 for c in structured
                            if (c["payload"] or {}).get("error"))
    structured_ok = structured_rows > 0 and platform_cited

    labels: list[str] = []
    notes: list[str] = []
    # --- mechanical stages, in funnel order, per route ---------------------
    if any(e.get("filter_notes") for e in explains):
        labels.append("routing_error")          # filters matched no known value
    if any(m["in_R"] == 0 for m in per_matcher):
        # expected corpus evidence never surfaced. Whether that is a FAILURE
        # depends on the other routes: cited structured/web evidence is a
        # legitimate substitute golden v1 just cannot express (route-awareness,
        # Gate 1D-2 — g020 was called a retrieval_miss for correctly using
        # structured_query).
        if structured_ok or web_cited:
            notes.append("route_substituted:"
                         + ("structured" if structured_ok else "web"))
        elif not explains:
            labels.append("no_evidence_gathered")   # never even searched
        else:
            labels.append("retrieval_miss")
    if any(m["in_R"] > 0 and m["in_S"] == 0 for m in per_matcher):
        labels.append("ranking_drop")
    if any(m["in_S"] > 0 and m["in_C"] == 0 for m in per_matcher):
        labels.append("evidence_unused")
    if structured_errors:
        labels.append("structured_query_error")
    if not explains and structured_rows > 0 and not platform_cited and expected:
        # the structured route was the only evidence gathered and its rows
        # went uncited — the structured-route twin of evidence_unused
        labels.append("evidence_unused")
    if bundle.get("validation", {}).get("dropped"):
        labels.append("validation_drop")
    if any("(wrapup)" in (s.get("tool_calls") or []) for s in bundle.get("steps", [])):
        labels.append("step_cap")
    if bundle.get("summary", {}).get("verdict") == "error":
        labels.append("runtime_error")
    # --- judge-assisted stages (present only when the judge ran) ----------
    labels += (bundle.get("judge") or {}).get("labels", [])

    return {"question_id": question["id"], "category": question.get("category"),
            "pass": not labels, "labels": sorted(set(labels)), "notes": notes,
            "routes": {"corpus": len(explains), "structured": len(structured),
                       "web": len(web_calls)},
            "matchers": per_matcher,
            "sets": {"R": len(r_ids), "S": len(s_ids), "C": len(cited_pairs)}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run", help="run directory under .qa-artifacts/runs/")
    ap.add_argument("--golden", default="v1")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    golden = yaml.safe_load((GOLDEN_DIR / f"{args.golden}.yaml").read_text())
    by_id = {q["id"]: q for q in golden["questions"]}
    run_dir = QA_ARTIFACTS_DIR / args.run
    if not run_dir.is_dir():
        sys.exit(f"no such run directory: {run_dir}")

    rows = []
    with connect() as conn:
        for path in sorted(run_dir.glob("*.json")):
            question = by_id.get(path.stem)
            if not question:
                continue
            rows.append(classify(conn, json.loads(path.read_text()), question))

    if args.json:
        print(json.dumps(rows, indent=2))
        return
    print(f"{'qid':<7} {'verdict':<7} labels / notes")
    for r in rows:
        extra = "".join(f"  ({n})" for n in r.get("notes", []))
        print(f"{r['question_id']:<7} {'PASS' if r['pass'] else 'FAIL':<7} "
              f"{', '.join(r['labels']) or '-'}{extra}")
    counts: dict[str, int] = {}
    for r in rows:
        for label in r["labels"]:
            counts[label] = counts.get(label, 0) + 1
    print(f"\n{sum(1 for r in rows if r['pass'])}/{len(rows)} clean")
    for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {label:<24} {n}")


if __name__ == "__main__":
    main()
