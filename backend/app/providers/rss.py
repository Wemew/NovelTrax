import re
import logging
from typing import List, Dict, Any, Optional
import feedparser
from app.providers.base import BaseFeedProvider, ReleaseItem
from app.parser import extract_series_and_volume, is_pdf_release

logger = logging.getLogger("noveltrax.providers.rss")

class RSSFeedProvider(BaseFeedProvider):
    """
    Generic RSS / Atom feed provider using feedparser.
    Works for publisher feeds, blogs, release tracking feeds, etc.
    """
    def __init__(self, name: str, url: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(name, url, config)

    def fetch_releases(self) -> List[ReleaseItem]:
        headers = {
            "User-Agent": "NovelTrax/1.0 (Book Release Monitor; +https://github.com/noveltrax)",
        }
        feed = feedparser.parse(self.url, request_headers=headers)
        if feed.bozo and not feed.entries:
            logger.error(f"Error parsing RSS feed at {self.url}: {feed.bozo_exception}")
            return []

        results = []
        for entry in feed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "")
            if not title or not link:
                continue

            # Strictly reject PDF releases
            if is_pdf_release(title, link):
                continue

            pub_date = entry.get("published") or entry.get("updated")
            summary = entry.get("summary") or entry.get("description") or ""

            # Attempt to extract cover image
            cover_url = None
            if "media_thumbnail" in entry and entry.media_thumbnail:
                cover_url = entry.media_thumbnail[0].get("url")
            elif "media_content" in entry and entry.media_content:
                cover_url = entry.media_content[0].get("url")
            elif "enclosures" in entry and entry.enclosures:
                for enc in entry.enclosures:
                    if "image" in enc.get("type", ""):
                        cover_url = enc.get("href")
                        break

            if not cover_url and summary:
                img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary, re.IGNORECASE)
                if img_match:
                    cover_url = img_match.group(1)

            author = entry.get("author") or ""
            parsed = extract_series_and_volume(title)

            clean_desc = re.sub(r'<[^>]+>', '', summary).strip()[:400]

            results.append(ReleaseItem(
                title=title,
                link=link,
                series_name=parsed["series_name"],
                volume=parsed["volume"],
                pub_date=pub_date,
                cover_url=cover_url,
                description=clean_desc,
                author=author,
                guid=entry.get("id"),
                raw_data={"confidence": parsed["confidence"], "subtitle": parsed["subtitle"]}
            ))

        return results
