from typing import Dict, Any, Type
from app.providers.base import BaseFeedProvider
from app.providers.jnovels import JNovelsProvider
from app.providers.rss import RSSFeedProvider
from app.providers.html_scraper import HtmlScraperProvider
from app.providers.novelupdates import NovelUpdatesProvider

PROVIDER_MAP: Dict[str, Type[BaseFeedProvider]] = {
    "jnovels": JNovelsProvider,
    "rss": RSSFeedProvider,
    "html_scraper": HtmlScraperProvider,
    "novelupdates": NovelUpdatesProvider,
}

def create_provider(feed_type: str, name: str, url: str, config: Dict[str, Any] = None) -> BaseFeedProvider:
    """Instantiate appropriate feed provider based on feed_type."""
    provider_cls = PROVIDER_MAP.get(feed_type.lower())
    if not provider_cls:
        # Default to RSS if URL indicates an RSS or Atom feed, otherwise HTML scraper
        if "rss" in url.lower() or "feed" in url.lower() or url.endswith(".xml"):
            provider_cls = RSSFeedProvider
        elif "jnovels.com" in url.lower():
            provider_cls = JNovelsProvider
        elif "novelupdates.com" in url.lower():
            provider_cls = NovelUpdatesProvider
        else:
            provider_cls = HtmlScraperProvider

    return provider_cls(name=name, url=url, config=config or {})
