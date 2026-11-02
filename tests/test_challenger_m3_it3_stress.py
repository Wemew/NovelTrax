"""
Empirical Challenger Stress Harness — Milestone 3, Iteration 3
Deep Stress Probing & Boundary Verification:
1. Oxford comma and multi-volume list variants across qbit, scanner, and parser
2. Titles with Oxford commas in the title text vs volume numbers
3. Info-hash boundary extraction in extract_torrent_hash (overflows, truncations, positions)
4. Full selective download simulation with multi-volume bundles
"""

import sys
import os
import base64

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))

from app.downloader.qbit import (
    parse_torrent_filename_volumes,
    QBitClient,
    ConnectionTestResult,
    SelectiveDownloadResult
)
from app.calibre.scanner import parse_calibre_volumes_and_series
from app.parser import extract_series_and_volume


class ChallengerRecorder:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def check(self, condition: bool, test_name: str, details: str = ""):
        if condition:
            self.passed += 1
            print(f"  [PASS] {test_name}")
        else:
            self.failed += 1
            self.failures.append({"test": test_name, "details": details})
            print(f"  [FAIL] {test_name}\n         Details: {details}")


def run_it3_stress_suite():
    r = ChallengerRecorder()

    print("\n" + "=" * 78)
    print("  EMPIRICAL CHALLENGER STRESS HARNESS — MILESTONE 3 ITERATION 3")
    print("=" * 78)

    # -------------------------------------------------------------------------
    # 1. Oxford Comma and Multi-Volume List Variants
    # -------------------------------------------------------------------------
    print("\n--- 1. Oxford Comma & Multi-Volume List Variants (qbit.py) ---")

    cases_qbit = [
        ("[Yen Press] DanMachi Vol 1, 2, and 3.epub", {1.0, 2.0, 3.0}, "DanMachi Vol 1, 2, and 3"),
        ("Overlord v1, 2, 3, and 4.epub", {1.0, 2.0, 3.0, 4.0}, "Overlord v1, 2, 3, and 4"),
        ("Ascendance of a Bookworm Books 1, 2, & 3.epub", {1.0, 2.0, 3.0}, "Bookworm Books 1, 2, & 3"),
        ("Slime Volume 1, 2, + 3.epub", {1.0, 2.0, 3.0}, "Slime Volume 1, 2, + 3"),
        ("Series Vol 1, 2, 3, 4, + 5.epub", {1.0, 2.0, 3.0, 4.0, 5.0}, "Series Vol 1, 2, 3, 4, + 5"),
        ("The Lion, the Witch, and the Wardrobe - Vol 1.epub", {1.0}, "The Lion, the Witch, and the Wardrobe - Vol 1"),
        ("DanMachi Vol 1, Vol 2, and Vol 3.epub", {1.0, 2.0, 3.0}, "Repeating keyword 'Vol 1, Vol 2, and Vol 3'"),
        ("Mushoku Tensei Vol 1.5, 2, and 3.epub", {1.5, 2.0, 3.0}, "Decimals in Oxford list 'Vol 1.5, 2, and 3'"),
        ("ReZero Vol 1 and 2 and 3.epub", {1.0, 2.0, 3.0}, "Chained 'and' without commas 'Vol 1 and 2 and 3'"),
    ]

    for fname, expected, desc in cases_qbit:
        _, vols = parse_torrent_filename_volumes(fname)
        r.check(vols == expected, f"qbit parse: {desc}", f"Expected {expected}, got {vols} from '{fname}'")

    print("\n--- 2. Oxford Comma & Multi-Volume List Variants (calibre/scanner.py) ---")
    for fname, expected, desc in cases_qbit:
        # scanner extracts (series, vols)
        s_name, s_vols = parse_calibre_volumes_and_series(fname)
        r.check(s_vols == expected, f"scanner parse: {desc}", f"Expected {expected}, got {s_vols} from '{fname}'")

    print("\n--- 3. Oxford Comma & Multi-Volume List Variants (parser.py) ---")
    cases_parser = [
        ("DanMachi Vol 1, 2, and 3", [1.0, 2.0, 3.0], True),
        ("Overlord v1, 2, 3, and 4", [1.0, 2.0, 3.0, 4.0], True),
        ("Ascendance of a Bookworm Books 1, 2, & 3", [1.0, 2.0, 3.0], True),
        ("Slime Volume 1, 2, + 3", [1.0, 2.0, 3.0], True),
        ("Series Vol 1, 2, 3, 4, + 5", [1.0, 2.0, 3.0, 4.0, 5.0], True),
        ("The Lion, the Witch, and the Wardrobe - Vol 1", [1.0], False),
    ]
    for raw_title, expected_vols, expect_bundle in cases_parser:
        res = extract_series_and_volume(raw_title)
        if expect_bundle:
            is_b = res.get("is_bundle", False)
            vols = res.get("volumes", [])
            r.check(is_b and vols == expected_vols, f"parser bundle: {raw_title}", f"Expected bundle with {expected_vols}, got is_bundle={is_b}, volumes={vols}")
        else:
            vol = res.get("volume")
            is_b = res.get("is_bundle", False)
            r.check((not is_b) and (vol == expected_vols[0]), f"parser single: {raw_title}", f"Expected single vol {expected_vols[0]}, got is_bundle={is_b}, vol={vol}")

    # -------------------------------------------------------------------------
    # 4. Info-Hash Boundary Probing in extract_torrent_hash
    # -------------------------------------------------------------------------
    print("\n--- 4. Info-Hash Boundary Probing (extract_torrent_hash) ---")

    valid_hex_40_lower = "4a5b6c7d8e9f0123456789abcdef0123456789ab"
    valid_hex_40_upper = "4A5B6C7D8E9F0123456789ABCDEF0123456789AB"
    valid_b32_32_upper = "JJZEYZLZJJZEYZLZJJZEYZLZJJZEYZLZ"
    valid_b32_32_lower = "jjzeyzlzjjzeyzlzjjzeyzlzjjzeyzlz"
    expected_b32_hex = "4a724c65794a724c65794a724c65794a724c6579"

    # 4.1 Valid Hash Normalization
    r.check(
        QBitClient.extract_torrent_hash(f"magnet:?dn=Test&xt=urn:btih:{valid_hex_40_lower}&tr=udp://tracker") == valid_hex_40_lower,
        "Magnet with params before and after BTIH (40-hex lowercase)"
    )
    r.check(
        QBitClient.extract_torrent_hash(f"magnet:?dn=Test&xt=urn:btih:{valid_hex_40_upper}&tr=udp://tracker") == valid_hex_40_lower,
        "Magnet with params before and after BTIH (40-hex uppercase)"
    )
    r.check(
        QBitClient.extract_torrent_hash(f"magnet:?dn=Test&xt=urn:btih:{valid_b32_32_upper}&tr=udp://tracker") == expected_b32_hex,
        "Magnet with params before and after BTIH (32-Base32 uppercase)"
    )
    r.check(
        QBitClient.extract_torrent_hash(f"magnet:?dn=Test&xt=urn:btih:{valid_b32_32_lower}&tr=udp://tracker") == expected_b32_hex,
        "Magnet with params before and after BTIH (32-Base32 lowercase)"
    )

    # 4.2 Malformed / Boundary Invalidation (Must return None)
    # Overflow 43-hex in magnet
    m_overflow_43 = f"magnet:?xt=urn:btih:{valid_hex_40_lower}def&dn=Test"
    r.check(
        QBitClient.extract_torrent_hash(m_overflow_43) is None,
        "43-hex overflow hash in magnet link returns None",
        f"Got: {QBitClient.extract_torrent_hash(m_overflow_43)}"
    )

    # Truncated 39-hex in magnet
    m_trunc_39 = f"magnet:?xt=urn:btih:{valid_hex_40_lower[:39]}&dn=Test"
    r.check(
        QBitClient.extract_torrent_hash(m_trunc_39) is None,
        "Truncated 39-hex hash in magnet link returns None",
        f"Got: {QBitClient.extract_torrent_hash(m_trunc_39)}"
    )

    # Truncated 35-hex in magnet
    m_trunc_35 = f"magnet:?xt=urn:btih:{valid_hex_40_lower[:35]}&dn=Test"
    r.check(
        QBitClient.extract_torrent_hash(m_trunc_35) is None,
        "Truncated 35-hex hash in magnet link returns None",
        f"Got: {QBitClient.extract_torrent_hash(m_trunc_35)}"
    )

    # Truncated 31-Base32 in magnet
    m_trunc_b32 = f"magnet:?xt=urn:btih:{valid_b32_32_upper[:31]}&dn=Test"
    r.check(
        QBitClient.extract_torrent_hash(m_trunc_b32) is None,
        "Truncated 31-Base32 hash in magnet link returns None",
        f"Got: {QBitClient.extract_torrent_hash(m_trunc_b32)}"
    )

    # Bare boundary tests
    r.check(QBitClient.extract_torrent_hash(valid_hex_40_lower) == valid_hex_40_lower, "Bare 40-hex lowercase returns hash")
    r.check(QBitClient.extract_torrent_hash(valid_hex_40_upper) == valid_hex_40_lower, "Bare 40-hex uppercase returns normalized lower hash")
    r.check(QBitClient.extract_torrent_hash(valid_b32_32_upper) == expected_b32_hex, "Bare 32-Base32 uppercase returns decoded 40-hex")
    r.check(QBitClient.extract_torrent_hash(valid_b32_32_lower) == expected_b32_hex, "Bare 32-Base32 lowercase returns decoded 40-hex")

    # Bare invalid/overflow
    r.check(QBitClient.extract_torrent_hash(valid_hex_40_lower + "123") is None, "Bare 43-hex overflow returns None")
    r.check(QBitClient.extract_torrent_hash(valid_hex_40_lower[:39]) is None, "Bare 39-hex truncated returns None")
    r.check(QBitClient.extract_torrent_hash(valid_hex_40_lower[:35]) is None, "Bare 35-hex truncated returns None")
    r.check(QBitClient.extract_torrent_hash(valid_b32_32_upper[:31]) is None, "Bare 31-Base32 truncated returns None")

    # Invalid characters in 32-char string (e.g. contains 8, 9, 0, 1 which are invalid in RFC 4648 Base32)
    invalid_b32 = "89018901890189018901890189018901"
    r.check(QBitClient.extract_torrent_hash(invalid_b32) is None, "Bare 32-char with invalid Base32 chars returns None")
    r.check(QBitClient.extract_torrent_hash(f"magnet:?xt=urn:btih:{invalid_b32}&dn=Test") is None, "Magnet 32-char with invalid Base32 chars returns None")

    # URL path embedded 40-hex hash
    url_test = f"https://tracker.example.com/download/{valid_hex_40_upper}.torrent"
    r.check(QBitClient.extract_torrent_hash(url_test) == valid_hex_40_lower, "URL with embedded 40-hex hash normalized to lowercase")

    # Non-string and empty inputs
    r.check(QBitClient.extract_torrent_hash("") is None, "Empty string returns None")
    r.check(QBitClient.extract_torrent_hash(None) is None, "None input returns None")
    r.check(QBitClient.extract_torrent_hash(123456789) is None, "Integer input returns None")

    # -------------------------------------------------------------------------
    # 5. Full Selective Download Simulation with Bundles
    # -------------------------------------------------------------------------
    print("\n--- 5. Full Selective Download Simulation with Bundles ---")

    client = QBitClient(host="127.0.0.1", port=8080)
    client.get_torrent_files = lambda th: [
        {"name": "[Yen Press] DanMachi Vol 1, 2, and 3.epub", "id": 0},
        {"name": "Overlord v1, 2, 3, and 4.epub", "id": 1},
        {"name": "Ascendance of a Bookworm Books 1, 2, & 3.epub", "id": 2},
        {"name": "Slime Volume 1, 2, + 3.epub", "id": 3},
        {"name": "Series Vol 1, 2, 3, 4, + 5.epub", "id": 4},
        {"name": "The Lion, the Witch, and the Wardrobe - Vol 1.epub", "id": 5},
    ]
    client.set_file_priorities = lambda th, idxs, priority: True

    # Scenario 1: User owns {1.0, 2.0}
    # - DanMachi (1,2,3): has 3.0 -> KEPT
    # - Overlord (1,2,3,4): has 3.0, 4.0 -> KEPT
    # - Bookworm (1,2,3): has 3.0 -> KEPT
    # - Slime (1,2,3): has 3.0 -> KEPT
    # - Series (1,2,3,4,5): has 3,4,5 -> KEPT
    # - Wardrobe (1): only has 1.0 -> SKIPPED (owned)
    res_1_2 = client.apply_selective_volume_download("mock_hash", owned_volumes={1.0, 2.0})
    kept_1_2 = set(res_1_2.get("kept_indices", []))
    skipped_1_2 = set(res_1_2.get("skipped_indices", []))
    r.check(
        kept_1_2 == {0, 1, 2, 3, 4} and skipped_1_2 == {5},
        "Owned {1.0, 2.0}: Bundles needing Vol 3+ kept, single Vol 1 skipped",
        f"Expected kept={{0, 1, 2, 3, 4}}, skipped={{5}}. Got kept={kept_1_2}, skipped={skipped_1_2}"
    )

    # Scenario 2: User owns {1.0, 2.0, 3.0}
    # - DanMachi (1,2,3): all owned -> SKIPPED
    # - Overlord (1,2,3,4): has 4.0 -> KEPT
    # - Bookworm (1,2,3): all owned -> SKIPPED
    # - Slime (1,2,3): all owned -> SKIPPED
    # - Series (1,2,3,4,5): has 4,5 -> KEPT
    # - Wardrobe (1): all owned -> SKIPPED
    res_1_3 = client.apply_selective_volume_download("mock_hash", owned_volumes={1.0, 2.0, 3.0})
    kept_1_3 = set(res_1_3.get("kept_indices", []))
    skipped_1_3 = set(res_1_3.get("skipped_indices", []))
    r.check(
        kept_1_3 == {1, 4} and skipped_1_3 == {0, 2, 3, 5},
        "Owned {1.0, 2.0, 3.0}: Overlord and Series kept, DanMachi/Bookworm/Slime/Wardrobe skipped",
        f"Expected kept={{1, 4}}, skipped={{0, 2, 3, 5}}. Got kept={kept_1_3}, skipped={skipped_1_3}"
    )

    # Scenario 3: User owns all {1.0, 2.0, 3.0, 4.0, 5.0}
    # All files should be skipped (or 0 kept)
    res_all = client.apply_selective_volume_download("mock_hash", owned_volumes={1.0, 2.0, 3.0, 4.0, 5.0})
    kept_all = set(res_all.get("kept_indices", []))
    skipped_all = set(res_all.get("skipped_indices", []))
    r.check(
        kept_all == set() and skipped_all == {0, 1, 2, 3, 4, 5},
        "Owned {1.0, 2.0, 3.0, 4.0, 5.0}: All 6 files skipped",
        f"Expected kept=empty, skipped={{0, 1, 2, 3, 4, 5}}. Got kept={kept_all}, skipped={skipped_all}"
    )

    print("\n" + "=" * 78)
    print(f"  CHALLENGER IT3 SUMMARY: {r.passed} PASSED | {r.failed} FAILED | {r.passed + r.failed} TOTAL")
    print("=" * 78)

    if r.failed > 0:
        print("\nREPRODUCIBLE EMPIRICAL DEFECTS DETECTED:")
        for idx, f in enumerate(r.failures, 1):
            print(f"  {idx}. {f['test']}")
            print(f"     Details: {f['details']}")
        print("\nVerdict: REQUEST_CHANGES")
        return False
    else:
        print("\nVerdict: APPROVE")
        return True


if __name__ == "__main__":
    success = run_it3_stress_suite()
    sys.exit(0 if success else 1)
