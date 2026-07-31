"""Unit tests: golden v2 case schema + compiler (GOLDEN-V2-DESIGN §4/§7/§9.1).

The compiler's job is to fail loudly on things that would otherwise grade
nothing and report PASS, so most of these tests assert on *rejection*. Nothing
here touches the database or a model.

Run: uv run pytest -q
"""

from __future__ import annotations

import copy

import pytest
import yaml

from experiments.cases import (
    CaseValidationError,
    compile_suite,
    load_suite,
    render_question,
    resolve_bindings,
)

# A minimal valid case: corpus enumeration under the honesty contract, exactly
# the shape §4 works through.
BASE_CASE = {
    "id": "v2c001",
    "class": "enumeration_text",
    "tier": "core",
    "provenance": "bank#23 (KS+SCR+SKL+LFT)",
    "question": "Which startup evaluations mention regulatory risk?",
    "bindings": {},
    "readiness": {
        "source_available": True,
        "tool_ready": False,
        "scope": "corpus",
        "operation": "exact_scan",
        "context_required": "none",
        "golden_priority": "p0",
    },
    "expected_route": ["scan_text"],
    "fallback_contract": "coverage_disclosure",
    "grade": {
        "deterministic": {
            "truth_set": "truth/v2c001.json",
            "must_disclose_coverage": True,
            "must_cite": True,
        },
        "judged": ["faithfulness"],
    },
}

# A tool-ready single-entity case bound to a fixture pool (bank §1.1 note).
BASE_SEL_CASE = {
    "id": "v2c002",
    "class": "single_entity_evidence",
    "tier": "core",
    "provenance": "bank#20 (CAP)",
    "question": "What risks did the research identify for {startup}?",
    "bindings": {"startup": "evaluated_startup"},
    "readiness": {
        "source_available": True,
        "tool_ready": True,
        "scope": "entity",
        "operation": "lookup",
        "context_required": "none",
        "golden_priority": "p0",
    },
    "expected_route": ["search_corpus", "get_source"],
    "grade": {"deterministic": {"must_cite": True}, "judged": ["faithfulness"]},
}

POOLS = {"pools": {
    "evaluated_startup": [
        {"name": "VeraAI", "entity_type": "company"},
        {"name": "BMI OrganBank", "entity_type": "company"},
        {"name": "ZeroPact", "entity_type": "company"},
        {"name": "Ask Norby", "entity_type": "company"},
    ],
    "empty_pool": [],
}}


def build(tmp_path, cases=None, scripts=None, version="v2.0", pools=POOLS):
    (tmp_path / "fixtures.yaml").write_text(yaml.safe_dump(pools))
    path = tmp_path / "suite.yaml"
    path.write_text(yaml.safe_dump({"version": version,
                                    "cases": cases if cases is not None else [BASE_CASE],
                                    "scripts": scripts or []}))
    return compile_suite(path)


def case(**overrides):
    """BASE_CASE with top-level overrides; nested blocks are merged one level so
    a test can change one readiness field without restating the other five."""
    out = copy.deepcopy(BASE_CASE)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            merged = copy.deepcopy(out[key])
            for k, v in value.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k] = {**merged[k], **v}
                else:
                    merged[k] = v
            out[key] = merged
        else:
            out[key] = value
    return out


def errors_from(tmp_path, **kwargs) -> str:
    with pytest.raises(CaseValidationError) as exc:
        build(tmp_path, **kwargs)
    return "\n".join(exc.value.errors)


# --- the shipped suite ----------------------------------------------------


def test_shipped_v2_suite_compiles():
    """The design's own worked example (§4) is the schema's executable
    reference — if it stops compiling, one of the two is wrong."""
    suite = load_suite("v2")
    assert suite.version == "v2.0"
    identity = suite.identity()
    assert identity["scoring_contract"].startswith("golden-v2.0/modes-")
    assert len(identity["suite_digest"]) == 64
    v2c017 = suite.by_id("v2c017")
    assert v2c017 is not None and v2c017.grading_mode == "honesty"
    assert v2c017.truth_set == "truth/v2c017.json"


