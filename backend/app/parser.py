import re
import unicodedata
from typing import Optional, Tuple, Dict, Any, List
from difflib import SequenceMatcher

def normalize_text(text: str) -> str:
    """Normalize text for consistent comparison: lowercase, unicode NFKD, strip punctuation."""
    if not text:
        return ""
    # Normalize unicode (accents, full-width characters)
    normalized = unicodedata.normalize("NFKD", text)
    # Convert to lowercase
    lowered = normalized.lower()
    # Replace dashes, colons, underscores with spaces
    replaced = re.sub(r"[-–—:_/\\]+", " ", lowered)
    # Remove remaining punctuation except alphanumeric and space
    cleaned = re.sub(r"[^\w\s]", "", replaced)
    # Collapse whitespace
    return " ".join(cleaned.split())

def string_similarity(a: str, b: str) -> float:
    """Calculate similarity ratio between two strings (0.0 to 1.0)."""
    norm_a = normalize_text(a)
    norm_b = normalize_text(b)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0
    return SequenceMatcher(None, norm_a, norm_b).ratio()

def is_pdf_release(raw_title: Optional[str] = None, link: Optional[str] = None, guid: Optional[str] = None) -> bool:
    """
    Determines if a release is a PDF release based on title, URL, or guid.
    NovelTrax tracks digital novels/EPUBs and strictly ignores PDF releases.
    """
    text_to_check = f"{raw_title or ''} {link or ''} {guid or ''}".lower()
    if not text_to_check.strip():
        return False
    # Check if ends with .pdf or has .pdf in extension
    if re.search(r'\.pdf(?:\?|#|$)', text_to_check):
        return True
    # Check if contains /pdf/ or format=pdf in url
    if link and ('/pdf/' in link.lower() or 'format=pdf' in link.lower()):
        return True
    # Check for PDF tag in parens or brackets: (PDF), [PDF], (PDF+EPUB), etc.
    if raw_title and re.search(r'[\(\[]\s*pdf\s*[\)\]]', raw_title, re.I):
        return True
    # Check for standalone word "pdf"
    if raw_title and re.search(r'\bpdf\b', raw_title, re.I):
        return True
    return False

def should_merge_series(name_a: str, name_b: str) -> bool:
    """
    Determines if two series names refer to the same underlying series
    (e.g., '86' and '86-Eighty Six', or 'Tenkyou no Alderamin' and 'Tenkyou no Alderamin (Alderamin On The Sky)').
    """
    if not name_a or not name_b:
        return False
    norm_a = normalize_text(name_a)
    norm_b = normalize_text(name_b)
    if not norm_a or not norm_b:
        return False
    if norm_a == norm_b:
        return True

    # Check high string similarity
    if string_similarity(norm_a, norm_b) >= 0.85:
        return True

    # Check word token subset / containment (e.g. '86' and '86 eighty six')
    tokens_a = set(norm_a.split())
    tokens_b = set(norm_b.split())

    stopwords = {"the", "a", "an", "in", "to", "of", "and", "is", "no", "on", "for"}
    sig_a = tokens_a - stopwords
    sig_b = tokens_b - stopwords

    if sig_a and sig_b:
        # If the significant words of one is entirely contained in the other
        if sig_a.issubset(sig_b) or sig_b.issubset(sig_a):
            # Ensure at least one token is distinctive (digits or length >= 2)
            shorter = sig_a if len(sig_a) <= len(sig_b) else sig_b
            if any(t.isdigit() or len(t) >= 2 for t in shorter):
                return True

    return False

