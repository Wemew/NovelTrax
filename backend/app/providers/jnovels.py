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

    @staticmethod
    def extract_synopsis_and_original_link(url: str) -> Dict[str, Optional[str]]:
        """
        Visits a J-Novels post or series page:
        1. Detects canonical 'Refer to original post' link.
        2. Extracts real novel synopsis from the 'Synopsis' section.
        3. Extracts high-resolution cover image.
        """
        orig_link = None
        synopsis = None
        cover_url = None

        def parse_synopsis_from_soup(target_soup):
            syn_header = target_soup.find(
                lambda tag: tag.name in ('h1', 'h2', 'h3', 'h4', 'strong', 'b', 'p') 
                and 'synopsis' in tag.get_text(strip=True).lower()
            )
            if not syn_header:
                return None
            
            paragraphs = []
            curr = syn_header.find_next_sibling()
            while curr and curr.name not in ('ol', 'ul', 'h1', 'h2', 'h3'):
                if curr.name == 'p':
                    p_text = curr.get_text(strip=True)
                    if p_text:
                        p_clean = re.sub(r'^(DIGITAL EXCLUSIVE!?|AVAILABLE IN ZEROBOOKS APP.*?)\s*', '', p_text, flags=re.I).strip()
                        if p_clean and not p_clean.lower().startswith('download'):
                            paragraphs.append(p_clean)
                elif curr.name == 'div' and any(c in (curr.get('class') or []) for c in ('share', 'sharedaddy', 'related')):
                    break
                curr = curr.find_next_sibling()
            
            return '\n\n'.join(paragraphs) if paragraphs else None

        with httpx.Client(headers=DEFAULT_HEADERS, timeout=12.0, follow_redirects=True) as client:
            try:
                resp = client.get(url)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    
                    # 1. Search for "Refer to original post" link
                    for a in soup.find_all("a"):
                        text = a.get_text(strip=True)
                        if re.search(r'refer\s+to\s+original\s+post', text, re.I):
                            orig_link = a.get("href")
                            break
                    
                    # 2. Extract synopsis
                    synopsis = parse_synopsis_from_soup(soup)

                    # 3. Extract cover image
                    article = soup.select_one("article, .post-content, .entry-content")
                    if article:
                        for img in article.find_all("img"):
                            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
                            if src and not any(bad in src.lower() for bad in ("gravatar", "cbz-reader", "download.jpg", "zerobooks", "logo")):
                                cover_url = src
                                break

                    # If volume post didn't have synopsis but has orig_link, fetch orig_link
                    if not synopsis and orig_link:
                        try:
                            resp_orig = client.get(orig_link)
                            if resp_orig.status_code == 200:
                                soup_orig = BeautifulSoup(resp_orig.text, "html.parser")
                                synopsis = parse_synopsis_from_soup(soup_orig)
                                if not cover_url:
                                    art_orig = soup_orig.select_one("article, .post-content, .entry-content")
                                    if art_orig:
                                        for img in art_orig.find_all("img"):
                                            src = img.get("src") or img.get("data-src")
                                            if src and not any(bad in src.lower() for bad in ("gravatar", "cbz-reader", "download.jpg", "zerobooks")):
                                                cover_url = src
                                                break
                        except Exception as e:
                            logger.debug(f"Error fetching canonical series link {orig_link}: {e}")

            except Exception as e:
                logger.debug(f"Error extracting details from {url}: {e}")

        return {
            "series_link": orig_link,
            "synopsis": synopsis,
            "cover_url": cover_url
        }

    def fetch_master_index(self, index_url: str = "https://jnovels.com/light-novel-epub-jp/") -> List[ReleaseItem]:
        """
        Parses the complete series directory from J-Novels index page.
        Yields canonical light novel series directly with their main series URLs.
        """
        results = []
        seen_links = set()
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=20.0, follow_redirects=True) as client:
            resp = client.get(index_url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            content = soup.select_one(".entry-content, article, .post-content")
            if not content:
                return results

            for a in content.find_all("a"):
                href = (a.get("href") or "").strip()
                raw_text = a.get_text(strip=True)
                if not href or not raw_text or not href.startswith("https://jnovels.com/"):
                    continue
                if any(bad in href for bad in ("mode=", "app.adjust", "/faq", "/category/", "/tag/", "/contact", "/author/")):
                    continue
                if href == "https://jnovels.com/" or href in seen_links:
                    continue

                seen_links.add(href)
                parsed = extract_series_and_volume(raw_text)
                series_name = parsed["series_name"]
                if not series_name:
                    continue

                results.append(ReleaseItem(
                    title=f"{series_name} (Light Novel)",
                    link=href,
                    series_name=series_name,
                    volume=parsed["volume"] or 1.0,
                    series_link=href,
                    source_feed_name="J-Novels Master Index",
                    raw_data={"is_master_index": True}
                ))

        logger.info(f"Fetched {len(results)} series from J-Novels Master Index ({index_url}).")
        return results

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
