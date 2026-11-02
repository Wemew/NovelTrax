"""
tests/test_challenger_m4_it2_stress.py
=============================================================================
NovelTrax Milestone 4 Iteration 2 - Empirical Challenger Stress Harness
=============================================================================
Adversarial white-box stress testing for:
1. Release group buzzwords rejection vs genuine group preservation.
2. PDF rejection boundary conditions across raw titles, links, GUIDs, and combined texts.
3. Case-insensitive deduplication and collation in `prioritized_groups` table.
4. Default sort ranking in `db.get_releases_grouped_by_series()` (wanted VIP #1).
5. State machine invariants for `ignored` flag transitions.
6. Vulnerability probe: Ingestion title mangling of `[J-Novel Club]` and hyphenated buzzwords.
"""

import sys
import os
import shutil
import tempfile
import sqlite3
from typing import List, Dict, Any

# Add backend directory to sys.path
backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

from app.parser import (
    extract_release_group,
    is_valid_group_candidate,
    is_pdf_release,
    BUZZWORDS,
    KNOWN_PUBLISHERS,
    DEFAULT_KNOWN_GROUPS
)
from app.database import Database
from app.providers.metadata import NovelMetadataProvider

class TestRunner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.total = 0

    def check(self, condition: bool, description: str, details: str = ""):
        self.total += 1
        if condition:
            self.passed += 1
            print(f"  [PASS] {description}")
        else:
            self.failed += 1
            print(f"  [FAIL] {description}")
            if details:
                print(f"         Details: {details}")

    def summarize(self, suite_name: str) -> bool:
        print("\n" + "=" * 76)
        print(f"  {suite_name} SUMMARY: {self.passed} PASSED | {self.failed} FAILED | {self.total} TOTAL")
        print("=" * 76)
        return self.failed == 0


def test_buzzwords_and_groups(r: TestRunner):
    print("\n" + "=" * 76)
    print("  SUITE 1: ADVERSARIAL BUZZWORD REJECTION & GROUP PRESERVATION")
    print("=" * 76)

    # 1.1 Technical & Composite Tags Rejection (Dispatched requirements)
    dispatched_composite_tags = [
        "FLAC / 24bit",
        "1080p | Dual-Audio",
        "Multi-Audio",
        "Dual-Audio / 10bit / HEVC",
        "BD 1080p AVC AAC",
        "Web-DL",
        "WebDL",
        "Multi-Subs | x265 | 10bit",
        "MP3 / 320k",
        "Batch / Complete",
        "Digital / Retail / EPUB",
        "Bonus + Artbook",
        "v01-15",
        "2021-2024",
        "PDF + EPUB",
        "Audiobook",
        "Lossless / FLAC",
        "TrueHD / Atmos",
        "720p / AAC / x264",
        "Uncensored / Remastered",
        "Dual-Audio / AAC",
        "Remux / 2160p / HDR",
        "English / Digital / Retail"
    ]

    for tag in dispatched_composite_tags:
        is_cand = is_valid_group_candidate(tag)
        r.check(not is_cand, f"Reject composite/technical tag: '[{tag}]'", f"Returned True for {tag}")
        title = f"[{tag}] Some Light Novel Vol. 1"
        extracted = extract_release_group(title)
        r.check(extracted is None, f"extract_release_group on '[{tag}]' returns None", f"Extracted: '{extracted}'")

    # 1.2 Genuine Groups & Publishers Preservation
    genuine_groups = [
        ("Yen Press", "[Yen Press] Overlord Vol. 16"),
        ("Stick", "[Stick] Mushoku Tensei Vol. 20"),
        ("LuTX", "[LuTX] DanMachi Vol. 19"),
        ("danke-Empire", "[danke-Empire] Bleach LN Vol. 1"),
        ("J-Novel Club", "[J-Novel Club] Ascendance of a Bookworm Vol. 10"),
        ("Seven Seas", "[Seven Seas] Classroom of the Elite Vol. 8"),
        ("One Peace Books", "[One Peace Books] Shield Hero Vol. 22"),
        ("Airship", "[Airship] Reincarnated as a Sword Vol. 12"),
        ("Square Enix Manga & Books", "[Square Enix Manga & Books] Otherside Picnic Vol. 1"),
        ("NaBi", "[NaBi] Sword Art Online Vol. 27"),
        ("a-sloth", "[a-sloth] High School DxD Vol. 1"),
        ("1R0N", "[1R0N] Date A Live Vol. 1"),
        ("Kobo", "[Kobo] Tearmoon Empire Vol. 4"),
        ("BookWalker", "[BookWalker] Konosuba Vol. 17"),
        ("TenmaLand", "[TenmaLand] Goblin Slayer Vol. 15")
    ]

    for expected, title in genuine_groups:
        extracted = extract_release_group(title)
        r.check(extracted == expected, f"Preserve genuine group: '{expected}'", f"Expected '{expected}', got '{extracted}'")

    # 1.3 Mixed Titles with Composite Buzzwords & Genuine Groups
    mixed_cases = [
        ("[Yen Press] Overlord Vol. 16 [1080p | Dual-Audio] [EPUB]", "Yen Press"),
        ("[Stick] [1080p] Mushoku Tensei Vol 20 [FLAC / 24bit]", "Stick"),
        ("[Seven Seas] [danke-Empire] Bleach LN Vol. 1 [Digital]", "danke-Empire"),
        ("[Multi-Audio] [FLAC / 24bit] Random Title Vol 1", None),
        ("Title Vol 1 [Bonus + Artbook] [J-Novel Club]", "J-Novel Club"),
        ("[LuTX] DanMachi Vol 19 [Dual-Audio / 10bit / HEVC] [Digital]", "LuTX")
    ]

    for title, expected in mixed_cases:
        extracted = extract_release_group(title)
        r.check(extracted == expected, f"Extract from mixed title: '{title}' -> '{expected}'", f"Expected '{expected}', got '{extracted}'")


