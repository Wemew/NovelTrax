#!/usr/bin/env python3
"""Static source-level guard for the false-In-Calibre repair UI (Recheck / Unlink Calibre).

Why static and not a render test: the interpreter used for the repo suites has neither
``jinja2`` nor ``fastapi`` installed, so the templates cannot be rendered.  This suite
reads the templates and ``README.md`` as text and proves the
``.agents/calibre_repair/CONTRACT.md`` §6 (owner D) deliverables are wired:

1. ``series.html`` per-row actions for Calibre-linked series only:
   ``.series-recheck-calibre-btn`` (``refresh-cw``, tooltip "Re-read Calibre and recompute
   this series' owned volumes/statuses") calling ``recheckSeriesCalibre({{ s.id }}, this)``
   and ``.series-unlink-calibre-btn`` (``unlink``, tooltip "Unlink from Calibre (repairs
   false In Calibre ownership)") calling ``unlinkSeriesCalibre({{ s.id }}, this)`` with a
   confirm() explaining the link/owned-volume removal and the Wanted/New reset.  The JS
   disables the button while running, POSTs ``/api/series/{id}/recheck-calibre`` /
   ``/api/series/{id}/unlink-calibre``, toasts the returned message/counts and reloads.
2. ``settings.html`` Series identity card offers the same per-row "Unlink Calibre" action
   when ``row.calibre_series_id`` is present, using the shared ``unlinkSeriesCalibre()``
   name/endpoint/confirm, without disturbing existing ids.
3. ``README.md`` documents the false In-Calibre failure mode, both repair actions and their
   effects (link/owned volumes cleared, releases back to Wanted/New; aliases and AniList
   metadata preserved; Recheck recomputes from the real library).
4. Every pinned literal of the earlier UI-guarded suites is still present and the edited
   templates have balanced Jinja block/expression/comment delimiters.

Bare ``assert`` is never used (Calibre runs with ``sys.flags.optimize == 2`` and
compiles bare asserts out, which would make this guard vacuous); an explicit
``check()`` helper raises ``AssertionError`` instead.

Stdlib only: os, re, sys.  No third-party imports, no network, no database.

Usage::

    & $py tests\\run_tests.py calibre_repair_ui
    & $py tests\\test_calibre_repair_ui.py
"""

import os
import re
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "backend", "app", "templates")

SERIES_TEMPLATE = os.path.join(TEMPLATES_DIR, "series.html")
SETTINGS_TEMPLATE = os.path.join(TEMPLATES_DIR, "settings.html")
BASE_TEMPLATE = os.path.join(TEMPLATES_DIR, "base.html")
FLAT_PARTIAL = os.path.join(TEMPLATES_DIR, "partials", "flat_release_row_chunk.html")
SERIES_CARD_PARTIAL = os.path.join(TEMPLATES_DIR, "partials", "series_card_chunk.html")
DISCOVERY_PARTIAL = os.path.join(TEMPLATES_DIR, "partials", "discovery_card_chunk.html")
README_FILE = os.path.join(PROJECT_ROOT, "README.md")

# --- §6 series.html repair actions ----------------------------------------
RECHECK_BUTTON_CLASS = "series-recheck-calibre-btn"
UNLINK_BUTTON_CLASS = "series-unlink-calibre-btn"
RECHECK_TOOLTIP = "Re-read Calibre and recompute this series' owned volumes/statuses"
UNLINK_TOOLTIP = "Unlink from Calibre (repairs false In Calibre ownership)"
RECHECK_ONCLICK = "recheckSeriesCalibre({{ s.id }}, this)"
UNLINK_ONCLICK = "unlinkSeriesCalibre({{ s.id }}, this)"
RECHECK_ICON = 'data-lucide="refresh-cw"'
UNLINK_ICON = 'data-lucide="unlink"'
CALIBRE_GUARD = "{% if s.calibre_series_id %}"

SERIES_JS = (
    "async function recheckSeriesCalibre(seriesId, btn)",
    "async function unlinkSeriesCalibre(seriesId, btn)",
)

SERIES_ENDPOINTS = (
    "/api/series/${id}/recheck-calibre",
    "/api/series/${id}/unlink-calibre",
)

