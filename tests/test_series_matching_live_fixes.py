#!/usr/bin/env python3
"""Series-matching regression tests against titles copied from the live library.

Covers the three production defects found in the 2026-11-17 audit:

1. Calibre owned-volume contamination: sibling series names split on " - "
   shared the parent's volumes ("Classroom of the Elite - Year 3" owning
   1-12.5 while Calibre holds two books), which suppressed requests for
   genuinely missing volumes.
2. Discovery leaked already-tracked/owned series: punctuation, case and
   cross-script alias spellings ("The Devil Is a Part-Timer",
   "Sword Oratoria") bypassed the raw ``lower(trim())`` exclusion and the
   dedup partition.
3. The series pickers could not find a tracked/Calibre series by the release
   spelling ("DanMachi") because only the stable name/authors were searched.

Hermetic: temp DB and temp Calibre metadata.db (``tempfile.gettempdir()``,
never ``mkdtemp``), no network, explicit ``check()`` only.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
for _path in (BASE_DIR, BACKEND_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.calibre.scanner import CalibreScanner  # noqa: E402
from app.database import Database  # noqa: E402
from app.parser import series_key  # noqa: E402
import app.main as main_module  # noqa: E402

_TEMP_PATHS = []


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def check_eq(actual, expected, message):
    check(actual == expected, f"{message}: expected {expected!r}, got {actual!r}")


def temp_path(suffix):
    path = os.path.join(
        tempfile.gettempdir(),
        f"noveltrax_live_matching_{os.getpid()}_{uuid.uuid4().hex[:10]}{suffix}",
    )
    _TEMP_PATHS.append(path)
    return path


def make_db():
    path = temp_path(".db")
    _TEMP_PATHS.extend([path + "-wal", path + "-shm"])
    db = Database(path)
    with db.get_connection() as conn:
        conn.execute("DELETE FROM feeds;")
        conn.commit()
    return db


def make_calibre_library():
    """Minimal Calibre metadata.db with the live sibling-series shape."""
    folder = temp_path("_calibre")
    os.makedirs(folder, exist_ok=True)
    db_path = os.path.join(folder, "metadata.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE series (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE,
                sort TEXT, link TEXT NOT NULL DEFAULT '', UNIQUE(name)
            );
            CREATE TABLE books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT 'Unknown' COLLATE NOCASE,
                sort TEXT COLLATE NOCASE,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                pubdate TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                series_index REAL NOT NULL DEFAULT 1.0,
                author_sort TEXT COLLATE NOCASE,
                path TEXT NOT NULL DEFAULT '',
                uuid TEXT,
                has_cover BOOL DEFAULT 0,
                last_modified TIMESTAMP NOT NULL DEFAULT '2000-01-01 00:00:00+00:00'
            );
            CREATE TABLE authors (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE,
                sort TEXT COLLATE NOCASE, link TEXT NOT NULL DEFAULT '', UNIQUE(name)
            );
            CREATE TABLE books_authors_link (
                id INTEGER PRIMARY KEY, book INTEGER NOT NULL,
                author INTEGER NOT NULL, UNIQUE(book, author)
            );
            CREATE TABLE books_series_link (
                id INTEGER PRIMARY KEY, book INTEGER NOT NULL,
                series INTEGER NOT NULL, UNIQUE(book)
            );
            """
        )
        cur = conn.cursor()
        series = [
            (53, "Classroom of the Elite"),
            (54, "Classroom of the Elite - Year 2"),
            (55, "Classroom of the Elite - Year 3"),
            (455, "Is It Wrong to Pick Up Girls in a Dungeon? - Familia Chronicle"),
            (12, "Is It Wrong to Pick Up Girls in a Dungeon?"),
            (900, "Overlord"),
        ]
        for series_id, name in series:
            cur.execute(
                "INSERT INTO series (id, name, sort) VALUES (?, ?, ?);",
                (series_id, name, name),
            )
        books = (
            [(53, float(i)) for i in range(1, 4)]      # parent: 1-3
            + [(54, float(i)) for i in range(1, 3)]    # Year 2: 1-2
            + [(55, 7.0), (55, 8.0)]                   # Year 3: 7-8 only (distinct)
            + [(455, 1.0), (455, 2.0)]                 # side story: 1-2
            + [(12, float(i)) for i in range(1, 6)]    # main DanMachi: 1-5
        )
        for series_id, index in books:
            cur.execute(
                "INSERT INTO books (title, series_index) VALUES (?, ?);",
                (f"Volume {index:g}", index),
            )
            cur.execute(
                "INSERT INTO books_series_link (book, series) VALUES (?, ?);",
                (cur.lastrowid, series_id),
            )
        # Omnibus title must still expand into every contained volume.
        cur.execute(
            "INSERT INTO books (title, series_index) VALUES ('Overlord Vol 1-3', 1.0);"
        )
        cur.execute(
            "INSERT INTO books_series_link (book, series) VALUES (?, 900);",
            (cur.lastrowid,),
        )
        conn.commit()
    finally:
        conn.close()
    return folder


