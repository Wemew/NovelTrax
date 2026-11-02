"""
tests/test_challenger_m2_omnibus.py

Milestone 2 Empirical Adversarial Fuzzing and Stress Harness:
- Calibre Omnibus Title Parsing (Ranges, Lists, Single Overrides, Fractionals)
- Dual Series Title Expansion in CalibreScanner
- Extreme/Adversarial Inputs (Empty, Out-of-Bounds, Negative, Noise)
- Non-strippable check() Assertion Verification under Calibre -O2 Optimization
"""

import os
import sys
import json
import sqlite3
import tempfile
import shutil

# Ensure backend directory is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.calibre.scanner import CalibreScanner, parse_calibre_volumes_and_series

def check(condition: bool, msg: str = ""):
    """Non-strippable assertion helper that survives Python -O/-O2 optimization."""
    if not condition:
        raise AssertionError(f"Check failed: {msg}")

class TestResultRecorder:
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
        print(f"CHALLENGER STRESS & FUZZING TEST SUMMARY")
        print(f"Passed: {self.passed} | Failed: {self.failed} | Total: {self.passed + self.failed}")
        if self.failures:
            print("\nFAILURES REPRODUCED:")
            for idx, f in enumerate(self.failures, 1):
                print(f"  {idx}. {f}")
        print("=" * 70)
        return self.failed

recorder = TestResultRecorder()

# ==============================================================================
# 1. Volume Ranges Matrix
# ==============================================================================
def test_volume_ranges():
    print("\n--- 1. Testing Volume Ranges Matrix ---")
    cases = [
        ("Vol 1-5", "Test Series", 1.0, {1.0, 2.0, 3.0, 4.0, 5.0}),
        ("v01-03", "Test Series", 1.0, {1.0, 2.0, 3.0}),
        ("v1-v4", "Test Series", 1.0, {1.0, 2.0, 3.0, 4.0}),
        ("v01 - v03", "Test Series", 1.0, {1.0, 2.0, 3.0}),
        ("[1-3]", "Test Series", 1.0, {1.0, 2.0, 3.0}),
        ("[Vol 1-3]", "Test Series", 1.0, {1.0, 2.0, 3.0}),
        ("(v01-04)", "Test Series", 1.0, {1.0, 2.0, 3.0, 4.0}),
        ("Vol. 1-2", "Test Series", 1.0, {1.0, 2.0}),
        ("1-4", "Test Series", 1.0, {1.0, 2.0, 3.0, 4.0}),
        ("Vol 1 to 3", "Test Series", 1.0, {1.0, 2.0, 3.0}),
        ("Vol 1–3", "Test Series", 1.0, {1.0, 2.0, 3.0}),  # en-dash
        ("Vol 1—3", "Test Series", 1.0, {1.0, 2.0, 3.0}),  # em-dash
        ("Vol 1~3", "Test Series", 1.0, {1.0, 2.0, 3.0}),  # tilde
        ("Vol 3-1", "Test Series", 1.0, {1.0, 2.0, 3.0}),  # reversed
    ]

    for title_suffix, s_name, s_idx, expected_vols in cases:
        full_title = f"{s_name} {title_suffix}"
        _, vols = parse_calibre_volumes_and_series(full_title, s_name, s_idx)
        if vols == expected_vols:
            recorder.record_pass(f"Range '{title_suffix}' -> {vols}")
        else:
            recorder.record_fail(f"Range '{title_suffix}'", expected_vols, vols)

