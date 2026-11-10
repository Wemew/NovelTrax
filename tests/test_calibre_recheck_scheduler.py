"""Scheduler tests for the Calibre false-ownership repair (CONTRACT v4 §4).

Hermetic: temp DB + fake CalibreScanner (series/books/omnibus map); the owner-A
DB repair methods are used when present and mirrored by a contract-faithful test
shim while they are landing. Explicit ``check()`` only (no bare asserts), no
network, no mkdtemp.
"""

import json
import math
import os
import sys
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import test_series_matching as matching
from test_series_matching import (
    FakeScanner,
    activity_events,
    check,
    cleanup_temp_databases,
    get_release,
    insert_release,
    insert_series,
    make_db,
    new_manager,
    normalize_text,
    patched_scanner,
    section,
    shutdown_manager,
)

SERIES = "Wrongly Linked Series"
CALIBRE_ID = 902
BOOK_VOL1 = 902001
BOOK_VOL2 = 902002
BOOK_VOL3 = 902003
BOOK_VOL5 = 902005


# ---------------------------------------------------------------------------
# DB repair shims: use the real owner-A methods when they exist, otherwise a
# minimal contract-faithful implementation so the scheduler tests are honest
# before task-19 lands. Both paths record the calls.
# ---------------------------------------------------------------------------
def _shim_unlink(db, series_id):
    sid = int(series_id)
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM series_tracking WHERE id = ?;", (sid,))
        found = cur.fetchone()
        if found is None:
            raise KeyError(f"series #{sid} not found")
        row = dict(found)
        name = (row.get("name") or "").strip()
        old_id = row.get("calibre_series_id")
        if old_id in (None, ""):
            return {
                "status": "success", "series_id": sid, "series_name": name,
                "old_calibre_series_id": None, "releases_reset": 0,
                "already_unlinked": True,
            }
        cur.execute(
            "UPDATE series_tracking SET calibre_series_id = NULL, "
            "owned_volumes_json = '[]', last_sync = CURRENT_TIMESTAMP WHERE id = ?;",
            (sid,),
        )
        cur.execute(
            "SELECT id, volume, status FROM releases "
            "WHERE lower(trim(COALESCE(series_name, ''))) = lower(trim(?));",
            (name,),
        )
        releases = [dict(item) for item in cur.fetchall()]
        reset = 0
        for release in releases:
            if (release.get("status") or "").strip().upper() != "IN_CALIBRE":
                continue
            try:
                volume = float(release.get("volume")) if release.get("volume") is not None else None
            except (TypeError, ValueError):
                volume = None
            target = "REQUESTED" if (volume is not None and volume > 1.0) else "NEW"
            cur.execute(
                "UPDATE releases SET status = ?, calibre_matched_book_id = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (target, release["id"]),
            )
            reset += 1
        conn.commit()
    try:
        db.log_activity(
            "SERIES_CALIBRE_UNLINKED",
            f"Series '{name}' unlinked from Calibre",
            json.dumps({"series": name, "old_calibre_series_id": old_id,
                        "releases_reset": reset}),
        )
    except Exception:
        pass
    return {
        "status": "success", "series_id": sid, "series_name": name,
        "old_calibre_series_id": old_id, "releases_reset": reset,
        "already_unlinked": False,
    }


def _shim_set_owned(db, series_id, volumes):
    sid = int(series_id)
    clean = set()
    for value in volumes or []:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number >= 0:
            clean.add(number)
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM series_tracking WHERE id = ?;", (sid,))
        if cur.fetchone() is None:
            return False
        cur.execute(
            "UPDATE series_tracking SET owned_volumes_json = ? WHERE id = ?;",
            (json.dumps(sorted(clean)), sid),
        )
        conn.commit()
    return True