# ---------------------------------------------------------------------------
# 1. Calibre volume isolation
# ---------------------------------------------------------------------------
def check_volumes_map_isolation():
    library = make_calibre_library()
    scanner = CalibreScanner(library)
    volumes = scanner.get_series_volumes_map()

    check_eq(volumes[str(55)], {7.0, 8.0},
             "Calibre volumes map: Year 3 must own only its own books")
    check_eq(volumes[str(53)], {1.0, 2.0, 3.0},
             "Calibre volumes map: parent must not inherit Year 3 volumes")
    check_eq(volumes[str(455)], {1.0, 2.0},
             "Calibre volumes map: Familia Chronicle must not inherit DanMachi volumes")
    check_eq(volumes[str(900)], {1.0, 2.0, 3.0},
             "Calibre volumes map: omnibus expansion must be preserved")

    # Dash components may exist for dual-title books, but a component already
    # owned by a real series must keep that series' volumes (sibling isolation).
    check_eq(volumes["Classroom of the Elite"], {1.0, 2.0, 3.0},
             "Calibre volumes map: parent component must not absorb Year 3 volumes")
    check_eq(volumes.get("year 3"), {7.0, 8.0},
             "Calibre volumes map: Year 3 component resolves to Year 3 only")

    db = make_db()
    db.sync_calibre_series(scanner.get_all_series(), volumes_map=volumes)
    rows = {
        row["calibre_series_id"]: json.loads(row["owned_volumes_json"] or "[]")
        for row in db.get_connection().execute(
            "SELECT calibre_series_id, owned_volumes_json FROM series_tracking;"
        )
    }
    check_eq(rows[55], [7.0, 8.0], "sync: Year 3 owned volumes")
    check_eq(rows[53], [1.0, 2.0, 3.0], "sync: parent owned volumes")
    check_eq(rows[455], [1.0, 2.0], "sync: Familia Chronicle owned volumes")
    check_eq(rows[12], [1.0, 2.0, 3.0, 4.0, 5.0], "sync: main DanMachi owned volumes")
    check_eq(rows[900], [1.0, 2.0, 3.0], "sync: omnibus owned volumes")


def check_sync_name_fallback_is_isolated():
    """Callers that only pass name keys must still get per-series volumes."""
    db = make_db()
    series = [
        {"series_id": 53, "series_name": "Classroom of the Elite",
         "book_count": 3, "latest_index": 3.0, "authors": "A"},
        {"series_id": 55, "series_name": "Classroom of the Elite - Year 3",
         "book_count": 2, "latest_index": 8.0, "authors": "A"},
    ]
    volumes_map = {
        "classroom of the elite": {1.0, 2.0, 3.0},
        "classroom of the elite year 3": {7.0, 8.0},
    }
    db.sync_calibre_series(series, volumes_map=volumes_map)
    rows = {
        row["calibre_series_id"]: json.loads(row["owned_volumes_json"] or "[]")
        for row in db.get_connection().execute(
            "SELECT calibre_series_id, owned_volumes_json FROM series_tracking;"
        )
    }
    check_eq(rows[53], [1.0, 2.0, 3.0], "name fallback: parent volumes")
    check_eq(rows[55], [7.0, 8.0], "name fallback: Year 3 volumes")

    # An id key always wins over a colliding name key.
    db2 = make_db()
    db2.sync_calibre_series(
        [{"series_id": 55, "series_name": "Classroom of the Elite - Year 3",
          "book_count": 2, "latest_index": 8.0, "authors": "A"}],
        volumes_map={"55": {7.0, 8.0}, "classroom of the elite year 3": {1.0}},
    )
    owned = json.loads(db2.get_connection().execute(
        "SELECT owned_volumes_json FROM series_tracking WHERE calibre_series_id = 55;"
    ).fetchone()[0])
    check_eq(owned, [7.0, 8.0], "id lookup must win over a colliding name key")