# ==============================================================================
# 2. Comma, Plus & Ampersand Lists Matrix
# ==============================================================================
def test_comma_plus_ampersand_lists():
    print("\n--- 2. Testing Comma/Plus/Ampersand Lists Matrix ---")
    cases = [
        ("Vol 1, 2, 3", "Series", 1.0, {1.0, 2.0, 3.0}),
        ("v01+v02+v03", "Series", 1.0, {1.0, 2.0, 3.0}),
        ("Vol 1 + 2", "Series", 1.0, {1.0, 2.0}),
        ("v1 & v2", "Series", 1.0, {1.0, 2.0}),
        ("Vol 1, 2", "Series", 1.0, {1.0, 2.0}),
        ("Book 1 & 2", "Series", 1.0, {1.0, 2.0}),
        ("Book 1, 2, 3", "Series", 1.0, {1.0, 2.0, 3.0}),
        ("Book 1 + 2", "Series", 1.0, {1.0, 2.0}),
        ("Bk 1, 2", "Series", 1.0, {1.0, 2.0}),
    ]

    for title_suffix, s_name, s_idx, expected_vols in cases:
        full_title = f"{s_name} {title_suffix}"
        _, vols = parse_calibre_volumes_and_series(full_title, s_name, s_idx)
        if vols == expected_vols:
            recorder.record_pass(f"List '{title_suffix}' -> {vols}")
        else:
            recorder.record_fail(f"List '{title_suffix}'", expected_vols, vols, "Failed to parse multi-volume list")

# ==============================================================================
# 3. Single Volume Override Matrix (Discard Calibre default series_index = 1.0)
# ==============================================================================
def test_single_volume_overrides():
    print("\n--- 3. Testing Single Volume Override Matrix ---")
    cases = [
        ("Series Title Vol 15", "Series Title", 1.0, {15.0}),
        ("Series Title Volume 15", "Series Title", 1.0, {15.0}),
        ("Series Title Vol. 15", "Series Title", 1.0, {15.0}),
        ("Series Title Book 15", "Series Title", 1.0, {15.0}),
        ("Series Title Bk 15", "Series Title", 1.0, {15.0}),
        ("Series Title v15", "Series Title", 1.0, {15.0}),
        ("Series Title v 15", "Series Title", 1.0, {15.0}),
        ("Series Title v05", "Series Title", 1.0, {5.0}),
        ("Series Title v01", "Series Title", 1.0, {1.0}),
        ("Series Title Vol 1", "Series Title", 1.0, {1.0}),
    ]

    for title, s_name, s_idx, expected_vols in cases:
        _, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        if vols == expected_vols:
            recorder.record_pass(f"Single '{title}' -> {vols}")
        else:
            recorder.record_fail(f"Single '{title}'", expected_vols, vols, "Default 1.0 not discarded or volume missed")

# ==============================================================================
# 4. Fractional Volumes Matrix
# ==============================================================================
def test_fractional_volumes():
    print("\n--- 4. Testing Fractional Volumes Matrix ---")
    cases = [
        ("Series Vol 7.5", "Series", 7.5, {7.5}),
        ("Series Vol 7.5", "Series", 1.0, {7.5}),     # Default 1.0 overridden by 7.5
        ("Series Vol 1.5", "Series", 1.0, {1.5}),     # Default 1.0 overridden by 1.5 (not {1.0, 1.5})
        ("Series v08.5", "Series", 1.0, {8.5}),       # Unspaced v fraction
        ("Series Vol 1-2.5", "Series", 1.0, {1.0, 2.0, 2.5}), # Range ending in fraction
    ]

    for title, s_name, s_idx, expected_vols in cases:
        _, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        if vols == expected_vols:
            recorder.record_pass(f"Fractional '{title}' -> {vols}")
        else:
            recorder.record_fail(f"Fractional '{title}'", expected_vols, vols, "Fractional volume parsing error")

