"""Scheduler tests for the lock-protected Calibre library snapshot cache
(CONTRACT v5 §6).

Hermetic: temp Database + TaskManager, fake ``CalibreScanner`` with read
counters, monkeypatched metadata provider (sync enrichment). Explicit
``check()`` only; no bare asserts, no network, no mkdtemp.
"""

import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import test_series_matching as matching
from test_series_matching import (
    BOOK_ID_327_VOL1,
    TRAPPED_CALIBRE,
    FakeScanner,
    MetadataFake,
    check,
    cleanup_temp_databases,
    insert_release,
    insert_series,
    make_db,
    new_manager,
    patched_metadata,
    patched_scanner,
    section,
    shutdown_manager,
)

import app.scheduler as scheduler_module


# ---------------------------------------------------------------------------
# 1. TTL cache: one scanner read within the TTL + defensive copies
# ---------------------------------------------------------------------------
def test_ttl_single_read_and_copies():
    section("1. snapshot cache: two reads within the TTL -> one scanner read")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    try:
        with patched_scanner():
            series1, books1 = tm._calibre_library_snapshot()
            series2, books2 = tm._calibre_library_snapshot()
            series3 = tm._calibre_series_snapshot()

        check(FakeScanner.series_calls == 1,
              f"one Calibre series read within the TTL: {FakeScanner.series_calls}")
        check(FakeScanner.books_calls == 1,
              f"one Calibre books read within the TTL: {FakeScanner.books_calls}")
        check(len(series1) == 2 and len(series2) == 2,
              f"both snapshots carry the library: {len(series1)}/{len(series2)}")
        check(series1 == series2, "cached snapshots are equal")
        check(series3 == series1, "series snapshot reuses the library cache")
        check(series1 is not series2, "snapshots are copies, not the same list")
        check(series1[0] is not series2[0], "rows are copied")
        check(series1[0] is not tm._calibre_snapshot_cache["series"][0],
              "callers never receive the cached row objects")
        check(tm._calibre_snapshot_cache["ts"] > 0, "cache timestamp recorded")

        # Mutating a returned snapshot must never corrupt the cache.
        series1[0]["series_name"] = "MUTATED"
        series1.append({"series_id": 999, "series_name": "Injected"})
        series4, books4 = tm._calibre_library_snapshot()
        check(series4[0]["series_name"] == TRAPPED_CALIBRE,
              f"cache not corrupted by caller mutation: {series4[0]}")
        check(len(series4) == 2, f"cache list not mutated: {len(series4)}")
        check(len(books4) == len(books2), "books copies consistent")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 2. TTL expiry and explicit invalidation refetch
# ---------------------------------------------------------------------------
def test_expiry_and_invalidation_refetch():
    section("2. snapshot cache: TTL expiry and invalidation refetch")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    try:
        with patched_scanner():
            tm._calibre_library_snapshot()
            check(FakeScanner.series_calls == 1, "initial read")

            tm._calibre_snapshot_cache["ts"] = (
                time.time() - scheduler_module.CALIBRE_SNAPSHOT_TTL_SECONDS - 1.0
            )
            tm._calibre_library_snapshot()
            check(FakeScanner.series_calls == 2,
                  f"expired snapshot refetches: {FakeScanner.series_calls}")

            tm._invalidate_calibre_snapshot_cache()
            check(tm._calibre_snapshot_cache["series"] == [],
                  "invalidation clears the cached series")
            check(float(tm._calibre_snapshot_cache["ts"]) == 0.0,
                  "invalidation resets the timestamp")
            tm._calibre_library_snapshot()
            check(FakeScanner.series_calls == 3,
                  f"invalidated snapshot refetches: {FakeScanner.series_calls}")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 3. Unavailable/torn scanner fallback is never cached as live
