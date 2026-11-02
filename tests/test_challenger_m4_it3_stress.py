"""
tests/test_challenger_m4_it3_stress.py

Adversarial Stress Test Suite for NovelTrax Milestone 4 Iteration 3:
- Defect A: Title Mangling Ingestion & VIP Release Group Extraction
- Defect B: Hyphenated & Composite Buzzword Rejection Boundary Probes
- Self-Healing Database Migration Verification
- Multi-Publisher, Ripper Priority & Unicode Tag Matrix
- Adversarial Boundary Probes & Future Hardening Invariants
"""

import os
import sys
import tempfile
import shutil
import sqlite3
from typing import List, Dict, Any, Optional

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))

from app.database import Database
from app.parser import (
    extract_release_group,
    is_valid_group_candidate,
    BUZZWORDS,
    KNOWN_PUBLISHERS,
    DEFAULT_KNOWN_GROUPS
)
from app.providers.metadata import NovelMetadataProvider


class ChallengerTestRunner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def check(self, condition: bool, description: str, failure_msg: str = ""):
        if condition:
            self.passed += 1
            print(f"  [PASS] {description}")
        else:
            self.failed += 1
            msg = f"  [FAIL] {description}"
            if failure_msg:
                msg += f"\n         Details: {failure_msg}"
            print(msg)
            self.failures.append(description)

    def note(self, info: str):
        print(f"  [NOTE] {info}")

    def summarize(self, suite_name: str):
        total = self.passed + self.failed
        pct = (self.passed / total * 100) if total > 0 else 0.0
        print("\n" + "=" * 76)
        print(f"  {suite_name} SUMMARY: {self.passed} PASSED | {self.failed} FAILED | {total} TOTAL ({pct:.1f}%)")
        print("=" * 76)
        if self.failed > 0:
            print("\n  LIST OF FAILURES:")
            for f in self.failures:
                print(f"    - {f}")
            sys.exit(1)


# ============================================================================
# SUITE 1: TITLE MANGLING INVARIANTS & FORMAT CLEANER STRESS (DEFECT A)
# ============================================================================
def test_title_mangling_invariants(runner: ChallengerTestRunner):
    print("\n" + "=" * 76)
    print("  SUITE 1: TITLE MANGLING INVARIANTS & FORMAT CLEANER STRESS (DEFECT A)")
    print("=" * 76)

    cases = [
        # (input_title, expected_clean_output, description)
        ("[J-Novel Club] Ascendance of a Bookworm Vol. 1",
         "[J-Novel Club] Ascendance of a Bookworm Vol. 1",
         "clean_novel_title preserves [J-Novel Club] prefix verbatim"),

        ("[J-Novel Heart] The Saint's Magic Power is Omnipotent Vol. 1",
         "[J-Novel Heart] The Saint's Magic Power is Omnipotent Vol. 1",
         "clean_novel_title preserves [J-Novel Heart] prefix verbatim"),

        ("[Novel] Ascendance of a Bookworm Vol. 1",
         "Ascendance of a Bookworm Vol. 1",
         "clean_novel_title strips standalone [Novel] tag"),

        ("[Light Novel] Overlord Vol. 1",
         "Overlord Vol. 1",
         "clean_novel_title strips standalone [Light Novel] tag"),

        ("(Light Novel) Overlord Vol. 1",
         "Overlord Vol. 1",
         "clean_novel_title strips standalone (Light Novel) tag"),

        ("[LN] DanMachi Vol. 19",
         "DanMachi Vol. 19",
         "clean_novel_title strips standalone [LN] tag"),

        ("(LN) DanMachi Vol. 19",
         "DanMachi Vol. 19",
         "clean_novel_title strips standalone (LN) tag"),

        ("[WN] Mushoku Tensei Vol. 25",
         "Mushoku Tensei Vol. 25",
         "clean_novel_title strips standalone [WN] tag"),

        ("(Web Novel) Re:Zero Arc 7",
         "Re:Zero Arc 7",
         "clean_novel_title strips standalone (Web Novel) tag"),

        ("Novel Club Chronicles Vol. 1",
         "Novel Club Chronicles Vol. 1",
         "clean_novel_title does NOT strip 'Novel' when part of title words"),

        ("The Novelist and the Fox Vol. 2",
         "The Novelist and the Fox Vol. 2",
         "clean_novel_title preserves 'Novelist' in series title"),

        ("Super Web Novel Adventure",
         "Super Web Novel Adventure",
         "clean_novel_title preserves 'Web Novel' when not in brackets/parentheses"),

        ("Boogiepop and Others [Novel]",
         "Boogiepop and Others",
         "clean_novel_title strips trailing [Novel]"),

        ("Welcome to the N.H.K. (Novel)",
         "Welcome to the N.H.K.",
         "clean_novel_title strips trailing (Novel)"),

        ("[Seven Seas] Classroom of the Elite [Light Novel] Vol. 1",
         "[Seven Seas] Classroom of the Elite Vol. 1",
         "clean_novel_title strips [Light Novel] while preserving [Seven Seas]"),

        ("[Yen Press] [LN] 86 - Eighty Six - Vol. 12",
         "[Yen Press] 86 - Eighty Six - Vol. 12",
         "clean_novel_title strips [LN] while preserving [Yen Press]")
    ]

    for raw, expected, desc in cases:
        cleaned = NovelMetadataProvider.clean_novel_title(raw)
        runner.check(cleaned == expected, desc, f"Input: '{raw}' -> Got: '{cleaned}', Expected: '{expected}'")


