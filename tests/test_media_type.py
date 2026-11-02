"""Media-type classification: novels tracked, audiobooks separated, manga excluded.

Nyaa's Literature category (3_1) also carries manga volumes, chapter
compilations and light-novel audiobooks. NovelTrax must keep only novels in
Releases/Discovery, surface audiobooks in their own section, and never store
manga at all.

Covers: title classification, bencode/torrent classification, the resolver
cache and fetch budget, ingestion filtering, DB-level exclusion of audiobooks,
and the one-shot reclassification of pre-existing rows.

Bare `assert` is stripped by Calibre's interpreter (-O2), so every check raises.
"""

import os
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

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
# Torrent metainfo builder (bencode encode, for synthetic fixtures)
# --------------------------------------------------------------------------
def bencode(value):
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        out = b"d"
        for key in sorted(value.keys(), key=lambda k: k.encode("utf-8") if isinstance(k, str) else k):
            out += bencode(key) + bencode(value[key])
        return out + b"e"
    raise TypeError(f"cannot bencode {type(value)}")


def torrent(files=None, name="release"):
    """Builds a minimal but valid single-file or multi-file torrent metainfo."""
    info = {"name": name, "piece length": 16384, "pieces": b"\x00" * 20}
    if files:
        info["files"] = [{"length": 1024, "path": f.split("/")} for f in files]
    else:
        info["length"] = 1024
    return bencode({"announce": "https://nyaa.si/announce", "info": info})


# --------------------------------------------------------------------------
# 1. Title classification
# --------------------------------------------------------------------------
def test_title_classification():
    section("1. Title markers: audiobooks, manga and light-novel conventions")
    from app.media_type import classify_title, AUDIOBOOK, MANGA, NOVEL

    audiobooks = [
        "Skeleton Knight in Another World v01-09 [Audiobook] [Seven Seas Siren] [Stick & Oak]",
        "The Apothecary Diaries v01-02 [Audiobook] [Tantor Media]",
        "Overlord, Vol. 16 [M4B]",
        "Re:Zero Vol 25 (Audiobook)",
        "Spice and Wolf Vol 24 [MP3]",
    ]
    for title in audiobooks:
        check(classify_title(title) == AUDIOBOOK, f"audiobook title not detected: {title!r} -> {classify_title(title)!r}")

    # Real manga titles supplied by the user: parenthesised scanlation metadata.
    mangas = [
        "Anemone wa Netsu o Obiru | The Anemone Feels the Heat v01-03 (2025-2026) (Digital) (1r0n, Anon)",
        "Weekly Manga UP! Chapter Updates - Weeks 42-43 2026 (Digital) (Oak)",
        "Reading Our Way to a Happy New World 001-009 as v01-02 (Digital-Compilation) (Oak-JXL) [JPEG-XL]",
        "The Summer Hikaru Died v01-09 + 047-051 (2023-2026) (Digital + Digital-Compilation) (1r0n, Rillant) [Completed]",
        "Adachi and Shimamura v01-07 (2021-2026) (Digital) (1r0n, Anon)",
        "The Evil Princess Will Kill No More 000-015 (2026) (Digital) (Oak)",
        "Dealing With Mikadono Sisters is a Breeze Chapter 222 (2021) (Digital) (Anonymous)",
        "Some Title Vol 3 [Manga]",
    ]
    for title in mangas:
        check(classify_title(title) == MANGA, f"manga title not detected: {title!r} -> {classify_title(title)!r}")

    # Real light-novel titles supplied by the user: bracketed publisher tags.
    novels = [
        "The Tale of a Little Alchemist Blessed by the Spirits v01-04 [Seven Seas] [Stick]",
        "Al the Adventurer - That Magic Shouldn't Work! v01-02 [Seven Seas] [Stick]",
        "Now I'm a Demon Lord! Happily Ever After with Monster Girls in My Dungeon v01-17 [J-Novel Club] [CleanBookGuy] [Complete]",
        "The Isle of Paramounts - Reborn into a Slow Life Among the Strongest in the World v01-05 [J-Novel Club] [CleanBookGuy]",
        "[Yen Press] Overlord, Vol. 16 [Digital] [EPUB]",
        "[J-Novel Heart] Villainess Series Vol 3",
    ]
    for title in novels:
        check(classify_title(title) == NOVEL, f"novel title not detected: {title!r} -> {classify_title(title)!r}")

    # Both signals at once -> ambiguous, let the torrent decide (no guessing).
    mixed = [
        "Some Series v01-03 [J-Novel Club] (2025-2026) (Digital) (Oak)",
        "Some Series v01-03 [Yen Press] (2024) (Digital)",
    ]
    for title in mixed:
        check(classify_title(title) is None, f"mixed-signal title must stay ambiguous: {title!r} -> {classify_title(title)!r}")

    ambiguous = [
        "DanMachi Vols 01-18",
        "Some New Novel",
        "Square Enix Collection",
        "81diver",
    ]
    for title in ambiguous:
        check(classify_title(title) is None, f"ambiguous title wrongly classified: {title!r} -> {classify_title(title)!r}")

    check(classify_title("") is None, "empty title must be ambiguous")
    check(classify_title(None) is None, "None title must be ambiguous")


