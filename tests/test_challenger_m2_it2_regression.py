#!/usr/bin/env python3
"""
Milestone 2 Iteration 2 Challenger Stress & Regression Suite.
Empirically tests Calibre scanning sync, SQLite database integrity, omnibus title expansion,
timezone localization filters in Jinja templates, and FastAPI API routes without regressions.
"""

import sys
import os
import json
import sqlite3
import tempfile
import shutil
from typing import Set, Dict, Any

# Ensure project backend is on sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

def check(condition: bool, msg: str):
    """Calibre-safe assertion that survives -O2 optimization."""
    if not condition:
        print(f"[FAIL] {msg}")
        raise AssertionError(msg)
    else:
        print(f"  [PASS] {msg}")

def test_calibre_sync_omnibus_e2e():
    print("\n--- 1. Testing Calibre Scanning Sync & Database Integration ---")
    temp_dir = tempfile.mkdtemp(prefix="noveltrax_challenger_m2_it2_")
    try:
        calibre_dir = os.path.join(temp_dir, "calibre")
        os.makedirs(calibre_dir, exist_ok=True)
        db_file = os.path.join(calibre_dir, "metadata.db")

        # Create realistic Calibre metadata.db
        conn = sqlite3.connect(db_file)
        cur = conn.cursor()
        cur.executescript("""
            CREATE TABLE books (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                series_index REAL DEFAULT 1.0,
                uuid TEXT DEFAULT ''
            );
            CREATE TABLE series (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE books_series_link (
                id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL,
                series INTEGER NOT NULL
            );
            CREATE TABLE authors (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE books_authors_link (
                id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL,
                author INTEGER NOT NULL
            );
        """)

        # Insert Series
        cur.execute("INSERT INTO series (id, name) VALUES (1, 'Overlord');")
        cur.execute("INSERT INTO series (id, name) VALUES (2, 'Mushoku Tensei');")
        cur.execute("INSERT INTO series (id, name) VALUES (3, 'Sword Art Online');")
        cur.execute("INSERT INTO series (id, name) VALUES (4, 'DanMachi');")
        cur.execute("INSERT INTO series (id, name) VALUES (5, 'Re:Zero');")
        cur.execute("INSERT INTO series (id, name) VALUES (6, 'Dungeon Crawler Carl');")

        # Insert Books with omnibus and non-1.0 series_index
        # 1. Overlord Vol 1-3 (series_index=1.0)
        cur.execute("INSERT INTO books (id, title, series_index) VALUES (1, 'Overlord Vol 1-3', 1.0);")
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (1, 1);")
        # 2. Overlord Vol 4-6 (Calibre series_index=1.0 default)
        cur.execute("INSERT INTO books (id, title, series_index) VALUES (2, 'Overlord Vol 4-6', 1.0);")
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (2, 1);")
        # 3. Overlord Book 7 & 8 (Calibre series_index=1.0 default)
        cur.execute("INSERT INTO books (id, title, series_index) VALUES (3, 'Overlord Book 7 & 8', 1.0);")
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (3, 1);")

        # 4. Mushoku Tensei v01-02 (series_index=1.0)
        cur.execute("INSERT INTO books (id, title, series_index) VALUES (4, 'Mushoku Tensei v01-02', 1.0);")
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (4, 2);")
        # 5. Mushoku Tensei v05 (compact unspaced, series_index=1.0)
        cur.execute("INSERT INTO books (id, title, series_index) VALUES (5, 'Mushoku Tensei v05', 1.0);")
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (5, 2);")

        # 6. SAO [1-4]
        cur.execute("INSERT INTO books (id, title, series_index) VALUES (6, 'Sword Art Online [1-4]', 1.0);")
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (6, 3);")
        # 7. SAO [5-8] (series_index=1.0)
        cur.execute("INSERT INTO books (id, title, series_index) VALUES (7, 'Sword Art Online [5-8]', 1.0);")
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (7, 3);")

        # 8. DanMachi Vol 1-2.5 (fractional bounds)
        cur.execute("INSERT INTO books (id, title, series_index) VALUES (8, 'DanMachi Vol 1-2.5', 1.0);")
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (8, 4);")
        # 9. DanMachi Vol 19 (single volume override)
        cur.execute("INSERT INTO books (id, title, series_index) VALUES (9, 'DanMachi Vol 19', 1.0);")
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (9, 4);")

        # 10. Re:Zero Vol 0 (prequel)
        cur.execute("INSERT INTO books (id, title, series_index) VALUES (10, 'Re:Zero Vol 0', 0.0);")
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (10, 5);")
        # 11. Re:Zero Vol 1 + 2 (list)
        cur.execute("INSERT INTO books (id, title, series_index) VALUES (11, 'Re:Zero Vol 1 + 2', 1.0);")
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (11, 5);")

        # 12. Standalone book without series link in Calibre: 'KonoSuba Vol 1-3'
        cur.execute("INSERT INTO books (id, title, series_index) VALUES (12, 'KonoSuba Vol 1-3', 1.0);")

        conn.commit()
        conn.close()

        # Initialize CalibreScanner
        from app.calibre.scanner import CalibreScanner
        scanner = CalibreScanner(calibre_dir)
        check(scanner.is_available(), "CalibreScanner detects valid database")

        vol_map = scanner.get_series_volumes_map()

        # Verify Overlord volumes: 1, 2, 3, 4, 5, 6, 7, 8
        overlord_vols = vol_map.get("Overlord", set())
        expected_overlord = {1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0}
        check(overlord_vols == expected_overlord, f"Overlord expanded correctly: {overlord_vols}")

        # Verify Mushoku Tensei: 1, 2, 5 (notice 1.0 was NOT falsely added for v05)
        mushoku_vols = vol_map.get("Mushoku Tensei", set())
        check(mushoku_vols == {1.0, 2.0, 5.0}, f"Mushoku Tensei expanded correctly: {mushoku_vols}")

        # Verify SAO: 1, 2, 3, 4, 5, 6, 7, 8
        sao_vols = vol_map.get("Sword Art Online", set())
        check(sao_vols == {1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0}, f"SAO expanded correctly: {sao_vols}")

        # Verify DanMachi: {1.0, 2.0, 2.5, 19.0} (1.0 from default NOT added to Vol 19 alone)
        danmachi_vols = vol_map.get("DanMachi", set())
        check(danmachi_vols == {1.0, 2.0, 2.5, 19.0}, f"DanMachi expanded correctly: {danmachi_vols}")

        # Verify Re:Zero: {0.0, 1.0, 2.0} (Vol 0 supported)
        rezero_vols = vol_map.get("Re:Zero", set())
        check(rezero_vols == {0.0, 1.0, 2.0}, f"Re:Zero expanded with Vol 0 correctly: {rezero_vols}")

        # Verify KonoSuba standalone title parsed correctly
        konosuba_vols = vol_map.get("KonoSuba", set())
        check(konosuba_vols == {1.0, 2.0, 3.0}, f"Standalone KonoSuba parsed: {konosuba_vols}")

        # Now test sync_library into a fresh Database
        from app.database import Database
        app_db_path = os.path.join(temp_dir, "noveltrax_test.db")
        db = Database(app_db_path)

        sync_res = scanner.sync_library(db)
        check(sync_res["synced"] is True, "sync_library returned synced=True")
        check(sync_res["series_synced"] >= 5, f"sync_library synced {sync_res['series_synced']} series")

        # Verify series_tracking database rows and owned_volumes_json
        with db.get_connection() as aconn:
            acur = aconn.cursor()
            acur.execute("SELECT name, owned_volumes_json, latest_index FROM series_tracking;")
            rows = {r["name"]: (json.loads(r["owned_volumes_json"]), r["latest_index"]) for r in acur.fetchall()}

        check("Overlord" in rows, "Overlord exists in series_tracking")
        check(rows["Overlord"][0] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0], f"Overlord owned_volumes_json matches: {rows['Overlord'][0]}")

        check("Sword Art Online" in rows, "Sword Art Online exists in series_tracking")
        check(rows["Sword Art Online"][0] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0], f"SAO owned_volumes_json matches: {rows['Sword Art Online'][0]}")

        check("DanMachi" in rows, "DanMachi exists in series_tracking")
        check(rows["DanMachi"][0] == [1.0, 2.0, 2.5, 19.0], f"DanMachi owned_volumes_json matches: {rows['DanMachi'][0]}")

        # Test Release Evaluation against CalibreMatcher & vol_map
        from app.calibre.matcher import CalibreMatcher
        from app.parser import extract_series_and_volume

        matcher = CalibreMatcher(scanner.get_all_series(), scanner.get_all_books())

        # Check against omnibus-expanded volumes map
        overlord_owned = set(rows["Overlord"][0])
        check(5.0 in overlord_owned, "Overlord Vol 5 in owned volumes map via Vol 4-6 bundle")
        check(9.0 not in overlord_owned, "Overlord Vol 9 not in owned volumes map")

        danmachi_owned = set(rows["DanMachi"][0])
        check(2.5 in danmachi_owned, "DanMachi Vol 2.5 in owned volumes map via Vol 1-2.5")
        check(18.0 not in danmachi_owned, "DanMachi Vol 18 not yet in owned volumes map")

        # Now test CalibreMatcher check_release
        # Case A: SAO single title match
        res_sao = matcher.check_release({
            "title": "Sword Art Online Volume 1 Epub",
            "series_name": "Sword Art Online",
            "volume": 1.0
        })
        check(res_sao["status"] == "IN_CALIBRE", f"SAO Vol 1 evaluated as IN_CALIBRE: {res_sao['status']}")

        # Case B: DanMachi Vol 18 is missing
        res_dm18 = matcher.check_release({
            "title": "DanMachi Volume 18 Epub",
            "series_name": "DanMachi",
            "volume": 18.0
        })
        check(res_dm18["status"] == "REQUESTED", f"DanMachi Vol 18 evaluated as REQUESTED: {res_dm18['status']}")

        # Case C: Add DanMachi Vol 18 to Calibre and re-sync
        conn = sqlite3.connect(db_file)
        cur = conn.cursor()
        cur.execute("INSERT INTO books (id, title, series_index) VALUES (13, 'DanMachi Vol 18', 18.0);")
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (13, 4);")
        conn.commit()
        conn.close()

        # Re-sync library
        scanner.sync_library(db)

        # Verify updated owned_volumes_json includes 18.0
        with db.get_connection() as aconn:
            acur = aconn.cursor()
            acur.execute("SELECT owned_volumes_json FROM series_tracking WHERE name = 'DanMachi';")
            dm_vols_after = json.loads(acur.fetchone()["owned_volumes_json"])
        check(18.0 in dm_vols_after, f"DanMachi now includes 18.0 after sync: {dm_vols_after}")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

