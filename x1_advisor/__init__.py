"""X1 Advisor — interactive research agent. See docs/ARCHITECTURE.md.

`.env` is loaded here, at package import, so every entry point gets it. It used
to load as a side effect of importing `x1_advisor.db`, which meant any module
needing an API key but not the database — the judge, for one — failed with a
bare KeyError depending on its import graph.
"""

from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
