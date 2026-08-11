"""Headless-Claude judge backend (Track H) — whole-answer judging, full evidence.

Why this exists: the 2026-08-04 false-positive audit
(`.qa-artifacts/reports/2026-08-04_judge_audit.md`, David-approved follow-up)
found ~92% of the OpenAI judge's faithfulness flags spurious, and the causes
were structural, not model quality: `evidence_texts()` stripped document
titles (the only place company names and report types live), `judge_one()`
showed each claim only its own citations (making every comparative claim
unjudgeable by construction), and the two-step inventory→entailment pipeline
mutated claims before grading them. This backend judges the way the auditors
audited: ONE call per answer that sees the question, the whole answer, and the
FULL evidence set with titles — then Python computes counts/scores/labels
mechanically so the label semantics stay identical to the OpenAI path.

Billing boundary (CC-AGENTS-DESIGN, PLAN Track H): headless `claude -p` runs
on the David-seat Max subscription — **dev/QA only, never a production path**.
Production judging must use an API-billed judge (`ADVISOR_JUDGE_BACKEND=openai`
or an Anthropic API key path that does not exist yet). The cost ledger records
the API-equivalent value of each call (see cost.py PRICING note), so "what
would this cost for real" stays measured, never silently $0.

Model default is `opus` (Opus 5 — David's explicit pick, 2026-08-04: the
judge should match the auditors that exposed the old judge, not undercut
them). Caveat that travels with the choice: judge volume shares David's
interactive seat quota, and a judged core run is ~45 evidence-heavy calls
(≈$7–10 API-equivalent). `ADVISOR_JUDGE_CC_MODEL=sonnet` steps down when the
seat is under pressure — an unknown resolved model raises in cost.py rather
than logging $0.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from x1_advisor.cost import Tracker, Usage

CC_MODEL = os.environ.get("ADVISOR_JUDGE_CC_MODEL", "opus")
CC_TIMEOUT = int(os.environ.get("ADVISOR_JUDGE_CC_TIMEOUT", "300"))
# same opt-in evidence cap as the OpenAI path: unset/0 = UNLIMITED
from x1_advisor.agent.judge import EVIDENCE_CHARS  # noqa: E402  (single source)

_SETTINGS = Path(__file__).resolve().parents[2] / "qa" / "cc-judge-settings.json"


class CCClaim(BaseModel):
    text: str
    is_factual: bool
    citation_numbers: list[int]
    verdict: str | None = None       # supported | partial | unsupported | None
    reason: str | None = None


class CCInventory(BaseModel):
    claims: list[CCClaim]


# The grading rules below are the durable semantics the 2026-08-04 audit
# established (each rule traces to a named false-positive class in the audit
# report). They are product semantics — what "faithful to evidence" means —
# not tuning against any particular test case.
CC_JUDGE_PROMPT = """\
You are grading a research agent's ANSWER for evidence faithfulness. The
EVIDENCE section lists every item the agent was shown, each as
[refN] TITLE (locator) followed by its text. Citations in the answer look
like [ref2] or [ref1, ref4].

Step 1 — inventory. List every distinct assertion in the ANSWER, in order.
For each: `text` (quoted or closely paraphrased — preserve the assertion's own
qualifiers, hedges and scope EXACTLY; never add scope it does not assert; a
distributive list "A does X, B does Y" stays two assertions, never a
cross-product), `is_factual`, and `citation_numbers` (the integer parts of the
refs attached to it; empty list if none).

`is_factual` is false for: hedges, questions, offers to help, restatements of
the user's question, connective prose — AND statements about the agent's own
research process (what it searched, what its searches did or did not return,
exclusions it made, completeness caveats like "this may not be exhaustive").
Process disclosures are not checkable world-facts and never require citations.

Step 2 — verdict, ONLY for assertions with is_factual true AND at least one
citation. For those, set `verdict` and a one-sentence `reason` naming the
element that decided it. Otherwise leave verdict and reason null.

