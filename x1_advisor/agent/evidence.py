"""Citation layer (PLAN Phase 4; design §8) — plain Python, framework-independent.

Every evidence block shown to the model carries a tiny ref (`ref1`, `ref2`, …).
The model is instructed to cite claims as `[ref3]` and to omit rather than guess.
After the turn, `validate_citations` repairs/dedupes/resolves/renumbers and DROPS
non-resolving refs. Three evidence kinds, three resolutions:

  chunk  → (document_id, block_index, page_number) — the UI deep-links via get_source
  web    → {url}
  query  → a named registry query + its params, rendered as **platform data**

The third kind exists because exact database answers used to carry no citation
at all: "50 startups, 42 published" was unsourced prose, so that whole question
class satisfied the citation bar trivially while looking perfect (Gate 1B-4).
It is deliberately a distinct type — a live database answer is reproducible by
re-running the query and is true only as of when it ran, which is a different
kind of claim from "this document says X".
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

_REF_RE = re.compile(r"\[\s*(ref\d+)(?:\s*,\s*(ref\d+))*\s*\]", re.I)
_REF_TOKEN = re.compile(r"ref\d+", re.I)


def canonical_params(params: dict[str, Any] | None) -> str:
    """Stable text form of query params — the identity half of a data citation."""
    return json.dumps(params or {}, sort_keys=True, default=str,
                      separators=(",", ":"))


def rows_digest(rows: list[dict[str, Any]]) -> str:
    """Fingerprint of a result set: tells you whether a re-run returned the same
    thing without storing the rows in the citation."""
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class Evidence:
    ref: str
    kind: str                       # 'chunk' | 'web' | 'query'
    title: str | None = None
    document_id: int | None = None
    block_index: int | None = None
    page_number: int | None = None
    url: str | None = None
    # 'query' evidence: a named registry query is reproducible, so its identity
    # is (name, params) and its provenance is (digest, row count, when, policy)
    query_name: str | None = None
    query_params: dict[str, Any] | None = None
    row_count: int | None = None
    result_digest: str | None = None
    acl_policy_version: int | None = None
    executed_at: str | None = None
    # The exact text the model was shown for this ref — snippet, get_source
    # upgrade, per-call web findings, or the structured-query result payload.
    # This is what the claim/citation judge must judge against: the current
    # database row is a different document than the one the model read, and
    # judging against it made scores drift with corpus changes (Gate 1D-1).
    # Web caveat: the snapshot is CALL-level (the call's findings text) —
    # per-URL page text does not exist to capture, so URLs from one call
    # share a snapshot and per-URL attribution is approximate.
    snapshot: str | None = None

    def resolvable(self) -> bool:
        if self.kind == "chunk":
            return self.document_id is not None and self.block_index is not None
        if self.kind == "query":
            return bool(self.query_name)
        return bool(self.url)

    def to_citation(self) -> dict[str, Any]:
        if self.kind == "chunk":
            out = {"type": "internal", "document_id": self.document_id,
                   "block_index": self.block_index, "page_number": self.page_number,
                   "title": self.title}
        elif self.kind == "query":
            # rendered as platform data, deliberately NOT as a document: the
            # reader needs to know this is a live database answer, reproducible
            # by re-running the named query, and true only as of `as_of`
            out = {"type": "platform_data", "title": self.title,
                   "query": self.query_name,
                   "params": self.query_params, "row_count": self.row_count,
                   "result_digest": self.result_digest,
                   "acl_policy_version": self.acl_policy_version,
                   "as_of": self.executed_at}
        else:
            out = {"type": "web", "url": self.url, "title": self.title}
        return {k: v for k, v in out.items() if v is not None}


@dataclass
class EvidenceRegistry:
    """Per-turn registry. Tools register evidence; the validator resolves refs."""

    _items: dict[str, Evidence] = field(default_factory=dict)
    _by_key: dict[tuple, str] = field(default_factory=dict)

    def register_chunk(self, *, document_id: int, block_index: int,
                       page_number: int | None, title: str | None,
                       snapshot: str | None = None) -> str:
        key = ("chunk", document_id, block_index)
        if key in self._by_key:
            ref = self._by_key[key]
            self.upgrade_snapshot(ref, snapshot)
            return ref
        ref = f"ref{len(self._items) + 1}"
        self._items[ref] = Evidence(ref, "chunk", title, document_id,
                                    block_index, page_number, snapshot=snapshot)
        self._by_key[key] = ref
        return ref

    def upgrade_snapshot(self, ref: str, text: str | None) -> None:
        """Keep the most complete view the model has had of this evidence —
        a get_source call shows more of the same block than the search snippet
        did, and the judge should see everything the model saw."""
        ev = self._items.get(ref.lower())
        if ev is not None and text and len(text) > len(ev.snapshot or ""):
            ev.snapshot = text

    def register_query(self, *, query_name: str, params: dict[str, Any] | None,
                       rows: list[dict[str, Any]],
                       acl_policy_version: int | None = None) -> str:
        """Register a structured-query result set as citable evidence.

        Exact database answers ("50 startups, 42 published") used to carry no
        citation at all, so that whole question class passed the citation bar
        with zero evidence while looking perfect. A named registry query is the
        most reproducible evidence the system has — re-run it and check.
        """
        key = ("query", query_name, canonical_params(params))
        if key in self._by_key:
            return self._by_key[key]
        ref = f"ref{len(self._items) + 1}"
        self._items[ref] = Evidence(
            ref, "query", title=f"X1 platform data — {query_name}",
            query_name=query_name, query_params=params or {},
            row_count=len(rows), result_digest=rows_digest(rows),
            acl_policy_version=acl_policy_version,
            executed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._by_key[key] = ref
        return ref

    def register_web(self, *, url: str, title: str | None = None,
                     snapshot: str | None = None) -> str:
        key = ("web", url)
        if key in self._by_key:
            ref = self._by_key[key]
            # a second web_research call may list the same URL under new
            # findings: both texts were shown for this ref, so both are its
            # evidence — append, never replace (distinct from the chunk case,
            # where longer text is a superset of shorter)
            ev = self._items[ref]
            if snapshot and snapshot not in (ev.snapshot or ""):
                ev.snapshot = (f"{ev.snapshot}\n\n{snapshot}" if ev.snapshot
                               else snapshot)
            return ref
        ref = f"ref{len(self._items) + 1}"
        self._items[ref] = Evidence(ref, "web", title, url=url, snapshot=snapshot)
        self._by_key[key] = ref
        return ref

    def get(self, ref: str) -> Evidence | None:
        return self._items.get(ref.lower())

    def ref_for_chunk(self, document_id: int, block_index: int) -> str | None:
        """The ref this registry assigned to a chunk identity, if any — how
        replay remaps a recorded ref onto the registry a fresh tool pass
        built. None means the replayed searches never surfaced the chunk."""
        return self._by_key.get(("chunk", document_id, block_index))

    def query_evidence(self, query_name: str,
                       params: dict[str, Any] | None) -> Evidence | None:
        """The evidence registered for a (query, params) identity, if any."""
        ref = self._by_key.get(("query", query_name, canonical_params(params)))
        return self._items.get(ref) if ref else None

    def __len__(self) -> int:
        return len(self._items)

    def to_list(self) -> list[dict[str, Any]]:
        """Serialize for the turn bundle.

        Tool results carry a `ref` and a title but not the identity behind it,
        so without this a stored turn cannot be replayed: `[ref3]` in the
        recorded answer would resolve to nothing. Replay needs the same mapping
        the live turn had.
        """
        return [asdict(e) for e in self._items.values()]

    @classmethod
    def from_list(cls, items: list[dict[str, Any]]) -> "EvidenceRegistry":
        reg = cls()
        for raw in items or []:
            ev = Evidence(**{k: v for k, v in raw.items()
                             if k in Evidence.__dataclass_fields__})
            reg._items[ev.ref] = ev
            key = (("chunk", ev.document_id, ev.block_index) if ev.kind == "chunk"
                   else ("query", ev.query_name, canonical_params(ev.query_params))
                   if ev.kind == "query" else ("web", ev.url))
            reg._by_key[key] = ev.ref
        return reg


def validate_citations(answer: str, registry: EvidenceRegistry) -> dict[str, Any]:
    """Resolve `[refN]` markers → numbered citations; drop non-resolving refs.

    Returns {"answer": cleaned text with [n] markers, "citations": [..],
             "emitted": count of ref markers seen, "resolved": count kept}.
    """
    order: list[str] = []
    distinct_emitted: set[str] = set()
    dropped: set[str] = set()

    def _sub(match: re.Match) -> str:
        numbers = []
        for token in _REF_TOKEN.findall(match.group(0)):
            ref = token.lower()
            distinct_emitted.add(ref)
            ev = registry.get(ref)
            if ev and ev.resolvable():
                if ref not in order:
                    order.append(ref)
                numbers.append(order.index(ref) + 1)
            else:
                dropped.add(ref)
        numbers = sorted(dict.fromkeys(numbers))
        return "[" + ",".join(str(n) for n in numbers) + "]" if numbers else ""

    cleaned = _REF_RE.sub(_sub, answer)
    cleaned = re.sub(r" +([.,;:])", r"\1", cleaned)          # tidy dangling space
    # `ref` is the join key back to the evidence registry (and its snapshot of
    # what the model saw) — without it the judge cannot pair citation n with
    # its per-ref evidence and had to re-derive it from the live database
    citations = [{**registry.get(ref).to_citation(), "ref": ref, "n": i + 1}
                 for i, ref in enumerate(order)]
    return {"answer": cleaned.strip(), "citations": citations,
            "emitted": len(distinct_emitted), "resolved": len(citations),
            "dropped": sorted(dropped)}
