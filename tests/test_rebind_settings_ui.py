#!/usr/bin/env python3
"""Static source-level guard for the release re-bind / series-identity / settings-features UI.

Why static and not a render test: the interpreter used for the repo suites has neither
``jinja2`` nor ``fastapi`` installed, so the templates cannot be rendered and ``main.py``
cannot be imported.  This module reads the templates, ``README.md`` and the other
UI-guarded templates as text and proves the CONTRACT_V2_REBIND.md §5.1, §5.2 and §6.4
deliverables are wired:

1. ``base.html`` ships the shared hidden ``#release-rebind-modal`` (mode radios
   bind/new_title/detach, tracked+Calibre search, default-unchecked pin checkbox,
   new-title input, apply/cancel/error, Escape/backdrop close and focus restore) plus
   the global JS helpers ``openReleaseRebindModal`` (accepts a button element *and* a
   plain ``{releaseId, releaseTitle, seriesName}`` object), ``closeReleaseRebindModal``,
   ``setReleaseRebindMode``, ``searchReleaseRebindSeries``, ``renderReleaseRebindResults``,
   ``selectReleaseRebindTarget``, ``applyReleaseRebind``, ``refreshReleaseRebindState``
   and the unpin action, posting ``/api/releases/{id}/rebind`` and
   ``/api/releases/{id}/unpin``.
2. Both release partials expose a compact ``.release-rebind-btn`` (data attributes +
   ``openReleaseRebindModal(this)``) and a ``.release-pinned-badge`` when
   ``r.series_pinned`` - while keeping every pinned download/ignore/status/state-chip
   literal used by the existing UI-wiring suites.
3. ``settings.html`` adds the Display & Logging card (timezone + datalist, log level,
   activity retention/max rows, prune-now), the Storage & Diagnostics card (storage
   health, network diagnostics + copy JSON, database backup download, VACUUM, prune),
   the Series identity & overrides card (search, reset rows, global alias list +
   delete, attribution audit with Re-bind), and the unsaved-changes guard
   (``#settings-dirty-badge``, ``beforeunload``, cleared by ``saveSettings()``), with
   ``textContent``/``createElement``-only rendering of server data.
4. ``README.md`` documents re-bind/different-series + pinned semantics, the per-series
   reset, timezone/log-level/activity-retention, storage health, network diagnostics,
   database backup/VACUUM, alias management and the attribution audit.
5. Every edited template has balanced Jinja block/expression/comment delimiters, and
   the pinned literals of the other UI-guarded templates are still present.

Bare ``assert`` is never used (Calibre runs with ``sys.flags.optimize == 2`` and
compiles bare asserts out, which would make this guard vacuous); an explicit
``check()`` helper raises ``AssertionError`` instead.

Stdlib only: os, re, sys.  No third-party imports, no network, no database.

Usage::

    & $py tests\\run_tests.py rebind_settings_ui
    & $py tests\\test_rebind_settings_ui.py
"""

import os
import re
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "backend", "app", "templates")

BASE_TEMPLATE = os.path.join(TEMPLATES_DIR, "base.html")
SETTINGS_TEMPLATE = os.path.join(TEMPLATES_DIR, "settings.html")
RELEASES_TEMPLATE = os.path.join(TEMPLATES_DIR, "releases.html")
FLAT_PARTIAL = os.path.join(TEMPLATES_DIR, "partials", "flat_release_row_chunk.html")
SERIES_PARTIAL = os.path.join(TEMPLATES_DIR, "partials", "series_card_chunk.html")
DISCOVERY_PARTIAL = os.path.join(TEMPLATES_DIR, "partials", "discovery_card_chunk.html")
DISCOVERY_TEMPLATE = os.path.join(TEMPLATES_DIR, "discovery.html")
SERIES_TEMPLATE = os.path.join(TEMPLATES_DIR, "series.html")
DOWNLOADS_TEMPLATE = os.path.join(TEMPLATES_DIR, "downloads.html")
SERIES_TEMPLATE = os.path.join(TEMPLATES_DIR, "series.html")
LOGS_TEMPLATE = os.path.join(TEMPLATES_DIR, "logs.html")
README_FILE = os.path.join(PROJECT_ROOT, "README.md")

# --- CONTRACT §5.1: base.html re-bind modal ---------------------------------
REBIND_MODAL_IDS = (
    "release-rebind-modal",
    "release-rebind-current",
    "release-rebind-search",
    "release-rebind-results",
    "release-rebind-remember",
    "release-rebind-new-title",
    "release-rebind-apply",
    "release-rebind-cancel",
    "release-rebind-error",
    "release-rebind-unpin",
    "release-rebind-selected",
    "release-rebind-close",
    "release-rebind-pin-row",
)

REBIND_MODE_VALUES = ("bind", "new_title", "detach")

