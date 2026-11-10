#!/usr/bin/env python3
"""Cross-module integration tests for release re-bind, per-series identity reset and the
ported settings/maintenance features (NovelTrax).

Acceptance tests for ``.agents/series_identity/CONTRACT_V2_REBIND.md``.  They wire the REAL
modules together:

    Database (releases + series_tracking + alias overrides + activity logs)
        -> TaskManager (rebind_release / reset_series_identity / prune_activity_logs)
            -> CalibreMatcher / series identity resolver
        -> app.diagnostics (storage health, series attribution audit)
        -> database maintenance helpers (stats, vacuum, backup, log prune)

Hermetic: temp DB/dirs under the system TEMP (mkdtemp is unusable in this sandbox), fake Calibre
snapshot, no network.  Bare ``assert`` is never used; every expectation goes through ``check``.

Usage::

    python tests/test_rebind_settings_integration.py
    python tests/run_tests.py rebind_settings_integration
"""

import json
import os
import shutil
import sqlite3
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

from app import diagnostics  # noqa: E402
from app.database import Database  # noqa: E402
from app.parser import normalize_text  # noqa: E402
from app.scheduler import TaskManager  # noqa: E402

_TEMP_DIRS = []
_CHECKS_RUN = [0]

CALIBRE_SERIES = [
    {
        "series_id": 327,
        "series_name": "Trapped in a Dating Sim: The World of Otome Games is Tough for Us, Too",
        "book_count": 10,
        "latest_index": 10.0,
        "authors": "Yomu Mishima",
    }
]
CALIBRE_BOOKS = [
    {
        "book_id": 327000 + volume,
        "title": "%s Vol. %d" % (CALIBRE_SERIES[0]["series_name"], volume),
        "series_id": 327,
        "series_name": CALIBRE_SERIES[0]["series_name"],
        "series_index": float(volume),
        "authors": "Yomu Mishima",
    }
    for volume in range(1, 11)
]
ANILIST_TITLE = "Trapped in a Dating Sim: Otome Games Are Tough for Us, Too!"


def check(condition, message):
    _CHECKS_RUN[0] += 1
    if not condition:
        raise AssertionError(message)


def check_eq(actual, expected, message):
    check(actual == expected, "%s (expected %r, got %r)" % (message, expected, actual))


def check_in(needle, haystack, message):
    check(needle in haystack, "%s (missing %r in %r)" % (message, needle, str(haystack)[:400]))


def check_not_in(needle, haystack, message):
    check(needle not in haystack, "%s (unexpected %r in %r)" % (message, needle, str(haystack)[:400]))


def make_temp_dir(label):
    path = os.path.join(tempfile.gettempdir(), "noveltrax-rebind-int-%s-%s" % (label, os.urandom(6).hex()))
    os.makedirs(path, exist_ok=True)
    _TEMP_DIRS.append(path)
    return path


def cleanup_temp_dirs():
    while _TEMP_DIRS:
        shutil.rmtree(_TEMP_DIRS.pop(), ignore_errors=True)