def test_pdf_rejection_matrix(r: TestRunner):
    print("\n" + "=" * 76)
    print("  SUITE 2: ADVERSARIAL PDF REJECTION MATRIX")
    print("=" * 76)

    # 2.1 Positives: Must be detected as PDF
    pdf_positives = [
        # (title, link, guid, desc)
        ("Overlord Vol 16.pdf", "https://nyaa.si/view/123", "nyaa:123", "Filename ending with .pdf in title"),
        ("[Yen Press] Overlord Vol 16 [PDF]", "https://nyaa.si/view/123", "nyaa:123", "Bracketed [PDF] tag in title"),
        ("[Yen Press] Overlord Vol 16 (PDF)", "https://nyaa.si/view/123", "nyaa:123", "Parenthesized (PDF) tag in title"),
        ("Overlord Vol 16 [PDF+EPUB]", "https://nyaa.si/view/123", "nyaa:123", "[PDF+EPUB] compound in title"),
        ("Overlord Vol 16 EPUB", "https://nyaa.si/download/123.pdf", "nyaa:123", "Bare .pdf at end of link URL"),
        ("Overlord Vol 16 EPUB", "https://nyaa.si/download/123.pdf?torrent=1", "nyaa:123", ".pdf before query parameter in link URL"),
        ("Overlord Vol 16 EPUB", "https://nyaa.si/download/123.pdf#fragment", "nyaa:123", ".pdf before hash fragment in link URL"),
        ("Overlord Vol 16 EPUB", "https://nyaa.si/download/123.pdf/download", "nyaa:123", ".pdf followed by slash in link URL"),
        ("Overlord Vol 16 EPUB", "https://example.com/pdf/overlord_16.epub", "nyaa:123", "/pdf/ path component in link URL"),
        ("Overlord Vol 16 EPUB", "https://example.com/download?format=pdf&id=1", "nyaa:123", "format=pdf query in link URL"),
        ("Overlord Vol 16 EPUB", "https://example.com/download?ext=pdf&id=1", "nyaa:123", "ext=pdf query in link URL"),
        ("Overlord Vol 16 EPUB", "https://example.com/download?filetype=pdf", "nyaa:123", "filetype=pdf query in link URL"),
        ("Overlord Vol 16 EPUB", "https://nyaa.si/view/123", "urn:nyaa:release:123.pdf", "Bare .pdf in GUID"),
        ("Overlord Vol 16 EPUB", "https://nyaa.si/view/123", "urn:nyaa:release:123.pdf?dl=1", ".pdf before query in GUID"),
    ]

    for title, link, guid, desc in pdf_positives:
        is_pdf = is_pdf_release(raw_title=title, link=link, guid=guid)
        r.check(is_pdf, f"PDF detected: {desc}", f"Expected True for title='{title}', link='{link}', guid='{guid}'")

    # 2.2 Negatives: Legitimate EPUBs that must NOT be flagged as PDF
    pdf_negatives = [
        ("[Yen Press] Overlord Vol 16 [EPUB]", "https://nyaa.si/view/123", "nyaa:123", "Clean EPUB title and standard URLs"),
        ("[Stick] Mushoku Tensei Vol 20 [Digital]", "https://nyaa.si/download/123.torrent", "nyaa:123", "Standard torrent download link"),
        ("The PDFun Book of Magic Vol 1 [EPUB]", "https://nyaa.si/view/123", "nyaa:123", "Word containing 'pdf' as substring ('PDFun')"),
        ("Sword Art Online Vol 1 [EPUB]", "https://example.com/download.epub?filetype=epub", "nyaa:123", "Explicit non-pdf query param filetype=epub"),
        ("DanMachi Vol 19 [EPUB]", "magnet:?xt=urn:btih:ABCDEF&dn=DanMachi%20Vol%2019%20%5BEPUB%5D", "nyaa:123", "Clean magnet download URL"),
    ]

    for title, link, guid, desc in pdf_negatives:
        is_pdf = is_pdf_release(raw_title=title, link=link, guid=guid)
        r.check(not is_pdf, f"EPUB preserved (not PDF): {desc}", f"Expected False for title='{title}', link='{link}', guid='{guid}'")


