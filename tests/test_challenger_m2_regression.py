# -*- coding: utf-8 -*-
"""
Empirical Challenger Regression & Edge Case Verification Suite
Milestone 2 Iteration 3
"""

import os
import sys
import tempfile
import sqlite3
import shutil
import threading
import time

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

from tests.mock_calibre import create_mock_calibre_db, add_book_to_mock_calibre
from app.calibre.scanner import CalibreScanner, parse_calibre_volumes_and_series
from app.database import Database
from app.timezone_util import get_app_timezone, format_localtime, format_localdate
from app.providers.metadata import NovelMetadataProvider, SlidingWindowRateLimiter

def check(condition: bool, msg: str):
    if not condition:
        raise AssertionError(f"CHALLENGER FAILURE: {msg}")

def test_scanner_edge_cases():
    print("\n--- 1. Testing Scanner Title & Volume Edge Cases ---")
    
    # Mob Psycho 100
    s, v = parse_calibre_volumes_and_series("Mob Psycho 100 - Vol 1", "Mob Psycho 100", 1.0)
    check(s == "Mob Psycho 100", f"Expected 'Mob Psycho 100', got {s}")
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")
    
    s, v = parse_calibre_volumes_and_series("Mob Psycho 100 - Vol 1-3", "Mob Psycho 100", 1.0)
    check(s == "Mob Psycho 100", f"Expected 'Mob Psycho 100', got {s}")
    check(v == {1.0, 2.0, 3.0}, f"Expected {{1.0, 2.0, 3.0}}, got {v}")

    s, v = parse_calibre_volumes_and_series("Mob Psycho 100 - Vol 1", None, None)
    check(s == "Mob Psycho 100", f"Expected 'Mob Psycho 100', got {s}")
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")

    s, v = parse_calibre_volumes_and_series("Mob Psycho 100 - Vol 1-3", None, None)
    check(s == "Mob Psycho 100", f"Expected 'Mob Psycho 100', got {s}")
    check(v == {1.0, 2.0, 3.0}, f"Expected {{1.0, 2.0, 3.0}}, got {v}")

    # 86 - Eighty Six
    s, v = parse_calibre_volumes_and_series("86 - Eighty Six - Vol 1", "86 - Eighty Six", 1.0)
    check(s == "86 - Eighty Six", f"Expected '86 - Eighty Six', got {s}")
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")

    s, v = parse_calibre_volumes_and_series("86 - Eighty Six - Vol 1-5", None, None)
    check(s == "86 - Eighty Six", f"Expected '86 - Eighty Six', got {s}")
    check(v == {1.0, 2.0, 3.0, 4.0, 5.0}, f"Expected 1-5, got {v}")

    s, v = parse_calibre_volumes_and_series("86 - 1-4", "86", 1.0)
    check(s == "86", f"Expected '86', got {s}")
    check(v == {1.0, 2.0, 3.0, 4.0}, f"Expected {{1.0, 2.0, 3.0, 4.0}}, got {v}")

    # Slime Taoshite 300-nen
    s, v = parse_calibre_volumes_and_series("Slime Taoshite 300-nen Vol 1", "Slime Taoshite 300-nen", 1.0)
    check(s == "Slime Taoshite 300-nen", f"Expected 'Slime Taoshite 300-nen', got {s}")
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")

    s, v = parse_calibre_volumes_and_series("Slime Taoshite 300-nen Vol 1", None, None)
    check(s == "Slime Taoshite 300-nen", f"Expected 'Slime Taoshite 300-nen', got {s}")
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")

    # Catch-22
    s, v = parse_calibre_volumes_and_series("Catch-22 - Vol 1", "Catch-22", 1.0)
    check(s == "Catch-22", f"Expected 'Catch-22', got {s}")
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")

    s, v = parse_calibre_volumes_and_series("Catch-22 - Vol 1-3", None, None)
    check(s == "Catch-22", f"Expected 'Catch-22', got {s}")
    check(v == {1.0, 2.0, 3.0}, f"Expected {{1.0, 2.0, 3.0}}, got {v}")

    # Cyberpunk 2077
    s, v = parse_calibre_volumes_and_series("Cyberpunk 2077 - Vol 1", "Cyberpunk 2077", 1.0)
    check(s == "Cyberpunk 2077", f"Expected 'Cyberpunk 2077', got {s}")
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")

    # 1984
    s, v = parse_calibre_volumes_and_series("1984 - Vol 1", "1984", 1.0)
    check(s == "1984", f"Expected '1984', got {s}")
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")

    # Steins;Gate 0
    s, v = parse_calibre_volumes_and_series("Steins;Gate 0 - Vol 1", "Steins;Gate 0", 1.0)
    check(s == "Steins;Gate 0", f"Expected 'Steins;Gate 0', got {s}")
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")

    # Classroom of the Elite: Year 2
    s, v = parse_calibre_volumes_and_series("Classroom of the Elite: Year 2 - Vol 1", "Classroom of the Elite: Year 2", 1.0)
    check(s == "Classroom of the Elite: Year 2", f"Expected 'Classroom of the Elite: Year 2', got {s}")
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")

    s, v = parse_calibre_volumes_and_series("Classroom of the Elite: Year 2 - Vol 1-3", "Classroom of the Elite: Year 2", 1.0)
    check(v == {1.0, 2.0, 3.0}, f"Expected {{1.0, 2.0, 3.0}}, got {v}")

    # Default 1.0 discard verification
    s, v = parse_calibre_volumes_and_series("Overlord Vol 4", "Overlord", 1.0)
    check(v == {4.0}, f"Expected {{4.0}} (discarding default index 1.0), got {v}")

    s, v = parse_calibre_volumes_and_series("Overlord Vol 4-6", "Overlord", 1.0)
    check(v == {4.0, 5.0, 6.0}, f"Expected {{4.0, 5.0, 6.0}}, got {v}")

    # Legitimate Vol 1 preservation
    s, v = parse_calibre_volumes_and_series("Overlord Vol 1-3", "Overlord", 1.0)
    check(v == {1.0, 2.0, 3.0}, f"Expected {{1.0, 2.0, 3.0}}, got {v}")

    print("  ✓ All scanner title and volume edge cases passed!")