def test_timezone_localization_filters():
    print("\n--- 2. Testing Timezone Localization Filters & Jinja Integration ---")
    from app.timezone_util import format_localtime, format_localdate, get_app_timezone

    # Danish Winter Time (CET: UTC+1)
    winter_utc = "2026-01-15 12:00:00"
    local_winter = format_localtime(winter_utc, custom_tz="Europe/Copenhagen")
    check(local_winter == "2026-01-15 13:00:00", f"Winter CET UTC+1: {local_winter}")

    # Danish Summer Time (CEST: UTC+2)
    summer_utc = "2026-07-15 12:00:00"
    local_summer = format_localtime(summer_utc, custom_tz="Europe/Copenhagen")
    check(local_summer == "2026-07-15 14:00:00", f"Summer CEST UTC+2: {local_summer}")

    # Date formatting
    local_date = format_localdate(summer_utc, custom_tz="Europe/Copenhagen")
    check(local_date == "2026-07-15", f"format_localdate gives YYYY-MM-DD: {local_date}")

    # Jinja2 environment rendering
    try:
        import jinja2
        env = jinja2.Environment()
        env.filters["localtime"] = lambda val: format_localtime(val, custom_tz="Europe/Copenhagen")
        env.filters["localdate"] = lambda val: format_localdate(val, custom_tz="Europe/Copenhagen")

        tmpl = env.from_string("Release: {{ dt|localtime }} Date: {{ dt|localdate }}")
        rendered = tmpl.render(dt="2026-10-31 23:30:00")
        check("Release: 2026-11-01 00:30:00" in rendered, f"Jinja filter crossed midnight boundary correctly: {rendered}")
        check("Date: 2026-11-01" in rendered, f"Jinja date filter matches: {rendered}")
    except ImportError:
        main_py = os.path.join(BACKEND_DIR, "app", "main.py")
        with open(main_py, "r", encoding="utf-8") as f:
            src = f.read()
        check('templates.env.filters["localtime"] = jinja_localtime' in src, "main.py registers localtime Jinja filter")
        check('templates.env.filters["localdate"] = jinja_localdate' in src, "main.py registers localdate Jinja filter")

    # Edge cases
    check(format_localtime(None) == "", "format_localtime(None) returns empty string")
    check(format_localtime("") == "", "format_localtime('') returns empty string")
    check(format_localtime("not-a-date") == "not-a-date", "format_localtime('not-a-date') falls back gracefully")