def test_case_insensitive_prioritized_groups(r: TestRunner, db_path: str):
    print("\n" + "=" * 76)
    print("  SUITE 3: CASE-INSENSITIVE DEDUPLICATION & COLLATION IN GROUPS")
    print("=" * 76)

    db = Database(db_path)
    db.ensure_prioritized_groups_table()

    # Verify initial seeded defaults (5 groups)
    initial_groups = db.get_prioritized_groups()
    r.check(len(initial_groups) == 5, f"Initial seeded groups count is 5 (got {len(initial_groups)})")
    names = {g["name"] for g in initial_groups}
    r.check("Yen Press" in names, "Yen Press present in initial groups")
    r.check("Stick" in names, "Stick present in initial groups")

    # Case variations of 'Yen Press'
    casing_variants = [
        "yen press",
        "YEN PRESS",
        "Yen Press",
        "yEn PrEsS",
        "  yen press  "  # leading/trailing spaces
    ]

    for variant in casing_variants:
        res = db.add_prioritized_group(variant, notes="VIP Publisher", priority=3)
        r.check(res, f"add_prioritized_group('{variant}') returns True")
        groups = db.get_prioritized_groups()
        r.check(len(groups) == 5, f"Group count remains 5 after adding '{variant}'", f"Found {len(groups)} groups: {[g['name'] for g in groups]}")

    # Check updated fields
    groups = db.get_prioritized_groups()
    yp = next((g for g in groups if g["name"].lower() == "yen press"), None)
    r.check(yp is not None, "Yen Press group found")
    r.check(yp["priority"] == 3, f"Yen Press priority updated to 3 (got {yp['priority']})")
    r.check(yp["notes"] == "VIP Publisher", f"Yen Press notes updated (got '{yp['notes']}')")

    # Add a brand new group in lowercase
    res_new = db.add_prioritized_group("lutx", notes="Ripper", priority=2)
    r.check(res_new, "Added new group 'lutx'")
    groups_after_new = db.get_prioritized_groups()
    r.check(len(groups_after_new) == 6, f"Group count is 6 after adding 'lutx' (got {len(groups_after_new)})")

    # Add uppercase of the same new group
    res_upper = db.add_prioritized_group("LUTX", notes="Ripper VIP", priority=4)
    r.check(res_upper, "Added uppercase 'LUTX'")
    groups_after_upper = db.get_prioritized_groups()
    r.check(len(groups_after_upper) == 6, f"Group count remains 6 after adding 'LUTX' (got {len(groups_after_upper)})")
    lutx = next((g for g in groups_after_upper if g["name"].lower() == "lutx"), None)
    r.check(lutx["priority"] == 4, f"LUTX priority updated to 4 (got {lutx['priority']})")

    # Remove group by ID
    del_res = db.remove_prioritized_group(lutx["id"])
    r.check(del_res, f"remove_prioritized_group({lutx['id']}) returned True")
    groups_after_del = db.get_prioritized_groups()
    r.check(len(groups_after_del) == 5, f"Group count back to 5 after removing LUTX (got {len(groups_after_del)})")
    r.check(not any(g["name"].lower() == "lutx" for g in groups_after_del), "LUTX is completely removed")


