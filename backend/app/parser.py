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

    # Strip leading download / site prefixes. "read"/"get" only when followed by a
    # separator, so real titles such as "Reading Our Way to a Happy New World" and
    # "Getting the Boot" keep their first word.
    cleaned = re.sub(r'^(?:\s*(?:free\s+)?download\b\s*[:\-–—]?\s*|\s*(?:read|get)\s*[:\-–—]\s*)', '', cleaned, flags=re.IGNORECASE)

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

# ---------------------------------------------------------------------------
# Part II (RELEASE_DETECTION_SPEC §12.8 / §12.12 / §12.13):
# chapter-range -> volume-range mapping, composite "+" tails, chapter data,
# publisher and re-release revision markers.
# ---------------------------------------------------------------------------

# "<chapters A-B> as v<X>-<Y>".  The volume numbers come from the "v" half ONLY:
# chapters 1-9 mapping to volumes 1-2 means volumes [1, 2], never volumes 1-9.
# The whole clause (chapter range + literal "as" + volume token) is removed from
# the series name, so `Reading Our Way to a Happy New World 001-009 as v01-02`
# keeps the clean series and does not leak "001-009 as" into the identity key.
_CHAPTER_AS_VOLUME_RE = re.compile(
    r"^(?P<series>.+?)\s+"
    r"(?P<chapter_start>\d{1,4}(?:\.\d+)?)\s*[-–—~]\s*(?P<chapter_end>\d{1,4}(?:\.\d+)?)"
    r"\s+as\s+"
    r"(?:volumes?|vols?|books?|v)\.?\s*"
    r"(?P<volume_start>\d{1,3}(?:\.\d+)?)"
    r"(?:\s*[-–—~]\s*(?P<volume_end>\d{1,3}(?:\.\d+)?))?"
    r"(?P<tail>.*)$",
    re.IGNORECASE,
)

# A trailing CHAIN of "+"-joined bare chapter ranges without any volume mapping:
# "Not Quite Love 001-005 + 006-009 + 010-012" -> series "Not Quite Love" and
# chapter ranges [[1,5],[6,9],[10,12]]; never volumes (§12.8 rule 6, §12.12 rule 5).
_TRAILING_CHAPTER_RANGE_CHAIN_RE = re.compile(
    r"^(?P<series>.+?)[\s\-–—:|]+"
    r"(?P<ranges>\d{1,4}(?:\.\d+)?\s*[-–—~]\s*\d{1,4}(?:\.\d+)?"
    r"(?:\s*\+\s*\d{1,4}(?:\.\d+)?\s*[-–—~]\s*\d{1,4}(?:\.\d+)?)+)$"
)
_RANGE_PAIR_RE = re.compile(r"(\d{1,4}(?:\.\d+)?)\s*[-–—~]\s*(\d{1,4}(?:\.\d+)?)")

# Composite "+ tails" after a parsed volume range (§12.12):
#   explicit volume continuation: "+ v10-12", "+ Vol 10" -> EXTENDS the volumes;
#   bare/zero-padded numeric range: "+ 047-051" -> chapter data only.
_VOLUME_TAIL_SEGMENT_RE = re.compile(
    r"(?:^|[+&,])\s*(?:\b(?:volumes?|vols?|books?|v)\.?)\s*(\d{1,3}(?:\.\d+)?)"
    r"(?:\s*[-–—~]\s*(?:\b(?:volumes?|vols?|books?|v)\.?\s*)?(\d{1,3}(?:\.\d+)?))?",
    re.IGNORECASE,
)
_BARE_TAIL_RANGE_RE = re.compile(
    r"(?:^|[+&,])\s*(\d{1,4}(?:\.\d+)?)\s*[-–—~]\s*(\d{1,4}(?:\.\d+)?)\b",
    re.IGNORECASE,
)

# Explicit chapter markers with a captured number: "Chapter 179", "Ch. 250", "c374", "#01".
_EXPLICIT_CHAPTER_NUMBER_RE = re.compile(
    r"(?i)(?:\b(?:chapters?|chap|ch)\.?\s*[-–—:]?\s*(\d{1,4}(?:\.\d+)?)\b"
    r"|\bc(\d{1,4}(?:\.\d+)?)\b"
    r"|#\s*(\d{1,4}(?:\.\d+)?))"
)

# Re-release / fix revision marker: "(f)" = 1, "(f2)" = second, "(f10)" = tenth.
# Case-insensitive; either bracket style. It is metadata — never a group signal
# and never allowed to alter series/volume/chapter extraction.
_REVISION_RE = re.compile(r"[\(\[]\s*f\s*(\d{0,3})\s*[\)\]]", re.IGNORECASE)

# Complete-series compilation tags: "[Completed]", "[Complete]", "(all volumes)".
_COMPLETE_SERIES_RE = re.compile(
    r"(?i)[\(\[]\s*(?:complete|completed|complete\s*series|completed\s*series|"
    r"all\s*volumes?|full\s*series)\s*[\)\]]"
)


def _is_padded_number(text: str) -> bool:
    """Zero-padded numbers ("001", "047") are chapter notation in manga feeds."""
    text = (text or "").strip()
    return len(text) > 1 and text[0] == "0"


def _expand_volume_span(start: float, end: float) -> List[float]:
    """Integer-stepped expansion of a volume span (start/end may be fractional)."""
    steps = int(round(end - start))
    if steps <= 0:
        return [start]
    return [start + i for i in range(steps + 1)]


