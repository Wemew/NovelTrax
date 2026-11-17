#!/usr/bin/env python3
"""Hardcover cross-check, series review queue, combine/split.

Hermetic: the provider's GraphQL transport is replaced with fixtures; temp DB.
The Rascal fixture mirrors the live Hardcover payload (primary_books_count 16,
books_count 19) and AniList's per-volume entries (volumes 1).
"""

import json
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
for _path in (TESTS_DIR, PROJECT_ROOT, os.path.join(PROJECT_ROOT, "backend")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.providers import hardcover  # noqa: E402
from test_series_matching import (  # noqa: E402
    check,
    cleanup_temp_databases,
    insert_release,
    insert_series,
    make_db,
    new_manager,
    shutdown_manager,
)


def check_eq(actual, expected, message):
    check(actual == expected, f"{message}: expected {expected!r}, got {actual!r}")


RASCAL = {
    "id": 132790,
    "name": "Rascal Does Not Dream (Light Novel)",
    "books_count": 19,
    "primary_books_count": 16,
    "slug": "rascal-does-not-dream-light-novel-132790",
    "author_name": "Hajime Kamoshida",
    "score": 1.0,
}


def check_provider_cache_and_match():
    hardcover.HardcoverProvider.clear_cache()
    hardcover.HardcoverProvider._memory_cache.clear()
    calls = {"n": 0}

    def fake_graphql(query, variables):
        calls["n"] += 1
        if "series_by_pk" in query:
            return {"series_by_pk": {
                "id": 132790, "name": RASCAL["name"], "books_count": 19,
                "primary_books_count": 16, "is_completed": True, "state": "active",
            }}
        return {"search": {"results": {"hits": [{"document": {
            "id": "132790", "name": RASCAL["name"], "books_count": 19,
            "primary_books_count": 16, "slug": RASCAL["slug"],
            "author_name": RASCAL["author_name"],
        }}]}}}

    original = hardcover.HardcoverProvider._graphql
    hardcover.HardcoverProvider._graphql = staticmethod(fake_graphql)
    try:
        first = hardcover.HardcoverProvider.search_series("Rascal Does Not Dream")
        second = hardcover.HardcoverProvider.search_series("Rascal Does Not Dream")
        check_eq(calls["n"], 2, "search + series detail, then cached (2 calls total)")
        check_eq(first.get("primary_books_count"), 16, "Hardcover primary volume count")
        check_eq(second.get("id"), 132790, "cached result reused")
        per_volume = hardcover.HardcoverProvider.search_series(
            "Rascal Does Not Dream of a Sister Home Alone"
        )
        check(per_volume is not None,
              "a per-volume AniList title resolves to the grouped Hardcover series")
        check_eq(per_volume.get("primary_books_count"), 16,
                 "per-volume lookup returns the grouped series size")
        miss = hardcover.HardcoverProvider.search_series("Completely Unrelated Title")
        check_eq(miss, None, "no low-similarity series is accepted")
    finally:
        hardcover.HardcoverProvider._graphql = original
        hardcover.HardcoverProvider.clear_cache()


def check_review_queue_and_combine():
    db = make_db()
    tm = new_manager(db)
    try:
        # Ordinary, alphabetically earlier series must not eat the lookup budget.
        for index in range(6):
            insert_series(
                db, f"A Ordinary Series {index}", calibre_id=None, is_monitored=1,
                book_count=10, latest_index=10.0,
                anilist_meta={"volumes": 10, "anilist_id": 9000 + index},
            )
        base_id = insert_series(
            db, "Rascal Does Not Dream of ...", calibre_id=128, is_monitored=1,
            book_count=15, latest_index=15.0,
            owned_volumes=[float(v) for v in range(1, 16)],
            anilist_meta={"volumes": 1, "anilist_id": 145168},
        )
        light_id = insert_series(db, "Rascal Does Not Dream Light Novels", calibre_id=None,
                                 is_monitored=1, book_count=15, latest_index=15.0)
        sister_id = insert_series(db, "Rascal Does Not Dream of a Sister Home Alone",
                                  calibre_id=None, is_monitored=1, book_count=1,
                                  latest_index=1.0)
        insert_release(db, "rascal-16",
                       "Rascal Does Not Dream of a Dear Friend Volume 1 Epub",
                       "Rascal Does Not Dream of a Sister Home Alone", volume=1.0,
                       status="NEW")
        insert_release(db, "rascal-base",
                       "Rascal Does Not Dream Volume 15 Epub",
                       "Rascal Does Not Dream of ...", volume=15.0, status="IN_CALIBRE")

        calls = {"n": 0, "titles": []}

        def fake_search(title, per_page=5):
            calls["n"] += 1
            calls["titles"].append(title)
            return dict(RASCAL)

        original = hardcover.HardcoverProvider.search_series
        hardcover.HardcoverProvider.search_series = staticmethod(fake_search)
        try:
            result = tm.reconcile_hardcover_series(limit=5)
            check_eq(result.get("queued"), 1, "one disagreement queued for review")
            reviews = db.list_series_metadata_reviews(status="pending")
            check_eq(len(reviews), 1, "review row persisted")
            review = reviews[0]
            check_eq(review["hardcover_volumes"], 16, "Hardcover volume count stored")
            check_eq(review["anilist_volumes"], 1, "AniList volume count stored")
            candidates = json.loads(review["candidates_json"])
            check(len(candidates) >= 1, "candidate series captured")
            check_eq(review["series_id"], base_id,
                     "anchor is the Calibre-linked series")
            # A second pass must not duplicate or re-query pending work.
            before = calls["n"]
            again = tm.reconcile_hardcover_series(limit=5)
            check_eq(again.get("queued"), 0, "already-reviewed series is not re-queued")
            check_eq(len(db.list_series_metadata_reviews(status=None)), 1,
                     "no second overlapping review is queued")
            second_titles = calls["titles"][before:]
            check(not any("Rascal" in title for title in second_titles),
                  "reviewed/candidate series are not looked up again")

            anchor = db.get_series_by_id(review["series_id"])
            keep_id = int(anchor["id"])
            drop_ids = [int(c["id"]) for c in candidates]
            combined = db.force_combine_series(keep_id, drop_ids, actor="user")
            check_eq(combined.get("status"), "success", "combine succeeds")
            check(len(combined.get("merged") or []) >= 1, "at least one series merged")
            check(db.get_series_by_id(drop_ids[0]) is None, "dropped tracker removed")
            with db.get_connection() as conn:
                moved = conn.execute(
                    "SELECT series_name FROM releases WHERE guid = 'rascal-16';"
                ).fetchone()
            check(moved is not None, "candidate release still exists")
            check_eq(moved["series_name"], anchor["name"],
                     "merged release re-keyed to the combined series")
            check_eq(db.resolve_series_metadata_review(review["id"], "combined"), True,
                     "review resolved as combined")
            check_eq(db.list_series_metadata_reviews(status="pending"), [],
                     "no pending review remains")
        finally:
            hardcover.HardcoverProvider.search_series = original
    finally:
        shutdown_manager(tm)
        cleanup_temp_databases()


def check_split_keeps_series():
    db = make_db()
    try:
        insert_series(db, "Split Base", calibre_id=800, is_monitored=1)
        insert_series(db, "Split Candidate", calibre_id=None, is_monitored=1)
        review_id = db.upsert_series_metadata_review(
            1, 10, 20, "Split Base", 1, 16,
            [{"id": 2, "name": "Split Candidate"}], "test",
        )
        check_eq(db.resolve_series_metadata_review(review_id, "split"), True,
                 "split resolves the review")
        check(db.get_series_by_id(1) is not None, "anchor remains after split")
        check(db.get_series_by_id(2) is not None, "candidate remains after split")
        check_eq(db.list_series_metadata_reviews(status="pending"), [],
                 "split review leaves the pending queue")
    finally:
        cleanup_temp_databases()


def main():
    hardcover.HardcoverProvider.set_db(None)
    check_provider_cache_and_match()
    check_review_queue_and_combine()
    check_split_keeps_series()
    print(
        "Hardcover metadata PASSED: cached provider, disagreement queue, "
        "combine/split persistence"
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
