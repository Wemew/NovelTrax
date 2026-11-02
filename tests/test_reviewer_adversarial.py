import sys
import os
import tempfile
import shutil
import threading
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from app.database import Database

temp_dir = tempfile.mkdtemp()
db_path = os.path.join(temp_dir, "test_concurrency.db")

try:
    print("--- Test 1: Fresh DB Migration & Seeding ---")
    db = Database(db_path)
    db.ensure_prioritized_groups_table()
    groups = db.get_prioritized_groups()
    print(f"Fresh DB groups count: {len(groups)}")
    assert len(groups) == 5, f"Expected 5 groups, got {len(groups)}"

    print("\n--- Test 2: Existing DB with case-duplicates and without NOCASE ---")
    legacy_db_path = os.path.join(temp_dir, "legacy.db")
    conn = sqlite3.connect(legacy_db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE prioritized_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            is_vip INTEGER DEFAULT 1,
            notes TEXT DEFAULT '',
            priority INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute("INSERT INTO prioritized_groups (name, priority) VALUES ('Yen Press', 1);")
    cur.execute("INSERT INTO prioritized_groups (name, priority) VALUES ('yen press', 2);")
    conn.commit()
    conn.close()

    legacy_db = Database(legacy_db_path)
    legacy_db.ensure_prioritized_groups_table()
    migrated_groups = legacy_db.get_prioritized_groups()
    print(f"Migrated groups count: {len(migrated_groups)}")
    names = [g["name"] for g in migrated_groups]
    print(f"Migrated group names: {names}")
    assert len([n for n in names if n.lower() == "yen press"]) == 1, "Duplicate yen press not deduplicated!"

    print("\n--- Test 3: Concurrency Stress Test on ensure_prioritized_groups_table ---")
    errors = []
    def worker_thread(tid):
        try:
            for i in range(10):
                db.ensure_prioritized_groups_table()
                db.is_group_prioritized("Yen Press")
                db.add_prioritized_group(f"Group_{tid}_{i}", priority=1)
                db.get_prioritized_groups()
        except Exception as e:
            errors.append(f"Thread {tid} error: {e}")

    threads = [threading.Thread(target=worker_thread, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"Concurrent worker errors: {len(errors)}")
    if errors:
        print(f"Sample errors: {errors[:5]}")
    assert len(errors) == 0, f"Encountered concurrency errors: {errors[:3]}"

    print("\n--- Test 4: update_release_status State Transitions ---")
    test_db = Database(os.path.join(temp_dir, "test_transitions.db"))
    _, rel_id = test_db.upsert_release({
        "guid": "rel_test_1",
        "title": "Test Release Vol 1",
        "series_name": "Test Series",
        "volume": 1.0,
        "status": "IGNORED"
    })
    rel = test_db.get_release_by_id(rel_id)
    assert rel["status"] == "IGNORED" and rel["ignored"] == 1, f"Initial ignored status mismatch: {dict(rel)}"
    print("  Initial: status=IGNORED, ignored=1 -> OK")

    transitions = ["IN_CALIBRE", "REQUESTED", "NEW", "DOWNLOADING", "BLACKLISTED", "IGNORED", "IN_CALIBRE"]
    for target_st in transitions:
        test_db.update_release_status(rel_id, target_st)
        cur_rel = test_db.get_release_by_id(rel_id)
        expected_ignored = 1 if target_st == "IGNORED" else 0
        assert cur_rel["status"] == target_st, f"Status not updated to {target_st}"
        assert cur_rel["ignored"] == expected_ignored, f"Ignored flag mismatch for {target_st}: got {cur_rel['ignored']}, expected {expected_ignored}"
        print(f"  Transition to {target_st}: status={cur_rel['status']}, ignored={cur_rel['ignored']} -> OK")

    print("\n--- Test 5: get_releases_grouped_by_series date_desc chronological ordering ---")
    sort_db = Database(os.path.join(temp_dir, "test_sort.db"))
    sort_db.add_prioritized_group("VIP Group", priority=10)

    # Add series with specific dates
    sort_db.upsert_release({
        "guid": "guid_a", "title": "Series A Vol 1", "series_name": "Series A",
        "volume": 1.0, "status": "NEW", "pub_date": "2026-05-01 12:00:00", "release_group": "Regular Group"
    })
    sort_db.upsert_release({
        "guid": "guid_b", "title": "Series B Vol 1", "series_name": "Series B",
        "volume": 1.0, "status": "REQUESTED", "pub_date": "2026-04-01 12:00:00", "release_group": "VIP Group"
    })
    sort_db.upsert_release({
        "guid": "guid_c", "title": "Series C Vol 1", "series_name": "Series C",
        "volume": 1.0, "status": "REQUESTED", "pub_date": "2026-06-01 12:00:00", "release_group": "Regular Group"
    })
    sort_db.upsert_release({
        "guid": "guid_d", "title": "Series D Vol 1", "series_name": "Series D",
        "volume": 1.0, "status": "REQUESTED", "pub_date": "2026-01-01 12:00:00", "release_group": "VIP Group"
    })

    # Test explicit sort='date_desc'
    grouped_date_desc = sort_db.get_releases_grouped_by_series(sort="date_desc")
    order_date_desc = [g["series_name"] for g in grouped_date_desc]
    dates_date_desc = [g["latest_date"] for g in grouped_date_desc]
    print(f"sort='date_desc' series order: {order_date_desc}")
    print(f"sort='date_desc' dates order: {dates_date_desc}")
    expected_order = ["Series C", "Series A", "Series B", "Series D"]
    assert order_date_desc == expected_order, f"date_desc corrupted! Expected {expected_order}, got {order_date_desc}"

    # Test default sort ('wanted_first')
    grouped_default = sort_db.get_releases_grouped_by_series()
    order_default = [g["series_name"] for g in grouped_default]
    print(f"default (wanted_first) order: {order_default}")
    # In wanted_first, Series B & D are VIP wanted, so they come first
    assert order_default[0] in ("Series B", "Series D") and order_default[1] in ("Series B", "Series D"), "Priority boost failed on default!"
    print("  date_desc and wanted_first separation verified -> OK")

    print("\nALL ADVERSARIAL STRESS PROBES PASSED!")

finally:
    shutil.rmtree(temp_dir, ignore_errors=True)
