#!/usr/bin/env python3
"""Regression coverage for qBittorrent selective volume filtering.

The manual/API submit applies file priorities immediately, but metadata often
resolves later. These checks cover the persisted wanted-volume handoff and the
30 s retry pass that deselects already-owned files once file names exist.
"""

import asyncio
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
for path in (PROJECT_ROOT, BACKEND_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.database import Database
import app.scheduler as scheduler_module
import app.main as main_module


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def seed_bundle(db, guid, hash_value, wanted_json='[2]'):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO releases
                (guid, title, series_name, volume, is_bundle, volumes_json,
                 missing_vols_json, download_wanted_json, download_status,
                 downloader, torrent_hash, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?);
            """,
            (guid, "Bundle Release", "Test Series", 2, 1, '[1,2]', '[2]',
             wanted_json, "DOWNLOADING", "qbit", hash_value, "REQUESTED"),
        )
        conn.commit()


def test_filter_once_and_retry():
    with tempfile.TemporaryDirectory(prefix="noveltrax_selective_", ignore_cleanup_errors=True) as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        seed_bundle(db, "bundle-1", "a" * 40)
        calls = []

        class FakeQbit:
            enabled = True

            def apply_selective_volume_download(self, torrent_hash, wanted_volumes=None,
                                                series_name=None):
                calls.append((torrent_hash, list(wanted_volumes or []), series_name))
                return {
                    "status": "filtered",
                    "skipped_volumes": [1.0],
                    "kept_volumes": [2.0],
                }

        original = scheduler_module.qbit_client_from_settings
        scheduler_module.qbit_client_from_settings = lambda settings: FakeQbit()
        try:
            manager = scheduler_module.TaskManager(db, "/nonexistent", data_dir=tmp)
            first = manager.check_and_filter_active_downloads()
            second = manager.check_and_filter_active_downloads()
        finally:
            scheduler_module.qbit_client_from_settings = original

        check(first == 1, "first filter pass did not report the filtered bundle")
        check(second == 0, "resolved bundle was filtered a second time")
        check(len(calls) == 1, "qBittorrent was called more than once for one signature")
        check(calls[0][1] == [2.0], "wanted volumes were not passed to the filter")


def test_metadata_pending_retries():
    with tempfile.TemporaryDirectory(prefix="noveltrax_selective_pending_", ignore_cleanup_errors=True) as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        seed_bundle(db, "bundle-2", "b" * 40)
        calls = []

        class PendingQbit:
            enabled = True

            def apply_selective_volume_download(self, torrent_hash, wanted_volumes=None,
                                                series_name=None):
                calls.append(torrent_hash)
                return {"status": "pending_metadata"}

        original = scheduler_module.qbit_client_from_settings
        scheduler_module.qbit_client_from_settings = lambda settings: PendingQbit()
        try:
            manager = scheduler_module.TaskManager(db, "/nonexistent", data_dir=tmp)
            manager.check_and_filter_active_downloads()
            manager.check_and_filter_active_downloads()
        finally:
            scheduler_module.qbit_client_from_settings = original

        check(len(calls) == 2, "pending metadata was not retried on the next pass")


def test_torbox_rows_excluded():
    with tempfile.TemporaryDirectory(prefix="noveltrax_selective_engine_", ignore_cleanup_errors=True) as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        seed_bundle(db, "bundle-3", "c" * 40)
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE releases SET downloader = 'torbox' WHERE guid = 'bundle-3';"
            )
            conn.commit()
        check(db.get_active_qbit_bundle_downloads() == [],
              "TorBox rows must not enter the qBittorrent filter pass")


def test_engine_switch_still_filters_queued_qbit():
    with tempfile.TemporaryDirectory(prefix="noveltrax_selective_switch_", ignore_cleanup_errors=True) as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        seed_bundle(db, "bundle-4", "e" * 40)
        db.set_setting("downloader_engine", "torbox")
        calls = []

        class FakeQbit:
            enabled = True

            def apply_selective_volume_download(self, torrent_hash, wanted_volumes=None,
                                                series_name=None):
                calls.append(torrent_hash)
                return {"status": "filtered", "skipped_volumes": [1.0],
                        "kept_volumes": [2.0]}

        original = scheduler_module.qbit_client_from_settings
        scheduler_module.qbit_client_from_settings = lambda settings: FakeQbit()
        try:
            manager = scheduler_module.TaskManager(db, "/nonexistent", data_dir=tmp)
            filtered = manager.check_and_filter_active_downloads()
        finally:
            scheduler_module.qbit_client_from_settings = original

        check(filtered == 1, "switching engines stranded queued qBittorrent downloads")
        check(len(calls) == 1, "queued qBittorrent download was not filtered")


def test_manual_submit_persists_wanted_and_filters():
    with tempfile.TemporaryDirectory(prefix="noveltrax_selective_api_", ignore_cleanup_errors=True) as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO releases
                    (guid, title, series_name, volume, is_bundle, volumes_json,
                     missing_vols_json, status, download_url, link)
                VALUES (?,?,?,?,?,?,?,?,?,?);
                """,
                ("api-bundle", "API Bundle", "API Series", 3, 1, '[1,2,3]',
                 '[2,3]', "REQUESTED", "magnet:?xt=urn:btih:" + "d" * 40,
                 "https://example.com/api-bundle"),
            )
            conn.commit()
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM releases WHERE guid = 'api-bundle';")
            release_id = cur.fetchone()["id"]

        calls = []

        class FakeQbit:
            enabled = True

            def add_torrent_url(self, url, category=None, savepath=None):
                return True

            @staticmethod
            def extract_torrent_hash(url):
                return "d" * 40

            def apply_selective_volume_download(self, torrent_hash, wanted_volumes=None,
                                                series_name=None):
                calls.append(list(wanted_volumes or []))
                return {
                    "status": "filtered",
                    "skipped_volumes": [1.0],
                    "kept_volumes": [2.0, 3.0],
                }

        original_db = main_module.db
        original_client = main_module.get_qbit_client
        original_engine = main_module._active_downloader_engine
        main_module.db = db
        main_module.get_qbit_client = lambda: FakeQbit()
        main_module._active_downloader_engine = lambda: "qbit"
        try:
            response = asyncio.run(main_module.api_release_download(release_id))
        finally:
            main_module.db = original_db
            main_module.get_qbit_client = original_client
            main_module._active_downloader_engine = original_engine

        check(response.status_code == 200, "manual download route did not return 200")
        row = db.get_release_by_id(release_id)
        check(row["downloader"] == "qbit", "manual submit did not persist the qbit engine")
        check(row["torrent_hash"] == "d" * 40, "manual submit did not persist the torrent hash")
        check(row["download_wanted_json"] == "[2.0, 3.0]",
              "manual submit did not persist wanted volumes: %r" % row["download_wanted_json"])
        check(calls == [[2.0, 3.0]], "manual submit did not apply selective volumes: %r" % calls)


def main():
    print("Selective download filter regression suite")
    test_filter_once_and_retry()
    test_metadata_pending_retries()
    test_torbox_rows_excluded()
    test_engine_switch_still_filters_queued_qbit()
    test_manual_submit_persists_wanted_and_filters()
    print("selective download filter regression suite PASSED")


if __name__ == "__main__":
    main()
