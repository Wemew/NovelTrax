import re
import logging
from typing import List, Dict, Any, Optional
import httpx
from bs4 import BeautifulSoup
from app.providers.base import BaseFeedProvider, ReleaseItem
from app.parser import extract_series_and_volume

logger = logging.getLogger("novelpulse.providers.html_scraper")

class HtmlScraperProvider(BaseFeedProvider):
    """
    Flexible HTML scraper using customizable CSS selectors or sensible auto-detection defaults.
    Config parameters:
      - item_selector: selector for post / entry containers (e.g. 'article', '.novel-item')
      - title_selector: selector inside container for title (e.g. 'h2 a', '.title')
      - link_selector: selector for URL (optional, defaults to title link)
      - image_selector: selector for cover image (e.g. 'img.cover')
      - date_selector: selector for date (e.g. 'time', '.date')
    """
    def __init__(self, name: str, url: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(name, url, config)
        self.item_selector = self.config.get("item_selector") or "article, .post, .entry, .item, tr.row"
        self.title_selector = self.config.get("title_selector") or "h2 a, h3 a, h1 a, .title a, a"
        self.link_selector = self.config.get("link_selector") or None
        self.image_selector = self.config.get("image_selector") or "img"
        self.date_selector = self.config.get("date_selector") or "time, .date, .time"

    def fetch_releases(self) -> List[ReleaseItem]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        with httpx.Client(headers=headers, timeout=15.0, follow_redirects=True) as client:
            resp = client.get(self.url)
            resp.raise_for_status()
            html_text = resp.text

        soup = BeautifulSoup(html_text, "html.parser")
        items = soup.select(self.item_selector)
        logger.info(f"Custom HTML scraper found {len(items)} elements matching selector '{self.item_selector}'")

        results = []
        for el in items:
            # Title & link
            title_el = el.select_one(self.title_selector)
            if not title_el:
                continue

            raw_title = title_el.get_text(strip=True)
            if not raw_title:
                continue

            # Link
            link = ""
            if self.link_selector:
                link_el = el.select_one(self.link_selector)
                if link_el:
                    link = link_el.get("href", "")
            if not link and title_el.name == "a":
                link = title_el.get("href", "")
            elif not link:
                a_tag = el.select_one("a")
                if a_tag:
                    link = a_tag.get("href", "")

            # Fix relative URLs
            if link and link.startswith("/"):
                # Append base domain
                from urllib.parse import urlparse
                parsed_base = urlparse(self.url)
                link = f"{parsed_base.scheme}://{parsed_base.netloc}{link}"

            # Cover image
            cover_url = None
            if self.image_selector:
                img_el = el.select_one(self.image_selector)
                if img_el:
                    cover_url = img_el.get("src") or img_el.get("data-src")
                    if cover_url and cover_url.startswith("/"):
                        from urllib.parse import urlparse
                        parsed_base = urlparse(self.url)
                        cover_url = f"{parsed_base.scheme}://{parsed_base.netloc}{cover_url}"

            # Date
            pub_date = None
            if self.date_selector:
                date_el = el.select_one(self.date_selector)
                if date_el:
                    pub_date = date_el.get("datetime") or date_el.get_text(strip=True)

            parsed = extract_series_and_volume(raw_title)

            results.append(ReleaseItem(
                title=raw_title,
                link=link,
                series_name=parsed["series_name"],
                volume=parsed["volume"],
                pub_date=pub_date,
                cover_url=cover_url,
                raw_data={"confidence": parsed["confidence"]}
            ))

        return results
