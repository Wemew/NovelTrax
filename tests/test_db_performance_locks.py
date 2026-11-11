#!/usr/bin/env python3
"""DB performance + lock-scope tests (v5 contract §5, owner A).

Covers:

* ``get_all_series`` output equivalence: rows/ordering/``monitored_only``,
  latest-release-id cover selection, tracker-cover fallback, ``None`` when no
  cover, empty DB;
* the guarded expression index ``idx_releases_series_name_norm``;
* a bounded SQL statement count for ``get_all_series`` (<= 3 total statements,
  one aggregate data SELECT) instead of a per-series correlated subquery;
* ``unlink_series_from_calibre`` / ``reset_series_identity`` correctness after the
  read-phase refactor (status matrix, identity/alias preservation, return dicts);
* a concurrency proof that the normalised release scan in
  ``unlink_series_from_calibre`` runs on a read connection: a second connection
  performs a short ``BEGIN IMMEDIATE`` write with a small timeout and must succeed
  while the scan is in progress (no write lock held during the Python scan).

Hermetic: temp DBs under ``tempfile.gettempdir()`` (never ``mkdtemp``), explicit
``check()`` raising ``AssertionError``, no network.
"""

import gc
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
for _path in (BASE_DIR, BACKEND_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import app.parser as parser_module
from app.database import Database
from app.parser import normalize_text

PASSED = 0
FAILED = 0
FAILURES = []
_TEMP_COUNTER = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def check(condition, message):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {message}")
        return
    FAILED += 1
    FAILURES.append(message)
    print(f"  [FAIL] {message}")
    raise AssertionError(message)


def check_equal(actual, expected, message):
    check(actual == expected, f"{message} (expected {expected!r}, got {actual!r})")


def check_raises(exc_type, func, message):
    try:
        func()
    except exc_type:
        check(True, message)
        return
    except Exception as exc:  # noqa: BLE001 - wrong type is a failure
        check(False, f"{message}: expected {exc_type.__name__}, "
                     f"got {type(exc).__name__}: {exc}")
        return
    check(False, f"{message}: expected {exc_type.__name__}, nothing was raised")


def section(title):
    print(f"\n--- {title} ---")


def _make_temp_dir(label):
    global _TEMP_COUNTER
    _TEMP_COUNTER += 1
    path = os.path.join(
        tempfile.gettempdir(),
        "noveltrax-db-perf-%d-%d-%s" % (os.getpid(), _TEMP_COUNTER, label),
    )
    os.makedirs(path, exist_ok=True)
    return path


def _cleanup_temp_dir(path):
    try:
        gc.collect()
    except Exception:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _insert_tracker(db, name, calibre_id=None, is_monitored=1, cover_url=None,
                    book_count=0, latest_index=0.0, identity_locked=0,
                    display_title=None, anilist_meta_json=None, authors="",
                    match_source=""):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO series_tracking
                (name, calibre_series_id, is_monitored, cover_url, book_count,
                 latest_index, identity_locked, display_title, anilist_meta_json,
                 authors, match_source, owned_volumes_json, last_sync)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', CURRENT_TIMESTAMP);
            """,
            (
                name, calibre_id, is_monitored, cover_url or "", book_count,
                latest_index, identity_locked, display_title or "",
                anilist_meta_json or "", authors, match_source or "",
            ),
        )
        conn.commit()
    return db.get_series_by_name(name)


def _insert_release(db, guid, series_name, volume, status, cover_url=None,
                    matched_book_id=None, title=None):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO releases
                (guid, title, series_name, volume, status, cover_url,
                 calibre_matched_book_id, ignored, media_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unknown');
        """, (
            guid,
            title or f"{series_name} fixture {guid}",
            series_name,
            volume,
            status,
            cover_url,
            matched_book_id,
            1 if status == "IGNORED" else 0,
        ))
        conn.commit()


def _release_row(db, guid):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM releases WHERE guid = ?;", (guid,))
        row = cur.fetchone()
        return dict(row) if row else None


