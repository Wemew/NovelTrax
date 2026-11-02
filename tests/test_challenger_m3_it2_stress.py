"""
Empirical Challenger Stress Harness — Milestone 3, Iteration 2
Adversarial stress-testing of downloader implementation:
1. Nested and sequential tag extraction with/without series titles
2. Bare numbers and title collisions with multiple delimiters
3. Selective download simulation for conjunctions and multi-volume lists
4. SSRF bypass probing on validate_downloader_destination
5. Info-hash boundary extraction and normalization
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))

from app.downloader.qbit import (
    parse_torrent_filename_volumes,
    QBitClient,
    ConnectionTestResult,
    SelectiveDownloadResult
)
from app.security import (
    validate_downloader_destination,
    is_cloud_metadata_ip,
    is_safe_downloader_url
)


class ChallengerTestRecorder:
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


def run_challenger_suite():
    r = ChallengerTestRecorder()

    print("\n" + "=" * 75)
    print("  EMPIRICAL CHALLENGER STRESS HARNESS — MILESTONE 3 ITERATION 2")
    print("=" * 75)

    # -------------------------------------------------------------------------
    # 1. Nested and Sequential Tags Probing
    # -------------------------------------------------------------------------
    print("\n--- 1. Nested and Sequential Tags Probing ---")

    fname_86 = "[Tag1][Tag2](Format) 86 - Eighty Six - 03 [1080p].epub"
    _, vols_86_s = parse_torrent_filename_volumes(fname_86, series_name="86")
    r.check(vols_86_s == {3.0}, "Nested tags with series_name='86' isolates Vol 3.0", f"Got {vols_86_s}")

    _, vols_86_full = parse_torrent_filename_volumes(fname_86, series_name="86 - Eighty Six")
    r.check(vols_86_full == {3.0}, "Nested tags with series_name='86 - Eighty Six' isolates Vol 3.0", f"Got {vols_86_full}")

    fname_86_vol = "[Tag1][Tag2](Format) 86 - Eighty Six - Vol 03 [1080p].epub"
    _, vols_86_vol = parse_torrent_filename_volumes(fname_86_vol, series_name=None)
    r.check(vols_86_vol == {3.0}, "Nested tags with explicit 'Vol' prefix isolates Vol 3.0 without series_name", f"Got {vols_86_vol}")

    fname_multi_tags = "[GroupA][SubGroupB](720p)(DualAudio)[Digital] Overlord - Vol 16.epub"
    _, vols_multi = parse_torrent_filename_volumes(fname_multi_tags)
    r.check(vols_multi == {16.0}, "Multiple leading bracket and parenthesis tags stripped iteratively", f"Got {vols_multi}")

    # -------------------------------------------------------------------------
    # 2. Bare Numbers and Title Collisions with Multiple Delimiters
    # -------------------------------------------------------------------------
    print("\n--- 2. Bare Numbers and Title Collisions with Multiple Delimiters ---")

    fname_1984_vol = "[Group] 1984 - Vol 01.mobi"
    _, v_1984_none = parse_torrent_filename_volumes(fname_1984_vol, series_name=None)
    r.check(v_1984_none == {1.0}, "Title '1984' with Vol 01 extracts {1.0} (series_name=None)", f"Got {v_1984_none}")

    _, v_1984_s = parse_torrent_filename_volumes(fname_1984_vol, series_name="1984")
    r.check(v_1984_s == {1.0}, "Title '1984' with Vol 01 extracts {1.0} (series_name='1984')", f"Got {v_1984_s}")

    fname_1984_bare = "[Group] 1984 - 01.mobi"
    _, v_1984_b = parse_torrent_filename_volumes(fname_1984_bare, series_name=None)
    r.check(v_1984_b == {1.0}, "Title '1984 - 01' filters release year 1984 and extracts {1.0}", f"Got {v_1984_b}")

    fname_catch22_omni = "[Group] Catch-22 (Novel) Vol 1-3.epub"
    _, v_c22_omni = parse_torrent_filename_volumes(fname_catch22_omni, series_name=None)
    r.check(v_c22_omni == {1.0, 2.0, 3.0}, "Title 'Catch-22 (Novel) Vol 1-3' extracts {1.0, 2.0, 3.0} (series_name=None)", f"Got {v_c22_omni}")

    _, v_c22_omni_s = parse_torrent_filename_volumes(fname_catch22_omni, series_name="Catch-22")
    r.check(v_c22_omni_s == {1.0, 2.0, 3.0}, "Title 'Catch-22 (Novel) Vol 1-3' extracts {1.0, 2.0, 3.0} (series_name='Catch-22')", f"Got {v_c22_omni_s}")

    fname_c22_bare = "[Group] Catch-22 - 03.epub"
    _, v_c22_b = parse_torrent_filename_volumes(fname_c22_bare, series_name=None)
    r.check(v_c22_b == {3.0}, "Title 'Catch-22 - 03' ignores hyphenated 22 and extracts bare {3.0}", f"Got {v_c22_b}")

    # -------------------------------------------------------------------------
    # 3. Multi-Volume Conjunction & List Formats
    # -------------------------------------------------------------------------
    print("\n--- 3. Multi-Volume Conjunction & List Formats ---")

    _, v_conj1 = parse_torrent_filename_volumes("Mushoku Tensei Vol 1 & 2.epub")
    r.check(v_conj1 == {1.0, 2.0}, "Conjunction '&' (Vol 1 & 2) extracts {1.0, 2.0}", f"Got {v_conj1}")

    _, v_conj2 = parse_torrent_filename_volumes("Overlord v1, 2, 3.epub")
    r.check(v_conj2 == {1.0, 2.0, 3.0}, "Comma list without Oxford comma (v1, 2, 3) extracts {1.0, 2.0, 3.0}", f"Got {v_conj2}")

    _, v_conj3 = parse_torrent_filename_volumes("Ascendance of a Bookworm Books 1 + 2.epub")
    r.check(v_conj3 == {1.0, 2.0}, "Plus '+' (Books 1 + 2) extracts {1.0, 2.0}", f"Got {v_conj3}")

    _, v_conj4 = parse_torrent_filename_volumes("Slime Volume 1 and 2.epub")
    r.check(v_conj4 == {1.0, 2.0}, "Word 'and' (Volume 1 and 2) extracts {1.0, 2.0}", f"Got {v_conj4}")

    # EMPIRICAL CHALLENGE: Oxford comma list 'Vol 1, 2, and 3'
    fname_oxford = "DanMachi Vol 1, 2, and 3.epub"
    _, v_oxford = parse_torrent_filename_volumes(fname_oxford)
    r.check(
        v_oxford == {1.0, 2.0, 3.0},
        "Oxford comma list 'Vol 1, 2, and 3' extracts all 3 volumes {1.0, 2.0, 3.0}",
        f"Filename '{fname_oxford}' expected {{1.0, 2.0, 3.0}}, but got {v_oxford} (dropped Vol 3.0!)"
    )

    # -------------------------------------------------------------------------
    # 4. Selective Download Simulation & Invalidation Verification
    # -------------------------------------------------------------------------
    print("\n--- 4. Selective Download Simulation & Invalidation Verification ---")

    client = QBitClient(host="127.0.0.1", port=8080)
    client.get_torrent_files = lambda th: [
        {"name": "Mushoku Tensei Vol 1 & 2.epub", "id": 0},
        {"name": "Overlord v1, 2, 3.epub", "id": 1},
        {"name": "Ascendance of a Bookworm Books 1 + 2.epub", "id": 2},
        {"name": "DanMachi Vol 1, 2, and 3.epub", "id": 3},
    ]
    client.set_file_priorities = lambda th, idxs, priority: True

    # Scenario A: User owns {1.0}
    res_partial = client.apply_selective_volume_download("mock_hash", owned_volumes={1.0})
    r.check(
        res_partial.skipped_files == 0 and len(res_partial.get("kept_indices", [])) == 4,
        "Partial owned {1.0}: all multi-volume files KEPT (0 skipped)",
        f"Skipped {res_partial.skipped_files} files: {res_partial.get('skipped_indices')}"
    )

    # Scenario B: User owns {1.0, 2.0} -> requires Vol 3.0 in Overlord and DanMachi
    res_two = client.apply_selective_volume_download("mock_hash", owned_volumes={1.0, 2.0})
    danmachi_kept = 3 in res_two.get("kept_indices", [])
    r.check(
        danmachi_kept,
        "Oxford comma file 'DanMachi Vol 1, 2, and 3' KEPT when user owns {1.0, 2.0} but needs Vol 3.0",
        f"DanMachi (id 3) was DESELECTED/SKIPPED in skipped_indices: {res_two.get('skipped_indices')} due to dropping Vol 3.0"
    )

    # -------------------------------------------------------------------------
    # 5. SSRF Destination Validation
    # -------------------------------------------------------------------------
    print("\n--- 5. SSRF Destination Validation ---")

    ssrf_cloud_cases = [
        ("http://169.254.169.254/", "IPv4 link-local cloud metadata"),
        ("http://169.254.169.254:8080", "IPv4 link-local with port"),
        ("169.254.169.254", "Bare IPv4 link-local"),
        ("http://[fe80::1]:8080", "IPv6 link-local"),
        ("http://[::ffff:169.254.169.254]:8080", "IPv4-mapped IPv6 link-local"),
        ("metadata.google.internal", "GCP metadata hostname"),
        ("METADATA.GOOGLE.INTERNAL", "GCP metadata hostname uppercase"),
        ("metadata", "AWS/OpenStack metadata hostname"),
        ("instance-data", "EC2 instance-data hostname"),
        ("file:///etc/passwd", "file:// scheme rejected"),
        ("gopher://127.0.0.1:70/", "gopher:// scheme rejected"),
    ]
    for target, desc in ssrf_cloud_cases:
        valid, msg = validate_downloader_destination(target, resolve_dns=False)
        r.check(not valid, f"SSRF Block: {desc} ({target})", f"Unexpectedly allowed {target}: {msg}")

    # Permitted destinations
    safe_destinations = [
        ("http://127.0.0.1:8080", "Loopback IPv4"),
        ("http://localhost:8080", "Loopback localhost"),
        ("http://192.168.1.100:8080", "RFC 1918 Class C private LAN"),
        ("http://10.0.0.5:8080", "RFC 1918 Class A private LAN"),
        ("http://172.16.0.2:8080", "RFC 1918 Class B private LAN"),
    ]
    for target, desc in safe_destinations:
        valid, msg = validate_downloader_destination(target, resolve_dns=False)
        r.check(valid, f"SSRF Allow: {desc} ({target})", f"Unexpectedly blocked {target}: {msg}")

    # -------------------------------------------------------------------------
    # 6. Info-Hash Boundary Extraction and Normalization
    # -------------------------------------------------------------------------
    print("\n--- 6. Info-Hash Boundary Extraction and Normalization ---")

    h_40_lower = "4a5b6c7d8e9f0123456789abcdef0123456789ab"
    h_40_upper = "4A5B6C7D8E9F0123456789ABCDEF0123456789AB"
    h_32_b32 = "JJZEYZLZJJZEYZLZJJZEYZLZJJZEYZLZ"
    expected_hex = "4a724c65794a724c65794a724c65794a724c6579"

    r.check(QBitClient.extract_torrent_hash(h_40_lower) == h_40_lower, "Extract bare 40-hex lowercase hash")
    r.check(QBitClient.extract_torrent_hash(h_40_upper) == h_40_lower, "Extract bare 40-hex uppercase normalized to lowercase")
    r.check(QBitClient.extract_torrent_hash(h_32_b32) == expected_hex, "Extract bare 32-Base32 hash decoded to 40-hex")

    m_mixed_urn = f"magnet:?xt=Urn:Btih:{h_32_b32}&dn=Novel"
    r.check(QBitClient.extract_torrent_hash(m_mixed_urn) == expected_hex, "Extract magnet link with mixed-case 'Urn:Btih:' and Base32")

    # EMPIRICAL CHALLENGE: Malformed/truncated hashes in magnet links
    # Truncated 39-hex in magnet link
    m_truncated = "magnet:?xt=urn:btih:4a5b6c7d8e9f0123456789abcdef0123456789a&dn=Novel"
    res_trunc = QBitClient.extract_torrent_hash(m_truncated)
    r.check(
        res_trunc is None,
        "Truncated 39-hex hash in magnet link returns None (expected None, got normalized 40-hex)",
        f"extract_torrent_hash('{m_truncated[:50]}...') returned '{res_trunc}' (length {len(str(res_trunc))}) instead of None"
    )

    # Truncated 35-hex in magnet link
    m_35 = "magnet:?xt=urn:btih:01234567890123456789012345678901234&dn=Novel"
    res_35 = QBitClient.extract_torrent_hash(m_35)
    r.check(
        res_35 is None,
        "Truncated 35-hex hash in magnet link returns None",
        f"extract_torrent_hash returned '{res_35}' (length {len(str(res_35))}) instead of None"
    )

    # Bare invalid hashes return None
    r.check(QBitClient.extract_torrent_hash("4a5b6c7d8e9f0123456789abcdef0123456789a") is None, "Bare 39-hex returns None")
    r.check(QBitClient.extract_torrent_hash("JJZEYZLZJJZEYZLZJJZEYZLZJJZEYZL") is None, "Bare 31-Base32 returns None")
    r.check(QBitClient.extract_torrent_hash("88888888888888888888888888888888") is None, "Bare non-Base32 returns None")
    r.check(QBitClient.extract_torrent_hash("") is None, "Empty string returns None")

    print("\n" + "=" * 75)
    print(f"  CHALLENGER SUMMARY: {r.passed} PASSED | {r.failed} FAILED | {r.passed + r.failed} TOTAL")
    print("=" * 75)

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
    success = run_challenger_suite()
    sys.exit(0 if success else 1)
