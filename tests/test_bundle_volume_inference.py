"""Scheduler tests for AniList publication metadata + Nyaa unmarked-bundle
volume inference (CONTRACT v3 §4, §7-B).

Hermetic: no network. The shared harness from ``test_series_matching`` provides
the temp Database/TaskManager, fake Calibre scanner, fake feed provider and
metadata monkeypatch. Explicit ``check()`` only (no bare asserts).
"""

import json
import os
import sys
import time
import types
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import test_series_matching as matching
from test_series_matching import (
    FakeProvider,
    FakeScanner,
    MetadataFake,
    activity_events,
    check,
    cleanup_temp_databases,
    get_release,
    insert_series,
    make_db,
    new_manager,
    normalize_text,
    patched_metadata,
    patched_provider,
    patched_scanner,
    section,
    shutdown_manager,
)

import app.scheduler as scheduler_module
from app.providers.base import ReleaseItem
from app.providers.metadata import NovelMetadataProvider

SERIES = "The Executioner and Her Way of Life"
FINISHED_META = {
    "title": SERIES,
    "english_title": SERIES,
    "format": "NOVEL",
    "format_label": "Light Novel",
    "is_light_novel": True,
    "status": "FINISHED",
    "status_label": "Finished",
    "volumes": 11,
    "chapters": 120,
    "total_volumes": 11,
    "source": "AniList",
}




@contextmanager
def recorded_publication_persist(db, calls):
    """Records ``store_series_publication_metadata`` calls (real or stubbed)."""
    original = getattr(db, "store_series_publication_metadata", None)

    def recorder(series_id, meta):
        calls.append((series_id, dict(meta) if isinstance(meta, dict) else meta))
        if original is not None:
            return original(series_id, meta)
        return None

    db.store_series_publication_metadata = recorder
    try:
        yield calls
    finally:
        if original is not None:
            db.store_series_publication_metadata = original
        else:
            try:
                delattr(db, "store_series_publication_metadata")
            except AttributeError:
                pass


def seed_publication_series(db, name=SERIES, meta=None, is_monitored=1):
    return insert_series(
        db, name, is_monitored=is_monitored, anilist_meta=meta or dict(FINISHED_META)
    )


def unmarked_release(series_name=SERIES, guid="exec-bundle", **overrides):
    release = {
        "title": f"{series_name} [Yen Press] [Stick]",
        "link": f"https://example.test/{guid}",
        "guid": guid,
        "series_name": series_name,
        "volume": None,
        "volumes": [],
        "media_type": "novel",
    }
    release.update(overrides)
    return release