@contextmanager
def patched_repair_db(db):
    """Wraps the DB repair methods (real when available) and records calls."""
    calls = {"unlink": [], "owned": []}
    real_unlink = getattr(db, "unlink_series_from_calibre", None)
    real_owned = getattr(db, "set_series_owned_volumes", None)
    had_instance_unlink = "unlink_series_from_calibre" in db.__dict__
    had_instance_owned = "set_series_owned_volumes" in db.__dict__
    original_instance_unlink = db.__dict__.get("unlink_series_from_calibre")
    original_instance_owned = db.__dict__.get("set_series_owned_volumes")

    def unlink(series_id, actor="user"):
        calls["unlink"].append((series_id, actor))
        if real_unlink is not None:
            return real_unlink(series_id, actor=actor)
        return _shim_unlink(db, series_id)

    def set_owned(series_id, volumes):
        calls["owned"].append((series_id, list(volumes or [])))
        if real_owned is not None:
            return real_owned(series_id, volumes)
        return _shim_set_owned(db, series_id, volumes)

    db.unlink_series_from_calibre = unlink
    db.set_series_owned_volumes = set_owned
    try:
        yield calls
    finally:
        if had_instance_unlink:
            db.unlink_series_from_calibre = original_instance_unlink
        else:
            try:
                delattr(db, "unlink_series_from_calibre")
            except AttributeError:
                pass
        if had_instance_owned:
            db.set_series_owned_volumes = original_instance_owned
        else:
            try:
                delattr(db, "set_series_owned_volumes")
            except AttributeError:
                pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def seed_library(series=None, books=None, volumes_map=None, available=True):
    FakeScanner.reset(series=series, books=books, volumes_map=volumes_map,
                      available=available)


def calibre_series(series_id=CALIBRE_ID, name=None, book_count=3, latest=3.0):
    return {
        "series_id": series_id,
        "series_name": name or SERIES,
        "book_count": book_count,
        "latest_index": latest,
        "authors": "Author",
    }


def calibre_book(book_id, volume, series_id=CALIBRE_ID, name=None):
    return {
        "book_id": book_id,
        "title": f"{name or SERIES} Vol {volume:g}",
        "series_id": series_id,
        "series_name": name or SERIES,
        "series_index": volume,
    }


def linked_release(db, guid, series_name, volume, status="IN_CALIBRE",
                   matched_book_id=None, title=None):
    release_id = insert_release(
        db, guid, title or f"{series_name} Vol {volume:g} (Novel)", series_name,
        volume=volume, status=status,
    )
    if matched_book_id is not None:
        with db.get_connection() as conn:
            conn.cursor().execute(
                "UPDATE releases SET calibre_matched_book_id = ? WHERE id = ?;",
                (matched_book_id, release_id),
            )
            conn.commit()
    return release_id


def owned_json(db, series_id):
    rows = db.get_series_identity_rows()
    for row in rows:
        if row.get("id") == series_id:
            return json.loads(row.get("owned_volumes_json") or "[]")
    return None


def get_row(db, series_id):
    for row in db.get_series_identity_rows():
        if row.get("id") == series_id:
            return row
    return None