REBIND_JS_FUNCTIONS = (
    "function openReleaseRebindModal(",
    "function closeReleaseRebindModal(",
    "function setReleaseRebindMode(",
    "function searchReleaseRebindSeries(",
    "function renderReleaseRebindResults(",
    "function selectReleaseRebindTarget(",
    "async function applyReleaseRebind(",
    "async function refreshReleaseRebindState(",
    "async function unpinReleaseRebind(",
    "function _releaseRebindError(",
)

REBIND_ENDPOINTS = (
    "/api/releases/${releaseId}/rebind",
    "/api/releases/${releaseId}/series-identity",
    "/api/releases/${releaseId}/unpin",
    "/api/series?search=",
    "/api/calibre/series",
)

REBIND_HELPER_TEXTS = (
    "Re-bind series",
    "Bind to an existing series",
    "Re-index under a new title &mdash; not this series and not any previous series",
    "Detach as a new standalone series",
    "Creates a brand-new series. It will not be linked to the current series or any previous one.",
    "Only this release (pin)",
)

# --- CONTRACT §5.1: partials ------------------------------------------------
REBIND_BUTTON_CLASS = "release-rebind-btn"
PINNED_BADGE_CLASS = "release-pinned-badge"
REBIND_BUTTON_MARKERS = (
    'class="release-rebind-btn',
    'data-release-id="{{ r.id }}"',
    'data-release-title="{{ r.title }}"',
    'data-series-name="{{ r.series_name or \'\' }}"',
    "onclick=\"openReleaseRebindModal(this)\"",
    'title="Re-bind series / mark different series"',
    "git-branch",
)
PINNED_BADGE_MARKERS = (
    'class="release-pinned-badge',
    'title="Series manually re-bound"',
    "{% if r.series_pinned %}",
)

# Existing literals that other suites pin and that must survive in the partials.
FLAT_PRESERVED = (
    'class="release-row',
    "data-lucide=\"download\"",
    "downloadRelease({{ r.id }}, this)",
    'data-release-id="{{ r.id }}"',
    'title="No torrent or magnet download URL available for this release"',
    "r.download_url",
    "r.link",
    "release-state-chip",
    "ignoreReleaseGlobal({{ r.id }}, this)",
    "unignoreReleaseGlobal({{ r.id }}, this)",
    "{% set dl_target",
)
SERIES_PRESERVED = (
    'class="series-card',
    'loading="lazy"',
    'decoding="async"',
    "cover-placeholder",
    "downloadRelease({{ r.id }}, this)",
    "release-state-chip",
    "ignoreReleaseGlobal({{ r.id }}, this)",
    "unignoreReleaseGlobal({{ r.id }}, this)",
)
BASE_PRESERVED = (
    "async function downloadRelease(releaseId, btn = null)",
    "/api/releases/${releaseId}/download",
    "/api/releases/${releaseId}/state",
    "/api/releases/${releaseId}/ignore",
    "/api/releases/${releaseId}/unignore",
    "async function ignoreReleaseGlobal(",
    "async function unignoreReleaseGlobal(",
    "async function backfillSeriesGlobal(",
    "function showToast(message, type = 'success')",
    'id="image-modal"',
    "function closeImageModal(",
    'id="page-progress-bar"',
    "const ProgressBar =",
    'href="/downloads"',
    'href="/settings"',
    'href="/audiobooks"',
)

# --- CONTRACT §5.2 / §6.4.3: settings.html series identity card -------------
SETTINGS_IDENTITY_IDS = (
    "series-identity-search",
    "series-identity-results",
    "series-alias-list",
    "series-attribution-result",
    "btn-series-attribution-audit",
    "series-identity-refresh",
    "btn-refresh-series-aliases",
)

SETTINGS_IDENTITY_JS = (
    "async function loadSeriesIdentityOverview(",
    "function renderSeriesIdentityResults(",
    "async function resetSeriesIdentity(",
    "function debounceSeriesIdentitySearch(",
    "async function loadSeriesAliasOverrides(",
    "async function deleteSeriesAlias(",
    "async function runSeriesAttributionAudit(",
)

SETTINGS_IDENTITY_ENDPOINTS = (
    "/api/series/identity",
    "/api/series/${id}/reset-identity",
    "/api/series/aliases",
    "/api/maintenance/series-attribution",
)

SETTINGS_IDENTITY_RESET_BADGE = "series-identity-reset-btn"
SETTINGS_ALIAS_DELETE_BADGE = "series-alias-delete-btn"

SERIES_IDENTITY_HELPER_TEXT = (
    "Reset forgets manual/learned aliases, AniList display data, manual locks and "
    "per-release pins for this series, then lets automatic matching take over again. "
    "Release titles are not changed."
)

# --- CONTRACT §6.4.1: Display & Logging card --------------------------------
DISPLAY_IDS = (
    "setting-timezone",
    "timezone-suggestions",
    "setting-log-level",
    "setting-activity-retention",
    "setting-activity-max-rows",
    "btn-prune-logs",
    "activity-prune-result",
)

