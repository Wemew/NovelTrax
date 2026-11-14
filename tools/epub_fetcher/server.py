"""HTTP wrapper around :mod:`epub_fetcher` for NovelTrax.

POST /fetch  {"url": "...", "timeout": 150} -> the downloaded file
GET  /health -> dependency/config status
"""

from __future__ import annotations

import os
import tempfile
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from epub_fetcher import FetchError, fetch


def _remove_after_response(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass

app = FastAPI(title="NovelTrax EPUB fetcher", version="1.0.0")
OUTPUT_DIR = os.getenv("EPUB_FETCHER_OUTPUT_DIR", os.path.join(tempfile.gettempdir(), "epub_fetcher"))
DEFAULT_TIMEOUT = float(os.getenv("EPUB_FETCHER_TIMEOUT", "150"))
FLARESOLVERR_URL = os.getenv("EPUB_FETCHER_FLARESOLVERR_URL", "")
HEADLESS = os.getenv("EPUB_FETCHER_HEADLESS", "false").strip().lower() in (
    "1", "true", "yes", "on"
)


class FetchRequest(BaseModel):
    url: str
    timeout: Optional[float] = None


@app.get("/health")
def health():
    try:
        import playwright  # noqa: F401
        playwright_ready = True
    except Exception:
        playwright_ready = False
    return {
        "status": "ok" if playwright_ready else "degraded",
        "playwright": playwright_ready,
        "flaresolverr": bool(FLARESOLVERR_URL),
        "headless": HEADLESS,
        "display": os.getenv("DISPLAY", ""),
        "output_dir": OUTPUT_DIR,
    }


@app.post("/fetch")
def fetch_endpoint(payload: FetchRequest):
    try:
        result = fetch(
            payload.url,
            OUTPUT_DIR,
            flaresolverr_url=FLARESOLVERR_URL,
            timeout=payload.timeout or DEFAULT_TIMEOUT,
            headless=HEADLESS,
        )
    except FetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return FileResponse(
        result.path,
        filename=result.filename,
        media_type="application/epub+zip",
        headers={"X-Fetch-Steps": " | ".join(result.steps)[:2000]},
        background=BackgroundTask(_remove_after_response, result.path),
    )


@app.exception_handler(FetchError)
def fetch_error_handler(_, exc: FetchError):
    return JSONResponse({"status": "error", "detail": str(exc)}, status_code=502)
