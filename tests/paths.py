"""Shared filesystem helpers for tests."""

from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent


def sample_file(name: str) -> Path:
    """Return a sample file from tests/ or the project root."""
    for base in (TESTS_DIR, PROJECT_ROOT):
        candidate = base / name
        if candidate.exists():
            return candidate
    return PROJECT_ROOT / name
