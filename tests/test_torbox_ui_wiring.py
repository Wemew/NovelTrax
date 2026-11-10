#!/usr/bin/env python3
"""Static source-level guard for the TorBox UI, docs and deploy wiring.

Why static and not a render test: the interpreter used for the repo suites has
neither ``jinja2`` nor ``fastapi`` installed, so ``main.py`` cannot be imported
and the templates cannot be rendered.  This module therefore reads the template
/ docs / compose sources as text and proves:

1. ``settings.html`` renders the download-engine selector and the complete
   TorBox card (exact element ids required by the port contract), never renders
   the saved API key, offers a "Test TorBox" button wired to
   ``POST /api/downloader/torbox/test``, and ``saveSettings()`` submits every
   TorBox key as a string (``'true'``/``'false'`` for checkboxes) while keeping
   the pre-existing eight payload keys.
2. ``downloads.html`` keeps the qBittorrent status/config/queue (the pinned
   ``btn-*`` classes, column headers and ``qbit_*`` inputs), gains the engine
   selector + TorBox explanation, and under ``engine == 'torbox'`` renders the
   contract's TorBox queue (``Active TorBox Queue``, ``#torbox-last-checked``,
   ``Check now`` / two-step ``Clear failed``, ``#torbox-queue-body``, six
   ``Torbox Name / Size / Progress / Speed / State / Controls`` columns and
   ``torbox-row-{{ t.release_id }}`` rows).  Every JS helper named by the
   contract is defined, the retry/remove/poll/clear-failed endpoints are the
   documented paths, and the poller uses the 1 s active / 5 s idle cadence
   against ``GET /api/downloads/status``.
3. ``base.html`` keeps ``downloadRelease(releaseId, btn)`` POSTing to
   ``/api/releases/{id}/download`` (plus the toast/success contract that
   ``tests/test_ui_wiring.py`` pins) and adds the response-``state`` chip update
   with ``GET /api/releases/{id}/state`` polling every 1500 ms until a terminal
   state, with no page reload and ``skipped`` treated as success.
4. Both release partials render the ``release-state-chip`` markup after the
   existing status chip and keep the ``dl_target`` gating / button markup.
5. ``README.md``, both compose files and the ``Dockerfile`` document the TorBox
   engine, its torrent-only limitation, ``DOWNLOAD_DIR`` and the ``/downloads``
   volume/permissions.
6. Every edited template has balanced Jinja block/macro/expression delimiters.

Bare ``assert`` is never used (Calibre runs with ``sys.flags.optimize == 2``
and compiles bare asserts out, which would make this guard vacuous); an
explicit ``check()`` helper raises ``AssertionError`` instead.

Stdlib only: os, re, sys.  No third-party imports.

Usage::

    & $py tests\\run_tests.py torbox_ui_wiring
    & $py tests\\test_torbox_ui_wiring.py
"""

import os
import re
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "backend", "app", "templates")

SETTINGS_TEMPLATE = os.path.join(TEMPLATES_DIR, "settings.html")
DOWNLOADS_TEMPLATE = os.path.join(TEMPLATES_DIR, "downloads.html")
BASE_TEMPLATE = os.path.join(TEMPLATES_DIR, "base.html")
FLAT_PARTIAL = os.path.join(TEMPLATES_DIR, "partials", "flat_release_row_chunk.html")
SERIES_PARTIAL = os.path.join(TEMPLATES_DIR, "partials", "series_card_chunk.html")
README_FILE = os.path.join(PROJECT_ROOT, "README.md")
COMPOSE_FILE = os.path.join(PROJECT_ROOT, "docker-compose.yml")
TRUENAS_COMPOSE_FILE = os.path.join(PROJECT_ROOT, "docker-compose.truenas.yml")
DOCKERFILE = os.path.join(PROJECT_ROOT, "Dockerfile")
MAIN_PY = os.path.join(PROJECT_ROOT, "backend", "app", "main.py")

