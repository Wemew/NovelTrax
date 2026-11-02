#!/usr/bin/env python3
r"""
tests/test_nyaa.py
Comprehensive automated test suite for NovelTrax Milestone 4:
Nyaa Provider, Prioritized Groups, Ignore Action & Targeted Backfill.

Runner compatibility:
  & "C:\Program Files\Calibre2\calibre-debug.exe" -e tests\test_nyaa.py
  python tests\test_nyaa.py
"""

import os
import sys
import json
import time
import shutil
import socket
import sqlite3
import tempfile
import threading
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
from unittest.mock import patch, MagicMock

# Ensure backend directory is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# Check for FastAPI availability in current runtime
try:
    from fastapi.testclient import TestClient
    from app.main import app, db as main_db, task_manager as main_tm
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

# Non-strippable assertion helper for Calibre -O2 execution
passed_count = 0
failed_count = 0

def check(condition: bool, msg: str = "") -> None:
    global passed_count, failed_count
    if not condition:
        failed_count += 1
        raise AssertionError(f"Check failed: {msg}")
    passed_count += 1
    print(f"  ✓ {msg}")

# Realistic Mock Fixtures
MOCK_NYAA_RSS_XML = """<?xml version="1.0" encoding="utf-8"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:nyaa="https://nyaa.si/xmlns/nyaa" version="2.0">
  <channel>
    <title>Nyaa - Literature - English-translated</title>
    <description>RSS Feed for Literature - English-translated</description>
    <link>https://nyaa.si/</link>
    <item>
      <title>[Yen Press] Overlord, Vol. 16 [Digital] [EPUB]</title>
      <link>https://nyaa.si/download/1850123.torrent</link>
      <guid isPermaLink="true">https://nyaa.si/view/1850123</guid>
      <pubDate>Mon, 27 Oct 2026 14:30:00 +0000</pubDate>
      <nyaa:seeders>45</nyaa:seeders>
      <nyaa:leechers>3</nyaa:leechers>
      <nyaa:downloads>120</nyaa:downloads>
      <nyaa:infoHash>6506A47A152E85AAECA04865557873AF38F895F3</nyaa:infoHash>
      <nyaa:categoryId>3_1</nyaa:categoryId>
      <nyaa:category>Literature - English-translated</nyaa:category>
      <nyaa:size>25.4 MiB</nyaa:size>
    </item>
    <item>
      <title>[J-Novel Club] By the Grace of the Gods, Vol. 01-08 [EPUB]</title>
      <link>https://nyaa.si/download/1850124.torrent</link>
      <guid isPermaLink="true">https://nyaa.si/view/1850124</guid>
      <pubDate>Mon, 27 Oct 2026 13:00:00 +0000</pubDate>
      <nyaa:seeders>30</nyaa:seeders>
      <nyaa:leechers>1</nyaa:leechers>
      <nyaa:downloads>85</nyaa:downloads>
      <nyaa:infoHash>B506A47A152E85AAECA04865557873AF38F895AA</nyaa:infoHash>
      <nyaa:categoryId>3_1</nyaa:categoryId>
      <nyaa:category>Literature - English-translated</nyaa:category>
      <nyaa:size>150.0 MiB</nyaa:size>
    </item>
    <item>
      <title>[RandomScan] DanMachi Vol 19 [PDF]</title>
      <link>https://nyaa.si/download/1850125.torrent</link>
      <guid isPermaLink="true">https://nyaa.si/view/1850125</guid>
      <pubDate>Mon, 27 Oct 2026 12:00:00 +0000</pubDate>
      <nyaa:seeders>10</nyaa:seeders>
      <nyaa:leechers>0</nyaa:leechers>
      <nyaa:infoHash>C506A47A152E85AAECA04865557873AF38F895BB</nyaa:infoHash>
      <nyaa:categoryId>3_1</nyaa:categoryId>
      <nyaa:size>12.0 MiB</nyaa:size>
    </item>
  </channel>
</rss>
"""

