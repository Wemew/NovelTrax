#!/usr/bin/env python3
"""Series identity core tests (contract §7 owner A).

Covers:

* ``app.series_identity``: title candidates (splits, colon parts/prefixes,
  parentheticals, AniList synonyms), ``clean_series_for_matching``, the
  ``CalibreSeriesIndex`` exact/substring/fuzzy/ambiguity rules including the
  MangaTrax fuzzy-head guard, ``resolve_series_title`` precedence and
  ``build_alias_lookup``;
* the user's exact case: AniList
  "Trapped in a Dating Sim: Otome Games Are Tough for Us, Too!" resolves to
  Calibre #327 through its synonym, while #174 ("... for Mobs, Too") is never
  chosen and a bare franchise prefix is ambiguous (``None``);
* ``app.database``: guarded/idempotent migrations, alias-override upsert winner
  rules, ``store_series_anilist_metadata`` never rewriting the stable name,
  ``link_series_to_calibre`` merge/re-key/alias/owned-volume/monitored,
  ``match_discovered_series_to_calibre`` for a non-tracked source,
  ``auto_link_series_to_calibre`` exact/fuzzy/ambiguous, duplicate merging,
  discovery exclusion (tracked rows and alias-override sources, monitored or
  not), identity-aware release grouping with a cosmetic ``display_title`` and
  the ``update_release_details`` stable-key preservation (M1 fix);
* ``app.providers.metadata``: AniList/Kitsu title keys (native/synonyms/
  alternate_titles) and ``fetch_series_metadata`` without requiring a synopsis.

Hermetic: no network (metadata HTTP is faked), temp databases live directly
under ``tempfile.gettempdir()`` (``tempfile.mkdtemp()`` is unusable in this
sandbox). Bare ``assert`` is never used: every check goes through ``check()``,
which raises ``AssertionError`` (Calibre's interpreter runs with ``-O2``).
"""

import gc
import json
import os
import shutil
import sys
import tempfile
import traceback
import types

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
for _path in (BASE_DIR, BACKEND_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import app.series_identity as si
from app.database import Database
from app.parser import normalize_text, string_similarity
from app.providers import metadata as metadata_module
from app.providers.metadata import NovelMetadataProvider

PASSED = 0
FAILED = 0
FAILURES = []
_TEMP_COUNTER = 0

TRAPPED_CALIBRE = (
    "Trapped in a Dating Sim: The World of Otome Games is Tough for Us, Too"
)
TRAPPED_MOBS_CALIBRE = (
    "Trapped in a Dating Sim: The World of Otome Games is Tough for Mobs, Too"
)
TRAPPED_ANILIST = "Trapped in a Dating Sim: Otome Games Are Tough for Us, Too!"
TRAPPED_ROMAJI = "Otome Game Sekai wa Mob ni Kibishii Sekai desu"


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


def check_contains(haystack, needle, message):
    check(
        needle in str(haystack or ""),
        f"{message} (expected {needle!r} in {str(haystack or '')[:200]!r})",
    )


def check_raises(exc_type, func, message):
    try:
        func()
    except exc_type:
        check(True, message)
        return
    except Exception as exc:  # noqa: BLE001 - wrong type is a failure
        check(
            False,
            f"{message}: expected {exc_type.__name__}, got {type(exc).__name__}: {exc}",
        )
        return
    check(False, f"{message}: expected {exc_type.__name__}, nothing was raised")


def section(title):
    print(f"\n--- {title} ---")


def _make_temp_dir(label):
    """Unique temp dir under the system TEMP (never ``tempfile.mkdtemp``)."""
    global _TEMP_COUNTER
    _TEMP_COUNTER += 1
    path = os.path.join(
        tempfile.gettempdir(),
        "noveltrax-series-identity-%d-%d-%s" % (os.getpid(), _TEMP_COUNTER, label),
    )
    os.makedirs(path, exist_ok=True)
    return path


def _cleanup_temp_dir(path):
    try:
        gc.collect()
    except Exception:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _insert_tracker(db, name, calibre_id=None, is_monitored=1, display_title=None,
                    anilist_meta_json=None, owned_volumes=None, match_source=None):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO series_tracking
                (name, calibre_series_id, is_monitored, display_title,
                 anilist_meta_json, owned_volumes_json, match_source)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                name, calibre_id, is_monitored, display_title or "",
                anilist_meta_json or "", json.dumps(owned_volumes or []),
                match_source or "",
            ),
        )
        conn.commit()


def _add_release(db, guid, series_name, volume=1.0, status="NEW", title=None):
    db.upsert_release({
        "guid": guid,
        "title": title or f"{series_name} Vol {volume}",
        "series_name": series_name,
        "volume": volume,
        "status": status,
    })
    row = None
    for candidate in db.get_releases(limit=500):
        if candidate.get("guid") == guid:
            row = candidate
            break
    if row is None:
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM releases WHERE guid = ?;", (guid,))
            found = cur.fetchone()
            row = {"id": found["id"] if found else None}
    return row


def _release_series_name(db, guid):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT series_name FROM releases WHERE guid = ?;", (guid,))
        row = cur.fetchone()
        return row["series_name"] if row else None


def _series_names(db):
    return [row["name"] for row in db.get_series_identity_rows()]


# ---------------------------------------------------------------------------
# 1. Candidate generation / cleaning
# ---------------------------------------------------------------------------
def test_title_candidates_and_cleaning():
    section("1. title_candidates, anilist_title_list and clean_series_for_matching")

    candidates = si.title_candidates(
        "Sousou no Frieren | Frieren: Beyond Journey's End"
    )
    check_contains(candidates, "Sousou no Frieren", "pipe split part is a candidate")
    check_contains(
        candidates,
        "Frieren: Beyond Journey's End",
        "pipe split second part is a candidate",
    )

    slash_candidates = si.title_candidates("Romaji Title / English Title")
    check_contains(slash_candidates, "Romaji Title", "slash split part is a candidate")
    check_contains(
        slash_candidates, "English Title", "second slash split part is a candidate"
    )

    dash_candidates = si.title_candidates("Japanese Title \u2013 English Title")
    check_contains(
        dash_candidates, "Japanese Title", "en-dash separated part is a candidate"
    )
    check_contains(
        dash_candidates, "English Title", "second en-dash part is a candidate"
    )

    colon_candidates = si.title_candidates("A: B: C")
    for expected in ("A", "A: B", "A: B: C"):
        check_contains(colon_candidates, expected, f"colon candidate {expected!r} present")

    paren_candidates = si.title_candidates(
        "Tenkyou no Alderamin (Alderamin On The Sky)"
    )
    check_contains(
        paren_candidates, "Alderamin On The Sky", "parenthetical part is a candidate"
    )

    meta = {
        "english_title": TRAPPED_ANILIST,
        "title": TRAPPED_ANILIST,
        "romaji_title": TRAPPED_ROMAJI,
        "native_title": "\u4e59\u5973\u30b2\u30fc\u30e0\u4e16\u754c\u306f\u30e2\u30d6\u306b\u53b3\u3057\u3044\u4e16\u754c\u3067\u3059",
        "synonyms": [TRAPPED_CALIBRE, "Trapped in a Dating Sim"],
    }
    ani_titles = si.anilist_title_list(meta)
    check_equals = check_equal
    check_equals(ani_titles[0], TRAPPED_ANILIST, "anilist_title_list starts with english")
    check_contains(ani_titles, TRAPPED_ROMAJI, "romaji title is included")
    check_contains(ani_titles, TRAPPED_CALIBRE, "synonym is included")
    check_contains(
        ani_titles,
        "\u4e59\u5973\u30b2\u30fc\u30e0\u4e16\u754c\u306f\u30e2\u30d6\u306b\u53b3\u3057\u3044\u4e16\u754c\u3067\u3059",
        "native title is included",
    )
    check_equal(
        len(ani_titles), len({normalize_text(value) for value in ani_titles}),
        "anilist_title_list is normalised-deduplicated",
    )
    candidate_meta = si.title_candidates(TRAPPED_ANILIST, meta)
    check_contains(
        candidate_meta, TRAPPED_CALIBRE, "AniList synonym appears in title_candidates"
    )

    for candidate in candidate_meta:
        check(candidate.strip() != "", "candidate is never empty")
        norm = normalize_text(candidate)
        check(bool(norm), f"candidate {candidate!r} normalises to non-empty")
        tokens = norm.split()
        check(
            not (len(tokens) == 1 and len(tokens[0]) < 3),
            f"candidate {candidate!r} is not a sub-3-character single token",
        )

    check_equal(
        si.clean_series_for_matching("[OSHI NO KO] 001-130 as v01-13"),
        "[OSHI NO KO]",
        "clean_series_for_matching strips a trailing chapter->volume clause",
    )
    check_equal(
        si.clean_series_for_matching("Overlord (Light Novel)"),
        "Overlord",
        "clean_series_for_matching strips a format parenthetical",
    )
    check_equal(
        si.clean_series_for_matching("Mushoku Tensei: Jobless Reincarnation"),
        "Mushoku Tensei: Jobless Reincarnation",
        "clean_series_for_matching keeps a real subtitle colon",
    )
    check_equal(
        si.clean_series_for_matching("Some Title {Plus}"),
        "Some Title",
        "clean_series_for_matching strips brace release tags",
    )
    check_equal(
        normalize_text(si.normalize_series("Re:ZERO -Starting Life-")),
        normalize_text("Re:ZERO -Starting Life-"),
        "normalize_series delegates to parser.normalize_text",
    )


