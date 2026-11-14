"""HTTP wrapper around :mod:`epub_fetcher` for NovelTrax.

POST /fetch  {"url": "...", "timeout": 150} -> the downloaded file
GET  /health -> dependency/config status
"""

from __future__ import annotations

import itertools
import os
import tempfile
import threading
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
try:
    MAX_CONCURRENCY = max(1, int(os.getenv("EPUB_FETCHER_MAX_CONCURRENCY", "1")))
except ValueError:
    MAX_CONCURRENCY = 1

_queue_cv = threading.Condition()
_queue_tickets = itertools.count()
_queue_next = 0
_queue_active = 0
_queue_waiters = 0


def _run_queued(fn):
    """FIFO gate: extra fetch requests wait instead of launching more browsers."""
    global _queue_next, _queue_active, _queue_waiters
    with _queue_cv:
        ticket = next(_queue_tickets)
        _queue_waiters += 1
        while ticket != _queue_next or _queue_active >= MAX_CONCURRENCY:
            _queue_cv.wait()
        _queue_waiters -= 1
        _queue_next += 1
        _queue_active += 1
        _queue_cv.notify_all()
    try:
        return fn()
    finally:
        with _queue_cv:
            _queue_active -= 1
            _queue_cv.notify_all()


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
        "max_concurrency": MAX_CONCURRENCY,
        "active_fetches": _queue_active,
        "queued_fetches": _queue_waiters,
        "output_dir": OUTPUT_DIR,
    }


@app.post("/fetch")
def fetch_endpoint(payload: FetchRequest):
    try:
        result = _run_queued(lambda: fetch(
            payload.url,
            OUTPUT_DIR,
            flaresolverr_url=FLARESOLVERR_URL,
            timeout=payload.timeout or DEFAULT_TIMEOUT,
            headless=HEADLESS,
        ))
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