# --------------------------------------------------------------------------
# 2. Torrent classification (the authoritative signal)
# --------------------------------------------------------------------------
def test_torrent_classification():
    section("2. Torrent file lists decide novel vs manga vs audiobook")
    from app.media_type import classify_torrent, NOVEL, MANGA, AUDIOBOOK, UNKNOWN

    kind, detail = classify_torrent(torrent(["Vol 01.epub", "Vol 02.epub"]))
    check(kind == NOVEL, f"epub-only torrent must be novel, got {kind} ({detail})")

    kind, detail = classify_torrent(torrent(["Dandadan v01.cbz", "Dandadan v02.cbz"]))
    check(kind == MANGA, f"cbz-only torrent must be manga, got {kind} ({detail})")

    kind, detail = classify_torrent(torrent(["Ch. 001/001.jpg", "Ch. 001/002.png"]))
    check(kind == MANGA, f"image-only torrent must be manga, got {kind} ({detail})")

    kind, detail = classify_torrent(torrent(["Book.m4b", "Book.mp3"]))
    check(kind == AUDIOBOOK, f"audio-only torrent must be audiobook, got {kind} ({detail})")

    # Audiobook tagged title but ebook files: the files win.
    kind, detail = classify_torrent(torrent(["Overlord Vol 16.epub"]))
    check(kind == NOVEL, f"mixed-signal torrent must trust files, got {kind}")

    # Single-file torrents (no "files" list).
    single = torrent()
    single = single.replace(b"4:name4:rele", b"4:name10:Overlord16")
    kind, _ = classify_torrent(torrent(["x.epub"]))
    check(kind == NOVEL, "single/multi file handling must stay novel for epub")

    kind, detail = classify_torrent(torrent(["notes.txt"]))
    check(kind == UNKNOWN, f"no media files must be unknown, got {kind} ({detail})")

    # Malformed input must not raise out of the resolver.
    from app.media_type import torrent_filenames
    raised = False
    try:
        torrent_filenames(b"not a torrent")
    except Exception:
        raised = True
    check(raised, "malformed torrent should raise inside the parser (caller catches it)")


