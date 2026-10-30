import os
import hashlib
import logging
from typing import Optional
import httpx

logger = logging.getLogger("noveltrax.covers")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Referer": "https://jnovels.com/"
}

class CoverCache:
    def __init__(self, data_dir: str):
        self.covers_dir = os.path.join(data_dir, "covers")
        os.makedirs(self.covers_dir, exist_ok=True)

    def get_filename(self, url: str) -> str:
        """Derive a safe local filename based on the SHA-256 hash of the URL."""
        ext = ".jpg"
        clean_url = url.split("?")[0].lower()
        if clean_url.endswith(".png"):
            ext = ".png"
        elif clean_url.endswith(".webp"):
            ext = ".webp"
        elif clean_url.endswith(".jpeg"):
            ext = ".jpeg"
            
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return f"{url_hash}{ext}"

    def get_path(self, url: str) -> str:
        return os.path.join(self.covers_dir, self.get_filename(url))

    def is_cached(self, url: str) -> bool:
        if not url:
            return False
        path = self.get_path(url)
        return os.path.isfile(path) and os.path.getsize(path) > 0

    async def get_or_download(self, url: str) -> Optional[str]:
        """Returns the local path to the cached cover image, downloading it if necessary."""
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            return None

        filepath = self.get_path(url)
        if os.path.isfile(filepath) and os.path.getsize(filepath) > 0:
            return filepath

        # Download and cache
        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and len(resp.content) > 100:
                    with open(filepath, "wb") as f:
                        f.write(resp.content)
                    logger.debug(f"Cached cover from {url} to {filepath}")
                    return filepath
                else:
                    logger.warning(f"Failed to fetch image from {url}: HTTP {resp.status_code}")
                    return None
        except Exception as e:
            logger.warning(f"Error caching cover from {url}: {e}")
            return None

    def cache_sync(self, url: str) -> Optional[str]:
        """Synchronous version for background scheduler jobs."""
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            return None

        filepath = self.get_path(url)
        if os.path.isfile(filepath) and os.path.getsize(filepath) > 0:
            return filepath

        try:
            with httpx.Client(headers=HEADERS, timeout=10.0, follow_redirects=True) as client:
                resp = client.get(url)
                if resp.status_code == 200 and len(resp.content) > 100:
                    with open(filepath, "wb") as f:
                        f.write(resp.content)
                    return filepath
        except Exception as e:
            logger.debug(f"Sync cover cache error for {url}: {e}")
            return None
