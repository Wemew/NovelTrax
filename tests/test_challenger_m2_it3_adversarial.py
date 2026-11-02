"""
tests/test_challenger_m2_it3_adversarial.py

Milestone 2 Iteration 3: Empirical Adversarial Challenge Suite
Focus: Edge-Case Verification of parse_calibre_volumes_and_series & CalibreScanner
Tested dimensions:
1. Multiple hyphens in title & subtitles
2. Numeric years & numbered franchise titles
3. Delimiter combinations & compound separators
4. Reverse omnibus ranges & reverse logic guards
5. Mixed unicode dashes (en-dash, em-dash, tilde, slash, to)
6. Zero-lock CalibreScanner integration & sync_library database serialization
7. Non-strippable check() assertion verification under Python -O2
"""

import os
import sys
import json
import sqlite3
import tempfile
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.calibre.scanner import CalibreScanner, parse_calibre_volumes_and_series
from app.database import Database

def check(condition: bool, msg: str = ""):
    """Non-strippable assertion helper that survives Python -O/-O2 optimization."""
    if not condition:
        raise AssertionError(f"Check failed: {msg}")

class ChallengerRecorder:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def record_pass(self, name: str):
        self.passed += 1
        print(f"  [PASS] {name}")

    def record_fail(self, name: str, expected: any, actual: any, details: str = ""):
        self.failed += 1
        fail_msg = f"{name} | Expected: {expected} | Actual: {actual}"
        if details:
            fail_msg += f" | {details}"
        self.failures.append(fail_msg)
        print(f"  [FAIL] {fail_msg}")

    def summary(self) -> int:
        print("\n" + "=" * 70)
        print("EMPIRICAL CHALLENGER ADVERSARIAL SUITE SUMMARY")
        print(f"Passed: {self.passed} | Failed: {self.failed} | Total: {self.passed + self.failed}")
        if self.failures:
            print("\nFAILURES REPRODUCED:")
            for idx, f in enumerate(self.failures, 1):
                print(f"  {idx}. {f}")
        print("=" * 70)
        return self.failed

recorder = ChallengerRecorder()

# ==============================================================================
# 1. Multiple Hyphens in Title & Subtitles
# ==============================================================================
def test_multiple_hyphens():
    print("\n--- 1. Testing Multiple Hyphens in Title & Subtitles ---")
    cases = [
        # (title, series_name, series_index, expected_series, expected_vols)
        (
            "Re:Zero - Starting Life in Another World - Vol 1",
            None,
            None,
            "Re:Zero - Starting Life in Another World",
            {1.0}
        ),
        (
            "Sword Art Online - Progressive - Vol 2",
            None,
            None,
            "Sword Art Online - Progressive",
            {2.0}
        ),
        (
            "Fate/stay night - Unlimited Blade Works - Vol 1",
            None,
            None,
            "Fate/stay night - Unlimited Blade Works",
            {1.0}
        ),
        (
            "That Time I Got Reincarnated as a Slime - The Ways of the Monster Nation - Vol 3-5",
            None,
            None,
            "That Time I Got Reincarnated as a Slime - The Ways of the Monster Nation",
            {3.0, 4.0, 5.0}
        ),
        (
            "Re:Zero - Starting Life in Another World - Vol 4",
            "Re:Zero - Starting Life in Another World",
            1.0,  # Default Calibre index 1.0 overridden by Vol 4
            "Re:Zero - Starting Life in Another World",
            {4.0}
        ),
        (
            "Re:Zero - Starting Life in Another World - Vol 1-3",
            "Re:Zero - Starting Life in Another World",
            1.0,
            "Re:Zero - Starting Life in Another World",
            {1.0, 2.0, 3.0}
        ),
    ]

    for title, s_name, s_idx, exp_series, exp_vols in cases:
        series, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        if series == exp_series and vols == exp_vols:
            recorder.record_pass(f"Multi-hyphen '{title}' -> Series: '{series}', Vols: {vols}")
        else:
            recorder.record_fail(f"Multi-hyphen '{title}'", (exp_series, exp_vols), (series, vols))

