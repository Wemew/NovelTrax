#!/usr/bin/env python3
"""Cross-module integration test: repairing a false "In Calibre" series.

Acceptance test for ``.agents/calibre_repair/CONTRACT.md`` (v4).  It wires the REAL modules:

    Database (trackers, releases, alias overrides, grouped view)
        -> TaskManager.unlink_series_calibre / recheck_series_calibre
            -> CalibreScanner (faked) / Calibre ownership + status reset + reconcile

Scenarios:
1. A tracker falsely linked to a Calibre series (the screenshot case) is unlinked: the Calibre link,
   owned volumes, book/latest counts are cleared, the releases move from IN_CALIBRE back to
   Wanted/New, identity data (name, display title, aliases) is preserved and the grouped Releases view
   stops reporting "In Calibre".
2. "Recheck Calibre" recomputes owned volumes from the real library and repairs releases that were
   incorrectly marked IN_CALIBRE for unowned volumes.
3. Rechecking a tracker whose Calibre series vanished performs the unlink flow; a missing Calibre
   library leaves the link untouched with an explanatory message; an unknown series id raises KeyError.

Hermetic: temp DB + fake Calibre scanner, no network. No bare ``assert`` (explicit ``check``).

Usage::

    python tests/test_calibre_repair_integration.py
    python tests/run_tests.py calibre_repair_integration
"""

