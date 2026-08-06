"""Replay the escalation-gate calibration set against the live gates.

The gates (experiments/adjudicate.py) let a calibrated judge overturn formula
failures. That privilege rests on the judge agreeing with the labeled intent —
and the rubrics WILL be edited. This harness is what makes a rubric edit a
measured change instead of a vibed one: every labeled item is replayed through
the same gate function, same rubric, same k-sample majority that production
runs use, and disagreement is reported per item. The recorded follow-up in
DECISIONS (2026-08-06): this replay is the trust condition before the
adjudicated gates run at nightly scale.

Two directions of disagreement, deliberately asymmetric:

* a **lenient label judged strict** (an `adequate` item the gate calls
  inadequate) costs a false failure downstream — reported, not fatal;
* a **must-fail item judged lenient** (stratum `must-fail`: citation theater,
  fabricated causality, a genuine overclaim) is the leniency ratchet moving —
  the exact failure mode David's "never absolve B" line forbids. Any such
  breach exits nonzero so nothing automated can shrug it off.

Storage split (same rule as judge_calibrate):

* `experiments/adjudication_calibration.jsonl` (tracked) — labels only:
  id, gate, label, stratum, provenance, ratification, note.
* `.qa-artifacts/calibration/adjudication_items.jsonl` (untracked, owner-only)
  — the bodies: real items point at run bundles (claim text / entity + side);
  synthetic must-fail items carry a full inline mini-bundle.

Run:  uv run python -m experiments.adjudicate_calibrate
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from experiments.adjudicate import (ADJ_SAMPLES, adjudicate_asserted_names,
                                    adjudicate_citation_coverage,
                                    adjudicate_coverage_claims,
                                    adjudicate_coverage_statement,
                                    adjudicate_entity_intrusion,
                                    adjudicate_faithfulness,
                                    adjudicate_quotes)
from experiments.judge_calibrate import cohens_kappa
from x1_advisor.agent.bundle import QA_ARTIFACTS_DIR
from x1_advisor.cost import Tracker

LABELS_PATH = Path(__file__).parent / "adjudication_calibration.jsonl"
ITEMS_PATH = QA_ARTIFACTS_DIR.parent / "calibration" / "adjudication_items.jsonl"
REPO_ROOT = QA_ARTIFACTS_DIR.parent.parent

# the lenient outcome per gate — the verdict that overturns a formula
# failure, and therefore the direction the must-fail check watches. (On the
# overclaim side a True verdict UPHOLDS the formula, so lenient is
# `not_asserted`; `credited` is lenient only toward recall, but a must-fail
# miss item would still be a ratchet breach if credited.)
_LENIENT = frozenset({"adequate", "faithful", "not_asserted", "credited",
                      "disclosed", "not_overclaim", "grounded", "met"})


@dataclass
class Result:
    id: str
    gate: str
    expected: str
    got: str
    votes: list
    stratum: str
    note: str

    @property
    def agree(self) -> bool:
        return self.expected == self.got

    @property
    def leniency_breach(self) -> bool:
        """A must-fail item the gate would wave through."""
        return (self.stratum == "must-fail" and not self.agree
                and self.got in _LENIENT)


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()]


def _bundle_of(item: dict) -> dict:
    if item.get("inline"):
        return item["inline"]
    return json.loads((REPO_ROOT / item["bundle"]).read_text())


def _group_key(item: dict) -> tuple:
    """One gate invocation per (gate, source) — real items sharing a bundle
    are adjudicated together, exactly as a production run would present them;
    each synthetic mini-bundle stands alone."""
    return (item["gate"], item.get("bundle") or item["id"])


def replay_citation(items: list[dict], *, tracker=None,
                    _transport=None) -> list[tuple[dict, str, list]]:
    bundle = _bundle_of(items[0])
    verdict = {"uncited_claims": [it["claim"] for it in items]}
    adj = adjudicate_citation_coverage(bundle, verdict, tracker=tracker,
                                       _transport=_transport)
    out = []
    for it, pc in zip(items, adj["per_claim"]):
        out.append((it, "adequate" if pc["adequate"] else "inadequate",
                    pc["votes"]))
    return out


def replay_faithfulness(items: list[dict], *, tracker=None,
                        _transport=None) -> list[tuple[dict, str, list]]:
    bundle = _bundle_of(items[0])
    stored = {v["claim"]: v for v in
              (bundle.get("judge") or {}).get("verdicts") or []}
    partials = []
    for it in items:
        if it.get("inline"):
            partials.append({"verdict": "partial", "claim": it["claim"],
                             "citations": it.get("cited") or [],
                             "reason": it.get("grader_reason", "")})
        else:
            # the production prompt includes the grader's own partial reason —
            # replay must too, so join it back from the stored bundle. A claim
            # the bundle no longer carries is a broken pointer: fail loudly,
            # never silently re-grade different text than was labeled.
            v = stored.get(it["claim"])
            if v is None:
                raise SystemExit(f"{it['id']}: labeled claim not found in "
                                 f"{it['bundle']} judge verdicts")
            partials.append({"verdict": "partial", "claim": it["claim"],
                             "citations": v.get("citations"),
                             "reason": v.get("reason", "")})
    verdict = {"counts": {"partial": len(partials)}, "verdicts": partials}
    adj = adjudicate_faithfulness(bundle, verdict, tracker=tracker,
                                  _transport=_transport)
    out = []
    for it, pc in zip(items, adj["per_claim"]):
        out.append((it, "faithful" if pc["faithful"] else "unfaithful",
                    pc["votes"]))
    return out


def replay_names(items: list[dict], *, tracker=None,
                 _transport=None) -> list[tuple[dict, str, list]]:
    bundle = _bundle_of(items[0])
    question = ((bundle.get("request") or {}).get("question")
                or (bundle.get("request") or {}).get("prompt") or "")
    answer = bundle.get("validation", {}).get("answer", "")
    grade = {"overclaimed": [it["entity"] for it in items
                             if it["side"] == "overclaim_flagged"],
             "missed": [it["entity"] for it in items
                        if it["side"] == "miss_flagged"]}
    adj = adjudicate_asserted_names(question, answer, grade, tracker=tracker,
                                    _transport=_transport)
    out = []
    for it in items:
        rec = adj["per_name"][it["side"]][it["entity"]]
        pos, neg = (("asserted", "not_asserted")
                    if it["side"] == "overclaim_flagged"
                    else ("credited", "uncredited"))
        out.append((it, pos if rec["verdict"] else neg, rec["votes"]))
    return out


def _qa_of(item: dict) -> tuple[str, str]:
    """(question, answer) — s5 inline items carry them flat; real items point
    at a bundle."""
    if item.get("inline") and "question" in item["inline"]:
        return item["inline"]["question"], item["inline"]["answer"]
    b = _bundle_of(item)
    q = ((b.get("request") or {}).get("question")
         or (b.get("request") or {}).get("prompt") or "")
    return q, b.get("validation", {}).get("answer", "")


def _evidence_of(item: dict) -> list[str]:
    """Raw evidence texts for the s5 gates: inline strings, or snapshots from
    the item's evidence_bundles (falling back to its own bundle)."""
    if item.get("inline") and "evidence" in item["inline"]:
        ev = item["inline"]["evidence"]
        return list(ev) if isinstance(ev[0], str) else \
            [e.get("snapshot") or "" for e in ev]
    paths = item.get("evidence_bundles") or [item["bundle"]]
    return [e.get("snapshot") or ""
            for p in paths
            for e in (json.loads((REPO_ROOT / p).read_text())
                      .get("evidence") or [])]


