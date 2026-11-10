#!/usr/bin/env python3
"""Database migrations, download-state derivation and TorBox orchestration tests.

Covers the owner-B slice of the TorBox port contract:

* ``app.download_state.release_download_state`` (pure derivation table) and the
  four release-dict producers in ``app.database`` that attach it.
* ``app.database``: guarded migrations (columns + index), settings seeds and the
  exact download-bookkeeping methods of §3.2.
* ``app.torbox_manager.TorBoxManager``: submit (torrent-only guard, concurrency
  gate, handle persistence), poller branches (ready→fetch→COMPLETED, timeout,
  failed/stalled/processing, queued-slot + promotion, rate limit), selective
  planner (zip / files subset / size-cap raise), fetch containment, retry /
  remove / clear-failed, downloads-dir creation and the status payload rules.

Hermetic: a real temp ``Database`` and an injected ``FakeDownloader`` (no
network, no sleeping on real fetches — the async path uses a threading gate).

Bare ``assert`` is never used (Calibre's -O2 interpreter would strip it); every
expectation goes through :func:`check`, which raises ``AssertionError``.

Usage::

    python tests/test_torbox_manager.py
    python tests/run_tests.py torbox_manager
"""

import gc
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.database import Database
from app.download_state import release_download_state
from app.downloader.base import (
    DownloaderAuthError,
    DownloaderError,
    DownloaderFileError,
    DownloaderNotFoundError,
    DownloaderRateLimitError,
)
from app.torbox_manager import (
    DEFAULT_DOWNLOADS_DIR,
    TORBOX_TORRENT_ONLY_MESSAGE,
    TorBoxManager,
    _parse_db_timestamp,
    _safe_dirname,
)

# Non-strippable assertion helper (Calibre runs with sys.flags.optimize == 2).
passed_count = 0
failed_count = 0

_TEMP_COUNTER = 0
_MAGNET = "magnet:?xt=urn:btih:" + "0123456789abcdef" * 2 + "01234567"
_TORRENT_URL = "https://nyaa.si/download/123456.torrent"


def check(condition, msg=""):
    global passed_count, failed_count
    if not condition:
        failed_count += 1
        raise AssertionError("Check failed: %s" % (msg or "unnamed condition"))
    passed_count += 1
    print("  OK %s" % (msg or "condition"))


def check_equal(actual, expected, msg=""):
    check(actual == expected, "%s (expected %r, got %r)" % (msg or "values equal", expected, actual))


def check_contains(haystack, needle, msg=""):
    check(
        needle in str(haystack or ""),
        "%s (expected %r in %r)" % (msg or "text contains", needle, str(haystack or "")[:300]),
    )


def check_raises(exc_type, func, msg="", contains=None):
    try:
        func()
    except exc_type as exc:
        if contains is not None:
            check_contains(str(exc), contains, "%s (message)" % (msg or exc_type.__name__))
        else:
            check(True, msg or ("raises %s" % exc_type.__name__))
        return
    except Exception as exc:  # noqa: BLE001 - wrong type is a failure
        raise AssertionError(
            "%s: expected %s, got %s: %s" % (msg or "raises", exc_type.__name__,
                                             type(exc).__name__, exc)
        )
    raise AssertionError("%s: expected %s, nothing was raised" % (msg or "raises", exc_type.__name__))


def _make_temp_dir(label):
    """Create a unique temp dir under the system TEMP.

    ``tempfile.mkdtemp()`` is unusable in this sandbox (its ACLs deny creating
    children), so create the directory explicitly and remove it in ``finally``.
    """
    global _TEMP_COUNTER
    _TEMP_COUNTER += 1
    path = os.path.join(
        tempfile.gettempdir(),
        "noveltrax-torbox-manager-%d-%d-%s" % (os.getpid(), _TEMP_COUNTER, label),
    )
    os.makedirs(path, exist_ok=True)
    return path


def _json_column(row, key):
    try:
        return json.loads(row.get(key) or "[]")
    except (TypeError, ValueError):
        return None


def _log_event_types(db, limit=100):
    try:
        return [str(entry.get("event_type") or "") for entry in db.get_logs(limit=limit)]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Fake engine
# ---------------------------------------------------------------------------
class FakeDownloader:
    """In-memory ``DownloaderClient`` used through ``downloader_factory``."""

    name = "torbox"

    def __init__(self, enabled=True):
        self.enabled = bool(enabled)
        self.torrent_id = 123
        self.queued_id = None
        self.hash_value = "a" * 40
        self.cached = False
        self.submitted = []
        self.fetched = []
        self.cancelled = []
        self.statuses = {}
        self.fail_submit = None
        self.status_error = None
        self.fetch_error = None
        self.fetch_size = 1200
        self.fetch_gate = None
        self.cancel_result = True
        self.connection_result = {
            "success": True,
            "engine": "torbox",
            "plan": "Essential",
            "active_slots": 1,
            "message": "Connected to TorBox (Essential); 1 active slot(s).",
        }
        self.connection_error = None
        self.engines_requested = []

    # -- protocol -------------------------------------------------------
    def submit(self, release, savepath, wanted_volumes=None):
        if self.fail_submit is not None:
            raise self.fail_submit
        wanted = [float(value) for value in (wanted_volumes or [])]
        self.submitted.append({
            "release": release,
            "savepath": savepath,
            "wanted": wanted,
        })
        if self.queued_id is not None:
            handle = {
                "engine": "torbox",
                "torrent_id": None,
                "queued_id": int(self.queued_id),
                "hash": self.hash_value,
                "savepath": savepath,
                "wanted_volumes": wanted,
            }
            return {
                "handle": handle,
                "state": "queued_slot",
                "cached": False,
                "queued_id": int(self.queued_id),
            }
        handle = {
            "engine": "torbox",
            "torrent_id": int(self.torrent_id),
            "hash": self.hash_value,
            "savepath": savepath,
            "wanted_volumes": wanted,
        }
        return {
            "handle": handle,
            "state": "ready" if self.cached else "queued",
            "cached": bool(self.cached),
        }

    def status(self, handle):
        if self.status_error is not None:
            raise self.status_error
        handle = handle or {}
        key = handle.get("torrent_id")
        if key is None:
            key = handle.get("queued_id")
        info = self.statuses.get(key)
        if info is None:
            info = self.statuses.get(str(key))
        if info is None:
            info = {"state": "queued", "raw_state": "queued", "files": [], "size": 0}
        return dict(info)

    def fetch_files(self, handle, files, dest_dir, progress_cb=None):
        self.fetched.append({
            "handle": dict(handle or {}),
            "files": files,
            "dest_dir": dest_dir,
        })
        if self.fetch_gate is not None:
            self.fetch_gate.wait(10.0)
        if self.fetch_error is not None:
            raise self.fetch_error
        os.makedirs(dest_dir, exist_ok=True)
        saved = []
        if isinstance(files, list) and files:
            for item in files:
                name = os.path.basename(str(item.get("name") or "file.bin"))
                path = os.path.join(dest_dir, name)
                with open(path, "wb") as handle_fp:
                    handle_fp.write(b"x" * 8)
                saved.append({
                    "name": name,
                    "path": path,
                    "size": int(item.get("size") or 8),
                })
        else:
            path = os.path.join(dest_dir, "torrent.zip")
            with open(path, "wb") as handle_fp:
                handle_fp.write(b"x" * 8)
            saved.append({"name": "torrent.zip", "path": path, "size": int(self.fetch_size)})
        if progress_cb is not None:
            try:
                progress_cb("file", int(self.fetch_size))
            except Exception:
                pass
        return saved

    def cancel(self, handle, delete_remote=True):
        self.cancelled.append(dict(handle or {}))
        return bool(self.cancel_result)

    def test_connection(self):
        if self.connection_error is not None:
            raise self.connection_error
        return dict(self.connection_result)


