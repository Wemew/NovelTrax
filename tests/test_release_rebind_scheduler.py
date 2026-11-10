"""Scheduler integration tests for the v2 release re-bind / per-series identity
reset feature (CONTRACT_V2_REBIND.md §3) plus automatic activity-log pruning
(§6.1/§6.3 scheduler side).

Hermetic: no network; the shared harness from ``test_series_matching`` is reused
(fake Calibre scanner, fake metadata provider, fake feed provider, temp DB under
``tempfile.gettempdir()``). Explicit ``check()`` raises only; no bare asserts.
"""

import json
import os
import sys
import types
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Reuse the v1 suite's hermetic fixtures (FakeScanner/MetadataFake/temp DB ...).
import test_series_matching as matching
from test_series_matching import (
    FakeProvider,
    FakeScanner,
    MetadataFake,
    BOOK_ID_327_VOL1,
    TRAPPED_ANILIST,
    TRAPPED_CALIBRE,
    activity_events,
    check,
    cleanup_temp_databases,
    get_release,
    insert_release,
    insert_series,
    make_anilist_meta,
    make_db,
    new_manager,
    normalize_text,
    patched_metadata,
    patched_provider,
    patched_scanner,
    section,
    shutdown_manager,
)

from app.database import Database  # noqa: F401  (import smoke: shim must exist)


# ---------------------------------------------------------------------------
# v2 fixture helpers (direct SQL for pin/lock state, exactly what the DB layer
# writes; keeps the scheduler tests independent from the DB test suite).
# ---------------------------------------------------------------------------
def set_release_pinned(db, release_id, pinned=1):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE releases SET series_pinned = ?, series_pinned_at = ? WHERE id = ?;",
            (1 if pinned else 0, "2026-01-01T00:00:00+00:00" if pinned else "", release_id),
        )
        conn.commit()


def set_series_locked(db, series_id, locked=1):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE series_tracking SET identity_locked = ? WHERE id = ?;",
            (1 if locked else 0, series_id),
        )
        conn.commit()


def count_activity_logs(db):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) AS n FROM activity_logs;")
        row = cur.fetchone()
        return int(row["n"]) if row else 0


def age_activity_rows(db, count):
    """Back-dates the ``count`` oldest activity rows so an age prune sees them."""
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM activity_logs ORDER BY id ASC LIMIT ?;", (int(count),))
        ids = [row["id"] for row in cur.fetchall()]
        for row_id in ids:
            cur.execute(
                "UPDATE activity_logs SET created_at = '2020-01-01 00:00:00' WHERE id = ?;",
                (row_id,),
            )
        conn.commit()
        return len(ids)


def seed_calibre_series_and_book(db, series_name, calibre_id=55, volume=1.0):
    """Creates a tracked Calibre-linked row (for reset/reconcile scenarios)."""
    series_id = insert_series(
        db, series_name, calibre_id=calibre_id, is_monitored=1,
        owned_volumes=[volume], book_count=1, latest_index=volume,
    )
    return series_id


class FakeNotifier:
    """Records notification calls instead of doing any network I/O."""

    def __init__(self):
        self.new_requests = []
        self.acquired = []

    def notify_new_request(self, release):
        self.new_requests.append(release)

    def notify_acquired(self, payload):
        self.acquired.append(payload)


def make_item(guid, series_name, volume, title=None):
    return matching.ReleaseItem(
        title=title or f"{series_name} Vol {volume} (Novel)",
        link=f"https://example.test/{guid}",
        series_name=series_name,
        volume=float(volume),
        pub_date="2026-11-02T00:00:00+00:00",
        guid=guid,
    )


# ---------------------------------------------------------------------------
# 1. Helpers
# ---------------------------------------------------------------------------
def test_pin_and_lock_helpers():
    section("1. _release_identity_pinned / _series_identity_locked helpers")
    db = make_db()
    tm = new_manager(db)
    try:
        check(tm._release_identity_pinned({"series_pinned": 1}) is True, "1 is pinned")
        check(tm._release_identity_pinned({"series_pinned": "1"}) is True, "string 1 is pinned")
        check(tm._release_identity_pinned({"series_pinned": 0}) is False, "0 is not pinned")
        check(tm._release_identity_pinned({}) is False, "missing key is not pinned")
        check(tm._release_identity_pinned(None) is False, "None is not pinned")
        check(tm._series_identity_locked({"identity_locked": 1}) is True, "locked tracker")
        check(tm._series_identity_locked({"identity_locked": 0}) is False, "unlocked tracker")
        check(tm._series_identity_locked({}) is False, "missing lock key")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 2. rebind_release: bind to Calibre / tracked series