# ---------------------------------------------------------------------------
# 1. Inference condition matrix + exact mutations
# ---------------------------------------------------------------------------
def test_inference_condition_matrix():
    section("1. infer_nyaa_bundle_volumes: condition matrix + mutations")
    db = make_db()
    tm = new_manager(db)
    metadata = MetadataFake()
    try:
        seed_publication_series(db)
        seed_publication_series(db, "Releasing Series", meta={
            **FINISHED_META, "status": "RELEASING", "status_label": "Releasing",
        })
        seed_publication_series(db, "Zero Volumes Series", meta={
            **FINISHED_META, "volumes": 0, "total_volumes": 0,
        })
        seed_publication_series(db, "Manga Format Series", meta={
            **FINISHED_META, "format": "MANGA", "format_label": "Manga",
            "is_light_novel": False,
        })
        metadata.reset(result=None)
        with patched_metadata(metadata):
            # Setting disabled: no mutation.
            db.set_setting(scheduler_module.NYAA_BUNDLE_VOLUME_INFERENCE_KEY, "false")
            release = unmarked_release()
            check(tm.infer_nyaa_bundle_volumes(release, SERIES, True) is False,
                  "disabled setting must skip inference")
            check("volumes" not in release or not release["volumes"],
                  f"disabled setting must not mutate: {release}")
            db.set_setting(scheduler_module.NYAA_BUNDLE_VOLUME_INFERENCE_KEY, "true")

            check(tm.infer_nyaa_bundle_volumes(unmarked_release(), SERIES, False) is False,
                  "non-Nyaa feed must skip inference")
            check(tm.infer_nyaa_bundle_volumes(
                unmarked_release(volume=3.0), SERIES, True) is False,
                "explicit volume must skip inference")
            check(tm.infer_nyaa_bundle_volumes(
                unmarked_release(volumes=[1.0, 2.0]), SERIES, True) is False,
                "explicit volumes list must skip inference")
            check(tm.infer_nyaa_bundle_volumes(
                unmarked_release(is_chapter=True), SERIES, True) is False,
                "chapter release must skip inference")
            check(tm.infer_nyaa_bundle_volumes(
                unmarked_release(media_type="manga"), SERIES, True) is False,
                "manga must skip inference")
            check(tm.infer_nyaa_bundle_volumes(
                unmarked_release(media_type="audiobook"), SERIES, True) is False,
                "audiobook must skip inference")
            check(tm.infer_nyaa_bundle_volumes(
                unmarked_release(series_name="Unknown Unmarked Series"),
                "Unknown Unmarked Series", True) is False,
                "missing publication metadata must skip inference")
            check(tm.infer_nyaa_bundle_volumes(
                unmarked_release(series_name="Releasing Series"), "Releasing Series", True
            ) is False, "Releasing status must skip inference")
            check(tm.infer_nyaa_bundle_volumes(
                unmarked_release(series_name="Zero Volumes Series"),
                "Zero Volumes Series", True) is False,
                "volumes 0 must skip inference")
            check(tm.infer_nyaa_bundle_volumes(
                unmarked_release(series_name="Manga Format Series"),
                "Manga Format Series", True) is False,
                "non-light-novel format must skip inference")

            # The contract example: Format Light Novel, Status Finished, 11 vols.
            bundle = unmarked_release()
            inferred = tm.infer_nyaa_bundle_volumes(bundle, SERIES, True)
            check(inferred is True, "finished 11-volume light novel must infer a bundle")
            check(bundle["volumes"] == [float(v) for v in range(1, 12)],
                  f"volumes 1..11: {bundle['volumes']}")
            check(bundle["volume"] == 1.0, f"volume 1.0: {bundle['volume']}")
            check(bundle["volume_start"] == 1.0, "volume_start 1.0")
            check(bundle["volume_end"] == 11.0, f"volume_end 11.0: {bundle.get('volume_end')}")
            check(bundle["is_bundle"] is True, "is_bundle True for N>1")
            check(bundle["is_complete"] is True, "is_complete True")
            check(bundle["volume_inferred"] is True, "volume_inferred True")
            check(bundle["volume_inference_source"] == "anilist_finished",
                  f"source: {bundle.get('volume_inference_source')}")

            # A single finished volume is complete but not a bundle.
            seed_publication_series(db, "One Volume Series", meta={
                **FINISHED_META, "volumes": 1, "total_volumes": 1,
            })
            single = unmarked_release(series_name="One Volume Series")
            check(tm.infer_nyaa_bundle_volumes(single, "One Volume Series", True) is True,
                  "1-volume finished series infers")
            check(single["is_bundle"] is False, "N=1 is not a bundle")
            check(single["volumes"] == [1.0], f"N=1 volumes: {single['volumes']}")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 2. publication_metadata_for: tracker first, provider + persist, budget, TTL
# ---------------------------------------------------------------------------
def test_publication_tracker_first():
    section("2. publication_metadata_for: tracked-row metadata first (no fetch)")
    db = make_db()
    tm = new_manager(db)
    metadata = MetadataFake()
    try:
        seed_publication_series(db)
        # Normalized fallback: punctuation-only difference must still find the row.
        insert_series(db, "Punctuated Series!", is_monitored=1, anilist_meta={
            **FINISHED_META, "title": "Punctuated Series", "english_title": "Punctuated Series",
        })
        metadata.reset(result=None)
        with patched_metadata(metadata):
            pub = tm.publication_metadata_for(SERIES)
            check(isinstance(pub, dict), f"tracker metadata must resolve: {pub}")
            check(pub["status"] == "FINISHED" and pub["volumes"] == 11,
                  f"publication fields: {pub}")
            check(pub["is_light_novel"] is True, f"light novel flag: {pub}")
            check(len(metadata.calls) == 0,
                  f"tracker metadata must not fetch: {metadata.calls}")

            normalized = tm.publication_metadata_for("Punctuated Series")
            check(isinstance(normalized, dict) and normalized["volumes"] == 11,
                  f"normalized tracker lookup: {normalized}")
    finally:
        shutdown_manager(tm)