def test_calibre_zero_lock_concurrency():
    print("\n--- 2. Testing Calibre Zero-Lock Concurrency & Immutable Mode ---")
    tmp_dir = tempfile.mkdtemp()
    try:
        db_path = os.path.join(tmp_dir, "metadata.db")
        create_mock_calibre_db(db_path)

        scanner = CalibreScanner(tmp_dir)
        check(scanner.is_available(), "Scanner should report available")
        
        # Test zero-lock read connection
        read_conn = scanner._get_connection()
        qo = read_conn.execute("PRAGMA query_only").fetchone()[0]
        check(qo == 1, f"Expected PRAGMA query_only = 1, got {qo}")
        read_conn.close()

        # Concurrent read while writer thread adds books
        stop_flag = False
        write_errors = []
        read_errors = []

        def writer():
            i = 100
            while not stop_flag and i < 150:
                try:
                    add_book_to_mock_calibre(db_path, f"Concurrent Book {i}", "Concurrent Series", float(i - 99), "Author X")
                    i += 1
                    time.sleep(0.01)
                except Exception as e:
                    write_errors.append(e)
                    break

        def reader():
            while not stop_flag:
                try:
                    vols_map = scanner.get_series_volumes_map()
                    check(isinstance(vols_map, dict), "vols_map should be dict")
                    summary = scanner.get_library_summary()
                    check(summary["available"] is True, "summary should be available")
                    time.sleep(0.01)
                except Exception as e:
                    read_errors.append(e)
                    break

        w_thread = threading.Thread(target=writer)
        r_thread = threading.Thread(target=reader)

        w_thread.start()
        r_thread.start()

        time.sleep(1.0)
        stop_flag = True

        w_thread.join(timeout=3.0)
        r_thread.join(timeout=3.0)

        check(len(write_errors) == 0, f"Writer encountered errors: {write_errors}")
        check(len(read_errors) == 0, f"Reader encountered zero-lock errors: {read_errors}")

        print("  ✓ Zero-lock concurrent reader/writer test passed with 0 errors!")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def test_database_persistence_and_wal():
    print("\n--- 3. Testing SQLite WAL Mode & Metadata Persistence ---")
    tmp_dir = tempfile.mkdtemp()
    try:
        db_path = os.path.join(tmp_dir, "test_app.db")
        db = Database(db_path)
        
        # Check WAL mode
        with db.get_connection() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            check(mode.lower() == "wal", f"Expected WAL mode, got {mode}")

        # Test metadata cache insertion, query, and rate limiting integration
        meta_sample = {
            "title": "Mob Psycho 100",
            "synopsis": "Shigeo Kageyama is an average middle school boy, nicknamed Mob...",
            "cover_url": "https://example.com/mob.jpg",
            "source": "anilist"
        }
        db.cache_novel_metadata("Mob Psycho 100", meta_sample)

        cached = db.get_cached_novel_metadata("mob psycho 100")
        check(cached is not None, "Cached metadata should be found case-insensitively")
        check(cached["title"] == "Mob Psycho 100", f"Cached title mismatch: {cached.get('title')}")
        check(cached["source"] == "anilist", f"Cached source mismatch: {cached.get('source')}")

        all_cached = db.get_all_novel_metadata_cache()
        check(len(all_cached) == 1, f"Expected 1 cached entry, got {len(all_cached)}")

        db.clear_novel_metadata_cache()
        check(len(db.get_all_novel_metadata_cache()) == 0, "Expected empty cache after clear")

        print("  ✓ SQLite database persistence and metadata cache passed!")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def test_timezone_formatting():
    print("\n--- 4. Testing Timezone Formatting Filters ---")
    from datetime import datetime, timezone
    
    # Copenhagen default
    tz = get_app_timezone()
    check(str(tz) == "Europe/Copenhagen", f"Expected Europe/Copenhagen, got {tz}")

    # Format timestamp
    dt_utc = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    formatted = format_localtime(dt_utc)
    check("14:00" in formatted, f"Expected 14:00 in CEST summer time, got {formatted}")

    d_formatted = format_localdate(dt_utc)
    check(d_formatted == "2026-06-15", f"Expected '2026-06-15', got {d_formatted}")

    print("  ✓ Timezone formatting verified!")

if __name__ == "__main__":
    print("=" * 70)
    print("  RUNNING CHALLENGER REGRESSION & EDGE CASE VERIFICATION")
    print("=" * 70)
    
    test_scanner_edge_cases()
    test_calibre_zero_lock_concurrency()
    test_database_persistence_and_wal()
    test_timezone_formatting()

    print("\n" + "=" * 70)
    print("  ALL CHALLENGER REGRESSION & EDGE CASE TESTS PASSED (100%)!")
    print("=" * 70)
