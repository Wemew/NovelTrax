"""
NovelTrax Milestone 4 Iteration 3: Empirical Integration & Regression Challenger Harness
Focus: End-to-end integration, Defect A (title preservation/mangling), Defect B (composite buzzwords),
       and zero-regression verification across the full NovelTrax stack.
Runtime: Calibre python interpreter (calibre-debug.exe -e)
"""

import sys
import os
import re
import json
import sqlite3
import tempfile
import shutil
import unittest
from unittest.mock import MagicMock
from typing import Dict, Any, List, Set

# Ensure repo root and backend are on sys.path
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND_DIR = os.path.join(REPO_ROOT, "backend")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from backend.app.database import Database
from backend.app.parser import (
    extract_release_group,
    is_valid_group_candidate,
    is_pdf_release,
    BUZZWORDS,
)
from backend.app.downloader.qbit import (
    QBitClient,
    parse_torrent_filename_volumes,
)
from backend.app.providers.metadata import NovelMetadataProvider
from backend.app.calibre.scanner import CalibreScanner, parse_calibre_volumes_and_series
from backend.app.security import is_safe_public_url, validate_downloader_destination


def check(condition: bool, msg: str = "Assertion failed"):
    """Runtime check resilient to Python -O / -O2 optimization."""
    if not condition:
        raise AssertionError(msg)