# ==============================================================================
# 2. Numeric Years & Numbered Franchises
# ==============================================================================
def test_numeric_years_and_numbered_franchises():
    print("\n--- 2. Testing Numeric Years & Numbered Franchises ---")
    cases = [
        # (title, series_name, series_index, expected_series, expected_vols)
        ("Neon Genesis Evangelion 1995 - Vol 1", None, None, "Neon Genesis Evangelion 1995", {1.0}),
        ("Ghost in the Shell (1995) Vol 1", None, None, "Ghost in the Shell (1995)", {1.0}),
        ("Neon Genesis Evangelion 1995 - Vol 2", "Neon Genesis Evangelion 1995", 1.0, "Neon Genesis Evangelion 1995", {2.0}),
        ("Cyberpunk 2077 - Vol 1", None, None, "Cyberpunk 2077", {1.0}),
        ("Cyberpunk 2077 - Vol 2", "Cyberpunk 2077", 1.0, "Cyberpunk 2077", {2.0}),
        ("2001: A Space Odyssey Vol 1", None, None, "2001: A Space Odyssey", {1.0}),
        ("Blade Runner 2049 - Vol 1-3", None, None, "Blade Runner 2049", {1.0, 2.0, 3.0}),
        ("1984 - Vol 1", None, None, "1984", {1.0}),
        ("Catch-22 Vol 2", "Catch-22", 1.0, "Catch-22", {2.0}),
        ("Eureka Seven AO (2012) Vol 1", None, None, "Eureka Seven AO (2012)", {1.0}),
        ("Steins;Gate 0 - Vol 2", "Steins;Gate 0", 1.0, "Steins;Gate 0", {2.0}),
        ("Mob Psycho 100 II - Vol 1", None, None, "Mob Psycho 100 II", {1.0}),
        ("86 - Eighty Six - Vol 11", "86 - Eighty Six", 1.0, "86 - Eighty Six", {11.0}),
    ]

    for title, s_name, s_idx, exp_series, exp_vols in cases:
        series, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        if series == exp_series and vols == exp_vols:
            recorder.record_pass(f"Numbered franchise '{title}' -> Series: '{series}', Vols: {vols}")
        else:
            recorder.record_fail(f"Numbered franchise '{title}'", (exp_series, exp_vols), (series, vols))

# ==============================================================================
# 3. Delimiter Combinations & Compound Separators
# ==============================================================================
def test_delimiter_combinations():
    print("\n--- 3. Testing Delimiter Combinations & Compound Separators ---")
    cases = [
        # (title, series_name, series_index, expected_series, expected_vols)
        ("Title 100 -- Vol 1", None, None, "Title 100", {1.0}),
        ("Title 100 : Vol 1", None, None, "Title 100", {1.0}),
        ("Title 100 | Vol 1", None, None, "Title 100", {1.0}),
        ("Title 100 -- Vol 1-3", None, None, "Title 100", {1.0, 2.0, 3.0}),
        ("Title 100 : Vol 1-3", None, None, "Title 100", {1.0, 2.0, 3.0}),
        ("Title 100 | Vol 1-3", None, None, "Title 100", {1.0, 2.0, 3.0}),
        ("Title 100 --- Vol 1", None, None, "Title 100", {1.0}),
        ("Title 100 - - Vol 1", None, None, "Title 100", {1.0}),
        ("Title 100 :: Vol 1", None, None, "Title 100", {1.0}),
        ("Title 100 : Vol 2", "Title 100", 1.0, "Title 100", {2.0}),
    ]

    for title, s_name, s_idx, exp_series, exp_vols in cases:
        series, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        if series == exp_series and vols == exp_vols:
            recorder.record_pass(f"Delimiter combo '{title}' -> Series: '{series}', Vols: {vols}")
        else:
            recorder.record_fail(f"Delimiter combo '{title}'", (exp_series, exp_vols), (series, vols))

# ==============================================================================
# 4. Reverse Omnibus Ranges & Reverse Range Guards
# ==============================================================================
def test_reverse_omnibus_ranges():
    print("\n--- 4. Testing Reverse Omnibus Ranges & Reverse Range Guards ---")
    cases = [
        # (title, series_name, series_index, expected_series, expected_vols)
        ("Series Vol 3-1", None, None, "Series", {1.0, 2.0, 3.0}),
        ("Series Vol 10-8", None, None, "Series", {8.0, 9.0, 10.0}),
        ("Series [Vol 3-1]", None, None, "Series", {1.0, 2.0, 3.0}),
        ("Series (v05-02)", None, None, "Series", {2.0, 3.0, 4.0, 5.0}),
        ("Series Volume 4 to 1", None, None, "Series", {1.0, 2.0, 3.0, 4.0}),
        ("Series Book 5-2", None, None, "Series", {2.0, 3.0, 4.0, 5.0}),
        # Reversed unadorned range guarding (must NOT invert descending numbers without prefix)
        ("Mob Psycho 100 - 1", "Mob Psycho 100", 1.0, "Mob Psycho 100", {1.0}),
        ("Series 86 - 1", "Series 86", 1.0, "Series 86", {1.0}),
        ("Series [3-1]", None, None, "Series", set()),
    ]

    for title, s_name, s_idx, exp_series, exp_vols in cases:
        series, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        if series == exp_series and vols == exp_vols:
            recorder.record_pass(f"Reverse range '{title}' -> Series: '{series}', Vols: {vols}")
        else:
            recorder.record_fail(f"Reverse range '{title}'", (exp_series, exp_vols), (series, vols))