# ---------------------------------------------------------------------------
def test_rebind_bind_to_calibre():
    section("2. rebind_release(bind): Calibre target + status recompute + pin")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    try:
        insert_series(db, TRAPPED_CALIBRE, calibre_id=327, is_monitored=1)
        release_id = insert_release(
            db, "rebind-bind-guid", "Old Trapped Release", "Some Old Series", volume=None
        )
        with patched_scanner():
            summary = tm.rebind_release(release_id, "bind", calibre_series_id=327)

        check(summary["status"] == "success", f"rebind status: {summary}")
        check(summary["series_name"] == TRAPPED_CALIBRE, f"canonical target: {summary}")
        check(summary["calibre_series_id"] == 327, f"calibre id: {summary}")
        check(summary["release_status"] == "IN_CALIBRE",
              f"Calibre-bound status recompute: {summary}")
        check(summary["pinned"] is True, f"pinned flag: {summary}")
        row = get_release(db, release_id)
        check(row["series_name"] == TRAPPED_CALIBRE, f"row re-bound: {row['series_name']}")
        check(int(row["series_pinned"]) == 1, "row pinned")
        check(row["series_pinned_at"], "pin timestamp written")
        check(row["status"] == "IN_CALIBRE", f"row status: {row['status']}")
        check(row["calibre_matched_book_id"] == BOOK_ID_327_VOL1,
              f"owned volume matched: {row['calibre_matched_book_id']}")
        events = activity_events(db, "SERIES_MANUALLY_MATCHED")
        check(len(events) >= 1, f"SERIES_MANUALLY_MATCHED logged: {events}")
        rebound = activity_events(db, "RELEASE_SERIES_REBOUND")
        check(len(rebound) >= 1, f"RELEASE_SERIES_REBOUND logged by the DB layer: {rebound}")
        check("IN_CALIBRE" in (summary.get("message") or ""), f"toast message: {summary}")
    finally:
        shutdown_manager(tm)


