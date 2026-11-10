#!/usr/bin/env python3
"""AniList publication metadata tests (v3 contract §2-§3, owner A).

Covers:

* ``NovelMetadataProvider.query_anilist`` GraphQL fields (format/status/volumes/
  chapters, ``format_in: [NOVEL]`` kept) and the mapped result keys
  (format, format_label, is_light_novel, status, status_label, volumes, chapters,
  total_volumes) — including a monkeypatched ``urllib.request.urlopen`` response
  because this test interpreter has no httpx;
* ``publication_from_meta`` matrix: dict / JSON string / nested GraphQL envelope /
  empty / garbage / label-only round-trips, count validation and status labels;
* publication keys surviving ``fetch_series_metadata`` / ``fetch_novel_metadata``
  on the live and SQLite-cached paths;
* ``novel_metadata_cache.publication_json`` guarded migration, write and parse-back
  (including a legacy cache table without the column);
* ``store_series_anilist_metadata`` persisting the publication keys in
  ``anilist_meta_json``;
* ``store_series_publication_metadata`` merging only publication keys, preserving
  name/display/calibre/aliases, and no-op for unknown ids / invalid meta /
  ``identity_locked`` rows.

Hermetic: no network (urllib monkeypatched), temp DBs under
``tempfile.gettempdir()`` (never ``mkdtemp``), explicit ``check()`` raising
``AssertionError`` (no bare asserts so Calibre's -O2 interpreter cannot strip them).
"""

import gc
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback
import urllib.request
from typing import Any, Dict, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
for _path in (BASE_DIR, BACKEND_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.database import Database
from app.providers import metadata as metadata_module
from app.providers.metadata import (
    PUBLICATION_KEYS,
    NovelMetadataProvider,
    publication_defaults,
)

PASSED = 0
FAILED = 0
FAILURES = []
_TEMP_COUNTER = 0

TRAPPED_TITLE = "Trapped in a Dating Sim: Otome Games Are Tough for Us, Too!"
EXECUTIONER_TITLE = "The Executioner and Her Way of Life"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def check(condition, message):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {message}")
        return
    FAILED += 1
    FAILURES.append(message)
    print(f"  [FAIL] {message}")
    raise AssertionError(message)


def check_equal(actual, expected, message):
    check(actual == expected, f"{message} (expected {expected!r}, got {actual!r})")


def check_raises(exc_type, func, message):
    try:
        func()
    except exc_type:
        check(True, message)
        return
    except Exception as exc:  # noqa: BLE001 - wrong type is a failure
        check(False, f"{message}: expected {exc_type.__name__}, "
                     f"got {type(exc).__name__}: {exc}")
        return
    check(False, f"{message}: expected {exc_type.__name__}, nothing was raised")


def section(title):
    print(f"\n--- {title} ---")


def _make_temp_dir(label):
    global _TEMP_COUNTER
    _TEMP_COUNTER += 1
    path = os.path.join(
        tempfile.gettempdir(),
        "noveltrax-publication-%d-%d-%s" % (os.getpid(), _TEMP_COUNTER, label),
    )
    os.makedirs(path, exist_ok=True)
    return path


def _cleanup_temp_dir(path):
    try:
        gc.collect()
    except Exception:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _assert_publication_shape(publication, message):
    check(isinstance(publication, dict), f"{message}: returns a dict")
    for key in PUBLICATION_KEYS:
        check(key in publication, f"{message}: contains {key}")


def _insert_tracker(db, name, calibre_id=None, is_monitored=1, display_title=None,
                    identity_locked=0, anilist_meta_json=None):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO series_tracking
                (name, calibre_series_id, is_monitored, display_title,
                 identity_locked, anilist_meta_json)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (name, calibre_id, is_monitored, display_title or "",
             identity_locked, anilist_meta_json or ""),
        )
        conn.commit()
    return db.get_series_by_name(name)