def test_default_sorting_boost(r: TestRunner, db_path: str):
    print("\n" + "=" * 76)
    print("  SUITE 4: DEFAULT SORTING BOOST IN GET_RELEASES_GROUPED_BY_SERIES")
    print("=" * 76)

    db = Database(db_path)
    db.ensure_prioritized_groups_table()

    # Clean previous records
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM releases;")
        cur.execute("DELETE FROM series_tracking;")
        conn.commit()

    # Releases to test:
    # 1. 'Beta Series'  - REQUESTED, group='Yen Press' (Wanted + VIP) -> Rank #1
    # 2. 'Zeta Series'  - REQUESTED, group='Seven Seas' (Wanted + VIP) -> Rank #2 (sorted alphabetically after Beta)
    # 3. 'Alpha Series' - REQUESTED, group='Other' (Wanted + Non-VIP) -> Rank #3
    # 4. 'Gamma Series' - REQUESTED, group='Other' (Wanted + Non-VIP) -> Rank #4
    # 5. 'Delta Series' - NEW, group='Yen Press' (Discovery + VIP)
    # 6. 'Epsilon Series' - IN_CALIBRE, group='Other' (In Calibre)

    releases = [
        {"guid": "beta-1", "title": "[Yen Press] Beta Series Vol. 1", "series_name": "Beta Series", "volume": 1.0, "status": "REQUESTED", "pub_date": "2026-11-01 10:00:00"},
        {"guid": "zeta-1", "title": "[Seven Seas] Zeta Series Vol. 1", "series_name": "Zeta Series", "volume": 1.0, "status": "REQUESTED", "pub_date": "2026-11-01 09:00:00"},
        {"guid": "alpha-1", "title": "[Other] Alpha Series Vol. 1", "series_name": "Alpha Series", "volume": 1.0, "status": "REQUESTED", "pub_date": "2026-11-01 11:00:00"},
        {"guid": "gamma-1", "title": "[Other] Gamma Series Vol. 1", "series_name": "Gamma Series", "volume": 1.0, "status": "REQUESTED", "pub_date": "2026-11-01 08:00:00"},
        {"guid": "delta-1", "title": "[Yen Press] Delta Series Vol. 1", "series_name": "Delta Series", "volume": 1.0, "status": "NEW", "pub_date": "2026-11-01 13:00:00"},
        {"guid": "epsilon-1", "title": "[Other] Epsilon Series Vol. 1", "series_name": "Epsilon Series", "volume": 1.0, "status": "IN_CALIBRE", "pub_date": "2026-11-01 14:00:00"},
    ]

    for rel in releases:
        db.upsert_release(rel)

    # Test 4.1: Default call without sort argument (should default to wanted_first)
    groups_default = db.get_releases_grouped_by_series()
    default_names = [g["series_name"] for g in groups_default]
    print(f"  [INFO] Default sort order: {default_names}")

    # Beta Series and Zeta Series have wanted releases from VIP groups (Yen Press and Seven Seas).
    # Alpha Series and Gamma Series have wanted releases from non-VIP group.
    # Therefore, Beta Series (#1) and Zeta Series (#2) MUST lead the list.
    r.check(default_names[0] == "Beta Series", "Default sort: Rank #1 is 'Beta Series' (Wanted + VIP group)")
    r.check(default_names[1] == "Zeta Series", "Default sort: Rank #2 is 'Zeta Series' (Wanted + VIP group)")
    r.check(default_names[2] == "Alpha Series", "Default sort: Rank #3 is 'Alpha Series' (Wanted + Non-VIP)")
    r.check(default_names[3] == "Gamma Series", "Default sort: Rank #4 is 'Gamma Series' (Wanted + Non-VIP)")

    # Test 4.2: Explicit sort="wanted_first" produces identical order
    groups_wanted = db.get_releases_grouped_by_series(sort="wanted_first")
    wanted_names = [g["series_name"] for g in groups_wanted]
    r.check(wanted_names == default_names, "sort='wanted_first' strictly matches default sort order")

    # Test 4.3: Explicit sort="date_desc" strictly orders by published date (Epsilon 14:00, Delta 13:00, Alpha 11:00)
    groups_date = db.get_releases_grouped_by_series(sort="date_desc")
    date_names = [g["series_name"] for g in groups_date]
    print(f"  [INFO] Date desc sort order: {date_names}")
    r.check(date_names[0] == "Epsilon Series", f"sort='date_desc': Rank #1 is 'Epsilon Series' (latest published, got {date_names[0]})")
    r.check(date_names[1] == "Delta Series", f"sort='date_desc': Rank #2 is 'Delta Series' (got {date_names[1]})")

    # Test 4.4: prioritized_only=True excludes Alpha Series and Gamma Series
    groups_prio = db.get_releases_grouped_by_series(prioritized_only=True)
    prio_names = [g["series_name"] for g in groups_prio]
    r.check("Alpha Series" not in prio_names, "prioritized_only=True excludes 'Alpha Series' (no VIP releases)")
    r.check("Gamma Series" not in prio_names, "prioritized_only=True excludes 'Gamma Series' (no VIP releases)")
    r.check("Beta Series" in prio_names, "prioritized_only=True includes 'Beta Series'")


