#!/usr/bin/env python3
"""Exact-volume TorBox fetches and the flat downloads directory.

Covers the hardened Nyaa/debrid contract:

* only volumes missing from Calibre are fetched from a bundle torrent;
* wanted volumes absent from the torrent stay wanted (PARTIAL, not DOWNLOADED);
* a flat downloads folder is used, collisions never overwrite unrelated files,
  and a failed fetch only removes files that fetch wrote.

Hermetic: ManagerEnv fake downloader/transport, temp DB + downloads dir.
"""

import json
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
for _path in (TESTS_DIR, PROJECT_ROOT, os.path.join(PROJECT_ROOT, "backend")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_torbox_manager import ManagerEnv, check, check_equal as check_eq
from app.downloader.torbox import TorboxDownloader


def poll_ready(env, release, files):
    env.manager.submit_release(release, source="manual")
    env.fake.submitted = []
    # submit_release persists the handle; the fake status key is its torrent id.
    handle = env.manager._handle_for_release(release["id"])
    key = (handle or {}).get("torrent_id")
    env.fake.statuses[key] = {
        "state": "ready",
        "raw_state": "completed",
        "download_present": True,
        "download_finished": True,
        "progress": 100,
        "size": sum(int(item.get("size") or 0) for item in files),
        "files": files,
        "cached": False,
    }
    return env.manager.poll_once(async_fetch=False)


def test_wanted_volumes_come_from_calibre_ownership():
    env = ManagerEnv()
    try:
        with env.db.get_connection() as conn:
            conn.execute(
                "INSERT INTO series_tracking (name, calibre_series_id, is_monitored, "
                "book_count, latest_index, owned_volumes_json) "
                "VALUES ('Overland', 900, 1, 3, 3.0, '[1,2,3]');"
            )
            conn.execute(
                "INSERT INTO series_alias_overrides (source_name, source_norm, "
                "target_name, target_norm, calibre_series_id, origin, actor) "
                "VALUES ('Overland Bundle', 'overland bundle', 'Overland', 'overland', "
                "900, 'anilist', 'anilist');"
            )
            conn.commit()

        release = env.add_release(
            "Overland v01-04 [EPUB]", volume=None,
            volumes_json="[1,2,3,4]", missing_vols_json="[]",
        )
        check_eq(env.manager._resolve_wanted_volumes(release), [4.0],
                 "bundle minus Calibre-owned volumes yields the exact missing volume")

        alias_release = env.add_release(
            "Overland Bundle v01-04", series_name="Overland Bundle", volume=None,
            volumes_json="[1,2,3,4]", missing_vols_json="[]",
        )
        check_eq(env.manager._resolve_wanted_volumes(alias_release), [4.0],
                 "alias spellings resolve the same Calibre ownership")

        partial = dict(release)
        partial["download_wanted_json"] = "[]"
        partial["missing_vols_json"] = "[9]"
        check_eq(env.manager._resolve_wanted_volumes(partial), [9.0],
                 "a partial fetch targets its stored leftovers")
    finally:
        env.cleanup()


def test_unknown_wanted_volumes_never_fetch_whole_torrent():
    env = ManagerEnv()
    try:
        release = env.add_release("Mystery Bundle [EPUB]", volume=None)
        outcome = poll_ready(env, release, [
            {"id": 1, "name": "Mystery v01.epub", "size": 10},
            {"id": 2, "name": "Mystery v02.epub", "size": 10},
        ])
        check_eq(outcome.get("completed"), 0, "unknown wanted volumes are not fetched")
        row = env.db.get_release_by_id(release["id"])
        check_eq(row.get("download_status"), "IDLE", "skipped release is reset to IDLE")
        check_eq(env.fake.fetched, [], "no file download was attempted")
        files = json.loads(row.get("download_files_json") or "[]")
        check_eq(files, [], "no files recorded")
    finally:
        env.cleanup()


def test_standalone_single_file_is_fetched():
    """An unnumbered/standalone release is exactly its one novel file."""
    env = ManagerEnv()
    try:
        release = env.add_release("Standalone Novel [EPUB]", volume=None)
        outcome = poll_ready(env, release, [
            {"id": 1, "name": "Standalone Novel.epub", "size": 8},
        ])
        check_eq(outcome.get("completed"), 1, "standalone single-file torrent is fetched")
        row = env.db.get_release_by_id(release["id"])
        check_eq(row.get("download_status"), "COMPLETED", "standalone release is completed")
        saved = json.loads(row.get("download_files_json") or "[]")
        check_eq(saved, [os.path.join(env.downloads, "Standalone Novel.epub")],
                 "standalone file lands flat in downloads_dir")
    finally:
        env.cleanup()


def test_partial_selection_keeps_missing_volumes_wanted():
    env = ManagerEnv()
    try:
        release = env.add_release(
            "Overland v01-03 (Bundle)", volume=None, is_bundle=1,
            volumes_json="[1,2,3]", missing_vols_json="[2,3]",
        )
        outcome = poll_ready(env, release, [
            {"id": 2, "name": "Overland v02.epub", "size": 10},
        ])
        check_eq(outcome.get("completed"), 1, "available part of the selection fetched")
        row = env.db.get_release_by_id(release["id"])
        check_eq(row.get("download_status"), "PARTIAL",
                 "partial fetch keeps a non-terminal download state")
        check_eq(row.get("status"), "REQUESTED",
                 "release remains wanted for the volumes not in the torrent")
        check_eq(json.loads(row.get("missing_vols_json") or "[]"), [3.0],
                 "only the absent wanted volume stays missing")
        check_eq(json.loads(row.get("download_wanted_json") or "[]"), [],
                 "the fetched volume is not re-requested")
        saved = json.loads(row.get("download_files_json") or "[]")
        check_eq(len(saved), 1, "one exact file fetched")
        check_eq(os.path.dirname(saved[0]), env.downloads,
                 "file lands directly in the flat downloads folder")
        # Startup repair must not suppress the leftovers.
        env.db.backfill_completed_download_statuses()
        check_eq(env.db.get_release_by_id(release["id"]).get("status"), "REQUESTED",
                 "startup backfill leaves a partial row REQUESTED")
    finally:
        env.cleanup()


def test_flat_folder_collision_is_disambiguated():
    env = ManagerEnv()
    try:
        os.makedirs(env.downloads, exist_ok=True)
        existing = os.path.join(env.downloads, "Vol 1.epub")
        with open(existing, "wb") as handle:
            handle.write(b"existing-download")
        release = env.add_release(
            "New Series v01 [EPUB]", volume=None,
            volumes_json="[1]", missing_vols_json="[1]",
        )
        poll_ready(env, release, [{"id": 1, "name": "Vol 1.epub", "size": 8}])
        with open(existing, "rb") as handle:
            check_eq(handle.read(), b"existing-download",
                     "an existing unrelated download is never overwritten")
        prefixed = os.path.join(env.downloads, "New Series v01 [EPUB] - Vol 1.epub")
        check(os.path.isfile(prefixed), "colliding file is stored with the release title")
        saved = json.loads(env.db.get_release_by_id(release["id"]).get("download_files_json") or "[]")
        check_eq(saved, [prefixed], "persisted path points at the flat, disambiguated file")
        check_eq([name for name in os.listdir(env.downloads) if os.path.isdir(os.path.join(env.downloads, name))],
                 [], "no per-release subdirectories are created")
    finally:
        env.cleanup()


def test_cleanup_only_removes_this_fetch_files():
    import tempfile
    root = os.path.join(
        tempfile.gettempdir(), "nt_cleanup_%d_%s" % (os.getpid(), os.urandom(4).hex()),
    )
    os.makedirs(root, exist_ok=True)
    try:
        keep = os.path.join(root, "keep.epub")
        own = os.path.join(root, "own.epub")
        part = own + ".part"
        for path, body in ((keep, b"keep"), (own, b"own"), (part, b"part")):
            with open(path, "wb") as handle:
                handle.write(body)
        downloader = object.__new__(TorboxDownloader)
        downloader._cleanup_fetch_outputs(
            root, [{"path": own}], [{"name": "own.epub"}],
        )
        check(os.path.isfile(keep), "cleanup keeps unrelated files in the shared folder")
        check(not os.path.exists(own), "cleanup removes the failed fetch's file")
        check(not os.path.exists(part), "cleanup removes the failed fetch's .part")
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def main():
    test_wanted_volumes_come_from_calibre_ownership()
    test_unknown_wanted_volumes_never_fetch_whole_torrent()
    test_standalone_single_file_is_fetched()
    test_partial_selection_keeps_missing_volumes_wanted()
    test_flat_folder_collision_is_disambiguated()
    test_cleanup_only_removes_this_fetch_files()
    print(
        "exact-volume TorBox PASSED: missing-only selection, partial leftovers stay "
        "wanted, flat collision-safe downloads, scoped cleanup"
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