def clean_feed_title(raw_title: str) -> str:
    """Strip web novel / download tags like [EPUB], (PDF), Free Download, etc. while preserving volume markers."""
    if not raw_title:
        return ""
    
    cleaned = raw_title.strip()

    # Strip leading download / site prefixes
    cleaned = re.sub(r'^(?:\s*download|\s*free\s+download|\s*read|\s*get)\s*[:\-–—]?\s*', '', cleaned, flags=re.IGNORECASE)

    # Strip file extensions if present
    cleaned = re.sub(r"\.(?:epub|pdf|mobi|azw3|zip|rar)$", "", cleaned, flags=re.IGNORECASE)
    
    # Remove format/type words enclosed in parens or brackets: (Light Novel), [Light Novel], etc.
    cleaned = re.sub(r'[\(\[]\s*(?:light\s+novel|web\s+novel|novel|ln|wn|epub|pdf|mobi|azw3)\s*[\)\]]', ' ', cleaned, flags=re.IGNORECASE)
    
    # Remove square brackets and parentheses tags, but preserve volume or volume range tags like [Vol 1-14], (v01-v07)
    def bracket_replacer(match):
        inner = match.group(0)
        unbracketed = inner[1:-1].strip()
        # If it's a year or year range like (2020), (2020-2022)
        if re.match(r"^(?:19|20)\d{2}(?:\s*[-–—~to]+\s*(?:19|20)\d{2})?$", unbracketed):
            return " "
        has_vol = re.search(r"(?:vol\.?|volume|v|books?|part|pt\.?)\s*\d+", unbracketed, re.IGNORECASE)
        has_range = re.search(r"\b\d+\s*[-–—~to]+\s*\d+\b", unbracketed, re.IGNORECASE)
        if has_vol or (has_range and not re.search(r"(?:19|20)\d{2}", unbracketed)):
            return f" {unbracketed} "
        return " "

    cleaned = re.sub(r"\[.*?\]", bracket_replacer, cleaned)
    cleaned = re.sub(r"\(.*?\)", bracket_replacer, cleaned)
    
    # Remove common format & site buzzwords (case-insensitive words)
    buzzwords = [
        r"\bepub\b", r"\bpdf\b", r"\bmobi\b", r"\bazw3\b",
        r"\bdownload\b", r"\bfree download\b",
        r"\blight novel\b", r"\bweb novel\b", r"\bln\b", r"\bwn\b",
        r"\braw\b", r"\btranslated\b", r"\benglish\b", r"\beng\b",
        r"\bscan\b", r"\bdigital\b", r"\bretail\b"
    ]
    for bw in buzzwords:
        cleaned = re.sub(bw, " ", cleaned, flags=re.IGNORECASE)

    # Strip bundle suffixes like 'all volumes', 'complete series'
    cleaned = re.sub(r'\b(?:all\s+volumes?|complete\s+(?:series|collection|set)?|box\s*set)\b', ' ', cleaned, flags=re.IGNORECASE)
    
    # Clean leading and trailing punctuation & spaces
    cleaned = re.sub(r"^[\s\-–—:;,|]+|[\s\-–—:;,|]+$", "", cleaned)
    return " ".join(cleaned.split())