class ManagerEnv:
    """Temp database + downloads dir + fake engine + manager."""

    def __init__(self, settings=None):
        self.tmpdir = _make_temp_dir("env")
        self.db_path = os.path.join(self.tmpdir, "noveltrax.db")
        self.db = Database(self.db_path)
        self.downloads = os.path.join(self.tmpdir, "downloads")
        self._counter = 0
        self.fake = FakeDownloader()
        self.db.set_setting("downloader_engine", "torbox")
        self.db.set_setting("torbox_enabled", "true")
        self.db.set_setting("torbox_api_key", "unit-test-api-key")
        self.db.set_setting("downloads_dir", self.downloads)
        for key, value in (settings or {}).items():
            self.db.set_setting(key, value)
        self.manager = TorBoxManager(
            self.db, downloader_factory=self._factory,
        )

    def _factory(self, engine):
        self.fake.engines_requested.append(engine)
        return self.fake

    def add_release(self, title, link=None, series_name="Overland", volume=None, **extra):
        self._counter += 1
        data = {
            "guid": "torbox-test-guid-%d" % self._counter,
            "title": title,
            "series_name": series_name,
            "volume": volume,
            "link": _MAGNET if link is None else link,
        }
        data.update(extra)
        ok, release_id = self.db.upsert_release(data)
        if not ok or release_id < 0:
            raise AssertionError("could not insert test release %r" % title)
        return self.db.get_release_by_id(release_id)

    def age_started_at(self, release_id, value="2000-01-01 00:00:00"):
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE releases SET downloader_started_at = ? WHERE id = ?;",
                (value, release_id),
            )
            conn.commit()
        conn.close()

    def cleanup(self):
        # sqlite connections are context-managed (committed, not closed); on Windows
        # the open handles block rmtree until the GC finalises them.
        if self.manager is not None:
            try:
                self.manager.stop()
            except Exception:
                pass
        if self.fake is not None:
            self.fake.fetch_gate = None
        self.manager = None
        self.db = None
        self.fake = None
        gc.collect()
        for _attempt in range(10):
            try:
                shutil.rmtree(self.tmpdir)
                return
            except OSError:
                gc.collect()
                time.sleep(0.1)
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ===========================================================================
# 1. Database migrations / settings / §3.2 methods
# ===========================================================================
def test_database_migrations_and_methods():
    env = ManagerEnv()
    try:
        with env.db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(releases);")
            columns = {row["name"] for row in cur.fetchall()}
            cur.execute("PRAGMA index_list(releases);")
            indexes = {row["name"] for row in cur.fetchall()}
        conn.close()
        for column in (
            "downloader", "downloader_id", "downloader_started_at",
            "download_wanted_json", "download_error", "download_attempts",
            "torbox_progress_json", "download_files_json", "downloaded_at",
        ):
            check(column in columns, "releases has the %r column" % column)
        check("idx_releases_download_status" in indexes, "download_status index exists")

        settings = env.db.get_all_settings()
        for key in (
            "downloader_engine", "torbox_enabled", "torbox_api_key",
            "torbox_delete_after_download", "torbox_strict_volume_selection",
            "torbox_delete_on_failure", "torbox_download_concurrency",
            "torbox_parallel_chunks", "torbox_max_download_gb",
            "torbox_poll_seconds", "torbox_max_concurrent", "torbox_timeout_hours",
            "downloads_dir",
        ):
            check(key in settings, "setting %r is seeded" % key)
        check_equal(settings.get("downloader_engine"), "torbox",
                    "test env overrides the engine setting")
        check_equal(settings.get("torbox_max_concurrent"), "2", "torbox_max_concurrent default")
        check_equal(settings.get("downloads_dir"), env.downloads, "downloads_dir setting persisted")

        # Seeded defaults are checked on a pristine database (the env overrides above).
        pristine = Database(os.path.join(env.tmpdir, "pristine.db"))
        pristine_settings = pristine.get_all_settings()
        check_equal(pristine_settings.get("downloader_engine"), "qbit",
                    "downloader_engine defaults to qbit")
        check_equal(pristine_settings.get("torbox_download_concurrency"), "4",
                    "torbox_download_concurrency default is 4")
        check_equal(pristine_settings.get("torbox_timeout_hours"), "6",
                    "torbox_timeout_hours default is 6")
        check(pristine_settings.get("downloads_dir", ""),
              "downloads_dir default is seeded from DOWNLOAD_DIR")

        release = env.add_release("Overland Vol 5 [EPUB]", volume=5.0)
        release_id = release["id"]

        env.db.update_download_handle(
            release_id, "torbox", downloader_id="42",
            torrent_hash="b" * 40, wanted_volumes=[4.0, 5.0],
        )
        row = env.db.get_release_by_id(release_id)
        check_equal(row["download_status"], "DOWNLOADING", "update_download_handle sets DOWNLOADING")
        check_equal(row["downloader"], "torbox", "update_download_handle sets the engine")
        check_equal(str(row["downloader_id"]), "42", "update_download_handle persists the handle")
        check_equal(row["torrent_hash"], "b" * 40, "update_download_handle persists the hash")
        check_equal(_json_column(row, "download_wanted_json"), [4.0, 5.0],
                    "update_download_handle persists wanted volumes")
        check(bool(str(row["downloader_started_at"] or "").strip()),
              "update_download_handle stamps downloader_started_at")
        started_at = row["downloader_started_at"]

        env.db.set_downloader_id(release_id, "77")
        row = env.db.get_release_by_id(release_id)
        check_equal(str(row["downloader_id"]), "77", "set_downloader_id replaces the handle")
        check_equal(row["downloader_started_at"], started_at,
                    "set_downloader_id keeps downloader_started_at")

        merged = env.db.update_torbox_progress(
            release_id, phase="ready", downloaded_bytes=None, progress_pct=50.0,
        )
        check_equal(merged.get("phase"), "ready", "update_torbox_progress merges fields")
        check("downloaded_bytes" not in merged, "update_torbox_progress drops None fields")
        check_equal(merged.get("progress_pct"), 50.0, "update_torbox_progress persists values")
        merged = env.db.update_torbox_progress(release_id, phase="fetching")
        check_equal(merged.get("phase"), "fetching", "second progress merge updates the phase")
        check_equal(merged.get("progress_pct"), 50.0, "progress merge keeps earlier fields")
        check_equal(env.db.get_torbox_progress(release_id), merged,
                    "get_torbox_progress returns the merged object")

        check_equal(env.db.increment_download_attempt(release_id), 1, "attempt counter starts at 1")
        check_equal(env.db.increment_download_attempt(release_id), 2, "attempt counter increments")
        env.db.set_download_error(release_id, "disk full")
        check_equal(env.db.get_release_by_id(release_id)["download_error"], "disk full",
                    "set_download_error persists the message")
        env.db.clear_download_error(release_id)
        row = env.db.get_release_by_id(release_id)
        check_equal(row["download_error"], "", "clear_download_error clears the message")
        check_equal(int(row["download_attempts"]), 0, "clear_download_error resets attempts")

        env.db.set_download_files(release_id, ["/downloads/a.epub", "/downloads/b.epub"])
        row = env.db.get_release_by_id(release_id)
        check_equal(_json_column(row, "download_files_json"),
                    ["/downloads/a.epub", "/downloads/b.epub"], "set_download_files persists paths")
        check(bool(str(row["downloaded_at"] or "").strip()), "set_download_files stamps downloaded_at")

        active = env.db.get_active_torbox_downloads()
        check_equal([item["id"] for item in active], [release_id],
                    "get_active_torbox_downloads returns the DOWNLOADING torbox row")
        env.db.update_downloader_state(release_id, "FAILED", "torbox")
        engine_rows = env.db.get_downloader_releases("torbox", limit=10)
        check_equal([item["id"] for item in engine_rows], [release_id],
                    "get_downloader_releases returns FAILED torbox rows")
        check_equal(env.db.get_active_torbox_downloads(), [],
                    "get_active_torbox_downloads excludes FAILED rows")

        env.db.clear_downloader_submission(release_id, "torbox")
        row = env.db.get_release_by_id(release_id)
        check_equal(row["download_status"], "IDLE", "clear_downloader_submission resets the status")
        check(row["downloader_id"] in (None, ""), "clear_downloader_submission clears the handle")
        check(row["torrent_hash"] in (None, ""), "clear_downloader_submission clears the hash")
        check(row["downloader_started_at"] in (None, ""),
              "clear_downloader_submission clears the start time")
        check_equal(_json_column(row, "download_wanted_json"), [],
                    "clear_downloader_submission clears wanted volumes")
        check_equal(_json_column(row, "download_files_json"), [],
                    "clear_downloader_submission clears downloaded files")
        check(row["downloaded_at"] in (None, ""), "clear_downloader_submission clears downloaded_at")
        check_equal(env.db.get_torbox_progress(release_id), {},
                    "clear_downloader_submission clears progress")
    finally:
        env.cleanup()


