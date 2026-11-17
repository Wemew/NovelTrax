#!/usr/bin/env python3
"""Downloaded-volume state on series cards.

A volume whose file is already in the downloads folder must render as a blue
"Downloaded" badge instead of amber "Wanted", even when no release row/torrent
handle reports it (e.g. "Days with My Stepsister, Vol. 8.epub" fetched earlier).
Volumes that are both downloaded and in Calibre are labelled "Downloaded" per
the card contract: downloaded state takes precedence.

Hermetic: temp DB, temp downloads folder, no network, explicit ``check()`` only.
"""

import json
import os
import shutil
import sys
import tempfile
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
for _path in (BASE_DIR, BACKEND_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.database import Database  # noqa: E402
from app.download_inventory import scan_downloaded_volumes  # noqa: E402
from app.parser import normalize_text  # noqa: E402

_TEMP_PATHS = []


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def check_eq(actual, expected, message):
    check(actual == expected, f"{message}: expected {expected!r}, got {actual!r}")


def temp_path(suffix):
    path = os.path.join(
        tempfile.gettempdir(),
        f"noveltrax_downloaded_vols_{os.getpid()}_{uuid.uuid4().hex[:10]}{suffix}",
    )
    _TEMP_PATHS.append(path)
    return path


def make_downloads_dir():
    folder = temp_path("_downloads")
    os.makedirs(folder, exist_ok=True)
    for name in (
        "Days with My Stepsister, Vol. 8.epub",
        "Days with My Stepsister, Vol. 9.azw3",
        "notes.txt",
    ):
        with open(os.path.join(folder, name), "wb") as handle:
            handle.write(b"x")
    torrent = os.path.join(folder, "Overlord")
    os.makedirs(torrent, exist_ok=True)
    with open(os.path.join(torrent, "Volume 4.epub"), "wb") as handle:
        handle.write(b"x")
    with open(os.path.join(torrent, "cover.jpg"), "wb") as handle:
        handle.write(b"x")
    return folder


def make_db():
    path = temp_path(".db")
    _TEMP_PATHS.extend([path + "-wal", path + "-shm"])
    db = Database(path)
    with db.get_connection() as conn:
        conn.execute("DELETE FROM feeds;")
        conn.commit()
    db.set_setting("filter_non_novels", "false")
    return db


def seed(db, series_name, volume, status="NEW", guid=None, volumes=None,
         download_status="IDLE", media_type="novel"):
    payload = volumes if volumes is not None else [volume]
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO releases (guid, title, link, series_name, volume, status, "
            "media_type, pub_date, volumes_json, missing_vols_json, download_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-11-01', ?, '[]', ?);",
            (
                guid or uuid.uuid4().hex[:12],
                f"{series_name} Vol {volume}",
                "https://example.test/release",
                series_name,
                volume,
                status,
                media_type,
                json.dumps(payload),
                download_status,
            ),
        )
        conn.commit()


def check_scan():
    folder = make_downloads_dir()
    volumes = scan_downloaded_volumes(folder)
    key = normalize_text("Days with My Stepsister")
    check_eq(volumes.get(key), [8.0, 9.0], "scan: parsed filename series/volumes")
    check_eq(volumes.get(normalize_text("Overlord")), [4.0],
             "scan: bare 'Volume 4.epub' falls back to the torrent folder name")
    check_eq(scan_downloaded_volumes(os.path.join(folder, "missing")), {},
             "scan: missing folder yields no volumes")


def check_group_payload():
    folder = make_downloads_dir()
    db = make_db()
    db.set_setting("downloads_dir", folder)
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO series_tracking (name, calibre_series_id, is_monitored, "
            "book_count, latest_index, owned_volumes_json) "
            "VALUES ('Days with My Stepsister', 900, 1, 7, 7.0, ?);",
            (json.dumps([float(v) for v in range(1, 8)]),),
        )
        conn.commit()
    seed(db, "Days with My Stepsister", 1.0, status="IN_CALIBRE",
         volumes=[float(v) for v in range(1, 9)], guid="dl-bundle")
    seed(db, "Days with My Stepsister", 10.0, status="DOWNLOADED",
         volumes=[10.0], guid="dl-ten", download_status="COMPLETED")

    groups = db.get_releases_grouped_by_series()
    group = next((g for g in groups if "stepsister" in (g.get("stable_name") or "").lower()), None)
    check(group is not None, "grouping: series card missing")
    check_eq(group.get("downloaded_volumes"), [8.0, 9.0, 10.0],
             "grouping: filesystem volumes 8/9 + completed download volume 10")
    check_eq(group.get("owned_volumes"), [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0],
             "grouping: Calibre-owned volumes unchanged")


def check_template_rendering():
    from urllib.parse import quote
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(
        os.path.join(BACKEND_DIR, "app", "templates")
    ))
    env.filters["safe_href"] = lambda value: value
    env.filters["urlencode"] = quote
    template = env.get_template("partials/series_card_chunk.html")
    group = {
        "series_key": "days with my stepsister",
        "series_name": "Days with My Stepsister",
        "stable_name": "Days with My Stepsister",
        "synopsis": "",
        "cover_url": None,
        "website_url": "",
        "has_wanted": True,
        "in_calibre": True,
        "has_downloaded": True,
        "has_prioritized": False,
        "calibre_book_count": 7,
        "calibre_latest_index": 7.0,
        "releases_count": 1,
        "releases": [],
        "all_volumes": [7.0, 8.0, 9.0],
        "owned_volumes": [7.0],
        "downloaded_volumes": [8.0],
    }
    html = template.render(grouped_series=[group])
    check("vol-spine-downloaded" in html, "render: downloaded badge class present")
    check('title="Volume 8 is downloaded' in html,
          "render: volume 8 labelled downloaded, not wanted/in-calibre")
    check(html.count("vol-spine-wanted") == 1,
          "render: only the genuinely missing volume stays wanted")
    check("> Downloaded</span>" in html, "render: legend names the Downloaded state")
    check("vol-spine-downloaded" in html and "Vol 8" in html,
          "render: badge shows the volume number")


def cleanup():
    for path in _TEMP_PATHS:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def main():
    try:
        check_scan()
        check_group_payload()
        check_template_rendering()
    finally:
        cleanup()
    print(
        "downloaded volumes PASSED: downloads-folder scan, group payload "
        "(filesystem + completed downloads), blue 'Downloaded' badge precedence"
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
