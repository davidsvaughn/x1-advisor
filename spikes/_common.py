"""Shared helpers for Phase-0 spikes (PLAN.md §Phase 0).

Each spike is a standalone script: `uv run python -m spikes.spike_a_cache_usage`.
Exit code 0 = PASS, 1 = FAIL, 2 = BLOCKED (missing credential). Spikes print the
raw evidence (usage blocks, content types) so results are auditable in the run log.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        print(f"BLOCKED: {name} is not set (add it to .env). Spike not run.")
        sys.exit(2)
    return value


def result(passed: bool, label: str) -> None:
    print(f"\n{'PASS' if passed else 'FAIL'}: {label}")
    sys.exit(0 if passed else 1)
