import re
import json
import logging
import math
from typing import Optional, Dict, Any, List
import time
import threading
import collections
import urllib.parse
import urllib.request
import urllib.error

try:
    import httpx
except ImportError:
    httpx = None

from app.security import safe_client_get, safe_client_request

logger = logging.getLogger("noveltrax.providers.metadata")

DEFAULT_HEADERS = {
    "User-Agent": "NovelTrax/1.0 (https://github.com/Wemew/NovelTrax)",
    "Accept": "application/json"
}

# ---------------------------------------------------------------------------
# AniList publication metadata (contract v3 §2)
# ---------------------------------------------------------------------------
#: AniList MediaFormat values normalised to our canonical form that count as a
#: light novel.  ``format_in: [NOVEL]`` already restricts the GraphQL query, but
#: the mapping must stay defensive: only the NOVEL family may drive bundle
#: volume inference.
_NOVEL_FORMAT_KEYS = {"NOVEL", "LIGHT_NOVEL"}

#: Display labels for the known AniList MediaStatus values.
STATUS_LABELS = {
    "FINISHED": "Finished",
    "RELEASING": "Releasing",
    "NOT_YET_RELEASED": "Not Yet Released",
    "HIATUS": "Hiatus",
    "CANCELLED": "Cancelled",
}

#: Reverse map used when only a previously-stored label is available.
_STATUS_LABEL_TO_RAW = {label.lower(): raw for raw, label in STATUS_LABELS.items()}

#: The normalized publication keys carried by every metadata result/cache row.
PUBLICATION_KEYS = (
    "format",
    "format_label",
    "is_light_novel",
    "status",
    "status_label",
    "volumes",
    "chapters",
    "total_volumes",
)


def _publication_token(value: Any) -> str:
    """Normalise a raw AniList enum value (``Light Novel`` -> ``LIGHT_NOVEL``)."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return re.sub(r"[^A-Za-z0-9]+", "_", text.upper()).strip("_")


def _publication_text(value: Any) -> str:
    """Best-effort stripped text for publication fields."""
    if value is None:
        return ""
    return str(value).strip()


def _publication_int(value: Any) -> Optional[int]:
    """Coerce a publication count: only finite integers >= 0 are accepted."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0 or not value.is_integer():
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        if not math.isfinite(number) or number < 0 or not number.is_integer():
            return None
        return int(number)
    return None


def _prettify_publication_value(value: Any) -> str:
    """Prettify an enum-ish value: ``NOT_YET_RELEASED`` -> ``Not Yet Released``."""
    token = _publication_token(value)
    if not token:
        return ""
    return token.replace("_", " ").title()


def _coerce_publication_mapping(meta: Any) -> Dict[str, Any]:
    """Return a dict from a metadata mapping, JSON string, or nothing."""
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, (bytes, bytearray)):
        try:
            meta = bytes(meta).decode("utf-8", "replace")
        except Exception:
            return {}
    if isinstance(meta, str) and meta.strip():
        try:
            parsed = json.loads(meta)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def publication_defaults() -> Dict[str, Any]:
    """The empty publication dict (all keys present, nothing known)."""
    return {
        "format": "",
        "format_label": "",
        "is_light_novel": False,
        "status": "",
        "status_label": "",
        "volumes": None,
        "chapters": None,
        "total_volumes": None,
    }