def _logs_of_type(db, event_type, limit=200):
    try:
        return [
            entry for entry in db.get_logs(limit=limit)
            if entry.get("event_type") == event_type
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 1. get_all_series equivalence
# ---------------------------------------------------------------------------
def test_get_all_series_equivalence():
    section("1. get_all_series: ordering, monitored_only, cover selection")
    tmp_dir = _make_temp_dir("all-series")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        check_equal(db.get_all_series(), [], "get_all_series on an empty DB is []")
        check_equal(db.get_all_series(monitored_only=True), [],
                    "monitored-only get_all_series on an empty DB is []")

        _insert_tracker(db, "Alpha Series", is_monitored=0)
        _insert_tracker(db, "beta series", is_monitored=1, cover_url="tracker-cover-b")
        _insert_tracker(db, "Gamma Series", is_monitored=0,
                        cover_url="tracker-cover-g")
        _insert_tracker(db, "Delta Series", is_monitored=1)

        # Cover selection = latest release id with a non-empty cover.
        _insert_release(db, "cov-a1", "Alpha Series", 1.0, "NEW",
                        cover_url="https://c/alpha-old.jpg")
        _insert_release(db, "cov-a2", "Alpha Series", 2.0, "NEW",
                        cover_url="https://c/alpha-new.jpg")
        _insert_release(db, "cov-a3", "Alpha Series", 3.0, "NEW", cover_url=None)
        # Normalised-name (case/whitespace-insensitive) cover attach for beta; the
        # tracker already has its own fallback cover, but a release cover wins.
        _insert_release(db, "cov-b1", "  BETA series  ", 1.0, "NEW",
                        cover_url="https://c/beta-release.jpg")
        # Gamma has no release cover -> tracker fallback cover.
        _insert_release(db, "cov-g1", "Gamma Series", 1.0, "NEW", cover_url="")

        rows = db.get_all_series()
        check_equal([row["name"] for row in rows],
                    ["beta series", "Delta Series", "Alpha Series", "Gamma Series"],
                    "default ordering is is_monitored DESC then name NOCASE ASC")
        by_name = {row["name"]: row for row in rows}
        for name in ("Alpha Series", "beta series", "Gamma Series", "Delta Series"):
            for key in ("id", "name", "display_title", "cover_url",
                        "calibre_series_id", "owned_volumes_json"):
                check(key in by_name[name], f"{name} row contains {key}")
            check("release_cover_url" not in by_name[name],
                  f"{name} row does not leak the internal release_cover_url key")
        check_equal(by_name["Alpha Series"]["cover_url"],
                    "https://c/alpha-new.jpg",
                    "cover_url is the latest release id with a non-empty cover")
        check_equal(by_name["beta series"]["cover_url"],
                    "https://c/beta-release.jpg",
                    "release cover wins over the tracker fallback cover")
        check_equal(by_name["Gamma Series"]["cover_url"], "tracker-cover-g",
                    "tracker fallback cover is used when no release has a cover")
        check_equal(by_name["Delta Series"]["cover_url"], None,
                    "cover_url is None when neither tracker nor releases have one")

        monitored = db.get_all_series(monitored_only=True)
        check_equal([row["name"] for row in monitored],
                    ["beta series", "Delta Series"],
                    "monitored_only filters and orders by name NOCASE ASC")
        check_equal(len(rows), 4, "non-monitored get_all_series returns every row")
        check_equal(len(by_name["Alpha Series"]), 18,
                    "row dict keeps the 17 tracker columns plus cover_url")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 2. Expression index + bounded statements
# ---------------------------------------------------------------------------
def test_expression_index_and_bounded_statements():
    section("2. expression index + <=3 SQL statements for get_all_series")
    tmp_dir = _make_temp_dir("statements")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        db.init_db()
        db.init_db()  # idempotent guarded index creation
        with db.get_connection() as conn:
            index_names = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index';")
            }
            index_columns = [
                dict(row)
                for row in conn.execute("PRAGMA index_list(releases);")
            ]
        check("idx_releases_series_name_norm" in index_names,
              "guarded expression index idx_releases_series_name_norm exists")
        check(
            any(row.get("name") == "idx_releases_series_name_norm"
                for row in index_columns),
            "the expression index is registered on the releases table",
        )

        for index in range(60):
            _insert_tracker(db, f"Series {index:03d}", is_monitored=index % 2)
            _insert_release(db, f"stmt-g{index}", f"Series {index:03d}", 1.0,
                            "NEW", cover_url=f"https://c/{index}.jpg")

        # Instrument every SQL statement executed through this DB's connections.
        # sqlite3.Connection is an immutable C type, so patch sqlite3.connect to
        # install a counting Connection/Cursor subclass factory instead.
        original_connect = sqlite3.connect
        captured = {"statements": []}

        class _CountingCursor:
            def __init__(self, cursor):
                self._cursor = cursor

            def execute(self, sql, *args, **kwargs):
                captured["statements"].append(str(sql))
                return self._cursor.execute(sql, *args, **kwargs)

            def executemany(self, sql, *args, **kwargs):
                captured["statements"].append(str(sql))
                return self._cursor.executemany(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._cursor, name)

        class _CountingConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                captured["statements"].append(str(sql))
                return super().execute(sql, *args, **kwargs)

            def cursor(self, *args, **kwargs):
                return _CountingCursor(super().cursor(*args, **kwargs))

        def counting_connect(*args, **kwargs):
            kwargs.setdefault("factory", _CountingConnection)
            return original_connect(*args, **kwargs)

        sqlite3.connect = counting_connect
        try:
            captured["statements"] = []
            rows = db.get_all_series()
            default_count = len(captured["statements"])
            default_statements = list(captured["statements"])

            captured["statements"] = []
            monitored_rows = db.get_all_series(monitored_only=True)
            monitored_count = len(captured["statements"])
        finally:
            sqlite3.connect = original_connect

        check_equal(len(rows), 60, "instrumented default call returns every series")
        check_equal(len(monitored_rows), 30,
                    "instrumented monitored_only call returns monitored series")
        check(default_count <= 3,
              f"get_all_series executes <=3 statements (got {default_count})")
        check(monitored_count <= 3,
              f"monitored-only get_all_series executes <=3 statements (got {monitored_count})")
        data_statements = [
            statement for statement in default_statements
            if statement.strip().upper().startswith("WITH")
            or "FROM SERIES_TRACKING" in statement.upper()
        ]
        check_equal(len(data_statements), 1,
                    "get_all_series uses exactly one aggregate data statement")
        check("GROUP BY LOWER(TRIM(SERIES_NAME))" in data_statements[0].upper(),
              "the data statement aggregates covers with GROUP BY lower(trim(...))")
        check("COVER_LATEST" in data_statements[0].upper(),
              "the data statement uses the cover_latest aggregate CTE")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 3. unlink/reset correctness after the read-phase refactor
