"""
tests/test_adversarial_deep.py
Deep adversarial stress-testing harness for Reviewer 2.
"""

import sys
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.downloader.qbit import (
    QBitClient,
    parse_torrent_filename_volumes,
    ConnectionTestResult,
    SelectiveDownloadResult,
)
from app.security import (
    validate_downloader_destination,
    is_cloud_metadata_ip,
    is_safe_downloader_url,
)

failures = []

def assert_eq(actual, expected, desc):
    if actual != expected:
        msg = f"[FAIL] {desc} | Expected: {expected} | Actual: {actual}"
        failures.append(msg)
        print(msg)
    else:
        print(f"  [PASS] {desc}")

print("======================================================================")
print("  ADVERSARIAL DEEP STRESS HARNESS - REVIEWER 2")
print("======================================================================")

# ----------------------------------------------------------------------
# 1. Numbered Series & Release Tags Matrix
# ----------------------------------------------------------------------
print("\n--- 1. Numbered Series & Release Tags Matrix ---")

test_cases_series = [
    # Single tags without series_name
    ("[Yen Press] Mob Psycho 100 - Vol 03.epub", None, (False, {3.0})),
    ("[Group] 86 - Vol 01.epub", None, (False, {1.0})),
    ("[Digital] 1984 - Vol 01.epub", None, (False, {1.0})),
    ("[Group] Catch-22 - Vol 02.epub", None, (False, {2.0})),
    ("[Group] Studio 54 - Vol 03.epub", None, (False, {3.0})),
    ("[Group] Apollo 13 - Vol 04.epub", None, (False, {4.0})),
    ("[Group] Gate 7 - Vol 05.epub", None, (False, {5.0})),
    
    # With series_name parameter
    ("[Yen Press] Mob Psycho 100 - Vol 03.epub", "Mob Psycho 100", (False, {3.0})),
    ("[Group] 86 - Vol 01.epub", "86", (False, {1.0})),
    ("[Group] 86 - 01.epub", "86", (False, {1.0})),
    ("[Digital] 1984 - Vol 01.epub", "1984", (False, {1.0})),
    ("[Group] Catch-22 - Vol 02.epub", "Catch-22", (False, {2.0})),
    ("[Group] Studio 54 - Vol 03.epub", "Studio 54", (False, {3.0})),
    ("[Group] Apollo 13 - Vol 04.epub", "Apollo 13", (False, {4.0})),
    ("[Group] Gate 7 - Vol 05.epub", "Gate 7", (False, {5.0})),
    
    # Multiple stacked tags with brackets and parens
    ("[Group] [Digital] (1080p) 86 - Vol 02.epub", None, (False, {2.0})),
    ("[Group] [Digital] (1080p) 86 - Vol 02.epub", "86", (False, {2.0})),
    ("[Group] [v2] [Digital] Mob Psycho 100 - Vol 04.epub", None, (False, {4.0})),
    ("[Group] [v2] [Digital] Mob Psycho 100 - Vol 04.epub", "Mob Psycho 100", (False, {4.0})),
    ("(Digital) [Yen Press] Catch-22 - Vol 1-3.epub", None, (False, {1.0, 2.0, 3.0})),
    ("(Digital) [Yen Press] Catch-22 - Vol 1-3.epub", "Catch-22", (False, {1.0, 2.0, 3.0})),
    
    # Tags containing numbers: e.g. [1080p], [x264], [2024]
    ("[1080p] [2024] 86 - Vol 01.epub", None, (False, {1.0})),
    ("[1080p] [2024] 86 - Vol 01.epub", "86", (False, {1.0})),
    ("[720p] Mob Psycho 100 - Vol 02.epub", None, (False, {2.0})),
    ("[720p] Mob Psycho 100 - Vol 02.epub", "Mob Psycho 100", (False, {2.0})),
    
    # Omnibus ranges in numbered series
    ("86 - Vol 01-03.epub", None, (False, {1.0, 2.0, 3.0})),
    ("86 - Vol 01-03.epub", "86", (False, {1.0, 2.0, 3.0})),
    ("Mob Psycho 100 [Vol 1-3].epub", None, (False, {1.0, 2.0, 3.0})),
    ("Mob Psycho 100 [Vol 1-3].epub", "Mob Psycho 100", (False, {1.0, 2.0, 3.0})),
    ("[Group] Mob Psycho 100 - Vol 01-03.epub", None, (False, {1.0, 2.0, 3.0})),
    ("[Group] Mob Psycho 100 - Vol 01-03.epub", "Mob Psycho 100", (False, {1.0, 2.0, 3.0})),
    
    # Inverted ranges with explicit prefix
    ("Series Vol 03-01.epub", None, (False, {1.0, 2.0, 3.0})),
    ("[Group] Series Vol 04-02.epub", None, (False, {2.0, 3.0, 4.0})),
]

