"""Part II release-detection conformance (RELEASE_DETECTION_SPEC §12.8–§12.13).

Covers the acceptance set added in Part II:

* §12.8  — ``<chapters A-B> as v<X>-<Y>`` chapter-range -> volume-range mapping;
* §12.10 — the added conformance vectors (Blue Lock collected release);
* §12.11.3 — multi-group tags (``(1r0n, Ushi)``, ``[1r0n] [Ushi]``, ...);
* §12.12 — composite ``+`` tails (``v01-09 + 047-051``, ``v01-09 + v10-12``);
* §12.13 — publisher vs group, ``(f)/(fN)`` revision markers, prefer-last;
* §13.1 rows 9-12 — the chapter-mapping acceptance rows;
* regression guards for the Part I behaviour that must not drift.

Calibre's interpreter runs with ``-O2`` (``__debug__ is False``), so bare
``assert`` is stripped at compile time.  Every check here goes through
``check()``, which raises ``AssertionError`` explicitly on failure.
"""

import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

from app.parser import (
    FORMAT_TOKENS,
    extract_publisher,
    extract_release_group,
    extract_release_groups,
    extract_series_and_volume,
    is_chapter_release,
    is_valid_group_candidate,
    parse_revision_marker,
)

PASSED = 0
FAILED = 0


def check(cond, msg):
    """Unstrippable assertion: raises explicitly instead of using bare assert."""
    global PASSED, FAILED
    if not cond:
        FAILED += 1
        raise AssertionError(msg)
    PASSED += 1


def section(title):
    print(f"\n--- {title} ---")


def _floats(values):
    return [float(v) for v in values]


def _ranges(values):
    return [[float(pair[0]), float(pair[1])] for pair in values]


def expect_parse(title, series, volumes, is_bundle, is_chapter, chapter_ranges):
    """Assert the core Part II field bundle for one release title."""
    parsed = extract_series_and_volume(title)
    check(
        parsed["series_name"] == series,
        f"{title!r}: series_name expected {series!r}, got {parsed['series_name']!r}",
    )
    check(
        _floats(parsed["volumes"]) == _floats(volumes),
        f"{title!r}: volumes expected {volumes!r}, got {parsed['volumes']!r}",
    )
    check(
        parsed["is_bundle"] is is_bundle,
        f"{title!r}: is_bundle expected {is_bundle!r}, got {parsed['is_bundle']!r}",
    )
    check(
        parsed["is_chapter"] is is_chapter,
        f"{title!r}: is_chapter expected {is_chapter!r}, got {parsed['is_chapter']!r}",
    )
    if chapter_ranges is not None:
        check(
            _ranges(parsed["chapter_ranges"]) == _ranges(chapter_ranges),
            f"{title!r}: chapter_ranges expected {chapter_ranges!r}, got {parsed['chapter_ranges']!r}",
        )
    # Convenience field must always mirror the first range.
    if chapter_ranges:
        check(
            _ranges([parsed["chapter_range"]]) == _ranges([chapter_ranges[0]]),
            f"{title!r}: chapter_range expected {chapter_ranges[0]!r}, got {parsed['chapter_range']!r}",
        )
    else:
        check(parsed["chapter_range"] is None, f"{title!r}: chapter_range must be None")
    # Series identity hygiene: no chapter range, "as" clause or "+" tail residue.
    check(
        not re.search(r"\d{1,4}\s*[-–—~]\s*\d{1,4}", parsed["series_name"]),
        f"{title!r}: series key still contains a numeric range: {parsed['series_name']!r}",
    )
    check(
        "+" not in parsed["series_name"] and not re.search(r"\bas\b", parsed["series_name"]),
        f"{title!r}: series key still contains a mapping/tail residue: {parsed['series_name']!r}",
    )
    return parsed


