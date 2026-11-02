"""
test_challenger_m2_it4_empiric.py
Adversarial Verification Suite for Milestone 2 Iteration 4
Empirical testing of omnibus parsing, lookahead disqualification, bare range cleaning,
and CalibreScanner integration.
"""

import sys
import os
import sqlite3
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))
from app.calibre.scanner import parse_calibre_volumes_and_series, CalibreScanner


def check(condition: bool, msg: str):
    if not condition:
        raise AssertionError(f"CHALLENGE FAILURE: {msg}")


def test_bare_ranges_numbered_series():
    """Verify bare ranges without series metadata for series containing numbers."""
    cases = [
        ("86 - 1-4", None, None, "86", {1.0, 2.0, 3.0, 4.0}),
        ("86 - 1-4", None, 1.0, "86", {1.0, 2.0, 3.0, 4.0}),
        ("86 1-4", None, None, "86", {1.0, 2.0, 3.0, 4.0}),
        ("86 - 1 to 4", None, None, "86", {1.0, 2.0, 3.0, 4.0}),
        ("86 - 1~4", None, None, "86", {1.0, 2.0, 3.0, 4.0}),
        ("Mob Psycho 100 - 1-3", None, None, "Mob Psycho 100", {1.0, 2.0, 3.0}),
        ("Mob Psycho 100 1-3", None, None, "Mob Psycho 100", {1.0, 2.0, 3.0}),
        ("Studio 54 - 1-3", None, None, "Studio 54", {1.0, 2.0, 3.0}),
        ("Apollo 13 - 1-3", None, None, "Apollo 13", {1.0, 2.0, 3.0}),
        ("Catch-22 - 1-3", None, None, "Catch-22", {1.0, 2.0, 3.0}),
        ("1984 - 1-3", None, None, "1984", {1.0, 2.0, 3.0}),
        ("Cyberpunk 2077 - 1-3", None, None, "Cyberpunk 2077", {1.0, 2.0, 3.0}),
        ("Neon Genesis Evangelion 1995 - 1-3", None, None, "Neon Genesis Evangelion 1995", {1.0, 2.0, 3.0}),
        ("Classroom of the Elite: Year 2 - 1-4", None, None, "Classroom of the Elite: Year 2", {1.0, 2.0, 3.0, 4.0}),
        ("Slime Taoshite 300-nen - 1-3", None, None, "Slime Taoshite 300-nen", {1.0, 2.0, 3.0}),
        ("86 - Eighty Six - 1-4", None, None, "86 - Eighty Six", {1.0, 2.0, 3.0, 4.0}),
    ]
    for title, s_name, s_idx, exp_series, exp_vols in cases:
        series, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        check(series == exp_series, f"Expected series '{exp_series}' for '{title}', got '{series}'")
        check(vols == exp_vols, f"Expected vols {exp_vols} for '{title}', got {vols}")
        print(f"  [PASS] Bare range '{title}' -> Series: '{series}', Vols: {vols}")


def test_numbered_titles_protection():
    """Verify series names with numbers are not truncated when single volume numbers appear."""
    cases = [
        ("86 - Vol 1", None, 1.0, "86", {1.0}),
        ("Mob Psycho 100 - Vol 1", None, 1.0, "Mob Psycho 100", {1.0}),
        ("Mob Psycho 100 - 1", None, 1.0, "Mob Psycho 100 - 1", {1.0}),
        ("Mob Psycho 100 - 50", None, 50.0, "Mob Psycho 100 - 50", {50.0}),
        ("Studio 54 - Vol 2", None, 2.0, "Studio 54", {2.0}),
        ("Catch-22 - Vol 2", None, 2.0, "Catch-22", {2.0}),
        ("86 - Eighty Six - Vol 11", None, 11.0, "86 - Eighty Six", {11.0}),
        ("Classroom of the Elite: Year 2 - Vol 1", None, 1.0, "Classroom of the Elite: Year 2", {1.0}),
        ("Slime Taoshite 300-nen Vol 1", None, 1.0, "Slime Taoshite 300-nen", {1.0}),
    ]
    for title, s_name, s_idx, exp_series, exp_vols in cases:
        series, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        check(series == exp_series, f"Expected series '{exp_series}' for '{title}', got '{series}'")
        check(vols == exp_vols, f"Expected vols {exp_vols} for '{title}', got {vols}")
        print(f"  [PASS] Protection '{title}' -> Series: '{series}', Vols: {vols}")