def extract_series_and_volume(raw_title: str) -> Dict[str, Any]:
    """
    Intelligently extracts series name, volume(s), bundle ranges, and optional subtitle
    from raw release titles commonly found on J-Novels, NovelUpdates, RSS feeds, etc.
    """
    cleaned = clean_feed_title(raw_title)
    
    # Result defaults
    result = {
        "raw_title": raw_title,
        "clean_title": cleaned,
        "series_name": cleaned,
        "volume": None,
        "is_bundle": False,
        "volume_start": None,
        "volume_end": None,
        "volumes": [],
        "subtitle": "",
        "confidence": 0.0
    }
    
    if not cleaned:
        return result

    # Pattern 0a: Multi-volume bundle range:
    # e.g., "Overlord Vol 1-14", "Classroom of the Elite Volume 1-11", "v01-v07", "Vol 1 to 5", "Vol. 01~10"
    bundle_pattern = re.compile(
        r"^(?P<series>.+?)\s*(?:[-–—:,]\s*)?"
        r"(?:\b(?:volumes|volume|vol|books|book)\b\.?|\bv\.?)\s*(?P<start>\d+(?:\.\d+)?)"
        r"\s*[-–—~to]+\s*"
        r"(?:(?:volumes|volume|vol|books|book)\b\.?|\bv\.?)?\s*(?P<end>\d+(?:\.\d+)?)"
        r"(?:\s*[:\-–—]\s*(?P<subtitle>.*))?$",
        re.IGNORECASE
    )
    m_bundle = bundle_pattern.match(cleaned)
    if m_bundle:
        series = m_bundle.group("series").strip(" -–—:,|")
        series = re.sub(r"(?i)\b(?:volumes|volume|vol\.?|books?)\b$", "", series).strip(" -–—:,|")
        subtitle = (m_bundle.group("subtitle") or "").strip(" -–—:,|")
        try:
            start_vol = float(m_bundle.group("start"))
            end_vol = float(m_bundle.group("end"))
            if start_vol > end_vol:
                start_vol, end_vol = end_vol, start_vol

            vols = []
            curr = int(start_vol)
            while curr <= int(end_vol):
                vols.append(float(curr))
                curr += 1
            if not vols:
                vols = [start_vol, end_vol]

            clean_series = re.sub(r'(?i)\s*\(?\b(light\s+novel|web\s+novel|novel|ln|wn)\b\)?', '', series).strip(" -–—:,|")
            return {
                "raw_title": raw_title,
                "clean_title": cleaned,
                "series_name": clean_series,
                "volume": start_vol,
                "is_bundle": True,
                "volume_start": start_vol,
                "volume_end": end_vol,
                "volumes": vols,
                "subtitle": subtitle,
                "confidence": 0.95
            }
        except ValueError:
            pass

    # Pattern 0b: Comma or plus separated bundle
    # e.g., "Monogatari Series Vol 1, 2, 3", "Series Vol 1 + 2 + 3"
    list_bundle_pattern = re.compile(
        r"^(?P<series>.+?)\s*(?:[-–—:,]\s*)?"
        r"(?:\b(?:volumes|volume|vol|books?)\b\.?|\bv\.?)\s*(?P<vols_str>\d+(?:\s*[,+&]\s*(?:(?:volumes|volume|vol|books?)\b\.?|\bv\.?)?\s*\d+)+)"
        r"(?:\s*[:\-–—]\s*(?P<subtitle>.*))?$",
        re.IGNORECASE
    )
    m_list = list_bundle_pattern.match(cleaned)
    if m_list:
        series = m_list.group("series").strip(" -–—:,|")
        series = re.sub(r"(?i)\b(?:volumes|volume|vol\.?|books?)\b$", "", series).strip(" -–—:,|")
        raw_nums = re.findall(r"\d+", m_list.group("vols_str"))
        if len(raw_nums) >= 2:
            parsed_vols = sorted(list(set(float(n) for n in raw_nums)))
            clean_series = re.sub(r'(?i)\s*\(?\b(light\s+novel|web\s+novel|novel|ln|wn)\b\)?', '', series).strip(" -–—:,|")
            return {
                "raw_title": raw_title,
                "clean_title": cleaned,
                "series_name": clean_series,
                "volume": parsed_vols[0],
                "is_bundle": True,
                "volume_start": parsed_vols[0],
                "volume_end": parsed_vols[-1],
                "volumes": parsed_vols,
                "subtitle": (m_list.group("subtitle") or "").strip(" -–—:,|"),
                "confidence": 0.95
            }

    # Pattern 1: Explicit volume/book/part keywords:
    # e.g., "DanMachi Volume 19", "Overlord Vol. 16", "The Eminence in Shadow v6", "Dungeon Crawler Carl Book 7: Subtitle", "Avatar Korra Turf Wars Part 1"
    # Matches: Series + [delimiter] + (Volume|Vol|v|Book|Part|Pt) + Number + [optional subtitle]
    p1 = re.compile(
        r"^(?P<series>.+?)\s*(?:[-–—:,]\s*)?"
        r"(?:\b(?:volume|vol|book|part|pt)\b\.?|\bv\.?)(?:\s*|\.)(?P<vol>\d+(?:\.\d+)?)"
        r"(?:\s*[:\-–—]\s*(?P<subtitle>.*))?$",
        re.IGNORECASE
    )
    m1 = p1.match(cleaned)
    if m1:
        series = m1.group("series").strip(" -–—:,|")
        series = re.sub(r"(?i)\b(?:volume|vol\.?|book|part|pt\.?)\b$", "", series).strip(" -–—:,|")
        series = re.sub(r'^(?:\s*download|\s*free\s+download|\s*read|\s*get)\s*[:\-–—]?\s*', '', series, flags=re.IGNORECASE).strip(" -–—:,|")
        vol_str = m1.group("vol")
        subtitle = (m1.group("subtitle") or "").strip(" -–—:,|")
        try:
            vol = float(vol_str)
            clean_series = re.sub(r'(?i)\s*\(?\b(light\s+novel|web\s+novel|novel|ln|wn)\b\)?', '', series).strip(" -–—:,|")
            return {
                "raw_title": raw_title,
                "clean_title": cleaned,
                "series_name": clean_series,
                "volume": vol,
                "is_bundle": False,
                "volume_start": vol,
                "volume_end": vol,
                "volumes": [vol],
                "subtitle": subtitle,
                "confidence": 0.95
            }
        except ValueError:
            pass

    # Pattern 2: Series followed by volume number directly separated by space or hyphen:
    # e.g., "Sword Art Online 28: Unital Ring VII", "Re:Zero 25"
    p2 = re.compile(
        r"^(?P<series>[A-Za-z0-9\s\'\"?!,\.]+?)\s+[-–—]?\s*(?P<vol>\d+(?:\.\d+)?)"
        r"(?:\s*[:\-–—]\s*(?P<subtitle>.*))?$",
        re.IGNORECASE
    )
    m2 = p2.match(cleaned)
    if m2:
        series = m2.group("series").strip(" -–—:,|")
        series = re.sub(r'^(?:\s*download|\s*free\s+download|\s*read|\s*get)\s*[:\-–—]?\s*', '', series, flags=re.IGNORECASE).strip(" -–—:,|")
        vol_str = m2.group("vol")
        subtitle = (m2.group("subtitle") or "").strip(" -–—:,|")
        try:
            vol = float(vol_str)
            if not (1900 <= vol <= 2099):
                clean_series = re.sub(r'(?i)\s*\(?\b(light\s+novel|web\s+novel|novel|ln|wn)\b\)?', '', series).strip(" -–—:,|")
                return {
                    "raw_title": raw_title,
                    "clean_title": cleaned,
                    "series_name": clean_series,
                    "volume": vol,
                    "is_bundle": False,
                    "volume_start": vol,
                    "volume_end": vol,
                    "volumes": [vol],
                    "subtitle": subtitle,
                    "confidence": 0.80
                }
        except ValueError:
            pass

    # Pattern 3: Chapter / Volume hybrid: "Series Name - Volume 3 Chapter 12"
    p3 = re.compile(
        r"^(?P<series>.+?)\s*[-–—:]\s*(?:Volume|Vol\.?|v|Book|Part)\s*(?P<vol>\d+(?:\.\d+)?)"
        r"(?:\s*(?:Chapter|c)\s*(?P<chap>\d+(?:\.\d+)?))?",
        re.IGNORECASE
    )
    m3 = p3.match(cleaned)
    if m3:
        series = m3.group("series").strip(" -–—:,|")
        series = re.sub(r'^(?:\s*download|\s*free\s+download|\s*read|\s*get)\s*[:\-–—]?\s*', '', series, flags=re.IGNORECASE).strip(" -–—:,|")
        vol_str = m3.group("vol")
        try:
            vol = float(vol_str)
            clean_series = re.sub(r'(?i)\s*\(?\b(light\s+novel|web\s+novel|novel|ln|wn)\b\)?', '', series).strip(" -–—:,|")
            return {
                "raw_title": raw_title,
                "clean_title": cleaned,
                "series_name": clean_series,
                "volume": vol,
                "is_bundle": False,
                "volume_start": vol,
                "volume_end": vol,
                "volumes": [vol],
                "subtitle": "",
                "confidence": 0.85
            }
        except ValueError:
            pass

    # Check if raw_title is an 'all volumes' / bundle release
    if re.search(r'(?i)\b(?:all\s+volumes?|complete\s+(?:series|collection)?|box\s*set)\b', raw_title):
        result["is_bundle"] = True
        if result["volume"] is None:
            result["volume"] = 1.0
            result["volume_start"] = 1.0

    # Fallback: if volume is still None and not a bundle, search for explicit volume keyword anywhere in raw_title
    if result["volume"] is None and not result["is_bundle"]:
        m_fallback = re.search(r'(?i)\b(?:volume|vol|book|part|pt)\b\.?\s*(\d+(?:\.\d+)?)', raw_title)
        if m_fallback:
            try:
                v = float(m_fallback.group(1))
                result["volume"] = v
                result["volume_start"] = v
                result["volume_end"] = v
                result["volumes"] = [v]
                result["confidence"] = 0.85
                extracted_prefix = re.split(r'(?i)\b(?:volume|vol|book|part|pt)\b', result["series_name"])[0]
                if extracted_prefix.strip(" -–—:,|"):
                    result["series_name"] = extracted_prefix.strip(" -–—:,|")
            except ValueError:
                pass

    # Ensure series_name and clean_title have no dangling light novel / novel / download / all volumes keywords
    clean_series = result["series_name"]
    clean_series = re.sub(r'^(?:\s*download|\s*free\s+download|\s*read|\s*get)\s*[:\-–—]?\s*', '', clean_series, flags=re.IGNORECASE)
    clean_series = re.sub(r'(?i)\b(?:all\s+volumes?|complete\s+(?:series|collection|set)?|box\s*set)\b', '', clean_series)
    clean_series = re.sub(r'(?i)\s*\(?\b(light\s+novel|web\s+novel|novel|ln|wn|epub|pdf)\b\)?', '', clean_series)
    clean_series = re.sub(r'(?i)\s*\[?\b(light\s+novel|web\s+novel|novel|ln|wn|epub|pdf)\b\]?', '', clean_series)
    clean_series = re.sub(r"(?i)\b(?:volume|vol\.?|book|part|pt\.?)\b$", "", clean_series).strip(" -–—:,|")
    result["series_name"] = clean_series
    result["clean_title"] = re.sub(r'(?i)\s*\(?\b(light\s+novel|web\s+novel|novel|ln|wn|epub|pdf)\b\)?', '', result["clean_title"]).strip(" -–—:,|")
    return result