# ==============================================================================
# 5. Dual Series Titles Matrix & Scanner Volume Map
# ==============================================================================
def test_dual_series_titles_and_scanner():
    print("\n--- 5. Testing Dual Series Titles & CalibreScanner Volumes Map ---")
    tmp_dir = tempfile.mkdtemp(prefix="noveltrax_dual_test_")
    try:
        calibre_db_path = os.path.join(tmp_dir, "metadata.db")
        conn = sqlite3.connect(calibre_db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, series_index REAL, uuid TEXT);")
        cur.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT UNIQUE);")
        cur.execute("CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER);")

        # 1. Series with pipe separator in Calibre series name
        cur.execute("INSERT INTO series (id, name) VALUES (1, 'KonoSuba | God Blessing on This Wonderful World');")
        cur.execute("INSERT INTO books (id, title, series_index) VALUES (1, 'KonoSuba | God Blessing on This Wonderful World Vol 1-3', 1.0);")
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (1, 1);")

        # 2. Series with slash separator in Calibre series name
        cur.execute("INSERT INTO series (id, name) VALUES (2, 'Dungeon ni Deai / Is It Wrong to Try to Pick Up Girls in a Dungeon?');")
        cur.execute("INSERT INTO books (id, title, series_index) VALUES (2, 'Dungeon ni Deai / Is It Wrong to Try to Pick Up Girls in a Dungeon? Vol 1-2', 1.0);")
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (2, 2);")

        conn.commit()
        conn.close()

        scanner = CalibreScanner(tmp_dir)
        check(scanner.is_available(), "Scanner should be available")
        vmap = scanner.get_series_volumes_map()

        # Check KonoSuba lookups
        expected_konosuba = {1.0, 2.0, 3.0}
        kono_keys = [
            "KonoSuba | God Blessing on This Wonderful World",
            "konosuba | god blessing on this wonderful world",
            "KonoSuba",
            "konosuba",
            "God Blessing on This Wonderful World",
            "god blessing on this wonderful world",
            "1", # series_id
            1,   # int series_id
        ]
        for k in kono_keys:
            if k in vmap and vmap[k] == expected_konosuba:
                recorder.record_pass(f"Dual title lookup key: {k!r} -> {vmap[k]}")
            else:
                recorder.record_fail(f"Dual title lookup key: {k!r}", expected_konosuba, vmap.get(k))

        # Check DanMachi lookups
        expected_danmachi = {1.0, 2.0}
        danmachi_keys = [
            "Dungeon ni Deai",
            "dungeon ni deai",
            "Is It Wrong to Try to Pick Up Girls in a Dungeon?",
            "is it wrong to try to pick up girls in a dungeon?",
            "2",
        ]
        for k in danmachi_keys:
            if k in vmap and vmap[k] == expected_danmachi:
                recorder.record_pass(f"Dual title lookup key: {k!r} -> {vmap[k]}")
            else:
                recorder.record_fail(f"Dual title lookup key: {k!r}", expected_danmachi, vmap.get(k))

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