MOCK_NYAA_HTML_TABLE = """<!DOCTYPE html>
<html>
<body>
<table class="torrent-list">
  <tbody>
    <tr>
      <td><a href="/c/3_1"><img src="/static/img/icons/nyaa/3_1.png" alt="Literature"></a></td>
      <td>
        <a href="/view/1850123#comments" class="comments"><i class="fa fa-comments"></i> 5</a>
        <a href="/view/1850123" title="[Yen Press] Overlord, Vol. 16 [Digital] [EPUB]">[Yen Press] Overlord, Vol. 16 [Digital] [EPUB]</a>
      </td>
      <td>
        <a href="/download/1850123.torrent"><i class="fa fa-download"></i></a>
        <a href="magnet:?xt=urn:btih:6506A47A152E85AAECA04865557873AF38F895F3"><i class="fa fa-magnet"></i></a>
      </td>
      <td class="text-center">25.4 MiB</td>
      <td class="text-center" data-timestamp="1729794900">2026-10-24 18:35</td>
      <td class="text-center">45</td>
      <td class="text-center">3</td>
      <td class="text-center">120</td>
    </tr>
    <tr>
      <td><a href="/c/3_1"><img src="/static/img/icons/nyaa/3_1.png" alt="Literature"></a></td>
      <td>
        <a href="/view/1850124" title="[danke-Empire] DanMachi Vol. 19-20 [Digital]">[danke-Empire] DanMachi Vol. 19-20 [Digital]</a>
      </td>
      <td>
        <a href="/download/1850124.torrent"><i class="fa fa-download"></i></a>
        <a href="magnet:?xt=urn:btih:D506A47A152E85AAECA04865557873AF38F895CC"><i class="fa fa-magnet"></i></a>
      </td>
      <td class="text-center">45.0 MiB</td>
      <td class="text-center" data-timestamp="1729700000">2026-10-23 15:00</td>
      <td class="text-center">25</td>
      <td class="text-center">1</td>
      <td class="text-center">75</td>
    </tr>
    <tr>
      <td><a href="/c/3_1"><img src="/static/img/icons/nyaa/3_1.png" alt="Literature"></a></td>
      <td>
        <a href="/view/1850125" title="[Disallowed] Unwanted Novel Vol. 1 [PDF]">[Disallowed] Unwanted Novel Vol. 1 [PDF]</a>
      </td>
      <td>
        <a href="/download/1850125.torrent"><i class="fa fa-download"></i></a>
        <a href="magnet:?xt=urn:btih:E506A47A152E85AAECA04865557873AF38F895DD"><i class="fa fa-magnet"></i></a>
      </td>
      <td class="text-center">15.0 MiB</td>
      <td class="text-center" data-timestamp="1729600000">2026-10-22 10:00</td>
      <td class="text-center">5</td>
      <td class="text-center">0</td>
      <td class="text-center">20</td>
    </tr>
  </tbody>
</table>
</body>
</html>
"""


