#!/usr/bin/env python3
"""Behaviour + static guard for the Calibre/tracked series picker snapshot cache.

CONTRACT rebind_perf v5 §4.  The repo test interpreter has no ``fastapi``, so
``main.py`` cannot be imported.  This suite therefore:

1. Extracts the *actual* cache globals and functions from ``main.py`` via
   :mod:`ast` and executes them in an isolated namespace with stub loaders, so the
   shipped implementation is tested (not a copy): first call loads, a second call
   within the 10 s TTL does not, expiry refetches, invalidation refetches, two
   concurrent callers share one load (single-flight), loader failures degrade to
   empty lists without raising and callers cannot corrupt the cache through the
   returned lists.
2. Statically proves both picker endpoints use the snapshot
   (``GET /api/calibre/series`` through a single ``asyncio.to_thread`` and
   ``GET /api/series`` via ``snapshot["tracked"]``) instead of rescanning
   Calibre/SQLite per request.
3. Statically proves every picker-affecting mutation invalidates the cache:
   re-bind, match-calibre, discovery/match, reset-identity, unlink-calibre,
   recheck-calibre and POST /api/calibre/sync (before the sync starts).
4. Preserves the route inventory (no route lost, count unchanged at 99).

Bare ``assert`` is never used (Calibre runs with ``sys.flags.optimize == 2`` and
compiles bare asserts out); an explicit ``check()`` helper raises ``AssertionError``.

Stdlib only: ast, logging, os, re, sys, threading, time, typing.  No network, no
database, no fastapi import.

Usage::

    & $py tests\\run_tests.py series_picker_cache
    & $py tests\\test_series_picker_cache.py
"""

import ast
import logging
import os
import re
import sys
import threading
import time
import typing

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend", "app")
MAIN_PY = os.path.join(BACKEND_DIR, "main.py")

CACHE_CONSTANT_NAMES = (
    "SERIES_PICKER_CACHE_TTL_SECONDS",
    "_series_picker_cache_lock",
    "_series_picker_cache",
)
CACHE_FUNCTION_NAMES = (
    "_series_picker_snapshot",
    "_invalidate_series_picker_cache",
)

# Every route that changes picker-relevant state must invalidate the snapshot.
INVALIDATING_HANDLERS = (
    "api_rebind_release",
    "api_match_series_to_calibre",
    "api_discovery_match",
    "api_reset_series_identity",
    "api_unlink_series_calibre",
    "api_recheck_series_calibre",
    "api_calibre_sync",
)

PICKER_HANDLERS = ("api_calibre_series_picker", "api_tracked_series_picker")

PINNED_ROUTES = (
    '@app.get("/series"',
    '@app.get("/api/calibre/series")',
    '@app.get("/api/series")',
    '@app.get("/api/series/identity")',
    '@app.get("/api/series/aliases")',
    '@app.post("/api/series/{series_id}/match-calibre")',
    '@app.post("/api/series/{series_id}/reset-identity")',
    '@app.post("/api/series/{series_id}/refresh-metadata")',
    '@app.post("/api/series/{series_id}/unlink-calibre")',
    '@app.post("/api/series/{series_id}/recheck-calibre")',
    '@app.post("/api/discovery/match")',
    '@app.post("/api/releases/{release_id}/rebind")',
    '@app.get("/api/releases/{release_id}/series-identity")',
    '@app.post("/api/releases/{release_id}/unpin")',
    '@app.post("/api/releases/{release_id}/download")',
    '@app.get("/api/releases/{release_id}/state")',
    '@app.get("/api/downloads/status")',
    '@app.get("/api/storage/health")',
    '@app.get("/api/diagnostics/network")',
    '@app.post("/api/maintenance/logs/prune")',
    '@app.get("/api/maintenance/database/stats")',
    '@app.get("/api/maintenance/backup")',
    '@app.post("/api/maintenance/database/vacuum")',
    '@app.get("/api/maintenance/series-attribution")',
    '@app.post("/api/calibre/sync")',
    '@app.post("/api/settings")',
    '@app.post("/api/debug/action")',
)

MIN_ROUTE_DECORATORS = 99


def read_text(path):
    """Read a UTF-8 text file, failing loudly (never silently) when absent."""
    if not os.path.isfile(path):
        raise AssertionError("expected file not found: %s" % path)
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def check(condition, message):
    """Unstrippable guard: works under ``sys.flags.optimize == 2``."""
    if not condition:
        raise AssertionError(message)