def _parse_tail_segments(tail: str) -> Tuple[List[float], List[List[float]], str]:
    """Parse the ``+ ...`` continuation of a volume-range release (§12.12).

    Returns ``(volumes, chapter_ranges, cleaned_tail)``:

    * an explicit volume continuation ("+ v10-12", "+ Vol 10") EXTENDS the volumes;
    * a bare numeric range ("+ 047-051") is CHAPTER data, never volumes;
    * a bare single number ("+ 047") is chapter data too.
    """
    volumes: List[float] = []
    chapter_ranges: List[List[float]] = []
    if not tail:
        return volumes, chapter_ranges, tail

    def _blank(text: str, start: int, end: int) -> str:
        return text[:start] + " " * (end - start) + text[end:]

    cleaned = tail
    for match in _VOLUME_TAIL_SEGMENT_RE.finditer(tail):
        try:
            start = float(match.group(1))
            end = float(match.group(2)) if match.group(2) else start
        except (TypeError, ValueError):
            continue
        if start > end:
            start, end = end, start
        if not (0 < start <= 300) or not (0 < end <= 300) or (end - start) > 100:
            continue
        for volume in _expand_volume_span(start, end):
            if volume not in volumes:
                volumes.append(volume)
        cleaned = _blank(cleaned, match.start(), match.end())

    for match in _BARE_TAIL_RANGE_RE.finditer(tail):
        try:
            start = float(match.group(1))
            end = float(match.group(2))
        except (TypeError, ValueError):
            continue
        if start > end:
            start, end = end, start
        if 1900 <= start <= 2099 and 1900 <= end <= 2099:
            continue  # an edition-year range is not chapter data
        if [start, end] not in chapter_ranges:
            chapter_ranges.append([start, end])
        cleaned = _blank(cleaned, match.start(), match.end())

    # Remaining bare single numbers are chapter data ("+ 047"), never volumes.
    for part in re.split(r"[+&,]", cleaned):
        part = part.strip()
        if not re.fullmatch(r"\d{1,4}(?:\.\d+)?", part or ""):
            continue
        number = float(part)
        if 1900 <= number <= 2099:
            continue
        single = [number, number]
        if single not in chapter_ranges:
            chapter_ranges.append(single)

    cleaned = " ".join(cleaned.replace("+", " ").replace("&", " ").replace(",", " ").split())
    return volumes, chapter_ranges, cleaned


def _extract_chapter_as_volume(parse_title: str) -> Optional[Dict[str, Any]]:
    """Parse the "<chapters A-B> as v<X>-<Y> [+ <chapters C-D>]" idiom (§12.8).

    Requires an explicit ``as`` plus an explicit volume token preceded by a
    chapter range, so it cannot consume an ordinary title (§12.8 rule 5).
    Returns the volumes from the ``v`` half, the chapter ranges as data, and a
    ``clean_title`` with the whole clause removed.
    """
    if not parse_title or not isinstance(parse_title, str):
        return None
    m = _CHAPTER_AS_VOLUME_RE.match(parse_title.strip())
    if not m:
        return None
    series = (m.group("series") or "").strip()
    if not series:
        return None
    try:
        chapter_start = float(m.group("chapter_start"))
        chapter_end = float(m.group("chapter_end"))
        volume_start = float(m.group("volume_start"))
        volume_end = float(m.group("volume_end")) if m.group("volume_end") else volume_start
    except (TypeError, ValueError):
        return None
    # An edition-year "range" is not a chapter range.
    if 1900 <= chapter_start <= 2099 and 1900 <= chapter_end <= 2099:
        return None
    if chapter_start > chapter_end:
        chapter_start, chapter_end = chapter_end, chapter_start
    if volume_start > volume_end:
        volume_start, volume_end = volume_end, volume_start
    # Same sanity guards as Pattern 1b: real volume ranges are small.
    if not (0 < volume_start <= 300) or not (0 < volume_end <= 300) or (volume_end - volume_start) > 100:
        return None

    tail = m.group("tail") or ""
    tail_volumes, tail_chapters, clean_tail = _parse_tail_segments(tail)
    chapter_ranges: List[List[float]] = [[chapter_start, chapter_end]]
    for chapter_range in tail_chapters:
        if chapter_range not in chapter_ranges:
            chapter_ranges.append(chapter_range)
    volumes = _expand_volume_span(volume_start, volume_end)
    for volume in tail_volumes:
        if volume not in volumes:
            volumes.append(volume)
    volumes = sorted(set(volumes))
    return {
        "series_prefix": series,
        "clean_title": f"{series} {clean_tail}".strip(),
        "chapter_ranges": chapter_ranges,
        "volumes": volumes,
        "volume_start": min(volumes),
        "volume_end": max(volumes),
    }


def _match_chapter_range_chain(cleaned: str) -> Optional[Dict[str, Any]]:
    """Match a trailing chain of "+"-joined bare chapter ranges (§12.12 rule 5)."""
    if not cleaned or not isinstance(cleaned, str):
        return None
    m = _TRAILING_CHAPTER_RANGE_CHAIN_RE.match(cleaned.strip())
    if not m:
        return None
    series = (m.group("series") or "").strip(" -–—:,|.")
    if not series:
        return None
    chapter_ranges: List[List[float]] = []
    for pair in _RANGE_PAIR_RE.finditer(m.group("ranges")):
        try:
            start = float(pair.group(1))
            end = float(pair.group(2))
        except (TypeError, ValueError):
            continue
        chapter_ranges.append([min(start, end), max(start, end)])
    if not chapter_ranges:
        return None
    return {"series": series, "chapter_ranges": chapter_ranges}


