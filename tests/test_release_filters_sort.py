#!/usr/bin/env python3
"""Release filters/sort hardening: downloaded scope, wanted-first, hide owned.

Covers live reports:
* the Downloaded tab only showed release-status DOWNLOADED rows instead of all
  series with downloaded volumes/files;
* "Wanted / Missing First" ignored groups whose gap exists only as unowned
  detected volumes (no REQUESTED row);
* fully owned single releases stayed visible (new opt-in hide), while a bundle
  containing at least one missing volume is always shown;
* In Calibre wins over a stale downloaded copy on a volume badge.
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
from test_series_matching import (  # noqa: E402
    check,
    cleanup_temp_databases,
    insert_release,
    insert_series,
    make_db,
)


def check_eq(actual, expected, message):
    check(actual == expected, f"{message}: expected {expected!r}, got {actual!r}")


def temp_downloads():
    path = os.path.join(
        tempfile.gettempdir(), f"nt_filters_{os.getpid()}_{uuid.uuid4().hex[:8]}",
    )
    os.makedirs(path, exist_ok=True)
    return path


def seed_release(db, guid, title, series_name, volumes, status, download_status="IDLE"):
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO releases (guid, title, link, series_name, volume, status, "
            "media_type, pub_date, volumes_json, download_status, is_bundle) "
            "VALUES (?, ?, ?, ?, ?, ?, 'novel', '2026-11-01', ?, ?, ?);",
            (guid, title, f"https://example.test/{guid}", series_name,
             float(volumes[0]), status, json.dumps([float(v) for v in volumes]),
             download_status, 1 if len(volumes) > 1 else 0),
        )
        conn.commit()


def check_grouped_behaviour():
    downloads = temp_downloads()
    db = make_db()
    db.set_setting("downloads_dir", downloads)
    try:
        insert_series(db, "Wanted Gap Series", calibre_id=900, is_monitored=1,
                      book_count=2, latest_index=2.0, owned_volumes=[1.0, 2.0])
        insert_series(db, "Complete Series", calibre_id=901, is_monitored=1,
                      book_count=2, latest_index=2.0, owned_volumes=[1.0, 2.0])
        insert_series(db, "Disk Series", calibre_id=902, is_monitored=1,
                      book_count=1, latest_index=1.0, owned_volumes=[1.0])
        insert_series(db, "Unknown Ownership Series", calibre_id=903,
                      is_monitored=1, owned_volumes=[])
        insert_series(db, "Downloaded Gap Series", calibre_id=904, is_monitored=1,
                      book_count=1, latest_index=1.0, owned_volumes=[1.0])
        insert_series(db, "Owned Feed Saga", calibre_id=905, is_monitored=1,
                      book_count=1, latest_index=1.0, owned_volumes=[1.0])
        insert_series(db, "Orphan File Series", calibre_id=None,
                      is_monitored=0, owned_volumes=[])

        # Wanted gap: volumes 1-2 owned, 3 detected but never requested.
        seed_release(db, "gap-single-1", "Wanted Gap Series Volume 1 Epub",
                     "Wanted Gap Series", [1], "IN_CALIBRE")
        seed_release(db, "gap-single-2", "Wanted Gap Series Volume 2 Epub",
                     "Wanted Gap Series", [2], "IN_CALIBRE")
        seed_release(db, "gap-bundle", "Wanted Gap Series v01-03",
                     "Wanted Gap Series", [1, 2, 3], "IN_CALIBRE")
        # Complete series: every release fully owned.
        seed_release(db, "complete-1", "Complete Series Volume 1 Epub",
                     "Complete Series", [1], "IN_CALIBRE")
        seed_release(db, "complete-2", "Complete Series Volume 2 Epub",
                     "Complete Series", [2], "IN_CALIBRE")
        # Disk series: file exists on disk, release row is IN_CALIBRE.
        seed_release(db, "disk-1", "Disk Series Volume 1 Epub",
                     "Disk Series", [1], "IN_CALIBRE")
        with open(os.path.join(downloads, "Disk Series Vol 1.epub"), "wb") as handle:
            handle.write(b"epub")
        seed_release(db, "unknown-bundle", "Unknown Ownership Series v01-03",
                     "Unknown Ownership Series", [1, 2, 3], "IN_CALIBRE")
        # Missing feed volume already downloaded: belongs in Downloaded, not
        # Releases.  The bundle is repaired to REQUESTED for its missing Vol 2.
        seed_release(db, "downloaded-gap", "Downloaded Gap Series v01-02",
                     "Downloaded Gap Series", [1, 2], "IN_CALIBRE")
        with open(os.path.join(downloads, "Downloaded Gap Series Vol 2.epub"), "wb") as handle:
            handle.write(b"epub")
        # Fully owned feed volume plus a newer downloaded file: extra volumes
        # are display-only and must not hide the complete series.
        seed_release(db, "extra-download-1", "Owned Feed Saga Volume 1 Epub",
                     "Owned Feed Saga", [1], "IN_CALIBRE")
        with open(os.path.join(downloads, "Owned Feed Saga Vol 2.epub"), "wb") as handle:
            handle.write(b"epub")
        # A downloaded file for a volume with no release row must still show
        # under Downloaded; its missing feed volume keeps it in Releases too.
        seed_release(db, "orphan-file-1", "Orphan File Series Volume 1 Epub",
                     "Orphan File Series", [1], "NEW")
        with open(os.path.join(downloads, "Orphan File Series Vol 2.epub"), "wb") as handle:
            handle.write(b"epub")

        # Incomplete bundle repair: v01-03 marked IN_CALIBRE while 3 is missing.
        repaired = db.repair_incomplete_bundle_statuses()
        check_eq(repaired, 2, "only bundles with known incomplete ownership are re-opened")
        with db.get_connection() as conn:
            unknown_row = conn.execute(
                "SELECT status FROM releases WHERE guid = 'unknown-bundle';"
            ).fetchone()
        check_eq(unknown_row["status"], "IN_CALIBRE",
                 "empty/legacy ownership never mass-reopens bundles")
        with db.get_connection() as conn:
            bundle_row = conn.execute(
                "SELECT status, missing_vols_json FROM releases WHERE guid = 'gap-bundle';"
            ).fetchone()
        check_eq(bundle_row["status"], "REQUESTED",
                 "incomplete bundle leaves IN_CALIBRE")
        check_eq(json.loads(bundle_row["missing_vols_json"]), [3.0],
                 "only the missing bundle volume is requested")

        # Wanted/Missing First: the gap series sorts ahead of the complete one.
        groups = db.get_releases_grouped_by_series(sort="wanted_first")
        order = [g["series_name"] for g in groups
                 if g["series_name"] in ("Wanted Gap Series", "Complete Series")]
        check_eq(order, ["Wanted Gap Series", "Complete Series"],
                 "wanted/missing first moves an unowned detected volume ahead")

        # Downloaded scope includes inventory-backed series, not just status rows.
        downloaded_groups = db.get_releases_grouped_by_series(status="DOWNLOADED")
        names = {g["series_name"] for g in downloaded_groups}
        check("Disk Series" in names,
              "downloaded filter includes a series with a downloaded file")
        check("Downloaded Gap Series" in names,
              "downloaded filter includes a series whose missing feed volume is on disk")
        check("Orphan File Series" in names,
              "a downloaded volume newer than the feed still appears under Downloaded")
        check("Complete Series" not in names,
              "downloaded filter excludes series with no downloaded volume")
        orphan = next(g for g in downloaded_groups if g["series_name"] == "Orphan File Series")
        check_eq(orphan["all_volumes"], [1.0, 2.0],
                 "the extra downloaded volume is rendered on the card")
        check_eq(orphan["downloaded_volumes"], [2.0],
                 "the extra file is marked downloaded, not wanted")

        # Fully downloaded gaps are no longer duplicated in Releases.
        releases = db.get_releases_grouped_by_series()
        release_names = {g["series_name"] for g in releases}
        check("Downloaded Gap Series" not in release_names,
              "a group whose missing volumes are all downloaded leaves Releases")
        check("Orphan File Series" in release_names,
              "a group with its feed volume missing stays in Releases")
        check("Owned Feed Saga" in release_names,
              "an owned feed series with only an extra downloaded volume stays in Releases")
        extra = next(g for g in releases if g["series_name"] == "Owned Feed Saga")
        check_eq(extra["feed_volumes"], [1.0],
                 "feed volume set excludes the newer downloaded file")
        check_eq(extra["all_volumes"], [1.0, 2.0],
                 "the newer downloaded volume is still displayed on the card")
        requested = db.get_releases_grouped_by_series(status="REQUESTED")
        check("Downloaded Gap Series" not in {g["series_name"] for g in requested},
              "fully downloaded groups do not linger under Requested")
        check_eq(
            db.count_releases_grouped_by_series(),
            len(releases),
            "grouped count applies the same downloaded-gap exclusion",
        )

        # Hide fully owned releases, keep bundles with any missing volume.
        hidden = db.get_releases_grouped_by_series(hide_owned=True)
        gap = next(g for g in hidden if g["series_name"] == "Wanted Gap Series")
        titles = {r["title"] for r in gap["releases"]}
        check_eq(titles, {"Wanted Gap Series v01-03"},
                 "hide-owned keeps only the bundle with a missing volume")
        check("Complete Series" not in {g["series_name"] for g in hidden},
              "a group with nothing left to show disappears")
    finally:
        cleanup_temp_databases()
        shutil.rmtree(downloads, ignore_errors=True)


def check_badge_precedence():
    from jinja2 import Environment, FileSystemLoader
    from urllib.parse import quote

    env = Environment(loader=FileSystemLoader(
        os.path.join(PROJECT_ROOT, "backend", "app", "templates")
    ))
    env.filters["safe_href"] = lambda value: value
    env.filters["urlencode"] = quote
    template = env.get_template("partials/series_card_chunk.html")
    group = {
        "series_key": "overlap", "series_name": "Overlap Series",
        "stable_name": "Overlap Series", "synopsis": "", "cover_url": None,
        "website_url": "", "has_wanted": False, "in_calibre": True,
        "has_downloaded": True, "has_prioritized": False,
        "calibre_book_count": 1, "calibre_latest_index": 1.0,
        "releases_count": 1, "releases": [],
        "all_volumes": [1.0, 2.0], "owned_volumes": [1.0],
        "downloaded_volumes": [1.0, 2.0],
    }
    html = template.render(grouped_series=[group])

    def badge(label):
        marker = "> Vol %s" % label
        index = html.find(marker)
        check(index >= 0, "volume %s badge rendered" % label)
        return html[max(0, index - 320):index]

    check("vol-spine-owned" in badge("1"),
          "In Calibre wins over a stale downloaded copy for an owned volume")
    check("vol-spine-downloaded" in badge("2"),
          "downloaded-only volume stays blue")


def main():
    check_grouped_behaviour()
    check_badge_precedence()
    print(
        "release filters PASSED: downloaded scope, wanted/missing sort, "
        "hide fully owned, In Calibre badge precedence"
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