# ==============================================================================
# 5. Mixed Unicode Dashes & Range Connectors
# ==============================================================================
def test_unicode_dashes_and_range_connectors():
    print("\n--- 5. Testing Mixed Unicode Dashes & Range Connectors ---")
    cases = [
        # (title, series_name, series_index, expected_series, expected_vols)
        ("Series Vol 1\u20133", None, None, "Series", {1.0, 2.0, 3.0}),     # en-dash
        ("Series Vol 1\u20143", None, None, "Series", {1.0, 2.0, 3.0}),     # em-dash
        ("Series Vol 1~3", None, None, "Series", {1.0, 2.0, 3.0}),          # tilde
        ("Series Vol 1/3", None, None, "Series", {1.0, 2.0, 3.0}),          # slash
        ("Series Vol 1 to 3", None, None, "Series", {1.0, 2.0, 3.0}),       # word 'to'
        ("Series Vol 3\u20131", None, None, "Series", {1.0, 2.0, 3.0}),     # reverse en-dash
        ("Series Vol 10\u20148", None, None, "Series", {8.0, 9.0, 10.0}),   # reverse em-dash
        ("Series Vol 5~2", None, None, "Series", {2.0, 3.0, 4.0, 5.0}),     # reverse tilde
        ("Series [Vol 1\u20134]", None, None, "Series", {1.0, 2.0, 3.0, 4.0}),
        ("Series [1\u20143]", None, None, "Series", {1.0, 2.0, 3.0}),
        ("Mob Psycho 100 - Vol 1\u20133", "Mob Psycho 100", 1.0, "Mob Psycho 100", {1.0, 2.0, 3.0}),
        ("86 - Vol 1\u20144", "86", 1.0, "86", {1.0, 2.0, 3.0, 4.0}),
    ]

    for title, s_name, s_idx, exp_series, exp_vols in cases:
        series, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        if series == exp_series and vols == exp_vols:
            recorder.record_pass(f"Unicode dash '{title}' -> Series: '{series}', Vols: {vols}")
        else:
            recorder.record_fail(f"Unicode dash '{title}'", (exp_series, exp_vols), (series, vols))

