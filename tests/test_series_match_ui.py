#!/usr/bin/env python3
"""Static source-level guard for the series identity / manual Calibre matching UI.

Why static and not a render test: the interpreter used for the repo suites has
neither ``jinja2`` nor ``fastapi`` installed, so ``main.py`` cannot be imported and
the templates cannot be rendered.  This suite therefore reads ``main.py``, the
templates and ``README.md`` as text and proves the CONTRACT §6 (series identity)
deliverables are wired:

1. ``main.py`` exposes the manual-matching API:
   ``GET /api/calibre/series`` (searchable/ranked picker), ``POST
   /api/series/{series_id}/match-calibre``, ``POST /api/discovery/match``,
   ``POST /api/series/{series_id}/refresh-metadata``, ``GET /api/series/aliases``
   and ``DELETE /api/series/aliases/{override_id}`` - with scanner snapshots,
   ``asyncio.to_thread`` for manager/DB work, ``SequenceMatcher`` ranking and the
   required 400/404 handling.  Every pre-existing route/decorator pinned by the
   downloader/UI wiring suites is still present.
2. ``series.html`` shows the AniList display title with a ``Tracks:`` line, keeps
   the Calibre ID line, exposes per-row ``Match Calibre`` / ``Refresh AniList``
   buttons and ships the ``#series-match-modal`` picker (ids, JS helper names,
   endpoints, 250 ms debounce, Escape/backdrop close).
3. ``partials/discovery_card_chunk.html`` gains the ``Match Calibre`` action
   (``discovery-match-btn`` + ``data-series-name``/``data-release-id``) without
   disturbing the Track/Blacklist buttons, and ``discovery.html`` ships the
   ``#discovery-match-modal`` picker while keeping all existing Track/Blacklist/
   backfill JS untouched.
4. ``README.md`` documents tracking name vs display title, AniList synonym/romaji/
   native matching, the manual pickers + API endpoints, the AniList refresh and the
   four ``series_anilist_*`` settings keys with their defaults.
5. Every edited template has balanced Jinja block/expression delimiters.

Bare ``assert`` is never used (Calibre runs with ``sys.flags.optimize == 2`` and
compiles bare asserts out, which would make this guard vacuous); an explicit
``check()`` helper raises ``AssertionError`` instead.

Stdlib only: os, re, sys.  No third-party imports, no network, no database.

Usage::

    & $py tests\\run_tests.py series_match_ui
    & $py tests\\test_series_match_ui.py
"""

import os
import re
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend", "app")
TEMPLATES_DIR = os.path.join(BACKEND_DIR, "templates")

MAIN_PY = os.path.join(BACKEND_DIR, "main.py")
SERIES_TEMPLATE = os.path.join(TEMPLATES_DIR, "series.html")
DISCOVERY_TEMPLATE = os.path.join(TEMPLATES_DIR, "discovery.html")
DISCOVERY_PARTIAL = os.path.join(TEMPLATES_DIR, "partials", "discovery_card_chunk.html")
README_FILE = os.path.join(PROJECT_ROOT, "README.md")

# --- main.py contract -------------------------------------------------------
# (decorator literal, handler function definition) - both must be source literals
# because the UI-wiring suites read main.py as text.
NEW_SERIES_ROUTES = (
    ('@app.get("/api/calibre/series")', "async def api_calibre_series_picker("),
    ('@app.post("/api/series/{series_id}/match-calibre")', "async def api_match_series_to_calibre("),
    ('@app.post("/api/discovery/match")', "async def api_discovery_match("),
    ('@app.post("/api/series/{series_id}/refresh-metadata")', "async def api_refresh_series_metadata("),
    ('@app.get("/api/series/aliases")', "async def api_get_series_aliases("),
    ('@app.delete("/api/series/aliases/{override_id}")', "async def api_delete_series_alias("),
)

MAIN_HELPERS = (
    "def _series_match_clean(",
    "def _series_match_normalize(",
    "def _calibre_series_picker_rows(",
    "def _series_picker_entry(",
    "def _rank_series_picker_entries(",
    "def _find_series_row(",
    "def _calibre_series_by_id(",
    "def _series_match_payload(",
)