def test_rebind_bind_to_tracked_series():
    section("3. rebind_release(bind): tracked (non-Calibre) target + remembered alias")
    db = make_db()
    FakeScanner.reset(series=[], books=[], volumes_map={})
    tm = new_manager(db)
    try:
        insert_series(db, "Tracked Manual Target", is_monitored=1)
        release_id = insert_release(
            db, "rebind-tracked-guid", "Some Release", "Another Old Series", volume=2.0
        )
        with patched_scanner():
            summary = tm.rebind_release(
                release_id, "bind", target_name="Tracked Manual Target", remember=True
            )
        check(summary["series_name"] == "Tracked Manual Target", f"target: {summary}")
        check(summary["calibre_series_id"] in (None, ""), f"no Calibre id: {summary}")
        check(summary["release_status"] == "REQUESTED",
              f"volume 2 => REQUESTED for a non-Calibre target: {summary}")
        row = get_release(db, release_id)
        check(row["series_name"] == "Tracked Manual Target", f"row target: {row['series_name']}")
        check(int(row["series_pinned"]) == 1, "row pinned")
        overrides = db.get_series_alias_overrides()
        check(
            any(normalize_text(o["source_name"]) == normalize_text("Another Old Series")
                and normalize_text(o["target_name"]) == normalize_text("Tracked Manual Target")
                for o in overrides),
            f"remember=True must register a manual alias: {overrides}",
        )
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 3. rebind_release: new_title / detach
# ---------------------------------------------------------------------------
def test_rebind_new_title_and_detach():
    section("4. rebind_release(new_title/detach): pinned new tracker, no aliases")
    db = make_db()
    FakeScanner.reset(series=[], books=[], volumes_map={})
    tm = new_manager(db)
    try:
        release_id = insert_release(
            db, "rebind-new-guid", "Old Series Vol 1 (Novel)", "Old Series", volume=1.0
        )
        summary = tm.rebind_release(release_id, "new_title", target_name="Brand New Manual Title")
        check(summary["series_name"] == "Brand New Manual Title", f"new title: {summary}")
        check(summary["created_series"] is True, f"created_series flag: {summary}")
        check(summary["release_status"] == "NEW", f"volume 1 => NEW: {summary}")
        row = get_release(db, release_id)
        check(row["series_name"] == "Brand New Manual Title", f"row series: {row['series_name']}")
        check(int(row["series_pinned"]) == 1, "row pinned")
        new_series = db.get_series_by_name("Brand New Manual Title")
        check(new_series is not None, "new tracker created")
        check(int(new_series["identity_locked"]) == 1, f"new tracker locked: {new_series}")
        check(int(new_series["is_monitored"]) == 1, "new tracker monitored")
        check(db.get_series_alias_overrides() == [],
              f"explicitly NOT bound to any previous series: {db.get_series_alias_overrides()}")

        # A volume > 1 gets REQUESTED.
        release_two = insert_release(
            db, "rebind-new-guid-2", "Old Series Vol 2 (Novel)", "Old Series", volume=2.0
        )
        summary_two = tm.rebind_release(
            release_two, "new_title", target_name="Brand New Manual Title Two"
        )
        check(summary_two["release_status"] == "REQUESTED",
              f"volume 2 => REQUESTED: {summary_two}")

        # detach with an explicit genuinely-new title behaves like new_title.
        release_three = insert_release(
            db, "rebind-detach-guid", "Old Series Vol 1 (Novel)", "Old Series", volume=1.0
        )
        detached = tm.rebind_release(
            release_three, "detach", target_name="Detached Standalone Fresh Title"
        )
        check(detached["release_status"] == "NEW", f"detach status: {detached}")
        check(db.get_series_by_name("Detached Standalone Fresh Title") is not None,
              "detach creates the new tracker")

        # Same/known titles are rejected (validation runs on a fresh row whose
        # current series is still "Old Series").
        release_validation = insert_release(
            db, "rebind-validation-guid", "Old Series Vol 1 (Novel)", "Old Series", volume=1.0
        )
        for bad_title in ("Old Series", "Brand New Manual Title", "Detached Standalone Fresh Title"):
            raised = False
            try:
                tm.rebind_release(release_validation, "new_title", target_name=bad_title)
            except ValueError:
                raised = True
            check(raised, f"re-indexing under a known title must raise ValueError: {bad_title}")
    finally:
        shutdown_manager(tm)


def test_rebind_preserves_ignored():
    section("5. rebind_release: IGNORED/BLACKLISTED veto data never downgraded")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    try:
        ignored_id = insert_release(
            db, "rebind-ignored-guid", "Ignored Release", "Old Ignored Series",
            volume=1.0, status="IGNORED",
        )
        summary = tm.rebind_release(ignored_id, "new_title", target_name="Fresh Ignored Title")
        check(summary["release_status"] == "IGNORED",
              f"IGNORED must be preserved by an explicit re-bind: {summary}")
        row = get_release(db, ignored_id)
        check(row["status"] == "IGNORED", f"stored status untouched: {row['status']}")
        check(int(row["series_pinned"]) == 1, "explicit re-bind still pins the row")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 4. reset_series_identity
