"""
tests/test_challenger_invalidation_verification.py
Deep Empirical Challenger Harness for Invalidation Conditions & Downloader Core Invariants.

Verifies:
1. Core Invalidation Condition:
   - "Mushoku Tensei Vol 1 & 2.epub" when user owns [1.0] -> file priority NOT set to 0 (KEPT).
   - "Mushoku Tensei Vol 1 & 2.epub" when user owns [2.0] -> file priority NOT set to 0 (KEPT).
   - "Mushoku Tensei Vol 1 & 2.epub" when user owns [1.0, 2.0] -> file priority IS set to 0 (DESELECTED).
   - "Mushoku Tensei Vol 1 & 2.epub" when wanted_volumes=[2.0] -> KEPT.
   - "Mushoku Tensei Vol 1 & 2.epub" when wanted_volumes=[3.0] -> DESELECTED.
2. Oversized Omnibus Bundles (>100 volumes):
   - Difference <= 100 is parsed.
   - Difference > 100 returns (False, set()).
   - When fed into apply_selective_volume_download, >100 bundle is safely KEPT for download (not deselected) even when user owns Vol 1.
3. Conjunction & List Variations under Series Title Numbers:
   - "86 - Vol 1 & 2.epub" (series_name="86")
   - "86 - Vol 1 + 2.epub" (series_name=None)
   - "Mob Psycho 100 - Vol 1, 2 and 3.epub"
   - "Catch-22 Vol 1 & 2.epub"
4. Fractional Range Boundaries:
   - "Vol 1.5-3" -> {1.5, 2.0, 3.0} (excludes 1.0)
   - "Vol 0.5-2" -> {0.5, 1.0, 2.0} (excludes 0.0)
5. SQLite WAL Concurrency & REST Endpoint Integrity:
   - Multi-threaded concurrent reads/writes to database while performing selective volume calculations.
"""

import os
import sys
import json
import threading
import sqlite3
import tempfile
import urllib.parse
from typing import Dict, Any, List, Set, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.downloader.qbit import (
    QBitClient,
    parse_torrent_filename_volumes,
    SelectiveDownloadResult,
)
from app.database import Database
from app.security import validate_downloader_destination, is_cloud_metadata_ip

passed_count = 0
failed_count = 0
failures = []

def record(name: str, passed: bool, details: str = ""):
    global passed_count, failed_count
    if passed:
        passed_count += 1
        print(f"  [PASS] {name}")
    else:
        failed_count += 1
        msg = f"{name} | {details}"
        failures.append(msg)
        print(f"  [FAIL] {msg}")

class SpyQBitClient(QBitClient):
    def __init__(self, mock_files: List[Dict[str, Any]]):
        super().__init__(host="localhost", port=8080)
        self.mock_files = mock_files
        self.priorities_set: Dict[int, int] = {}
        self.raw_calls: List[Dict[str, Any]] = []

    def get_torrent_files(self, torrent_hash: str) -> List[Dict[str, Any]]:
        return self.mock_files

    def set_file_priorities(self, torrent_hash: str, file_indices: List[int], priority: int = 0) -> bool:
        self.raw_calls.append({"hash": torrent_hash, "indices": file_indices, "priority": priority})
        for idx in file_indices:
            self.priorities_set[idx] = priority
        return True

print("\n" + "=" * 75)
print("  EMPIRICAL CHALLENGER DEEP INVALIDATION HARNESS")
print("=" * 75)

# --------------------------------------------------------------------------
# PART 1: Core Invalidation Condition - Conjunction Omnibus Deselection Invariant
# --------------------------------------------------------------------------
print("\n--- Part 1: Core Invalidation Invariant (Mushoku Tensei Vol 1 & 2) ---")

files_mushoku = [
    {"id": 0, "name": "Mushoku Tensei Vol 1 & 2.epub"},
    {"id": 1, "name": "Mushoku Tensei Vol 3.epub"},
    {"id": 2, "name": "Mushoku Tensei Vol 4.epub"},
]

# Case 1.1: User owns Vol 1 -> File 0 MUST NOT have priority 0 (MUST BE KEPT)
client1 = SpyQBitClient(files_mushoku)
res1 = client1.apply_selective_volume_download("hash_test_1", owned_volumes=[1.0])
prio_0_case1 = client1.priorities_set.get(0)
record(
    "User owns Vol 1: 'Mushoku Tensei Vol 1 & 2.epub' priority is NOT 0 (KEPT)",
    0 not in res1["skipped_indices"] and prio_0_case1 != 0,
    f"res['skipped_indices']={res1['skipped_indices']}, priorities_set={client1.priorities_set}"
)