# --------------------------------------------------------------------------
# §12.8 — chapter-range -> volume-range mapping
# --------------------------------------------------------------------------
def test_12_8_chapter_range_volume_mapping():
    section("§12.8 chapter-range -> volume-range mapping")

    title = (
        "Reading Our Way to a Happy New World 001-009 as v01-02 + 010-012 "
        "(Digital-Compilation) (Oak)"
    )
    parsed = expect_parse(
        title,
        "Reading Our Way to a Happy New World",
        [1.0, 2.0],
        True,
        False,
        [[1.0, 9.0], [10.0, 12.0]],
    )
    # The volume half supplies the volumes; the chapters are never volumes.
    check(4.0 not in parsed["volumes"], "§12.8: chapter 4 leaked into volumes")
    check(9.0 not in parsed["volumes"], "§12.8: chapter 9 leaked into volumes")
    check(parsed["volume"] == 1.0, "§12.8: primary volume must be the start of the v range")
    check(parsed["volume_start"] == 1.0 and parsed["volume_end"] == 2.0,
          "§12.8: volume_start/volume_end must come from the v half")
    # §7.1: the compound format tag must never be the group; the real group is Oak.
    check(extract_release_group(title) == "Oak",
          f"§7.1: group expected 'Oak', got {extract_release_group(title)!r}")
    check(extract_release_groups(title) == ["Oak"],
          f"§7.1: groups expected ['Oak'], got {extract_release_groups(title)!r}")
    check("Digital-Compilation" not in extract_release_groups(title),
          "§7.1: 'Digital-Compilation' must never be a group")
    check(extract_publisher(title) is None, "§7.1: no publisher in the Reading Our Way vector")

    # Minimal mapping form (§13.1 row 10).
    expect_parse("Some Novel 001-009 as v01-02", "Some Novel", [1.0, 2.0], True, False,
                 [[1.0, 9.0]])

    # Single mapped volume (§13.1 row 11): not a bundle.
    expect_parse("Series 010-012 as v03", "Series", [3.0], False, False,
                 [[10.0, 12.0]])

    # All single-volume mapping spellings (§12.12 table).
    for form in ("as v01", "as v1", "as vol 1", "as volume 1"):
        expect_parse(f"Not Quite Love 001-005 {form}", "Not Quite Love", [1.0], False, False,
                     [[1.0, 5.0]])

    # Single mapping + trailing chapter range.
    expect_parse("Not Quite Love 001-005 as v01 + 006-009", "Not Quite Love", [1.0], False,
                 False, [[1.0, 5.0], [6.0, 9.0]])
    # Range mapping + trailing chapter range.
    expect_parse("Not Quite Love 001-005 as v01-02", "Not Quite Love", [1.0, 2.0], True,
                 False, [[1.0, 5.0]])

    # Rule 6: a bare chapter range with no mapping is chapter data, not volumes.
    parsed = expect_parse("Series 001-009", "Series", [], False, True, [[1.0, 9.0]])
    check(parsed["volume"] is None, "§12.8 rule 6: bare chapter range must not invent a volume")

    # The mapping needs BOTH the literal "as" and a volume token: no clause, no volumes.
    parsed = extract_series_and_volume("Reading Our Way to a Happy New World 001-009")
    check(parsed["volumes"] == [], "§12.8 rule 5: mapping fired without 'as v<X>'")
    check(parsed["is_bundle"] is False, "§12.8 rule 5: mapping fired without 'as v<X>' (bundle)")

    # The read-prefix trap control (§13.1 row 12): full title stays intact.
    parsed = extract_series_and_volume("Reading Our Way to a Happy New World")
    check(parsed["series_name"] == "Reading Our Way to a Happy New World",
          f"§13.1 row 12: series mangled to {parsed['series_name']!r}")
    check(parsed["volumes"] == [] and parsed["is_bundle"] is False,
          "§13.1 row 12: plain title must not yield volumes")
    check(parsed["is_chapter"] is False, "§13.1 row 12: plain title must not be a chapter")


