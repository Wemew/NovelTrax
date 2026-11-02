#!/usr/bin/env python3
r"""
tests/test_tier5_adversarial_db_providers.py
Adversarial Coverage & Negative Fuzzing Suite for NovelTrax Milestone 5 Tier 5.
Covers:
  - Database Invariants: multi-pass migration idempotency, WAL concurrency, prioritized_groups collation & whitespace, update_release_status state machine invariants.
  - Parser & Group Extraction: is_valid_group_candidate with tricky punctuation/buzzwords, ripper-over-publisher precedence permutations, unicode and non-ascii tags, title preservation during clean/ingest, dimensional video resolution rejection matrix.
  - Provider Resilience: malformed RSS XML feeds (XXE rejection, non-XML payload), HTTP error response handling, Nyaa scraper pagination boundary parsing, JNovels master index parsing edge cases.

Runner compatibility:
  & "C:\Program Files\Calibre2\calibre-debug.exe" -e tests\test_tier5_adversarial_db_providers.py
  python tests\test_tier5_adversarial_db_providers.py
"""

import os
import sys
import json
import time
import shutil
import sqlite3
import tempfile
import threading
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

from app.database import Database
from app.parser import (
    extract_series_and_volume,
    is_pdf_release,
    is_valid_group_candidate,
    extract_release_group,
    clean_feed_title,
    normalize_text,
    get_canonical_series_name,
    BUZZWORDS,
    KNOWN_PUBLISHERS,
    DEFAULT_KNOWN_GROUPS
)
from app.providers.nyaa import NyaaNovelProvider, NYAA_BASE_URL
from app.providers.jnovels import JNovelsProvider
import app.providers.jnovels as jnovels_mod
from app.providers.rss import RSSFeedProvider
from app.providers.base import ReleaseItem

# Non-strippable assertion helper for Calibre -O2 execution
passed_count = 0
failed_count = 0

def check(condition: bool, msg: str = "", fail_detail: str = "") -> None:
    global passed_count, failed_count
    if not condition:
        failed_count += 1
        detail = f": {fail_detail}" if fail_detail else ""
        raise AssertionError(f"Check failed: {msg}{detail}")
    passed_count += 1
    print(f"  ✓ {msg}")


# =====================================================================
# SUITE 1: DATABASE INVARIANTS
# =====================================================================

