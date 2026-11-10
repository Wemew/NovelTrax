#!/usr/bin/env python3
"""Static + AST guard for the release re-bind / series-identity API (and settings extras).

Why static and not an import test: the interpreter used for the repo suites has no
``fastapi``/``jinja2``, so ``main.py`` cannot be imported and the routes cannot be
called.  This module parses ``backend/app/main.py`` with :mod:`ast` and proves the
CONTRACT_V2_REBIND §4 surface:

1. The six new routes are declared with their exact paths/handler names:
   ``POST /api/releases/{release_id}/rebind``, ``POST /api/series/{series_id}/reset-identity``,
   ``GET /api/series/identity``, ``GET /api/releases/{release_id}/series-identity``,
   ``POST /api/releases/{release_id}/unpin`` and ``GET /api/series`` (tracked picker).
2. Payload validation / error mapping is structurally present in each handler: the
   re-bind handler validates the mode + target and maps bad input to HTTP 400,
   unknown rows to HTTP 404 (and unexpected failures to 500); the reset/unpin routes
   do the same; the two picker endpoints never raise and degrade to empty lists.
3. Every blocking TaskManager/DB call is dispatched through ``asyncio.to_thread``
   (checked structurally: the awaited call's first argument resolves to the expected
   ``task_manager.X`` / ``db.Y`` helper), so the event loop is never blocked.
4. The tracked-series picker builds the contract payload fields
   (``id/name/display_title/calibre_series_id/identity_locked/book_count/
   owned_volumes/tracked``) and filters by the shared normalized name/display/authors.
5. The whole pre-existing route inventory is preserved: no pinned decorator was
   lost and the total decorator count never drops below the post-series-identity
   baseline.
6. Settings-feature endpoints (CONTRACT_V2_REBIND §6) are likewise declared and
   wired through ``asyncio.to_thread`` once the addendum is implemented here.

Bare ``assert`` is never used (Calibre runs with ``sys.flags.optimize == 2`` and
compiles bare asserts out, which would make this guard vacuous); an explicit
``check()`` helper raises ``AssertionError`` instead.

Stdlib only: ast, os, re, sys.  No third-party imports, no network, no database.

Usage::

    & $py tests\\run_tests.py settings_features_api
    & $py tests\\test_settings_features_api.py
"""

import ast
import os
import re
import shutil
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend", "app")
MAIN_PY = os.path.join(BACKEND_DIR, "main.py")
DIAGNOSTICS_PY = os.path.join(BACKEND_DIR, "diagnostics.py")
BACKEND_PATH = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_PATH not in sys.path:
    sys.path.insert(0, BACKEND_PATH)

from app import diagnostics  # noqa: E402
from app.database import Database  # noqa: E402

# --- CONTRACT_V2_REBIND §4 routes ------------------------------------------
NEW_REBIND_ROUTES = (
    ('@app.post("/api/releases/{release_id}/rebind")', "api_rebind_release"),
    ('@app.post("/api/series/{series_id}/reset-identity")', "api_reset_series_identity"),
    ('@app.get("/api/series/identity")', "api_series_identity_overview"),
    ('@app.get("/api/releases/{release_id}/series-identity")', "api_release_series_identity"),
    ('@app.post("/api/releases/{release_id}/unpin")', "api_unpin_release_series"),
    ('@app.get("/api/series")', "api_tracked_series_picker"),
)

# --- CONTRACT_V2_REBIND §6.3 settings/diagnostics routes -------------------
NEW_SETTINGS_ROUTES = (
    ('@app.get("/api/storage/health")', "api_storage_health"),
    ('@app.get("/api/diagnostics/network")', "api_network_diagnostics"),
    ('@app.post("/api/maintenance/logs/prune")', "api_prune_activity_logs"),
    ('@app.get("/api/maintenance/database/stats")', "api_database_stats"),
    ('@app.get("/api/maintenance/backup")', "api_database_backup"),
    ('@app.post("/api/maintenance/database/vacuum")', "api_vacuum_database"),
    ('@app.get("/api/maintenance/series-attribution")', "api_series_attribution_audit"),
)

# CONTRACT_V2_REBIND §6.5: the existing debug-action endpoint gains one safe action.
DEBUG_ACTION_LITERALS = (
    "clear-descriptions",
    "clear-discovery",
    "clear-thumbnails",
    "purge-pdf",
    "clear-metadata",
)

ALL_NEW_ROUTES = NEW_REBIND_ROUTES + NEW_SETTINGS_ROUTES