def test_publication_provider_and_persist():
    section("3. publication_metadata_for: bounded provider fetch + persist + cache")
    db = make_db()
    tm = new_manager(db)
    metadata = MetadataFake()
    persist_calls = []
    try:
        series_id = insert_series(db, "Provider Series", is_monitored=1)
        metadata.reset(result=dict(FINISHED_META))
        budget = {"remaining": 2}
        with patched_metadata(metadata), \
                recorded_publication_persist(db, persist_calls):
            pub = tm.publication_metadata_for("Provider Series", budget)
            check(isinstance(pub, dict) and pub["volumes"] == 11,
                  f"provider publication: {pub}")
            check(len(metadata.calls) == 1, f"one provider fetch: {metadata.calls}")
            check(budget["remaining"] == 1, f"budget decremented: {budget}")
            check(len(persist_calls) == 1 and persist_calls[0][0] == series_id,
                  f"persisted to the tracker row: {persist_calls}")

            second = tm.publication_metadata_for("Provider Series", budget)
            check(second == pub, "cache hit returns the same publication")
            check(len(metadata.calls) == 1, f"cache hit must not fetch: {metadata.calls}")
            check(budget["remaining"] == 1, f"cache hit must not spend budget: {budget}")

            persisted_row = db.get_series_by_name("Provider Series")
            try:
                persisted_meta = json.loads(persisted_row.get("anilist_meta_json") or "{}")
            except (TypeError, ValueError):
                persisted_meta = {}
            check(persisted_meta.get("volumes") == 11 and persisted_meta.get("status") == "FINISHED",
                  f"publication persisted to the tracker row: {persisted_meta}")

            # TTL expiry refetches when no tracker row has persisted metadata.
            ttl_name = "No Tracker TTL Series"
            metadata.reset(result=dict(FINISHED_META))
            budget_ttl = {"remaining": 5}
            tm.publication_metadata_for(ttl_name, budget_ttl)
            check(len(metadata.calls) == 1, f"initial TTL fetch: {metadata.calls}")
            ttl_key = normalize_text(ttl_name)
            tm._publication_cache[ttl_key]["ts"] = (
                time.time() - scheduler_module.PUBLICATION_CACHE_TTL_SECONDS - 5
            )
            tm.publication_metadata_for(ttl_name, budget_ttl)
            check(len(metadata.calls) == 2, f"TTL expiry refetches: {metadata.calls}")

            # Exhausted budget returns None without calling the provider.
            fresh = new_manager(db)
            try:
                metadata.reset(result=dict(FINISHED_META))
                with patched_metadata(metadata):
                    exhausted = fresh.publication_metadata_for(
                        "Another Unknown Series", {"remaining": 0}
                    )
                check(exhausted is None, f"exhausted budget returns None: {exhausted}")
                check(len(metadata.calls) == 0, "exhausted budget must not fetch")
            finally:
                shutdown_manager(fresh)

            # No budget -> a one-off budget applies per call (one lookup each).
            fresh = new_manager(db)
            try:
                metadata.reset(result=dict(FINISHED_META))
                with patched_metadata(metadata):
                    one_off = fresh.publication_metadata_for("One Off Series")
                    check(isinstance(one_off, dict) and one_off["volumes"] == 11,
                          f"one-off budget lookup: {one_off}")
                    check(len(metadata.calls) == 1,
                          f"one-off budget allows exactly one lookup: {metadata.calls}")

                    shared_budget = {"remaining": 3}
                    for index in range(4):
                        fresh.publication_metadata_for(
                            f"Shared Budget Series {index}", shared_budget
                        )
                check(len(metadata.calls) == 4,
                      f"shared budget of 3 caps lookups: {metadata.calls}")
                check(shared_budget["remaining"] == 0,
                      f"shared budget exhausted: {shared_budget}")
            finally:
                shutdown_manager(fresh)

            # Provider failure never raises.
            fresh = new_manager(db)
            try:
                metadata.reset(error=RuntimeError("simulated metadata outage"))
                with patched_metadata(metadata):
                    failed = fresh.publication_metadata_for("Broken Series")
                check(failed is None, f"provider failure returns None: {failed}")
            finally:
                shutdown_manager(fresh)
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 3. _ingest_backfill_releases inference matrix
# ---------------------------------------------------------------------------
class RecordingMatcher:
    def __init__(self):
        self.calls = []
        self.results = []
        self._index = 0

    def check_release(self, release, auto_request_monitored=True):
        self.calls.append(dict(release))
        if self._index < len(self.results):
            result = self.results[self._index]
            self._index += 1
            return result
        return {
            "status": "NEW",
            "matched_book_id": None,
            "matched_series": None,
            "effective_volume": 1.0,
            "missing_volumes": [1.0],
            "reason": "test",
        }