def test_ignored_state_transitions(r: TestRunner, db_path: str):
    print("\n" + "=" * 76)
    print("  SUITE 5: IGNORED STATE INVARIANTS & TRANSITIONS")
    print("=" * 76)

    db = Database(db_path)
    is_new, rel_id = db.upsert_release({
        "guid": "transition-test-1",
        "series_name": "Transition Series",
        "title": "[Stick] Transition Series Vol 5",
        "volume": 5.0,
        "release_group": "Stick",
        "status": "REQUESTED",
        "link": "https://nyaa.si/view/999"
    })
    r.check(rel_id > 0, f"Inserted test release with ID {rel_id}")

    # 1. Ignore release
    db.ignore_release(rel_id)
    with db.get_connection() as conn:
        row = conn.execute("SELECT status, ignored FROM releases WHERE id = ?;", (rel_id,)).fetchone()
        r.check(row["status"] == "IGNORED", f"Status updated to IGNORED (got {row['status']})")
        r.check(row["ignored"] == 1, f"ignored column flag is 1 (got {row['ignored']})")

    # 2. Transition directly to IN_CALIBRE (e.g. user manually imported the book or calibre sync matches)
    db.update_release_status(rel_id, "IN_CALIBRE", matched_book_id=42)
    with db.get_connection() as conn:
        row = conn.execute("SELECT status, ignored, calibre_matched_book_id FROM releases WHERE id = ?;", (rel_id,)).fetchone()
        r.check(row["status"] == "IN_CALIBRE", f"Status transitioned to IN_CALIBRE (got {row['status']})")
        r.check(row["ignored"] == 0, f"ignored flag reset to 0 upon IN_CALIBRE transition (got {row['ignored']})")
        r.check(row["calibre_matched_book_id"] == 42, f"calibre_matched_book_id is 42 (got {row['calibre_matched_book_id']})")

    # Verify release is visible in get_releases(status="IN_CALIBRE")
    in_cal = db.get_releases(status="IN_CALIBRE")
    r.check(any(r_item["id"] == rel_id for r_item in in_cal), "Release visible in get_releases(status='IN_CALIBRE')")

    # Verify visible in default get_releases()
    all_active = db.get_releases()
    r.check(any(r_item["id"] == rel_id for r_item in all_active), "Release visible in default get_releases()")

    # 3. Transition to IGNORED again, then to REQUESTED (unignore)
    db.ignore_release(rel_id)
    with db.get_connection() as conn:
        row = conn.execute("SELECT status, ignored FROM releases WHERE id = ?;", (rel_id,)).fetchone()
        r.check(row["status"] == "IGNORED" and row["ignored"] == 1, "Re-ignored release has status=IGNORED, ignored=1")

    db.unignore_release(rel_id)
    with db.get_connection() as conn:
        row = conn.execute("SELECT status, ignored FROM releases WHERE id = ?;", (rel_id,)).fetchone()
        r.check(row["status"] == "REQUESTED" and row["ignored"] == 0, "Unignored release has status=REQUESTED, ignored=0")


