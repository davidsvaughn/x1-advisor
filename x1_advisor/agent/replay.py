"""Replay a stored turn (Gate 1C) — three modes, because "rerun it" is ambiguous.

A single rerun cannot tell you whether behavior changed because the model
changed, the data changed, the index changed, or the tools changed. Each mode
holds different things still:

* **frozen-tools** — replay the recorded evidence, not the tools. Reconstruct
  the captured messages through the last recorded tool result, forbid further
  tool use, and run only final synthesis + validation. Isolates *"the model
  mishandled good evidence"*, and doubles as a cheap judge-recalibration runner.
  If the model tries to call a tool anyway, that is a contract error and the
  replay fails rather than guessing which stored result to pair it with.
* **live-tools** — replay the recorded tool calls, not the model. Re-execute the
  recorded calls in recorded order against today's code and data and diff the
  evidence identities against the bundle. Isolates tool/data/index drift.
  Produces diffs, never a new answer.
* **full** — rerun everything: what a user would get today.

**Replay never trusts the stored ACL.** The bundle's `acl_resolved` is forensic —
it says what was true then. Feeding it back into live tools could resurrect
revoked access or replay an admin entitlement out of context, so live modes
resolve authorization for the *replaying* principal, and say which one they used
(QA-LOOP-DESIGN §4.4).

Run: uv run python -m x1_advisor.agent.replay <turn_id>
         [--mode frozen-tools|live-tools|full] [--as admin|<user_id>] [--times N]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from haystack.dataclasses import ChatMessage

from x1_advisor.agent.evidence import (EvidenceRegistry, canonical_params,
                                       rows_digest, validate_citations)
from x1_advisor.cost import Tracker, Usage
from x1_advisor.fingerprint import turn_fingerprint

NO_MORE_TOOLS = (
    "Write your final answer now from the evidence already gathered above. "
    "Do not call any tools — they are unavailable. Cite refs exactly as before.")


def _acl_for(spec: str) -> Any:
    if spec == "admin":
        return "admin"
    try:
        return {"user_id": int(spec)}
    except ValueError:
        raise SystemExit(f"--as must be 'admin' or a user id, got {spec!r}") from None


def fingerprint_delta(conn, bundle: dict) -> dict[str, tuple]:
    """What has moved since the turn ran. Every mode reports this."""
    from x1_advisor.agent.advisor import AGENT_MODEL, SYSTEM_PROMPT
    from x1_advisor.agent.evidence import EvidenceRegistry as _R
    from x1_advisor.agent.tools import build_tools
    from x1_advisor.index import active_config

    now = turn_fingerprint(
        conn, prompt=SYSTEM_PROMPT,
        tools=build_tools(conn, acl="admin", registry=_R(), tracker=None),
        agent_model=AGENT_MODEL, config_id=active_config(conn).id)
    then = bundle.get("fingerprint", {})

    def flat(d, prefix=""):
        out = {}
        for k, v in (d or {}).items():
            out.update(flat(v, f"{prefix}{k}.") if isinstance(v, dict)
                       else {f"{prefix}{k}": v})
        return out

    a, b = flat(then), flat(now)
    # the resolved model id is only knowable after a call, so "now" is always
    # None here — reporting it as a delta would be a false positive on every
    # replay. `full` mode surfaces the real one in its own result.
    a.pop("agent_model_resolved", None)
    b.pop("agent_model_resolved", None)
    return {k: (a.get(k), b.get(k)) for k in sorted(set(a) | set(b))
            if a.get(k) != b.get(k)}


def _messages_through_last_tool(bundle: dict) -> list[ChatMessage]:
    """Captured conversation up to and including the final tool result."""
    raw = bundle.get("messages") or []
    last_tool = max((i for i, m in enumerate(raw) if m.get("role") == "tool"),
                    default=-1)
    if last_tool < 0:
        raise SystemExit("this turn called no tools — frozen-tools has nothing "
                         "to freeze; use --mode full")
    return [ChatMessage.from_dict(m) for m in raw[: last_tool + 1]]


def replay_frozen_tools(conn, bundle: dict, tracker: Tracker) -> dict[str, Any]:
    # Same generator factory production uses — Responses transport and the
    # configured reasoning effort. Replaying through a different transport
    # would change synthesis while claiming to hold everything but synthesis
    # randomness still, which is the one promise this mode makes.
    from x1_advisor.agent.advisor import AGENT_MODEL, agent_generator

    # a bundle without the evidence registry cannot have its citations
    # re-validated: every ref the fresh synthesis emits would resolve to
    # nothing and be silently dropped, scoring zero citations on a turn that
    # may have cited perfectly. Refusing loudly beats replaying wrong
    # (Gate 1D-5 — pre-registry schema-v2 bundles were accepted and zeroed).
    if not bundle.get("evidence") and bundle.get("validation", {}).get("citations"):
        return {"mode": "frozen-tools", "contract_error":
                "bundle predates the evidence registry: recorded citations "
                "cannot be re-validated, every replayed ref would silently "
                "drop. Re-run the turn to produce a replayable bundle."}

    messages = _messages_through_last_tool(bundle)
    reply = agent_generator().run(
        [*messages, ChatMessage.from_user(NO_MORE_TOOLS)])["replies"][0]
    if reply.tool_calls:
        return {"mode": "frozen-tools", "contract_error":
                "model emitted a tool call with tools unavailable; refusing to "
                "pair it with a stored result",
                "attempted": [tc.tool_name for tc in reply.tool_calls]}
    tracker.log(provider="openai", model=AGENT_MODEL, stage="replay.synthesis",
                usage=Usage.from_haystack_meta("openai", reply.meta))
    registry = EvidenceRegistry.from_list(bundle.get("evidence") or [])
    validated = validate_citations(reply.text or "", registry)
    before = bundle.get("validation", {})
    return {"mode": "frozen-tools", "answer": validated["answer"],
            "citations_then": _cite_keys(before.get("citations", [])),
            "citations_now": _cite_keys(validated["citations"]),
            "emitted_then": before.get("emitted"), "emitted_now": validated["emitted"],
            "dropped_then": before.get("dropped"), "dropped_now": validated["dropped"]}


def _cite_keys(citations: list[dict]) -> list[str]:
    """Citations as stable identities — the numbering changes, the source doesn't."""
    out = []
    for c in citations:
        if c.get("type") == "internal":
            out.append(f"doc{c['document_id']}#{c['block_index']}")
        elif c.get("type") == "platform_data":
            out.append(f"query:{c.get('query')}")
        else:
            out.append(str(c.get("url")))
    return sorted(out)


