"""Agent tools (PLAN Phase 4) — compact-output by construction.

CONTEXT DISCIPLINE (§9; David's explicit priority 2026-07-08): every tool result is
replayed into the model on EVERY subsequent step of the turn, so fat results balloon
input tokens quadratically across a loop. Contract here:
  - search_corpus returns refs + SNIPPETS (flagged `_truncated`), never full blocks;
  - get_source is the deliberate escalation path for one block's full text (bounded);
  - web_research returns distilled findings (bounded, flagged) + citation refs;
  - structured_query returns at most queries.MAX_ROWS rows;
  - items are never silently dropped — caps are visible in the tool contract and
    truncation is always flagged.
"""

from __future__ import annotations

import json
import os
from typing import Any

from haystack.tools import Tool

from x1_advisor.agent.evidence import EvidenceRegistry
from x1_advisor.agent.queries import catalog, run_query
from x1_advisor.cost import Tracker, Usage, estimate
from x1_advisor.retrieval import retrieve

SNIPPET_CHARS = 600
SOURCE_CHARS = 6000
WEB_FINDINGS_CHARS = 1600
SEARCH_K = 8
WEB_MODEL = "gpt-5.1"


def _clip(text: str, limit: int) -> tuple[str, bool]:
    text = text.strip()
    return (text[:limit].rstrip() + " …", True) if len(text) > limit else (text, False)


