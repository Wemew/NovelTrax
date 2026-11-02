"""
tests/test_challenger_m2_fuzzing.py

Milestone 2 Iteration 2: Empirical Challenger Fuzzing & Edge-Case Stress Suite
Focus Areas:
1. High volume numbers (Vol 100, Vol 999, Vol 2000, Vol 2001 out-of-bounds, Vol 100-105 range, Vol 1998-1999 year disambiguation)
2. Multi-token variations (Book 1, Book 2, Book 3; v1 + v2 + v3; [Vol. 01 - Vol. 03]; Bk. 1, Bk. 2)
3. Non-English & diverse punctuation (–, —, ~, /, combinations with brackets and ranges)
4. Volume 0 combinations (Vol 0, [0-3], Vol 0 + 1, Vol 0.5, Vol 0-2, v0, Book 0)
5. Default 1.0 discard behavior (series_index=1.0 vs series_index=5.0 with various volume titles)
6. Complex multi-volume whitespace, casing, and delimiter fuzzing
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.calibre.scanner import parse_calibre_volumes_and_series

def check(cond: bool, msg: str = ""):
    if not cond:
        raise AssertionError(f"Check failed: {msg}")

class FuzzResultRecorder:
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
        print("EMPIRICAL CHALLENGER FUZZING & STRESS HARNESS SUMMARY")
        print(f"Passed: {self.passed} | Failed: {self.failed} | Total: {self.passed + self.failed}")
        if self.failures:
            print("\nFAILURES REPRODUCED:")
            for idx, f in enumerate(self.failures, 1):
                print(f"  {idx}. {f}")
        print("=" * 70)
        return self.failed

recorder = FuzzResultRecorder()

# ==============================================================================
# 1. High Volume Numbers & Out-of-Bounds Stress Matrix
# ==============================================================================
def test_high_volume_numbers():
    print("\n--- 1. Testing High Volume Numbers & Bounds Matrix ---")
    cases = [
        ("Series Vol 100", "Series", 1.0, {100.0}, "Vol 100 should parse and discard default 1.0"),
        ("Series Volume 999", "Series", 1.0, {999.0}, "Vol 999 should parse and discard default 1.0"),
        ("Series Vol 2000", "Series", 1.0, {2000.0}, "Vol 2000 is maximum valid volume boundary"),
        ("Series Vol 2001", "Series", 1.0, {1.0}, "Vol 2001 is out of bounds (> 2000), should be ignored"),
        ("Series Vol 2001", "Series", None, set(), "Vol 2001 out of bounds with None index produces empty set"),
        ("Series Vol 100-103", "Series", 1.0, {100.0, 101.0, 102.0, 103.0}, "High volume range 100-103"),
        ("Series [Vol 997-1000]", "Series", 1.0, {997.0, 998.0, 999.0, 1000.0}, "Bracketed high volume range 997-1000"),
        ("Series (1998-1999)", "Series", 1.0, {1.0}, "Publication year without volume marker should not expand"),
        ("Series [Vol 1998-1999]", "Series", 1.0, {1998.0, 1999.0}, "Explicit volume marker on year-sized volume expands"),
        ("Series Vol 1-2005", "Series", 1.0, {1.0}, "Range exceeding bounds (2005 > 2000, diff > 100) rejected"),
    ]

    for title, s_name, s_idx, expected, desc in cases:
        _, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        if vols == expected:
            recorder.record_pass(f"{desc}: '{title}' -> {vols}")
        else:
            recorder.record_fail(f"{desc}: '{title}'", expected, vols)

# ==============================================================================
# 2. Multi-Token Variations Matrix
# ==============================================================================
def test_multi_token_variations():
    print("\n--- 2. Testing Multi-Token Variations Matrix ---")
    cases = [
        ("Series Book 1, Book 2, Book 3", "Series", 1.0, {1.0, 2.0, 3.0}, "Multi-token Book list with commas"),
        ("Series v1 + v2 + v3", "Series", 1.0, {1.0, 2.0, 3.0}, "Multi-token v list with pluses"),
        ("Series [Vol. 01 - Vol. 03]", "Series", 1.0, {1.0, 2.0, 3.0}, "Bracketed with Vol. prefixes on both ends"),
        ("Series [v01 - v03]", "Series", 1.0, {1.0, 2.0, 3.0}, "Bracketed with v prefixes on both ends"),
        ("Series [Bk. 1 - Bk. 3]", "Series", 1.0, {1.0, 2.0, 3.0}, "Bracketed with Bk. prefixes on both ends"),
        ("Series Vol 1, Vol 2, Vol 3", "Series", 1.0, {1.0, 2.0, 3.0}, "Repeated Vol keyword in list"),
        ("Series Vol. 1 & Vol. 2", "Series", 1.0, {1.0, 2.0}, "Repeated Vol. with ampersand"),
        ("Series Bk 1 + Bk 2", "Series", 1.0, {1.0, 2.0}, "Bk prefix with plus"),
        ("Series Volume 1 & Volume 2 & Volume 3", "Series", 1.0, {1.0, 2.0, 3.0}, "Repeated full Volume keyword with ampersands"),
        ("Series Book 1, 2, Book 3", "Series", 1.0, {1.0, 2.0, 3.0}, "Mixed token keyword in list"),
    ]

    for title, s_name, s_idx, expected, desc in cases:
        _, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        if vols == expected:
            recorder.record_pass(f"{desc}: '{title}' -> {vols}")
        else:
            recorder.record_fail(f"{desc}: '{title}'", expected, vols)

# ==============================================================================
# 3. Non-English & Diverse Punctuation Matrix
# ==============================================================================
def test_non_english_punctuation():
    print("\n--- 3. Testing Non-English & Diverse Punctuation Matrix ---")
    cases = [
        ("Series Vol 1–3", "Series", 1.0, {1.0, 2.0, 3.0}, "En-dash standard range"),
        ("Series Vol 1—3", "Series", 1.0, {1.0, 2.0, 3.0}, "Em-dash standard range"),
        ("Series Vol 1~3", "Series", 1.0, {1.0, 2.0, 3.0}, "Tilde standard range"),
        ("Series Vol 1/3", "Series", 1.0, {1.0, 2.0, 3.0}, "Slash standard range"),
        ("Series [1–3]", "Series", 1.0, {1.0, 2.0, 3.0}, "Bracket en-dash"),
        ("Series [1—3]", "Series", 1.0, {1.0, 2.0, 3.0}, "Bracket em-dash"),
        ("Series [1~3]", "Series", 1.0, {1.0, 2.0, 3.0}, "Bracket tilde"),
        ("Series [1/3]", "Series", 1.0, {1.0, 2.0, 3.0}, "Bracket slash"),
        ("Series (v01–v03)", "Series", 1.0, {1.0, 2.0, 3.0}, "Parenthesized en-dash with v prefix"),
        ("Series (v01—v03)", "Series", 1.0, {1.0, 2.0, 3.0}, "Parenthesized em-dash with v prefix"),
        ("Series (v01~v03)", "Series", 1.0, {1.0, 2.0, 3.0}, "Parenthesized tilde with v prefix"),
        ("Series (v01/v03)", "Series", 1.0, {1.0, 2.0, 3.0}, "Parenthesized slash with v prefix"),
    ]

    for title, s_name, s_idx, expected, desc in cases:
        _, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        if vols == expected:
            recorder.record_pass(f"{desc}: '{title}' -> {vols}")
        else:
            recorder.record_fail(f"{desc}: '{title}'", expected, vols)

# ==============================================================================
# 4. Volume 0 Combinations Matrix
# ==============================================================================
def test_volume_zero_combinations():
    print("\n--- 4. Testing Volume 0 Combinations Matrix ---")
    cases = [
        ("Series Vol 0", "Series", None, {0.0}, "Vol 0 with None index"),
        ("Series Vol 0", "Series", 1.0, {0.0}, "Vol 0 with default 1.0 index (1.0 discarded)"),
        ("Series [0-3]", "Series", 1.0, {0.0, 1.0, 2.0, 3.0}, "Bracketed 0-3 range"),
        ("Series Vol 0 + 1", "Series", 1.0, {0.0, 1.0}, "Vol 0 + 1 list"),
        ("Series Vol 0, 1, 2", "Series", 1.0, {0.0, 1.0, 2.0}, "Vol 0, 1, 2 list"),
        ("Series Vol 0.5", "Series", 1.0, {0.5}, "Vol 0.5 fractional prequel (1.0 discarded)"),
        ("Series Vol 0-2", "Series", 1.0, {0.0, 1.0, 2.0}, "Standard range Vol 0-2"),
        ("Series v0", "Series", 1.0, {0.0}, "Compact v0 token (1.0 discarded)"),
        ("Series Book 0", "Series", 1.0, {0.0}, "Book 0 token (1.0 discarded)"),
        ("Series [0.0 - 2.0]", "Series", None, {0.0, 1.0, 2.0}, "Explicit decimal bracket range [0.0 - 2.0]"),
        ("Series", "Series", 0.0, {0.0}, "No volume in title, series_index=0.0 should be preserved"),
    ]

    for title, s_name, s_idx, expected, desc in cases:
        _, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        if vols == expected:
            recorder.record_pass(f"{desc}: '{title}' -> {vols}")
        else:
            recorder.record_fail(f"{desc}: '{title}'", expected, vols)

# ==============================================================================
# 5. Default 1.0 Discard vs series_index=5.0 Matrix
# ==============================================================================
def test_default_discard_vs_custom_index():
    print("\n--- 5. Testing Default 1.0 Discard vs series_index=5.0 Matrix ---")
    cases = [
        # Discard 1.0 when title explicitly indicates non-1 volume
        ("Overlord Vol 5", "Overlord", 1.0, {5.0}, "Vol 5 with series_index=1.0: 1.0 must be discarded"),
        ("Overlord Vol 5", "Overlord", 5.0, {5.0}, "Vol 5 with series_index=5.0: produces {5.0}"),
        ("Overlord Vol 4-6", "Overlord", 1.0, {4.0, 5.0, 6.0}, "Vol 4-6 with series_index=1.0: 1.0 must be discarded"),
        ("Overlord Vol 4-6", "Overlord", 5.0, {4.0, 5.0, 6.0}, "Vol 4-6 with series_index=5.0: 5.0 is in range"),
        ("Overlord Vol 2", "Overlord", 1.0, {2.0}, "Vol 2 with series_index=1.0: 1.0 must be discarded"),
        ("Overlord Vol 2", "Overlord", 5.0, {2.0, 5.0}, "Vol 2 with series_index=5.0: custom index 5.0 preserved"),
        # Title includes 1.0
        ("Overlord Vol 1-3", "Overlord", 1.0, {1.0, 2.0, 3.0}, "Vol 1-3 with series_index=1.0: 1.0 kept because in title"),
        ("Overlord Vol 1-3", "Overlord", 5.0, {1.0, 2.0, 3.0, 5.0}, "Vol 1-3 with series_index=5.0: custom index 5.0 preserved"),
        # Title has NO volume
        ("Overlord", "Overlord", 1.0, {1.0}, "No volume in title with series_index=1.0: 1.0 preserved"),
        ("Overlord", "Overlord", 5.0, {5.0}, "No volume in title with series_index=5.0: 5.0 preserved"),
        ("Overlord", "Overlord", None, set(), "No volume in title with series_index=None: empty set"),
        ("Overlord", "Overlord", -1.0, set(), "Negative series_index ignored: empty set"),
    ]

    for title, s_name, s_idx, expected, desc in cases:
        _, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        if vols == expected:
            recorder.record_pass(f"{desc}: (index={s_idx}) -> {vols}")
        else:
            recorder.record_fail(f"{desc}: (index={s_idx})", expected, vols)

# ==============================================================================
# 6. Title Series Name Cleaning & Normalization Matrix
# ==============================================================================
def test_series_name_cleaning():
    print("\n--- 6. Testing Series Name Cleaning Matrix ---")
    cases = [
        ("Sword Art Online Vol 1-3", None, 1.0, "Sword Art Online", {1.0, 2.0, 3.0}),
        ("Overlord [Vol 1-4]", None, 1.0, "Overlord", {1.0, 2.0, 3.0, 4.0}),
        ("Mushoku Tensei - Vol 05", None, 1.0, "Mushoku Tensei", {5.0}),
        ("Ascendance of a Bookworm: Part 3 Volume 1", None, 1.0, "Ascendance of a Bookworm: Part 3", {1.0}),
        ("No Game No Life, Vol. 11", None, 1.0, "No Game No Life", {11.0}),
    ]

    for title, s_name, s_idx, expected_series, expected_vols in cases:
        detected_series, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        series_match = (detected_series == expected_series)
        vols_match = (vols == expected_vols)
        if series_match and vols_match:
            recorder.record_pass(f"Clean series & vols: '{title}' -> Series: '{detected_series}', Vols: {vols}")
        else:
            recorder.record_fail(
                f"Clean series & vols: '{title}'",
                f"Series: '{expected_series}', Vols: {expected_vols}",
                f"Series: '{detected_series}', Vols: {vols}"
            )

def run_all():
    print("=" * 70)
    print("  RUNNING EXTENDED EMPIRICAL FUZZING & EDGE-CASE STRESS SUITE")
    print("=" * 70)

    test_high_volume_numbers()
    test_multi_token_variations()
    test_non_english_punctuation()
    test_volume_zero_combinations()
    test_default_discard_vs_custom_index()
    test_series_name_cleaning()

    failures = recorder.summary()
    if failures > 0:
        print(f"\n[!] Empirical Challenger found {failures} failure(s)!")
        sys.exit(1)
    else:
        print("\n[+] All Extended Empirical Fuzzing and Stress tests passed successfully!")
        sys.exit(0)

if __name__ == "__main__":
    run_all()