def make_backfill_release(series_name, guid, volume=None, title=None):
    return ReleaseItem(
        title=title or f"{series_name} [Yen Press] [Stick]",
        link=f"https://example.test/{guid}",
        series_name=series_name,
        volume=volume,
        pub_date="2026-11-02T00:00:00+00:00",
        guid=guid,
    )


def test_ingest_backfill_inference_matrix():
    section("4. _ingest_backfill_releases: J-Novels untouched, Nyaa infers bundles")
    metadata = MetadataFake()
    with patched_metadata(metadata):
        # --- J-Novels: infer flag off -> unmarked release stays unmarked. ---
        db = make_db()
        tm = new_manager(db)
        try:
            seed_publication_series(db)
            tm.classify_media = lambda release: "novel"
            matcher = RecordingMatcher()
            matcher.results = [{
                "status": "REQUESTED", "matched_book_id": None,
                "matched_series": {"series_id": 902, "series_name": SERIES},
                "effective_volume": 1.0, "missing_volumes": [1.0], "reason": "test",
            }]
            counts = tm._ingest_backfill_releases(
                [make_backfill_release(SERIES, "jnovels-unmarked")],
                matcher,
                {SERIES.lower(): {"name": SERIES}},
                set(),
            )
            check(counts["upserted"] == 1, f"J-Novels release ingested: {counts}")
            check(len(matcher.calls) == 1, "matcher called once")
            check(not matcher.calls[0].get("volumes"),
                  f"J-Novels matcher payload must be untouched: {matcher.calls[0]}")
            row = db.get_releases(limit=10)[0]
            check(not row["is_bundle"], "J-Novels row is not a bundle")
            check(json.loads(row["volumes_json"] or "[]") != [float(v) for v in range(1, 12)],
                  f"J-Novels row must not carry inferred 1..11 volumes: {row['volumes_json']}")

            # --- Nyaa: infer flag on -> complete-series bundle + missing set. ---
            db2 = make_db()
            tm2 = new_manager(db2)
            try:
                seed_publication_series(db2)
                tm2.classify_media = lambda release: "novel"
                matcher2 = RecordingMatcher()
                matcher2.results = [{
                    "status": "REQUESTED", "matched_book_id": None,
                    "matched_series": {"series_id": 902, "series_name": SERIES},
                    "effective_volume": 1.0,
                    "missing_volumes": [float(v) for v in range(2, 12)],
                    "reason": "bundle",
                }]
                counts2 = tm2._ingest_backfill_releases(
                    [make_backfill_release(SERIES, "nyaa-unmarked")],
                    matcher2,
                    {SERIES.lower(): {"name": SERIES}},
                    set(),
                    infer_nyaa_bundles=True,
                )
                check(counts2["upserted"] == 1, f"Nyaa bundle ingested: {counts2}")
                payload = matcher2.calls[0]
                check(payload.get("volumes") == [float(v) for v in range(1, 12)],
                      f"matcher sees inferred volumes: {payload.get('volumes')}")
                check(payload.get("is_bundle") is True, "matcher sees is_bundle")
                row2 = db2.get_releases(limit=10)[0]
                check(json.loads(row2["volumes_json"]) == [float(v) for v in range(1, 12)],
                      f"stored volumes_json: {row2['volumes_json']}")
                check(bool(row2["is_bundle"]), "stored is_bundle")
                check(row2["volume"] == 1.0, f"stored volume 1.0: {row2['volume']}")
                missing = json.loads(row2["missing_vols_json"])
                check(missing == [float(v) for v in range(2, 12)],
                      f"matcher missing volumes copied to the row: {missing}")

                # Explicit volume release is untouched by the inference.
                matcher2b = RecordingMatcher()
                matcher2b.results = [{
                    "status": "IN_CALIBRE", "matched_book_id": 12,
                    "matched_series": {"series_id": 902, "series_name": SERIES},
                    "effective_volume": 3.0, "missing_volumes": [], "reason": "owned",
                }]
                tm2._ingest_backfill_releases(
                    [make_backfill_release(SERIES, "nyaa-explicit-vol3", volume=3.0,
                                           title=f"{SERIES} Vol. 3 [Yen Press]")],
                    matcher2b,
                    {SERIES.lower(): {"name": SERIES}},
                    set(),
                    infer_nyaa_bundles=True,
                )
                explicit_payload = matcher2b.calls[0]
                check(explicit_payload.get("volume") == 3.0,
                      f"explicit volume kept: {explicit_payload.get('volume')}")
                check(explicit_payload.get("volumes") == [3.0],
                      f"explicit volume list kept as-is: {explicit_payload.get('volumes')}")
                explicit_row = [r for r in db2.get_releases(limit=10)
                                if r["guid"] == "nyaa-explicit-vol3"][0]
                check(explicit_row["volume"] == 3.0, "explicit stored volume")
                check(not explicit_row["is_bundle"], "explicit release is not a bundle")

                # Budget 0 disables the publication lookup -> no inference.
                db2.set_setting(scheduler_module.SERIES_ANILIST_MATCH_BUDGET_KEY, "0")
                matcher2c = RecordingMatcher()
                tm2._ingest_backfill_releases(
                    [make_backfill_release("Budget Zero Series", "nyaa-budget-zero",
                                           title="Budget Zero Series [Stick]")],
                    matcher2c,
                    {},
                    set(),
                    infer_nyaa_bundles=True,
                )
                check(not matcher2c.calls[0].get("volumes"),
                      "budget 0 must skip Nyaa bundle inference")
            finally:
                shutdown_manager(tm2)
        finally:
            shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 4. check_feeds integration
