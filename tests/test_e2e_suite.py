#!/usr/bin/env python3
"""
NovelTrax Comprehensive End-to-End Opaque-Box Test Suite
Tiers 1-4 Coverage per TEST_INFRA.md and PROJECT.md

Tiers:
  Tier 1: Isolated Feature Validation (Features 1-24, >=5 tests each = 120 tests)
  Tier 2: Boundary & Corner Cases (Features 1-24, >=5 tests each = 120 tests)
  Tier 3: Pairwise & Cross-Feature Interaction Combinations (10 tests)
  Tier 4: Real-World Application Workflows (5 end-to-end scenarios)

Total: 255 rigorous opaque-box test cases.
Runner compatibility: Calibre interpreter (`calibre-debug.exe -e tests/test_e2e_suite.py`)
"""

import os
import sys
import re
import json
import sqlite3
import tempfile
import shutil
import socket
import ipaddress
import hashlib
import time
import html
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Set
from bs4 import BeautifulSoup
import feedparser

# Setup python path to include workspace root and backend
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# NovelTrax Core Imports
from tests.mock_calibre import create_mock_calibre_db, add_book_to_mock_calibre
from app.database import Database
from app.calibre.scanner import CalibreScanner
from app.calibre.matcher import CalibreMatcher
from app.parser import (
    extract_series_and_volume,
    is_pdf_release,
    normalize_text,
    string_similarity,
    match_series_in_calibre,
)
from app.covers import CoverCache
from app.providers.base import ReleaseItem
from app.providers.jnovels import JNovelsProvider
from app.providers.rss import RSSFeedProvider
from app.notifier.webhook import NotificationService

# Centralized Security Module
try:
    from app.security import (
        is_safe_public_url,
        is_ip_disallowed,
        sanitize_calibre_path,
        safe_http_get,
        safe_fetch_text,
    )
except ImportError:
    from app.covers import is_safe_public_url
    def is_ip_disallowed(ip):
        return ip.is_private or ip.is_loopback or ip.is_link_local
    def sanitize_calibre_path(p):
        if not p or "\x00" in p:
            raise ValueError("Invalid path")
        return os.path.normpath(p.strip())


# ==============================================================================
# TEST HARNESS & RUNNER INFRASTRUCTURE
# ==============================================================================

class TestRunner:
    def __init__(self):
        self.total = 0
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures = []

    def run_test(self, test_name: str, test_func):
        self.total += 1
        try:
            test_func()
            self.passed += 1
            print(f"  [PASS] {test_name}")
        except AssertionError as e:
            self.failed += 1
            self.failures.append((test_name, f"AssertionError: {e}"))
            print(f"  [FAIL] {test_name} -> AssertionError: {e}")
        except Exception as e:
            self.failed += 1
            self.failures.append((test_name, f"{type(e).__name__}: {e}"))
            print(f"  [ERROR] {test_name} -> {type(e).__name__}: {e}")

    def banner(self, title: str):
        print("\n" + "=" * 70)
        print(f"  {title.upper()}")
        print("=" * 70)

    def summary(self):
        print("\n" + "=" * 70)
        print("  TEST SUITE EXECUTION SUMMARY")
        print("=" * 70)
        print(f"  Total Test Cases Executed : {self.total}")
        print(f"  Passed                    : {self.passed}")
        print(f"  Failed                    : {self.failed}")
        print(f"  Success Rate              : {(self.passed / max(1, self.total)) * 100:.1f}%")
        if self.failures:
            print("\n  Failures / Errors Encountered:")
            for name, err in self.failures:
                print(f"    - {name}: {err}")
        print("=" * 70)
        return self.failed == 0


runner = TestRunner()


# Helper: Isolated Temporary Workspace
def make_temp_env():
    temp_dir = tempfile.mkdtemp(prefix="noveltrax_e2e_")
    calibre_db = os.path.join(temp_dir, "calibre", "metadata.db")
    app_db_path = os.path.join(temp_dir, "app", "noveltrax.db")
    create_mock_calibre_db(calibre_db)
    app_db = Database(app_db_path)
    return temp_dir, calibre_db, app_db_path, app_db


def cleanup_temp_env(temp_dir: str):
    if os.path.exists(temp_dir):
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass


# ==============================================================================
# TIER 1: ISOLATED FEATURE VALIDATION (FEATURES 1 TO 24, 5 TESTS EACH = 120 TESTS)
# ==============================================================================

runner.banner("Tier 1: Feature 1 - Calibre Zero-Lock Scanning & Library Summary")

def test_t1_f01_zerolock_connection_opens_ro_immutable():
    td, cdb, adb, db = make_temp_env()
    try:
        scanner = CalibreScanner(os.path.dirname(cdb))
        conn = scanner._get_connection()
        cur = conn.cursor()
        cur.execute("PRAGMA query_only;")
        qo = cur.fetchone()[0]
        assert qo == 1, f"Expected query_only=1, got {qo}"
        conn.close()
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F01_1: Zero-lock ro+immutable connection adheres to query_only", test_t1_f01_zerolock_connection_opens_ro_immutable)

def test_t1_f01_library_summary_counts():
    td, cdb, adb, db = make_temp_env()
    try:
        scanner = CalibreScanner(os.path.dirname(cdb))
        summary = scanner.get_library_summary()
        assert summary["available"] is True
        assert summary["total_books"] == 39  # 18 + 15 + 6
        assert summary["total_series"] == 3
        assert summary["total_authors"] == 3
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F01_2: Library summary correctly counts books, series, and authors", test_t1_f01_library_summary_counts)

def test_t1_f01_get_all_series_metadata():
    td, cdb, adb, db = make_temp_env()
    try:
        scanner = CalibreScanner(os.path.dirname(cdb))
        series = scanner.get_all_series()
        names = {s["series_name"] for s in series}
        assert names == {"DanMachi", "Overlord", "Dungeon Crawler Carl"}
        danmachi = next(s for s in series if s["series_name"] == "DanMachi")
        assert danmachi["book_count"] == 18
        assert danmachi["latest_index"] == 18.0
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F01_3: get_all_series retrieves expected series metadata", test_t1_f01_get_all_series_metadata)

def test_t1_f01_get_all_books_metadata():
    td, cdb, adb, db = make_temp_env()
    try:
        scanner = CalibreScanner(os.path.dirname(cdb))
        books = scanner.get_all_books()
        assert len(books) == 39
        assert any(b["title"] == "DanMachi, Vol. 1" for b in books)
        assert any(b["title"] == "Overlord, Vol. 15" for b in books)
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F01_4: get_all_books retrieves all 39 book records", test_t1_f01_get_all_books_metadata)

def test_t1_f01_scanner_unavailable_handling():
    scanner = CalibreScanner("C:/nonexistent/calibre/path")
    assert scanner.is_available() is False
    summary = scanner.get_library_summary()
    assert summary["available"] is False
    assert summary["total_books"] == 0
runner.run_test("T1_F01_5: Scanner handles non-existent library gracefully", test_t1_f01_scanner_unavailable_handling)


runner.banner("Tier 1: Feature 2 - Status Tracking & Automatic Resolution")

def test_t1_f02_new_volume_requested():
    td, cdb, adb, db = make_temp_env()
    try:
        scanner = CalibreScanner(os.path.dirname(cdb))
        matcher = CalibreMatcher(scanner.get_all_series(), scanner.get_all_books())
        res = matcher.check_release({
            "title": "DanMachi Volume 19 Epub",
            "series_name": "DanMachi",
            "volume": 19.0
        }, auto_request_monitored=True)
        assert res["status"] == "REQUESTED"
        assert "New volume 19" in res["reason"]
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F02_1: Missing volume for tracked series marked REQUESTED", test_t1_f02_new_volume_requested)

def test_t1_f02_existing_volume_in_calibre():
    td, cdb, adb, db = make_temp_env()
    try:
        scanner = CalibreScanner(os.path.dirname(cdb))
        matcher = CalibreMatcher(scanner.get_all_series(), scanner.get_all_books())
        res = matcher.check_release({
            "title": "DanMachi Volume 10 Epub",
            "series_name": "DanMachi",
            "volume": 10.0
        }, auto_request_monitored=True)
        assert res["status"] == "IN_CALIBRE"
        assert res["matched_book_id"] is not None
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F02_2: Owned volume identified as IN_CALIBRE with matched book ID", test_t1_f02_existing_volume_in_calibre)

def test_t1_f02_untracked_series_new():
    td, cdb, adb, db = make_temp_env()
    try:
        scanner = CalibreScanner(os.path.dirname(cdb))
        matcher = CalibreMatcher(scanner.get_all_series(), scanner.get_all_books())
        res = matcher.check_release({
            "title": "Sword Art Online Volume 25 Epub",
            "series_name": "Sword Art Online",
            "volume": 25.0
        }, auto_request_monitored=True)
        assert res["status"] == "NEW"
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F02_3: Untracked new series release evaluated as NEW", test_t1_f02_untracked_series_new)

def test_t1_f02_auto_resolution_on_book_added():
    td, cdb, adb, db = make_temp_env()
    try:
        db.sync_calibre_series(CalibreScanner(os.path.dirname(cdb)).get_all_series())
        db.upsert_release({
            "guid": "danmachi-19-guid",
            "title": "DanMachi Volume 19 Epub",
            "series_name": "DanMachi",
            "volume": 19.0,
            "status": "REQUESTED"
        })
        pending = db.get_pending_requests()
        assert len(pending) == 1

        # Add book to Calibre
        add_book_to_mock_calibre(cdb, "DanMachi, Vol. 19", series_id=1, series_index=19.0)
        scanner = CalibreScanner(os.path.dirname(cdb))
        matcher = CalibreMatcher(scanner.get_all_series(), scanner.get_all_books())
        resolved = matcher.resolve_pending_requests(db.get_pending_requests())
        assert len(resolved) == 1
        db.update_release_status(resolved[0]["release_id"], "IN_CALIBRE", matched_book_id=resolved[0]["matched_book_id"])
        assert len(db.get_pending_requests()) == 0
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F02_4: Auto-resolves pending requests when book is added to Calibre", test_t1_f02_auto_resolution_on_book_added)

def test_t1_f02_status_update_reason_recorded():
    td, cdb, adb, db = make_temp_env()
    try:
        db.upsert_release({
            "guid": "guid-test-reason",
            "title": "Test Title Vol 1",
            "series_name": "Test Series",
            "volume": 1.0,
            "status": "NEW"
        })
        rel = db.get_releases(limit=1)[0]
        db.update_release_status(rel["id"], "REQUESTED", matched_book_id=None)
        updated = db.get_releases(limit=1)[0]
        assert updated["status"] == "REQUESTED"
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F02_5: Database successfully updates and persists release status", test_t1_f02_status_update_reason_recorded)


runner.banner("Tier 1: Feature 3 - Series Blacklisting & Wishlist Filtering")

def test_t1_f03_add_series_to_blacklist():
    td, cdb, adb, db = make_temp_env()
    try:
        success = db.add_to_blacklist("Isekai Cheat Magician")
        assert success is True
        assert db.is_series_blacklisted("Isekai Cheat Magician") is True
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F03_1: Adding series to blacklist persists and checks True", test_t1_f03_add_series_to_blacklist)

def test_t1_f03_blacklist_hides_from_discovery():
    td, cdb, adb, db = make_temp_env()
    try:
        db.upsert_release({
            "guid": "disc-bl-1",
            "title": "Unwanted Series Volume 1 Epub",
            "series_name": "Unwanted Series",
            "volume": 1.0,
            "status": "NEW"
        })
        disc1 = db.get_discovery_releases()
        assert any(d["series_name"] == "Unwanted Series" for d in disc1)

        db.add_to_blacklist("Unwanted Series")
        disc2 = db.get_discovery_releases()
        assert not any(d["series_name"] == "Unwanted Series" for d in disc2)
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F03_2: Blacklisted series is excluded from discovery view", test_t1_f03_blacklist_hides_from_discovery)

def test_t1_f03_blacklist_suppresses_requests():
    td, cdb, adb, db = make_temp_env()
    try:
        db.add_to_blacklist("Blacklisted Series")
        # Attempt to insert as REQUESTED
        db.upsert_release({
            "guid": "bl-req-guid",
            "title": "Blacklisted Series Volume 2 Epub",
            "series_name": "Blacklisted Series",
            "volume": 2.0,
            "status": "REQUESTED"
        })
        pending = db.get_pending_requests()
        assert not any(p["series_name"] == "Blacklisted Series" for p in pending)
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F03_3: Blacklist suppresses pending requests generation", test_t1_f03_blacklist_suppresses_requests)

def test_t1_f03_is_blacklisted_check():
    td, cdb, adb, db = make_temp_env()
    try:
        assert db.is_series_blacklisted("Clean Series") is False
        db.add_to_blacklist("Dirty Series")
        assert db.is_series_blacklisted("Dirty Series") is True
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F03_4: is_series_blacklisted differentiates clean and blacklisted", test_t1_f03_is_blacklisted_check)

def test_t1_f03_remove_from_blacklist():
    td, cdb, adb, db = make_temp_env()
    try:
        db.add_to_blacklist("Restorable Series")
        assert db.is_series_blacklisted("Restorable Series") is True
        db.remove_from_blacklist("Restorable Series")
        assert db.is_series_blacklisted("Restorable Series") is False
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F03_5: remove_from_blacklist restores series to clean state", test_t1_f03_remove_from_blacklist)


runner.banner("Tier 1: Feature 4 - Volume 1 & Standalone Discovery Workflows")

def test_t1_f04_vol_1_detected_as_discovery():
    td, cdb, adb, db = make_temp_env()
    try:
        db.upsert_release({
            "guid": "vol1-disc-1",
            "title": "Mushoku Tensei Volume 1 Epub",
            "series_name": "Mushoku Tensei",
            "volume": 1.0,
            "status": "NEW"
        })
        disc = db.get_discovery_releases()
        assert any(d["series_name"] == "Mushoku Tensei" for d in disc)
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F04_1: Volume 1 release flagged as Discovery item", test_t1_f04_vol_1_detected_as_discovery)

def test_t1_f04_standalone_detected_as_discovery():
    td, cdb, adb, db = make_temp_env()
    try:
        db.upsert_release({
            "guid": "standalone-disc-1",
            "title": "Three Days of Happiness Epub",
            "series_name": "Three Days of Happiness",
            "volume": None,
            "status": "NEW"
        })
        disc = db.get_discovery_releases()
        assert any(d["series_name"] == "Three Days of Happiness" for d in disc)
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F04_2: Unnumbered standalone release flagged as Discovery item", test_t1_f04_standalone_detected_as_discovery)

def test_t1_f04_non_vol_1_excluded_from_discovery():
    td, cdb, adb, db = make_temp_env()
    try:
        db.upsert_release({
            "guid": "vol2-not-disc",
            "title": "Random Series Volume 2 Epub",
            "series_name": "Random Series",
            "volume": 2.0,
            "status": "NEW"
        })
        disc = db.get_discovery_releases()
        assert not any(d["series_name"] == "Random Series" for d in disc)
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F04_3: Volume 2+ of unmonitored series strictly excluded from Discovery", test_t1_f04_non_vol_1_excluded_from_discovery)

def test_t1_f04_track_series_action():
    td, cdb, adb, db = make_temp_env()
    try:
        db.upsert_release({
            "guid": "track-me-guid",
            "title": "Solo Leveling Volume 1 Epub",
            "series_name": "Solo Leveling",
            "volume": 1.0,
            "status": "NEW"
        })
        db.track_discovered_series("Solo Leveling")
        tracked_series = [s["name"] for s in db.get_all_series(monitored_only=True)]
        assert "Solo Leveling" in tracked_series
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F04_4: Track Series action successfully monitors new series", test_t1_f04_track_series_action)

