#!/usr/bin/env python3
"""Cross-module integration tests for series identity matching (NovelTrax).

This is the acceptance test for the series-identity feature described in
``.agents/series_identity/CONTRACT.md``.  It wires the REAL modules together:

    Database (Calibre trackers + releases + alias overrides)
        -> TaskManager (identity snapshot, manual match, enrichment, reconcile)
            -> app.series_identity.CalibreSeriesIndex / resolve_series_title
                -> CalibreMatcher / parser.match_series_in_calibre

The user-reported scenario is reproduced end to end:
  * Calibre has two similar series:
      #174 "Trapped in a Dating Sim: The World of Otome Games is Tough for Mobs"
      #327 "Trapped in a Dating Sim: The World of Otome Games is Tough for Us, Too"
  * Discovery shows the AniList English title
      "Trapped in a Dating Sim: Otome Games Are Tough for Us, Too!"
  * AniList synonyms contain the #327 Calibre spelling.
The #327 series must be linked/reconciled (IN_CALIBRE for an owned volume) while
#174 is never selected; a prefix-only match between the two must stay ambiguous
(manual match required instead of guessing).

Hermetic: temp DB under the system TEMP (mkdtemp is unusable in this sandbox),
fake Calibre snapshots, fake AniList metadata, no network.  Bare ``assert`` is
never used (the Calibre interpreter strips it); every expectation goes through
:func:`check`, which raises ``AssertionError``.

Usage::

    python tests/test_series_identity_integration.py
    python tests/run_tests.py series_identity_integration
"""