# ==============================================================================
# 6. Calibre Zero-Lock Scanner Full Integration & sync_library
# ==============================================================================
def test_calibre_scanner_adversarial_integration():
    print("\n--- 6. Testing CalibreScanner Zero-Lock Integration & sync_library ---")
    tmp_dir = tempfile.mkdtemp(prefix="noveltrax_challenger_m2_it3_")
    try:
        calibre_dir = os.path.join(tmp_dir, "calibre")
        os.makedirs(calibre_dir, exist_ok=True)
        calibre_db_path = os.path.join(calibre_dir, "metadata.db")
        app_db_path = os.path.join(tmp_dir, "app_noveltrax.db")

        # Initialize mock Calibre metadata.db with full schema
        conn = sqlite3.connect(calibre_db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, series_index REAL, uuid TEXT);")
        cur.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT UNIQUE);")
        cur.execute("CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER);")
        cur.execute("CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT UNIQUE);")
        cur.execute("CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);")

        cur.execute("INSERT INTO authors (id, name) VALUES (1, 'Author Alpha');")

        mock_data = [
            # (series_id, series_name, book_id, book_title, series_index)
            (1, "Re:Zero - Starting Life in Another World", 1, "Re:Zero - Starting Life in Another World - Vol 1-3", 1.0),
            (1, "Re:Zero - Starting Life in Another World", 2, "Re:Zero - Starting Life in Another World - Vol 4", 1.0),
            (2, "Neon Genesis Evangelion 1995", 3, "Neon Genesis Evangelion 1995 - Vol 1-2", 1.0),
            (3, "Ghost in the Shell (1995)", 4, "Ghost in the Shell (1995) Vol 1", 1.0),
            (4, "Title 100", 5, "Title 100 -- Vol 1-3", 1.0),
            (5, "Mob Psycho 100", 6, "Mob Psycho 100 - Vol 1", 1.0),
            (5, "Mob Psycho 100", 7, "Mob Psycho 100 - Vol 2-3", 1.0),
            (6, "86 - Eighty Six", 8, "86 - Eighty Six - Vol 1-4", 1.0),
        ]

        seen_series = set()
        for s_id, s_name, b_id, b_title, s_idx in mock_data:
            if s_id not in seen_series:
                cur.execute("INSERT INTO series (id, name) VALUES (?, ?);", (s_id, s_name))
                seen_series.add(s_id)
            cur.execute("INSERT INTO books (id, title, series_index) VALUES (?, ?, ?);", (b_id, b_title, s_idx))
            cur.execute("INSERT INTO books_series_link (book, series) VALUES (?, ?);", (b_id, s_id))
            cur.execute("INSERT INTO books_authors_link (book, author) VALUES (?, 1);", (b_id,))

        conn.commit()
        conn.close()

        # Instantiate zero-lock scanner
        scanner = CalibreScanner(calibre_dir)
        check(scanner.is_available(), "CalibreScanner should be available")

        # Test get_series_volumes_map
        vmap = scanner.get_series_volumes_map()

        expected_map = {
            "re:zero - starting life in another world": {1.0, 2.0, 3.0, 4.0},
            "neon genesis evangelion 1995": {1.0, 2.0},
            "ghost in the shell (1995)": {1.0},
            "title 100": {1.0, 2.0, 3.0},
            "mob psycho 100": {1.0, 2.0, 3.0},
            "86 - eighty six": {1.0, 2.0, 3.0, 4.0},
        }

        for s_key, exp_vols in expected_map.items():
            actual_vols = vmap.get(s_key)
            if actual_vols == exp_vols:
                recorder.record_pass(f"vmap lookup '{s_key}' -> {actual_vols}")
            else:
                recorder.record_fail(f"vmap lookup '{s_key}'", exp_vols, actual_vols)

        # Test sync_library populating Database series_tracking
        db = Database(app_db_path)
        sync_res = scanner.sync_library(db)
        check(sync_res["synced"] is True, "sync_library should return synced=True")
        check(sync_res["series_synced"] == 6, f"Expected 6 series synced, got {sync_res['series_synced']}")
        recorder.record_pass("sync_library successfully synced all 6 edge-case series")

        all_tracked = db.get_all_series()
        tracked_by_name = {s["name"].lower(): s for s in all_tracked}

        for s_key, exp_vols in expected_map.items():
            check(s_key in tracked_by_name, f"Series '{s_key}' should be present in series_tracking")
            raw_json = tracked_by_name[s_key].get("owned_volumes_json") or "[]"
            vols_list = sorted(json.loads(raw_json))
            expected_list = sorted(list(exp_vols))
            if vols_list == expected_list:
                recorder.record_pass(f"DB owned_volumes_json for '{s_key}' -> {vols_list}")
            else:
                recorder.record_fail(f"DB owned_volumes_json for '{s_key}'", expected_list, vols_list)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

# ==============================================================================
# 7. Non-Strippable Assertion Verification
# ==============================================================================
def test_assertion_survives_optimization():
    print("\n--- 7. Testing Non-Strippable check() Assertions ---")
    print(f"  Current sys.flags.optimize: {sys.flags.optimize}")
    try:
        check(False, "Expected verification failure")
        recorder.record_fail("check(False) did not raise AssertionError", "AssertionError", "None")
    except AssertionError:
        recorder.record_pass("check() non-strippable assertion verified under runtime optimization")

# ==============================================================================
# Main Runner
# ==============================================================================
if __name__ == "__main__":
    print("=" * 70)
    print("  RUNNING MILESTONE 2 ITERATION 3: EMPIRICAL ADVERSARIAL CHALLENGER SUITE")
    print("=" * 70)

    test_multiple_hyphens()
    test_numeric_years_and_numbered_franchises()
    test_delimiter_combinations()
    test_reverse_omnibus_ranges()
    test_unicode_dashes_and_range_connectors()
    test_calibre_scanner_adversarial_integration()
    test_assertion_survives_optimization()

    failed = recorder.summary()
    if failed > 0:
        print(f"\n[-] {failed} tests failed! Adversarial challenge reproduced defects.")
        sys.exit(1)
    else:
        print("\n[+] ALL EMPIRICAL ADVERSARIAL CHALLENGER TESTS PASSED SUCCESSFULLY!")
        sys.exit(0)
