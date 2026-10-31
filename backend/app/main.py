import os
import json
import logging
from typing import Dict, Any, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from app.database import Database
from app.calibre.scanner import CalibreScanner
from app.providers.registry import create_provider
from app.scheduler import TaskManager
from app.covers import CoverCache

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("noveltrax")

# Directories & Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "..", "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "noveltrax.db")
CALIBRE_DIR = os.getenv("CALIBRE_DIR", "/calibre")

# Initialize Database, Cache & Task Manager
db = Database(DB_PATH)
cover_cache = CoverCache(DATA_DIR)
task_manager = TaskManager(db, CALIBRE_DIR)

# Lifespan Context Manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting NovelTrax application...")
    task_manager.start()
    # Trigger initial Calibre sync if available
    scanner = CalibreScanner(db.get_setting("calibre_path", CALIBRE_DIR))
    if scanner.is_available():
        logger.info("Found Calibre library metadata.db on startup! Running initial scan...")
        task_manager.sync_calibre()
    else:
        logger.info(f"Calibre library metadata.db not yet found at {scanner.db_path}. Will re-check periodically.")
    yield
    logger.info("Shutting down NovelTrax application...")
    task_manager.stop()

app = FastAPI(title="NovelTrax", lifespan=lifespan)

# Security Headers Middleware
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

# Setup Templates
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# --- HTML UI Routes ---

@app.get("/", response_class=HTMLResponse)
async def dashboard_view(request: Request):
    stats = db.get_dashboard_stats()
    calibre_path = db.get_setting("calibre_path", CALIBRE_DIR)
    scanner = CalibreScanner(calibre_path)
    calibre_status = scanner.get_library_summary()
    pending_requests = db.get_pending_requests()
    recent_logs = db.get_logs(limit=10)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "active_page": "dashboard",
            "stats": stats,
            "calibre_status": calibre_status,
            "pending_requests": pending_requests,
            "recent_logs": recent_logs
        }
    )

@app.get("/series", response_class=HTMLResponse)
async def series_view(request: Request):
    series_list = db.get_all_series()
    return templates.TemplateResponse(
        request=request,
        name="series.html",
        context={
            "active_page": "series",
            "series_list": series_list
        }
    )

@app.get("/releases", response_class=HTMLResponse)
async def releases_view(request: Request, status: Optional[str] = None):
    releases = db.get_releases(status=status, limit=500)
    return templates.TemplateResponse(
        request=request,
        name="releases.html",
        context={
            "active_page": "releases",
            "releases": releases,
            "current_status": status
        }
    )

@app.get("/feeds", response_class=HTMLResponse)
async def feeds_view(request: Request):
    feeds = db.get_all_feeds()
    return templates.TemplateResponse(
        request=request,
        name="feeds.html",
        context={
            "active_page": "feeds",
            "feeds": feeds
        }
    )

@app.get("/logs", response_class=HTMLResponse)
async def logs_view(request: Request):
    logs = db.get_logs(limit=150)
    return templates.TemplateResponse(
        request=request,
        name="logs.html",
        context={
            "active_page": "logs",
            "logs": logs
        }
    )

@app.get("/discovery", response_class=HTMLResponse)
async def discovery_view(request: Request):
    discovered = db.get_discovery_releases(limit=100)
    blacklisted = db.get_blacklisted_series()
    return templates.TemplateResponse(
        request=request,
        name="discovery.html",
        context={
            "active_page": "discovery",
            "discovered_releases": discovered,
            "blacklisted_series": blacklisted
        }
    )

@app.get("/settings", response_class=HTMLResponse)
async def settings_view(request: Request):
    settings = db.get_all_settings()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "active_page": "settings",
            "settings": settings
        }
    )

# --- API Endpoints ---

@app.post("/api/calibre/sync")
async def api_calibre_sync():
    """Trigger manual Calibre library rescan and request resolution."""
    res = task_manager.sync_calibre()
    return JSONResponse(res)

@app.get("/api/calibre/test")
async def api_calibre_test(path: str = Query(...)):
    """Test connection to Calibre library at given path."""
    scanner = CalibreScanner(path)
    summary = scanner.get_library_summary()
    return JSONResponse(summary)

