#!/usr/bin/env python3
"""End-to-end integration tests for the TorBox download engine (NovelTrax).

This suite is the cross-module acceptance test for the port contract in
``.agents/torbox_port/CONTRACT.md``.  Unlike the owner suites, it wires the REAL
stack together:

    Database (settings + release rows)
        -> TorBoxManager (submit / poll / fetch / retry / remove / clear-failed)
            -> TorboxDownloader (real API client)
                -> fake HTTP transport (FakeApi) + fake CDN stream

No network and no second third-party dependency: the transport is patched at the
module-local symbols (``app.downloader.torbox.urllib``, ``_open_no_redirect``,
``security``) exactly as the engine suite does.

It also statically guards ``backend/app/main.py`` route wiring (FastAPI is not
installed in this interpreter, so it cannot be imported here).

Bare ``assert`` is never used: every expectation goes through :func:`check`,
which raises ``AssertionError`` (Calibre's -O2 interpreter strips bare asserts).

Usage::

    python tests/test_torbox_integration.py
    python tests/run_tests.py torbox_integration
"""

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
for _path in (BACKEND_DIR, PROJECT_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.database import Database
from app.downloader import torbox as torbox_mod
from app.downloader.base import QUEUED_ID_PREFIX
from app.torbox_manager import TorBoxManager, _cbz_only_manga

INFOHASH = "0123456789abcdef0123456789abcdef01234567"
MAGNET = "magnet:?xt=urn:btih:" + INFOHASH + "&dn=Integration+Novel+Vol+1"
SECRET_KEY = "tb-integration-secret-key-do-not-log"
JNOVELS_URL = "https://jnovels.com/some-novel-epub/"

_CHECKS_RUN = [0]
_TEMP_DIRS = []


# ---------------------------------------------------------------------------
# Explicit checks (never bare ``assert``)
# ---------------------------------------------------------------------------
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
    """Hermetic temp dir: mkdtemp() ACLs are unusable under this session's sandbox."""
    path = os.path.join(tempfile.gettempdir(), "noveltrax-torbox-int-%s-%s" % (label, os.urandom(6).hex()))
    os.makedirs(path, exist_ok=True)
    _TEMP_DIRS.append(path)
    return path


def cleanup_temp_dirs():
    while _TEMP_DIRS:
        shutil.rmtree(_TEMP_DIRS.pop(), ignore_errors=True)


# ---------------------------------------------------------------------------
# Fake HTTP transport
# ---------------------------------------------------------------------------
class FakeHeaders:
    def __init__(self, mapping=None):
        self._data = {str(key).lower(): str(value) for key, value in (mapping or {}).items()}

    def get(self, key, default=None):
        return self._data.get(str(key).lower(), default)

    def items(self):
        return list(self._data.items())


class FakeStream:
    def __init__(self, body=b"", status=200, headers=None):
        self.body = body.encode("utf-8") if isinstance(body, str) else bytes(body or b"")
        self.status = status
        self.headers = FakeHeaders(headers or {})
        self._position = 0
        self.closed = False

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self.body[self._position:]
            self._position = len(self.body)
        else:
            chunk = self.body[self._position:self._position + int(size)]
            self._position += len(chunk)
        return bytes(chunk)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class UnroutedRequest(RuntimeError):
    pass


class RecordedRequest:
    def __init__(self, via, request, timeout):
        self.via = via
        self.url = request.full_url
        self.method = request.get_method()
        self.body = request.data or b""
        parsed = urllib.parse.urlsplit(self.url)
        self.path = parsed.path
        self.params = {
            key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()
        }


class FakeApi:
    """URL-substring router for API JSON calls and CDN streams."""

    def __init__(self):
        self._routes = []
        self.calls = []
        self._lock = threading.Lock()

    def on(self, needle, result):
        self._routes.append((needle, result))
        return self

    def _dispatch(self, call):
        for needle, result in self._routes:
            if needle in call.url:
                if callable(result):
                    result = result(call)
                if isinstance(result, FakeStream):
                    return result
                if isinstance(result, (bytes, bytearray)):
                    return FakeStream(bytes(result))
                if isinstance(result, str):
                    return FakeStream(result.encode("utf-8"))
                return FakeStream(json.dumps(result).encode("utf-8"))
        raise UnroutedRequest("no FakeApi route for %s %s %s" % (call.via, call.method, call.url))

    def _record(self, via, request, timeout):
        call = RecordedRequest(via, request, timeout)
        with self._lock:
            self.calls.append(call)
        return call

    def urlopen(self, request, timeout=None):
        return self._dispatch(self._record("api", request, timeout))

    def open_stream(self, request, timeout=None):
        return self._dispatch(self._record("stream", request, timeout))

    def calls_for(self, needle, method=None):
        with self._lock:
            return [
                call for call in self.calls
                if needle in call.url and (method is None or call.method == method)
            ]


class FakeSecurity:
    """Allows every URL (the real SSRF shim is unit-tested by the engine suite)."""

    def __init__(self):
        self.checked = []

    def is_safe_public_url(self, url):
        self.checked.append(url)
        return True


def json_ok(data):
    return {"success": True, "data": data}


def json_error(status, detail):
    return FakeStream(
        json.dumps({"success": False, "detail": detail}),
        status=status,
        headers={"Content-Type": "application/json"},
    )


class TorBoxTransport:
    """Installs/restores the fake transport around the real TorboxDownloader."""

    def __init__(self):
        self.api = FakeApi()
        self.security = FakeSecurity()
        self._saved = {}

    def __enter__(self):
        self._saved = {
            "urllib": torbox_mod.urllib,
            "security": torbox_mod.security,
            "open_no_redirect": torbox_mod._open_no_redirect,
        }
        torbox_mod.urllib = type(
            "FakeUrllib",
            (),
            {
                "request": type("R", (), {"Request": urllib.request.Request, "urlopen": self.api.urlopen}),
                "parse": urllib.parse,
                "error": urllib.error,
            },
        )()
        torbox_mod.security = self.security
        torbox_mod._open_no_redirect = self.api.open_stream
        return self

    def __exit__(self, exc_type, exc, tb):
        for key, value in self._saved.items():
            setattr(torbox_mod, key, value)
        return False


def zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class Ctx:
    """Temp DB + downloads dir + manager wired to the real engine + fake transport."""

    def __init__(self, label="case"):
        self.root = make_temp_dir(label)
        self.db = Database(os.path.join(self.root, "noveltrax.db"))
        self.downloads = os.path.join(self.root, "downloads")
        os.makedirs(self.downloads, exist_ok=True)
        for key, value in (
            ("downloader_engine", "torbox"),
            ("torbox_enabled", "true"),
            ("torbox_api_key", SECRET_KEY),
            ("downloads_dir", self.downloads),
            ("torbox_delete_after_download", "true"),
            ("torbox_delete_on_failure", "true"),
            ("torbox_strict_volume_selection", "true"),
            ("torbox_max_concurrent", "5"),
            ("torbox_timeout_hours", "6"),
        ):
            self.db.set_setting(key, value)
        self.manager = TorBoxManager(self.db)
        self.transport = TorBoxTransport()
        self._counter = 0

    def insert_release(self, title="Integration Novel Vol 1", download_url=MAGNET, **overrides):
        self._counter += 1
        values = {
            "guid": "https://nyaa.si/view/%d" % (9000 + self._counter),
            "title": title,
            "series_name": "Integration Novel",
            "volume": 1.0,
            "link": "https://nyaa.si/view/%d" % (9000 + self._counter),
            "download_url": download_url,
            "status": "REQUESTED",
            "is_bundle": 0,
            "volumes_json": "[]",
            "missing_vols_json": "[]",
            "media_type": "light_novel",
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
            return int(cur.lastrowid)

    def row(self, release_id):
        return self.db.get_release_by_id(release_id)

    def downloads_rows(self):
        return self.db.get_downloader_releases("torbox", limit=50)

    def activity_types(self):
        return [str(entry.get("event_type") or "") for entry in self.db.get_logs(limit=300)]


def ready_info(files, size=None, raw="completed"):
    total = size if size is not None else sum(int(item.get("size") or 0) for item in files)
    return {
        "id": 777,
        "torrent_id": 777,
        "hash": INFOHASH,
        "download_state": raw,
        "download_present": True,
        "download_finished": True,
        "progress": 100,
        "size": total,
        "download_speed": 0,
        "eta": 0,
        "seeds": 4,
        "files": files,
    }


def wire_happy_path_transport(transport, files, file_bodies, zip_bytes_value=None, live_info=None):
    """Configure API routes for submit -> ready -> whole-torrent zip fetch."""
    api = transport.api
    api.on("/torrents/checkcached", json_ok({}))
    api.on(
        "/torrents/createtorrent",
        json_ok({"torrent_id": 777, "hash": INFOHASH}),
    )
    info_holder = {
        "info": live_info if live_info is not None
        else ready_info(files, size=len(zip_bytes_value or b""))
    }
    api.on("/torrents/mylist", lambda call: json_ok(info_holder["info"]))
    api.on("/torrents/requestdl", json_ok("https://cdn.torbox.example/archive.zip"))
    api.on("/torrents/controltorrent", json_ok(None))
    api.on(
        "cdn.torbox.example/archive.zip",
        FakeStream(zip_bytes_value, headers={"Content-Length": str(len(zip_bytes_value or b""))}),
    )
    return info_holder


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_happy_path_submit_poll_fetch_complete():
    """submit -> queued status -> ready -> whole-torrent zip fetch -> COMPLETED."""
    ctx = Ctx("happy")
    archive = zip_bytes({"Integration Novel v01.epub": b"epub-bytes-1"})
    files = [{"id": 1, "name": "Integration Novel v01.epub", "size": 12}]

    with ctx.transport as transport:
        # First poll must not fetch: TorBox reports the torrent still downloading.
        live = wire_happy_path_transport(
            transport, files, {}, archive,
            live_info=dict(
                ready_info(files, size=len(archive)),
                download_state="downloading",
                download_present=False,
                download_finished=False,
                progress=42,
            ),
        )
        release_id = ctx.insert_release()
        result = ctx.manager.submit_release(ctx.row(release_id), source="manual")
        check(result.get("success") is True, "submit succeeds: %r" % (result,))
        check_eq(result.get("engine"), "torbox", "submit reports the torbox engine")
        check_eq(result.get("cached"), False, "uncached submission")
        check("createtorrent" in transport.api.calls_for("/torrents/createtorrent")[0].url,
              "createtorrent was posted")

        row = ctx.row(release_id)
        check_eq(row.get("download_status"), "DOWNLOADING", "row is DOWNLOADING after submit")
        check_eq(row.get("downloader"), "torbox", "row records the torbox engine")
        check_eq(str(row.get("downloader_id")), "777", "row persists the torrent id handle")

        first = ctx.manager.poll_once(async_fetch=False)
        check_eq(first.get("status"), "ok", "first poll is ok")
        check_eq(first.get("completed"), 0, "downloading torrent is not fetched yet")
        check_eq(ctx.row(release_id).get("download_status"), "DOWNLOADING",
                 "still downloading after first poll")

        # TorBox finishes: now a poll must fetch the whole-torrent zip.
        live["info"] = ready_info(files, size=len(archive))
        second = ctx.manager.poll_once(async_fetch=False)
        check_eq(second.get("completed"), 1, "ready torrent is completed in the poll pass")
        check_eq(second.get("failed"), 0, "no failures on the happy path")

        row = ctx.row(release_id)
        check_eq(row.get("download_status"), "COMPLETED", "row is COMPLETED")
        check(row.get("downloaded_at"), "downloaded_at is stamped")
        saved = json.loads(row.get("download_files_json") or "[]")
        check_eq(len(saved), 1, "one file path persisted")
        check(os.path.isfile(saved[0]), "fetched file exists on disk: %r" % (saved[0],))
        check_in("Integration Novel v01.epub", saved[0], "saved file keeps its name")
        with open(saved[0], "rb") as handle:
            check_eq(handle.read(), b"epub-bytes-1", "saved file content matches the zip entry")
        progress = ctx.db.get_torbox_progress(release_id)
        check_eq(progress.get("phase"), "done", "progress phase is done")
        check(progress.get("ready_detected_at"), "ready_detected_at was persisted")
        check(progress.get("fetch_started_at"), "fetch_started_at was persisted")

        deletes = transport.api.calls_for("/torrents/controltorrent")
        check(len(deletes) >= 1, "delete-after-download called controltorrent")
        check_in('"operation": "delete"', deletes[0].body.decode("utf-8", "replace"),
                 "controltorrent asks for a delete")
        events = ctx.activity_types()
        check_in("TORBOX_SUBMITTED", events, "submission audited")
        check_in("TORBOX_DOWNLOADED", events, "completion audited")
        # Secret hygiene across the whole run: the API key never appears in logs.
        logs = json.dumps(ctx.db.get_logs(limit=300))
        check_not_in(SECRET_KEY, logs, "API key never logged")


def test_failure_marks_failed_and_deletes_remote():
    ctx = Ctx("failure")
    with ctx.transport as transport:
        transport.api.on("/torrents/checkcached", json_ok({}))
        transport.api.on("/torrents/createtorrent", json_ok({"torrent_id": 778, "hash": INFOHASH}))
        transport.api.on(
            "/torrents/mylist",
            json_ok(
                {
                    "id": 778,
                    "hash": INFOHASH,
                    "download_state": "error",
                    "download_present": False,
                    "download_finished": False,
                    "progress": 0,
                    "size": 10,
                    "files": [],
                }
            ),
        )
        transport.api.on("/torrents/controltorrent", json_ok(None))

        release_id = ctx.insert_release(title="Broken Novel Vol 1")
        ctx.manager.submit_release(ctx.row(release_id), source="manual")
        outcome = ctx.manager.poll_once(async_fetch=False)

        check_eq(outcome.get("failed"), 1, "failed torrent counted")
        row = ctx.row(release_id)
        check_eq(row.get("download_status"), "FAILED", "row is FAILED")
        check_eq(int(row.get("download_attempts") or 0), 1, "attempt counter incremented")
        check_in("attempt 1", str(row.get("download_error") or ""), "error names the attempt")
        check_in("error", str(row.get("download_error") or "").lower(),
                 "error records the TorBox state")
        check(len(transport.api.calls_for("/torrents/controltorrent")) >= 1,
              "delete-on-failure removes the remote torrent")
        check_in("TORBOX_FAILED", ctx.activity_types(), "failure audited")


def test_torrent_only_guard_rejects_jnovels_links():
    ctx = Ctx("guard")
    release_id = ctx.insert_release(
        title="J-Novel Web Release",
        download_url=JNOVELS_URL,
        guid=JNOVELS_URL,
        link=JNOVELS_URL,
    )
    result = ctx.manager.submit_release(ctx.row(release_id), source="manual")
    check_eq(result.get("success"), False, "non-torrent release is not submitted")
    check_eq(result.get("skipped"), True, "non-torrent release is reported as skipped")
    check_in("torrent", str(result.get("message") or "").lower(),
             "message explains TorBox handles torrents only")
    check_eq(ctx.row(release_id).get("download_status"), "IDLE", "rejected release stays IDLE")
    check(not ctx.row(release_id).get("downloader_id"), "no engine handle was persisted")


def test_concurrency_gate_blocks_new_submit():
    ctx = Ctx("concurrency")
    ctx.db.set_setting("torbox_max_concurrent", "1")
    with ctx.transport as transport:
        transport.api.on("/torrents/checkcached", json_ok({}))
        transport.api.on("/torrents/createtorrent", json_ok({"torrent_id": 779, "hash": INFOHASH}))
        first_id = ctx.insert_release(title="Active Novel Vol 1")
        ctx.manager.submit_release(ctx.row(first_id), source="manual")
        second_id = ctx.insert_release(title="Blocked Novel Vol 2", guid="https://nyaa.si/view/9999")
        blocked = ctx.manager.submit_release(ctx.row(second_id), source="manual")
        check_eq(blocked.get("success"), False, "second submit is refused at the cap")
        check_eq(blocked.get("retryable"), True, "refusal is marked retryable")
        check_in("concurrency", str(blocked.get("message") or "").lower(),
                 "message names the concurrency limit")
        check_eq(ctx.row(second_id).get("download_status"), "IDLE",
                 "blocked release was not queued")


def test_selective_volume_fetch_only_downloads_missing_files():
    """Strict selection: a bundle whose only missing volume is 3 fetches just v03."""
    ctx = Ctx("selective")
    files = [
        {"id": 11, "name": "Integration Novel v01.epub", "size": 11},
        {"id": 12, "name": "Integration Novel v02.epub", "size": 12},
        {"id": 13, "name": "Integration Novel v03.epub", "size": 13},
    ]
    with ctx.transport as transport:
        api = transport.api
        api.on("/torrents/checkcached", json_ok({}))
        api.on("/torrents/createtorrent", json_ok({"torrent_id": 880, "hash": INFOHASH}))
        api.on("/torrents/mylist", json_ok(dict(ready_info(files), id=880, torrent_id=880)))

        def requestdl(call):
            file_id = call.params.get("file_id")
            if not file_id:
                return json_ok("https://cdn.torbox.example/zip")
            return json_ok("https://cdn.torbox.example/f%s.epub" % file_id)

        api.on("/torrents/requestdl", requestdl)
        for file_id, body in ((11, b"vol-1"), (12, b"vol-2"), (13, b"vol-3")):
            api.on(
                "cdn.torbox.example/f%d.epub" % file_id,
                FakeStream(body, headers={"Content-Length": str(len(body))}),
            )
        api.on("/torrents/controltorrent", json_ok(None))

        release_id = ctx.insert_release(
            title="Integration Novel v01-03 (Bundle)",
            is_bundle=1,
            volumes_json="[1, 2, 3]",
            missing_vols_json="[3]",
            volume=None,
        )
        ctx.manager.submit_release(ctx.row(release_id), source="manual")
        outcome = ctx.manager.poll_once(async_fetch=False)

        check_eq(outcome.get("completed"), 1, "selective fetch completed")
        saved = json.loads(ctx.row(release_id).get("download_files_json") or "[]")
        check_eq(len(saved), 1, "only the missing volume was fetched")
        check_in("v03", saved[0], "the missing volume (v03) is the fetched file")
        for other in ("v01", "v02"):
            check_eq(
                [call for call in api.calls_for("cdn.torbox.example/f") if other in call.url],
                [],
                "owned volume %s was not downloaded" % other,
            )
        with open(saved[0], "rb") as handle:
            check_eq(handle.read(), b"vol-3", "selective file content is correct")


def test_retry_remove_and_clear_failed_lifecycle():
    ctx = Ctx("lifecycle")
    with ctx.transport as transport:
        api = transport.api
        api.on("/torrents/checkcached", json_ok({}))
        api.on("/torrents/createtorrent", json_ok({"torrent_id": 991, "hash": INFOHASH}))
        api.on("/torrents/mylist", json_ok(ready_info([{"id": 1, "name": "x.epub", "size": 1}])))
        api.on("/torrents/requestdl", json_ok("https://cdn.torbox.example/x.epub"))
        api.on("cdn.torbox.example/x.epub", FakeStream(b"z", headers={"Content-Length": "1"}))
        api.on("/torrents/controltorrent", json_ok(None))

        release_id = ctx.insert_release(title="Lifecycle Novel Vol 1")
        ctx.manager.submit_release(ctx.row(release_id), source="manual")
        # Force a failure, then retry (must cancel the stale remote job first).
        ctx.db.update_downloader_state(release_id, "FAILED", "torbox")
        ctx.db.set_download_error(release_id, "forced failure (attempt 1, 2026-01-01 00:00:00 UTC)")
        before = len(api.calls_for("/torrents/controltorrent"))

        retried = ctx.manager.retry_release(release_id)
        check_eq(retried.get("status"), "success", "retry resubmits successfully: %r" % (retried,))
        row = ctx.row(release_id)
        check_eq(row.get("download_status"), "DOWNLOADING", "retry returns the row to DOWNLOADING")
        check_eq(str(row.get("downloader_id")), "991", "retry persists the new handle")
        check(len(api.calls_for("/torrents/controltorrent")) > before,
              "retry cancels the stale remote torrent first")

        # The per-release 5 s rate limit rejects an immediate second retry.
        ctx.db.update_downloader_state(release_id, "FAILED", "torbox")
        limited = ctx.manager.retry_release(release_id)
        check_eq(limited.get("status"), "error", "immediate second retry is rejected")
        check_eq(limited.get("code"), 429, "rate limit maps to code 429")

        # Remove a FAILED row: local-only (never waits on the network).
        before = len(api.calls_for("/torrents/controltorrent"))
        removed = ctx.manager.remove_release(release_id)
        check_eq(removed.get("status"), "success", "remove succeeds")
        check_eq(ctx.row(release_id).get("download_status"), "IDLE", "remove resets the row to IDLE")
        check_eq(len(api.calls_for("/torrents/controltorrent")), before,
                 "remove of a FAILED row makes no remote call")

        # Clear failed: two failed rows are reset locally.
        failed_ids = []
        for index in (1, 2):
            rid = ctx.insert_release(
                title="Cleared Novel Vol %d" % index,
                guid="https://nyaa.si/view/8%03d" % index,
            )
            ctx.db.update_download_handle(rid, "torbox", downloader_id=str(500 + index))
            ctx.db.update_downloader_state(rid, "FAILED", "torbox")
            failed_ids.append(rid)
        cleared = ctx.manager.clear_failed()
        check_eq(cleared.get("status"), "success", "clear-failed succeeds")
        check_eq(cleared.get("removed"), 2, "clear-failed removes both failed rows")
        for rid in failed_ids:
            check_eq(ctx.row(rid).get("download_status"), "IDLE",
                     "cleared release %s is IDLE" % rid)


def test_build_status_derivation_rules():
    ctx = Ctx("status")
    with ctx.transport as transport:
        api = transport.api
        api.on("/torrents/checkcached", json_ok({}))
        api.on("/torrents/createtorrent", json_ok({"torrent_id": 660, "hash": INFOHASH}))
        # TorBox still says the torrent is ready even though we marked the row FAILED.
        api.on("/torrents/mylist", json_ok(ready_info([{"id": 1, "name": "a.epub", "size": 3}])))
        api.on("/torrents/controltorrent", json_ok(None))
        api.on("/user/me", json_ok({"plan": 2, "active_slot_count": 1}))

        release_id = ctx.insert_release(title="Status Novel Vol 1")
        ctx.manager.submit_release(ctx.row(release_id), source="manual")
        ctx.db.update_downloader_state(release_id, "FAILED", "torbox")
        ctx.db.set_download_error(release_id, "deliberate failure")
        ctx.db.update_torbox_progress(release_id, phase="failed")

        payload = ctx.manager.build_status(1.0)
        check_eq(payload.get("engine"), "torbox", "status payload names the engine")
        check_in("downloader", payload, "status payload has a downloader block")
        check_eq(payload.get("count"), 1, "one active/failed row is reported")
        row = payload["torrents"][0]
        check_eq(row.get("release_id"), release_id, "status row identifies the release")
        check_eq(row.get("state"), "failed", "DB FAILED beats TorBox 'ready'")
        check_eq(row.get("failed"), True, "status row flags failure")
        check_in("deliberate failure", str(row.get("error") or ""), "status row exposes the error")
        check("is_active" in row, "status row has is_active")
        check_eq(row.get("engine"), "torbox", "status row engine field")
        check(isinstance(row.get("progress"), (int, float)), "progress is a normalised number")
        check("speed_bps" in row and "downloaded_bytes" in row and "total_bytes" in row,
              "status row exposes the progress counters")

        # Local fetch phases override the live engine state while fetching.
        ctx.db.update_downloader_state(release_id, "DOWNLOADING", "torbox")
        ctx.db.update_torbox_progress(
            release_id, phase="fetching", downloaded_bytes=5, total_bytes=10,
            speed_bps=1000, progress_pct=50,
        )
        payload = ctx.manager.build_status(1.0)
        row = payload["torrents"][0]
        check_eq(row.get("phase"), "fetching", "local fetching phase is reported")
        check_eq(row.get("downloaded_bytes"), 5, "local byte counter wins while fetching")
        check_eq(row.get("progress_pct"), 50, "local progress percent is reported")


def test_cbz_only_helper_matrix():
    """The .cbz-only detector ignores clutter but flags manga packs."""
    cases = [
        ([{"id": 1, "name": "Series v01.cbz", "size": 10}], True, 1, 1),
        (
            [
                {"id": 1, "name": "Series v01.cbz", "size": 10},
                {"id": 2, "name": "covers/cover.jpg", "size": 5},
                {"id": 3, "name": "info.nfo", "size": 1},
            ],
            True, 1, 1,
        ),
        (
            [
                {"id": 1, "name": "Series v01.cbz", "size": 10},
                {"id": 2, "name": "Series v02.epub", "size": 20},
            ],
            False, 1, 2,
        ),
        ([{"id": 1, "name": "Series v01.EPUB", "size": 10}], False, 0, 1),
        ([{"id": 1, "name": "notes.txt", "size": 1}], False, 0, 0),
        ([], False, 0, 0),
        (None, False, 0, 0),
        (["Book Vol 1.CBZ"], True, 1, 1),
    ]
    for files, expected_manga, expected_cbz, expected_content in cases:
        result = _cbz_only_manga(files)
        check_eq(result["manga_only"], expected_manga, "manga_only for %r" % (files,))
        check_eq(result["cbz_count"], expected_cbz, "cbz_count for %r" % (files,))
        check_eq(result["content_count"], expected_content, "content_count for %r" % (files,))


def test_cbz_only_torrent_rejected_as_manga():
    """A torrent whose content files are all .cbz is rejected as manga, never fetched."""
    ctx = Ctx("cbz-only")
    files = [
        {"id": 21, "name": "Manga Rip v01.cbz", "size": 11},
        {"id": 22, "name": "Manga Rip v02.cbz", "size": 12},
        {"id": 23, "name": "cover.jpg", "size": 5},
    ]
    with ctx.transport as transport:
        api = transport.api
        api.on("/torrents/checkcached", json_ok({}))
        api.on("/torrents/createtorrent", json_ok({"torrent_id": 555, "hash": INFOHASH}))
        api.on(
            "/torrents/mylist",
            json_ok(
                dict(
                    ready_info(files),
                    id=555,
                    torrent_id=555,
                    download_state="downloading",
                    download_present=False,
                    download_finished=False,
                    progress=10,
                )
            ),
        )
        api.on("/torrents/controltorrent", json_ok(None))
        # No /torrents/requestdl route: any fetch attempt would raise UnroutedRequest.

        release_id = ctx.insert_release(title="Manga Rip v01-02", series_name="Manga Rip")
        submitted = ctx.manager.submit_release(ctx.row(release_id), source="manual")
        check_eq(submitted.get("success"), True, "torrent is submitted before the file list exists")

        outcome = ctx.manager.poll_once(async_fetch=False)
        check_eq(outcome.get("rejected_manga"), 1, "poll reports one manga rejection")
        check_eq(outcome.get("completed"), 0, "manga torrent is not completed/fetched")
        check_eq(outcome.get("failed"), 0, "manga rejection is not a download failure")

        row = ctx.row(release_id)
        check_eq(row.get("media_type"), "manga", "release is re-classified as manga")
        check_eq(row.get("download_status"), "IDLE", "submission is detached (IDLE)")
        check(not row.get("downloader_id"), "engine handle was cleared")
        check_eq(row.get("status"), "IGNORED", "release is ignored/hidden from now on")
        check_eq(int(row.get("ignored") or 0), 1, "ignored flag is set")
        check(release_id not in [r.get("id") for r in ctx.db.get_releases(limit=200)],
              "hidden release is excluded from the normal release list")
        check(release_id not in [r.get("id") for r in ctx.db.get_pending_requests()],
              "hidden release is excluded from wanted requests")
        check(not ctx.db.get_torbox_progress(release_id).get("phase") == "fetching",
              "no fetch phase was started")
        check(len(api.calls_for("/torrents/requestdl")) == 0,
              "no download link was requested for a manga torrent")
        check(len(api.calls_for("/torrents/controltorrent")) >= 1,
              "the remote manga torrent is deleted to free the slot")
        check_in("TORBOX_MANGA_REJECTED", ctx.activity_types(),
                 "manga rejection is audited")
        check_in("cbz", json.dumps(ctx.db.get_logs(limit=50)).lower(),
                 "audit detail names the .cbz detection")

        # A later feed re-poll must not resurrect the ignored release: the status
        # precedence rules keep IGNORED unless the user explicitly un-ignores it.
        ctx.db.upsert_release(
            {
                "guid": row.get("guid"),
                "title": row.get("title"),
                "series_name": row.get("series_name"),
                "status": "REQUESTED",
                "media_type": "light_novel",
            }
        )
        refreshed = ctx.row(release_id)
        check_eq(refreshed.get("status"), "IGNORED",
                 "feed re-ingest cannot lift the IGNORED status")
        check_eq(int(refreshed.get("ignored") or 0), 1,
                 "feed re-ingest keeps the release hidden")


def test_mixed_cbz_epub_torrent_is_not_rejected():
    """A torrent that also contains an .epub is a novel bundle and must download."""
    ctx = Ctx("mixed")
    files = [
        {"id": 31, "name": "Mixed Novel v01.cbz", "size": 10},
        {"id": 32, "name": "Mixed Novel v01.epub", "size": 20},
    ]
    archive = zip_bytes({"Mixed Novel v01.cbz": b"cbz", "Mixed Novel v01.epub": b"epub"})
    with ctx.transport as transport:
        live = wire_happy_path_transport(
            transport, files, {}, archive,
            live_info=dict(ready_info(files, size=len(archive)), id=777, torrent_id=777),
        )
        release_id = ctx.insert_release(title="Mixed Novel v01")
        ctx.manager.submit_release(ctx.row(release_id), source="manual")
        outcome = ctx.manager.poll_once(async_fetch=False)
        check_eq(outcome.get("completed"), 1, "mixed torrent is fetched normally")
        check_eq(outcome.get("rejected_manga"), 0, "mixed torrent is not rejected as manga")
        check_eq(ctx.row(release_id).get("media_type"), "light_novel",
                 "media type is unchanged for a novel bundle")
        check_eq(ctx.row(release_id).get("download_status"), "COMPLETED", "release is COMPLETED")


def test_main_py_route_wiring_static_guards():
    """FastAPI cannot be imported here, so guard the edited main.py as source text."""
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as handle:
        source = handle.read()
    for route in [
        '@app.post("/api/downloads/torbox/poll")',
        '@app.post("/api/downloads/torbox/{release_id}/retry")',
        '@app.post("/api/downloads/torbox/{release_id}/remove")',
        '@app.post("/api/downloads/torbox/clear-failed")',
        '@app.post("/api/downloader/torbox/test")',
        '@app.get("/api/releases/{release_id}/state")',
    ]:
        check_in(route, source, "main.py declares %s" % route)
    for literal in [
        "torbox_manager = TorBoxManager(db)",
        "torbox_manager.start()",
        "torbox_manager.stop()",
        "TORBOX_API_KEY_MASK",
        "_active_downloader_engine()",
        "asyncio.to_thread(torbox_manager.submit_release",
        "asyncio.to_thread(torbox_manager.poll_now",
        "asyncio.to_thread(torbox_manager.retry_release",
        "asyncio.to_thread(torbox_manager.remove_release",
        "asyncio.to_thread(torbox_manager.clear_failed",
        "asyncio.to_thread(torbox_manager.test_connection",
        "Cache-Control",
        '"engine": "torbox"',
        '"engine": "qbit"',
        "_masked_settings()",
        "torbox_api_key_set",
    ]:
        check_in(literal, source, "main.py wiring literal %r" % literal)
    # The real API key must never be rendered: settings context only carries the mask.
    check_in("settings[\"torbox_api_key\"] = TORBOX_API_KEY_MASK", source,
             "settings context replaces the key with the mask")
    check_not_in("torbox_api_key = SECRET", source, "no hard-coded secret in main.py")
    # Every torbox manager call must be off the event loop.
    check_not_in("torbox_manager.poll_once(async_fetch=False)", source,
                 "routes must not run a blocking poll on the event loop")


def test_engine_settings_roundtrip_through_database():
    ctx = Ctx("settings")
    check_eq(ctx.manager.engine(), "torbox", "manager resolves the configured engine")
    check_eq(ctx.db.get_setting("downloader_engine"), "torbox", "engine setting persisted")
    check_eq(str(ctx.manager.downloads_dir()), os.path.abspath(ctx.downloads),
             "downloads_dir resolves the configured absolute folder")
    check(os.path.isdir(ctx.downloads), "downloads dir exists")
    # A custom downloads dir is created on demand and used for the per-release folder.
    custom = os.path.join(ctx.root, "custom-downloads")
    ctx.db.set_setting("downloads_dir", custom)
    check_eq(str(ctx.manager.downloads_dir()), os.path.abspath(custom),
             "custom downloads_dir is honoured")
    check(os.path.isdir(custom), "custom downloads dir is created on demand")


def main():
    tests = [
        ("engine settings roundtrip", test_engine_settings_roundtrip_through_database),
        ("submit -> poll -> fetch -> COMPLETED", test_happy_path_submit_poll_fetch_complete),
        ("failure -> FAILED + remote delete", test_failure_marks_failed_and_deletes_remote),
        ("torrent-only guard (J-Novels link)", test_torrent_only_guard_rejects_jnovels_links),
        ("concurrency gate", test_concurrency_gate_blocks_new_submit),
        ("selective volume fetch", test_selective_volume_fetch_only_downloads_missing_files),
        ("retry / remove / clear-failed lifecycle", test_retry_remove_and_clear_failed_lifecycle),
        ("build_status derivation rules", test_build_status_derivation_rules),
        (".cbz-only detector matrix", test_cbz_only_helper_matrix),
        (".cbz-only torrent rejected as manga", test_cbz_only_torrent_rejected_as_manga),
        ("mixed .cbz + .epub torrent not rejected", test_mixed_cbz_epub_torrent_is_not_rejected),
        ("main.py route wiring (static)", test_main_py_route_wiring_static_guards),
    ]
    failures = []
    print("=" * 78)
    print("TorBox integration suite (real Database + TorBoxManager + TorboxDownloader)")
    print("=" * 78)
    for label, test in tests:
        print("\n--- %s ---" % label)
        try:
            test()
            print("  [PASS] %s" % label)
        except Exception as exc:  # noqa: BLE001 - reported as a suite failure
            import traceback

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
