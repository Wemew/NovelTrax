"""
tests/test_challenger_m3_stress.py

Milestone 3 Empirical Challenger Stress Test Suite
Adversarial verification of Downloader & Queue Management:
1. Volume parsing fuzzing (boundaries, unicode, inverted ranges, prequels, delimiters, multi-directory)
2. Numbered series prefix collisions & group tag stripping order of operations
3. The Omnibus Subset Rule stress testing (partial ownership, full ownership, unparsed files, single archives)
4. Hash extraction & Base32 decoding fuzzing
5. QBitClient polymorphism, timeout resilience & mock network fault injection
6. Downloader UI template & static contract verification
"""

import os
import sys
import json
import urllib.parse
from typing import Dict, Any, List, Set

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.downloader.qbit import (
    QBitClient,
    ConnectionTestResult,
    SelectiveDownloadResult,
    parse_torrent_filename_volumes,
    NOVEL_EXTENSIONS,
    ANCILLARY_EXTENSIONS
)

def check(cond: bool, msg: str = ""):
    if not cond:
        raise AssertionError(f"Check failed: {msg}")

class ChallengerRecorder:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def record_pass(self, name: str):
        self.passed += 1
        print(f"  [PASS] {name}")

    def record_fail(self, name: str, expected: Any, actual: Any, details: str = ""):
        self.failed += 1
        fail_msg = f"{name} | Expected: {expected} | Actual: {actual}"
        if details:
            fail_msg += f" | {details}"
        self.failures.append(fail_msg)
        print(f"  [FAIL] {fail_msg}")

    def summary(self) -> int:
        print("\n" + "=" * 70)
        print("EMPIRICAL CHALLENGER M3 STRESS HARNESS SUMMARY")
        print(f"Total Passed: {self.passed} | Total Failed: {self.failed} | Total Executed: {self.passed + self.failed}")
        if self.failures:
            print("\nFAILURES REPRODUCED:")
            for idx, f in enumerate(self.failures, 1):
                print(f"  {idx}. {f}")
        print("=" * 70)
        return self.failed

recorder = ChallengerRecorder()

# ==============================================================================
# SECTION 1: Volume Parsing Adversarial Matrix & Edge-Case Stress Testing
# ==============================================================================
print("\n--- SECTION 1: Volume Parsing Adversarial Matrix ---")

# 1.1 Inverted Omnibus Range (e.g. Vol 3-1 should normalize to {1, 2, 3})
is_anc, vols = parse_torrent_filename_volumes("Series Vol 03-01.epub")
if vols == {1.0, 2.0, 3.0}:
    recorder.record_pass("Inverted omnibus range 'Vol 03-01' normalizes to {1, 2, 3}")
else:
    recorder.record_fail("Inverted omnibus range", {1.0, 2.0, 3.0}, vols)

# 1.2 En Dash and Em Dash Ranges
is_anc, vols = parse_torrent_filename_volumes("Overlord Vol 01–03.epub")
if vols == {1.0, 2.0, 3.0}:
    recorder.record_pass("En dash omnibus range 'Vol 01–03' parses {1, 2, 3}")
else:
    recorder.record_fail("En dash omnibus range", {1.0, 2.0, 3.0}, vols)

is_anc, vols = parse_torrent_filename_volumes("Overlord Vol 04—06.epub")
if vols == {4.0, 5.0, 6.0}:
    recorder.record_pass("Em dash omnibus range 'Vol 04—06' parses {4, 5, 6}")
else:
    recorder.record_fail("Em dash omnibus range", {4.0, 5.0, 6.0}, vols)

# 1.3 Tilde and Word 'to' Ranges
is_anc, vols = parse_torrent_filename_volumes("Mushoku Tensei Vol 7~9.epub")
if vols == {7.0, 8.0, 9.0}:
    recorder.record_pass("Tilde omnibus range 'Vol 7~9' parses {7, 8, 9}")
else:
    recorder.record_fail("Tilde omnibus range", {7.0, 8.0, 9.0}, vols)

is_anc, vols = parse_torrent_filename_volumes("Mushoku Tensei Volume 10 to 12.epub")
if vols == {10.0, 11.0, 12.0}:
    recorder.record_pass("Keyword 'to' omnibus range 'Volume 10 to 12' parses {10, 11, 12}")
