"""DOWNLOADED lifecycle: fetched releases stop being Wanted until Calibre."""

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_BASE = os.path.dirname(_HERE)
for _p in (_BASE, os.path.join(_BASE, "backend"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.database import Database  # noqa: E402
from app.status_precedence import resolve_status  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.init_db()
    return db


def add_release(db, guid, status="REQUESTED"):
    _, release_id = db.upsert_release({
        "guid": guid, "title": f"{guid} Volume 1 Epub",
        "link": f"https://jnovels.com/{guid}/", "series_name": guid, "volume": 1.0,
    })
    db.update_release_status(release_id, status)
    return release_id


def test_mark_downloaded_respects_terminals():
    db = make_db()
    requested = add_release(db, "requested")
    db.mark_release_downloaded(requested)
    check(db.get_release_by_id(requested)["status"] == "DOWNLOADED", db.get_release_by_id(requested))

    in_calibre = add_release(db, "in-calibre", "IN_CALIBRE")
    db.mark_release_downloaded(in_calibre)
    check(db.get_release_by_id(in_calibre)["status"] == "IN_CALIBRE", db.get_release_by_id(in_calibre))

    ignored = add_release(db, "ignored", "IGNORED")
    db.mark_release_downloaded(ignored)
    check(db.get_release_by_id(ignored)["status"] == "IGNORED", db.get_release_by_id(ignored))


def test_backfill_completed_and_precedence():
    db = make_db()
    release_id = add_release(db, "legacy-download")
    db.update_downloader_state(release_id, "COMPLETED", "http")
    check(db.get_release_by_id(release_id)["status"] == "REQUESTED", "still requested before backfill")
    check(db.backfill_completed_download_statuses() == 1, "one repaired row")
    check(db.get_release_by_id(release_id)["status"] == "DOWNLOADED", db.get_release_by_id(release_id))

    check(resolve_status("DOWNLOADED", "REQUESTED") == "DOWNLOADED", "feed cannot revive request")
    check(resolve_status("DOWNLOADED", "NEW") == "DOWNLOADED", "feed cannot downgrade")
    check(resolve_status("DOWNLOADED", "IN_CALIBRE") == "IN_CALIBRE", "Calibre wins")
    check(resolve_status("IN_CALIBRE", "DOWNLOADED") == "IN_CALIBRE", "download cannot downgrade Calibre")


def test_backfill_is_idempotent():
    db = make_db()
    release_id = add_release(db, "done")
    db.update_downloader_state(release_id, "COMPLETED", "http")
    db.mark_release_downloaded(release_id)
    check(db.backfill_completed_download_statuses() == 0, "already downloaded")


def main():
    test_mark_downloaded_respects_terminals()
    test_backfill_completed_and_precedence()
    test_backfill_is_idempotent()
    print("downloaded status suite PASSED")


if __name__ == "__main__":
    main()
