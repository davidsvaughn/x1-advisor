"""Ask the research agent one question from the CLI, with full cost visibility.

Run:  uv run python -m x1_advisor.agent.ask "your question" [--save] [--json]

Prints the answer, the numbered citations, and the PER-STEP USAGE TABLE (input /
cached / output tokens + cost per generation step) — the context-bloat instrument:
input tokens should grow roughly linearly with compact tool results; superlinear
growth or vanishing cached_tokens means a fat result or a broken prompt prefix.
"""

from __future__ import annotations

import argparse
import json
import sys

from x1_advisor.agent.advisor import run_turn, save_turn
from x1_advisor.db import connect


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("question")
    ap.add_argument("--save", action="store_true", help="persist to advisor.threads/turns")
    ap.add_argument("--json", action="store_true", help="dump the raw result object")
    ap.add_argument("--bundle", action="store_true",
                    help="include the full turn bundle in --json (large: every "
                         "message the model saw + retrieval explain)")
    args = ap.parse_args()

    with connect() as conn:
        result = run_turn(conn, args.question)
        if args.save:
            save_turn(conn, result)

    if args.json:
        payload = result if args.bundle else {k: v for k, v in result.items()
                                              if k != "bundle"}
        print(json.dumps(payload, indent=2, default=str))
        return

    print(f"\n{'=' * 72}\nQ: {result['question']}\n{'=' * 72}")
    print(result["answer"])
    if result["citations"]:
        print("\nSources:")
        for c in result["citations"]:
            loc = (f"doc {c['document_id']} block {c['block_index']}"
                   + (f" p.{c['page_number']}" if c.get("page_number") is not None else "")
                   ) if c["type"] == "internal" else c["url"]
            print(f"  [{c['n']}] {c.get('title', '')} — {loc}")

    print(f"\n-- per-step usage (context-bloat instrument) --")
    print(f"{'step':>4} {'input':>8} {'cached':>8} {'output':>8} {'cost$':>10}  tools")
    for s in result["steps"]:
        print(f"{s['step']:>4} {s['input_tokens']:>8} {s['cached_tokens']:>8} "
              f"{s['output_tokens']:>8} {s['cost_usd']:>10.6f}  {','.join(s['tool_calls']) or '-'}")
    cs = result["citation_stats"]
    print(f"\nturn: ${result['cost_usd']:.4f}"
          f"{' ⚠ OVER SOFT CAP' if result['over_soft_cap'] else ''}"
          f" | {result['latency_ms']}ms | citations {cs['resolved']}/{cs['emitted']}"
          f" distinct refs resolved"
          + (f" (DROPPED: {', '.join(cs['dropped'])})" if cs["dropped"] else "")
          + f" | evidence registered: {cs['evidence_registered']}")
    if result.get("bundle_path"):
        print(f"bundle: turn {result['turn_id']} → {result['bundle_path']}")
    sys.exit(0)


if __name__ == "__main__":
    main()
