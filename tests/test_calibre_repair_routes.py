#!/usr/bin/env python3
"""Static + AST guard for the Calibre false-ownership repair routes (CONTRACT v4 §5).

The interpreter used for the repo suites has no ``fastapi``/``jinja2``, so
``main.py`` cannot be imported.  This suite parses ``backend/app/main.py`` with
:mod:`ast` and proves:

1. Exactly the two new routes exist with their literal decorators and async
   handler names: ``POST /api/series/{series_id}/unlink-calibre`` and
   ``POST /api/series/{series_id}/recheck-calibre``.
2. The handlers dispatch the blocking TaskManager calls through
   ``asyncio.to_thread`` (``task_manager.unlink_series_calibre`` /
   ``task_manager.recheck_series_calibre``) after an existence lookup, and map an
   unknown series to HTTP 404 while containing unexpected errors as HTTP 500
   JSON (the unlink route stays a 200 success when the series is already
   unlinked).
3. The response payloads carry the contract summary keys
   (``old_calibre_series_id``/``releases_reset``/``already_unlinked`` for unlink,
   ``linked``/``owned_volumes``/``reconciled``/``message`` for recheck) with a
   ``status`` envelope.
4. No pre-existing route/decorator was lost: the route-decorator count grows
   97 -> 99 and every route pinned by earlier features is still present.

Bare ``assert`` is never used (Calibre runs with ``sys.flags.optimize == 2`` and
compiles bare asserts out); an explicit ``check()`` helper raises ``AssertionError``
instead.

Stdlib only: ast, os, re, sys.  No fastapi import, no network, no database.

Usage::

    & $py tests\\run_tests.py calibre_repair_routes
    & $py tests\\test_calibre_repair_routes.py
"""

import ast
import os
import re
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend", "app")
MAIN_PY = os.path.join(BACKEND_DIR, "main.py")

# CONTRACT v4 §5: exactly these two routes are added (97 -> 99 decorators).
NEW_ROUTES = (
    ('@app.post("/api/series/{series_id}/unlink-calibre")', "api_unlink_series_calibre"),
    ('@app.post("/api/series/{series_id}/recheck-calibre")', "api_recheck_series_calibre"),
)

# Every route pinned by earlier features - none of these decorators may disappear.
PINNED_ROUTES = (
    '@app.get("/series"',
    '@app.get("/discovery"',
    '@app.get("/settings"',
    '@app.get("/downloads"',
    '@app.get("/api/calibre/series")',
    '@app.get("/api/calibre/test")',
    '@app.post("/api/calibre/sync")',
    '@app.get("/api/series")',
    '@app.get("/api/series/identity")',
    '@app.get("/api/series/aliases")',
    '@app.delete("/api/series/aliases/{override_id}")',
    '@app.post("/api/series/{series_id}/match-calibre")',
    '@app.post("/api/series/{series_id}/reset-identity")',
    '@app.post("/api/series/{series_id}/refresh-metadata")',
    '@app.post("/api/series/{series_id}/toggle")',
    '@app.post("/api/series/{series_id}/link")',
    '@app.post("/api/series/{series_id}/backfill")',
    '@app.post("/api/series/backfill")',
    '@app.post("/api/series/{series_id}/fetch-info")',
    '@app.post("/api/discovery/match")',
    '@app.post("/api/discovery/track")',
    '@app.post("/api/discovery/blacklist")',
    '@app.post("/api/releases/{release_id}/rebind")',
    '@app.get("/api/releases/{release_id}/series-identity")',
    '@app.post("/api/releases/{release_id}/unpin")',
    '@app.post("/api/releases/{release_id}/download")',
    '@app.get("/api/releases/{release_id}/state")',
    '@app.get("/api/downloads/status")',
    '@app.post("/api/downloader/torbox/test")',
    '@app.get("/api/storage/health")',
    '@app.get("/api/diagnostics/network")',
    '@app.post("/api/maintenance/logs/prune")',
    '@app.get("/api/maintenance/database/stats")',
    '@app.get("/api/maintenance/backup")',
    '@app.post("/api/maintenance/database/vacuum")',
    '@app.get("/api/maintenance/series-attribution")',
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


def _call_name(node):
    """Resolves a Call target to its dotted name ('asyncio.to_thread', 'db.x', ...)."""
    func = node.func if isinstance(node, ast.Call) else node
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    return ".".join(reversed(parts))


def http_status_constants(func_node):
    """Set of literal HTTPException(status_code=...) values raised inside a handler."""
    statuses = set()
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call) or _call_name(node) != "HTTPException":
            continue
        for keyword in node.keywords:
            if keyword.arg == "status_code":
                try:
                    statuses.add(int(ast.literal_eval(keyword.value)))
                except (TypeError, ValueError):
                    pass
    return statuses


def to_thread_targets(func_node):
    """Names of the callables dispatched via ``await asyncio.to_thread(<target>, ...)``."""
    targets = set()
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call) or _call_name(node) != "asyncio.to_thread":
            continue
        if not node.args:
            continue
        target = node.args[0]
        if isinstance(target, ast.Name):
            targets.add(target.id)
        elif isinstance(target, ast.Attribute):
            targets.add(_call_name(target))
    return targets