# ============================================================================
# SUITE 2: SELF-HEALING DATABASE MIGRATION FORENSIC VERIFICATION
# ============================================================================
def test_self_healing_migration(runner: ChallengerTestRunner):
    print("\n" + "=" * 76)
    print("  SUITE 2: SELF-HEALING DATABASE MIGRATION FORENSIC VERIFICATION")
    print("=" * 76)

    temp_dir = tempfile.mkdtemp(prefix="noveltrax_challenger_m4_it3_db_")
    db_path = os.path.join(temp_dir, "legacy_corrupted.db")

    try:
        # Create an un-migrated legacy schema with mangled records
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
        # Insert corrupted records from prior iteration bugs
        cur.execute("INSERT INTO releases (guid, title, series_name, release_group) VALUES (?, ?, ?, ?)",
                    ("mangled-1", "J- Club] Ascendance of a Bookworm Vol. 1", "Ascendance of a Bookworm", ""))
        cur.execute("INSERT INTO releases (guid, title, series_name, release_group) VALUES (?, ?, ?, ?)",
                    ("mangled-2", "J- Club] By the Grace of the Gods Vol. 2", "By the Grace of the Gods", ""))
        cur.execute("INSERT INTO releases (guid, title, series_name, release_group) VALUES (?, ?, ?, ?)",
                    ("mangled-3", "Yen Press] Overlord Vol. 16", "Overlord", ""))
        cur.execute("INSERT INTO releases (guid, title, series_name, release_group) VALUES (?, ?, ?, ?)",
                    ("clean-1", "[Seven Seas] Mushoku Tensei Vol. 1", "Mushoku Tensei", "Seven Seas"))
        conn.commit()
        conn.close()

        # Instantiate Database wrapper, triggering init_db() and its self-healing migrations
        db = Database(db_path)

        with db.get_connection() as c:
            r1 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'mangled-1';").fetchone()
            runner.check(r1["title"] == "[J-Novel Club] Ascendance of a Bookworm Vol. 1",
                         "Self-healing migration repaired 'J- Club]' to '[J-Novel Club]'",
                         f"Got: title='{r1['title']}'")
            runner.check(r1["release_group"] == "J-Novel Club",
                         "Self-healing migration backfilled release_group = 'J-Novel Club'",
                         f"Got: release_group='{r1['release_group']}'")

            r2 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'mangled-2';").fetchone()
            runner.check(r2["title"] == "[J-Novel Club] By the Grace of the Gods Vol. 2",
                         "Self-healing migration repaired second mangled J-Novel Club title")
            runner.check(r2["release_group"] == "J-Novel Club",
                         "Self-healing migration backfilled second release_group")

            r3 = c.execute("SELECT title FROM releases WHERE guid = 'mangled-3';").fetchone()
            runner.check(r3["title"] == "[Yen Press] Overlord Vol. 16",
                         "Self-healing migration restored missing opening bracket on 'Yen Press]'",
                         f"Got: title='{r3['title']}'")

            r4 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'clean-1';").fetchone()
            runner.check(r4["title"] == "[Seven Seas] Mushoku Tensei Vol. 1" and r4["release_group"] == "Seven Seas",
                         "Self-healing migration leaves already clean records completely untouched")

    finally:
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass


# ============================================================================
# SUITE 3: BUZZWORDS & COMPOSITE TECHNICAL NOISE REJECTION MATRIX (DEFECT B)
# ============================================================================
def test_composite_buzzword_matrix(runner: ChallengerTestRunner):
    print("\n" + "=" * 76)
    print("  SUITE 3: BUZZWORDS & COMPOSITE TECHNICAL NOISE REJECTION MATRIX (DEFECT B)")
    print("=" * 76)

    # Core target matrix of technical noise tags remediated under Defect B
    composite_noise_tags = [
        # Video codecs & containers
        "Blu-Ray / 1080p",
        "Web-DL / 1080p",
        "Web-DL / 2160p / HDR",
        "BDRip / 720p / x264",
        "BD-Remux / 1080p",
        "Remux / 2160p / HDR10",
        "MKV / H.264 / AAC",
        "MKV / H.265 / 10bit",
        "MP4 / x264 / 720p",
        "AVI / Xvid / MP3",
        "ISO / DVD-Rip",
        "WebRip / 1080p / HEVC",
        "Web-DL / 4K / UHD",
        "FLV / 480p / MP3",
        "WMV / 720p",
        "MOV / H264 / 1080p",

        # Audio formats, bitrates, and channels
        "Audiobook - 64kbps",
        "Audiobook - 128kbps - MP3",
        "Audiobook - 320kbps",
        "FLAC / 24bit / Lossless",
        "Lossless / FLAC / 16bit",
        "TrueHD / Atmos / 7.1",
        "DTS / 5.1 / Lossless",
        "EAC3 / 5.1 / 640kbps",
        "AAC / 256kbps",
        "Opus / 128kbps",
        "Multi-Audio / Dual-Audio",
        "Multi Audio / FLAC",
        "Dual Audio / AAC",
        "Audiobook / M4B",

        # Subtitles, language & packaging
        "Multi-Subs / x265 / 10bit",
        "Dual-Sub / 1080p / BluRay",
        "Softsub / Eng",
        "Hardsub / 720p",
        "English / Digital / Retail",
        "Digital / Retail / EPUB",
        "Retail / Scans / Fixed",
        "Batch / Complete / v1",
        "Ongoing / Digital / Pack",
        "Bonus + Artbook + Extras",
        "Artbooks / Illustrations / Scans",
        "Uncensored / Remastered / Fixed",
        "Repack / Proper / Rerip",
        "Untrimmed / Scans",
        "Jap / Raw / Scans",
        "Chs / Cht / Digital",

        # Document formats & volume ranges
        "EPUB + MOBI + AZW3",
        "PDF + EPUB + CBR + CBZ",
        "v01-15 / Complete",
        "v01-v20 / Digital",
        "2019-2024 / Complete",
        "2023 / Digital / Retail"
    ]

    for tag in composite_noise_tags:
        valid = is_valid_group_candidate(tag)
        runner.check(not valid,
                     f"Noise tag rejected as group candidate: '[{tag}]'",
                     f"is_valid_group_candidate returned True for '{tag}'")
        extracted = extract_release_group(f"[{tag}] Title Vol 1")
        runner.check(extracted is None or extracted != tag,
                     f"extract_release_group ignores '[{tag}]'",
                     f"extract_release_group mistakenly returned '{extracted}'")

    # Positive group preservation: Genuine publishers and release groups MUST be accepted
    genuine_groups = [
        "Yen Press",
        "J-Novel Club",
        "J-Novel Heart",
        "Seven Seas",
        "Seven Seas Entertainment",
        "One Peace Books",
        "Airship",
        "Ghost Ship",
        "Square Enix Manga & Books",
        "Square Enix",
        "Cross Infinite World",
        "Tentai Books",
        "Hanashi Media",
        "Kodansha",
        "Kodansha USA",
        "Viz Media",
        "Dark Horse",
        "Kaiten Books",
        "Stick",
        "LuTX",
        "danke-Empire",
        "Lu自主规制",
        "a-sloth",
        "1R0N",
        "Kobo",
        "BookWalker",
        "NaBi",
        "TenmaLand",
        "aKraa",
        "LostYears",
        "Anonymous"
    ]

    for group in genuine_groups:
        valid = is_valid_group_candidate(group)
        runner.check(valid,
                     f"Genuine group candidate accepted: '{group}'",
                     f"is_valid_group_candidate falsely returned False for '{group}'")
        extracted = extract_release_group(f"[{group}] Novel Title Vol 1")
        runner.check(extracted is not None and extracted.lower() == group.lower(),
                     f"extract_release_group extracts genuine group: '{group}'",
                     f"Got: '{extracted}', Expected: '{group}'")