class SlidingWindowRateLimiter:
    """
    Thread-safe sliding-window rate limiter for external metadata APIs (AniList & Kitsu).
    Enforces request limits per sliding time window and dynamically respects
    X-RateLimit-Remaining and Retry-After HTTP headers.
    """
    def __init__(self, name: str, max_requests: int = 60, time_window: float = 60.0):
        self.name = name
        self.max_requests = max_requests
        self.time_window = time_window
        self.lock = threading.Lock()
        self.timestamps = collections.deque()
        self.retry_until: float = 0.0

    def wait_if_needed(self):
        with self.lock:
            now = time.time()
            if self.retry_until > now:
                wait_time = self.retry_until - now
                logger.warning(f"[{self.name}] Cooldown active. Pausing for {wait_time:.1f}s...")
                time.sleep(wait_time)
                now = time.time()

            while self.timestamps and (now - self.timestamps[0]) >= self.time_window:
                self.timestamps.popleft()

            if len(self.timestamps) >= self.max_requests:
                wait_time = self.time_window - (now - self.timestamps[0]) + 0.05
                if wait_time > 0:
                    logger.info(f"[{self.name}] Rate limit reached ({len(self.timestamps)}/{self.max_requests}). Pausing for {wait_time:.2f}s...")
                    time.sleep(wait_time)
                    now = time.time()
                    while self.timestamps and (now - self.timestamps[0]) >= self.time_window:
                        self.timestamps.popleft()

            self.timestamps.append(time.time())

    def handle_response_headers(self, headers):
        if not headers:
            return
        try:
            remaining = None
            if hasattr(headers, "get"):
                remaining = headers.get("X-RateLimit-Remaining") or headers.get("x-ratelimit-remaining")
            if remaining is not None:
                rem_val = int(remaining)
                if rem_val <= 2:
                    logger.warning(f"[{self.name}] Quota nearly exhausted ({rem_val} remaining). Pacing...")
                    with self.lock:
                        time.sleep(1.0)
        except Exception:
            pass

    def handle_rate_limit_error(self, headers=None):
        wait_secs = 60.0
        if headers and hasattr(headers, "get"):
            retry_after = headers.get("Retry-After") or headers.get("retry-after")
            if retry_after:
                try:
                    wait_secs = max(1.0, float(retry_after))
                except ValueError:
                    wait_secs = 60.0
        logger.warning(f"[{self.name}] 429 Too Many Requests received. Setting rate-limit cooldown for {wait_secs}s.")
        with self.lock:
            self.retry_until = max(self.retry_until, time.time() + wait_secs)

_ANILIST_LIMITER = SlidingWindowRateLimiter("AniList", max_requests=90, time_window=60.0)
_KITSU_LIMITER = SlidingWindowRateLimiter("Kitsu", max_requests=30, time_window=60.0)

