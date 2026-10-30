import re
import logging
from typing import List, Dict, Any, Optional
import httpx
from bs4 import BeautifulSoup
from app.providers.base import BaseFeedProvider, ReleaseItem
from app.parser import extract_series_and_volume

logger = logging.getLogger("noveltrax.providers.novelupdates")

class NovelUpdatesProvider(BaseFeedProvider):
    """
    Provider for NovelUpdates release feeds or custom series reading lists.
    Can ingest NovelUpdates RSS feeds or series release listings.
    """
    def __init__(self, name: str = "NovelUpdates", url: str = "https://www.novelupdates.com/", config: Optional[Dict[str, Any]] = None):
        super().__init__(name, url, config)

    def fetch_releases(self) -> List[ReleaseItem]:
        # If an RSS URL is supplied, use feedparser
        if "rss" in self.url.lower() or "feed" in self.url.lower():
            from app.providers.rss import RSSFeedProvider
            rss_prov = RSSFeedProvider(self.name, self.url, self.config)
            return rss_prov.fetch_releases()

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://www.novelupdates.com/"
        }
        with httpx.Client(headers=headers, timeout=15.0, follow_redirects=True) as client:
            resp = client.get(self.url)
            resp.raise_for_status()
            html_text = resp.text

        soup = BeautifulSoup(html_text, "html.parser")
        results = []

        # NovelUpdates table rows or list items
        rows = soup.select("table#myTable tr, .search_main_box, .my-list-row")
        for row in rows:
            title_tag = row.select_one(".search_title a, a.rl_clink, .series-title a")
            if not title_tag:
                continue

            series_title = title_tag.get_text(strip=True)
            link = title_tag.get("href", "")

            # Look for release / chapter / volume info
            rel_tag = row.select_one(".search_stats, .chp-release, .release-info")
            rel_text = rel_tag.get_text(strip=True) if rel_tag else ""
            combined_title = f"{series_title} {rel_text}".strip()

            parsed = extract_series_and_volume(combined_title)

            # Image
            img_tag = row.select_one("img")
            cover_url = img_tag.get("src") if img_tag else None

            results.append(ReleaseItem(
                title=combined_title,
                link=link,
                series_name=parsed["series_name"] or series_title,
                volume=parsed["volume"],
                cover_url=cover_url,
                series_link=link,
                raw_data={"confidence": parsed["confidence"]}
            ))

        return results
