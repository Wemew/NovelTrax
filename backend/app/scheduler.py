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

logger = logging.getLogger("novelpulse.scheduler")

class TaskManager:
    def __init__(self, db: Database, calibre_path: str):
        self.db = db
        self.calibre_path = calibre_path
        self.scheduler = AsyncIOScheduler()
        self.is_syncing_calibre = False
        self.is_checking_feeds = False

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

            # Get monitored series names for filtering
            monitored_series = {s["name"].lower(): s for s in self.db.get_all_series(monitored_only=True)}
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
                        # Evaluate against Calibre
                        match_result = matcher.check_release(
                            r.to_dict(),
                            auto_request_monitored=auto_request
                        )

                        # Determine if series is monitored
                        series_name = r.series_name or ""
                        is_monitored = False
                        if series_name.lower() in monitored_series:
                            is_monitored = True
                        elif match_result.get("matched_series"):
                            is_monitored = True

                        status = match_result["status"]
                        # If not matched to any monitored series and auto_request is strict, keep as NEW or IGNORED
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
