"""
tests/test_omnibus_timezone.py

Dedicated test suite for NovelTrax Milestone 2:
- Calibre Omnibus Title Parsing (Vol 1-3, v01-02, [1-4], etc.) & Series Volumes Map
- sync_library populating owned_volumes_json in series_tracking
- Timezone Localization (Europe/Copenhagen, custom TZ, DST handling) & Jinja2 filters
- SQLite novel_metadata_cache CRUD, cache hit bypassing API, and SlidingWindowRateLimiter
- UI Top Progress Bar DOM element and scripts in base.html

Note: Calibre executes with -O2, so standard 'assert' statements may be stripped.
All verifications in this suite use explicit, non-strippable check() assertions.
"""

import os
import sys
import json
import time
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta

# Ensure backend directory is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.calibre.scanner import CalibreScanner, parse_calibre_volumes_and_series
from app.database import Database
from app.timezone_util import get_app_timezone, format_localtime, format_localdate
from app.providers.metadata import NovelMetadataProvider, SlidingWindowRateLimiter

def check(condition: bool, msg: str = ""):
    """Non-strippable assertion helper that survives Python -O/-O2 optimization."""
    if not condition:
        raise AssertionError(f"Check failed: {msg}")

def make_test_calibre_db(db_path: str):
    """Creates a mock Calibre library metadata.db with single and omnibus books."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            series_index REAL DEFAULT 1.0,
            uuid TEXT DEFAULT 'mock-uuid'
        );
    """)
    cur.execute("""
        CREATE TABLE series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );
    """)
    cur.execute("""
        CREATE TABLE books_series_link (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book INTEGER NOT NULL,
            series INTEGER NOT NULL
        );
    """)
    cur.execute("""
        CREATE TABLE authors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        );
    """)
    cur.execute("""
        CREATE TABLE books_authors_link (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book INTEGER NOT NULL,
            author INTEGER NOT NULL
        );
    """)

    # 1. Series: Overlord (id=1)
    # Book 1: Vol 1-3 omnibus (series_index=1.0)
    # Book 2: Vol 4 (series_index=4.0)
    cur.execute("INSERT INTO series (id, name) VALUES (1, 'Overlord');")
    cur.execute("INSERT INTO books (id, title, series_index) VALUES (1, 'Overlord Vol 1-3', 1.0);")
    cur.execute("INSERT INTO books_series_link (book, series) VALUES (1, 1);")
    cur.execute("INSERT INTO books (id, title, series_index) VALUES (2, 'Overlord Vol 4', 4.0);")
    cur.execute("INSERT INTO books_series_link (book, series) VALUES (2, 1);")

    # 2. Series: Mushoku Tensei (id=2)
    # Book 3: v01-02 bundle
    # Book 4: v03
    cur.execute("INSERT INTO series (id, name) VALUES (2, 'Mushoku Tensei');")
    cur.execute("INSERT INTO books (id, title, series_index) VALUES (3, 'Mushoku Tensei v01-02', 1.0);")
    cur.execute("INSERT INTO books_series_link (book, series) VALUES (3, 2);")
    cur.execute("INSERT INTO books (id, title, series_index) VALUES (4, 'Mushoku Tensei v03', 3.0);")
    cur.execute("INSERT INTO books_series_link (book, series) VALUES (4, 2);")

    # 3. Series: Sword Art Online (id=3)
    # Book 5: [1-4] bracket omnibus
    cur.execute("INSERT INTO series (id, name) VALUES (3, 'Sword Art Online');")
    cur.execute("INSERT INTO books (id, title, series_index) VALUES (5, 'Sword Art Online [1-4]', 1.0);")
    cur.execute("INSERT INTO books_series_link (book, series) VALUES (5, 3);")

    # Author
    cur.execute("INSERT INTO authors (id, name) VALUES (1, 'Author San');")
    for b_id in [1, 2, 3, 4, 5]:
        cur.execute("INSERT INTO books_authors_link (book, author) VALUES (?, 1);", (b_id,))

    conn.commit()
    conn.close()

