from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from datetime import datetime
import hashlib

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
        volume_end: Optional[float] = None
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

    @abstractmethod
    def fetch_releases(self) -> List[ReleaseItem]:
        """Fetch and return list of ReleaseItem instances from the source feed."""
        pass

# Alias for compatibility with BaseProvider references
BaseProvider = BaseFeedProvider
