"""Series identity resolution shared by Calibre matching, feed ingestion and the UI.

Naming model (ported from MangaTrax, authoritative):

* the **stable matching key** lives in ``series_tracking.name`` and
  ``releases.series_name``.  It is the Calibre series name when the series exists
  in Calibre, otherwise the name the user typed / the feed used.  Matching,
  grouping, de-duplication and Calibre lookups key off it, so **nothing in this
  module (or its callers) may rewrite it from external metadata**.
* the **display title** is cosmetic: AniList's canonical English title.  It is
  never fed back into a matching lookup.

The module is deliberately dependency-free (stdlib + ``app.parser`` only) so it
is importable in the hermetic test interpreter that has no fastapi/httpx/jinja2.
It performs no I/O: DB rows and AniList metadata are passed in by the caller.

Matching rules (contract §3):

1. query candidates: the whole title, ``|``/``/``/``;``/`` – ``/`` — `` splits,
   colon parts and progressive colon prefixes, parenthetical parts, the cleaned
   variant and AniList english/title/romaji/native/synonyms;
2. series candidates: Calibre name, display title, stored AniList titles and
   synonyms, alias-override source names, plus split/colon variants;
3. exact normalised match on any candidate pair -> unique target; a single
   candidate matching two different series is ambiguous -> ``None`` (manual match);
4. substring containment: both strings >= 6 chars and length ratio >= 0.70;
   two different series matching the same candidate -> ``None``;
5. fuzzy: full query vs full series name only, ``score > threshold``, length
   ratio >= 0.75, >= 1 shared token of length >= 4, agreeing title head tokens
   (MangaTrax fuzzy-head guard, ported in the Lead addendum), and a
   ``best - second >= AMBIGUITY_SCORE_DELTA`` margin;
6. AniList synonyms turn the correct Calibre spelling into an *exact* candidate;
7. two names that share an identical prefix but differ in a distinguishing
   qualifier ("... for Mobs" vs "... for Us, Too") never match via
   substring/fuzzy unless the match is exact.
"""

from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.parser import normalize_text, string_similarity

logger = logging.getLogger("noveltrax.series_identity")

# ---------------------------------------------------------------------------
# Tunables (contract §3)
# ---------------------------------------------------------------------------
CALIBRE_MATCH_THRESHOLD = 0.88
AUTO_LINK_FUZZY_THRESHOLD = 0.90
SUBSTRING_MIN_LEN = 6
SUBSTRING_MIN_RATIO = 0.70
AMBIGUITY_SCORE_DELTA = 0.02

#: Fuzzy matching only compares strings whose length ratio is at least this.
FUZZY_MIN_LEN_RATIO = 0.75
#: Fuzzy matching requires at least one shared word token this long (distinctive).
FUZZY_MIN_SHARED_TOKEN_LEN = 4
#: Meaningful-head token agreement (MangaTrax parser.py:1703-1734).
FUZZY_HEAD_TOKEN_RATIO = 0.80

#: Tokens ignored when computing the "head" of a title for the fuzzy guard.
_MATCH_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "at", "to", "and", "or", "is", "are",
    "be", "becomes", "became", "with", "for", "as", "its", "it", "this", "that",
    "another", "world", "new", "my", "his", "her", "their", "our", "from", "by",
    "was", "were", "been", "am", "has", "have", "had", "not", "but", "so",
    "if", "then", "than", "i", "you", "he", "she", "we", "they",
}

#: Distinguishing qualifier tokens used by rule 7 ("... for Mobs" vs "... for Us, Too").
_DISTINGUISHING_TOKENS = {"mobs", "mob"}

# ---------------------------------------------------------------------------
# Candidate splitting
# ---------------------------------------------------------------------------
# Alternate title separators: "A | B", "A / B", "A // B", "A; B", "A – B", "A — B".
# Colon is deliberately NOT a default delimiter: the whole title is always kept
# and colon parts/prefixes are only added as EXTRA (lower priority) candidates.
_CANDIDATE_SPLIT_RE = re.compile(r"\s*[|/]\s*|\s*;\s*|\s+[–—]\s+")
_COLON_SPLIT_RE = re.compile(r"\s*[:：]\s*")
_PAREN_CONTENT_RE = re.compile(r"\(([^()]+)\)")
_BRACE_TAG_RE = re.compile(r"\{[^{}]*\}")

#: Parenthetical contents that are release metadata, never title fragments.
_PAREN_NOISE_RE = re.compile(
    r"(?i)\b(?:epub|pdf|mobi|azw3|cbz|cbr|light\s+novel|web\s+novel|novel|ln|wn|"
    r"digital|retail|official|complete|completed|vol(?:ume)?s?\.?\s*\d|"
    r"book\s*\d|all\s+volumes?|box\s*set)\b"
)

