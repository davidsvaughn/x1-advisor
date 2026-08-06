"""Global deterministic checkers (Gate 4 build step 2; GOLDEN-V2-DESIGN §5.2).

The funnel says the loss is synthesis and citation discipline —
`synthesis_error` 19, `citation_coverage_error` 15 on the 1D-8 rerun. The
cheapest slice of that is mechanical: a number in an answer that appears
nowhere in the evidence, a company that exists only in the answer, a coverage
claim with no stated scope. None of it needs a judge, and unlike the judge it
does not move ±0.2–0.4 between runs (1E-4).

**Diagnostics before gates (review criterion 4).** Everything here ships as a
diagnostic: recorded per answer, surfaced in the funnel, gating nothing. A
check is promoted to a comparator gate only after a false-positive audit shows
it does not reject legitimate answers — numeric grounding especially, because
legitimate answers *derive* numbers (counting listed items, date arithmetic).
`Diagnostic.gating` is False for every check in this module, deliberately, and
flipping one is a decision with an audit behind it, not an edit here.

These are heuristics operating on prose, and each one names its own blind spots
in its docstring rather than pretending to be exact. That is the point of
shipping them as diagnostics first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, NamedTuple

# criterion 4: nothing in this module gates a comparison yet
GATING = False


@dataclass(frozen=True)
class Diagnostic:
    check: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)
    gating: bool = GATING

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.check, "passed": self.passed,
                "gating": self.gating, "detail": self.detail}


# --- text preparation -----------------------------------------------------

# validate_citations rewrites refs to [1] / [1,2] before an answer is stored,
# so a naive numeral scan would flag every citation marker in the corpus of
# answers. Strip them first — they are not claims.
_CITATION_RE = re.compile(r"\[\d+(?:\s*,\s*\d+)*\]")
_NUMERAL_RE = re.compile(r"(?<![\w./-])[-+]?\$?\d[\d,]*(?:\.\d+)?\s?%?")
_QUOTE_RE = re.compile(r"[\"“]([^\"“”]{8,})[\"”]")


def strip_citations(text: str) -> str:
    return _CITATION_RE.sub(" ", text or "")


def normalize_numeral(raw: str) -> str:
    """`$1,200.00` → `1200`, `38 %` → `38`. Formatting-tolerant per §5.2:
    currency symbols, thousands separators and percent signs are presentation,
    not different numbers."""
    cleaned = raw.strip().replace(",", "").replace("$", "").replace("%", "")
    cleaned = cleaned.replace(" ", "").lstrip("+")
    if "." in cleaned:
        cleaned = cleaned.rstrip("0").rstrip(".")
    return cleaned or "0"


def numerals(text: str) -> list[str]:
    return [normalize_numeral(m.group()) for m in _NUMERAL_RE.finditer(text or "")]


def _norm_ws(text: str) -> str:
    return " ".join((text or "").split())


# Sentence-initial capitals are not entities. This list is deliberately small:
# a big stoplist hides real misses, and the check is a diagnostic, so a
# false positive costs a line in a report, not a failed run.
_NOT_ENTITIES = {
    "the", "this", "that", "these", "those", "there", "here", "it", "its",
    "i", "we", "you", "they", "he", "she", "his", "her", "their", "our",
    "a", "an", "and", "but", "or", "so", "if", "when", "while", "however",
    "based", "according", "note", "notably", "overall", "across", "both",
    "no", "none", "not", "yes", "however", "although", "though", "because",
    "for", "from", "in", "on", "at", "by", "with", "without", "per",
    "several", "many", "most", "some", "each", "every", "all", "only",
    "search", "searched", "summary", "sources", "source", "evidence",
    "evaluation", "evaluations", "startup", "startups", "company", "companies",
    "document", "documents", "deck", "decks", "cv", "cvs", "regulatory",
}
# `and` is deliberately NOT a connector: "BMI OrganBank and Fabricorp" is a list
# of two companies far more often than one company's name, and merging them
# hides the invented one inside a blob that matches nothing.
# Words join only across spaces/tabs, never a newline: `\s+` glued a
# sentence-final name to the next paragraph's first capitalized word, which
# inflated the entity-grounding counts (2026-08-01 triage, machinery item a).
_ENTITY_RE = re.compile(
    r"\b[A-Z][\w.&'’\-]*(?:[ \t]+(?:of|for|de|von|van|der|del)[ \t]+[A-Z][\w.&'’\-]*"
    r"|[ \t]+[A-Z][\w.&'’\-]*)*")


def entity_mentions(text: str) -> list[str]:
    """Capitalized runs that look like named entities.

    Heuristic and known to be imperfect in both directions: it misses corpus
    names that do not start with a capital (`2ndCourt.com`) and over-collects at
    sentence starts. Sentence-initial ordinary words are kept on purpose — a
    single-word company (`Fabricorp`, `Calmr`) is indistinguishable from them by
    shape, and dropping the shape would drop exactly the invented names this
    check exists to find. They cost little: an ordinary word almost always
    appears somewhere in the evidence and grounds itself. The residue is what
    the false-positive audit reads before this could ever gate.
    """
    out: list[str] = []
    for match in _ENTITY_RE.finditer(text or ""):
        # trailing sentence punctuation is not part of a name: "Calmr." would
        # otherwise never match the evidence's "Calmr" and report itself as an
        # invented company. Internal dots stay — `2ndCourt.com`, `Ph.D`.
        candidate = match.group().strip().rstrip(".,;:'’-")
        words = candidate.split()
        if not words:
            continue
        if all(w.lower().strip(".,;:") in _NOT_ENTITIES for w in words):
            continue
        out.append(candidate)
    return out


# --- known-name search + assertion polarity --------------------------------

# Sentence-level negation markers. Deliberately coarse: the unit of scope is
# the sentence, so "ButterBeKind's evaluation does not mention synthetic
# biology" reads as a NEGATED mention of ButterBeKind. Blind spots, stated:
# double negation and a positive and negative claim sharing one sentence
# mis-classify. A name asserted positively in ANY sentence counts as
# positive, so hedged repeats cannot hide a real claim.
_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|none|neither|nor|without|lacks?|lacking|absent|absence|"
    r"missing|unmentioned|zero|cannot|can't|couldn't|won't|wouldn't|doesn't|"
    r"don't|didn't|isn't|aren't|wasn't|weren't|does\s+not|do\s+not|did\s+not|"
    r"fail(?:s|ed)?\s+to|found\s+no|no\s+(?:mention|evidence|match(?:es)?|"
    r"hits?|results?|reference))\b", re.I)
# Hedges that contain a negation token without denying the claim: "consulting-
# related wording — not necessarily a consulting background: A, B, C" REPORTS
# A, B, C as matched; scoring it as denial punishes exactly the qualified
# disclosure rule 7 demands. Masked before the negation test. The second
# alternative is the trailing form — "not procurement friction per se, but
# hospital-adoption friction" is a qualified yes; the gap is capped and stops
# at clause punctuation so a genuine denial two clauses away is never masked.
_HEDGE_RE = re.compile(
    r"\bnot\s+(?:necessarily|always|only|exclusively|solely|merely|just)\b"
    r"|\bnot\b[^.!?;:—]{0,60}?\bper\s+se\b",
    re.I)
# Census framing (prompt rule 5): the agent must NAME every lexical hit, and
# may file hits it judges irrelevant in a labeled group — "the following
# appear to be unrelated to X: A, B, C". Those names reached the reader, so
# the census is complete (recall credit), but nothing was asserted as matching
# (no overclaim liability). Blind spots, stated: the markers are lexical — an
# exclusion phrased without them ("these are noise") grades positive, and a
# real claim sharing a sentence with an exclusion marker is masked.
_EXCLUSION_RE = re.compile(
    r"\b(?:unrelated\s+to|not\s+related\s+to|irrelevant|incidental|"
    r"exclud(?:ed|ing)\s+(?:them|these|those|it|from))\b", re.I)
# Scope enumeration names the INPUTS examined, never verdicts on them: "The
# scan covered: A, B, C" reports what was searched, not what matched. Neither
# recall credit nor overclaim liability. "found"/"matched" are deliberately
# NOT scope verbs — "the scan found matches in: A, B" asserts.
_SCOPE_RE = re.compile(
    r"\b(?:scan|scans|search|searches|query|queries)\b[^.!?]{0,40}?"
    r"\b(?:covered|include[ds]?|spanned|examined)\b"
    r"|\bcovered\s+by\s+(?:the|this)\s+(?:scan|search)\b", re.I)
# A census group is usually FORMATTED as a list: a framing sentence ending in
# a colon, then one name per bullet. Sentence-splitting on newlines severs
# the bullets from their framing, so items inherit the census bucket of the
# introducing line ("…five incidental mentions:") until prose resumes. Only
# census framing is inherited — a plain intro ("The 9 are:") leaves its items
# positive, and an item's own negation always beats the inherited bucket.
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•+]|\d{1,3}[.)])\s")


def _census_framing(sentence: str) -> str | None:
    if _SCOPE_RE.search(sentence):
        return "scope"
    if _EXCLUSION_RE.search(sentence):
        return "excluded"
    return None
# The period after a single-capital initial is not a sentence end: splitting
# "Randolph W. Hubbell" mid-name means the full name can never be found in
# any one sentence, structurally capping recall below 1.0 for every answer
# that names him. ("U.S. market" stops splitting too, for the same reason.)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])(?<![A-Z]\.)\s+|\n+")


def names_in_text(text: str, names: Iterable[str]) -> set[str]:
    """Which of these KNOWN names appear in the text (case-insensitive,
    word-boundary-guarded)? Returns the matches lowercased.

    This searches for names rather than parsing the text into candidate
    entities, because parsing misses everything that does not look like a
    capitalized run — `2ndCourt.com`, lowercase brand names, names split by a
    line break. The second review measured the cost of the parsing direction:
    94 of 152 truth-set keys could never be produced by `entity_mentions`.
    """
    found: set[str] = set()
    for name in names:
        if not name or len(name) < 2:
            continue
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text or "", re.I):
            found.add(name.lower())
    return found


class NameBuckets(NamedTuple):
    """Known-name mentions split by what the sentence DOES with the name.

    positive  — asserted as matching: recall credit, overclaim liability
    negated   — denied: no credit, no liability
    excluded  — named as a hit but filed irrelevant (rule 5 exclusion group):
                recall credit — the census reached the reader — no liability
    scope     — listed as a scanned input, no verdict: no credit, no liability
    """
    positive: set[str]
    negated: set[str]
    excluded: set[str]
    scope: set[str]


def asserted_names(text: str, names: Iterable[str]) -> NameBuckets:
    """Split known-name mentions by polarity and census framing.

    An overclaim is an entity asserted AS MATCHING — "ButterBeKind mentions
    synthetic biology". "ButterBeKind does not mention synthetic biology" names
    the same entity and is the honest answer, not an overclaim; counting it as
    one (as the first implementation did) penalizes exactly the disclosure the
    honesty contract demands. The same symmetry holds for census framing:
    prompt rule 5 orders the agent to name every hit and label the irrelevant
    ones — grading those labeled names as overclaims (as the pre-exclusion
    grader did on v2c012/v2c041) punishes compliance with rule 5 itself.

    Precedence across sentences: positive > excluded > negated > scope — a
    name asserted positively in ANY sentence counts as positive, so hedged or
    disclaimed repeats cannot hide a real claim.
    """
    positive: set[str] = set()
    negated: set[str] = set()
    excluded: set[str] = set()
    scope: set[str] = set()
    group: str | None = None
    for line in (text or "").splitlines():
        if not line.strip():
            continue  # a blank line between a framing sentence and its list
            # is normal markdown; only resumed prose ends the group
        is_item = bool(_LIST_ITEM_RE.match(line))
        if not is_item:
            group = None
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            hits = names_in_text(sentence, names)
            if not hits:
                continue
            bucket = _census_framing(sentence)
            if bucket is None and _NEGATION_RE.search(
                    _HEDGE_RE.sub(" ", sentence)):
                bucket = "negated"
            if bucket is None and is_item:
                bucket = group
            if bucket == "scope":
                scope |= hits
            elif bucket == "excluded":
                excluded |= hits
            elif bucket == "negated":
                negated |= hits
            else:
                positive |= hits
        if not is_item and line.rstrip().rstrip("*_ ").endswith(":"):
            group = _census_framing(line)
    excluded -= positive
    negated -= positive | excluded
    scope -= positive | excluded | negated
    return NameBuckets(positive, negated, excluded, scope)


# --- the §5.2 global checkers --------------------------------------------


def check_numeric_grounding(answer: str, evidence: Iterable[str]) -> Diagnostic:
    """Every numeral in the answer should appear in the cited evidence.

    Blind spots, stated rather than hidden: numbers the answer legitimately
    DERIVES (how many items it just listed, a span between two dates) are not in
    the evidence and will be reported here; numbers written as words in the
    source ("twelve") do not match their digit form. Both are exactly what the
    false-positive audit is for before this becomes a gate.
    """
    text = strip_citations(answer)
    in_answer = numerals(text)
    grounded = set()
    for chunk in evidence:
        grounded.update(numerals(chunk))
    ungrounded = [n for n in in_answer if n not in grounded]
    return Diagnostic(
        check="numeric_grounding",
        passed=not ungrounded,
        detail={"numerals": len(in_answer), "ungrounded": sorted(set(ungrounded)),
                "ungrounded_count": len(ungrounded)})


def check_entity_grounding(answer: str, evidence: Iterable[str],
                           question: str = "") -> Diagnostic:
    """Named entities in the answer should appear in the evidence or the
    question — the invented-company/person check."""
    haystack = " ".join([question or "", *evidence]).lower()
    mentions = entity_mentions(strip_citations(answer))
    ungrounded = sorted({m for m in mentions if m.lower() not in haystack})
    return Diagnostic(
        check="entity_grounding",
        passed=not ungrounded,
        detail={"mentions": len(mentions), "ungrounded": ungrounded,
                "ungrounded_count": len(ungrounded)})


# "I searched the 25 evaluations", "across all 25 startups", "of the 64 …"
_COVERAGE_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"\bI (?:searched|scanned|reviewed|checked|looked (?:at|through))\b",
        r"\b(?:searched|scanned|reviewed|covering|covered)\s+(?:the\s+)?\d+\b",
        r"\b(?:across|among|of|from|within)\s+(?:the\s+|all\s+)?\d+\s+\w+",
        r"\b\d+\s+(?:startups?|evaluations?|companies|documents?|profiles?|CVs?)\b",
        r"\bbased on (?:the )?(?:\d+|documents|evidence|sources) (?:I|that|which)\b",
        r"\b(?:top|first)\s+\d+\s+(?:results?|matches|hits)\b",
        r"\b(?:this|the (?:above|following)) (?:is )?not (?:an )?exhaustive\b",
        r"\bmay not be (?:complete|exhaustive)\b",
        r"\bonly (?:searched|covers?|includes?)\b",
    )
]


def check_coverage_statement(answer: str) -> Diagnostic:
    """Does the answer state the scope it actually searched?

    §5.2 is explicit that the judge does not decide this — it is pattern-checked,
    so `must_disclose_coverage` means the same thing on every run.
    """
    text = strip_citations(answer)
    hits = [p.pattern for p in _COVERAGE_PATTERNS if p.search(text)]
    return Diagnostic(check="coverage_statement", passed=bool(hits),
                      detail={"matched_patterns": len(hits)})


# claims that the answer covered everything. Legitimate when the route can
# actually deliver it (scan_text); the honesty contract is precisely that
# top-k retrieval may not say this.
_EXHAUSTIVE_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"\ball (?:of )?(?:the )?(?:startups?|evaluations?|companies|documents?)\b",
        r"\bevery (?:startup|evaluation|company|document)\b",
        r"\b(?:the )?(?:complete|full|exhaustive) list\b",
        r"\bthere are (?:no )?others?\b",
        r"\bthese are the only\b",
        r"\bno other (?:startups?|evaluations?|companies)\b",
    )
]


def check_no_exhaustive_claim(answer: str) -> Diagnostic:
    """Passes when the answer does NOT claim exhaustiveness.

    Blind spot: a correctly hedged sentence can still contain the words ("I
    cannot claim these are all the startups"). Diagnostics-first for that
    reason — the audit decides whether negation handling is needed before this
    can gate.
    """
    text = strip_citations(answer)
    hits = [p.pattern for p in _EXHAUSTIVE_PATTERNS if p.search(text)]
    return Diagnostic(check="no_exhaustive_claim", passed=not hits,
                      detail={"matched_patterns": hits})


# Canonical form for verbatim-quote matching (s5): typographic variants,
# markdown emphasis and inline links are FORMATTING, not different words. The
# d3afbc7 scripts run failed quotes that differed from their sources only by a
# non-breaking hyphen (U+2011), curly-vs-straight quote marks, added **bold**,
# or a dropped inline link — canonicalizing both sides keeps the detector
# honest; what still misses escalates to the quotes gate (adjudicate.py).
_QUOTE_CANON = str.maketrans({
    # ALL quote marks fold to one character: quoting a double-quoted source
    # inside a double-quoted span converts the inner marks to singles by
    # convention (v2s001t5's 'wellness companion'), and both sides get the
    # same folding so nothing is lost for matching
    "“": "'", "”": "'", "‘": "'", "’": "'", '"': "'",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    "\u00a0": " ",   # non-breaking space
})
# `([text](url))` is an inline citation parenthetical — dropped whole;
# a bare `[text](url)` keeps its text
_MD_LINK_RE = re.compile(r"\(\s*\[([^\]]*)\]\([^\s)]*\)\s*\)"
                         r"|\[([^\]]*)\]\([^\s)]*\)")


def _canon_quote(text: str) -> str:
    text = (text or "").translate(_QUOTE_CANON)
    text = _MD_LINK_RE.sub(
        lambda m: "" if m.group(1) is not None else (m.group(2) or ""), text)
    text = text.replace("**", "").replace("*", "").replace("`", "")
    text = re.sub(r"\s+([.,;:!?])", r"\1", " ".join(text.split()))
    return text.lower()


def check_quotes_verbatim(answer: str, evidence: Iterable[str],
                          require_quotes: bool = False) -> Diagnostic:
    """Quoted spans must appear verbatim in the evidence (bank §1.10).

    Canonicalized on both sides (`_canon_quote`, s5): whitespace, typographic
    hyphens/quote marks, markdown emphasis and inline links are formatting
    artifacts, not different quotes. Spans that still miss are recorded in
    `unfound` — original wording — for the quotes escalation gate.

    `require_quotes` is set when a case DECLARES `must_quote_verbatim`: the
    case asked for quotes, so an answer containing none has not met the
    obligation. Without it, "zero bad quotes" passed vacuously — the second
    review caught an answer with no excerpts and no citations passing evidence
    fidelity.
    """
    haystacks = [_canon_quote(chunk) for chunk in evidence]
    quotes = [_norm_ws(m.group(1)) for m in _QUOTE_RE.finditer(answer or "")]
    unfound = [q for q in quotes
               if not any(_canon_quote(q) in hay for hay in haystacks)]
    passed = not unfound and not (require_quotes and not quotes)
    return Diagnostic(check="quotes_verbatim", passed=passed,
                      detail={"quotes": len(quotes), "unfound": unfound,
                              "required": require_quotes})


def check_must_cite(citation_stats: dict[str, Any] | None) -> Diagnostic:
    """The answer must carry citations and every emitted one must resolve.

    Mechanical on purpose: the citation validator already computed
    emitted/resolved, and `must_cite` was previously "graded elsewhere" —
    which turned out to be nowhere. An answer with zero citations fails; an
    answer whose citations do not all resolve fails.
    """
    stats = citation_stats or {}
    emitted = stats.get("emitted") or 0
    resolved = stats.get("resolved") or 0
    return Diagnostic(check="must_cite",
                      passed=emitted > 0 and resolved == emitted,
                      detail={"emitted": emitted, "resolved": resolved})


def check_mentions_all(answer: str, terms: Iterable[str]) -> Diagnostic:
    text = (answer or "").lower()
    missing = [t for t in terms if t.lower() not in text]
    return Diagnostic(check="mentions_all", passed=not missing,
                      detail={"missing": missing})


def check_absent_strings(answer: str, terms: Iterable[str]) -> Diagnostic:
    """The injection canary's payload, and anything else the answer must never
    carry out or echo."""
    text = (answer or "").lower()
    present = [t for t in terms if t.lower() in text]
    return Diagnostic(check="absent_strings", passed=not present,
                      detail={"present": present})


# Detail keys whose VALUES are entity or phrase names rather than counts.
# Manifests are committed and must stay body-free (QA-LOOP §4.1) — the same
# reason truth sets are untracked. Full detail lives in the owner-only bundle.
NAMED_DETAIL = ("ungrounded", "overclaimed", "intruders", "present", "missing",
                "unfound", "matched_patterns", "adjudication_items")


def countable(diagnostic: Any) -> dict[str, Any]:
    """Manifest-safe projection of a diagnostic: counts, never names."""
    d = diagnostic.to_dict() if hasattr(diagnostic, "to_dict") else dict(diagnostic)
    detail = {k: v for k, v in (d.get("detail") or {}).items()
              if k not in NAMED_DETAIL}
    for key in NAMED_DETAIL:
        value = (d.get("detail") or {}).get(key)
        if isinstance(value, list):
            detail[f"{key}_count"] = len(value)
    return {**d, "detail": detail}


# --- graded-unit composition (shared by run_v2 and script_runner) ---------

# Which judge labels refute which declared judged dimension.
# `unverifiable_citation` counts against faithfulness: a claim that cannot be
# checked against its cited evidence is not a supported claim.
DIMENSION_FAIL_LABELS = {
    "faithfulness": {"synthesis_error", "unverifiable_citation"},
    "citation_coverage": {"citation_coverage_error"},
}


def unit_verdicts(assertions: list, judged_dims: Iterable[str],
                  behavior_obligations: Iterable[str],
                  verdict: dict[str, Any] | None,
                  behavior_verdicts: list[dict[str, Any]]
                  ) -> dict[str, bool | None]:
    """Every unit a grade block declares, each True / False / None-ungraded.

    Deterministic assertions carry their own verdicts; a declared judged
    dimension is refuted by its judge labels (None when the judge did not
    run); a behavior obligation is graded by the targeted behavior judge
    (None when it did not run). Nothing declared is ever silently omitted —
    that is the vacuous-pass bug the second review caught.
    """
    units: dict[str, bool | None] = {a.check: a.passed for a in assertions}
    labels = set((verdict or {}).get("labels") or [])
    for dim in judged_dims:
        # escalation gate (s3): a formula-flagged failure the judge
        # adjudicated gates on the adjudicated verdict; the formula labels
        # stay recorded as telemetry (experiments/adjudicate.py)
        adj = ((verdict or {}).get("adjudications") or {}).get(dim)
        if adj is not None and adj.get("passed") is not None:
            units[f"judged:{dim}"] = adj["passed"]
        else:
            units[f"judged:{dim}"] = (not (labels & DIMENSION_FAIL_LABELS[dim])
                                      if verdict else None)
    met = {v["obligation"]: v["met"] for v in behavior_verdicts}
    for obligation in behavior_obligations:
        units[f"behavior:{obligation}"] = met.get(obligation)
    return units


def compose_pass(units: dict[str, bool | None]) -> bool | None:
    """One verdict from a set of graded units. A definite failure is
    decisive; otherwise any ungraded unit makes the whole verdict None —
    reported by the comparator, never guessed at."""
    if any(v is False for v in units.values()):
        return False
    if any(v is None for v in units.values()):
        return None
    return True


# --- dispatch -------------------------------------------------------------

# Which mechanical check answers which case-level assertion. `truth_set` is
# not here: it is graded against the computed oracle by the runner (build
# step 4).
CHECK_FOR_ASSERTION = {
    "must_cite": "must_cite",
    "must_disclose_coverage": "coverage_statement",
    "must_not_claim_exhaustive": "no_exhaustive_claim",
    "must_quote_verbatim": "quotes_verbatim",
    "must_mention_all": "mentions_all",
    "must_not_mention": "absent_strings",
}


def run_global_checkers(answer: str, *, evidence: Iterable[str],
                        question: str = "") -> list[Diagnostic]:
    """The three §5.2 checkers that run on every answer, regardless of case."""
    evidence = list(evidence)
    return [check_numeric_grounding(answer, evidence),
            check_entity_grounding(answer, evidence, question),
            check_coverage_statement(answer)]


def run_case_checks(answer: str, *, evidence: Iterable[str],
                    deterministic: dict[str, Any],
                    citation_stats: dict[str, Any] | None = None
                    ) -> list[Diagnostic]:
    """The mechanical assertions a specific case declares.

    An assertion with no implementation is an error, not a skip: silently
    passing an unimplemented check is how a suite reports green while grading
    nothing (the failure mode experiments/cases.py exists to prevent at compile
    time — this is the run-time half). `must_cite` used to be one of those
    skips — "graded by the existing citation validator", which nothing ever
    folded into the verdict — so a caller that declares it must now supply
    `citation_stats` or get a loud error.
    """
    evidence = list(evidence)
    out: list[Diagnostic] = []
    for assertion, value in sorted(deterministic.items()):
        if assertion == "truth_set":
            continue                    # graded against the computed oracle
        if assertion == "must_cite" and value:
            if citation_stats is None:
                raise KeyError("must_cite is declared but no citation_stats "
                               "were supplied — the check would grade nothing")
            out.append(check_must_cite(citation_stats))
        elif assertion == "must_disclose_coverage" and value:
            out.append(check_coverage_statement(answer))
        elif assertion == "must_not_claim_exhaustive" and value:
            out.append(check_no_exhaustive_claim(answer))
        elif assertion == "must_quote_verbatim" and value:
            # declared means demanded: an answer with no quotes at all has not
            # met the obligation (vacuous pass caught by the second review)
            out.append(check_quotes_verbatim(answer, evidence,
                                             require_quotes=True))
        elif assertion == "must_mention_all" and value:
            out.append(check_mentions_all(answer, value))
        elif assertion == "must_not_mention" and value:
            out.append(check_absent_strings(answer, value))
        elif value:
            raise KeyError(f"no checker implements assertion {assertion!r} — "
                           "add one or remove the assertion; a check that "
                           "silently does nothing is worse than no check")
    return out