TIMEZONE_SUGGESTIONS = (
    "Europe/Copenhagen",
    "Europe/London",
    "America/New_York",
    "America/Los_Angeles",
    "Asia/Tokyo",
    "UTC",
)

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# --- CONTRACT §6.4.2: Storage & Diagnostics card ----------------------------
STORAGE_IDS = (
    "btn-storage-health",
    "storage-health-result",
    "btn-network-diagnostics",
    "network-diagnostics-result",
    "btn-copy-diagnostics",
    "btn-download-backup",
    "btn-vacuum-db",
    "database-maintenance-result",
)

STORAGE_JS = (
    "async function loadStorageHealth(",
    "async function runNetworkDiagnostics(",
    "async function copyNetworkDiagnostics(",
    "function downloadDatabaseBackup(",
    "async function vacuumDatabase(",
)

STORAGE_ENDPOINTS = (
    "/api/storage/health",
    "/api/diagnostics/network",
    "/api/maintenance/backup",
    "/api/maintenance/database/vacuum",
    "/api/maintenance/logs/prune",
)

# --- CONTRACT §6.4.4: unsaved-changes guard ---------------------------------
DIRTY_MARKERS = (
    'id="settings-dirty-badge"',
    'class="hidden',
    'id="settings-form"',
    "beforeunload",
    "function _markSettingsDirty(",
    "function _clearSettingsDirty(",
    "_clearSettingsDirty();",
)

SETTINGS_PAYLOAD_NEW_KEYS = (
    "app_timezone",
    "log_level",
    "activity_retention_days",
    "activity_max_rows",
    "webhook_new_request_notifications",
    "webhook_acquired_notifications",
)

SETTINGS_PAYLOAD_PINNED_KEYS = (
    "calibre_path",
    "calibre_sync_interval_minutes",
    "feed_check_interval_minutes",
    "backfill_max_pages",
    "backfill_delay_seconds",
    "backfill_include_nyaa",
    "track_official_only",
    "feed_catchup_enabled",
    "feed_catchup_max_pages",
    "discord_webhook_url",
    "apprise_url",
    "downloader_engine",
    "torbox_enabled",
    "torbox_api_key",
    "torbox_delete_after_download",
    "torbox_strict_volume_selection",
    "torbox_delete_on_failure",
    "torbox_download_concurrency",
    "torbox_parallel_chunks",
    "torbox_max_download_gb",
    "torbox_poll_seconds",
    "torbox_max_concurrent",
    "torbox_timeout_hours",
    "downloads_dir",
)

# Existing settings ids pinned by the TorBox / Nyaa-backfill / feed-catchup suites.
SETTINGS_PRESERVED_IDS = (
    "setting-calibre-path",
    "setting-calibre-interval",
    "setting-feed-interval",
    "setting-feed-catchup-pages",
    "setting-feed-catchup-enabled",
    "setting-backfill-pages",
    "setting-backfill-include-nyaa",
    "setting-backfill-delay",
    "setting-official-only",
    "btn-reset-nyaa-backfill",
    "setting-downloader-engine",
    "setting-torbox-enabled",
    "setting-torbox-api-key",
    "setting-torbox-delete-after",
    "setting-torbox-strict-selection",
    "setting-torbox-delete-on-failure",
    "setting-torbox-download-concurrency",
    "setting-torbox-parallel-chunks",
    "setting-torbox-max-download-gb",
    "setting-torbox-poll-seconds",
    "setting-torbox-max-concurrent",
    "setting-torbox-timeout-hours",
    "setting-downloads-dir",
    "btn-test-torbox",
    "torbox-test-result",
    "btn-clear-desc",
    "btn-clear-discovery",
    "btn-clear-thumbs",
    "btn-purge-pdf",
)

SETTINGS_PRESERVED_JS = (
    "async function testCalibreConnection(",
    "async function testSingleWebhook(",
    "async function testTorboxConnection(",
    "async function saveSettings(",
    "async function debugAction(",
    "async function resetNyaaBackfillCursor(",
)

# --- README ---------------------------------------------------------------
# --- CONTRACT §6.5: webhook toggles, local-time logs, metadata-cache clear ---
NOTIFICATION_IDS = (
    "setting-webhook-new-request",
    "setting-webhook-acquired",
)

NOTIFICATION_HELP_MARKERS = (
    "New-request notifications",
    "Acquired notifications",
    "webhook_new_request_notifications",
    "webhook_acquired_notifications",
)

MAINTENANCE_METADATA_MARKERS = (
    'id="btn-clear-metadata"',
    "Clear Metadata Cache",
    "AniList/Kitsu",
    "onclick=\"debugAction('clear-metadata')\"",
    "action === 'clear-metadata'",
    "releases, requests and settings are untouched",
)

LOGS_MARKERS = (
    '{% extends "base.html" %}',
    "{% for log in logs %}",
    "log.event_type",
    "log.message",
    "log.details",
    "No activity logs recorded yet.",
    '|localtime("%Y-%m-%d %H:%M:%S")',
    'title="{{ log.created_at }}"',
)