# ---------------------------------------------------------------------------
# 1. Valid link: recompute owned volumes + reconcile statuses
# ---------------------------------------------------------------------------
def test_recheck_valid_link_recomputes_ownership():
    section("1. recheck_series_calibre: recomputes owned volumes + reconciles")
    db = make_db()
    seed_library(
        series=[calibre_series(book_count=3, latest=3.0)],
        books=[calibre_book(BOOK_VOL1, 1.0), calibre_book(BOOK_VOL2, 2.0),
               calibre_book(BOOK_VOL3, 3.0)],
        # Omnibus expansion: Vol 4 is owned through a bundle even without a
        # separate book row, exactly like sync_calibre's volumes map.
        volumes_map={str(CALIBRE_ID): {1.0, 2.0, 3.0, 4.0}},
    )
    tm = new_manager(db)
    try:
        series_id = insert_series(
            db, SERIES, calibre_id=CALIBRE_ID, is_monitored=1,
            # The false "6 books in Calibre" state from the screenshot.
            owned_volumes=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            book_count=6, latest_index=6.0,
        )
        vol1 = linked_release(db, "recheck-vol1", SERIES, 1.0, "IN_CALIBRE", BOOK_VOL1)
        vol5 = linked_release(db, "recheck-vol5", SERIES, 5.0, "IN_CALIBRE", BOOK_VOL5)
        vol2 = insert_release(db, "recheck-vol2", f"{SERIES} Vol 2 (Novel)",
                              SERIES, volume=2.0, status="NEW")
        ignored = insert_release(db, "recheck-ignored", f"{SERIES} Vol 2 (Novel)",
                                 SERIES, volume=2.0, status="IGNORED")

        with patched_scanner(), patched_repair_db(db) as calls:
            result = tm.recheck_series_calibre(series_id)

        check(result["status"] == "success", f"recheck status: {result}")
        check(result["linked"] is True, f"still linked: {result}")
        check(result["calibre_series_id"] == CALIBRE_ID, f"calibre id: {result}")
        check(result["owned_volumes"] == [1.0, 2.0, 3.0, 4.0],
              f"owned volumes from real books + omnibus map: {result['owned_volumes']}")
        check(owned_json(db, series_id) == [1.0, 2.0, 3.0, 4.0],
              f"persisted owned_volumes_json: {owned_json(db, series_id)}")
        check(len(calls["owned"]) == 1 and calls["owned"][0][0] == series_id,
              f"set_series_owned_volumes called once: {calls['owned']}")

        row = get_release(db, vol1)
        check(row["status"] == "IN_CALIBRE", f"owned volume 1 stays In Calibre: {row['status']}")
        check(row["calibre_matched_book_id"] == BOOK_VOL1,
              f"owned volume 1 matched book: {row['calibre_matched_book_id']}")
        row = get_release(db, vol5)
        check(row["status"] == "REQUESTED",
              f"unowned volume 5 returns to REQUESTED: {row['status']}")
        check(row["calibre_matched_book_id"] is None,
              f"stale matched book cleared: {row['calibre_matched_book_id']}")
        row = get_release(db, vol2)
        check(row["status"] == "IN_CALIBRE", f"NEW owned volume 2 reconciled: {row['status']}")
        row = get_release(db, ignored)
        check(row["status"] == "IGNORED", "IGNORED release untouched by the repair")

        check(result["reconciled"].get("stale_in_calibre_reset") == 1,
              f"stale In-Calibre count: {result['reconciled']}")
        check("checked" in result["reconciled"], f"reconcile counts: {result['reconciled']}")
        events = activity_events(db, "SERIES_CALIBRE_RECHECKED")
        check(len(events) == 1, f"SERIES_CALIBRE_RECHECKED logged once: {len(events)}")
        details = json.loads(events[0]["details"] or "{}")
        check(details.get("calibre_series_id") == CALIBRE_ID, f"activity calibre id: {details}")
        check(details.get("owned_volumes") == 4, f"activity owned count: {details}")
        check("4 owned volume" in result["message"], f"message: {result['message']}")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 2. Linked Calibre series gone -> unlink flow
# ---------------------------------------------------------------------------
def test_recheck_missing_calibre_series_unlinks():
    section("2. recheck_series_calibre: missing Calibre series triggers the unlink flow")
    db = make_db()
    seed_library(
        series=[calibre_series(series_id=111, name="Some Other Calibre Series")],
        books=[calibre_book(111001, 1.0, series_id=111, name="Some Other Calibre Series")],
        volumes_map={"111": {1.0}},
    )
    tm = new_manager(db)
    try:
        series_id = insert_series(
            db, SERIES, calibre_id=CALIBRE_ID, is_monitored=1,
            owned_volumes=[1.0, 2.0, 3.0], book_count=6, latest_index=6.0,
        )
        vol1 = linked_release(db, "gone-vol1", SERIES, 1.0, "IN_CALIBRE", BOOK_VOL1)
        vol3 = linked_release(db, "gone-vol3", SERIES, 3.0, "IN_CALIBRE", BOOK_VOL3)
        requested = insert_release(db, "gone-requested", f"{SERIES} Vol 2 (Novel)",
                                   SERIES, volume=2.0, status="REQUESTED")

        with patched_scanner(), patched_repair_db(db) as calls:
            result = tm.recheck_series_calibre(series_id)

        check(result["status"] == "success", f"recheck status: {result}")
        check(result["linked"] is False, f"unlinked result: {result}")
        check(result["unlinked"] is True, f"unlinked flag: {result}")
        check(result["calibre_series_id"] == CALIBRE_ID, f"old calibre id: {result}")
        check(result["releases_reset"] == 2, f"two IN_CALIBRE releases reset: {result}")
        check(len(calls["unlink"]) == 1 and calls["unlink"][0][0] == series_id,
              f"db unlink delegated: {calls['unlink']}")
        check(owned_json(db, series_id) == [], "owned volumes cleared by the unlink")
        row = get_row(db, series_id)
        check(row is not None and row.get("calibre_series_id") is None,
              f"tracker unlinked: {row}")

        row = get_release(db, vol1)
        check(row["status"] == "NEW", f"volume 1 -> NEW: {row['status']}")
        check(row["calibre_matched_book_id"] is None, "matched book cleared (vol 1)")
        row = get_release(db, vol3)
        check(row["status"] == "REQUESTED", f"volume 3 -> REQUESTED: {row['status']}")
        check(row["calibre_matched_book_id"] is None, "matched book cleared (vol 3)")
        row = get_release(db, requested)
        check(row["status"] == "REQUESTED", "existing REQUESTED release untouched")

        check(len(activity_events(db, "SERIES_CALIBRE_UNLINKED")) >= 1,
              "SERIES_CALIBRE_UNLINKED activity logged by the unlink flow")
        check(len(activity_events(db, "SERIES_CALIBRE_RECHECKED")) == 1,
              "SERIES_CALIBRE_RECHECKED logged for the unlink outcome")
        check("no longer exists" in result["message"], f"message: {result['message']}")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 3. Unknown / already-unlinked