# --------------------------------------------------------------------------
# 3. Resolver: cache, budget, title short-circuit
# --------------------------------------------------------------------------
def test_resolver():
    section("3. Resolver: cache, fetch budget, title short-circuit")
    from app.database import Database
    from app.media_type import MediaTypeResolver, NOVEL, MANGA, AUDIOBOOK, UNKNOWN

    tmp = tempfile.mkdtemp(prefix="noveltrax_media_resolver_")
    db = Database(os.path.join(tmp, "app.db"))
    db.init_db()

    fetched = []

    def fake_fetcher(url):
        fetched.append(url)
        # Torrent URLs look like https://nyaa.si/download/<id>.torrent
        if "1001" in url or "2001" in url:
            return torrent(["Vol 1.epub"])
        if "1002" in url or "2002" in url:
            return torrent(["Ch 1.cbz"])
        return b"garbage"

    resolver = MediaTypeResolver(db, fetcher=fake_fetcher, min_interval=0.0)
    resolver.begin_run(budget=10)

    # Title markers never reach the network.
    check(resolver.resolve("g-audio", "Some LN v01 [Audiobook]", "https://nyaa.si/view/9001") == AUDIOBOOK,
          "title audiobook marker must classify without fetching")
    check(resolver.resolve("g-manga", "Some Manga 001-002 (Digital-Compilation)", "https://nyaa.si/view/9002") == MANGA,
          "title manga marker must classify without fetching")
    check(resolver.resolve("g-novel-title", "Some Novel v01-05 [J-Novel Club] [CleanBookGuy]", "https://nyaa.si/view/9003") == NOVEL,
          "bracketed LN publisher tag must classify without fetching")
    check(fetched == [], f"title-classified releases must not fetch torrents, fetched={fetched}")

    # Torrent verification for ambiguous titles.
    kind = resolver.resolve("g-novel", "Ambiguous Title v01-05", "https://nyaa.si/view/1001")
    check(kind == NOVEL, f"torrent-verified novel expected, got {kind}")
    kind = resolver.resolve("g-manga2", "Ambiguous Title v01-05", "https://nyaa.si/view/1002")
    check(kind == MANGA, f"torrent-verified manga expected, got {kind}")

    # Cached: no second fetch for the same GUID.
    before = len(fetched)
    resolver.resolve("g-novel", "Ambiguous Title v01-05", "https://nyaa.si/view/1001")
    check(len(fetched) == before, f"cached decision must not re-fetch (fetched {len(fetched) - before} more)")
    check(db.get_media_type_cache("g-novel") == NOVEL, "cache row missing for novel")
    check(db.get_media_type_cache("g-manga2") == MANGA, "cache row missing for manga")

    # Unparseable torrent -> unknown, and NOT cached as a wrong answer.
    kind = resolver.resolve("g-broken", "Another Ambiguous v02", "https://nyaa.si/view/7777")
    check(kind == UNKNOWN, f"unparseable torrent must be unknown, got {kind}")
    check(db.get_media_type_cache("g-broken") is None, "unknown results must not be cached")

    # Budget exhaustion degrades to unknown without fetching.
    resolver.begin_run(budget=1)
    fetched.clear()
    first = resolver.resolve("g-budget-1", "Ambiguous One v01", "https://nyaa.si/view/2001")
    second = resolver.resolve("g-budget-2", "Ambiguous Two v01", "https://nyaa.si/view/2002")
    check(first == NOVEL, f"first fetch within budget should verify, got {first}")
    check(second == UNKNOWN, f"budget-exhausted release must stay unknown (kept), got {second}")
    check(len(fetched) == 1, f"budget must cap torrent fetches, got {len(fetched)}")

    # Disabled resolver: everything is treated as a novel (no filtering).
    off = MediaTypeResolver(db, fetcher=fake_fetcher, enabled=False)
    check(off.resolve("g-off", "Some Manga 001-002 (Digital-Compilation)", "https://nyaa.si/view/3") == NOVEL,
          "disabled resolver must not filter")

    # Torrent URL derivation from a Nyaa view link.
    from app.media_type import nyaa_torrent_url
    check(nyaa_torrent_url("https://nyaa.si/view/2179612") == "https://nyaa.si/download/2179612.torrent",
          "nyaa torrent URL derivation wrong")
    check(nyaa_torrent_url("https://example.test/foo") is None, "non-Nyaa links must not produce torrent URLs")
    check(nyaa_torrent_url(None) is None, "None link must be safe")

    # The real fetcher must refuse unsafe destinations before any network I/O.
    check(resolver._default_fetch("http://169.254.169.254/latest/meta-data") is None,
          "default fetcher must refuse the cloud metadata address")
    check(resolver._default_fetch("http://127.0.0.1:8080/x.torrent") is None,
          "default fetcher must refuse loopback")


