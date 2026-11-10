#!/usr/bin/env python3
"""Release re-bind / "different series" + per-series identity reset DB tests (v2 §2, §6).

Covers the owner-A persistence layer of CONTRACT_V2_REBIND:

* guarded ``releases.series_pinned``/``series_pinned_at`` and
  ``series_tracking.identity_locked`` columns (idempotent migrations);
* ``rebind_release_series`` in ``bind`` / ``new_title`` / ``different_series`` /
  ``detach`` modes (pin, canonical target name, tracker creation via the existing
  link helper, validation of genuinely-new titles, alias override only with
  ``remember=True``, activity log);
* ``reset_series_identity`` (aliases/display/lock/pins cleared, tracker and
  releases kept, per-series partial flags);
* ``get_series_identity_overview`` / ``get_release_series_identity`` /
  ``unpin_release_series``;
* pinned releases survive ``upsert_release`` re-ingestion and are never re-keyed
  by link/merge/auto-link; identity-locked trackers are skipped by auto-link,
  duplicate merges and AniList enrichment;
* settings defaults + maintenance helpers from §6: ``prune_activity_logs``,
  ``database_stats``, ``vacuum_database``, ``backup_database`` and
  ``get_series_attribution_rows``.

Hermetic: temp DBs directly under ``tempfile.gettempdir()`` (``tempfile.mkdtemp``
is unusable in this sandbox), no network, no bare ``assert`` (every check raises
``AssertionError`` because Calibre's interpreter runs with ``-O2``).
"""

import gc
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
for _path in (BASE_DIR, BACKEND_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.database import Database
from app.parser import normalize_text

PASSED = 0
FAILED = 0
FAILURES = []
_TEMP_COUNTER = 0

CALIBRE_SERIES = "Trapped in a Dating Sim: The World of Otome Games is Tough for Us, Too"
DISPLAY_TITLE = "Trapped in a Dating Sim: Otome Games Are Tough for Us, Too!"
OTHER_SERIES = "Completely Different Franchise"


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


def check_raises(exc_type, func, message, contains=None):
    try:
        func()
    except exc_type as exc:
        if contains is not None:
            check(
                contains in str(exc),
                f"{message} (expected {contains!r} in {str(exc)!r})",
            )
        else:
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
        "noveltrax-rebind-db-%d-%d-%s" % (os.getpid(), _TEMP_COUNTER, label),
    )
    os.makedirs(path, exist_ok=True)
    return path


def _cleanup_temp_dir(path):
    try:
        gc.collect()
    except Exception:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _insert_tracker(db, name, calibre_id=None, is_monitored=1, display_title=None,
                    identity_locked=0, match_source="", authors="", anilist_id=None,
                    anilist_meta_json=None, owned_volumes=None):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO series_tracking
                (name, calibre_series_id, is_monitored, display_title,
                 identity_locked, match_source, authors, anilist_id,
                 anilist_meta_json, owned_volumes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                name, calibre_id, is_monitored, display_title or "",
                identity_locked, match_source or "", authors or "", anilist_id,
                anilist_meta_json or "", json.dumps(owned_volumes or []),
            ),
        )
        conn.commit()
    row = db.get_series_by_name(name)
    return row


def _release_row(db, guid):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM releases WHERE guid = ?;", (guid,))
        row = cur.fetchone()
        return dict(row) if row else None


def _add_release(db, guid, series_name, volume=1.0, status="NEW",
                 title=None):
    db.upsert_release({
        "guid": guid,
        "title": title or f"{series_name} Vol {volume}",
        "series_name": series_name,
        "volume": volume,
        "status": status,
    })
    row = _release_row(db, guid)
    if row is None:
        raise AssertionError(f"release fixture {guid!r} was not created")
    return row


def _pin_directly(db, release_id, series_name):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE releases SET series_name = ?, series_pinned = 1, "
            "series_pinned_at = '2026-01-01T00:00:00Z' WHERE id = ?;",
            (series_name, release_id),
        )
        conn.commit()