# --------------------------------------------------------------------------
# §12.10 — added conformance vectors (Blue Lock collected release)
# --------------------------------------------------------------------------
def test_12_10_added_vectors():
    section("§12.10 added conformance vectors")

    # Paren metadata must not imply light-novel handling; the parser leaves the
    # volume count to metadata inference (§12.9.2 — scheduler side, not the parser).
    parsed = extract_series_and_volume("Blue Lock - Episode Nagi (2024-2026) (Digital) (Stick)")
    check(parsed["series_name"] == "Blue Lock - Episode Nagi",
          f"§12.10: series mangled to {parsed['series_name']!r}")
    check(parsed["volumes"] == [], "§12.10: parser must not invent volumes without markers")
    check(parsed["is_bundle"] is False, "§12.10: no explicit volume markers -> not a bundle")
    check(parsed["is_chapter"] is False, "§12.10: collected release must not be a chapter")
    check(extract_release_groups("Blue Lock - Episode Nagi (2024-2026) (Digital) (Stick)") == ["Stick"],
          "§12.10: paren group 'Stick' must be extracted")

    parsed = extract_series_and_volume("Blue Lock - Episode Nagi [Stick]")
    check(parsed["series_name"] == "Blue Lock - Episode Nagi",
          f"§12.10: bracket group polluted series: {parsed['series_name']!r}")
    check(extract_release_groups("Blue Lock - Episode Nagi [Stick]") == ["Stick"],
          "§12.10: square-bracket 'Stick' group must be extracted")

    parsed = extract_series_and_volume("Blue Lock - Episode Nagi (2024-2026) (Stick)")
    check(parsed["series_name"] == "Blue Lock - Episode Nagi",
          f"§12.10: series mangled to {parsed['series_name']!r}")
    check(extract_release_groups("Blue Lock - Episode Nagi (2024-2026) (Stick)") == ["Stick"],
          "§12.10: group still detected without (Digital)")

    # Explicit markers win: no metadata inference applies.
    expect_parse("Blue Lock - Episode Nagi v01-08 (Digital) (Stick)",
                 "Blue Lock - Episode Nagi", [float(v) for v in range(1, 9)], True, False, [])


# --------------------------------------------------------------------------
# §12.11.3 — multiple release groups in one tag
# --------------------------------------------------------------------------
def test_12_11_3_multi_group_tags():
    section("§12.11.3 multi-group tag vectors")

    base = "Sister and Giant - A Young Lady Is Reborn in Another World v01-06 (2024-2026)"
    six = [float(v) for v in range(1, 7)]

    title = f"{base} (Digital) (1r0n, Ushi)"
    expect_parse(title, "Sister and Giant - A Young Lady Is Reborn in Another World", six, True,
                 False, [])
    check(extract_release_groups(title) == ["1r0n", "Ushi"],
          f"§12.11.3: comma groups expected ['1r0n','Ushi'], got {extract_release_groups(title)!r}")
    check(extract_release_group(title) == "1r0n",
          f"§12.11.3: primary group expected '1r0n', got {extract_release_group(title)!r}")
    check("1r0n, Ushi" not in extract_release_groups(title),
          "§12.11.1: groups must be split, never concatenated")

    title = f"{base} (1r0n & Ushi)"
    check(extract_release_groups(title) == ["1r0n", "Ushi"],
          f"§12.11.3: '&' groups expected ['1r0n','Ushi'], got {extract_release_groups(title)!r}")
    expect_parse(title, "Sister and Giant - A Young Lady Is Reborn in Another World", six, True,
                 False, [])

    title = f"{base} [1r0n] [Ushi]"
    check(extract_release_groups(title) == ["1r0n", "Ushi"],
          f"§12.11.3: separate brackets expected ['1r0n','Ushi'], got {extract_release_groups(title)!r}")
    expect_parse(title, "Sister and Giant - A Young Lady Is Reborn in Another World", six, True,
                 False, [])

    title = f"{base} (Digital) (1r0n)"
    check(extract_release_groups(title) == ["1r0n"],
          f"§12.11.3: (Digital) must not be a group, got {extract_release_groups(title)!r}")
    expect_parse(title, "Sister and Giant - A Young Lady Is Reborn in Another World", six, True,
                 False, [])

    # 'and' and '/' separators split just like ',' and '&' (§12.11.1).
    for separator in (" and ", "/"):
        title = f"{base} (1r0n{separator}Ushi)"
        check(extract_release_groups(title) == ["1r0n", "Ushi"],
              f"§12.11.1: separator {separator!r} expected ['1r0n','Ushi'], "
              f"got {extract_release_groups(title)!r}")

    # Multi-group from the §12.13.3 joint release.
    title = (
        "The Exiled Heavy Knight Knows How to Game the System 001-171 as v01-19 "
        "(Digital-Compilation) (K-MANGA) (Rillant + Aquila + DigitalMangaFan)"
    )
    check(extract_release_groups(title) == ["Rillant", "Aquila", "DigitalMangaFan"],
          f"§12.13.3: groups expected the three '+' parts, got {extract_release_groups(title)!r}")
    check(extract_release_group(title) == "Rillant",
          f"§12.13.3: primary group expected 'Rillant', got {extract_release_group(title)!r}")

    # Known/prioritized matches still outrank first-appearance order.
    ordered = extract_release_groups("[Ushi] [1r0n]", known_groups=["Ushi"])
    check(ordered and ordered[0] == "Ushi",
          f"§12.11: prioritized group must win, got {ordered!r}")


