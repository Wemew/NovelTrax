#!/usr/bin/env python3
"""Per-series backfill includes the J-Novels targeted search.

Live case: the Nyaa bundle "Days with My Stepsister v01-08" is tracked, Calibre
owns 1-7, and the site has a standalone "Volume 8 Epub" post.  The global
J-Novels ``?s=epub`` pages and the feed's catch-up window never list that post,
and the earlier global backfill rejected it while the series was unmonitored;
only a targeted ``?s=<series>`` search finds it.  The HTTP routes now pass
``include_jnovels=True``; plain callers stay hermetic by default.
"""

import os
import sys
import tempfile
import uuid

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
for _path in (TESTS_DIR, PROJECT_ROOT, os.path.join(PROJECT_ROOT, "backend")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_series_matching import (  # noqa: E402
    check,
    cleanup_temp_databases,
    insert_series,
    make_db,
    new_manager,
    shutdown_manager,
)
from app.providers.base import ReleaseItem  # noqa: E402


def check_eq(actual, expected, message):
    check(actual == expected, f"{message}: expected {expected!r}, got {actual!r}")


def make_item(guid, title, series_name, volume):
    return ReleaseItem(
        guid=guid,
        title=title,
        series_name=series_name,
        volume=volume,
        link=f"https://jnovels.com/{guid}/",
        pub_date="2026-11-10T07:44:28+00:00",
        source_feed_name="J-Novels",
    )


def check_targeted_jnovels_backfill():
    db = make_db()
    tm = new_manager(db)
    empty_calibre = os.path.join(tempfile.gettempdir(), "nt_no_calibre_" + uuid.uuid4().hex[:8])
    os.makedirs(empty_calibre, exist_ok=True)
    try:
        tm.calibre_path = empty_calibre
        db.set_setting("calibre_path", empty_calibre)
        insert_series(
            db, "Days with My Stepsister", calibre_id=900, is_monitored=1,
            book_count=7, latest_index=7.0,
            owned_volumes=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0],
        )
        vol8 = make_item(
            "days-with-my-stepsister-volume-8-epub",
            "Days with My Stepsister Volume 8 Epub",
            "Days with My Stepsister",
            8.0,
        )
        calls = {"jnovels": [], "nyaa": 0}

        import app.providers.jnovels as jnovels_mod
        import app.providers.nyaa as nyaa_mod

        original_jn = jnovels_mod.JNovelsProvider.search_series
        original_nyaa = nyaa_mod.NyaaNovelProvider.search_series
        try:
            def fake_jnovels(self, series_name, max_pages=3, stop_check=None):
                calls["jnovels"].append((series_name, max_pages))
                return [vol8]

            def fake_nyaa(self, series_name, max_pages=3, stop_check=None):
                calls["nyaa"] += 1
                return []

            jnovels_mod.JNovelsProvider.search_series = fake_jnovels
            nyaa_mod.NyaaNovelProvider.search_series = fake_nyaa

            result = tm.backfill_series(
                "Days with My Stepsister", max_pages=1, include_jnovels=True,
            )
            check_eq(result.get("status"), "success", "targeted backfill status")
            check_eq(result.get("releases_found"), 1, "one J-Novels release found")
            check_eq(result.get("requested"), 1, "missing volume 8 is requested")
            check_eq(calls["jnovels"], [("Days with My Stepsister", 1)],
                     "J-Novels search is queried for the series")
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT title, series_name, volume, status FROM releases WHERE guid = ?;",
                    (vol8.guid,),
                ).fetchone()
            check(row is not None, "volume 8 release persisted")
            check_eq(row["title"], "Days with My Stepsister Volume 8 Epub",
                     "volume 8 title stored")
            check_eq(float(row["volume"]), 8.0, "volume 8 parsed")
            check_eq(row["status"], "REQUESTED",
                     "missing volume 8 is wanted, not IN_CALIBRE")
        finally:
            jnovels_mod.JNovelsProvider.search_series = original_jn
            nyaa_mod.NyaaNovelProvider.search_series = original_nyaa
    finally:
        shutdown_manager(tm)
        cleanup_temp_databases()
        import shutil
        shutil.rmtree(empty_calibre, ignore_errors=True)


def check_default_stays_hermetic():
    db = make_db()
    tm = new_manager(db)
    try:
        insert_series(db, "Hermetic Series", calibre_id=901, is_monitored=1,
                      book_count=1, latest_index=1.0, owned_volumes=[1.0])
        called = {"jnovels": 0}
        import app.providers.jnovels as jnovels_mod
        import app.providers.nyaa as nyaa_mod

        original_jn = jnovels_mod.JNovelsProvider.search_series
        original_nyaa = nyaa_mod.NyaaNovelProvider.search_series
        try:
            def fake_jnovels(self, *args, **kwargs):
                called["jnovels"] += 1
                raise AssertionError("J-Novels search must be opt-in")

            nyaa_mod.NyaaNovelProvider.search_series = (
                lambda self, *a, **kw: []
            )
            jnovels_mod.JNovelsProvider.search_series = fake_jnovels
            result = tm.backfill_series("Hermetic Series", max_pages=1)
            check_eq(result.get("status"), "success", "default backfill status")
            check_eq(called["jnovels"], 0,
                     "plain backfill_series callers keep the old Nyaa-only path")
        finally:
            jnovels_mod.JNovelsProvider.search_series = original_jn
            nyaa_mod.NyaaNovelProvider.search_series = original_nyaa
    finally:
        shutdown_manager(tm)
        cleanup_temp_databases()


def main():
    check_targeted_jnovels_backfill()
    check_default_stays_hermetic()
    print(
        "series backfill J-Novels PASSED: targeted ?s=<series> search finds the "
        "missing volume-8 post (REQUESTED), default callers stay hermetic"
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
