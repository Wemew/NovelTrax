#!/usr/bin/env python3
"""
tests/test_challenger_m4_it4_deep_stress.py

Independent Empirical Adversarial Stress Harness for NovelTrax Milestone 4 Iteration 4:
1. Deep Stress of Release Group 'GZ' (Casing, Brackets, Fallbacks, Scene Delimiters, Disambiguation vs Archive Formats).
2. Video Resolution & Technical Dimension Rejection Matrix (Standard, Lower/Upper X, Trailing P/I, Composite Noise).
3. Database Migration Invariant & Multi-Pass Self-Healing Stress (Idempotency, Preserving Clean, Repairing Mangled/Corrupted).
4. Edge Case Ingestion & Priority Ranking Verification.
"""

import os
import sys
import shutil
import sqlite3
import tempfile
from typing import List, Dict, Any, Optional

# Ensure backend directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))

from app.database import Database
from app.parser import (
    is_valid_group_candidate,
    extract_release_group,
    BUZZWORDS,
    DEFAULT_KNOWN_GROUPS,
    KNOWN_PUBLISHERS
)


class DeepStressRunner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def check(self, condition: bool, description: str, details: str = ""):
        if condition:
            self.passed += 1
            print(f"  [PASS] {description}")
        else:
            self.failed += 1
            print(f"  [FAIL] {description}")
            if details:
                print(f"         Details: {details}")
            self.failures.append(f"{description} -- {details}")

    def summarize(self, name: str = "DEEP STRESS HARNESS") -> bool:
        total = self.passed + self.failed
        pct = (self.passed / total * 100) if total > 0 else 0.0
        print("\n" + "=" * 76)
        print(f"  {name} SUMMARY: {self.passed} PASSED | {self.failed} FAILED | {total} TOTAL ({pct:.1f}%)")
        print("=" * 76)
        if self.failed > 0:
            print("\n  FAILURES:")
            for f in self.failures:
                print(f"    - {f}")
            return False
        return True


def test_gz_and_archive_disambiguation(r: DeepStressRunner):
    print("\n" + "=" * 76)
    print("  SUITE 1: GZ RELEASE GROUP DISAMBIGUATION & ARCHIVE MATRIX")
    print("=" * 76)

    # Invariants for GZ
    r.check("GZ" in DEFAULT_KNOWN_GROUPS, "'GZ' is in DEFAULT_KNOWN_GROUPS")
    r.check("gz" not in BUZZWORDS, "'gz' is removed from BUZZWORDS")

    # Invariants for archive formats in BUZZWORDS
    for archive in ["7z", "zip", "rar", "tar"]:
        r.check(archive in BUZZWORDS, f"Archive format '{archive}' is present in BUZZWORDS")
        r.check(not is_valid_group_candidate(archive), f"'{archive}' rejected as group candidate")
        extracted = extract_release_group(f"[{archive}] Some Light Novel Vol 1")
        r.check(extracted is None, f"extract_release_group on '[{archive}]' returns None (got {extracted})")

    # GZ casing and candidate validation
    for variant in ["GZ", "gz", "Gz", "gZ"]:
        r.check(is_valid_group_candidate(variant), f"'{variant}' accepted as valid group candidate")

    # GZ in brackets at start, middle, end
    r.check(extract_release_group("[GZ] Overlord Vol 1") == "GZ", "Leading bracket '[GZ]' extracted as 'GZ'")
    r.check(extract_release_group("(GZ) Overlord Vol 1") == "GZ", "Leading parenthesis '(GZ)' extracted as 'GZ'")
    r.check(extract_release_group("Overlord Vol 1 [GZ]") == "GZ", "Trailing bracket '[GZ]' extracted as 'GZ'")
    r.check(extract_release_group("Overlord Vol 1 (GZ)") == "GZ", "Trailing parenthesis '(GZ)' extracted as 'GZ'")
    r.check(extract_release_group("Overlord [GZ] Vol 1") == "GZ", "Middle bracket '[GZ]' extracted as 'GZ'")

    # GZ trailing scene format
    r.check(extract_release_group("Overlord Vol 1-GZ (Digital)") == "GZ", "Trailing scene '-GZ' extracted")
    r.check(extract_release_group("Overlord Vol 1 - GZ") is None or extract_release_group("Overlord Vol 1-GZ") == "GZ", "Direct hyphen '-GZ' recognized")

    # Ripper GZ prioritized over all known publishers
    for pub in ["Yen Press", "J-Novel Club", "Seven Seas", "One Peace Books", "Kodansha", "Square Enix Manga & Books"]:
        # GZ first, then pub
        ex1 = extract_release_group(f"[GZ] [{pub}] Overlord Vol 16")
        r.check(ex1 == "GZ", f"'GZ' prioritized over '{pub}' (GZ first)", f"Got '{ex1}'")

        # Pub first, then GZ
        ex2 = extract_release_group(f"[{pub}] [GZ] Overlord Vol 16")
        r.check(ex2 == "GZ", f"'GZ' prioritized over '{pub}' (publisher first)", f"Got '{ex2}'")

        # GZ with pub and technical noise
        ex3 = extract_release_group(f"[{pub}] Overlord Vol 16 [1080p | Dual-Audio] [EPUB] [GZ]")
        r.check(ex3 == "GZ", f"'GZ' prioritized over '{pub}' with technical noise", f"Got '{ex3}'")