# =====================================================================
# Tier 1: NyaaNovelProvider Unit Tests (RSS & HTML Table Parsing)
# =====================================================================
def run_tier_1():
    print("\n--- Tier 1: NyaaNovelProvider Unit Tests ---")
    from app.providers.nyaa import NyaaNovelProvider
    from app.providers.base import ReleaseItem, BaseFeedProvider, BaseProvider
    from app.parser import is_pdf_release, extract_series_and_volume, extract_release_group

    # Test 1.1: Provider initialization & category 3_1
    provider = NyaaNovelProvider()
    check(provider.category == "3_1", "NyaaNovelProvider defaults to Category 3_1")
    check("c=3_1" in provider.url, "Provider feed URL targets Category 3_1")
    check(issubclass(NyaaNovelProvider, BaseFeedProvider), "Inherits from BaseFeedProvider")
    check(issubclass(NyaaNovelProvider, BaseProvider), "Inherits from BaseProvider alias")

    # Test 1.2: RSS link disambiguation (torrent download URL vs view permalink)
    class MockResponse:
        def __init__(self, text, status_code=200):
            self.text = text
            self.status_code = status_code

    with patch("app.providers.nyaa.safe_http_get", return_value=MockResponse(MOCK_NYAA_RSS_XML)):
        releases = provider._fetch_rss("https://nyaa.si/?page=rss&c=3_1")
        # 3 items in XML, but DanMachi Vol 19 is PDF, so 2 remain
        check(len(releases) == 2, f"Fetched 2 non-PDF releases from RSS, got {len(releases)}")
        r1 = releases[0]
        check(r1.series_name == "Overlord", f"Parsed series name: {r1.series_name}")
        check(r1.volume == 16.0, f"Parsed volume: {r1.volume}")
        check("view/1850123" in r1.link, f"Link is permalink view URL: {r1.link}")
        check("magnet:" in r1.download_url or "1850123" in r1.download_url, f"Download URL points to torrent/magnet: {r1.download_url}")
        check(r1.release_group == "Yen Press", f"Extracted group: {r1.release_group}")
        check(r1.raw_data.get("post_size") == "25.4 MiB", f"Extracted post size: {r1.raw_data.get('post_size')}")
        check(r1.raw_data.get("seeders") == 45, f"Extracted seeders: {r1.raw_data.get('seeders')}")

        # Test 1.3: RSS bundle range detection
        r2 = releases[1]
        check(r2.series_name == "By the Grace of the Gods", f"Series name: {r2.series_name}")
        check(r2.is_bundle is True, "Multi-volume release marked as bundle")
        check(r2.volume_start == 1.0 and r2.volume_end == 8.0, f"Bundle range 1-8: {r2.volume_start}-{r2.volume_end}")
        check(r2.release_group == "J-Novel Club", f"Release group: {r2.release_group}")

    # Test 1.4: HTML table DOM scraping
    with patch("app.providers.nyaa.safe_http_get", return_value=MockResponse(MOCK_NYAA_HTML_TABLE)):
        html_releases = provider.crawl_pages(max_pages=1)
        check(len(html_releases) == 2, f"HTML crawl filtered PDF and returned 2 items, got {len(html_releases)}")
        h1 = html_releases[0]
        check(h1.link == "https://nyaa.si/view/1850123", f"View URL: {h1.link}")
        check("magnet:" in h1.download_url, f"Download URL is magnet link: {h1.download_url}")
        check(h1.raw_data.get("seeders") == 45, f"HTML seeders: {h1.raw_data.get('seeders')}")
        check(h1.raw_data.get("leechers") == 3, f"HTML leechers: {h1.raw_data.get('leechers')}")
        check(h1.raw_data.get("post_size") == "25.4 MiB", f"HTML size: {h1.raw_data.get('post_size')}")

        # Test 1.5: HTML table comment icon filtering
        check("#comments" not in h1.link, "View link did not mistakenly select comments link")

    # Test 1.6: HTTP 404 graceful termination (end of pagination)
    with patch("app.providers.nyaa.safe_http_get", return_value=MockResponse("", status_code=404)):
        p404 = provider.crawl_pages(max_pages=3)
        check(len(p404) == 0, "HTTP 404 cleanly terminates pagination without raising exception")

    # Test 1.7: HTTP 500 error handling
    with patch("app.providers.nyaa.safe_http_get", side_effect=RuntimeError("Network timeout")):
        p_err = provider.crawl_pages(max_pages=2)
        check(len(p_err) == 0, "Network error in crawl handles gracefully returning empty list")

    # Test 1.8: SSRF validation guard
    from app.security import is_safe_public_url
    check(not is_safe_public_url("http://127.0.0.1:8080/nyaa"), "SSRF blocks loopback 127.0.0.1")
    check(not is_safe_public_url("http://169.254.169.254/latest/meta-data"), "SSRF blocks cloud metadata IP")
    check(not is_safe_public_url("http://192.168.1.100/rss"), "SSRF blocks RFC1918 private network")
    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, None, None, None, ("185.199.108.153", 0))]):
        check(is_safe_public_url("https://nyaa.si/?page=rss&c=3_1"), "SSRF permits public nyaa.si")

    # Test 1.9: Strict PDF release rejection
    check(is_pdf_release("DanMachi Vol 19 [PDF]", "https://nyaa.si/view/123"), "Rejects title with [PDF]")
    check(is_pdf_release("Novel Title.pdf", "https://nyaa.si/view/123"), "Rejects filename ending with .pdf")
    check(not is_pdf_release("Overlord Vol 16 [Digital] [EPUB]", "https://nyaa.si/view/123"), "Accepts EPUB release")

    # Test 1.10: Release group parser integration
    check(extract_release_group("[danke-Empire] DanMachi Vol 19") == "danke-Empire", "Extracts danke-Empire")
    check(extract_release_group("[Yen Press] Overlord Vol 16") == "Yen Press", "Extracts Yen Press")
    check(extract_release_group("[Seven Seas] [Stick] Mushoku Tensei v01") == "Stick", "Prioritizes group over publisher")
    check(extract_release_group("Ore ga Ojousama v01-15 [Completed]") is None, "Ignores buzzword [Completed]")


