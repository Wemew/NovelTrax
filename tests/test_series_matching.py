"""Series identity integration tests: parser/matcher alt-title matching,
scheduler identity snapshot/resolution, Calibre-sync auto-link + bounded AniList
enrichment + status reconciliation, check_feeds alternate-title fallback, manual
match wrapper and the backfill display-title (no stable-key rewrite) fix.

Hermetic: no network. ``NovelMetadataProvider.fetch_series_metadata`` and
``app.scheduler.CalibreScanner`` are monkeypatched per test; feeds use a fake
provider. Temp databases live directly under ``tempfile.gettempdir()`` because
``tempfile.mkdtemp()`` is unusable in this sandbox. Explicit ``check()`` raises
(no bare asserts so the runner's assert-preserving recompile has nothing to
strip).
"""

import asyncio
import gc
import json
import os
import sys
import tempfile
import time
import traceback
import types
import uuid
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))


# ---------------------------------------------------------------------------
# check() helper -- explicit raises, never bare asserts
# ---------------------------------------------------------------------------
def check(condition, message):
    if not condition:
        raise AssertionError(message)


def section(title):
    print(f"\n--- {title} ---")


# ---------------------------------------------------------------------------
# Import app.scheduler without the third-party provider stack (bs4/feedparser
# are absent from this test interpreter).
# ---------------------------------------------------------------------------
REGISTRY_STUBBED = False
try:
    from app.scheduler import TaskManager  # noqa: F401
    import app.scheduler as scheduler_module
except ImportError:
    _registry_stub = types.ModuleType("app.providers.registry")

    def _unpatched_create_provider(*args, **kwargs):
        raise RuntimeError("create_provider was not patched for this test")

    _registry_stub.create_provider = _unpatched_create_provider
    sys.modules["app.providers.registry"] = _registry_stub
    from app.scheduler import TaskManager  # noqa: F401
    import app.scheduler as scheduler_module
    REGISTRY_STUBBED = True

