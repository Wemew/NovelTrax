import os
import json
import logging
import math
import threading
import time
import concurrent.futures
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError:
    class BackgroundScheduler:
        def __init__(self, *args, **kwargs):
            self.running = False
            self.jobs = {}
        def add_job(self, func, trigger=None, **kwargs):
            self.jobs[kwargs.get("id", str(func))] = kwargs
        def reschedule_job(self, job_id, trigger=None, **kwargs):
            if job_id in self.jobs:
                self.jobs[job_id].update(kwargs)
        def start(self):
            self.running = True
        def shutdown(self, wait=False):
            self.running = False
from app import background_tasks
from app.database import Database
from app.calibre.scanner import CalibreScanner
from app.calibre.matcher import CalibreMatcher
from app.providers.registry import create_provider
from app.providers.catchup import (
    DEFAULT_CATCHUP_MAX_PAGES,
    clamp_max_pages,
    compute_scan_watermarks,
)
from app.notifier.webhook import NotificationService
from app.covers import CoverCache
from app.downloader.qbit import qbit_client_from_settings

logger = logging.getLogger("noveltrax.scheduler")

#: Settings keys for the time-aware catch-up scan.  Read with hard-coded
#: defaults (nothing is seeded into app_settings) so existing databases get the
#: feature immediately; an invalid value falls back to the default.
FEED_CATCHUP_ENABLED_KEY = "feed_catchup_enabled"
FEED_CATCHUP_MAX_PAGES_KEY = "feed_catchup_max_pages"

#: Automatic activity-log pruning settings + schedule (contract v2 §6.1/§6.3).
ACTIVITY_RETENTION_DAYS_KEY = "activity_retention_days"
ACTIVITY_MAX_ROWS_KEY = "activity_max_rows"
ACTIVITY_RETENTION_DAYS_DEFAULT = 90
ACTIVITY_MAX_ROWS_DEFAULT = 50000
ACTIVITY_PRUNE_INTERVAL_MINUTES = 360

#: qBittorrent selective file-priority refresh cadence. Torrent metadata can
#: arrive after submit; the pass retries until file names can be mapped.
SELECTIVE_FILTER_INTERVAL_SECONDS = 30

#: Webhook event toggles (contract v2 §6.5). Read once per scan/loop; the
#: default is enabled so existing installs keep sending notifications.
WEBHOOK_NEW_REQUEST_KEY = "webhook_new_request_notifications"
WEBHOOK_ACQUIRED_KEY = "webhook_acquired_notifications"
_DISABLED_SETTING_VALUES = frozenset({"false", "0", "no", "off"})

#: Series identity / AniList title sync settings.  All are read with hard-coded
#: defaults (never seeded into app_settings) so an existing install gets the
#: feature without a migration; "false"/"0"/"no"/"off" disables a boolean.
SERIES_ANILIST_SYNC_ENABLED_KEY = "series_anilist_sync_enabled"
SERIES_ANILIST_SYNC_LIMIT_KEY = "series_anilist_sync_limit"
SERIES_ANILIST_MATCH_ENABLED_KEY = "series_anilist_match_enabled"
SERIES_ANILIST_MATCH_BUDGET_KEY = "series_anilist_match_budget"
SERIES_ANILIST_SYNC_LIMIT_DEFAULT = 15
SERIES_ANILIST_MATCH_BUDGET_DEFAULT = 10

#: How long a successful per-series AniList title lookup stays hot in process.
SERIES_ALT_TITLE_TTL_SECONDS = 6 * 60 * 60

#: Nyaa unmarked-bundle volume inference (contract v3 §4). Unmarked Nyaa
#: light-novel batch releases are treated as the complete series when AniList
#: reports Format = Light Novel, Status = Finished and a volume count.
NYAA_BUNDLE_VOLUME_INFERENCE_KEY = "nyaa_bundle_volume_inference"
PUBLICATION_CACHE_TTL_SECONDS = 6 * 60 * 60
NYAA_FEED_TYPES = frozenset({"nyaa", "nyaa_novel", "nyaa_scraper", "nyaa.si"})
#: One-off provider budget for publication lookups when the caller passes None.
PUBLICATION_ONE_OFF_BUDGET = 3

#: TTL for the in-process Calibre library snapshot used by the re-bind status/
#: reconcile path (contract v5 §6). A Calibre scan is comparatively expensive and
#: a re-bind reads the library several times; one read per 10 s is enough because
#: re-bind does not modify Calibre (sync_calibre/recheck_series_calibre invalidate).
CALIBRE_SNAPSHOT_TTL_SECONDS = 10.0