# ==============================================================================
# 6. Extreme & Adversarial Inputs Matrix
# ==============================================================================
def test_extreme_and_adversarial_inputs():
    print("\n--- 6. Testing Extreme & Adversarial Inputs Matrix ---")

    # 6a. Empty and None titles
    try:
        s, vols = parse_calibre_volumes_and_series("", "Test Series", 1.0)
        check(vols == {1.0}, f"Expected {{1.0}}, got {vols}")
        recorder.record_pass("Empty title with series and series_index")
    except Exception as e:
        recorder.record_fail("Empty title with series and series_index", "No exception", str(e))

    try:
        s, vols = parse_calibre_volumes_and_series("", None, None)
        check(vols == set(), f"Expected set(), got {vols}")
        recorder.record_pass("Empty title with None series and None index")
    except Exception as e:
        recorder.record_fail("Empty title with None series and None index", "No exception", str(e))

    try:
        s, vols = parse_calibre_volumes_and_series("   ", "   ", None)
        check(vols == set(), f"Expected set(), got {vols}")
        recorder.record_pass("Whitespace-only title and series")
    except Exception as e:
        recorder.record_fail("Whitespace-only title and series", "No exception", str(e))

    # 6b. Huge ranges (guard against Memory Exhaustion / CPU hanging)
    try:
        s, vols = parse_calibre_volumes_and_series("Series Vol 1-50000", "Series", 1.0)
        # Must not expand 50,000 numbers into set
        check(len(vols) <= 2, f"Huge range 1-50000 should be rejected from full expansion, got {len(vols)} items")
        recorder.record_pass(f"Huge range 1-50000 safely bounded (size={len(vols)})")
    except Exception as e:
        recorder.record_fail("Huge range 1-50000", "Safely bounded set", str(e))

    try:
        s, vols = parse_calibre_volumes_and_series("Series Vol 1-500", "Series", 1.0)
        # Diff > 100 should not expand
        check(len(vols) <= 2, f"Diff > 100 should not expand, got {len(vols)} items")
        recorder.record_pass(f"Range diff > 100 safely bounded (size={len(vols)})")
    except Exception as e:
        recorder.record_fail("Range diff > 100", "Safely bounded set", str(e))

    # 6c. Zero and negative numbers
    try:
        s, vols = parse_calibre_volumes_and_series("Series Vol 0", "Series", None)
        check(0.0 in vols, f"Vol 0 should produce 0.0 volume, got {vols}")
        recorder.record_pass("Vol 0 recognized as 0.0")
    except Exception as e:
        recorder.record_fail("Vol 0", "0.0 in vols", str(e))

    try:
        s, vols = parse_calibre_volumes_and_series("Series Vol -5", "Series", None)
        check(-5.0 not in vols, f"Vol -5 should not produce negative volume, got {vols}")
        recorder.record_pass("Vol -5 ignored without generating negative volume")
    except Exception as e:
        recorder.record_fail("Vol -5", "-5.0 not in vols", str(e))

    # 6d. Non-numeric noise and special characters
    try:
        s, vols = parse_calibre_volumes_and_series("Series Vol ABC", "Series", 1.0)
        check(vols == {1.0}, f"Vol ABC should not produce numbers, got {vols}")
        recorder.record_pass("Vol ABC noise handled cleanly")
    except Exception as e:
        recorder.record_fail("Vol ABC noise", "{1.0}", str(e))

    try:
        s, vols = parse_calibre_volumes_and_series("Series !@#$%^&*()", "Series", 1.0)
        check(vols == {1.0}, f"Special chars should not crash, got {vols}")
        recorder.record_pass("Special characters handled cleanly")
    except Exception as e:
        recorder.record_fail("Special characters", "{1.0}", str(e))

# ==============================================================================
# 7. Non-Strippable Assertion Verification under Calibre -O2
# ==============================================================================
def test_non_strippable_assertions():
    print("\n--- 7. Testing Non-Strippable check() Assertions ---")
    opt_level = getattr(sys.flags, "optimize", 0)
    print(f"  Current sys.flags.optimize: {opt_level}")

    # Verify check() works and raises AssertionError
    passed_check = False
    try:
        check(True, "Should pass")
        passed_check = True
    except Exception:
        passed_check = False
    check(passed_check, "check(True) should not raise")

    caught_failure = False
    try:
        check(False, "Forced failure for verification")
    except AssertionError as e:
        caught_failure = True
        check("Forced failure for verification" in str(e), "Exception message intact")

    if caught_failure:
        recorder.record_pass("check() non-strippable assertion verified under runtime optimization")
    else:
        recorder.record_fail("check() non-strippable assertion", "AssertionError raised", "No exception raised!")

def run_all_challenger_tests():
    print("=" * 70)
    print("  RUNNING MILESTONE 2: EMPIRICAL OMNIBUS FUZZING CHALLENGER SUITE")
    print("=" * 70)

    test_volume_ranges()
    test_comma_plus_ampersand_lists()
    test_single_volume_overrides()
    test_fractional_volumes()
    test_dual_series_titles_and_scanner()
    test_extreme_and_adversarial_inputs()
    test_non_strippable_assertions()

    fail_count = recorder.summary()
    if fail_count > 0:
        print(f"\n[!] Empirical Challenger discovered {fail_count} failure(s) in Omnibus Parsing!")
        sys.exit(1)
    else:
        print("\n[+] All Challenger Fuzzing and Stress tests passed successfully!")
        sys.exit(0)

if __name__ == "__main__":
    run_all_challenger_tests()