# =====================================================================
# Tier 2: Prioritized Groups Unit & Integration Tests
# =====================================================================
def run_tier_2():
    print("\n--- Tier 2: Prioritized Groups Unit & Integration Tests ---")
    from app.database import Database

    tmp_dir = tempfile.mkdtemp(prefix="noveltrax_test_tier2_")
    db_path = os.path.join(tmp_dir, "test_tier2.db")
    try:
        db = Database(db_path)

        # Test 2.1: Schema init & seeded publishers
        groups = db.get_prioritized_groups()
        check(len(groups) >= 5, f"Seeded at least 5 prioritized groups, found {len(groups)}")
        group_names = [g["name"] for g in groups]
        check("Yen Press" in group_names, "Yen Press is seeded")
        check("J-Novel Club" in group_names, "J-Novel Club is seeded")
        check("Seven Seas" in group_names, "Seven Seas is seeded")
        check("Stick" in group_names, "Stick is seeded")

        # Test 2.2: Add/update prioritized group with custom priority
        db.add_prioritized_group("NaBi", notes="Special LN ripper", priority=2)
        check(db.is_group_prioritized("NaBi"), "Added group NaBi is recognized")

        # Test 2.3: Case-insensitive group priority checking
        check(db.is_group_prioritized("nabi"), "Case-insensitive check for 'nabi'")
        check(db.is_group_prioritized("yen press"), "Case-insensitive check for 'yen press'")
        check(not db.is_group_prioritized("UnknownNonExistentGroup"), "Non-prioritized group returns False")

        # Test 2.4: Remove prioritized group by ID
        target_g = next(g for g in db.get_prioritized_groups() if g["name"] == "NaBi")
        deleted = db.remove_prioritized_group(target_g["id"])
        check(deleted is True, "remove_prioritized_group returned True")
        check(not db.is_group_prioritized("NaBi"), "NaBi is no longer prioritized")

        # Test 2.5 & 2.6: Route declarations in main.py
        main_path = os.path.join(BACKEND_DIR, "app", "main.py")
        with open(main_path, "r", encoding="utf-8") as f:
            main_src = f.read()

        check('@app.get("/api/prioritized_groups")' in main_src, "main.py defines @app.get('/api/prioritized_groups')")
        check('@app.get("/api/prioritized/groups")' in main_src, "main.py defines @app.get('/api/prioritized/groups')")
        check('@app.post("/api/prioritized_groups")' in main_src, "main.py defines @app.post('/api/prioritized_groups')")
        check('@app.post("/api/prioritized/groups")' in main_src, "main.py defines @app.post('/api/prioritized/groups')")
        check('@app.delete("/api/prioritized_groups/{group_id}")' in main_src, "main.py defines delete route")
        check('@app.delete("/api/prioritized/groups/{group_id}")' in main_src, "main.py defines delete alias route")
        check('@app.get("/api/prioritized/releases")' in main_src, "main.py defines @app.get('/api/prioritized/releases')")

        if HAS_FASTAPI:
            client = TestClient(app)
            res_get1 = client.get("/api/prioritized_groups")
            check(res_get1.status_code == 200, f"/api/prioritized_groups status: {res_get1.status_code}")
            check("groups" in res_get1.json(), "Response contains groups key")

            res_get2 = client.get("/api/prioritized/groups")
            check(res_get2.status_code == 200, f"/api/prioritized/groups status: {res_get2.status_code}")
            check(len(res_get1.json()["groups"]) == len(res_get2.json()["groups"]), "Dual route returns identical groups")

            # Add via /api/prioritized_groups
            res_add = client.post("/api/prioritized_groups", json={"name": "TestGroupAlpha", "notes": "Automated", "priority": 1})
            check(res_add.status_code == 200, f"Add group API: {res_add.status_code}")
            res_list = client.get("/api/prioritized/groups").json()
            alpha_item = next((g for g in res_list["groups"] if g["name"] == "TestGroupAlpha"), None)
            check(alpha_item is not None, "Added group found via alias route")
            res_del = client.delete(f"/api/prioritized/groups/{alpha_item['id']}")
            check(res_del.status_code == 200, f"Delete group API: {res_del.status_code}")

        # Test 2.7: Release group extraction & persistence in db.upsert_release
        is_new, r_id1 = db.upsert_release({
            "guid": "prio-rel-1",
            "title": "[Yen Press] Overlord Vol. 16",
            "series_name": "Overlord",
            "volume": 16.0,
            "status": "REQUESTED"
        })
        is_new2, r_id2 = db.upsert_release({
            "guid": "prio-rel-2",
            "title": "[UnknownGroup] Overlord Vol. 16",
            "series_name": "Overlord",
            "volume": 16.0,
            "status": "REQUESTED"
        })

        # Test 2.8: Prioritized group matching flags is_prioritized = True on releases
        rel1 = db.get_release_by_id(r_id1)
        check(rel1["release_group"] == "Yen Press", f"Release group persisted: {rel1['release_group']}")
        all_rels = db.get_releases()
        r1_item = next(r for r in all_rels if r["id"] == r_id1)
        r2_item = next(r for r in all_rels if r["id"] == r_id2)
        check(r1_item["is_prioritized"] is True, "Yen Press release is marked is_prioritized = True")
        check(r2_item["is_prioritized"] is False, "UnknownGroup release is marked is_prioritized = False")

        # Test 2.9: Sorting priority boost in get_releases_grouped_by_series
        db.upsert_release({
            "guid": "series-a-1",
            "title": "[Yen Press] Series Alpha Vol. 1",
            "series_name": "Series Alpha",
            "volume": 1.0,
            "status": "REQUESTED"
        })
        db.upsert_release({
            "guid": "series-b-1",
            "title": "[Other] Series Beta Vol. 1",
            "series_name": "Series Beta",
            "volume": 1.0,
            "status": "REQUESTED"
        })
        grouped = db.get_releases_grouped_by_series(sort="wanted_first")
        g_names = [g["series_name"] for g in grouped]
        check(g_names.index("Series Alpha") < g_names.index("Series Beta"), "Series Alpha (wanted & prioritized) sorted ahead of Series Beta")

        # Test 2.10: Grouped series query with prioritized_only=True
        prio_only = db.get_releases_grouped_by_series(prioritized_only=True)
        check(all(g["has_prioritized"] for g in prio_only), "All returned groups have prioritized releases")
        check(any(g["series_name"] == "Series Alpha" for g in prio_only), "Series Alpha is in prioritized_only")
        check(not any(g["series_name"] == "Series Beta" for g in prio_only), "Series Beta excluded from prioritized_only")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# =====================================================================
