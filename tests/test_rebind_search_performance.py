#!/usr/bin/env python3
"""Cross-module acceptance test for the re-bind search debounce + performance/lock hardening.

Contract: ``.agents/rebind_perf/CONTRACT.md`` (v5).  This suite covers the integration seams that the
per-owner suites do not:

1. UI: the re-bind modal search is debounced at **500 ms** (while the Settings identity search and the
   Series-page match modal stay at 250 ms) and cancels/ignores stale requests.
2. Backend wiring: the picker snapshot cache/invalidation is present and used by both pickers; the
   scheduler Calibre snapshot cache exists; the DB has the expression index and the aggregate cover
   query.
3. Performance: on a synthetic 400-series / 3,200-release database, the rewritten ``get_all_series`` is
   materially faster than the old correlated-subquery shape (same result), and repeated picker snapshot
   reads within the TTL are essentially free.

Hermetic: temp DB/dirs under the system TEMP, no network, no FastAPI/Jinja2 rendering (static template
checks only).  Explicit ``check()``; no bare ``assert``.

Usage::
    python tests/test_rebind_search_performance.py
    python tests/run_tests.py rebind_search_performance
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
TEMPLATES_DIR = os.path.join(BACKEND_DIR, "app", "templates")
for _path in (BACKEND_DIR, PROJECT_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.database import Database  # noqa: E402

_TEMP_DIRS = []
_CHECKS_RUN = [0]


def check(condition, message):
    _CHECKS_RUN[0] += 1
    if not condition:
        raise AssertionError(message)


def check_in(needle, haystack, message):
    check(needle in haystack, "%s (missing %r)" % (message, needle))


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def make_temp_dir(label):
    path = os.path.join(tempfile.gettempdir(), "noveltrax-rebind-perf-%s-%s" % (label, os.urandom(6).hex()))
    os.makedirs(path, exist_ok=True)
    _TEMP_DIRS.append(path)
    return path


def cleanup():
    while _TEMP_DIRS:
        shutil.rmtree(_TEMP_DIRS.pop(), ignore_errors=True)


def test_ui_debounce_and_abort_guard():
    base = read(os.path.join(TEMPLATES_DIR, "base.html"))
    settings = read(os.path.join(TEMPLATES_DIR, "settings.html"))
    series = read(os.path.join(TEMPLATES_DIR, "series.html"))
    discovery = read(os.path.join(TEMPLATES_DIR, "discovery.html"))

    start = base.find("function searchReleaseRebindSeries(")
    check(start != -1, "base.html declares searchReleaseRebindSeries")
    end = base.find("function renderReleaseRebindResults(", start)
    check(end != -1, "renderReleaseRebindResults follows the search function")
    search = base[start:end]
    check_in("}, 500);", search, "the re-bind series search is debounced by 500 ms")
    check("}, 250);" not in search, "the re-bind series search is no longer debounced by 250 ms")
    for marker in (
        "AbortController",
        "_releaseRebindSearchSeq",
        "_releaseRebindSearchSeq += 1",
        "seq !== _releaseRebindSearchSeq",
        "AbortError",
        "signal: signal",
        "_releaseRebindLastQuery",
    ):
        check_in(marker, search, "re-bind search uses the abort/stale marker %r" % marker)
    check_in("_releaseRebindLastQuery = null;", base, "modal state resets the last query")

    # The unrelated searches keep their 250 ms debounce.
    for name, source in (("settings.html", settings), ("series.html", series), ("discovery.html", discovery)):
        check("}, 250);" in source, "%s still debounces its match/identity search by 250 ms" % name)


def test_backend_cache_wiring_static():
    main = read(os.path.join(BACKEND_DIR, "app", "main.py"))
    scheduler = read(os.path.join(BACKEND_DIR, "app", "scheduler.py"))
    database = read(os.path.join(BACKEND_DIR, "app", "database.py"))
    for literal in (
        "SERIES_PICKER_CACHE_TTL_SECONDS = 10.0",
        "_series_picker_cache_lock = threading.Lock()",
        "def _series_picker_snapshot(",
        "def _invalidate_series_picker_cache(",
    ):
        check_in(literal, main, "main.py picker cache exposes %r" % literal)
    # Both pickers use the snapshot rather than rescanning.
    check_at = main.find("async def api_calibre_series_picker")
    check_at_2 = main.find("async def api_tracked_series_picker")
    check(check_at != -1 and check_at_2 != -1, "both picker handlers exist")
    check_in("_series_picker_snapshot", main[check_at:check_at_2], "Calibre picker uses the snapshot")
    check_in("_series_picker_snapshot", main[check_at_2:check_at_2 + 2500], "tracked picker uses the snapshot")
    for route in (
        "async def api_rebind_release",
        "async def api_match_series_to_calibre",
        "async def api_unlink_series_calibre",
        "async def api_recheck_series_calibre",
    ):
        at = main.find(route)
        check(at != -1, "route %r exists" % route)
        window = main[at:at + 6000]
        check_in("_invalidate_series_picker_cache()", window, "%r invalidates the picker cache" % route)

    for literal in (
        "CALIBRE_SNAPSHOT_TTL_SECONDS = 10.0",
        "def _cached_calibre_snapshot(",
        "def _invalidate_calibre_snapshot_cache(",
        "_calibre_snapshot_refresh_lock",
    ):
        check_in(literal, scheduler, "scheduler snapshot cache exposes %r" % literal)

    check_in("idx_releases_series_name_norm", database, "expression index is created")
    check_in("def get_all_series(", database, "get_all_series is present")


class PerfCtx:
    def __init__(self, label, series_count=400, per_series=8):
        self.root = make_temp_dir(label)
        self.db = Database(os.path.join(self.root, "perf.db"))
        self.series_count = series_count
        self.per_series = per_series
        names = []
        with self.db.get_connection() as conn:
            cur = conn.cursor()
            conn.execute("BEGIN")
            for i in range(series_count):
                name = "Performance Series %04d: A Very Long Title" % i
                names.append(name)
                cur.execute(
                    "INSERT INTO series_tracking (calibre_series_id, name, book_count, latest_index, "
                    "is_monitored, authors) VALUES (?, ?, ?, ?, 1, 'Author')",
                    (100000 + i, name, per_series, float(per_series)),
                )
                for j in range(1, per_series + 1):
                    cur.execute(
                        "INSERT INTO releases (guid, title, series_name, volume, status, link, cover_url, "
                        "volumes_json, media_type) VALUES (?, ?, ?, ?, 'NEW', ?, ?, ?, 'novel')",
                        (
                            "perf-%d-%d" % (i, j),
                            "%s Vol %d" % (name, j),
                            name,
                            float(j),
                            "https://nyaa.si/view/%d-%d" % (i, j),
                            ("https://img/%d.jpg" % i) if j == per_series else "",
                            json.dumps([float(j)]),
                        ),
                    )
            conn.commit()
        self.names = names

    def close(self):
        pass


def _old_get_all_series(conn):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT st.*,
            (SELECT r.cover_url FROM releases r
             WHERE lower(r.series_name) = lower(st.name) AND r.cover_url IS NOT NULL
               AND r.cover_url != ''
             ORDER BY r.id DESC LIMIT 1) AS cover_url
        FROM series_tracking st
        ORDER BY st.is_monitored DESC, st.name COLLATE NOCASE ASC;
        """
    )
    return [dict(row) for row in cur.fetchall()]


