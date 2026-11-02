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
    # 1. Check title indicators
    if raw_title:
        # Check for PDF tag in parens or brackets: (PDF), [PDF], (PDF+EPUB), etc.
        if re.search(r'[\(\[]\s*pdf\s*[\)\]]', raw_title, re.IGNORECASE):
            return True
        # Check for standalone word "pdf"
        if re.search(r'\bpdf\b', raw_title, re.IGNORECASE):
            return True
        # Check for explicit .pdf extension in title
        if re.search(r'\.pdf(?:\s|$)', raw_title, re.IGNORECASE):
            return True

    # 2. Check URL / GUID fields individually
    for target in (link, guid):
        if not target:
            continue
        target_clean = target.strip()
        # Direct .pdf before query params, hash fragment, delimiter or at end of URL
        if re.search(r'\.pdf(?:\?|#|&|/|$)', target_clean, re.IGNORECASE):
            return True
        target_lower = target_clean.lower()
        if '/pdf/' in target_lower or 'format=pdf' in target_lower or 'ext=pdf' in target_lower or 'filetype=pdf' in target_lower:
            return True

    # 3. Defense-in-depth: check combined normalized text with whitespace delimiter
    parts = [p.lower() for p in (raw_title, link, guid) if p]
    text_to_check = ' '.join(parts)
    if text_to_check and re.search(r'\.pdf(?:\?|#|&|/|\s|$)', text_to_check):
        return True

    return False

SERIES_ALIASES = [
    {
        "canonical": "Alya Sometimes Hides Her Feelings in Russian",
        "aliases": [
            "alya sometimes hides her feelings in russian",
            "the neighboring aarya san who sometimes acts affectionate and murmuring in russian",
            "the neighboring aaryasan who sometimes acts affectionate and murmuring in russian",
            "tokidoki bosotto russia go de dereru tonari no alya san",
            "roshidere"
        ]
    },
    {
        "canonical": "I Got a Cheat Ability in a Different World, and Become Extraordinary Even in the Real World",
        "aliases": [
            "i got a cheat skill in another world and became unrivaled in the real world too",
            "i got a cheat ability in a different world and become extraordinary even in the real world",
            "isekai de cheat skill wo te ni shita ore wa genjitsu sekai wo mo musou suru"
        ]
    },
    {
        "canonical": "By the Grace of the Gods",
        "aliases": [
            "by the grace of the gods",
            "kami tachi ni hirowareta otoko",
            "kamitachi ni hirowareta otoko"
        ]
    },
    {
        "canonical": "86-Eighty Six",
        "aliases": [
            "86",
            "86 eighty six",
            "eighty six"
        ]
    },
    {
        "canonical": "Tenkyou no Alderamin (Alderamin On The Sky)",
        "aliases": [
            "tenkyou no alderamin",
            "alderamin on the sky",
            "tenkyou no alderamin alderamin on the sky"
        ]
    },
    {
        "canonical": "Infinite Dendrogram",
        "aliases": [
            "infinite dendrogram",
            "infinite dendrogram all volumes"
        ]
    }
]

# Fast O(1) alias lookup dictionary:
CANONICAL_LOOKUP: Dict[str, str] = {}
for _group in SERIES_ALIASES:
    _canon = _group["canonical"]
    for _alias in _group["aliases"]:
        CANONICAL_LOOKUP[normalize_text(_alias)] = _canon

def get_canonical_series_name(name: str) -> str:
    """Returns canonical series name if matched against known alias sets, otherwise original name."""
    if not name:
        return ""
    norm = normalize_text(name)
    if norm in CANONICAL_LOOKUP:
        return CANONICAL_LOOKUP[norm]

    # Check dual titles separated by | or / (e.g. "Japanese Title | English Title")
    if "|" in name or "/" in name:
        for sub in re.split(r'\s*[|/]\s*', name):
            sub_norm = normalize_text(sub)
            if sub_norm in CANONICAL_LOOKUP:
                return CANONICAL_LOOKUP[sub_norm]

    return name

