import os
import json
import math
import time
import threading
import difflib
import logging
import asyncio
import sqlite3
import tempfile
import zoneinfo
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from app.database import Database
from app import diagnostics
from app.calibre.scanner import CalibreScanner
from app.providers.registry import create_provider
from app.scheduler import TaskManager
from app.covers import CoverCache
from app.security import is_safe_public_url, sanitize_calibre_path
from app.downloader.qbit import QBitClient, qbit_client_from_settings
from app.download_state import release_download_state
from app.torbox_manager import TorBoxManager

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

# TorBox debrid download engine (see .agents/torbox_port/CONTRACT.md)
TORBOX_API_KEY_MASK = "••••••••••••"
DOWNLOADER_ENGINES = ("qbit", "torbox")

# Initialize Database, Cache & Task Manager
db = Database(DB_PATH)
cover_cache = CoverCache(DATA_DIR)
task_manager = TaskManager(db, CALIBRE_DIR)
torbox_manager = TorBoxManager(db)

def get_qbit_client() -> QBitClient:
    """Instantiates QBitClient configured from current app_settings in database."""
    return qbit_client_from_settings(db.get_all_settings())

def _active_downloader_engine() -> str:
    """Resolves the configured download engine, falling back to qBittorrent."""
    engine = str(db.get_setting("downloader_engine", "qbit") or "qbit").strip().lower()
    if engine not in DOWNLOADER_ENGINES:
        logger.warning(f"Unknown downloader_engine '{engine}'; falling back to qbit.")
        return "qbit"
    return engine


def _masked_settings() -> Dict[str, Any]:
    """Returns a copy of the settings with the TorBox API key replaced by the mask token."""
    settings = db.get_all_settings()
    api_key = str(settings.get("torbox_api_key") or "").strip()
    settings["torbox_api_key"] = TORBOX_API_KEY_MASK if api_key else ""
    return settings


def _torbox_api_key_is_set() -> bool:
    return bool(str(db.get_setting("torbox_api_key", "") or "").strip())


def _torbox_last_poll_seconds_ago() -> Optional[int]:
    raw = str(db.get_setting("torbox_last_poll_at", "") or "").strip()
    if not raw:
        return None
    try:
        stamp = float(raw)
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    return max(0, int(time.time() - stamp))


def _torbox_downloads_dir() -> str:
    """Resolves the TorBox fetch directory without failing page rendering."""
    try:
        return torbox_manager.downloads_dir()
    except Exception as exc:
        logger.warning(f"Could not resolve downloads_dir: {exc}")
        return str(db.get_setting("downloads_dir", os.getenv("DOWNLOAD_DIR", "/downloads")) or "/downloads")


def _coerce_int(value: Any) -> Optional[int]:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> Optional[float]:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _clamp_numeric_setting(payload: Dict[str, Any], key: str, low: float, high: float, integer: bool = False) -> None:
    """Clamps one numeric setting in place; unparsable values are dropped (saved value kept)."""
    if key not in payload:
        return
    number = _coerce_int(payload.get(key)) if integer else _coerce_float(payload.get(key))
    if number is None:
        payload.pop(key, None)
        return
    number = max(float(low), min(float(high), float(number)))
    payload[key] = str(int(number)) if number.is_integer() else str(number)


def _validated_downloads_dir(raw_value: Any) -> str:
    """Validates, creates when missing and normalises the local downloads directory."""
    candidate = str(raw_value if raw_value is not None else "").strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="downloads_dir must be an absolute path (received an empty value)")
    if "\x00" in candidate or ".." in candidate:
        raise HTTPException(status_code=400, detail="downloads_dir must not contain NUL bytes or '..' path segments")
    if not os.path.isabs(candidate):
        raise HTTPException(status_code=400, detail=f"downloads_dir must be an absolute path, got '{candidate}'")
    normalized = os.path.normpath(candidate)
    try:
        os.makedirs(normalized, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"downloads_dir '{normalized}' is not usable: {exc}") from exc
    return normalized


def _prepare_settings_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Applies TorBox key masking, engine validation and numeric clamping before persistence."""
    clean = dict(payload or {})

    # A blank or masked API key means "keep the saved key" (never clear it from the UI).
    if "torbox_api_key" in clean:
        supplied = clean.get("torbox_api_key")
        supplied_text = "" if supplied is None else str(supplied).strip()
        if not supplied_text or supplied_text == TORBOX_API_KEY_MASK:
            clean.pop("torbox_api_key", None)

    if "downloader_engine" in clean:
        engine = str(clean.get("downloader_engine") or "").strip().lower()
        if engine not in DOWNLOADER_ENGINES:
            raise HTTPException(status_code=400, detail="downloader_engine must be 'qbit' or 'torbox'")
        clean["downloader_engine"] = engine

    if "app_timezone" in clean:
        timezone_value = str(clean.get("app_timezone") or "").strip()
        if timezone_value:
            try:
                zoneinfo.ZoneInfo(timezone_value)
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "app_timezone must be a valid IANA timezone name "
                        "(e.g. Europe/Copenhagen) or empty"
                    ),
                ) from exc
        clean["app_timezone"] = timezone_value

    if "log_level" in clean:
        level = str(clean.get("log_level") or "").strip().upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise HTTPException(
                status_code=400,
                detail="log_level must be one of DEBUG, INFO, WARNING, ERROR",
            )
        clean["log_level"] = level

    _clamp_numeric_setting(clean, "activity_retention_days", 0, 3650, integer=True)
    _clamp_numeric_setting(clean, "activity_max_rows", 0, 1000000, integer=True)

    for key, low, high in (
        ("torbox_poll_seconds", 5, 3600),
        ("torbox_max_concurrent", 1, 20),
        ("torbox_download_concurrency", 1, 16),
        ("torbox_parallel_chunks", 1, 8),
    ):
        _clamp_numeric_setting(clean, key, low, high, integer=True)

    _clamp_numeric_setting(clean, "torbox_max_download_gb", 1, 10240)
    _clamp_numeric_setting(clean, "torbox_timeout_hours", 0, 168)

    if "downloads_dir" in clean:
        clean["downloads_dir"] = _validated_downloads_dir(clean.get("downloads_dir"))

    return clean


def _result_error_code(result: Any) -> int:
    """Extracts the numeric error code from a TorBox manager result dict (0 when absent)."""
    if not isinstance(result, dict):
        return 0
    try:
        return int(result.get("code") or 0)
    except (TypeError, ValueError):
        return 0


# --- Series identity / manual Calibre matching helpers (see .agents/series_identity/CONTRACT.md) ---

def _series_match_clean(value: Any) -> str:
    """Conservative series-spelling cleanup before normalized comparison."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        from app.series_identity import clean_series_for_matching
        return clean_series_for_matching(text) or text
    except Exception:
        return text


def _series_match_normalize(value: Any) -> str:
    """Normalises a series title/author for the Calibre match picker."""
    text = _series_match_clean(value)
    if not text:
        return ""
    try:
        from app.parser import normalize_text
        return normalize_text(text)
    except Exception:
        return " ".join(text.lower().split())


def _series_row_calibre_id(row: Dict[str, Any]) -> Optional[int]:
    """Extracts the Calibre series id from a scanner or series_tracking row."""
    for key in ("series_id", "calibre_series_id", "id"):
        if row.get(key) is not None:
            parsed = _coerce_int(row.get(key))
            if parsed is not None:
                return parsed
    return None


def _calibre_series_picker_rows() -> List[Dict[str, Any]]:
    """Calibre series snapshot for the match pickers.

    Prefers the TaskManager snapshot (scanner with stale/torn fallback), then a direct
    Calibre scan, then tracked ``series_tracking`` rows that already carry a Calibre id.
    Never raises: an unavailable scanner yields an empty candidate list upstream.
    """
    try:
        snapshot_fn = getattr(task_manager, "_calibre_series_snapshot", None)
        if callable(snapshot_fn):
            rows = snapshot_fn() or []
            if isinstance(rows, list) and rows:
                return [dict(row) for row in rows if isinstance(row, dict)]
    except Exception as exc:
        logger.warning(f"Calibre series snapshot unavailable: {exc}")

    try:
        scanner = CalibreScanner(db.get_setting("calibre_path", CALIBRE_DIR))
        if scanner.is_available():
            rows = scanner.get_all_series() or []
            if rows:
                return [dict(row) for row in rows if isinstance(row, dict)]
    except Exception as exc:
        logger.warning(f"Calibre series scan unavailable: {exc}")

    try:
        return [dict(row) for row in db.get_all_series() or [] if row.get("calibre_series_id")]
    except Exception:
        return []


