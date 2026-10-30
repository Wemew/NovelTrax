import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), 'backend')))

from app.parser import extract_series_and_volume, normalize_text, match_series_in_calibre

test_titles = [
    "DanMachi Volume 19 Epub",
    "DanMachi – Volume 19 [EPUB]",
    "Associate Professor Akira Takatsuki's Conjecture Volume 7 Epub",
    "From Villainess to Healer Volume 7 Epub",
    "I'm Just a Villager, So What? – Volume 05",
    "Classroom of the Elite: Year 2 Volume 12.5 Epub",
    "Overlord Volume 16 [PDF+EPUB]",
    "Solo Leveling Vol. 8 [Epub]",
    "The Eminence in Shadow v6 Epub",
    "Ascendance of a Bookworm: Part 5 Volume 8",
    "That Time I Got Reincarnated as a Slime, Vol. 21 (light novel)",
    "Sword Art Online 28: Unital Ring VII [Epub]",
    "Dungeon Crawler Carl: Book 7 - This Inevitable Ruin"
]

print("=== TESTING NOVEL TITLE PARSER ===")
for t in test_titles:
    res = extract_series_and_volume(t)
    print(f"Title: {t}")
    print(f"  -> Series:   '{res['series_name']}'")
    print(f"  -> Volume:   {res['volume']}")
    print(f"  -> Subtitle: '{res['subtitle']}'")
    print(f"  -> Conf:     {res['confidence']}")

calibre_series = [
    {"id": 1, "name": "DanMachi", "latest_index": 18.0},
    {"id": 2, "name": "Classroom of the Elite: Year 2", "latest_index": 11.0},
    {"id": 3, "name": "Sword Art Online", "latest_index": 27.0},
    {"id": 4, "name": "Dungeon Crawler Carl", "latest_index": 6.0},
    {"id": 5, "name": "Mushoku Tensei: Jobless Reincarnation", "latest_index": 25.0}
]

print("\n=== TESTING CALIBRE SERIES MATCHING ===")
for test_s in ["DanMachi", "danmachi", "DanMachi!", "Classroom of the Elite: Year 2", "Sword Art Online", "Mushoku Tensei"]:
    m = match_series_in_calibre(test_s, calibre_series)
    print(f"Lookup '{test_s}' -> Matched: {m['name'] if m else 'None'}")
