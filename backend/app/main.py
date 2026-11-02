import os
import json
import math
import logging
import asyncio
from typing import Dict, Any, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from app.database import Database
from app.calibre.scanner import CalibreScanner
from app.providers.registry import create_provider
from app.scheduler import TaskManager
from app.covers import CoverCache
from app.security import is_safe_public_url, sanitize_calibre_path
from app.downloader.qbit import QBitClient

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

def get_qbit_client() -> QBitClient:
    """Instantiates QBitClient configured from current app_settings in database."""
    settings = db.get_all_settings()
    url = settings.get("qbit_url", "").strip()
    host = settings.get("qbit_host", "").strip()
    port_str = settings.get("qbit_port", "").strip()
    use_ssl_str = str(settings.get("qbit_use_ssl", "false")).lower()
    enabled_str = str(settings.get("qbit_enabled", "true")).lower()
    is_enabled = enabled_str not in ("false", "0", "no", "off")

    if url and not host:
        from urllib.parse import urlparse
        p = urlparse(url)
        host = p.hostname or "localhost"
        port = p.port or (443 if p.scheme == "https" else 8080)
        use_ssl = (p.scheme == "https")
    else:
        host = host or "localhost"
        port = int(port_str) if port_str.isdigit() else 8080
        use_ssl = (use_ssl_str in ("true", "1", "yes"))

    return QBitClient(
        host=host,
        port=port,
        username=settings.get("qbit_username", "admin"),
        password=settings.get("qbit_password", ""),
        use_ssl=use_ssl,
        base_url=url if url else None,
        enabled=is_enabled,
        category=settings.get("qbit_category", "novels") or "novels",
        tags=settings.get("qbit_tags", "noveltrax") or "noveltrax",
        savepath=settings.get("qbit_savepath", os.getenv("DOWNLOAD_DIR", "/downloads"))
    )

from app.providers.metadata import NovelMetadataProvider
NovelMetadataProvider.set_db(db)

# Lifespan Context Manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting NovelTrax application...")
    task_manager.start()
    # Trigger initial database cleanup (PDF purge and discovery deduplication)
    try:
        db.ensure_prioritized_groups_table()
        db.deduplicate_and_clean_discovery()
        logger.info("Startup database maintenance completed: PDF releases purged, series deduplicated, prioritized groups seeded.")
    except Exception as e:
        logger.warning(f"Startup database maintenance warning: {e}")

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
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'self';"
    )
    return response

# Setup Templates
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

from app.timezone_util import format_localtime, format_localdate

def jinja_localtime(value: Optional[str], fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    user_tz = db.get_setting("app_timezone", "")
    return format_localtime(value, fmt=fmt, custom_tz=user_tz)

def jinja_localdate(value: Optional[str], fmt: str = "%Y-%m-%d") -> str:
    user_tz = db.get_setting("app_timezone", "")
    return format_localdate(value, fmt=fmt, custom_tz=user_tz)

templates.env.filters["localtime"] = jinja_localtime
templates.env.filters["localdate"] = jinja_localdate

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
async def releases_view(
    request: Request,
    status: Optional[str] = None,
    view: str = Query("series"),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=10, le=200),
    sort: str = Query("date_desc"),
    q: Optional[str] = Query(None),
    include_ignored: bool = Query(False)
):
    clean_q = q.strip() if q else None
    if view != "flat":
        all_groups = db.get_releases_grouped_by_series(status=status, search=clean_q, sort=sort, include_ignored=include_ignored)
        total_count = len(all_groups)
        total_pages = max(1, math.ceil(total_count / page_size))
        current_page = min(page, total_pages) if total_count > 0 else 1
        offset = (current_page - 1) * page_size
        grouped_series = all_groups[offset : offset + page_size]
        releases = []
    else:
        grouped_series = []
        total_count = db.count_releases(status=status, search=clean_q, include_ignored=include_ignored)
        total_pages = max(1, math.ceil(total_count / page_size))
        current_page = min(page, total_pages) if total_count > 0 else 1
        offset = (current_page - 1) * page_size
        releases = db.get_releases(status=status, limit=page_size, offset=offset, search=clean_q, sort=sort, include_ignored=include_ignored)

    status_counts = db.get_release_status_counts()

    return templates.TemplateResponse(
        request=request,
        name="releases.html",
        context={
            "active_page": "releases",
            "releases": releases,
            "grouped_series": grouped_series,
            "current_status": status,
            "current_view": view,
            "current_sort": sort,
            "search_query": clean_q or "",
            "current_page": current_page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_count": total_count,
            "status_counts": status_counts,
            "include_ignored": include_ignored
        }
    )