def _stored_meta(db, series_id):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT anilist_meta_json FROM series_tracking WHERE id = ?;", (series_id,))
        row = cur.fetchone()
    if row is None or not row["anilist_meta_json"]:
        return {}
    try:
        parsed = json.loads(row["anilist_meta_json"])
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _cache_columns(db):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(novel_metadata_cache);")
        return {row["name"] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Fake AniList HTTP layer (no httpx in this interpreter)
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.headers = {}

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _AniListStub:
    """Monkeypatches ``urllib.request.urlopen`` for query_anilist tests."""

    def __init__(self, media):
        self.media = media
        self.requests = []
        self._original = None

    def __enter__(self):
        self._original = urllib.request.urlopen
        stub = self

        def fake_urlopen(req, timeout=10):
            try:
                body = json.loads(req.data.decode("utf-8"))
            except Exception:
                body = {}
            stub.requests.append(body)
            return _FakeResponse({"data": {"Media": stub.media}})

        urllib.request.urlopen = fake_urlopen
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._original is not None:
            urllib.request.urlopen = self._original
        return False

    @property
    def query(self):
        if not self.requests:
            return ""
        payload = self.requests[0]
        return str(payload.get("query") or "")


def _anilist_media(**overrides):
    media = {
        "id": 98765,
        "title": {
            "romaji": "Otome Game Sekai wa Mob ni Kibishii Sekai desu",
            "english": TRAPPED_TITLE,
            "native": None,
        },
        "synonyms": ["Trapped in a Dating Sim"],
        "format": "NOVEL",
        "status": "FINISHED",
        "volumes": 11,
        "chapters": 120,
        "description": "A long enough AniList description for the parser.",
        "coverImage": {"large": "https://example.invalid/cover.jpg",
                       "extraLarge": None},
    }
    media.update(overrides)
    return media


# ---------------------------------------------------------------------------
# 1. publication_from_meta matrix
# ---------------------------------------------------------------------------
def test_publication_from_meta_matrix():
    section("1. publication_from_meta: light novel / status / volume coercion")
    full = NovelMetadataProvider.publication_from_meta({
        "format": "NOVEL",
        "status": "FINISHED",
        "volumes": 11,
        "chapters": 120,
    })
    _assert_publication_shape(full, "full publication dict")
    check_equal(full["format"], "NOVEL", "raw format is upper-case")
    check_equal(full["format_label"], "Light Novel", "NOVEL maps to 'Light Novel'")
    check_equal(full["is_light_novel"], True, "NOVEL is a light novel")
    check_equal(full["status"], "FINISHED", "raw status is upper-case")
    check_equal(full["status_label"], "Finished", "FINISHED maps to 'Finished'")
    check_equal(full["volumes"], 11, "volumes is an int")
    check_equal(full["chapters"], 120, "chapters is an int")
    check_equal(full["total_volumes"], 11, "total_volumes aliases volumes")

    for raw_format in ("NOVEL", "LIGHT_NOVEL", "LIGHT NOVEL", "light novel",
                       "Novel", "light-novel"):
        publication = NovelMetadataProvider.publication_from_meta({"format": raw_format})
        check_equal(publication["is_light_novel"], True,
                    f"format {raw_format!r} counts as a light novel")
        check_equal(publication["format_label"], "Light Novel",
                    f"format {raw_format!r} label is Light Novel")

    manga = NovelMetadataProvider.publication_from_meta({"format": "MANGA"})
    check_equal(manga["is_light_novel"], False, "MANGA is not a light novel")
    check_equal(manga["format_label"], "Manga", "MANGA label is prettified")
    one_shot = NovelMetadataProvider.publication_from_meta({"format": "ONE_SHOT"})
    check_equal(one_shot["is_light_novel"], False, "ONE_SHOT is not a light novel")
    check_equal(one_shot["format_label"], "One Shot", "enums are prettified")

    status_labels = {
        "FINISHED": "Finished",
        "RELEASING": "Releasing",
        "NOT_YET_RELEASED": "Not Yet Released",
        "HIATUS": "Hiatus",
        "CANCELLED": "Cancelled",
    }
    for raw_status, expected_label in status_labels.items():
        publication = NovelMetadataProvider.publication_from_meta({"status": raw_status})
        check_equal(publication["status_label"], expected_label,
                    f"status {raw_status} maps to {expected_label!r}")
    unknown_status = NovelMetadataProvider.publication_from_meta({"status": "ONGOING"})
    check_equal(unknown_status["status"], "ONGOING", "unknown raw status is kept")
    check_equal(unknown_status["status_label"], "Ongoing",
                "unknown raw status label is prettified")

    coerced = NovelMetadataProvider.publication_from_meta(
        {"volumes": "11", "chapters": "120.0"}
    )
    check_equal(coerced["volumes"], 11, "string volumes are coerced to int")
    check_equal(coerced["chapters"], 120, "string chapters are coerced to int")
    check_equal(
        NovelMetadataProvider.publication_from_meta({"volumes": 11.0})["volumes"],
        11, "integral float volumes are coerced to int")
    for bad_value in (-3, 2.5, "x", True, [11], {"n": 11}):
        publication = NovelMetadataProvider.publication_from_meta({"volumes": bad_value})
        check_equal(publication["volumes"], None,
                    f"invalid volumes {bad_value!r} -> None")
        check_equal(publication["total_volumes"], None,
                    f"invalid total_volumes for {bad_value!r} -> None")
    alias = NovelMetadataProvider.publication_from_meta({"total_volumes": 9})
    check_equal(alias["volumes"], 9, "total_volumes input populates volumes")

    # JSON string (stored anilist_meta_json), nested GraphQL envelope and
    # label-only round-trips.
    from_json = NovelMetadataProvider.publication_from_meta(json.dumps({
        "format": "NOVEL", "status": "RELEASING", "volumes": 7,
    }))
    check_equal(from_json["volumes"], 7, "JSON string meta is parsed")
    check_equal(from_json["status_label"], "Releasing", "JSON string status label")
    nested = NovelMetadataProvider.publication_from_meta({
        "data": {"Media": {"format": "NOVEL", "status": "FINISHED", "volumes": 11}}
    })
    check_equal(nested["volumes"], 11, "nested GraphQL envelope is supported")
    check_equal(nested["status_label"], "Finished", "nested envelope status label")
    nested_media = NovelMetadataProvider.publication_from_meta({
        "media": {"format": "LIGHT_NOVEL", "volumes": 3}
    })
    check_equal(nested_media["is_light_novel"], True,
                "nested 'media' envelope is supported")
    label_only = NovelMetadataProvider.publication_from_meta({
        "format_label": "Light Novel",
        "status_label": "Finished",
        "total_volumes": 11,
    })
    check_equal(label_only["is_light_novel"], True,
                "label-only meta derives the light-novel flag")
    check_equal(label_only["status"], "FINISHED",
                "label-only meta derives the raw status")
    check_equal(label_only["volumes"], 11, "label-only meta keeps total_volumes")

    # Empty / garbage inputs never raise and always return the full shape.
    for empty_meta in (None, "", "   ", "garbage", "[]", [], {}, 42, object()):
        publication = NovelMetadataProvider.publication_from_meta(empty_meta)
        _assert_publication_shape(publication, f"meta {type(empty_meta).__name__}")
        check_equal(publication["is_light_novel"], False,
                    f"meta {type(empty_meta).__name__} has no light-novel flag")
        check_equal(publication["volumes"], None,
                    f"meta {type(empty_meta).__name__} has no volumes")
    defaults = publication_defaults()
    _assert_publication_shape(defaults, "publication_defaults")
    check_equal(len(PUBLICATION_KEYS), 8, "PUBLICATION_KEYS has the eight keys")
    round_trip = NovelMetadataProvider.publication_from_meta(full)
    check_equal(round_trip, full, "publication_from_meta round-trips its own output")


# ---------------------------------------------------------------------------
# 2. query_anilist: GraphQL fields + parsed result
# ---------------------------------------------------------------------------
def test_query_anilist_publication_fields():
    section("2. query_anilist: GraphQL format/status/volumes/chapters + mapping")
    with _AniListStub(_anilist_media()) as stub:
        result = NovelMetadataProvider.query_anilist(TRAPPED_TITLE)
    check(result is not None, "query_anilist returns a result for a NOVEL entry")
    query = stub.query
    for field in ("format", "status", "volumes", "chapters"):
        check(field in query, f"GraphQL query requests the {field} field")
    check("format_in: [NOVEL]" in query,
          "GraphQL query keeps format_in: [NOVEL] (light-novel entries only)")
    _assert_publication_shape(result, "query_anilist result")
    check_equal(result["format"], "NOVEL", "query_anilist format mapped")
    check_equal(result["format_label"], "Light Novel", "query_anilist format_label")
    check_equal(result["is_light_novel"], True, "query_anilist is_light_novel")
    check_equal(result["status"], "FINISHED", "query_anilist status mapped")
    check_equal(result["status_label"], "Finished", "query_anilist status_label")
    check_equal(result["volumes"], 11, "query_anilist volumes mapped")
    check_equal(result["total_volumes"], 11, "query_anilist total_volumes alias")
    check_equal(result["chapters"], 120, "query_anilist chapters mapped")
    for legacy_key in ("title", "english_title", "synonyms", "anilist_id",
                       "synopsis", "cover_url", "source"):
        check(legacy_key in result, f"query_anilist keeps legacy key {legacy_key}")

    with _AniListStub(_anilist_media(format="MANGA", status="RELEASING")):
        manga_result = NovelMetadataProvider.query_anilist("Some Manga")
    check(manga_result is not None, "query_anilist still returns a MANGA payload")
    check_equal(manga_result["is_light_novel"], False,
                "MANGA format is never a light novel")
    check_equal(manga_result["format_label"], "Manga", "MANGA format label")

    with _AniListStub(_anilist_media(format=None, status=None, volumes=None,
                                     chapters=None)):
        missing_result = NovelMetadataProvider.query_anilist("Missing Fields")
    check(missing_result is not None, "query_anilist tolerates missing fields")
    check_equal(missing_result["format"], "", "missing format -> ''")
    check_equal(missing_result["status"], "", "missing status -> ''")
    check_equal(missing_result["volumes"], None, "missing volumes -> None")
    check_equal(missing_result["chapters"], None, "missing chapters -> None")
    check_equal(missing_result["is_light_novel"], False,
                "missing format is not a light novel")


# ---------------------------------------------------------------------------
# 3. fetch_* live and cached publication passthrough
# ---------------------------------------------------------------------------
def test_fetch_publication_paths():
    section("3. fetch_series_metadata / fetch_novel_metadata publication keys")
    saved = {
        "query_anilist": NovelMetadataProvider.__dict__.get("query_anilist"),
        "query_kitsu": NovelMetadataProvider.__dict__.get("query_kitsu"),
        "default_db": NovelMetadataProvider._default_db,
    }
    cache_dir = _make_temp_dir("fetch-cache")
    try:
        anilist_live = {
            "title": EXECUTIONER_TITLE,
            "english_title": EXECUTIONER_TITLE,
            "canonical_title": EXECUTIONER_TITLE,
            "romaji_title": "Shokei Shoujo no Virgin Road",
            "native_title": None,
            "synonyms": [],
            "anilist_id": 12345,
            "synopsis": "A long enough synopsis for the legacy fetch path.",
            "cover_url": "",
            "source": "AniList",
            "format": "NOVEL",
            "status": "FINISHED",
            "volumes": 11,
            "chapters": 55,
        }
        NovelMetadataProvider.query_anilist = classmethod(
            lambda cls, title: dict(anilist_live)
        )
        NovelMetadataProvider.query_kitsu = classmethod(lambda cls, title: None)
        NovelMetadataProvider._default_db = None

        live = NovelMetadataProvider.fetch_series_metadata(EXECUTIONER_TITLE)
        check(live is not None, "live fetch_series_metadata returns a result")
        _assert_publication_shape(live, "live fetch_series_metadata")
        check_equal(live["format_label"], "Light Novel",
                    "live fetch_series_metadata carries format_label")
        check_equal(live["status_label"], "Finished",
                    "live fetch_series_metadata carries status_label")
        check_equal(live["volumes"], 11, "live fetch_series_metadata carries volumes")

        novel_live = NovelMetadataProvider.fetch_novel_metadata(EXECUTIONER_TITLE)
        check(novel_live is not None, "live fetch_novel_metadata returns a result")
        _assert_publication_shape(novel_live, "live fetch_novel_metadata")
        check_equal(novel_live["total_volumes"], 11,
                    "live fetch_novel_metadata carries total_volumes")

        # Kitsu fallback: no publication data, but the keys are present.
        kitsu_only = {
            "title": EXECUTIONER_TITLE,
            "english_title": EXECUTIONER_TITLE,
            "canonical_title": EXECUTIONER_TITLE,
            "romaji_title": None,
            "native_title": None,
            "synonyms": [],
            "alternate_titles": [],
            "anilist_id": None,
            "synopsis": "A long enough Kitsu synopsis for the fetch path.",
            "cover_url": "",
            "source": "Kitsu",
        }
        NovelMetadataProvider.query_anilist = classmethod(lambda cls, title: None)
        NovelMetadataProvider.query_kitsu = classmethod(
            lambda cls, title: dict(kitsu_only)
        )
        kitsu_result = NovelMetadataProvider.fetch_series_metadata(EXECUTIONER_TITLE)
        check(kitsu_result is not None, "Kitsu fallback fetch_series_metadata works")
        _assert_publication_shape(kitsu_result, "Kitsu fetch_series_metadata")
        check_equal(kitsu_result["is_light_novel"], False,
                    "Kitsu result has no light-novel flag")
        check_equal(kitsu_result["volumes"], None,
                    "Kitsu result has no publication volumes")

        # Cached path: the DB cache round-trips the publication dict.
        cache_db = Database(os.path.join(cache_dir, "cache.db"))
        cache_key = NovelMetadataProvider.clean_search_title(EXECUTIONER_TITLE).lower()
        cache_db.cache_novel_metadata(cache_key, {
            "title": EXECUTIONER_TITLE,
            "synopsis": "cached synopsis",
            "cover_url": "",
            "source": "AniList",
            "format": "NOVEL",
            "status": "FINISHED",
            "volumes": 11,
            "chapters": 55,
        })
        NovelMetadataProvider.set_db(cache_db)
        cached_series = NovelMetadataProvider.fetch_series_metadata(EXECUTIONER_TITLE)
        check(cached_series is not None, "cached fetch_series_metadata returns a result")
        check_equal(cached_series.get("cached"), True,
                    "cached fetch_series_metadata is marked cached")
        _assert_publication_shape(cached_series, "cached fetch_series_metadata")
        check_equal(cached_series["format_label"], "Light Novel",
                    "cached fetch_series_metadata carries format_label")
        check_equal(cached_series["status_label"], "Finished",
                    "cached fetch_series_metadata carries status_label")
        check_equal(cached_series["volumes"], 11,
                    "cached fetch_series_metadata carries volumes")

        cached_novel = NovelMetadataProvider.fetch_novel_metadata(EXECUTIONER_TITLE)
        check(cached_novel is not None, "cached fetch_novel_metadata returns a result")
        _assert_publication_shape(cached_novel, "cached fetch_novel_metadata")
        check_equal(cached_novel["total_volumes"], 11,
                    "cached fetch_novel_metadata carries total_volumes")
    finally:
        NovelMetadataProvider.query_anilist = saved["query_anilist"]
        NovelMetadataProvider.query_kitsu = saved["query_kitsu"]
        NovelMetadataProvider._default_db = saved["default_db"]
        _cleanup_temp_dir(cache_dir)


# ---------------------------------------------------------------------------
# 4. novel_metadata_cache publication_json persistence
# ---------------------------------------------------------------------------
def test_metadata_cache_publication_json():
    section("4. novel_metadata_cache: guarded publication_json column round-trip")
    tmp_dir = _make_temp_dir("cache")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        db.ensure_metadata_cache_table()
        db.ensure_metadata_cache_table()  # idempotent
        check("publication_json" in _cache_columns(db),
              "novel_metadata_cache has the publication_json column")

        db.cache_novel_metadata("executioner", {
            "title": EXECUTIONER_TITLE,
            "synopsis": "synopsis",
            "cover_url": "",
            "source": "AniList",
            "format": "NOVEL",
            "status": "FINISHED",
            "volumes": 11,
            "chapters": 55,
        })
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT publication_json FROM novel_metadata_cache WHERE query_key = ?;",
                ("executioner",),
            )
            raw_publication = cur.fetchone()["publication_json"]
        stored_publication = json.loads(raw_publication)
        check_equal(stored_publication.get("format"), "NOVEL",
                    "cache stores the raw publication format")
        check_equal(stored_publication.get("status"), "FINISHED",
                    "cache stores the raw publication status")
        check_equal(stored_publication.get("volumes"), 11,
                    "cache stores the publication volume count")

        cached = db.get_cached_novel_metadata("Executioner")
        check(cached is not None, "get_cached_novel_metadata finds the row")
        _assert_publication_shape(cached, "cached row")
        check_equal(cached["format_label"], "Light Novel",
                    "cached row exposes format_label")
        check_equal(cached["status_label"], "Finished",
                    "cached row exposes status_label")
        check_equal(cached["volumes"], 11, "cached row exposes volumes")
        check_equal(cached["total_volumes"], 11, "cached row exposes total_volumes")

        # Plain metadata (no publication) stores an empty publication JSON.
        db.cache_novel_metadata("plain", {"title": "Plain", "source": "Kitsu"})
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT publication_json FROM novel_metadata_cache WHERE query_key = ?;",
                ("plain",),
            )
            plain_publication = cur.fetchone()["publication_json"]
        check_equal(json.loads(plain_publication).get("format"), "",
                    "plain cache rows keep an empty publication format")
        plain_cached = db.get_cached_novel_metadata("plain")
        _assert_publication_shape(plain_cached, "plain cached row")
        check_equal(plain_cached["volumes"], None,
                    "plain cached row has no volumes")

        # Re-caching with newer publication data updates the stored JSON.
        db.cache_novel_metadata("executioner", {
            "title": EXECUTIONER_TITLE,
            "synopsis": "synopsis",
            "source": "AniList",
            "format": "NOVEL",
            "status": "RELEASING",
            "volumes": 12,
        })
        updated = db.get_cached_novel_metadata("executioner")
        check_equal(updated["status_label"], "Releasing",
                    "re-cached publication status is updated")
        check_equal(updated["volumes"], 12, "re-cached publication volumes updated")

        all_cached = db.get_all_novel_metadata_cache()
        check_equal(set(all_cached.keys()), {"executioner", "plain"},
                    "get_all_novel_metadata_cache still works")
    finally:
        _cleanup_temp_dir(tmp_dir)

    # Legacy cache table created before publication_json must keep working.
    legacy_dir = _make_temp_dir("legacy-cache")
    try:
        legacy_path = os.path.join(legacy_dir, "legacy.db")
        legacy_conn = sqlite3.connect(legacy_path)
        try:
            legacy_conn.executescript("""
                CREATE TABLE novel_metadata_cache (
                    query_key TEXT PRIMARY KEY,
                    title TEXT,
                    synopsis TEXT,
                    cover_url TEXT,
                    source TEXT,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO novel_metadata_cache
                    (query_key, title, synopsis, cover_url, source)
                VALUES ('legacy', 'Legacy Title', 'legacy synopsis', '', 'AniList');
            """)
            legacy_conn.commit()
        finally:
            legacy_conn.close()
        legacy_db = Database(legacy_path)
        legacy_cached = legacy_db.get_cached_novel_metadata("legacy")
        check(legacy_cached is not None, "a legacy cache row is readable")
        check_equal(legacy_cached["title"], "Legacy Title",
                    "a legacy cache row keeps its title")
        check("publication_json" in _cache_columns(legacy_db),
              "the publication_json migration is applied to a legacy cache table")
        _assert_publication_shape(legacy_cached, "legacy cached row")
        check_equal(legacy_cached["volumes"], None,
                    "a legacy cache row has no publication volumes")
    finally:
        _cleanup_temp_dir(legacy_dir)


