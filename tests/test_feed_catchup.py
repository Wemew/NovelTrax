"""Time-aware ("catch-up") automatic feed scanning.

Policy under test
-----------------
A fixed scan window misses releases when a site publishes more between two
scans than the window holds.  Every scan therefore keeps a watermark inside
``feeds.config_json`` and, on the next automatic scan, pages backwards until it
reaches a release the previous scan already covered:

* ``last_scan_oldest_pub_date`` -- coverage boundary (ISO-8601, UTC);
* ``last_scan_anchor_guid``     -- newest item GUID/link of the previous scan
  (the J-Novels fallback boundary, since some article pages omit timestamps);
* ``last_scan_started_at``      -- when the scan that wrote the watermarks began.

The pure arithmetic lives in ``app.providers.catchup`` (stdlib only); the
orchestration lives in ``TaskManager.check_feeds``.

Test environment
----------------
This suite is hermetic: standard library + ``app.database`` + ``app.scheduler``
only.  The provider stack (bs4/feedparser/httpx) is not installed in the
lightweight runner, so ``app.providers.registry`` is replaced by a one-function
stub before ``app.scheduler`` is imported and every provider instance is a
``FakeProvider``; no network access happens.  ``tempfile.mkdtemp`` directories
created by this interpreter are not writable under the DSH Windows sandbox, so
the temporary SQLite database is created as a uniquely named file directly in
the system temp directory instead.

Bare ``assert`` statements are stripped by Calibre's interpreter (-O2), so every
check here raises ``AssertionError`` explicitly.
"""

import gc
import json
import os
import sys
import tempfile
import time
import traceback
import types
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


# ---------------------------------------------------------------------------
# check() helper -- explicit raises, never bare asserts
# ---------------------------------------------------------------------------
def check(condition, message):
    if not condition:
        raise AssertionError(message)


def section(title):
    print(f"\n--- {title} ---")


# ---------------------------------------------------------------------------
# Import app.scheduler without the third-party provider stack.
#
# scheduler.py imports app.providers.registry at module level, and the real
# registry imports bs4/feedparser-based providers.  In the lightweight runtime
# those modules are absent, so install a minimal registry stub first.  When the
# full provider stack *is* installed the stub is unnecessary; either way the
# tests patch app.scheduler.create_provider per test.
# ---------------------------------------------------------------------------
REGISTRY_STUBBED = False
try:  # pragma: no cover - depends on the interpreter environment
    from app.scheduler import TaskManager  # noqa: F401
    import app.scheduler as scheduler_module
except ImportError:
    _registry_stub = types.ModuleType("app.providers.registry")

    def _unpatched_create_provider(*args, **kwargs):
        raise RuntimeError("create_provider was not patched for this test")

    _registry_stub.create_provider = _unpatched_create_provider
    sys.modules["app.providers.registry"] = _registry_stub
    from app.scheduler import TaskManager  # noqa: F401
    import app.scheduler as scheduler_module

    REGISTRY_STUBBED = True

from app.database import Database
from app.providers import catchup
from app.providers.base import BaseFeedProvider, ReleaseItem

from contextlib import contextmanager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class FakeProvider:
    """Scriptable stand-in for a real feed provider.

    ``normal_items``/``since_items`` model ``fetch_releases`` vs
    ``fetch_releases_since``; the call logs let a test prove which method the
    scheduler chose and with which watermark arguments.
    """

    normal_items = []
    since_items = []
    since_meta = {}
    normal_calls = []
    since_calls = []
    error = None

    def __init__(self, name=None, url=None, config=None):
        self.name = name
        self.url = url
        self.config = config or {}
        self.last_scan_meta = {}

    @classmethod
    def reset(cls, normal_items=None, since_items=None, since_meta=None, error=None):
        cls.normal_items = list(normal_items or [])
        cls.since_items = list(since_items or [])
        cls.since_meta = dict(since_meta or {})
        cls.normal_calls = []
        cls.since_calls = []
        cls.error = error

    def fetch_releases(self):
        FakeProvider.normal_calls.append(self.name)
        if FakeProvider.error is not None:
            raise FakeProvider.error
        return list(FakeProvider.normal_items)

    def fetch_releases_since(self, *, cutoff_iso=None, stop_guid=None, max_pages=None):
        FakeProvider.since_calls.append({
            "cutoff_iso": cutoff_iso,
            "stop_guid": stop_guid,
            "max_pages": max_pages,
        })
        if FakeProvider.error is not None:
            raise FakeProvider.error
        self.last_scan_meta = dict(FakeProvider.since_meta)
        return list(FakeProvider.since_items)