# ---------------------------------------------------------------------------
def test_reset_series_identity_recovers():
    section("6. reset_series_identity: clears identity state then auto-recovers")
    db = make_db()
    FakeScanner.reset(
        series=[{"series_id": 55, "series_name": "Series Z", "book_count": 1,
                 "latest_index": 1.0, "authors": "Author Z"}],
        books=[{"book_id": 55001, "title": "Series Z Vol 1", "series_id": 55,
                "series_name": "Series Z", "series_index": 1.0}],
        volumes_map={"55": {1.0}},
    )
    tm = new_manager(db)
    metadata = MetadataFake()
    try:
        series_id = insert_series(
            db, "Series Z", is_monitored=1, display_title="Series Z AniList Display",
            anilist_meta=make_anilist_meta("Series Z AniList Display", anilist_id=42),
        )
        set_series_locked(db, series_id, 1)
        db.add_series_alias_override("Series Z Alt Spelling", "Series Z", origin="manual")
        pinned_release = insert_release(
            db, "reset-pinned-guid", "Series Z (Novel)", "Series Z", volume=1.0, status="NEW"
        )
        set_release_pinned(db, pinned_release, 1)

        metadata.reset(result=lambda name: make_anilist_meta(f"{name} Refetched"))
        with patched_scanner(), patched_metadata(metadata):
            summary = tm.reset_series_identity(series_id)

        check(summary["status"] == "success", f"reset status: {summary}")
        check(int(summary["aliases_removed"]) >= 1, f"aliases cleared: {summary}")
        check(int(summary["releases_unpinned"]) == 1, f"pins cleared: {summary}")
        check(bool(summary["display_cleared"]), f"display cleared: {summary}")
        check(bool(summary["locked_cleared"]), f"lock cleared: {summary}")
        check("releases_rekeyed" in summary["reconciled"],
              f"reconciled summary must expose releases_rekeyed: {summary['reconciled']}")
        check(isinstance(summary["reconciled"], dict), f"reconcile counts: {summary}")
        check(summary["linked"] is not None, f"best-effort auto-link recovery: {summary}")
        check(len(metadata.calls) >= 1, "forced AniList recovery lookup attempted")

        row = db.get_series_by_name("Series Z")
        check(row is not None, "tracker kept by reset")
        check((row.get("display_title") or "") == "", f"display title cleared: {row}")
        check(int(row.get("identity_locked") or 0) == 0, "identity lock cleared")
        check(row.get("calibre_series_id") == 55, f"recovery re-linked to Calibre: {row}")
        pinned_row = get_release(db, pinned_release)
        check(int(pinned_row["series_pinned"]) == 0, "per-release pin cleared by reset")

        raised = False
        try:
            tm.reset_series_identity(999999)
        except (KeyError, LookupError):
            raised = True
        check(raised, "reset of an unknown series must raise KeyError/LookupError")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 5. check_feeds pin enforcement
# ---------------------------------------------------------------------------
def test_check_feeds_pinned_survives_reingest():
    section("7. check_feeds: a pinned release is never moved, status still recomputed")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    metadata = MetadataFake()
    try:
        db.add_feed("Fake Feed", "rss", "https://example.test/feed", json.dumps({}), True, 60)
        pinned_release = insert_release(
            db, "pinned-guid", "Old Pinned Title", TRAPPED_CALIBRE, volume=None, status="NEW"
        )
        set_release_pinned(db, pinned_release, 1)
        item = matching.ReleaseItem(
            title=f"{TRAPPED_ANILIST} (Novel)",
            link="https://example.test/pinned",
            series_name=TRAPPED_ANILIST,   # feed/title-derived spelling would normally canonicalise
            volume=None,
            pub_date="2026-11-02T00:00:00+00:00",
            guid="pinned-guid",
        )
        FakeProvider.reset(items=[item])
        metadata.reset(result=None)
        with patched_scanner(), patched_metadata(metadata), patched_provider():
            result = tm.check_feeds()

        check(result["status"] == "success", f"scan status: {result}")
        row = get_release(db, pinned_release)
        check(row["series_name"] == TRAPPED_CALIBRE,
              f"pinned series_name must survive a feed poll: {row['series_name']}")
        check(int(row["series_pinned"]) == 1, "pin state survives the poll")
        check(row["status"] == "IN_CALIBRE",
              f"status is still computed against the pinned series: {row['status']}")
        check(row["calibre_matched_book_id"] in (None, BOOK_ID_327_VOL1),
              f"pinned series ownership: {row['calibre_matched_book_id']}")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 6. reconcile skips pinned
