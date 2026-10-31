import re
import logging
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import urllib.request
try:
    import httpx
except ImportError:
    httpx = None
from bs4 import BeautifulSoup
from app.providers.base import BaseFeedProvider, ReleaseItem
from app.parser import extract_series_and_volume, is_pdf_release

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
    def clean_description_text(text: str) -> str:
        if not text:
            return ""
        # Strip leading "Description" or "Synopsis" labels and subsequent linebreaks
        cleaned = re.sub(r'^\s*(\*\*)?(description|synopsis|summary|story|about\s+the\s+book)(\*\*)?\s*[:\-–—]?\s*\n*', '', text, flags=re.I)
        cleaned = re.sub(r'AVAILABLE IN ZEROBOOKS APP.*?(iOS|\n|$)', '', cleaned, flags=re.I)
        cleaned = re.sub(r'DIGITAL EXCLUSIVE!?\s*', '', cleaned, flags=re.I)
        cleaned = re.sub(r'You can now Download .*? Epub', '', cleaned, flags=re.I)
        cleaned = re.sub(r'You can now download .*? Format', '', cleaned, flags=re.I)
        # Strip Associated Names / Related Series metadata from description body
        cleaned = re.split(r'\n\s*(associated\s+(names|titles)|related\s+series|genre\s*[:\-–—])', cleaned, flags=re.I)[0]
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
        return cleaned

    @staticmethod
    def parse_description_from_soup(orig_soup: BeautifulSoup) -> Optional[str]:
        """
        Extracts novel description/synopsis from J-Novels or WordPress DOM.
        Handles #editdescription, #description, #synopsis, headings, inline labels,
        and fallback narrative paragraph detection while strictly ignoring comments.
        """
        if not orig_soup:
            return None

        # Work on a cloned soup to avoid mutating caller DOM
        soup = BeautifulSoup(str(orig_soup), "html.parser")

        # 0. Strictly strip comments, user discussions, widgets, and footers
        for unwanted in soup.select(
            "#comments, .comments-area, .comment-list, .comment, .wpd-comment, "
            "#respond, .wpdiscuz_top_clearing, .post-meta-container, .sharedaddy, "
            ".jp-relatedposts, footer, script, style, nav, aside, .sidebar"
        ):
            unwanted.decompose()

        # 1. Direct container match (#editdescription, #description, #synopsis, .item-synopsis, etc.)
        desc_container = soup.select_one(
            "#editdescription, #description, #synopsis, .item-synopsis, .entry-description, div[id*='description'], div[id*='synopsis']"
        )
        if desc_container:
            c_id = (desc_container.get("id") or "").lower()
            c_class = " ".join(desc_container.get("class") or []).lower()
            if "site" not in c_class and "comment" not in c_id and "comment" not in c_class:
                text = desc_container.get_text(separator="\n", strip=True)
                cleaned = JNovelsProvider.clean_description_text(text)
                if len(cleaned) > 20:
                    return cleaned

        content = soup.select_one(".post-content, .entry-content, .entry, .post-inner, article") or soup
        keywords = ('description', 'synopsis', 'plot', 'summary', 'about the book', 'about the novel', 'story')

        # 2. Look for headings or labels in entry content
        for tag in content.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'b', 'span', 'p']):
            raw_t = tag.get_text(separator=' ', strip=True)
            t_lower = raw_t.lower().strip()

            matched_kw = None
            for kw in keywords:
                if t_lower == kw or t_lower.startswith(f"{kw}:") or t_lower.startswith(f"{kw} :") or t_lower.startswith(f"**{kw}") or t_lower.startswith(f"{kw} -") or t_lower.startswith(f"{kw} –"):
                    matched_kw = kw
                    break

            if matched_kw:
                paragraphs = []
                
                # Find the containing block element (usually <p>, <div>, or heading)
                block = tag.find_parent(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
                if not block:
                    block = tag

                # Check if block itself has text beyond the keyword
                block_text = block.get_text(separator="\n", strip=True)
                cleaned_block = re.sub(rf'^\s*(\*\*)?{matched_kw}(\*\*)?\s*[:\-–—]?\s*', '', block_text, flags=re.I).strip()
                if len(cleaned_block) > 25 and not cleaned_block.lower().startswith('download'):
                    if not re.match(r'^(associated\s+(names|titles)|genre)', cleaned_block, re.I):
                        paragraphs.append(cleaned_block)

                # Check subsequent siblings of the block
                curr = block.find_next_sibling()
                while curr and curr.name not in ('h1', 'h2', 'h3', 'h4', 'ol', 'ul', 'table'):
                    if curr.name in ('p', 'blockquote'):
                        ptxt = curr.get_text(separator="\n", strip=True)
                        p_lower = ptxt.lower().strip()
                        if any(p_lower.startswith(bad) for bad in ['download', 'zerobooks', 'available in zerobooks']):
                            break
                        if any(p_lower.startswith(bad) for bad in ['associated names', 'associated titles', 'genre', 'related series']):
                            curr = curr.find_next_sibling()
                            continue
                        if ptxt:
                            paragraphs.append(ptxt)
                    elif curr.name == 'div':
                        c_id = (curr.get('id') or '').lower()
                        c_cls = " ".join(curr.get('class') or []).lower()
                        if any(bad in c_cls for bad in ('share', 'sharedaddy', 'related', 'comments', 'meta', 'code-block', 'ai-track')):
                            break
                        dtxt = curr.get_text(separator="\n", strip=True)
                        if dtxt and len(dtxt) > 25:
                            paragraphs.append(dtxt)
                            break
                    curr = curr.find_next_sibling()

                if paragraphs:
                    result = "\n\n".join(paragraphs)
                    cleaned = JNovelsProvider.clean_description_text(result)
                    if len(cleaned) > 25:
                        return cleaned

        # 3. Fallback: Gather narrative paragraphs between intro/ads and download list
        candidate_paragraphs = []
        for p in content.find_all('p'):
            ptxt = p.get_text(separator="\n", strip=True)
            p_lower = ptxt.lower()
            if not ptxt or len(ptxt) < 35:
                continue
            if any(p_lower.startswith(bad) for bad in [
                'you can now download', 'you can download', 'download', 'available in zerobooks',
                'share this', 'subscribe', 'genre', 'associated names', 'associated titles', 'type your email'
            ]):
                continue
            if 'zerobooks' in p_lower:
                continue
            if p.find_parent(['ol', 'ul', 'form', 'nav', 'header', 'footer']):
                continue
            candidate_paragraphs.append(ptxt)
            if len(candidate_paragraphs) >= 4:
                break

        if candidate_paragraphs:
            result = "\n\n".join(candidate_paragraphs)
            cleaned = JNovelsProvider.clean_description_text(result)
            if len(cleaned) > 40:
                return cleaned

        return None

    @staticmethod
    def parse_cover_from_soup(target_soup: BeautifulSoup) -> Optional[str]:
        """Extracts high-resolution novel cover image, filtering out ads and icons."""
        for selector in ("img.wp-post-image", "img.attachment-post-thumbnail", ".post-thumbnail img", ".entry-content img", "article img", "img"):
            for img in target_soup.select(selector):
                src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
                if src and src.startswith("http") and not any(bad in src.lower() for bad in ("gravatar", "cbz-reader", "download.jpg", "zerobooks", "zero-book", "logo", "banner", "avatar", "support-us", ".gif")):
                    return src
        return None

    @staticmethod
    def extract_synopsis_and_original_link(url: str, series_name: Optional[str] = None) -> Dict[str, Optional[str]]:
        """
        Visits a J-Novels post or series page:
        1. Detects canonical 'Refer to original post' link if volume release.
        2. Extracts real novel description/synopsis from #editdescription, #synopsis, or content.
        3. Extracts high-resolution cover image.
        4. If synopsis or cover is missing from post, falls back to Kitsu / AniList public databases.
        """
        orig_link = None
        synopsis = None
        cover_url = None
        soup = None

        if httpx is not None:
            try:
                with httpx.Client(headers=DEFAULT_HEADERS, timeout=15.0, follow_redirects=True) as client:
                    resp = client.get(url)
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, "html.parser")
            except Exception as e:
                logger.debug(f"Error fetching {url} with httpx: {e}")
        else:
            try:
                req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    soup = BeautifulSoup(resp.read().decode('utf-8', errors='ignore'), "html.parser")
            except Exception as e:
                logger.debug(f"Error fetching {url} with urllib: {e}")

        if soup:
            # 1. Search for "Refer to original post" link
            for a in soup.find_all("a"):
                text = a.get_text(separator=' ', strip=True)
                href = (a.get("href") or "").strip()
                if re.search(r'refer\s+to\s+original\s+post|original\s+post|main\s+series\s+post|main\s+post', text, re.I):
                    if href.startswith("http") and "jnovels.com" in href:
                        orig_link = href
                        break

            # 2. Extract description from page
            synopsis = JNovelsProvider.parse_description_from_soup(soup)

            # 3. Extract cover image from page
            cover_url = JNovelsProvider.parse_cover_from_soup(soup)

            # If volume post didn't have synopsis or cover, but has orig_link, fetch orig_link
            if (not synopsis or not cover_url) and orig_link and orig_link != url:
                try:
                    soup_orig = None
                    if httpx is not None:
                        with httpx.Client(headers=DEFAULT_HEADERS, timeout=15.0, follow_redirects=True) as client:
                            resp_orig = client.get(orig_link)
                            if resp_orig.status_code == 200:
                                soup_orig = BeautifulSoup(resp_orig.text, "html.parser")
                    else:
                        req_orig = urllib.request.Request(orig_link, headers=DEFAULT_HEADERS)
                        with urllib.request.urlopen(req_orig, timeout=15) as resp_orig:
                            soup_orig = BeautifulSoup(resp_orig.read().decode('utf-8', errors='ignore'), "html.parser")

                    if soup_orig:
                        if not synopsis:
                            synopsis = JNovelsProvider.parse_description_from_soup(soup_orig)
                        if not cover_url:
                            cover_url = JNovelsProvider.parse_cover_from_soup(soup_orig)
                except Exception as e:
                    logger.debug(f"Error fetching canonical series link {orig_link}: {e}")

        # If url itself was already the main series page, orig_link is url
        final_series_link = orig_link or (url if not any(v in url.lower() for v in ('volume', 'vol-', 'vol.')) else None)

        # 4. PRIMARY: Query AniList & Kitsu for official metadata (prioritizing English title)
        search_title = series_name
        if not search_title and soup:
            title_tag = soup.select_one("h1.entry-title, h1.post-title, h1, title")
            if title_tag:
                search_title = title_tag.get_text(strip=True)
        if not search_title and url:
            parsed_slug = re.sub(r'https?://[^/]+/', '', url).replace('-', ' ').strip('/')
            search_title = parsed_slug

        meta = None
        if search_title:
            try:
                from app.providers.metadata import NovelMetadataProvider
                meta = NovelMetadataProvider.fetch_novel_metadata(search_title)
            except Exception as e:
                logger.debug(f"External metadata query failed for '{search_title}': {e}")

        # Use AniList/Kitsu metadata if found; otherwise retain source scraped values as fallback
        if meta and meta.get("synopsis"):
            synopsis = meta["synopsis"]
            logger.info(f"Enriched synopsis for '{search_title}' using {meta.get('source')}.")
        if meta and meta.get("cover_url"):
            cover_url = meta["cover_url"]
            logger.info(f"Enriched cover for '{search_title}' using {meta.get('source')}.")

        english_title = meta.get("title") if meta else None

        return {
            "series_link": final_series_link,
            "synopsis": synopsis,
            "cover_url": cover_url,
            "english_title": english_title,
            "metadata_source": meta.get("source") if meta else None
        }

    @staticmethod
    def fetch_master_index(index_url: str = "https://jnovels.com/light-novel-epub-jp/") -> List[ReleaseItem]:
        """
        Parses the complete series directory from J-Novels index page.
        Yields canonical light novel series directly with their main series URLs.
        """
        results = []
        seen_links = set()
        soup = None

        if httpx is not None:
            try:
                with httpx.Client(headers=DEFAULT_HEADERS, timeout=20.0, follow_redirects=True) as client:
                    resp = client.get(index_url)
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, "html.parser")
            except Exception as e:
                logger.debug(f"Error fetching master index with httpx: {e}")
        else:
            try:
                req = urllib.request.Request(index_url, headers=DEFAULT_HEADERS)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    soup = BeautifulSoup(resp.read().decode('utf-8', errors='ignore'), "html.parser")
            except Exception as e:
                logger.debug(f"Error fetching master index with urllib: {e}")

        if not soup:
            return results

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

                from app.providers.metadata import NovelMetadataProvider
                clean_series = NovelMetadataProvider.clean_novel_title(series_name)
                results.append(ReleaseItem(
                    title=clean_series,
                    link=href,
                    series_name=clean_series,
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

        while current_url and page_num <= self.max_pages:
            logger.debug(f"JNovels fetching page {page_num}: {current_url}")
            html_text = None
            if httpx is not None:
                try:
                    with httpx.Client(headers=DEFAULT_HEADERS, timeout=15.0, follow_redirects=True) as client:
                        resp = client.get(current_url)
                        if resp.status_code == 404:
                            logger.info(f"Reached end of pagination at page {page_num} (HTTP 404).")
                            break
                        resp.raise_for_status()
                        html_text = resp.text
                except Exception as e:
                    logger.warning(f"Error fetching page {page_num} ({current_url}) with httpx: {e}")
                    break
            else:
                try:
                    req = urllib.request.Request(current_url, headers=DEFAULT_HEADERS)
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        html_text = resp.read().decode('utf-8', errors='ignore')
                except Exception as e:
                    logger.warning(f"Error fetching page {page_num} ({current_url}) with urllib: {e}")
                    break

            if not html_text:
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

                    # Strictly ignore PDF releases
                    if is_pdf_release(raw_title, link):
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

            # Strictly ignore PDF releases
            if is_pdf_release(raw_title, link):
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