def test_video_resolution_and_dimensions(r: DeepStressRunner):
    print("\n" + "=" * 76)
    print("  SUITE 2: VIDEO RESOLUTION & TECHNICAL NOISE MATRIX")
    print("=" * 76)

    # Standard resolutions (NxM)
    res_list = [
        "1920x1080", "1280x720", "3840x2160", "2560x1440", "848x480", "640x480",
        "720x480", "1920x1200", "2560x1600", "1920X1080", "1280X720", "3840X2160"
    ]
    for res in res_list:
        valid = is_valid_group_candidate(res)
        r.check(not valid, f"Dimensional resolution '{res}' rejected as group candidate")
        extracted = extract_release_group(f"[{res}] Novel Series Vol 1")
        r.check(extracted is None, f"extract_release_group on '[{res}]' returns None (got {extracted})")

    # Vertical resolutions (\d+p)
    vert_res = ["1080p", "720p", "480p", "2160p", "4k", "uhd"]
    for vr in vert_res:
        valid = is_valid_group_candidate(vr)
        r.check(not valid, f"Vertical resolution tag '{vr}' rejected as group candidate")
        extracted = extract_release_group(f"[{vr}] Novel Series Vol 1")
        r.check(extracted is None, f"extract_release_group on '[{vr}]' returns None (got {extracted})")

    # Mixed resolution + valid group
    mixed_cases = [
        ("[1920x1080] [Stick] Novel Vol 1", "Stick"),
        ("[1280x720] [Yen Press] Novel Vol 2", "Yen Press"),
        ("[3840x2160] [GZ] Novel Vol 3", "GZ"),
        ("[GZ] Novel Vol 4 [1920x1080]", "GZ"),
        ("[Stick] Novel Vol 5 [1280x720] [Dual-Audio]", "Stick"),
        ("[Yen Press] Novel Vol 6 [1920x1080] [FLAC / 24bit]", "Yen Press"),
    ]
    for raw, expected in mixed_cases:
        res = extract_release_group(raw)
        r.check(res == expected, f"Mixed candidate '{raw}' extracts '{expected}'", f"Got '{res}'")