# Every route the earlier features pinned (downloader Tier 4, UI wiring, TorBox,
# series identity) - none of these decorators may disappear.
PINNED_EXISTING_ROUTES = (
    '@app.get("/downloads"',
    '@app.get("/api/downloader/queue"',
    '@app.get("/api/downloads"',
    '@app.get("/api/downloads/status"',
    '@app.post("/api/downloader/test"',
    '@app.post("/api/downloader/configure"',
    '@app.post("/api/downloader/pause/{torrent_hash}"',
    '@app.post("/api/downloader/resume/{torrent_hash}"',
    '@app.delete("/api/downloader/delete/{torrent_hash}"',
    '@app.post("/api/downloads/{torrent_hash}/selective"',
    '@app.post("/api/releases/{release_id}/download"',
    '@app.get("/api/releases/{release_id}/state")',
    '@app.post("/api/downloads/torbox/poll")',
    '@app.post("/api/downloader/torbox/test")',
    '@app.get("/audiobooks"',
    '@app.get("/settings"',
    '@app.post("/api/settings")',
    '@app.get("/series"',
    '@app.get("/discovery"',
    '@app.post("/api/calibre/sync")',
    '@app.get("/api/calibre/test")',
    '@app.post("/api/series/{series_id}/toggle")',
    '@app.post("/api/series/{series_id}/link")',
    '@app.post("/api/series/{series_id}/backfill")',
    '@app.post("/api/series/backfill")',
    '@app.post("/api/series/{series_id}/fetch-info")',
    '@app.post("/api/discovery/track")',
    '@app.post("/api/discovery/blacklist")',
    '@app.get("/api/calibre/series")',
    '@app.post("/api/series/{series_id}/match-calibre")',
    '@app.post("/api/discovery/match")',
    '@app.post("/api/series/{series_id}/refresh-metadata")',
    '@app.get("/api/series/aliases")',
    '@app.delete("/api/series/aliases/{override_id}")',
)

# Decorator count right after the series-identity feature landed; it may only grow.
MIN_ROUTE_DECORATORS = 97

_TEMP_COUNTER = 0


def _make_temp_dir(label):
    """Create a unique temp dir under the system TEMP (mkdtemp is unusable here)."""
    global _TEMP_COUNTER
    _TEMP_COUNTER += 1
    path = os.path.join(
        tempfile.gettempdir(),
        "noveltrax-settings-api-%d-%d-%s" % (os.getpid(), _TEMP_COUNTER, label),
    )
    os.makedirs(path, exist_ok=True)
    return path


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
    segment = ast.get_source_segment(source, node)
    return segment or ""


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


def is_async(func_node):
    return isinstance(func_node, ast.AsyncFunctionDef)


# ---------------------------------------------------------------------------
# 1. route declarations + inventory preservation
# ---------------------------------------------------------------------------
def check_route_declarations():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    for literal, handler_name in ALL_NEW_ROUTES:
        check(literal in source, "main.py: missing the re-bind/identity route %r" % literal)
        check(
            handler_name in functions,
            "main.py: missing the re-bind/identity handler %r" % handler_name,
        )
        check(
            is_async(functions[handler_name]),
            "main.py: handler %r must be an async route" % handler_name,
        )

    decorator_count = len(
        re.findall(r'^@app\.(?:get|post|put|delete|patch)\(', source, re.MULTILINE)
    )
    check(
        decorator_count >= MIN_ROUTE_DECORATORS,
        "main.py: route decorator count dropped to %d (baseline >= %d) - a route was lost"
        % (decorator_count, MIN_ROUTE_DECORATORS),
    )


def check_preserved_routes():
    source = read_text(MAIN_PY)
    for route in PINNED_EXISTING_ROUTES:
        check(route in source, "main.py: pre-existing route literal %r was lost" % route)
    check(
        re.search(r'target\.get\("download_url"\)\s*or\s*target\.get\("link"\)', source)
        is not None,
        "main.py: the release-download URL resolution pinned by test_ui_wiring.py changed",
    )