# Picker snapshot cache (CONTRACT rebind_perf v5 §4): a process-level, short-TTL,
# lock-protected snapshot so repeated search keystrokes (and parallel clients) do not
# rescan the Calibre library / series_tracking on every request. The loader runs while
# holding the lock, which makes a cache miss single-flight instead of a stampede.
SERIES_PICKER_CACHE_TTL_SECONDS = 10.0
_series_picker_cache_lock = threading.Lock()
_series_picker_cache = {"ts": 0.0, "calibre": [], "tracked": []}


def _series_picker_snapshot() -> Dict[str, List[Dict[str, Any]]]:
    """Returns the cached ``{"calibre": [...], "tracked": [...]}`` picker snapshot.

    Double-checked under ``_series_picker_cache_lock``: a fresh snapshot is returned
    without touching Calibre/SQLite, while a cache miss loads both lists once inside
    the lock (single-flight for concurrent keystrokes in worker threads). Never
    raises: a failing loader degrades to an empty list for that section. Callers must
    not mutate the returned rows/lists - the routes build their own entry lists.
    """
    try:
        with _series_picker_cache_lock:
            cached = _series_picker_cache
            age = time.monotonic() - float(cached.get("ts") or 0.0)
            if cached.get("ts") and age < SERIES_PICKER_CACHE_TTL_SECONDS:
                return {
                    "calibre": list(cached.get("calibre") or []),
                    "tracked": list(cached.get("tracked") or []),
                }

            calibre: List[Dict[str, Any]] = []
            tracked: List[Dict[str, Any]] = []
            try:
                calibre = [
                    dict(row) for row in (_calibre_series_picker_rows() or [])
                    if isinstance(row, dict)
                ]
            except Exception as exc:
                logger.warning(f"Series picker snapshot: Calibre series unavailable: {exc}")
                calibre = []
            try:
                tracked = [
                    dict(row) for row in (db.get_all_series() or [])
                    if isinstance(row, dict)
                ]
            except Exception as exc:
                logger.warning(f"Series picker snapshot: tracked series unavailable: {exc}")
                tracked = []

            cached["ts"] = time.monotonic()
            cached["calibre"] = calibre
            cached["tracked"] = tracked
            return {"calibre": list(calibre), "tracked": list(tracked)}
    except Exception as exc:  # final containment: pickers must never 500
        logger.warning(f"Series picker snapshot failed: {exc}")
        return {"calibre": [], "tracked": []}


def _invalidate_series_picker_cache() -> None:
    """Drops the picker snapshot so the next picker request rescans (never raises)."""
    try:
        with _series_picker_cache_lock:
            _series_picker_cache["ts"] = 0.0
            _series_picker_cache["calibre"] = []
            _series_picker_cache["tracked"] = []
    except Exception as exc:
        logger.warning(f"Could not invalidate the series picker cache: {exc}")


