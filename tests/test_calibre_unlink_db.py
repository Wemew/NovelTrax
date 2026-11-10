#!/usr/bin/env python3
"""Calibre false-ownership repair DB tests (v4 contract §3, owner A).

Covers:

* ``Database.unlink_series_from_calibre``: KeyError for unknown ids, safe no-op for
  an already-unlinked series, clearing only ``calibre_series_id`` /
  ``owned_volumes_json`` / ``last_sync`` (name, display title, AniList meta,
  ``identity_locked`` and alias overrides preserved), the ``IN_CALIBRE`` status
  reset matrix (vol > 1 -> REQUESTED, None/1.0 -> NEW), ``calibre_matched_book_id``
  clearing, IGNORED/BLACKLISTED preservation, activity log and the grouped view no
  longer reporting the series as In Calibre;
* ``Database.set_series_owned_volumes``: sorted/deduped finite non-negative JSON,
  ``False`` for unknown ids, no other row changes.

Hermetic: temp DBs under ``tempfile.gettempdir()`` (never ``mkdtemp``), explicit
``check()`` raising ``AssertionError`` (no bare asserts, so Calibre's -O2
interpreter cannot strip them), no network.
"""

import gc
import json
import os
import shutil
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

SERIES_NAME = "Trapped in a Dating Sim: The World of Otome Games is Tough for Us, Too"
DISPLAY_TITLE = "Trapped in a Dating Sim: Otome Games Are Tough for Us, Too!"
ANILIST_META = json.dumps({
    "title": DISPLAY_TITLE,
    "format": "NOVEL",
    "format_label": "Light Novel",
    "status": "FINISHED",
    "volumes": 9,
})
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
        "noveltrax-calibre-repair-%d-%d-%s" % (os.getpid(), _TEMP_COUNTER, label),
    )
    os.makedirs(path, exist_ok=True)
    return path