else:
    recorder.record_fail("Keyword 'to' omnibus range", {10.0, 11.0, 12.0}, vols)

# 1.4 High Volume Numbers
is_anc, vols = parse_torrent_filename_volumes("Guin Saga Vol 140.epub")
if vols == {140.0}:
    recorder.record_pass("High volume number 'Vol 140' parsed as {140.0}")
else:
    recorder.record_fail("High volume number", {140.0}, vols)

# 1.5 File without numbers
is_anc, vols = parse_torrent_filename_volumes("Overlord_Afterword_Special.epub")
if vols == set() and not is_anc:
    recorder.record_pass("Novel file without numbers returns vols=set() and is_ancillary=False")
else:
    recorder.record_fail("File without numbers", (False, set()), (is_anc, vols))

# 1.6 Volume 0 / Prequels
is_anc, vols = parse_torrent_filename_volumes("ReZero Vol 0.epub")
if vols == {0.0}:
    recorder.record_pass("Prequel 'Vol 0' parsed as {0.0}")
else:
    recorder.record_fail("Prequel 'Vol 0'", {0.0}, vols)

# 1.7 Decimal / Fractional Volumes
is_anc, vols = parse_torrent_filename_volumes("Classroom of the Elite Vol 4.5.epub")
if vols == {4.5}:
    recorder.record_pass("Decimal volume 'Vol 4.5' parsed as {4.5}")
else:
    recorder.record_fail("Decimal volume", {4.5}, vols)

is_anc, vols = parse_torrent_filename_volumes("Classroom of the Elite Vol 11.75.epub")
if vols == {11.75}:
    recorder.record_pass("Decimal volume 'Vol 11.75' parsed as {11.75}")
else:
    recorder.record_fail("Decimal volume 11.75", {11.75}, vols)

# 1.8 Numbered Series without bracketed prefix
is_anc, vols = parse_torrent_filename_volumes("86 - Eighty Six - Vol 04.epub", series_name="86 - Eighty Six")
if vols == {4.0}:
    recorder.record_pass("Numbered series without brackets '86 - Eighty Six - Vol 04' isolates Vol 4")
else:
    recorder.record_fail("Numbered series 86", {4.0}, vols)

is_anc, vols = parse_torrent_filename_volumes("Mob Psycho 100 - Vol 02.epub", series_name="Mob Psycho 100")
if vols == {2.0}:
    recorder.record_pass("Numbered series without brackets 'Mob Psycho 100 - Vol 02' isolates Vol 2")
else:
    recorder.record_fail("Numbered series Mob Psycho 100", {2.0}, vols)

# 1.9 Ancillary / Clutter Extensions
clutter_samples = [
    "cover.jpg", "cover.png", "sample.webp", "series.nfo", "readme.txt",
    "info.url", "checksum.sfv", "notes.md", "metadata.xml", "settings.ini"
]
all_clutter_passed = True
for f in clutter_samples:
    anc, v = parse_torrent_filename_volumes(f)
    if not anc or v != set():
        all_clutter_passed = False
        recorder.record_fail(f"Clutter file '{f}'", (True, set()), (anc, v))
if all_clutter_passed:
    recorder.record_pass("All 10 clutter / ancillary extensions correctly identified as is_ancillary=True")

# 1.10 Non-novel Extensions (Media / Binaries)
media_samples = ["episode01.mkv", "soundtrack.mp3", "installer.exe", "backup.iso"]
all_media_passed = True
for f in media_samples:
    anc, v = parse_torrent_filename_volumes(f)
    if not anc or v != set():
        all_media_passed = False
        recorder.record_fail(f"Non-novel media file '{f}'", (True, set()), (anc, v))
if all_media_passed:
    recorder.record_pass("Non-novel media / binary files correctly flagged as ancillary (to not download)")

# 1.11 Nested Folder Structures
is_anc, vols = parse_torrent_filename_volumes("Overlord/Volume 03/novel.epub")
if vols == {3.0}:
    recorder.record_pass("Nested directory 'Overlord/Volume 03/novel.epub' resolves {3.0}")
else:
    recorder.record_fail("Nested directory Volume 03", {3.0}, vols)

is_anc, vols = parse_torrent_filename_volumes("Sword Art Online/Vol 1-3/bundle.epub")
if vols == {1.0, 2.0, 3.0}:
    recorder.record_pass("Nested omnibus directory 'Sword Art Online/Vol 1-3/bundle.epub' resolves {1, 2, 3}")