# ---------------------------------------------------------------------------
def test_unavailable_scanner_fallback_not_cached():
    section("3. snapshot cache: unavailable/torn scanner fallback is not cached")
    db = make_db()
    insert_series(db, TRAPPED_CALIBRE, calibre_id=327, is_monitored=1,
                  owned_volumes=[1.0, 2.0], book_count=2, latest_index=2.0)
    tm = new_manager(db)
    try:
        # Unavailable scanner -> tracked-row fallback (series + synthesized books).
        FakeScanner.reset(available=False)
        with patched_scanner():
            series, books = tm._calibre_library_snapshot()
            series_again, books_again = tm._calibre_library_snapshot()
        check(len(series) == 1 and series[0]["series_id"] == 327,
              f"fallback series from tracked calibre rows: {series}")
        check(len(books) == 2, f"fallback books are synthesized: {books}")
        check(series == series_again and books == books_again,
              "fallback remains stable across calls")
        check(tm._calibre_snapshot_cache["series"] == [],
              f"an unavailable read is never cached as live: {tm._calibre_snapshot_cache}")
        check(FakeScanner.series_calls == 0,
              "unavailable scanner never reached get_all_series")

        # Torn read (scanner claims available but raises) must not poison the cache.
        FakeScanner.library()
        original = FakeScanner.get_all_series

        def _explode(self):
            raise RuntimeError("simulated torn Calibre read")

        FakeScanner.get_all_series = _explode
        try:
            with patched_scanner():
                torn_series, torn_books = tm._calibre_library_snapshot()
        finally:
            FakeScanner.get_all_series = original
        check(len(torn_series) == 1 and torn_series[0]["series_id"] == 327,
              f"torn read falls back to tracked rows: {torn_series}")
        check(tm._calibre_snapshot_cache["series"] == [],
              "torn read is not cached as live data")

        # Recovery: a healthy scanner fills the cache again.
        FakeScanner.library()
        with patched_scanner():
            recovered_series, recovered_books = tm._calibre_library_snapshot()
        check(len(recovered_series) == 2,
              f"healthy scanner recovers the live snapshot: {recovered_series}")
        check(FakeScanner.series_calls == 1, "one live read after recovery")
        check(len(tm._calibre_snapshot_cache["series"]) == 2,
              "live snapshot cached after recovery")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 4. Re-bind status/reconcile path reuses one snapshot
# ---------------------------------------------------------------------------
def test_rebind_status_calls_reuse_snapshot():
    section("4. re-bind status + reconcile reuse one Calibre snapshot")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    try:
        release = {
            "title": f"{TRAPPED_CALIBRE} Vol 1 (Novel)",
            "series_name": TRAPPED_CALIBRE,
            "volume": 1.0,
        }
        with patched_scanner():
            first = tm._calibre_release_status(release, TRAPPED_CALIBRE, 327)
            second = tm._calibre_release_status(release, TRAPPED_CALIBRE, 327)
            reconciled = tm.reconcile_series_releases(TRAPPED_CALIBRE)

        check(first == second,
              f"two rebind status computations agree: {first!r} vs {second!r}")
        check(first == ("IN_CALIBRE", BOOK_ID_327_VOL1),
              f"status recomputed from the real library: {first!r}")
        check(FakeScanner.series_calls == 1,
              f"one series read serves both status calls + reconcile: {FakeScanner.series_calls}")
        check(FakeScanner.books_calls == 1,
              f"one books read serves both status calls + reconcile: {FakeScanner.books_calls}")
        check(isinstance(reconciled, dict) and "checked" in reconciled,
              f"reconcile ran on the cached snapshot: {reconciled}")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 5. sync_calibre / recheck_series_calibre invalidate for a fresh library read