README_MARKERS = (
    "Re-bind",
    "Different Series",
    "release-rebind-btn",
    "/api/releases/{release_id}/rebind",
    "/api/releases/{release_id}/unpin",
    "release-pinned-badge",
    "Series identity & overrides",
    "/api/series/{series_id}/reset-identity",
    "Attribution audit",
    "/api/maintenance/series-attribution",
    "app_timezone",
    "log_level",
    "activity_retention_days",
    "activity_max_rows",
    "every 6 hours",
    "/api/storage/health",
    "/api/diagnostics/network",
    "/api/maintenance/backup",
    "/api/maintenance/database/vacuum",
    "DELETE /api/series/aliases/{override_id}",
    "Unsaved changes",
    "tests/test_rebind_settings_ui.py",
    "webhook_new_request_notifications",
    "webhook_acquired_notifications",
    "localtime",
    "Clear Metadata Cache",
    "Deferred settings features",
    "log-file rotation",
    "500 ms",
    "aborts stale in-flight requests",
)


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


def isolate(source, start_marker, end_marker, label):
    """Return the source slice between two markers, failing when not found."""
    start = source.find(start_marker)
    check(start != -1, "%s: missing start marker %r" % (label, start_marker))
    end = source.find(end_marker, start + len(start_marker))
    check(end != -1, "%s: missing end marker %r" % (label, end_marker))
    return source[start:end]


# ---------------------------------------------------------------------------
# 1. base.html re-bind modal
# ---------------------------------------------------------------------------
def check_rebind_modal_markup():
    source = read_text(BASE_TEMPLATE)

    check('id="release-rebind-modal"' in source, "base.html: missing #release-rebind-modal")
    for element_id in REBIND_MODAL_IDS:
        check(
            'id="%s"' % element_id in source,
            "base.html: missing the re-bind modal element id %r" % element_id,
        )
    check(
        'onclick="closeReleaseRebindModal()"' in source,
        "base.html: the re-bind modal has no backdrop close handler",
    )
    for mode in REBIND_MODE_VALUES:
        check(
            'name="release-rebind-mode" value="%s"' % mode in source,
            "base.html: missing the re-bind mode radio %r" % mode,
        )
    check(
        "checked onchange=\"setReleaseRebindMode('bind')\"" in source
        or 'value="bind" checked' in source,
        "base.html: the re-bind modal does not default to bind mode",
    )
    remember_start = source.find('id="release-rebind-remember"')
    check(remember_start != -1, "base.html: missing the #release-rebind-remember checkbox")
    check(
        "checked" not in source[max(0, remember_start - 120):remember_start + 220],
        "base.html: the only-this-release pin checkbox must default unchecked",
    )
    check(
        'id="release-rebind-new-title"' in source,
        "base.html: missing the #release-rebind-new-title input",
    )
    for helper_text in REBIND_HELPER_TEXTS:
        check(
            helper_text in source,
            "base.html: missing the re-bind helper text %r" % helper_text,
        )