# ===========================================================================
# 2. download_state derivation + attachment in the release producers
# ===========================================================================
def test_download_state_module():
    base = {"id": 7, "download_status": "IDLE", "status": "REQUESTED", "downloader": ""}
    state = release_download_state(base)
    check_equal(state["state"], "not_downloaded", "IDLE derives not_downloaded")
    check_equal(state["label"], "Not downloaded", "not_downloaded label")
    check_equal(state["release_id"], 7, "release_id comes from the row id")
    check(state["can_download"] and not state["can_retry"] and not state["terminal"],
          "not_downloaded is downloadable, not retryable, not terminal")

    downloading = release_download_state({
        "id": 8,
        "download_status": "DOWNLOADING",
        "downloader": "torbox",
        "torbox_progress_json": json.dumps({"progress_pct": 42.5, "speed_bps": 1024}),
    })
    check_equal(downloading["state"], "downloading", "DOWNLOADING derives downloading")
    check_equal(downloading["progress_pct"], 42.5, "downloading progress comes from progress JSON")
    check_equal(downloading["speed_bps"], 1024, "downloading speed comes from progress JSON")
    check_equal(downloading["engine"], "torbox", "engine comes from the downloader column")

    downloaded = release_download_state({"id": 9, "download_status": "COMPLETED"})
    check_equal(downloaded["state"], "downloaded", "COMPLETED derives downloaded")
    check(not downloaded["can_download"], "downloaded is not downloadable")

    failed = release_download_state({
        "id": 10, "download_status": "FAILED", "download_error": "timed out",
    })
    check_equal(failed["state"], "failed", "FAILED derives failed")
    check_equal(failed["error"], "timed out", "failed exposes the downloaded error")
    check(failed["can_download"] and failed["can_retry"], "failed is downloadable and retryable")

    failed_by_status = release_download_state({"id": 11, "status": "FAILED"})
    check_equal(failed_by_status["state"], "failed", "release status FAILED beats download_status")

    in_calibre = release_download_state({"id": 12, "status": "IN_CALIBRE"})
    check_equal(in_calibre["state"], "in_calibre", "IN_CALIBRE derives in_calibre")
    check(in_calibre["terminal"] and not in_calibre["can_download"],
          "in_calibre is terminal and not downloadable")

    # FAILED (download_status) must win over release status IN_CALIBRE (first match wins).
    precedence = release_download_state({"id": 13, "download_status": "FAILED", "status": "IN_CALIBRE"})
    check_equal(precedence["state"], "failed", "download_status FAILED wins over IN_CALIBRE")

    malformed = release_download_state({"id": 14, "torbox_progress_json": "not json"})
    check_equal(malformed["progress_pct"], 0.0, "malformed progress JSON degrades to 0")
    check_equal(release_download_state(None)["state"], "not_downloaded",
                "None input degrades to not_downloaded")

    env = ManagerEnv()
    try:
        release = env.add_release("Overland Vol 5 [EPUB]", volume=5.0)
        release_id = release["id"]
        env.db.update_download_handle(release_id, "torbox", downloader_id="9", wanted_volumes=[5.0])
        env.db.update_torbox_progress(release_id, phase="fetching", progress_pct=12.5, speed_bps=900)

        by_id = env.db.get_release_by_id(release_id)
        check_equal(by_id["download_state"]["state"], "downloading",
                    "get_release_by_id attaches download_state")
        check_equal(by_id["download_state"]["progress_pct"], 12.5,
                    "attached download_state includes progress")

        listed = [item for item in env.db.get_releases(limit=50) if item["id"] == release_id]
        check_equal(len(listed), 1, "get_releases returns the release")
        check("download_state" in listed[0], "get_releases attaches download_state")

        grouped = env.db.get_releases_grouped_by_series(include_ignored=True)
        grouped_rows = [
            row for group in grouped for row in group.get("releases", [])
            if row.get("id") == release_id
        ]
        check_equal(len(grouped_rows), 1, "grouped-by-series contains the release")
        check("download_state" in grouped_rows[0],
              "get_releases_grouped_by_series attaches download_state")

        prioritized = env.add_release(
            "Overland Vol 6 [EPUB]", volume=6.0, release_groups=["Test Priority Group"],
        )
        env.db.add_prioritized_group("Test Priority Group")
        prio_rows = [item for item in env.db.get_prioritized_releases(limit=50)
                     if item["id"] == prioritized["id"]]
        check_equal(len(prio_rows), 1, "get_prioritized_releases returns the prioritized release")
        check("download_state" in prio_rows[0], "get_prioritized_releases attaches download_state")
    finally:
        env.cleanup()


# ===========================================================================
# 3. submit_release: happy path, guard, concurrency gate
# ===========================================================================
def test_submit_happy_path():
    env = ManagerEnv(settings={"torbox_max_concurrent": "10"})
    try:
        release = env.add_release("Overland Vol 5 [EPUB]", volume=5.0)
        result = env.manager.submit_release(release, source="manual")
        check(result.get("success") is True, "submit happy path succeeds")
        check_equal(result.get("engine"), "torbox", "submit reports the engine")
        check_equal(result.get("state"), "downloading", "submit returns state=downloading")
        check_equal(len(env.fake.submitted), 1, "engine.submit was called once")

        row = env.db.get_release_by_id(release["id"])
        check_equal(row["download_status"], "DOWNLOADING", "submit marks the row DOWNLOADING")
        check_equal(row["downloader"], "torbox", "submit persists engine provenance")
        check_equal(str(row["downloader_id"]), "123", "submit persists the torrent handle")
        check_equal(row["torrent_hash"], "a" * 40, "submit persists the torrent hash")
        check_equal(_json_column(row, "download_wanted_json"), [5.0],
                    "submit persists the wanted volumes")
        check(bool(str(row["downloader_started_at"] or "").strip()),
              "submit stamps downloader_started_at")
        row_state = release_download_state(row)
        check_equal(row_state["state"], "downloading", "submit state visible through download_state")

        savepath = env.fake.submitted[0]["savepath"]
        check_equal(savepath, os.path.join(env.downloads, "Overland Vol 5 [EPUB]"),
                    "submit derives <downloads_dir>/<safe title> as savepath")
        check(os.path.isdir(env.downloads), "submit creates the downloads directory")
        check("TORBOX_SUBMITTED" in _log_event_types(env.db), "submit audits TORBOX_SUBMITTED")

        cached_release = env.add_release("Overland Vol 6 [EPUB]", volume=6.0)
        env.fake.cached = True
        cached_result = env.manager.submit_release(cached_release)
        check(cached_result.get("success") is True and cached_result.get("cached") is True,
              "cached submit reports cached=True")
        check("TORBOX_CACHED" in _log_event_types(env.db), "cached submit audits TORBOX_CACHED")
        env.fake.cached = False

        queued_release = env.add_release("Overland Vol 7 [EPUB]", volume=7.0)
        env.fake.queued_id = 55
        queued_result = env.manager.submit_release(queued_release)
        env.fake.queued_id = None
        check(queued_result.get("success") is True and queued_result.get("queued") is True,
              "queued-slot submit succeeds with queued=True")
        check_equal(queued_result.get("state"), "queued_slot", "queued-slot submit state")
        queued_row = env.db.get_release_by_id(queued_release["id"])
        check_equal(str(queued_row["downloader_id"]), "queued:55",
                    "queued handle is persisted as queued:<id>")

        env.fake.fail_submit = DownloaderError("TorBox rejected the magnet")
        failed_release = env.add_release("Overland Vol 8 [EPUB]", volume=8.0)
        failed_result = env.manager.submit_release(failed_release)
        env.fake.fail_submit = None
        check(failed_result.get("success") is False, "submit engine failure is reported, not raised")
        check_contains(failed_result.get("message"), "rejected the magnet",
                       "submit failure message carries the engine reason")
        check_equal(env.db.get_release_by_id(failed_release["id"])["download_status"], "IDLE",
                    "failed submit leaves the row IDLE")
        check("TORBOX_FAILED" in _log_event_types(env.db), "failed submit audits TORBOX_FAILED")

        env.db.set_setting("downloader_engine", "qbit")
        wrong_engine = env.manager.submit_release(release)
        check(wrong_engine.get("success") is False, "submit refuses when TorBox is not the engine")
        check_contains(wrong_engine.get("message"), "not the active download engine",
                       "wrong-engine message")
    finally:
        env.cleanup()


