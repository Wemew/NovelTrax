"""Chapter-vs-volume classification and ingestion filtering.

Policy under test: a number is only a VOLUME when the title carries an explicit
volume keyword (Vol / Volume / Vols / V. / Book / Part) or a multi-volume range.
A bare trailing number ("Dandadan 250") or an explicit Chapter/Ch. marker is a
CHAPTER and must never reach Releases, Discovery, the wishlist, or the DB.

Bare `assert` statements are stripped by Calibre's interpreter (-O2), so every
check here raises explicitly.
"""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

PASSED = 0
FAILED = 0
FAILURES = []


def check(cond, msg):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        FAILURES.append(msg)
        print(f"  [FAIL] {msg}")


def section(title):
    print(f"\n--- {title} ---")


# --------------------------------------------------------------------------
# 1. Parser classification
# --------------------------------------------------------------------------
def test_chapter_classification():
    section("1. Parser: bare numbers are chapters, keyword numbers are volumes")
    from app.parser import extract_series_and_volume as parse, is_chapter_release

    chapter_titles = [
        "Dandadan 250",
        "Dandadan 251",
        "[Group] Dandadan 250 [Digital]",
        "Dandadan - 250",
        "One Piece 1100",
        "Solo Leveling Chapter 179",
        "Solo Leveling Ch. 179",
        "Solo Leveling Chap 179",
        "Berserk c374",
        "Mob Psycho 100",
        "Kaguya-sama 271",
        "Blue Lock 265",
        "Oshi no Ko 155",
    ]
    for title in chapter_titles:
        check(
            is_chapter_release(title) is True,
            f"chapter title not classified as chapter: {title!r} -> {parse(title)}"
        )

    volume_titles = [
        "DanMachi Volume 19",
        "Overlord Vol. 16",
        "The Eminence in Shadow v6",
        "Dungeon Crawler Carl Book 7",
        "Avatar Korra Turf Wars Part 1",
        "DanMachi Vols 01-18",
        "Overlord Vol 1-14",
        "DanMachi Omnibus 1 (Vols 1-3)",
        "[Yen Press] Overlord, Vol. 16 [Digital] [EPUB]",
        "Mob Psycho 100 - Vol 1",
        "86 Vol 1",
        "Dandadan Vol 250",
        "Some New Novel",
    ]
    for title in volume_titles:
        check(
            is_chapter_release(title) is False,
            f"volume title wrongly classified as chapter: {title!r} -> {parse(title)}"
        )

    # Diagnostic fields stay populated for chapters (back-compat for callers).
    parsed = parse("Dandadan 250")
    check(parsed["series_name"] == "Dandadan", f"chapter series name wrong: {parsed['series_name']!r}")
    check(parsed["volume"] == 250.0, f"chapter volume diagnostic wrong: {parsed['volume']!r}")
    check(parsed["is_chapter"] is True, "parsed dict must carry is_chapter=True")

    parsed = parse("Dandadan Vol 250")
    check(parsed["is_chapter"] is False, "explicit Vol keyword must not be a chapter")
    check(parsed["volume"] == 250.0, f"explicit volume wrong: {parsed['volume']!r}")

    # Bundles are never chapters.
    for title in ("DanMachi Vols 01-18", "86 - 1-4", "Overlord Vol 1-3"):
        parsed = parse(title)
        if parsed["is_bundle"]:
            check(is_chapter_release(title) is False, f"bundle wrongly flagged as chapter: {title!r}")

    # Bare numeric ranges are bundles of volumes, not chapters, and must expand.
    for title, expected in (
        ("DanMachi 1-4", [1.0, 2.0, 3.0, 4.0]),
        ("86 - 1-4", [1.0, 2.0, 3.0, 4.0]),
        ("Mob Psycho 100 - 1-3", [1.0, 2.0, 3.0]),
    ):
        parsed = parse(title)
        check(parsed["is_bundle"] is True, f"bare range not recognised as bundle: {title!r} -> {parsed}")
        check(parsed["volumes"] == expected, f"bare range volumes wrong for {title!r}: {parsed['volumes']}")
        check(is_chapter_release(title) is False, f"bare range wrongly flagged as chapter: {title!r}")
        check(parsed["series_name"] != "", f"bare range lost its series name: {title!r}")

    # Garbage input must not raise.
    check(is_chapter_release("") is False, "empty title must not be a chapter")
    check(is_chapter_release(None) is False, "None title must not be a chapter")


# --------------------------------------------------------------------------
# 2. Ingestion filter drops chapters from a provider batch
# --------------------------------------------------------------------------
def test_ingestion_filter():
    section("2. Ingestion: TaskManager.filter_chapter_releases drops chapters")
    from app.database import Database
    from app.scheduler import TaskManager

    tmp = tempfile.mkdtemp(prefix="noveltrax_chapter_filter_")
    db = Database(os.path.join(tmp, "app.db"))
    db.init_db()
    tm = TaskManager(db, calibre_path=tmp)

    class Item:
        def __init__(self, title):
            self.title = title
            self.volume = None
            self.series_name = ""

    batch = [
        Item("Dandadan 250"),
        Item("DanMachi Volume 19"),
        Item("Solo Leveling Ch. 179"),
        Item("Overlord Vol. 16"),
        Item("Mob Psycho 100"),
    ]
    kept = tm.filter_chapter_releases(batch)
    kept_titles = [i.title for i in kept]
    check(len(kept) == 2, f"expected 2 kept releases, got {len(kept)}: {kept_titles}")
    check("DanMachi Volume 19" in kept_titles, "explicit volume release was dropped")
    check("Overlord Vol. 16" in kept_titles, "explicit volume release was dropped")
    check("Dandadan 250" not in kept_titles, "chapter release was kept")
    check("Mob Psycho 100" not in kept_titles, "bare-number title was kept")

    # Dict-shaped items are supported too (provider to_dict payloads).
    dict_batch = [{"title": "Dandadan 251"}, {"title": "Re:Zero Vol 25"}]
    kept_dicts = tm.filter_chapter_releases(dict_batch)
    check(len(kept_dicts) == 1, f"dict batch: expected 1 kept, got {kept_dicts}")
    check(kept_dicts[0]["title"] == "Re:Zero Vol 25", f"wrong dict survivor: {kept_dicts}")