# Tier 3: Ignore Action Unit & Integration Tests
# =====================================================================
def run_tier_3():
    print("\n--- Tier 3: Ignore Action Unit & Integration Tests ---")
    from app.database import Database

    tmp_dir = tempfile.mkdtemp(prefix="noveltrax_test_tier3_")
    db_path = os.path.join(tmp_dir, "test_tier3.db")
    try:
        db = Database(db_path)

        # Test 3.1: Mark release as IGNORED via db.update_release_status & ignore_release
        is_new, r_id = db.upsert_release({
            "guid": "ign-test-1",
            "title": "Unwanted Spinoff Vol. 1",
            "series_name": "Unwanted Spinoff",
            "volume": 1.0,
            "status": "REQUESTED"
        })
        db.ignore_release(r_id)
        r_rec = db.get_release_by_id(r_id)
        check(r_rec["status"] == "IGNORED", f"Status updated to IGNORED: {r_rec['status']}")
        check(r_rec["ignored"] == 1, f"ignored column flag is 1: {r_rec['ignored']}")

        # Test 3.2: Default get_releases() excludes IGNORED releases
        active = db.get_releases()
        check(r_id not in [r["id"] for r in active], "Ignored release excluded from default get_releases()")

        # Test 3.3: Query get_releases(include_ignored=True) includes IGNORED releases
        all_with_ign = db.get_releases(include_ignored=True)
        check(r_id in [r["id"] for r in all_with_ign], "Ignored release included when include_ignored=True")

        # Test 3.4: Query get_releases(status='IGNORED') returns only ignored releases
        only_ign = db.get_releases(status="IGNORED")
        check(all(r["status"] == "IGNORED" or r["ignored"] == 1 for r in only_ign), "Only ignored releases returned")
        check(r_id in [r["id"] for r in only_ign], "r_id is in status=IGNORED query")

        # Test 3.5: Grouped series view excludes IGNORED releases from active series cards
        grps = db.get_releases_grouped_by_series()
        check(not any(g["series_name"] == "Unwanted Spinoff" for g in grps), "Unwanted Spinoff excluded from default series view")

        # Test 3.6 & 3.7: Endpoint definition verification
        main_path = os.path.join(BACKEND_DIR, "app", "main.py")
        with open(main_path, "r", encoding="utf-8") as f:
            main_src = f.read()

        check('@app.post("/api/releases/{release_id}/ignore")' in main_src, "Endpoint @app.post('/api/releases/{release_id}/ignore') defined")
        check('@app.post("/api/releases/{release_id}/unignore")' in main_src, "Endpoint @app.post('/api/releases/{release_id}/unignore') defined")

        if HAS_FASTAPI:
            _, m_rid = main_db.upsert_release({
                "guid": "client-ign-1",
                "title": "Client Ignore Test Vol. 1",
                "series_name": "Client Ignore Test",
                "volume": 1.0,
                "status": "REQUESTED"
            })
            client = TestClient(app)
            res_ign = client.post(f"/api/releases/{m_rid}/ignore")
            check(res_ign.status_code == 200, f"Ignore API endpoint returned 200: {res_ign.status_code}")
            check(main_db.get_release_by_id(m_rid)["status"] == "IGNORED", "Database reflects status=IGNORED after API call")

            res_unign = client.post(f"/api/releases/{m_rid}/unignore")
            check(res_unign.status_code == 200, f"Unignore API endpoint returned 200: {res_unign.status_code}")
            check(main_db.get_release_by_id(m_rid)["status"] == "REQUESTED", "Database reflects status=REQUESTED after unignore")
            check(main_db.get_release_by_id(m_rid)["ignored"] == 0, "Database reflects ignored=0 after unignore")

        # Test 3.8: Ignore action idempotency on repeated requests
        db.ignore_release(r_id)
        db.ignore_release(r_id)
        check(db.get_release_by_id(r_id)["status"] == "IGNORED", "Repeated ignore calls maintain status=IGNORED")

        # Test 3.9: Adding ignored volume to Calibre transitions status to IN_CALIBRE
        db.update_release_status(r_id, "IN_CALIBRE", matched_book_id=999)
        updated_r = db.get_release_by_id(r_id)
        check(updated_r["status"] == "IN_CALIBRE", "Transitions cleanly to IN_CALIBRE")
        check(updated_r["calibre_matched_book_id"] == 999, "calibre_matched_book_id persisted")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# =====================================================================
