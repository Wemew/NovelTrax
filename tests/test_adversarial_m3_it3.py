"""
Empirical Adversarial Stress Test Suite for Milestone 3 Iteration 3
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))

from app.downloader.qbit import (
    parse_torrent_filename_volumes,
    QBitClient,
    SelectiveDownloadResult
)
from app.calibre.scanner import parse_calibre_volumes_and_series
from app.parser import extract_series_and_volume

class AdversarialM3It3Tests(unittest.TestCase):
    def test_oxford_comma_and_conjunction_permutations(self):
        cases = [
            ("DanMachi Vol 1, 2, and 3.epub", {1.0, 2.0, 3.0}),
            ("DanMachi Vol 1, 2, & 3.epub", {1.0, 2.0, 3.0}),
            ("DanMachi Vol 1, 2, + 3.epub", {1.0, 2.0, 3.0}),
            ("Overlord Vol 1, 2, 3, 4, and 5.epub", {1.0, 2.0, 3.0, 4.0, 5.0}),
            ("Overlord Vol 1, 2, 3, 4, + 5.epub", {1.0, 2.0, 3.0, 4.0, 5.0}),
            ("Overlord Vol 1, 2, 3, 4, & 5.epub", {1.0, 2.0, 3.0, 4.0, 5.0}),
            ("Mushoku Tensei v1, 2, 3, 4, 5, and 6.epub", {1.0, 2.0, 3.0, 4.0, 5.0, 6.0}),
            ("Slime Volume 1, Volume 2, and Volume 3.epub", {1.0, 2.0, 3.0}),
            ("Ascendance of a Bookworm Books 1, 2, 3, and 4.epub", {1.0, 2.0, 3.0, 4.0}),
            ("Series Vol 1, 1.5, and 2.epub", {1.0, 1.5, 2.0}),
            ("Series Vol 1, 2.5, + 3.epub", {1.0, 2.5, 3.0}),
        ]
        for fname, expected in cases:
            is_anc, vols = parse_torrent_filename_volumes(fname)
            self.assertFalse(is_anc, f"Expected {fname} not to be ancillary")
            self.assertEqual(vols, expected, f"Failed parsing {fname}: got {vols}, expected {expected}")

    def test_calibre_scanner_oxford_comma(self):
        cases = [
            ("DanMachi Vol 1, 2, and 3", {1.0, 2.0, 3.0}),
            ("DanMachi Vol 1, 2, & 3", {1.0, 2.0, 3.0}),
            ("DanMachi Vol 1, 2, + 3", {1.0, 2.0, 3.0}),
            ("Overlord Vol 1, 2, 3, 4, and 5", {1.0, 2.0, 3.0, 4.0, 5.0}),
            ("Overlord Vol 1, 2, 3, 4, + 5", {1.0, 2.0, 3.0, 4.0, 5.0}),
        ]
        for title, expected in cases:
            series, vols = parse_calibre_volumes_and_series(title)
            self.assertEqual(vols, expected, f"Calibre scanner failed on {title}: got {vols}, expected {expected}")

    def test_feed_parser_oxford_comma(self):
        cases = [
            ("DanMachi Vol 1, 2, and 3", [1.0, 2.0, 3.0]),
            ("DanMachi Vol 1, 2, & 3", [1.0, 2.0, 3.0]),
            ("DanMachi Vol 1, 2, + 3", [1.0, 2.0, 3.0]),
            ("Overlord Vol 1, 2, 3, 4, and 5", [1.0, 2.0, 3.0, 4.0, 5.0]),
            ("Overlord Vol 1, 2, 3, 4, + 5", [1.0, 2.0, 3.0, 4.0, 5.0]),
        ]
        for title, expected in cases:
            res = extract_series_and_volume(title)
            self.assertIsNotNone(res, f"Feed parser failed to parse {title}")
            self.assertTrue(res.get("is_bundle"), f"Expected is_bundle=True for {title}")
            self.assertEqual(res.get("volumes"), expected, f"Feed parser volumes mismatch for {title}")

    def test_selective_volume_download_simulation(self):
        client = QBitClient(host="127.0.0.1", port=8080)
        client.get_torrent_files = lambda th: [
            {"id": 0, "name": "Mushoku Tensei Vol 1 & 2.epub"},
            {"id": 1, "name": "Overlord v1, 2, 3.epub"},
            {"id": 2, "name": "DanMachi Vol 1, 2, and 3.epub"},
            {"id": 3, "name": "DanMachi Vol 1, 2, 3, 4, and 5.epub"},
        ]
        client.set_file_priorities = lambda th, idxs, priority: True

        # Test Case 1: user owns {1.0, 2.0}
        # - Mushoku Tensei Vol 1 & 2: owned {1.0, 2.0} -> SKIPPED
        # - Overlord v1, 2, 3: needs 3.0 -> KEPT
        # - DanMachi Vol 1, 2, and 3: needs 3.0 -> KEPT
        # - DanMachi Vol 1, 2, 3, 4, and 5: needs 3, 4, 5 -> KEPT
        res1 = client.apply_selective_volume_download("mock_hash1", owned_volumes={1.0, 2.0})
        self.assertEqual(res1["status"], "filtered")
        self.assertIn(0, res1["skipped_indices"])
        self.assertIn(1, res1["kept_indices"])
        self.assertIn(2, res1["kept_indices"])
        self.assertIn(3, res1["kept_indices"])

        # Test Case 2: user owns {1.0, 2.0, 3.0}
        # - Mushoku Tensei Vol 1 & 2: owned {1.0, 2.0} -> SKIPPED
        # - Overlord v1, 2, 3: owned {1.0, 2.0, 3.0} -> SKIPPED
        # - DanMachi Vol 1, 2, and 3: owned {1.0, 2.0, 3.0} -> SKIPPED
        # - DanMachi Vol 1, 2, 3, 4, and 5: needs 4, 5 -> KEPT
        res2 = client.apply_selective_volume_download("mock_hash2", owned_volumes={1.0, 2.0, 3.0})
        self.assertEqual(res2["status"], "filtered")
        self.assertIn(0, res2["skipped_indices"])
        self.assertIn(1, res2["skipped_indices"])
        self.assertIn(2, res2["skipped_indices"])
        self.assertIn(3, res2["kept_indices"])

        # Test Case 3: user owns {1.0, 2.0, 3.0, 4.0, 5.0}
        # All files are owned -> ALL SKIPPED
        res3 = client.apply_selective_volume_download("mock_hash3", owned_volumes={1.0, 2.0, 3.0, 4.0, 5.0})
        self.assertEqual(res3["status"], "all_owned")
        self.assertEqual(set(res3["skipped_indices"]), {0, 1, 2, 3})
        self.assertEqual(len(res3["kept_indices"]), 0)

    def test_info_hash_boundaries_and_malformed_urls(self):
        # Truncated hashes
        self.assertIsNone(QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:4a5b6c7d8e9f0123456789abcdef0123456789a")) # 39 hex
        self.assertIsNone(QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:01234567890123456789012345678901234")) # 35 hex
        self.assertIsNone(QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:JJZEYZLZJJZEYZLZJJZEYZLZJJZEYZL")) # 31 b32
        self.assertIsNone(QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:12345678901234567890")) # 20 hex
        self.assertIsNone(QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:a")) # 1 char
        self.assertIsNone(QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:&dn=Test")) # empty btih

        # Overflow hashes
        self.assertIsNone(QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:4a5b6c7d8e9f0123456789abcdef0123456789ab1")) # 41 hex
        self.assertIsNone(QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:4a5b6c7d8e9f0123456789abcdef0123456789ab123")) # 43 hex
        self.assertIsNone(QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:JJZEYZLZJJZEYZLZJJZEYZLZJJZEYZLZ2")) # 33 b32

        # Invalid characters in 32 or 40 char candidates
        self.assertIsNone(QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:88888888888888888888888888888888")) # 32 chars non-b32
        self.assertIsNone(QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:01010101010101010101010101010101")) # 0 and 1 are invalid b32
        self.assertIsNone(QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:4a5b6c7d8e9f0123456789abcdef0123456789ag")) # 40 chars non-hex ('g')

        # Bare truncated/overflow/invalid
        self.assertIsNone(QBitClient.extract_torrent_hash("4a5b6c7d8e9f0123456789abcdef0123456789a")) # 39 hex
        self.assertIsNone(QBitClient.extract_torrent_hash("4a5b6c7d8e9f0123456789abcdef0123456789ab1")) # 41 hex
        self.assertIsNone(QBitClient.extract_torrent_hash("01234567890123456789012345678901234")) # 35 hex
        self.assertIsNone(QBitClient.extract_torrent_hash("JJZEYZLZJJZEYZLZJJZEYZLZJJZEYZL")) # 31 b32
        self.assertIsNone(QBitClient.extract_torrent_hash("JJZEYZLZJJZEYZLZJJZEYZLZJJZEYZLZ2")) # 33 b32
        self.assertIsNone(QBitClient.extract_torrent_hash("88888888888888888888888888888888")) # 32 chars non-b32
        self.assertIsNone(QBitClient.extract_torrent_hash("4a5b6c7d8e9f0123456789abcdef0123456789ag")) # 40 non-hex

        # Valid 40-hex lowercase and uppercase
        canonical_hex = "4a5b6c7d8e9f0123456789abcdef0123456789ab"
        self.assertEqual(QBitClient.extract_torrent_hash(canonical_hex), canonical_hex)
        self.assertEqual(QBitClient.extract_torrent_hash(canonical_hex.upper()), canonical_hex)
        self.assertEqual(QBitClient.extract_torrent_hash(f"magnet:?xt=urn:btih:{canonical_hex}"), canonical_hex)
        self.assertEqual(QBitClient.extract_torrent_hash(f"magnet:?xt=urn:btih:{canonical_hex.upper()}"), canonical_hex)
        self.assertEqual(QBitClient.extract_torrent_hash(f"magnet:?dn=TestNovel&xt=Urn:Btih:{canonical_hex.upper()}&tr=udp://tracker"), canonical_hex)

        # Valid 32-Base32
        b32_hash = "JJZEYZLZJJZEYZLZJJZEYZLZJJZEYZLZ"
        expected_hex = "4a724c65794a724c65794a724c65794a724c6579"
        self.assertEqual(QBitClient.extract_torrent_hash(b32_hash), expected_hex)
        self.assertEqual(QBitClient.extract_torrent_hash(b32_hash.lower()), expected_hex)
        self.assertEqual(QBitClient.extract_torrent_hash(f"magnet:?xt=urn:btih:{b32_hash}"), expected_hex)
        self.assertEqual(QBitClient.extract_torrent_hash(f"magnet:?xt=urn:btih:{b32_hash.lower()}"), expected_hex)
        self.assertEqual(QBitClient.extract_torrent_hash(f"magnet:?dn=Book&xt=Urn:Btih:{b32_hash}&tr=http://tracker"), expected_hex)


def run():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AdversarialM3It3Tests)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print("\nALL ADVERSARIAL M3 ITERATION 3 TESTS PASSED!")

if __name__ == "__main__":
    run()
