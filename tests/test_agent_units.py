"""Unit tests: prompt-prefix stability (§9 CI assertion), citation validator,
chunker, structured-query ACL predicates.

Run: uv run pytest -q
"""

import hashlib

import pytest

from x1_advisor.agent import queries
from x1_advisor.agent.advisor import SYSTEM_PROMPT
from x1_advisor.agent.evidence import EvidenceRegistry, validate_citations
from x1_advisor.ingest.chunker import chunk_markdown

# §9: the system prompt is the cached prompt prefix. Any byte change invalidates
# the cache for every turn — so changing it must be deliberate: update this hash
# in the same commit and say why in the message.
SYSTEM_PROMPT_SHA256 = "dc236bb7a28dc61c3dde170aead6f7c328eaa10024bb763a30d6388a7ca3c13a"


def test_prompt_prefix_stability():
    actual = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
    assert actual == SYSTEM_PROMPT_SHA256, (
        "SYSTEM_PROMPT changed — this invalidates the prompt cache for every turn. "
        f"If intentional, update SYSTEM_PROMPT_SHA256 to {actual!r} in this test."
    )


def test_citation_validator_resolves_dedupes_and_drops():
    reg = EvidenceRegistry()
    r1 = reg.register_chunk(document_id=10, block_index=2, page_number=None, title="Doc A")
    r2 = reg.register_web(url="https://example.com/x", title="Web B")
    answer = f"Claim one [{r1}]. Claim two [{r1}, {r2}]. Bogus [ref99]. Again [{r2}]"
    out = validate_citations(answer, reg)
    assert out["resolved"] == 2 and out["emitted"] == 3
    assert out["dropped"] == ["ref99"]
    assert "[ref" not in out["answer"] and "[1]" in out["answer"] and "[1,2]" in out["answer"]
    assert out["citations"][0] == {"type": "internal", "document_id": 10,
                                   "block_index": 2, "title": "Doc A", "n": 1}
    assert out["citations"][1]["url"] == "https://example.com/x"


def test_chunk_dedup_registry_reuses_refs():
    reg = EvidenceRegistry()
    a = reg.register_chunk(document_id=1, block_index=1, page_number=3, title="T")
    b = reg.register_chunk(document_id=1, block_index=1, page_number=3, title="T")
    assert a == b and len(reg) == 1


def test_chunker_paged_mode_and_spans():
    md = "# Page 1\n\nSlide one body.\n\n# Page 2\n\nSlide two body with more text."
    blocks = chunk_markdown(md)
    assert [b.page_number for b in blocks] == [1, 2]
    for b in blocks:
        assert md[b.char_start:b.char_end].strip() == b.text


def test_structured_query_acl_predicates():
    # admin is unrestricted; everyone else is gated on both classes
    assert queries._company_acl("admin") == ("", [])
    assert queries._eval_acl("admin") == ("", [])
    assert queries._company_acl({"user_id": 1}) == (" AND s.is_published", [])
    assert queries._eval_acl({"user_id": 1}) == (" AND e.is_visible", [])
    # drafts have an owner carve-out; hidden evaluations deliberately do not
    sql, args = queries._company_acl(
        {"user_id": 1, "owned_entity_ids": {"startup_company": [3, 4]}})
    assert sql == " AND (s.is_published OR s.id = ANY(%s))" and args == [[3, 4]]
    assert queries._eval_acl(
        {"user_id": 1, "owned_entity_ids": {"startup_company": [3]}}) == (
            " AND e.is_visible", [])
    for bad in ("everyone", None, 7):
        with pytest.raises(ValueError):
            queries._company_acl(bad)


def test_structured_query_limit_validation():
    assert queries._limit({}, 10) == 10
    assert queries._limit({"limit": "7"}, 10) == 7
    assert queries._limit({"limit": 9999}, 10) == queries.MAX_ROWS   # capped, not silent
    for bad in ({"limit": "many"}, {"limit": None}, {"limit": 0}, {"limit": -3}):
        with pytest.raises(ValueError):
            queries._limit(bad, 10)


def test_run_query_requires_a_valid_acl():
    with pytest.raises(ValueError):
        queries.run_query(None, "count_startups", None, acl="everyone")
    with pytest.raises(KeyError):
        queries.run_query(None, "no_such_query", None, acl="admin")


def test_chunker_groups_paragraphs_under_headings():
    md = "\n\n".join(["## Section A", "para " + "x" * 300, "## Section B", "para " + "y" * 300])
    blocks = chunk_markdown(md)
    assert len(blocks) == 2
    assert blocks[0].text.startswith("## Section A")
    assert blocks[1].text.startswith("## Section B")