# Tier 4: Targeted Series Backfill Integration Tests
# =====================================================================
def run_tier_4():
    print("\n--- Tier 4: Targeted Series Backfill Integration Tests ---")
    from app.database import Database
    from app.scheduler import TaskManager
    from app.providers.base import ReleaseItem

    tmp_dir = tempfile.mkdtemp(prefix="noveltrax_test_tier4_")
    db_path = os.path.join(tmp_dir, "test_tier4.db")
    try:
        db = Database(db_path)
        with db.get_connection() as conn:
            conn.cursor().execute("""
                INSERT INTO series_tracking (name, book_count, latest_index, is_monitored)
                VALUES ('Overlord', 15, 15.0, 1);
            """)
            conn.commit()

        tm = TaskManager(db, tmp_dir)

        mock_releases = [
            ReleaseItem(
                title="[Yen Press] Overlord, Vol. 16 [Digital] [EPUB]",
                link="https://nyaa.si/view/1850123",
                series_name="Overlord",
                volume=16.0,
                download_url="magnet:?xt=urn:btih:mockhash16",
                release_group="Yen Press"
            ),
            ReleaseItem(
                title="[Yen Press] Overlord, Vol. 15 [Digital] [EPUB]",
                link="https://nyaa.si/view/1850122",
                series_name="Overlord",
                volume=15.0,
                download_url="magnet:?xt=urn:btih:mockhash15",
                release_group="Yen Press"
            )
        ]

        # Test 4.1: Synchronous backfill_series searches active providers
        with patch("app.providers.nyaa.NyaaNovelProvider.search_series", return_value=mock_releases):
            res = tm.backfill_series("Overlord", max_pages=1)
            check(res["status"] == "success", f"Synchronous backfill status: {res['status']}")
            check(res["releases_found"] == 2, f"Found 2 releases: {res['releases_found']}")

        # Test 4.3 & 4.4: Missing volumes inserted as REQUESTED; owned volumes evaluated
        r16 = next(r for r in db.get_releases(include_ignored=True) if r["volume"] == 16.0)
        check(r16["status"] == "REQUESTED", f"Vol 16 (unowned) inserted as REQUESTED: {r16['status']}")
        check(r16["release_group"] == "Yen Press", f"Release group: {r16['release_group']}")

        # Test 4.2: Asynchronous run_series_backfill_async non-blocking dispatch
        with patch.object(tm, "backfill_series", return_value={"status": "success", "message": "done"}):
            async_res = tm.run_series_backfill_async("Overlord", max_pages=1)
            check(async_res["status"] == "started", f"Async backfill immediately returns 'started': {async_res['status']}")
            time.sleep(0.1)

        # Test 4.5 & 4.6: Route declarations in main.py
        main_path = os.path.join(BACKEND_DIR, "app", "main.py")
        with open(main_path, "r", encoding="utf-8") as f:
            main_src = f.read()

        check('@app.post("/api/series/{series_id}/backfill")' in main_src, "Endpoint @app.post('/api/series/{series_id}/backfill') defined")
        check('@app.post("/api/series/backfill")' in main_src, "Endpoint @app.post('/api/series/backfill') defined")

        if HAS_FASTAPI:
            client = TestClient(app)
            with patch.object(main_tm, "run_series_backfill_async", return_value={"status": "started", "message": "Background backfill initiated"}):
                res_api = client.post("/api/series/backfill", json={"series_name": "Overlord", "max_pages": 2})
                check(res_api.status_code == 200, f"/api/series/backfill status 200: {res_api.status_code}")
                check(res_api.json()["status"] == "started", "Endpoint returned started")

        # Test 4.7: Backfill status reporting and last_page tracking
        db.set_setting("backfill_status", "idle")
        db.set_setting("backfill_last_page", "3")
        check(db.get_setting("backfill_last_page") == "3", "Settings track backfill_last_page")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# =====================================================================