# --------------------------------------------------------------------------
# §12.12 — composite tails
# --------------------------------------------------------------------------
def test_12_12_composite_tails():
    section("§12.12 composite '+' tails")

    title = (
        "The Summer Hikaru Died v01-09 + 047-051 (2023-2026) "
        "(Digital + Digital-Compilation) (1r0n, Rillant) [Completed]"
    )
    parsed = expect_parse(
        title,
        "The Summer Hikaru Died",
        [float(v) for v in range(1, 10)],
        True,
        False,
        [[47.0, 51.0]],
    )
    check(parsed["volume"] == 1.0, "§12.12: primary volume must remain 1")
    check(parsed["is_complete"] is True, "§12.12: [Completed] must set is_complete")
    check(parsed.get("is_complete_series") is True,
          "§12.12: is_complete_series alias must mirror is_complete")
    check("1r0n" in parsed["release_groups"] and "Rillant" in parsed["release_groups"],
          f"§12.12: joint groups lost: {parsed['release_groups']!r}")
    check(parsed["release_group"] == "1r0n",
          f"§12.12: primary release_group expected '1r0n', got {parsed['release_group']!r}")
    # Rule 1: the bare chapter tail is chapter data only, never volumes 47-51.
    check(47.0 not in parsed["volumes"] and 51.0 not in parsed["volumes"],
          f"§12.12 rule 1: chapter tail leaked into volumes: {parsed['volumes']!r}")
    check(parsed["chapter_number"] is None, "§12.12: a range tail must not become chapter_number")
    check(parsed["revision"] is None and parsed["is_rerelease"] is False,
          "§12.13.2: revision must stay None without an (f) marker")
    # Rule 3: a '+' inside a format tag is a format separator, never a group.
    groups = extract_release_groups(title)
    check("Digital" not in groups and "Digital + Digital-Compilation" not in groups,
          f"§12.12 rule 3: format-tag '+' leaked into groups: {groups!r}")
    # ... and it never yields volumes either.
    parsed = extract_series_and_volume("Some Series (Digital + Digital-Compilation)")
    check(parsed["volumes"] == [] and parsed["is_bundle"] is False,
          f"§12.12 rule 3: format-tag '+' produced volumes: {parsed['volumes']!r}")
    check(extract_release_groups("Some Series (Digital + Digital-Compilation)") == [],
          "§12.12 rule 3: format-tag '+' produced a group")

    # Rule 2: an explicit volume continuation EXTENDS the volume set.
    expect_parse(
        "The Summer Hikaru Died v01-09 + v10-12 (2023-2026) (Digital) (1r0n, Rillant)",
        "The Summer Hikaru Died",
        [float(v) for v in range(1, 13)],
        True,
        False,
        [],
    )

    # Rule 4: explicit volume markers outrank a complete/complete-series marker.
    parsed = expect_parse("Some Series v01-03 [Complete]", "Some Series",
                          [1.0, 2.0, 3.0], True, False, [])
    check(parsed["is_complete"] is True, "§12.12 rule 4: [Complete] must set is_complete")
    check(parsed["is_bundle"] is True and parsed["volumes"] == [1.0, 2.0, 3.0],
          "§12.12 rule 4: complete marker must not overwrite parsed volumes")

    # Rule 5: the same tail rule with no volume mapping at all.
    title = "Series 001-005 + 006-009 + 010-012"
    parsed = expect_parse(title, "Series", [], False, True,
                          [[1.0, 5.0], [6.0, 9.0], [10.0, 12.0]])
    check(parsed["subtitle"] == "", "§12.12 rule 5: no range residue may survive as subtitle")
    expect_parse("Not Quite Love 001-005 + 006-009", "Not Quite Love", [], False, True,
                 [[1.0, 5.0], [6.0, 9.0]])


