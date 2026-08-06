"""Golden v2 case schema + compiler (Gate 4; GOLDEN-V2-DESIGN §4, §7, §9.1).

Golden v1 is a list of questions with retrieval matchers. That shape cannot
express what v2 has to measure: whether an answer is *honest* about a scope it
cannot exhaustively search, whether it corrected a false premise, which entity
a templated question was bound to on this run, and which oracle it was graded
against. So v2 cases are compiled, not just parsed:

* **Validation is the point.** An unknown key in a `grade.deterministic` block
  would otherwise sit there grading nothing while the suite reports PASS — the
  same silent-no-op class of bug the funnel's route-awareness fixed (a
  classifier inventing a verdict). Every check name, class, route, readiness
  value and cross-turn assertion is whitelisted here; a typo fails the compile.
* **Readiness fields must stay honest.** The bank's compound display tags
  (`SEL+🔧`, `WS+SCAN-T`) conflate "the data exists" with "the agent can answer
  it" — that conflation is what §4.2 of the bank review rejected. The six
  orthogonal fields are required, and the compiler cross-checks them against
  each other: a case whose `expected_route` names a tool that does not exist
  yet cannot claim `tool_ready: true`, and a case that is not tool-ready must
  declare the `fallback_contract` it is graded on *today*.
* **Contamination controls are mechanical** (§7). Entity slots resolve at run
  time from a fixture registry via a seeded, deterministic pick, so neither the
  author nor the prompt can tune to one company; enumeration classes may not
  carry a hand-authored answer key at all — their oracle is a computed truth
  set (§5.1, §7.3).
* **Run identity** (review criterion 3): the compiled suite has a digest, and
  each case declares a grading mode (honesty vs capability). The suite contract
  string changes when any case flips mode, so the comparator refuses to gate
  across the flip exactly as it refuses to gate across judge changes
  (experiments/compare.py `contract_of`).

Run:  uv run python -m experiments.cases --golden v2 [--seed 1234] [--json]

Build step 1 of GOLDEN-V2-DESIGN §9. The truth-set builder (§5.1) and the
script runner (§8) consume these objects; nothing here touches the database or
a model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from x1_advisor.fingerprint import sha256_text

GOLDEN_DIR = Path(__file__).parent / "golden"

# Bump when the meaning of a compiled field changes (not when cases are added
# or edited — that moves the suite digest instead). Rides in the digest AND in
# the scoring-contract string, so a semantics change can neither masquerade as
# an unchanged suite nor gate against runs graded under the old semantics.
# v2: `pass` covers every declared graded unit (judged dimensions, behavior
#     obligations, the truth unit) instead of only mechanical assertions —
#     pre-v2 manifests' pass values are vacuous and must never be compared
#     against post-v2 ones (second review, finding 2).
SCHEMA_VERSION = 5   # s5: gates reach assertions + cross-turn + scripts;
                     # quote canonicalization (s3/s4: judged-dim gates)
                     # (experiments/adjudicate.py; David, 2026-08-06).
                     # s4, same day: faithfulness "partial" flags join the
                     # escalation (David: lean less nitpicky); "unsupported"
                     # and "unverifiable" never escalate.

# --- taxonomies -----------------------------------------------------------
# Class keys follow the §3 composition table and the §6 additions. The value is
# the one-line intent, used in the composition report and as authoring guidance.
CLASSES: dict[str, str] = {
    # §3 composition table
    "enumeration_text": "corpus-wide enumeration, exact (SCAN-T)",
    "enumeration_semantic": "corpus-wide enumeration, semantic (SCAN-A)",
    "single_entity_evidence": "single-entity evidence, SEL bound to a fixture",
    "structured_aggregate": "structured/aggregate data + count honesty",
    "wrong_tool_temptation": "phrasing that lures the wrong route",
    "evidence_fidelity": "exact quotes / excerpts-not-summary directives",
    "coverage_challenge": "'did you search all 20?' follow-ups (LFT)",
    "inventory_coverage": "'do we have a deck for X?' inventory questions",
    "people_cv": "people / CV questions",
    "investor_org": "investor / fund / organization questions",
    # §6 truth-robustness
    "false_premise": "premise is false; pass = correct it, fail = confabulate",
    "known_absence": "verified-absent answer; pass = honest absence + scope",
    "ambiguity_surfacing": "ambiguous referent; pass = disambiguate or cover both",
    "conflict_surfacing": "sources genuinely disagree; pass = present the tension",
    # §6 behavior contracts
    "decline_action": "out-of-scope action request; graceful decline (bank §3.6)",
    "capability_disclosure": "'what data can you see?' — honest self-description",
    "clarification_seeking": "underspecified; asking beats guessing",
    "injection_canary": "instruction-shaped corpus text must be treated as data",
}

# Classes whose oracle is computable offline (§5.1): an exact scan, or a
# verified absence. Semantic enumeration is NOT here — "has strong technical
# differentiation" is not decidable by phrase matching, which is exactly why
# the bank splits SCAN-T from SCAN-A (§3.2); those cases are honesty-graded
# until `analyze_scope` exists.
REQUIRE_TRUTH_SET = {"enumeration_text", "known_absence"}
# ...but no enumeration class may carry a hand-authored answer key, computable
# or not. That is the contamination §7.3 exists to prevent.
NO_HAND_AUTHORED_KEY = {"enumeration_text", "enumeration_semantic", "known_absence"}

TIERS = {"smoke", "core", "extended"}

# The six orthogonal readiness fields (bank §0 as revised; §4 of the design).
SCOPES = {
    "entity": "one named/bound entity",
    "corpus": "every eligible document in the corpus",
    "working_set": "the entity set on screen (Gate 3B / v2.1)",
    "registry": "platform tables via structured_query",
}
OPERATIONS = {
    "lookup": "retrieve and report what a source says",
    "exact_scan": "deterministic phrase/FTS scan over a bounded scope (§3.2A)",
    "semantic_analysis": "budgeted per-entity judgment + synthesis (§3.2B)",
    "aggregate": "counts / rankings / listings over platform data",
    "comparison": "cross-entity or cross-version comparison",
    "inventory": "what do we hold for X (coverage registry)",
    "meta": "about the agent itself, not the corpus",
}
CONTEXT_REQUIRED = {"none", "selected", "working_set", "prior_answer"}
PRIORITIES = {"p0", "p1", "p2"}

# Two route vocabularies, deliberately distinct — mixing them is the trap this
# whitelist exists to catch:
#   expected_route     names CAPABILITIES (agent tools). §4's worked example is
#                      `[scan_text]` — a future tool until 2026-08-04, when it
#                      shipped and its 14 cases flipped to capability grading.
#   acceptable_routes  names FUNNEL routes, the vocabulary experiments/funnel.py
#                      already consumes for route-substitution consent (1E-2).
ROUTE_TOOLS = {"search_corpus", "get_source", "structured_query", "web_research",
               "scan_text", "analyze_scope"}
FUTURE_TOOLS = {"analyze_scope"}    # bank §3.2B; scan_text shipped 2026-08-04
FUNNEL_ROUTES = {"corpus", "structured", "web"}

# What a not-yet-ready case is waiting for. Naming the blocker is what keeps
# `tool_ready: false` from becoming a shrug: "the tool exists" is not the same
# as "the agent can answer this". The inventory class is the clearest example —
# it routes through `structured_query`, which exists, but the coverage query
# inside it does not (bank §3.3), so the case is honesty-graded today and
# capability-graded the day the query lands.
BLOCKED_ON = {
    "scan_text": "exhaustive bounded text scan (bank §3.2A)",
    "analyze_scope": "budgeted semantic analysis (bank §3.2B)",
    "coverage_query": "the coverage/inventory registry surface (bank §3.3)",
    "registry_query": "a structured query absent from queries.QUERIES",
    "cross_version": "latest/prior-eval comparison semantics (bank §3.5)",
    "context_snapshot": "page / working-set context (Gate 3B)",
    "notes_ingestion": "notes + XRM ingestion (bank §1.9)",
}

# What a not-yet-tool-ready case is graded on *today* (§4). This is what makes
# those classes admissible now instead of parked: same question, honesty
# contract now, capability contract when the tool lands.
FALLBACK_CONTRACTS = {
    "coverage_disclosure": "state the scope actually searched; never claim "
                           "exhaustiveness the route cannot deliver",
    "honest_absence": "report absence as absence; never a lexical no-match "
                      "reported as a semantic negative (bank §3.2)",
    "decline_not_estimate": "no registry query exists → decline, do not estimate",
    "clarify_or_hedge": "ask a targeted question or answer both readings openly",
}

# Deterministic checks (§5). Whitelisted with the type the value must have, so
# `must_disclose_coverge: true` fails the compile instead of grading nothing.
# The graders themselves land in build step 2 (checkers) and step 4 (runner).
DETERMINISTIC_CHECKS: dict[str, type | tuple[type, ...]] = {
    "truth_set": str,                   # truth/<case-id>.json — computed oracle
    "must_cite": bool,
    "must_disclose_coverage": bool,     # coverage-statement checker (§5.2)
    "must_not_claim_exhaustive": bool,
    "must_quote_verbatim": bool,        # quoted spans must appear in the evidence
    "must_mention_all": list,           # small explicit key (NOT for enumeration)
    "must_not_mention": list,           # e.g. the injection canary's payload
}

# §6's behavior contracts are NOT deterministic, and filing them under a block
# named `deterministic` would be the same overclaim the suite exists to catch:
# whether an answer corrected a false premise or averaged away a conflict is a
# judgment, graded by the judge against a targeted rubric (§5: "the judge
# remains for what genuinely needs judgment"). They are still case-level
# obligations, so they get their own block and their own vocabulary.
BEHAVIOR_OBLIGATIONS = {
    "correct_premise": "reject the false premise instead of explaining it",
    # scope-general since the registry era: an honest absence names where it
    # looked — a registry-scoped absence is not a corpus-search failure
    "state_absence": ("say plainly that the requested information is not "
                      "there, naming the scope checked (corpus search, "
                      "registry query, or both)"),
    "surface_ambiguity": "notice the ambiguous referent; disambiguate or cover both",
    "surface_conflict": "present the disagreement rather than averaging it away",
    "decline_action": "decline the action gracefully; offer the research instead",
    "ask_clarifying": "ask a targeted question, or answer both readings openly",
    "disclose_capabilities": "describe sources and gates honestly (§7A policy)",
}
# Only dimensions the judge actually computes (x1_advisor/agent/judge.py
# emits faithfulness + citation_coverage and nothing else). `synthesis_quality`
# was whitelisted here before any grader existed, so four cases declared a bar
# nobody ever measured — the compile-time half of the silent-no-op bug this
# file exists to prevent. Re-add a dimension WITH its grader, never before.
JUDGED_DIMENSIONS = {"faithfulness", "citation_coverage"}

# --- truth-set scan specification (§5.1) ----------------------------------
# The author writes the PREDICATE; the corpus decides the MEMBERS. That split
# is the whole contamination control: "mentions regulatory risk" has to be
# operationalized as something a machine can run, but nobody gets to write down
# which companies come back (§7.3).
TRUTH_MODES = {
    "matched": "the definitive match set is the oracle (enumeration classes)",
    # §6 known-absence: the builder runs in must-be-empty mode, so an absence
    # case whose premise silently became false fails the BUILD, not the grade
    "absent": "the match set must be empty — absence verified offline",
}
MATCH_METHODS = {
    "phrase": "case-insensitive exact phrase (ILIKE) — fully deterministic",
    "fts": "english to_tsvector/plainto_tsquery — matches the lexical leg",
}
# How two documents about the same company are counted. The test corpus holds
# both prod fixture bundles and test-env entities, and 9 company names appear in
# BOTH (Calmr, Angiex, BMI OrganBank, …), so "how many startups did you search"
# has two defensible answers. The spec picks one and the truth set records both
# counts — a coverage denominator must never be an accident.
ENTITY_KEYS = {
    "name": "one company name = one entity (what a user means)",
    "ref": "one (env, entity_type, id|name) record = one entity",
}
SCAN_ENTITY_TYPES = {"startup_company", "cv", "investor", "organization"}
SOURCE_TYPES = {"upload", "website", "eval_premium", "eval_basic", "eval_section",
                "deck_extract", "research_note", "profile"}
# record_summary chunks are model-GENERATED text (Gate 1B made them
# retrieval-only). A phrase that appears only in a summary is not evidence that
# the source says it, so scans cover source blocks unless told otherwise.
GRANULARITIES = {"block", "record_summary"}

# Cross-turn assertions (§8). A script passes only as a sequence; these are the
# assertions that cannot be expressed per turn. Values are the required params;
# the checks themselves are the script runner's job (build step 4).
CROSS_TURN_ASSERTIONS: dict[str, set[str]] = {
    # turn N's answer must operate on the entity set established in turn M
    "set_carryover": {"from_turn", "to_turn"},
    # quotes in turn N must come from the sources established in turn M
    "quotes_from_turn": {"from_turn", "to_turn"},
    # a coverage claim is graded against what the PRIOR turn actually searched
    # (from that turn's bundle, not from the answer's own claims) — §8
    "coverage_claim_grounded": {"turn"},
    # turn N introduces no entity absent from turn M's set
    "no_new_entities": {"from_turn", "to_turn"},
}

BINDING_MODES = {
    # slot appears as {slot} in the question text and is substituted
    "substitute",
    # v2.0 stand-in for a selected-page context (bank §1.1 note): the question
    # keeps its verbatim "this startup" wording and the bound entity is supplied
    # as an explicit selected-entity fixture. Becomes a real context snapshot at
    # Gate 3B / v2.1.
    "selected_entity",
}

_SLOT_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_CASE_ID_RE = re.compile(r"^v2c\d{3}$")
_SCRIPT_ID_RE = re.compile(r"^v2s\d{3}$")


class CaseValidationError(Exception):
    """Every problem in the suite, not just the first — this is an authoring
    tool, and fixing one typo per compile is a waste of a working day."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"{len(errors)} case validation error(s):\n  "
                         + "\n  ".join(errors))