# ---------------------------------------------------------------------------
def test_unlink_and_reset_correctness():
    section("3. unlink/reset correctness with the read-before-write refactor")
    tmp_dir = _make_temp_dir("correctness")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        series_name = "Refactor Series"
        tracker = _insert_tracker(db, series_name, calibre_id=77,
                                  display_title="Refactor Display",
                                  book_count=4, latest_index=4.0,
                                  identity_locked=1)
        _insert_tracker(db, "Other Series", calibre_id=5)
        db.add_series_alias_override("Refactor Alias", series_name, 77,
                                     origin="manual")
        _insert_release(db, "ref-vol3", series_name, 3.0, "IN_CALIBRE",
                        matched_book_id=13, title=f"{series_name} Vol 3")
        _insert_release(db, "ref-vol1", series_name, 1.0, "IN_CALIBRE",
                        matched_book_id=11, title=f"{series_name} Vol 1")
        _insert_release(db, "ref-ignored", series_name, 2.0, "IGNORED",
                        matched_book_id=12, title=f"{series_name} Vol 2")

        result = db.unlink_series_from_calibre(tracker["id"], actor="tester")
        check_equal(result["status"], "success", "unlink returns success")
        check_equal(result["already_unlinked"], False, "unlink is performed")
        check_equal(result["releases_reset"], 2,
                    "unlink resets the two non-veto IN_CALIBRE releases")
        check_equal(set(result.keys()),
                    {"status", "series_id", "series_name",
                     "old_calibre_series_id", "releases_reset",
                     "already_unlinked"},
                    "unlink return dict shape is unchanged")
        check_equal(_release_row(db, "ref-vol3")["status"], "REQUESTED",
                    "vol 3 release reset to REQUESTED")
        check_equal(_release_row(db, "ref-vol1")["status"], "NEW",
                    "vol 1 release reset to NEW")
        check_equal(_release_row(db, "ref-vol3")["calibre_matched_book_id"], None,
                    "reset release matched id cleared")
        check_equal(_release_row(db, "ref-ignored")["status"], "IGNORED",
                    "IGNORED release untouched")
        check_equal(_release_row(db, "ref-ignored")["calibre_matched_book_id"], 12,
                    "IGNORED release matched id untouched")
        stored = db.get_series_by_name(series_name)
        check_equal(stored["calibre_series_id"], None, "unlink clears the Calibre id")
        check_equal(stored["display_title"], "Refactor Display",
                    "unlink preserves display_title")
        check_equal(stored["identity_locked"], 1,
                    "unlink preserves identity_locked")
        check_equal(len(db.get_series_alias_overrides()), 1,
                    "unlink preserves alias overrides")
        check_equal(len(_logs_of_type(db, "SERIES_CALIBRE_UNLINKED")), 1,
                    "unlink logs SERIES_CALIBRE_UNLINKED")
        check_raises(KeyError, lambda: db.unlink_series_from_calibre(999999),
                     "unlink of an unknown series raises KeyError")

        # reset_series_identity after the read-phase refactor.
        manual_name = "Refactor Manual Title"
        manual = _insert_tracker(db, manual_name, identity_locked=1,
                                 display_title="Manual Display",
                                 anilist_meta_json=json.dumps({"format": "NOVEL"}),
                                 match_source="manual-new", is_monitored=1)
        db.add_series_alias_override("Manual Alias", manual_name, origin="manual")
        _insert_release(db, "man-r1", manual_name, 1.0, "NEW",
                        title="Original Parsed Name Vol 1")
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE releases SET series_pinned = 1, series_pinned_at = 'x' "
                "WHERE guid = 'man-r1';"
            )
            conn.commit()
        reset = db.reset_series_identity(manual["id"], actor="tester")
        check_equal(reset["status"], "success", "reset returns success")
        check_equal(reset["releases_rekeyed"], 1,
                    "reset re-keys the manual title back to the parsed series")
        check_equal(reset["releases_unpinned"], 1, "reset unpins the release")
        check_equal(reset["aliases_removed"], 1, "reset removes the series alias")
        check_equal(set(reset.keys()),
                    {"status", "series_id", "series_name", "aliases_removed",
                     "releases_unpinned", "releases_rekeyed",
                     "display_cleared", "locked_cleared"},
                    "reset return dict shape is unchanged")
        check_equal(_release_row(db, "man-r1")["series_name"],
                    "Original Parsed Name",
                    "release restored to its title-parsed series")
        check_equal(_release_row(db, "man-r1")["series_pinned"], 0,
                    "release unpinned by reset")
        reset_row = db.get_series_by_name(manual_name)
        check_equal(reset_row["identity_locked"], 0, "reset clears identity_locked")
        check_equal(reset_row["display_title"], "", "reset clears display_title")
        check_equal(reset_row["anilist_meta_json"], "", "reset clears anilist meta")
        check_equal(len(_logs_of_type(db, "SERIES_IDENTITY_RESET")), 1,
                    "reset logs SERIES_IDENTITY_RESET")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 4. No write lock across the normalised release scan (concurrency proof)