# ---------------------------------------------------------------------------
# 2. The user's Trapped in a Dating Sim case + ambiguity guards
# ---------------------------------------------------------------------------
def test_trapped_in_a_dating_sim_case():
    section("2. Trapped in a Dating Sim: synonym -> #327, #174 never, prefix ambiguous")
    calibre = [
        {"series_id": 327, "series_name": TRAPPED_CALIBRE, "book_count": 9},
        {"series_id": 174, "series_name": TRAPPED_MOBS_CALIBRE, "book_count": 3},
    ]
    index = si.CalibreSeriesIndex(calibre)
    meta = {"english_title": TRAPPED_ANILIST, "synonyms": [TRAPPED_CALIBRE]}
    alt_names = si.anilist_title_list(meta)

    match = index.match(TRAPPED_ANILIST, alt_names=alt_names)
    check(match is not None, "AniList English title resolves to a Calibre series")
    check_equal(match.get("series_id"), 327, "it resolves to Calibre #327")

    without_synonym = index.match(TRAPPED_ANILIST)
    check(
        without_synonym is None,
        "without the AniList synonym spelling the match is not guessed",
    )

    # The #174 "for Mobs" series must never be returned for the AniList title.
    for alt_variant in (alt_names, None, [TRAPPED_ROMAJI], ["Trapped in a Dating Sim"]):
        resolved = index.match(TRAPPED_ANILIST, alt_names=alt_variant)
        check(
            resolved is None or resolved.get("series_id") != 174,
            "the '... for Mobs, Too' series is never chosen for the AniList title",
        )

    check(
        index.match("Trapped in a Dating Sim") is None,
        "a bare franchise prefix shared by both Calibre series is ambiguous -> None",
    )

    # Rule 7 helper: shared prefix + distinguishing qualifier.
    norm_us = normalize_text(TRAPPED_CALIBRE)
    norm_mobs = normalize_text(TRAPPED_MOBS_CALIBRE)
    check(
        si._has_distinguishing_divergence(norm_us, norm_mobs),
        "distinguishing divergence detects 'for Mobs' vs 'for Us, Too'",
    )


# ---------------------------------------------------------------------------
# 3. CalibreSeriesIndex rules
# ---------------------------------------------------------------------------
def test_calibre_series_index_rules():
    section("3. CalibreSeriesIndex exact / substring / fuzzy / ambiguity rules")
    index = si.CalibreSeriesIndex([
        {"series_id": 1, "series_name": "Mushoku Tensei: Jobless Reincarnation"},
        {"series_id": 2, "series_name": "Overlord"},
        {"series_id": 3, "series_name": "Sword Art Online"},
        {"series_id": 4, "series_name": "Sword Art Online: Progressive"},
        {"series_id": 5, "series_name": "Moonlit Sword Chronicle"},
    ])
    check_equal(len(index), 5, "index length equals the usable Calibre series count")

    check_equal(
        index.match("Overlord").get("series_id"), 2, "exact normalised match"
    )
    check_equal(
        index.match("Mushoku Tensei", threshold=0.99).get("series_id"),
        1,
        "short franchise spelling exact-matches the Calibre subtitle form",
    )
    check_equal(
        index.match("Mushoku Tensei: Jobless Reincarnation").get("series_id"),
        1,
        "AniList subtitle spelling exact-matches the Calibre series",
    )
    check(
        index.match("Sword Art Online") is None,
        "a prefix shared by two Calibre series is ambiguous -> None",
    )
    check_equal(
        index.match("Sword Art Online: Progressive").get("series_id"),
        4,
        "the more specific full title still resolves uniquely",
    )

    # Substring containment needs >= 6 chars and length ratio >= 0.70.
    check_equal(
        si.CalibreSeriesIndex([{"series_id": 9, "series_name": "The Faraway Paladin"}])
        .match("The Faraway Paladin: The Torch Portent")
        .get("series_id"),
        9,
        "a subtitle-qualified spelling resolves via its exact colon-prefix candidate",
    )
    check_equal(
        si.CalibreSeriesIndex([{"series_id": 9, "series_name": "Faraway Paladin"}])
        .match("The Faraway Paladin")
        .get("series_id"),
        9,
        "substring containment accepts an 0.84 length-ratio containment",
    )
    check(
        si.CalibreSeriesIndex([{"series_id": 9, "series_name": "The Faraway Paladin"}])
        .match("The Faraway Paladin of the Torch Portent") is None,
        "a long low-ratio qualifier is rejected (ratio >= 0.70 required)",
    )

    # Fuzzy: score > threshold, length ratio >= 0.75, shared token >= 4 chars.
    check_equal(
        index.match("Moonlit Sword Chronicles", threshold=0.90).get("series_id"),
        5,
        "conservative fuzzy/substring match for a one-letter plural difference",
    )

    # MangaTrax fuzzy-head guard: shared generic suffix only must be rejected.
    head_guard = si.CalibreSeriesIndex([
        {"series_id": 10, "series_name": "Level 0 Demon King Becomes an Adventurer in Another World"},
    ]).match("The Red Ranger Becomes an Adventurer in Another World", threshold=0.5)
    check(
        head_guard is None,
        "fuzzy head-token guard rejects a shared generic suffix (MangaTrax port)",
    )
    heads_a = si._fuzzy_head_tokens("The Red Ranger Becomes an Adventurer")
    heads_b = si._fuzzy_head_tokens("Level 0 Demon King Becomes an Adventurer")
    check_equal(
        heads_a, ["red", "ranger"], "fuzzy head tokens skip stopwords and digits"
    )
    check(
        not si._fuzzy_head_agrees(
            "The Red Ranger Becomes an Adventurer",
            "Level 0 Demon King Becomes an Adventurer",
        ),
        "generic suffix alone does not make the title heads agree",
    )

    # Fuzzy ambiguity: near-equal scores for two different series -> None.
    ambiguous_fuzzy = si.CalibreSeriesIndex([
        {"series_id": 11, "series_name": "Adventures of the Seven Kingdom"},
        {"series_id": 12, "series_name": "Adventures of the Seven Kingdomsz"},
    ]).match("Adventures of the Seven Kingdoms", threshold=0.5)
    check(
        ambiguous_fuzzy is None,
        "scores within AMBIGUITY_SCORE_DELTA for different series are ambiguous",
    )

    # Exact ambiguity: duplicate normalised Calibre names -> None (manual match).
    duplicate_exact = si.CalibreSeriesIndex([
        {"series_id": 13, "series_name": "Mushoku Tensei"},
        {"series_id": 14, "series_name": "Mushoku Tensei!"},
    ]).match("Mushoku Tensei")
    check(duplicate_exact is None, "two series with the same exact spelling -> None")

    check(si.CalibreSeriesIndex([]).match("Anything") is None, "empty index matches nothing")
    check_equal(si.CALIBRE_MATCH_THRESHOLD, 0.88, "Calibre match threshold constant")
    check_equal(si.AUTO_LINK_FUZZY_THRESHOLD, 0.90, "auto-link fuzzy threshold constant")
    check_equal(si.SUBSTRING_MIN_LEN, 6, "substring minimum length constant")
    check_equal(si.SUBSTRING_MIN_RATIO, 0.70, "substring minimum ratio constant")
    check_equal(si.AMBIGUITY_SCORE_DELTA, 0.02, "ambiguity score delta constant")


