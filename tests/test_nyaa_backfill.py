#!/usr/bin/env python3
"""Nyaa historical-backfill phase inside the polite historical backfill.

Policy under test
-----------------
``TaskManager.run_polite_backfill`` used to walk only the J-Novels master index,
J-Novels synopses and the J-Novels historical pages; the only Nyaa backfill was
the separate targeted per-series search.  This suite pins the new resumable
Nyaa deep-pagination phase:

* enabled by ``backfill_include_nyaa`` (or the ``include_nyaa`` kwarg, which
  overrides the setting for one run);
* the cursor lives in ``backfill_nyaa_next_page`` + ``backfill_nyaa_done`` and
  advances page by page, so a stopped / page-capped / transiently failing run
  resumes instead of restarting;
* ``max_pages == -1`` skips both deep phases, ``0`` is unlimited and ``> 0``
  caps this run's pages (same semantics as the J-Novels phase);
* empty crawl results end the phase and mark it done, EXCEPT a transient
  ``stop_reason == "error"`` crawl, which keeps the cursor un-done;
* the shared ``_ingest_backfill_releases`` helper persists both phases' rows
  (J-Novels behaviour stays identical; the Nyaa phase also drops PDFs/manga);
* ``reset_nyaa_backfill_cursor()`` rewinds the cursor and logs the reset.

Hermetic: standard library + ``app.database`` + ``app.scheduler`` only.  The
provider stack (bs4/feedparser/httpx) is not installed in the lightweight test
runner, so ``app.providers.registry`` is stubbed before ``app.scheduler`` is
imported and ``app.providers.nyaa`` / ``app.providers.jnovels`` are replaced in
``sys.modules`` with fake providers; no network access happens.

``tempfile.mkdtemp()`` directories are not writable in this DSH Windows
sandbox, so the temp database directory is created explicitly and removed in
``finally`` (the same helper pattern as tests/test_torbox_manager.py).

Bare ``assert`` is never used (Calibre's -O2 interpreter would strip it); every
expectation goes through :func:`check`, which raises ``AssertionError``.

Usage::

    & $py tests\\run_tests.py nyaa_backfill
    & $py tests\\test_nyaa_backfill.py
"""