#: Format tags in parentheses/brackets that are never part of a series title.
_FORMAT_PAREN_RE = re.compile(
    r"(?i)[\(\[]\s*(?:light\s+novel|web\s+novel|novel|ln|wn|epub|pdf|mobi|azw3|"
    r"cbz|cbr|digital|retail|official|complete|completed|all\s+volumes?|box\s*set)"
    r"\s*[\)\]]"
)
#: "001-130 as v01-13" style mappings and trailing volume/range clauses.
_TRAILING_AS_VOLUME_RE = re.compile(
    r"(?i)\s*[-\u2013\u2014:,|]?\s*\b\d{1,4}(?:\.\d+)?\s*[-\u2013\u2014~]\s*"
    r"\d{1,4}(?:\.\d+)?\s+as\s+v(?:ol(?:ume)?s?\.?)?\s*\d+(?:\.\d+)?"
    r"(?:\s*[-\u2013\u2014~]\s*\d+(?:\.\d+)?)?\s*$"
)
_TRAILING_VOLUME_RANGE_RE = re.compile(
    r"(?i)\s*[-\u2013\u2014:,|]?\s*\b(?:vol(?:ume)?s?\.?|v\.?|books?)\s*"
    r"\d+(?:\.\d+)?(?:\s*[-\u2013\u2014~]\s*\d+(?:\.\d+)?)?\s*$"
)
_TRAILING_BARE_RANGE_RE = re.compile(
    r"\s*[-\u2013\u2014:,|]?\s*\d{1,4}(?:\.\d+)?\s*[-\u2013\u2014~]\s*"
    r"\d{1,4}(?:\.\d+)?\s*$"
)


def _coerce_anilist_meta(meta: Optional[Any]) -> Dict[str, Any]:
    """Return an AniList metadata dict from a dict OR a JSON string row value."""
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, str) and meta.strip():
        try:
            parsed = json.loads(meta)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _dedupe_texts(values: Iterable[Any]) -> List[str]:
    """Strip/dedupe text values by normalised form, preserving order."""
    result: List[str] = []
    seen: Set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text:
            continue
        norm = normalize_text(text)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        result.append(text)
    return result


