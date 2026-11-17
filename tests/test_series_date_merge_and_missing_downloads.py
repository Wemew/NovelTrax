#!/usr/bin/env python3
"""Ordinal-date series merging + clearing downloads whose files vanished.

1. Live split case: J-Novels numbers "Our Dating Story" volumes as "8th Date
   Volume 8"; those releases must group under the single tracked series (Calibre
   series 320), not one card per volume.
2. A release whose downloaded files were removed and never reached Calibre must
   revert to REQUESTED (tracked, monitored) or NEW (untracked) on the next
   Calibre scan, and its stale engine handle must be reset.

Hermetic: temp database + temp downloads folder, no network.
"""

import json
import os
import shutil
import sys
import tempfile
import uuid

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
for _path in (TESTS_DIR, PROJECT_ROOT, os.path.join(PROJECT_ROOT, "backend")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.database import Database  # noqa: E402
from app.parser import extract_series_and_volume, get_canonical_series_name  # noqa: E402
from test_series_matching import check, cleanup_temp_databases, insert_release, insert_series, make_db  # noqa: E402


def check_eq(actual, expected, message):
    check(actual == expected, f"{message}: expected {expected!r}, got {actual!r}")


def temp_dir(label):
    path = os.path.join(
        tempfile.gettempdir(), f"nt_{label}_{os.getpid()}_{uuid.uuid4().hex[:8]}",
    )
    os.makedirs(path, exist_ok=True)
    return path


def check_ordinal_date_merging():
    parsed = extract_series_and_volume(
        "You Were Experienced, I Was Not: Our Dating Story 8th Date Volume 8 Epub"
    )
    check_eq(parsed.get("series_name"),
             "You Were Experienced, I Was Not: Our Dating Story",
             "parser drops the ordinal-date volume marker from the series name")
    check_eq(parsed.get("volume"), 8.0, "parser keeps the real volume number")
    check_eq(
        get_canonical_series_name(
            "You Were Experienced, I Was Not: Our Dating Story 1st Date"
        ),
        "You Were Experienced, I Was Not: Our Dating Story",
        "canonical name strips the ordinal-date suffix for existing rows",
    )

    db = make_db()
    try:
        insert_series(
            db, "You Were Experienced, I Was Not", calibre_id=320,
            is_monitored=1, book_count=8, latest_index=8.0,
            owned_volumes=[float(v) for v in range(1, 9)],
        )
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO series_alias_overrides (source_name, source_norm, "
                "target_name, target_norm, calibre_series_id, origin, actor) "
                "VALUES ('You Were Experienced, I Was Not: Our Dating Story', "
                "'you were experienced i was not our dating story', "
                "'You Were Experienced, I Was Not', 'you were experienced i was not', "
                "320, 'anilist', 'anilist');"
            )
            conn.commit()
        ordinals = {1: "1st", 2: "2nd", 3: "3rd"}
        for volume in range(1, 10):
            suffix = ordinals.get(volume, f"{volume}th")
            status = "IN_CALIBRE" if volume <= 8 else "REQUESTED"
            insert_release(
                db, f"od-{volume}",
                f"You Were Experienced, I Was Not: Our Dating Story {suffix} Date "
                f"Volume {volume} Epub",
                f"You Were Experienced, I Was Not: Our Dating Story {suffix} Date",
                volume=float(volume), status=status,
            )
        groups = db.get_releases_grouped_by_series()
        matches = [
            group for group in groups
            if "experienced" in (group.get("series_name") or "").lower()
        ]
        check_eq(len(matches), 1, "all ordinal-date volumes group under one series")
        group = matches[0]
        check_eq(group.get("calibre_series_id"), 320, "group links to the Calibre series")
        check_eq(group.get("all_volumes"), [float(v) for v in range(1, 10)],
                 "volumes 1-9 are aggregated")
        check_eq(group.get("owned_volumes"), [float(v) for v in range(1, 9)],
                 "owned volumes 1-8 come from Calibre")
        check_eq(group.get("releases_count"), 9, "all nine releases are in the group")
    finally:
        cleanup_temp_databases()


def insert_download_release(db, guid, series_name, status, download_status, files,
                            volume=1.0, volumes=None):
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO releases (guid, title, link, series_name, volume, status, "
            "media_type, pub_date, download_status, download_files_json, "
            "torbox_progress_json, download_wanted_json, volumes_json) "
            "VALUES (?, ?, ?, ?, ?, ?, 'novel', '2026-11-01', ?, ?, "
            "'{\"pct\": 100}', '[]', ?);",
            (guid, f"{series_name} {guid}", f"https://example.test/{guid}",
             series_name, float(volume), status, download_status,
             json.dumps(files), json.dumps(volumes if volumes is not None else [float(volume)])),
        )
        conn.commit()


def check_missing_download_reconciliation():
    downloads = temp_dir("downloads")
    outside = temp_dir("outside")
    try:
        db = make_db()
        try:
            insert_series(db, "Tracked Series", calibre_id=700, is_monitored=1)
            insert_series(db, "Owned Series", calibre_id=701, is_monitored=1,
                          owned_volumes=[1.0, 2.0])
            with db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO series_alias_overrides (source_name, source_norm, "
                    "target_name, target_norm, calibre_series_id, origin, actor) "
                    "VALUES ('Owned Alias', 'owned alias', 'Owned Series', "
                    "'owned series', 701, 'anilist', 'anilist');"
                )
                conn.commit()
            present = os.path.join(downloads, "Tracked Series Vol 1.epub")
            with open(present, "wb") as handle:
                handle.write(b"epub")
            gone_tracked = os.path.join(downloads, "Tracked Series Vol 2.epub")
            gone_untracked = os.path.join(downloads, "Other Series Vol 1.epub")
            gone_in_calibre = os.path.join(downloads, "Tracked Series Vol 3.epub")
            gone_outside = os.path.join(outside, "Elsewhere.epub")

            insert_download_release(db, "keep-present", "Tracked Series",
                                    "DOWNLOADED", "COMPLETED", [present])
            insert_download_release(db, "clear-tracked", "Tracked Series",
                                    "DOWNLOADED", "COMPLETED", [gone_tracked])
            insert_download_release(db, "clear-untracked", "Other Series",
                                    "DOWNLOADED", "COMPLETED", [gone_untracked])
            insert_download_release(db, "keep-in-calibre", "Tracked Series",
                                    "IN_CALIBRE", "COMPLETED", [gone_in_calibre])
            insert_download_release(db, "keep-outside", "Tracked Series",
                                    "DOWNLOADED", "COMPLETED", [gone_outside])
            gone_owned = os.path.join(downloads, "Owned Series Vol 2.epub")
            gone_owned_alias = os.path.join(downloads, "Owned Alias Vol 1.epub")
            insert_download_release(db, "owned-volume", "Owned Series",
                                    "DOWNLOADED", "COMPLETED", [gone_owned],
                                    volume=2.0)
            insert_download_release(db, "owned-alias", "Owned Alias",
                                    "DOWNLOADED", "COMPLETED", [gone_owned_alias],
                                    volume=1.0)
            partial_files = [
                os.path.join(downloads, "Tracked Series Vol 4.epub"),
                os.path.join(downloads, "Tracked Series Vol 5.epub"),
                os.path.join(downloads, "Tracked Series Vol 6.epub"),
            ]
            for path in partial_files[:2]:
                with open(path, "wb") as handle:
                    handle.write(b"epub")
            insert_download_release(db, "partial-bundle", "Tracked Series",
                                    "DOWNLOADED", "COMPLETED", partial_files,
                                    volume=4.0, volumes=[4.0, 5.0, 6.0])

            check_eq(db.reconcile_missing_downloads(""), 0,
                     "no downloads dir configured => no reconciliation")
            check_eq(db.reconcile_missing_downloads(os.path.join(downloads, "nope")), 0,
                     "missing downloads dir => no reconciliation (never guess)")
            check_eq(db.reconcile_missing_downloads(downloads), 5,
                     "vanished downloads cleared, owned volumes marked In Calibre, "
                     "partial bundle reconciled")

            def row(guid):
                return db.get_connection().execute(
                    "SELECT status, download_status, download_files_json, "
                    "missing_vols_json, torrent_hash, downloaded_at "
                    "FROM releases WHERE guid = ?;",
                    (guid,),
                ).fetchone()

            tracked = row("clear-tracked")
            check_eq(tracked["status"], "REQUESTED",
                     "tracked+monitored series returns to Wanted")
            check_eq(tracked["download_status"], "IDLE", "engine state reset")
            check_eq(json.loads(tracked["download_files_json"]), [],
                     "stale file list cleared")
            check_eq(tracked["downloaded_at"], None, "downloaded_at cleared")

            untracked = row("clear-untracked")
            check_eq(untracked["status"], "NEW",
                     "untracked series returns to Discovery")

            owned = row("owned-volume")
            check_eq(owned["status"], "IN_CALIBRE",
                     "owned volume is marked In Calibre, never re-wanted")
            check_eq(json.loads(owned["download_files_json"]), [],
                     "owned volume's stale file list cleared")
            check_eq(row("owned-alias")["status"], "IN_CALIBRE",
                     "alias-spelled release resolves Calibre ownership too")

            partial = row("partial-bundle")
            check_eq(partial["status"], "REQUESTED",
                     "partially lost bundle stays wanted")
            check_eq(partial["download_status"], "PARTIAL",
                     "partially lost bundle reports PARTIAL")
            check_eq(json.loads(partial["missing_vols_json"]), [6.0],
                     "only the removed volume is wanted again")
            remaining_files = json.loads(partial["download_files_json"])
            check_eq(len(remaining_files), 2, "existing files are kept in the record")
            check(all(os.path.isfile(path) for path in remaining_files),
                  "kept files still exist")

            kept = row("keep-present")
            check_eq(kept["status"], "DOWNLOADED",
                     "existing file keeps the release downloaded")
            check_eq(row("keep-in-calibre")["status"], "IN_CALIBRE",
                     "IN_CALIBRE is never reverted")
            check_eq(row("keep-outside")["status"], "DOWNLOADED",
                     "paths outside downloads_dir never trigger a clear")

            events = [log["event_type"] for log in db.get_logs(limit=20)]
            check("RELEASE_DOWNLOAD_MISSING" in events,
                  "clearing is audited")
            check(os.path.isfile(present), "kept file is not deleted")
        finally:
            cleanup_temp_databases()
    finally:
        shutil.rmtree(downloads, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


def main():
    check_ordinal_date_merging()
    check_missing_download_reconciliation()
    print(
        "date-suffix merge + missing-download reset PASSED: one series card for "
        "Nth Date volumes, vanished downloads return to Wanted/Discovery"
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