MAIN_MARKERS = (
    # Calibre snapshot + fallback to tracked series rows.
    "_calibre_series_snapshot",
    "scanner.get_all_series()",
    'if row.get("calibre_series_id")',
    # Picker normalisation reuses the shared identity helpers (CONTRACT §6.1).
    "clean_series_for_matching",
    "from app.parser import normalize_text",
    # Ranking uses difflib as required by CONTRACT §6.1.
    "difflib.SequenceMatcher",
    "def _rank_series_picker_entries",
    # Manager/DB work is off the event loop.
    "task_manager.match_series_to_calibre, source_name, calibre_row",
    "asyncio.to_thread(task_manager.refresh_series_metadata",
    "asyncio.to_thread(db.get_series_alias_overrides)",
    "asyncio.to_thread(db.delete_series_alias_override",
    # Error handling required by CONTRACT §6.1.
    'detail="Series not found"',
    'detail="calibre_series_id is required"',
    'detail="series_name is required"',
    'detail=f"Unknown Calibre series id: {calibre_series_id}"',
    'detail="Alias override not found"',
    # Picker never raises: scanner failure degrades to an empty list.
    '"status": "success", "series": [], "count": 0',
    # Alias payload shape.
    '"status": "success", "aliases": aliases or []',
)

# Every route pinned by tests/test_downloader.py Tier 4 plus the guarded
# download endpoint from tests/test_ui_wiring.py / tests/test_torbox_ui_wiring.py.
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
    '@app.get("/audiobooks"',
    '@app.post("/api/downloads/torbox/poll")',
    '@app.post("/api/downloader/torbox/test")',
)

PINNED_EXISTING_HELPERS = (
    "def get_qbit_client(",
    "def _active_downloader_engine(",
    "def _masked_settings(",
    "def _prepare_settings_payload(",
    "def api_release_download(",
    "def api_fetch_synopsis(",
)

RAW_KEY_REGRESSION_PATTERN = r'target\.get\("download_url"\)\s*or\s*target\.get\("link"\)'

# --- series.html contract ---------------------------------------------------
SERIES_DISPLAY_PRIMARY = "{{ s.display_title or s.name }}"
SERIES_TRACKS_LINE = "Tracks: {{ s.name }}"
SERIES_CALIBRE_ID_LINE = "Calibre ID: #{{ s.calibre_series_id }}"
SERIES_MATCH_BUTTON_CLASS = "series-match-btn"
SERIES_ANILIST_BUTTON_CLASS = "series-anilist-btn"
SERIES_MATCH_ONCLICK = "openSeriesMatchModal(this.dataset.seriesName, this.dataset.seriesId)"
SERIES_ANILIST_ONCLICK = "refreshSeriesMetadata({{ s.id }}, this)"

SERIES_MODAL_IDS = (
    "series-match-modal",
    "series-match-search",
    "series-match-results",
    "series-match-cancel",
    "series-match-source",
)

SERIES_JS_FUNCTIONS = (
    "function openSeriesMatchModal(",
    "function closeSeriesMatchModal(",
    "function onSeriesMatchSearchInput(",
    "function searchCalibreSeries(",
    "function renderSeriesMatchResults(",
    "function applySeriesMatch(",
    "function refreshSeriesMetadata(",
)

SERIES_ENDPOINTS = (
    "/api/calibre/series",
    "/api/series/${seriesId}/match-calibre",
    "/api/discovery/match",
    "/api/series/${id}/refresh-metadata",
)

# Existing series.html wiring that must stay reachable.
SERIES_PRESERVED = (
    "function setFilter(",
    "function filterSeries(",
    "function handleEditLink(",
    "function editSeriesLink(",
    "function handleFetchSeriesDetails(",
    "triggerCalibreSync()",
    "toggleSeries({{ s.id }})",
    "/api/series/${seriesId}/link",
    "/api/series/${seriesId}/fetch-info",
    'class="series-row',
    'data-monitored="{{ s.is_monitored }}"',
)

# --- discovery contract -----------------------------------------------------
DISCOVERY_MATCH_BUTTON_CLASS = "discovery-match-btn"
DISCOVERY_MATCH_ONCLICK = "openDiscoveryMatchModal(this.dataset.seriesName, this.dataset.releaseId)"

DISCOVERY_PARTIAL_PRESERVED = (
    'class="discovery-card',
    "track-btn-{{ item.id }}",
    "blacklist-btn-{{ item.id }}",
    "handleTrackBtn(this)",
    "handleBlacklistBtn(this)",
    "Track Series",
    "Blacklist",
    "data-title=",
    "data-series=",
    "Load Description",
)