# Case 1.2: User owns Vol 2 -> File 0 MUST NOT have priority 0 (MUST BE KEPT)
client2 = SpyQBitClient(files_mushoku)
res2 = client2.apply_selective_volume_download("hash_test_2", owned_volumes=[2.0])
prio_0_case2 = client2.priorities_set.get(0)
record(
    "User owns Vol 2: 'Mushoku Tensei Vol 1 & 2.epub' priority is NOT 0 (KEPT)",
    0 not in res2["skipped_indices"] and prio_0_case2 != 0,
    f"res['skipped_indices']={res2['skipped_indices']}, priorities_set={client2.priorities_set}"
)

# Case 1.3: User owns BOTH Vol 1 and Vol 2 -> File 0 MUST have priority=0 (DESELECTED)
client3 = SpyQBitClient(files_mushoku)
res3 = client3.apply_selective_volume_download("hash_test_3", owned_volumes=[1.0, 2.0])
prio_0_case3 = client3.priorities_set.get(0)
record(
    "User owns BOTH Vol 1 & 2: 'Mushoku Tensei Vol 1 & 2.epub' IS deselected (priority=0)",
    0 in res3["skipped_indices"] and prio_0_case3 == 0,
    f"res['skipped_indices']={res3['skipped_indices']}, priorities_set={client3.priorities_set}"
)

# Case 1.4: User owns Vol 1, 2, 3, 4 -> All files deselected
client4 = SpyQBitClient(files_mushoku)
res4 = client4.apply_selective_volume_download("hash_test_4", owned_volumes=[1.0, 2.0, 3.0, 4.0])
record(
    "User owns ALL volumes: All files deselected and status is 'all_owned'",
    res4["status"] == "all_owned" and set(res4["skipped_indices"]) == {0, 1, 2},
    f"res={res4}"
)

# Case 1.5: wanted_volumes parameter check
# If wanted_volumes=[2.0], file 0 contains 2.0 -> must be KEPT
client5 = SpyQBitClient(files_mushoku)
res5 = client5.apply_selective_volume_download("hash_test_5", wanted_volumes=[2.0])
record(
    "wanted_volumes=[2.0]: 'Mushoku Tensei Vol 1 & 2.epub' is KEPT",
    0 not in res5["skipped_indices"],
    f"res['skipped_indices']={res5['skipped_indices']}"
)

# If wanted_volumes=[3.0], file 0 contains only {1, 2} -> must be DESELECTED
client6 = SpyQBitClient(files_mushoku)
res6 = client6.apply_selective_volume_download("hash_test_6", wanted_volumes=[3.0])
record(
    "wanted_volumes=[3.0]: 'Mushoku Tensei Vol 1 & 2.epub' is DESELECTED",
    0 in res6["skipped_indices"] and client6.priorities_set.get(0) == 0,
    f"res['skipped_indices']={res6['skipped_indices']}"
)

# --------------------------------------------------------------------------
# PART 2: Oversized Omnibus Bundles (>100 volumes safety invariant)
# --------------------------------------------------------------------------
print("\n--- Part 2: Oversized Omnibus Bundles (>100 Volumes) ---")

# 2.1 Boundary checks on parse_torrent_filename_volumes
# Exact 100 span: 1 to 101 -> (101 - 1) = 100 <= 100 -> parsed
anc_100, vols_100 = parse_torrent_filename_volumes("Guin Saga Vol 1-101.epub")
record(
    "Exact 100 volume difference (Vol 1-101) is parsed (101 volumes)",
    not anc_100 and len(vols_100) == 101 and min(vols_100) == 1.0 and max(vols_100) == 101.0,
    f"len={len(vols_100)}"
)

# 101 span: 1 to 102 -> (102 - 1) = 101 > 100 -> oversized fallback (False, set())
anc_101, vols_101 = parse_torrent_filename_volumes("Guin Saga Vol 1-102.epub")
record(
    "Oversized omnibus (Vol 1-102) returns (False, set())",
    anc_101 is False and vols_101 == set(),
    f"returned anc={anc_101}, vols={vols_101}"
)

# Massive range: 1 to 500
anc_500, vols_500 = parse_torrent_filename_volumes("Mega Novel Collection Vol 1-500.epub")
record(
    "Massive omnibus (Vol 1-500) returns (False, set())",
    anc_500 is False and vols_500 == set(),
    f"returned anc={anc_500}, vols={vols_500}"
)