else:
    recorder.record_fail("Nested omnibus directory", {1.0, 2.0, 3.0}, vols)


# ==============================================================================
# SECTION 2: ADVERSARIAL CHALLENGES & VULNERABILITY INVESTIGATION
# ==============================================================================
print("\n--- SECTION 2: Adversarial Challenges & Edge-Case Probing ---")

# CHALLENGE A: Numbered Series Collision Under Bracketed Group Tags
# When a release contains a group tag like "[Yen Press] 86 - Eighty Six - 03.epub",
# does parse_torrent_filename_volumes correctly extract {3.0}?
anc_86, vols_86 = parse_torrent_filename_volumes("[Yen Press] 86 - Eighty Six - 03.epub", series_name="86 - Eighty Six")
if vols_86 == {3.0}:
    recorder.record_pass("Release with leading bracket tag '[Yen Press] 86 - Eighty Six - 03' correctly parses {3.0}")
else:
    recorder.record_fail(
        "Numbered series collision with leading bracket tag",
        expected={3.0},
        actual=vols_86,
        details="Order of operations defect in qbit.py:113: series_name stripping is anchored at ^ before [Group] tag is removed, extracting {86.0} instead of {3.0}"
    )

# CHALLENGE B: Mob Psycho 100 Phantom Omnibus Under Bracketed Group Tags
anc_mob, vols_mob = parse_torrent_filename_volumes("[Group] Mob Psycho 100 - 05.epub", series_name="Mob Psycho 100")
if vols_mob == {5.0}:
    recorder.record_pass("Release '[Group] Mob Psycho 100 - 05.epub' correctly parses {5.0}")
else:
    recorder.record_fail(
        "Phantom omnibus range in Mob Psycho 100 with leading bracket tag",
        expected={5.0},
        actual=f"Set with {len(vols_mob)} volumes (Vols {min(vols_mob) if vols_mob else 0}-{max(vols_mob) if vols_mob else 0})",
        details="Defect: '[Group]' prevented series_name stripping, leaving '100 - 05', which was inverted to omnibus range 5-100"
    )

# CHALLENGE C: Fallback behavior for oversized omnibus ranges (>100 volumes)
# If a torrent contains "Guin Saga Vol 1-120.epub", should it be parsed as {1.0} or kept safely?
anc_120, vols_120 = parse_torrent_filename_volumes("Guin Saga Vol 1-120.epub")
if vols_120 != {1.0}:
    recorder.record_pass("Oversized omnibus range does not dangerously fall back to volume 1")
else:
    recorder.record_fail(
        "Oversized omnibus range fallback to volume 1",
        expected="Either omnibus volumes or unparsed empty set",
        actual=vols_120,
        details="Defect: When (e_val - s_val) > 100 in qbit.py:140, range parser passes through to single volume regex matching 'Vol 1', deselecting the entire 120-volume bundle if user owns Vol 1"
    )


# ==============================================================================
# SECTION 3: The Omnibus Subset Rule & Selective Priority Allocation
# ==============================================================================
print("\n--- SECTION 3: The Omnibus Subset Rule & Selective Priority Allocation ---")

class MockOpenerHelper:
    def __init__(self, files_payload: List[Dict[str, Any]]):
        self.files_payload = files_payload
        self.prio_calls = []

    def open(self, req, timeout=10.0):
        url = req.full_url
        if "/api/v2/torrents/files" in url:
            class MockResp:
                status = 200
                def read(self):
                    return json.dumps(files_payload_copy).encode("utf-8")
                def __enter__(self): return self
                def __exit__(self, *args): pass
            files_payload_copy = self.files_payload
            return MockResp()
        elif "/api/v2/torrents/filePrio" in url:
            data = req.data.decode("utf-8")
            parsed = urllib.parse.parse_qs(data)
            self.prio_calls.append(parsed)
            class MockRespOk:
                status = 200
                def read(self): return b""
                def __enter__(self): return self
                def __exit__(self, *args): pass
            return MockRespOk()
        elif "/api/v2/auth/login" in url:
            class MockLogin:
                status = 200
                def read(self): return b"Ok."
                def __enter__(self): return self
                def __exit__(self, *args): pass
            return MockLogin()
        raise NotImplementedError(f"Unhandled mock URL: {url}")

