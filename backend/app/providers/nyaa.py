"""
Nyaa Light Novel Provider for NovelTrax.
Scrapes and monitors Nyaa (Category 3_1: Literature - English-translated).
Supports:
  1. RSS Feed monitoring via https://nyaa.si/?page=rss&c=3_1
  2. Multi-page HTML crawling and targeted series search via https://nyaa.si/?f=0&c=3_1&q={query}
  3. SSRF-safe HTTP fetching via app.security.safe_http_get
  4. Rich torrent metadata extraction (magnet, .torrent URL, size, seeds, leeches, date)
  5. Title, bundle, and release group parsing via app.parser
"""

import re
import time
import logging
import urllib.parse
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Callable

import feedparser
from bs4 import BeautifulSoup

from app.providers.base import BaseFeedProvider, BaseProvider, ReleaseItem
from app.providers.catchup import (
    clamp_max_pages,
    dedupe_by_identity,
    latest_meta,
    parse_timestamp,
    truncate_newest_first,
)
from app.parser import extract_series_and_volume, is_pdf_release, extract_release_group
from app.security import safe_http_get, is_safe_public_url

logger = logging.getLogger("noveltrax.providers.nyaa")

NYAA_BASE_URL = "https://nyaa.si"
NYAA_CATEGORY_LIT_ENG = "3_1"  # Literature - English-translated
DEFAULT_FEED_URL = f"{NYAA_BASE_URL}/?page=rss&c={NYAA_CATEGORY_LIT_ENG}"
DEFAULT_SEARCH_URL = f"{NYAA_BASE_URL}/?f=0&c={NYAA_CATEGORY_LIT_ENG}"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
}