@app.get("/prioritized", response_class=HTMLResponse)
async def prioritized_view(
    request: Request,
    view: str = Query("series"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=10, le=200),
    sort: str = Query("wanted_first"),
    q: Optional[str] = Query(None)
):
    clean_q = q.strip() if q else None
    prioritized_groups = db.get_prioritized_groups()

    if view != "flat":
        all_groups = db.get_releases_grouped_by_series(status=status, search=clean_q, sort=sort, prioritized_only=True)
        total_count = len(all_groups)
        total_pages = max(1, math.ceil(total_count / page_size))
        current_page = min(page, total_pages) if total_count > 0 else 1
        offset = (current_page - 1) * page_size
        grouped_series = all_groups[offset : offset + page_size]
        releases = []
    else:
        grouped_series = []
        total_count = db.count_prioritized_releases(status=status, search=clean_q)
        total_pages = max(1, math.ceil(total_count / page_size))
        current_page = min(page, total_pages) if total_count > 0 else 1
        offset = (current_page - 1) * page_size
        releases = db.get_prioritized_releases(status=status, limit=page_size, offset=offset, search=clean_q, sort=sort)

    return templates.TemplateResponse(
        request=request,
        name="prioritized.html",
        context={
            "active_page": "prioritized",
            "prioritized_groups": prioritized_groups,
            "releases": releases,
            "grouped_series": grouped_series,
            "current_status": status,
            "current_view": view,
            "current_sort": sort,
            "search_query": clean_q or "",
            "current_page": current_page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_count": total_count
        }
    )

@app.get("/api/prioritized/cards")
async def api_prioritized_cards(
    request: Request,
    status: Optional[str] = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(8, ge=1, le=50),
    search: Optional[str] = Query(None),
    sort: str = Query("wanted_first")
):
    clean_s = search.strip() if search else None
    groups = db.get_releases_grouped_by_series(status=status, limit=limit, offset=offset, search=clean_s, sort=sort, prioritized_only=True)
    total = len(db.get_releases_grouped_by_series(status=status, search=clean_s, prioritized_only=True))
    has_more = (offset + len(groups)) < total
    html = templates.get_template("partials/series_card_chunk.html").render(
        {"request": request, "grouped_series": groups}
    )
    return JSONResponse({
        "status": "success",
        "offset": offset,
        "count": len(groups),
        "total": total,
        "has_more": has_more,
        "html": html
    })

@app.get("/api/prioritized/flat_rows")
async def api_prioritized_flat_rows(
    request: Request,
    status: Optional[str] = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    sort: str = Query("date_desc")
):
    clean_s = search.strip() if search else None
    releases = db.get_prioritized_releases(status=status, limit=limit, offset=offset, search=clean_s, sort=sort)
    total = db.count_prioritized_releases(status=status, search=clean_s)
    has_more = (offset + len(releases)) < total
    html = templates.get_template("partials/flat_release_row_chunk.html").render(
        {"request": request, "releases": releases}
    )
    return JSONResponse({
        "status": "success",
        "offset": offset,
        "count": len(releases),
        "total": total,
        "has_more": has_more,
        "html": html
    })

# --- Lazy Loading API Endpoints for Releases & Discovery ---
@app.get("/api/releases/cards")
async def api_releases_cards(
    request: Request,
    status: Optional[str] = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(8, ge=1, le=50),
    search: Optional[str] = Query(None),
    sort: str = Query("date_desc")
):
    clean_s = search.strip() if search else None
    groups = db.get_releases_grouped_by_series(status=status, limit=limit, offset=offset, search=clean_s, sort=sort)
    total = db.count_releases_grouped_by_series(status=status, search=clean_s)
    has_more = (offset + len(groups)) < total
    html = templates.get_template("partials/series_card_chunk.html").render(
        {"request": request, "grouped_series": groups}
    )
    return JSONResponse({
        "status": "success",
        "offset": offset,
        "count": len(groups),
        "total": total,
        "has_more": has_more,
        "html": html
    })

@app.get("/api/releases/flat_rows")
async def api_releases_flat_rows(
    request: Request,
    status: Optional[str] = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    sort: str = Query("date_desc")
):
    clean_s = search.strip() if search else None
    releases = db.get_releases(status=status, limit=limit, offset=offset, search=clean_s, sort=sort)
    total = db.count_releases(status=status, search=clean_s)
    has_more = (offset + len(releases)) < total
    html = templates.get_template("partials/flat_release_row_chunk.html").render(
        {"request": request, "releases": releases}
    )
    return JSONResponse({
        "status": "success",
        "offset": offset,
        "count": len(releases),
        "total": total,
        "has_more": has_more,
        "html": html
    })