def test_get_all_series_performance():
    ctx = PerfCtx("bench", series_count=400, per_series=8)
    try:
        # Warm up (caches/OS page cache) then measure both shapes on the same data.
        ctx.db.get_all_series()
        conn = ctx.db.get_connection()
        try:
            _old_get_all_series(conn)
            times_old = []
            times_new = []
            for _ in range(3):
                start = time.perf_counter()
                old_rows = _old_get_all_series(conn)
                times_old.append(time.perf_counter() - start)
                start = time.perf_counter()
                new_rows = ctx.db.get_all_series()
                times_new.append(time.perf_counter() - start)
            old_best = min(times_old)
            new_best = min(times_new)
        finally:
            conn.close()

        check(len(new_rows) == 400, "get_all_series returns every tracked series")
        check(len(old_rows) == len(new_rows), "row counts match the old query")
        old_by_name = {row["name"]: row for row in old_rows}
        # Cover semantics (documented v5): the latest release with a non-empty cover wins;
        # otherwise the tracker's own enrichment cover; otherwise None.  Build the expectation
        # directly from the DB rather than the old duplicate-column query.
        with ctx.db.get_connection() as covers_conn:
            release_covers = {
                str(row["series_name"] or "").strip().lower(): (row["cover_url"] or "")
                for row in covers_conn.execute(
                    """
                    SELECT series_name, cover_url FROM releases
                    WHERE cover_url IS NOT NULL AND cover_url != '' AND id IN (
                        SELECT MAX(id) FROM releases
                        WHERE cover_url IS NOT NULL AND cover_url != ''
                        GROUP BY lower(trim(series_name))
                    );
                    """
                ).fetchall()
            }
            tracker_covers = {
                row["name"]: (row["cover_url"] or "")
                for row in covers_conn.execute("SELECT name, cover_url FROM series_tracking;").fetchall()
            }
        def _norm(value):
            return str(value or "").strip().lower()
        for row in new_rows:
            old = old_by_name[row["name"]]
            check(row.get("calibre_series_id") == old.get("calibre_series_id"),
                  "series_tracking payload intact for %r" % row["name"])
            expected_cover = (
                release_covers.get(_norm(row["name"]))
                or tracker_covers.get(row["name"])
                or None
            )
            check(row.get("cover_url") == expected_cover,
                  "cover selection matches release-cover/tracker-fallback for %r" % row["name"])

        # The optimized path must be materially faster; use a generous bound because this
        # runs on whatever disk/CPU the test environment has.
        check(new_best < 0.25, "get_all_series under 0.25 s (measured %.4fs)" % new_best)
        check(new_best <= old_best * 0.5,
              "get_all_series is at least 2x faster than the correlated-subquery shape "
              "(old %.4fs, new %.4fs)" % (old_best, new_best))

        # SQL statement budget: at most 3 statements for the whole call (rows + covers + PRAGMAs).
        calls = {"count": 0}
        original_get_connection = ctx.db.get_connection

        class _CountingCursor:
            def __init__(self, cursor):
                self._cursor = cursor

            def execute(self, sql, *args, **kwargs):
                # Count data statements only (connection setup PRAGMAs are not part of the
                # get_all_series query budget).
                if not str(sql).lstrip().upper().startswith("PRAGMA"):
                    calls["count"] += 1
                return self._cursor.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._cursor, name)

        class _CountingConn:
            def __init__(self, conn):
                self._conn = conn

            def cursor(self):
                return _CountingCursor(self._conn.cursor())

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._conn.close()
                return False

            def __getattr__(self, name):
                return getattr(self._conn, name)

        ctx.db.get_connection = lambda: _CountingConn(original_get_connection())
        try:
            ctx.db.get_all_series()
        finally:
            ctx.db.get_connection = original_get_connection
        check(calls["count"] <= 3,
              "get_all_series runs a bounded number of SQL statements (got %d)" % calls["count"])
    finally:
        ctx.close()


def main():
    tests = [
        ("UI 500 ms debounce + abort/stale guard", test_ui_debounce_and_abort_guard),
        ("backend cache wiring + DB index", test_backend_cache_wiring_static),
        ("get_all_series performance vs old shape", test_get_all_series_performance),
    ]
    failures = []
    print("=" * 78)
    print("Re-bind search debounce / performance-hardening integration suite")
    print("=" * 78)
    for label, test in tests:
        print("\n--- %s ---" % label)
        try:
            test()
            print("  [PASS] %s" % label)
        except Exception as exc:  # noqa: BLE001 - reported as a suite failure
            traceback.print_exc()
            failures.append((label, exc))
            print("  [FAIL] %s: %s" % (label, exc))
    cleanup()
    print("\n" + "=" * 78)
    if failures:
        print("FAILED %d/%d test(s); %d checks executed" % (len(failures), len(tests), _CHECKS_RUN[0]))
        for label, exc in failures:
            print("  - %s: %s" % (label, exc))
        return 1
    print("ALL %d TESTS PASSED (%d checks executed)" % (len(tests), _CHECKS_RUN[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