def replay_quotes(items: list[dict], *, tracker=None,
                  _transport=None) -> list[tuple[dict, str, list]]:
    question, answer = _qa_of(items[0])
    adj = adjudicate_quotes(question, answer,
                            [it["quote"] for it in items],
                            _evidence_of(items[0]), tracker=tracker,
                            _transport=_transport)
    return [(it, "faithful" if pc["faithful"] else "unfaithful", pc["votes"])
            for it, pc in zip(items, adj["per_item"])]


def replay_coverage_statement(items: list[dict], *, tracker=None,
                              _transport=None) -> list[tuple[dict, str, list]]:
    question, answer = _qa_of(items[0])
    adj = adjudicate_coverage_statement(question, answer, tracker=tracker,
                                        _transport=_transport)
    pc = adj["per_item"][0]
    return [(items[0], "disclosed" if pc["disclosed"] else "undisclosed",
             pc["votes"])]


def replay_coverage_claims(items: list[dict], *, tracker=None,
                           _transport=None) -> list[tuple[dict, str, list]]:
    question, answer = _qa_of(items[0])
    adj = adjudicate_coverage_claims(question, answer,
                                     [it["number"] for it in items],
                                     items[0].get("telemetry") or {},
                                     _evidence_of(items[0]), tracker=tracker,
                                     _transport=_transport)
    return [(it, "overclaim" if pc["overclaim"] else "not_overclaim",
             pc["votes"]) for it, pc in zip(items, adj["per_item"])]


def replay_entity_intrusion(items: list[dict], *, tracker=None,
                            _transport=None) -> list[tuple[dict, str, list]]:
    question, answer = _qa_of(items[0])
    adj = adjudicate_entity_intrusion(question, answer,
                                      [it["entity"] for it in items],
                                      _evidence_of(items[0]), tracker=tracker,
                                      _transport=_transport)
    return [(it, "grounded" if pc["grounded"] else "ungrounded", pc["votes"])
            for it, pc in zip(items, adj["per_item"])]


