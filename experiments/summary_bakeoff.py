"""E4b (record-summary role): gpt-5-mini vs gpt-5.4-nano vs gpt-5.6-luna.

The working model (gpt-5-mini, DECISIONS 2026-07-09) was a provisional pick —
feature-level evidence only, no competing model ever tried. This runs the
designed E4b comparison for ONE role: ~20 stratified live documents, each
summarized by every candidate through the EXACT production path
(summaries.py prompts, window split, reasoning_effort=minimal), then:

* deterministic check: does the summary name the entity the title names?
  (entity identity is the whole reason record summaries exist);
* blind judge: gpt-5.6-terra (neutral — not a candidate) scores the three
  summaries per document on identity / groundedness / retrieval-handle,
  presented in per-document randomized order as A/B/C.

Committed manifest rows are body-free (scores, costs, booleans); the
summary texts land in owner-only .qa-artifacts. Winner (if any) still needs
David + a DECISIONS entry before MODEL in summaries.py changes.

Run:  uv run python -m experiments.summary_bakeoff [--docs 20] [--seed e4b]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

from experiments.manifest import code_fingerprint, git_sha, open_new_manifest
from x1_advisor.agent.bundle import QA_ARTIFACTS_DIR
from x1_advisor.cost import Tracker, Usage
from x1_advisor.db import connect
from x1_advisor.ingest.summaries import (MAP_PROMPT, REDUCE_PROMPT,
                                         SINGLE_PROMPT, split_windows)

CANDIDATES = ("gpt-5-mini", "gpt-5.4-nano", "gpt-5.6-luna")
JUDGE_MODEL = "gpt-5.6-terra"
JUDGE_DOC_CHARS = 16_000

JUDGE_PROMPT = """\
Three search-index summaries (A, B, C) were written for the DOCUMENT below.
Score each 1-5 on:
- identity: does it name the entity and document kind a searcher needs?
- groundedness: is every stated fact supported by the shown document text?
  (The excerpt may be truncated — penalize contradictions or suspicious
  specifics absent from the text, not mere omission.)
- handle: would this summary make the document findable for the questions
  it can answer (load-bearing facts, numbers, conclusions)?

Respond ONLY with JSON, no prose:
{"A": {"identity": n, "groundedness": n, "handle": n},
 "B": {...}, "C": {...}, "best": "A|B|C", "reason": "one sentence"}

TITLE: {title}
KIND: {kind}

DOCUMENT (may be truncated):
---
{body}
---

SUMMARY A:
{a}

SUMMARY B:
{b}