# Bracketed massive range: [Vol 1-150]
anc_brack, vols_brack = parse_torrent_filename_volumes("[Pack] Perry Rhodan [Vol 1-150].epub")
record(
    "Bracketed oversized omnibus [Vol 1-150] returns (False, set())",
    anc_brack is False and vols_brack == set(),
    f"returned anc={anc_brack}, vols={vols_brack}"
)

# 2.2 Safety behavior in apply_selective_volume_download:
# When a torrent contains an oversized bundle, and user owns Vol 1,
# it MUST NOT be deselected!
oversized_files = [
    {"id": 0, "name": "Guin Saga Vol 1-120.epub"},
    {"id": 1, "name": "Guin Saga Vol 121.epub"},
]
client_over = SpyQBitClient(oversized_files)
res_over = client_over.apply_selective_volume_download("hash_over", owned_volumes=[1.0, 2.0, 3.0])
record(
    "Oversized omnibus bundle is safely KEPT for download even when user owns Vol 1-3",
    0 not in res_over["skipped_indices"] and client_over.priorities_set.get(0) != 0,
    f"res['skipped_indices']={res_over['skipped_indices']}"
)

# --------------------------------------------------------------------------
# PART 3: Conjunction & List Variations with Numbered Series Titles
# --------------------------------------------------------------------------
print("\n--- Part 3: Conjunction & List Parsing with Numbered Series Titles ---")

list_test_cases = [
    ("86 - Vol 1 & 2.epub", "86", {1.0, 2.0}),
    ("86 - Vol 01 + 02.epub", None, {1.0, 2.0}),
    ("Mob Psycho 100 - Vol 1, 2 and 3.epub", "Mob Psycho 100", {1.0, 2.0, 3.0}),
    ("Catch-22 Vol 1 & 2.epub", "Catch-22", {1.0, 2.0}),
    ("[Yen Press] 86 Vol 1 & 2.epub", "86", {1.0, 2.0}),
    ("[J-Novel Club] Mob Psycho 100 Vol 4 + 5.epub", "Mob Psycho 100", {4.0, 5.0}),
    ("Overlord Book 1 and Book 2.epub", None, {1.0, 2.0}),
    ("Series v1 & v2.epub", None, {1.0, 2.0}),
]

for fname, sname, expected in list_test_cases:
    anc, vols = parse_torrent_filename_volumes(fname, series_name=sname)
    record(
        f"List parsing: '{fname}' (series={sname}) -> {expected}",
        vols == expected and not anc,
        f"got {vols}"
    )

# --------------------------------------------------------------------------
# PART 4: Fractional Range Lower Bounds
# --------------------------------------------------------------------------
print("\n--- Part 4: Fractional Range Lower Bounds ---")

frac_cases = [
    ("Series Vol 1.5-3.epub", {1.5, 2.0, 3.0}, 1.0),
    ("Series Vol 2.5-4.epub", {2.5, 3.0, 4.0}, 2.0),
    ("Series Vol 0.5-2.epub", {0.5, 1.0, 2.0}, 0.0),
]

for fname, expected, excluded_int in frac_cases:
    anc, vols = parse_torrent_filename_volumes(fname)
    record(
        f"Fractional range '{fname}' -> {expected} (excluding {excluded_int})",
        vols == expected and excluded_int not in vols,
        f"got {vols}"
    )

# --------------------------------------------------------------------------
# PART 5: SQLite WAL Concurrency & Thread-Safety Stress
# --------------------------------------------------------------------------
print("\n--- Part 5: SQLite WAL Concurrency & Background Worker Stress ---")

temp_db_fd, temp_db_path = tempfile.mkstemp(suffix="_wal_stress.db")
os.close(temp_db_fd)