# ---------------------------------------------------------------------------
# 2. POST /api/releases/{release_id}/rebind
# ---------------------------------------------------------------------------
def check_rebind_route():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    check("api_rebind_release" in functions, "main.py: api_rebind_release handler is missing")
    func = functions["api_rebind_release"]
    segment = function_source(source, func)

    statuses = http_status_constants(func)
    for status in (400, 404, 500):
        check(
            status in statuses,
            "main.py: rebind route never raises HTTP %s (got %r)" % (status, sorted(statuses)),
        )
    check(
        "Error" not in statuses and 422 not in statuses,
        "main.py: rebind route uses an unexpected HTTP status (%r)" % sorted(statuses),
    )

    targets = to_thread_targets(func)
    for target in (
        "_release_row",
        "_calibre_series_by_id",
        "_tracked_series_by_name",
        "task_manager.rebind_release",
    ):
        check(
            target in targets,
            "main.py: rebind route does not dispatch %r through asyncio.to_thread (got %r)"
            % (target, sorted(targets)),
        )

    for marker in (
        'mode == "different_series"',
        'mode not in ("bind", "new_title", "detach")',
        "mode must be one of: bind, new_title, detach",
        'mode=bind requires calibre_series_id or target_name',
        "Unknown Calibre series id",
        "remember",
        'actor="user"',
        "ValueError",
        "KeyError",
        "LookupError",
        'summary.setdefault("status", "success")',
    ):
        check(
            marker in segment,
            "main.py: rebind route is missing the validation/mapping marker %r" % marker,
        )

    check(
        segment.count("asyncio.to_thread") >= 3,
        "main.py: rebind route performs blocking work outside asyncio.to_thread",
    )
    check(
        "HTTPException(status_code=400" in segment and "HTTPException(status_code=404" in segment,
        "main.py: rebind route is missing its 400/404 HTTPException mapping",
    )


# ---------------------------------------------------------------------------
# 3. POST /api/series/{series_id}/reset-identity
# ---------------------------------------------------------------------------
def check_reset_identity_route():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    check("api_reset_series_identity" in functions,
          "main.py: api_reset_series_identity handler is missing")
    func = functions["api_reset_series_identity"]
    segment = function_source(source, func)

    statuses = http_status_constants(func)
    for status in (400, 404, 500):
        check(
            status in statuses,
            "main.py: reset-identity route never raises HTTP %s (got %r)"
            % (status, sorted(statuses)),
        )

    targets = to_thread_targets(func)
    for target in ("_find_series_row", "task_manager.reset_series_identity"):
        check(
            target in targets,
            "main.py: reset-identity route does not dispatch %r through asyncio.to_thread"
            % target,
        )

    for marker in (
        '"aliases_removed"',
        '"releases_unpinned"',
        '"display_cleared"',
        '"locked_cleared"',
        '"reconciled"',
        '"linked"',
        '"message"',
    ):
        check(
            marker in segment,
            "main.py: reset-identity response is missing the contract key %r" % marker,
        )


# ---------------------------------------------------------------------------
# 4. GET /api/series/identity + GET /api/releases/{id}/series-identity + unpin
# ---------------------------------------------------------------------------
def check_series_identity_overview_route():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    check("api_series_identity_overview" in functions,
          "main.py: api_series_identity_overview handler is missing")
    func = functions["api_series_identity_overview"]
    segment = function_source(source, func)

    check(
        "task_manager" not in segment or "asyncio.to_thread" in segment,
        "main.py: identity overview performs a blocking manager call synchronously",
    )
    check(
        "db.get_series_identity_overview" in to_thread_targets(func),
        "main.py: identity overview does not dispatch db.get_series_identity_overview "
        "through asyncio.to_thread",
    )
    check(
        not http_status_constants(func),
        "main.py: identity overview must never raise (contract: empty list on failure)",
    )
    for marker in ('"status": "success"', '"series"', '"count"'):
        check(marker in segment, "main.py: identity overview response is missing %r" % marker)
    check(
        'limit: int = Query(100, ge=1, le=500)' in source,
        "main.py: identity overview limit is not clamped to 1..500",
    )


def check_release_identity_route():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    check("api_release_series_identity" in functions,
          "main.py: api_release_series_identity handler is missing")
    func = functions["api_release_series_identity"]
    segment = function_source(source, func)

    check(
        "db.get_release_series_identity" in to_thread_targets(func),
        "main.py: release series-identity does not dispatch db.get_release_series_identity "
        "through asyncio.to_thread",
    )
    statuses = http_status_constants(func)
    check(404 in statuses, "main.py: release series-identity does not 404 an unknown release")
    check(500 in statuses, "main.py: release series-identity does not map unexpected DB errors")
    check(
        'payload["status"] = "success"' in segment,
        "main.py: release series-identity payload does not enforce status=success",
    )
    check(
        'payload["release_status"] = payload.pop("status", "")' in segment,
        "main.py: release series-identity does not preserve the release status separately "
        "from the API envelope status",
    )