def _chapter_number_from_title(raw_title: str) -> Optional[float]:
    """Explicit chapter marker number (Chapter/Ch./c/#) when the title has one."""
    if not raw_title or not isinstance(raw_title, str):
        return None
    m = _EXPLICIT_CHAPTER_NUMBER_RE.search(raw_title)
    if not m:
        return None
    for group in m.groups():
        if group:
            try:
                return float(group)
            except (TypeError, ValueError):
                return None
    return None


def _is_complete_series_title(raw_title: str) -> bool:
    """True for a complete-series compilation tag ("[Completed]", "[Complete]")."""
    if not raw_title or not isinstance(raw_title, str):
        return False
    return bool(_COMPLETE_SERIES_RE.search(raw_title))


def parse_revision_marker(raw_title: str) -> Dict[str, Any]:
    """Parse a re-release/fix revision marker: ``(f)`` -> 1, ``(f2)`` -> 2.

    Returns ``{"revision": int | None, "is_rerelease": bool}``.  A revision is
    metadata about WHICH revision of the same upload this is: it must never
    alter series, volumes, chapters, publisher or release group (§12.13.2).
    """
    result: Dict[str, Any] = {"revision": None, "is_rerelease": False}
    if not raw_title or not isinstance(raw_title, str):
        return result
    m = _REVISION_RE.search(raw_title)
    if not m:
        return result
    digits = (m.group(1) or "").strip()
    if not digits:
        revision = 1
    else:
        try:
            revision = int(digits)
        except ValueError:
            revision = None
    if revision is None or revision <= 0:
        return result
    result["revision"] = revision
    result["is_rerelease"] = True
    return result


def _strip_trailing_metadata_blocks(text: str) -> str:
    """Remove trailing ``(metadata)`` / ``[metadata]`` blocks from a title.

    Used by the chapter fallback so a bare trailing number is still detected when
    the release ends in parenthesised feed metadata ("Dandadan 250 (Digital)
    (1r0n)") — the normal Nyaa form (§12.2.1 trap 1).
    """
    if not text or not isinstance(text, str):
        return text or ""
    cleaned = text.strip()
    previous = None
    while cleaned and cleaned != previous:
        previous = cleaned
        cleaned = re.sub(r"\s*[\(\[]\s*[^\(\[]*?\s*[\)\]]\s*$", "", cleaned).strip()
    return cleaned


def _strip_chapter_residue(series: str) -> str:
    """Strip a trailing chapter marker/number from a chapter release's series key.

    A chapter release must not keep the chapter number in the series identity
    (§12.2.1 trap 2), otherwise every chapter of an untracked series creates a
    new phantom series ("Dandadan 250", "Dandadan 251", ...).
    """
    if not series or not isinstance(series, str):
        return series
    stripped = series.strip()
    stripped = re.sub(
        r"(?i)\s*(?:\b(?:chapters?|chap|ch)\.?\s*[-–—:]?\s*\d{1,4}(?:\.\d+)?"
        r"|\bc\d{1,4}(?:\.\d+)?"
        r"|#\s*\d{1,4}(?:\.\d+)?)\s*$",
        "",
        stripped,
    )
    stripped = re.sub(r"(?i)\s*\b(?:chapters?|chap|ch)\.?\s*$", "", stripped)
    stripped = stripped.strip(" -–—:,|.")
    return stripped if stripped else series