def test_adversarial_vulnerabilities(r: TestRunner, db_path: str):
    print("\n" + "=" * 76)
    print("  SUITE 6: ADVERSARIAL VULNERABILITY PROBES (DOCUMENTED FINDINGS)")
    print("=" * 76)

    # Vulnerability 1: Ingestion title mangling of '[J-Novel Club]'
    raw_jnovel_title = "[J-Novel Club] Ascendance of a Bookworm Vol. 1"
    cleaned = NovelMetadataProvider.clean_novel_title(raw_jnovel_title)
    # We check whether the bug exists:
    jnovel_mangled = (cleaned == "J- Club] Ascendance of a Bookworm Vol. 1")
    r.check(not jnovel_mangled, 
            "clean_novel_title preserves '[J-Novel Club]' without mangling to 'J- Club]'",
            f"VULNERABILITY CONFIRMED: clean_novel_title mutated '{raw_jnovel_title}' to '{cleaned}'")

    # Ingestion via Database.upsert_release with unextracted release_group:
    db = Database(db_path)
    is_new, rel_id = db.upsert_release({
        "guid": "jnovel-probe-1",
        "title": "[J-Novel Club] By the Grace of the Gods Vol. 1",
        "series_name": "By the Grace of the Gods"
    })
    with db.get_connection() as conn:
        row = conn.execute("SELECT title, release_group FROM releases WHERE id = ?;", (rel_id,)).fetchone()
        stored_title = row["title"]
        stored_group = row["release_group"]
        jnovel_extracted = (stored_group == "J-Novel Club")
        r.check(jnovel_extracted,
                "upsert_release correctly extracts 'J-Novel Club' release group",
                f"VULNERABILITY CONFIRMED: Stored title='{stored_title}', Stored group='{stored_group}'")

    # Vulnerability 2: Composite tags with hyphenated buzzwords like [Blu-Ray / 1080p] or [Web-DL 2160p HDR]
    bluray_tag = "Blu-Ray / 1080p"
    bluray_rejected = not is_valid_group_candidate(bluray_tag)
    r.check(bluray_rejected,
            f"Reject composite tag '[{bluray_tag}]' containing hyphenated buzzwords",
            f"VULNERABILITY CONFIRMED: is_valid_group_candidate returned True for '{bluray_tag}' because hyphen split separated 'Blu' and 'Ray'")