def _series_picker_entry(
    row: Dict[str, Any],
    tracked_by_name: Dict[str, Dict[str, Any]],
    tracked_by_calibre_id: Dict[int, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Normalises a scanner/tracker row into the picker payload shape."""
    if not isinstance(row, dict):
        return None
    calibre_id = _series_row_calibre_id(row)
    name = str(row.get("series_name") or row.get("name") or "").strip()
    if calibre_id is None or not name:
        return None
    tracked_row = tracked_by_calibre_id.get(calibre_id) or tracked_by_name.get(_series_match_normalize(name))
    display_title = str(row.get("display_title") or "").strip()
    if not display_title and tracked_row:
        display_title = str(tracked_row.get("display_title") or "").strip()
    return {
        "id": calibre_id,
        "name": name,
        "display_title": display_title,
        "authors": str(row.get("authors") or row.get("author") or "").strip(),
        "book_count": _coerce_int(row.get("book_count")) or 0,
        "tracked": bool(tracked_row),
        "score": 0.0,
    }


def _rank_series_picker_entries(entries: List[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
    """Ranks picker entries: exact match first, then substring, then SequenceMatcher score."""
    source_norm = _series_match_normalize(source)
    if not source_norm:
        for entry in entries:
            entry["score"] = 0.0
        entries.sort(key=lambda item: (item.get("name") or "").lower())
        return entries

    def _score(entry: Dict[str, Any]) -> tuple:
        candidates = [
            norm for norm in (
                _series_match_normalize(entry.get("name")),
                _series_match_normalize(entry.get("display_title")),
            ) if norm
        ]
        best = 0.0
        is_exact = False
        is_substring = False
        for candidate in candidates:
            if candidate == source_norm:
                is_exact = True
                best = 1.0
            elif source_norm in candidate or candidate in source_norm:
                is_substring = True
                best = max(best, 0.95)
            best = max(best, difflib.SequenceMatcher(None, source_norm, candidate).ratio())
        entry["score"] = round(float(best), 4)
        return (not is_exact, not is_substring, -best, (entry.get("name") or "").lower())

    entries.sort(key=_score)
    return entries


def _find_series_row(series_id: int) -> Optional[Dict[str, Any]]:
    """Looks up a series_tracking row by primary key."""
    target = _coerce_int(series_id)
    if target is None:
        return None
    try:
        for row in db.get_all_series() or []:
            if _coerce_int(row.get("id")) == target:
                return dict(row)
    except Exception as exc:
        logger.warning(f"Could not read series #{series_id}: {exc}")
    return None


def _calibre_series_by_id(calibre_series_id: int) -> Optional[Dict[str, Any]]:
    """Builds a Calibre series dict for a manual match from the snapshot or tracker row."""
    for row in _calibre_series_picker_rows():
        if _series_row_calibre_id(row) != calibre_series_id:
            continue
        name = str(row.get("series_name") or row.get("name") or "").strip()
        return {
            "id": calibre_series_id,
            "series_id": calibre_series_id,
            "name": name,
            "series_name": name,
            "display_title": str(row.get("display_title") or "").strip(),
            "authors": str(row.get("authors") or row.get("author") or "").strip(),
            "book_count": _coerce_int(row.get("book_count")) or 0,
            "latest_index": _coerce_float(row.get("latest_index")) or 0.0,
        }

    lookup = getattr(db, "get_series_by_calibre_id", None)
    if callable(lookup):
        try:
            tracker = lookup(calibre_series_id)
        except Exception:
            tracker = None
        if tracker:
            return dict(tracker)
    return None


def _series_match_payload(result: Any) -> Dict[str, Any]:
    """Normalises a TaskManager manual-match summary for the JSON response."""
    payload = dict(result) if isinstance(result, dict) else {}
    payload.setdefault("status", "success")
    payload.setdefault("reconciled", {})
    return payload


# --- AniList publication metadata for the series page (CONTRACT volume_inference §5) ---

_LIGHT_NOVEL_FORMAT_TOKENS = ("NOVEL", "LIGHT_NOVEL")
_PUBLICATION_FORMAT_LABELS = {
    "NOVEL": "Light Novel",
    "LIGHT_NOVEL": "Light Novel",
    "MANGA": "Manga",
    "ONE_SHOT": "One Shot",
    "TV": "TV",
    "MOVIE": "Movie",
    "OVA": "OVA",
    "ONA": "ONA",
    "SPECIAL": "Special",
    "MUSIC": "Music",
}
_PUBLICATION_STATUS_LABELS = {
    "FINISHED": "Finished",
    "RELEASING": "Releasing",
    "NOT_YET_RELEASED": "Not Yet Released",
    "HIATUS": "Hiatus",
    "CANCELLED": "Cancelled",
}


def _publication_token(value: Any) -> str:
    """Canonical upper-case token for a raw AniList format/status value."""
    text = str(value or "").strip()
    if not text:
        return ""
    return "_".join(text.upper().replace("-", " ").replace("_", " ").split())


def _publication_int(value: Any) -> Optional[int]:
    """Coerces a publication count to a finite non-negative int (else None)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    try:
        count = int(number)
    except (OverflowError, ValueError):
        return None
    return count if count >= 0 else None


def _publication_label(token: str, labels: Dict[str, str]) -> str:
    """Maps a canonical token to its display label, prettifying unknown values."""
    if not token:
        return ""
    if token in labels:
        return labels[token]
    return " ".join(part.capitalize() for part in token.split("_") if part)


def _publication_meta_mapping(raw_meta: Any) -> Dict[str, Any]:
    """Coerces a dict / JSON string / cached metadata row to a metadata mapping."""
    if isinstance(raw_meta, dict):
        return dict(raw_meta)
    if isinstance(raw_meta, str):
        text = raw_meta.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _series_publication_fields(raw_meta: Any) -> Dict[str, Any]:
    """Normalised AniList publication fields for a series row; never raises.

    Accepts a raw metadata dict, an ``anilist_meta_json`` string (or None) and
    returns the series-page keys (``anilist_format``/``anilist_format_label``/
    ``is_light_novel``/``anilist_status``/``anilist_status_label``/
    ``total_volumes``/``anilist_chapters``). Prefers the provider-side
    normaliser when the metadata module exposes ``publication_from_meta``; the
    local parser fills in anything the provider could not answer.
    """
    fields: Dict[str, Any] = {
        "anilist_format": "",
        "anilist_format_label": "",
        "is_light_novel": False,
        "anilist_status": "",
        "anilist_status_label": "",
        "total_volumes": None,
        "anilist_chapters": None,
    }
    try:
        meta = _publication_meta_mapping(raw_meta)
        if not meta:
            return fields

        provider_data: Dict[str, Any] = {}
        try:
            from app.providers.metadata import NovelMetadataProvider
            normalizer = getattr(NovelMetadataProvider, "publication_from_meta", None)
            if callable(normalizer):
                candidate = normalizer(meta)
                if isinstance(candidate, dict):
                    provider_data = candidate
        except Exception:
            provider_data = {}

        raw_format = _publication_token(provider_data.get("format") or meta.get("format"))
        raw_status = _publication_token(provider_data.get("status") or meta.get("status"))

        format_label = str(
            provider_data.get("format_label") or meta.get("format_label") or ""
        ).strip()
        if not format_label:
            format_label = _publication_label(raw_format, _PUBLICATION_FORMAT_LABELS)

        status_label = str(
            provider_data.get("status_label") or meta.get("status_label") or ""
        ).strip()
        if not status_label:
            status_label = _publication_label(raw_status, _PUBLICATION_STATUS_LABELS)

        is_light_novel = provider_data.get("is_light_novel")
        if not isinstance(is_light_novel, bool):
            raw_flag = meta.get("is_light_novel")
            if isinstance(raw_flag, bool):
                is_light_novel = raw_flag
            elif raw_flag is not None:
                is_light_novel = str(raw_flag).strip().lower() in ("1", "true", "yes", "on")
            else:
                is_light_novel = raw_format in _LIGHT_NOVEL_FORMAT_TOKENS

        total_volumes = _publication_int(provider_data.get("total_volumes"))
        if total_volumes is None:
            total_volumes = _publication_int(provider_data.get("volumes"))
        if total_volumes is None:
            total_volumes = _publication_int(meta.get("total_volumes"))
        if total_volumes is None:
            total_volumes = _publication_int(meta.get("volumes"))

        chapters = _publication_int(provider_data.get("chapters"))
        if chapters is None:
            chapters = _publication_int(meta.get("chapters"))

        fields.update({
            "anilist_format": raw_format,
            "anilist_format_label": format_label,
            "is_light_novel": bool(is_light_novel),
            "anilist_status": raw_status,
            "anilist_status_label": status_label,
            "total_volumes": total_volumes,
            "anilist_chapters": chapters,
        })
    except Exception:
        return fields
    return fields


# --- Release re-bind / series identity reset helpers (CONTRACT_V2_REBIND §4) ---

def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Parses JSON booleans plus the usual 'true'/'1'/'yes' string spellings."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def _coerce_volume_list(value: Any) -> List[Any]:
    """Parses a tracked row's owned volumes (list or JSON string) without raising."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "[]")
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _release_row(release_id: int) -> Optional[Dict[str, Any]]:
    """Reads a release row for existence checks; never raises."""
    try:
        row = db.get_release_by_id(release_id)
    except Exception as exc:
        logger.warning(f"Could not read release #{release_id}: {exc}")
        return None
    return dict(row) if row else None


def _tracked_series_by_name(name: str) -> Optional[Dict[str, Any]]:
    """Exact (normalized) tracked-series lookup used by re-bind target resolution."""
    target_norm = _series_match_normalize(name)
    if not target_norm:
        return None
    try:
        for row in db.get_all_series() or []:
            if _series_match_normalize(row.get("name")) == target_norm:
                return dict(row)
    except Exception as exc:
        logger.warning(f"Tracked series lookup failed for '{name}': {exc}")
    return None


# --- Diagnostics / maintenance helpers (CONTRACT_V2_REBIND §6) ---

def _utc_iso_now() -> str:
    """UTC ISO-8601 timestamp for diagnostics/maintenance payloads."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _apply_configured_log_level() -> None:
    """Applies the persisted log_level setting to the root logger at startup."""
    try:
        level_name = str(db.get_setting("log_level", "INFO") or "INFO").strip().upper()
        level = getattr(logging, level_name, None)
        if isinstance(level, int):
            logging.getLogger().setLevel(level)
            logger.info(f"Root log level set to {level_name}.")
    except Exception as exc:
        logger.warning(f"Could not apply the configured log level: {exc}")


def _create_database_backup(db_path: str) -> tuple:
    """Creates a consistent SQLite snapshot inside DATA_DIR; returns (path, timestamp)."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    fd, backup_path = tempfile.mkstemp(prefix=".noveltrax-backup-", suffix=".db", dir=DATA_DIR)
    os.close(fd)
    try:
        os.remove(backup_path)
        # Prefer the database layer's tested online-backup helper; fall back to the
        # sqlite3 backup API directly on the configured database path.
        backup_fn = getattr(db, "backup_database", None)
        if callable(backup_fn):
            backup_fn(backup_path)
        else:
            source = sqlite3.connect(db_path, timeout=10.0)
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
                source.close()
    except Exception:
        try:
            os.remove(backup_path)
        except OSError:
            pass
        raise
    return backup_path, stamp


from app.providers.metadata import NovelMetadataProvider
NovelMetadataProvider.set_db(db)

# Lifespan Context Manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    _apply_configured_log_level()
    logger.info("Starting NovelTrax application...")
    task_manager.start()
    torbox_manager.start()
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
    torbox_manager.stop()
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
    # Enrich every tracked series with the persisted AniList publication metadata
    # (Format / Status / Volumes) for the series-page card. Keys already present on
    # the row win, so cached/enriched rows are never overwritten.
    for series in series_list:
        if not isinstance(series, dict):
            continue
        raw_meta = series.get("anilist_meta_json")
        if raw_meta in (None, ""):
            raw_meta = series.get("anilist_meta")
        publication = _series_publication_fields(raw_meta)
        for key, value in publication.items():
            series.setdefault(key, value)
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
    settings = _masked_settings()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "active_page": "settings",
            "settings": settings,
            "downloader_engine": _active_downloader_engine(),
            "torbox_api_key_set": _torbox_api_key_is_set()
        }
    )

@app.get("/downloads", response_class=HTMLResponse)
async def downloads_view(request: Request):
    """Renders Downloader & Queue Management page with live status and active torrents."""
    engine = _active_downloader_engine()
    settings = _masked_settings()

    if engine == "torbox":
        status_payload = await asyncio.to_thread(torbox_manager.build_status, 1.2)
        torrents = status_payload.get("torrents") or []
        return templates.TemplateResponse(
            request=request,
            name="downloads.html",
            context={
                "active_page": "downloads",
                "engine": engine,
                "downloader_engine": engine,
                "torrents": torrents,
                "torbox_status": status_payload.get("downloader") or {},
                "torbox_last_checked_seconds_ago": _torbox_last_poll_seconds_ago(),
                "settings": settings,
                "download_dir": _torbox_downloads_dir()
            }
        )

    qbit = get_qbit_client()
    status = await asyncio.to_thread(qbit.test_connection, timeout=2.0)
    torrents = await asyncio.to_thread(qbit.get_torrents, category=qbit.category) if status.get("success") else []

    return templates.TemplateResponse(
        request=request,
        name="downloads.html",
        context={
            "active_page": "downloads",
            "engine": engine,
            "downloader_engine": engine,
            "qbit_status": status,
            "torrents": torrents,
            "settings": settings,
            "download_dir": settings.get("qbit_savepath", "/downloads")
        }
    )

@app.get("/audiobooks", response_class=HTMLResponse)
async def audiobooks_view(request: Request):
    """Renders the dedicated Audiobooks page for light-novel audio releases."""
    book_rows = db.get_audiobook_releases(limit=200)
    total_audiobooks = db.count_audiobook_releases()
    settings = db.get_all_settings()

    return templates.TemplateResponse(
        request=request,
        name="audiobooks.html",
        context={
            "active_page": "audiobooks",
            "book_rows": book_rows,
            "total_audiobooks": total_audiobooks,
            "settings": settings
        }
    )

# --- API Endpoints ---

@app.post("/api/calibre/sync")
async def api_calibre_sync():
    """Trigger manual Calibre library rescan and request resolution."""
    _invalidate_series_picker_cache()
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

# --- Series identity: manual Calibre matching, AniList refresh and aliases ---

@app.get("/api/calibre/series")
async def api_calibre_series_picker(
    search: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(25, ge=1, le=200)
):
    """Searchable, similarity-ranked Calibre series picker used by the manual match modal."""
    try:
        snapshot = await asyncio.to_thread(_series_picker_snapshot)
        raw_rows = snapshot.get("calibre") or []
        tracked_rows = snapshot.get("tracked") or []
    except Exception as exc:
        logger.warning(f"Calibre series picker could not load series: {exc}")
        return JSONResponse({"status": "success", "series": [], "count": 0})

    try:
        tracked_by_name: Dict[str, Dict[str, Any]] = {}
        tracked_by_calibre_id: Dict[int, Dict[str, Any]] = {}
        for row in tracked_rows or []:
            if not isinstance(row, dict):
                continue
            norm = _series_match_normalize(row.get("name"))
            if norm:
                tracked_by_name.setdefault(norm, row)
            calibre_id = _coerce_int(row.get("calibre_series_id"))
            if calibre_id is not None:
                tracked_by_calibre_id.setdefault(calibre_id, row)

        entries: List[Dict[str, Any]] = []
        seen_ids = set()
        for row in raw_rows or []:
            entry = _series_picker_entry(row, tracked_by_name, tracked_by_calibre_id)
            if entry is None or entry["id"] in seen_ids:
                continue
            seen_ids.add(entry["id"])
            entries.append(entry)

        search_norm = _series_match_normalize(search)
        if search_norm:
            entries = [
                entry for entry in entries
                if search_norm in _series_match_normalize(entry.get("name"))
                or search_norm in _series_match_normalize(entry.get("display_title"))
                or search_norm in _series_match_normalize(entry.get("authors"))
            ]

        entries = _rank_series_picker_entries(entries, source or "")
        payload = entries[:limit]
        return JSONResponse({"status": "success", "series": payload, "count": len(payload)})
    except Exception as exc:
        logger.warning(f"Calibre series picker failed: {exc}")
        return JSONResponse({"status": "success", "series": [], "count": 0})

@app.post("/api/series/{series_id}/match-calibre")
async def api_match_series_to_calibre(series_id: int, payload: Optional[Dict[str, Any]] = None):
    """Manually links a tracked series to a Calibre series picked from the modal."""
    series_row = await asyncio.to_thread(_find_series_row, series_id)
    if not series_row:
        raise HTTPException(status_code=404, detail="Series not found")

    p = payload or {}
    calibre_series_id = _coerce_int(p.get("calibre_series_id"))
    if calibre_series_id is None:
        raise HTTPException(status_code=400, detail="calibre_series_id is required")

    calibre_row = await asyncio.to_thread(_calibre_series_by_id, calibre_series_id)
    if not calibre_row:
        raise HTTPException(status_code=400, detail=f"Unknown Calibre series id: {calibre_series_id}")

    source_name = str(series_row.get("name") or "").strip()
    try:
        result = await asyncio.to_thread(
            task_manager.match_series_to_calibre, source_name, calibre_row, actor="user"
        )
    except Exception as exc:
        logger.warning(f"Manual Calibre match failed for '{source_name}': {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Could not match series: {exc}") from exc
    _invalidate_series_picker_cache()
    return JSONResponse(_series_match_payload(result))

@app.post("/api/discovery/match")
async def api_discovery_match(payload: Optional[Dict[str, Any]] = None):
    """Links a discovered (untracked) series to a Calibre series and saves the alias."""
    p = payload or {}
    source_name = str(p.get("series_name") or "").strip()
    if not source_name:
        raise HTTPException(status_code=400, detail="series_name is required")
    calibre_series_id = _coerce_int(p.get("calibre_series_id"))
    if calibre_series_id is None:
        raise HTTPException(status_code=400, detail="calibre_series_id is required")

    calibre_row = await asyncio.to_thread(_calibre_series_by_id, calibre_series_id)
    if not calibre_row:
        raise HTTPException(status_code=400, detail=f"Unknown Calibre series id: {calibre_series_id}")

    match_kwargs: Dict[str, Any] = {"actor": "user"}
    release_id = _coerce_int(p.get("release_id"))
    if release_id is not None:
        match_kwargs["release_id"] = release_id
    try:
        result = await asyncio.to_thread(
            task_manager.match_series_to_calibre, source_name, calibre_row, **match_kwargs
        )
    except Exception as exc:
        logger.warning(f"Discovery Calibre match failed for '{source_name}': {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Could not match series: {exc}") from exc
    _invalidate_series_picker_cache()
    return JSONResponse(_series_match_payload(result))

@app.post("/api/series/{series_id}/refresh-metadata")
async def api_refresh_series_metadata(series_id: int):
    """Fetches fresh AniList metadata (display title, synonyms) and reconciles the series."""
    series_row = await asyncio.to_thread(_find_series_row, series_id)
    if not series_row:
        raise HTTPException(status_code=404, detail="Series not found")

    try:
        result = await asyncio.to_thread(task_manager.refresh_series_metadata, series_id)
    except Exception as exc:
        logger.warning(f"AniList refresh failed for series #{series_id}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Could not refresh series metadata: {exc}") from exc

    payload = dict(result) if isinstance(result, dict) else {}
    payload.setdefault("status", "success")
    payload.setdefault("display_title", str(series_row.get("display_title") or ""))
    payload.setdefault("anilist_id", series_row.get("anilist_id"))
    payload.setdefault("linked", bool(series_row.get("calibre_series_id")))
    payload.setdefault("reconciled", {})
    payload.setdefault("message", "")
    return JSONResponse(payload)

@app.get("/api/series/aliases")
async def api_get_series_aliases():
    """Lists persistent series alias overrides (manual picks + AniList-derived titles)."""
    try:
        aliases = await asyncio.to_thread(db.get_series_alias_overrides)
    except Exception as exc:
        logger.warning(f"Could not read series alias overrides: {exc}")
        aliases = []
    return JSONResponse({"status": "success", "aliases": aliases or []})

@app.delete("/api/series/aliases/{override_id}")
async def api_delete_series_alias(override_id: int):
    """Deletes a persistent series alias override."""
    try:
        deleted = await asyncio.to_thread(db.delete_series_alias_override, override_id)
    except Exception as exc:
        logger.warning(f"Could not delete series alias override #{override_id}: {exc}")
        raise HTTPException(status_code=500, detail="Could not delete the alias override") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Alias override not found")
    _invalidate_series_picker_cache()
    return JSONResponse({"status": "success", "deleted": True})


@app.get("/api/series/{series_id}/management")
async def api_series_management(series_id: int):
    """Series detail panel: registered releases, aliases and AniList titles."""
    try:
        result = await asyncio.to_thread(db.get_series_management, series_id)
    except Exception as exc:
        logger.warning(f"Could not load series #{series_id} management: {exc}")
        raise HTTPException(status_code=500, detail="Could not load series details") from exc
    if not result:
        raise HTTPException(status_code=404, detail="Series not found")
    return JSONResponse({"status": "success", **result})


@app.post("/api/series/{series_id}/identity")
async def api_update_series_identity(
    series_id: int, payload: Optional[Dict[str, Any]] = None
):
    """Edits the cosmetic display title and clears a wrong AniList match."""
    p = payload or {}
    display_title = p.get("display_title") if "display_title" in p else None
    if display_title is not None and not isinstance(display_title, str):
        raise HTTPException(status_code=400, detail="display_title must be text")
    clear_anilist = _coerce_bool(p.get("clear_anilist"), False)
    anilist_locked = p.get("anilist_locked")
    if anilist_locked is not None:
        anilist_locked = _coerce_bool(anilist_locked, False)
    try:
        row = await asyncio.to_thread(
            db.update_series_display_identity,
            series_id,
            display_title,
            clear_anilist,
            anilist_locked,
            "user",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning(f"Could not update series #{series_id} identity: {exc}")
        raise HTTPException(status_code=500, detail="Could not update the series identity") from exc
    if not row:
        raise HTTPException(status_code=404, detail="Series not found")
    try:
        await asyncio.to_thread(
            task_manager.invalidate_series_identity_cache,
            row.get("name"),
            row.get("display_title"),
        )
    except Exception:
        pass
    _invalidate_series_picker_cache()
    return JSONResponse({
        "status": "success",
        "message": "Series identity updated",
        "series": {
            "id": row.get("id"),
            "name": row.get("name"),
            "display_title": row.get("display_title") or "",
            "calibre_series_id": row.get("calibre_series_id"),
            "book_count": row.get("book_count"),
            "latest_index": row.get("latest_index"),
        },
    })


@app.post("/api/series/{series_id}/anilist")
async def api_map_series_anilist(
    series_id: int, payload: Optional[Dict[str, Any]] = None
):
    """Pins one series to an exact AniList entry from a URL or numeric id."""
    from app.providers.metadata import NovelMetadataProvider

    p = payload or {}
    raw_value = (
        p.get("url_or_id")
        or p.get("anilist_url")
        or p.get("url")
        or p.get("anilist_id")
    )
    anilist_id = NovelMetadataProvider.parse_anilist_id(raw_value)
    if anilist_id is None:
        raise HTTPException(
            status_code=400,
            detail="Paste an AniList manga URL or numeric AniList id",
        )
    series_row = await asyncio.to_thread(_find_series_row, series_id)
    if not series_row:
        raise HTTPException(status_code=404, detail="Series not found")
    try:
        meta = await asyncio.to_thread(
            NovelMetadataProvider.query_anilist_by_id, anilist_id
        )
    except Exception as exc:
        logger.warning(f"AniList id lookup failed for #{anilist_id}: {exc}")
        meta = None
    if not meta:
        raise HTTPException(
            status_code=404,
            detail=f"AniList entry #{anilist_id} was not found as a light novel",
        )
    try:
        row = await asyncio.to_thread(
            db.map_series_anilist, series_id, meta, "user"
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning(f"Could not map series #{series_id} to AniList #{anilist_id}: {exc}")
        raise HTTPException(status_code=500, detail="Could not map the AniList entry") from exc
    if not row:
        raise HTTPException(status_code=404, detail="Series not found")
    try:
        await asyncio.to_thread(
            task_manager.invalidate_series_identity_cache,
            row.get("name"),
            row.get("display_title"),
            meta.get("title"),
            meta.get("english_title"),
        )
    except Exception:
        pass
    _invalidate_series_picker_cache()
    return JSONResponse({
        "status": "success",
        "message": f"Mapped to AniList #{anilist_id}",
        "anilist": {
            "id": anilist_id,
            "title": meta.get("title"),
            "english_title": meta.get("english_title"),
            "display_title": row.get("display_title"),
            "synonyms": meta.get("synonyms") or [],
        },
        "series": {
            "id": row.get("id"),
            "name": row.get("name"),
            "display_title": row.get("display_title") or "",
            "anilist_id": row.get("anilist_id"),
            "anilist_locked": bool(_coerce_int(row.get("anilist_locked"))),
            "calibre_series_id": row.get("calibre_series_id"),
            "book_count": row.get("book_count"),
            "latest_index": row.get("latest_index"),
        },
    })


@app.post("/api/series/{series_id}/synonyms/spin-out")
async def api_spin_out_series_synonym(
    series_id: int, payload: Optional[Dict[str, Any]] = None
):
    """Splits one registered synonym into its own tracked series."""
    p = payload or {}
    alias_id = _coerce_int(p.get("alias_id"))
    source_name = str(p.get("source_name") or "").strip()
    new_name = str(p.get("new_name") or "").strip()
    move_matching = _coerce_bool(p.get("move_matching_releases"), True)
    try:
        result = await asyncio.to_thread(
            db.spin_out_series_synonym,
            series_id,
            source_name or None,
            new_name or None,
            alias_id,
            move_matching,
            "user",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning(f"Could not spin out synonym for series #{series_id}: {exc}")
        raise HTTPException(status_code=500, detail="Could not split the synonym") from exc
    _invalidate_series_picker_cache()
    return JSONResponse(result)


@app.post("/api/series/{series_id}/aliases/{alias_id}/retarget")
async def api_retarget_series_alias(
    series_id: int, alias_id: int, payload: Optional[Dict[str, Any]] = None
):
    """Re-maps one registered synonym to another tracked or Calibre series."""
    p = payload or {}
    target_name = str(p.get("target_name") or "").strip() or None
    calibre_series_id = _coerce_int(p.get("calibre_series_id"))
    move_matching = _coerce_bool(p.get("move_matching_releases"), False)
    try:
        result = await asyncio.to_thread(
            db.retarget_series_alias,
            series_id,
            alias_id,
            target_name,
            calibre_series_id,
            move_matching,
            "user",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning(
            f"Could not retarget alias #{alias_id} for series #{series_id}: {exc}"
        )
        raise HTTPException(status_code=500, detail="Could not re-map the synonym") from exc
    _invalidate_series_picker_cache()
    return JSONResponse(result)

# --- Release re-bind / per-series identity reset ---

@app.get("/api/series")
async def api_tracked_series_picker(
    search: Optional[str] = Query(None),
    limit: int = Query(25, ge=1, le=500)
):
    """Searchable tracked-series picker for the release re-bind modal; never raises."""
    try:
        snapshot = await asyncio.to_thread(_series_picker_snapshot)
        rows = snapshot.get("tracked") or []
    except Exception as exc:
        logger.warning(f"Tracked series picker could not load series: {exc}")
        return JSONResponse({"status": "success", "series": [], "count": 0})

    try:
        search_norm = _series_match_normalize(search)
        entries: List[Dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            series_id = _coerce_int(row.get("id"))
            name = str(row.get("name") or "").strip()
            if series_id is None or not name:
                continue
            display_title = str(row.get("display_title") or "").strip()
            authors = str(row.get("authors") or "").strip()
            if search_norm:
                haystack = " ".join(
                    _series_match_normalize(part) for part in (name, display_title, authors)
                )
                if search_norm not in haystack:
                    continue
            name_norm = _series_match_normalize(name)
            entries.append({
                "id": series_id,
                "name": name,
                "display_title": display_title,
                "authors": authors,
                "calibre_series_id": _coerce_int(row.get("calibre_series_id")),
                "identity_locked": _coerce_bool(row.get("identity_locked"), False),
                "book_count": _coerce_int(row.get("book_count")) or 0,
                "owned_volumes": _coerce_volume_list(row.get("owned_volumes_json")),
                "tracked": True,
                "_exact": 1 if search_norm and name_norm == search_norm else 0,
                "_prefix": 1 if search_norm and name_norm.startswith(search_norm) else 0,
            })
        entries.sort(key=lambda item: (-item["_exact"], -item["_prefix"], item["name"].lower()))
        for entry in entries:
            entry.pop("_exact", None)
            entry.pop("_prefix", None)
        payload = entries[:limit]
        return JSONResponse({"status": "success", "series": payload, "count": len(payload)})
    except Exception as exc:
        logger.warning(f"Tracked series picker failed: {exc}")
        return JSONResponse({"status": "success", "series": [], "count": 0})

@app.get("/api/series/identity")
async def api_series_identity_overview(
    search: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500)
):
    """Tracked-series identity overview (display/lock/alias/pin state) for Settings."""
    try:
        series = await asyncio.to_thread(db.get_series_identity_overview, search=search, limit=limit)
    except Exception as exc:
        logger.warning(f"Series identity overview failed: {exc}")
        series = []
    rows = [dict(row) for row in series or [] if isinstance(row, dict)]
    return JSONResponse({"status": "success", "series": rows, "count": len(rows)})

@app.post("/api/releases/{release_id}/rebind")
async def api_rebind_release(release_id: int, payload: Optional[Dict[str, Any]] = None):
    """Re-binds a release to an existing series or re-indexes it under a new title."""
    release = await asyncio.to_thread(_release_row, release_id)
    if not release:
        raise HTTPException(status_code=404, detail="Release not found")

    p = payload or {}
    mode = str(p.get("mode") or "").strip().lower()
    if mode == "different_series":
        mode = "new_title"
    if mode not in ("bind", "new_title", "detach"):
        raise HTTPException(status_code=400, detail="mode must be one of: bind, new_title, detach")

    target_name = str(p.get("target_name") or "").strip() or None
    calibre_series_id = _coerce_int(p.get("calibre_series_id"))
    remember = _coerce_bool(p.get("remember"), False)

    if mode == "bind":
        if calibre_series_id is not None:
            calibre_row = await asyncio.to_thread(_calibre_series_by_id, calibre_series_id)
            if not calibre_row:
                raise HTTPException(status_code=400, detail=f"Unknown Calibre series id: {calibre_series_id}")
            resolved_name = str(calibre_row.get("series_name") or calibre_row.get("name") or "").strip()
            if resolved_name:
                target_name = resolved_name
        elif target_name:
            tracked = await asyncio.to_thread(_tracked_series_by_name, target_name)
            if tracked:
                target_name = str(tracked.get("name") or target_name).strip()
                tracked_calibre_id = _coerce_int(tracked.get("calibre_series_id"))
                if tracked_calibre_id is not None:
                    calibre_series_id = tracked_calibre_id
        else:
            raise HTTPException(status_code=400, detail="mode=bind requires calibre_series_id or target_name")

    try:
        result = await asyncio.to_thread(
            task_manager.rebind_release,
            release_id,
            mode,
            target_name=target_name,
            calibre_series_id=calibre_series_id,
            remember=remember,
            actor="user",
        )
    except (ValueError, KeyError, LookupError) as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Invalid re-bind request") from exc
    except Exception as exc:
        logger.warning(f"Release re-bind failed for #{release_id}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Could not re-bind release: {exc}") from exc

    _invalidate_series_picker_cache()
    summary = dict(result) if isinstance(result, dict) else {}
    summary.setdefault("status", "success")
    return JSONResponse(summary)

@app.get("/api/releases/{release_id}/series-identity")
async def api_release_series_identity(release_id: int):
    """Release identity payload (current series/pin state) used by the re-bind modal."""
    try:
        identity = await asyncio.to_thread(db.get_release_series_identity, release_id)
    except Exception as exc:
        logger.warning(f"Could not read series identity for release #{release_id}: {exc}")
        raise HTTPException(status_code=500, detail="Could not load the release identity") from exc
    if not identity:
        raise HTTPException(status_code=404, detail="Release not found")
    payload = dict(identity)
    # The identity row's own "status" is the release status; the API envelope status
    # must stay "success" so the modal's success check works. Keep both.
    payload["release_status"] = payload.pop("status", "")
    payload["status"] = "success"
    return JSONResponse(payload)

@app.post("/api/releases/{release_id}/unpin")
async def api_unpin_release_series(release_id: int):
    """Removes the manual re-bind pin from one release so automatic matching may move it again."""
    release = await asyncio.to_thread(_release_row, release_id)
    if not release:
        raise HTTPException(status_code=404, detail="Release not found")
    try:
        unpinned = await asyncio.to_thread(db.unpin_release_series, release_id)
    except Exception as exc:
        logger.warning(f"Could not unpin release #{release_id}: {exc}")
        raise HTTPException(status_code=500, detail="Could not unpin the release") from exc
    if not unpinned:
        raise HTTPException(status_code=404, detail="Release is not pinned")
    return JSONResponse({"status": "success", "unpinned": True})

@app.post("/api/series/{series_id}/reset-identity")
async def api_reset_series_identity(series_id: int):
    """Forgets manual/learned identity state for one series so auto-matching can resume."""
    series_row = await asyncio.to_thread(_find_series_row, series_id)
    if not series_row:
        raise HTTPException(status_code=404, detail="Series not found")
    try:
        result = await asyncio.to_thread(task_manager.reset_series_identity, series_id)
    except (KeyError, LookupError) as exc:
        raise HTTPException(status_code=404, detail=str(exc) or "Series not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Invalid reset request") from exc
    except Exception as exc:
        logger.warning(f"Series identity reset failed for #{series_id}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Could not reset series identity: {exc}") from exc

    _invalidate_series_picker_cache()
    summary = dict(result) if isinstance(result, dict) else {}
    payload = {
        "status": summary.get("status") or "success",
        "series_id": summary.get("series_id", series_id),
        "series_name": summary.get("series_name", series_row.get("name") or ""),
        "aliases_removed": summary.get("aliases_removed", 0),
        "releases_unpinned": summary.get("releases_unpinned", 0),
        "display_cleared": summary.get("display_cleared", False),
        "locked_cleared": summary.get("locked_cleared", False),
        "reconciled": summary.get("reconciled") or {},
        "linked": summary.get("linked"),
        "message": summary.get("message", ""),
    }
    return JSONResponse(payload)

# --- Calibre false-ownership repair (CONTRACT calibre_repair v4 §5) ---

@app.post("/api/series/{series_id}/unlink-calibre")
async def api_unlink_series_calibre(series_id: int):
    """Removes the tracker's Calibre link and resets its false In-Calibre releases."""
    series_row = await asyncio.to_thread(_find_series_row, series_id)
    if not series_row:
        raise HTTPException(status_code=404, detail="Series not found")
    try:
        result = await asyncio.to_thread(task_manager.unlink_series_calibre, series_id)
    except (KeyError, LookupError) as exc:
        raise HTTPException(status_code=404, detail=str(exc) or "Series not found") from exc
    except Exception as exc:
        logger.warning(f"Calibre unlink failed for series #{series_id}: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail=f"Could not unlink the series from Calibre: {exc}",
        ) from exc

    _invalidate_series_picker_cache()
    payload = dict(result) if isinstance(result, dict) else {}
    # Already-unlinked is a successful 200 response, never an error.
    payload["status"] = "success"
    payload.setdefault("series_id", series_id)
    payload.setdefault("series_name", series_row.get("name") or "")
    payload.setdefault("old_calibre_series_id", None)
    payload.setdefault("releases_reset", 0)
    payload.setdefault("already_unlinked", False)
    payload.setdefault("message", "")
    return JSONResponse(payload)

@app.post("/api/series/{series_id}/recheck-calibre")
async def api_recheck_series_calibre(series_id: int):
    """Re-reads Calibre, recomputes owned volumes and reconciles false In-Calibre releases."""
    series_row = await asyncio.to_thread(_find_series_row, series_id)
    if not series_row:
        raise HTTPException(status_code=404, detail="Series not found")
    try:
        result = await asyncio.to_thread(task_manager.recheck_series_calibre, series_id)
    except (KeyError, LookupError) as exc:
        raise HTTPException(status_code=404, detail=str(exc) or "Series not found") from exc
    except Exception as exc:
        logger.warning(f"Calibre recheck failed for series #{series_id}: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail=f"Could not recheck the series Calibre link: {exc}",
        ) from exc

    _invalidate_series_picker_cache()
    payload = dict(result) if isinstance(result, dict) else {}
    payload.setdefault("status", "success")
    payload.setdefault("series_id", series_id)
    payload.setdefault("series_name", series_row.get("name") or "")
    payload.setdefault("linked", bool(series_row.get("calibre_series_id")))
    payload.setdefault("owned_volumes", [])
    payload.setdefault("reconciled", {})
    payload.setdefault("message", "")
    return JSONResponse(payload)

# --- Storage / network / maintenance diagnostics (CONTRACT_V2_REBIND §6.3) ---

@app.get("/api/storage/health")
async def api_storage_health():
    """Calibre, data/downloads directory and database health; never raises."""
    try:
        calibre_path = db.get_setting("calibre_path", CALIBRE_DIR)
        downloads_dir = db.get_setting("downloads_dir", os.getenv("DOWNLOAD_DIR", "/downloads"))
        health = await asyncio.to_thread(
            diagnostics.storage_health, db, calibre_path, DATA_DIR, downloads_dir
        )
        payload = dict(health) if isinstance(health, dict) else {}
        payload.setdefault("status", "success")
        payload.setdefault("checked_at", _utc_iso_now())
        payload.setdefault("overall_ok", False)
        return JSONResponse(payload)
    except Exception as exc:
        logger.warning(f"Storage health check failed: {type(exc).__name__}: {exc}")
        return JSONResponse({
            "status": "degraded",
            "checked_at": _utc_iso_now(),
            "overall_ok": False,
            "error": str(exc)[:300],
        })

@app.get("/api/diagnostics/network")
async def api_network_diagnostics():
    """Provider reachability diagnostics (AniList/Kitsu/Nyaa/J-Novels); never raises."""
    try:
        result = await asyncio.to_thread(diagnostics.network_diagnostics, 5.0)
        payload = dict(result) if isinstance(result, dict) else {}
        payload.setdefault("checked_at", _utc_iso_now())
        payload.setdefault("results", [])
        payload.setdefault("ok", False)
        payload.setdefault("status", "success")
        return JSONResponse(payload)
    except Exception as exc:
        logger.warning(f"Network diagnostics failed: {type(exc).__name__}: {exc}")
        return JSONResponse({
            "status": "degraded",
            "checked_at": _utc_iso_now(),
            "results": [],
            "ok": False,
            "error": str(exc)[:300],
        })

@app.post("/api/maintenance/logs/prune")
async def api_prune_activity_logs():
    """Prunes activity_logs using the retention settings (age and/or row cap)."""
    days = _coerce_int(db.get_setting("activity_retention_days", "90")) or 0
    max_rows = _coerce_int(db.get_setting("activity_max_rows", "50000")) or 0
    days = max(0, min(3650, days))
    max_rows = max(0, min(1000000, max_rows))
    try:
        result = await asyncio.to_thread(db.prune_activity_logs, days, max_rows)
    except Exception as exc:
        logger.warning(f"Activity log prune failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Could not prune activity logs: {exc}") from exc

    summary = dict(result) if isinstance(result, dict) else {}
    removed = int(summary.get("removed") or 0)
    payload = {
        "status": "success",
        "removed_by_age": int(summary.get("removed_by_age") or 0),
        "removed_by_rows": int(summary.get("removed_by_rows") or 0),
        "removed": removed,
        "remaining": summary.get("remaining"),
        "retention_days": days,
        "max_rows": max_rows,
    }
    if removed > 0:
        try:
            await asyncio.to_thread(
                db.log_activity,
                "LOGS_PRUNED",
                f"Pruned {removed} activity log row(s)",
                json.dumps({"removed": removed, "retention_days": days, "max_rows": max_rows}),
            )
        except Exception:
            pass
    return JSONResponse(payload)

@app.get("/api/maintenance/database/stats")
async def api_database_stats():
    """Database file/WAL sizes, row counts and a guarded PRAGMA quick_check."""
    try:
        stats = await asyncio.to_thread(db.database_stats)
    except Exception as exc:
        logger.warning(f"Database stats failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Could not read database stats: {exc}") from exc

    payload = dict(stats) if isinstance(stats, dict) else {}

    def _quick_check():
        with db.get_connection() as conn:
            return conn.execute("PRAGMA quick_check;").fetchone()[0]

    integrity = None
    try:
        integrity = await asyncio.to_thread(_quick_check)
    except Exception as exc:
        integrity = f"error: {exc}"
    payload.setdefault("status", "success")
    payload["integrity"] = integrity
    return JSONResponse(payload)

@app.get("/api/maintenance/backup")
async def api_database_backup():
    """Downloads a consistent SQLite snapshot and deletes the temp copy afterwards."""
    db_path = str(getattr(db, "db_path", "") or "")
    if not db_path or not os.path.isfile(db_path):
        raise HTTPException(status_code=500, detail="Database file not found")
    try:
        backup_path, stamp = await asyncio.to_thread(_create_database_backup, db_path)
    except Exception as exc:
        logger.warning(f"Database backup failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Could not create the database backup") from exc
    filename = f"noveltrax-backup-{stamp}.db"
    return FileResponse(
        backup_path,
        media_type="application/octet-stream",
        filename=filename,
        background=BackgroundTask(os.remove, backup_path),
    )

@app.post("/api/maintenance/database/vacuum")
async def api_vacuum_database():
    """Runs integrity_check + VACUUM + ANALYZE on the SQLite database."""
    try:
        result = await asyncio.to_thread(db.vacuum_database)
    except Exception as exc:
        logger.warning(f"Database vacuum failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Could not optimize the database: {exc}") from exc
    payload = dict(result) if isinstance(result, dict) else {}
    payload.setdefault("status", "success")
    payload.setdefault("before_bytes", 0)
    payload.setdefault("after_bytes", 0)
    payload.setdefault("integrity", "ok")
    payload.setdefault("message", "")
    payload["before_human"] = diagnostics.format_bytes(payload.get("before_bytes") or 0)
    payload["after_human"] = diagnostics.format_bytes(payload.get("after_bytes") or 0)
    return JSONResponse(payload)

@app.get("/api/maintenance/series-attribution")
async def api_series_attribution_audit(limit: int = Query(200, ge=1, le=1000)):
    """Read-only audit of releases whose stored series disagrees with the resolver."""
    try:
        result = await asyncio.to_thread(
            diagnostics.series_attribution_audit, db, limit=limit, scan_limit=50000
        )
        payload = dict(result) if isinstance(result, dict) else {}
        payload.setdefault("checked", 0)
        payload.setdefault("count", 0)
        payload.setdefault("items", [])
        payload.setdefault("status", "success")
        return JSONResponse(payload)
    except Exception as exc:
        logger.warning(f"Series attribution audit failed: {type(exc).__name__}: {exc}")
        return JSONResponse({
            "status": "degraded",
            "checked": 0,
            "count": 0,
            "items": [],
            "error": str(exc)[:300],
        })

@app.post("/api/settings")
async def api_save_settings(payload: Dict[str, Any]):
    """Save application settings and dynamically reschedule background jobs."""
    payload = _prepare_settings_payload(payload)

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
    """Retrieves list of active torrents from the active downloader engine."""
    if _active_downloader_engine() == "torbox":
        payload = await asyncio.to_thread(torbox_manager.build_status)
        return JSONResponse({
            "status": "success",
            "engine": "torbox",
            "torrents": payload.get("torrents") or []
        })

    qbit = get_qbit_client()
    torrents = await asyncio.to_thread(qbit.get_torrents, category=qbit.category)
    return JSONResponse({"status": "success", "torrents": torrents})

@app.get("/api/downloads/status")
async def api_downloads_status():
    """Returns connectivity and version status of the active downloader engine."""
    if _active_downloader_engine() == "torbox":
        payload = await asyncio.to_thread(torbox_manager.build_status)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    qbit = get_qbit_client()
    status = await asyncio.to_thread(qbit.test_connection, timeout=3.0)
    return JSONResponse({
        "status": "success",
        "engine": "qbit",
        "connected": bool(status.get("success")),
        "version": status.get("version", ""),
        "message": status.get("message", "")
    }, headers={"Cache-Control": "no-store"})

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

@app.post("/api/downloader/torbox/test")
async def api_torbox_test(payload: Optional[Dict[str, Any]] = None):
    """Tests the TorBox API connection; blank/masked keys fall back to the saved API key."""
    p = payload or {}
    supplied_key = p.get("torbox_api_key")
    api_key = None
    if isinstance(supplied_key, str):
        candidate = supplied_key.strip()
        if candidate and candidate != TORBOX_API_KEY_MASK:
            api_key = candidate
    res = await asyncio.to_thread(torbox_manager.test_connection, api_key)
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

@app.post("/api/downloads/torbox/poll")
async def api_torbox_poll():
    """Runs one synchronous TorBox poll pass (Check now)."""
    if _active_downloader_engine() != "torbox":
        raise HTTPException(status_code=409, detail="TorBox is not the active downloader engine")
    result = dict((await asyncio.to_thread(torbox_manager.poll_now)) or {})
    # The UI treats a 200 as success; keep the pass diagnostics under poll_status.
    result["poll_status"] = result.get("status", "")
    result["status"] = "success"
    return JSONResponse(result)

@app.post("/api/downloads/torbox/{release_id}/retry")
async def api_torbox_retry(release_id: int):
    """Retries a failed TorBox download for a release."""
    result = await asyncio.to_thread(torbox_manager.retry_release, release_id)
    error_code = _result_error_code(result)
    if error_code:
        raise HTTPException(status_code=error_code, detail=result.get("message") or "TorBox retry failed")
    return JSONResponse(result)

@app.post("/api/downloads/torbox/{release_id}/remove")
async def api_torbox_remove(release_id: int):
    """Removes a saved TorBox download submission and its remote torrent best-effort."""
    result = await asyncio.to_thread(torbox_manager.remove_release, release_id)
    error_code = _result_error_code(result)
    if error_code:
        raise HTTPException(status_code=error_code, detail=result.get("message") or "TorBox removal failed")
    return JSONResponse(result)

@app.post("/api/downloads/torbox/clear-failed")
async def api_torbox_clear_failed():
    """Clears every failed TorBox download locally."""
    result = await asyncio.to_thread(torbox_manager.clear_failed)
    return JSONResponse(result)

@app.get("/api/releases/{release_id}/state")
async def api_release_download_state(release_id: int):
    """Returns the download state chip payload for a release."""
    release = db.get_release_by_id(release_id)
    if not release:
        raise HTTPException(status_code=404, detail="Release not found")
    return JSONResponse({"status": "success", **release_download_state(release)})

@app.post("/api/releases/{release_id}/download")
async def api_release_download(release_id: int):
    """Triggers manual download of a release via the active downloader engine."""
    target = db.get_release_by_id(release_id)
    if not target:
        raise HTTPException(status_code=404, detail="Release not found")

    if _active_downloader_engine() == "torbox":
        result = await asyncio.to_thread(torbox_manager.submit_release, target, source="manual")
        state = release_download_state(db.get_release_by_id(release_id) or target)
        if result.get("skipped"):
            return JSONResponse({
                "status": "skipped",
                "engine": "torbox",
                "message": result.get("message") or "TorBox downloads torrent/magnet releases only.",
                "state": state
            })
        if result.get("success"):
            return JSONResponse({
                "status": "success",
                "engine": "torbox",
                "cached": bool(result.get("cached")),
                "message": "Sent to Torbox successfully",
                "state": state
            })
        return JSONResponse({
            "status": "error",
            "engine": "torbox",
            "message": result.get("message") or "Failed to add release to TorBox",
            "state": state
        }, status_code=500)

    download_url = target.get("download_url") or target.get("link")
    if not download_url:
        raise HTTPException(status_code=400, detail="No downloadable URL or magnet link for release")

    qbit = get_qbit_client()
    savepath = db.get_setting("qbit_savepath", "/downloads")
    category = db.get_setting("qbit_category", "novels")

    # Resolve wanted volumes once: they are persisted with the download handle
    # so the periodic file-priority pass can reapply selection after metadata
    # resolves, and so retries target the same subset.
    missing = target.get("missing_volumes")
    if missing is None:
        try:
            missing = json.loads(target.get("missing_vols_json") or "[]")
        except (TypeError, ValueError):
            missing = []
    elif isinstance(missing, str):
        try:
            missing = json.loads(missing)
        except (TypeError, ValueError):
            missing = []
    try:
        wanted_volumes = sorted({
            round(float(v), 2) for v in (missing or [])
        })
    except (TypeError, ValueError):
        wanted_volumes = []
    vols = target.get("volumes")
    if vols is None:
        try:
            vols = json.loads(target.get("volumes_json") or "[]")
        except (TypeError, ValueError):
            vols = []
    elif isinstance(vols, str):
        try:
            vols = json.loads(vols)
        except (TypeError, ValueError):
            vols = []

    success = await asyncio.to_thread(
        qbit.add_torrent_url,
        url=download_url,
        category=category,
        savepath=savepath
    )

    if success:
        torrent_h = qbit.extract_torrent_hash(download_url)
        db.update_download_handle(
            release_id,
            "qbit",
            downloader_id=torrent_h or "",
            torrent_hash=torrent_h,
            wanted_volumes=wanted_volumes,
        )
        db.log_activity("MANUAL_DOWNLOAD", f"Sent to downloader: '{target['title']}'", f"URL: {download_url[:60]}...")

        # Deselect files whose volumes are already owned. Metadata may still be
        # resolving; the 30 s scheduler pass retries until file names are known.
        if torrent_h and target.get("is_bundle") and wanted_volumes and (
            not vols or len(wanted_volumes) < len(vols)
        ):
            selection = await asyncio.to_thread(
                qbit.apply_selective_volume_download,
                torrent_h,
                wanted_volumes=wanted_volumes,
                series_name=target.get("series_name")
            )
            status = (selection or {}).get("status")
            if status in ("filtered", "all_owned"):
                skipped = (selection or {}).get("skipped_volumes") or []
                kept = (selection or {}).get("kept_volumes") or []
                db.log_activity(
                    "SELECTIVE_DOWNLOAD",
                    f"Selective download applied for '{target['title']}': "
                    f"downloading {len(kept)} missing volume file(s), "
                    f"skipped {len(skipped)} owned file(s).",
                    json.dumps({
                        "release_id": release_id,
                        "torrent_hash": torrent_h,
                        "wanted_volumes": wanted_volumes,
                        "skipped_volumes": skipped,
                        "kept_volumes": kept,
                        "status": status,
                    }),
                )

        return JSONResponse({"status": "success", "engine": "qbit", "message": "Sent to downloader successfully"})
    else:
        return JSONResponse({"status": "error", "engine": "qbit", "message": "Failed to add release to downloader"}, status_code=500)

# --- Historical Backfill & Synopsis Endpoints ---

@app.post("/api/backfill/index")
async def api_backfill_index():
    """Immediately populates Discovery from J-Novels Complete Series Index."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, task_manager.ingest_master_index)
    return JSONResponse(result)

@app.post("/api/backfill/start")
async def api_backfill_start(payload: Optional[Dict[str, Any]] = None):
    """Starts the polite historical backfill (Master index + synopses + J-Novels/Nyaa pagination)."""
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

    # Optional per-run override of the backfill_include_nyaa setting.
    include_nyaa = p.get("include_nyaa")
    if include_nyaa is not None and not isinstance(include_nyaa, bool):
        include_nyaa = str(include_nyaa).strip().lower() not in ("false", "0", "no", "off")

    if bool(p.get("reset_nyaa", False)):
        task_manager.reset_nyaa_backfill_cursor()

    asyncio.create_task(task_manager.run_polite_backfill(
        max_pages=max_pages,
        enrich_synopses=enrich,
        delay_seconds=delay,
        include_nyaa=include_nyaa
    ))
    return JSONResponse({"status": "started", "message": "Polite backfill started in background"})

@app.post("/api/backfill/nyaa/reset")
async def api_backfill_nyaa_reset():
    """Resets the resumable Nyaa historical-backfill cursor back to page 1."""
    return JSONResponse(task_manager.reset_nyaa_backfill_cursor())

@app.post("/api/debug/action")
async def api_debug_action(payload: Dict[str, Any]):
    """Debug maintenance operations: clear descriptions, discovery, cover cache or metadata cache."""
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
    elif action == "clear-metadata":
        count = db.clear_novel_metadata_cache()
        return JSONResponse({"status": "success", "message": f"Cleared {count} cached metadata entr{'y' if count == 1 else 'ies'}."})
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
                "description": details.get("synopsis"),
                # Expanded from a J-Novels light-novel hub page: novel by provenance.
                "media_type": "novel"
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
