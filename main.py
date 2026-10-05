"""ASGI entrypoint for Docker, Vercel, Railway, and local uvicorn.

Run:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

from app.main import app

__all__ = ["app"]

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