from app.calibre.matcher import CalibreMatcher
from app.database import Database
from app.parser import match_series_in_calibre, normalize_text
from app.providers.base import ReleaseItem
from app.providers.metadata import NovelMetadataProvider
from app.series_identity import (
    AUTO_LINK_FUZZY_THRESHOLD,
    CALIBRE_MATCH_THRESHOLD,
    CalibreSeriesIndex,
    build_alias_lookup,
    resolve_series_title,
    title_candidates,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
TRAPPED_CALIBRE = "Trapped in a Dating Sim: The World of Otome Games is Tough for Us, Too"
TRAPPED_MOBS_CALIBRE = "Trapped in a Dating Sim: The World of Otome Games is Tough for Mobs, Too"
TRAPPED_ANILIST = "Trapped in a Dating Sim: Otome Games Are Tough for Us, Too!"
TRAPPED_ROMAJI = "Otome Game no Hametsu Flag"
BOOK_ID_327_VOL1 = 327001
BOOK_ID_327_VOL2 = 327002
BOOK_ID_174_VOL1 = 174001


class FakeScanner:
    """Scriptable stand-in for ``app.scheduler.CalibreScanner``."""

    available = True
    series = []
    books = []
    volumes_map = {}
    series_calls = 0
    books_calls = 0
    volumes_map_calls = 0

    def __init__(self, calibre_path):
        self.db_path = calibre_path

    @classmethod
    def reset(cls, series=None, books=None, volumes_map=None, available=True):
        cls.available = available
        cls.series = [dict(item) for item in (series or [])]
        cls.books = [dict(item) for item in (books or [])]
        cls.volumes_map = dict(volumes_map or {})
        cls.series_calls = 0
        cls.books_calls = 0
        cls.volumes_map_calls = 0

    @classmethod
    def library(cls):
        """The user's "Trapped in a Dating Sim" library (the contract case)."""
        series = [
            {
                "series_id": 327,
                "series_name": TRAPPED_CALIBRE,
                "book_count": 3,
                "latest_index": 3.0,
                "authors": "Author One",
            },
            {
                "series_id": 174,
                "series_name": TRAPPED_MOBS_CALIBRE,
                "book_count": 1,
                "latest_index": 1.0,
                "authors": "Author One",
            },
        ]
        books = [
            {"book_id": BOOK_ID_327_VOL1, "title": f"{TRAPPED_CALIBRE} Vol 1",
             "series_id": 327, "series_name": TRAPPED_CALIBRE, "series_index": 1.0},
            {"book_id": BOOK_ID_327_VOL2, "title": f"{TRAPPED_CALIBRE} Vol 2",
             "series_id": 327, "series_name": TRAPPED_CALIBRE, "series_index": 2.0},
            {"book_id": 327003, "title": f"{TRAPPED_CALIBRE} Vol 3",
             "series_id": 327, "series_name": TRAPPED_CALIBRE, "series_index": 3.0},
            {"book_id": BOOK_ID_174_VOL1, "title": f"{TRAPPED_MOBS_CALIBRE} Vol 1",
             "series_id": 174, "series_name": TRAPPED_MOBS_CALIBRE, "series_index": 1.0},
        ]
        cls.reset(series=series, books=books, volumes_map={"327": {1.0, 2.0, 3.0}, "174": {1.0}})

    def is_available(self):
        return FakeScanner.available

    def get_all_series(self):
        FakeScanner.series_calls += 1
        return [dict(item) for item in FakeScanner.series]

    def get_all_books(self):
        FakeScanner.books_calls += 1
        return [dict(item) for item in FakeScanner.books]

    def get_series_volumes_map(self):
        FakeScanner.volumes_map_calls += 1
        return {key: set(value) for key, value in FakeScanner.volumes_map.items()}


@contextmanager
def patched_scanner():
    original = scheduler_module.CalibreScanner
    scheduler_module.CalibreScanner = FakeScanner
    try:
        yield
    finally:
        scheduler_module.CalibreScanner = original


class MetadataFake:
    """Patch for ``NovelMetadataProvider.fetch_series_metadata``."""

    def __init__(self):
        self.calls = []
        self.result = None          # dict, callable(name), or None
        self.error = None

    def __call__(self, raw_title):
        name = str(raw_title or "")
        self.calls.append(name)
        if self.error is not None:
            raise self.error
        if callable(self.result):
            return self.result(name)
        return self.result

    def reset(self, result=None, error=None):
        self.calls = []
        self.result = result
        self.error = error


@contextmanager
def patched_metadata(fake):
    raw = NovelMetadataProvider.__dict__.get("fetch_series_metadata")
    NovelMetadataProvider.fetch_series_metadata = fake
    try:
        yield fake
    finally:
        if raw is not None:
            NovelMetadataProvider.fetch_series_metadata = raw
        else:
            try:
                delattr(NovelMetadataProvider, "fetch_series_metadata")
            except AttributeError:
                pass


class FakeProvider:
    """RSS-ish stand-in for ``app.scheduler.create_provider``."""

    items = []
    error = None
    calls = 0

    def __init__(self, name=None, url=None, config=None):
        self.name = name
        self.url = url
        self.config = config or {}
        self.last_scan_meta = {}

    @classmethod
    def reset(cls, items=None, error=None):
        cls.items = list(items or [])
        cls.error = error
        cls.calls = 0

    def fetch_releases(self):
        FakeProvider.calls += 1
        if FakeProvider.error is not None:
            raise FakeProvider.error
        return list(FakeProvider.items)

    def fetch_releases_since(self, *, cutoff_iso=None, stop_guid=None, max_pages=None):
        return self.fetch_releases()


@contextmanager
def patched_provider():
    original = scheduler_module.create_provider

    def _fake_create_provider(feed_type, name, url, config=None):
        return FakeProvider(name=name, url=url, config=config)

    scheduler_module.create_provider = _fake_create_provider
    try:
        yield
    finally:
        scheduler_module.create_provider = original


_TEMP_DB_PATHS = []


def make_db():
    """Temporary Database: unique file in the temp dir (no mkdtemp in sandbox)."""
    db_path = os.path.join(
        tempfile.gettempdir(),
        "noveltrax_series_matching_%d_%s.db" % (os.getpid(), uuid.uuid4().hex[:10]),
    )
    _TEMP_DB_PATHS.extend([db_path, db_path + "-wal", db_path + "-shm"])
    db = Database(db_path)
    # Database init seeds default feeds; the tests add exactly what they need.
    with db.get_connection() as conn:
        conn.cursor().execute("DELETE FROM feeds;")
        conn.commit()
    db.set_setting("filter_non_novels", "false")
    return db


def new_manager(db):
    return TaskManager(db, calibre_path=tempfile.gettempdir())


def shutdown_manager(tm):
    try:
        tm._cover_executor.shutdown(wait=False)
    except Exception:
        pass


def cleanup_temp_databases():
    gc.collect()
    for path in _TEMP_DB_PATHS:
        for _ in range(3):
            try:
                os.remove(path)
                break
            except FileNotFoundError:
                break
            except OSError:
                gc.collect()
                time.sleep(0.05)


def insert_series(db, name, calibre_id=None, is_monitored=1, display_title=None,
                  anilist_meta=None, owned_volumes=None, book_count=0, latest_index=0.0):
    """Seeds a ``series_tracking`` row without touching other owners' modules."""
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO series_tracking "
            "(name, calibre_series_id, is_monitored, book_count, latest_index, owned_volumes_json) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (
                name, calibre_id, is_monitored, book_count, latest_index,
                json.dumps(sorted(owned_volumes or [])),
            ),
        )
        conn.commit()
        series_id = cur.lastrowid
    if display_title is not None or anilist_meta is not None:
        db.update_series_identity(
            series_id,
            display_title=display_title,
            anilist_meta_json=json.dumps(anilist_meta) if anilist_meta is not None else None,
        )
    return series_id


def insert_release(db, guid, title, series_name, volume=None, status="NEW", link=None):
    if link is None:
        link = f"https://example.test/{guid}"
    release = {
        "guid": guid,
        "title": title,
        "link": link,
        "series_name": series_name,
        "volume": volume,
        "status": status,
        "pub_date": "2026-11-01T00:00:00+00:00",
        "media_type": "novel",
    }
    is_new, release_id = db.upsert_release(release)
    return release_id


def get_release(db, release_id):
    return db.get_release_by_id(release_id)


def activity_events(db, event_type):
    return [row for row in db.get_logs(500) if row["event_type"] == event_type]


def make_anilist_meta(title, synonyms=None, anilist_id=None):
    return {
        "title": title,
        "english_title": title,
        "romaji_title": "",
        "native_title": "",
        "synonyms": list(synonyms or []),
        "anilist_id": anilist_id,
        "source": "AniList",
    }


