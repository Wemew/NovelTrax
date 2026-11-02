import re
import base64
import os
from typing import Set, Tuple, Optional

def test_parse(filename: str, series_name: Optional[str] = None) -> Tuple[bool, Set[float]]:
    normalized_path = filename.replace('\\', '/')
    base_name = os.path.basename(normalized_path)
    name_no_ext, ext = os.path.splitext(base_name)
    ext_lower = ext.lower()
    novel_exts = ('.epub', '.azw3', '.mobi', '.kfx', '.pdf', '.cbz', '.cbr', '.zip', '.rar', '.7z')
    anc_exts = ('.txt', '.nfo', '.url', '.html', '.htm', '.jpg', '.jpeg', '.png', '.webp', '.sfv', '.lnk', '.md', '.torrent', '.xml', '.ini')
    if ext_lower in anc_exts or ext_lower not in novel_exts:
        return True, set()

    cleaned = name_no_ext
    # Strip release groups before series_name
    cleaned = re.sub(r'^\s*\[[^\]]+\]\s*', '', cleaned)
    cleaned = re.sub(r'^\s*\([^\)]+\)\s*', '', cleaned)
    if series_name:
        cleaned = re.sub(r'^' + re.escape(series_name), '', cleaned, flags=re.IGNORECASE).strip(' -–—:,|._')

    search_targets = [cleaned]
    path_parts = [p for p in normalized_path.split('/')[:-1] if p]
    if path_parts:
        search_targets.append(path_parts[-1])
        search_targets.append(os.path.splitext(normalized_path)[0])

    for target in search_targets:
        t_cleaned = re.sub(r'^\s*\[[^\]]+\]\s*', '', target).strip()
        t_cleaned = re.sub(r'^\s*\([^\)]+\)\s*', '', t_cleaned).strip()
        if series_name:
            t_cleaned = re.sub(r'^' + re.escape(series_name), '', t_cleaned, flags=re.IGNORECASE).strip(' -–—:,|._')

        # 1. Multi-volume omnibus range
        m_range = re.search(
            r'(?i)(?:'
            r'(?P<p1>\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*(?P<start1>\d+(?:\.\d+)?)\s*[-–—~to/]+\s*(?:(?P<p2>\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)?(?P<end1>\d+(?:\.\d+)?)\b'
            r'|'
            r'\[\s*(?P<start2>\d+(?:\.\d+)?)\s*[-–—~to/]+\s*(?P<end2>\d+(?:\.\d+)?)\s*\]'
            r')',
            t_cleaned
        )
        if m_range:
            p1 = m_range.group('p1')
            s_val = float(m_range.group('start1') or m_range.group('start2'))
            e_val = float(m_range.group('end1') or m_range.group('end2'))
            if s_val > e_val:
                if not p1:
                    pass
                else:
                    s_val, e_val = e_val, s_val
            if s_val <= e_val and 0 <= s_val <= 2000 and 0 <= e_val <= 2000 and (e_val - s_val) <= 100:
                vols = set()
                curr = int(s_val)
                while curr <= int(e_val):
                    if curr >= s_val:
                        vols.add(float(curr))
                    curr += 1
                if s_val != int(s_val):
                    vols.add(round(s_val, 2))
                if e_val != int(e_val):
                    vols.add(round(e_val, 2))
                return False, vols

        # 2. Multi-volume list / conjunction: Vol 1 & 2, Vol 1 + 2, Vol 1, 2, 3, Vol 1 and 2
        m_list = re.search(
            r'(?i)(?:(?:\b(?:volumes?|volume|vols?|vol|books?|book|bk|parts?|pt)\b\.?|\bv\.?)\s*)'
            r'(?P<list>\d+(?:\.\d+)?(?:\s*(?:[,+&]|and)\s*(?:(?:\b(?:volumes?|volume|vols?|vol|books?|book|bk|parts?|pt)\b\.?|\bv\.?)\s*)?\d+(?:\.\d+)?)+)',
            t_cleaned
        )
        if m_list:
            raw_nums = re.findall(r'\d+(?:\.\d+)?', m_list.group('list'))
            vols = {round(float(n), 2) for n in raw_nums if 0 <= float(n) <= 2000}
            if vols:
                return False, vols

        # 3. Explicit volume keyword: Vol 01, v08, Vol 4.5
        m_single = re.search(
            r'(?i)(?:^|[\s_\[\(-])(?:(?:\b(?:volumes?|volume|vols?|vol|books?|book|bk|parts?|pt)\b\.?|\bv\.?))\s*#?\s*([0-9]+(?:\.[0-9]+)?)(?:[\s_\]\).-]|$)',
            t_cleaned
        )
        if m_single:
            v_val = float(m_single.group(1))
            if 0 <= v_val <= 2000:
                return False, {round(v_val, 2)}

        # 4. Bare number after delimiter
        m_bare = re.search(
            r'(?i)(?:^|\s+[-–—_]\s+)(\d{1,3}(?:\.\d+)?)(?:\s*\[|\s*\(|\s+[-–—]|\s*$)',
            t_cleaned
        )
        if m_bare:
            b_val = float(m_bare.group(1))
            if 0 <= b_val <= 500 and not (1900 <= b_val <= 2099):
                return False, {round(b_val, 2)}

    return False, set()

if __name__ == "__main__":
    print('86 - Vol 01.epub ->', test_parse('86 - Vol 01.epub'))
    print('Mob Psycho 100 - Vol 02.pdf ->', test_parse('Mob Psycho 100 - Vol 02.pdf'))
    print('Catch-22 - Vol 1-3.epub ->', test_parse('Catch-22 - Vol 1-3.epub'))
    print('[J-Novel Club] Mob Psycho 100 - Vol 02.epub ->', test_parse('[J-Novel Club] Mob Psycho 100 - Vol 02.epub', series_name='Mob Psycho 100'))
    print('[Yen Press] Catch-22 - Vol 1-3.epub ->', test_parse('[Yen Press] Catch-22 - Vol 1-3.epub', series_name='Catch-22'))
    print('Mushoku Tensei Vol 1 & 2.epub ->', test_parse('Mushoku Tensei Vol 1 & 2.epub'))
    print('Re:Zero Vol 1 + 2.epub ->', test_parse('Re:Zero Vol 1 + 2.epub'))
    print('Konosuba Vol 1 and 2.epub ->', test_parse('Konosuba Vol 1 and 2.epub'))
    print('Overlord Vol 1, 2, 3.epub ->', test_parse('Overlord Vol 1, 2, 3.epub'))
    print('Series Vol 1.5-3.epub ->', test_parse('Series Vol 1.5-3.epub'))