class Ctx:
    def __init__(self, label="case"):
        self.root = make_temp_dir(label)
        self.db = Database(os.path.join(self.root, "noveltrax.db"))
        self.calibre_dir = os.path.join(self.root, "calibre")
        os.makedirs(self.calibre_dir, exist_ok=True)
        self.downloads = os.path.join(self.root, "downloads")
        os.makedirs(self.downloads, exist_ok=True)
        self.db.set_setting("calibre_path", self.calibre_dir)
        self.db.set_setting("downloads_dir", self.downloads)
        self.tm = TaskManager(self.db, self.calibre_dir, data_dir=self.root)
        self.tm._calibre_series_snapshot = lambda: [dict(item) for item in CALIBRE_SERIES]
        self.tm._calibre_library_snapshot = lambda: (
            [dict(item) for item in CALIBRE_SERIES],
            [dict(item) for item in CALIBRE_BOOKS],
        )
        self._counter = 0

    def close(self):
        try:
            self.tm._cover_executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            self.tm.stop()
        except Exception:
            pass

    def insert_release(self, title, series_name, volume=1.0, status="REQUESTED", **overrides):
        self._counter += 1
        guid = "https://nyaa.si/view/%d" % (60000 + self._counter)
        values = {
            "guid": guid,
            "title": title,
            "series_name": series_name,
            "volume": volume,
            "link": guid,
            "download_url": "magnet:?xt=urn:btih:" + ("a" * 40),
            "status": status,
            "media_type": "light_novel",
            "volumes_json": json.dumps([volume]),
            "missing_vols_json": json.dumps([volume]),
        }
        values.update(overrides)
        columns = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        with self.db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO releases (%s) VALUES (%s);" % (columns, placeholders),
                tuple(values.values()),
            )
            conn.commit()
            return int(cur.lastrowid), guid

    def row(self, release_id):
        return self.db.get_release_by_id(release_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_rebind_to_calibre_is_durable_against_reingest():
    ctx = Ctx("bind")
    try:
        ctx.db.sync_calibre_series(CALIBRE_SERIES)
        release_id, guid = ctx.insert_release(
            title="Wrong Series Vol 2", series_name="Wrong Series", volume=2.0, status="REQUESTED",
        )
        summary = ctx.tm.rebind_release(
            release_id, mode="bind", calibre_series_id=327, actor="user",
        )
        check_eq(summary.get("status"), "success", "bind re-bind succeeds: %r" % (summary,))
        row = ctx.row(release_id)
        check_eq(row.get("series_name"), CALIBRE_SERIES[0]["series_name"],
                 "release is re-keyed to the Calibre stable name")
        check_eq(int(row.get("series_pinned") or 0), 1, "release is pinned")
        check_in(row.get("status"), ("IN_CALIBRE", "REQUESTED"),
                 "status is recomputed for the Calibre target")
        check(not ctx.db.get_series_alias_overrides() or all(
            normalize_text(a.get("source_name") or "") != normalize_text("Wrong Series")
            for a in ctx.db.get_series_alias_overrides()
        ), "default bind does not create a global alias (per-release pin only)")

        # Feed re-ingest of the same GUID with the old parsed series must not undo the pin.
        ctx.db.upsert_release(
            {
                "guid": guid,
                "title": "Wrong Series Vol 2",
                "series_name": "Wrong Series",
                "volume": 2.0,
                "link": guid,
                "status": "REQUESTED",
            }
        )
        row = ctx.row(release_id)
        check_eq(row.get("series_name"), CALIBRE_SERIES[0]["series_name"],
                 "re-ingest cannot move a pinned release")
        check_eq(int(row.get("series_pinned") or 0), 1, "pin survives re-ingest")

        # Reconcile of the linked series must skip the pinned row (it is already correct).
        ctx.tm.reconcile_series_releases(CALIBRE_SERIES[0]["series_name"])
        check_eq(ctx.row(release_id).get("series_name"), CALIBRE_SERIES[0]["series_name"],
                 "reconcile leaves the pinned release where the user put it")

        # Unpin restores automatic behaviour for that one release.
        check_eq(ctx.db.unpin_release_series(release_id), True, "unpin succeeds")
        check_eq(int(ctx.row(release_id).get("series_pinned") or 0), 0, "release is unpinned")
    finally:
        ctx.close()


def test_new_title_reindex_is_not_linked_to_previous_series():
    ctx = Ctx("newtitle")
    try:
        ctx.db.sync_calibre_series(CALIBRE_SERIES)
        parsed = "Trapped in a Dating Sim: Otome Games Are Tough for Us, Too"
        release_id, _ = ctx.insert_release(
            title=parsed + " Vol 3",
            series_name=parsed,
            volume=3.0,
            status="REQUESTED",
        )
        summary = ctx.tm.rebind_release(
            release_id, mode="new_title", target_name="Custom Spinoff Reindex", actor="user",
        )
        check_eq(summary.get("status"), "success", "new-title re-index succeeds: %r" % (summary,))
        row = ctx.row(release_id)
        check_eq(row.get("series_name"), "Custom Spinoff Reindex", "release moves to the new title")
        check_eq(int(row.get("series_pinned") or 0), 1, "release is pinned")
        check_eq(row.get("status"), "REQUESTED", "volume > 1 becomes wanted in the new series")

        tracker = ctx.db.get_series_by_name("Custom Spinoff Reindex")
        check(tracker is not None, "a new tracked series was created")
        check_eq(int(tracker.get("identity_locked") or 0), 1,
                 "the new series is identity-locked against automatic merging")
        check_eq(tracker.get("match_source"), "manual-new", "tracker records the manual origin")

        # Explicitly not under the same or any previous series: no alias override back to the old name.
        aliases = ctx.db.get_series_alias_overrides()
        for alias in aliases:
            check(
                normalize_text(alias.get("target_name") or "") != normalize_text("Custom Spinoff Reindex")
                or normalize_text(alias.get("source_name") or "") not in (
                    normalize_text(parsed), normalize_text(CALIBRE_SERIES[0]["series_name"]),
                ),
                "no alias links the new title to the previous/Calibre series",
            )

        # Reset restores the title-parsed series and unlocks the manual tracker.
        reset = ctx.tm.reset_series_identity(int(tracker["id"]))
        check_eq(reset.get("status"), "success", "per-series reset succeeds: %r" % (reset,))
        check(reset.get("aliases_removed", 0) >= 0, "reset reports removed aliases")
        check(reset.get("releases_rekeyed", 0) >= 1, "reset re-keys the re-indexed release")
        row = ctx.row(release_id)
        check_eq(int(row.get("series_pinned") or 0), 0, "reset unpins the release")
        check_eq(row.get("series_name"), parsed,
                 "reset restores the title-parsed series name")
        tracker = ctx.db.get_series_by_name("Custom Spinoff Reindex")
        check(tracker is not None, "the manual tracker row still exists after reset (no data loss)")
        check_eq(int(tracker.get("identity_locked") or 0), 0, "reset unlocks the tracker")
        check_eq(tracker.get("display_title") or "", "", "reset clears the display title")
    finally:
        ctx.close()


def test_detach_creates_a_standalone_series():
    ctx = Ctx("detach")
    try:
        ctx.db.sync_calibre_series(CALIBRE_SERIES)
        release_id, _ = ctx.insert_release(
            title="Some Parent Series Vol 1", series_name="Some Parent Series", volume=1.0, status="NEW",
        )
        summary = ctx.tm.rebind_release(
            release_id, mode="detach", target_name="Standalone Side Story", actor="user",
        )
        check_eq(summary.get("status"), "success", "detach succeeds: %r" % (summary,))
        row = ctx.row(release_id)
        check_eq(row.get("series_name"), "Standalone Side Story", "release is detached to the new title")
        check_eq(row.get("status"), "NEW", "volume 1 in a fresh series is a discovery candidate")
        check_eq(int(row.get("series_pinned") or 0), 1, "detached release is pinned")
        tracker = ctx.db.get_series_by_name("Standalone Side Story")
        check(tracker is not None and int(tracker.get("identity_locked") or 0) == 1,
              "the standalone tracker is created and locked")

        # Existing/known titles must be rejected: a re-index must be genuinely new.
        rejected = None
        try:
            ctx.tm.rebind_release(
                release_id, mode="new_title",
                target_name=CALIBRE_SERIES[0]["series_name"], actor="user",
            )
        except Exception as exc:  # ValueError expected
            rejected = exc
        check(rejected is not None, "re-indexing under a known Calibre series is rejected")
        check_in("existing series", str(rejected).lower(), "the rejection explains why")

        # The current series spelling is also rejected ("not this series").
        current_rejected = None
        try:
            ctx.tm.rebind_release(
                release_id, mode="new_title", target_name="Standalone Side Story", actor="user",
            )
        except Exception as exc:
            current_rejected = exc
        check(current_rejected is not None, "re-indexing under the current series is rejected")
    finally:
        ctx.close()


def test_per_series_reset_clears_aliases_display_and_pins():
    ctx = Ctx("reset")
    try:
        ctx.db.sync_calibre_series(CALIBRE_SERIES)
        tracker = ctx.db.get_series_by_calibre_id(327)
        check(tracker is not None, "Calibre tracker exists")
        ctx.db.add_series_alias_override(
            "Trapped in a Dating Sim", CALIBRE_SERIES[0]["series_name"], calibre_series_id=327,
            origin="manual", actor="test",
        )
        ctx.db.update_series_identity(
            int(tracker["id"]), display_title=ANILIST_TITLE, anilist_id=991122,
            anilist_meta_json=json.dumps({"english_title": ANILIST_TITLE, "synonyms": [CALIBRE_SERIES[0]["series_name"]]}),
            match_source="anilist",
        )
        release_id, _ = ctx.insert_release(
            title="Pinned Vol 1", series_name=CALIBRE_SERIES[0]["series_name"], volume=1.0,
            status="REQUESTED",
        )
        ctx.db.get_connection().execute(
            "UPDATE releases SET series_pinned = 1 WHERE id = ?;", (release_id,)
        ).connection.commit()

        overview = ctx.db.get_series_identity_overview()
        entry = next((row for row in overview if int(row.get("series_id") or 0) == int(tracker["id"])), None)
        check(entry is not None, "overview lists the tracked series")
        check(entry.get("display_title") == ANILIST_TITLE, "overview exposes the display title")
        check(int(entry.get("aliases_count") or 0) >= 1, "overview counts alias overrides")

        reset = ctx.tm.reset_series_identity(int(tracker["id"]))
        check_eq(reset.get("status"), "success", "reset succeeds")
        check(reset.get("aliases_removed", 0) >= 1, "reset removed the alias override")
        check(reset.get("releases_unpinned", 0) >= 1, "reset unpinned the release")
        check_eq(reset.get("display_cleared"), True, "reset cleared display/AniList data")
        refreshed = ctx.db.get_series_by_calibre_id(327)
        check_eq(refreshed.get("display_title") or "", "", "display title cleared")
        check_eq(int(refreshed.get("identity_locked") or 0), 0, "identity lock cleared")
        check_eq(ctx.db.get_series_alias_overrides(), [], "no alias overrides remain for the series")
        check_eq(ctx.row(release_id).get("series_name"), CALIBRE_SERIES[0]["series_name"],
                 "reset never re-keys non-pinned stable names")
        check_eq(int(ctx.row(release_id).get("series_pinned") or 0), 0, "release unpinned")
    finally:
        ctx.close()


def test_settings_maintenance_features():
    ctx = Ctx("settings-features")
    try:
        # storage health on real temp dirs
        health = diagnostics.storage_health(
            ctx.db, ctx.calibre_dir, ctx.root, ctx.downloads,
        )
        check_in(health.get("status"), ("success", "degraded"), "storage health returns a status")
        check_eq(health.get("data_dir", {}).get("writable"), True, "data dir is writable")
        check_eq(health.get("downloads_dir", {}).get("writable"), True, "downloads dir is writable")
        check_eq(health.get("calibre", {}).get("available"), False,
                 "an empty Calibre path is reported unavailable (not raised)")
        check("database" in health and "row_counts" in health["database"],
              "storage health includes database info")
        check_eq(diagnostics.format_bytes(2048), "2.0 KB", "format_bytes renders KB")

        # attribution audit flags a release filed under the wrong series
        ctx.db.sync_calibre_series(CALIBRE_SERIES)
        release_id, _ = ctx.insert_release(
            title=CALIBRE_SERIES[0]["series_name"] + " Vol 4",
            series_name="Some Wrong Series",
            volume=4.0,
            status="REQUESTED",
        )
        audit = diagnostics.series_attribution_audit(ctx.db, limit=50)
        check(audit.get("count", 0) >= 1, "attribution audit flags the mismatched release")
        item = next(item for item in audit["items"] if int(item["release_id"]) == release_id)
        check_eq(item.get("parsed_series"), CALIBRE_SERIES[0]["series_name"],
                 "audit suggests the title-parsed series")

        # activity log retention + prune-now
        for idx in range(5):
            ctx.db.log_activity("TEST_EVENT", "recent log %d" % idx)
        with ctx.db.get_connection() as conn:
            conn.execute(
                "UPDATE activity_logs SET created_at = datetime('now', '-200 days') "
                "WHERE message = 'recent log 0';"
            )
            conn.commit()
        ctx.db.set_setting("activity_retention_days", "30")
        ctx.db.set_setting("activity_max_rows", "100")
        pruned = ctx.tm.prune_activity_logs()
        check(pruned.get("removed", 0) >= 1, "prune removed the old activity row: %r" % (pruned,))
        check(pruned.get("remaining", 0) >= 1, "prune kept the recent rows")

        # database stats + vacuum + backup
        stats = ctx.db.database_stats()
        check(stats.get("size_bytes", 0) > 0, "database stats report a size")
        check(stats.get("row_counts", {}).get("releases", 0) >= 1, "database stats count releases")
        vacuum = ctx.db.vacuum_database()
        check_eq(vacuum.get("ok"), True, "vacuum succeeds on a temp database: %r" % (vacuum,))
        check_in(vacuum.get("integrity"), ("ok",), "integrity check passes")
        backup_path = os.path.join(ctx.root, "backup.db")
        ctx.db.backup_database(backup_path)
        check(os.path.isfile(backup_path) and os.path.getsize(backup_path) > 0,
              "database backup file is created")
        with sqlite3.connect(backup_path) as conn:
            count = conn.execute("SELECT count(*) FROM releases;").fetchone()[0]
        check(count >= 1, "backup database is readable and contains the releases")
    finally:
        ctx.close()


def test_main_py_and_schema_static_guards():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    db_path = os.path.join(BACKEND_DIR, "app", "database.py")
    main_source = open(main_path, "r", encoding="utf-8").read()
    db_source = open(db_path, "r", encoding="utf-8").read()
    for route in (
        '@app.post("/api/releases/{release_id}/rebind")',
        '@app.post("/api/series/{series_id}/reset-identity")',
        '@app.get("/api/series/identity")',
        '@app.get("/api/releases/{release_id}/series-identity")',
        '@app.post("/api/releases/{release_id}/unpin")',
        '@app.get("/api/storage/health")',
        '@app.get("/api/diagnostics/network")',
        '@app.post("/api/maintenance/logs/prune")',
        '@app.get("/api/maintenance/database/stats")',
        '@app.get("/api/maintenance/backup")',
        '@app.post("/api/maintenance/database/vacuum")',
        '@app.get("/api/maintenance/series-attribution")',
    ):
        check_in(route, main_source, "main.py declares %s" % route)
    for literal in (
        "series_pinned",
        "identity_locked",
        "def rebind_release_series",
        "def reset_series_identity",
        "def prune_activity_logs",
        "def database_stats",
        "def vacuum_database",
        "def backup_database",
    ):
        check_in(literal, db_source, "database.py implements %r" % literal)
    check_in("clear-metadata", main_source, "debug/action supports clear-metadata")
    check("NOVELTRAX_DEBUG_ACTIONS" not in main_source,
          "no accidental debug gate was introduced by this change")
    check_not_in("task_manager.scanner.metadata_db_exists()", main_source,
                 "the old broken scanner call stays gone")


def main():
    tests = [
        ("re-bind to Calibre is durable", test_rebind_to_calibre_is_durable_against_reingest),
        ("new-title re-index is not linked", test_new_title_reindex_is_not_linked_to_previous_series),
        ("detach creates a standalone series", test_detach_creates_a_standalone_series),
        ("per-series reset clears identity state", test_per_series_reset_clears_aliases_display_and_pins),
        ("settings/maintenance features", test_settings_maintenance_features),
        ("main.py/schema guards", test_main_py_and_schema_static_guards),
    ]
    failures = []
    print("=" * 78)
    print("Release re-bind / identity reset / settings integration suite")
    print("=" * 78)
    for label, test in tests:
        print("\n--- %s ---" % label)
        try:
            test()
            print("  [PASS] %s" % label)
        except Exception as exc:  # noqa: BLE001 - reported as suite failure
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