# ---------------------------------------------------------------------------
# 1. Parser / CalibreSeriesIndex: the user's case + ambiguity + head guard
# ---------------------------------------------------------------------------
def test_parser_user_case_and_ambiguity():
    section("1. parser match_series_in_calibre: synonym exact + ambiguity guards")

    FakeScanner.reset(
        series=[
            {"series_id": 327, "series_name": TRAPPED_CALIBRE, "book_count": 8, "latest_index": 8.0},
            {"series_id": 174, "series_name": TRAPPED_MOBS_CALIBRE, "book_count": 3, "latest_index": 3.0},
        ],
        books=[],
    )
    calibre_series = [dict(item) for item in FakeScanner.series]

    # The user's case: AniList English title + the Calibre spelling as an alt
    # name (AniList synonym) is an EXACT hit on #327.
    matched = match_series_in_calibre(
        TRAPPED_ANILIST, calibre_series, alt_names=[TRAPPED_CALIBRE]
    )
    check(matched is not None, "synonym-driven match must resolve to a Calibre series")
    check(matched["series_id"] == 327, f"must match #327: {matched}")

    # The "for Mobs" series stays itself (exact name).
    mobs = match_series_in_calibre(TRAPPED_MOBS_CALIBRE, calibre_series)
    check(mobs is not None and mobs["series_id"] == 174, f"mobs exact match: {mobs}")

    # A bare shared franchise prefix is ambiguous (two Calibre series) -> None.
    prefix = match_series_in_calibre("Trapped in a Dating Sim", calibre_series)
    check(prefix is None, f"shared prefix must be ambiguous -> None (got {prefix})")

    # Longer shared prefix still matches both -> ambiguous -> None (never guess).
    shared_prefix = match_series_in_calibre(
        "Trapped in a Dating Sim: The World of Otome Games is Tough", calibre_series
    )
    check(shared_prefix is None, f"prefix-only match must be ambiguous (got {shared_prefix})")

    # Without the synonym, the display title must not be silently collapsed onto
    # either Trapped series.
    no_alt = match_series_in_calibre(TRAPPED_ANILIST, calibre_series)
    check(no_alt is None, f"display title without alt names must not guess (got {no_alt})")

    # Two-argument legacy call still works for an exact name.
    legacy = match_series_in_calibre(TRAPPED_CALIBRE, calibre_series)
    check(legacy is not None and legacy["series_id"] == 327, f"legacy 2-arg call: {legacy}")


def test_parser_head_token_guard():
    section("2. parser: fuzzy head-token guard (MangaTrax port)")
    calibre_series = [
        {"series_id": 9, "series_name": "The Red Ranger Becomes an Adventurer in Another World"},
        {"series_id": 10, "series_name": "Level 0 Demon King Becomes an Adventurer in Another World"},
    ]
    # Shared generic suffix, different head: must never fuzzy-match.
    rejected = match_series_in_calibre(
        "Level 0 Demon King Becomes an Adventurer in Another World",
        calibre_series,
    )
    check(rejected is not None and rejected["series_id"] == 10,
          f"exact head must still match: {rejected}")
    mismatch = match_series_in_calibre(
        "A Random Farmer Becomes an Adventurer in Another World",
        calibre_series,
    )
    check(mismatch is None, f"fuzzy tail-only match must be rejected: {mismatch}")

    # Substring containment still resolves a proportionally-sized alternate title.
    contained = match_series_in_calibre(
        "Ascendance of a Bookworm",
        [{"series_id": 11, "series_name": "Ascendance of a Bookworm: Part 2"}],
    )
    check(contained is not None and contained["series_id"] == 11,
          f"substring containment must still match: {contained}")


# ---------------------------------------------------------------------------
# 2. CalibreMatcher: series_aliases + alt_series_names + canonical keys
# ---------------------------------------------------------------------------
def test_matcher_alt_series_names():
    section("3. CalibreMatcher: aliases, alt_series_names, canonical keys")
    FakeScanner.library()
    series = [dict(item) for item in FakeScanner.series]
    books = [dict(item) for item in FakeScanner.books]
    alias_map = {normalize_text(TRAPPED_ANILIST): TRAPPED_CALIBRE}
    matcher = CalibreMatcher(series, books, series_aliases=alias_map)

    result = matcher.check_release(
        {"title": f"{TRAPPED_ANILIST} (Novel)", "series_name": TRAPPED_ANILIST, "volume": None}
    )
    check(result["status"] == "IN_CALIBRE", f"alias-resolved release must be IN_CALIBRE: {result}")
    check(result["matched_series_name"] == TRAPPED_CALIBRE,
          f"matched_series_name must be canonical: {result}")
    check(result["canonical_series_name"] == TRAPPED_CALIBRE,
          f"canonical_series_name must be canonical: {result}")
    check(result["matched_series"]["series_name"] == TRAPPED_CALIBRE,
          f"matched_series must carry the canonical name: {result['matched_series']}")
    check(result["matched_book_id"] == BOOK_ID_327_VOL1,
          f"vol 1 ownership: {result['matched_book_id']}")

    # alt_series_names path with no alias map at all.
    plain = CalibreMatcher(series, books)
    alt = plain.check_release(
        {"title": f"{TRAPPED_ANILIST} (Novel)", "series_name": TRAPPED_ANILIST, "volume": None},
        alt_series_names=[TRAPPED_CALIBRE],
    )
    check(alt["status"] == "IN_CALIBRE", f"alt_series_names must match #327: {alt}")
    check(alt["canonical_series_name"] == TRAPPED_CALIBRE, f"canonical name: {alt}")

    # Unknown series keeps the legacy shape (additive keys are None).
    unknown = plain.check_release(
        {"title": "Completely Unknown Thing (Novel)", "series_name": "Zzz Qqq Xyzzy", "volume": None}
    )
    check(unknown["status"] == "NEW", f"unknown release must still be NEW: {unknown}")
    check(unknown["canonical_series_name"] is None, f"unknown canonical key: {unknown}")
    check(unknown["matched_series"] is None, f"unknown matched_series: {unknown}")


