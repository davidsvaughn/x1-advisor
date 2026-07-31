"""Turn bundles — `research_record` v2 (Gate 1A, QA-LOOP-DESIGN §4.1).

One complete, replayable record per turn: what was asked, under which ACL,
against which code/prompt/corpus, every message the model saw *verbatim*, how
retrieval reached its results, what the model wrote before validation, and what
validation did to it. Without this a failing turn can only be re-run, not
diagnosed — and a re-run is a different turn.

**No silent truncation (P3).** Bundles store everything the model saw and said.
Tool results are bounded by construction (tools.py), which is what makes that
affordable. Any size cap must be opt-in config with an unlimited default.

**Bundles are an access surface (P5).** A bundle contains evidence text the
requesting user was entitled to see, so reading one later is a *second* access
path — admin-only in v1, ACL re-evaluated at read time if ever exposed wider.
That is why the local export is owner-only and gitignored, and why the stored
`acl_resolved` is forensic: replay must re-resolve authorization rather than
feed a stored entitlement back into live tools (§4.4).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Sequence

from x1_advisor.fingerprint import REPO_ROOT, turn_fingerprint

SCHEMA_VERSION = 2

# Full bundle exports never enter git: they carry entitled evidence text and
# untrusted corpus/web content. Owner-only, outside the tree the repo tracks.
QA_ARTIFACTS_DIR = Path(os.environ.get(
    "ADVISOR_QA_ARTIFACTS_DIR", str(REPO_ROOT / ".qa-artifacts"))) / "runs"
QA_EXPORT_ENABLED = os.environ.get("ADVISOR_QA_EXPORT", "1") not in ("0", "false", "")
# Opt-in retention. Default: keep everything — nothing is deleted unless the
# operator asks for it, and pruning says out loud what it removed.
QA_RETENTION_DAYS = float(os.environ.get("ADVISOR_QA_RETENTION_DAYS", "0")) or None


def serialize_messages(messages: Sequence[Any]) -> list[dict]:
    """The exact message list the model saw, including every tool result string."""
    out = []
    for m in messages:
        try:
            out.append(m.to_dict())
        except Exception as exc:  # noqa: BLE001 — a bundle must never fail a turn
            out.append({"_serialization_error": f"{type(exc).__name__}: {exc}",
                        "_repr": repr(m)})
    return out


def build_bundle(conn, *, question: str, history: list[dict] | None,
                 thread_id: int | None, acl: Any, prompt: str,
                 tools: Sequence[Any], agent_model: str, config_id: str,
                 messages: Sequence[Any], retrieval_explain: list[dict],
                 raw_answer: str, validated: dict, steps: list[dict],
                 summary: dict) -> dict[str, Any]:
    principal = ({"user_id": None, "role": "admin"} if acl == "admin"
                 else {"user_id": acl.get("user_id"), "role": "user"})
    return {
        "schema_version": SCHEMA_VERSION,
        "request": {
            "question": question,
            "history": history or [],
            "thread_id": thread_id,
            "principal": principal,
            # FORENSIC snapshot — compare what-was against what-is. Never fed
            # back into live tools on replay (§4.4).
            "acl_resolved": acl if acl == "admin" else dict(acl),
        },
        "fingerprint": turn_fingerprint(conn, prompt=prompt, tools=tools,
                                        agent_model=agent_model,
                                        config_id=config_id),
        "summary": summary,
        "steps": steps,
        "messages": serialize_messages(messages),
        "retrieval_explain": retrieval_explain,
        "raw_answer": raw_answer,
        "validation": validated,
        # Gate 1B fills faithfulness; kept present-and-null so a bundle never
        # implies a judge ran when none did
        "scores": {"citation_resolvability": (
            validated["resolved"] / validated["emitted"]
            if validated.get("emitted") else None),
            "faithfulness": None},
    }


def _prune(directory: Path) -> None:
    if QA_RETENTION_DAYS is None:
        return
    cutoff = time.time() - QA_RETENTION_DAYS * 86400
    removed = [p.name for p in directory.glob("*.json") if p.stat().st_mtime < cutoff]
    for name in removed:
        (directory / name).unlink()
    if removed:
        print(f"[qa-artifacts] retention {QA_RETENTION_DAYS}d removed "
              f"{len(removed)} bundle(s): {', '.join(sorted(removed)[:5])}"
              + (" …" if len(removed) > 5 else ""))


def export_bundle(bundle: dict, *, turn_id: int, thread_id: int) -> Path | None:
    """Write the complete bundle to owner-only local storage. Never raises —
    an export problem must not fail the turn that produced it."""
    if not QA_EXPORT_ENABLED:
        return None
    try:
        QA_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(QA_ARTIFACTS_DIR, 0o700)
        os.chmod(QA_ARTIFACTS_DIR.parent, 0o700)
        path = QA_ARTIFACTS_DIR / f"turn_{turn_id:08d}_thread_{thread_id}.json"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(bundle, fh, default=str, indent=1)
        _prune(QA_ARTIFACTS_DIR)
        return path
    except Exception as exc:  # noqa: BLE001
        print(f"[qa-artifacts] export failed for turn {turn_id}: "
              f"{type(exc).__name__}: {exc}")
        return None