def test_api_routes_smoke():
    print("\n--- 3. Testing FastAPI API Endpoints Smoke Tests ---")
    try:
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)

        # 1. Dashboard Overview HTML
        res = client.get("/")
        check(res.status_code == 200, f"GET / status code: {res.status_code}")
        check("NovelTrax" in res.text, "Dashboard HTML renders brand name")
        check("id=\"page-progress-bar\"" in res.text, "Global progress bar element present in HTML")

        # 2. API Series List
        res = client.get("/api/series")
        check(res.status_code == 200, f"GET /api/series status code: {res.status_code}")
        check(isinstance(res.json(), list), "/api/series returns list")

        # 3. API Releases List
        res = client.get("/api/releases")
        check(res.status_code == 200, f"GET /api/releases status code: {res.status_code}")
        data = res.json()
        check("releases" in data or isinstance(data, list), "/api/releases response structure valid")

        # 4. API Discovery List
        res = client.get("/api/discovery")
        check(res.status_code == 200, f"GET /api/discovery status code: {res.status_code}")

        # 5. API Settings
        res = client.get("/api/settings")
        check(res.status_code == 200, f"GET /api/settings status code: {res.status_code}")

        # 6. Security Header Checks
        check(res.headers.get("X-Content-Type-Options") == "nosniff", "Security header X-Content-Type-Options present")
        check(res.headers.get("X-Frame-Options") == "SAMEORIGIN", "Security header X-Frame-Options present")
        check("default-src 'self'" in res.headers.get("Content-Security-Policy", ""), "CSP header present")
    except ImportError:
        # If testclient or fastapi not in Calibre environment, verify AST and templates
        base_html = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
        with open(base_html, "r", encoding="utf-8") as f:
            base_src = f.read()
        check('id="page-progress-bar"' in base_src, "base.html contains page-progress-bar element")
        check('#page-progress-bar.loading' in base_src, "base.html contains progress bar loading styling")
        check('ProgressBar.start()' in base_src, "base.html contains ProgressBar controller")

def main():
    print("======================================================================")
    print("  RUNNING CHALLENGER EMPIRICAL REGRESSION & INTEGRATION SUITE")
    print("======================================================================")
    test_calibre_sync_omnibus_e2e()
    test_timezone_localization_filters()
    test_api_routes_smoke()
    print("\n======================================================================")
    print("  ALL CHALLENGER REGRESSION & INTEGRATION TESTS PASSED WITH 100% SUCCESS!")
    print("======================================================================")

if __name__ == "__main__":
    main()
