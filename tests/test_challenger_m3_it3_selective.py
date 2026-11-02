#!/usr/bin/env python
"""
Milestone 3 Iteration 3: Challenger Selective Volume Download Invalidation Probing
Tests multi-volume conjunction & Oxford comma handling in apply_selective_volume_download.

Verification targets:
1. Oxford comma: DanMachi Vol 1, 2, and 3.epub
   - user owns {1.0}        -> KEPT (priority unchanged)
   - user owns {1.0, 2.0}   -> KEPT (priority unchanged)
   - user owns {1.0, 2.0, 3.0} -> DESELECTED (priority 0)
2. Plus '+': Slime Volume 1, 2, + 3.epub
   - user owns {1.0, 2.0}   -> KEPT (priority unchanged)
   - user owns {1.0, 2.0, 3.0} -> DESELECTED (priority 0)
3. Ampersand '&': Books 1, 2, & 3.epub
   - user owns {1.0, 2.0}   -> KEPT (priority unchanged)
   - user owns {1.0, 2.0, 3.0} -> DESELECTED (priority 0)
4. Wanted volumes semantics & whitespace variations
"""

import sys
import os
from typing import Dict, List, Set, Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))

from backend.app.downloader.qbit import (
    QBitClient,
    parse_torrent_filename_volumes,
    SelectiveDownloadResult,
)


class MockQBitClient(QBitClient):
    def __init__(self, files: List[Dict[str, Any]]):
        super().__init__(host="127.0.0.1", port=8080)
        self._mock_files = files
        self.priority_calls: List[Dict[str, Any]] = []

    def get_torrent_files(self, torrent_hash: str) -> List[Dict[str, Any]]:
        return self._mock_files

    def set_file_priorities(self, torrent_hash: str, file_indices: List[int], priority: int) -> bool:
        self.priority_calls.append({
            "hash": torrent_hash,
            "file_indices": list(file_indices),
            "priority": priority
        })
        return True


