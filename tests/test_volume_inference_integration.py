#!/usr/bin/env python3
"""Cross-module integration test: AniList publication metadata -> Nyaa bundle volume inference.

Acceptance test for ``.agents/volume_inference/CONTRACT.md`` (v3).  It wires the REAL modules:

    NovelMetadataProvider (publication fields + normalizer)
        -> Database (metadata cache publication_json, series anilist_meta_json)
            -> TaskManager.check_feeds (Nyaa feed ingestion, bounded publication lookup,
               unmarked-batch inference, Calibre matcher, grouped release coverage)

A fake Nyaa provider feeds one unmarked batch release plus controls (Releasing series, MANGA-format
series, explicit Vol. 3 release).  The AniList lookup is monkeypatched (no network).  The test asserts
the stored release volume set, bundle flags, missing coverage, activity log, series-page metadata
persistence and idempotency.

Hermetic: temp DB/dirs under the system TEMP (mkdtemp is unusable in this sandbox), no network.
No bare ``assert`` - every expectation goes through ``check``.

Usage::

    python tests/test_volume_inference_integration.py
    python tests/run_tests.py volume_inference_integration
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback
import types

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
for _path in (BACKEND_DIR, PROJECT_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Provider modules import bs4/feedparser at module level; stub them for this stdlib-only runner.
for _missing, _stub in (
    ("bs4", {"BeautifulSoup": object, "NavigableString": str, "Tag": object}),
    ("feedparser", {"parse": lambda *args, **kwargs: None}),
):
    try:
        __import__(_missing)
    except ImportError:
        sys.modules[_missing] = types.SimpleNamespace(**_stub)

from app import scheduler as scheduler_module  # noqa: E402
from app.database import Database  # noqa: E402
from app.parser import normalize_text  # noqa: E402
from app.providers.base import ReleaseItem  # noqa: E402
from app.providers.metadata import NovelMetadataProvider  # noqa: E402
from app.scheduler import TaskManager  # noqa: E402

_TEMP_DIRS = []
_CHECKS_RUN = [0]

EXECUTIONER = "The Executioner and Her Way of Life"
RELEASING = "Releasing Test Series"
MANGA_FORMAT = "Manga Format Test Series"
EXPLICIT = "Explicit Volume Series"

#: Fake AniList metadata per series (the real publication_from_meta normalizer is used).
FAKE_METADATA = {
    EXECUTIONER: {
        "title": EXECUTIONER, "english_title": EXECUTIONER, "canonical_title": EXECUTIONER,
        "format": "NOVEL", "status": "FINISHED", "volumes": 11, "chapters": 90,
        "anilist_id": 900001, "source": "AniList",
    },
    RELEASING: {
        "title": RELEASING, "english_title": RELEASING, "canonical_title": RELEASING,
        "format": "NOVEL", "status": "RELEASING", "volumes": 5,
        "anilist_id": 900002, "source": "AniList",
    },
    MANGA_FORMAT: {
        "title": MANGA_FORMAT, "english_title": MANGA_FORMAT, "canonical_title": MANGA_FORMAT,
        "format": "MANGA", "status": "FINISHED", "volumes": 20,
        "anilist_id": 900003, "source": "AniList",
    },
    EXPLICIT: {
        "title": EXPLICIT, "english_title": EXPLICIT, "canonical_title": EXPLICIT,
        "format": "NOVEL", "status": "FINISHED", "volumes": 10,
        "anilist_id": 900004, "source": "AniList",
    },
}


def check(condition, message):
    _CHECKS_RUN[0] += 1
    if not condition:
        raise AssertionError(message)


def check_eq(actual, expected, message):
    check(actual == expected, "%s (expected %r, got %r)" % (message, expected, actual))


def check_in(needle, haystack, message):
    check(needle in haystack, "%s (missing %r in %r)" % (message, needle, str(haystack)[:400]))


def make_temp_dir(label):
    path = os.path.join(tempfile.gettempdir(), "noveltrax-volinf-%s-%s" % (label, os.urandom(6).hex()))
    os.makedirs(path, exist_ok=True)
    _TEMP_DIRS.append(path)
    return path


def cleanup_temp_dirs():
    while _TEMP_DIRS:
        shutil.rmtree(_TEMP_DIRS.pop(), ignore_errors=True)


def fake_fetch_series_metadata(raw_title):
    """Stands in for NovelMetadataProvider.fetch_series_metadata (no network)."""
    key = normalize_text(raw_title)
    for name, meta in FAKE_METADATA.items():
        if normalize_text(name) == key:
            return dict(meta)
    return None


class FakeNyaaProvider:
    """Minimal Nyaa-shaped provider returning a fixed release page."""

    def __init__(self, releases):
        self._releases = releases
        self.last_scan_meta = {}

    def fetch_releases(self):
        return list(self._releases)

    def fetch_releases_since(self, cutoff_iso=None, stop_guid=None, max_pages=1):
        return list(self._releases)


def make_releases():
    magnet = "magnet:?xt=urn:btih:" + ("b" * 40)
    return [
        ReleaseItem(
            title=EXECUTIONER + " [Yen Press] [Stick]",
            link="https://nyaa.si/view/700001",
            series_name=EXECUTIONER,
            volume=None,
            guid="nyaa-700001",
            download_url=magnet,
            pub_date="2026-11-01T00:00:00+00:00",
        ),
        ReleaseItem(
            title=RELEASING + " [Yen Press] [Stick]",
            link="https://nyaa.si/view/700002",
            series_name=RELEASING,
            volume=None,
            guid="nyaa-700002",
            download_url=magnet,
            pub_date="2026-11-02T00:00:00+00:00",
        ),
        ReleaseItem(
            title=MANGA_FORMAT + " [Yen Press] [Stick]",
            link="https://nyaa.si/view/700003",
            series_name=MANGA_FORMAT,
            volume=None,
            guid="nyaa-700003",
            download_url=magnet,
            pub_date="2026-11-03T00:00:00+00:00",
        ),
        ReleaseItem(
            title=EXPLICIT + " Vol. 3 [Yen Press] [Stick]",
            link="https://nyaa.si/view/700004",
            series_name=EXPLICIT,
            volume=3.0,
            guid="nyaa-700004",
            download_url=magnet,
            pub_date="2026-11-04T00:00:00+00:00",
        ),
    ]


def release_row(db, guid):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM releases WHERE guid = ?;", (guid,))
        row = cur.fetchone()
        return dict(row) if row else None


class Ctx:
    def __init__(self, label="case"):
        self.root = make_temp_dir(label)
        self.db = Database(os.path.join(self.root, "noveltrax.db"))
        self.calibre_dir = os.path.join(self.root, "calibre")
        os.makedirs(self.calibre_dir, exist_ok=True)
        self.data_dir = os.path.join(self.root, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.db.set_setting("calibre_path", self.calibre_dir)
        # Disable media classification network/DB heuristics: the fake titles are neutral and
        # this test targets the volume inference, not the media resolver.
        self.db.set_setting("filter_non_novels", "false")
        self.db.set_setting("series_anilist_match_budget", "10")
        self.db.add_feed(
            "Nyaa Light Novels (3_1)", "nyaa", "https://nyaa.si/?page=rss&c=3_1", "{}",
            auto_request=True, interval=60,
        )
        self.tm = TaskManager(self.db, self.calibre_dir, data_dir=self.data_dir)
        self._orig_fetch = NovelMetadataProvider.__dict__["fetch_series_metadata"]
        self._orig_create_provider = scheduler_module.create_provider
        NovelMetadataProvider.fetch_series_metadata = staticmethod(fake_fetch_series_metadata)
        def _fake_create_provider(feed_type, name, url, config=None):
            # Only the Nyaa feed carries the batch page; other seeded feeds return nothing so
            # they cannot re-upsert (and clear) the same GUID.
            if str(feed_type or "").lower() == "nyaa" or "nyaa.si" in str(url or "").lower():
                return FakeNyaaProvider(make_releases())
            return FakeNyaaProvider([])
        scheduler_module.create_provider = _fake_create_provider
        self._metadata_calls = []
        self._wrap_metadata_calls()

    def _wrap_metadata_calls(self):
        original = NovelMetadataProvider.fetch_series_metadata

        def tracked(raw_title):
            self._metadata_calls.append(raw_title)
            return original(raw_title)

        NovelMetadataProvider.fetch_series_metadata = staticmethod(tracked)

    def close(self):
        try:
            self.tm._cover_executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            self.tm.stop()
        except Exception:
            pass
        NovelMetadataProvider.fetch_series_metadata = self._orig_fetch
        scheduler_module.create_provider = self._orig_create_provider


def test_check_feeds_infers_complete_bundle():
    ctx = Ctx("infer")
    try:
        result = ctx.tm.check_feeds()
        check_in(result.get("status"), ("success",), "feed check succeeds: %r" % (result,))

        row = release_row(ctx.db, "nyaa-700001")
        check(row is not None, "unmarked Nyaa batch release was stored")
        volumes = json.loads(row.get("volumes_json") or "[]")
        check_eq(volumes, [float(v) for v in range(1, 12)],
                 "release stores the AniList complete-series volume set 1..11")
        check_eq(row.get("is_bundle"), 1, "inferred release is marked as a bundle")
        check_eq(float(row.get("volume") or 0.0), 1.0, "bundle is stored as volume 1 of the set")
        check_eq(row.get("series_name"), EXECUTIONER, "series name is preserved")

        # The grouped Releases view must show all 11 detected volumes for the bundle.
        groups = ctx.db.get_releases_grouped_by_series()
        group = next((g for g in groups if normalize_text(g.get("stable_name") or g.get("series_name") or "") == normalize_text(EXECUTIONER)), None)
        check(group is not None, "grouped view contains the inferred series")
        check_eq(sorted(float(v) for v in group.get("all_volumes") or []),
                 [float(v) for v in range(1, 12)],
                 "series coverage shows all 11 inferred volumes")

        # Activity log entry exactly once, with the AniList publication details.
        logs = ctx.db.get_logs(limit=50)
        inferred = [log for log in logs if log.get("event_type") == "BUNDLE_VOLUMES_INFERRED"]
        check_eq(len(inferred), 1, "one BUNDLE_VOLUMES_INFERRED activity was logged")
        details = json.loads(inferred[0].get("details") or "{}")
        check_eq(details.get("volumes"), 11, "activity records the inferred volume count")
        check_eq(details.get("anilist_status"), "FINISHED", "activity records AniList status")
        check_eq(details.get("anilist_format"), "NOVEL", "activity records AniList format")
    finally:
        ctx.close()


def test_non_finished_and_non_novel_releases_are_not_inferred():
    ctx = Ctx("controls")
    try:
        ctx.tm.check_feeds()

        releasing = release_row(ctx.db, "nyaa-700002")
        check(releasing is not None, "Releasing-series release was stored")
        check_eq(json.loads(releasing.get("volumes_json") or "[]"), [],
                 "Releasing series is never inferred as a complete bundle")
        check_eq(releasing.get("is_bundle"), 0, "Releasing release is not a bundle")

        manga = release_row(ctx.db, "nyaa-700003")
        check(manga is not None, "MANGA-format release was stored")
        check_eq(json.loads(manga.get("volumes_json") or "[]"), [],
                 "non-Light-Novel format is never inferred")
        check_eq(manga.get("is_bundle"), 0, "MANGA-format release is not a bundle")

        explicit = release_row(ctx.db, "nyaa-700004")
        check(explicit is not None, "explicit-volume release was stored")
        check_eq(float(explicit.get("volume") or 0.0), 3.0, "explicit Vol. 3 keeps its parsed volume")
        check_eq(json.loads(explicit.get("volumes_json") or "[]"), [3.0],
                 "explicit volume marker is not replaced by an inferred set")
        check_eq(explicit.get("is_bundle"), 0, "single explicit volume is not a bundle")
    finally:
        ctx.close()


def test_second_scan_is_idempotent():
    ctx = Ctx("idempotent")
    try:
        ctx.tm.check_feeds()
        ctx.tm.check_feeds()
        with ctx.db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS c FROM releases WHERE guid = 'nyaa-700001';")
            count = cur.fetchone()["c"]
        check_eq(count, 1, "second scan updates instead of duplicating the release")
        row = release_row(ctx.db, "nyaa-700001")
        check_eq(json.loads(row.get("volumes_json") or "[]"),
                 [float(v) for v in range(1, 12)],
                 "second scan keeps the inferred volume set")
        logs = ctx.db.get_logs(limit=100)
        inferred = [log for log in logs if log.get("event_type") == "BUNDLE_VOLUMES_INFERRED"]
        check_eq(len(inferred), 1, "existing rows do not re-log the inference")
    finally:
        ctx.close()


def test_tracker_persists_publication_and_monitored_status():
    ctx = Ctx("tracked")
    try:
        ctx.db.track_discovered_series(EXECUTIONER)
        ctx.tm.check_feeds()

        tracker = ctx.db.get_series_by_name(EXECUTIONER)
        check(tracker is not None, "tracker exists")
        meta = tracker.get("anilist_meta_json")
        if isinstance(meta, str):
            meta = json.loads(meta)
        meta = meta or {}
        check_eq(meta.get("status"), "FINISHED", "tracker persists AniList status")
        check_eq(meta.get("volumes"), 11, "tracker persists AniList volume count")
        check_eq(str(meta.get("format") or "").upper(), "NOVEL", "tracker persists AniList format")
        check_eq(meta.get("format_label"), "Light Novel", "tracker carries the Light Novel label")

        row = release_row(ctx.db, "nyaa-700001")
        check_in(row.get("status"), ("REQUESTED", "NEW", "IN_CALIBRE"),
                 "tracked release has a valid lifecycle status: %r" % (row.get("status"),))
        check_eq(json.loads(row.get("volumes_json") or "[]"),
                 [float(v) for v in range(1, 12)],
                 "tracked release keeps the inferred volume set")

        # Series publication fields are what the /series page consumes.
        publication = NovelMetadataProvider.publication_from_meta(tracker.get("anilist_meta_json"))
        check_eq(publication.get("format_label"), "Light Novel", "series page shows Light Novel")
        check_eq(publication.get("status_label"), "Finished", "series page shows Finished")
        check_eq(publication.get("volumes"), 11, "series page shows 11 volumes")
    finally:
        ctx.close()


def test_database_cache_and_provider_publication_roundtrip():
    ctx = Ctx("cache")
    try:
        meta = fake_fetch_series_metadata(EXECUTIONER)
        publication = NovelMetadataProvider.publication_from_meta(meta)
        check_eq(publication.get("format_label"), "Light Novel", "normalizer labels NOVEL as Light Novel")
        check_eq(publication.get("status_label"), "Finished", "normalizer labels FINISHED as Finished")
        check_eq(publication.get("volumes"), 11, "normalizer reads the volume count")
        check_eq(publication.get("is_light_novel"), True, "normalizer flags a light novel")

        ctx.db.cache_novel_metadata(EXECUTIONER.lower(), meta)
        cached = ctx.db.get_cached_novel_metadata(EXECUTIONER.lower())
        check(cached is not None, "metadata cache stores the lookup")
        check_eq(cached.get("status"), "FINISHED", "cache round-trips the publication status")
        check_eq(cached.get("volumes"), 11, "cache round-trips the volume count")
        check_eq(cached.get("format_label"), "Light Novel", "cache round-trips the format label")
    finally:
        ctx.close()


def test_main_py_and_provider_static_guards():
    provider_source = open(os.path.join(BACKEND_DIR, "app", "providers", "metadata.py"), "r", encoding="utf-8").read()
    scheduler_source = open(os.path.join(BACKEND_DIR, "app", "scheduler.py"), "r", encoding="utf-8").read()
    for literal in ("format", "status", "volumes", "chapters", "format_in: [NOVEL]"):
        check_in(literal, provider_source, "AniList query/metadata covers %r" % literal)
    check_in("def publication_from_meta", provider_source, "provider exposes publication_from_meta")
    check_in("def infer_nyaa_bundle_volumes", scheduler_source, "scheduler exposes the inference helper")
    check_in("def publication_metadata_for", scheduler_source, "scheduler exposes the publication lookup")
    check_in("infer_nyaa_bundles=True", scheduler_source, "Nyaa backfill enables the inference")
    check_in("BUNDLE_VOLUMES_INFERRED", scheduler_source, "inference is audited")
    main_source = open(os.path.join(BACKEND_DIR, "app", "main.py"), "r", encoding="utf-8").read()
    check_in("_series_publication_fields", main_source, "series page enriches publication fields")
    settings_source = open(os.path.join(BACKEND_DIR, "app", "templates", "settings.html"), "r", encoding="utf-8").read()
    check_in("setting-nyaa-bundle-inference", settings_source, "settings exposes the inference toggle")
    series_source = open(os.path.join(BACKEND_DIR, "app", "templates", "series.html"), "r", encoding="utf-8").read()
    check_in("series-publication-line", series_source, "series page renders the publication line")


def main():
    tests = [
        ("unmarked Nyaa bundle inferred as complete series", test_check_feeds_infers_complete_bundle),
        ("Releasing / MANGA-format / explicit-volume controls", test_non_finished_and_non_novel_releases_are_not_inferred),
        ("second scan is idempotent", test_second_scan_is_idempotent),
        ("tracker publication metadata + monitored status", test_tracker_persists_publication_and_monitored_status),
        ("metadata cache publication round-trip", test_database_cache_and_provider_publication_roundtrip),
        ("static guards (provider/scheduler/UI)", test_main_py_and_provider_static_guards),
    ]
    failures = []
    print("=" * 78)
    print("AniList publication metadata / Nyaa bundle volume inference integration suite")
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
