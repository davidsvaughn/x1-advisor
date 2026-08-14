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
# 2026-08-05: overclaim-discipline rule (David-approved wording) — rule 7 now
# requires crediting scan matches to the phrase that fired, never upgraded to
# the asked concept (15 overclaimed entities in the fdba68a baseline run).
# 2026-08-06: census-completeness rule (David-approved wording) — rule 5 now
# requires every matched entity to appear by name; grouping/annotating is
# welcome, silent curation is not (one 1bb0fe1 run dropped 4 matched names
# as "incidental" without naming them).
# 2026-08-06 (2): population-statistics rule (David-approved) — rule 6 now
# requires running structured_query for statistics and reporting what the
# registry computes, named for what it measures; a figure synthesized from
# retrieved passages is a sample artifact, not a statistic (an agent declined
# an aggregate it could partially serve, without running the registry, and
# characterized corpus-wide content off two searches).
SYSTEM_PROMPT_SHA256 = "01ffce9b81be013ec94740e69e1992639e4978ff019a3aeca276ed857f48e1c5"
# 2026-08-05 (2): entity-class semantics (David-approved) — entity_type is
# the unit the census enumerates; person-evidence also lives in company
# docs (team/founder sections outweigh the cv corpus ~4x), so people
# questions pair a cv census with a company-doc search.
# 2026-08-06: base-token probe guidance (David-approved) — scan_text now
# tells the agent to probe each concept's base token alongside compound
# phrases; a compound-only scan reliably missed a base-token-only entity
# in all three post-fix measurement runs.
# 2026-08-06 (2): terms_fired rollup (David-approved) — each matched entity
# reports exact per-phrase matching-chunk counts; excerpt sampling hid
# which phrase fired (v2c012: agent asserted an exact phrase was absent
# corpus-wide while it appeared verbatim in an unsampled chunk).
# 2026-08-06 (3): four registry queries (the David-approved coverage/aggregate
# capability build, bank §1.4/§1.5/§3.3): documents_for_company,
# evaluation_score_stats, investors_for_company, count_cvs — the catalog is
# embedded in structured_query's description, so the digest moves.
# 2026-08-14: label resolver (triage thread-021 issue 2, David-queued):
# startups_by_label + list_labels — one reusable resolver across the
# platform's label vocabularies (industry/sector/region), replacing the
# 34s text-census fallback for classification questions.
TOOL_SCHEMA_SHA256 = "18efb7430a0489ce8034a51d6461937fafe1427bba9af788b672217e924549c6"


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
                                   "block_index": 2, "title": "Doc A",
                                   "ref": r1, "n": 1}
    assert out["citations"][1]["url"] == "https://example.com/x"
    assert out["citations"][1]["ref"] == r2   # the join key to evidence snapshots


def test_structured_query_results_are_citable_platform_data():
    reg = EvidenceRegistry()
    rows = [{"startups": 50, "published": 42}]
    r1 = reg.register_query(query_name="count_startups", params={}, rows=rows,
                            acl_policy_version=2)
    # identical (query, params) dedupes to one ref; different params do not
    assert reg.register_query(query_name="count_startups", params={}, rows=rows,
                              acl_policy_version=2) == r1
    r2 = reg.register_query(query_name="list_startups", params={"limit": 2},
                            rows=[{"name": "A"}, {"name": "B"}], acl_policy_version=2)
    assert r2 != r1 and len(reg) == 2

    out = validate_citations(f"There are 50 startups [{r1}]; two are [{r2}].", reg)
    assert out["resolved"] == 2 and out["dropped"] == []
    c = out["citations"][0]
    # rendered as platform data, never as a document
    assert c["type"] == "platform_data" and c["query"] == "count_startups"
    assert c["row_count"] == 1 and c["acl_policy_version"] == 2
    assert c["as_of"] and c["result_digest"]
    assert "document_id" not in c and "url" not in c
    # the digest is identity, not decoration: different rows, different digest
    reg2 = EvidenceRegistry()
    reg2.register_query(query_name="count_startups", params={},
                        rows=[{"startups": 51, "published": 42}], acl_policy_version=2)
    assert reg2.get("ref1").result_digest != reg.get(r1).result_digest


def test_chunk_dedup_registry_reuses_refs():
    reg = EvidenceRegistry()
    a = reg.register_chunk(document_id=1, block_index=1, page_number=3, title="T")
    b = reg.register_chunk(document_id=1, block_index=1, page_number=3, title="T")
    assert a == b and len(reg) == 1


def test_evidence_snapshots_record_what_the_model_saw():
    # Gate 1D-1: the judge must judge against these, never the live database
    reg = EvidenceRegistry()
    r = reg.register_chunk(document_id=1, block_index=1, page_number=None,
                           title="T", snapshot="short snippet …")
    # get_source shows MORE of the same block → snapshot upgrades to the
    # fuller view; a later, shorter view never downgrades it
    reg.upgrade_snapshot(r, "the full block text, much longer than the snippet")
    reg.upgrade_snapshot(r, "tiny")
    assert reg.get(r).snapshot == "the full block text, much longer than the snippet"
    # a re-search returning the same chunk upgrades through register too
    reg2 = EvidenceRegistry()
    a = reg2.register_chunk(document_id=1, block_index=1, page_number=None,
                            title="T", snapshot="123")
    reg2.register_chunk(document_id=1, block_index=1, page_number=None,
                        title="T", snapshot="123456")
    assert reg2.get(a).snapshot == "123456"
    # web evidence APPENDS across calls: both findings texts were shown for
    # this ref, and neither is a superset of the other
    w = reg.register_web(url="https://x.test/a", snapshot="findings from call 1")
    reg.register_web(url="https://x.test/a", snapshot="findings from call 2")
    reg.register_web(url="https://x.test/a", snapshot="findings from call 1")
    assert reg.get(w).snapshot == "findings from call 1\n\nfindings from call 2"
    # snapshots ride the bundle round-trip that replay and the judge rely on
    back = EvidenceRegistry.from_list(reg.to_list())
    assert back.get(r).snapshot == reg.get(r).snapshot
    assert back.get(w).snapshot == reg.get(w).snapshot
    # but they are turn-bundle data, never part of the outward citation
    assert "snapshot" not in reg.get(r).to_citation()