mock_files = [
    {"id": 0, "name": "Overlord - Vol 01-03.epub", "size": 10485760},
    {"id": 1, "name": "Overlord - Vol 04.epub", "size": 3145728},
    {"id": 2, "name": "Overlord - Vol 05.epub", "size": 3145728},
    {"id": 3, "name": "Overlord - Vol 06.epub", "size": 3145728},
    {"id": 4, "name": "Extra_Side_Stories.epub", "size": 1048576},
    {"id": 5, "name": "cover.jpg", "size": 524288},
    {"id": 6, "name": "release.nfo", "size": 2048}
]

# 3.1 Partial Omnibus Ownership: User owns {1.0, 2.0} (Missing 3, 4, 5, 6)
# Vol 1-3 MUST BE KEPT because Vol 3 is missing!
helper = MockOpenerHelper(mock_files)
client = QBitClient(host="localhost", port=8080)
client.opener = helper
client.authenticated = True

res = client.apply_selective_volume_download(
    torrent_hash="a" * 40,
    owned_volumes=[1.0, 2.0],
    series_name="Overlord"
)

if 0 not in res["skipped_indices"]:
    recorder.record_pass("Omnibus Vol 01-03 KEPT when user owns {1, 2} but lacks Vol 3")
else:
    recorder.record_fail("Omnibus Vol 01-03 retained", "Kept (not in skipped)", res["skipped_indices"])

if 4 not in res["skipped_indices"]:
    recorder.record_pass("Unparsed novel file Extra_Side_Stories.epub KEPT by safety fallback")
else:
    recorder.record_fail("Unparsed novel file safety fallback", "Kept", res["skipped_indices"])

if 5 in res["skipped_indices"] and 6 in res["skipped_indices"]:
    recorder.record_pass("Ancillary files cover.jpg & release.nfo SKIPPED (priority=0)")
else:
    recorder.record_fail("Ancillary files skipped", "5 and 6 skipped", res["skipped_indices"])

# 3.2 Full Omnibus Ownership: User owns {1.0, 2.0, 3.0, 4.0} (Missing 5, 6)
# Vol 1-3 MUST BE SKIPPED because all contained volumes {1, 2, 3} are owned!
helper2 = MockOpenerHelper(mock_files)
client.opener = helper2
res2 = client.apply_selective_volume_download(
    torrent_hash="a" * 40,
    owned_volumes=[1.0, 2.0, 3.0, 4.0],
    series_name="Overlord"
)

if 0 in res2["skipped_indices"]:
    recorder.record_pass("Omnibus Vol 01-03 SKIPPED when all contained volumes {1, 2, 3} are owned")
else:
    recorder.record_fail("Omnibus Vol 01-03 skipped when owned", "Skipped", res2["skipped_indices"])

if 1 in res2["skipped_indices"]:
    recorder.record_pass("Single Vol 04 SKIPPED when owned")
else:
    recorder.record_fail("Single Vol 04 skipped", "Skipped", res2["skipped_indices"])

if 2 not in res2["skipped_indices"] and 3 not in res2["skipped_indices"]:
    recorder.record_pass("Missing Vol 05 & Vol 06 KEPT for active download")
else:
    recorder.record_fail("Missing Vol 05 & 06 kept", "Kept", res2["skipped_indices"])

# 3.3 Single Archive Torrent Protection
single_archive_files = [{"id": 0, "name": "Overlord_Complete_Series.zip", "size": 100000000}]
helper3 = MockOpenerHelper(single_archive_files)
client.opener = helper3
res3 = client.apply_selective_volume_download(
    torrent_hash="b" * 40,
    owned_volumes=[1.0, 2.0]
)
if res3["status"] == "single_archive" and res3["skipped_files"] == 0:
    recorder.record_pass("Monolithic archive correctly identified as 'single_archive' with 0 skipped")
else:
    recorder.record_fail("Single archive handling", "single_archive status", res3)

# 3.4 Empty Files List (Pending DHT Metadata)
empty_files = []
helper4 = MockOpenerHelper(empty_files)
client.opener = helper4
res4 = client.apply_selective_volume_download(
    torrent_hash="c" * 40,
    owned_volumes=[1.0, 2.0]
)
if res4["status"] == "pending_metadata" and res4["skipped_files"] == 0:
    recorder.record_pass("Empty files list handled gracefully as 'pending_metadata'")