# ---------------------------------------------------------------------------
# 5. store_series_anilist_metadata persists publication keys
# ---------------------------------------------------------------------------
def test_store_series_anilist_metadata_publication():
    section("5. store_series_anilist_metadata persists publication keys")
    tmp_dir = _make_temp_dir("series-meta")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        tracker = _insert_tracker(db, "Executioner Series", calibre_id=55)
        db.store_series_anilist_metadata(
            tracker["id"],
            {
                "title": EXECUTIONER_TITLE,
                "english_title": EXECUTIONER_TITLE,
                "romaji_title": "Shokei Shoujo no Virgin Road",
                "synonyms": ["The Executioner and Her Way of Life"],
                "anilist_id": 12345,
                "source": "AniList",
                "format": "NOVEL",
                "status": "FINISHED",
                "volumes": 11,
                "chapters": 55,
            },
        )
        stored = _stored_meta(db, tracker["id"])
        for key in PUBLICATION_KEYS:
            check(key in stored, f"stored anilist_meta_json contains {key}")
        check_equal(stored["format"], "NOVEL", "stored raw format")
        check_equal(stored["format_label"], "Light Novel", "stored format_label")
        check_equal(stored["is_light_novel"], True, "stored is_light_novel")
        check_equal(stored["status"], "FINISHED", "stored raw status")
        check_equal(stored["status_label"], "Finished", "stored status_label")
        check_equal(stored["volumes"], 11, "stored volumes")
        check_equal(stored["chapters"], 55, "stored chapters")
        check_equal(stored["total_volumes"], 11, "stored total_volumes")
        stored_row = db.get_series_by_name("Executioner Series")
        check_equal(stored_row["name"], "Executioner Series",
                    "store_series_anilist_metadata keeps the stable name")
        check_equal(stored_row["display_title"], EXECUTIONER_TITLE,
                    "store_series_anilist_metadata still sets the display title")

        # Identity-locked trackers keep the v2 behaviour (no enrichment writes).
        locked = _insert_tracker(db, "Locked Series", identity_locked=1)
        db.store_series_anilist_metadata(
            locked["id"],
            {"title": "Should Not Apply", "format": "NOVEL", "status": "FINISHED",
             "volumes": 3},
        )
        check_equal(_stored_meta(db, locked["id"]), {},
                    "identity-locked trackers are not enriched")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 6. store_series_publication_metadata merge semantics
