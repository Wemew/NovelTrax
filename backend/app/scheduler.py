import os
import json
import logging
from typing import Optional, List, Dict, Any
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.database import Database
from app.calibre.scanner import CalibreScanner
from app.calibre.matcher import CalibreMatcher
from app.providers.registry import create_provider
from app.notifier.webhook import NotificationService
from app.covers import CoverCache

logger = logging.getLogger("noveltrax.scheduler")

class TaskManager:
    def __init__(self, db: Database, calibre_path: str, data_dir: Optional[str] = None):
        self.db = db
        self.calibre_path = calibre_path
        self.cover_cache = CoverCache(data_dir or os.getenv("DATA_DIR", "/app/data"))
        self.scheduler = AsyncIOScheduler()
        self.is_syncing_calibre = False
        self.is_checking_feeds = False
        self.is_backfilling = False
        self.stop_backfill_requested = False

    def get_notifier(self) -> NotificationService:
        discord_url = self.db.get_setting("discord_webhook_url", "")
        generic_url = self.db.get_setting("apprise_url", "")
        return NotificationService(discord_webhook=discord_url, generic_webhook=generic_url)

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

    def stop(self):
        self.scheduler.shutdown(wait=False)

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
                    releases = provider.fetch_releases()
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
                            # If not matched to any monitored series and auto_request is strict, keep as NEW
                            if status == "REQUESTED" and not is_monitored and not auto_request:
                                status = "NEW"

                        rel_dict = r.to_dict()
                        rel_dict.update({
                            "source_feed_id": feed_id,
                            "source_feed_name": feed_name,
                            "status": status,
                            "calibre_matched_book_id": match_result.get("matched_book_id")
                        })

                        is_new, rel_id = self.db.upsert_release(rel_dict)

                        # Pre-cache cover image locally in background
                        if r.cover_url:
                            self.cover_cache.cache_sync(r.cover_url)

                        if is_new and status == "REQUESTED":
                            total_requested += 1
                            new_requests_for_feed += 1
                            self.db.log_activity(
                                "NEW_RELEASE",
                                f"New release requested: {r.title}",
                                f"Series: {series_name} Vol {r.volume} from '{feed_name}'"
                            )
                            notifier.notify_new_request(rel_dict)

                    self.db.update_feed_status(feed_id, status="OK")
                    logger.info(f"Checked feed '{feed_name}': {len(releases)} items parsed, {new_requests_for_feed} new requests.")

                except Exception as feed_err:
                    err_msg = str(feed_err)
                    logger.error(f"Error checking feed '{feed_name}': {err_msg}")
                    self.db.update_feed_status(feed_id, status="ERROR", error=err_msg)
                    self.db.log_activity("ERROR", f"Feed check error: {feed_name}", err_msg)

            return {
                "status": "success",
                "total_found": total_found,
                "total_requested": total_requested
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
                        "status": "NEW"
                    }
                    is_new, _ = self.db.upsert_release(rel_dict)
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

    def get_backfill_status(self) -> Dict[str, Any]:
        return {
            "running": self.is_backfilling,
            "status": self.db.get_setting("backfill_status", "idle"),
            "progress": self.db.get_setting("backfill_progress", "Idle")
        }

    async def run_polite_backfill(self, max_pages: Optional[int] = None, enrich_synopses: bool = True, delay_seconds: Optional[float] = None):
        """
        Runs the polite historical backfill:
        1. Ingests Master Index.
        2. Slowly enriches discovery synopses & covers with polite rate-limiting.
        3. Paginates backwards through J-Novels for older volume releases.
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

                    target_url = d.get("series_link") or d.get("link")
                    if target_url:
                        self.db.set_setting("backfill_progress", f"Enriching synopsis {idx}/{total_to_enrich}: {d.get('series_name')}")
                        details = await asyncio.to_thread(JNovelsProvider.extract_synopsis_and_original_link, target_url)
                        if details.get("synopsis") or details.get("series_link") or details.get("cover_url"):
                            self.db.update_release_details(
                                d["id"],
                                description=details.get("synopsis"),
                                series_link=details.get("series_link"),
                                cover_url=details.get("cover_url")
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
                    if not releases:
                        logger.info(f"Historical backfill reached end of available feed pages at page {page_num}.")
                        break

                    for r in releases:
                        s_name = r.series_name or ""
                        if not s_name:
                            continue
                        if s_name.lower() in blacklisted_series:
                            continue

                        if s_name.lower() in monitored:
                            m_res = matcher.check_release(r.to_dict(), auto_request_monitored=True)
                            rel_dict = r.to_dict()
                            rel_dict.update({
                                "status": m_res["status"],
                                "calibre_matched_book_id": m_res.get("matched_book_id")
                            })
                            is_new, _ = self.db.upsert_release(rel_dict)
                            if r.cover_url:
                                self.cover_cache.cache_sync(r.cover_url)
                        elif r.volume is None or r.volume == 1.0:
                            # Vol 1 or standalone unmonitored series -> Add to Discovery!
                            rel_dict = r.to_dict()
                            rel_dict.update({
                                "status": "NEW",
                                "calibre_matched_book_id": None
                            })
                            is_new, _ = self.db.upsert_release(rel_dict)
                            if r.cover_url:
                                self.cover_cache.cache_sync(r.cover_url)

                    page_num += 1
                    await asyncio.sleep(delay_seconds)

            self.db.set_setting("backfill_status", "completed")
            self.db.set_setting("backfill_progress", "Historical backfill finished successfully!")
            self.db.log_activity("BACKFILL_FINISHED", "Polite backfill completed")

        except Exception as e:
            logger.error(f"Backfill error: {e}", exc_info=True)
            self.db.set_setting("backfill_status", "error")
            self.db.set_setting("backfill_progress", f"Error: {e}")
            self.db.log_activity("ERROR", "Backfill error", str(e))
        finally:
            self.is_backfilling = False