# ---------------------------------------------------------------------------
# 4. resolve_series_title + build_alias_lookup
# ---------------------------------------------------------------------------
def test_resolve_series_title_precedence():
    section("4. resolve_series_title precedence (alias > display/AniList > exact)")
    rows = [
        {
            "id": 1,
            "name": TRAPPED_CALIBRE,
            "display_title": TRAPPED_ANILIST,
            "calibre_series_id": 327,
            "anilist_meta_json": json.dumps({"romaji_title": TRAPPED_ROMAJI}),
        },
        {
            "id": 2,
            "name": "Overlord",
            "display_title": "",
            "calibre_series_id": 5,
            "anilist_meta_json": "",
        },
    ]
    aliases = [
        {
            "id": 77,
            "source_name": "Trapped as a Dating Sim (fan title)",
            "source_norm": normalize_text("Trapped as a Dating Sim (fan title)"),
            "target_name": TRAPPED_CALIBRE,
            "target_norm": normalize_text(TRAPPED_CALIBRE),
            "calibre_series_id": 327,
            "origin": "manual",
        }
    ]

    alias_resolution = si.resolve_series_title(
        "Trapped as a Dating Sim (fan title)", rows, aliases
    )
    check_equal(alias_resolution.canonical_name, TRAPPED_CALIBRE, "alias target is canonical")
    check_equal(alias_resolution.source, "alias", "alias override wins (source='alias')")
    check(
        alias_resolution.matched and alias_resolution.calibre_series_id == 327,
        "alias resolution carries matched=True and the Calibre id",
    )
    check_equal(
        alias_resolution.get("display_title"),
        TRAPPED_ANILIST,
        "alias resolution exposes the row's cosmetic display title",
    )

    display_resolution = si.resolve_series_title(TRAPPED_ANILIST, rows)
    check_equal(display_resolution.canonical_name, TRAPPED_CALIBRE,
                "display title resolves to the stable Calibre name")
    check_equal(display_resolution.source, "anilist", "display title source is 'anilist'")

    romaji_resolution = si.resolve_series_title(TRAPPED_ROMAJI, rows)
    check_equal(romaji_resolution.canonical_name, TRAPPED_CALIBRE,
                "stored AniList romaji title resolves to the stable name")
    check_equal(romaji_resolution.source, "anilist", "romaji source is 'anilist'")

    exact_resolution = si.resolve_series_title("Overlord", rows)
    check_equal(exact_resolution.source, "exact", "exact stable-name match source is 'exact'")
    check_equal(exact_resolution.series_id, 2, "exact resolution carries the tracker id")

    unknown = si.resolve_series_title("A Series Nobody Tracks", rows)
    check_equal(unknown.matched, False, "unknown title is not matched")
    check_equal(unknown.source, "none", "unknown title source is 'none'")
    check_equal(
        unknown.canonical_name, "A Series Nobody Tracks",
        "unknown title falls back to the stripped input wording",
    )

    ambiguous_rows = [
        {"id": 3, "name": "Ambiguous Alpha", "display_title": "Shared Display"},
        {"id": 4, "name": "Ambiguous Beta", "display_title": "Shared Display"},
    ]
    ambiguous = si.resolve_series_title("Shared Display", ambiguous_rows)
    check_equal(ambiguous.matched, False, "ambiguous display title is not guessed")
    check_equal(ambiguous.source, "none", "ambiguous display title source is 'none'")

    lookup = si.build_alias_lookup(rows, aliases)
    alias_norm = normalize_text("Trapped as a Dating Sim (fan title)")
    check(alias_norm in lookup, "build_alias_lookup indexes override sources")
    check_equal(
        lookup[alias_norm]["target_name"], TRAPPED_CALIBRE,
        "alias lookup value carries the stable target name",
    )
    display_norm = normalize_text(TRAPPED_ANILIST)
    check(display_norm in lookup, "build_alias_lookup indexes tracked display titles")
    check_equal(
        lookup[display_norm]["canonical_name"], TRAPPED_CALIBRE,
        "alias lookup canonical name is the stable name",
    )
    check_equal(
        si.SeriesResolution("x", "x").to_dict()["source"], "none",
        "SeriesResolution is dict-friendly",
    )


# ---------------------------------------------------------------------------
# 5. Database migrations / identity rows
# ---------------------------------------------------------------------------
def test_migrations_are_guarded_and_idempotent():
    section("5. series_tracking identity columns + series_alias_overrides are idempotent")
    tmp_dir = _make_temp_dir("migrations")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        for _ in range(3):
            db.init_db()

        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(series_tracking);")
            columns = {row["name"] for row in cur.fetchall()}
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table';"
            )
            tables = {row["name"] for row in cur.fetchall()}
        for column in ("display_title", "anilist_id", "anilist_meta_json", "match_source"):
            check(column in columns, f"series_tracking has the {column} column")
        check("series_alias_overrides" in tables, "series_alias_overrides table exists")

        _insert_tracker(
            db, TRAPPED_CALIBRE, calibre_id=327, display_title=TRAPPED_ANILIST,
            anilist_meta_json=json.dumps({"title": TRAPPED_ANILIST}),
            owned_volumes=[1.0, 2.0],
        )
        db.add_series_alias_override(TRAPPED_ANILIST, TRAPPED_CALIBRE, 327, origin="anilist")
        for _ in range(2):
            db.init_db()

        row = db.get_series_by_calibre_id(327)
        check(row is not None, "tracker row survives repeated init_db")
        check_equal(row["name"], TRAPPED_CALIBRE, "stable name survives re-init")
        check_equal(row["display_title"], TRAPPED_ANILIST, "display title survives re-init")
        check_equal(json.loads(row["owned_volumes_json"]), [1.0, 2.0],
                    "owned volumes survive re-init")
        overrides = db.get_series_alias_overrides()
        check_equal(len(overrides), 1, "alias override survives repeated init_db")
        check_equal(
            db.get_series_by_name(TRAPPED_CALIBRE)["id"], row["id"],
            "get_series_by_name resolves the stable row",
        )
        identity_rows = db.get_series_identity_rows()
        check_equal(len(identity_rows), 1, "get_series_identity_rows returns all trackers")
        check(
            "display_title" in identity_rows[0] and "anilist_meta_json" in identity_rows[0],
            "identity rows include the new columns",
        )
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 6. Alias override upsert rules
# ---------------------------------------------------------------------------
def test_alias_override_upsert_rules():
    section("6. alias overrides: manual rows win, upsert updates, delete returns bool")
    tmp_dir = _make_temp_dir("aliases")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        first = db.add_series_alias_override("Fancy Feed Title", "Calibre Series A", 11,
                                             origin="anilist", actor="anilist")
        check_equal(first["target_name"], "Calibre Series A", "initial alias target")
        check_equal(first["origin"], "anilist", "initial alias origin is anilist")

        manual = db.add_series_alias_override("Fancy Feed Title", "Calibre Series B", 12,
                                              origin="manual", actor="user")
        check_equal(manual["target_name"], "Calibre Series B", "manual row overwrites target")
        check_equal(manual["origin"], "manual", "manual row origin wins")

        ignored = db.add_series_alias_override("Fancy Feed Title", "Calibre Series C", 13,
                                               origin="anilist", actor="anilist")
        check_equal(ignored["target_name"], "Calibre Series B",
                    "an AniList upsert never overwrites a manual row")
        check_equal(ignored["origin"], "manual", "manual origin is preserved")

        # A learned row can be updated by another learned row (no manual row).
        db.add_series_alias_override("Learned Feed Title", "Learned Target A", 21,
                                     origin="anilist", actor="anilist")
        learned_updated = db.add_series_alias_override(
            "Learned Feed Title", "Learned Target B", 22,
            origin="candidate", actor="scheduler",
        )
        check_equal(learned_updated["target_name"], "Learned Target B",
                    "a non-manual row can be updated by a learned row")
        check_equal(learned_updated["origin"], "candidate", "learned origin is updated")

        check_equal(len(db.get_series_alias_overrides()), 2,
                    "upsert keeps a single row per normalized source")
        check_raises(ValueError, lambda: db.add_series_alias_override("", "Target"),
                     "blank source raises ValueError")
        check_raises(ValueError, lambda: db.add_series_alias_override("Source", " "),
                     "blank target raises ValueError")

        learned_id = learned_updated["id"]
        check_equal(db.delete_series_alias_override(learned_id), True,
                    "delete returns True for an existing override")
        check_equal(db.delete_series_alias_override(learned_id), False,
                    "delete returns False for a missing override")
        check_equal(len(db.get_series_alias_overrides()), 1,
                    "only the manual override remains after the learned row is deleted")
        check_equal(db.get_series_alias_overrides()[0]["origin"], "manual",
                    "the surviving override is the manual one")

        # Persistence across a fresh Database instance.
        db.add_series_alias_override("Persistent Alias", "Persistent Target", 9,
                                     origin="manual")
        db2 = Database(os.path.join(tmp_dir, "app.db"))
        persisted = db2.get_series_alias_overrides()
        check_equal(len(persisted), 2, "alias overrides persist across connections")
        persisted_targets = {row["target_name"] for row in persisted}
        check_contains(persisted_targets, "Persistent Target",
                       "persisted alias target is intact")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 7. link_series_to_calibre