def test_torrent_only_guard():
    env = ManagerEnv(settings={"torbox_max_concurrent": "10"})
    try:
        jnovels = env.add_release(
            "Overland Vol 1 [EPUB]",
            link="https://jnovels.com/overland-volume-1-epub/",
            volume=1.0,
            guid="https://jnovels.com/overland-volume-1-epub/",
        )
        rejected = env.manager.submit_release(jnovels)
        check(rejected.get("success") is False, "non-torrent submit is rejected")
        check(rejected.get("skipped") is True, "non-torrent submit is a skip, not an error")
        check_equal(rejected.get("message"), TORBOX_TORRENT_ONLY_MESSAGE,
                    "non-torrent rejection uses the contract message")
        check_equal(env.fake.submitted, [], "no engine submit for a web-page link")
        check_equal(env.db.get_release_by_id(jnovels["id"])["download_status"], "IDLE",
                    "rejected web-page release stays IDLE")
        check("TORBOX_SKIPPED" in _log_event_types(env.db), "rejection audits TORBOX_SKIPPED")

        torrent_release = env.add_release(
            "Overland Vol 2 [EPUB]", link=_TORRENT_URL, volume=2.0,
        )
        check(env.manager.submit_release(torrent_release).get("success") is True,
              ".torrent URL is accepted")

        download_path_release = env.add_release(
            "Overland Vol 3 [EPUB]", link="https://nyaa.si/download/777",
            volume=3.0, guid="https://nyaa.si/download/777",
        )
        check(env.manager.submit_release(download_path_release).get("success") is True,
              "/download/ URL is accepted")

        guid_flip = env.add_release(
            "Overland Vol 4 [EPUB]",
            link="https://jnovels.com/overland-volume-4-epub/",
            volume=4.0,
            guid="https://nyaa.si/view/4242",
        )
        check(env.manager.submit_release(guid_flip).get("success") is True,
              "a torrent guid flips an otherwise web-page link into a torrent release")
    finally:
        env.cleanup()


def test_concurrency_gate():
    env = ManagerEnv(settings={"torbox_max_concurrent": "1"})
    try:
        first = env.add_release("Overland Vol 1 [EPUB]", volume=1.0)
        second = env.add_release("Overland Vol 2 [EPUB]", volume=2.0)
        check(env.manager.submit_release(first).get("success") is True, "first submit fills the gate")
        blocked = env.manager.submit_release(second)
        check(blocked.get("success") is False, "second submit is blocked by concurrency")
        check(blocked.get("retryable") is True, "concurrency rejection is retryable")
        check_contains(blocked.get("message"), "concurrency limit reached",
                       "concurrency rejection message")
        check_equal(len(env.fake.submitted), 1, "blocked submit never reaches the engine")
        check_equal(env.db.get_release_by_id(second["id"])["download_status"], "IDLE",
                    "blocked release stays IDLE")

        # A queued-slot row does not hold an active TorBox slot: it must not count.
        env.db.set_downloader_id(first["id"], "queued:31")
        check(env.manager.submit_release(second).get("success") is True,
              "queued-slot rows are excluded from the concurrency gate")
        check_equal(len(env.fake.submitted), 2, "queued-excluded submit reaches the engine")
    finally:
        env.cleanup()


# ===========================================================================
# 4. poll pass: ready -> fetch -> COMPLETED
# ===========================================================================
def test_poll_ready_to_fetch_completion():
    env = ManagerEnv()
    try:
        release = env.add_release("Overland Vol 5 [EPUB]", volume=5.0)
        env.manager.submit_release(release)
        env.fake.statuses[123] = {
            "state": "ready",
            "raw_state": "completed",
            "progress": 1.0,
            "size": 1200,
            "files": [{"id": 1, "name": "Overland Vol 5.epub", "size": 1200}],
            "cached": False,
        }

        result = env.manager.poll_once(async_fetch=False)
        check_equal(result.get("status"), "ok", "poll pass returns status ok")
        check_equal(result.get("processed"), 1, "poll pass processed one release")
        check_equal(result.get("completed"), 1, "poll pass counts one completed fetch")
        check_equal(result.get("failed"), 0, "poll pass counts no failures")

        row = env.db.get_release_by_id(release["id"])
        check_equal(row["download_status"], "COMPLETED", "ready release completes")
        check_equal(row["downloader"], "torbox", "completion keeps the engine")
        check(bool(str(row["downloaded_at"] or "").strip()), "completion stamps downloaded_at")
        files = _json_column(row, "download_files_json")
        check(bool(files), "completion persists download_files_json")
        check(os.path.isfile(files[0]), "persisted download file exists on disk")
        progress = env.db.get_torbox_progress(release["id"])
        check_equal(progress.get("phase"), "done", "completion moves the phase to done")
        check_contains(progress.get("fetch_started_at"), "T", "fetch_started_at is an ISO stamp")
        check(progress.get("downloaded_bytes", 0) > 0, "completion records downloaded bytes")

        dest_dir = os.path.join(env.downloads, "Overland Vol 5 [EPUB]")
        check_equal(env.fake.fetched[0]["dest_dir"], dest_dir,
                    "fetch destination is <downloads_dir>/<safe title>")
        check(env.fake.fetched[0]["files"] is None,
              "small all-needed bundle fetches the whole-torrent zip")
        check(env.fake.cancelled and env.fake.cancelled[-1].get("torrent_id") == 123,
              "delete-after-download cancels the remote torrent")
        check("TORBOX_DOWNLOADED" in _log_event_types(env.db),
              "completion audits TORBOX_DOWNLOADED")

        idle = env.manager.poll_once(async_fetch=False)
        check_equal(idle.get("status"), "idle", "a later poll pass with no active rows is idle")

        # Async path: poller hands ready releases to the bounded executor (deduped).
        async_release = env.add_release("Overland Vol 6 [EPUB]", volume=6.0)
        env.fake.submitted = []
        env.manager.submit_release(async_release)
        env.fake.torrent_id = 456
        env.fake.submitted = []
        env.manager.submit_release(async_release)  # resubmit: row now points at 456
        env.fake.statuses[456] = {
            "state": "ready",
            "raw_state": "completed",
            "progress": 1.0,
            "size": 500,
            "files": [{"id": 2, "name": "Overland Vol 6.epub", "size": 500}],
        }
        env.fake.fetch_gate = threading.Event()
        async_result = env.manager.poll_once(async_fetch=True)
        check_equal(async_result.get("queued_fetch"), 1,
                    "async poll queues the ready release on the fetch executor")
        row = env.db.get_release_by_id(async_release["id"])
        check_equal(row["download_status"], "DOWNLOADING",
                    "async fetch is still in flight right after the poll")
        duplicate = env.manager._submit_fetch(
            env.db.get_release_by_id(async_release["id"]), env.fake,
            {"torrent_id": 456, "hash": ""}, {"files": []},
        )
        check(duplicate is False, "per-release fetch dedupe rejects a second submission")
        env.fake.fetch_gate.set()
        env.manager._wait_for_fetches(timeout=10.0)
        row = env.db.get_release_by_id(async_release["id"])
        check_equal(row["download_status"], "COMPLETED", "async fetch completes the release")
    finally:
        env.cleanup()