DISCOVERY_MODAL_IDS = (
    "discovery-match-modal",
    "discovery-match-search",
    "discovery-match-results",
    "discovery-match-cancel",
    "discovery-match-source",
)

DISCOVERY_JS_FUNCTIONS = (
    "function openDiscoveryMatchModal(",
    "function closeDiscoveryMatchModal(",
    "function onDiscoveryMatchSearchInput(",
    "function searchDiscoveryCalibreSeries(",
    "function renderDiscoveryMatchResults(",
    "function applyDiscoveryMatch(",
)

DISCOVERY_ENDPOINTS = (
    "/api/calibre/series",
    "/api/discovery/match",
)

# Existing discovery.html Track/Blacklist/backfill/synopsis JS that must survive.
DISCOVERY_PRESERVED = (
    "function switchTab(",
    "function filterDiscovery(",
    "function filterBlacklist(",
    "function handleTrackBtn(",
    "function handleBlacklistBtn(",
    "function handleUnblockBtn(",
    "function trackDiscovered(",
    "function blacklistDiscovered(",
    "function submitManualBlacklist(",
    "function removeFromBlacklist(",
    "function triggerMasterIndexImport(",
    "function checkBackfillStatus(",
    "function toggleBackfill(",
    "function stopBackfill(",
    "function handleFetchSynopsis(",
    "synDiv.textContent",
    "/api/discovery/track",
    "/api/discovery/blacklist",
    "/api/backfill/start",
    "/api/backfill/status",
    "/api/backfill/stop",
    "data.nyaa_next_page",
    "data.nyaa_done",
    'include "partials/discovery_card_chunk.html"',
)