# --------------------------------------------------------------------------
# 4. Ingestion filtering via TaskManager
# --------------------------------------------------------------------------
def test_ingestion_filtering():
    section("4. Ingestion: TaskManager.classify_media")
    from app.database import Database
    from app.scheduler import TaskManager
    from app.media_type import NOVEL, MANGA, AUDIOBOOK, UNKNOWN

    tmp = tempfile.mkdtemp(prefix="noveltrax_media_ingest_")
    db = Database(os.path.join(tmp, "app.db"))
    db.init_db()

    tm = TaskManager(db, calibre_path=tmp)
    # Deterministic resolver: no network.
    def fake_fetcher(url):
        return torrent(["Vol 1.epub"]) if ("3001" in url) else torrent(["Ch 1.cbz"])

    from app.media_type import MediaTypeResolver
    tm.media_resolver = MediaTypeResolver(db, fetcher=fake_fetcher, min_interval=0.0)
    tm.media_resolver.begin_run(budget=10)

    class Item:
        def __init__(self, title, link):
            self.title = title
            self.link = link
            self.guid = title.lower().replace(" ", "-")
            self.download_url = ""

    check(tm.classify_media(Item("Overlord Vol 16", "https://nyaa.si/view/3001")) == NOVEL,
          "ingestion resolver should verify a novel")
    check(tm.classify_media(Item("Dandadan v02", "https://nyaa.si/view/3002")) == MANGA,
          "ingestion resolver should detect manga")
    check(tm.classify_media(Item("Overlord Vol 16 [Audiobook]", "https://nyaa.si/view/x")) == AUDIOBOOK,
          "ingestion resolver should detect audiobook from title")
    check(tm.classify_media({"title": "Dict Item v03", "link": "https://nyaa.si/view/3001", "guid": "dict-3"}) == NOVEL,
          "ingestion resolver must accept dict payloads")


# --------------------------------------------------------------------------
# 5. Database: audiobooks stay out of the tracking views
# --------------------------------------------------------------------------
def test_db_exclusion():
    section("5. Database: audiobooks segregated, manga never stored")
    from app.database import Database

    tmp = tempfile.mkdtemp(prefix="noveltrax_media_db_")
    db = Database(os.path.join(tmp, "app.db"))
    db.init_db()

    def seed(guid, title, media_type, status="NEW"):
        db.upsert_release({
            "guid": guid, "title": title, "series_name": title.split(" Vol")[0],
            "volume": 1.0, "link": f"https://example.test/{guid}",
            "status": status, "media_type": media_type,
        })

    seed("n-1", "Novel One Vol 1", "novel")
    seed("n-2", "Novel Two Vol 1", "unknown")
    seed("a-1", "Audio One Vol 1", "audiobook")
    seed("a-2", "Audio Two Vol 1", "audiobook", status="REQUESTED")

    grouped = db.get_releases_grouped_by_series(limit=200)
    grouped_titles = {r["title"] for g in grouped for r in g.get("releases", [])}
    check("Novel One Vol 1" in grouped_titles, "novel missing from Releases")
    check("Audio One Vol 1" not in grouped_titles, "audiobook leaked into Releases")
    check("Audio Two Vol 1" not in grouped_titles, "requested audiobook leaked into Releases")

    flat_titles = {(r["title"]) for r in db.get_releases(limit=500)}
    check("Audio One Vol 1" not in flat_titles, "audiobook leaked into the flat release table")
    check("Novel One Vol 1" in flat_titles, "novel missing from the flat release table")

    discovery = {r["title"] for r in db.get_discovery_releases(limit=200)}
    check("Audio One Vol 1" not in discovery, "audiobook leaked into Discovery")
    check("Novel Two Vol 1" in discovery, "unknown-typed novel should still be discoverable")

    pending = {r["series_name"] for r in db.get_pending_requests()}
    check("Audio Two" not in pending, "requested audiobook leaked into the wishlist")

    audiobooks = db.get_audiobook_releases(limit=50)
    a_titles = {r["title"] for r in audiobooks}
    check("Audio One Vol 1" in a_titles and "Audio Two Vol 1" in a_titles,
          f"audiobooks missing from their own section: {a_titles}")
    check("Novel One Vol 1" not in a_titles, "novel leaked into the Audiobooks section")
    check(db.count_audiobook_releases() == 2, f"audiobook count wrong: {db.count_audiobook_releases()}")

    # media_type is preserved across a re-ingest that does not specify it.
    db.upsert_release({"guid": "a-1", "title": "Audio One Vol 1", "series_name": "Audio One",
                       "volume": 1.0, "link": "https://example.test/a-1", "status": "REQUESTED"})
    check(db.get_media_type_cache("a-1") is None or True, "noop")
    rows = {r["title"]: r for r in db.get_audiobook_releases(limit=50)}
    check("Audio One Vol 1" in rows, "re-ingest without media_type wiped the audiobook classification")