# ---------------------------------------------------------------------------
# 2. Discovery exclusion / dedup
# ---------------------------------------------------------------------------
def seed_release(db, guid, series_name, volume=1.0, status="NEW", media="novel"):
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO releases (guid, title, link, series_name, volume, status, "
            "media_type, pub_date) VALUES (?, ?, ?, ?, ?, ?, ?, '2026-11-01');",
            (guid, f"{series_name} {guid}", f"https://example.test/{guid}",
             series_name, volume, status, media),
        )
        conn.commit()


def check_discovery_matching():
    db = make_db()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO series_tracking (name, calibre_series_id, is_monitored, "
            "book_count, latest_index, owned_volumes_json) "
            "VALUES ('The Devil is a Part-Timer!', 12, 1, 10, 10.0, '[1,2,3]');"
        )
        conn.execute(
            "INSERT INTO series_tracking (name, calibre_series_id, is_monitored, "
            "book_count, latest_index, owned_volumes_json) "
            "VALUES ('Is It Wrong to Pick Up Girls in a Dungeon? - Sword Oratoria', "
            "456, 1, 10, 10.0, '[1,2]');"
        )
        conn.execute(
            "INSERT INTO series_alias_overrides (source_name, source_norm, target_name, "
            "target_norm, calibre_series_id, origin, actor) "
            "VALUES (?, ?, ?, ?, 456, 'anilist', 'anilist');",
            (
                "มันผิดรึไงถ้าใจอยากจะพบรักในดันเจี้ยน ภาคพิเศษ Sword Oratoria",
                "thai-sword-oratoria",
                "Is It Wrong to Pick Up Girls in a Dungeon? - Sword Oratoria",
                "is it wrong to pick up girls in a dungeon sword oratoria",
            ),
        )
        conn.commit()

    seed_release(db, "d1", "The Devil Is a Part-Timer")          # tracked variant
    seed_release(db, "d2", "Sword Oratoria")                     # alias variant
    seed_release(db, "d3", "D-Genesis_ Three Years After the Dungeons Appeared")
    seed_release(db, "d4", "Ayakashi à la Mode")                 # accent duplicate
    seed_release(db, "d5", "Ayakashi a la Mode")
    seed_release(db, "d6", "Brand New Series")                   # genuinely new
    seed_release(db, "d7", "The Devil Is a Part-Timer", volume=2.0)  # not discovery
    seed_release(db, "d8", "Another New Series", status="IN_CALIBRE")

    # Register the spelling that resolves to an existing tracked series.
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO series_alias_overrides (source_name, source_norm, target_name, "
            "target_norm, calibre_series_id, origin, actor) "
            "VALUES ('D-Genesis: Three Years after the Dungeons Appeared', "
            "'d genesis three years after the dungeons appeared', "
            "'The Devil is a Part-Timer!', "
            "'the devil is a part timer', 12, 'anilist', 'anilist');"
        )
        conn.commit()

    items = db.get_discovery_releases(limit=50)
    names = sorted(item["series_name"] for item in items)
    check_eq(names, ["Ayakashi a la Mode", "Brand New Series"],
             "discovery: tracked/aliased variants excluded, accent duplicate collapsed")
    check_eq(db.count_discovery_releases(), 2, "discovery: count matches the list")

    # Blacklist also matches punctuation/case variants.
    db.add_to_blacklist("Brand-New Series!", "test")
    check_eq(db.count_discovery_releases(), 1,
             "discovery: blacklist must use the normalized series key")
    left = db.get_discovery_releases(limit=50)
    check_eq([item["series_name"] for item in left], ["Ayakashi a la Mode"],
             "discovery: blacklisted row filtered, other rows kept")

    # series_key is the shared identity primitive behind the SQL filter.
    check_eq(series_key("The Devil is a Part-Timer!"),
             series_key("The Devil Is a Part-Timer"),
             "series_key: punctuation/case variants must agree")
    check_eq(series_key("Ayakashi à la Mode"), series_key("Ayakashi a la Mode"),
             "series_key: accents must fold")
    check_eq(series_key("มันผิดรึไงถ้าใจอยากจะพบรักในดันเจี้ยน ภาคพิเศษ Sword Oratoria"),
             "sword oratoria",
             "series_key: a mixed-script alias must expose its Latin half")


