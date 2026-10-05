"""Filesystem locations for the application package."""

from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent
APP_DIR = CORE_DIR.parent
PROJECT_ROOT = APP_DIR.parent
WEB_DIR = APP_DIR / "web"


def read_web_page(name: str) -> str:
    path = WEB_DIR / name
    if not path.is_file():
        raise FileNotFoundError(path)
    return path.read_text(encoding="utf-8")