def should_merge_series(name_a: str, name_b: str) -> bool:
    """
    Determines if two series names refer to the same underlying series
    (e.g., '86' and '86-Eighty Six', 'Alya Sometimes Hides Her Feelings in Russian' and
    'The Neighboring Aarya-San Who Sometimes Acts Affectionate and Murmuring in Russian').
    """
    if not name_a or not name_b:
        return False
    norm_a = normalize_text(name_a)
    norm_b = normalize_text(name_b)
    if not norm_a or not norm_b:
        return False
    if norm_a == norm_b:
        return True

    # 1. Fast O(1) canonical check
    canon_a = get_canonical_series_name(name_a)
    canon_b = get_canonical_series_name(name_b)
    if canon_a != name_a and canon_b != name_b and canon_a == canon_b:
        return True
    if normalize_text(canon_a) == normalize_text(canon_b):
        return True

    # 2. Check dual title parts separated by | or /
    parts_a = [normalize_text(p) for p in re.split(r'\s*[|/]\s*', name_a) if len(p.strip()) >= 3]
    parts_b = [normalize_text(p) for p in re.split(r'\s*[|/]\s*', name_b) if len(p.strip()) >= 3]
    for pa in parts_a:
        for pb in parts_b:
            if pa == pb:
                return True
            if len(pa) >= 8 and len(pb) >= 8 and (pa in pb or pb in pa):
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

# Genuine HTML tag syntax: an opening/closing tag whose name starts with a letter
# (e.g. <script>, </b>, <img src=x>). Deliberately does NOT match bracketed or
# parenthesized publisher tags such as "[J-Novel Club]" or "(Light Novel)".
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")

def strip_html_markup(text: str) -> str:
    """
    Removes residual HTML markup from scraped titles/series names while preserving
    legitimate bracketed text. Script/style elements are dropped with their contents
    since they can never be part of a title; remaining tag syntax is replaced by space.
    """
    if not text or not isinstance(text, str) or "<" not in text:
        return text
    cleaned = re.sub(
        r"(?is)<\s*(?:script|style)\b[^>]*>.*?<\s*/\s*(?:script|style)\s*>",
        " ",
        text,
    )
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    return cleaned

