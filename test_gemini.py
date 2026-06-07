"""
Smoke-test Gemini API connectivity using GEMINI_API_KEY from .env.

Usage (from project root):
    python test_gemini.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from this file's directory (same pattern as main.py / env_service.py)
_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH)

DEFAULT_MODEL = "gemini-2.5-flash"


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}…{key[-4:]}"


def test_gemini() -> int:
    api_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    if not api_key:
        print("Error: GEMINI_API_KEY is not set.")
        print(f"Add it to {_ENV_PATH} or export it in your shell.")
        return 1

    try:
        from google import genai
    except ImportError:
        print("Error: google-genai is not installed.")
        print("Run: pip install google-genai")
        return 1

    print(f"Env file: {_ENV_PATH} ({'found' if _ENV_PATH.is_file() else 'missing'})")
    print(f"API key: {_mask_key(api_key)}")
    print(f"Model:   {model}")
    print("Initializing Gemini client…")

    client = genai.Client(api_key=api_key)

    prompt = (
        "You are a connectivity check. Reply with exactly one short sentence "
        "confirming you received this message."
    )

    try:
        print(f"Sending request to {model}…")
        response = client.models.generate_content(model=model, contents=prompt)
        text = (response.text or "").strip()

        print("\nSuccess — Gemini response:")
        print("-" * 40)
        print(text or "(empty response)")
        print("-" * 40)
        return 0

    except Exception as exc:
        print(f"\nRequest failed: {exc}")
        print("\nTips:")
        print("  • Verify GEMINI_API_KEY at https://aistudio.google.com/apikey")
        print(f"  • Try another model: set GEMINI_MODEL=gemini-2.0-flash in .env")
        return 1


if __name__ == "__main__":
    sys.exit(test_gemini())
