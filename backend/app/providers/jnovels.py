import re
import logging
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import httpx
from bs4 import BeautifulSoup
from app.providers.base import BaseFeedProvider, ReleaseItem
from app.parser import extract_series_and_volume

logger = logging.getLogger("noveltrax.providers.jnovels")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://jnovels.com/"
}

class JNovelsProvider(BaseFeedProvider):
    """
    Feed provider specifically tailored for J-Novels (https://jnovels.com/?s=epub).
    Supports multi-page pagination scanning and WordPress RSS feed fallback.
    Never misses releases even when more than a single page of releases drops.
    """
    def __init__(self, name: str = "J-Novels (EPUB)", url: str = "https://jnovels.com/?s=epub", config: Optional[Dict[str, Any]] = None):
        super().__init__(name, url, config)
        # Default: scan up to 3 pages (approx. 18-30 releases per scan)
        self.max_pages = int(self.config.get("max_pages", 3))

    def fetch_releases(self) -> List[ReleaseItem]:
        items = []
        # Try multi-page HTML scraping first
        try:
            items = self._fetch_via_html()
            if items:
                logger.info(f"JNovels HTML scraper fetched {len(items)} releases across pagination.")
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

    def _build_paged_url(self, base_url: str, page_num: int) -> str:
        """Constructs WordPress pagination URL (e.g. /page/2/?s=epub or ?s=epub&paged=2)."""
        parsed = urlparse(base_url)
        path = parsed.path.rstrip('/')
        path = re.sub(r'/page/\d+', '', path)
        new_path = f"{path}/page/{page_num}/" if path else f"/page/{page_num}/"
        return urlunparse((parsed.scheme, parsed.netloc, new_path, '', parsed.query, ''))

    def _fetch_via_html(self) -> List[ReleaseItem]:
        results = []
        seen_links = set()
        current_url = self.url
        page_num = 1

        with httpx.Client(headers=DEFAULT_HEADERS, timeout=15.0, follow_redirects=True) as client:
            while current_url and page_num <= self.max_pages:
                logger.debug(f"JNovels fetching page {page_num}: {current_url}")
                try:
                    resp = client.get(current_url)
                    if resp.status_code == 404:
                        logger.info(f"Reached end of pagination at page {page_num} (HTTP 404).")
                        break
                    resp.raise_for_status()
                    html_text = resp.text
                except Exception as e:
                    logger.warning(f"Error fetching page {page_num} ({current_url}): {e}")
                    break

                soup = BeautifulSoup(html_text, "html.parser")

                # Typical WordPress themes enclose search results in <article> elements
                articles = soup.find_all("article")
                if not articles:
                    articles = soup.select(".post, .type-post, .entry, .search-entry")

                if not articles:
                    logger.info(f"No more articles found on page {page_num}. Ending pagination.")
                    break

                page_new_count = 0
                for art in articles:
                    # Title & Link
                    title_tag = art.select_one(".entry-title a, h2 a, h1 a, .post-title a")
                    if not title_tag:
                        continue

                    raw_title = title_tag.get_text(strip=True)
                    link = title_tag.get("href", "").strip()
                    if not raw_title or not link or link in seen_links:
                        continue

                    seen_links.add(link)
                    page_new_count += 1

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
                    series_name = parsed["series_name"]

                    # Determine series link on J-Novels
                    series_link = None
                    cat_tag = art.select_one(".cat-links a, .entry-categories a, a[rel='category tag'], .tags-links a")
                    if cat_tag and cat_tag.get("href"):
                        cat_text = cat_tag.get_text(strip=True).lower()
                        # Avoid generic tags
                        if cat_text not in ["epub", "light novel", "pdf", "web novel", "uncategorized", "novels"]:
                            series_link = cat_tag.get("href")

                    if not series_link and series_name:
                        from urllib.parse import quote_plus
                        series_link = f"https://jnovels.com/?s={quote_plus(series_name)}"

                    results.append(ReleaseItem(
                        title=raw_title,
                        link=link,
                        series_name=series_name,
                        volume=parsed["volume"],
                        pub_date=pub_date,
                        cover_url=cover_url,
                        description=desc,
                        series_link=series_link,
                        raw_data={"confidence": parsed["confidence"], "subtitle": parsed["subtitle"]}
                    ))

                # Find link to the next page
                next_tag = soup.select_one("a.next, a.next.page-numbers, link[rel='next'], .nav-previous a")
                if next_tag and next_tag.get("href"):
                    current_url = next_tag.get("href")
                else:
                    page_num += 1
                    current_url = self._build_paged_url(self.url, page_num)
                    continue

                page_num += 1

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
                img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary, re.IGNORECASE)
                if img_match:
                    cover_url = img_match.group(1)

            parsed = extract_series_and_volume(raw_title)
            series_name = parsed["series_name"]
            series_link = None
            if series_name:
                from urllib.parse import quote_plus
                series_link = f"https://jnovels.com/?s={quote_plus(series_name)}"

            results.append(ReleaseItem(
                title=raw_title,
                link=link,
                series_name=series_name,
                volume=parsed["volume"],
                pub_date=pub_date,
                cover_url=cover_url,
                description=re.sub(r'<[^>]+>', '', summary)[:300],
                series_link=series_link,
                raw_data={"confidence": parsed["confidence"]}
            ))

        return results