def _logs_of_type(db, event_type, limit=200):
    try:
        return [
            entry for entry in db.get_logs(limit=limit)
            if entry.get("event_type") == event_type
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 1. Migrations + settings defaults
# ---------------------------------------------------------------------------
def test_migrations_and_settings_defaults():
    section("1. guarded v2 columns, idempotent init_db, new settings defaults")
    tmp_dir = _make_temp_dir("migrations")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        for _ in range(3):
            db.init_db()

        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(releases);")
            release_columns = {row["name"] for row in cur.fetchall()}
            cur.execute("PRAGMA table_info(series_tracking);")
            tracking_columns = {row["name"] for row in cur.fetchall()}
        for column in ("series_pinned", "series_pinned_at"):
            check(column in release_columns, f"releases has the {column} column")
        check("identity_locked" in tracking_columns,
              "series_tracking has the identity_locked column")

        settings = db.get_all_settings()
        check_equal(settings.get("app_timezone"), "", "app_timezone defaults to empty")
        check_equal(settings.get("log_level"), "INFO", "log_level defaults to INFO")
        check_equal(settings.get("activity_retention_days"), "90",
                    "activity_retention_days defaults to 90")
        check_equal(settings.get("activity_max_rows"), "50000",
                    "activity_max_rows defaults to 50000")
        check_equal(settings.get("webhook_new_request_notifications"), "true",
                    "webhook_new_request_notifications defaults to true")
        check_equal(settings.get("webhook_acquired_notifications"), "true",
                    "webhook_acquired_notifications defaults to true")

        # A user-set value survives repeated init_db (INSERT OR IGNORE).
        db.set_setting("log_level", "DEBUG")
        db.init_db()
        check_equal(db.get_setting("log_level"), "DEBUG",
                    "an existing setting value is not overwritten by init_db")

        # Defaults on the column-adding migration path for a legacy DB.
        _insert_tracker(db, CALIBRE_SERIES, calibre_id=327)
        row = db.get_series_by_name(CALIBRE_SERIES)
        check_equal(row["identity_locked"], 0, "existing trackers default identity_locked=0")
        release = _add_release(db, "mig-release", CALIBRE_SERIES)
        check_equal(release["series_pinned"], 0, "existing releases default series_pinned=0")
        check_equal(release["series_pinned_at"], "", "existing releases default pinned_at=''")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 2. bind mode
# ---------------------------------------------------------------------------
def test_rebind_bind_mode():
    section("2. rebind bind: existing target, canonical name, pin, no alias by default")
    tmp_dir = _make_temp_dir("bind")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        _insert_tracker(db, CALIBRE_SERIES, calibre_id=327)
        old_series = "Old Untracked Spelling"
        release = _add_release(db, "bind-r1", old_series, 3.0, "REQUESTED")

        result = db.rebind_release_series(
            release["id"], "bind", calibre_series_id=327, actor="tester"
        )
        check_equal(result["status"], "success", "bind returns status success")
        check_equal(result["mode"], "bind", "bind echoes the mode")
        check_equal(result["series_name"], CALIBRE_SERIES,
                    "bind uses the canonical tracked series name")
        check_equal(result["calibre_series_id"], 327, "bind returns the Calibre id")
        check_equal(result["pinned"], True, "bind pins the release")
        check_equal(result["created_series"], False, "bind to an existing tracker does not create one")
        check_equal(result["alias_recorded"], False,
                    "bind without remember creates no alias override")
        check_equal(result["previous_series"], old_series,
                    "bind reports the previous series spelling")

        stored = _release_row(db, "bind-r1")
        check_equal(stored["series_name"], CALIBRE_SERIES,
                    "stored release series_name moved to the canonical name")
        check_equal(stored["series_pinned"], 1, "stored release is pinned")
        check(bool(stored["series_pinned_at"]), "stored release has a pin timestamp")
        check_equal(db.get_series_alias_overrides(), [],
                    "bind without remember leaves the alias table untouched")

        identity = db.get_release_series_identity(release["id"])
        check(identity is not None, "get_release_series_identity returns a row")
        check_equal(identity["series_pinned"], True, "release identity shows the pin")
        check_equal(identity["tracking_series"]["name"], CALIBRE_SERIES,
                    "release identity resolves the tracking series")
        check_equal(identity["tracking_series"]["calibre_series_id"], 327,
                    "release identity exposes the tracking Calibre id")
        check_equal(identity["volume"], 3.0, "release identity keeps the volume")

        # Bind by exact tracked name (no Calibre id needed).
        _insert_tracker(db, "User Tracked Series", is_monitored=1)
        release2 = _add_release(db, "bind-r2", old_series, 2.0, "NEW")
        result2 = db.rebind_release_series(
            release2["id"], "bind", target_name="User Tracked Series"
        )
        check_equal(result2["series_name"], "User Tracked Series",
                    "bind by target_name resolves the tracked row")
        check_equal(result2["calibre_series_id"], None,
                    "a tracker without a Calibre id binds with calibre None")

        # remember=True records a manual alias override.
        release3 = _add_release(db, "bind-r3", "Remembered Old Spelling", 1.0, "NEW")
        result3 = db.rebind_release_series(
            release3["id"], "bind", target_name="User Tracked Series",
            remember=True, actor="tester",
        )
        check_equal(result3["alias_recorded"], True,
                    "bind with remember records an alias override")
        overrides = db.get_series_alias_overrides()
        remembered = next(
            (row for row in overrides
             if normalize_text(row["source_name"]) == normalize_text("Remembered Old Spelling")),
            None,
        )
        check(remembered is not None, "the remembered alias override exists")
        check_equal(remembered["target_name"], "User Tracked Series",
                    "remembered alias targets the canonical name")
        check_equal(remembered["origin"], "manual", "remembered alias origin is manual")

        # Validation / errors.
        check_raises(
            ValueError,
            lambda: db.rebind_release_series(release["id"], "bind"),
            "bind without target or Calibre id raises ValueError",
        )
        check_raises(
            LookupError,
            lambda: db.rebind_release_series(release["id"], "bind", calibre_series_id=99999),
            "bind to an unknown Calibre id without a name raises LookupError",
        )
        check_raises(
            KeyError,
            lambda: db.rebind_release_series(999999, "bind", calibre_series_id=327),
            "rebinding a missing release raises KeyError",
        )
        check_raises(
            ValueError,
            lambda: db.rebind_release_series(release["id"], "not-a-mode", target_name="X"),
            "an unknown rebind mode raises ValueError",
        )
        logs = _logs_of_type(db, "RELEASE_SERIES_REBOUND")
        check_equal(len(logs), 3, "every successful bind logged RELEASE_SERIES_REBOUND")
        details = json.loads(logs[0]["details"])
        for key in ("mode", "previous_series", "new_series", "calibre_series_id",
                    "remember", "pinned"):
            check(key in details, f"rebind activity details carry {key}")
    finally:
        _cleanup_temp_dir(tmp_dir)


def test_rebind_bind_creates_target():
    section("3. rebind bind creates a missing target tracker via the link helper")
    tmp_dir = _make_temp_dir("bind-create")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        release = _add_release(db, "bind-new", "Some Discovery Series", 1.0, "NEW")
        result = db.rebind_release_series(
            release["id"], "bind",
            target_name="Brand New Calibre Series",
            calibre_series_id=555,
        )
        check_equal(result["created_series"], True,
                    "bind creates a tracker that did not exist")
        check_equal(result["series_name"], "Brand New Calibre Series",
                    "created tracker uses the requested target name")
        check_equal(result["calibre_series_id"], 555, "created tracker carries the Calibre id")
        created = db.get_series_by_calibre_id(555)
        check(created is not None, "the created tracker exists")
        check_equal(created["name"], "Brand New Calibre Series",
                    "created tracker name is the canonical target")
        check_equal(created["is_monitored"], 1, "created tracker is monitored")
        check_equal(_release_row(db, "bind-new")["series_name"], "Brand New Calibre Series",
                    "release was moved to the created tracker")
        check_equal(_release_row(db, "bind-new")["series_pinned"], 1,
                    "release was pinned to the created tracker")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 4. new_title / different_series mode
# ---------------------------------------------------------------------------
def test_rebind_new_title_mode():
    section("4. rebind new_title/different_series: locked new tracker, validation")
    tmp_dir = _make_temp_dir("new-title")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        _insert_tracker(db, CALIBRE_SERIES, calibre_id=327, display_title=DISPLAY_TITLE)
        db.add_series_alias_override("Known Alias Source", CALIBRE_SERIES, 327,
                                     origin="manual")
        old_series = "Known Existing Series"
        _insert_tracker(db, old_series)
        release = _add_release(db, "new-r1", old_series, 2.0, "REQUESTED")
        previous_override_count = len(db.get_series_alias_overrides())
        previous_title = release["title"]

        new_name = "A Genuinely New Series Title"
        result = db.rebind_release_series(
            release["id"], "new_title", target_name=new_name, actor="tester"
        )
        check_equal(result["status"], "success", "new_title returns success")
        check_equal(result["mode"], "new_title", "new_title echoes the mode")
        check_equal(result["series_name"], new_name, "release gets the new title")
        check_equal(result["calibre_series_id"], None, "new_title has no Calibre id")
        check_equal(result["created_series"], True, "new_title creates a tracker")
        check_equal(result["alias_recorded"], False, "new_title creates no alias override")

        stored = _release_row(db, "new-r1")
        check_equal(stored["series_name"], new_name, "stored release uses the new title")
        check_equal(stored["series_pinned"], 1, "stored release is pinned")
        check_equal(stored["volume"], 2.0, "new_title keeps the release volume")
        check_equal(stored["title"], previous_title, "new_title keeps the release title")

        tracker = db.get_series_by_name(new_name)
        check(tracker is not None, "the new tracker exists")
        check_equal(tracker["identity_locked"], 1, "the new tracker is identity-locked")
        check_equal(tracker["match_source"], "manual-new", "match_source is 'manual-new'")
        check_equal(tracker["is_monitored"], 1, "the new tracker is monitored")
        check_contains = check
        check_contains("jnovels.com" in (tracker.get("website_url") or ""),
                       "the new tracker gets the default website search")
        check_equal(len(db.get_series_alias_overrides()), previous_override_count,
                    "new_title never adds an alias override")
        check_equal(db.get_series_by_name(old_series)["identity_locked"], 0,
                    "the previous tracker is not touched")

        # different_series is an alias of new_title.
        release2 = _add_release(db, "new-r2", old_series, 1.0, "NEW")
        result2 = db.rebind_release_series(
            release2["id"], "different_series", target_name="Another Brand New Series"
        )
        check_equal(result2["mode"], "new_title",
                    "different_series is normalised to new_title")
        check_equal(db.get_series_by_name("Another Brand New Series")["identity_locked"], 1,
                    "different_series also creates a locked tracker")

        # Validation: same/known/blank/invalid titles are rejected.
        validation_release = _add_release(db, "new-r3", old_series, 1.0, "NEW")
        conflict_message = "that title already belongs to an existing series"
        for bad_title, label in (
            (old_series, "the current series name"),
            (CALIBRE_SERIES, "an existing tracked Calibre series"),
            (DISPLAY_TITLE, "an existing display title"),
            ("Known Alias Source", "an existing alias source"),
            ("Another Brand New Series", "an existing new tracker"),
        ):
            check_raises(
                ValueError,
                lambda bad_title=bad_title: db.rebind_release_series(
                    validation_release["id"], "new_title", target_name=bad_title
                ),
                f"new_title rejects {label}",
                contains=conflict_message,
            )
        check_raises(
            ValueError,
            lambda: db.rebind_release_series(validation_release["id"], "new_title",
                                             target_name=""),
            "new_title rejects a blank title",
        )
        check_raises(
            ValueError,
            lambda: db.rebind_release_series(validation_release["id"], "new_title",
                                             target_name="A"),
            "new_title rejects a single-character title",
        )
        check_raises(
            ValueError,
            lambda: db.rebind_release_series(validation_release["id"], "new_title",
                                             target_name="X" * 301),
            "new_title rejects a >300-character title",
        )
        check_raises(
            ValueError,
            lambda: db.rebind_release_series(validation_release["id"], "new_title",
                                             target_name="***"),
            "new_title rejects a title with no letters/digits",
        )
        check_equal(_release_row(db, "new-r3")["series_pinned"], 0,
                    "a rejected new_title leaves the release unpinned")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 5. detach mode
# ---------------------------------------------------------------------------
def test_rebind_detach_mode():
    section("5. rebind detach: derived title, new locked tracker, collision guard")
    tmp_dir = _make_temp_dir("detach")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        _insert_tracker(db, "Old Known Series", is_monitored=1)
        # Title parses to the same series -> detach without a title must fail.
        same_release = _add_release(
            db, "detach-same", "Old Known Series", 1.0, "NEW",
            title="Old Known Series Vol 1",
        )
        check_raises(
            ValueError,
            lambda: db.rebind_release_series(same_release["id"], "detach"),
            "detach without a title fails when the parsed series is the current one",
            contains="that title already belongs to an existing series",
        )
        # Title parses to a brand-new series -> detach derives and creates it.
        fresh_release = _add_release(
            db, "detach-fresh", "Old Known Series", 2.0, "NEW",
            title="Fresh Detached Title Vol 2",
        )
        result = db.rebind_release_series(fresh_release["id"], "detach")
        check_equal(result["status"], "success", "detach returns success")
        check_equal(result["mode"], "detach", "detach echoes the mode")
        check_equal(result["series_name"], "Fresh Detached Title",
                    "detach derives the new series from the release title")
        check_equal(result["created_series"], True, "detach creates a tracker")
        detached = db.get_series_by_name("Fresh Detached Title")
        check(detached is not None, "the detached tracker exists")
        check_equal(detached["identity_locked"], 1, "the detached tracker is locked")
        check_equal(detached["match_source"], "manual-new",
                    "the detached tracker has match_source='manual-new'")
        check_equal(_release_row(db, "detach-fresh")["series_pinned"], 1,
                    "the detached release is pinned")
        check_equal(db.get_series_alias_overrides(), [],
                    "detach creates no alias override")

        # detach with an explicit valid title.
        explicit_release = _add_release(
            db, "detach-explicit", "Old Known Series", 3.0, "NEW",
            title="Old Known Series Vol 3",
        )
        explicit = db.rebind_release_series(
            explicit_release["id"], "detach", target_name="Operator Supplied Title"
        )
        check_equal(explicit["series_name"], "Operator Supplied Title",
                    "detach honours an explicit new title")
        check(db.get_series_by_name("Operator Supplied Title") is not None,
              "the explicit detached tracker exists")

        # detach with an explicit title that collides with a known series.
        collision_release = _add_release(
            db, "detach-collision", "Old Known Series", 4.0, "NEW",
            title="Old Known Series Vol 4",
        )
        check_raises(
            ValueError,
            lambda: db.rebind_release_series(
                collision_release["id"], "detach", target_name="Old Known Series"
            ),
            "detach rejects an explicit title that equals a known series",
            contains="that title already belongs to an existing series",
        )
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 6. reset_series_identity
# ---------------------------------------------------------------------------
def test_reset_series_identity():
    section("6. reset_series_identity: aliases/display/lock/pins cleared, data kept")
    tmp_dir = _make_temp_dir("reset")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        series = _insert_tracker(
            db, CALIBRE_SERIES, calibre_id=327,
            display_title=DISPLAY_TITLE,
            identity_locked=1,
            match_source="anilist",
            anilist_id=98765,
            anilist_meta_json=json.dumps({"title": DISPLAY_TITLE}),
            owned_volumes=[1.0, 2.0],
        )
        _insert_tracker(db, OTHER_SERIES, calibre_id=5)
        db.add_series_alias_override("Old Alias A", CALIBRE_SERIES, 327, origin="manual")
        db.add_series_alias_override("Old Alias B", CALIBRE_SERIES, 327, origin="anilist")
        db.add_series_alias_override(CALIBRE_SERIES, OTHER_SERIES, 5, origin="manual")
        db.add_series_alias_override(DISPLAY_TITLE, CALIBRE_SERIES, 327, origin="manual")
        db.add_series_alias_override("Unrelated Alias", OTHER_SERIES, 5, origin="manual")

        own_release = _add_release(db, "reset-r1", CALIBRE_SERIES, 1.0, "REQUESTED")
        _pin_directly(db, own_release["id"], CALIBRE_SERIES)
        display_release = _add_release(db, "reset-r2", DISPLAY_TITLE, 1.0, "NEW")
        _pin_directly(db, display_release["id"], DISPLAY_TITLE)
        other_release = _add_release(db, "reset-r3", OTHER_SERIES, 1.0, "REQUESTED")
        _pin_directly(db, other_release["id"], OTHER_SERIES)
        other_count_before = len(db.get_releases(limit=100))

        result = db.reset_series_identity(series["id"], actor="tester")
        check_equal(result["status"], "success", "reset returns status success")
        check_equal(result["series_id"], series["id"], "reset reports the series id")
        check_equal(result["series_name"], CALIBRE_SERIES, "reset reports the stable name")
        check_equal(result["aliases_removed"], 4,
                    "reset removed aliases targeting or sourced from the series")
        check_equal(result["releases_unpinned"], 1,
                    "reset unpinned the release stored under the stable name")
        check_equal(result["display_cleared"], True, "reset reports display cleared")
        check_equal(result["locked_cleared"], True, "reset reports the lock cleared")
        check_equal(result["releases_rekeyed"], 0,
                    "a non-manual release title parses back to the same name (no rekey)")

        stored = db.get_series_by_name(CALIBRE_SERIES)
        check(stored is not None, "the tracker row still exists after reset")
        check_equal(stored["calibre_series_id"], 327,
                    "reset keeps the Calibre linkage (library fact, not identity state)")
        check_equal(_release_row(db, "reset-r1")["series_name"], CALIBRE_SERIES,
                    "reset keeps the release series name when the parsed title matches")
        check_equal(stored["display_title"], "", "display_title cleared")
        check_equal(stored["anilist_id"], None, "anilist_id cleared")
        check_equal(stored["anilist_meta_json"], "", "anilist_meta_json cleared")
        check_equal(stored["match_source"], "", "match_source cleared")
        check_equal(stored["identity_locked"], 0, "identity_locked cleared")
        check_equal(stored["name"], CALIBRE_SERIES, "the stable name is kept")
        check_equal(len(db.get_releases(limit=100)), other_count_before,
                    "reset never deletes releases")

        check_equal(_release_row(db, "reset-r1")["series_pinned"], 0,
                    "the series' pinned release was unpinned")
        check_equal(_release_row(db, "reset-r1")["series_pinned_at"], "",
                    "the unpinned release has an empty pin timestamp")
        check_equal(_release_row(db, "reset-r2")["series_pinned"], 1,
                    "a pinned release stored under another spelling is untouched")
        check_equal(_release_row(db, "reset-r3")["series_pinned"], 1,
                    "another series' pinned release is untouched")
        remaining = {
            normalize_text(row["source_name"]) for row in db.get_series_alias_overrides()
        }
        check(normalize_text("Unrelated Alias") in remaining,
              "unrelated alias overrides survive the reset")
        check(normalize_text("Old Alias A") not in remaining,
              "aliases targeting the series were removed")
        check(normalize_text(CALIBRE_SERIES) not in remaining,
              "aliases sourced from the series were removed")
        check_equal(len(_logs_of_type(db, "SERIES_IDENTITY_RESET")), 1,
                    "reset logged SERIES_IDENTITY_RESET")

        # Partial reset flags: aliases/display/pins are optional, lock always clears.
        partial = _insert_tracker(
            db, "Partial Reset Series", identity_locked=1,
            display_title="Partial Display",
        )
        db.add_series_alias_override("Partial Alias", "Partial Reset Series",
                                     origin="manual")
        partial_release = _add_release(db, "reset-r4", "Partial Reset Series", 1.0, "NEW")
        _pin_directly(db, partial_release["id"], "Partial Reset Series")
        partial_result = db.reset_series_identity(
            partial["id"], clear_aliases=False, clear_display=False,
            unpin_releases=False,
        )
        check_equal(partial_result["aliases_removed"], 0, "clear_aliases=False keeps aliases")
        check_equal(partial_result["releases_unpinned"], 0,
                    "unpin_releases=False keeps pins")
        check_equal(partial_result["display_cleared"], False,
                    "clear_display=False reports no display clear")
        partial_stored = db.get_series_by_name("Partial Reset Series")
        check_equal(partial_stored["display_title"], "Partial Display",
                    "clear_display=False keeps the display title")
        check_equal(partial_stored["identity_locked"], 0,
                    "identity_locked is always cleared")
        check_equal(partial_result["locked_cleared"], True,
                    "partial reset still reports the lock cleared")

        check_raises(
            KeyError,
            lambda: db.reset_series_identity(999999),
            "resetting a missing series raises KeyError",
        )
        check_raises(
            ValueError,
            lambda: db.reset_series_identity(None),
            "a missing series id raises ValueError",
        )
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 7. overview / release identity / unpin
# ---------------------------------------------------------------------------
def test_identity_overview_and_unpin():
    section("7. get_series_identity_overview, get_release_series_identity, unpin")
    tmp_dir = _make_temp_dir("overview")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        series = _insert_tracker(
            db, CALIBRE_SERIES, calibre_id=327, display_title=DISPLAY_TITLE,
            identity_locked=1, match_source="manual", authors="Umino Chika",
            owned_volumes=[1.0, 2.0, 3.0],
        )
        _insert_tracker(db, OTHER_SERIES, calibre_id=5, authors="Someone Else")
        db.add_series_alias_override("Overview Alias One", CALIBRE_SERIES,
                                     origin="manual")
        db.add_series_alias_override("Overview Alias Two", CALIBRE_SERIES,
                                     origin="anilist")
        one = _add_release(db, "overview-r1", CALIBRE_SERIES, 1.0, "NEW")
        two = _add_release(db, "overview-r2", CALIBRE_SERIES, 2.0, "REQUESTED")
        _pin_directly(db, one["id"], CALIBRE_SERIES)
        _pin_directly(db, two["id"], CALIBRE_SERIES)
        _add_release(db, "overview-r3", OTHER_SERIES, 1.0, "NEW")

        overview = db.get_series_identity_overview()
        check_equal(len(overview), 2, "overview lists every tracked series")
        row = next(entry for entry in overview if entry["name"] == CALIBRE_SERIES)
        for key in (
            "series_id", "name", "display_title", "calibre_series_id",
            "identity_locked", "match_source", "anilist_id", "aliases_count",
            "pinned_releases", "releases_count", "owned_volumes",
        ):
            check(key in row, f"overview row contains {key}")
        check_equal(row["series_id"], series["id"], "overview carries the series id")
        check_equal(row["display_title"], DISPLAY_TITLE, "overview carries display_title")
        check_equal(row["calibre_series_id"], 327, "overview carries the Calibre id")
        check_equal(row["identity_locked"], True, "overview exposes the identity lock")
        check_equal(row["aliases_count"], 2, "overview counts aliases targeting the series")
        check_equal(row["pinned_releases"], 2, "overview counts pinned releases")
        check_equal(row["releases_count"], 2, "overview counts releases of the series")
        check_equal(row["owned_volumes"], [1.0, 2.0, 3.0],
                    "overview returns parsed owned volumes")

        filtered = db.get_series_identity_overview(search="chika")
        check_equal([entry["name"] for entry in filtered], [CALIBRE_SERIES],
                    "overview search matches authors (normalised substring)")
        filtered_display = db.get_series_identity_overview(search="otome games")
        check_equal([entry["name"] for entry in filtered_display], [CALIBRE_SERIES],
                    "overview search matches the display title")
        filtered_name = db.get_series_identity_overview(search="dating sim")
        check_equal([entry["name"] for entry in filtered_name], [CALIBRE_SERIES],
                    "overview search matches the stable name")
        check_equal(db.get_series_identity_overview(search="no such thing"), [],
                    "overview search with no match returns an empty list")
        limited = db.get_series_identity_overview(limit=0)
        check_equal(len(limited), 1, "overview limit is clamped to at least 1")
        check_equal(len(db.get_series_identity_overview(limit=5000)), 2,
                    "overview limit is clamped to at most 500")

        identity = db.get_release_series_identity(one["id"])
        check(identity is not None, "release identity returned for a known release")
        check_equal(identity["series_pinned"], True, "release identity reports the pin")
        check_equal(identity["series_name"], CALIBRE_SERIES, "release identity series name")
        check_equal(identity["tracking_series"]["display_title"], DISPLAY_TITLE,
                    "release identity exposes the display title")
        check_equal(identity["tracking_series"]["identity_locked"], True,
                    "release identity exposes the identity lock")
        check_equal(db.get_release_series_identity(999999), None,
                    "release identity for a missing release is None")

        check_equal(db.unpin_release_series(one["id"]), True,
                    "unpin returns True for a pinned release")
        check_equal(_release_row(db, "overview-r1")["series_pinned"], 0,
                    "unpin clears series_pinned")
        check_equal(_release_row(db, "overview-r1")["series_pinned_at"], "",
                    "unpin clears the pin timestamp")
        check_equal(db.unpin_release_series(one["id"]), False,
                    "unpin returns False when the release is not pinned")
        check_equal(db.unpin_release_series(999999), False,
                    "unpin returns False for a missing release")
        check_equal(len(_logs_of_type(db, "RELEASE_SERIES_UNPINNED")), 1,
                    "unpin logged RELEASE_SERIES_UNPINNED")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 8. Pinned release survives re-ingestion / canonicalisation
# ---------------------------------------------------------------------------
def test_pinned_survives_reingestion_and_merges():
    section("8. pinned release survives upsert/update and series re-keying")
    tmp_dir = _make_temp_dir("pinned")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        _insert_tracker(db, CALIBRE_SERIES, calibre_id=327)
        old_series = "Old Parsed Series"
        release = _add_release(db, "pinned-r1", old_series, 1.0, "NEW")
        pinned_name = "User Chosen New Series"
        db.rebind_release_series(release["id"], "new_title", target_name=pinned_name)
        check_equal(_release_row(db, "pinned-r1")["series_pinned"], 1,
                    "release is pinned after new_title")

        # Feed re-ingestion with the same GUID and the old parsed series name.
        db.upsert_release({
            "guid": "pinned-r1",
            "title": "Old Parsed Series Vol 1 (Digital)",
            "series_name": old_series,
            "volume": 1.0,
            "status": "NEW",
        })
        stored = _release_row(db, "pinned-r1")
        check_equal(stored["series_name"], pinned_name,
                    "pinned release keeps its series_name through upsert_release")
        check_equal(stored["series_pinned"], 1,
                    "pinned release stays pinned through upsert_release")

        db.update_release_details(release["id"], series_name="Another Title Payload")
        check_equal(_release_row(db, "pinned-r1")["series_name"], pinned_name,
                    "pinned release keeps its series_name through update_release_details")

        # link_series_to_calibre skips pinned releases but re-keys unpinned ones.
        unpinned = _add_release(db, "pinned-r2", old_series, 2.0, "NEW")
        source_tracker = _insert_tracker(db, old_series, is_monitored=1)
        result = db.link_series_to_calibre(
            old_series,
            {"series_name": CALIBRE_SERIES, "series_id": 327, "book_count": 9},
            origin="manual",
        )
        check_equal(result["rekeyed_releases"], 1,
                    "only the unpinned release was re-keyed by link")
        check_equal(_release_row(db, "pinned-r2")["series_name"], CALIBRE_SERIES,
                    "unpinned release was re-keyed to the Calibre name")
        check_equal(_release_row(db, "pinned-r1")["series_name"], pinned_name,
                    "pinned release survived the series link untouched")
        check(db.get_series_by_name(pinned_name) is not None,
              "the manual new-title tracker still exists after the link")

        # duplicate merges never touch locked trackers.
        locked = _insert_tracker(db, "Locked Manual Series", identity_locked=1)
        # A duplicate spelling that would otherwise be merged into the locked row.
        duplicate = _insert_tracker(
            db, "Locked Manual Series Alias", identity_locked=1,
            anilist_meta_json=json.dumps({"synonyms": ["Locked Manual Series"]}),
        )
        merge_result = db.merge_duplicate_series("Locked Manual Series",
                                                 "Locked Manual Series Alias")
        check_equal(merge_result.get("merged"), False,
                    "merge_duplicate_series refuses a locked drop tracker")
        check_equal(merge_result.get("reason"), "identity_locked",
                    "merge refusal reason is identity_locked")
        check(db.get_series_by_name("Locked Manual Series Alias") is not None,
              "the locked duplicate tracker was not deleted")
        merge_keep_locked = db.merge_duplicate_series("Locked Manual Series Alias",
                                                      "Locked Manual Series")
        check_equal(merge_keep_locked.get("reason"), "identity_locked",
                    "merging into a locked keep tracker is also refused")

        # auto_link skips identity-locked trackers entirely.
        locked_calibre = _insert_tracker(
            db, "Auto Link Should Skip", identity_locked=1,
            anilist_meta_json=json.dumps({"synonyms": ["Overlord"]}),
        )
        linked = db.auto_link_series_to_calibre(
            [{"series_id": 60, "series_name": "Overlord", "book_count": 16}],
            actor="scheduler",
        )
        check_equal(linked, [], "auto_link returns no link for identity-locked rows")
        check_equal(db.get_series_by_name("Auto Link Should Skip")["calibre_series_id"],
                    None, "the identity-locked tracker stays unlinked")

        # AniList enrichment skips identity-locked trackers.
        db.store_series_anilist_metadata(
            locked_calibre["id"],
            {"english_title": "Should Not Apply", "title": "Should Not Apply",
             "synonyms": ["Overlord 2"], "anilist_id": 1, "source": "AniList"},
        )
        locked_stored = db.get_series_by_name("Auto Link Should Skip")
        check_equal(locked_stored["display_title"], "",
                    "enrichment does not set display_title on a locked tracker")
        check_equal(locked_stored["anilist_id"], None,
                    "enrichment does not set anilist_id on a locked tracker")
        override_sources = {
            normalize_text(row["source_name"]) for row in db.get_series_alias_overrides()
        }
        check(normalize_text("Overlord 2") not in override_sources,
              "enrichment does not add alias overrides for a locked tracker")
        # Guard against unused-variable warnings from the fixture rows.
        check(duplicate is not None and locked is not None
              and source_tracker is not None and unpinned is not None,
              "fixtures for the pinned/locked skip checks were created")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 9. Maintenance: activity-log retention
# ---------------------------------------------------------------------------
def test_prune_activity_logs():
    section("9. prune_activity_logs: age rule, row cap, clamps, return shape")
    tmp_dir = _make_temp_dir("prune")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        with db.get_connection() as conn:
            cur = conn.cursor()
            for index in range(3):
                cur.execute(
                    "INSERT INTO activity_logs (event_type, message, created_at) "
                    "VALUES (?, ?, datetime('now', '-200 days'));",
                    ("OLD_EVENT", f"old {index}"),
                )
            for index in range(4):
                cur.execute(
                    "INSERT INTO activity_logs (event_type, message) VALUES (?, ?);",
                    ("NEW_EVENT", f"new {index}"),
                )
            conn.commit()

        aged = db.prune_activity_logs(retention_days=90, max_rows=0)
        check_equal(aged["removed_by_age"], 3, "age rule removed the 3 old rows")
        check_equal(aged["removed_by_rows"], 0, "row cap disabled removes nothing")
        check_equal(aged["removed"], 3, "removed is the sum of both rules")
        check_equal(aged["remaining"], 4, "remaining count is reported")
        check_equal(len(db.get_logs(limit=100)), 4, "only recent rows remain")

        capped = db.prune_activity_logs(retention_days=0, max_rows=2)
        check_equal(capped["removed_by_age"], 0, "age rule disabled removes nothing")
        check_equal(capped["removed_by_rows"], 2, "row cap keeps only the newest 2 rows")
        check_equal(capped["remaining"], 2, "row cap reports the remaining count")

        clamped = db.prune_activity_logs(retention_days=-10, max_rows=10 ** 9)
        check_equal(clamped["removed"], 0, "clamped inputs delete nothing")

        noop = db.prune_activity_logs(retention_days=90, max_rows=50000)
        check_equal(noop["removed"], 0, "a no-op prune removes nothing")
        check_equal(noop["remaining"], 2, "a no-op prune keeps the rows")
        check_equal(set(noop.keys()),
                    {"removed_by_age", "removed_by_rows", "removed", "remaining"},
                    "prune returns exactly the documented keys")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 10. Maintenance: stats / vacuum / backup / attribution rows
# ---------------------------------------------------------------------------
def test_database_stats_and_maintenance():
    section("10. database_stats, vacuum_database, backup_database, attribution rows")
    tmp_dir = _make_temp_dir("maintenance")
    try:
        db_path = os.path.join(tmp_dir, "app.db")
        db = Database(db_path)
        _insert_tracker(db, CALIBRE_SERIES, calibre_id=327)
        _add_release(db, "maint-r1", CALIBRE_SERIES, 1.0, "NEW")
        _add_release(db, "maint-r2", "Ignored Series", 1.0, "IGNORED")
        _add_release(db, "maint-r3", "Blacklisted Series", 1.0, "BLACKLISTED")
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE releases SET ignored = 1 WHERE guid = 'maint-r1';"
            )
            conn.commit()
        release = _add_release(db, "maint-r4", "Pinned Discovery", 1.0, "NEW")
        _pin_directly(db, release["id"], "Pinned Discovery")
        _add_release(db, "maint-r5", "Clean Attribution Candidate", 2.0, "REQUESTED")

        stats = db.database_stats()
        for key in (
            "path", "size_bytes", "wal_size_bytes", "page_count", "page_size",
            "freelist_count", "row_counts",
        ):
            check(key in stats, f"database_stats contains {key}")
        check_equal(stats["path"], db_path, "database_stats reports the DB path")
        check(stats["size_bytes"] > 0, "database_stats reports a positive DB size")
        check(stats["page_size"] > 0, "database_stats reports the page size")
        check(stats["page_count"] > 0, "database_stats reports the page count")
        for table in (
            "releases", "series_tracking", "activity_logs",
            "novel_metadata_cache", "feeds", "series_alias_overrides",
        ):
            check(table in stats["row_counts"], f"database_stats counts {table} rows")
        check_equal(stats["row_counts"]["releases"], 5,
                    "database_stats counts all releases")
        check_equal(stats["row_counts"]["series_tracking"], 1,
                    "database_stats counts tracked series")

        vacuum = db.vacuum_database()
        check_equal(vacuum["ok"], True, "vacuum_database succeeds on a temp DB")
        check_equal(vacuum["integrity"], "ok", "integrity_check reports ok")
        check(vacuum["before_bytes"] > 0, "vacuum reports the before size")
        check(vacuum["after_bytes"] > 0, "vacuum reports the after size")
        check(bool(vacuum["message"]), "vacuum reports a human message")
        check(db.get_series_by_name(CALIBRE_SERIES) is not None,
              "database is still readable after vacuum")

        backup_path = os.path.join(tmp_dir, "backups", "noveltrax-backup.db")
        returned = db.backup_database(backup_path)
        check_equal(returned, os.path.abspath(backup_path),
                    "backup_database returns the destination path")
        check(os.path.isfile(backup_path), "backup file exists")
        backup_conn = sqlite3.connect(backup_path)
        try:
            backup_count = backup_conn.execute("SELECT COUNT(*) FROM releases;").fetchone()[0]
            backup_tracking = backup_conn.execute(
                "SELECT COUNT(*) FROM series_tracking;"
            ).fetchone()[0]
        finally:
            backup_conn.close()
        check_equal(backup_count, 5, "backup contains the release rows")
        check_equal(backup_tracking, 1, "backup contains the series rows")
        check_raises(
            ValueError,
            lambda: db.backup_database(""),
            "backup_database rejects an empty destination",
        )

        attribute_rows = db.get_series_attribution_rows(scan_limit=50000)
        returned_titles = {row["title"] for row in attribute_rows}
        check(any("Clean Attribution Candidate" in title for title in returned_titles),
              "attribution rows include eligible releases")
        check(not any("Ignored Series" in (row["series_name"] or "")
                      for row in attribute_rows),
              "attribution rows exclude IGNORED releases")
        check(not any("Blacklisted Series" in (row["series_name"] or "")
                      for row in attribute_rows),
              "attribution rows exclude BLACKLISTED releases")
        check(not any(row["series_pinned"] for row in attribute_rows),
              "attribution rows exclude pinned releases")
        check(not any(
            normalize_text(row["series_name"]) == normalize_text(CALIBRE_SERIES)
            for row in attribute_rows
        ), "attribution rows exclude flag-ignored releases")
        for key in ("id", "title", "series_name", "volume", "status",
                    "series_pinned", "ignored", "media_type"):
            check(key in attribute_rows[0], f"attribution row contains {key}")
        limited_rows = db.get_series_attribution_rows(scan_limit=1)
        check_equal(len(limited_rows), 1, "attribution scan_limit limits the returned rows")
        check_equal(len(db.get_series_attribution_rows(scan_limit=-5)), 1,
                    "attribution scan_limit is clamped to at least 1")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 11. reset rekey_to_parsed: undo a manual re-index