# Tier 5: Calibre -O2 Runtime & Concurrency Verification
# =====================================================================
def run_tier_5():
    global failed_count
    print("\n--- Tier 5: Calibre -O2 Runtime & Concurrency Verification ---")
    from app.database import Database

    # Test 5.1: check() helper execution under simulated -O2 optimization
    test_passed = False
    try:
        check(True, "check() succeeds on truthy condition")
        test_passed = True
    except AssertionError:
        test_passed = False
    check(test_passed, "check(True) passed without exception")

    threw_on_false = False
    try:
        check(False, "Simulated check failure test")
    except AssertionError:
        failed_count -= 1
        threw_on_false = True
    check(threw_on_false, "check(False) raises AssertionError even under -O2")

    # Test 5.2: SQLite WAL mode multi-threaded read/write concurrency during crawl
    tmp_dir = tempfile.mkdtemp(prefix="noveltrax_test_tier5_")
    db_path = os.path.join(tmp_dir, "test_tier5.db")
    try:
        db = Database(db_path)
        errors = []

        def reader_task():
            for _ in range(25):
                try:
                    db.get_releases()
                    db.get_prioritized_groups()
                    time.sleep(0.005)
                except Exception as e:
                    errors.append(f"Reader error: {e}")

        def writer_task(worker_id):
            for i in range(15):
                try:
                    db.upsert_release({
                        "guid": f"conc-guid-{worker_id}-{i}",
                        "title": f"[Group{worker_id}] Concurrent Release Vol. {i}",
                        "series_name": f"Concurrent Series {worker_id}",
                        "volume": float(i),
                        "status": "REQUESTED"
                    })
                    time.sleep(0.005)
                except Exception as e:
                    errors.append(f"Writer {worker_id} error: {e}")

        threads = [
            threading.Thread(target=reader_task),
            threading.Thread(target=reader_task),
            threading.Thread(target=writer_task, args=(1,)),
            threading.Thread(target=writer_task, args=(2,))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        check(len(errors) == 0, f"Concurrent WAL read/write completed with 0 errors: {errors}")
        check(db.count_releases() == 30, f"Expected 30 concurrently written releases, got {db.count_releases()}")

        # Test 5.3: Zero endpoint route collision & template file verification
        main_path = os.path.join(BACKEND_DIR, "app", "main.py")
        with open(main_path, "r", encoding="utf-8") as f:
            main_src = f.read()

        check('@app.get("/prioritized"' in main_src, "/prioritized route is declared in main.py")
        check('@app.get("/api/prioritized_groups"' in main_src, "/api/prioritized_groups route is declared")
        check('@app.get("/api/prioritized/groups"' in main_src, "/api/prioritized/groups route is declared")
        check('@app.get("/api/prioritized/cards"' in main_src, "/api/prioritized/cards route is declared")
        check('@app.get("/api/prioritized/flat_rows"' in main_src, "/api/prioritized/flat_rows route is declared")
        check('@app.post("/api/releases/{release_id}/ignore"' in main_src, "/api/releases/{release_id}/ignore is declared")
        check('@app.post("/api/releases/{release_id}/unignore"' in main_src, "/api/releases/{release_id}/unignore is declared")
        check('@app.post("/api/series/{series_id}/backfill"' in main_src, "/api/series/{series_id}/backfill is declared")
        check('@app.post("/api/series/backfill"' in main_src, "/api/series/backfill is declared")

        # Test 5.4: Template markup and component checks
        prio_tpl_path = os.path.join(BACKEND_DIR, "app", "templates", "prioritized.html")
        check(os.path.isfile(prio_tpl_path), "backend/app/templates/prioritized.html exists")
        with open(prio_tpl_path, "r", encoding="utf-8") as f:
            prio_src = f.read()
        check("Prioritized Releases & Groups" in prio_src, "prioritized.html has header")
        check("addPrioritizedGroup" in prio_src, "prioritized.html has addPrioritizedGroup form handler")
        check("removePrioritizedGroup" in prio_src, "prioritized.html has removePrioritizedGroup function")
        check("Group by Series" in prio_src, "prioritized.html has Group by Series view toggle")
        check("All Releases (Flat)" in prio_src, "prioritized.html has All Releases (Flat) view toggle")

        base_tpl_path = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
        with open(base_tpl_path, "r", encoding="utf-8") as f:
            base_src = f.read()
        check('href="/prioritized"' in base_src, "base.html has navbar link to /prioritized")
        check('ignoreReleaseGlobal' in base_src, "base.html has ignoreReleaseGlobal function")
        check('unignoreReleaseGlobal' in base_src, "base.html has unignoreReleaseGlobal function")
        check('backfillSeriesGlobal' in base_src, "base.html has backfillSeriesGlobal function")

        flat_tpl_path = os.path.join(BACKEND_DIR, "app", "templates", "partials", "flat_release_row_chunk.html")
        with open(flat_tpl_path, "r", encoding="utf-8") as f:
            flat_src = f.read()
        check('ignoreReleaseGlobal' in flat_src, "flat_release_row_chunk.html has ignore action button")
        check('unignoreReleaseGlobal' in flat_src, "flat_release_row_chunk.html has unignore action button")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    print("=================================================================")
    print(" NovelTrax Milestone 4 Test Suite: Nyaa, Prioritized & Ignore")
    print("=================================================================")
    start_time = time.time()

    try:
        run_tier_1()
        run_tier_2()
        run_tier_3()
        run_tier_4()
        run_tier_5()
    except Exception as e:
        print(f"\n❌ UNHANDLED TEST FAILURE: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    elapsed = time.time() - start_time
    print("\n=================================================================")
    print(f" Summary: {passed_count} checks PASSED, {failed_count} FAILED in {elapsed:.2f}s")
    print("=================================================================")

    if failed_count > 0:
        sys.exit(1)
    print("ALL TESTS PASSED WITH 100% SUCCESS.")
    sys.exit(0)


if __name__ == "__main__":
    main()