def _cleanup_temp_dir(path):
    try:
        gc.collect()
    except Exception:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _insert_tracker(db, name, calibre_id=None, owned_volumes=None,
                    display_title=None, anilist_meta_json=None,
                    identity_locked=0, authors="", is_monitored=1,
                    book_count=0, latest_index=0.0):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO series_tracking
                (name, calibre_series_id, is_monitored, display_title, authors,
                 owned_volumes_json, anilist_meta_json, identity_locked,
                 book_count, latest_index, last_sync)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
            """,
            (
                name, calibre_id, is_monitored, display_title or "", authors or "",
                json.dumps(sorted(owned_volumes or [])), anilist_meta_json or "",
                identity_locked, book_count, latest_index,
            ),
        )
        conn.commit()
    return db.get_series_by_name(name)


def _insert_release(db, guid, series_name, volume, status,
                    matched_book_id=None, title=None):
    """Insert a release fixture directly (bypasses discovery de-duplication)."""
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO releases
                (guid, title, series_name, volume, status, calibre_matched_book_id,
                 ignored, media_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'unknown');
        """, (
            guid,
            title or f"{series_name} fixture {guid}",
            series_name,
            volume,
            status,
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


def _tracker_row(db, name):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM series_tracking WHERE lower(trim(name)) = lower(trim(?));",
            (name,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def _group_for(db, name):
    for group in db.get_releases_grouped_by_series(include_ignored=False):
        if normalize_text(group.get("stable_name") or "") == normalize_text(name):
            return group
    return None


def _logs_of_type(db, event_type, limit=200):
    try:
        return [
            entry for entry in db.get_logs(limit=limit)
            if entry.get("event_type") == event_type
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 1. Unknown id / already unlinked no-op
# ---------------------------------------------------------------------------
def test_unknown_and_already_unlinked():
    section("1. unlink: KeyError unknown, safe no-op when already unlinked")
    tmp_dir = _make_temp_dir("noop")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        check_raises(
            KeyError,
            lambda: db.unlink_series_from_calibre(999999),
            "unlinking an unknown series raises KeyError",
        )

        tracker = _insert_tracker(db, "Unlinked Series", calibre_id=None,
                                  owned_volumes=[1.0, 2.0])
        _insert_release(db, "noop-r1", "Unlinked Series", 2.0, "IN_CALIBRE",
                        matched_book_id=42, title="Unlinked Series Vol 2")
        result = db.unlink_series_from_calibre(tracker["id"])
        check_equal(result["status"], "success", "no-op unlink returns success")
        check_equal(result["already_unlinked"], True,
                    "already-unlinked series is reported as a no-op")
        check_equal(result["releases_reset"], 0,
                    "no-op unlink resets no releases")
        check_equal(result["old_calibre_series_id"], None,
                    "no-op unlink reports no old Calibre id")
        check_equal(result["series_name"], "Unlinked Series",
                    "no-op unlink reports the series name")

        stored = _tracker_row(db, "Unlinked Series")
        check_equal(json.loads(stored["owned_volumes_json"]), [1.0, 2.0],
                    "no-op unlink keeps the owned volumes untouched")
        release = _release_row(db, "noop-r1")
        check_equal(release["status"], "IN_CALIBRE",
                    "no-op unlink never resets releases")
        check_equal(release["calibre_matched_book_id"], 42,
                    "no-op unlink keeps the matched book id")
        check_equal(_logs_of_type(db, "SERIES_CALIBRE_UNLINKED"), [],
                    "no-op unlink logs nothing")

        check_equal(db.set_series_owned_volumes(999999, [1.0]), False,
                    "setting owned volumes for an unknown id returns False")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 2. Full unlink: status matrix, identity preservation, log, grouped view
# ---------------------------------------------------------------------------
def test_unlink_status_matrix_and_preservation():
    section("2. unlink: reset matrix, matched-id clearing, identity preserved")
    tmp_dir = _make_temp_dir("unlink")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        tracker = _insert_tracker(
            db, SERIES_NAME, calibre_id=327,
            owned_volumes=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            display_title=DISPLAY_TITLE,
            anilist_meta_json=ANILIST_META,
            identity_locked=1,
            authors="Author A",
            book_count=6,
            latest_index=6.0,
        )
        _insert_tracker(db, OTHER_SERIES, calibre_id=5)
        db.add_series_alias_override("Old Alias A", SERIES_NAME, 327, origin="manual")
        db.add_series_alias_override(SERIES_NAME, OTHER_SERIES, 5, origin="manual")
        aliases_before = db.get_series_alias_overrides()

        _insert_release(db, "u-vol3", SERIES_NAME, 3.0, "IN_CALIBRE",
                        matched_book_id=103, title=f"{SERIES_NAME} Vol 3")
        _insert_release(db, "u-vol2", SERIES_NAME, 2.0, "IN_CALIBRE",
                        matched_book_id=102, title=f"{SERIES_NAME} Vol 2")
        _insert_release(db, "u-vol1", SERIES_NAME, 1.0, "IN_CALIBRE",
                        matched_book_id=101, title=f"{SERIES_NAME} Vol 1")
        _insert_release(db, "u-volnone", SERIES_NAME, None, "IN_CALIBRE",
                        matched_book_id=100, title="Standalone")
        _insert_release(db, "u-vol15", SERIES_NAME, 1.5, "IN_CALIBRE",
                        matched_book_id=115, title="Special Edition")
        _insert_release(db, "u-ignored", SERIES_NAME, 3.0, "IGNORED",
                        matched_book_id=111, title="Ignored Release")
        _insert_release(db, "u-blacklisted", SERIES_NAME, 2.0, "BLACKLISTED",
                        matched_book_id=222, title="Blacklisted Release")
        _insert_release(db, "u-requested", SERIES_NAME, 4.0, "REQUESTED",
                        matched_book_id=333, title=f"{SERIES_NAME} Vol 4")
        _insert_release(db, "u-other", OTHER_SERIES, 1.0, "IN_CALIBRE",
                        matched_book_id=444, title=f"{OTHER_SERIES} Vol 1")

        group_before = _group_for(db, SERIES_NAME)
        check(group_before is not None, "the series group exists before unlink")
        check_equal(group_before["in_calibre"], True,
                    "grouped view reports In Calibre before the repair")
        check_equal(group_before["calibre_series_id"], 327,
                    "grouped view exposes the wrong Calibre id before the repair")
        check_equal(group_before["calibre_book_count"], 6,
                    "grouped view exposes the stale book count before the repair")
        check_equal(group_before["calibre_latest_index"], 6.0,
                    "grouped view exposes the stale latest index before the repair")

        result = db.unlink_series_from_calibre(tracker["id"], actor="tester")
        check_equal(result["status"], "success", "unlink returns status success")
        check_equal(result["already_unlinked"], False, "unlink is not a no-op")
        check_equal(result["series_id"], tracker["id"], "unlink reports the series id")
        check_equal(result["series_name"], SERIES_NAME, "unlink reports the series name")
        check_equal(result["old_calibre_series_id"], 327,
                    "unlink reports the removed Calibre id")
        check_equal(result["releases_reset"], 5,
                    "all five non-veto IN_CALIBRE releases were reset")

        stored = _tracker_row(db, SERIES_NAME)
        check_equal(stored["calibre_series_id"], None,
                    "unlink clears calibre_series_id")
        check_equal(stored["book_count"], 0,
                    "unlink clears the link-derived book count")
        check_equal(stored["latest_index"], 0.0,
                    "unlink clears the link-derived latest index")
        check_equal(json.loads(stored["owned_volumes_json"]), [],
                    "unlink leaves an empty owned-volumes list")
        check_equal(stored["name"], SERIES_NAME, "unlink keeps the stable name")
        check_equal(stored["display_title"], DISPLAY_TITLE,
                    "unlink keeps the cosmetic display title")
        check_equal(stored["anilist_meta_json"], ANILIST_META,
                    "unlink keeps the stored AniList metadata")
        check_equal(stored["identity_locked"], 1,
                    "unlink never touches identity_locked")
        check_equal(stored["authors"], "Author A", "unlink keeps authors")
        check_equal(len(db.get_series_alias_overrides()), len(aliases_before),
                    "unlink never touches alias overrides")

        expected_statuses = {
            "u-vol3": "REQUESTED",
            "u-vol2": "REQUESTED",
            "u-vol15": "REQUESTED",
            "u-vol1": "NEW",
            "u-volnone": "NEW",
        }
        for guid, expected_status in expected_statuses.items():
            release = _release_row(db, guid)
            check_equal(release["status"], expected_status,
                        f"{guid} reset to {expected_status}")
            check_equal(release["calibre_matched_book_id"], None,
                        f"{guid} matched book id cleared")
            check_equal(release["ignored"], 0, f"{guid} is not ignored")

        ignored = _release_row(db, "u-ignored")
        check_equal(ignored["status"], "IGNORED", "IGNORED status is untouched")
        check_equal(ignored["calibre_matched_book_id"], 111,
                    "IGNORED matched book id is untouched")
        check_equal(ignored["ignored"], 1, "IGNORED ignored flag is untouched")
        blacklisted = _release_row(db, "u-blacklisted")
        check_equal(blacklisted["status"], "BLACKLISTED",
                    "BLACKLISTED status is untouched")
        check_equal(blacklisted["calibre_matched_book_id"], 222,
                    "BLACKLISTED matched book id is untouched")
        requested = _release_row(db, "u-requested")
        check_equal(requested["status"], "REQUESTED",
                    "an existing REQUESTED status is kept")
        check_equal(requested["calibre_matched_book_id"], None,
                    "a stale matched book id on a non-In-Calibre row is cleared")
        other = _release_row(db, "u-other")
        check_equal(other["status"], "IN_CALIBRE",
                    "another series' release is untouched")
        check_equal(other["calibre_matched_book_id"], 444,
                    "another series' matched book id is untouched")

        logs = _logs_of_type(db, "SERIES_CALIBRE_UNLINKED")
        check_equal(len(logs), 1, "unlink logged SERIES_CALIBRE_UNLINKED exactly once")
        details = json.loads(logs[0]["details"])
        check_equal(details.get("series_id"), tracker["id"],
                    "unlink activity log carries the series id")
        check_equal(details.get("old_calibre_series_id"), 327,
                    "unlink activity log carries the old Calibre id")
        check_equal(details.get("releases_reset"), 5,
                    "unlink activity log carries the reset count")
        check_equal(details.get("actor"), "tester",
                    "unlink activity log carries the actor")

        group_after = _group_for(db, SERIES_NAME)
        check(group_after is not None, "the series group still exists after unlink")
        check_equal(group_after["in_calibre"], False,
                    "grouped view no longer reports In Calibre after the repair")
        check_equal(group_after["calibre_series_id"], None,
                    "grouped view no longer exposes a Calibre id")
        check_equal(group_after["calibre_book_count"], 0,
                    "grouped view no longer reports stale books after the repair")
        check_equal(group_after["calibre_latest_index"], 0.0,
                    "grouped view no longer reports a stale latest index")
        check_equal(group_after["is_monitored"], True,
                    "grouped view keeps the tracker's monitoring state")
        check_equal(group_after["stable_name"], SERIES_NAME,
                    "grouped view keeps the tracked stable name after unlink")

        # A tracked-but-unlinked row with explicit owned volumes still counts as
        # In Calibre (manual ownership), but never shows stale Calibre counts.
        _insert_tracker(db, "Manual Owned Series", calibre_id=None,
                        owned_volumes=[1.0, 2.0])
        _insert_release(db, "owned-r1", "Manual Owned Series", 2.0, "REQUESTED",
                        title="Manual Owned Series Vol 2")
        manual_group = _group_for(db, "Manual Owned Series")
        check(manual_group is not None, "the manual-owned series group exists")
        check_equal(manual_group["in_calibre"], True,
                    "explicit owned volumes keep the group marked In Calibre")
        check_equal(manual_group["calibre_series_id"], None,
                    "a manual-owned group exposes no Calibre id")
        check_equal(manual_group["calibre_book_count"], 0,
                    "a manual-owned group exposes no Calibre book count")
        check_equal(manual_group["calibre_latest_index"], 0.0,
                    "a manual-owned group exposes no Calibre latest index")

        # Second call is a no-op and does not log again.
        again = db.unlink_series_from_calibre(tracker["id"])
        check_equal(again["already_unlinked"], True,
                    "a second unlink is reported as already-unlinked")
        check_equal(again["releases_reset"], 0, "a second unlink resets nothing")
        check_equal(len(_logs_of_type(db, "SERIES_CALIBRE_UNLINKED")), 1,
                    "a second unlink logs nothing new")
        check_equal(_release_row(db, "u-vol3")["status"], "REQUESTED",
                    "a second unlink leaves the reset statuses alone")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 3. set_series_owned_volumes
# ---------------------------------------------------------------------------
def test_set_series_owned_volumes():
    section("3. set_series_owned_volumes: sorted/deduped/finite/non-negative JSON")
    tmp_dir = _make_temp_dir("owned-volumes")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        tracker = _insert_tracker(
            db, SERIES_NAME, calibre_id=327, owned_volumes=[1.0],
            display_title=DISPLAY_TITLE, anilist_meta_json=ANILIST_META,
            identity_locked=1, authors="Author A",
        )
        check_equal(db.set_series_owned_volumes(tracker["id"], [3, 1, 2, 2, 1.5]),
                    True, "setting owned volumes returns True")
        stored = _tracker_row(db, SERIES_NAME)
        check_equal(json.loads(stored["owned_volumes_json"]),
                    [1.0, 1.5, 2.0, 3.0],
                    "owned volumes are sorted and de-duplicated floats")
        check_equal(stored["calibre_series_id"], 327,
                    "set_series_owned_volumes keeps the Calibre link")
        check_equal(stored["name"], SERIES_NAME, "set_series_owned_volumes keeps name")
        check_equal(stored["display_title"], DISPLAY_TITLE,
                    "set_series_owned_volumes keeps display_title")
        check_equal(stored["anilist_meta_json"], ANILIST_META,
                    "set_series_owned_volumes keeps AniList metadata")
        check_equal(stored["identity_locked"], 1,
                    "set_series_owned_volumes keeps identity_locked")

        check_equal(db.set_series_owned_volumes(tracker["id"], [
            "2", float("nan"), float("inf"), float("-inf"), -1, True, None,
            "x", 2.0,
        ]), True, "invalid owned-volume entries are tolerated")
        sanitized = json.loads(_tracker_row(db, SERIES_NAME)["owned_volumes_json"])
        check_equal(sanitized, [2.0],
                    "only finite non-negative numeric values are accepted")

        check_equal(db.set_series_owned_volumes(tracker["id"], []), True,
                    "an empty owned-volume list is accepted")
        check_equal(json.loads(_tracker_row(db, SERIES_NAME)["owned_volumes_json"]),
                    [], "an empty owned-volume list clears ownership")
        check_equal(db.set_series_owned_volumes(tracker["id"], None), True,
                    "None is treated as an empty owned-volume list")
        check_equal(json.loads(_tracker_row(db, SERIES_NAME)["owned_volumes_json"]),
                    [], "None clears ownership")
        check_equal(db.set_series_owned_volumes(999999, [1.0]), False,
                    "unknown series id returns False")
        check_equal(_logs_of_type(db, "SERIES_CALIBRE_UNLINKED"), [],
                    "setting owned volumes logs no unlink activity")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
TESTS = [
    test_unknown_and_already_unlinked,
    test_unlink_status_matrix_and_preservation,
    test_set_series_owned_volumes,
]


def main():
    global FAILED
    print("=" * 78)
    print("  NovelTrax Calibre false-ownership repair DB tests (v4 §3)")
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
    print("ALL CALIBRE UNLINK DB TESTS PASSED")


if __name__ == "__main__":
    main()