@contextmanager
def patched_provider():
    """Routes ``app.scheduler.create_provider`` to :class:`FakeProvider`."""
    original = scheduler_module.create_provider

    def _fake_create_provider(feed_type, name, url, config=None):
        return FakeProvider(name=name, url=url, config=config)

    scheduler_module.create_provider = _fake_create_provider
    try:
        yield
    finally:
        scheduler_module.create_provider = original


def make_item(volume, pub_date, guid_prefix="fake-guid"):
    """Builds a novel-shaped ReleaseItem that survives the ingestion filters."""
    return ReleaseItem(
        title=f"Fake Series, Vol. {volume} [EPUB]",
        link=f"https://example.test/releases/{volume}",
        series_name="Fake Series",
        volume=float(volume),
        pub_date=pub_date,
        guid=f"{guid_prefix}-{volume}",
    )


def iso(day, hour=0):
    return datetime(2026, 11, day, hour, tzinfo=UTC).isoformat()


_TEMP_DB_PATHS = []


def make_feed_db():
    """Creates a temporary Database with only the feeds a test adds.

    ``tempfile.mkdtemp()`` cannot be used here: the DSH Windows sandbox does
    not provision write access for directories created by this child
    interpreter.  A uniquely named file directly in the temp directory is
    writable instead and still isolated per test.
    """
    db_path = os.path.join(
        tempfile.gettempdir(),
        "noveltrax_feed_catchup_%d_%s.db" % (os.getpid(), uuid.uuid4().hex[:10]),
    )
    _TEMP_DB_PATHS.extend([db_path, db_path + "-wal", db_path + "-shm"])
    db = Database(db_path)
    with db.get_connection() as conn:
        conn.cursor().execute("DELETE FROM feeds;")
        conn.commit()
    # Disable the media resolver so classification never attempts a fetch.
    db.set_setting("filter_non_novels", "false")
    return db


def cleanup_temp_databases():
    """Best-effort removal of the temporary SQLite files (Windows locks)."""
    gc.collect()
    for path in _TEMP_DB_PATHS:
        for _ in range(3):
            try:
                os.remove(path)
                break
            except FileNotFoundError:
                break
            except OSError:
                gc.collect()
                time.sleep(0.05)


def new_manager(db):
    return TaskManager(db, calibre_path=tempfile.gettempdir())


def add_feed(db, config=None, name="Fake Feed", interval=60):
    return db.add_feed(
        name,
        "rss",
        "https://example.test/feed",
        json.dumps(config or {}),
        True,
        interval,
    )


def set_feed_config(db, feed_id, config):
    """Rewrites a feed's config_json while keeping the row's own values."""
    feed = db.get_feed(feed_id)
    db.update_feed(
        feed_id,
        feed["name"],
        feed["feed_type"],
        feed["url"],
        json.dumps(config),
        bool(feed["enabled"]),
        bool(feed["auto_request"]),
        feed["check_interval_minutes"],
    )


def stored_config(db, feed_id):
    return json.loads(db.get_feed(feed_id)["config_json"] or "{}")


def activity_events(db, event_type):
    return [row for row in db.get_logs(200) if row["event_type"] == event_type]