# --- compiled objects -----------------------------------------------------


@dataclass(frozen=True)
class Readiness:
    source_available: bool
    tool_ready: bool
    scope: str
    operation: str
    context_required: str
    golden_priority: str

    def to_dict(self) -> dict[str, Any]:
        return {"source_available": self.source_available,
                "tool_ready": self.tool_ready, "scope": self.scope,
                "operation": self.operation,
                "context_required": self.context_required,
                "golden_priority": self.golden_priority}


@dataclass(frozen=True)
class Grade:
    deterministic: dict[str, Any]       # mechanical checks (§5.2)
    judged: tuple[str, ...]             # judge dimensions
    behavior: tuple[str, ...] = ()      # judged against a targeted rubric (§6)

    def to_dict(self) -> dict[str, Any]:
        return {"deterministic": dict(sorted(self.deterministic.items())),
                "judged": list(self.judged), "behavior": list(self.behavior)}


@dataclass(frozen=True)
class TruthSpec:
    """Scope definition recorded with every truth set (§5.1) — and the input
    `scan_text` will take when it ships, which is why building the grader
    prototypes the tool."""
    mode: str
    entity_type: str
    entity_key: str
    source_types: tuple[str, ...]
    granularity: tuple[str, ...]
    method: str
    any_terms: tuple[str, ...]
    all_terms: tuple[str, ...]
    acl: str = "admin"          # golden runs are admin-scoped; Gate 6 personas
                                # need their own truth sets, not a reuse of these

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "entity_type": self.entity_type,
                "entity_key": self.entity_key,
                "source_types": list(self.source_types),
                "granularity": list(self.granularity), "acl": self.acl,
                "match": {"method": self.method, "any": list(self.any_terms),
                          "all": list(self.all_terms)}}