def check_unpin_route():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    check("api_unpin_release_series" in functions,
          "main.py: api_unpin_release_series handler is missing")
    func = functions["api_unpin_release_series"]
    segment = function_source(source, func)

    targets = to_thread_targets(func)
    for target in ("_release_row", "db.unpin_release_series"):
        check(
            target in targets,
            "main.py: unpin route does not dispatch %r through asyncio.to_thread" % target,
        )
    statuses = http_status_constants(func)
    check(404 in statuses, "main.py: unpin route does not 404 unknown/non-pinned releases")
    check(500 in statuses, "main.py: unpin route does not map unexpected DB errors")
    check('"unpinned": True' in segment,
          "main.py: unpin response does not return {unpinned: true}")


# ---------------------------------------------------------------------------
# 5. GET /api/series (tracked-series picker)
# ---------------------------------------------------------------------------
def check_tracked_series_picker_route():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    check("api_tracked_series_picker" in functions,
          "main.py: api_tracked_series_picker handler is missing")
    func = functions["api_tracked_series_picker"]
    segment = function_source(source, func)

    check(
        "db.get_all_series" in to_thread_targets(func),
        "main.py: tracked picker does not dispatch db.get_all_series through asyncio.to_thread",
    )
    check(
        not http_status_constants(func),
        "main.py: tracked picker must never raise (contract: empty list on failure)",
    )
    for marker in (
        '"id": series_id',
        '"name": name',
        '"display_title": display_title',
        '"authors": authors',
        '"calibre_series_id"',
        '"identity_locked": _coerce_bool',
        '"book_count"',
        '"owned_volumes"',
        '"tracked": True',
        "_series_match_normalize",
        '"status": "success", "series": [], "count": 0',
    ):
        check(
            marker in segment,
            "main.py: tracked picker is missing the contract marker %r" % marker,
        )
    check(
        'limit: int = Query(25, ge=1, le=500)' in source,
        "main.py: tracked picker limit is not clamped to 1..500",
    )


# ---------------------------------------------------------------------------
# 6. shared helpers
# ---------------------------------------------------------------------------
def check_rebind_helpers():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    for helper in ("_coerce_bool", "_coerce_volume_list", "_release_row", "_tracked_series_by_name"):
        check(helper in functions, "main.py: missing re-bind helper %r" % helper)

    bool_segment = function_source(source, functions["_coerce_bool"])
    for literal in ('"1"', '"true"', '"yes"', '"0"', '"false"', '"no"'):
        check(
            literal in bool_segment,
            "main.py: _coerce_bool does not accept the %s spelling" % literal,
        )

    volumes_segment = function_source(source, functions["_coerce_volume_list"])
    check('json.loads' in volumes_segment,
          "main.py: _coerce_volume_list does not parse owned_volumes_json strings")
    check('isinstance(value, list)' in volumes_segment,
          "main.py: _coerce_volume_list does not pass through list values")

    lookup_segment = function_source(source, functions["_tracked_series_by_name"])
    check('_series_match_normalize' in lookup_segment,
          "main.py: _tracked_series_by_name does not compare normalized names")
    check("db.get_all_series" in lookup_segment,
          "main.py: _tracked_series_by_name does not read tracked series rows")


# ---------------------------------------------------------------------------
# 7. settings validation (§6.1) + startup log level
# ---------------------------------------------------------------------------
def check_settings_validation():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    check("_prepare_settings_payload" in functions,
          "main.py: _prepare_settings_payload helper is missing")
    segment = function_source(source, functions["_prepare_settings_payload"])

    for marker in (
        'zoneinfo.ZoneInfo',
        '"app_timezone"',
        "app_timezone must be a valid IANA timezone name",
        '"log_level"',
        '"DEBUG", "INFO", "WARNING", "ERROR"',
        "log_level must be one of DEBUG, INFO, WARNING, ERROR",
        '"activity_retention_days"',
        '"activity_max_rows"',
        "HTTPException(status_code=400",
    ):
        check(
            marker in segment,
            "main.py: settings validation is missing the marker %r" % marker,
        )
    check(
        '_clamp_numeric_setting(clean, "activity_retention_days", 0, 3650, integer=True)'
        in segment,
        "main.py: activity_retention_days is not clamped to 0..3650",
    )
    check(
        '_clamp_numeric_setting(clean, "activity_max_rows", 0, 1000000, integer=True)'
        in segment,
        "main.py: activity_max_rows is not clamped to 0..1000000",
    )

    check("zoneinfo" in source and "import zoneinfo" in source,
          "main.py: zoneinfo is not imported for timezone validation")
    check("_apply_configured_log_level" in functions,
          "main.py: _apply_configured_log_level helper is missing")
    level_segment = function_source(source, functions["_apply_configured_log_level"])
    for marker in ('"log_level"', '"INFO"', "logging.getLogger().setLevel(level)"):
        check(
            marker in level_segment,
            "main.py: startup log-level application is missing the marker %r" % marker,
        )
    lifespan_segment = function_source(source, functions["lifespan"])
    check(
        "_apply_configured_log_level()" in lifespan_segment,
        "main.py: the persisted log_level is not applied at app startup (lifespan)",
    )