# ---------------------------------------------------------------------------
def test_reconcile_skips_pinned():
    section("8. reconcile_series_releases: pinned releases are skipped")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    try:
        insert_series(db, TRAPPED_CALIBRE, calibre_id=327, is_monitored=1)
        db.add_series_alias_override(TRAPPED_ANILIST, TRAPPED_CALIBRE,
                                     calibre_series_id=327, origin="anilist")
        pinned_release = insert_release(
            db, "reconcile-pinned-guid", "Pinned Release", TRAPPED_ANILIST,
            volume=1.0, status="NEW",
        )
        set_release_pinned(db, pinned_release, 1)
        normal_release = insert_release(
            db, "reconcile-normal-guid", "Normal Release", TRAPPED_ANILIST,
            volume=2.0, status="NEW",
        )
        with patched_scanner():
            counts = tm.reconcile_series_releases(TRAPPED_CALIBRE)

        check(counts["checked"] == 1, f"only the unpinned release is checked: {counts}")
        pinned_row = get_release(db, pinned_release)
        check(pinned_row["series_name"] == TRAPPED_ANILIST,
              f"pinned row not re-keyed: {pinned_row['series_name']}")
        check(pinned_row["status"] == "NEW", f"pinned row status untouched: {pinned_row['status']}")
        normal_row = get_release(db, normal_release)
        check(normal_row["series_name"] == TRAPPED_CALIBRE,
              f"unpinned row re-keyed: {normal_row['series_name']}")
        check(normal_row["status"] == "IN_CALIBRE",
              f"unpinned row reconciled: {normal_row['status']}")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 7. identity_locked trackers are skipped by enrichment/auto-link
# ---------------------------------------------------------------------------
def test_enrichment_and_autolink_skip_locked_trackers():
    section("9. enrich/sync: identity_locked trackers are never enriched or linked")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    metadata = MetadataFake()
    try:
        # A manually created "different series" tracker whose title happens to
        # be a known AniList spelling of a Calibre series must stay untouched.
        locked_id = insert_series(db, TRAPPED_ANILIST, is_monitored=1)
        set_series_locked(db, locked_id, 1)
        unlocked_id = insert_series(db, "Enrich Me", is_monitored=1)
        metadata.reset(result=lambda name: (
            make_anilist_meta(f"{name} AniList", synonyms=[TRAPPED_CALIBRE])
            if normalize_text(name) == normalize_text("Enrich Me") else None
        ))
        with patched_scanner(), patched_metadata(metadata):
            enrich = tm.enrich_tracked_series_anilist(limit=10)
        check(normalize_text(TRAPPED_ANILIST) not in
              {normalize_text(call) for call in metadata.calls},
              f"locked tracker must not be enriched: {metadata.calls}")
        check(enrich["synced"] >= 1, f"unlocked tracker enriched: {enrich}")
        check(enrich["linked"] >= 1, f"unlocked tracker auto-linked: {enrich}")
        locked_row = self_row(db, locked_id)
        check(locked_row["calibre_series_id"] is None,
              f"locked tracker must not be auto-linked: {locked_row}")
        check(not (locked_row.get("anilist_meta_json") or "").strip(),
              f"locked tracker must keep empty AniList metadata: {locked_row}")

        with patched_scanner(), patched_metadata(metadata):
            sync_result = tm.sync_calibre()
        check(sync_result["status"] == "success", f"sync status: {sync_result}")
        check(sync_result.get("series_linked") == 0,
              f"locked tracker must not be linked by sync auto-link: {sync_result}")
        locked_row = self_row(db, locked_id)
        check(locked_row["calibre_series_id"] is None,
              f"locked tracker still unlinked after sync: {locked_row}")
        check(int(locked_row.get("identity_locked") or 0) == 1,
              "locked tracker keeps its lock through sync")
    finally:
        shutdown_manager(tm)


def self_row(db, series_id):
    for row in db.get_series_identity_rows():
        if row.get("id") == series_id:
            return row
    return {}