# ---------------------------------------------------------------------------
# 3. Snapshot + resolve_release_series_identity
# ---------------------------------------------------------------------------
def test_identity_snapshot_and_resolution():
    section("4. TaskManager: identity snapshot + release canonicalisation")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    try:
        insert_series(
            db, TRAPPED_CALIBRE, calibre_id=327,
            display_title=TRAPPED_ANILIST,
            anilist_meta=make_anilist_meta(TRAPPED_ANILIST, anilist_id=327),
        )
        db.add_series_alias_override(
            TRAPPED_ANILIST, TRAPPED_CALIBRE, calibre_series_id=327, origin="anilist"
        )
        with patched_scanner():
            snapshot = tm.series_identity_snapshot()
            check(len(snapshot["rows"]) == 1, f"snapshot rows: {snapshot['rows']}")
            check(len(snapshot["aliases"]) == 1, f"snapshot aliases: {snapshot['aliases']}")
            check(normalize_text(TRAPPED_ANILIST) in snapshot["alias_map"],
                  f"alias map must contain the AniList spelling: {snapshot['alias_map']}")
            check(len(snapshot["calibre_series"]) == 2,
                  f"calibre series snapshot: {snapshot['calibre_series']}")
            matcher_aliases = tm._series_alias_map_for_matcher(snapshot)
            check(matcher_aliases.get(normalize_text(TRAPPED_ANILIST)) == TRAPPED_CALIBRE,
                  f"matcher alias map: {matcher_aliases}")

            identity = tm.resolve_release_series_identity(
                {"series_name": TRAPPED_ANILIST}, snapshot
            )
            check(identity["matched"] is True, f"AniList title must resolve: {identity}")
            check(identity["series_name"] == TRAPPED_CALIBRE,
                  f"canonical stable name: {identity}")
            check(identity["calibre_series_id"] == 327, f"calibre id: {identity}")
            check(identity["display_title"] == TRAPPED_ANILIST, f"display title: {identity}")
            check(identity["source"] in ("alias", "exact", "anilist"), f"source: {identity}")

            unknown = tm.resolve_release_series_identity(
                {"series_name": "Zzz Qqq Xyzzy"}, snapshot
            )
            check(unknown["matched"] is False, f"unknown must not match: {unknown}")
            check(unknown["series_name"] == "Zzz Qqq Xyzzy", f"unknown name preserved: {unknown}")
            check(unknown["calibre_series_id"] is None, f"unknown calibre id: {unknown}")
            check(unknown["source"] == "none", f"unknown source: {unknown}")
    finally:
        shutdown_manager(tm)
        db.close() if hasattr(db, "close") else None


# ---------------------------------------------------------------------------
# 4. sync_calibre: volumes map, auto-link, bounded enrichment, outage-contained
# ---------------------------------------------------------------------------
def test_sync_calibre_autolink_enrichment():
    section("5. sync_calibre: omnibus volumes map + auto-link + AniList enrichment")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    metadata = MetadataFake()
    try:
        insert_series(db, TRAPPED_ANILIST, is_monitored=1)
        release_id = insert_release(
            db, "sync-release-1", f"{TRAPPED_ANILIST} (Novel)", TRAPPED_ANILIST, volume=None
        )
        metadata.reset(result=lambda name: (
            make_anilist_meta(TRAPPED_ANILIST, synonyms=[TRAPPED_CALIBRE], anilist_id=991)
            if normalize_text(name) == normalize_text(TRAPPED_ANILIST) else None
        ))
        with patched_scanner(), patched_metadata(metadata):
            result = tm.sync_calibre()

        check(result["status"] == "success", f"sync must succeed: {result}")
        for key in ("series_count", "books_count", "resolved_requests", "resolved_items"):
            check(key in result, f"legacy sync key missing: {key}")
        check(result.get("series_anilist_synced") == 1,
              f"one series enriched: {result}")
        check(result.get("series_linked") == 1, f"one series auto-linked: {result}")
        check(FakeScanner.volumes_map_calls >= 1,
              "sync_calibre must read the omnibus volumes map")

        linked_row = db.get_series_by_calibre_id(327)
        check(linked_row is not None, "Calibre series #327 must be tracked after sync")
        check(linked_row["name"] == TRAPPED_CALIBRE, f"stable key: {linked_row['name']}")
        owned = json.loads(linked_row.get("owned_volumes_json") or "[]")
        check(owned == [1.0, 2.0, 3.0], f"owned volumes must keep omnibus vols: {owned}")
        check(bool(linked_row.get("anilist_meta_json")),
              "linked row must keep AniList metadata")

        row = get_release(db, release_id)
        check(row["series_name"] == TRAPPED_CALIBRE, f"release re-keyed: {row['series_name']}")
        check(row["status"] == "IN_CALIBRE", f"release reconciled to IN_CALIBRE: {row['status']}")
        check(row["calibre_matched_book_id"] == BOOK_ID_327_VOL1,
              f"matched book: {row['calibre_matched_book_id']}")
        check(len(activity_events(db, "SERIES_AUTO_LINKED")) >= 1,
              "SERIES_AUTO_LINKED activity must be logged")
    finally:
        shutdown_manager(tm)


def test_sync_calibre_metadata_outage_contained():
    section("6. sync_calibre: metadata outage never breaks the sync")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    metadata = MetadataFake()
    try:
        insert_series(db, TRAPPED_ANILIST, is_monitored=1)
        metadata.reset(error=RuntimeError("simulated metadata outage"))
        with patched_scanner(), patched_metadata(metadata):
            result = tm.sync_calibre()
        check(result["status"] == "success", f"sync must survive a metadata outage: {result}")
        check(result.get("series_anilist_synced") == 0,
              f"no series enriched during outage: {result}")
        check(result.get("series_linked") == 0,
              f"no metadata-free auto-link for an AniList-only title: {result}")
        check(len(metadata.calls) >= 1, "enrichment must have been attempted")
        # The Calibre series rows are still synced and tracked normally.
        check(db.get_series_by_calibre_id(327) is not None,
              "Calibre series sync must continue during a metadata outage")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 5. reconcile_series_releases status precedence