for fname, sname, expected in test_cases_series:
    res = parse_torrent_filename_volumes(fname, series_name=sname)
    assert_eq(res, expected, f"Parsing '{fname}' (series={sname})")

# ----------------------------------------------------------------------
# 2. Conjunction Lists & Selective Download Invariant
# ----------------------------------------------------------------------
print("\n--- 2. Conjunction Lists & Selective Download Invariant ---")

test_cases_conj = [
    ("Mushoku Tensei Vol 1 & 2.epub", (False, {1.0, 2.0})),
    ("Mushoku Tensei v1 + 2.epub", (False, {1.0, 2.0})),
    ("Mushoku Tensei Books 1, 2, 3.epub", (False, {1.0, 2.0, 3.0})),
    ("Mushoku Tensei Vol 1 and 2.epub", (False, {1.0, 2.0})),
    ("Overlord Vol 1, 2 & 3.epub", (False, {1.0, 2.0, 3.0})),
    ("Classroom of the Elite Vol 11.5 & 11.75.epub", (False, {11.5, 11.75})),
    ("Bookworm Part 1 Books 1 and 2.epub", (False, {1.0, 2.0})),
    ("[Group] Series Vol 1 & 2 [Digital].epub", (False, {1.0, 2.0})),
]

for fname, expected in test_cases_conj:
    res = parse_torrent_filename_volumes(fname)
    assert_eq(res, expected, f"Conjunction '{fname}'")

# Test Selective Download Invariant with Conjunctions
print("\n--- Testing Selective Download Invariant ---")
c = QBitClient(host="localhost", port=8080)
from test_downloader import MockUrllibOpener
mock_opener = MockUrllibOpener(c)
c.opener = mock_opener
c.authenticated = True

# Scenario A: User owns Volume 1.
# Torrent contains File 0 (Vol 1 & 2.epub) and File 1 (Vol 3.epub).
# File 0 MUST NOT be deselected because user needs Volume 2!
mock_opener.mock_files = [
    {"id": 0, "name": "Mushoku Tensei Vol 1 & 2.epub", "size": 10000000},
    {"id": 1, "name": "Mushoku Tensei Vol 3.epub", "size": 5000000},
]
res_sel_a = c.apply_selective_volume_download("hash_conj_a", owned_volumes={1.0})
assert_eq(0 in res_sel_a["kept_indices"], True, "File 0 (Vol 1 & 2) is KEPT when user owns only Vol 1")
assert_eq(0 in res_sel_a["skipped_indices"], False, "File 0 (Vol 1 & 2) is NOT skipped when user needs Vol 2")
assert_eq(1 in res_sel_a["kept_indices"], True, "File 1 (Vol 3) is KEPT when user needs Vol 3")

# Scenario B: User owns Volume 1 AND Volume 2.
# File 0 MUST be deselected because ALL contained volumes {1, 2} are owned.
res_sel_b = c.apply_selective_volume_download("hash_conj_b", owned_volumes={1.0, 2.0})
assert_eq(0 in res_sel_b["skipped_indices"], True, "File 0 (Vol 1 & 2) is SKIPPED when user owns Vols 1 and 2")
assert_eq(0 in res_sel_b["kept_indices"], False, "File 0 (Vol 1 & 2) is NOT kept when user owns all volumes")
assert_eq(1 in res_sel_b["kept_indices"], True, "File 1 (Vol 3) is KEPT when user needs Vol 3")

# Scenario C: User uses wanted_volumes=[2.0, 3.0].
res_sel_c = c.apply_selective_volume_download("hash_conj_c", wanted_volumes=[2.0, 3.0])
assert_eq(0 in res_sel_c["kept_indices"], True, "File 0 (Vol 1 & 2) is KEPT when wanted_volumes contains 2.0")

# ----------------------------------------------------------------------
# 3. SSRF Link-Local & Cloud Metadata Protection
# ----------------------------------------------------------------------
print("\n--- 3. SSRF Link-Local & Cloud Metadata Protection ---")

ssrf_blocked_vectors = [
    # IPv4 link-local & cloud metadata
    ("169.254.169.254", "http"),
    ("169.254.1.1", "http"),
    ("169.254.0.1:8080", "http"),
    ("http://169.254.169.254/latest/meta-data/", None),
    ("http://169.254.169.254:8080", None),
    
    # IPv6 link-local
    ("[fe80::1]", "http"),
    ("http://[fe80::1]:8080", None),
    ("http://[FE80::1]/", None),
    
    # IPv4-mapped IPv6 link-local
    ("[::ffff:169.254.169.254]", "http"),
    ("http://[::ffff:169.254.169.254]:8080", None),
    
    # Cloud metadata hostnames
    ("metadata.google.internal", "http"),
    ("METADATA.GOOGLE.INTERNAL", "http"),
    ("metadata.google.internal.", "http"),
    ("http://metadata.google.internal/computeMetadata/v1/", None),
    ("instance-data", "http"),
    ("http://instance-data:8080", None),
    ("metadata", "http"),
    ("http://metadata/", None),
    
    # Non-HTTP schemes
    ("file:///etc/passwd", None),
    ("ftp://localhost:21", None),
    ("gopher://127.0.0.1:70", None),
    ("javascript://alert(1)", None),
    ("dict://localhost:2628", None),
]