UNLINK_CONFIRM_MARKERS = (
    "Unlink this series from Calibre?",
    "owned-volume data are removed",
    "Wanted/New",
    "Aliases and AniList metadata are preserved",
)

# literals pinned by tests/test_series_match_ui.py + tests/test_ui_wiring.py +
# tests/test_bundle_inference_ui.py.
SERIES_PINNED = (
    '{% extends "base.html" %}',
    "{{ s.display_title or s.name }}",
    "Tracks: {{ s.name }}",
    "Calibre ID: #{{ s.calibre_series_id }}",
    "series-publication-line",
    'class="series-row',
    'data-monitored="{{ s.is_monitored }}"',
    "series-match-btn",
    "series-anilist-btn",
    "openSeriesMatchModal(this.dataset.seriesName, this.dataset.seriesId)",
    "refreshSeriesMetadata({{ s.id }}, this)",
    'id="series-match-modal"',
    'id="series-match-search"',
    'id="series-match-results"',
    'id="series-match-cancel"',
    'id="series-match-source"',
    "function openSeriesMatchModal(",
    "function closeSeriesMatchModal(",
    "function searchCalibreSeries(",
    "function renderSeriesMatchResults(",
    "function applySeriesMatch(",
    "function setFilter(",
    "function filterSeries(",
    "function handleEditLink(",
    "function handleFetchSeriesDetails(",
    "toggleSeries({{ s.id }})",
)

# --- §6 settings.html per-row unlink ---------------------------------------
SETTINGS_UNLINK_FUNCTION = "async function unlinkSeriesCalibre(seriesId, btn)"
SETTINGS_UNLINK_ENDPOINT = "/api/series/${id}/unlink-calibre"
SETTINGS_ROW_GUARD = "if (row.calibre_series_id)"

# Existing settings ids/keys pinned by the rebind/bundle-inference suites.
SETTINGS_PINNED_IDS = (
    "setting-calibre-path",
    "setting-calibre-interval",
    "setting-feed-interval",
    "setting-nyaa-bundle-inference",
    "setting-downloader-engine",
    "setting-torbox-api-key",
    "setting-downloads-dir",
    "series-identity-search",
    "series-identity-results",
    "series-alias-list",
    "series-attribution-result",
    "settings-dirty-badge",
    "settings-form",
    "btn-clear-metadata",
)

SETTINGS_PINNED_PAYLOAD_KEYS = (
    "calibre_path",
    "calibre_sync_interval_minutes",
    "feed_check_interval_minutes",
    "feed_catchup_enabled",
    "backfill_include_nyaa",
    "track_official_only",
    "nyaa_bundle_volume_inference",
    "downloader_engine",
    "torbox_api_key",
    "webhook_new_request_notifications",
    "app_timezone",
    "log_level",
    "activity_retention_days",
)

# --- README ---------------------------------------------------------------
README_MARKERS = (
    "Repairing false In Calibre ownership",
    "Recheck Calibre",
    "Unlink Calibre",
    "/api/series/{series_id}/recheck-calibre",
    "/api/series/{series_id}/unlink-calibre",
    "false **In Calibre**",
    "owned_volumes_json",
    "calibre_series_id",
    "REQUESTED",
    "NEW",
    "SERIES_CALIBRE_UNLINKED",
    "SERIES_CALIBRE_RECHECKED",
    "alias overrides",
    "identity locks",
    "Match Calibre",
    "Series identity & overrides",
)