# ---------------------------------------------------------------------------
# 8. backfill enrichment skips pinned releases
# ---------------------------------------------------------------------------
def test_backfill_enrichment_skips_pinned():
    section("10. run_polite_backfill: pinned release display metadata is not written")
    db = make_db()
    tm = new_manager(db)

    class FakeJNovels:
        @staticmethod
        def extract_synopsis_and_original_link(url, series_name=None):
            title = str(series_name or "Unknown")
            return {
                "series_link": None,
                "synopsis": ("A sufficiently long synopsis for " + title + ". ") * 3,
                "cover_url": None,
                "english_title": title + " AniList Display",
                "metadata_source": "AniList",
                "hub_volumes": [],
            }

        def __init__(self, *args, **kwargs):
            pass

    fake_module = types.ModuleType("app.providers.jnovels")
    fake_module.JNovelsProvider = FakeJNovels
    previous = sys.modules.get("app.providers.jnovels")
    try:
        sys.modules["app.providers.jnovels"] = fake_module
        tm.ingest_master_index = lambda: {"status": "success"}
        tracked_id = insert_series(db, TRAPPED_ANILIST, is_monitored=0)
        discovered_spelling = TRAPPED_ANILIST.rstrip("!")
        pinned_release = insert_release(
            db, "backfill-pinned-guid", f"{discovered_spelling} (Novel)",
            discovered_spelling, volume=None,
        )
        set_release_pinned(db, pinned_release, 1)
        insert_release(
            db, "backfill-free-guid", "Unknown Parsed Series (Novel)",
            "Unknown Parsed Series", volume=None,
        )
        import asyncio
        asyncio.run(tm.run_polite_backfill(max_pages=-1, enrich_synopses=True, delay_seconds=0))

        pinned_row = get_release(db, pinned_release)
        check(pinned_row["series_name"] == discovered_spelling,
              f"pinned stable key untouched by enrichment: {pinned_row['series_name']}")
        tracked_row = self_row(db, tracked_id)
        check((tracked_row.get("display_title") or "") == "",
              f"pinned release must not write display metadata: {tracked_row}")
        check(db.get_cached_novel_metadata(normalize_text("Unknown Parsed Series")) is not None,
              "unpinned unknown series still cached for later matching")
    finally:
        if previous is not None:
            sys.modules["app.providers.jnovels"] = previous
        else:
            sys.modules.pop("app.providers.jnovels", None)
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 9. Automatic activity-log pruning
# ---------------------------------------------------------------------------
def test_prune_activity_logs_settings():
    section("11. prune_activity_logs: settings-driven age + row-cap pruning")
    db = make_db()
    tm = new_manager(db)
    try:
        db.set_setting("activity_retention_days", "90")
        db.set_setting("activity_max_rows", "1000")
        for index in range(5):
            db.log_activity("TEST", f"old candidate {index}")
        aged = age_activity_rows(db, 2)
        check(aged == 2, f"two rows back-dated: {aged}")

        result = tm.prune_activity_logs()
        check(result["status"] == "success", f"prune status: {result}")
        check(int(result.get("removed_by_age") or 0) == 2,
              f"age prune removes the two old rows: {result}")
        check(int(result.get("removed") or 0) == 2, f"total removed: {result}")
        check(count_activity_logs(db) == 3, "remaining rows after age prune")

        # Row cap: 0 days disables the age rule, max_rows keeps the newest rows.
        db.set_setting("activity_retention_days", "0")
        db.set_setting("activity_max_rows", "2")
        for index in range(6):
            db.log_activity("TEST", f"fresh {index}")
        before = count_activity_logs(db)
        capped = tm.prune_activity_logs()
        check(capped["status"] == "success", f"cap prune status: {capped}")
        check(int(capped.get("removed_by_rows") or 0) == before - 2,
              f"row cap removes down to 2: before={before} result={capped}")
        check(count_activity_logs(db) == 2, "remaining rows after row cap")

        # Both rules disabled -> a no-op.
        db.set_setting("activity_retention_days", "0")
        db.set_setting("activity_max_rows", "0")
        noop = tm.prune_activity_logs()
        check(int(noop.get("removed") or 0) == 0, f"0/0 disables pruning: {noop}")
        check(count_activity_logs(db) == 2, "rows untouched when pruning is disabled")

        # Invalid settings fall back to the defaults and never raise.
        db.set_setting("activity_retention_days", "not-a-number")
        db.set_setting("activity_max_rows", "")
        fallback = tm.prune_activity_logs()
        check(fallback["status"] == "success", f"invalid settings fall back: {fallback}")

        # A DB failure is contained (never raises).
        original = db.prune_activity_logs

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated prune failure")

        db.prune_activity_logs = _boom
        try:
            failure = tm.prune_activity_logs()
        finally:
            db.prune_activity_logs = original
        check(failure["status"] == "error", f"failure contained: {failure}")
        check("simulated prune failure" in (failure.get("message") or ""),
              f"failure message: {failure}")
    finally:
        shutdown_manager(tm)


