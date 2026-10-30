import re
import logging
from typing import List, Dict, Any, Optional
import httpx
from bs4 import BeautifulSoup
from app.providers.base import BaseFeedProvider, ReleaseItem
from app.parser import extract_series_and_volume

logger = logging.getLogger("novelpulse.providers.jnovels")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://jnovels.com/"
}

class JNovelsProvider(BaseFeedProvider):
    """
    Feed provider specifically tailored for J-Novels (https://jnovels.com/?s=epub).
    Supports HTML search scraping and WordPress RSS feed fallback.
    """
    def __init__(self, name: str = "J-Novels (EPUB)", url: str = "https://jnovels.com/?s=epub", config: Optional[Dict[str, Any]] = None):
        super().__init__(name, url, config)

    def fetch_releases(self) -> List[ReleaseItem]:
        items = []
        # Try HTML scraping first
        try:
            items = self._fetch_via_html()
            if items:
                logger.info(f"JNovels HTML scraper fetched {len(items)} releases.")
                return items
        except Exception as e:
            logger.warning(f"JNovels HTML scraper encountered error: {e}. Falling back to RSS feed.")

        # Fallback to WordPress RSS feed: https://jnovels.com/?s=epub&feed=rss2 or https://jnovels.com/feed/
        try:
            items = self._fetch_via_rss()
            if items:
                logger.info(f"JNovels RSS fallback fetched {len(items)} releases.")
                return items
        except Exception as e:
            logger.error(f"JNovels RSS fallback also failed: {e}")

        return items

    def _fetch_via_html(self) -> List[ReleaseItem]:
        results = []
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=15.0, follow_redirects=True) as client:
            resp = client.get(self.url)
            resp.raise_for_status()
            html_text = resp.text

        soup = BeautifulSoup(html_text, "html.parser")

        # Typical WordPress themes enclose search results in <article> elements
        articles = soup.find_all("article")
        if not articles:
            # Fallback to post containers
            articles = soup.select(".post, .type-post, .entry, .search-entry")

        for art in articles:
            # Title & Link
            title_tag = art.select_one(".entry-title a, h2 a, h1 a, .post-title a")
            if not title_tag:
                continue

            raw_title = title_tag.get_text(strip=True)
            link = title_tag.get("href", "").strip()
            if not raw_title or not link:
                continue

            # Cover Image
            img_tag = art.select_one("img.wp-post-image, img.attachment-post-thumbnail, .post-thumbnail img, img")
            cover_url = None
            if img_tag:
                cover_url = img_tag.get("src") or img_tag.get("data-src") or img_tag.get("data-lazy-src")

            # Date
            date_tag = art.select_one("time.entry-date, time.published, time")
            pub_date = None
            if date_tag:
                pub_date = date_tag.get("datetime") or date_tag.get_text(strip=True)

            # Summary / Description
            summary_tag = art.select_one(".entry-summary, .entry-content, p")
            desc = summary_tag.get_text(strip=True) if summary_tag else ""

            # Extract series name and volume number
            parsed = extract_series_and_volume(raw_title)

            results.append(ReleaseItem(
                title=raw_title,
                link=link,
                series_name=parsed["series_name"],
                volume=parsed["volume"],
                pub_date=pub_date,
                cover_url=cover_url,
                description=desc,
                raw_data={"confidence": parsed["confidence"], "subtitle": parsed["subtitle"]}
            ))

        return results

    def _fetch_via_rss(self) -> List[ReleaseItem]:
        # Formulate RSS feed URL
        rss_url = self.url
        if "feed=rss2" not in rss_url and "/feed" not in rss_url:
            if "?" in rss_url:
                rss_url = f"{rss_url}&feed=rss2"
            else:
                rss_url = f"{rss_url.rstrip('/')}/feed/"

        import feedparser
        feed = feedparser.parse(rss_url, request_headers=DEFAULT_HEADERS)
        results = []

        for entry in feed.entries:
            raw_title = entry.get("title", "")
            link = entry.get("link", "")
            if not raw_title or not link:
                continue

            pub_date = entry.get("published") or entry.get("updated")
            summary = entry.get("summary") or entry.get("description") or ""

            # Find image thumbnail in enclosures or description
            cover_url = None
            if "media_thumbnail" in entry and entry.media_thumbnail:
                cover_url = entry.media_thumbnail[0].get("url")
            elif "enclosures" in entry and entry.enclosures:
                for enc in entry.enclosures:
                    if "image" in enc.get("type", ""):
                        cover_url = enc.get("href")
                        break

            if not cover_url and summary:
                # Try finding <img> in summary
                img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary, re.IGNORECASE)
                if img_match:
                    cover_url = img_match.group(1)

            parsed = extract_series_and_volume(raw_title)

            results.append(ReleaseItem(
                title=raw_title,
                link=link,
                series_name=parsed["series_name"],
                volume=parsed["volume"],
                pub_date=pub_date,
                cover_url=cover_url,
                description=re.sub(r'<[^>]+>', '', summary)[:300],
                raw_data={"confidence": parsed["confidence"]}
            ))

        return results