class TaskManager:
    def __init__(self, db: Database, calibre_path: str, data_dir: Optional[str] = None):
        self.db = db
        self.calibre_path = calibre_path
        default_data_dir = os.getenv("DATA_DIR") or os.path.dirname(os.path.abspath(db.db_path))
        self.cover_cache = CoverCache(data_dir or default_data_dir)
        self.scheduler = BackgroundScheduler()
        self._cover_executor = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="noveltrax-covers")
        self.is_syncing_calibre = False
        self.is_checking_feeds = False
        self.is_backfilling = False
        self.stop_backfill_requested = False
        self.media_resolver = self._build_media_resolver()
        # In-process TTL cache for AniList title lookups (normalized series name
        # -> {"titles": [...], "ts": epoch}). Prevents a per-release network hit
        # during a big scan and across scans; the SQLite metadata cache is still
        # consulted first, so a restart is cheap.
        self._series_alt_cache: Dict[str, Dict[str, Any]] = {}
        # In-process TTL cache for AniList publication metadata (format/status/
        # volumes), normalized series name -> {"data": pub, "ts": epoch}.
        self._publication_cache: Dict[str, Dict[str, Any]] = {}
        # Lock-protected TTL cache for the Calibre library snapshot (series +
        # books) used by the re-bind status/reconcile path. Scans are expensive;
        # a re-bind reads the library several times but never changes it.
        self._calibre_snapshot_lock = threading.Lock()
        self._calibre_snapshot_refresh_lock = threading.Lock()
        self._calibre_snapshot_cache: Dict[str, Any] = {
            "ts": 0.0, "series": [], "books": [],
        }
        # Releases whose qBittorrent file priorities were already resolved, keyed
        # by (release_id, torrent_hash, wanted tuple). Metadata can take a few
        # seconds to resolve, so the 30 s pass retries until it can decide, then
        # stops re-calling the filePrio endpoint for the same wanted set.
        self._selective_filter_done: set = set()

    def _task_report(self, **kwargs) -> None:
        reporter = getattr(self, "_task_reporter", None)
        if reporter is not None:
            try:
                reporter.report(**kwargs)
            except Exception:
                pass

    def _build_media_resolver(self):
        from app.media_type import MediaTypeResolver
        enabled = self.db.get_setting("filter_non_novels", "true").strip().lower() != "false"
        try:
            budget = int(self.db.get_setting("media_verify_budget", "25"))
        except Exception:
            budget = 25
        resolver = MediaTypeResolver(self.db, enabled=enabled)
        resolver.begin_run(budget)
        return resolver

    def classify_media(self, release) -> str:
        """
        Resolves a release's media type (novel / audiobook / manga / unknown).

        Manga must never enter the database; audiobooks are stored but excluded
        from the tracking views. Anything inconclusive is kept as 'unknown'.
        """
        from app.media_type import UNKNOWN

        resolver = getattr(self, "media_resolver", None)
        if resolver is None:
            return UNKNOWN
        title = release.get("title", "") if isinstance(release, dict) else getattr(release, "title", "")
        link = release.get("link", "") if isinstance(release, dict) else getattr(release, "link", "")
        guid = release.get("guid", "") if isinstance(release, dict) else getattr(release, "guid", "")
        download = release.get("download_url", "") if isinstance(release, dict) else getattr(release, "download_url", "")
        try:
            return resolver.resolve(guid or "", title or "", link, download)
        except Exception as exc:
            logger.debug(f"Media classification failed for {title!r}: {exc}")
            return UNKNOWN


    def get_notifier(self) -> NotificationService:
        discord_url = self.db.get_setting("discord_webhook_url", "")
        generic_url = self.db.get_setting("apprise_url", "")
        return NotificationService(discord_webhook=discord_url, generic_webhook=generic_url)

    # --- Chapter handling & release enrichment (spec §12.2 / §12.8 / §12.11) ---

    def chapter_context(self) -> set:
        """
        Normalised names of series we already know: the Calibre library plus the
        tracked rows. Used to decide whether a bare-numbered release is a chapter
        of a known series or the only instalment of a series we have never seen.
        """
        from app.parser import normalize_text

        names: set = set()
        try:
            calibre_dir = self.db.get_setting("calibre_path", self.calibre_path)
            scanner = CalibreScanner(calibre_dir)
            if scanner.is_available():
                for entry in scanner.get_all_series():
                    if entry.get("name"):
                        names.add(normalize_text(entry["name"]))
        except Exception as exc:
            logger.debug(f"Chapter context: Calibre lookup skipped: {exc}")
        try:
            for row in self.db.get_all_series(monitored_only=False):
                if row.get("name"):
                    names.add(normalize_text(row["name"]))
        except Exception as exc:
            logger.debug(f"Chapter context: tracked-series lookup skipped: {exc}")
        return names

    def should_discard_chapter(self, title: str, known_series: Optional[set] = None) -> bool:
        """
        Context-guarded chapter discard (spec §12.2).

        * an explicit chapter marker ("Ch. 179", "c374") is always a chapter;
        * a bare trailing number is a chapter only when stripping it yields a
          series we already know -- otherwise it may be the first volume of a
          series we have never seen, so it is kept (with the number exposed as
          chapter data, not as a volume).
        """
        if not title:
            return False
        try:
            from app.parser import normalize_text, _CHAPTER_MARKER_RE, _VOLUME_KEYWORD_RE
        except Exception:
            return False

        setting = (self.db.get_setting("discard_unmarked_chapters", "true") or "").strip().lower()
        if setting == "false":
            return False

        try:
            from app.parser import extract_series_and_volume, is_chapter_release
            if not is_chapter_release(title):
                return False
            parsed = extract_series_and_volume(title)
        except Exception:
            return False

        base_series = (parsed.get("series_name") or "").strip()
        explicit_marker = bool(_CHAPTER_MARKER_RE.search(title)) and not _VOLUME_KEYWORD_RE.search(title)
        if explicit_marker:
            logger.debug(f"Discarding chapter (explicit marker): {title!r}")
            return True
        if not base_series:
            return False
        if known_series is None:
            # No context available: keep (never guess).
            return False
        if normalize_text(base_series) in known_series:
            logger.debug(f"Discarding chapter of known series '{base_series}': {title!r}")
            return True
        logger.info(f"Keeping unmarked number for unknown series '{base_series}': {title!r}")
        return False

    def filter_chapter_releases(self, releases: List[Any], known_series: Optional[set] = None) -> List[Any]:
        """
        Drops chapter instalments from a provider batch.

        With ``known_series`` (normal ingestion) the discard is context-guarded;
        without it the strict rule applies (every bare-numbered release is a
        chapter), which keeps legacy callers and tests deterministic.
        """
        from app.parser import is_chapter_release

        kept: List[Any] = []
        discarded: List[str] = []
        for item in releases:
            title = item.get("title", "") if isinstance(item, dict) else getattr(item, "title", "")
            if known_series is None:
                if is_chapter_release(title):
                    discarded.append(title)
                    continue
            elif self.should_discard_chapter(title, known_series):
                discarded.append(title)
                continue
            kept.append(item)

        if discarded:
            # Discards must be auditable rather than silent (spec §12.2). One
            # summary event per batch avoids flooding the activity log on
            # chapter-heavy feeds; the release names are included (capped).
            preview = "; ".join(discarded[:10])
            suffix = "" if len(discarded) <= 10 else f" … (+{len(discarded) - 10} more)"
            try:
                self.db.log_activity(
                    "CHAPTER_DISCARDED",
                    f"Discarded {len(discarded)} chapter release(s)",
                    preview + suffix
                )
            except Exception as exc:
                logger.debug(f"Chapter discard activity log failed: {exc}")
            logger.info(f"Chapter filter: discarded {len(discarded)} release(s), kept {len(kept)}.")
        return kept

    def _enrich_release(self, rel_dict: dict) -> dict:
        """
        Adds the consolidated detection fields to a release payload before it is
        persisted: group set, publisher, revision marker, chapter data.

        Degrades gracefully while optional parser helpers are unavailable.
        """
        title = rel_dict.get("title") or ""
        if not title:
            return rel_dict

        try:
            from app.parser import extract_series_and_volume
            parsed = extract_series_and_volume(title)
            chapter_number = parsed.get("chapter_number")
            if chapter_number is None and parsed.get("is_chapter"):
                # Pre-consolidation parsers only expose the number as `volume`.
                chapter_number = parsed.get("volume")
            rel_dict.setdefault("chapter_number", chapter_number)
            rel_dict.setdefault("chapter_ranges", parsed.get("chapter_ranges") or [])
            # A retained chapter of an unknown series must not masquerade as a volume.
            if parsed.get("is_chapter") and chapter_number is not None and rel_dict.get("volume") is not None:
                same = abs(float(rel_dict.get("volume") or 0) - float(chapter_number or 0)) < 0.001
                if same:
                    rel_dict["volume"] = None
                    rel_dict["is_bundle"] = False
        except Exception as exc:
            logger.debug(f"Enrich: parse failed for {title!r}: {exc}")

        if not rel_dict.get("release_groups"):
            groups: List[str] = []
            try:
                from app.parser import extract_release_groups
                groups = list(extract_release_groups(title) or [])
            except Exception:
                groups = []
            if not groups and rel_dict.get("release_group"):
                groups = [rel_dict["release_group"]]
            rel_dict["release_groups"] = groups
            if groups and not rel_dict.get("release_group"):
                rel_dict["release_group"] = groups[0]

        if not rel_dict.get("publisher"):
            try:
                from app.parser import extract_publisher
                rel_dict["publisher"] = extract_publisher(title) or ""
            except Exception:
                rel_dict["publisher"] = ""

        if "revision" not in rel_dict:
            try:
                from app.parser import parse_revision_marker
                marker = parse_revision_marker(title) or {}
                rel_dict["revision"] = int(marker.get("revision") or 0)
            except Exception:
                rel_dict["revision"] = 0
        return rel_dict

    def _upsert(self, rel_dict: dict):
        """Enriches a payload with the consolidated detection fields, then persists it."""
        return self.db.upsert_release(self._enrich_release(rel_dict))

    # --- Series identity / Calibre auto-link / AniList title integration -------
    #
    # Contract: .agents/series_identity/CONTRACT.md §5.2-§5.5.  The matching
    # rules themselves live in app.series_identity (owner A); everything here is
    # the scheduler glue: a per-scan snapshot, canonicalisation of release series
    # names, bounded AniList lookups, best-effort enrichment and status
    # reconciliation.  Every helper is best-effort: a metadata outage or a
    # partially-migrated database must never break a Calibre sync or feed scan.

    @staticmethod
    def _resolution_value(resolution: Any, key: str, default: Any = None) -> Any:
        """Reads a field from a SeriesResolution that may be a dict or an object."""
        if resolution is None:
            return default
        try:
            if isinstance(resolution, dict):
                value = resolution.get(key, default)
            else:
                value = getattr(resolution, key, default)
                if callable(value):
                    value = value()
            return default if value is None else value
        except Exception:
            return default

    @staticmethod
    def _parse_anilist_meta(row: Any) -> Optional[dict]:
        """Parses a series_tracking row's anilist_meta_json into a dict."""
        if not isinstance(row, dict):
            return None
        raw = row.get("anilist_meta_json")
        if not raw:
            return None
        try:
            meta = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return meta if isinstance(meta, dict) else None

    @staticmethod
    def _anilist_titles(meta: Optional[dict]) -> List[str]:
        """Returns the AniList title set (english/title/romaji/native/synonyms)."""
        try:
            from app.series_identity import anilist_title_list
            return [str(t).strip() for t in anilist_title_list(meta) if str(t or "").strip()]
        except Exception:
            if not isinstance(meta, dict):
                return []
            titles: List[str] = []
            for key in ("english_title", "title", "romaji_title", "native_title"):
                value = meta.get(key)
                if value and str(value).strip():
                    titles.append(str(value).strip())
            synonyms = meta.get("synonyms")
            if isinstance(synonyms, list):
                titles.extend(str(s).strip() for s in synonyms if str(s or "").strip())
            return titles

    @staticmethod
    def _dedupe_titles(titles: List[str], exclude_norm: Optional[str] = None) -> List[str]:
        """Order-preserving de-duplication of candidate titles."""
        from app.parser import normalize_text
        seen = set()
        cleaned: List[str] = []
        for title in titles or []:
            text = str(title or "").strip()
            if not text:
                continue
            norm = normalize_text(text)
            if not norm or norm in seen:
                continue
            if exclude_norm and norm == exclude_norm:
                continue
            seen.add(norm)
            cleaned.append(text)
        return cleaned

    # --- Calibre library snapshot cache (contract v5 §6) --------------------

    def _invalidate_calibre_snapshot_cache(self) -> None:
        """Drops the cached Calibre library snapshot.

        Called at the start of ``sync_calibre`` and ``recheck_series_calibre``
        (both must read the fresh library). Re-binds never change Calibre, so
        their status/reconcile reads reuse the cache.
        """
        try:
            with self._calibre_snapshot_lock:
                self._calibre_snapshot_cache = {
                    "ts": 0.0, "series": [], "books": [],
                }
        except Exception as exc:
            logger.debug(f"Calibre snapshot invalidation failed: {exc}")

    @staticmethod
    def _snapshot_copies(values: Any) -> List[dict]:
        """Defensive copies so callers can never mutate the cached snapshot."""
        return [dict(item) for item in (values or []) if isinstance(item, dict)]

    def _cached_calibre_snapshot(self):
        """Returns fresh cached ``(series, books)`` copies, or None on a miss."""
        try:
            now = time.time()
            with self._calibre_snapshot_lock:
                cache = self._calibre_snapshot_cache
                age = now - float(cache.get("ts") or 0.0)
                series = cache.get("series") or []
                if age < CALIBRE_SNAPSHOT_TTL_SECONDS and series:
                    return (
                        self._snapshot_copies(series),
                        self._snapshot_copies(cache.get("books")),
                    )
        except Exception as exc:
            logger.debug(f"Calibre snapshot cache lookup failed: {exc}")
        return None

    def _store_calibre_snapshot(self, series: List[dict], books: List[dict]) -> None:
        """Caches a non-empty live Calibre read (never an empty 'live' result)."""
        series = self._snapshot_copies(series)
        if not series:
            return
        try:
            with self._calibre_snapshot_lock:
                self._calibre_snapshot_cache = {
                    "ts": time.time(),
                    "series": series,
                    "books": self._snapshot_copies(books),
                }
        except Exception as exc:
            logger.debug(f"Calibre snapshot cache store failed: {exc}")

    def _read_calibre_library(self):
        """One live scanner read: ``(series, books)`` or ``(None, None)``.

        The scanner I/O deliberately runs outside the cache-state lock; the
        single-flight refresh lock serialises concurrent refreshes so a batch of
        re-binds cannot stampede N full Calibre scans.
        """
        try:
            calibre_dir = self.db.get_setting("calibre_path", self.calibre_path)
            scanner = CalibreScanner(calibre_dir)
            if not scanner.is_available():
                return None, None
            live_series = list(scanner.get_all_series() or [])
            if not live_series:
                return None, None
            live_books = list(scanner.get_all_books() or [])
            return live_series, live_books
        except Exception as exc:
            logger.debug(f"Calibre library snapshot unavailable: {exc}")
            return None, None

    def _calibre_live_snapshot(self):
        """Fresh/cached ``(series, books)`` with single-flight refresh.

        Fast path: copy from the TTL cache under the short cache lock. Miss:
        take the refresh lock, double-check the cache (another thread may have
        refreshed), and only then perform the scanner read — other threads wait
        on the refresh lock instead of scanning concurrently. Returns
        ``(None, None)`` when Calibre is unavailable (callers fall back and the
        failure is never cached as live data).
        """
        cached = self._cached_calibre_snapshot()
        if cached is not None:
            return cached
        try:
            with self._calibre_snapshot_refresh_lock:
                cached = self._cached_calibre_snapshot()
                if cached is not None:
                    return cached
                live_series, live_books = self._read_calibre_library()
                if not live_series:
                    return None, None
                self._store_calibre_snapshot(live_series, live_books)
                return (
                    self._snapshot_copies(live_series),
                    self._snapshot_copies(live_books),
                )
        except Exception as exc:
            logger.debug(f"Calibre snapshot refresh failed: {exc}")
            return None, None

    def _calibre_series_fallback(self) -> List[dict]:
        """Fallback Calibre series list built from tracked Calibre-linked rows."""
        fallback: List[dict] = []
        try:
            rows = self.db.get_all_series(monitored_only=False)
        except Exception as exc:
            logger.debug(f"Calibre series fallback failed: {exc}")
            return fallback
        for row in rows or []:
            calibre_id = row.get("calibre_series_id")
            if calibre_id in (None, ""):
                continue
            name = (row.get("name") or "").strip()
            if not name:
                continue
            try:
                series_id = int(calibre_id)
            except (TypeError, ValueError):
                continue
            try:
                latest = float(row.get("latest_index") or 0.0)
            except (TypeError, ValueError):
                latest = 0.0
            try:
                book_count = int(row.get("book_count") or 0)
            except (TypeError, ValueError):
                book_count = 0
            fallback.append({
                "series_id": series_id,
                "series_name": name,
                "book_count": book_count,
                "latest_index": latest,
                "authors": row.get("authors") or "",
            })
        return fallback

    def _calibre_series_snapshot(self) -> List[dict]:
        """Live Calibre series list with a stale-safe fallback.

        Uses the lock-protected TTL snapshot cache (contract v5 §6); when the
        library is unavailable or the read is torn, falls back to the tracked
        rows that already carry a ``calibre_series_id`` so identity matching
        never sees an empty library during a transient Calibre failure.
        """
        cached = self._cached_calibre_snapshot()
        if cached is not None:
            return cached[0]
        live = self._calibre_live_snapshot()
        if live is not None and live[0]:
            return live[0]
        return self._calibre_series_fallback()

    def _fallback_calibre_books(self, series_list: List[dict]) -> List[dict]:
        """Synthesizes books for a stale series snapshot from tracked rows.

        Used only when the Calibre library read is torn; the release->volume
        checks need book rows, and owned_volumes_json is the best available
        truth without opening Calibre.
        """
        books: List[dict] = []
        try:
            rows = self.db.get_all_series(monitored_only=False)
        except Exception:
            rows = []
        by_calibre_id = {}
        for row in rows or []:
            calibre_id = row.get("calibre_series_id")
            if calibre_id not in (None, ""):
                by_calibre_id[str(calibre_id)] = row
        for ser in series_list or []:
            name = (ser.get("series_name") or ser.get("name") or "").strip()
            series_id = ser.get("series_id")
            if not name or series_id is None:
                continue
            row = by_calibre_id.get(str(series_id))
            owned: List[float] = []
            if row is not None:
                try:
                    raw = json.loads(row.get("owned_volumes_json") or "[]")
                    if isinstance(raw, list):
                        owned = [float(v) for v in raw if v is not None]
                except (TypeError, ValueError):
                    owned = []
            if not owned:
                try:
                    latest = float(ser.get("latest_index") or 0.0)
                except (TypeError, ValueError):
                    latest = 0.0
                owned = [float(v) for v in range(1, int(latest) + 1)] if latest > 0 else []
            for vol in owned:
                books.append({
                    "book_id": int(series_id) * 1000 + int(round(vol)),
                    "title": f"{name} Vol {vol:.0f}",
                    "series_id": series_id,
                    "series_name": name,
                    "series_index": float(vol),
                })
        return books

    def _calibre_library_snapshot(self):
        """Returns ``(series_list, books_list)`` for the current Calibre library.

        Fresh non-empty cached snapshots are returned as defensive copies; a miss
        performs one single-flight scanner read and caches non-empty results.
        The unavailable/torn fallback (tracked Calibre rows + synthesized books)
        keeps working and is never cached as a live empty result.
        """
        live = self._calibre_live_snapshot()
        if live is not None and live[0]:
            return live[0], live[1]
        series_list = self._calibre_series_fallback()
        return series_list, self._fallback_calibre_books(series_list)

    def series_identity_snapshot(self) -> dict:
        """Builds the per-scan series identity snapshot (contract §5.2)."""
        rows: List[dict] = []
        aliases: List[dict] = []
        try:
            rows = self.db.get_series_identity_rows()
        except Exception as exc:
            logger.debug(f"Series identity rows unavailable: {exc}")
            try:
                rows = self.db.get_all_series(monitored_only=False)
            except Exception:
                rows = []
        try:
            aliases = self.db.get_series_alias_overrides()
        except Exception as exc:
            logger.debug(f"Series alias overrides unavailable: {exc}")
            aliases = []
        calibre_series = self._calibre_series_snapshot()
        alias_map: Dict[str, dict] = {}
        calibre_index = None
        try:
            from app.series_identity import CalibreSeriesIndex, build_alias_lookup
            alias_map = build_alias_lookup(rows, aliases)
            calibre_index = CalibreSeriesIndex(calibre_series)
        except Exception as exc:
            logger.debug(f"Series identity index unavailable: {exc}")
        return {
            "rows": rows,
            "aliases": aliases,
            "alias_map": alias_map,
            "calibre_series": calibre_series,
            "calibre_index": calibre_index,
        }

    def _series_alias_map_for_matcher(self, snapshot: Optional[dict] = None) -> Dict[str, str]:
        """Builds ``{normalize_text(spelling): canonical tracked name}`` for the matcher."""
        from app.parser import normalize_text
        if snapshot is None:
            snapshot = {"rows": [], "aliases": []}
            try:
                snapshot["rows"] = self.db.get_series_identity_rows()
            except Exception:
                try:
                    snapshot["rows"] = self.db.get_all_series(monitored_only=False)
                except Exception:
                    snapshot["rows"] = []
            try:
                snapshot["aliases"] = self.db.get_series_alias_overrides()
            except Exception:
                snapshot["aliases"] = []
        mapping: Dict[str, str] = {}
        for override in snapshot.get("aliases") or []:
            source = (override.get("source_name") or "").strip()
            target = (override.get("target_name") or "").strip()
            if source and target:
                norm_source = normalize_text(source)
                if norm_source:
                    mapping[norm_source] = target
        for row in snapshot.get("rows") or []:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            norm_name = normalize_text(name)
            if not norm_name:
                continue
            display_title = (row.get("display_title") or "").strip()
            if display_title:
                norm_display = normalize_text(display_title)
                if norm_display and norm_display != norm_name:
                    mapping.setdefault(norm_display, name)
            for title in self._anilist_titles(self._parse_anilist_meta(row)):
                norm_title = normalize_text(title)
                if norm_title and norm_title != norm_name:
                    mapping.setdefault(norm_title, name)
        return mapping

    def _series_titles_from_metadata_cache(self, series_name: str) -> List[str]:
        """Reads cached AniList titles for a series from ``novel_metadata_cache``.

        Keys are normalised with ``parser.normalize_text`` (never plain lower) so
        punctuation-heavy titles such as "Trapped in a Dating Sim: ..." hit the
        same cache bucket as the matcher's candidate keys.
        """
        from app.parser import normalize_text
        titles: List[str] = []
        keys: List[str] = []
        norm_key = normalize_text(series_name)
        if norm_key:
            keys.append(norm_key)
        try:
            from app.providers.metadata import NovelMetadataProvider
            for candidate in (NovelMetadataProvider.clean_search_title(series_name), series_name):
                if candidate and candidate not in keys:
                    keys.append(candidate)
        except Exception:
            if series_name not in keys:
                keys.append(series_name)
        for key in keys:
            try:
                cached = self.db.get_cached_novel_metadata(key)
            except Exception:
                continue
            if cached:
                titles.extend(self._anilist_titles(cached))
        return self._dedupe_titles(titles, exclude_norm=norm_key)

    def _cached_series_alternate_titles(self, series_name: str,
                                        rows: Optional[List[dict]] = None) -> List[str]:
        """Cached-only title lookup (no network) used during a feed scan/link."""
        from app.parser import normalize_text
        norm_key = normalize_text(series_name or "")
        if not norm_key:
            return []
        titles: List[str] = []
        cache = getattr(self, "_series_alt_cache", None) or {}
        cached = cache.get(norm_key)
        if cached:
            titles.extend(cached.get("titles") or [])
        if rows is None:
            try:
                rows = self.db.get_series_identity_rows()
            except Exception:
                rows = []
        for row in rows or []:
            name = (row.get("name") or "").strip()
            display = (row.get("display_title") or "").strip()
            if normalize_text(name) != norm_key and (
                not display or normalize_text(display) != norm_key
            ):
                continue
            titles.extend(self._anilist_titles(self._parse_anilist_meta(row)))
            if display:
                titles.append(display)
        titles.extend(self._series_titles_from_metadata_cache(series_name))
        return self._dedupe_titles(titles, exclude_norm=norm_key)

    def lookup_series_alternate_titles(self, series_name: str, force: bool = False) -> List[str]:
        """Bounded AniList title lookup for a series (contract §5.2).

        Consults, in order: the per-process TTL cache, the tracked rows' stored
        AniList metadata, ``novel_metadata_cache`` (normalized keys), and finally
        one ``NovelMetadataProvider.fetch_series_metadata`` call per series per
        TTL. Never raises and never performs a network lookup for an empty name.
        """
        try:
            from app.parser import normalize_text
            name = (series_name or "").strip()
            key = normalize_text(name)
            if not key:
                return []
            cache = getattr(self, "_series_alt_cache", None)
            if cache is None:
                cache = self._series_alt_cache = {}
            now = time.time()
            cached = cache.get(key)
            if cached and not force and (now - float(cached.get("ts") or 0.0)) < SERIES_ALT_TITLE_TTL_SECONDS:
                return list(cached.get("titles") or [])

            titles: List[str] = []
            try:
                rows = self.db.get_series_identity_rows()
            except Exception:
                rows = []
            for row in rows or []:
                row_name = (row.get("name") or "").strip()
                display = (row.get("display_title") or "").strip()
                if normalize_text(row_name) != key and (
                    not display or normalize_text(display) != key
                ):
                    continue
                titles.extend(self._anilist_titles(self._parse_anilist_meta(row)))
                if display:
                    titles.append(display)
            # A forced lookup deliberately bypasses the SQLite metadata cache so
            # the provider is asked again (it may still answer from its own cache).
            if not force:
                titles.extend(self._series_titles_from_metadata_cache(name))

            titles = self._dedupe_titles(titles, exclude_norm=key)
            if not titles:
                meta = None
                try:
                    from app.providers.metadata import NovelMetadataProvider
                    meta = NovelMetadataProvider.fetch_series_metadata(name)
                except Exception as exc:
                    logger.debug(f"AniList series title lookup failed for '{name}': {exc}")
                    meta = None
                if meta:
                    titles = self._dedupe_titles(
                        self._anilist_titles(meta), exclude_norm=key
                    )
                    try:
                        self.db.cache_novel_metadata(key, dict(meta))
                    except Exception as exc:
                        logger.debug(f"Could not cache AniList metadata for '{name}': {exc}")

            cache[key] = {"titles": list(titles), "ts": now}
            return list(titles)
        except Exception as exc:
            logger.debug(f"Alternate-title lookup skipped for {series_name!r}: {exc}")
            return []

    def resolve_release_series_identity(self, release_dict: dict, snapshot: Optional[dict] = None) -> dict:
        """Canonicalises a release's series name through the identity snapshot.

        Order (contract §5.2): alias overrides / tracked display+AniList titles /
        exact series title first (``resolve_series_title``), then a Calibre fuzzy
        match via the snapshot's ``CalibreSeriesIndex``. Returns the canonical
        stable name, its display title, the Calibre series id, and the alternate
        names to hand to the release matcher.
        """
        from app.parser import normalize_text
        result = {
            "series_name": "",
            "display_title": "",
            "calibre_series_id": None,
            "matched": False,
            "source": "none",
            "alt_names": [],
        }
        if isinstance(release_dict, str):
            release_dict = {"series_name": release_dict}
        if not isinstance(release_dict, dict):
            return result
        title = (release_dict.get("series_name") or "").strip()
        if not title:
            return result
        norm_title = normalize_text(title)
        result["series_name"] = title
        result["display_title"] = title

        try:
            if snapshot is None:
                snapshot = self.series_identity_snapshot()
        except Exception as exc:
            logger.debug(f"Series identity snapshot failed: {exc}")
            return result

        rows = snapshot.get("rows") or []
        aliases = snapshot.get("aliases") or []
        alt_names: List[str] = list(self._cached_series_alternate_titles(title, rows=rows))

        canonical = title
        display_title = title
        series_id: Optional[int] = None
        calibre_series_id: Optional[int] = None
        matched = False
        source = "none"
        matched_row: Optional[dict] = None

        resolution = None
        try:
            from app.series_identity import resolve_series_title
            resolution = resolve_series_title(title, rows, aliases)
        except Exception as exc:
            logger.debug(f"resolve_series_title failed for '{title}': {exc}")

        if resolution is not None:
            resolved_canonical = str(
                self._resolution_value(resolution, "canonical_name", "") or ""
            ).strip()
            if resolved_canonical:
                canonical = resolved_canonical
            resolved_display = str(
                self._resolution_value(resolution, "display_title", "") or ""
            ).strip()
            if resolved_display:
                display_title = resolved_display
            series_id = self._resolution_value(resolution, "series_id")
            calibre_series_id = self._resolution_value(resolution, "calibre_series_id")
            source = str(self._resolution_value(resolution, "source", "none") or "none")
            matched = bool(self._resolution_value(resolution, "matched", False))
            if matched and normalize_text(canonical) != norm_title:
                source = source if source not in ("", "none") else "alias"
            if (
                matched
                and series_id is None
                and calibre_series_id in (None, "")
                and normalize_text(canonical) != norm_title
                and not any(
                    normalize_text(row.get("name") or "") == normalize_text(canonical)
                    for row in rows
                )
            ):
                # A stale alias override can point at a merged/deleted tracked
                # row; never let that dead target shadow the real series or the
                # Calibre fuzzy fallback below.
                matched = False
                source = "none"
                canonical = title
                display_title = title
                series_id = None
                calibre_series_id = None

        # Find the tracked row backing the resolution (for display/AniList titles).
        for row in rows:
            row_calibre = row.get("calibre_series_id")
            row_id = row.get("id")
            if series_id is not None and row_id == series_id:
                matched_row = row
                break
            if calibre_series_id is not None and row_calibre == calibre_series_id:
                matched_row = row
                break
            if normalize_text(row.get("name") or "") == normalize_text(canonical):
                matched_row = row
                break
        if matched_row is not None:
            if not (display_title or "").strip() or normalize_text(display_title) == normalize_text(canonical):
                row_display = (matched_row.get("display_title") or "").strip()
                if row_display:
                    display_title = row_display
            if calibre_series_id is None and matched_row.get("calibre_series_id") is not None:
                calibre_series_id = matched_row.get("calibre_series_id")
            alt_names.extend(self._anilist_titles(self._parse_anilist_meta(matched_row)))

        if not matched:
            # Calibre fuzzy fallback: a discovered/provider title that no tracked
            # row/alias knows may still be a near-match for a Calibre series.
            calibre_index = snapshot.get("calibre_index")
            calibre_series_list = snapshot.get("calibre_series") or []
            if calibre_index is None and calibre_series_list:
                try:
                    from app.series_identity import CalibreSeriesIndex
                    calibre_index = CalibreSeriesIndex(calibre_series_list)
                except Exception as exc:
                    logger.debug(f"CalibreSeriesIndex unavailable: {exc}")
                    calibre_index = None
            if calibre_index is not None and calibre_series_list:
                try:
                    matched_calibre = calibre_index.match(
                        title, alt_names=alt_names or None
                    )
                except Exception as exc:
                    logger.debug(f"Calibre fuzzy resolve failed for '{title}': {exc}")
                    matched_calibre = None
                if matched_calibre:
                    matched_canonical = self._canonical_calibre_name(matched_calibre)
                    if matched_canonical:
                        canonical = matched_canonical
                        display_title = matched_canonical
                        calibre_series_id = matched_calibre.get("series_id") or matched_calibre.get("id")
                        matched = True
                        source = "fuzzy"
                        # Pick up the tracked row's display titles for the match.
                        for row in rows:
                            if normalize_text(row.get("name") or "") == normalize_text(canonical):
                                alt_names.extend(self._anilist_titles(self._parse_anilist_meta(row)))
                                row_display = (row.get("display_title") or "").strip()
                                if row_display:
                                    alt_names.append(row_display)
                                break

        result["series_name"] = (canonical or title).strip()
        result["display_title"] = (display_title or result["series_name"]).strip()
        result["calibre_series_id"] = calibre_series_id
        if series_id is not None:
            result["series_id"] = series_id
        result["matched"] = bool(matched)
        result["source"] = source or ("none" if not matched else "fuzzy")
        result["alt_names"] = self._dedupe_titles(alt_names, exclude_norm=normalize_text(result["series_name"]))
        return result

    @staticmethod
    def _canonical_calibre_name(series: dict) -> str:
        if not isinstance(series, dict):
            return ""
        return str(series.get("series_name") or series.get("name") or "").strip()

    def _rekey_release_series_name(self, release_id: Optional[int], canonical_name: str) -> bool:
        """Re-keys one release to a canonical tracked/Calibre name.

        ``db.update_release_details`` deliberately protects the stored stable
        key from *metadata* rewrites; reconcile/identity adoption is the opposite
        direction (a detached spelling -> the linked tracked name), so it updates
        the column directly. Returns True when a row was re-keyed.
        """
        from app.parser import normalize_text
        canonical = (canonical_name or "").strip()
        if release_id is None or not canonical:
            return False
        try:
            with self.db.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT series_name, series_pinned FROM releases WHERE id = ?;", (release_id,))
                row = cur.fetchone()
                if row is None:
                    return False
                if self._release_identity_pinned(dict(row)):
                    return False
                stored = (row["series_name"] or "").strip()
                if normalize_text(stored) == normalize_text(canonical):
                    return False
                cur.execute(
                    "UPDATE releases SET series_name = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?;",
                    (canonical, release_id),
                )
                conn.commit()
                return True
        except Exception as exc:
            logger.debug(f"Stable-key re-key failed for release #{release_id}: {exc}")
            return False

    def _link_targets(self, links: List[dict]) -> List[str]:
        """Extracts unique canonical target names from *successful* link summaries.

        The DB layer returns ``{"status": "skipped", "reason": "identity_locked"}``
        (or ``"error"``) summaries for rows it deliberately did not link; those
        must never be counted as a successful auto-link or reconciled.
        """
        from app.parser import normalize_text
        targets: List[str] = []
        seen = set()
        for link in links or []:
            if not isinstance(link, dict):
                continue
            status = str(link.get("status") or "success").strip().lower()
            if status != "success":
                continue
            target = str(link.get("target") or link.get("source") or "").strip()
            norm = normalize_text(target)
            if norm and norm not in seen:
                seen.add(norm)
                targets.append(target)
        return targets

    @staticmethod
    def _webhook_setting_enabled(db, key: str) -> bool:
        """Reads a webhook event toggle (default True; false/0/no/off disable)."""
        try:
            raw = db.get_setting(key, "true")
        except Exception:
            return True
        return str(raw or "").strip().lower() not in _DISABLED_SETTING_VALUES

    @staticmethod
    def _release_identity_pinned(release: Any) -> bool:
        """True when a release row carries the manual re-bind pin (``series_pinned``).

        Pinned releases keep their ``series_name`` verbatim through feed
        re-ingestion/canonicalisation/reconciliation until explicitly reset
        (contract v2 §2.1/§3). Handles rows read before the v2 migration.
        """
        if not isinstance(release, dict):
            return False
        value = release.get("series_pinned")
        if value is None:
            return False
        try:
            return int(value) == 1
        except (TypeError, ValueError):
            return bool(value)

    @staticmethod
    def _series_identity_locked(row: Any) -> bool:
        """True when a tracked series is identity-locked (manual new title)."""
        if not isinstance(row, dict):
            return False
        value = row.get("identity_locked")
        if value is None:
            return False
        try:
            return int(value) == 1
        except (TypeError, ValueError):
            return bool(value)

    @staticmethod
    def _series_anilist_locked(row: Any) -> bool:
        """True when automatic AniList enrichment is disabled for a series."""
        if not isinstance(row, dict):
            return False
        value = row.get("anilist_locked")
        if value is None:
            return False
        try:
            return int(value) == 1
        except (TypeError, ValueError):
            return bool(value)

    def _release_row_by_guid(self, guid: str) -> Optional[dict]:
        """Reads the stored identity/pin state for a feed GUID (never raises).

        Used by ``check_feeds`` to honour a per-release pin *before* the
        re-ingestion upsert and identity canonicalisation run.
        """
        if not guid:
            return None
        try:
            with self.db.get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, series_name, series_pinned, status, volume "
                    "FROM releases WHERE guid = ?;",
                    (str(guid),),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        except Exception as exc:
            logger.debug(f"Pinned-release lookup failed for guid {guid!r}: {exc}")
            return None

    def _calibre_release_status(self, release: dict, canonical_name: str,
                                calibre_series_id: Optional[int]):
        """Recomputes ``(status, matched_book_id)`` for a release bound to Calibre.

        Returns ``(None, None)`` when the Calibre snapshot is unavailable so the
        caller can fall back to the volume-based NEW/REQUESTED rule.
        """
        series_list, books = self._calibre_library_snapshot()
        if not series_list:
            return None, None
        try:
            matcher = CalibreMatcher(
                series_list,
                books,
                series_aliases=self._series_alias_map_for_matcher(),
            )
        except Exception:
            matcher = CalibreMatcher(series_list, books)
        alt_names: List[str] = []
        try:
            target_row = (
                self.db.get_series_by_calibre_id(calibre_series_id)
                if calibre_series_id is not None else None
            )
        except Exception:
            target_row = None
        if target_row is not None:
            alt_names.extend(self._anilist_titles(self._parse_anilist_meta(target_row)))
            display = (target_row.get("display_title") or "").strip()
            if display:
                alt_names.append(display)
        payload = dict(release)
        payload["series_name"] = canonical_name
        try:
            result = matcher.check_release(
                payload,
                auto_request_monitored=True,
                alt_series_names=self._dedupe_titles(alt_names) or None,
            )
        except Exception as exc:
            logger.debug(f"Re-bind status recompute failed: {exc}")
            return None, None
        status = (result.get("status") or "").strip().upper()
        if status not in ("IN_CALIBRE", "REQUESTED", "NEW"):
            return None, None
        return status, result.get("matched_book_id")

    # --- AniList publication metadata / Nyaa unmarked-bundle inference -------
    #
    # Contract: .agents/volume_inference/CONTRACT.md §4. The provider normalizes
    # the AniList publication fields (format/status/volumes); this scheduler glue
    # looks them up through a per-process TTL cache, tracks a bounded fetch
    # budget, and turns an unmarked Nyaa batch release into the complete series
    # when AniList says Light Novel + Finished + N volumes.

    @staticmethod
    def _publication_from_meta(meta: Any) -> Dict[str, Any]:
        """Normalizes a metadata dict/JSON string via the provider helper.

        Returns ``{}`` when nothing is known or the provider/helper is missing
        (a partially deployed provider must never break a feed scan).
        """
        if not meta:
            return {}
        try:
            from app.providers.metadata import NovelMetadataProvider
            helper = getattr(NovelMetadataProvider, "publication_from_meta", None)
            if helper is None:
                return {}
            publication = helper(meta)
            return dict(publication) if isinstance(publication, dict) else {}
        except Exception as exc:
            logger.debug(f"Publication metadata normalization failed: {exc}")
            return {}

    @staticmethod
    def _publication_usable(publication: Any) -> bool:
        """True when a publication dict carries a status or a positive volume count."""
        if not isinstance(publication, dict):
            return False
        status = str(publication.get("status") or "").strip()
        raw_volumes = publication.get("volumes")
        if raw_volumes is None:
            raw_volumes = publication.get("total_volumes")
        try:
            volumes = int(raw_volumes)
        except (TypeError, ValueError):
            volumes = 0
        return bool(status) or volumes >= 1

    def publication_metadata_for(self, series_name: str,
                                 budget: Optional[Dict[str, int]] = None) -> Optional[Dict[str, Any]]:
        """Looks up normalized AniList publication metadata for a series.

        Order (contract v3 §4): TTL cache → tracked row's stored AniList meta →
        one bounded ``NovelMetadataProvider.fetch_series_metadata`` call (the
        budget is decremented; a one-off budget of 3 applies when none is given),
        persisting the result to the tracker when a row exists. Never raises.
        """
        try:
            from app.parser import normalize_text
            name = (series_name or "").strip()
            key = normalize_text(name)
            if not key:
                return None
            cache = getattr(self, "_publication_cache", None)
            if cache is None:
                cache = self._publication_cache = {}
            now = time.time()
            cached = cache.get(key)
            if cached and (now - float(cached.get("ts") or 0.0)) < PUBLICATION_CACHE_TTL_SECONDS:
                data = cached.get("data")
                return dict(data) if isinstance(data, dict) else None

            row: Optional[dict] = None
            try:
                row = self.db.get_series_by_name(name)
            except Exception as exc:
                logger.debug(f"Publication tracker lookup failed for '{name}': {exc}")
                row = None
            if row is None:
                try:
                    for candidate in self.db.get_all_series(monitored_only=False) or []:
                        if normalize_text(candidate.get("name") or "") == key:
                            row = candidate
                            break
                except Exception as exc:
                    logger.debug(f"Publication normalized tracker lookup failed: {exc}")
                    row = None
            if row is not None:
                raw_meta = row.get("anilist_meta")
                if raw_meta is None:
                    raw_meta = row.get("anilist_meta_json")
                stored = self._publication_from_meta(raw_meta)
                if self._publication_usable(stored):
                    cache[key] = {"data": stored, "ts": now}
                    return dict(stored)

            # A manually pinned/cleared series must never fall through to a
            # fuzzy title search: an exact stored AniList id is the only allowed
            # remote lookup, and a cleared row returns no publication metadata.
            anilist_locked = self._series_anilist_locked(row) if row is not None else False
            if anilist_locked:
                stored_id = row.get("anilist_id") if row is not None else None
                try:
                    stored_id = int(stored_id) if stored_id else None
                except (TypeError, ValueError):
                    stored_id = None
                if stored_id is None:
                    return None
                effective_budget = budget
                if effective_budget is None:
                    effective_budget = {"remaining": PUBLICATION_ONE_OFF_BUDGET}
                try:
                    remaining = int(effective_budget.get("remaining", 0) or 0)
                except (AttributeError, TypeError, ValueError):
                    remaining = 0
                if remaining <= 0:
                    return None
                try:
                    effective_budget["remaining"] = remaining - 1
                except Exception:
                    pass
                try:
                    from app.providers.metadata import NovelMetadataProvider
                    meta = NovelMetadataProvider.query_anilist_by_id(stored_id)
                except Exception as exc:
                    logger.debug(
                        "Pinned publication metadata lookup failed for '%s': %s",
                        name, exc,
                    )
                    meta = None
                publication = self._publication_from_meta(meta)
                if not self._publication_usable(publication):
                    return None
                cache[key] = {"data": publication, "ts": now}
                return dict(publication)

            # Bounded provider lookup (one call per series per TTL at most).
            effective_budget = budget
            if effective_budget is None:
                effective_budget = {"remaining": PUBLICATION_ONE_OFF_BUDGET}
            try:
                remaining = int(effective_budget.get("remaining", 0) or 0)
            except (AttributeError, TypeError, ValueError):
                remaining = 0
            if remaining <= 0:
                return None
            try:
                effective_budget["remaining"] = remaining - 1
            except Exception:
                pass

            meta = None
            try:
                from app.providers.metadata import NovelMetadataProvider
                meta = NovelMetadataProvider.fetch_series_metadata(name)
            except Exception as exc:
                logger.debug(f"Publication metadata lookup failed for '{name}': {exc}")
                meta = None
            if not meta:
                return None
            publication = self._publication_from_meta(meta)
            if not self._publication_usable(publication):
                return None
            if row is not None and row.get("id") is not None:
                try:
                    self.db.store_series_publication_metadata(int(row["id"]), meta)
                except Exception as exc:
                    logger.debug(
                        f"Publication metadata persist failed for '{name}': {exc}"
                    )
            cache[key] = {"data": publication, "ts": now}
            return dict(publication)
        except Exception as exc:
            logger.debug(f"Publication metadata lookup skipped for {series_name!r}: {exc}")
            return None

    def infer_nyaa_bundle_volumes(self, release: Dict[str, Any],
                                  canonical_series_name: str,
                                  is_nyaa_feed: bool,
                                  budget: Optional[Dict[str, int]] = None) -> bool:
        """Infers the complete-series volume set for an unmarked Nyaa bundle.

        Returns True (and mutates ``release`` in place) only when the setting is
        enabled, the feed is Nyaa, the release carries no volume information at
        all, is not manga/audiobook, and AniList reports a FINISHED light novel
        with a known volume count. Contract v3 §4.
        """
        try:
            if not isinstance(release, dict):
                return False
            try:
                inference_enabled = self._setting_flag(
                    self.db.get_setting(NYAA_BUNDLE_VOLUME_INFERENCE_KEY, "true"),
                    default=True,
                )
            except Exception:
                inference_enabled = True
            if not inference_enabled:
                return False
            if not is_nyaa_feed:
                return False
            if release.get("volume") is not None:
                return False
            if release.get("volumes"):
                return False
            if release.get("is_chapter"):
                return False
            media_type = str(release.get("media_type") or "").strip().lower()
            if media_type in ("manga", "audiobook"):
                return False
            canonical = (canonical_series_name or "").strip()
            if not canonical:
                canonical = str(release.get("series_name") or "").strip()
            publication = self.publication_metadata_for(canonical, budget)
            if not isinstance(publication, dict):
                return False
            if not bool(publication.get("is_light_novel")):
                return False
            status = str(publication.get("status") or "").strip().upper()
            if status != "FINISHED":
                return False
            raw_volumes = publication.get("volumes")
            if raw_volumes is None:
                raw_volumes = publication.get("total_volumes")
            try:
                total = int(raw_volumes)
            except (TypeError, ValueError):
                return False
            if total < 1:
                return False

            release["volumes"] = [float(v) for v in range(1, total + 1)]
            release["volume"] = 1.0
            release["volume_start"] = 1.0
            release["volume_end"] = float(total)
            release["is_bundle"] = total > 1
            release["is_complete"] = True
            release["volume_inferred"] = True
            release["volume_inference_source"] = "anilist_finished"
            logger.info(
                "Inferred complete-series bundle for '%s': %s volume(s) "
                "(AniList %s/%s).",
                release.get("title") or release.get("series_name"),
                total,
                publication.get("status") or "?",
                publication.get("format") or "?",
            )
            return True
        except Exception as exc:
            logger.debug(f"Bundle volume inference skipped: {exc}")
            return False

    def invalidate_series_identity_cache(self, *names: Any) -> None:
        """Drops in-process AniList/publication cache entries for these names."""
        from app.parser import normalize_text
        for name in names:
            key = normalize_text(str(name or "").strip())
            if not key:
                continue
            try:
                self._series_alt_cache.pop(key, None)
            except Exception:
                pass
            try:
                self._publication_cache.pop(key, None)
            except Exception:
                pass

    def enrich_tracked_series_anilist(self, limit: int = SERIES_ANILIST_SYNC_LIMIT_DEFAULT) -> dict:
        """Fetches AniList metadata for a bounded batch of un-enriched tracked rows.

        Then attempts the Calibre auto-link for the enriched rows (contract
        §5.2/§5.3). Never raises: a metadata outage leaves rows untouched.
        Returns ``{"synced", "linked", "failed"}`` plus ``linked_names``.
        """
        result = {"synced": 0, "linked": 0, "failed": 0, "linked_names": [], "links": []}
        try:
            batch_limit = int(limit)
        except (TypeError, ValueError):
            batch_limit = SERIES_ANILIST_SYNC_LIMIT_DEFAULT
        if batch_limit <= 0:
            return result
        try:
            rows = self.db.get_series_identity_rows()
        except Exception:
            try:
                rows = self.db.get_all_series(monitored_only=False)
            except Exception:
                return result

        pending = [
            row for row in rows or []
            if not (row.get("anilist_meta_json") or "").strip()
            and (row.get("name") or "").strip()
            and row.get("id") is not None
            # A manually locked/new-title tracker must never be enriched: its
            # identity is deliberately detached from the automatic graph.
            and not self._series_identity_locked(row)
            # A manual AniList map or an explicit AniList clear must also stick.
            and not self._series_anilist_locked(row)
        ][:batch_limit]
        if not pending:
            return result

        synced_names: List[str] = []
        for row in pending:
            name = (row.get("name") or "").strip()
            series_id = row.get("id")
            meta = None
            try:
                from app.providers.metadata import NovelMetadataProvider
                meta = NovelMetadataProvider.fetch_series_metadata(name)
            except Exception as exc:
                logger.debug(f"AniList enrichment failed for '{name}': {exc}")
                meta = None
            if not meta:
                result["failed"] += 1
                continue
            try:
                self.db.store_series_anilist_metadata(int(series_id), meta)
            except Exception as exc:
                logger.debug(f"AniList metadata persist failed for '{name}': {exc}")
                result["failed"] += 1
                continue
            result["synced"] += 1
            synced_names.append(name)

        if not synced_names:
            return result

        # Auto-link the enriched rows that are still unlinked (bounded: the DB
        # method only looks at tracked rows without a Calibre id).
        try:
            calibre_series_list = self._calibre_series_snapshot()
            links = self.db.auto_link_series_to_calibre(calibre_series_list, actor="scheduler") or []
        except Exception as exc:
            logger.debug(f"Auto-link after AniList enrichment failed: {exc}")
            links = []
        if links:
            result["links"] = list(links)
            from app.parser import normalize_text
            synced_norms = {normalize_text(name) for name in synced_names}
            for link in links:
                if str(link.get("status") or "success").strip().lower() != "success":
                    continue
                source = str(link.get("source") or "").strip()
                if normalize_text(source) in synced_norms:
                    result["linked"] += 1
                    target = str(link.get("target") or source).strip()
                    if target:
                        result["linked_names"].append(target)
        return result

    def reconcile_series_releases(self, series_name: str) -> dict:
        """Re-runs Calibre matching for a linked series' releases (contract §5.2).

        Releases stored under an alias/display variant of the series are
        re-keyed to the canonical tracked name first, then their statuses are
        recomputed with ``enforce_precedence=True`` so IGNORED / BLACKLISTED /
        IN_CALIBRE are never downgraded. Returns
        ``{"checked", "in_calibre", "requested", "new"}`` (+ ``rekeyed``).
        """
        from app.parser import normalize_text
        counts = {"checked": 0, "in_calibre": 0, "requested": 0, "new": 0, "rekeyed": 0}
        target = (series_name or "").strip()
        if not target:
            return counts
        try:
            snapshot = self.series_identity_snapshot()
        except Exception as exc:
            logger.debug(f"Reconcile snapshot failed for '{target}': {exc}")
            return counts
        rows = snapshot.get("rows") or []
        aliases = snapshot.get("aliases") or []

        canonical = target
        calibre_series_id: Optional[int] = None
        target_row: Optional[dict] = None
        # Prefer a direct stable-name lookup: callers pass the canonical tracked
        # name, so a stale alias override must never redirect reconciliation.
        target_norm = normalize_text(target)
        for row in rows:
            if normalize_text(row.get("name") or "") != target_norm:
                continue
            target_row = row
            if row.get("calibre_series_id") is not None:
                calibre_series_id = row.get("calibre_series_id")
            break
        if target_row is None:
            try:
                from app.series_identity import resolve_series_title
                resolution = resolve_series_title(target, rows, aliases)
            except Exception as exc:
                logger.debug(f"Reconcile resolution failed for '{target}': {exc}")
                resolution = None
            if resolution is not None:
                resolved_canonical = str(
                    self._resolution_value(resolution, "canonical_name", "") or ""
                ).strip()
                resolved_calibre = self._resolution_value(resolution, "calibre_series_id")
                # Ignore a dead resolution (e.g. a stale alias override pointing
                # at a merged/deleted row) instead of reconciling the wrong series.
                if resolved_canonical and (
                    resolved_calibre is not None
                    or any(normalize_text(row.get("name") or "") == normalize_text(resolved_canonical)
                           for row in rows)
                ):
                    canonical = resolved_canonical
                    calibre_series_id = resolved_calibre
                    target_norm = normalize_text(canonical)
                    for row in rows:
                        if normalize_text(row.get("name") or "") == target_norm:
                            target_row = row
                            if calibre_series_id is None and row.get("calibre_series_id") is not None:
                                calibre_series_id = row.get("calibre_series_id")
                            break
        if calibre_series_id in (None, ""):
            if target_row is not None:
                calibre_series_id = target_row.get("calibre_series_id")
        if calibre_series_id in (None, ""):
            # Without a Calibre series id there is nothing to reconcile against.
            return counts

        series_list, books = self._calibre_library_snapshot()
        if not series_list:
            return counts
        try:
            matcher = CalibreMatcher(series_list, books)
        except Exception as exc:
            logger.debug(f"Reconcile matcher unavailable: {exc}")
            return counts
        alt_names: List[str] = []
        if target_row is not None:
            alt_names.extend(self._anilist_titles(self._parse_anilist_meta(target_row)))
            display = (target_row.get("display_title") or "").strip()
            if display:
                alt_names.append(display)
        alt_names = self._dedupe_titles(alt_names, exclude_norm=target_norm) or None

        # Candidate spellings: canonical + every alias-override source that
        # targets it. Any other stored variant is resolved through the snapshot.
        variant_norms = {target_norm}
        for override in aliases:
            if normalize_text(override.get("target_name") or "") == target_norm:
                source_norm = normalize_text(override.get("source_name") or "")
                if source_norm:
                    variant_norms.add(source_norm)

        try:
            releases = self.db.get_releases(limit=50000, include_ignored=True)
        except Exception as exc:
            logger.debug(f"Reconcile release read failed: {exc}")
            releases = []

        for release in releases or []:
            # Manually pinned releases are explicitly excluded from automatic
            # reconciliation until the user resets/unpins them.
            if self._release_identity_pinned(release):
                continue
            stored = (release.get("series_name") or "").strip()
            if not stored:
                continue
            stored_norm = normalize_text(stored)
            is_variant = stored_norm in variant_norms
            if not is_variant:
                try:
                    variant_resolution = resolve_series_title(stored, rows, aliases)
                except Exception:
                    variant_resolution = None
                if variant_resolution is not None:
                    resolved = str(
                        self._resolution_value(variant_resolution, "canonical_name", "") or ""
                    ).strip()
                    is_variant = bool(
                        self._resolution_value(variant_resolution, "matched", False)
                    ) and normalize_text(resolved) == target_norm
            if not is_variant:
                continue

            release_id = release.get("id")
            if release_id is None:
                continue
            counts["checked"] += 1
            stored_status = (release.get("status") or "").strip().upper()
            # User-veto tier: never touched by automation.
            if stored_status in ("IGNORED", "BLACKLISTED"):
                continue

            if stored_norm != target_norm:
                if self._rekey_release_series_name(release_id, canonical):
                    counts["rekeyed"] += 1

            payload = dict(release)
            payload["series_name"] = canonical
            try:
                match_result = matcher.check_release(
                    payload,
                    auto_request_monitored=True,
                    alt_series_names=alt_names,
                )
            except Exception as exc:
                logger.debug(f"Reconcile check_release failed for release #{release_id}: {exc}")
                continue
            incoming_status = (match_result.get("status") or "").strip().upper()
            # An "unofficial" IGNORED verdict is not a user veto; do not let it
            # overwrite any stored lifecycle status.
            if incoming_status in ("", "IGNORED", "BLACKLISTED"):
                continue
            try:
                self.db.update_release_status(
                    release_id,
                    status=incoming_status,
                    matched_book_id=match_result.get("matched_book_id"),
                    enforce_precedence=True,
                )
            except Exception as exc:
                logger.debug(f"Reconcile status update failed for release #{release_id}: {exc}")
                continue
            if incoming_status == "IN_CALIBRE":
                counts["in_calibre"] += 1
            elif incoming_status == "REQUESTED":
                counts["requested"] += 1
            elif incoming_status == "NEW":
                counts["new"] += 1

        if counts["rekeyed"]:
            try:
                self.db.log_activity(
                    "SERIES_REKEYED",
                    f"Re-keyed releases to tracked series '{canonical}'",
                    f"rekeyed={counts['rekeyed']} checked={counts['checked']}",
                )
            except Exception:
                pass
        return counts

    def refresh_series_metadata(self, series_id: int) -> dict:
        """Fetches AniList metadata for one tracked series, then link + reconcile."""
        result = {
            "status": "error",
            "message": "",
            "display_title": "",
            "anilist_id": None,
            "linked": False,
            "reconciled": {},
        }
        try:
            series_id_int = int(series_id)
        except (TypeError, ValueError):
            result["message"] = f"Invalid series id: {series_id!r}"
            return result
        row = None
        try:
            for candidate in self.db.get_series_identity_rows() or []:
                if candidate.get("id") == series_id_int:
                    row = candidate
                    break
        except Exception as exc:
            logger.debug(f"Series row lookup failed for #{series_id_int}: {exc}")
        if row is None:
            result["message"] = f"Series #{series_id_int} not found"
            return result

        name = (row.get("name") or "").strip()
        anilist_locked = self._series_anilist_locked(row)
        stored_anilist_id = row.get("anilist_id")
        try:
            stored_anilist_id = int(stored_anilist_id) if stored_anilist_id else None
        except (TypeError, ValueError):
            stored_anilist_id = None
        meta = None
        if anilist_locked and stored_anilist_id is None:
            result["message"] = (
                "AniList is manually cleared/pinned for this series; "
                "map an AniList URL or id to refresh it"
            )
        elif name or stored_anilist_id is not None:
            try:
                from app.providers.metadata import NovelMetadataProvider
                if stored_anilist_id is not None:
                    meta = NovelMetadataProvider.query_anilist_by_id(stored_anilist_id)
                else:
                    meta = NovelMetadataProvider.fetch_series_metadata(name)
            except Exception as exc:
                logger.debug(f"AniList refresh failed for '{name}': {exc}")
                meta = None
            if meta:
                try:
                    self.db.store_series_anilist_metadata(
                        series_id_int, meta, force=anilist_locked
                    )
                    if stored_anilist_id is not None:
                        preferred_display = (
                            meta.get("english_title")
                            or meta.get("title")
                            or meta.get("canonical_title")
                        )
                        if preferred_display:
                            self.db.update_series_identity(
                                series_id_int, display_title=preferred_display
                            )
                except Exception as exc:
                    logger.debug(f"AniList refresh persist failed for '{name}': {exc}")
                    meta = None

        linked = row.get("calibre_series_id") not in (None, "")
        target_name = name
        if not linked and name:
            try:
                calibre_series_list = self._calibre_series_snapshot()
                links = self.db.auto_link_series_to_calibre(calibre_series_list, actor="user") or []
            except Exception as exc:
                logger.debug(f"Auto-link during AniList refresh failed: {exc}")
                links = []
            from app.parser import normalize_text
            for link in links:
                if str(link.get("status") or "success").strip().lower() != "success":
                    continue
                if normalize_text(link.get("source") or "") == normalize_text(name):
                    target_name = str(link.get("target") or name).strip()
                    linked = True
                    break

        reconciled = {}
        if linked:
            try:
                reconciled = self.reconcile_series_releases(target_name)
            except Exception as exc:
                logger.debug(f"Reconcile after AniList refresh failed: {exc}")
                reconciled = {}
            result["linked"] = True

        display_title = (meta or {}).get("english_title") or (meta or {}).get("title") or (
            row.get("display_title") or ""
        ) or name
        anilist_id = (meta or {}).get("anilist_id") or row.get("anilist_id")
        if anilist_locked and stored_anilist_id is None and not meta:
            refresh_message = (
                "AniList is manually cleared/pinned for this series; "
                "map an AniList URL or id to refresh it"
            )
        else:
            refresh_message = (
                f"AniList metadata refreshed for '{name}'"
                if meta else f"No AniList metadata found for '{name}'"
            )
        result.update({
            "status": "success",
            "display_title": str(display_title or "").strip(),
            "anilist_id": anilist_id,
            "reconciled": reconciled,
            "message": refresh_message,
        })
        try:
            self.db.log_activity(
                "SERIES_ANILIST_REFRESHED",
                f"AniList metadata refreshed for '{name}'",
                json.dumps({
                    "series_id": series_id_int,
                    "synced": bool(meta),
                    "linked": bool(result["linked"]),
                }),
            )
        except Exception:
            pass
        return result

    def match_series_to_calibre(self, source_name: str, calibre_series: dict,
                                actor: str = "user", release_id: Optional[int] = None) -> dict:
        """Manually links a tracked/discovered series to a Calibre series.

        Wraps ``db.link_series_to_calibre`` (existing tracker) or
        ``db.match_discovered_series_to_calibre`` (discovery-only series), then
        reconciles the linked series' releases and logs ``SERIES_MANUALLY_MATCHED``.
        ``release_id`` is accepted for callers that want to act on one release;
        the merge/re-key is series-wide so it needs no extra handling.
        """
        source = (source_name or "").strip()
        if not source:
            raise ValueError("source_name must not be empty")
        target_name = self._canonical_calibre_name(calibre_series)
        if not target_name:
            raise ValueError("calibre_series must carry a series_name/name")
        calibre_series_id = calibre_series.get("series_id") or calibre_series.get("id")

        source_row = None
        try:
            source_row = self.db.get_series_by_name(source)
        except Exception as exc:
            logger.debug(f"Series lookup failed for '{source}': {exc}")

        if source_row is not None:
            summary = self.db.link_series_to_calibre(
                source, calibre_series, actor=actor, owned_volumes=None
            )
            discovered = False
        else:
            summary = self.db.match_discovered_series_to_calibre(
                source, calibre_series, actor=actor
            )
            discovered = True

        summary = dict(summary or {})
        summary.setdefault("status", "success")
        summary.setdefault("source", source)
        summary.setdefault("target", target_name)
        if summary.get("calibre_series_id") in (None, "") and calibre_series_id is not None:
            summary["calibre_series_id"] = calibre_series_id
        summary["discovered"] = discovered

        # The merged tracker's canonical name may differ in case from the
        # scanner spelling; reconcile using the stored tracked row.
        canonical_target = str(summary.get("target") or target_name)
        try:
            stored_target = self.db.get_series_by_calibre_id(
                summary.get("calibre_series_id")
            ) if summary.get("calibre_series_id") is not None else None
            if stored_target and (stored_target.get("name") or "").strip():
                canonical_target = stored_target["name"].strip()
        except Exception as exc:
            logger.debug(f"Linked series row lookup failed: {exc}")

        reconciled = {}
        try:
            reconciled = self.reconcile_series_releases(canonical_target)
        except Exception as exc:
            logger.debug(f"Reconcile after manual match failed: {exc}")
            reconciled = {}
        summary["reconciled"] = reconciled

        try:
            self.db.log_activity(
                "SERIES_MANUALLY_MATCHED",
                f"Series manually matched: '{source}' -> '{canonical_target}'",
                json.dumps({
                    "actor": actor,
                    "source": source,
                    "target": canonical_target,
                    "calibre_series_id": summary.get("calibre_series_id"),
                    "release_id": release_id,
                    "merged": bool(summary.get("merged")),
                    "rekeyed_releases": summary.get("rekeyed_releases", 0),
                }),
            )
        except Exception:
            pass
        return summary

    def rebind_release(self, release_id: int, mode: str,
                       target_name: Optional[str] = None,
                       calibre_series_id: Optional[int] = None,
                       remember: bool = False, actor: str = "user") -> dict:
        """Explicit per-release re-bind / new-title / detach (contract v2 §3).

        Delegates the pinned write to ``db.rebind_release_series`` and then
        recomputes the release's ownership status: a Calibre-bound target goes
        through ``CalibreMatcher.check_release``; a brand-new/detached title gets
        NEW for volume None/1.0 and REQUESTED otherwise. This is an explicit user
        action (``enforce_precedence=False``) but an existing IGNORED/BLACKLISTED
        veto is never downgraded. Returns the DB summary plus
        ``release_status``/``message`` for the toast.
        """
        try:
            release_id_int = int(release_id)
        except (TypeError, ValueError):
            raise ValueError(f"invalid release id: {release_id!r}")
        mode_clean = str(mode or "").strip().lower()
        if not mode_clean:
            raise ValueError("mode is required")

        summary = dict(self.db.rebind_release_series(
            release_id_int,
            mode_clean,
            target_name=target_name,
            calibre_series_id=calibre_series_id,
            remember=bool(remember),
            actor=actor,
        ) or {})
        release = self.db.get_release_by_id(release_id_int)
        if not release:
            raise LookupError(f"release #{release_id_int} not found")

        canonical_name = str(
            summary.get("series_name") or release.get("series_name") or ""
        ).strip()
        previous_series = str(summary.get("previous_series") or "").strip()
        target_calibre_id = summary.get("calibre_series_id")
        if target_calibre_id in (None, "") and canonical_name:
            target_row = None
            try:
                target_row = self.db.get_series_by_name(canonical_name)
            except Exception as exc:
                logger.debug(f"Re-bind target lookup failed for '{canonical_name}': {exc}")
            if target_row is not None:
                target_calibre_id = target_row.get("calibre_series_id")

        stored_status = (release.get("status") or "").strip().upper()
        new_status: Optional[str] = None
        matched_book_id: Optional[int] = None
        if target_calibre_id not in (None, "") and canonical_name:
            try:
                new_status, matched_book_id = self._calibre_release_status(
                    release, canonical_name, int(target_calibre_id)
                )
            except (TypeError, ValueError):
                new_status, matched_book_id = None, None
        if new_status is None:
            volume = release.get("volume")
            try:
                volume_value = float(volume) if volume is not None else None
            except (TypeError, ValueError):
                volume_value = None
            bundle_volumes = self._volume_list(release.get("volumes_json"))
            if bundle_volumes:
                # A bundle has no single volume column; keep it wanted when any
                # contained volume is beyond the first.
                new_status = "REQUESTED" if any(v > 1.0 for v in bundle_volumes) else "NEW"
            else:
                new_status = "REQUESTED" if (volume_value is not None and volume_value > 1.0) else "NEW"

        if stored_status in ("IGNORED", "BLACKLISTED"):
            # Explicit user veto data: a re-bind must not lift it.
            release_status = stored_status
            matched_book_id = None
        else:
            release_status = new_status or "NEW"
            try:
                self.db.update_release_status(
                    release_id_int,
                    status=release_status,
                    matched_book_id=matched_book_id,
                    enforce_precedence=False,
                )
            except Exception as exc:
                logger.debug(f"Re-bind status write failed for release #{release_id_int}: {exc}")

        if mode_clean == "bind":
            try:
                self.db.log_activity(
                    "SERIES_MANUALLY_MATCHED",
                    f"Release re-bound to series: '{previous_series}' -> '{canonical_name}'",
                    json.dumps({
                        "actor": actor,
                        "release_id": release_id_int,
                        "mode": mode_clean,
                        "previous_series": previous_series,
                        "target": canonical_name,
                        "calibre_series_id": target_calibre_id,
                        "remember": bool(remember),
                        "pinned": True,
                    }),
                )
            except Exception:
                pass

        if mode_clean == "bind":
            message = f"Release re-bound to '{canonical_name}'"
        elif mode_clean == "detach":
            message = f"Release detached as new series '{canonical_name}'"
        else:
            message = f"Release re-indexed under new series '{canonical_name}'"
        if release_status:
            message += f" ({release_status})"

        summary.update({
            "status": "success",
            "release_id": release_id_int,
            "mode": mode_clean,
            "previous_series": previous_series,
            "series_name": canonical_name,
            "calibre_series_id": target_calibre_id,
            "release_status": release_status,
            "new_status": release_status,
            "pinned": True,
            "message": message,
        })
        return summary

    def reset_series_identity(self, series_id: int) -> dict:
        """Per-series identity reset + one bounded automatic recovery pass.

        ``db.reset_series_identity`` forgets aliases/display/locks/pins for the
        series (it does not delete the tracker or change release titles). This
        wrapper then lets automatic matching take over again: one forced AniList
        title lookup, an auto-link attempt against the current Calibre snapshot,
        and a status reconcile. Everything after the DB reset is best-effort.
        """
        try:
            series_id_int = int(series_id)
        except (TypeError, ValueError):
            raise ValueError(f"invalid series id: {series_id!r}")

        try:
            summary = dict(self.db.reset_series_identity(
                series_id_int, actor="user", rekey_to_parsed=True
            ) or {})
        except TypeError:
            # Older DB layer without the parsed-title re-key extension.
            summary = dict(self.db.reset_series_identity(series_id_int, actor="user") or {})
        series_name = str(summary.get("series_name") or "").strip()
        linked = None
        reconciled: Dict[str, Any] = {}

        if series_name:
            try:
                row = self.db.get_series_by_name(series_name)
            except Exception as exc:
                logger.debug(f"Reset recovery row lookup failed for '{series_name}': {exc}")
                row = None
            if row is not None:
                target_name = (row.get("name") or series_name).strip() or series_name
                calibre_id = row.get("calibre_series_id")
                if calibre_id in (None, ""):
                    match_enabled = True
                    try:
                        match_enabled = self._setting_flag(
                            self.db.get_setting(SERIES_ANILIST_MATCH_ENABLED_KEY, "true"),
                            default=True,
                        )
                    except Exception:
                        match_enabled = True
                    if match_enabled:
                        try:
                            self.lookup_series_alternate_titles(target_name, force=True)
                        except Exception as exc:
                            logger.debug(f"Reset AniList recovery skipped for '{target_name}': {exc}")
                    try:
                        calibre_series_list = self._calibre_series_snapshot()
                        links = self.db.auto_link_series_to_calibre(
                            calibre_series_list, actor="user"
                        ) or []
                    except Exception as exc:
                        logger.debug(f"Reset auto-link recovery skipped: {exc}")
                        links = []
                    from app.parser import normalize_text
                    for link in links:
                        if str(link.get("status") or "success").strip().lower() != "success":
                            continue
                        if normalize_text(link.get("source") or "") != normalize_text(target_name):
                            continue
                        linked = dict(link)
                        linked_target = str(link.get("target") or target_name).strip()
                        if linked_target:
                            target_name = linked_target
                        break
                try:
                    reconciled = self.reconcile_series_releases(target_name)
                except Exception as exc:
                    logger.debug(f"Reset reconcile skipped for '{target_name}': {exc}")
                    reconciled = {}

        aliases_removed = int(summary.get("aliases_removed") or 0)
        releases_unpinned = int(summary.get("releases_unpinned") or 0)
        locked_cleared = int(summary.get("locked_cleared") or 0)
        releases_rekeyed = int(summary.get("releases_rekeyed") or 0)
        if not isinstance(reconciled, dict):
            reconciled = {}
        # Surface the DB re-key count in the reconciled summary the route returns.
        reconciled.setdefault("releases_rekeyed", releases_rekeyed)
        summary.update({
            "status": "success",
            "series_id": series_id_int,
            "reconciled": reconciled,
            "linked": linked,
            "message": (
                f"Identity reset for '{series_name}': "
                f"{aliases_removed} alias(es), {releases_unpinned} pin(s), "
                f"{locked_cleared} lock(s) cleared"
            ),
        })
        return summary

    # --- Calibre false-ownership repair (contract v4 §4) --------------------
    #
    # A tracker may carry a wrong calibre_series_id (AniList synonym/fuzzy
    # matching), which makes the series page report volumes as "In Calibre"
    # that are not actually owned. These two operators repair that: recheck
    # recomputes ownership from the real Calibre books, unlink removes the link
    # and resets the affected releases through the DB layer.

    def _series_row_by_id(self, series_id: int) -> Optional[dict]:
        """Tracker row by primary key (falls back to the plain series list)."""
        rows: List[dict] = []
        try:
            rows = self.db.get_series_identity_rows()
        except Exception:
            rows = []
        if not rows:
            try:
                rows = self.db.get_all_series(monitored_only=False)
            except Exception:
                rows = []
        for row in rows or []:
            if row.get("id") == series_id:
                return row
        return None

    @staticmethod
    def _calibre_id_matches(value: Any, target: Any) -> bool:
        """Compares a Calibre series id from a dict against the target id."""
        if value is None or target is None:
            return False
        try:
            return int(value) == int(target)
        except (TypeError, ValueError):
            return str(value) == str(target)

    @staticmethod
    def _current_owned_volumes(row: dict) -> List[float]:
        """Parses the tracker's stored owned_volumes_json (defensive)."""
        raw = row.get("owned_volumes_json") if isinstance(row, dict) else None
        try:
            values = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
        owned: List[float] = []
        if isinstance(values, list):
            for value in values:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number) and number >= 0:
                    owned.append(number)
        return sorted(set(owned))

    def _calibre_owned_volumes_for_series(self, calibre_series_id: Any,
                                          scanner: Any,
                                          books: List[dict]) -> List[float]:
        """Recomputes the owned volumes of one Calibre series from real books.

        Uses ``scanner.get_series_volumes_map`` (exactly like ``sync_calibre``)
        when available so omnibus books ("Vol 1-3") expand to every contained
        volume, and the finite ``series_index`` values of that series' books.
        """
        owned: set = set()
        try:
            volumes_map = scanner.get_series_volumes_map()
        except Exception as exc:
            logger.debug(f"Recheck volumes map unavailable: {exc}")
            volumes_map = None
        if isinstance(volumes_map, dict) and volumes_map:
            for key in (str(calibre_series_id), calibre_series_id):
                try:
                    mapped = volumes_map.get(key)
                except TypeError:
                    mapped = None
                if not mapped:
                    continue
                for value in mapped:
                    try:
                        number = float(value)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(number) and number >= 0:
                        owned.add(number)
                break
        for book in books or []:
            if not self._calibre_id_matches(book.get("series_id"), calibre_series_id):
                continue
            try:
                number = float(book.get("series_index"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number >= 0:
                owned.add(number)
        return sorted(owned)

    def _reset_stale_in_calibre_releases(self, series_name: str,
                                         owned_volumes: List[float]) -> int:
        """Repairs releases marked IN_CALIBRE whose volume is not actually owned.

        Explicit repair path (v4 "Recheck Calibre"): the normal reconcile path
        intentionally preserves IN_CALIBRE through status precedence, which would
        leave false-ownership rows stuck after a wrong Calibre link. Volumes in
        ``owned_volumes`` stay IN_CALIBRE; anything else returns to REQUESTED
        (volume > 1) / NEW (volume None/1.0). Pinned/veto rows are untouched.
        Returns the number of rows reset.
        """
        from app.parser import normalize_text
        name = (series_name or "").strip()
        if not name:
            return 0
        target_norm = normalize_text(name)
        if not target_norm:
            return 0
        owned = set()
        for value in owned_volumes or []:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                owned.add(number)
        try:
            releases = self.db.get_releases(limit=50000, include_ignored=True)
        except Exception as exc:
            logger.debug(f"Stale-In-Calibre scan failed for '{name}': {exc}")
            return 0
        reset = 0
        for release in releases or []:
            stored = (release.get("series_name") or "").strip()
            if not stored or normalize_text(stored) != target_norm:
                continue
            if self._release_identity_pinned(release):
                continue
            status = (release.get("status") or "").strip().upper()
            if status != "IN_CALIBRE":
                continue
            try:
                volume = float(release.get("volume")) if release.get("volume") is not None else None
            except (TypeError, ValueError):
                volume = None
            if volume is not None and volume in owned:
                continue
            target_status = "REQUESTED" if (volume is not None and volume > 1.0) else "NEW"
            try:
                # Explicit repair: status precedence must not preserve the false
                # IN_CALIBRE classification, and the stale matched book id must
                # actually be cleared (update_release_status COALESCEs it).
                with self.db.get_connection() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE releases SET status = ?, ignored = 0, "
                        "calibre_matched_book_id = NULL, updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ?;",
                        (target_status, release["id"]),
                    )
                    conn.commit()
                reset += 1
            except Exception as exc:
                logger.debug(
                    f"Stale In-Calibre reset failed for release #{release.get('id')}: {exc}"
                )
        if reset:
            logger.info(
                "Recheck reset %s stale In-Calibre release(s) for '%s'.", reset, name
            )
        return reset

    def recheck_series_calibre(self, series_id: int) -> Dict[str, Any]:
        """Re-reads Calibre and repairs a tracker's owned volumes/statuses.

        Contract v4 §4: verifies the linked Calibre series still exists (unlinks
        it when gone), recomputes ``owned_volumes_json`` from the real books
        (omnibus-aware), reconciles the tracker's releases against the real
        library, logs ``SERIES_CALIBRE_RECHECKED`` and returns a summary.
        Raises KeyError for an unknown tracker id; every Calibre read failure is
        contained and reported in the returned message (link left unchanged).
        """
        try:
            series_id_int = int(series_id)
        except (TypeError, ValueError):
            raise KeyError(f"series #{series_id!r} not found")
        row = self._series_row_by_id(series_id_int)
        if row is None:
            raise KeyError(f"series #{series_id_int} not found")
        series_name = (row.get("name") or "").strip()
        calibre_series_id = row.get("calibre_series_id")

        if calibre_series_id in (None, ""):
            return {
                "status": "success",
                "linked": False,
                "already_unlinked": True,
                "series_id": series_id_int,
                "series_name": series_name,
                "message": f"Series '{series_name}' is not linked to Calibre; nothing to recheck.",
            }

        read_error: Optional[str] = None
        scanner = None
        series_list: List[dict] = []
        books: List[dict] = []
        # A recheck must read the fresh Calibre library (contract v5 §6).
        self._invalidate_calibre_snapshot_cache()
        try:
            calibre_dir = self.db.get_setting("calibre_path", self.calibre_path)
            scanner = CalibreScanner(calibre_dir)
            if not scanner.is_available():
                read_error = (
                    f"Calibre library is not accessible at {getattr(scanner, 'db_path', calibre_dir)}"
                )
            else:
                series_list = list(scanner.get_all_series() or [])
                books = list(scanner.get_all_books() or [])
                # Fresh library read: seed the snapshot cache for the reconcile
                # pass (and any re-bind/status call right after the recheck).
                self._store_calibre_snapshot(series_list, books)
        except Exception as exc:
            logger.debug(f"Recheck Calibre read failed for series #{series_id_int}: {exc}")
            read_error = f"Could not read the Calibre library ({exc})"
            series_list = []
            books = []

        if read_error:
            return {
                "status": "success",
                "linked": True,
                "unlinked": False,
                "series_id": series_id_int,
                "series_name": series_name,
                "calibre_series_id": calibre_series_id,
                "owned_volumes": self._current_owned_volumes(row),
                "reconciled": {},
                "message": f"Recheck for '{series_name}' skipped: {read_error}; the link was left unchanged.",
            }

        linked_series = None
        for candidate in series_list:
            if self._calibre_id_matches(
                candidate.get("series_id", candidate.get("id")), calibre_series_id
            ):
                linked_series = candidate
                break

        if linked_series is None:
            unlink_summary: Dict[str, Any] = {}
            try:
                unlink_summary = dict(
                    self.db.unlink_series_from_calibre(series_id_int, actor="user") or {}
                )
            except Exception as exc:
                logger.debug(f"Recheck unlink failed for series #{series_id_int}: {exc}")
                return {
                    "status": "error",
                    "linked": True,
                    "unlinked": False,
                    "series_id": series_id_int,
                    "series_name": series_name,
                    "calibre_series_id": calibre_series_id,
                    "message": (
                        f"Calibre series #{calibre_series_id} no longer exists, but the "
                        f"unlink failed: {exc}"
                    ),
                }
            releases_reset = int(unlink_summary.get("releases_reset") or 0)
            try:
                self.db.log_activity(
                    "SERIES_CALIBRE_RECHECKED",
                    f"Calibre recheck unlinked '{series_name}'",
                    json.dumps({
                        "series": series_name,
                        "calibre_series_id": calibre_series_id,
                        "owned_volumes": 0,
                        "unlinked": True,
                        "releases_reset": releases_reset,
                    }),
                )
            except Exception:
                pass
            return {
                "status": "success",
                "linked": False,
                "unlinked": True,
                "series_id": series_id_int,
                "series_name": series_name,
                "calibre_series_id": calibre_series_id,
                "releases_reset": releases_reset,
                "already_unlinked": bool(unlink_summary.get("already_unlinked")),
                "message": (
                    f"Calibre series #{calibre_series_id} no longer exists; "
                    f"'{series_name}' was unlinked and {releases_reset} release(s) reset."
                ),
            }

        owned_volumes = self._calibre_owned_volumes_for_series(
            calibre_series_id, scanner, books
        )
        persist_ok = True
        try:
            persist_ok = bool(self.db.set_series_owned_volumes(series_id_int, owned_volumes))
        except Exception as exc:
            logger.debug(f"Recheck owned-volume persist failed for series #{series_id_int}: {exc}")
            persist_ok = False

        reconciled: Dict[str, Any] = {}
        stale_reset = 0
        try:
            stale_reset = self._reset_stale_in_calibre_releases(series_name, owned_volumes)
        except Exception as exc:
            logger.debug(f"Recheck stale-In-Calibre reset failed for '{series_name}': {exc}")
        try:
            reconciled = self.reconcile_series_releases(series_name)
        except Exception as exc:
            logger.debug(f"Recheck reconcile failed for '{series_name}': {exc}")
            reconciled = {}
        if isinstance(reconciled, dict):
            reconciled["stale_in_calibre_reset"] = stale_reset

        try:
            self.db.log_activity(
                "SERIES_CALIBRE_RECHECKED",
                f"Calibre ownership recomputed for '{series_name}'",
                json.dumps({
                    "series": series_name,
                    "calibre_series_id": calibre_series_id,
                    "owned_volumes": len(owned_volumes),
                    "stale_in_calibre_reset": stale_reset,
                    "reconciled": reconciled,
                }),
            )
        except Exception:
            pass

        in_calibre_count = int(reconciled.get("in_calibre") or 0)
        message = (
            f"Rechecked '{series_name}' against Calibre series #{calibre_series_id}: "
            f"{len(owned_volumes)} owned volume(s) recomputed, "
            f"{stale_reset} stale In-Calibre release(s) reset, "
            f"{in_calibre_count} release(s) now In Calibre."
        )
        if not persist_ok:
            message += " (Owned-volume storage update failed; releases were still reconciled.)"
        return {
            "status": "success",
            "linked": True,
            "unlinked": False,
            "series_id": series_id_int,
            "series_name": series_name,
            "calibre_series_id": calibre_series_id,
            "owned_volumes": owned_volumes,
            "reconciled": reconciled,
            "message": message,
        }

    def unlink_series_calibre(self, series_id: int) -> Dict[str, Any]:
        """Removes a tracker's Calibre link and resets its releases (contract v4).

        Delegates to ``db.unlink_series_from_calibre`` (which logs
        ``SERIES_CALIBRE_UNLINKED``), then returns the DB summary plus a
        user-facing message. Raises KeyError for an unknown tracker id.
        """
        try:
            series_id_int = int(series_id)
        except (TypeError, ValueError):
            raise KeyError(f"series #{series_id!r} not found")
        summary = dict(
            self.db.unlink_series_from_calibre(series_id_int, actor="user") or {}
        )
        series_name = str(summary.get("series_name") or "").strip()
        releases_reset = int(summary.get("releases_reset") or 0)
        if summary.get("already_unlinked"):
            summary["message"] = (
                f"Series '{series_name}' was already unlinked from Calibre."
            )
        else:
            summary["message"] = (
                f"Series '{series_name}' unlinked from Calibre "
                f"(#{summary.get('old_calibre_series_id')}); "
                f"{releases_reset} release(s) returned to Wanted/New."
            )
        logger.info(
            "Calibre unlink for series #%s (%s): %s release(s) reset.",
            summary.get("series_id", series_id_int),
            series_name,
            releases_reset,
        )
        return summary

    @staticmethod
    def _volume_list(raw: Any) -> List[float]:
        """Parses a release volume JSON column into a rounded float list."""
        values: Any = raw
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return []
            try:
                values = json.loads(text)
            except (TypeError, ValueError):
                return []
        if not isinstance(values, (list, tuple, set)):
            return []
        clean: List[float] = []
        for value in values:
            try:
                clean.append(round(float(value), 2))
            except (TypeError, ValueError):
                continue
        return sorted(set(clean))

    def check_and_filter_active_downloads(self) -> int:
        """Deselects already-owned volume files in active qBittorrent bundles.

        The manual/auto submit applies selection immediately, but torrent
        metadata (file names) often resolves only after the torrent is added.
        This periodic pass retries until the files can be mapped, then records
        the release/wanted signature and stops calling qBittorrent for it.
        """
        try:
            rows = self.db.get_active_qbit_bundle_downloads()
        except Exception as exc:
            logger.debug("Selective filter could not load active downloads: %s", exc)
            return 0
        if not rows:
            return 0

        try:
            qbit = qbit_client_from_settings(self.db.get_all_settings())
        except Exception as exc:
            logger.debug("Selective filter could not build qBittorrent client: %s", exc)
            return 0
        if not getattr(qbit, "enabled", True):
            return 0

        filtered = 0
        for row in rows:
            release_id = int(row.get("id") or 0)
            torrent_hash = str(row.get("torrent_hash") or "").strip()
            if not release_id or not torrent_hash:
                continue
            wanted = self._volume_list(row.get("download_wanted_json"))
            if not wanted:
                wanted = self._volume_list(row.get("missing_vols_json"))
            if not wanted:
                continue
            volumes = self._volume_list(row.get("volumes_json"))
            if volumes and len(wanted) >= len(volumes):
                # Nothing owned inside this bundle; no file needs deselecting.
                continue
            signature = (release_id, torrent_hash, tuple(wanted))
            if signature in self._selective_filter_done:
                continue
            try:
                result = qbit.apply_selective_volume_download(
                    torrent_hash,
                    wanted_volumes=wanted,
                    series_name=str(row.get("series_name") or "").strip() or None,
                ) or {}
            except Exception as exc:
                logger.debug(
                    "Selective filter failed for release #%s (%s): %s",
                    release_id, torrent_hash[:10], exc,
                )
                continue

            status = str(result.get("status") or "")
            if status == "pending_metadata":
                # Metadata is not available yet; retry on the next pass.
                continue
            if status in ("filtered", "all_owned"):
                skipped = result.get("skipped_volumes") or []
                kept = result.get("kept_volumes") or []
                self.db.log_activity(
                    "SELECTIVE_DOWNLOAD",
                    f"Selective download for release #{release_id}: "
                    f"downloading {len(kept)} missing volume file(s), "
                    f"skipped {len(skipped)} owned file(s).",
                    json.dumps({
                        "release_id": release_id,
                        "torrent_hash": torrent_hash,
                        "wanted_volumes": wanted,
                        "skipped_volumes": skipped,
                        "kept_volumes": kept,
                        "status": status,
                    }),
                )
                filtered += 1
            elif status == "disabled":
                return filtered
            self._selective_filter_done.add(signature)
        return filtered

    def start(self):
        try:
            background_tasks.prune(self.db, days=7)
            stale = self.db.mark_stale_background_tasks()
            if stale:
                logger.info("Marked %s interrupted background task(s) failed", stale)
        except Exception as exc:
            logger.debug("Background-task startup cleanup skipped: %s", exc)
        calibre_min = int(self.db.get_setting("calibre_sync_interval_minutes", "15"))
        feed_min = int(self.db.get_setting("feed_check_interval_minutes", "60"))

        # Schedule periodic jobs
        self.scheduler.add_job(
            self.sync_calibre,
            "interval",
            minutes=max(1, calibre_min),
            id="calibre_sync",
            replace_existing=True
        )
        self.scheduler.add_job(
            self.check_feeds,
            "interval",
            minutes=max(5, feed_min),
            id="feed_check",
            replace_existing=True
        )
        # qBittorrent file priorities can only be set once torrent metadata has
        # resolved; retry until each active bundle is filtered, then stop.
        try:
            self.scheduler.add_job(
                self.check_and_filter_active_downloads,
                "interval",
                seconds=SELECTIVE_FILTER_INTERVAL_SECONDS,
                id="qbit_selective_filter",
                replace_existing=True,
                max_instances=1,
            )
        except Exception as exc:
            logger.warning(f"Could not schedule qbit_selective_filter: {exc}")
        # Automatic activity-log pruning (settings activity_retention_days /
        # activity_max_rows). A database hiccup must never break startup, so both
        # the immediate catch-up call and the recurring job are guarded.
        try:
            self.prune_activity_logs()
        except Exception as exc:
            logger.debug(f"Startup activity-log prune skipped: {exc}")
        try:
            self.scheduler.add_job(
                self.prune_activity_logs,
                "interval",
                minutes=ACTIVITY_PRUNE_INTERVAL_MINUTES,
                id="activity_prune",
                replace_existing=True,
            )
        except Exception as exc:
            logger.warning(f"Could not schedule activity_prune: {exc}")
        self.scheduler.start()
        logger.info("Scheduler started successfully.")

    @staticmethod
    def _activity_prune_int_setting(db, key: str, default: int) -> int:
        """Reads an integer setting with a hard default (invalid -> default)."""
        try:
            value = int(db.get_setting(key, str(default)))
        except (TypeError, ValueError):
            value = default
        return max(0, value)

    def prune_activity_logs(self) -> Dict[str, Any]:
        """Prunes old/excess ``activity_logs`` rows using the retention settings.

        ``activity_retention_days`` (default 90; 0 disables the age rule) and
        ``activity_max_rows`` (default 50000; 0 disables the row cap) are read
        with int guards; the actual pruning is delegated to
        ``db.prune_activity_logs(days, max_rows)``. An INFO line is logged only
        when rows were removed. Never raises.
        """
        result: Dict[str, Any] = {
            "status": "success",
            "removed": 0,
            "removed_by_age": 0,
            "removed_by_rows": 0,
            "remaining": 0,
        }
        try:
            days = self._activity_prune_int_setting(
                self.db, ACTIVITY_RETENTION_DAYS_KEY, ACTIVITY_RETENTION_DAYS_DEFAULT
            )
            max_rows = self._activity_prune_int_setting(
                self.db, ACTIVITY_MAX_ROWS_KEY, ACTIVITY_MAX_ROWS_DEFAULT
            )
            pruned = self.db.prune_activity_logs(days, max_rows)
            if isinstance(pruned, dict):
                result.update(pruned)
            result["status"] = "success"
            try:
                removed = int(result.get("removed") or 0)
            except (TypeError, ValueError):
                removed = 0
            if removed > 0:
                logger.info(
                    "Activity log pruned: %s row(s) removed "
                    "(%s by age, %s by row cap, %s remaining).",
                    removed,
                    result.get("removed_by_age", 0),
                    result.get("removed_by_rows", 0),
                    result.get("remaining", 0),
                )
        except Exception as exc:
            logger.warning(f"Activity log prune skipped: {exc}")
            result["status"] = "error"
            result["message"] = str(exc)
        return result

    def reschedule_jobs(self, calibre_minutes: Optional[int] = None, feed_minutes: Optional[int] = None):
        """
        Dynamically updates scheduler intervals when settings are changed.
        """
        if calibre_minutes is None:
            try:
                calibre_minutes = int(self.db.get_setting("calibre_sync_interval_minutes", "15"))
            except ValueError:
                calibre_minutes = 15

        if feed_minutes is None:
            try:
                feed_minutes = int(self.db.get_setting("feed_check_interval_minutes", "60"))
            except ValueError:
                feed_minutes = 60

        calibre_minutes = max(1, calibre_minutes)
        feed_minutes = max(5, feed_minutes)

        if self.scheduler.running:
            try:
                self.scheduler.reschedule_job(
                    "calibre_sync",
                    trigger="interval",
                    minutes=calibre_minutes
                )
                logger.info(f"Rescheduled calibre_sync to run every {calibre_minutes} minutes")
            except Exception as e:
                logger.warning(f"Could not reschedule calibre_sync: {e}")

            try:
                self.scheduler.reschedule_job(
                    "feed_check",
                    trigger="interval",
                    minutes=feed_minutes
                )
                logger.info(f"Rescheduled feed_check to run every {feed_minutes} minutes")
            except Exception as e:
                logger.warning(f"Could not reschedule feed_check: {e}")

    def stop(self):
        self.scheduler.shutdown(wait=False)
        self._cover_executor.shutdown(wait=False)

    @background_tasks.tracked_task("calibre_sync", "Calibre library sync")
    def sync_calibre(self) -> Dict[str, Any]:
        """
        Synchronizes series and books from Calibre's metadata.db.
        1. Auto-populates series into series_tracking.
        2. Checks all currently REQUESTED releases.
        3. Removes/resolves requests for books newly added to Calibre.
        """
        if self.is_syncing_calibre:
            return {"status": "in_progress", "message": "Calibre sync already running"}

        self.is_syncing_calibre = True
        self._task_report(message="Reading Calibre library…", progress_pct=5)
        try:
            # A sync must read the fresh Calibre library (contract v5 §6).
            self._invalidate_calibre_snapshot_cache()
            calibre_dir = self.db.get_setting("calibre_path", self.calibre_path)
            scanner = CalibreScanner(calibre_dir)

            if not scanner.is_available():
                msg = f"Calibre library metadata.db not accessible at: {scanner.db_path}"
                logger.warning(msg)
                self.db.log_activity("CALIBRE_SYNC", "Calibre sync failed", msg)
                return {"status": "error", "message": msg}

            series_list = scanner.get_all_series()
            books_list = scanner.get_all_books()
            # The sync just read the fresh library; seed the snapshot cache so
            # the identity/reconcile work below (and a concurrent re-bind) reuse
            # it instead of rescanning.
            self._store_calibre_snapshot(series_list, books_list)

            # Omnibus-aware owned volumes: get_series_volumes_map() expands
            # bundled omnibus titles ("Vol 1-3") into their contained volumes, so
            # a periodic sync must not degrade owned_volumes_json to the single
            # latest_index (which would make existing volumes look missing).
            volumes_map = None
            try:
                volumes_map = scanner.get_series_volumes_map()
            except Exception as exc:
                logger.debug(f"Calibre volumes map unavailable; owned volumes may be coarse: {exc}")

            # 1. Auto-populate series into database
            if volumes_map:
                updated_series_count = self.db.sync_calibre_series(series_list, volumes_map=volumes_map)
            else:
                updated_series_count = self.db.sync_calibre_series(series_list)

            self._task_report(
                message=f"Synced {updated_series_count} series; resolving requests…",
                progress_pct=55,
            )
            # 2. Check and resolve active requests
            pending_requests = self.db.get_pending_requests()
            matcher = CalibreMatcher(series_list, books_list)
            newly_acquired = matcher.resolve_pending_requests(pending_requests)

            self._task_report(
                message=f"Resolved {len(newly_acquired)} acquired book(s); finishing…",
                progress_pct=85,
            )
            notifier = self.get_notifier()
            notify_acquired_enabled = self._webhook_setting_enabled(
                self.db, WEBHOOK_ACQUIRED_KEY
            )
            for acq in newly_acquired:
                # Mark as acquired in Calibre (removes from active request list)
                self.db.update_release_status(acq["release_id"], status="IN_CALIBRE", matched_book_id=acq["matched_book_id"])
                self.db.log_activity(
                    "REQUEST_RESOLVED",
                    f"Book acquired into Calibre: '{acq['title']}'",
                    f"Matched Calibre Book #{acq['matched_book_id']} ({acq['reason']})"
                )
                if notify_acquired_enabled:
                    notifier.notify_acquired(acq)

            # --- 3. Series identity: auto-link, bounded AniList enrichment, reconcile ---
            # Everything below is best-effort: a metadata outage or a database
            # that has not had the identity migration applied must never break
            # the Calibre sync itself. Counters are additive return keys.
            series_linked = 0
            series_anilist_synced = 0
            auto_links: List[Dict[str, Any]] = []
            try:
                auto_links = list(
                    self.db.auto_link_series_to_calibre(series_list, actor="scheduler") or []
                )
            except Exception as exc:
                logger.debug(f"Series auto-link skipped: {exc}")

            try:
                sync_enabled = self._setting_flag(
                    self.db.get_setting(SERIES_ANILIST_SYNC_ENABLED_KEY, "true"),
                    default=True,
                )
                if sync_enabled:
                    try:
                        sync_limit = int(
                            self.db.get_setting(
                                SERIES_ANILIST_SYNC_LIMIT_KEY,
                                str(SERIES_ANILIST_SYNC_LIMIT_DEFAULT),
                            )
                        )
                    except (TypeError, ValueError):
                        sync_limit = SERIES_ANILIST_SYNC_LIMIT_DEFAULT
                    enrichment = self.enrich_tracked_series_anilist(limit=sync_limit)
                    series_anilist_synced = int(enrichment.get("synced") or 0)
                    # The enrichment pass auto-links its own rows; carry those
                    # summaries up so step 3 reconciles/logs them too.
                    auto_links.extend(enrichment.get("links") or [])
                    if series_anilist_synced:
                        # Enriched rows may now match a Calibre series (AniList
                        # synonyms/romaji/native titles).
                        try:
                            auto_links.extend(
                                self.db.auto_link_series_to_calibre(
                                    series_list, actor="scheduler"
                                ) or []
                            )
                        except Exception as exc:
                            logger.debug(f"Post-enrichment auto-link skipped: {exc}")
            except Exception as exc:
                logger.debug(f"Series AniList enrichment skipped: {exc}")

            linked_targets = self._link_targets(auto_links)
            series_linked = len(linked_targets)
            for target in linked_targets:
                reconciled: Dict[str, Any] = {}
                try:
                    reconciled = self.reconcile_series_releases(target)
                except Exception as exc:
                    logger.debug(f"Reconcile after auto-link failed for '{target}': {exc}")
                try:
                    self.db.log_activity(
                        "SERIES_AUTO_LINKED",
                        f"Series auto-linked to Calibre: '{target}'",
                        json.dumps({
                            "series": target,
                            "reconciled": reconciled,
                        }),
                    )
                except Exception:
                    pass

            log_msg = f"Synced {updated_series_count} series, {len(books_list)} books. Resolved {len(newly_acquired)} requests."
            if series_linked or series_anilist_synced:
                log_msg += f" Linked {series_linked} series, enriched {series_anilist_synced} with AniList."
            logger.info(log_msg)
            self.db.log_activity("CALIBRE_SYNC", "Calibre library sync completed", log_msg)

            return {
                "status": "success",
                "series_count": updated_series_count,
                "books_count": len(books_list),
                "resolved_requests": len(newly_acquired),
                "resolved_items": newly_acquired,
                # Additive series-identity counters (legacy callers keep every key).
                "series_linked": series_linked,
                "series_anilist_synced": series_anilist_synced,
            }
        except Exception as e:
            logger.error(f"Error during Calibre sync: {e}", exc_info=True)
            self.db.log_activity("ERROR", "Calibre sync error", str(e))
            return {"status": "error", "message": str(e)}
        finally:
            self.is_syncing_calibre = False

    # --- Time-aware (catch-up) feed scanning helpers ---

    def feed_catchup_enabled(self) -> bool:
        """Whether automatic catch-up scanning is enabled (default: yes)."""
        try:
            value = self.db.get_setting(FEED_CATCHUP_ENABLED_KEY, "true")
        except Exception as exc:
            logger.debug(f"Could not read {FEED_CATCHUP_ENABLED_KEY}: {exc}")
            return True
        return (value or "true").strip().lower() != "false"

    def feed_catchup_max_pages(self) -> int:
        """Global page budget for one catch-up scan (clamped to 1..50)."""
        try:
            value = self.db.get_setting(FEED_CATCHUP_MAX_PAGES_KEY, str(DEFAULT_CATCHUP_MAX_PAGES))
        except Exception as exc:
            logger.debug(f"Could not read {FEED_CATCHUP_MAX_PAGES_KEY}: {exc}")
            value = str(DEFAULT_CATCHUP_MAX_PAGES)
        return clamp_max_pages(value, default=DEFAULT_CATCHUP_MAX_PAGES)

    def _persist_feed_watermarks(
        self,
        feed: Dict[str, Any],
        config: Dict[str, Any],
        cutoff: Optional[str],
        anchor: Optional[str],
        scan_started_at: str,
    ) -> None:
        """Merges the new scan watermarks into ``feeds.config_json``.

        ``database.py`` has no config-only update helper and is out of scope for
        this feature, so the feed row's own values are passed back to the
        existing ``update_feed`` unchanged apart from the merged config.
        """
        merged = dict(config or {})
        if cutoff:
            merged["last_scan_oldest_pub_date"] = cutoff
        if anchor:
            merged["last_scan_anchor_guid"] = anchor
        merged["last_scan_started_at"] = scan_started_at
        try:
            interval = int(feed.get("check_interval_minutes") or 60)
        except (TypeError, ValueError):
            interval = 60
        self.db.update_feed(
            feed["id"],
            feed["name"],
            feed["feed_type"],
            feed["url"],
            json.dumps(merged),
            bool(feed.get("enabled", True)),
            bool(feed.get("auto_request", True)),
            interval,
        )

    @background_tasks.tracked_task("feed_scan", "Feed scan")
    def check_feeds(self) -> Dict[str, Any]:
        """
        Polls all enabled feeds, checks releases against Calibre,
        and requests new volumes for monitored series.
        """
        from app.parser import normalize_text

        if self.is_checking_feeds:
            return {"status": "in_progress", "message": "Feed check already running"}

        self.is_checking_feeds = True
        total_found = 0
        total_requested = 0
        skipped_manga = 0
        skipped_audiobooks = 0

        # Time-aware catch-up scan (additive counters, old return keys kept).
        catchup_enabled = self.feed_catchup_enabled()
        catchup_max_pages_setting = self.feed_catchup_max_pages()
        scan_started_at = datetime.now(timezone.utc).isoformat()
        feeds_catchup = 0
        catchup_extra_items = 0
        max_pages_reached = 0

        # Refresh the per-scan verification budget so a large backlog of
        # unverified releases drains over successive scans.
        self.media_resolver = self._build_media_resolver()

        # Per-scan series-identity state. A metadata/DB failure degrades to the
        # classic matching path; it never aborts the scan.
        identity_snapshot: Dict[str, Any] = {
            "rows": [], "aliases": [], "alias_map": {},
            "calibre_series": [], "calibre_index": None,
        }
        series_match_enabled = True
        series_match_budget = SERIES_ANILIST_MATCH_BUDGET_DEFAULT
        alt_lookups_used = 0

        try:
            calibre_dir = self.db.get_setting("calibre_path", self.calibre_path)
            scanner = CalibreScanner(calibre_dir)
            calibre_series = scanner.get_all_series() if scanner.is_available() else []
            calibre_books = scanner.get_all_books() if scanner.is_available() else []
            try:
                identity_snapshot = self.series_identity_snapshot()
            except Exception as exc:
                logger.debug(f"Series identity snapshot skipped: {exc}")
            try:
                matcher = CalibreMatcher(
                    calibre_series,
                    calibre_books,
                    series_aliases=self._series_alias_map_for_matcher(identity_snapshot),
                )
            except Exception as exc:
                logger.debug(f"Alias-aware matcher unavailable ({exc}); using classic matching")
                matcher = CalibreMatcher(calibre_series, calibre_books)
            try:
                series_match_enabled = self._setting_flag(
                    self.db.get_setting(SERIES_ANILIST_MATCH_ENABLED_KEY, "true"),
                    default=True,
                )
            except Exception:
                series_match_enabled = True
            try:
                series_match_budget = max(
                    0,
                    int(self.db.get_setting(
                        SERIES_ANILIST_MATCH_BUDGET_KEY,
                        str(SERIES_ANILIST_MATCH_BUDGET_DEFAULT),
                    )),
                )
            except (TypeError, ValueError):
                series_match_budget = SERIES_ANILIST_MATCH_BUDGET_DEFAULT

            # One bounded AniList publication-lookup budget per scan (contract
            # v3 §4): unmarked Nyaa bundles may need a metadata lookup each.
            publication_budget: Dict[str, int] = {
                "remaining": max(0, series_match_budget)
            }

            # Get monitored series names and blacklisted series for filtering
            monitored_series = {s["name"].lower(): s for s in self.db.get_all_series(monitored_only=True)}
            blacklisted_series = {b["name"].lower() for b in self.db.get_blacklisted_series()}
            notifier = self.get_notifier()
            # Webhook event toggles are read once per scan, not per release.
            notify_new_requests_enabled = self._webhook_setting_enabled(
                self.db, WEBHOOK_NEW_REQUEST_KEY
            )

            feeds = [f for f in self.db.get_all_feeds() if f.get("enabled")]
            self._task_report(total=len(feeds), message=f"Scanning {len(feeds)} enabled feed(s)…")
            for feed_index, feed in enumerate(feeds, 1):

                feed_id = feed["id"]
                feed_name = feed["name"]
                feed_type = feed["feed_type"]
                url = feed["url"]
                auto_request = bool(feed["auto_request"])
                config = json.loads(feed["config_json"] or "{}")
                is_nyaa_feed = (
                    str(feed_type or "").strip().lower() in NYAA_FEED_TYPES
                    or "nyaa.si" in str(url or "").lower()
                )

                try:
                    provider = create_provider(feed_type, feed_name, url, config)

                    # --- Time-aware scan (catch-up backwards to the previous
                    # scan) ---------------------------------------------------
                    # Watermarks live in config_json; no schema change.  The
                    # per-feed override wins over the global page budget.
                    previous_cutoff = config.get("last_scan_oldest_pub_date") if isinstance(config, dict) else None
                    previous_anchor = config.get("last_scan_anchor_guid") if isinstance(config, dict) else None
                    effective_max_pages = clamp_max_pages(
                        config.get("catchup_max_pages") if isinstance(config, dict) else None,
                        default=catchup_max_pages_setting,
                    )
                    catchup_active = bool(
                        catchup_enabled and (previous_cutoff or previous_anchor)
                    )
                    self._task_report(
                        done=feed_index - 1, total=len(feeds),
                        message=f"Scanning {feed_name}…",
                    )
                    catchup_meta: Dict[str, Any] = {}
                    if catchup_active:
                        raw_releases = list(provider.fetch_releases_since(
                            cutoff_iso=previous_cutoff,
                            stop_guid=previous_anchor,
                            max_pages=effective_max_pages,
                        ) or [])
                        catchup_meta = dict(getattr(provider, "last_scan_meta", None) or {})
                    else:
                        raw_releases = list(provider.fetch_releases() or [])

                    # Compute the watermarks before filtering so chapter drops
                    # still count as "seen" for coverage purposes.
                    new_cutoff, new_anchor = compute_scan_watermarks(
                        previous_cutoff,
                        previous_anchor,
                        raw_releases,
                        reached_boundary=(
                            True if not catchup_active
                            else bool(catchup_meta.get("reached_boundary"))
                        ),
                        scan_started_at=scan_started_at,
                    )

                    releases = self.filter_chapter_releases(raw_releases, self.chapter_context())
                    total_found += len(releases)

                    new_requests_for_feed = 0
                    for r in releases:
                        series_name = r.series_name or ""
                        canonical_series_name = series_name
                        identity: Optional[Dict[str, Any]] = None
                        rel_dict = r.to_dict()

                        # A manually re-bound release is pinned: automatic
                        # identity must never canonicalise or re-key it. It is
                        # still matched against its pinned (stored) series so the
                        # ownership status stays correct.
                        pinned_row = self._release_row_by_guid(r.guid) if r.guid else None
                        pinned = self._release_identity_pinned(pinned_row)
                        if pinned:
                            stored_series = (pinned_row.get("series_name") or "").strip()
                            if stored_series:
                                series_name = stored_series
                                canonical_series_name = stored_series
                            rel_dict["series_name"] = canonical_series_name or None
                        else:
                            # Canonicalise the stored/provider series spelling to
                            # the stable tracked key before matching (alias
                            # override -> tracked display/AniList titles -> exact
                            # -> Calibre fuzzy).
                            if series_name:
                                try:
                                    identity = self.resolve_release_series_identity(rel_dict, identity_snapshot)
                                except Exception as exc:
                                    logger.debug(f"Series identity resolve failed for '{series_name}': {exc}")
                                    identity = None
                                if identity:
                                    candidate_name = (identity.get("series_name") or "").strip()
                                    if candidate_name:
                                        canonical_series_name = candidate_name
                            rel_dict["series_name"] = canonical_series_name or series_name or None

                        # Media classification now runs before matching: the Nyaa
                        # bundle inference needs it and manga must be dropped
                        # before any matcher/DB work (audiobooks are stored).
                        from app.media_type import MANGA, AUDIOBOOK, UNKNOWN
                        media_type = self.classify_media(r)
                        if media_type == MANGA:
                            skipped_manga += 1
                            continue
                        if media_type == AUDIOBOOK:
                            skipped_audiobooks += 1
                        inferred_bundle = False

                        # If either the original or canonical spelling is
                        # blacklisted, the release stays blacklisted.
                        is_blacklisted = bool(
                            series_name and series_name.lower() in blacklisted_series
                        ) or bool(
                            canonical_series_name and canonical_series_name.lower() in blacklisted_series
                        )

                        if is_blacklisted:
                            # Immediately mark as BLACKLISTED
                            status = "BLACKLISTED"
                            match_result = {"status": "BLACKLISTED", "matched_book_id": None}
                            is_monitored = False
                            eff_vol = float(r.volume) if r.volume is not None else 1.0
                        else:
                            alt_names = list(identity.get("alt_names") or []) if identity else []
                            if pinned and canonical_series_name:
                                alt_names = self._cached_series_alternate_titles(
                                    canonical_series_name
                                )
                            # Unmarked Nyaa light-novel batch release? Infer the
                            # complete series volume set from AniList before the
                            # Calibre matcher runs (contract v3 §4).
                            if (
                                media_type not in (MANGA, AUDIOBOOK)
                                and canonical_series_name
                            ):
                                inferred_bundle = self.infer_nyaa_bundle_volumes(
                                    rel_dict,
                                    canonical_series_name,
                                    is_nyaa_feed,
                                    publication_budget,
                                )
                            # Evaluate against Calibre
                            match_result = matcher.check_release(
                                rel_dict,
                                auto_request_monitored=auto_request,
                                alt_series_names=alt_names or None,
                            )
                            if inferred_bundle:
                                # Persist the bundle's complete missing set.
                                rel_dict["missing_volumes"] = (
                                    match_result.get("missing_volumes") or []
                                )

                            # Bounded AniList lookup fallback (contract §5.4): a NEW
                            # vol-1/unnumbered release whose series spelling nobody
                            # knows may still be resolvable through AniList titles.
                            # Never for a pinned release: the re-bind must survive.
                            if (
                                not pinned
                                and match_result.get("status") == "NEW"
                                and canonical_series_name
                                and not (identity or {}).get("matched")
                                and (r.volume is None or r.volume == 1.0)
                                and series_match_enabled
                                and alt_lookups_used < series_match_budget
                            ):
                                alt_lookups_used += 1
                                try:
                                    anilist_titles = self.lookup_series_alternate_titles(
                                        canonical_series_name
                                    )
                                except Exception as exc:
                                    logger.debug(
                                        f"AniList title fallback failed for '{canonical_series_name}': {exc}"
                                    )
                                    anilist_titles = []
                                if anilist_titles:
                                    retry_payload = dict(rel_dict)
                                    retry_payload["series_name"] = canonical_series_name
                                    retry_result = matcher.check_release(
                                        retry_payload,
                                        auto_request_monitored=auto_request,
                                        alt_series_names=anilist_titles,
                                    )
                                    if retry_result.get("status") != "NEW":
                                        match_result = retry_result
                                        adopted = (
                                            retry_result.get("canonical_series_name") or ""
                                        ).strip()
                                        if adopted:
                                            canonical_series_name = adopted
                                            rel_dict["series_name"] = adopted

                            # Determine if series is monitored
                            is_monitored = False
                            if canonical_series_name and canonical_series_name.lower() in monitored_series:
                                is_monitored = True
                            elif series_name and series_name.lower() in monitored_series:
                                is_monitored = True
                            elif match_result.get("matched_series"):
                                is_monitored = True

                            status = match_result["status"]
                            eff_vol = match_result.get("effective_volume") or r.volume or 1.0

                            # If not matched to any monitored series and auto_request is strict, keep as NEW
                            if status == "REQUESTED" and not is_monitored and not auto_request:
                                status = "NEW"
                            elif status == "NEW" and is_monitored and auto_request:
                                # Series monitored by user, but release is not in Calibre -> REQUEST it!
                                status = "REQUESTED"

                        final_volume = r.volume if r.volume is not None else (eff_vol if (is_monitored or status in ("IN_CALIBRE", "REQUESTED")) else None)
                        if inferred_bundle and rel_dict.get("volume") is not None:
                            # Bundle releases are stored as volume 1 of the set.
                            final_volume = float(rel_dict["volume"])

                        rel_dict.update({
                            "source_feed_id": feed_id,
                            "source_feed_name": feed_name,
                            "status": status,
                            "volume": final_volume,
                            "calibre_matched_book_id": match_result.get("matched_book_id"),
                            "media_type": media_type if media_type else UNKNOWN
                        })

                        is_new, rel_id = self._upsert(rel_dict)

                        # An existing row keeps its stored series_name through the
                        # upsert (deliberate stable-key rule). When identity
                        # resolution proved a different stable tracked key, re-key
                        # the row so a scan cannot split one series across spellings.
                        if (
                            not pinned
                            and not is_new and rel_id and rel_id > 0
                            and canonical_series_name
                            and normalize_text(canonical_series_name) != normalize_text(series_name)
                            and (
                                (identity or {}).get("matched")
                                or (
                                    match_result.get("matched_series")
                                    and match_result.get("canonical_series_name")
                                )
                            )
                        ):
                            stored_row = None
                            try:
                                stored_row = self.db.get_release_by_id(rel_id)
                            except Exception:
                                stored_row = None
                            if stored_row:
                                stored_name = (stored_row.get("series_name") or "").strip()
                                stored_status = (stored_row.get("status") or "").strip().upper()
                                if (
                                    stored_name
                                    and stored_status not in ("IGNORED", "BLACKLISTED")
                                    and normalize_text(stored_name) != normalize_text(canonical_series_name)
                                ):
                                    self._rekey_release_series_name(rel_id, canonical_series_name)

                        if is_new and inferred_bundle:
                            try:
                                bundle_publication = (
                                    self.publication_metadata_for(canonical_series_name) or {}
                                )
                                self.db.log_activity(
                                    "BUNDLE_VOLUMES_INFERRED",
                                    f"Bundle volumes inferred for '{r.title}'",
                                    json.dumps({
                                        "series": canonical_series_name or series_name,
                                        "volumes": len(rel_dict.get("volumes") or []),
                                        "anilist_status": bundle_publication.get("status"),
                                        "anilist_format": bundle_publication.get("format"),
                                        "title": r.title,
                                    }),
                                )
                            except Exception as exc:
                                logger.debug(f"Bundle inference activity log failed: {exc}")

                        # Pre-cache cover image asynchronously without blocking feed scanning
                        if r.cover_url:
                            try:
                                self._cover_executor.submit(self.cover_cache.cache_sync, r.cover_url)
                            except Exception:
                                pass

                        if is_new and status == "REQUESTED":
                            total_requested += 1
                            new_requests_for_feed += 1
                            self.db.log_activity(
                                "NEW_RELEASE",
                                f"New release requested: {r.title}",
                                f"Series: {canonical_series_name or series_name} Vol {final_volume} from '{feed_name}'"
                            )
                            if notify_new_requests_enabled:
                                notifier.notify_new_request(rel_dict)

                    self.db.update_feed_status(feed_id, status="OK")
                    # Persist the time-aware watermarks only after a fully
                    # successful scan, so a failed scan is simply retried.
                    self._persist_feed_watermarks(
                        feed, config if isinstance(config, dict) else {},
                        new_cutoff, new_anchor, scan_started_at,
                    )

                    if catchup_active:
                        feeds_catchup += 1
                        extra_pages = int(catchup_meta.get("extra_pages") or 0)
                        extra_items = int(catchup_meta.get("extra_items") or 0)
                        catchup_extra_items += extra_items
                        hit_page_cap = bool(
                            catchup_meta.get("max_pages_reached")
                            or catchup_meta.get("stop_reason") == "page_cap"
                        )
                        if hit_page_cap:
                            max_pages_reached += 1
                        # One summary entry per extended scan; never per item.
                        if extra_pages > 0 or hit_page_cap:
                            self.db.log_activity(
                                "FEED_CATCHUP",
                                f"Feed catch-up scan extended: {feed_name}",
                                (
                                    f"extra_pages={extra_pages}, extra_items={extra_items}, "
                                    f"stop_reason={catchup_meta.get('stop_reason') or 'unknown'}, "
                                    f"max_pages={effective_max_pages}"
                                ),
                            )
                    logger.info(f"Checked feed '{feed_name}': {len(releases)} items parsed, {new_requests_for_feed} new requests.")

                except Exception as feed_err:
                    err_msg = str(feed_err)
                    logger.error(f"Error checking feed '{feed_name}': {err_msg}")
                    self.db.update_feed_status(feed_id, status="ERROR", error=err_msg)
                    self.db.log_activity("ERROR", f"Feed check error: {feed_name}", err_msg)

            return {
                "status": "success",
                "total_found": total_found,
                "total_requested": total_requested,
                "skipped_manga": skipped_manga,
                "skipped_audiobooks": skipped_audiobooks,
                # Additive catch-up diagnostics (old callers keep every key).
                "feeds_catchup": feeds_catchup,
                "catchup_extra_items": catchup_extra_items,
                "max_pages_reached": max_pages_reached,
            }
        except Exception as e:
            logger.error(f"Error checking feeds: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}
        finally:
            self.is_checking_feeds = False

    def ingest_master_index(self) -> Dict[str, Any]:
        """
        Ingests the complete series index from J-Novels (https://jnovels.com/light-novel-epub-jp/).
        1. Auto-links canonical series hubs for Calibre series.
        2. Populates Discovery panel with 1,300+ unreviewed series.
        """
        from app.providers.jnovels import JNovelsProvider
        import hashlib
        provider = JNovelsProvider()
        try:
            items = provider.fetch_master_index()
            calibre_dir = self.db.get_setting("calibre_path", self.calibre_path)
            scanner = CalibreScanner(calibre_dir)
            calibre_series = {s["series_name"].lower(): s for s in scanner.get_all_series()} if scanner.is_available() else {}
            blacklisted = {b["name"].lower() for b in self.db.get_blacklisted_series()}

            linked_calibre = 0
            new_discoveries = 0

            for item in items:
                s_name = item.series_name or ""
                s_lower = s_name.lower()

                if s_lower in blacklisted:
                    continue

                if s_lower in calibre_series:
                    with self.db.get_connection() as conn:
                        cur = conn.cursor()
                        cur.execute("""
                            UPDATE series_tracking 
                            SET website_url = ? 
                            WHERE lower(name) = lower(?) AND (website_url IS NULL OR website_url = '' OR website_url LIKE '%?s=%');
                        """, (item.series_link, s_name))
                        conn.commit()
                    linked_calibre += 1
                else:
                    guid = hashlib.sha256(item.link.encode("utf-8")).hexdigest()[:16]
                    rel_dict = {
                        "guid": guid,
                        "title": item.title,
                        "series_name": s_name,
                        "volume": 1.0,
                        "link": item.link,
                        "series_link": item.series_link,
                        "source_feed_name": "J-Novels Master Index",
                        "status": "NEW",
                        # The master index is a light-novel catalogue, so these
                        # series rows are novels by provenance.
                        "media_type": "novel"
                    }
                    is_new, _ = self._upsert(rel_dict)
                    if is_new:
                        new_discoveries += 1

            msg = f"Master Index Ingestion: Processed {len(items)} series. Linked {linked_calibre} Calibre hubs, added {new_discoveries} new discoveries."
            logger.info(msg)
            self.db.set_setting("backfill_progress", f"Master Index Ingest: {new_discoveries} new discoveries, {linked_calibre} Calibre hubs linked.")
            self.db.log_activity("MASTER_INDEX_INGEST", "Imported J-Novels Complete Series Index", msg)
            return {"status": "success", "total_indexed": len(items), "linked_calibre": linked_calibre, "new_discoveries": new_discoveries}
        except Exception as e:
            logger.error(f"Master index ingest error: {e}", exc_info=True)
            self.db.log_activity("ERROR", "Master Index Ingestion Failed", str(e))
            return {"status": "error", "message": str(e)}

    def stop_backfill(self):
        """Signals the running backfiller to gracefully stop."""
        self.stop_backfill_requested = True
        self.db.set_setting("backfill_status", "stopping")
        self.db.set_setting("backfill_progress", "Stop requested, finishing current item...")

    @staticmethod
    def _setting_flag(raw: Optional[str], default: bool = True) -> bool:
        """Normalises a boolean-as-text app_setting ("false"/"0"/"no"/"off" are off)."""
        text = str(raw or "").strip().lower()
        if not text:
            return default
        return text not in ("false", "0", "no", "off")

    def reset_nyaa_backfill_cursor(self) -> Dict[str, Any]:
        """Resets the resumable Nyaa historical-backfill cursor to page 1."""
        self.db.set_setting("backfill_nyaa_next_page", "1")
        self.db.set_setting("backfill_nyaa_done", "false")
        message = "Nyaa historical backfill cursor reset: the next backfill run re-scans from page 1."
        self.db.log_activity("NYAA_BACKFILL_RESET", "Nyaa historical backfill cursor reset", message)
        return {"status": "success", "message": message}

    def get_backfill_status(self) -> Dict[str, Any]:
        try:
            delay = float(self.db.get_setting("backfill_delay_seconds", "2.0"))
        except ValueError:
            delay = 2.0
        try:
            max_p = int(self.db.get_setting("backfill_max_pages", "30"))
        except ValueError:
            max_p = 30
        try:
            nyaa_next_page = max(1, int(self.db.get_setting("backfill_nyaa_next_page", "1")))
        except (TypeError, ValueError):
            nyaa_next_page = 1

        return {
            "running": self.is_backfilling,
            "status": self.db.get_setting("backfill_status", "idle"),
            "progress": self.db.get_setting("backfill_progress", "Idle"),
            "delay_seconds": delay,
            "max_pages": max_p,
            # Additive keys for the resumable Nyaa historical-pagination phase.
            "include_nyaa": self._setting_flag(self.db.get_setting("backfill_include_nyaa", "true"), default=True),
            "nyaa_next_page": nyaa_next_page,
            "nyaa_done": self._setting_flag(self.db.get_setting("backfill_nyaa_done", "false"), default=False),
        }

    def _ingest_backfill_releases(self, releases: List[Any], matcher: CalibreMatcher,
                                  monitored: Dict[str, Any], blacklisted: set,
                                  infer_nyaa_bundles: bool = False) -> Dict[str, int]:
        """
        Persists one historical-backfill page with the shared ingestion policy.

        Called by BOTH the J-Novels and the Nyaa deep-pagination phases, so the
        monitored/unmonitored decision logic exists exactly once:
        * empty series names, blacklisted series and manga are rejected;
        * monitored series -> matcher.check_release(auto_request_monitored=True);
        * unmonitored -> IN_CALIBRE upsert, or a NEW discovery row for volume
          None / 1.0; every other unowned volume is rejected (not persisted).

        ``infer_nyaa_bundles=True`` (Nyaa deep-pagination phase) runs the
        unmarked-bundle volume inference from contract v3 §4 before matching,
        using one bounded publication-lookup budget per page call, and persists
        the inferred complete-series volume set/missing volumes.

        Returns counters ``{"upserted", "rejected", "new"}`` so the caller can
        report how much the phase actually changed.
        """
        from app.media_type import MANGA, AUDIOBOOK, UNKNOWN

        counts = {"upserted": 0, "rejected": 0, "new": 0}
        publication_budget: Optional[Dict[str, int]] = None
        if infer_nyaa_bundles:
            try:
                budget_setting = max(0, int(self.db.get_setting(
                    SERIES_ANILIST_MATCH_BUDGET_KEY,
                    str(SERIES_ANILIST_MATCH_BUDGET_DEFAULT),
                )))
            except (TypeError, ValueError):
                budget_setting = SERIES_ANILIST_MATCH_BUDGET_DEFAULT
            publication_budget = {"remaining": budget_setting}

        for r in releases:
            s_name = r.series_name or ""
            if not s_name:
                counts["rejected"] += 1
                continue
            if s_name.lower() in blacklisted:
                counts["rejected"] += 1
                continue

            media_type = self.classify_media(r)
            if media_type == MANGA:
                counts["rejected"] += 1
                continue

            rel_dict = r.to_dict()
            inferred_bundle = False
            if (
                infer_nyaa_bundles
                and media_type != AUDIOBOOK
                and s_name
            ):
                inferred_bundle = self.infer_nyaa_bundle_volumes(
                    rel_dict, s_name, True, publication_budget
                )

            def _volume_for(m_res: Dict[str, Any]) -> Any:
                if inferred_bundle and rel_dict.get("volume") is not None:
                    return float(rel_dict["volume"])
                return r.volume if r.volume is not None else (
                    m_res.get("effective_volume") or r.volume or 1.0
                )

            if s_name.lower() in monitored:
                m_res = matcher.check_release(rel_dict, auto_request_monitored=True)
                if inferred_bundle:
                    rel_dict["missing_volumes"] = m_res.get("missing_volumes") or []
                eff_vol = m_res.get("effective_volume") or r.volume or 1.0
                rel_dict.update({
                    "status": m_res["status"],
                    "volume": _volume_for(m_res),
                    "calibre_matched_book_id": m_res.get("matched_book_id"),
                    "media_type": media_type or UNKNOWN
                })
                is_new, _ = self._upsert(rel_dict)
                counts["upserted"] += 1
                if is_new:
                    counts["new"] += 1
                if r.cover_url:
                    self.cover_cache.cache_sync(r.cover_url)
            else:
                # Evaluate unmonitored releases against Calibre
                m_res = matcher.check_release(rel_dict, auto_request_monitored=False)
                if m_res["status"] == "IN_CALIBRE":
                    if inferred_bundle:
                        rel_dict["missing_volumes"] = m_res.get("missing_volumes") or []
                    eff_vol = m_res.get("effective_volume") or r.volume or 1.0
                    rel_dict.update({
                        "status": "IN_CALIBRE",
                        "volume": _volume_for(m_res),
                        "calibre_matched_book_id": m_res.get("matched_book_id"),
                        "media_type": media_type or UNKNOWN
                    })
                    is_new, _ = self._upsert(rel_dict)
                    counts["upserted"] += 1
                    if is_new:
                        counts["new"] += 1
                elif inferred_bundle or r.volume is None or r.volume == 1.0:
                    # Vol 1 / standalone / inferred complete bundle -> Discovery.
                    if inferred_bundle:
                        rel_dict["missing_volumes"] = m_res.get("missing_volumes") or []
                    rel_dict.update({
                        "status": "NEW",
                        "calibre_matched_book_id": None,
                        "media_type": media_type or UNKNOWN
                    })
                    if inferred_bundle:
                        rel_dict["volume"] = float(rel_dict.get("volume") or 1.0)
                    is_new, _ = self._upsert(rel_dict)
                    counts["upserted"] += 1
                    if is_new:
                        counts["new"] += 1
                    if r.cover_url:
                        self.cover_cache.cache_sync(r.cover_url)
                else:
                    counts["rejected"] += 1
        return counts

    @background_tasks.tracked_task("backfill", "Polite backfill")
    async def run_polite_backfill(self, max_pages: Optional[int] = None, enrich_synopses: bool = True,
                                  delay_seconds: Optional[float] = None,
                                  include_nyaa: Optional[bool] = None):
        """
        Runs the polite historical backfill:
        1. Ingests Master Index.
        2. Slowly enriches discovery synopses & covers with polite rate-limiting.
        3. Paginates backwards through J-Novels for older volume releases.
        4. Deep-paginates Nyaa category 3_1 with a resumable page cursor
           (``backfill_include_nyaa``; the ``include_nyaa`` kwarg overrides it
           for this run). ``include_nyaa=None`` reads the setting.
        """
        import asyncio
        if self.is_backfilling:
            return {"status": "in_progress", "message": "Backfill already running"}

        if max_pages is None:
            try:
                max_pages = int(self.db.get_setting("backfill_max_pages", "30"))
            except ValueError:
                max_pages = 30

        if delay_seconds is None:
            try:
                delay_seconds = float(self.db.get_setting("backfill_delay_seconds", "2.0"))
            except ValueError:
                delay_seconds = 2.0

        if include_nyaa is None:
            include_nyaa = self._setting_flag(
                self.db.get_setting("backfill_include_nyaa", "true"), default=True
            )

        # Per-phase ingestion counters, reported in the final BACKFILL_FINISHED
        # progress line and activity log.
        jnovels_upserted = 0
        jnovels_rejected = 0
        nyaa_upserted = 0
        nyaa_rejected = 0
        nyaa_summary = ""

        self.is_backfilling = True
        self.stop_backfill_requested = False
        self.db.set_setting("backfill_status", "running")
        self.db.set_setting("backfill_progress", "Ingesting J-Novels Complete Series Index...")

        try:
            # 1. Master Index
            self._task_report(message="Ingesting J-Novels Complete Series Index…", progress_pct=5)
            self.db.set_setting("backfill_progress", "Ingesting J-Novels Complete Series Index...")
            index_res = await asyncio.to_thread(self.ingest_master_index)
            if self.stop_backfill_requested:
                return

            if index_res.get("status") == "error":
                logger.warning(f"Master index ingest reported error: {index_res.get('message')}")

            # 2. Enrich Synopses for Discovery items
            if enrich_synopses:
                from app.providers.jnovels import JNovelsProvider
                discovery_items = self.db.get_discovery_releases(limit=300)
                needs_enrichment = [d for d in discovery_items if not d.get("description") or len(d.get("description", "")) < 30]
                total_to_enrich = len(needs_enrichment)

                self._task_report(
                    total=total_to_enrich, message=f"Enriching {total_to_enrich} discovery synopsis(es)…",
                    progress_pct=15,
                )
                for idx, d in enumerate(needs_enrichment, 1):
                    if self.stop_backfill_requested:
                        break
                    self._task_report(
                        done=idx - 1, total=total_to_enrich,
                        message=f"Enriching synopsis {idx}/{total_to_enrich}: {d.get('series_name') or d.get('title') or ''}",
                    )

                    target_url = d.get("series_link") or d.get("link") or ""
                    self.db.set_setting("backfill_progress", f"Enriching synopsis {idx}/{total_to_enrich}: {d.get('series_name')}")
                    details = await asyncio.to_thread(
                        JNovelsProvider.extract_synopsis_and_original_link, 
                        target_url, 
                        series_name=d.get('series_name')
                    )
                    from app.providers.metadata import NovelMetadataProvider
                    # The parsed/tracked series name is the STABLE matching key.
                    # The provider (AniList/Kitsu) English title is display data
                    # only: it must never be written to releases.series_name, or
                    # the release detaches from its Calibre series (M1 bug).
                    stable_series_name = (d.get("series_name") or "").strip()
                    display_title = NovelMetadataProvider.clean_novel_title(
                        details.get("english_title") or ""
                    )
                    new_title = None
                    if d.get("title") and display_title:
                        if any(v in d["title"].lower() for v in ("volume", "vol.", "vol ")):
                            from app.parser import extract_series_and_volume
                            parsed = extract_series_and_volume(d["title"])
                            vol = parsed.get("volume")
                            new_title = f"{display_title} Vol. {vol}" if vol is not None else display_title
                        else:
                            new_title = display_title

                    has_enrichment = bool(
                        details.get("synopsis")
                        or details.get("series_link")
                        or details.get("cover_url")
                        or (display_title and display_title != stable_series_name)
                    )
                    if has_enrichment:
                        self.db.update_release_details(
                            d["id"],
                            description=details.get("synopsis"),
                            series_link=details.get("series_link"),
                            cover_url=details.get("cover_url"),
                            title=new_title,
                        )
                        if details.get("cover_url"):
                            self.cover_cache.cache_sync(details["cover_url"])

                    pinned_release = self._release_identity_pinned(d)
                    if (
                        not pinned_release
                        and display_title and stable_series_name
                        and display_title != stable_series_name
                    ):
                        # Persist the AniList title as display metadata of the
                        # tracked series when the release already resolves to one.
                        meta = {
                            "title": display_title,
                            "english_title": display_title,
                            "source": details.get("metadata_source") or "AniList",
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                        }
                        target_series_id = None
                        try:
                            identity = self.resolve_release_series_identity(
                                {"series_name": stable_series_name}
                            )
                        except Exception as exc:
                            logger.debug(
                                f"Series identity resolve during enrichment failed for "
                                f"'{stable_series_name}': {exc}"
                            )
                            identity = None
                        if identity and identity.get("matched"):
                            target_series_id = identity.get("series_id")
                            if target_series_id is None and identity.get("calibre_series_id") is not None:
                                try:
                                    row = self.db.get_series_by_calibre_id(
                                        identity["calibre_series_id"]
                                    )
                                    if row:
                                        target_series_id = row.get("id")
                                except Exception:
                                    target_series_id = None
                        if target_series_id is not None:
                            try:
                                self.db.update_series_identity(
                                    int(target_series_id),
                                    display_title=display_title,
                                    anilist_meta_json=json.dumps(meta),
                                )
                            except Exception as exc:
                                logger.debug(
                                    f"Display-title persist failed for series "
                                    f"#{target_series_id}: {exc}"
                                )
                        else:
                            # Unknown series: keep the release's stable key and
                            # cache the title so a later Calibre sync/manual match
                            # can use it as a matching candidate.
                            try:
                                from app.parser import normalize_text
                                cache_key = normalize_text(stable_series_name)
                                if cache_key:
                                    self.db.cache_novel_metadata(cache_key, {
                                        **meta,
                                        "synopsis": details.get("synopsis") or "",
                                        "cover_url": details.get("cover_url") or "",
                                    })
                            except Exception as exc:
                                logger.debug(
                                    f"Provider-title cache failed for '{stable_series_name}': {exc}"
                                )

                    await asyncio.sleep(delay_seconds)

            # 3. Deep Pagination for historical volumes (stops automatically when end of feed is reached)
            if not self.stop_backfill_requested and max_pages != -1:
                from app.providers.jnovels import JNovelsProvider
                provider = JNovelsProvider(config={"max_pages": 1})
                calibre_dir = self.db.get_setting("calibre_path", self.calibre_path)
                scanner = CalibreScanner(calibre_dir)
                calibre_series = scanner.get_all_series() if scanner.is_available() else []
                calibre_books = scanner.get_all_books() if scanner.is_available() else []
                matcher = CalibreMatcher(calibre_series, calibre_books)
                monitored = {s["name"].lower(): s for s in self.db.get_all_series(monitored_only=True)}
                blacklisted_series = {b["name"].lower() for b in self.db.get_blacklisted_series()}

                page_num = 1
                while not self.stop_backfill_requested:
                    # If max_pages is positive and exceeded, finish
                    if max_pages > 0 and page_num > max_pages:
                        break

                    target_text = f"page {page_num}/{max_pages}" if max_pages > 0 else f"page {page_num} (scanning until end)"
                    self.db.set_setting("backfill_progress", f"Checking historical feed {target_text}...")
                    paged_url = provider._build_paged_url("https://jnovels.com/?s=epub", page_num)
                    page_provider = JNovelsProvider(url=paged_url, config={"max_pages": 1})
                    releases = await asyncio.to_thread(page_provider.fetch_releases)
                    releases = self.filter_chapter_releases(releases, self.chapter_context())
                    if not releases:
                        logger.info(f"Historical backfill reached end of available feed pages at page {page_num}.")
                        break

                    counts = self._ingest_backfill_releases(releases, matcher, monitored, blacklisted_series)
                    jnovels_upserted += counts["upserted"]
                    jnovels_rejected += counts["rejected"]

                    page_num += 1
                    await asyncio.sleep(delay_seconds)

            # 4. Nyaa Deep Pagination (resumable deep crawl of category 3_1).
            # The cursor lives in app_settings so a stopped/capped run resumes on
            # the next page instead of restarting at page 1. Deliberately gated
            # on the same max_pages semantics as the J-Novels phase: -1 skips
            # every deep-pagination phase, 0 is unlimited, >0 is a page budget
            # for THIS run.
            if not self.stop_backfill_requested and max_pages != -1 and include_nyaa:
                from app.providers.nyaa import NyaaNovelProvider
                from app.parser import is_pdf_release
                from app.media_type import MANGA

                try:
                    nyaa_next_page = max(1, int(self.db.get_setting("backfill_nyaa_next_page", "1")))
                except (TypeError, ValueError):
                    nyaa_next_page = 1
                nyaa_done = self._setting_flag(
                    self.db.get_setting("backfill_nyaa_done", "false"), default=False
                )

                if nyaa_done:
                    logger.info("Nyaa historical backfill already complete; skipping (cursor reset re-enables it).")
                    self.db.set_setting(
                        "backfill_progress",
                        "Nyaa historical pages already complete; reset the cursor in Settings to re-scan."
                    )
                else:
                    calibre_dir = self.db.get_setting("calibre_path", self.calibre_path)
                    scanner = CalibreScanner(calibre_dir)
                    calibre_series = scanner.get_all_series() if scanner.is_available() else []
                    calibre_books = scanner.get_all_books() if scanner.is_available() else []
                    matcher = CalibreMatcher(calibre_series, calibre_books)
                    monitored = {s["name"].lower(): s for s in self.db.get_all_series(monitored_only=True)}
                    blacklisted_series = {b["name"].lower() for b in self.db.get_blacklisted_series()}

                    provider = NyaaNovelProvider(config={"max_pages": 1})
                    started_at_page = nyaa_next_page
                    page_num = nyaa_next_page
                    pages_this_run = 0
                    releases_this_run = 0
                    reached_end = False

                    while not self.stop_backfill_requested:
                        # Positive max_pages caps THIS run's Nyaa pages; the
                        # cursor is already pointing at the next unscanned page.
                        if max_pages > 0 and pages_this_run >= max_pages:
                            break

                        self.db.set_setting("backfill_progress", f"Checking Nyaa historical page {page_num}...")
                        try:
                            raw_releases = await asyncio.to_thread(
                                provider.crawl_pages,
                                max_pages=1,
                                start_page=page_num,
                                stop_check=lambda: self.stop_backfill_requested
                            )
                        except Exception as exc:
                            logger.warning(f"Nyaa historical page {page_num} raised: {exc}")
                            raw_releases = []
                            crawl_meta = {"stop_reason": "error"}
                        else:
                            crawl_meta = getattr(provider, "last_crawl_meta", None) or {}
                        stop_reason = crawl_meta.get("stop_reason")

                        # Emptiness decides end-of-feed BEFORE chapter/PDF/manga
                        # filtering: a page whose rows were all chapters is not
                        # the end of the feed.
                        if not raw_releases:
                            if stop_reason == "error":
                                logger.warning(
                                    f"Nyaa historical backfill aborted at page {page_num}: transient "
                                    "fetch error; cursor kept for the next run."
                                )
                                self.db.set_setting(
                                    "backfill_progress",
                                    f"Nyaa historical page {page_num} failed (transient); will resume there next run."
                                )
                                break
                            if self.stop_backfill_requested or stop_reason == "stop_requested":
                                break
                            reached_end = True
                            self.db.set_setting("backfill_nyaa_done", "true")
                            self.db.set_setting("backfill_nyaa_next_page", "1")
                            self.db.set_setting("backfill_last_page", str(page_num))
                            logger.info(f"Nyaa historical backfill reached the end of the feed at page {page_num}.")
                            break

                        releases = self.filter_chapter_releases(raw_releases, self.chapter_context())
                        # Nyaa backfill is EPUB/torrent only and manga must never
                        # enter the database (same policy as backfill_series).
                        filtered_releases = []
                        policy_rejected = 0
                        for rel in releases:
                            if is_pdf_release(rel.title, rel.link, rel.guid):
                                policy_rejected += 1
                                continue
                            if self.classify_media(rel) == MANGA:
                                policy_rejected += 1
                                continue
                            filtered_releases.append(rel)
                        releases = filtered_releases

                        counts = self._ingest_backfill_releases(
                            releases, matcher, monitored, blacklisted_series,
                            infer_nyaa_bundles=True,
                        )
                        pages_this_run += 1
                        releases_this_run += len(releases)
                        nyaa_upserted += counts["upserted"]
                        nyaa_rejected += policy_rejected + counts["rejected"]

                        page_num += 1
                        self.db.set_setting("backfill_nyaa_next_page", str(page_num))
                        self.db.set_setting("backfill_last_page", str(page_num))
                        await asyncio.sleep(delay_seconds)

                    next_page = 1 if reached_end else page_num
                    summary = (
                        f"Nyaa historical backfill: {pages_this_run} page(s), {releases_this_run} release(s) "
                        f"({nyaa_upserted} upserted, {nyaa_rejected} rejected)"
                    )
                    if reached_end:
                        summary += "; reached the end of the Nyaa feed."
                    elif self.stop_backfill_requested:
                        summary += f"; stopped at page {page_num}, cursor kept for resume."
                    elif max_pages > 0 and pages_this_run >= max_pages:
                        summary += f"; page budget reached, next run resumes at page {next_page}."
                    else:
                        summary += f"; next run resumes at page {next_page}."
                    self.db.set_setting("backfill_progress", summary)
                    nyaa_summary = summary
                    logger.info(summary)
                    self.db.log_activity(
                        "NYAA_BACKFILL_FINISHED",
                        "Nyaa historical backfill phase finished",
                        json.dumps({
                            "pages": pages_this_run,
                            "releases": releases_this_run,
                            "reached_end": reached_end,
                            "started_at_page": started_at_page,
                            "next_page": next_page,
                            "upserted": nyaa_upserted,
                            "rejected": nyaa_rejected,
                        })
                    )

            total_upserted = jnovels_upserted + nyaa_upserted
            total_rejected = jnovels_rejected + nyaa_rejected
            self.db.set_setting("backfill_status", "completed")
            final_progress = (
                f"Historical backfill finished successfully! ({total_upserted} upserted, {total_rejected} rejected)"
            )
            if nyaa_summary:
                final_progress += " " + nyaa_summary
            self.db.set_setting("backfill_progress", final_progress)
            self.db.log_activity(
                "BACKFILL_FINISHED",
                "Polite backfill completed",
                f"upserted={total_upserted}, rejected={total_rejected}"
            )

        except Exception as e:
            logger.error(f"Backfill error: {e}", exc_info=True)
            self.db.set_setting("backfill_status", "error")
            self.db.set_setting("backfill_progress", f"Error: {e}")
            self.db.log_activity("ERROR", "Backfill error", str(e))
        finally:
            self.is_backfilling = False

    def run_series_backfill_async(self, series_name: str, max_pages: int = 3) -> Dict[str, Any]:
        """
        Launches targeted series backfill in a background daemon thread:
        Searches active providers (including NyaaNovelProvider) for older/missing volumes,
        matches against Calibre library, and upserts results.
        """
        if self.is_backfilling:
            return {"status": "in_progress", "message": "Another backfill is already running"}

        self.is_backfilling = True
        self.stop_backfill_requested = False

        def _worker():
            self.db.set_setting("backfill_status", "running")
            self.db.set_setting("backfill_progress", f"Backfilling '{series_name}' across providers (up to {max_pages} pages)...")
            self.db.set_setting("backfill_last_page", "1")
            try:
                res = self.backfill_series(series_name, max_pages=max_pages)
                self.db.set_setting("backfill_status", "idle")
                self.db.set_setting("backfill_progress", res.get("message", f"Backfill for '{series_name}' complete."))
                self.db.set_setting("backfill_last_page", str(max_pages))
            except Exception as e:
                logger.error(f"Error in async series backfill for '{series_name}': {e}", exc_info=True)
                self.db.set_setting("backfill_status", "error")
                self.db.set_setting("backfill_progress", f"Backfill failed: {e}")
            finally:
                self.is_backfilling = False

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return {"status": "started", "message": f"Historical backfill for '{series_name}' started in background."}

    @background_tasks.tracked_task(
        "series_backfill", lambda self, series_name, max_pages=3: f"Backfill: {series_name}",
        scope=lambda self, series_name, max_pages=3: str(series_name),
    )
    def backfill_series(self, series_name: str, max_pages: int = 3) -> Dict[str, Any]:
        """
        Synchronous worker executing targeted series search across providers.
        """
        from app.calibre.scanner import CalibreScanner
        from app.calibre.matcher import CalibreMatcher
        from app.parser import is_pdf_release, extract_release_group

        calibre_dir = self.db.get_setting("calibre_path", self.calibre_path)
        scanner = CalibreScanner(calibre_dir)
        if scanner.is_available():
            calibre_series = scanner.get_all_series()
            calibre_books = scanner.get_all_books()
        else:
            calibre_series = []
            calibre_books = []
            for s in self.db.get_all_series():
                ser_id = s.get("calibre_series_id") or s.get("id")
                ser_name = s.get("name") or s.get("series_name", "")
                latest_idx = float(s.get("latest_index", 0.0) or 0.0)
                calibre_series.append({
                    "series_id": ser_id,
                    "series_name": ser_name,
                    "book_count": s.get("book_count", 0),
                    "latest_index": latest_idx
                })
                if latest_idx > 0.0:
                    curr_v = 1.0
                    while curr_v <= latest_idx:
                        calibre_books.append({
                            "book_id": int(ser_id * 1000 + curr_v),
                            "title": f"{ser_name} Vol {curr_v:.0f}",
                            "series_id": ser_id,
                            "series_name": ser_name,
                            "series_index": curr_v
                        })
                        curr_v += 1.0
        matcher = CalibreMatcher(calibre_series, calibre_books)

        total_found = 0
        total_requested = 0
        # Webhook toggle read once for the whole backfill run (contract §6.5).
        notify_new_requests_enabled = self._webhook_setting_enabled(
            self.db, WEBHOOK_NEW_REQUEST_KEY
        )

        # Search Nyaa provider
        nyaa_releases = []
        try:
            from app.providers.nyaa import NyaaNovelProvider
            nyaa = NyaaNovelProvider(config={"delay_seconds": 1.0})
            nyaa_releases = nyaa.search_series(
                series_name,
                max_pages=max_pages,
                stop_check=lambda: self.stop_backfill_requested
            )
        except Exception as e:
            logger.debug(f"Nyaa provider search skipped or unavailable: {e}")

        all_incoming = self.filter_chapter_releases(nyaa_releases, self.chapter_context())
        for rel in all_incoming:
            if self.stop_backfill_requested:
                break
            if is_pdf_release(rel.title, rel.link, rel.guid):
                continue

            from app.media_type import MANGA, UNKNOWN
            media_type = self.classify_media(rel)
            if media_type == MANGA:
                continue

            total_found += 1
            m_eval = matcher.check_release(rel.to_dict(), auto_request_monitored=True)
            st = m_eval["status"]
            matched_id = m_eval.get("matched_book_id")

            rel_dict = rel.to_dict()
            rg = extract_release_group(rel.title)
            rel_dict.update({
                "status": st,
                "calibre_matched_book_id": matched_id,
                "release_group": rg or "",
                "media_type": media_type or UNKNOWN
            })

            is_new, rel_id = self._upsert(rel_dict)
            if st == "REQUESTED":
                total_requested += 1
                if is_new and rel_id > 0 and notify_new_requests_enabled:
                    try:
                        rel_data = self.db.get_release_by_id(rel_id)
                        if rel_data:
                            self.get_notifier().notify_new_request(rel_data)
                    except Exception:
                        pass

        self.db.set_setting("backfill_last_page", str(max_pages))
        summary = f"Backfilled '{series_name}': found {total_found} releases ({total_requested} wanted/missing)."
        self.db.log_activity("SERIES_BACKFILLED", summary)
        return {
            "status": "success",
            "message": summary,
            "series_name": series_name,
            "releases_found": total_found,
            "requested": total_requested
        }