# ===========================================================================
# 5. poll failure branches: failed / stalled / processing / rate limit / auth
# ===========================================================================
def test_poll_failure_branches():
    env = ManagerEnv(settings={"torbox_max_concurrent": "10"})
    try:
        # TorBox reports failed -> FAILED + remote delete.
        failed_release = env.add_release("Overland Vol 1 [EPUB]", volume=1.0)
        env.manager.submit_release(failed_release)
        env.fake.statuses[123] = {"state": "failed", "raw_state": "error", "files": [], "size": 0}
        result = env.manager.poll_once(async_fetch=False)
        check_equal(result.get("failed"), 1, "failed state counts as failed")
        row = env.db.get_release_by_id(failed_release["id"])
        check_equal(row["download_status"], "FAILED", "failed state marks the row FAILED")
        check_contains(row["download_error"], "reported state 'error'",
                       "failed state error names the raw TorBox state")
        check_contains(row["download_error"], "attempt 1", "failed state records the attempt")
        check(env.fake.cancelled and env.fake.cancelled[-1].get("torrent_id") == 123,
              "delete-on-failure removes the remote torrent")
        check("TORBOX_FAILED" in _log_event_types(env.db), "failed state audits TORBOX_FAILED")

        # stalled: surfaced once, never auto-retried, row stays DOWNLOADING.
        stalled_release = env.add_release("Overland Vol 2 [EPUB]", volume=2.0)
        env.fake.torrent_id = 200
        env.manager.submit_release(stalled_release)
        env.fake.statuses[200] = {"state": "stalled", "raw_state": "stalled", "files": [], "size": 0}
        result = env.manager.poll_once(async_fetch=False)
        check_equal(result.get("failed"), 0, "stalled is not a failure")
        check_equal(env.db.get_release_by_id(stalled_release["id"])["download_status"],
                    "DOWNLOADING", "stalled row stays DOWNLOADING")
        check("TORBOX_STALLED" in _log_event_types(env.db), "stalled audited once")

        # processing: persisted phase, keep polling, never fetch yet.
        processing_release = env.add_release("Overland Vol 3 [EPUB]", volume=3.0)
        env.fake.torrent_id = 300
        env.manager.submit_release(processing_release)
        env.fake.statuses[300] = {"state": "processing", "raw_state": "processing", "files": [], "size": 0}
        env.manager.poll_once(async_fetch=False)
        row = env.db.get_release_by_id(processing_release["id"])
        check_equal(row["download_status"], "DOWNLOADING", "processing keeps the row active")
        check_equal(env.db.get_torbox_progress(processing_release["id"]).get("phase"),
                    "processing", "processing persists the local phase")
        check("TORBOX_PROCESSING" in _log_event_types(env.db), "processing audited once")
        check_equal(env.fake.fetched, [], "processing never fetches files")

        # auth error -> terminal failure for that row.
        auth_release = env.add_release("Overland Vol 4 [EPUB]", volume=4.0)
        env.fake.torrent_id = 400
        env.manager.submit_release(auth_release)
        env.fake.status_error = DownloaderAuthError("TorBox rejected the API key (HTTP 401).")
        env.manager.poll_once(async_fetch=False)
        env.fake.status_error = None
        check_equal(env.db.get_release_by_id(auth_release["id"])["download_status"],
                    "FAILED", "auth error fails the release")

        # not-found -> terminal failure for that row.
        notfound_release = env.add_release("Overland Vol 5 [EPUB]", volume=5.0)
        env.fake.torrent_id = 500
        env.manager.submit_release(notfound_release)
        env.fake.status_error = DownloaderNotFoundError("TorBox torrent 500 is no longer in the account.")
        env.manager.poll_once(async_fetch=False)
        env.fake.status_error = None
        check_equal(env.db.get_release_by_id(notfound_release["id"])["download_status"],
                    "FAILED", "not-found fails the release")

        # generic engine error -> audit once + continue, row stays DOWNLOADING.
        transient_release = env.add_release("Overland Vol 6 [EPUB]", volume=6.0)
        env.fake.torrent_id = 600
        env.manager.submit_release(transient_release)
        env.fake.status_error = DownloaderError("TorBox HTTP 500 calling /torrents/mylist.")
        result = env.manager.poll_once(async_fetch=False)
        env.fake.status_error = None
        check_equal(env.db.get_release_by_id(transient_release["id"])["download_status"],
                    "DOWNLOADING", "transient engine error keeps the release active")
        check_equal(result.get("failed"), 0, "transient engine error is not a failure")

        # rate limit -> break the whole pass with rate_limited.
        rate_release = env.add_release("Overland Vol 7 [EPUB]", volume=7.0)
        env.fake.torrent_id = 700
        env.manager.submit_release(rate_release)
        env.fake.status_error = DownloaderRateLimitError("slow down", retry_after=30)
        result = env.manager.poll_once(async_fetch=False)
        env.fake.status_error = None
        check_equal(result.get("status"), "rate_limited", "rate limit aborts the poll pass")
        check_equal(env.db.get_release_by_id(rate_release["id"])["download_status"],
                    "DOWNLOADING", "rate-limited release stays active")

        # missing persisted handle -> failed.
        missing_release = env.add_release("Overland Vol 8 [EPUB]", volume=8.0)
        env.db.update_download_handle(missing_release["id"], "torbox", downloader_id="")
        env.manager.poll_once(async_fetch=False)
        check_equal(env.db.get_release_by_id(missing_release["id"])["download_status"],
                    "FAILED", "missing torrent_id/queued_id fails the release")
    finally:
        env.cleanup()


def test_fetch_failure_containment():
    env = ManagerEnv()
    try:
        release = env.add_release("Overland Vol 5 [EPUB]", volume=5.0)
        env.manager.submit_release(release)
        env.fake.statuses[123] = {
            "state": "ready",
            "raw_state": "completed",
            "progress": 1.0,
            "size": 1200,
            "files": [{"id": 1, "name": "Overland Vol 5.epub", "size": 1200}],
        }
        dest_dir = os.path.join(env.downloads, "Overland Vol 5 [EPUB]")
        os.makedirs(dest_dir, exist_ok=True)
        partial_path = os.path.join(dest_dir, ".torbox-download.zip.part")
        keep_path = os.path.join(dest_dir, "already-there.epub")
        with open(partial_path, "wb") as handle:
            handle.write(b"partial")
        with open(keep_path, "wb") as handle:
            handle.write(b"kept")

        env.fake.fetch_error = DownloaderFileError("Disk full while writing the archive.")
        result = env.manager.poll_once(async_fetch=False)
        check_equal(result.get("failed"), 1, "fetch failure counts as failed")
        row = env.db.get_release_by_id(release["id"])
        check_equal(row["download_status"], "FAILED", "fetch failure marks the row FAILED")
        check_contains(row["download_error"], "Fetch failed",
                       "fetch failure error is actionable")
        check(not os.path.exists(partial_path), "failed fetch removes .torbox-* partials")
        check(os.path.exists(keep_path), "failed fetch never removes user files")
        check_equal(env.db.get_torbox_progress(release["id"]).get("phase"), "failed",
                    "fetch failure persists phase failed")
    finally:
        env.cleanup()