# ---------------------------------------------------------------------------
def test_check_feeds_nyaa_bundle_inference():
    section("5. check_feeds: Nyaa bundle stored as complete series + activity log")
    db = make_db()
    FakeScanner.reset(
        series=[{"series_id": 902, "series_name": SERIES, "book_count": 1,
                 "latest_index": 1.0, "authors": "Author"}],
        books=[{"book_id": 902001, "title": f"{SERIES} Vol 1", "series_id": 902,
                "series_name": SERIES, "series_index": 1.0}],
        volumes_map={"902": {1.0}},
    )
    tm = new_manager(db)
    metadata = MetadataFake()
    try:
        seed_publication_series(db)
        db.add_feed("Nyaa LN", "nyaa", "https://nyaa.si/?page=rss&c=3_1",
                    json.dumps({}), True, 60)
        tm.classify_media = lambda release: "novel"
        item = ReleaseItem(
            title=f"{SERIES} [Yen Press] [Stick]",
            link="https://example.test/exec-bundle",
            series_name=SERIES,
            volume=None,
            pub_date="2026-11-02T00:00:00+00:00",
            guid="exec-bundle",
        )
        FakeProvider.reset(items=[item])
        metadata.reset(result=None)
        with patched_metadata(metadata), \
                patched_scanner(), patched_provider():
            result = tm.check_feeds()

        check(result["status"] == "success", f"scan status: {result}")
        rows = db.get_releases(limit=20)
        check(len(rows) == 1, f"one bundle row: {[r['guid'] for r in rows]}")
        row = rows[0]
        check(row["series_name"] == SERIES, f"series: {row['series_name']}")
        check(json.loads(row["volumes_json"]) == [float(v) for v in range(1, 12)],
              f"stored volumes 1..11: {row['volumes_json']}")
        check(bool(row["is_bundle"]), "stored is_bundle")
        check(row["volume"] == 1.0, f"stored volume 1.0: {row['volume']}")
        check(json.loads(row["missing_vols_json"]) == [float(v) for v in range(2, 12)],
              f"missing volumes 2..11: {row['missing_vols_json']}")
        check(row["status"] == "REQUESTED", f"bundle status: {row['status']}")

        events = activity_events(db, "BUNDLE_VOLUMES_INFERRED")
        check(len(events) == 1, f"exactly one BUNDLE_VOLUMES_INFERRED activity: {len(events)}")
        details = json.loads(events[0]["details"] or "{}")
        check(details.get("series") == SERIES, f"activity series: {details}")
        check(details.get("volumes") == 11, f"activity volumes: {details}")
        check(details.get("anilist_status") == "FINISHED", f"activity status: {details}")
        check(details.get("anilist_format") == "NOVEL", f"activity format: {details}")

        # Second scan of the same release: no duplicate row, no duplicate log.
        FakeProvider.reset(items=[item])
        with patched_metadata(metadata), \
                patched_scanner(), patched_provider():
            second = tm.check_feeds()
        check(second["status"] == "success", f"second scan status: {second}")
        check(len(db.get_releases(limit=20)) == 1, "second scan must not duplicate the row")
        check(len(activity_events(db, "BUNDLE_VOLUMES_INFERRED")) == 1,
              "second scan must not duplicate the inference log")
    finally:
        shutdown_manager(tm)