def check_rebind_modal_js():
    source = read_text(BASE_TEMPLATE)
    for function_name in REBIND_JS_FUNCTIONS:
        check(
            function_name in source,
            "base.html: missing the re-bind JS function %r" % function_name,
        )
    for endpoint in REBIND_ENDPOINTS:
        check(
            endpoint in source,
            "base.html: the re-bind JS does not use the endpoint %r" % endpoint,
        )
    check(
        "input[name=\"release-rebind-mode\"]" in source,
        "base.html: setReleaseRebindMode() does not sync the mode radios",
    )
    search_body = isolate(
        source,
        "function searchReleaseRebindSeries(",
        "function renderReleaseRebindResults(",
        "base.html searchReleaseRebindSeries",
    )
    check(
        "}, 500);" in search_body,
        "base.html: the re-bind series search is not debounced by 500 ms",
    )
    check(
        "}, 250);" not in search_body,
        "base.html: the re-bind series search still uses the old 250 ms debounce",
    )
    check(
        "let _releaseRebindAbortController = null;" in source
        and "let _releaseRebindSearchSeq = 0;" in source
        and "let _releaseRebindLastQuery = null;" in source,
        "base.html: the re-bind search abort/sequence/last-query state is missing",
    )
    check(
        "new AbortController()" in search_body,
        "base.html: the re-bind search does not create an AbortController",
    )
    check(
        search_body.count("signal: signal") >= 2,
        "base.html: both re-bind picker fetches must receive the abort signal",
    )
    check(
        "_releaseRebindAbortController.abort()" in search_body,
        "base.html: the re-bind search does not abort the previous request",
    )
    check(
        "_releaseRebindSearchSeq += 1;" in search_body
        and "const seq = _releaseRebindSearchSeq;" in search_body
        and "seq !== _releaseRebindSearchSeq" in search_body,
        "base.html: the re-bind search has no stale-response sequence guard",
    )
    check(
        "term === _releaseRebindLastQuery && _releaseRebindResults.length > 0"
        in search_body,
        "base.html: the re-bind search does not skip an identical already-loaded query",
    )
    check(
        "_releaseRebindLastQuery = term;" in search_body,
        "base.html: the re-bind search does not record the loaded query",
    )
    check(
        "err.name === 'AbortError'" in search_body,
        "base.html: the re-bind search does not swallow AbortError",
    )
    open_body = isolate(
        source, "function openReleaseRebindModal(", "function closeReleaseRebindModal(",
        "base.html openReleaseRebindModal",
    )
    close_body = isolate(
        source, "function closeReleaseRebindModal(", "function setReleaseRebindMode(",
        "base.html closeReleaseRebindModal",
    )
    for function_label, body in (
        ("openReleaseRebindModal", open_body),
        ("closeReleaseRebindModal", close_body),
    ):
        check(
            "_releaseRebindLastQuery = null;" in body,
            "base.html: %s() does not reset the loaded-query marker" % function_label,
        )
        check(
            "_releaseRebindAbortController.abort()" in body,
            "base.html: %s() does not abort an in-flight re-bind search"
            % function_label,
        )
    check(
        "_releaseRebindSearchSeq += 1;" in close_body,
        "base.html: closeReleaseRebindModal() does not invalidate in-flight responses",
    )
    check(
        re.search(r"\.key === 'Escape'", source) is not None,
        "base.html: the re-bind modal does not close on Escape",
    )
    check(
        "closeReleaseRebindModal();" in source,
        "base.html: Escape does not call closeReleaseRebindModal()",
    )
    check(
        "_releaseRebindFocus" in source and ".focus()" in source,
        "base.html: the re-bind modal does not restore focus to the opener",
    )
    check(
        "source.dataset" in source,
        "base.html: openReleaseRebindModal() does not accept a button element argument",
    )
    check(
        "source.releaseId" in source and "source.releaseTitle" in source,
        "base.html: openReleaseRebindModal() does not accept the object call form "
        "({releaseId, releaseTitle, seriesName})",
    )
    check(
        "location.reload()" in source and "showToast(" in source,
        "base.html: a successful re-bind does not toast and reload",
    )

    results_body = isolate(
        source,
        "function renderReleaseRebindResults(",
        "function selectReleaseRebindTarget(",
        "base.html renderReleaseRebindResults",
    )
    check(
        "innerHTML" not in results_body,
        "base.html: re-bind search results must be rendered without innerHTML",
    )
    check(
        "textContent" in results_body and "createElement" in results_body,
        "base.html: re-bind search results are not rendered with textContent/createElement",
    )


def check_base_preserved():
    source = read_text(BASE_TEMPLATE)
    for marker in BASE_PRESERVED:
        check(marker in source, "base.html: pre-existing wiring %r was lost" % marker)


# ---------------------------------------------------------------------------
# 2. release partials
# ---------------------------------------------------------------------------
def check_rebind_partial(path, label):
    source = read_text(path)
    for marker in REBIND_BUTTON_MARKERS:
        check(
            marker in source,
            "%s: missing the re-bind action marker %r" % (label, marker),
        )
    check(
        REBIND_BUTTON_CLASS in source,
        "%s: missing the %r button class" % (label, REBIND_BUTTON_CLASS),
    )
    for marker in PINNED_BADGE_MARKERS:
        check(
            marker in source,
            "%s: missing the pinned-badge marker %r" % (label, marker),
        )


def check_release_partials_preserved():
    for marker in FLAT_PRESERVED:
        check(
            marker in read_text(FLAT_PARTIAL),
            "partials/flat_release_row_chunk.html: pre-existing wiring %r was lost" % marker,
        )
    for marker in SERIES_PRESERVED:
        check(
            marker in read_text(SERIES_PARTIAL),
            "partials/series_card_chunk.html: pre-existing wiring %r was lost" % marker,
        )
    check(
        read_text(RELEASES_TEMPLATE).count('include "partials/') >= 2,
        "releases.html: the grouped/flat release partial includes were lost",
    )


def check_other_search_debounces():
    """Only the re-bind picker moved to 500 ms; the other searches stay at 250 ms."""
    settings = read_text(SETTINGS_TEMPLATE)
    check(
        "}, 250);" in isolate(
            settings,
            "function debounceSeriesIdentitySearch(",
            "async function resetSeriesIdentity(",
            "settings.html debounceSeriesIdentitySearch",
        ),
        "settings.html: the series-identity search must keep its 250 ms debounce",
    )
    series = read_text(SERIES_TEMPLATE)
    series_match = isolate(
        series,
        "function onSeriesMatchSearchInput(",
        "async function searchCalibreSeries(",
        "series.html onSeriesMatchSearchInput",
    )
    check(
        "}, 250);" in series_match,
        "series.html: the match modal search must keep its 250 ms debounce",
    )
    check(
        "}, 500);" not in series_match,
        "series.html: the match modal search must not be changed to 500 ms",
    )


