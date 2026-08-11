"""Truth-set builder (Gate 4 build step 2; GOLDEN-V2-DESIGN §5.1).

"Which startup evaluations mention regulatory risk?" has a property nothing in
golden v1 has: **the exact true answer is computable offline**. This module
computes it — a deterministic phrase/FTS scan over `advisor.doc_chunks` within
a bounded scope — and writes the definitive per-entity match set to
`.qa-artifacts/truth/<case-id>.json`. Grading then measures entity-level recall,
precision and overclaim with no judge involved, so a regression shows at n=1
instead of being lost in the judge's ±0.2–0.4 per-question variance (1E-4).

Three properties this file exists to guarantee:

* **Nobody authors an oracle** (§7.3, review criterion 5). The case supplies a
  predicate — a scope plus phrases, which is the operationalization of the
  question's own wording — and the corpus supplies the membership. Every truth
  set records its builder version, the corpus text watermark, and the full scope
  definition, so any result is rebuildable and auditable.
* **A stale truth set fails loudly, never grades quietly** (§5.1). The stored
  watermark is compared against the live corpus on every load; a mismatch raises.
* **Absence is verified, not assumed** (§6). `mode: absent` runs the same scan
  in must-be-empty mode: a known-absence case whose premise silently became
  false fails the BUILD rather than producing a fabrication canary that no
  longer canaries anything.

Entity counting is where a coverage denominator goes quietly wrong. The test
corpus holds both prod fixture bundles and test-env entities, and 9 company
names (Calmr, Angiex, BMI OrganBank, …) appear as BOTH. So "how many startups
did you search" has two defensible answers; the spec picks one via
`entity_key`, and the truth set records both counts either way.

Run:
    uv run python -m experiments.truth --golden v2            # build all
    uv run python -m experiments.truth --golden v2 --case v2c017
    uv run python -m experiments.truth --golden v2 --check    # staleness only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

from experiments.cases import (
    GOLDEN_DIR,
    Case,
    CaseValidationError,
    Suite,
    TruthSpec,
    load_suite,
)
from x1_advisor.agent.bundle import QA_ARTIFACTS_DIR
from x1_advisor.db import connect
from x1_advisor.fingerprint import corpus_text_watermark, sha256_text
from x1_advisor.scan import ScanScope
from x1_advisor.scan import scan as _engine_scan

# Bump when the SCAN semantics change (match evaluation, entity keying, status
# assignment). Every truth set records the version it was built with, and a
# version mismatch is staleness — old sets are rebuilt, never reinterpreted.
# v2: entity keys for CV/investor/org scopes are the entity's NAME derived from
#     the document title ("Paul Jaminet — CV" → "Paul Jaminet"), matching
#     script_runner.entity_vocabulary. v1 keyed those entities by the full
#     title, which no answer ever names — recall was structurally zero for
#     every people/investor case (second review, finding 3).
# v3: phrase matching requires a word-start boundary (scan.py _match_columns).
#     v2's bare-substring ILIKE manufactured phantom entities — "CE mark"
#     fired inside "performance marketing", "FDA" inside a URL token — and the
#     2026-08-06 oracle audit found 9 of v2c009's 14 matched entities were
#     artifacts. The right edge stays open ("regulatory risk" still matches
#     "regulatory risks").
BUILDER_VERSION = 3

# Truth sets contain corpus-derived match sets (which companies say what), so
# they live in owner-only local storage with the bundles, not in git. Only
# digests and aggregate counts are committed (see DIGEST_MANIFEST).
TRUTH_DIR = QA_ARTIFACTS_DIR.parent / "truth"
HISTORY_DIR = TRUTH_DIR / "history"
DIGEST_MANIFEST = GOLDEN_DIR / "truth_digests.json"


class TruthSetError(Exception):
    """Build-time problem: the scan cannot produce a usable oracle."""


class TruthSetStale(Exception):
    """The stored oracle does not match the corpus it would grade against."""


# --- the scan -------------------------------------------------------------


def scan(conn, spec: TruthSpec) -> dict[str, Any]:
    """Run the bounded scan. Pure function of (corpus, spec) — no model, no ACL
    beyond the admin scope golden runs use.

    The engine lives in `x1_advisor.scan` — it IS `scan_text` (bank §3.2A):
    same bounded-scope query, same per-entity `matched | no_match | not_indexed`
    statuses, same coverage counts. Sharing the code is the design intent:
    capability grading checks that the agent *reports* this exact scan
    faithfully, so grader and tool must never drift apart. This wrapper adds
    the oracle policy the engine doesn't carry: truth sets are admin-scoped.
    """
    if spec.acl != "admin":
        # Gate 6 personas need their own truth sets; reusing an admin-scoped
        # oracle to grade a restricted persona would demand evidence the agent
        # is not allowed to see.
        raise TruthSetError(f"only admin-scoped truth sets are supported, got "
                            f"acl={spec.acl!r}")
    scope = ScanScope(entity_type=spec.entity_type, entity_key=spec.entity_key,
                      source_types=spec.source_types,
                      granularity=spec.granularity, method=spec.method,
                      any_terms=spec.any_terms, all_terms=spec.all_terms)
    return _engine_scan(conn, scope, acl="admin")


# --- truth sets -----------------------------------------------------------


def truth_path(case_id: str) -> Path:
    return TRUTH_DIR / f"{case_id}.json"


def content_digest(payload: dict[str, Any]) -> str:
    """Digest over the ORACLE, not the file: scope + statuses + matches.

    Excludes `built_at` and the corpus watermark on purpose — rebuilding after
    an unrelated corpus edit should say "the answer key did not move", which is
    exactly the signal H1 triage wants. Staleness is a separate check.
    """
    body = {"builder_version": payload["builder_version"],
            "scope": payload["scope"],
            "counts": payload["counts"],
            "entities": [{"key": e["key"], "status": e["status"],
                          "chunks": sorted((c["document_id"], c["block_index"])
                                           for c in e["chunks"])}
                         for e in payload["entities"]]}
    return sha256_text(json.dumps(body, sort_keys=True, separators=(",", ":")))


def build_truth_set(conn, case: Case) -> dict[str, Any]:
    if not case.truth_spec:
        raise TruthSetError(f"{case.id}: no truth_spec")
    spec = case.truth_spec
    result = scan(conn, spec)

    if spec.mode == "absent":
        # §6: the known-absence class is the fabrication canary. If the corpus
        # grew a mention, the case's premise is now false and the case must be
        # re-authored — failing the build is the only honest outcome.
        matched = [e["key"] for e in result["entities"] if e["status"] == "matched"]
        if matched:
            raise TruthSetError(
                f"{case.id}: mode 'absent' requires an empty match set, but "
                f"{len(matched)} entit{'y' if len(matched) == 1 else 'ies'} "
                f"matched — the case's premise is no longer true and the case "
                f"needs re-authoring (entities withheld from this message; see "
                f"{truth_path(case.id)} after a --force build)")

    payload = {
        "case_id": case.id,
        "builder_version": BUILDER_VERSION,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "corpus": corpus_text_watermark(conn),
        "scope": spec.to_dict(),
        "question": case.question,      # what this oracle is the answer to
        **result,
    }
    payload["digest"] = content_digest(payload)
    return payload


def write_truth_set(payload: dict[str, Any]) -> tuple[Path, str | None]:
    """Write owner-only, archiving any previous version. Returns (path, previous
    digest) so the caller can report the rebuild diff (§5.1)."""
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    for directory in (TRUTH_DIR, TRUTH_DIR.parent):
        os.chmod(directory, 0o700)
    path = truth_path(payload["case_id"])

    previous = None
    if path.exists():
        old = json.loads(path.read_text())
        previous = old.get("digest")
        if previous != payload["digest"]:
            # never a silent overwrite: the superseded oracle stays available
            # for the diff a reviewer will want
            HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(HISTORY_DIR, 0o700)
            archived = HISTORY_DIR / f"{payload['case_id']}.{(previous or 'none')[:8]}.json"
            archived.write_text(json.dumps(old, indent=2))
            os.chmod(archived, 0o600)

    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh, indent=2)
    os.chmod(path, 0o600)
    return path, previous


def load_truth_set(conn, case: Case, *, strict: bool = True) -> dict[str, Any]:
    """Read the oracle for a case, refusing a stale one.

    §5.1: "a stale truth set must fail loudly, not grade quietly". Grading
    against an oracle built from a different corpus produces confident numbers
    about a corpus that no longer exists.
    """
    path = truth_path(case.id)
    if not path.exists():
        raise TruthSetStale(f"{case.id}: no truth set at {path} — run "
                            f"`uv run python -m experiments.truth --case {case.id}`")
    payload = json.loads(path.read_text())
    if not strict:
        return payload
    for problem in truth_set_problems(conn, case, payload):
        raise TruthSetStale(f"{case.id}: {problem}")
    return payload


def truth_set_problems(conn, case: Case, payload: dict[str, Any],
                       watermark: dict[str, Any] | None = None) -> list[str]:
    """Every reason this stored oracle may not be used, in one pass."""
    problems: list[str] = []
    if payload.get("builder_version") != BUILDER_VERSION:
        problems.append(f"built by builder v{payload.get('builder_version')}, "
                        f"this is v{BUILDER_VERSION} — rebuild")
    live = watermark if watermark is not None else corpus_text_watermark(conn)
    stored = payload.get("corpus") or {}
    for key in ("document_digest", "chunk_digest"):
        if stored.get(key) != live.get(key):
            problems.append(f"corpus {key} moved since the scan "
                            f"({stored.get(key)} → {live.get(key)}) — rebuild")
    if case.truth_spec and payload.get("scope") != case.truth_spec.to_dict():
        problems.append("scope definition in the case no longer matches the "
                        "scan that produced this oracle — rebuild")
    if payload.get("digest") != content_digest(payload):
        problems.append("digest does not match its own content — file edited "
                        "by hand? truth sets are computed, never edited (§7.3)")
    return problems


# --- committed digest manifest -------------------------------------------


def read_manifest() -> dict[str, Any]:
    if DIGEST_MANIFEST.exists():
        return json.loads(DIGEST_MANIFEST.read_text())
    return {"builder_version": BUILDER_VERSION, "truth_sets": {}}


def update_manifest(entries: dict[str, dict[str, Any]]) -> None:
    """Commit digests + aggregate counts, never match sets.

    This is the git-visible half: a reviewer sees that v2c017's oracle moved
    from 12 matched to 3 without any company name entering the repository.
    """
    manifest = read_manifest()
    manifest["builder_version"] = BUILDER_VERSION
    manifest.setdefault("truth_sets", {}).update(entries)
    manifest["truth_sets"] = dict(sorted(manifest["truth_sets"].items()))
    DIGEST_MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")


def manifest_entry(payload: dict[str, Any]) -> dict[str, Any]:
    return {"digest": payload["digest"],
            "builder_version": payload["builder_version"],
            "counts": payload["counts"],
            "scope": payload["scope"]}


# --- generated fixture pools (§7.1) ---------------------------------------

# Pools whose membership is a plain fact of the corpus. Generated rather than
# hand-listed for the same reason truth sets are: an author-written membership
# list is an answer key by another name. Each pool must stay NEUTRAL with
# respect to the questions asked of it — `deck_startup` may bind a case that
# compares a deck against an evaluation, but must never bind the case that asks
# whether a deck exists, or the answer is guaranteed before the run starts.
POOL_QUERIES: dict[str, tuple[str, str]] = {
    "evaluated_startup": (
        "companies with at least one evaluation document in the corpus",
        """SELECT DISTINCT ch.metadata->>'company_name' AS name,
                  coalesce(ch.metadata->>'entity_ref_env', 'test') AS env
           FROM advisor.doc_chunks ch
           JOIN advisor.documents d ON d.id = ch.document_id
           WHERE d.superseded_by IS NULL
             AND d.source_type IN ('eval_section', 'eval_basic', 'eval_premium')
             AND ch.metadata->>'entity_type' = 'startup_company'
             AND ch.metadata->>'company_name' IS NOT NULL
           ORDER BY 1"""),
    "deck_startup": (
        "companies with a pitch-deck extract AND an evaluation (conflict cases)",
        """SELECT DISTINCT ch.metadata->>'company_name' AS name,
                  coalesce(ch.metadata->>'entity_ref_env', 'test') AS env
           FROM advisor.doc_chunks ch
           JOIN advisor.documents d ON d.id = ch.document_id
           WHERE d.superseded_by IS NULL AND d.source_type = 'deck_extract'
             AND ch.metadata->>'company_name' IS NOT NULL
             AND EXISTS (SELECT 1 FROM advisor.doc_chunks c2
                         JOIN advisor.documents d2 ON d2.id = c2.document_id
                         WHERE d2.superseded_by IS NULL
                           AND d2.source_type IN ('eval_section','eval_basic','eval_premium')
                           AND c2.metadata->>'company_name' = ch.metadata->>'company_name')
           ORDER BY 1"""),
    "single_evaluation_startup": (
        "companies whose entire evaluation history is one bundle — so any "
        "question premised on a score change or a second evaluation is false",
        # The collision guard (2026-08-07): a company whose NORMALIZED name
        # (case/punctuation folded) collides with another corpus record —
        # 2ndCourt.com vs 2ndCourtcom, Accelium Partners AG vs
        # AcceliumPartnersAG — cannot anchor a "no second evaluation"
        # premise. Each record holds one bundle, but the corpus under the
        # name the USER typed holds two evaluation histories, so the premise
        # is registry-true and corpus-false; v2c045 (410a95c) answered about
        # the duplicate's evaluation as "the second evaluation".
        """SELECT DISTINCT ch.metadata->>'company_name' AS name, 'prod' AS env
           FROM advisor.doc_chunks ch
           JOIN advisor.documents d ON d.id = ch.document_id
           WHERE d.superseded_by IS NULL
             AND d.source_type IN ('eval_section', 'eval_basic', 'eval_premium')
             AND ch.metadata->>'entity_ref_env' = 'prod'
             AND ch.metadata->>'company_name' IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM startup_companies s
                             WHERE s.name = ch.metadata->>'company_name')
             AND NOT EXISTS (
                 SELECT 1 FROM advisor.doc_chunks c3
                 WHERE c3.metadata->>'company_name' IS NOT NULL
                   AND c3.metadata->>'company_name'
                       <> ch.metadata->>'company_name'
                   AND regexp_replace(lower(c3.metadata->>'company_name'),
                                      '[^a-z0-9]', '', 'g')
                     = regexp_replace(lower(ch.metadata->>'company_name'),
                                      '[^a-z0-9]', '', 'g'))
           ORDER BY 1"""),
}

GENERATED_FIXTURES = GOLDEN_DIR / "fixtures.generated.yaml"


def build_pools(conn) -> dict[str, list[dict[str, Any]]]:
    return {name: [{"name": r["name"], "entity_type": "startup_company",
                    "note": r.get("env")}
                   for r in conn.execute(sql).fetchall()]
            for name, (_, sql) in POOL_QUERIES.items()}


def write_pools(pools: dict[str, list[dict[str, Any]]]) -> None:
    lines = ["# GENERATED by `uv run python -m experiments.truth --pools` —"
             " do not edit.",
             "# Entity-slot pools whose membership is computed from the corpus"
             " (§7.1).",
             "# Hand-listed pools and the registry's documentation live in"
             " fixtures.yaml.",
             "pools:"]
    for name, entries in sorted(pools.items()):
        lines.append(f"  # {POOL_QUERIES[name][0]}")
        lines.append(f"  {name}:")
        for entry in entries:
            note = f", note: {entry['note']}" if entry.get("note") else ""
            lines.append(f"    - {{name: {json.dumps(entry['name'])}, "
                         f"entity_type: startup_company{note}}}")
    GENERATED_FIXTURES.write_text("\n".join(lines) + "\n")


# --- CLI ------------------------------------------------------------------


def cases_with_truth_sets(suite: Suite, only: str | None = None) -> list[Case]:
    cases = [c for c in suite.cases if c.truth_spec]
    if only:
        cases = [c for c in cases if c.id == only]
        if not cases:
            raise SystemExit(f"{only} is not a case with a truth_spec")
    return cases


def _check(conn, suite: Suite, cases: list[Case]) -> int:
    watermark = corpus_text_watermark(conn)
    manifest = read_manifest().get("truth_sets", {})
    stale = 0
    for case in cases:
        path = truth_path(case.id)
        if not path.exists():
            print(f"  MISSING  {case.id}")
            stale += 1
            continue
        payload = json.loads(path.read_text())
        problems = truth_set_problems(conn, case, payload, watermark=watermark)
        committed = (manifest.get(case.id) or {}).get("digest")
        if committed and committed != payload.get("digest"):
            problems.append(f"digest differs from the committed manifest "
                            f"({committed[:8]}… vs {payload['digest'][:8]}…)")
        if problems:
            stale += 1
            print(f"  STALE    {case.id}")
            for problem in problems:
                print(f"           {problem}")
        else:
            print(f"  ok       {case.id}  {payload['counts']['matched']}/"
                  f"{payload['counts']['eligible']} matched  "
                  f"{payload['digest'][:8]}…")
    print(f"\n{len(cases) - stale}/{len(cases)} truth sets current")
    return stale


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", default="v2")
    ap.add_argument("--case", default=None, help="build/check one case only")
    ap.add_argument("--check", action="store_true",
                    help="report staleness and exit 1 if any (H1 QA harness)")
    ap.add_argument("--pools", action="store_true",
                    help="regenerate the computed entity-slot pools (§7.1)")
    args = ap.parse_args()

    if args.pools:
        with connect() as conn:
            pools = build_pools(conn)
        write_pools(pools)
        for name, entries in sorted(pools.items()):
            print(f"  {name:<28} {len(entries):>3} entities")
        print(f"\nwritten: {GENERATED_FIXTURES}")
        return

    try:
        suite = load_suite(args.golden)
    except CaseValidationError as exc:
        print(f"suite does not compile ({len(exc.errors)} error(s)):",
              file=sys.stderr)
        for err in exc.errors:
            print(f"  {err}", file=sys.stderr)
        sys.exit(2)

    cases = cases_with_truth_sets(suite, args.case)
    if not cases:
        print("no cases carry a truth_spec yet")
        return

    with connect() as conn:
        if args.check:
            print(f"== truth sets for golden {suite.version} ==")
            sys.exit(1 if _check(conn, suite, cases) else 0)

        entries, failed = {}, []
        for case in cases:
            try:
                payload = build_truth_set(conn, case)
            except TruthSetError as exc:
                failed.append(str(exc))
                print(f"  FAILED   {case.id}")
                continue
            path, previous = write_truth_set(payload)
            counts = payload["counts"]
            moved = (" (unchanged)" if previous == payload["digest"]
                     else f" (was {previous[:8]}…)" if previous else " (new)")
            print(f"  built    {case.id}  matched {counts['matched']}/"
                  f"{counts['eligible']}  no_match {counts['no_match']}  "
                  f"not_indexed {counts['not_indexed']}  "
                  f"chunks {counts['matching_chunks']}/{counts['scanned_chunks']}"
                  f"  {payload['digest'][:8]}…{moved}")
            entries[case.id] = manifest_entry(payload)

        if entries:
            update_manifest(entries)
            print(f"\ntruth sets (owner-only): {TRUTH_DIR}")
            print(f"digests (committed):     {DIGEST_MANIFEST}")
        for message in failed:
            print(f"\n{message}", file=sys.stderr)
        sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