import json
import os
import shutil
import sys
import tempfile
import traceback

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
for _path in (BACKEND_DIR, PROJECT_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# The scheduler imports the provider registry (jnovels/nyaa/...), whose modules
# import bs4/feedparser at module level.  Those third-party packages are not
# installed in this stdlib-only test interpreter, so provide inert stubs: the
# tests never parse HTML/feeds (Calibre snapshots and AniList metadata are fake).
import types  # noqa: E402  (import after sys.path setup is intentional)

for _missing, _stub in (
    ("bs4", {"BeautifulSoup": object, "NavigableString": str, "Tag": object}),
    ("feedparser", {"parse": lambda *args, **kwargs: None}),
):
    try:
        __import__(_missing)
    except ImportError:
        sys.modules[_missing] = types.SimpleNamespace(**_stub)

from app.database import Database  # noqa: E402
from app.scheduler import TaskManager  # noqa: E402
from app.series_identity import CalibreSeriesIndex, resolve_series_title  # noqa: E402
from app.parser import normalize_text  # noqa: E402

_TEMP_DIRS = []
_CHECKS_RUN = [0]

# The exact user-reported titles.
ANILIST_TITLE = "Trapped in a Dating Sim: Otome Games Are Tough for Us, Too!"
CALIBRE_327 = "Trapped in a Dating Sim: The World of Otome Games is Tough for Us, Too"
CALIBRE_174 = "Trapped in a Dating Sim: The World of Otome Games is Tough for Mobs"
ANILIST_META = {
    "canonical_title": ANILIST_TITLE,
    "title": ANILIST_TITLE,
    "english_title": ANILIST_TITLE,
    "romaji_title": "Otomege Sekai wa Mob ni Kibishii Sekai desu",
    "native_title": "乙女ゲー世界はモブに厳しい世界です",
    "synonyms": [CALIBRE_327, "Trapped in a Dating Sim"],
    "anilist_id": 991122,
    "source": "AniList",
}


# ---------------------------------------------------------------------------
# Explicit checks (never bare ``assert``)
# ---------------------------------------------------------------------------
def check(condition, message):
    _CHECKS_RUN[0] += 1
    if not condition:
        raise AssertionError(message)


def check_eq(actual, expected, message):
    check(actual == expected, "%s (expected %r, got %r)" % (message, expected, actual))


def check_in(needle, haystack, message):
    check(needle in haystack, "%s (missing %r in %r)" % (message, needle, str(haystack)[:400]))


def check_not_in(needle, haystack, message):
    check(needle not in haystack, "%s (unexpected %r in %r)" % (message, needle, str(haystack)[:400]))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def make_temp_dir(label):
    path = os.path.join(tempfile.gettempdir(), "noveltrax-series-identity-%s-%s" % (label, os.urandom(6).hex()))
    os.makedirs(path, exist_ok=True)
    _TEMP_DIRS.append(path)
    return path


def cleanup_temp_dirs():
    while _TEMP_DIRS:
        shutil.rmtree(_TEMP_DIRS.pop(), ignore_errors=True)


def calibre_series_fixture():
    return [
        {
            "series_id": 327,
            "series_name": CALIBRE_327,
            "book_count": 10,
            "latest_index": 10.0,
            "authors": "Yomu Mishima",
        },
        {
            "series_id": 174,
            "series_name": CALIBRE_174,
            "book_count": 12,
            "latest_index": 12.0,
            "authors": "Yomu Mishima",
        },
    ]


def calibre_books_fixture():
    books = []
    for series_id, name, count in ((327, CALIBRE_327, 10), (174, CALIBRE_174, 12)):
        for volume in range(1, count + 1):
            books.append(
                {
                    "book_id": series_id * 1000 + volume,
                    "title": "%s Vol. %d" % (name, volume),
                    "series_id": series_id,
                    "series_name": name,
                    "series_index": float(volume),
                    "authors": "Yomu Mishima",
                }
            )
    return books


class Ctx:
    """Temp DB + TaskManager + fake Calibre snapshots + fake AniList titles."""

    def __init__(self, label="case"):
        self.root = make_temp_dir(label)
        self.db = Database(os.path.join(self.root, "noveltrax.db"))
        self.db.set_setting("calibre_path", os.path.join(self.root, "calibre"))
        self.tm = TaskManager(self.db, os.path.join(self.root, "calibre"), data_dir=self.root)
        self.series = calibre_series_fixture()
        self.books = calibre_books_fixture()
        self.metadata_calls = []
        self._counter = 0

        # Fake Calibre scanner: the integration test supplies the library data.
        self.tm._calibre_series_snapshot = lambda: list(self.series)
        self.tm._calibre_library_snapshot = lambda: (list(self.series), list(self.books))

        # Fake AniList/Kitsu provider (no network).
        from app.providers.metadata import NovelMetadataProvider
        self._provider = NovelMetadataProvider
        self._orig_fetch = NovelMetadataProvider.__dict__["fetch_series_metadata"]
        ctx = self

        def fake_fetch(cls, raw_title, *args, **kwargs):
            ctx.metadata_calls.append(str(raw_title))
            if normalize_text(raw_title) in (
                normalize_text(ANILIST_TITLE),
                normalize_text(CALIBRE_327),
                normalize_text(CALIBRE_174),
            ):
                return dict(ANILIST_META)
            return None

        self._provider.fetch_series_metadata = classmethod(fake_fetch)

    def close(self):
        try:
            self._provider.fetch_series_metadata = self._orig_fetch
        except Exception:
            pass
        try:
            self.tm._cover_executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            self.tm.stop()
        except Exception:
            pass

    def insert_release(self, title, series_name, volume=1.0, status="NEW", **overrides):
        self._counter += 1
        values = {
            "guid": "https://nyaa.si/view/%d" % (70000 + self._counter),
            "title": title,
            "series_name": series_name,
            "volume": volume,
            "link": "https://nyaa.si/view/%d" % (70000 + self._counter),
            "status": status,
            "media_type": "light_novel",
            "volumes_json": json.dumps([volume]),
            "missing_vols_json": json.dumps([volume]),
        }
        values.update(overrides)
        columns = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        with self.db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO releases (%s) VALUES (%s);" % (columns, placeholders),
                tuple(values.values()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def row(self, release_id):
        return self.db.get_release_by_id(release_id)


def tracker_by_calibre_id(ctx, calibre_id):
    return ctx.db.get_series_by_calibre_id(calibre_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_screenshot_case_is_matched_to_calibre_327_end_to_end():
    ctx = Ctx("screenshot")
    try:
        ctx.db.sync_calibre_series(ctx.series)
        release_id = ctx.insert_release(
            title=ANILIST_TITLE + " Epub",
            series_name=ANILIST_TITLE,
            volume=1.0,
            status="NEW",
        )
        present_before = [r.get("id") for r in ctx.db.get_discovery_releases(limit=100)]
        check(release_id in present_before, "the unmatched release starts in Discovery")

        # 1. Identity lookup returns the AniList alternate titles.
        alt_titles = ctx.tm.lookup_series_alternate_titles(ANILIST_TITLE)
        check_in(CALIBRE_327, alt_titles, "AniList synonym is available to the matcher")

        # 2. The Calibre index selects #327 via the synonym, never #174.
        index = CalibreSeriesIndex(ctx.series)
        matched = index.match(ANILIST_TITLE, alt_names=alt_titles)
        check(matched is not None, "the AniList title resolves to a Calibre series")
        check_eq(
            int(matched.get("series_id")), 327,
            "the synonym picks Calibre #327 (not the similar #174)",
        )

        # 3. A bare prefix match between the two similarly named series is ambiguous.
        prefix_match = index.match("Trapped in a Dating Sim")
        check_eq(prefix_match, None, "a prefix shared by two Calibre series stays ambiguous")

        # 4. Manual/discovery match links the release to #327.
        calibre_327 = next(item for item in ctx.series if item["series_id"] == 327)
        summary = ctx.tm.match_series_to_calibre(ANILIST_TITLE, calibre_327, actor="user")
        check_eq(summary.get("status"), "success", "manual/discovery match succeeds")
        check_eq(int(summary.get("calibre_series_id")), 327, "match summary names Calibre #327")
        check_eq(summary.get("discovered"), True, "the source was an untracked discovery series")

        tracker = tracker_by_calibre_id(ctx, 327)
        check(tracker is not None, "a tracker row exists for Calibre #327")
        check_eq(tracker.get("name"), CALIBRE_327, "the tracker keeps the Calibre stable name")
        release = ctx.row(release_id)
        check_eq(release.get("series_name"), CALIBRE_327, "the release is re-keyed to the stable name")
        aliases = ctx.db.get_series_alias_overrides()
        check(
            any(
                normalize_text(a.get("source_name")) == normalize_text(ANILIST_TITLE)
                and normalize_text(a.get("target_name")) == normalize_text(CALIBRE_327)
                for a in aliases
            ),
            "an alias override maps the AniList title to the Calibre stable name",
        )

        # 5. Reconcile recomputes the status: volume 1 is owned in Calibre.
        reconciled = ctx.tm.reconcile_series_releases(CALIBRE_327)
        check(reconciled.get("checked", 0) >= 1, "reconcile checked the re-keyed release")
        check(reconciled.get("in_calibre", 0) >= 1, "reconcile marks the owned volume IN_CALIBRE")
        release = ctx.row(release_id)
        check_eq(release.get("status"), "IN_CALIBRE", "release status becomes IN_CALIBRE")
        check(release.get("calibre_matched_book_id") is not None,
              "release records the matched Calibre book id")

        # 6. It disappears from Discovery and never touches the #174 tracker.
        present_after = [r.get("id") for r in ctx.db.get_discovery_releases(limit=100)]
        check_not_in(release_id, present_after, "matched release is no longer a Discovery card")
        tracker_174 = tracker_by_calibre_id(ctx, 174)
        check(tracker_174 is not None, "the #174 tracker is untouched (still exists)")
        check_eq(tracker_174.get("name"), CALIBRE_174, "the #174 tracker keeps its own name")
        # No release may be filed under the "for Mobs" series.
        mobs_releases = [
            r for r in ctx.db.get_releases(limit=500, include_ignored=True)
            if normalize_text(r.get("series_name") or "") == normalize_text(CALIBRE_174)
        ]
        check_eq(mobs_releases, [], "no release is misfiled under the '... for Mobs' series")
    finally:
        ctx.close()


def test_mobs_title_resolves_to_174():
    ctx = Ctx("mobs")
    try:
        ctx.db.sync_calibre_series(ctx.series)
        index = CalibreSeriesIndex(ctx.series)
        matched = index.match(CALIBRE_174)
        check(matched is not None, "the 'for Mobs' spelling resolves")
        check_eq(int(matched.get("series_id")), 174, "the 'for Mobs' spelling picks Calibre #174")
        resolved = resolve_series_title(CALIBRE_174, ctx.db.get_series_identity_rows(), [])
        check_eq(int(resolved.get("calibre_series_id") or 0), 174,
                 "resolve_series_title points at Calibre #174")
    finally:
        ctx.close()


def test_future_release_inherits_the_manual_alias():
    ctx = Ctx("inherit")
    try:
        ctx.db.sync_calibre_series(ctx.series)
        calibre_327 = next(item for item in ctx.series if item["series_id"] == 327)
        ctx.tm.match_series_to_calibre(ANILIST_TITLE, calibre_327, actor="user")

        # A later release arrives with the AniList spelling again.
        new_id = ctx.insert_release(
            title=ANILIST_TITLE + " Vol. 2 Epub",
            series_name=ANILIST_TITLE,
            volume=2.0,
            status="NEW",
        )
        snapshot = ctx.tm.series_identity_snapshot()
        identity = ctx.tm.resolve_release_series_identity(ctx.row(new_id), snapshot)
        check_eq(identity.get("series_name"), CALIBRE_327,
                 "a future release is canonicalised to the stable Calibre name")
        check_eq(int(identity.get("calibre_series_id") or 0), 327,
                 "the identity resolution exposes the Calibre series id")
        check(identity.get("matched") is True, "the future release counts as matched")

        # ... and the feed matcher treats its volume as wanted, not as a new series.
        # In the real ingestion path check_feeds persists the canonical name before
        # matching, so emulate that here.
        from app.calibre.matcher import CalibreMatcher
        matcher = CalibreMatcher(ctx.series, ctx.books)
        payload = dict(ctx.row(new_id))
        payload["series_name"] = identity.get("series_name")
        result = matcher.check_release(
            payload, auto_request_monitored=True,
            alt_series_names=identity.get("alt_names") or [],
        )
        check_in(result.get("status"), ("REQUESTED", "IN_CALIBRE"),
                 "the matched series produces a tracked status, not NEW/DISCOVERY")
        check_in(CALIBRE_327, str(result.get("reason") or ""),
                 "the matcher reason names the Calibre stable series")
    finally:
        ctx.close()


def test_sync_enrichment_links_tracker_and_sets_display_title_only():
    ctx = Ctx("enrich")
    try:
        # Discovery-tracked series starts under the AniList spelling (no Calibre link).
        ctx.db.track_discovered_series(ANILIST_TITLE)
        release_id = ctx.insert_release(
            title=ANILIST_TITLE + " Epub",
            series_name=ANILIST_TITLE,
            volume=1.0,
            status="REQUESTED",
        )
        row = ctx.db.get_series_by_name(ANILIST_TITLE)
        check(row is not None, "the discovered series has a tracker row")
        check(row.get("calibre_series_id") is None, "it starts unlinked")

        summary = ctx.tm.enrich_tracked_series_anilist(limit=10)
        check_eq(summary.get("status") if summary.get("status") else "ok", "ok",
                 "enrichment summary is usable")
        check(summary.get("synced", 0) >= 1, "at least one tracker was enriched from AniList")
        check(summary.get("linked", 0) >= 1, "the enriched tracker auto-linked to Calibre")

        linked = ctx.db.get_series_by_calibre_id(327)
        check(linked is not None, "the tracker now carries Calibre series id 327")
        check_eq(linked.get("name"), CALIBRE_327,
                 "the stable tracking key is the Calibre name after linking")
        check_eq(linked.get("display_title"), ANILIST_TITLE,
                 "the AniList English title lives only in display_title")
        meta = json.loads(linked.get("anilist_meta_json") or "{}")
        check_eq(linked.get("anilist_id"), 991122, "AniList id persisted on the tracker")
        check_in(CALIBRE_327, meta.get("synonyms") or [], "synonyms persisted on the tracker")

        release = ctx.row(release_id)
        check_eq(release.get("series_name"), CALIBRE_327,
                 "the release was re-keyed to the stable name (no phantom series)")
        check(ctx.db.get_series_by_name(ANILIST_TITLE) is None,
              "the duplicate AniList tracker was merged away")
    finally:
        ctx.close()


def test_metadata_failure_never_breaks_enrichment():
    ctx = Ctx("outage")
    try:
        ctx.db.track_discovered_series(ANILIST_TITLE)
        # Simulate a metadata outage.
        ctx._provider.fetch_series_metadata = classmethod(lambda cls, raw, *a, **k: None)
        summary = ctx.tm.enrich_tracked_series_anilist(limit=10)
        check_eq(summary.get("synced", 0), 0, "outage: nothing synced")
        check(summary.get("failed", 0) >= 1, "outage: the failure is counted, not raised")
        row = ctx.db.get_series_by_name(ANILIST_TITLE)
        check(row is not None, "outage: the tracker row survives untouched")
        check_eq(row.get("name"), ANILIST_TITLE, "outage: the stable key is unchanged")
    finally:
        ctx.close()


def test_main_py_route_wiring_static_guards():
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as handle:
        source = handle.read()
    for route in (
        '@app.get("/api/calibre/series")',
        '@app.post("/api/series/{series_id}/match-calibre")',
        '@app.post("/api/discovery/match")',
        '@app.post("/api/series/{series_id}/refresh-metadata")',
        '@app.get("/api/series/aliases")',
        '@app.delete("/api/series/aliases/{override_id}")',
    ):
        check_in(route, source, "main.py declares %s" % route)
    for literal in (
        "match_series_to_calibre",
        "refresh_series_metadata",
        "get_series_alias_overrides",
        "get_series_by_calibre_id",
        "_calibre_series_snapshot",
    ):
        check_in(literal, source, "main.py wires %r" % literal)
    check_not_in("task_manager.scanner.metadata_db_exists()", source,
                 "the broken scanner attribute bug stays fixed")


def main():
    tests = [
        ("screenshot case -> Calibre #327 end to end", test_screenshot_case_is_matched_to_calibre_327_end_to_end),
        ("'for Mobs' title resolves to #174", test_mobs_title_resolves_to_174),
        ("future release inherits the alias", test_future_release_inherits_the_manual_alias),
        ("sync enrichment links tracker, display title only",
         test_sync_enrichment_links_tracker_and_sets_display_title_only),
        ("metadata outage contained", test_metadata_failure_never_breaks_enrichment),
        ("main.py route wiring (static)", test_main_py_route_wiring_static_guards),
    ]
    failures = []
    print("=" * 78)
    print("Series identity integration suite (real Database + TaskManager + matcher)")
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
    cleanup_temp_dirs()
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
