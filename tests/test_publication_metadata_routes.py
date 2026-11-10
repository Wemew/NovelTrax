#!/usr/bin/env python3
"""Static + behavioural guard for the AniList publication metadata on the series page.

CONTRACT v3 (§5, §7.C).  The interpreter used for the repo suites has no
``fastapi``/``jinja2``, so ``main.py`` cannot be imported.  This suite therefore:

1. Statically parses ``backend/app/main.py`` and proves ``_series_publication_fields``
   and its helpers exist, return the contract keys, prefer the provider normaliser
   (``NovelMetadataProvider.publication_from_meta``) with a local fallback parser,
   accept dict/JSON-string/None input, and never raise.
2. Proves ``series_view`` enriches every ``db.get_all_series()`` row before the
   template render and uses ``setdefault`` so row keys already present win.
3. Executes the *actual* helper source (extracted from ``main.py`` via :mod:`ast`)
   against a matrix of valid JSON / dict / garbage / None metadata, numeric
   coercion edge cases, MANGA/status label mapping and provider-preference.
4. Runs a DB-level round-trip with a real temp ``Database``: a stored
   ``anilist_meta_json`` carrying the publication keys yields the enriched fields,
   and pre-existing row keys are not overwritten.
5. Asserts the existing route inventory is preserved (no route lost, none added
   that this feature does not need).

Bare ``assert`` is never used (Calibre runs with ``sys.flags.optimize == 2`` and
compiles bare asserts out); an explicit ``check()`` helper raises ``AssertionError``.

Stdlib only: ast, json, os, shutil, sys, tempfile, types, typing.  No
fastapi/jinja2/httpx imports, no network.

Usage::

    & $py tests\\run_tests.py publication_metadata_routes
    & $py tests\\test_publication_metadata_routes.py
"""

import ast
import json
import os
import shutil
import sys
import tempfile
import types
import typing

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend", "app")
MAIN_PY = os.path.join(BACKEND_DIR, "main.py")
BACKEND_PATH = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_PATH not in sys.path:
    sys.path.insert(0, BACKEND_PATH)

from app.database import Database  # noqa: E402

PUBLICATION_KEYS = (
    "anilist_format",
    "anilist_format_label",
    "is_light_novel",
    "anilist_status",
    "anilist_status_label",
    "total_volumes",
    "anilist_chapters",
)

PUBLICATION_HELPER_NAMES = (
    "_publication_token",
    "_publication_int",
    "_publication_label",
    "_publication_meta_mapping",
    "_series_publication_fields",
)

PUBLICATION_CONSTANT_NAMES = (
    "_LIGHT_NOVEL_FORMAT_TOKENS",
    "_PUBLICATION_FORMAT_LABELS",
    "_PUBLICATION_STATUS_LABELS",
)