for target, scheme in ssrf_blocked_vectors:
    is_valid, msg = validate_downloader_destination(target, scheme=scheme, resolve_dns=False)
    assert_eq(is_valid, False, f"SSRF blocked: '{target}' (scheme={scheme}) -> {msg}")

# Legitimate local configurations MUST be permitted
legit_allowed_destinations = [
    ("http://localhost:8080", None),
    ("http://127.0.0.1:8080", None),
    ("http://192.168.1.50:8080", None),
    ("http://10.0.0.5:8080", None),
    ("https://localhost:8443", None),
    ("127.0.0.1:8080", "http"),
    ("192.168.0.2:9091", "http"),
    ("localhost", "http"),
    ("[::1]", "http"),
    ("http://[::1]:8080", None),
]

for target, scheme in legit_allowed_destinations:
    is_valid, msg = validate_downloader_destination(target, scheme=scheme, resolve_dns=False)
    assert_eq(is_valid, True, f"Legitimate allowed: '{target}' (scheme={scheme})")

# ----------------------------------------------------------------------
# 4. Bare Hash & Magnet Handling
# ----------------------------------------------------------------------
print("\n--- 4. Bare Hash & Magnet Handling ---")

# 40-char hex
hex40_lower = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
hex40_upper = hex40_lower.upper()
hex40_mixed = "Da39A3Ee5E6b4B0d3255BfEf95601890Afd80709"

assert_eq(QBitClient.extract_torrent_hash(hex40_lower), hex40_lower, "Bare 40-hex lowercase")
assert_eq(QBitClient.extract_torrent_hash(hex40_upper), hex40_lower, "Bare 40-hex uppercase")
assert_eq(QBitClient.extract_torrent_hash(hex40_mixed), hex40_lower, "Bare 40-hex mixed case")

# 32-char Base32
# Base32 'JJZEYZLZJJZEYZLZJJZEYZLZJJZEYZLZ' -> 20 bytes -> hex
b32_test = "JJZEYZLZJJZEYZLZJJZEYZLZJJZEYZLZ"
b32_hex = QBitClient.extract_torrent_hash(b32_test)
assert_eq(b32_hex is not None and len(b32_hex) == 40, True, "Bare 32-Base32 uppercase converts to 40-hex")
assert_eq(QBitClient.extract_torrent_hash(b32_test.lower()), b32_hex, "Bare 32-Base32 lowercase converts to same 40-hex")

# Magnet links
mag_hex = f"magnet:?xt=urn:btih:{hex40_upper}&dn=Test"
assert_eq(QBitClient.extract_torrent_hash(mag_hex), hex40_lower, "Magnet with 40-hex uppercase")

mag_b32 = f"magnet:?xt=urn:btih:{b32_test}&dn=TestB32"
assert_eq(QBitClient.extract_torrent_hash(mag_b32), b32_hex, "Magnet with 32-Base32 converts to 40-hex")

# Invalid lengths & garbage
assert_eq(QBitClient.extract_torrent_hash("da39a3ee5e6b4b0d3255bfef95601890afd8070"), None, "39 chars (invalid length) -> None")
assert_eq(QBitClient.extract_torrent_hash("da39a3ee5e6b4b0d3255bfef95601890afd807099"), None, "41 chars (invalid length) -> None")
assert_eq(QBitClient.extract_torrent_hash("JJZEYZLZJJZEYZLZJJZEYZLZJJZEYZL"), None, "31 chars Base32 -> None")
assert_eq(QBitClient.extract_torrent_hash("JJZEYZLZJJZEYZLZJJZEYZLZJJZEYZLZX"), None, "33 chars Base32 -> None")
assert_eq(QBitClient.extract_torrent_hash("not a hash at all!"), None, "Garbage text -> None")
assert_eq(QBitClient.extract_torrent_hash(""), None, "Empty string -> None")
assert_eq(QBitClient.extract_torrent_hash(None), None, "None input -> None")

# ----------------------------------------------------------------------
# 5. Summary
# ----------------------------------------------------------------------
print("\n======================================================================")
print(f"ADVERSARIAL DEEP STRESS HARNESS SUMMARY: {len(failures)} FAILURES")
print("======================================================================")
if failures:
    print(f"VERDICT: REQUEST_CHANGES ({len(failures)} failed checks)")
    sys.exit(1)
else:
    print("VERDICT: APPROVE (100% SUCCESS ACROSS ALL ADVERSARIAL MATRIX PROBES)")
    sys.exit(0)
