"""Chunker v1 (`chunker_version = 'ck1'`): structure-aware, block-level only.

Rules (PLAN Phase 1):
  - Paged docs (deck extracts carry ``# Page N`` delimiters): page = slide = chunk;
    oversized pages split on heading/paragraph boundaries, keeping the page number.
  - Non-paged docs: split on headings; paragraphs grouped to a target block size,
    small trailing fragments merged.
  - ``block_index`` is sequential and stable for a given (markdown, chunker) pair;
    re-ingest is version-and-append at the document level, so indices never mutate
    in place.
  - ``char_span`` = [start, end) into the document's markdown.

No sentence granularity, no LLM record-summaries here (those are separate
`granularity='record_summary'` rows added by a later pass through the generator
registry).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CHUNKER_VERSION = "ck1"

PAGE_RE = re.compile(r"^# Page (\d+)\s*$", re.M)
HEADING_RE = re.compile(r"^#{1,4} ", re.M)

TARGET = 2400   # chars; ~500-600 tokens
MIN_BLOCK = 300


@dataclass
class Block:
    block_index: int
    text: str
    page_number: int | None
    char_start: int
    char_end: int


def _split_paragraph_groups(text: str, offset: int, page: int | None,
                            start_index: int) -> list[Block]:
    """Group heading-delimited runs of paragraphs into ~TARGET-char blocks."""
    blocks: list[Block] = []
    # segment boundaries: headings and blank-line paragraph breaks
    pieces: list[tuple[int, str]] = []
    pos = 0
    for para in re.split(r"\n{2,}", text):
        if para.strip():
            at = text.index(para, pos)
            pieces.append((at, para))
            pos = at + len(para)

    cur_start: int | None = None
    cur_end = 0
    cur_len = 0
    idx = start_index

    def flush() -> None:
        nonlocal cur_start, cur_len, idx
        if cur_start is None:
            return
        seg = text[cur_start:cur_end].strip()
        if seg:
            blocks.append(Block(idx, seg, page, offset + cur_start, offset + cur_end))
            idx += 1
        cur_start, cur_len = None, 0

    for at, para in pieces:
        starts_heading = bool(HEADING_RE.match(para))
        if cur_start is not None and (
            cur_len + len(para) > TARGET or (starts_heading and cur_len >= MIN_BLOCK)
        ):
            flush()
        if cur_start is None:
            cur_start = at
        cur_end = at + len(para)
        cur_len += len(para)
    flush()
    return blocks


def chunk_markdown(markdown: str) -> list[Block]:
    """Split a document's markdown into citation blocks (see module docstring)."""
    pages = list(PAGE_RE.finditer(markdown))
    blocks: list[Block] = []

    if len(pages) >= 2:  # paged mode: deck extracts
        for i, m in enumerate(pages):
            page_no = int(m.group(1))
            start = m.end()
            end = pages[i + 1].start() if i + 1 < len(pages) else len(markdown)
            body = markdown[start:end].strip()
            if not body:
                continue
            if len(body) <= TARGET * 2:
                at = markdown.index(body[:40], start)
                blocks.append(Block(len(blocks), body, page_no, at, at + len(body)))
            else:
                for b in _split_paragraph_groups(markdown[start:end], start,
                                                 page_no, len(blocks)):
                    blocks.append(b)
        # anything before the first page marker (title matter) becomes block 0-shifted last
        preamble = markdown[: pages[0].start()].strip()
        if preamble:
            blocks.append(Block(len(blocks), preamble, None, 0, pages[0].start()))
        return blocks

    return _split_paragraph_groups(markdown, 0, None, 0)