# ===========================================================================
# 6. timeout, queued slot + promotion
# ===========================================================================
def test_timeout_failure():
    env = ManagerEnv(settings={"torbox_timeout_hours": "1"})
    try:
        release = env.add_release("Overland Vol 1 [EPUB]", volume=1.0)
        env.manager.submit_release(release)
        env.age_started_at(release["id"], "2000-01-01 00:00:00")
        result = env.manager.poll_once(async_fetch=False)
        check_equal(result.get("failed"), 1, "timed-out release counts as failed")
        row = env.db.get_release_by_id(release["id"])
        check_equal(row["download_status"], "FAILED", "timeout marks the row FAILED")
        check_contains(row["download_error"], "Timed out after", "timeout error message")
        check_equal(row["status"], "REQUESTED", "timed-out release stays REQUESTED for a retry")

        queued = env.add_release("Overland Vol 2 [EPUB]", volume=2.0)
        env.fake.queued_id = 12
        env.manager.submit_release(queued)
        env.fake.queued_id = None
        env.age_started_at(queued["id"], "2000-01-01 00:00:00")
        env.manager.poll_once(async_fetch=False)
        queued_row = env.db.get_release_by_id(queued["id"])
        check_equal(queued_row["download_status"], "FAILED", "queued timeout fails the row")
        check_contains(queued_row["download_error"], "No free TorBox active slot",
                       "queued timeout explains the missing slot")

        disabled = ManagerEnv(settings={"torbox_timeout_hours": "0"})
        try:
            release0 = disabled.add_release("Overland Vol 3 [EPUB]", volume=3.0)
            disabled.manager.submit_release(release0)
            disabled.age_started_at(release0["id"], "2000-01-01 00:00:00")
            disabled.fake.statuses[123] = {"state": "downloading", "raw_state": "downloading",
                                           "files": [], "size": 0}
            disabled.manager.poll_once(async_fetch=False)
            check_equal(disabled.db.get_release_by_id(release0["id"])["download_status"],
                        "DOWNLOADING", "timeout_hours=0 disables the timeout")
        finally:
            disabled.cleanup()
    finally:
        env.cleanup()


def test_queued_slot_and_promotion():
    env = ManagerEnv()
    try:
        release = env.add_release("Overland Vol 5 [EPUB]", volume=5.0)
        env.fake.queued_id = 9
        submit = env.manager.submit_release(release)
        env.fake.queued_id = None
        check_equal(submit.get("state"), "queued_slot", "queued submit state")

        env.fake.statuses[9] = {
            "state": "queued_slot", "raw_state": "queued", "queue_position": 2,
            "active_limit": 2, "files": [], "size": 0,
        }
        result = env.manager.poll_once(async_fetch=False)
        check_equal(result.get("processed"), 1, "queued poll is processed")
        check_equal(result.get("failed"), 0, "queued poll is not a failure")
        check_equal(env.db.get_release_by_id(release["id"])["download_status"],
                    "DOWNLOADING", "queued row stays active")
        check("TORBOX_QUEUED" in _log_event_types(env.db), "queued slot audited once")

        # Promotion: queued id activates as a real torrent and is fetched this pass.
        env.fake.statuses[9] = {
            "state": "ready",
            "raw_state": "completed",
            "promoted_torrent_id": 555,
            "progress": 1.0,
            "size": 800,
            "files": [{"id": 1, "name": "Overland Vol 5.epub", "size": 800}],
        }
        env.fake.fetch_size = 800
        result = env.manager.poll_once(async_fetch=False)
        row = env.db.get_release_by_id(release["id"])
        check_equal(str(row["downloader_id"]), "555", "promotion persists the real torrent id")
        check_equal(row["download_status"], "COMPLETED", "promoted ready release is fetched")
        check("TORBOX_PROMOTED" in _log_event_types(env.db), "promotion audited once")

        # Poller cadence: idle backs off, ready/fetching keeps the floor.
        idle_manager = TorBoxManager(env.db, downloader_factory=env._factory)
        idle_interval = idle_manager._next_interval()
        floor = max(5.0, float(env.db.get_setting("torbox_poll_seconds", "5") or 5))
        check(idle_interval >= min(30.0, max(15.0, floor * 3)),
              "idle poll interval backs off to the idle cadence")
        busy = env.add_release("Overland Vol 6 [EPUB]", volume=6.0)
        env.db.update_download_handle(busy["id"], "torbox", downloader_id="456")
        env.db.update_torbox_progress(busy["id"], phase="ready")
        check_equal(idle_manager._next_interval(), floor,
                    "ready/fetching phase keeps the active poll floor")
    finally:
        env.cleanup()


# ===========================================================================
# 7. retry / remove / clear-failed
# ===========================================================================
def test_retry_release():
    env = ManagerEnv()
    try:
        check_equal(env.manager.retry_release(999999).get("code"), 404,
                    "retry of a missing release is 404")

        release = env.add_release("Overland Vol 5 [EPUB]", volume=5.0)
        env.manager.submit_release(release)
        env.fake.statuses[123] = {"state": "failed", "raw_state": "error", "files": [], "size": 0}
        env.manager.poll_once(async_fetch=False)
        check_equal(env.db.get_release_by_id(release["id"])["download_status"], "FAILED",
                    "release is FAILED before retry")

        env.fake.submitted = []
        env.fake.cancelled = []
        retry = env.manager.retry_release(release["id"])
        check_equal(retry.get("status"), "success", "retry resubmits successfully")
        check(env.fake.cancelled and env.fake.cancelled[-1].get("torrent_id") == 123,
              "retry cancels the stale remote handle first")
        check_equal(len(env.fake.submitted), 1, "retry re-submits to the engine")
        row = env.db.get_release_by_id(release["id"])
        check_equal(row["download_status"], "DOWNLOADING", "retry puts the row back to DOWNLOADING")
        check_equal(row["download_error"], "", "retry clears the previous error")
        check_equal(int(row["download_attempts"]), 0, "retry resets the attempt counter")

        rate_limited = env.manager.retry_release(release["id"])
        check_equal(rate_limited.get("code"), 429, "immediate second retry is rate-limited")

        engine_env = ManagerEnv(settings={"downloader_engine": "qbit"})
        try:
            wrong_engine_release = engine_env.add_release("Overland Vol 1 [EPUB]", volume=1.0)
            check_equal(engine_env.manager.retry_release(wrong_engine_release["id"]).get("code"),
                        409, "retry refuses when TorBox is not the active engine")
        finally:
            engine_env.cleanup()

        skipped_env = ManagerEnv()
        try:
            web_release = skipped_env.add_release(
                "Overland Vol 1 [EPUB]",
                link="https://jnovels.com/overland-volume-1-epub/",
                guid="https://jnovels.com/overland-volume-1-epub/",
                volume=1.0,
            )
            skipped_env.db.update_downloader_state(web_release["id"], "FAILED", "torbox")
            skipped = skipped_env.manager.retry_release(web_release["id"])
            check_equal(skipped.get("status"), "skipped",
                        "retry of a web-page release maps to status=skipped")
        finally:
            skipped_env.cleanup()
    finally:
        env.cleanup()


def test_remove_and_clear_failed():
    env = ManagerEnv()
    try:
        check_equal(env.manager.remove_release(999999).get("code"), 404,
                    "remove of a missing release is 404")

        failed_release = env.add_release("Overland Vol 1 [EPUB]", volume=1.0)
        env.manager.submit_release(failed_release)
        env.fake.statuses[123] = {"state": "failed", "raw_state": "error", "files": [], "size": 0}
        env.manager.poll_once(async_fetch=False)
        env.fake.cancelled = []
        removed = env.manager.remove_release(failed_release["id"])
        check_equal(removed.get("status"), "success", "remove FAILED row succeeds")
        check_equal(removed.get("remote_deleted"), False, "FAILED row is not re-cancelled remotely")
        check_equal(env.fake.cancelled, [], "FAILED remove makes no remote call")
        row = env.db.get_release_by_id(failed_release["id"])
        check_equal(row["download_status"], "IDLE", "remove resets the row to IDLE")
        check(row["downloader_id"] in (None, ""), "remove clears the engine handle")
        check_equal(_json_column(row, "download_files_json"), [], "remove clears fetched files")
        check_equal(row["download_error"], "", "remove clears the error")

        active_release = env.add_release("Overland Vol 2 [EPUB]", volume=2.0)
        env.fake.torrent_id = 222
        env.manager.submit_release(active_release)
        env.fake.cancelled = []
        removed_active = env.manager.remove_release(active_release["id"])
        check_equal(removed_active.get("remote_deleted"), True,
                    "active row attempts the remote delete")
        check(env.fake.cancelled and env.fake.cancelled[-1].get("torrent_id") == 222,
              "active remove cancels the remote torrent")

        clear_env = ManagerEnv()
        try:
            for index in range(2):
                item = clear_env.add_release("Overland Vol %d [EPUB]" % (index + 1),
                                             volume=float(index + 1))
                clear_env.manager.submit_release(item)
                clear_env.fake.statuses[123] = {
                    "state": "failed", "raw_state": "error", "files": [], "size": 0,
                }
                clear_env.manager.poll_once(async_fetch=False)
            active = clear_env.add_release("Overland Vol 3 [EPUB]", volume=3.0)
            clear_env.fake.torrent_id = 333
            clear_env.manager.submit_release(active)
            result = clear_env.manager.clear_failed()
            check_equal(result.get("status"), "success", "clear-failed succeeds")
            check_equal(result.get("failed_entries"), 2, "clear-failed finds both FAILED rows")
            check_equal(result.get("removed"), 2, "clear-failed removes both rows")
            check_equal(result.get("remote_deleted"), 0, "clear-failed performs no remote deletes")
            for item in clear_env.db.get_downloader_releases("torbox", limit=50):
                check_equal(item["download_status"], "DOWNLOADING",
                            "clear-failed leaves the active row untouched")
        finally:
            clear_env.cleanup()
    finally:
        env.cleanup()


