"""Calibrate the claim/citation judge (Gate 1B-3; blind flow since Gate 1D-4).

"An LLM judge is appropriate for scale, but calibrate it against a small
human-labeled claim/citation set. Do not turn the judge's own score into another
unverified proxy." — ARCHITECTURE-PLAN-REVIEW-2026-07-30

Two label provenances, and the distinction is the whole point:

* **synthetic** — known-answer cases built by objective mutation (a number
  changed, an entity swapped, a hedge hardened into a claim). Ground truth is
  mechanical, so these catch a judge that is broken: one that rubber-stamps
  everything, or that leaks outside knowledge. They are *not* evidence that the
  judge agrees with a human on real, ambiguous text.
* **human** — real (claim, evidence) pairs sampled from actual turns and labeled
  by a person. Only these support a claim of agreement on the real distribution.

Storage split (Gate 1D-4 — the review caught both halves done wrong):

* `experiments/judge_calibration.jsonl` (tracked) — labels ONLY: id, label,
  provenance, stratum, note. Evidence bodies are never committed; the first
  version shipped 72k chars of entitled corpus text to the repo.
* `.qa-artifacts/calibration/items.jsonl` (untracked, owner-only) — the bodies:
  id → claim, evidence, stratum, locator. What the scorer joins against.
* `.qa-artifacts/calibration/pending.jsonl` (untracked) — the labeling view:
  id, claim, evidence, label. **Nothing else.** The first version wrote
  `"stratum": <the judge's verdict>` into the file it claimed was blind —
  handing the labeler the answer being withheld. Strata now stay in
  items.jsonl and are joined back by id at ingest, after the label exists.
  Row *order* is blinded too (`blind_order`): the stratum rotation leaked
  through line position, which the field-level fix did not touch.

Workflow:
  uv run python -m experiments.judge_calibrate --sample 32 --run <run_id>
  # edit .qa-artifacts/calibration/pending.jsonl: set "label" on each line
  uv run python -m experiments.judge_calibrate --ingest
  uv run python -m experiments.judge_calibrate                # score agreement
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import OpenAI

from x1_advisor.agent.judge import (CALIBRATION_SET, ENTAILMENT_PROMPT,
                                    EVIDENCE_CHARS, JUDGE_MODEL, LABELS,
                                    MIN_HUMAN_LABELS, Entailment, _ask,
                                    calibration_state, load_calibration_set)
from x1_advisor.agent.bundle import QA_ARTIFACTS_DIR
from x1_advisor.cost import Tracker

# the judge owns the set path and the trust rules; this module only measures
SET_PATH = CALIBRATION_SET
load = load_calibration_set

CALIBRATION_DIR = QA_ARTIFACTS_DIR.parent / "calibration"
ITEMS_PATH = CALIBRATION_DIR / "items.jsonl"      # bodies + strata (machine)
PENDING_PATH = CALIBRATION_DIR / "pending.jsonl"  # blind labeling view (human)
# diversity guard (1E-5): a labeled set drawn mostly from one turn calibrates
# almost nothing, whatever its size
PER_QUESTION_CAP = 2


def blind_order(items: list[dict]) -> list[dict]:
    """Order a batch so row position carries no information about the stratum.

    Blindness is a property of the whole FILE, not just its fields. The 1D-4
    fix removed the judge's verdict from pending.jsonl, but `take()` emits one
    item per stratum in a fixed rotation, so line position still spelled it
    out: index % 3 == 0 was `unsupported`, 1 `partial`, 2 `supported`, for all
    32 rows. A labeler who noticed the cycle would have been anchored by the
    exact value the blind file exists to withhold.

    Sorted by a hash of the id: deterministic (no seed, reproducible across
    runs and machines) and uncorrelated with the stratum.
    """
    import hashlib
    return sorted(items,
                  key=lambda it: hashlib.sha256(it["id"].encode()).hexdigest())


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()]


def _append_jsonl(path: Path, records: list[dict]) -> None:
    CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CALIBRATION_DIR, 0o700)
    with path.open("a") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    os.chmod(path, 0o600)


def cohens_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Agreement corrected for chance — raw accuracy flatters a skewed set."""
    n = len(pairs)
    if not n:
        return None
    observed = sum(1 for a, b in pairs if a == b) / n
    ga, gb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum((ga[k] / n) * (gb[k] / n) for k in set(ga) | set(gb))
    return None if expected == 1 else (observed - expected) / (1 - expected)


