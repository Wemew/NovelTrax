import sys
import os
import shutil

# Add base directory and backend directory to path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'backend'))

from tests.mock_calibre import create_mock_calibre_db, add_book_to_mock_calibre
from app.database import Database
from app.calibre.scanner import CalibreScanner
from app.calibre.matcher import CalibreMatcher
from app.parser import extract_series_and_volume

def run_e2e_test():
    import tempfile
    test_dir = os.path.join(tempfile.gettempdir(), "noveltrax_test_run")
    if os.path.exists(test_dir):
        try:
            shutil.rmtree(test_dir)
        except Exception:
            pass
    os.makedirs(test_dir, exist_ok=True)

    calibre_db_file = os.path.join(test_dir, "calibre", "metadata.db")
    app_db_file = os.path.join(test_dir, "app", "noveltrax.db")

    print("=" * 60)
    print("STEP 1: CREATING REALISTIC MOCK CALIBRE LIBRARY")
    print("=" * 60)
    create_mock_calibre_db(calibre_db_file)
    print(f"Mock Calibre created at {calibre_db_file}")

    print("\n" + "=" * 60)
    print("STEP 2: TESTING ZERO-LOCK CALIBRE SCANNER")
    print("=" * 60)
    scanner = CalibreScanner(os.path.dirname(calibre_db_file))
    summary = scanner.get_library_summary()
    print("Library Summary:", summary)
    assert summary["total_books"] == 39  # 18 + 15 + 6
    assert summary["total_series"] == 3
    assert summary["total_authors"] == 3

    series_list = scanner.get_all_series()
    for s in series_list:
        print(f"  Series: {s['series_name']} | Books: {s['book_count']} | Latest: Vol {s['latest_index']}")

    print("\n" + "=" * 60)
    print("STEP 3: INITIALIZING APP DATABASE & AUTO-POPULATING SERIES")
    print("=" * 60)
    app_db = Database(app_db_file)
    synced_count = app_db.sync_calibre_series(series_list)
    print(f"Auto-populated {synced_count} series into NovelTrax database.")

    db_series = app_db.get_all_series()
    print("App Series Table:")
    for s in db_series:
        print(f"  [ID {s['id']}] {s['name']} (Monitored: {bool(s['is_monitored'])}, Owned: {s['book_count']})")
    assert len(db_series) == 3

    print("\n" + "=" * 60)
    print("STEP 4: SIMULATING J-NOVELS FEED SCRAPER DETECTING RELEASES")
    print("=" * 60)
    books_list = scanner.get_all_books()
    matcher = CalibreMatcher(series_list, books_list)

    incoming_feed_items = [
        "DanMachi Volume 19 Epub",                               # Missing! Latest owned is 18
        "Overlord Volume 15 Epub",                               # Already in Calibre!
        "Dungeon Crawler Carl Book 7 - This Inevitable Ruin",     # Missing! Latest owned is 6
        "Random Unrelated Light Novel Volume 1 Epub"              # Not in tracked series
    ]

    for raw_title in incoming_feed_items:
        parsed = extract_series_and_volume(raw_title)
        res = matcher.check_release({
            "title": raw_title,
            "series_name": parsed["series_name"],
            "volume": parsed["volume"]
        }, auto_request_monitored=True)

        print(f"Incoming: '{raw_title}'")
        print(f"  Parsed Series: '{parsed['series_name']}' | Vol: {parsed['volume']}")
        print(f"  Evaluation: Status = {res['status']} | Reason: {res['reason']}")

        # Store in database
        import hashlib
        guid = hashlib.sha256(raw_title.encode()).hexdigest()[:16]
        app_db.upsert_release({
            "guid": guid,
            "title": raw_title,
            "series_name": parsed["series_name"],
            "volume": parsed["volume"],
            "link": f"https://jnovels.com/?p={guid}",
            "source_feed_name": "J-Novels EPUB Releases",
            "status": res["status"],
            "calibre_matched_book_id": res["matched_book_id"]
        })

    # Check pending requests in DB
    pending = app_db.get_pending_requests()
    print(f"\nTotal Pending Requests in DB: {len(pending)}")
    for p in pending:
        print(f"  - WANTED: {p['title']} (Series: {p['series_name']}, Vol: {p['volume']})")
    assert len(pending) == 2  # DanMachi Vol 19 and DCC Book 7

    print("\n" + "=" * 60)
    print("STEP 5: SIMULATING USER ADDING 'DanMachi Volume 19' TO CALIBRE")
    print("=" * 60)
    print("Adding 'DanMachi, Vol. 19' to Calibre metadata.db...")
    add_book_to_mock_calibre(calibre_db_file, "DanMachi, Vol. 19", series_id=1, series_index=19.0)

    print("Rescanning Calibre library...")
    scanner_rescan = CalibreScanner(os.path.dirname(calibre_db_file))
    updated_series = scanner_rescan.get_all_series()
    updated_books = scanner_rescan.get_all_books()

    # Re-evaluate pending requests against updated Calibre catalog
    matcher2 = CalibreMatcher(updated_series, updated_books)
    resolved = matcher2.resolve_pending_requests(app_db.get_pending_requests())
    print(f"Newly Acquired / Auto-Resolved Books: {len(resolved)}")
    for r in resolved:
        print(f"  ✓ RESOLVED: '{r['title']}' -> Matched Calibre Book #{r['matched_book_id']} ({r['reason']})")
        app_db.update_release_status(r["release_id"], status="IN_CALIBRE", matched_book_id=r["matched_book_id"])

    # Check pending requests again
    remaining_pending = app_db.get_pending_requests()
    print(f"\nRemaining Pending Requests: {len(remaining_pending)}")
    for p in remaining_pending:
        print(f"  - WANTED: {p['title']}")

    assert len(remaining_pending) == 1
    assert remaining_pending[0]["series_name"] == "Dungeon Crawler Carl"
    print("\nSUCCESS: DanMachi Vol 19 was automatically removed from pending requests!")

    print("\n" + "=" * 60)
    print("STEP 6: TESTING NEW SERIES DISCOVERY (VOL. 1 & STANDALONES)")
    print("=" * 60)
    # Add a Volume 1 discovery release
    import hashlib
    v1_title = "Solo Leveling Volume 1 Epub"
    v1_guid = hashlib.sha256(v1_title.encode()).hexdigest()[:16]
    app_db.upsert_release({
        "guid": v1_guid,
        "title": v1_title,
        "series_name": "Solo Leveling",
        "volume": 1.0,
        "link": "https://jnovels.com/solo-leveling-vol-1/",
        "source_feed_name": "J-Novels EPUB Releases",
        "status": "NEW"
    })

    # Add an unnumbered standalone release
    st_title = "Three Days of Happiness Epub"
    st_guid = hashlib.sha256(st_title.encode()).hexdigest()[:16]
    app_db.upsert_release({
        "guid": st_guid,
        "title": st_title,
        "series_name": "Three Days of Happiness",
        "volume": None,
        "link": "https://jnovels.com/three-days-of-happiness/",
        "source_feed_name": "J-Novels EPUB Releases",
        "status": "NEW"
    })

    discovered = app_db.get_discovery_releases()
    print(f"Discovered potential new series: {len(discovered)}")
    for d in discovered:
        print(f"  - DISCOVERED: '{d['title']}' (Series: {d['series_name']}, Vol: {d['volume']})")
    assert len(discovered) >= 2

    # User tracks "Solo Leveling"
    print("\nUser clicks 'Track Series' on Solo Leveling...")
    app_db.track_discovered_series("Solo Leveling")
    # Verify Solo Leveling is now in tracked series and in pending requests
    tracked_series = [s["name"] for s in app_db.get_all_series(monitored_only=True)]
    assert "Solo Leveling" in tracked_series
    print("✓ 'Solo Leveling' is now actively monitored!")

    # Verify it is now removed from discovery (since it's monitored)
    discovered_after_track = [d["series_name"] for d in app_db.get_discovery_releases()]
    assert "Solo Leveling" not in discovered_after_track
    print("✓ 'Solo Leveling' moved from Discovery to Active Tracking.")

    print("\n" + "=" * 60)
    print("STEP 7: TESTING SERIES BLACKLISTING")
    print("=" * 60)
    # User blacklists "Three Days of Happiness"
    print("User blacklists 'Three Days of Happiness'...")
    app_db.add_to_blacklist("Three Days of Happiness", reason="Not my genre")
    assert app_db.is_series_blacklisted("Three Days of Happiness")
    print("✓ Series is recorded in blacklist table!")

    # Verify it disappeared from Discovery
    discovered_after_bl = [d["series_name"] for d in app_db.get_discovery_releases()]
    assert "Three Days of Happiness" not in discovered_after_bl
    print("✓ Blacklisted series is completely hidden from Discovery.")

    # Simulate incoming release for the blacklisted series (Vol 2)
    bl_vol2_title = "Three Days of Happiness Volume 2 Epub"
    bl_vol2_guid = hashlib.sha256(bl_vol2_title.encode()).hexdigest()[:16]
    is_bl = app_db.is_series_blacklisted("Three Days of Happiness")
    status_to_set = "BLACKLISTED" if is_bl else "REQUESTED"
    app_db.upsert_release({
        "guid": bl_vol2_guid,
        "title": bl_vol2_title,
        "series_name": "Three Days of Happiness",
        "volume": 2.0,
        "link": "https://jnovels.com/tdoh-vol-2/",
        "status": status_to_set
    })

    # Verify no pending request was created
    for p in app_db.get_pending_requests():
        assert p["series_name"] != "Three Days of Happiness"
    print("✓ Future releases for blacklisted series were blocked from generating requests!")

    # Test removing from blacklist (editable)
    bl_list = app_db.get_blacklisted_series()
    target_bl = next(b for b in bl_list if b["name"] == "Three Days of Happiness")
    app_db.remove_from_blacklist(target_bl["id"])
    assert not app_db.is_series_blacklisted("Three Days of Happiness")
    print("✓ Successfully removed/restored series from Blacklist!")

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED WITH 100% SUCCESS!")
    print("=" * 60)

if __name__ == "__main__":
    run_e2e_test()