# ===========================================================================
# 8. selective planner
# ===========================================================================
def test_plan_selection():
    env = ManagerEnv()
    try:
        release = {"id": 1, "title": "Overland Bundle [EPUB]", "series_name": "Overland"}

        empty = env.manager._plan_selection(release, [], [])
        check_equal(empty.get("action"), "zip", "unknown wanted volumes fall back to zip")
        check(bool(empty.get("reason")), "zip fallback explains itself")

        no_strict_env = ManagerEnv(settings={"torbox_strict_volume_selection": "false"})
        try:
            relaxed = no_strict_env.manager._plan_selection(
                release, [{"id": 1, "name": "Overland Vol 1.epub", "size": 10}], [1.0],
            )
            check_equal(relaxed.get("action"), "zip", "strict disabled forces zip")
            check_equal(relaxed.get("strict"), False, "planner reports strict=False")
        finally:
            no_strict_env.cleanup()

        no_files = env.manager._plan_selection(release, [], [1.0])
        check_equal(no_files.get("action"), "zip", "no file list falls back to zip")

        unmappable = env.manager._plan_selection(
            release, [{"id": 1, "name": "bonus-content.epub", "size": 10}], [1.0],
        )
        check_equal(unmappable.get("action"), "zip", "unmappable file names fall back to zip")
        check_contains(unmappable.get("reason"), "cannot be mapped", "unmappable reason")

        absent = env.manager._plan_selection(
            release, [{"id": 1, "name": "Overland Vol 3.epub", "size": 10}], [1.0],
        )
        check_equal(absent.get("action"), "zip", "wanted volume absent from torrent falls back to zip")
        check_contains(absent.get("reason"), "absent from the torrent", "absent-volume reason")

        all_needed = env.manager._plan_selection(
            release,
            [{"id": 1, "name": "Overland Vol 1.epub", "size": 10},
             {"id": 2, "name": "Overland Vol 2.epub", "size": 10}],
            [1.0, 2.0],
        )
        check_equal(all_needed.get("action"), "zip", "small all-needed bundle uses the zip")
        check_equal(all_needed.get("selected_count"), 2, "all-needed selection counts files")

        subset = env.manager._plan_selection(
            release,
            [{"id": 1, "name": "Overland Vol 1.epub", "size": 10},
             {"id": 2, "name": "Overland Vol 2.epub", "size": 20},
             {"id": 3, "name": "Overland Vol 3.epub", "size": 30}],
            [2.0],
        )
        check_equal(subset.get("action"), "files", "strict subset downloads exact files")
        check_equal(subset.get("selected_count"), 1, "subset selects one file")
        check_equal(len(subset.get("files") or []), 1, "subset file list has one descriptor")
        check_equal(subset.get("files")[0]["name"], "Overland Vol 2.epub",
                    "subset selects the wanted volume")
        check_equal(subset.get("selected_volumes"), [2.0], "subset reports selected volumes")
        check_equal(subset.get("skipped_volumes"), [1.0, 3.0], "subset reports skipped volumes")

        ancillary = env.manager._plan_selection(
            release,
            [{"id": 1, "name": "cover.jpg", "size": 5},
             {"id": 2, "name": "Overland Vol 1.epub", "size": 10}],
            [1.0],
        )
        check_equal(ancillary.get("action"), "zip", "ancillary files never block the zip decision")

        all_needed_large = env.manager._plan_selection(
            release,
            [{"id": 1, "name": "Overland Vol 1.epub", "size": 10}],
            [1.0],
            total_size=60 * 1024 ** 3,
        )
        check_equal(all_needed_large.get("action"), "files",
                    "oversized all-needed bundle downloads exact files")

        cap_env = ManagerEnv(settings={"torbox_max_download_gb": "1"})
        try:
            over_cap = [
                {"id": 1, "name": "Overland Vol 1.epub", "size": 10},
                {"id": 2, "name": "Overland Vol 2.epub", "size": 2 * 1024 ** 3},
            ]
            check_raises(
                DownloaderFileError,
                lambda: cap_env.manager._plan_selection(release, over_cap, [2.0]),
                msg="planner raises before any link when a selected file exceeds the cap",
                contains="above the configured cap",
            )
            check_equal(cap_env.manager._max_download_bytes(), 1024 ** 3,
                        "max-download cap clamps to 1 GiB")
        finally:
            cap_env.cleanup()

        huge_bundle = [
            {"id": index, "name": "Overland Vol %d.epub" % (index + 1), "size": 10}
            for index in range(21)
        ]
        many_files = env.manager._plan_selection(
            release, huge_bundle, [float(index + 1) for index in range(21)],
            total_size=0,
        )
        check_equal(many_files.get("action"), "files",
                    "bundles above the zip file-count limit download exact files")
    finally:
        env.cleanup()


# ===========================================================================
# 9. downloads dir, status payload, connection test, lifecycle
# ===========================================================================
def test_downloads_dir_and_lifecycle():
    env = ManagerEnv()
    try:
        nested = os.path.join(env.tmpdir, "nested", "downloads-root")
        env.db.set_setting("downloads_dir", nested)
        resolved = env.manager.downloads_dir()
        check_equal(resolved, os.path.abspath(nested), "downloads_dir resolves to an absolute path")
        check(os.path.isdir(nested), "downloads_dir creates the directory on demand")
        check_equal(env.manager.downloads_dir(), resolved, "downloads_dir is stable across calls")

        empty = env.db.get_setting("downloads_dir", "")
        check_equal(empty, nested, "downloads_dir setting round-trips")

        env.db.set_setting("torbox_enabled", "true")
        env.fake.enabled = True
        env.manager.start()
        check(env.manager.running is True, "start() runs the poller thread")
        first_thread = env.manager._thread
        env.manager.start()
        check(env.manager._thread is first_thread, "start() is idempotent")
        env.manager.stop()
        check(env.manager.running is False, "stop() stops the poller thread")
        env.manager.stop()
        check(env.manager.running is False, "stop() is idempotent")

        check_equal(env.manager.engine(), "torbox", "engine() resolves the configured engine")
        env.manager.get_downloader()
        check("torbox" in env.fake.engines_requested, "get_downloader() uses the injected factory")
        check_equal(_safe_dirname("Over/land: Vol 5?"), "Over_land_ Vol 5_",
                    "safe dirname strips illegal characters")
        check_equal(len(_safe_dirname("x" * 200)), 120, "safe dirname caps at 120 characters")
        check_equal(_safe_dirname(""), "torbox-download", "safe dirname falls back for empty titles")
        check(_parse_db_timestamp("2026-01-02 03:04:05") is not None,
              "DB timestamps parse for the timeout check")
    finally:
        env.cleanup()