# --- suite identity (review criterion 3) ----------------------------------


def test_digest_ignores_case_order_but_tracks_content(tmp_path):
    second = case(id="v2c002", question="Which evaluations mention FDA?",
                  grade={"deterministic": {"truth_set": "truth/v2c002.json"}})
    a = build(tmp_path, cases=[BASE_CASE, second])
    b = build(tmp_path, cases=[second, BASE_CASE])
    assert a.digest == b.digest, "reordering the YAML is not a suite change"

    edited = build(tmp_path, cases=[case(question="Which evaluations mention "
                                                  "regulatory risk at all?"),
                                    second])
    assert edited.digest != a.digest


# the same inventory case before and after its coverage query exists (§3, §4):
# same question, honesty bar today, capability bar tomorrow
INVENTORY_HONESTY = dict(
    BASE_SEL_CASE, **{"class": "inventory_coverage"},
    question="Do we have a pitch deck for {startup}?",
    readiness={**BASE_SEL_CASE["readiness"], "tool_ready": False,
               "operation": "inventory"},
    fallback_contract="honest_absence", blocked_on="coverage_query",
    grade={"deterministic": {"must_cite": True}})
INVENTORY_CAPABILITY = dict(
    INVENTORY_HONESTY,
    readiness={**INVENTORY_HONESTY["readiness"], "tool_ready": True},
    fallback_contract=None, blocked_on=None)


def test_contract_string_moves_only_when_a_case_flips_grading_mode(tmp_path):
    honesty = build(tmp_path, cases=[BASE_CASE, INVENTORY_HONESTY])
    # editing a question does not change what the bar MEANS
    reworded = build(tmp_path, cases=[case(question="Which evaluations mention "
                                                    "regulatory risk at all?"),
                                      INVENTORY_HONESTY])
    assert reworded.contract == honesty.contract
    assert reworded.digest != honesty.digest

    # the coverage query landing does: the comparator must refuse to gate the
    # honesty-era run against the capability-era one
    capability = build(tmp_path, cases=[BASE_CASE, INVENTORY_CAPABILITY])
    assert capability.contract != honesty.contract
    assert capability.by_id("v2c002").grading_mode == "capability"
    assert honesty.by_id("v2c002").grading_mode == "honesty"


# --- grade blocks ---------------------------------------------------------


def test_unknown_deterministic_check_is_rejected(tmp_path):
    """The silent-no-op bug this compiler exists to prevent: a misspelled check
    grades nothing and the case reports PASS."""
    out = errors_from(tmp_path, cases=[case(grade={"deterministic": {
        "must_disclose_coverge": True}})])
    assert "unknown deterministic check 'must_disclose_coverge'" in out


def test_deterministic_check_types_are_enforced(tmp_path):
    out = errors_from(tmp_path, cases=[case(grade={"deterministic": {
        "must_cite": "yes", "must_not_mention": []}})])
    assert "must_cite must be true/false" in out
    assert "must_not_mention must be a non-empty list" in out


def test_empty_grade_block_is_rejected(tmp_path):
    # dict(), not case(): this must REPLACE the grade block, not merge into it
    out = errors_from(tmp_path, cases=[dict(BASE_CASE,
                                            grade={"deterministic": {},
                                                   "judged": []})])
    assert "grade block is empty" in out


def test_unknown_judged_dimension_is_rejected(tmp_path):
    out = errors_from(tmp_path, cases=[case(grade={"judged": ["vibes"]})])
    assert "grade.judged entry='vibes'" in out


# --- readiness coherence --------------------------------------------------


def test_readiness_block_is_required_in_full(tmp_path):
    out = errors_from(tmp_path, cases=[dict(BASE_CASE,
                                            readiness={"source_available": True,
                                                       "tool_ready": False})])
    assert "readiness is missing" in out
    for missing in ("scope", "operation", "context_required", "golden_priority"):
        assert missing in out