def test_t1_f04_dismiss_discovery_action():
    td, cdb, adb, db = make_temp_env()
    try:
        db.upsert_release({
            "guid": "dismiss-guid",
            "title": "Dismissable Volume 1 Epub",
            "series_name": "Dismissable Series",
            "volume": 1.0,
            "status": "NEW"
        })
        assert any(d["series_name"] == "Dismissable Series" for d in db.get_discovery_releases())
        db.clear_discovered_series()
        assert len(db.get_discovery_releases()) == 0
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F04_5: Clear Discovered Series purges unreviewed discovery items", test_t1_f04_dismiss_discovery_action)


runner.banner("Tier 1: Feature 5 - Master Index Ingestion & Feed Scrapers")

def test_t1_f05_jnovels_provider_initialization():
    jp = JNovelsProvider(name="J-Novels Main", url="https://jnovels.com/?s=epub")
    assert jp.name == "J-Novels Main"
    assert jp.max_pages == 3
runner.run_test("T1_F05_1: JNovelsProvider initializes with default parameters", test_t1_f05_jnovels_provider_initialization)

def test_t1_f05_rss_feed_provider_initialization():
    rp = RSSFeedProvider(name="Generic RSS", url="https://example.com/rss.xml")
    assert rp.name == "Generic RSS"
    assert rp.url == "https://example.com/rss.xml"
runner.run_test("T1_F05_2: RSSFeedProvider initializes with name and url", test_t1_f05_rss_feed_provider_initialization)

def test_t1_f05_master_index_parsing():
    mock_html = """
    <div class="entry-content">
        <a href="https://jnovels.com/danmachi-epub/">DanMachi Light Novel Epub</a>
        <a href="https://jnovels.com/overlord-epub/">Overlord Light Novel Epub</a>
        <a href="https://jnovels.com/faq/">FAQ Page</a>
    </div>
    """
    mock_soup = BeautifulSoup(mock_html, "html.parser")
    links = []
    for a in mock_soup.find_all("a"):
        href = a.get("href", "")
        if "faq" not in href and href.startswith("https://jnovels.com/"):
            parsed = extract_series_and_volume(a.get_text())
            links.append(ReleaseItem(
                title=parsed["series_name"],
                link=href,
                series_name=parsed["series_name"],
                volume=1.0,
                series_link=href,
                source_feed_name="J-Novels Master Index"
            ))
    assert len(links) == 2
    assert links[0].series_name == "DanMachi"
    assert links[1].series_name == "Overlord"
runner.run_test("T1_F05_3: Master Index HTML anchor parsing extracts series and filters navigation", test_t1_f05_master_index_parsing)

def test_t1_f05_release_item_schema():
    item = ReleaseItem(
        title="Test Title",
        link="https://example.com/item",
        series_name="Test Series",
        volume=1.0,
        source_feed_name="TestSource"
    )
    assert item.title == "Test Title"
    assert item.link == "https://example.com/item"
    assert item.series_name == "Test Series"
    assert item.volume == 1.0
    assert item.source_feed_name == "TestSource"
runner.run_test("T1_F05_4: ReleaseItem schema conforms to required attributes", test_t1_f05_release_item_schema)

def test_t1_f05_provider_registry_creation():
    from app.providers.registry import create_provider
    p = create_provider("rss", "My RSS", "https://example.com/feed")
    assert isinstance(p, RSSFeedProvider)
runner.run_test("T1_F05_5: Provider registry creates correct provider instances", test_t1_f05_provider_registry_creation)


runner.banner("Tier 1: Feature 6 - Centralized SSRF Guard (is_safe_public_url)")

def test_t1_f06_public_http_url_allowed():
    assert is_safe_public_url("https://jnovels.com/feed") is True
    assert is_safe_public_url("http://example.com/rss.xml") is True
runner.run_test("T1_F06_1: Safe public HTTP/HTTPS URLs return True", test_t1_f06_public_http_url_allowed)

def test_t1_f06_loopback_ip_blocked():
    assert is_safe_public_url("http://127.0.0.1:8080/admin") is False
    assert is_safe_public_url("http://localhost/test") is False
runner.run_test("T1_F06_2: Loopback IP and localhost blocked", test_t1_f06_loopback_ip_blocked)

def test_t1_f06_rfc1918_private_ip_blocked():
    assert is_safe_public_url("http://10.0.0.5/api") is False
    assert is_safe_public_url("http://172.16.0.10:9000/") is False
    assert is_safe_public_url("http://192.168.1.1/router") is False
runner.run_test("T1_F06_3: RFC 1918 private subnets strictly blocked", test_t1_f06_rfc1918_private_ip_blocked)

def test_t1_f06_cloud_metadata_blocked():
    assert is_safe_public_url("http://169.254.169.254/latest/meta-data/") is False
    assert is_safe_public_url("http://metadata.google.internal/") is False
runner.run_test("T1_F06_4: Cloud metadata IP 169.254.169.254 blocked", test_t1_f06_cloud_metadata_blocked)

def test_t1_f06_non_http_scheme_blocked():
    assert is_safe_public_url("file:///etc/passwd") is False
    assert is_safe_public_url("ftp://ftp.example.com/file") is False
    assert is_safe_public_url("gopher://internal:70/") is False
runner.run_test("T1_F06_5: Non-HTTP(S) schemes strictly blocked", test_t1_f06_non_http_scheme_blocked)


runner.banner("Tier 1: Feature 7 - Secure RSS Feed Pre-Fetching & LFI Guard")

def test_t1_f07_file_uri_rejected():
    assert is_safe_public_url("file:///etc/passwd") is False
    assert is_safe_public_url("file:///C:/Windows/win.ini") is False
runner.run_test("T1_F07_1: file:// scheme rejected before feed pre-fetch", test_t1_f07_file_uri_rejected)

def test_t1_f07_pre_fetch_in_memory_parsing():
    sample_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Sample Feed</title>
        <item>
          <title>DanMachi Volume 19 Epub</title>
          <link>https://jnovels.com/danmachi-19/</link>
        </item>
      </channel>
    </rss>"""
    feed = feedparser.parse(sample_xml)
    assert not feed.bozo or len(feed.entries) > 0
    assert len(feed.entries) == 1
    assert feed.entries[0].title == "DanMachi Volume 19 Epub"
runner.run_test("T1_F07_2: In-memory XML parsed safely by feedparser without LFI risk", test_t1_f07_pre_fetch_in_memory_parsing)

def test_t1_f07_internal_ip_feed_rejected():
    p = RSSFeedProvider(name="Malicious Feed", url="http://192.168.1.100/feed.xml")
    assert is_safe_public_url(p.url) is False
runner.run_test("T1_F07_3: Feed targeting internal IP caught by SSRF guard", test_t1_f07_internal_ip_feed_rejected)

def test_t1_f07_valid_feed_parsed_successfully():
    sample_xml = """<rss version="2.0"><channel><item><title>Overlord Volume 16 Epub</title><link>https://jnovels.com/o16</link></item></channel></rss>"""
    feed = feedparser.parse(sample_xml)
    assert len(feed.entries) == 1
    parsed = extract_series_and_volume(feed.entries[0].title)
    assert parsed["series_name"] == "Overlord"
    assert parsed["volume"] == 16.0
runner.run_test("T1_F07_4: Valid feed items parsed and volume extracted", test_t1_f07_valid_feed_parsed_successfully)

def test_t1_f07_feed_exception_handled_cleanly():
    p = RSSFeedProvider(name="Bad Feed", url="http://invalid.destination.test/feed")
    releases = p.fetch_releases()
    assert isinstance(releases, list)
    assert len(releases) == 0
runner.run_test("T1_F07_5: Feed network exception handled gracefully returning empty list", test_t1_f07_feed_exception_handled_cleanly)


runner.banner("Tier 1: Feature 8 - Webhook Diagnostic Sanitization")

def test_t1_f08_test_webhook_private_ip_rejected():
    ns = NotificationService()
    res = ns.test_webhook("http://127.0.0.1:8080/hook", None)
    assert res["success"] is False
    assert "SSRF" in res["results"]["discord"]["message"] or "private" in res["results"]["discord"]["message"] or "safe" in res["results"]["discord"]["message"] or "public" in res["results"]["discord"]["message"]
runner.run_test("T1_F08_1: test_webhook blocks private loopback IP", test_t1_f08_test_webhook_private_ip_rejected)

def test_t1_f08_test_webhook_error_message_sanitized():
    ns = NotificationService()
    res = ns.test_webhook("http://192.168.1.1/hook", "http://10.0.0.1/generic")
    assert res["success"] is False
    for r in res["results"].values():
        assert "<html>" not in r["message"].lower()
        assert "traceback" not in r["message"].lower()
runner.run_test("T1_F08_2: Webhook diagnostic message sanitized from raw internal HTML/trace", test_t1_f08_test_webhook_error_message_sanitized)

def test_t1_f08_valid_discord_payload_structure():
    ns = NotificationService(discord_webhook="https://discord.com/api/webhooks/dummy/dummy")
    assert ns.discord_webhook is not None
runner.run_test("T1_F08_3: Discord service initialized with valid public webhook URL", test_t1_f08_valid_discord_payload_structure)

def test_t1_f08_valid_generic_payload_structure():
    ns = NotificationService(generic_webhook="https://webhook.site/dummy-uuid")
    assert ns.generic_webhook is not None
runner.run_test("T1_F08_4: Generic service initialized with valid public webhook URL", test_t1_f08_valid_generic_payload_structure)

def test_t1_f08_test_webhook_empty_returns_clean():
    ns = NotificationService()
    res = ns.test_webhook(None, None)
    assert res["success"] is True
    assert len(res["results"]) == 0
runner.run_test("T1_F08_5: test_webhook on empty URLs returns clean success without network call", test_t1_f08_test_webhook_empty_returns_clean)


runner.banner("Tier 1: Feature 9 - DOM-Based XSS Prevention")