# ---------------------------------------------------------------------------
def test_sync_and_recheck_invalidate_cache():
    section("5. sync_calibre and recheck_series_calibre invalidate the snapshot")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    metadata = MetadataFake()
    try:
        insert_series(db, TRAPPED_CALIBRE, calibre_id=327, is_monitored=1,
                      owned_volumes=[1.0], book_count=3, latest_index=3.0)
        insert_release(db, "snapshot-cache-rel", f"{TRAPPED_CALIBRE} Vol 1 (Novel)",
                       TRAPPED_CALIBRE, volume=1.0, status="REQUESTED")
        metadata.reset(result=None)
        with patched_scanner(), patched_metadata(metadata):
            # Prime the cache, then a full sync must read the fresh library.
            tm._calibre_library_snapshot()
            check(FakeScanner.series_calls == 1, "cache primed before sync")
            sync_result = tm.sync_calibre()
            check(sync_result["status"] == "success", f"sync status: {sync_result}")
            check(FakeScanner.series_calls == 2,
                  f"sync invalidated and rescanned: {FakeScanner.series_calls}")

            calls_after_sync = FakeScanner.series_calls
            tm._calibre_library_snapshot()
            check(FakeScanner.series_calls == calls_after_sync,
                  "sync's fresh read seeds the cache (no extra scan)")

        # A recheck of the linked series must also read the fresh library.
        linked_id = None
        for row in db.get_series_identity_rows():
            if row.get("calibre_series_id") == 327:
                linked_id = row.get("id")
                break
        check(linked_id is not None, "linked tracker row exists")
        with patched_scanner(), patched_metadata(metadata):
            calls_before_recheck = FakeScanner.series_calls
            recheck_result = tm.recheck_series_calibre(linked_id)
        check(recheck_result["status"] == "success", f"recheck status: {recheck_result}")
        check(recheck_result["linked"] is True, f"recheck kept the link: {recheck_result}")
        check(FakeScanner.series_calls == calls_before_recheck + 1,
              f"recheck invalidated and rescanned exactly once: {FakeScanner.series_calls}")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 6. Concurrent reads: single-flight refresh, consistent cache
# ---------------------------------------------------------------------------
def test_concurrent_reads_single_flight():
    section("6. snapshot cache: concurrent threads refresh once (single-flight)")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    original_get_series = FakeScanner.get_all_series

    def slow_get_series(self):
        time.sleep(0.05)  # widen the race window between threads
        return original_get_series(self)

    FakeScanner.get_all_series = slow_get_series
    results = []
    errors = []
    lock = threading.Lock()

    def worker():
        try:
            snapshot = tm._calibre_library_snapshot()
            with lock:
                results.append(snapshot)
        except Exception as exc:  # pragma: no cover - failure diagnostic
            with lock:
                errors.append(exc)

    try:
        with patched_scanner():
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
    finally:
        FakeScanner.get_all_series = original_get_series

        check(not errors, f"concurrent snapshot reads must not raise: {errors}")
        check(len(results) == 4, f"all four threads returned a snapshot: {len(results)}")
        check(FakeScanner.series_calls <= 2,
              f"concurrent refresh is single-flight/bounded: {FakeScanner.series_calls} scan(s)")
        check(all(len(series) == 2 for series, _books in results),
              "every thread received the full library snapshot")
        check(len(tm._calibre_snapshot_cache["series"]) == 2,
              "cache state is consistent after the concurrent refresh")
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
TESTS = [
    ("ttl single read/copies", test_ttl_single_read_and_copies),
    ("expiry + invalidation", test_expiry_and_invalidation_refetch),
    ("unavailable not cached", test_unavailable_scanner_fallback_not_cached),
    ("rebind status reuse", test_rebind_status_calls_reuse_snapshot),
    ("sync/recheck invalidate", test_sync_and_recheck_invalidate_cache),
    ("concurrent single-flight", test_concurrent_reads_single_flight),
]


def main():
    print("=" * 78)
    print("  NovelTrax Calibre snapshot cache tests")
    print("=" * 78)

    failures = []
    try:
        for name, test in TESTS:
            try:
                test()
                print(f"  [PASS] {name}")
            except Exception as exc:  # noqa: BLE001 - report every failure
                failures.append((name, exc))
                print(f"  [FAIL] {name}: {type(exc).__name__}: {exc}")
                import traceback
                print(traceback.format_exc())
    finally:
        cleanup_temp_databases()

    print("\n" + "=" * 78)
    if failures:
        print(f"  {len(TESTS) - len(failures)}/{len(TESTS)} test groups passed, "
              f"{len(failures)} failed")
        print("=" * 78)
        raise SystemExit(1)
    print(f"  all {len(TESTS)} test groups passed")
    print("=" * 78)
    print("ALL CALIBRE-SNAPSHOT-CACHE TESTS PASSED")


if __name__ == "__main__":
    main()