# ---------------------------------------------------------------------------
# 3. settings.html
# ---------------------------------------------------------------------------
def check_settings_identity_card():
    source = read_text(SETTINGS_TEMPLATE)
    for element_id in SETTINGS_IDENTITY_IDS:
        check(
            'id="%s"' % element_id in source,
            "settings.html: missing the series-identity element id %r" % element_id,
        )
    for function_name in SETTINGS_IDENTITY_JS:
        check(
            function_name in source,
            "settings.html: missing the series-identity JS function %r" % function_name,
        )
    for endpoint in SETTINGS_IDENTITY_ENDPOINTS:
        check(
            endpoint in source,
            "settings.html: the series-identity JS does not use the endpoint %r" % endpoint,
        )
    check(
        SETTINGS_IDENTITY_RESET_BADGE in source,
        "settings.html: missing the %r row button class" % SETTINGS_IDENTITY_RESET_BADGE,
    )
    check(
        SETTINGS_ALIAS_DELETE_BADGE in source,
        "settings.html: missing the %r alias button class" % SETTINGS_ALIAS_DELETE_BADGE,
    )
    check(
        SERIES_IDENTITY_HELPER_TEXT in source,
        "settings.html: missing the per-series reset helper text",
    )
    check(
        "series-identity-reset-btn" in source and "data-series-id" in source,
        "settings.html: the identity rows do not expose data-series-id",
    )
    check(
        "confirm(" in source,
        "settings.html: identity reset/alias delete are not confirmed",
    )
    check(
        "}, 250);" in isolate(
            source,
            "function debounceSeriesIdentitySearch(",
            "async function resetSeriesIdentity(",
            "settings.html debounceSeriesIdentitySearch",
        ),
        "settings.html: the series-identity search is not debounced by 250 ms",
    )
    check(
        "Tracks: " in source,
        "settings.html: identity rows do not render the Tracks: display/tracking line",
    )
    check(
        "openReleaseRebindModal(" in source,
        "settings.html: the attribution audit does not offer the re-bind modal",
    )
    check(
        "releaseTitle:" in source and "seriesName:" in source,
        "settings.html: the attribution re-bind does not pass the object call form",
    )


def check_settings_display_card():
    source = read_text(SETTINGS_TEMPLATE)
    for element_id in DISPLAY_IDS:
        check(
            'id="%s"' % element_id in source,
            "settings.html: missing the display/logging element id %r" % element_id,
        )
    for zone in TIMEZONE_SUGGESTIONS:
        check(
            'value="%s"' % zone in source,
            "settings.html: the timezone datalist is missing %r" % zone,
        )
    for level in LOG_LEVELS:
        check(
            'value="%s"' % level in source,
            "settings.html: the log-level select is missing %r" % level,
        )
    check(
        "restart" in source and "6 hours" in source,
        "settings.html: the display/logging help does not mention restart and 6-hour pruning",
    )
    check(
        "0 disables" in source or "<strong>0 disables</strong>" in source,
        "settings.html: the activity-log limits do not explain that 0 disables a rule",
    )
    for element_id in ("setting-activity-retention", "setting-activity-max-rows"):
        start = source.find('id="%s"' % element_id)
        check(start != -1, "settings.html: missing #%s" % element_id)
        check(
            'min="0"' in source[start:source.find(">", start)],
            "settings.html: #%s has no min=0 bound" % element_id,
        )


def check_settings_storage_card():
    source = read_text(SETTINGS_TEMPLATE)
    for element_id in STORAGE_IDS:
        check(
            'id="%s"' % element_id in source,
            "settings.html: missing the storage/diagnostics element id %r" % element_id,
        )
    for function_name in STORAGE_JS:
        check(
            function_name in source,
            "settings.html: missing the storage/diagnostics JS function %r" % function_name,
        )
    for endpoint in STORAGE_ENDPOINTS:
        check(
            endpoint in source,
            "settings.html: the storage/diagnostics JS does not use the endpoint %r" % endpoint,
        )
    check(
        "window.location = '/api/maintenance/backup'" in source,
        "settings.html: downloadDatabaseBackup() does not use window.location for the backup",
    )
    check(
        "navigator.clipboard" in source,
        "settings.html: copyNetworkDiagnostics() has no clipboard implementation",
    )


def check_settings_save_payload():
    source = read_text(SETTINGS_TEMPLATE)
    body = isolate(
        source, "async function saveSettings", "async function resetNyaaBackfillCursor",
        "settings.html saveSettings",
    )
    for key in SETTINGS_PAYLOAD_NEW_KEYS:
        check(
            (key + ":") in body,
            "settings.html: saveSettings() is missing the new settings key %r" % key,
        )
    for key in SETTINGS_PAYLOAD_PINNED_KEYS:
        check(
            (key + ":") in body,
            "settings.html: saveSettings() dropped the pinned payload key %r" % key,
        )
    check(
        "document.getElementById('setting-timezone').value" in body,
        "settings.html: saveSettings() does not submit the timezone input",
    )
    check(
        "document.getElementById('setting-log-level').value" in body,
        "settings.html: saveSettings() does not submit the log-level select",
    )
    check(
        "document.getElementById('setting-activity-retention').value" in body,
        "settings.html: saveSettings() does not submit the activity retention value",
    )
    check(
        "document.getElementById('setting-activity-max-rows').value" in body,
        "settings.html: saveSettings() does not submit the activity max-rows value",
    )


