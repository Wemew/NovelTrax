#!/usr/bin/env python3
"""Series identity guards: spinoffs, post-volume subtitles and alternate titles."""

import os
import sys
import traceback

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
for _path in (BACKEND_DIR, BASE_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.parser import extract_series_and_volume  # noqa: E402
from app.series_identity import (  # noqa: E402
    CalibreSeriesIndex,
    normalize_text,
    resolve_series_title,
    title_candidates,
)

PARENT = "Tensei Shitara Slime Datta Ken"
SPINOFF = "Trinity in Tempest"
PARENT_EN = "That Time I Got Reincarnated as a Slime"


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def _index(*names):
    return CalibreSeriesIndex([
        {"series_id": index + 1, "series_name": name}
        for index, name in enumerate(names)
    ])


def test_spinoff_suffix_and_parent_fallback():
    print("--> Testing spinoff suffix, parent-only guard and subtitle fallback...")
    both = _index(PARENT, SPINOFF)
    for query in (f"{PARENT}: {SPINOFF}", f"{PARENT} - {SPINOFF}"):
        match = both.match(query)
        check(match is not None and match["series_name"] == SPINOFF, (query, match))
        with_alt = both.match(query, alt_names=[SPINOFF])
        check(with_alt is not None and with_alt["series_name"] == SPINOFF, (query, with_alt))

    parent_only = _index(PARENT)
    check(parent_only.match(f"{PARENT}: {SPINOFF}", alt_names=[SPINOFF]) is None,
          "parent-only Calibre must not capture a spinoff whose suffix is an own alias")
    check(parent_only.match(f"{PARENT}: {SPINOFF}", alt_names=[PARENT_EN, SPINOFF]) is None,
          "a parent spelling in alt_names must not capture an aliased spinoff")
    check(parent_only.match(f"{PARENT} - {SPINOFF}", alt_names=[SPINOFF]) is None,
          "parent-only Calibre must not capture a dash spinoff whose suffix is an own alias")
    check(parent_only.match(f"{PARENT}: {SPINOFF}")["series_name"] == PARENT,
          "without a distinct-title signal, a parent prefix is the safe fallback")

    subtitle = _index("Failure Frame")
    match = subtitle.match(
        "Failure Frame: I Became the Strongest and Annihilated Everything With Low-Level Spells"
    )
    check(match is not None and match["series_name"] == "Failure Frame", match)
    print("    [PASSED] suffix wins; parent-only aliased spinoffs stay unmatched; subtitles fall back.")


def test_dash_and_alternate_ordering():
    print("--> Testing dash preference without reversing symmetric alternates...")
    candidates = title_candidates(f"{PARENT} - {SPINOFF}")
    check(candidates.index(SPINOFF) < candidates.index(PARENT), candidates)
    alt = "The Devil is a Part-Timer!"
    combined = f"Hataraku Maou-sama! | {alt}"
    candidates = title_candidates(combined)
    check(candidates[0] == combined, candidates)
    check(candidates.index("Hataraku Maou-sama!") < candidates.index(alt), candidates)
    check(_index(alt).match(combined)["series_name"] == alt, "pipe alternate must exact-match")
    print("    [PASSED] dash suffix precedes dash prefix; symmetric alternates keep their order.")


def test_tracked_resolution_and_subtitles():
    print("--> Testing tracked-row resolution and post-volume subtitles...")
    rows = [
        {"id": 1, "name": PARENT, "display_title": "", "calibre_series_id": 1},
        {"id": 2, "name": SPINOFF, "display_title": "", "calibre_series_id": 2},
    ]
    for query in (f"{PARENT}: {SPINOFF}", f"{PARENT} - {SPINOFF}"):
        resolved = resolve_series_title(query, rows, [])
        check(resolved.canonical_name == SPINOFF, (query, resolved))
    parent_rows = [{"id": 1, "name": "Failure Frame", "display_title": "", "calibre_series_id": 1}]
    resolved = resolve_series_title(
        "Failure Frame: I Became the Strongest and Annihilated Everything", parent_rows, []
    )
    check(resolved.canonical_name == "Failure Frame", resolved)

    cases = [
        ("Solo Leveling Vol. 1: The Beginning", "Solo Leveling", 1.0),
        ("Solo Leveling v01 - The Beginning", "Solo Leveling", 1.0),
        ("Solo Leveling Vol. 01 (The Beginning)", "Solo Leveling", 1.0),
    ]
    for title, series, volume in cases:
        parsed = extract_series_and_volume(title)
        check(parsed["series_name"] == series, (title, parsed))
        check(parsed["volume"] == volume, (title, parsed))
    print("    [PASSED] tracked spinoffs resolve and volume-marker subtitles stay out of the series key.")


def main():
    print("=" * 78)
    print("Running series mapping guard suite")
    print("=" * 78)
    tests = [
        test_spinoff_suffix_and_parent_fallback,
        test_dash_and_alternate_ordering,
        test_tracked_resolution_and_subtitles,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except Exception:
            failures += 1
            print(f"  [FAILED] {test.__name__}")
            traceback.print_exc()
    print("=" * 78)
    if failures:
        print(f"SERIES MAPPING GUARD SUITE FAILED ({failures} test(s))")
        return 1
    print("ALL SERIES MAPPING GUARD TESTS PASSED!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