# ---------------------------------------------------------------------------
# 1. route declarations + inventory preservation
# ---------------------------------------------------------------------------
def check_route_declarations():
    source, tree = parse_main()
    functions = functions_by_name(tree)

    for literal, handler_name in NEW_ROUTES:
        check(literal in source, "main.py: missing the Calibre repair route %r" % literal)
        check(
            source.count(literal) == 1,
            "main.py: the Calibre repair route %r is declared more than once" % literal,
        )
        check(
            handler_name in functions,
            "main.py: missing the Calibre repair handler %r" % handler_name,
        )
        check(
            isinstance(functions[handler_name], ast.AsyncFunctionDef),
            "main.py: handler %r must be an async route" % handler_name,
        )

    decorator_count = len(
        re.findall(r'^@app\.(?:get|post|put|delete|patch)\(', source, re.MULTILINE)
    )
    check(
        decorator_count >= MIN_ROUTE_DECORATORS,
        "main.py: route decorator count is %d, expected >= %d after the two Calibre "
        "repair routes (97 -> 99)" % (decorator_count, MIN_ROUTE_DECORATORS),
    )


def check_preserved_routes():
    source = read_text(MAIN_PY)
    for route in PINNED_ROUTES:
        check(route in source, "main.py: pre-existing route literal %r was lost" % route)
    check(
        re.search(r'target\.get\("download_url"\)\s*or\s*target\.get\("link"\)', source)
        is not None,
        "main.py: the release-download URL resolution pinned by test_ui_wiring.py changed",
    )


# ---------------------------------------------------------------------------
# 2. POST /api/series/{series_id}/unlink-calibre
# ---------------------------------------------------------------------------
def check_unlink_route():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    check("api_unlink_series_calibre" in functions,
          "main.py: api_unlink_series_calibre handler is missing")
    func = functions["api_unlink_series_calibre"]
    segment = function_source(source, func)

    targets = to_thread_targets(func)
    for target in ("_find_series_row", "task_manager.unlink_series_calibre"):
        check(
            target in targets,
            "main.py: unlink-calibre does not dispatch %r through asyncio.to_thread "
            "(got %r)" % (target, sorted(targets)),
        )
    check(
        segment.count("asyncio.to_thread") >= 2,
        "main.py: unlink-calibre performs blocking work outside asyncio.to_thread",
    )

    statuses = http_status_constants(func)
    check(404 in statuses,
          "main.py: unlink-calibre does not map an unknown series to HTTP 404 (got %r)"
          % sorted(statuses))
    check(500 in statuses,
          "main.py: unlink-calibre does not contain unexpected errors as HTTP 500")
    check(
        'detail="Series not found"' in segment,
        "main.py: unlink-calibre is missing the 404 'Series not found' detail",
    )

    for marker in (
        'payload["status"] = "success"',
        '"old_calibre_series_id"',
        '"releases_reset"',
        '"already_unlinked"',
        '"message"',
        "isinstance(result, dict)",
    ):
        check(
            marker in segment,
            "main.py: unlink-calibre payload is missing the contract marker %r" % marker,
        )
    check(
        "already_unlinked" in segment and "status_code=400" not in segment,
        "main.py: unlink-calibre must stay a 200 success when already unlinked",
    )
    check(
        "JSONResponse(" in segment,
        "main.py: unlink-calibre does not return a JSON response",
    )


# ---------------------------------------------------------------------------
# 3. POST /api/series/{series_id}/recheck-calibre
# ---------------------------------------------------------------------------
def check_recheck_route():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    check("api_recheck_series_calibre" in functions,
          "main.py: api_recheck_series_calibre handler is missing")
    func = functions["api_recheck_series_calibre"]
    segment = function_source(source, func)

    targets = to_thread_targets(func)
    for target in ("_find_series_row", "task_manager.recheck_series_calibre"):
        check(
            target in targets,
            "main.py: recheck-calibre does not dispatch %r through asyncio.to_thread "
            "(got %r)" % (target, sorted(targets)),
        )
    check(
        segment.count("asyncio.to_thread") >= 2,
        "main.py: recheck-calibre performs blocking work outside asyncio.to_thread",
    )

    statuses = http_status_constants(func)
    check(404 in statuses,
          "main.py: recheck-calibre does not map an unknown series to HTTP 404 (got %r)"
          % sorted(statuses))
    check(500 in statuses,
          "main.py: recheck-calibre does not contain unexpected errors as HTTP 500")
    check(
        'detail="Series not found"' in segment,
        "main.py: recheck-calibre is missing the 404 'Series not found' detail",
    )

    for marker in (
        'payload.setdefault("status", "success")',
        '"linked"',
        '"owned_volumes"',
        '"reconciled"',
        '"message"',
        "isinstance(result, dict)",
    ):
        check(
            marker in segment,
            "main.py: recheck-calibre payload is missing the contract marker %r" % marker,
        )
    check(
        "JSONResponse(" in segment,
        "main.py: recheck-calibre does not return a JSON response",
    )


def main():
    check_route_declarations()
    check_preserved_routes()
    check_unlink_route()
    check_recheck_route()

    print(
        "calibre-repair routes guard PASSED: POST /api/series/{id}/unlink-calibre + "
        "/recheck-calibre declared with async handlers, existence 404 mapping, "
        "asyncio.to_thread dispatch of task_manager.unlink_series_calibre / "
        "recheck_series_calibre, 500 containment, contract summary keys "
        "(old_calibre_series_id/releases_reset/already_unlinked; linked/owned_volumes/"
        "reconciled/message), and the full pre-existing route inventory preserved "
        "(97 -> 99 decorators)"
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