import asyncio
import gc
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import types

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
for _path in (PROJECT_ROOT, BACKEND_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

TEMPLATES_DIR = os.path.join(BACKEND_DIR, "app", "templates")
SETTINGS_TEMPLATE = os.path.join(TEMPLATES_DIR, "settings.html")
DISCOVERY_TEMPLATE = os.path.join(TEMPLATES_DIR, "discovery.html")
MAIN_PY = os.path.join(BACKEND_DIR, "app", "main.py")
SCHEDULER_PY = os.path.join(BACKEND_DIR, "app", "scheduler.py")

# ---------------------------------------------------------------------------
# Import app.scheduler without the third-party provider stack.
# scheduler.py imports app.providers.registry at module level and the real
# registry imports the bs4/feedparser-based providers, which are absent here.
# ---------------------------------------------------------------------------
REGISTRY_STUBBED = False
try:  # pragma: no cover - depends on the interpreter environment
    from app.scheduler import TaskManager
    import app.scheduler as scheduler_module  # noqa: F401
except ImportError:
    _registry_stub = types.ModuleType("app.providers.registry")

    def _unpatched_create_provider(*args, **kwargs):
        raise RuntimeError("create_provider was not patched for this test")

    _registry_stub.create_provider = _unpatched_create_provider
    sys.modules["app.providers.registry"] = _registry_stub
    from app.scheduler import TaskManager
    import app.scheduler as scheduler_module  # noqa: F401

    REGISTRY_STUBBED = True

from app.database import Database
from app.providers.base import ReleaseItem

# ---------------------------------------------------------------------------
# check() helper -- explicit raises, never bare asserts
# ---------------------------------------------------------------------------
passed_count = 0
failed_count = 0
_TEMP_COUNTER = 0
_TEMP_ENVS = []


def check(condition, message=""):
    global passed_count, failed_count
    if not condition:
        failed_count += 1
        raise AssertionError("Check failed: %s" % (message or "unnamed condition"))
    passed_count += 1
    print("  OK %s" % (message or "condition"))


def check_equal(actual, expected, message=""):
    check(
        actual == expected,
        "%s (expected %r, got %r)" % (message or "values equal", expected, actual),
    )


def section(title):
    print("\n--- %s ---" % title)


# ---------------------------------------------------------------------------
# Temp dir / environment
# ---------------------------------------------------------------------------
def _make_temp_dir(label):
    """Create a unique temp dir under the system TEMP (mkdtemp is unusable here)."""
    global _TEMP_COUNTER
    _TEMP_COUNTER += 1
    path = os.path.join(
        tempfile.gettempdir(),
        "noveltrax-nyaa-backfill-%d-%d-%s" % (os.getpid(), _TEMP_COUNTER, label),
    )
    os.makedirs(path, exist_ok=True)
    return path


class BackfillEnv:
    """Temp database + ``TaskManager`` with the network layers neutralised."""

    def __init__(self, settings=None):
        _TEMP_ENVS.append(self)
        self.tmpdir = _make_temp_dir("env")
        self.db = Database(os.path.join(self.tmpdir, "noveltrax.db"))
        for key, value in (settings or {}).items():
            self.db.set_setting(key, str(value))
        self.tm = TaskManager(self.db, os.path.join(self.tmpdir, "calibre"))
        # The master-index ingest needs the provider stack and would also hit
        # the network: replace it with a no-op success.
        self.tm.ingest_master_index = lambda: {
            "status": "success",
            "total_indexed": 0,
            "linked_calibre": 0,
            "new_discoveries": 0,
        }
        # Media classification is network/verify-budget dependent in the real
        # resolver; every fixture here is a novel.
        self.tm.classify_media = lambda release: "light_novel"

    def run(self, **kwargs):
        kwargs.setdefault("enrich_synopses", False)
        kwargs.setdefault("delay_seconds", 0.01)
        return asyncio.run(self.tm.run_polite_backfill(**kwargs))

    def setting(self, key, default=""):
        return self.db.get_setting(key, default)

    def cleanup(self):
        tm = self.tm
        self.tm = None
        if tm is not None:
            try:
                tm._cover_executor.shutdown(wait=False)
            except Exception:
                pass
        self.db = None
        gc.collect()
        for _attempt in range(10):
            try:
                shutil.rmtree(self.tmpdir)
                return
            except OSError:
                gc.collect()
                time.sleep(0.05)
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def cleanup_all_envs():
    while _TEMP_ENVS:
        env = _TEMP_ENVS.pop()
        try:
            env.cleanup()
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------------------
# Fake providers injected into sys.modules
# ---------------------------------------------------------------------------
EVENTS = []


def reset_events():
    del EVENTS[:]


class FakeJNovels:
    """Stands in for ``app.providers.jnovels.JNovelsProvider``."""

    pages = {}
    calls = []

    @classmethod
    def reset(cls, pages=None):
        cls.pages = dict(pages or {})
        cls.calls = []

    def __init__(self, name="Fake J-Novels", url=None, config=None):
        self.name = name
        self.url = url or ""
        self.config = config or {}

    def _build_paged_url(self, base_url, page_num):
        return "%s&page=%d" % (base_url, page_num)

    def fetch_releases(self):
        page = 1
        if "page=" in self.url:
            try:
                page = int(self.url.rsplit("page=", 1)[1])
            except (IndexError, ValueError):
                page = 1
        FakeJNovels.calls.append({"page": page})
        EVENTS.append(("jnovels", page))
        return list(FakeJNovels.pages.get(page, []))


class FakeNyaa:
    """Stands in for ``app.providers.nyaa.NyaaNovelProvider``."""

    pages = {}
    error_pages = set()
    calls = []
    stop_after_page = None
    stop_callback = None

    @classmethod
    def reset(cls, pages=None, error_pages=None):
        cls.pages = dict(pages or {})
        cls.error_pages = set(error_pages or ())
        cls.calls = []
        cls.stop_after_page = None
        cls.stop_callback = None

    def __init__(self, name="Fake Nyaa", url=None, config=None):
        self.name = name
        self.url = url
        self.config = config or {}
        self.last_crawl_meta = {}

    def crawl_pages(self, max_pages=1, start_page=1, query=None, date_cutoff=None,
                    delay=None, progress_cb=None, stop_check=None):
        FakeNyaa.calls.append({
            "start_page": start_page,
            "max_pages": max_pages,
            "has_stop_check": callable(stop_check),
        })
        EVENTS.append(("nyaa", start_page))

        if start_page in FakeNyaa.error_pages:
            self.last_crawl_meta = {"pages_scanned": 0, "stop_reason": "error", "items": 0}
            return []

        rows = list(FakeNyaa.pages.get(start_page, []))
        if rows:
            self.last_crawl_meta = {
                "pages_scanned": 1,
                "stop_reason": "page_cap",
                "items": len(rows),
            }
        else:
            self.last_crawl_meta = {"pages_scanned": 0, "stop_reason": "end_of_feed", "items": 0}

        if (FakeNyaa.stop_after_page is not None
                and start_page >= FakeNyaa.stop_after_page
                and FakeNyaa.stop_callback is not None):
            FakeNyaa.stop_callback()
        return rows


# Replace the real provider modules BEFORE run_polite_backfill imports them
# (the imports are function-local so they resolve through sys.modules).
sys.modules["app.providers.jnovels"] = types.SimpleNamespace(JNovelsProvider=FakeJNovels)
sys.modules["app.providers.nyaa"] = types.SimpleNamespace(NyaaNovelProvider=FakeNyaa)


# ---------------------------------------------------------------------------
# Fixtures / DB helpers
# ---------------------------------------------------------------------------
def jnovels_item(series, volume, guid):
    label = "Vol. %d" % int(volume) if volume is not None else ""
    title = ("%s %s Epub" % (series, label)).replace("  ", " ").strip()
    return ReleaseItem(
        title=title,
        link="https://jnovels.com/%s/" % guid,
        series_name=series,
        volume=volume,
        guid=guid,
        source_feed_name="J-Novels (EPUB)",
    )


def nyaa_item(page, index):
    series = "Nyaa Backfill Test Series %d-%d" % (page, index)
    return ReleaseItem(
        title="[Yen Press] %s, Vol. 1 [Digital] [EPUB]" % series,
        link="https://nyaa.si/view/%d%d" % (page, index),
        guid="nyaa-backfill-%d-%d" % (page, index),
        series_name=series,
        volume=1.0,
        download_url="magnet:?xt=urn:btih:%d%d" % (page, index),
        source_feed_name="Nyaa Light Novels",
    )


def stored_guids(db):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT guid FROM releases;")
        return {row["guid"] for row in cur.fetchall()}


def events_of_type(db, event_type, limit=400):
    return [
        row for row in db.get_logs(limit=limit)
        if (row.get("event_type") or "") == event_type
    ]


def latest_event(db, event_type, limit=400):
    rows = events_of_type(db, event_type, limit=limit)
    return rows[0] if rows else None


def event_detail(db, event_type, limit=400):
    row = latest_event(db, event_type, limit=limit)
    if not row:
        return {}
    try:
        return json.loads(row.get("details") or "{}")
    except (TypeError, ValueError):
        return {}


def new_env(settings=None):
    """Fresh env; the caller is responsible for resetting the fake providers."""
    reset_events()
    return BackfillEnv(settings=settings)


# ---------------------------------------------------------------------------
# 1. J-Novels phase first, then Nyaa from page 1; both rows reach the DB.
# ---------------------------------------------------------------------------
def test_jnovels_phase_runs_before_nyaa_page_one():
    section("1. J-Novels pagination first, then Nyaa from page 1; rows ingested")
    FakeJNovels.reset(pages={
        1: [
            jnovels_item("Fake Jnovels Discovery Alpha", 1.0, "jnovels-alpha-vol-1"),
            jnovels_item("Fake Jnovels Unowned Beta", 2.0, "jnovels-beta-vol-2"),
            jnovels_item("Fake Jnovels Monitored Gamma", 3.0, "jnovels-monitored-vol-3"),
        ],
    })
    FakeNyaa.reset(pages={1: [nyaa_item(1, 1)], 2: []})
    env = new_env()
    try:
        # A monitored series takes the matcher/auto-request branch of the shared
        # ingestion helper (the J-Novels phase's pre-existing behaviour).
        env.db.sync_calibre_series([{
            "series_id": 9601,
            "series_name": "Fake Jnovels Monitored Gamma",
            "book_count": 1,
            "latest_index": 1.0,
        }])

        result = env.run(max_pages=1)
        check(result is None or isinstance(result, dict), "run_polite_backfill completes")

        check_equal(len(FakeJNovels.calls), 1, "max_pages=1 caps J-Novels to page 1")
        check(bool(FakeNyaa.calls), "Nyaa phase ran")
        check_equal(FakeNyaa.calls[0]["start_page"], 1, "Nyaa phase starts at page 1")
        check_equal(FakeNyaa.calls[0]["max_pages"], 1, "Nyaa crawl requests one page per call")
        check(FakeNyaa.calls[0]["has_stop_check"], "Nyaa crawl receives the stop_check callable")

        check(("jnovels", 1) in EVENTS, "J-Novels page 1 was fetched")
        check(("nyaa", 1) in EVENTS, "Nyaa page 1 was fetched")
        check(
            EVENTS.index(("jnovels", 1)) < EVENTS.index(("nyaa", 1)),
            "J-Novels phase runs before the Nyaa phase",
        )

        guids = stored_guids(env.db)
        check("jnovels-alpha-vol-1" in guids, "J-Novels vol-1 discovery row is persisted")
        check("nyaa-backfill-1-1" in guids, "Nyaa page-1 row is persisted")
        check(
            "jnovels-monitored-vol-3" in guids,
            "J-Novels monitored-series row is persisted through the matcher branch",
        )
        check(
            "jnovels-beta-vol-2" not in guids,
            "J-Novels unowned volume 2 is still rejected by the shared ingestion policy",
        )

        detail = event_detail(env.db, "NYAA_BACKFILL_FINISHED")
        check(detail, "NYAA_BACKFILL_FINISHED activity log was written")
        check_equal(detail.get("pages"), 1, "log detail reports 1 Nyaa page")
        check_equal(detail.get("started_at_page"), 1, "log detail reports the start page")
        check_equal(detail.get("reached_end"), False, "log detail reports the page-cap stop")
        check((detail.get("upserted") or 0) >= 1, "log detail counts upserted rows")

        finished = latest_event(env.db, "BACKFILL_FINISHED")
        check(finished is not None, "BACKFILL_FINISHED activity log was written")
        check(
            "upserted=" in (finished.get("details") or ""),
            "BACKFILL_FINISHED detail carries the upserted counter",
        )
        check(
            "rejected=" in (finished.get("details") or ""),
            "BACKFILL_FINISHED detail carries the rejected counter",
        )
        progress = env.setting("backfill_progress", "")
        check(
            "Nyaa historical backfill:" in progress,
            "the final backfill progress line keeps the Nyaa summary: %r" % progress,
        )
        check_equal(env.setting("backfill_status", ""), "completed", "backfill status is completed")
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# 2. The cursor advances page by page and a second run resumes at the next page.
# ---------------------------------------------------------------------------
def test_cursor_advances_and_second_run_resumes():
    section("2. Cursor advances per page; a second run resumes at the next page")
    FakeJNovels.reset(pages={1: []})
    FakeNyaa.reset(pages={1: [nyaa_item(1, 1)], 2: [nyaa_item(2, 1)], 3: []})
    env = new_env()
    try:
        env.run(max_pages=1)
        check_equal(env.setting("backfill_nyaa_next_page", "1"), "2", "cursor advanced to page 2 after page 1")
        check_equal(env.setting("backfill_last_page", ""), "2", "cursor is mirrored to backfill_last_page")
        check_equal(env.setting("backfill_nyaa_done", "false"), "false", "phase is not marked done while resuming")
        calls_after_first = list(FakeNyaa.calls)
        check_equal(len(calls_after_first), 1, "first run scanned exactly one Nyaa page")
        check_equal(calls_after_first[0]["start_page"], 1, "first run started at page 1")

        detail = event_detail(env.db, "NYAA_BACKFILL_FINISHED")
        check_equal(detail.get("next_page"), 2, "log detail points at page 2 for the next run")

        FakeJNovels.reset(pages={1: []})
        env.run(max_pages=1)
        scanned = [call["start_page"] for call in FakeNyaa.calls]
        check_equal(scanned, [1, 2], "second run resumed at page 2 (did not restart at page 1)")
        check_equal(env.setting("backfill_nyaa_next_page", "1"), "3", "cursor advanced to page 3")

        detail = event_detail(env.db, "NYAA_BACKFILL_FINISHED")
        check_equal(detail.get("started_at_page"), 2, "second run reports started_at_page=2")

        guids = stored_guids(env.db)
        check("nyaa-backfill-1-1" in guids, "page-1 row persisted")
        check("nyaa-backfill-2-1" in guids, "page-2 row persisted by the resumed run")
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# 3. End of feed marks the phase done; a later run skips Nyaa entirely.
# ---------------------------------------------------------------------------
def test_end_of_feed_marks_done_and_later_run_skips():
    section("3. Empty page ends the phase, marks it done; later runs skip Nyaa")
    FakeJNovels.reset(pages={1: []})
    FakeNyaa.reset(pages={1: [nyaa_item(1, 1)], 2: [nyaa_item(2, 1)], 3: []})
    env = new_env()
    try:
        env.run(max_pages=0)
        scanned = [call["start_page"] for call in FakeNyaa.calls]
        check_equal(scanned, [1, 2, 3], "unlimited run scans pages 1, 2 then the empty page 3")
        check_equal(env.setting("backfill_nyaa_done", "false"), "true", "end of feed marks the phase done")
        check_equal(env.setting("backfill_nyaa_next_page", "1"), "1", "done cursor is reset to page 1")
        detail = event_detail(env.db, "NYAA_BACKFILL_FINISHED")
        check_equal(detail.get("reached_end"), True, "log detail reports reached_end")
        check_equal(detail.get("pages"), 2, "log detail counts the two content pages")
        check_equal(len(events_of_type(env.db, "NYAA_BACKFILL_FINISHED")), 1,
                    "exactly one NYAA_BACKFILL_FINISHED log per phase run")

        FakeJNovels.reset(pages={1: []})
        calls_before_second = len(FakeNyaa.calls)
        events_before = len(events_of_type(env.db, "NYAA_BACKFILL_FINISHED"))
        env.run(max_pages=0)
        check_equal(len(FakeNyaa.calls), calls_before_second,
                    "a done phase performs no further Nyaa page requests")
        check_equal(len(events_of_type(env.db, "NYAA_BACKFILL_FINISHED")), events_before,
                    "a done phase logs no new phase-finished event")
        check_equal(env.setting("backfill_nyaa_done", "false"), "true", "done flag survives the skipped run")
        check_equal(env.setting("backfill_nyaa_next_page", "1"), "1", "cursor stays at page 1")
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# 4. reset_nyaa_backfill_cursor() rewinds cursor + done and logs the reset.
# ---------------------------------------------------------------------------
def test_reset_clears_cursor_and_done():
    section("4. reset_nyaa_backfill_cursor() clears the cursor + done flag")
    FakeJNovels.reset(pages={1: []})
    FakeNyaa.reset(pages={1: [nyaa_item(1, 1)], 2: []})
    env = new_env()
    try:
        env.db.set_setting("backfill_nyaa_next_page", "7")
        env.db.set_setting("backfill_nyaa_done", "true")

        result = env.tm.reset_nyaa_backfill_cursor()
        check_equal(result.get("status"), "success", "reset returns success")
        check("message" in result, "reset returns a message")
        check_equal(env.setting("backfill_nyaa_next_page", "1"), "1", "reset rewinds the cursor to 1")
        check_equal(env.setting("backfill_nyaa_done", "false"), "false", "reset clears the done flag")
        check_equal(len(events_of_type(env.db, "NYAA_BACKFILL_RESET")), 1,
                    "NYAA_BACKFILL_RESET activity log was written")

        env.run(max_pages=0)
        first_page = FakeNyaa.calls[0]["start_page"] if FakeNyaa.calls else None
        check_equal(first_page, 1, "the next run re-scans from page 1 after a reset")
        check_equal(env.setting("backfill_nyaa_done", "false"), "true", "run re-scans to the end of the feed")
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# 5. include_nyaa=false (setting or kwarg) and max_pages=-1 skip the phase.
# ---------------------------------------------------------------------------
def test_include_nyaa_disabled_skips_phase():
    section("5. include_nyaa=false (setting or kwarg) skips the Nyaa phase")
    env = new_env(settings={"backfill_include_nyaa": "false", "backfill_nyaa_next_page": "5"})
    FakeJNovels.reset(pages={1: []})
    FakeNyaa.reset(pages={1: [nyaa_item(1, 1)]})
    try:
        env.run(max_pages=5)
        check_equal(len(FakeNyaa.calls), 0, "setting backfill_include_nyaa=false skips Nyaa crawling")
        check_equal(env.setting("backfill_nyaa_next_page", "1"), "5", "skipped phase leaves the cursor alone")
        check_equal(env.setting("backfill_nyaa_done", "false"), "false", "skipped phase leaves the done flag alone")
        check_equal(len(events_of_type(env.db, "NYAA_BACKFILL_FINISHED")), 0,
                    "skipped phase writes no NYAA_BACKFILL_FINISHED event")
    finally:
        env.cleanup()

    env = new_env()
    FakeJNovels.reset(pages={1: []})
    FakeNyaa.reset(pages={1: [nyaa_item(1, 1)]})
    try:
        env.run(max_pages=5, include_nyaa=False)
        check_equal(len(FakeNyaa.calls), 0, "include_nyaa=False kwarg skips Nyaa crawling")
        check_equal(env.setting("backfill_nyaa_next_page", "1"), "1",
                    "explicit False leaves the cursor at its default")
    finally:
        env.cleanup()


def test_max_pages_minus_one_skips_both_deep_phases():
    section("6. max_pages=-1 skips BOTH deep-pagination phases")
    env = new_env()
    FakeJNovels.reset(pages={1: [jnovels_item("Fake Jnovels Gamma", 1.0, "jnovels-gamma")]})
    FakeNyaa.reset(pages={1: [nyaa_item(1, 1)]})
    try:
        env.run(max_pages=-1, include_nyaa=True)
        check_equal(len(FakeJNovels.calls), 0, "max_pages=-1 skips the J-Novels pagination phase")
        check_equal(len(FakeNyaa.calls), 0, "max_pages=-1 skips the Nyaa phase")
        check_equal(env.setting("backfill_nyaa_next_page", "1"), "1", "cursor unchanged by a skipped run")
        check_equal(env.setting("backfill_nyaa_done", "false"), "false", "done flag unchanged")
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# 7. Positive max_pages caps the Nyaa pages of THIS run; the cursor is kept.
# ---------------------------------------------------------------------------
def test_max_pages_cap_respected():
    section("7. max_pages > 0 caps the Nyaa page budget and keeps the cursor")
    FakeJNovels.reset(pages={1: []})
    FakeNyaa.reset(pages={
        1: [nyaa_item(1, 1)],
        2: [nyaa_item(2, 1)],
        3: [nyaa_item(3, 1)],
        4: [],
    })
    env = new_env()
    try:
        env.run(max_pages=2)
        scanned = [call["start_page"] for call in FakeNyaa.calls]
        check_equal(scanned, [1, 2], "a max_pages=2 run scans exactly two Nyaa pages")
        check_equal(env.setting("backfill_nyaa_next_page", "1"), "3", "cursor points at the first unscanned page")
        check_equal(env.setting("backfill_nyaa_done", "false"), "false", "page cap does not mark the phase done")
        detail = event_detail(env.db, "NYAA_BACKFILL_FINISHED")
        check_equal(detail.get("pages"), 2, "log detail reports exactly the two scanned pages")
        check_equal(detail.get("reached_end"), False, "page cap is not an end-of-feed")
        check_equal(detail.get("next_page"), 3, "log detail reports the resume page")

        FakeJNovels.reset(pages={1: []})
        env.run(max_pages=2)
        scanned = [call["start_page"] for call in FakeNyaa.calls]
        check_equal(scanned, [1, 2, 3, 4], "the resumed run continues at page 3, then sees the empty page 4")
        check_equal(env.setting("backfill_nyaa_done", "false"), "true", "the resumed run reaches the end of the feed")
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# 8. Stop requested mid-run stops after the current page and keeps the cursor.
# ---------------------------------------------------------------------------
def test_stop_mid_run_keeps_cursor():
    section("8. stop_backfill mid-run stops after the current page and keeps the cursor")
    FakeJNovels.reset(pages={1: []})
    FakeNyaa.reset(pages={1: [nyaa_item(1, 1)], 2: [nyaa_item(2, 1)]})
    env = new_env()
    FakeNyaa.stop_after_page = 1
    FakeNyaa.stop_callback = lambda: setattr(env.tm, "stop_backfill_requested", True)
    try:
        env.run(max_pages=0)
        scanned = [call["start_page"] for call in FakeNyaa.calls]
        check_equal(scanned, [1], "stop requested after page 1 finishes the current page only")
        check_equal(env.setting("backfill_nyaa_next_page", "1"), "2", "cursor points at the page after the stopped one")
        check_equal(env.setting("backfill_nyaa_done", "false"), "false", "a stop request does not mark the phase done")
        detail = event_detail(env.db, "NYAA_BACKFILL_FINISHED")
        check_equal(detail.get("pages"), 1, "log detail counts the one page finished before the stop")
        check_equal(detail.get("next_page"), 2, "log detail reports the resume page")

        FakeJNovels.reset(pages={1: []})
        FakeNyaa.reset(pages={2: [nyaa_item(2, 1)], 3: []})
        env.run(max_pages=0)
        scanned = [call["start_page"] for call in FakeNyaa.calls]
        check_equal(scanned, [2, 3], "the next run resumes at the stopped page and reaches the end")
        check_equal(env.setting("backfill_nyaa_done", "false"), "true", "resumed run completes the crawl")
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# 9. A transient Nyaa error does NOT mark the phase done; the cursor is kept.
# ---------------------------------------------------------------------------
def test_transient_error_does_not_mark_done():
    section("9. A transient Nyaa crawl error does not mark the phase done")
    FakeJNovels.reset(pages={1: []})
    FakeNyaa.reset(pages={1: []}, error_pages={1})
    env = new_env()
    try:
        env.run(max_pages=0)
        check_equal(len(FakeNyaa.calls), 1, "the failing page was attempted")
        check_equal(env.setting("backfill_nyaa_done", "false"), "false", "a transient error does not mark done")
        check_equal(env.setting("backfill_nyaa_next_page", "1"), "1", "the cursor stays on the failed page")
        detail = event_detail(env.db, "NYAA_BACKFILL_FINISHED")
        check(detail, "the phase still logs its outcome")
        check_equal(detail.get("reached_end"), False, "the error is not reported as end-of-feed")

        FakeJNovels.reset(pages={1: []})
        FakeNyaa.reset(pages={1: [nyaa_item(1, 1)], 2: []})
        env.run(max_pages=0)
        check_equal(FakeNyaa.calls[0]["start_page"], 1, "the recovery run retries the failed page")
        check_equal(env.setting("backfill_nyaa_done", "false"), "true", "the recovery run reaches the end of the feed")
        check("nyaa-backfill-1-1" in stored_guids(env.db), "the recovered page's row is ingested")
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# 10. The Nyaa phase additionally drops PDF and manga rows (EPUB novels only).
# ---------------------------------------------------------------------------
def test_nyaa_phase_skips_pdf_and_manga():
    section("10. Nyaa phase drops PDF and manga rows (EPUB novels only)")
    pdf_release = ReleaseItem(
        title="[Yen Press] Nyaa Backfill Test PDF Omega, Vol. 1 [PDF]",
        link="https://nyaa.si/view/99001",
        guid="nyaa-backfill-pdf-1",
        series_name="Nyaa Backfill Test PDF Omega",
        volume=1.0,
        download_url="magnet:?xt=urn:btih:pdf1",
        source_feed_name="Nyaa Light Novels",
    )
    manga_release = ReleaseItem(
        title="[Manga] Nyaa Backfill Test Manga Omega, Vol. 1 [EPUB]",
        link="https://nyaa.si/view/99002",
        guid="nyaa-backfill-manga-1",
        series_name="Nyaa Backfill Test Manga Omega",
        volume=1.0,
        download_url="magnet:?xt=urn:btih:manga1",
        source_feed_name="Nyaa Light Novels",
    )
    novel = nyaa_item(1, 7)
    FakeJNovels.reset(pages={1: []})
    FakeNyaa.reset(pages={1: [pdf_release, manga_release, novel], 2: []})
    env = new_env()
    try:
        env.tm.classify_media = (
            lambda release: "manga"
            if "Manga" in (getattr(release, "title", "") or "")
            else "light_novel"
        )
        env.run(max_pages=1)

        guids = stored_guids(env.db)
        check(novel.guid in guids, "the EPUB novel row is ingested")
        check("nyaa-backfill-pdf-1" not in guids, "a PDF release is never persisted by the Nyaa phase")
        check("nyaa-backfill-manga-1" not in guids, "a manga release is never persisted by the Nyaa phase")

        detail = event_detail(env.db, "NYAA_BACKFILL_FINISHED")
        check((detail.get("rejected") or 0) >= 2, "PDF/manga rows are counted as rejected")
        check_equal(detail.get("releases"), 1, "only the EPUB novel is counted as an ingested candidate")
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# 11. get_backfill_status() is additive-only and reports the new keys.
# ---------------------------------------------------------------------------
def test_backfill_status_additive_keys():
    section("11. get_backfill_status() additive Nyaa keys")
    env = new_env(settings={
        "backfill_max_pages": "12",
        "backfill_include_nyaa": "false",
        "backfill_nyaa_next_page": "4",
        "backfill_nyaa_done": "true",
    })
    try:
        status = env.tm.get_backfill_status()
        for key in ("running", "status", "progress", "delay_seconds", "max_pages"):
            check(key in status, "pre-existing status key %r preserved" % key)
        check_equal(status.get("max_pages"), 12, "max_pages still comes from the setting")
        check_equal(status.get("include_nyaa"), False, "include_nyaa exposes the setting as a bool")
        check_equal(status.get("nyaa_next_page"), 4, "nyaa_next_page exposes the cursor as an int")
        check_equal(status.get("nyaa_done"), True, "nyaa_done exposes the done flag as a bool")
        check(isinstance(status.get("nyaa_next_page"), int), "nyaa_next_page is an int")
    finally:
        env.cleanup()

    env = new_env()
    try:
        status = env.tm.get_backfill_status()
        check_equal(status.get("include_nyaa"), True, "include_nyaa defaults to true")
        check_equal(status.get("nyaa_next_page"), 1, "nyaa_next_page defaults to 1")
        check_equal(status.get("nyaa_done"), False, "nyaa_done defaults to false")
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# 12. Static source-level wiring guard (templates + routes as text).
# ---------------------------------------------------------------------------
def read_text(path):
    if not os.path.isfile(path):
        raise AssertionError("expected file not found: %s" % path)
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def test_static_wiring():
    section("12. Static UI/route wiring for the Nyaa backfill")
    settings_src = read_text(SETTINGS_TEMPLATE)
    check(
        'id="setting-backfill-include-nyaa"' in settings_src,
        "settings.html: missing the Include Nyaa historical pages checkbox id",
    )
    check(
        "Include Nyaa historical pages" in settings_src,
        "settings.html: missing the checkbox label",
    )
    check(
        "backfill_include_nyaa:" in settings_src,
        "settings.html: saveSettings() does not submit backfill_include_nyaa",
    )
    check(
        'id="btn-reset-nyaa-backfill"' in settings_src,
        "settings.html: missing the Reset Nyaa backfill cursor button id",
    )
    check(
        "resetNyaaBackfillCursor()" in settings_src,
        "settings.html: the reset button is not wired to resetNyaaBackfillCursor()",
    )
    check(
        "/api/backfill/nyaa/reset" in settings_src,
        "settings.html: resetNyaaBackfillCursor() does not POST /api/backfill/nyaa/reset",
    )

    discovery_src = read_text(DISCOVERY_TEMPLATE)
    check(
        "Nyaa" in discovery_src,
        "discovery.html: the start confirmation does not mention Nyaa",
    )
    check(
        "data.nyaa_next_page" in discovery_src,
        "discovery.html: the status banner does not render data.nyaa_next_page",
    )
    check(
        "data.nyaa_done" in discovery_src,
        "discovery.html: the status banner does not render data.nyaa_done",
    )

    main_src = read_text(MAIN_PY)
    check(
        '@app.post("/api/backfill/nyaa/reset")' in main_src,
        "main.py: missing POST /api/backfill/nyaa/reset",
    )
    check(
        "reset_nyaa_backfill_cursor()" in main_src,
        "main.py: the backfill routes never call reset_nyaa_backfill_cursor()",
    )
    check(
        "include_nyaa=include_nyaa" in main_src,
        "main.py: /api/backfill/start does not pass include_nyaa through to run_polite_backfill",
    )
    check(
        "include_nyaa: Optional[bool] = None" in read_text(SCHEDULER_PY),
        "scheduler.py: run_polite_backfill has no include_nyaa kwarg",
    )

    # Jinja delimiter balance (same guard the other UI wiring suites use).
    for path, label in ((SETTINGS_TEMPLATE, "settings.html"), (DISCOVERY_TEMPLATE, "discovery.html")):
        source = read_text(path)
        check_equal(source.count("{%"), source.count("%}"), "%s: balanced '{%%' vs '%%}'" % label)
        check_equal(source.count("{{"), source.count("}}"), "%s: balanced '{{' vs '}}'" % label)
        check_equal(
            len(re.findall(r"\{%-?\s*if\b", source)),
            len(re.findall(r"\{%-?\s*endif\s*-?%\}", source)),
            "%s: balanced if/endif" % label,
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
TESTS = [
    ("jnovels-then-nyaa", test_jnovels_phase_runs_before_nyaa_page_one),
    ("cursor-resume", test_cursor_advances_and_second_run_resumes),
    ("end-of-feed", test_end_of_feed_marks_done_and_later_run_skips),
    ("reset-cursor", test_reset_clears_cursor_and_done),
    ("include-nyaa-off", test_include_nyaa_disabled_skips_phase),
    ("max-pages-minus-one", test_max_pages_minus_one_skips_both_deep_phases),
    ("max-pages-cap", test_max_pages_cap_respected),
    ("stop-keeps-cursor", test_stop_mid_run_keeps_cursor),
    ("transient-error", test_transient_error_does_not_mark_done),
    ("pdf-manga-skip", test_nyaa_phase_skips_pdf_and_manga),
    ("status-keys", test_backfill_status_additive_keys),
    ("static-wiring", test_static_wiring),
]


def main():
    print("=" * 78)
    print("  NovelTrax Nyaa historical-backfill phase tests")
    print("  provider registry stub: %s" % REGISTRY_STUBBED)
    print("  temp dir: %s" % tempfile.gettempdir())
    print("=" * 78)

    failures = []
    try:
        for name, test in TESTS:
            try:
                test()
                print("  [PASS] %s" % name)
            except Exception as exc:  # noqa: BLE001 - report every failure
                failures.append((name, exc))
                print("  [FAIL] %s: %s: %s" % (name, type(exc).__name__, exc))
                print(traceback.format_exc())
    finally:
        cleanup_all_envs()

    print("\n" + "=" * 78)
    print("  checks passed: %d  failed: %d" % (passed_count, failed_count))
    if failures:
        print("  %d/%d test groups passed, %d failed" % (
            len(TESTS) - len(failures), len(TESTS), len(failures)))
        print("=" * 78)
        raise SystemExit(1)
    print("  all %d test groups passed" % len(TESTS))
    print("=" * 78)
    print("ALL NYAA BACKFILL TESTS PASSED")


if __name__ == "__main__":
    main()