@dataclass(frozen=True)
class Case:
    id: str
    cls: str                        # YAML key is `class`; that name is a keyword
    tier: str
    provenance: str
    question: str
    bindings: dict[str, str]        # slot -> fixture-pool name
    binding_mode: str
    readiness: Readiness
    expected_route: tuple[str, ...]
    acceptable_routes: tuple[str, ...]
    fallback_contract: str | None
    expected_scope: str
    context_fixture: str | None
    grade: Grade
    truth_spec: TruthSpec | None = None
    blocked_on: str | None = None
    web_required: bool = False
    notes: str | None = None

    @property
    def grading_mode(self) -> str:
        """Honesty until the tool exists, capability after (§4). The suite
        contract string is a digest over these, so the flip is never silent."""
        return "capability" if self.readiness.tool_ready else "honesty"

    @property
    def truth_set(self) -> str | None:
        return self.grade.deterministic.get("truth_set")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "class": self.cls, "tier": self.tier,
                "provenance": self.provenance, "question": self.question,
                "bindings": dict(sorted(self.bindings.items())),
                "binding_mode": self.binding_mode,
                "readiness": self.readiness.to_dict(),
                "expected_route": list(self.expected_route),
                "acceptable_routes": list(self.acceptable_routes),
                "fallback_contract": self.fallback_contract,
                "blocked_on": self.blocked_on,
                "expected_scope": self.expected_scope,
                "context_fixture": self.context_fixture,
                "grade": self.grade.to_dict(),
                "truth_spec": self.truth_spec.to_dict() if self.truth_spec else None,
                "web_required": self.web_required,
                "grading_mode": self.grading_mode}


@dataclass(frozen=True)
class Turn:
    n: int                          # 1-based; the gate unit is the script
    question: str
    grade: Grade

    def to_dict(self) -> dict[str, Any]:
        return {"n": self.n, "question": self.question,
                "grade": self.grade.to_dict()}


@dataclass(frozen=True)
class Script:
    id: str
    cls: str
    tier: str
    provenance: str
    bindings: dict[str, str]
    binding_mode: str
    readiness: Readiness
    turns: tuple[Turn, ...]
    cross_turn: tuple[dict[str, Any], ...]
    fallback_contract: str | None = None
    blocked_on: str | None = None
    notes: str | None = None

    @property
    def grading_mode(self) -> str:
        return "capability" if self.readiness.tool_ready else "honesty"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "class": self.cls, "tier": self.tier,
                "provenance": self.provenance,
                "bindings": dict(sorted(self.bindings.items())),
                "binding_mode": self.binding_mode,
                "readiness": self.readiness.to_dict(),
                "turns": [t.to_dict() for t in self.turns],
                "cross_turn": [dict(sorted(a.items())) for a in self.cross_turn],
                "fallback_contract": self.fallback_contract,
                "blocked_on": self.blocked_on,
                "grading_mode": self.grading_mode}