# ---------------------------------------------------------------------------
def test_link_series_to_calibre():
    section("7. link_series_to_calibre: merge, re-key all spellings, aliases, volumes")
    tmp_dir = _make_temp_dir("link")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        db.sync_calibre_series([
            {
                "series_id": 327,
                "series_name": TRAPPED_CALIBRE,
                "book_count": 9,
                "latest_index": 8.0,
                "authors": "Author A",
            },
        ])
        # A duplicate tracker under the AniList spelling, plus releases stored
        # under several different spellings of the same series.
        _insert_tracker(db, TRAPPED_ANILIST, is_monitored=1,
                        owned_volumes=[1.0, 2.0])
        db.add_series_alias_override("Legacy Alias Spelling", TRAPPED_ANILIST,
                                     origin="anilist")
        _add_release(db, "link-r1", TRAPPED_ANILIST, 1.0, "NEW")
        _add_release(db, "link-r2", "Trapped in a Dating Sim", 2.0, "NEW")
        _add_release(db, "link-r3", "Legacy Alias Spelling", 3.0, "NEW")
        _add_release(db, "link-r4", TRAPPED_CALIBRE, 8.0, "IN_CALIBRE")

        result = db.link_series_to_calibre(
            TRAPPED_ANILIST,
            {"series_name": TRAPPED_CALIBRE, "series_id": 327,
             "book_count": 12, "latest_index": 10.0},
            actor="tester",
            owned_volumes=[7.0, 8.0],
            origin="candidate",
        )
        check_equal(result["status"], "success", "link returns status success")
        check_equal(result["target"], TRAPPED_CALIBRE, "link target is the Calibre name")
        check_equal(result["calibre_series_id"], 327, "link carries the Calibre series id")
        check_equal(result["merged"], True, "duplicate tracker row was merged")
        check_equal(result["rekeyed_releases"], 3,
                    "releases under every candidate source spelling were re-keyed")
        check_equal(result["book_count"], 12, "merged book count is the max")
        check_equal(result["owned_volumes"], [1.0, 2.0, 7.0, 8.0],
                    "owned volumes are unioned (tracker + link arguments)")

        check(TRAPPED_ANILIST not in _series_names(db),
              "the duplicate AniList-spelling tracker row was deleted")
        target_row = db.get_series_by_calibre_id(327)
        check_equal(target_row["name"], TRAPPED_CALIBRE,
                    "the stable (Calibre) name is unchanged")
        check_equal(target_row["match_source"], "calibre",
                    "origin='candidate' marks an automatic Calibre match")
        check_equal(target_row["is_monitored"], 1, "monitored is OR-merged")
        check_equal(target_row["book_count"], 12, "target book_count merged")
        check_equal(
            sorted(json.loads(target_row["owned_volumes_json"])),
            [1.0, 2.0, 7.0, 8.0],
            "target owned volumes merged",
        )

        check_equal(_release_series_name(db, "link-r1"), TRAPPED_CALIBRE,
                    "release under the exact source name re-keyed")
        check_equal(_release_series_name(db, "link-r2"), TRAPPED_CALIBRE,
                    "release under a split candidate spelling re-keyed")
        check_equal(_release_series_name(db, "link-r3"), TRAPPED_CALIBRE,
                    "release under an alias-override source re-keyed")
        check_equal(_release_series_name(db, "link-r4"), TRAPPED_CALIBRE,
                    "release already at the target name is untouched")
        check_equal(
            db.get_releases(limit=50) and
            next(r["status"] for r in db.get_releases(limit=50) if r["guid"] == "link-r4"),
            "IN_CALIBRE",
            "release statuses are not touched by link_series_to_calibre",
        )

        overrides = db.get_series_alias_overrides()
        source_norms = {row["source_norm"] for row in overrides}
        check(normalize_text(TRAPPED_ANILIST) in source_norms,
              "an alias override is registered for the source name")
        check(normalize_text("Legacy Alias Spelling") in source_norms,
              "an alias override is registered for the legacy alias spelling")
        check(normalize_text("Trapped in a Dating Sim") in source_norms,
              "an alias override is registered for candidate spellings")

        # Deleting an override stops future resolution but never undoes re-keys.
        anilist_override = next(
            row for row in overrides if row["source_norm"] == normalize_text(TRAPPED_ANILIST)
        )
        check_equal(db.delete_series_alias_override(anilist_override["id"]), True,
                    "override deletion succeeds")
        check_equal(_release_series_name(db, "link-r1"), TRAPPED_CALIBRE,
                    "deleting an override never undoes a re-key")
        check_equal(db.get_series_by_calibre_id(327)["name"], TRAPPED_CALIBRE,
                    "deleting an override never changes the stable name")

        # Creating a missing target row + a manual (explicit) link origin.
        manual = db.link_series_to_calibre(
            "Fresh Discovered Title",
            {"series_name": "Brand New Calibre Series", "series_id": 900,
             "book_count": 4, "latest_index": 3.0},
            actor="tester",
        )
        created = db.get_series_by_calibre_id(900)
        check(created is not None, "a missing target tracker row is created")
        check_equal(created["name"], "Brand New Calibre Series",
                    "created tracker uses the Calibre stable name")
        check_equal(created["is_monitored"], 1, "created tracker defaults to monitored")
        check_contains(created["website_url"], "jnovels.com",
                       "created tracker gets the default J-Novels website search")
        check_equal(created["match_source"], "manual",
                    "explicit links are marked match_source='manual'")
        check_equal(manual["merged"], False, "creating a target is not a merge")

        check_raises(
            ValueError,
            lambda: db.link_series_to_calibre("Source", {}),
            "empty Calibre series name raises ValueError",
        )
        check_raises(
            ValueError,
            lambda: db.link_series_to_calibre("", {"series_name": "X"}),
            "empty source name raises ValueError",
        )
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 8. match_discovered_series_to_calibre
# ---------------------------------------------------------------------------
def test_match_discovered_series_to_calibre():
    section("8. match_discovered_series_to_calibre (non-tracked source)")
    tmp_dir = _make_temp_dir("discovered-match")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        db.sync_calibre_series([
            {"series_id": 327, "series_name": TRAPPED_CALIBRE,
             "book_count": 9, "latest_index": 8.0, "authors": ""},
        ])
        _insert_tracker(db, TRAPPED_ANILIST, is_monitored=0)  # possible stale tracker
        _add_release(db, "disc-r1", TRAPPED_ANILIST, 1.0, "NEW")
        _add_release(db, "disc-r2", "Trapped in a Dating Sim", 2.0, "NEW")

        target = db.get_series_by_calibre_id(327)
        result = db.match_discovered_series_to_calibre(
            TRAPPED_ANILIST,
            {"series_name": TRAPPED_CALIBRE, "series_id": 327, "book_count": 10},
            actor="user",
        )
        check_equal(result["status"], "success", "discovery match returns success")
        check_equal(result["discovered"], True, "discovery match is flagged discovered=True")
        check_equal(result["target"], TRAPPED_CALIBRE, "discovery match target is the Calibre name")
        check(result["rekeyed_releases"] >= 2, "discovery match re-keys the source releases")

        check_equal(db.get_series_by_calibre_id(327)["is_monitored"], 1,
                    "the matched target becomes monitored")
        check_equal(_release_series_name(db, "disc-r1"), TRAPPED_CALIBRE,
                    "discovery release under the AniList title re-keyed")
        check_equal(_release_series_name(db, "disc-r2"), TRAPPED_CALIBRE,
                    "discovery release under a candidate spelling re-keyed")
        check_equal(db.get_discovery_releases(limit=100), [],
                    "the matched series is no longer a Discovery item")
        aliases = {row["source_norm"]: row for row in db.get_series_alias_overrides()}
        check(normalize_text(TRAPPED_ANILIST) in aliases,
              "discovery match persists an alias override for future releases")

        # No target row at all: match_discovered creates it.
        _add_release(db, "disc-r3", "Lonely Discovery Title", 1.0, "NEW")
        created_result = db.match_discovered_series_to_calibre(
            "Lonely Discovery Title",
            {"series_name": "Lonely Calibre Series", "series_id": 901,
             "book_count": 2, "latest_index": 1.0},
        )
        check_equal(created_result["target"], "Lonely Calibre Series",
                    "discovery match creates a missing target row")
        created = db.get_series_by_name("Lonely Calibre Series")
        check(created is not None and created["is_monitored"] == 1,
              "created discovery target is monitored")
        check_equal(_release_series_name(db, "disc-r3"), "Lonely Calibre Series",
                    "the discovered release was re-keyed to the created target")

        check_raises(
            ValueError,
            lambda: db.match_discovered_series_to_calibre("", {"series_name": "X"}),
            "blank discovery source raises ValueError",
        )
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 9. auto_link_series_to_calibre + duplicate merge
# ---------------------------------------------------------------------------
def test_auto_link_series_to_calibre():
    section("9. auto_link_series_to_calibre: exact/fuzzy/ambiguous + duplicate merge")
    tmp_dir = _make_temp_dir("auto-link")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        calibre = [
            {"series_id": 327, "series_name": TRAPPED_CALIBRE,
             "book_count": 9, "latest_index": 8.0},
            {"series_id": 174, "series_name": TRAPPED_MOBS_CALIBRE,
             "book_count": 3, "latest_index": 2.0},
            {"series_id": 30, "series_name": "Mushoku Tensei: Jobless Reincarnation",
             "book_count": 20, "latest_index": 19.0},
            {"series_id": 31, "series_name": "Moonlit Sword Chronicle",
             "book_count": 5, "latest_index": 4.0},
        ]
        _insert_tracker(db, TRAPPED_ANILIST, is_monitored=1,
                        anilist_meta_json=json.dumps(
                            {"english_title": TRAPPED_ANILIST,
                             "synonyms": [TRAPPED_CALIBRE]}
                        ))
        _insert_tracker(db, "Mushoku Tensei", is_monitored=1)
        _insert_tracker(db, "Moonlit Sword Chronicles", is_monitored=1)

        results = db.auto_link_series_to_calibre(calibre, actor="scheduler")
        linked = {result["source"]: result for result in results}
        check(TRAPPED_ANILIST in linked,
              "tracker with an AniList synonym is auto-linked")
        check_equal(linked[TRAPPED_ANILIST]["target"], TRAPPED_CALIBRE,
                    "the AniList-synonym tracker links to #327, never #174")
        check_equal(linked[TRAPPED_ANILIST]["calibre_series_id"], 327,
                    "the synonym link carries the #327 Calibre id")
        check("Mushoku Tensei" in linked,
              "the short franchise tracker auto-links via candidate exact match")
        check_equal(linked["Mushoku Tensei"]["calibre_series_id"], 30,
                    "the short franchise tracker links to the right series")
        check("Moonlit Sword Chronicles" in linked,
              "the one-letter-different tracker auto-links conservatively")
        check_equal(linked["Moonlit Sword Chronicles"]["calibre_series_id"], 31,
                    "conservative fuzzy links to the intended series")
        check(TRAPPED_ANILIST not in _series_names(db),
              "the AniList tracker row was merged into the stable Calibre name")
        check_equal(db.get_series_by_calibre_id(174), None,
                    "the '... for Mobs' Calibre series is never linked by this pass")

        # Ambiguous duplicate Calibre rows: leave the tracker for manual match.
        _insert_tracker(db, "Ambiguous Series Zeta", is_monitored=1)
        ambiguous_results = db.auto_link_series_to_calibre(
            [
                {"series_id": 40, "series_name": "Ambiguous Series Zeta"},
                {"series_id": 41, "series_name": "Ambiguous Series Zeta!"},
            ],
            actor="scheduler",
        )
        check_equal(ambiguous_results, [],
                    "ambiguous exact duplicates produce no auto-link")
        ambiguous_row = db.get_series_by_name("Ambiguous Series Zeta")
        check(ambiguous_row is not None and ambiguous_row["calibre_series_id"] is None,
              "the ambiguous tracker keeps calibre_series_id NULL for a manual match")

        # One bad row never aborts the pass.
        _insert_tracker(db, "* * *", is_monitored=1)
        _insert_tracker(db, "Overlord", is_monitored=1)
        noisy_results = db.auto_link_series_to_calibre(
            [None, "not-a-dict", {"series_id": 50, "series_name": "Overlord"}],
            actor="scheduler",
        )
        check_equal(len(noisy_results), 1, "bad Calibre entries are ignored, the valid one links")
        check_equal(noisy_results[0]["target"], "Overlord", "valid remainder is linked")

        # Duplicate tracker rows for the same Calibre series are merged.
        _insert_tracker(db, "Solo Leveling", is_monitored=1)
        _insert_tracker(db, "Solo Leveling: Ragnarok Spinoff Alias", is_monitored=1)
        _insert_tracker(db, "Solo Leveling: Ragnarok Spinoff Alias Rogue", is_monitored=1)
        _insert_tracker(db, "Solo Leveling: Ragnarok Spinoff Alias",
                        display_title="Solo Leveling: Ragnarok Spinoff Alias")
        # Make the two rows genuinely duplicate an AniList-candidate spelling.
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE series_tracking SET anilist_meta_json = ? "
                "WHERE lower(name) = lower(?);",
                (json.dumps({"synonyms": ["Solo Leveling: Ragnarok Spinoff Alias"]}),
                 "Solo Leveling"),
            )
            conn.commit()
        merge_results = db.auto_link_series_to_calibre(
            [{"series_id": 60, "series_name": "Solo Leveling"}], actor="scheduler"
        )
        check_equal({result["target"] for result in merge_results}, {"Solo Leveling"},
                    "every duplicate tracker links to the same stable Calibre name")
        check_equal(len(merge_results), 3,
                    "all three duplicate tracker rows are processed, never skipped")
        check(db.get_series_by_name("Solo Leveling: Ragnarok Spinoff Alias") is None,
              "an AniList-candidate duplicate tracker row is merged away")
        check(db.get_series_by_name("Solo Leveling: Ragnarok Spinoff Alias Rogue") is None,
              "a redundant duplicate tracker row is merged away")
        check_equal(db.get_series_by_name("Solo Leveling")["calibre_series_id"], 60,
                    "the kept tracker carries the Calibre id")

        check_equal(db.auto_link_series_to_calibre([]), [],
                    "empty Calibre list returns an empty result list")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 10. store_series_anilist_metadata
