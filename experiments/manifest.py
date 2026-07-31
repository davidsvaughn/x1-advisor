"""Run-manifest naming and creation (Step 0.4 — no-clobber).

Manifests were named `{date}_{config}_{golden}.jsonl` and opened `"w"`, so a
second run on the same day silently overwrote the first: the 0.778 retrieval
baseline was destroyed in place and only survived because it had been committed
(QA-LOOP-DESIGN §4.5). A comparator is worthless if the thing it compares
against can vanish.

Two guarantees here:

* **Identity** — the filename carries the git short SHA and a sequence number,
  so `date_config_golden` is no longer the identity of a run. A dirty worktree
  is stamped `+dirty`: the SHA does not describe the code that ran, and a
  manifest must not claim otherwise. (Corpus/index/prompt fingerprints join
  this in Gate 1A; the SHA alone does not identify behavior — recall moved with
  zero code change when record summaries landed.)
* **Immutability** — files are created with `O_EXCL`. Not "check then write":
  the exclusive create *is* the check, so nothing can overwrite an existing
  manifest, and the sequence number simply advances.
"""

from __future__ import annotations

import itertools
import subprocess
from pathlib import Path
from typing import TextIO

RUNS_DIR = Path(__file__).parent / "runs"
_GIT_CWD = Path(__file__).parent


def _git(*args: str) -> str | None:
    try:
        p = subprocess.run(["git", *args], capture_output=True, text=True,
                           timeout=5, cwd=_GIT_CWD)
    except Exception:  # noqa: BLE001 — git absent or wedged; caller degrades
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def git_sha() -> str:
    return _git("rev-parse", "--short", "HEAD") or "unknown"


def git_dirty() -> bool:
    """True when tracked files differ from HEAD (untracked files don't count)."""
    out = _git("status", "--porcelain", "--untracked-files=no")
    return bool(out) if out is not None else True   # unknown → assume dirty


def code_fingerprint() -> str:
    return git_sha() + ("+dirty" if git_dirty() else "")


def open_new_manifest(stem: str, *, runs_dir: Path = RUNS_DIR,
                      suffix: str = ".jsonl") -> tuple[str, Path, TextIO]:
    """→ (run_id, path, open file). Never overwrites an existing manifest."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    fp = code_fingerprint()
    for n in itertools.count(1):
        run_id = f"{stem}_{fp}_r{n}"
        path = runs_dir / f"{run_id}{suffix}"
        try:
            return run_id, path, path.open("x")
        except FileExistsError:
            continue
    raise AssertionError("unreachable")
