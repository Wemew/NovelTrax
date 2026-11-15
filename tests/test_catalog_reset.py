#!/usr/bin/env python3
"""Catalog factory reset: mappings/aliases/caches cleared, settings preserved."""

import os
import shutil
import sqlite3
import sys
import tempfile
import traceback

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
for _path in (BACKEND_DIR, BASE_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.database import Database  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def count(db, table):
    with db.get_connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0]


def test_reset_catalog_and_preserve_configuration():
    print("--> Testing catalog reset clears mappings/aliases but keeps settings...")
    tmp = tempfile.mkdtemp(prefix="noveltrax_reset_")
    try:
        db = Database(os.path.join(tmp, "noveltrax.db"))
        db.set_setting("calibre_path", "/custom/calibre")
        db.ensure_metadata_cache_table()
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO releases (guid, title, series_name, volume, status) "
                "VALUES ('reset-release', 'Seed v01', 'Seed Series', 1, 'REQUESTED');"
            )
            conn.execute(
                "INSERT INTO series_tracking (name, calibre_series_id, display_title) "
                "VALUES ('Seed Series', 42, 'Seed Display');"
            )
            conn.execute(
                "INSERT INTO series_alias_overrides "
                "(source_name, source_norm, target_name, target_norm, origin) "
                "VALUES ('Old Alias', 'old alias', 'Seed Series', 'seed series', 'manual');"
            )
            conn.execute("INSERT INTO series_blacklist (name, reason) VALUES ('Bad Series', 'seed');")
            conn.execute("INSERT INTO release_media_cache (guid, media_type) VALUES ('reset-release', 'novel');")
            conn.execute(
                "INSERT INTO epub_metadata_reviews (file_path, title, review_status) "
                "VALUES ('seed.epub', 'Seed', 'pending');"
            )
            conn.execute("INSERT INTO background_tasks (kind, title, status) VALUES ('seed', 'seed', 'running');")
            conn.execute(
                "INSERT INTO novel_metadata_cache (query_key, title, source) "
                "VALUES ('seed', 'Seed', 'test');"
            )
            conn.commit()
        db.add_series_alias_override("Old Alias", "Seed Series")
        db.add_prioritized_group("Custom Group")
        db.add_to_blacklist("Bad Series", "seed")
        db.log_activity("SEED_EVENT", "seed")

        result = db.reset_all_data(actor="tester")
        check(result["status"] == "success", result)
        check(result["cleared"]["series_alias_overrides"] >= 1, result)
        backup = result.get("backup_path") or ""
        check(backup and os.path.isfile(backup), result)
        check("/backups/" in backup.replace("\\", "/"), backup)

        for table in (
            "releases", "series_tracking", "series_blacklist", "series_alias_overrides",
            "release_media_cache", "novel_metadata_cache", "epub_metadata_reviews",
            "background_tasks",
        ):
            check(count(db, table) == 0, f"{table} was not cleared")
        check(db.get_setting("calibre_path") == "/custom/calibre", "settings were not preserved")
        feeds = db.get_all_feeds()
        check(len(feeds) == 2, feeds)
        check({f["feed_type"] for f in feeds} == {"jnovels", "nyaa"}, feeds)
        groups = {g["name"] for g in db.get_prioritized_groups()}
        check(groups == {"Yen Press", "J-Novel Club", "Seven Seas", "Stick", "One Peace Books"}, groups)
        logs = db.get_activity_logs(limit=10) if hasattr(db, "get_activity_logs") else []
        if not logs:
            with db.get_connection() as conn:
                logs = [dict(r) for r in conn.execute(
                    "SELECT * FROM activity_logs ORDER BY id DESC LIMIT 10;"
                )]
        check(len(logs) == 1 and logs[0]["event_type"] == "SYSTEM_RESET", logs)

        second = db.reset_all_data(actor="tester")
        check(second["status"] == "success", second)
        check(len(db.get_all_feeds()) == 2, "second reset duplicated feeds")
        check(len(db.get_prioritized_groups()) == 5, "second reset duplicated groups")
        print("    [PASSED] mappings, aliases, caches, queues and audit log cleared; settings/stock seeds preserved.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 78)
    print("Running catalog factory-reset suite")
    print("=" * 78)
    try:
        test_reset_catalog_and_preserve_configuration()
    except Exception:
        traceback.print_exc()
        print("CATALOG RESET SUITE FAILED")
        return 1
    print("ALL CATALOG RESET TESTS PASSED!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
