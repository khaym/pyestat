"""Pytest bootstrap: load secrets from .env so tests can read them via os.environ.

Loading happens at collection time so that any test (or pytest skip marker) that
inspects the environment sees the values from .env. Variables already defined in
the real environment win — `.env` only fills gaps.

This applies to *development*. Library users are not forced into python-dotenv:
the library reads `ESTAT_APP_ID` directly from `os.environ` regardless of how it
got there.
"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


# Walk up from this file to find a .env at the project root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env", override=False)
