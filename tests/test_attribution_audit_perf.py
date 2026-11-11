#!/usr/bin/env python3
"""Attribution-audit performance / resilience tests (v5 follow-up).

The audit used to resolve every release with ``resolve_series_title`` (which rebuilds its alias
and row lookups on each call), so on large libraries the request could run for minutes and a
reverse proxy returned an HTML error page — the browser then reported
``Unexpected token '<' ... is not valid JSON``.  The audit now:

* skips the expensive parser/resolver when the title only appends volume/format noise to the
  stored series spelling (a meaningful suffix such as "Short Story Collection" still parses);
* memoises resolver results per parsed-series spelling;
* precomputes the stored-name -> Calibre-id map once;
* returns within a hard time budget and marks the response ``truncated``.

Hermetic: fake DB objects, no network, no temp dirs required.  Explicit ``check()``; no bare
``assert``.

Usage::
    python tests/test_attribution_audit_perf.py
    python tests/run_tests.py attribution_audit_perf
"""

import os
import sys
import time
import traceback

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
for _path in (BACKEND_DIR, PROJECT_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app import diagnostics  # noqa: E402

_CHECKS_RUN = [0]


def check(condition, message):
    _CHECKS_RUN[0] += 1
    if not condition:
        raise AssertionError(message)


def check_eq(actual, expected, message):
    check(actual == expected, "%s (expected %r, got %r)" % (message, expected, actual))


class FakeDB:
    def __init__(self, rows, series_rows=None, aliases=None, fail=False):
        self._rows = list(rows or [])
        self._series = list(series_rows or [])
        self._aliases = list(aliases or [])
        self._fail = fail

    def get_series_identity_rows(self):
        return list(self._series)

    def get_series_alias_overrides(self):
        return list(self._aliases)

    def get_series_attribution_rows(self, limit=200, **kwargs):
        if self._fail:
            raise RuntimeError("db unavailable")
        return self._rows[: max(1, int(limit))]


def _row(idx, title, stored, status="REQUESTED", pinned=0):
    return {
        "id": idx,
        "title": title,
        "series_name": stored,
        "status": status,
        "series_pinned": pinned,
    }


def _series(idx, name, calibre_id=None):
    return {"id": idx, "name": name, "calibre_series_id": calibre_id}


def test_noise_only_helper():
    helper = diagnostics._attribution_title_noise_only
    check(helper("series one vol 1", "series one"), "volume suffix is noise")
    check(helper("series one volume 2 epub pdf", "series one"), "format suffix is noise")
    check(not helper("series one short story collection", "series one"),
          "a meaningful suffix must force the parser path")
    check(not helper("series one spin off vol 1", "series one"),
          "a spin-off suffix is not noise")
    check(not helper("[group] series one vol 1", "series one"),
          "a leading group tag forces the parser path")
    check(not helper("other title vol 1", "series one"),
          "a title without the stored spelling is never noise-only")


def test_fast_path_skips_parser_and_resolver():
    series = [_series(i, "Fast Series %03d" % i) for i in range(100)]
    rows = []
    for i in range(5000):
        name = "Fast Series %03d" % (i % 100)
        rows.append(_row(i + 1, "%s Vol %d" % (name, (i % 10) + 1), name))
    calls = {"parse": 0, "resolve": 0}
    original_parse = diagnostics._parsed_release_series
    original_resolve = diagnostics.resolve_series_title

    def counting_parse(title):
        calls["parse"] += 1
        return original_parse(title)

    def counting_resolve(*args, **kwargs):
        calls["resolve"] += 1
        return original_resolve(*args, **kwargs)

    diagnostics._parsed_release_series = counting_parse
    if original_resolve is not None:
        diagnostics.resolve_series_title = counting_resolve
    try:
        start = time.perf_counter()
        audit = diagnostics.series_attribution_audit(
            FakeDB(rows, series), limit=200, scan_limit=50000, time_budget=30.0,
        )
        elapsed = time.perf_counter() - start
    finally:
        diagnostics._parsed_release_series = original_parse
        if original_resolve is not None:
            diagnostics.resolve_series_title = original_resolve

    check_eq(audit.get("checked"), 5000, "all 5000 correctly-attributed releases were scanned")
    check_eq(audit.get("count"), 0, "no false drift was reported")
    check_eq(calls["parse"], 0, "the parser is skipped for volume/format-only suffixes")
    check_eq(calls["resolve"], 0, "the resolver is skipped on the fast path")
    check(elapsed < 5.0, "fast-path audit completes quickly (%.3fs)" % elapsed)
    check_eq(audit.get("truncated"), False, "a quick audit is not truncated")


def test_meaningful_suffix_still_detected():
    series = [_series(1, "Base Series", 1001)]
    rows = [
        _row(1, "Base Series Short Story Collection Vol 1", "Base Series"),
    ]
    audit = diagnostics.series_attribution_audit(FakeDB(rows, series), limit=10, time_budget=10.0)
    check_eq(audit.get("count"), 1, "a meaningful subtitle suffix is flagged as drift")
    item = audit["items"][0]
    check_eq(item.get("series_name"), "Base Series", "stored series is reported")
    check("Short Story Collection" in str(item.get("suggested_series") or ""),
          "suggested series includes the parsed suffix: %r" % (item.get("suggested_series"),))


def test_resolver_memoised_per_parsed_series():
    series = [_series(1, "Base Series", 1001)]
    rows = [_row(i + 1, "Wrong Title %d Spin Off Vol 1" % (i % 3), "Stored %d" % i)
            for i in range(300)]
    calls = {"count": 0}
    original_resolve = diagnostics.resolve_series_title

    def counting_resolve(*args, **kwargs):
        calls["count"] += 1
        return original_resolve(*args, **kwargs)

    if original_resolve is not None:
        diagnostics.resolve_series_title = counting_resolve
    try:
        audit = diagnostics.series_attribution_audit(FakeDB(rows, series), limit=1000, time_budget=10.0)
    finally:
        if original_resolve is not None:
            diagnostics.resolve_series_title = original_resolve
    check(audit.get("checked") == 300, "all rows checked")
    check(calls["count"] <= 5, "resolver is memoised per parsed series (called %d times)" % calls["count"])


def test_time_budget_returns_truncated_json():
    rows = [_row(i + 1, "Unmatched Series %d Short Story Collection Vol 1" % i, "Stored %d" % i)
            for i in range(2000)]
    original_parse = diagnostics._parsed_release_series

    def slow_parse(title):
        time.sleep(0.01)
        return original_parse(title)

    diagnostics._parsed_release_series = slow_parse
    try:
        start = time.perf_counter()
        audit = diagnostics.series_attribution_audit(
            FakeDB(rows, []), limit=200, time_budget=0.2,
        )
        elapsed = time.perf_counter() - start
    finally:
        diagnostics._parsed_release_series = original_parse
    check_eq(audit.get("truncated"), True, "the audit reports truncation when the budget is hit")
    check(audit.get("checked", 0) < 2000, "the audit did not scan everything after the budget")
    check(elapsed < 3.0, "the audit respects its time budget (%.3fs)" % elapsed)
    check(isinstance(audit.get("items"), list), "truncated audits still return a JSON-serialisable list")


def test_broken_db_is_contained():
    audit = diagnostics.series_attribution_audit(FakeDB([], fail=True), limit=10)
    check(isinstance(audit, dict), "audit returns a dict when the DB query fails")
    check_eq(audit.get("count"), 0, "failed audit has no items")
    check(isinstance(audit.get("error"), str), "failed audit keeps an error field")


def test_settings_ui_audit_resilience_static():
    settings = open(os.path.join(BACKEND_DIR, "app", "templates", "settings.html"), "r", encoding="utf-8").read()
    start = settings.find("async function runSeriesAttributionAudit()")
    end = settings.find("// ------------------------------------------------------------------\n    // Display & logging helpers", start)
    check(start != -1 and end != -1, "audit function found in settings.html")
    body = settings[start:end]
    check("content-type" in body.lower(), "audit checks the response content-type before JSON parsing")
    check("application/json" in body, "audit requires a JSON response")
    check("AbortController" in body and "60000" in body, "audit has a client timeout")
    check("HTML page instead of JSON" in body, "audit explains an HTML/proxy error page")
    check("truncated" in body and "elapsed_ms" in body, "audit surfaces server-side truncation")
    check("res.json()" in body, "audit still parses the JSON payload")


def main():
    tests = [
        ("noise-only helper", test_noise_only_helper),
        ("fast path skips parser/resolver", test_fast_path_skips_parser_and_resolver),
        ("meaningful suffix still detected", test_meaningful_suffix_still_detected),
        ("resolver memoised per parsed series", test_resolver_memoised_per_parsed_series),
        ("time budget returns truncated JSON", test_time_budget_returns_truncated_json),
        ("broken DB contained", test_broken_db_is_contained),
        ("settings UI audit resilience (static)", test_settings_ui_audit_resilience_static),
    ]
    failures = []
    print("=" * 78)
    print("Attribution-audit performance / resilience suite")
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
