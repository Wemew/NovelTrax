"""Context-guarded chapter discard (spec §12.2).

A bare-numbered release is only a chapter when stripping the number yields a
series we already know (Calibre library ∪ tracked rows). Otherwise it may be the
first instalment of a series we have never seen, and is kept -- with the number
exposed as chapter data rather than as a volume.

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


class Item:
    """Minimal provider-release stand-in."""

    def __init__(self, title, link="https://example.test/x"):
        self.title = title
        self.link = link
        self.guid = title.lower().replace(" ", "-")
        self.download_url = ""
        self.series_name = None
        self.volume = None
        self.is_bundle = False
        self.volumes = []


def main():
    from app.database import Database
    from app.scheduler import TaskManager

    tmp = tempfile.mkdtemp(prefix="noveltrax_chapter_ctx_")
    db = Database(os.path.join(tmp, "app.db"))
    db.init_db()
    tm = TaskManager(db, calibre_path=tmp)

    # ------------------------------------------------------------------
    section("1. Decision function: context guards the bare-number rule")
    # ------------------------------------------------------------------
    known = {"dandadan", "chainsaw man", "danmachi"}

    # Explicit chapter markers are always chapters, regardless of context.
    for title in ("Dandadan Ch. 250", "Dandadan c250", "Solo Leveling Chapter 179"):
        check(tm.should_discard_chapter(title, known) is True,
              f"explicit chapter marker must be discarded even for unknown series: {title!r}")
        check(tm.should_discard_chapter(title, set()) is True,
              f"explicit chapter marker must be discarded with empty context: {title!r}")

    # Bare number + known series -> chapter.
    for title in ("Dandadan 250", "Dandadan 250.5", "Chainsaw Man 180"):
        check(tm.should_discard_chapter(title, known) is True,
              f"bare number for a known series must be discarded: {title!r}")

    # Bare number + unknown base series -> kept (may be a first volume).
    check(tm.should_discard_chapter("Some New Novel 28", known) is False,
          "bare number for an unknown series must be kept")
    check(tm.should_discard_chapter("Mob Psycho 100", known) is False,
          "'Mob Psycho 100' must be kept: the number is part of the title")

    # Volume releases are never chapters.
    for title in ("Dandadan Vol 12", "DanMachi Vols 01-18", "Dandadan 1-4", "Dandadan Omnibus 1 (Vols 1-3)"):
        check(tm.should_discard_chapter(title, known) is False,
              f"volume release must never be discarded: {title!r}")

    # No context at all -> never guess.
    check(tm.should_discard_chapter("Dandadan 250", None) is False,
          "without context the guard must not discard (never guess)")

    # ------------------------------------------------------------------
    section("2. Setting gate")
    # ------------------------------------------------------------------
    db.set_setting("discard_unmarked_chapters", "false")
    check(tm.should_discard_chapter("Dandadan 250", known) is False,
          "discard_unmarked_chapters=false must keep chapters")
    check(tm.should_discard_chapter("Dandadan Ch. 250", known) is False,
          "discard_unmarked_chapters=false must keep explicit chapters too")
    db.set_setting("discard_unmarked_chapters", "true")
    check(tm.should_discard_chapter("Dandadan 250", known) is True,
          "discard_unmarked_chapters=true must restore discarding")

    # ------------------------------------------------------------------
    section("3. Batch filtering keeps unknown-series releases")
    # ------------------------------------------------------------------
    batch = [
        Item("Dandadan 250"),            # known series -> dropped
        Item("Chainsaw Man 180"),        # known series -> dropped
        Item("Some New Novel 28"),       # unknown series -> kept
        Item("Overlord Vol. 16"),        # volume -> kept
        Item("Dandadan Vol 12"),         # volume for known series -> kept
        Item("Some New Novel Vol 2"),    # volume -> kept
    ]
    kept = tm.filter_chapter_releases(batch, known)
    kept_titles = [item.title for item in kept]
    check("Dandadan 250" not in kept_titles, "known-series chapter was not discarded")
    check("Chainsaw Man 180" not in kept_titles, "known-series chapter was not discarded")
    check("Some New Novel 28" in kept_titles, "unknown-series release was wrongly discarded")
    check("Overlord Vol. 16" in kept_titles, "volume release was wrongly discarded")
    check("Dandadan Vol 12" in kept_titles, "volume release for a known series was wrongly discarded")
    check(len(kept) == 4, f"expected 4 kept releases, got {len(kept)}: {kept_titles}")

    # Legacy strict mode (no context) still drops every bare-numbered release.
    strict = tm.filter_chapter_releases(batch)
    strict_titles = [item.title for item in strict]
    check("Some New Novel 28" not in strict_titles, "strict mode must drop bare numbers")
    check("Overlord Vol. 16" in strict_titles, "strict mode must keep volumes")
    check(len(strict) == 3, f"strict mode expected 3 kept, got {len(strict)}: {strict_titles}")

    # ------------------------------------------------------------------
    section("4. Discards are auditable")
    # ------------------------------------------------------------------
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM activity_logs WHERE event_type = 'CHAPTER_DISCARDED';")
        events = cur.fetchone()[0]
    check(events >= 1, "batch filtering must write a CHAPTER_DISCARDED activity event")

    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT details FROM activity_logs WHERE event_type = 'CHAPTER_DISCARDED' ORDER BY id DESC LIMIT 1;")
        details = cur.fetchone()["details"] or ""
    check("Dandadan 250" in details, f"activity details should name the discarded release, got {details!r}")

    # ------------------------------------------------------------------
    section("5. Enrichment: a kept chapter is not stored as a volume")
    # ------------------------------------------------------------------
    enriched = tm._enrich_release({
        "guid": "keep-1",
        "title": "Some New Novel 28",
        "series_name": "Some New Novel",
        "volume": 28.0,
        "link": "https://example.test/keep-1",
        "status": "NEW",
    })
    check(enriched.get("volume") is None,
          f"kept chapter must not claim a volume number, got {enriched.get('volume')!r}")
    check(enriched.get("chapter_number") == 28.0,
          f"chapter number must be exposed as data, got {enriched.get('chapter_number')!r}")
    check(enriched.get("series_name") == "Some New Novel",
          f"series key must stay clean, got {enriched.get('series_name')!r}")
    check(isinstance(enriched.get("chapter_ranges"), list), "chapter_ranges must always be a list")

    # A genuine volume release keeps its volume.
    vol = tm._enrich_release({
        "guid": "vol-1", "title": "Overlord Vol. 16", "series_name": "Overlord",
        "volume": 16.0, "link": "https://example.test/vol-1", "status": "NEW",
    })
    check(vol.get("volume") == 16.0, f"volume release lost its volume: {vol.get('volume')!r}")

    # Group/revision fields are populated when the parser supports them.
    grouped = tm._enrich_release({
        "guid": "grp-1",
        "title": "The Summer Hikaru Died v01-09 + 047-051 (2023-2026) (Digital) (1r0n, Rillant)",
        "series_name": "The Summer Hikaru Died", "volume": 1.0,
        "link": "https://example.test/grp-1", "status": "NEW",
    })
    check(isinstance(grouped.get("release_groups"), list), "release_groups must be a list after enrichment")
    check(grouped.get("revision") is not None, "revision must be populated after enrichment")

    print("\n" + "=" * 70)
    print(f"  {PASSED} checks passed, {FAILED} failed")
    print("=" * 70)
    if FAILED:
        for failure in FAILURES:
            print(f"  - {failure}")
        raise SystemExit(1)
    print("ALL CHAPTER-CONTEXT TESTS PASSED")


if __name__ == "__main__":
    main()