@dataclass(frozen=True)
class Suite:
    version: str                    # e.g. "v2.0"
    cases: tuple[Case, ...]
    scripts: tuple[Script, ...]
    fixtures: dict[str, list[dict[str, Any]]]
    digest: str
    source_path: Path | None = None

    @property
    def units(self) -> tuple[Case | Script, ...]:
        """Gate units: a script counts once, not once per turn (§8)."""
        return (*self.cases, *self.scripts)

    @property
    def mode_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for unit in self.units:
            counts[unit.grading_mode] = counts.get(unit.grading_mode, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def contract(self) -> str:
        """Scoring-contract string for experiments/compare.py.

        A run graded with case v2c017 on the honesty contract is not comparable
        to one graded after `scan_text` flipped it to capability — same
        question, different bar. Naming the mode *counts* would not survive a
        mixed suite (one case flips, another flips back, counts unchanged), so
        the string carries a digest over every unit's mode. It deliberately
        does NOT include the suite digest: adding a case must not sever
        comparability with prior runs, and the suite digest is recorded
        separately as part of run identity (§4)."""
        modes = json.dumps({u.id: u.grading_mode for u in sorted(
            self.units, key=lambda u: u.id)}, sort_keys=True, separators=(",", ":"))
        return (f"golden-{self.version}/s{SCHEMA_VERSION}"
                f"/modes-{sha256_text(modes)[:8]}")

    def by_id(self, unit_id: str) -> Case | Script | None:
        return next((u for u in self.units if u.id == unit_id), None)

    def identity(self) -> dict[str, Any]:
        """Run-identity fields every v2 result row carries (review criterion 3).
        Resolved bindings and truth-set digests are added by the runner."""
        return {"suite": self.version, "suite_digest": self.digest,
                "schema_version": SCHEMA_VERSION, "scoring_contract": self.contract}


# --- validation helpers ---------------------------------------------------


class _Errors:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, where: str, message: str) -> None:
        self.items.append(f"{where}: {message}")

    def enum(self, where: str, field_name: str, value: Any,
             allowed: set[str] | dict[str, str]) -> bool:
        if value in allowed:
            return True
        self.add(where, f"{field_name}={value!r} is not one of "
                        f"{sorted(allowed)}")
        return False

    def require(self, where: str, cond: bool, message: str) -> bool:
        if not cond:
            self.add(where, message)
        return cond