# ---------------------------------------------------------------------------
def test_store_series_anilist_metadata():
    section("10. store_series_anilist_metadata: display title/aliases, name never changes")
    tmp_dir = _make_temp_dir("anilist-store")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        _insert_tracker(db, TRAPPED_CALIBRE, calibre_id=327)
        row = db.get_series_by_name(TRAPPED_CALIBRE)
        meta = {
            "title": TRAPPED_ANILIST,
            "english_title": TRAPPED_ANILIST,
            "romaji_title": TRAPPED_ROMAJI,
            "native_title": "\u4e59\u5973\u30b2\u30fc\u30e0\u4e16\u754c",
            "synonyms": [TRAPPED_CALIBRE, "A Dating Sim Story"],
            "anilist_id": 98765,
            "source": "AniList",
        }
        db.store_series_anilist_metadata(row["id"], meta)
        stored = db.get_series_by_name(TRAPPED_CALIBRE)
        check_equal(stored["name"], TRAPPED_CALIBRE,
                    "store_series_anilist_metadata never changes the stable name")
        check_equal(stored["display_title"], TRAPPED_ANILIST,
                    "display_title holds the cosmetic AniList title")
        check_equal(stored["anilist_id"], 98765, "anilist_id is persisted")
        check_equal(stored["match_source"], "anilist", "blank match_source becomes 'anilist'")
        parsed_meta = json.loads(stored["anilist_meta_json"])
        for key in ("title", "english_title", "romaji_title", "native_title",
                    "synonyms", "source", "fetched_at"):
            check(key in parsed_meta, f"anilist_meta_json contains {key}")
        check_equal(parsed_meta["romaji_title"], TRAPPED_ROMAJI, "meta romaji persisted")
        check_equal(parsed_meta["synonyms"], [TRAPPED_CALIBRE, "A Dating Sim Story"],
                    "meta synonyms persisted")
        check_equal(parsed_meta["source"], "AniList", "meta source persisted")

        overrides = {row["source_norm"]: row for row in db.get_series_alias_overrides()}
        check(normalize_text(TRAPPED_ANILIST) in overrides,
              "English AniList title registered as an alias override")
        check(normalize_text(TRAPPED_ROMAJI) in overrides,
              "romaji AniList title registered as an alias override")
        check(normalize_text("A Dating Sim Story") in overrides,
              "AniList synonym registered as an alias override")
        check(normalize_text(TRAPPED_CALIBRE) not in overrides,
              "an AniList spelling equal to the stable name is not an override")
        check_equal(overrides[normalize_text(TRAPPED_ANILIST)]["origin"], "anilist",
                    "AniList-derived overrides are marked origin='anilist'")

        # Manual match_source/link is never overwritten by the metadata sync.
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE series_tracking SET match_source = 'manual' WHERE id = ?;",
                (row["id"],),
            )
            conn.commit()
        db.store_series_anilist_metadata(row["id"], dict(meta, synonyms=[]))
        check_equal(db.get_series_by_name(TRAPPED_CALIBRE)["match_source"], "manual",
                    "a non-blank match_source is preserved")

        # A display title equal to the stable name clears the cosmetic field.
        _insert_tracker(db, "Overlord", calibre_id=5)
        overlord = db.get_series_by_name("Overlord")
        db.store_series_anilist_metadata(
            overlord["id"],
            {"english_title": "Overlord", "title": "Overlord", "anilist_id": 2,
             "source": "AniList"},
        )
        check_equal(db.get_series_by_name("Overlord")["display_title"], "",
                    "display_title falls back to empty when it equals the stable name")

        db.store_series_anilist_metadata(999999, meta)  # unknown id must be a no-op
        check_equal(len(db.get_series_identity_rows()), 2,
                    "storing metadata for an unknown series id is a safe no-op")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 11. update_release_details stable-key preservation (M1 fix)
