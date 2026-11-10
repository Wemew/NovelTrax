#!/usr/bin/env python3
"""Static source-level guard for the AniList publication metadata + Nyaa bundle-inference UI.

Why static and not a render test: the interpreter used for the repo suites has neither
``jinja2`` nor ``fastapi`` installed, so the templates cannot be rendered.  This suite
reads the templates, host templates and ``README.md`` as text and proves the
``.agents/volume_inference/CONTRACT.md`` §6 (owner D) deliverables are wired:

1. ``series.html`` renders the compact ``.series-publication-line`` under the existing
   name / ``Tracks:`` / ``Calibre ID:`` block, built only from the parts present
   (``anilist_format_label`` · ``anilist_status_label`` · ``total_volumes`` vols) with
   the exact "AniList publication metadata (Format · Status · Volumes)" tooltip — while
   keeping every literal pinned by the series-identity/UI-wiring suites (display title,
   Tracks line, Calibre ID line, ``.series-match-btn`` / ``.series-anilist-btn``, the
   series match modal ids/JS and the row attributes).
2. ``settings.html`` adds ``#setting-nyaa-bundle-inference`` to the Automation & Polling
   card (default checked unless the setting is false/0/no/off), the "Infer bundle volumes
   from AniList" label + help text, and submits ``nyaa_bundle_volume_inference`` in
   ``saveSettings()`` as the usual ``checked ? 'true' : 'false'`` string — keeping every
   existing settings id and payload key.
3. ``README.md`` documents the AniList Format/Status/Volumes fields, the Nyaa unmarked
   bundle-inference rule and its limits, the ``nyaa_bundle_volume_inference`` toggle and
   the The Executioner and Her Way of Life → Vol 1–11 example.
4. The flat release row / series card still render the ``Bundle: Vol x–y`` marker from
   ``r.volumes`` / ``r.is_bundle`` (so an inferred bundle is visible), and the shared
   UI-guarded templates keep their pinned literals.
5. Every edited template has balanced Jinja block/expression/comment delimiters.

Bare ``assert`` is never used (Calibre runs with ``sys.flags.optimize == 2`` and
compiles bare asserts out, which would make this guard vacuous); an explicit
``check()`` helper raises ``AssertionError`` instead.

Stdlib only: os, re, sys.  No third-party imports, no network, no database.

Usage::

    & $py tests\\run_tests.py bundle_inference_ui
    & $py tests\\test_bundle_inference_ui.py
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
DISCOVERY_TEMPLATE = os.path.join(TEMPLATES_DIR, "discovery.html")
LOGS_TEMPLATE = os.path.join(TEMPLATES_DIR, "logs.html")
README_FILE = os.path.join(PROJECT_ROOT, "README.md")

# --- §6 series.html -------------------------------------------------------
PUBLICATION_LINE_CLASS = "series-publication-line"
PUBLICATION_TOOLTIP = "AniList publication metadata (Format · Status · Volumes)"
PUBLICATION_FIELDS = (
    "s.anilist_format_label",
    "s.anilist_status_label",
    "s.total_volumes",
)
PUBLICATION_PART_GUARDS = (
    "{% if pub_format %}",
    "{% if pub_status %}",
    "{% if pub_volumes %}",
    "{% if pub_format or pub_status or pub_volumes %}",
)
PUBLICATION_LITERALS = (
    " · ",
    " vols",
)

# literals pinned by tests/test_series_match_ui.py + tests/test_ui_wiring.py.
SERIES_PINNED = (
    '{% extends "base.html" %}',
    "{{ s.display_title or s.name }}",
    "Tracks: {{ s.name }}",
    "Calibre ID: #{{ s.calibre_series_id }}",
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
    "/api/calibre/series",
    "/api/series/${seriesId}/match-calibre",
)

# --- §6 settings.html -----------------------------------------------------
NYAA_SETTING_ID = 'id="setting-nyaa-bundle-inference"'
NYAA_SETTING_KEY = "nyaa_bundle_volume_inference"
NYAA_SETTING_LABEL = "Infer bundle volumes from AniList"
NYAA_SETTING_HELP = (
    "Unmarked Nyaa light-novel batch releases are treated as the complete series "
    "when AniList reports Format: Light Novel, Status: Finished and a volume count."
)
NYAA_DEFAULT_GUARD = (
    "settings.get('nyaa_bundle_volume_inference', 'true')|lower not in "
    "['false', '0', 'no', 'off']"
)
NYAA_PAYLOAD_GUARD = (
    "nyaa_bundle_volume_inference: document.getElementById("
    "'setting-nyaa-bundle-inference').checked ? 'true' : 'false'"
)

# A representative set of settings ids/sections pinned by older suites.
SETTINGS_PINNED_IDS = (
    "setting-calibre-path",
    "setting-calibre-interval",
    "setting-feed-interval",
    "setting-feed-catchup-enabled",
    "setting-feed-catchup-pages",
    "setting-backfill-pages",
    "setting-backfill-include-nyaa",
    "setting-backfill-delay",
    "setting-official-only",
    "setting-downloader-engine",
    "setting-torbox-enabled",
    "setting-torbox-api-key",
    "setting-downloads-dir",
    "setting-timezone",
    "setting-log-level",
    "setting-activity-retention",
    "setting-activity-max-rows",
    "setting-webhook-new-request",
    "setting-webhook-acquired",
    "series-identity-search",
    "series-identity-results",
    "storage-health-result",
    "network-diagnostics-result",
    "settings-dirty-badge",
    "btn-clear-metadata",
    "btn-test-torbox",
    "btn-reset-nyaa-backfill",
)

SETTINGS_PINNED_PAYLOAD_KEYS = (
    "calibre_path",
    "calibre_sync_interval_minutes",
    "feed_check_interval_minutes",
    "feed_catchup_enabled",
    "feed_catchup_max_pages",
    "backfill_max_pages",
    "backfill_include_nyaa",
    "backfill_delay_seconds",
    "track_official_only",
    "discord_webhook_url",
    "apprise_url",
    "webhook_new_request_notifications",
    "webhook_acquired_notifications",
    "downloader_engine",
    "torbox_enabled",
    "torbox_api_key",
    "torbox_download_concurrency",
    "downloads_dir",
    "app_timezone",
    "log_level",
    "activity_retention_days",
    "activity_max_rows",
)

# --- bundle rendering (must remain visible for inferred bundles) -----------
FLAT_BUNDLE_MARKERS = (
    "{% if r.is_bundle or (r.volumes and r.volumes|length > 1) %}",
    "Bundle: Vol {{ r.volumes[0] }}–{{ r.volumes[-1] }}",
    "r.missing_volumes",
)
SERIES_CARD_BUNDLE_MARKERS = (
    "{% if r.is_bundle or (r.volumes and r.volumes|length > 1) %}",
    "Bundle: {{ r.volumes|length }} vols",
)

# --- UI-guarded sibling templates that must keep working -------------------
CROSS_TEMPLATE_MARKERS = (
    (DISCOVERY_PARTIAL, "partials/discovery_card_chunk.html", (
        'class="discovery-card',
        "track-btn-{{ item.id }}",
        "blacklist-btn-{{ item.id }}",
        "Track Series",
        "handleTrackBtn(this)",
    )),
    (SERIES_CARD_PARTIAL, "partials/series_card_chunk.html", (
        'class="series-card',
        'loading="lazy"',
        'decoding="async"',
        "cover-placeholder",
        "downloadRelease({{ r.id }}, this)",
        "release-state-chip",
        "release-rebind-btn",
        "release-pinned-badge",
    )),
    (BASE_TEMPLATE, "base.html", (
        'id="release-rebind-modal"',
        "function openReleaseRebindModal(",
        "async function downloadRelease(releaseId, btn = null)",
        "function showToast(message, type = 'success')",
        'id="image-modal"',
    )),
    (DISCOVERY_TEMPLATE, "discovery.html", (
        "textContent",
        "function handleTrackBtn(",
        "data.nyaa_next_page",
    )),
    (LOGS_TEMPLATE, "logs.html", (
        '|localtime("%Y-%m-%d %H:%M:%S")',
        'title="{{ log.created_at }}"',
    )),
)

# --- README ---------------------------------------------------------------
README_MARKERS = (
    "AniList Publication Metadata & Nyaa Bundle Volume Inference",
    "Format",
    "Status",
    "Volumes",
    "total_volumes",
    "Light Novel",
    "Finished",
    "nyaa_bundle_volume_inference",
    "Infer bundle volumes from AniList",
    "unmarked Nyaa light-novel batch",
    "complete series",
    "is_bundle",
    "volume_inferred",
    "The Executioner and Her Way of Life",
    "Vol 1–11",
    "Limits",
    "Nyaa feeds only",
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
# 1. series.html publication line
# ---------------------------------------------------------------------------
def check_series_publication_line():
    source = read_text(SERIES_TEMPLATE)
    check(
        PUBLICATION_LINE_CLASS in source,
        "series.html: missing the %r meta line" % PUBLICATION_LINE_CLASS,
    )
    for field in PUBLICATION_FIELDS:
        check(
            field in source,
            "series.html: the publication line does not read %r" % field,
        )
    for guard in PUBLICATION_PART_GUARDS:
        check(
            guard in source,
            "series.html: the publication line does not gate presence with %r" % guard,
        )
    check(
        PUBLICATION_TOOLTIP in source,
        "series.html: missing the publication tooltip %r" % PUBLICATION_TOOLTIP,
    )
    for literal in PUBLICATION_LITERALS:
        check(
            literal in source,
            "series.html: the publication line is missing the literal %r" % literal,
        )
    # Placement: inside the same name block, after the Calibre ID line.
    calibre_index = source.find("Calibre ID: #{{ s.calibre_series_id }}")
    publication_index = source.find(PUBLICATION_LINE_CLASS)
    check(calibre_index != -1, "series.html: the Calibre ID line was lost")
    check(
        calibre_index < publication_index,
        "series.html: the publication line is not placed after the Calibre ID line",
    )
    block_start = source.rfind('<div class="min-w-0">', 0, publication_index)
    check(
        block_start != -1,
        "series.html: the publication line is not inside the series-name block",
    )
    check(
        'class="series-publication-line' in source,
        "series.html: the publication line class is not attached to its span",
    )


def check_series_pinned():
    source = read_text(SERIES_TEMPLATE)
    for marker in SERIES_PINNED:
        check(
            marker in source,
            "series.html: pinned literal %r was lost" % marker,
        )


# ---------------------------------------------------------------------------
# 2. settings.html Nyaa bundle-inference toggle
# ---------------------------------------------------------------------------
def check_settings_bundle_toggle():
    source = read_text(SETTINGS_TEMPLATE)

    check(NYAA_SETTING_ID in source, "settings.html: missing the %r checkbox" % NYAA_SETTING_ID)
    check(
        NYAA_SETTING_LABEL in source,
        "settings.html: missing the %r label" % NYAA_SETTING_LABEL,
    )
    check(
        NYAA_SETTING_HELP in source,
        "settings.html: missing the bundle-inference help text",
    )
    check(
        NYAA_DEFAULT_GUARD in source,
        "settings.html: the checkbox is not default-checked unless the setting is "
        "false/0/no/off",
    )
    check(
        "checked{% endif %}" in source[
            source.find(NYAA_SETTING_ID):source.find(NYAA_SETTING_ID) + 420
        ],
        "settings.html: the Nyaa bundle-inference checkbox has no checked state",
    )
    check(
        NYAA_SETTING_KEY in source,
        "settings.html: the %r setting key is not referenced" % NYAA_SETTING_KEY,
    )

    # Placement inside the Automation & Polling card (between its header and the
    # Download Engine section that follows it).
    automation_index = source.find("Automation & Polling Intervals")
    download_index = source.find("Download Engine", automation_index)
    checkbox_index = source.find(NYAA_SETTING_ID)
    check(automation_index != -1, "settings.html: the Automation & Polling card was lost")
    check(download_index != -1, "settings.html: the Download Engine card was lost")
    check(
        automation_index < checkbox_index < download_index,
        "settings.html: the Nyaa bundle-inference checkbox is not inside the "
        "Automation & Polling card",
    )

    save_body = isolate(
        source, "async function saveSettings", "async function resetNyaaBackfillCursor",
        "settings.html saveSettings",
    )
    check(
        NYAA_PAYLOAD_GUARD in save_body,
        "settings.html: saveSettings() does not submit "
        "nyaa_bundle_volume_inference as 'true'/'false'",
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
# 3. inferred bundle still renders as a bundle
# ---------------------------------------------------------------------------
def check_bundle_rendering():
    flat = read_text(FLAT_PARTIAL)
    for marker in FLAT_BUNDLE_MARKERS:
        check(
            marker in flat,
            "partials/flat_release_row_chunk.html: the flat release row no longer "
            "renders the bundle marker %r" % marker,
        )
    card = read_text(SERIES_CARD_PARTIAL)
    for marker in SERIES_CARD_BUNDLE_MARKERS:
        check(
            marker in card,
            "partials/series_card_chunk.html: the series card no longer renders the "
            "bundle marker %r" % marker,
        )


# ---------------------------------------------------------------------------
# 4. sibling UI-guarded templates still intact
# ---------------------------------------------------------------------------
def check_cross_templates():
    for path, label, markers in CROSS_TEMPLATE_MARKERS:
        source = read_text(path)
        for marker in markers:
            check(
                marker in source,
                "%s: pinned literal %r was lost" % (label, marker),
            )
    discovery = read_text(DISCOVERY_TEMPLATE)
    check(
        "box.outerHTML =" not in discovery,
        "discovery.html: raw outerHTML injection is back",
    )


# ---------------------------------------------------------------------------
# 5. README
# ---------------------------------------------------------------------------
def check_readme():
    source = read_text(README_FILE)
    for marker in README_MARKERS:
        check(
            marker in source,
            "README.md: the publication/inference documentation does not mention %r"
            % marker,
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
    check_series_publication_line()
    check_series_pinned()
    check_settings_bundle_toggle()
    check_settings_pinned()
    check_bundle_rendering()
    check_cross_templates()
    check_readme()

    templates = (
        (SERIES_TEMPLATE, "series.html"),
        (SETTINGS_TEMPLATE, "settings.html"),
        (FLAT_PARTIAL, "partials/flat_release_row_chunk.html"),
        (SERIES_CARD_PARTIAL, "partials/series_card_chunk.html"),
    )
    for path, label in templates:
        check_balanced_jinja(path, label)

    print(
        "bundle-inference-ui static guard PASSED: series.html .series-publication-line "
        "from anilist_format_label/anilist_status_label/total_volumes with the pinned "
        "tooltip and series pinned literals, settings.html #setting-nyaa-bundle-inference "
        "default-on toggle + help + saveSettings payload key, flat/card Bundle: Vol "
        "rendering preserved, cross-template pinned literals intact, README publication/"
        "inference docs, and balanced Jinja delimiters in %d edited template(s)"
        % len(templates)
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
