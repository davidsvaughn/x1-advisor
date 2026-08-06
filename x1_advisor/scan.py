"""Bounded exhaustive text scan — one engine, two consumers.

This module is the deterministic phrase/FTS scan that (a) the `scan_text`
agent tool exposes to the model (bank §3.2A) and (b) the truth-set builder
(`experiments/truth.py`) uses to compute enumeration oracles (design §5.1).
It moved here from experiments/truth.py when the tool shipped; sharing the
engine is the design intent — capability grading checks that the agent
*reports* this scan faithfully, so grader and tool must be the same code.

Refactor discipline (HANDOFF 2026-08-04 §3): for `acl="admin"` this must
behave byte-identically to the truth builder it replaced — same rows, same
entity keys, same statuses, same counts — or every committed truth digest
moves. `uv run python -m experiments.truth --check` is the proof.

ACL (bank §3.2A disclosure policy): a non-admin scan never reads gated text.
The visible scan applies the same `_acl_sql` predicates retrieval uses (one
ACL implementation, never two), and a second existence-only query surfaces
the PREMIUM-gated class per entity as status `restricted` — "purchase-gated
material exists here, it was not scanned" — because a `no_match` verdict
over partially scanned text would be a lie. Private and unpublished
documents are excluded from the scan AND from every count: eligible/scanned
totals must not reveal them indirectly, so an entity whose only in-scope
material is private reads as `not_indexed`. Hidden evaluations likewise.

The lexical honesty rule rides the tool contract, not this engine: a
`no_match` means "no phrase matched the indexed eligible text", never a
semantic negative (bank §3.2 honesty rule).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Deliberately the retriever's own predicate builder: the two evidence paths
# must never disagree about what a requester may see (tools.py ACL note).
from x1_advisor.retrieval import _acl_sql


@dataclass(frozen=True)
class ScanScope:
    """The bounded scope a scan runs over — the scan half of a TruthSpec.

    `mode` and `acl` stay on TruthSpec: they are oracle policy (must the match
    set be empty; whose truth set is this), not scan mechanics.
    """
    entity_type: str
    entity_key: str                     # 'name' | 'ref' (env:id)
    source_types: tuple[str, ...]
    granularity: tuple[str, ...]        # ('block',) everywhere that matters:
                                        # record summaries are generated text,
                                        # not evidence (Gate 1B)
    method: str                         # 'phrase' (word-start match) | 'fts'
    any_terms: tuple[str, ...]
    all_terms: tuple[str, ...]


def _match_columns(scope: ScanScope) -> tuple[str, list[str], list[str]]:
    """One boolean column per term, evaluated in the database.

    Per-term columns rather than one combined predicate: the phrase list is the
    authored half of the oracle, so an audit has to be able to ask "which term
    actually fired" without re-running anything.
    """
    terms = [*scope.any_terms, *scope.all_terms]
    cols, params = [], []
    for i, term in enumerate(terms):
        if scope.method == "phrase":
            # Word-start match, open on the right: "CE mark" matches
            # "CE marked" but never "performance marketing"; "FDA" never
            # fires inside a URL token. The right edge stays open so
            # inflections ("regulatory risks") keep matching. \m anchors to
            # a word character, so terms starting with punctuation stay
            # bare-substring.
            pat = re.escape(term)
            if term[:1].isalnum() or term[:1] == "_":
                pat = r"\m" + pat
            cols.append(f"(c.text ~* %s) AS t{i}")
            params.append(pat)
        else:                                   # fts
            cols.append(f"(to_tsvector('english', c.text) "
                        f"@@ plainto_tsquery('english', %s)) AS t{i}")
            params.append(term)
    return ", ".join(cols), params, terms


def _hit(row: dict, scope: ScanScope, terms: list[str]) -> tuple[bool, list[str]]:
    n_any = len(scope.any_terms)
    fired = [term for i, term in enumerate(terms) if row.get(f"t{i}")]
    any_ok = (not scope.any_terms) or any(row.get(f"t{i}") for i in range(n_any))
    all_ok = all(row.get(f"t{i}") for i in range(n_any, len(terms)))
    return (any_ok and all_ok), fired


def _entity_key(row: dict, scope: ScanScope) -> str:
    name = row.get("name") or f"<{scope.entity_type}:{row.get('entity_id')}>"
    if scope.entity_key == "name":
        return name
    env = row.get("env") or "test"
    return f"{env}:{row.get('entity_id') or name}"


# CV / investor / organization chunks carry no company_name; the entity's NAME
# is the title's first ' — ' segment ("Paul Jaminet — CV" → "Paul Jaminet"),
# the same convention entity_vocabulary uses. The full title is a key no
# answer can ever match (truth builder v2).
_ENTITY_COLS = """coalesce(c.metadata->>'company_name',
                            split_part(d.title, ' — ', 1)) AS name,
                   coalesce(c.metadata->>'entity_ref_env', 'test') AS env,
                   c.metadata->>'entity_id' AS entity_id"""

_SCOPE_WHERE = """WHERE d.superseded_by IS NULL
              AND d.source_type = ANY(%s)
              AND c.granularity = ANY(%s)
              AND c.metadata->>'entity_type' = %s"""


def scan(conn, scope: ScanScope, *, acl: Any = "admin",
         include_text: bool = False) -> dict[str, Any]:
    """Run the bounded scan. Pure function of (corpus, scope, acl) — no model.

    `include_text=False` (the truth-builder path) returns exactly what the
    builder always stored: chunk identities, never chunk text — truth files
    must not grow corpus text they never held. `include_text=True` (the tool
    path) adds text/title/page per matching chunk so the caller can render
    citable excerpts.
    """
    cols, params, terms = _match_columns(scope)
    acl_sql, acl_params = _acl_sql(acl)
    text_cols = ("c.text, c.page_number, d.title AS doc_title, "
                 if include_text else "")
    rows = conn.execute(
        f"""SELECT c.id AS chunk_id, c.document_id, c.block_index,
                   {text_cols}{_ENTITY_COLS},
                   {cols}
            FROM advisor.doc_chunks c
            JOIN advisor.documents d ON d.id = c.document_id
            {_SCOPE_WHERE}{acl_sql}""",
        (*params, list(scope.source_types), list(scope.granularity),
         scope.entity_type, *acl_params),
    ).fetchall()

    entities: dict[str, dict[str, Any]] = {}
    refs: set[tuple[str, str]] = set()
    names: set[str] = set()
    for raw in rows:
        row = dict(raw)
        key = _entity_key(row, scope)
        refs.add((row.get("env") or "test",
                  str(row.get("entity_id") or row.get("name"))))
        if row.get("name"):
            names.add(row["name"])
        entity = entities.setdefault(key, {"key": key, "status": "no_match",
                                           "scanned_chunks": 0, "chunks": [],
                                           "records": []})
        entity["scanned_chunks"] += 1
        record = {"env": row.get("env") or "test", "entity_id": row.get("entity_id"),
                  "name": row.get("name")}
        if record not in entity["records"]:
            entity["records"].append(record)
        hit, fired = _hit(row, scope, terms)
        if hit:
            entity["status"] = "matched"
            # every match, never a sample: a truncated oracle grades a smaller
            # question than the one the case asks
            chunk = {"chunk_id": row["chunk_id"],
                     "document_id": row["document_id"],
                     "block_index": row["block_index"],
                     "terms": fired}
            if include_text:
                chunk["text"] = row.get("text")
                chunk["page_number"] = row.get("page_number")
                chunk["title"] = row.get("doc_title")
            entity["chunks"].append(chunk)

    # entities the platform knows about that have nothing in scope: a coverage
    # claim has to account for them, and "we hold no documents for X" is a
    # different answer from "X does not mention it"
    if scope.entity_type == "startup_company":
        for raw in conn.execute(
                "SELECT id, name FROM startup_companies ORDER BY id").fetchall():
            row = {"name": raw["name"], "entity_id": str(raw["id"]), "env": "test"}
            key = _entity_key(row, scope)
            refs.add(("test", str(raw["id"])))
            names.add(raw["name"])
            if key not in entities:
                entities[key] = {"key": key, "status": "not_indexed",
                                 "scanned_chunks": 0, "chunks": [],
                                 "records": [row]}

    statuses = ("matched", "no_match", "not_indexed")
    if acl != "admin":
        statuses = (*statuses, "restricted")
        _apply_restricted(conn, scope, acl, entities, refs, names)

    ordered = [entities[k] for k in sorted(entities)]
    by_status = {s: sum(1 for e in ordered if e["status"] == s)
                 for s in statuses}
    return {
        "entities": ordered,
        "counts": {
            "eligible": len(ordered),
            "scanned": sum(1 for e in ordered if e["scanned_chunks"]),
            **by_status,
            "scanned_chunks": sum(e["scanned_chunks"] for e in ordered),
            "matching_chunks": sum(len(e["chunks"]) for e in ordered),
            # both denominators, always — 9 names exist in two envs, so these
            # differ and a grader must know which one the case meant
            "entity_names": len(names),
            "entity_records": len(refs),
        },
    }


def _apply_restricted(conn, scope: ScanScope, acl: Any,
                      entities: dict[str, dict[str, Any]],
                      refs: set[tuple[str, str]], names: set[str]) -> None:
    """Existence-only pass for the disclosable gated class (bank §3.2A).

    Premium-gated evaluation text this requester has not purchased is in
    scope but was not scanned. Its *existence* is disclosable (same policy as
    search_corpus's gated-vs-absent note), so the entity gets status
    `restricted` instead of a false `no_match`/`not_indexed`, and any entity
    that DID match on visible text is flagged `gated_unscanned` — "there is
    more here than the scan could read". Private / unpublished / hidden-eval
    material never reaches this query: undisclosable classes stay out of
    every status and every count.
    """
    purchased = (acl.get("purchased_evaluation_ids") or []) if isinstance(acl, dict) else []
    not_purchased = ""
    params: list[Any] = [list(scope.source_types), list(scope.granularity),
                         scope.entity_type]
    if purchased:
        not_purchased = (" AND NOT COALESCE((c.metadata->>'evaluation_id')::bigint"
                         " = ANY(%s), false)")
        params.append(purchased)
    gated = conn.execute(
        f"""SELECT DISTINCT {_ENTITY_COLS}
            FROM advisor.doc_chunks c
            JOIN advisor.documents d ON d.id = c.document_id
            {_SCOPE_WHERE}
              AND d.visibility <> 'private'
              AND d.is_published
              AND COALESCE((c.metadata->>'acl_eval_is_visible')::boolean, true)
              AND COALESCE((c.metadata->>'acl_premium_gated')::boolean, false)
              {not_purchased}""",
        params,
    ).fetchall()
    for raw in gated:
        row = dict(raw)
        key = _entity_key(row, scope)
        refs.add((row.get("env") or "test",
                  str(row.get("entity_id") or row.get("name"))))
        if row.get("name"):
            names.add(row["name"])
        entity = entities.setdefault(key, {"key": key, "status": "restricted",
                                           "scanned_chunks": 0, "chunks": [],
                                           "records": []})
        entity["gated_unscanned"] = True
        record = {"env": row.get("env") or "test",
                  "entity_id": row.get("entity_id"), "name": row.get("name")}
        if record not in entity["records"]:
            entity["records"].append(record)
        if entity["status"] in ("no_match", "not_indexed"):
            entity["status"] = "restricted"