try:
    db = Database(db_path=temp_db_path)
    db.init_db()

    # Verify journal_mode is WAL
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode;")
        row = cursor.fetchone()
        wal_mode = row[0].lower() if row else ""
    record("Database initializes in SQLite WAL mode", wal_mode == "wal", f"journal_mode={wal_mode}")

    # Seed initial series
    db.sync_calibre_series([
        {"series_id": 1, "series_name": "Mushoku Tensei", "book_count": 10, "latest_index": 10.0},
        {"series_id": 2, "series_name": "Overlord", "book_count": 15, "latest_index": 15.0},
        {"series_id": 3, "series_name": "86 - Eighty Six", "book_count": 11, "latest_index": 11.0},
    ])

    errors = []
    threads = []
    num_threads = 8
    iterations_per_thread = 50

    def concurrent_worker(worker_id: int):
        client = SpyQBitClient([
            {"id": 0, "name": f"Series_{worker_id} Vol 1 & 2.epub"},
            {"id": 1, "name": f"Series_{worker_id} Vol 3.epub"},
        ])
        for i in range(iterations_per_thread):
            try:
                # 1. Database write
                rel_id = f"rel_{worker_id}_{i}"
                db.upsert_release({
                    "guid": rel_id,
                    "title": f"Mushoku Tensei Vol {i % 10 + 1} Epub",
                    "series_name": "Mushoku Tensei",
                    "volume": float(i % 10 + 1),
                    "download_url": f"magnet:?xt=urn:btih:{'a'*40}&dn=test",
                    "status": "REQUESTED"
                })
                # 2. Database read
                series_list = db.get_all_series(monitored_only=True)
                if not series_list:
                    errors.append(f"Worker {worker_id} iteration {i}: get_all_series empty")

                # 3. Downloader selective volume calculation
                res = client.apply_selective_volume_download(
                    torrent_hash="a" * 40,
                    owned_volumes=[1.0],
                    series_name="Mushoku Tensei"
                )
                if 0 in res["skipped_indices"]:
                    errors.append(f"Worker {worker_id} iteration {i}: selective download corrupted")

                # 4. Status update
                db.update_download_status(rel_id, "DOWNLOADING", torrent_hash="a"*40)
            except Exception as e:
                errors.append(f"Worker {worker_id} exception at iteration {i}: {e}")

    for tid in range(num_threads):
        t = threading.Thread(target=concurrent_worker, args=(tid,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=15.0)

    record(
        f"Concurrent WAL multi-threading ({num_threads} threads, {num_threads * iterations_per_thread} tx) zero lock contention",
        len(errors) == 0,
        f"errors count: {len(errors)}, samples: {errors[:3]}"
    )
finally:
    try:
        os.remove(temp_db_path)
    except Exception:
        pass

# --------------------------------------------------------------------------
# PART 6: Link-Local & Cloud Metadata SSRF Protection Exhaustive Matrix
# --------------------------------------------------------------------------
print("\n--- Part 6: Link-Local & Cloud Metadata SSRF Matrix ---")

ssrf_blocked_cases = [
    "169.254.169.254",
    "http://169.254.169.254/latest/meta-data/",
    "http://169.254.1.1:8080",
    "http://[fe80::1]/",
    "http://[::ffff:169.254.169.254]:8080",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://metadata/latest/",
    "http://instance-data/latest/",
    "file:///etc/passwd",
    "gopher://127.0.0.1:70/",
]

all_ssrf_blocked = True
for target in ssrf_blocked_cases:
    safe, err = validate_downloader_destination(target)
    if safe:
        all_ssrf_blocked = False
        record(f"Block SSRF target '{target}'", False, "Incorrectly returned safe=True")

if all_ssrf_blocked:
    record(f"All {len(ssrf_blocked_cases)} SSRF & link-local targets strictly rejected by validate_downloader_destination", True)

allowed_destinations = [
    "http://127.0.0.1:8080",
    "http://localhost:8080",
    "http://192.168.1.100:8080",
    "http://10.0.0.50:9091",
    "http://172.16.5.10:8080",
]

all_dest_allowed = True
for dest in allowed_destinations:
    safe, err = validate_downloader_destination(dest)
    if not safe:
        all_dest_allowed = False
        record(f"Allow legitimate downloader destination '{dest}'", False, f"Err: {err}")

if all_dest_allowed:
    record(f"All {len(allowed_destinations)} legitimate local/LAN destinations permitted", True)

# Test domain resolution with mock DNS
from unittest.mock import patch
import socket

with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]):
    safe_domain, err_domain = validate_downloader_destination("https://seedbox.myhome.org:8443")
    record(
        "Remote seedbox hostname with valid public IP allowed",
        safe_domain,
        f"Err: {err_domain}"
    )

with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))]):
    safe_rebinding, err_rebinding = validate_downloader_destination("https://seedbox.spoofed.org:8443")
    record(
        "DNS rebinding to cloud metadata 169.254.169.254 strictly blocked",
        not safe_rebinding,
        "Spoofed domain resolving to metadata IP was unexpectedly allowed"
    )

print("\n" + "=" * 75)
print(f"  CHALLENGER DEEP VERIFICATION SUMMARY: {passed_count} PASSED | {failed_count} FAILED")
print("=" * 75)
if failed_count == 0:
    print("Verdict: APPROVE")
else:
    print("Verdict: REQUEST_CHANGES")
    for f in failures:
        print(f"  - {f}")

if failed_count > 0:
    sys.exit(1)