# --- sibling UI-guarded templates that must keep working -------------------
CROSS_TEMPLATE_MARKERS = (
    (BASE_TEMPLATE, "base.html", (
        'id="release-rebind-modal"',
        "function openReleaseRebindModal(",
        "async function downloadRelease(releaseId, btn = null)",
        "function showToast(message, type = 'success')",
    )),
    (FLAT_PARTIAL, "partials/flat_release_row_chunk.html", (
        "downloadRelease({{ r.id }}, this)",
        "release-state-chip",
        "Bundle: Vol {{ r.volumes[0] }}–{{ r.volumes[-1] }}",
    )),
    (SERIES_CARD_PARTIAL, "partials/series_card_chunk.html", (
        'class="series-card',
        'loading="lazy"',
        'decoding="async"',
        "cover-placeholder",
        "release-rebind-btn",
        "release-pinned-badge",
    )),
    (DISCOVERY_PARTIAL, "partials/discovery_card_chunk.html", (
        'class="discovery-card',
        "Track Series",
        "handleTrackBtn(this)",
    )),
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
# 1. series.html repair buttons
# ---------------------------------------------------------------------------
def check_series_repair_buttons():
    source = read_text(SERIES_TEMPLATE)

    check(
        RECHECK_BUTTON_CLASS in source,
        "series.html: missing the %r action" % RECHECK_BUTTON_CLASS,
    )
    check(
        UNLINK_BUTTON_CLASS in source,
        "series.html: missing the %r action" % UNLINK_BUTTON_CLASS,
    )
    check(
        RECHECK_ICON in source,
        "series.html: the Recheck Calibre button does not use the refresh-cw icon",
    )
    check(
        UNLINK_ICON in source,
        "series.html: the Unlink Calibre button does not use the unlink icon",
    )
    check(
        RECHECK_TOOLTIP in source,
        "series.html: missing the Recheck Calibre tooltip %r" % RECHECK_TOOLTIP,
    )
    check(
        UNLINK_TOOLTIP in source,
        "series.html: missing the Unlink Calibre tooltip %r" % UNLINK_TOOLTIP,
    )
    check(
        RECHECK_ONCLICK in source,
        "series.html: the recheck button does not call recheckSeriesCalibre({{ s.id }}, this)",
    )
    check(
        UNLINK_ONCLICK in source,
        "series.html: the unlink button does not call unlinkSeriesCalibre({{ s.id }}, this)",
    )

    # Both buttons must live in the same per-row actions block, read the linked id and
    # be gated on s.calibre_series_id (rendered only for linked series).
    anilist_index = source.find(RECHECK_BUTTON_CLASS)
    guard_index = source.rfind(CALIBRE_GUARD, 0, anilist_index)
    unlink_index = source.find(UNLINK_BUTTON_CLASS)
    end_index = source.find("{% endif %}", unlink_index)
    check(
        guard_index != -1,
        "series.html: the check-calibre actions are not gated on s.calibre_series_id",
    )
    check(
        guard_index < anilist_index < unlink_index < end_index,
        "series.html: the check-calibre actions are not inside one "
        "{% if s.calibre_series_id %} block",
    )
    check(
        'data-series-id="{{ s.id }}"' in source[anilist_index:end_index]
        and 'data-series-name="{{ s.name }}"' in source[anilist_index:end_index],
        "series.html: the check-calibre buttons do not expose data-series-id/name",
    )


def check_series_repair_js():
    source = read_text(SERIES_TEMPLATE)
    for function_name in SERIES_JS:
        check(
            function_name in source,
            "series.html: missing the repair JS function %r" % function_name,
        )
    for endpoint in SERIES_ENDPOINTS:
        check(
            endpoint in source,
            "series.html: the repair JS does not POST %r" % endpoint,
        )
    for marker in UNLINK_CONFIRM_MARKERS:
        check(
            marker in source,
            "series.html: the unlink confirm() does not mention %r" % marker,
        )

    recheck_body = isolate(
        source,
        "async function recheckSeriesCalibre(",
        "async function unlinkSeriesCalibre(",
        "series.html recheckSeriesCalibre",
    )
    unlink_body = isolate(
        source,
        "async function unlinkSeriesCalibre(",
        "document.addEventListener('keydown'",
        "series.html unlinkSeriesCalibre",
    )
    for label, body in (("recheck", recheck_body), ("unlink", unlink_body)):
        check(
            "btn.disabled = true" in body and "btn.disabled = false" in body,
            "series.html: %sSeriesCalibre() does not disable the button while running"
            % label,
        )
        check(
            "location.reload()" in body,
            "series.html: %sSeriesCalibre() does not reload after success" % label,
        )
        check(
            "showToast(" in body and "data.message" in body,
            "series.html: %sSeriesCalibre() does not toast the server message" % label,
        )
        check(
            "confirm(" in body or label == "recheck",
            "series.html: unlinkSeriesCalibre() has no confirmation step",
        )
    check(
        "releases_reset" in unlink_body,
        "series.html: unlinkSeriesCalibre() does not surface the reset release count",
    )


def check_series_pinned():
    source = read_text(SERIES_TEMPLATE)
    for marker in SERIES_PINNED:
        check(
            marker in source,
            "series.html: pinned literal %r was lost" % marker,
        )


# ---------------------------------------------------------------------------
# 2. settings.html Series identity per-row Unlink Calibre
# ---------------------------------------------------------------------------
def check_settings_unlink():
    source = read_text(SETTINGS_TEMPLATE)
    check(
        UNLINK_BUTTON_CLASS in source,
        "settings.html: the Series identity card has no %r action"
        % UNLINK_BUTTON_CLASS,
    )
    check(
        "Unlink Calibre" in source,
        "settings.html: the per-row unlink action has no visible label",
    )
    check(
        UNLINK_TOOLTIP in source,
        "settings.html: missing the per-row unlink tooltip",
    )
    check(
        SETTINGS_ROW_GUARD in source,
        "settings.html: the per-row unlink action is not gated on row.calibre_series_id",
    )
    check(
        SETTINGS_UNLINK_FUNCTION in source,
        "settings.html: missing the shared %r function" % SETTINGS_UNLINK_FUNCTION,
    )
    check(
        SETTINGS_UNLINK_ENDPOINT in source,
        "settings.html: unlinkSeriesCalibre() does not POST %r"
        % SETTINGS_UNLINK_ENDPOINT,
    )
    settings_unlink = isolate(
        source,
        "async function unlinkSeriesCalibre(",
        "async function loadSeriesAliasOverrides(",
        "settings.html unlinkSeriesCalibre",
    )
    check(
        "confirm(" in settings_unlink,
        "settings.html: the per-row unlink action has no confirmation step",
    )
    check(
        "showToast(" in settings_unlink and "releases_reset" in settings_unlink,
        "settings.html: the per-row unlink action does not toast the reset count",
    )
    check(
        "loadSeriesIdentityOverview(" in settings_unlink,
        "settings.html: the per-row unlink action does not refresh the overview list",
    )


def check_settings_pinned():
    source = read_text(SETTINGS_TEMPLATE)
    for element_id in SETTINGS_PINNED_IDS:
        check(
            'id="%s"' % element_id in source,
            "settings.html: pre-existing element id %r was lost" % element_id,
        )
    save_body = isolate(
        source, "async function saveSettings", "async function resetNyaaBackfillCursor",
        "settings.html saveSettings",
    )
    for key in SETTINGS_PINNED_PAYLOAD_KEYS:
        check(
            (key + ":") in save_body,
            "settings.html: saveSettings() dropped the pinned payload key %r" % key,
        )


# ---------------------------------------------------------------------------
# 3. sibling UI-guarded templates still intact
# ---------------------------------------------------------------------------
def check_cross_templates():
    for path, label, markers in CROSS_TEMPLATE_MARKERS:
        source = read_text(path)
        for marker in markers:
            check(
                marker in source,
                "%s: pinned literal %r was lost" % (label, marker),
            )


# ---------------------------------------------------------------------------
# 4. README
# ---------------------------------------------------------------------------
def check_readme():
    source = read_text(README_FILE)
    for marker in README_MARKERS:
        check(
            marker in source,
            "README.md: the Calibre-repair documentation does not mention %r" % marker,
        )


# ---------------------------------------------------------------------------
# 5. balanced Jinja delimiters
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
    check_series_repair_buttons()
    check_series_repair_js()
    check_series_pinned()
    check_settings_unlink()
    check_settings_pinned()
    check_cross_templates()
    check_readme()

    templates = (
        (SERIES_TEMPLATE, "series.html"),
        (SETTINGS_TEMPLATE, "settings.html"),
    )
    for path, label in templates:
        check_balanced_jinja(path, label)

    print(
        "calibre-repair-ui static guard PASSED: series.html per-row "
        "series-recheck-calibre-btn / series-unlink-calibre-btn (icons, tooltips, "
        "s.calibre_series_id gating, confirm text) + recheckSeriesCalibre/"
        "unlinkSeriesCalibre JS (disable, POST recheck-calibre/unlink-calibre, toast "
        "counts, reload), settings.html Series identity per-row Unlink Calibre, README "
        "false-In-Calibre repair docs, pinned-literals preserved and balanced Jinja "
        "delimiters in %d edited template(s)" % len(templates)
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