# ============================================================================
# SUITE 4: MULTI-PUBLISHER & RIPPER GROUP PRIORITIZATION MATRIX
# ============================================================================
def test_multi_publisher_and_ripper_priority(runner: ChallengerTestRunner):
    print("\n" + "=" * 76)
    print("  SUITE 4: MULTI-PUBLISHER & RIPPER GROUP PRIORITIZATION MATRIX")
    print("=" * 76)

    test_matrix = [
        # (title, expected_group, description)
        ("[Yen Press] [Stick] Overlord Vol. 16 [Digital] [EPUB]",
         "Stick",
         "Ripper 'Stick' prioritized over publisher 'Yen Press' when both bracketed"),

        ("[Stick] [Yen Press] Overlord Vol. 16",
         "Stick",
         "Ripper 'Stick' prioritized over publisher 'Yen Press' regardless of order"),

        ("[Seven Seas] [danke-Empire] Classroom of the Elite Vol. 1 [Web-DL / 1080p]",
         "danke-Empire",
         "Ripper 'danke-Empire' prioritized over 'Seven Seas' amidst technical noise"),

        ("[J-Novel Club] [LuTX] Ascendance of a Bookworm Vol. 1",
         "LuTX",
         "Ripper 'LuTX' prioritized over publisher 'J-Novel Club'"),

        ("[One Peace Books] [NaBi] The Rising of the Shield Hero Vol. 22",
         "NaBi",
         "Ripper 'NaBi' prioritized over publisher 'One Peace Books'"),

        ("[Square Enix Manga & Books] [1R0N] The Apothecary Diaries Vol. 4",
         "1R0N",
         "Ripper '1R0N' prioritized over 'Square Enix Manga & Books'"),

        ("[Yen Press] [J-Novel Club] Mixed Anthology Vol. 1",
         "Yen Press",
         "Multiple publishers: First known publisher extracted when no ripper present"),

        ("[Kodansha USA] [Seven Seas] Collaborative Release Vol. 1",
         "Seven Seas",
         "Multiple publishers: Default known group 'Seven Seas' prioritized over 'Kodansha USA'"),

        ("Re:Zero Vol. 25 (J-Novel Club) [Audiobook - 64kbps]",
         "J-Novel Club",
         "Parenthesized publisher extracted while composite audiobook tag rejected"),

        ("DanMachi Vol. 19-Stick (Digital) [EPUB]",
         "Stick",
         "Trailing scene ripper group '-Stick' extracted accurately"),

        ("[Airship] Mushoku Tensei Vol. 26 [Batch / Complete] [EPUB]",
         "Airship",
         "Publisher 'Airship' extracted while [Batch / Complete] is rejected"),

        ("[Ghost Ship] Yuuna and the Haunted Hot Springs Vol. 10 [Digital]",
         "Ghost Ship",
         "Publisher 'Ghost Ship' extracted while [Digital] is rejected"),

        ("[Cross Infinite World] The Reincarnated Princess Vol. 1",
         "Cross Infinite World",
         "Publisher 'Cross Infinite World' extracted cleanly"),

        ("[Hanashi Media] Another World Survival Vol. 1 [EPUB]",
         "Hanashi Media",
         "Publisher 'Hanashi Media' extracted cleanly"),

        ("[Tentai Books] From Toxic Classmate to Girlfriend Vol. 1",
         "Tentai Books",
         "Publisher 'Tentai Books' extracted cleanly")
    ]

    for title, expected, desc in test_matrix:
        extracted = extract_release_group(title)
        runner.check(extracted is not None and extracted.lower() == expected.lower(),
                     desc,
                     f"Title: '{title}' -> Extracted: '{extracted}', Expected: '{expected}'")


