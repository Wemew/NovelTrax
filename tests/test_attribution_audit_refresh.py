#!/usr/bin/env python3
"""Regression guard: re-binding from the Settings attribution audit refreshes the audit in place.

Bug report: after running the Attribution audit and re-binding a single release, the page reloaded
(``location.reload()`` in the shared re-bind modal), so the audit results were lost and the operator
had to click "Run attribution audit" again.  The shared modal now accepts an optional ``onSuccess``
callback; the attribution audit passes one that removes the fixed row and re-runs the audit, while
every other caller keeps the original reload behaviour.

No browser/Jinja2 is available in this test interpreter, so this is a static source guard in the same
spirit as the other UI wiring suites.  It executes the real template text; it never renders Jinja.

Usage::

    python tests/test_attribution_audit_refresh.py
    python tests/run_tests.py attribution_audit_refresh
"""

import os
import sys
import traceback

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "backend", "app", "templates")
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")

_CHECKS_RUN = [0]


def check(condition, message):
    _CHECKS_RUN[0] += 1
    if not condition:
        raise AssertionError(message)


def check_in(needle, haystack, message):
    check(needle in haystack, "%s (missing %r)" % (message, needle))


def check_not_in(needle, haystack, message):
    check(needle not in haystack, "%s (unexpected %r)" % (message, needle))


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def function_source(source, start_marker, end_marker):
    start = source.find(start_marker)
    check(start != -1, "template contains %r" % start_marker)
    end = source.find(end_marker, start + len(start_marker))
    check(end != -1, "template contains %r after %r" % (end_marker, start_marker))
    return source[start:end]


def test_shared_modal_supports_success_callback():
    base = read(os.path.join(TEMPLATES_DIR, "base.html"))

    # The modal state carries the optional callback and resets it.
    check_in("onSuccess: null", base, "modal state declares an onSuccess slot")
    check_in(
        "_releaseRebind.onSuccess = onSuccess;",
        base,
        "openReleaseRebindModal stores the caller callback",
    )
    check_in("typeof source.onSuccess === 'function'", base,
             "openReleaseRebindModal accepts source.onSuccess")
    check_in("typeof source.on_success === 'function'", base,
             "openReleaseRebindModal accepts the snake_case alias")
    check_in("_releaseRebind.onSuccess = null;", base,
             "closeReleaseRebindModal clears the callback")

    apply_source = function_source(base, "async function applyReleaseRebind()", "async function unpinReleaseRebind")
    check_in("const successCallback = _releaseRebind.onSuccess;", apply_source,
             "applyReleaseRebind captures the callback before closing")
    check_in("if (typeof successCallback === 'function') {", apply_source,
             "applyReleaseRebind branches on the callback")
    check_in("successCallback(data);", apply_source, "applyReleaseRebind invokes the callback")
    check_in("setTimeout(function () { location.reload(); }, 500);", apply_source,
             "applyReleaseRebind keeps the reload fallback for normal callers")
    # The reload must be the fallback, not unconditional: callback check precedes it.
    check(
        apply_source.find("successCallback(data);") < apply_source.find("setTimeout(function () { location.reload(); }, 500);"),
        "callback invocation precedes the reload fallback",
    )

    unpin_source = function_source(base, "async function unpinReleaseRebind()", "    </script>")
    check_in("const successCallback = _releaseRebind.onSuccess;", unpin_source,
             "unpin also captures the callback")
    check_in("successCallback(data);", unpin_source, "unpin invokes the callback")
    check_in("setTimeout(function () { location.reload(); }, 500);", unpin_source,
             "unpin keeps the reload fallback")


def test_attribution_audit_refreshes_in_place():
    settings = read(os.path.join(TEMPLATES_DIR, "settings.html"))
    audit_source = function_source(settings, "async function runSeriesAttributionAudit()", "// ------------------------------------------------------------------\n    // Display & logging helpers")
    check_in("/api/maintenance/series-attribution", audit_source,
             "audit fetches the attribution endpoint")
    check_in("series-attribution-result", audit_source, "audit renders into its result container")
    check_in("openReleaseRebindModal({", audit_source, "audit rows open the shared re-bind modal")
    check_in("onSuccess: function () {", audit_source,
             "audit re-bind passes an onSuccess callback")
    callback_source = audit_source[audit_source.find("onSuccess: function () {"):]
    check_in("row.remove();", callback_source,
             "the fixed audit row is removed immediately")
    check_in("runSeriesAttributionAudit();", callback_source,
             "the audit re-runs automatically after a successful re-bind")
    # The callback must not reload the page (that was the bug).
    callback_block = callback_source[:callback_source.find("});", callback_source.find("onSuccess")) + 3] if "});" in callback_source else callback_source[:400]
    check_not_in("location.reload", callback_block,
                 "the attribution onSuccess callback never reloads the page")
    # Sanity: the normal audit execution path still exists.
    check_in("Attribution audit: ", audit_source, "audit still reports checked/flagged counts")
    check_in("_identityMessage(box, 'Attribution audit failed: '", audit_source,
             "audit still contains its failure message")


def test_other_callers_keep_reload_and_pinned_literals():
    base = read(os.path.join(TEMPLATES_DIR, "base.html"))
    settings = read(os.path.join(TEMPLATES_DIR, "settings.html"))
    partial = read(os.path.join(TEMPLATES_DIR, "partials", "flat_release_row_chunk.html"))

    # The generic release-row callers pass no callback, so they still reload via the fallback.
    check_in("openReleaseRebindModal(this)", partial,
             "release rows keep the element form (no callback)")
    # Pinned modal + template literals from the previous releases.
    for literal in (
        "release-rebind-modal",
        "release-rebind-apply",
        "release-rebind-unpin",
        "setReleaseRebindMode",
        "searchReleaseRebindSeries",
        "renderReleaseRebindResults",
        "applyReleaseRebind",
        "refreshReleaseRebindState",
    ):
        check_in(literal, base, "base.html keeps %r" % literal)
    for literal in (
        "series-identity-results",
        "series-attribution-result",
        "btn-series-attribution-audit",
        "series-alias-list",
        "setting-nyaa-bundle-inference",
    ):
        check_in(literal, settings, "settings.html keeps %r" % literal)
    # Balanced Jinja delimiters in the edited templates.
    for name, source in (("base.html", base), ("settings.html", settings)):
        for opening, closing in (("{% if", "{% endif %}"), ("{% for", "{% endfor %}"), ("{% block", "{% endblock %}")):
            check(source.count(opening) == source.count(closing),
                  "%s has balanced %r/%r (%d/%d)" % (name, opening, closing, source.count(opening), source.count(closing)))
        check(source.count("{{") == source.count("}}"),
              "%s has balanced {{ }} delimiters" % name)


def main():
    tests = [
        ("shared modal supports onSuccess callback", test_shared_modal_supports_success_callback),
        ("attribution audit refreshes in place", test_attribution_audit_refreshes_in_place),
        ("other callers keep reload + pinned literals", test_other_callers_keep_reload_and_pinned_literals),
    ]
    failures = []
    print("=" * 78)
    print("Attribution-audit re-bind refresh regression guard")
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