def _as_bool(errors: _Errors, where: str, field_name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    errors.add(where, f"{field_name} must be true/false, got {value!r}")
    return False


def _compile_readiness(errors: _Errors, where: str, raw: Any) -> Readiness:
    if not isinstance(raw, dict):
        errors.add(where, "readiness block is required (six orthogonal fields; "
                          "the bank's compound tags are not a substitute)")
        raw = {}
    missing = {"source_available", "tool_ready", "scope", "operation",
               "context_required", "golden_priority"} - set(raw)
    if missing:
        errors.add(where, f"readiness is missing {sorted(missing)}")
    unknown = set(raw) - {"source_available", "tool_ready", "scope", "operation",
                          "context_required", "golden_priority"}
    if unknown:
        errors.add(where, f"readiness has unknown field(s) {sorted(unknown)}")
    scope = raw.get("scope")
    operation = raw.get("operation")
    context_required = raw.get("context_required")
    priority = raw.get("golden_priority")
    errors.enum(where, "readiness.scope", scope, SCOPES)
    errors.enum(where, "readiness.operation", operation, OPERATIONS)
    errors.enum(where, "readiness.context_required", context_required,
                CONTEXT_REQUIRED)
    errors.enum(where, "readiness.golden_priority", priority, PRIORITIES)
    return Readiness(
        source_available=_as_bool(errors, where, "readiness.source_available",
                                  raw.get("source_available")),
        tool_ready=_as_bool(errors, where, "readiness.tool_ready",
                            raw.get("tool_ready")),
        scope=scope, operation=operation,
        context_required=context_required, golden_priority=priority)


def _compile_grade(errors: _Errors, where: str, raw: Any) -> Grade:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        errors.add(where, f"grade must be a mapping, got {type(raw).__name__}")
        raw = {}
    unknown = set(raw) - {"deterministic", "judged", "behavior"}
    if unknown:
        errors.add(where, f"grade has unknown block(s) {sorted(unknown)}")

    det = raw.get("deterministic") or {}
    if not isinstance(det, dict):
        errors.add(where, "grade.deterministic must be a mapping")
        det = {}
    for key, value in det.items():
        expected = DETERMINISTIC_CHECKS.get(key)
        if expected is None:
            # the whole reason this compiler exists: an unrecognized check name
            # would grade nothing and report PASS
            errors.add(where, f"unknown deterministic check {key!r} "
                              f"(known: {sorted(DETERMINISTIC_CHECKS)})")
            continue
        if expected is bool and not isinstance(value, bool):
            errors.add(where, f"{key} must be true/false, got {value!r}")
        elif expected is str and not isinstance(value, str):
            errors.add(where, f"{key} must be a string, got {value!r}")
        elif expected is list and not (isinstance(value, list)
                                       and value
                                       and all(isinstance(v, str) for v in value)):
            errors.add(where, f"{key} must be a non-empty list of strings, "
                              f"got {value!r}")

    judged = raw.get("judged") or []
    if not isinstance(judged, list):
        errors.add(where, "grade.judged must be a list")
        judged = []
    for dim in judged:
        errors.enum(where, "grade.judged entry", dim, JUDGED_DIMENSIONS)

    behavior = raw.get("behavior") or []
    if not isinstance(behavior, list):
        errors.add(where, "grade.behavior must be a list")
        behavior = []
    for obligation in behavior:
        errors.enum(where, "grade.behavior entry", obligation, BEHAVIOR_OBLIGATIONS)

    errors.require(where, bool(det or judged or behavior),
                   "grade block is empty — the case would assert nothing")
    return Grade(deterministic=dict(det), judged=tuple(judged),
                 behavior=tuple(behavior))


def _compile_truth_spec(errors: _Errors, where: str, raw: Any) -> TruthSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.add(where, "truth_spec must be a mapping")
        return None
    unknown = set(raw) - {"mode", "entity_type", "entity_key", "source_types",
                          "granularity", "match", "acl"}
    if unknown:
        errors.add(where, f"truth_spec has unknown field(s) {sorted(unknown)}")

    mode = raw.get("mode", "matched")
    errors.enum(where, "truth_spec.mode", mode, TRUTH_MODES)
    entity_type = raw.get("entity_type")
    errors.enum(where, "truth_spec.entity_type", entity_type, SCAN_ENTITY_TYPES)
    entity_key = raw.get("entity_key", "name")
    errors.enum(where, "truth_spec.entity_key", entity_key, ENTITY_KEYS)

    source_types = tuple(raw.get("source_types") or ())
    errors.require(where, bool(source_types),
                   "truth_spec.source_types is required — an unbounded scan has "
                   "no coverage denominator to grade against")
    for st in source_types:
        errors.enum(where, "truth_spec.source_types entry", st, SOURCE_TYPES)
    granularity = tuple(raw.get("granularity") or ("block",))
    for g in granularity:
        errors.enum(where, "truth_spec.granularity entry", g, GRANULARITIES)

    match = raw.get("match") or {}
    if not isinstance(match, dict):
        errors.add(where, "truth_spec.match must be a mapping")
        match = {}
    unknown_match = set(match) - {"method", "any", "all"}
    if unknown_match:
        errors.add(where, f"truth_spec.match has unknown field(s) "
                          f"{sorted(unknown_match)}")
    method = match.get("method", "phrase")
    errors.enum(where, "truth_spec.match.method", method, MATCH_METHODS)
    any_terms = tuple(match.get("any") or ())
    all_terms = tuple(match.get("all") or ())
    for label, terms in (("any", any_terms), ("all", all_terms)):
        for term in terms:
            if not isinstance(term, str) or not term.strip():
                errors.add(where, f"truth_spec.match.{label} entry {term!r} is "
                                  "not a non-empty string")
    errors.require(where, bool(any_terms or all_terms),
                   "truth_spec.match needs at least one term in `any` or `all`")

    return TruthSpec(mode=mode, entity_type=entity_type, entity_key=entity_key,
                     source_types=source_types, granularity=granularity,
                     method=method, any_terms=any_terms, all_terms=all_terms,
                     acl=raw.get("acl", "admin"))


def _check_slots(errors: _Errors, where: str, question: str,
                 bindings: dict[str, str], binding_mode: str,
                 readiness: Readiness) -> None:
    """Slot discipline (§7.1). A slot in the text with no binding resolves to
    nothing; a binding no one uses is dead weight that still perturbs the
    seeded pick for every other slot."""
    used = set(_SLOT_RE.findall(question))
    declared = set(bindings)
    if binding_mode == "substitute":
        for slot in sorted(used - declared):
            errors.add(where, f"question uses {{{slot}}} with no matching entry "
                              f"in bindings")
        for slot in sorted(declared - used):
            errors.add(where, f"binding {slot!r} is declared but never appears "
                              f"in the question (binding_mode: substitute)")
    elif binding_mode == "selected_entity":
        if used:
            errors.add(where, f"binding_mode: selected_entity keeps the question "
                              f"verbatim, but it contains {sorted(used)}")
        errors.require(where, len(declared) == 1,
                       "binding_mode: selected_entity needs exactly one slot, "
                       f"got {sorted(declared)}")
        errors.require(where, readiness.context_required == "selected",
                       "binding_mode: selected_entity requires "
                       "readiness.context_required: selected")


def _check_readiness_coherence(errors: _Errors, where: str, readiness: Readiness,
                               expected_route: tuple[str, ...],
                               fallback_contract: str | None,
                               blocked_on: str | None,
                               grade: Grade, suite_version: str,
                               in_script: bool = False) -> None:
    """The cross-checks that keep the six fields from drifting into decoration."""
    future = sorted(set(expected_route) & FUTURE_TOOLS)
    if future and readiness.tool_ready:
        errors.add(where, f"tool_ready: true but expected_route names {future}, "
                          f"which does not exist yet")
    if readiness.tool_ready and blocked_on:
        errors.add(where, f"tool_ready: true but blocked_on={blocked_on!r}")
    if not readiness.tool_ready and not (blocked_on or future):
        # the compound-tag failure the bank review rejected: "not ready" with no
        # statement of what is missing is not a readiness field, it is a mood
        errors.add(where, "tool_ready: false must name what it waits on — set "
                          f"blocked_on ({sorted(BLOCKED_ON)}) or route to a "
                          "future tool")

    # A not-ready case is admissible now precisely because it declares what it
    # is graded on instead (§4). A ready case with a fallback would be graded
    # on the weaker bar forever.
    if readiness.tool_ready and fallback_contract:
        errors.add(where, f"fallback_contract {fallback_contract!r} set on a "
                          "tool_ready case — capability grading applies")
    if not readiness.tool_ready and not fallback_contract:
        errors.add(where, "tool_ready: false requires a fallback_contract "
                          "(what the case is graded on today, §4)")

    if fallback_contract == "coverage_disclosure":
        errors.require(where,
                       grade.deterministic.get("must_disclose_coverage") is True,
                       "fallback_contract: coverage_disclosure requires "
                       "must_disclose_coverage: true — the contract IS the check")

    # §2: v2.0 is the context-free wave plus fixture-bound SEL. Working-set and
    # prior-answer context wait for Gate 3B (v2.1); prior_answer is admissible
    # only inside a script, where a prior turn actually establishes it.
    deferred = {"working_set"} if in_script else {"working_set", "prior_answer"}
    if suite_version.startswith("v2.0") and readiness.context_required in deferred:
        errors.add(where, f"context_required: {readiness.context_required} is a "
                          "v2.1 case (§2) — v2.0 covers context-free cases plus "
                          "selected-entity fixtures; prior_answer belongs in a "
                          "script")


def _check_class_contract(errors: _Errors, where: str, cls: str, case_id: str,
                          grade: Grade, truth_spec: TruthSpec | None) -> None:
    """Class-specific obligations from §5.1/§6/§7.3."""
    det = grade.deterministic
    truth_set = det.get("truth_set")
    if truth_set is not None:
        expected = f"truth/{case_id}.json"
        if truth_set != expected:
            errors.add(where, f"truth_set must be {expected!r} (keyed by case id "
                              f"so an oracle cannot be cross-wired), got "
                              f"{truth_set!r}")

    # the two halves of a computed oracle: the file the grader reads and the
    # spec the builder ran. One without the other is a truth set nobody can
    # rebuild, or a scan nobody grades against.
    if truth_set and not truth_spec:
        errors.add(where, "truth_set requires a truth_spec — an oracle that "
                          "cannot be recomputed from its scope definition is "
                          "hand-authored by another name (§5.1)")
    if truth_spec and not truth_set:
        errors.add(where, "truth_spec is set but grade.deterministic.truth_set "
                          "is not — nothing would read the scan")
    if truth_spec:
        expected_mode = ("absent" if cls == "known_absence" else "matched")
        errors.require(where, truth_spec.mode == expected_mode,
                       f"class {cls!r} needs truth_spec.mode: {expected_mode!r} "
                       f"(got {truth_spec.mode!r})")
    if cls in REQUIRE_TRUTH_SET:
        # §7.3: the exact-scan oracle comes from the corpus, so the author
        # cannot bake in a friendly answer key. §6: absence is verified offline
        # by the same builder in must-be-empty mode.
        errors.require(where, bool(truth_set),
                       f"class {cls!r} requires a computed truth_set (§5.1/§7.3) "
                       "— its oracle may not be hand-authored")
    if cls in NO_HAND_AUTHORED_KEY:
        errors.require(where, "must_mention_all" not in det,
                       f"class {cls!r} may not use must_mention_all: that is a "
                       "hand-authored answer key, exactly what v2 exists to "
                       "avoid (§7.3)")

    required_behavior = {
        "false_premise": "correct_premise",
        "known_absence": "state_absence",
        "ambiguity_surfacing": "surface_ambiguity",
        "conflict_surfacing": "surface_conflict",
        "decline_action": "decline_action",
        "clarification_seeking": "ask_clarifying",
        "capability_disclosure": "disclose_capabilities",
    }.get(cls)
    if required_behavior:
        errors.require(where, required_behavior in grade.behavior,
                       f"class {cls!r} requires grade.behavior to include "
                       f"{required_behavior!r} — otherwise the behavior it "
                       "exists to test is ungraded")
    if cls == "evidence_fidelity":
        # this one IS mechanical: a quoted span either appears verbatim in the
        # cited evidence or it does not
        errors.require(where, det.get("must_quote_verbatim") is True,
                       "class 'evidence_fidelity' requires must_quote_verbatim: true")
    if cls == "injection_canary":
        errors.require(where, bool(det.get("must_not_mention")),
                       "class 'injection_canary' requires must_not_mention "
                       "(the payload the answer must never carry out or echo)")


def _compile_case(errors: _Errors, raw: Any, suite_version: str,
                  fixtures: dict[str, Any]) -> Case | None:
    if not isinstance(raw, dict):
        errors.add("<case>", f"expected a mapping, got {type(raw).__name__}")
        return None
    case_id = str(raw.get("id") or "").strip()
    where = case_id or "<case with no id>"
    if not _CASE_ID_RE.match(case_id):
        errors.add(where, "id must match v2cNNN (e.g. v2c017)")

    known = {"id", "class", "tier", "provenance", "question", "bindings",
             "binding_mode", "readiness", "expected_route", "acceptable_routes",
             "fallback_contract", "blocked_on", "expected_scope",
             "context_fixture", "grade", "truth_spec", "web_required", "notes"}
    unknown = set(raw) - known
    if unknown:
        errors.add(where, f"unknown field(s) {sorted(unknown)}")

    cls = raw.get("class")
    errors.enum(where, "class", cls, CLASSES)
    tier = raw.get("tier")
    errors.enum(where, "tier", tier, TIERS)

    # §3 curation: every case says where it came from, so the provenance finding
    # that motivated v2 can never be repeated silently.
    provenance = str(raw.get("provenance") or "").strip()
    errors.require(where, bool(provenance),
                   "provenance is required (bank row or LFT thread)")

    question = str(raw.get("question") or "").strip()
    errors.require(where, bool(question), "question is required")

    bindings = raw.get("bindings") or {}
    if not isinstance(bindings, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in bindings.items()):
        errors.add(where, "bindings must be a mapping of slot -> fixture pool")
        bindings = {}
    for slot, pool in sorted(bindings.items()):
        if pool not in fixtures:
            errors.add(where, f"binding {slot!r} names fixture pool {pool!r}, "
                              f"which is not in the registry")

    binding_mode = raw.get("binding_mode") or "substitute"
    errors.enum(where, "binding_mode", binding_mode, BINDING_MODES)

    readiness = _compile_readiness(errors, where, raw.get("readiness"))

    expected_route = tuple(raw.get("expected_route") or [])
    for tool in expected_route:
        errors.enum(where, "expected_route entry", tool, ROUTE_TOOLS)
    acceptable_routes = tuple(raw.get("acceptable_routes") or [])
    for route in acceptable_routes:
        errors.enum(where, "acceptable_routes entry", route, FUNNEL_ROUTES)

    web_required = bool(raw.get("web_required"))
    if "web_research" in expected_route and not web_required:
        # §3 curation: web-required cases stay parked until E3, same as v1
        # practice — but they must say so rather than silently costing a search.
        errors.add(where, "expected_route names web_research, so the case must "
                          "declare web_required: true (parked until E3, §3)")

    fallback_contract = raw.get("fallback_contract")
    if fallback_contract is not None:
        errors.enum(where, "fallback_contract", fallback_contract,
                    FALLBACK_CONTRACTS)

    blocked_on = raw.get("blocked_on")
    if blocked_on is not None:
        errors.enum(where, "blocked_on", blocked_on, BLOCKED_ON)
    elif not readiness.tool_ready:
        # routing to a future tool already says what the case waits on; carry it
        # into the compiled object so a triage reader never has to infer it
        blocked_on = next((t for t in expected_route if t in FUTURE_TOOLS), None)

    # expected_scope defaults to the readiness scope — they are the same concept
    # (what the answer must cover); an explicit value is allowed where grading
    # scope legitimately differs from the question's scope.
    expected_scope = raw.get("expected_scope") or readiness.scope
    errors.enum(where, "expected_scope", expected_scope, SCOPES)

    context_fixture = raw.get("context_fixture")
    if context_fixture is not None and suite_version.startswith("v2.0"):
        errors.add(where, "context_fixture is a v2.1 field (Gate 3B) — v2.0 "
                          "binds selected entities via binding_mode")

    grade = _compile_grade(errors, where, raw.get("grade"))
    truth_spec = _compile_truth_spec(errors, where, raw.get("truth_spec"))

    if question:
        _check_slots(errors, where, question, bindings, binding_mode, readiness)
    _check_readiness_coherence(errors, where, readiness, expected_route,
                               fallback_contract, blocked_on, grade,
                               suite_version)
    if cls in CLASSES:
        _check_class_contract(errors, where, cls, case_id, grade, truth_spec)

    return Case(id=case_id, cls=cls, tier=tier, provenance=provenance,
                question=question, bindings=dict(bindings),
                binding_mode=binding_mode, readiness=readiness,
                expected_route=expected_route,
                acceptable_routes=acceptable_routes,
                fallback_contract=fallback_contract, blocked_on=blocked_on,
                expected_scope=expected_scope, context_fixture=context_fixture,
                grade=grade, truth_spec=truth_spec, web_required=web_required,
                notes=raw.get("notes"))


def _compile_script(errors: _Errors, raw: Any, suite_version: str,
                    fixtures: dict[str, Any]) -> Script | None:
    if not isinstance(raw, dict):
        errors.add("<script>", f"expected a mapping, got {type(raw).__name__}")
        return None
    script_id = str(raw.get("id") or "").strip()
    where = script_id or "<script with no id>"
    if not _SCRIPT_ID_RE.match(script_id):
        errors.add(where, "id must match v2sNNN (e.g. v2s001)")

    known = {"id", "class", "tier", "provenance", "bindings", "binding_mode",
             "readiness", "turns", "cross_turn", "fallback_contract",
             "blocked_on", "notes"}
    unknown = set(raw) - known
    if unknown:
        errors.add(where, f"unknown field(s) {sorted(unknown)}")

    cls = raw.get("class")
    errors.enum(where, "class", cls, CLASSES)
    tier = raw.get("tier")
    errors.enum(where, "tier", tier, TIERS)
    provenance = str(raw.get("provenance") or "").strip()
    errors.require(where, bool(provenance),
                   "provenance is required (SCR script or LFT thread)")

    bindings = raw.get("bindings") or {}
    if not isinstance(bindings, dict):
        errors.add(where, "bindings must be a mapping of slot -> fixture pool")
        bindings = {}
    for slot, pool in sorted(bindings.items()):
        if pool not in fixtures:
            errors.add(where, f"binding {slot!r} names fixture pool {pool!r}, "
                              f"which is not in the registry")
    binding_mode = raw.get("binding_mode") or "substitute"
    errors.enum(where, "binding_mode", binding_mode, BINDING_MODES)
    readiness = _compile_readiness(errors, where, raw.get("readiness"))

    fallback_contract = raw.get("fallback_contract")
    if fallback_contract is not None:
        errors.enum(where, "fallback_contract", fallback_contract,
                    FALLBACK_CONTRACTS)
    blocked_on = raw.get("blocked_on")
    if blocked_on is not None:
        errors.enum(where, "blocked_on", blocked_on, BLOCKED_ON)

    raw_turns = raw.get("turns") or []
    if not isinstance(raw_turns, list):
        errors.add(where, "turns must be a list")
        raw_turns = []
    # a one-turn script is a case; the point of §8 is what only a sequence can
    # test (coreference, working-set carryover, evidence fidelity over a set)
    errors.require(where, len(raw_turns) >= 2,
                   f"a script needs at least 2 turns, got {len(raw_turns)}")

    turns: list[Turn] = []
    declared = set(bindings)
    for i, raw_turn in enumerate(raw_turns, 1):
        turn_where = f"{where} turn {i}"
        if not isinstance(raw_turn, dict):
            errors.add(turn_where, "each turn must be a mapping")
            continue
        unknown_turn = set(raw_turn) - {"question", "grade", "notes"}
        if unknown_turn:
            errors.add(turn_where, f"unknown field(s) {sorted(unknown_turn)}")
        question = str(raw_turn.get("question") or "").strip()
        errors.require(turn_where, bool(question), "question is required")
        # slots are declared once for the whole script and may appear in any turn
        for slot in sorted(set(_SLOT_RE.findall(question)) - declared):
            errors.add(turn_where, f"turn uses {{{slot}}} with no matching entry "
                                   f"in the script's bindings")
        turns.append(Turn(n=i, question=question,
                          grade=_compile_grade(errors, turn_where,
                                               raw_turn.get("grade"))))

    if binding_mode == "substitute" and declared:
        used = {s for t in turns for s in _SLOT_RE.findall(t.question)}
        for slot in sorted(declared - used):
            errors.add(where, f"binding {slot!r} is declared but appears in no "
                              f"turn (binding_mode: substitute)")

    raw_cross = raw.get("cross_turn") or []
    if not isinstance(raw_cross, list):
        errors.add(where, "cross_turn must be a list")
        raw_cross = []
    cross_turn: list[dict[str, Any]] = []
    for i, assertion in enumerate(raw_cross, 1):
        a_where = f"{where} cross_turn[{i}]"
        if not isinstance(assertion, dict) or "type" not in assertion:
            errors.add(a_where, "each assertion needs a type and its params")
            continue
        kind = assertion["type"]
        if not errors.enum(a_where, "type", kind, CROSS_TURN_ASSERTIONS):
            continue
        required = CROSS_TURN_ASSERTIONS[kind]
        params = {k: v for k, v in assertion.items() if k != "type"}
        missing = required - set(params)
        if missing:
            errors.add(a_where, f"{kind} requires {sorted(missing)}")
        extra = set(params) - required
        if extra:
            errors.add(a_where, f"{kind} got unknown param(s) {sorted(extra)}")
        for key in sorted(required & set(params)):
            value = params[key]
            if not isinstance(value, int) or not 1 <= value <= len(turns):
                errors.add(a_where, f"{key}={value!r} is not a turn number in "
                                    f"1..{len(turns)}")
        if {"from_turn", "to_turn"} <= set(params):
            errors.require(a_where,
                           isinstance(params["from_turn"], int)
                           and isinstance(params["to_turn"], int)
                           and params["from_turn"] < params["to_turn"],
                           "from_turn must come before to_turn")
        cross_turn.append(dict(assertion))

    # §8: the gate unit is the script, and what makes it a script rather than a
    # batch of turns is at least one assertion that spans them.
    errors.require(where, bool(cross_turn),
                   "a script needs at least one cross_turn assertion — without "
                   "one it is just consecutive cases (§8)")

    # readiness applies to the script as a unit; its checks are the union over
    # turns (a coverage contract is satisfied by the turn that discloses).
    union: dict[str, Any] = {}
    for turn in turns:
        for key, value in turn.grade.deterministic.items():
            if value is True or key not in union:
                union[key] = value
    _check_readiness_coherence(errors, where, readiness, (), fallback_contract,
                               blocked_on, Grade(union, ()), suite_version,
                               in_script=True)

    return Script(id=script_id, cls=cls, tier=tier, provenance=provenance,
                  bindings=dict(bindings), binding_mode=binding_mode,
                  readiness=readiness, turns=tuple(turns),
                  cross_turn=tuple(cross_turn),
                  fallback_contract=fallback_contract, blocked_on=blocked_on,
                  notes=raw.get("notes"))


# --- fixtures + seeded binding resolution (§7.1) --------------------------


FIXTURE_KEYS = {"entity_type", "name", "entity_id", "note"}


def load_fixtures(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Entity-slot fixture registry: pool name -> candidate entities.

    Pools are named by the PROPERTY they guarantee (`has_regulatory_mention`),
    never by a company, so a case can say what it needs without naming what it
    will get. Pools whose membership is corpus-derived are generated by the
    truth-set builder (build step 2) — this file holds the hand-listed ones.
    """
    pools: dict[str, Any] = {}
    errors: list[str] = []
    # hand-listed registry + the machine-generated pools beside it; a name
    # defined in both is ambiguous, not a merge
    for source in (path, path.with_name("fixtures.generated.yaml")):
        if not source.exists():
            continue
        raw = yaml.safe_load(source.read_text()) or {}
        found = raw.get("pools") or {}
        if not isinstance(found, dict):
            raise CaseValidationError([f"{source.name}: `pools` must be a mapping"])
        for name, entries in found.items():
            if name in pools:
                errors.append(f"{source.name}: pool {name!r} is already defined "
                              "in fixtures.yaml")
            pools[name] = entries
    for pool, entries in pools.items():
        if not isinstance(entries, list):
            errors.append(f"{path.name}: pool {pool!r} must be a list")
            continue
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict) or not entry.get("name"):
                errors.append(f"{path.name}: pool {pool!r}[{i}] needs a name")
                continue
            unknown = set(entry) - FIXTURE_KEYS
            if unknown:
                errors.append(f"{path.name}: pool {pool!r}[{i}] has unknown "
                              f"key(s) {sorted(unknown)}")
    if errors:
        raise CaseValidationError(errors)
    return pools


def resolve_bindings(unit: Case | Script, *, seed: int | str,
                     fixtures: dict[str, list[dict[str, Any]]]
                     ) -> dict[str, dict[str, Any]]:
    """Bind entity slots for one run — deterministic in (seed, case, slot, pool).

    This is David's P4 §9 rule ("avoid overusing a single company name") made
    mechanical, and the contamination control §7.1 asks for: the author writes
    a property, the run picks the entity, and paired runs pin the same seed so
    a comparison never moves the entities underneath the numbers. The case id
    is in the hash so two cases in one run do not collide on the same company.
    """
    resolved: dict[str, dict[str, Any]] = {}
    for slot, pool_name in sorted(unit.bindings.items()):
        pool = fixtures.get(pool_name)
        if not pool:
            # loud, not a silent skip: an empty pool means the case was never
            # actually exercised, which is worse than a failing case
            raise CaseValidationError(
                [f"{unit.id}: fixture pool {pool_name!r} is "
                 f"{'missing' if pool is None else 'empty'} — cannot bind slot "
                 f"{slot!r}"])
        candidates = sorted(pool, key=lambda e: (e["name"], e.get("entity_id") or 0))
        h = hashlib.sha256(f"{seed}|{unit.id}|{slot}|{pool_name}".encode())
        resolved[slot] = candidates[int.from_bytes(h.digest()[:8], "big")
                                    % len(candidates)]
    return resolved


def render_question(text: str, bound: dict[str, dict[str, Any]]) -> str:
    """Substitute `{slot}` with the bound entity's name. Unbound slots are left
    intact and will fail the compile, never silently render as empty."""
    def sub(match: re.Match) -> str:
        entry = bound.get(match.group(1))
        return entry["name"] if entry else match.group(0)
    return _SLOT_RE.sub(sub, text)


# --- compile --------------------------------------------------------------


def compile_suite(path: Path, *, fixtures_path: Path | None = None) -> Suite:
    """YAML → validated Suite. Raises CaseValidationError listing every problem."""
    raw = yaml.safe_load(path.read_text()) or {}
    version = str(raw.get("version") or "").strip()
    errors = _Errors()
    if not re.match(r"^v2\.\d+$", version):
        errors.add(path.name, f"version must look like v2.0, got {version!r}")

    fixtures_path = fixtures_path or (path.parent / "fixtures.yaml")
    fixtures = load_fixtures(fixtures_path)

    cases = [c for c in (_compile_case(errors, raw_case, version, fixtures)
                         for raw_case in (raw.get("cases") or [])) if c]
    scripts = [s for s in (_compile_script(errors, raw_script, version, fixtures)
                           for raw_script in (raw.get("scripts") or [])) if s]

    seen: dict[str, str] = {}
    for unit in (*cases, *scripts):
        if unit.id in seen:
            errors.add(unit.id, "duplicate id")
        seen[unit.id] = unit.id
    # bank §0 curation rule made mechanical: repeated historical copies of one
    # question count once, not as independent demand
    texts: dict[str, str] = {}
    for case in cases:
        key = " ".join(case.question.lower().split())
        if key in texts:
            errors.add(case.id, f"question text duplicates {texts[key]} — "
                                "curate, don't copy (bank §0)")
        texts[key] = case.id

    if errors.items:
        raise CaseValidationError(errors.items)

    # sorted by id: reordering the YAML file does not change the suite, editing
    # a case does. The digest is what a result row points at when it says which
    # suite graded it (§4 run identity).
    body = json.dumps({"schema_version": SCHEMA_VERSION, "version": version,
                       "cases": [c.to_dict() for c in sorted(cases, key=lambda c: c.id)],
                       "scripts": [s.to_dict() for s in sorted(scripts, key=lambda s: s.id)]},
                      sort_keys=True, separators=(",", ":"))
    return Suite(version=version, cases=tuple(cases), scripts=tuple(scripts),
                 fixtures=fixtures, digest=sha256_text(body), source_path=path)


def load_suite(name: str = "v2") -> Suite:
    return compile_suite(GOLDEN_DIR / f"{name}.yaml")


# --- CLI ------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", default="v2", help="suite file under golden/")
    ap.add_argument("--seed", default=None,
                    help="resolve entity bindings with this seed and show them")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        suite = load_suite(args.golden)
    except CaseValidationError as exc:
        print(f"INVALID ({len(exc.errors)} error(s)):", file=sys.stderr)
        for err in exc.errors:
            print(f"  {err}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as exc:
        sys.exit(str(exc))

    if args.json:
        out = {**suite.identity(),
               "cases": [c.to_dict() for c in suite.cases],
               "scripts": [s.to_dict() for s in suite.scripts]}
        print(json.dumps(out, indent=2))
        return

    print(f"== golden {suite.version} ==  ({suite.source_path})")
    print(f"cases: {len(suite.cases)}   scripts: {len(suite.scripts)}   "
          f"turns: {sum(len(s.turns) for s in suite.scripts)}")
    print(f"suite digest:     {suite.digest[:16]}…")
    print(f"scoring contract: {suite.contract}")
    print(f"grading modes:    {suite.mode_counts or '-'}")

    by_tier: dict[str, int] = {}
    for unit in suite.units:
        by_tier[unit.tier] = by_tier.get(unit.tier, 0) + 1
    print(f"tiers:            {dict(sorted(by_tier.items())) or '-'}")

    print("\n-- composition (§3) --")
    by_class: dict[str, list[str]] = {}
    for unit in suite.units:
        by_class.setdefault(unit.cls, []).append(unit.id)
    for cls in CLASSES:
        ids = by_class.get(cls, [])
        print(f"  {cls:<24} {len(ids):>3}  {CLASSES[cls]}")
    parked = [c.id for c in suite.cases if c.web_required]
    if parked:
        print(f"\nweb-required (parked until E3): {', '.join(parked)}")

    if args.seed is not None:
        print(f"\n-- bindings @ seed {args.seed} --")
        for unit in suite.units:
            if not unit.bindings:
                continue
            bound = resolve_bindings(unit, seed=args.seed, fixtures=suite.fixtures)
            shown = ", ".join(f"{k}={v['name']}" for k, v in sorted(bound.items()))
            print(f"  {unit.id:<8} {shown}")
            if isinstance(unit, Case) and unit.binding_mode == "substitute":
                print(f"           {render_question(unit.question, bound)}")


if __name__ == "__main__":
    main()