# ---------------------------------------------------------------------------
def test_recheck_unknown_and_already_unlinked():
    section("3. recheck_series_calibre: unknown id -> KeyError; unlinked -> no-op")
    db = make_db()
    seed_library(series=[], books=[], volumes_map={})
    tm = new_manager(db)
    try:
        raised = False
        try:
            tm.recheck_series_calibre(999999)
        except KeyError:
            raised = True
        check(raised, "unknown series id must raise KeyError")

        series_id = insert_series(db, "Never Linked Series", is_monitored=1)
        with patched_scanner(), patched_repair_db(db) as calls:
            result = tm.recheck_series_calibre(series_id)
        check(result["status"] == "success", f"no-op status: {result}")
        check(result["linked"] is False and result["already_unlinked"] is True,
              f"already-unlinked result: {result}")
        check(result["series_id"] == series_id, f"series id echo: {result}")
        check(calls["unlink"] == [], "already-unlinked recheck must not call the DB unlink")
        check(calls["owned"] == [], "already-unlinked recheck must not set owned volumes")
        check("not linked to Calibre" in result["message"], f"message: {result['message']}")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 4. Calibre read failures are contained
# ---------------------------------------------------------------------------
def test_recheck_scanner_unavailable_contained():
    section("4. recheck_series_calibre: Calibre read failures leave the link unchanged")
    db = make_db()
    tm = new_manager(db)
    try:
        series_id = insert_series(
            db, SERIES, calibre_id=CALIBRE_ID, is_monitored=1,
            owned_volumes=[1.0, 2.0], book_count=2, latest_index=2.0,
        )
        # Unavailable scanner.
        seed_library(available=False)
        with patched_scanner(), patched_repair_db(db) as calls:
            result = tm.recheck_series_calibre(series_id)
        check(result["status"] == "success", f"contained status: {result}")
        check(result["linked"] is True and result.get("unlinked") is not True,
              f"link left unchanged: {result}")
        check(result["calibre_series_id"] == CALIBRE_ID, f"calibre id unchanged: {result}")
        check(result["reconciled"] == {}, f"no reconcile on read failure: {result}")
        check("left unchanged" in result["message"], f"message: {result['message']}")
        check(calls["unlink"] == [], "unavailable read must not unlink")
        row = get_row(db, series_id)
        check(row.get("calibre_series_id") == CALIBRE_ID, "tracker still linked")

        # Scanner raises mid-read.
        seed_library(
            series=[calibre_series()],
            books=[calibre_book(BOOK_VOL1, 1.0)],
            volumes_map={str(CALIBRE_ID): {1.0}},
        )
        original = FakeScanner.get_all_series

        def _explode(self):
            raise RuntimeError("simulated torn Calibre read")

        FakeScanner.get_all_series = _explode
        try:
            with patched_scanner(), patched_repair_db(db):
                result = tm.recheck_series_calibre(series_id)
        finally:
            FakeScanner.get_all_series = original
        check(result["status"] == "success", f"torn read contained: {result}")
        check(result["linked"] is True, f"torn read leaves the link: {result}")
        check("Could not read the Calibre library" in result["message"],
              f"read failure reported: {result['message']}")
        row = get_row(db, series_id)
        check(row.get("calibre_series_id") == CALIBRE_ID, "torn read keeps the link")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 5. Unlink wrapper + grouped view no longer reports In Calibre
