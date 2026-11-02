import sys
import os
import tempfile
import sqlite3

sys.path.insert(0, os.path.abspath("backend"))

from app.providers.metadata import NovelMetadataProvider
from app.parser import is_valid_group_candidate, extract_release_group, BUZZWORDS
from app.database import Database

def run_forensic_checks():
    print("=" * 70)
    print("  FORENSIC INTEGRITY AUDIT — MILESTONE 4 ITERATION 3")
    print("=" * 70)

    # 1. clean_novel_title
    test_cases = [
        ('[J-Novel Club] Ascendance of a Bookworm Vol. 1', '[J-Novel Club] Ascendance of a Bookworm Vol. 1'),
        ('Overlord (Novel)', 'Overlord'),
        ('Overlord [Light Novel]', 'Overlord'),
        ('Overlord (Web Novel) Vol 1', 'Overlord Vol 1'),
        ('Overlord (LN)', 'Overlord'),
        ('Overlord (WN)', 'Overlord'),
        ('[Yen Press] Overlord [Light Novel] Vol 1', '[Yen Press] Overlord Vol 1'),
        ('   Overlord (Novel)   ', 'Overlord'),
        ('Novel (Novel)', 'Novel'),
        ("The Novel's Extra (Novel)", "The Novel's Extra"),
        ('[Seven Seas] Classroom of the Elite (Light Novel) Vol. 1', '[Seven Seas] Classroom of the Elite Vol. 1'),
        ('Mushoku Tensei: Isekai Ittara Honki Dasu (WN)', 'Mushoku Tensei: Isekai Ittara Honki Dasu'),
    ]
    for raw, expected in test_cases:
        res = NovelMetadataProvider.clean_novel_title(raw)
        if not (res == expected): raise AssertionError(f"clean_novel_title mismatch: got {res!r}, expected {expected!r}")
    print("[PASS] Check 1: clean_novel_title authentic regex transformation without mangling")

    # 2. is_valid_group_candidate & BUZZWORDS
    invalid_candidates = [
        'Blu-Ray / 1080p',
        'Web-DL 2160p HDR',
        'Audiobook - 64kbps',
        'FLAC / 24bit',
        'MKV',
        'MP4',
        'x265 / HEVC / 10bit',
        'JAP / RAW',
        'CHS / CHT',
        '1080p',
        'Multi-Audio',
        'Dual-Audio',
        'Audiobook',
        '2021',
        '2021-2024',
        'v01-05',
        'Vol 1',
        'Multi-Subs | x265 | 10bit',
        'MP3 / 320k',
        'Batch / Complete',
        'Digital / Retail / EPUB',
        'Bonus + Artbook',
        'TrueHD / Atmos',
        '720p / AAC / x264',
        'Uncensored / Remastered',
        'Remux / 2160p / HDR',
        'English / Digital / Retail',
        'Audiobook - 320kbps',
        'MKV / 1080p',
        'ISO',
        'WMV',
        'AVI',
        'FLV',
        'WEBM'
    ]
    for c in invalid_candidates:
        valid = is_valid_group_candidate(c)
        if not (not valid): raise AssertionError(f"Candidate {c!r} should be invalid but was accepted!")
    print(f"[PASS] Check 2: is_valid_group_candidate rejected all {len(invalid_candidates)} technical/buzzword tags")

    # 3. Genuine Group Preservation
    valid_candidates = [
        'J-Novel Club',
        'Yen Press',
        'Stick',
        'LuTX',
        'Square Enix Manga & Books',
        'One Peace Books',
        'danke-Empire',
        'Seven Seas',
        'LostYears',
        'NaBi',
        'Lu自主规制',
        'Airship',
        'Kodansha',
        'Viz Media',
        'Anonymous',
        'a-sloth',
        '1R0N',
        'Kobo',
        'BookWalker',
        'TenmaLand'
    ]
    for c in valid_candidates:
        valid = is_valid_group_candidate(c)
        if not (valid): raise AssertionError(f"Candidate {c!r} should be valid but was rejected!")
    print(f"[PASS] Check 3: is_valid_group_candidate preserved all {len(valid_candidates)} genuine release groups")

    # 4. Database.upsert_release raw title & group extraction
    temp_dir = tempfile.mkdtemp(prefix="forensic_m4_it3_")
    db_path = os.path.join(temp_dir, "test.db")
    db = Database(db_path)
    is_new, rel_id = db.upsert_release({
        "guid": "forensic-jnovel-1",
        "title": "[J-Novel Club] Ascendance of a Bookworm Vol. 1",
        "series_name": "Ascendance of a Bookworm",
        "volume": 1.0,
        "status": "REQUESTED"
    })
    with db.get_connection() as conn:
        row = conn.execute("SELECT title, series_name, release_group FROM releases WHERE id = ?;", (rel_id,)).fetchone()
        if not (row["title"] == "[J-Novel Club] Ascendance of a Bookworm Vol. 1"): raise AssertionError(f"Title corrupted: {row['title']}")
        if not (row["series_name"] == "Ascendance of a Bookworm"): raise AssertionError("assertion failed at line 121")
        if not (row["release_group"] == "J-Novel Club"): raise AssertionError(f"Group extraction failed: {row['release_group']}")
    print("[PASS] Check 4: Database.upsert_release strictly preserves raw title and extracts release group")

    # 5. Database self-healing migration in init_db
    with db.get_connection() as conn:
        conn.execute("INSERT INTO releases (guid, title, status, release_group) VALUES ('legacy-corrupted-1', 'J- Club] Ascendance of a Bookworm Vol. 2', 'REQUESTED', '');")
        conn.execute("INSERT INTO releases (guid, title, status, release_group) VALUES ('legacy-missing-bracket', 'Yen Press] Overlord Vol. 1', 'REQUESTED', '');")
        conn.commit()

    db.init_db()

    with db.get_connection() as conn:
        r1 = conn.execute("SELECT title, release_group FROM releases WHERE guid = 'legacy-corrupted-1';").fetchone()
        if not (r1["title"] == "[J-Novel Club] Ascendance of a Bookworm Vol. 2"): raise AssertionError(f"Migration failed for J- Club: {r1['title']}")
        if not (r1["release_group"] == "J-Novel Club"): raise AssertionError(f"Migration failed to backfill release_group: {r1['release_group']}")
        r2 = conn.execute("SELECT title, release_group FROM releases WHERE guid = 'legacy-missing-bracket';").fetchone()
        if not (r2["title"] == "[Yen Press] Overlord Vol. 1"): raise AssertionError(f"Migration failed to restore bracket: {r2['title']}")
        if not (r2["release_group"] == "Yen Press"): raise AssertionError(f"Migration failed to extract Yen Press: {r2['release_group']}")
    print("[PASS] Check 5: Database.init_db self-healing SQL migration genuinely restores legacy mangled titles & groups")

    print("=" * 70)
    print("  ALL FORENSIC INTEGRITY CHECKS PASSED (5/5) — ZERO INTEGRITY VIOLATIONS")
    print("=" * 70)

if __name__ == "__main__":
    run_forensic_checks()
