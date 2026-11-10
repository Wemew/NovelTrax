import os
import json
import logging
import threading
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

logger = logging.getLogger("noveltrax.scheduler")

#: Settings keys for the time-aware catch-up scan.  Read with hard-coded
#: defaults (nothing is seeded into app_settings) so existing databases get the
#: feature immediately; an invalid value falls back to the default.
FEED_CATCHUP_ENABLED_KEY = "feed_catchup_enabled"
FEED_CATCHUP_MAX_PAGES_KEY = "feed_catchup_max_pages"


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

    def start(self):
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
        self.scheduler.start()
        logger.info("Scheduler started successfully.")

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
        try:
            calibre_dir = self.db.get_setting("calibre_path", self.calibre_path)
            scanner = CalibreScanner(calibre_dir)

            if not scanner.is_available():
                msg = f"Calibre library metadata.db not accessible at: {scanner.db_path}"
                logger.warning(msg)
                self.db.log_activity("CALIBRE_SYNC", "Calibre sync failed", msg)
                return {"status": "error", "message": msg}

            series_list = scanner.get_all_series()
            books_list = scanner.get_all_books()

            # 1. Auto-populate series into database
            updated_series_count = self.db.sync_calibre_series(series_list)

            # 2. Check and resolve active requests
            pending_requests = self.db.get_pending_requests()
            matcher = CalibreMatcher(series_list, books_list)
            newly_acquired = matcher.resolve_pending_requests(pending_requests)

            notifier = self.get_notifier()
            for acq in newly_acquired:
                # Mark as acquired in Calibre (removes from active request list)
                self.db.update_release_status(acq["release_id"], status="IN_CALIBRE", matched_book_id=acq["matched_book_id"])
                self.db.log_activity(
                    "REQUEST_RESOLVED",
                    f"Book acquired into Calibre: '{acq['title']}'",
                    f"Matched Calibre Book #{acq['matched_book_id']} ({acq['reason']})"
                )
                notifier.notify_acquired(acq)

            log_msg = f"Synced {updated_series_count} series, {len(books_list)} books. Resolved {len(newly_acquired)} requests."
            logger.info(log_msg)
            self.db.log_activity("CALIBRE_SYNC", "Calibre library sync completed", log_msg)

            return {
                "status": "success",
                "series_count": updated_series_count,
                "books_count": len(books_list),
                "resolved_requests": len(newly_acquired),
                "resolved_items": newly_acquired
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

    def check_feeds(self) -> Dict[str, Any]:
        """
        Polls all enabled feeds, checks releases against Calibre,
        and requests new volumes for monitored series.
        """
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

        try:
            calibre_dir = self.db.get_setting("calibre_path", self.calibre_path)
            scanner = CalibreScanner(calibre_dir)
            calibre_series = scanner.get_all_series() if scanner.is_available() else []
            calibre_books = scanner.get_all_books() if scanner.is_available() else []
            matcher = CalibreMatcher(calibre_series, calibre_books)

            # Get monitored series names and blacklisted series for filtering
            monitored_series = {s["name"].lower(): s for s in self.db.get_all_series(monitored_only=True)}
            blacklisted_series = {b["name"].lower() for b in self.db.get_blacklisted_series()}
            notifier = self.get_notifier()

            feeds = self.db.get_all_feeds()
            for feed in feeds:
                if not feed["enabled"]:
                    continue

                feed_id = feed["id"]
                feed_name = feed["name"]
                feed_type = feed["feed_type"]
                url = feed["url"]
                auto_request = bool(feed["auto_request"])
                config = json.loads(feed["config_json"] or "{}")

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

                        # If series is blacklisted, immediately mark as BLACKLISTED
                        if series_name and series_name.lower() in blacklisted_series:
                            status = "BLACKLISTED"
                            match_result = {"status": "BLACKLISTED", "matched_book_id": None}
                        else:
                            # Evaluate against Calibre
                            match_result = matcher.check_release(
                                r.to_dict(),
                                auto_request_monitored=auto_request
                            )

                            # Determine if series is monitored
                            is_monitored = False
                            if series_name.lower() in monitored_series:
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

                        # Light-novel tracking: manga is dropped, audiobooks are
                        # stored but kept out of the Releases/Discovery/wishlist views.
                        from app.media_type import MANGA, AUDIOBOOK, UNKNOWN
                        media_type = self.classify_media(r)
                        if media_type == MANGA:
                            skipped_manga += 1
                            continue
                        if media_type == AUDIOBOOK:
                            skipped_audiobooks += 1

                        rel_dict = r.to_dict()
                        rel_dict.update({
                            "source_feed_id": feed_id,
                            "source_feed_name": feed_name,
                            "status": status,
                            "volume": final_volume,
                            "calibre_matched_book_id": match_result.get("matched_book_id"),
                            "media_type": media_type if media_type else UNKNOWN
                        })

                        is_new, rel_id = self._upsert(rel_dict)

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
                                f"Series: {series_name} Vol {final_volume} from '{feed_name}'"
                            )
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
                                  monitored: Dict[str, Any], blacklisted: set) -> Dict[str, int]:
        """
        Persists one historical-backfill page with the shared ingestion policy.

        Called by BOTH the J-Novels and the Nyaa deep-pagination phases, so the
        monitored/unmonitored decision logic exists exactly once:
        * empty series names, blacklisted series and manga are rejected;
        * monitored series -> matcher.check_release(auto_request_monitored=True);
        * unmonitored -> IN_CALIBRE upsert, or a NEW discovery row for volume
          None / 1.0; every other unowned volume is rejected (not persisted).

        Returns counters ``{"upserted", "rejected", "new"}`` so the caller can
        report how much the phase actually changed.
        """
        from app.media_type import MANGA, UNKNOWN

        counts = {"upserted": 0, "rejected": 0, "new": 0}
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

            if s_name.lower() in monitored:
                m_res = matcher.check_release(r.to_dict(), auto_request_monitored=True)
                eff_vol = m_res.get("effective_volume") or r.volume or 1.0
                rel_dict = r.to_dict()
                rel_dict.update({
                    "status": m_res["status"],
                    "volume": r.volume if r.volume is not None else eff_vol,
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
                m_res = matcher.check_release(r.to_dict(), auto_request_monitored=False)
                if m_res["status"] == "IN_CALIBRE":
                    eff_vol = m_res.get("effective_volume") or r.volume or 1.0
                    rel_dict = r.to_dict()
                    rel_dict.update({
                        "status": "IN_CALIBRE",
                        "volume": r.volume if r.volume is not None else eff_vol,
                        "calibre_matched_book_id": m_res.get("matched_book_id"),
                        "media_type": media_type or UNKNOWN
                    })
                    is_new, _ = self._upsert(rel_dict)
                    counts["upserted"] += 1
                    if is_new:
                        counts["new"] += 1
                elif r.volume is None or r.volume == 1.0:
                    # Vol 1 or standalone unmonitored series -> Add to Discovery!
                    rel_dict = r.to_dict()
                    rel_dict.update({
                        "status": "NEW",
                        "calibre_matched_book_id": None,
                        "media_type": media_type or UNKNOWN
                    })
                    is_new, _ = self._upsert(rel_dict)
                    counts["upserted"] += 1
                    if is_new:
                        counts["new"] += 1
                    if r.cover_url:
                        self.cover_cache.cache_sync(r.cover_url)
                else:
                    counts["rejected"] += 1
        return counts

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

                for idx, d in enumerate(needs_enrichment, 1):
                    if self.stop_backfill_requested:
                        break

                    target_url = d.get("series_link") or d.get("link") or ""
                    self.db.set_setting("backfill_progress", f"Enriching synopsis {idx}/{total_to_enrich}: {d.get('series_name')}")
                    details = await asyncio.to_thread(
                        JNovelsProvider.extract_synopsis_and_original_link, 
                        target_url, 
                        series_name=d.get('series_name')
                    )
                    from app.providers.metadata import NovelMetadataProvider
                    new_series_name = NovelMetadataProvider.clean_novel_title(details.get("english_title") or d.get("series_name"))
                    new_title = None
                    if d.get("title"):
                        if any(v in d["title"].lower() for v in ("volume", "vol.", "vol ")):
                            from app.parser import extract_series_and_volume
                            parsed = extract_series_and_volume(d["title"])
                            vol = parsed.get("volume")
                            new_title = f"{new_series_name} Vol. {vol}" if vol is not None else new_series_name
                        else:
                            new_title = new_series_name
                    else:
                        new_title = new_series_name

                    if details.get("synopsis") or details.get("series_link") or details.get("cover_url") or (new_series_name and new_series_name != d.get("series_name")):
                        self.db.update_release_details(
                            d["id"],
                            description=details.get("synopsis"),
                            series_link=details.get("series_link"),
                            cover_url=details.get("cover_url"),
                            series_name=new_series_name,
                            title=new_title
                        )
                        if details.get("cover_url"):
                            self.cover_cache.cache_sync(details["cover_url"])

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

                        counts = self._ingest_backfill_releases(releases, matcher, monitored, blacklisted_series)
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
                if is_new and rel_id > 0:
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

