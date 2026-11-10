#!/usr/bin/env python3
"""Hermetic tests for the NovelTrax download-engine package (TorBox + qBittorrent adapter).

Everything here is stdlib only and offline:

* ``FakeApi`` is a URL-substring router that replaces the engine's HTTP plumbing at the
  module boundary (``torbox.urllib`` and ``torbox._open_no_redirect``), records every
  request and serves ``FakeStream`` responses.  No socket is ever opened.
* ``FakeSecurity`` replaces ``torbox.security`` so the SSRF shim can be exercised without
  DNS (the real ``security.is_safe_public_url`` resolves hostnames).
* ``FakeTime`` replaces ``torbox.time`` so retry backoff is recorded instead of slept.
* Temporary directories come from ``tempfile.mkdtemp()`` and are removed at the end.

Style: no bare ``assert`` statements (Calibre's -O2 interpreter would strip them); every
expectation is an explicit ``check()`` that raises ``AssertionError``.
"""

import io
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
for _path in (_BACKEND_DIR, _REPO_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.downloader import (  # noqa: E402  (sys.path set above)
    DEFAULT_DOWNLOADER_ENGINE,
    DOWNLOADER_ENGINES,
    QBitDownloader,
    TorboxDownloader,
    as_bool,
    build_downloader,
    resolve_downloader_engine,
)
from app.downloader import torbox as torbox_mod  # noqa: E402
from app.downloader.base import (  # noqa: E402
    DownloaderAuthError,
    DownloaderError,
    DownloaderFileError,
    DownloaderNotFoundError,
    DownloaderRateLimitError,
    QUEUED_ID_PREFIX,
    encode_queued_handle_id,
    extract_torrent_hash,
    parse_queued_handle_id,
)

SECRET_KEY = "tb-SUPER-SECRET-API-KEY-12345"
INFOHASH = "0123456789abcdef0123456789abcdef01234567"
MAGNET = "magnet:?xt=urn:btih:" + INFOHASH + "&dn=Novel+Volume+1"
TORRENT_BYTES = b"d4:info4:name4:test4:piece20:01234567890123456789e"

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
    check(needle in haystack, "%s (missing %r in %r)" % (message, needle, haystack))


def check_not_in(needle, haystack, message):
    check(needle not in haystack, "%s (unexpected %r in %r)" % (message, needle, haystack))


def expect_error(expected_type, fn, label, message_contains=None):
    """Run *fn* and require *expected_type* (with an optional message substring)."""
    caught = None
    try:
        fn()
    except expected_type as exc:
        caught = exc
    except BaseException as exc:  # noqa: BLE001 - converted to a clear failure
        raise AssertionError(
            "%s: expected %s, got %s: %s" % (label, expected_type.__name__, type(exc).__name__, exc)
        ) from exc
    if caught is None:
        raise AssertionError("%s: expected %s, nothing was raised" % (label, expected_type.__name__))
    if message_contains is not None:
        check_in(message_contains, str(caught), label + ": unexpected error message")
    return caught


# ---------------------------------------------------------------------------
# Temp directories
# ---------------------------------------------------------------------------
def make_temp_dir():
    """Create a hermetic temp dir under the process TEMP directory.

    NOTE: ``tempfile.mkdtemp()`` creates the directory with mode 0o700, whose Windows
    DACL is not readable/creatable by this session's sandbox token (child creation
    fails with EACCES).  We keep the same temp-dir intent with an explicit random name
    plus the default inherited permissions; nothing leaves the process TEMP directory.
    """
    path = os.path.join(tempfile.gettempdir(), "noveltrax-torbox-dl-%s" % os.urandom(8).hex())
    os.makedirs(path, exist_ok=True)
    _TEMP_DIRS.append(path)
    return path


def cleanup_temp_dirs():
    while _TEMP_DIRS:
        shutil.rmtree(_TEMP_DIRS.pop(), ignore_errors=True)


# ---------------------------------------------------------------------------
# Fake HTTP layer
# ---------------------------------------------------------------------------
class FakeHeaders:
    """Minimal case-insensitive header mapping (``.get`` is all the engine uses)."""

    def __init__(self, mapping=None):
        self._data = {}
        for key, value in (mapping or {}).items():
            self._data[str(key).lower()] = str(value)

    def get(self, key, default=None):
        return self._data.get(str(key).lower(), default)

    def items(self):
        return list(self._data.items())

    def __contains__(self, key):
        return str(key).lower() in self._data


class FakeStream:
    """Readable response body with a status code, headers and close()."""

    def __init__(self, body=b"", status=200, headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.body = bytes(body or b"")
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
    """A request reached the fake transport with no configured route."""


class RecordedRequest:
    """One HTTP request observed by :class:`FakeApi`."""

    def __init__(self, via, request, timeout):
        self.via = via
        self.url = request.full_url
        self.method = request.get_method()
        self.headers = {
            str(key).lower(): value for key, value in request.header_items()
        }
        self.body = request.data or b""
        self.timeout = timeout
        parsed = urllib.parse.urlsplit(self.url)
        self.path = parsed.path
        self.params = {
            key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()
        }

    def __repr__(self):
        return "RecordedRequest(via=%r, method=%r, url=%r)" % (self.via, self.method, self.url)


class FakeApi:
    """URL-substring router used for API calls and for streamed downloads."""

    def __init__(self):
        self._routes = []
        self.calls = []
        self._lock = threading.Lock()

    # -- routing ------------------------------------------------------------
    def on(self, needle, result):
        """Route every request whose URL contains *needle* to *result*.

        *result* may be ``bytes``/``str`` (response body), a ``dict``/``list``
        (JSON body), a ``FakeStream`` (status/headers included) or a callable
        ``fn(call)`` returning/raising one of those.
        """
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

    # -- transport hooks ----------------------------------------------------
    def _record(self, via, request, timeout):
        call = RecordedRequest(via, request, timeout)
        with self._lock:
            self.calls.append(call)
        return call

    def urlopen(self, request, timeout=None):
        """Replacement for ``urllib.request.urlopen`` (API calls)."""
        return self._dispatch(self._record("api", request, timeout))

    def open_stream(self, request, timeout=None):
        """Replacement for ``torbox._open_no_redirect`` (torrent + file streams)."""
        return self._dispatch(self._record("stream", request, timeout))

    # -- inspection ---------------------------------------------------------
    def reset(self):
        """Forget all routes and recorded calls (fresh scenario on the same fake)."""
        with self._lock:
            self._routes = []
            self.calls = []

    def calls_for(self, needle, method=None, via=None):
        matches = []
        with self._lock:
            for call in self.calls:
                if needle not in call.url:
                    continue
                if method is not None and call.method != method:
                    continue
                if via is not None and call.via != via:
                    continue
                matches.append(call)
        return matches

    def last_call(self, needle, method=None, via=None):
        matches = self.calls_for(needle, method=method, via=via)
        return matches[-1] if matches else None


def json_ok(data):
    """TorBox API envelope with a ``data`` payload."""
    return {"success": True, "data": data}


def http_error(status, body=b"", headers=None):
    """Route callable that raises ``urllib.error.HTTPError`` with a JSON body."""

    def _raise(call):
        raise urllib.error.HTTPError(
            call.url, status, "HTTP %d" % status, FakeHeaders(headers or {}), io.BytesIO(body),
        )

    return _raise


def _raise_runtime(message):
    """Route callable that raises a non-Downloader exception (transport failure)."""

    def _raise(call):
        raise RuntimeError(message)

    return _raise


class FakeSecurity:
    """Stand-in for ``app.security``; allows everything except denied substrings."""

    def __init__(self, denied=()):
        self.denied = tuple(denied)
        self.checked = []

    def is_safe_public_url(self, url):
        self.checked.append(url)
        return not any(token in (url or "") for token in self.denied)


class FakeTime:
    """Stand-in for the ``time`` module: sleeps are recorded, clocks are fixed."""

    def __init__(self):
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def monotonic(self):
        return 100000.0

    def time(self):
        return 1700000000.0


@contextmanager
def torbox_env(security=None, clock=None):
    """Patch the TorBox module's HTTP transport, SSRF guard and clock for one test."""
    saved = {
        "security": torbox_mod.security,
        "urllib": torbox_mod.urllib,
        "open_no_redirect": torbox_mod._open_no_redirect,
        "time": torbox_mod.time,
    }
    api = FakeApi()
    security = security if security is not None else FakeSecurity()
    clock = clock if clock is not None else FakeTime()
    torbox_mod.security = security
    torbox_mod.urllib = SimpleNamespace(
        request=SimpleNamespace(Request=urllib.request.Request, urlopen=api.urlopen),
        parse=urllib.parse,
        error=urllib.error,
    )
    torbox_mod._open_no_redirect = api.open_stream
    torbox_mod.time = clock
    try:
        yield SimpleNamespace(api=api, security=security, clock=clock)
    finally:
        for name, value in saved.items():
            setattr(torbox_mod, name, value)


class LogCapture(logging.Handler):
    """Collects formatted log records so secret-hygiene checks can scan them."""

    def __init__(self):
        logging.Handler.__init__(self)
        self.messages = []

    def emit(self, record):
        try:
            self.messages.append(record.getMessage())
        except Exception:  # noqa: BLE001 - never let logging break the test
            self.messages.append(str(record.msg))


# ---------------------------------------------------------------------------
# Helpers: settings DB, qBittorrent fake, zip builder
# ---------------------------------------------------------------------------
class FakeDB:
    """Just enough of ``Database`` for engine selection/construction."""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)


class FakeQbitClient:
    """Records the calls ``QBitDownloader`` makes on the real client interface."""

    def __init__(self):
        self.enabled = True
        self.category = "novels"
        self.tags = "noveltrax"
        self.savepath = "/downloads"
        self.add_calls = []
        self.get_calls = []
        self.file_calls = []
        self.delete_calls = []
        self.torrents = []
        self.files = []
        self.add_result = True

    def test_connection(self):
        return {"success": True, "engine": "qbit", "version": "v5.0.0"}

    def add_torrent(self, url_or_magnet, savepath=None, category=None, tags=None, paused=False):
        self.add_calls.append({
            "url_or_magnet": url_or_magnet,
            "savepath": savepath,
            "category": category,
            "tags": tags,
            "paused": paused,
        })
        return self.add_result

    def get_torrents(self, category=None, filter_status="all"):
        self.get_calls.append({"category": category, "filter_status": filter_status})
        return list(self.torrents)

    def get_torrent_files(self, torrent_hash):
        self.file_calls.append(torrent_hash)
        return list(self.files)

    def delete_torrent(self, torrent_hash, delete_files=False):
        self.delete_calls.append({"hash": torrent_hash, "delete_files": delete_files})
        return True


def build_zip(entries):
    """Build an in-memory zip. *entries*: list of ``(name, data, external_attr|None)``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data, external_attr in entries:
            if external_attr is None:
                archive.writestr(name, data)
            else:
                info = zipfile.ZipInfo(name)
                info.external_attr = external_attr
                archive.writestr(info, data)
    return buffer.getvalue()


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def list_dir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Tests: engine selection / construction
# ---------------------------------------------------------------------------
def test_engine_constants_and_as_bool():
    check_eq(DOWNLOADER_ENGINES, ("qbit", "torbox"), "DOWNLOADER_ENGINES value")
    check_eq(DEFAULT_DOWNLOADER_ENGINE, "qbit", "DEFAULT_DOWNLOADER_ENGINE value")
    true_values = ("1", "true", "TRUE", "yes", "on", "enabled", " true ")
    for value in true_values:
        check(as_bool(value) is True, "as_bool(%r) should be True" % (value,))
    false_values = ("0", "false", "FALSE", "no", "off", "disabled")
    for value in false_values:
        check(as_bool(value, True) is False, "as_bool(%r, True) should be False" % (value,))
    check(as_bool(None) is False, "as_bool(None) defaults to False")
    check(as_bool(None, True) is True, "as_bool(None, True) uses the default")
    check(as_bool("", True) is True, "as_bool('', True) uses the default")
    check(as_bool("unexpected", True) is True, "as_bool(unknown, True) uses the default")
    check(as_bool("unexpected", False) is False, "as_bool(unknown, False) uses the default")
    check(as_bool(True) is True, "as_bool(True) passes booleans through")
    check(as_bool(False, True) is False, "as_bool(False) passes booleans through")


def test_resolve_downloader_engine():
    check_eq(resolve_downloader_engine(FakeDB({})), "qbit", "unset engine falls back to qbit")
    check_eq(
        resolve_downloader_engine(FakeDB({"downloader_engine": "torbox"})),
        "torbox",
        "torbox engine is selected",
    )
    check_eq(
        resolve_downloader_engine(FakeDB({"downloader_engine": " TORBOX "})),
        "torbox",
        "engine setting is stripped and lower-cased",
    )
    check_eq(
        resolve_downloader_engine(FakeDB({"downloader_engine": "deluge"})),
        "qbit",
        "unknown engine falls back to qbit",
    )
    check_eq(
        resolve_downloader_engine(FakeDB({"downloader_engine": ""})),
        "qbit",
        "empty engine falls back to qbit",
    )


def test_build_downloader_torbox():
    db = FakeDB({
        "downloader_engine": "torbox",
        "torbox_api_key": SECRET_KEY,
        "torbox_enabled": "true",
        "torbox_delete_after_download": "false",
        "torbox_max_download_gb": "2.5",
        "torbox_download_concurrency": "99",
        "torbox_parallel_chunks": "0",
    })
    downloader = build_downloader(db)
    check(isinstance(downloader, TorboxDownloader), "torbox engine builds a TorboxDownloader")
    check_eq(downloader.name, "torbox", "torbox downloader name")
    check_eq(downloader.api_key, SECRET_KEY, "API key is passed through")
    check(downloader.enabled is True, "enabled is parsed from torbox_enabled")
    check(downloader.delete_after_download is False, "delete_after_download is parsed")
    check_eq(downloader.max_file_bytes, int(2.5 * 1024 ** 3), "max_download_gb becomes max_file_bytes")
    check_eq(downloader.max_concurrency, 16, "download concurrency is clamped to 16")
    check_eq(downloader.parallel_chunks, 1, "parallel chunks are clamped to 1")
    check_not_in(SECRET_KEY, repr(downloader), "torbox repr must not leak the API key")

    clamped_high = build_downloader(FakeDB({
        "downloader_engine": "torbox",
        "torbox_api_key": SECRET_KEY,
        "torbox_max_download_gb": "999999",
    }))
    check_eq(clamped_high.max_file_bytes, 10240 * 1024 ** 3, "max_download_gb clamps at 10240")

    defaults = build_downloader(FakeDB({
        "downloader_engine": "torbox",
        "torbox_api_key": "key",
        "torbox_max_download_gb": "not-a-number",
        "torbox_download_concurrency": "nope",
        "torbox_parallel_chunks": "nope",
    }))
    check_eq(defaults.max_file_bytes, 64 * 1024 ** 3, "invalid max_download_gb falls back to 64 GiB")
    check_eq(defaults.max_concurrency, 4, "invalid concurrency falls back to 4")
    check_eq(defaults.parallel_chunks, 4, "invalid chunk count falls back to 4")

    disabled = build_downloader(FakeDB({
        "downloader_engine": "torbox",
        "torbox_api_key": SECRET_KEY,
        "torbox_enabled": "false",
    }))
    check(disabled.enabled is False, "torbox_enabled=false disables the engine")

    explicit = build_downloader(
        FakeDB({"downloader_engine": "qbit", "torbox_api_key": SECRET_KEY}),
        engine="torbox",
    )
    check(isinstance(explicit, TorboxDownloader), "explicit engine argument wins over the setting")


def test_build_downloader_qbit():
    fake_client = FakeQbitClient()
    adapter = build_downloader(FakeDB({}), qbit_client=fake_client)
    check(isinstance(adapter, QBitDownloader), "qbit engine builds a QBitDownloader")
    check_eq(adapter.name, "qbit", "qbit adapter name")
    check(adapter.client is fake_client, "injected qBittorrent client is used")

    db = FakeDB({
        "qbit_url": "http://192.168.1.5:9090",
        "qbit_username": "admin-user",
        "qbit_password": "admin-pass",
        "qbit_category": "manga",
        "qbit_tags": "noveltrax-tags",
        "qbit_savepath": "/data/downloads",
        "qbit_enabled": "false",
    })
    configured = build_downloader(db)
    check(isinstance(configured, QBitDownloader), "qbit engine builds a QBitDownloader from settings")
    client = configured.client
    check_eq(client.base_url, "http://192.168.1.5:9090", "qbit_url is used as base_url")
    check_eq(client.host, "192.168.1.5", "qbit_url host is parsed")
    check_eq(client.port, 9090, "qbit_url port is parsed")
    check_eq(client.username, "admin-user", "qbit_username is passed")
    check_eq(client.password, "admin-pass", "qbit_password is passed")
    check_eq(client.category, "manga", "qbit_category is passed")
    check_eq(client.tags, "noveltrax-tags", "qbit_tags is passed")
    check_eq(client.savepath, "/data/downloads", "qbit_savepath is passed")
    check(client.enabled is False, "qbit_enabled=false disables the client")
    check(configured.enabled is False, "adapter enabled mirrors the client")


def test_qbit_adapter_protocol():
    client = FakeQbitClient()
    client.torrents = [{"hash": INFOHASH, "state": "stalledUP", "progress": 1.0}]
    client.files = [{"name": "Vol 1.epub", "size": 1234}]
    adapter = QBitDownloader(client)

    result = adapter.test_connection()
    check_eq(result.get("success"), True, "adapter test_connection delegates")

    submitted = adapter.submit({"download_url": MAGNET, "link": None}, "/downloads/Series")
    check_eq(len(client.add_calls), 1, "adapter submit calls add_torrent once")
    add_call = client.add_calls[0]
    check_eq(add_call["url_or_magnet"], MAGNET, "adapter submit passes the magnet")
    check_eq(add_call["savepath"], "/downloads/Series", "adapter submit passes the savepath")
    check_eq(add_call["category"], "novels", "adapter submit uses the configured category")
    check_eq(add_call["tags"], "noveltrax", "adapter submit uses the configured tags")
    check_eq(submitted["state"], "downloading", "adapter submit state")
    check_eq(submitted["handle"]["hash"], INFOHASH, "adapter submit extracts the infohash")
    check_eq(submitted["handle"]["engine"], "qbit", "adapter submit handle engine")

    client.add_result = False
    expect_error(
        DownloaderError,
        lambda: adapter.submit({"download_url": MAGNET}, "/downloads/Series"),
        "adapter submit raises when qBittorrent rejects the torrent",
        message_contains="rejected the torrent",
    )
    client.add_result = True

    status = adapter.status({"hash": INFOHASH})
    check_eq(status["state"], "ready", "stalledUP maps to ready")
    check_eq(status["progress"], 1.0, "adapter status progress is passed through")
    check_eq(len(status["files"]), 1, "adapter status lists torrent files")
    check_eq(status["files"][0]["name"], "Vol 1.epub", "adapter status file name")
    check_eq(status["files"][0]["size"], 1234, "adapter status file size")
    check_eq(status["files"][0]["id"], 0, "adapter status file index")
    check_eq(len(client.get_calls), 1, "adapter status asks the configured category")
    check_eq(client.get_calls[0]["category"], None, "adapter status lets the client apply its category")

    downloader_state = adapter.status({"hash": "f" * 40})
    check_eq(downloader_state["state"], "queued", "unknown torrent reports queued")
    check_eq(downloader_state["files"], [], "unknown torrent has no files")

    check_eq(adapter.fetch_files({"hash": INFOHASH}, None, "/tmp/x"), [], "qBittorrent fetch_files is a no-op")
    check(adapter.cancel({"hash": INFOHASH}) is True, "adapter cancel delegates to delete_torrent")
    check_eq(len(client.delete_calls), 1, "adapter cancel calls delete_torrent once")
    check_eq(client.delete_calls[0]["hash"], INFOHASH, "adapter cancel passes the hash")
    check_eq(client.delete_calls[0]["delete_files"], False, "cancel(delete_remote=True) keeps the data files")
    check(adapter.cancel({}) is False, "adapter cancel without a hash returns False")
    check(adapter.cancel({"hash": INFOHASH}, delete_remote=False) is True, "cancel(delete_remote=False) returns True")
    check_eq(client.delete_calls[-1]["delete_files"], True, "cancel(delete_remote=False) deletes qBittorrent data")


# ---------------------------------------------------------------------------
# Tests: base helpers
# ---------------------------------------------------------------------------
def test_base_helpers():
    check_eq(QUEUED_ID_PREFIX, "queued:", "queued handle prefix")
    check_eq(encode_queued_handle_id(17), "queued:17", "queued id encoding")
    check_eq(parse_queued_handle_id("queued:17"), 17, "queued id decoding")
    check_eq(parse_queued_handle_id("QUEUED:17"), 17, "queued id decoding is case-insensitive")
    check_eq(parse_queued_handle_id("17"), None, "plain ids are not queued ids")
    check_eq(parse_queued_handle_id("queued:not-a-number"), None, "garbage queued ids decode to None")
    check_eq(parse_queued_handle_id(None), None, "None is not a queued id")

    check_eq(extract_torrent_hash(MAGNET), INFOHASH, "magnet infohash extraction")
    check_eq(extract_torrent_hash("urn:btih:" + INFOHASH.upper()), INFOHASH, "hex infohash is lower-cased")
    check_eq(extract_torrent_hash("not a magnet"), None, "non-magnet strings have no infohash")
    check_eq(extract_torrent_hash(None), None, "None has no infohash")

    rate_limited = DownloaderRateLimitError("slow down", retry_after=12.5)
    check_eq(rate_limited.retry_after, 12.5, "rate-limit error keeps retry_after")
    check(isinstance(DownloaderAuthError("x"), DownloaderError), "auth errors derive from DownloaderError")
    check(isinstance(DownloaderFileError("x"), DownloaderError), "file errors derive from DownloaderError")
    check(isinstance(DownloaderNotFoundError("x"), DownloaderError), "not-found errors derive from DownloaderError")


# ---------------------------------------------------------------------------
# Tests: submission
# ---------------------------------------------------------------------------
def test_magnet_submit_uses_multipart():
    with torbox_env() as env:
        env.api.on("/torrents/checkcached", json_ok([]))
        env.api.on("/torrents/createtorrent", json_ok({"torrent_id": 4242, "hash": INFOHASH}))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        result = downloader.submit(
            {"title": "Series Vol 1", "download_url": MAGNET},
            "/downloads/Series Vol 1",
            wanted_volumes=[1.0, 2.0],
        )

        check_eq(result["cached"], False, "uncached magnet submit reports cached=False")
        check_eq(result["state"], "queued", "uncached magnet submit state")
        handle = result["handle"]
        check_eq(handle["engine"], "torbox", "handle engine")
        check_eq(handle["torrent_id"], 4242, "handle torrent_id")
        check_eq(handle["hash"], INFOHASH, "handle infohash")
        check_eq(handle["savepath"], "/downloads/Series Vol 1", "handle savepath")
        check_eq(handle["wanted_volumes"], [1.0, 2.0], "handle wanted volumes")
        check_eq(handle["cached"], False, "handle cached flag")

        cached_calls = env.api.calls_for("/torrents/checkcached", method="GET")
        check_eq(len(cached_calls), 1, "checkcached is called once for a magnet")
        check_eq(cached_calls[0].params.get("hash"), INFOHASH, "checkcached hash parameter")
        check_eq(cached_calls[0].params.get("format"), "list", "checkcached format parameter")
        check_eq(cached_calls[0].params.get("list_files"), "true", "checkcached list_files parameter")

        create_calls = env.api.calls_for("/torrents/createtorrent", method="POST")
        check_eq(len(create_calls), 1, "createtorrent is posted once")
        create_call = create_calls[0]
        check_in("multipart/form-data", create_call.headers.get("content-type"), "createtorrent content type")
        check_in(b'name="magnet"', create_call.body, "createtorrent multipart magnet field")
        check_in(MAGNET.encode("utf-8"), create_call.body, "createtorrent multipart magnet value")
        check_eq(
            create_call.headers.get("authorization"),
            "Bearer " + SECRET_KEY,
            "API key travels in the Authorization header",
        )
        check_not_in(SECRET_KEY, create_call.url, "API key is not in the request URL")
        check_not_in("token=", create_call.url, "magnet submission does not use token query auth")

        check_eq(len(env.security.checked), 2, "SSRF guard validates the API base per request")
        check_eq(downloader._throttle_events, 0, "no throttling events recorded")


def test_magnet_submit_cached():
    with torbox_env() as env:
        env.api.on("/torrents/checkcached", json_ok({INFOHASH: {"hash": INFOHASH, "size": 99}}))
        env.api.on("/torrents/createtorrent", json_ok({"torrent_id": 7}))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        result = downloader.submit({"title": "Cached", "link": MAGNET}, "/downloads/Cached")
        check_eq(result["cached"], True, "cached magnet submit reports cached=True")
        check_eq(result["state"], "ready", "cached magnet submit state is ready")
        check_eq(result["handle"]["cached"], True, "cached flag is persisted on the handle")
        check_eq(result["handle"]["hash"], INFOHASH, "cached submit still records the infohash")


def test_torrent_file_multipart_submit():
    torrent_url = "https://nyaa.si/download/556677.torrent"
    with torbox_env() as env:
        env.api.on(torrent_url, TORRENT_BYTES)
        env.api.on("/torrents/createtorrent", json_ok({"torrent_id": 99, "hash": INFOHASH}))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        result = downloader.submit({"title": "Torrent File", "download_url": torrent_url}, "/downloads/Torrent")

        stream_calls = env.api.calls_for(torrent_url, via="stream")
        check_eq(len(stream_calls), 1, ".torrent URL is fetched exactly once")
        check_eq(stream_calls[0].headers.get("user-agent"), "NovelTrax/1.0", "torrent fetch User-Agent")
        check_not_in(SECRET_KEY, stream_calls[0].url, "torrent fetch URL never carries the API key")

        create_calls = env.api.calls_for("/torrents/createtorrent", method="POST")
        check_eq(len(create_calls), 1, ".torrent upload posts once")
        body = create_calls[0].body
        check_in("multipart/form-data", create_calls[0].headers.get("content-type"), "upload content type")
        check_in(b'name="file"', body, "upload uses the file field")
        check_in(b'filename="release.torrent"', body, "upload filenames the torrent")
        check_in(b"Content-Type: application/x-bittorrent", body, "upload declares the torrent media type")
        check_in(TORRENT_BYTES, body, "upload carries the fetched torrent bytes")
        check_eq(result["handle"]["torrent_id"], 99, "torrent upload handle id")
        check_eq(result["handle"]["hash"], INFOHASH, "torrent upload keeps the returned infohash")
        check_eq(env.api.calls_for("/torrents/checkcached"), [], "file uploads skip checkcached")


def test_view_url_derives_torrent_download():
    derived_url = "https://nyaa.si/download/1234.torrent"
    with torbox_env() as env:
        env.api.on(derived_url, TORRENT_BYTES)
        env.api.on("/torrents/createtorrent", json_ok({"torrent_id": 12}))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        result = downloader.submit(
            {"title": "View URL", "download_url": "https://nyaa.si/view/1234?ignored=1"},
            "/downloads/View",
        )
        stream_calls = env.api.calls_for("nyaa.si", via="stream")
        check_eq(len(stream_calls), 1, "/view/<id> derives exactly one torrent fetch")
        check_eq(stream_calls[0].url, derived_url, "/view/<id> becomes /download/<id>.torrent")
        check_eq(env.api.calls_for("/view/"), [], "the view page itself is never fetched")
        check_eq(result["handle"]["torrent_id"], 12, "derived torrent submit handle")
        check_eq(env.api.calls_for("/torrents/createtorrent")[0].body.count(TORRENT_BYTES), 1, "derived torrent bytes uploaded once")


def test_html_and_webpage_rejected():
    with torbox_env() as env:
        env.api.on("nyaa.si/download/9.torrent", b"<!DOCTYPE html><html><body>Cloudflare</body></html>")
        env.api.on("/torrents/createtorrent", json_ok({"torrent_id": 1}))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        error = expect_error(
            DownloaderError,
            lambda: downloader.submit({"title": "HTML", "download_url": "https://nyaa.si/view/9"}, "/downloads/HTML"),
            "HTML body must never be submitted as a torrent",
            message_contains="no magnet or valid .torrent",
        )
        check_in("did not return a bencoded .torrent file", str(error), "HTML rejection explains the bad body")
        check_eq(env.api.calls_for("/torrents/createtorrent"), [], "HTML body never reaches createtorrent")

    with torbox_env() as env:
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        error = expect_error(
            DownloaderError,
            lambda: downloader.submit(
                {"title": "J-Novel", "download_url": "https://jnovels.com/some-novel-page"},
                "/downloads/JNovel",
            ),
            "plain web pages have no torrent metadata",
            message_contains="no magnet or valid .torrent",
        )
        check_in("https://jnovels.com/some-novel-page", str(error), "rejection lists the rejected candidate")
        check_eq(env.api.calls, [], "a plain web page causes no network call")

    with torbox_env() as env:
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        expect_error(
            DownloaderError,
            lambda: downloader.submit({"title": "No link"}, "/downloads/None"),
            "release without any candidate is rejected",
            message_contains="candidates: none",
        )
        check_eq(env.api.calls, [], "no candidate means no network call")


def test_checkcached_variants():
    downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
    cases = [
        (json_ok({INFOHASH: {"hash": INFOHASH}}), True, "dict keyed by infohash is cached"),
        (json_ok({"other": {"hash": INFOHASH}}), True, "dict entry hash match is cached"),
        (json_ok([INFOHASH]), True, "list of infohashes is cached"),
        (json_ok([]), False, "empty list is not cached"),
        (json_ok({"other": {"hash": "f" * 40}}), False, "non-matching dict is not cached"),
        (json_ok(["f" * 40]), False, "non-matching list is not cached"),
        (json_ok(None), False, "None payload is not cached"),
    ]
    for payload, expected, label in cases:
        with torbox_env() as env:
            env.api.on("/torrents/checkcached", payload)
            actual = downloader._check_cached(INFOHASH)
            check_eq(actual, expected, "checkcached: " + label)
            call = env.api.last_call("/torrents/checkcached")
            check_eq(call.params.get("hash"), INFOHASH, "checkcached hash for " + label)


# ---------------------------------------------------------------------------
# Tests: state normalisation and status payloads
# ---------------------------------------------------------------------------
def test_normalise_state_matrix():
    cases = [
        ("completed", {}, "ready"),
        ("cached", {}, "ready"),
        ("Completed", {}, "ready"),
        ("Completed", {"download_present": True}, "ready"),
        ("error", {"download_present": True}, "ready"),
        ("downloading", {"download_present": True}, "ready"),
        ("error", {}, "failed"),
        ("failed", {}, "failed"),
        ("missingfiles", {}, "failed"),
        ("dead", {}, "failed"),
        ("uploading", {}, "processing"),
        ("download finished", {}, "processing"),
        ("completed", {"download_finished": True}, "processing"),
        ("completed", {"download_present": False}, "processing"),
        ("queued", {"download_present": False}, "processing"),
        ("stalled (no seeds)", {}, "stalled"),
        ("stalled", {}, "stalled"),
        ("paused", {}, "stalled"),
        ("queued", {}, "stalled"),
        ("downloading", {}, "downloading"),
        ("metadl", {}, "downloading"),
        ("checkingresumedata", {}, "downloading"),
        ("checking", {}, "downloading"),
        ("weird-state", {}, "queued"),
        ("", {}, "queued"),
    ]
    for raw_state, row, expected in cases:
        actual = TorboxDownloader._normalise_state(raw_state, row)
        check_eq(actual, expected, "_normalise_state(%r, %r)" % (raw_state, row))
    check_eq(
        TorboxDownloader._normalise_state("error", {"download_present": True}),
        "ready",
        "download_present=true precedence: ready beats an error state",
    )
    check_eq(
        TorboxDownloader._normalise_state("completed", {"download_present": False}),
        "processing",
        "download_present=false precedence: processing beats a ready state",
    )


def test_status_payload_and_progress():
    row = {
        "id": 5,
        "hash": INFOHASH,
        "download_state": "cached",
        "download_present": True,
        "progress": 55,
        "size": 1000,
        "download_speed": 123,
        "eta": 10,
        "seeds": 3,
        "files": [
            {"id": 1, "name": "Vol 1.epub", "size": 500},
            {"name": "Vol 2.epub", "size": 500},
        ],
    }
    with torbox_env() as env:
        env.api.on("/torrents/mylist", json_ok([row]))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        status = downloader.status({"engine": "torbox", "torrent_id": 5, "hash": INFOHASH})
        check_eq(status["state"], "ready", "cached row with download_present is ready")
        check_eq(status["cached"], True, "cached flag derived from raw state")
        check_eq(status["progress"], 0.55, "percent progress is normalised to 0..1")
        check_eq(status["size"], 1000, "size is passed through")
        check_eq(status["download_speed"], 123, "speed is passed through")
        check_eq(len(status["files"]), 2, "file list is normalised")
        check_eq(status["files"][0], {"id": 1, "name": "Vol 1.epub", "size": 500}, "first file descriptor")
        check_eq(status["files"][1]["id"], 1, "missing file ids fall back to the index")
        call = env.api.last_call("/torrents/mylist")
        check_eq(call.params.get("id"), "5", "mylist asks for the torrent id")
        check_eq(call.params.get("bypass_cache"), "true", "mylist bypasses the 600s cache")

    with torbox_env() as env:
        env.api.on("/torrents/mylist", json_ok([]))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        expect_error(
            DownloaderNotFoundError,
            lambda: downloader.status({"engine": "torbox", "torrent_id": 5}),
            "missing torrent raises DownloaderNotFoundError",
        )
        expect_error(
            DownloaderNotFoundError,
            lambda: downloader.status({"engine": "torbox"}),
            "handle without ids raises DownloaderNotFoundError",
        )


# ---------------------------------------------------------------------------
# Tests: queued slot handling and promotion
# ---------------------------------------------------------------------------
def test_queued_slot_status():
    handle = {"engine": "torbox", "torrent_id": None, "queued_id": 31, "hash": INFOHASH}
    with torbox_env() as env:
        env.api.on("/torrents/getqueued", json_ok([
            {"queued_id": 31, "queue_position": 2, "active_limit": 3, "size": 1234, "hash": INFOHASH},
        ]))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        status = downloader.status(handle)
        check_eq(status["state"], "queued_slot", "queued submission reports queued_slot")
        check_eq(status["queued_id"], 31, "queued id is echoed")
        check_eq(status["queue_position"], 2, "queue position is reported")
        check_eq(status["active_limit"], 3, "active slot limit is reported")
        check_eq(status["size"], 1234, "queued size is reported")
        check_eq(status["progress"], 0.0, "queued progress is 0")
        check_eq(status["files"], [], "queued submission has no files yet")
        check_eq(env.api.calls_for("/torrents/mylist"), [], "still-queued items do not query mylist")


def test_queued_promotion_via_getqueued():
    handle = {"engine": "torbox", "torrent_id": None, "queued_id": 31, "hash": INFOHASH}
    with torbox_env() as env:
        env.api.on("/torrents/getqueued", json_ok([{"queued_id": 31, "torrent_id": 88}]))
        env.api.on("/torrents/mylist", json_ok([{
            "id": 88,
            "hash": INFOHASH,
            "download_state": "completed",
            "download_present": True,
            "progress": 100,
            "size": 4321,
            "files": [{"id": 3, "name": "Vol 1.epub", "size": 4321}],
        }]))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        status = downloader.status(handle)
        check_eq(status.get("promoted_torrent_id"), 88, "queued row with torrent_id is reported as promoted")
        check_eq(status.get("queued_id"), 31, "promotion keeps the queued id")
        check_eq(status["state"], "ready", "promoted torrent uses the normal status payload")
        check_eq(status["size"], 4321, "promoted torrent size")
        check_eq(len(status["files"]), 1, "promoted torrent files")


def test_queued_promotion_via_mylist_fallback():
    handle = {"engine": "torbox", "torrent_id": None, "queued_id": 31, "hash": INFOHASH}
    rows = [
        {"id": 5, "hash": "f" * 40, "download_state": "downloading", "progress": 10},
        {"id": 77, "hash": INFOHASH, "download_state": "downloading", "progress": 25, "size": 999},
    ]
    with torbox_env() as env:
        env.api.on("/torrents/getqueued", json_ok([]))
        env.api.on("/torrents/mylist", json_ok(rows))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        status = downloader.status(handle)
        check_eq(status.get("promoted_torrent_id"), 77, "disappeared queued row is found in mylist by hash")
        check_eq(status["state"], "downloading", "promoted fallback uses the live state")


def test_queued_lookup_not_found_is_tolerated():
    handle = {"engine": "torbox", "torrent_id": None, "queued_id": 31, "hash": INFOHASH}
    with torbox_env() as env:
        env.api.on("/torrents/getqueued", http_error(404, b'{"detail":"not found"}'))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        status = downloader.status(handle)
        check_eq(status["state"], "queued_slot", "missing queued entry stays queued instead of failing")
        check_eq(status["queue_position"], None, "unknown queue position is None")


# ---------------------------------------------------------------------------
# Tests: fetch_files (zip and per-file)
# ---------------------------------------------------------------------------
def test_fetch_files_zip_path():
    temp_root = make_temp_dir()
    dest = os.path.join(temp_root, "downloads")
    zip_bytes = build_zip([
        ("Vol 1.epub", b"epub-one-bytes", None),
        ("extras/readme.txt", b"readme-bytes", None),
    ])
    with torbox_env() as env:
        env.api.on("/torrents/requestdl", json_ok("https://download.torbox.app/torrent-321.zip"))
        env.api.on("download.torbox.app/torrent-321.zip", zip_bytes)
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        saved = downloader.fetch_files({"engine": "torbox", "torrent_id": 321}, None, dest)

        check_eq(len(saved), 2, "zip extraction returns every file")
        names = sorted(entry["name"] for entry in saved)
        check_eq(names, ["Vol 1.epub", "readme.txt"], "zip extraction keeps files and flattens directories")
        by_name = {entry["name"]: entry for entry in saved}
        check_eq(read_bytes(by_name["Vol 1.epub"]["path"]), b"epub-one-bytes", "extracted file content")
        check_eq(read_bytes(by_name["readme.txt"]["path"]), b"readme-bytes", "extracted nested file content")
        check_eq(by_name["Vol 1.epub"]["size"], len(b"epub-one-bytes"), "extracted size")
        check(os.path.isfile(os.path.join(dest, "Vol 1.epub")), "extracted file lives under dest_dir")

        requestdl_calls = env.api.calls_for("/torrents/requestdl", via="api")
        check_eq(len(requestdl_calls), 1, "whole-torrent fetch requests one zip link")
        check_eq(requestdl_calls[0].params.get("torrent_id"), "321", "zip link requests the torrent id")
        check_eq(requestdl_calls[0].params.get("zip_link"), "true", "zip link sets zip_link=true")
        check_eq(requestdl_calls[0].params.get("token"), SECRET_KEY, "requestdl uses token query auth")
        check_eq(requestdl_calls[0].headers.get("authorization"), None, "requestdl does not send the Authorization header")
        stream_calls = env.api.calls_for("download.torbox.app/torrent-321.zip", via="stream")
        check_eq(len(stream_calls), 1, "zip body is streamed once")
        check_eq(stream_calls[0].headers.get("user-agent"), "NovelTrax/1.0", "stream User-Agent")
        leftovers = [name for name in list_dir(dest) if name.startswith(".torbox-") or name.endswith(".part")]
        check_eq(leftovers, [], "no partial zip remains after extraction")


def test_fetch_files_zip_slip_rejected():
    cases = [
        ([("..", None)], "../escape.txt", "Unsafe '..' path", "parent-traversal member"),
        ([("/absolute.txt", None)], "absolute.txt", "Unsafe absolute path", "absolute-path member"),
        ([("link", 0o120777 << 16)], "link", "symlink", "symlink member"),
        ([("C:/windows.txt", None)], "windows.txt", "Unsafe absolute path", "drive-letter member"),
    ]
    for entries, escape_name, expected_phrase, label in cases:
        temp_root = make_temp_dir()
        dest = os.path.join(temp_root, "downloads")
        members = []
        for name, attr in entries:
            if name == "..":
                name = "../escape.txt"
            members.append((name, b"payload", attr))
        zip_bytes = build_zip(members)
        with torbox_env() as env:
            env.api.on("/torrents/requestdl", json_ok("https://download.torbox.app/bad.zip"))
            env.api.on("download.torbox.app/bad.zip", zip_bytes)
            downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
            error = expect_error(
                DownloaderFileError,
                lambda: downloader.fetch_files({"engine": "torbox", "torrent_id": 7}, None, dest),
                "zip-slip rejection: " + label,
            )
            check_in(expected_phrase, str(error), "zip-slip rejection message: " + label)
        check(not os.path.exists(os.path.join(temp_root, escape_name)), "zip-slip escaped the download dir: " + label)
        check_eq(list_dir(dest), [], "zip-slip leaves no extracted file: " + label)


def test_fetch_files_per_file_retry_403():
    temp_root = make_temp_dir()
    dest = os.path.join(temp_root, "downloads")
    files = [{"id": 11, "name": "Vol 1.epub", "size": 11}]
    link_requests = []
    progress = []

    def requestdl(call):
        link_requests.append(call)
        return json_ok("https://download.torbox.app/link-%d.bin" % len(link_requests))

    with torbox_env() as env:
        env.api.on("/torrents/requestdl", requestdl)
        env.api.on("download.torbox.app/link-1.bin", FakeStream(b"", status=403))
        env.api.on(
            "download.torbox.app/link-2.bin",
            FakeStream(b"hello world", status=200, headers={"Content-Length": "11"}),
        )
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        saved = downloader.fetch_files(
            {"engine": "torbox", "torrent_id": 12}, files, dest,
            progress_cb=lambda name, written: progress.append((name, written)),
        )

        check_eq(len(link_requests), 2, "403 retry requests a fresh requestdl link")
        check_eq(link_requests[0].params.get("file_id"), "11", "first attempt requests the file id")
        check_eq(link_requests[1].params.get("file_id"), "11", "retry requests the same file id")
        stream_urls = [call.url for call in env.api.calls_for("download.torbox.app", via="stream")]
        check_eq(stream_urls, [
            "https://download.torbox.app/link-1.bin",
            "https://download.torbox.app/link-2.bin",
        ], "each attempt streams the fresh link (never the stale one)")
        check_eq(env.clock.sleeps, [1.0], "403 retry backs off 1s")
        check_eq(len(saved), 1, "retry eventually saves the file")
        check_eq(saved[0]["size"], 11, "saved file size")
        check_eq(read_bytes(saved[0]["path"]), b"hello world", "saved file content (single stream)")
        check_eq(progress, [("Vol 1.epub", 11)], "final progress reported once")
        check_eq(downloader._throttle_events, 0, "403 is not counted as throttling")


def test_fetch_files_per_file_parallel():
    temp_root = make_temp_dir()
    dest = os.path.join(temp_root, "downloads")
    files = [
        {"id": 21, "name": "Vol 1.epub", "size": 5},
        {"id": 22, "name": "Vol 2.epub", "size": 6},
    ]

    def requestdl(call):
        file_id = call.params.get("file_id")
        return json_ok("https://download.torbox.app/byid-%s.bin" % file_id)

    with torbox_env() as env:
        env.api.on("/torrents/requestdl", requestdl)
        env.api.on("download.torbox.app/byid-21.bin", FakeStream(b"one!!", headers={"Content-Length": "5"}))
        env.api.on("download.torbox.app/byid-22.bin", FakeStream(b"two!!!", headers={"Content-Length": "6"}))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True, max_concurrency=2)
        saved = downloader.fetch_files({"engine": "torbox", "torrent_id": 23}, files, dest)

        check_eq(len(saved), 2, "parallel fetch saves both files")
        by_name = {entry["name"]: entry for entry in saved}
        check_eq(sorted(by_name), ["Vol 1.epub", "Vol 2.epub"], "parallel fetch file names")
        check_eq(read_bytes(by_name["Vol 1.epub"]["path"]), b"one!!", "parallel fetch first content")
        check_eq(read_bytes(by_name["Vol 2.epub"]["path"]), b"two!!!", "parallel fetch second content")
        requestdl_ids = sorted(
            call.params.get("file_id") for call in env.api.calls_for("/torrents/requestdl")
        )
        check_eq(requestdl_ids, ["21", "22"], "parallel fetch requests each file id once")
        leftovers = [name for name in list_dir(dest) if name.endswith(".part")]
        check_eq(leftovers, [], "parallel fetch leaves no .part files")


def test_fetch_files_size_cap_precheck():
    temp_root = make_temp_dir()
    dest = os.path.join(temp_root, "downloads")
    files = [{"id": 1, "name": "huge.epub", "size": 100}]
    with torbox_env() as env:
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True, max_file_bytes=10)
        error = expect_error(
            DownloaderFileError,
            lambda: downloader.fetch_files({"engine": "torbox", "torrent_id": 3}, files, dest),
            "reported file size above the cap fails before any link request",
            message_contains="above the configured cap",
        )
        check_in("torbox_max_download_gb", str(error), "size-cap error names the setting")
        check_eq(env.api.calls, [], "size cap is enforced before any requestdl")

        expect_error(
            DownloaderFileError,
            lambda: downloader.fetch_files({"engine": "torbox", "torrent_id": 3, "size": 100}, None, dest),
            "whole-torrent archive above the cap is rejected",
            message_contains="whole-torrent archive",
        )
        check_eq(env.api.calls, [], "archive size cap is enforced before any requestdl")

    expect_error(
        DownloaderNotFoundError,
        lambda: downloader.fetch_files({"engine": "torbox"}, None, dest),
        "fetch_files needs a torrent_id",
    )


# ---------------------------------------------------------------------------
# Tests: cancel / connection / SSRF guard
# ---------------------------------------------------------------------------
def test_cancel():
    with torbox_env() as env:
        env.api.on("/torrents/controltorrent", json_ok({}))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)

        check(downloader.cancel({"engine": "torbox", "torrent_id": 5}) is True, "cancel active torrent returns True")
        control_calls = env.api.calls_for("/torrents/controltorrent", method="POST")
        check_eq(len(control_calls), 1, "cancel posts controltorrent once")
        payload = json.loads(control_calls[0].body.decode("utf-8"))
        check_eq(payload.get("operation"), "delete", "cancel operation is delete")
        check_eq(payload.get("torrent_id"), 5, "cancel targets the torrent id")
        check_in("application/json", control_calls[0].headers.get("content-type"), "cancel sends a JSON body")

        check(downloader.cancel({"engine": "torbox", "queued_id": 9}) is True, "cancel queued submission returns True")
        payload = json.loads(env.api.last_call("/torrents/controltorrent").body.decode("utf-8"))
        check_eq(payload.get("queued_id"), 9, "cancel targets the queued id")
        check_eq(payload.get("torrent_id"), None, "queued cancel does not send a torrent id")

        calls_before = len(env.api.calls)
        check(downloader.cancel({}) is False, "cancel without ids returns False")
        check_eq(len(env.api.calls), calls_before, "cancel without ids makes no HTTP call")

        check(downloader.cancel({"torrent_id": 5}, delete_remote=False) is True, "cancel(delete_remote=False) is a no-op")
        check_eq(len(env.api.calls), calls_before, "cancel(delete_remote=False) makes no HTTP call")

    with torbox_env() as env:
        env.api.on("/torrents/controltorrent", http_error(404, b'{"detail":"gone"}'))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        check(downloader.cancel({"torrent_id": 5}) is True, "already-deleted torrent counts as cancelled")

    with torbox_env() as env:
        env.api.on("/torrents/controltorrent", http_error(500, b'{"detail":"boom"}'))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        check(downloader.cancel({"torrent_id": 5}) is False, "controltorrent failure is reported as False")


def test_test_connection():
    disabled = TorboxDownloader(api_key="", enabled=True)
    check(disabled.enabled is False, "empty API key disables the engine")
    result = disabled.test_connection()
    check_eq(result["success"], False, "disabled connection test fails")
    check_in("disabled or there is no API key", result["message"], "disabled connection message")

    with torbox_env() as env:
        env.api.on("/user/me", json_ok({"plan": "Pro", "active_slot_count": 2}))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        result = downloader.test_connection()
        check_eq(result["success"], True, "connection test succeeds")
        check_eq(result["engine"], "torbox", "connection result engine")
        check_eq(result["plan"], "Pro", "connection result plan")
        check_eq(result["active_slots"], 2, "connection result active slots")
        check_in("Connected to Torbox", result["message"], "connection success message")
        check_not_in(SECRET_KEY, json.dumps(result), "connection result never leaks the API key")

        env.api.reset()
        env.api.on("/user/me", http_error(403, b'{"detail":"bad key"}'))
        failed = downloader.test_connection()
        check_eq(failed["success"], False, "auth failure is returned, not raised")
        check_in("rejected the API key", failed["message"], "auth failure message")
        check_not_in(SECRET_KEY, failed["message"], "auth failure message never leaks the API key")

        env.api.reset()
        env.api.on("/user/me", http_error(429, b'{"detail":"slow down"}', headers={"Retry-After": "7"}))
        throttled = downloader.test_connection()
        check_eq(throttled["success"], False, "rate limit is returned as failure")
        check_in("rate limit", throttled["message"], "rate-limit message")
        check_in("7s", throttled["message"], "rate-limit retry_after is surfaced")


def test_unsafe_urls_blocked():
    with torbox_env(security=FakeSecurity(denied=("127.0.0.1",))) as env:
        check_eq(
            torbox_mod.validate_public_url("https://api.torbox.app/v1/api"),
            "https://api.torbox.app/v1/api",
            "shim accepts allowed public URLs",
        )
        expect_error(
            torbox_mod._UnsafeURLError,
            lambda: torbox_mod.validate_public_url("http://127.0.0.1/secret"),
            "shim rejects loopback URLs",
            message_contains="not an allowed public",
        )
        env.api.on("/user/me", json_ok({"plan": "Pro"}))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True, base_url="http://127.0.0.1:9/")
        result = downloader.test_connection()
        check_eq(result["success"], False, "connection test reports a blocked API base URL")
        check_in("not allowed", result["message"], "blocked base URL message")
        check_eq(env.api.calls, [], "blocked base URL never reaches the transport")
        expect_error(
            DownloaderError,
            lambda: downloader._request("GET", "/user/me"),
            "API requests to a blocked base URL raise DownloaderError",
            message_contains="not allowed",
        )

    temp_root = make_temp_dir()
    dest = os.path.join(temp_root, "downloads")
    files = [{"id": 1, "name": "Vol 1.epub", "size": 10}]
    with torbox_env(security=FakeSecurity(denied=("127.0.0.1",))) as env:
        env.api.on("/torrents/requestdl", json_ok("http://127.0.0.1/evil.bin"))
        downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
        error = expect_error(
            DownloaderFileError,
            lambda: downloader.fetch_files({"engine": "torbox", "torrent_id": 2}, files, dest),
            "unsafe download link is blocked",
            message_contains="Blocked unsafe download URL",
        )
        check_in("127.0.0.1", str(error), "blocked URL is named in the error")
        check_eq(env.api.calls_for("127.0.0.1", via="stream"), [], "blocked link is never streamed")


def test_secret_hygiene():
    downloader = TorboxDownloader(api_key=SECRET_KEY, enabled=True)
    check_not_in(SECRET_KEY, repr(downloader), "repr never contains the API key")
    check_not_in(SECRET_KEY, str(downloader), "str never contains the API key")
    check_in("***", repr(downloader), "repr masks the API key")

    capture = LogCapture()
    app_logger = logging.getLogger("noveltrax.downloader")
    previous_level = app_logger.level
    app_logger.setLevel(logging.DEBUG)
    app_logger.addHandler(capture)
    error_messages = []
    try:
        with torbox_env() as env:
            env.api.on("/torrents/checkcached", json_ok([]))
            env.api.on("/torrents/createtorrent", json_ok({"torrent_id": 1, "hash": INFOHASH}))
            env.api.on("/torrents/mylist", json_ok([{
                "id": 1, "hash": INFOHASH, "download_state": "downloading", "progress": 10,
            }]))
            env.api.on("/user/me", json_ok({"plan": "Pro"}))
            env.api.on("/torrents/controltorrent", json_ok({}))
            env.api.on("/torrents/requestdl", http_error(500, b'{"detail":"boom"}'))
            try:
                downloader.submit({"title": "Secret", "download_url": MAGNET}, "/downloads/Secret")
                downloader.status({"engine": "torbox", "torrent_id": 1, "hash": INFOHASH})
                downloader.test_connection()
                downloader.cancel({"torrent_id": 1})
                downloader._requestdl(1, file_id=2)
            except DownloaderError as exc:
                error_messages.append(str(exc))
    finally:
        app_logger.removeHandler(capture)
        app_logger.setLevel(previous_level)

    check_eq(len(error_messages), 1, "requestdl failure is raised for inspection")
    check(len(capture.messages) > 0, "log capture observed engine log records")
    for message in capture.messages:
        check_not_in(SECRET_KEY, message, "log line leaks the API key: %r" % (message,))
        check_not_in("token=", message, "log line leaks a token query: %r" % (message,))
    for message in error_messages:
        check_not_in(SECRET_KEY, message, "error text leaks the API key: %r" % (message,))
        check_not_in("token=", message, "error text leaks a token query: %r" % (message,))
        check_in("/torrents/requestdl", message, "requestdl error names the endpoint, not the key")

    with torbox_env() as env:
        env.api.on("/torrents/checkcached", json_ok([]))
        env.api.on("/torrents/createtorrent", json_ok({"torrent_id": 2, "hash": INFOHASH}))
        downloader.submit({"title": "Header", "download_url": MAGNET}, "/downloads/Header")
        create_call = env.api.last_call("/torrents/createtorrent")
        check_eq(create_call.headers.get("authorization"), "Bearer " + SECRET_KEY, "key travels only in the header")
        check_not_in(SECRET_KEY, create_call.url, "submission URL never carries the key")

    with torbox_env() as env:
        env.api.on("/torrents/checkcached", json_ok([]))
        env.api.on("/torrents/createtorrent", _raise_runtime("socket boom"))
        try:
            downloader.submit({"title": "Boom", "download_url": MAGNET}, "/downloads/Boom")
            raised = None
        except DownloaderError as exc:
            raised = str(exc)
        check(raised is not None, "transport failure raises DownloaderError")
        check_not_in(SECRET_KEY, raised, "transport error never leaks the key")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
TESTS = [
    ("engine_constants_and_as_bool", test_engine_constants_and_as_bool),
    ("resolve_downloader_engine", test_resolve_downloader_engine),
    ("build_downloader_torbox", test_build_downloader_torbox),
    ("build_downloader_qbit", test_build_downloader_qbit),
    ("qbit_adapter_protocol", test_qbit_adapter_protocol),
    ("base_helpers", test_base_helpers),
    ("magnet_submit_uses_multipart", test_magnet_submit_uses_multipart),
    ("magnet_submit_cached", test_magnet_submit_cached),
    ("torrent_file_multipart_submit", test_torrent_file_multipart_submit),
    ("view_url_derives_torrent_download", test_view_url_derives_torrent_download),
    ("html_and_webpage_rejected", test_html_and_webpage_rejected),
    ("checkcached_variants", test_checkcached_variants),
    ("normalise_state_matrix", test_normalise_state_matrix),
    ("status_payload_and_progress", test_status_payload_and_progress),
    ("queued_slot_status", test_queued_slot_status),
    ("queued_promotion_via_getqueued", test_queued_promotion_via_getqueued),
    ("queued_promotion_via_mylist_fallback", test_queued_promotion_via_mylist_fallback),
    ("queued_lookup_not_found_is_tolerated", test_queued_lookup_not_found_is_tolerated),
    ("fetch_files_zip_path", test_fetch_files_zip_path),
    ("fetch_files_zip_slip_rejected", test_fetch_files_zip_slip_rejected),
    ("fetch_files_per_file_retry_403", test_fetch_files_per_file_retry_403),
    ("fetch_files_per_file_parallel", test_fetch_files_per_file_parallel),
    ("fetch_files_size_cap_precheck", test_fetch_files_size_cap_precheck),
    ("cancel", test_cancel),
    ("test_connection", test_test_connection),
    ("unsafe_urls_blocked", test_unsafe_urls_blocked),
    ("secret_hygiene", test_secret_hygiene),
]


def main():
    failures = []
    checks_start = _CHECKS_RUN[0]
    for name, test in TESTS:
        try:
            test()
        except AssertionError as exc:
            failures.append((name, exc))
            print("  FAIL %s: %s" % (name, exc))
        except BaseException as exc:  # noqa: BLE001 - converted to a readable failure
            failures.append((name, exc))
            print("  ERROR %s: %s: %s" % (name, type(exc).__name__, exc))
            traceback.print_exc()
        else:
            print("  PASS %s" % name)
    executed = _CHECKS_RUN[0] - checks_start
    cleanup_temp_dirs()
    if failures:
        lines = ["%d/%d test group(s) failed (%d checks executed)" % (len(failures), len(TESTS), executed)]
        for name, exc in failures:
            lines.append("  - %s: %s: %s" % (name, type(exc).__name__, exc))
        raise AssertionError("\n".join(lines))
    print("ALL %d TEST GROUPS PASSED (%d checks executed)" % (len(TESTS), executed))


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print("FAILED: %s" % (exc,))
        raise SystemExit(1)
