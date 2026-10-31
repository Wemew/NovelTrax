import re
import unicodedata
from typing import Optional, Tuple, Dict, Any
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

def clean_feed_title(raw_title: str) -> str:
    """Strip web novel / download tags like [EPUB], (PDF), Free Download, etc."""
    if not raw_title:
        return ""
    
    cleaned = raw_title.strip()
    
    # Remove format/type words enclosed in parens or brackets: (Light Novel), [Light Novel], etc.
    cleaned = re.sub(r'[\(\[]\s*(?:light\s+novel|web\s+novel|novel|ln|wn|epub|pdf|mobi|azw3)\s*[\)\]]', ' ', cleaned, flags=re.IGNORECASE)
    
    # Remove square brackets and parentheses tags like [EPUB], (Light Novel), [PDF+EPUB]
    cleaned = re.sub(r"\[.*?\]", " ", cleaned)
    cleaned = re.sub(r"\(.*?\)", " ", cleaned)
    
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
    
    # Clean leading and trailing punctuation & spaces
    cleaned = re.sub(r"^[\s\-–—:;,|]+|[\s\-–—:;,|]+$", "", cleaned)
    return " ".join(cleaned.split())

def extract_series_and_volume(raw_title: str) -> Dict[str, Any]:
    """
    Intelligently extracts the series name, volume number, and optional subtitle
    from raw release titles commonly found on J-Novels, NovelUpdates, RSS feeds, etc.
    """
    cleaned = clean_feed_title(raw_title)
    
    # Result defaults
    result = {
        "raw_title": raw_title,
        "clean_title": cleaned,
        "series_name": cleaned,
        "volume": None,
        "subtitle": "",
        "confidence": 0.0
    }
    
    if not cleaned:
        return result

    # Pattern 1: Explicit volume/book keywords:
    # e.g., "DanMachi Volume 19", "Overlord Vol. 16", "The Eminence in Shadow v6", "Dungeon Crawler Carl Book 7: Subtitle"
    # Matches: Series + [delimiter] + (Volume|Vol|v|Book) + Number + [optional subtitle]
    p1 = re.compile(
        r"^(?P<series>.+?)\s*(?:[-–—:,]\s*)?"
        r"(?:\b(?:volume|vol|book)\b|\bvol\.|\bv)(?:\s*|\.)(?P<vol>\d+(?:\.\d+)?)"
        r"(?:\s*[:\-–—]\s*(?P<subtitle>.*))?$",
        re.IGNORECASE
    )
    m1 = p1.match(cleaned)
    if m1:
        series = m1.group("series").strip(" -–—:,|")
        # Strip any dangling "Vol" or "Book" if present
        series = re.sub(r"(?i)\b(?:volume|vol\.?|book)\b$", "", series).strip(" -–—:,|")
        vol_str = m1.group("vol")
        subtitle = (m1.group("subtitle") or "").strip(" -–—:,|")
        try:
            vol = float(vol_str)
            return {
                "raw_title": raw_title,
                "clean_title": cleaned,
                "series_name": series,
                "volume": vol,
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
        # Only accept if series name doesn't end with a bare number (to avoid year confusion like "2024")
        vol_str = m2.group("vol")
        subtitle = (m2.group("subtitle") or "").strip(" -–—:,|")
        try:
            vol = float(vol_str)
            # Filter out obvious years unless reasonable volume
            if not (1900 <= vol <= 2099):
                return {
                    "raw_title": raw_title,
                    "clean_title": cleaned,
                    "series_name": series,
                    "volume": vol,
                    "subtitle": subtitle,
                    "confidence": 0.80
                }
        except ValueError:
            pass

    # Pattern 3: Chapter / Volume hybrid: "Series Name - Volume 3 Chapter 12"
    p3 = re.compile(
        r"^(?P<series>.+?)\s*[-–—:]\s*(?:Volume|Vol\.?|v)\s*(?P<vol>\d+(?:\.\d+)?)"
        r"(?:\s*(?:Chapter|c)\s*(?P<chap>\d+(?:\.\d+)?))?",
        re.IGNORECASE
    )
    m3 = p3.match(cleaned)
    if m3:
        series = m3.group("series").strip(" -–—:,|")
        vol_str = m3.group("vol")
        try:
            vol = float(vol_str)
            return {
                "raw_title": raw_title,
                "clean_title": cleaned,
                "series_name": series,
                "volume": vol,
                "subtitle": "",
                "confidence": 0.85
            }
        except ValueError:
            pass

    # Ensure series_name and clean_title have no dangling light novel / novel keywords
    clean_series = re.sub(r'(?i)\s*\(?\b(light\s+novel|web\s+novel|novel|ln|wn)\b\)?', '', result["series_name"]).strip(" -–—:,|")
    result["series_name"] = clean_series
    result["clean_title"] = re.sub(r'(?i)\s*\(?\b(light\s+novel|web\s+novel|novel|ln|wn)\b\)?', '', result["clean_title"]).strip(" -–—:,|")
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