def test_judge_evidence_provenance_detection():
    from x1_advisor.agent.judge import evidence_provenance, evidence_texts

    reg = EvidenceRegistry()
    r = reg.register_chunk(document_id=7, block_index=0, page_number=None,
                           title="T", snapshot="what the model saw")
    validated = validate_citations(f"Claim [{r}].", reg)
    bundle = {"evidence": reg.to_list(), "validation": validated, "messages": []}
    assert evidence_provenance(bundle) == "turn-snapshot"
    # snapshot path needs no database connection at all
    texts = evidence_texts(None, bundle)
    assert texts[1]["text"] == "what the model saw"
    assert texts[1]["kind"] == "internal"
    # a pre-snapshot (schema v2) bundle is detected, not silently mis-judged
    legacy = {"evidence": [{"ref": "ref1", "kind": "chunk", "document_id": 7,
                            "block_index": 0}],
              "validation": validated, "messages": []}
    assert evidence_provenance(legacy) == "reconstructed-legacy"
    # zero-citation bundles go by their schema contract, not by resolution
    assert evidence_provenance({"evidence": [], "validation": {"citations": []},
                                "messages": []}) == "reconstructed-legacy"
    assert evidence_provenance({"schema_version": 3, "evidence": [],
                                "validation": {"citations": []},
                                "messages": []}) == "turn-snapshot"


def test_summary_windows_cover_the_whole_document():
    from x1_advisor.ingest.summaries import MAP_WINDOW_CHARS, split_windows

    # the bug this replaced was a silent head slice, so the invariant that
    # matters is coverage: every character reaches exactly one window, in order
    for text in ("", "short doc", "a" * 30_000,
                 ("para " * 500 + "\n\n") * 20, "x" * 12_000 + "\n\n" + "y" * 5):
        windows = split_windows(text)
        assert windows, "splitter must always produce at least one window"
        assert all(len(w) <= MAP_WINDOW_CHARS for w in windows)
        assert "".join(windows).replace("\n\n", "") == text.replace("\n\n", "")


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


def test_calibration_row_order_does_not_leak_the_stratum():
    """Blindness is a property of the file, not just its fields.

    The sampler draws one item per stratum in a fixed rotation, so before
    `blind_order` the pending file's line position spelled out the judge's
    verdict exactly: index % 3 == 0 was `unsupported`, 1 `partial`, 2
    `supported`, for every one of 32 rows.
    """
    from experiments.judge_calibrate import blind_order

    cycle = ["unsupported", "partial", "supported"]
    items = [{"id": f"g{i // 3:03d}_v{i}", "stratum": cycle[i % 3]}
             for i in range(30)]
    assert [i["stratum"] for i in items] == [cycle[i % 3] for i in range(30)]

    out = blind_order(items)
    for m in range(3):        # no residue class may be one stratum only
        assert len({it["stratum"] for k, it in enumerate(out) if k % 3 == m}) > 1
    assert sorted(i["id"] for i in out) == sorted(i["id"] for i in items)
    assert blind_order(items) == out          # deterministic, no seed


def test_usage_reads_cached_tokens_from_both_openai_wire_shapes():
    """Responses and Chat Completions disagree on where cached tokens live.

    Reading only `prompt_tokens_details` scores every Responses-API call as
    zero cached and bills the whole prompt at full input price — silent, and
    worst exactly where the cached prefix is largest.
    """
    from x1_advisor.cost import Usage

    completions = Usage.from_haystack_meta("openai", {"usage": {
        "prompt_tokens": 1000, "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 800}}})
    responses = Usage.from_haystack_meta("openai", {"usage": {
        "input_tokens": 1000, "output_tokens": 50,
        "input_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0}}})

    assert completions == responses
    assert responses.cache_read_tokens == 800
    assert responses.input_tokens == 200        # uncached remainder, not 1000


def test_cache_writes_are_counted_and_billed_on_gpt_5_6():
    """gpt-5.6 bills cache writes at 1.25x input; an earlier revision zeroed them.

    A test that only ever passes `cache_write_tokens: 0` cannot see this, which
    is how the defect survived its first test.
    """
    from x1_advisor.cost import PRICING, Usage, estimate

    u = Usage.from_haystack_meta("openai", {"usage": {
        "input_tokens": 1000, "output_tokens": 0,
        "input_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 400}}})
    assert (u.cache_write_tokens, u.cache_read_tokens, u.input_tokens) == (400, 100, 500)

    rates = PRICING["openai"]["gpt-5.6-terra"]
    assert rates["cache_write"] == rates["input"] * 1.25

    b = estimate(provider="openai", model="gpt-5.6-terra", usage=u)
    assert b.cache_write_cost == 400 * rates["cache_write"] / 1_000_000
    assert b.cache_write_cost > 0        # the bug: silently 0