def test_check_feeds_non_nyaa_feed_untouched():
    section("6. check_feeds: non-Nyaa feeds never get bundle inference")
    db = make_db()
    FakeScanner.reset(series=[], books=[], volumes_map={})
    tm = new_manager(db)
    metadata = MetadataFake()
    try:
        seed_publication_series(db)
        db.add_feed("Regular Feed", "rss", "https://example.test/feed",
                    json.dumps({}), True, 60)
        tm.classify_media = lambda release: "novel"
        item = ReleaseItem(
            title=f"{SERIES} [Yen Press] [Stick]",
            link="https://example.test/regular",
            series_name=SERIES,
            volume=None,
            pub_date="2026-11-02T00:00:00+00:00",
            guid="regular-unmarked",
        )
        FakeProvider.reset(items=[item])
        metadata.reset(result=None)
        with patched_metadata(metadata), \
                patched_scanner(), patched_provider():
            result = tm.check_feeds()
        check(result["status"] == "success", f"scan status: {result}")
        row = db.get_releases(limit=10)[0]
        check(json.loads(row["volumes_json"] or "[]") != [float(v) for v in range(1, 12)],
              f"non-Nyaa release must stay unmarked: {row['volumes_json']}")
        check(not row["is_bundle"], "non-Nyaa release is not a bundle")
        check(not activity_events(db, "BUNDLE_VOLUMES_INFERRED"),
              "no bundle activity for a non-Nyaa feed")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
TESTS = [
    ("inference condition matrix", test_inference_condition_matrix),
    ("publication tracker first", test_publication_tracker_first),
    ("publication provider persist", test_publication_provider_and_persist),
    ("backfill inference matrix", test_ingest_backfill_inference_matrix),
    ("check_feeds nyaa bundle", test_check_feeds_nyaa_bundle_inference),
    ("check_feeds non-nyaa", test_check_feeds_non_nyaa_feed_untouched),
]


def main():
    print("=" * 78)
    print("  NovelTrax AniList publication + Nyaa bundle inference tests")
    print("=" * 78)

    failures = []
    try:
        for name, test in TESTS:
            try:
                test()
                print(f"  [PASS] {name}")
            except Exception as exc:  # noqa: BLE001 - report every failure
                failures.append((name, exc))
                print(f"  [FAIL] {name}: {type(exc).__name__}: {exc}")
                import traceback
                print(traceback.format_exc())
    finally:
        cleanup_temp_databases()

    print("\n" + "=" * 78)
    if failures:
        print(f"  {len(TESTS) - len(failures)}/{len(TESTS)} test groups passed, "
              f"{len(failures)} failed")
        print("=" * 78)
        raise SystemExit(1)
    print(f"  all {len(TESTS)} test groups passed")
    print("=" * 78)
    print("ALL BUNDLE-VOLUME-INFERENCE TESTS PASSED")


if __name__ == "__main__":
    main()