def run_selective_probing():
    passed = 0
    failed = 0
    failures = []

    def check(cond: bool, desc: str, detail: str = ""):
        nonlocal passed, failed
        if cond:
            print(f"  [PASS] {desc}")
            passed += 1
        else:
            print(f"  [FAIL] {desc} | {detail}")
            failed += 1
            failures.append((desc, detail))

    print("======================================================================")
    print("CHALLENGER M3-IT3: SELECTIVE VOLUME DOWNLOAD INVALIDATION PROBING")
    print("======================================================================\n")

    # ------------------------------------------------------------------
    # Step 1: Volume parsing tests on the target filenames
    # ------------------------------------------------------------------
    print("--- 1. parse_torrent_filename_volumes verification ---")
    f_oxford = "DanMachi Vol 1, 2, and 3.epub"
    is_anc, vols_ox = parse_torrent_filename_volumes(f_oxford)
    check(not is_anc and vols_ox == {1.0, 2.0, 3.0},
          f"Oxford comma filename '{f_oxford}'",
          f"Expected {{1.0, 2.0, 3.0}}, got anc={is_anc}, vols={vols_ox}")

    f_plus = "Slime Volume 1, 2, + 3.epub"
    is_anc, vols_plus = parse_torrent_filename_volumes(f_plus)
    check(not is_anc and vols_plus == {1.0, 2.0, 3.0},
          f"Plus filename '{f_plus}'",
          f"Expected {{1.0, 2.0, 3.0}}, got anc={is_anc}, vols={vols_plus}")

    f_amp = "Books 1, 2, & 3.epub"
    is_anc, vols_amp = parse_torrent_filename_volumes(f_amp)
    check(not is_anc and vols_amp == {1.0, 2.0, 3.0},
          f"Ampersand filename '{f_amp}'",
          f"Expected {{1.0, 2.0, 3.0}}, got anc={is_anc}, vols={vols_amp}")

    # Whitespace variations
    f_ox_nowhitespace = "DanMachi Vol 1,2,and 3.epub"
    _, vols_ox_nw = parse_torrent_filename_volumes(f_ox_nowhitespace)
    check(vols_ox_nw == {1.0, 2.0, 3.0},
          f"Oxford comma compact '{f_ox_nowhitespace}'",
          f"Expected {{1.0, 2.0, 3.0}}, got {vols_ox_nw}")

    f_ox_extra_space = "DanMachi Vol 1 , 2 , and 3.epub"
    _, vols_ox_es = parse_torrent_filename_volumes(f_ox_extra_space)
    check(vols_ox_es == {1.0, 2.0, 3.0},
          f"Oxford comma spaced '{f_ox_extra_space}'",
          f"Expected {{1.0, 2.0, 3.0}}, got {vols_ox_es}")

    # ------------------------------------------------------------------
    # Step 2: Invalidation probing against apply_selective_volume_download
    # ------------------------------------------------------------------
    print("\n--- 2. apply_selective_volume_download Probing ---")

    # We construct a multi-file torrent containing the candidate files plus an extras file
    torrent_files = [
        {"id": 0, "name": "DanMachi Vol 1, 2, and 3.epub", "size": 10485760},
        {"id": 1, "name": "Slime Volume 1, 2, + 3.epub", "size": 12582912},
        {"id": 2, "name": "Books 1, 2, & 3.epub", "size": 8388608},
        {"id": 3, "name": "DanMachi Vol 04.epub", "size": 5242880},
    ]

    # Test 2.1: Oxford comma DanMachi Vol 1, 2, and 3
    # Case A: User owns {1.0} -> file KEPT (priority unchanged)
    c1 = MockQBitClient(torrent_files)
    res_a = c1.apply_selective_volume_download("mock_hash_1", owned_volumes={1.0})
    danmachi_kept_a = 0 in res_a["kept_indices"]
    danmachi_skipped_a = 0 in res_a["skipped_indices"]
    c1_prio_0_ids = [idx for call in c1.priority_calls if call["priority"] == 0 for idx in call["file_indices"]]
    check(danmachi_kept_a and not danmachi_skipped_a and 0 not in c1_prio_0_ids,
          "DanMachi Oxford comma: User owns {1.0} -> file KEPT (priority unchanged)",
          f"kept={res_a['kept_indices']}, skipped={res_a['skipped_indices']}, prio0={c1_prio_0_ids}")

    # Case B: User owns {1.0, 2.0} -> file KEPT (priority unchanged) because Vol 3.0 is needed!
    c2 = MockQBitClient(torrent_files)
    res_b = c2.apply_selective_volume_download("mock_hash_2", owned_volumes={1.0, 2.0})
    danmachi_kept_b = 0 in res_b["kept_indices"]
    danmachi_skipped_b = 0 in res_b["skipped_indices"]
    c2_prio_0_ids = [idx for call in c2.priority_calls if call["priority"] == 0 for idx in call["file_indices"]]
    check(danmachi_kept_b and not danmachi_skipped_b and 0 not in c2_prio_0_ids,
          "DanMachi Oxford comma: User owns {1.0, 2.0} -> file KEPT (priority unchanged)",
          f"kept={res_b['kept_indices']}, skipped={res_b['skipped_indices']}, prio0={c2_prio_0_ids}")

    # Case C: User owns {1.0, 2.0, 3.0} -> file DESELECTED (priority 0)
    c3 = MockQBitClient(torrent_files)
    res_c = c3.apply_selective_volume_download("mock_hash_3", owned_volumes={1.0, 2.0, 3.0})
    danmachi_kept_c = 0 in res_c["kept_indices"]
    danmachi_skipped_c = 0 in res_c["skipped_indices"]
    c3_prio_0_ids = [idx for call in c3.priority_calls if call["priority"] == 0 for idx in call["file_indices"]]
    check(not danmachi_kept_c and danmachi_skipped_c and 0 in c3_prio_0_ids,
          "DanMachi Oxford comma: User owns {1.0, 2.0, 3.0} -> file DESELECTED (priority 0)",
          f"kept={res_c['kept_indices']}, skipped={res_c['skipped_indices']}, prio0={c3_prio_0_ids}")

    # Test 2.2: Plus '+' Slime Volume 1, 2, + 3.epub (id: 1)
    # Case A: User owns {1.0, 2.0} -> file KEPT
    c4 = MockQBitClient(torrent_files)
    res_slime_a = c4.apply_selective_volume_download("mock_hash_4", owned_volumes={1.0, 2.0})
    slime_kept_a = 1 in res_slime_a["kept_indices"]
    slime_skipped_a = 1 in res_slime_a["skipped_indices"]
    c4_prio_0_ids = [idx for call in c4.priority_calls if call["priority"] == 0 for idx in call["file_indices"]]
    check(slime_kept_a and not slime_skipped_a and 1 not in c4_prio_0_ids,
          "Slime '+': User owns {1.0, 2.0} -> file KEPT",
          f"kept={res_slime_a['kept_indices']}, skipped={res_slime_a['skipped_indices']}, prio0={c4_prio_0_ids}")

    # Case B: User owns {1.0, 2.0, 3.0} -> file DESELECTED
    c5 = MockQBitClient(torrent_files)
    res_slime_b = c5.apply_selective_volume_download("mock_hash_5", owned_volumes={1.0, 2.0, 3.0})
    slime_kept_b = 1 in res_slime_b["kept_indices"]
    slime_skipped_b = 1 in res_slime_b["skipped_indices"]
    c5_prio_0_ids = [idx for call in c5.priority_calls if call["priority"] == 0 for idx in call["file_indices"]]
    check(not slime_kept_b and slime_skipped_b and 1 in c5_prio_0_ids,
          "Slime '+': User owns {1.0, 2.0, 3.0} -> file DESELECTED (priority 0)",
          f"kept={res_slime_b['kept_indices']}, skipped={res_slime_b['skipped_indices']}, prio0={c5_prio_0_ids}")

    # Test 2.3: Ampersand '&' Books 1, 2, & 3.epub (id: 2)
    # Case A: User owns {1.0, 2.0} -> file KEPT
    c6 = MockQBitClient(torrent_files)
    res_amp_a = c6.apply_selective_volume_download("mock_hash_6", owned_volumes={1.0, 2.0})
    amp_kept_a = 2 in res_amp_a["kept_indices"]
    amp_skipped_a = 2 in res_amp_a["skipped_indices"]
    c6_prio_0_ids = [idx for call in c6.priority_calls if call["priority"] == 0 for idx in call["file_indices"]]
    check(amp_kept_a and not amp_skipped_a and 2 not in c6_prio_0_ids,
          "Books '&': User owns {1.0, 2.0} -> file KEPT",
          f"kept={res_amp_a['kept_indices']}, skipped={res_amp_a['skipped_indices']}, prio0={c6_prio_0_ids}")

    # Case B: User owns {1.0, 2.0, 3.0} -> file DESELECTED
    c7 = MockQBitClient(torrent_files)
    res_amp_b = c7.apply_selective_volume_download("mock_hash_7", owned_volumes={1.0, 2.0, 3.0})
    amp_kept_b = 2 in res_amp_b["kept_indices"]
    amp_skipped_b = 2 in res_amp_b["skipped_indices"]
    c7_prio_0_ids = [idx for call in c7.priority_calls if call["priority"] == 0 for idx in call["file_indices"]]
    check(not amp_kept_b and amp_skipped_b and 2 in c7_prio_0_ids,
          "Books '&': User owns {1.0, 2.0, 3.0} -> file DESELECTED (priority 0)",
          f"kept={res_amp_b['kept_indices']}, skipped={res_amp_b['skipped_indices']}, prio0={c7_prio_0_ids}")

    # ------------------------------------------------------------------
    # Step 3: Wanted Volumes semantic probing
    # ------------------------------------------------------------------
    print("\n--- 3. wanted_volumes Parameter Probing ---")
    c8 = MockQBitClient(torrent_files)
    res_wanted_3 = c8.apply_selective_volume_download("mock_hash_8", wanted_volumes={3.0})
    # Since ids 0, 1, 2 all contain volume 3.0, they should all be KEPT!
    check(0 in res_wanted_3["kept_indices"] and 1 in res_wanted_3["kept_indices"] and 2 in res_wanted_3["kept_indices"],
          "wanted_volumes={3.0}: All three multi-volume bundles containing Vol 3.0 KEPT",
          f"kept={res_wanted_3['kept_indices']}, skipped={res_wanted_3['skipped_indices']}")

    c9 = MockQBitClient(torrent_files)
    res_wanted_4 = c9.apply_selective_volume_download("mock_hash_9", wanted_volumes={4.0})
    # ids 0, 1, 2 only contain 1-3, so none have vol 4.0; id 3 is Vol 4.0 so id 3 is KEPT, ids 0,1,2 skipped!
    check(0 in res_wanted_4["skipped_indices"] and 1 in res_wanted_4["skipped_indices"] and 2 in res_wanted_4["skipped_indices"] and 3 in res_wanted_4["kept_indices"],
          "wanted_volumes={4.0}: Only DanMachi Vol 04 KEPT, bundles 1-3 skipped",
          f"kept={res_wanted_4['kept_indices']}, skipped={res_wanted_4['skipped_indices']}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n======================================================================")
    print(f"CHALLENGER SELECTIVE PROBING SUMMARY: {passed} PASSED | {failed} FAILED | {passed + failed} TOTAL")
    print("======================================================================")

    if failed > 0:
        print("\nFailures:")
        for desc, detail in failures:
            print(f"  - {desc}: {detail}")
        sys.exit(1)
    else:
        print("\nAll invalidation checks passed successfully.")
        sys.exit(0)


if __name__ == "__main__":
    run_selective_probing()