def _acceptable_candidate(value: Any) -> bool:
    """Drop empty candidates and single tokens shorter than 3 characters."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    norm = normalize_text(text)
    if not norm:
        return False
    tokens = norm.split()
    if len(tokens) == 1 and len(tokens[0]) < 3:
        return False
    return True


def normalize_series(name: str) -> str:
    """Normalise a series spelling for comparison (``app.parser.normalize_text``)."""
    return normalize_text(name)


def clean_series_for_matching(name: str) -> str:
    """Conservative cleanup of a series spelling before matching.

    Removes brace release tags, format parentheses and trailing volume/range
    clauses ("[OSHI NO KO] 001-130 as v01-13" -> "[OSHI NO KO]").  Parenthesised
    or bracketed parts of real titles are preserved; only recognised
    format/volume noise is stripped.
    """
    text = str(name or "").strip()
    if not text:
        return ""
    cleaned = _BRACE_TAG_RE.sub(" ", text)
    cleaned = _FORMAT_PAREN_RE.sub(" ", cleaned)
    for _ in range(3):
        previous = cleaned
        cleaned = _TRAILING_AS_VOLUME_RE.sub("", cleaned)
        cleaned = _TRAILING_VOLUME_RANGE_RE.sub("", cleaned)
        cleaned = _TRAILING_BARE_RANGE_RE.sub("", cleaned)
        if cleaned == previous:
            break
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -\u2013\u2014:,|./")
    return cleaned or text.strip(" -\u2013\u2014:,|./")


def anilist_title_list(meta: Optional[Any]) -> List[str]:
    """Title spellings carried by an AniList metadata dict.

    Order: english, title/canonical, romaji, native, then synonyms.
    """
    data = _coerce_anilist_meta(meta)
    if not data:
        return []
    values: List[Any] = []
    for key in ("english_title", "title", "canonical_title", "romaji_title", "native_title"):
        values.append(data.get(key))
    synonyms = data.get("synonyms")
    if isinstance(synonyms, (list, tuple, set)):
        values.extend(synonyms)
    elif isinstance(synonyms, str):
        values.append(synonyms)
    return _dedupe_texts(values)


def _parenthetical_parts(title: str) -> List[str]:
    parts: List[str] = []
    for match in _PAREN_CONTENT_RE.finditer(title or ""):
        inner = (match.group(1) or "").strip()
        if not inner or _PAREN_NOISE_RE.search(inner):
            continue
        parts.append(inner)
    return parts


def _split_parts(title: str) -> List[str]:
    """Alternate-title parts on ``|``, ``/``, ``;``, en/em dash separators."""
    if not title:
        return []
    return [part.strip() for part in _CANDIDATE_SPLIT_RE.split(title) if part.strip()]


def _colon_variants(title: str) -> List[str]:
    """Colon parts and progressive colon prefixes, longest/most specific first."""
    parts = [part.strip() for part in _COLON_SPLIT_RE.split(title or "") if part.strip()]
    if len(parts) < 2:
        return []
    variants: List[str] = []
    # Full prefixes (A: B, A: B: C ...) longest first; the whole title is added
    # by the caller, so start at len-1.
    for length in range(len(parts) - 1, 1, -1):
        variants.append(": ".join(parts[:length]))
    # Individual parts last (least specific).
    variants.extend(parts)
    return variants


def _structural_variants(title: str) -> List[str]:
    """Split parts, colon parts/prefixes and parenthetical parts of *title*."""
    if not title:
        return []
    variants: List[str] = []
    variants.extend(_split_parts(title))
    variants.extend(_colon_variants(title))
    variants.extend(_parenthetical_parts(title))
    return variants


def title_candidates(raw_title: str, anilist_meta: Optional[Any] = None) -> List[str]:
    """Plausible spellings of *raw_title* for matching.

    Includes the whole title, alternate-title splits, colon parts and progressive
    colon prefixes, parenthetical parts, the cleaned variant and every AniList
    english/title/romaji/native/synonym spelling.  Never returns empty or
    one-token (<3 chars) candidates.
    """
    raw = str(raw_title or "").strip()
    meta = _coerce_anilist_meta(anilist_meta)
    values: List[Any] = [raw]
    if raw:
        values.append(clean_series_for_matching(raw))
        values.extend(_structural_variants(raw))
    if meta:
        for meta_title in anilist_title_list(meta):
            values.append(meta_title)
            values.append(clean_series_for_matching(meta_title))
            values.extend(_split_parts(meta_title))
    candidates: List[str] = []
    seen: Set[str] = set()
    for value in values:
        if not _acceptable_candidate(value):
            continue
        norm = normalize_text(value)
        if norm in seen:
            continue
        seen.add(norm)
        candidates.append(value.strip())
    return candidates


# ---------------------------------------------------------------------------
# Fuzzy guards (ported from MangaTrax parser.py:1703-1734, Lead addendum)
# ---------------------------------------------------------------------------
def _fuzzy_head_tokens(text: str, limit: int = 2) -> List[str]:
    """First *limit* meaningful (non-stopword, non-numeric) tokens of a title."""
    tokens = [token for token in normalize_text(text).split() if token]
    meaningful = [
        token for token in tokens
        if token not in _MATCH_STOPWORDS and not token.isdigit()
    ]
    return meaningful[:limit]


def _fuzzy_head_agrees(query: str, candidate: str) -> bool:
    """True when the two titles share a distinctive leading token.

    Stops fuzzy similarity that comes only from a shared generic suffix:
    "The Red Ranger Becomes an Adventurer in Another World" must not fuzzy-match
    "Level 0 Demon King Becomes an Adventurer in Another World".
    """
    query_heads = _fuzzy_head_tokens(query)
    candidate_heads = _fuzzy_head_tokens(candidate)
    if not query_heads or not candidate_heads:
        return False
    for left in query_heads:
        for right in candidate_heads:
            if left == right:
                return True
            if SequenceMatcher(None, left, right).ratio() >= FUZZY_HEAD_TOKEN_RATIO:
                return True
    return False


def _shared_long_token(query_norm: str, series_norm: str) -> bool:
    """True when both normalised strings share a word token of length >= 4."""
    query_tokens = {
        token for token in query_norm.split()
        if len(token) >= FUZZY_MIN_SHARED_TOKEN_LEN
    }
    if not query_tokens:
        return False
    series_tokens = {
        token for token in series_norm.split()
        if len(token) >= FUZZY_MIN_SHARED_TOKEN_LEN
    }
    return bool(query_tokens & series_tokens)


def _token_common_prefix(left: Sequence[str], right: Sequence[str]) -> int:
    count = 0
    while count < min(len(left), len(right)) and left[count] == right[count]:
        count += 1
    return count


def _has_distinguishing_divergence(query_norm: str, series_norm: str) -> bool:
    """Rule 7: shared prefix + a distinguishing qualifier means "different series".

    "Trapped in a Dating Sim: The World of Otome Games is Tough for Us, Too"
    (#327) and "... is Tough for Mobs, Too" (#174) share a long identical prefix;
    a qualifier token ("mobs") on only one side, or an "us too"-style tail on only
    one side, must never be crossed by substring/fuzzy matching.
    """
    query_tokens = query_norm.split()
    series_tokens = series_norm.split()
    if not query_tokens or not series_tokens:
        return False
    common = _token_common_prefix(query_tokens, series_tokens)
    if common < 3:
        return False
    shorter = min(len(query_tokens), len(series_tokens))
    if shorter <= 0 or (common / shorter) < 0.5:
        return False
    query_rest = query_tokens[common:]
    series_rest = series_tokens[common:]
    if not query_rest or not series_rest:
        return False
    query_only = set(query_rest) - set(series_rest)
    series_only = set(series_rest) - set(query_rest)
    if (query_only | series_only) & _DISTINGUISHING_TOKENS:
        return True
    # "... for Us, Too" tail present on only one side of the divergence.
    if ("too" in query_only) != ("too" in series_only):
        return True
    return False


# ---------------------------------------------------------------------------
# Calibre library index
# ---------------------------------------------------------------------------
def _series_name(series: Dict[str, Any]) -> str:
    """Stable name of a Calibre series dict / series_tracking row."""
    if not isinstance(series, dict):
        return ""
    for key in ("series_name", "name", "calibre_series_name"):
        value = series.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _series_identity_key(series: Dict[str, Any]) -> Tuple[str, str]:
    """Identity of a series dict: its Calibre id when present, else its name."""
    if isinstance(series, dict):
        for key in ("series_id", "id", "calibre_series_id"):
            value = series.get(key)
            if value not in (None, ""):
                return ("id", str(value))
    return ("name", normalize_text(_series_name(series)))


def _series_entry_norms(series: Dict[str, Any]) -> List[str]:
    """Normalised spellings a Calibre series row may be exactly matched by.

    The full stable name, ``display_title``, stored AniList titles and the same
    alternate-title/colon split variants the query side produces, so a bare
    franchise spelling ("Mushoku Tensei") can still exact-match the Calibre
    subtitle form ("Mushoku Tensei: Jobless Reincarnation").  When such a short
    variant is shared by two Calibre series the exact phase reports ambiguity and
    ``match()`` returns ``None`` (manual match) instead of guessing.
    """
    values: List[Any] = []
    name = _series_name(series)
    if not name:
        return []
    values.append(name)
    display = series.get("display_title")
    if isinstance(display, str) and display.strip():
        values.append(display)
    meta = _coerce_anilist_meta(
        series.get("anilist_meta") or series.get("anilist_meta_json")
    )
    values.extend(anilist_title_list(meta))
    norms: List[str] = []
    seen: Set[str] = set()
    for value in values:
        if not _acceptable_candidate(value):
            continue
        spellings = [value] + _structural_variants(value)
        for spelling in spellings:
            if not _acceptable_candidate(spelling):
                continue
            norm = normalize_text(spelling)
            if norm and norm not in seen:
                seen.add(norm)
                norms.append(norm)
    return norms


def _series_trigrams(norm: str) -> Set[str]:
    """Character 3-grams of a normalised name (fuzzy candidate index key)."""
    if not norm:
        return set()
    if len(norm) <= 3:
        return {norm}
    return {norm[index:index + 3] for index in range(len(norm) - 2)}


class CalibreSeriesIndex:
    """Precomputed lookup index over a Calibre series list (built once per sync).

    ``match()`` is the single implementation of the Calibre matching rules: exact
    (multi-candidate, ambiguity -> ``None``), strict substring containment
    (>= 6 chars, length ratio >= 0.70, ambiguity -> ``None``) and conservative
    fuzzy scoring (see the module docstring).  The library is normalised once;
    per-query work is an O(1) exact dict lookup, length-bucketed substring passes
    and a narrowing token/3-gram candidate set for fuzzy scoring.
    """

    __slots__ = (
        "_entries", "_by_norm", "_by_length", "_by_token", "_by_gram",
    )

    #: Tokens shared by more series than this are too generic to narrow a fuzzy match.
    _FUZZY_RARE_TOKEN_BUCKET = 200
    #: Only very small 3-gram buckets are OR-ed in on top of the token candidates.
    _FUZZY_TINY_GRAM_BUCKET = 20

    def __init__(self, calibre_series_list: Optional[Sequence[Dict[str, Any]]]):
        entries: List[Tuple[str, Dict[str, Any]]] = []
        by_norm: Dict[str, List[int]] = {}
        by_length: Dict[int, List[int]] = {}
        by_token: Dict[str, List[int]] = {}
        by_gram: Dict[str, List[int]] = {}
        for series in calibre_series_list or []:
            if not isinstance(series, dict):
                continue
            norm = normalize_text(_series_name(series))
            if not norm:
                continue
            index = len(entries)
            entries.append((norm, series))
            for entry_norm in _series_entry_norms(series):
                by_norm.setdefault(entry_norm, []).append(index)
            by_length.setdefault(len(norm), []).append(index)
            for gram in _series_trigrams(norm):
                by_gram.setdefault(gram, []).append(index)
            for token in set(norm.split()):
                by_token.setdefault(token, []).append(index)
        self._entries = entries
        self._by_norm = by_norm
        self._by_length = by_length
        self._by_token = by_token
        self._by_gram = by_gram

    def __len__(self) -> int:
        return len(self._entries)

    # -- query candidate construction -----------------------------------
    @staticmethod
    def _query_candidates(
        extracted_series: str,
        alt_names: Optional[Sequence[str]] = None,
    ) -> List[str]:
        """Ordered, de-duplicated query candidates (most specific first).

        Whole title -> AniList alt names (synonyms drive EXACT matches) -> cleaned
        variant -> split parts -> colon prefixes/parts -> parenthetical parts.
        """
        raw = str(extracted_series or "").strip()
        alts = _dedupe_texts([str(a) for a in (alt_names or []) if isinstance(a, str)])
        values: List[Any] = [raw]
        values.extend(alts)
        if raw:
            cleaned = clean_series_for_matching(raw)
            values.append(cleaned)
            values.extend(_split_parts(raw))
            values.extend(_colon_variants(raw))
            values.extend(_parenthetical_parts(raw))
        for alt in alts:
            cleaned_alt = clean_series_for_matching(alt)
            values.append(cleaned_alt)
            values.extend(_split_parts(alt))
        candidates: List[str] = []
        seen: Set[str] = set()
        for value in values:
            if not _acceptable_candidate(value):
                continue
            norm = normalize_text(value)
            if norm in seen:
                continue
            seen.add(norm)
            candidates.append(value.strip())
        return candidates

    # -- exact / substring / fuzzy helpers ------------------------------
    @staticmethod
    def _unique_series(
        indices: Iterable[int],
        entries: Sequence[Tuple[str, Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """Distinct series dicts among *indices* (duplicate rows collapse by identity)."""
        seen: Set[Tuple[str, str]] = set()
        result: List[Dict[str, Any]] = []
        for index in indices:
            series = entries[index][1]
            key = _series_identity_key(series)
            if key in seen:
                continue
            seen.add(key)
            result.append(series)
        return result

    def _exact_match(self, candidates: Sequence[str]) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Return ``(match, ambiguous)`` for the exact phase."""
        ambiguous = False
        for candidate in candidates:
            norm = normalize_text(candidate)
            indices = self._by_norm.get(norm)
            if not indices:
                continue
            unique = self._unique_series(indices, self._entries)
            if len(unique) == 1:
                return unique[0], False
            if len(unique) > 1:
                ambiguous = True
                logger.info(
                    "Ambiguous exact series candidate '%s' matches %d Calibre series; "
                    "leaving for manual match.",
                    candidate, len(unique),
                )
        return None, ambiguous

    def _substring_match(self, candidates: Sequence[str]) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Return ``(match, ambiguous)`` for the strict substring-containment phase."""
        for candidate in candidates:
            norm_candidate = normalize_text(candidate)
            if len(norm_candidate) < SUBSTRING_MIN_LEN:
                continue
            low = max(SUBSTRING_MIN_LEN, int(round(len(norm_candidate) * SUBSTRING_MIN_RATIO)))
            high = int(len(norm_candidate) / SUBSTRING_MIN_RATIO) + 1
            matched: List[int] = []
            for length in range(low, high + 1):
                for index in self._by_length.get(length, ()):
                    norm_series = self._entries[index][0]
                    shorter, longer = (
                        (norm_series, norm_candidate)
                        if len(norm_series) <= len(norm_candidate)
                        else (norm_candidate, norm_series)
                    )
                    if len(longer) == 0:
                        continue
                    if (len(shorter) / len(longer)) < SUBSTRING_MIN_RATIO:
                        continue
                    if len(shorter) < SUBSTRING_MIN_LEN:
                        continue
                    if shorter in longer and not _has_distinguishing_divergence(
                        norm_candidate, norm_series
                    ):
                        matched.append(index)
            unique = self._unique_series(matched, self._entries)
            if len(unique) == 1:
                return unique[0], False
            if len(unique) > 1:
                logger.info(
                    "Ambiguous substring series candidate '%s' matches %d Calibre "
                    "series; leaving for manual match.",
                    candidate, len(unique),
                )
                return None, True
        return None, False

    def _fuzzy_indices(self, norm_query: str) -> List[int]:
        """Narrowed, length-bounded indices for fuzzy scoring."""
        low = int(len(norm_query) * FUZZY_MIN_LEN_RATIO)
        high = int(len(norm_query) / FUZZY_MIN_LEN_RATIO)
        candidate_indices: Set[int] = set()
        for token in set(norm_query.split()):
            bucket = self._by_token.get(token)
            if bucket and len(bucket) <= self._FUZZY_RARE_TOKEN_BUCKET:
                candidate_indices.update(bucket)
        for gram in _series_trigrams(norm_query):
            bucket = self._by_gram.get(gram)
            if bucket and len(bucket) <= self._FUZZY_TINY_GRAM_BUCKET:
                candidate_indices.update(bucket)
        if not candidate_indices:
            # Rare single-token query or a library where every bucket is huge:
            # fall back to the (already length-bounded) full entry list.
            return [
                index for index, (norm_series, _series) in enumerate(self._entries)
                if low <= len(norm_series) <= high
            ]
        return [
            index for index in sorted(candidate_indices)
            if low <= len(self._entries[index][0]) <= high
        ]

    def _fuzzy_match(self, extracted_series: str, threshold: float) -> Optional[Dict[str, Any]]:
        norm_query = normalize_text(extracted_series)
        if not norm_query:
            return None
        best_score = threshold
        best_series: Optional[Dict[str, Any]] = None
        best_key: Optional[Tuple[str, str]] = None
        second_score = 0.0
        second_key: Optional[Tuple[str, str]] = None
        for index in self._fuzzy_indices(norm_query):
            norm_series, series = self._entries[index]
            if len(norm_series) < 4:
                continue
            shorter, longer = sorted((len(norm_series), len(norm_query)))
            if longer == 0 or (shorter / longer) < FUZZY_MIN_LEN_RATIO:
                continue
            if _has_distinguishing_divergence(norm_query, norm_series):
                logger.info(
                    "Rejected fuzzy series match '%s' -> '%s': distinguishing qualifier "
                    "differs (e.g. 'for Mobs' vs 'for Us, Too').",
                    extracted_series, norm_series,
                )
                continue
            if not _fuzzy_head_agrees(norm_query, norm_series):
                logger.info(
                    "Rejected fuzzy series match '%s' -> '%s': title head tokens disagree.",
                    extracted_series, norm_series,
                )
                continue
            if not _shared_long_token(norm_query, norm_series):
                continue
            score = string_similarity(extracted_series, norm_series)
            if score <= best_score:
                if score > second_score:
                    second_score = score
                    second_key = _series_identity_key(series)
                continue
            key = _series_identity_key(series)
            if best_series is not None:
                second_score = max(second_score, best_score)
                second_key = best_key
            best_score = score
            best_series = series
            best_key = key
        if best_series is None:
            return None
        if (
            second_key is not None
            and second_key != best_key
            and (best_score - second_score) < AMBIGUITY_SCORE_DELTA
        ):
            logger.info(
                "Ambiguous fuzzy series match '%s': %.3f vs %.3f within %.2f; "
                "leaving for manual match.",
                extracted_series, best_score, second_score, AMBIGUITY_SCORE_DELTA,
            )
            return None
        logger.info(
            "Accepted fuzzy series match '%s' -> '%s' (score %.3f).",
            extracted_series, _series_name(best_series), best_score,
        )
        return best_series

    # -- public API -----------------------------------------------------
    def match(
        self,
        extracted_series: str,
        alt_names: Optional[Sequence[str]] = None,
        threshold: float = CALIBRE_MATCH_THRESHOLD,
    ) -> Optional[Dict[str, Any]]:
        """Match *extracted_series* (plus optional alt spellings) to a Calibre series.

        Returns the matched Calibre series dict or ``None`` (unknown/ambiguous;
        callers offer the manual picker).  ``alt_names`` (AniList titles, synonyms,
        alias-override sources) are tried in the exact phase after the primary
        name, so a synonym that equals the Calibre string is an exact match.
        """
        if not extracted_series or not self._entries:
            return None
        if not normalize_text(extracted_series):
            return None
        candidates = self._query_candidates(extracted_series, alt_names)

        match, ambiguous = self._exact_match(candidates)
        if match is not None:
            return match
        if ambiguous:
            return None

        match, ambiguous = self._substring_match(candidates)
        if match is not None:
            return match
        if ambiguous:
            return None

        # Fuzzy scoring deliberately uses the FULL query string (not fragments),
        # so a colon fragment cannot fuzzy-match an unrelated franchise.
        return self._fuzzy_match(extracted_series, threshold)


# ---------------------------------------------------------------------------
# Series resolution (tracked rows, aliases, AniList titles)
# ---------------------------------------------------------------------------
class SeriesResolution:
    """Result of :func:`resolve_series_title` (attribute- and dict-friendly)."""

    __slots__ = (
        "canonical_name", "display_title", "series_id", "calibre_series_id",
        "matched", "source", "title",
    )

    def __init__(
        self,
        canonical_name: str,
        display_title: str,
        series_id: Optional[int] = None,
        calibre_series_id: Optional[int] = None,
        matched: bool = False,
        source: str = "none",
        title: str = "",
    ):
        self.canonical_name = canonical_name
        self.display_title = display_title
        self.series_id = series_id
        self.calibre_series_id = calibre_series_id
        self.matched = matched
        self.source = source
        self.title = title

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.__slots__:
            value = getattr(self, key)
            return default if value is None and key in ("series_id", "calibre_series_id") else value
        return default

    def __getitem__(self, key: str) -> Any:
        if key not in self.__slots__:
            raise KeyError(key)
        return getattr(self, key)

    def __contains__(self, key: object) -> bool:
        return key in self.__slots__

    def keys(self) -> List[str]:
        return list(self.__slots__)

    def items(self) -> List[Tuple[str, Any]]:
        return [(key, getattr(self, key)) for key in self.__slots__]

    def values(self) -> List[Any]:
        return [getattr(self, key) for key in self.__slots__]

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.items())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            "SeriesResolution(canonical_name=%r, display_title=%r, series_id=%r, "
            "calibre_series_id=%r, matched=%r, source=%r)"
            % (
                self.canonical_name, self.display_title, self.series_id,
                self.calibre_series_id, self.matched, self.source,
            )
        )


def _resolution_from_row(
    row: Dict[str, Any],
    source: str,
    title: str,
) -> SeriesResolution:
    name = _series_name(row) or ""
    display = ""
    if isinstance(row.get("display_title"), str):
        display = row["display_title"].strip()
    if not display:
        meta = _coerce_anilist_meta(
            row.get("anilist_meta") or row.get("anilist_meta_json")
        )
        titles = anilist_title_list(meta)
        if titles:
            display = titles[0]
    return SeriesResolution(
        canonical_name=name,
        display_title=display or name,
        series_id=row.get("id"),
        calibre_series_id=row.get("calibre_series_id"),
        matched=True,
        source=source,
        title=title,
    )


def _unknown_resolution(title: str) -> SeriesResolution:
    raw = str(title or "").strip()
    return SeriesResolution(
        canonical_name=raw,
        display_title=raw,
        series_id=None,
        calibre_series_id=None,
        matched=False,
        source="none",
        title=raw,
    )


def _row_key(row: Dict[str, Any]) -> Tuple[str, str]:
    if row.get("id") not in (None, ""):
        return ("id", str(row.get("id")))
    return ("name", normalize_text(_series_name(row)))


def _row_source_variants(row: Dict[str, Any]) -> List[Tuple[str, str]]:
    """``(normalised spelling, source)`` variants of a tracked series row.

    Sources: ``name`` (stable key), ``display``, ``anilist`` (stored meta) and
    ``split`` (alternate-title parts).  Colon parts are included only as
    ``split`` low-priority variants.
    """
    name = _series_name(row)
    if not name:
        return []
    variants: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    def add(value: Any, source: str) -> None:
        if not _acceptable_candidate(value):
            return
        norm = normalize_text(value)
        if norm and norm not in seen:
            seen.add(norm)
            variants.append((norm, source))

    add(name, "name")
    display = row.get("display_title")
    if isinstance(display, str):
        add(display, "display")
        for part in _split_parts(display):
            add(part, "split")
    meta = _coerce_anilist_meta(row.get("anilist_meta") or row.get("anilist_meta_json"))
    for meta_title in anilist_title_list(meta):
        add(meta_title, "anilist")
        for part in _split_parts(meta_title):
            add(part, "split")
    for part in _split_parts(name):
        add(part, "split")
    for part in _colon_variants(name):
        add(part, "split")
    return variants


def build_alias_lookup(
    series_rows: Optional[Sequence[Dict[str, Any]]],
    aliases: Optional[Sequence[Dict[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    """Map normalised alternate spellings to their tracked/override target.

    Explicit ``series_alias_overrides`` always win over spellings derived from
    tracked rows.  Each value is a dict with ``target_name`` (the stable key),
    ``target_norm``, ``canonical_name``, ``calibre_series_id``, ``series_id``,
    ``origin``, ``override_id``/``source_name`` and ``kind``
    (``"override"`` | ``"tracked"``).
    """
    lookup: Dict[str, Dict[str, Any]] = {}
    for override in aliases or []:
        if not isinstance(override, dict):
            continue
        target = str(override.get("target_name") or "").strip()
        source_norm = normalize_text(
            str(override.get("source_norm") or override.get("source_name") or "")
        )
        if not target or not source_norm:
            continue
        # A deliberate source==target row is kept: it shadows a stale display/
        # AniList spelling after a synonym split into its own series.
        lookup[source_norm] = {
            "target_name": target,
            "target_norm": normalize_text(target),
            "canonical_name": target,
            "calibre_series_id": override.get("calibre_series_id"),
            "series_id": None,
            "origin": override.get("origin") or "manual",
            "override_id": override.get("id"),
            "source_name": override.get("source_name"),
            "kind": "override",
        }
    for row in series_rows or []:
        if not isinstance(row, dict):
            continue
        name = _series_name(row)
        if not name:
            continue
        for norm, source in _row_source_variants(row):
            if norm == normalize_text(name):
                continue
            if norm in lookup:
                continue
            lookup[norm] = {
                "target_name": name,
                "target_norm": normalize_text(name),
                "canonical_name": name,
                "calibre_series_id": row.get("calibre_series_id"),
                "series_id": row.get("id"),
                "origin": source,
                "override_id": None,
                "source_name": name,
                "kind": "tracked",
            }
    return lookup


def resolve_series_title(
    title: str,
    series_rows: Optional[Sequence[Dict[str, Any]]],
    aliases: Optional[Sequence[Dict[str, Any]]] = None,
    anilist_meta: Optional[Any] = None,
) -> SeriesResolution:
    """Resolve *title* to a tracked series (stable key + display title).

    Precedence: alias overrides first, then a title lookup built from tracked
    rows (stable name, display title, stored AniList titles/synonyms), then
    normalised exact matching.  Unknown titles return
    ``matched=False, source='none'`` with ``canonical_name`` = the stripped title
    so callers can fall back to Calibre fuzzy matching.  Ambiguous matches are
    treated as unknown (never guess).
    """
    raw = str(title or "").strip()
    if not raw:
        return _unknown_resolution(raw)
    meta = _coerce_anilist_meta(anilist_meta)
    candidates = title_candidates(raw, meta)

    alias_lookup = build_alias_lookup((), aliases)
    for candidate in candidates:
        entry = alias_lookup.get(normalize_text(candidate))
        if not entry:
            continue
        target = entry["target_name"]
        row = _find_row_by_name(series_rows, target)
        if row is not None:
            return _resolution_from_row(row, "alias", raw)
        return SeriesResolution(
            canonical_name=target,
            display_title=target,
            series_id=entry.get("series_id"),
            calibre_series_id=entry.get("calibre_series_id"),
            matched=True,
            source="alias",
            title=raw,
        )

    row_lookup: Dict[str, Dict[Tuple[str, str], Tuple[Dict[str, Any], str]]] = {}
    for row in series_rows or []:
        if not isinstance(row, dict):
            continue
        key = _row_key(row)
        for norm, source in _row_source_variants(row):
            bucket = row_lookup.setdefault(norm, {})
            if key not in bucket:
                bucket[key] = (row, source)
            else:
                # Keep the strongest source for the same row (name > anilist > display > split).
                rank = {"name": 0, "anilist": 1, "display": 2, "split": 3}
                existing_source = bucket[key][1]
                if rank.get(source, 9) < rank.get(existing_source, 9):
                    bucket[key] = (row, source)

    ambiguous = False
    for candidate in candidates:
        bucket = row_lookup.get(normalize_text(candidate))
        if not bucket:
            continue
        if len(bucket) == 1:
            row, source = next(iter(bucket.values()))
            resolution_source = "exact" if source == "name" else (
                "anilist" if source in ("anilist", "display") else "exact"
            )
            return _resolution_from_row(row, resolution_source, raw)
        ambiguous = True
        logger.info(
            "Ambiguous resolved series candidate '%s' matches %d tracked series; "
            "leaving for manual match.",
            candidate, len(bucket),
        )
    if ambiguous:
        return _unknown_resolution(raw)
    return _unknown_resolution(raw)


def _find_row_by_name(
    series_rows: Optional[Sequence[Dict[str, Any]]],
    name: str,
) -> Optional[Dict[str, Any]]:
    target_norm = normalize_text(name)
    for row in series_rows or []:
        if isinstance(row, dict) and normalize_text(_series_name(row)) == target_norm:
            return row
    return None
