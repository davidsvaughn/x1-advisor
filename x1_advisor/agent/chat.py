"""Interactive multi-turn chat with the research agent (dev REPL).

Run:  uv run python -m x1_advisor.agent.chat [--acl admin]

Every exchange persists TWICE, automatically, via save_turn:

* `advisor.threads` / `advisor.turns` — the transactional copy. The session
  prints its thread id up front and each turn's id as it lands, so a moment
  is addressable later as "thread N, turn M".
* a complete turn bundle — everything the model saw, called and said — in
  owner-only local storage: `.qa-artifacts/runs/turn_<id>_thread_<id>.json`.
  This is what the teacher loop reads for a post-mortem; the file exists the
  moment the turn finishes, so a live session can be inspected mid-flight.

Langfuse traces each turn as well (release = git sha).

`/flag [note]` marks the exchange that JUST happened as regression-suite
material: it appends a pointer (thread, turn, question, bundle path, note)
to `.qa-artifacts/repl/flagged.jsonl`. Flagged exchanges are candidates for
QUESTION-BANK/golden-v2 curation — a human step, never an auto-added case.

History follows the §9 discipline (last 5 exchanges verbatim, older
condensed; prior-turn tool results are never replayed).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from x1_advisor.agent.advisor import run_turn, save_turn
from x1_advisor.agent.bundle import QA_ARTIFACTS_DIR
from x1_advisor.agent.flags import flag_exchange
from x1_advisor.db import connect


def _locator(c: dict) -> str:
    """One printable line per citation, whatever its kind — the first REPL
    session died on a KeyError because platform_data citations have no url."""
    title = c.get("title") or ""
    kind = c.get("type")
    if kind == "internal":
        loc = f"doc {c.get('document_id')}#{c.get('block_index')}"
        if c.get("page_number") is not None:
            loc += f" p{c['page_number']}"
    elif kind == "platform_data":
        loc = (f"platform query {c.get('query')}"
               f" ({c.get('row_count')} rows, as of {c.get('as_of')})")
    else:
        loc = c.get("url") or "(no locator)"
    return f"{title} — {loc}" if title else loc


def _flag(last: dict | None, thread_id: int | None, note: str) -> None:
    if not last:
        print("nothing to flag yet — ask something first")
        return
    path = flag_exchange(thread_id=thread_id, turn_id=last.get("turn_id"),
                         question=last.get("question"), note=note,
                         cost_usd=last.get("cost_usd"),
                         bundle=last.get("bundle_path"))
    print(f"flagged turn {last.get('turn_id')} -> {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--acl", default="admin")
    args = ap.parse_args()

    history: list[dict] = []
    thread_id: int | None = None
    last: dict | None = None
    print("X1 Advisor — interactive (ctrl-d to exit; /flag [note] queues the "
          "last exchange for regression curation)")
    with connect() as conn:
        while True:
            try:
                question = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not question:
                continue
            if question == "/flag" or question.startswith("/flag "):
                _flag(last, thread_id, note=question[len("/flag"):].strip())
                continue
            result = run_turn(conn, question, acl=args.acl, history=history)
            first_turn = thread_id is None
            thread_id = save_turn(conn, result, thread_id=thread_id)
            if first_turn:
                print(f"[thread {thread_id} — bundles land in "
                      f"{QA_ARTIFACTS_DIR}]")
            # run_turn only READS history — the caller owns the append. The
            # first REPL shipped without this line, so every "multi-turn"
            # session was actually stateless.
            history += [{"role": "user", "content": question},
                        {"role": "assistant", "content": result["answer"]}]
            last = result
            print(f"\n{result['answer']}")
            for c in result["citations"]:
                print(f"  [{c['n']}] {_locator(c)}")
            trail = (f"  -- turn {result.get('turn_id')}"
                     f" · ${result['cost_usd']:.4f}")
            if result.get("bundle_path"):
                trail += f" · {Path(result['bundle_path']).name}"
            print(trail)


if __name__ == "__main__":
    main()