def test_build_status():
    env = ManagerEnv()
    try:
        release = env.add_release("Overland Vol 5 [EPUB]", volume=5.0)
        env.manager.submit_release(release)
        env.fake.statuses[123] = {
            "state": "downloading",
            "raw_state": "downloading",
            "progress": 0.4,
            "size": 1000,
            "download_speed": 250,
            "eta": 3,
            "seeds": 5,
            "files": [{"id": 1, "name": "Overland Vol 5.epub", "size": 1000}],
            "cached": False,
        }
        payload = env.manager.build_status()
        check_equal(payload.get("status"), "success", "status payload is success when connected")
        check_equal(payload.get("engine"), "torbox", "status payload engine")
        check_equal(payload.get("count"), 1, "status payload counts the row")
        check_equal(payload["downloader"].get("connected"), True, "status payload reports connected")
        check_equal(payload["downloader"].get("plan"), "Essential", "status payload carries the plan")
        check(bool(payload.get("generated_at")), "status payload has generated_at")
        check_equal(payload.get("totals", {}).get("download_speed"), 250,
                    "totals sum the active row download speed")
        row = payload["torrents"][0]
        check_equal(row.get("release_id"), release["id"], "row carries the release id")
        check_equal(row.get("state"), "downloading", "row derives state from live status")
        check_equal(row.get("progress"), 0.4, "row progress normalises to 0..1")
        check_equal(row.get("progress_pct"), 40.0, "row percent")
        check_equal(row.get("download_speed"), 250, "row download speed")
        check_equal(row.get("is_active"), True, "downloading row is active")
        check_equal(row.get("selection"), "missing volumes 5", "row selection text for wanted volumes")
        check_equal(row.get("engine"), "torbox", "row engine")
        check_equal(row.get("failed"), False, "row not failed")
        check_all_numbers = row.get("total_bytes"), row.get("downloaded_bytes"), row.get(
            "size", row.get("size_bytes"))
        check(all(isinstance(value, (int, float)) and value is not None
                  for value in check_all_numbers), "row numeric fields are never None")

        # FAILED wins over a live ready state.
        env.db.set_download_error(release["id"], "boom")
        env.db.update_downloader_state(release["id"], "FAILED", "torbox")
        env.fake.statuses[123] = {
            "state": "ready", "raw_state": "completed", "progress": 1.0,
            "size": 1000, "files": [], "cached": False,
        }
        failed_payload = env.manager.build_status()
        failed_row = failed_payload["torrents"][0]
        check_equal(failed_row.get("state"), "failed", "FAILED row renders failed even if TorBox is ready")
        check_equal(failed_row.get("failed"), True, "FAILED row reports failed=True")
        check_equal(failed_row.get("error"), "boom", "FAILED row exposes the stored error")
        check_equal(failed_row.get("can_retry"), True, "FAILED row can be retried")

        # Fetching local phase wins over the live state and uses local counters.
        env.db.update_download_handle(release["id"], "torbox", downloader_id="123")
        env.db.update_torbox_progress(
            release["id"], phase="fetching", downloaded_bytes=500,
            total_bytes=1000, speed_bps=100,
        )
        fetching_payload = env.manager.build_status()
        fetching_row = fetching_payload["torrents"][0]
        check_equal(fetching_row.get("state"), "fetching", "local fetching phase overrides live state")
        check_equal(fetching_row.get("progress"), 0.5, "fetching progress uses local byte counters")
        check_equal(fetching_row.get("eta_seconds"), 5, "fetching ETA = remaining bytes / speed")
        check_equal(fetching_row.get("phase"), "fetching", "row phase is the local phase")
        check_equal(fetching_row.get("is_active"), True, "fetching row is active")

        # last_poll_at is exposed after a poll pass stamps the setting.
        env.fake.statuses[123] = {"state": "downloading", "raw_state": "downloading",
                                  "files": [], "size": 0}
        env.manager.poll_once(async_fetch=False)
        stamped = env.manager.build_status()
        check(isinstance(stamped.get("last_poll_at"), float), "status payload exposes last_poll_at")
        check(bool(stamped.get("last_checked_at")), "status payload exposes last_checked_at")

        # Degraded, never raising, when the live engine call fails.
        env.fake.status_error = DownloaderError("TorBox HTTP 500 calling /torrents/mylist.")
        degraded = env.manager.build_status()
        check_equal(degraded.get("status"), "degraded", "failed live status degrades the payload")
        check_equal(degraded.get("live_state_available"), False,
                    "failed live status reports live_state_available=False")
        env.fake.status_error = None

        # Disabled engine.
        env.fake.enabled = False
        disabled = env.manager.build_status()
        check_equal(disabled.get("status"), "disabled", "disabled engine reports status=disabled")
        check_equal(disabled["downloader"].get("enabled"), False, "disabled engine flag")
        check_equal(disabled.get("count"), 1, "disabled engine still returns DB rows")
        env.fake.enabled = True

        # Connection failure degrades instead of raising.
        env.fake.connection_result = {"success": False, "message": "TorBox rejected the API key."}
        connection_failed = env.manager.build_status()
        check_equal(connection_failed.get("status"), "degraded", "connection failure degrades")
        check_equal(connection_failed["downloader"].get("connected"), False,
                    "connection failure reports connected=False")

        # Selection text variants.
        other = env.add_release("Overland Vol 6 [EPUB]", volume=6.0)
        env.db.update_download_handle(other["id"], "torbox", downloader_id="66")
        env.db.set_setting("torbox_strict_volume_selection", "true")
        strict_payload = env.manager.build_status()
        strict_rows = {row["release_id"]: row for row in strict_payload["torrents"]}
        check_equal(strict_rows[other["id"]].get("selection"), "all volumes",
                    "strict row without wanted volumes renders 'all volumes'")
        env.db.set_setting("torbox_strict_volume_selection", "false")
        legacy_payload = env.manager.build_status()
        legacy_rows = {row["release_id"]: row for row in legacy_payload["torrents"]}
        check_equal(legacy_rows[other["id"]].get("selection"), "legacy whole-torrent",
                    "non-strict row without wanted volumes renders 'legacy whole-torrent'")
        env.db.set_setting("torbox_strict_volume_selection", "true")

        # Connection test helper (uses the injected fake, no network).
        env.fake.connection_result = {
            "success": True,
            "engine": "torbox",
            "plan": "Essential",
            "active_slots": 1,
            "message": "Connected to TorBox (Essential); 1 active slot(s).",
        }
        test_ok = env.manager.test_connection()
        check_equal(test_ok.get("success"), True, "test_connection returns success")
        check_equal(test_ok.get("active_slots"), 1, "test_connection returns active slots")
        env.fake.connection_result = {"success": False, "message": "TorBox rejected the API key."}
        test_failed = env.manager.test_connection()
        check_equal(test_failed.get("success"), False, "test_connection reports auth failure")
        env.fake.connection_error = RuntimeError("socket exploded")
        test_crashed = env.manager.test_connection()
        check_equal(test_crashed.get("success"), False, "test_connection contains unexpected errors")
        env.fake.connection_error = None
        check_equal(DEFAULT_DOWNLOADS_DIR, os.getenv("DOWNLOAD_DIR", "/downloads"),
                    "DEFAULT_DOWNLOADS_DIR honours the DOWNLOAD_DIR env default")
    finally:
        env.cleanup()


# ===========================================================================
# main
# ===========================================================================
def main():
    tests = [
        test_database_migrations_and_methods,
        test_download_state_module,
        test_submit_happy_path,
        test_torrent_only_guard,
        test_concurrency_gate,
        test_poll_ready_to_fetch_completion,
        test_poll_failure_branches,
        test_fetch_failure_containment,
        test_timeout_failure,
        test_queued_slot_and_promotion,
        test_retry_release,
        test_remove_and_clear_failed,
        test_plan_selection,
        test_downloads_dir_and_lifecycle,
        test_build_status,
    ]
    global passed_count, failed_count
    failed_tests = []
    for func in tests:
        print("\n--- %s ---" % func.__name__)
        try:
            func()
        except AssertionError as exc:
            failed_tests.append(func.__name__)
            failed_count += 1
            print("  FAIL %s: %s" % (func.__name__, exc))
        except Exception:
            failed_tests.append(func.__name__)
            failed_count += 1
            print("  FAIL %s: unexpected error" % func.__name__)
            traceback.print_exc()

    print("\n" + "=" * 70)
    print("torbox_manager checks: %d passed, %d failed, %d test group(s) failed"
          % (passed_count, failed_count, len(failed_tests)))
    if failed_tests:
        print("FAILED: %s" % ", ".join(failed_tests))
        raise SystemExit(1)
    print("ALL TORBOX_MANAGER TESTS PASSED")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