# ============================================================================
# SUITE 5: RAW TITLE INGESTION & DATABASE VIP BOOSTING INVARIANTS
# ============================================================================
def test_raw_title_ingestion_and_db_vip(runner: ChallengerTestRunner):
    print("\n" + "=" * 76)
    print("  SUITE 5: RAW TITLE INGESTION & DATABASE VIP BOOSTING INVARIANTS")
    print("=" * 76)

    temp_dir = tempfile.mkdtemp(prefix="noveltrax_challenger_m4_it3_ingest_")
    db_path = os.path.join(temp_dir, "ingest_test.db")

    try:
        db = Database(db_path)
        # Ensure default prioritized groups are seeded (as done on app startup in main.py)
        db.ensure_prioritized_groups_table()

        ingestion_cases = [
            {
                "guid": "raw-jnovel-1",
                "title": "[J-Novel Club] Ascendance of a Bookworm Part 5 Vol 8 [EPUB]",
                "series_name": "Ascendance of a Bookworm",
                "volume": 8.0,
                "expected_group": "J-Novel Club",
                "expected_vip": True
            },
            {
                "guid": "raw-yenpress-stick-1",
                "title": "[Yen Press] [Stick] Overlord Vol. 16 [Digital] [1080p | Dual-Audio]",
                "series_name": "Overlord",
                "volume": 16.0,
                "expected_group": "Stick",
                "expected_vip": True
            },
            {
                "guid": "raw-sevenseas-1",
                "title": "[Seven Seas] Classroom of the Elite: Year 2 Vol. 9.5 [Blu-Ray / 1080p]",
                "series_name": "Classroom of the Elite",
                "volume": 9.5,
                "expected_group": "Seven Seas",
                "expected_vip": True
            },
            {
                "guid": "raw-lutx-1",
                "title": "[LuTX] Is It Wrong to Try to Pick Up Girls in a Dungeon? Vol. 19 [Web-DL / 2160p / HDR]",
                "series_name": "DanMachi",
                "volume": 19.0,
                "expected_group": "LuTX",
                "expected_vip": False  # LuTX is not in default 5 seeded groups (Yen Press, J-Novel Club, Seven Seas, Stick, One Peace Books)
            },
            {
                "guid": "raw-noise-only-1",
                "title": "[Multi-Audio / FLAC / 24bit] [1080p] Unknown Novel Vol. 1",
                "series_name": "Unknown Novel",
                "volume": 1.0,
                "expected_group": "",
                "expected_vip": False
            }
        ]

        for item in ingestion_cases:
            is_new, rel_id = db.upsert_release({
                "guid": item["guid"],
                "title": item["title"],
                "series_name": item["series_name"],
                "volume": item["volume"]
            })

            with db.get_connection() as conn:
                row = conn.execute("SELECT title, series_name, release_group FROM releases WHERE id = ?;", (rel_id,)).fetchone()
                # 1. Verify title was NOT mutated or mangled
                runner.check(row["title"] == item["title"],
                             f"Raw title preserved verbatim in DB for guid='{item['guid']}'",
                             f"Stored: '{row['title']}', Original: '{item['title']}'")

                # 2. Verify extracted release_group
                runner.check(row["release_group"] == item["expected_group"],
                             f"release_group correctly extracted as '{item['expected_group']}' for guid='{item['guid']}'",
                             f"Stored group: '{row['release_group']}'")

        # Now test prioritization recognition
        releases = db.get_releases(status="REQUESTED")
        rel_map = {r["guid"]: r for r in releases}

        for item in ingestion_cases:
            guid = item["guid"]
            r_record = rel_map.get(guid)
            runner.check(r_record is not None, f"Release record retrieved for guid='{guid}'")
            if r_record:
                is_prio = bool(r_record.get("is_prioritized"))
                runner.check(is_prio == item["expected_vip"],
                             f"is_prioritized matches expected {item['expected_vip']} for guid='{guid}' (group='{item['expected_group']}')",
                             f"Got is_prioritized={is_prio}")

        # Test dynamic addition of LuTX to prioritized groups
        db.add_prioritized_group("LuTX", priority=5, notes="Custom Scanlator")
        lutx_prio = db.is_group_prioritized("LuTX")
        runner.check(lutx_prio, "is_group_prioritized('LuTX') is True after dynamic addition")

        # Re-check get_releases to ensure LuTX release is now marked is_prioritized = True
        releases_updated = db.get_releases(status="REQUESTED")
        lutx_record = next(r for r in releases_updated if r["guid"] == "raw-lutx-1")
        runner.check(bool(lutx_record.get("is_prioritized")) is True,
                     "Previously un-prioritized LuTX release dynamically gets is_prioritized=True after group added")

    finally:
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass


# ============================================================================
# SUITE 6: UNICODE GROUPS & SPECIAL PUNCTUATION INVARIANTS
# ============================================================================
def test_unicode_and_special_punctuation(runner: ChallengerTestRunner):
    print("\n" + "=" * 76)
    print("  SUITE 6: UNICODE GROUPS & SPECIAL PUNCTUATION INVARIANTS")
    print("=" * 76)

    cases = [
        # Unicode Chinese group
        ("[Lu自主规制] [Digital] Battle Through the Heavens Vol. 1",
         "Lu自主规制",
         "Unicode Hanzi group 'Lu自主规制' extracted intact"),

        # Punctuation & Ampersand
        ("[Square Enix Manga & Books] Otherside Picnic Vol. 1",
         "Square Enix Manga & Books",
         "Group with ampersand 'Square Enix Manga & Books' extracted intact"),

        # Hyphenated group names
        ("[danke-Empire] Bleach Official Character Book [Digital]",
         "danke-Empire",
         "Hyphenated group 'danke-Empire' extracted intact"),

        ("[a-sloth] Re:Zero Ex Vol. 1 [Retail]",
         "a-sloth",
         "Hyphenated group 'a-sloth' extracted intact"),

        ("[J-Novel Club] Realist Hero Vol. 1",
         "J-Novel Club",
         "Hyphenated publisher 'J-Novel Club' extracted intact"),

        # Em-dash scene group delimiter
        ("Overlord Vol. 16—Stick [Digital]",
         "Stick",
         "Em-dash scene group delimiter extracted intact"),

        # Hyphen scene group delimiter
        ("DanMachi Vol. 19-LuTX [EPUB]",
         "LuTX",
         "Hyphen scene group delimiter extracted intact"),

        # Tilde delimited composite noise
        ("[1080p~Dual-Audio~HEVC] Title Vol. 1",
         None,
         "Tilde delimited composite noise tag rejected as group candidate"),

        # Parenthesized publisher tag
        ("(J-Novel Club) Ascendance of a Bookworm Vol. 1",
         "J-Novel Club",
         "Parenthesized publisher '(J-Novel Club)' extracted intact"),

        # Multiple bracketed noise tags with trailing publisher
        ("[Multi-Audio] [Dual-Sub] [FLAC] Title Vol. 1 [Yen Press]",
         "Yen Press",
         "Trailing publisher extracted correctly after multiple noise brackets")
    ]

    for title, expected, desc in cases:
        extracted = extract_release_group(title)
        if expected is None:
            runner.check(extracted is None, desc, f"Title: '{title}' -> Unexpected group '{extracted}'")
        else:
            runner.check(extracted is not None and extracted.lower() == expected.lower(),
                         desc,
                         f"Title: '{title}' -> Extracted: '{extracted}', Expected: '{expected}'")


