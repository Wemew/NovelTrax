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

    @staticmethod
    def clean_novel_title(t: str) -> str:
        """Removes '(Novel)' or '(Light Novel)' suffixes from canonical titles."""
        if not t:
            return ""
        t = re.sub(r'\s*\((Novel|Light Novel)\)', '', t, flags=re.I)
        return t.strip()

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
        titles_obj = attr.get("titles", {})
        en_title = titles_obj.get("en") or titles_obj.get("en_us")
        canonical_title = attr.get("canonicalTitle")

        preferred_title = cls.clean_novel_title(en_title or canonical_title)
        clean_en = cls.clean_novel_title(en_title) if en_title else None

        if synopsis and len(synopsis.strip()) > 20:
            return {
                "title": preferred_title,
                "english_title": clean_en,
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
        en_title = title_obj.get("english")
        romaji_title = title_obj.get("romaji")

        preferred_title = cls.clean_novel_title(en_title or romaji_title)
        clean_en = cls.clean_novel_title(en_title) if en_title else None

        if desc and len(desc) > 20:
            return {
                "title": preferred_title,
                "english_title": clean_en,
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
        2. Queries AniList and Kitsu.
        3. Prioritizes English title if multiple are available.
        """
        cleaned = cls.clean_search_title(raw_title)
        if not cleaned:
            return None

        # 1. Try AniList first (official curated English titles for light novels)
        anilist_res = None
        try:
            anilist_res = cls.query_anilist(cleaned)
        except Exception as e:
            logger.debug(f"AniList query failed for '{cleaned}': {e}")

        # If AniList has an English title and synopsis, return it
        if anilist_res and anilist_res.get("english_title") and anilist_res.get("synopsis"):
            logger.info(f"Retrieved novel metadata for '{cleaned}' from AniList with English title '{anilist_res['english_title']}'.")
            return anilist_res

        # 2. Try Kitsu (covers web novels, manhwa, danmei, and light novels)
        kitsu_res = None
        try:
            kitsu_res = cls.query_kitsu(cleaned)
        except Exception as e:
            logger.debug(f"Kitsu query failed for '{cleaned}': {e}")

        # If Kitsu has an English title and synopsis, return it
        if kitsu_res and kitsu_res.get("english_title") and kitsu_res.get("synopsis"):
            logger.info(f"Retrieved novel metadata for '{cleaned}' from Kitsu with English title '{kitsu_res['english_title']}'.")
            return kitsu_res

        # If neither had an explicit English title, return whichever found a synopsis
        if anilist_res and anilist_res.get("synopsis"):
            return anilist_res
        if kitsu_res and kitsu_res.get("synopsis"):
            return kitsu_res

        return None

        return None