SUMMARY C:
{c}
"""


def entity_from_title(title: str) -> str | None:
    """Titles are '<Entity> — <Kind>'; the entity is the identity target."""
    head = (title or "").split(" — ")[0].strip()
    return head or None


# per-model lowest-effort tier: gpt-5-mini's API generation calls it
# "minimal"; the 5.4/5.6 generations renamed it "none". Resolved on first
# 400 and cached, so every candidate runs at its own floor (production
# parity: summaries need no deliberation).
_EFFORT: dict[str, str] = {}


def summarize(client: OpenAI, tracker: Tracker, model: str, row: dict) -> dict:
    body = row["markdown"] or ""
    windows = split_windows(body)
    fmt = {"title": row["title"], "kind": row["source_type"]}
    t0 = time.monotonic()

    def ask(prompt: str) -> str:
        from openai import BadRequestError
        for effort in (_EFFORT.get(model, "minimal"), "none"):
            try:
                resp = client.chat.completions.create(
                    model=model, reasoning_effort=effort,
                    messages=[{"role": "user", "content": prompt}])
                _EFFORT[model] = effort
                break
            except BadRequestError as exc:
                if "reasoning_effort" not in str(exc):
                    raise
        tracker.log(provider="openai", model=model, stage="e4b.summary",
                    usage=Usage.from_haystack_meta("openai", resp.usage.model_dump()))
        return (resp.choices[0].message.content or "").strip()

    if len(windows) == 1:
        text = ask(SINGLE_PROMPT.format(body=windows[0], **fmt))
    else:
        parts = [ask(MAP_PROMPT.format(body=w, i=i, n=len(windows), **fmt))
                 for i, w in enumerate(windows, 1)]
        text = ask(REDUCE_PROMPT.format(
            parts="\n".join(f"PART {i}: {p}" for i, p in enumerate(parts, 1)), **fmt))
    return {"model": model, "text": text, "windows": len(windows),
            "latency_s": round(time.monotonic() - t0, 2)}


def judge(client: OpenAI, tracker: Tracker, row: dict,
          labeled: dict[str, str]) -> dict:
    prompt = (JUDGE_PROMPT
              .replace("{title}", row["title"] or "")
              .replace("{kind}", row["source_type"])
              .replace("{body}", (row["markdown"] or "")[:JUDGE_DOC_CHARS])
              .replace("{a}", labeled["A"]).replace("{b}", labeled["B"])
              .replace("{c}", labeled["C"]))
    for attempt in range(2):
        resp = client.chat.completions.create(
            model=JUDGE_MODEL,
            messages=[{"role": "user", "content": prompt}])
        tracker.log(provider="openai", model=JUDGE_MODEL, stage="e4b.judge",
                    usage=Usage.from_haystack_meta("openai", resp.usage.model_dump()))
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw)
        try:
            out = json.loads(raw)
            if all(k in out for k in ("A", "B", "C", "best")):
                return out
        except ValueError:
            pass
        prompt += "\n\nYour previous output failed to parse. Output ONLY the JSON object."
    return {}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--docs", type=int, default=20)
    ap.add_argument("--seed", default="e4b")
    args = ap.parse_args()
    rng = random.Random(args.seed)

    with connect() as conn:
        rows = conn.execute(
            """SELECT id, title, source_type, markdown FROM advisor.documents
               WHERE superseded_by IS NULL AND length(markdown) > 200
               ORDER BY id""").fetchall()
    # stratified: spread across source types, include long (map/reduce) docs
    by_type: dict[str, list] = defaultdict(list)
    for r in rows:
        by_type[r["source_type"]].append(r)
    sample: list[dict] = []
    types = sorted(by_type)
    while len(sample) < min(args.docs, len(rows)):
        for t in types:
            if by_type[t] and len(sample) < args.docs:
                sample.append(by_type[t].pop(rng.randrange(len(by_type[t]))))
    long_docs = sum(1 for r in sample if len(r["markdown"]) > 12_000)
    print(f"{len(sample)} docs sampled across {len(types)} source types "
          f"({long_docs} map/reduce)")

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    run_id, manifest_path, manifest_file = open_new_manifest(
        f"{dt.date.today()}_e4b_summaries")
    tracker = Tracker(run_id=run_id)
    started_sha = git_sha()

    def one_doc(row: dict) -> dict:
        outs = {}
        for m in CANDIDATES:
            outs[m] = summarize(client, tracker, m, row)
        order = list(CANDIDATES)
        rng2 = random.Random(f"{args.seed}:{row['id']}")
        rng2.shuffle(order)
        labels = dict(zip("ABC", order))          # label -> model
        verdict = judge(client, tracker, row,
                        {lb: outs[m]["text"] for lb, m in labels.items()})
        entity = entity_from_title(row["title"])
        return {"row": row, "outs": outs, "labels": labels,
                "verdict": verdict, "entity": entity}

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(one_doc, sample))

    agg = {m: defaultdict(float) for m in CANDIDATES}
    counts = {m: defaultdict(int) for m in CANDIDATES}
    artifact = []
    with manifest_file as manifest:
        for res in results:
            row, outs, labels = res["row"], res["outs"], res["labels"]
            verdict, entity = res["verdict"], res["entity"]
            model_of = {m: lb for lb, m in labels.items()}
            for m in CANDIDATES:
                named = bool(entity and entity.lower() in outs[m]["text"].lower())
                scores = verdict.get(model_of[m], {}) if verdict else {}
                best = bool(verdict) and labels.get(verdict.get("best")) == m
                rec = {"run_id": run_id, "experiment": "E4b-summaries",
                       "git_sha": started_sha, "doc_id": row["id"],
                       "source_type": row["source_type"], "model": m,
                       "windows": outs[m]["windows"],
                       "latency_s": outs[m]["latency_s"],
                       "entity_named": named,
                       "judge": {k: scores.get(k) for k in
                                 ("identity", "groundedness", "handle")},
                       "judge_best": best}
                manifest.write(json.dumps(rec) + "\n")
                counts[m]["n"] += 1
                counts[m]["entity_named"] += named
                counts[m]["best"] += best
                for k in ("identity", "groundedness", "handle"):
                    if scores.get(k) is not None:
                        agg[m][k] += scores[k]
                        counts[m][f"{k}_n"] += 1
            artifact.append({"doc_id": row["id"], "title": row["title"],
                             "labels": labels, "verdict": verdict,
                             "summaries": {m: outs[m]["text"] for m in CANDIDATES}})
        manifest.write(json.dumps({
            "record": "summary", "run_id": run_id, "git_sha": started_sha,
            "code_fingerprint": code_fingerprint(), "seed": args.seed,
            "judge_model": JUDGE_MODEL, "docs": len(sample)}) + "\n")

    art_dir = QA_ARTIFACTS_DIR / run_id
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "summaries_and_verdicts.json").write_text(
        json.dumps(artifact, indent=2))

    by_model_cost: dict[str, float] = defaultdict(float)
    for rec in tracker.records:
        by_model_cost[rec.model] += rec.cost_usd
    print(f"\n== {run_id} ==")
    print(f"{'model':<14} {'best':>5} {'named':>6} {'ident':>6} "
          f"{'ground':>7} {'handle':>7} {'$/doc':>8} {'s/doc':>6}")
    lat = {m: [] for m in CANDIDATES}
    for res in results:
        for m in CANDIDATES:
            lat[m].append(res["outs"][m]["latency_s"])
    for m in CANDIDATES:
        c = counts[m]
        mean = lambda k: (agg[m][k] / c[f"{k}_n"]) if c[f"{k}_n"] else float("nan")
        print(f"{m:<14} {c['best']:>3}/{c['n']:<2} "
              f"{c['entity_named']:>3}/{c['n']:<2} "
              f"{mean('identity'):>6.2f} {mean('groundedness'):>7.2f} "
              f"{mean('handle'):>7.2f} "
              f"{by_model_cost.get(m, 0) / max(c['n'], 1):>8.5f} "
              f"{sum(lat[m]) / len(lat[m]):>6.1f}")
    print(f"judge cost: ${by_model_cost.get(JUDGE_MODEL, 0):.4f}   "
          f"total: ${tracker.run_total:.4f}")
    print(f"manifest: {manifest_path}")
    print(f"texts + verdicts: {art_dir / 'summaries_and_verdicts.json'}")


if __name__ == "__main__":
    main()