# ---------------------------------------------------------------------------
def test_update_release_details_stable_key():
    section("11. update_release_details keeps the stable key (M1 fix)")
    tmp_dir = _make_temp_dir("stable-update")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        _insert_tracker(db, TRAPPED_CALIBRE, calibre_id=327, display_title=TRAPPED_ANILIST)

        # Pinned behaviour (tests/test_db_identity.py): SERIES_ALIASES canonical
        # titles never replace a stored alias spelling.
        _add_release(db, "stable-alias", "The Neighboring Aarya-San Who Sometimes "
                     "Acts Affectionate and Murmuring in Russian", 3.0, "REQUESTED")
        release = next(r for r in db.get_releases(limit=50) if r["guid"] == "stable-alias")
        db.update_release_details(
            release["id"],
            series_name="Alya Sometimes Hides Her Feelings in Russian",
        )
        check_equal(
            db.get_release_by_id(release["id"])["series_name"],
            "The Neighboring Aarya-San Who Sometimes Acts Affectionate and Murmuring in Russian",
            "canonical SERIES_ALIASES title never rewrites the stored stable spelling",
        )
        # A genuinely different explicit name is still written (pinned behaviour).
        db.update_release_details(release["id"], series_name="A Completely Different Series")
        check_equal(db.get_release_by_id(release["id"])["series_name"],
                    "A Completely Different Series",
                    "an explicitly different series name is still written")

        # A tracked stable key survives an AniList display-title payload.
        _add_release(db, "stable-tracked", TRAPPED_CALIBRE, 1.0, "REQUESTED")
        tracked_release = next(
            r for r in db.get_releases(limit=50) if r["guid"] == "stable-tracked"
        )
        db.update_release_details(tracked_release["id"], series_name=TRAPPED_ANILIST)
        check_equal(db.get_release_by_id(tracked_release["id"])["series_name"],
                    TRAPPED_CALIBRE,
                    "an AniList display title never replaces a tracked stable key")

        # Untracked display spelling + tracked incoming canonical -> adopt stable key.
        _add_release(db, "adopt-stable", TRAPPED_ANILIST, 1.0, "NEW")
        adopt_release = next(
            r for r in db.get_releases(limit=50) if r["guid"] == "adopt-stable"
        )
        db.update_release_details(adopt_release["id"], series_name=TRAPPED_CALIBRE)
        check_equal(db.get_release_by_id(adopt_release["id"])["series_name"],
                    TRAPPED_CALIBRE,
                    "an untracked display spelling adopts a known tracked stable key")

        # Punctuation-only differences keep the stored spelling.
        _add_release(db, "punct-stable", "Some Series, Too", 1.0, "NEW")
        punct_release = next(
            r for r in db.get_releases(limit=50) if r["guid"] == "punct-stable"
        )
        db.update_release_details(punct_release["id"], series_name="Some Series Too")
        check_equal(db.get_release_by_id(punct_release["id"])["series_name"],
                    "Some Series, Too",
                    "normalised-equal spellings preserve the stored stable spelling")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 12. Discovery exclusion + clear_discovered_series
# ---------------------------------------------------------------------------
def test_discovery_exclusion():
    section("12. Discovery excludes tracked rows and alias sources (monitored or not)")
    tmp_dir = _make_temp_dir("discovery")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        _insert_tracker(db, "Tracked Monitored Series", is_monitored=1)
        _insert_tracker(db, "Tracked Unmonitored Series", is_monitored=0)
        db.add_series_alias_override(
            "Alias Per Feed", "Tracked Unmonitored Series", origin="manual"
        )
        _add_release(db, "disc-tracked", "Tracked Unmonitored Series", 1.0, "NEW")
        _add_release(db, "disc-alias", "Alias Per Feed", 1.0, "NEW")
        _add_release(db, "disc-genuine", "Genuine New Discovery", 1.0, "NEW")

        shown = {row["series_name"] for row in db.get_discovery_releases(limit=100)}
        check("Genuine New Discovery" in shown, "a genuine discovery item is shown")
        check("Tracked Unmonitored Series" not in shown,
              "a tracked (even unmonitored) release is not Discovery")
        check("Alias Per Feed" not in shown,
              "an alias-override source targeting a tracked series is not Discovery")
        check_equal(db.count_discovery_releases(), 1,
                    "count_discovery_releases mirrors the discovery predicate")

        cleared = db.clear_discovered_series()
        check_equal(cleared, 1, "clear_discovered_series deletes only genuine discoveries")
        check_equal(db.get_discovery_releases(limit=100), [],
                    "no discovery items remain after clearing")
        check_equal(_release_series_name(db, "disc-genuine"), None,
                    "the genuine discovery row was deleted")
        check_equal(_release_series_name(db, "disc-alias"), "Alias Per Feed",
                    "the alias-matched release was not deleted by clear_discovered_series")
        check_equal(_release_series_name(db, "disc-tracked"), "Tracked Unmonitored Series",
                    "the tracked-series release was not deleted by clear_discovered_series")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 13. Identity-aware grouping with cosmetic display title