@app.get("/api/discovery/cards")
async def api_discovery_cards(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(12, ge=1, le=60),
    search: Optional[str] = Query(None),
    sort: str = Query("date_desc")
):
    clean_s = search.strip() if search else None
    discovered = db.get_discovery_releases(limit=limit, offset=offset, search=clean_s, sort=sort)
    total = db.count_discovery_releases(search=clean_s)
    has_more = (offset + len(discovered)) < total
    html = templates.get_template("partials/discovery_card_chunk.html").render(
        {"request": request, "discovered_releases": discovered}
    )
    return JSONResponse({
        "status": "success",
        "offset": offset,
        "count": len(discovered),
        "total": total,
        "has_more": has_more,
        "html": html
    })

@app.get("/api/releases/grouped")
@app.get("/api/series_releases")
async def api_releases_grouped(
    status: Optional[str] = None,
    include_ignored: bool = Query(False)
):
    """Returns all releases aggregated into canonical series cards."""
    groups = db.get_releases_grouped_by_series(status=status, include_ignored=include_ignored)
    return JSONResponse({"status": "success", "count": len(groups), "series_groups": groups})

api_get_series_releases = api_releases_grouped

@app.get("/api/releases")
async def api_get_releases(
    status: Optional[str] = None,
    include_ignored: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None),
    sort: str = Query("date_desc")
):
    """Returns list of releases matching filters, with default exclusion of IGNORED."""
    releases = db.get_releases(status=status, limit=limit, offset=offset, search=search, sort=sort, include_ignored=include_ignored)
    return JSONResponse({"status": "success", "count": len(releases), "releases": releases})

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
async def discovery_view(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=6, le=120),
    q: Optional[str] = Query(None),
    sort: str = Query("date_desc")
):
    clean_q = q.strip() if q else None
    total_count = db.count_discovery_releases(search=clean_q)
    total_pages = max(1, math.ceil(total_count / page_size))
    current_page = min(page, total_pages) if total_count > 0 else 1
    offset = (current_page - 1) * page_size
    discovered = db.get_discovery_releases(limit=page_size, offset=offset, search=clean_q, sort=sort)
    blacklisted = db.get_blacklisted_series()
    settings = db.get_all_settings()

    return templates.TemplateResponse(
        request=request,
        name="discovery.html",
        context={
            "active_page": "discovery",
            "discovered_releases": discovered,
            "blacklisted_series": blacklisted,
            "settings": settings,
            "current_page": current_page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_count": total_count,
            "search_query": clean_q or "",
            "current_sort": sort
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

@app.get("/downloads", response_class=HTMLResponse)
async def downloads_view(request: Request):
    """Renders Downloader & Queue Management page with live status and active torrents."""
    qbit = get_qbit_client()
    status = await asyncio.to_thread(qbit.test_connection, timeout=2.0)
    torrents = await asyncio.to_thread(qbit.get_torrents, category=qbit.category) if status.get("success") else []
    settings = db.get_all_settings()

    return templates.TemplateResponse(
        request=request,
        name="downloads.html",
        context={
            "active_page": "downloads",
            "qbit_status": status,
            "torrents": torrents,
            "settings": settings,
            "download_dir": settings.get("qbit_savepath", "/downloads")
        }
    )

# --- API Endpoints ---

@app.post("/api/calibre/sync")
async def api_calibre_sync():
    """Trigger manual Calibre library rescan and request resolution."""
    res = await asyncio.to_thread(task_manager.sync_calibre)
    return JSONResponse(res)

@app.get("/api/calibre/test")
async def api_calibre_test(path: str = Query(...)):
    """Test connection to Calibre library at given path."""
    try:
        clean_path = sanitize_calibre_path(path)
    except ValueError as e:
        return JSONResponse({"available": False, "error": str(e), "total_books": 0, "total_series": 0, "total_authors": 0}, status_code=400)

    def _test():
        scanner = CalibreScanner(clean_path)
        return scanner.get_library_summary()
    summary = await asyncio.to_thread(_test)
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
    res = await asyncio.to_thread(task_manager.check_feeds)
    return JSONResponse(res)

@app.post("/api/feeds/check/{feed_id}")
async def api_check_single_feed(feed_id: int):
    """Check a specific feed by id."""
    feed = db.get_feed(feed_id)
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")

    try:
        def _fetch():
            provider = create_provider(
                feed["feed_type"],
                feed["name"],
                feed["url"],
                json.loads(feed["config_json"] or "{}")
            )
            return provider.fetch_releases()
        releases = await asyncio.to_thread(_fetch)
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

    if not is_safe_public_url(url):
        return JSONResponse({
            "status": "error",
            "message": "Blocked by SSRF protection: URL must target a public HTTP/HTTPS destination."
        }, status_code=400)

    try:
        def _test():
            provider = create_provider(feed_type, "Test Provider", url, config)
            return provider.fetch_releases()
        releases = await asyncio.to_thread(_test)
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

    if not is_safe_public_url(url):
        return JSONResponse({
            "status": "error",
            "message": "Blocked by SSRF protection: Feed URL must target a public HTTP/HTTPS destination."
        }, status_code=400)

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

@app.post("/api/feeds/{feed_id}")
async def api_update_feed(feed_id: int, payload: Dict[str, Any]):
    """Update an existing feed configuration."""
    feed = db.get_feed(feed_id)
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")

    name = payload.get("name", feed["name"])
    feed_type = payload.get("feed_type", feed["feed_type"])
    url = payload.get("url", feed["url"])
    interval = int(payload.get("interval", feed["check_interval_minutes"]))
    auto_request = bool(payload.get("auto_request", feed["auto_request"]))
    config = payload.get("config")
    config_json = json.dumps(config) if config is not None else feed["config_json"]

    if url and not is_safe_public_url(url):
        return JSONResponse({
            "status": "error",
            "message": "Blocked by SSRF protection: Feed URL must target a public HTTP/HTTPS destination."
        }, status_code=400)

    db.update_feed(
        feed_id=feed_id,
        name=name,
        feed_type=feed_type,
        url=url,
        config_json=config_json,
        enabled=bool(feed["enabled"]),
        auto_request=auto_request,
        interval=interval
    )
    db.log_activity("FEED_UPDATED", f"Updated feed: {name}", f"Interval: {interval}m, URL: {url}")
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

@app.post("/api/releases/{release_id}/ignore")
async def api_ignore_release(release_id: int):
    """1-Click Ignore: marks release as IGNORED and hides from active views."""
    target = db.get_release_by_id(release_id)
    if not target:
        raise HTTPException(status_code=404, detail="Release not found")
    db.ignore_release(release_id)
    db.log_activity("RELEASE_IGNORED", f"Ignored release #{release_id}: {target.get('title')}")
    return JSONResponse({"status": "success", "message": "Release marked as ignored"})

@app.post("/api/releases/{release_id}/unignore")
async def api_unignore_release(release_id: int):
    """1-Click Unignore: restores an IGNORED release to REQUESTED."""
    target = db.get_release_by_id(release_id)
    if not target:
        raise HTTPException(status_code=404, detail="Release not found")
    db.unignore_release(release_id)
    db.log_activity("RELEASE_RESTORED", f"Restored release #{release_id}: {target.get('title')}")
    return JSONResponse({"status": "success", "message": "Release restored from ignored"})

# --- Prioritized Release Groups Endpoints ---

@app.get("/api/prioritized_groups")
@app.get("/api/prioritized/groups")
async def api_get_prioritized_groups():
    """Returns all prioritized release groups with live release counts."""
    groups = db.get_prioritized_groups()
    return JSONResponse({"status": "success", "groups": groups})

@app.post("/api/prioritized_groups")
@app.post("/api/prioritized/groups")
async def api_add_prioritized_group(payload: Dict[str, Any]):
    """Add or update a prioritized release group."""
    name = (payload.get("name") or "").strip()
    notes = payload.get("notes", "")
    priority = int(payload.get("priority", 1))
    if not name:
        return JSONResponse({"status": "error", "message": "Group name is required"}, status_code=400)

    success = db.add_prioritized_group(name, notes, priority)
    db.log_activity("GROUP_PRIORITIZED", f"Added prioritized release group: {name}", notes)
    return JSONResponse({"status": "success", "message": f"Added prioritized group: {name}"})

@app.delete("/api/prioritized_groups/{group_id}")
@app.delete("/api/prioritized/groups/{group_id}")
async def api_delete_prioritized_group(group_id: int):
    """Remove a prioritized release group."""
    deleted = db.remove_prioritized_group(group_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Prioritized group not found")
    db.log_activity("GROUP_UNPRIORITIZED", f"Removed prioritized group #{group_id}")
    return JSONResponse({"status": "success", "message": f"Removed prioritized group #{group_id}"})

@app.get("/api/prioritized/releases")
async def api_get_prioritized_releases(status: Optional[str] = None):
    """Returns releases from prioritized groups."""
    releases = db.get_prioritized_releases(status=status)
    return JSONResponse({"status": "success", "count": len(releases), "releases": releases})

# --- Targeted Series Backfill Endpoints ---

@app.post("/api/series/{series_id}/backfill")
async def api_series_backfill(series_id: int, payload: Optional[Dict[str, Any]] = None):
    """Executes a targeted search backfill in background for a tracked series by series ID."""
    series_list = db.get_all_series()
    target = next((s for s in series_list if s["id"] == series_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Series not found")

    p = payload or {}
    max_pages = int(p.get("max_pages", 3))
    res = task_manager.run_series_backfill_async(target["name"], max_pages=max_pages)
    return JSONResponse(res)

@app.post("/api/series/backfill")
async def api_series_backfill_by_name(payload: Dict[str, Any]):
    """Executes a targeted search backfill in background by series name."""
    series_name = (payload.get("series_name") or "").strip()
    if not series_name:
        return JSONResponse({"status": "error", "message": "Series name is required"}, status_code=400)
    max_pages = int(payload.get("max_pages", 3))
    res = task_manager.run_series_backfill_async(series_name, max_pages=max_pages)
    return JSONResponse(res)

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
    """Save application settings and dynamically reschedule background jobs."""
    for k, v in payload.items():
        db.set_setting(k, str(v))

    # Synchronize feed interval across all existing feeds if updated
    if "feed_check_interval_minutes" in payload:
        try:
            feed_interval = int(payload["feed_check_interval_minutes"])
            db.update_all_feed_intervals(feed_interval)
        except (ValueError, TypeError):
            pass

    # Update Calibre library path if changed
    if "calibre_path" in payload:
        task_manager.calibre_path = str(payload["calibre_path"])

    if "track_official_only" in payload:
        db.deduplicate_and_clean_discovery()

    # Dynamically reschedule APScheduler tasks
    calibre_min = None
    if "calibre_sync_interval_minutes" in payload:
        try:
            calibre_min = int(payload["calibre_sync_interval_minutes"])
        except (ValueError, TypeError):
            pass

    feed_min = None
    if "feed_check_interval_minutes" in payload:
        try:
            feed_min = int(payload["feed_check_interval_minutes"])
        except (ValueError, TypeError):
            pass

    task_manager.reschedule_jobs(calibre_minutes=calibre_min, feed_minutes=feed_min)
    db.log_activity("SETTINGS_UPDATED", "Application settings and automation intervals updated")
    return JSONResponse({"status": "success"})

@app.post("/api/settings/test-webhook")
async def api_test_webhook(payload: Optional[Dict[str, Any]] = None):
    """Sends a live test notification to Discord and/or generic webhook."""
    p = payload or {}
    discord_url = p.get("discord_webhook_url")
    apprise_url = p.get("apprise_url")

    # If not passed in body, fall back to current saved settings
    if discord_url is None:
        discord_url = db.get_setting("discord_webhook_url", "")
    if apprise_url is None:
        apprise_url = db.get_setting("apprise_url", "")

    notifier = task_manager.get_notifier()
    result = notifier.test_webhook(discord_url=discord_url, apprise_url=apprise_url)
    status_code = 200 if result.get("status") == "success" else 400
    return JSONResponse(result, status_code=status_code)

# --- Downloader & Queue Management Endpoints ---

@app.get("/api/downloader/queue")
@app.get("/api/downloads")
@app.get("/api/downloads/queue")
async def api_downloader_queue():
    """Retrieves list of active torrents from qBittorrent."""
    qbit = get_qbit_client()
    torrents = await asyncio.to_thread(qbit.get_torrents, category=qbit.category)
    return JSONResponse({"status": "success", "torrents": torrents})

@app.get("/api/downloads/status")
async def api_downloads_status():
    """Returns connectivity and version status of the in-app downloader."""
    qbit = get_qbit_client()
    status = await asyncio.to_thread(qbit.test_connection, timeout=3.0)
    return JSONResponse({
        "status": "success",
        "connected": bool(status.get("success")),
        "version": status.get("version", ""),
        "message": status.get("message", "")
    })

@app.post("/api/downloader/test")
@app.post("/api/settings/downloader/test")
async def api_downloader_test(payload: Optional[Dict[str, Any]] = None):
    """Tests connection to qBittorrent using payload parameters or current database settings."""
    p = payload or {}
    host = p.get("qbit_host") or p.get("host")
    port = p.get("qbit_port") or p.get("port")
    username = p.get("qbit_username") or p.get("username")
    password = p.get("qbit_password") or p.get("password")
    use_ssl = p.get("qbit_use_ssl") if p.get("qbit_use_ssl") is not None else False
    qbit_url = p.get("qbit_url") or p.get("url")

    if qbit_url and not host:
        from urllib.parse import urlparse
        u = urlparse(qbit_url)
        host = u.hostname or "localhost"
        port = u.port or (443 if u.scheme == "https" else 8080)
        use_ssl = (u.scheme == "https")

    if host or username or (port is not None):
        client = QBitClient(
            host=host or "localhost",
            port=int(port or 8080),
            username=username or "",
            password=password or "",
            use_ssl=bool(use_ssl),
            base_url=qbit_url if qbit_url else None,
            enabled=True
        )
    else:
        client = get_qbit_client()

    res = await asyncio.to_thread(client.test_connection, timeout=3.0, force=True)
    return JSONResponse(res)

@app.post("/api/downloader/configure")
@app.post("/api/settings/downloader")
async def api_downloader_configure(payload: Dict[str, Any]):
    """Configures and saves qBittorrent downloader settings."""
    qbit_host = (payload.get("qbit_host") or "").strip()
    qbit_port = str(payload.get("qbit_port") or "8080").strip()
    qbit_user = (payload.get("qbit_username") or "").strip()
    qbit_pass = payload.get("qbit_password")
    qbit_ssl = str(payload.get("qbit_use_ssl", "false")).lower()
    qbit_enabled = str(payload.get("qbit_enabled", "true")).lower()
    qbit_category = (payload.get("qbit_category") or "novels").strip() or "novels"
    qbit_savepath = (payload.get("qbit_savepath") or "/downloads").strip() or "/downloads"
    qbit_url = (payload.get("qbit_url") or "").strip()

    # SSRF destination safety validation before saving configuration
    dest_to_check = qbit_url or qbit_host
    if dest_to_check:
        from app.security import validate_downloader_destination
        scheme = "https" if qbit_ssl in ("true", "1", "yes") else "http"
        is_safe, error_msg = validate_downloader_destination(dest_to_check, scheme=scheme)
        if not is_safe:
            return JSONResponse({
                "status": "error",
                "message": error_msg,
                "connection": {
                    "success": False,
                    "version": "",
                    "message": error_msg
                }
            }, status_code=400)

    if qbit_host:
        db.set_setting("qbit_host", qbit_host)
    if qbit_port:
        db.set_setting("qbit_port", qbit_port)
    db.set_setting("qbit_username", qbit_user)
    if qbit_pass is not None:
        db.set_setting("qbit_password", qbit_pass)
    db.set_setting("qbit_use_ssl", qbit_ssl)
    db.set_setting("qbit_enabled", qbit_enabled)
    db.set_setting("qbit_category", qbit_category)
    db.set_setting("qbit_savepath", qbit_savepath)
    if qbit_url:
        db.set_setting("qbit_url", qbit_url)

    db.log_activity("DOWNLOADER_CONFIGURED", f"qBittorrent downloader configured: {qbit_host}:{qbit_port} (Enabled: {qbit_enabled})")

    client = get_qbit_client()
    test_res = await asyncio.to_thread(client.test_connection, timeout=3.0, force=True)
    return JSONResponse({
        "status": "success",
        "message": "Downloader settings saved successfully",
        "connection": test_res
    })

@app.post("/api/downloader/pause/{torrent_hash}")
@app.post("/api/downloads/{torrent_hash}/pause")
async def api_downloader_pause(torrent_hash: str):
    """Pauses a torrent in qBittorrent."""
    qbit = get_qbit_client()
    success = await asyncio.to_thread(qbit.pause_torrents, torrent_hash)
    return JSONResponse({"status": "success" if success else "error"})

@app.post("/api/downloader/resume/{torrent_hash}")
@app.post("/api/downloads/{torrent_hash}/resume")
async def api_downloader_resume(torrent_hash: str):
    """Resumes a paused torrent in qBittorrent."""
    qbit = get_qbit_client()
    success = await asyncio.to_thread(qbit.resume_torrents, torrent_hash)
    return JSONResponse({"status": "success" if success else "error"})

@app.delete("/api/downloader/delete/{torrent_hash}")
@app.delete("/api/downloads/{torrent_hash}")
@app.post("/api/downloads/{torrent_hash}/delete")
async def api_downloader_delete(torrent_hash: str, delete_files: bool = Query(False)):
    """Deletes a torrent from qBittorrent, optionally deleting downloaded data files."""
    qbit = get_qbit_client()
    success = await asyncio.to_thread(qbit.delete_torrents, torrent_hash, delete_files=delete_files)
    return JSONResponse({"status": "success" if success else "error"})

@app.post("/api/downloads/{torrent_hash}/selective")
async def api_downloads_selective(torrent_hash: str, payload: Optional[Dict[str, Any]] = None):
    """Applies selective volume downloading to a multi-file torrent bundle."""
    qbit = get_qbit_client()
    p = payload or {}
    owned = p.get("owned_volumes")
    wanted = p.get("wanted_volumes")
    series_name = p.get("series_name")
    res = await asyncio.to_thread(
        qbit.apply_selective_volume_download,
        torrent_hash,
        owned_volumes=owned,
        wanted_volumes=wanted,
        series_name=series_name
    )
    return JSONResponse(res)

@app.post("/api/releases/{release_id}/download")
async def api_release_download(release_id: int):
    """Triggers manual download of a release via qBittorrent, with selective volume filtering if bundle."""
    target = db.get_release_by_id(release_id)
    if not target:
        raise HTTPException(status_code=404, detail="Release not found")

    download_url = target.get("download_url") or target.get("link")
    if not download_url:
        raise HTTPException(status_code=400, detail="No downloadable URL or magnet link for release")

    qbit = get_qbit_client()
    savepath = db.get_setting("qbit_savepath", "/downloads")
    category = db.get_setting("qbit_category", "novels")

    success = await asyncio.to_thread(
        qbit.add_torrent_url,
        url=download_url,
        category=category,
        savepath=savepath
    )

    if success:
        torrent_h = qbit.extract_torrent_hash(download_url)
        db.update_download_status(release_id, "DOWNLOADING", torrent_h)
        db.log_activity("MANUAL_DOWNLOAD", f"Sent to downloader: '{target['title']}'", f"URL: {download_url[:60]}...")

        # Trigger selective volume unchecking if bundle
        missing = target.get("missing_volumes") or json.loads(target.get("missing_vols_json") or "[]")
        vols = target.get("volumes") or json.loads(target.get("volumes_json") or "[]")
        if torrent_h and target.get("is_bundle") and missing and len(missing) < len(vols):
            sel_count = await asyncio.to_thread(
                qbit.apply_selective_volume_download,
                torrent_h,
                wanted_volumes=[float(v) for v in missing],
                series_name=target.get("series_name")
            )
            db.log_activity("SELECTIVE_DOWNLOAD", f"Selective download applied for '{target['title']}': skipped {int(sel_count)} owned volume files.")

        return JSONResponse({"status": "success", "message": "Sent to downloader successfully"})
    else:
        return JSONResponse({"status": "error", "message": "Failed to add release to downloader"}, status_code=500)

# --- Historical Backfill & Synopsis Endpoints ---

@app.post("/api/backfill/index")
async def api_backfill_index():
    """Immediately populates Discovery from J-Novels Complete Series Index."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, task_manager.ingest_master_index)
    return JSONResponse(result)

@app.post("/api/backfill/start")
async def api_backfill_start(payload: Optional[Dict[str, Any]] = None):
    """Starts the polite historical backfill (Master index + synopses + pagination)."""
    if task_manager.is_backfilling:
        return JSONResponse({"status": "in_progress", "message": "Backfill already running"})

    p = payload or {}
    max_pages = p.get("max_pages")
    if max_pages is not None:
        max_pages = int(max_pages)
    delay = p.get("delay")
    if delay is not None:
        delay = float(delay)
    enrich = bool(p.get("enrich_synopses", True))

    asyncio.create_task(task_manager.run_polite_backfill(
        max_pages=max_pages,
        enrich_synopses=enrich,
        delay_seconds=delay
    ))
    return JSONResponse({"status": "started", "message": "Polite backfill started in background"})

@app.post("/api/debug/action")
async def api_debug_action(payload: Dict[str, Any]):
    """Debug maintenance operations: clear descriptions, discovery, or cover cache."""
    action = payload.get("action")
    if action == "clear-descriptions":
        count = db.clear_all_descriptions()
        return JSONResponse({"status": "success", "message": f"Cleared descriptions across {count} items."})
    elif action == "clear-discovery":
        count = db.clear_discovered_series()
        return JSONResponse({"status": "success", "message": f"Cleared {count} unreviewed series from Discovery."})
    elif action == "clear-thumbnails":
        count = task_manager.cover_cache.clear_cache()
        return JSONResponse({"status": "success", "message": f"Purged {count} cached cover images from disk."})
    elif action == "purge-pdf":
        count = db.purge_pdf_releases()
        db.deduplicate_and_clean_discovery()
        return JSONResponse({"status": "success", "message": f"Purged {count} PDF release(s) and deduplicated discovery series."})
    else:
        return JSONResponse({"status": "error", "message": f"Unknown action: {action}"}, status_code=400)

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
        row = dict(row)

    target_url = row.get("series_link") or row.get("link")
    from app.providers.jnovels import JNovelsProvider

    if not target_url:
        from app.providers.metadata import NovelMetadataProvider
        meta = await asyncio.to_thread(NovelMetadataProvider.fetch_novel_metadata, row.get("series_name") or row.get("title") or "")
        details = {
            "synopsis": meta.get("synopsis") if meta else None,
            "cover_url": meta.get("cover_url") if meta else None,
            "english_title": meta.get("title") if meta else None,
            "series_link": None
        }
    else:
        details = await asyncio.to_thread(
            JNovelsProvider.extract_synopsis_and_original_link, 
            target_url, 
            series_name=row.get("series_name") or row.get("title") or ""
        )

    from app.providers.metadata import NovelMetadataProvider
    new_series_name = NovelMetadataProvider.clean_novel_title(details.get("english_title") or row.get("series_name") or row.get("title") or "")
    new_title = None
    if row.get("title"):
        if any(v in row["title"].lower() for v in ("volume", "vol.", "vol ")):
            from app.parser import extract_series_and_volume
            parsed = extract_series_and_volume(row["title"])
            vol = parsed.get("volume")
            new_title = f"{new_series_name} Vol. {vol}" if vol is not None else new_series_name
        else:
            new_title = new_series_name
    else:
        new_title = new_series_name

    db.update_release_details(
        release_id,
        description=details.get("synopsis"),
        series_link=details.get("series_link"),
        cover_url=details.get("cover_url"),
        series_name=new_series_name,
        title=new_title
    )
    if details.get("cover_url"):
        task_manager.cover_cache.cache_sync(details["cover_url"])

    # Unpack sub-volume releases discovered on hub post (e.g. Infinite Dendrogram Vol 1-23, Cheat Ability)
    hub_vols = details.get("hub_volumes", [])
    if hub_vols:
        import hashlib
        from app.calibre.matcher import CalibreMatcher
        track_official_only = db.get_setting("track_official_only", "true").lower() == "true"
        matcher = None
        try:
            scanner = task_manager.scanner
            if scanner and scanner.metadata_db_exists():
                c_series = scanner.get_all_series()
                c_books = scanner.get_all_books()
                matcher = CalibreMatcher(c_series, c_books)
        except Exception:
            pass

        for hv in hub_vols:
            is_off = hv.get("is_official", True)
            if track_official_only and not is_off:
                continue

            vol_num = hv["volume"]
            vol_disp = int(vol_num) if vol_num.is_integer() else vol_num
            hv_title = f"{new_series_name} Volume {vol_disp} Epub"
            hv_guid = hashlib.sha256(f"{new_series_name}_{vol_num}_{hv['link']}".encode()).hexdigest()[:16]

            st = "NEW"
            matched_id = None
            if matcher:
                m_eval = matcher.check_release({
                    "series_name": new_series_name,
                    "volume": vol_num,
                    "title": hv_title,
                    "is_official": 1 if is_off else 0
                }, auto_request_monitored=True)
                st = m_eval["status"]
                matched_id = m_eval.get("matched_book_id")

            db.upsert_release({
                "guid": hv_guid,
                "title": hv_title,
                "series_name": new_series_name,
                "volume": vol_num,
                "link": hv["link"],
                "series_link": details.get("series_link") or target_url,
                "source_feed_name": row.get("source_feed_name") or "J-Novels Hub",
                "status": st,
                "calibre_matched_book_id": matched_id,
                "is_official": 1 if is_off else 0,
                "cover_url": details.get("cover_url"),
                "description": details.get("synopsis")
            })

    return JSONResponse({
        "status": "success",
        "synopsis": details.get("synopsis"),
        "series_link": details.get("series_link"),
        "cover_url": details.get("cover_url"),
        "series_name": new_series_name,
        "title": new_title or row.get("title")
    })

@app.post("/api/series/{series_id}/fetch-info")
async def api_fetch_series_info(series_id: int):
    """Fetches description and cover image for a tracked series from its website hub."""
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM series_tracking WHERE id = ?;", (series_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Series not found")
        row = dict(row)

    target_url = row.get("website_url")
    if not target_url or "?s=" in target_url:
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT series_link, link FROM releases 
                WHERE lower(series_name) = lower(?) AND series_link IS NOT NULL AND series_link != '' 
                ORDER BY id DESC LIMIT 1;
            """, (row.get("name") or "",))
            rel = cur.fetchone()
            if rel and rel["series_link"]:
                target_url = rel["series_link"]

    from app.providers.jnovels import JNovelsProvider

    if not target_url:
        from app.providers.metadata import NovelMetadataProvider
        meta = await asyncio.to_thread(NovelMetadataProvider.fetch_novel_metadata, row.get("name") or "")
        if not meta or (not meta.get("synopsis") and not meta.get("cover_url")):
            return JSONResponse({"status": "error", "message": "No description found online"}, status_code=404)
        details = {
            "synopsis": meta.get("synopsis"),
            "cover_url": meta.get("cover_url"),
            "series_link": None
        }
    else:
        details = await asyncio.to_thread(
            JNovelsProvider.extract_synopsis_and_original_link, 
            target_url, 
            series_name=row.get("name") or ""
        )

    db.update_series_details(
        series_id,
        description=details.get("synopsis"),
        cover_url=details.get("cover_url"),
        website_url=details.get("series_link") or target_url
    )
    if details.get("cover_url"):
        task_manager.cover_cache.cache_sync(details["cover_url"])

    return JSONResponse({
        "status": "success",
        "description": details.get("synopsis"),
        "cover_url": details.get("cover_url"),
        "website_url": details.get("series_link") or target_url
    })

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)
