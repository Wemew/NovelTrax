#!/usr/bin/env python3
r"""
tests/test_challenger_m4_nyaa.py
Adversarial Challenger Test Harness for NovelTrax Milestone 4:
Nyaa Light Novel Provider, Prioritized Groups, Series Ranking Boost & Ignore State Machine.

Execution Command:
  & "C:\Program Files\Calibre2\calibre-debug.exe" -e tests\test_challenger_m4_nyaa.py
"""

import os
import sys
import json
import socket
import sqlite3
import tempfile
import shutil
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
from unittest.mock import patch, MagicMock

# Ensure backend directory is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.providers.nyaa import NyaaNovelProvider
from app.providers.base import ReleaseItem
from app.parser import (
    is_pdf_release,
    extract_series_and_volume,
    extract_release_group,
    is_valid_group_candidate,
    KNOWN_PUBLISHERS,
    DEFAULT_KNOWN_GROUPS,
    BUZZWORDS
)
from app.database import Database


class ChallengerRecorder:
    """Non-strippable test recorder for Calibre -O2 execution."""
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures: List[Dict[str, str]] = []

    def check(self, condition: bool, test_name: str, details: str = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  [PASS] {test_name}")
            return True
        else:
            self.failed += 1
            self.failures.append({"test": test_name, "details": details})
            print(f"  [FAIL] {test_name}\n         Details: {details}")
            return False


# =============================================================================
# Mock Responses Helper
# =============================================================================
class MockHttpResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


# =============================================================================
# PART 1: Nyaa Novel Provider Stress & Fuzz Testing
# =============================================================================
def run_part_1_nyaa_fuzz(r: ChallengerRecorder):
    print("\n" + "=" * 75)
    print("  PART 1: NYAA NOVEL PROVIDER STRESS & FUZZ TESTING")
    print("=" * 75)

    provider = NyaaNovelProvider()

    # 1.1 Fuzz HTML crawler with malformed / boundary inputs
    print("\n--- 1.1 HTML Crawler Fuzzing & Malformed Schemas ---")

    # A. Zero-byte content
    with patch("app.providers.nyaa.safe_http_get", return_value=MockHttpResponse("")):
        res_empty = provider.crawl_pages(max_pages=1)
        r.check(isinstance(res_empty, list) and len(res_empty) == 0,
                "Zero-byte HTML content yields empty list without exception")

    # B. HTML without table
    with patch("app.providers.nyaa.safe_http_get", return_value=MockHttpResponse("<html><body><div>No tables here</div></body></html>")):
        res_no_table = provider.crawl_pages(max_pages=1)
        r.check(len(res_no_table) == 0, "HTML without torrent-list table terminates crawl gracefully")

    # C. Malformed table with empty rows & truncated tags
    fuzz_malformed_html = """
    <table class="torrent-list">
      <tbody>
        <tr></tr>
        <tr><td></td></tr>
        <tr><td>Col1</td><td>Col2 no link</td></tr>
        <tr>
          <td><a href="/c/3_1">Lit</a></td>
          <td>
            <!-- Only comments link, missing main title -->
            <a href="/view/111#comments" class="comments">3</a>
          </td>
        </tr>
        <tr>
          <td><a href="/c/3_1">Lit</a></td>
          <td>
            <a href="/view/222">   </a> <!-- Whitespace title -->
          </td>
        </tr>
        <tr class="danger">
          <td><a href="/c/3_1">Lit</a></td>
          <td>
            <a href="/view/333#comments" class="comments">10</a>
            <a href="/view/333" title="Valid Title Vol. 1">Valid Title Vol. 1</a>
          </td>
          <td>
            <a href="/download/333.torrent"><i class="fa fa-download"></i></a>
            <a href="magnet:?xt=urn:btih:3333333333333333333333333333333333333333&dn=Valid+Title+Vol.+1"></a>
          </td>
          <td>50.0 MiB</td>
          <td data-timestamp="1730000000">2026-10-27</td>
          <td>999999</td>
          <td>12</td>
        </tr>
      </tbody>
    </table>
    """
    with patch("app.providers.nyaa.safe_http_get", return_value=MockHttpResponse(fuzz_malformed_html)):
        res_fuzz = provider.crawl_pages(max_pages=1)
        r.check(len(res_fuzz) == 1,
                "Malformed table fuzzing filters empty/broken rows and extracts the 1 valid row",
                f"Got {len(res_fuzz)} items")
        if res_fuzz:
            r.check(res_fuzz[0].series_name == "Valid Title",
                    "Valid row extracted correct series name from malformed table")
            r.check(res_fuzz[0].raw_data.get("seeders") == 999999,
                    "Large integer seeders parsed correctly")

    # D. Non-numeric seeders & leechers fuzzing
    fuzz_non_numeric_html = """
    <table class="torrent-list">
      <tbody>
        <tr>
          <td></td>
          <td><a href="/view/444">Non Numeric Seeds Vol. 1</a></td>
          <td><a href="magnet:?xt=urn:btih:4444444444444444444444444444444444444444"></a></td>
          <td>10 MB</td>
          <td data-timestamp="not_a_number">Invalid Date</td>
          <td>N/A</td>
          <td>-</td>
        </tr>
        <tr>
          <td></td>
          <td><a href="/view/555">Empty Seeds Vol. 2</a></td>
          <td><a href="magnet:?xt=urn:btih:5555555555555555555555555555555555555555"></a></td>
          <td></td>
          <td></td>
          <td></td>
          <td></td>
        </tr>
      </tbody>
    </table>
    """
    with patch("app.providers.nyaa.safe_http_get", return_value=MockHttpResponse(fuzz_non_numeric_html)):
        res_non_num = provider.crawl_pages(max_pages=1)
        r.check(len(res_non_num) == 2, "Rows with non-numeric / empty seeds and dates parsed without crash")
        if len(res_non_num) >= 2:
            r.check(res_non_num[0].raw_data.get("seeders") is None, "Non-numeric seeders 'N/A' parsed as None")
            r.check(res_non_num[0].raw_data.get("leechers") is None, "Non-numeric leechers '-' parsed as None")
            r.check(res_non_num[1].raw_data.get("seeders") is None, "Empty seeders td parsed as None")

    # E. Date cutoff boundary testing
    date_cutoff_html = """
    <table class="torrent-list">
      <tbody>
        <tr>
          <td></td>
          <td><a href="/view/601">Recent Release Vol. 1</a></td>
          <td><a href="magnet:?xt=urn:btih:6016016016016016016016016016016016016016"></a></td>
          <td>10 MB</td>
          <td data-timestamp="1730000000">Recent</td>
          <td>5</td><td>1</td>
        </tr>
        <tr>
          <td></td>
          <td><a href="/view/602">Old Release Vol. 2</a></td>
          <td><a href="magnet:?xt=urn:btih:6026026026026026026026026026026026026026"></a></td>
          <td>10 MB</td>
          <td data-timestamp="1700000000">Old</td>
          <td>5</td><td>1</td>
        </tr>
        <tr>
          <td></td>
          <td><a href="/view/603">Older Release Vol. 3</a></td>
          <td><a href="magnet:?xt=urn:btih:6036036036036036036036036036036036036036"></a></td>
          <td>10 MB</td>
          <td data-timestamp="1690000000">Older</td>
          <td>5</td><td>1</td>
        </tr>
      </tbody>
    </table>
    """
    cutoff = datetime.fromtimestamp(1720000000, tz=timezone.utc)
    with patch("app.providers.nyaa.safe_http_get", return_value=MockHttpResponse(date_cutoff_html)):
        res_cutoff = provider.crawl_pages(max_pages=1, date_cutoff=cutoff)
        r.check(len(res_cutoff) == 1,
                "Date cutoff halts crawling immediately when encounter item older than cutoff",
                f"Expected 1 item, got {len(res_cutoff)}")

    # 1.2 PDF Rejection on Complex Titles
    print("\n--- 1.2 PDF Rejection on Complex Titles ---")

    # Case A: Explicit bracketed [PDF] tag with epub extension
    t_pdf_bracket = "[Publisher] Title [PDF].epub"
    r.check(is_pdf_release(t_pdf_bracket) is True,
            "Rejects '[Publisher] Title [PDF].epub' (explicit [PDF] tag)")

    # Case B: Standard .pdf extension
    t_pdf_ext = "Title.pdf"
    r.check(is_pdf_release(t_pdf_ext) is True,
            "Rejects 'Title.pdf' (file extension .pdf)")

    # Case C: Standalone word 'PDF' in title: 'PDF Expert Guide.epub'
    t_pdf_word = "PDF Expert Guide.epub"
    pdf_word_res = is_pdf_release(t_pdf_word)
    r.check(pdf_word_res is True,
            "is_pdf_release flags 'PDF Expert Guide.epub' due to '\\bpdf\\b' word boundary rule",
            f"Result: {pdf_word_res}")

    # Case D: Mixed PDF+EPUB bundle release
    t_pdf_mixed = "[Publisher] Overlord Vol 16 [PDF+EPUB]"
    r.check(is_pdf_release(t_pdf_mixed) is True,
            "Rejects '[Publisher] Overlord Vol 16 [PDF+EPUB]' containing PDF")

    # Case E: Legitimate EPUB title with words containing 'pdf' as substring
    t_epub_clean = "[Yen Press] The Spider and the Butterfly Vol. 1 [EPUB]"
    r.check(is_pdf_release(t_epub_clean) is False,
            "Accepts clean EPUB release '[Yen Press] The Spider and the Butterfly Vol. 1 [EPUB]'")

    # Case F: URL with /pdf/ path component
    r.check(is_pdf_release("Clean Title", link="https://tracker.org/torrents/pdf/view/123") is True,
            "Rejects release when link contains '/pdf/' path component")

    # 1.3 Magnet URI extraction with diverse query parameters and info-hash casing
    print("\n--- 1.3 Magnet URI Extraction & Casing Invariants ---")

    mock_magnet_html = """
    <table class="torrent-list">
      <tbody>
        <tr>
          <td></td>
          <td><a href="/view/701">Upper Case Hash Vol. 1</a></td>
          <td>
            <a href="magnet:?xt=urn:btih:6506A47A152E85AAECA04865557873AF38F895F3&dn=Upper+Case+Hash+Vol.+1"></a>
          </td>
          <td>20 MB</td><td>2026-10-27</td><td>10</td><td>0</td>
        </tr>
        <tr>
          <td></td>
          <td><a href="/view/702">Lower Case Hash Vol. 2</a></td>
          <td>
            <a href="magnet:?xt=urn:btih:6506a47a152e85aeca04865557873af38f895f3&dn=Lower+Case+Hash+Vol.+2"></a>
          </td>
          <td>20 MB</td><td>2026-10-27</td><td>10</td><td>0</td>
        </tr>
        <tr>
          <td></td>
          <td><a href="/view/703">Reversed Query Params Vol. 3</a></td>
          <td>
            <a href="magnet:?dn=Reversed+Params&tr=udp%3A%2F%2Ftracker.example.com%3A6969&xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD&tr=http%3A%2F%2Ftracker2.org"></a>
          </td>
          <td>20 MB</td><td>2026-10-27</td><td>10</td><td>0</td>
        </tr>
      </tbody>
    </table>
    """
    with patch("app.providers.nyaa.safe_http_get", return_value=MockHttpResponse(mock_magnet_html)):
        res_mag = provider.crawl_pages(max_pages=1)
        r.check(len(res_mag) == 3, "Extracted all 3 magnet rows with different parameter ordering")
        if len(res_mag) >= 3:
            r.check("6506A47A152E85AAECA04865557873AF38F895F3" in res_mag[0].download_url,
                    "Preserves uppercase info-hash in magnet download_url")
            r.check("6506a47a152e85aeca04865557873af38f895f3" in res_mag[1].download_url,
                    "Preserves lowercase info-hash in magnet download_url")
            r.check("AABBCCDDEEFF00112233445566778899AABBCCDD" in res_mag[2].download_url,
                    "Extracts info-hash when xt param is not the first parameter")

    # RSS feed nyaa_infohash synthesized magnet test
    mock_rss_infohash = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa">
      <channel>
        <title>Nyaa RSS</title>
        <item>
          <title>[Yen Press] Special Title &amp; Symbols Vol. 1</title>
          <link>https://nyaa.si/view/801</link>
          <guid>https://nyaa.si/view/801</guid>
          <nyaa:infoHash>FEDCBA9876543210FEDCBA9876543210FEDCBA98</nyaa:infoHash>
        </item>
      </channel>
    </rss>
    """
    with patch("app.providers.nyaa.safe_http_get", return_value=MockHttpResponse(mock_rss_infohash)):
        res_rss = provider._fetch_rss("https://nyaa.si/?page=rss&c=3_1")
        r.check(len(res_rss) == 1, "RSS parsed item with nyaa:infoHash tag")
        if res_rss:
            dl = res_rss[0].download_url
            r.check(dl.startswith("magnet:?xt=urn:btih:FEDCBA9876543210FEDCBA9876543210FEDCBA98"),
                    "Synthesized magnet URL uses infoHash attribute")
            r.check("dn=" in dl, "Synthesized magnet URL includes encoded dn parameter")


# =============================================================================
# PART 2: Release Group Extraction Matrix & Buzzword Rejection
# =============================================================================
def run_part_2_release_group_matrix(r: ChallengerRecorder):
    print("\n" + "=" * 75)
    print("  PART 2: RELEASE GROUP EXTRACTION MATRIX & BUZZWORD REJECTION")
    print("=" * 75)

    # 2.1 Known Publishers
    print("\n--- 2.1 Publisher Tag Extraction ---")
    publishers_matrix = [
        ("[Yen Press] Overlord Vol. 16", "Yen Press"),
        ("[J-Novel Club] By the Grace of the Gods Vol. 1", "J-Novel Club"),
        ("[Seven Seas] Mushoku Tensei Vol. 1", "Seven Seas"),
        ("[One Peace Books] The Rising of the Shield Hero Vol. 1", "One Peace Books"),
        ("[Square Enix Manga] The Apothecary Diaries Vol. 1", "Square Enix Manga"),
        ("[Square Enix Manga & Books] Otherside Picnic Vol. 1", "Square Enix Manga & Books"),
        ("[Airship] Classroom of the Elite Vol. 1", "Airship"),
        ("[Kodansha] Monogatari Series Vol. 1", "Kodansha"),
        ("[Viz Media] Bleach Can't Fear Your Own World Vol. 1", "Viz Media")
    ]
    for raw_title, expected_pub in publishers_matrix:
        extracted = extract_release_group(raw_title)
        r.check(extracted == expected_pub,
                f"Extract publisher '{expected_pub}' from '{raw_title}'",
                f"Got '{extracted}'")

    # 2.2 Known Rip / Scan Groups
    print("\n--- 2.2 Rip / Scan Group Extraction ---")
    rippers_matrix = [
        ("[LuTX] DanMachi Vol. 19", "LuTX"),
        ("[danke-Empire] Bleach Vol. 1", "danke-Empire"),
        ("[Anonymous] Re:Zero Vol. 25", "Anonymous"),
        ("[Stick] Mushoku Tensei Vol. 1", "Stick"),
        ("[NaBi] Sword Art Online Vol. 27", "NaBi"),
        ("[Kobo] Ascendance of a Bookworm Vol. 1", "Kobo"),
        ("Overlord Vol. 16-Stick (Digital)", "Stick")
    ]
    for raw_title, expected_group in rippers_matrix:
        extracted = extract_release_group(raw_title)
        r.check(extracted == expected_group,
                f"Extract ripper group '{expected_group}' from '{raw_title}'",
                f"Got '{extracted}'")

    # 2.3 Publisher vs Ripper Group Prioritization
    print("\n--- 2.3 Publisher vs Ripper Group Prioritization ---")
    combined_titles = [
        ("[Seven Seas] [Stick] Mushoku Tensei Vol. 1", "Stick"),
        ("[Stick] [Seven Seas] Mushoku Tensei Vol. 1", "Stick"),
        ("[Yen Press] [danke-Empire] Overlord Vol. 16", "danke-Empire"),
        ("[One Peace Books] [LuTX] Shield Hero Vol. 1", "LuTX")
    ]
    for raw_title, expected_prio in combined_titles:
        extracted = extract_release_group(raw_title)
        r.check(extracted == expected_prio,
                f"Ripper '{expected_prio}' prioritized over publisher in '{raw_title}'",
                f"Got '{extracted}'")

    # 2.4 False Positives to Reject (Buzzwords & Technical Tags)
    print("\n--- 2.4 False Positive Technical Tag Rejection ---")
    false_positives = [
        ("Title [Batch]", None),
        ("Title [Complete]", None),
        ("Title [Digital]", None),
        ("Title [EPUB]", None),
        ("Title [v01-10]", None),
        ("Title [1080p]", None),
        ("Title [Multi-Audio]", None)  # Adversarial probe
    ]
    for raw_title, expected_none in false_positives:
        extracted = extract_release_group(raw_title)
        r.check(extracted is expected_none,
                f"Reject false-positive buzzword tag in '{raw_title}' (expected None)",
                f"Extracted unexpected candidate: '{extracted}'")


# =============================================================================
# PART 3: Prioritized Groups CRUD & Series Boosting
# =============================================================================
def run_part_3_prioritized_groups(r: ChallengerRecorder):
    print("\n" + "=" * 75)
    print("  PART 3: PRIORITIZED GROUPS CRUD & SERIES BOOSTING")
    print("=" * 75)

    tmp_dir = tempfile.mkdtemp(prefix="noveltrax_challenger_m4_p3_")
    db_path = os.path.join(tmp_dir, "challenger_p3.db")
    try:
        db = Database(db_path)

        # 3.1 Idempotent Table Initialization & Seeding
        print("\n--- 3.1 Idempotent Table Creation & Seed Defaults ---")
        db.ensure_prioritized_groups_table()
        db.ensure_prioritized_groups_table()
        db.ensure_prioritized_groups_table()

        seeded = db.get_prioritized_groups()
        r.check(len(seeded) == 5,
                f"ensure_prioritized_groups_table idempotent with exactly 5 defaults (found {len(seeded)})",
                f"Found: {[g['name'] for g in seeded]}")

        seeded_names = {g["name"] for g in seeded}
        expected_seeds = {"Yen Press", "J-Novel Club", "Seven Seas", "Stick", "One Peace Books"}
        r.check(seeded_names == expected_seeds,
                "Seeded groups match expected default publisher set",
                f"Difference: {seeded_names ^ expected_seeds}")

        # 3.2 Duplicate Additions & Case Sensitivity / Collation Probe
        print("\n--- 3.2 Duplicate Additions & Case Sensitivity Collation ---")

        # A. Exact duplicate name update
        added_exact = db.add_prioritized_group("Yen Press", notes="Updated VIP note", priority=5)
        r.check(added_exact is True, "Updating existing group 'Yen Press' returned True")
        groups_after_exact = db.get_prioritized_groups()
        r.check(len(groups_after_exact) == 5,
                "Exact duplicate 'Yen Press' does not create duplicate row",
                f"Count: {len(groups_after_exact)}")
        yp = next(g for g in groups_after_exact if g["name"] == "Yen Press")
        r.check(yp["priority"] == 5 and yp["notes"] == "Updated VIP note",
                "Existing group updated priority and notes on conflict")

        # B. Case-variant addition probe (testing SQLite column collation)
        # Testing if 'yen press' (lowercase) is rejected/merged or creates duplicate
        db.add_prioritized_group("yen press", notes="lowercase duplicate attempt")
        groups_after_case = db.get_prioritized_groups()
        has_duplicate_case = len(groups_after_case) > 5
        r.check(not has_duplicate_case,
                "Case-insensitive deduplication in add_prioritized_group prevents duplicate 'yen press'",
                f"Table count increased to {len(groups_after_case)}: {[g['name'] for g in groups_after_case]}")

        # Clean up duplicate if created to isolate subsequent tests
        for g in groups_after_case:
            if g["name"] == "yen press":
                db.remove_prioritized_group(g["id"])

        # C. Whitespace trimming & empty name rejection
        r.check(db.add_prioritized_group("   ") is False, "Empty/whitespace name rejected")
        db.add_prioritized_group("  LuTX  ", notes="Rip Group", priority=3)
        lutx_entry = next((g for g in db.get_prioritized_groups() if "LuTX" in g["name"]), None)
        r.check(lutx_entry is not None and lutx_entry["name"] == "LuTX",
                "Group name leading/trailing whitespace trimmed properly")

        # 3.3 Deletion Verification
        print("\n--- 3.3 Deletion Operations ---")
        if lutx_entry:
            deleted = db.remove_prioritized_group(lutx_entry["id"])
            r.check(deleted is True, "remove_prioritized_group returned True for valid ID")
            r.check(db.is_group_prioritized("LuTX") is False, "LuTX is no longer prioritized after deletion")

        deleted_fake = db.remove_prioritized_group(99999)
        r.check(deleted_fake is False, "remove_prioritized_group returned False for non-existent ID")

        # 3.4 Series Grouping Ranking Boost & Alphabetical Tie-Breaking
        print("\n--- 3.4 get_releases_grouped_by_series Ranking Boost ---")
        # Setup:
        # 1. "Beta Series" -> REQUESTED, release_group="Yen Press" (Wanted + Prioritized) -> priority 1
        # 2. "Alpha Series" -> REQUESTED, release_group="NormalGroup" (Wanted normal) -> priority 2
        # 3. "Gamma Series" -> REQUESTED, release_group="NormalGroup" (Wanted normal) -> priority 2
        # 4. "Delta Series" -> NEW, release_group="Yen Press" (Discovery + Prioritized) -> priority 3
        # 5. "Epsilon Series" -> IN_CALIBRE, release_group="NormalGroup" (In Calibre) -> priority 6

        db.upsert_release({
            "guid": "b-1",
            "title": "[Yen Press] Beta Series Vol. 1",
            "series_name": "Beta Series",
            "volume": 1.0,
            "status": "REQUESTED"
        })
        db.upsert_release({
            "guid": "a-1",
            "title": "[Other] Alpha Series Vol. 1",
            "series_name": "Alpha Series",
            "volume": 1.0,
            "status": "REQUESTED"
        })
        db.upsert_release({
            "guid": "g-1",
            "title": "[Other] Gamma Series Vol. 1",
            "series_name": "Gamma Series",
            "volume": 1.0,
            "status": "REQUESTED"
        })
        db.upsert_release({
            "guid": "d-1",
            "title": "[Yen Press] Delta Series Vol. 1",
            "series_name": "Delta Series",
            "volume": 1.0,
            "status": "NEW"
        })
        db.upsert_release({
            "guid": "e-1",
            "title": "[Other] Epsilon Series Vol. 1",
            "series_name": "Epsilon Series",
            "volume": 1.0,
            "status": "IN_CALIBRE"
        })

        # Test A: Explicit sort="wanted_first" (Milestone 4 ranking mode)
        grouped_wanted = db.get_releases_grouped_by_series(sort="wanted_first")
        wanted_names = [g["series_name"] for g in grouped_wanted]

        r.check(wanted_names[0] == "Beta Series",
                "sort='wanted_first' ranks series with wanted releases from prioritized group ('Beta Series') at #1",
                f"Actual order: {wanted_names}")

        r.check(wanted_names[1] == "Alpha Series" and wanted_names[2] == "Gamma Series",
                "sort='wanted_first' sorts un-prioritized wanted series alphabetically ('Alpha Series', 'Gamma Series')",
                f"Actual positions: {wanted_names[1:3]}")

        # Test B: Legacy / priority sort mode
        grouped_prio = db.get_releases_grouped_by_series(sort="priority")
        prio_names = [g["series_name"] for g in grouped_prio]
        r.check(prio_names[0] == "Beta Series",
                "sort='priority' ranks wanted prioritized series ('Beta Series') at #1",
                f"Actual order: {prio_names}")
        r.check(prio_names.index("Delta Series") > prio_names.index("Gamma Series"),
                "sort='priority' ranks discovery series ('Delta Series') after all wanted series",
                f"Delta index: {prio_names.index('Delta Series')}")

        # Test C: Default parameter sort behavior inspection
        grouped_default = db.get_releases_grouped_by_series()
        default_names = [g["series_name"] for g in grouped_default]
        default_boosted = (default_names[0] == "Beta Series")
        r.check(default_boosted,
                "Default get_releases_grouped_by_series() applies priority boost ranking (Beta Series #1)",
                f"Default sort parameter defaults to date_desc, producing: {default_names}")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# =============================================================================
# PART 4: 1-Click Ignore / Unignore State Machine
# =============================================================================
def run_part_4_ignore_state_machine(r: ChallengerRecorder):
    print("\n" + "=" * 75)
    print("  PART 4: 1-CLICK IGNORE / UNIGNORE STATE MACHINE")
    print("=" * 75)

    tmp_dir = tempfile.mkdtemp(prefix="noveltrax_challenger_m4_p4_")
    db_path = os.path.join(tmp_dir, "challenger_p4.db")
    try:
        db = Database(db_path)

        # 4.1 Basic State Transition: REQUESTED -> IGNORED
        print("\n--- 4.1 State Transition: REQUESTED -> IGNORED ---")
        _, rel_id = db.upsert_release({
            "guid": "ign-m4-1",
            "title": "[Yen Press] Unwanted Spinoff Vol. 1",
            "series_name": "Unwanted Spinoff",
            "volume": 1.0,
            "status": "REQUESTED"
        })

        r_init = db.get_release_by_id(rel_id)
        r.check(r_init["status"] == "REQUESTED" and (r_init["ignored"] == 0 or r_init["ignored"] is None),
                "Initial release is REQUESTED with ignored=0")

        # Perform ignore
        ign_result = db.ignore_release(rel_id)
        r.check(ign_result is True, "ignore_release returned True")

        r_ignored = db.get_release_by_id(rel_id)
        r.check(r_ignored["status"] == "IGNORED" and r_ignored["ignored"] == 1,
                "Release transitioned to status='IGNORED' and ignored=1")

        # 4.2 Query Filter Exclusions & Inclusions
        print("\n--- 4.2 Query Filter Exclusions & Inclusions ---")

        # Default get_releases excludes ignored
        active_rels = db.get_releases()
        r.check(rel_id not in [x["id"] for x in active_rels],
                "Default get_releases() excludes IGNORED release")

        # Default get_releases_grouped_by_series excludes series
        active_series = db.get_releases_grouped_by_series()
        r.check("Unwanted Spinoff" not in [g["series_name"] for g in active_series],
                "Default get_releases_grouped_by_series() excludes IGNORED series")

        # Default get_prioritized_releases excludes ignored
        prio_rels = db.get_prioritized_releases()
        r.check(rel_id not in [x["id"] for x in prio_rels],
                "Default get_prioritized_releases() excludes IGNORED release")

        # include_ignored=True includes release in all views
        all_rels = db.get_releases(include_ignored=True)
        r.check(rel_id in [x["id"] for x in all_rels],
                "get_releases(include_ignored=True) includes IGNORED release")

        all_series = db.get_releases_grouped_by_series(include_ignored=True)
        r.check("Unwanted Spinoff" in [g["series_name"] for g in all_series],
                "get_releases_grouped_by_series(include_ignored=True) includes IGNORED series")

        # status='IGNORED' filter returns it specifically
        only_ign = db.get_releases(status="IGNORED")
        r.check(rel_id in [x["id"] for x in only_ign],
                "get_releases(status='IGNORED') returns the ignored release")

        # 4.3 State Transition: IGNORED -> REQUESTED (Unignore)
        print("\n--- 4.3 State Transition: IGNORED -> REQUESTED (Unignore) ---")
        unign_result = db.unignore_release(rel_id)
        r.check(unign_result is True, "unignore_release returned True")

        r_restored = db.get_release_by_id(rel_id)
        r.check(r_restored["status"] == "REQUESTED" and r_restored["ignored"] == 0,
                "Release restored to status='REQUESTED' and ignored=0")

        # Reappears in default views
        restored_active = db.get_releases()
        r.check(rel_id in [x["id"] for x in restored_active],
                "Restored release immediately visible in default get_releases()")

        restored_groups = db.get_releases_grouped_by_series()
        r.check("Unwanted Spinoff" in [g["series_name"] for g in restored_groups],
                "Restored series immediately visible in default get_releases_grouped_by_series()")

        # 4.4 Boundary & Edge Case Stress Testing
        print("\n--- 4.4 Edge Cases & State Invariants ---")

        # A. Idempotent ignore
        db.ignore_release(rel_id)
        db.ignore_release(rel_id)
        r.check(db.get_release_by_id(rel_id)["status"] == "IGNORED",
                "Repeated ignore_release calls remain stable at status='IGNORED'")

        # B. Non-existent ID handling
        r.check(db.ignore_release(999999) is False, "ignore_release on invalid ID returns False")
        r.check(db.unignore_release(999999) is False, "unignore_release on invalid ID returns False")

        # C. Re-upserting an ignored release does not un-ignore it
        db.upsert_release({
            "guid": "ign-m4-1",
            "title": "[Yen Press] Unwanted Spinoff Vol. 1",
            "series_name": "Unwanted Spinoff",
            "volume": 1.0,
            "status": "REQUESTED"
        })
        r_after_reupsert = db.get_release_by_id(rel_id)
        r.check(r_after_reupsert["status"] == "IGNORED" and r_after_reupsert["ignored"] == 1,
                "Periodic feed re-upsert of existing release preserves IGNORED state")

        # D. State Machine Subtlety Probe: Transition to IN_CALIBRE on acquired book
        # When an ignored release is acquired in Calibre, does update_release_status reset ignored=0?
        db.update_release_status(rel_id, "IN_CALIBRE", matched_book_id=888)
        r_calibre = db.get_release_by_id(rel_id)
        in_calibre_rels = db.get_releases(status="IN_CALIBRE")
        is_visible_in_calibre = rel_id in [x["id"] for x in in_calibre_rels]
        r.check(is_visible_in_calibre,
                "Release transitioned to IN_CALIBRE resets ignored flag and is visible in get_releases(status='IN_CALIBRE')",
                f"Release status={r_calibre['status']}, ignored={r_calibre.get('ignored')}; visible: {is_visible_in_calibre}")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# =============================================================================
# Main Harness Runner
# =============================================================================
def main():
    print("\n" + "=" * 75)
    print("  NOVELTRAX MILESTONE 4 EMPIRICAL CHALLENGER HARNESS")
    print("=" * 75)

    r = ChallengerRecorder()

    run_part_1_nyaa_fuzz(r)
    run_part_2_release_group_matrix(r)
    run_part_3_prioritized_groups(r)
    run_part_4_ignore_state_machine(r)

    print("\n" + "=" * 75)
    print(f"  CHALLENGER SUITE SUMMARY: {r.passed} PASSED | {r.failed} FAILED | {r.passed + r.failed} TOTAL")
    print("=" * 75)

    if r.failed > 0:
        print("\nREPRODUCIBLE EMPIRICAL DEFECTS & FINDINGS:")
        for idx, f in enumerate(r.failures, 1):
            print(f"  {idx}. {f['test']}")
            print(f"     Details: {f['details']}")
        print("\nVerdict: REQUEST_CHANGES")
        sys.exit(1)
    else:
        print("\nVerdict: APPROVE")
        sys.exit(0)


if __name__ == "__main__":
    main()