# ---------------------------------------------------------------------------
def test_grouped_releases_identity_display_title():
    section("13. get_releases_grouped_by_series groups by identity + display_title")
    tmp_dir = _make_temp_dir("grouping")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        _insert_tracker(db, TRAPPED_CALIBRE, calibre_id=327,
                        display_title=TRAPPED_ANILIST)
        db.add_series_alias_override(TRAPPED_ANILIST, TRAPPED_CALIBRE, 327,
                                     origin="anilist")
        _add_release(db, "group-r1", TRAPPED_CALIBRE, 1.0, "REQUESTED")
        _add_release(db, "group-r2", TRAPPED_ANILIST, 2.0, "REQUESTED")

        groups = db.get_releases_grouped_by_series(include_ignored=False)
        trapped_groups = [
            group for group in groups
            if "otto" in group["stable_name"].lower()
            or "otome" in group["stable_name"].lower()
        ]
        check_equal(len(trapped_groups), 1,
                    "stable name + AniList display spelling collapse into ONE group")
        group = trapped_groups[0]
        check_equal(group["stable_name"], TRAPPED_CALIBRE,
                    "group stable_name is the Calibre stable key")
        check_equal(group["series_key"], normalize_text(TRAPPED_CALIBRE),
                    "group series_key is the normalized STABLE name")
        check_equal(group["display_title"], TRAPPED_ANILIST,
                    "group carries the cosmetic display_title")
        check_equal(group["display_name"], TRAPPED_ANILIST,
                    "group display_name stays the cosmetic display title")
        check_equal(group["series_name"], TRAPPED_ANILIST,
                    "group series_name renders the cosmetic display title")
        check_equal(group["releases_count"], 2,
                    "both spellings are aggregated under the single group")
        check_equal(group["calibre_series_id"], 327, "group exposes the Calibre series id")

        all_series = db.get_all_series()
        tracked = next(row for row in all_series if row["name"] == TRAPPED_CALIBRE)
        check_equal(tracked["display_title"], TRAPPED_ANILIST,
                    "get_all_series selects the new display_title column")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# 14. Metadata provider title keys
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self.headers = {}
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttp:
    """Minimal ``httpx`` stand-in used for the metadata parsing tests."""

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False


def test_metadata_provider_title_keys():
    section("14. metadata provider: native/synonyms/alternate_titles + series fetch")
    saved = {
        "httpx": getattr(metadata_module, "httpx", None),
        "safe_client_get": getattr(metadata_module, "safe_client_get", None),
        "safe_client_request": getattr(metadata_module, "safe_client_request", None),
        "query_anilist": NovelMetadataProvider.__dict__.get("query_anilist"),
        "query_kitsu": NovelMetadataProvider.__dict__.get("query_kitsu"),
        "default_db": NovelMetadataProvider._default_db,
    }
    try:
        anilist_payload = {
            "data": {
                "Media": {
                    "id": 98765,
                    "title": {
                        "romaji": TRAPPED_ROMAJI,
                        "english": TRAPPED_ANILIST,
                        "native": "\u4e59\u5973\u30b2\u30fc\u30e0\u4e16\u754c",
                    },
                    "synonyms": [TRAPPED_CALIBRE],
                    "description": "",
                    "coverImage": {"large": "https://example.invalid/cover.jpg",
                                   "extraLarge": None},
                }
            }
        }
        metadata_module.httpx = _FakeHttp
        metadata_module.safe_client_request = (
            lambda client, method, url, json=None: _FakeResponse(anilist_payload)
        )
        anilist = NovelMetadataProvider.query_anilist("Trapped in a Dating Sim")
        check(anilist is not None, "query_anilist returns title data without a description")
        for key in ("canonical_title", "title", "english_title", "romaji_title",
                    "native_title", "synonyms", "anilist_id", "source",
                    "synopsis", "cover_url"):
            check(key in anilist, f"query_anilist result contains {key}")
        check_equal(anilist["english_title"], TRAPPED_ANILIST,
                    "query_anilist english_title parsed")
        check_equal(anilist["romaji_title"], TRAPPED_ROMAJI,
                    "query_anilist romaji_title parsed")
        check_equal(anilist["native_title"], "\u4e59\u5973\u30b2\u30fc\u30e0\u4e16\u754c",
                    "query_anilist native_title parsed")
        check_equal(anilist["synonyms"], [TRAPPED_CALIBRE],
                    "query_anilist synonyms parsed")
        check_equal(anilist["anilist_id"], 98765, "query_anilist anilist_id parsed")
        check_equal(anilist["source"], "AniList", "query_anilist source is AniList")
        check_equal(anilist["synopsis"], "", "missing description becomes an empty synopsis")
        graphql = metadata_module.NovelMetadataProvider.query_anilist.__doc__ or ""
        check(bool(graphql), "query_anilist is documented")

        kitsu_payload = {
            "data": [
                {
                    "attributes": {
                        "subtype": "novel",
                        "canonicalTitle": "Mushoku Tensei",
                        "titles": {
                            "en": "Mushoku Tensei: Jobless Reincarnation",
                            "ja_jp": "\u7121\u8077\u8ee2\u751f",
                        },
                        "abbreviatedTitles": ["MT"],
                        "synopsis": "A long enough synopsis for Kitsu parsing.",
                        "posterImage": {"large": "https://example.invalid/kitsu.jpg"},
                    }
                }
            ]
        }
        metadata_module.safe_client_get = (
            lambda client, url, max_redirects=5: _FakeResponse(kitsu_payload)
        )
        kitsu = NovelMetadataProvider.query_kitsu("Mushoku Tensei")
        check(kitsu is not None, "query_kitsu returns title data")
        for key in ("canonical_title", "title", "english_title", "romaji_title",
                    "native_title", "alternate_titles"):
            check(key in kitsu, f"query_kitsu result contains {key}")
        check_equal(kitsu["canonical_title"], "Mushoku Tensei",
                    "query_kitsu canonical_title parsed")
        check_equal(kitsu["english_title"], "Mushoku Tensei: Jobless Reincarnation",
                    "query_kitsu english_title parsed")
        check_contains(kitsu["alternate_titles"], "Mushoku Tensei: Jobless Reincarnation",
                       "query_kitsu alternate_titles includes titles values")
        check_contains(kitsu["alternate_titles"], "MT",
                       "query_kitsu alternate_titles includes abbreviatedTitles")
        check_equal(kitsu["romaji_title"], "\u7121\u8077\u8ee2\u751f",
                    "query_kitsu romaji_title parsed from ja_jp")

        # fetch_series_metadata must not require a synopsis.
        title_only = {
            "title": TRAPPED_ANILIST,
            "english_title": TRAPPED_ANILIST,
            "canonical_title": TRAPPED_ANILIST,
            "romaji_title": TRAPPED_ROMAJI,
            "native_title": None,
            "synonyms": [TRAPPED_CALIBRE],
            "anilist_id": 98765,
            "synopsis": "",
            "cover_url": "",
            "source": "AniList",
        }
        NovelMetadataProvider.query_anilist = classmethod(
            lambda cls, title: dict(title_only)
        )
        fetched = NovelMetadataProvider.fetch_series_metadata(TRAPPED_ANILIST)
        check(fetched is not None, "fetch_series_metadata returns title data without synopsis")
        check_equal(fetched["english_title"], TRAPPED_ANILIST,
                    "fetch_series_metadata keeps english_title")
        check_equal(fetched["synonyms"], [TRAPPED_CALIBRE],
                    "fetch_series_metadata keeps synonyms")

        NovelMetadataProvider.query_anilist = classmethod(lambda cls, title: None)
        NovelMetadataProvider.query_kitsu = classmethod(lambda cls, title: None)
        check_equal(NovelMetadataProvider.fetch_series_metadata("Nothing At All"), None,
                    "fetch_series_metadata returns None when both providers fail")
        check_equal(NovelMetadataProvider.fetch_series_metadata(""), None,
                    "fetch_series_metadata returns None for a blank title")

        # Cache reuse returns the same title keys even without a synopsis.
        cache_dir = _make_temp_dir("metadata-cache")
        try:
            cache_db = Database(os.path.join(cache_dir, "app.db"))
            cache_db.cache_novel_metadata(
                "solo leveling",
                {"title": "Solo Leveling", "synopsis": "cached synopsis",
                 "source": "AniList"},
            )
            NovelMetadataProvider.set_db(cache_db)
            cached = NovelMetadataProvider.fetch_series_metadata("Solo Leveling Vol 1 Epub")
            check(cached is not None, "fetch_series_metadata serves from the SQLite cache")
            check_equal(cached["canonical_title"], "Solo Leveling",
                        "cached series metadata keeps canonical_title")
            check_equal(cached["english_title"], "Solo Leveling",
                        "cached series metadata keeps english_title")
            check_equal(cached["source"], "AniList", "cached source survives")
        finally:
            NovelMetadataProvider._default_db = None
            _cleanup_temp_dir(cache_dir)

        # fetch_novel_metadata still exposes the legacy keys plus the new ones.
        NovelMetadataProvider.query_anilist = classmethod(
            lambda cls, title: dict(title_only, synopsis="A long enough synopsis for tests.")
        )
        NovelMetadataProvider.query_kitsu = classmethod(lambda cls, title: None)
        legacy = NovelMetadataProvider.fetch_novel_metadata(TRAPPED_ANILIST)
        check(legacy is not None, "fetch_novel_metadata still returns synopsis metadata")
        for key in ("title", "english_title", "synopsis", "cover_url", "source"):
            check(key in legacy, f"fetch_novel_metadata keeps legacy key {key}")
        for key in ("canonical_title", "romaji_title", "native_title", "synonyms",
                    "anilist_id"):
            check(key in legacy, f"fetch_novel_metadata passes through new key {key}")
    finally:
        metadata_module.httpx = saved["httpx"]
        metadata_module.safe_client_get = saved["safe_client_get"]
        metadata_module.safe_client_request = saved["safe_client_request"]
        NovelMetadataProvider.query_anilist = saved["query_anilist"]
        NovelMetadataProvider.query_kitsu = saved["query_kitsu"]
        NovelMetadataProvider._default_db = saved["default_db"]