# ---------------------------------------------------------------------------
def test_unlink_wrapper_and_grouped_view():
    section("5. unlink_series_calibre: resets releases; grouped view no longer In Calibre")
    db = make_db()
    seed_library(
        series=[calibre_series()],
        books=[calibre_book(BOOK_VOL1, 1.0), calibre_book(BOOK_VOL2, 2.0)],
        volumes_map={str(CALIBRE_ID): {1.0, 2.0}},
    )
    tm = new_manager(db)
    try:
        series_id = insert_series(
            db, SERIES, calibre_id=CALIBRE_ID, is_monitored=1,
            owned_volumes=[1.0, 2.0], book_count=2, latest_index=2.0,
        )
        vol1 = linked_release(db, "unlink-vol1", SERIES, 1.0, "IN_CALIBRE", BOOK_VOL1)
        vol2 = linked_release(db, "unlink-vol2", SERIES, 2.0, "IN_CALIBRE", BOOK_VOL2)
        ignored = insert_release(db, "unlink-ignored", f"{SERIES} Vol 2 (Novel)",
                                 SERIES, volume=2.0, status="IGNORED")

        grouped_before = db.get_releases_grouped_by_series()
        group = next((g for g in grouped_before
                      if normalize_text(g.get("series_name") or "") == normalize_text(SERIES)),
                     None)
        check(group is not None, "series group exists before unlink")
        check(bool(group.get("in_calibre")), f"grouped view before unlink is In Calibre: {group}")

        with patched_scanner(), patched_repair_db(db) as calls:
            summary = tm.unlink_series_calibre(series_id)

        check(summary["status"] == "success", f"unlink status: {summary}")
        check(summary["already_unlinked"] is False, f"linked before unlink: {summary}")
        check(summary["old_calibre_series_id"] == CALIBRE_ID, f"old id: {summary}")
        check(summary["releases_reset"] == 2, f"two releases reset: {summary}")
        check("returned to Wanted/New" in summary["message"], f"message: {summary['message']}")
        check(len(calls["unlink"]) == 1, f"db unlink delegated once: {calls['unlink']}")
        row = get_row(db, series_id)
        check(row.get("calibre_series_id") is None, "calibre link removed")
        check(owned_json(db, series_id) == [], "owned volumes cleared")
        check(get_release(db, vol1)["status"] == "NEW", "vol 1 reset to NEW")
        check(get_release(db, vol2)["status"] == "REQUESTED", "vol 2 reset to REQUESTED")
        check(get_release(db, vol2)["calibre_matched_book_id"] is None,
              "matched book cleared")
        check(get_release(db, ignored)["status"] == "IGNORED", "IGNORED untouched")
        check(len(activity_events(db, "SERIES_CALIBRE_UNLINKED")) >= 1,
              "SERIES_CALIBRE_UNLINKED logged")

        grouped_after = db.get_releases_grouped_by_series()
        group = next((g for g in grouped_after
                      if normalize_text(g.get("series_name") or "") == normalize_text(SERIES)),
                     None)
        check(group is not None, "series group still exists after unlink")
        check(not group.get("in_calibre"), f"grouped view no longer In Calibre: {group}")
        check(group.get("calibre_series_id") is None, f"grouped calibre id cleared: {group}")

        # Second unlink is a reported no-op.
        with patched_repair_db(db):
            second = tm.unlink_series_calibre(series_id)
        check(second["already_unlinked"] is True, f"second unlink no-op: {second}")
        check(second["releases_reset"] == 0, "no releases reset on the second call")
        check("already unlinked" in second["message"], f"no-op message: {second['message']}")

        raised = False
        try:
            tm.unlink_series_calibre(999999)
        except KeyError:
            raised = True
        check(raised, "unknown series id must raise KeyError")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
TESTS = [
    ("recheck valid link", test_recheck_valid_link_recomputes_ownership),
    ("recheck missing series", test_recheck_missing_calibre_series_unlinks),
    ("recheck unknown/unlinked", test_recheck_unknown_and_already_unlinked),
    ("recheck read failure", test_recheck_scanner_unavailable_contained),
    ("unlink wrapper/grouped", test_unlink_wrapper_and_grouped_view),
]


def main():
    print("=" * 78)
    print("  NovelTrax Calibre false-ownership repair (scheduler) tests")
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
    print("ALL CALIBRE-RECHECK SCHEDULER TESTS PASSED")


if __name__ == "__main__":
    main()