# --- settings.html contract -------------------------------------------------
SETTINGS_ENGINE_ID = 'id="setting-downloader-engine"'
SETTINGS_ENGINE_HELP = (
    "TorBox downloads Nyaa torrent releases into the local downloads folder; "
    "direct web links (J-Novels) are not supported."
)

SETTINGS_TORBOX_IDS = (
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
)

SETTINGS_TORBOX_PAYLOAD_KEYS = (
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

# Pre-existing payload keys pinned by earlier behaviour: they must not be lost.
SETTINGS_EXISTING_PAYLOAD_KEYS = (
    "calibre_path",
    "calibre_sync_interval_minutes",
    "feed_check_interval_minutes",
    "backfill_max_pages",
    "backfill_delay_seconds",
    "track_official_only",
    "discord_webhook_url",
    "apprise_url",
)

# --- downloads.html contract ------------------------------------------------
QBIT_PINNED_COLUMNS = ("Name", "Size", "Progress", "Speed", "Status", "Actions")
QBIT_PINNED_BUTTONS = ("btn-pause", "btn-resume", "btn-delete")
QBIT_PINNED_INPUTS = ("qbit_url", "qbit_username", "qbit_password", "qbit_category")

TORBOX_QUEUE_HEADERS = ("Torrent Name", "Size", "Progress", "Speed", "State", "Controls")

# Exact JS function names required by CONTRACT.md §7.2 (test contract).
TORBOX_JS_FUNCTIONS = (
    "refreshDownloadsNow",
    "renderTorboxQueue",
    "_torboxRowHtml",
    "_torboxProgressHtml",
    "_torboxStateHtml",
    "_torboxControlsHtml",
    "_torboxEmptyRow",
    "_dropTorboxRow",
    "_visibleFailedTorboxIds",
    "_ensureTorboxEmptyState",
    "retryTorbox",
    "removeTorbox",
    "clearFailedTorbox",
    "checkTorboxNow",
    "_queueConfirm",
    "_fmtMb",
    "_fmtEta",
)

TORBOX_ENDPOINTS = (
    "/api/downloads/status",
    "/api/downloads/torbox/poll",
    "/api/downloads/torbox/clear-failed",
    "/api/downloads/torbox/",
    "/retry",
    "/remove",
    "/api/settings",
)

# --- base.html contract -----------------------------------------------------
BASE_HELPER_SIGNATURE = "async function downloadRelease(releaseId, btn = null)"
BASE_DOWNLOAD_URL = "/api/releases/${releaseId}/download"
BASE_STATE_URL = "/api/releases/${releaseId}/state"
BASE_POLL_MS = "1500"
BASE_CHIP_CLASS = "release-state-chip"

# --- partials contract ------------------------------------------------------
CHIP_STATE_ATTR = 'data-state="{{ r.download_state.state }}"'
CHIP_LABEL = "r.download_state.label"
CHIP_ERROR = "r.download_state.error"
CHIP_CONDITION = "{% if r.download_state %}"
DOWNLOAD_BUTTON_TITLE = 'title="Send to downloader (engine selected in Settings)"'
DOWNLOAD_CALL = "downloadRelease({{ r.id }}, this)"
DISABLED_BUTTON_TITLE = 'title="No torrent or magnet download URL available for this release"'

# Contract §7.2 phase labels that the JavaScript renderer must emit.
TORBOX_PHASE_LABELS = (
    "Fetching ",
    "Ready — fetch queued",
    "Processing…",
    "Processing on TorBox…",
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


def count_matches(pattern, source):
    return len(re.findall(pattern, source))


def isolate(source, start_marker, end_marker, label):
    """Return the source slice between two markers, failing when not found."""
    start = source.find(start_marker)
    check(
        start != -1,
        "%s: could not find start marker %r" % (label, start_marker),
    )
    end = source.find(end_marker, start + len(start_marker))
    check(
        end != -1,
        "%s: could not find end marker %r after %r"
        % (label, end_marker, start_marker),
    )
    return source[start:end]


# ---------------------------------------------------------------------------
# 1. settings.html
# ---------------------------------------------------------------------------
def check_settings_engine_selector():
    source = read_text(SETTINGS_TEMPLATE)

    check(
        SETTINGS_ENGINE_ID in source,
        "settings.html: missing the download-engine <select> %s" % SETTINGS_ENGINE_ID,
    )
    check(
        '<option value="qbit"' in source,
        "settings.html: the engine selector has no qbit option",
    )
    check(
        '<option value="torbox"' in source,
        "settings.html: the engine selector has no torbox option",
    )
    check(
        SETTINGS_ENGINE_HELP in source,
        "settings.html: missing the engine help text %r" % SETTINGS_ENGINE_HELP,
    )
    check(
        "settings.downloader_engine" in source,
        "settings.html: the engine selector is not bound to settings.downloader_engine",
    )


def check_settings_torbox_card():
    source = read_text(SETTINGS_TEMPLATE)

    check("TorBox" in source, "settings.html: no TorBox card is documented in the UI")
    for element_id in SETTINGS_TORBOX_IDS:
        check(
            'id="%s"' % element_id in source,
            "settings.html: missing the TorBox element id %r" % element_id,
        )

    # API key: password input, empty value, masked placeholder, never the key.
    key_index = source.find('id="setting-torbox-api-key"')
    check(key_index != -1, "settings.html: missing the TorBox API key input")
    check(
        'type="password"' in source[max(0, key_index - 200):key_index],
        "settings.html: the TorBox API key input is not type=password",
    )
    check(
        'name="torbox_api_key"' in source,
        "settings.html: the TorBox API key input is not named torbox_api_key",
    )
    check(
        'setting-torbox-api-key" name="torbox_api_key" value=""' in source,
        "settings.html: the TorBox API key input must ship with an empty value",
    )
    check(
        "value=\"{{ settings.torbox_api_key" not in source,
        "settings.html: the TorBox API key must never be rendered back into the HTML",
    )
    check(
        "torbox_api_key_set" in source,
        "settings.html: the API key placeholder is not gated on torbox_api_key_set",
    )
    check(
        "API key saved — leave blank to keep" in source,
        "settings.html: missing the masked API key placeholder text",
    )

    # Test button + result surface + endpoint.
    check(
        "Test TorBox" in source,
        "settings.html: missing the 'Test TorBox' button label",
    )
    check(
        "testTorboxConnection()" in source,
        "settings.html: the Test TorBox button is not wired to testTorboxConnection()",
    )
    check(
        "/api/downloader/torbox/test" in source,
        "settings.html: testTorboxConnection() does not POST /api/downloader/torbox/test",
    )
    check(
        'id="torbox-test-result"' in source,
        "settings.html: missing the #torbox-test-result result box",
    )


def check_settings_save_payload():
    source = read_text(SETTINGS_TEMPLATE)
    body = isolate(source, "async function saveSettings", "async function debugAction", "settings.html")

    for key in SETTINGS_TORBOX_PAYLOAD_KEYS:
        check(
            (key + ":") in body,
            "settings.html: saveSettings() payload is missing the key %r" % key,
        )
    for key in SETTINGS_EXISTING_PAYLOAD_KEYS:
        check(
            (key + ":") in body,
            "settings.html: saveSettings() dropped the pre-existing key %r" % key,
        )
    check(
        body.count("checked ? 'true' : 'false'") >= 4,
        "settings.html: TorBox checkboxes are not serialised as 'true'/'false' strings",
    )
    check(
        "document.getElementById('setting-torbox-api-key').value" in body,
        "settings.html: saveSettings() does not submit the API key input value",
    )
    check(
        "document.getElementById('setting-downloads-dir').value" in body,
        "settings.html: saveSettings() does not submit the downloads folder input",
    )


# ---------------------------------------------------------------------------
# 2. downloads.html
# ---------------------------------------------------------------------------
def check_downloads_qbit_preserved():
    source = read_text(DOWNLOADS_TEMPLATE)

    check(
        "Downloader & Queue Management" in source,
        "downloads.html: lost the pinned page header 'Downloader & Queue Management'",
    )
    for column in QBIT_PINNED_COLUMNS:
        check(
            (">" + column + "<") in source,
            "downloads.html: lost the pinned qBittorrent column header %r" % column,
        )
    for button_class in QBIT_PINNED_BUTTONS:
        check(
            button_class in source,
            "downloads.html: lost the pinned control button class %r" % button_class,
        )
    for field in QBIT_PINNED_INPUTS:
        check(
            'id="%s"' % field in source,
            "downloads.html: lost the pinned qBittorrent settings input %r" % field,
        )
    check(
        'id="downloader-config-panel"' in source,
        "downloads.html: lost the in-app downloader config panel",
    )
    check(
        "toggleDownloaderConfig" in source and "saveDownloaderConfig" in source,
        "downloads.html: lost the qBittorrent config panel handlers",
    )
    for handler in ("pauseTorrent", "resumeTorrent", "deleteTorrent"):
        check(
            "function %s(" % handler in source,
            "downloads.html: lost the qBittorrent queue handler %r" % handler,
        )


def check_downloads_engine_selector():
    source = read_text(DOWNLOADS_TEMPLATE)

    check(
        'id="downloader-engine"' in source,
        "downloads.html: the config panel has no download-engine selector",
    )
    check(
        "saveDownloaderEngine" in source,
        "downloads.html: the download-engine selector has no save handler",
    )
    check(
        '<option value="torbox"' in source and '<option value="qbit"' in source,
        "downloads.html: the engine selector does not offer qbit and torbox",
    )
    check(
        "{% if engine == 'torbox' %}" in source,
        "downloads.html: the TorBox queue is not gated on {% if engine == 'torbox' %}",
    )
    check(
        "TorBox handles torrent releases only" in source,
        "downloads.html: missing the TorBox torrent-only explanation",
    )
    check(
        "J-Novels" in source,
        "downloads.html: the TorBox explanation does not mention the unsupported J-Novels links",
    )


def check_downloads_torbox_queue():
    source = read_text(DOWNLOADS_TEMPLATE)

    check(
        "Active TorBox Queue" in source,
        "downloads.html: missing the 'Active TorBox Queue' section header",
    )
    check(
        'id="torbox-last-checked"' in source,
        "downloads.html: missing the #torbox-last-checked timestamp",
    )
    check(
        'id="torbox-queue-body"' in source,
        "downloads.html: missing the #torbox-queue-body tbody",
    )
    check(
        "torbox-row-{{ t.release_id }}" in source,
        "downloads.html: TorBox rows do not use the torbox-row-{{ t.release_id }} id",
    )
    check(
        'data-release-id="{{ t.release_id }}"' in source,
        "downloads.html: TorBox rows do not carry data-release-id",
    )
    for header in TORBOX_QUEUE_HEADERS:
        check(
            (">" + header + "<") in source,
            "downloads.html: missing the TorBox queue column header %r" % header,
        )
    check(
        "Check now" in source and "checkTorboxNow()" in source,
        "downloads.html: missing the 'Check now' button wired to checkTorboxNow()",
    )
    check(
        "Clear failed" in source and "clearFailedTorbox()" in source,
        "downloads.html: missing the 'Clear failed' button wired to clearFailedTorbox()",
    )
    check(
        "retryTorbox(" in source and "removeTorbox(" in source,
        "downloads.html: missing the TorBox retry/remove controls",
    )
    check(
        'title="Retry TorBox download"' in source,
        "downloads.html: failed TorBox rows do not expose a Retry button title",
    )
    for endpoint in TORBOX_ENDPOINTS:
        check(
            endpoint in source,
            "downloads.html: missing the required endpoint literal %r" % endpoint,
        )
    check(
        "`/api/downloads/torbox/${id}/retry`" in source,
        "downloads.html: retryTorbox() does not POST /api/downloads/torbox/{id}/retry",
    )
    check(
        "`/api/downloads/torbox/${id}/remove`" in source,
        "downloads.html: removeTorbox() does not POST /api/downloads/torbox/{id}/remove",
    )


def check_downloads_js_contract():
    source = read_text(DOWNLOADS_TEMPLATE)

    for function_name in TORBOX_JS_FUNCTIONS:
        check(
            "function %s(" % function_name in source,
            "downloads.html: missing the TorBox JS helper function %r" % function_name,
        )

    # Polling cadence: 1 s active / 5 s idle, in-place updates only.
    check(
        "TORBOX_POLL_ACTIVE_MS = 1000" in source,
        "downloads.html: missing the 1 s active poll constant",
    )
    check(
        "TORBOX_POLL_IDLE_MS = 5000" in source,
        "downloads.html: missing the 5 s idle poll constant",
    )
    check(
        "setTimeout(refreshDownloadsNow" in source,
        "downloads.html: the TorBox queue poller does not use setTimeout(refreshDownloadsNow, ...)",
    )
    check(
        "clearTimeout(_torboxPollTimer)" in source,
        "downloads.html: the TorBox poller never clears its pending timer",
    )
    check(
        "renderTorboxQueue(data.torrents || [])" in source,
        "downloads.html: refreshDownloadsNow() does not render data.torrents from /api/downloads/status",
    )
    check(
        "refreshDownloadsNow();" in source,
        "downloads.html: the TorBox poller is never started on page load",
    )

    # Phase labels required by CONTRACT.md §7.2.
    for label in TORBOX_PHASE_LABELS:
        check(
            label in source,
            "downloads.html: missing the TorBox phase label %r" % label,
        )

    # Failed rows surface the error text (escaped) and a retry control.
    check(
        "_esc(t.error)" in source,
        "downloads.html: failed TorBox error text is not escaped",
    )
    check(
        "_queueConfirm(" in source and "< 4000" in source,
        "downloads.html: _queueConfirm() is not the 4 s double-click confirm",
    )
    # Two-step clear-failed confirm.
    clear_body = isolate(
        source, "async function clearFailedTorbox", "// Start the TorBox queue poller", "downloads.html"
    )
    check(
        "_queueConfirm(" in clear_body,
        "downloads.html: clearFailedTorbox() is not a two-step confirm",
    )
    check(
        "/api/downloads/torbox/clear-failed" in clear_body,
        "downloads.html: clearFailedTorbox() does not POST clear-failed",
    )


# ---------------------------------------------------------------------------
# 3. base.html
# ---------------------------------------------------------------------------
def check_base_download_release():
    source = read_text(BASE_TEMPLATE)

    check(
        BASE_HELPER_SIGNATURE in source,
        "base.html: missing the global helper %r" % BASE_HELPER_SIGNATURE,
    )
    check(
        BASE_DOWNLOAD_URL in source,
        "base.html: downloadRelease() does not POST %r" % BASE_DOWNLOAD_URL,
    )
    check(
        BASE_STATE_URL in source,
        "base.html: downloadRelease() does not poll %r" % BASE_STATE_URL,
    )
    check(
        BASE_POLL_MS in source,
        "base.html: the release-state poller does not use the 1500 ms cadence",
    )
    check(
        "document.hidden" in source,
        "base.html: the release-state poller does not skip while the tab is hidden",
    )
    check(
        "data.status === 'skipped'" in source,
        "base.html: downloadRelease() does not treat status 'skipped' as success",
    )
    check(
        "can_download" in source and "btn.disabled = !state.can_download" in source,
        "base.html: downloadRelease() does not enable/disable the button from state.can_download",
    )
    check(
        "chip.dataset.state" in source and "data-state" in source,
        "base.html: downloadRelease() does not update the chip data-state",
    )
    check(
        "chip.setAttribute('data-state'" in source,
        "base.html: downloadRelease() does not write the chip data-state attribute",
    )
    check(
        BASE_CHIP_CLASS in source,
        "base.html: downloadRelease() does not look up the %r chip" % BASE_CHIP_CLASS,
    )

    body = isolate(
        source, "async function downloadRelease", "async function toggleSeries", "base.html"
    )
    check(
        "showToast(data.message" in body,
        "base.html: downloadRelease() no longer surfaces data.message through showToast",
    )
    check(
        "data.status === 'success'" in body,
        "base.html: downloadRelease() no longer checks status == 'success'",
    )
    check(
        "method: 'POST'" in body,
        "base.html: downloadRelease() does not issue an HTTP POST",
    )
    check(
        "btn.disabled = true" in body and "btn.disabled = false" in body,
        "base.html: downloadRelease() does not disable/re-enable the button",
    )
    check(
        "finally" in body,
        "base.html: downloadRelease() has no finally block to restore the button",
    )
    check(
        "location.reload" not in body,
        "base.html: downloadRelease() must update in place and never reload the page",
    )
    check(
        "_applyReleaseState(data.state, btn)" in body,
        "base.html: downloadRelease() does not apply the response state",
    )
    check(
        "_pollReleaseState(releaseId, btn)" in body,
        "base.html: downloadRelease() does not start the release-state poller",
    )


# ---------------------------------------------------------------------------
# 4. release partials
# ---------------------------------------------------------------------------
def check_release_chip_markup(path, label):
    source = read_text(path)

    check(
        CHIP_CONDITION in source,
        "%s: the download-state chip is not gated on the r.download_state condition"
        % label,
    )
    check(
        BASE_CHIP_CLASS in source,
        "%s: missing the release-state-chip markup" % label,
    )
    check(
        CHIP_STATE_ATTR in source,
        "%s: missing data-state=\"{{ r.download_state.state }}\" on the chip" % label,
    )
    check(
        CHIP_LABEL in source,
        "%s: the chip does not render r.download_state.label" % label,
    )
    check(
        CHIP_ERROR in source,
        "%s: the chip title does not include r.download_state.error" % label,
    )
    check(
        DOWNLOAD_BUTTON_TITLE in source,
        "%s: the download button title is not the engine-agnostic text" % label,
    )
    check(
        DOWNLOAD_CALL in source,
        "%s: lost the downloadRelease({{ r.id }}, this) button wiring" % label,
    )
    check(
        DISABLED_BUTTON_TITLE in source,
        "%s: lost the disabled no-torrent download fallback title" % label,
    )
    check(
        "r.download_url" in source and "r.link" in source,
        "%s: lost the dl_target gating on r.download_url/r.link" % label,
    )
    check(
        "{% set dl_target" in source,
        "%s: lost the dl_target guard that gates the download button" % label,
    )
    check(
        source.count('data-release-id="{{ r.id }}"') >= 2,
        "%s: expected data-release-id on both the enabled and the disabled button" % label,
    )


# ---------------------------------------------------------------------------
# 5. docs / compose / Dockerfile
# ---------------------------------------------------------------------------
def check_docs_and_deploy():
    readme = read_text(README_FILE)
    compose = read_text(COMPOSE_FILE)
    truenas = read_text(TRUENAS_COMPOSE_FILE)
    dockerfile = read_text(DOCKERFILE)

    for needle in (
        "TorBox",
        "torbox.app",
        "DOWNLOAD_DIR",
        "/downloads",
        "torrent",
        "J-Novels",
        "tests/test_torbox_downloader.py",
        "tests/test_torbox_manager.py",
        "tests/test_torbox_ui_wiring.py",
        "run_tests.py torbox",
        "torbox_api_key",
    ):
        check(
            needle in readme,
            "README.md: does not document %r (TorBox engine setup/docs)" % needle,
        )

    check(
        "DOWNLOADS_DIR" not in compose,
        "docker-compose.yml: typo DOWNLOADS_DIR (the env var is DOWNLOAD_DIR)",
    )
    for needle in ("DOWNLOAD_DIR=/downloads", "./downloads:/downloads", "TorBox"):
        check(
            needle in compose,
            "docker-compose.yml: missing %r (downloads volume/env)" % needle,
        )
    for needle in ("DOWNLOAD_DIR=/downloads", "/downloads", "downloads:/downloads"):
        check(
            needle in truenas,
            "docker-compose.truenas.yml: missing %r (downloads volume/env)" % needle,
        )
    check(
        "ENV DOWNLOAD_DIR=/downloads" in dockerfile,
        "Dockerfile: missing ENV DOWNLOAD_DIR=/downloads",
    )
    check(
        "/downloads" in dockerfile and "mkdir -p" in dockerfile,
        "Dockerfile: the container does not create the /downloads folder",
    )
    check(
        "chmod 777 /app/data /calibre /downloads" in dockerfile,
        "Dockerfile: /downloads is not made writable for any unprivileged UID",
    )
    check(
        "chown -R appuser:appuser /app /calibre /downloads" in dockerfile,
        "Dockerfile: /downloads is not owned by the app user",
    )


def check_main_py_contract():
    """The UI still targets the endpoint the pre-existing main.py exposes."""
    main_source = read_text(MAIN_PY)
    check(
        '@app.post("/api/releases/{release_id}/download")' in main_source,
        "main.py: the release download endpoint the UI POSTs to is missing",
    )


# ---------------------------------------------------------------------------
# 6. balanced Jinja delimiters
# ---------------------------------------------------------------------------
def check_balanced_jinja(path, label):
    """Balanced Jinja block/macro tags and balanced expression delimiters."""
    source = read_text(path)

    block_pairs = (
        ("if/endif", r"\{%-?\s*if\b", r"\{%-?\s*endif\s*-?%\}"),
        ("for/endfor", r"\{%-?\s*for\b", r"\{%-?\s*endfor\s*-?%\}"),
        ("block/endblock", r"\{%-?\s*block\b", r"\{%-?\s*endblock\s*-?%\}"),
        ("macro/endmacro", r"\{%-?\s*macro\b", r"\{%-?\s*endmacro\s*-?%\}"),
    )
    for name, open_pattern, close_pattern in block_pairs:
        opened = count_matches(open_pattern, source)
        closed = count_matches(close_pattern, source)
        check(
            opened == closed,
            "%s: unbalanced Jinja %s - %d opening tag(s) vs %d closing tag(s)"
            % (label, name, opened, closed),
        )

    open_tag = source.count("{%")
    close_tag = source.count("%}")
    check(
        open_tag == close_tag,
        "%s: unbalanced Jinja statement delimiters - %d '{%%' vs %d '%%}'"
        % (label, open_tag, close_tag),
    )

    open_expr = source.count("{{")
    close_expr = source.count("}}")
    check(
        open_expr == close_expr,
        "%s: unbalanced Jinja expression delimiters - %d '{{' vs %d '}}'"
        % (label, open_expr, close_expr),
    )


def main():
    check_settings_engine_selector()
    check_settings_torbox_card()
    check_settings_save_payload()

    check_downloads_qbit_preserved()
    check_downloads_engine_selector()
    check_downloads_torbox_queue()
    check_downloads_js_contract()

    check_base_download_release()

    check_release_chip_markup(FLAT_PARTIAL, "partials/flat_release_row_chunk.html")
    check_release_chip_markup(SERIES_PARTIAL, "partials/series_card_chunk.html")

    check_docs_and_deploy()
    check_main_py_contract()

    templates = (
        (SETTINGS_TEMPLATE, "settings.html"),
        (DOWNLOADS_TEMPLATE, "downloads.html"),
        (BASE_TEMPLATE, "base.html"),
        (FLAT_PARTIAL, "partials/flat_release_row_chunk.html"),
        (SERIES_PARTIAL, "partials/series_card_chunk.html"),
    )
    for path, label in templates:
        check_balanced_jinja(path, label)

    print(
        "torbox-ui-wiring static guard PASSED: settings engine selector + TorBox "
        "card/test/save payload, downloads engine-aware TorBox queue + JS helpers "
        "+ 1 s/5 s polling, base.html download-state chip + /state polling, release "
        "chips in both partials, docs/compose/Dockerfile DOWNLOAD_DIR wiring, and "
        "balanced Jinja delimiters in %d edited template(s)" % len(templates)
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
