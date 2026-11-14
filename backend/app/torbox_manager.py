"""TorBox orchestration for NovelTrax (submit → poll → fetch → complete).

This module owns everything *around* the TorBox API client: the daemon poller,
the concurrency gate, the bounded fetch executor, selective file planning, the
status payload served to the Downloads page and the retry/remove/clear actions.

Dependency-light by contract (TorBox port §5): stdlib plus ``app.downloader``,
``app.downloader.base``, ``app.downloader.qbit.parse_torrent_filename_volumes``
and ``app.download_state``.  No FastAPI and no scheduler imports, because the
test suite imports this module in the bundled stdlib-only interpreter.

TorBox handles **torrent releases only** (Nyaa magnets / ``.torrent`` URLs).
Direct web-page releases (J-Novels, NovelUpdates, ...) cannot be fetched by
TorBox and are rejected with an actionable message.
"""

import json
import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.download_state import release_download_state
from app.downloader import as_bool, build_downloader, resolve_downloader_engine
from app.downloader.base import (
    DownloaderAuthError,
    DownloaderError,
    DownloaderFileError,
    DownloaderNotFoundError,
    DownloaderRateLimitError,
    encode_queued_handle_id,
    parse_queued_handle_id,
)
from app.downloader.qbit import parse_torrent_filename_volumes

logger = logging.getLogger("noveltrax.torbox")

# Local fetch target when the setting is absent (env-seeded in database.py).
DEFAULT_DOWNLOADS_DIR = os.getenv("DOWNLOAD_DIR", "/downloads")

# A whole-torrent zip is only used when it is comfortably under the size cap and
# the bundle is small; otherwise the exact per-file links are fetched so every
# file is individually size-checked (TorBox port §5.4).
TORBOX_ZIP_MAX_FILES = 20
TORBOX_ZIP_SAFETY_RATIO = 0.9

# Retry rate limit per release (seconds) for ``retry_release`` (TorBox port §5.6).
TORBOX_RETRY_MIN_SECONDS = 5.0

# Poller idle/active cadence bounds (TorBox port §5.2).
TORBOX_ACTIVE_POLL_FLOOR = 5.0
TORBOX_IDLE_POLL_MIN = 15.0
TORBOX_IDLE_POLL_MAX = 30.0

TORBOX_TORRENT_ONLY_MESSAGE = (
    "TorBox downloads torrent/magnet releases only (Nyaa). This release's link is a "
    "direct web page, which TorBox cannot fetch."
)

_VIEW_PATH_RE = re.compile(r"/view/\d+/?$", re.IGNORECASE)
_HEX_INFOHASH_RE = re.compile(r"[0-9a-fA-F]{40}")
_BASE32_INFOHASH_RE = re.compile(r"[A-Za-z2-7]{32}")
_SAFE_DIRNAME_STRIP_RE = re.compile(r"[^\w\s.()\[\]-]+")

# Torrent entries that are metadata/clutter rather than the payload itself. They
# are ignored when deciding whether a torrent is ".cbz-only" so a manga pack that
# also ships a cover.jpg/.nfo still counts as manga.
_ANCILLARY_TORRENT_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".nfo", ".txt", ".url", ".html", ".htm", ".xml", ".json", ".md",
    ".sfv", ".md5", ".sha1", ".sha256", ".lnk", ".db", ".ini", ".log",
}
#: A .cbz-only torrent is treated as a manga rip (never a light novel).
_CBZ_ONLY_MANGA_MIN_FILES = 1
_CBZ_EXTENSION = ".cbz"



# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------
def _safe_dirname(title: Any) -> str:
    """Filesystem-safe folder name for a release's fetched files.

    Mirrors the reference engine: strip uncommon characters to ``_``, collapse
    whitespace, cap at 120 chars, fall back to ``torbox-download``.
    """
    cleaned = _SAFE_DIRNAME_STRIP_RE.sub("_", str(title or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
    return cleaned[:120] or "torbox-download"


def _torrent_file_extension(name: Any) -> str:
    """Lower-case extension of a torrent entry's basename (``""`` when absent)."""
    text = str(name or "").replace("\\", "/").strip()
    base = text.rsplit("/", 1)[-1]
    return os.path.splitext(base)[1].lower()


def _cbz_only_manga(files: Any) -> Dict[str, Any]:
    """Detect a torrent whose content files are all ``.cbz`` (a manga rip).

    NovelTrax tracks light novels, and the title-based media filter runs at
    ingestion (before TorBox's file list exists), so a manga pack with a
    light-novel-looking title can still reach the download queue. TorBox's file
    list is the reliable signal: when every non-ancillary entry ends in ``.cbz``
    the torrent is manga and must be rejected instead of downloaded into the
    novel library. Covers/.nfo/.txt-style clutter is ignored. A mixed torrent
    (``.cbz`` + ``.epub``) is *not* rejected here — it still contains a novel.

    Returns ``{"manga_only", "cbz_count", "content_count", "ignored_count",
    "files"}`` where ``files`` is a short sample of the .cbz entry names.
    """
    result: Dict[str, Any] = {
        "manga_only": False,
        "cbz_count": 0,
        "content_count": 0,
        "ignored_count": 0,
        "files": [],
    }
    if not isinstance(files, list):
        return result
    content: List[Any] = []
    ignored = 0
    for item in files:
        if isinstance(item, dict):
            name = item.get("name") or item.get("path") or item.get("short_name")
        else:
            name = item
        if not name:
            ignored += 1
            continue
        extension = _torrent_file_extension(name)
        if extension in _ANCILLARY_TORRENT_EXTENSIONS:
            ignored += 1
            continue
        content.append((extension, str(name)))
    cbz_names = [name for extension, name in content if extension == _CBZ_EXTENSION]
    result.update(
        {
            "cbz_count": len(cbz_names),
            "content_count": len(content),
            "ignored_count": ignored,
            "files": cbz_names[:10],
        }
    )
    result["manga_only"] = (
        len(cbz_names) >= _CBZ_ONLY_MANGA_MIN_FILES
        and len(cbz_names) == len(content)
    )
    return result


def _parse_db_timestamp(value: Any) -> Optional[datetime]:
    """Parse a SQLite ``CURRENT_TIMESTAMP``/ISO string as a naive UTC datetime."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _utc_stamp() -> str:
    """``YYYY-MM-DD HH:MM:SS`` (UTC), the format the rest of the DB uses."""
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_from_epoch(epoch: Any) -> Optional[str]:
    try:
        value = float(epoch)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _seconds_between(start_value: Any, end_value: Any) -> float:
    """Seconds between two stored UTC timestamps (0.0 when unparseable)."""
    start = _parse_db_timestamp(start_value)
    end = _parse_db_timestamp(end_value)
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def _fmt_volumes(volumes: Any) -> str:
    """``[1.0, 2.5]`` → ``"1, 2.5"`` (integers without a trailing ``.0``)."""
    parts: List[str] = []
    for value in volumes or []:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        parts.append(str(int(number)) if number.is_integer() else str(number))
    return ", ".join(parts)


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _json_list(value: Any) -> List[Any]:
    """Decode a JSON list column (accepts an already-decoded list)."""
    if isinstance(value, (list, tuple, set)):
        return list(value)
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return decoded if isinstance(decoded, list) else []


def _torrentish(value: Any, allow_view: bool = False) -> bool:
    """True when *value* can identify a torrent for TorBox (magnet/URL/infohash)."""
    text = str(value or "").strip()
    if not text:
        return False
    lower = text.lower()
    if lower.startswith("magnet:"):
        return True
    if "urn:btih:" in lower:
        return True
    if _HEX_INFOHASH_RE.fullmatch(text):
        return True
    if _BASE32_INFOHASH_RE.fullmatch(text):
        return True
    if lower.startswith("http://") or lower.startswith("https://"):
        path = lower.split("?", 1)[0].split("#", 1)[0]
        if path.endswith(".torrent"):
            return True
        if "/download/" in path:
            return True
        if allow_view and _VIEW_PATH_RE.search(path):
            return True
    return False


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
class TorBoxManager:
    """Owns the TorBox poller, fetch executor and user-facing operations.

    ``downloader_factory`` is ``Callable[[str], DownloaderClient]`` and exists so
    tests can inject a fake engine without any network access.  The default
    builds the configured engine through ``app.downloader.build_downloader``.
    """

    def __init__(self, db: Any, downloader_factory: Optional[Callable[[str], Any]] = None):
        self.db = db
        self._downloader_factory = downloader_factory

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()

        self._state_lock = threading.Lock()
        self._fetch_executor: Optional[ThreadPoolExecutor] = None
        self._fetching = set()
        self._fetch_locks: Dict[int, threading.Lock] = {}
        self._fetch_futures: Dict[int, Any] = {}

        # Non-fatal messages are audited once per distinct message/release.
        self._notified: Dict[int, str] = {}
        # Per-release retry rate limiting (epoch seconds).
        self._retry_last: Dict[int, float] = {}

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Start the daemon poller thread (idempotent)."""
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._poller_loop,
                name="noveltrax-torbox-poller",
                daemon=True,
            )
            self._thread.start()
        logger.info("TorBox poller thread started.")

    def stop(self) -> None:
        """Stop the poller, wake it, join briefly and shut the fetch executor down."""
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._state_lock:
            executor = self._fetch_executor
            self._fetch_executor = None
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:  # Python 3.8 fallback (cancel_futures added in 3.9)
                executor.shutdown(wait=False)
        logger.info("TorBox poller thread stopped.")

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(
            thread is not None
            and thread.is_alive()
            and not self._stop_event.is_set()
        )

    # -- engine / fs helpers ------------------------------------------------
    def engine(self) -> str:
        """Currently configured downloader engine (``qbit``/``torbox``)."""
        return resolve_downloader_engine(self.db)

    def get_downloader(self, engine: Optional[str] = None) -> Any:
        """Build the engine client (tests inject a fake through the factory)."""
        selected = str(engine or self.engine() or "torbox").strip().lower()
        if self._downloader_factory is not None:
            return self._downloader_factory(selected)
        return build_downloader(self.db, engine=selected)

    def downloads_dir(self) -> str:
        """Absolute local directory fetched files land in (created on demand)."""
        raw = self.db.get_setting("downloads_dir", DEFAULT_DOWNLOADS_DIR)
        path = str(raw or DEFAULT_DOWNLOADS_DIR).strip() or DEFAULT_DOWNLOADS_DIR
        path = os.path.abspath(os.path.expanduser(path))
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            raise DownloaderFileError(
                f"Could not create the downloads directory '{path}': {exc.strerror or exc} "
                f"(errno {exc.errno}). Check that DOWNLOAD_DIR points at a writable, "
                "mounted folder."
            ) from exc
        return path

    # -- settings helpers ---------------------------------------------------
    def _max_concurrent(self) -> int:
        return _clamp_int(self.db.get_setting("torbox_max_concurrent", "2"), 1, 20, 2)

    def _poll_floor(self) -> float:
        try:
            value = float(self.db.get_setting("torbox_poll_seconds", "5") or 5)
        except (TypeError, ValueError):
            value = 5.0
        return max(TORBOX_ACTIVE_POLL_FLOOR, value)

    def _timeout_hours(self) -> float:
        try:
            value = float(self.db.get_setting("torbox_timeout_hours", "6") or 0)
        except (TypeError, ValueError):
            value = 6.0
        if value != value:  # NaN guard
            return 6.0
        return max(0.0, min(value, 168.0))

    def _max_download_bytes(self) -> int:
        try:
            max_gb = float(self.db.get_setting("torbox_max_download_gb", "64") or 64)
        except (TypeError, ValueError):
            max_gb = 64.0
        if max_gb != max_gb:
            max_gb = 64.0
        max_gb = max(1.0, min(max_gb, 10240.0))
        return int(max_gb * 1024 ** 3)

    def _delete_after_download(self) -> bool:
        return as_bool(self.db.get_setting("torbox_delete_after_download", "true"), default=True)

    def _delete_on_failure(self) -> bool:
        return as_bool(self.db.get_setting("torbox_delete_on_failure", "true"), default=True)

    def _strict_selection(self) -> bool:
        return as_bool(
            self.db.get_setting("torbox_strict_volume_selection", "true"), default=True,
        )

    # -- URL / wanted-volume helpers ---------------------------------------
    @staticmethod
    def _resolve_submit_url(release: Dict[str, Any]) -> str:
        for key in ("download_url", "link"):
            value = str(release.get(key) or "").strip()
            if value:
                return value
        return ""

    def _is_torrent_release(self, release: Dict[str, Any], url: str) -> bool:
        """Torrent-only guard: magnet/.torrent//download/ URL, or a row that flips it."""
        if _torrentish(url):
            return True
        # A row can still be a torrent release when the primary link is a view page
        # but the GUID/torrent_url identifies the Nyaa torrent.
        for key in ("guid", "torrent_url"):
            if _torrentish(release.get(key), allow_view=True):
                return True
        return False

    @staticmethod
    def _json_float_list(value: Any) -> List[float]:
        values: List[float] = []
        for item in _json_list(value):
            try:
                number = round(float(item), 2)
            except (TypeError, ValueError):
                continue
            if number not in values:
                values.append(number)
        return values

    def _resolve_wanted_volumes(self, release: Dict[str, Any]) -> List[float]:
        """Authoritative wanted-volume list for a release.

        Order (TorBox port §5.1): persisted ``download_wanted_json`` (non-empty) →
        ``missing_vols_json`` → ``volumes_json`` (all) → the single ``volume``
        column → ``[]``.  Empty means "unknown", never "nothing missing".
        """
        release = release if isinstance(release, dict) else {}
        for key in ("download_wanted_json", "missing_vols_json", "volumes_json"):
            values = self._json_float_list(release.get(key))
            if values:
                return values
        volume = release.get("volume")
        if volume is not None:
            try:
                return [round(float(volume), 2)]
            except (TypeError, ValueError):
                return []
        return []

    def _handle_from_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Reconstruct a downloader handle from a persisted release row."""
        row = row if isinstance(row, dict) else {}
        raw = row.get("downloader_id")
        hash_value = str(row.get("torrent_hash") or "")
        queued_id = parse_queued_handle_id(raw)
        if queued_id is not None:
            return {
                "engine": "torbox",
                "queued_id": queued_id,
                "torrent_id": None,
                "hash": hash_value,
            }
        try:
            torrent_id = int(raw or 0)
        except (TypeError, ValueError):
            torrent_id = 0
        if torrent_id:
            return {"engine": "torbox", "torrent_id": torrent_id, "hash": hash_value}
        return {}

    def _handle_for_release(self, release_id: int) -> Dict[str, Any]:
        try:
            row = self.db.get_release_by_id(release_id) or {}
        except Exception:
            return {}
        return self._handle_from_row(row)

    # -- auditing helpers ---------------------------------------------------
    def _log_activity(self, event_type: str, message: str, details: str = "") -> None:
        try:
            self.db.log_activity(event_type, message, details or "")
        except Exception as exc:  # auditing must never break a download
            logger.debug("Could not write activity log %s: %s", event_type, exc)

    def _surface_once(
        self, release_id: int, message: str, title: str, event_type: str = "TORBOX_ISSUE",
    ) -> None:
        """Audit a non-fatal TorBox problem once per distinct message."""
        try:
            key = int(release_id or 0)
        except (TypeError, ValueError):
            key = 0
        previous = self._notified.get(key)
        if previous == message:
            return
        self._notified[key] = message
        label = {
            "TORBOX_QUEUED": "queued",
            "TORBOX_PROMOTED": "activated",
            "TORBOX_STALLED": "stalled",
            "TORBOX_PROCESSING": "finalizing",
        }.get(event_type, "download issue")
        self._log_activity(event_type, f"TorBox {label}: '{title}'", message)
        logger.warning("TorBox %s for '%s': %s", label, title, message)

    # -- submit -------------------------------------------------------------
    def submit_release(self, release: Any, source: str = "manual") -> Dict[str, Any]:
        """Submit one release to TorBox and persist its handle.

        Never raises for normal user errors; returns a plain dict with
        ``success``/``skipped``/``engine``/``message``.
        """
        release = release if isinstance(release, dict) else {}
        engine = self.engine()
        title = str(release.get("title") or "")
        try:
            release_id = int(release.get("id") or 0)
        except (TypeError, ValueError):
            release_id = 0

        if engine != "torbox":
            return {
                "success": False,
                "engine": engine,
                "message": "TorBox is not the active download engine.",
            }

        url = self._resolve_submit_url(release)
        if not url:
            return {
                "success": False,
                "engine": "torbox",
                "message": f"No download URL for '{title or 'release'}'.",
            }
        if not self._is_torrent_release(release, url):
            self._log_activity(
                "TORBOX_SKIPPED",
                f"TorBox skipped a non-torrent release: '{title}'",
                f"url={url.split('?', 1)[0]}; Source: {source}" if source else f"url={url.split('?', 1)[0]}",
            )
            return {
                "success": False,
                "skipped": True,
                "engine": "torbox",
                "message": TORBOX_TORRENT_ONLY_MESSAGE,
            }

        wanted = self._resolve_wanted_volumes(release)

        # Concurrency gate: active submissions only.  Queued-slot rows do not hold a
        # TorBox active slot and therefore do not count against max-concurrent.
        max_concurrent = self._max_concurrent()
        try:
            active_rows = self.db.get_active_torbox_downloads()
        except Exception:
            active_rows = []
        active = [
            row for row in active_rows
            if parse_queued_handle_id(row.get("downloader_id")) is None
        ]
        if len(active) >= max_concurrent:
            message = (
                f"TorBox concurrency limit reached ({max_concurrent} active download(s)); "
                f"'{title}' was not submitted."
            )
            self._log_activity(
                "TORBOX_FAILED", message, f"torbox_max_concurrent={max_concurrent}",
            )
            return {
                "success": False,
                "engine": "torbox",
                "message": message,
                "retryable": True,
            }

        try:
            savepath = os.path.join(self.downloads_dir(), _safe_dirname(title))
        except DownloaderFileError as exc:
            self._log_activity("TORBOX_FAILED", str(exc), f"Source: {source}" if source else "")
            return {"success": False, "engine": "torbox", "message": str(exc)}

        try:
            downloader = self.get_downloader("torbox")
            result = downloader.submit(release, savepath, wanted) or {}
        except DownloaderError as exc:
            message = f"TorBox submit failed for '{title}': {exc}"
            logger.warning(message)
            self._log_activity("TORBOX_FAILED", message, f"Source: {source}" if source else "")
            return {
                "success": False,
                "engine": "torbox",
                "message": str(exc),
                "retryable": True,
            }
        except Exception as exc:  # unexpected: contain, never 500 the route
            logger.warning("TorBox submit crashed for '%s': %s", title, type(exc).__name__)
            return {
                "success": False,
                "engine": "torbox",
                "message": f"TorBox submit failed ({type(exc).__name__}).",
                "retryable": True,
            }

        handle = result.get("handle") or {}
        torrent_id = handle.get("torrent_id")
        queued_id = handle.get("queued_id")
        persisted_id = (
            str(torrent_id) if torrent_id
            else (encode_queued_handle_id(queued_id) if queued_id else "")
        )
        if not persisted_id:
            message = f"TorBox did not return a torrent handle for '{title}'."
            self._log_activity("TORBOX_FAILED", message, f"Source: {source}" if source else "")
            return {"success": False, "engine": "torbox", "message": message}

        try:
            self.db.update_download_handle(
                release_id,
                "torbox",
                downloader_id=persisted_id,
                torrent_hash=handle.get("hash"),
                wanted_volumes=wanted,
            )
        except Exception as exc:
            logger.warning("Could not persist the TorBox handle for release %s: %s", release_id, exc)
            return {
                "success": False,
                "engine": "torbox",
                "message": "TorBox accepted the release but the download handle could not be saved.",
            }

        cached = bool(result.get("cached"))
        self.request_poll()

        if queued_id and not torrent_id:
            message = "queued: waiting for a free TorBox slot"
            self._log_activity(
                "TORBOX_SUBMITTED",
                f"Sent to TorBox (queued): '{title}'",
                f"queued_id={queued_id}; {message}"
                + (f"; Source: {source}" if source else "")
                + (f"; wantedVolumes={wanted}" if wanted else ""),
            )
            return {
                "success": True,
                "engine": "torbox",
                "cached": False,
                "queued": True,
                "state": "queued_slot",
                "message": message,
            }

        event = "TORBOX_CACHED" if cached else "TORBOX_SUBMITTED"
        detail_parts = [f"torrent_id={torrent_id}", f"hash={handle.get('hash') or ''}"]
        if wanted:
            detail_parts.append(f"wantedVolumes={wanted}")
        if source:
            detail_parts.append(f"Source: {source}")
        self._log_activity(
            event,
            f"Sent to TorBox{' (cached)' if cached else ''}: '{title}'",
            "; ".join(detail_parts),
        )
        return {
            "success": True,
            "engine": "torbox",
            "cached": cached,
            "queued": False,
            "state": "downloading",
            "message": (
                "TorBox reports this release cached; fetching starts on the next poll."
                if cached else "Sent to TorBox successfully."
            ),
        }

    # -- poller -------------------------------------------------------------
    def request_poll(self) -> None:
        """Wake the poller immediately (after a submit or a Check Now click)."""
        try:
            self._wake_event.set()
        except Exception:
            pass

    def _next_interval(self) -> float:
        """Adaptive poll cadence: short while busy, backed off when idle."""
        floor = self._poll_floor()
        busy = False
        with self._state_lock:
            busy = bool(self._fetching)
        if not busy:
            try:
                rows = self.db.get_active_torbox_downloads()
            except Exception:
                rows = []
            for row in rows:
                try:
                    data = json.loads(row.get("torbox_progress_json") or "{}")
                except (TypeError, ValueError):
                    data = {}
                if isinstance(data, dict) and data.get("phase") in ("ready", "fetching", "routing"):
                    busy = True
                    break
        if busy:
            return floor
        return min(TORBOX_IDLE_POLL_MAX, max(TORBOX_IDLE_POLL_MIN, floor * 3))

    def _poller_loop(self) -> None:
        logger.info("TorBox poller loop started.")
        while not self._stop_event.is_set():
            try:
                interval = self._next_interval()
            except Exception:
                interval = self._poll_floor()
            self._wake_event.wait(interval)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            try:
                self.poll_once()
            except Exception as exc:
                logger.warning("TorBox poller pass failed: %s", exc, exc_info=True)
        logger.info("TorBox poller loop stopped.")

    # -- fetch executor -----------------------------------------------------
    def _ensure_fetch_executor(self) -> ThreadPoolExecutor:
        with self._state_lock:
            if self._fetch_executor is None:
                self._fetch_executor = ThreadPoolExecutor(
                    max_workers=self._max_concurrent(),
                    thread_name_prefix="noveltrax-torbox-fetch",
                )
            return self._fetch_executor

    def _submit_fetch(
        self, release: Dict[str, Any], downloader: Any, handle: Dict[str, Any], info: Dict[str, Any],
    ) -> bool:
        """Schedule one ready release on the bounded fetch executor (per-release dedupe)."""
        try:
            release_id = int((release or {}).get("id") or 0)
        except (TypeError, ValueError):
            release_id = 0
        if not release_id:
            return False
        lock: Optional[threading.Lock] = None
        with self._state_lock:
            if release_id in self._fetching:
                return False
            lock = self._fetch_locks.setdefault(release_id, threading.Lock())
            if not lock.acquire(blocking=False):
                return False
            self._fetching.add(release_id)
        try:
            executor = self._ensure_fetch_executor()
            future = executor.submit(
                self._fetch_thread, release, downloader, handle, info, lock,
            )
        except Exception as exc:
            logger.warning("Could not schedule TorBox fetch for release %s: %s", release_id, exc)
            with self._state_lock:
                self._fetching.discard(release_id)
                self._fetch_locks.pop(release_id, None)
            try:
                lock.release()
            except RuntimeError:
                pass
            return False
        with self._state_lock:
            self._fetch_futures[release_id] = future
        return True

    def _fetch_thread(
        self, release: Dict[str, Any], downloader: Any, handle: Dict[str, Any],
        info: Dict[str, Any], lock: threading.Lock,
    ) -> None:
        release_id = _coerce_int((release or {}).get("id"), 0)
        try:
            self._fetch_release(release, downloader, handle, info)
        except Exception as exc:  # _fetch_release contains its own failures
            logger.warning(
                "TorBox fetch thread crashed for release %s: %s", release_id,
                type(exc).__name__, exc_info=True,
            )
            try:
                self._fail_release(
                    release_id, f"{type(exc).__name__}: {exc}", downloader=downloader, handle=handle,
                )
            except Exception:
                pass
        finally:
            with self._state_lock:
                self._fetching.discard(release_id)
                self._fetch_futures.pop(release_id, None)
            try:
                lock.release()
            except RuntimeError:
                pass

    def _wait_for_fetches(self, timeout: float = 10.0) -> None:
        """Test/API helper: wait (bounded) for in-flight fetch futures to finish."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            with self._state_lock:
                futures = list(self._fetch_futures.values())
            if not futures:
                return
            for future in futures:
                try:
                    future.result(timeout=max(0.01, deadline - time.monotonic()))
                except Exception:
                    pass
            with self._state_lock:
                if not self._fetch_futures:
                    return
            time.sleep(0.01)

    # -- polling ------------------------------------------------------------
    def poll_once(self, async_fetch: Optional[bool] = None) -> Dict[str, Any]:
        """Run one polling pass over every active TorBox release.

        ``async_fetch`` defaults to True only when the poller thread is running;
        direct callers/tests run the fetch inline so results are deterministic.
        """
        if async_fetch is None:
            async_fetch = self.running

        try:
            self.db.set_setting("torbox_last_poll_at", str(time.time()))
        except Exception:
            pass

        base_result = {
            "status": "ok",
            "processed": 0,
            "checked": 0,
            "completed": 0,
            "failed": 0,
            "queued_fetch": 0,
            "fetching": 0,
            "last_checked_at": _iso_from_epoch(time.time()),
        }

        try:
            downloader = self.get_downloader("torbox")
        except Exception as exc:
            logger.warning("Could not build the TorBox downloader: %s", type(exc).__name__)
            return {**base_result, "status": "disabled", "message": "TorBox is not configured."}

        if not getattr(downloader, "enabled", False):
            return {**base_result, "status": "disabled"}

        try:
            rows = self.db.get_active_torbox_downloads()
        except Exception as exc:
            logger.warning("Could not read active TorBox downloads: %s", exc)
            rows = []
        if not rows:
            return {**base_result, "status": "idle"}

        timeout_hours = self._timeout_hours()
        processed = 0
        completed = 0
        failed = 0
        queued_fetch = 0
        rejected_manga = 0
        rate_limited = False

        for release in rows:
            release_id = 0
            title = str((release or {}).get("title") or "")
            handle: Dict[str, Any] = {}
            try:
                release_id = int(release.get("id"))
                queued_id = parse_queued_handle_id(release.get("downloader_id"))

                # Hard timeout (0 disables) — the release stays REQUESTED for a retry.
                started = _parse_db_timestamp(release.get("downloader_started_at"))
                if started is not None and timeout_hours > 0:
                    age_hours = (datetime.utcnow() - started).total_seconds() / 3600.0
                    if age_hours > timeout_hours:
                        if queued_id is not None:
                            message = (
                                f"No free TorBox active slot for {age_hours:.1f}h "
                                f"(limit {timeout_hours:g}h); the release stays REQUESTED for a retry."
                            )
                        else:
                            message = (
                                f"Timed out after {age_hours:.1f}h (limit {timeout_hours:g}h); "
                                "the release stays REQUESTED for a retry."
                            )
                        self._fail_release(release_id, message, downloader=downloader)
                        failed += 1
                        continue

                if queued_id is not None:
                    handle = {
                        "engine": "torbox",
                        "queued_id": queued_id,
                        "torrent_id": None,
                        "hash": str(release.get("torrent_hash") or ""),
                    }
                else:
                    torrent_id = _coerce_int(release.get("downloader_id"), 0)
                    if not torrent_id:
                        self._fail_release(
                            release_id,
                            "Missing TorBox torrent_id/queued_id on the release row.",
                            downloader=downloader,
                        )
                        failed += 1
                        continue
                    handle = {
                        "engine": "torbox",
                        "torrent_id": torrent_id,
                        "hash": str(release.get("torrent_hash") or ""),
                    }

                try:
                    info = downloader.status(handle) or {}
                except DownloaderRateLimitError as exc:
                    self._surface_once(release_id, str(exc), title, event_type="TORBOX_RATE_LIMITED")
                    rate_limited = True
                    break
                except (DownloaderAuthError, DownloaderNotFoundError) as exc:
                    self._fail_release(release_id, str(exc), downloader=downloader, handle=handle)
                    failed += 1
                    continue
                except DownloaderError as exc:
                    self._surface_once(release_id, str(exc), title)
                    continue

                processed += 1

                # A queued submission just activated: capture the real torrent id and
                # keep processing the normal flow in this same pass.
                promoted = info.get("promoted_torrent_id")
                if promoted:
                    try:
                        self.db.set_downloader_id(release_id, str(promoted))
                    except Exception as exc:
                        logger.debug("Could not persist promoted TorBox id: %s", exc)
                    self._surface_once(
                        release_id,
                        f"Activated from queued_id={handle.get('queued_id')} as TorBox torrent {promoted}.",
                        title,
                        event_type="TORBOX_PROMOTED",
                    )
                    handle = {
                        "engine": "torbox",
                        "torrent_id": _coerce_int(promoted, 0),
                        "queued_id": handle.get("queued_id"),
                        "hash": handle.get("hash") or "",
                    }
                    self._notified.pop(release_id, None)

                # TorBox-only media guard, checked as soon as the file list exists
                # (even while the cloud torrent is still downloading) so a manga
                # pack never consumes bandwidth or lands in the novel library.
                manga_detection = _cbz_only_manga(info.get("files"))
                if manga_detection["manga_only"]:
                    self._reject_cbz_manga_torrent(
                        release_id, title, downloader, handle, manga_detection,
                    )
                    rejected_manga += 1
                    continue

                state = str(info.get("state") or "queued")

                if state == "queued_slot":
                    message = "queued: waiting for a free TorBox slot"
                    position = info.get("queue_position")
                    active_limit = info.get("active_limit")
                    if position is not None:
                        message += f" (position {position})"
                    if active_limit is not None:
                        message += f" [active limit {active_limit}]"
                    self._surface_once(
                        release_id, message, title, event_type="TORBOX_QUEUED",
                    )
                    continue

                if state == "failed":
                    self._fail_release(
                        release_id,
                        f"TorBox reported state '{info.get('raw_state') or 'failed'}'.",
                        downloader=downloader,
                        handle=handle,
                    )
                    failed += 1
                    continue

                if state == "stalled":
                    self._surface_once(
                        release_id,
                        f"TorBox reports '{info.get('raw_state') or 'stalled'}' (no seeds/progress); "
                        "it is not retried automatically.",
                        title,
                        event_type="TORBOX_STALLED",
                    )
                    continue

                if state == "processing":
                    # Finished on TorBox but the files are not published yet: keep
                    # polling (the timeout still applies), never request a link yet.
                    try:
                        self.db.update_torbox_progress(release_id, phase="processing")
                    except Exception as exc:
                        logger.debug("Could not persist processing phase: %s", exc)
                    self._surface_once(
                        release_id,
                        "TorBox is still finalizing the download; files are not ready yet "
                        "(waiting before fetching).",
                        title,
                        event_type="TORBOX_PROCESSING",
                    )
                    continue

                if state != "ready":
                    continue

                try:
                    progress = self.db.get_torbox_progress(release_id)
                except Exception:
                    progress = {}
                if not progress.get("ready_detected_at"):
                    try:
                        self.db.update_torbox_progress(
                            release_id,
                            phase="ready",
                            ready_detected_at=_utc_iso(),
                            total_bytes=_coerce_int(info.get("size"), 0) or None,
                            progress_pct=0.0,
                        )
                    except Exception as exc:
                        logger.debug("Could not persist ready phase: %s", exc)

                if async_fetch:
                    if self._submit_fetch(release, downloader, handle, info):
                        queued_fetch += 1
                    continue

                outcome = self._fetch_release(release, downloader, handle, info)
                if outcome.get("status") == "completed":
                    completed += 1
                elif outcome.get("status") == "rejected_manga":
                    rejected_manga += 1
                elif outcome.get("status") == "failed":
                    failed += 1

            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "TorBox poll failed for release %s (%s): %s",
                    release_id, title, message, exc_info=True,
                )
                try:
                    self._fail_release(
                        release_id,
                        message,
                        downloader=downloader,
                        handle=handle or None,
                    )
                except Exception:
                    pass
                failed += 1
                continue

        with self._state_lock:
            fetching = len(self._fetching)
        return {
            "status": "rate_limited" if rate_limited else "ok",
            "processed": processed,
            "checked": processed,
            "completed": completed,
            "failed": failed,
            "rejected_manga": rejected_manga,
            "queued_fetch": queued_fetch,
            "fetching": fetching,
            "last_checked_at": _iso_from_epoch(time.time()),
        }

    def poll_now(self) -> Dict[str, Any]:
        """Check-now action: run one synchronous pass and return fresh counters."""
        self.request_poll()
        result = self.poll_once(async_fetch=False)
        try:
            result["last_checked_at"] = _iso_from_epoch(
                self.db.get_setting("torbox_last_poll_at", "0")
            ) or result.get("last_checked_at")
        except Exception:
            pass
        with self._state_lock:
            result["fetching"] = len(self._fetching)
        return result

    # -- fetch --------------------------------------------------------------
    def _filesystem_error(self, action: str, path: str, exc: OSError) -> str:
        text = (
            f"Could not {action} '{path}': {exc.strerror or exc} (errno {exc.errno}). "
            "Check that DOWNLOAD_DIR points at a writable, mounted folder."
        )
        try:
            usage = shutil.disk_usage(path if os.path.isdir(path) else os.path.dirname(path) or ".")
            text += f" Free space: {usage.free / (1024 ** 3):.2f} GiB."
        except Exception:
            pass
        return text

    @staticmethod
    def _cleanup_partials(dest_dir: str) -> None:
        """Remove temp/partial fetch files (``*.part`` / ``.torbox-*``); keeps user files."""
        if not dest_dir or not os.path.isdir(dest_dir):
            return
        for root, _dirs, files in os.walk(dest_dir):
            for filename in files:
                if not (filename.lower().endswith(".part") or filename.startswith(".torbox-")):
                    continue
                try:
                    os.remove(os.path.join(root, filename))
                except OSError:
                    pass

    def _progress_callback(self, release_id: int, total_bytes: int) -> Callable[[str, int], None]:
        """Throttled progress writer: at most 1 write / 2 s or 1 % (final always)."""
        tracker = {
            "files": {},
            "lock": threading.Lock(),
            "started": time.monotonic(),
            "last_save": 0.0,
            "last_pct": -1.0,
        }

        def _callback(label: Any, written: Any) -> None:
            now = time.monotonic()
            try:
                written_int = int(written or 0)
            except (TypeError, ValueError):
                return
            name = str(label or "")
            with tracker["lock"]:
                tracker["files"][name] = max(tracker["files"].get(name, 0), written_int)
                downloaded = sum(tracker["files"].values())
            total = int(total_bytes or 0)
            if total:
                downloaded = min(downloaded, total)
            pct = (downloaded / total * 100.0) if total else 0.0
            final = bool(total) and downloaded >= total
            if (
                not final
                and now - tracker["last_save"] < 2.0
                and (not total or pct - tracker["last_pct"] < 1.0)
            ):
                return
            tracker["last_save"] = now
            tracker["last_pct"] = pct
            elapsed = max(0.001, now - tracker["started"])
            speed = int(downloaded / elapsed) if downloaded else 0
            try:
                self.db.update_torbox_progress(
                    release_id,
                    phase="fetching",
                    downloaded_bytes=downloaded,
                    total_bytes=total or None,
                    speed_bps=speed,
                    progress_pct=round(pct, 1),
                )
            except Exception as exc:  # progress must never abort a download
                logger.debug("Could not persist TorBox progress for release %s: %s", release_id, exc)

        return _callback

    def _check_file_size(self, item: Dict[str, Any], max_bytes: int) -> None:
        item = item or {}
        size = _coerce_int(item.get("size"), 0)
        if size > max_bytes:
            name = str(item.get("name") or item.get("short_name") or f"file {item.get('id')}")
            size_gb = size / (1024 ** 3)
            cap_gb = max_bytes / (1024 ** 3)
            raise DownloaderFileError(
                f"TorBox file '{name}' is {size_gb:.1f} GiB ({size} bytes), above the "
                f"configured cap of {cap_gb:g} GiB (torbox_max_download_gb); refusing to "
                "download it."
            )

    def _plan_selection(
        self,
        release: Dict[str, Any],
        files: Any,
        wanted_volumes: Any,
        total_size: int = 0,
    ) -> Dict[str, Any]:
        """Decide exactly which TorBox files to fetch — safe by default.

        NovelTrax adaptation of the reference planner: when ownership cannot be
        determined safely it falls back to the whole-torrent zip (never skip, never
        download a subset it cannot justify).  Only a verifiable strict subset or a
        verifiably-needed small bundle downloads exact files.
        """
        release = release if isinstance(release, dict) else {}
        title = str(release.get("title") or "")
        series_name = str(release.get("series_name") or "").strip()
        strict = self._strict_selection()
        wanted = {round(float(value), 2) for value in (wanted_volumes or [])}
        file_items = [item for item in (files or []) if isinstance(item, dict)]

        plan: Dict[str, Any] = {
            "strict": strict,
            "wanted_volumes": sorted(wanted),
            "selected_volumes": [],
            "skipped_volumes": [],
            "selected_count": 0,
            "total_files": len(file_items),
            "mapping": [],
        }

        if not strict:
            return {
                **plan,
                "action": "zip",
                "files": None,
                "reason": "strict volume selection is disabled: whole-torrent zip",
            }
        if not wanted:
            return {
                **plan,
                "action": "zip",
                "files": None,
                "reason": "wanted volumes unknown: whole-torrent zip (never skipped)",
            }
        if not file_items:
            return {
                **plan,
                "action": "zip",
                "files": None,
                "reason": "TorBox returned no file list: whole-torrent zip",
            }

        mapping: List[Dict[str, Any]] = []
        novel_indices: List[int] = []
        unmapped: List[str] = []
        for index, item in enumerate(file_items):
            name = str(item.get("name") or item.get("short_name") or "")
            try:
                is_ancillary, volumes = parse_torrent_filename_volumes(name, series_name)
            except Exception:
                is_ancillary, volumes = False, set()
            volume_set = {round(float(value), 2) for value in (volumes or [])}
            mapping.append({
                "index": index,
                "id": item.get("id"),
                "name": name,
                "volumes": sorted(volume_set),
                "ancillary": bool(is_ancillary),
            })
            if is_ancillary:
                continue
            novel_indices.append(index)
            if not volume_set:
                unmapped.append(name or f"file #{index}")

        plan["mapping"] = mapping
        if not novel_indices:
            return {
                **plan,
                "action": "zip",
                "files": None,
                "reason": "no novel file names could be mapped to volumes: whole-torrent zip",
            }
        if unmapped:
            reason = (
                f"{len(unmapped)} file name(s) cannot be mapped to volumes "
                f"({', '.join(unmapped[:3])}): whole-torrent zip"
            )
            logger.info("TorBox selection for '%s': %s", title, reason)
            return {**plan, "action": "zip", "files": None, "reason": reason}

        available = set()
        for index in novel_indices:
            available.update(mapping[index]["volumes"])
        absent = sorted(wanted - available)
        if absent:
            reason = (
                f"wanted volume(s) {_fmt_volumes(absent)} are absent from the torrent: "
                "whole-torrent zip"
            )
            logger.info("TorBox selection for '%s': %s", title, reason)
            return {**plan, "action": "zip", "files": None, "reason": reason}

        selected_indices = [
            index for index in novel_indices if set(mapping[index]["volumes"]) & wanted
        ]
        covered = set()
        for index in selected_indices:
            covered.update(mapping[index]["volumes"])
        all_needed = (
            len(selected_indices) == len(novel_indices)
            and (covered & wanted) == wanted
        )
        selected_files = [file_items[index] for index in selected_indices]
        max_bytes = self._max_download_bytes()
        estimated_total = _coerce_int(total_size, 0)
        if not estimated_total:
            estimated_total = sum(_coerce_int(item.get("size"), 0) for item in file_items)

        result = {
            **plan,
            "selected_volumes": sorted(covered & wanted),
            "skipped_volumes": sorted(available - wanted),
            "selected_count": len(selected_files),
        }

        if all_needed:
            if (
                len(novel_indices) <= TORBOX_ZIP_MAX_FILES
                and (
                    not estimated_total
                    or estimated_total <= int(max_bytes * TORBOX_ZIP_SAFETY_RATIO)
                )
            ):
                return {
                    **result,
                    "action": "zip",
                    "files": None,
                    "reason": f"all {len(novel_indices)} needed file(s): whole-torrent zip",
                }
            for item in selected_files:
                self._check_file_size(item, max_bytes)
            return {
                **result,
                "action": "files",
                "files": selected_files,
                "reason": (
                    f"all {len(novel_indices)} file(s) needed but the archive is "
                    f"{estimated_total / (1024 ** 3):.1f} GiB / {len(novel_indices)} file(s); "
                    "fetching the exact files per-file"
                ),
            }

        for item in selected_files:
            self._check_file_size(item, max_bytes)
        return {
            **result,
            "action": "files",
            "files": selected_files,
            "reason": (
                f"strict subset: {len(selected_files)}/{len(file_items)} file(s) selected, "
                f"{len(result['skipped_volumes'])} owned volume(s) skipped"
            ),
        }

    def _fetch_release(
        self, release: Dict[str, Any], downloader: Any, handle: Dict[str, Any], info: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Fetch one ready release into ``<downloads_dir>/<safe title>/`` (contained)."""
        release = release if isinstance(release, dict) else {}
        info = info if isinstance(info, dict) else {}
        release_id = _coerce_int(release.get("id"), 0)
        title = str(release.get("title") or "")
        reason = ""
        dest_dir = ""
        try:
            # Safety net for the poller's media guard: never fetch a .cbz-only
            # torrent (manga rip) into the novel downloads folder.
            manga_detection = _cbz_only_manga(info.get("files"))
            if manga_detection["manga_only"]:
                self._reject_cbz_manga_torrent(
                    release_id, title, downloader, handle, manga_detection,
                )
                return {"status": "rejected_manga"}

            dest_dir = os.path.join(self.downloads_dir(), _safe_dirname(title))
            try:
                os.makedirs(dest_dir, exist_ok=True)
            except OSError as exc:
                raise DownloaderFileError(self._filesystem_error("create directory", dest_dir, exc)) from exc

            wanted = self._resolve_wanted_volumes(release)
            files = info.get("files") or []
            plan = self._plan_selection(release, files, wanted, total_size=_coerce_int(info.get("size"), 0))
            reason = str(plan.get("reason") or "")

            if plan.get("action") == "skip":
                try:
                    downloader.cancel(handle)
                except Exception as exc:
                    logger.debug("Could not cancel skipped TorBox job: %s", exc)
                self.db.update_downloader_state(release_id, "IDLE", "torbox")
                self.db.update_torbox_progress(release_id, phase="done")
                self._log_activity(
                    "SELECTIVE_DOWNLOAD",
                    f"TorBox skipped '{title}': {reason}.",
                    json.dumps({"wanted_volumes": [], "torrent_id": handle.get("torrent_id")}),
                )
                return {"status": "skipped"}

            selection = plan.get("files") if plan.get("action") == "files" else None
            total_bytes = _coerce_int(info.get("size"), 0)
            if selection:
                selected_total = sum(_coerce_int(item.get("size"), 0) for item in selection)
                if selected_total:
                    total_bytes = selected_total

            try:
                progress = self.db.get_torbox_progress(release_id)
            except Exception:
                progress = {}
            ready_at = progress.get("ready_detected_at") or info.get("ready_detected_at") or _utc_iso()
            fetch_started_mono = time.monotonic()
            fetch_started_iso = _utc_iso()
            try:
                self.db.update_torbox_progress(
                    release_id,
                    phase="fetching",
                    ready_detected_at=ready_at,
                    fetch_started_at=fetch_started_iso,
                    total_bytes=total_bytes or None,
                    downloaded_bytes=0,
                    speed_bps=0,
                    progress_pct=0.0,
                )
            except Exception as exc:
                logger.debug("Could not persist TorBox fetch start: %s", exc)

            fetch_handle = dict(handle or {})
            fetch_handle["size"] = int(total_bytes or 0)
            saved = downloader.fetch_files(
                fetch_handle,
                selection,
                dest_dir,
                progress_cb=self._progress_callback(release_id, total_bytes),
            )
            fetch_finished_mono = time.monotonic()
            fetch_finished_iso = _utc_iso()
            fetch_seconds = max(0.001, fetch_finished_mono - fetch_started_mono)

            downloaded_bytes = 0
            for item in saved or []:
                if isinstance(item, dict):
                    downloaded_bytes += _coerce_int(item.get("size"), 0)
            if not downloaded_bytes:
                downloaded_bytes = total_bytes
            total_bytes = total_bytes or downloaded_bytes
            speed_bps = int(downloaded_bytes / fetch_seconds) if downloaded_bytes else 0
            paths = [str(item.get("path") or "") for item in (saved or []) if isinstance(item, dict)]

            try:
                self.db.set_download_files(release_id, paths)
            except Exception as exc:
                logger.debug("Could not persist download files for release %s: %s", release_id, exc)
            try:
                self.db.update_torbox_progress(
                    release_id,
                    phase="routing",
                    downloaded_bytes=downloaded_bytes,
                    total_bytes=total_bytes or None,
                    speed_bps=speed_bps,
                    fetch_finished_at=fetch_finished_iso,
                    progress_pct=100.0,
                )
                self.db.clear_download_error(release_id)
                self.db.update_downloader_state(release_id, "COMPLETED", "torbox")
                self.db.mark_release_downloaded(release_id)
                try:
                    from app.epub_prefill import prepare_downloaded_epub
                    release = self.db.get_release_by_id(release_id)
                    for path in paths:
                        if path.lower().endswith(".epub"):
                            prepare_downloaded_epub(self.db, release or {}, path)
                except Exception as meta_exc:
                    logger.debug("EPUB metadata prefill failed for release %s: %s", release_id, meta_exc)
                self.db.update_torbox_progress(release_id, phase="done")
            except Exception as exc:
                logger.warning("Could not persist TorBox completion for release %s: %s", release_id, exc)

            if self._delete_after_download() and handle:
                try:
                    downloader.cancel(handle)
                except Exception as exc:
                    logger.debug("Could not delete the TorBox torrent after fetch: %s", exc)

            latency = {
                "ready_detected_at": ready_at,
                "fetch_started_at": fetch_started_iso,
                "fetch_finished_at": fetch_finished_iso,
                "ready_to_fetch_seconds": round(_seconds_between(ready_at, fetch_started_iso), 1),
                "fetch_seconds": round(fetch_seconds, 1),
                "downloaded_bytes": downloaded_bytes,
                "total_bytes": total_bytes,
                "speed_bps": speed_bps,
            }
            self._log_activity(
                "TORBOX_DOWNLOADED",
                f"TorBox download complete: '{title}'",
                json.dumps({
                    "message": (
                        f"Saved {len(paths)} file(s) to {dest_dir} ({reason}); "
                        f"fetch {latency['fetch_seconds']:.1f}s "
                        f"({downloaded_bytes / (1024 ** 3):.2f} GiB at "
                        f"{speed_bps / (1024 ** 2):.1f} MiB/s)"
                    ),
                    "latency": latency,
                    "files": paths,
                }),
            )
            return {"status": "completed", "latency": latency, "files": paths}
        except DownloaderError as exc:
            message = f"Fetch failed ({reason}): {exc}" if reason else f"Fetch failed: {exc}"
            logger.warning("TorBox fetch failed for release %s (%s): %s", release_id, title, exc)
            self._cleanup_partials(dest_dir)
            self._fail_release(release_id, message, downloader=downloader, handle=handle)
            try:
                self.db.update_torbox_progress(release_id, phase="failed")
            except Exception:
                pass
            self._notified.pop(release_id, None)
            return {"status": "failed", "error": str(exc)}
        except OSError as exc:
            message = f"Fetch failed ({reason}): {self._filesystem_error('write into', dest_dir, exc)}" \
                if reason else f"Fetch failed: {self._filesystem_error('write into', dest_dir, exc)}"
            logger.warning("TorBox fetch I/O failed for release %s (%s): %s", release_id, title, exc)
            self._cleanup_partials(dest_dir)
            self._fail_release(release_id, message, downloader=downloader, handle=handle)
            try:
                self.db.update_torbox_progress(release_id, phase="failed")
            except Exception:
                pass
            self._notified.pop(release_id, None)
            return {"status": "failed", "error": str(exc)}
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "TorBox fetch crashed for release %s (%s): %s",
                release_id, title, message, exc_info=True,
            )
            self._cleanup_partials(dest_dir)
            self._fail_release(release_id, message, downloader=downloader, handle=handle)
            try:
                self.db.update_torbox_progress(release_id, phase="failed")
            except Exception:
                pass
            self._notified.pop(release_id, None)
            return {"status": "failed", "error": message}

    # -- failure / status helpers ------------------------------------------
    def _fail_release(
        self,
        release_id: int,
        reason: str,
        downloader: Any = None,
        handle: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mark a release FAILED, persist the visible reason and free the slot."""
        try:
            release_id = int(release_id or 0)
        except (TypeError, ValueError):
            release_id = 0
        reason = str(reason or "TorBox download failed.")
        title = ""
        try:
            row = self.db.get_release_by_id(release_id) or {}
            title = str(row.get("title") or "")
        except Exception:
            row = {}
        try:
            attempts = self.db.increment_download_attempt(release_id)
        except Exception:
            attempts = 0
        stored_reason = f"{reason} (attempt {attempts}, {_utc_stamp()} UTC)"
        remote = ""
        if self._delete_on_failure():
            target_handle = handle or self._handle_from_row(row)
            if target_handle and (
                target_handle.get("torrent_id") or target_handle.get("queued_id")
            ):
                try:
                    engine = downloader or self.get_downloader("torbox")
                    remote = (
                        " Removed from TorBox." if engine.cancel(target_handle)
                        else " TorBox remove failed."
                    )
                except Exception as exc:
                    logger.debug("Could not delete failed TorBox job %s: %s", release_id, exc)
                    remote = " TorBox remove failed."
        for action in (
            lambda: self.db.set_download_error(release_id, stored_reason),
            lambda: self.db.update_downloader_state(release_id, "FAILED", "torbox"),
        ):
            try:
                action()
            except Exception as exc:
                logger.warning("Could not persist TorBox failure for release %s: %s", release_id, exc)
        self._log_activity(
            "TORBOX_FAILED",
            f"TorBox download failed: '{title}'",
            f"{reason}{remote}",
        )
        logger.warning("TorBox download failed for '%s': %s%s", title, reason, remote)

    # -- status payload -----------------------------------------------------
    def _reject_cbz_manga_torrent(
        self,
        release_id: int,
        title: str,
        downloader: Any,
        handle: Optional[Dict[str, Any]],
        detection: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Reject a ``.cbz``-only torrent as manga (never download it).

        A TorBox file list that contains only ``.cbz`` content files is a manga
        rip, not a light novel. The remote torrent is deleted (freeing the active
        slot), the release is re-classified as ``manga`` and ignored/hidden
        (status IGNORED + ignored=1, which every tracking view excludes and later
        feed re-polls cannot undo), and the submission bookkeeping is reset to
        IDLE. The reason is audited once as ``TORBOX_MANGA_REJECTED``.
        """
        try:
            release_id = int(release_id or 0)
        except (TypeError, ValueError):
            release_id = 0
        title = str(title or "")
        detection = detection if isinstance(detection, dict) else _cbz_only_manga([])
        remote = ""
        if downloader is not None and handle:
            try:
                remote = (
                    " Removed from TorBox." if downloader.cancel(handle)
                    else " TorBox remove failed."
                )
            except Exception as exc:
                logger.debug("Could not delete manga-only TorBox job %s: %s", release_id, exc)
                remote = " TorBox remove failed."
        detail = json.dumps(
            {
                "release_id": release_id,
                "cbz_files": detection.get("cbz_count"),
                "content_files": detection.get("content_count"),
                "ignored_files": detection.get("ignored_count"),
                "sample_files": detection.get("files") or [],
            }
        )
        try:
            self.db.update_release_media_type(release_id, "manga")
        except Exception as exc:
            logger.warning("Could not classify release %s as manga: %s", release_id, exc)
        try:
            self.db.update_torbox_progress(release_id, phase="done", rejected_as="manga")
        except Exception:
            pass
        try:
            self.db.clear_downloader_submission(release_id, "torbox")
        except Exception as exc:
            logger.warning(
                "Could not reset the manga-only TorBox submission %s: %s", release_id, exc,
            )
        # Hide this specific release from now on. ``ignore_release`` sets
        # status=IGNORED + ignored=1, which every view excludes and which the
        # status-precedence rules never lift on a later feed re-poll (only an
        # explicit user un-ignore restores it). Hiding the release itself (not a
        # series blacklist) is exactly what the user asked for: a .cbz torrent is
        # a manga rip, so that release is noise for a light-novel tracker.
        hidden = False
        try:
            hidden = bool(self.db.ignore_release(release_id))
        except Exception as exc:
            logger.warning("Could not hide manga-only release %s: %s", release_id, exc)
        detail = json.dumps(
            {
                "release_id": release_id,
                "cbz_files": detection.get("cbz_count"),
                "content_files": detection.get("content_count"),
                "ignored_files": detection.get("ignored_count"),
                "sample_files": detection.get("files") or [],
                "hidden": hidden,
            }
        )
        self._notified.pop(release_id, None)
        self._log_activity(
            "TORBOX_MANGA_REJECTED",
            f"TorBox rejected .cbz-only torrent as manga and hid the release: '{title}'",
            detail + remote,
        )
        logger.info(
            "TorBox rejected a .cbz-only torrent as manga for release %s (%s); release hidden=%s.",
            release_id, title, hidden,
        )

    def _status_row(self, release: Dict[str, Any], info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Normalise one release row (+ optional live status) for the status API."""
        release = release if isinstance(release, dict) else {}
        info = info if isinstance(info, dict) else {}
        try:
            progress_data = json.loads(release.get("torbox_progress_json") or "{}")
        except (TypeError, ValueError):
            progress_data = {}
        if not isinstance(progress_data, dict):
            progress_data = {}

        local_phase = str(progress_data.get("phase") or "")
        db_status = str(release.get("download_status") or "").upper()

        # The DB is authoritative for failures: TorBox may still report `ready` for a
        # torrent we refused locally (size cap, bad mapping), which used to look stuck.
        state = "failed" if db_status == "FAILED" else str(info.get("state") or "queued")
        if local_phase in ("fetching", "routing", "processing") and db_status != "FAILED":
            state = local_phase

        files = info.get("files") or []
        live_progress = max(0.0, min(1.0, _coerce_float(info.get("progress"), 0.0)))
        live_speed = max(0, _coerce_int(info.get("download_speed"), 0))
        live_eta = info.get("eta")
        live_eta = max(0, _coerce_int(live_eta, 0)) if isinstance(live_eta, (int, float)) else 0

        local_total = _coerce_int(progress_data.get("total_bytes"), 0) or _coerce_int(info.get("size"), 0)
        local_downloaded = _coerce_int(progress_data.get("downloaded_bytes"), 0)
        local_speed = max(0, _coerce_int(progress_data.get("speed_bps"), 0))

        if local_phase == "fetching" and local_total:
            progress = local_downloaded / local_total
            speed = local_speed or live_speed
            eta = (
                int((local_total - local_downloaded) / speed)
                if speed > 0 and local_total > local_downloaded else 0
            )
            downloaded_bytes = local_downloaded
            total_bytes = local_total
            speed_bps = local_speed
        else:
            progress = live_progress
            speed = live_speed
            eta = live_eta
            size = _coerce_int(info.get("size"), 0)
            downloaded_bytes = int(round(size * progress))
            total_bytes = local_total or size
            speed_bps = live_speed
        progress = max(0.0, min(1.0, progress))
        size_bytes = _coerce_int(info.get("size"), 0) or total_bytes

        try:
            last_checked_epoch = float(self.db.get_setting("torbox_last_poll_at", "0") or 0)
        except (TypeError, ValueError):
            last_checked_epoch = 0.0
        last_checked_iso = _iso_from_epoch(last_checked_epoch)
        last_checked_seconds_ago = (
            max(0, int(time.time() - last_checked_epoch)) if last_checked_epoch else None
        )

        wanted_volumes = self._json_float_list(release.get("download_wanted_json"))
        if not wanted_volumes:
            wanted_volumes = self._json_float_list(release.get("missing_vols_json"))
        strict_volumes = self._strict_selection()
        if wanted_volumes:
            selection = "missing volumes " + _fmt_volumes(wanted_volumes)
        elif not strict_volumes:
            selection = "legacy whole-torrent"
        else:
            selection = "all volumes"

        failed = state == "failed" or db_status == "FAILED"
        error = str(release.get("download_error") or "").strip()
        if failed and not error:
            error = f"TorBox reported state '{info.get('raw_state') or 'failed'}'."
        can_retry = bool(failed)

        return {
            "release_id": release.get("id"),
            "hash": str(release.get("torrent_hash") or release.get("downloader_id") or ""),
            "name": str(release.get("title") or ""),
            "engine": "torbox",
            "state": state,
            "failed": failed,
            "error": error,
            "attempts": _coerce_int(release.get("download_attempts"), 0),
            "can_retry": can_retry,
            "download_status": db_status,
            "phase": local_phase or ("done" if db_status == "COMPLETED" else "ready" if state == "ready" else ""),
            "progress_pct": round(progress * 100.0, 1),
            "downloaded_bytes": max(0, downloaded_bytes),
            "total_bytes": max(0, total_bytes),
            "speed_bps": max(0, speed_bps),
            "eta_seconds": max(0, _coerce_int(eta, 0)),
            "size": max(0, size_bytes),
            "size_bytes": max(0, size_bytes),
            "progress": round(progress, 4),
            "progress_percent": round(progress * 100.0, 1),
            "dlspeed": max(0, speed),
            "download_speed": max(0, speed),
            "upload_speed": 0,
            "ratio": 0.0,
            "seeds": max(0, _coerce_int(info.get("seeds"), 0)),
            "is_active": state in (
                "downloading", "ready", "processing", "fetching", "routing", "queued_slot",
            ),
            "queued": state == "queued_slot",
            "queue_position": info.get("queue_position"),
            "active_limit": info.get("active_limit"),
            "raw_state": str(info.get("raw_state") or ""),
            "cached": bool(info.get("cached")),
            "files": files,
            "selection": selection,
            "wanted_volumes": wanted_volumes,
            "downloaded_at": str(release.get("downloaded_at") or ""),
            "last_checked_at": last_checked_iso,
            "last_checked_seconds_ago": last_checked_seconds_ago,
        }

    def build_status(self, timeout: float = 1.5) -> Dict[str, Any]:
        """Server-side status payload for the Downloads page; never raises."""
        base: Dict[str, Any] = {
            "status": "degraded",
            "engine": "torbox",
            "generated_at": _utc_iso(),
            "live_state_available": False,
            "downloader": {
                "enabled": False,
                "connected": False,
                "version": "",
                "message": "",
                "plan": "",
                "active_slots": 0,
            },
            "torrents": [],
            "count": 0,
            "totals": {"download_speed": 0, "progress": 0.0, "eta_seconds": 0},
            "last_poll_at": None,
            "last_checked_at": None,
        }
        try:
            base["last_poll_at"] = self._last_poll_epoch()
            base["last_checked_at"] = _iso_from_epoch(base["last_poll_at"])
        except Exception:
            pass

        try:
            downloader = self.get_downloader("torbox")
        except Exception as exc:
            base["downloader"]["message"] = f"Could not build the TorBox client ({type(exc).__name__})."
            return base

        enabled = bool(getattr(downloader, "enabled", False))
        base["downloader"]["enabled"] = enabled
        if not enabled:
            base["status"] = "disabled"
            base["downloader"]["message"] = (
                "TorBox is disabled or no API key is configured (Settings → Downloader)."
            )
            base["torrents"] = self._status_db_rows(None)
            base["count"] = len(base["torrents"])
            return base

        degraded = False
        connection: Dict[str, Any] = {}
        try:
            connection = downloader.test_connection() or {}
        except DownloaderError as exc:
            connection = {"success": False, "message": str(exc)}
        except Exception as exc:
            connection = {
                "success": False,
                "message": f"Connection test failed ({type(exc).__name__}).",
            }
        connected = bool(connection.get("success"))
        base["downloader"].update({
            "connected": connected,
            "version": str(connection.get("version") or ""),
            "message": str(connection.get("message") or ""),
            "plan": str(connection.get("plan") or ""),
            "active_slots": max(0, _coerce_int(connection.get("active_slots"), 0)),
        })
        if not connected:
            degraded = True

        live: Dict[int, Dict[str, Any]] = {}
        base["torrents"] = self._status_db_rows(
            downloader if (connected and enabled) else None, live,
        )
        if live.get("_degraded"):
            degraded = True
        base["count"] = len(base["torrents"])
        base["live_state_available"] = bool(connected and not degraded)

        active_rows = [row for row in base["torrents"] if row.get("is_active")]
        total_size = sum(_coerce_int(row.get("total_bytes"), 0) for row in active_rows)
        downloaded = sum(_coerce_int(row.get("downloaded_bytes"), 0) for row in active_rows)
        base["totals"] = {
            "download_speed": sum(_coerce_int(row.get("speed_bps"), 0) for row in active_rows),
            "progress": round(downloaded / total_size, 4) if total_size else 0.0,
            "eta_seconds": max(
                [_coerce_int(row.get("eta_seconds"), 0) for row in active_rows] or [0]
            ),
        }

        base["status"] = "success" if not degraded else "degraded"
        return base

    def _last_poll_epoch(self) -> Optional[float]:
        try:
            value = float(self.db.get_setting("torbox_last_poll_at", "0") or 0)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _status_db_rows(
        self, downloader: Any = None, live: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """DB-derived rows; a live ``status()`` is called only for DOWNLOADING rows."""
        live = live if isinstance(live, dict) else {}
        try:
            rows = self.db.get_downloader_releases("torbox", limit=20)
        except Exception as exc:
            logger.debug("Could not read TorBox releases for status: %s", exc)
            return []
        result: List[Dict[str, Any]] = []
        for row in rows:
            info: Dict[str, Any] = {}
            if (
                downloader is not None
                and str(row.get("download_status") or "").upper() == "DOWNLOADING"
            ):
                handle = self._handle_from_row(row)
                if handle:
                    try:
                        info = downloader.status(handle) or {}
                    except DownloaderError as exc:
                        live["_degraded"] = True
                        info = {}
                        logger.debug(
                            "Live TorBox status failed for release %s: %s", row.get("id"), exc,
                        )
                    except Exception as exc:
                        live["_degraded"] = True
                        info = {}
                        logger.debug(
                            "Live TorBox status crashed for release %s: %s",
                            row.get("id"), type(exc).__name__,
                        )
            result.append(self._status_row(row, info))
        return result

    # -- retry / remove / clear --------------------------------------------
    def retry_release(self, release_id: int) -> Dict[str, Any]:
        """Cancel the stale TorBox job and resubmit a release (rate-limited)."""
        target = self.db.get_release_by_id(release_id)
        if not target:
            return {"status": "error", "message": "Release not found", "code": 404}
        if self.engine() != "torbox":
            return {
                "status": "error",
                "message": "TorBox is not the active download engine.",
                "code": 409,
            }
        now = time.time()
        last = self._retry_last.get(release_id, 0.0)
        if now - last < TORBOX_RETRY_MIN_SECONDS:
            return {
                "status": "error",
                "message": (
                    f"Retry rate-limited; wait {TORBOX_RETRY_MIN_SECONDS:g}s between retries."
                ),
                "code": 429,
            }
        self._retry_last[release_id] = now

        old_handle = self._handle_from_row(target)
        if old_handle:
            try:
                self.get_downloader("torbox").cancel(old_handle)
            except Exception as exc:
                logger.debug("Could not cancel stale TorBox job on retry: %s", exc)

        try:
            self.db.clear_download_error(release_id)
            self.db.update_downloader_state(release_id, "IDLE", "torbox")
        except Exception as exc:
            logger.warning("Could not reset release %s for a TorBox retry: %s", release_id, exc)

        result = dict(self.submit_release(target, source="retry"))
        if result.get("success"):
            result["status"] = "skipped" if result.get("skipped") else "success"
        else:
            result["status"] = "skipped" if result.get("skipped") else "error"
        return result

    def remove_release(self, release_id: int) -> Dict[str, Any]:
        """Remove a release from the TorBox queue (local cleanup always succeeds)."""
        target = self.db.get_release_by_id(release_id)
        if not target:
            return {"status": "error", "message": "Release not found", "code": 404}
        # FAILED rows already had their remote cleanup attempted when they failed;
        # re-cancelling can block on TorBox timeouts and makes Remove look dead.
        is_failed = str(target.get("download_status") or "").upper() == "FAILED"
        handle = {} if is_failed else self._handle_from_row(target)
        deleted = False
        remote_error = ""
        if handle:
            try:
                deleted = bool(self.get_downloader("torbox").cancel(handle))
            except Exception as exc:
                remote_error = str(exc)
                logger.debug("Could not remove TorBox job for release %s: %s", release_id, exc)
        self.db.clear_downloader_submission(release_id, "torbox")
        return {
            "status": "success",
            "removed": True,
            "remote_deleted": deleted,
            "remote_error": remote_error,
            "message": (
                "Removed failed item from the queue" if is_failed
                else (
                    "Removed from TorBox queue" if deleted
                    else "Removed from the queue (no active remote job)"
                )
            ),
        }

    def clear_failed(self) -> Dict[str, Any]:
        """Remove every FAILED torbox queue item locally (no remote waits)."""
        try:
            rows = self.db.get_downloader_releases("torbox", limit=500)
        except Exception as exc:
            logger.warning("Could not read TorBox releases for clear-failed: %s", exc)
            rows = []
        failed_rows = [
            row for row in rows
            if str(row.get("download_status") or "").upper() == "FAILED"
        ]
        removed = 0
        for row in failed_rows:
            try:
                self.db.clear_downloader_submission(row.get("id"), "torbox")
                removed += 1
            except Exception as exc:
                logger.warning("Could not clear failed TorBox release %s: %s", row.get("id"), exc)
        return {
            "status": "success",
            "failed_entries": len(failed_rows),
            "removed": removed,
            "remote_deleted": 0,
        }

    # -- connection test ----------------------------------------------------
    def test_connection(self, api_key: Optional[str] = None) -> Dict[str, Any]:
        """Test TorBox connectivity (never raises; optional transient key)."""
        try:
            key = str(api_key or "").strip()
            if key:
                downloader = self._build_transient_downloader(key)
            else:
                downloader = self.get_downloader("torbox")
            result = downloader.test_connection() or {}
        except DownloaderError as exc:
            return {"success": False, "engine": "torbox", "message": str(exc),
                    "plan": "", "active_slots": 0}
        except Exception as exc:
            return {
                "success": False,
                "engine": "torbox",
                "message": f"TorBox connection test failed ({type(exc).__name__}).",
                "plan": "",
                "active_slots": 0,
            }
        if not isinstance(result, dict):
            result = {"success": bool(result)}
        payload = {
            "success": bool(result.get("success")),
            "engine": "torbox",
            "version": str(result.get("version") or ""),
            "message": str(result.get("message") or ""),
            "plan": str(result.get("plan") or ""),
            "active_slots": max(0, _coerce_int(result.get("active_slots"), 0)),
        }
        return payload

    def _build_transient_downloader(self, api_key: str) -> Any:
        """Build a throwaway TorBox client for a user-supplied (possibly unsaved) key."""
        from app.downloader.torbox import TorboxDownloader

        max_download_gb = 64
        try:
            max_download_gb = float(self.db.get_setting("torbox_max_download_gb", "64") or 64)
        except (TypeError, ValueError):
            pass
        max_download_gb = max(1.0, min(max_download_gb, 10240.0))
        concurrency = _clamp_int(
            self.db.get_setting("torbox_download_concurrency", "4"), 1, 16, 4,
        )
        chunks = _clamp_int(self.db.get_setting("torbox_parallel_chunks", "4"), 1, 8, 4)
        return TorboxDownloader(
            api_key=api_key,
            enabled=True,
            max_file_bytes=int(max_download_gb * 1024 ** 3),
            max_concurrency=concurrency,
            parallel_chunks=chunks,
        )


__all__ = ["TorBoxManager", "TORBOX_TORRENT_ONLY_MESSAGE", "DEFAULT_DOWNLOADS_DIR"]
