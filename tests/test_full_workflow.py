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
    test_dir = os.path.join(tempfile.gettempdir(), "novelpulse_test_run")
    if os.path.exists(test_dir):
        try:
            shutil.rmtree(test_dir)
        except Exception:
            pass
    os.makedirs(test_dir, exist_ok=True)

    calibre_db_file = os.path.join(test_dir, "calibre", "metadata.db")
    app_db_file = os.path.join(test_dir, "app", "novelpulse.db")

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
    print(f"Auto-populated {synced_count} series into NovelPulse database.")

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
    print("ALL TESTS PASSED WITH 100% SUCCESS!")
    print("=" * 60)

if __name__ == "__main__":
    run_e2e_test()