# ---------------------------------------------------------------------------
def test_reconcile_status_precedence():
    section("7. reconcile_series_releases: status precedence + alias re-key")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    try:
        insert_series(db, TRAPPED_CALIBRE, calibre_id=327)
        db.add_series_alias_override(
            TRAPPED_ANILIST, TRAPPED_CALIBRE, calibre_series_id=327, origin="anilist"
        )
        id_in_calibre = insert_release(
            db, "rec-new", f"{TRAPPED_ANILIST} (Novel)", TRAPPED_ANILIST, volume=1.0, status="NEW"
        )
        id_ignored = insert_release(
            db, "rec-ignored", f"{TRAPPED_ANILIST} (Novel) v1", TRAPPED_ANILIST, volume=1.0,
            status="IGNORED",
        )
        id_owned = insert_release(
            db, "rec-owned", f"{TRAPPED_ANILIST} Vol 2", TRAPPED_ANILIST, volume=2.0,
            status="IN_CALIBRE",
        )
        id_missing = insert_release(
            db, "rec-missing", f"{TRAPPED_ANILIST} Vol 7", TRAPPED_ANILIST, volume=7.0,
            status="REQUESTED",
        )
        with patched_scanner():
            counts = tm.reconcile_series_releases(TRAPPED_CALIBRE)

        check(counts["checked"] == 4, f"all four variant releases checked: {counts}")
        check(counts["in_calibre"] == 2, f"two owned volumes: {counts}")
        check(counts["requested"] == 1, f"one missing volume stays requested: {counts}")
        check(counts["new"] == 0, f"nothing new: {counts}")
        check(counts["rekeyed"] == 3, f"veto row must not be re-keyed: {counts}")

        new_row = get_release(db, id_in_calibre)
        check(new_row["series_name"] == TRAPPED_CALIBRE, f"re-keyed: {new_row['series_name']}")
        check(new_row["status"] == "IN_CALIBRE", f"NEW -> IN_CALIBRE: {new_row['status']}")
        check(new_row["calibre_matched_book_id"] == BOOK_ID_327_VOL1,
              f"matched vol 1 book: {new_row['calibre_matched_book_id']}")

        ignored_row = get_release(db, id_ignored)
        check(ignored_row["status"] == "IGNORED", "IGNORED must never be lifted")
        check(ignored_row["series_name"] == TRAPPED_ANILIST,
              "veto row series_name must not be re-keyed")

        owned_row = get_release(db, id_owned)
        check(owned_row["status"] == "IN_CALIBRE", "IN_CALIBRE must be preserved")
        check(owned_row["calibre_matched_book_id"] == BOOK_ID_327_VOL2,
              f"volume 2 book: {owned_row['calibre_matched_book_id']}")

        missing_row = get_release(db, id_missing)
        check(missing_row["status"] == "REQUESTED", "REQUESTED must be preserved")
        check(missing_row["series_name"] == TRAPPED_CALIBRE,
              f"missing volume re-keyed: {missing_row['series_name']}")
        check(len(activity_events(db, "SERIES_REKEYED")) == 1,
              "one bounded SERIES_REKEYED summary must be logged")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 6. check_feeds: AniList fallback + mismatch pair never confused
# ---------------------------------------------------------------------------
def test_check_feeds_alt_title_fallback():
    section("8. check_feeds: bounded AniList fallback resolves the mismatch pair")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    metadata = MetadataFake()
    try:
        feed_id = db.add_feed(
            "Fake Feed", "rss", "https://example.test/feed", json.dumps({}), True, 60
        )
        check(feed_id > 0, "feed seeded")
        # A pre-existing row stored under the AniList/romaji spelling must be
        # re-keyed to the canonical Calibre name, not duplicated mid-scan.
        pre_existing_id = insert_release(
            db, "feed-guid-trapped", "Old Trapped Release", TRAPPED_ROMAJI, volume=None
        )
        items = [
            ReleaseItem(
                title=f"{TRAPPED_ROMAJI} (Novel)",
                link="https://example.test/trapped-romaji",
                series_name=TRAPPED_ROMAJI,
                volume=None,
                pub_date="2026-11-02T00:00:00+00:00",
                guid="feed-guid-trapped",
            ),
            ReleaseItem(
                title=f"{TRAPPED_MOBS_CALIBRE} (Novel)",
                link="https://example.test/trapped-mobs",
                series_name=TRAPPED_MOBS_CALIBRE,
                volume=None,
                pub_date="2026-11-01T00:00:00+00:00",
                guid="feed-guid-mobs",
            ),
        ]
        FakeProvider.reset(items=items)
        metadata.reset(result=lambda name: (
            make_anilist_meta(TRAPPED_ANILIST, synonyms=[TRAPPED_CALIBRE], anilist_id=327)
            if normalize_text(name) == normalize_text(TRAPPED_ROMAJI) else None
        ))
        with patched_scanner(), patched_metadata(metadata), patched_provider():
            result = tm.check_feeds()

        check(result["status"] == "success", f"check_feeds status: {result}")
        for key in ("total_found", "total_requested", "skipped_manga", "skipped_audiobooks",
                    "feeds_catchup", "catchup_extra_items", "max_pages_reached"):
            check(key in result, f"legacy check_feeds key missing: {key}")
        check(result["total_found"] == 2, f"two releases ingested: {result}")
        check(len(metadata.calls) == 1,
              f"bounded AniList lookup: exactly one fetch (got {metadata.calls})")

        rows = db.get_releases(limit=50)
        by_guid = {row["guid"]: row for row in rows}
        trapped = by_guid.get("feed-guid-trapped")
        mobs = by_guid.get("feed-guid-mobs")
        check(trapped is not None, "AniList-fallback release persisted")
        check(mobs is not None, "mobs release persisted")
        check(trapped["id"] == pre_existing_id,
              f"pre-existing row must be updated, not duplicated: {trapped['id']} vs {pre_existing_id}")
        check(trapped["series_name"] == TRAPPED_CALIBRE,
              f"fallback must adopt the #327 stable key: {trapped['series_name']}")
        check(trapped["status"] == "IN_CALIBRE",
              f"fallback release is in Calibre: {trapped['status']}")
        check(trapped["calibre_matched_book_id"] in (None, BOOK_ID_327_VOL1),
              f"fallback matched #327 vol 1 (existing rows are reconciled later): "
              f"{trapped['calibre_matched_book_id']}")
        check(mobs["series_name"] == TRAPPED_MOBS_CALIBRE,
              f"mobs release keeps its own series: {mobs['series_name']}")
        check(mobs["status"] == "IN_CALIBRE", f"mobs release in Calibre: {mobs['status']}")
        check(mobs["calibre_matched_book_id"] == BOOK_ID_174_VOL1,
              f"mobs release matched #174: {mobs['calibre_matched_book_id']}")
        check(trapped["calibre_matched_book_id"] != mobs["calibre_matched_book_id"],
              "the two Trapped series must resolve to different Calibre books")
    finally:
        shutdown_manager(tm)