def test_start_schedules_activity_prune():
    section("12. TaskManager.start: activity_prune job is registered every 6 hours")
    db = make_db()
    tm = new_manager(db)
    try:
        db.set_setting("activity_retention_days", "0")
        db.set_setting("activity_max_rows", "0")
        tm.start()
        jobs = getattr(tm.scheduler, "jobs", None)
        if isinstance(jobs, dict):
            # Stdlib fallback scheduler (no APScheduler in the test interpreter).
            check("activity_prune" in jobs,
                  f"activity_prune must be registered: {list(jobs)}")
            entry = jobs["activity_prune"]
            minutes = entry.get("minutes") if isinstance(entry, dict) else None
            check(minutes == 360, f"activity_prune interval must be 360 minutes: {entry}")
            check("calibre_sync" in jobs and "feed_check" in jobs,
                  "existing jobs keep being registered")
        else:  # pragma: no cover - depends on the interpreter having APScheduler
            job = tm.scheduler.get_job("activity_prune")
            check(job is not None, "activity_prune job must exist")
            interval = getattr(job.trigger, "interval", None)
            check(interval is not None and interval.total_seconds() == 360 * 60,
                  f"activity_prune trigger interval: {interval}")
    finally:
        try:
            tm.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 10. Webhook event toggles (contract v2 §6.5)
# ---------------------------------------------------------------------------
def test_webhook_setting_enabled_helper():
    section("13. _webhook_setting_enabled: default on, false/0/no/off disable")
    db = make_db()
    tm = new_manager(db)
    try:
        check(tm._webhook_setting_enabled(db, "missing_key") is True, "missing key defaults on")
        db.set_setting("toggle_key", "")
        check(tm._webhook_setting_enabled(db, "toggle_key") is True, "empty value defaults on")
        for value in ("true", "TRUE", "1", "yes", "on"):
            db.set_setting("toggle_key", value)
            check(tm._webhook_setting_enabled(db, "toggle_key") is True, f"{value} enables")
        for value in ("false", "FALSE", "0", "no", "off", " Off "):
            db.set_setting("toggle_key", value)
            check(tm._webhook_setting_enabled(db, "toggle_key") is False, f"{value} disables")
    finally:
        shutdown_manager(tm)


def test_webhook_new_request_toggle_check_feeds():
    section("14. check_feeds: webhook_new_request_notifications toggle")
    db = make_db()
    FakeScanner.reset(series=[], books=[], volumes_map={})
    tm = new_manager(db)
    metadata = MetadataFake()
    notifier = FakeNotifier()
    tm.get_notifier = lambda: notifier
    try:
        db.add_feed("Fake Feed", "rss", "https://example.test/feed", json.dumps({}), True, 60)
        insert_series(db, "Webhook Series", is_monitored=1)
        metadata.reset(result=None)
        FakeProvider.reset(items=[make_item("wh-new-1", "Webhook Series", 2.0)])
        with patched_scanner(), patched_metadata(metadata), patched_provider():
            tm.check_feeds()
        check(len(notifier.new_requests) == 1,
              f"missing setting (default on) must notify: {len(notifier.new_requests)}")

        db.set_setting("webhook_new_request_notifications", "false")
        FakeProvider.reset(items=[make_item("wh-new-2", "Webhook Series", 3.0)])
        with patched_scanner(), patched_metadata(metadata), patched_provider():
            tm.check_feeds()
        check(len(notifier.new_requests) == 1,
              f"disabled toggle must skip notify_new_request: {len(notifier.new_requests)}")

        db.set_setting("webhook_new_request_notifications", "true")
        FakeProvider.reset(items=[make_item("wh-new-3", "Webhook Series", 4.0)])
        with patched_scanner(), patched_metadata(metadata), patched_provider():
            tm.check_feeds()
        check(len(notifier.new_requests) == 2,
              f"explicit true re-enables notifications: {len(notifier.new_requests)}")
    finally:
        shutdown_manager(tm)