def check_clear_discovered_matches_view():
    """The maintenance purge must delete exactly the rows the Discovery page hides."""
    db = make_db()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO series_tracking (name, calibre_series_id, is_monitored, "
            "book_count, latest_index, owned_volumes_json) "
            "VALUES ('The Devil is a Part-Timer!', 12, 1, 10, 10.0, '[1]');"
        )
        conn.execute(
            "INSERT INTO series_tracking (name, calibre_series_id, is_monitored, "
            "book_count, latest_index, owned_volumes_json) "
            "VALUES ('Is It Wrong to Pick Up Girls in a Dungeon? - Sword Oratoria', "
            "456, 1, 10, 10.0, '[1]');"
        )
        conn.execute(
            "INSERT INTO series_alias_overrides (source_name, source_norm, target_name, "
            "target_norm, calibre_series_id, origin, actor) "
            "VALUES ('Sword Oratoria', 'sword oratoria', "
            "'Is It Wrong to Pick Up Girls in a Dungeon? - Sword Oratoria', "
            "'is it wrong to pick up girls in a dungeon sword oratoria', "
            "456, 'anilist', 'anilist');"
        )
        conn.commit()

    seed_release(db, "c1", "The Devil Is a Part-Timer")
    seed_release(db, "c2", "Sword Oratoria")
    seed_release(db, "c3", "Unreviewed New Series")
    seed_release(db, "c4", "Pinned New Series")
    with db.get_connection() as conn:
        conn.execute("UPDATE releases SET series_pinned = 1 WHERE guid = 'c4';")
        conn.commit()

    removed = db.clear_discovered_series()
    check_eq(removed, 1, "purge: only the row shown in Discovery is deleted")
    with db.get_connection() as conn:
        left = {row["guid"] for row in conn.execute("SELECT guid FROM releases;")}
    check_eq(sorted(left), ["c1", "c2", "c4"],
             "purge: tracked/alias/pinned rows must survive")


# ---------------------------------------------------------------------------
# 3. Series pickers find alias spellings
# ---------------------------------------------------------------------------
def check_picker_alias_search():
    tracked = [
        {"id": 12, "name": "Is It Wrong to Pick Up Girls in a Dungeon?",
         "display_title": "", "authors": "Fujino Omori", "calibre_series_id": 12,
         "book_count": 20, "owned_volumes_json": "[1,2,3]"},
        {"id": 134, "name": "Is It Wrong to Pick Up Girls in a Dungeon? - Sword Oratoria",
         "display_title": "", "authors": "Fujino Omori", "calibre_series_id": 456,
         "book_count": 20, "owned_volumes_json": "[1,2]"},
        {"id": 900, "name": "Overlord", "display_title": "", "authors": "Kugane Maruyama",
         "calibre_series_id": 900, "book_count": 16, "owned_volumes_json": "[1]"},
    ]
    aliases = [
        {"source_name": "DanMachi", "target_name": "Is It Wrong to Pick Up Girls in a Dungeon?",
         "calibre_series_id": 12},
        {"source_name": "Danmachi Sword Oratoria",
         "target_name": "Is It Wrong to Pick Up Girls in a Dungeon? - Sword Oratoria",
         "calibre_series_id": 456},
        {"source_name": "Dungeon ni Deai wo Motomeru no wa Machigatteiru no Darou ka",
         "target_name": "Is It Wrong to Pick Up Girls in a Dungeon?",
         "calibre_series_id": 12},
    ]
    payload = main_module._tracked_series_picker_payload(tracked, aliases, "DanMachi", 10)
    ids = [entry["id"] for entry in payload]
    check_eq(ids, [12, 134], "picker: alias substring search returns both DanMachi series")
    check_eq(ids[0], 12, "picker: the exact alias match ranks first")

    payload = main_module._tracked_series_picker_payload(tracked, aliases, "Danmachi Sword", 10)
    check_eq([entry["id"] for entry in payload], [134], "picker: alias prefix search")

    payload = main_module._tracked_series_picker_payload(tracked, aliases, "Machigatteiru", 10)
    check_eq([entry["id"] for entry in payload], [12],
             "picker: romaji alias search must find the tracked series")

    payload = main_module._tracked_series_picker_payload(tracked, aliases, "overlord", 10)
    check_eq([entry["id"] for entry in payload], [900], "picker: plain name search still works")

    payload = main_module._tracked_series_picker_payload(tracked, aliases, "nope", 10)
    check_eq(payload, [], "picker: unrelated queries stay empty")


def cleanup():
    for path in _TEMP_PATHS:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def main():
    try:
        check_volumes_map_isolation()
        check_sync_name_fallback_is_isolated()
        check_discovery_matching()
        check_clear_discovered_matches_view()
        check_picker_alias_search()
    finally:
        cleanup()
    print(
        "series-matching live fixes PASSED: per-series Calibre volumes (no sibling "
        "union, omnibus kept), alias/punctuation/cross-script discovery exclusion + "
        "dedup, and alias-aware tracked/Calibre picker search"
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