class TestMilestone4Iteration3Integration(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="noveltrax_m4it3_challenger_")
        self.db_path = os.path.join(self.test_dir, "app_data.db")
        self.calibre_dir = os.path.join(self.test_dir, "calibre")
        os.makedirs(self.calibre_dir, exist_ok=True)
        self.calibre_db_path = os.path.join(self.calibre_dir, "metadata.db")

        # Initialize mock Calibre DB
        self._init_mock_calibre()

        # Initialize app DB
        self.db = Database(db_path=self.db_path)
        self.db.init_db()

    def tearDown(self):
        if hasattr(self, "db") and self.db:
            try:
                self.db.close()
            except Exception:
                pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _init_mock_calibre(self):
        conn = sqlite3.connect(self.calibre_db_path)
        cur = conn.cursor()
        cur.executescript("""
            CREATE TABLE books (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                series_index REAL DEFAULT 1.0,
                path TEXT DEFAULT ''
            );
            CREATE TABLE series (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE books_series_link (
                id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL,
                series INTEGER NOT NULL
            );
            CREATE TABLE authors (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE books_authors_link (
                id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL,
                author INTEGER NOT NULL
            );
        """)

        # Seed series
        series_data = [
            (1, "Overlord"),
            (2, "Mushoku Tensei"),
            (3, "Mob Psycho 100"),
            (4, "86 - Eighty Six"),
            (5, "DanMachi"),
        ]
        cur.executemany("INSERT INTO series (id, name) VALUES (?, ?)", series_data)

        # Seed books with single and omnibus titles
        books = [
            # Overlord Vol 1-3 (Omnibus)
            (1, "Overlord Vol 1-3", 1.0, 1),
            # Mushoku Tensei Vol 1, 2, and 3 (Oxford list)
            (2, "Mushoku Tensei Vol 1, 2, and 3", 1.0, 2),
            # Mob Psycho 100 - Vol 1 (Numbered series)
            (3, "Mob Psycho 100 - Vol 1", 1.0, 3),
            # 86 - 1-4 (Bare range numbered series)
            (4, "86 - Eighty Six - Vol 1-4", 1.0, 4),
            # DanMachi Books 1 to 18
            (5, "DanMachi Vol 1-18", 1.0, 5),
        ]
        for b_id, title, s_idx, s_id in books:
            cur.execute("INSERT INTO books (id, title, series_index) VALUES (?, ?, ?)", (b_id, title, s_idx))
            cur.execute("INSERT INTO books_series_link (book, series) VALUES (?, ?)", (b_id, s_id))

        conn.commit()
        conn.close()

    # =========================================================================
    # SUITE 1: END-TO-END ACQUISITION & INTEGRATION PIPELINE
    # =========================================================================
    def test_full_e2e_acquisition_pipeline(self):
        print("\n--- SUITE 1: End-to-End Acquisition & Integration Pipeline ---")

        # 1. Calibre Library Scan
        scanner = CalibreScanner(self.calibre_db_path)
        check(scanner.is_available(), "Calibre scanner must be available")
        vmap = scanner.get_series_volumes_map()

        check(vmap.get("overlord") == {1.0, 2.0, 3.0}, "Overlord volumes map failed")
        check(vmap.get("mushoku tensei") == {1.0, 2.0, 3.0}, "Mushoku Tensei volumes map failed")
        check(vmap.get("mob psycho 100") == {1.0}, "Mob Psycho 100 volumes map failed")
        check(vmap.get("86 - eighty six") == {1.0, 2.0, 3.0, 4.0}, "86 volumes map failed")
        check(len(vmap.get("danmachi", set())) == 18, "DanMachi volumes map failed")
        print("  [PASS] 1.1 Calibre omnibus & volume map extraction")

        # 2. Add custom Prioritized Group
        check(self.db.add_prioritized_group("LuTX", priority=4), "Failed to add LuTX")
        check(self.db.is_group_prioritized("lutx"), "LuTX should be case-insensitively prioritized")
        check(self.db.is_group_prioritized("J-Novel Club"), "J-Novel Club should be seeded as prioritized")
        check(self.db.is_group_prioritized("Yen Press"), "Yen Press should be seeded as prioritized")
        print("  [PASS] 1.2 Prioritized groups management")

        # 3. Ingest candidate releases
        releases = [
            {
                "guid": "rel-guid-1",
                "title": "[J-Novel Club] By the Grace of the Gods Vol. 1 [Digital] [EPUB]",
                "link": "https://nyaa.si/view/1001",
                "download_url": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
                "series_name": "By the Grace of the Gods",
                "volume": 1.0,
                "status": "REQUESTED",
            },
            {
                "guid": "rel-guid-2",
                "title": "[Blu-Ray / 1080p] Overlord Vol 16 [Dual-Audio]",
                "link": "https://nyaa.si/view/1002",
                "download_url": "magnet:?xt=urn:btih:1123456789abcdef0123456789abcdef01234567",
                "series_name": "Overlord",
                "volume": 16.0,
                "status": "REQUESTED",
            },
            {
                "guid": "rel-guid-3",
                "title": "[Yen Press] Overlord Vol 16 [Web-DL 2160p HDR] [EPUB]",
                "link": "https://nyaa.si/view/1003",
                "download_url": "magnet:?xt=urn:btih:2123456789abcdef0123456789abcdef01234567",
                "series_name": "Overlord",
                "volume": 16.0,
                "status": "REQUESTED",
            },
            {
                "guid": "rel-guid-4",
                "title": "[Stick] Mushoku Tensei Vol 20 [Audiobook - 64kbps]",
                "link": "https://nyaa.si/view/1004",
                "download_url": "magnet:?xt=urn:btih:3123456789abcdef0123456789abcdef01234567",
                "series_name": "Mushoku Tensei",
                "volume": 20.0,
                "status": "REQUESTED",
            },
            {
                "guid": "rel-guid-5",
                "title": "[LuTX] DanMachi Vol 19 [Digital]",
                "link": "https://nyaa.si/view/1005",
                "download_url": "magnet:?xt=urn:btih:4123456789abcdef0123456789abcdef01234567",
                "series_name": "DanMachi",
                "volume": 19.0,
                "status": "REQUESTED",
            },
        ]

        inserted_ids = []
        for r in releases:
            is_new, r_id = self.db.upsert_release(r)
            check(r_id is not None and r_id > 0, f"Failed to upsert release: {r['title']}")
            inserted_ids.append(r_id)

        # 4. Verify Raw Title Preservation and Group Extraction in DB
        db_r1 = self.db.get_release_by_id(inserted_ids[0])
        check(
            db_r1["title"] == "[J-Novel Club] By the Grace of the Gods Vol. 1 [Digital] [EPUB]",
            f"Title mangled! Got: {db_r1['title']}"
        )
        check(db_r1["release_group"] == "J-Novel Club", f"Expected group J-Novel Club, got: {db_r1['release_group']}")
        check(self.db.is_group_prioritized(db_r1["release_group"]), "J-Novel Club must be prioritized")

        db_r2 = self.db.get_release_by_id(inserted_ids[1])
        check(
            db_r2["release_group"] == "" or db_r2["release_group"] is None,
            f"Composite tag [Blu-Ray / 1080p] was not rejected! Got group: {db_r2['release_group']}"
        )
        check(not self.db.is_group_prioritized(db_r2["release_group"] or ""), "Non-group release must not be prioritized")

        db_r3 = self.db.get_release_by_id(inserted_ids[2])
        check(db_r3["release_group"] == "Yen Press", f"Expected Yen Press, got: {db_r3['release_group']}")
        check(self.db.is_group_prioritized(db_r3["release_group"]), "Yen Press release must be prioritized")

        db_r4 = self.db.get_release_by_id(inserted_ids[3])
        check(db_r4["release_group"] == "Stick", f"Expected Stick, got: {db_r4['release_group']}")
        check(self.db.is_group_prioritized(db_r4["release_group"]), "Stick release must be prioritized")

        db_r5 = self.db.get_release_by_id(inserted_ids[4])
        check(db_r5["release_group"] == "LuTX", f"Expected LuTX, got: {db_r5['release_group']}")
        check(self.db.is_group_prioritized(db_r5["release_group"]), "LuTX release must be prioritized")

        # Also verify is_prioritized in get_releases()
        all_initial = self.db.get_releases(include_ignored=True)
        r_map = {r["id"]: r for r in all_initial}
        check(r_map[inserted_ids[0]]["is_prioritized"] is True, "Release 1 is_prioritized must be True")
        check(r_map[inserted_ids[1]]["is_prioritized"] is False, "Release 2 is_prioritized must be False")
        check(r_map[inserted_ids[2]]["is_prioritized"] is True, "Release 3 is_prioritized must be True")
        print("  [PASS] 1.3 DB raw title preservation and release group extraction")

        # 5. Test 1-Click Ignore & Unignore State Machine
        check(self.db.ignore_release(inserted_ids[2]), "ignore_release failed")
        db_r3_ign = self.db.get_release_by_id(inserted_ids[2])
        check(db_r3_ign["status"] == "IGNORED", "Status must be IGNORED")
        check(db_r3_ign["ignored"] == 1, "ignored column must be 1")

        # Excluded from default get_releases()
        active_releases = self.db.get_releases()
        active_ids = [r["id"] for r in active_releases]
        check(inserted_ids[2] not in active_ids, "Ignored release must be excluded from default query")

        # Included in include_ignored=True
        all_releases = self.db.get_releases(include_ignored=True)
        all_ids = [r["id"] for r in all_releases]
        check(inserted_ids[2] in all_ids, "Ignored release must appear when include_ignored=True")

        # Unignore
        check(self.db.unignore_release(inserted_ids[2]), "unignore_release failed")
        db_r3_unign = self.db.get_release_by_id(inserted_ids[2])
        check(db_r3_unign["status"] == "REQUESTED", "Status must be restored to REQUESTED")
        check(db_r3_unign["ignored"] == 0, "ignored column must be reset to 0")
        print("  [PASS] 1.4 1-Click Ignore / Unignore state machine")

        # 6. Test Grouped Series Priority Sorting
        grouped = self.db.get_releases_grouped_by_series()
        # Series with wanted prioritized releases have has_prioritized=True
        for g in grouped:
            check(g["has_prioritized"] is True, f"Series {g['series_name']} must have has_prioritized=True")
        print("  [PASS] 1.5 Series priority boost grouping")

        # 7. Test Downloader Selective Download Filter
        torrent_files = [
            {"index": 0, "name": "DanMachi Vol 1-18.epub", "size": 100000000},
            {"index": 1, "name": "DanMachi Vol 19.epub", "size": 10000000},
            {"index": 2, "name": "DanMachi Vol 20.epub", "size": 10000000},
            {"index": 3, "name": "Cover.jpg", "size": 500000},
        ]
        user_owned = set(vmap.get("danmachi", set()))  # Volumes 1.0 to 18.0
        client = QBitClient()
        client.get_torrent_files = MagicMock(return_value=torrent_files)
        client.set_file_priorities = MagicMock(return_value=True)

        action = client.apply_selective_volume_download(
            torrent_hash="mockhash",
            owned_volumes=user_owned,
            series_name="DanMachi",
        )
        check(action.get("status") == "filtered", f"Expected filtered, got: {action.get('status')}")
        check(0 in action.get("skipped_indices", []), "Owned Vol 1-18 must be skipped")
        check(1 in action.get("kept_indices", []), "Missing Vol 19 must be kept")
        check(2 in action.get("kept_indices", []), "Missing Vol 20 must be kept")
        check(3 in action.get("skipped_indices", []), "Ancillary cover must be skipped")
        print("  [PASS] 1.6 Downloader selective download filtering")

    # =========================================================================
    # SUITE 2: ADVERSARIAL BUZZWORD & COMPOSITE TAG REJECTION MATRIX
    # =========================================================================
    def test_composite_buzzword_rejection_matrix(self):
        print("\n--- SUITE 2: Adversarial Buzzword & Composite Tag Rejection Matrix ---")

        adversarial_noise_tags = [
            # Technical video tags
            "[Blu-Ray / 1080p]",
            "[Web-DL 2160p HDR]",
            "[MKV / H.264]",
            "[MP4 / x265 / 10bit]",
            "[2160p UHD HDR10]",
            "[Dual-Audio / 10bit / HEVC]",
            "[BD 1080p AVC AAC]",
            "[Multi-Audio / Multi-Subs]",
            "[Remux / 2160p / HDR]",
            "[720p / AAC / x264]",
            "[x265 / 10bit / Dual-Audio]",
            "[WebDL - 1080p]",
            "[ISO / Remux]",
            "[FLV / WMV]",
            "[TS / M4V]",
            # Technical audio tags
            "[Audiobook - 64kbps]",
            "[MP3 / 320kbps]",
            "[MP3 / 320k]",
            "[FLAC / 24bit]",
            "[Lossless / FLAC]",
            "[TrueHD / Atmos]",
            "[Dual-Audio / AAC]",
            # Formatting & retail noise
            "[Digital / Retail / EPUB]",
            "[Bonus + Artbook]",
            "[PDF + EPUB]",
            "[v01-15]",
            "[2021-2024]",
            "[Uncensored / Remastered]",
            "[English / Digital / Retail]",
            "[Complete / Batch]",
        ]

        for tag in adversarial_noise_tags:
            cand = tag.strip("[]() ")
            is_valid = is_valid_group_candidate(cand)
            check(not is_valid, f"Vulnerability! Noise tag was accepted as group: '{tag}'")
            extracted = extract_release_group(f"{tag} Series Title Vol 1")
            check(extracted is None, f"Vulnerability! Noise tag was extracted as group: '{extracted}' from '{tag}'")

        print(f"  [PASS] 2.1 Successfully rejected {len(adversarial_noise_tags)} composite noise tags")

        # Legitimate publisher and fan groups must be preserved
        genuine_groups = [
            "J-Novel Club",
            "Square Enix Manga & Books",
            "One Peace Books",
            "Yen Press",
            "Seven Seas",
            "danke-Empire",
            "LuTX",
            "Stick",
            "Airship",
            "Kodansha",
            "Viz Media",
            "NaBi",
            "a-sloth",
            "1R0N",
            "Kobo",
            "BookWalker",
            "Lu自主规制",
        ]

        for group in genuine_groups:
            check(is_valid_group_candidate(group), f"Regression! Genuine group rejected: '{group}'")
            # In bracketed title
            title = f"[{group}] Sample LN Title Vol. 1 [Digital]"
            ext = extract_release_group(title)
            check(ext == group, f"Regression! Group extraction failed for '{title}': got '{ext}', expected '{group}'")

        print(f"  [PASS] 2.2 Successfully preserved all {len(genuine_groups)} genuine publisher & ripper groups")

    # =========================================================================
    # SUITE 3: TITLE PRESERVATION & clean_novel_title INTEGRITY
    # =========================================================================
    def test_title_preservation_and_clean_novel_title(self):
        print("\n--- SUITE 3: Title Preservation & clean_novel_title Integrity ---")

        # Test clean_novel_title strictly preserves publisher brackets
        cases = [
            (
                "[J-Novel Club] Ascendance of a Bookworm Vol. 1",
                "[J-Novel Club] Ascendance of a Bookworm Vol. 1",
            ),
            (
                "[J-Novel Club] By the Grace of the Gods Vol. 1 (Light Novel)",
                "[J-Novel Club] By the Grace of the Gods Vol. 1",
            ),
            (
                "Overlord [LN] Vol 1",
                "Overlord Vol 1",
            ),
            (
                "Mushoku Tensei (Novel) Vol 2",
                "Mushoku Tensei Vol 2",
            ),
            (
                "Slime [Web Novel] Vol 3",
                "Slime Vol 3",
            ),
            (
                "DanMachi (WN) Vol 4",
                "DanMachi Vol 4",
            ),
            (
                "[Yen Press] Overlord, Vol. 16 (Light Novel) [Digital]",
                "[Yen Press] Overlord, Vol. 16 [Digital]",
            ),
        ]

        for raw, expected in cases:
            cleaned = NovelMetadataProvider.clean_novel_title(raw)
            check(
                cleaned == expected,
                f"clean_novel_title error!\n  Raw:      {raw}\n  Expected: {expected}\n  Got:      {cleaned}"
            )

        print("  [PASS] 3.1 clean_novel_title does not mangle publisher tags or brackets")

        # Verify Database.upsert_release does not modify title
        raw_test_title = "[J-Novel Club] Ascendance of a Bookworm Vol. 1 [Digital]"
        is_new, rel_id = self.db.upsert_release({
            "guid": "guid-test-bookworm-1",
            "title": raw_test_title,
            "link": "https://nyaa.si/view/9999",
            "series_name": "Ascendance of a Bookworm",
            "volume": 1.0,
            "status": "REQUESTED",
        })
        stored = self.db.get_release_by_id(rel_id)
        check(stored["title"] == raw_test_title, f"upsert_release mutated title! Got: {stored['title']}")
        check(stored["release_group"] == "J-Novel Club", f"Expected group 'J-Novel Club', got: {stored['release_group']}")
        print("  [PASS] 3.2 upsert_release preserves raw title verbatim and extracts J-Novel Club")

    # =========================================================================
    # SUITE 4: SELF-HEALING DATABASE MIGRATION
    # =========================================================================
    def test_self_healing_database_migration(self):
        print("\n--- SUITE 4: Self-Healing Database Migration ---")

        # Inject legacy mangled records directly into SQLite
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO releases (guid, title, series_name, volume, status, release_group, ignored, link)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "guid-legacy-1",
            "J- Club] Ascendance of a Bookworm Vol. 1",
            "Ascendance of a Bookworm",
            1.0,
            "REQUESTED",
            "",
            0,
            "https://test.link/1"
        ))
        cur.execute("""
            INSERT INTO releases (guid, title, series_name, volume, status, release_group, ignored, link)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "guid-legacy-2",
            "Yen Press] Overlord Vol. 16",
            "Overlord",
            16.0,
            "REQUESTED",
            "",
            0,
            "https://test.link/2"
        ))
        conn.commit()
        conn.close()

        # Re-run init_db() to trigger self-healing migrations
        self.db.init_db()

        healed_1 = self.db.get_releases(search="Ascendance of a Bookworm")
        check(len(healed_1) >= 1, "Healed record 1 not found")
        check(
            healed_1[0]["title"].startswith("[J-Novel Club]"),
            f"Record 1 not healed! Title: {healed_1[0]['title']}"
        )
        check(
            healed_1[0]["release_group"] == "J-Novel Club",
            f"Release group not backfilled! Group: {healed_1[0]['release_group']}"
        )

        healed_2 = self.db.get_releases(search="Overlord")
        found_h2 = [r for r in healed_2 if "Overlord Vol. 16" in r["title"]]
        check(len(found_h2) >= 1, "Healed record 2 not found")
        check(
            found_h2[0]["title"].startswith("[Yen Press]"),
            f"Record 2 missing bracket! Title: {found_h2[0]['title']}"
        )
        print("  [PASS] 4.1 Self-healing migration successfully restored mangled titles and groups")

    # =========================================================================
    # SUITE 5: DUAL-ROUTE API & TEMPLATE INTEGRITY
    # =========================================================================
    def test_api_routes_and_templates(self):
        print("\n--- SUITE 5: Dual-Route API & Template Integrity ---")

        main_path = os.path.join(BACKEND_DIR, "app", "main.py")
        with open(main_path, "r", encoding="utf-8") as f:
            main_src = f.read()

        required_routes = [
            '@app.get("/prioritized"',
            '@app.get("/api/prioritized_groups"',
            '@app.get("/api/prioritized/groups"',
            '@app.get("/api/prioritized/releases"',
            '@app.post("/api/releases/{release_id}/ignore"',
            '@app.post("/api/releases/{release_id}/unignore"',
            '@app.post("/api/series/{series_id}/backfill"',
            '@app.post("/api/series/backfill"',
            '@app.get("/api/series_releases"',
            '@app.get("/api/releases/grouped"',
        ]

        for r in required_routes:
            check(r in main_src, f"Required route definition missing in main.py: {r}")

        print(f"  [PASS] 5.1 All {len(required_routes)} required M4 routes verified in main.py")

        # Check template existence and content
        prioritized_html = os.path.join(BACKEND_DIR, "app", "templates", "prioritized.html")
        base_html = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
        flat_chunk = os.path.join(BACKEND_DIR, "app", "templates", "partials", "flat_release_row_chunk.html")
        series_chunk = os.path.join(BACKEND_DIR, "app", "templates", "partials", "series_card_chunk.html")

        for path in [prioritized_html, base_html, flat_chunk, series_chunk]:
            check(os.path.exists(path), f"Template missing: {path}")

        with open(base_html, "r", encoding="utf-8") as f:
            base_content = f.read()
            check('href="/prioritized"' in base_content or "href='/prioritized'" in base_content, "base.html missing /prioritized link")
            check("ignoreReleaseGlobal" in base_content, "base.html missing ignoreReleaseGlobal")
            check("unignoreReleaseGlobal" in base_content, "base.html missing unignoreReleaseGlobal")
            check("backfillSeriesGlobal" in base_content, "base.html missing backfillSeriesGlobal")

        print("  [PASS] 5.2 Template and JavaScript handler integrity confirmed")


def run_tests():
    suite = unittest.TestLoader().loadTestsFromTestCase(TestMilestone4Iteration3Integration)
    runner = unittest.TextTestRunner(verbosity=2)
    res = runner.run(suite)
    if not res.wasSuccessful():
        print(f"\n❌ CHALLENGER INTEGRATION TESTS FAILED: {len(res.failures)} failures, {len(res.errors)} errors")
        sys.exit(1)
    else:
        print("\n✅ CHALLENGER INTEGRATION TESTS PASSED: 100% SUCCESS")
        sys.exit(0)


if __name__ == "__main__":
    run_tests()