# ---------------------------------------------------------------------------
# 8. diagnostics module (§6.2) - static
# ---------------------------------------------------------------------------
def check_diagnostics_module_static():
    check(os.path.isfile(DIAGNOSTICS_PY), "backend/app/diagnostics.py does not exist")
    source = read_text(DIAGNOSTICS_PY)
    for function_name in (
        "def storage_health(",
        "def network_diagnostics(",
        "def series_attribution_audit(",
        "def format_bytes(",
    ):
        check(function_name in source, "diagnostics.py: missing %r" % function_name)
    for marker in (
        "graphql.anilist.co",
        "kitsu.io",
        "nyaa.si",
        "jnovels.com",
        "safe_http_get",
        "status_code",
        "< 500",
        "shutil.disk_usage",
        "os.access",
        "tempfile.mkstemp",
        "get_series_attribution_rows",
        "get_series_identity_rows",
        "get_series_alias_overrides",
        "resolve_series_title",
        "extract_series_and_volume",
        "clean_feed_title",
        "series_pinned",
        "IGNORED",
        "BLACKLISTED",
        "degraded",
    ):
        check(marker in source, "diagnostics.py: missing the contract marker %r" % marker)


# ---------------------------------------------------------------------------
# 9. maintenance/diagnostics routes (§6.3) - static
# ---------------------------------------------------------------------------
def check_maintenance_routes():
    source, tree = parse_main()
    functions = functions_by_name(tree)

    expected_targets = {
        "api_storage_health": {"diagnostics.storage_health"},
        "api_network_diagnostics": {"diagnostics.network_diagnostics"},
        "api_prune_activity_logs": {"db.prune_activity_logs"},
        "api_database_stats": {"db.database_stats"},
        "api_database_backup": {"_create_database_backup"},
        "api_vacuum_database": {"db.vacuum_database"},
        "api_series_attribution_audit": {"diagnostics.series_attribution_audit"},
    }
    for handler_name, expected in expected_targets.items():
        check(handler_name in functions,
              "main.py: %s handler is missing" % handler_name)
        targets = to_thread_targets(functions[handler_name])
        for target in expected:
            check(
                target in targets,
                "main.py: %s does not dispatch %r through asyncio.to_thread (got %r)"
                % (handler_name, target, sorted(targets)),
            )

    check("from app import diagnostics" in source,
          "main.py: diagnostics module is not imported")
    check("BackgroundTask" in source and "from fastapi import" in source,
          "main.py: BackgroundTask is not imported for the backup cleanup")

    # Never-raise diagnostics endpoints must degrade instead of raising.
    for handler_name in (
        "api_storage_health",
        "api_network_diagnostics",
        "api_series_attribution_audit",
    ):
        func = functions[handler_name]
        segment = function_source(source, func)
        check(
            not http_status_constants(func),
            "main.py: %s must never raise (contract: degraded payload)" % handler_name,
        )
        check(
            '"degraded"' in segment,
            "main.py: %s does not return a degraded payload on failure" % handler_name,
        )

    # Maintenance endpoints that may fail must map to HTTP 500 JSON.
    for handler_name in (
        "api_prune_activity_logs",
        "api_database_stats",
        "api_vacuum_database",
    ):
        statuses = http_status_constants(functions[handler_name])
        check(
            500 in statuses,
            "main.py: %s does not map failures to HTTP 500 (got %r)"
            % (handler_name, sorted(statuses)),
        )

    backup_segment = function_source(source, functions["api_database_backup"])
    for marker in (
        'filename = f"noveltrax-backup-{stamp}.db"',
        "FileResponse(",
        "BackgroundTask(os.remove, backup_path)",
        "application/octet-stream",
    ):
        check(marker in backup_segment,
              "main.py: database backup route is missing %r" % marker)
    backup_helper = function_source(source, functions["_create_database_backup"])
    check(
        'getattr(db, "backup_database", None)' in backup_helper
        and "source.backup(destination)" in backup_helper,
        "main.py: _create_database_backup neither uses db.backup_database nor the "
        "sqlite3 online backup API fallback",
    )

    stats_segment = function_source(source, functions["api_database_stats"])
    for marker in ("PRAGMA quick_check", '"integrity"', "asyncio.to_thread(_quick_check)"):
        check(marker in stats_segment,
              "main.py: database stats route is missing %r" % marker)

    prune_segment = function_source(source, functions["api_prune_activity_logs"])
    for marker in (
        '"removed_by_age"',
        '"removed_by_rows"',
        '"removed"',
        '"remaining"',
        "LOGS_PRUNED",
        "activity_retention_days",
        "activity_max_rows",
    ):
        check(marker in prune_segment,
              "main.py: log-prune route is missing %r" % marker)

    vacuum_segment = function_source(source, functions["api_vacuum_database"])
    for marker in ('"before_bytes"', '"after_bytes"', '"integrity"', "format_bytes"):
        check(marker in vacuum_segment,
              "main.py: vacuum route is missing %r" % marker)

    attribution_segment = function_source(source, functions["api_series_attribution_audit"])
    for marker in ('"checked"', '"count"', '"items"', "limit: int = Query(200, ge=1, le=1000)"):
        check(marker in attribution_segment or marker in source,
              "main.py: attribution route is missing %r" % marker)