def test_webhook_acquired_toggle_sync_calibre():
    section("15. sync_calibre: webhook_acquired_notifications toggle")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    metadata = MetadataFake()
    notifier = FakeNotifier()
    tm.get_notifier = lambda: notifier
    try:
        insert_release(db, "wh-acq-1", "Trapped Vol 1", TRAPPED_CALIBRE,
                       volume=1.0, status="REQUESTED")
        metadata.reset(result=None)
        with patched_scanner(), patched_metadata(metadata):
            tm.sync_calibre()
        check(len(notifier.acquired) == 1,
              f"default on must notify_acquired: {len(notifier.acquired)}")

        db.set_setting("webhook_acquired_notifications", "false")
        insert_release(db, "wh-acq-2", "Trapped Vol 2", TRAPPED_CALIBRE,
                       volume=2.0, status="REQUESTED")
        with patched_scanner(), patched_metadata(metadata):
            tm.sync_calibre()
        check(len(notifier.acquired) == 1,
              f"disabled toggle must skip notify_acquired: {len(notifier.acquired)}")
    finally:
        shutdown_manager(tm)


def test_webhook_new_request_toggle_backfill_series():
    section("16. backfill_series: webhook_new_request_notifications toggle")
    db = make_db()
    # backfill_series imports CalibreScanner locally, so exercise the tracker-
    # backed fallback snapshot (a Calibre-linked tracked row with owned vol 1).
    insert_series(
        db, "Webhook Backfill Series", calibre_id=901, is_monitored=1,
        owned_volumes=[1.0], book_count=1, latest_index=1.0,
    )
    tm = new_manager(db)
    notifier = FakeNotifier()
    tm.get_notifier = lambda: notifier
    tm.classify_media = lambda release: "novel"

    class FakeNyaa:
        items = []
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def search_series(self, series_name, max_pages=3, stop_check=None):
            FakeNyaa.calls += 1
            return list(FakeNyaa.items)

    fake_module = types.ModuleType("app.providers.nyaa")
    fake_module.NyaaNovelProvider = FakeNyaa
    previous = sys.modules.get("app.providers.nyaa")
    try:
        sys.modules["app.providers.nyaa"] = fake_module
        FakeNyaa.items = [make_item("wh-bf-1", "Webhook Backfill Series", 2.0)]
        tm.backfill_series("Webhook Backfill Series", max_pages=1)
        check(len(notifier.new_requests) == 1,
              f"default on must notify in backfill_series: {len(notifier.new_requests)}")

        db.set_setting("webhook_new_request_notifications", "false")
        FakeNyaa.items = [make_item("wh-bf-2", "Webhook Backfill Series", 3.0)]
        tm.backfill_series("Webhook Backfill Series", max_pages=1)
        check(len(notifier.new_requests) == 1,
              f"disabled toggle must skip backfill notify: {len(notifier.new_requests)}")
    finally:
        if previous is not None:
            sys.modules["app.providers.nyaa"] = previous
        else:
            sys.modules.pop("app.providers.nyaa", None)
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
TESTS = [
    ("pin/lock helpers", test_pin_and_lock_helpers),
    ("rebind bind calibre", test_rebind_bind_to_calibre),
    ("rebind bind tracked", test_rebind_bind_to_tracked_series),
    ("rebind new_title/detach", test_rebind_new_title_and_detach),
    ("rebind preserves ignored", test_rebind_preserves_ignored),
    ("reset series identity", test_reset_series_identity_recovers),
    ("check_feeds pinned", test_check_feeds_pinned_survives_reingest),
    ("reconcile skips pinned", test_reconcile_skips_pinned),
    ("locked tracker skip", test_enrichment_and_autolink_skip_locked_trackers),
    ("backfill pinned skip", test_backfill_enrichment_skips_pinned),
    ("prune activity logs", test_prune_activity_logs_settings),
    ("start schedules prune", test_start_schedules_activity_prune),
    ("webhook helper", test_webhook_setting_enabled_helper),
    ("webhook new request (feeds)", test_webhook_new_request_toggle_check_feeds),
    ("webhook acquired (sync)", test_webhook_acquired_toggle_sync_calibre),
    ("webhook new request (backfill)", test_webhook_new_request_toggle_backfill_series),
]


def main():
    print("=" * 78)
    print("  NovelTrax release re-bind / identity reset (scheduler) tests")
    print(f"  temp dir: {tempfile.gettempdir()}")
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
    print("ALL RELEASE-REBIND SCHEDULER TESTS PASSED")


if __name__ == "__main__":
    main()
