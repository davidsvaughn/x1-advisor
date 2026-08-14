"""Eval-bundle parsing → SourceDoc records.

FOUR original bundle generations exist in prod (verified live 2026-07-08 by
sampling gs://x1-app-www-prod/reports/ — all must parse; the agent ultimately runs
against prod):

  gen-0b (oldest): ``company_name``/``score``/``sections``/``summary_interaction``;
    sections carry {score, question, justification, summary, interactions}; no
    premium/basic reports, no deck/website.
  gen-0a: ``premium_report``/``basic_report``/``score`` + FLAT section keys —
    ``section_{key}_findings`` (str) and ``section_{key}_summ_score``
    ({analysis, score}); ``summary_interaction`` duplicates basic_report. Section
    key ``market_condition`` (singular) appears here.
  gen-1 (no schema_version): top-level ``premium_report``/``basic_report``/``score``/
    ``sections`` (each section: analysis, research_findings, score{value,...});
    deck text at ``inputs.pitch_deck.extracted_text``; no website content.
  gen-2 (schema_version=2): same top-level app-facing keys PLUS ``outputs.*``
    (premium_markdown/basic_markdown/section_results) and
    ``outputs.company_data.{pitchDeckContent, websiteContent}``. A test-only
    variant ("gen2-outputs") carries only the ``outputs.*`` half.

An EXPERIMENTAL shape exists on test only (top-level entityType/entityId/report —
75/79 test blobs as of 2026-07-08). It is deliberately NOT parsed: `parse_bundle`
raises `UnsupportedBundleShape` so callers can count/skip it loudly. Build against
prod's contract, not the experiment.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

SECTION_LABELS = {  # ReportChatService.php:211-219 — stable product vocabulary
    "problem": "Problem Definition",
    "founder": "Founder Background",
    "team": "Team Expertise",
    "technology": "Technology & IP",
    "market": "Market Opportunity",
    "market_conditions": "Market Conditions",
    "traction": "Traction & Growth",
}


class UnsupportedBundleShape(ValueError):
    """Bundle does not match either original generation."""


@dataclass
class SourceDoc:
    """One advisor.documents row-to-be, parsed from a bundle."""

    source_type: str          # 'eval_premium'|'eval_basic'|'eval_section'|'deck_extract'|'website'
    title: str
    markdown: str
    visibility: str           # 'private'|'x1'|'public'
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.markdown.encode()).hexdigest()


def _first(*vals: Any) -> Any:
    for v in vals:
        if v:
            return v
    return None


_GEN0A_FINDINGS = re.compile(r"^section_(.+)_findings$")

_KEY_ALIASES = {"market_condition": "market_conditions"}  # gen-0a singular


def detect_generation(bundle: dict) -> str:
    if "premium_report" in bundle and "sections" in bundle:
        return "gen2" if bundle.get("schema_version") else "gen1"
    if "premium_report" in bundle and any(_GEN0A_FINDINGS.match(k) for k in bundle):
        return "gen0a"
    if "summary_interaction" in bundle and isinstance(bundle.get("sections"), dict):
        return "gen0b"
    outputs = bundle.get("outputs") or {}
    if isinstance(outputs, dict) and (
        "premium_markdown" in outputs or "section_results" in outputs
    ):
        return "gen2-outputs"  # outputs.* only, no top-level app keys (seen on test)
    raise UnsupportedBundleShape(
        f"not an original-shape bundle (top-level keys: {sorted(bundle)[:8]})"
    )


def _sections_dict(bundle: dict, gen: str) -> dict[str, dict]:
    """Normalize every generation's sections into {key: {analysis,
    research_findings, score}} with gen-0 aliases folded in."""
    if gen == "gen0a":
        out: dict[str, dict] = {}
        for k, v in bundle.items():
            m = _GEN0A_FINDINGS.match(k)
            if not m:
                continue
            key = _KEY_ALIASES.get(m.group(1), m.group(1))
            summ = bundle.get(f"section_{m.group(1)}_summ_score") or {}
            out[key] = {
                "analysis": summ.get("analysis"),
                "research_findings": v,
                "score": summ.get("score"),
            }
        return out
    if gen == "gen0b":
        out = {}
        for k, sec in (bundle.get("sections") or {}).items():
            if not isinstance(sec, dict):
                continue
            key = _KEY_ALIASES.get(k, k)
            analysis_parts = [p for p in (sec.get("summary"), sec.get("justification")) if p]
            out[key] = {
                "analysis": "\n\n".join(analysis_parts),
                "research_findings": None,   # interactions are raw Q&A loops; not evidence-grade
                "score": sec.get("score"),
            }
        return out
    sections = bundle.get("sections")
    if isinstance(sections, dict) and sections:
        return sections
    out = {}
    for sr in (bundle.get("outputs") or {}).get("section_results") or []:
        if isinstance(sr, dict) and sr.get("sectionName"):
            out[sr["sectionName"]] = {
                "analysis": sr.get("analysis"),
                "research_findings": sr.get("rawFindings"),
                "score": sr.get("score"),
            }
    return out


def _company_row(bundle: dict) -> dict:
    """gen-1 keeps the startup row at inputs.company.startup_companies_row."""
    company = (bundle.get("inputs") or {}).get("company") or {}
    row = company.get("startup_companies_row")
    return row if isinstance(row, dict) else {}


def startup_company_id(bundle: dict) -> int | None:
    meta = bundle.get("metadata") or {}
    v = _first(
        meta.get("startup_company_id"),
        ((bundle.get("inputs") or {}).get("startup_company") or {}).get("id"),
        _company_row(bundle).get("id"),
    )
    return int(v) if v is not None else None


def company_name(bundle: dict) -> str | None:
    outputs = bundle.get("outputs") or {}
    return _first(
        bundle.get("company_name"),  # gen-0b
        (outputs.get("company_data") or {}).get("name"),
        ((bundle.get("inputs") or {}).get("startup_company") or {}).get("name"),
        _company_row(bundle).get("name"),
        ((bundle.get("inputs") or {}).get("company") or {}).get("name"),
    )


def overall_score(bundle: dict) -> float | None:
    v = _first(bundle.get("score"), (bundle.get("outputs") or {}).get("weighted_score"))
    return float(v) if v is not None else None


def _section_score(section: dict) -> float | None:
    s = section.get("score")
    if isinstance(s, dict):
        s = s.get("value")
    return float(s) if s is not None else None


def parse_bundle(bundle: dict, source_ref: str,
                 fallback_company_name: str | None = None) -> list[SourceDoc]:
    """Original-shape bundle → SourceDocs. Raises UnsupportedBundleShape otherwise.

    Visibility policy (PLAN §0.2 recommended treatments, pending David's final
    confirm — all revisable via re-stamp, not re-extraction):
      - premium full text: 'x1' + premium_gated metadata (purchase check at query time)
      - basic report + platform-authored section findings: 'x1' (open to signed-in users)
      - deck extract: 'private' unless the source deck's document row says otherwise
        (max-restrictive inheritance; resolver in backfill upgrades it when known)
      - website content: 'public' (it was public on the web)
    """
    gen = detect_generation(bundle)
    outputs = bundle.get("outputs") or {}
    inputs = bundle.get("inputs") or {}
    name = company_name(bundle) or fallback_company_name or "Unknown company"
    score = overall_score(bundle)
    base_meta: dict[str, Any] = {
        "bundle_generation": gen,
        "company_name": name,
        "eval_overall_score": score,
    }
    docs: list[SourceDoc] = []

    premium = _first(bundle.get("premium_report"), outputs.get("premium_markdown"))
    if premium:
        docs.append(SourceDoc(
            "eval_premium", f"{name} — Premium Investability Report", premium, "x1",
            {**base_meta, "premium_gated": True},
        ))

    basic = _first(
        bundle.get("basic_report"),
        outputs.get("basic_markdown"),
        bundle.get("summary_interaction") if gen == "gen0b" else None,
    )
    if basic:
        docs.append(SourceDoc(
            "eval_basic", f"{name} — Basic Evaluation Report", basic, "x1", dict(base_meta),
        ))

    for key, sec in _sections_dict(bundle, gen).items():
        if not isinstance(sec, dict):
            continue
        label = SECTION_LABELS.get(key, key.replace("_", " ").title())
        # 2026-08-14 (David): analysis and research findings are DIFFERENT
        # kinds of text — analysis is the evaluation's editorial conclusion
        # (verbatim inside the premium report); research_findings is the raw
        # pre-editorial research log (search queries, sourced facts, gaps —
        # NOT in the premium report, and 2-6x larger). Fused they blur
        # provenance (raw research cited as "(evaluation section)") and
        # pollute assertion censuses with query strings, so each becomes its
        # own document with its own source_type.
        if sec.get("analysis"):
            docs.append(SourceDoc(
                "eval_section", f"{name} — {label} (evaluation section)",
                str(sec["analysis"]), "x1",
                {**base_meta, "section_key": key,
                 "section_score": _section_score(sec)},
            ))
        if sec.get("research_findings"):
            docs.append(SourceDoc(
                "eval_research", f"{name} — {label} (research findings)",
                str(sec["research_findings"]), "x1",
                {**base_meta, "section_key": key},
            ))

    deck = _first(
        (inputs.get("pitch_deck") or {}).get("extracted_text"),          # gen-1
        (outputs.get("company_data") or {}).get("pitchDeckContent"),     # gen-2
    )
    if deck:
        deck_src = inputs.get("pitch_deck") or {}
        docs.append(SourceDoc(
            "deck_extract", f"{name} — Pitch Deck (extracted)", deck, "private",
            {**base_meta,
             "deck_document_id": deck_src.get("id"),
             "deck_gcs_path": _first(deck_src.get("source_gcs_path"),
                                     deck_src.get("file_path"),
                                     inputs.get("pitch_deck_gcs_path"))},
        ))

    website = (outputs.get("company_data") or {}).get("websiteContent")
    if website:
        docs.append(SourceDoc(
            "website", f"{name} — Website content", website, "public",
            {**base_meta,
             "website_url": _first((outputs.get("company_data") or {}).get("websiteUrl"),
                                   inputs.get("company_url"))},
        ))

    for d in docs:
        d.metadata["source_ref"] = source_ref
    return docs