# ---------------------------------------------------------------------------
# 10. diagnostics helpers - behaviour (stdlib + temp DB/fakes, no network)
# ---------------------------------------------------------------------------
def check_diagnostics_behaviour():
    # format_bytes
    for value, expected in (
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1048576, "1.0 MB"),
        (5 * 1024 ** 3, "5.0 GB"),
    ):
        check(
            diagnostics.format_bytes(value) == expected,
            "diagnostics.format_bytes(%r) expected %r, got %r"
            % (value, expected, diagnostics.format_bytes(value)),
        )
    check(diagnostics.format_bytes("bogus") == "0 B",
          "diagnostics.format_bytes must contain unparsable input")
    check(diagnostics.format_bytes(None) == "0 B",
          "diagnostics.format_bytes(None) must be '0 B'")

    # storage_health against a real temp Database + temp dirs.
    data_dir = _make_temp_dir("data")
    calibre_dir = _make_temp_dir("calibre")
    downloads_dir = _make_temp_dir("downloads")
    db = None
    try:
        db = Database(os.path.join(data_dir, "noveltrax.db"))
        health = diagnostics.storage_health(db, calibre_dir, data_dir, downloads_dir)
        check(isinstance(health, dict), "storage_health must return a dict")
        check(health.get("data_dir", {}).get("ok") is True,
              "storage_health: writable data dir is not ok")
        check(health.get("downloads_dir", {}).get("ok") is True,
              "storage_health: writable downloads dir is not ok")
        check(health.get("database", {}).get("ok") is True,
              "storage_health: database section is not ok")
        counts = health.get("database", {}).get("row_counts", {})
        for key in ("releases", "series_tracking", "activity_logs", "releases_new"):
            check(key in counts, "storage_health: database row_counts is missing %r" % key)
        check(health.get("calibre", {}).get("ok") is False,
              "storage_health: a missing Calibre library must be reported as not ok")
        check(health.get("status") in ("success", "degraded"),
              "storage_health: status must be success/degraded")
        check("checked_at" in health, "storage_health: missing checked_at")

        # Never raises for missing paths / a broken DB.
        broken = diagnostics.storage_health(
            db, "", "/definitely/not/a/dir-a", "/definitely/not/a/dir-b"
        )
        check(isinstance(broken, dict) and broken.get("status") == "degraded",
              "storage_health: missing dirs must degrade, not raise")
        check(broken.get("data_dir", {}).get("ok") is False,
              "storage_health: missing data_dir must be not ok")
    finally:
        for path in (data_dir, calibre_dir, downloads_dir):
            shutil.rmtree(path, ignore_errors=True)

    # network_diagnostics with a patched fetch (no real network).
    original_fetch = diagnostics.safe_http_get

    class _FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

    try:
        calls = []

        def fake_ok(url, **kwargs):
            calls.append((url, kwargs.get("timeout")))
            return _FakeResponse(200)

        diagnostics.safe_http_get = fake_ok
        result = diagnostics.network_diagnostics(timeout=30.0)
        check(isinstance(result, dict), "network_diagnostics must return a dict")
        check(len(result.get("results", [])) == len(diagnostics.NETWORK_TARGETS),
              "network_diagnostics must probe every provider")
        check(all(entry["ok"] for entry in result["results"]),
              "network_diagnostics: HTTP 200 must be ok")
        check(result.get("ok") is True, "network_diagnostics: overall ok must be True")
        check(result.get("timeout_seconds", 99) <= 5.0,
              "network_diagnostics must cap the timeout at 5 s")
        check(all((timeout or 0) <= 5.0 for _url, timeout in calls),
              "network_diagnostics must pass a <=5 s timeout to the fetch")
        check(any("anilist" in url for url, _t in calls)
              and any("nyaa" in url for url, _t in calls)
              and any("jnovels" in url for url, _t in calls),
              "network_diagnostics must probe AniList/Nyaa/J-Novels")

        diagnostics.safe_http_get = lambda url, **kwargs: _FakeResponse(503)
        result = diagnostics.network_diagnostics()
        check(result.get("ok") is False and not any(e["ok"] for e in result["results"]),
              "network_diagnostics: HTTP 5xx must not be ok")

        def fake_boom(url, **kwargs):
            raise RuntimeError("connection refused")

        diagnostics.safe_http_get = fake_boom
        result = diagnostics.network_diagnostics()
        check(isinstance(result, dict) and result.get("ok") is False,
              "network_diagnostics must contain fetch exceptions")
        check(all(entry.get("error") for entry in result["results"]),
              "network_diagnostics must record an error per failed provider")
    finally:
        diagnostics.safe_http_get = original_fetch

    # series_attribution_audit with a fake DB + the real parser/resolver.
    target = "Trapped in a Dating Sim: The World of Otome Games is Tough for Us, Too"
    alias_source = "Trapped in a Dating Sim: Otome Games Are Tough for Us, Too!"
    release_title = "Trapped in a Dating Sim: Otome Games Are Tough for Us, Too! Volume 1"

    class _AttributionFakeDB:
        def __init__(self, rows):
            self._rows = rows

        def get_series_identity_rows(self):
            return [{
                "id": 1,
                "name": target,
                "display_title": "",
                "calibre_series_id": 327,
                "anilist_meta_json": "",
                "match_source": "",
                "identity_locked": 0,
            }]

        def get_series_alias_overrides(self):
            return [{"id": 1, "source_name": alias_source, "target_name": target}]

        def get_series_attribution_rows(self, limit=200, **kwargs):
            return list(self._rows)[:limit]

    audit = diagnostics.series_attribution_audit(_AttributionFakeDB([
        {"id": 11, "title": release_title, "series_name": "Definitely Wrong Series",
         "status": "NEW", "series_pinned": 0},
        {"id": 12, "title": release_title, "series_name": target,
         "status": "REQUESTED", "series_pinned": 0},
        {"id": 13, "title": release_title, "series_name": "Wrong But Ignored",
         "status": "IGNORED", "series_pinned": 0},
        {"id": 14, "title": release_title, "series_name": "Wrong But Pinned",
         "status": "NEW", "series_pinned": 1},
    ]), limit=200)
    check(isinstance(audit, dict), "series_attribution_audit must return a dict")
    check(audit.get("checked") == 4,
          "series_attribution_audit: checked should count scanned rows (got %r)"
          % audit.get("checked"))
    flagged_ids = [item.get("release_id") for item in audit.get("items", [])]
    check(11 in flagged_ids,
          "series_attribution_audit: a clearly mismatched release was not flagged")
    check(13 not in flagged_ids and 14 not in flagged_ids,
          "series_attribution_audit: IGNORED/pinned releases must be skipped")
    check(audit.get("count") == len(audit.get("items", [])),
          "series_attribution_audit: count must match items")
    flagged = next(item for item in audit["items"] if item["release_id"] == 11)
    for key in (
        "release_id",
        "title",
        "series_name",
        "parsed_series",
        "suggested_series",
        "suggested_calibre_series_id",
        "pinned",
    ):
        check(key in flagged,
              "series_attribution_audit: audit item is missing %r" % key)
    check(flagged.get("pinned") is False,
          "series_attribution_audit: unpinned row reported pinned")

    capped = diagnostics.series_attribution_audit(_AttributionFakeDB([
        {"id": 21, "title": release_title, "series_name": "Wrong A", "status": "NEW"},
        {"id": 22, "title": release_title, "series_name": "Wrong B", "status": "NEW"},
    ]), limit=1)
    check(len(capped.get("items", [])) <= 1,
          "series_attribution_audit: limit is not applied to the item cap")

    empty = diagnostics.series_attribution_audit(_AttributionFakeDB([]), limit=10)
    check(empty.get("checked") == 0 and empty.get("count") == 0 and empty.get("items") == [],
          "series_attribution_audit: empty DB must return empty counters")

    class _BrokenAttributionDB:
        def get_series_identity_rows(self):
            raise RuntimeError("db down")

        def get_series_alias_overrides(self):
            raise RuntimeError("db down")

        def get_series_attribution_rows(self, **kwargs):
            raise RuntimeError("db down")

    broken_audit = diagnostics.series_attribution_audit(_BrokenAttributionDB())
    check(isinstance(broken_audit, dict) and broken_audit.get("checked") == 0,
          "series_attribution_audit must contain DB failures (never raise)")


