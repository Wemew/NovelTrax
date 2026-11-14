from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from datetime import datetime
import hashlib

from app.providers.catchup import clamp_max_pages, latest_meta, truncate_newest_first

class ReleaseItem:
    def __init__(
        self,
        title: str,
        link: str,
        series_name: Optional[str] = None,
        volume: Optional[float] = None,
        pub_date: Optional[str] = None,
        cover_url: Optional[str] = None,
        description: Optional[str] = None,
        guid: Optional[str] = None,
        author: Optional[str] = None,
        series_link: Optional[str] = None,
        source_feed_name: Optional[str] = None,
        raw_data: Optional[Dict[str, Any]] = None,
        download_url: Optional[str] = None,
        release_group: Optional[str] = None,
        is_bundle: bool = False,
        volumes: Optional[List[float]] = None,
        volume_start: Optional[float] = None,
        volume_end: Optional[float] = None,
        source_link: Optional[str] = None,
    ):
        self.title = title.strip()
        self.link = link.strip()
        self.series_name = series_name
        self.volume = volume
        self.pub_date = pub_date or datetime.utcnow().isoformat()
        self.cover_url = cover_url
        self.description = description or ""
        self.author = author or ""
        self.series_link = series_link
        self.source_feed_name = source_feed_name or ""
        self.raw_data = raw_data or {}
        self.download_url = download_url
        self.release_group = release_group
        self.is_bundle = is_bundle
        self.volumes = volumes or ([volume] if volume is not None else [])
        self.volume_start = volume_start
        self.volume_end = volume_end
        # Direct download link behind a hider/shortener (jnovels "VOLUME n" anchor).
        self.source_link = str(source_link or "").strip()
        
        # Calculate consistent GUID if not provided
        if not guid:
            unique_str = f"{self.title}:{self.series_name}:{self.volume}:{self.link}"
            self.guid = hashlib.sha256(unique_str.encode("utf-8")).hexdigest()[:16]
        else:
            self.guid = guid

    def to_dict(self) -> Dict[str, Any]:
        return {
            "guid": self.guid,
            "title": self.title,
            "link": self.link,
            "series_name": self.series_name,
            "volume": self.volume,
            "pub_date": self.pub_date,
            "cover_url": self.cover_url,
            "description": self.description,
            "author": self.author,
            "series_link": self.series_link,
            "source_feed_name": self.source_feed_name,
            "download_url": self.download_url,
            "source_link": self.source_link,
            "release_group": self.release_group,
            "is_bundle": self.is_bundle,
            "volumes": self.volumes,
            "volume_start": self.volume_start,
            "volume_end": self.volume_end,
        }

class BaseFeedProvider(ABC):
    def __init__(self, name: str, url: str, config: Optional[Dict[str, Any]] = None):
        self.name = name
        self.url = url
        self.config = config or {}
        #: Additive diagnostics for the most recent :meth:`fetch_releases_since`
        #: call.  ``TaskManager`` uses ``reached_boundary`` to decide whether the
        #: scan watermark may advance and logs the rest (pages/extra items).
        self.last_scan_meta: Dict[str, Any] = {}

    @abstractmethod
    def fetch_releases(self) -> List[ReleaseItem]:
        """Fetch and return list of ReleaseItem instances from the source feed."""
        pass

    def fetch_releases_since(
        self,
        *,
        cutoff_iso: Optional[str] = None,
        stop_guid: Optional[str] = None,
        max_pages: Optional[int] = None,
    ) -> List[ReleaseItem]:
        """Best-effort catch-up scan for feeds that cannot paginate.

        The default implementation fetches the provider's normal window and
        then keeps only the newest-first run of items that are still newer than
        *cutoff_iso* and above *stop_guid*.  Providers that can page further
        backwards (Nyaa, J-Novels) override this to close the gap instead of
        merely trimming the visible window.

        ``fetch_releases()`` and every other existing method keep their old
        behaviour; this is purely additive.
        """
        items = list(self.fetch_releases() or [])
        kept, boundary = truncate_newest_first(items, cutoff_iso=cutoff_iso, stop_guid=stop_guid)
        self.last_scan_meta = latest_meta(
            reached_boundary=boundary is not None,
            pages=1,
            stop_reason=boundary or "single_window",
            max_pages=clamp_max_pages(max_pages),
            extra_pages=0,
            extra_items=0,
            candidates=len(items),
        )
        return kept


# Alias for compatibility with BaseProvider references
BaseProvider = BaseFeedProvider