def check_settings_dirty_guard():
    source = read_text(SETTINGS_TEMPLATE)
    for marker in DIRTY_MARKERS:
        check(
            marker in source,
            "settings.html: the unsaved-changes guard is missing %r" % marker,
        )
    check(
        'id="settings-dirty-badge" class="hidden' in source,
        "settings.html: #settings-dirty-badge must start hidden",
    )
    save_body = isolate(
        source, "async function saveSettings", "async function resetNyaaBackfillCursor",
        "settings.html saveSettings",
    )
    check(
        "_clearSettingsDirty();" in save_body,
        "settings.html: a successful saveSettings() does not clear the dirty badge",
    )


def check_settings_textcontent_only():
    source = read_text(SETTINGS_TEMPLATE)
    block = isolate(
        source,
        "// Series identity & overrides (per-series reset, aliases, attribution)",
        "// Unsaved-changes guard",
        "settings.html series/diagnostics JS",
    )
    check(
        "innerHTML" not in block,
        "settings.html: server data must be rendered with textContent/createElement, "
        "never innerHTML (regression in the series/diagnostics JS block)",
    )
    check(
        "textContent" in block and "createElement" in block,
        "settings.html: the series/diagnostics JS does not render with textContent/createElement",
    )


def check_settings_preserved():
    source = read_text(SETTINGS_TEMPLATE)
    for element_id in SETTINGS_PRESERVED_IDS:
        check(
            'id="%s"' % element_id in source,
            "settings.html: pre-existing element id %r was lost" % element_id,
        )
    for function_name in SETTINGS_PRESERVED_JS:
        check(
            function_name in source,
            "settings.html: pre-existing settings JS %r was lost" % function_name,
        )


def check_settings_notification_toggles():
    source = read_text(SETTINGS_TEMPLATE)
    for element_id in NOTIFICATION_IDS:
        check(
            'id="%s"' % element_id in source,
            "settings.html: missing the webhook toggle id %r" % element_id,
        )
    for marker in NOTIFICATION_HELP_MARKERS:
        check(
            marker in source,
            "settings.html: the Notifications card is missing %r" % marker,
        )
    for element_id in NOTIFICATION_IDS:
        start = source.find('id="%s"' % element_id)
        check(start != -1, "settings.html: missing #%s" % element_id)
        tail = source[start:start + 400]
        check(
            "!= 'false' %}checked{% endif %}" in tail,
            "settings.html: #%s does not default to checked"
            % element_id,
        )
    save_body = isolate(
        source, "async function saveSettings", "async function resetNyaaBackfillCursor",
        "settings.html saveSettings",
    )
    check(
        "webhook_new_request_notifications:" in save_body
        and "webhook_acquired_notifications:" in save_body,
        "settings.html: saveSettings() does not submit the webhook event toggles",
    )


def check_settings_metadata_clear():
    source = read_text(SETTINGS_TEMPLATE)
    for marker in MAINTENANCE_METADATA_MARKERS:
        check(
            marker in source,
            "settings.html: the metadata-cache maintenance action is missing %r" % marker,
        )
    check(
        'id="btn-clear-desc"' in source
        and 'id="btn-clear-discovery"' in source
        and 'id="btn-clear-thumbs"' in source
        and 'id="btn-purge-pdf"' in source,
        "settings.html: one of the four pre-existing maintenance actions was lost",
    )


def check_logs_template():
    source = read_text(LOGS_TEMPLATE)
    for marker in LOGS_MARKERS:
        check(
            marker in source,
            "logs.html: the local-time log rendering is missing %r" % marker,
        )