def test_case_cannot_be_tool_ready_for_a_tool_that_does_not_exist(tmp_path):
    out = errors_from(tmp_path, cases=[case(readiness={"tool_ready": True},
                                            fallback_contract=None)])
    assert "expected_route names ['scan_text'], which does not exist yet" in out


def test_not_ready_case_must_name_what_it_waits_on(tmp_path):
    """"Not ready" with no stated blocker is the compound-tag shrug the bank
    review rejected. The inventory class is why the blocker cannot be inferred
    from the route: it goes through `structured_query`, which exists, while the
    coverage query inside it does not (bank §3.3)."""
    silent = errors_from(tmp_path, cases=[dict(
        BASE_SEL_CASE, readiness={**BASE_SEL_CASE["readiness"],
                                  "tool_ready": False},
        fallback_contract="coverage_disclosure",
        grade={"deterministic": {"must_cite": True,
                                 "must_disclose_coverage": True}})])
    assert "must name what it waits on" in silent

    stated = build(tmp_path, cases=[dict(
        BASE_SEL_CASE, **{"class": "inventory_coverage"},
        question="Do we have a pitch deck for {startup}?",
        readiness={**BASE_SEL_CASE["readiness"], "tool_ready": False,
                   "operation": "inventory"},
        fallback_contract="honest_absence", blocked_on="coverage_query",
        grade={"deterministic": {"must_cite": True}})])
    assert stated.by_id("v2c002").blocked_on == "coverage_query"


def test_future_route_supplies_the_blocker_without_restating_it(tmp_path):
    suite = build(tmp_path, cases=[BASE_CASE])
    assert suite.by_id("v2c001").blocked_on == "scan_text"


def test_fallback_contract_is_required_exactly_when_not_tool_ready(tmp_path):
    missing = errors_from(tmp_path, cases=[case(fallback_contract=None)])
    assert "requires a fallback_contract" in missing

    spurious = errors_from(tmp_path, cases=[dict(
        BASE_SEL_CASE, fallback_contract="coverage_disclosure")])
    assert "set on a tool_ready case" in spurious


def test_coverage_disclosure_contract_requires_its_check(tmp_path):
    out = errors_from(tmp_path, cases=[case(grade={"deterministic": {
        "must_disclose_coverage": False}})])
    assert "requires must_disclose_coverage: true" in out


def test_v2_0_defers_working_set_and_prior_answer_context(tmp_path):
    out = errors_from(tmp_path, cases=[case(
        readiness={"context_required": "working_set"})])
    assert "is a v2.1 case" in out


def test_web_routed_case_must_declare_itself_parked(tmp_path):
    out = errors_from(tmp_path, cases=[case(expected_route=["web_research"])])
    assert "web_required: true" in out


# --- class contracts (§5.1, §6, §7.3) -------------------------------------


def test_enumeration_requires_a_computed_truth_set(tmp_path):
    out = errors_from(tmp_path, cases=[case(grade={"deterministic": {
        "truth_set": None, "must_disclose_coverage": True, "must_cite": True}})])
    assert "requires a computed truth_set" in out


def test_enumeration_may_not_carry_a_hand_authored_answer_key(tmp_path):
    """§7.3 — the friendly answer key is exactly what v1's provenance failure
    was made of."""
    out = errors_from(tmp_path, cases=[case(grade={"deterministic": {
        "must_mention_all": ["VeraAI", "Calmr"]}})])
    assert "may not use must_mention_all" in out


def test_truth_set_is_keyed_by_case_id(tmp_path):
    out = errors_from(tmp_path, cases=[case(grade={"deterministic": {
        "truth_set": "truth/v2c999.json"}})])
    assert "must be 'truth/v2c001.json'" in out


