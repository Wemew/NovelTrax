r"""
tests/test_m3_challenger_stress.py
Empirical Challenger Stress Harness for Milestone 3 (Downloader & Queue Management).

Tests combinatorial edge cases:
1. Numbered series titles without series_name (86, Mob Psycho 100, Catch-22, Studio 54, Apollo 13, Gate 7)
2. Numbered series titles with release groups and series_name ([J-Novel Club] Mob Psycho 100, [Yen Press] Catch-22)
3. Multi-volume list/conjunction formats (Mushoku Tensei Vol 1 & 2, Re:Zero Vol 1 + 2, Konosuba Vol 1 and 2)
4. Missing volume deselection in apply_selective_volume_download (omnibus subset rule violation)
5. Fractional range lower bound (Vol 1.5-3 erroneously injecting 1.0)
6. Raw hex and Base32 hashes in extract_torrent_hash

Run via Calibre interpreter:
& "C:\Program Files\Calibre2\calibre-debug.exe" -e tests\test_m3_challenger_stress.py
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.downloader.qbit import (
    QBitClient,
    parse_torrent_filename_volumes,
    SelectiveDownloadResult,
)

passed_tests = 0
failed_tests = 0
findings = []

def record_result(test_name: str, passed: bool, details: str):
    global passed_tests, failed_tests
    if passed:
        passed_tests += 1
        print(f"  [PASS] {test_name}")
    else:
        failed_tests += 1
        findings.append({"test": test_name, "details": details})
        print(f"  [FAIL] {test_name}\n         Details: {details}")

print("\n" + "=" * 70)
print("  EMPIRICAL CHALLENGER STRESS HARNESS — MILESTONE 3")
print("=" * 70)

# ----------------------------------------------------------------------
# 1. Numbered titles without series_name
# ----------------------------------------------------------------------
print("\n--- 1. Numbered Series Filename Parsing (series_name=None) ---")

cases_numbered = [
    ("86 - Vol 01.epub", {1.0}, "86 Volume 1 parsed as single volume"),
    ("Mob Psycho 100 - Vol 02.pdf", {2.0}, "Mob Psycho 100 Volume 2 parsed as single volume"),
    ("Catch-22 - Vol 1-3.epub", {1.0, 2.0, 3.0}, "Catch-22 Omnibus Vol 1-3 parsed as {1, 2, 3}"),
    ("Studio 54 - Vol 05.epub", {5.0}, "Studio 54 Volume 5 parsed as single volume"),
    ("Apollo 13 - Vol 01.epub", {1.0}, "Apollo 13 Volume 1 parsed as single volume"),
    ("Gate 7 - Vol 01.epub", {1.0}, "Gate 7 Volume 1 parsed as single volume"),
]

for fname, expected, desc in cases_numbered:
    anc, vols = parse_torrent_filename_volumes(fname, series_name=None)
    passed = (vols == expected)
    record_result(
        desc,
        passed,
        f"Filename '{fname}' expected {expected}, but got {vols} (count: {len(vols)})"
    )

# ----------------------------------------------------------------------
# 2. Release groups with series_name
# ----------------------------------------------------------------------
print("\n--- 2. Release Group Tags with series_name Parameter ---")

cases_groups = [
    (
        "[J-Novel Club] Mob Psycho 100 - Vol 02.epub",
        "Mob Psycho 100",
        {2.0},
        "Group tag [J-Novel Club] with series_name='Mob Psycho 100'"
    ),
    (
        "[Yen Press] Catch-22 - Vol 1-3.epub",
        "Catch-22",
        {1.0, 2.0, 3.0},
        "Group tag [Yen Press] with series_name='Catch-22'"
    ),
    (
        "[Digital] 86 - Vol 01.epub",
        "86",
        {1.0},
        "Group tag [Digital] with series_name='86'"
    ),
]

for fname, sname, expected, desc in cases_groups:
    anc, vols = parse_torrent_filename_volumes(fname, series_name=sname)
    passed = (vols == expected)
    record_result(
        desc,
        passed,
        f"Filename '{fname}' (series_name='{sname}') expected {expected}, but got {vols}"
    )

# ----------------------------------------------------------------------
# 3. Multi-volume conjunctions & list formats (&, +, and, comma)
# ----------------------------------------------------------------------
print("\n--- 3. Multi-Volume Conjunction & List Formats ---")

cases_lists = [
    ("Mushoku Tensei Vol 1 & 2.epub", {1.0, 2.0}, "Conjunction '&' (Vol 1 & 2)"),
    ("Re:Zero Vol 1 + 2.epub", {1.0, 2.0}, "Plus '+' (Vol 1 + 2)"),
    ("Konosuba Vol 1 and 2.epub", {1.0, 2.0}, "Word 'and' (Vol 1 and 2)"),
    ("Overlord Vol 1, 2, 3.epub", {1.0, 2.0, 3.0}, "Comma list (Vol 1, 2, 3)"),
]

for fname, expected, desc in cases_lists:
    anc, vols = parse_torrent_filename_volumes(fname)
    passed = (vols == expected)
    record_result(
        desc,
        passed,
        f"Filename '{fname}' expected {expected}, but got {vols}"
    )

# ----------------------------------------------------------------------
# 4. Critical Invalidation Condition: Missing Volume Deselection
# ----------------------------------------------------------------------
print("\n--- 4. Invalidation Condition: Missing Volume Deselection in Omnibus Bundles ---")

class MockClient(QBitClient):
    def __init__(self, mock_files):
        super().__init__()
        self.mock_files = mock_files
        self.deselected_ids = []

    def get_torrent_files(self, torrent_hash):
        return self.mock_files

    def set_file_priorities(self, torrent_hash, file_indices, priority=0):
        if priority == 0:
            self.deselected_ids.extend(file_indices)
        return True

# Scenario A: User owns Vol 1, but is missing Vol 2. Torrent has "Mushoku Tensei Vol 1 & 2.epub"
client_a = MockClient([
    {"id": 0, "name": "Mushoku Tensei Vol 1 & 2.epub"},
    {"id": 1, "name": "Mushoku Tensei Vol 3.epub"},
])
res_a = client_a.apply_selective_volume_download("mockhash1", owned_volumes=[1.0])
deselected_vol2 = 0 in res_a["skipped_indices"]

record_result(
    "Omnibus Vol 1 & 2 KEPT when user owns Vol 1 but needs Vol 2",
    not deselected_vol2,
    f"File 0 (Mushoku Tensei Vol 1 & 2.epub) was ERRONEOUSLY DESELECTED (skipped_indices={res_a['skipped_indices']}) when user is missing Vol 2!"
)

# Scenario B: User owns Vols 1, 2, 3 of Catch-22. Torrent has Vol 1-3 and Vol 4-6. Vol 1-3 MUST be deselected.
client_b = MockClient([
    {"id": 0, "name": "Catch-22 - Vol 1-3.epub"},
    {"id": 1, "name": "Catch-22 - Vol 4-6.epub"},
])
res_b = client_b.apply_selective_volume_download("mockhash2", owned_volumes=[1.0, 2.0, 3.0])
skipped_vol1_3 = 0 in res_b["skipped_indices"]

record_result(
    "Omnibus Vol 1-3 DESELECTED when user owns Vols 1, 2, 3",
    skipped_vol1_3,
    f"File 0 (Catch-22 - Vol 1-3.epub) was NOT skipped (skipped_indices={res_b['skipped_indices']}) even though user owns all volumes {1.0, 2.0, 3.0}!"
)

# ----------------------------------------------------------------------
# 5. Fractional range lower bounds
# ----------------------------------------------------------------------
print("\n--- 5. Fractional Range Lower Bounds ---")

anc, vols_frac = parse_torrent_filename_volumes("Series Vol 1.5-3.epub")
passed_frac = (1.0 not in vols_frac and vols_frac == {1.5, 2.0, 3.0})
record_result(
    "Fractional range 'Vol 1.5-3' does not contain integer 1.0",
    passed_frac,
    f"Filename 'Series Vol 1.5-3.epub' erroneously included 1.0: {vols_frac}"
)

# ----------------------------------------------------------------------
# 6. Raw hex & Base32 hashes in extract_torrent_hash
# ----------------------------------------------------------------------
print("\n--- 6. Raw Torrent Hashes in extract_torrent_hash ---")

raw_hex = "4a5b6c7d8e9f0123456789abcdef0123456789ab"
res_raw_hex = QBitClient.extract_torrent_hash(raw_hex)
record_result(
    "extract_torrent_hash parses raw 40-character hex string",
    res_raw_hex == raw_hex.lower(),
    f"extract_torrent_hash('{raw_hex}') returned {res_raw_hex} (expected '{raw_hex}')"
)

raw_b32 = "JJZEYZLZJJZEYZLZJJZEYZLZJJZEYZLZ"
res_raw_b32 = QBitClient.extract_torrent_hash(raw_b32)
record_result(
    "extract_torrent_hash parses raw 32-character Base32 string",
    res_raw_b32 is not None and len(res_raw_b32) == 40,
    f"extract_torrent_hash('{raw_b32}') returned {res_raw_b32} (expected 40-char hex)"
)

# ----------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------
print("\n" + "=" * 70)
print(f"  HARNESS SUMMARY: {passed_tests} PASSED | {failed_tests} FAILED")
print("=" * 70)
print(f"Verdict: {'APPROVE' if failed_tests == 0 else 'REQUEST_CHANGES'}")

if failed_tests > 0:
    sys.exit(1)
