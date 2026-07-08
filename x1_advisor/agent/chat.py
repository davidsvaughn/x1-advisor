"""Interactive multi-turn chat with the research agent (dev REPL).

Run:  uv run python -m x1_advisor.agent.chat [--acl admin]
Each turn persists to advisor.threads/turns and prints cost + citations; history
follows the §9 discipline (last 5 exchanges verbatim, older condensed; prior-turn
tool results are never replayed).
"""

from __future__ import annotations

import argparse

from x1_advisor.agent.advisor import run_turn, save_turn
from x1_advisor.db import connect


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--acl", default="admin")
    args = ap.parse_args()

    history: list[dict] = []
    thread_id = None
    print("X1 Advisor — interactive (ctrl-d to exit)")
    with connect() as conn:
        while True:
            try:
                question = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not question:
                continue
            result = run_turn(conn, question, acl=args.acl, history=history)
            thread_id = save_turn(conn, result, thread_id=thread_id)
            print(f"\n{result['answer']}")
            for c in result["citations"]:
                loc = (f"doc {c['document_id']}#{c['block_index']}"
                       if c["type"] == "internal" else c["url"])
                print(f"  [{c['n']}] {c.get('title') or ''} — {loc}")
            print(f"  (${result['cost_usd']:.4f}, {result['latency_ms']}ms, "
                  f"{len(result['steps'])} steps, thread {thread_id})")
            history += [{"role": "user", "content": question},
                        {"role": "assistant", "content": result["answer"]}]


if __name__ == "__main__":
    main()
