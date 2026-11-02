import sys
import os
import tempfile
import sqlite3
import gc
import shutil

# Add backend directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))

from app.database import Database
from app.parser import is_pdf_release, extract_release_group, is_valid_group_candidate, BUZZWORDS

def run_adversarial_audit():
    print("=====================================================================")
    print(" FORENSIC AUDITOR INDEPENDENT ADVERSARIAL INJECTION SUITE (M4-IT2)  ")
    print("=====================================================================")
    
    passed = 0
    failed = 0

    def assert_check(condition, description):
        nonlocal passed, failed
        if condition:
            print(f"  [PASS] {description}")
            passed += 1
        else:
            print(f"  [FAIL] {description}")
            failed += 1

    # -------------------------------------------------------------
    # 1. Unusual Case-Variant Group Names & Collation Stress
    # -------------------------------------------------------------
    print("\n--- 1. Case-Variant Group Names & DB Collation Stress ---")
    tmpdir1 = tempfile.mkdtemp()
    try:
        db_path = os.path.join(tmpdir1, "test_adv.db")
        db = Database(db_path)
        db.init_db()

        # Seed defaults
        initial_groups = db.get_prioritized_groups()
        assert_check(len(initial_groups) == 5, f"Initial seeded groups == 5 (got {len(initial_groups)})")

        # Try inserting 'yen press', 'YEN PRESS', 'yEn PrEsS'
        res1 = db.add_prioritized_group("yen press", notes="lowercase", priority=2)
        res2 = db.add_prioritized_group("YEN PRESS", notes="uppercase", priority=3)
        res3 = db.add_prioritized_group("  yEn PrEsS  ", notes="mixed case spaces", priority=5)

        assert_check(res1 and res2 and res3, "All case-variant inserts returned True (handled via conflict)")

        all_groups = db.get_prioritized_groups()
        yen_press_entries = [g for g in all_groups if g["name"].lower() == "yen press"]
        assert_check(len(yen_press_entries) == 1, f"Exactly 1 'Yen Press' entry exists (got {len(yen_press_entries)})")
        assert_check(yen_press_entries[0]["priority"] == 5, f"Updated priority is 5 (got {yen_press_entries[0]['priority']})")
        assert_check(yen_press_entries[0]["notes"] == "mixed case spaces", "Updated notes persisted correctly")

        # Query checks with case variations
        assert_check(db.is_group_prioritized("YEN PRESS"), "is_group_prioritized matches uppercase")
        assert_check(db.is_group_prioritized("yen press"), "is_group_prioritized matches lowercase")
        assert_check(db.is_group_prioritized("  yEn PrEsS  "), "is_group_prioritized matches padded mixed case")

        # Exotic Unicode group names
        unicode_group = "Lu自主规制"
        db.add_prioritized_group(unicode_group, notes="Unicode group", priority=4)
        assert_check(db.is_group_prioritized("Lu自主规制"), "Unicode group 'Lu自主规制' recognized")
        assert_check(db.is_group_prioritized("lu自主规制"), "Unicode group case-insensitively recognized")

        # Migration idempotency: call ensure_prioritized_groups_table multiple times
        db.ensure_prioritized_groups_table()
        db.ensure_prioritized_groups_table()
        groups_after_reensure = db.get_prioritized_groups()
        assert_check(len(groups_after_reensure) == 6, f"Group count preserved after multiple migrations (expected 6, got {len(groups_after_reensure)})")
    finally:
        del db
        gc.collect()
        shutil.rmtree(tmpdir1, ignore_errors=True)

    # -------------------------------------------------------------
    # 2. State Transitions & Dirty Flag Recovery
    # -------------------------------------------------------------
    print("\n--- 2. State Transitions & Dirty Flag Recovery ---")
    tmpdir2 = tempfile.mkdtemp()
    try:
        db_path = os.path.join(tmpdir2, "test_dirty.db")
        db = Database(db_path)
        db.init_db()

        # Direct DB injection of dirty states
        with db.get_connection() as conn:
            cur = conn.cursor()
            # Dirty state 1: status = 'REQUESTED', ignored = 1
            cur.execute("""
                INSERT INTO releases (guid, title, status, ignored, created_at)
                VALUES ('dirty-1', 'Dirty Release 1', 'REQUESTED', 1, CURRENT_TIMESTAMP);
            """)
            # Dirty state 2: status = 'IGNORED', ignored = 0
            cur.execute("""
                INSERT INTO releases (guid, title, status, ignored, created_at)
                VALUES ('dirty-2', 'Dirty Release 2', 'IGNORED', 0, CURRENT_TIMESTAMP);
            """)
            conn.commit()

        # Trigger self-healing via init_db
        db.init_db()

        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT ignored FROM releases WHERE guid = 'dirty-1';")
            d1_ignored = cur.fetchone()["ignored"]
            cur.execute("SELECT ignored FROM releases WHERE guid = 'dirty-2';")
            d2_ignored = cur.fetchone()["ignored"]

        assert_check(d1_ignored == 0, f"Dirty-1 (REQUESTED, 1) self-healed to ignored=0 (got {d1_ignored})")
        assert_check(d2_ignored == 1, f"Dirty-2 (IGNORED, 0) self-healed to ignored=1 (got {d2_ignored})")

        # Test state machine transitions
        # Add a normal release
        ok, rel_id = db.upsert_release({
            "guid": "clean-rel-1",
            "title": "Clean Release 1",
            "series_name": "Test Series",
            "volume": 1.0,
            "status": "REQUESTED"
        })
        assert_check(ok and rel_id > 0, "Clean release inserted")

        # Ignore it
        db.ignore_release(rel_id)
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status, ignored FROM releases WHERE id = ?;", (rel_id,))
            row = cur.fetchone()
        assert_check(row["status"] == "IGNORED" and row["ignored"] == 1, "ignore_release sets status=IGNORED, ignored=1")

        # Transition directly from IGNORED to IN_CALIBRE via update_release_status
        db.update_release_status(rel_id, "IN_CALIBRE", matched_book_id=42)
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status, ignored, calibre_matched_book_id FROM releases WHERE id = ?;", (rel_id,))
            row = cur.fetchone()
        assert_check(row["status"] == "IN_CALIBRE" and row["ignored"] == 0 and row["calibre_matched_book_id"] == 42,
                     f"update_release_status to IN_CALIBRE clears ignored to 0 (status={row['status']}, ignored={row['ignored']})")

        # Transition to NEW
        db.update_release_status(rel_id, "NEW")
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status, ignored FROM releases WHERE id = ?;", (rel_id,))
            row = cur.fetchone()
        assert_check(row["status"] == "NEW" and row["ignored"] == 0, "update_release_status to NEW keeps ignored=0")

        # Transition back to IGNORED via update_release_status
        db.update_release_status(rel_id, "IGNORED")
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status, ignored FROM releases WHERE id = ?;", (rel_id,))
            row = cur.fetchone()
        assert_check(row["status"] == "IGNORED" and row["ignored"] == 1, "update_release_status to IGNORED sets ignored=1")
    finally:
        del db
        gc.collect()
        shutil.rmtree(tmpdir2, ignore_errors=True)

    # -------------------------------------------------------------
    # 3. PDF Regex Against Diverse URL Schemes & Delimiters
    # -------------------------------------------------------------
    print("\n--- 3. PDF Regex & URL Scheme Hardening ---")
    pdf_positive_cases = [
        # (title, link, guid, desc)
        ("Overlord Vol 1", "https://example.com/books/123.pdf", None, "Direct .pdf link"),
        ("Overlord Vol 1", "https://example.com/books/123.PDF", None, "Uppercase .PDF link"),
        ("Overlord Vol 1", "https://example.com/books/123.pdf?token=abc&auth=1", None, "Query params after .pdf"),
        ("Overlord Vol 1", "https://example.com/books/123.pdf#page=1", None, "Fragment after .pdf"),
        ("Overlord Vol 1", "https://example.com/books/123.pdf&ref=xyz", None, "Ampersand after .pdf"),
        ("Overlord Vol 1", "https://example.com/books/123.pdf/download", None, "Slash after .pdf"),
        ("Overlord Vol 1", "https://example.com/download?format=pdf", None, "format=pdf query param"),
        ("Overlord Vol 1", "https://example.com/download?filetype=pdf", None, "filetype=pdf query param"),
        ("Overlord Vol 1", "https://example.com/download?ext=pdf", None, "ext=pdf query param"),
        ("Overlord Vol 1", "https://example.com/pdf/download/123", None, "/pdf/ path component"),
        ("Title [PDF]", None, None, "Title with [PDF]"),
        ("Title (PDF)", None, None, "Title with (PDF)"),
        ("Title [PDF+EPUB]", None, None, "Title with [PDF+EPUB]"),
        ("Title.pdf", None, None, "Title ending with .pdf"),
        ("Some PDF Guide", None, None, "Standalone word 'PDF' in title"),
        ("Clean Title", None, "urn:sha1:12345.pdf", "GUID ending with .pdf"),
        ("Clean Title", None, "https://storage.cdn/doc.pdf?dl=1", "GUID URL with .pdf and query"),
    ]

    for title, link, guid, desc in pdf_positive_cases:
        res = is_pdf_release(raw_title=title, link=link, guid=guid)
        assert_check(res is True, f"Positive PDF detection: {desc}")

    pdf_negative_cases = [
        # (title, link, guid, desc)
        ("Overlord Vol 16 [Digital] [EPUB]", "https://nyaa.si/view/12345", "12345", "Clean EPUB release"),
        ("So I'm a Spider, So What? Vol 01", "https://nyaa.si/download/54321.torrent", "54321", "Torrent URL"),
        ("Speedster of the Void Book 3", "magnet:?xt=urn:btih:ABCDEF&dn=Speedster", "guid-1", "Magnet link"),
        ("PDFish Adventures Vol 1", "https://example.com/item/100", "guid-2", "Word containing 'pdf' as substring ('PDFish')"),
        ("Classroom of the Elite Vol. 11.5", "https://example.com/epubs/cotel_11_5.epub", "guid-3", "EPUB download URL"),
    ]

    for title, link, guid, desc in pdf_negative_cases:
        res = is_pdf_release(raw_title=title, link=link, guid=guid)
        assert_check(res is False, f"Negative PDF detection (correctly allowed): {desc}")

    # -------------------------------------------------------------
    # 4. Group Extraction & Buzzword Rejection Boundary Matrix
    # -------------------------------------------------------------
    print("\n--- 4. Group Extraction & Buzzword Rejection Boundaries ---")
    group_buzzword_negative_cases = [
        "[Multi-Audio] Anime Title",
        "[Dual-Audio] Anime Title",
        "[FLAC] Anime Soundtrack",
        "[AAC] Title",
        "[MP3] Title",
        "[Opus] Title",
        "[M4B] Audiobook Title",
        "[1080p] Video Title",
        "[720p] Video Title",
        "[2160p] Video Title",
        "[4K] [HDR] Video Title",
        "[HEVC] [10bit] Title",
        "[x265] [BluRay] Title",
        "[Multi-Audio / FLAC / 1080p] Complex Title",
        "[FLAC+AAC+MP3] Audio Pack",
        "[Complete] Novel Series",
        "[Digital] Novel Series",
        "[Batch] Novel Series",
        "[EPUB] Novel Series",
        "[2023-2024] Series Title",
        "[v01-05] Series Title",
    ]

    for title in group_buzzword_negative_cases:
        extracted = extract_release_group(title)
        assert_check(extracted is None, f"Technical tag correctly rejected: '{title}' -> None (got {extracted})")

    group_positive_cases = [
        ("[Yen Press] Overlord Vol 16", "Yen Press"),
        ("[J-Novel Club] By the Grace of the Gods Vol 1", "J-Novel Club"),
        ("[Seven Seas] Mushoku Tensei Vol 1", "Seven Seas"),
        ("[Stick] Mushoku Tensei Vol 1", "Stick"),
        ("[One Peace Books] Shield Hero Vol 1", "One Peace Books"),
        ("[danke-Empire] Bleach v01", "danke-Empire"),
        ("[NaBi] Classroom of the Elite Vol 1", "NaBi"),
        ("[Lu自主规制] Chinese Translation Vol 1", "Lu自主规制"),
        ("Overlord Vol 16 - Stick (Digital)", "Stick"),
        ("Re:Zero Vol 1 (J-Novel Club)", "J-Novel Club"),
    ]

    for title, expected in group_positive_cases:
        extracted = extract_release_group(title)
        assert_check(extracted == expected, f"Group correctly extracted: '{title}' -> '{expected}' (got '{extracted}')")

    print(f"\nAdversarial Probe Summary: {passed} passed, {failed} failed.")
    if failed > 0:
        print("RESULT: FAILED")
        sys.exit(1)
    else:
        print("RESULT: ALL ADVERSARIAL CHECKS PASSED")
        sys.exit(0)

if __name__ == "__main__":
    run_adversarial_audit()