# ---------------------------------------------------------------------------
def test_reset_rekeys_manual_series():
    section("11. reset rekey_to_parsed restores title-parsed names for manual series")
    tmp_dir = _make_temp_dir("reset-rekey")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        # Manual new_title flow: the release title still parses to the old series.
        release = _add_release(
            db, "rekey-r1", "Original Parsed Name", 1.0, "NEW",
            title="Original Parsed Name Vol 1",
        )
        db.rebind_release_series(release["id"], "new_title",
                                 target_name="Manual Custom Identity")
        tracker = db.get_series_by_name("Manual Custom Identity")
        check(tracker is not None, "the manual new-title tracker exists")
        check_equal(tracker["identity_locked"], 1, "the manual tracker is locked")
        check_equal(tracker["match_source"], "manual-new",
                    "the manual tracker is marked manual-new")
        # Simulate a later Calibre link on the manual tracker: reset must keep it.
        db.update_series_identity(tracker["id"], calibre_series_id=777)
        # Second release under the manual identity, with a different parsed title.
        _add_release(
            db, "rekey-r2", "Manual Custom Identity", 2.0, "NEW",
            title="Another Parsed Title Vol 2",
        )
        # A release of a different series must never be touched.
        _add_release(db, "rekey-r3", "Unrelated Series", 1.0, "NEW")

        result = db.reset_series_identity(tracker["id"])
        check_equal(result["releases_rekeyed"], 2,
                    "reset re-keyed the manual series' releases to parsed titles")
        check_equal(result["releases_unpinned"], 1,
                    "reset unpinned the manually re-bound release")
        check_equal(_release_row(db, "rekey-r1")["series_name"], "Original Parsed Name",
                    "the first release is restored to its title-parsed series")
        check_equal(_release_row(db, "rekey-r2")["series_name"], "Another Parsed Title",
                    "the second release is restored to its title-parsed series")
        check_equal(_release_row(db, "rekey-r3")["series_name"], "Unrelated Series",
                    "releases of other series are untouched by the reset")
        check_equal(_release_row(db, "rekey-r1")["series_pinned"], 0,
                    "the restored release is unpinned")
        restored_tracker = db.get_series_by_name("Manual Custom Identity")
        check(restored_tracker is not None, "reset keeps the manual tracker row")
        check_equal(restored_tracker["identity_locked"], 0,
                    "reset clears the manual identity lock")
        check_equal(restored_tracker["match_source"], "", "reset clears match_source")
        check_equal(restored_tracker["calibre_series_id"], 777,
                    "reset keeps the Calibre linkage of the tracker")

        # rekey_to_parsed=False keeps the manual re-index in place.
        opt_out_release = _add_release(
            db, "rekey-r4", "Opt Out Parsed Name", 1.0, "NEW",
            title="Opt Out Parsed Name Vol 1",
        )
        db.rebind_release_series(opt_out_release["id"], "new_title",
                                 target_name="Second Manual Series")
        second_tracker = db.get_series_by_name("Second Manual Series")
        opt_out = db.reset_series_identity(
            second_tracker["id"], rekey_to_parsed=False
        )
        check_equal(opt_out["releases_rekeyed"], 0,
                    "rekey_to_parsed=False disables the parsed re-key")
        check_equal(_release_row(db, "rekey-r4")["series_name"], "Second Manual Series",
                    "the manual identity is kept when rekey_to_parsed=False")
        check_equal(_release_row(db, "rekey-r4")["series_pinned"], 0,
                    "the release is still unpinned when rekey_to_parsed=False")

        # A normal (non-manual) tracker is never re-keyed by reset.
        normal = _insert_tracker(db, "Normal Calibre Series", calibre_id=888)
        _add_release(db, "rekey-r5", "Normal Calibre Series", 1.0, "NEW")
        normal_result = db.reset_series_identity(normal["id"])
        check_equal(normal_result["releases_rekeyed"], 0,
                    "reset never re-keys releases of a non-manual tracker")
        check_equal(_release_row(db, "rekey-r5")["series_name"], "Normal Calibre Series",
                    "the non-manual release name is unchanged")
        check_equal(db.get_series_by_name("Normal Calibre Series")["calibre_series_id"], 888,
                    "reset keeps the non-manual tracker's Calibre id")
        check_equal(len(_logs_of_type(db, "SERIES_IDENTITY_RESET")), 3,
                    "each reset logged SERIES_IDENTITY_RESET")
        reset_details = json.loads(_logs_of_type(db, "SERIES_IDENTITY_RESET")[0]["details"])
        check("releases_rekeyed" in reset_details,
              "reset activity details include releases_rekeyed")
        check("rekey_to_parsed" in reset_details,
              "reset activity details include rekey_to_parsed")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
TESTS = [
    test_migrations_and_settings_defaults,
    test_rebind_bind_mode,
    test_rebind_bind_creates_target,
    test_rebind_new_title_mode,
    test_rebind_detach_mode,
    test_reset_series_identity,
    test_identity_overview_and_unpin,
    test_pinned_survives_reingestion_and_merges,
    test_prune_activity_logs,
    test_database_stats_and_maintenance,
    test_reset_rekeys_manual_series,
]


def main():
    global FAILED
    print("=" * 78)
    print("  NovelTrax release re-bind / identity reset DB tests (v2 §2, §6)")
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
    print("ALL RELEASE REBIND DB TESTS PASSED")


if __name__ == "__main__":
    main()