def _finalize_parse(raw_title: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Add the Part II detection fields and chapter data to a parse result."""
    raw = raw_title if isinstance(raw_title, str) else ""
    result.setdefault("chapter_number", None)
    result.setdefault("chapter_ranges", [])
    result.setdefault("chapter_range", None)
    result.setdefault("chapter_start", None)
    result.setdefault("chapter_end", None)
    result.setdefault("numbered_series_name", "")
    result.setdefault("publisher", None)
    result.setdefault("release_group", None)
    result.setdefault("release_groups", [])
    result.setdefault("revision", None)
    result.setdefault("is_rerelease", False)
    result.setdefault("is_complete", False)

    # Complete-series compilation tags are status information, never a volume
    # override: explicit volume markers always outrank them (§12.12 rule 4).
    result["is_complete"] = bool(_is_complete_series_title(raw))
    result["is_complete_series"] = result["is_complete"]

    # Publisher / ordered group set / revision: all metadata, never volume signals.
    groups: List[str] = list(result.get("release_groups") or [])
    if not groups:
        try:
            groups = list(extract_release_groups(raw) or [])
        except Exception:
            groups = []
    result["release_groups"] = groups
    if not result.get("release_group"):
        if groups:
            result["release_group"] = groups[0]
        else:
            try:
                result["release_group"] = extract_release_group(raw)
            except Exception:
                result["release_group"] = None
    if not result.get("publisher"):
        try:
            result["publisher"] = extract_publisher(raw)
        except Exception:
            result["publisher"] = None
    revision_marker = parse_revision_marker(raw)
    if result.get("revision") is None:
        result["revision"] = revision_marker["revision"]
    if not result.get("is_rerelease"):
        result["is_rerelease"] = revision_marker["is_rerelease"]

    # ----- Chapter data (spec §12.2 traps 2/3, §12.8 rule 4) ----------------
    is_chapter = bool(result.get("is_chapter"))
    if (
        not is_chapter
        and result.get("volume") is None
        and not result.get("is_bundle")
        and not (result.get("chapter_ranges") or [])
        and not result["is_complete"]
        and not _VOLUME_KEYWORD_RE.search(raw)
    ):
        marker_number = _chapter_number_from_title(raw)
        if marker_number is not None:
            is_chapter = True
            result["is_chapter"] = True
            result["chapter_number"] = marker_number
        else:
            cleaned_title = (result.get("clean_title") or "").strip()
            trailing = _TRAILING_BARE_NUMBER_RE.search(cleaned_title)
            if trailing:
                try:
                    number = float(trailing.group(1))
                except (TypeError, ValueError):
                    number = None
                # Edition years are not chapter numbers (spec §5 Pattern 2 guard).
                if number is not None and not (1900 <= number <= 2099):
                    is_chapter = True
                    result["is_chapter"] = True
                    result["chapter_number"] = number

    if is_chapter and not result.get("is_bundle"):
        if result.get("chapter_number") is None and result.get("volume") is not None:
            try:
                result["chapter_number"] = float(result["volume"])
            except (TypeError, ValueError):
                pass
        cleaned_series = (result.get("series_name") or "").strip()
        stripped_series = _strip_chapter_residue(cleaned_series)
        if stripped_series and stripped_series != cleaned_series:
            result["numbered_series_name"] = cleaned_series
            result["series_name"] = stripped_series
        elif not result.get("numbered_series_name"):
            # Pre-strip title (e.g. "Mob Psycho 100" for series "Mob Psycho"):
            # lets context-aware callers protect genuinely numbered series names.
            result["numbered_series_name"] = (
                result.get("clean_title") or cleaned_series or ""
            ).strip()

    ranges: List[List[float]] = []
    for chapter_range in result.get("chapter_ranges") or []:
        if isinstance(chapter_range, (list, tuple)) and len(chapter_range) >= 2:
            try:
                ranges.append([float(chapter_range[0]), float(chapter_range[1])])
            except (TypeError, ValueError):
                continue
    result["chapter_ranges"] = ranges
    if ranges:
        result["chapter_range"] = [ranges[0][0], ranges[0][1]]
        result["chapter_start"] = min(r[0] for r in ranges)
        result["chapter_end"] = max(r[1] for r in ranges)
        if result.get("chapter_number") is None and len(ranges) == 1 and ranges[0][0] == ranges[0][1]:
            result["chapter_number"] = ranges[0][0]
    return result


def _extract_series_and_volume_inner(raw_title: str) -> Dict[str, Any]:
    """
    Intelligently extracts series name, volume(s), bundle ranges, and optional subtitle
    from raw release titles commonly found on J-Novels, NovelUpdates, RSS feeds, etc.
    """
    parse_title = raw_title.strip() if isinstance(raw_title, str) else ""
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
        parse_title,
        re.IGNORECASE,
    )
    if m_paren_bundle:
        parse_title = (
            f"{m_paren_bundle.group('series')} {m_paren_bundle.group('range')} "
            f"{m_paren_bundle.group('rest')}"
        )
        cleaned = clean_feed_title(parse_title)

    # §12.8 pre-pass: "<chapters A-B> as v<X>-<Y> [+ <chapters C-D>]".  This must
    # run BEFORE the volume patterns so the "v" half (not the chapter numbers)
    # supplies the volumes and the literal "as" clause never pollutes the series.
    chapter_meta = _extract_chapter_as_volume(parse_title)
    if chapter_meta:
        mapped_clean = clean_feed_title(chapter_meta["clean_title"])
        return {
            "raw_title": raw_title,
            "clean_title": mapped_clean,
            "series_name": mapped_clean,
            "volume": chapter_meta["volumes"][0] if chapter_meta["volumes"] else None,
            "is_bundle": len(chapter_meta["volumes"]) > 1,
            "is_chapter": False,
            "volume_start": chapter_meta["volume_start"],
            "volume_end": chapter_meta["volume_end"],
            "volumes": list(chapter_meta["volumes"]),
            "subtitle": "",
            "confidence": 0.95,
            "chapter_number": None,
            "chapter_ranges": [list(r) for r in chapter_meta["chapter_ranges"]],
        }

    # §12.12 rule 5 pre-pass: a chain of "+"-joined bare chapter ranges with no
    # volume mapping at all is a chapter batch.  Every range is chapter data and
    # the whole chain (plus the "+" separators) leaves the series key clean.
    if not _VOLUME_KEYWORD_RE.search(cleaned):
        chapter_chain = _match_chapter_range_chain(cleaned)
        if chapter_chain:
            return {
                "raw_title": raw_title,
                "clean_title": cleaned,
                "series_name": chapter_chain["series"],
                "volume": None,
                "is_bundle": False,
                "is_chapter": True,
                "volume_start": None,
                "volume_end": None,
                "volumes": [],
                "subtitle": "",
                "confidence": 0.80,
                "chapter_number": None,
                "chapter_ranges": [list(r) for r in chapter_chain["chapter_ranges"]],
            }

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
    # Composite "+" tails (§12.12): an explicit volume continuation ("+ v10-12")
    # extends the volume set, while a bare range ("+ 047-051") is chapter data.
    bundle_pattern = re.compile(
        r"^(?P<series>.+?)\s*(?:[-–—:,]\s*)?"
        r"(?:\b(?:volumes?|vols?|books?|book)\b\.?|\bv\.?)\s*(?P<start>\d+(?:\.\d+)?)"
        r"\s*[-–—~to]+\s*"
        r"(?:(?:volumes?|vols?|books?|book)\b\.?|\bv\.?)?\s*(?P<end>\d+(?:\.\d+)?)"
        r"(?P<tail>.*)$",
        re.IGNORECASE
    )
    m_bundle = bundle_pattern.match(cleaned)
    if m_bundle:
        series = m_bundle.group("series").strip(" -–—:,|")
        series = re.sub(r"(?i)\b(?:volumes|volume|vol\.?|books?)\b$", "", series).strip(" -–—:,|")
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

            # The remainder is either a composite "+" tail (§12.12) or a subtitle
            # introduced by ":"/"-".  Explicit volume continuations EXTEND the
            # volumes; a bare numeric tail is chapter data only (never 47-51).
            tail = (m_bundle.group("tail") or "").strip()
            if tail and tail[0] in "+&,":
                tail_volumes, tail_chapters, _tail_rest = _parse_tail_segments(tail)
                subtitle = ""
            else:
                tail_volumes, tail_chapters = [], []
                subtitle = tail.strip(" -–—:,|")
            if tail_volumes:
                vols = sorted(set(vols) | set(tail_volumes))
                end_vol = max(end_vol, max(tail_volumes))

            clean_series = re.sub(r'(?i)\s*\(?\b(light\s+novel|web\s+novel|novel|ln|wn)\b\)?', '', series).strip(" -–—:,|")
            return {
                "raw_title": raw_title,
                "clean_title": cleaned,
                "series_name": clean_series,
                "volume": vols[0] if vols else start_vol,
                "is_bundle": True,
                "is_chapter": False,
                "volume_start": vols[0] if vols else start_vol,
                "volume_end": end_vol,
                "volumes": vols,
                "subtitle": subtitle,
                "confidence": 0.95,
                "chapter_number": None,
                "chapter_ranges": [list(r) for r in tail_chapters],
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
        series = re.sub(r'^(?:\s*(?:free\s+)?download\b\s*[:\-–—]?\s*|\s*(?:read|get)\s*[:\-–—]\s*)', '', series, flags=re.IGNORECASE).strip(" -–—:,|")
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
    #
    # Zero-padded ranges are chapter notation in manga feeds ("Series 001-009"):
    # they are chapter data, never invented volumes (§12.8 rule 6).
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
        if series and start >= 0 and end >= 0 and (
            _is_padded_number(m1b.group("start")) or _is_padded_number(m1b.group("end"))
        ):
            clean_series = re.sub(r'(?i)\s*\(?\b(light\s+novel|web\s+novel|novel|ln|wn)\b\)?', '', series).strip(" -–—:,|")
            return {
                "raw_title": raw_title,
                "clean_title": cleaned,
                "series_name": clean_series,
                "volume": None,
                "is_bundle": False,
                "is_chapter": True,
                "volume_start": None,
                "volume_end": None,
                "volumes": [],
                "subtitle": "",
                "confidence": 0.80,
                "chapter_number": None,
                "chapter_ranges": [[min(start, end), max(start, end)]],
            }
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

    # Pattern 3: Chapter / Volume hybrid: "Series Name - Volume 3 Chapter 12".
    # Evaluated before Pattern 2 so the explicit volume keyword wins over the
    # trailing chapter number, and the chapter number is captured as data (§9.2).
    p3 = re.compile(
        r"^(?P<series>.+?)\s*[-–—:]\s*(?:Volume|Vol\.?|v|Book|Part)\s*(?P<vol>\d+(?:\.\d+)?)"
        r"(?:\s*(?:Chapter|c)\s*(?P<chap>\d+(?:\.\d+)?))?",
        re.IGNORECASE
    )
    m3 = p3.match(cleaned)
    if m3:
        series = m3.group("series").strip(" -–—:,|")
        series = re.sub(r'^(?:\s*(?:free\s+)?download\b\s*[:\-–—]?\s*|\s*(?:read|get)\s*[:\-–—]\s*)', '', series, flags=re.IGNORECASE).strip(" -–—:,|")
        vol_str = m3.group("vol")
        chap_str = m3.group("chap")
        try:
            vol = float(vol_str)
            chapter_number = float(chap_str) if chap_str else None
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
                "confidence": 0.85,
                "chapter_number": chapter_number,
                "chapter_ranges": [[chapter_number, chapter_number]] if chapter_number is not None else [],
            }
        except ValueError:
            pass

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
        series = re.sub(r'^(?:\s*(?:free\s+)?download\b\s*[:\-–—]?\s*|\s*(?:read|get)\s*[:\-–—]\s*)', '', series, flags=re.IGNORECASE).strip(" -–—:,|")
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
    # Site prefix: "download"/"free download" always; "read"/"get" ONLY when followed
    # by a separator (§2 step 2).  Without this guard "Reading Our Way to a Happy
    # New World" loses its first four letters on the no-pattern path.
    clean_series = re.sub(
        r'^(?:\s*(?:free\s+)?download\b\s*[:\-–—]?\s*|\s*(?:read|get)\s*[:\-–—]\s*)',
        '',
        clean_series,
        flags=re.IGNORECASE,
    )
    clean_series = re.sub(r'(?i)\b(?:all\s+volumes?|complete\s+(?:series|collection|set)?|box\s*set)\b', '', clean_series)
    clean_series = re.sub(r'(?i)\s*\(?\b(light\s+novel|web\s+novel|novel|ln|wn|epub|pdf)\b\)?', '', clean_series)
    clean_series = re.sub(r'(?i)\s*\[?\b(light\s+novel|web\s+novel|novel|ln|wn|epub|pdf)\b\]?', '', clean_series)
    clean_series = re.sub(r"(?i)\b(?:volumes?|vols?\.?|books?|parts?|pts?\.?)\b$", "", clean_series).strip(" -–—:,|")
    result["series_name"] = clean_series
    result["clean_title"] = re.sub(r'(?i)\s*\(?\b(light\s+novel|web\s+novel|novel|ln|wn|epub|pdf)\b\)?', '', result["clean_title"]).strip(" -–—:,|")
    return result


def extract_series_and_volume(raw_title: str) -> Dict[str, Any]:
    """Intelligently extract series name, volume(s), bundle ranges and subtitle.

    Part II additions (RELEASE_DETECTION_SPEC §12.2/§12.8/§12.11/§12.12/§12.13):
    the returned dict additionally exposes ``chapter_number``, ``chapter_ranges``,
    ``chapter_range`` (first range for convenience), ``chapter_start`` /
    ``chapter_end``, ``numbered_series_name`` and the metadata fields
    ``release_group`` (legacy primary), ``release_groups`` (ordered set),
    ``publisher``, ``revision``, ``is_rerelease`` and ``is_complete``.
    """
    return _finalize_parse(raw_title, _extract_series_and_volume_inner(raw_title))


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
    #
    # The trailing test runs on the title with its trailing metadata blocks
    # stripped (§12.2.1 trap 1): virtually every real release ends in
    # "(Digital) (1r0n)", which would otherwise defeat the check.
    if _VOLUME_KEYWORD_RE.search(raw_title) is None:
        stripped_title = _strip_trailing_metadata_blocks(raw_title)
        trailing = _TRAILING_BARE_NUMBER_RE.search(stripped_title)
        if trailing:
            try:
                number = float(trailing.group(1))
            except (TypeError, ValueError):
                number = None
            # Edition years are not chapter numbers (spec §5 Pattern 2 year guard).
            if number is None or not (1900 <= number <= 2099):
                return True
    return False


def match_series_in_calibre(
    extracted_series: str,
    calibre_series_list: list,
    threshold: float = 0.80,
    alt_names: Optional[list] = None,
    index: Optional[object] = None,
) -> Optional[dict]:
    """
    Matches an extracted series name against Calibre's series list.

    The matching rules live in exactly one place: :class:`app.series_identity.
    CalibreSeriesIndex` (exact normalized candidates, conservative substring
    containment, fuzzy scoring with a head-token guard, and ambiguity rejection).
    This function is the backwards-compatible entry point used by the release
    matcher and the scheduler, so it must never reimplement those rules.

    ``calibre_series_list`` entries may have ``series_name`` or ``name``.
    ``alt_names`` are AniList titles/synonyms (or alias candidates) that are
    tried in addition to the primary name; per the identity contract they are
    ranked after an exact hit on the primary name. ``index`` lets a caller pass
    a prebuilt ``CalibreSeriesIndex`` (the release matcher builds one per
    library, not per release). Two-argument callers keep the legacy signature.

    Returns the matching Calibre series dict or None.
    """
    if not extracted_series or not calibre_series_list:
        return None

    from app.series_identity import CalibreSeriesIndex

    if not isinstance(index, CalibreSeriesIndex):
        index = CalibreSeriesIndex(calibre_series_list)

    clean_alt = [name for name in (alt_names or []) if name and str(name).strip()]
    return index.match(
        extracted_series,
        alt_names=clean_alt or None,
        threshold=threshold,
    )


KNOWN_PUBLISHERS = {
    'yen press', 'yen on', 'j-novel club', 'j-novel heart', 'seven seas',
    'seven seas entertainment', 'seven seas siren', 'airship', 'ghost ship',
    'one peace books', 'cross infinite world', 'tentai books', 'hanashi media',
    'square enix', 'square enix manga & books', 'kodansha', 'kodansha comics',
    'kodansha usa', 'kodansha digital', 'viz', 'viz media', 'tokyopop',
    'dark horse', 'dark horse comics', 'kaiten books', 'denpa', 'vertical',
    'monogatari', 'sol press',
    # Official digital platforms / manga publishers seen in Nyaa release titles
    # (§12.13.3).  Normalised for case and hyphenation before comparison, so
    # "(K-MANGA)", "(K MANGA)" and "(KManga)" all resolve to a publisher and can
    # never capture the release-group slot.
    'k-manga', 'k manga', 'kmanga', 'manga up', 'manga up!', 'shueisha',
    'shueisha manga', 'bookwalker', 'comikey', 'azuki', 'mangamo', 'inkr',
    'coolmic', 'tappytoon', 'tapas', 'manta', 'yen plus',
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

# §7.1 vocabulary gap: compound format/edition tags ("Digital-Compilation",
# "Batched", "Complete-Collection") are built from words that were not all in
# BUZZWORDS, so `is_valid_group_candidate`'s all-tokens rule accepted them as
# release groups and they shadowed the real group.  FORMAT_TOKENS is the
# superset used by the validity filter: only candidates made _entirely_ of
# format/edition words are rejected, so publishers and genuine groups (including
# "Square Enix Manga & Books", "danke-Empire", non-ASCII names) are unaffected.
_COMPOUND_FORMAT_TOKENS = {
    "compilation", "compilations", "digital-compilation", "digital compilation",
    "digital+digital-compilation", "digital + digital-compilation",
    "batched", "batch", "complete-collection", "complete collection",
    "complete-series", "complete series", "full-series", "full series",
    "all-volumes", "all volumes", "collection", "collections",
    "omnibus", "edition", "editions", "special", "deluxe",
    "collector", "collectors", "box", "boxset", "box-set",
    "colored", "coloured", "color", "colour",
    "remaster", "uncensored",
}
FORMAT_TOKENS = set(BUZZWORDS) | _COMPOUND_FORMAT_TOKENS


def _normalise_publisher_key(name: str) -> str:
    """Normalise a publisher name for case- and hyphen-insensitive comparison."""
    if not name:
        return ""
    key = str(name).strip().lower()
    key = re.sub(r"[-–—_]+", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    return key


_NORMALISED_PUBLISHERS = {_normalise_publisher_key(name) for name in KNOWN_PUBLISHERS}


def _is_known_publisher(name: str) -> bool:
    """True when *name* is a recognised publisher/platform token (§12.13)."""
    if not name:
        return False
    return _normalise_publisher_key(name) in _NORMALISED_PUBLISHERS


def is_valid_group_candidate(clean: str, known_groups: Optional[List[str]] = None) -> bool:
    """True when a bracket/paren candidate can be a release group name."""
    clean = (clean or "").strip() if isinstance(clean, str) else ""
    if not clean or len(clean) < 2 or len(clean) > 40:
        return False
    # Explicitly recognized groups bypass buzzwords
    groups = {g.lower() for g in (known_groups or DEFAULT_KNOWN_GROUPS)}
    if clean.lower() in groups:
        return True
    # Re-release/fix revision markers ("f", "f2", "f10") are metadata, never groups.
    if re.match(r"^f\s*\d{0,3}$", clean, re.IGNORECASE):
        return False
    if re.match(r"^(?:19|20)\d{2}(?:\s*[-–—~to/]\s*(?:19|20)\d{2})?$", clean):
        return False
    # Volume/chapter tags — including plural forms — are never release groups.
    if re.match(
        r"^(?:vols?\.?|volumes?|v|chapters?|chap|ch\.?|c|books?|parts?|pts?)\.?\s*\d+",
        clean,
        re.IGNORECASE,
    ):
        return False
    if re.match(r"^\d+(?:\s*[-–—~to/]\s*\d+)?$", clean):
        return False
    lower = clean.lower()
    if lower in FORMAT_TOKENS:
        return False
    tokens = [t.strip().lower() for t in re.split(r"[\s\-–—~_.+/|,:&]+", clean) if t.strip()]
    if tokens and all(
        t in FORMAT_TOKENS
        or re.match(r"^\d+(?:p|bit|k|kbps|i|fps)?$", t)
        or re.match(r"^\d+x\d+$", t)
        or re.match(r"^(?:19|20)\d{2}$", t)
        for t in tokens
    ):
        return False
    return True


# Multi-group tag separators: "(1r0n, Ushi)", "(1r0n & Ushi)", "(1r0n/Ushi)",
# "(1r0n + Ushi)", "(1r0n and Ushi)".  Each piece is validated individually so a
# format tag or a publisher can never enter a multi-group list (§12.11).
_GROUP_SPLIT_RE = re.compile(r"\s*(?:[,&+]|/|\band\b)\s*", re.IGNORECASE)


def split_release_group_value(value: str) -> List[str]:
    """Split a stored/tagged release-group value into validated group names."""
    if not value:
        return []
    groups: List[str] = []
    for part in _GROUP_SPLIT_RE.split(str(value).strip()):
        part = (part or "").strip()
        if not part:
            continue
        if not is_valid_group_candidate(part) or _is_known_publisher(part):
            continue
        if part not in groups:
            groups.append(part)
    return groups


def _split_group_list(candidate: str) -> Optional[List[str]]:
    """Split a multi-group bracket candidate into individually valid groups."""
    if not candidate or not _GROUP_SPLIT_RE.search(candidate):
        return None
    groups = split_release_group_value(candidate)
    return groups if groups else None


def extract_release_groups(raw_title: str, known_groups: Optional[List[str]] = None) -> List[str]:
    """Extract release groups as an ordered list; the first entry is primary.

    Ordering rules (position matters, not just vocabulary):

    1. known / prioritized groups win;
    2. otherwise the LAST valid bracket candidate is the group -- the convention is
       ``... (Publisher) (Group)``, so an unrecognised publisher-shaped token in an
       earlier bracket must never steal the group slot (§12.13.3);
    3. then the scene-style ``-GROUP`` suffix, then a bounded known-group substring.

    Format tags, publishers (``KNOWN_PUBLISHERS``), revision markers (``f``/``fN``)
    and single-character tokens are filtered out first.  "+"-joined candidates are
    split and validated one by one (§12.11), so ``(1r0n, Ushi)`` yields
    ``["1r0n", "Ushi"]`` rather than one concatenated pseudo-group.
    """
    if not raw_title or not isinstance(raw_title, str):
        return []

    groups_to_check = sorted(list(known_groups or []) + DEFAULT_KNOWN_GROUPS, key=len, reverse=True)

    # 1. Bracket candidates: split multi-group tags FIRST, then validate every
    #    piece independently (§12.11.1), so a noise word in the same block cannot
    #    hide a real group.  A recognised publisher block is never split.
    bracket_groups: List[List[str]] = []
    for b1, b2 in re.findall(r'\[([^\]]+)\]|\(([^\)]+)\)', raw_title):
        cand = (b1 or b2).strip()
        if not cand or _is_known_publisher(cand):
            continue
        split = _split_group_list(cand)
        if split is not None:
            bracket_groups.append(split)
            continue
        if is_valid_group_candidate(cand, known_groups=groups_to_check):
            bracket_groups.append([cand])

    ordered: List[str] = []

    def _add(name: str):
        if name and not _is_known_publisher(name) and name not in ordered:
            ordered.append(name)

    # 1a. Known/prioritized groups win (in bracket order).
    for parts in bracket_groups:
        for part in parts:
            for kg in groups_to_check:
                if part.lower() == kg.lower() and not _is_known_publisher(kg):
                    _add(part)

    # 1b. Prefer-last: the last valid bracket candidate is the conventional group
    #     slot; its "+"-split parts keep their order, so the first part is primary.
    for parts in reversed(bracket_groups):
        for part in parts:
            _add(part)

    if ordered:
        return ordered

    # 2. Scene-style suffix: -GROUP before trailing brackets or end of string.
    m_scene = re.search(r'[-–—]([A-Za-z0-9_]{2,30})(?:\s*[\(\[]|\s*$)', raw_title)
    if m_scene:
        cand = m_scene.group(1).strip()
        if is_valid_group_candidate(cand, known_groups=groups_to_check) and not _is_known_publisher(cand):
            return [cand]

    # 3. Bounded known-group substring search (publishers are not groups).
    for kg in groups_to_check:
        if _is_known_publisher(kg):
            continue
        escaped = re.escape(kg)
        if re.search(r'(?:^|[\s_.\-\(\[])' + escaped + r'(?:$|[\s_.\-\)\]])', raw_title, re.IGNORECASE):
            return [kg]

    return []


def _legacy_publisher_group(raw_title: str, known_groups: Optional[List[str]] = None) -> Optional[str]:
    """Backward-compatible publisher fallback for :func:`extract_release_group`.

    Legacy callers (VIP prioritisation, ingestion tests) expect a lone publisher
    tag such as ``[Yen Press]`` to still be returned by the single-value
    ``extract_release_group``.  The new list API deliberately excludes publishers
    (§12.13.1), so the legacy wrapper falls back to the publisher only when there
    is no real group at all.
    """
    if not raw_title or not isinstance(raw_title, str):
        return None
    groups_to_check = sorted(list(known_groups or []) + DEFAULT_KNOWN_GROUPS, key=len, reverse=True)
    publishers: List[str] = []
    for b1, b2 in re.findall(r'\[([^\]]+)\]|\(([^\)]+)\)', raw_title):
        cand = (b1 or b2).strip()
        if not _is_known_publisher(cand):
            continue
        if not is_valid_group_candidate(cand, known_groups=groups_to_check):
            continue
        publishers.append(cand)
    if not publishers:
        return None
    # A known-group name among the publishers wins (e.g. "[Kodansha USA] [Seven Seas]").
    for cand in publishers:
        for kg in groups_to_check:
            if cand.lower() == kg.lower():
                return kg
    return publishers[0]


def extract_release_group(raw_title: str, known_groups: Optional[List[str]] = None) -> Optional[str]:
    """
    Extracts the primary release group or publisher tag from raw release titles.
    e.g. '[Yen Press] Overlord Vol. 1' -> 'Yen Press'
         '[Seven Seas] [Stick] Mushoku Tensei v01' -> 'Stick'
         'Re:Zero Vol 1 (J-Novel Club)' -> 'J-Novel Club'
         '[danke-Empire] Bleach v01-v20 [Completed]' -> 'danke-Empire'

    Backward-compatible single-value wrapper over :func:`extract_release_groups`.
    The full ordered set is available from that function and is exposed as
    ``release_groups`` in the parse result.
    """
    groups = extract_release_groups(raw_title, known_groups=known_groups)
    if groups:
        return groups[0]
    return _legacy_publisher_group(raw_title, known_groups=known_groups)


def extract_publisher(raw_title: str) -> Optional[str]:
    """Return the publisher named in a bracket/paren block, or ``None``.

    Publishers are not scanlation groups: they are recognised separately from
    ``KNOWN_PUBLISHERS`` (``[Square Enix]``, ``(K-MANGA)``, ``[J-Novel Club]``, …)
    and surfaced as their own field rather than allowed to stand in for a group
    (§12.13.1/§12.13.3).  Either bracket style works; the bracket text is returned.
    """
    if not raw_title or not isinstance(raw_title, str):
        return None
    for candidate in re.findall(r"[\(\[]([^\)\]]+)[\)\]]", raw_title):
        candidate = candidate.strip()
        if not candidate:
            continue
        if _is_known_publisher(candidate):
            return candidate
        # A multi-tag block may still carry the publisher ("(Parser + K-MANGA)").
        for part in re.split(r"\s*(?:[,&+/]|\band\b)\s*", candidate, flags=re.IGNORECASE):
            part = (part or "").strip()
            if part and _is_known_publisher(part):
                return part
    return None

