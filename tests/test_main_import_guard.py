#!/usr/bin/env python3
"""Production-import guard for ``app.main`` (runs without FastAPI/Jinja2 installed).

Why this exists
---------------
The bundled/Calibre test interpreter has no FastAPI/Jinja2, so every other suite treats
``app/main.py`` as source text.  That missed a real production crash: ``main.py`` imported
``BackgroundTask`` from ``fastapi`` (``fastapi`` does not export it; it lives in
``starlette.background``), and the container died at startup with
``ImportError: cannot import name 'BackgroundTask' from 'fastapi'``.

This suite provides **faithful-enough stubs** of the FastAPI/Starlette surface used by
``app.main`` (deliberately mirroring the real API, e.g. ``BackgroundTask`` is NOT a
``fastapi`` attribute) plus ``bs4``/``feedparser`` stubs for the provider registry.  It then:

1. imports the REAL ``app.main`` module,
2. asserts the expected new route handlers are registered,
3. runs the FastAPI lifespan (startup + shutdown) once, catching runtime import/setup
   regressions in scheduler/TorBox/database maintenance,
4. cleans up its temp DATA/CALIBRE/DOWNLOAD dirs.

Hermetic: temp dirs via ``tempfile.gettempdir()`` (``mkdtemp`` is unusable in this sandbox), no
network (TorBox disabled, Calibre path empty).  No bare ``assert`` (Calibre's -O2 strips them);
every expectation goes through ``check``.

Usage::

    python tests/test_main_import_guard.py
    python tests/run_tests.py main_import_guard
"""

import asyncio
import os
import shutil
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

_CHECKS_RUN = [0]
_TEMP_DIRS = []


def check(condition, message):
    _CHECKS_RUN[0] += 1
    if not condition:
        raise AssertionError(message)


def check_in(needle, haystack, message):
    check(needle in haystack, "%s (missing %r)" % (message, needle))


def _make_temp_dir(label):
    path = os.path.join(tempfile.gettempdir(), "noveltrax-import-guard-%s-%s" % (label, os.urandom(6).hex()))
    os.makedirs(path, exist_ok=True)
    _TEMP_DIRS.append(path)
    return path


def _cleanup():
    while _TEMP_DIRS:
        shutil.rmtree(_TEMP_DIRS.pop(), ignore_errors=True)


def _install_fastapi_stubs():
    """Install faithful-enough fastapi/starlette stubs; returns the FastAPI class."""
    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=500, detail=None, **kwargs):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class Request:
        pass

    def Query(default=None, **kwargs):
        return default

    class FastAPI:
        def __init__(self, *args, **kwargs):
            self.routes = []

        def _decorator(self, *args, **kwargs):
            def wrap(func):
                self.routes.append((func.__name__, kwargs.get("response_class")))
                return func

            return wrap

        def get(self, *a, **k):
            return self._decorator(*a, **k)

        def post(self, *a, **k):
            return self._decorator(*a, **k)

        def put(self, *a, **k):
            return self._decorator(*a, **k)

        def delete(self, *a, **k):
            return self._decorator(*a, **k)

        def patch(self, *a, **k):
            return self._decorator(*a, **k)

        def middleware(self, *a, **k):
            return self._decorator(*a, **k)

        def add_middleware(self, *a, **k):
            return None

        def mount(self, *a, **k):
            return None

    fastapi.FastAPI = FastAPI
    fastapi.Request = Request
    fastapi.Query = Query
    fastapi.HTTPException = HTTPException
    # Faithful mirror: fastapi does NOT export BackgroundTask/BackgroundTasks names used
    # by main.py; it must come from starlette.background.
    check(not hasattr(fastapi, "BackgroundTask"),
          "stub fastapi must not expose BackgroundTask (mirrors the real package)")
    sys.modules["fastapi"] = fastapi

    responses = types.ModuleType("fastapi.responses")

    class _Response:
        def __init__(self, content=None, *args, **kwargs):
            self.content = content
            self.kwargs = kwargs

    for name in (
        "HTMLResponse", "JSONResponse", "FileResponse", "RedirectResponse",
        "Response", "StreamingResponse",
    ):
        setattr(responses, name, type(name, (_Response,), {}))
    sys.modules["fastapi.responses"] = responses

    class _Env:
        def __init__(self):
            self.filters = {}

    class Jinja2Templates:
        def __init__(self, *args, **kwargs):
            self.env = _Env()
            self.globals = {}

        def TemplateResponse(self, *args, **kwargs):
            return _Response(kwargs.get("name"))

    templating = types.ModuleType("fastapi.templating")
    templating.Jinja2Templates = Jinja2Templates
    sys.modules["fastapi.templating"] = templating

    staticfiles = types.ModuleType("fastapi.staticfiles")
    staticfiles.StaticFiles = type("StaticFiles", (), {"__init__": lambda self, *a, **k: None})
    sys.modules["fastapi.staticfiles"] = staticfiles

    starlette = types.ModuleType("starlette")
    background = types.ModuleType("starlette.background")

    class BackgroundTask:
        def __init__(self, func, *args, **kwargs):
            self.func = func
            self.args = args
            self.kwargs = kwargs

        def __call__(self):
            return self.func(*self.args, **self.kwargs)

    background.BackgroundTask = BackgroundTask
    sys.modules["starlette"] = starlette
    sys.modules["starlette.background"] = background
    return FastAPI