- supported: every factual element follows from the evidence. Faithful
  paraphrase counts ("helpful" supports "supportive"). Titles and locators ARE
  evidence: a document titled "X — CV" establishes whose CV it is; a section
  titled "Y — Technology & IP (evaluation section)" establishes the company
  and document type. A hedged claim ("could", "potential", "appears") is
  supported when the evidence supports the possibility. A claim that evidence
  is ABSENT ("the documents do not name…", "no public data found") is
  supported when the cited sources state that absence or simply exhibit it.
  A comparative or set-level claim ("the clearest match", "the only one
  that…", "broadly consistent") is judged against the WHOLE evidence set
  shown, not just the cited items.
- partial: part of the claim is genuinely supported and another factual
  element is genuinely absent or weaker — not merely re-worded.
- unsupported: the evidence contradicts the claim or does not establish it.
  Numbers, dates and quantities must match: "68" is not support for "78".

Judge only against the evidence given; do not use outside knowledge to decide
whether a world-claim is true.

Do not use any tools. Respond with ONLY a JSON object as your message text —
no prose, no code fences:
{"claims": [{"text": "...", "is_factual": true, "citation_numbers": [1, 3],
             "verdict": "supported", "reason": "..."}, ...]}

QUESTION:
{question}

ANSWER:
---
{answer}
---

EVIDENCE (everything the agent was shown):
{evidence}
"""

# entailment-only variant for calibration items, which are stored (claim,
# evidence-text) pairs with no titles or full-set context — the tempered
# rules still apply, the informational fixes cannot (nothing to add).
CC_ENTAIL_PROMPT = """\
Decide whether the SOURCE text supports the CLAIM.

- supported: every factual element of the claim follows from the source.
  Faithful paraphrase counts. A hedged claim ("could", "potential") is
  supported when the source supports the possibility. A claim that evidence is
  absent is supported when the source states or exhibits that absence.
- partial: part is genuinely supported and another factual element is
  genuinely absent or weaker — not merely re-worded.
- unsupported: the source contradicts the claim or does not establish it.
  Numbers, dates and quantities must match: "68" is not support for "78".

Judge only against the source given; no outside knowledge.

Output ONLY a JSON object, no prose, no code fences:
{"verdict": "supported", "reason": "one sentence naming the deciding element"}

CLAIM:
{claim}

SOURCE:
---
{source}
"""


def _claude_env() -> dict[str, str]:
    """Minimal env, cron-safe: ~/.claude holds an EXPIRED OAuth session on
    this machine, so CLAUDE_CONFIG_DIR must always be set explicitly."""
    return {
        "HOME": os.environ.get("HOME", str(Path.home())),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "TERM": "dumb",
        "CLAUDE_CONFIG_DIR": os.environ.get(
            "CLAUDE_CONFIG_DIR", str(Path.home() / ".claude-max")),
    }


# Global bound on concurrent headless judge processes (2026-08-07, the
# parallel harness): case workers × pooled gate samples would otherwise
# multiply into dozens of simultaneous CLI subprocesses on the shared seat.
# One semaphore here bounds the whole process regardless of who is calling.
_JUDGE_GATE = threading.Semaphore(
    int(os.environ.get("ADVISOR_JUDGE_CONCURRENCY", "6")))


class JudgeTransportDown(RuntimeError):
    """Systemic judge-transport failure: too many dead `claude -p` calls.

    Raised by the tripwire below, on purpose, to kill the run LOUDLY — a run
    whose judge is down must not complete as a plausible-looking formula-only
    run (David, 2026-08-11: isolated flakes are contained, outages crash)."""


# Transport containment (2026-08-11). An isolated dead subprocess — timeout,
# nonzero exit, garbage stdout — must not kill a 15-minute graded run: the
# call is treated as an unusable judge sample (returns None) and the caller's
# fail-safe keeps the FORMULA verdict standing, which can only ever keep a
# failure, never absolve one. Every containment is counted and printed; the
# tripwire turns systemic failure into a loud crash. A missing CLI binary is
# NOT contained — that is a configuration error and fails fast.
_TRANSPORT_LOCK = threading.Lock()
_TRANSPORT_STATS = {"calls": 0, "failures": 0}
# tripwire: crash once failures >= MAX_FAILURES AND failures/calls > MAX_RATE
CC_MAX_TRANSPORT_FAILURES = int(
    os.environ.get("ADVISOR_JUDGE_MAX_TRANSPORT_FAILURES", "3"))
CC_MAX_TRANSPORT_RATE = float(
    os.environ.get("ADVISOR_JUDGE_MAX_TRANSPORT_RATE", "0.10"))


def transport_stats() -> dict[str, int]:
    """Snapshot of this process's headless-call transport counters — recorded
    on run summary rows so a contained flake is visible, never silent."""
    with _TRANSPORT_LOCK:
        return dict(_TRANSPORT_STATS)


def _transport_failure(stage: str, detail: str) -> None:
    """Count one dead transport call; crash the run if it looks systemic."""
    with _TRANSPORT_LOCK:
        _TRANSPORT_STATS["failures"] += 1
        calls = _TRANSPORT_STATS["calls"]
        failures = _TRANSPORT_STATS["failures"]
    print(f"[judge-cc] transport failure {failures}/{calls} at {stage}: "
          f"{detail} — sample discarded, formula verdict stands",
          file=sys.stderr)
    if (failures >= CC_MAX_TRANSPORT_FAILURES
            and failures / calls > CC_MAX_TRANSPORT_RATE):
        raise JudgeTransportDown(
            f"{failures} of {calls} headless judge calls failed at transport "
            f"(tripwire: >{CC_MAX_TRANSPORT_RATE:.0%} after "
            f"{CC_MAX_TRANSPORT_FAILURES}) — the judge is down, refusing to "
            f"finish a silently formula-only run. Last failure at {stage}: "
            f"{detail}")
    return None


def _run_claude(prompt: str, *, tracker: Tracker | None,
                stage: str) -> dict[str, Any] | None:
    """One headless call → parsed CLI JSON result, cost logged per model.

    Returns None when THIS call's transport died (contained + counted above);
    raises JudgeTransportDown when the process-wide failure rate says the
    judge is down.

    Runs from an empty temp cwd OUTSIDE the repo so the judge never inherits
    project CLAUDE.md/settings context — the judge grades text, it is not a
    project collaborator. Tool use is denied via qa/cc-judge-settings.json and
    --max-turns 1 (belt and braces: a judge that can read files could grade
    against evidence the agent was never shown).
    """
    binary = shutil.which("claude", path=_claude_env()["PATH"])
    if not binary:
        raise RuntimeError("claude CLI not on PATH — the cc judge backend "
                           "needs it (or set ADVISOR_JUDGE_BACKEND=openai)")
    with _TRANSPORT_LOCK:
        _TRANSPORT_STATS["calls"] += 1
    cwd = tempfile.mkdtemp(prefix="cc-judge-")
    try:
        # --max-turns 3, not 1: if the model reaches for a (denied) tool
        # anyway, one turn is burned on the denial and it needs another to
        # produce the JSON. With 1 the CLI exits is_error on the first stray
        # tool call — which killed a whole 47-bundle rejudge on 2026-08-04.
        with _JUDGE_GATE:
            proc = subprocess.run(
                [binary, "-p", "--model", CC_MODEL, "--output-format", "json",
                 "--max-turns", "3", "--settings", str(_SETTINGS)],
                input=prompt, capture_output=True, text=True,
                timeout=CC_TIMEOUT, cwd=cwd, env=_claude_env(),
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return _transport_failure(stage, f"{exc.__class__.__name__}: "
                                         f"{str(exc)[:200]}")
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    if proc.returncode != 0:
        return _transport_failure(
            stage, f"claude -p exit {proc.returncode}: "
                   f"{(proc.stderr or proc.stdout)[:400]}")
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        return _transport_failure(
            stage, f"CLI stdout not JSON: {proc.stdout[:200]!r}")
    if tracker:
        for model_id, mu in (out.get("modelUsage") or {}).items():
            tracker.log(provider="anthropic",
                        model=mu.get("canonicalModel", model_id), stage=stage,
                        usage=Usage(
                            input_tokens=int(mu.get("inputTokens", 0)),
                            output_tokens=int(mu.get("outputTokens", 0)),
                            cache_read_tokens=int(mu.get("cacheReadInputTokens", 0)),
                            cache_write_tokens=int(mu.get("cacheCreationInputTokens", 0)),
                        ))
    return out


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def _parse_json_result(text: str) -> dict:
    return json.loads(_FENCE_RE.sub("", (text or "").strip()))


def _judged_model(cli_out: dict[str, Any] | None) -> str:
    """`cc:<canonical model>` — lands in the manifest projection, which is what
    makes the scoring contract sever between cc-judged and openai-judged runs."""
    for model_id, mu in (cli_out or {}).get("modelUsage", {}).items():
        canonical = mu.get("canonicalModel", model_id)
        if "haiku" not in canonical:      # CLI-internal glue model
            return f"cc:{canonical}"
    return f"cc:{CC_MODEL}"


def _evidence_block(bundle: dict) -> tuple[str, dict[int, dict[str, Any]]]:
    """Render EVERY evidence item the agent saw — titles included — and the
    citation-number → evidence map for locator/unverifiable bookkeeping."""
    by_ref = {e["ref"]: e for e in bundle.get("evidence") or []}
    num_of_ref: dict[str, int] = {}
    sources: dict[int, dict[str, Any]] = {}
    for c in bundle.get("validation", {}).get("citations", []):
        ev = by_ref.get(c["ref"])
        if ev is None:
            continue
        num_of_ref[c["ref"]] = c["n"]
        locator = (f"doc {ev.get('document_id')} block {ev.get('block_index')}"
                   if ev.get("kind") == "chunk"
                   else ev.get("url") or ev.get("query_name") or "")
        sources[c["n"]] = {"kind": c.get("type"), "title": ev.get("title") or "",
                           "text": ev.get("snapshot") or "", "locator": locator}
    parts = []
    for ref, ev in by_ref.items():
        n = num_of_ref.get(ref)
        label = f"[ref{n}]" if n is not None else f"[{ref} — shown, uncited]"
        title = ev.get("title") or "(untitled)"
        locator = (f"doc {ev.get('document_id')} block {ev.get('block_index')}"
                   if ev.get("kind") == "chunk"
                   else ev.get("url") or ev.get("query_name") or "")
        text = (ev.get("snapshot") or "")[:EVIDENCE_CHARS]
        parts.append(f"{label} {title} ({locator})\n{text}")
    return "\n\n".join(parts), sources


def judge_bundle_cc(conn, bundle: dict, *, tracker: Tracker | None = None,
                    calibration: dict | None = None,
                    _transport: Callable[..., dict | None] | None = None,
                    ) -> dict[str, Any] | None:
    """Same contract as `judge.judge_bundle`; returns None if the judge call
    itself is unusable after a retry (caller records the case as UNGRADED —
    never a fabricated verdict).

    `_transport` injects a fake CLI runner in tests.
    """
    from x1_advisor.agent.judge import SCHEMA_VERSION, evidence_provenance

    run = _transport or _run_claude
    answer = bundle.get("validation", {}).get("answer", "")
    question = ((bundle.get("request") or {}).get("question")
                or (bundle.get("request") or {}).get("prompt") or "")
    evidence, sources = _evidence_block(bundle)
    prompt = (CC_JUDGE_PROMPT
              .replace("{question}", question)
              .replace("{answer}", answer)
              .replace("{evidence}", evidence))

    inventory, cli_out = None, None
    for attempt in range(2):
        cli_out = run(prompt, tracker=tracker, stage="judge.cc")
        try:
            candidate = CCInventory.model_validate(
                _parse_json_result((cli_out or {}).get("result", "")))
        except (ValueError, ValidationError) as exc:
            if attempt == 1:
                return None          # ungraded, loudly — never guessed
            prompt += ("\n\nYour previous output failed to parse "
                       f"({exc.__class__.__name__}). Output ONLY the JSON object.")
            continue
        # A cited factual claim with LIVE evidence and no verdict is an
        # incomplete judgment, not an unverifiable claim (2934f7a run,
        # v2c028: coercing these to 'unverifiable' label-failed a case whose
        # every judged claim was supported). Retry once naming the gaps;
        # still incomplete → the case is UNGRADED. Claims whose citations
        # all point at dead refs are excluded here — the judge was shown no
        # evidence for them, so a null verdict is the honest output and the
        # dead-citation branch below handles them.
        unjudged = [c.text for c in candidate.claims
                    if c.is_factual and c.citation_numbers and not c.verdict
                    and any(sources.get(n, {}).get("text")
                            for n in c.citation_numbers)]
        if not unjudged:
            inventory = candidate
            break
        if attempt == 1:
            return None              # incomplete twice — ungraded, never coerced
        listed = "; ".join(f'"{t[:80]}"' for t in unjudged[:10])
        prompt += ("\n\nYour output left these cited factual assertions "
                   f"without a verdict: {listed}. Re-output the COMPLETE JSON "
                   "object with a verdict (supported | partial | unsupported) "
                   "for every factual assertion that has at least one "
                   "citation shown in the evidence.")
    if inventory is None:
        return None

    claims = inventory.claims
    factual = [c for c in claims if c.is_factual]
    cited = [c for c in factual if c.citation_numbers]
    uncited = [c for c in factual if not c.citation_numbers]

    verdicts = []
    for c in cited:
        missing = [n for n in c.citation_numbers if not sources.get(n, {}).get("text")]
        if len(missing) == len(c.citation_numbers):
            verdicts.append({"claim": c.text, "citations": c.citation_numbers,
                             "verdict": "unverifiable",
                             "reason": f"no evidence text for {missing}",
                             "locators": []})
            continue
        # by construction non-null here: live-cited claims without a verdict
        # were retried above, and twice-incomplete judgments returned None
        verdicts.append({"claim": c.text, "citations": c.citation_numbers,
                         "verdict": c.verdict,
                         "reason": c.reason or "",
                         "locators": [sources[n]["locator"]
                                      for n in c.citation_numbers
                                      if sources.get(n)]})

    counts = {v: sum(1 for x in verdicts if x["verdict"] == v)
              for v in ("supported", "partial", "unsupported", "unverifiable")}
    judged = counts["supported"] + counts["partial"] + counts["unsupported"]
    return {
        "schema_version": SCHEMA_VERSION,
        "judge_model": _judged_model(cli_out),
        "judge_backend": "cc",
        "calibration": calibration or {"state": "uncalibrated"},
        "evidence_provenance": evidence_provenance(bundle),
        "claims": {"total": len(claims), "factual": len(factual),
                   "cited": len(cited), "uncited": len(uncited)},
        "verdicts": verdicts,
        "uncited_claims": [c.text for c in uncited],
        "counts": counts,
        "scores": {
            "faithfulness": (counts["supported"] / judged) if judged else None,
            "citation_coverage": (len(cited) / len(factual)) if factual else None,
        },
        "labels": sorted(
            ({"synthesis_error"} if counts["unsupported"] or counts["partial"] else set())
            | ({"citation_coverage_error"} if uncited else set())
            | ({"unverifiable_citation"} if counts["unverifiable"] else set())),
    }


def entail_cc(claim: str, source: str, *, tracker: Tracker | None = None,
              _transport: Callable[..., dict | None] | None = None,
              ) -> tuple[str, str]:
    """Single (claim, source) entailment — the calibration replay path."""
    run = _transport or _run_claude
    prompt = (CC_ENTAIL_PROMPT.replace("{claim}", claim)
              .replace("{source}", source))
    for attempt in range(2):
        out = run(prompt, tracker=tracker, stage="judge.cc.calibrate")
        try:
            parsed = _parse_json_result((out or {}).get("result", ""))
            verdict = parsed.get("verdict")
            if verdict in ("supported", "partial", "unsupported"):
                return verdict, parsed.get("reason", "")
        except ValueError:
            pass
        prompt += "\n\nYour previous output failed to parse. Output ONLY the JSON object."
    return "unverifiable", "judge returned nothing parseable"