def parse_main():
    source = read_text(MAIN_PY)
    try:
        tree = ast.parse(source, filename=MAIN_PY)
    except SyntaxError as exc:  # pragma: no cover - py_compile catches this first
        raise AssertionError("main.py is not syntactically valid: %s" % exc)
    return source, tree


def functions_by_name(tree):
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def function_source(source, node):
    return ast.get_source_segment(source, node) or ""


class _QuietLogger:
    """No-op logger stand-in so the deliberately-failing loader tests stay silent."""

    def warning(self, *args, **kwargs):
        pass


class _FakeDB:
    """Minimal Database stand-in for the tracked-series loader."""

    def __init__(self, rows=None, error=None):
        self.rows = list(rows or [])
        self.error = error
        self.calls = 0

    def get_all_series(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [dict(row) for row in self.rows]


def load_picker_cache_namespace():
    """Exec the real cache globals/functions from main.py in an isolated namespace."""
    source, tree = parse_main()
    segments = []
    found_constants = set()
    found_functions = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            target_names = {getattr(target, "id", None) for target in node.targets}
            matched = target_names & set(CACHE_CONSTANT_NAMES)
            if matched:
                segment = ast.get_source_segment(source, node)
                if segment:
                    segments.append(segment)
                    found_constants.update(matched)
        elif isinstance(node, ast.FunctionDef) and node.name in CACHE_FUNCTION_NAMES:
            segment = ast.get_source_segment(source, node)
            if segment:
                segments.append(segment)
                found_functions.add(node.name)
    check(
        set(CACHE_CONSTANT_NAMES) <= found_constants,
        "main.py: missing picker cache constant(s) %r"
        % sorted(set(CACHE_CONSTANT_NAMES) - found_constants),
    )
    check(
        set(CACHE_FUNCTION_NAMES) <= found_functions,
        "main.py: missing picker cache function(s) %r"
        % sorted(set(CACHE_FUNCTION_NAMES) - found_functions),
    )

    namespace = {
        "time": time,
        "threading": threading,
        "logging": logging,
        "logger": _QuietLogger(),
        "Any": typing.Any,
        "Dict": typing.Dict,
        "List": typing.List,
        "db": _FakeDB(),
        "_calibre_series_picker_rows": lambda: [],
    }
    exec("\n\n".join(segments), namespace)  # noqa: S102 - executing the shipped cache code
    check(callable(namespace.get("_series_picker_snapshot")),
          "main.py: _series_picker_snapshot is not callable")
    check(callable(namespace.get("_invalidate_series_picker_cache")),
          "main.py: _invalidate_series_picker_cache is not callable")
    check(isinstance(namespace.get("_series_picker_cache_lock"), type(threading.Lock())),
          "main.py: _series_picker_cache_lock is not a threading.Lock()")
    check(namespace.get("_series_picker_cache") == {"ts": 0.0, "calibre": [], "tracked": []},
          "main.py: _series_picker_cache must start as {ts:0.0, calibre:[], tracked:[]}")
    check(abs(float(namespace.get("SERIES_PICKER_CACHE_TTL_SECONDS")) - 10.0) < 1e-9,
          "main.py: SERIES_PICKER_CACHE_TTL_SECONDS must be 10.0")
    return namespace


def _install_loaders(namespace, calibre_rows, tracked_rows, calibre_calls, db_calls):
    def load_calibre():
        calibre_calls.append(1)
        if isinstance(calibre_rows, Exception):
            raise calibre_rows
        return [dict(row) for row in (calibre_rows or [])]

    class _DB:
        def get_all_series(self):
            db_calls.append(1)
            if isinstance(tracked_rows, Exception):
                raise tracked_rows
            return [dict(row) for row in (tracked_rows or [])]

    namespace["_calibre_series_picker_rows"] = load_calibre
    namespace["db"] = _DB()


# ---------------------------------------------------------------------------
# 1. behaviour: TTL / invalidation / single-flight / failures
# ---------------------------------------------------------------------------
def check_cache_behaviour():
    namespace = load_picker_cache_namespace()
    snapshot = namespace["_series_picker_snapshot"]
    invalidate = namespace["_invalidate_series_picker_cache"]

    calibre_calls = []
    db_calls = []
    _install_loaders(
        namespace,
        [{"series_id": 1, "series_name": "Calibre A"}, {"series_id": 2, "series_name": "Calibre B"}],
        [{"id": 11, "name": "Tracked A"}],
        calibre_calls,
        db_calls,
    )
    invalidate()

    first = snapshot()
    check(first.get("calibre") and first.get("tracked"),
          "cache: first call did not load both sections")
    check(len(calibre_calls) == 1 and len(db_calls) == 1,
          "cache: first call must load Calibre and tracked series exactly once "
          "(calibre=%d tracked=%d)" % (len(calibre_calls), len(db_calls)))
    check(first["calibre"][0]["series_name"] == "Calibre A", "cache: calibre rows missing")

    # Second call within the TTL must be a cache hit.
    second = snapshot()
    check(len(calibre_calls) == 1 and len(db_calls) == 1,
          "cache: a second call within the 10 s TTL rescanned (calibre=%d tracked=%d)"
          % (len(calibre_calls), len(db_calls)))
    check(second["tracked"] == first["tracked"], "cache: cached rows changed between hits")
    check(second["calibre"] is not first["calibre"],
          "cache: snapshot must return copies of the cached lists")

    # Mutating a returned list must not corrupt the cache.
    second["calibre"].append({"series_id": 999, "series_name": "Injected"})
    third = snapshot()
    check(len(third["calibre"]) == 2,
          "cache: appending to a returned list corrupted the cached snapshot")
    check(all(row.get("series_id") != 999 for row in third["calibre"]),
          "cache: injected row leaked into the cached snapshot")

    # Explicit invalidation forces a refetch.
    invalidate()
    fourth = snapshot()
    check(len(calibre_calls) == 2 and len(db_calls) == 2,
          "cache: invalidation did not force a refetch (calibre=%d tracked=%d)"
          % (len(calibre_calls), len(db_calls)))
    check(fourth["calibre"] == first["calibre"], "cache: refetch returned different rows")

    # TTL expiry refetches.
    invalidate()
    namespace["SERIES_PICKER_CACHE_TTL_SECONDS"] = 0.05
    try:
        snapshot()
        check(len(calibre_calls) == 3, "cache: setup load before TTL expiry failed")
        time.sleep(0.08)
        snapshot()
        check(len(calibre_calls) == 4 and len(db_calls) == 4,
              "cache: TTL expiry did not refetch (calibre=%d tracked=%d)"
              % (len(calibre_calls), len(db_calls)))
    finally:
        namespace["SERIES_PICKER_CACHE_TTL_SECONDS"] = 10.0

    # Failure containment: raising loaders must degrade to empty lists, never raise.
    invalidate()
    _install_loaders(namespace, RuntimeError("calibre down"), RuntimeError("db down"),
                     calibre_calls, db_calls)
    failed = snapshot()
    check(isinstance(failed, dict) and failed.get("calibre") == [] and failed.get("tracked") == [],
          "cache: loader failures must return empty lists instead of raising")
    check(isinstance(snapshot(), dict), "cache: a cached failure must not raise on the next call")

    # Partial failure: one section fails, the other is still returned.
    invalidate()
    _install_loaders(namespace, [{"series_id": 7, "series_name": "Only Calibre"}],
                     RuntimeError("db down"), calibre_calls, db_calls)
    partial = snapshot()
    check(len(partial.get("calibre") or []) == 1 and partial.get("tracked") == [],
          "cache: a tracked-series failure must not discard the Calibre rows")

    # Single-flight: two concurrent callers on a cold cache share one load.
    invalidate()
    namespace["SERIES_PICKER_CACHE_TTL_SECONDS"] = 10.0
    started = threading.Event()
    release = threading.Event()
    load_count = {"calibre": 0}

    def slow_calibre():
        load_count["calibre"] += 1
        started.set()
        release.wait(2.0)
        return [{"series_id": 42, "series_name": "Slow Calibre"}]

    class _DB:
        def get_all_series(self):
            return [{"id": 42, "name": "Slow Tracked"}]

    namespace["_calibre_series_picker_rows"] = slow_calibre
    namespace["db"] = _DB()
    results = {}

    def worker(name):
        results[name] = snapshot()

    first_thread = threading.Thread(target=worker, args=("one",), daemon=True)
    second_thread = threading.Thread(target=worker, args=("two",), daemon=True)
    first_thread.start()
    check(started.wait(2.0), "cache: single-flight loader never started")
    second_thread.start()
    time.sleep(0.05)  # second caller is now blocked on the cache lock
    release.set()
    first_thread.join(3.0)
    second_thread.join(3.0)
    check(load_count["calibre"] == 1,
          "cache: two concurrent callers triggered %d loads instead of one "
          "(single-flight broken)" % load_count["calibre"])
    check(sorted(results.keys()) == ["one", "two"], "cache: concurrent callers did not return")
    check(results["one"].get("calibre") == results["two"].get("calibre"),
          "cache: concurrent callers saw inconsistent snapshots")


# ---------------------------------------------------------------------------
# 2. static guards: snapshot implementation, picker routes, invalidation
# ---------------------------------------------------------------------------
def check_static_implementation():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    check("import threading" in source,
          "main.py: threading is not imported for the picker cache lock")
    for function_name in CACHE_FUNCTION_NAMES:
        check(function_name in functions, "main.py: missing %r" % function_name)

    snapshot_segment = function_source(source, functions["_series_picker_snapshot"])
    for marker in (
        "with _series_picker_cache_lock",
        "SERIES_PICKER_CACHE_TTL_SECONDS",
        "time.monotonic()",
        "_calibre_series_picker_rows()",
        "db.get_all_series()",
        "except Exception",
        '{"calibre": [], "tracked": []}',
    ):
        check(marker in snapshot_segment,
              "main.py: _series_picker_snapshot is missing the marker %r" % marker)

    invalidate_segment = function_source(
        source, functions["_invalidate_series_picker_cache"]
    )
    check("with _series_picker_cache_lock" in invalidate_segment,
          "main.py: _invalidate_series_picker_cache does not take the cache lock")
    check('["ts"] = 0.0' in invalidate_segment,
          "main.py: _invalidate_series_picker_cache does not reset the timestamp")


def check_picker_routes():
    source, tree = parse_main()
    functions = functions_by_name(tree)

    check("api_calibre_series_picker" in functions,
          "main.py: api_calibre_series_picker handler is missing")
    calibre_segment = function_source(source, functions["api_calibre_series_picker"])
    check("asyncio.to_thread(_series_picker_snapshot)" in calibre_segment,
          "GET /api/calibre/series must load through one "
          "asyncio.to_thread(_series_picker_snapshot)")
    check("_series_picker_snapshot" in calibre_segment,
          "GET /api/calibre/series does not use the picker snapshot")
    check("_calibre_series_picker_rows(" not in calibre_segment,
          "GET /api/calibre/series still scans Calibre directly")
    check("db.get_all_series" not in calibre_segment,
          "GET /api/calibre/series still calls db.get_all_series directly")
    check('snapshot.get("calibre")' in calibre_segment
          and 'snapshot.get("tracked")' in calibre_segment,
          "GET /api/calibre/series does not read both snapshot sections")

    check("api_tracked_series_picker" in functions,
          "main.py: api_tracked_series_picker handler is missing")
    tracked_segment = function_source(source, functions["api_tracked_series_picker"])
    check("_series_picker_snapshot" in tracked_segment,
          "GET /api/series does not use the picker snapshot")
    check('snapshot.get("tracked")' in tracked_segment,
          "GET /api/series does not read snapshot['tracked']")
    check("db.get_all_series" not in tracked_segment,
          "GET /api/series still calls db.get_all_series directly")


def check_invalidation_routes():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    for handler_name in INVALIDATING_HANDLERS:
        check(handler_name in functions,
              "main.py: handler %r is missing" % handler_name)
        segment = function_source(source, functions[handler_name])
        check("_invalidate_series_picker_cache()" in segment,
              "main.py: %r does not invalidate the series picker cache" % handler_name)

    sync_segment = function_source(source, functions["api_calibre_sync"])
    invalidate_index = sync_segment.find("_invalidate_series_picker_cache()")
    sync_index = sync_segment.find("asyncio.to_thread(task_manager.sync_calibre")
    check(
        invalidate_index != -1 and sync_index != -1 and invalidate_index < sync_index,
        "main.py: POST /api/calibre/sync must invalidate the picker cache before the "
        "sync starts",
    )


def check_route_inventory():
    source = read_text(MAIN_PY)
    for route in PINNED_ROUTES:
        check(route in source, "main.py: pre-existing route literal %r was lost" % route)
    decorator_count = len(
        re.findall(r'^@app\.(?:get|post|put|delete|patch)\(', source, re.MULTILINE)
    )
    check(
        decorator_count >= MIN_ROUTE_DECORATORS,
        "main.py: route decorator count dropped to %d (expected >= %d)"
        % (decorator_count, MIN_ROUTE_DECORATORS),
    )


def main():
    check_cache_behaviour()
    check_static_implementation()
    check_picker_routes()
    check_invalidation_routes()
    check_route_inventory()

    print(
        "series-picker cache guard PASSED: lock-protected 10 s TTL snapshot with "
        "single-flight loading (hit/TTL-expiry/invalidation/single-flight/failure "
        "degradation), both picker endpoints consume the snapshot (no per-keystroke "
        "Calibre/SQLite scans), all 6 picker mutations + POST /api/calibre/sync "
        "invalidate it, and the existing route inventory is preserved"
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
