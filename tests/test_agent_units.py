"""Unit tests: prompt-prefix stability (§9 CI assertion), citation validator,
chunker, structured-query ACL predicates.

Run: uv run pytest -q
"""

import hashlib

import pytest

from x1_advisor import filters
from x1_advisor.agent import queries
from x1_advisor.agent.advisor import SYSTEM_PROMPT
from x1_advisor.agent.evidence import EvidenceRegistry, validate_citations
from x1_advisor.ingest.chunker import chunk_markdown

# §9: the cached prompt prefix is the system prompt PLUS the tool schemas. Any
# byte change in either invalidates the cache for every turn — so changing one
# must be deliberate: update the hash in the same commit and say why in the
# message. Pinning only the prompt missed four tool-description edits in Phase 4
# alone (DESIGN-REVIEW F4).
SYSTEM_PROMPT_SHA256 = "dc236bb7a28dc61c3dde170aead6f7c328eaa10024bb763a30d6388a7ca3c13a"
# 2026-07-30 Gate 1B: search_corpus documents `document_summary_not_citable`
TOOL_SCHEMA_SHA256 = "5a92782e69c0468b35c3bbb8d5a9de07f6464ef35708d4da869075e97b8a8976"


def test_prompt_prefix_stability():
    actual = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
    assert actual == SYSTEM_PROMPT_SHA256, (
        "SYSTEM_PROMPT changed — this invalidates the prompt cache for every turn. "
        f"If intentional, update SYSTEM_PROMPT_SHA256 to {actual!r} in this test."
    )


def test_tool_schema_stability():
    from x1_advisor.agent.tools import build_tools
    from x1_advisor.fingerprint import tool_schema_digest

    # build_tools only closes over conn/tracker; it touches neither at build time
    tools = build_tools(None, acl="admin", registry=EvidenceRegistry(), tracker=None)
    actual = tool_schema_digest(tools)
    assert actual == TOOL_SCHEMA_SHA256, (
        "A tool name, description or parameter schema changed — this invalidates "
        "the prompt cache for every turn, exactly like a SYSTEM_PROMPT edit. Note "
        "structured_query's description embeds queries.catalog() and search_corpus's "
        "schema embeds the filter registry, so registry edits land here too. If "
        f"intentional, update TOOL_SCHEMA_SHA256 to {actual!r}."
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


def test_manifests_never_overwrite(tmp_path):
    from experiments.manifest import open_new_manifest

    ids, paths = [], []
    for _ in range(3):
        run_id, path, fh = open_new_manifest("2026-07-30_cfg_v1", runs_dir=tmp_path)
        fh.write(run_id + "\n")
        fh.close()
        ids.append(run_id)
        paths.append(path)
    assert len(set(paths)) == 3, "a rerun overwrote an existing manifest"
    assert [p.name.rsplit("_r", 1)[1] for p in paths] == ["1.jsonl", "2.jsonl", "3.jsonl"]
    # the run_id inside the file identifies the file it lives in
    for run_id, path in zip(ids, paths):
        assert path.read_text().strip() == run_id == path.stem
    # a pre-existing file at the next sequence number is stepped over, not clobbered
    (tmp_path / paths[0].name).write_text("original")
    _, path, fh = open_new_manifest("2026-07-30_cfg_v1", runs_dir=tmp_path)
    fh.close()
    assert path != paths[0] and (tmp_path / paths[0].name).read_text() == "original"


def test_chunker_paged_mode_and_spans():
    md = "# Page 1\n\nSlide one body.\n\n# Page 2\n\nSlide two body with more text."
    blocks = chunk_markdown(md)
    assert [b.page_number for b in blocks] == [1, 2]
    for b in blocks:
        assert md[b.char_start:b.char_end].strip() == b.text


def test_filter_registry_rejects_unknown_keys_and_injection():
    # the F1 chain: a prompt-injected filter KEY must never reach SQL
    for hostile in ("company_name') = '' OR 1=1 --",
                    "x' , (SELECT 1) AS y --",
                    "source_type; DROP TABLE advisor.documents",
                    "SOURCE_TYPE",       # registry is exact-match, not fuzzy
                    ""):
        with pytest.raises(filters.FilterError):
            filters.compile_filters(None, {hostile: "profile"})


def test_filter_enum_aliases_and_validation():
    c = filters.compile_filters(None, {"entity_type": "startup"})
    assert c.applied == {"entity_type": "startup_company"}
    assert c.sql == " AND c.metadata->>'entity_type' = %s"
    assert c.params == ("startup_company",) and not c.notes
    # list form → ANY(), still a single bound parameter
    c = filters.compile_filters(None, {"source_type": ["eval_section", "eval_premium"]})
    assert c.sql == " AND c.metadata->>'source_type' = ANY(%s)"
    assert c.params == (["eval_section", "eval_premium"],)
    with pytest.raises(filters.FilterError):
        filters.compile_filters(None, {"source_type": "eval_sections"})
    for bad in ({"section_key": []}, {"section_key": "  "}, {"section_key": True},
                {"section_key": {"a": 1}}):
        with pytest.raises(filters.FilterError):
            filters.compile_filters(None, bad)


def test_filter_schema_is_static_and_matches_registry():
    schema = filters.filters_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(filters.FIELDS)
    assert schema["properties"]["source_type"]["anyOf"][0]["enum"] == \
        list(filters.FIELDS["source_type"].values)
    assert "enum" not in schema["properties"]["company_name"]["anyOf"][0]


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
