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
from x1_advisor.agent.queries import QUERIES, catalog, run_query
from x1_advisor.cost import Tracker, Usage, estimate
from x1_advisor.filters import FIELDS, FilterError, compile_filters, filters_json_schema
from x1_advisor.fingerprint import ACL_POLICY_VERSION
from x1_advisor.retrieval import retrieve
from x1_advisor.scan import ScanScope
from x1_advisor.scan import scan as run_scan

SNIPPET_CHARS = 600
SOURCE_CHARS = 6000
SUMMARY_CONTEXT_CHARS = 400
WEB_FINDINGS_CHARS = 1600
SEARCH_K = 8
WEB_MODEL = "gpt-5.1"

# scan_text display caps (bank §3.2A). Counts and entity NAME lists are always
# exact and complete — a scan that dropped a matched entity's identity would
# re-create the sampler problem the tool exists to fix. The caps bound only
# how many excerpt bodies ride back into context (§9), they are visible in the
# tool contract, and overflow is flagged, never silent.
SCAN_EXCERPT_CHARS = 240
SCAN_EXCERPTS_PER_ENTITY = 2      # per matched entity; more via get_source
SCAN_EXCERPT_ENTITY_CAP = 24      # entities with excerpt bodies; rest name-only
SCAN_MAX_PHRASES = 12


