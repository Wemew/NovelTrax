"""HTTP wrapper around :mod:`epub_fetcher` for NovelTrax.

POST /fetch  {"url": "...", "timeout": 150} -> the downloaded file
GET  /health -> dependency/config status
"""

from __future__ import annotations

import itertools
import logging
import os
import tempfile
import threading
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from epub_fetcher import FetchError, fetch

LOG_LEVEL = os.getenv("EPUB_FETCHER_LOG_LEVEL", "INFO").strip().upper() or "INFO"
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("epub_fetcher.server")


def _remove_after_response(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class DeletingFileResponse(FileResponse):
    """FileResponse that deletes the temp file even if the client disconnects."""

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            _remove_after_response(self.path)


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
        "log_level": LOG_LEVEL,
        "max_concurrency": MAX_CONCURRENCY,
        "active_fetches": _queue_active,
        "queued_fetches": _queue_waiters,
        "output_dir": OUTPUT_DIR,
    }


@app.post("/fetch")
def fetch_endpoint(payload: FetchRequest):
    arrived = time.monotonic()
    budget = float(payload.timeout or DEFAULT_TIMEOUT)
    logger.info("Fetch request accepted: %s (queue: %s active, %s waiting, budget %.0fs)",
                payload.url, _queue_active, _queue_waiters, budget)

    def run():
        waited = time.monotonic() - arrived
        remaining = budget - waited
        if remaining < 10.0:
            raise FetchError(
                f"Timed out after {waited:.0f}s waiting for the EPUB fetcher queue "
                f"(request budget {budget:.0f}s)"
            )
        logger.info("Fetch starting after %.1fs queue wait: %s (remaining %.0fs)",
                    waited, payload.url, remaining)
        return fetch(
            payload.url,
            OUTPUT_DIR,
            flaresolverr_url=FLARESOLVERR_URL,
            timeout=remaining,
            headless=HEADLESS,
        )

    try:
        result = _run_queued(run)
    except FetchError as exc:
        logger.warning("Fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("Fetch succeeded: %s (%s bytes)", result.filename, os.path.getsize(result.path))
    return DeletingFileResponse(
        result.path,
        filename=result.filename,
        media_type="application/epub+zip",
        headers={"X-Fetch-Steps": " | ".join(result.steps)[:2000]},
    )


@app.exception_handler(FetchError)
def fetch_error_handler(_, exc: FetchError):
    return JSONResponse({"status": "error", "detail": str(exc)}, status_code=502)
