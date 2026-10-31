import re
import json
import logging
from typing import Optional, Dict, Any
import urllib.parse
import urllib.request

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger("noveltrax.providers.metadata")

DEFAULT_HEADERS = {
    "User-Agent": "NovelTrax/1.0 (https://github.com/Wemew/NovelTrax)",
    "Accept": "application/json"
}

class NovelMetadataProvider:
    """
    Fetches rich novel metadata (synopses, official cover artwork, canonical titles)
    from public community databases (Kitsu and AniList).
    Operates as an automatic fallback when source novel post has no synopsis.
    """

    @staticmethod
    def clean_search_title(raw_title: str) -> str:
        """
        Strips release noise (epub, pdf, volume numbers, subtitle cruft)
        to form an accurate database search query.
        """
        if not raw_title:
            return ""
        t = raw_title
        # Remove format indicators and bracketed suffixes
        t = re.sub(r'\(?\b(epub|pdf|cbr|cbz|format)\b\)?', '', t, flags=re.I)
        t = re.sub(r'\b(all\s+volumes|volume\s*\d+(\.\d+)?|vol\s*\d+(\.\d+)?|book\s*\d+)\b.*$', '', t, flags=re.I)
        t = re.sub(r'\(?\b(light\s+novel|web\s+novel|novel|ln|wn|jp|en|raw)\b\)?', '', t, flags=re.I)
        # Strip trailing secondary subtitle after dash or separator if long
        t = re.sub(r'\s+[\-\–—]\s+.*$', '', t)
        t = re.sub(r'[:–—]\s+[A-Za-z\s]{15,}$', '', t)
        t = re.sub(r'\s+', ' ', t).strip(' :–—-_()[]')
        return t

    @classmethod
    def query_kitsu(cls, cleaned_title: str) -> Optional[Dict[str, Any]]:
        """Queries Kitsu JSON:API for light novels and translated web novels."""
        url = f"https://kitsu.io/api/edge/manga?filter[text]={urllib.parse.quote(cleaned_title)}"
        data = None

        if httpx is not None:
            with httpx.Client(headers={**DEFAULT_HEADERS, "Accept": "application/vnd.api+json"}, timeout=10.0, follow_redirects=True) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
        else:
            req = urllib.request.Request(url, headers={**DEFAULT_HEADERS, "Accept": "application/vnd.api+json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))

        if not data:
            return None

        items = data.get("data", [])
        if not items:
            return None

        # Prefer items tagged with subtype == 'novel'
        novel_item = next((it for it in items if it.get("attributes", {}).get("subtype") == "novel"), items[0])
        attr = novel_item.get("attributes", {})
        synopsis = attr.get("synopsis")
        poster = attr.get("posterImage", {})
        cover = poster.get("large") or poster.get("original") or poster.get("medium")
        canonical_title = attr.get("canonicalTitle")

        if synopsis and len(synopsis.strip()) > 20:
            return {
                "title": canonical_title,
                "synopsis": synopsis.strip(),
                "cover_url": cover,
                "source": "Kitsu"
            }
        return None

    @classmethod
    def query_anilist(cls, cleaned_title: str) -> Optional[Dict[str, Any]]:
        """Queries AniList GraphQL API for official light novel metadata."""
        query = """
        query ($search: String) {
          Media (search: $search, type: MANGA, format_in: [NOVEL]) {
            id
            title {
              romaji
              english
            }
            description(asHtml: false)
            coverImage {
              large
              extraLarge
            }
          }
        }
        """
        url = "https://graphql.anilist.co"
        req_payload = {"query": query, "variables": {"search": cleaned_title}}
        data = None

        if httpx is not None:
            with httpx.Client(headers={**DEFAULT_HEADERS, "Content-Type": "application/json"}, timeout=10.0) as client:
                resp = client.post(url, json=req_payload)
                if resp.status_code == 200:
                    data = resp.json()
        else:
            payload_bytes = json.dumps(req_payload).encode('utf-8')
            req = urllib.request.Request(url, data=payload_bytes, headers={**DEFAULT_HEADERS, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))

        if not data:
            return None

        media = data.get("data", {}).get("Media")
        if not media:
            return None

        desc = media.get("description")
        if desc:
            # Strip any remaining HTML tags from AniList description
            desc = re.sub(r'<[^>]+>', '', desc).strip()

        cover_img = media.get("coverImage", {})
        cover = cover_img.get("large") or cover_img.get("extraLarge")
        title_obj = media.get("title", {})
        canonical_title = title_obj.get("english") or title_obj.get("romaji")

        if desc and len(desc) > 20:
            return {
                "title": canonical_title,
                "synopsis": desc,
                "cover_url": cover,
                "source": "AniList"
            }
        return None

    @classmethod
    def fetch_novel_metadata(cls, raw_title: str) -> Optional[Dict[str, Any]]:
        """
        Unified metadata fetcher:
        1. Normalizes novel title.
        2. Tries Kitsu API (covers both Light Novels & Web Novels).
        3. Falls back to AniList GraphQL API (Japanese Light Novels).
        """
        cleaned = cls.clean_search_title(raw_title)
        if not cleaned:
            return None

        # 1. Try Kitsu
        try:
            kitsu_res = cls.query_kitsu(cleaned)
            if kitsu_res and kitsu_res.get("synopsis"):
                logger.info(f"Retrieved novel metadata for '{cleaned}' from Kitsu.")
                return kitsu_res
        except Exception as e:
            logger.debug(f"Kitsu metadata query failed for '{cleaned}': {e}")

        # 2. Try AniList fallback
        try:
            anilist_res = cls.query_anilist(cleaned)
            if anilist_res and anilist_res.get("synopsis"):
                logger.info(f"Retrieved novel metadata for '{cleaned}' from AniList.")
                return anilist_res
        except Exception as e:
            logger.debug(f"AniList metadata query failed for '{cleaned}': {e}")

        return None