# ---------------------------------------------------------------------------
# 11. §6.5 debug-action: safe clear-metadata action
# ---------------------------------------------------------------------------
def check_debug_action_clear_metadata():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    check("api_debug_action" in functions, "main.py: api_debug_action handler is missing")
    check(
        '@app.post("/api/debug/action")' in source,
        "main.py: the existing POST /api/debug/action route was removed",
    )
    segment = function_source(source, functions["api_debug_action"])
    for literal in DEBUG_ACTION_LITERALS:
        check(
            '"%s"' % literal in segment,
            "main.py: debug-action no longer handles the %r action (existing actions "
            "must stay, clear-metadata must be added)" % literal,
        )
    check(
        "db.clear_novel_metadata_cache()" in segment,
        "main.py: the clear-metadata debug action does not call db.clear_novel_metadata_cache()",
    )
    check(
        '"status": "success"' in segment and "Cleared " in segment,
        "main.py: the clear-metadata debug action does not return a success message",
    )
    # The four pre-existing branches must still be present verbatim.
    for marker in (
        'action == "clear-descriptions"',
        'action == "clear-discovery"',
        'action == "clear-thumbnails"',
        'action == "purge-pdf"',
    ):
        check(marker in segment,
              "main.py: the pre-existing debug-action branch %r was changed" % marker)
    check(
        '{\"status\": \"error\", \"message\": f\"Unknown action: {action}\"}' in segment
        or 'f"Unknown action: {action}"' in segment,
        "main.py: debug-action lost its unknown-action error response",
    )

    # DB-level: the underlying cache purge really removes rows.
    data_dir = _make_temp_dir("metadata-clear")
    try:
        db = Database(os.path.join(data_dir, "noveltrax.db"))
        db.ensure_metadata_cache_table()
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO novel_metadata_cache (query_key, title) VALUES ('k1', 'Title');"
            )
            conn.commit()
        removed = db.clear_novel_metadata_cache()
        check(removed == 1,
              "Database.clear_novel_metadata_cache() should clear 1 seeded row (got %r)"
              % removed)
        check(db.clear_novel_metadata_cache() == 0,
              "Database.clear_novel_metadata_cache() should report 0 on an empty cache")
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def main():
    check_route_declarations()
    check_preserved_routes()
    check_rebind_route()
    check_reset_identity_route()
    check_series_identity_overview_route()
    check_release_identity_route()
    check_unpin_route()
    check_tracked_series_picker_route()
    check_rebind_helpers()
    check_settings_validation()
    check_diagnostics_module_static()
    check_maintenance_routes()
    check_diagnostics_behaviour()
    check_debug_action_clear_metadata()

    print(
        "settings-features API guard PASSED: 6 re-bind/series-identity routes + 7 "
        "settings/diagnostics/maintenance routes, payload validation (mode/target/"
        "timezone/log-level/retention) mapped to 400/404/500, blocking calls "
        "dispatched via asyncio.to_thread, diagnostics module (storage/network/"
        "attribution) behaviour-tested with temp DBs/fakes and no network, startup "
        "log-level application, the §6.5 debug-action clear-metadata purge, and the "
        "full pre-existing route inventory preserved"
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