def test_check_feeds_budget_and_setting():
    section("9. check_feeds: AniList fallback budget and disable switch")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    metadata = MetadataFake()
    try:
        db.set_setting("series_anilist_match_budget", "0")
        db.add_feed("Fake Feed", "rss", "https://example.test/feed", json.dumps({}), True, 60)
        items = [
            ReleaseItem(
                title=f"Unknown Romaji {index} (Novel)",
                link=f"https://example.test/unknown-{index}",
                series_name=f"Otome Game no Hametsu Flag {index}",
                volume=None,
                pub_date="2026-11-02T00:00:00+00:00",
                guid=f"budget-guid-{index}",
            )
            for index in range(3)
        ]
        FakeProvider.reset(items=items)
        metadata.reset(result=make_anilist_meta(TRAPPED_ANILIST, synonyms=[TRAPPED_CALIBRE]))
        with patched_scanner(), patched_metadata(metadata), patched_provider():
            result = tm.check_feeds()
        check(result["status"] == "success", f"scan status: {result}")
        check(len(metadata.calls) == 0,
              f"budget=0 must disable AniList lookups (calls={metadata.calls})")
        check(result["total_found"] == 3, f"all releases still ingested: {result}")
        db.set_setting("series_anilist_match_budget", "10")
        db.set_setting("series_anilist_match_enabled", "false")
        FakeProvider.reset(items=items)
        metadata.reset(result=make_anilist_meta(TRAPPED_ANILIST, synonyms=[TRAPPED_CALIBRE]))
        with patched_scanner(), patched_metadata(metadata), patched_provider():
            result = tm.check_feeds()
        check(len(metadata.calls) == 0,
              f"match_enabled=false must disable AniList lookups (calls={metadata.calls})")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 7. lookup_series_alternate_titles cache + normalized cache keys
# ---------------------------------------------------------------------------
def test_lookup_series_alternate_titles():
    section("10. lookup_series_alternate_titles: TTL cache + normalized keys")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    metadata = MetadataFake()
    try:
        db.set_setting("calibre_path", tempfile.gettempdir())
        metadata.reset(result=make_anilist_meta(
            TRAPPED_ANILIST, synonyms=[TRAPPED_CALIBRE, TRAPPED_ROMAJI], anilist_id=327
        ))
        with patched_scanner(), patched_metadata(metadata):
            first = tm.lookup_series_alternate_titles("Otome Game no Hametsu Flag")
            check(TRAPPED_ANILIST in first, f"english title must be a candidate: {first}")
            check(TRAPPED_CALIBRE in first, f"synonym must be a candidate: {first}")
            check(len(metadata.calls) == 1, f"one fetch: {metadata.calls}")
            second = tm.lookup_series_alternate_titles("Otome Game no Hametsu Flag")
            check(second == first, "second lookup must hit the in-memory cache")
            check(len(metadata.calls) == 1,
                  f"TTL cache must prevent a second fetch: {metadata.calls}")
            forced = tm.lookup_series_alternate_titles("Otome Game no Hametsu Flag", force=True)
            check(len(metadata.calls) == 2, f"force=True re-asks the provider: {metadata.calls}")
            check(forced == first, "forced result stable")

        # A metadata-cache row stored under parser.normalize_text() must be found
        # even for a punctuation-heavy title (MangaTrax matcher cache-key bug).
        db.cache_novel_metadata(
            normalize_text("Punctuated: Cache Series"),
            make_anilist_meta("Punctuated Cache Alternate", synonyms=["Punctuated Cache Alias"]),
        )
        fresh = new_manager(db)
        try:
            metadata.reset(result=None)
            with patched_scanner(), patched_metadata(metadata):
                cached = fresh.lookup_series_alternate_titles("Punctuated: Cache Series")
            check("Punctuated Cache Alternate" in cached,
                  f"normalized cache key must be read: {cached}")
            check(len(metadata.calls) == 0,
                  f"cache hit must not fetch: {metadata.calls}")
        finally:
            shutdown_manager(fresh)

        # Unknown series with no metadata: empty list, never raises.
        metadata.reset(result=None)
        with patched_scanner(), patched_metadata(metadata):
            unknown = tm.lookup_series_alternate_titles("Zzz Qqq Xyzzy")
        check(unknown == [], f"unknown series must return []: {unknown}")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 8. Manual match wrapper