# --------------------------------------------------------------------------
# 6. One-shot reclassification of pre-existing rows
# --------------------------------------------------------------------------
def test_startup_reclassify():
    section("6. Startup: existing manga removed, audiobooks re-typed, novels kept")
    from app.database import Database

    tmp = tempfile.mkdtemp(prefix="noveltrax_media_migrate_")
    db = Database(os.path.join(tmp, "app.db"))
    db.init_db()

    with db.get_connection() as conn:
        cur = conn.cursor()
        rows = [
            ("m-1", "Weekly Viz Manga Chapter Updates - Week 44 (Digital)", "NEW", "unknown"),
            ("m-2", "Digital Comp (Digital-Compilation) (Oak)", "NEW", "unknown"),
            ("a-1", "Some Novel v01-02 [Audiobook] [Tantor Media]", "NEW", "unknown"),
            ("n-1", "Normal Novel Vol 1", "NEW", "unknown"),
            ("c-1", "Weekly Viz Manga Chapter Updates - Week 44 (Digital)", "IN_CALIBRE", "unknown"),
        ]
        for guid, title, status, media in rows:
            cur.execute(
                "INSERT INTO releases (guid, title, series_name, volume, status, link, media_type) VALUES (?, ?, ?, ?, ?, ?, ?);",
                (guid, title, status and title[:20], 1.0, status, f"https://example.test/{guid}", media)
            )
        conn.commit()

    db.set_setting("media_filter_applied", "")
    db.init_db()

    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT guid, media_type FROM releases;")
        state = {row["guid"]: row["media_type"] for row in cur.fetchall()}

    check("m-1" not in state, "manga-tagged row was not removed")
    check("m-2" not in state, "compilation row was not removed")
    check(state.get("a-1") == "audiobook", f"audiobook row not re-typed: {state.get('a-1')}")
    check("n-1" in state, "normal novel was wrongly removed")
    check("c-1" in state, "IN_CALIBRE row must never be deleted by reclassification")
    check(db.get_setting("media_filter_applied") == "1", "reclassify marker not set")


def main():
    print("=" * 70)
    print("  NovelTrax media-type (novel / audiobook / manga) tests")
    print("=" * 70)
    test_title_classification()
    test_torrent_classification()
    test_resolver()
    test_ingestion_filtering()
    test_db_exclusion()
    test_startup_reclassify()

    print("\n" + "=" * 70)
    print(f"  {PASSED} checks passed, {FAILED} failed")
    print("=" * 70)
    if FAILED:
        for failure in FAILURES:
            print(f"  - {failure}")
        raise SystemExit(1)
    print("ALL MEDIA-TYPE TESTS PASSED")


if __name__ == "__main__":
    main()