else:
    recorder.record_fail("Pending metadata handling", "pending_metadata status", res4)


# ==============================================================================
# SECTION 4: Hash Extraction & Base32 Decoding Fuzzing
# ==============================================================================
print("\n--- SECTION 4: Hash Extraction & Base32 Decoding Fuzzing ---")

# 4.1 Standard 40-character Hex Hash
hex_hash = "3b5a451121852033c467a9099db1fb2c62c2f42a"
magnet_hex = f"magnet:?xt=urn:btih:{hex_hash}&dn=Overlord"
extracted_hex = QBitClient.extract_torrent_hash(magnet_hex)
if extracted_hex == hex_hash:
    recorder.record_pass("Extract 40-character hex hash from magnet link")
else:
    recorder.record_fail("Hex hash extraction", hex_hash, extracted_hex)

# 4.2 Uppercase 40-character Hex Hash
magnet_hex_upper = f"magnet:?xt=urn:btih:{hex_hash.upper()}&dn=Overlord"
extracted_upper = QBitClient.extract_torrent_hash(magnet_hex_upper)
if extracted_upper == hex_hash:
    recorder.record_pass("Extract uppercase 40-character hex hash normalized to lowercase")
else:
    recorder.record_fail("Uppercase hex hash extraction", hex_hash, extracted_upper)

# 4.3 32-character Base32 Hash
import base64
expected_raw = bytes.fromhex(hex_hash)
b32_str = base64.b32encode(expected_raw).decode("ascii")
magnet_b32 = f"magnet:?xt=urn:btih:{b32_str}&dn=Overlord"
extracted_b32 = QBitClient.extract_torrent_hash(magnet_b32)
if extracted_b32 == hex_hash:
    recorder.record_pass(f"Decode 32-character Base32 hash '{b32_str}' into exact 40-character hex")
else:
    recorder.record_fail("Base32 decoding", hex_hash, extracted_b32)

# 4.4 Lowercase 32-character Base32 Hash
magnet_b32_lower = f"magnet:?xt=urn:btih:{b32_str.lower()}&dn=Overlord"
extracted_b32_lower = QBitClient.extract_torrent_hash(magnet_b32_lower)
if extracted_b32_lower == hex_hash:
    recorder.record_pass("Decode lowercase Base32 hash accurately into 40-character hex")
else:
    recorder.record_fail("Lowercase Base32 decoding", hex_hash, extracted_b32_lower)

# 4.5 Malformed / Empty / None Hashes
if QBitClient.extract_torrent_hash("") is None:
    recorder.record_pass("Empty string hash extraction returns None safely")
else:
    recorder.record_fail("Empty hash extraction", None, QBitClient.extract_torrent_hash(""))

if QBitClient.extract_torrent_hash("https://example.com/not_a_magnet") is None:
    recorder.record_pass("Non-magnet URL returns None safely")
else:
    recorder.record_fail("Non-magnet extraction", None, QBitClient.extract_torrent_hash("https://example.com/not_a_magnet"))


# ==============================================================================
# SECTION 5: Result Type Polymorphism & Serialization
# ==============================================================================
print("\n--- SECTION 5: Result Type Polymorphism & Serialization ---")

# 5.1 ConnectionTestResult
ctr_true = ConnectionTestResult({"success": True, "version": "v5.0.0", "message": "OK"})
ctr_false = ConnectionTestResult({"success": False, "version": "", "message": "Failed"})

if bool(ctr_true) is True and ctr_true == True and ctr_true["version"] == "v5.0.0":
    recorder.record_pass("ConnectionTestResult(success=True) behaves as boolean True, == True, and dict")
else:
    recorder.record_fail("ConnectionTestResult True polymorphism", True, ctr_true)

if bool(ctr_false) is False and ctr_false == False:
    recorder.record_pass("ConnectionTestResult(success=False) behaves as boolean False and == False")
else:
    recorder.record_fail("ConnectionTestResult False polymorphism", False, ctr_false)

json_ctr = json.dumps(ctr_true)
if '"success": true' in json_ctr:
    recorder.record_pass("ConnectionTestResult serializes cleanly to JSON")
else:
    recorder.record_fail("ConnectionTestResult JSON serialization", "contains success: true", json_ctr)