# --------------------------------------------------------------------------
# 3. Existing rows: chapters are purged, legit rows and user decisions survive
# --------------------------------------------------------------------------
def test_purge_existing_rows():
    section("3. Cleanup: chapter rows purged once, real volumes and IN_CALIBRE kept")
    from app.database import Database

    tmp = tempfile.mkdtemp(prefix="noveltrax_chapter_purge_")
    db = Database(os.path.join(tmp, "app.db"))
    db.init_db()

    seeds = [
        ("ch-1", "Dandadan 250", "Dandadan", 250.0, "NEW"),
        ("ch-2", "Dandadan 251", "Dandadan", 251.0, "REQUESTED"),
        ("vol-1", "DanMachi Volume 19", "DanMachi", 19.0, "REQUESTED"),
        ("vol-2", "Overlord Vol. 16", "Overlord", 16.0, "NEW"),
        ("own-1", "Dandadan 100", "Dandadan", 100.0, "IN_CALIBRE"),
        ("ign-1", "Mob Psycho 100", "Mob Psycho", 100.0, "IGNORED"),
    ]
    with db.get_connection() as conn:
        cur = conn.cursor()
        for guid, title, series, vol, status in seeds:
            cur.execute(
                "INSERT INTO releases (guid, title, series_name, volume, status, link) VALUES (?, ?, ?, ?, ?, ?);",
                (guid, title, series, vol, status, f"https://example.test/{guid}")
            )
        conn.commit()

    removed = db.purge_chapter_releases()
    check(removed == 2, f"expected 2 chapter rows purged, got {removed}")

    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT guid FROM releases ORDER BY guid;")
        survivors = [row["guid"] for row in cur.fetchall()]

    for guid in ("ch-1", "ch-2"):
        check(guid not in survivors, f"chapter row {guid} survived the purge")
    for guid in ("vol-1", "vol-2", "own-1", "ign-1"):
        check(guid in survivors, f"legitimate row {guid} was deleted by the purge")

    # Purge is idempotent.
    check(db.purge_chapter_releases() == 0, "second purge should remove nothing")

    # And after purging, chapters appear in neither Releases nor Discovery.
    grouped = db.get_releases_grouped_by_series(limit=100)
    grouped_titles = {r["title"] for g in grouped for r in g.get("releases", [])}
    check("Dandadan 250" not in grouped_titles, "purged chapter still shown in Releases")
    discovery_titles = {d["title"] for d in db.get_discovery_releases(limit=100)}
    check("Dandadan 250" not in discovery_titles, "purged chapter still shown in Discovery")


# --------------------------------------------------------------------------
# 4. One-shot startup cleanup is guarded by a setting
# --------------------------------------------------------------------------
def test_startup_cleanup_is_one_shot():
    section("4. Startup: cleanup runs once and never deletes IN_CALIBRE rows")
    from app.database import Database

    tmp = tempfile.mkdtemp(prefix="noveltrax_chapter_startup_")
    db = Database(os.path.join(tmp, "app.db"))
    db.init_db()

    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO releases (guid, title, series_name, volume, status, link) VALUES (?, ?, ?, ?, ?, ?);",
            ("stale-ch", "Dandadan 300", "Dandadan", 300.0, "NEW", "https://example.test/stale")
        )
        conn.commit()

    # Simulate an upgrade: drop the marker, then re-init as the app does at startup.
    db.set_setting("chapter_filter_applied", "")
    db.init_db()

    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM releases WHERE guid = 'stale-ch';")
        remaining = cur.fetchone()[0]
    check(remaining == 0, "startup cleanup did not remove the stale chapter row")
    check(db.get_setting("chapter_filter_applied") == "1", "cleanup marker was not set")

    # A chapter row added AFTER startup (e.g. by an older code path) is not
    # silently deleted on every init -- the marker prevents repeated passes.
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO releases (guid, title, series_name, volume, status, link) VALUES (?, ?, ?, ?, ?, ?);",
            ("late-ch", "Dandadan 301", "Dandadan", 301.0, "NEW", "https://example.test/late")
        )
        conn.commit()
    db.init_db()
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM releases WHERE guid = 'late-ch';")
        still_there = cur.fetchone()[0]
    check(still_there == 1, "cleanup must not re-run once the marker is set")


def main():
    print("=" * 70)
    print("  NovelTrax chapter-vs-volume filter tests")
    print("=" * 70)
    test_chapter_classification()
    test_ingestion_filter()
    test_purge_existing_rows()
    test_startup_cleanup_is_one_shot()

    print("\n" + "=" * 70)
    print(f"  {PASSED} checks passed, {FAILED} failed")
    print("=" * 70)
    if FAILED:
        for f in FAILURES:
            print(f"  - {f}")
        raise SystemExit(1)
    print("ALL CHAPTER-FILTER TESTS PASSED")


if __name__ == "__main__":
    main()