def replay_live_tools(conn, bundle: dict, acl: Any, tracker: Tracker) -> dict[str, Any]:
    """Re-execute the recorded tool calls THROUGH THE REAL TOOLS under the
    replaying principal.

    The first two versions were shadows (1D-5 review, then 1E-6): they called
    retrieval internals directly — their own `k`, their own get_source SQL,
    no ACL/gated-note/filter-compilation paths — so a tool-contract change
    was invisible to the thing meant to detect drift. Now `build_tools()`
    supplies the same closures the live agent runs:

    * a fresh evidence registry is built by the replayed searches themselves;
    * recorded refs are remapped recorded→identity→new ref before dispatch —
      a chain that cannot be rebuilt under this principal (the replayed
      searches never surfaced that chunk) is reported as a finding, not
      papered over with the stored entitlement's view;
    * structured digests come from the new registry's own registration,
      the identical code path the live turn used.

    Web calls are skipped: replaying them would diff the internet, not the
    code. Produces diffs, never a new answer.
    """
    import hashlib

    from x1_advisor.agent.tools import build_tools

    old_reg = EvidenceRegistry.from_list(bundle.get("evidence") or [])
    new_reg = EvidenceRegistry()
    explain: list[dict] = []
    tools = {t.name: t.function
             for t in build_tools(conn, acl=acl, registry=new_reg,
                                  tracker=tracker, explain_out=explain)}

    stored_explains = {e["call"]: e for e in bundle.get("retrieval_explain") or []}
    stored_queries = {
        (ev.get("query_name"), canonical_params(ev.get("query_params"))):
            (ev.get("result_digest"), ev.get("row_count"))
        for ev in bundle.get("evidence") or [] if ev.get("kind") == "query"}
    # what get_source actually showed the model, per recorded ref (from the
    # recorded tool results — works for pre-snapshot bundles too)
    stored_sources: dict[str, str] = {}
    for m in bundle.get("messages") or []:
        for content in m.get("content") or []:
            tcr = content.get("tool_call_result")
            if not tcr:
                continue
            origin = tcr.get("origin") or {}
            if origin.get("tool_name") != "get_source":
                continue
            try:
                payload = json.loads(tcr.get("result") or "")
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("text"):
                stored_sources[(origin.get("arguments") or {}).get("ref")] = \
                    payload["text"]

    def _sha(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:12]

    diffs, search_n = [], 0
    for m in bundle.get("messages") or []:
        for content in m.get("content") or []:
            call = content.get("tool_call")
            if not call:
                continue
            name, args = call.get("tool_name"), call.get("arguments") or {}
            if name == "web_research":
                diffs.append({"call": None, "tool": name,
                              "skipped": "live web is not reproducible; replaying "
                                         "it would diff the internet, not the code"})
                continue
            if name not in tools:
                diffs.append({"call": None, "tool": name,
                              "error_now": "tool no longer exists — contract drift"})
                continue
            if name == "search_corpus":
                search_n += 1
                payload = json.loads(tools[name](**args))
                if payload.get("error"):
                    diffs.append({"call": search_n, "tool": name,
                                  "query": args.get("query"),
                                  "error_now": payload["error"]})
                    continue
                now = explain[-1]["returned"] if explain else []
                then = (stored_explains.get(search_n) or {}).get("returned") or []
                entry = {
                    "call": search_n, "tool": name, "query": args.get("query"),
                    "returned_then": then, "returned_now": now,
                    "lost": [c for c in then if c not in now],
                    "gained": [c for c in now if c not in then],
                    "identical": then == now}
                # boundary behavior is part of the contract — surface it
                for key in ("access_note", "filter_notes"):
                    if payload.get(key):
                        entry[f"{key}_now"] = payload[key]
                diffs.append(entry)
            elif name == "get_source":
                old_ref = args.get("ref")
                old_ev = old_reg.get(old_ref) if old_ref else None
                if old_ev is None or old_ev.kind != "chunk":
                    diffs.append({"call": None, "tool": name, "ref": old_ref,
                                  "error_now": "recorded ref not resolvable from "
                                  "the bundle's evidence registry (pre-registry "
                                  "bundle?) — cannot compare"})
                    continue
                locator = f"doc{old_ev.document_id}#{old_ev.block_index}"
                new_ref = new_reg.ref_for_chunk(old_ev.document_id,
                                                old_ev.block_index)
                if new_ref is None:
                    diffs.append({"call": None, "tool": name, "ref": old_ref,
                                  "locator": locator,
                                  "error_now": "ref chain broken under this "
                                  "principal — no replayed search surfaced the "
                                  "chunk, so get_source cannot be issued"})
                    continue
                payload = json.loads(tools[name](ref=new_ref))
                if payload.get("error"):
                    diffs.append({"call": None, "tool": name, "ref": old_ref,
                                  "locator": locator,
                                  "error_now": payload["error"]})
                    continue
                then_text = stored_sources.get(old_ref)
                now_text = payload.get("text") or ""
                diffs.append({
                    "call": None, "tool": name, "ref": old_ref,
                    "ref_now": new_ref, "locator": locator,
                    "sha_then": _sha(then_text) if then_text else None,
                    "sha_now": _sha(now_text),
                    "chars_then": len(then_text) if then_text else None,
                    "chars_now": len(now_text),
                    "identical": (then_text == now_text) if then_text else None})
            elif name == "structured_query":
                key = (args.get("name"), canonical_params(args.get("params")))
                digest_then, rows_then = stored_queries.get(key, (None, None))
                payload = json.loads(tools[name](**args))
                if payload.get("error"):
                    diffs.append({"call": None, "tool": name,
                                  "query": args.get("name"),
                                  "digest_then": digest_then,
                                  "error_now": payload["error"]})
                    continue
                # the digest the tool itself registered — same code path as live
                ev_now = new_reg.query_evidence(args.get("name"), args.get("params"))
                diffs.append({"call": None, "tool": name,
                              "query": args.get("name"),
                              "row_count_then": rows_then,
                              "row_count_now": payload.get("row_count"),
                              "digest_then": digest_then,
                              "digest_now": ev_now.result_digest if ev_now else None,
                              "identical": (digest_then == ev_now.result_digest
                                            if digest_then and ev_now else None)})
    return {"mode": "live-tools", "acl_used": acl, "calls": diffs}