def test_lookahead_and_bracketed_suffixes():
    """Verify lookahead disambiguation and bracketed/parenthetical metadata suffixes."""
    cases = [
        ("86 - 1-4 [Digital]", None, None, "86", {1.0, 2.0, 3.0, 4.0}),
        ("86 - 1-4 (Light Novel)", None, None, "86", {1.0, 2.0, 3.0, 4.0}),
        ("Mob Psycho 100 - 1-3 [Digital]", None, None, "Mob Psycho 100", {1.0, 2.0, 3.0}),
        ("Mob Psycho 100 - Vol 1-3 (Digital) [Kobo]", None, None, "Mob Psycho 100", {1.0, 2.0, 3.0}),
        ("Apollo 13 - 1-3 [Omnibus]", None, None, "Apollo 13", {1.0, 2.0, 3.0}),
        ("86 - 1-2.5", None, None, "86", {1.0, 2.0, 2.5}),
        ("Series Vol 4-1", None, 1.0, "Series", {1.0, 2.0, 3.0, 4.0}),
        ("Series 4-1", None, 1.0, "Series 4-1", {1.0}),
    ]
    for title, s_name, s_idx, exp_series, exp_vols in cases:
        series, vols = parse_calibre_volumes_and_series(title, s_name, s_idx)
        check(series == exp_series, f"Expected series '{exp_series}' for '{title}', got '{series}'")
        check(vols == exp_vols, f"Expected vols {exp_vols} for '{title}', got {vols}")
        print(f"  [PASS] Lookahead/Suffix '{title}' -> Series: '{series}', Vols: {vols}")


def test_calibre_scanner_mock_db():
    """Verify CalibreScanner reads omnibus bare ranges directly from metadata.db."""
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "metadata.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, series_index REAL)")
        conn.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER)")

        books_data = [
            (1, "86 - 1-4", 1.0),
            (2, "Mob Psycho 100 - 1-3", 1.0),
            (3, "Overlord Vol 1-3", 1.0),
            (4, "Studio 54 - 1-3", 1.0),
            (5, "Mob Psycho 100 - Vol 1", 1.0),
        ]
        for bid, title, s_idx in books_data:
            conn.execute("INSERT INTO books (id, title, series_index) VALUES (?, ?, ?)", (bid, title, s_idx))

        # Add explicit series for Overlord to test linked series
        conn.execute("INSERT INTO series (id, name) VALUES (1, 'Overlord')")
        conn.execute("INSERT INTO books_series_link (id, book, series) VALUES (1, 3, 1)")

        conn.commit()
        conn.close()

        scanner = CalibreScanner(td)
        vmap = scanner.get_series_volumes_map()

        check("86" in vmap, "Key '86' missing in vmap")
        check(vmap["86"] == {1.0, 2.0, 3.0, 4.0}, f"Expected 86 vols {{1,2,3,4}}, got {vmap['86']}")

        check("Mob Psycho 100" in vmap, "Key 'Mob Psycho 100' missing in vmap")
        check(vmap["Mob Psycho 100"] == {1.0, 2.0, 3.0}, f"Expected Mob Psycho 100 vols {{1,2,3}}, got {vmap['Mob Psycho 100']}")

        check("Studio 54" in vmap, "Key 'Studio 54' missing in vmap")
        check(vmap["Studio 54"] == {1.0, 2.0, 3.0}, f"Expected Studio 54 vols {{1,2,3}}, got {vmap['Studio 54']}")

        check("Overlord" in vmap, "Key 'Overlord' missing in vmap")
        check(vmap["Overlord"] == {1.0, 2.0, 3.0}, f"Expected Overlord vols {{1,2,3}}, got {vmap['Overlord']}")

        print("  [PASS] CalibreScanner metadata.db zero-lock volume map verified successfully!")


def main():
    print("=" * 70)
    print("  RUNNING CHALLENGER ITERATION 4 ADVERSARIAL STRESS SUITE")
    print("=" * 70)

    print("\n--- 1. Testing Bare Ranges with Numbered Series ---")
    test_bare_ranges_numbered_series()

    print("\n--- 2. Testing Numbered Titles Protection ---")
    test_numbered_titles_protection()

    print("\n--- 3. Testing Lookahead & Suffix Disambiguation ---")
    test_lookahead_and_bracketed_suffixes()

    print("\n--- 4. Testing CalibreScanner Mock Database Integration ---")
    test_calibre_scanner_mock_db()

    print("\n" + "=" * 70)
    print("CHALLENGER ITERATION 4 ADVERSARIAL SUITE: ALL TESTS PASSED WITH 100% SUCCESS!")
    print("=" * 70)


if __name__ == "__main__":
    main()