# --------------------------------------------------------------------------
# §12.13 — publisher vs group, revision markers, prefer-last
# --------------------------------------------------------------------------
def test_12_13_publisher_and_revision():
    section("§12.13 publisher vs group, revision markers, prefer-last")

    # §12.13 headline vector: publisher excluded, group extracted.
    title = (
        "The Hero and the Sage, Reincarnated and Engaged 001-039 as v01-08 "
        "(Digital-Compilation) (Square Enix) (DigitalMangaFan)"
    )
    parsed = expect_parse(title, "The Hero and the Sage, Reincarnated and Engaged",
                          [float(v) for v in range(1, 9)], True, False, [[1.0, 39.0]])
    check(parsed["release_groups"] == ["DigitalMangaFan"],
          f"§12.13.1: groups expected ['DigitalMangaFan'], got {parsed['release_groups']!r}")
    check(parsed["publisher"] == "Square Enix",
          f"§12.13.1: publisher expected 'Square Enix', got {parsed['publisher']!r}")

    # §12.13.3: official platform is a publisher, never the group.
    title = (
        "The Exiled Heavy Knight Knows How to Game the System 001-171 as v01-19 "
        "(Digital-Compilation) (K-MANGA) (Rillant + Aquila + DigitalMangaFan)"
    )
    parsed = expect_parse(title, "The Exiled Heavy Knight Knows How to Game the System",
                          [float(v) for v in range(1, 20)], True, False, [[1.0, 171.0]])
    check(parsed["publisher"] == "K-MANGA",
          f"§12.13.3: publisher expected 'K-MANGA', got {parsed['publisher']!r}")
    check("K-MANGA" not in parsed["release_groups"],
          "§12.13.3: K-MANGA must never be a group")

    # Publisher alone must not be returned as a group by the list API (§12.13.1).
    title = "Some Series v01-03 (Square Enix)"
    check(extract_release_groups(title) == [],
          f"§12.13.1: publisher-only tag leaked into groups: {extract_release_groups(title)!r}")
    check(extract_publisher(title) == "Square Enix", "§12.13.1: publisher must be exposed")
    # Back-compat: the legacy single-value API still returns the publisher when
    # there is no real group (existing callers/tests depend on this).
    check(extract_release_group(title) == "Square Enix",
          f"back-compat: legacy group expected 'Square Enix', got {extract_release_group(title)!r}")
    check(extract_release_groups("Some Series v01-03 (DigitalMangaFan)") == ["DigitalMangaFan"],
          "§12.13.1: a real group must still be extracted")

    # A publisher inside a multi-tag block is still recognised as a publisher.
    check(extract_publisher("Some Series (Rillant + K-MANGA)") == "K-MANGA",
          "§12.13.3: publisher inside a multi-tag block must be recognised")

    # §12.13.3 prefer-last: an unrecognised publisher-shaped token in an earlier
    # bracket must not steal the group slot.
    prefer_last = extract_release_groups("Some Series v01-03 (Unknown Publisher Platform) (Rillant)")
    check(prefer_last and prefer_last[0] == "Rillant",
          f"§12.13.3 prefer-last: expected Rillant first, got {prefer_last!r}")

    # Publisher vocabulary (§12.13.3 expansion): case/hyphenation normalised.
    publisher_names = [
        "Yen Press", "J-Novel Club", "Seven Seas", "K-MANGA", "Manga UP",
        "Viz Media", "Shueisha", "BookWalker", "Comikey", "Azuki", "Mangamo",
        "Inkr", "Coolmic",
    ]
    for name in publisher_names:
        tag = f"[{name}] Some Series Vol 1"
        check(extract_publisher(tag) == name,
              f"publisher vocabulary: expected {name!r} from {tag!r}, got {extract_publisher(tag)!r}")
        check(is_valid_group_candidate(name),
              f"publisher vocabulary: {name!r} must still pass candidate validity")
        check(name not in extract_release_groups(tag),
              f"publisher vocabulary: {name!r} must never be returned as a group")

    # §12.13.2 revision markers: (f) -> 1, (fN) -> N; never a group; no other effect.
    revisions = {"(f)": 1, "(f2)": 2, "(f10)": 10, "(F)": 1}
    for marker, expected in revisions.items():
        marker_result = parse_revision_marker(f"Some Series Vol 1 {marker}")
        check(marker_result["revision"] == expected,
              f"§12.13.2: {marker} expected revision {expected}, got {marker_result!r}")
        check(marker_result["is_rerelease"] is True,
              f"§12.13.2: {marker} must be flagged is_rerelease")
        check(is_valid_group_candidate(marker.strip("()[]")) is False,
              f"§12.13.2: {marker} must never pass group-candidate validity")
        check(extract_release_groups(f"Some Series Vol 1 {marker}") == [],
              f"§12.13.2: {marker} leaked into groups: {extract_release_groups('Some Series Vol 1 ' + marker)!r}")
        check(extract_release_group(f"Some Series Vol 1 {marker}") is None,
              f"§12.13.2: legacy group must ignore {marker}")
        parsed = extract_series_and_volume(f"Some Series Vol 1 {marker}")
        check(parsed["series_name"] == "Some Series",
              f"§12.13.2: {marker} altered the series: {parsed['series_name']!r}")
        check(_floats(parsed["volumes"]) == [1.0],
              f"§12.13.2: {marker} altered the volumes: {parsed['volumes']!r}")
        check(parsed["revision"] == expected and parsed["is_rerelease"] is True,
              f"§12.13.2: {marker} not exposed on the parse result: {parsed['revision']!r}")

    no_marker = parse_revision_marker("Some Series Vol 1")
    check(no_marker == {"revision": None, "is_rerelease": False},
          f"§12.13.2: missing marker expected None/False, got {no_marker!r}")

    # Full publisher + group chain with a revision marker: only the group survives.
    title = "Some Series v01-03 (Digital-Compilation) (Square Enix) (DigitalMangaFan) (f2)"
    parsed = extract_series_and_volume(title)
    check(parsed["release_groups"] == ["DigitalMangaFan"],
          f"§12.13.2: groups expected ['DigitalMangaFan'], got {parsed['release_groups']!r}")
    check(parsed["publisher"] == "Square Enix", "§12.13.2: publisher lost in full chain")
    check(parsed["revision"] == 2 and parsed["is_rerelease"] is True,
          f"§12.13.2: revision expected 2, got {parsed['revision']!r}")

    # §7.1 vocabulary gap: compound format/edition tags never pass validity.
    check("compilation" in FORMAT_TOKENS, "§7.1: FORMAT_TOKENS must contain 'compilation'")
    for compound in ("Digital-Compilation", "Batched", "Complete-Collection", "Compilation",
                     "Digital + Digital-Compilation", "Complete Collection"):
        check(not is_valid_group_candidate(compound),
              f"§7.1: compound format tag {compound!r} must be rejected as a group")
        check(extract_release_groups(f"[{compound}] Some Series Vol 1") == [],
              f"§7.1: {compound!r} leaked into groups")