def match_series_in_calibre(extracted_series: str, calibre_series_list: list, threshold: float = 0.80) -> Optional[dict]:
    """
    Matches an extracted series name against Calibre's series list.
    calibre_series_list can have 'name' or 'series_name'.
    Returns the best matching dict or None.
    """
    if not extracted_series or not calibre_series_list:
        return None
    
    norm_extracted = normalize_text(extracted_series)
    
    # 1. Exact match (normalized)
    for c_ser in calibre_series_list:
        s_name = c_ser.get("series_name") or c_ser.get("name", "")
        if normalize_text(s_name) == norm_extracted:
            return c_ser
            
    # 2. Substring containment match (e.g. "Mushoku Tensei" in "Mushoku Tensei: Jobless Reincarnation")
    for c_ser in calibre_series_list:
        s_name = c_ser.get("series_name") or c_ser.get("name", "")
        norm_c = normalize_text(s_name)
        if len(norm_c) >= 5 and len(norm_extracted) >= 5:
            if norm_c in norm_extracted or norm_extracted in norm_c:
                return c_ser

    # 3. Fuzzy similarity match
    best_match = None
    best_score = threshold
    for c_ser in calibre_series_list:
        s_name = c_ser.get("series_name") or c_ser.get("name", "")
        score = string_similarity(extracted_series, s_name)
        if score > best_score:
            best_score = score
            best_match = c_ser
            
    return best_match