def build_tools(conn, *, acl: Any, registry: EvidenceRegistry,
                tracker: Tracker) -> list[Tool]:
    _ENTITY_TYPES = {"startup_company", "investor", "cv", "investment_company",
                     "investment_fund", "organization"}
    _ENTITY_ALIASES = {"startup": "startup_company", "company": "startup_company",
                       "person": "cv", "fund": "investment_fund", "org": "organization"}

    def search_corpus(query: str, filters: dict | None = None) -> str:
        if filters and "entity_type" in filters:
            et = _ENTITY_ALIASES.get(str(filters["entity_type"]), filters["entity_type"])
            if et not in _ENTITY_TYPES:
                return json.dumps({"error": f"invalid entity_type {filters['entity_type']!r};"
                                            f" valid: {sorted(_ENTITY_TYPES)}"})
            filters = {**filters, "entity_type": et}
        hits = retrieve(conn, query, acl=acl, filters=filters, k=SEARCH_K,
                        tracker=tracker)
        # gated-vs-absent: on an empty result for a NON-admin, check (count/class
        # only — no titles, no content) whether access-restricted material exists,
        # so the agent can say "restricted" instead of the misleading "not found"
        gated_note = None
        if not hits and acl != "admin":
            open_hits = [
                h for h in retrieve(conn, query, acl="admin", filters=filters,
                                    k=SEARCH_K, tracker=tracker)
                # platform-hidden evals are hidden, not user-gated: never reveal
                if h.metadata.get("acl_eval_is_visible") is not False
            ]
            if open_hits:
                classes = sorted({
                    "premium report (purchase required)"
                    if h.metadata.get("acl_premium_gated") else
                    "private document" if h.metadata.get("acl_visibility") == "private"
                    else "unpublished draft"
                    for h in open_hits})
                gated_note = (f"{len(open_hits)} relevant blocks exist but are "
                              f"access-restricted for this user: {', '.join(classes)}. "
                              "Tell the user the material exists but requires access "
                              "— do not say it doesn't exist.")
        items = []
        for h in hits:
            ref = registry.register_chunk(document_id=h.document_id,
                                          block_index=h.block_index,
                                          page_number=h.page_number, title=h.title)
            snippet, truncated = _clip(h.text, SNIPPET_CHARS)
            item = {"ref": ref, "title": h.title, "source_type": h.source_type,
                    "snippet": snippet}
            if h.page_number is not None:
                item["page"] = h.page_number
            if truncated:
                item["_truncated"] = True   # full text via get_source(ref)
            items.append(item)
        out = {"results": items, "k": len(items)}
        if gated_note:
            out["access_note"] = gated_note
        return json.dumps(out)

    def get_source(ref: str) -> str:
        ev = registry.get(ref)
        if not ev or ev.kind != "chunk":
            return json.dumps({"error": f"unknown or non-internal ref {ref!r}"})
        row = conn.execute(
            """SELECT c.text, c.page_number, d.title, d.source_type
               FROM advisor.doc_chunks c JOIN advisor.documents d ON d.id = c.document_id
               WHERE c.document_id = %s AND c.block_index = %s""",
            (ev.document_id, ev.block_index),
        ).fetchone()
        if not row:
            return json.dumps({"error": f"{ref} no longer resolves"})
        text, truncated = _clip(row["text"], SOURCE_CHARS)
        out = {"ref": ref, "title": row["title"], "source_type": row["source_type"],
               "page": row["page_number"], "text": text}
        if truncated:
            out["_truncated"] = True
        return json.dumps(out)

    def structured_query(name: str, params: dict | None = None) -> str:
        try:
            # same ACL the retriever enforces — the two evidence paths must not
            # disagree about what this requester may see (queries.py header)
            rows = run_query(conn, name, params, acl=acl)
        except (KeyError, ValueError, TypeError) as exc:
            # bad query name / bad param value: an error the model can act on,
            # never an uncaught 500 out of the service
            return json.dumps({"error": str(exc)})
        return json.dumps({"rows": rows, "row_count": len(rows)}, default=str)

    def web_research(question: str) -> str:
        from openai import OpenAI

        resp = OpenAI(api_key=os.environ["OPENAI_API_KEY"]).responses.create(
            model=WEB_MODEL,
            tools=[{"type": "web_search"}],
            include=["web_search_call.action.sources"],
            max_output_tokens=1200,  # bounded: latency + cost + replay discipline
            instructions=("Answer concisely with the key facts and figures; "
                          "no preamble, no advice, no follow-up questions."),
            input=question,
        )
        usage = Usage(input_tokens=resp.usage.input_tokens if resp.usage else 0,
                      output_tokens=resp.usage.output_tokens if resp.usage else 0)
        tracker.log(provider="openai", model=WEB_MODEL, stage="tool.web_research",
                    usage=usage, tool_calls=["web_search"])
        # citations must be ATTRIBUTABLE: the model needs (ref, url, title) to know
        # which ref backs which claim — bare ref ids force it to omit citations.
        sources: dict[str, dict] = {}
        for item in resp.output:
            itype = getattr(item, "type", "")
            if itype == "message":
                for content in getattr(item, "content", []) or []:
                    for ann in getattr(content, "annotations", []) or []:
                        if getattr(ann, "type", "") == "url_citation" and getattr(ann, "url", None):
                            ref = registry.register_web(url=ann.url,
                                                        title=getattr(ann, "title", None))
                            sources.setdefault(ref, {"ref": ref, "url": ann.url,
                                                     "title": getattr(ann, "title", None)})
            elif itype == "web_search_call":
                for src in getattr(getattr(item, "action", None), "sources", None) or []:
                    if getattr(src, "type", "") == "url" and getattr(src, "url", None):
                        ref = registry.register_web(url=src.url)
                        sources.setdefault(ref, {"ref": ref, "url": src.url,
                                                 "title": None})
        findings, truncated = _clip(resp.output_text or "", WEB_FINDINGS_CHARS)
        out = {"findings": findings, "sources": list(sources.values())[:8]}
        if truncated:
            out["_truncated"] = True
        return json.dumps(out)

    return [
        Tool(name="search_corpus",
             description=(
                 "Search the X1 corpus (startup/investor/CV profiles, evaluation "
                 "reports and sections, pitch-deck extracts, website content). "
                 "Returns ranked snippets with citation refs. Optional filters: "
                 "source_type (profile|eval_section|eval_premium|eval_basic|"
                 "deck_extract|website), company_name, section_key, entity_type "
                 "(startup_company|investor|cv|investment_company|investment_fund|"
                 "organization). Prefer NO filters for broad discovery questions; "
                 "add filters only to narrow a specific document class."),
             parameters={"type": "object",
                         "properties": {
                             "query": {"type": "string"},
                             "filters": {"type": "object",
                                         "description": "optional metadata equality filters"}},
                         "required": ["query"]},
             function=search_corpus),
        Tool(name="get_source",
             description="Fetch the full text of one evidence block by its ref "
                         "(escalation path when a search snippet was truncated).",
             parameters={"type": "object",
                         "properties": {"ref": {"type": "string"}},
                         "required": ["ref"]},
             function=get_source),
        Tool(name="structured_query",
             description=f"Run a named read-only aggregate/list query. Available: {catalog()}",
             parameters={"type": "object",
                         "properties": {"name": {"type": "string"},
                                        "params": {"type": "object"}},
                         "required": ["name"]},
             function=structured_query),
        Tool(name="web_research",
             description="Research a question on the live web. Call this whenever the "
                         "answer depends on the CURRENT state of the world — market "
                         "conditions, competitor landscapes, funding climate, recent "
                         "news, anything the user frames as 'now'/'currently'/'this "
                         "quarter' — even when corpus documents discuss the topic: "
                         "corpus evaluations are point-in-time snapshots. Returns "
                         "distilled findings + citation refs.",
             parameters={"type": "object",
                         "properties": {"question": {"type": "string"}},
                         "required": ["question"]},
             function=web_research),
    ]