# ---------------------------------------------------------------------------
# 4. other UI-guarded templates still intact
# ---------------------------------------------------------------------------
def check_other_templates_pinned():
    discovery_partial = read_text(DISCOVERY_PARTIAL)
    for marker in (
        'class="discovery-card',
        "track-btn-{{ item.id }}",
        "blacklist-btn-{{ item.id }}",
        "Track Series",
        "Blacklist",
        "discovery-match-btn",
    ):
        check(
            marker in discovery_partial,
            "partials/discovery_card_chunk.html: pinned literal %r was lost" % marker,
        )

    discovery = read_text(DISCOVERY_TEMPLATE)
    for marker in (
        "function handleTrackBtn(",
        "function handleBlacklistBtn(",
        "function handleFetchSynopsis(",
        "textContent",
        "data.nyaa_next_page",
    ):
        check(
            marker in discovery,
            "discovery.html: pinned literal %r was lost" % marker,
        )
    check(
        "box.outerHTML =" not in discovery,
        "discovery.html: raw outerHTML injection is back",
    )

    series = read_text(SERIES_TEMPLATE)
    for marker in (
        "{{ s.display_title or s.name }}",
        "Tracks: {{ s.name }}",
        "series-match-btn",
        "series-match-modal",
    ):
        check(marker in series, "series.html: pinned literal %r was lost" % marker)

    downloads = read_text(DOWNLOADS_TEMPLATE)
    for marker in (
        "Downloader & Queue Management",
        "btn-pause",
        "btn-resume",
        "btn-delete",
        'id="qbit_url"',
        'id="qbit_username"',
        'id="qbit_password"',
        'id="qbit_category"',
        "Active TorBox Queue",
        'id="torbox-queue-body"',
    ):
        check(marker in downloads, "downloads.html: pinned literal %r was lost" % marker)


# ---------------------------------------------------------------------------
# 5. README
# ---------------------------------------------------------------------------
def check_readme():
    source = read_text(README_FILE)
    for marker in README_MARKERS:
        check(
            marker in source,
            "README.md: the re-bind/settings documentation does not mention %r" % marker,
        )


# ---------------------------------------------------------------------------
# 6. balanced Jinja delimiters
# ---------------------------------------------------------------------------
def check_balanced_jinja(path, label):
    """Balanced Jinja block/macro tags, expressions and comments."""
    source = read_text(path)

    block_pairs = (
        ("if/endif", r"\{%-?\s*if\b", r"\{%-?\s*endif\s*-?%\}"),
        ("for/endfor", r"\{%-?\s*for\b", r"\{%-?\s*endfor\s*-?%\}"),
        ("block/endblock", r"\{%-?\s*block\b", r"\{%-?\s*endblock\s*-?%\}"),
        ("macro/endmacro", r"\{%-?\s*macro\b", r"\{%-?\s*endmacro\s*-?%\}"),
    )
    for name, open_pattern, close_pattern in block_pairs:
        opened = len(re.findall(open_pattern, source))
        closed = len(re.findall(close_pattern, source))
        check(
            opened == closed,
            "%s: unbalanced Jinja %s - %d opening tag(s) vs %d closing tag(s)"
            % (label, name, opened, closed),
        )

    check(
        source.count("{%") == source.count("%}"),
        "%s: unbalanced Jinja statement delimiters - %d '{%%' vs %d '%%}'"
        % (label, source.count("{%"), source.count("%}")),
    )
    check(
        source.count("{{") == source.count("}}"),
        "%s: unbalanced Jinja expression delimiters - %d '{{' vs %d '}}'"
        % (label, source.count("{{"), source.count("}}")),
    )
    check(
        source.count("{#") == source.count("#}"),
        "%s: unbalanced Jinja comment delimiters - %d '{#' vs %d '#}'"
        % (label, source.count("{#"), source.count("#}")),
    )


def main():
    check_rebind_modal_markup()
    check_rebind_modal_js()
    check_other_search_debounces()
    check_base_preserved()

    check_rebind_partial(FLAT_PARTIAL, "partials/flat_release_row_chunk.html")
    check_rebind_partial(SERIES_PARTIAL, "partials/series_card_chunk.html")
    check_release_partials_preserved()

    check_settings_identity_card()
    check_settings_display_card()
    check_settings_storage_card()
    check_settings_save_payload()
    check_settings_dirty_guard()
    check_settings_textcontent_only()
    check_settings_preserved()
    check_settings_notification_toggles()
    check_settings_metadata_clear()
    check_logs_template()

    check_other_templates_pinned()
    check_readme()

    templates = (
        (BASE_TEMPLATE, "base.html"),
        (SETTINGS_TEMPLATE, "settings.html"),
        (RELEASES_TEMPLATE, "releases.html"),
        (LOGS_TEMPLATE, "logs.html"),
        (FLAT_PARTIAL, "partials/flat_release_row_chunk.html"),
        (SERIES_PARTIAL, "partials/series_card_chunk.html"),
    )
    for path, label in templates:
        check_balanced_jinja(path, label)

    print(
        "rebind-settings-ui static guard PASSED: base.html re-bind modal (ids, modes, "
        "tracked+Calibre search debounced at 500 ms with AbortController/sequence/"
        "last-query stale-response protection, default-unchecked pin, new-title/detach, "
        "Escape/backdrop/focus, rebind+unpin endpoints, element+object call forms), both "
        "release partials (release-rebind-btn + release-pinned-badge), settings "
        "identity/reset/alias/attribution card, display & logging card, storage & "
        "diagnostics card, unsaved-changes guard, textContent-only rendering, README "
        "documentation, pinned-literal preservation and balanced Jinja delimiters in "
        "%d edited template(s)" % len(templates)
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
