import re
from typing import Optional, Set, Tuple

def parse_calibre_volumes_and_series_candidate(
    title: str,
    series_name: Optional[str] = None,
    series_index: Optional[float] = None
) -> Tuple[Optional[str], Set[float]]:
    vols: Set[float] = set()
    detected_series: Optional[str] = series_name.strip() if series_name else None

    # 1. Start with Calibre's series_index if present (allow Volume 0.0)
    if series_index is not None and series_index >= 0:
        vols.add(round(float(series_index), 2))

    if not title:
        return detected_series, vols

    # Pre-clean title to detect series name if not explicitly provided
    if not detected_series:
        clean_name = re.sub(
            r"(?i)\s*(?:[-–—:]\s*)?(?:(?:\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*\d+.*|\[.*?\]|\(.*?\))\s*$",
            "",
            title
        ).strip(" -–—:,|.")
        # Trailing bare range check: ensure start <= end to prevent truncating titles like 'Mob Psycho 100 - 1'
        m_bare_trail = re.search(
            r"(?i)(?:\s*[-–—:]\s*|\s+)(?P<s_bare>\d+(?:\.\d+)?)\s*[-–—~to/]+\s*(?P<e_bare>\d+(?:\.\d+)?)\s*$",
            clean_name
        )
        if m_bare_trail:
            s_b = float(m_bare_trail.group("s_bare"))
            e_b = float(m_bare_trail.group("e_bare"))
            if s_b <= e_b:
                clean_name = clean_name[:m_bare_trail.start()].strip(" -–—:,|.")
        if clean_name:
            detected_series = clean_name

    # Prioritize clean_t before raw title to prevent numbers in series names
    # from prematurely matching volume patterns
    candidates = []
    if detected_series:
        clean_t = re.sub(r"^" + re.escape(detected_series), "", title, flags=re.IGNORECASE).strip(" -–—:,|.")
        if clean_t:
            candidates.append(clean_t)
    candidates.append(title)

    # 2. Extract volumes from title into isolated local set
    extracted_vols: Set[float] = set()
    found_range = False

    for c in candidates:
        if not c:
            continue
        # Bracketed ranges like [1-4], [Vol 1-3], (v01-04), [0-2]
        m_bracket = re.search(
            r"[\[\(]\s*(?:(?P<p1>\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)?(?P<start>\d+(?:\.\d+)?)"
            r"\s*[-–—~to/]+\s*"
            r"(?:(?P<p2>\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)?(?P<end>\d+(?:\.\d+)?)\s*[\]\)]",
            c,
            re.IGNORECASE
        )
        if m_bracket:
            p1 = m_bracket.group("p1")
            p2 = m_bracket.group("p2")
            if not p1 and p2:
                # Syntactic guard: title delimiter preceding volume tag inside brackets (e.g. '[100 - Vol 1]')
                pass
            else:
                s_val = float(m_bracket.group("start"))
                e_val = float(m_bracket.group("end"))
                has_marker = bool(p1 or p2)
                if s_val > e_val:
                    if not p1:
                        # Logic guard: reversed ranges require explicit volume prefix
                        pass
                    else:
                        s_val, e_val = e_val, s_val
                if s_val <= e_val and 0 <= s_val <= 2000 and 0 <= e_val <= 2000 and (e_val - s_val) <= 100:
                    if (s_val < 1000 and e_val < 1000) or has_marker:
                        curr = int(s_val)
                        while curr <= int(e_val):
                            if curr >= s_val:
                                extracted_vols.add(float(curr))
                            curr += 1
                        if e_val != int(e_val):
                            extracted_vols.add(round(float(e_val), 2))
                        if s_val != int(s_val):
                            extracted_vols.add(round(float(s_val), 2))
                        found_range = True
                        break

        # Standard range: Vol 1-3, v01-02, Volume 1 to 4, 01-03, Vol 1-2.5, bare 1-4
        # Dual-branch regex ensures p1 is required whenever a prefix is present, or range is purely unadorned
        # Negative lookahead (?!\s*[-–—~to/]+\s*\d) prevents greedy false matching on titles like '86 - 1-4'
        m_range = re.search(
            r"(?i)(?:"
            r"(?P<p1>\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*(?P<start1>\d+(?:\.\d+)?)\s*[-–—~to/]+\s*(?:(?P<p2>\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)?(?P<end1>\d+(?:\.\d+)?)\b"
            r"|"
            r"(?<![-–—/])\b(?P<start2>\d+(?:\.\d+)?)\s*[-–—~to/]+\s*(?P<end2>\d+(?:\.\d+)?)\b(?!\s*[-–—~to/]+\s*\d)"
            r")",
            c
        )
        if m_range:
            p1 = m_range.group("p1")
            p2 = m_range.group("p2")
            s_val = float(m_range.group("start1") or m_range.group("start2"))
            e_val = float(m_range.group("end1") or m_range.group("end2"))
            has_marker = bool(p1 or p2)
            if s_val > e_val:
                # Logic guard: reversed ranges (e.g. Vol 3-1) ONLY allowed when p1 explicitly matched
                if not p1:
                    pass
                else:
                    s_val, e_val = e_val, s_val
            if s_val <= e_val and 0 <= s_val <= 2000 and 0 <= e_val <= 2000 and (e_val - s_val) <= 100:
                if (s_val < 1000 and e_val < 1000) or has_marker:
                    curr = int(s_val)
                    while curr <= int(e_val):
                        if curr >= s_val:
                            extracted_vols.add(float(curr))
                        curr += 1
                    if e_val != int(e_val):
                        extracted_vols.add(round(float(e_val), 2))
                    if s_val != int(s_val):
                        extracted_vols.add(round(float(s_val), 2))
                    found_range = True
                    break

    if not found_range:
        for c in candidates:
            # Pattern list: v1, 2, 3 or Vol 1 + 2 or v1 & v2 or Book 1 & 2
            m_list = re.search(
                r"(?i)(?:(?:\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)"
                r"(?P<list>\d+(?:\.\d+)?(?:\s*[,+&]\s*(?:(?:\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)?\d+(?:\.\d+)?)+)",
                c
            )
            if m_list:
                raw_nums = re.findall(r"\d+(?:\.\d+)?", m_list.group("list"))
                for n in raw_nums:
                    val = round(float(n), 2)
                    if 0 <= val <= 2000:
                        extracted_vols.add(val)
                if extracted_vols:
                    break

            # Single volume pattern: Vol. 23, v05, Volume 12, Book 3, v15, Vol 1.5
            m_single = re.search(
                r"(?i)(?:\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*(\d+(?:\.\d+)?)\b",
                c
            )
            if m_single:
                try:
                    s_num = float(m_single.group(1))
                    if 0 <= s_num <= 2000:
                        extracted_vols.add(round(s_num, 2))
                        break
                except ValueError:
                    pass

    # 3. Discard Calibre default series_index = 1.0 if title explicitly specifies volumes not containing 1.0
    if extracted_vols:
        if series_index == 1.0 and 1.0 not in extracted_vols:
            vols.discard(1.0)
        vols.update(extracted_vols)

    # Fallback title clean if series still undetected
    if not detected_series:
        clean_name = re.sub(
            r"(?i)\s*(?:[-–—:]\s*)?(?:(?:\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*\d+.*|\[.*?\]|\(.*?\))\s*$",
            "",
            title
        ).strip(" -–—:,|.")
        m_bare_trail = re.search(
            r"(?i)(?:\s*[-–—:]\s*|\s+)(?P<s_bare>\d+(?:\.\d+)?)\s*[-–—~to/]+\s*(?P<e_bare>\d+(?:\.\d+)?)\s*$",
            clean_name
        )
        if m_bare_trail:
            s_b = float(m_bare_trail.group("s_bare"))
            e_b = float(m_bare_trail.group("e_bare"))
            if s_b <= e_b:
                clean_name = clean_name[:m_bare_trail.start()].strip(" -–—:,|.")
        if clean_name:
            detected_series = clean_name

    return detected_series, vols