def replay_full(conn, bundle: dict, acl: Any) -> dict[str, Any]:
    from x1_advisor.agent.advisor import run_turn

    question = bundle.get("request", {}).get("question", "")
    result = run_turn(conn, question, acl=acl,
                      history=bundle.get("request", {}).get("history") or None)
    before = bundle.get("validation", {})
    return {"mode": "full", "acl_used": acl, "answer": result["answer"],
            "citations_then": _cite_keys(before.get("citations", [])),
            "citations_now": _cite_keys(result["citations"]),
            "cost_then": bundle.get("summary", {}).get("cost_usd"),
            "cost_now": result["cost_usd"],
            "steps_then": bundle.get("summary", {}).get("steps"),
            "steps_now": len(result["steps"])}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("turn_id", type=int)
    ap.add_argument("--mode", default="frozen-tools",
                    choices=("frozen-tools", "live-tools", "full"))
    ap.add_argument("--as", dest="principal", default="admin",
                    help="ACL to resolve for the REPLAYING principal; the "
                         "stored one is never reused")
    ap.add_argument("--times", type=int, default=1,
                    help="repeat (for known-stochastic cases only, not by default)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from x1_advisor.db import connect

    acl = _acl_for(args.principal)
    tracker = Tracker(run_id=f"replay:{args.turn_id}")
    with connect() as conn:
        row = conn.execute("SELECT research_record FROM advisor.turns WHERE id = %s",
                           (args.turn_id,)).fetchone()
        if not row or not row["research_record"]:
            sys.exit(f"turn {args.turn_id} has no research_record")
        bundle = row["research_record"]
        delta = fingerprint_delta(conn, bundle)
        runs = []
        for _ in range(max(1, args.times)):
            if args.mode == "frozen-tools":
                runs.append(replay_frozen_tools(conn, bundle, tracker))
            elif args.mode == "live-tools":
                runs.append(replay_live_tools(conn, bundle, acl, tracker))
            else:
                runs.append(replay_full(conn, bundle, acl))

    # `full` re-runs the agent, which bills through run_turn's own tracker —
    # add it in rather than printing $0.0000 for a replay that cost money
    total = tracker.run_total + sum(r.get("cost_now") or 0 for r in runs)
    out = {"turn_id": args.turn_id, "mode": args.mode,
           "fingerprint_delta": {k: list(v) for k, v in delta.items()},
           "runs": runs, "cost_usd": round(total, 6)}
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return

    print(f"turn {args.turn_id}  mode={args.mode}")
    print(f"question: {bundle.get('request', {}).get('question', '')[:90]}")
    print("\n== fingerprint delta (then -> now) ==")
    if not delta:
        print("  nothing moved")
    for k, (a, b) in delta.items():
        print(f"  {k:<32} {str(a)[:26]:<28} -> {str(b)[:26]}")
    for r in runs:
        print(f"\n== {r['mode']} ==")
        if r.get("contract_error"):
            attempted = f" ({r['attempted']})" if r.get("attempted") else ""
            print(f"  CONTRACT ERROR: {r['contract_error']}{attempted}")
            continue
        if r["mode"] == "live-tools":
            print(f"  acl used: {r['acl_used']} (stored ACL deliberately ignored)")
            for c in r["calls"]:
                state = ("IDENTICAL" if c.get("identical")
                         else "DRIFTED" if c.get("identical") is False
                         else "NO STORED BASELINE")
                if c.get("skipped"):
                    print(f"  {c['tool']:<17} SKIPPED — {c['skipped']}")
                elif c.get("error_now"):
                    print(f"  {c['tool']:<17} ERROR NOW — {c['error_now']}"
                          + (f" (stored digest {c['digest_then']})"
                             if c.get("digest_then") else ""))
                elif c.get("tool") == "structured_query":
                    print(f"  {c['tool']:<17} {c['query']}: {state}  "
                          f"rows {c['row_count_then']} -> {c['row_count_now']}  "
                          f"digest {c['digest_then']} -> {c['digest_now']}")
                elif c.get("tool") == "get_source":
                    print(f"  {c['tool']:<17} {c['ref']} ({c['locator']}): {state}  "
                          f"sha {c['sha_then']} -> {c['sha_now']}  "
                          f"chars {c['chars_then']} -> {c['chars_now']}")
                else:
                    print(f"  search #{c['call']:<9} "
                          f"{'IDENTICAL' if c['identical'] else 'DRIFTED'}  "
                          f"lost={c['lost']} gained={c['gained']}")
        else:
            same = r["citations_then"] == r["citations_now"]
            print(f"  citations {'identical' if same else 'CHANGED'}")
            print(f"    then: {r['citations_then']}")
            print(f"    now : {r['citations_now']}")
            if r["mode"] == "full":
                print(f"  cost  {r['cost_then']} -> {r['cost_now']}")
                print(f"  steps {r['steps_then']} -> {r['steps_now']}")
    print(f"\nreplay cost ${total:.4f}")


if __name__ == "__main__":
    main()