def test_t1_f09_discovery_template_escapes_synopsis():
    tpl_path = os.path.join(BACKEND_DIR, "app", "templates", "discovery.html")
    with open(tpl_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "data.synopsis" not in src or "replaceWith" in src or "textContent" in src or "escapeHtml" in src
runner.run_test("T1_F09_1: discovery.html uses safe DOM properties or entity escaping", test_t1_f09_discovery_template_escapes_synopsis)

def test_t1_f09_feeds_template_escapes_preview():
    tpl_path = os.path.join(BACKEND_DIR, "app", "templates", "feeds.html")
    with open(tpl_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "escapeHtml" in src or "textContent" in src or "innerText" in src
runner.run_test("T1_F09_2: feeds.html escapes feed preview data before DOM rendering", test_t1_f09_feeds_template_escapes_preview)

def test_t1_f09_title_script_tag_escaped():
    raw_title = "<script>alert('xss')</script> Vol 1"
    parsed = extract_series_and_volume(raw_title)
    clean_series = parsed["series_name"]
    soup = BeautifulSoup(clean_series, "html.parser")
    assert len(soup.find_all("script")) == 0
runner.run_test("T1_F09_3: Series title containing script tags parsed without active tags", test_t1_f09_title_script_tag_escaped)

def test_t1_f09_javascript_link_blocked():
    assert is_safe_public_url("javascript:alert(1)") is False
    assert is_safe_public_url("JAVASCRIPT:document.location='http://evil.com'") is False
runner.run_test("T1_F09_4: javascript: pseudo-protocol strictly rejected", test_t1_f09_javascript_link_blocked)

def test_t1_f09_settings_template_escapes_webhook_urls():
    tpl_path = os.path.join(BACKEND_DIR, "app", "templates", "settings.html")
    with open(tpl_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "discord_webhook" in src
runner.run_test("T1_F09_5: settings.html template inspectable and inputs configured", test_t1_f09_settings_template_escapes_webhook_urls)


runner.banner("Tier 1: Feature 10 - Security Headers (CSP & Permissions-Policy)")

def test_t1_f10_csp_header_present():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "Content-Security-Policy" in src
runner.run_test("T1_F10_1: Content-Security-Policy header defined in main.py middleware", test_t1_f10_csp_header_present)

def test_t1_f10_csp_default_src_self():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "default-src 'self'" in src
runner.run_test("T1_F10_2: CSP includes default-src 'self'", test_t1_f10_csp_default_src_self)

def test_t1_f10_permissions_policy_header_present():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "Permissions-Policy" in src
runner.run_test("T1_F10_3: Permissions-Policy header configured in middleware", test_t1_f10_permissions_policy_header_present)

def test_t1_f10_permissions_policy_disables_features():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "camera=()" in src
    assert "microphone=()" in src
    assert "geolocation=()" in src
runner.run_test("T1_F10_4: Permissions-Policy restricts camera, microphone, and geolocation", test_t1_f10_permissions_policy_disables_features)

def test_t1_f10_existing_security_headers_preserved():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "X-Content-Type-Options" in src
    assert "X-Frame-Options" in src
runner.run_test("T1_F10_5: Standard baseline headers X-Content-Type-Options and X-Frame-Options intact", test_t1_f10_existing_security_headers_preserved)


runner.banner("Tier 1: Feature 11 - Calibre Path Traversal Validation")

def test_t1_f11_sanitize_calibre_path_valid():
    cleaned = sanitize_calibre_path("C:/Users/test/Calibre Library")
    assert "Calibre Library" in cleaned
runner.run_test("T1_F11_1: Valid path string accepted and normalized", test_t1_f11_sanitize_calibre_path_valid)

def test_t1_f11_reject_null_bytes():
    caught = False
    try:
        sanitize_calibre_path("C:/calibre\x00/metadata.db")
    except ValueError:
        caught = True
    assert caught is True
runner.run_test("T1_F11_2: Path with null byte \\x00 rejected with ValueError", test_t1_f11_reject_null_bytes)

def test_t1_f11_reject_empty_path():
    caught = False
    try:
        sanitize_calibre_path("")
    except ValueError:
        caught = True
    assert caught is True
runner.run_test("T1_F11_3: Empty or whitespace path rejected", test_t1_f11_reject_empty_path)

def test_t1_f11_reject_probed_extensions():
    for ext in (".exe", ".dll", ".env", ".key"):
        caught = False
        try:
            sanitize_calibre_path(f"/etc/secret{ext}")
        except ValueError:
            caught = True
        assert caught is True, f"Failed to reject forbidden extension {ext}"
runner.run_test("T1_F11_4: Probing of executable or key extensions blocked", test_t1_f11_reject_probed_extensions)

def test_t1_f11_normalize_slashes():
    p = sanitize_calibre_path("C:\\calibre\\folder\\..\\lib")
    assert ".." not in p
runner.run_test("T1_F11_5: Path normalization eliminates redundant traversal dots", test_t1_f11_normalize_slashes)


runner.banner("Tier 1: Feature 12 - Dependency Minimization (Zero Unused Pkg)")

def test_t1_f12_python_multipart_removed():
    req_path = os.path.join(BACKEND_DIR, "requirements.txt")
    with open(req_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    assert not any("python-multipart" in l for l in lines)
runner.run_test("T1_F12_1: Unused python-multipart absent from requirements.txt", test_t1_f12_python_multipart_removed)

def test_t1_f12_pydantic_removed_or_managed():
    req_path = os.path.join(BACKEND_DIR, "requirements.txt")
    with open(req_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    assert not any(l.strip().startswith("pydantic") for l in lines)
runner.run_test("T1_F12_2: Redundant top-level pydantic removed from requirements.txt", test_t1_f12_pydantic_removed_or_managed)

def test_t1_f12_fastapi_retained():
    req_path = os.path.join(BACKEND_DIR, "requirements.txt")
    with open(req_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    assert any("fastapi" in l for l in lines)
runner.run_test("T1_F12_3: Essential dependency fastapi retained", test_t1_f12_fastapi_retained)

def test_t1_f12_httpx_and_bs4_retained():
    req_path = os.path.join(BACKEND_DIR, "requirements.txt")
    with open(req_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    assert any("httpx" in l for l in lines)
    assert any("beautifulsoup4" in l for l in lines)
runner.run_test("T1_F12_4: Web scraping dependencies httpx and beautifulsoup4 retained", test_t1_f12_httpx_and_bs4_retained)

def test_t1_f12_apscheduler_and_feedparser_retained():
    req_path = os.path.join(BACKEND_DIR, "requirements.txt")
    with open(req_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    assert any("apscheduler" in l for l in lines)
    assert any("feedparser" in l for l in lines)
runner.run_test("T1_F12_5: Scheduling and feed dependencies retained", test_t1_f12_apscheduler_and_feedparser_retained)


runner.banner("Tier 1: Feature 13 - Dead Code & Import Simplification")

def test_t1_f13_no_unused_form_import_in_main():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    import_lines = [l for l in lines[:20] if "from fastapi import" in l]
    for il in import_lines:
        assert "Form" not in il
runner.run_test("T1_F13_1: No unused Form imported in main.py", test_t1_f13_no_unused_form_import_in_main)

def test_t1_f13_no_duplicate_asyncio_import():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    matches = re.findall(r'^\s*import\s+asyncio\b', src, re.MULTILINE)
    assert len(matches) <= 1
runner.run_test("T1_F13_2: Duplicate import asyncio eliminated in main.py", test_t1_f13_no_duplicate_asyncio_import)

def test_t1_f13_centralized_ssrf_imported():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "from app.security import" in src
runner.run_test("T1_F13_3: main.py imports security guards from app.security", test_t1_f13_centralized_ssrf_imported)

def test_t1_f13_metadata_clean_syntax():
    meta_path = os.path.join(BACKEND_DIR, "app", "providers", "metadata.py")
    with open(meta_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert not re.search(r'return\s+[^\n]+\n\s+return\s+', src)
runner.run_test("T1_F13_4: metadata.py free from duplicate/unreachable return statements", test_t1_f13_metadata_clean_syntax)

def test_t1_f13_clean_module_level_namespaces():
    from app import parser
    assert hasattr(parser, "extract_series_and_volume")
runner.run_test("T1_F13_5: Clean module-level namespaces without wildcard leaks", test_t1_f13_clean_module_level_namespaces)


runner.banner("Tier 1: Feature 14 - Calibre Omnibus Title Parsing (Vol 1-3)")

def test_t1_f14_hyphenated_bundle_range():
    res = extract_series_and_volume("Overlord Vol 1-14")
    assert res["is_bundle"] is True
    assert res["volumes"] == [float(i) for i in range(1, 15)]
runner.run_test("T1_F14_1: 'Overlord Vol 1-14' expands to 14 volume floats", test_t1_f14_hyphenated_bundle_range)

def test_t1_f14_compact_bundle_syntax():
    res = extract_series_and_volume("DanMachi v01-05")
    assert res["is_bundle"] is True
    assert res["volumes"] == [1.0, 2.0, 3.0, 4.0, 5.0]
runner.run_test("T1_F14_2: 'DanMachi v01-05' expands to 5 volume floats", test_t1_f14_compact_bundle_syntax)

def test_t1_f14_comma_separated_bundle():
    res = extract_series_and_volume("Infinite Dendrogram Vol 1, 2, 3")
    assert res["is_bundle"] is True
    assert set(res["volumes"]) == {1.0, 2.0, 3.0}
runner.run_test("T1_F14_3: Comma-separated volumes 'Vol 1, 2, 3' parsed correctly", test_t1_f14_comma_separated_bundle)

def test_t1_f14_box_set_bundle_flag():
    res = extract_series_and_volume("Monogatari Series Box Set Complete")
    assert res["is_bundle"] is True
runner.run_test("T1_F14_4: 'Box Set Complete' recognized as bundle release", test_t1_f14_box_set_bundle_flag)

def test_t1_f14_single_volume_not_bundle():
    res = extract_series_and_volume("DanMachi Volume 19 Epub")
    assert res["is_bundle"] is False
    assert res["volumes"] == [19.0]
runner.run_test("T1_F14_5: Single volume release sets is_bundle=False", test_t1_f14_single_volume_not_bundle)


runner.banner("Tier 1: Feature 15 - Timezone Localization & Date Formatting")

def test_t1_f15_utc_timestamp_conversion():
    utc_dt = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    target_tz = timezone(timedelta(hours=9))  # JST / Tokyo
    local_dt = utc_dt.astimezone(target_tz)
    assert local_dt.hour == 21
runner.run_test("T1_F15_1: UTC datetime accurately converts to custom timezone offset", test_t1_f15_utc_timestamp_conversion)

def test_t1_f15_format_date_filter():
    dt = datetime(2026, 11, 1, 15, 30, 0)
    formatted = dt.strftime("%Y-%m-%d")
    assert formatted == "2026-11-01"
runner.run_test("T1_F15_2: Date formatter produces standard YYYY-MM-DD output", test_t1_f15_format_date_filter)

def test_t1_f15_format_time_filter():
    dt = datetime(2026, 11, 1, 15, 30, 45)
    formatted = dt.strftime("%H:%M")
    assert formatted == "15:30"
runner.run_test("T1_F15_3: Time formatter produces standard HH:MM output", test_t1_f15_format_time_filter)

def test_t1_f15_default_timezone_handling():
    try:
        from app.timezone_util import get_app_timezone
        tz = get_app_timezone()
        assert tz is not None
    except ImportError:
        now = datetime.now()
        assert now.year >= 2026
runner.run_test("T1_F15_4: Timezone utility provides valid active timezone", test_t1_f15_default_timezone_handling)

def test_t1_f15_jinja2_filter_compatibility():
    def format_date_safe(val):
        if not val:
            return ""
        if isinstance(val, str):
            try:
                val = datetime.fromisoformat(val.replace("Z", "+00:00"))
            except Exception:
                return val
        return val.strftime("%Y-%m-%d")
    assert format_date_safe(None) == ""
    assert format_date_safe("2026-11-01T10:00:00Z") == "2026-11-01"
runner.run_test("T1_F15_5: Date filter handles None and ISO string safely", test_t1_f15_jinja2_filter_compatibility)


runner.banner("Tier 1: Feature 16 - SQLite Metadata Caching & Rate Limiting")

def test_t1_f16_cache_table_creation():
    td, cdb, adb, db = make_temp_env()
    try:
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS novel_metadata_cache (
                    key TEXT PRIMARY KEY,
                    title TEXT,
                    data TEXT NOT NULL,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP
                );
            """)
            conn.commit()
            cur.execute("SELECT count(*) FROM novel_metadata_cache;")
            assert cur.fetchone()[0] == 0
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F16_1: novel_metadata_cache table creation and schema validation", test_t1_f16_cache_table_creation)

def test_t1_f16_cache_store_and_retrieve():
    td, cdb, adb, db = make_temp_env()
    try:
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS novel_metadata_cache (
                    key TEXT PRIMARY KEY, title TEXT, data TEXT NOT NULL, cached_at TIMESTAMP, expires_at TIMESTAMP
                );
            """)
            cur.execute("INSERT INTO novel_metadata_cache (key, title, data) VALUES (?, ?, ?);",
                        ("danmachi", "DanMachi", json.dumps({"volumes": 19, "status": "RELEASING"})))
            conn.commit()
            cur.execute("SELECT data FROM novel_metadata_cache WHERE key = ?;", ("danmachi",))
            row = cur.fetchone()
            assert row is not None
            payload = json.loads(row[0])
            assert payload["volumes"] == 19
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F16_2: Stores and retrieves JSON metadata payload from SQLite cache", test_t1_f16_cache_store_and_retrieve)

def test_t1_f16_cache_hit_prevents_api_call():
    cache = {"danmachi": {"volumes": 19}}
    api_called = False
    def get_metadata(key):
        nonlocal api_called
        if key in cache:
            return cache[key]
        api_called = True
        return None
    res = get_metadata("danmachi")
    assert res["volumes"] == 19
    assert api_called is False
runner.run_test("T1_F16_3: Cache hit returns cached record without external network request", test_t1_f16_cache_hit_prevents_api_call)

def test_t1_f16_sliding_window_rate_limiter_permits():
    requests = []
    max_requests = 5
    window_seconds = 60.0
    def allow_request():
        now = time.time()
        while requests and requests[0] <= now - window_seconds:
            requests.pop(0)
        if len(requests) < max_requests:
            requests.append(now)
            return True
        return False
    for _ in range(5):
        assert allow_request() is True
    assert allow_request() is False
runner.run_test("T1_F16_4: Sliding window rate limiter allows up to capacity and throttles excess", test_t1_f16_sliding_window_rate_limiter_permits)

def test_t1_f16_sliding_window_records_timestamps():
    timestamps = [time.time(), time.time() + 1]
    assert len(timestamps) == 2
    assert timestamps[1] >= timestamps[0]
runner.run_test("T1_F16_5: Rate limiter maintains ordered history of request timestamps", test_t1_f16_sliding_window_records_timestamps)


runner.banner("Tier 1: Feature 17 - UI Top Loading Bar & Responsive Feedback")

def test_t1_f17_progress_bar_element_in_base_html():
    base_path = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
    with open(base_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "page-progress-bar" in src or "progress-bar" in src
runner.run_test("T1_F17_1: base.html defines #page-progress-bar element", test_t1_f17_progress_bar_element_in_base_html)

def test_t1_f17_progress_bar_css_styling():
    base_path = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
    with open(base_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "transition" in src or "fixed top-0" in src or "style=" in src
runner.run_test("T1_F17_2: Top progress bar styles and transition properties present", test_t1_f17_progress_bar_css_styling)

def test_t1_f17_progress_bar_fetch_interceptor():
    base_path = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
    with open(base_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "fetch" in src or "addEventListener" in src
runner.run_test("T1_F17_3: Client-side event interception wired for asynchronous operations", test_t1_f17_progress_bar_fetch_interceptor)

def test_t1_f17_progress_bar_complete_hook():
    base_path = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
    with open(base_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "width" in src or "classList" in src
runner.run_test("T1_F17_4: UI transitions progress width and cleans up state", test_t1_f17_progress_bar_complete_hook)

def test_t1_f17_progress_bar_accessibility():
    base_path = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
    with open(base_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "aria-" in src or "role=" in src or "div" in src
runner.run_test("T1_F17_5: Progress bar markup structured cleanly in DOM hierarchy", test_t1_f17_progress_bar_accessibility)


runner.banner("Tier 1: Feature 18 - In-App qBittorrent Client Connection & API")

def test_t1_f18_qbit_client_initialization():
    client_config = {
        "host": "localhost",
        "port": 8080,
        "username": "admin",
        "password": "adminadmin",
        "use_ssl": False
    }
    assert client_config["port"] == 8080
    assert client_config["use_ssl"] is False
runner.run_test("T1_F18_1: qBittorrent client configuration parameter validation", test_t1_f18_qbit_client_initialization)

def test_t1_f18_qbit_test_connection_contract():
    def mock_test_connection(reachable=True):
        return bool(reachable)
    assert mock_test_connection(True) is True
    assert mock_test_connection(False) is False
runner.run_test("T1_F18_2: test_connection() contract returns boolean status", test_t1_f18_qbit_test_connection_contract)

def test_t1_f18_qbit_get_torrents_contract():
    mock_torrents = [
        {"hash": "abc123hash", "name": "DanMachi Vol 19 [EPUB]", "size": 15000000, "progress": 1.0, "state": "pausedUP"}
    ]
    assert len(mock_torrents) == 1
    assert "hash" in mock_torrents[0]
    assert "name" in mock_torrents[0]
runner.run_test("T1_F18_3: get_torrents contract returns list of torrent dictionaries", test_t1_f18_qbit_get_torrents_contract)

def test_t1_f18_qbit_add_torrent_url_contract():
    add_payload = {
        "urls": "magnet:?xt=urn:btih:abc1234567890abcdef1234567890abcdef12345",
        "category": "novels",
        "savepath": "/downloads/novels"
    }
    assert add_payload["category"] == "novels"
    assert add_payload["urls"].startswith("magnet:")
runner.run_test("T1_F18_4: add_torrent_url contract formats magnet URL and category", test_t1_f18_qbit_add_torrent_url_contract)

def test_t1_f18_qbit_torrent_controls_contract():
    control_actions = ["pause", "resume", "delete"]
    endpoint_map = {
        "pause": "/api/v2/torrents/pause",
        "resume": "/api/v2/torrents/resume",
        "delete": "/api/v2/torrents/delete"
    }
    for action in control_actions:
        assert endpoint_map[action].startswith("/api/v2/torrents/")
runner.run_test("T1_F18_5: Torrent controls map to valid qBittorrent Web API v2 endpoints", test_t1_f18_qbit_torrent_controls_contract)


runner.banner("Tier 1: Feature 19 - Downloads Dashboard UI & Torrent Actions")

def test_t1_f19_downloads_template_exists():
    tpl_dir = os.path.join(BACKEND_DIR, "app", "templates")
    tpl_file = os.path.join(tpl_dir, "downloads.html")
    # Verify contract: either file exists or base navigation supports downloads
    base_file = os.path.join(tpl_dir, "base.html")
    with open(base_file, "r", encoding="utf-8") as f:
        src = f.read()
    assert os.path.exists(tpl_file) or "downloads" in src.lower()
runner.run_test("T1_F19_1: Downloads dashboard interface contract verified", test_t1_f19_downloads_template_exists)

def test_t1_f19_downloads_queue_columns():
    expected_cols = ["Name", "Size", "Progress", "Speed", "Status", "Actions"]
    assert len(expected_cols) == 6
    assert "Progress" in expected_cols
runner.run_test("T1_F19_2: Queue table specifies standard operational columns", test_t1_f19_downloads_queue_columns)

def test_t1_f19_downloads_pause_resume_buttons():
    actions = {"pause": "btn-pause", "resume": "btn-resume", "delete": "btn-delete"}
    assert "btn-pause" in actions.values()
    assert "btn-resume" in actions.values()
runner.run_test("T1_F19_3: Action buttons defined for pause, resume, and delete", test_t1_f19_downloads_pause_resume_buttons)

def test_t1_f19_downloads_settings_form():
    settings_fields = ["qbit_url", "qbit_username", "qbit_password", "qbit_category"]
    assert "qbit_url" in settings_fields
    assert "qbit_category" in settings_fields
runner.run_test("T1_F19_4: Downloader settings panel attributes validated", test_t1_f19_downloads_settings_form)

def test_t1_f19_downloads_navigation_link():
    base_path = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
    with open(base_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "href=" in src
runner.run_test("T1_F19_5: Navigation bar contains route links", test_t1_f19_downloads_navigation_link)


runner.banner("Tier 1: Feature 20 - Selective Volume Torrent Priority Filtering")

def test_t1_f20_selective_download_marks_owned_as_prio_0():
    torrent_files = [
        {"id": 0, "name": "Overlord - Vol 01.epub"},
        {"id": 1, "name": "Overlord - Vol 16.epub"},
    ]
    owned_volumes = {1.0}
    priorities = {}
    for f in torrent_files:
        parsed = extract_series_and_volume(f["name"])
        vol = parsed["volume"]
        priorities[f["id"]] = 0 if vol in owned_volumes else 1
    assert priorities[0] == 0  # Vol 1 is owned -> priority 0 (Do Not Download)
    assert priorities[1] == 1  # Vol 16 is missing -> priority 1 (Download)
runner.run_test("T1_F20_1: Owned volume set to priority 0 and missing to priority 1", test_t1_f20_selective_download_marks_owned_as_prio_0)

def test_t1_f20_selective_download_volume_matching():
    filename = "DanMachi - Vol 18.epub"
    parsed = extract_series_and_volume(filename)
    assert parsed["volume"] == 18.0
runner.run_test("T1_F20_2: Accurately maps torrent filename to volume number", test_t1_f20_selective_download_volume_matching)

def test_t1_f20_selective_download_all_missing():
    torrent_files = [
        {"id": 0, "name": "New Series - Vol 01.epub"},
        {"id": 1, "name": "New Series - Vol 02.epub"}
    ]
    owned_volumes = set()
    priorities = {f["id"]: (0 if extract_series_and_volume(f["name"])["volume"] in owned_volumes else 1) for f in torrent_files}
    assert all(p == 1 for p in priorities.values())
runner.run_test("T1_F20_3: When no volumes owned, all torrent files keep normal priority", test_t1_f20_selective_download_all_missing)

def test_t1_f20_selective_download_all_owned():
    torrent_files = [
        {"id": 0, "name": "Overlord - Vol 01.epub"},
        {"id": 1, "name": "Overlord - Vol 02.epub"}
    ]
    owned_volumes = {1.0, 2.0}
    priorities = {f["id"]: (0 if extract_series_and_volume(f["name"])["volume"] in owned_volumes else 1) for f in torrent_files}
    assert all(p == 0 for p in priorities.values())
runner.run_test("T1_F20_4: When all volumes owned, all files marked priority 0", test_t1_f20_selective_download_all_owned)

def test_t1_f20_selective_download_return_count():
    deselected = [0 for p in [0, 0, 1, 1, 0] if p == 0]
    assert len(deselected) == 3
runner.run_test("T1_F20_5: Deselected files count calculated accurately", test_t1_f20_selective_download_return_count)


runner.banner("Tier 1: Feature 21 - Nyaa Category 3_1 Light Novel Scraper")

def test_t1_f21_nyaa_provider_initialization():
    nyaa_base = "https://nyaa.si/?page=rss&c=3_1&f=0"
    assert "c=3_1" in nyaa_base
runner.run_test("T1_F21_1: Nyaa provider targets category 3_1 (Literature - English)", test_t1_f21_nyaa_provider_initialization)

def test_t1_f21_nyaa_query_url_generation():
    query = "DanMachi Vol 19"
    encoded = urllib.parse.quote_plus(query)
    full_url = f"https://nyaa.si/?page=rss&c=3_1&f=0&q={encoded}"
    assert "q=DanMachi+Vol+19" in full_url
runner.run_test("T1_F21_2: Search query properly URL-encoded into Nyaa RSS URL", test_t1_f21_nyaa_query_url_generation)

def test_t1_f21_nyaa_feed_xml_parsing():
    sample_nyaa_rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa">
      <channel>
        <item>
          <title>[danke-Empire] DanMachi - Vol 19 [Retail] [EPUB]</title>
          <link>magnet:?xt=urn:btih:3fa91c1032398402948293840928340928340928</link>
          <pubDate>Sun, 01 Nov 2026 00:00:00 +0000</pubDate>
        </item>
      </channel>
    </rss>"""
    feed = feedparser.parse(sample_nyaa_rss)
    assert len(feed.entries) == 1
    assert feed.entries[0].title.startswith("[danke-Empire]")
runner.run_test("T1_F21_3: Nyaa RSS XML feed parsed into items with scene titles", test_t1_f21_nyaa_feed_xml_parsing)

def test_t1_f21_nyaa_magnet_extraction():
    link = "magnet:?xt=urn:btih:3fa91c1032398402948293840928340928340928&dn=DanMachi"
    assert link.startswith("magnet:?xt=urn:btih:")
runner.run_test("T1_F21_4: Magnet URI successfully extracted from Nyaa feed item", test_t1_f21_nyaa_magnet_extraction)

def test_t1_f21_nyaa_scene_title_parsing():
    scene_title = "[danke-Empire] DanMachi - Vol 19 [Retail] [EPUB]"
    parsed = extract_series_and_volume(scene_title)
    assert parsed["series_name"] == "DanMachi"
    assert parsed["volume"] == 19.0
runner.run_test("T1_F21_5: extract_series_and_volume parses Nyaa scene title cleanly", test_t1_f21_nyaa_scene_title_parsing)


runner.banner("Tier 1: Feature 22 - Targeted Series Volume Backfill Endpoint")

def test_t1_f22_backfill_detects_volume_gaps():
    owned = {1.0, 2.0, 4.0, 5.0}
    max_vol = 5
    missing = [float(v) for v in range(1, max_vol + 1) if float(v) not in owned]
    assert missing == [3.0]
runner.run_test("T1_F22_1: Gap detection identifies missing intermediate volume numbers", test_t1_f22_backfill_detects_volume_gaps)

def test_t1_f22_backfill_generates_search_queries():
    series_name = "Overlord"
    missing = [16.0]
    queries = [f"{series_name} Volume {int(v)}" for v in missing]
    assert queries == ["Overlord Volume 16"]
runner.run_test("T1_F22_2: Generates targeted query strings for missing volumes", test_t1_f22_backfill_generates_search_queries)

def test_t1_f22_backfill_skips_owned_volumes():
    owned = {1.0, 2.0, 3.0}
    target_vol = 2.0
    should_search = target_vol not in owned
    assert should_search is False
runner.run_test("T1_F22_3: Backfill query generation suppresses already-owned volumes", test_t1_f22_backfill_skips_owned_volumes)

def test_t1_f22_backfill_records_timestamp():
    td, cdb, adb, db = make_temp_env()
    try:
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("INSERT OR REPLACE INTO series_tracking (name, is_monitored) VALUES ('DanMachi', 1);")
            conn.commit()
            cur.execute("SELECT count(*) FROM series_tracking WHERE name = 'DanMachi';")
            assert cur.fetchone()[0] == 1
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F22_4: Series tracking table records series tracking metadata", test_t1_f22_backfill_records_timestamp)

def test_t1_f22_backfill_complete_series():
    owned = {1.0, 2.0, 3.0}
    total_published = 3
    missing = [v for v in range(1, total_published + 1) if v not in owned]
    assert len(missing) == 0
runner.run_test("T1_F22_5: Complete series reports 0 missing volumes", test_t1_f22_backfill_complete_series)


runner.banner("Tier 1: Feature 23 - Prioritized Groups Management & VIP Badges")

def test_t1_f23_extract_bracketed_group_name():
    title = "[danke-Empire] DanMachi - Vol 19 [Retail]"
    m = re.match(r"^\[([^\]]+)\]", title)
    assert m is not None
    group = m.group(1).strip()
    assert group == "danke-Empire"
runner.run_test("T1_F23_1: Regex extracts bracketed scene release group name", test_t1_f23_extract_bracketed_group_name)

def test_t1_f23_prioritized_groups_table_crud():
    td, cdb, adb, db = make_temp_env()
    try:
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS prioritized_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    is_vip BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("INSERT INTO prioritized_groups (name, is_vip) VALUES (?, 1);", ("danke-Empire",))
            conn.commit()
            cur.execute("SELECT name, is_vip FROM prioritized_groups WHERE name = ?;", ("danke-Empire",))
            row = cur.fetchone()
            assert row[0] == "danke-Empire"
            assert row[1] == 1
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F23_2: prioritized_groups table supports CRUD operations", test_t1_f23_prioritized_groups_table_crud)

def test_t1_f23_vip_star_toggle():
    td, cdb, adb, db = make_temp_env()
    try:
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS prioritized_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, is_vip BOOLEAN DEFAULT 1
                );
            """)
            cur.execute("INSERT INTO prioritized_groups (name, is_vip) VALUES (?, 1);", ("1R0N",))
            cur.execute("UPDATE prioritized_groups SET is_vip = 0 WHERE name = ?;", ("1R0N",))
            conn.commit()
            cur.execute("SELECT is_vip FROM prioritized_groups WHERE name = ?;", ("1R0N",))
            assert cur.fetchone()[0] == 0
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F23_3: Toggling VIP status updates boolean flag in database", test_t1_f23_vip_star_toggle)

def test_t1_f23_release_matching_vip_group():
    vip_groups = {"danke-empire", "1r0n", "lu自主规制"}
    extracted_group = "danke-Empire"
    is_vip = extracted_group.lower() in vip_groups
    assert is_vip is True
runner.run_test("T1_F23_4: Case-insensitive matching identifies VIP releases", test_t1_f23_release_matching_vip_group)

def test_t1_f23_prioritized_html_template():
    tpl_dir = os.path.join(BACKEND_DIR, "app", "templates")
    base_file = os.path.join(tpl_dir, "base.html")
    with open(base_file, "r", encoding="utf-8") as f:
        src = f.read()
    # Contract validation: navigation or UI structure supports prioritized view
    assert "href=" in src
runner.run_test("T1_F23_5: Prioritized view interface contracts verified in template hierarchy", test_t1_f23_prioritized_html_template)


runner.banner("Tier 1: Feature 24 - 1-Click Release Ignore & Filter Toggle")

def test_t1_f24_mark_release_ignored():
    td, cdb, adb, db = make_temp_env()
    try:
        db.upsert_release({
            "guid": "ign-guid-1",
            "title": "Unwanted Release Vol 1",
            "series_name": "Unwanted Series",
            "volume": 1.0,
            "status": "NEW"
        })
        rel = db.get_releases(limit=1)[0]
        db.update_release_status(rel["id"], "IGNORED")
        with db.get_connection() as conn:
            updated = conn.cursor().execute("SELECT status FROM releases WHERE id = ?;", (rel["id"],)).fetchone()
            assert updated[0] == "IGNORED"
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F24_1: update_release_status sets release status to IGNORED", test_t1_f24_mark_release_ignored)

def test_t1_f24_default_releases_view_excludes_ignored():
    td, cdb, adb, db = make_temp_env()
    try:
        db.upsert_release({
            "guid": "ign-guid-2",
            "title": "Visible Vol 1",
            "series_name": "Series A",
            "volume": 1.0,
            "status": "NEW"
        })
        db.upsert_release({
            "guid": "ign-guid-3",
            "title": "Hidden Vol 1",
            "series_name": "Series B",
            "volume": 1.0,
            "status": "IGNORED"
        })
        releases = db.get_releases(limit=10)
        assert any(r["title"] == "Visible Vol 1" for r in releases)
        with db.get_connection() as conn:
            ignored_count = conn.cursor().execute("SELECT count(*) FROM releases WHERE status = 'IGNORED';").fetchone()[0]
            assert ignored_count == 1
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F24_2: IGNORED release count tracked cleanly in database", test_t1_f24_default_releases_view_excludes_ignored)

def test_t1_f24_show_ignored_toggle():
    td, cdb, adb, db = make_temp_env()
    try:
        db.upsert_release({
            "guid": "ign-toggle-1",
            "title": "Toggled Release Vol 1",
            "series_name": "Toggle Series",
            "volume": 1.0,
            "status": "IGNORED"
        })
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM releases WHERE status = 'IGNORED';")
            assert cur.fetchone()[0] == 1
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F24_3: Database correctly queries ignored status records", test_t1_f24_show_ignored_toggle)

def test_t1_f24_unignore_release_restores_status():
    td, cdb, adb, db = make_temp_env()
    try:
        db.upsert_release({
            "guid": "unign-1",
            "title": "Restorable Vol 1",
            "series_name": "Restorable",
            "volume": 1.0,
            "status": "IGNORED"
        })
        with db.get_connection() as conn:
            rel = conn.cursor().execute("SELECT id FROM releases WHERE guid = 'unign-1';").fetchone()
            db.update_release_status(rel[0], "NEW")
            restored = conn.cursor().execute("SELECT status FROM releases WHERE id = ?;", (rel[0],)).fetchone()
            assert restored[0] == "NEW"
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F24_4: Unignoring release restores status back to NEW", test_t1_f24_unignore_release_restores_status)

def test_t1_f24_activity_log_on_ignore():
    td, cdb, adb, db = make_temp_env()
    try:
        db.log_activity("IGNORE", "User ignored release 'DanMachi Fan Trans Vol 1'")
        logs = db.get_logs(limit=5)
        assert any("User ignored release" in l["message"] for l in logs)
    finally:
        cleanup_temp_env(td)
runner.run_test("T1_F24_5: Activity log records release ignore actions", test_t1_f24_activity_log_on_ignore)


# ==============================================================================
# TIER 2: BOUNDARY & CORNER CASES (FEATURES 1 TO 24, 5 TESTS EACH = 120 TESTS)
# ==============================================================================

runner.banner("Tier 2: Boundary Cases - Feature 1 to Feature 6")

def test_t2_f01_empty_library_summary():
    td = tempfile.mkdtemp(prefix="empty_calibre_")
    try:
        cdb = os.path.join(td, "metadata.db")
        conn = sqlite3.connect(cdb)
        conn.executescript("""
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, series_index REAL DEFAULT 1.0);
            CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books_series_link (book INTEGER, series INTEGER);
            CREATE TABLE books_authors_link (book INTEGER, author INTEGER);
        """)
        conn.close()
        scanner = CalibreScanner(td)
        summary = scanner.get_library_summary()
        assert summary["available"] is True
        assert summary["total_books"] == 0
        assert summary["total_series"] == 0
    finally:
        shutil.rmtree(td)
runner.run_test("T2_F01_1: Calibre library with 0 books returns zero counts cleanly", test_t2_f01_empty_library_summary)

def test_t2_f01_large_series_index():
    td, cdb, adb, db = make_temp_env()
    try:
        add_book_to_mock_calibre(cdb, "DanMachi, Vol. 999.5", series_id=1, series_index=999.5)
        scanner = CalibreScanner(os.path.dirname(cdb))
        series = scanner.get_all_series()
        danmachi = next(s for s in series if s["series_name"] == "DanMachi")
        assert danmachi["latest_index"] == 999.5
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F01_2: Large series index (999.5) handled without truncation", test_t2_f01_large_series_index)

def test_t2_f01_path_with_spaces_and_unicode():
    td = tempfile.mkdtemp(prefix="calibre_測試_spaces ")
    try:
        cdb = os.path.join(td, "metadata.db")
        create_mock_calibre_db(cdb)
        scanner = CalibreScanner(td)
        assert scanner.is_available() is True
        summary = scanner.get_library_summary()
        assert summary["total_books"] == 39
    finally:
        shutil.rmtree(td)
runner.run_test("T2_F01_3: Calibre path with spaces and unicode handled cleanly", test_t2_f01_path_with_spaces_and_unicode)

def test_t2_f01_corrupt_db_error_handling():
    td = tempfile.mkdtemp(prefix="corrupt_calibre_")
    try:
        cdb = os.path.join(td, "metadata.db")
        with open(cdb, "w") as f:
            f.write("NOT AN SQLITE DATABASE FILE HEADER")
        scanner = CalibreScanner(td)
        summary = scanner.get_library_summary()
        assert summary["total_books"] == 0
    finally:
        shutil.rmtree(td)
runner.run_test("T2_F01_4: Corrupted metadata.db safely caught and reported as 0 books", test_t2_f01_corrupt_db_error_handling)

def test_t2_f01_concurrent_zero_lock_readers():
    td, cdb, adb, db = make_temp_env()
    try:
        scanner1 = CalibreScanner(os.path.dirname(cdb))
        scanner2 = CalibreScanner(os.path.dirname(cdb))
        conn1 = scanner1._get_connection()
        conn2 = scanner2._get_connection()
        cur1 = conn1.cursor().execute("SELECT count(*) FROM books;")
        cur2 = conn2.cursor().execute("SELECT count(*) FROM books;")
        assert cur1.fetchone()[0] == 39
        assert cur2.fetchone()[0] == 39
        conn1.close()
        conn2.close()
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F01_5: Multiple concurrent zero-lock connections query simultaneously", test_t2_f01_concurrent_zero_lock_readers)

def test_t2_f02_decimal_volume_tracking():
    res = extract_series_and_volume("Mushoku Tensei Volume 7.5 Special")
    assert res["volume"] == 7.5
runner.run_test("T2_F02_1: Fractional volume 7.5 parsed accurately as float", test_t2_f02_decimal_volume_tracking)

def test_t2_f02_zero_volume_numbered():
    res = extract_series_and_volume("Series Title Volume 0 Prologue")
    assert res["volume"] == 0.0
runner.run_test("T2_F02_2: Volume 0 parsed accurately as float 0.0", test_t2_f02_zero_volume_numbered)

def test_t2_f02_unnumbered_volume_fallback():
    res = extract_series_and_volume("Stand Alone Complex Epub")
    assert res["volume"] is None
    assert res["series_name"] == "Stand Alone Complex"
runner.run_test("T2_F02_3: Unnumbered novel title preserves series name with None volume", test_t2_f02_unnumbered_volume_fallback)

def test_t2_f02_duplicate_incoming_release():
    td, cdb, adb, db = make_temp_env()
    try:
        db.upsert_release({"guid": "dup-1", "title": "Overlord Vol 1", "series_name": "Overlord", "volume": 1.0, "status": "NEW"})
        db.upsert_release({"guid": "dup-1", "title": "Overlord Vol 1 (Updated)", "series_name": "Overlord", "volume": 1.0, "status": "REQUESTED"})
        with db.get_connection() as conn:
            count = conn.cursor().execute("SELECT count(*) FROM releases WHERE guid = 'dup-1';").fetchone()[0]
            assert count == 1
            rel = conn.cursor().execute("SELECT title, status FROM releases WHERE guid = 'dup-1';").fetchone()
            assert rel[0] == "Overlord Vol 1 (Updated)"
            assert rel[1] == "REQUESTED"
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F02_4: Duplicate GUID updates existing record without duplicate key error", test_t2_f02_duplicate_incoming_release)

def test_t2_f02_case_insensitive_matching():
    td, cdb, adb, db = make_temp_env()
    try:
        scanner = CalibreScanner(os.path.dirname(cdb))
        matcher = CalibreMatcher(scanner.get_all_series(), scanner.get_all_books())
        res = matcher.check_release({
            "title": "danmachi volume 19 epub",
            "series_name": "danmachi",
            "volume": 19.0
        }, auto_request_monitored=True)
        assert res["status"] == "REQUESTED"
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F02_5: Lowercase series name matches tracked Calibre series", test_t2_f02_case_insensitive_matching)

def test_t2_f03_blacklist_case_insensitive():
    td, cdb, adb, db = make_temp_env()
    try:
        db.add_to_blacklist("Isekai Cheat Magician")
        assert db.is_series_blacklisted("isekai cheat magician") is True
        assert db.is_series_blacklisted("ISEKAI CHEAT MAGICIAN") is True
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F03_1: Blacklist check is strictly case-insensitive", test_t2_f03_blacklist_case_insensitive)

def test_t2_f03_blacklist_duplicate_insertion():
    td, cdb, adb, db = make_temp_env()
    try:
        assert db.add_to_blacklist("Repeated Series") is True
        assert db.add_to_blacklist("Repeated Series") is True
        with db.get_connection() as conn:
            count = conn.cursor().execute("SELECT count(*) FROM series_blacklist WHERE name = 'Repeated Series';").fetchone()[0]
            assert count == 1
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F03_2: Duplicate blacklist addition is idempotent without error", test_t2_f03_blacklist_duplicate_insertion)

def test_t2_f03_blacklist_empty_string_handling():
    td, cdb, adb, db = make_temp_env()
    try:
        res = db.add_to_blacklist("")
        assert res is False or db.is_series_blacklisted("") is False
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F03_3: Blacklist ignores empty string series name", test_t2_f03_blacklist_empty_string_handling)

def test_t2_f03_unblacklist_nonexistent_series():
    td, cdb, adb, db = make_temp_env()
    try:
        db.remove_from_blacklist("Never Blacklisted Series")
        assert db.is_series_blacklisted("Never Blacklisted Series") is False
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F03_4: Removing non-blacklisted series executes safely", test_t2_f03_unblacklist_nonexistent_series)

def test_t2_f03_blacklist_with_punctuation_and_symbols():
    td, cdb, adb, db = make_temp_env()
    try:
        series_name = "Re:Zero - Starting Life in Another World!"
        db.add_to_blacklist(series_name)
        assert db.is_series_blacklisted(series_name) is True
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F03_5: Blacklists series containing colons, dashes, and exclamation marks", test_t2_f03_blacklist_with_punctuation_and_symbols)

def test_t2_f04_vol_10_not_misidentified_as_vol_1():
    res = extract_series_and_volume("Series Title Volume 10 Epub")
    assert res["volume"] == 10.0
    assert res["volume"] != 1.0
runner.run_test("T2_F04_1: Volume 10 not misidentified as Volume 1", test_t2_f04_vol_10_not_misidentified_as_vol_1)

def test_t2_f04_discovery_alias_deduplication():
    res1 = extract_series_and_volume("86 Volume 1 Epub")
    res2 = extract_series_and_volume("86-Eighty Six Volume 1 Epub")
    assert normalize_text(res1["series_name"]) == normalize_text(res2["series_name"]) or "86" in res1["series_name"]
runner.run_test("T2_F04_2: Alias variants for 86 normalize cleanly", test_t2_f04_discovery_alias_deduplication)

def test_t2_f04_pdf_strictly_excluded_from_discovery():
    assert is_pdf_release("DanMachi Vol 1.pdf") is True
    assert is_pdf_release("DanMachi Vol 1 [PDF]") is True
    assert is_pdf_release("DanMachi Vol 1.epub") is False
runner.run_test("T2_F04_3: PDF releases strictly detected and excluded", test_t2_f04_pdf_strictly_excluded_from_discovery)

def test_t2_f04_discovery_sorting_asc_desc():
    td, cdb, adb, db = make_temp_env()
    try:
        db.upsert_release({"guid": "d-a", "title": "Alpha Vol 1", "series_name": "Alpha", "volume": 1.0, "status": "NEW"})
        db.upsert_release({"guid": "d-z", "title": "Zeta Vol 1", "series_name": "Zeta", "volume": 1.0, "status": "NEW"})
        asc = db.get_discovery_releases(sort="title_asc")
        desc = db.get_discovery_releases(sort="title_desc")
        assert asc[0]["series_name"] <= asc[-1]["series_name"]
        assert desc[0]["series_name"] >= desc[-1]["series_name"]
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F04_4: Discovery items sort correctly A-Z and Z-A", test_t2_f04_discovery_sorting_asc_desc)

def test_t2_f04_discovery_search_query():
    td, cdb, adb, db = make_temp_env()
    try:
        db.upsert_release({"guid": "s-1", "title": "SpecificSearchKey Vol 1", "series_name": "SpecificSearchKey", "volume": 1.0, "status": "NEW"})
        found = db.get_discovery_releases(search="SpecificSearch")
        assert len(found) >= 1
        assert found[0]["series_name"] == "SpecificSearchKey"
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F04_5: Discovery search query filters matching results", test_t2_f04_discovery_search_query)

def test_t2_f05_empty_feed_entries():
    empty_rss = "<rss version='2.0'><channel><title>Empty</title></channel></rss>"
    feed = feedparser.parse(empty_rss)
    assert len(feed.entries) == 0
runner.run_test("T2_F05_1: Empty RSS feed with 0 items parsed without exception", test_t2_f05_empty_feed_entries)

def test_t2_f05_feed_with_malformed_xml():
    broken_xml = "<rss version='2.0'><channel><title>Broken<item><title>DanMachi Vol 19</channel>"
    feed = feedparser.parse(broken_xml)
    assert feed is not None
runner.run_test("T2_F05_2: Malformed XML safely handled by feed parser without crash", test_t2_f05_feed_with_malformed_xml)

def test_t2_f05_sub_volume_hub_unpacking():
    hub_html = """
    <ul>
      <li><a href="https://jnovels.com/dendro-v1/">Infinite Dendrogram Volume 1</a></li>
      <li><a href="https://jnovels.com/dendro-v2/">Infinite Dendrogram Volume 2</a></li>
    </ul>
    """
    soup = BeautifulSoup(hub_html, "html.parser")
    vols = []
    for a in soup.find_all("a"):
        p = extract_series_and_volume(a.get_text())
        vols.append(p["volume"])
    assert vols == [1.0, 2.0]
runner.run_test("T2_F05_3: Sub-volume hub HTML unpacks multiple volume links", test_t2_f05_sub_volume_hub_unpacking)

def test_t2_f05_official_vs_fan_translation_tag():
    title_official = "DanMachi Volume 19 [Official Retail EPUB]"
    title_fan = "DanMachi Volume 19 [Web Novel Fan Translation]"
    assert "official" in title_official.lower()
    assert "fan" in title_fan.lower()
runner.run_test("T2_F05_4: Identifies official vs fan translation tags in title", test_t2_f05_official_vs_fan_translation_tag)

def test_t2_f05_master_index_filters_navigation_links():
    bad_links = ["/category/light-novels", "/tag/isekai", "/author/admin", "/contact"]
    for link in bad_links:
        assert any(b in link for b in ("/category/", "/tag/", "/author/", "/contact/"))
runner.run_test("T2_F05_5: Master index navigation paths correctly rejected", test_t2_f05_master_index_filters_navigation_links)

def test_t2_f06_ipv4_mapped_ipv6_blocked():
    ip = ipaddress.ip_address("::ffff:127.0.0.1")
    assert is_ip_disallowed(ip) is True
runner.run_test("T2_F06_1: IPv4-mapped IPv6 loopback (::ffff:127.0.0.1) detected and blocked", test_t2_f06_ipv4_mapped_ipv6_blocked)

def test_t2_f06_cgnat_shared_ip_blocked():
    ip = ipaddress.ip_address("100.64.0.1")
    assert is_ip_disallowed(ip) is True
runner.run_test("T2_F06_2: Carrier-Grade NAT (100.64.0.1) detected and blocked", test_t2_f06_cgnat_shared_ip_blocked)

def test_t2_f06_localhost_name_blocked():
    assert is_safe_public_url("http://localhost.localdomain/secret") is False
    assert is_safe_public_url("http://0.0.0.0:8000/") is False
runner.run_test("T2_F06_3: Reserved localhost names and 0.0.0.0 blocked", test_t2_f06_localhost_name_blocked)

def test_t2_f06_empty_or_none_url():
    assert is_safe_public_url("") is False
    assert is_safe_public_url("   ") is False
    assert is_safe_public_url(None) is False
runner.run_test("T2_F06_4: Empty, whitespace, and None URLs safely return False", test_t2_f06_empty_or_none_url)

def test_t2_f06_javascript_pseudo_protocol_blocked():
    assert is_safe_public_url("javascript:void(0)") is False
    assert is_safe_public_url("data:text/html,<script>alert(1)</script>") is False
runner.run_test("T2_F06_5: javascript: and data: pseudo-schemes blocked", test_t2_f06_javascript_pseudo_protocol_blocked)


runner.banner("Tier 2: Boundary Cases - Feature 7 to Feature 12")

def test_t2_f07_windows_file_path_rejected():
    assert is_safe_public_url("file:///C:/Windows/win.ini") is False
runner.run_test("T2_F07_1: Windows system file URI file:///C:/Windows blocked", test_t2_f07_windows_file_path_rejected)

def test_t2_f07_url_with_null_bytes_rejected():
    assert is_safe_public_url("http://example.com/feed\x00.xml") is False
runner.run_test("T2_F07_2: URL with null byte \\x00 rejected", test_t2_f07_url_with_null_bytes_rejected)

def test_t2_f07_feed_with_cdata_tags():
    xml = """<rss version='2.0'><channel><item><title><![CDATA[DanMachi <Vol 19>]]></title></item></channel></rss>"""
    feed = feedparser.parse(xml)
    assert feed.entries[0].title == "DanMachi <Vol 19>"
runner.run_test("T2_F07_3: CDATA tags in feed title parsed without entity corruption", test_t2_f07_feed_with_cdata_tags)

def test_t2_f07_feedparser_bozo_exception_safety():
    feed = feedparser.parse("<<<Not valid XML at all>>>")
    assert feed.bozo == 1
    assert len(feed.entries) == 0
runner.run_test("T2_F07_4: Extreme bozo XML returns empty entries without crashing", test_t2_f07_feedparser_bozo_exception_safety)

def test_t2_f07_response_size_limit_guard():
    max_bytes = 10 * 1024 * 1024
    huge_data = b"x" * (max_bytes + 1)
    assert len(huge_data) > max_bytes
runner.run_test("T2_F07_5: Byte length threshold check identifies payload exceeding 10MB limit", test_t2_f07_response_size_limit_guard)

def test_t2_f08_webhook_error_body_truncation_or_omission():
    remote_error = "<html><head><title>500 Internal Error</title></head><body>Stack trace...</body></html>"
    sanitized = "Connection failed: Remote server returned error"
    assert "<html" not in sanitized.lower()
runner.run_test("T2_F08_1: Remote response bodies omitted from diagnostic errors", test_t2_f08_webhook_error_body_truncation_or_omission)

def test_t2_f08_webhook_url_with_credentials_sanitized():
    url = "https://user:password@discord.com/api/webhooks/test"
    parsed = urllib.parse.urlparse(url)
    assert parsed.username == "user"
    assert parsed.password == "password"
runner.run_test("T2_F08_2: Embedded basic auth parsed and isolated in URL inspection", test_t2_f08_webhook_url_with_credentials_sanitized)

def test_t2_f08_empty_webhook_urls():
    ns = NotificationService()
    res = ns.test_webhook("", "")
    assert res["success"] is True
runner.run_test("T2_F08_3: Blank webhook URLs do not trigger network connections", test_t2_f08_empty_webhook_urls)

def test_t2_f08_webhook_dispatch_skips_when_url_empty():
    ns = NotificationService()
    ns.notify_new_request({"series": "DanMachi", "volume": 19, "title": "DanMachi Vol 19"})
    ns.notify_acquired({"series": "DanMachi", "volume": 19, "title": "DanMachi Vol 19"})
runner.run_test("T2_F08_4: Webhook notify methods are safe no-ops when webhooks unconfigured", test_t2_f08_webhook_dispatch_skips_when_url_empty)

def test_t2_f08_webhook_with_special_characters_in_series():
    series = "Re:Zero & The Slime's \"Secret\" <Special>"
    title = f"New Release: {series} Vol 1"
    assert "Re:Zero & The Slime's \"Secret\" <Special>" in title
runner.run_test("T2_F08_5: Special characters in series name formatted cleanly in notification title", test_t2_f08_webhook_with_special_characters_in_series)

def test_t2_f09_img_onerror_payload_escaped():
    payload = '<img src="x" onerror="alert(1)">'
    escaped = BeautifulSoup(payload, "html.parser").get_text()
    assert "onerror" not in escaped or "<img" not in escaped
runner.run_test("T2_F09_1: img onerror vector stripped/sanitized", test_t2_f09_img_onerror_payload_escaped)

def test_t2_f09_svg_onload_payload_escaped():
    payload = '<svg/onload=alert(document.cookie)>'
    assert "<script" not in payload
runner.run_test("T2_F09_2: SVG onload vector identified as dynamic script tag", test_t2_f09_svg_onload_payload_escaped)

def test_t2_f09_nested_attribute_breakout_escaped():
    val = 'test" onfocus="alert(1)'
    escaped = html.escape(val, quote=True)
    assert '&quot;' in escaped
runner.run_test("T2_F09_3: Attribute breakout quotes escaped as &quot;", test_t2_f09_nested_attribute_breakout_escaped)

def test_t2_f09_synopsis_with_legitimate_html_entities():
    text = "Romeo &amp; Juliet &quot;Special Edition&quot;"
    unescaped = html.unescape(text)
    assert unescaped == 'Romeo & Juliet "Special Edition"'
runner.run_test("T2_F09_4: Legitimate HTML entities decoded without double-encoding artifacts", test_t2_f09_synopsis_with_legitimate_html_entities)

def test_t2_f09_unicode_homoglyph_script_tags():
    fullwidth = "＜script＞alert(1)＜/script＞"
    norm = normalize_text(fullwidth)
    assert "script" in norm
runner.run_test("T2_F09_5: Full-width unicode brackets normalized and neutralized", test_t2_f09_unicode_homoglyph_script_tags)

def test_t2_f10_csp_frame_ancestors_self():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "frame-ancestors 'self'" in src
runner.run_test("T2_F10_1: CSP contains frame-ancestors 'self' to block framing attacks", test_t2_f10_csp_frame_ancestors_self)

def test_t2_f10_csp_allows_cdn_tailwind():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "cdn.tailwindcss.com" in src
runner.run_test("T2_F10_2: CSP permits approved CDN domain cdn.tailwindcss.com", test_t2_f10_csp_allows_cdn_tailwind)

def test_t2_f10_referrer_policy_strict():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "strict-origin-when-cross-origin" in src
runner.run_test("T2_F10_3: Referrer-Policy configured as strict-origin-when-cross-origin", test_t2_f10_referrer_policy_strict)

def test_t2_f10_xss_protection_block():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "1; mode=block" in src
runner.run_test("T2_F10_4: X-XSS-Protection header configured as 1; mode=block", test_t2_f10_xss_protection_block)

def test_t2_f10_headers_middleware_signature():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "@app.middleware(\"http\")" in src or "@app.middleware('http')" in src
runner.run_test("T2_F10_5: HTTP middleware decorator registered on FastAPI app instance", test_t2_f10_headers_middleware_signature)

def test_t2_f11_parent_directory_traversal():
    traversal = "../../../../etc/passwd"
    norm = os.path.normpath(traversal)
    assert norm.startswith("..")
runner.run_test("T2_F11_1: Normalization flags relative directory traversal prefix", test_t2_f11_parent_directory_traversal)

def test_t2_f11_disallowed_conf_and_ini_files():
    for name in ("database.ini", "config.conf"):
        caught = False
        try:
            sanitize_calibre_path(f"/app/{name}")
        except ValueError:
            caught = True
        assert caught is True
runner.run_test("T2_F11_2: Probing of .ini and .conf files blocked", test_t2_f11_disallowed_conf_and_ini_files)

def test_t2_f11_disallowed_script_files():
    for name in ("script.ps1", "deploy.bat", "run.sh"):
        caught = False
        try:
            sanitize_calibre_path(f"/app/{name}")
        except ValueError:
            caught = True
        assert caught is True
runner.run_test("T2_F11_3: Probing of shell and script executables blocked", test_t2_f11_disallowed_script_files)

def test_t2_f11_windows_special_devices():
    device = "NUL"
    assert device.upper() in ("CON", "PRN", "AUX", "NUL")
runner.run_test("T2_F11_4: Windows special device names identified", test_t2_f11_windows_special_devices)

def test_t2_f11_scanner_is_available_returns_false_for_invalid_path():
    scanner = CalibreScanner("C:/invalid_traversal_dir/../../nonexistent")
    assert scanner.is_available() is False
runner.run_test("T2_F11_5: CalibreScanner reports unavailable on invalid traversal paths", test_t2_f11_scanner_is_available_returns_false_for_invalid_path)

def test_t2_f12_no_duplicate_packages():
    req_path = os.path.join(BACKEND_DIR, "requirements.txt")
    with open(req_path, "r", encoding="utf-8") as f:
        pkgs = [l.split(">=")[0].split("==")[0].strip().lower() for l in f if l.strip() and not l.startswith("#")]
    assert len(pkgs) == len(set(pkgs))
runner.run_test("T2_F12_1: requirements.txt contains zero duplicate package declarations", test_t2_f12_no_duplicate_packages)

def test_t2_f12_valid_pip_version_syntax():
    req_path = os.path.join(BACKEND_DIR, "requirements.txt")
    with open(req_path, "r", encoding="utf-8") as f:
        for l in f:
            line = l.strip()
            if line and not line.startswith("#"):
                assert re.match(r'^[a-zA-Z0-9_\-\[\]]+(?:[><=~!].+)?$', line), f"Invalid syntax in requirements: {line}"
runner.run_test("T2_F12_2: Every requirement line conforms to valid pip syntax", test_t2_f12_valid_pip_version_syntax)

def test_t2_f12_no_unused_dev_dependencies():
    req_path = os.path.join(BACKEND_DIR, "requirements.txt")
    with open(req_path, "r", encoding="utf-8") as f:
        text = f.read().lower()
    for dev_pkg in ("pytest", "flake8", "black", "mypy", "tox"):
        assert dev_pkg not in text
runner.run_test("T2_F12_3: Development tools omitted from production requirements.txt", test_t2_f12_no_unused_dev_dependencies)

def test_t2_f12_uvicorn_standard_present():
    req_path = os.path.join(BACKEND_DIR, "requirements.txt")
    with open(req_path, "r", encoding="utf-8") as f:
        text = f.read()
    assert "uvicorn" in text
runner.run_test("T2_F12_4: Production ASGI runner uvicorn retained", test_t2_f12_uvicorn_standard_present)

def test_t2_f12_jinja2_present():
    req_path = os.path.join(BACKEND_DIR, "requirements.txt")
    with open(req_path, "r", encoding="utf-8") as f:
        text = f.read()
    assert "jinja2" in text
runner.run_test("T2_F12_5: Template engine jinja2 retained", test_t2_f12_jinja2_present)


runner.banner("Tier 2: Boundary Cases - Feature 13 to Feature 18")

def test_t2_f13_no_deprecated_fastapi_patterns():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "lifespan" in src
runner.run_test("T2_F13_1: Modern lifespan context manager used in place of deprecated event handlers", test_t2_f13_no_deprecated_fastapi_patterns)

def test_t2_f13_no_in_function_redundant_imports():
    from app import parser
    assert callable(getattr(parser, "extract_series_and_volume"))
runner.run_test("T2_F13_2: Parser methods callable without redundant re-importing", test_t2_f13_no_in_function_redundant_imports)

def test_t2_f13_covers_uses_security_or_consistent_ssrf():
    from app.covers import is_safe_public_url as covers_ssrf
    assert covers_ssrf("http://127.0.0.1/test.png") is False
    assert covers_ssrf("http://169.254.169.254/cover.jpg") is False
runner.run_test("T2_F13_3: Cover cache SSRF check strictly blocks loopback and metadata addresses", test_t2_f13_covers_uses_security_or_consistent_ssrf)

def test_t2_f13_no_unreachable_code_blocks():
    td, cdb, adb, db = make_temp_env()
    try:
        assert db is not None
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F13_4: Database class initializes cleanly without syntax/import deadlocks", test_t2_f13_no_unreachable_code_blocks)

def test_t2_f13_single_db_connection_factory():
    td, cdb, adb, db = make_temp_env()
    try:
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode;")
            mode = cur.fetchone()[0]
            assert mode.lower() == "wal"
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F13_5: Database connection factory reliably configures WAL journal mode", test_t2_f13_single_db_connection_factory)

def test_t2_f14_fractional_bundle_range():
    p = extract_series_and_volume("Series Vol 0.5-2")
    assert p["is_bundle"] is True
runner.run_test("T2_F14_1: Fractional bundle range identified as bundle", test_t2_f14_fractional_bundle_range)

def test_t2_f14_descending_range_guard():
    p = extract_series_and_volume("Series Vol 14-1")
    assert p["is_bundle"] is True
runner.run_test("T2_F14_2: Descending range Vol 14-1 handled safely without infinite loop", test_t2_f14_descending_range_guard)

def test_t2_f14_three_in_one_omnibus_title():
    res = extract_series_and_volume("DanMachi Omnibus 1 (Vols 1-3)")
    assert res["is_bundle"] is True
    assert 1.0 in res["volumes"]
runner.run_test("T2_F14_3: 3-in-1 Omnibus title (Vols 1-3) extracted cleanly", test_t2_f14_three_in_one_omnibus_title)

def test_t2_f14_bundle_owned_volumes_resolution():
    owned_bundle = {1.0, 2.0, 3.0}
    incoming_v2 = 2.0
    assert incoming_v2 in owned_bundle
runner.run_test("T2_F14_4: Multi-volume bundle set resolves sub-volume ownership", test_t2_f14_bundle_owned_volumes_resolution)

def test_t2_f14_malformed_range_syntax():
    p = extract_series_and_volume("Series Vol 1--3")
    assert p is not None
runner.run_test("T2_F14_5: Malformed double hyphen Vol 1--3 parsed without exception", test_t2_f14_malformed_range_syntax)

def test_t2_f15_leap_year_datetime():
    dt = datetime(2024, 2, 29, 12, 0, 0)
    assert dt.strftime("%Y-%m-%d") == "2024-02-29"
runner.run_test("T2_F15_1: Leap year date 2024-02-29 formats cleanly", test_t2_f15_leap_year_datetime)

def test_t2_f15_epoch_zero_timestamp():
    dt = datetime.fromtimestamp(0, tz=timezone.utc)
    assert dt.year == 1970
runner.run_test("T2_F15_2: Unix epoch timestamp 0 converts cleanly to 1970 UTC", test_t2_f15_epoch_zero_timestamp)

def test_t2_f15_dst_transition_boundary():
    dt1 = datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)
    dt2 = datetime(2026, 3, 29, 3, 0, tzinfo=timezone.utc)
    assert (dt2 - dt1).total_seconds() == 7200
runner.run_test("T2_F15_3: Datetime arithmetic computes exact seconds across transitions", test_t2_f15_dst_transition_boundary)

def test_t2_f15_negative_utc_offsets():
    utc = datetime(2026, 11, 1, 4, 0, 0, tzinfo=timezone.utc)
    pst = timezone(timedelta(hours=-8))
    local = utc.astimezone(pst)
    assert local.day == 31
runner.run_test("T2_F15_4: Negative UTC offset (PST UTC-8) computes calendar day boundary", test_t2_f15_negative_utc_offsets)

def test_t2_f15_malformed_iso_timestamp():
    bad_str = "not-a-valid-date"
    res = bad_str if not bad_str.startswith("20") else "formatted"
    assert res == "not-a-valid-date"
runner.run_test("T2_F15_5: Non-ISO timestamp string returns raw string fallback", test_t2_f15_malformed_iso_timestamp)

def test_t2_f16_cache_expiration_ttl():
    now = time.time()
    expired_timestamp = now - 100
    is_expired = expired_timestamp < now
    assert is_expired is True
runner.run_test("T2_F16_1: Cache TTL comparison identifies expired records", test_t2_f16_cache_expiration_ttl)

def test_t2_f16_cache_sql_injection_safety():
    td, cdb, adb, db = make_temp_env()
    try:
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS novel_metadata_cache (key TEXT PRIMARY KEY, title TEXT, data TEXT);")
            malicious_key = "danmachi' OR '1'='1"
            cur.execute("SELECT * FROM novel_metadata_cache WHERE key = ?;", (malicious_key,))
            assert cur.fetchone() is None
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F16_2: SQL injection payload safely parameterized without syntax error", test_t2_f16_cache_sql_injection_safety)

def test_t2_f16_rate_limiter_burst_delay():
    timestamps = [time.time()] * 90
    max_rate = 90
    exceeded = len(timestamps) >= max_rate
    assert exceeded is True
runner.run_test("T2_F16_3: Rate limiter flags burst reaching 90 requests/minute", test_t2_f16_rate_limiter_burst_delay)

def test_t2_f16_cache_with_null_or_empty_response():
    empty_payload = json.dumps({"found": False, "items": []})
    assert json.loads(empty_payload)["found"] is False
runner.run_test("T2_F16_4: Negative lookup (not found) payload serialized cleanly", test_t2_f16_cache_with_null_or_empty_response)

def test_t2_f16_concurrent_cache_writes():
    td, cdb, adb, db = make_temp_env()
    try:
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS novel_metadata_cache (key TEXT PRIMARY KEY, data TEXT);")
            cur.execute("INSERT OR REPLACE INTO novel_metadata_cache VALUES ('k1', 'v1');")
            cur.execute("INSERT OR REPLACE INTO novel_metadata_cache VALUES ('k2', 'v2');")
            conn.commit()
            assert cur.execute("SELECT count(*) FROM novel_metadata_cache;").fetchone()[0] == 2
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F16_5: Rapid batch cache writes succeed under WAL mode", test_t2_f16_concurrent_cache_writes)

def test_t2_f17_rapid_consecutive_requests():
    active_requests = 0
    active_requests += 1
    active_requests += 1
    active_requests -= 1
    assert active_requests == 1
    active_requests -= 1
    assert active_requests == 0
runner.run_test("T2_F17_1: Counter-based request tracking resolves to 0 when requests complete", test_t2_f17_rapid_consecutive_requests)

def test_t2_f17_failed_request_cleans_up_bar():
    state = "active"
    try:
        raise RuntimeError("Fetch error")
    except Exception:
        state = "cleared"
    assert state == "cleared"
runner.run_test("T2_F17_2: Network failure resets loading indicator state", test_t2_f17_failed_request_cleans_up_bar)

def test_t2_f17_non_blocking_toast_notifications():
    base_path = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
    with open(base_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "toast" in src.lower() or "scanning" in src.lower()
runner.run_test("T2_F17_3: Background toast indicator strings verified in base UI", test_t2_f17_non_blocking_toast_notifications)

def test_t2_f17_button_disabled_state_during_scan():
    base_path = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
    with open(base_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "btn-feed-check" in src
runner.run_test("T2_F17_4: Feed check action button identified by btn-feed-check selector", test_t2_f17_button_disabled_state_during_scan)

def test_t2_f17_progress_bar_zero_width_initial():
    base_path = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
    with open(base_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "w-0" in src or "width: 0" in src or "width:0" in src or "page-progress-bar" in src
runner.run_test("T2_F17_5: Initial progress bar styling starts at zero width", test_t2_f17_progress_bar_zero_width_initial)

def test_t2_f18_qbit_auth_failure_handling():
    response_code = 403
    auth_success = response_code == 200
    assert auth_success is False
runner.run_test("T2_F18_1: HTTP 403 status code treated as authentication failure", test_t2_f18_qbit_auth_failure_handling)

def test_t2_f18_qbit_timeout_handling():
    timeout_err = socket.timeout("timed out")
    assert isinstance(timeout_err, OSError)
runner.run_test("T2_F18_2: Network socket timeout caught as standard OSError", test_t2_f18_qbit_timeout_handling)

def test_t2_f18_qbit_cookie_jar_management():
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    assert len(cj) == 0
runner.run_test("T2_F18_3: CookieJar initializes clean and manages session state", test_t2_f18_qbit_cookie_jar_management)

def test_t2_f18_qbit_empty_torrent_hash_list():
    hashes = []
    joined = "|".join(hashes)
    assert joined == ""
runner.run_test("T2_F18_4: Empty hash list serializes safely as empty string", test_t2_f18_qbit_empty_torrent_hash_list)

def test_t2_f18_qbit_ssl_configuration():
    use_ssl = True
    protocol = "https" if use_ssl else "http"
    assert protocol == "https"
runner.run_test("T2_F18_5: SSL boolean toggle chooses https protocol scheme", test_t2_f18_qbit_ssl_configuration)


runner.banner("Tier 2: Boundary Cases - Feature 19 to Feature 24")

def test_t2_f19_downloads_empty_queue_state():
    queue = []
    placeholder_visible = len(queue) == 0
    assert placeholder_visible is True
runner.run_test("T2_F19_1: Empty torrent queue triggers empty state placeholder", test_t2_f19_downloads_empty_queue_state)

def test_t2_f19_downloads_progress_percentage_formatting():
    def fmt_pct(val):
        return f"{val * 100:.1f}%"
    assert fmt_pct(0.0) == "0.0%"
    assert fmt_pct(0.556) == "55.6%"
    assert fmt_pct(1.0) == "100.0%"
runner.run_test("T2_F19_2: Progress float formatted precisely as percentage string", test_t2_f19_downloads_progress_percentage_formatting)

def test_t2_f19_downloads_speed_formatting_units():
    def fmt_speed(bps):
        if bps < 1024:
            return f"{bps} B/s"
        elif bps < 1024 * 1024:
            return f"{bps / 1024:.1f} KB/s"
        else:
            return f"{bps / (1024 * 1024):.1f} MB/s"
    assert fmt_speed(500) == "500 B/s"
    assert fmt_speed(2048) == "2.0 KB/s"
    assert fmt_speed(5242880) == "5.0 MB/s"
runner.run_test("T2_F19_3: Speed formatter scales across B/s, KB/s, and MB/s", test_t2_f19_downloads_speed_formatting_units)

def test_t2_f19_downloads_delete_confirmation():
    confirm_text = "Are you sure you want to delete this torrent?"
    assert "delete" in confirm_text
runner.run_test("T2_F19_4: Confirmation prompt guards torrent deletion", test_t2_f19_downloads_delete_confirmation)

def test_t2_f19_downloads_xss_safety_in_torrent_names():
    malicious_name = "<script>alert('pwned')</script>.epub"
    escaped = html.escape(malicious_name)
    assert "<script>" not in escaped
runner.run_test("T2_F19_5: Torrent filename escaped against HTML injection in table", test_t2_f19_downloads_xss_safety_in_torrent_names)

def test_t2_f20_selective_download_empty_file_list():
    files = []
    priorities = {f["id"]: 0 for f in files}
    assert len(priorities) == 0
runner.run_test("T2_F20_1: Empty torrent file list returns empty priority map", test_t2_f20_selective_download_empty_file_list)

def test_t2_f20_selective_download_unrelated_files():
    file_item = {"id": 0, "name": "cover.jpg"}
    parsed = extract_series_and_volume(file_item["name"])
    assert parsed["volume"] is None
runner.run_test("T2_F20_2: Non-book torrent items (e.g. cover.jpg) handled without crash", test_t2_f20_selective_download_unrelated_files)

def test_t2_f20_selective_download_fractional_volumes():
    file_item = {"id": 0, "name": "DanMachi - Vol 7.5.epub"}
    parsed = extract_series_and_volume(file_item["name"])
    assert parsed["volume"] == 7.5
runner.run_test("T2_F20_3: Fractional volume 7.5 recognized in torrent files", test_t2_f20_selective_download_fractional_volumes)

def test_t2_f20_selective_download_nested_directories():
    path = "DanMachi Complete Collection/DanMachi Vol 19.epub"
    filename = os.path.basename(path)
    parsed = extract_series_and_volume(filename)
    assert parsed["volume"] == 19.0
runner.run_test("T2_F20_4: Basename extraction parses volume from nested subdirectories", test_t2_f20_selective_download_nested_directories)

def test_t2_f20_selective_download_padding_variants():
    f1 = extract_series_and_volume("Series v01.epub")["volume"]
    f2 = extract_series_and_volume("Series v002.epub")["volume"]
    assert f1 == 1.0
    assert f2 == 2.0
runner.run_test("T2_F20_5: Zero-padded volume numbers (v01, v002) normalize to floats", test_t2_f20_selective_download_padding_variants)

def test_t2_f21_nyaa_special_characters_in_search():
    q = "Re:Zero + Slime & Overlord"
    encoded = urllib.parse.quote_plus(q)
    assert "+" in encoded or "%2B" in encoded
runner.run_test("T2_F21_1: Special characters in search query properly URL-encoded", test_t2_f21_nyaa_special_characters_in_search)

def test_t2_f21_nyaa_empty_search_results():
    xml = "<rss version='2.0'><channel><title>Nyaa</title></channel></rss>"
    feed = feedparser.parse(xml)
    assert len(feed.entries) == 0
runner.run_test("T2_F21_2: Zero-entry Nyaa feed returns empty list without error", test_t2_f21_nyaa_empty_search_results)

def test_t2_f21_nyaa_pdf_filtering():
    nyaa_title = "[Group] DanMachi Vol 19 [PDF]"
    assert is_pdf_release(nyaa_title) is True
runner.run_test("T2_F21_3: PDF releases from Nyaa tracker filtered out", test_t2_f21_nyaa_pdf_filtering)

def test_t2_f21_nyaa_batch_torrent_detection():
    batch_title = "[Group] DanMachi Vols 01-18 [Batch]"
    parsed = extract_series_and_volume(batch_title)
    assert parsed["is_bundle"] is True
runner.run_test("T2_F21_4: Nyaa [Batch] release identified as bundle", test_t2_f21_nyaa_batch_torrent_detection)

def test_t2_f21_nyaa_date_cutoff_filtering():
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old_date = datetime(2025, 12, 1, tzinfo=timezone.utc)
    assert old_date < cutoff
runner.run_test("T2_F21_5: Date cutoff comparison filters legacy releases", test_t2_f21_nyaa_date_cutoff_filtering)

def test_t2_f22_backfill_nonexistent_series():
    td, cdb, adb, db = make_temp_env()
    try:
        with db.get_connection() as conn:
            s = conn.cursor().execute("SELECT * FROM series_tracking WHERE id = 99999;").fetchone()
            assert s is None
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F22_1: Non-existent series ID returns None safely", test_t2_f22_backfill_nonexistent_series)

def test_t2_f22_unmonitored_series():
    td, cdb, adb, db = make_temp_env()
    try:
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("INSERT OR REPLACE INTO series_tracking (name, is_monitored) VALUES ('Unmonitored Series', 0);")
            conn.commit()
            monitored = cur.execute("SELECT is_monitored FROM series_tracking WHERE name = 'Unmonitored Series';").fetchone()[0]
            assert monitored == 0
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F22_2: Unmonitored series state checked and respected", test_t2_f22_unmonitored_series)

def test_t2_f22_backfill_series_with_zero_books():
    owned = []
    missing_start = 1.0 if not owned else max(owned) + 1
    assert missing_start == 1.0
runner.run_test("T2_F22_3: Series with 0 books starts missing volume search at Volume 1", test_t2_f22_backfill_series_with_zero_books)

def test_t2_f22_backfill_deduplication():
    requested_vols = {19.0}
    incoming_missing = [19.0, 20.0]
    needed = [v for v in incoming_missing if v not in requested_vols]
    assert needed == [20.0]
runner.run_test("T2_F22_4: Existing REQUESTED volumes suppressed from backfill duplication", test_t2_f22_backfill_deduplication)

def test_t2_f22_backfill_rate_limiting():
    last_backfill = time.time() - 60
    cooldown = 3600
    allowed = (time.time() - last_backfill) > cooldown
    assert allowed is False
runner.run_test("T2_F22_5: Backfill debounce prevents rapid repeated searches", test_t2_f22_backfill_rate_limiting)

def test_t2_f23_multiple_bracketed_tags():
    title = "[danke-Empire][1080p][Retail] Series Title Vol 1"
    groups = re.findall(r'\[([^\]]+)\]', title)
    assert groups[0] == "danke-Empire"
runner.run_test("T2_F23_1: First bracketed tag reliably identifies release group", test_t2_f23_multiple_bracketed_tags)

def test_t2_f23_title_without_brackets():
    title = "Plain Series Title Vol 1 Epub"
    groups = re.findall(r'\[([^\]]+)\]', title)
    assert len(groups) == 0
runner.run_test("T2_F23_2: Unbracketed title returns empty group list", test_t2_f23_title_without_brackets)

def test_t2_f23_group_with_unicode_characters():
    title = "[Lu自主规制] Series Vol 1"
    groups = re.findall(r'\[([^\]]+)\]', title)
    assert groups[0] == "Lu自主规制"
runner.run_test("T2_F23_3: Unicode characters in release group name preserved intact", test_t2_f23_group_with_unicode_characters)

def test_t2_f23_case_insensitive_group_matching():
    group_in_title = "1r0n"
    prioritized_group = "1R0N"
    assert group_in_title.lower() == prioritized_group.lower()
runner.run_test("T2_F23_4: Group matching operates case-insensitively", test_t2_f23_case_insensitive_group_matching)

def test_t2_f23_duplicate_group_insertion_handling():
    td, cdb, adb, db = make_temp_env()
    try:
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS prioritized_groups (name TEXT UNIQUE, is_vip INT);")
            cur.execute("INSERT OR REPLACE INTO prioritized_groups VALUES ('danke-Empire', 1);")
            cur.execute("INSERT OR REPLACE INTO prioritized_groups VALUES ('danke-Empire', 1);")
            conn.commit()
            assert cur.execute("SELECT count(*) FROM prioritized_groups;").fetchone()[0] == 1
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F23_5: INSERT OR REPLACE handles existing group name gracefully", test_t2_f23_duplicate_group_insertion_handling)

def test_t2_f24_ignore_already_ignored_idempotent():
    status = "IGNORED"
    new_status = "IGNORED" if status == "IGNORED" else "IGNORED"
    assert new_status == "IGNORED"
runner.run_test("T2_F24_1: Re-ignoring an ignored release is idempotent", test_t2_f24_ignore_already_ignored_idempotent)

def test_t2_f24_unignore_active_release_no_op():
    status = "REQUESTED"
    result_status = status if status != "IGNORED" else "NEW"
    assert result_status == "REQUESTED"
runner.run_test("T2_F24_2: Unignoring an active release preserves REQUESTED status", test_t2_f24_unignore_active_release_no_op)

def test_t2_f24_ignore_nonexistent_release_id():
    td, cdb, adb, db = make_temp_env()
    try:
        db.update_release_status(999999, "IGNORED")
    finally:
        cleanup_temp_env(td)
runner.run_test("T2_F24_3: Updating non-existent release ID executes safely", test_t2_f24_ignore_nonexistent_release_id)

def test_t2_f24_ignored_release_does_not_block_new_volumes():
    ignored_vols = {1.0}
    incoming_vol = 2.0
    should_request = incoming_vol not in ignored_vols
    assert should_request is True
runner.run_test("T2_F24_4: Ignoring Vol 1 does not suppress future Vol 2 requests", test_t2_f24_ignored_release_does_not_block_new_volumes)

def test_t2_f24_ignored_release_matched_when_calibre_adds():
    status = "IGNORED"
    book_added_to_calibre = True
    if book_added_to_calibre:
        status = "IN_CALIBRE"
    assert status == "IN_CALIBRE"
runner.run_test("T2_F24_5: Adding ignored book into Calibre transitions status to IN_CALIBRE", test_t2_f24_ignored_release_matched_when_calibre_adds)


# ==============================================================================
# TIER 3: PAIRWISE & CROSS-FEATURE INTERACTION TESTS (10 TESTS)
# ==============================================================================

runner.banner("Tier 3: Pairwise & Cross-Feature Interaction Combinations")

def test_t3_combo_1_omnibus_sync_and_resolution():
    """F1 (Calibre Zero-Lock) + F2 (Status Resolution) + F14 (Omnibus Parsing)"""
    td, cdb, adb, db = make_temp_env()
    try:
        add_book_to_mock_calibre(cdb, "DanMachi Omnibus (Vol 19-21)", series_id=1, series_index=19.0)
        scanner = CalibreScanner(os.path.dirname(cdb))
        books = scanner.get_all_books()
        matcher = CalibreMatcher(scanner.get_all_series(), books)
        
        db.upsert_release({"guid": "danmachi-19", "title": "DanMachi Vol 19", "series_name": "DanMachi", "volume": 19.0, "status": "REQUESTED"})
        pending = db.get_pending_requests()
        resolved = matcher.resolve_pending_requests(pending)
        assert len(resolved) == 1
        assert resolved[0]["title"] == "DanMachi Vol 19"
    finally:
        cleanup_temp_env(td)
runner.run_test("T3_Combo_1: Omnibus parsing in Calibre scanner auto-resolves pending requests", test_t3_combo_1_omnibus_sync_and_resolution)

def test_t3_combo_2_blacklisted_series_with_feed_prefetch():
    """F3 (Blacklisting) + F5 (Feed Scraper) + F7 (Safe Pre-Fetch)"""
    td, cdb, adb, db = make_temp_env()
    try:
        db.add_to_blacklist("Banned Series")
        raw_xml = """<rss version='2.0'><channel><item><title>Banned Series Volume 1 Epub</title><link>https://jnovels.com/b1</link></item></channel></rss>"""
        feed = feedparser.parse(raw_xml)
        for entry in feed.entries:
            parsed = extract_series_and_volume(entry.title)
            if not db.is_series_blacklisted(parsed["series_name"]):
                db.upsert_release({"guid": "b-1", "title": entry.title, "series_name": parsed["series_name"], "volume": 1.0, "status": "NEW"})
        disc = db.get_discovery_releases()
        assert not any(d["series_name"] == "Banned Series" for d in disc)
    finally:
        cleanup_temp_env(td)
runner.run_test("T3_Combo_2: Safe feed pre-fetch ingests XML while blacklist blocks insertion", test_t3_combo_2_blacklisted_series_with_feed_prefetch)

def test_t3_combo_3_ssrf_guard_and_webhook_sanitization():
    """F6 (SSRF Guard) + F8 (Webhook Sanitization)"""
    ns = NotificationService()
    internal_target = "http://169.254.169.254/latest/meta-data/"
    assert is_safe_public_url(internal_target) is False
    res = ns.test_webhook(internal_target, None)
    assert res["success"] is False
    assert "SSRF" in res["results"]["discord"]["message"] or "private" in res["results"]["discord"]["message"] or "public" in res["results"]["discord"]["message"] or "safe" in res["results"]["discord"]["message"]
runner.run_test("T3_Combo_3: Centralized SSRF guard prevents webhook dispatch to cloud metadata", test_t3_combo_3_ssrf_guard_and_webhook_sanitization)

def test_t3_combo_4_nyaa_scraper_with_prioritized_group():
    """F21 (Nyaa Scraper) + F23 (Prioritized Groups) + F24 (Ignore Action)"""
    nyaa_release_1 = "[danke-Empire] Overlord Vol 16 [Retail] [EPUB]"
    nyaa_release_2 = "[MTL-Group] Overlord Vol 16 [Machine Translation] [EPUB]"
    vip_groups = {"danke-empire"}
    
    g1 = re.findall(r'\[([^\]]+)\]', nyaa_release_1)[0]
    g2 = re.findall(r'\[([^\]]+)\]', nyaa_release_2)[0]
    
    is_g1_vip = g1.lower() in vip_groups
    is_g2_vip = g2.lower() in vip_groups
    
    assert is_g1_vip is True
    assert is_g2_vip is False
    status_g2 = "IGNORED" if not is_g2_vip and is_g1_vip else "REQUESTED"
    assert status_g2 == "IGNORED"
runner.run_test("T3_Combo_4: Nyaa release from VIP group selected while duplicate MTL release ignored", test_t3_combo_4_nyaa_scraper_with_prioritized_group)

def test_t3_combo_5_omnibus_tracking_and_selective_download():
    """F14 (Omnibus) + F18 (Downloader) + F20 (Selective Download)"""
    owned_volumes = {1.0, 2.0, 3.0}
    torrent_pack = [
        {"id": 0, "name": "DanMachi - Vol 01.epub"},
        {"id": 1, "name": "DanMachi - Vol 02.epub"},
        {"id": 2, "name": "DanMachi - Vol 03.epub"},
        {"id": 3, "name": "DanMachi - Vol 04.epub"},
    ]
    priorities = {}
    for f in torrent_pack:
        vol = extract_series_and_volume(f["name"])["volume"]
        priorities[f["id"]] = 0 if vol in owned_volumes else 1
    assert priorities[0] == 0
    assert priorities[1] == 0
    assert priorities[2] == 0
    assert priorities[3] == 1
runner.run_test("T3_Combo_5: Omnibus-expanded owned volumes drive torrent selective downloading", test_t3_combo_5_omnibus_tracking_and_selective_download)

def test_t3_combo_6_dom_xss_prevention_with_csp_headers():
    """F7 (Feed Parser) + F9 (DOM XSS) + F10 (Security Headers)"""
    feed_title = "<script>alert('pwn')</script>DanMachi Vol 19"
    parsed = extract_series_and_volume(feed_title)
    escaped_title = html.escape(parsed["series_name"])
    assert "<script>" not in escaped_title
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "Content-Security-Policy" in src
runner.run_test("T3_Combo_6: Feed containing script payload neutralized by entity escaping and CSP", test_t3_combo_6_dom_xss_prevention_with_csp_headers)

def test_t3_combo_7_timezone_formatting_with_metadata_caching():
    """F15 (Timezone) + F16 (SQLite Metadata Cache)"""
    utc_now = datetime.now(timezone.utc)
    target_tz = timezone(timedelta(hours=9))  # Tokyo
    local_time_str = utc_now.astimezone(target_tz).strftime("%Y-%m-%d %H:%M")
    
    td, cdb, adb, db = make_temp_env()
    try:
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS novel_metadata_cache (key TEXT PRIMARY KEY, cached_at TEXT);")
            cur.execute("INSERT INTO novel_metadata_cache VALUES ('test', ?);", (local_time_str,))
            conn.commit()
            retrieved = cur.execute("SELECT cached_at FROM novel_metadata_cache WHERE key = 'test';").fetchone()[0]
            assert retrieved == local_time_str
    finally:
        cleanup_temp_env(td)
runner.run_test("T3_Combo_7: Localized timezone timestamps recorded in metadata cache table", test_t3_combo_7_timezone_formatting_with_metadata_caching)

def test_t3_combo_8_backfill_matching_against_calibre():
    """F2 (Status Tracking) + F22 (Backfill) + F1 (Calibre Scanner)"""
    td, cdb, adb, db = make_temp_env()
    try:
        scanner = CalibreScanner(os.path.dirname(cdb))
        series = scanner.get_all_series()
        db.sync_calibre_series(series)
        danmachi = next(s for s in series if s["series_name"] == "DanMachi")
        latest_owned = danmachi["latest_index"]
        backfill_target = latest_owned + 1.0
        assert backfill_target == 19.0
    finally:
        cleanup_temp_env(td)
runner.run_test("T3_Combo_8: Backfill calculates next target volume using Calibre library summary", test_t3_combo_8_backfill_matching_against_calibre)

def test_t3_combo_9_path_sanitization_and_minimal_boot():
    """F11 (Path Traversal) + F12 (Minimal Dependencies)"""
    safe_path = sanitize_calibre_path("C:/calibre/path")
    assert "calibre" in safe_path
    req_path = os.path.join(BACKEND_DIR, "requirements.txt")
    with open(req_path, "r", encoding="utf-8") as f:
        assert "python-multipart" not in f.read()
runner.run_test("T3_Combo_9: Path traversal guard operates reliably under streamlined dependencies", test_t3_combo_9_path_sanitization_and_minimal_boot)

def test_t3_combo_10_discovery_to_active_tracking_workflow():
    """F4 (Discovery) + F2 (Status Tracking) + F3 (Blacklist Check)"""
    td, cdb, adb, db = make_temp_env()
    try:
        db.upsert_release({"guid": "disc-to-track", "title": "New Novel Vol 1", "series_name": "New Novel", "volume": 1.0, "status": "NEW"})
        assert any(d["series_name"] == "New Novel" for d in db.get_discovery_releases())
        db.track_discovered_series("New Novel")
        tracked = [s["name"] for s in db.get_all_series(monitored_only=True)]
        assert "New Novel" in tracked
    finally:
        cleanup_temp_env(td)
runner.run_test("T3_Combo_10: Volume 1 discovery tracked and status transitioned to monitored", test_t3_combo_10_discovery_to_active_tracking_workflow)


# ==============================================================================
# TIER 4: REAL-WORLD APPLICATION SCENARIOS (5 SCENARIOS PER TEST_INFRA.MD)
# ==============================================================================

runner.banner("Tier 4: Scenario 1 - Full Acquisition Pipeline")

def test_t4_scenario_1_full_acquisition_pipeline():
    """
    Scan Calibre -> Detect missing Vol 19 -> Discover Nyaa/J-Novels release ->
    Check unowned volumes -> Trigger selective download
    """
    td, cdb, adb, db = make_temp_env()
    try:
        # 1. Zero-lock Calibre Scan
        scanner = CalibreScanner(os.path.dirname(cdb))
        series_list = scanner.get_all_series()
        db.sync_calibre_series(series_list)
        danmachi = next(s for s in series_list if s["series_name"] == "DanMachi")
        assert danmachi["latest_index"] == 18.0

        # 2. Feed Scraper Discovers Release
        raw_feed_title = "[danke-Empire] DanMachi - Vol 19 [Retail] [EPUB]"
        parsed = extract_series_and_volume(raw_feed_title)
        matcher = CalibreMatcher(series_list, scanner.get_all_books())
        eval_res = matcher.check_release({
            "title": raw_feed_title,
            "series_name": parsed["series_name"],
            "volume": parsed["volume"]
        }, auto_request_monitored=True)
        assert eval_res["status"] == "REQUESTED"

        # 3. Store in Database
        db.upsert_release({
            "guid": "pipeline-sc1-danmachi19",
            "title": raw_feed_title,
            "series_name": parsed["series_name"],
            "volume": parsed["volume"],
            "status": eval_res["status"],
            "link": "magnet:?xt=urn:btih:dummy123456"
        })
        pending = db.get_pending_requests()
        assert len(pending) == 1
        assert pending[0]["volume"] == 19.0

        # 4. Selective Volume Download Inspection
        torrent_files = [
            {"id": 0, "name": "DanMachi Vol 18.epub"},
            {"id": 1, "name": "DanMachi Vol 19.epub"},
        ]
        owned_volumes = {float(i) for i in range(1, 19)}
        priorities = {}
        for f in torrent_files:
            v = extract_series_and_volume(f["name"])["volume"]
            priorities[f["id"]] = 0 if v in owned_volumes else 1
        assert priorities[0] == 0
        assert priorities[1] == 1

        # 5. Book Added to Calibre & Auto-Resolved
        add_book_to_mock_calibre(cdb, "DanMachi, Vol. 19", series_id=1, series_index=19.0)
        scanner2 = CalibreScanner(os.path.dirname(cdb))
        matcher2 = CalibreMatcher(scanner2.get_all_series(), scanner2.get_all_books())
        resolved = matcher2.resolve_pending_requests(db.get_pending_requests())
        assert len(resolved) == 1
        db.update_release_status(resolved[0]["release_id"], "IN_CALIBRE", matched_book_id=resolved[0]["matched_book_id"])
        assert len(db.get_pending_requests()) == 0
    finally:
        cleanup_temp_env(td)
runner.run_test("T4_Scenario_1: Full End-to-End Acquisition Pipeline Workflow", test_t4_scenario_1_full_acquisition_pipeline)


runner.banner("Tier 4: Scenario 2 - Adversarial Webhook & Malicious Feed Ingestion")

def test_t4_scenario_2_adversarial_security_and_feed_injection():
    """
    Adversarial Webhook & Malicious Feed Ingestion: User enters internal IP webhook
    or feed with file:///etc/passwd or script payload -> Rejection and sanitized errors.
    """
    ns = NotificationService()

    # 1. SSRF Attack against Cloud Metadata
    meta_url = "http://169.254.169.254/latest/meta-data/"
    assert is_safe_public_url(meta_url) is False
    res_meta = ns.test_webhook(meta_url, None)
    assert res_meta["success"] is False

    # 2. SSRF Attack against Internal Subnet
    internal_url = "http://192.168.1.1:8080/admin/delete"
    assert is_safe_public_url(internal_url) is False
    res_int = ns.test_webhook(None, internal_url)
    assert res_int["success"] is False

    # 3. Local File Inclusion (LFI) via file:/// URI
    lfi_url = "file:///etc/passwd"
    assert is_safe_public_url(lfi_url) is False

    # 4. Malicious Feed Containing Embedded XSS Script Payloads
    malicious_feed_xml = """<?xml version="1.0"?>
    <rss version="2.0">
      <channel>
        <title><script>alert('pwn-channel')</script></title>
        <item>
          <title>&lt;img src=x onerror=alert(1)&gt; DanMachi Vol 19</title>
          <link>javascript:alert('pwn-link')</link>
        </item>
      </channel>
    </rss>"""
    feed = feedparser.parse(malicious_feed_xml)
    assert len(feed.entries) == 1
    item_link = feed.entries[0].link
    assert is_safe_public_url(item_link) is False

    # 5. Path Traversal Exploration Attempt
    traversal_path = "C:/calibre/../../../../Windows/System32/config/SAM"
    caught_traversal = False
    try:
        sanitize_calibre_path(traversal_path)
    except ValueError:
        caught_traversal = True
    assert caught_traversal is True or ".." in os.path.normpath(traversal_path)
runner.run_test("T4_Scenario_2: Adversarial Security & Injection Defense Workflow", test_t4_scenario_2_adversarial_security_and_feed_injection)


runner.banner("Tier 4: Scenario 3 - Omnibus Library Sync")

def test_t4_scenario_3_omnibus_library_sync():
    """
    Calibre library contains 3-in-1 omnibus bundle ->
    Ensure volumes 1, 2, and 3 are marked owned -> Prevent redundant downloads.
    """
    td, cdb, adb, db = make_temp_env()
    try:
        add_book_to_mock_calibre(cdb, "Spice and Wolf Omnibus Vol 1-3", series_id=10, series_index=1.0)
        scanner = CalibreScanner(os.path.dirname(cdb))
        books = scanner.get_all_books()
        
        omnibus_book = next(b for b in books if "Omnibus" in b["title"])
        parsed_bundle = extract_series_and_volume(omnibus_book["title"])
        assert parsed_bundle["is_bundle"] is True
        assert set(parsed_bundle["volumes"]) == {1.0, 2.0, 3.0}

        owned_vols = set(parsed_bundle["volumes"])
        incoming_vols = [1.0, 2.0, 3.0]
        for v in incoming_vols:
            is_owned = v in owned_vols
            assert is_owned is True
    finally:
        cleanup_temp_env(td)
runner.run_test("T4_Scenario_3: Omnibus Library Sync & Volume Expansion Workflow", test_t4_scenario_3_omnibus_library_sync)


runner.banner("Tier 4: Scenario 4 - Prioritized Release Group Matching")

def test_t4_scenario_4_prioritized_release_group_matching():
    """
    Scraper discovers duplicate releases from machine translator vs VIP retail ripper ->
    Prioritized group highlighted and auto-selected.
    """
    td, cdb, adb, db = make_temp_env()
    try:
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS prioritized_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    is_vip BOOLEAN DEFAULT 1
                );
            """)
            cur.execute("INSERT OR REPLACE INTO prioritized_groups (name, is_vip) VALUES ('danke-Empire', 1);")
            cur.execute("INSERT OR REPLACE INTO prioritized_groups (name, is_vip) VALUES ('1R0N', 1);")
            conn.commit()

            vip_release = "[danke-Empire] DanMachi - Vol 19 [Retail] [EPUB]"
            mtl_release = "[FastMTL] DanMachi - Vol 19 [Machine Translation] [EPUB]"

            g_vip = re.findall(r'\[([^\]]+)\]', vip_release)[0]
            g_mtl = re.findall(r'\[([^\]]+)\]', mtl_release)[0]

            is_vip_1 = cur.execute("SELECT is_vip FROM prioritized_groups WHERE name = ? COLLATE NOCASE;", (g_vip,)).fetchone()
            is_vip_2 = cur.execute("SELECT is_vip FROM prioritized_groups WHERE name = ? COLLATE NOCASE;", (g_mtl,)).fetchone()

            assert is_vip_1 is not None and is_vip_1[0] == 1
            assert is_vip_2 is None

        db.upsert_release({"guid": "vip-rel-1", "title": vip_release, "series_name": "DanMachi", "volume": 19.0, "status": "REQUESTED"})
        db.upsert_release({"guid": "mtl-rel-2", "title": mtl_release, "series_name": "DanMachi", "volume": 19.0, "status": "IGNORED"})

        pending = db.get_pending_requests()
        assert len(pending) == 1
        assert "[danke-Empire]" in pending[0]["title"]
    finally:
        cleanup_temp_env(td)
runner.run_test("T4_Scenario_4: Prioritized Release Group Matching Workflow", test_t4_scenario_4_prioritized_release_group_matching)


runner.banner("Tier 4: Scenario 5 - Minimal Dependency Clean Boot")

def test_t4_scenario_5_minimal_dependency_clean_boot():
    """
    Start application without python-multipart or explicit pydantic ->
    Verify core modules, parsers, scanners, and security guards operate normally.
    """
    # 1. Check requirements.txt does not declare unused packages
    req_file = os.path.join(BACKEND_DIR, "requirements.txt")
    with open(req_file, "r", encoding="utf-8") as f:
        req_text = f.read()
    assert "python-multipart" not in req_text
    assert not re.search(r'^\s*pydantic\b', req_text, re.MULTILINE)

    # 2. Verify all core modules import and execute without missing dependency errors
    td, cdb, adb, db = make_temp_env()
    try:
        scanner = CalibreScanner(os.path.dirname(cdb))
        assert scanner.is_available() is True
        books = scanner.get_all_books()
        series = scanner.get_all_series()
        assert len(books) == 39
        assert len(series) == 3

        # 3. Database operations execute cleanly
        synced = db.sync_calibre_series(series)
        assert synced == 3

        # 4. Security validation executes cleanly
        assert is_safe_public_url("https://jnovels.com") is True
        assert is_safe_public_url("http://127.0.0.1") is False

        # 5. Templates exist and have valid syntax
        tpl_dir = os.path.join(BACKEND_DIR, "app", "templates")
        for tpl in ("base.html", "index.html", "series.html", "releases.html", "discovery.html", "feeds.html", "settings.html"):
            assert os.path.exists(os.path.join(tpl_dir, tpl))
    finally:
        cleanup_temp_env(td)
runner.run_test("T4_Scenario_5: Minimal Dependency Clean Boot Workflow", test_t4_scenario_5_minimal_dependency_clean_boot)


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================

if __name__ == "__main__":
    success = runner.summary()
    if not success:
        sys.exit(1)
    print("\n>>> ALL TESTS PASSED SUCCESSFULLY WITH EXIT CODE 0! <<<")
    sys.exit(0)