# --------------------------------------------------------------------------
# §13.1 rows 9-12 — the chapter-mapping acceptance set
# --------------------------------------------------------------------------
def test_13_1_acceptance_rows():
    section("§13.1 rows 9-12 acceptance set")

    # Row 9: full chapter-mapping vector; +010-012 is chapter data; group is Oak.
    title = ("Reading Our Way to a Happy New World 001-009 as v01-02 + 010-012 "
             "(Digital-Compilation) (Oak)")
    expect_parse(title, "Reading Our Way to a Happy New World", [1.0, 2.0], True, False,
                 [[1.0, 9.0], [10.0, 12.0]])
    check(extract_release_group(title) == "Oak", "§13.1 row 9: group must be Oak")
    check(is_chapter_release(title) is False,
          "§13.1 row 9: a mapped release yields volumes and is not a chapter release")

    # Row 10: minimal mapping form.
    title = "Some Novel 001-009 as v01-02"
    expect_parse(title, "Some Novel", [1.0, 2.0], True, False, [[1.0, 9.0]])
    check(is_chapter_release(title) is False, "§13.1 row 10: mapped release is not a chapter")

    # Row 11: single mapped volume.
    title = "Series 010-012 as v03"
    expect_parse(title, "Series", [3.0], False, False, [[10.0, 12.0]])

    # Row 12: read-prefix control — full title preserved, nothing inferred.
    title = "Reading Our Way to a Happy New World"
    parsed = expect_parse(title, title, [], False, False, [])
    check(parsed["volume"] is None, "§13.1 row 12: plain title must not yield a volume")
    check(is_chapter_release(title) is False, "§13.1 row 12: plain title is not a chapter")