def replay_behavior(items: list[dict], *, tracker=None,
                    _transport=None) -> list[tuple[dict, str, list]]:
    from experiments.adjudicate import adjudicate_behavior
    from experiments.cases import BEHAVIOR_OBLIGATIONS

    out = []
    for it in items:                    # one obligation per invocation
        if it.get("inline"):
            q, a = it["inline"]["question"], it["inline"]["answer"]
            reason = it["inline"].get("reason", "")
        else:
            q, a = _qa_of(it)
            b = _bundle_of(it)
            v = next((v for v in b.get("behavior_verdicts") or []
                      if v["obligation"] == it["obligation"]), None)
            if v is None:
                raise SystemExit(f"{it['id']}: obligation {it['obligation']} "
                                 f"not in {it['bundle']} behavior_verdicts")
            reason = v.get("reason", "")
        adj = adjudicate_behavior(q, a, it["obligation"],
                                  BEHAVIOR_OBLIGATIONS[it["obligation"]],
                                  reason, tracker=tracker,
                                  _transport=_transport)
        pc = adj["per_item"][0]
        out.append((it, "met" if pc["met"] else "unmet", pc["votes"]))
    return out


_REPLAY = {"citation_coverage": replay_citation,
           "faithfulness": replay_faithfulness,
           "asserted_names": replay_names,
           "quotes": replay_quotes,
           "coverage_statement": replay_coverage_statement,
           "coverage_claims": replay_coverage_claims,
           "entity_intrusion": replay_entity_intrusion,
           "behavior": replay_behavior}


def replay(labels: list[dict], items: list[dict], *, tracker=None,
           _transport=None, workers: int = 4) -> tuple[list[Result], list[str]]:
    """Join labels to bodies, run one gate invocation per group (pooled),
    return per-item results plus the ids excluded for missing bodies."""
    bodies = {i["id"]: i for i in items}
    joined, missing = [], []
    for lab in labels:
        body = bodies.get(lab["id"])
        if body is None:
            missing.append(lab["id"])
            continue
        joined.append({**body, "_label": lab})

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for it in joined:
        groups[_group_key(it)].append(it)

    def run_group(group: list[dict]) -> list[Result]:
        gate = group[0]["gate"]
        out = []
        for it, got, votes in _REPLAY[gate](group, tracker=tracker,
                                            _transport=_transport):
            lab = it["_label"]
            out.append(Result(id=it["id"], gate=gate,
                              expected=lab["label"], got=got, votes=votes,
                              stratum=lab.get("stratum", ""),
                              note=lab.get("note", "")))
        return out

    ordered = list(groups.values())
    if _transport is not None or workers <= 1:
        batches = [run_group(g) for g in ordered]   # deterministic for stubs
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            batches = list(pool.map(run_group, ordered))
    return [r for batch in batches for r in batch], missing


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    labels = _load_jsonl(LABELS_PATH)
    if not labels:
        sys.exit(f"no labels in {LABELS_PATH}")
    tracker = Tracker(run_id="adjudication-calibration")
    results, missing = replay(labels, _load_jsonl(ITEMS_PATH), tracker=tracker)
    if missing:
        print(f"⚠ {len(missing)} labeled item(s) have no local bodies (bodies "
              f"are never committed; they live only in {ITEMS_PATH}): "
              f"{', '.join(missing)}\n  EXCLUDED from agreement below.")

    by_gate: dict[str, list[Result]] = defaultdict(list)
    for r in results:
        by_gate[r.gate].append(r)
    print(f"gate samples per item: {ADJ_SAMPLES} (majority)\n")
    for gate, rs in sorted(by_gate.items()):
        acc = sum(r.agree for r in rs) / len(rs)
        kappa = cohens_kappa([(r.expected, r.got) for r in rs])
        print(f"  {gate:<19} n={len(rs):<3} agreement {acc:.2f}  kappa "
              f"{'n/a' if kappa is None else format(kappa, '.2f')}")
    overall = sum(r.agree for r in results) / len(results)
    print(f"  {'overall':<19} n={len(results):<3} agreement {overall:.2f}")

    for r in results:
        if not r.agree:
            print(f"  ! {r.id:<10} expected {r.expected:<12} got {r.got:<12} "
                  f"votes={r.votes}  {r.note}")

    breaches = [r for r in results if r.leniency_breach]
    print(f"\nmust-fail items judged lenient (leniency-ratchet breach): "
          f"{len(breaches)}")
    for r in breaches:
        print(f"  !! {r.id}  {r.note}")
    print(f"cost ${tracker.run_total:.4f}")
    sys.exit(1 if breaches else 0)


if __name__ == "__main__":
    main()