# --- README contract --------------------------------------------------------
README_MARKERS = (
    "Series Identity",
    "display title",
    "AniList",
    "romaji",
    "native",
    "synonym",
    "Manual match picker",
    "Match Calibre",
    "series_alias_overrides",
    "GET /api/calibre/series",
    "POST /api/series/{series_id}/match-calibre",
    "POST /api/discovery/match",
    "GET /api/series/aliases",
    "DELETE /api/series/aliases/{override_id}",
    "POST /api/series/{series_id}/refresh-metadata",
    "series_anilist_sync_enabled",
    "series_anilist_sync_limit",
    "series_anilist_match_enabled",
    "series_anilist_match_budget",
    "`true`",
    "`15`",
    "`10`",
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


# ---------------------------------------------------------------------------
# 1. main.py
# ---------------------------------------------------------------------------
def check_main_routes():
    source = read_text(MAIN_PY)
    for route, handler in NEW_SERIES_ROUTES:
        check(route in source, "main.py: missing the series-identity route %r" % route)
        check(handler in source, "main.py: missing the handler definition %r" % handler)
    for helper in MAIN_HELPERS:
        check(helper in source, "main.py: missing the series-identity helper %r" % helper)
    for marker in MAIN_MARKERS:
        check(marker in source, "main.py: missing series-identity marker %r" % marker)


def check_main_preserves_existing_wiring():
    source = read_text(MAIN_PY)
    for route in PINNED_EXISTING_ROUTES:
        check(route in source, "main.py: pre-existing route literal %r was lost" % route)
    for helper in PINNED_EXISTING_HELPERS:
        check(helper in source, "main.py: pre-existing helper %r was lost" % helper)
    check(
        re.search(RAW_KEY_REGRESSION_PATTERN, source) is not None,
        "main.py: the release-download URL resolution (download_url or link) that "
        "tests/test_ui_wiring.py pins was changed",
    )
    check(
        "def api_calibre_test(" in source and '@app.get("/api/calibre/test")' in source,
        "main.py: the existing /api/calibre/test endpoint was lost",
    )
    check(
        '@app.get("/api/series_releases")' in source and "def api_releases_grouped(" in source,
        "main.py: the existing grouped releases endpoint was lost",
    )


# ---------------------------------------------------------------------------
# 2. series.html
# ---------------------------------------------------------------------------
def check_series_display_title():
    source = read_text(SERIES_TEMPLATE)
    check(
        SERIES_DISPLAY_PRIMARY in source,
        "series.html: the primary series name is not '{{ s.display_title or s.name }}'",
    )
    check(
        SERIES_TRACKS_LINE in source,
        "series.html: missing the 'Tracks: {{ s.name }}' stable-name line",
    )
    check(
        "s.display_title != s.name" in source,
        "series.html: the Tracks line is not gated on display_title differing from name",
    )
    check(
        SERIES_CALIBRE_ID_LINE in source,
        "series.html: the 'Calibre ID: #{{ s.calibre_series_id }}' line was lost",
    )
    for preserved in SERIES_PRESERVED:
        check(preserved in source, "series.html: pre-existing wiring %r was lost" % preserved)


def check_series_actions():
    source = read_text(SERIES_TEMPLATE)
    check(
        SERIES_MATCH_BUTTON_CLASS in source,
        "series.html: missing the per-row match button class %r" % SERIES_MATCH_BUTTON_CLASS,
    )
    check(
        SERIES_ANILIST_BUTTON_CLASS in source,
        "series.html: missing the per-row AniList button class %r" % SERIES_ANILIST_BUTTON_CLASS,
    )
    check(
        SERIES_MATCH_ONCLICK in source,
        "series.html: the match button does not call openSeriesMatchModal(row.dataset...)",
    )
    check(
        SERIES_ANILIST_ONCLICK in source,
        "series.html: the refresh button does not call refreshSeriesMetadata(id, this)",
    )
    check(
        'data-series-id="{{ s.id }}"' in source and 'data-series-name="{{ s.name }}"' in source,
        "series.html: the row action buttons do not expose data-series-id/data-series-name",
    )
    check(
        "Change match" in source,
        "series.html: the match button does not switch its label/title to 'Change match' when linked",
    )
    check(
        "Match Calibre" in source and "Refresh AniList" in source,
        "series.html: the row actions are missing their 'Match Calibre' / 'Refresh AniList' labels",
    )


def check_series_modal():
    source = read_text(SERIES_TEMPLATE)
    for element_id in SERIES_MODAL_IDS:
        check(
            'id="%s"' % element_id in source,
            "series.html: missing the match-modal element id %r" % element_id,
        )
    check(
        "Match series to Calibre" in source,
        "series.html: the match modal title 'Match series to Calibre' is missing",
    )
    for function_name in SERIES_JS_FUNCTIONS:
        check(
            function_name in source,
            "series.html: missing the match-modal JS function %r" % function_name,
        )
    check(
        "onSeriesMatchSearchInput()" in source and "}, 250);" in source,
        "series.html: the match search input is not debounced by 250 ms",
    )
    check(
        "evt.key === 'Escape'" in source,
        "series.html: the match modal does not close on Escape",
    )
    check(
        'onclick="closeSeriesMatchModal()"' in source,
        "series.html: the match modal has no backdrop/cancel close handler",
    )
    for endpoint in SERIES_ENDPOINTS:
        check(
            endpoint in source,
            "series.html: the match JS does not use the endpoint %r" % endpoint,
        )
    check(
        "location.reload()" in source,
        "series.html: a successful match/refresh no longer reloads the page",
    )
    check(
        "showToast(" in source,
        "series.html: the match/refresh flows no longer surface toasts",
    )
    check(
        "already tracked" in source and "Already linked" in source,
        "series.html: the match picker does not mark already-tracked/linked Calibre series",
    )


# ---------------------------------------------------------------------------
# 3. discovery partial + page
# ---------------------------------------------------------------------------
def check_discovery_partial():
    source = read_text(DISCOVERY_PARTIAL)
    check(
        DISCOVERY_MATCH_BUTTON_CLASS in source,
        "partials/discovery_card_chunk.html: missing the Match Calibre action %r"
        % DISCOVERY_MATCH_BUTTON_CLASS,
    )
    check(
        "Match Calibre" in source,
        "partials/discovery_card_chunk.html: the match action has no visible label",
    )
    check(
        'data-series-name="{{ item.series_name }}"' in source,
        "partials/discovery_card_chunk.html: the match button does not expose data-series-name",
    )
    check(
        'data-release-id="{{ item.id }}"' in source,
        "partials/discovery_card_chunk.html: the match button does not expose data-release-id",
    )
    check(
        DISCOVERY_MATCH_ONCLICK in source,
        "partials/discovery_card_chunk.html: the match button does not call "
        "openDiscoveryMatchModal(seriesName, releaseId)",
    )
    for preserved in DISCOVERY_PARTIAL_PRESERVED:
        check(
            preserved in source,
            "partials/discovery_card_chunk.html: pre-existing card markup/pill %r was lost"
            % preserved,
        )
    check(
        'class="grid grid-cols-2 gap-2"' in source,
        "partials/discovery_card_chunk.html: the Track/Blacklist grid layout was changed",
    )


def check_discovery_page():
    source = read_text(DISCOVERY_TEMPLATE)
    for element_id in DISCOVERY_MODAL_IDS:
        check(
            'id="%s"' % element_id in source,
            "discovery.html: missing the match-modal element id %r" % element_id,
        )
    check(
        "Match discovered series to Calibre" in source,
        "discovery.html: the discovery match modal title is missing",
    )
    for function_name in DISCOVERY_JS_FUNCTIONS:
        check(
            function_name in source,
            "discovery.html: missing the match-modal JS function %r" % function_name,
        )
    check(
        "onDiscoveryMatchSearchInput()" in source and "}, 250);" in source,
        "discovery.html: the discovery match search is not debounced by 250 ms",
    )
    check(
        "evt.key === 'Escape'" in source,
        "discovery.html: the discovery match modal does not close on Escape",
    )
    check(
        'onclick="closeDiscoveryMatchModal()"' in source,
        "discovery.html: the discovery match modal has no backdrop close handler",
    )
    for endpoint in DISCOVERY_ENDPOINTS:
        check(
            endpoint in source,
            "discovery.html: the discovery match JS does not use the endpoint %r" % endpoint,
        )
    for preserved in DISCOVERY_PRESERVED:
        check(
            preserved in source,
            "discovery.html: pre-existing Track/Blacklist/backfill wiring %r was lost" % preserved,
        )
    check(
        "already tracked" in source and "Already linked" in source,
        "discovery.html: the match picker does not mark already-tracked/linked Calibre series",
    )
    check(
        "box.outerHTML =" not in source,
        "discovery.html: regression - raw outerHTML injection is back in the template",
    )


# ---------------------------------------------------------------------------
# 4. README
# ---------------------------------------------------------------------------
def check_readme():
    source = read_text(README_FILE)
    for marker in README_MARKERS:
        check(
            marker in source,
            "README.md: the series-identity documentation does not mention %r" % marker,
        )


# ---------------------------------------------------------------------------
# 5. balanced Jinja delimiters
# ---------------------------------------------------------------------------
def check_balanced_jinja(path, label):
    source = read_text(path)
    open_block = source.count("{%")
    close_block = source.count("%}")
    check(
        open_block == close_block,
        "%s: unbalanced Jinja statement delimiters - %d '{%%' vs %d '%%}'"
        % (label, open_block, close_block),
    )
    open_expr = source.count("{{")
    close_expr = source.count("}}")
    check(
        open_expr == close_expr,
        "%s: unbalanced Jinja expression delimiters - %d '{{' vs %d '}}'"
        % (label, open_expr, close_expr),
    )
    open_comment = source.count("{#")
    close_comment = source.count("#}")
    check(
        open_comment == close_comment,
        "%s: unbalanced Jinja comment delimiters - %d '{#' vs %d '#}'"
        % (label, open_comment, close_comment),
    )


def main():
    check_main_routes()
    check_main_preserves_existing_wiring()
    check_series_display_title()
    check_series_actions()
    check_series_modal()
    check_discovery_partial()
    check_discovery_page()
    check_readme()

    templates = (
        (SERIES_TEMPLATE, "series.html"),
        (DISCOVERY_TEMPLATE, "discovery.html"),
        (DISCOVERY_PARTIAL, "partials/discovery_card_chunk.html"),
    )
    for path, label in templates:
        check_balanced_jinja(path, label)

    print(
        "series-match-ui static guard PASSED: 6 manual-match/refresh/alias routes + "
        "picker helpers in main.py, series.html display-title/Tracks line + row "
        "actions + match modal JS, discovery card Match Calibre action + discovery "
        "match modal JS (Track/Blacklist/backfill preserved), README series-identity "
        "section, and balanced Jinja delimiters in %d edited template(s)"
        % len(templates)
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
