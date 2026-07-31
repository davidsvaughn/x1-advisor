"""Typed filter layer for corpus search (Step 0 — F1 injection, F7 silent misses).

Metadata filters used to be compiled by interpolating the filter **key** straight
into SQL (``c.metadata->>'{key}'``) with only the value parameterized. Filter
dicts come from the LLM, and the LLM reads corpus and web text, so the chain
*malicious document text → prompt-injected filter key → arbitrary SQL inside the
ACL-bearing query* was open. A successful injection there is an ACL bypass, not
just a crash (DESIGN-REVIEW-2026-07-30 F1; ARCHITECTURE-PLAN-REVIEW-2026-07-30
P0, which asks for a typed filter DSL rather than a bare key whitelist).

This module is both the fix and the seam:

* a **registry** of filterable fields — nothing outside it is filterable;
* **allowlisted operators** per field (today ``eq`` and ``in``);
* **type validation** per field kind;
* **canonical-value resolution** shared by every field of a kind — a registry
  resolver, not a per-field special case (project rule: no one-off resolvers);
* a **compiler** that emits fixed SQL fragments built from registry constants.
  No model-supplied text ever reaches SQL except as a bound parameter.

F7: a value that resolves to nothing is not silently an empty result. Enum
fields raise with the valid list; text fields keep the filter (so the result is
an honest zero rather than a quietly broadened search) and attach a NOTE naming
the nearest known values, so the model can correct itself instead of concluding
the corpus is empty. This generalizes the ``entity_type`` "startup" vs
"startup_company" failure (DECISIONS 2026-07-08, instrumentation catch #5).

The declared enum values are the **contract**: they are static so the tool
schema — part of the cached prompt prefix (§9) — never shifts with corpus
content. `unknown_corpus_values()` is the drift check that fails loudly when
ingest starts stamping a value the registry does not declare.
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

VALUE_CACHE_TTL_S = 300.0     # canonical-value cache; corpus edits show up within this
NEAREST_SUGGESTIONS = 5       # how many candidates a "did you mean" note names

_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")


class FilterError(ValueError):
    """Rejected filter — surfaced to the model as a tool error it can act on."""


@dataclass(frozen=True)
class FilterField:
    key: str
    kind: str                                   # "enum" | "text"
    description: str
    values: tuple[str, ...] = ()                # enum only: the declared contract
    aliases: Mapping[str, str] = field(default_factory=dict)
    operators: frozenset[str] = frozenset({"eq", "in"})

    def __post_init__(self) -> None:
        # the key is interpolated into SQL as a JSON path, so it must be a plain
        # identifier from THIS file and nothing else
        if not _IDENT.match(self.key):
            raise ValueError(f"filter key {self.key!r} is not a plain identifier")
        if self.kind not in ("enum", "text"):
            raise ValueError(f"unknown filter kind {self.kind!r}")
        if (self.kind == "enum") != bool(self.values):
            raise ValueError(f"{self.key}: enum fields declare values, text fields do not")


FIELDS: dict[str, FilterField] = {
    f.key: f for f in (
        FilterField(
            key="source_type", kind="enum",
            description="document class",
            values=("profile", "eval_section", "eval_premium", "eval_basic",
                    "deck_extract", "website"),
        ),
        FilterField(
            key="entity_type", kind="enum",
            description="kind of entity the document describes",
            values=("startup_company", "investor", "cv", "investment_company",
                    "investment_fund", "organization"),
            aliases={"startup": "startup_company", "company": "startup_company",
                     "person": "cv", "fund": "investment_fund",
                     "org": "organization"},
        ),
        FilterField(
            key="section_key", kind="enum",
            description="evaluation section",
            values=("problem", "market", "market_conditions", "traction",
                    "technology", "team", "founder"),
        ),
        FilterField(
            key="company_name", kind="text",
            description="company the document is about, as stored in the corpus "
                        "(near-misses are resolved to the stored form)",
        ),
    )
}


def filters_json_schema() -> dict[str, Any]:
    """JSON schema for the model-facing `filters` argument.

    Built from registry constants only — byte-stable across corpus changes, so
    it can live in the cached prompt prefix.
    """
    props: dict[str, Any] = {}
    for name, f in FIELDS.items():
        scalar: dict[str, Any] = {"type": "string"}
        if f.kind == "enum":
            scalar["enum"] = list(f.values)
        props[name] = {"description": f.description,
                       "anyOf": [scalar, {"type": "array", "items": scalar}]}
    return {"type": "object", "additionalProperties": False, "properties": props}


# --------------------------------------------------------------------------
# canonical values

_value_cache: dict[str, tuple[float, tuple[str, ...]]] = {}


def clear_value_cache() -> None:
    _value_cache.clear()


def known_values(conn, f: FilterField) -> tuple[str, ...]:
    """Distinct stored values for a field, over the **default-open** corpus only.

    Scoped to open material deliberately: these values are echoed back in
    "did you mean" notes, and a suggestion list must never disclose a draft,
    private, hidden or purchase-gated document's existence. The trade-off is
    that an owner filtering on their own draft company gets no canonicalization
    — the filter still applies verbatim, so nothing breaks, it just doesn't
    get corrected.
    """
    hit = _value_cache.get(f.key)
    if hit and time.monotonic() - hit[0] < VALUE_CACHE_TTL_S:
        return hit[1]
    # local import breaks the retrieval → filters cycle; `{}` is the
    # least-privileged ACL (owns nothing, purchased nothing) = the open set
    from x1_advisor.retrieval import _acl_sql

    acl_sql, acl_params = _acl_sql({})
    rows = conn.execute(
        f"""SELECT DISTINCT c.metadata->>'{f.key}' AS v
            FROM advisor.doc_chunks c
            JOIN advisor.documents d ON d.id = c.document_id
            WHERE d.superseded_by IS NULL
              AND c.metadata->>'{f.key}' IS NOT NULL
              {acl_sql}""",
        acl_params,
    ).fetchall()
    values = tuple(sorted(r["v"] for r in rows))
    _value_cache[f.key] = (time.monotonic(), values)
    return values


def unknown_corpus_values(conn) -> dict[str, list[str]]:
    """Drift check: stored enum values the registry does not declare.

    Non-empty means ingest started stamping something the tool schema hides from
    the model. Used by the probe suite; keep it loud rather than tolerant.
    """
    out: dict[str, list[str]] = {}
    for f in FIELDS.values():
        if f.kind != "enum":
            continue
        extra = sorted(set(known_values(conn, f)) - set(f.values))
        if extra:
            out[f.key] = extra
    return out


# --------------------------------------------------------------------------
# compilation

@dataclass(frozen=True)
class CompiledFilters:
    """Trusted, already-validated filter — the only thing retrieval will apply."""
    sql: str = ""
    params: tuple = ()
    notes: tuple[str, ...] = ()
    applied: Mapping[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.sql)


def _as_list(f: FilterField, value: Any) -> tuple[list[str], str]:
    """→ (values, operator). Scalars are `eq`; lists are `in`."""
    if isinstance(value, (list, tuple, set)):
        if "in" not in f.operators:
            raise FilterError(f"filter {f.key!r} does not accept a list of values")
        items = list(value)
        if not items:
            raise FilterError(f"filter {f.key!r} was given an empty list")
        op = "in"
    else:
        if "eq" not in f.operators:
            raise FilterError(f"filter {f.key!r} requires a list of values")
        items, op = [value], "eq"
    out = []
    for v in items:
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise FilterError(f"filter {f.key!r}: expected text, got {type(v).__name__}")
        text = str(v).strip()
        if not text:
            raise FilterError(f"filter {f.key!r} was given an empty value")
        out.append(text)
    return out, op


def _resolve(conn, f: FilterField, values: list[str]) -> tuple[list[str], str | None]:
    if f.kind == "enum":
        resolved = []
        for v in values:
            canon = f.aliases.get(v.lower(), v)
            match = next((d for d in f.values if d.lower() == canon.lower()), None)
            if match is None:
                raise FilterError(
                    f"invalid {f.key} {v!r}; valid values: {list(f.values)}")
            resolved.append(match)
        return resolved, None

    known = known_values(conn, f)
    resolved, unresolved = [], []
    for v in values:
        exact = next((k for k in known if k.lower() == v.lower()), None)
        if exact is not None:
            resolved.append(exact)
            continue
        # one shared text resolver: unique containment either direction
        near = [k for k in known if v.lower() in k.lower() or k.lower() in v.lower()]
        if len(near) == 1:
            resolved.append(near[0])
        else:
            unresolved.append(v)
    if not unresolved:
        return resolved, None
    suggestions = sorted({
        s for v in unresolved
        for s in difflib.get_close_matches(v, known, n=NEAREST_SUGGESTIONS, cutoff=0.4)
    })[:NEAREST_SUGGESTIONS]
    note = (f"filter {f.key}={unresolved!r} matched no known value"
            f" ({len(known)} known)"
            + (f"; nearest: {suggestions}" if suggestions else "")
            + ". The filter was applied as given, so an empty result means the"
              " value is wrong, not that the corpus is empty.")
    # never drop the unresolved value: dropping it would broaden the search
    # behind the caller's back
    return resolved + unresolved, note


def compile_filters(conn, filters: Any) -> CompiledFilters:
    """Validate + compile model-supplied filters into fixed SQL fragments."""
    if not filters:
        return CompiledFilters()
    if isinstance(filters, CompiledFilters):
        return filters
    if not isinstance(filters, dict):
        raise FilterError(f"filters must be an object, got {type(filters).__name__}")

    sql, params, notes, applied = "", [], [], {}
    for raw_key, raw_value in filters.items():
        f = FIELDS.get(str(raw_key))
        if f is None:
            raise FilterError(
                f"unknown filter {raw_key!r}; filterable fields: {sorted(FIELDS)}")
        values, op = _as_list(f, raw_value)
        values, note = _resolve(conn, f, values)
        if note:
            notes.append(note)
        # the ONLY interpolation is f.key — a registry constant matched against
        # _IDENT at construction. Values are always bound parameters.
        if op == "eq":
            sql += f" AND c.metadata->>'{f.key}' = %s"
            params.append(values[0])
            applied[f.key] = values[0]
        else:
            sql += f" AND c.metadata->>'{f.key}' = ANY(%s)"
            params.append(values)
            applied[f.key] = values
    return CompiledFilters(sql=sql, params=tuple(params), notes=tuple(notes),
                           applied=applied)