# ---------------------------------------------------------------------------
def test_no_write_lock_during_release_scan():
    section("4. unlink keeps no write transaction open during the release scan")
    tmp_dir = _make_temp_dir("locking")
    db_path = os.path.join(tmp_dir, "app.db")
    try:
        db = Database(db_path)
        series_name = "Locking Target Series"
        tracker = _insert_tracker(db, series_name, calibre_id=99)
        # One matching release + many non-matching rows: the scan must normalise
        # them all, which is the slow O(releases) part.
        _insert_release(db, "lock-match", series_name, 2.0, "IN_CALIBRE",
                        matched_book_id=1, title=f"{series_name} Vol 2")
        with db.get_connection() as conn:
            cur = conn.cursor()
            filler_rows = [
                (f"filler-{index}", f"Filler Series {index} Vol 1",
                 f"Filler Series {index}", 1.0, "NEW", None, None, 0,
                 "unknown")
                for index in range(2000)
            ]
            cur.executemany("""
                INSERT INTO releases
                    (guid, title, series_name, volume, status, cover_url,
                     calibre_matched_book_id, ignored, media_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, filler_rows)
            conn.commit()

        scan_started = threading.Event()
        original_normalize = parser_module.normalize_text
        state = {"calls": 0}

        def delaying_normalize(text):
            state["calls"] += 1
            result = original_normalize(text)
            if state["calls"] == 2:
                # First release row of the normalised scan: signal the writer and
                # hold the scan for longer than the writer's busy timeout.
                scan_started.set()
                time.sleep(0.8)
            return result

        unlink_result = {}
        unlink_error = {}

        def run_unlink():
            try:
                unlink_result.update(
                    db.unlink_series_from_calibre(tracker["id"], actor="tester")
                )
            except BaseException as exc:  # noqa: BLE001 - reported by assertion
                unlink_error["exc"] = exc

        writer_result = {}

        def run_short_writer():
            if not scan_started.wait(5.0):
                writer_result["error"] = "scan never started"
                return
            conn = None
            try:
                # Small busy timeout: if the unlink held a write transaction while
                # scanning (old behaviour) this raises "database is locked".
                conn = sqlite3.connect(db_path, timeout=0.25)
                conn.execute("BEGIN IMMEDIATE;")
                conn.execute(
                    "UPDATE app_settings SET value = 'concurrency-probe' "
                    "WHERE key = 'log_level';"
                )
                conn.commit()
                writer_result["ok"] = True
            except Exception as exc:  # noqa: BLE001 - reported by assertion
                writer_result["ok"] = False
                writer_result["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                if conn is not None:
                    conn.close()

        parser_module.normalize_text = delaying_normalize
        try:
            unlink_thread = threading.Thread(target=run_unlink, daemon=True)
            writer_thread = threading.Thread(target=run_short_writer, daemon=True)
            unlink_thread.start()
            writer_thread.start()
            writer_thread.join(timeout=10.0)
            unlink_thread.join(timeout=20.0)
        finally:
            parser_module.normalize_text = original_normalize

        check(not unlink_thread.is_alive(), "the unlink thread completed")
        check(not writer_thread.is_alive(), "the concurrent writer thread completed")
        check(not unlink_error, f"unlink raised no error ({unlink_error})")
        check_equal(unlink_result.get("status"), "success",
                    "unlink succeeded with a concurrent short writer")
        check_equal(unlink_result.get("releases_reset"), 1,
                    "the matching IN_CALIBRE release was reset")
        check(writer_result.get("ok") is True,
              "the short BEGIN IMMEDIATE write succeeded while the scan ran "
              f"({writer_result.get('error')})")
        check_equal(db.get_setting("log_level"), "concurrency-probe",
                    "the concurrent writer's update is persisted")
        check_equal(_release_row(db, "lock-match")["status"], "REQUESTED",
                    "unlink still reset the matching release after the concurrency test")
        check(db.get_series_by_name(series_name)["calibre_series_id"] is None,
              "unlink still cleared the Calibre link after the concurrency test")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
TESTS = [
    test_get_all_series_equivalence,
    test_expression_index_and_bounded_statements,
    test_unlink_and_reset_correctness,
    test_no_write_lock_during_release_scan,
]


def main():
    global FAILED
    print("=" * 78)
    print("  NovelTrax DB performance + lock-scope tests (v5 §5)")
    print(f"  interpreter optimize level: {sys.flags.optimize}")
    print("=" * 78)
    for test_fn in TESTS:
        try:
            test_fn()
        except AssertionError:
            # check() already recorded and printed the failure.
            continue
        except Exception as exc:  # noqa: BLE001 - keep running the remaining suites
            FAILED += 1
            FAILURES.append(f"{test_fn.__name__}: {exc}")
            print(f"  [FAIL] {test_fn.__name__} raised {type(exc).__name__}: {exc}")
            traceback.print_exc()

    print("\n" + "=" * 78)
    print(f"  {PASSED} checks passed, {FAILED} failed")
    print("=" * 78)
    if FAILED:
        for failure in FAILURES:
            print(f"  - {failure}")
        raise SystemExit(1)
    print("ALL DB PERFORMANCE/LOCK TESTS PASSED")


if __name__ == "__main__":
    main()