def sample_pairs(limit: int, run: str | None = None) -> int:
    """Draw unlabeled real (claim, evidence) pairs for a human labelling pass.

    Claims come from the judge's own inventory in exported bundles, and the
    evidence shown to the labeler is built EXACTLY the way the judge's
    entailment prompt builds its SOURCE — same per-citation clipping, same
    joining — so human and judge grade the same text.

    **Stratified across the judge's verdicts, and blind.** Random sampling
    would be almost all `supported`, which makes kappa unstable and teaches
    nothing about the boundary that actually matters. The stratum is recorded
    in items.jsonl (machine side) and joined back at --ingest; the pending
    file the human reads carries no verdict-derived field at all.

    **And diverse** (1E-5): the first pass drew 23 of 32 items from g001 with
    only nine unique evidence payloads, because it drained sorted bundles in
    file order — 30 labels from one question's marketing page would have
    triggered "human-calibrated" while calibrating almost nothing. Now: only
    snapshot-judged bundles are eligible, at most PER_QUESTION_CAP items per
    question, and duplicate evidence payloads are skipped while distinct ones
    remain.
    """
    import hashlib

    from x1_advisor.agent.judge import evidence_texts
    from x1_advisor.db import connect

    existing = ({i["id"] for i in load()}
                | {i["id"] for i in _load_jsonl(ITEMS_PATH)})
    runs_dir = QA_ARTIFACTS_DIR / run if run else QA_ARTIFACTS_DIR
    buckets: dict[str, list[dict]] = {"supported": [], "partial": [],
                                      "unsupported": [], "unverifiable": []}
    with connect() as conn:
        for path in sorted(runs_dir.rglob("*.json")):
            bundle = json.loads(path.read_text())
            judge = bundle.get("judge") or {}
            verdicts = judge.get("verdicts") or []
            # legacy-judged pairs would calibrate the judge on evidence the
            # model never saw — snapshot bundles only, enforced here rather
            # than left to operator discipline
            if not verdicts or judge.get("evidence_provenance") != "turn-snapshot":
                continue
            sources = evidence_texts(conn, bundle)
            for k, v in enumerate(verdicts):
                text = "\n\n".join(
                    f"[{n}] {sources[n]['text'][:EVIDENCE_CHARS]}"
                    for n in v["citations"]
                    if sources.get(n) and sources[n]["text"])
                # Run-qualified: `g001_v19` names a DIFFERENT (claim, evidence)
                # pair in every run, so the bare form collided across runs.
                # Nothing was corrupted — the `existing` check simply skipped
                # the new pair as already-seen, quietly shrinking each later
                # draw and biasing it away from questions labeled before.
                # Legacy bare ids stay as they are; labels reference them.
                item_id = f"{path.parent.name}:{path.stem}_v{k}"
                if not text or item_id in existing:
                    continue
                buckets.setdefault(v["verdict"], []).append({
                    "id": item_id, "question": path.stem,
                    "claim": v["claim"], "evidence": text,
                    "stratum": v["verdict"],
                    "locator": ", ".join(v["locators"]) or path.stem,
                })

    # interleave each stratum by question so no single turn dominates
    def by_question_rotation(items: list[dict]) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for it in items:
            groups.setdefault(it["question"], []).append(it)
        out, queues = [], list(groups.values())
        while queues:
            queues = [q for q in queues if q]
            for q in queues:
                if q:
                    out.append(q.pop(0))
        return out

    for b in buckets:
        buckets[b] = by_question_rotation(buckets[b])

    picked: list[dict] = []
    per_question: Counter = Counter()
    seen_evidence: set[str] = set()

    def take(strict: bool) -> None:
        order = ["unsupported", "partial", "supported", "unverifiable"]
        progress = True
        while len(picked) < limit and progress:
            progress = False
            for b in order:
                for it in buckets[b]:
                    ev_hash = hashlib.sha256(it["evidence"].encode()).hexdigest()
                    if per_question[it["question"]] >= PER_QUESTION_CAP:
                        continue
                    if strict and ev_hash in seen_evidence:
                        continue
                    buckets[b].remove(it)
                    picked.append(it)
                    per_question[it["question"]] += 1
                    seen_evidence.add(ev_hash)
                    progress = True
                    break
                if len(picked) >= limit:
                    return

    take(strict=True)      # unique evidence payloads first
    take(strict=False)     # relax dedup (never the per-question cap) if short
    picked = blind_order(picked)   # row position must not spell out the stratum

    _append_jsonl(ITEMS_PATH, picked)
    # the human-facing file: claim + evidence + an empty label. Deliberately
    # NOTHING derived from the judge's verdict — that is the blindness.
    _append_jsonl(PENDING_PATH, [{"id": p["id"], "claim": p["claim"],
                                  "evidence": p["evidence"], "label": None}
                                 for p in picked])
    return len(picked)