def run_tests():
    total_passed = 0
    print("=" * 70)
    print("  RUNNING MILESTONE 2: OMNIBUS, TIMEZONE & METADATA CACHE TEST SUITE")
    print("=" * 70)

    # --------------------------------------------------------------------------
    # 1. Calibre Omnibus Bundle Parsing Tests
    # --------------------------------------------------------------------------
    print("\n--- 1. Testing Calibre Omnibus Bundle Parsing ---")

    # Test 1a: Standard hyphen range bundle
    detected_s, vols = parse_calibre_volumes_and_series("Overlord Vol 1-3", "Overlord", 1.0)
    check(vols == {1.0, 2.0, 3.0}, f"Expected {{1.0, 2.0, 3.0}}, got {vols}")
    check(detected_s == "Overlord", f"Expected Overlord, got {detected_s}")
    print("  ✓ 'Overlord Vol 1-3' expanded to {1.0, 2.0, 3.0}")
    total_passed += 1

    # Test 1b: Zero-padded v01-02 bundle
    detected_s, vols = parse_calibre_volumes_and_series("Mushoku Tensei v01-02", "Mushoku Tensei", 1.0)
    check(vols == {1.0, 2.0}, f"Expected {{1.0, 2.0}}, got {vols}")
    print("  ✓ 'Mushoku Tensei v01-02' expanded to {1.0, 2.0}")
    total_passed += 1

    # Test 1c: Bracketed omnibus [1-4]
    detected_s, vols = parse_calibre_volumes_and_series("Sword Art Online [1-4]", "Sword Art Online", 1.0)
    check(vols == {1.0, 2.0, 3.0, 4.0}, f"Expected {{1.0, 2.0, 3.0, 4.0}}, got {vols}")
    print("  ✓ 'Sword Art Online [1-4]' expanded to {1.0, 2.0, 3.0, 4.0}")
    total_passed += 1

    # Test 1d: Comma-separated list bundle 'Vol 1, 2, 3'
    detected_s, vols = parse_calibre_volumes_and_series("Monogatari Vol 1, 2, 3", "Monogatari", 1.0)
    check(vols == {1.0, 2.0, 3.0}, f"Expected {{1.0, 2.0, 3.0}}, got {vols}")
    print("  ✓ 'Monogatari Vol 1, 2, 3' parsed to {1.0, 2.0, 3.0}")
    total_passed += 1

    # Test 1e: Plus-separated list bundle 'v01+v02'
    detected_s, vols = parse_calibre_volumes_and_series("Re:Zero v01+v02", "Re:Zero", 1.0)
    check(vols == {1.0, 2.0}, f"Expected {{1.0, 2.0}}, got {vols}")
    print("  ✓ 'Re:Zero v01+v02' parsed to {1.0, 2.0}")
    total_passed += 1

    # Test 1f: Discard default 1.0 when single volume in title is Vol 5
    detected_s, vols = parse_calibre_volumes_and_series("DanMachi Vol. 19", "DanMachi", 1.0)
    check(vols == {19.0}, f"Expected {{19.0}} without default 1.0, got {vols}")
    print("  ✓ Default Calibre index 1.0 discarded when title specifies Vol 19")
    total_passed += 1

    # Test 1g: Standalone title without series tag
    detected_s, vols = parse_calibre_volumes_and_series("Spice and Wolf Vol 1-3", None, None)
    check(vols == {1.0, 2.0, 3.0}, f"Expected {{1.0, 2.0, 3.0}}, got {vols}")
    check("Spice and Wolf" in (detected_s or ""), f"Expected 'Spice and Wolf', got {detected_s}")
    print("  ✓ Standalone book title without series tag extracted series and omnibus volumes")
    total_passed += 1

    # Test 1h: Multi-volume bundle starting > 1 discards Calibre default 1.0 (Overlord Vol 4-6)
    detected_s, vols = parse_calibre_volumes_and_series("Overlord Vol 4-6", "Overlord", 1.0)
    check(vols == {4.0, 5.0, 6.0}, f"Expected {{4.0, 5.0, 6.0}}, got {vols}")
    print("  ✓ 'Overlord Vol 4-6' expanded to {4.0, 5.0, 6.0} and discarded default 1.0")
    total_passed += 1

    # Test 1i: Bracketed multi-volume bundle starting > 1 discards default 1.0 (Sword Art Online [5-8])
    detected_s, vols = parse_calibre_volumes_and_series("Sword Art Online [5-8]", "Sword Art Online", 1.0)
    check(vols == {5.0, 6.0, 7.0, 8.0}, f"Expected {{5.0, 6.0, 7.0, 8.0}}, got {vols}")
    print("  ✓ 'Sword Art Online [5-8]' expanded to {5.0, 6.0, 7.0, 8.0} and discarded default 1.0")
    total_passed += 1

    # Test 1j: Book & list bundle starting > 1 discards default 1.0 (Book 2 & 3, Bk 4 + 5)
    detected_s, vols = parse_calibre_volumes_and_series("Ascendance of a Bookworm Book 2 & 3", "Ascendance of a Bookworm", 1.0)
    check(vols == {2.0, 3.0}, f"Expected {{2.0, 3.0}}, got {vols}")
    detected_s2, vols2 = parse_calibre_volumes_and_series("Ascendance of a Bookworm Bk 4 + 5", "Ascendance of a Bookworm", 1.0)
    check(vols2 == {4.0, 5.0}, f"Expected {{4.0, 5.0}}, got {vols2}")
    print("  ✓ 'Book 2 & 3' and 'Bk 4 + 5' parsed and discarded default 1.0")
    total_passed += 1

    # Test 1k: Compact unspaced volume tags (v15, v05, v08.5)
    detected_s, vols = parse_calibre_volumes_and_series("Series Title v15", "Series Title", 1.0)
    check(vols == {15.0}, f"Expected {{15.0}}, got {vols}")
    detected_s, vols = parse_calibre_volumes_and_series("Series Title v05", "Series Title", 1.0)
    check(vols == {5.0}, f"Expected {{5.0}}, got {vols}")
    detected_s, vols = parse_calibre_volumes_and_series("Series Title v08.5", "Series Title", 1.0)
    check(vols == {8.5}, f"Expected {{8.5}}, got {vols}")
    print("  ✓ Compact unspaced v15, v05, v08.5 successfully parsed and discarded default 1.0")
    total_passed += 1

    # Test 1l: Fractional range upper-bounds and single fractional volumes (Vol 1-2.5, Vol 1.5)
    detected_s, vols = parse_calibre_volumes_and_series("Series Vol 1-2.5", "Series", 1.0)
    check(vols == {1.0, 2.0, 2.5}, f"Expected {{1.0, 2.0, 2.5}}, got {vols}")
    detected_s, vols = parse_calibre_volumes_and_series("Series Vol 1.5", "Series", 1.0)
    check(vols == {1.5}, f"Expected {{1.5}}, got {vols}")
    print("  ✓ Fractional bounds 'Vol 1-2.5' and 'Vol 1.5' accurately parsed")
    total_passed += 1

    # Test 1m: Volume 0 prequel support (Re:Zero Vol 0)
    detected_s, vols = parse_calibre_volumes_and_series("Re:Zero Vol 0", "Re:Zero", None)
    check(vols == {0.0}, f"Expected {{0.0}}, got {vols}")
    detected_s, vols = parse_calibre_volumes_and_series("Re:Zero Vol 0-2", "Re:Zero", None)
    check(vols == {0.0, 1.0, 2.0}, f"Expected {{0.0, 1.0, 2.0}}, got {vols}")
    print("  ✓ Volume 0 prequels ('Re:Zero Vol 0', 'Re:Zero Vol 0-2') recognized as 0.0")
    total_passed += 1

    # Test 1n: Titles with numbers preceding delimiters (prevent false volume generation)
    # Mob Psycho 100 - Vol 1 -> {1.0}
    s, v = parse_calibre_volumes_and_series("Mob Psycho 100 - Vol 1", "Mob Psycho 100", 1.0)
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")
    check(s == "Mob Psycho 100", f"Expected 'Mob Psycho 100', got {s}")

    s, v = parse_calibre_volumes_and_series("Mob Psycho 100 - Vol 1", None, None)
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")
    check(s == "Mob Psycho 100", f"Expected 'Mob Psycho 100', got {s}")

    # 86 - Vol 1 -> {1.0}
    s, v = parse_calibre_volumes_and_series("86 - Vol 1", "86", 1.0)
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")
    check(s == "86", f"Expected '86', got {s}")

    s, v = parse_calibre_volumes_and_series("86 - Vol 1", None, None)
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")
    check(s == "86", f"Expected '86', got {s}")

    # Classroom of the Elite: Year 2 - Vol 1 -> {1.0}
    s, v = parse_calibre_volumes_and_series("Classroom of the Elite: Year 2 - Vol 1", "Classroom of the Elite: Year 2", 1.0)
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")
    check(s == "Classroom of the Elite: Year 2", f"Expected 'Classroom of the Elite: Year 2', got {s}")

    s, v = parse_calibre_volumes_and_series("Classroom of the Elite: Year 2 - Vol 1", None, None)
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")
    check(s == "Classroom of the Elite: Year 2", f"Expected 'Classroom of the Elite: Year 2', got {s}")

    # Catch-22 - Vol 1 -> {1.0}
    s, v = parse_calibre_volumes_and_series("Catch-22 - Vol 1", None, None)
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")
    check(s == "Catch-22", f"Expected 'Catch-22', got {s}")

    # Steins;Gate 0 - Vol 1 -> {1.0}
    s, v = parse_calibre_volumes_and_series("Steins;Gate 0 - Vol 1", None, None)
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")
    check(s == "Steins;Gate 0", f"Expected 'Steins;Gate 0', got {s}")

    print("  ✓ Titles with numbers preceding delimiters ('Mob Psycho 100', '86', 'Catch-22', etc.) correctly parsed as {1.0}")
    total_passed += 1

    # Test 1o: Series title preservation in clean_name
    # 86 - Eighty Six - Vol 1 -> series '86 - Eighty Six'
    s, v = parse_calibre_volumes_and_series("86 - Eighty Six - Vol 1", None, None)
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")
    check(s == "86 - Eighty Six", f"Expected '86 - Eighty Six', got {s}")

    # Slime Taoshite 300-nen Vol 1 -> series 'Slime Taoshite 300-nen'
    s, v = parse_calibre_volumes_and_series("Slime Taoshite 300-nen Vol 1", None, None)
    check(v == {1.0}, f"Expected {{1.0}}, got {v}")
    check(s == "Slime Taoshite 300-nen", f"Expected 'Slime Taoshite 300-nen', got {s}")

    print("  ✓ Series names with numbers/hyphens ('86 - Eighty Six', 'Slime Taoshite 300-nen') preserved without truncation")
    total_passed += 1

    # Test 1p: Genuine omnibus ranges for numbered series
    # Mob Psycho 100 - Vol 1-3 -> {1.0, 2.0, 3.0}
    s, v = parse_calibre_volumes_and_series("Mob Psycho 100 - Vol 1-3", "Mob Psycho 100", 1.0)
    check(v == {1.0, 2.0, 3.0}, f"Expected {{1.0, 2.0, 3.0}}, got {v}")
    check(s == "Mob Psycho 100", f"Expected 'Mob Psycho 100', got {s}")

    s, v = parse_calibre_volumes_and_series("Mob Psycho 100 - Vol 1-3", None, None)
    check(v == {1.0, 2.0, 3.0}, f"Expected {{1.0, 2.0, 3.0}}, got {v}")
    check(s == "Mob Psycho 100", f"Expected 'Mob Psycho 100', got {s}")

    # 86 - 1-4 -> {1.0, 2.0, 3.0, 4.0}
    s, v = parse_calibre_volumes_and_series("86 - 1-4", "86", 1.0)
    check(v == {1.0, 2.0, 3.0, 4.0}, f"Expected {{1.0, 2.0, 3.0, 4.0}}, got {v}")
    check(s == "86", f"Expected '86', got {s}")

    # Standalone 86 - 1-4 without Calibre series metadata (series_name=None)
    s, v = parse_calibre_volumes_and_series("86 - 1-4", None, None)
    check(v == {1.0, 2.0, 3.0, 4.0}, f"Expected {{1.0, 2.0, 3.0, 4.0}}, got {v}")
    check(s == "86", f"Expected '86', got {s}")

    s, v = parse_calibre_volumes_and_series("86 - 1-4", None, 1.0)
    check(v == {1.0, 2.0, 3.0, 4.0}, f"Expected {{1.0, 2.0, 3.0, 4.0}}, got {v}")
    check(s == "86", f"Expected '86', got {s}")

    # Standalone Mob Psycho 100 - 1-3 without Calibre series metadata (bare range)
    s, v = parse_calibre_volumes_and_series("Mob Psycho 100 - 1-3", None, None)
    check(v == {1.0, 2.0, 3.0}, f"Expected {{1.0, 2.0, 3.0}}, got {v}")
    check(s == "Mob Psycho 100", f"Expected 'Mob Psycho 100', got {s}")

    # Standalone Studio 54 - 1-3 and Apollo 13 - 1-3 without Calibre series metadata
    s, v = parse_calibre_volumes_and_series("Studio 54 - 1-3", None, None)
    check(v == {1.0, 2.0, 3.0}, f"Expected {{1.0, 2.0, 3.0}}, got {v}")
    check(s == "Studio 54", f"Expected 'Studio 54', got {s}")

    s, v = parse_calibre_volumes_and_series("Apollo 13 - 1-3", None, None)
    check(v == {1.0, 2.0, 3.0}, f"Expected {{1.0, 2.0, 3.0}}, got {v}")
    check(s == "Apollo 13", f"Expected 'Apollo 13', got {s}")

    print("  ✓ Genuine omnibus ranges for numbered series ('Mob Psycho 100 - Vol 1-3', '86 - 1-4', 'Studio 54 - 1-3', 'Apollo 13 - 1-3') expanded accurately with series_name=None")
    total_passed += 1

    # --------------------------------------------------------------------------
    # 2. Scanner get_series_volumes_map & sync_library Integration Tests
    # --------------------------------------------------------------------------
    print("\n--- 2. Testing Calibre Scanner volumes map & sync_library ---")
    tmp_dir = tempfile.mkdtemp(prefix="noveltrax_test_m2_")
    try:
        calibre_db_path = os.path.join(tmp_dir, "calibre", "metadata.db")
        app_db_path = os.path.join(tmp_dir, "app", "noveltrax.db")
        make_test_calibre_db(calibre_db_path)

        scanner = CalibreScanner(os.path.dirname(calibre_db_path))
        check(scanner.is_available(), "Calibre scanner should be available")

        # Verify get_series_volumes_map
        vmap = scanner.get_series_volumes_map()
        check(isinstance(vmap, dict), "vmap should be a dict")
        check("overlord" in vmap, "Overlord should be in vmap")
        check({1.0, 2.0, 3.0, 4.0}.issubset(vmap["overlord"]), f"Overlord should have 1..4, got {vmap['overlord']}")
        check({1.0, 2.0, 3.0}.issubset(vmap["mushoku tensei"]), f"Mushoku Tensei should have 1..3, got {vmap['mushoku tensei']}")
        check({1.0, 2.0, 3.0, 4.0}.issubset(vmap["sword art online"]), f"SAO should have 1..4, got {vmap['sword art online']}")
        print("  ✓ get_series_volumes_map correctly aggregated single & omnibus volumes across Calibre series")
        total_passed += 1

        # Test sync_library(db) populating owned_volumes_json
        db = Database(app_db_path)
        sync_res = scanner.sync_library(db)
        check(sync_res["synced"] is True, "sync_library should return synced=True")
        check(sync_res["series_synced"] == 3, f"Expected 3 series synced, got {sync_res['series_synced']}")

        all_series = db.get_all_series()
        series_by_name = {s["name"].lower(): s for s in all_series}

        check("overlord" in series_by_name, "Overlord in series_tracking")
        ov_overlord = json.loads(series_by_name["overlord"].get("owned_volumes_json") or "[]")
        check(ov_overlord == [1.0, 2.0, 3.0, 4.0], f"Expected Overlord owned_volumes [1.0, 2.0, 3.0, 4.0], got {ov_overlord}")

        ov_mushoku = json.loads(series_by_name["mushoku tensei"].get("owned_volumes_json") or "[]")
        check(ov_mushoku == [1.0, 2.0, 3.0], f"Expected Mushoku owned_volumes [1.0, 2.0, 3.0], got {ov_mushoku}")

        ov_sao = json.loads(series_by_name["sword art online"].get("owned_volumes_json") or "[]")
        check(ov_sao == [1.0, 2.0, 3.0, 4.0], f"Expected SAO owned_volumes [1.0, 2.0, 3.0, 4.0], got {ov_sao}")
        print("  ✓ sync_library populated series_tracking.owned_volumes_json with sorted expanded volumes")
        total_passed += 1

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # --------------------------------------------------------------------------
    # 3. Timezone Localization Tests
    # --------------------------------------------------------------------------
    print("\n--- 3. Testing Timezone Localization & Formatting ---")

    # Test 3a: Default Denmark timezone (Europe/Copenhagen)
    default_tz = get_app_timezone()
    check(str(default_tz) == "Europe/Copenhagen", f"Expected Europe/Copenhagen, got {default_tz}")
    print("  ✓ get_app_timezone defaults to 'Europe/Copenhagen'")
    total_passed += 1

    # Test 3b: Custom timezone resolution
    ny_tz = get_app_timezone(tz_name="America/New_York")
    check(str(ny_tz) == "America/New_York", f"Expected America/New_York, got {ny_tz}")
    tokyo_tz = get_app_timezone(custom_tz="Asia/Tokyo")
    check(str(tokyo_tz) == "Asia/Tokyo", f"Expected Asia/Tokyo, got {tokyo_tz}")
    print("  ✓ get_app_timezone successfully resolves custom tz_name and custom_tz")
    total_passed += 1

    # Test 3c: Winter Time (CET: UTC+1) in Copenhagen
    # 2026-01-15 12:00:00 UTC -> 13:00:00 Copenhagen
    winter_utc = "2026-01-15 12:00:00"
    winter_local = format_localtime(winter_utc, tz_name="Europe/Copenhagen")
    check(winter_local == "2026-01-15 13:00:00", f"Expected '2026-01-15 13:00:00', got '{winter_local}'")
    print("  ✓ Winter time correctly converted UTC -> CET (+1 hour)")
    total_passed += 1

    # Test 3d: Summer Daylight Saving Time (CEST: UTC+2) in Copenhagen
    # 2026-07-15 12:00:00 UTC -> 14:00:00 Copenhagen
    summer_utc = "2026-07-15 12:00:00"
    summer_local = format_localtime(summer_utc, tz_name="Europe/Copenhagen")
    check(summer_local == "2026-07-15 14:00:00", f"Expected '2026-07-15 14:00:00', got '{summer_local}'")
    print("  ✓ Summer time correctly converted UTC -> CEST (+2 hours with DST)")
    total_passed += 1

    # Test 3e: format_localdate
    local_date = format_localdate(summer_utc, tz_name="Europe/Copenhagen")
    check(local_date == "2026-07-15", f"Expected '2026-07-15', got '{local_date}'")
    print("  ✓ format_localdate formatted local date string correctly")
    total_passed += 1

    # Test 3f: Jinja2 filter registration in main.py & template engine
    try:
        import jinja2
        j_env = jinja2.Environment()
        j_env.filters["localtime"] = lambda val, fmt="%Y-%m-%d %H:%M:%S": format_localtime(val, fmt=fmt, tz_name="Europe/Copenhagen")
        j_env.filters["localdate"] = lambda val, fmt="%Y-%m-%d": format_localdate(val, fmt=fmt, tz_name="Europe/Copenhagen")
        rendered_time = j_env.from_string("{{ dt | localtime }}").render(dt="2026-01-15 12:00:00")
        check(rendered_time == "2026-01-15 13:00:00", f"Expected '2026-01-15 13:00:00', got '{rendered_time}'")
        rendered_date = j_env.from_string("{{ dt | localdate }}").render(dt="2026-07-15 12:00:00")
        check(rendered_date == "2026-07-15", f"Expected '2026-07-15', got '{rendered_date}'")
    except ImportError:
        pass

    main_py_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_py_path, "r", encoding="utf-8") as f:
        main_src = f.read()
    check('templates.env.filters["localtime"] = jinja_localtime' in main_src, "main.py missing localtime filter")
    check('templates.env.filters["localdate"] = jinja_localdate' in main_src, "main.py missing localdate filter")
    check('format_localtime' in main_src, "main.py missing format_localtime import")
    check('format_localdate' in main_src, "main.py missing format_localdate import")
    print("  ✓ Jinja2 filters 'localtime' and 'localdate' registered on templates.env.filters in main.py")
    total_passed += 1

    # --------------------------------------------------------------------------
    # 4. SQLite Metadata Cache & Sliding-Window Rate Limiting Tests
    # --------------------------------------------------------------------------
    print("\n--- 4. Testing SQLite Metadata Cache & Rate Limiting ---")
    tmp_db_dir = tempfile.mkdtemp(prefix="noveltrax_cache_test_")
    try:
        c_db_path = os.path.join(tmp_db_dir, "noveltrax.db")
        cache_db = Database(c_db_path)

        # Ensure cache table creation
        cache_db.ensure_metadata_cache_table()
        check(cache_db.get_cached_novel_metadata("nonexistent") is None, "Nonexistent item should return None")

        # Test caching
        sample_data = {
            "title": "DanMachi",
            "synopsis": "Bell Cranel is a solo adventurer...",
            "cover_url": "https://example.com/danmachi.jpg",
            "source": "AniList"
        }
        cache_db.cache_novel_metadata("danmachi", sample_data)
        cached_res = cache_db.get_cached_novel_metadata("danmachi")
        check(cached_res is not None, "Cached record should not be None")
        check(cached_res["title"] == "DanMachi", f"Expected title DanMachi, got {cached_res['title']}")
        check(cached_res["source"] == "AniList", f"Expected source AniList, got {cached_res['source']}")
        print("  ✓ Database.cache_novel_metadata and get_cached_novel_metadata verified")
        total_passed += 1

        # Test get_all_novel_metadata_cache and clear_novel_metadata_cache
        all_cache = cache_db.get_all_novel_metadata_cache()
        check("danmachi" in all_cache, "danmachi in all_cache")
        check(len(all_cache) == 1, f"Expected 1 item, got {len(all_cache)}")
        deleted = cache_db.clear_novel_metadata_cache()
        check(deleted == 1, f"Expected 1 deleted, got {deleted}")
        check(len(cache_db.get_all_novel_metadata_cache()) == 0, "Cache should be empty after clear")
        print("  ✓ get_all_novel_metadata_cache and clear_novel_metadata_cache verified")
        total_passed += 1

        # Test NovelMetadataProvider cache hit prevents network query
        cache_db.cache_novel_metadata("solo leveling", {
            "title": "Solo Leveling",
            "synopsis": "Sung Jinwoo rises from the weakest to the strongest hunter.",
            "cover_url": "https://example.com/solo.jpg",
            "source": "cache"
        })
        NovelMetadataProvider.set_db(cache_db)
        fetched = NovelMetadataProvider.fetch_novel_metadata("Solo Leveling Volume 1 Epub", db=cache_db)
        check(fetched is not None, "fetch_novel_metadata should return cached record")
        check(fetched["title"] == "Solo Leveling", f"Expected Solo Leveling, got {fetched['title']}")
        check(fetched["source"] == "cache", f"Expected cache source, got {fetched['source']}")
        print("  ✓ NovelMetadataProvider.fetch_novel_metadata successfully served from SQLite cache")
        total_passed += 1

        # Test SlidingWindowRateLimiter
        limiter = SlidingWindowRateLimiter("TestLimiter", max_requests=3, time_window=1.0)
        t0 = time.time()
        limiter.wait_if_needed()
        limiter.wait_if_needed()
        limiter.wait_if_needed()
        # 4th request must be throttled
        limiter.wait_if_needed()
        t1 = time.time()
        elapsed = t1 - t0
        check(elapsed >= 0.95, f"Limiter should have paused for sliding window, elapsed: {elapsed:.2f}s")
        print("  ✓ SlidingWindowRateLimiter throttles requests exceeding window capacity")
        total_passed += 1

        # Test 429 Retry-After handling
        headers = {"Retry-After": "0.3"}
        limiter.handle_rate_limit_error(headers)
        check(limiter.retry_until > time.time(), "retry_until should be in future")
        t_before = time.time()
        limiter.wait_if_needed()
        t_after = time.time()
        check((t_after - t_before) >= 0.25, f"Expected Retry-After pause >= 0.25s, got {t_after - t_before:.2f}s")
        print("  ✓ SlidingWindowRateLimiter respects HTTP 429 Retry-After cooldown header")
        total_passed += 1

    finally:
        shutil.rmtree(tmp_db_dir, ignore_errors=True)

    # --------------------------------------------------------------------------
    # 5. UI Top Progress Bar Verification
    # --------------------------------------------------------------------------
    print("\n--- 5. Testing UI Top Progress Bar in base.html ---")
    base_html_path = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
    check(os.path.isfile(base_html_path), "base.html exists")
    with open(base_html_path, "r", encoding="utf-8") as f:
        base_html = f.read()

    check('id="page-progress-bar"' in base_html, "base.html contains element #page-progress-bar")
    check('#page-progress-bar.loading' in base_html, "base.html contains loading CSS for #page-progress-bar")
    check('const ProgressBar =' in base_html, "base.html contains ProgressBar JavaScript controller")
    check("ProgressBar.start()" in base_html, "base.html activates ProgressBar on internal navigation")
    check("ProgressBar.done()" in base_html, "base.html finalizes ProgressBar on page show")
    print("  ✓ #page-progress-bar element, loading styles, and ProgressBar controller present in base.html")
    total_passed += 1

    # --------------------------------------------------------------------------
    # Summary
    # --------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print(f"  ALL {total_passed} MILESTONE 2 TESTS PASSED WITH 100% SUCCESS!")
    print("=" * 70)

if __name__ == "__main__":
    run_tests()
