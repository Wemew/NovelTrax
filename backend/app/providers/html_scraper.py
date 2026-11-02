import re
import logging
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse
try:
    import httpx
except ImportError:
    httpx = None
from bs4 import BeautifulSoup
from app.providers.base import BaseFeedProvider, ReleaseItem
from app.parser import extract_series_and_volume, is_pdf_release
from app.security import safe_client_get

logger = logging.getLogger("noveltrax.providers.html_scraper")

class HtmlScraperProvider(BaseFeedProvider):
    """
    Flexible HTML scraper using customizable CSS selectors and pagination support.
    Config parameters:
      - item_selector: selector for post / entry containers (e.g. 'article', '.novel-item')
      - title_selector: selector inside container for title (e.g. 'h2 a', '.title')
      - link_selector: selector for URL (optional, defaults to title link)
      - image_selector: selector for cover image (e.g. 'img.cover')
      - date_selector: selector for date (e.g. 'time', '.date')
      - next_selector: selector for next page link (e.g. 'a.next')
      - max_pages: number of pagination pages to scan (default 3)
    """
    def __init__(self, name: str, url: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(name, url, config)
        self.item_selector = self.config.get("item_selector") or "article, .post, .entry, .item, tr.row"
        self.title_selector = self.config.get("title_selector") or "h2 a, h3 a, h1 a, .title a, a"
        self.link_selector = self.config.get("link_selector") or None
        self.image_selector = self.config.get("image_selector") or "img"
        self.date_selector = self.config.get("date_selector") or "time, .date, .time"
        self.next_selector = self.config.get("next_selector") or "a.next, a.next-page, a[rel='next']"
        self.max_pages = int(self.config.get("max_pages", 3))

    def fetch_releases(self) -> List[ReleaseItem]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        results = []
        seen_links = set()
        current_url = self.url
        page_num = 1

        with httpx.Client(headers=headers, timeout=15.0, follow_redirects=False) as client:
            while current_url and page_num <= self.max_pages:
                try:
                    resp = safe_client_get(client, current_url)
                    resp.raise_for_status()
                    html_text = resp.text
                except Exception as e:
                    logger.warning(f"Error fetching page {page_num} ({current_url}): {e}")
                    break

                soup = BeautifulSoup(html_text, "html.parser")
                items = soup.select(self.item_selector)
                if not items:
                    break

                for el in items:
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

                    if link and link.startswith("/"):
                        parsed_base = urlparse(self.url)
                        link = f"{parsed_base.scheme}://{parsed_base.netloc}{link}"

                    if not link or link in seen_links:
                        continue
                    seen_links.add(link)

                    # Strictly reject PDF releases
                    if is_pdf_release(raw_title, link):
                        continue

                    # Cover image
                    cover_url = None
                    if self.image_selector:
                        img_el = el.select_one(self.image_selector)
                        if img_el:
                            cover_url = img_el.get("src") or img_el.get("data-src")
                            if cover_url and cover_url.startswith("/"):
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

                # Check next page
                next_tag = soup.select_one(self.next_selector)
                if next_tag and next_tag.get("href"):
                    next_url = next_tag.get("href")
                    if next_url.startswith("/"):
                        parsed_base = urlparse(self.url)
                        next_url = f"{parsed_base.scheme}://{parsed_base.netloc}{next_url}"
                    current_url = next_url
                    page_num += 1
                else:
                    break

        return results
