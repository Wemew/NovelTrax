#!/usr/bin/env python3
"""Static source-level guard for the NovelTrax UI wiring.

Covers two gaps this milestone closes:

1. Every release row/card exposes a "Download" action that POSTs the release to
   ``/api/releases/{release_id}/download`` through the global ``downloadRelease``
   helper in ``base.html``, gated on the same ``download_url``/``link`` fields the
   endpoint itself reads from the database.
2. The "Add New Feed" preset dropdown in ``feeds.html`` offers the Nyaa
   category 3_1 preset, and ``onFeedTypeChange()`` fills the provider's default
   category 3_1 RSS URL for it.
3. The dedicated Audiobooks section exists end-to-end at the source level: the
   ``/audiobooks`` page route in ``main.py`` reads the two audiobook database
   methods, ``base.html`` links it in the nav with a headphones icon and the
   ``active_page == 'audiobooks'`` active state, and ``audiobooks.html`` renders
   an empty state plus an escaped, ``|localdate``-formatted table of feed rows.

Why static instead of a render test: the Calibre embedded interpreter on this
machine has neither ``jinja2`` nor ``fastapi`` installed, so templates cannot be
rendered here.  This module therefore only reads the template sources as text,
proves the required markup/handler is present, and proves the Jinja block and
expression delimiters are balanced.

Stdlib only: os, re, sys.  No third-party imports.

Bare ``assert`` is never used: Calibre runs with ``sys.flags.optimize == 2`` and
compiles asserts out, which would make this guard vacuous.

Usage::

    & "C:\\Program Files\\Calibre2\\calibre-debug.exe" -e tests\\test_ui_wiring.py
"""

import os
import re
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "backend", "app", "templates")

FLAT_PARTIAL = os.path.join(
    TEMPLATES_DIR, "partials", "flat_release_row_chunk.html"
)
SERIES_PARTIAL = os.path.join(
    TEMPLATES_DIR, "partials", "series_card_chunk.html"
)
BASE_TEMPLATE = os.path.join(TEMPLATES_DIR, "base.html")
FEEDS_TEMPLATE = os.path.join(TEMPLATES_DIR, "feeds.html")
AUDIOBOOKS_TEMPLATE = os.path.join(TEMPLATES_DIR, "audiobooks.html")
MAIN_PY = os.path.join(PROJECT_ROOT, "backend", "app", "main.py")

# Dedicated Audiobooks section wiring (route -> data source -> nav -> page).
AUDIOBOOKS_ENDPOINT = '@app.get("/audiobooks", response_class=HTMLResponse)'
AUDIOBOOKS_TEMPLATE_NAME = 'name="audiobooks.html"'
AUDIOBOOKS_LIST_CALL = "db.get_audiobook_releases(limit=200)"
AUDIOBOOKS_COUNT_CALL = "db.count_audiobook_releases()"
AUDIOBOOKS_ACTIVE_CONTEXT = '"active_page": "audiobooks"'
AUDIOBOOKS_NAV_HREF = 'href="/audiobooks"'
AUDIOBOOKS_NAV_ICON = 'data-lucide="headphones"'
AUDIOBOOKS_NAV_ACTIVE = "{% if active_page == 'audiobooks' %}"
AUDIOBOOKS_EMPTY_TEXT = "No audiobook releases yet."
AUDIOBOOKS_NYAA_HINT = "[Audiobook]"
AUDIOBOOKS_DATE_FILTER = "row.pub_date|localdate"
AUDIOBOOKS_OPEN_REL = 'rel="noopener noreferrer"'

# Nyaa preset markup required by the feature (category 3_1 = Literature,
# English-translated); the value must be the registry key accepted by
# feed_type ("nyaa" -> NyaaNovelProvider in app/providers/registry.py).
NYAA_OPTION = (
    '<option value="nyaa">Nyaa \u2014 Light Novels (category 3_1, English)</option>'
)
NYAA_DEFAULT_URL = "https://nyaa.si/?page=rss&c=3_1"

DOWNLOAD_ICON = 'data-lucide="download"'
RELEASE_ID_ATTR = 'data-release-id="{{ r.id }}"'
DOWNLOAD_CALL = "downloadRelease({{ r.id }}, this)"
DOWNLOAD_DISABLED_TITLE = (
    'title="No torrent or magnet download URL available for this release"'
)

DOWNLOAD_ENDPOINT = '@app.post("/api/releases/{release_id}/download")'
HELPER_SIGNATURE = "async function downloadRelease(releaseId, btn = null)"
HELPER_ENDPOINT = "/api/releases/${releaseId}/download"


def read_text(path):
    """Read a UTF-8 text file, failing loudly (not silently) when it is absent."""
    if not os.path.isfile(path):
        raise AssertionError("expected file not found: %s" % path)
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def require(condition, message):
    """Unstrippable guard: works under ``sys.flags.optimize == 2``."""
    if not condition:
        raise AssertionError(message)


def count_matches(pattern, source):
    return len(re.findall(pattern, source))