def clean_feed_title(raw_title: str) -> str:
    """Strip web novel / download tags like [EPUB], (PDF), Free Download, etc. while preserving volume markers."""
    if not raw_title:
        return ""
    
    cleaned = raw_title.strip()

    # Strip residual HTML markup (e.g. <script>…</script>, <b>…</b>) from scraped titles.
    cleaned = strip_html_markup(cleaned)

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

    # Pattern 0: Parenthetical volume range preceded by an omnibus/edition number,
    # e.g. "DanMachi Omnibus 1 (Vols 1-3)". Normalize it to "DanMachi Omnibus Vols 1-3"
    # so the bundle-range matcher expands the range while the edition number is kept
    # out of the extracted series name.
    m_paren_bundle = re.match(
        r"^(?P<series>.+?)\s+\d+\s*\(\s*"
        r"(?P<range>(?:volumes?|vols?|books?|book)\.?\s*\d+(?:\.\d+)?\s*[-–—~to]+\s*"
        r"(?:(?:volumes?|vols?|books?|book)\.?\s*)?\d+(?:\.\d+)?)"
        r"\s*\)(?P<rest>.*)$",
        raw_title.strip() if isinstance(raw_title, str) else "",
        re.IGNORECASE,
    )
    if m_paren_bundle:
        cleaned = clean_feed_title(
            f"{m_paren_bundle.group('series')} {m_paren_bundle.group('range')} {m_paren_bundle.group('rest')}"
        )

    # Result defaults
    result = {
        "raw_title": raw_title,
        "clean_title": cleaned,
        "series_name": cleaned,
        "volume": None,
        "is_bundle": False,
        "is_chapter": False,
        "volume_start": None,
        "volume_end": None,
        "volumes": [],
        "subtitle": "",
        "confidence": 0.0
    }
    
    if not cleaned:
        return result

    # Pattern 0a: Multi-volume bundle range:
    # e.g., "Overlord Vol 1-14", "Classroom of the Elite Volume 1-11", "v01-v07", "Vol 1 to 5", "Vol. 01~10",
    # "DanMachi Vols 01-18" (plural forms Vol/Vols/Volumes)
    bundle_pattern = re.compile(
        r"^(?P<series>.+?)\s*(?:[-–—:,]\s*)?"
        r"(?:\b(?:volumes?|vols?|books?|book)\b\.?|\bv\.?)\s*(?P<start>\d+(?:\.\d+)?)"
        r"\s*[-–—~to]+\s*"
        r"(?:(?:volumes?|vols?|books?|book)\b\.?|\bv\.?)?\s*(?P<end>\d+(?:\.\d+)?)"
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
                "is_chapter": False,
                "volume_start": start_vol,
                "volume_end": end_vol,
                "volumes": vols,
                "subtitle": subtitle,
                "confidence": 0.95
            }
        except ValueError:
            pass

    # Pattern 0b: Comma or plus separated bundle
    # e.g., "Monogatari Series Vol 1, 2, 3", "Series Vol 1 + 2 + 3", "DanMachi Vols 1, 2, 3"
    list_bundle_pattern = re.compile(
        r"^(?P<series>.+?)\s*(?:[-–—:,]\s*)?"
        r"(?:\b(?:volumes?|vols?|books?)\b\.?|\bv\.?)\s*(?P<vols_str>\d+(?:\s*(?:,\s*(?:and\b|[+&])|[,+&]|\band\b)\s*(?:(?:volumes?|vols?|books?)\b\.?|\bv\.?)?\s*\d+)+)"
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
                "is_chapter": False,
                "volume_start": parsed_vols[0],
                "volume_end": parsed_vols[-1],
                "volumes": parsed_vols,
                "subtitle": (m_list.group("subtitle") or "").strip(" -–—:,|"),
                "confidence": 0.95
            }

    # Pattern 1: Explicit volume/book/part keywords:
    # e.g., "DanMachi Volume 19", "Infinite Dendrogram Volumes 1", "Overlord Vol. 16", "The Eminence in Shadow v6", "Dungeon Crawler Carl Book 7: Subtitle", "Avatar Korra Turf Wars Part 1"
    # Matches: Series + [delimiter] + (Volume|Volumes|Vol|Vols|v|Book|Books|Part|Parts|Pt|Pts) + Number + [optional subtitle]
    p1 = re.compile(
        r"^(?P<series>.+?)\s*(?:[-–—:,]\s*)?"
        r"(?:\b(?:volumes?|vols?|books?|parts?|pts?)\b\.?|\bv\.?)(?:\s*|\.)(?P<vol>\d+(?:\.\d+)?)"
        r"(?:\s*[:\-–—]\s*(?P<subtitle>.*))?$",
        re.IGNORECASE
    )
    m1 = p1.match(cleaned)
    if m1:
        series = m1.group("series").strip(" -–—:,|")
        series = re.sub(r"(?i)\b(?:volumes?|vols?\.?|books?|parts?|pts?\.?)\b$", "", series).strip(" -–—:,|")
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
                "is_chapter": False,
                "volume_start": vol,
                "volume_end": vol,
                "volumes": [vol],
                "subtitle": subtitle,
                "confidence": 0.95
            }
        except ValueError:
            pass

    # Pattern 1b: Series followed by a BARE numeric range, with no volume keyword.
    # e.g. "DanMachi 1-4", "86 - 1-4", "Mob Psycho 100 - 1-3"
    #
    # A range cannot be a chapter number, so it is treated as a bundle and kept
    # (and expanded). Without this, the bare number would fall through to
    # Pattern 2 and be misread as a single chapter/volume number.
    p1b = re.compile(
        r"^(?P<series>.+?)\s*(?:[-–—:,]\s*)?(?P<start>\d{1,3}(?:\.\d+)?)\s*[-–—~]\s*(?P<end>\d{1,3}(?:\.\d+)?)$",
        re.IGNORECASE
    )
    m1b = p1b.match(cleaned)
    if m1b:
        series = m1b.group("series").strip(" -–—:,|")
        try:
            start = float(m1b.group("start"))
            end = float(m1b.group("end"))
        except ValueError:
            start = end = -1.0
        # Sanity bounds: real volume ranges are small and ascending. Anything
        # else (years, chapter counts, junk) falls through to the later patterns.
        if series and 0 < start <= 300 and start <= end <= 300 and (end - start) <= 100:
            steps = int(round(end - start))
            vols = [start + i for i in range(steps + 1)]
            if steps == 0:
                vols = [start]
            clean_series = re.sub(r'(?i)\s*\(?\b(light\s+novel|web\s+novel|novel|ln|wn)\b\)?', '', series).strip(" -–—:,|")
            return {
                "raw_title": raw_title,
                "clean_title": cleaned,
                "series_name": clean_series,
                "volume": start,
                "is_bundle": True,
                "is_chapter": False,
                "volume_start": start,
                "volume_end": end,
                "volumes": vols,
                "subtitle": "",
                "confidence": 0.85
            }

    # Pattern 2: Series followed by a BARE number, with no volume keyword.
    # e.g. "Sword Art Online 28: Unital Ring VII", "Re:Zero 25", "Dandadan 250"
    #
    # A bare number is ambiguous: some feeds use it for volumes, but on chapter
    # feeds (and for manga-style releases that leak into the literature
    # categories) it is almost always a chapter number. Treat it as a chapter
    # and let the ingestion pipeline drop it; an explicit "Vol/Volume/V." prefix
    # is required for a bare number to count as a volume. The parsed volume is
    # still reported for diagnostic/back-compat callers -- only the is_chapter
    # flag drives the filtering decision.
    p2 = re.compile(
        r"^(?P<series>[A-Za-z0-9\s\'\"?!,\.\-]+?)\s+[-–—]?\s*(?P<vol>\d+(?:\.\d+)?)"
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
                    "is_chapter": True,
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
                "is_chapter": False,
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
        m_fallback = re.search(r'(?i)\b(?:volumes?|vols?|books?|parts?|pts?)\b\.?\s*(\d+(?:\.\d+)?)', raw_title)
        if m_fallback:
            try:
                v = float(m_fallback.group(1))
                result["volume"] = v
                result["volume_start"] = v
                result["volume_end"] = v
                result["volumes"] = [v]
                result["confidence"] = 0.85
                extracted_prefix = re.split(r'(?i)\b(?:volumes?|vols?|books?|parts?|pts?)\b', result["series_name"])[0]
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
    clean_series = re.sub(r"(?i)\b(?:volumes?|vols?\.?|books?|parts?|pts?\.?)\b$", "", clean_series).strip(" -–—:,|")
    result["series_name"] = clean_series
    result["clean_title"] = re.sub(r'(?i)\s*\(?\b(light\s+novel|web\s+novel|novel|ln|wn|epub|pdf)\b\)?', '', result["clean_title"]).strip(" -–—:,|")
    return result


# Explicit chapter markers, e.g. "Chapter 250", "Ch. 250", "Ch 250", "chap 12".
# The trailing number requirement keeps ordinary words such as "Chronicles" safe.
_CHAPTER_MARKER_RE = re.compile(
    r"(?i)(?:\b(?:chapters?|chap|ch)\.?\s*[-–—:]?\s*\d{1,4}(?:\.\d+)?\b"
    r"|\bc\d{1,4}(?:\.\d+)?\b)"
)

# Volume keyword that legitimises a bare number as a volume.
_VOLUME_KEYWORD_RE = re.compile(
    r"(?i)\b(?:volumes?|vols?|books?|parts?|pts?)\.?\s*\d|\bv\.?\s*\d"
)

# A title ending in a bare number, optionally wrapped in one trailing bracket,
# preceded by whitespace or a dash/colon boundary: "... 250", "... - 250", "[250]".
_TRAILING_BARE_NUMBER_RE = re.compile(
    r"(?i)(?:[\s\-–—:(\[])(\d{1,4}(?:\.\d+)?)\s*[\]\)]?\s*$"
)


def is_chapter_release(raw_title: str) -> bool:
    """
    True when a feed title denotes a chapter (or a non-volume instalment) rather
    than a book volume.

    Policy: a number only counts as a volume when it carries an explicit volume
    keyword (Vol / Volume / Vols / V. / Book / Part) or when it is part of a
    multi-volume range ("Vols 1-3", "1-4"). A *bare* number appended to a series
    name ("Dandadan 250", "Re:Zero 25") is treated as a chapter number, as is any
    title with an explicit Chapter/Ch. marker and no volume keyword.

    Chapters are dropped by the ingestion pipeline so they never reach Releases
    or Discovery. The parser still reports the parsed number so that diagnostic
    and back-compat callers keep working -- only this predicate drives filtering.
    """
    if not raw_title or not isinstance(raw_title, str):
        return False

    try:
        parsed = extract_series_and_volume(raw_title)
    except Exception:
        return False

    if parsed.get("is_chapter"):
        return True
    if parsed.get("is_bundle"):
        return False
    if parsed.get("volume") is not None:
        return False
    # No volume was resolved: an explicit chapter marker makes it a chapter.
    if _CHAPTER_MARKER_RE.search(raw_title) and not _VOLUME_KEYWORD_RE.search(raw_title):
        return True
    # Fallback for series names the strict patterns cannot represent (hyphens,
    # colons, apostrophes, brackets): a title that ends in a bare number with no
    # volume keyword anywhere is a chapter. "Sword Art Online 2024 Edition Vol 1"
    # is protected by the keyword test.
    if _VOLUME_KEYWORD_RE.search(raw_title) is None:
        if _TRAILING_BARE_NUMBER_RE.search(raw_title.strip()):
            return True
    return False


def match_series_in_calibre(extracted_series: str, calibre_series_list: list, threshold: float = 0.80) -> Optional[dict]:
    """
    Matches an extracted series name against Calibre's series list.
    calibre_series_list can have 'name' or 'series_name'.
    Supports dual titles separated by | or / (e.g. Romaji | English Title).
    Returns the best matching dict or None.
    """
    if not extracted_series or not calibre_series_list:
        return None
    
    candidates = [extracted_series]
    if "|" in extracted_series or "/" in extracted_series:
        for sub in re.split(r'\s*[|/]\s*', extracted_series):
            if len(sub.strip()) >= 3 and sub.strip() not in candidates:
                candidates.append(sub.strip())

    for cand in candidates:
        norm_extracted = normalize_text(cand)
        if not norm_extracted:
            continue
        
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


KNOWN_PUBLISHERS = {
    'yen press', 'yen on', 'j-novel club', 'j-novel heart', 'seven seas',
    'seven seas entertainment', 'airship', 'ghost ship', 'one peace books',
    'cross infinite world', 'tentai books', 'hanashi media', 'square enix',
    'square enix manga & books', 'kodansha', 'kodansha usa', 'viz', 'viz media',
    'tokyopop', 'dark horse', 'kaiten books', 'denpa', 'vertical', 'monogatari'
}

DEFAULT_KNOWN_GROUPS = [
    "Stick", "Yen Press", "J-Novel Club", "Seven Seas", "One Peace Books",
    "danke-Empire", "Lu自主规制", "a-sloth", "1R0N", "Kobo", "BW", "BookWalker",
    "nao", "GZ", "Anonymous", "TenmaLand", "LuCirAn", "NaBi", "aKraa", "LostYears"
]

BUZZWORDS = {
    # Document and book formats
    "epub", "pdf", "cbz", "cbr", "mobi", "azw3", "azw", "djvu", "fb2", "txt", "html", "htm", "rtf", "lit", "prc",
    "zip", "rar", "7z", "tar",
    # Audio formats, codecs, channels & buzzwords
    "audio", "multi-audio", "dual-audio", "multi audio", "dual audio",
    "multi", "dual", "audiobook", "audiobooks", "audio-book", "audio book",
    "flac", "aac", "mp3", "opus", "ogg", "wav", "m4a", "m4b", "alac", "ac3", "eac3", "dts", "lossless", "truehd", "atmos",
    "kbps",
    # Video resolutions, codecs, containers & formats
    "1080p", "720p", "480p", "2160p", "4k", "uhd", "hd", "hq", "fhd", "sd",
    "remux", "bd", "bluray", "blu-ray", "blu", "ray", "bdrerip", "bdrip", "dvd", "dvdrip", "vob",
    "web-dl", "webdl", "webrip", "siterip", "web", "dl",
    "mkv", "mp4", "avi", "wmv", "iso", "m4v", "mov", "flv", "webm", "ts",
    "x264", "x265", "h264", "h265", "hevc", "avc", "av1", "xvid", "divx", "h", "x",
    "10bit", "10-bit", "8bit", "8-bit", "bit", "hdr", "hdr10", "sdr",
    # Subtitles, dubs & language tags
    "sub", "subs", "subbed", "dub", "dubs", "dubbed",
    "multi-sub", "dual-sub", "multisub", "dualsub", "multi-subs", "dual-subs", "raw", "softsub", "hardsub",
    "jap", "jpn", "chs", "cht",
    # Status, packaging & edition tags
    "digital", "retail", "rip", "scan", "scanlation", "scans",
    "complete", "completed", "complete series", "ongoing", "batch", "pack",
    "novel", "novels", "light novel", "light novels", "ln", "wn", "web novel",
    "english", "eng", "en", "official", "official release",
    "repack", "proper", "rerip", "untrimmed", "fixed", "remastered", "uncensored", "censored",
    "bonus", "extras", "artbook", "artbooks", "illustrations", "illustration",
    "v1", "v2", "v3", "v4", "v5"
}

def is_valid_group_candidate(clean: str, known_groups: Optional[List[str]] = None) -> bool:
    clean = clean.strip()
    if not clean or len(clean) < 2 or len(clean) > 40:
        return False
    # Explicitly recognized groups bypass buzzwords
    groups = {g.lower() for g in (known_groups or DEFAULT_KNOWN_GROUPS)}
    if clean.lower() in groups:
        return True
    if re.match(r"^(?:19|20)\d{2}(?:\s*[-–—~to/]\s*(?:19|20)\d{2})?$", clean):
        return False
    if re.match(r"^(?:vol\.?|volume|v|ch\.?|chapter|c|book)\s*\d+", clean, re.IGNORECASE):
        return False
    if re.match(r"^\d+(?:\s*[-–—~to/]\s*\d+)?$", clean):
        return False
    lower = clean.lower()
    if lower in BUZZWORDS:
        return False
    tokens = [t.strip().lower() for t in re.split(r"[\s\-–—~_.+/|,:&]+", clean) if t.strip()]
    if tokens and all(t in BUZZWORDS or re.match(r"^\d+(?:p|bit|k|kbps)?$|^\d+x\d+$", t) or re.match(r"^(?:19|20)\d{2}$", t) for t in tokens):
        return False
    return True

def extract_release_group(raw_title: str, known_groups: Optional[List[str]] = None) -> Optional[str]:
    """
    Extracts release group or publisher tag from raw release titles.
    e.g. '[Yen Press] Overlord Vol. 1' -> 'Yen Press'
         '[Seven Seas] [Stick] Mushoku Tensei v01' -> 'Stick'
         'Re:Zero Vol 1 (J-Novel Club)' -> 'J-Novel Club'
         '[danke-Empire] Bleach v01-v20 [Completed]' -> 'danke-Empire'
    """
    if not raw_title:
        return None

    groups_to_check = sorted(list(known_groups or []) + DEFAULT_KNOWN_GROUPS, key=len, reverse=True)

    # 1. Extract bracketed candidates [Group] or (Group)
    bracket_pairs = re.findall(r'\[([^\]]+)\]|\(([^\)]+)\)', raw_title)
    all_candidates = []
    for b1, b2 in bracket_pairs:
        cand = (b1 or b2).strip()
        if is_valid_group_candidate(cand, known_groups=groups_to_check):
            all_candidates.append(cand)

    if all_candidates:
        # Separate into ripper groups vs publishers
        non_publishers = [c for c in all_candidates if c.lower() not in KNOWN_PUBLISHERS]
        publishers = [c for c in all_candidates if c.lower() in KNOWN_PUBLISHERS]

        # Check against known groups first among non_publishers
        if non_publishers:
            for cand in non_publishers:
                for kg in groups_to_check:
                    if cand.lower() == kg.lower():
                        return kg
            return non_publishers[0]

        # If only publishers, match against known groups or return first publisher
        if publishers:
            for cand in publishers:
                for kg in groups_to_check:
                    if cand.lower() == kg.lower():
                        return kg
            return publishers[0]

    # 2. Trailing scene group pattern e.g. Title-Stick (Digital)
    m_scene = re.search(r'[-–—]([A-Za-z0-9_]{2,30})(?:\s*[\(\[]|\s*$)', raw_title)
    if m_scene:
        cand = m_scene.group(1).strip()
        if is_valid_group_candidate(cand, known_groups=groups_to_check):
            for kg in groups_to_check:
                if cand.lower() == kg.lower():
                    return kg
            return cand

    # 3. Known groups substring match
    for kg in groups_to_check:
        escaped = re.escape(kg)
        if re.search(r'(?:^|[\s_.\-\(\[])' + escaped + r'(?:$|[\s_.\-\)\]])', raw_title, re.IGNORECASE):
            return kg

    return None