import json
import os
import shutil
import sys
import tempfile
import traceback
import types

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
for _path in (BACKEND_DIR, PROJECT_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Provider modules import bs4/feedparser at module level; stub them for this stdlib-only runner.
for _missing, _stub in (
    ("bs4", {"BeautifulSoup": object, "NavigableString": str, "Tag": object}),
    ("feedparser", {"parse": lambda *args, **kwargs: None}),
):
    try:
        __import__(_missing)
    except ImportError:
        sys.modules[_missing] = types.SimpleNamespace(**_stub)

from app import scheduler as scheduler_module  # noqa: E402
from app.database import Database  # noqa: E402
from app.parser import normalize_text  # noqa: E402
from app.scheduler import TaskManager  # noqa: E402

_TEMP_DIRS = []
_CHECKS_RUN = [0]

CALIBRE_ID = 327
CALIBRE_NAME = "Re:Zero -Starting Life in Another World-"
DISPLAY_TITLE = "ReZero -Short Story Collection"
CALIBRE_SERIES = [
    {
        "series_id": CALIBRE_ID,
        "series_name": CALIBRE_NAME,
        "book_count": 6,
        "latest_index": 6.0,
        "authors": "Tappei Nagatsuki",
    }
]
CALIBRE_BOOKS = [
    {
        "book_id": 327000 + volume,
        "title": "%s Vol. %d" % (CALIBRE_NAME, volume),
        "series_id": CALIBRE_ID,
        "series_name": CALIBRE_NAME,
        "series_index": float(volume),
        "authors": "Tappei Nagatsuki",
    }
    for volume in range(1, 7)
]


def check(condition, message):
    _CHECKS_RUN[0] += 1
    if not condition:
        raise AssertionError(message)


def check_eq(actual, expected, message):
    check(actual == expected, "%s (expected %r, got %r)" % (message, expected, actual))


def check_in(needle, haystack, message):
    check(needle in haystack, "%s (missing %r in %r)" % (message, needle, str(haystack)[:400]))


def make_temp_dir(label):
    path = os.path.join(tempfile.gettempdir(), "noveltrax-calrepair-%s-%s" % (label, os.urandom(6).hex()))
    os.makedirs(path, exist_ok=True)
    _TEMP_DIRS.append(path)
    return path


def cleanup_temp_dirs():
    while _TEMP_DIRS:
        shutil.rmtree(_TEMP_DIRS.pop(), ignore_errors=True)


class FakeScanner:
    """Minimal CalibreScanner surface used by recheck/sync/reconcile."""

    def __init__(self, calibre_dir=None, series=None, books=None, volumes_map=None, available=True):
        self.db_path = os.path.join(calibre_dir or "", "metadata.db")
        self._series = [dict(item) for item in (series or [])]
        self._books = [dict(item) for item in (books or [])]
        self._volumes_map = dict(volumes_map or {})

    def is_available(self):
        return True

    def get_all_series(self):
        return [dict(item) for item in self._series]

    def get_all_books(self):
        return [dict(item) for item in self._books]

    def get_series_volumes_map(self):
        return {key: list(value) for key, value in self._volumes_map.items()}


class Ctx:
    def __init__(self, label="case", available=True, series=None, books=None, volumes_map=None):
        self.root = make_temp_dir(label)
        self.db = Database(os.path.join(self.root, "noveltrax.db"))
        self.calibre_dir = os.path.join(self.root, "calibre")
        os.makedirs(self.calibre_dir, exist_ok=True)
        self.data_dir = os.path.join(self.root, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.db.set_setting("calibre_path", self.calibre_dir)
        self.db.set_setting("filter_non_novels", "false")
        self.tm = TaskManager(self.db, self.calibre_dir, data_dir=self.data_dir)
        self.fake_scanner = FakeScanner(
            self.calibre_dir,
            series=series if series is not None else CALIBRE_SERIES,
            books=books if books is not None else CALIBRE_BOOKS,
            volumes_map=volumes_map if volumes_map is not None else {CALIBRE_ID: [1, 2, 3, 4, 5, 6]},
        )
        self._orig_scanner = scheduler_module.CalibreScanner
        scanner_available = bool(available)
        fake = self.fake_scanner

        class _Shim:
            def __init__(self, *args, **kwargs):
                self._scanner = fake

            def is_available(self):
                return scanner_available

            def __getattr__(self, name):
                return getattr(self._scanner, name)

        scheduler_module.CalibreScanner = _Shim

    def close(self):
        scheduler_module.CalibreScanner = self._orig_scanner
        try:
            self.tm._cover_executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            self.tm.stop()
        except Exception:
            pass

    def make_tracker(self, name=CALIBRE_NAME, calibre_id=CALIBRE_ID, display_title=DISPLAY_TITLE):
        self.db.sync_calibre_series(
            [
                {
                    "series_id": calibre_id,
                    "series_name": name,
                    "book_count": 6,
                    "latest_index": 6.0,
                    "authors": "Tappei Nagatsuki",
                }
            ],
            volumes_map={calibre_id: [1, 2, 3, 4, 5, 6]},
        )
        tracker = self.db.get_series_by_name(name)
        if display_title:
            self.db.update_series_identity(int(tracker["id"]), display_title=display_title)
        return tracker

    def insert_release(self, guid, volume, status, matched_book_id=None, title=None):
        title = title or "%s %s" % (CALIBRE_NAME, "" if volume is None else "Vol %s" % volume)
        with self.db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO releases (guid, title, series_name, volume, status, link,
                                      calibre_matched_book_id, volumes_json, media_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'novel');
                """,
                (
                    guid, title, CALIBRE_NAME, volume, status,
                    "https://nyaa.si/view/" + guid, matched_book_id,
                    json.dumps([] if volume is None else [volume]),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def row(self, release_id):
        return self.db.get_release_by_id(release_id)


def group_for(db, series_name):
    groups = db.get_releases_grouped_by_series()
    for group in groups:
        if normalize_text(group.get("stable_name") or group.get("series_name") or "") == normalize_text(series_name):
            return group
    return None


def test_unlink_repairs_the_screenshot_case():
    ctx = Ctx("unlink")
    try:
        tracker = ctx.make_tracker()
        ctx.db.add_series_alias_override(
            "Rezero short story collection", CALIBRE_NAME,
            calibre_series_id=CALIBRE_ID, origin="manual", actor="test",
        )
        release_ids = [
            ctx.insert_release("repair-1", 1.0, "IN_CALIBRE", 327001),
            ctx.insert_release("repair-2", 2.0, "IN_CALIBRE", 327002),
            ctx.insert_release("repair-6", 6.0, "IN_CALIBRE", 327006),
            ctx.insert_release("repair-ignored", 3.0, "IGNORED", 327003),
        ]
        before = group_for(ctx.db, CALIBRE_NAME)
        check(before is not None, "group exists before the repair")
        check_eq(before.get("in_calibre"), True, "group is falsely In Calibre before repair")
        check_eq(before.get("calibre_series_id"), CALIBRE_ID, "group exposes the wrong Calibre id")

        result = ctx.tm.unlink_series_calibre(int(tracker["id"]))
        check_eq(result.get("status"), "success", "unlink succeeds")
        check_eq(result.get("old_calibre_series_id"), CALIBRE_ID, "unlink reports the old Calibre id")
        check_eq(result.get("releases_reset"), 3, "the three IN_CALIBRE releases were reset")

        stored = ctx.db.get_series_by_name(CALIBRE_NAME)
        check_eq(stored.get("calibre_series_id"), None, "Calibre link cleared")
        check_eq(json.loads(stored.get("owned_volumes_json") or "[]"), [], "owned volumes cleared")
        check_eq(int(stored.get("book_count") or 0), 0, "stale Calibre book count cleared")
        check_eq(float(stored.get("latest_index") or 0.0), 0.0, "stale latest index cleared")
        check_eq(stored.get("display_title"), DISPLAY_TITLE, "display title preserved")
        check_eq(stored.get("name"), CALIBRE_NAME, "stable tracking name preserved")

        check_eq(ctx.row(release_ids[0]).get("status"), "NEW", "vol 1 returns to discovery")
        check_eq(ctx.row(release_ids[1]).get("status"), "REQUESTED", "vol 2 returns to wanted")
        check_eq(ctx.row(release_ids[2]).get("status"), "REQUESTED", "vol 6 returns to wanted")
        check_eq(int(ctx.row(release_ids[1]).get("calibre_matched_book_id") or 0), 0,
                 "matched Calibre book id cleared")
        check_eq(ctx.row(release_ids[3]).get("status"), "IGNORED",
                 "user-vetoed release keeps IGNORED")

        aliases = ctx.db.get_series_alias_overrides()
        check(any(normalize_text(a.get("target_name") or "") == normalize_text(CALIBRE_NAME) for a in aliases),
              "alias overrides pointing at the series are preserved")

        after = group_for(ctx.db, CALIBRE_NAME)
        check(after is not None, "group still exists after unlink")
        check_eq(after.get("in_calibre"), False, "group is no longer In Calibre")
        check_eq(after.get("calibre_series_id"), None, "group no longer exposes a Calibre id")

        logs = [log for log in ctx.db.get_logs(limit=50) if log.get("event_type") == "SERIES_CALIBRE_UNLINKED"]
        check_eq(len(logs), 1, "unlink is audited exactly once")
        details = json.loads(logs[0].get("details") or "{}")
        check_eq(details.get("old_calibre_series_id"), CALIBRE_ID, "audit records the old link")
    finally:
        ctx.close()


def test_recheck_recomputes_ownership_and_repairs_stale_rows():
    ctx = Ctx("recheck")
    try:
        tracker = ctx.make_tracker()
        owned = ctx.insert_release("recheck-1", 1.0, "IN_CALIBRE", 327001)
        stale = ctx.insert_release("recheck-5", 5.0, "IN_CALIBRE", 327005)
        ctx.db.set_series_owned_volumes(int(tracker["id"]), [])

        result = ctx.tm.recheck_series_calibre(int(tracker["id"]))
        check_eq(result.get("status"), "success", "recheck succeeds")
        check_eq(result.get("linked"), True, "recheck reports the link")
        check_eq(result.get("owned_volumes"), [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
                 "owned volumes recomputed from the real library")

        stored = ctx.db.get_series_by_name(CALIBRE_NAME)
        check_eq(json.loads(stored.get("owned_volumes_json") or "[]"),
                 [1.0, 2.0, 3.0, 4.0, 5.0, 6.0], "owned volumes persisted")
        # Reconcile marks volume 1 owned; volume 5 was already true here, so both remain owned.
        check_in(ctx.row(owned).get("status"), ("IN_CALIBRE",), "actually-owned volume stays IN_CALIBRE")
        check_in(ctx.row(stale).get("status"), ("IN_CALIBRE", "REQUESTED", "NEW"),
                 "stale row has a valid lifecycle status")

        # A release marked IN_CALIBRE for a volume that is NOT owned must be reset.
        bogus = ctx.insert_release("recheck-bogus", 9.0, "IN_CALIBRE", 327009)
        again = ctx.tm.recheck_series_calibre(int(tracker["id"]))
        check_eq(again.get("status"), "success", "second recheck succeeds")
        check_eq(ctx.row(bogus).get("status"), "REQUESTED",
                 "IN_CALIBRE row for an unowned volume is reset to wanted")

        logs = [log for log in ctx.db.get_logs(limit=50) if log.get("event_type") == "SERIES_CALIBRE_RECHECKED"]
        check(len(logs) >= 2, "recheck is audited")
    finally:
        ctx.close()


def test_recheck_unlinks_a_vanished_calibre_series():
    ctx = Ctx("vanished", series=CALIBRE_SERIES, books=CALIBRE_BOOKS)
    try:
        tracker = ctx.make_tracker(calibre_id=999)
        release = ctx.insert_release("vanish-2", 2.0, "IN_CALIBRE", 999002)
        result = ctx.tm.recheck_series_calibre(int(tracker["id"]))
        check_eq(result.get("status"), "success", "recheck succeeds")
        check_eq(result.get("unlinked"), True, "a vanished Calibre series is unlinked")
        check_eq(result.get("linked"), False, "recheck reports the series unlinked")
        stored = ctx.db.get_series_by_name(CALIBRE_NAME)
        check_eq(stored.get("calibre_series_id"), None, "link cleared on vanished target")
        check_eq(ctx.row(release).get("status"), "REQUESTED", "release reset after unlink")
    finally:
        ctx.close()


def test_recheck_containment_and_unknown_id():
    ctx = Ctx("containment", available=False)
    try:
        tracker = ctx.make_tracker()
        release = ctx.insert_release("contain-1", 1.0, "IN_CALIBRE", 327001)
        result = ctx.tm.recheck_series_calibre(int(tracker["id"]))
        check_eq(result.get("status"), "success", "recheck remains a success when Calibre is unavailable")
        check_eq(result.get("linked"), True, "link left unchanged when the library is unreadable")
        check_in("skipped", str(result.get("message") or "").lower(), "message explains the skip")
        check_eq(ctx.row(release).get("status"), "IN_CALIBRE", "release untouched when recheck could not run")
        stored = ctx.db.get_series_by_name(CALIBRE_NAME)
        check_eq(stored.get("calibre_series_id"), CALIBRE_ID, "link preserved when Calibre is unavailable")

        unknown = None
        try:
            ctx.tm.recheck_series_calibre(999999)
        except Exception as exc:
            unknown = exc
        check(isinstance(unknown, KeyError), "unknown series raises KeyError")
    finally:
        ctx.close()


def test_static_guards():
    main_source = open(os.path.join(BACKEND_DIR, "app", "main.py"), "r", encoding="utf-8").read()
    scheduler_source = open(os.path.join(BACKEND_DIR, "app", "scheduler.py"), "r", encoding="utf-8").read()
    db_source = open(os.path.join(BACKEND_DIR, "app", "database.py"), "r", encoding="utf-8").read()
    series_source = open(os.path.join(BACKEND_DIR, "app", "templates", "series.html"), "r", encoding="utf-8").read()
    settings_source = open(os.path.join(BACKEND_DIR, "app", "templates", "settings.html"), "r", encoding="utf-8").read()
    readme = open(os.path.join(PROJECT_ROOT, "README.md"), "r", encoding="utf-8").read()
    for route in (
        '@app.post("/api/series/{series_id}/unlink-calibre")',
        '@app.post("/api/series/{series_id}/recheck-calibre")',
    ):
        check_in(route, main_source, "main.py declares %r" % route)
    for literal in ("def unlink_series_calibre", "def recheck_series_calibre"):
        check_in(literal, scheduler_source, "scheduler implements %r" % literal)
    for literal in ("def unlink_series_from_calibre", "def set_series_owned_volumes"):
        check_in(literal, db_source, "database implements %r" % literal)
    for literal in ("series-recheck-calibre-btn", "series-unlink-calibre-btn", "recheckSeriesCalibre", "unlinkSeriesCalibre"):
        check_in(literal, series_source, "series page exposes %r" % literal)
    check_in("series-unlink-calibre-btn", settings_source, "settings identity card exposes unlink")
    check_in("Repairing false In Calibre ownership", readme, "README documents the repair actions")


def main():
    tests = [
        ("unlink repairs the screenshot case", test_unlink_repairs_the_screenshot_case),
        ("recheck recomputes ownership + stale rows", test_recheck_recomputes_ownership_and_repairs_stale_rows),
        ("recheck unlinks a vanished Calibre series", test_recheck_unlinks_a_vanished_calibre_series),
        ("recheck containment + unknown id", test_recheck_containment_and_unknown_id),
        ("static guards", test_static_guards),
    ]
    failures = []
    print("=" * 78)
    print("False In Calibre repair integration suite (unlink / recheck)")
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
    cleanup_temp_dirs()
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