def check_download_action(path, label):
    """Both per-release action groups must carry the wired Download button."""
    source = read_text(path)

    require(
        DOWNLOAD_ICON in source,
        "%s: missing the compact download button (data-lucide=\"download\") "
        "in the per-release action group" % label,
    )
    require(
        RELEASE_ID_ATTR in source,
        "%s: download button is missing the data-release-id=\"{{ r.id }}\" "
        "attribute that identifies the release" % label,
    )
    require(
        DOWNLOAD_CALL in source,
        "%s: download button does not call downloadRelease({{ r.id }}, this)"
        % label,
    )
    require(
        source.count(RELEASE_ID_ATTR) >= 2,
        "%s: expected data-release-id on BOTH the enabled and the disabled "
        "download-button branches (found %d occurrence(s))"
        % (label, source.count(RELEASE_ID_ATTR)),
    )
    require(
        "r.download_url" in source and "r.link" in source,
        "%s: download button is not gated on the release download_url/link "
        "fields the /api/releases/{id}/download endpoint reads from the DB"
        % label,
    )
    require(
        DOWNLOAD_DISABLED_TITLE in source,
        "%s: missing the disabled, title-explained download fallback for "
        "releases without a torrent/magnet URL" % label,
    )
    require(
        "disabled" in source,
        "%s: the no-URL download fallback is not disabled" % label,
    )


def check_base_helper():
    """base.html must define the one global downloadRelease(releaseId, btn) helper."""
    source = read_text(BASE_TEMPLATE)

    require(
        HELPER_SIGNATURE in source,
        "base.html: missing the global helper %r next to the other global "
        "release helpers" % HELPER_SIGNATURE,
    )
    require(
        HELPER_ENDPOINT in source,
        "base.html: downloadRelease() does not POST to the release download "
        "endpoint %r" % HELPER_ENDPOINT,
    )
    require(
        "method: 'POST'" in source,
        "base.html: downloadRelease() does not issue an HTTP POST",
    )

    start = source.find("async function downloadRelease")
    end = source.find("async function toggleSeries", start)
    require(
        start != -1 and end > start,
        "base.html: could not isolate the downloadRelease() helper body",
    )
    body = source[start:end]

    require(
        "showToast(data.message" in body,
        "base.html: downloadRelease() does not surface the response 'message' "
        "field through showToast()",
    )
    require(
        "data.status === 'success'" in body,
        "base.html: downloadRelease() does not check the endpoint's "
        "status == 'success' success contract",
    )
    require(
        "btn.disabled = true" in body and "btn.disabled = false" in body,
        "base.html: downloadRelease() does not disable the button in flight "
        "and re-enable it afterwards",
    )
    require(
        "finally" in body,
        "base.html: downloadRelease() does not re-enable the button in a "
        "finally block (error paths would leave it disabled)",
    )


def check_feeds_nyaa_preset():
    """feeds.html must expose the Nyaa preset and prefill its default URL."""
    source = read_text(FEEDS_TEMPLATE)

    require(
        NYAA_OPTION in source,
        "feeds.html: missing the Add New Feed preset option %r" % NYAA_OPTION,
    )
    require(
        NYAA_DEFAULT_URL in source,
        "feeds.html: the Nyaa preset does not use the provider's default "
        "category 3_1 URL %r" % NYAA_DEFAULT_URL,
    )
    require(
        "if (type === 'nyaa')" in source,
        "feeds.html: onFeedTypeChange() has no Nyaa branch to fill the "
        "category 3_1 URL",
    )
    require(
        'id="new-feed-type"' in source,
        "feeds.html: the feed-type <select> (id=\"new-feed-type\") is missing",
    )


def check_endpoint_contract():
    """The UI assumes the documented endpoint contract still exists in main.py."""
    source = read_text(MAIN_PY)

    require(
        DOWNLOAD_ENDPOINT in source,
        "main.py: release download endpoint %r is missing" % DOWNLOAD_ENDPOINT,
    )
    require(
        "db.get_release_by_id(release_id)" in source,
        "main.py: release download endpoint no longer looks the release up by id",
    )
    require(
        re.search(
            r'target\.get\("download_url"\)\s*or\s*target\.get\("link"\)',
            source,
        )
        is not None,
        "main.py: release download endpoint no longer resolves its URL from "
        "download_url falling back to link; the template gate would drift",
    )