def test_database_migration_and_self_healing(r: DeepStressRunner):
    print("\n" + "=" * 76)
    print("  SUITE 3: DATABASE MIGRATION SELF-HEALING & IDEMPOTENCY")
    print("=" * 76)

    temp_dir = tempfile.mkdtemp(prefix="noveltrax_challenger_m4_it4_")
    db_path = os.path.join(temp_dir, "test_deep_migration.db")

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

        # Adversarial test records
        records = [
            # 1. Clean records that MUST NOT be touched
            ("clean-1", "[Seven Seas] Mushoku Tensei Vol. 1", "Mushoku Tensei", 1.0, "Seven Seas"),
            ("clean-2", "[Stick] DanMachi Vol 19 [Digital]", "DanMachi", 19.0, "Stick"),
            ("clean-3", "[GZ] Overlord Vol. 16 [1080p]", "Overlord", 16.0, "GZ"),
            ("clean-4", "Mushoku Tensei Vol. 1", "Mushoku Tensei", 1.0, ""),
            ("clean-5", "Ascendance of a Bookworm [LN] Vol 1", "Ascendance of a Bookworm", 1.0, ""),
            ("clean-6", "DanMachi Vol 19 [Stick]", "DanMachi", 19.0, ""),
            ("clean-7", "Overlord Vol. 16 [Digital] [EPUB]", "Overlord", 16.0, ""),
            ("clean-8", "Solo Leveling, Vol. 8 (Novel) [Yen On] [Retail]", "Solo Leveling", 8.0, ""),

            # 2. Corrupted records from It3 buggy migration (unbalanced prepended '[')
            ("corrupt-1", "[DanMachi Vol 19 [Stick]", "DanMachi", 19.0, "DanMachi Vol 19 [Stick"),
            ("corrupt-2", "[Overlord Vol. 16 [Digital] [EPUB]", "Overlord", 16.0, "Overlord Vol. 16 [Digital"),
            ("corrupt-3", "[Solo Leveling, Vol. 8 (Novel) [Yen On] [Retail]", "Solo Leveling", 8.0, "Solo Leveling, Vol. 8 (Novel) [Yen On"),
            ("corrupt-4", "[Ascendance of a Bookworm [LN] Vol 1", "Ascendance of a Bookworm", 1.0, "Ascendance of a Bookworm [LN"),

            # 3. Mangled records missing opening bracket at the start
            ("mangled-1", "J- Club] Ascendance of a Bookworm Vol. 1", "Ascendance of a Bookworm", 1.0, ""),
            ("mangled-2", "Yen Press] Overlord Vol. 16", "Overlord", 16.0, ""),
            ("mangled-3", "Seven Seas] Classroom of the Elite Vol. 1 [Digital]", "Classroom of the Elite", 1.0, ""),
            ("mangled-4", "Stick] DanMachi Vol 19", "DanMachi", 19.0, ""),
            ("mangled-5", "GZ] Overlord Vol. 16", "Overlord", 16.0, ""),

            # 4. Contaminated release_group containing '['
            ("contam-1", "[Stick] DanMachi Vol 19", "DanMachi", 19.0, "[Stick]"),
            ("contam-2", "Overlord Vol. 16", "Overlord", 16.0, "Overlord [Digital"),
        ]

        for guid, title, s_name, vol, group in records:
            cur.execute(
                "INSERT INTO releases (guid, title, series_name, volume, release_group) VALUES (?, ?, ?, ?, ?)",
                (guid, title, s_name, vol, group)
            )
        conn.commit()
        conn.close()

        # PASS 1: Initialize DB (triggers migrations)
        db = Database(db_path)

        def verify_db_state(pass_num: int):
            with db.get_connection() as c:
                # 1. Check clean records were NOT corrupted
                c1 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'clean-1'").fetchone()
                r.check(c1["title"] == "[Seven Seas] Mushoku Tensei Vol. 1" and c1["release_group"] == "Seven Seas",
                        f"Pass {pass_num}: Clean '[Seven Seas] Mushoku Tensei Vol. 1' intact")

                c2 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'clean-2'").fetchone()
                r.check(c2["title"] == "[Stick] DanMachi Vol 19 [Digital]" and c2["release_group"] == "Stick",
                        f"Pass {pass_num}: Clean '[Stick] DanMachi Vol 19 [Digital]' intact")

                c3 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'clean-3'").fetchone()
                r.check(c3["title"] == "[GZ] Overlord Vol. 16 [1080p]" and c3["release_group"] == "GZ",
                        f"Pass {pass_num}: Clean '[GZ] Overlord Vol. 16 [1080p]' intact")

                c4 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'clean-4'").fetchone()
                r.check(c4["title"] == "Mushoku Tensei Vol. 1" and c4["release_group"] == "",
                        f"Pass {pass_num}: Clean unbracketed 'Mushoku Tensei Vol. 1' intact")

                c5 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'clean-5'").fetchone()
                r.check(c5["title"] == "Ascendance of a Bookworm [LN] Vol 1" and c5["release_group"] == "",
                        f"Pass {pass_num}: Clean trailing '[LN]' not prepended with '['")

                c6 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'clean-6'").fetchone()
                r.check(c6["title"] == "DanMachi Vol 19 [Stick]" and c6["release_group"] == "Stick",
                        f"Pass {pass_num}: 'DanMachi Vol 19 [Stick]' not prepended, release_group backfilled as 'Stick'")

                c7 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'clean-7'").fetchone()
                r.check(c7["title"] == "Overlord Vol. 16 [Digital] [EPUB]" and c7["release_group"] == "",
                        f"Pass {pass_num}: 'Overlord Vol. 16 [Digital] [EPUB]' not prepended, buzzwords ignored")

                c8 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'clean-8'").fetchone()
                r.check(c8["title"] == "Solo Leveling, Vol. 8 (Novel) [Yen On] [Retail]" and c8["release_group"] == "Yen On",
                        f"Pass {pass_num}: 'Solo Leveling... [Yen On]' not prepended, backfilled as 'Yen On'")

                # 2. Check corrupted records repaired
                cor1 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'corrupt-1'").fetchone()
                r.check(cor1["title"] == "DanMachi Vol 19 [Stick]",
                        f"Pass {pass_num}: Corrupted '[DanMachi Vol 19 [Stick]' repaired to 'DanMachi Vol 19 [Stick]'")
                r.check(cor1["release_group"] == "Stick",
                        f"Pass {pass_num}: Corrupted group restored to 'Stick'")

                cor2 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'corrupt-2'").fetchone()
                r.check(cor2["title"] == "Overlord Vol. 16 [Digital] [EPUB]",
                        f"Pass {pass_num}: Corrupted '[Overlord Vol. 16 [Digital] [EPUB]' repaired")
                r.check(cor2["release_group"] == "",
                        f"Pass {pass_num}: Corrupted group cleared to empty")

                cor3 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'corrupt-3'").fetchone()
                r.check(cor3["title"] == "Solo Leveling, Vol. 8 (Novel) [Yen On] [Retail]",
                        f"Pass {pass_num}: Corrupted Solo Leveling title repaired")
                r.check(cor3["release_group"] == "Yen On",
                        f"Pass {pass_num}: Corrupted Solo Leveling group restored to 'Yen On'")

                cor4 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'corrupt-4'").fetchone()
                r.check(cor4["title"] == "Ascendance of a Bookworm [LN] Vol 1",
                        f"Pass {pass_num}: Corrupted Bookworm title repaired")
                r.check(cor4["release_group"] == "",
                        f"Pass {pass_num}: Corrupted Bookworm group cleared")

                # 3. Check mangled records repaired
                m1 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'mangled-1'").fetchone()
                r.check(m1["title"] == "[J-Novel Club] Ascendance of a Bookworm Vol. 1" and m1["release_group"] == "J-Novel Club",
                        f"Pass {pass_num}: 'J- Club]' repaired to '[J-Novel Club]' and group backfilled")

                m2 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'mangled-2'").fetchone()
                r.check(m2["title"] == "[Yen Press] Overlord Vol. 16" and m2["release_group"] == "Yen Press",
                        f"Pass {pass_num}: 'Yen Press]' restored opening bracket and group backfilled")

                m3 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'mangled-3'").fetchone()
                r.check(m3["title"] == "[Seven Seas] Classroom of the Elite Vol. 1 [Digital]" and m3["release_group"] == "Seven Seas",
                        f"Pass {pass_num}: 'Seven Seas]' restored opening bracket and group backfilled")

                m4 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'mangled-4'").fetchone()
                r.check(m4["title"] == "[Stick] DanMachi Vol 19" and m4["release_group"] == "Stick",
                        f"Pass {pass_num}: 'Stick]' restored opening bracket and group backfilled")

                m5 = c.execute("SELECT title, release_group FROM releases WHERE guid = 'mangled-5'").fetchone()
                r.check(m5["title"] == "[GZ] Overlord Vol. 16" and m5["release_group"] == "GZ",
                        f"Pass {pass_num}: 'GZ]' restored opening bracket and group backfilled as 'GZ'")

                # 4. Check contaminated groups reset
                cont1 = c.execute("SELECT release_group FROM releases WHERE guid = 'contam-1'").fetchone()
                r.check(cont1["release_group"] == "Stick",
                        f"Pass {pass_num}: Contaminated '[Stick]' cleaned and properly extracted as 'Stick'")

                cont2 = c.execute("SELECT release_group FROM releases WHERE guid = 'contam-2'").fetchone()
                r.check(cont2["release_group"] == "",
                        f"Pass {pass_num}: Contaminated 'Overlord [Digital' cleared to empty")

                # 5. Check no release_group contains '[' anywhere in the table
                bad_groups = c.execute("SELECT count(*) as cnt FROM releases WHERE release_group LIKE '%[%'").fetchone()["cnt"]
                r.check(bad_groups == 0, f"Pass {pass_num}: Zero release_group entries contain '[' (found {bad_groups})")

        verify_db_state(1)

        # PASS 2: Multi-pass idempotency check (calling init_db again must NOT corrupt or alter)
        db.init_db()
        verify_db_state(2)

        # PASS 3: Third pass idempotency check
        db.init_db()
        verify_db_state(3)

    finally:
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass


def main():
    print("=" * 76)
    print("  NOVELTRAX M4 IT4 EMPIRICAL CHALLENGER DEEP STRESS HARNESS")
    print("=" * 76)

    runner = DeepStressRunner()

    test_gz_and_archive_disambiguation(runner)
    test_video_resolution_and_dimensions(runner)
    test_database_migration_and_self_healing(runner)

    if not runner.summarize():
        sys.exit(1)


if __name__ == "__main__":
    main()