def ingest() -> None:
    """Fold labeled pending items into the tracked set — labels only, strata
    joined back by id, bodies stay in the untracked store.

    Records `assist_shown` per label: whether a second opinion existed for that
    item when it was labeled. A label formed with a suggestion on screen is not
    independent of that suggestion, and a calibration set exists precisely to be
    independent — so the condition travels with the data instead of living in
    someone's memory of how the session went.
    """
    pending = _load_jsonl(PENDING_PATH)
    items = {i["id"]: i for i in _load_jsonl(ITEMS_PATH)}
    existing = {i["id"] for i in load()}
    assist = {r["id"]: r.get("label")
              for r in _load_jsonl(CALIBRATION_DIR / "assist.jsonl")}
    done = [p for p in pending if p.get("label") in LABELS]
    rest = [p for p in pending if p.get("label") not in LABELS]
    bad = [p["id"] for p in rest if p.get("label")]
    if bad:
        sys.exit(f"labels must be one of {LABELS}; fix and re-run: {bad}")

    added = 0
    with SET_PATH.open("a") as fh:
        for p in done:
            if p["id"] in existing:
                continue
            meta = items.get(p["id"], {})
            fh.write(json.dumps({
                "id": p["id"], "provenance": "human", "label": p["label"],
                "stratum": meta.get("stratum"),
                "assist_shown": p["id"] in assist,
                "assist_label": assist.get(p["id"]),
                "note": meta.get("locator", ""),
            }) + "\n")
            added += 1
    PENDING_PATH.write_text("".join(json.dumps(p) + "\n" for p in rest))
    print(f"ingested {added} human label(s) into {SET_PATH.name}; "
          f"{len(rest)} still pending")

    shown = [p for p in done if p["id"] in assist]
    if shown:
        matched = sum(1 for p in shown if p["label"] == assist[p["id"]])
        print(f"independence: {len(shown)} of {added} label(s) were made with a "
              f"second opinion visible; {matched} match it "
              f"({matched / len(shown):.0%})")
        if matched == len(shown) and len(shown) >= 10:
            print("  ⚠ EVERY such label matches the suggestion. Treat these as "
                  "assisted, not independent: agreement measured against them "
                  "is partly agreement with the assistant, so they cannot by "
                  "themselves settle which judge is right. Draw a fresh sample "
                  "with no second opinion to get an unanchored read.")
    state = calibration_state()
    print(f"calibration state: {state['state']} ({state['human_labels']} human; "
          f"{MIN_HUMAN_LABELS} needed for 'human-calibrated')")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=0,
                    help="draw N unlabeled real pairs for a human pass")
    ap.add_argument("--run", default=None,
                    help="sample from one .qa-artifacts run directory")
    ap.add_argument("--ingest", action="store_true",
                    help="fold labeled pending.jsonl items into the tracked set")
    args = ap.parse_args()

    if args.sample:
        n = sample_pairs(args.sample, run=args.run)
        items = _load_jsonl(ITEMS_PATH)
        counts = Counter(i["stratum"] for i in items)
        by_q = Counter(i.get("question", "?") for i in items if i.get("question"))
        print(f"sampled {n} pair(s) → {PENDING_PATH}")
        print(f"strata so far: {dict(counts)}  (balanced on purpose — reweight "
              "before reading accuracy as a population estimate; the labeler "
              "never sees these)")
        print(f"questions represented: {len(by_q)}  (cap {PER_QUESTION_CAP}/question; "
              f"max share: {by_q.most_common(1)[0] if by_q else 'n/a'})")
        print("For each line: read `claim` against `evidence`, set `label` to "
              "supported | partial | unsupported. Then run --ingest.")
        return
    if args.ingest:
        ingest()
        return

    labeled = [i for i in load() if i.get("label") in LABELS]
    if not labeled:
        sys.exit(f"no labeled items in {SET_PATH}")
    bodies = {i["id"]: i for i in _load_jsonl(ITEMS_PATH)}
    runnable, missing = [], []
    for i in labeled:
        if i.get("claim") and i.get("evidence"):     # pre-1D inline bodies
            runnable.append(i)
        elif i["id"] in bodies:
            runnable.append({**i, "claim": bodies[i["id"]]["claim"],
                             "evidence": bodies[i["id"]]["evidence"]})
        else:
            missing.append(i["id"])
    if missing:
        print(f"⚠ {len(missing)} labeled item(s) have no local evidence bodies "
              f"(bodies are never committed; they live only in "
              f"{ITEMS_PATH}): {', '.join(missing)}\n  These are EXCLUDED from "
              "agreement — the number below covers the rest.")
    if not runnable:
        sys.exit("no runnable items (all bodies missing)")

    tracker = Tracker(run_id="judge-calibration")

    # agreement is measured for the judge that will actually grade — the
    # backend dispatch mirrors judge.judge_bundle exactly
    from x1_advisor.agent.judge import JUDGE_BACKEND
    if JUDGE_BACKEND == "cc":
        from x1_advisor.agent.judge_cc import entail_cc

        def run(item: dict) -> tuple[dict, str]:
            verdict, _ = entail_cc(item["claim"], item["evidence"],
                                   tracker=tracker)
            return item, verdict
        workers = 4          # subprocesses on the shared seat, not API threads
    else:
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

        def run(item: dict) -> tuple[dict, str]:
            v = _ask(client, tracker,
                     ENTAILMENT_PROMPT.format(claim=item["claim"],
                                              source=item["evidence"]),
                     Entailment, "judge.calibrate")
            return item, (v.verdict if v else "unverifiable")
        workers = 6

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, runnable))

    pairs = [(i["label"], got) for i, got in results]
    accuracy = sum(1 for a, b in pairs if a == b) / len(pairs)
    kappa = cohens_kappa(pairs)

    print(f"judge: {calibration_state()['judge_model']}   items: {len(runnable)}")
    # `human` splits by whether a second opinion was on screen. Agreement with an
    # assisted label is partly agreement with whatever made the suggestion, so
    # the two never get averaged into one number.
    buckets = [("synthetic", lambda i: i.get("provenance") == "synthetic"),
               ("human/independent", lambda i: i.get("provenance") == "human"
                and not i.get("assist_shown")),
               ("human/assisted", lambda i: i.get("provenance") == "human"
                and i.get("assist_shown"))]
    for name, pred in buckets:
        subset = [(i, g) for i, g in results if pred(i)]
        if subset:
            acc = sum(1 for i, g in subset if i["label"] == g) / len(subset)
            k = cohens_kappa([(i["label"], g) for i, g in subset])
            note = ("  <- the one that can settle a judge choice"
                    if name == "human/independent" else
                    "  <- assisted: not independent evidence"
                    if name == "human/assisted" else "")
            print(f"  {name:<19} n={len(subset):<4} accuracy {acc:.2f}  "
                  f"kappa {'n/a' if k is None else format(k, '.2f')}{note}")
    print(f"overall accuracy {accuracy:.2f}   Cohen's kappa "
          f"{'n/a' if kappa is None else format(kappa, '.2f')}")

    # the safety-critical direction: a judge that calls unsupported claims
    # supported is worse than one that is merely strict
    missed = [(i, g) for i, g in results
              if i["label"] == "unsupported" and g == "supported"]
    print(f"unsupported judged supported (false clean bill): {len(missed)}")
    for i, g in results:
        if i["label"] != g:
            print(f"  ! {i['id']:<14} expected {i['label']:<12} got {g:<12} "
                  f"{i.get('note', '')}")

    state = calibration_state()
    print(f"\ncalibration state: {state['state']} "
          f"({state['human_labels']} human / {state['synthetic_labels']} synthetic; "
          f"{MIN_HUMAN_LABELS} human needed for 'human-calibrated')")
    print(f"cost ${tracker.run_total:.4f}")


if __name__ == "__main__":
    main()