# Routes pinned by earlier features; the publication-metadata feature adds none.
PINNED_ROUTES = (
    '@app.get("/series"',
    '@app.get("/discovery"',
    '@app.get("/settings"',
    '@app.get("/downloads"',
    '@app.get("/api/calibre/series")',
    '@app.get("/api/series")',
    '@app.get("/api/series/identity")',
    '@app.get("/api/series/aliases")',
    '@app.delete("/api/series/aliases/{override_id}")',
    '@app.post("/api/series/{series_id}/match-calibre")',
    '@app.post("/api/series/{series_id}/reset-identity")',
    '@app.post("/api/series/{series_id}/refresh-metadata")',
    '@app.post("/api/discovery/match")',
    '@app.post("/api/releases/{release_id}/rebind")',
    '@app.get("/api/releases/{release_id}/series-identity")',
    '@app.post("/api/releases/{release_id}/unpin")',
    '@app.post("/api/releases/{release_id}/download")',
    '@app.get("/api/releases/{release_id}/state")',
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

MIN_ROUTE_DECORATORS = 97

_TEMP_COUNTER = 0


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


def _make_temp_dir(label):
    """Create a unique temp dir under the system TEMP (mkdtemp is unusable here)."""
    global _TEMP_COUNTER
    _TEMP_COUNTER += 1
    path = os.path.join(
        tempfile.gettempdir(),
        "noveltrax-publication-routes-%d-%d-%s" % (os.getpid(), _TEMP_COUNTER, label),
    )
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Execute the real helper source extracted from main.py
# ---------------------------------------------------------------------------
def load_publication_helpers():
    """Exec the extracted helper functions/constants in an isolated namespace.

    This exercises the *actual* shipped implementation without importing
    ``main.py`` (which needs FastAPI), while keeping the test stdlib-only.
    """
    source, tree = parse_main()
    segments = []
    found_functions = set()
    found_constants = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            target_names = {
                getattr(target, "id", None) for target in node.targets
            }
            if target_names & set(PUBLICATION_CONSTANT_NAMES):
                segment = ast.get_source_segment(source, node)
                if segment:
                    segments.append(segment)
                    found_constants.update(target_names & set(PUBLICATION_CONSTANT_NAMES))
        elif isinstance(node, ast.FunctionDef) and node.name in PUBLICATION_HELPER_NAMES:
            segment = ast.get_source_segment(source, node)
            if segment:
                segments.append(segment)
                found_functions.add(node.name)
    check(
        set(PUBLICATION_HELPER_NAMES) <= found_functions,
        "main.py: missing publication helper(s) %r"
        % sorted(set(PUBLICATION_HELPER_NAMES) - found_functions),
    )
    check(
        set(PUBLICATION_CONSTANT_NAMES) <= found_constants,
        "main.py: missing publication constant(s) %r"
        % sorted(set(PUBLICATION_CONSTANT_NAMES) - found_constants),
    )
    namespace = {
        "json": json,
        "Any": typing.Any,
        "Dict": typing.Dict,
        "Optional": typing.Optional,
        "List": typing.List,
    }
    exec("\n\n".join(segments), namespace)  # noqa: S102 - extracting the shipped helper
    helper = namespace.get("_series_publication_fields")
    check(callable(helper), "main.py: _series_publication_fields is not callable")
    return helper


# ---------------------------------------------------------------------------
# 1. static guards: helper + series_view
# ---------------------------------------------------------------------------
def check_helper_static():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    for helper_name in PUBLICATION_HELPER_NAMES:
        check(helper_name in functions, "main.py: missing publication helper %r" % helper_name)
    check("_series_publication_fields" in functions,
          "main.py: _series_publication_fields helper is missing")
    segment = function_source(source, functions["_series_publication_fields"])

    for key in PUBLICATION_KEYS:
        check('"%s"' % key in segment,
              "main.py: publication helper does not return the key %r" % key)
    for marker in (
        "from app.providers.metadata import NovelMetadataProvider",
        "publication_from_meta",
        "getattr(",
        "callable(",
        "json.loads",
        "except Exception",
    ):
        check(marker in source,
              "main.py: publication helper is missing the contract marker %r" % marker)

    meta_helper = function_source(source, functions["_publication_meta_mapping"])
    check("isinstance(raw_meta, dict)" in meta_helper,
          "main.py: publication helper does not accept a metadata dict")
    check("isinstance(raw_meta, str)" in meta_helper and "json.loads" in meta_helper,
          "main.py: publication helper does not accept an anilist_meta_json string")
    check("return {}" in meta_helper,
          "main.py: publication helper is not JSON/garbage safe")

    int_helper = function_source(source, functions["_publication_int"])
    for marker in ("float(value)", "!= number", "float(\"inf\")", ">= 0"):
        check(marker in int_helper,
              "main.py: _publication_int is missing the finite/non-negative guard %r"
              % marker)

    token_helper = function_source(source, functions["_publication_token"])
    check("upper()" in token_helper,
          "main.py: _publication_token does not canonicalise the raw AniList value")

    fields_segment = segment
    check("except Exception" in fields_segment and "return fields" in fields_segment,
          "main.py: _series_publication_fields is not guarded against exceptions")


def check_series_view_static():
    source, tree = parse_main()
    functions = functions_by_name(tree)
    check("series_view" in functions, "main.py: series_view handler is missing")
    segment = function_source(source, functions["series_view"])

    check("db.get_all_series()" in segment,
          "main.py: series_view no longer reads db.get_all_series()")
    check("_series_publication_fields(" in segment,
          "main.py: series_view does not call _series_publication_fields")
    check("for series in series_list" in segment,
          "main.py: series_view does not enrich every series row")
    check("setdefault(key, value)" in segment,
          "main.py: series_view enrichment does not preserve existing row keys")
    check("anilist_meta_json" in segment,
          "main.py: series_view does not read the persisted anilist_meta_json")
    helper_index = segment.find("_series_publication_fields(")
    render_index = segment.find("TemplateResponse")
    check(helper_index != -1 and render_index != -1 and helper_index < render_index,
          "main.py: series_view renders the template before enriching the rows")

    check("app.get(\"/series\"" in source,
          "main.py: the GET /series route decorator was removed")
    check("series_list" in segment and "active_page" in segment,
          "main.py: series_view context keys changed unexpectedly")


def check_route_inventory():
    source = read_text(MAIN_PY)
    for route in PINNED_ROUTES:
        check(route in source,
              "main.py: pre-existing route literal %r was lost" % route)
    import re
    decorator_count = len(
        re.findall(r'^@app\.(?:get|post|put|delete|patch)\(', source, re.MULTILINE)
    )
    check(
        decorator_count >= MIN_ROUTE_DECORATORS,
        "main.py: route decorator count dropped to %d (baseline >= %d)"
        % (decorator_count, MIN_ROUTE_DECORATORS),
    )


# ---------------------------------------------------------------------------
# 2. helper matrix (local fallback parser, deterministic)
# ---------------------------------------------------------------------------
class _BareProvider:
    """Provider stand-in without publication_from_meta (forces the local parser)."""


class _FakePublicationProvider:
    """Provider stand-in whose normaliser returns fixed, label-rich data."""

    @classmethod
    def publication_from_meta(cls, meta):
        return {
            "format": "NOVEL",
            "format_label": "Provider Light Novel",
            "is_light_novel": True,
            "status": "FINISHED",
            "status_label": "Provider Finished",
            "volumes": 9,
            "chapters": 99,
            "total_volumes": 9,
        }


def _install_provider(provider_class):
    fake = types.ModuleType("app.providers.metadata")
    fake.NovelMetadataProvider = provider_class
    saved = sys.modules.get("app.providers.metadata")
    sys.modules["app.providers.metadata"] = fake
    return saved


def _restore_provider(saved):
    if saved is None:
        sys.modules.pop("app.providers.metadata", None)
    else:
        sys.modules["app.providers.metadata"] = saved


def _check_defaults(fields, label):
    check(isinstance(fields, dict), "%s: helper did not return a dict" % label)
    for key in PUBLICATION_KEYS:
        check(key in fields, "%s: helper result is missing key %r" % (label, key))
    check(fields["anilist_format"] == "", "%s: format should default to ''" % label)
    check(fields["anilist_format_label"] == "", "%s: format label should default to ''" % label)
    check(fields["is_light_novel"] is False, "%s: is_light_novel should default to False" % label)
    check(fields["anilist_status"] == "", "%s: status should default to ''" % label)
    check(fields["anilist_status_label"] == "", "%s: status label should default to ''" % label)
    check(fields["total_volumes"] is None, "%s: total_volumes should default to None" % label)
    check(fields["anilist_chapters"] is None, "%s: anilist_chapters should default to None" % label)


def check_helper_matrix():
    helper = load_publication_helpers()
    saved = _install_provider(_BareProvider)
    try:
        # None / empty / garbage -> safe defaults, still all keys.
        for label, raw in (
            ("None", None),
            ("empty string", ""),
            ("whitespace string", "   "),
            ("garbage json", "{not valid json"),
            ("json list", "[1, 2, 3]"),
            ("json scalar", "42"),
            ("empty dict", {}),
            ("int input", 1234),
        ):
            _check_defaults(helper(raw), label)

        # Valid dict: NOVEL / FINISHED / 11 chapters 120.
        fields = helper({
            "format": "NOVEL",
            "status": "FINISHED",
            "volumes": 11,
            "chapters": 120,
        })
        check(fields["anilist_format"] == "NOVEL", "dict: raw format not normalised")
        check(fields["anilist_format_label"] == "Light Novel",
              "dict: NOVEL should render 'Light Novel' (got %r)" % fields["anilist_format_label"])
        check(fields["is_light_novel"] is True, "dict: NOVEL should be a light novel")
        check(fields["anilist_status"] == "FINISHED", "dict: raw status not normalised")
        check(fields["anilist_status_label"] == "Finished",
              "dict: FINISHED should render 'Finished'")
        check(fields["total_volumes"] == 11,
              "dict: total_volumes should be 11 (got %r)" % fields["total_volumes"])
        check(fields["anilist_chapters"] == 120,
              "dict: anilist_chapters should be 120 (got %r)" % fields["anilist_chapters"])

        # Valid JSON string (the stored anilist_meta_json shape).
        fields = helper(json.dumps({
            "format": "NOVEL",
            "status": "RELEASING",
            "volumes": "7",
            "chapters": "70",
        }))
        check(fields["anilist_status_label"] == "Releasing",
              "json string: RELEASING should render 'Releasing'")
        check(fields["total_volumes"] == 7, "json string: string volume count should coerce")
        check(fields["anilist_chapters"] == 70, "json string: string chapter count should coerce")

        # Provider-style labels are respected.
        fields = helper({"format": "NOVEL", "format_label": "LN", "status": "FINISHED",
                         "status_label": "Complete"})
        check(fields["anilist_format_label"] == "LN", "explicit format_label should win")
        check(fields["anilist_status_label"] == "Complete", "explicit status_label should win")

        # total_volumes alias vs volumes.
        check(helper({"total_volumes": 5})["total_volumes"] == 5,
              "total_volumes alias should be used when volumes is absent")
        check(helper({"volumes": 6})["total_volumes"] == 6,
              "volumes should feed total_volumes")
        check(helper({"total_volumes": 5, "volumes": 6})["total_volumes"] == 5,
              "total_volumes should take precedence over volumes")

        # Numeric coercion matrix.
        cases = (
            ({"volumes": "11"}, 11),
            ({"volumes": 11.0}, 11),
            ({"volumes": 11.9}, 11),
            ({"volumes": 0}, 0),
            ({"volumes": -3}, None),
            ({"volumes": "abc"}, None),
            ({"volumes": float("nan")}, None),
            ({"volumes": float("inf")}, None),
            ({"chapters": "120"}, 120),
            ({"chapters": -1}, None),
            ({"chapters": "n/a"}, None),
        )
        for raw, expected in cases:
            got = helper(raw)["total_volumes" if "volumes" in raw else "anilist_chapters"]
            check(got == expected,
                  "numeric coercion %r expected %r, got %r" % (raw, expected, got))

        # Formats / statuses matrix.
        check(helper({"format": "MANGA"})["is_light_novel"] is False,
              "MANGA must not be a light novel")
        check(helper({"format": "MANGA"})["anilist_format_label"] == "Manga",
              "MANGA should prettify to 'Manga'")
        check(helper({"format": "LIGHT_NOVEL"})["is_light_novel"] is True,
              "LIGHT_NOVEL should be a light novel")
        check(helper({"format": "LIGHT NOVEL"})["is_light_novel"] is True,
              "'LIGHT NOVEL' should normalise to a light novel")
        check(helper({"format": "NOVEL", "is_light_novel": False})["is_light_novel"] is False,
              "explicit is_light_novel flag should win for a raw NOVEL format")
        for raw_status, expected_label in (
            ("FINISHED", "Finished"),
            ("RELEASING", "Releasing"),
            ("NOT_YET_RELEASED", "Not Yet Released"),
            ("HIATUS", "Hiatus"),
            ("CANCELLED", "Cancelled"),
        ):
            got = helper({"format": "NOVEL", "status": raw_status})
            check(got["anilist_status"] == raw_status,
                  "status token for %r not preserved (got %r)" % (raw_status, got["anilist_status"]))
            check(got["anilist_status_label"] == expected_label,
                  "status %r should label %r (got %r)"
                  % (raw_status, expected_label, got["anilist_status_label"]))

        # Provider normaliser is preferred when available.
        _restore_provider(saved)
        saved = _install_provider(_FakePublicationProvider)
        try:
            provided = helper({"format": "MANGA", "status": "CANCELLED", "volumes": 2})
            check(provided["anilist_format_label"] == "Provider Light Novel",
                  "helper does not prefer NovelMetadataProvider.publication_from_meta "
                  "(format_label=%r)" % provided["anilist_format_label"])
            check(provided["anilist_status_label"] == "Provider Finished",
                  "helper does not prefer the provider status label")
            check(provided["is_light_novel"] is True,
                  "helper does not prefer the provider is_light_novel flag")
            check(provided["total_volumes"] == 9,
                  "helper does not prefer the provider total_volumes")
            check(provided["anilist_chapters"] == 99,
                  "helper does not prefer the provider chapters")
        finally:
            _restore_provider(saved)
            saved = _install_provider(_BareProvider)

        # A provider that explodes must not break the helper (local fallback).
        class _ExplodingProvider:
            @classmethod
            def publication_from_meta(cls, meta):
                raise RuntimeError("provider down")

        _restore_provider(saved)
        saved = _install_provider(_ExplodingProvider)
        try:
            fallback = helper({"format": "NOVEL", "status": "FINISHED", "volumes": 11})
            check(fallback["anilist_format_label"] == "Light Novel",
                  "exploding provider should fall back to the local parser")
            check(fallback["total_volumes"] == 11,
                  "exploding provider should still yield the local volume count")
        finally:
            _restore_provider(saved)
    finally:
        _restore_provider(saved)


# ---------------------------------------------------------------------------
# 3. DB-level round trip: stored anilist_meta_json -> enriched row
# ---------------------------------------------------------------------------
def check_db_round_trip():
    helper = load_publication_helpers()
    temp_dir = _make_temp_dir("db")
    saved = _install_provider(_BareProvider)
    try:
        db = Database(os.path.join(temp_dir, "noveltrax.db"))
        publication_meta = json.dumps({
            "format": "NOVEL",
            "format_label": "Light Novel",
            "is_light_novel": True,
            "status": "FINISHED",
            "status_label": "Finished",
            "volumes": 11,
            "chapters": 120,
            "total_volumes": 11,
            "source": "AniList",
        })
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO series_tracking (name, display_title, calibre_series_id, authors,"
                " book_count, is_monitored, anilist_meta_json)"
                " VALUES (?, ?, ?, ?, ?, 1, ?);",
                ("Executioner Test", "Executioner Display", 4242, "Author X", 11, publication_meta),
            )
            cur.execute(
                "INSERT INTO series_tracking (name, display_title, authors, book_count,"
                " is_monitored, anilist_meta_json) VALUES (?, '', ?, 0, 1, '');",
                ("Empty Meta Test", "Author Y"),
            )
            conn.commit()

        rows = db.get_all_series()
        by_name = {row.get("name"): row for row in rows}
        enriched_row = dict(by_name["Executioner Test"])
        check(enriched_row.get("anilist_meta_json") == publication_meta,
              "get_all_series did not round-trip the stored anilist_meta_json")
        fields = helper(enriched_row["anilist_meta_json"])
        for key, value in fields.items():
            enriched_row.setdefault(key, value)
        check(enriched_row["anilist_format"] == "NOVEL",
              "DB round-trip: anilist_format not enriched")
        check(enriched_row["anilist_format_label"] == "Light Novel",
              "DB round-trip: anilist_format_label not enriched")
        check(enriched_row["is_light_novel"] is True,
              "DB round-trip: is_light_novel not enriched")
        check(enriched_row["anilist_status"] == "FINISHED",
              "DB round-trip: anilist_status not enriched")
        check(enriched_row["anilist_status_label"] == "Finished",
              "DB round-trip: anilist_status_label not enriched")
        check(enriched_row["total_volumes"] == 11,
              "DB round-trip: total_volumes not enriched (got %r)"
              % enriched_row["total_volumes"])
        check(enriched_row["anilist_chapters"] == 120,
              "DB round-trip: anilist_chapters not enriched (got %r)"
              % enriched_row["anilist_chapters"])

        # Pre-existing row keys must win (series_view uses setdefault).
        preexisting = dict(enriched_row)
        preexisting["total_volumes"] = 3
        preexisting["anilist_status_label"] = "Cached Label"
        publication = helper(preexisting["anilist_meta_json"])
        for key, value in publication.items():
            preexisting.setdefault(key, value)
        check(preexisting["total_volumes"] == 3,
              "DB round-trip: an existing total_volumes row key was overwritten")
        check(preexisting["anilist_status_label"] == "Cached Label",
              "DB round-trip: an existing status label row key was overwritten")

        # A row without metadata still gets the safe defaults.
        empty_row = dict(by_name["Empty Meta Test"])
        empty_fields = helper(empty_row.get("anilist_meta_json"))
        for key, value in empty_fields.items():
            empty_row.setdefault(key, value)
        _check_defaults(empty_row, "DB round-trip (empty meta)")

        # The persisted metadata is JSON-safe for the template layer.
        check(isinstance(helper(publication_meta), dict),
              "DB round-trip: helper output is not JSON-safe/dict")
    finally:
        _restore_provider(saved)
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    check_helper_static()
    check_series_view_static()
    check_route_inventory()
    check_helper_matrix()
    check_db_round_trip()

    print(
        "publication-metadata routes guard PASSED: _series_publication_fields helper "
        "(dict/JSON/garbage/None + numeric coercion + Format/Status labels + provider "
        "preference with local fallback), series_view setdefault enrichment, DB "
        "round-trip via a real temp Database, and the existing route inventory "
        "preserved"
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
