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
from pathlib import Path
from typing import TextIO

# one implementation of "which code ran" — shared with turn bundles
from x1_advisor.fingerprint import code_fingerprint, git_dirty, git_sha  # noqa: F401

RUNS_DIR = Path(__file__).parent / "runs"


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