# ---------------------------------------------------------------------------
def test_store_series_publication_metadata():
    section("6. store_series_publication_metadata merges only publication keys")
    tmp_dir = _make_temp_dir("publication-merge")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        existing_meta = {"title": "Existing Title", "custom": "keep-me"}
        tracker = _insert_tracker(
            db, "Executioner Series", calibre_id=55,
            display_title="Executioner Display",
            anilist_meta_json=json.dumps(existing_meta),
        )
        db.add_series_alias_override("Executioner Alias", "Executioner Series",
                                     origin="manual")
        aliases_before = len(db.get_series_alias_overrides())
        db.store_series_publication_metadata(tracker["id"], {
            "format": "NOVEL",
            "status": "FINISHED",
            "volumes": 11,
            "chapters": 55,
        })
        stored = _stored_meta(db, tracker["id"])
        check_equal(stored.get("custom"), "keep-me",
                    "publication merge preserves unrelated stored keys")
        check_equal(stored.get("title"), "Existing Title",
                    "publication merge preserves the stored title key")
        check_equal(stored.get("format"), "NOVEL", "publication merge stores format")
        check_equal(stored.get("format_label"), "Light Novel",
                    "publication merge stores format_label")
        check_equal(stored.get("is_light_novel"), True,
                    "publication merge stores is_light_novel")
        check_equal(stored.get("status"), "FINISHED", "publication merge stores status")
        check_equal(stored.get("status_label"), "Finished",
                    "publication merge stores status_label")
        check_equal(stored.get("volumes"), 11, "publication merge stores volumes")
        check_equal(stored.get("chapters"), 55, "publication merge stores chapters")
        check_equal(stored.get("total_volumes"), 11,
                    "publication merge stores total_volumes")

        row = db.get_series_by_name("Executioner Series")
        check_equal(row["name"], "Executioner Series", "merge never changes name")
        check_equal(row["display_title"], "Executioner Display",
                    "merge never changes display_title")
        check_equal(row["calibre_series_id"], 55, "merge never changes calibre id")
        check_equal(len(db.get_series_alias_overrides()), aliases_before,
                    "merge never touches alias overrides")

        # A full AniList result dict merges only the publication keys.
        db.store_series_publication_metadata(tracker["id"], {
            "title": "A Different Title",
            "english_title": "A Different English Title",
            "format": "NOVEL",
            "status": "RELEASING",
            "volumes": 12,
        })
        stored = _stored_meta(db, tracker["id"])
        check_equal(stored.get("title"), "Existing Title",
                    "a full AniList dict does not overwrite the stored title")
        check_equal(stored.get("status_label"), "Releasing",
                    "a full AniList dict updates the publication status")
        check_equal(stored.get("volumes"), 12,
                    "a full AniList dict updates the publication volumes")

        # No-op cases: unknown id, invalid meta, empty publication, locked tracker.
        db.store_series_publication_metadata(999999, {"format": "NOVEL"})
        check_equal(len(db.get_series_identity_rows()), 1,
                    "unknown series id is a safe no-op")
        before_invalid = _stored_meta(db, tracker["id"])
        for invalid_meta in (None, "garbage", [], {}, {"volumes": -2}, object()):
            db.store_series_publication_metadata(tracker["id"], invalid_meta)
        check_equal(_stored_meta(db, tracker["id"]), before_invalid,
                    "invalid/empty metadata leaves the stored JSON unchanged")
        locked = _insert_tracker(
            db, "Locked Series", identity_locked=1,
            anilist_meta_json=json.dumps({"format": "NOVEL", "volumes": 3}),
        )
        db.store_series_publication_metadata(
            locked["id"], {"format": "NOVEL", "status": "FINISHED", "volumes": 11}
        )
        locked_meta = _stored_meta(db, locked["id"])
        check_equal(locked_meta.get("volumes"), 3,
                    "identity-locked rows are not modified")

        # Never raises even for pathological inputs.
        long_title = "X" * 5000
        db.store_series_publication_metadata(
            tracker["id"], {"title": long_title, "format": None, "volumes": "NaN"}
        )
        check(True, "store_series_publication_metadata never raises")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
TESTS = [
    test_publication_from_meta_matrix,
    test_query_anilist_publication_fields,
    test_fetch_publication_paths,
    test_metadata_cache_publication_json,
    test_store_series_anilist_metadata_publication,
    test_store_series_publication_metadata,
]


def main():
    global FAILED
    print("=" * 78)
    print("  NovelTrax AniList publication metadata tests (v3 §2-§3)")
    print(f"  interpreter optimize level: {sys.flags.optimize}")
    print("=" * 78)
    for test_fn in TESTS:
        try:
            test_fn()
        except AssertionError:
            # check() already recorded and printed the failure.
            continue
        except Exception as exc:  # noqa: BLE001 - keep running the remaining suites
            FAILED += 1
            FAILURES.append(f"{test_fn.__name__}: {exc}")
            print(f"  [FAIL] {test_fn.__name__} raised {type(exc).__name__}: {exc}")
            traceback.print_exc()

    print("\n" + "=" * 78)
    print(f"  {PASSED} checks passed, {FAILED} failed")
    print("=" * 78)
    if FAILED:
        for failure in FAILURES:
            print(f"  - {failure}")
        raise SystemExit(1)
    print("ALL PUBLICATION METADATA TESTS PASSED")


if __name__ == "__main__":
    main()