def test_m4_it4_remediations(r: TestRunner):
    print("\n" + "=" * 76)
    print("  SUITE 7: MILESTONE 4 ITERATION 4 REMEDIATION INVARIANTS")
    print("=" * 76)

    # 7.1 Release Group 'GZ' Validation & Ripper Prioritization
    gz_valid = is_valid_group_candidate("GZ")
    r.check(gz_valid, "is_valid_group_candidate('GZ') returns True", "Returned False")

    gz_simple = extract_release_group("[GZ] Title Vol 1")
    r.check(gz_simple == "GZ", "extract_release_group('[GZ] Title Vol 1') returns 'GZ'", f"Got '{gz_simple}'")

    gz_mixed = extract_release_group("[GZ] Title Vol 1 [Yen Press]")
    r.check(gz_mixed == "GZ", "extract_release_group prioritizes ripper 'GZ' over publisher 'Yen Press'", f"Got '{gz_mixed}'")

    gz_rev = extract_release_group("[Yen Press] Title Vol 1 [GZ]")
    r.check(gz_rev == "GZ", "extract_release_group prioritizes ripper 'GZ' regardless of bracket order", f"Got '{gz_rev}'")

    gz_scene = extract_release_group("Title Vol 1-GZ (Digital)")
    r.check(gz_scene == "GZ", "extract_release_group parses trailing scene group '-GZ'", f"Got '{gz_scene}'")

    # 7.2 Dimensional Resolution Rejection Matrix
    resolutions = ["1920x1080", "1280x720", "3840x2160", "2560x1440", "848x480", "640x480", "1920X1080"]
    for res in resolutions:
        valid = is_valid_group_candidate(res)
        r.check(not valid, f"is_valid_group_candidate('{res}') returns False", f"Expected False for '{res}'")
        extracted = extract_release_group(f"[{res}] Some Novel Vol 1")
        r.check(extracted is None, f"extract_release_group on '[{res}]' returns None", f"Extracted '{extracted}'")

    res_with_pub = extract_release_group("[1920x1080] [Yen Press] Overlord Vol. 16")
    r.check(res_with_pub == "Yen Press", "Resolution filtered, genuine publisher 'Yen Press' extracted", f"Got '{res_with_pub}'")

    res_with_gz = extract_release_group("[GZ] Overlord Vol. 16 [1920x1080]")
    r.check(res_with_gz == "GZ", "Resolution filtered, genuine ripper 'GZ' extracted", f"Got '{res_with_gz}'")

    # 7.3 Pre-existing Database Migration Safety & Defect Repair
    temp_dir = tempfile.mkdtemp(prefix="noveltrax_test_m4_it4_")
    db_path = os.path.join(temp_dir, "test_migration.db")

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE releases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guid TEXT UNIQUE,
                title TEXT,
                series_name TEXT,
                volume REAL,
                release_group TEXT DEFAULT '',
                status TEXT DEFAULT 'REQUESTED',
                ignored INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        pre_records = [
            ("pre-1", "DanMachi Vol 19 [Stick]", "DanMachi", 19.0, ""),
            ("pre-2", "Overlord Vol. 16 [Digital] [EPUB]", "Overlord", 16.0, ""),
            ("pre-3", "Solo Leveling, Vol. 8 (Novel) [Yen On] [Retail]", "Solo Leveling", 8.0, ""),
            ("pre-4", "Ascendance of a Bookworm [LN] Vol 1", "Ascendance of a Bookworm", 1.0, ""),
            ("pre-5", "Mushoku Tensei Vol. 1", "Mushoku Tensei", 1.0, ""),
            ("clean-1", "[Seven Seas] Mushoku Tensei Vol. 1", "Mushoku Tensei", 1.0, "Seven Seas"),
            ("clean-2", "[Stick] DanMachi Vol 19 [Digital]", "DanMachi", 19.0, "Stick"),
            ("mangled-1", "J- Club] Ascendance of a Bookworm Vol. 1", "Ascendance of a Bookworm", 1.0, ""),
            ("mangled-2", "Yen Press] Overlord Vol. 16", "Overlord", 16.0, ""),
            ("corrupted-1", "[DanMachi Vol 19 [Stick]", "DanMachi", 19.0, "DanMachi Vol 19 [Stick"),
            ("corrupted-2", "[Overlord Vol. 16 [Digital] [EPUB]", "Overlord", 16.0, "Overlord Vol. 16 [Digital"),
        ]

        for guid, title, s_name, vol, group in pre_records:
            cur.execute(
                "INSERT INTO releases (guid, title, series_name, volume, release_group) VALUES (?, ?, ?, ?, ?)",
                (guid, title, s_name, vol, group)
            )
        conn.commit()
        conn.close()

        # Trigger Database.init_db() migrations
        db = Database(db_path)

        with db.get_connection() as c:
            # Check pre-existing records were NOT corrupted
            r1 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'pre-1'").fetchone()
            r.check(r1["title"] == "DanMachi Vol 19 [Stick]", "Pre-existing 'DanMachi Vol 19 [Stick]' not prepended with '['")
            r.check(r1["release_group"] == "Stick", "Backfill extracted release_group 'Stick' for pre-existing record")

            r2 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'pre-2'").fetchone()
            r.check(r2["title"] == "Overlord Vol. 16 [Digital] [EPUB]", "Pre-existing 'Overlord Vol. 16 [Digital] [EPUB]' not prepended with '['")

            r3 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'pre-3'").fetchone()
            r.check(r3["title"] == "Solo Leveling, Vol. 8 (Novel) [Yen On] [Retail]", "Pre-existing 'Solo Leveling... [Yen On] [Retail]' not prepended with '['")
            r.check(r3["release_group"] == "Yen On", "Backfill extracted release_group 'Yen On'")

            r4 = c.execute("SELECT title FROM releases WHERE guid = 'pre-4'").fetchone()
            r.check(r4["title"] == "Ascendance of a Bookworm [LN] Vol 1", "Pre-existing 'Ascendance of a Bookworm [LN] Vol 1' not prepended with '['")

            r5 = c.execute("SELECT title FROM releases WHERE guid = 'pre-5'").fetchone()
            r.check(r5["title"] == "Mushoku Tensei Vol. 1", "Unbracketed 'Mushoku Tensei Vol. 1' unchanged")

            # Check clean records untouched
            c1 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'clean-1'").fetchone()
            r.check(c1["title"] == "[Seven Seas] Mushoku Tensei Vol. 1" and c1["release_group"] == "Seven Seas", "Clean record untouched")

            c2 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'clean-2'").fetchone()
            r.check(c2["title"] == "[Stick] DanMachi Vol 19 [Digital]" and c2["release_group"] == "Stick", "Clean '[Stick] DanMachi Vol 19 [Digital]' untouched")

            # Check legacy mangled records repaired
            m1 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'mangled-1'").fetchone()
            r.check(m1["title"] == "[J-Novel Club] Ascendance of a Bookworm Vol. 1", "Legacy 'J- Club]' repaired to '[J-Novel Club]'")
            r.check(m1["release_group"] == "J-Novel Club", "Legacy 'J- Club]' release_group backfilled")

            m2 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'mangled-2'").fetchone()
            r.check(m2["title"] == "[Yen Press] Overlord Vol. 16", "Legacy 'Yen Press]' restored missing opening bracket")
            r.check(m2["release_group"] == "Yen Press", "Legacy 'Yen Press]' release_group backfilled")

            # Check Iteration 3 corrupted records repaired
            cor1 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'corrupted-1'").fetchone()
            r.check(cor1["title"] == "DanMachi Vol 19 [Stick]", "Corrupted '[DanMachi Vol 19 [Stick]' repaired")
            r.check(cor1["release_group"] == "Stick", "Corrupted release_group 'DanMachi Vol 19 [Stick' corrected to 'Stick'")

            cor2 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'corrupted-2'").fetchone()
            r.check(cor2["title"] == "Overlord Vol. 16 [Digital] [EPUB]", "Corrupted '[Overlord Vol. 16 [Digital] [EPUB]' repaired")
            r.check(cor2["release_group"] == "", "Corrupted release_group 'Overlord Vol. 16 [Digital' reset to ''")

    finally:
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass


def main():
    print("=" * 76)
    print("  NOVELTRAX MILESTONE 4 ITERATION 2 EMPIRICAL CHALLENGER HARNESS")
    print("=" * 76)

    runner = TestRunner()

    temp_dir = tempfile.mkdtemp(prefix="noveltrax_challenger_m4_it2_")
    db_path = os.path.join(temp_dir, "test_noveltrax.db")

    try:
        test_buzzwords_and_groups(runner)
        test_pdf_rejection_matrix(runner)
        test_case_insensitive_prioritized_groups(runner, db_path)
        test_default_sorting_boost(runner, db_path)
        test_ignored_state_transitions(runner, db_path)
        test_adversarial_vulnerabilities(runner, db_path)
        test_m4_it4_remediations(runner)
    finally:
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass

    if not runner.summarize("CHALLENGER M4 ITERATION 2 STRESS HARNESS"):
        sys.exit(1)


if __name__ == "__main__":
    main()