# --------------------------------------------------------------------------
# §12.2 traps 2/3 — chapter data is first-class and the series key stays clean
# --------------------------------------------------------------------------
def test_chapter_data_traps():
    section("§12.2 traps: chapter data exposed, series key clean")

    for title, expected_series, expected_number in (
        ("Dandadan 250", "Dandadan", 250.0),
        ("Dandadan 250.5", "Dandadan", 250.5),
        # Trap 1: trailing parenthesised feed metadata must not defeat the flag.
        ("Dandadan 250 (Digital) (1r0n)", "Dandadan", 250.0),
        ("Dandadan 250 (2024-2026) (Digital) (Stick)", "Dandadan", 250.0),
        ("Dandadan Ch. 250", "Dandadan", 250.0),
        ("Chainsaw Man 180", "Chainsaw Man", 180.0),
        ("Solo Leveling Ch. 179", "Solo Leveling", 179.0),
        ("Berserk c374", "Berserk", 374.0),
    ):
        parsed = extract_series_and_volume(title)
        # Trap 2: the series key must be the clean series, never "Series 250".
        check(parsed["series_name"] == expected_series,
              f"{title!r}: clean series expected {expected_series!r}, got {parsed['series_name']!r}")
        check(parsed["is_chapter"] is True,
              f"{title!r}: is_chapter must be True")
        check(parsed["chapter_number"] == expected_number,
              f"{title!r}: chapter_number expected {expected_number}, got {parsed['chapter_number']!r}")
        check(isinstance(parsed["chapter_ranges"], list),
              f"{title!r}: chapter_ranges must always be a list")
        check(is_chapter_release(title) is True,
              f"{title!r}: is_chapter_release must be True")

    # An explicit volume keyword is never a chapter.
    parsed = extract_series_and_volume("Dandadan Vol. 12")
    check(parsed["is_chapter"] is False and parsed["volume"] == 12.0,
          f"'Dandadan Vol. 12' must stay a volume: {parsed['volume']!r}")

    # Pattern 3 hybrid: volume AND chapter are both captured.
    parsed = extract_series_and_volume("Dandadan - Volume 3 Chapter 12")
    check(parsed["volume"] == 3.0, f"hybrid: volume expected 3, got {parsed['volume']!r}")
    check(parsed["chapter_number"] == 12.0,
          f"hybrid: chapter_number expected 12, got {parsed['chapter_number']!r}")
    check(parsed["is_chapter"] is False, "hybrid: a volume+chapter release is not chapter-only")