# ---------------------------------------------------------------------------
# 15. Owner-B cross-owner regressions: stale alias targets + canonical re-key
# ---------------------------------------------------------------------------
def test_stale_alias_retarget_after_link():
    section("15. link retargets dead alias targets + re-keys canonical-case spellings")
    tmp_dir = _make_temp_dir("stale-alias")
    try:
        db = Database(os.path.join(tmp_dir, "app.db"))
        # The owner-B repro: row A (AniList spelling) gets AniList metadata whose
        # synonym is Calibre name C, creating an override C -> A.
        _insert_tracker(db, TRAPPED_ANILIST, is_monitored=1)
        row_a = db.get_series_by_name(TRAPPED_ANILIST)
        db.store_series_anilist_metadata(
            row_a["id"],
            {
                "english_title": TRAPPED_ANILIST,
                "title": TRAPPED_ANILIST,
                "synonyms": [TRAPPED_CALIBRE],
                "source": "AniList",
            },
        )
        db.add_series_alias_override("Legacy Alias For A", TRAPPED_ANILIST,
                                     origin="anilist")
        overrides = {row["source_norm"]: row for row in db.get_series_alias_overrides()}
        check(normalize_text(TRAPPED_CALIBRE) in overrides,
              "AniList metadata created the C -> A override before the link")
        check_equal(overrides[normalize_text(TRAPPED_CALIBRE)]["target_name"],
                    TRAPPED_ANILIST, "the stale override initially targets row A")

        db.link_series_to_calibre(
            TRAPPED_ANILIST,
            {"series_name": TRAPPED_CALIBRE, "series_id": 327, "book_count": 9},
            actor="scheduler",
            origin="candidate",
        )

        remaining = db.get_series_alias_overrides()
        stale_targets = [
            row for row in remaining
            if normalize_text(row["target_norm"]) == normalize_text(TRAPPED_ANILIST)
        ]
        check_equal(stale_targets, [],
                    "no alias override still targets the merged-away source spelling")
        canonical_override = next(
            (row for row in remaining
             if row["source_norm"] == normalize_text(TRAPPED_CALIBRE)), None
        )
        check(canonical_override is None,
              "no alias override shadows the new canonical spelling itself")
        legacy_override = next(
            (row for row in remaining
             if row["source_norm"] == normalize_text("Legacy Alias For A")), None
        )
        check(legacy_override is not None,
              "overrides that targeted the old spelling are retargeted, not dropped")
        check_equal(legacy_override["target_name"], TRAPPED_CALIBRE,
                    "a retargeted override points at the new canonical name")

        resolution = si.resolve_series_title(
            TRAPPED_CALIBRE, db.get_series_identity_rows(),
            db.get_series_alias_overrides(),
        )
        check_equal(resolution.matched, True,
                    "the canonical Calibre name still resolves after the merge")
        check_equal(resolution.canonical_name, TRAPPED_CALIBRE,
                    "resolution returns the live canonical name, never the dead target")
        check_equal(resolution.calibre_series_id, 327,
                    "resolution carries the live Calibre series id")
        check(resolution.source in ("exact", "alias"),
              "the canonical name resolves to itself (exact or via a live alias)")
        legacy_resolution = si.resolve_series_title(
            "Legacy Alias For A", db.get_series_identity_rows(),
            db.get_series_alias_overrides(),
        )
        check_equal(legacy_resolution.canonical_name, TRAPPED_CALIBRE,
                    "the retargeted legacy alias now resolves to the canonical name")

        # Case/punctuation-equivalent source spellings are canonicalized.
        _insert_tracker(db, TRAPPED_CALIBRE.lower(), is_monitored=1)
        _add_release(db, "case-r1", TRAPPED_CALIBRE.lower(), 1.0, "NEW")
        _add_release(db, "case-r2", TRAPPED_CALIBRE + "!", 2.0, "NEW")
        case_result = db.link_series_to_calibre(
            TRAPPED_CALIBRE.lower(),
            {"series_name": TRAPPED_CALIBRE, "series_id": 328, "book_count": 3},
        )
        check_equal(_release_series_name(db, "case-r1"), TRAPPED_CALIBRE,
                    "a case-equivalent stored spelling is re-keyed to the canonical name")
        check_equal(_release_series_name(db, "case-r2"), TRAPPED_CALIBRE,
                    "a punctuation-equivalent stored spelling is re-keyed too")
        check(case_result["rekeyed_releases"] >= 2,
              "case/punctuation-equivalent re-keys are counted")
    finally:
        _cleanup_temp_dir(tmp_dir)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
TESTS = [
    test_title_candidates_and_cleaning,
    test_trapped_in_a_dating_sim_case,
    test_calibre_series_index_rules,
    test_resolve_series_title_precedence,
    test_migrations_are_guarded_and_idempotent,
    test_alias_override_upsert_rules,
    test_link_series_to_calibre,
    test_match_discovered_series_to_calibre,
    test_auto_link_series_to_calibre,
    test_store_series_anilist_metadata,
    test_update_release_details_stable_key,
    test_discovery_exclusion,
    test_grouped_releases_identity_display_title,
    test_metadata_provider_title_keys,
    test_stale_alias_retarget_after_link,
]


def main():
    global FAILED
    print("=" * 78)
    print("  NovelTrax series identity core tests (owner A)")
    print(f"  interpreter optimize level: {sys.flags.optimize}")
    print("=" * 78)
    for test_fn in TESTS:
        try:
            test_fn()
        except AssertionError:
            # check() already recorded the failure and printed it.
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
    print("ALL SERIES IDENTITY CORE TESTS PASSED")


if __name__ == "__main__":
    main()