class NyaaNovelProvider(BaseFeedProvider):
    """
    Dedicated zero-authentication Nyaa provider for English Light Novel releases.
    Targets Category 3_1 (Literature - English-translated).
    """

    def __init__(
        self,
        name: str = "Nyaa Light Novels",
        url: str = DEFAULT_FEED_URL,
        config: Optional[Dict[str, Any]] = None
    ):
        super().__init__(name, url, config)
        self.category = str(self.config.get("category", NYAA_CATEGORY_LIT_ENG))
        self.max_pages = int(self.config.get("max_pages", 2))
        self.delay_seconds = float(self.config.get("delay_seconds", 1.5))
        self.query = self.config.get("query", "")

    def fetch_releases(self) -> List[ReleaseItem]:
        """BaseFeedProvider entrypoint: checks feed or runs query crawl."""
        if self.query:
            return self.crawl_pages(max_pages=self.max_pages, query=self.query)
        return self.check_feed()

    def check_feed(self) -> List[ReleaseItem]:
        """
        Polls Nyaa RSS feed for latest releases in category 3_1.
        Falls back to HTML crawl if RSS feed is unavailable or empty.
        """
        feed_url = self.url if "page=rss" in self.url else f"{NYAA_BASE_URL}/?page=rss&c={self.category}"
        try:
            logger.info(f"Checking Nyaa RSS feed at {feed_url}...")
            releases = self._fetch_rss(feed_url)
            if releases:
                logger.info(f"Nyaa RSS feed fetched {len(releases)} releases.")
                return releases
            logger.info("Nyaa RSS returned 0 entries; falling back to HTML crawl.")
        except Exception as e:
            logger.warning(f"Error fetching Nyaa RSS feed ({e}); falling back to HTML crawl.")

        # HTML fallback
        return self.crawl_pages(max_pages=self.max_pages)

    def _fetch_rss(self, feed_url: str) -> List[ReleaseItem]:
        """
        SSRF-safe RSS XML fetcher and parser using safe_http_get + feedparser in-memory.
        """
        resp = safe_http_get(feed_url, timeout=15.0, headers=DEFAULT_HEADERS)
        feed = feedparser.parse(resp.text)

        if feed.bozo and not feed.entries:
            logger.warning(f"Feedparser error on Nyaa RSS: {feed.bozo_exception}")
            return []

        results: List[ReleaseItem] = []
        seen_links = set()

        for entry in feed.entries:
            raw_title = entry.get("title", "").strip()
            raw_link = entry.get("link", "").strip()
            guid = entry.get("id") or entry.get("guid") or ""
            summary = entry.get("summary") or entry.get("description") or ""

            if not raw_title:
                continue

            # Link disambiguation: web permalink vs download url
            web_post_link = ""
            download_url = None

            if guid.startswith("http") and not guid.endswith(".torrent") and not guid.startswith("magnet:"):
                web_post_link = guid

            if not web_post_link and summary:
                view_match = re.search(r'href=["\'](https?://[^"\']*/view/\d+[^"\']*)["\']', summary)
                if view_match:
                    web_post_link = view_match.group(1)

            torrent_url = ""
            magnet_url = ""

            if raw_link.endswith(".torrent") or "/download/" in raw_link:
                torrent_url = raw_link
                if not web_post_link:
                    web_post_link = re.sub(r"/download/(\d+)(?:\.torrent)?", r"/view/\1", raw_link)
            elif raw_link.startswith("magnet:"):
                magnet_url = raw_link
            elif not web_post_link:
                web_post_link = raw_link

            if not web_post_link:
                web_post_link = raw_link

            # Check enclosures
            if not download_url and "enclosures" in entry and entry.enclosures:
                for enc in entry.enclosures:
                    href = enc.get("href", "")
                    enc_type = enc.get("type", "")
                    if "torrent" in enc_type or href.endswith(".torrent"):
                        torrent_url = href
                        break
                    elif href.startswith("magnet:"):
                        magnet_url = href
                        break

            # Check tracker specific fields (nyaa_infohash)
            info_hash = entry.get("nyaa_infohash", "")
            if not magnet_url and info_hash:
                magnet_url = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(raw_title)}"

            download_url = magnet_url or torrent_url or raw_link

            if not web_post_link or web_post_link in seen_links:
                continue
            seen_links.add(web_post_link)

            # Strictly reject PDF releases
            if is_pdf_release(raw_title, web_post_link, guid):
                continue

            # Publication Date
            pub_date = entry.get("published")
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    pub_date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
                except Exception:
                    pass
            if not pub_date:
                pub_date = datetime.now(timezone.utc).isoformat()

            # Seeders, Leechers, Size
            seeders = None
            if "nyaa_seeders" in entry:
                try:
                    seeders = int(entry.nyaa_seeders)
                except (ValueError, TypeError):
                    pass

            leechers = None
            if "nyaa_leechers" in entry:
                try:
                    leechers = int(entry.nyaa_leechers)
                except (ValueError, TypeError):
                    pass

            post_size = entry.get("nyaa_size", "")

            # Title & Bundle Parsing
            parsed = extract_series_and_volume(raw_title)
            rel_group = extract_release_group(raw_title)

            meta_desc = f"Size: {post_size}" if post_size else ""
            if seeders is not None:
                meta_desc += f" | Seeders: {seeders}"
            if leechers is not None:
                meta_desc += f" | Leechers: {leechers}"

            results.append(ReleaseItem(
                title=raw_title,
                link=web_post_link,
                series_name=parsed["series_name"],
                volume=parsed["volume"],
                pub_date=pub_date,
                description=meta_desc,
                guid=guid or web_post_link,
                source_feed_name=self.name,
                download_url=download_url,
                release_group=rel_group,
                is_bundle=parsed.get("is_bundle", False),
                volumes=parsed.get("volumes", []),
                volume_start=parsed.get("volume_start"),
                volume_end=parsed.get("volume_end"),
                raw_data={
                    "confidence": parsed["confidence"],
                    "post_size": post_size,
                    "seeders": seeders,
                    "leechers": leechers,
                    "info_hash": info_hash,
                    "torrent_url": torrent_url,
                    "magnet_url": magnet_url,
                    "subtitle": parsed.get("subtitle", "")
                }
            ))

        return results

    def search_series(
        self,
        series_title: str,
        max_pages: int = 3,
        alt_names: Optional[List[str]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        date_cutoff: Optional[datetime] = None
    ) -> List[ReleaseItem]:
        """
        Executes targeted search on Nyaa for a light novel series.
        Searches primary title and optional alternate titles.
        """
        clean_name = series_title.strip()
        if not clean_name:
            return []

        all_results: List[ReleaseItem] = []
        seen_links = set()

        if stop_check and stop_check():
            return []

        # 1. Search primary title
        primary_results = self.crawl_pages(
            max_pages=max_pages,
            query=clean_name,
            date_cutoff=date_cutoff,
            stop_check=stop_check
        )
        for r in primary_results:
            if r.link not in seen_links:
                seen_links.add(r.link)
                all_results.append(r)

        # 2. Search alternate aliases
        if alt_names:
            for alt in alt_names:
                if stop_check and stop_check():
                    break
                alt_clean = (alt or "").strip()
                if alt_clean and alt_clean.lower() != clean_name.lower():
                    time.sleep(self.delay_seconds)
                    if stop_check and stop_check():
                        break
                    alt_res = self.crawl_pages(
                        max_pages=max_pages,
                        query=alt_clean,
                        date_cutoff=date_cutoff,
                        stop_check=stop_check
                    )
                    for r in alt_res:
                        if r.link not in seen_links:
                            seen_links.add(r.link)
                            all_results.append(r)

        return all_results

    def fetch_releases_since(
        self,
        *,
        cutoff_iso: Optional[str] = None,
        stop_guid: Optional[str] = None,
        max_pages: Optional[int] = None,
    ) -> List[ReleaseItem]:
        """Catch-up scan: RSS first, then keep crawling backwards to the cutoff.

        Nyaa RSS entries are timestamped and every HTML table row carries a
        ``data-timestamp`` used by :meth:`crawl_pages`' existing ``date_cutoff``
        support, so the scan stops as soon as a release older than the previous
        scan is reached.  ``max_pages`` bounds the total work.
        """
        page_limit = clamp_max_pages(max_pages)
        cutoff_dt = parse_timestamp(cutoff_iso)

        if self.query:
            # Query crawls have no RSS window; page with the same cutoff logic.
            items = self.crawl_pages(
                max_pages=page_limit, query=self.query, date_cutoff=cutoff_dt
            )
            return self._finish_catchup(items, cutoff_iso, stop_guid, page_limit)

        feed_url = self.url if "page=rss" in self.url else f"{NYAA_BASE_URL}/?page=rss&c={self.category}"
        rss_items: List[ReleaseItem] = []
        try:
            rss_items = self._fetch_rss(feed_url)
        except Exception as exc:
            logger.warning(f"Error fetching Nyaa RSS feed during catch-up ({exc}); crawling HTML instead.")

        if not rss_items:
            # RSS unavailable or empty: crawl like fetch_releases does, but with
            # the previous scan's cutoff as the stopping point.
            items = self.crawl_pages(max_pages=page_limit, date_cutoff=cutoff_dt)
            return self._finish_catchup(items, cutoff_iso, stop_guid, page_limit)

        # The RSS window is the newest page.  If it already contains the
        # previous scan's boundary no HTML crawl is needed at all.
        kept, boundary = truncate_newest_first(rss_items, cutoff_iso=cutoff_iso, stop_guid=stop_guid)
        if boundary is not None:
            self.last_crawl_meta = {"pages_scanned": 0, "stop_reason": None, "items": 0}
            self.last_scan_meta = latest_meta(
                reached_boundary=True,
                pages=1,
                stop_reason=boundary,
                max_pages=page_limit,
                extra_pages=0,
                extra_items=0,
                candidates=len(rss_items),
            )
            return kept

        remaining_pages = page_limit - 1
        if remaining_pages <= 0:
            # A one-page catch-up budget cannot look past the RSS window.
            self.last_scan_meta = latest_meta(
                reached_boundary=False,
                pages=1,
                stop_reason="page_cap",
                max_pages=page_limit,
                extra_pages=0,
                extra_items=0,
                candidates=len(rss_items),
            )
            return rss_items

        # Every RSS item is still newer than the cutoff: page further back.
        # Crawl from page 1 (the RSS entry count does not match the HTML page
        # size exactly) and de-duplicate against the RSS window.
        crawl_items = self.crawl_pages(
            max_pages=remaining_pages, start_page=1, date_cutoff=cutoff_dt
        )
        merged = dedupe_by_identity(list(rss_items) + list(crawl_items))
        return self._finish_catchup(
            merged, cutoff_iso, stop_guid, page_limit, base_pages=1
        )

    def _finish_catchup(
        self,
        items: List[ReleaseItem],
        cutoff_iso: Optional[str],
        stop_guid: Optional[str],
        page_limit: int,
        base_pages: int = 0,
    ) -> List[ReleaseItem]:
        """Truncates a catch-up window and records the scan diagnostics."""
        crawl_meta = dict(getattr(self, "last_crawl_meta", None) or {})
        crawl_pages = int(crawl_meta.get("pages_scanned") or 0)
        total_pages = crawl_pages + max(0, int(base_pages))
        kept, boundary = truncate_newest_first(items, cutoff_iso=cutoff_iso, stop_guid=stop_guid)
        reason = boundary or crawl_meta.get("stop_reason")
        reached = boundary is not None or reason in ("cutoff", "end_of_feed")
        self.last_scan_meta = latest_meta(
            reached_boundary=reached,
            pages=total_pages,
            stop_reason=reason,
            max_pages=page_limit,
            extra_pages=max(0, total_pages - self.max_pages),
            extra_items=max(0, len(items) - len(kept)),
            candidates=len(items),
        )
        return kept

    def crawl_pages(
        self,
        max_pages: int = 1,
        start_page: int = 1,
        query: Optional[str] = None,
        date_cutoff: Optional[datetime] = None,
        delay: Optional[float] = None,
        progress_cb: Optional[Any] = None,
        stop_check: Optional[Callable[[], bool]] = None
    ) -> List[ReleaseItem]:
        """
        Sequentially crawls Nyaa HTML table pages with polite rate-limiting.
        Stops if reaching date_cutoff or when end of results is reached.
        """
        delay_sec = delay if delay is not None else self.delay_seconds
        results: List[ReleaseItem] = []
        seen_links = set()
        pages_scanned = 0
        stop_reason: Optional[str] = None
        self.last_crawl_meta = {"pages_scanned": 0, "stop_reason": None, "items": 0}

        for page in range(start_page, start_page + max_pages):
            if stop_check and stop_check():
                logger.info("Nyaa crawl stop requested by caller.")
                stop_reason = "stop_requested"
                break

            params = {
                "f": "0",
                "c": self.category,
                "s": "id",
                "o": "desc",
                "p": str(page)
            }
            if query:
                params["q"] = query

            page_url = f"{NYAA_BASE_URL}/?{urllib.parse.urlencode(params)}"
            if progress_cb:
                progress_cb(page, max_pages, page_url)

            try:
                resp = safe_http_get(page_url, timeout=15.0, headers=DEFAULT_HEADERS)
                if resp.status_code == 404:
                    logger.info(f"Nyaa reached end of results at page {page}.")
                    stop_reason = "end_of_feed"
                    break
                html = resp.text
            except Exception as e:
                logger.warning(f"Error fetching Nyaa page {page} ({page_url}): {e}")
                stop_reason = "error"
                break

            pages_scanned += 1
            soup = BeautifulSoup(html, "html.parser")
            rows = soup.select("table.torrent-list tbody tr")
            if not rows:
                logger.info(f"No torrent rows found on Nyaa page {page}. End of search.")
                stop_reason = "end_of_feed"
                break

            reached_date_cutoff = False
            for r in rows:
                # Column 2: Title & View Link (exclude comments icon)
                title_links = [a for a in r.select("td:nth-of-type(2) a") if not a.get("class") or "comments" not in a.get("class", [])]
                if not title_links:
                    continue
                title_el = title_links[-1]
                raw_title = title_el.get_text(strip=True)
                view_href = title_el.get("href", "")
                view_url = f"{NYAA_BASE_URL}{view_href}" if view_href.startswith("/") else view_href

                if not raw_title or not view_url or view_url in seen_links:
                    continue
                seen_links.add(view_url)

                # Strictly reject PDF releases
                if is_pdf_release(raw_title, view_url):
                    continue

                # Column 3: Torrent & Magnet Links
                dl_links = r.select("td:nth-of-type(3) a")
                torrent_url = ""
                magnet_url = ""
                for a in dl_links:
                    h = a.get("href", "")
                    if h.endswith(".torrent") or "/download/" in h:
                        torrent_url = f"{NYAA_BASE_URL}{h}" if h.startswith("/") else h
                    elif h.startswith("magnet:"):
                        magnet_url = h

                download_url = magnet_url or torrent_url or view_url

                # Column 4: File Size
                size_td = r.select_one("td:nth-of-type(4)")
                post_size = size_td.get_text(strip=True) if size_td else ""

                # Column 5: Date & Cutoff Check
                date_td = r.select_one("td:nth-of-type(5)")
                pub_date = date_td.get_text(strip=True) if date_td else ""
                raw_timestamp = date_td.get("data-timestamp") if date_td else None

                if date_cutoff and raw_timestamp:
                    try:
                        item_dt = datetime.fromtimestamp(int(raw_timestamp), tz=timezone.utc)
                        cutoff_dt = date_cutoff if date_cutoff.tzinfo else date_cutoff.replace(tzinfo=timezone.utc)
                        if item_dt < cutoff_dt:
                            reached_date_cutoff = True
                            logger.info(f"Reached date cutoff {cutoff_dt} at release {item_dt}. Stopping crawl.")
                            break
                    except Exception:
                        pass

                # Column 6 & 7: Seeders & Leechers
                seeds_td = r.select_one("td:nth-of-type(6)")
                seeders_val = None
                if seeds_td:
                    try:
                        seeders_val = int(seeds_td.get_text(strip=True))
                    except (ValueError, TypeError):
                        pass

                leeches_td = r.select_one("td:nth-of-type(7)")
                leechers_val = None
                if leeches_td:
                    try:
                        leechers_val = int(leeches_td.get_text(strip=True))
                    except (ValueError, TypeError):
                        pass

                # Title & Bundle Parsing
                parsed = extract_series_and_volume(raw_title)
                rel_group = extract_release_group(raw_title)

                meta_desc = f"Size: {post_size}" if post_size else ""
                if seeders_val is not None:
                    meta_desc += f" | Seeders: {seeders_val}"
                if leechers_val is not None:
                    meta_desc += f" | Leechers: {leechers_val}"

                results.append(ReleaseItem(
                    title=raw_title,
                    link=view_url,
                    series_name=parsed["series_name"],
                    volume=parsed["volume"],
                    pub_date=pub_date or datetime.now(timezone.utc).isoformat(),
                    description=meta_desc,
                    guid=view_url,
                    source_feed_name=self.name,
                    download_url=download_url,
                    release_group=rel_group,
                    is_bundle=parsed.get("is_bundle", False),
                    volumes=parsed.get("volumes", []),
                    volume_start=parsed.get("volume_start"),
                    volume_end=parsed.get("volume_end"),
                    raw_data={
                        "confidence": parsed["confidence"],
                        "post_size": post_size,
                        "seeders": seeders_val,
                        "leechers": leechers_val,
                        "torrent_url": torrent_url,
                        "magnet_url": magnet_url,
                        "subtitle": parsed.get("subtitle", "")
                    }
                ))

            if reached_date_cutoff:
                stop_reason = "cutoff"
                break

            if page < (start_page + max_pages - 1) and delay_sec > 0:
                time.sleep(delay_sec)

        if stop_reason is None:
            stop_reason = "page_cap"
        self.last_crawl_meta = {
            "pages_scanned": pages_scanned,
            "stop_reason": stop_reason,
            "items": len(results),
        }
        return results