def _scan_excerpt(text: str, terms: list[str]) -> str:
    """A window of the block around the first fired term — the citable proof a
    match is a match. Verbatim source text (no whitespace normalization): the
    snapshot registered for the ref must be exactly what the model was shown,
    and quoted spans must survive a verbatim check against the source."""
    text = (text or "").strip()
    lower = text.lower()
    positions = [p for p in (lower.find(t.lower()) for t in terms) if p >= 0]
    start = max(0, min(positions) - SCAN_EXCERPT_CHARS // 3) if positions else 0
    prefix = "… " if start > 0 else ""
    suffix = " …" if start + SCAN_EXCERPT_CHARS < len(text) else ""
    return prefix + text[start:start + SCAN_EXCERPT_CHARS].strip() + suffix

# E7 (PLAN Gate 5): when a record summary routes retrieval to a document, is
# its generated text shown to the model as labelled non-citable context
# (default — current behavior), or does the summary influence ROUTING ONLY?
# The "not citable" boundary is enforcement-by-instruction: nothing structural
# stops a claim born in summary text from wearing the replacement block's ref
# (Gate 1D review, finding 7). Whether hiding the text reduces synthesis_error
# or just degrades answers is an empirical question — E7 runs the suite both
# ways and lets compare decide. Rides the turn fingerprint as a feature flag.
SUMMARY_CONTEXT_ENABLED = os.environ.get(
    "ADVISOR_SUMMARY_CONTEXT", "1") not in ("0", "false", "")


def _clip(text: str, limit: int) -> tuple[str, bool]:
    text = text.strip()
    return (text[:limit].rstrip() + " …", True) if len(text) > limit else (text, False)


def build_tools(conn, *, acl: Any, registry: EvidenceRegistry,
                tracker: Tracker,
                explain_out: list[dict] | None = None) -> list[Tool]:
    """`explain_out` collects one retrieval explain per search (QA-LOOP §4.2).
    It rides to the turn bundle, never into the model's context."""

    def search_corpus(query: str, filters: dict | None = None) -> str:
        # validate + compile at the model-facing boundary: retrieval only ever
        # applies an already-typed filter (filters.py — F1)
        try:
            compiled = compile_filters(conn, filters)
        except FilterError as exc:
            return json.dumps({"error": str(exc)})
        hits = retrieve(conn, query, acl=acl, filters=compiled, k=SEARCH_K,
                        tracker=tracker, explain_out=explain_out,
                        expand_summaries=True)
        # gated-vs-absent: on an empty result for a NON-admin, check (count/class
        # only — no titles, no content) whether access-restricted material exists,
        # so the agent can say "restricted" instead of the misleading "not found"
        gated_note = None
        if not hits and acl != "admin":
            open_hits = [
                h for h in retrieve(conn, query, acl="admin", filters=compiled,
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
            snippet, truncated = _clip(h.text, SNIPPET_CHARS)
            # snapshot = the snippet as shown, truncation marker and all: the
            # judge must judge against what the model read, not the full block
            ref = registry.register_chunk(document_id=h.document_id,
                                          block_index=h.block_index,
                                          page_number=h.page_number, title=h.title,
                                          snapshot=snippet)
            item = {"ref": ref, "title": h.title, "source_type": h.source_type,
                    "snippet": snippet}
            if h.page_number is not None:
                item["page"] = h.page_number
            if truncated:
                item["_truncated"] = True   # full text via get_source(ref)
            if h.routed_by_summary and SUMMARY_CONTEXT_ENABLED:
                # a generated summary routed us to this document. Context only:
                # it has no ref of its own and must never be cited (Gate 1B).
                item["document_summary_not_citable"], _ = _clip(
                    h.routed_by_summary, SUMMARY_CONTEXT_CHARS)
            items.append(item)
        out = {"results": items, "k": len(items)}
        if compiled.notes:
            # F7: a filter value that matched nothing known says so, with the
            # nearest stored values — an empty result is never left ambiguous
            out["filter_notes"] = list(compiled.notes)
        if gated_note:
            out["access_note"] = gated_note
        return json.dumps(out)

    def scan_text(phrases: Any, entity_type: str = "startup_company",
                  source_types: Any = None, match: str = "phrase") -> str:
        # every validation failure is an error the model can act on — echoing
        # the valid vocabulary, F7-style, never an empty result it might read
        # as "the corpus holds nothing"
        if isinstance(phrases, str):
            phrases = [phrases]
        phrases = [p.strip() for p in (phrases or [])
                   if isinstance(p, str) and p.strip()]
        if not phrases:
            return json.dumps({"error": "phrases must be a non-empty list of "
                                        "text phrases"})
        if len(phrases) > SCAN_MAX_PHRASES:
            return json.dumps({"error": f"at most {SCAN_MAX_PHRASES} phrases "
                                        "per scan — run narrower scans"})
        ef = FIELDS["entity_type"]
        entity_type = ef.aliases.get(entity_type, entity_type)
        if entity_type not in ef.values:
            return json.dumps({"error": f"unknown entity_type {entity_type!r}; "
                                        f"valid: {sorted(ef.values)}"})
        sf = FIELDS["source_type"]
        if isinstance(source_types, str):
            source_types = [source_types]
        source_types = list(source_types or sf.values)
        unknown = sorted(set(source_types) - set(sf.values))
        if unknown:
            return json.dumps({"error": f"unknown source_types {unknown}; "
                                        f"valid: {sorted(sf.values)}"})
        if match not in ("phrase", "keywords"):
            return json.dumps({"error": "match must be 'phrase' or 'keywords'"})

        scope = ScanScope(entity_type=entity_type, entity_key="name",
                          source_types=tuple(source_types),
                          # blocks only, structurally: record summaries are
                          # generated text, not evidence (Gate 1B) — a scan
                          # match must be a match in a source document
                          granularity=("block",),
                          method="phrase" if match == "phrase" else "fts",
                          any_terms=tuple(phrases), all_terms=())
        result = run_scan(conn, scope, acl=acl, include_text=True)

        # the scan itself is citable evidence, query-kind: deterministic and
        # reproducible from (scope, corpus), which is exactly what a coverage
        # claim ("3 of 52 mention X") needs behind it
        scope_dict = {"entity_type": entity_type, "source_types": source_types,
                      "match": match, "phrases": phrases}
        scan_ref = registry.register_query(
            query_name="scan_text", params=scope_dict,
            rows=[{"entity": e["key"], "status": e["status"]}
                  for e in result["entities"]],
            acl_policy_version=ACL_POLICY_VERSION)

        matched_out: list[dict] = []
        capped = 0
        for e in (e for e in result["entities"] if e["status"] == "matched"):
            item: dict[str, Any] = {"entity": e["key"],
                                    "matched_chunks": len(e["chunks"])}
            if e.get("gated_unscanned"):
                item["gated_unscanned"] = True
            chunks = sorted(e["chunks"], key=lambda c: (c["document_id"],
                                                        c["block_index"]))
            if len(matched_out) < SCAN_EXCERPT_ENTITY_CAP:
                excerpts = []
                for chunk in chunks[:SCAN_EXCERPTS_PER_ENTITY]:
                    excerpt = _scan_excerpt(chunk.get("text") or "",
                                            chunk["terms"])
                    ref = registry.register_chunk(
                        document_id=chunk["document_id"],
                        block_index=chunk["block_index"],
                        page_number=chunk.get("page_number"),
                        title=chunk.get("title"), snapshot=excerpt)
                    ex = {"ref": ref, "title": chunk.get("title"),
                          "terms": chunk["terms"], "excerpt": excerpt}
                    if chunk.get("page_number") is not None:
                        ex["page"] = chunk["page_number"]
                    excerpts.append(ex)
                item["excerpts"] = excerpts
                if len(chunks) > SCAN_EXCERPTS_PER_ENTITY:
                    # counted, just not displayed — full text via get_source
                    item["more_matches"] = len(chunks) - SCAN_EXCERPTS_PER_ENTITY
            else:
                capped += 1
                item["excerpts"] = []
            matched_out.append(item)

        out: dict[str, Any] = {
            "ref": scan_ref, "scope": scope_dict, "counts": result["counts"],
            "matched": matched_out,
            "no_match": [e["key"] for e in result["entities"]
                         if e["status"] == "no_match"],
            "not_indexed": [e["key"] for e in result["entities"]
                            if e["status"] == "not_indexed"],
        }
        restricted = [e["key"] for e in result["entities"]
                      if e["status"] == "restricted"]
        if restricted:
            out["restricted"] = restricted
            out["access_note"] = (
                "'restricted' entities have purchase-gated material in scope "
                "that was NOT scanned. Tell the user gated material exists for "
                "them and requires access — do not report absence.")
        if capped:
            out["_excerpts_capped"] = (
                f"excerpt bodies shown for the first {SCAN_EXCERPT_ENTITY_CAP} "
                f"matched entities only ({capped} more listed without "
                "excerpts); all names and counts are exact")
        payload = json.dumps(out)
        registry.upgrade_snapshot(scan_ref, payload)  # verbatim what the model saw
        return payload

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
        # the model has now seen more of this block than the search snippet —
        # the ref's snapshot upgrades to the fuller view
        registry.upgrade_snapshot(ref, text)
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
        # exact database answers are evidence too, and the most reproducible
        # kind the system has (Gate 1B-4). Echoing name/params keeps the result
        # self-describing for replay and for the claim/citation judge.
        ref = registry.register_query(query_name=name, params=params or {},
                                      rows=rows,
                                      acl_policy_version=ACL_POLICY_VERSION)
        payload = json.dumps({"ref": ref, "query": name, "params": params or {},
                              # rows alone are not self-describing: "47" does not
                              # say *what* was counted or under whose scope. The
                              # description carries the predicates the SQL applied,
                              # so the claim built on it can actually be checked.
                              "description": QUERIES[name]["description"],
                              "acl_scope": "admin" if acl == "admin" else "requesting user",
                              "rows": rows, "row_count": len(rows)}, default=str)
        registry.upgrade_snapshot(ref, payload)   # verbatim what the model saw
        return payload

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
        # Each ref's snapshot is THIS call's findings — not a pool of every web
        # call in the turn, which credited one URL with another's evidence.
        findings, truncated = _clip(resp.output_text or "", WEB_FINDINGS_CHARS)
        sources: dict[str, dict] = {}
        for item in resp.output:
            itype = getattr(item, "type", "")
            if itype == "message":
                for content in getattr(item, "content", []) or []:
                    for ann in getattr(content, "annotations", []) or []:
                        if getattr(ann, "type", "") == "url_citation" and getattr(ann, "url", None):
                            ref = registry.register_web(url=ann.url,
                                                        title=getattr(ann, "title", None),
                                                        snapshot=findings)
                            sources.setdefault(ref, {"ref": ref, "url": ann.url,
                                                     "title": getattr(ann, "title", None)})
            elif itype == "web_search_call":
                for src in getattr(getattr(item, "action", None), "sources", None) or []:
                    if getattr(src, "type", "") == "url" and getattr(src, "url", None):
                        ref = registry.register_web(url=src.url, snapshot=findings)
                        sources.setdefault(ref, {"ref": ref, "url": src.url,
                                                 "title": None})
        out = {"findings": findings, "sources": list(sources.values())[:8]}
        if truncated:
            out["_truncated"] = True
        return json.dumps(out)

    return [
        Tool(name="search_corpus",
             description=(
                 "Search the X1 corpus (startup/investor/CV profiles, evaluation "
                 "reports and sections, pitch-deck extracts, website content). "
                 "Returns ranked snippets with citation refs. Filters are optional "
                 "and narrow the search; each accepts one value or a list of "
                 "values. Prefer NO filters for broad discovery questions; add "
                 "filters only to narrow a specific document class. A result may "
                 "carry `document_summary_not_citable`: an auto-generated gist of "
                 "the document, useful for judging relevance but NOT evidence — "
                 "cite the result's `ref`, which points at the source text, and "
                 "call get_source(ref) if you need to verify a detail the summary "
                 "asserts. For exhaustive which/all/every checks over a scope, "
                 "use scan_text instead."),
             parameters={"type": "object",
                         "properties": {"query": {"type": "string"},
                                        "filters": filters_json_schema()},
                         "required": ["query"]},
             function=search_corpus),
        Tool(name="scan_text",
             description=(
                 "Exhaustively scan EVERY indexed document block in a bounded "
                 "scope for exact phrases (or keywords), returning a per-entity "
                 "verdict — matched / no_match / not_indexed — with complete "
                 "coverage counts and a citable excerpt per match. Unlike "
                 "search_corpus (a top-ranked sample), this is a census of the "
                 "indexed text: use it for 'which/all/every/how many … mention "
                 "X' questions over corpus entities instead of repeated "
                 "searches, and cite the result's own `ref` for coverage "
                 "counts. A no_match is lexical — the phrases did not appear "
                 "in that entity's indexed text — never proof the topic is "
                 "absent semantically. Each match reports which phrase fired "
                 "(`terms`) — attribute matches to the fired phrase, not to "
                 "your search intent. A scan covers only documents attributed "
                 "to the scanned class. Information about people also lives "
                 "in company documents (team/founder evaluation sections, "
                 "pitch decks) — for people questions, scan `cv` for the "
                 "per-person census and additionally search company "
                 "documents, attributing each finding to its source."),
             parameters={"type": "object",
                         "properties": {
                             "phrases": {
                                 "type": "array", "items": {"type": "string"},
                                 "description": (
                                     "Short literal phrases; an entity matches "
                                     "if ANY phrase appears (case-insensitive). "
                                     "Include common variants (e.g. 'FDA', "
                                     f"'CE mark'). Max {SCAN_MAX_PHRASES}.")},
                             "entity_type": {
                                 "type": "string",
                                 "enum": list(FIELDS["entity_type"].values),
                                 "description": "the entity class the scan "
                                                "enumerates — every verdict "
                                                "and count is one entity of "
                                                "this class (cv = an "
                                                "individual person via their "
                                                "CV/profile; startup_company "
                                                "= a company)"},
                             "source_types": {
                                 "description": "document classes to scan "
                                                "(default: all)",
                                 "anyOf": [
                                     {"type": "string",
                                      "enum": list(FIELDS["source_type"].values)},
                                     {"type": "array",
                                      "items": {"type": "string",
                                                "enum": list(FIELDS["source_type"].values)}}]},
                             "match": {
                                 "type": "string",
                                 "enum": ["phrase", "keywords"],
                                 "description": "phrase = exact substring "
                                                "(default); keywords = stemmed "
                                                "full-text word matching"},
                         },
                         "required": ["phrases"]},
             function=scan_text),
        Tool(name="get_source",
             description="Fetch the full text of one evidence block by its ref "
                         "(escalation path when a search snippet was truncated).",
             parameters={"type": "object",
                         "properties": {"ref": {"type": "string"}},
                         "required": ["ref"]},
             function=get_source),
        Tool(name="structured_query",
             description=(
                 "Run a named read-only aggregate/list query against the platform "
                 "database. The result carries a citation `ref` like any other "
                 "evidence — cite it for the counts, lists and rankings it "
                 f"returns. Available: {catalog()}"),
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
