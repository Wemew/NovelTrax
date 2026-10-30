import os
import json
import logging
from typing import Dict, Any, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from app.database import Database
from app.calibre.scanner import CalibreScanner
from app.providers.registry import create_provider
from app.scheduler import TaskManager

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("novelpulse")

# Directories & Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "..", "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "novelpulse.db")
CALIBRE_DIR = os.getenv("CALIBRE_DIR", "/calibre")

# Initialize Database & Task Manager
db = Database(DB_PATH)
task_manager = TaskManager(db, CALIBRE_DIR)

# Lifespan Context Manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting NovelPulse application...")
    task_manager.start()
    # Trigger initial Calibre sync if available
    scanner = CalibreScanner(db.get_setting("calibre_path", CALIBRE_DIR))
    if scanner.is_available():
        logger.info("Found Calibre library metadata.db on startup! Running initial scan...")
        task_manager.sync_calibre()
    else:
        logger.info(f"Calibre library metadata.db not yet found at {scanner.db_path}. Will re-check periodically.")
    yield
    logger.info("Shutting down NovelPulse application...")
    task_manager.stop()

app = FastAPI(title="NovelPulse", lifespan=lifespan)

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

    return templates.TemplateResponse("index.html", {
        "request": request,
        "active_page": "dashboard",
        "stats": stats,
        "calibre_status": calibre_status,
        "pending_requests": pending_requests,
        "recent_logs": recent_logs
    })

@app.get("/series", response_class=HTMLResponse)
async def series_view(request: Request):
    series_list = db.get_all_series()
    return templates.TemplateResponse("series.html", {
        "request": request,
        "active_page": "series",
        "series_list": series_list
    })

@app.get("/releases", response_class=HTMLResponse)
async def releases_view(request: Request, status: Optional[str] = None):
    releases = db.get_releases(status=status, limit=500)
    return templates.TemplateResponse("releases.html", {
        "request": request,
        "active_page": "releases",
        "releases": releases,
        "current_status": status
    })

@app.get("/feeds", response_class=HTMLResponse)
async def feeds_view(request: Request):
    feeds = db.get_all_feeds()
    return templates.TemplateResponse("feeds.html", {
        "request": request,
        "active_page": "feeds",
        "feeds": feeds
    })

@app.get("/logs", response_class=HTMLResponse)
async def logs_view(request: Request):
    logs = db.get_logs(limit=150)
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "active_page": "logs",
        "logs": logs
    })

@app.get("/settings", response_class=HTMLResponse)
async def settings_view(request: Request):
    settings = db.get_all_settings()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "active_page": "settings",
        "settings": settings
    })

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

@app.post("/api/releases/{release_id}/status")
async def api_update_release_status(release_id: int, payload: Dict[str, Any]):
    """Update release status (e.g., mark as IN_CALIBRE, REQUESTED, or IGNORED)."""
    status = payload.get("status")
    if not status:
        return JSONResponse({"status": "error", "message": "Status required"}, status_code=400)

    db.update_release_status(release_id, status)
    return JSONResponse({"status": "success"})

@app.post("/api/settings")
async def api_save_settings(payload: Dict[str, Any]):
    """Save application settings."""
    for k, v in payload.items():
        db.set_setting(k, str(v))
    db.log_activity("SETTINGS_UPDATED", "Application settings updated")
    return JSONResponse({"status": "success"})

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)