# ---------------------------------------------------------------------------
# 1. Pure helpers in app.providers.catchup
# ---------------------------------------------------------------------------
def test_helper_timestamps():
    section("1. catchup: timestamp parsing and comparison")
    expected = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    for value in (
        "2026-11-01T12:00:00+00:00",
        "2026-11-01T12:00:00Z",
        "2026-11-01 12:00:00",
        "2026-11-01T12:00:00.000000+00:00",
        "2026-11-01",
    ):
        parsed = catchup.parse_timestamp(value)
        if value == "2026-11-01":
            check(parsed == datetime(2026, 11, 1, 0, 0, tzinfo=UTC), f"date-only parse wrong: {value!r} -> {parsed!r}")
        else:
            check(parsed == expected, f"ISO parse wrong: {value!r} -> {parsed!r}")

    epoch = expected.timestamp()
    check(catchup.parse_timestamp(int(epoch)) == expected, "epoch int parse wrong")
    check(catchup.parse_timestamp(str(int(epoch))) == expected, "epoch string parse wrong")
    check(catchup.parse_timestamp(float(epoch)) == expected, "epoch float parse wrong")

    naive = datetime(2026, 11, 1, 12, 0)
    check(catchup.parse_timestamp(naive) == expected, "naive datetime must be assumed UTC")

    offset = datetime(2026, 11, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    check(catchup.parse_timestamp(offset) == expected, "aware datetime must be normalised to UTC")

    rfc = catchup.parse_timestamp("01 Nov 2026 12:00:00 +0000")
    check(rfc == expected, f"RFC 2822 pubDate parse wrong: {rfc!r}")

    for garbage in (None, "", "   ", "not a date", True, False, [], {}):
        check(catchup.parse_timestamp(garbage) is None, f"garbage must parse to None: {garbage!r}")

    check(catchup.to_iso("2026-11-01T12:00:00Z") == "2026-11-01T12:00:00+00:00", "to_iso normalisation wrong")

    check(catchup.is_newer_than(iso(3), iso(2)) is True, "newer timestamp must be newer")
    check(catchup.is_newer_than(iso(1), iso(2)) is False, "older timestamp must not be newer")
    check(catchup.is_newer_than(iso(2), iso(2)) is False, "equal timestamp must not be newer")
    check(catchup.is_newer_than(None, iso(2)) is True, "unknown item timestamp must fail open")
    check(catchup.is_newer_than(iso(1), None) is True, "missing cutoff must keep the item")
    check(catchup.is_newer_than(iso(1), "garbage") is True, "unparseable cutoff must fail open")


def test_helper_selection():
    section("2. catchup: cutoff filtering, anchor truncation, watermark maths")
    items = [
        {"guid": "g5", "pub_date": iso(5)},
        {"guid": "g4", "pub_date": iso(4)},
        {"guid": "g3", "pub_date": iso(3)},
        {"guid": "g2", "pub_date": iso(2)},
    ]

    kept = catchup.filter_newer_than(items, iso(3))
    check([item["guid"] for item in kept] == ["g5", "g4"], f"filter_newer_than wrong: {kept}")

    truncated = catchup.truncate_at_anchor(items, "g3")
    check([item["guid"] for item in truncated] == ["g5", "g4"], f"anchor truncation wrong: {truncated}")
    check(catchup.truncate_at_anchor(items, None) == items, "missing anchor must not truncate")
    check(catchup.truncate_at_anchor(items, "does-not-exist") == items, "absent anchor must not truncate")

    kept, reason = catchup.truncate_newest_first(items, cutoff_iso=iso(2), stop_guid="g3")
    check([item["guid"] for item in kept] == ["g5", "g4"], f"combined truncation wrong: {kept}")
    check(reason == "anchor", f"anchor must win over cutoff: {reason!r}")

    kept, reason = catchup.truncate_newest_first(items, cutoff_iso=iso(3))
    check([item["guid"] for item in kept] == ["g5", "g4"], f"cutoff truncation wrong: {kept}")
    check(reason == "cutoff", f"cutoff reason wrong: {reason!r}")

    kept, reason = catchup.truncate_newest_first(items[:2], cutoff_iso=iso(1))
    check(len(kept) == 2 and reason is None, "exhausted list without boundary must report None")
    kept, reason = catchup.truncate_newest_first(items, cutoff_iso=None, stop_guid=None)
    check(kept == items and reason is None, "no boundaries must return the whole list")

    mixed = [
        {"guid": "a", "pub_date": iso(4)},
        {"guid": "b", "pub_date": "unparseable"},
        {"guid": "c", "pub_date": iso(1)},
        {"guid": "d"},
    ]
    check(catchup.oldest_pub_date(mixed) == iso(1), "oldest_pub_date must pick the earliest parseable date")
    check(catchup.oldest_pub_date([{"guid": "x"}]) is None, "oldest_pub_date must be None without timestamps")
    check(catchup.newest_identity(mixed) == "a", "newest_identity must use the first item's GUID")
    check(catchup.newest_identity([]) is None, "empty list has no identity")

    deduped = catchup.dedupe_by_identity([
        {"guid": "one", "link": "https://l/1"},
        {"guid": "one", "link": "https://l/1"},
        {"guid": "two", "link": "https://l/2"},
        {"guid": "three", "link": "https://l/1"},
    ])
    check([item["guid"] for item in deduped] == ["one", "two"], f"dedupe wrong: {deduped}")

    for value, expected_pages in (
        (None, catchup.DEFAULT_CATCHUP_MAX_PAGES),
        ("7", 7),
        (7.9, 7),
        (0, 1),
        (-5, 1),
        (9999, catchup.HARD_MAX_CATCHUP_PAGES),
        ("abc", catchup.DEFAULT_CATCHUP_MAX_PAGES),
        (object(), catchup.DEFAULT_CATCHUP_MAX_PAGES),
    ):
        check(
            catchup.clamp_max_pages(value) == expected_pages,
            f"clamp_max_pages({value!r}) != {expected_pages}",
        )


def test_helper_watermarks():
    section("3. catchup: watermark computation (first scan / reached / capped)")
    newest = make_item(5, iso(5), guid_prefix="wk")
    older = make_item(1, iso(1), guid_prefix="wk")
    items = [newest, make_item(3, iso(3), guid_prefix="wk3"), older]
    started = "2026-11-10T09:00:00+00:00"

    cutoff, anchor = catchup.compute_scan_watermarks(None, None, items, False, started)
    check(catchup.parse_timestamp(cutoff) == catchup.parse_timestamp(iso(1)), "first-scan cutoff must be the oldest item")
    check(anchor == newest.guid, f"first-scan anchor wrong: {anchor!r}")

    cutoff, anchor = catchup.compute_scan_watermarks(None, "old-anchor", [], False, started)
    check(cutoff == started, "first scan without items must use scan_started_at")
    check(anchor == "old-anchor", "empty scan must keep the previous anchor")

    advanced, _ = catchup.compute_scan_watermarks(iso(1), "old-anchor", [newest], True, started)
    check(catchup.parse_timestamp(advanced) == catchup.parse_timestamp(iso(5)), "reached scan must advance to its oldest item")

    frozen, anchor = catchup.compute_scan_watermarks(iso(1), "old-anchor", [newest], False, started)
    check(frozen == iso(1), "page-capped scan must never move the cutoff")
    check(anchor == newest.guid, "page-capped scan still updates the anchor")

    same, _ = catchup.compute_scan_watermarks(iso(1), "old-anchor", [], True, started)
    check(same == iso(1), "reached scan without items must keep the cutoff")


# ---------------------------------------------------------------------------
# 2. Scheduler integration
# ---------------------------------------------------------------------------
def test_base_provider_default_path():
    section("3b. BaseFeedProvider: default fetch_releases_since(cutoff_iso/anchor)")
    old_iso = "2026-10-01T00:00:00+00:00"

    class StubProvider(BaseFeedProvider):
        """Minimal provider relying on the default catch-up implementation.

        Regression guard: the base default used to call the helper with a
        ``cutoff_iso=`` keyword that the helper did not accept, which raised
        ``TypeError`` on every second scan of RSS/HTML/generic feeds.
        """

        def __init__(self, items):
            super().__init__("stub", "https://example.test/feed", {})
            self._items = list(items)

        def fetch_releases(self):
            return list(self._items)

    items = [make_item(3, iso(3)), make_item(2, iso(2)), make_item(1, iso(1))]

    provider = StubProvider(items)
    kept = provider.fetch_releases_since(cutoff_iso=iso(2))
    check([item.guid for item in kept] == ["fake-guid-3"], f"base default cutoff path: {kept}")
    check(provider.last_scan_meta.get("reached_boundary") is True, "base cutoff boundary must be reported")
    check(provider.last_scan_meta.get("stop_reason") == "cutoff", f"base stop reason: {provider.last_scan_meta}")

    provider = StubProvider(items)
    kept = provider.fetch_releases_since(cutoff_iso=None, stop_guid="fake-guid-2")
    check([item.guid for item in kept] == ["fake-guid-3"], f"base default anchor path: {kept}")
    check(provider.last_scan_meta.get("stop_reason") == "anchor", f"base anchor reason: {provider.last_scan_meta}")

    provider = StubProvider(items)
    kept = provider.fetch_releases_since(cutoff_iso=old_iso)
    check(len(kept) == 3, f"items newer than the cutoff must all be kept: {kept}")
    check(provider.last_scan_meta.get("reached_boundary") is False, "exhausted window cannot prove coverage")
    check(provider.last_scan_meta.get("stop_reason") == "single_window", f"base single-window reason: {provider.last_scan_meta}")
    check(provider.last_scan_meta.get("max_pages") == catchup.DEFAULT_CATCHUP_MAX_PAGES,
          f"base max_pages default: {provider.last_scan_meta}")


def test_first_scan_stores_watermarks():
    section("4. Scheduler: first scan uses fetch_releases and stores watermarks")
    db = make_feed_db()
    tm = new_manager(db)
    try:
        feed_id = add_feed(db, {"max_pages": 2})
        FakeProvider.reset(normal_items=[
            make_item(3, iso(3)),
            make_item(2, iso(2)),
            make_item(1, iso(1)),
        ])
        with patched_provider():
            result = tm.check_feeds()

        check(result["status"] == "success", f"check_feeds status: {result}")
        for key in ("total_found", "total_requested", "skipped_manga", "skipped_audiobooks"):
            check(key in result, f"legacy return key missing: {key}")
        check(result["total_found"] == 3, f"first scan total_found: {result['total_found']}")
        check(result["feeds_catchup"] == 0, "first scan must not report catch-up")
        check(len(FakeProvider.normal_calls) == 1, "first scan must call fetch_releases")
        check(FakeProvider.since_calls == [], "first scan must not request catch-up")

        config = stored_config(db, feed_id)
        check(config.get("last_scan_oldest_pub_date") == iso(1), f"stored cutoff: {config}")
        check(config.get("last_scan_anchor_guid") == "fake-guid-3", f"stored anchor: {config}")
        check(catchup.parse_timestamp(config.get("last_scan_started_at")) is not None, "scan_started_at must be stored")
        check(db.get_feed(feed_id)["last_status"] == "OK", "feed status must be OK")
        check(activity_events(db, "FEED_CATCHUP") == [], "first scan must not log FEED_CATCHUP")
    finally:
        tm._cover_executor.shutdown(wait=False)


def test_second_scan_extends():
    section("5. Scheduler: catch-up scan pages back to the previous scan and advances")
    db = make_feed_db()
    tm = new_manager(db)
    try:
        feed_id = add_feed(db)
        FakeProvider.reset(normal_items=[make_item(2, iso(2)), make_item(1, iso(1))])
        with patched_provider():
            first = tm.check_feeds()
        check(first["total_found"] == 2, f"seed scan total_found: {first}")

        FakeProvider.reset(
            since_items=[make_item(5, iso(5)), make_item(4, iso(4))],
            since_meta={
                "reached_boundary": True,
                "pages_scanned": 4,
                "extra_pages": 2,
                "extra_items": 3,
                "stop_reason": "cutoff",
                "max_pages_reached": False,
            },
        )
        with patched_provider():
            second = tm.check_feeds()

        check(len(FakeProvider.normal_calls) == 0, "catch-up scan must not fall back to fetch_releases")
        check(len(FakeProvider.since_calls) == 1, "catch-up scan must call fetch_releases_since once")
        call = FakeProvider.since_calls[0]
        check(catchup.parse_timestamp(call["cutoff_iso"]) == catchup.parse_timestamp(iso(1)),
              f"cutoff forwarded to provider: {call}")
        check(call["stop_guid"] == "fake-guid-2", f"anchor forwarded to provider: {call}")
        check(call["max_pages"] == 10, f"default page budget must be 10: {call}")

        check(second["total_found"] == 2, f"extended scan total_found: {second}")
        check(second["feeds_catchup"] == 1, f"feeds_catchup: {second}")
        check(second["catchup_extra_items"] == 3, f"catchup_extra_items: {second}")

        config = stored_config(db, feed_id)
        check(catchup.parse_timestamp(config["last_scan_oldest_pub_date"]) == catchup.parse_timestamp(iso(4)),
              f"watermark must advance to the oldest item seen: {config}")
        check(config["last_scan_anchor_guid"] == "fake-guid-5", f"anchor must advance: {config}")

        events = activity_events(db, "FEED_CATCHUP")
        check(len(events) == 1, f"one FEED_CATCHUP activity entry expected: {events}")
        details = events[0]["details"] or ""
        check("extra_pages=2" in details and "stop_reason=cutoff" in details, f"activity details: {details}")
    finally:
        tm._cover_executor.shutdown(wait=False)


def test_catchup_disabled():
    section("6. Scheduler: feed_catchup_enabled=false keeps the legacy scan")
    db = make_feed_db()
    tm = new_manager(db)
    try:
        feed_id = add_feed(db)
        db.set_setting("feed_catchup_enabled", "false")
        FakeProvider.reset(normal_items=[make_item(1, iso(1))])
        with patched_provider():
            tm.check_feeds()

        FakeProvider.reset(normal_items=[make_item(3, iso(3)), make_item(2, iso(2))])
        with patched_provider():
            result = tm.check_feeds()

        check(FakeProvider.since_calls == [], "disabled catch-up must never call fetch_releases_since")
        check(len(FakeProvider.normal_calls) == 1, "disabled catch-up must call fetch_releases")
        check(result["feeds_catchup"] == 0, "disabled catch-up must not report catch-up")
        config = stored_config(db, feed_id)
        check(catchup.parse_timestamp(config["last_scan_oldest_pub_date"]) == catchup.parse_timestamp(iso(2)),
              f"legacy scan watermark must follow the window: {config}")
        check(config["last_scan_anchor_guid"] == "fake-guid-3", f"legacy scan anchor: {config}")
    finally:
        tm._cover_executor.shutdown(wait=False)


def test_page_cap_freezes_watermark():
    section("7. Scheduler: a page-capped catch-up keeps the old watermark (no gap)")
    db = make_feed_db()
    tm = new_manager(db)
    try:
        feed_id = add_feed(db)
        FakeProvider.reset(normal_items=[make_item(2, iso(2)), make_item(1, iso(1))])
        with patched_provider():
            tm.check_feeds()
        before = stored_config(db, feed_id)
        check(before["last_scan_oldest_pub_date"] == iso(1), f"seed watermark: {before}")

        FakeProvider.reset(
            since_items=[make_item(6, iso(6)), make_item(5, iso(5))],
            since_meta={
                "reached_boundary": False,
                "pages_scanned": 10,
                "extra_pages": 8,
                "extra_items": 0,
                "stop_reason": "page_cap",
                "max_pages_reached": True,
            },
        )
        with patched_provider():
            result = tm.check_feeds()

        after = stored_config(db, feed_id)
        check(after["last_scan_oldest_pub_date"] == iso(1),
              f"capped scan must not advance the watermark past unscanned releases: {after}")
        check(after["last_scan_anchor_guid"] == "fake-guid-6", f"anchor still advances: {after}")
        check(result["max_pages_reached"] == 1, f"max_pages_reached counter: {result}")
        check(len(activity_events(db, "FEED_CATCHUP")) == 1, "capped catch-up must be logged")
    finally:
        tm._cover_executor.shutdown(wait=False)


def test_global_setting_and_feed_override():
    section("8. Scheduler: page budget = clamped per-feed override or global setting")
    db = make_feed_db()
    tm = new_manager(db)
    try:
        feed_id = add_feed(db)

        def next_call_max_pages():
            FakeProvider.reset(
                since_items=[],
                since_meta={"reached_boundary": True, "pages_scanned": 1, "stop_reason": "cutoff"},
            )
            with patched_provider():
                tm.check_feeds()
            check(len(FakeProvider.since_calls) == 1, "expected exactly one catch-up call")
            return FakeProvider.since_calls[0]["max_pages"]

        # Seed a watermark, then exercise the override/setting combinations.
        FakeProvider.reset(normal_items=[make_item(1, iso(1))])
        with patched_provider():
            tm.check_feeds()

        set_feed_config(db, feed_id, {
            "catchup_max_pages": 999,
            "last_scan_oldest_pub_date": iso(1),
            "last_scan_anchor_guid": "fake-guid-1",
        })
        check(next_call_max_pages() == catchup.HARD_MAX_CATCHUP_PAGES, "override must clamp to 50")

        set_feed_config(db, feed_id, {
            "catchup_max_pages": 0,
            "last_scan_oldest_pub_date": iso(1),
            "last_scan_anchor_guid": "fake-guid-1",
        })
        check(next_call_max_pages() == 1, "override must clamp up to 1")

        set_feed_config(db, feed_id, {
            "catchup_max_pages": "abc",
            "last_scan_oldest_pub_date": iso(1),
            "last_scan_anchor_guid": "fake-guid-1",
        })
        db.set_setting("feed_catchup_max_pages", "7")
        check(next_call_max_pages() == 7, "invalid override must fall back to the setting")

        set_feed_config(db, feed_id, {
            "catchup_max_pages": 2,
            "last_scan_oldest_pub_date": iso(1),
            "last_scan_anchor_guid": "fake-guid-1",
        })
        check(next_call_max_pages() == 2, "per-feed override must win over the setting")

        set_feed_config(db, feed_id, {
            "last_scan_oldest_pub_date": iso(1),
            "last_scan_anchor_guid": "fake-guid-1",
        })
        db.set_setting("feed_catchup_max_pages", "not-a-number")
        check(next_call_max_pages() == catchup.DEFAULT_CATCHUP_MAX_PAGES,
              "invalid setting must fall back to the default")

        with db.get_connection() as conn:
            conn.cursor().execute("DELETE FROM app_settings WHERE key = 'feed_catchup_max_pages';")
            conn.commit()
        check(next_call_max_pages() == catchup.DEFAULT_CATCHUP_MAX_PAGES,
              "missing setting must default to 10 pages")
    finally:
        tm._cover_executor.shutdown(wait=False)


def test_jnovels_anchor_fallback():
    section("9. Scheduler: anchor-GUID catch-up for feeds without timestamps (J-Novels)")
    db = make_feed_db()
    tm = new_manager(db)
    try:
        # Simulate a J-Novels feed whose previous scan only persisted an anchor:
        # no publish dates are available for the items.
        feed_id = add_feed(db, {"last_scan_anchor_guid": "jnovels-anchor"})
        FakeProvider.reset(
            since_items=[
                make_item(9, "unknown publication date", guid_prefix="jnovels"),
                make_item(8, "unknown publication date", guid_prefix="jnovels"),
            ],
            since_meta={
                "reached_boundary": True,
                "pages_scanned": 3,
                "extra_pages": 2,
                "extra_items": 0,
                "stop_reason": "anchor",
                "max_pages_reached": False,
            },
        )
        with patched_provider():
            result = tm.check_feeds()

        check(len(FakeProvider.since_calls) == 1, "anchor catch-up must call fetch_releases_since")
        call = FakeProvider.since_calls[0]
        check(call["cutoff_iso"] is None, f"no timestamp cutoff when the feed has none: {call}")
        check(call["stop_guid"] == "jnovels-anchor", f"anchor must be forwarded: {call}")
        check(result["total_found"] == 2, f"anchor scan total_found: {result['total_found']}")
        check(result["feeds_catchup"] == 1, f"feeds_catchup: {result}")

        config = stored_config(db, feed_id)
        check(catchup.parse_timestamp(config["last_scan_oldest_pub_date"]) is not None,
              "watermark must be a valid timestamp even without item dates")
        check(config["last_scan_anchor_guid"] == "jnovels-9", f"anchor must advance to the newest item: {config}")
    finally:
        tm._cover_executor.shutdown(wait=False)


def test_error_path_keeps_watermarks():
    section("10. Scheduler: a failing feed is ERROR and never persists watermarks")
    db = make_feed_db()
    tm = new_manager(db)
    try:
        feed_id = add_feed(db)
        FakeProvider.reset(normal_items=[make_item(2, iso(2)), make_item(1, iso(1))])
        with patched_provider():
            tm.check_feeds()
        before = stored_config(db, feed_id)
        check(before.get("last_scan_oldest_pub_date") == iso(1), f"seed watermark: {before}")
        catchup_events_before = len(activity_events(db, "FEED_CATCHUP"))

        FakeProvider.reset(
            since_items=[make_item(3, iso(3))],
            since_meta={"reached_boundary": True, "pages_scanned": 2, "stop_reason": "cutoff"},
            error=RuntimeError("simulated provider failure"),
        )
        with patched_provider():
            result = tm.check_feeds()

        check(result["status"] == "success", "one failing feed must not abort the scan")
        check(result["total_found"] == 0, f"failed feed must contribute nothing: {result}")
        feed = db.get_feed(feed_id)
        check(feed["last_status"] == "ERROR", f"feed status must be ERROR: {feed['last_status']}")
        check("simulated provider failure" in (feed["last_error"] or ""), f"feed error text: {feed['last_error']}")

        after = stored_config(db, feed_id)
        check(after == before, f"failed scan must not rewrite watermarks: before={before} after={after}")
        check(len(activity_events(db, "FEED_CATCHUP")) == catchup_events_before,
              "failed scan must not log a catch-up extension")
    finally:
        tm._cover_executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
TESTS = [
    ("helpers/timestamps", test_helper_timestamps),
    ("helpers/selection", test_helper_selection),
    ("helpers/watermarks", test_helper_watermarks),
    ("base provider default", test_base_provider_default_path),
    ("first scan", test_first_scan_stores_watermarks),
    ("second scan extends", test_second_scan_extends),
    ("catch-up disabled", test_catchup_disabled),
    ("page cap freezes watermark", test_page_cap_freezes_watermark),
    ("page budget settings", test_global_setting_and_feed_override),
    ("jnovels anchor fallback", test_jnovels_anchor_fallback),
    ("error path", test_error_path_keeps_watermarks),
]


def main():
    print("=" * 78)
    print("  NovelTrax time-aware feed catch-up tests")
    print(f"  provider registry stub: {REGISTRY_STUBBED}")
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
                print(traceback.format_exc())
    finally:
        cleanup_temp_databases()

    print("\n" + "=" * 78)
    if failures:
        print(f"  {len(TESTS) - len(failures)}/{len(TESTS)} test groups passed, {len(failures)} failed")
        print("=" * 78)
        raise SystemExit(1)
    print(f"  all {len(TESTS)} test groups passed")
    print("=" * 78)
    print("ALL FEED-CATCHUP TESTS PASSED")


if __name__ == "__main__":
    main()