# --------------------------------------------------------------------------
# Regression guards — Part I behaviour that must not drift
# --------------------------------------------------------------------------
def test_regressions():
    section("Regression guards (Part I behaviour)")

    expect_parse("DanMachi Vols 01-18", "DanMachi",
                 [float(v) for v in range(1, 19)], True, False, [])
    expect_parse("86 - 1-4", "86", [1.0, 2.0, 3.0, 4.0], True, False, [])
    expect_parse("Mob Psycho 100 - 1-3", "Mob Psycho 100", [1.0, 2.0, 3.0], True, False, [])
    expect_parse("DanMachi Omnibus 1 (Vols 1-3)", "DanMachi Omnibus", [1.0, 2.0, 3.0], True,
                 False, [])
    expect_parse("Overlord Vol 1, 2, and 3", "Overlord", [1.0, 2.0, 3.0], True, False, [])
    expect_parse("DanMachi Volume 19", "DanMachi", [19.0], False, False, [])
    expect_parse("Overlord Vol. 16", "Overlord", [16.0], False, False, [])

    # Publisher tags are preserved by the legacy single-value API (VIP matching),
    # exposed by extract_publisher, and never returned by the list API.
    for name in ("J-Novel Club", "Yen Press", "Seven Seas", "One Peace Books"):
        title = f"[{name}] Some Series Vol 1"
        check(extract_release_group(title) == name,
              f"regression: legacy group expected {name!r}, got {extract_release_group(title)!r}")
        check(extract_publisher(title) == name,
              f"regression: publisher expected {name!r}, got {extract_publisher(title)!r}")
        check(name not in extract_release_groups(title),
              f"regression: {name!r} must not be a group in the list API")
    check(extract_release_group("[Seven Seas] [Stick] Mushoku Tensei v01") == "Stick",
          "regression: ripper group must outrank a publisher")
    check(extract_release_groups("[Seven Seas] [Stick] Mushoku Tensei v01") == ["Stick"],
          "regression: publisher must be filtered from the group list")
    check(extract_release_group("Ore ga Ojousama v01-15 [Completed]") is None,
          "regression: '[Completed]' must never be a group")
    check(extract_release_group("DanMachi Vol. 19-Stick (Digital) [EPUB]") == "Stick",
          "regression: scene-style '-Stick' suffix must still be extracted")
    check(is_chapter_release("Mob Psycho 100") is True,
          "regression: 'Mob Psycho 100' stays a diagnostic chapter (context guard decides)")
    check(extract_release_group("[Yen Press] Overlord, Vol. 16 [Digital] [EPUB]") == "Yen Press",
          "regression: [Yen Press] single publisher must stay backward compatible")


def main():
    print("=" * 74)
    print("  NovelTrax Part II release-mapping conformance (spec §12.8-§12.13)")
    print("=" * 74)
    try:
        test_12_8_chapter_range_volume_mapping()
        test_12_10_added_vectors()
        test_12_11_3_multi_group_tags()
        test_12_12_composite_tails()
        test_12_13_publisher_and_revision()
        test_13_1_acceptance_rows()
        test_chapter_data_traps()
        test_regressions()
    except AssertionError as exc:
        print("\n" + "=" * 74)
        print(f"  FAILED: {exc}")
        print(f"  {PASSED} checks passed, {FAILED} failed")
        print("=" * 74)
        raise SystemExit(1)

    print("\n" + "=" * 74)
    print(f"  {PASSED} checks passed, {FAILED} failed")
    print("=" * 74)
    if FAILED:
        raise SystemExit(1)
    print("ALL RELEASE-MAPPING CONFORMANCE TESTS PASSED")


if __name__ == "__main__":
    main()