# ---------------------------------------------------------------------------
def test_manual_match_wrapper():
    section("11. match_series_to_calibre: discovery link + re-key + activity")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    try:
        release_id = insert_release(
            db, "manual-release-1", f"{TRAPPED_ANILIST} (Novel)", TRAPPED_ANILIST, volume=1.0
        )
        calibre_series = {"series_id": 327, "series_name": TRAPPED_CALIBRE,
                          "book_count": 3, "latest_index": 3.0, "authors": "Author One"}
        with patched_scanner():
            summary = tm.match_series_to_calibre(
                TRAPPED_ANILIST, calibre_series, actor="user", release_id=release_id
            )
        check(summary["status"] == "success", f"manual match status: {summary}")
        check(summary["source"] == TRAPPED_ANILIST, f"source: {summary}")
        check(summary["target"] == TRAPPED_CALIBRE, f"target: {summary}")
        check(summary["calibre_series_id"] == 327, f"calibre id: {summary}")
        check(summary["discovered"] is True, f"discovery path expected: {summary}")
        check("reconciled" in summary, f"reconcile summary: {summary}")
        row = get_release(db, release_id)
        check(row["series_name"] == TRAPPED_CALIBRE, f"release re-keyed: {row['series_name']}")
        check(row["status"] == "IN_CALIBRE", f"release reconciled: {row['status']}")
        overrides = db.get_series_alias_overrides()
        check(any(normalize_text(o["source_name"]) == normalize_text(TRAPPED_ANILIST)
                  for o in overrides), f"alias override persisted: {overrides}")
        events = activity_events(db, "SERIES_MANUALLY_MATCHED")
        check(len(events) == 1, f"SERIES_MANUALLY_MATCHED logged: {events}")
        check("user" in (events[0]["details"] or ""), f"actor logged: {events[0]}")

        for bad_args in (("", calibre_series), (TRAPPED_CALIBRE, {"series_id": 1})):
            raised = False
            try:
                tm.match_series_to_calibre(bad_args[0], bad_args[1])
            except ValueError:
                raised = True
            check(raised, f"invalid manual match must raise ValueError: {bad_args}")

        # Existing tracker-row path: link/merge (not the discovery path).
        insert_series(db, "Another Discovery Name", is_monitored=1)
        other_release = insert_release(
            db, "manual-release-2", "Another Release (Novel)", "Another Discovery Name",
            volume=1.0,
        )
        other_calibre = {"series_id": 174, "series_name": TRAPPED_MOBS_CALIBRE,
                         "book_count": 1, "latest_index": 1.0}
        with patched_scanner():
            merged_summary = tm.match_series_to_calibre(
                "Another Discovery Name", other_calibre, actor="user"
            )
        check(merged_summary["discovered"] is False,
              f"existing tracker must use the link path: {merged_summary}")
        check(merged_summary["target"] == TRAPPED_MOBS_CALIBRE,
              f"merge target: {merged_summary}")
        check(db.get_series_by_name("Another Discovery Name") is None,
              "merged duplicate tracker row must be removed")
        other_row = get_release(db, other_release)
        check(other_row["series_name"] == TRAPPED_MOBS_CALIBRE,
              f"merge re-keys releases: {other_row['series_name']}")
    finally:
        shutdown_manager(tm)


def test_enrichment_bounded_batch():
    section("11b. enrich_tracked_series_anilist: bounded batches")
    db = make_db()
    FakeScanner.library()
    tm = new_manager(db)
    metadata = MetadataFake()
    try:
        for index in range(4):
            insert_series(db, f"Enrich Series {index}", is_monitored=1)
        metadata.reset(result=lambda name: make_anilist_meta(f"{name} AniList"))
        with patched_scanner(), patched_metadata(metadata):
            result = tm.enrich_tracked_series_anilist(limit=2)
        check(result["synced"] == 2, f"limit must bound the batch: {result}")
        check(len(metadata.calls) == 2, f"two provider calls only: {metadata.calls}")
        pending = [
            row for row in db.get_series_identity_rows()
            if not (row.get("anilist_meta_json") or "").strip()
        ]
        check(len(pending) == 2, f"remaining rows stay pending for later batches: {pending}")

        metadata.reset(result=lambda name: make_anilist_meta(f"{name} AniList"))
        with patched_scanner(), patched_metadata(metadata):
            disabled = tm.enrich_tracked_series_anilist(limit=0)
        check(disabled["synced"] == 0 and len(metadata.calls) == 0,
              f"limit=0 must not call the provider: {disabled}")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 9. refresh_series_metadata