def run_tests():
    test_cases = [
        # Requirement 1
        ("86 - 1-4", None, None, ("86", {1.0, 2.0, 3.0, 4.0})),
        # Requirement 2
        ("86 - 1-4", None, 1.0, ("86", {1.0, 2.0, 3.0, 4.0})),
        # Requirement 3
        ("Mob Psycho 100 - 1-3", None, None, ("Mob Psycho 100", {1.0, 2.0, 3.0})),
        # Requirement 4
        ("Studio 54 - 1-3", None, None, ("Studio 54", {1.0, 2.0, 3.0})),
        # Requirement 5
        ("Apollo 13 - 1-3", None, None, ("Apollo 13", {1.0, 2.0, 3.0})),
        # Requirement 6
        ("86 - Vol 1", None, 1.0, ("86", {1.0})),
        ("Mob Psycho 100 - Vol 1", None, 1.0, ("Mob Psycho 100", {1.0})),
        ("Classroom of the Elite: Year 2 - Vol 1", None, 1.0, ("Classroom of the Elite: Year 2", {1.0})),
        ("Catch-22 - Vol 1", None, 1.0, ("Catch-22", {1.0})),
        ("86 - Eighty Six - Vol 1", None, 1.0, ("86 - Eighty Six", {1.0})),
        ("Slime Taoshite 300-nen Vol 1", None, 1.0, ("Slime Taoshite 300-nen", {1.0})),
        # Additional edge cases
        ("Catch-22", None, None, ("Catch-22", set())),
        ("1984", None, None, ("1984", set())),
        ("The Great Gatsby", None, None, ("The Great Gatsby", set())),
        ("Overlord 1-4", None, None, ("Overlord", {1.0, 2.0, 3.0, 4.0})),
        ("Sword Art Online [1-4]", None, None, ("Sword Art Online", {1.0, 2.0, 3.0, 4.0})),
        ("Series (1998-1999)", None, None, ("Series", set())),
        ("Book Title (Ebook)", None, None, ("Book Title", set())),
        ("Mob Psycho 100 - 1", "Mob Psycho 100", 1.0, ("Mob Psycho 100", {1.0})),
        ("Mob Psycho 100 - 1", None, 1.0, ("Mob Psycho 100 - 1", {1.0})),
        ("86 - 1", "86", 1.0, ("86", {1.0})),
        ("86 - 1", None, 1.0, ("86 - 1", {1.0})),
        ("Series Vol 3-1", None, None, ("Series", {1.0, 2.0, 3.0})),
        ("Series [3-1]", None, None, ("Series", set())),
    ]

    passed = 0
    failed = 0
    for title, s_name, s_idx, expected in test_cases:
        actual = parse_calibre_volumes_and_series_candidate(title, s_name, s_idx)
        if actual == expected:
            print(f"  [PASS] {title} (s={s_name}, i={s_idx}) -> {actual}")
            passed += 1
        else:
            print(f"  [FAIL] {title} (s={s_name}, i={s_idx}) -> Actual: {actual} | Expected: {expected}")
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed.")
    assert failed == 0, f"{failed} tests failed!"

if __name__ == "__main__":
    run_tests()