class NovelMetadataProvider:
    """
    Fetches rich novel metadata (synopses, official cover artwork, canonical titles)
    from public community databases (Kitsu and AniList).
    Operates as an automatic fallback when source novel post has no synopsis.
    """
    _default_db = None

    @classmethod
    def set_db(cls, db):
        cls._default_db = db

    @staticmethod
    def clean_search_title(raw_title: str) -> str:
        """
        Strips release noise (epub, pdf, volume numbers, subtitle cruft)
        to form an accurate database search query. Preserves colons in titles.
        """
        if not raw_title:
            return ""
        t = raw_title
        # Remove format indicators and bracketed suffixes
        t = re.sub(r'\(?\b(epub|pdf|cbr|cbz|format)\b\)?', '', t, flags=re.I)
        t = re.sub(r'\b(all\s+volumes|volume\s*\d+(\.\d+)?|vol\s*\d+(\.\d+)?|book\s*\d+)\b.*$', '', t, flags=re.I)
        t = re.sub(r'\(?\b(light\s+novel|web\s+novel|novel|ln|wn|jp|en|raw)\b\)?', '', t, flags=re.I)
        t = re.sub(r'\[?\b(light\s+novel|web\s+novel|novel|ln|wn|jp|en|raw)\b\]?', '', t, flags=re.I)
        t = re.sub(r'\s+', ' ', t).strip(' :–—-_()[]')
        return t

    @staticmethod
    def clean_novel_title(t: str) -> str:
        """Removes '(Novel)', '(Light Novel)', and bracketed format noise."""
        if not t:
            return ""
        t = re.sub(r'\s*[\(\[]\s*(?:light\s+novel|web\s+novel|novel|ln|wn)\s*[\)\]]', '', t, flags=re.I)
        return re.sub(r'\s+', ' ', t).strip(' :–—-_')

    @classmethod
    def publication_from_meta(cls, meta: Any) -> Dict[str, Any]:
        """Normalise publication metadata from any AniList/cached metadata shape.

        Accepts a raw AniList result dict, ``series_tracking.anilist_meta_json``
        (JSON string), the metadata-cache row, an empty value or garbage.  Always
        returns the normalized publication dict with every key present
        (``format``, ``format_label``, ``is_light_novel``, ``status``,
        ``status_label``, ``volumes``, ``chapters``, ``total_volumes``); counts are
        validated to finite integers >= 0.  Never raises (contract v3 §2).
        """
        publication = publication_defaults()
        try:
            data = _coerce_publication_mapping(meta)
            if not data:
                return publication
            # Tolerate raw GraphQL envelopes ({"data": {"Media": {...}}}) as well
            # as our flat result rows.
            nested_media: Optional[Dict[str, Any]] = None
            if isinstance(data.get("Media"), dict):
                nested_media = data["Media"]
            elif isinstance(data.get("media"), dict):
                nested_media = data["media"]
            elif isinstance(data.get("data"), dict):
                nested_envelope = data["data"]
                if isinstance(nested_envelope.get("Media"), dict):
                    nested_media = nested_envelope["Media"]
                elif isinstance(nested_envelope.get("media"), dict):
                    nested_media = nested_envelope["media"]
            if nested_media is not None:
                data = nested_media

            raw_format = _publication_text(
                data.get("format") or data.get("anilist_format")
            )
            format_label_value = _publication_text(
                data.get("format_label") or data.get("anilist_format_label")
            )
            if not raw_format and format_label_value:
                label_token = _publication_token(format_label_value)
                raw_format = "NOVEL" if label_token in _NOVEL_FORMAT_KEYS else label_token
            format_token = _publication_token(raw_format)
            is_light_novel = format_token in _NOVEL_FORMAT_KEYS
            if not format_token and data.get("is_light_novel") is True:
                # A previously-stored normalized meta row may carry only the flag.
                format_token = "NOVEL"
                is_light_novel = True
            publication["format"] = format_token
            if is_light_novel:
                publication["format_label"] = "Light Novel"
            else:
                publication["format_label"] = (
                    format_label_value or _prettify_publication_value(format_token)
                )
            publication["is_light_novel"] = bool(is_light_novel)

            raw_status = _publication_text(
                data.get("status") or data.get("anilist_status")
            )
            status_label_value = _publication_text(
                data.get("status_label") or data.get("anilist_status_label")
            )
            if not raw_status and status_label_value:
                raw_status = _STATUS_LABEL_TO_RAW.get(
                    status_label_value.strip().lower(), ""
                ) or _publication_token(status_label_value)
            status_token = _publication_token(raw_status)
            publication["status"] = status_token
            status_label = STATUS_LABELS.get(status_token)
            if not status_label:
                status_label = status_label_value or _prettify_publication_value(status_token)
            publication["status_label"] = status_label

            volumes = _publication_int(data.get("volumes"))
            if volumes is None:
                volumes = _publication_int(data.get("total_volumes"))
            if volumes is None:
                volumes = _publication_int(data.get("anilist_volumes"))
            chapters = _publication_int(data.get("chapters"))
            if chapters is None:
                chapters = _publication_int(data.get("anilist_chapters"))
            publication["volumes"] = volumes
            publication["chapters"] = chapters
            publication["total_volumes"] = volumes
        except Exception as exc:  # pragma: no cover - defensive: never raises
            logger.debug("publication_from_meta failed: %s", exc)
            return publication_defaults()
        return publication

    @classmethod
    def _with_publication(cls, result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Return *result* with the normalized publication keys merged in."""
        if not isinstance(result, dict):
            return result
        merged = dict(result)
        publication = cls.publication_from_meta(result)
        for key, value in publication.items():
            if value in (None, "", False) and merged.get(key) not in (None, "", False):
                # An empty normalized value must not clobber real stored data.
                continue
            merged[key] = value
        return merged

    @classmethod
    def query_kitsu(cls, cleaned_title: str) -> Optional[Dict[str, Any]]:
        """Queries Kitsu JSON:API for light novels and translated web novels.

        Returns the title identity keys (``canonical_title``, ``title``,
        ``english_title``, ``romaji_title``, ``native_title``,
        ``alternate_titles``) plus synopsis/cover when available.  The synopsis is
        not required: series-identity consumers use :meth:`fetch_series_metadata`.
        """
        url = f"https://kitsu.io/api/edge/manga?filter[text]={urllib.parse.quote(cleaned_title)}"
        data = None

        _KITSU_LIMITER.wait_if_needed()
        try:
            if httpx is not None:
                with httpx.Client(headers={**DEFAULT_HEADERS, "Accept": "application/vnd.api+json"}, timeout=10.0, follow_redirects=False) as client:
                    # Redirects are followed manually so every hop is SSRF-validated.
                    resp = safe_client_get(client, url, max_redirects=5)
                    _KITSU_LIMITER.handle_response_headers(resp.headers)
                    if resp.status_code == 429:
                        _KITSU_LIMITER.handle_rate_limit_error(resp.headers)
                        return None
                    if resp.status_code == 200:
                        data = resp.json()
            else:
                req = urllib.request.Request(url, headers={**DEFAULT_HEADERS, "Accept": "application/vnd.api+json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    _KITSU_LIMITER.handle_response_headers(resp.headers)
                    data = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _KITSU_LIMITER.handle_rate_limit_error(e.headers)
            return None
        except Exception as e:
            logger.debug(f"Kitsu query exception for '{cleaned_title}': {e}")
            return None

        if not data:
            return None

        items = data.get("data", [])
        if not items:
            return None

        # Prefer items tagged with subtype == 'novel'
        novel_item = next((it for it in items if it.get("attributes", {}).get("subtype") == "novel"), items[0])
        attr = novel_item.get("attributes", {})
        synopsis = attr.get("synopsis")
        poster = attr.get("posterImage", {}) or {}
        cover = poster.get("large") or poster.get("original") or poster.get("medium")
        titles_obj = attr.get("titles", {})
        if not isinstance(titles_obj, dict):
            titles_obj = {}
        en_title = titles_obj.get("en") or titles_obj.get("en_us")
        canonical_raw = attr.get("canonicalTitle") or ""
        romaji_raw = titles_obj.get("ja_jp") or ""
        native_raw = titles_obj.get("ja") or titles_obj.get("ja_jp") or ""

        preferred_title = cls.clean_novel_title(en_title or canonical_raw)
        clean_en = cls.clean_novel_title(en_title) if en_title else ""
        clean_canonical = cls.clean_novel_title(canonical_raw) if canonical_raw else ""

        alternate_titles = []
        candidates = list(titles_obj.values()) + list(attr.get("abbreviatedTitles") or [])
        for value in candidates:
            if not isinstance(value, str):
                continue
            cleaned_value = cls.clean_novel_title(value)
            if cleaned_value and cleaned_value not in alternate_titles:
                alternate_titles.append(cleaned_value)

        result = {
            "title": preferred_title,
            "english_title": clean_en or None,
            "canonical_title": clean_canonical or preferred_title,
            "romaji_title": cls.clean_novel_title(romaji_raw) if romaji_raw else None,
            "native_title": cls.clean_novel_title(native_raw) if native_raw else None,
            "alternate_titles": alternate_titles,
            "synonyms": list(alternate_titles),
            "anilist_id": None,
            "synopsis": synopsis.strip() if synopsis else "",
            "cover_url": cover,
            "source": "Kitsu",
        }
        return result if (result.get("title") or alternate_titles) else None

    @classmethod
    def _anilist_graphql(cls, req_payload: Dict[str, Any], label: str) -> Optional[Dict[str, Any]]:
        """POSTs one GraphQL payload to AniList with the shared rate limiter."""
        url = "https://graphql.anilist.co"
        _ANILIST_LIMITER.wait_if_needed()
        try:
            if httpx is not None:
                # POST through the shared helper so redirect hops are SSRF-validated too.
                with httpx.Client(headers={**DEFAULT_HEADERS, "Content-Type": "application/json"}, timeout=10.0, follow_redirects=False) as client:
                    resp = safe_client_request(client, "post", url, json=req_payload)
                    _ANILIST_LIMITER.handle_response_headers(resp.headers)
                    if resp.status_code == 429:
                        _ANILIST_LIMITER.handle_rate_limit_error(resp.headers)
                        return None
                    if resp.status_code == 200:
                        return resp.json()
                    return None
            payload_bytes = json.dumps(req_payload).encode("utf-8")
            req = urllib.request.Request(url, data=payload_bytes, headers={**DEFAULT_HEADERS, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                _ANILIST_LIMITER.handle_response_headers(resp.headers)
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _ANILIST_LIMITER.handle_rate_limit_error(e.headers)
            return None
        except Exception as e:
            logger.debug(f"AniList query exception for '{label}': {e}")
            return None

    @classmethod
    def _parse_anilist_media(cls, media: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalizes one AniList Media node into the provider result shape."""
        if not isinstance(media, dict):
            return None
        desc = media.get("description")
        if desc:
            # Strip any remaining HTML tags from AniList description
            desc = re.sub(r'<[^>]+>', '', desc).strip()

        cover_img = media.get("coverImage", {}) or {}
        cover = cover_img.get("large") or cover_img.get("extraLarge")
        title_obj = media.get("title", {}) or {}
        en_title = title_obj.get("english")
        romaji_title = title_obj.get("romaji")
        native_title = title_obj.get("native")

        preferred_title = cls.clean_novel_title(en_title or romaji_title or "")
        clean_en = cls.clean_novel_title(en_title) if en_title else ""
        clean_romaji = cls.clean_novel_title(romaji_title) if romaji_title else ""
        clean_native = cls.clean_novel_title(native_title) if native_title else ""
        clean_canonical = cls.clean_novel_title(en_title or romaji_title or "")

        synonyms = []
        for value in media.get("synonyms") or []:
            if not isinstance(value, str):
                continue
            cleaned_value = cls.clean_novel_title(value)
            if cleaned_value and cleaned_value not in synonyms:
                synonyms.append(cleaned_value)

        authors = []
        for edge in ((media.get("staff") or {}).get("edges") or []):
            try:
                role = str((edge or {}).get("role") or "").lower()
                name = str((((edge or {}).get("node") or {}).get("name") or {}).get("full") or "").strip()
            except Exception:
                continue
            if not name:
                continue
            if "art" in role:
                continue
            if "story" in role or "original" in role or "author" in role:
                if name not in authors:
                    authors.append(name)
        result = {
            "title": preferred_title,
            "english_title": clean_en or None,
            "canonical_title": clean_canonical,
            "romaji_title": clean_romaji or None,
            "native_title": clean_native or None,
            "synonyms": synonyms,
            "anilist_id": media.get("id"),
            "author": ", ".join(authors[:2]),
            "synopsis": desc or "",
            "cover_url": cover,
            "source": "AniList",
        }
        # Contract v3 §2: Format / Status / Volumes travel with every result.
        result.update(cls.publication_from_meta({
            "format": media.get("format"),
            "status": media.get("status"),
            "volumes": media.get("volumes"),
            "chapters": media.get("chapters"),
        }))
        if not (result.get("title") or synonyms):
            return None
        return result

    @classmethod
    def query_anilist(cls, cleaned_title: str) -> Optional[Dict[str, Any]]:
        """Queries AniList GraphQL API for official light novel metadata.

        Returns the title identity keys used by the series-identity resolver
        (``canonical_title``, ``title``, ``english_title``, ``romaji_title``,
        ``native_title``, ``synonyms``, ``anilist_id``, ``source``), the existing
        synopsis/cover fields and the normalized publication keys (``format``,
        ``format_label``, ``is_light_novel``, ``status``, ``status_label``,
        ``volumes``, ``chapters``, ``total_volumes``).  ``format_in: [NOVEL]`` is
        kept so the API only ever returns light-novel entries.  A missing
        description no longer hides the titles.
        """
        query = """
        query ($search: String) {
          Media (search: $search, type: MANGA, format_in: [NOVEL]) {
            id
            title {
              romaji
              english
              native
            }
            synonyms
            format
            status
            volumes
            chapters
            description(asHtml: false)
            coverImage {
              large
              extraLarge
            }
            staff(perPage: 5) {
              edges {
                role
                node { name { full } }
              }
            }
          }
        }
        """
        data = cls._anilist_graphql(
            {"query": query, "variables": {"search": cleaned_title}},
            cleaned_title,
        )
        if not data:
            return None
        media = data.get("data", {}).get("Media")
        return cls._parse_anilist_media(media)

    @classmethod
    def parse_anilist_id(cls, value: Any) -> Optional[int]:
        """Extracts a numeric AniList id from a URL or bare id string."""
        text = str(value or "").strip()
        if not text:
            return None
        match = re.search(r"anilist\.co/(?:manga|anime|novel)/(\d+)", text, re.IGNORECASE)
        if not match:
            match = re.fullmatch(r"\s*(\d+)\s*", text)
        if not match:
            return None
        try:
            anilist_id = int(match.group(1))
        except (TypeError, ValueError):
            return None
        return anilist_id if anilist_id > 0 else None

    @classmethod
    def query_anilist_by_id(cls, anilist_id: Any) -> Optional[Dict[str, Any]]:
        """Fetches one AniList light-novel entry by its unique AniList id.

        Used by the manual series mapping action, which accepts an AniList URL
        or a bare numeric id. The id query is exact and bypasses search ranking,
        so it cannot drift to a similarly named spin-off such as '... Alter'.
        """
        try:
            target_id = int(anilist_id)
        except (TypeError, ValueError):
            return None
        if target_id <= 0:
            return None
        query = """
        query ($id: Int) {
          Media (id: $id, type: MANGA, format_in: [NOVEL]) {
            id
            title {
              romaji
              english
              native
            }
            synonyms
            format
            status
            volumes
            chapters
            description(asHtml: false)
            coverImage {
              large
              extraLarge
            }
            staff(perPage: 5) {
              edges {
                role
                node { name { full } }
              }
            }
          }
        }
        """
        data = cls._anilist_graphql(
            {"query": query, "variables": {"id": target_id}},
            f"AniList id {target_id}",
        )
        if not data:
            return None
        media = data.get("data", {}).get("Media")
        result = cls._parse_anilist_media(media)
        if result is not None:
            result["anilist_id"] = target_id
        return result

    @classmethod
    def fetch_series_metadata(cls, raw_title: str) -> Optional[Dict[str, Any]]:
        """Title-only metadata lookup for series identity (AniList first, Kitsu fallback).

        Unlike :meth:`fetch_novel_metadata` a synopsis is **not** required: this
        returns the title identity keys (canonical/english/romaji/native/synonyms,
        AniList id and source) even when the provider has no description.  The
        existing ``novel_metadata_cache`` is consulted first via the same cleaned
        title keys.  Never raises.
        """
        cleaned = cls.clean_search_title(raw_title)
        if not cleaned:
            return None
        active_db = cls._default_db
        if active_db is not None:
            try:
                for k in (cleaned.lower(), str(raw_title or "").strip().lower()):
                    cached = active_db.get_cached_novel_metadata(k)
                    if cached and (cached.get("title") or "").strip():
                        cached_title = (cached.get("title") or "").strip()
                        cached_result = {
                            "title": cached_title,
                            "english_title": cached_title,
                            "canonical_title": cached_title,
                            "romaji_title": None,
                            "native_title": None,
                            "synonyms": [],
                            "anilist_id": None,
                            "synopsis": cached.get("synopsis", "") or "",
                            "cover_url": cached.get("cover_url", "") or "",
                            "source": cached.get("source", "cache"),
                            "cached": True,
                        }
                        # Contract v3 §3: publication keys survive cache hits.
                        cached_result.update(cls.publication_from_meta(cached))
                        return cls._with_publication(cached_result)
            except Exception as e:
                logger.debug(f"Series metadata cache lookup failed for '{cleaned}': {e}")

        result = None
        try:
            anilist_res = cls.query_anilist(cleaned)
        except Exception as e:
            logger.debug(f"AniList series-metadata query failed for '{cleaned}': {e}")
            anilist_res = None
        if anilist_res and (anilist_res.get("title") or anilist_res.get("synonyms")):
            result = anilist_res
        else:
            try:
                kitsu_res = cls.query_kitsu(cleaned)
            except Exception as e:
                logger.debug(f"Kitsu series-metadata query failed for '{cleaned}': {e}")
                kitsu_res = None
            if kitsu_res and (kitsu_res.get("title") or kitsu_res.get("alternate_titles")):
                result = kitsu_res

        if result and active_db is not None:
            try:
                # Persist the publication keys alongside the legacy cache fields
                # (cache_novel_metadata stores publication_json from this payload).
                cache_payload = dict(result)
                cache_payload.setdefault("synopsis", "")
                active_db.cache_novel_metadata(cleaned.lower(), cache_payload)
                english_key = (result.get("english_title") or "").strip().lower()
                if english_key and english_key != cleaned.lower():
                    active_db.cache_novel_metadata(english_key, cache_payload)
            except Exception as e:
                logger.debug(f"Failed to cache series metadata for '{cleaned}': {e}")
        return cls._with_publication(result)

    @classmethod
    def fetch_novel_metadata(cls, raw_title: str, db: Optional[Any] = None) -> Optional[Dict[str, Any]]:
        """
        Unified metadata fetcher:
        1. Normalizes novel title.
        2. Checks SQLite metadata cache before external API calls.
        3. Queries AniList and Kitsu with sliding-window rate limiters.
        4. Prioritizes English title if multiple are available.
        5. Saves authoritative results to SQLite cache.
        """
        cleaned = cls.clean_search_title(raw_title)
        if not cleaned:
            return None

        active_db = db or cls._default_db
        if active_db is not None:
            try:
                for k in (cleaned.lower(), raw_title.lower()):
                    cached = active_db.get_cached_novel_metadata(k)
                    if cached and (cached.get("synopsis") or cached.get("cover_url")):
                        logger.info(f"Retrieved novel metadata for '{cleaned}' from SQLite cache.")
                        cached_title = cached.get("title") or cleaned
                        cached_result = {
                            "title": cached_title,
                            "english_title": cached_title,
                            # Title identity keys travel through the cache too
                            # (the cache itself only stores title/synopsis/cover/source).
                            "canonical_title": cached_title,
                            "romaji_title": None,
                            "native_title": None,
                            "synonyms": [],
                            "anilist_id": None,
                            "synopsis": cached.get("synopsis", ""),
                            "cover_url": cached.get("cover_url", ""),
                            "source": cached.get("source", "cache"),
                            "cached": True,
                        }
                        # Contract v3 §3: publication keys survive cache hits.
                        cached_result.update(cls.publication_from_meta(cached))
                        return cls._with_publication(cached_result)
            except Exception as e:
                logger.debug(f"Metadata cache lookup failed for '{cleaned}': {e}")

        # 1. Try AniList first (official curated English titles for light novels)
        anilist_res = None
        try:
            anilist_res = cls.query_anilist(cleaned)
        except Exception as e:
            logger.debug(f"AniList query failed for '{cleaned}': {e}")

        result = None
        # If AniList has an English title and synopsis, return it
        if anilist_res and anilist_res.get("english_title") and anilist_res.get("synopsis"):
            logger.info(f"Retrieved novel metadata for '{cleaned}' from AniList with English title '{anilist_res['english_title']}'.")
            result = anilist_res
        else:
            # 2. Try Kitsu (covers web novels, manhwa, danmei, and light novels)
            kitsu_res = None
            try:
                kitsu_res = cls.query_kitsu(cleaned)
            except Exception as e:
                logger.debug(f"Kitsu query failed for '{cleaned}': {e}")

            if kitsu_res and kitsu_res.get("english_title") and kitsu_res.get("synopsis"):
                logger.info(f"Retrieved novel metadata for '{cleaned}' from Kitsu with English title '{kitsu_res['english_title']}'.")
                result = kitsu_res
            elif anilist_res and anilist_res.get("synopsis"):
                result = anilist_res
            elif kitsu_res and kitsu_res.get("synopsis"):
                result = kitsu_res

        result = cls._with_publication(result)
        if result and active_db is not None:
            try:
                active_db.cache_novel_metadata(cleaned.lower(), result)
                if result.get("english_title") and result["english_title"].lower() != cleaned.lower():
                    active_db.cache_novel_metadata(result["english_title"].lower(), result)
            except Exception as e:
                logger.debug(f"Failed to cache metadata for '{cleaned}': {e}")

        return result