def run_suite_database_invariants():
    print("\n" + "=" * 70)
    print(" SUITE 1: DATABASE INVARIANTS & CONCURRENCY")
    print("=" * 70)

    tmp_dir = tempfile.mkdtemp(prefix="noveltrax_t5_db_")
    try:
        # -------------------------------------------------------------
        # Test 1.1: Multi-pass Migration Idempotency on Dirty Datasets
        # -------------------------------------------------------------
        print("\n--- Test 1.1: Multi-pass Migration Idempotency & Dirty Data ---")
        legacy_db_path = os.path.join(tmp_dir, "legacy_dirty.db")

        # Step 1: Pre-populate legacy table schema with diverse dirty records
        raw_conn = sqlite3.connect(legacy_db_path)
        raw_cur = raw_conn.cursor()
        raw_cur.execute("""
            CREATE TABLE series_tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                calibre_series_id INTEGER,
                name TEXT UNIQUE NOT NULL,
                authors TEXT DEFAULT '',
                book_count INTEGER DEFAULT 0,
                latest_index REAL DEFAULT 0.0,
                is_monitored INTEGER DEFAULT 1,
                last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        raw_cur.execute("""
            CREATE TABLE releases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guid TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                series_name TEXT,
                volume REAL,
                link TEXT,
                pub_date TEXT,
                cover_url TEXT,
                description TEXT,
                author TEXT,
                source_feed_id INTEGER,
                source_feed_name TEXT,
                status TEXT DEFAULT 'REQUESTED',
                calibre_matched_book_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        raw_cur.execute("""
            CREATE TABLE feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                feed_type TEXT NOT NULL,
                url TEXT NOT NULL,
                config_json TEXT DEFAULT '{}',
                enabled INTEGER DEFAULT 1,
                auto_request INTEGER DEFAULT 1,
                check_interval_minutes INTEGER DEFAULT 60,
                last_checked TIMESTAMP,
                last_status TEXT,
                last_error TEXT
            );
        """)
        # Pre-create prioritized_groups WITHOUT COLLATE NOCASE to test schema rebuild
        raw_cur.execute("""
            CREATE TABLE prioritized_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                notes TEXT DEFAULT '',
                priority INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Insert dirty legacy releases
        raw_cur.execute("INSERT INTO releases (guid, title, series_name, volume, status) VALUES (?, ?, ?, ?, ?);",
                        ("g_mangled_1", "J- Club] Mapping: The Trash-Tier Skill Vol 1", "Mapping", 1.0, "REQUESTED"))
        raw_cur.execute("INSERT INTO releases (guid, title, series_name, volume, status) VALUES (?, ?, ?, ?, ?);",
                        ("g_unbalanced_bracketed", "[[Yen Press] Overlord Vol 16", "Overlord", 16.0, "REQUESTED"))
        raw_cur.execute("INSERT INTO releases (guid, title, series_name, volume, status) VALUES (?, ?, ?, ?, ?);",
                        ("g_missing_open", "DanMachi Vol 19]", "DanMachi", 19.0, "REQUESTED"))
        raw_cur.execute("INSERT INTO releases (guid, title, series_name, volume, status) VALUES (?, ?, ?, ?, ?);",
                        ("g_clean_bracketed", "[Yen Press] Overlord, Vol. 16 [Digital] [EPUB]", "Overlord", 16.0, "REQUESTED"))
        raw_cur.execute("INSERT INTO releases (guid, title, series_name, volume, status) VALUES (?, ?, ?, ?, ?);",
                        ("g_status_ignored", "[Stick] Unwanted Release Vol 1", "Unwanted", 1.0, "IGNORED"))
        raw_cur.execute("INSERT INTO releases (guid, title, series_name, volume, status) VALUES (?, ?, ?, ?, ?);",
                        ("g_status_requested", "[Stick] Wanted Release Vol 2", "Wanted", 2.0, "REQUESTED"))

        # Insert case-variant duplicates in legacy prioritized_groups
        raw_cur.execute("INSERT INTO prioritized_groups (name, notes, priority) VALUES (?, ?, ?);",
                        ("Stick", "Primary VIP group", 5))
        raw_cur.execute("INSERT INTO prioritized_groups (name, notes, priority) VALUES (?, ?, ?);",
                        ("stick", "Duplicate lowercase row", 1))

        raw_conn.commit()
        raw_conn.close()

        # Step 2: Initialize Database class 5 consecutive times on dirty database
        for pass_num in range(1, 6):
            db_inst = Database(legacy_db_path)
            check(db_inst is not None, f"Database init pass {pass_num} executed successfully")

        # Step 3: Validate migration results and idempotency
        with db_inst.get_connection() as conn:
            cur = conn.cursor()

            # Verify schema columns were added to series_tracking
            cur.execute("PRAGMA table_info(series_tracking);")
            st_cols = {r["name"] for r in cur.fetchall()}
            check("website_url" in st_cols, "series_tracking has website_url column")
            check("description" in st_cols, "series_tracking has description column")
            check("cover_url" in st_cols, "series_tracking has cover_url column")
            check("owned_volumes_json" in st_cols, "series_tracking has owned_volumes_json column")

            # Verify schema columns were added to releases
            cur.execute("PRAGMA table_info(releases);")
            rel_cols = {r["name"] for r in cur.fetchall()}
            check("release_group" in rel_cols, "releases has release_group column")
            check("ignored" in rel_cols, "releases has ignored column")
            check("download_status" in rel_cols, "releases has download_status column")
            check("is_bundle" in rel_cols, "releases has is_bundle column")
            check("volumes_json" in rel_cols, "releases has volumes_json column")

            # Check repair of mangled title
            cur.execute("SELECT title, release_group FROM releases WHERE guid = 'g_mangled_1';")
            m_row = cur.fetchone()
            check("[J-Novel Club]" in m_row["title"], f"Mangled title repaired with [J-Novel Club], got: {m_row['title']}")
            check(m_row["release_group"] == "J-Novel Club", f"Mangled release_group set to J-Novel Club, got: {m_row['release_group']}")

            # Check unbalanced leading bracket repair
            cur.execute("SELECT title FROM releases WHERE guid = 'g_unbalanced_bracketed';")
            unb_row = cur.fetchone()
            check(unb_row["title"] == "[Yen Press] Overlord Vol 16", f"Unbalanced bracket '[[Yen Press]' repaired to '[Yen Press]', got: {unb_row['title']}")

            # Check missing opening bracket repair
            cur.execute("SELECT title FROM releases WHERE guid = 'g_missing_open';")
            mis_row = cur.fetchone()
            check(mis_row["title"].startswith("[DanMachi"), f"Missing leading bracket repaired with '[', got: {mis_row['title']}")

            # Check clean bracketed release group extraction
            cur.execute("SELECT title, release_group FROM releases WHERE guid = 'g_clean_bracketed';")
            cln_row = cur.fetchone()
            check(cln_row["release_group"] == "Yen Press", f"Release group extracted as 'Yen Press', got: {cln_row['release_group']}")

            # Check ignored status sync invariants
            cur.execute("SELECT ignored FROM releases WHERE guid = 'g_status_ignored';")
            check(cur.fetchone()[0] == 1, "status='IGNORED' has ignored=1")

            cur.execute("SELECT ignored FROM releases WHERE guid = 'g_status_requested';")
            check(cur.fetchone()[0] == 0, "status='REQUESTED' has ignored=0")

            # Check prioritized_groups deduplication and COLLATE NOCASE rebuild via ensure_prioritized_groups_table
            db_inst.ensure_prioritized_groups_table()
            cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='prioritized_groups';")
            prio_sql = cur.fetchone()[0]
            check("collate nocase" in prio_sql.lower(), "prioritized_groups rebuilt with COLLATE NOCASE")

            cur.execute("SELECT count(*) FROM prioritized_groups WHERE lower(name) = 'stick';")
            check(cur.fetchone()[0] == 1, f"prioritized_groups deduplicated 'stick'/'Stick' to exactly 1 row")

            # Verify performance indexes exist
            cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='releases';")
            idx_names = {r[0] for r in cur.fetchall()}
            check("idx_releases_release_group" in idx_names, "Index idx_releases_release_group exists")
            check("idx_releases_ignored" in idx_names, "Index idx_releases_ignored exists")
            check("idx_releases_status" in idx_names, "Index idx_releases_status exists")

        # -------------------------------------------------------------
        # Test 1.2: WAL Concurrency Under Parallel Read/Write Threads
        # -------------------------------------------------------------
        print("\n--- Test 1.2: WAL Concurrency Under Parallel Threads ---")
        wal_db_path = os.path.join(tmp_dir, "wal_concurrency.db")
        wal_db = Database(wal_db_path)

        # Confirm WAL mode active
        with wal_db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode;")
            j_mode = cur.fetchone()[0].lower()
            check(j_mode == "wal", f"Database journal mode is WAL (got {j_mode})")

        thread_errors: List[str] = []
        threads: List[threading.Thread] = []

        def worker_writer(thread_id: int):
            try:
                for i in range(15):
                    guid = f"thr_{thread_id}_item_{i}"
                    title = f"[Stick] Concurrency Light Novel {thread_id} Vol {i} [EPUB]"
                    wal_db.upsert_release({
                        "guid": guid,
                        "title": title,
                        "series_name": f"Concurrency LN {thread_id}",
                        "volume": float(i + 1),
                        "status": "REQUESTED"
                    })
                    time.sleep(0.005)
            except Exception as e:
                thread_errors.append(f"Writer-{thread_id}: {type(e).__name__} - {e}")

        def worker_status_updater(thread_id: int):
            try:
                for i in range(10):
                    rel_id = (i % 5) + 1
                    status = "IGNORED" if (i % 2 == 0) else "REQUESTED"
                    wal_db.update_release_status(rel_id, status)
                    time.sleep(0.007)
            except Exception as e:
                thread_errors.append(f"Updater-{thread_id}: {type(e).__name__} - {e}")

        def worker_reader(thread_id: int):
            try:
                for _ in range(15):
                    _ = wal_db.get_releases(limit=50)
                    _ = wal_db.get_prioritized_releases(limit=50)
                    _ = wal_db.get_dashboard_stats()
                    time.sleep(0.005)
            except Exception as e:
                thread_errors.append(f"Reader-{thread_id}: {type(e).__name__} - {e}")

        def worker_group_manager(thread_id: int):
            try:
                for i in range(10):
                    grp_name = f"Group_{thread_id}_{i}"
                    wal_db.add_prioritized_group(grp_name, priority=i)
                    _ = wal_db.is_group_prioritized(grp_name)
                    time.sleep(0.006)
            except Exception as e:
                thread_errors.append(f"GroupMgr-{thread_id}: {type(e).__name__} - {e}")

        # Spawn 10 concurrent threads (writers, updaters, readers, group managers)
        for t_id in range(3):
            threads.append(threading.Thread(target=worker_writer, args=(t_id,)))
        for t_id in range(2):
            threads.append(threading.Thread(target=worker_status_updater, args=(t_id,)))
        for t_id in range(3):
            threads.append(threading.Thread(target=worker_reader, args=(t_id,)))
        for t_id in range(2):
            threads.append(threading.Thread(target=worker_group_manager, args=(t_id,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)

        check(len(thread_errors) == 0, f"Concurrent threads completed with 0 errors (errors: {thread_errors})")
        total_rel = wal_db.count_releases(include_ignored=True)
        check(total_rel == 45, f"Expected 45 concurrently inserted releases (got {total_rel})")

        with wal_db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check;")
            integrity_result = cur.fetchone()[0]
            check(integrity_result == "ok", f"Database integrity check passed: {integrity_result}")

        # -------------------------------------------------------------
        # Test 1.3: Prioritized Groups Collation & Whitespace Handling
        # -------------------------------------------------------------
        print("\n--- Test 1.3: Prioritized Groups Collation & Whitespace ---")
        prio_db_path = os.path.join(tmp_dir, "prio_collation.db")
        prio_db = Database(prio_db_path)

        # Seed initial groups
        check(prio_db.add_prioritized_group("  Yen Press  ", notes="Official LN publisher", priority=5), "Added group with whitespace padding")
        check(prio_db.is_group_prioritized("yen press"), "is_group_prioritized('yen press') lowercase recognized")
        check(prio_db.is_group_prioritized("  YEN PRESS  "), "is_group_prioritized('  YEN PRESS  ') with uppercase and whitespace recognized")
        check(prio_db.is_group_prioritized("Yen Press"), "is_group_prioritized('Yen Press') recognized")
        check(not prio_db.is_group_prioritized("Unknown Publisher"), "Non-prioritized group returns False")
        check(not prio_db.is_group_prioritized(""), "Empty group name returns False")
        check(not prio_db.is_group_prioritized(None), "None group name returns False")
        check(not prio_db.add_prioritized_group("   "), "Adding pure whitespace group rejected")

        # Duplicate case update
        prio_db.add_prioritized_group("yen press", notes="Updated notes", priority=10)
        groups = prio_db.get_prioritized_groups()
        yen_groups = [g for g in groups if g["name"].lower() == "yen press"]
        check(len(yen_groups) == 1, f"Exactly 1 Yen Press group row exists after case-variant update (found {len(yen_groups)})")
        check(yen_groups[0]["priority"] == 10, f"Group priority updated to 10 (got {yen_groups[0]['priority']})")
        check(yen_groups[0]["notes"] == "Updated notes", f"Group notes updated (got {yen_groups[0]['notes']})")

        # Ingest releases with whitespace and casing variations in release_group
        prio_db.upsert_release({"guid": "g_prio_1", "title": "Overlord Vol 16", "release_group": "yen press", "status": "REQUESTED"})
        prio_db.upsert_release({"guid": "g_prio_2", "title": "DanMachi Vol 19", "release_group": "  YEN PRESS  ", "status": "REQUESTED"})
        prio_db.upsert_release({"guid": "g_unprio_1", "title": "Random Novel Vol 1", "release_group": "OtherGroup", "status": "REQUESTED"})

        prio_releases = prio_db.get_prioritized_releases()
        prio_guids = {r["guid"] for r in prio_releases}
        check("g_prio_1" in prio_guids, "g_prio_1 ('yen press') included in prioritized releases")
        check("g_prio_2" in prio_guids, "g_prio_2 ('  YEN PRESS  ') included in prioritized releases")
        check("g_unprio_1" not in prio_guids, "g_unprio_1 ('OtherGroup') excluded from prioritized releases")
        check(prio_db.count_prioritized_releases() == 2, f"count_prioritized_releases() returned 2 (got {prio_db.count_prioritized_releases()})")

        # Live release count in get_prioritized_groups
        active_groups = prio_db.get_prioritized_groups()
        yen_meta = next(g for g in active_groups if g["name"].lower() == "yen press")
        check(yen_meta["release_count"] == 2, f"Yen Press live release_count is 2 (got {yen_meta['release_count']})")

        # -------------------------------------------------------------
        # Test 1.4: update_release_status State Machine Invariants
        # -------------------------------------------------------------
        print("\n--- Test 1.4: update_release_status State Machine Invariants ---")
        sm_db_path = os.path.join(tmp_dir, "sm_invariants.db")
        sm_db = Database(sm_db_path)

        # Initial insert in NEW status
        is_new, r_id = sm_db.upsert_release({"guid": "sm_rel_1", "title": "Spice and Wolf Vol 1", "status": "NEW"})
        check(is_new, "Initial release created as new")
        r = sm_db.get_release_by_id(r_id)
        check(r["status"] == "NEW", f"Initial status is 'NEW' (got {r['status']})")
        check(r["ignored"] == 0, f"Initial ignored is 0 (got {r['ignored']})")

        # Transition 1: NEW -> REQUESTED
        sm_db.update_release_status(r_id, "REQUESTED")
        r = sm_db.get_release_by_id(r_id)
        check(r["status"] == "REQUESTED", "Transition to REQUESTED sets status='REQUESTED'")
        check(r["ignored"] == 0, "status='REQUESTED' maintains ignored=0")

        # Transition 2: REQUESTED -> IGNORED
        sm_db.update_release_status(r_id, "IGNORED")
        r = sm_db.get_release_by_id(r_id)
        check(r["status"] == "IGNORED", "Transition to IGNORED sets status='IGNORED'")
        check(r["ignored"] == 1, "status='IGNORED' sets ignored=1")

        # Transition 3: Unignore back to REQUESTED via unignore_release
        check(sm_db.unignore_release(r_id), "unignore_release returned True")
        r = sm_db.get_release_by_id(r_id)
        check(r["status"] == "REQUESTED", "unignore_release sets status='REQUESTED'")
        check(r["ignored"] == 0, "unignore_release sets ignored=0")

        # Transition 4: Ignore via ignore_release
        check(sm_db.ignore_release(r_id), "ignore_release returned True")
        r = sm_db.get_release_by_id(r_id)
        check(r["status"] == "IGNORED", "ignore_release sets status='IGNORED'")
        check(r["ignored"] == 1, "ignore_release sets ignored=1")

        # Transition 5: IGNORED -> IN_CALIBRE with book ID
        sm_db.update_release_status(r_id, "IN_CALIBRE", matched_book_id=777)
        r = sm_db.get_release_by_id(r_id)
        check(r["status"] == "IN_CALIBRE", "Transition to IN_CALIBRE sets status='IN_CALIBRE'")
        check(r["ignored"] == 0, "Transition from IGNORED to IN_CALIBRE resets ignored=0")
        check(r["calibre_matched_book_id"] == 777, f"calibre_matched_book_id recorded 777 (got {r['calibre_matched_book_id']})")

        # Transition 6: Calling update_release_status without matched_book_id preserves existing book ID (COALESCE)
        sm_db.update_release_status(r_id, "IN_CALIBRE", matched_book_id=None)
        r = sm_db.get_release_by_id(r_id)
        check(r["calibre_matched_book_id"] == 777, "COALESCE preserved existing calibre_matched_book_id=777")

        # Upsert idempotency: upserting existing IN_CALIBRE release preserves IN_CALIBRE status
        is_new_again, r_id_again = sm_db.upsert_release({"guid": "sm_rel_1", "title": "Spice and Wolf Vol 1 (Updated Title)", "status": "REQUESTED"})
        check(not is_new_again and r_id_again == r_id, "Existing release updated without new row insert")
        r = sm_db.get_release_by_id(r_id)
        check(r["status"] == "IN_CALIBRE", f"Status IN_CALIBRE preserved across upsert (got {r['status']})")
        check(r["title"] == "Spice and Wolf Vol 1 (Updated Title)", "Release title updated properly")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# =====================================================================
# SUITE 2: PARSER & GROUP EXTRACTION
# =====================================================================

def run_suite_parser_group_extraction():
    print("\n" + "=" * 70)
    print(" SUITE 2: PARSER & GROUP EXTRACTION ADVERSARIAL MATRIX")
    print("=" * 70)

    # -------------------------------------------------------------
    # Test 2.1: is_valid_group_candidate Tricky Punctuation & Buzzwords
    # -------------------------------------------------------------
    print("\n--- Test 2.1: is_valid_group_candidate Boundary & Buzzwords ---")

    # Rejection of empty, single characters, and overly long tags
    check(not is_valid_group_candidate(""), "Empty string rejected")
    check(not is_valid_group_candidate(" "), "Whitespace string rejected")
    check(not is_valid_group_candidate("a"), "Single character 'a' rejected (< 2 chars)")
    check(not is_valid_group_candidate("Z"), "Single character 'Z' rejected (< 2 chars)")
    check(not is_valid_group_candidate("A" * 41), "Overly long tag rejected (> 40 chars)")

    # Rejection of dates, years, and volume indicators
    for year_tag in ["1999", "2020", "2024", "2020-2022", "2021/2023", "2020~2024"]:
        check(not is_valid_group_candidate(year_tag), f"Year tag '{year_tag}' rejected")

    for vol_tag in ["Vol 1", "vol. 02", "Volume 12", "v01", "v12", "Book 4", "Ch. 5", "Chapter 10", "1-12", "01"]:
        check(not is_valid_group_candidate(vol_tag), f"Volume/chapter indicator '{vol_tag}' rejected")

    # Rejection of buzzwords across media formats, audio, video, packaging
    buzzword_samples = [
        "epub", "pdf", "cbz", "cbr", "mobi", "azw3", "zip", "rar", "7z", "tar",
        "audio", "dual-audio", "multi-audio", "audiobook", "flac", "aac", "mp3", "opus", "lossless",
        "1080p", "720p", "480p", "2160p", "4k", "uhd", "hd", "fhd", "remux", "bluray", "blu-ray", "web-dl",
        "x264", "x265", "h264", "hevc", "10bit", "8bit", "hdr",
        "sub", "subs", "dub", "multi-sub", "dual-sub", "raw", "softsub", "hardsub",
        "digital", "retail", "rip", "scan", "batch", "pack", "complete", "completed", "ongoing",
        "light novel", "web novel", "ln", "wn", "english", "eng", "repack", "proper", "uncensored", "bonus"
    ]
    for bw in buzzword_samples:
        check(not is_valid_group_candidate(bw), f"Buzzword '{bw}' rejected")

    # Compound buzzword token strings
    compound_buzz = [
        "1080p Web-DL", "Digital Retail", "FLAC Lossless", "Dual-Audio Multi-Sub",
        "x265 10bit HDR", "BluRay Remux Complete", "Retail EPUB Batch"
    ]
    for cb in compound_buzz:
        check(not is_valid_group_candidate(cb), f"Compound buzzword '{cb}' rejected")

    # Preservation of genuine known and unknown groups
    genuine_groups = [
        "Stick", "Yen Press", "J-Novel Club", "Seven Seas", "One Peace Books",
        "danke-Empire", "Lu自主规制", "a-sloth", "1R0N", "Kobo", "BW", "BookWalker",
        "nao", "GZ", "Anonymous", "TenmaLand", "LuCirAn", "NaBi", "aKraa", "LostYears",
        "KuroNovel", "CaféNovel", "Manga-Téca", "桜都字幕组", "ZeroBookGroup"
    ]
    for grp in genuine_groups:
        check(is_valid_group_candidate(grp), f"Genuine group '{grp}' accepted as candidate")

    # -------------------------------------------------------------
    # Test 2.2: Ripper-over-Publisher Precedence Across Permutations
    # -------------------------------------------------------------
    print("\n--- Test 2.2: Ripper-over-Publisher Precedence Permutations ---")

    permutations = [
        # (raw_title, expected_group, description)
        ("[Yen Press] [Stick] Overlord Vol. 16 [Digital] [EPUB]", "Stick", "Publisher first, Ripper second in square brackets"),
        ("[Stick] [Yen Press] Overlord Vol. 16 [Digital]", "Stick", "Ripper first, Publisher second in square brackets"),
        ("[Seven Seas] (danke-Empire) Mushoku Tensei v01", "danke-Empire", "Publisher brackets, Ripper parens"),
        ("(danke-Empire) [Seven Seas] Mushoku Tensei v01", "danke-Empire", "Ripper parens, Publisher brackets"),
        ("(Seven Seas) [danke-Empire] Mushoku Tensei v01", "danke-Empire", "Publisher parens, Ripper brackets"),
        ("[J-Novel Club] By the Grace of the Gods Vol 1 [NaBi]", "NaBi", "Publisher leading, Ripper trailing"),
        ("Overlord Vol 16 [Yen Press] (Stick)", "Stick", "Title first, Publisher brackets, Ripper parens"),
        ("[UnknownRipper] [Stick] DanMachi Vol 19", "Stick", "Unknown ripper + Known VIP ripper prioritizes Stick"),
        ("[Yen Press] Overlord, Vol. 16 [Digital] [EPUB]", "Yen Press", "Publisher-only release correctly returns Publisher"),
        ("Overlord, Vol. 16 (J-Novel Club)", "J-Novel Club", "Publisher-only in parens correctly returns Publisher"),
        ("Overlord Vol. 16 [One Peace Books]", "One Peace Books", "Publisher-only trailing bracket returns Publisher"),
    ]
    for raw_t, exp_g, desc in permutations:
        extracted = extract_release_group(raw_t)
        check(extracted == exp_g, f"{desc}: extracted '{extracted}' == '{exp_g}'")

    # Test custom known_groups override parameter
    custom_extracted = extract_release_group("[CustomVIP] [Yen Press] Novel Vol 1", known_groups=["CustomVIP"])
    check(custom_extracted == "CustomVIP", f"Custom group recognized via known_groups parameter (got '{custom_extracted}')")

    # -------------------------------------------------------------
    # Test 2.3: Unicode & Non-ASCII Tags
    # -------------------------------------------------------------
    print("\n--- Test 2.3: Unicode & Non-ASCII Tags ---")

    unicode_cases = [
        ("[Lu自主规制] DanMachi Vol 19 [Retail]", "Lu自主规制"),
        ("[桜都字幕组] Sword Art Online Vol 28", "桜都字幕组"),
        ("[CaféNovel] Re:Zero Vol 35", "CaféNovel"),
        ("[Manga-Téca] Classroom of the Elite Vol 11", "Manga-Téca"),
    ]
    for raw_t, exp_g in unicode_cases:
        res = extract_release_group(raw_t)
        check(res == exp_g, f"Extracted unicode group '{exp_g}' from '{raw_t}' (got '{res}')")

    # Unicode title preservation during normalize_text and canonical extraction
    unicode_title = "Tokidoki Bosotto Russia-go de Dereru Tonari no Alya-san | 時々ボソッとロシア語でデレる隣のアーリャさん"
    canon = get_canonical_series_name(unicode_title)
    check("Alya Sometimes Hides Her Feelings in Russian" in canon, f"Unicode bilingual title mapped to canonical English title (got '{canon}')")

    # -------------------------------------------------------------
    # Test 2.4: Title Preservation During Clean / Ingest
    # -------------------------------------------------------------
    print("\n--- Test 2.4: Title Preservation During Ingestion ---")
    tmp_ingest_dir = tempfile.mkdtemp(prefix="noveltrax_t5_ingest_")
    try:
        ingest_db = Database(os.path.join(tmp_ingest_dir, "ingest.db"))
        raw_release_title = "[danke-Empire] DanMachi - Vol 19 [Retail] [EPUB] [CRC32: 9A8B7C6D]"

        # clean_feed_title should produce clean title for search/display
        cleaned = clean_feed_title(raw_release_title)
        check("CRC32" not in cleaned or "Retail" not in cleaned, "clean_feed_title stripped buzzwords")

        # Ingestion into database must strictly preserve raw_release_title in releases.title
        _, rel_id = ingest_db.upsert_release({
            "guid": "guid_preserve_1",
            "title": raw_release_title,
            "series_name": "DanMachi (Light Novel)",
            "volume": 19.0
        })
        stored_rel = ingest_db.get_release_by_id(rel_id)
        check(stored_rel["title"] == raw_release_title, f"Raw title preserved verbatim in database: '{stored_rel['title']}'")
        check(stored_rel["series_name"] == "DanMachi", f"Series name cleaned without '(Light Novel)': '{stored_rel['series_name']}'")
        check(stored_rel["release_group"] == "danke-Empire", f"Release group extracted: '{stored_rel['release_group']}'")
        check(stored_rel["volume"] == 19.0, f"Volume parsed: {stored_rel['volume']}")

        # Re-upsert with same GUID maintains exact title
        ingest_db.upsert_release({
            "guid": "guid_preserve_1",
            "title": raw_release_title,
            "series_name": "DanMachi",
            "volume": 19.0
        })
        stored_again = ingest_db.get_release_by_id(rel_id)
        check(stored_again["title"] == raw_release_title, "Re-upsert preserved raw title untouched")

    finally:
        shutil.rmtree(tmp_ingest_dir, ignore_errors=True)

    # -------------------------------------------------------------
    # Test 2.5: Dimensional Video Resolution Rejection Matrix
    # -------------------------------------------------------------
    print("\n--- Test 2.5: Dimensional Video Resolution Matrix ---")
    res_matrix = [
        "1920x1080", "1280x720", "3840x2160", "2560x1440", "848x480", "640x480",
        "720x480", "1920x1200", "2560x1600", "1920X1080", "1280X720", "3840X2160"
    ]
    for res in res_matrix:
        check(not is_valid_group_candidate(res), f"Dimensional resolution '{res}' rejected as group candidate")
        extracted = extract_release_group(f"[{res}] Novel Series Vol 1")
        check(extracted is None, f"extract_release_group on '[{res}]' returned None (got '{extracted}')")

    # Mixed resolution and authentic group
    mixed_resolution_cases = [
        (f"[1920x1080] [Stick] Novel Vol 1", "Stick"),
        (f"[1280x720] [Yen Press] Novel Vol 2", "Yen Press"),
        (f"[3840x2160] [danke-Empire] Novel Vol 3", "danke-Empire"),
        (f"[Seven Seas] Novel Vol 4 [1920x1080]", "Seven Seas"),
        (f"[Stick] Novel Vol 5 [1280x720] [Dual-Audio]", "Stick"),
    ]
    for raw_t, exp_g in mixed_resolution_cases:
        res = extract_release_group(raw_t)
        check(res == exp_g, f"Resolution tag rejected and genuine group '{exp_g}' extracted from '{raw_t}'")


# =====================================================================
# SUITE 3: PROVIDER RESILIENCE
# =====================================================================

def run_suite_provider_resilience():
    print("\n" + "=" * 70)
    print(" SUITE 3: PROVIDER RESILIENCE & ADVERSARIAL PAYLOADS")
    print("=" * 70)

    # -------------------------------------------------------------
    # Test 3.1: Malformed RSS XML Feeds (XXE Rejection & Non-XML Payloads)
    # -------------------------------------------------------------
    print("\n--- Test 3.1: Malformed RSS XML Feeds & XXE Rejection ---")

    non_xml_payloads = [
        ("HTML 502 Bad Gateway", "<html><head><title>502 Bad Gateway</title></head><body><h1>502 Bad Gateway</h1><hr>cloudflare</body></html>"),
        ("JSON Error Response", '{"error": "Internal server error", "code": 500, "details": "Database unreachable"}'),
        ("Plain Text 404", "404 Not Found - The requested URL was not found on this server."),
        ("Truncated Corrupted XML", '<?xml version="1.0"?><rss version="2.0"><channel><title>Nyaa</title><item><title>DanMachi Vol 19</title><link>https://'),
        ("Zero Byte Empty String", "")
    ]

    for label, payload in non_xml_payloads:
        # Mock safe_http_get returning non_XML payload
        mock_resp = MagicMock()
        mock_resp.text = payload
        mock_resp.status_code = 200

        with patch("app.providers.rss.safe_http_get", return_value=mock_resp):
            rss_prov = RSSFeedProvider(name="Test RSS", url="https://example.com/feed.xml")
            items = rss_prov.fetch_releases()
            check(items == [], f"RSS provider gracefully returned empty list on {label}")

        with patch("app.providers.nyaa.safe_http_get", return_value=mock_resp):
            nyaa_prov = NyaaNovelProvider()
            items = nyaa_prov._fetch_rss("https://nyaa.si/?page=rss&c=3_1")
            check(items == [], f"Nyaa provider gracefully returned empty list on {label}")

    # XXE Attack Payload
    xxe_xml = """<?xml version="1.0" encoding="utf-8"?>
    <!DOCTYPE foo [
      <!ELEMENT foo ANY >
      <!ENTITY xxe SYSTEM "file:///C:/Windows/win.ini" >
      <!ENTITY xxe_net SYSTEM "http://169.254.169.254/latest/meta-data/" >
    ]>
    <rss version="2.0">
      <channel>
        <title>&xxe; Feed Title</title>
        <item>
          <title>&xxe; DanMachi Vol 19 &xxe_net; [EPUB]</title>
          <link>https://nyaa.si/view/999999</link>
          <guid>https://nyaa.si/view/999999</guid>
        </item>
      </channel>
    </rss>
    """
    mock_xxe_resp = MagicMock()
    mock_xxe_resp.text = xxe_xml
    mock_xxe_resp.status_code = 200

    with patch("app.providers.nyaa.safe_http_get", return_value=mock_xxe_resp):
        nyaa_prov = NyaaNovelProvider()
        parsed_items = nyaa_prov._fetch_rss("https://nyaa.si/?page=rss&c=3_1")
        # In-memory feedparser does not resolve external entities by default; verify win.ini content not leaked
        for item in parsed_items:
            check("[extensions]" not in item.title and "169.254" not in item.title, "XXE entity expansion neutralized without system file disclosure")

    with patch("app.providers.rss.safe_http_get", return_value=mock_xxe_resp):
        rss_prov = RSSFeedProvider(name="XXE Test", url="https://example.com/feed.xml")
        parsed_items = rss_prov.fetch_releases()
        for item in parsed_items:
            check("[extensions]" not in item.title and "169.254" not in item.title, "RSS XXE entity expansion neutralized without system file disclosure")

    # -------------------------------------------------------------
    # Test 3.2: HTTP Error Response Handling
    # -------------------------------------------------------------
    print("\n--- Test 3.2: HTTP Error Response Handling ---")

    # HTTP 404 cleanly terminates pagination without exception
    mock_404 = MagicMock()
    mock_404.status_code = 404
    mock_404.text = "404 Not Found"

    with patch("app.providers.nyaa.safe_http_get", return_value=mock_404):
        nyaa = NyaaNovelProvider()
        crawl_items = nyaa.crawl_pages(max_pages=3)
        check(crawl_items == [], "Nyaa crawl_pages on HTTP 404 cleanly terminates loop returning []")

    # Network Exception / Timeout Handling
    with patch("app.providers.nyaa.safe_http_get", side_effect=Exception("Connection timed out (15.0s)")):
        nyaa = NyaaNovelProvider()
        crawl_items = nyaa.crawl_pages(max_pages=2)
        check(crawl_items == [], "Nyaa crawl_pages handles timeout gracefully returning []")

        feed_items = nyaa.check_feed()
        check(feed_items == [], "Nyaa check_feed handles timeout gracefully falling back and returning []")

    with patch("app.providers.rss.safe_http_get", side_effect=Exception("SSRF blocked: private IP")):
        rss = RSSFeedProvider("Blocked Feed", "http://192.168.1.50/feed")
        rss_items = rss.fetch_releases()
        check(rss_items == [], "RSS feed handles SSRF exception gracefully returning []")

    # -------------------------------------------------------------
    # Test 3.3: Nyaa Scraper Pagination Boundary Parsing
    # -------------------------------------------------------------
    print("\n--- Test 3.3: Nyaa Scraper Pagination Boundary Parsing ---")

    # Boundary 1: max_pages = 0
    nyaa = NyaaNovelProvider()
    check(nyaa.crawl_pages(max_pages=0) == [], "crawl_pages(max_pages=0) returns empty list immediately")

    # Boundary 2: Empty HTML table
    empty_html = """<!DOCTYPE html><html><body><table class="torrent-list"><tbody></tbody></table></body></html>"""
    mock_empty_table = MagicMock()
    mock_empty_table.status_code = 200
    mock_empty_table.text = empty_html

    with patch("app.providers.nyaa.safe_http_get", return_value=mock_empty_table):
        items = nyaa.crawl_pages(max_pages=2)
        check(items == [], "crawl_pages on empty HTML table breaks loop cleanly returning []")

    # Boundary 3: Table rows with malformed columns (missing tags, comments-only links, missing links)
    malformed_table_html = """<!DOCTYPE html>
    <html><body>
    <table class="torrent-list">
      <tbody>
        <!-- Row 1: Valid EPUB release -->
        <tr>
          <td><a href="/?c=3_1">Literature</a></td>
          <td><a href="/view/1850100">[Yen Press] Overlord Vol 16 [EPUB]</a></td>
          <td><a href="/download/1850100.torrent">DL</a><a href="magnet:?xt=urn:btih:ABCDEF">MAG</a></td>
          <td>25.0 MiB</td>
          <td data-timestamp="1730000000">2026-10-27 12:00</td>
          <td>50</td>
          <td>5</td>
        </tr>
        <!-- Row 2: Empty row with no td -->
        <tr></tr>
        <!-- Row 3: Missing title link (only comments icon) -->
        <tr>
          <td>Category</td>
          <td><a class="comments" href="/view/1850101#comments">3</a></td>
          <td><a href="/download/1850101.torrent">DL</a></td>
          <td>10.0 MiB</td>
          <td data-timestamp="1730000000">2026-10-27</td>
          <td>1</td>
          <td>0</td>
        </tr>
        <!-- Row 4: PDF release (must be filtered) -->
        <tr>
          <td>Category</td>
          <td><a href="/view/1850102">[Random] DanMachi Vol 19 [PDF]</a></td>
          <td><a href="/download/1850102.torrent">DL</a></td>
          <td>12.0 MiB</td>
          <td data-timestamp="1730000000">2026-10-27</td>
          <td>10</td>
          <td>0</td>
        </tr>
        <!-- Row 5: Non-numeric seeders/leechers and invalid timestamp -->
        <tr>
          <td>Category</td>
          <td><a href="/view/1850103">[Stick] Mushoku Tensei v01 [EPUB]</a></td>
          <td><a href="magnet:?xt=urn:btih:FEDCBA">MAG</a></td>
          <td>18.5 MiB</td>
          <td data-timestamp="invalid_ts">Unknown Date</td>
          <td>N/A</td>
          <td>-</td>
        </tr>
      </tbody>
    </table>
    </body></html>
    """
    mock_malformed = MagicMock()
    mock_malformed.status_code = 200
    mock_malformed.text = malformed_table_html

    with patch("app.providers.nyaa.safe_http_get", return_value=mock_malformed):
        items = nyaa.crawl_pages(max_pages=1)
        check(len(items) == 2, f"Malformed table parsed successfully: 2 valid non-PDF releases extracted (got {len(items)})")
        check(items[0].title == "[Yen Press] Overlord Vol 16 [EPUB]", "First item is Overlord Vol 16")
        check(items[0].download_url == "magnet:?xt=urn:btih:ABCDEF", "Magnet link prioritized over torrent download")
        check(items[1].title == "[Stick] Mushoku Tensei v01 [EPUB]", "Second item is Mushoku Tensei v01")
        check(items[1].raw_data["seeders"] is None, "Non-numeric seeders parsed safely as None")
        check(items[1].raw_data["leechers"] is None, "Non-numeric leechers parsed safely as None")

    # Boundary 4: Date cutoff termination
    cutoff_dt = datetime.fromtimestamp(1730000050, tz=timezone.utc)
    with patch("app.providers.nyaa.safe_http_get", return_value=mock_malformed):
        items_cutoff = nyaa.crawl_pages(max_pages=1, date_cutoff=cutoff_dt)
        check(len(items_cutoff) == 0, f"Date cutoff strictly filtered out older releases (got {len(items_cutoff)})")

    # Boundary 5: stop_check callback aborts crawl immediately
    call_count = 0
    def stop_cb():
        nonlocal call_count
        call_count += 1
        return True

    items_stopped = nyaa.crawl_pages(max_pages=5, stop_check=stop_cb)
    check(len(items_stopped) == 0, "stop_check aborts crawl before network request")

    # -------------------------------------------------------------
    # Test 3.4: JNovels Master Index Parsing Edge Cases
    # -------------------------------------------------------------
    print("\n--- Test 3.4: JNovels Master Index Parsing Edge Cases ---")

    # Edge Case 1: HTML lacking .entry-content container
    no_content_html = "<html><body><header>Nav</header><footer>Footer</footer></body></html>"
    mock_resp_1 = MagicMock()
    mock_resp_1.read.return_value = no_content_html.encode("utf-8")
    mock_resp_1.__enter__.return_value = mock_resp_1
    mock_resp_1.__exit__.return_value = None

    with patch.object(jnovels_mod, "httpx", None):
        with patch("urllib.request.urlopen", return_value=mock_resp_1):
            res = JNovelsProvider.fetch_master_index("https://jnovels.com/light-novel-epub-jp/")
            check(res == [], "Missing .entry-content returns [] cleanly")

    # Edge Case 2: Content with diverse navigation links, empty anchors, and duplicate links
    master_index_html = """<!DOCTYPE html>
    <html><body>
    <div class="entry-content">
      <!-- Invalid / navigation anchors -->
      <a href="">Empty Link</a>
      <a href="https://jnovels.com/">Home</a>
      <a href="https://jnovels.com/category/light-novels/">Category</a>
      <a href="https://jnovels.com/tag/action/">Tag</a>
      <a href="https://jnovels.com/faq/">FAQ</a>
      <a href="https://jnovels.com/contact/">Contact Us</a>
      <a href="https://google.com/search">External</a>
      <a href="https://jnovels.com/author/admin/">Author</a>
      <a href="https://jnovels.com/sample/?mode=grid">Grid Mode</a>

      <!-- Valid series anchors -->
      <a href="https://jnovels.com/danmachi-epub/">Is It Wrong to Try to Pick Up Girls in a Dungeon? (Light Novel)</a>
      <a href="https://jnovels.com/overlord-epub/">Overlord [Light Novel] Vol 1-16</a>
      <a href="https://jnovels.com/86-eighty-six-epub/">86-Eighty Six (LN)</a>
      <a href="https://jnovels.com/roshidere-epub/">Alya Sometimes Hides Her Feelings in Russian</a>
      <!-- Duplicate anchor -->
      <a href="https://jnovels.com/danmachi-epub/">DanMachi Duplicate Anchor</a>
    </div>
    </body></html>
    """
    mock_resp_2 = MagicMock()
    mock_resp_2.read.return_value = master_index_html.encode("utf-8")
    mock_resp_2.__enter__.return_value = mock_resp_2
    mock_resp_2.__exit__.return_value = None

    with patch.object(jnovels_mod, "httpx", None):
        with patch("urllib.request.urlopen", return_value=mock_resp_2):
            master_results = JNovelsProvider.fetch_master_index("https://jnovels.com/light-novel-epub-jp/")
            check(len(master_results) == 4, f"Master index parsed exactly 4 unique canonical series (got {len(master_results)})")
            extracted_names = [r.series_name for r in master_results]
            check("Is It Wrong to Try to Pick Up Girls in a Dungeon?" in extracted_names, "DanMachi title cleaned")
            check("Overlord" in extracted_names, "Overlord title cleaned")
            check("86-Eighty Six" in extracted_names, "86-Eighty Six title cleaned")
            check("Alya Sometimes Hides Her Feelings in Russian" in extracted_names, "Alya title cleaned")
            for r in master_results:
                check(r.series_link == r.link, f"Series link points to canonical hub: {r.link}")
                check(r.raw_data.get("is_master_index"), "Marked with is_master_index flag")

    # Edge Case 3: Network exception handling in master index
    with patch.object(jnovels_mod, "httpx", None):
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            err_res = JNovelsProvider.fetch_master_index("https://jnovels.com/light-novel-epub-jp/")
            check(err_res == [], "Network error in master index returns empty list without raising exception")

    # Edge Case 4: Test httpx branch if simulated in container environment
    mock_httpx = MagicMock()
    mock_client = MagicMock()
    mock_hx_resp = MagicMock()
    mock_hx_resp.status_code = 200
    mock_hx_resp.text = master_index_html
    mock_client.get.return_value = mock_hx_resp
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None
    mock_httpx.Client.return_value = mock_client

    with patch.object(jnovels_mod, "httpx", mock_httpx):
        hx_results = JNovelsProvider.fetch_master_index("https://jnovels.com/light-novel-epub-jp/")
        check(len(hx_results) == 4, f"httpx transport parsed exactly 4 unique canonical series (got {len(hx_results)})")


# =====================================================================
# SUITE 4: CALIBRE -O2 RUNTIME SAFETY
# =====================================================================

def run_suite_calibre_o2_safety():
    print("\n" + "=" * 70)
    print(" SUITE 4: CALIBRE -O2 RUNTIME SAFETY & UNSTRIPPABLE ASSERTIONS")
    print("=" * 70)

    # Verify check() function raises AssertionError on falsy conditions
    check(True, "check(True) executes cleanly")

    assertion_caught = False
    try:
        check(False, "Intended failure check for -O2 validation")
    except AssertionError:
        assertion_caught = True
        global failed_count
        failed_count -= 1  # Revert intentionally triggered failure count

    check(assertion_caught, "check(False) raises AssertionError deterministically under all optimization flags")


# =====================================================================
# MAIN RUNNER
# =====================================================================

def main():
    print("=================================================================")
    print(" NovelTrax Milestone 5 Tier 5: Adversarial DB & Providers Suite")
    print("=================================================================")
    start_time = time.time()

    try:
        run_suite_database_invariants()
        run_suite_parser_group_extraction()
        run_suite_provider_resilience()
        run_suite_calibre_o2_safety()
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
        print("VERDICT: FAILED (Bugs detected)")
        sys.exit(1)
    print("ALL TESTS PASSED WITH 100% SUCCESS.")
    sys.exit(0)


if __name__ == "__main__":
    main()