# ---------------------------------------------------------------------------
def test_refresh_series_metadata():
    section("12. refresh_series_metadata: AniList refresh + link + reconcile")
    db = make_db()
    FakeScanner.reset(
        series=[{"series_id": 55, "series_name": "Series Y", "book_count": 1,
                 "latest_index": 1.0, "authors": "Author Y"}],
        books=[{"book_id": 55001, "title": "Series Y Vol 1", "series_id": 55,
                "series_name": "Series Y", "series_index": 1.0}],
        volumes_map={"55": {1.0}},
    )
    tm = new_manager(db)
    metadata = MetadataFake()
    try:
        series_id = insert_series(db, "Series Y", is_monitored=1)
        insert_release(db, "refresh-release-1", "Series Y (Novel)", "Series Y", volume=1.0)
        metadata.reset(result=make_anilist_meta("Series Y AniList", synonyms=["Series Y Alt"],
                                                anilist_id=777))
        with patched_scanner(), patched_metadata(metadata):
            result = tm.refresh_series_metadata(series_id)
        check(result["status"] == "success", f"refresh status: {result}")
        check(result["display_title"] == "Series Y AniList", f"display title: {result}")
        check(result["anilist_id"] == 777, f"anilist id: {result}")
        check(result["linked"] is True, f"refresh must auto-link: {result}")
        check(isinstance(result["reconciled"], dict) and result["reconciled"].get("in_calibre") == 1,
              f"reconciled counts: {result['reconciled']}")
        row = db.get_series_by_calibre_id(55)
        check(row is not None, "series linked to Calibre #55")
        check(row["display_title"] == "Series Y AniList", f"display persisted: {row}")
        check(len(activity_events(db, "SERIES_ANILIST_REFRESHED")) == 1,
              "SERIES_ANILIST_REFRESHED logged")

        missing = tm.refresh_series_metadata(999999)
        check(missing["status"] == "error", f"unknown series -> error: {missing}")
    finally:
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# 10. Backfill enrichment must never rewrite the stable series key (M1 bug)
# ---------------------------------------------------------------------------
def test_backfill_enrichment_keeps_stable_key():
    section("13. run_polite_backfill: AniList title is display metadata, not the key")
    db = make_db()
    tm = new_manager(db)

    class FakeJNovels:
        @staticmethod
        def extract_synopsis_and_original_link(url, series_name=None):
            title = str(series_name or "Unknown")
            return {
                "series_link": None,
                "synopsis": ("A sufficiently long synopsis for " + title + ". ") * 3,
                "cover_url": None,
                "english_title": title + " AniList Display",
                "metadata_source": "AniList",
                "hub_volumes": [],
            }

        def __init__(self, *args, **kwargs):
            pass

    fake_module = types.ModuleType("app.providers.jnovels")
    fake_module.JNovelsProvider = FakeJNovels
    previous_jnovels = sys.modules.get("app.providers.jnovels")
    try:
        sys.modules["app.providers.jnovels"] = fake_module
        tm.ingest_master_index = lambda: {"status": "success"}
        insert_release(
            db, "backfill-unknown", "Unknown Parsed Series (Novel)", "Unknown Parsed Series",
            volume=None,
        )
        # A discovered release whose stored spelling normalizes to a tracked row
        # (differs by punctuation, so it is still a discovery row).
        tracked_name = TRAPPED_ANILIST
        discovered_spelling = TRAPPED_ANILIST.rstrip("!")
        insert_series(db, tracked_name, is_monitored=0)
        insert_release(
            db, "backfill-tracked", f"{discovered_spelling} (Novel)", discovered_spelling,
            volume=None,
        )
        asyncio.run(tm.run_polite_backfill(max_pages=-1, enrich_synopses=True, delay_seconds=0))

        all_rows = db.get_releases(limit=50, include_ignored=True)
        by_guid = {row["guid"]: row for row in all_rows}
        unknown = by_guid.get("backfill-unknown")
        tracked = by_guid.get("backfill-tracked")
        check(unknown is not None, "unknown discovery release must remain")
        check(tracked is not None, "tracked-resolution discovery release must remain")
        check(unknown["series_name"] == "Unknown Parsed Series",
              f"AniList title must not rewrite the stable key: {unknown['series_name']}")
        check(tracked["series_name"] == discovered_spelling,
              f"AniList title must not rewrite a resolvable stable key: {tracked['series_name']}")
        check("AniList Display" in (unknown["title"] or ""),
              f"display title may update the release title: {unknown['title']}")
        check("synopsis" in (unknown["description"] or "").lower(),
              "synopsis enrichment still stored")

        tracked_row = db.get_series_by_name(tracked_name)
        check(tracked_row is not None, "tracked row present")
        check(tracked_row["display_title"] == discovered_spelling + " AniList Display",
              f"tracked display_title gets the provider title: {tracked_row['display_title']}")

        cached = db.get_cached_novel_metadata(normalize_text("Unknown Parsed Series"))
        check(cached is not None, "unknown series provider title must be cached for later matching")
        check("AniList Display" in (cached.get("title") or ""),
              f"cached title: {cached}")
    finally:
        if previous_jnovels is not None:
            sys.modules["app.providers.jnovels"] = previous_jnovels
        else:
            sys.modules.pop("app.providers.jnovels", None)
        shutdown_manager(tm)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
TESTS = [
    ("parser user case", test_parser_user_case_and_ambiguity),
    ("parser head guard", test_parser_head_token_guard),
    ("matcher alt names", test_matcher_alt_series_names),
    ("identity resolve", test_identity_snapshot_and_resolution),
    ("sync autolink/enrich", test_sync_calibre_autolink_enrichment),
    ("sync outage", test_sync_calibre_metadata_outage_contained),
    ("reconcile statuses", test_reconcile_status_precedence),
    ("check_feeds fallback", test_check_feeds_alt_title_fallback),
    ("check_feeds budget", test_check_feeds_budget_and_setting),
    ("lookup alternate titles", test_lookup_series_alternate_titles),
    ("manual match", test_manual_match_wrapper),
    ("enrichment bounded", test_enrichment_bounded_batch),
    ("refresh metadata", test_refresh_series_metadata),
    ("backfill stable key", test_backfill_enrichment_keeps_stable_key),
]


def main():
    print("=" * 78)
    print("  NovelTrax series identity matching + scheduler tests")
    print(f"  provider registry stub: {REGISTRY_STUBBED}")
    print(f"  temp dir: {tempfile.gettempdir()}")
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
                print(traceback.format_exc())
    finally:
        cleanup_temp_databases()

    print("\n" + "=" * 78)
    if failures:
        print(f"  {len(TESTS) - len(failures)}/{len(TESTS)} test groups passed, {len(failures)} failed")
        print("=" * 78)
        raise SystemExit(1)
    print(f"  all {len(TESTS)} test groups passed")
    print("=" * 78)
    print("ALL SERIES-MATCHING TESTS PASSED")


if __name__ == "__main__":
    main()
