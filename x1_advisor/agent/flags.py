"""Flag live exchanges (REPL or dev console) as regression-suite candidates.

A flag is a POINTER — thread, turn, question, bundle path, optional note —
appended to `.qa-artifacts/repl/flagged.jsonl` (owner-only, never in git:
questions and notes are corpus-adjacent content). The file is APPEND-ONLY:
editing a note re-appends, and readers take the LATEST record per turn_id
(`latest_flags()`); the full history stays as audit trail. Flags feed the
curation step: a human promotes worthwhile ones into QUESTION-BANK /
golden v2. Nothing is ever auto-added to the suite from here.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from x1_advisor.agent.bundle import QA_ARTIFACTS_DIR

FLAG_FILE = QA_ARTIFACTS_DIR.parent / "repl" / "flagged.jsonl"


def flag_exchange(*, thread_id: int | None, turn_id: int | None,
                  question: str | None, note: str | None = None,
                  cost_usd: float | None = None,
                  bundle: str | None = None) -> Path:
    """Append one flag record; returns the queue file path."""
    if bundle is None and turn_id and thread_id:
        candidate = QA_ARTIFACTS_DIR / f"turn_{turn_id:08d}_thread_{thread_id}.json"
        bundle = str(candidate) if candidate.exists() else None
    FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(FLAG_FILE.parent, 0o700)
    existed = FLAG_FILE.exists()
    with FLAG_FILE.open("a") as fh:
        fh.write(json.dumps({
            "flagged_at": dt.datetime.now(dt.timezone.utc)
                            .isoformat(timespec="seconds"),
            "thread_id": thread_id,
            "turn_id": turn_id,
            "question": question,
            "bundle": bundle,
            "cost_usd": cost_usd,
            "note": note or None,
        }) + "\n")
    if not existed:
        os.chmod(FLAG_FILE, 0o600)
    return FLAG_FILE


def latest_flags() -> dict[str, dict]:
    """Latest record per turn_id — the current note for every flagged turn."""
    latest: dict[str, dict] = {}
    if FLAG_FILE.exists():
        for line in FLAG_FILE.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                if rec.get("turn_id") is not None:
                    latest[str(rec["turn_id"])] = rec
    return latest