# ============================================================================
# SUITE 7: ADVERSARIAL BOUNDARY PROBES & RESILIENCE AUDIT (DOCUMENTED LIMITS)
# ============================================================================
def test_adversarial_boundary_probes(runner: ChallengerTestRunner):
    print("\n" + "=" * 76)
    print("  SUITE 7: ADVERSARIAL BOUNDARY PROBES & RESILIENCE AUDIT (DOCUMENTED LIMITS)")
    print("=" * 76)

    # Probe 1: Interlaced video format '1080i'
    tag_1080i = "TS / 1080i / AAC"
    is_valid_1080i = is_valid_group_candidate(tag_1080i)
    runner.note(f"Probe 1 (Interlaced 1080i in '[{tag_1080i}]'): candidate_valid={is_valid_1080i} (regex supports \\d+p, \\d+i is benign omission for literature tracker)")

    # Probe 2: ProRes video editing codec
    tag_prores = "MOV / ProRes / 1080p"
    is_valid_prores = is_valid_group_candidate(tag_prores)
    runner.note(f"Probe 2 (ProRes codec in '[{tag_prores}]'): candidate_valid={is_valid_prores} (ProRes is studio video codec, rare in Category 3_1)")

    # Probe 3: Japanese fullwidth corner brackets 【...】
    japanese_bracket_title = "【Yen Press】 Overlord Vol. 14"
    jp_extracted = extract_release_group(japanese_bracket_title)
    runner.note(f"Probe 3 (Japanese corner brackets '【...】'): extracted='{jp_extracted}' (standard brackets '[...]' and '( ... )' are primary standard)")

    # Probe 4: Scene group with spaced dash delimiter
    spaced_dash_title = "DanMachi Vol. 19 – LuTX [EPUB]"
    spaced_extracted = extract_release_group(spaced_dash_title)
    runner.note(f"Probe 4 (Spaced en-dash ' – LuTX'): extracted='{spaced_extracted}' (unspaced dash '-LuTX' and bracketed '[LuTX]' are primary standard)")

    # Probe 5: Very long group candidate > 40 chars
    long_tag = "A" * 45
    long_rejected = not is_valid_group_candidate(long_tag)
    runner.check(long_rejected,
                 "Oversized tag (> 40 chars) rejected by boundary guard",
                 "Tag > 40 chars was unexpectedly accepted")

    # Probe 6: Single-char candidate rejected
    single_char = "X"
    single_rejected = not is_valid_group_candidate(single_char)
    runner.check(single_rejected,
                 "Single-character candidate rejected by boundary guard (< 2 chars)",
                 "Single-char tag was unexpectedly accepted")


def main():
    print("=" * 76)
    print("  NOVELTRAX CHALLENGER M4 ITERATION 3 ADVERSARIAL STRESS HARNESS")
    print("=" * 76)

    runner = ChallengerTestRunner()

    test_title_mangling_invariants(runner)
    test_self_healing_migration(runner)
    test_composite_buzzword_matrix(runner)
    test_multi_publisher_and_ripper_priority(runner)
    test_raw_title_ingestion_and_db_vip(runner)
    test_unicode_and_special_punctuation(runner)
    test_adversarial_boundary_probes(runner)

    runner.summarize("CHALLENGER M4 ITERATION 3 STRESS HARNESS")


if __name__ == "__main__":
    main()