@pytest.mark.parametrize("cls,check", [
    ("false_premise", "must_correct_premise"),
    ("ambiguity_surfacing", "must_surface_ambiguity"),
    ("conflict_surfacing", "must_surface_conflict"),
    ("decline_action", "must_decline_action"),
    ("clarification_seeking", "must_ask_clarifying"),
    ("evidence_fidelity", "must_quote_verbatim"),
])
def test_behavior_classes_must_grade_the_behavior_they_test(tmp_path, cls, check):
    out = errors_from(tmp_path, cases=[dict(
        BASE_SEL_CASE, **{"class": cls, "question": "Tell me about Matt Young."},
        bindings={})])
    assert f"requires {check}: true" in out


def test_injection_canary_must_name_the_payload(tmp_path):
    out = errors_from(tmp_path, cases=[dict(
        BASE_SEL_CASE, **{"class": "injection_canary"}, bindings={},
        question="Summarize the planted document.")])
    assert "requires must_not_mention" in out


# --- entity slots (§7.1) --------------------------------------------------


def test_slot_and_binding_must_agree(tmp_path):
    undeclared = errors_from(tmp_path, cases=[dict(BASE_SEL_CASE, bindings={})])
    assert "uses {startup} with no matching entry" in undeclared

    unused = errors_from(tmp_path, cases=[dict(
        BASE_SEL_CASE, question="What risks did the research identify?")])
    assert "declared but never appears in the question" in unused


def test_binding_to_an_unregistered_pool_fails_the_compile(tmp_path):
    out = errors_from(tmp_path, cases=[dict(
        BASE_SEL_CASE, bindings={"startup": "startups_with_a_moat"})])
    assert "which is not in the registry" in out


def test_selected_entity_mode_keeps_the_question_verbatim(tmp_path):
    """bank §1.1: bind SEL cases to a fixture; do not silently broaden them —
    and do not rewrite the user's wording either, since the phrasing is the
    test."""
    verbatim = build(tmp_path, cases=[dict(
        BASE_SEL_CASE, binding_mode="selected_entity",
        question="What are the biggest risks mentioned for this startup?",
        readiness={**BASE_SEL_CASE["readiness"], "context_required": "selected"})])
    assert verbatim.by_id("v2c002").binding_mode == "selected_entity"

    with_slot = errors_from(tmp_path, cases=[dict(
        BASE_SEL_CASE, binding_mode="selected_entity",
        readiness={**BASE_SEL_CASE["readiness"], "context_required": "selected"})])
    assert "keeps the question verbatim" in with_slot

    no_context = errors_from(tmp_path, cases=[dict(
        BASE_SEL_CASE, binding_mode="selected_entity",
        question="What are the biggest risks mentioned for this startup?")])
    assert "requires readiness.context_required: selected" in no_context


def test_bindings_resolve_deterministically_and_rotate_across_cases(tmp_path):
    suite = build(tmp_path, cases=[BASE_SEL_CASE])
    unit = suite.by_id("v2c002")
    first = resolve_bindings(unit, seed=1234, fixtures=suite.fixtures)
    again = resolve_bindings(unit, seed=1234, fixtures=suite.fixtures)
    assert first == again, "a paired run must pin identical bindings (§4)"
    assert first["startup"] in POOLS["pools"]["evaluated_startup"]

    # a different seed is free to pick differently; across seeds the pool is
    # actually exercised rather than one company being the de-facto answer
    picks = {resolve_bindings(unit, seed=s, fixtures=suite.fixtures)["startup"]["name"]
             for s in range(40)}
    assert len(picks) > 1


def test_empty_pool_fails_loudly_rather_than_skipping_the_case(tmp_path):
    suite = build(tmp_path, cases=[dict(BASE_SEL_CASE,
                                        bindings={"startup": "empty_pool"})])
    with pytest.raises(CaseValidationError) as exc:
        resolve_bindings(suite.by_id("v2c002"), seed=1, fixtures=suite.fixtures)
    assert "empty" in str(exc.value)