def _install_parser_stubs():
    for name, attrs in (
        ("bs4", {"BeautifulSoup": object, "NavigableString": str, "Tag": object}),
        ("feedparser", {"parse": lambda *args, **kwargs: None}),
    ):
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = types.SimpleNamespace(**attrs)


def test_production_import_and_lifespan():
    data_dir = _make_temp_dir("data")
    calibre_dir = _make_temp_dir("calibre")
    downloads_dir = _make_temp_dir("downloads")
    os.environ["DATA_DIR"] = data_dir
    os.environ["CALIBRE_DIR"] = calibre_dir
    os.environ["DOWNLOAD_DIR"] = downloads_dir
    os.environ.setdefault("TORBOX_ENABLED", "false")

    FastAPI = _install_fastapi_stubs()
    _install_parser_stubs()

    import app.main as main  # noqa: E402  (import after stubbing, like production)

    check(isinstance(main.app, FastAPI), "the FastAPI application object was created")
    handlers = {name for name, _ in main.app.routes}
    for expected in (
        "api_database_backup",
        "api_vacuum_database",
        "api_database_stats",
        "api_storage_health",
        "api_network_diagnostics",
        "api_prune_activity_logs",
        "api_series_attribution_audit",
        "api_rebind_release",
        "api_reset_series_identity",
        "api_series_identity_overview",
        "api_release_series_identity",
        "api_unpin_release_series",
    ):
        check_in(expected, handlers, "route handler %r is registered" % expected)
    check(len(handlers) >= 80, "the full route surface is registered (%d handlers)" % len(handlers))

    # The bug that crashed production: BackgroundTask must be imported from starlette.background.
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    source = open(main_path, "r", encoding="utf-8").read()
    for line in source.splitlines():
        if line.strip().startswith("from fastapi import"):
            check("BackgroundTask" not in line,
                  "fastapi import must not request BackgroundTask: %r" % line)
    check_in("from starlette.background import BackgroundTask", source,
             "BackgroundTask is imported from starlette.background")

    async def _run_lifespan():
        async with main.lifespan(main.app):
            pass

    asyncio.run(_run_lifespan())
    check(True, "FastAPI lifespan startup and shutdown completed")
    # Idempotent shutdown (the lifespan already stopped the background workers).
    for stop_target in ("task_manager", "torbox_manager"):
        try:
            getattr(main, stop_target).stop()
        except Exception:
            pass


def main():
    tests = [("production import + lifespan", test_production_import_and_lifespan)]
    failures = []
    print("=" * 78)
    print("NovelTrax production-import guard (faithful FastAPI/Starlette stubs)")
    print("=" * 78)
    for label, test in tests:
        print("\n--- %s ---" % label)
        try:
            test()
            print("  [PASS] %s" % label)
        except Exception as exc:  # noqa: BLE001 - reported as a suite failure
            traceback.print_exc()
            failures.append((label, exc))
            print("  [FAIL] %s: %s" % (label, exc))
    _cleanup()
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