def check_audiobooks_page():
    """The dedicated /audiobooks page must be routed, nav-linked and escaped."""
    main_source = read_text(MAIN_PY)

    require(
        AUDIOBOOKS_ENDPOINT in main_source,
        "main.py: dedicated audiobooks page route %r is missing"
        % AUDIOBOOKS_ENDPOINT,
    )
    require(
        AUDIOBOOKS_TEMPLATE_NAME in main_source,
        "main.py: the /audiobooks route does not render audiobooks.html",
    )
    require(
        AUDIOBOOKS_LIST_CALL in main_source,
        "main.py: the /audiobooks route does not read "
        "db.get_audiobook_releases(limit=200)",
    )
    require(
        AUDIOBOOKS_COUNT_CALL in main_source,
        "main.py: the /audiobooks route does not read db.count_audiobook_releases()",
    )
    require(
        AUDIOBOOKS_ACTIVE_CONTEXT in main_source,
        "main.py: the /audiobooks route does not set active_page to 'audiobooks'",
    )

    base_source = read_text(BASE_TEMPLATE)
    require(
        AUDIOBOOKS_NAV_HREF in base_source,
        'base.html: missing the Audiobooks nav link (href="/audiobooks")',
    )
    require(
        AUDIOBOOKS_NAV_ICON in base_source,
        'base.html: the Audiobooks nav link does not use '
        'data-lucide="headphones"',
    )
    require(
        AUDIOBOOKS_NAV_ACTIVE in base_source,
        "base.html: the Audiobooks nav link has no "
        "active_page == 'audiobooks' active state",
    )
    require(
        "Audiobooks" in base_source,
        "base.html: the /audiobooks nav entry is not labelled Audiobooks",
    )

    page_source = read_text(AUDIOBOOKS_TEMPLATE)
    require(
        '{% extends "base.html" %}' in page_source,
        "audiobooks.html: does not extend base.html",
    )
    require(
        "{% block content %}" in page_source,
        "audiobooks.html: missing the content block that base.html renders",
    )
    require(
        "book_rows" in page_source,
        "audiobooks.html: does not iterate the book_rows context key",
    )
    require(
        "total_audiobooks" in page_source,
        "audiobooks.html: does not show the total_audiobooks count badge",
    )
    require(
        AUDIOBOOKS_EMPTY_TEXT in page_source,
        "audiobooks.html: missing the empty state text %r"
        % AUDIOBOOKS_EMPTY_TEXT,
    )
    require(
        AUDIOBOOKS_NYAA_HINT in page_source,
        "audiobooks.html: the empty state does not mention the Nyaa "
        "[Audiobook] tag detection",
    )
    require(
        AUDIOBOOKS_DATE_FILTER in page_source,
        "audiobooks.html: published dates are not formatted through the "
        "globally registered |localdate filter",
    )
    require(
        'target="_blank"' in page_source and AUDIOBOOKS_OPEN_REL in page_source,
        'audiobooks.html: the Open link is not target="_blank" '
        'rel="noopener noreferrer"',
    )
    require(
        "|safe" not in page_source,
        "audiobooks.html: feed-controlled values must be auto-escaped, never "
        "rendered with |safe",
    )


def check_balanced_jinja(path, label):
    """Balanced Jinja block tags and balanced ``{{ ... }}`` expressions."""
    source = read_text(path)

    block_pairs = (
        ("if/endif", r"\{%-?\s*if\b", r"\{%-?\s*endif\s*-?%\}"),
        ("for/endfor", r"\{%-?\s*for\b", r"\{%-?\s*endfor\s*-?%\}"),
        ("block/endblock", r"\{%-?\s*block\b", r"\{%-?\s*endblock\s*-?%\}"),
    )
    for name, open_pattern, close_pattern in block_pairs:
        opened = count_matches(open_pattern, source)
        closed = count_matches(close_pattern, source)
        require(
            opened == closed,
            "%s: unbalanced Jinja %s - %d opening tag(s) vs %d closing tag(s)"
            % (label, name, opened, closed),
        )

    open_tag = source.count("{%")
    close_tag = source.count("%}")
    require(
        open_tag == close_tag,
        "%s: unbalanced Jinja statement delimiters - %d '{%%' vs %d '%%}'"
        % (label, open_tag, close_tag),
    )

    open_expr = source.count("{{")
    close_expr = source.count("}}")
    require(
        open_expr == close_expr,
        "%s: unbalanced Jinja expression delimiters - %d '{{' vs %d '}}'"
        % (label, open_expr, close_expr),
    )


def main():
    check_download_action(FLAT_PARTIAL, "partials/flat_release_row_chunk.html")
    check_download_action(SERIES_PARTIAL, "partials/series_card_chunk.html")
    check_base_helper()
    check_feeds_nyaa_preset()
    check_audiobooks_page()
    check_endpoint_contract()

    templates = (
        (FLAT_PARTIAL, "partials/flat_release_row_chunk.html"),
        (SERIES_PARTIAL, "partials/series_card_chunk.html"),
        (BASE_TEMPLATE, "base.html"),
        (FEEDS_TEMPLATE, "feeds.html"),
        (AUDIOBOOKS_TEMPLATE, "audiobooks.html"),
    )
    for path, label in templates:
        check_balanced_jinja(path, label)

    print(
        "ui-wiring static guard PASSED: download button wired in both release "
        "partials, downloadRelease() helper present, Nyaa preset present, "
        "audiobooks route/nav/page wired, Jinja delimiters balanced in %d "
        "edited template(s)" % len(templates)
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