def test_render_question_substitutes_only_bound_slots():
    bound = {"startup": {"name": "VeraAI"}}
    assert render_question("Risks for {startup}?", bound) == "Risks for VeraAI?"
    # an unbound slot stays visible instead of rendering as an empty string
    assert render_question("Risks for {other}?", bound) == "Risks for {other}?"


# --- suite-level rules ----------------------------------------------------


def test_duplicate_ids_and_duplicate_questions_are_rejected(tmp_path):
    dup_id = errors_from(tmp_path, cases=[BASE_CASE, case(
        question="Which evaluations mention FDA?")])
    assert "duplicate id" in dup_id

    dup_text = errors_from(tmp_path, cases=[BASE_CASE, case(
        id="v2c002", grade={"deterministic": {"truth_set": "truth/v2c002.json"}})])
    assert "curate, don't copy" in dup_text


def test_every_problem_is_reported_not_just_the_first(tmp_path):
    with pytest.raises(CaseValidationError) as exc:
        build(tmp_path, cases=[case(id="nope", tier="gold",
                                    provenance="", grade={"judged": ["vibes"]})])
    assert len(exc.value.errors) >= 4


# --- scripts (§8) ---------------------------------------------------------


SCRIPT = {
    "id": "v2s001",
    "class": "coverage_challenge",
    "tier": "core",
    "provenance": "SCR script A + LFT thread #94",
    "bindings": {},
    "readiness": {
        "source_available": True,
        "tool_ready": True,
        "scope": "corpus",
        "operation": "lookup",
        "context_required": "prior_answer",
        "golden_priority": "p0",
    },
    "turns": [
        {"question": "Show me all the startups you have evaluations for.",
         "grade": {"deterministic": {"must_cite": True}}},
        {"question": "Which of those mention regulatory risk?",
         "grade": {"deterministic": {"must_disclose_coverage": True}}},
        {"question": "Did you search all 20?",
         "grade": {"deterministic": {"must_disclose_coverage": True}}},
    ],
    "cross_turn": [
        {"type": "set_carryover", "from_turn": 1, "to_turn": 2},
        {"type": "coverage_claim_grounded", "turn": 3},
    ],
}


def test_script_compiles_with_prior_answer_context(tmp_path):
    """prior_answer context is inadmissible as a single case in v2.0 but is the
    whole point of a script — a prior turn actually establishes the set (§8)."""
    suite = build(tmp_path, cases=[], scripts=[SCRIPT])
    script = suite.by_id("v2s001")
    assert [t.n for t in script.turns] == [1, 2, 3]
    assert suite.units and len(suite.units) == 1, "a script is one gate unit"


def test_script_needs_a_sequence_and_a_cross_turn_assertion(tmp_path):
    one_turn = errors_from(tmp_path, cases=[], scripts=[
        dict(SCRIPT, turns=SCRIPT["turns"][:1])])
    assert "at least 2 turns" in one_turn

    no_assertion = errors_from(tmp_path, cases=[], scripts=[
        dict(SCRIPT, cross_turn=[])])
    assert "at least one cross_turn assertion" in no_assertion


def test_cross_turn_assertions_are_validated(tmp_path):
    unknown = errors_from(tmp_path, cases=[], scripts=[dict(
        SCRIPT, cross_turn=[{"type": "vibe_check", "turn": 1}])])
    assert "type='vibe_check'" in unknown

    out_of_range = errors_from(tmp_path, cases=[], scripts=[dict(
        SCRIPT, cross_turn=[{"type": "coverage_claim_grounded", "turn": 9}])])
    assert "is not a turn number in 1..3" in out_of_range

    backwards = errors_from(tmp_path, cases=[], scripts=[dict(
        SCRIPT, cross_turn=[{"type": "set_carryover", "from_turn": 3,
                             "to_turn": 1}])])
    assert "from_turn must come before to_turn" in backwards

    missing_param = errors_from(tmp_path, cases=[], scripts=[dict(
        SCRIPT, cross_turn=[{"type": "set_carryover", "from_turn": 1}])])
    assert "requires ['to_turn']" in missing_param
