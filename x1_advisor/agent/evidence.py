"""Citation layer (PLAN Phase 4; design §8) — plain Python, framework-independent.

Every evidence block shown to the model carries a tiny ref (`ref1`, `ref2`, …).
The model is instructed to cite claims as `[ref3]` and to omit rather than guess.
After the turn, `validate_citations` repairs/dedupes/resolves/renumbers and DROPS
non-resolving refs: internal evidence resolves to (document_id, block_index,
page_number), web evidence to {url}. The final answer shows `[1] [2] …` with a
source list — the UI deep-links via get_source / the url.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_REF_RE = re.compile(r"\[\s*(ref\d+)(?:\s*,\s*(ref\d+))*\s*\]", re.I)
_REF_TOKEN = re.compile(r"ref\d+", re.I)


@dataclass
class Evidence:
    ref: str
    kind: str                       # 'chunk' | 'web'
    title: str | None = None
    document_id: int | None = None
    block_index: int | None = None
    page_number: int | None = None
    url: str | None = None

    def resolvable(self) -> bool:
        if self.kind == "chunk":
            return self.document_id is not None and self.block_index is not None
        return bool(self.url)

    def to_citation(self) -> dict[str, Any]:
        if self.kind == "chunk":
            out = {"type": "internal", "document_id": self.document_id,
                   "block_index": self.block_index, "page_number": self.page_number,
                   "title": self.title}
        else:
            out = {"type": "web", "url": self.url, "title": self.title}
        return {k: v for k, v in out.items() if v is not None}


@dataclass
class EvidenceRegistry:
    """Per-turn registry. Tools register evidence; the validator resolves refs."""

    _items: dict[str, Evidence] = field(default_factory=dict)
    _by_key: dict[tuple, str] = field(default_factory=dict)

    def register_chunk(self, *, document_id: int, block_index: int,
                       page_number: int | None, title: str | None) -> str:
        key = ("chunk", document_id, block_index)
        if key in self._by_key:
            return self._by_key[key]
        ref = f"ref{len(self._items) + 1}"
        self._items[ref] = Evidence(ref, "chunk", title, document_id,
                                    block_index, page_number)
        self._by_key[key] = ref
        return ref

    def register_web(self, *, url: str, title: str | None = None) -> str:
        key = ("web", url)
        if key in self._by_key:
            return self._by_key[key]
        ref = f"ref{len(self._items) + 1}"
        self._items[ref] = Evidence(ref, "web", title, url=url)
        self._by_key[key] = ref
        return ref

    def get(self, ref: str) -> Evidence | None:
        return self._items.get(ref.lower())

    def __len__(self) -> int:
        return len(self._items)


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
    citations = [{**registry.get(ref).to_citation(), "n": i + 1}
                 for i, ref in enumerate(order)]
    return {"answer": cleaned.strip(), "citations": citations,
            "emitted": len(distinct_emitted), "resolved": len(citations),
            "dropped": sorted(dropped)}