FALLBACK_COVER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="150" viewBox="0 0 100 150"><rect width="100" height="150" rx="8" fill="#1e293b" stroke="#334155" stroke-width="2"/><path d="M35 55h30M35 70h30M35 85h20" stroke="#64748b" stroke-width="2" stroke-linecap="round"/><text x="50" y="115" font-family="system-ui,-apple-system,sans-serif" font-size="10" font-weight="600" fill="#94a3b8" text-anchor="middle">EPUB</text></svg>"""

@app.get("/api/covers")
async def api_get_cover(url: str = Query(...)):
    """Serves locally cached cover image, downloading on-demand if not already on disk."""
    if not url:
        return Response(content=FALLBACK_COVER_SVG, media_type="image/svg+xml", status_code=200)

    filepath = await cover_cache.get_or_download(url)
    if filepath and os.path.isfile(filepath):
        media_type = "image/jpeg"
        if filepath.endswith(".png"):
            media_type = "image/png"
        elif filepath.endswith(".webp"):
            media_type = "image/webp"

        return FileResponse(
            filepath,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=864000"}
        )

    # Safe fallback placeholder (prevents open redirects to arbitrary third-party URLs)
    return Response(content=FALLBACK_COVER_SVG, media_type="image/svg+xml", status_code=200)

@app.post("/api/feeds/check")
async def api_feeds_check():
    """Trigger check for all enabled feeds."""
    res = task_manager.check_feeds()
    return JSONResponse(res)

@app.post("/api/feeds/check/{feed_id}")
async def api_check_single_feed(feed_id: int):
    """Check a specific feed by id."""
    feed = db.get_feed(feed_id)
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")

    try:
        provider = create_provider(
            feed["feed_type"],
            feed["name"],
            feed["url"],
            json.loads(feed["config_json"] or "{}")
        )
        releases = provider.fetch_releases()
        db.update_feed_status(feed_id, "OK")
        return JSONResponse({"status": "success", "items_found": len(releases)})
    except Exception as e:
        db.update_feed_status(feed_id, "ERROR", str(e))
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/feeds/test")
async def api_feeds_test(payload: Dict[str, Any]):
    """Live test a feed or scraper extraction without saving to database."""
    feed_type = payload.get("feed_type", "jnovels")
    url = payload.get("url", "")
    config = payload.get("config", {})

    if not url:
        return JSONResponse({"status": "error", "message": "URL is required"}, status_code=400)

    try:
        provider = create_provider(feed_type, "Test Provider", url, config)
        releases = provider.fetch_releases()
        items = [r.to_dict() for r in releases]
        return JSONResponse({"status": "success", "count": len(items), "items": items})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/feeds")
async def api_add_feed(payload: Dict[str, Any]):
    """Add a new feed or web scraper."""
    name = payload.get("name")
    feed_type = payload.get("feed_type")
    url = payload.get("url")
    interval = payload.get("interval", 60)
    auto_request = payload.get("auto_request", True)
    config = payload.get("config", {})

    if not name or not url or not feed_type:
        return JSONResponse({"status": "error", "message": "Missing required fields"}, status_code=400)

    feed_id = db.add_feed(
        name=name,
        feed_type=feed_type,
        url=url,
        config_json=json.dumps(config),
        auto_request=auto_request,
        interval=interval
    )
    db.log_activity("FEED_ADDED", f"Added new feed: {name}", f"Type: {feed_type}, URL: {url}")
    return JSONResponse({"status": "success", "feed_id": feed_id})

@app.delete("/api/feeds/{feed_id}")
async def api_delete_feed(feed_id: int):
    """Delete a feed."""
    db.delete_feed(feed_id)
    return JSONResponse({"status": "success"})

@app.post("/api/series/{series_id}/toggle")
async def api_toggle_series(series_id: int):
    """Toggle monitored status for a series."""
    is_monitored = db.toggle_series_monitored(series_id)
    return JSONResponse({"status": "success", "is_monitored": is_monitored})

@app.post("/api/series/{series_id}/link")
async def api_update_series_link(series_id: int, payload: Dict[str, Any]):
    """Update or save custom website link for a series."""
    website_url = payload.get("website_url", "")
    db.update_series_website_url(series_id, website_url)
    return JSONResponse({"status": "success", "website_url": website_url})

@app.post("/api/releases/{release_id}/status")
async def api_update_release_status(release_id: int, payload: Dict[str, Any]):
    """Update release status (e.g., mark as IN_CALIBRE, REQUESTED, or IGNORED)."""
    status = payload.get("status")
    if not status:
        return JSONResponse({"status": "error", "message": "Status required"}, status_code=400)

    db.update_release_status(release_id, status)
    return JSONResponse({"status": "success"})

@app.post("/api/blacklist")
async def api_add_blacklist(payload: Dict[str, Any]):
    """Add a series to the blacklist and purge pending requests for it."""
    name = payload.get("name")
    reason = payload.get("reason", "")
    if not name:
        return JSONResponse({"status": "error", "message": "Series name required"}, status_code=400)

    success = db.add_to_blacklist(name, reason)
    db.log_activity("SERIES_BLACKLISTED", f"Blacklisted series: {name}", reason)
    return JSONResponse({"status": "success"})

@app.delete("/api/blacklist/{blacklist_id}")
async def api_delete_blacklist(blacklist_id: int):
    """Remove a series from the blacklist."""
    db.remove_from_blacklist(blacklist_id)
    db.log_activity("BLACKLIST_REMOVED", f"Removed series #{blacklist_id} from blacklist")
    return JSONResponse({"status": "success"})

@app.post("/api/discovery/track")
async def api_discovery_track(payload: Dict[str, Any]):
    """Track a newly discovered series and request the volume."""
    series_name = payload.get("series_name")
    release_id = payload.get("release_id")
    if not series_name:
        return JSONResponse({"status": "error", "message": "Series name required"}, status_code=400)

    db.track_discovered_series(series_name, release_id)
    db.log_activity("SERIES_TRACKED", f"Started tracking new series: {series_name}")
    return JSONResponse({"status": "success"})

@app.post("/api/discovery/blacklist")
async def api_discovery_blacklist(payload: Dict[str, Any]):
    """Blacklist a series directly from a discovery card."""
    series_name = payload.get("series_name")
    reason = payload.get("reason", "Blacklisted from Discovery panel")
    if not series_name:
        return JSONResponse({"status": "error", "message": "Series name required"}, status_code=400)

    db.add_to_blacklist(series_name, reason)
    db.log_activity("SERIES_BLACKLISTED", f"Blacklisted series from discovery: {series_name}")
    return JSONResponse({"status": "success"})

@app.post("/api/settings")
async def api_save_settings(payload: Dict[str, Any]):
    """Save application settings."""
    for k, v in payload.items():
        db.set_setting(k, str(v))
    db.log_activity("SETTINGS_UPDATED", "Application settings updated")
    return JSONResponse({"status": "success"})

# --- Historical Backfill & Synopsis Endpoints ---

@app.post("/api/backfill/index")
async def api_backfill_index():
    """Immediately populates Discovery from J-Novels Complete Series Index."""
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, task_manager.ingest_master_index)
    return JSONResponse(result)

@app.post("/api/backfill/start")
async def api_backfill_start(payload: Optional[Dict[str, Any]] = None):
    """Starts the polite historical backfill (Master index + synopses + pagination)."""
    import asyncio
    if task_manager.is_backfilling:
        return JSONResponse({"status": "in_progress", "message": "Backfill already running"})

    p = payload or {}
    max_pages = int(p.get("max_pages", 30))
    delay = float(p.get("delay", 2.0))
    enrich = bool(p.get("enrich_synopses", True))

    asyncio.create_task(task_manager.run_polite_backfill(
        max_pages=max_pages,
        enrich_synopses=enrich,
        delay_seconds=delay
    ))
    return JSONResponse({"status": "started", "message": "Polite backfill started in background"})

@app.post("/api/backfill/stop")
async def api_backfill_stop():
    """Signals the running backfiller to stop gracefully."""
    task_manager.stop_backfill()
    return JSONResponse({"status": "stopping", "message": "Backfill stopping"})

@app.get("/api/backfill/status")
async def api_backfill_status():
    """Returns current backfill progress and state."""
    return JSONResponse(task_manager.get_backfill_status())

@app.post("/api/releases/{release_id}/synopsis")
async def api_fetch_synopsis(release_id: int):
    """On-demand synopsis and canonical series link extraction for a release."""
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM releases WHERE id = ?;", (release_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Release not found")

    target_url = row["series_link"] or row["link"]
    if not target_url:
        return JSONResponse({"status": "error", "message": "No link available"}, status_code=400)

    from app.providers.jnovels import JNovelsProvider
    details = JNovelsProvider.extract_synopsis_and_original_link(target_url)

    db.update_release_details(
        release_id,
        description=details.get("synopsis"),
        series_link=details.get("series_link"),
        cover_url=details.get("cover_url")
    )
    if details.get("cover_url"):
        task_manager.cover_cache.cache_sync(details["cover_url"])

    return JSONResponse({
        "status": "success",
        "synopsis": details.get("synopsis"),
        "series_link": details.get("series_link"),
        "cover_url": details.get("cover_url")
    })

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)