# 5.2 SelectiveDownloadResult
sdr = SelectiveDownloadResult({"status": "filtered", "skipped_files": 4, "kept_files": 2})

if int(sdr) == 4 and sdr == 4 and sdr["status"] == "filtered":
    recorder.record_pass("SelectiveDownloadResult behaves as int(4), == 4, and dict")
else:
    recorder.record_fail("SelectiveDownloadResult int polymorphism", 4, int(sdr))

json_sdr = json.dumps(sdr)
if '"skipped_files": 4' in json_sdr:
    recorder.record_pass("SelectiveDownloadResult serializes cleanly to JSON")
else:
    recorder.record_fail("SelectiveDownloadResult JSON serialization", "contains skipped_files: 4", json_sdr)


# ==============================================================================
# SECTION 6: Network Fault Injection & Offline Resilience
# ==============================================================================
print("\n--- SECTION 6: Network Fault Injection & Offline Resilience ---")

# 6.1 Connection Refused / Unreachable Host
offline_client = QBitClient(host="127.0.0.1", port=59999, enabled=True)
conn_res = offline_client.test_connection(timeout=0.5, force=True)
if conn_res.success is False:
    recorder.record_pass("Offline client test_connection gracefully returns success=False without crashing")
else:
    recorder.record_fail("Offline client connection test", False, conn_res.success)

# 6.2 Disabled Client
disabled_client = QBitClient(host="127.0.0.1", port=8080, enabled=False)
disabled_res = disabled_client.test_connection(timeout=0.5, force=True)
if disabled_res.success is False and "disabled" in disabled_res["message"].lower():
    recorder.record_pass("Disabled client test_connection returns success=False and clear explanation")
else:
    recorder.record_fail("Disabled client test_connection", False, disabled_res)

# 6.3 Offline Client Queue Fetch
torrents_offline = offline_client.get_torrents()
if torrents_offline == []:
    recorder.record_pass("Offline client get_torrents returns empty list [] without crash")
else:
    recorder.record_fail("Offline client get_torrents", [], torrents_offline)

# 6.4 Offline Client Torrent Actions
if offline_client.pause_torrents("any_hash") is False:
    recorder.record_pass("Offline client pause_torrents returns False safely")
else:
    recorder.record_fail("Offline pause_torrents", False, True)

if offline_client.resume_torrents("any_hash") is False:
    recorder.record_pass("Offline client resume_torrents returns False safely")
else:
    recorder.record_fail("Offline resume_torrents", False, True)

if offline_client.delete_torrents("any_hash") is False:
    recorder.record_pass("Offline client delete_torrents returns False safely")
else:
    recorder.record_fail("Offline delete_torrents", False, True)


# ==============================================================================
# SECTION 7: Template Source & UI Contract Verification
# ==============================================================================
print("\n--- SECTION 7: Template Source & UI Contract Verification ---")

tpl_path = os.path.join(BACKEND_DIR, "app", "templates", "downloads.html")
with open(tpl_path, "r", encoding="utf-8") as f:
    tpl_src = f.read()

# 7.1 Verify Offline Badges and In-App Drawer in downloads.html
if "Needs In-App Setup" in tpl_src and "Online" in tpl_src and "Disconnected" in tpl_src:
    recorder.record_pass("downloads.html contains dynamic status badges for Online and Disconnected/Setup states")
else:
    recorder.record_fail("downloads.html status badges", "Online & Disconnected", "Not found in template source")

# 7.2 Verify Action Buttons
for btn in ["btn-pause", "btn-resume", "btn-delete"]:
    if btn in tpl_src:
        recorder.record_pass(f"downloads.html contains required action button class '{btn}'")
    else:
        recorder.record_fail(f"downloads.html action button {btn}", True, False)

# 7.3 Verify Base Navigation Integration
base_path = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
with open(base_path, "r", encoding="utf-8") as f:
    base_src = f.read()

if 'href="/downloads"' in base_src:
    recorder.record_pass("base.html contains navigation link to /downloads")
else:
    recorder.record_fail("base.html downloads link", True, False)


# ==============================================================================
# FINAL SUMMARY
# ==============================================================================
failed_count = recorder.summary()
print(f"\nEmpirical verification completed. Total failures detected: {failed_count}")
sys.exit(1 if failed_count > 0 else 0)

